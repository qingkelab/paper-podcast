/** 统计可见字符数（忽略空白/换行）。 */
export function charCount(text) {
  return (text || '').replace(/\s/g, '').length;
}

/**
 * 按字符数截断（中文友好，逐字符截断）。
 * 保留换行（供 Markdown 结构化文案使用），仅压缩多余空格与连续空行。
 */
export function truncateChars(text, max) {
  const clean = String(text || '')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  if (charCount(clean) <= max) return clean;
  const arr = Array.from(clean);
  let count = 0;
  let out = '';
  for (const ch of arr) {
    if (/\s/.test(ch)) {
      out += ch;
      continue;
    }
    if (count >= max) break;
    out += ch;
    count += 1;
  }
  return out.trim();
}

/**
 * 按句子边界截断：在不超过 max 的前提下，尽量回退到最近一个句末标点或换行，
 * 保证不截在词/句中间。用于文案兜底，主路径应靠模型自行控制在预算内。
 */
export function truncateAtSentence(text, max) {
  const clean = String(text || '')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  if (charCount(clean) <= max) return clean;

  const arr = Array.from(clean);
  let out = '';
  let count = 0; // 非空白字符数
  let breakIdx = -1; // 最近一个可安全截断处（句末标点或换行）的 out.length
  for (const ch of arr) {
    if (count >= max) break;
    out += ch;
    if (/\s/.test(ch)) {
      if (ch === '\n') breakIdx = out.length;
      continue;
    }
    count += 1;
    if ('。！？!?；;'.includes(ch)) breakIdx = out.length;
  }

  // 若在预算内已出现过句末/换行，且其后残留不多，则回退到该处，避免断句
  if (breakIdx > 0 && out.length - breakIdx < 80) {
    out = out.slice(0, breakIdx);
  }
  return out.trim();
}

/**
 * 标题截断：不超过 max 字且语义完整——若截断落在拉丁/数字词中间，回退到上一个词边界，
 * 并去掉结尾悬空的标点。中文逐字截断（汉字本身原子，不产生半截字）。
 */
export function truncateTitle(text, max) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (charCount(clean) <= max) return clean;

  const arr = Array.from(clean);
  let out = '';
  let count = 0;
  let lastWordBreak = -1;
  for (const ch of arr) {
    if (count >= max) break;
    out += ch;
    if (/\s/.test(ch)) {
      lastWordBreak = out.length - 1;
      continue;
    }
    count += 1;
  }

  // 若停在某个拉丁/数字词中间，回退到上一个空格处
  const next = arr[out.length];
  if (
    next &&
    /[A-Za-z0-9]/.test(next) &&
    /[A-Za-z0-9]/.test(out.slice(-1)) &&
    lastWordBreak > 0
  ) {
    out = out.slice(0, lastWordBreak);
  }
  return out.trim().replace(/[，,、：:；;！!？?。]+$/, '').trim() || truncateChars(clean, max);
}

/** 从一段文本中抽取前 N 个句子。 */
export function firstSentences(text, n) {
  const s = String(text || '')
    .replace(/\s+/g, ' ')
    .split(/(?<=[。！？!?；;])/);
  const picked = s.filter((x) => x.trim()).slice(0, n);
  return picked.join('').trim();
}

/** 粗略判断文本是否以中文为主（用于兜底：审校/模型输出变成英文时丢弃）。 */
export function isChineseText(text) {
  const s = String(text || '');
  const cjk = (s.match(/[\u3400-\u4dbf\u4e00-\u9fff]/g) || []).length;
  return cjk >= 20;
}
