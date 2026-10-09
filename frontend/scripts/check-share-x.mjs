/**
 * 前端纯逻辑的极小自检（node --experimental-strip-types，无第三方依赖）。
 *
 * 为什么有这个东西：仓库里没有前端测试框架，但「按 X 的加权字数截断」
 * 这类逻辑恰恰是最容易悄悄写错的（CJK 记 2、链接记 23、截断不能切进链接里），
 * 而错了以后表现是「发出去的推文末尾被截掉」—— 只有真发了才看得出来。
 * 所以这里用 Node 自带的 TS 剥离跑几条断言，能跑、够快、不引依赖。
 *
 *   cd frontend && pnpm test:units
 */
import assert from 'node:assert/strict'

import {
  X_TEXT_BUDGET,
  buildEpisodeShareText,
  buildProductShareText,
  weightedLength,
  xIntentUrl,
} from '../src/utils/shareX.ts'

const URL = 'https://qingkelab.github.io/paper-podcast/#/share/abc123def456'

// --- 加权长度 --------------------------------------------------------------

assert.equal(weightedLength('abc'), 3, 'ASCII 按 1 计')
assert.equal(weightedLength('论文'), 4, 'CJK 按 2 计')
assert.equal(weightedLength(URL), 23, '链接固定算 23（与 X 一致）')
// 链接里的字符不再单独计数，否则一条 100 字符的链接会被算成 100+
assert.equal(weightedLength(`看这里 ${URL}`), 6 + 1 + 23)
// 日文 / 韩文同样算宽字符
assert.equal(weightedLength('ひらがな'), 8)
assert.equal(weightedLength('한글'), 4)

// --- 单集分享文案 ----------------------------------------------------------

const shortCase = buildEpisodeShareText({
  title: 'LoRA: Low-Rank Adaptation',
  hook: '冻结原权重、只训练低秩分解矩阵，显存占用大幅下降。',
  url: URL,
  duration: '03:10',
})
assert.ok(shortCase.includes('LoRA: Low-Rank Adaptation'), '短标题要完整保留')
assert.ok(shortCase.includes(URL), '链接必须在正文里（不走 url= 参数）')
assert.ok(shortCase.includes('#论文解读'), '品牌标签要在')
assert.ok(shortCase.includes('03:10'), '时长要带上')
assert.ok(weightedLength(shortCase) <= X_TEXT_BUDGET, '整体不超过预算')

const longCase = buildEpisodeShareText({
  title: `STEPQuant: ${'When and Where Errors Matter in Delta-Rule Recurrent State Quantization '.repeat(3)}`,
  hook: '把量化误差拆成「无记忆」和「有记忆」两部分，' + '分开处理才不会互相掩盖。'.repeat(12),
  url: URL,
  duration: '04:10',
})
assert.ok(weightedLength(longCase) <= X_TEXT_BUDGET, `超长内容也要压进预算，实际 ${weightedLength(longCase)}`)
assert.ok(longCase.includes(URL), '截断绝不能切掉链接')
assert.ok(longCase.includes('#青稞社区'), '截断要优先牺牲正文，不能吃掉标签')
assert.ok(longCase.includes('…'), '被截断的部分要有省略号，别像写了一半')
assert.ok(!/…《|》…/.test(longCase), '省略号不该出现在书名的括号边缘这种奇怪位置')

// 标题里本身带 # 和 & 时不能把链接或标签搅乱
const tricky = buildEpisodeShareText({
  title: 'R&D #1: Attention & You',
  url: URL,
  duration: '05:00',
})
assert.ok(tricky.includes('#1: Attention & You') || tricky.includes('#1: Attention'), '标题里的 # 要原样保留')
assert.ok(tricky.endsWith('#论文解读 #AI播客 #青稞社区'), '标签永远是最后一段')

// --- 项目分享文案 ----------------------------------------------------------

const product = buildProductShareText(URL)
assert.ok(weightedLength(product) <= X_TEXT_BUDGET, '项目文案也要在预算内')
assert.ok(product.includes(URL))
assert.ok(product.includes('关注青稞，每天学习最新论文'), '产品分享要带社区引导语')

// --- intent 地址 -----------------------------------------------------------

const intent = xIntentUrl(shortCase)
assert.ok(intent.startsWith('https://x.com/intent/post?text='))
assert.equal(decodeURIComponent(intent.split('text=')[1]), shortCase, '编码后要能原样解回来')
for (const raw of ['&', '#', '+', '?', '=']) {
  const text = `a${raw}b 论文`
  const encoded = xIntentUrl(text).split('text=')[1]
  assert.ok(!encoded.includes(raw), `${raw} 必须被转义，否则会被当成参数分隔符`)
  assert.equal(decodeURIComponent(encoded), text)
}

console.log('share-x: all assertions passed')
