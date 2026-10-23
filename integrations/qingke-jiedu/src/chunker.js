/**
 * 论文结构化切片（Paper section / chunk parser）
 *
 * 深度解读需要「全文参与分析」，而不是把正文 slice(0, N)。这里把三种来源统一切成
 * 带结构信息的 chunk：
 *   - html：arXiv HTML 版（`.ltx_section` / `<figure>` / MathML annotation）—— 真实 section hierarchy；
 *   - tex ：e-print TeX 源码经 latexToText() 处理后保留 `## 标题` / `图注：` / `$$公式$$` 标记；
 *   - pdf ：PDF 抽出的带换行文本（textLines），按编号标题启发式分节，无结构时按句子边界切片。
 *
 * 输出：
 *   {
 *     kind, sections: [{id,title,level,order,chunkIds,charCount}],
 *     chunks: [{id,index,sectionId,sectionTitle,sectionPath,type,order,text,numbers,terms}],
 *     stats: {sectionCount,chunkCount,chars,coveredChars,coverage}
 *   }
 * chunk id 稳定（c1、c2…，按原文顺序），并带上 sectionTitle / type，供检索与证据审计复用。
 */
import { JSDOM } from 'jsdom';

/** chunk 类型：段落 / 公式 / 图注 / 表格 / 列表 / 标题块 / 摘要。 */
export const CHUNK_TYPES = ['paragraph', 'formula', 'figure', 'table', 'list', 'heading', 'abstract'];

const SKIP_TAGS = new Set(['script', 'style', 'nav', 'header', 'footer', 'aside', 'noscript']);
const HEADING_CLASS = /ltx_title_section|ltx_title_subsection|ltx_title_paragraph|ltx_title_document|title_section/i;

/** 归一化空白：行内压空格、保留段落级换行。 */
export function normalizeText(text) {
  return String(text || '')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t\u00a0]+/g, ' ')
    .replace(/ *\n */g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** 抽取文本里的数字/百分比（供证据审计与检索排序复用）。 */
export function extractNumbers(text) {
  const out = new Set();
  for (const m of String(text || '').matchAll(/\d+(?:[.,]\d+)?\s*(?:%|％|万|亿|k|K|M|B|x|×|倍)?/g)) {
    const raw = m[0].replace(/\s+/g, '');
    if (raw.replace(/[^\d]/g, '').length >= 1) out.add(raw);
  }
  return [...out];
}

const STOP_WORDS = new Set(
  ('the a an and or of to in for with on at by from as is are was were be been this that these those we our it its their they them he she his her not but also can may' +
    ' 我们 本文 作者 论文 一个 一种 这个 那个 以及 并且 但是 因为 所以 如果 可以 需要 通过 使用 进行 提供 结果 方法 如图 表 中 的 了 与 和 或 在 是 有 对 为 上 下 等')
    .split(/\s+/)
    .filter(Boolean),
);

/** 轻量分词：英文词 + 中文 2-gram（无需 NLP 依赖）。 */
export function tokenize(text) {
  const s = String(text || '').toLowerCase();
  const tokens = [];
  for (const m of s.matchAll(/[a-z][a-z0-9+#_.-]{1,}/g)) {
    if (!STOP_WORDS.has(m[0]) && m[0].length > 2) tokens.push(m[0]);
  }
  for (const m of s.matchAll(/[\u4e00-\u9fff]{2,}/g)) {
    const seg = m[0];
    for (let i = 0; i + 1 < seg.length; i++) {
      const bg = seg.slice(i, i + 2);
      if (!STOP_WORDS.has(bg)) tokens.push(bg);
    }
  }
  return tokens;
}

/** 取 chunk 的高频词（用于检索与 section 主题匹配）。 */
export function topTerms(text, limit = 24) {
  const freq = new Map();
  for (const t of tokenize(text)) freq.set(t, (freq.get(t) || 0) + 1);
  return [...freq.entries()]
    .sort((a, b) => b[1] - a[1] || b[0].length - a[0].length)
    .slice(0, limit)
    .map(([t]) => t);
}

/** 按句子边界把长段落切成不超过 maxChars 的片段。 */
export function splitByBudget(text, maxChars) {
  const clean = normalizeText(text);
  if (!clean) return [];
  if (clean.length <= maxChars) return [clean];

  // 先按空行/换行切段，再把超长段按句子切
  const blocks = clean.split(/\n{2,}/);
  const out = [];
  let buf = '';
  const flush = () => {
    const t = buf.trim();
    if (t) out.push(t);
    buf = '';
  };
  for (const block of blocks) {
    for (const piece of splitSentences(block, maxChars)) {
      if (buf && buf.length + piece.length + 1 > maxChars) flush();
      buf = buf ? `${buf}\n${piece}` : piece;
    }
  }
  flush();
  return out;
}

/** 句子级切分（中文句末标点 + 英文句点），单句超长时再按词/字硬切。 */
function splitSentences(text, maxChars) {
  const parts = normalizeText(text)
    .split(/(?<=[。！？!?；;：:])\s*|(?<=\.)\s+(?=[A-Z(])/)
    .map((s) => s.trim())
    .filter(Boolean);
  const out = [];
  let buf = '';
  for (const p of parts) {
    if (p.length > maxChars) {
      if (buf) {
        out.push(buf);
        buf = '';
      }
      for (let i = 0; i < p.length; i += maxChars) out.push(p.slice(i, i + maxChars));
      continue;
    }
    if (buf && buf.length + p.length > maxChars) {
      out.push(buf);
      buf = p;
    } else {
      buf += p;
    }
  }
  if (buf) out.push(buf);
  return out;
}

/** 判断一个块是不是公式（TeX 标记或 LaTeX 密度高）。 */
function looksLikeFormula(text) {
  const s = String(text || '');
  if (/\$\$[\s\S]*\$\$|\\begin\{(equation|align|gather|eqnarray)/.test(s)) return true;
  const latexHits = (s.match(/\\(frac|sum|argmax|argmin|mathbb|mathcal|hat|theta|alpha|beta|nabla|prod|int|sqrt|log|exp|softmax)\b/g) || []).length;
  return latexHits >= 2 && s.length < 600;
}

function looksLikeFigure(text) {
  return /^图注[:：]|^Figure\s*\d+|^图\s*\d+\s*[:：]/.test(String(text || '').trim());
}

function looksLikeTable(text) {
  return /^表\s*\d+|^Table\s*\d+/i.test(String(text || '').trim()) || /^\|.+\|$/m.test(String(text || ''));
}

/** 依据内容推断 chunk 类型。 */
export function classifyBlock(text, fallback = 'paragraph') {
  const t = String(text || '').trim();
  if (!t) return fallback;
  if (looksLikeFormula(t)) return 'formula';
  if (looksLikeFigure(t)) return 'figure';
  if (looksLikeTable(t)) return 'table';
  if (/^\s*[-*·]\s+/m.test(t) && t.split('\n').length > 2) return 'list';
  return fallback;
}

/**
 * 组装最终结构：把「块」序列变成 chunk 序列 + section 索引。
 * blocks: [{sectionTitle, sectionPath, type, text}]
 */
export function assemble(blocks, { maxChunkChars = 1600, kind = 'html' } = {}) {
  const sections = [];
  const chunks = [];
  const sectionByPath = new Map();

  const ensureSection = (title, path) => {
    const name = title || '正文';
    const key = path || name;
    if (sectionByPath.has(key)) return sectionByPath.get(key);
    const sec = {
      id: `s${sections.length + 1}`,
      title: name,
      path: key,
      order: sections.length,
      level: Math.max(1, key.split('›').length),
      chunkIds: [],
      charCount: 0,
    };
    sections.push(sec);
    sectionByPath.set(key, sec);
    return sec;
  };

  for (const block of blocks) {
    if (block.type === 'heading') continue; // 标题只用于划分 section，不单独成 chunk
    const text = normalizeText(block.text);
    if (!text) continue;
    const sec = ensureSection(block.sectionTitle, block.sectionPath);
    for (const piece of splitByBudget(text, maxChunkChars)) {
      const chunk = {
        id: `c${chunks.length + 1}`,
        index: chunks.length,
        sectionId: sec.id,
        sectionTitle: sec.title,
        sectionPath: sec.path,
        type: block.type || classifyBlock(piece),
        order: chunks.length,
        text: piece,
        numbers: extractNumbers(piece),
        terms: topTerms(piece),
      };
      chunks.push(chunk);
      sec.chunkIds.push(chunk.id);
      sec.charCount += piece.length;
    }
  }

  const chars = chunks.reduce((n, c) => n + c.text.length, 0);
  return {
    kind,
    sections,
    chunks,
    stats: {
      kind,
      sectionCount: sections.length,
      chunkCount: chunks.length,
      chars,
      coverage: chars,
      truncated: false,
    },
  };
}

/** ============ HTML 结构化切片 ============ */

/** 从 MathML 元素里取 LaTeX（arXiv HTML 会带 annotation）。 */
function mathToLatex(el) {
  const ann = el.querySelector?.('annotation[encoding="application/x-tex"]');
  if (ann?.textContent) return ann.textContent.trim();
  const alt = el.getAttribute?.('alttext');
  if (alt) return alt.trim();
  return (el.textContent || '').replace(/\s+/g, ' ').trim();
}

/**
 * 取节点文本，但把 `<math>` 子树替换成 `$LaTeX$`（而不是它的 textContent）。
 *
 * 为什么不能直接用 textContent：arXiv HTML 的 MathML 同时含**可见数字**
 * （`<mn>41.0</mn>`）与 `application/x-tex` **注释**（同一个 `41.0`），
 * textContent 会把两者首尾相接 → `41.041.0`、`N=6N=6`、`1/41/4`。
 * 这类粘连会污染检索与审计，并让「终稿数字能否在原文定位」误判成 unsupported
 * （例：正文写 41.0 BLEU，原文被拼成 41.041.0 → 数字索引里没有干净的 41.0）。
 */
function textWithMath(node) {
  let out = '';
  for (const child of node.childNodes || []) {
    if (child.nodeType !== 1) {
      out += child.textContent || '';
      continue;
    }
    const tag = child.tagName?.toLowerCase() || '';
    if (tag === 'math') {
      out += ` $${mathToLatex(child)}$ `;
    } else if (typeof child.querySelector === 'function' && child.querySelector('math')) {
      out += textWithMath(child); // 公式藏在下级元素里，继续递归而不是整段取 textContent
    } else {
      out += child.textContent || '';
    }
  }
  return out;
}

/**
 * 遍历 arXiv HTML 正文，按 document order 抽 section / paragraph / formula / figure / table。
 * @param {Document} root 已解析的 DOM（jsdom）
 */
export function blocksFromDom(root) {
  const blocks = [];
  const stack = []; // [{title, level}]
  let sawContent = false; // 文档标题（首个 h1）不算章节

  const sectionPath = () => stack.map((s) => s.title).filter(Boolean).join('›') || '正文';
  const currentTitle = () => stack.length ? stack[stack.length - 1].title : '正文';

  const pushBlock = (type, text) => {
    const t = normalizeText(text);
    if (!t || t.length < 2) return;
    if (type !== 'heading') sawContent = true;
    blocks.push({ sectionTitle: currentTitle(), sectionPath: sectionPath(), type, text: t });
  };

  const walk = (el) => {
    for (const node of el.children || []) {
      const tag = node.tagName ? node.tagName.toLowerCase() : '';
      if (SKIP_TAGS.has(tag)) continue;
      const cls = node.getAttribute?.('class') || '';

      const isHeading =
        /^h[1-6]$/.test(tag) ||
        (HEADING_CLASS.test(cls) && (node.textContent || '').trim().length < 200);
      if (isHeading) {
        const level = /^h[1-6]$/.test(tag) ? Number(tag[1]) : /subsection/i.test(cls) ? 3 : 2;
        const title = normalizeText(node.textContent).replace(/^\d+(\.\d+)*\s*/, '');
        if (title) {
          // 首个一级标题按「论文标题」处理，不作为章节进入 path
          if (level === 1 && !sawContent && !stack.length) continue;
          sawContent = true;
          while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
          stack.push({ title, level });
          pushBlock('heading', title);
          continue;
        }
      }

      // 表格：arXiv 里既可能是 <table>，也可能是 <figure class="ltx_table"> 包着 <table>
      const isTableNode = tag === 'table' || (/ltx_table/.test(cls) && node.querySelector('table'));
      if (isTableNode) {
        const cap = node.querySelector('figcaption') || node.querySelector('caption') || node.querySelector('.ltx_caption');
        const caption = normalizeText(cap ? textWithMath(cap) : '');
        const body = normalizeText(textWithMath(node)).slice(0, 1200);
        pushBlock('table', `${caption ? `${caption}\n` : ''}${body}`.trim());
        continue;
      }

      if (tag === 'figure' || /ltx_figure/.test(cls)) {
        const cap = node.querySelector('figcaption') || node.querySelector('.ltx_caption');
        const caption = normalizeText(cap ? textWithMath(cap) : textWithMath(node));
        pushBlock('figure', caption ? `图注：${caption}` : '');
        // figure 内部文字不再重复计入
        continue;
      }

      if (tag === 'math' || /ltx_(math|equation|displaymath)/.test(cls)) {
        pushBlock('formula', `$$${mathToLatex(node)}$$`);
        continue;
      }

      if (tag === 'p' || /ltx_para|ltx_abstract/.test(cls)) {
        // 段落里可能内嵌公式：拆出来单独成块，便于审计公式
        const maths = [...node.querySelectorAll('math')];
        // 注意：段落容器常常是包住 <p> 的 <div class="ltx_para">，直接取 textContent
        // 会把 MathML 的可见数字与 TeX 注释粘起来（41.0 + 41.0 → 41.041.0）。
        const text = textWithMath(node);
        pushBlock(/ltx_abstract/.test(cls) ? 'abstract' : 'paragraph', text);
        // 段内公式额外单独成块：检索「方法/公式」与证据审计都要能单独命中
        for (const m of maths) {
          const latex = mathToLatex(m);
          if (latex) pushBlock('formula', `$$${latex}$$`);
        }
        continue;
      }

      if (tag === 'ul' || tag === 'ol') {
        pushBlock('list', [...node.querySelectorAll('li')].map((li) => `- ${normalizeText(textWithMath(li))}`).join('\n'));
        continue;
      }

      if (tag === 'figcaption') continue;
      walk(node);
    }
  };

  const body = root.body || root;
  walk(body);
  return blocks;
}

/** 从 HTML 字符串构建结构（独立入口，便于测试）。 */
export function chunksFromHtml(html, opts = {}) {
  const Ctor = opts.JSDOM || JSDOM;
  const dom = new Ctor(String(html || ''));
  const doc = dom.window.document;
  doc.querySelectorAll('nav, footer, header, script, style, aside').forEach((el) => el.remove());
  const blocks = blocksFromDom(doc);
  return assemble(blocks, { ...opts, kind: 'html' });
}

/** ============ TeX（latexToText 输出）结构化切片 ============ */

/** latexToText() 会把章节写成 `## 标题`、图注写成 `图注：…`、公式写成 `$$…$$`。 */
export function blocksFromMarkdownish(text) {
  const blocks = [];
  const stack = [];
  const lines = normalizeText(text).split('\n');
  let buf = [];

  const path = () => stack.join('›') || '正文';
  const title = () => stack[stack.length - 1] || '正文';
  const flush = (type) => {
    const t = buf.join('\n').trim();
    buf = [];
    if (t) blocks.push({ sectionTitle: title(), sectionPath: path(), type: classifyBlock(t, type || 'paragraph'), text: t });
  };

  for (const line of lines) {
    const h = line.match(/^(#{2,4})\s+(.+)$/);
    if (h) {
      flush();
      const level = h[1].length;
      while (stack.length >= level - 1) stack.pop();
      stack.push(h[2].trim());
      blocks.push({ sectionTitle: title(), sectionPath: path(), type: 'heading', text: h[2].trim() });
      continue;
    }
    if (/^图注[:：]/.test(line.trim())) {
      flush();
      blocks.push({ sectionTitle: title(), sectionPath: path(), type: 'figure', text: line.trim() });
      continue;
    }
    if (/^\$\$[\s\S]*\$\$$\s*$/.test(line.trim()) && line.trim().length < 1200) {
      flush();
      blocks.push({ sectionTitle: title(), sectionPath: path(), type: 'formula', text: line.trim() });
      continue;
    }
    buf.push(line);
  }
  flush();
  return blocks;
}

/** ============ PDF（带换行文本）结构化切片 ============ */

const NUMBERED_HEADING = /^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][A-Za-z0-9\-图\s]{2,60})$/;
const NAMED_HEADING = /^\s*(abstract|introduction|related work|background|method|methods|approach|model|architecture|experiments?|evaluation|results?|analysis|ablation[s]?|discussion|limitations?|conclusion[s]?|references|appendix|摘要|引言|相关工作|背景|方法|模型|架构|实验|评估|结果|分析|消融|讨论|局限|结论|参考文献|附录)\s*[:：]?$/i;

/** 从带换行的 PDF 文本里识别编号/命名标题。 */
export function looksLikePdfHeading(line) {
  const s = String(line || '').trim();
  if (!s || s.length > 90) return null;
  const m = s.match(NUMBERED_HEADING);
  if (m && !/[。！？.!?]$/.test(s) && (m[2].match(/[A-Za-z]/g) || []).length >= 3) {
    return { level: m[1].split('.').length, title: `${m[1]} ${m[2].trim()}` };
  }
  if (NAMED_HEADING.test(s)) return { level: 1, title: s.replace(/[:：]$/, '') };
  return null;
}

export function blocksFromPlainText(text) {
  const blocks = [];
  const lines = normalizeText(text).split('\n');
  const stack = [];
  let buf = [];

  const path = () => stack.join('›') || '正文';
  const title = () => stack[stack.length - 1] || '正文';
  const flush = () => {
    const t = buf.join('\n').trim();
    buf = [];
    if (t) blocks.push({ sectionTitle: title(), sectionPath: path(), type: classifyBlock(t), text: t });
  };

  for (const line of lines) {
    const head = looksLikePdfHeading(line);
    if (head) {
      flush();
      while (stack.length >= head.level) stack.pop();
      stack.push(head.title);
      blocks.push({ sectionTitle: title(), sectionPath: path(), type: 'heading', text: head.title });
      continue;
    }
    buf.push(line);
  }
  flush();
  return blocks;
}

/** ============ 统一入口 ============ */

/**
 * 构建论文结构。优先用调用方给的结构化输入（HTML/TeX/PDF 各自解析），
 * 没有时按 kind 从纯文本兜底重建。
 * @returns {{sections:Array,chunks:Array,stats:Object}}
 */
export function buildPaperStructure({ kind = 'html', text = '', textLines = '', html = '', structure = null, maxChunkChars = 1600, JSDOM } = {}) {
  if (structure && Array.isArray(structure.chunks) && structure.chunks.length >= 3) {
    return structure;
  }
  if (kind === 'html' && html) {
    try {
      return chunksFromHtml(html, { maxChunkChars, JSDOM });
    } catch {
      /* 落到纯文本兜底 */
    }
  }
  if (kind === 'tex') return assemble(blocksFromMarkdownish(text), { maxChunkChars, kind: 'tex' });
  if (textLines) return assemble(blocksFromPlainText(textLines), { maxChunkChars, kind: 'pdf' });

  // 最后的兜底：整段纯文本按句子切片（保证 30k+ 论文也能全文参与分析）
  const blocks = splitByBudget(text, maxChunkChars).map((t) => ({
    sectionTitle: '正文',
    sectionPath: '正文',
    type: classifyBlock(t),
    text: t,
  }));
  return assemble(blocks, { maxChunkChars, kind: 'text' });
}

/** 结构摘要（日志 / SSE detail 用）。 */
export function structureSummary(structure) {
  const s = structure?.stats || {};
  return `${s.sectionCount || 0} 节 / ${s.chunkCount || 0} chunks / ${s.chars || 0} 字`;
}

/** 按 section 取 chunks（检索与审计复用）。 */
export function chunksBySection(structure, sectionId) {
  return (structure?.chunks || []).filter((c) => c.sectionId === sectionId);
}
