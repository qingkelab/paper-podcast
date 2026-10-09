/**
 * Mock 模式的「视频解读播客」：用 Canvas + MediaRecorder 现场录一段竖版占位视频。
 *
 * 为什么不打包一个真实 mp4：真实产物约 4.6MB，塞进仓库既臃肿又和「离线演示」无关；
 * 而 <video> 必须是真视频，静态图代替不了（破播放器）。
 * 所以这里在浏览器里录：Canvas 画 936×1210（竖版，与契约同比例，尺寸减半以省 CPU）
 * 的几帧画面 + 一条静音音轨，MediaRecorder 直接产出 Blob URL。
 *
 * 关键取舍：
 * - 录制是**真实时间**的，所以只录 ~6 秒，且全站只录一次（见 mock.ts 的共享逻辑）；
 * - 任何一步不被浏览器支持（无 MediaRecorder / 无 canvas.captureStream / 编码失败）
 *   都返回 null，由调用方降级为「没有视频」，绝不抛异常——离线演示不能因为
 *   视频造不出来就整页报错。
 */

/** 画幅：契约是 936×1210（必须偶数），这里等比减半，省一半像素量，比例完全一致 */
export const MOCK_VIDEO_WIDTH = 468
export const MOCK_VIDEO_HEIGHT = 605

/** 演示视频总时长（录制是真实时间，别再长了） */
const RECORD_SEC = 6
/** 录制的帧率：够看出「画面在切换」即可 */
const FPS = 12

/** 与 main.css 的 --speaker-a / --speaker-b 保持一致，字幕条沿用同一套配色 */
const SPEAKER_COLORS = {
  A: '#d3a24a',
  B: '#79a9c9',
} as const

/** 候选编码：MediaRecorder 各家支持不一致，逐个探测 */
const CANDIDATE_MIME_TYPES = [
  'video/webm;codecs=vp9,opus',
  'video/webm;codecs=vp8,opus',
  'video/webm;codecs=vp8',
  'video/webm',
  'video/mp4;codecs=avc1.42E01E,mp4a.40.2',
  'video/mp4',
] as const

export interface MockVideoSegment {
  speaker: 'A' | 'B'
  text: string
}

export interface RenderMockVideoParams {
  title: string
  /** 脚本段落，用来生成画面与字幕条 */
  segments: MockVideoSegment[]
  /** 期望总时长；默认 RECORD_SEC */
  durationSec?: number
}

export interface RenderMockVideoResult {
  blob: Blob
  /** 录制时长（秒） */
  durationSec: number
  width: number
  height: number
  /** 画面段数（片头 + 正文 + 片尾） */
  sceneCount: number
  mimeType: string
}

type Ctx = CanvasRenderingContext2D

const SERIF = '"Iowan Old Style", Palatino, "Songti SC", Georgia, serif'
const SANS = '-apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif'

/** 一个画面：片头（论文首页）→ 正文（论文原图）→ 片尾（信息图） */
interface Scene {
  kind: 'intro' | 'body' | 'outro'
  /** 画面顶部的小标签 */
  label: string
  /** 字幕（正文画面用脚本原文，片头/片尾用固定文案） */
  speaker: 'A' | 'B'
  subtitle: string
}

function createContext(): { canvas: HTMLCanvasElement; ctx: Ctx } | null {
  if (typeof document === 'undefined') return null
  const canvas = document.createElement('canvas')
  canvas.width = MOCK_VIDEO_WIDTH
  canvas.height = MOCK_VIDEO_HEIGHT
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

/** 按宽度折行（中英混排按字符累加，够用且不会溢出） */
function wrapText(ctx: Ctx, text: string, maxWidth: number, maxLines: number): string[] {
  const lines: string[] = []
  let current = ''
  for (const char of text) {
    const next = current + char
    if (ctx.measureText(next).width > maxWidth && current) {
      lines.push(current)
      current = char
      if (lines.length >= maxLines) return lines
    } else {
      current = next
    }
  }
  if (current && lines.length < maxLines) lines.push(current)
  return lines
}

function truncate(text: string, max: number): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  return clean.length <= max ? clean : `${clean.slice(0, max)}…`
}

/** 把脚本段落压成少量画面：录制时长有限，画面太多每段都看不清 */
function buildScenes(title: string, segments: MockVideoSegment[]): Scene[] {
  const usable = segments.length ? segments : [{ speaker: 'A' as const, text: '这是 Mock 模式现场录制的占位视频。' }]
  const bodyCount = Math.min(4, usable.length)
  const step = usable.length / bodyCount

  const scenes: Scene[] = [
    {
      kind: 'intro',
      label: '片头 · 论文首页',
      speaker: 'A',
      subtitle: `欢迎收听《${truncate(title, 22)}》的视频解读，本期由两位主播逐段对谈。`,
    },
  ]

  for (let index = 0; index < bodyCount; index += 1) {
    const segment = usable[Math.min(usable.length - 1, Math.floor(index * step))]
    if (!segment) continue
    scenes.push({
      kind: 'body',
      label: `正文 · 论文原图 ${index + 1}/${bodyCount}`,
      speaker: segment.speaker,
      subtitle: segment.text,
    })
  }

  const last = usable[usable.length - 1]
  scenes.push({
    kind: 'outro',
    label: '片尾 · 生成信息图',
    speaker: last?.speaker ?? 'B',
    subtitle: '以上就是这篇论文的核心内容，完整解读请见下方结构化解读卡片。',
  })

  return scenes
}

/** 片头：模拟论文首页（纸面排版），与 mockArt.ts 的封面观感一致 */
function drawIntro(ctx: Ctx, title: string): void {
  const gradient = ctx.createLinearGradient(0, 0, MOCK_VIDEO_WIDTH * 0.4, MOCK_VIDEO_HEIGHT)
  gradient.addColorStop(0, '#ffffff')
  gradient.addColorStop(1, '#eef2f7')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, MOCK_VIDEO_WIDTH, MOCK_VIDEO_HEIGHT)

  ctx.fillStyle = '#b0762f'
  ctx.fillRect(0, 0, MOCK_VIDEO_WIDTH, 5)

  const margin = 34
  ctx.fillStyle = '#8794a6'
  ctx.font = `500 11px ${SANS}`
  ctx.fillText('arXiv preprint · mocked page render', margin, 46)

  ctx.fillStyle = '#0f1722'
  ctx.font = `700 25px ${SERIF}`
  let cursorY = 84
  for (const line of wrapText(ctx, title, MOCK_VIDEO_WIDTH - margin * 2, 3)) {
    ctx.fillText(line, margin, cursorY)
    cursorY += 32
  }

  ctx.fillStyle = '#3d4a5c'
  ctx.font = `400 12px ${SANS}`
  ctx.fillText('Mock Author · Paper Podcast Demo', margin, cursorY + 10)
  cursorY += 30

  ctx.strokeStyle = '#c9d4e2'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.moveTo(margin, cursorY)
  ctx.lineTo(MOCK_VIDEO_WIDTH - margin, cursorY)
  ctx.stroke()

  ctx.fillStyle = '#0f1722'
  ctx.font = `700 15px ${SERIF}`
  ctx.fillText('Abstract', margin, cursorY + 30)
  ctx.fillStyle = '#4b5768'
  ctx.font = `400 12px ${SANS}`
  let bodyY = cursorY + 54
  for (let line = 0; line < 9; line += 1) {
    const lineWidth = (MOCK_VIDEO_WIDTH - margin * 2) * (line % 4 === 3 ? 0.55 : 1)
    ctx.fillRect(margin, bodyY, lineWidth, 6)
    bodyY += 18
  }

  // teaser 图占位
  const teaserY = bodyY + 16
  roundRect(ctx, margin, teaserY, MOCK_VIDEO_WIDTH - margin * 2, 120, 6)
  ctx.fillStyle = '#f7fafd'
  ctx.fill()
  ctx.strokeStyle = '#c9d4e2'
  ctx.stroke()
  const boxes = 3
  const gap = 10
  const boxWidth = (MOCK_VIDEO_WIDTH - margin * 2 - gap * (boxes + 1)) / boxes
  for (let index = 0; index < boxes; index += 1) {
    const bx = margin + gap + index * (boxWidth + gap)
    roundRect(ctx, bx, teaserY + 22, boxWidth, 62, 4)
    ctx.fillStyle = index % 2 === 0 ? '#b0762f' : '#8fa2b8'
    ctx.globalAlpha = 0.18
    ctx.fill()
    ctx.globalAlpha = 1
    ctx.strokeStyle = index % 2 === 0 ? '#b0762f' : '#93a4b8'
    ctx.stroke()
  }
  ctx.fillStyle = '#7d8a9c'
  ctx.font = `italic 11px ${SERIF}`
  ctx.textAlign = 'center'
  ctx.fillText('Figure 1: model overview', MOCK_VIDEO_WIDTH / 2, teaserY + 106)
  ctx.textAlign = 'left'
}

/** 正文：模拟「按图注从 PDF 渲染出的论文原图」 */
function drawBody(ctx: Ctx, accent: string, progress: number): void {
  ctx.fillStyle = '#fbfcfe'
  ctx.fillRect(0, 0, MOCK_VIDEO_WIDTH, MOCK_VIDEO_HEIGHT)
  ctx.strokeStyle = '#ccd6e3'
  ctx.lineWidth = 1.5
  ctx.strokeRect(1, 1, MOCK_VIDEO_WIDTH - 2, MOCK_VIDEO_HEIGHT - 2)

  // 注意力矩阵示意（网格按 progress 逐步点亮，让画面「在动」）
  const cell = 13
  const cols = Math.floor((MOCK_VIDEO_WIDTH - 96) / cell)
  const rows = Math.floor((MOCK_VIDEO_HEIGHT - 260) / cell)
  const originX = (MOCK_VIDEO_WIDTH - cols * cell) / 2
  const originY = 120
  const lit = Math.ceil(cols * rows * progress)
  for (let index = 0; index < cols * rows; index += 1) {
    const row = Math.floor(index / cols)
    const col = index % cols
    const weight = Math.max(0, 1 - (Math.abs(row - col) / rows) * 2.1)
    ctx.globalAlpha = index < lit ? Math.min(1, weight * 1.6) : 0.06
    ctx.fillStyle = accent
    ctx.fillRect(originX + col * cell, originY + row * cell, cell - 2, cell - 2)
  }
  ctx.globalAlpha = 1

  ctx.fillStyle = '#6b7a8f'
  ctx.font = `500 11px ${SANS}`
  ctx.textAlign = 'center'
  ctx.fillText('input tokens →', MOCK_VIDEO_WIDTH / 2, originY + rows * cell + 22)
  ctx.textAlign = 'left'
}

/** 片尾：模拟「模型生成的信息图」。白底，与后端 illustration.ILLUSTRATION_PALETTE 一致 */
function drawOutro(ctx: Ctx, title: string, progress: number): void {
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, MOCK_VIDEO_WIDTH, MOCK_VIDEO_HEIGHT)

  ctx.fillStyle = '#1a2233'
  ctx.font = `700 21px ${SANS}`
  ctx.fillText(truncate(title, 18), 32, 74)

  ctx.fillStyle = '#6b7a8f'
  ctx.font = `400 11px ${SANS}`
  ctx.fillText('Mock 信息图 · 离线现场生成', 32, 96)

  const labels = ['论文 PDF', '结构解析', '核心解读', '双人播客']
  labels.forEach((label, index) => {
    const y = 150 + index * 92
    const active = progress > (index + 0.5) / labels.length
    roundRect(ctx, 32, y, MOCK_VIDEO_WIDTH - 64, 72, 10)
    ctx.fillStyle = '#f4f7fa'
    ctx.fill()
    ctx.strokeStyle = active ? '#2f6fb5' : '#dde5ee'
    ctx.lineWidth = 2
    ctx.stroke()
    ctx.fillStyle = active ? '#2f6fb5' : '#93a4b8'
    ctx.beginPath()
    ctx.arc(58, y + 30, 6, 0, Math.PI * 2)
    ctx.fill()
    ctx.fillStyle = '#33415a'
    ctx.font = `600 15px ${SANS}`
    ctx.fillText(label, 76, y + 36)
    ctx.fillStyle = '#dde5ee'
    ctx.fillRect(76, y + 48, (MOCK_VIDEO_WIDTH - 140) * (active ? 1 : 0.3), 6)
  })
}

/** 底部字幕条：极浅底 + 分隔线，正文深色 */
function drawSubtitle(ctx: Ctx, scene: Scene, timeInScene: number): void {
  const color = SPEAKER_COLORS[scene.speaker]
  const barHeight = 168
  const top = MOCK_VIDEO_HEIGHT - barHeight

  ctx.fillStyle = '#f4f7fa'
  ctx.fillRect(0, top, MOCK_VIDEO_WIDTH, barHeight)
  ctx.fillStyle = '#dde5ee'
  ctx.fillRect(0, top, MOCK_VIDEO_WIDTH, 2)

  // 顶部小标签 + 画面进度条
  ctx.fillStyle = '#6b7a8f'
  ctx.font = `500 11px ${SANS}`
  ctx.fillText(scene.label, 24, top + 26)
  ctx.fillStyle = '#dde5ee'
  ctx.fillRect(24, top + 34, MOCK_VIDEO_WIDTH - 48, 2)

  // 只用一个颜色圆点表示谁在说话：**不出「主播A / 主播B」文字标签**，
  // 谁在说话听声音就知道，多一行字只会分散注意力、也占掉字幕空间。
  ctx.fillStyle = color
  ctx.beginPath()
  ctx.arc(30, top + 62, 5, 0, Math.PI * 2)
  ctx.fill()

  // 字幕正文：逐段「打字机」出现，证明画面确实在按时间推进
  ctx.fillStyle = '#16202f'
  ctx.font = `400 15px ${SANS}`
  const shown = truncate(scene.subtitle, Math.max(6, Math.ceil(scene.subtitle.length * Math.min(1, timeInScene * 0.9))))
  const lines = wrapText(ctx, shown, MOCK_VIDEO_WIDTH - 48, 4)
  lines.forEach((line, index) => {
    ctx.fillText(line, 24, top + 104 + index * 24)
  })
}

/** 整帧渲染：画面 + 字幕条 + 顶部「演示模式」水印（避免被当真产物） */
function drawFrame(ctx: Ctx, scenes: Scene[], title: string, elapsed: number, total: number): void {
  const perScene = total / scenes.length
  const index = Math.min(scenes.length - 1, Math.floor(elapsed / perScene))
  const scene = scenes[index] ?? scenes[0]!
  const timeInScene = elapsed - index * perScene
  const progress = Math.min(1, Math.max(0, timeInScene / perScene))

  ctx.save()
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, MOCK_VIDEO_WIDTH, MOCK_VIDEO_HEIGHT)

  if (scene.kind === 'intro') drawIntro(ctx, title)
  else if (scene.kind === 'outro') drawOutro(ctx, title, progress)
  else drawBody(ctx, index % 2 === 0 ? '#2f6f8f' : '#b0762f', progress)

  drawSubtitle(ctx, scene, timeInScene)

  // 右上角水印：明确这是 Mock 占位，不是真实产物
  ctx.fillStyle = 'rgba(8, 13, 20, 0.72)'
  roundRect(ctx, MOCK_VIDEO_WIDTH - 148, 16, 132, 26, 13)
  ctx.fill()
  ctx.fillStyle = '#d3a24a'
  ctx.font = `600 11px ${SANS}`
  ctx.fillText('演示模式 · 占位视频', MOCK_VIDEO_WIDTH - 136, 33)
  ctx.restore()
}

/** 挑一个浏览器支持的编码格式 */
function pickMimeType(Recorder: typeof MediaRecorder): string | null {
  for (const candidate of CANDIDATE_MIME_TYPES) {
    try {
      if (Recorder.isTypeSupported(candidate)) return candidate
    } catch {
      // 某些实现 isTypeSupported 会抛错，忽略继续试
    }
  }
  return null
}

/**
 * 造一条静音音轨。视频解读播客本来就该有音轨（真实产物是 AAC），
 * 缺音轨会让 <video> 的音量控件不可用，演示看起来就不像个视频。
 * 拿不到就算了——只有画面轨的 WebM 一样能播。
 */
function attachSilentAudioTrack(stream: MediaStream): () => void {
  const scope = globalThis as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext }
  const Ctor = scope.AudioContext ?? scope.webkitAudioContext
  if (typeof Ctor !== 'function') return () => {}

  let context: AudioContext | null = null
  try {
    context = new Ctor()
    const destination = context.createMediaStreamDestination()
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    gain.gain.value = 0 // 完全静音，只为占住一条音轨
    oscillator.connect(gain)
    gain.connect(destination)
    oscillator.start()
    for (const track of destination.stream.getAudioTracks()) stream.addTrack(track)
  } catch {
    void context?.close().catch(() => {})
    return () => {}
  }

  return () => {
    void context?.close().catch(() => {})
  }
}

/**
 * 现场录制演示视频。任何环节不被支持都返回 null（调用方降级），不抛异常。
 */
export async function renderMockVideo(
  params: RenderMockVideoParams,
): Promise<RenderMockVideoResult | null> {
  const Recorder = (globalThis as unknown as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder
  if (typeof Recorder !== 'function') return null

  const made = createContext()
  if (!made) return null
  const { canvas, ctx } = made
  if (typeof canvas.captureStream !== 'function') return null

  const mimeType = pickMimeType(Recorder)
  if (!mimeType) return null

  const total = Math.max(2, params.durationSec ?? RECORD_SEC)
  const scenes = buildScenes(params.title, params.segments)

  // 先画第一帧：某些浏览器在 captureStream 时画布还是空白，会录出黑屏
  drawFrame(ctx, scenes, params.title, 0, total)

  let stream: MediaStream
  try {
    stream = canvas.captureStream(FPS)
  } catch {
    return null
  }
  const detachAudio = attachSilentAudioTrack(stream)

  let recorder: MediaRecorder
  try {
    recorder = new Recorder(stream, { mimeType, videoBitsPerSecond: 900_000 })
  } catch {
    detachAudio()
    return null
  }

  const chunks: BlobPart[] = []
  const finished = new Promise<Blob | null>((resolve) => {
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data)
    }
    recorder.onerror = () => resolve(null)
    recorder.onstop = () => resolve(chunks.length ? new Blob(chunks, { type: mimeType }) : null)
  })

  let raf = 0
  const startedAt = performance.now()
  const tick = (): void => {
    const now = performance.now()
    drawFrame(ctx, scenes, params.title, Math.min(total, (now - startedAt) / 1000), total)
    if (now - startedAt < total * 1000) raf = window.requestAnimationFrame(tick)
  }

  try {
    recorder.start(250)
    raf = window.requestAnimationFrame(tick)
    await new Promise<void>((resolve) => window.setTimeout(resolve, total * 1000 + 60))
    if (recorder.state !== 'inactive') recorder.stop()
  } catch {
    if (recorder.state !== 'inactive') {
      try {
        recorder.stop()
      } catch {
        // 已经停了
      }
    }
    window.cancelAnimationFrame(raf)
    detachAudio()
    stream.getTracks().forEach((track) => track.stop())
    return null
  }

  const blob = await finished
  window.cancelAnimationFrame(raf)
  detachAudio()
  stream.getTracks().forEach((track) => track.stop())

  if (!blob || blob.size === 0) return null

  return {
    blob,
    durationSec: Math.round(total * 10) / 10,
    width: MOCK_VIDEO_WIDTH,
    height: MOCK_VIDEO_HEIGHT,
    sceneCount: scenes.length,
    mimeType,
  }
}
