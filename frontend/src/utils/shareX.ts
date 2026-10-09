/**
 * 分享到 X（Twitter）。
 *
 * 走的是官方 **intent 链接**，不需要任何密钥、OAuth 或第三方 SDK：
 * `https://x.com/intent/post?text=…` 打开一个预填好的发帖框，用户自己决定发不发。
 * 这一点很重要 —— 我们在任何情况下都**不替用户发帖**，也不碰他的账号。
 *
 * 三个必须自己处理的细节：
 *
 * 1. **长度按 X 的「加权字数」算，不是字符数**。中日韩字符权重 2，链接固定算 23。
 *    不按这个算的话，一条看起来很短的标题加上链接就可能被 X 截断末尾 ——
 *    而末尾正好是我们最想让人看到的那句品牌引导。
 * 2. **链接必须是公开链接**。私有单集没有对外可访问的地址，硬发出去对方只会看到
 *    一个「需要登录/不存在」的页面。调用方要么先开分享，要么别显示这个按钮。
 * 3. **链接整条拼进正文，不用 `url=` 参数**。用参数的话 X 会拿链接去抓卡片，
 *    而我们的公开页是 SPA（hash 路由），爬虫抓不到内容，卡片会是空的；
 *    拼进正文反而稳定，位置也能自己控制。
 */

const CJK_RANGES: ReadonlyArray<readonly [number, number]> = [
  [0x1100, 0x11ff], // 韩文字母
  [0x2e80, 0xa4cf], // 中日韩部首 / 假名 / 汉字
  [0xac00, 0xd7a3], // 韩文音节
  [0xf900, 0xfaff], // 兼容汉字
  [0xfe30, 0xfe4f], // 中日韩兼容符号
  [0xff00, 0xff60], // 全角形式
  [0xffe0, 0xffe6], // 全角符号
  [0x20000, 0x3fffd], // 扩展区汉字
]

/** 是否是「权重 2」的宽字符（X 对 CJK 就是这么算的） */
function isWide(char: string): boolean {
  const code = char.codePointAt(0) ?? 0
  return CJK_RANGES.some(([start, end]) => code >= start && code <= end)
}

/** X 的加权长度：CJK 记 2，其余记 1，URL 记 23 */
export function weightedLength(text: string): number {
  const withoutUrls = text.replace(/https?:\/\/\S+/g, '')
  let total = (text.match(/https?:\/\/\S+/g) ?? []).length * 23
  for (const char of withoutUrls) total += isWide(char) ? 2 : 1
  return total
}

/** 按加权长度截断（加不加省略号由调用方决定） */
function clampWeighted(text: string, limit: number, suffix = '…'): string {
  if (weightedLength(text) <= limit) return text
  let out = ''
  let used = 0
  const budget = Math.max(0, limit - weightedLength(suffix))
  for (const char of text) {
    const cost = isWide(char) ? 2 : 1
    if (used + cost > budget) break
    out += char
    used += cost
  }
  return `${out.trimEnd()}${suffix}`
}

/** 单条推文的加权预算。X 上限 280，留一点余量给「不同客户端对 emoji 计数不一致」 */
export const X_TEXT_BUDGET = 262

export interface XShareInput {
  /** 论文标题（可能很长，会自动截断） */
  title: string
  /** 一句话看点，通常取解读里的「创新点」第一条 */
  hook?: string | null
  /** 公开可访问的地址 */
  url: string
  /** 时长文案，如 `04:10` */
  duration?: string | null
  /** 附加标签（不带 #） */
  hashtags?: string[]
}

const DEFAULT_HASHTAGS = ['论文解读', 'AI播客', '青稞社区']

function hashtagLine(tags: readonly string[]): string {
  return tags.map((tag) => `#${tag}`).join(' ')
}

/**
 * 拼一条「分享某期播客」的推文。
 *
 * 顺序是「标题 → 看点 → 链接 → 标签」：先给内容、再给地址、最后给标签 ——
 * 标签放前面会让人一眼看到一串 # 号，反而看不出这条在讲什么。
 */
export function buildEpisodeShareText(input: XShareInput): string {
  const tags = hashtagLine(input.hashtags?.length ? input.hashtags : DEFAULT_HASHTAGS)
  const tail = `\n\n${input.url}\n\n${tags}`
  const head = input.duration ? `的双人 AI 播客解读（${input.duration}）` : '的双人 AI 播客解读'

  // 固定开销 = 书名号 + 模板 + 链接 + 标签 + **看点前那个换行**。
  // 少算任何一个都会让最终文案超预算 —— 实测漏算换行时刚好超 1 个加权字，
  // 而「差一个字」的表现就是 X 把末尾的品牌标签截掉，正好截掉最想让人看到的那句。
  const overhead = weightedLength(`《》${head}`) + weightedLength(tail) + 1
  const budget = Math.max(20, X_TEXT_BUDGET - overhead)

  // 看点比标题更像「人话」，但也更能省，所以先让标题占 60%，看点用剩下的
  const titleBudget = Math.min(budget, Math.round(budget * 0.6))
  const shortTitle = clampWeighted(input.title.trim(), titleBudget)
  const hookBudget = Math.max(0, budget - weightedLength(shortTitle))
  const hookText = input.hook?.trim() ? input.hook.trim().replace(/\s+/g, ' ') : ''
  // 看点被截断时也要留省略号：不留的话读起来是「话说到一半就没了」，
  // 而这条推文是给陌生人看的第一印象（实测截到「…该方法用门控」就断在那儿）
  const hook = hookText ? clampWeighted(hookText, hookBudget) : ''

  const body = hook ? `\n${hook}` : ''
  return `《${shortTitle}》${head}${body}${tail}`
}

/** 拼一条「分享这个项目」的推文（首页 / 关于页用） */
export function buildProductShareText(url: string, tags: readonly string[] = DEFAULT_HASHTAGS): string {
  const pitch = clampWeighted(
    '上传一篇论文（PDF 或链接），几分钟后拿到：视频解读播客 + 双人对谈音频 + 逐段脚本 + 结构化解读。',
    X_TEXT_BUDGET - weightedLength(`\n\n${url}\n\n关注青稞，每天学习最新论文。\n\n${hashtagLine(tags)}`),
  )
  return `${pitch}\n\n${url}\n\n关注青稞，每天学习最新论文。\n\n${hashtagLine(tags)}`
}

/**
 * 生成 intent 地址。
 *
 * 用 `x.com/intent/post`：老的 `twitter.com/intent/tweet` 会 301 过去，
 * 多一跳而且在部分客户端里会被当成外链拦截。
 */
export function xIntentUrl(text: string): string {
  return `https://x.com/intent/post?text=${encodeURIComponent(text)}`
}
