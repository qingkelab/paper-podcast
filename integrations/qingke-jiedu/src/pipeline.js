import crypto from 'node:crypto';
import { config } from './config.js';
import { fetchSource } from './fetchSource.js';
import { extractWebpage } from './extractText.js';
import { pdfToImages, extractPdfInfo } from './pdfToImages.js';
import { webToImages } from './webToImages.js';
import { fetchArxivMeta } from './arxiv.js';
import { createProvider } from './ai/index.js';
import { savePrepared, saveCopy, markCopyError, readResult } from './store.js';
import { charCount, truncateAtSentence, truncateTitle } from './textUtils.js';
import { extractDateFromText, extractInstitution, extractTerms, parseIsoDate, parsePdfDate } from './meta.js';
import { normalizeTypography } from './typography.js';
import { buildMemoryContext, rememberPaper, rememberTerms, rememberTitles } from './memory.js';

/**
 * 两段式流程：转图与文案彼此独立。
 *
 *   阶段一 prepareUrl / prepareUpload —— 抓取 + 转图 + 抽取正文（零模型依赖，模型挂了也能用）；
 *   阶段二 generateCopy              —— 读回阶段一的正文，生成文案 / 标题（唯一依赖模型的部分）。
 *
 * 阶段一先把图片写进 output/{id}/，所以阶段二失败时图片、ZIP、summary.md 仍然可下载。
 */

/**
 * 中文 PDF（HTML 转 PDF 的译文、中文笔记）开头常是「中文标题 + 英文标题 + 作者 + 邮箱 + 日期」，
 * 且正文抽取会压掉换行；这里截到邮箱/日期/摘要/链接之前，再砍掉后面的英文标题与作者名，
 * 只留「中文标题 + 首个英文词」（如「递归循环 Transformer」）。
 */
function titleFromChineseHead(text) {
  const head = String(text || '')
    .split(/[\w.+-]+@[\w.-]+|\babstract\b|摘要|关键词|arxiv\s*:|https?:\/\//i)[0]
    .replace(/\b\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\b/g, '')
    .replace(/\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b/g, '')
    .trim();
  if (!/[\u4e00-\u9fff]/.test(head)) return '';

  // 「递归循环 Transformer Recurrent Looped Transformer Yifan Zhang」
  //   → group1 = 递归循环 Transformer，group2 = 后面的英文标题 / 作者
  const m = head.match(/^([\u4e00-\u9fff\s·、，,：:]*[A-Za-z][\w-]*)(?:\s+[\s\S]*)?$/);
  const title = (m ? m[1] : head).replace(/\s+/g, ' ').trim();
  return title.length >= 2 && title.length <= 80 ? title : '';
}

/** 从 PDF 正文里粗取标题（去掉 arXiv 头部与版权声明后，找首个 Title-Case 短语）。 */
function titleFromPdfText(text) {
  let t = String(text || '');
  t = t
    .replace(/^arxiv\s*:\s*\d{4}\.\d{4,5}(v\d+)?\s*(\[[^\]]*\])?[^A-Za-z\u4e00-\u9fff]*/i, '')
    .replace(/provided proper attribution is provided[^.]*\.\s*/i, '')
    .trim();

  // 中文标题优先（中文 PDF 用英文启发式会挑到英文副标题或作者名）
  const zh = titleFromChineseHead(t);
  if (zh) return zh;

  const titleCase = t.match(/\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){2,4})\b/);
  if (titleCase) return titleCase[1].trim();

  const upper = t.match(/\b([A-Z]{3,}(?:\s+[A-Z]{3,}){1,12})\b/);
  if (upper) return upper[1].trim();

  const first = t.split(/\s{2,}/).find((s) => s.length > 2 && s.length < 140);
  return (first || t).slice(0, 100);
}

function urlBaseName(url) {
  try {
    const u = new URL(url);
    return decodeURIComponent(u.pathname.split('/').pop() || u.hostname);
  } catch {
    return url;
  }
}

/** 去掉模型擅自写出的「## 论文链接」小节或结尾裸 URL，避免与自动补的链接重复。 */
function stripLinkSection(text) {
  let t = String(text || '');
  t = t.replace(/\n*\s*#{2,3}\s*论文链接[\s\S]*$/i, '');
  t = t.replace(/\n*\s*(?:链接|原文链接|论文地址)\s*[:：]?\s*https?:\/\/\S+\s*$/i, '');
  t = t.replace(/\n*\s*https?:\/\/\S+\s*$/i, '');
  return t.trimEnd();
}

/** 给各小节标题加 emoji 图标（幂等：已有图标的标题不会被重复加）。 */
const HEADING_ICONS = [
  ['一句话总结', '🎯'],
  ['背景', '📖'],
  ['核心方法', '⚙️'],
  ['结果', '📊'],
  ['局限与结论', '⚖️'],
];

function decorateHeadings(text) {
  let t = String(text || '');
  for (const [name, icon] of HEADING_ICONS) {
    // 只匹配「## 名字」行首的标题，且前面没有图标（避免二次处理重复加）
    const re = new RegExp(`(#{2,3}\\s*)(?!${icon}\\s)${name}`, 'g');
    t = t.replace(re, `$1${icon} ${name}`);
  }
  return t;
}

/** 从文案「一句话总结」小节抽取一行摘要，供记忆存储。 */
function extractSummary(copy) {
  const m = String(copy || '').match(/一句话总结[^\n]*\n\s*([^\n]+)/);
  return m ? m[1].trim() : '';
}

/** 落盘时正文的最大长度（防极端长网页把 result.json 撑爆）。 */
const MAX_STORED_TEXT = 100000;

function storableSource(source) {
  const text = String(source.text || '');
  return {
    ...source,
    text: text.length > MAX_STORED_TEXT ? text.slice(0, MAX_STORED_TEXT) : text,
  };
}

function publicSource(source) {
  return {
    url: source.url || '',
    title: source.title || '',
    byline: source.byline || '',
    excerpt: source.excerpt || '',
    institution: source.institution || '',
    date: source.date || '',
    textLength: charCount(source.text),
  };
}

/** 阶段一返回给前端的结构：只有图片与来源信息，没有文案。 */
function preparedPayload({ id, type, url, source, images }) {
  return {
    id,
    type,
    copyStatus: 'pending',
    source: publicSource({ ...source, url }),
    images: images.map((i) => ({ ...i, url: `/files/${id}/${i.filename}` })),
    zipUrl: `/files/${id}/images.zip`,
    markdownUrl: `/files/${id}/summary.md`,
  };
}

async function persistPrepared({ id, type, url, source, images }) {
  const { images: saved } = await savePrepared(id, images, {
    type,
    url,
    sourceTitle: source.title || '',
    institution: source.institution || '',
    date: source.date || '',
    source: storableSource(source),
  });
  return preparedPayload({ id, type, url, source, images: saved });
}

/** 把 PDF 整理成 source（标题 / 作者 / 正文 / 机构 / 时间 / 术语）。 */
async function sourceFromPdf(buffer, finalUrl, fallbackTitle) {
  const info = await extractPdfInfo(buffer);
  const arxiv = /arxiv\.org/.test(finalUrl) ? await fetchArxivMeta(finalUrl) : null;
  return {
    type: 'pdf',
    title:
      arxiv?.title ||
      info.title ||
      titleFromPdfText(info.text) ||
      fallbackTitle ||
      urlBaseName(finalUrl),
    byline: arxiv?.authors || info.author || '',
    excerpt: '',
    text: info.text,
    institution: extractInstitution(info.text),
    date: arxiv?.published ? parseIsoDate(arxiv.published) : parsePdfDate(info.creationDate),
    terms: extractTerms(info.text),
  };
}

/** 阶段一（链接）：抓取 → 转图 → 抽取正文，落盘后立即可下载。 */
export async function prepareUrl(url) {
  const id = crypto.randomUUID();
  const raw = await fetchSource(url);

  let source;
  let images;

  if (raw.type === 'pdf') {
    // 转图与正文抽取并行，两者都只依赖本地渲染
    const [pages, preparedSource] = await Promise.all([
      pdfToImages(raw.buffer),
      sourceFromPdf(raw.buffer, raw.finalUrl),
    ]);
    images = pages;
    source = preparedSource;
  } else {
    const meta = extractWebpage(raw.html, raw.finalUrl);
    images = await webToImages(raw.finalUrl);
    source = {
      type: 'webpage',
      ...meta,
      date:
        parseIsoDate(meta.date) ||
        extractDateFromText(`${meta.byline} ${meta.text.slice(0, 400)}`) ||
        meta.date ||
        '',
      terms: extractTerms(meta.text),
    };
  }

  return persistPrepared({ id, type: raw.type, url: raw.finalUrl, source, images });
}

/** 阶段一（上传）：PDF → 转图；.md/.txt → 纯文本（无图片）。 */
export async function prepareUpload({ filename, buffer }) {
  const id = crypto.randomUUID();
  const isPdf = /\.pdf$/i.test(filename) || buffer.subarray(0, 5).toString() === '%PDF-';

  let source;
  let images;

  if (isPdf) {
    const [pages, preparedSource] = await Promise.all([
      pdfToImages(buffer),
      sourceFromPdf(buffer, filename, filename.replace(/\.pdf$/i, '')),
    ]);
    images = pages;
    source = preparedSource;
  } else {
    const rawText = buffer.toString('utf-8').replace(/\r\n/g, '\n');
    const lines = rawText.split('\n').map((l) => l.trim());
    const firstLine = lines.find((l) => l) || '';
    const mdTitle = (firstLine.match(/^#+\s*(.*)/) || [])[1] || firstLine;
    images = [];
    source = {
      type: 'text',
      title: mdTitle.slice(0, 80) || filename.replace(/\.(md|txt)$/i, ''),
      byline: '',
      excerpt: '',
      text: rawText.replace(/\s+/g, ' ').trim(),
      institution: extractInstitution(rawText),
      date: extractDateFromText(rawText.slice(0, 1000)),
      terms: extractTerms(rawText),
    };
  }

  return persistPrepared({
    id,
    type: isPdf ? 'pdf' : 'text',
    url: filename,
    source,
    images,
  });
}

/** 文案后处理：标题压字数、文案按句边界兜底、补链接块，产出最终可发布的标题/文案。 */
function composeCopy(generated, source, url, limits) {
  const title = truncateTitle(generated.title, limits.maxTitleChars) || '值得一读的新进展';
  const copyContent = stripLinkSection(
    truncateAtSentence(normalizeTypography(decorateHeadings(generated.copy)), limits.maxCopyChars),
  );
  // 文末自动补「论文链接」（不计入 1000 字，只保留这一条）
  const linkSection = url && /^https?:\/\//.test(url) ? `\n\n## 🔗 论文链接\n${url}` : '';
  const copy = copyContent + linkSection;
  const titles = (generated.titles || [title])
    .map((t) => truncateTitle(t, limits.maxTitleChars))
    .filter(Boolean);
  if (!titles.length) titles.push(title);

  return { title, copy, copyContent, titles };
}

/**
 * 阶段二：读回阶段一落盘的正文，生成解读文案 + 爆款标题。
 * 模型不可用时抛错并记录 copyStatus=error —— 图片不受任何影响。
 */
export async function generateCopy(id, providerName, model) {
  const record = await readResult(id); // 记录不存在时抛 ENOENT，由路由层转 404
  const source = {
    ...record.source,
    type: record.type,
    title: record.source?.title || record.sourceTitle || '',
  };
  source.text = String(source.text || '');

  const limits = {
    maxCopyChars: config.maxCopyChars,
    maxTitleChars: config.maxTitleChars,
  };

  let provider;
  let generated;
  try {
    provider = createProvider(providerName, model); // 未配置 key 时在这里就报错
    // 记忆：注入相关历史论文 + 已用标题，让新内容可自然引用旧内容
    source.memory = await buildMemoryContext({ terms: source.terms, title: source.title });
    generated = await provider.generate({ source, limits });
  } catch (err) {
    await markCopyError(id, (err && err.message) || String(err));
    throw err;
  }

  const composed = composeCopy(generated, source, record.url, limits);
  await saveCopy(id, {
    title: composed.title,
    titles: composed.titles,
    copy: composed.copy,
    reasoning: generated.reasoning || '',
    style: generated.style || null,
  });

  // 记忆：记录术语、标题、本文摘要，供后续优化与引用（异步、失败不影响主流程）
  rememberTerms(source.terms).catch(() => {});
  rememberTitles(composed.titles).catch(() => {});
  if (/^https?:\/\//.test(record.url || '')) {
    rememberPaper({
      title: source.title,
      url: record.url,
      type: record.type,
      summary: extractSummary(composed.copyContent),
      institution: record.institution || source.institution || '',
      terms: source.terms || [],
    }).catch(() => {});
  }

  return {
    id,
    type: record.type,
    copyStatus: 'done',
    source: publicSource({
      ...source,
      url: record.url || '',
      institution: record.institution || source.institution || '',
      date: record.date || source.date || '',
    }),
    title: { text: composed.title, charCount: charCount(composed.title) },
    titles: composed.titles.map((t) => ({ text: t, charCount: charCount(t) })),
    copy: { text: composed.copy, charCount: charCount(composed.copyContent) },
    reasoning: generated.reasoning || '',
    style: generated.style || null,
    provider: provider.name,
    model: provider.model || '',
    zipUrl: `/files/${id}/images.zip`,
    markdownUrl: `/files/${id}/summary.md`,
  };
}

/** 跑完两段的一站式入口：文案失败时返回图片部分（copyStatus=error），不让图片陪葬。 */
async function attachCopy(prepared, providerName, model) {
  try {
    const generated = await generateCopy(prepared.id, providerName, model);
    return { ...prepared, ...generated };
  } catch (err) {
    return {
      ...prepared,
      copyStatus: 'error',
      copyError: (err && err.message) || String(err),
    };
  }
}

/** 兼容入口：链接 → 转图 + 文案（/api/process）。 */
export async function processUrl(url, providerName, model) {
  return attachCopy(await prepareUrl(url), providerName, model);
}
