import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { gunzipSync } from 'node:zlib';
import { config } from './config.js';
import { fetchArxivHtml, parseArxivId } from './arxivHtml.js';
import { pdfToImages, extractPdfInfo, extractPdfTextBlocks } from './pdfToImages.js';
import { svgToPng } from './webToImages.js';
import { assemble, blocksFromMarkdownish } from './chunker.js';

const execFileP = promisify(execFile);

/**
 * arXiv 取源（多级回退）：
 *  1) HTML 版——图文最全（含图注）；老论文的矢量图是 <object data="…svg">/内联 svg，会栅格化成 PNG
 *  2) TeX 源码（e-print）——HTML 404 或 HTML 抓不到图时使用：正文更干净、公式是原生 LaTeX，图从源码包里取
 *  3) PDF——兜底正文（拿不到配图）
 * 本地化素材统一放 output/_arxivsrc/<id>/，通过 /_arxivsrc 静态路由访问。
 */
export const ARXIV_SRC_DIR = path.join(config.outputDir, '_arxivsrc');

/** 本地素材的对外地址（公众号同步等需要绝对 URL 才能下载）。 */
const SELF_BASE = process.env.PUBLIC_BASE_URL || `http://127.0.0.1:${config.port}`;
const srcUrl = (id, file) => `${SELF_BASE}/_arxivsrc/${id}/${encodeURIComponent(file)}`;

const RASTER_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp']);

/** LaTeX 源码 → 可读正文（保留公式、章节标题、图注；去掉排版命令）。 */
export function latexToText(tex) {
  let t = String(tex || '');
  t = t.replace(/^\s*%.*$/gm, ''); // 整行注释
  t = t.replace(/(?<!\\)%.*$/gm, ''); // 行尾注释（保留 \%）
  t = t.replace(/\\(sub)*section\*?\{([^{}]*)\}/g, (m, sub, title) => `\n\n${sub ? '####' : '##'} ${title}\n`);
  t = t.replace(/\\paragraph\*?\{([^{}]*)\}/g, '\n\n**$1**：');
  t = t.replace(/\\caption\*?\{([^{}]*)\}/g, '\n图注：$1\n');
  t = t.replace(/\\begin\{abstract\}([\s\S]*?)\\end\{abstract\}/g, '\n\n## 摘要\n$1\n');
  t = t.replace(
    /\\begin\{(equation|equation\*|align|align\*|gather|gather\*|eqnarray)\}([\s\S]*?)\\end\{\1\}/g,
    '\n\n$$$2$$\n\n',
  );
  t = t.replace(/\\\[([\s\S]*?)\\\]/g, '\n\n$$$1$$\n');
  t = t.replace(/\\\((.*?)\\\)/g, ' $$$1$$ ');
  t = t.replace(/\\item\s*/g, '\n- ');
  t = t.replace(/\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}/g, '（图：$1）');
  t = t.replace(/\\href\{([^}]*)\}\{([^}]*)\}/g, '$2（$1）');
  t = t.replace(/\\(?:textbf|textit|emph|texttt|textrm|textsc|textsf|mbox|mathrm|mathbf|mathit|operatorname|url)\s*/g, '');
  t = t.replace(
    /\\(?:label|ref|eqref|autoref|cite|citep|citet|nocite|bibliography|bibliographystyle|footnote|thanks|vspace|hspace|noindent|centering|small|large|footnotesize|tiny|bigskip|medskip|smallskip|newpage|clearpage|index|protect|makeatletter|makeatother)\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?/g,
    '',
  );
  t = t.replace(/\\(?:begin|end)\{[^}]*\}(?:\[[^\]]*\])?/g, '\n');
  t = t.replace(/\\([a-zA-Z@]+)\*?(?:\[[^\]]*\])?\{([^{}]*)\}/g, '$2');
  t = t.replace(/\\([a-zA-Z@]+)\*?(\[[^\]]*\])?/g, ' ');
  t = t.replace(/\\[\\%&_#{}]/g, (m) => m.slice(1));
  t = t.replace(/[{}]/g, '');
  t = t.replace(/[ \t]+/g, ' ');
  t = t.replace(/\n{3,}/g, '\n\n');
  return t.trim();
}

/** 从 tex 源码抽「图片文件名 → 图注」。 */
function captionMap(tex) {
  const map = new Map();
  const figs = String(tex || '').match(/\\begin\{figure\*?\}[\s\S]*?\\end\{figure\*?\}/g) || [];
  for (const block of figs) {
    const inc = block.match(/\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}/);
    const cap = block.match(/\\caption\*?\{([\s\S]*?)\}\s*(?:\\label|\\end)/);
    if (!inc) continue;
    const file = inc[1].trim().split('/').pop();
    const caption = cap ? latexToText(cap[1]).replace(/\s+/g, ' ').slice(0, 300) : '';
    if (!map.has(file)) map.set(file, caption);
  }
  return map;
}

async function listFiles(dir, out = []) {
  const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) await listFiles(p, out);
    else out.push(p);
  }
  return out;
}

/** 下载 arXiv e-print（TeX 源码包）并解包；已解包过则直接复用。 */
async function fetchTexSource(id, destDir) {
  const existing = await listFiles(destDir);
  if (existing.some((f) => f.toLowerCase().endsWith('.tex'))) return destDir;

  const res = await fetch(`https://arxiv.org/e-print/${id}`, {
    redirect: 'follow',
    signal: AbortSignal.timeout(config.fetchTimeoutMs),
    headers: { 'User-Agent': config.userAgent },
  });
  if (!res.ok) throw new Error(`下载 TeX 源码失败 HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  await fs.mkdir(destDir, { recursive: true });

  if (buf.length > 2 && buf[0] === 0x1f && buf[1] === 0x8b) {
    const tarPath = path.join(destDir, '_src.tar.gz');
    await fs.writeFile(tarPath, buf);
    try {
      await execFileP('tar', ['-xzf', tarPath, '-C', destDir], { timeout: 180000 });
    } catch {
      const raw = gunzipSync(buf); // 单文件 gz
      await fs.writeFile(path.join(destDir, 'main.tex'), raw);
    } finally {
      await fs.unlink(tarPath).catch(() => {});
    }
  } else {
    await fs.writeFile(path.join(destDir, 'main.tex'), buf); // 已是纯 tex
  }
  return destDir;
}

/** 选主 tex（优先含 \begin{document}），并合并其 \input 的一级文件。 */
async function readMainTex(extractDir) {
  const files = (await listFiles(extractDir)).filter((f) => f.toLowerCase().endsWith('.tex'));
  if (!files.length) throw new Error('TeX 源码里没有 .tex 文件');
  const withStats = [];
  for (const f of files) withStats.push({ f, content: await fs.readFile(f, 'utf-8').catch(() => '') });
  withStats.sort((a, b) => b.content.length - a.content.length);
  const main = withStats.find((x) => /\\begin\{document\}/.test(x.content)) || withStats[0];
  const parts = [main.content];
  const inputs = [...main.content.matchAll(/\\(?:input|include)\{([^}]+)\}/g)].map((m) => m[1].trim());
  for (const inc of inputs) {
    const name = inc.endsWith('.tex') ? inc : `${inc}.tex`;
    const hit = withStats.find((x) => x.f !== main.f && x.f.endsWith(name));
    if (hit) parts.push(hit.content);
  }
  return parts.join('\n\n');
}

/** 从 TeX 源码包里取图（位图直接用；PDF 矢量图用 pdfjs 渲成 PNG）。 */
async function texFigures(destDir, tex, id) {
  const caps = captionMap(tex);
  const all = await listFiles(destDir);
  const raster = all.filter((f) => RASTER_EXT.has(path.extname(f).toLowerCase()));
  const pdfs = all.filter((f) => path.extname(f).toLowerCase() === '.pdf').slice(0, 24);
  const figFiles = [...raster];
  for (const pf of pdfs) {
    try {
      const pages = await pdfToImages(await fs.readFile(pf));
      if (pages[0]) {
        const out = pf.replace(/\.pdf$/i, '.png');
        await fs.writeFile(out, pages[0].buffer);
        figFiles.push(out);
      }
    } catch {
      /* 单张失败跳过 */
    }
  }
  figFiles.sort((a, b) => a.localeCompare(b));
  return figFiles.slice(0, 30).map((f, i) => {
    const base = path.basename(f);
    return {
      num: i + 1,
      caption: caps.get(base) || caps.get(base.replace(/\.(png|jpe?g|gif|webp)$/i, '')) || base,
      url: srcUrl(id, base),
      path: f,
    };
  });
}

/** 把 HTML 里的图本地化：位图直接用 CDN，SVG（object/内联）栅格化成 PNG。 */
async function materializeHtmlFigures(figures, id, log) {
  if (!figures || !figures.length) return [];
  const out = [];
  const destDir = path.join(ARXIV_SRC_DIR, id);
  for (const f of figures) {
    try {
      if (f.kind === 'img' && f.url) {
        out.push({ num: out.length + 1, caption: f.caption || '', url: f.url, path: null });
        continue;
      }
      let markup = f.svgMarkup || '';
      if (!markup && f.url) {
        const res = await fetch(f.url, {
          signal: AbortSignal.timeout(config.fetchTimeoutMs),
          headers: { 'User-Agent': config.userAgent },
        });
        if (!res.ok) continue;
        markup = await res.text();
      }
      if (!markup || !/<svg/i.test(markup)) continue;
      await fs.mkdir(destDir, { recursive: true });
      const png = await svgToPng(markup);
      if (!png || !png.buffer) continue;
      const file = path.join(destDir, `fig_${String(out.length + 1).padStart(2, '0')}.png`);
      await fs.writeFile(file, png.buffer);
      out.push({
        num: out.length + 1,
        caption: f.caption || '',
        url: srcUrl(id, path.basename(file)),
        path: file,
      });
    } catch (err) {
      log(`图片处理失败已跳过：${(err && err.message) || err}`);
    }
  }
  return out;
}

/** TeX 源码整篇（正文 + 图），供 HTML 不可用或无图时使用。 */
export async function fetchArxivTex(url, log = () => {}) {
  const id = parseArxivId(url);
  if (!id) throw new Error('不是 arXiv 论文链接（无法解析 ID）');
  const destDir = path.join(ARXIV_SRC_DIR, id);
  await fetchTexSource(id, destDir);
  const tex = await readMainTex(destDir);
  const text = latexToText(tex);
  if (!text || text.length < 500) throw new Error('TeX 正文过短');
  const figures = await texFigures(destDir, tex, id);
  // 结构化切片：latexToText 保留了 `## 章节` / `图注：` / `$$公式$$` 标记，可直接还原层次
  let structure = null;
  try {
    structure = assemble(blocksFromMarkdownish(text), { kind: 'tex' });
  } catch {
    structure = null;
  }
  log(`TeX 源码可用（正文 ${text.length} 字，${figures.length} 张图）`);
  return { id, title: '', kind: 'tex', text, structure, codeUrl: '', figures };
}

/**
 * 主入口：按 HTML → TeX → PDF 回退取源。
 * @returns {Promise<{id,title,kind,text,figures,codeUrl}>}
 */
export async function fetchArxivSource(url, log = () => {}) {
  const id = parseArxivId(url);
  if (!id) throw new Error('不是 arXiv 论文链接（无法解析 ID）');

  // 1) HTML 版（图文最全）
  try {
    const html = await fetchArxivHtml(url);
    if (html.text && html.text.length > 500) {
      let figures = await materializeHtmlFigures(html.figures || [], id, log);
      let kind = 'html';
      if (!figures.length) {
        // HTML 抓不到图（老论文常见）：用 TeX 源码补图
        try {
          const tex = await fetchArxivTex(url, log);
          if (tex.figures.length) {
            figures = tex.figures;
            kind = 'html+tex';
            log(`HTML 无可用图，已用 TeX 源码补 ${figures.length} 张`);
          }
        } catch (err) {
          log(`TeX 补图失败：${(err && err.message) || err}`);
        }
      }
      log(`取源完成：${kind}（${figures.length} 张图）`);
      return {
        id,
        title: html.title,
        kind,
        text: html.text,
        structure: html.structure || null,
        codeUrl: html.codeUrl || '',
        figures,
      };
    }
  } catch (err) {
    log(`HTML 版不可用（${(err && err.message) || err}），回退 TeX 源码`);
  }

  // 2) TeX 源码
  try {
    const tex = await fetchArxivTex(url, log);
    log(`取源完成：tex（${tex.figures.length} 张图）`);
    return tex;
  } catch (err) {
    log(`TeX 源码不可用（${(err && err.message) || err}），回退 PDF`);
  }

  // 3) PDF 兜底（正文可用、无配图）
  const res = await fetch(`https://arxiv.org/pdf/${id}`, {
    redirect: 'follow',
    signal: AbortSignal.timeout(config.fetchTimeoutMs),
    headers: { 'User-Agent': config.userAgent },
  });
  if (!res.ok) throw new Error(`抓取 arXiv 失败（HTML 404 / TeX 失败 / PDF HTTP ${res.status}）`);
  const pdf = Buffer.from(await res.arrayBuffer());
  if (pdf.subarray(0, 5).toString() !== '%PDF-') throw new Error('arXiv PDF 响应异常');
  const info = await extractPdfInfo(pdf);
  if (!info.text || info.text.length < 300) throw new Error('PDF 正文提取失败');
  // 深度解读需要章节结构：额外取一份「保留换行」的正文（失败不影响主流程）
  let textLines = '';
  try {
    const blocks = await extractPdfTextBlocks(pdf);
    textLines = blocks.lines.join('\n');
  } catch {
    textLines = '';
  }
  log(`取源完成：pdf（无配图，正文 ${info.text.length} 字）`);
  return {
    id,
    title: info.title || '',
    kind: 'pdf',
    text: info.text,
    textLines,
    structure: null,
    codeUrl: '',
    figures: [],
  };
}
