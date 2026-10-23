/**
 * 解析 arXiv ID 并调 API 拿精确标题/作者/发表时间。
 *
 * 入参可以是 arXiv 链接（abs / pdf / html 都行），也可以直接是裸 ID（`1706.03762` 或带版本
 * `1706.03762v7`）——浏览器的 /api/arxiv/meta 端点传的就是裸 ID。
 * 非 arXiv 输入返回 null。
 */
export async function fetchArxivMeta(input) {
  let id;
  const bare = String(input || '').trim();
  const bareMatch = bare.match(/^(\d{4}\.\d{4,5})(v\d+)?$/i);
  if (bareMatch) {
    id = bareMatch[1];
  } else {
    try {
      const u = new URL(bare);
      const m = u.pathname.match(/\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5})(v\d+)?/i);
      if (!m) return null;
      id = m[1];
    } catch {
      return null;
    }
  }

  try {
    const res = await fetch(
      `https://export.arxiv.org/api/query?id_list=${encodeURIComponent(id)}&max_results=1`,
      { signal: AbortSignal.timeout(10000) },
    );
    if (!res.ok) return null;
    const xml = await res.text();
    const entry = xml.match(/<entry>[\s\S]*?<\/entry>/);
    if (!entry) return null;
    const title = (entry[0].match(/<title>([\s\S]*?)<\/title>/) || [])[1] || '';
    const authors = [...entry[0].matchAll(/<name>([\s\S]*?)<\/name>/g)].map((m) => m[1]);
    const published = (entry[0].match(/<published>([\s\S]*?)<\/published>/) || [])[1] || '';
    return {
      title: title.replace(/\s+/g, ' ').trim(),
      authors: authors.join(', '),
      published,
    };
  } catch {
    return null;
  }
}
