import { createCanvas } from '@napi-rs/canvas';
import { getPdfDocument } from './pdfjs.js';
import { config } from './config.js';

/**
 * 将 PDF 逐页渲染为 PNG。
 * @param {Buffer|Uint8Array} buffer
 * @returns {Promise<Array<{pageNumber:number, width:number, height:number, buffer:Buffer, label:string}>>}
 */
export async function pdfToImages(buffer) {
  const doc = await getPdfDocument(buffer);
  const total = Math.min(doc.numPages, config.maxPdfPages);
  const pages = [];

  for (let i = 1; i <= total; i++) {
    const page = await doc.getPage(i);
    const viewport = page.getViewport({ scale: config.pdfScale });
    const width = Math.floor(viewport.width);
    const height = Math.floor(viewport.height);

    const canvas = createCanvas(width, height);
    const ctx = canvas.getContext('2d');
    // 白底，避免透明 PDF 导出黑底
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);

    await page.render({ canvasContext: ctx, viewport }).promise;

    pages.push({
      pageNumber: i,
      width,
      height,
      buffer: canvas.toBuffer('image/png'),
      label: `第 ${i} 页`,
    });
    page.cleanup();
  }

  await doc.cleanup();
  return pages;
}

/**
 * 从 PDF 提取正文文本 + 元数据标题/作者（供 AI 文案使用）。
 */
export async function extractPdfInfo(buffer, maxChars = 9000) {
  const doc = await getPdfDocument(buffer);

  let metaTitle = '';
  let author = '';
  let creationDate = '';
  try {
    const md = await doc.getMetadata();
    metaTitle = (md && md.info && md.info.Title) || '';
    author = (md && md.info && md.info.Author) || '';
    creationDate = (md && md.info && md.info.CreationDate) || '';
  } catch {
    /* 元数据缺失时忽略 */
  }

  let text = '';
  const pages = Math.min(doc.numPages, 12);
  for (let i = 1; i <= pages; i++) {
    const page = await doc.getPage(i);
    const tc = await page.getTextContent();
    text += tc.items.map((it) => (it && it.str ? it.str : '')).join(' ') + '\n';
    page.cleanup();
    if (text.length > maxChars) break;
  }
  await doc.cleanup();

  return {
    text: text
      .replace(/\s+/g, ' ')
      .replace(/provided proper attribution is provided[^.]*\.\s*/i, '')
      .trim()
      .slice(0, maxChars),
    title: metaTitle,
    author,
    creationDate,
  };
}

/**
 * 从 PDF 抽取「保留换行」的正文：按行分组文本 item，返回 lines（每行一条）。
 * 深度解读需要章节结构（`1 Introduction` / `3.2 Method` 这类编号标题只在行首可辨认），
 * 而 extractPdfInfo 会把正文压成一行、丢掉全部结构，所以这里单独提供一份带行的版本。
 * 普通图文解读（/api/copy）继续用 extractPdfInfo，行为不变。
 *
 * @returns {Promise<{text:string, lines:string[], pages:number}>}
 */
export async function extractPdfTextBlocks(buffer, { maxPages = 40, maxChars = 120000 } = {}) {
  const doc = await getPdfDocument(buffer);
  const pages = Math.min(doc.numPages, maxPages);
  const lines = [];

  for (let i = 1; i <= pages; i++) {
    const page = await doc.getPage(i);
    const tc = await page.getTextContent();
    let current = '';
    let lastY = null;

    for (const item of tc.items) {
      const str = item && typeof item.str === 'string' ? item.str : '';
      const y = Array.isArray(item?.transform) ? item.transform[5] : null;
      // 换行判定：pdfjs 的 hasEOL，或 y 坐标变化超过半个字高
      const yChanged = lastY != null && y != null && Math.abs(y - lastY) > 3;
      if (current && (item?.hasEOL || yChanged)) {
        pushLine(lines, current);
        current = '';
      }
      if (str) current += str;
      if (y != null) lastY = y;
      if (item?.hasEOL) lastY = null;
    }
    if (current) pushLine(lines, current);
    lines.push(''); // 分页空行
    page.cleanup();
    if (lines.join('\n').length > maxChars) break;
  }
  await doc.cleanup();

  const text = lines.join('\n').trim();
  return { text, lines, pages };
}

/** 行内压空格、去掉纯装饰行。 */
function pushLine(lines, raw) {
  const line = String(raw || '')
    .replace(/[ \t\u00a0]+/g, ' ')
    .replace(/^\s+|\s+$/g, '');
  if (!line) return;
  if (/^[-–—_=·.\s]+$/.test(line)) return; // 分隔线
  lines.push(line);
}
