/**
 * 浏览器端抓取：默认直连；对不放 CORS 的站点走用户配置的代理。
 * 代理格式两种：`https://corsproxy.io/?url=`（尾部拼接编码后的 URL）或含 `{url}` 占位符。
 */

export function proxiedUrl(url, proxy = '') {
  const p = String(proxy || '').trim();
  if (!p) return url;
  if (p.includes('{url}')) return p.replace('{url}', encodeURIComponent(url));
  return p + encodeURIComponent(url);
}

export function isArxivUrl(url) {
  try {
    return /(^|\.)arxiv\.org$/i.test(new URL(url).hostname);
  } catch {
    return false;
  }
}

/** 从 arXiv 链接解析论文 ID（abs/pdf/html 均可）。 */
export function parseArxivId(url) {
  try {
    const m = new URL(url).pathname.match(/\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5})(v\d+)?/i);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

async function doFetch(url, { proxy = '', timeoutMs = 30000, forceProxy = false } = {}) {
  const target = forceProxy ? proxiedUrl(url, proxy) : url;
  const res = await fetch(target, { signal: AbortSignal.timeout(timeoutMs), redirect: 'follow' });
  return res;
}

/**
 * 抓文本：直连失败（CORS/网络错误）且配置了代理时，自动用代理重试一次。
 * forceProxy=true 时跳过直连（已知无 CORS 的端点，如 export.arxiv.org）。
 */
export async function fetchText(url, opts = {}) {
  const { proxy = '', forceProxy = false } = opts;
  try {
    const res = await doFetch(url, { ...opts, forceProxy });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.text();
  } catch (err) {
    if (!forceProxy && proxy) {
      const res = await doFetch(url, { ...opts, forceProxy: true });
      if (!res.ok) throw new Error(`HTTP ${res.status}（经代理）`);
      return await res.text();
    }
    const msg = (err && err.message) || String(err);
    if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) {
      throw new Error(
        `无法直接抓取（目标站点未开放跨域）：${url}\n可在「接口设置」里填一个 CORS 代理后重试。`,
      );
    }
    throw err;
  }
}

/** 抓二进制（PDF 等），失败同样可回退代理。 */
export async function fetchBytes(url, opts = {}) {
  const { proxy = '', forceProxy = false } = opts;
  try {
    const res = await doFetch(url, { ...opts, forceProxy });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.arrayBuffer();
  } catch (err) {
    if (!forceProxy && proxy) {
      const res = await doFetch(url, { ...opts, forceProxy: true });
      if (!res.ok) throw new Error(`HTTP ${res.status}（经代理）`);
      return await res.arrayBuffer();
    }
    const msg = (err && err.message) || String(err);
    if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) {
      throw new Error(
        `无法直接抓取（目标站点未开放跨域）：${url}\n可在「接口设置」里填一个 CORS 代理后重试。`,
      );
    }
    throw err;
  }
}
