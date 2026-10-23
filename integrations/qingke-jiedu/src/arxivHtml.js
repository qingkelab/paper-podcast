import { JSDOM } from 'jsdom';
import { config } from './config.js';
import { blocksFromDom, assemble } from './chunker.js';

/** 从 arXiv 链接解析出论文 ID（abs/pdf/html 均可）。 */
export function parseArxivId(url) {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5})(v\d+)?/i);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

function resolveUrl(src, base) {
  try {
    return new URL(src, base).href;
  } catch {
    return src;
  }
}

/** 从文本里抽论文代码链接（github.com，过滤 arXiv 自身工具/渲染链接）。 */
export function extractCodeUrl(html) {
  const s = String(html || '');
  const urls = [...s.matchAll(/https?:\/\/github\.com\/[A-Za-z0-9_.\-/]+/gi)].map((m) => m[0]);
  const clean = urls.filter(
    (u) => !/github\.com\/arXiv\//i.test(u) && !/html_feedback/i.test(u) && !/LaTeXML/i.test(u),
  );
  return clean[0] || '';
}

/**
 * 抓取 arXiv 论文的 HTML 版本，抽取正文文本与图片（CDN 链接 + 图注）。
 * 图片形态：`<img>`（新论文）／`<object data="…svg">`（老论文矢量图）／内联 `<svg>`。
 * @returns {Promise<{id:string, base:string, text:string, figures:Array<{url:string,caption:string,kind:string,svgMarkup?:string}>}>}
 */
export async function fetchArxivHtml(url) {
  const id = parseArxivId(url);
  if (!id) throw new Error('不是 arXiv 论文链接（无法解析 ID）');

  const htmlUrl = `https://arxiv.org/html/${id}`;
  const res = await fetch(htmlUrl, {
    redirect: 'follow',
    signal: AbortSignal.timeout(config.fetchTimeoutMs),
    headers: { 'User-Agent': config.userAgent },
  });
  if (!res.ok) throw new Error(`抓取 arXiv HTML 失败 HTTP ${res.status}`);
  const html = await res.text();
  const base = res.url || htmlUrl;

  const dom = new JSDOM(html);
  const doc = dom.window.document;

  // 正文文本：去掉脚本/样式/导航等
  doc.querySelectorAll('script, style, nav, footer, header, aside').forEach((el) => el.remove());

  // 结构化切片（section / paragraph / formula / figure / table），供深度解读全文分析
  let structure = null;
  try {
    structure = assemble(blocksFromDom(doc), { kind: 'html', maxChunkChars: config.chunkChars });
  } catch {
    structure = null; // 结构抽取失败不影响纯文本路径
  }

  const text = (doc.body?.textContent || '').replace(/\s+/g, ' ').trim();

  // 标题：优先 h1.ltx_title，其次 <title>（去掉 [ID] 前缀）
  const h1 = doc.querySelector('h1.ltx_title, h1.ltx_title_document, h1.title');
  const titleEl = h1 || doc.querySelector('title');
  const title = (titleEl?.textContent || '')
    .replace(/\s+/g, ' ')
    .replace(/^\[?\d{4}\.\d{4,5}(v\d+)?\]?\s*/i, '')
    .trim();

  // 作者：优先逐个取 .ltx_personname（剥掉机构/邮箱/脚注）；找不到再退回整块清理。
  const authorsEl = doc.querySelector('.ltx_authors');
  let authors = '';
  if (authorsEl) {
    const names = [...authorsEl.querySelectorAll('.ltx_personname')]
      .map((el) => {
        const clone = el.cloneNode(true);
        clone.querySelectorAll('.ltx_note, .ltx_sup, sup').forEach((n) => n.remove());
        return (clone.textContent || '').replace(/\s+/g, ' ').replace(/[†‡*§¶]+\s*/g, '').trim();
      })
      .filter(Boolean);
    if (names.length) {
      authors = names.join(', ');
    } else {
      const clone = authorsEl.cloneNode(true);
      clone.querySelectorAll('.ltx_note, .ltx_sup, sup').forEach((n) => n.remove());
      authors = (clone.textContent || '')
        .replace(/\s+/g, ' ')
        .replace(/^\s*Authors?\s*[:：]?\s*/i, '')
        .replace(/[†‡*§¶]+\s*/g, '')
        .trim();
    }
  }

  // 图片：figure 内的内容图（img / object[data] / 内联 svg），跳过 logo/静态资源/base64
  const figures = [];
  const visited = new Set();
  const BAD_SRC = /static\/|funders|logo|glyph|smileybones|favicon|icon/i;
  for (const f of doc.querySelectorAll('figure, .ltx_figure, .figure')) {
    if (visited.has(f)) continue;
    visited.add(f);
    const cap = f.querySelector('figcaption');
    const caption = (cap?.textContent || '').replace(/\s+/g, ' ').trim();

    const img = f.querySelector('img');
    const src = img?.getAttribute('src') || '';
    if (src && !src.startsWith('data:') && !BAD_SRC.test(src)) {
      figures.push({
        url: resolveUrl(src, base),
        caption: caption || (img.getAttribute('alt') || '').replace(/\s+/g, ' ').trim(),
        kind: 'img',
      });
      continue;
    }

    const obj = f.querySelector('object[data]');
    const data = obj?.getAttribute('data') || '';
    if (data && !data.startsWith('data:') && !BAD_SRC.test(data)) {
      const isSvg = /svg/i.test(obj?.getAttribute('type') || '') || /\.svg(\?|$)/i.test(data);
      if (isSvg) {
        figures.push({ url: resolveUrl(data, base), caption, kind: 'svg' });
      }
      // 非 svg（如 object 指向 pdf）暂不支持，跳过
      continue;
    }

    const inlineSvg = f.querySelector('svg');
    if (inlineSvg) {
      figures.push({
        url: '',
        caption,
        kind: 'inline-svg',
        svgMarkup: inlineSvg.outerHTML.slice(0, 400000),
      });
    }
  }

  return {
    id,
    base,
    title,
    authors,
    text,
    structure, // 结构化切片（section/chunk），供深度解读全文分析
    figures: figures.slice(0, 40),
    // 代码链接：只在正文前段（摘要/引言）里找，避开页脚工具与参考文献里的链接
    codeUrl: extractCodeUrl(text.slice(0, 5000)),
  };
}
