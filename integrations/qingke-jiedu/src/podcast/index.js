import fs from 'node:fs/promises';
import path from 'node:path';
import { config } from '../config.js';
import { fetchArxivMeta } from '../arxiv.js';
import { fetchArxivSource } from '../arxivSource.js';
import { pdfToImages } from '../pdfToImages.js';
import { webToImages } from '../webToImages.js';
import { fetchSource } from '../fetchSource.js';
import { extractWebpage } from '../extractText.js';
import { generateScript } from './writer.js';
import { synthesizeScenes } from './tts.js';
import { prepareVisuals } from './frames.js';
import { buildVideo } from './compose.js';
import { checkStyle } from '../styleCheck.js';

export const PODCAST_OUT = path.join(config.outputDir, 'podcast');

/** 下载远程图片到本地文件（供画面用）；失败返回 null。 */
async function downloadTo(url, file, timeoutMs = 30000) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    if (!res.ok) return null;
    const buf = Buffer.from(await res.arrayBuffer());
    if (!buf.length) return null;
    await fs.writeFile(file, buf);
    return file;
  } catch {
    return null;
  }
}

async function fetchPdfBuffer(url) {
  const res = await fetch(url, {
    redirect: 'follow',
    signal: AbortSignal.timeout(config.fetchTimeoutMs),
    headers: { 'User-Agent': config.userAgent },
  });
  if (!res.ok) throw new Error(`下载 PDF 失败 HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  if (buf.subarray(0, 5).toString() !== '%PDF-') throw new Error('响应不是 PDF');
  return buf;
}

/** 第 1 步：抽取素材（论文属性 / 正文 / 图片 / 页面图）。 */
async function extractAssets(url, workDir, log, emit) {
  const isArxiv = /arxiv\.org\/(abs|pdf|html)\/\d{4}\.\d{4,5}/.test(url);
  const figDir = path.join(workDir, 'figures');
  const pageDir = path.join(workDir, 'pages');
  await fs.mkdir(figDir, { recursive: true });
  await fs.mkdir(pageDir, { recursive: true });

  if (isArxiv) {
    log('检测到 arXiv 链接：抽取 HTML 图片 + PDF 整页');
    const arxiv = await fetchArxivMeta(url);
    const src = await fetchArxivSource(url, log);
    const text = src.text || '';

    // 图片：HTML CDN 图 / SVG 栅格化图 / TeX 源码图（已本地化的直接复制，远程的下载）
    const figures = [];
    for (let i = 0; i < (src.figures || []).length; i++) {
      const f = src.figures[i];
      const file = path.join(figDir, `fig_${String(i + 1).padStart(2, '0')}.png`);
      let local = null;
      if (f.path) {
        try {
          await fs.copyFile(f.path, file);
          local = file;
        } catch {
          local = null;
        }
      } else if (f.url) {
        local = await downloadTo(f.url, file);
      }
      if (local) {
        figures.push({ num: figures.length + 1, caption: f.caption || '', page: null, url: f.url, path: local });
      } else {
        log(`⚠️ 图片 ${i + 1} 不可用，跳过`);
      }
    }

    // PDF 整页（开场封面 + 无图场景画面 + 背景）
    const pages = [];
    let coverPath = null;
    try {
      emit({ stage: 'pages' });
      const pdf = await fetchPdfBuffer(`https://arxiv.org/pdf/${src.id}`);
      const rendered = await pdfToImages(pdf);
      const cap = Math.min(rendered.length, config.podcastMaxPages);
      for (let i = 0; i < cap; i++) {
        const file = path.join(pageDir, `page_${String(i + 1).padStart(2, '0')}.png`);
        await fs.writeFile(file, rendered[i].buffer);
        pages.push(file);
      }
      coverPath = pages[0] || null;
      log(`PDF 整页渲染完成：${pages.length} 页`);
    } catch (err) {
      log(`⚠️ PDF 整页不可用（${err.message}），仅用图片画面`);
    }

    return {
      sourceType: 'arxiv',
      url: `https://arxiv.org/abs/${src.id}`,
      title: arxiv?.title || src.title || src.id,
      authors: arxiv?.authors || '',
      institution: '',
      date: arxiv?.published ? `${Number(String(arxiv.published).slice(0, 4))}年${Number(String(arxiv.published).slice(5, 7))}月` : '',
      abstract: text.slice(0, 1000),
      text,
      figures,
      pages,
      coverPath,
    };
  }

  // 普通网页：正文 + 整页截图（作为"页面"画面）
  log('检测到普通链接：抽取正文与整页截图');
  emit({ stage: 'web' });
  const raw = await fetchSource(url);
  const finalUrl = raw.finalUrl || url;
  let text = '';
  let title = '';
  try {
    const meta = extractWebpage(raw.html || '', finalUrl);
    title = meta.title || '';
    text = meta.text || '';
  } catch {
    /* text 尽力而为 */
  }
  const pages = [];
  let coverPath = null;
  try {
    const shots = await webToImages(finalUrl);
    const cap = Math.min(shots.length, config.podcastMaxPages);
    for (let i = 0; i < cap; i++) {
      const file = path.join(pageDir, `page_${String(i + 1).padStart(2, '0')}.png`);
      await fs.writeFile(file, shots[i].buffer);
      pages.push(file);
    }
    coverPath = pages[0] || null;
    log(`网页整页截图完成：${pages.length} 张`);
  } catch (err) {
    log(`⚠️ 网页截图失败（${err.message}）`);
  }
  return {
    sourceType: 'web',
    url: finalUrl,
    title: title || finalUrl,
    authors: '',
    institution: '',
    date: '',
    abstract: text.slice(0, 1000),
    text,
    figures: [], // 网页暂不做图注裁剪，画面走整页截图
    pages,
    coverPath,
  };
}

/** 播客总编排：素材 → 脚本 → 配音 → 画面 → 合成。onProgress(stage, detail)。 */
export async function generatePodcast({ url, providerName, model, duration = 'auto', voice, engine }, onProgress, log = () => {}) {
  const id = crypto.randomUUID();
  const workDir = path.join(PODCAST_OUT, id);
  await fs.mkdir(workDir, { recursive: true });

  const emit = (stage, detail = '') => {
    if (onProgress) onProgress(stage, detail);
  };

  emit('extract', '正在抽取论文内容与图片…');
  const assets = await extractAssets(url, workDir, log, emit);

  emit('write', '写稿模型正在撰写播客脚本…');
  const script = await generateScript(assets, { providerName, model, duration }, log);

  // 场景标记：标题进首景（画面用），figure 只保留实际存在的图
  script.scenes[0].title = script.title;

  emit('tts', '正在合成语音…');
  const tts = await synthesizeScenes(script.scenes, workDir, { engine, voice }, (i, n) => {
    if (onProgress) onProgress('tts', `正在合成第 ${i}/${n} 段语音…`);
    log(`语音 ${i}/${n}`);
  });
  log(`旁白总时长：${tts.scenes.reduce((s, x) => s + (x.duration || 0), 0).toFixed(0)}s`);

  // 超长 → 加速重录一版
  const totalAudio = tts.scenes.reduce((s, x) => s + (x.duration || 0), 0);
  if (totalAudio > config.podcastMaxSeconds) {
    log(`⚠️ 旁白 ${totalAudio.toFixed(0)}s 超长，加速到 +16% 重录`);
    const scenes2 = await synthesizeScenes(script.scenes, workDir, { engine: tts.engine, voice: tts.voice, rate: '+16%' });
    tts.scenes = scenes2.scenes;
  }

  emit('frame', '正在绘制视频画面…');
  await prepareVisuals(tts.scenes, assets, workDir);

  emit('compose', 'ffmpeg 正在合成视频…');
  const video = await buildVideo(tts.scenes, workDir, log);

  // 供前端展示的脚本/元数据（scene 的 narration 与配图）
  const scenesMeta = tts.scenes.map((s) => ({
    narration: s.narration,
    figure: s.figure,
    duration: s.duration,
  }));

  // 文风体检（口播文本按段落/句长/标点密度统计）
  const style = checkStyle(scenesMeta.map((s) => s.narration).join('\n\n'), 'podcast');

  await fs.writeFile(
    path.join(workDir, 'meta.json'),
    JSON.stringify(
      {
        id,
        title: script.title,
        duration: video.duration,
        totalChars: script.totalChars,
        engine: tts.engine,
        voice: tts.voice,
        source: assets,
        scenes: scenesMeta,
        style,
      },
      null,
      1,
    ),
    'utf-8',
  );

  return {
    id,
    title: script.title,
    videoDuration: video.duration,
    videoFile: video.path,
    videoUrl: `/api/podcast/files/${id}/podcast.mp4`,
    totalChars: script.totalChars,
    engine: tts.engine,
    voice: tts.voice,
    scenes: scenesMeta,
    style,
    source: {
      type: assets.sourceType,
      url: assets.url,
      title: assets.title,
      authors: assets.authors,
      institution: assets.institution,
      date: assets.date,
    },
    figureCount: assets.figures.length,
    pageCount: assets.pages.length,
    providerName: providerName || config.llmProvider,
  };
}
