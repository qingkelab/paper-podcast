/** 按时间/分类搜索 arXiv 最新论文，只返回标题等元信息（不做解读）。 */

function fmtDate(d) {
  return d.toISOString().slice(0, 10).replace(/-/g, ''); // YYYYMMDD
}

/** 带重试的 fetch：arXiv 对 429/5xx 限流，按 Retry-After / 指数退避重试。 */
async function fetchWithRetry(url, { retries = 3 } = {}) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(25000) });
      if (res.ok) return res;

      const status = res.status;
      if (status === 429 || status >= 500) {
        lastErr = new Error(`arXiv 搜索失败 HTTP ${status}`);
        if (attempt < retries) {
          const retryAfter = Number(res.headers.get('retry-after') || 0);
          const wait = retryAfter > 0 ? retryAfter * 1000 : Math.min(3000 * 2 ** attempt, 20000);
          await new Promise((r) => setTimeout(r, wait));
          continue;
        }
      } else {
        throw new Error(`arXiv 搜索失败 HTTP ${status}`);
      }
    } catch (err) {
      // 网络错误 / 超时也重试
      lastErr = err;
      if (attempt >= retries) break;
      await new Promise((r) => setTimeout(r, Math.min(3000 * 2 ** attempt, 20000)));
    }
  }
  throw lastErr instanceof Error
    ? lastErr
    : new Error('arXiv 搜索失败：请求过于频繁（429），请稍后再试');
}

/**
 * @param {{days?:number, category?:string, keyword?:string, max?:number}} opts
 * @returns {Promise<Array<{id:string,title:string,published:string,authors:string,url:string}>>}
 */
export async function searchArxiv({ days = 3, category = '', keyword = '', max = 60 } = {}) {
  const now = new Date();
  const from = new Date(now.getTime() - days * 86400000);

  const parts = [];
  if (category) parts.push(`cat:${category}`);
  if (keyword) parts.push(`all:"${keyword}"`);
  parts.push(`submittedDate:[${fmtDate(from)}0000 TO ${fmtDate(now)}2359]`);

  const url =
    `https://export.arxiv.org/api/query?search_query=${encodeURIComponent(parts.join(' AND '))}` +
    `&sortBy=submittedDate&sortOrder=descending&max_results=${max}`;

  const res = await fetchWithRetry(url);
  const xml = await res.text();

  const entries = [...xml.matchAll(/<entry>([\s\S]*?)<\/entry>/g)].map((m) => m[1]);
  const papers = [];
  for (const e of entries) {
    const rawId = (e.match(/<id>([\s\S]*?)<\/id>/) || [])[1] || '';
    const title = ((e.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || '').replace(/\s+/g, ' ').trim();
    const published = (e.match(/<published>([\s\S]*?)<\/published>/) || [])[1] || '';
    const authors = [...e.matchAll(/<name>([\s\S]*?)<\/name>/g)].map((m) => m[1]).slice(0, 3).join(', ');
    if (!title) continue;
    // 统一成不带版本号的 ID（2610.02116v1 → 2610.02116），与前端/浏览器版保持一致
    const bare = rawId.replace(/^https?:\/\/arxiv\.org\/abs\//, '') || rawId;
    const id = (bare.match(/^(\d{4}\.\d{4,5})(v\d+)?$/i) || [])[1] || bare;
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
