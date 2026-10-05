/**
 * Mock 配图生成器：封面 / 论文原图 / 生成信息图，全部在浏览器里现场造，不请求任何网络资源。
 *
 * 产物形状对齐 docs/API.md §1/§2 的真实后端产物：
 * - 封面 = 「论文首页渲染图」（Canvas 画成纸面排版，935×1210，与真实后端同尺寸）；
 * - 原图 = 「按 Figure N: 图注从 PDF 渲染出的区域」（Canvas 画成带 "Figure N" 字样的示意图）；
 * - 信息图 = 内联 SVG，**内含 SMIL 动画**（<animateMotion> + <animate>），
 *   转成 Blob URL 交给 <object type="image/svg+xml"> 引用，这样离线也能验证动画在播。
 *
 * 全部输出是确定性伪随机（同一个 seed 每次得到同一张图），刷新页面不会闪来闪去。
 */

import type { Figure, Illustration } from './types'

/** 与真实后端一致的封面尺寸 */
export const COVER_WIDTH = 935
export const COVER_HEIGHT = 1210
/** 与真实后端一致的信息图尺寸 */
export const ILLUSTRATION_WIDTH = 1280
export const ILLUSTRATION_HEIGHT = 720

// ---------------------------------------------------------------------------
// 确定性伪随机
// ---------------------------------------------------------------------------

function hashSeed(input: string): number {
  let hash = 2166136261
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

/** mulberry32：小而够用的确定性 PRNG */
function makeRandom(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let value = state
    value = Math.imul(value ^ (value >>> 15), value | 1)
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61)
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}

function pick<T>(random: () => number, items: readonly T[], fallback: T): T {
  if (!items.length) return fallback
  return items[Math.floor(random() * items.length) % items.length] ?? fallback
}

// ---------------------------------------------------------------------------
// Canvas 基础工具
// ---------------------------------------------------------------------------

type Ctx = CanvasRenderingContext2D

const SERIF = '"Iowan Old Style", Palatino, "Songti SC", Georgia, serif'
const SANS = '-apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif'

/** 按宽度折行；中英混排时按字符累加，够用且不会溢出 */
function wrapText(ctx: Ctx, text: string, maxWidth: number, maxLines: number): string[] {
  const lines: string[] = []
  let current = ''
  for (const char of text) {
    if (char === '\n') {
      lines.push(current)
      current = ''
      if (lines.length >= maxLines) break
      continue
    }
    const next = current + char
    if (ctx.measureText(next).width > maxWidth && current) {
      lines.push(current)
      current = char
      if (lines.length >= maxLines) break
    } else {
      current = next
    }
  }
  if (lines.length < maxLines && current) lines.push(current)
  return lines.slice(0, maxLines)
}

/** 折行 + 末尾省略号（超出 maxLines 时） */
function wrapWithEllipsis(ctx: Ctx, text: string, maxWidth: number, maxLines: number): string[] {
  const lines = wrapText(ctx, text, maxWidth, maxLines)
  const joined = lines.join('')
  if (joined.length < text.length) {
    const last = lines[lines.length - 1] ?? ''
    let trimmed = last
    while (trimmed && ctx.measureText(`${trimmed}…`).width > maxWidth) trimmed = trimmed.slice(0, -1)
    lines[lines.length - 1] = `${trimmed}…`
  }
  return lines
}

function createCanvas(width: number, height: number): { canvas: HTMLCanvasElement; ctx: Ctx } | null {
  if (typeof document === 'undefined') return null
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  return { canvas, ctx }
}

function roundRect(ctx: Ctx, x: number, y: number, width: number, height: number, radius: number): void {
  const r = Math.min(radius, width / 2, height / 2)
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + width, y, x + width, y + height, r)
  ctx.arcTo(x + width, y + height, x, y + height, r)
  ctx.arcTo(x, y + height, x, y, r)
  ctx.arcTo(x, y, x + width, y, r)
  ctx.closePath()
}

/** 纸面配色的强调色：跟真实论文首页一样克制 */
const ACCENTS = ['#b0762f', '#2f6f8f', '#7a4a8f', '#2f7f5f'] as const

// ---------------------------------------------------------------------------
// 封面：论文首页渲染图
// ---------------------------------------------------------------------------

/** 在纸面板上画一块「teaser 图」占位（方框 + 箭头），让封面像真的论文首页 */
function drawTeaser(ctx: Ctx, x: number, y: number, width: number, height: number, accent: string, random: () => number): void {
  ctx.save()
  roundRect(ctx, x, y, width, height, 8)
  ctx.fillStyle = '#f9fbfd'
  ctx.fill()
  ctx.strokeStyle = '#c9d4e2'
  ctx.lineWidth = 1.5
  ctx.stroke()

  const boxes = 3 + (random() > 0.5 ? 1 : 0)
  const gap = 22
  const boxWidth = (width - gap * (boxes + 1)) / boxes
  const boxHeight = height * 0.46
  const boxY = y + height * 0.24
  const centers: Array<{ cx: number; cy: number }> = []

  for (let index = 0; index < boxes; index += 1) {
    const bx = x + gap + index * (boxWidth + gap)
    roundRect(ctx, bx, boxY, boxWidth, boxHeight, 6)
    ctx.fillStyle = index % 2 === 0 ? accent : '#8fa2b8'
    ctx.globalAlpha = 0.16
    ctx.fill()
    ctx.globalAlpha = 1
    ctx.strokeStyle = index % 2 === 0 ? accent : '#93a4b8'
    ctx.lineWidth = 1.4
    ctx.stroke()

    // 框里画几条「文字线」
    ctx.fillStyle = '#9aa8ba'
    for (let line = 0; line < 3; line += 1) {
      const lineWidth = boxWidth * (0.78 - line * 0.14)
      ctx.fillRect(bx + boxWidth * 0.11, boxY + boxHeight * 0.28 + line * (boxHeight * 0.2), lineWidth, 5)
    }
    centers.push({ cx: bx + boxWidth / 2, cy: boxY + boxHeight / 2 })
  }

  // 箭头
  ctx.strokeStyle = '#8fa2b8'
  ctx.fillStyle = '#8fa2b8'
  ctx.lineWidth = 1.6
  for (let index = 0; index < centers.length - 1; index += 1) {
    const from = centers[index]
    const to = centers[index + 1]
    if (!from || !to) continue
    const fromX = from.cx + boxWidth / 2 + 3
    const toX = to.cx - boxWidth / 2 - 3
    ctx.beginPath()
    ctx.moveTo(fromX, from.cy)
    ctx.lineTo(toX - 6, to.cy)
    ctx.stroke()
    ctx.beginPath()
    ctx.moveTo(toX, to.cy)
    ctx.lineTo(toX - 8, to.cy - 4.5)
    ctx.lineTo(toX - 8, to.cy + 4.5)
    ctx.closePath()
    ctx.fill()
  }

  ctx.fillStyle = '#7d8a9c'
  ctx.font = `italic 17px ${SERIF}`
  ctx.textAlign = 'center'
  ctx.fillText('Figure 1: model overview', x + width / 2, y + height - 12)
  ctx.textAlign = 'left'
  ctx.restore()
}

/** 生成封面：外观与后端「PDF 第一页整页渲染」的产物一致 */
export function buildCoverDataUrl(seed: string, title: string, abstract: string): string {
  const made = createCanvas(COVER_WIDTH, COVER_HEIGHT)
  if (!made) return ''
  const { canvas, ctx } = made
  const random = makeRandom(hashSeed(`cover:${seed}`))
  const accent = pick(random, ACCENTS, ACCENTS[0])

  // 纸面
  const paper = ctx.createLinearGradient(0, 0, COVER_WIDTH * 0.4, COVER_HEIGHT)
  paper.addColorStop(0, '#ffffff')
  paper.addColorStop(1, '#eef2f7')
  ctx.fillStyle = paper
  ctx.fillRect(0, 0, COVER_WIDTH, COVER_HEIGHT)

  // 左右页边装饰线，模仿论文排版
  ctx.fillStyle = accent
  ctx.globalAlpha = 0.7
  ctx.fillRect(0, 0, COVER_WIDTH, 7)
  ctx.globalAlpha = 1

  const margin = 82
  const contentWidth = COVER_WIDTH - margin * 2
  let cursorY = margin + 26

  // 会议 / 年份角标
  ctx.fillStyle = '#8794a6'
  ctx.font = `500 19px ${SANS}`
  ctx.fillText('arXiv preprint · mocked page render', margin, cursorY - 24)

  // 标题
  ctx.fillStyle = '#0f1722'
  ctx.font = `700 40px ${SERIF}`
  const titleLines = wrapWithEllipsis(ctx, title, contentWidth, 4)
  for (const line of titleLines) {
    ctx.fillText(line, margin, cursorY + 18)
    cursorY += 52
  }
  cursorY += 10

  // 作者
  ctx.fillStyle = '#3d4a5c'
  ctx.font = `400 20px ${SANS}`
  const authorLines = wrapText(ctx, 'Mock Author · Paper Podcast Demo · Offline Preview', contentWidth, 2)
  for (const line of authorLines) {
    ctx.fillText(line, margin, cursorY + 12)
    cursorY += 30
  }
  cursorY += 14

  // 分隔线
  ctx.strokeStyle = '#c9d4e2'
  ctx.lineWidth = 1.5
  ctx.beginPath()
  ctx.moveTo(margin, cursorY)
  ctx.lineTo(COVER_WIDTH - margin, cursorY)
  ctx.stroke()
  cursorY += 44

  // Abstract
  ctx.fillStyle = '#0f1722'
  ctx.font = `700 26px ${SERIF}`
  ctx.fillText('Abstract', margin, cursorY)
  cursorY += 34

  ctx.fillStyle = '#4b5768'
  ctx.font = `400 19px ${SANS}`
  const body = abstract.trim() || '这是一张 Mock 模式现场生成的封面占位图，用来验证详情页 hero 图的宽高比预留与降级表现。'
  const abstractLines = wrapWithEllipsis(ctx, body, contentWidth, 9)
  for (const line of abstractLines) {
    ctx.fillText(line, margin, cursorY)
    cursorY += 30
  }

  // teaser 图 + 页脚
  const teaserY = Math.min(cursorY + 34, COVER_HEIGHT - 300)
  drawTeaser(ctx, margin, teaserY, contentWidth, 232, accent, random)

  ctx.fillStyle = '#96a3b4'
  ctx.font = `400 17px ${SANS}`
  ctx.textAlign = 'center'
  ctx.fillText('1', COVER_WIDTH / 2, COVER_HEIGHT - 44)
  ctx.textAlign = 'left'

  return canvas.toDataURL('image/png')
}

// ---------------------------------------------------------------------------
// 论文原图：带 "Figure N" 字样的示意图
// ---------------------------------------------------------------------------

const FIGURE_SIZES: ReadonlyArray<{ width: number; height: number }> = [
  { width: 473, height: 690 },
  { width: 626, height: 403 },
  { width: 820, height: 471 },
]

const FIGURE_SHAPES = [
  'encoder-decoder',
  'attention-heatmap',
  'scaling-curve',
  'data-flow',
] as const

function drawPaperSheet(ctx: Ctx, width: number, height: number): void {
  ctx.fillStyle = '#fbfcfe'
  ctx.fillRect(0, 0, width, height)
  ctx.strokeStyle = '#ccd6e3'
  ctx.lineWidth = 2
  ctx.strokeRect(1, 1, width - 2, height - 2)
}

function drawEncoderDecoder(ctx: Ctx, x: number, y: number, width: number, height: number, accent: string): void {
  const blockWidth = width * 0.3
  const blockHeight = height * 0.62
  const positions = [x + width * 0.06, x + width * 0.66]
  const labels = ['Encoder', 'Decoder']
  positions.forEach((bx, index) => {
    roundRect(ctx, bx, y + height * 0.1, blockWidth, blockHeight, 8)
    ctx.fillStyle = '#ffffff'
    ctx.fill()
    ctx.strokeStyle = index === 0 ? accent : '#7f90a6'
    ctx.lineWidth = 2
    ctx.stroke()
    ctx.fillStyle = index === 0 ? accent : '#5d6c80'
    ctx.font = `600 ${Math.round(height * 0.09)}px ${SANS}`
    ctx.textAlign = 'center'
    ctx.fillText(labels[index] ?? '', bx + blockWidth / 2, y + height * 0.28)
    ctx.textAlign = 'left'
    ctx.strokeStyle = '#c2cddc'
    ctx.lineWidth = 1.5
    for (let line = 0; line < 4; line += 1) {
      const ly = y + height * 0.38 + line * (height * 0.09)
      ctx.beginPath()
      ctx.moveTo(bx + blockWidth * 0.12, ly)
      ctx.lineTo(bx + blockWidth * 0.88, ly)
      ctx.stroke()
    }
  })
  // 中间的注意力箭头
  const midY = y + height * 0.45
  ctx.strokeStyle = accent
  ctx.fillStyle = accent
  ctx.lineWidth = 2.2
  ctx.beginPath()
  ctx.moveTo(x + width * 0.36, midY)
  ctx.lineTo(x + width * 0.64, midY)
  ctx.stroke()
  ctx.beginPath()
  ctx.moveTo(x + width * 0.64, midY)
  ctx.lineTo(x + width * 0.64 - 11, midY - 6)
  ctx.lineTo(x + width * 0.64 - 11, midY + 6)
  ctx.closePath()
  ctx.fill()
  ctx.fillStyle = '#6b7a8f'
  ctx.font = `500 ${Math.round(height * 0.075)}px ${SANS}`
  ctx.textAlign = 'center'
  ctx.fillText('attention', x + width * 0.5, midY - 12)
  ctx.textAlign = 'left'
}

function drawHeatmap(ctx: Ctx, x: number, y: number, width: number, height: number, accent: string, random: () => number): void {
  const cell = 8
  const gridWidth = Math.floor((width * 0.72) / cell) * cell
  const gridHeight = Math.floor((height * 0.72) / cell) * cell
  const originX = x + (width - gridWidth) / 2
  const originY = y + (height - gridHeight) / 2
  const cellsX = gridWidth / cell
  const cellsY = gridHeight / cell
  for (let row = 0; row < cellsY; row += 1) {
    for (let col = 0; col < cellsX; col += 1) {
      // 让高亮沿对角线散开，看起来像注意力权重
      const distance = Math.abs(row - col) / cellsY
      const weight = Math.max(0, (1 - distance * 2.1) * (0.55 + random() * 0.45))
      ctx.globalAlpha = Math.min(1, weight)
      ctx.fillStyle = accent
      ctx.fillRect(originX + col * cell, originY + row * cell, cell - 1, cell - 1)
    }
  }
  ctx.globalAlpha = 1
  ctx.fillStyle = '#6b7a8f'
  ctx.font = `500 13px ${SANS}`
  ctx.textAlign = 'center'
  ctx.fillText('input tokens →', originX + gridWidth / 2, originY + gridHeight + 20)
  ctx.textAlign = 'left'
}

function drawCurve(ctx: Ctx, x: number, y: number, width: number, height: number, accent: string, random: () => number): void {
  const padding = Math.min(width, height) * 0.14
  const plotX = x + padding
  const plotY = y + padding * 0.9
  const plotWidth = width - padding * 2
  const plotHeight = height - padding * 2

  ctx.strokeStyle = '#c8d3e1'
  ctx.lineWidth = 1.4
  ctx.beginPath()
  ctx.moveTo(plotX, plotY)
  ctx.lineTo(plotX, plotY + plotHeight)
  ctx.lineTo(plotX + plotWidth, plotY + plotHeight)
  ctx.stroke()

  const series = [accent, '#7f90a6', '#5aa88a']
  series.forEach((color, seriesIndex) => {
    ctx.strokeStyle = color
    ctx.lineWidth = 2.6
    ctx.beginPath()
    let offset = random() * 0.25
    for (let step = 0; step <= 40; step += 1) {
      const t = step / 40
      const value = Math.min(1, 1 - Math.exp(-(t + offset) * (2.4 + seriesIndex)))
      const px = plotX + t * plotWidth
      const py = plotY + plotHeight - value * plotHeight * (0.9 - seriesIndex * 0.08)
      if (step === 0) ctx.moveTo(px, py)
      else ctx.lineTo(px, py)
    }
    ctx.stroke()
    offset += 0.1
  })

  ctx.fillStyle = '#6b7a8f'
  ctx.font = `500 13px ${SANS}`
  ctx.fillText('steps →', plotX + plotWidth - 62, plotY + plotHeight + 20)
}

function drawDataFlow(ctx: Ctx, x: number, y: number, width: number, height: number, accent: string): void {
  const steps = 4
  const gap = width * 0.045
  const boxWidth = (width - gap * (steps + 1)) / steps
  const boxHeight = height * 0.44
  const boxY = y + height * 0.22
  const labels = ['input', 'embed', 'blocks', 'output']
  for (let index = 0; index < steps; index += 1) {
    const bx = x + gap + index * (boxWidth + gap)
    roundRect(ctx, bx, boxY, boxWidth, boxHeight, 7)
    ctx.fillStyle = index === steps - 1 ? accent : '#ffffff'
    ctx.globalAlpha = index === steps - 1 ? 0.18 : 1
    ctx.fill()
    ctx.globalAlpha = 1
    ctx.strokeStyle = index === 0 || index === steps - 1 ? accent : '#a9b6c6'
    ctx.lineWidth = 1.8
    ctx.stroke()
    ctx.fillStyle = '#5d6c80'
    ctx.font = `600 ${Math.round(height * 0.085)}px ${SANS}`
    ctx.textAlign = 'center'
    ctx.fillText(labels[index] ?? '', bx + boxWidth / 2, boxY + boxHeight / 2 + 4)
    ctx.textAlign = 'left'
    if (index < steps - 1) {
      const ax = bx + boxWidth + gap * 0.2
      const ay = boxY + boxHeight / 2
      ctx.strokeStyle = '#93a4b8'
      ctx.lineWidth = 1.8
      ctx.beginPath()
      ctx.moveTo(ax, ay)
      ctx.lineTo(ax + gap * 0.6, ay)
      ctx.stroke()
    }
  }
}

/** 生成一张「论文原图」：带 Figure N 字样 + 与 kind/shape 对应的示意图 */
export function buildFigureDataUrl(seed: string, label: string, shapeHint: string): string {
  const size = FIGURE_SIZES[hashSeed(`${seed}:size`) % FIGURE_SIZES.length] ?? FIGURE_SIZES[0]!
  const made = createCanvas(size.width, size.height)
  if (!made) return ''
  const { canvas, ctx } = made
  const random = makeRandom(hashSeed(`figure:${seed}`))
  const accent = pick(random, ACCENTS, ACCENTS[0])
  const shape = FIGURE_SHAPES.includes(shapeHint as (typeof FIGURE_SHAPES)[number])
    ? shapeHint
    : FIGURE_SHAPES[hashSeed(`${seed}:shape`) % FIGURE_SHAPES.length]!

  drawPaperSheet(ctx, size.width, size.height)

  // 图注标签（真实后端也会把 Figure N 渲染在区域里，这里语义一致）
  ctx.fillStyle = accent
  ctx.font = `700 ${Math.max(15, Math.round(size.height * 0.045))}px ${SANS}`
  ctx.fillText(label, 18, 34)

  const insetX = 18
  const insetY = 54
  const insetWidth = size.width - insetX * 2
  const insetHeight = size.height - insetY - 26

  if (shape === 'attention-heatmap') {
    drawHeatmap(ctx, insetX, insetY, insetWidth, insetHeight, accent, random)
  } else if (shape === 'scaling-curve') {
    drawCurve(ctx, insetX, insetY, insetWidth, insetHeight, accent, random)
  } else if (shape === 'data-flow') {
    drawDataFlow(ctx, insetX, insetY, insetWidth, insetHeight, accent)
  } else {
    drawEncoderDecoder(ctx, insetX, insetY, insetWidth, insetHeight, accent)
  }

  ctx.strokeStyle = '#dbe3ec'
  ctx.lineWidth = 1.5
  ctx.strokeRect(insetX - 4, insetY - 12, insetWidth + 8, insetHeight + 18)

  return canvas.toDataURL('image/png')
}

// ---------------------------------------------------------------------------
// 信息图：内联 SVG（含 SMIL 动画）→ Blob URL
// ---------------------------------------------------------------------------

function escapeXml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
}

function truncate(text: string, max: number): string {
  const trimmed = text.trim()
  return trimmed.length <= max ? trimmed : `${trimmed.slice(0, max)}…`
}

/**
 * 生成信息图 SVG 源码。
 * 关键点：内含 SMIL 动画（<animateMotion> 让光点沿流水线跑，<animate> 让节点呼吸），
 * 因此必须用 <object type="image/svg+xml"> 引用才会播放动画。
 */
export function buildIllustrationSvg(title: string, accent = '#5ea8e0'): string {
  const safeTitle = escapeXml(truncate(title, 34))
  const nodes = [
    { x: 70, label: '论文 PDF' },
    { x: 350, label: '结构解析' },
    { x: 630, label: '核心解读' },
    { x: 910, label: '双人播客' },
  ]
  const nodeWidth = 240
  const nodeHeight = 150
  const nodeY = 300
  const centerY = nodeY + nodeHeight / 2

  const nodeMarkup = nodes
    .map((node, index) => {
      const begin = `${(index * 0.45).toFixed(2)}s`
      return `    <g>
      <rect x="${node.x}" y="${nodeY}" width="${nodeWidth}" height="${nodeHeight}" rx="16" fill="#16233a" stroke="${accent}" stroke-opacity="0.55" stroke-width="2">
        <animate attributeName="stroke-opacity" values="0.35;0.85;0.35" dur="3.2s" begin="${begin}" repeatCount="indefinite" />
      </rect>
      <circle cx="${node.x + 30}" cy="${nodeY + 38}" r="7" fill="${accent}">
        <animate attributeName="r" values="6;9;6" dur="2.4s" begin="${begin}" repeatCount="indefinite" />
      </circle>
      <text x="${node.x + 52}" y="${nodeY + 45}" fill="#eef4fb" font-size="26" font-weight="600" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif">${escapeXml(node.label)}</text>
      <text x="${node.x + 30}" y="${nodeY + 92}" fill="#8ea3bd" font-size="19" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif">Mock 信息图 · 节点 ${index + 1}</text>
      <rect x="${node.x + 30}" y="${nodeY + 112}" width="${nodeWidth - 60}" height="8" rx="4" fill="#22314a">
        <animate attributeName="width" values="${(nodeWidth - 60) * 0.35};${nodeWidth - 60};${(nodeWidth - 60) * 0.35}" dur="4.6s" begin="${begin}" repeatCount="indefinite" />
      </rect>
    </g>`
    })
    .join('\n')

  const arrowMarkup = nodes
    .slice(0, -1)
    .map((node, index) => {
      const next = nodes[index + 1]
      const fromX = node.x + nodeWidth + 6
      const toX = (next?.x ?? fromX) - 10
      return `    <g>
      <line x1="${fromX}" y1="${centerY}" x2="${toX}" y2="${centerY}" stroke="${accent}" stroke-opacity="0.4" stroke-width="3" stroke-dasharray="10 8">
        <animate attributeName="stroke-dashoffset" values="0;-36" dur="1.4s" repeatCount="indefinite" />
      </line>
      <path d="M${toX + 12},${centerY} L${toX - 4},${centerY - 8} L${toX - 4},${centerY + 8} Z" fill="${accent}" fill-opacity="0.85" />
    </g>`
    })
    .join('\n')

  const first = nodes[0] ?? { x: 70, label: '' }
  const last = nodes[nodes.length - 1] ?? first
  const pathD = `M ${first.x + nodeWidth / 2} ${nodeY - 34} L ${first.x + nodeWidth / 2} ${centerY} L ${last.x + nodeWidth / 2} ${centerY} L ${last.x + nodeWidth / 2} ${nodeY + nodeHeight + 40}`

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${ILLUSTRATION_WIDTH} ${ILLUSTRATION_HEIGHT}" width="${ILLUSTRATION_WIDTH}" height="${ILLUSTRATION_HEIGHT}" role="img" aria-label="${safeTitle} 信息图">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0f1a2c" />
      <stop offset="100%" stop-color="#132238" />
    </linearGradient>
    <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="6" result="blur" />
      <feMerge>
        <feMergeNode in="blur" />
        <feMergeNode in="SourceGraphic" />
      </feMerge>
    </filter>
  </defs>

  <rect x="0" y="0" width="${ILLUSTRATION_WIDTH}" height="${ILLUSTRATION_HEIGHT}" rx="22" fill="url(#bg)" />
  <rect x="1" y="1" width="${ILLUSTRATION_WIDTH - 2}" height="${ILLUSTRATION_HEIGHT - 2}" rx="21" fill="none" stroke="#24344c" stroke-width="2" />

  <text x="70" y="104" fill="#f2f6fc" font-size="44" font-weight="700" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif">${safeTitle}</text>
  <text x="70" y="148" fill="#8ea3bd" font-size="22" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif">Mock 模式现场生成的信息图 · SVG 内含 SMIL 动画</text>
  <line x1="70" y1="182" x2="${ILLUSTRATION_WIDTH - 70}" y2="182" stroke="#24344c" stroke-width="2" />

${arrowMarkup}

${nodeMarkup}

  <circle r="7" fill="#ffd479" filter="url(#glow)">
    <animateMotion dur="5.2s" repeatCount="indefinite" rotate="auto" path="${pathD}" />
    <animate attributeName="opacity" values="0.35;1;0.35" dur="2.6s" repeatCount="indefinite" />
  </circle>

  <text x="70" y="${ILLUSTRATION_HEIGHT - 52}" fill="#7d90aa" font-size="20" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif">动画验证：黄色光点沿流水线循环移动，节点描边与进度条呼吸</text>
</svg>`
}

function svgToBlobUrl(svg: string): string {
  if (typeof URL === 'undefined' || typeof Blob === 'undefined') return ''
  return URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }))
}

// ---------------------------------------------------------------------------
// 聚合入口
// ---------------------------------------------------------------------------

export interface MockArtwork {
  cover_url: string | null
  cover_width: number | null
  cover_height: number | null
  figures: Figure[]
  illustration: Illustration | null
}

export interface BuildArtworkInput {
  /** 稳定 seed（用 episode id，刷新后重算得到同一张图） */
  seed: string
  title: string
  abstract: string
  sourceType: 'pdf' | 'url' | 'text'
  /** Blob URL 的回收登记（交给 mock.ts 的 objectUrls 统一 revoke） */
  track: (url: string) => void
}

const FIGURE_CAPTIONS = [
  (label: string) => `${label}: 模型整体结构示意 —— 左侧做特征提取，右侧逐层生成输出，中间用注意力连接。`,
  (label: string) => `${label}: 注意力权重分布。对角线越亮表示当前位置对同位置的信息依赖越强。`,
  (label: string) => `${label}: 训练曲线对比。横轴为训练步数，纵轴为归一化指标，三条曲线对应消融实验的三组设置。`,
] as const

function makeFigure(seed: string, index: number, shape: string, title: string, id: string): Figure {
  const label = `Figure ${index + 1}`
  const url = buildFigureDataUrl(`${seed}:${index}`, label, shape)
  const size = FIGURE_SIZES[hashSeed(`${seed}:${index}:size`) % FIGURE_SIZES.length] ?? FIGURE_SIZES[0]!
  const captionOf = FIGURE_CAPTIONS[index % FIGURE_CAPTIONS.length] ?? FIGURE_CAPTIONS[0]
  return {
    id,
    kind: 'figure',
    label,
    caption: `${captionOf(label)} （${title} · Mock 生成，非真实论文插图）`,
    page: 3 + index,
    url,
    width: size.width,
    height: size.height,
  }
}

/**
 * 一次生成某个 episode 的全部配图。
 * 语义上刻意与后端对齐：text 来源没有 PDF，所以没有原图，封面退回用信息图。
 */
export function buildArtwork(input: BuildArtworkInput): MockArtwork {
  const { seed, title, abstract, sourceType } = input
  const random = makeRandom(hashSeed(`artwork:${seed}`))
  const hasPdf = sourceType !== 'text'

  // 信息图永远有（契约：模型生成的信息图，source 区分 model / fallback）
  const svgUrl = svgToBlobUrl(buildIllustrationSvg(title))
  const pngUrl = buildIllustrationPngDataUrl(title)
  if (svgUrl) input.track(svgUrl)

  const illustration: Illustration = {
    png_url: pngUrl,
    svg_url: svgUrl,
    width: ILLUSTRATION_WIDTH,
    height: ILLUSTRATION_HEIGHT,
    source: random() > 0.25 ? 'model' : 'fallback',
  }

  if (!hasPdf) {
    // text 来源：没有 PDF → 没有原图；封面退回用信息图（对齐契约 §2 的 fallback 描述）
    return {
      cover_url: pngUrl || svgUrl || null,
      cover_width: ILLUSTRATION_WIDTH,
      cover_height: ILLUSTRATION_HEIGHT,
      figures: [],
      illustration,
    }
  }

  const count = random() > 0.5 ? 3 : 2
  const shapes = ['encoder-decoder', 'attention-heatmap', 'scaling-curve', 'data-flow']
  const figures: Figure[] = []
  for (let index = 0; index < count; index += 1) {
    figures.push(makeFigure(seed, index, shapes[index % shapes.length] ?? 'encoder-decoder', title, `f${index + 1}`))
  }

  return {
    cover_url: buildCoverDataUrl(seed, title, abstract) || null,
    cover_width: COVER_WIDTH,
    cover_height: COVER_HEIGHT,
    figures,
    illustration,
  }
}

/** 信息图的 PNG 版（Canvas 现场画，供 <object> 里的 <img> 降级使用） */
export function buildIllustrationPngDataUrl(title: string): string {
  const made = createCanvas(ILLUSTRATION_WIDTH, ILLUSTRATION_HEIGHT)
  if (!made) return ''
  const { canvas, ctx } = made
  const accent = '#5ea8e0'

  const background = ctx.createLinearGradient(0, 0, ILLUSTRATION_WIDTH, ILLUSTRATION_HEIGHT)
  background.addColorStop(0, '#0f1a2c')
  background.addColorStop(1, '#132238')
  ctx.fillStyle = background
  ctx.fillRect(0, 0, ILLUSTRATION_WIDTH, ILLUSTRATION_HEIGHT)

  ctx.fillStyle = '#f2f6fc'
  ctx.font = `700 44px ${SANS}`
  ctx.fillText(truncate(title, 30), 70, 104)

  ctx.fillStyle = '#8ea3bd'
  ctx.font = `400 22px ${SANS}`
  ctx.fillText('Mock 模式现场生成的信息图（静态 PNG）', 70, 148)

  ctx.strokeStyle = '#24344c'
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.moveTo(70, 182)
  ctx.lineTo(ILLUSTRATION_WIDTH - 70, 182)
  ctx.stroke()

  const labels = ['论文 PDF', '结构解析', '核心解读', '双人播客']
  const nodeWidth = 240
  const nodeHeight = 150
  const nodeY = 300
  labels.forEach((label, index) => {
    const x = 70 + index * 280
    roundRect(ctx, x, nodeY, nodeWidth, nodeHeight, 16)
    ctx.fillStyle = '#16233a'
    ctx.fill()
    ctx.strokeStyle = accent
    ctx.globalAlpha = 0.55
    ctx.lineWidth = 2
    ctx.stroke()
    ctx.globalAlpha = 1
    ctx.fillStyle = accent
    ctx.beginPath()
    ctx.arc(x + 30, nodeY + 38, 7, 0, Math.PI * 2)
    ctx.fill()
    ctx.fillStyle = '#eef4fb'
    ctx.font = `600 26px ${SANS}`
    ctx.fillText(label, x + 52, nodeY + 45)
    ctx.fillStyle = '#8ea3bd'
    ctx.font = `400 19px ${SANS}`
    ctx.fillText(`节点 ${index + 1}`, x + 30, nodeY + 92)

    if (index < labels.length - 1) {
      const arrowX = x + nodeWidth + 8
      const centerY = nodeY + nodeHeight / 2
      ctx.strokeStyle = accent
      ctx.globalAlpha = 0.6
      ctx.lineWidth = 3
      ctx.beginPath()
      ctx.moveTo(arrowX, centerY)
      ctx.lineTo(arrowX + 24, centerY)
      ctx.stroke()
      ctx.globalAlpha = 1
    }
  })

  ctx.fillStyle = '#7d90aa'
  ctx.font = `400 20px ${SANS}`
  ctx.fillText('静态版用于 <object> 的降级展示；动画请见 illustration.svg', 70, ILLUSTRATION_HEIGHT - 60)

  return canvas.toDataURL('image/png')
}
