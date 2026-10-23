import { JSDOM } from 'jsdom';
import { Readability } from '@mozilla/readability';

/**
 * 用 Readability 从 HTML 抽取正文标题/作者/摘要/正文。
 */
export function extractWebpage(html, url) {
  const dom = new JSDOM(html, { url, pretendToBeVisual: false });
  const doc = dom.window.document;

  let article = null;
  try {
    article = new Readability(doc).parse();
  } catch {
    /* 忽略，走兜底 */
  }

  const title = article?.title || doc.title || '';
  const byline = article?.byline || '';
  const excerpt = article?.excerpt || '';
  const content =
    article?.textContent || doc.body?.textContent || doc.documentElement?.textContent || '';

  // 机构 / 时间：优先读 meta 标签
  const metaContent = (sel) => {
    const el = doc.querySelector(sel);
    return el ? (el.getAttribute('content') || el.getAttribute('datetime') || '').trim() : '';
  };
  const institution =
    metaContent('meta[name="citation_author_institution"]') ||
    metaContent('meta[name="dc.publisher"]') ||
    metaContent('meta[name="citation_journal_title"]') ||
    '';
  const date =
    metaContent('meta[name="citation_publication_date"]') ||
    metaContent('meta[property="article:published_time"]') ||
    metaContent('meta[name="date"]') ||
    metaContent('meta[property="og:article:published_time"]') ||
    metaContent('time[datetime]') ||
    '';

  return {
    title: title.trim(),
    byline: byline.trim(),
    excerpt: excerpt.trim(),
    text: content.replace(/\s+/g, ' ').trim(),
    institution,
    date,
  };
}
