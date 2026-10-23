/**
 * 网页正文抽取：@mozilla/readability（CDN UMD 全局）+ 原生 DOMParser。
 * 浏览器版没有网页截图能力，只抽正文供文案生成。
 */
export function extractWebArticle(html, url = '') {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  if (!window.Readability) throw new Error('Readability 未加载');
  const article = new window.Readability(doc).parse();
  const text = (article?.textContent || '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return {
    title: article?.title || doc.querySelector('title')?.textContent?.trim() || url,
    byline: article?.byline || '',
    excerpt: article?.excerpt || '',
    text,
  };
}
