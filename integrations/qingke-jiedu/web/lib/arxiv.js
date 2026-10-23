/**
 * arXiv 搜索 / 元数据：浏览器里**尽量不需要代理**。
 *
 * 现实约束（都实测过）：
 *   - `export.arxiv.org/api/query`（搜索与元数据）**不放 CORS** → 直连必失败，需要代理；
 *   - `arxiv.org/abs|list|search` 也不放 CORS；
 *   - `arxiv.org/html/<id>` **带 `Access-Control-Allow-Origin: *`** → 单篇标题/作者可直连解析；
 *   - `api.openalex.org` **带 CORS** → 关键词搜索可用（但最近几天的 arXiv 覆盖不全）；
 *   - `api.semanticscholar.org` 带 CORS，但无 key 时容易 429。
 *
 * 所以按下面的顺序退：同源后端 → 用户代理 + 官方 API → OpenAlex（关键词）→ S2。
 * 每一步失败都继续，最后才报错，并且错误信息会说明「哪一步为什么失败、怎么补救」。
 */
import { fetchText, proxiedUrl } from './net.js';

const OPENALEX = 'https://api.openalex.org/works';
const S2 = 'https://api.semanticscholar.org/graph/v1';
/** arXiv ID：YYMM.NNNNN，YY 是 07–26（2007 年以后的编号规则），避免把别人的数字串当 ID。 */
const ARXIV_ID_RE = /\b((?:0[7-9]|1\d|2[0-6])\d{2}\.\d{4,5})(v\d+)?\b/;
/** 只从真正的 arXiv 链接里取 ID（Zenodo 等 DOI 里也有一串数字，不能瞎认）。 */
const ARXIV_URL_ID_RE = /arxiv\.org\/(?:abs|pdf|html)\/((?:0[7-9]|1\d|2[0-6])\d{2}\.\d{4,5})/i;

/** 同源 Node 后端探测（浏览器版部署在 GitHub Pages 时没有后端，会静默跳过）。 */
let backendProbe = null;
export function detectBackend() {
  if (backendProbe) return backendProbe;
  backendProbe = (async () => {
    try {
      if (typeof location === 'undefined') return false;
      const res = await fetch(`${location.origin}/api/arxiv/search?days=1&max=1`, { signal: AbortSignal.timeout(8000) });
      const data = await res.json().catch(() => null);
      return Boolean(res.ok && data && Array.isArray(data.papers));
    } catch {
      return false;
    }
  })();
  return backendProbe;
}

/** 仅供测试/切换环境时重置探测缓存。 */
export function resetBackendProbe() {
  backendProbe = null;
}

function fmtDate(d) {
  return d.toISOString().slice(0, 10).replace(/-/g, ''); // YYYYMMDD
}

async function fetchApiText(url, proxy) {
  // export.arxiv.org 已知无 CORS，必须走代理；没有代理时由上层换别的数据源
  if (!String(proxy || '').trim()) throw new Error('NO_PROXY');
  const res = await fetch(proxiedUrl(url, proxy), { signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`arXiv API 失败 HTTP ${res.status}（经代理）`);
  return res.text();
}

/** arXiv ID → 纯数字 ID（OpenAlex / S2 的返回里都可能是带版本或 URL 的形态）。 */
function cleanArxivId(raw) {
  const m = String(raw || '').match(ARXIV_ID_RE);
  return m ? m[1] : '';
}

/** OpenAlex 结果 → 应用里的论文结构；只保留能定位到 arXiv 的那些。 */
export function mapOpenAlexWork(work) {
  const doi = String(work?.doi || '');
  // arXiv 的 DOI 形如 https://doi.org/10.48550/arXiv.2501.12948 → 直接抓 ID，别按 '.' 切
  let id = (doi.match(/10\.48550\/arxiv\.(\d{4}\.\d{4,5})/i) || [])[1] || '';
  if (!id) {
    for (const loc of work?.locations || []) {
      const hit =
        (String(loc?.landing_page_url || '').match(ARXIV_URL_ID_RE) || [])[1] ||
        (String(loc?.pdf_url || '').match(ARXIV_URL_ID_RE) || [])[1] ||
        '';
      if (hit) {
        id = hit;
        break;
      }
    }
  }
  if (!id) return null;
  const authors = (work?.authorships || []).map((a) => a?.author?.display_name).filter(Boolean);
  return {
    id,
    title: String(work?.display_name || work?.title || '').replace(/\s+/g, ' ').trim(),
    published: String(work?.publication_date || '').slice(0, 10),
    authors: authors.slice(0, 3).join(', '),
    url: `https://arxiv.org/abs/${id}`,
    pdfUrl: `https://arxiv.org/pdf/${id}`,
  };
}

/** 关键词搜索（OpenAlex，免代理）。无关键词时 OpenAlex 的「最新」几乎都是非 arXiv，故不采用。 */
export async function searchViaOpenAlex({ keyword = '', max = 60, days = 3 } = {}) {
  if (!String(keyword || '').trim()) throw new Error('OpenAlex 需要关键词');
  const url =
    `${OPENALEX}?search=${encodeURIComponent(keyword)}&sort=publication_date:desc` +
    `&per-page=${Math.min(100, Math.max(10, max))}`;
  const res = await fetch(url, { signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`OpenAlex HTTP ${res.status}`);
  const data = await res.json();
  const papers = (data?.results || []).map(mapOpenAlexWork).filter(Boolean);
  if (!papers.length) throw new Error('OpenAlex 没有匹配到 arXiv 论文');
  // 只要「最近 days 天」的（OpenAlex 的日期可能比 arXiv 晚一两天，取并集更稳）
  const since = Date.now() - Math.max(days, 1) * 86400000;
  const recent = papers.filter((p) => !p.published || new Date(`${p.published}T00:00:00Z`).getTime() >= since);
  return { papers: recent.length ? recent : papers, source: 'openalex' };
}

/** 关键词搜索（Semantic Scholar，免代理；无 key 时可能 429）。 */
export async function searchViaSemanticScholar({ keyword = '', max = 60 } = {}) {
  if (!String(keyword || '').trim()) throw new Error('Semantic Scholar 需要关键词');
  const url =
    `${S2}/paper/search?query=${encodeURIComponent(keyword)}&limit=${Math.min(100, Math.max(10, max))}` +
    `&fields=title,publicationDate,authors,externalIds`;
  const res = await fetch(url, { signal: AbortSignal.timeout(30000) });
  if (!res.ok) throw new Error(`Semantic Scholar HTTP ${res.status}`);
  const data = await res.json();
  const papers = (data?.data || [])
    .map((p) => {
      const id = cleanArxivId(p?.externalIds?.ArXiv || '');
      if (!id) return null;
      return {
        id,
        title: String(p?.title || '').replace(/\s+/g, ' ').trim(),
        published: String(p?.publicationDate || '').slice(0, 10),
        authors: (p?.authors || []).map((a) => a?.name).filter(Boolean).slice(0, 3).join(', '),
        url: `https://arxiv.org/abs/${id}`,
        pdfUrl: `https://arxiv.org/pdf/${id}`,
      };
    })
    .filter(Boolean);
  if (!papers.length) throw new Error('Semantic Scholar 没有匹配到 arXiv 论文');
  return { papers, source: 'semanticscholar' };
}

/**
 * 按时间/分类/关键词搜索最新论文。
 * @returns {Promise<Array<{id,title,published,authors,url,pdfUrl}>>}
 */
export async function searchArxiv({ days = 3, category = '', keyword = '', max = 60, proxy = '' } = {}) {
  const now = new Date();
  const from = new Date(now.getTime() - days * 86400000);

  const parts = [];
  if (category) parts.push(`cat:${category}`);
  if (keyword) parts.push(`all:"${keyword}"`);
  parts.push(`submittedDate:[${fmtDate(from)}0000 TO ${fmtDate(now)}2359]`);

  const url =
    `https://export.arxiv.org/api/query?search_query=${encodeURIComponent(parts.join(' AND '))}` +
    `&sortBy=submittedDate&sortOrder=descending&max_results=${max}`;
  const attempts = [];

  // 1) 同源 Node 后端：它自己调 arXiv，浏览器这边没有跨域问题（本地跑 server.js 时最省事）
  if (await detectBackend()) {
    try {
      const res = await fetch(
        `${location.origin}/api/arxiv/search?days=${days}&category=${encodeURIComponent(category)}&keyword=${encodeURIComponent(keyword)}`,
        { signal: AbortSignal.timeout(30000) },
      );
      const data = await res.json();
      if (res.ok && Array.isArray(data.papers) && data.papers.length) {
        data.papers.source = 'backend';
        return data.papers;
      }
      attempts.push(`同源后端：返回 ${res.status}`);
    } catch (err) {
      attempts.push(`同源后端：${(err && err.message) || err}`);
    }
  } else {
    attempts.push('同源后端：不可用（页面没有配套 Node 后端）');
  }

  // 2) 用户配置的代理 → arXiv 官方 API（最准，支持分类与精确日期）
  if (String(proxy || '').trim()) {
    try {
      const papers = parseAtomEntries(await fetchApiText(url, proxy));
      papers.source = 'arxiv-api';
      return papers;
    } catch (err) {
      attempts.push(`arXiv 官方 API（经代理）：${(err && err.message) || err}`);
    }
  }

  // 3) 免代理的关键词搜索：OpenAlex → Semantic Scholar（都带 CORS）
  if (String(keyword || '').trim()) {
    for (const [name, fn] of [
      ['openalex', searchViaOpenAlex],
      ['semanticscholar', searchViaSemanticScholar],
    ]) {
      try {
        const { papers, source } = await fn({ keyword, max, days });
        if (papers.length) {
          papers.source = source;
          return papers;
        }
      } catch (err) {
        attempts.push(`${name}：${(err && err.message) || err}`);
      }
    }
  }

  const detail = attempts.length ? `\n已尝试：${attempts.join('；')}` : '';
  throw new Error(
    '搜索失败：arXiv 官方 API（export.arxiv.org）不支持跨域直连。' +
      '两种办法：① 先在关键词框里填一个关键词，会走 OpenAlex/Semantic Scholar 免代理搜索；' +
      '② 在「接口设置 → CORS 代理」填一个代理（如 https://corsproxy.io/?url=），就能按分类与日期精确查最新论文。' +
      detail,
  );
}

/**
 * 按 ID 取精确标题 / 作者 / 发布时间（深度解读出处块用）。
 * 免代理路径：同源后端 → arXiv HTML 页（带 CORS）→ Semantic Scholar。
 */
export async function fetchArxivMeta(id, proxy = '') {
  const attempts = [];
  // 1) 同源后端
  if (await detectBackend()) {
    try {
      const res = await fetch(`${location.origin}/api/arxiv/meta?id=${encodeURIComponent(id)}`, {
        signal: AbortSignal.timeout(20000),
      });
      if (res.ok) {
        const meta = await res.json();
        if (meta?.title) return { ...meta, source: 'backend' };
      }
      attempts.push(`同源后端：HTTP ${res.status}`);
    } catch (err) {
      attempts.push(`同源后端：${(err && err.message) || err}`);
    }
  }

  // 2) 代理 → 官方 API
  if (String(proxy || '').trim()) {
    try {
      const url = `https://export.arxiv.org/api/query?id_list=${encodeURIComponent(id)}&max_results=1`;
      const papers = parseAtomEntries(await fetchApiText(url, proxy));
      if (papers[0]) return { ...papers[0], source: 'arxiv-api' };
      attempts.push('arXiv 官方 API（经代理）：没有返回条目');
    } catch (err) {
      attempts.push(`arXiv 官方 API（经代理）：${(err && err.message) || err}`);
    }
  }

  // 3) arXiv HTML 页（/html/ 带 CORS）→ 直连解析标题与作者
  try {
    const meta = await fetchMetaFromHtml(id);
    if (meta) return meta;
    attempts.push('arXiv HTML 页：没有解析到标题');
  } catch (err) {
    attempts.push(`arXiv HTML 页：${(err && err.message) || err}`);
  }

  // 4) Semantic Scholar
  try {
    const res = await fetch(`${S2}/paper/arXiv:${encodeURIComponent(id)}?fields=title,publicationDate,authors`, {
      signal: AbortSignal.timeout(20000),
    });
    if (res.ok) {
      const p = await res.json();
      if (p?.title) {
        return {
          id,
          title: String(p.title).replace(/\s+/g, ' ').trim(),
          authors: (p.authors || []).map((a) => a?.name).filter(Boolean).slice(0, 3).join(', '),
          published: String(p.publicationDate || '').slice(0, 10),
          url: `https://arxiv.org/abs/${id}`,
          source: 'semanticscholar',
        };
      }
    }
    attempts.push(`semanticscholar：HTTP ${res.status}`);
  } catch (err) {
    attempts.push(`semanticscholar：${(err && err.message) || err}`);
  }

  throw new Error(`取 arXiv 元数据失败（${id}）${attempts.length ? `\n已尝试：${attempts.join('；')}` : ''}`);
}

/**
 * 从 arXiv HTML 版页面解析标题与作者（`/html/` 实测带 `Access-Control-Allow-Origin: *`）。
 * 只解析标题/作者——日期不在 HTML 里，缺了就留空，不猜。
 */
export async function fetchMetaFromHtml(id) {
  const candidates = [`https://arxiv.org/html/${id}`, `https://arxiv.org/html/${id}v1`];
  for (const url of candidates) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(25000), redirect: 'follow' });
      if (!res.ok) continue;
      const html = await res.text();
      return parseArxivHtmlMeta(html, id);
    } catch {
      /* 试下一个候选 */
    }
  }
  return null;
}

/** 纯函数：从 arXiv HTML 里取标题与作者（标题优先 article 里的 h1，其次 <title>）。 */
export function parseArxivHtmlMeta(html, id = '') {
  const src = String(html || '');
  const pick = (re) => {
    const m = src.match(re);
    return m ? m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() : '';
  };
  const title =
    pick(/<h1[^>]*class="[^"]*ltx_title_document[^"]*"[^>]*>([\s\S]*?)<\/h1>/i) || pick(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (!title) return null;
  const authors = [...src.matchAll(/class="[^"]*ltx_personname[^"]*"[^>]*>([\s\S]*?)<\//g)]
    .map((m) =>
      m[1]
        .replace(/<[^>]+>/g, '') // 去掉脚注链接等标签
        .replace(/\s*\d+\s*/g, ' ') // 作者名后面的脚注编号
        .replace(/[\s,，、]+$/, '')
        .replace(/\s+/g, ' ')
        .trim(),
    )
    .filter(Boolean)
    .slice(0, 3)
    .join(', ');
  return {
    id,
    title,
    authors,
    published: '',
    url: `https://arxiv.org/abs/${id}`,
    pdfUrl: `https://arxiv.org/pdf/${id}`,
    source: 'arxiv-html',
  };
}

function parseAtomEntries(xml) {
  const entries = [...String(xml).matchAll(/<entry>([\s\S]*?)<\/entry>/g)].map((m) => m[1]);
  const papers = [];
  for (const e of entries) {
    const rawId = (e.match(/<id>([\s\S]*?)<\/id>/) || [])[1] || '';
    const title = ((e.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || '')
      .replace(/\s+/g, ' ')
      .trim();
    const published = (e.match(/<published>([\s\S]*?)<\/published>/) || [])[1] || '';
    const authors = [...e.matchAll(/<name>([\s\S]*?)<\/name>/g)]
      .map((m) => m[1])
      .slice(0, 3)
      .join(', ');
    if (!title) continue;
    // 统一成不带版本号的 ID（1706.03762v7 → 1706.03762），与其余调用方一致
    const bare = rawId.replace(/^https?:\/\/arxiv\.org\/abs\//, '') || rawId;
    const id = cleanArxivId(bare) || bare;
    papers.push({
      id,
      title,
      published: (published || '').slice(0, 10),
      authors,
      url: `https://arxiv.org/abs/${id}`,
      pdfUrl: `https://arxiv.org/pdf/${id}`,
    });
  }
  return papers;
}

/** 备用通道： fetchText 兜底（某些代理把 export.arxiv.org 包成可读流）。 */
export { fetchText };
