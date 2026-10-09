/**
 * 关键词高亮（契约 §2.9）。
 *
 * 规则只有一条：**大小写不敏感的原文包含**。
 * 关键词是模型从论文里抽的，可能是英文而正文是中文（或反过来），
 * 所以对不上就不高亮 —— 不做翻译映射，也不为了实现高亮去编造对应词。
 *
 * 产出是**纯数据**（`[{ text, hit }]`），由模板用数组渲染成文本节点。
 * 为什么不用 `v-html` 拼 `<mark>`：正文是模型生成的原文，拼字符串迟早会把
 * 正文里的 `<` 变成标签（那是一条现成的 XSS 路径）。切成片段交给 Vue 渲染，
 * 文本永远只是文本。
 */

export interface HighlightSegment {
  /** 片段原文 */
  text: string
  /** 是否是命中的关键词 */
  hit: boolean
}

/** 一段正文最多切这么多片：关键词命中过密时（例如关键词就是「模型」）保住渲染性能 */
const MAX_SEGMENTS = 400

/**
 * 清洗关键词：去空白、去重（大小写不敏感）、**长的排前面**。
 * 长的排前面是为了让 `Self-Attention` 先于 `Attention` 命中 ——
 * 否则会把 `Self-` 留在高亮外面，看起来像漏了半截。
 */
export function normalizeKeywords(raw: readonly string[] | null | undefined): string[] {
  if (!raw || !raw.length) return []
  const seen = new Set<string>()
  const result: string[] = []
  raw.forEach((item) => {
    const value = typeof item === 'string' ? item.trim() : ''
    if (!value) return
    const key = value.toLowerCase()
    if (seen.has(key)) return
    seen.add(key)
    result.push(value)
  })
  return result.sort((a, b) => b.length - a.length)
}

/**
 * 把正文切成片段。命中处 `hit: true`，其余是原样文本（**一个字都不会丢**）。
 * `keywords` 为空时返回单个未命中片段，调用方不必自己判断。
 */
export function splitByKeywords(
  text: string | null | undefined,
  keywords: readonly string[] | null | undefined,
): HighlightSegment[] {
  const source = text ?? ''
  if (!source) return []
  const list = normalizeKeywords(keywords)
  if (!list.length) return [{ text: source, hit: false }]

  const haystack = source.toLowerCase()
  const needles = list.map((item) => ({ raw: item, lower: item.toLowerCase() }))
  const segments: HighlightSegment[] = []
  let cursor = 0

  while (cursor < source.length && segments.length < MAX_SEGMENTS) {
    let bestIndex = -1
    let bestLength = 0
    for (const needle of needles) {
      const at = haystack.indexOf(needle.lower, cursor)
      if (at < 0) continue
      // 命中位置最靠前的优先；同一位置时**先遍历到的（更长的）**优先，所以用严格小于
      if (bestIndex < 0 || at < bestIndex) {
        bestIndex = at
        bestLength = needle.raw.length
      }
    }
    if (bestIndex < 0) break
    if (bestIndex > cursor) {
      segments.push({ text: source.slice(cursor, bestIndex), hit: false })
    }
    segments.push({ text: source.slice(bestIndex, bestIndex + bestLength), hit: true })
    cursor = bestIndex + bestLength
  }

  if (cursor < source.length) {
    // 超过片段上限时，剩下的正文整体作为一段未命中文本 —— 绝不截断内容
    segments.push({ text: source.slice(cursor), hit: false })
  }
  return segments
}

/** 这一集有没有可高亮的关键词（没有就不显示开关，省得用户白点一次） */
export function hasKeywords(raw: readonly string[] | null | undefined): boolean {
  return normalizeKeywords(raw).length > 0
}
