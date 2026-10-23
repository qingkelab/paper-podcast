import fs from 'node:fs/promises';
import path from 'node:path';
import { config } from './config.js';

const file = path.join(config.outputDir, 'memory.json');
const MAX_PAPERS = 200;
const MAX_TITLES = 80;

/** 读取记忆（缺省结构兜底）。 */
export async function loadMemory() {
  try {
    const raw = await fs.readFile(file, 'utf-8');
    const m = JSON.parse(raw);
    return { papers: [], glossary: {}, titles: [], ...m };
  } catch {
    return { papers: [], glossary: {}, titles: [] };
  }
}

async function saveMemory(mem) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, JSON.stringify(mem, null, 2), 'utf-8');
}

/** 记录一篇已解读的论文（按 url 去重、置顶）。 */
export async function rememberPaper(entry) {
  if (!entry || !entry.url) return;
  const mem = await loadMemory();
  mem.papers = [
    {
      title: entry.title || '',
      url: entry.url,
      type: entry.type || '',
      summary: entry.summary || '',
      institution: entry.institution || '',
      terms: entry.terms || [],
      at: entry.at || new Date().toISOString(),
    },
    ...mem.papers.filter((p) => p && p.url !== entry.url),
  ].slice(0, MAX_PAPERS);
  await saveMemory(mem);
}

/** 累积术语表（term -> 出现次数），越常出现的术语越稳定。 */
export async function rememberTerms(terms) {
  if (!terms || !terms.length) return;
  const mem = await loadMemory();
  for (const raw of terms) {
    const k = String(raw).trim();
    if (k) mem.glossary[k] = (mem.glossary[k] || 0) + 1;
  }
  await saveMemory(mem);
}

/** 记录用过的标题（去重、置顶、封顶），避免后续重复。 */
export async function rememberTitles(titles) {
  const arr = Array.isArray(titles) ? titles.map((t) => String(t).trim()).filter(Boolean) : [];
  if (!arr.length) return;
  const mem = await loadMemory();
  mem.titles = [...new Set([...arr, ...mem.titles])].slice(0, MAX_TITLES);
  await saveMemory(mem);
}

/** 按术语 + 标题关键词匹配，找出相关历史论文（得分降序）。 */
export async function findRelated({ terms = [], title = '', limit = 3 } = {}) {
  const mem = await loadMemory();
  const qterms = (terms || []).map((t) => String(t).toLowerCase());
  const qtitle = String(title || '').toLowerCase();
  const tw = qtitle
    .split(/[^a-z0-9\u4e00-\u9fff]+/i)
    .filter((w) => w.length >= 3);

  return mem.papers
    .map((p) => {
      const pterms = (p.terms || []).map((t) => String(t).toLowerCase());
      let score = 0;
      for (const t of qterms) if (pterms.includes(t)) score += 2;
      const ptitle = String(p.title || '').toLowerCase();
      for (const w of tw) if (ptitle.includes(w)) score += 1;
      return { p, score };
    })
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((x) => x.p);
}

/** 构建注入提示词的历史参考块（相关论文 + 已用标题）。 */
export async function buildMemoryContext({ terms = [], title = '' } = {}) {
  const related = await findRelated({ terms, title });
  const mem = await loadMemory();
  const recentTitles = (mem.titles || []).slice(0, 12);
  if (!related.length && !recentTitles.length) return '';

  const lines = [];
  if (related.length) {
    lines.push('## 历史记忆：相关已解读论文（确有承接/同源时可自然提一句，勿生硬，无需则不引用）');
    for (const p of related) {
      const line = `- 《${p.title}》${p.summary ? `：${p.summary}` : ''}`.trim();
      lines.push(line);
    }
  }
  if (recentTitles.length) {
    lines.push('已用过的标题（避免重复）：' + recentTitles.join(' / '));
  }
  return lines.join('\n');
}
