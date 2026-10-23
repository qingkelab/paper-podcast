/** 日期格式化：ISO/PDF 日期 → 「YYYY年M月」。 */
export function parseIsoDate(iso) {
  const m = String(iso || '').match(/(\d{4})-(\d{2})/);
  return m ? `${Number(m[1])}年${Number(m[2])}月` : '';
}

export function parsePdfDate(d) {
  const m = String(d || '').match(/D:(\d{4})(\d{2})(\d{2})/);
  return m ? `${Number(m[1])}年${Number(m[2])}月` : '';
}

const MONTHS = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
  jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
};

/** 从文本里粗取日期（支持「2023年8月」「12 Jun 2017」「2017-06」等）。 */
export function extractDateFromText(text) {
  const s = String(text || '');
  let m = s.match(/(20\d{2})\s*年\s*(\d{1,2})\s*月/);
  if (m) return `${Number(m[1])}年${Number(m[2])}月`;
  m = s.match(/(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(20\d{2})/i);
  if (m) return `${Number(m[3])}年${MONTHS[m[2].toLowerCase()]}月`;
  m = s.match(/(20\d{2})[-/.](\d{1,2})/);
  if (m) return `${Number(m[1])}年${Number(m[2])}月`;
  return '';
}

const INST_PATTERNS = [
  // 英文：X University / X Institute / X Lab 等
  /\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,2}\s+(?:University|Institute|College|Academy|Laboratory|Labs|School|Research)\b/g,
  // 英文：University/Institute of X
  /\b(?:University|Institute)\s+of\s+[A-Z][\w-]*(?:\s+[A-Z][\w-]*){0,3}/g,
  // 已知科技公司/机构
  /\b(?:Google|Microsoft|OpenAI|DeepMind|Meta|Facebook|Amazon|IBM|NVIDIA|Huawei|Tencent|Alibaba|Baidu|ByteDance|Apple|Anthropic|Mistral|Cohere|Intel|Samsung|Adobe|Salesforce)\b/gi,
  // 中文机构
  /[\u4e00-\u9fff]{2,12}(?:大学|学院|研究院|研究所|实验室|科学院)/g,
];

/** 从论文/网页正文前缀粗提取机构名（去重、去子串冗余，最多 3 个）。 */
export function extractInstitution(text, maxChars = 3000) {
  let s = String(text || '')
    // 先去掉邮箱与 "Email:" 标签，避免被当成机构名的一部分
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, ' ')
    .replace(/\bEmail\b[^,，\s]*/gi, ' ')
    .slice(0, maxChars);
  const found = [];
  const seen = new Set();
  for (const re of INST_PATTERNS) {
    for (const m of s.matchAll(re)) {
      const t = m[0].trim();
      const key = t.toLowerCase();
      if (t.length >= 2 && !seen.has(key)) {
        seen.add(key);
        found.push(t);
      }
    }
  }
  // 去掉被更长匹配包含的子串（如 "Google" 被 "Google Research" 覆盖）
  const filtered = found.filter(
    (t) => !found.some((o) => o !== t && o.toLowerCase().includes(t.toLowerCase())),
  );
  return filtered.slice(0, 3).join('、');
}

const TERM_STOP = new Set(
  ('the and for with are was were this that these those from into over under between during before after ' +
    'above below their them they our your its but not can may will would should could been being have has had ' +
    'does did where when which while figure table section equation abstract introduction method methods related ' +
    'work works results result conclusion conclusions references appendix available code data we propose proposed ' +
    'based show shows shown our new first second finally however moreover therefore thus also yet using used use ' +
    'provides provided compared comparison experiments experimental benchmark benchmarks state art university ' +
    'institute google microsoft openai deepmind meta email one two three four five both each all some such other ' +
    'another same different several many most more less only very just too present paper study author authors ' +
    'et al following figure fig tab table tables')
    .split(/\s+/),
);

/** 从正文抽术语表：全大写缩略词 + 首字母大写的词/词组，供文案强制对齐用。 */
export function extractTerms(text, max = 60) {
  const s = String(text || '');
  const acronyms = s.match(/\b[A-Z]{2,8}\b/g) || [];
  const titled = s.match(/\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b/g) || [];

  const seen = new Set();
  const out = [];
  for (const t of [...acronyms, ...titled]) {
    const k = t.toLowerCase();
    if (TERM_STOP.has(k) || k.length < 2 || seen.has(k)) continue;
    seen.add(k);
    out.push(t);
  }
  return out.slice(0, max);
}
