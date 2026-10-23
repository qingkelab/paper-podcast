/**
 * 文案收尾（与 src/pipeline.js 的 composeCopy 同一套规则）：
 * 标题截断、小节 emoji 装饰、剥离模型自写的链接、文末统一补「论文链接」。
 */
import { truncateAtSentence, truncateTitle } from '../../src/textUtils.js';
import { normalizeTypography } from '../../src/typography.js';

function stripLinkSection(text) {
  let t = String(text || '');
  t = t.replace(/\n*\s*#{2,3}\s*论文链接[\s\S]*$/i, '');
  t = t.replace(/\n*\s*(?:链接|原文链接|论文地址)\s*[:：]?\s*https?:\/\/\S+\s*$/i, '');
  t = t.replace(/\n*\s*https?:\/\/\S+\s*$/i, '');
  return t.trimEnd();
}

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
    const re = new RegExp(`(#{2,3}\\s*)(?!${icon}\\s)${name}`, 'g');
    t = t.replace(re, `$1${icon} ${name}`);
  }
  return t;
}

export function composeCopy(generated, source, url, limits) {
  const title = truncateTitle(generated.title, limits.maxTitleChars) || '值得一读的新进展';
  const copyContent = stripLinkSection(
    truncateAtSentence(normalizeTypography(decorateHeadings(generated.copy)), limits.maxCopyChars),
  );
  const linkSection = url && /^https?:\/\//.test(url) ? `\n\n## 🔗 论文链接\n${url}` : '';
  const copy = copyContent + linkSection;
  const titles = (generated.titles || [title])
    .map((t) => truncateTitle(t, limits.maxTitleChars))
    .filter(Boolean);
  if (!titles.length) titles.push(title);
  return { title, copy, copyContent, titles };
}
