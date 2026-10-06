import { ApiError } from './error'
import { renderMockAudio } from './mockAudio'
import { renderMockVideo } from './mockVideo'
import { buildArtwork } from './mockArt'
import type { MockArtwork } from './mockArt'
import { MOCK_PAPERS } from './mockPapers'
import type { MockPaper, MockScriptSegment } from './mockPapers'
import type {
  Analysis,
  ApiAdapter,
  CreateFileInput,
  CreateTextInput,
  CreateUrlInput,
  Episode,
  EpisodeOptions,
  EpisodeOptionsInput,
  EpisodeStatus,
  EpisodeSummary,
  FigureRotateDirection,
  HealthPayload,
  ListEpisodesParams,
  ListEpisodesResult,
  OptionsPayload,
  PaperMeta,
  Script,
  ScriptSegment,
  SourceType,
  VideoInfo,
} from './types'
import { STAGES } from '../utils/stages'
import { countWords } from '../utils/format'

/**
 * Mock 适配器：纯浏览器离线运行（GitHub Pages 演示用）。
 * 完整复刻 docs/API.md 的接口语义：
 * - POST /api/episodes 之后在内存里推进状态机（每阶段约 700ms）；
 * - 音频用 Web Audio API 现场合成的 WAV Blob URL 代替；
 * - 内置 3 篇真实论文（Attention Is All You Need / ResNet / LoRA）+ 1 个通用模板。
 *
 * 把 URL 或文本里带上 "fail" 字样，可以让任务在「深度解读」阶段模拟失败，
 * 用来演示失败态与「重新生成」流程（点重试后即可正常完成）。
 */

export const mode = 'mock' as const

const STORAGE_KEY = 'paper-podcast:mock:episodes:v1'
/** 每个阶段的模拟耗时（契约 §4 要求约 700ms） */
const STAGE_MS = 700
/** 来源（文件名 / 链接 / 文本）里带上这个标记，可演示失败态与重试流程 */
const FAIL_DEMO = /fail-demo/i

/** Mock 音频片段时长：太长会拖慢浏览器渲染，36 秒足够验证播放器 */
const MOCK_AUDIO_SEC = 36

/**
 * Mock 视频片段时长。演示视频是用 MediaRecorder **实时**录制的，
 * 录多久就要等多久，所以只录 6 秒（画面按片头/正文/片尾切换，够验证结构与互斥播放）。
 */
const MOCK_VIDEO_SEC = 6

/**
 * 重新合成视频的模拟耗时。
 *
 * 真实后端复用画面分配、不调模型，实测约 11 秒。Mock 里有两点不同：
 *   - 画面根本不含真实配图（是 Canvas 画的通用占位场景），没有「按新配图重渲染」这回事，
 *     所以不必真的重录一遍（重录一次要 6 秒实时时间，纯浪费）；
 *   - 演示时要看得清「loading 态」，也不宜太快。
 * 取 3 秒：足够让 loading/禁用态被看见，又不至于让演示卡住。
 * 契约层面的语义完全一致：延迟 → 换掉视频 URL（内容变，URL 就变）→ video.stale 归位 false。
 */
const MOCK_VIDEO_REBUILD_MS = 3000

const DEFAULT_OPTIONS: EpisodeOptions = {
  duration_min: 5,
  level: 'intro',
  voice_a: 'zh_male_dayixiansheng_v2_saturn_bigtts',
  voice_b: 'zh_female_mizaitongxue_v2_saturn_bigtts',
}

const OPTIONS: OptionsPayload = {
  durations: [
    { value: 3, label: '3 分钟' },
    { value: 5, label: '5 分钟' },
    { value: 10, label: '10 分钟' },
  ],
  levels: [
    { value: 'intro', label: '入门' },
    { value: 'advanced', label: '进阶' },
    { value: 'expert', label: '专业' },
  ],
  voices: [
    {
      id: 'zh_male_dayixiansheng_v2_saturn_bigtts',
      label: '大义先生（男声·学术沉稳）',
      gender: 'male',
      pair: 'mizai-dayi',
    },
    {
      id: 'zh_female_mizaitongxue_v2_saturn_bigtts',
      label: '米仔同学（女声·清亮好奇）',
      gender: 'female',
      pair: 'mizai-dayi',
    },
    {
      id: 'zh_male_wennuanahu_v2_saturn_bigtts',
      label: '温暖阿虎（男声·温和解说）',
      gender: 'male',
      pair: 'ahu-wanwan',
    },
    {
      id: 'zh_female_wanwanxiaohe_v2_saturn_bigtts',
      label: '湾湾小何（女声·专业播报）',
      gender: 'female',
      pair: 'ahu-wanwan',
    },
    {
      id: 'zh_male_shaonianzixin_v2_saturn_bigtts',
      label: '少年梓辛（男声·年轻活力）',
      gender: 'male',
      pair: 'zixin-yujie',
    },
    {
      id: 'zh_female_gaolengyujie_v2_saturn_bigtts',
      label: '高冷御姐（女声·冷静克制）',
      gender: 'female',
      pair: 'zixin-yujie',
    },
  ],
}

// ---------------------------------------------------------------------------
// 内存状态
// ---------------------------------------------------------------------------

const episodes: Episode[] = []
const timers = new Map<string, number>()
const audioJobs = new Map<string, Promise<void>>()
const objectUrls = new Map<string, Set<string>>()
/** 需要模拟一次失败的单集（首次推进到「深度解读」时失败） */
const failOnce = new Set<string>()

/**
 * 现场录制的演示视频。录制是真实时间的，所以全站只录一次、所有单集共用同一个 Blob URL。
 * 注意：**不能**登记进 objectUrls（那是按单集回收的）——否则删掉某一集时会把
 * 其他集正在用的 URL 一起 revoke 掉，视频就成了破播放器。
 */
let sharedVideo: VideoInfo | null = null
let sharedVideoUrl: string | null = null
/** 录制出来的原始 Blob：重新合成时用它造一个新的 Blob URL（内容一样，URL 必须变） */
let sharedVideoBlob: Blob | null = null
let videoJob: Promise<VideoInfo | null> | null = null

let loaded = false

function nowIso(): string {
  return new Date().toISOString()
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

function makeId(): string {
  const hex = '0123456789abcdef'
  let id = ''
  do {
    id = ''
    for (let i = 0; i < 8; i += 1) id += hex[Math.floor(Math.random() * hex.length)]
  } while (episodes.some((episode) => episode.id === id))
  return id
}

function findEpisode(id: string): Episode | undefined {
  return episodes.find((episode) => episode.id === id)
}

function requireEpisode(id: string): Episode {
  const episode = findEpisode(id)
  if (!episode) throw new ApiError('单集不存在或已被删除', 404)
  return episode
}

function trackObjectUrl(episodeId: string, url: string): string {
  const set = objectUrls.get(episodeId) ?? new Set<string>()
  set.add(url)
  objectUrls.set(episodeId, set)
  return url
}

function revokeObjectUrls(episodeId: string): void {
  const set = objectUrls.get(episodeId)
  if (!set) return
  set.forEach((url) => URL.revokeObjectURL(url))
  objectUrls.delete(episodeId)
}

// ---------------------------------------------------------------------------
// 内容生成
// ---------------------------------------------------------------------------

function genericPaper(): MockPaper {
  return MOCK_PAPERS[3] ?? MOCK_PAPERS[0]!
}

function paperByArxivId(arxivId: string): MockPaper | undefined {
  return MOCK_PAPERS.find((paper) => paper.meta.arxiv_id === arxivId)
}

/** 从 arXiv 链接里抽 id：/abs/1706.03762v5 → 1706.03762 */
function extractArxivId(url: string): string | null {
  const match = url.match(/(\d{4}\.\d{4,5})(v\d+)?/)
  return match?.[1] ?? null
}

function titleFromFilename(filename: string): string {
  const base = filename.replace(/\.pdf$/i, '').replace(/[_-]+/g, ' ').trim()
  return base ? base.slice(0, 120) : '未命名论文'
}

function titleFromText(text: string): string {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.replace(/^[#>\s\d.、)）]+/, '').trim())
    .filter((line) => line.length >= 4)
  const first = lines[0]
  if (!first) return '粘贴的论文正文'
  const cleaned = first.replace(/[。：:；;，,]$/, '')
  if (cleaned.length <= 48) return cleaned
  // 粘贴的正文常常是一整段：截到第一个句读，做成标题的样子
  const head = cleaned.slice(0, 48)
  const cut = Math.max(head.lastIndexOf('。'), head.lastIndexOf('，'), head.lastIndexOf('；'))
  return `${cut >= 12 ? head.slice(0, cut) : head}…`
}

function titleFromUrl(url: string): string {
  try {
    const parsed = new URL(url)
    const segments = parsed.pathname.split('/').filter(Boolean)
    const last = segments[segments.length - 1]
    if (last && /[a-zA-Z\u4e00-\u9fa5]/.test(last)) {
      return decodeURIComponent(last).replace(/[_-]+/g, ' ').replace(/\.(html?|pdf)$/i, '').slice(0, 120)
    }
    return parsed.hostname
  } catch {
    return url.slice(0, 120)
  }
}

interface ContentPlan {
  meta: PaperMeta
  analysis: Analysis
  segments: MockScriptSegment[]
}

function planContent(paper: MockPaper, title: string): ContentPlan {
  return {
    meta: { ...paper.meta, title: title || paper.meta.title },
    analysis: {
      background: paper.analysis.background,
      innovations: [...paper.analysis.innovations],
      method: paper.analysis.method,
      experiments: paper.analysis.experiments,
      conclusion: paper.analysis.conclusion,
      limitations: [...paper.analysis.limitations],
      value: paper.analysis.value,
      future: [...paper.analysis.future],
    },
    segments: paper.script.map((segment) => ({ ...segment })),
  }
}

function buildScript(plan: ContentPlan, options: EpisodeOptions): Script {
  const segments: ScriptSegment[] = plan.segments.map((segment, index) => ({
    speaker: segment.speaker,
    text: segment.text,
    round: index,
  }))
  return {
    segments,
    word_count: countWords(segments.map((segment) => segment.text).join('')),
    est_duration_sec: options.duration_min * 60,
  }
}

function normalizeOptions(options?: EpisodeOptionsInput): EpisodeOptions {
  return {
    duration_min: options?.duration_min ?? DEFAULT_OPTIONS.duration_min,
    level: options?.level ?? DEFAULT_OPTIONS.level,
    voice_a: options?.voice_a ?? DEFAULT_OPTIONS.voice_a,
    voice_b: options?.voice_b ?? DEFAULT_OPTIONS.voice_b,
  }
}

function createEpisodeRecord(input: {
  title: string
  sourceType: SourceType
  sourceRef: string | null
  options?: EpisodeOptionsInput
  paper?: MockPaper
  createdDaysAgo?: number
  createdAt?: string
  shouldFail?: boolean
}): Episode {
  const options = normalizeOptions(input.options)
  const paper = input.paper ?? genericPaper()
  const plan = planContent(paper, input.title)
  const created =
    input.createdAt ?? new Date(Date.now() - (input.createdDaysAgo ?? 0) * 86400000).toISOString()
  const id = makeId()

  const episode: Episode = {
    id,
    title: input.title || plan.meta.title || '未命名论文',
    source_type: input.sourceType,
    source_ref: input.sourceRef,
    status: 'queued',
    stage_label: '排队中',
    progress: 0,
    error: null,
    options,
    paper_meta: plan.meta,
    analysis: plan.analysis,
    script: buildScript(plan, options),
    // 配图（契约 §1 的新字段）：全部现场生成，见 mockArt.ts
    ...buildArtworkFor(id, input.sourceType, plan.meta),
    // 视频解读（契约 §1 的 video 字段）：等单集完成时现场录制，见 ensureVideo
    video: null,
    audio_url: null,
    audio_duration_sec: null,
    audio_bytes: null,
    created_at: created,
    updated_at: created,
  }
  if (input.shouldFail) failOnce.add(episode.id)
  episodes.push(episode)
  return episode
}

/** 配图依赖 episode id 作为随机种子，因此必须等 id 定下来之后再生成 */
function buildArtworkFor(id: string, sourceType: SourceType, meta: PaperMeta): MockArtwork {
  return buildArtwork({
    seed: id,
    title: meta.title ?? '未命名论文',
    abstract: meta.abstract ?? '',
    sourceType,
    track: (url) => trackObjectUrl(id, url),
  })
}

// ---------------------------------------------------------------------------
// 状态机推进
// ---------------------------------------------------------------------------

const PIPELINE: EpisodeStatus[] = ['parsing', 'analyzing', 'scripting', 'synthesizing', 'completed']

function stageDef(status: EpisodeStatus): { label: string; progress: number } {
  const def = STAGES.find((stage) => stage.status === status)
  return { label: def?.label ?? '处理中', progress: def?.progress ?? 0 }
}

function schedule(id: string, tick: () => void): void {
  const existing = timers.get(id)
  if (existing !== undefined) window.clearTimeout(existing)
  timers.set(
    id,
    window.setTimeout(() => {
      timers.delete(id)
      tick()
    }, STAGE_MS),
  )
}

function clearSchedule(id: string): void {
  const existing = timers.get(id)
  if (existing !== undefined) {
    window.clearTimeout(existing)
    timers.delete(id)
  }
}

/** 推进流水线；firstStage 默认从 parsing 开始，用于刷新页面后从断点续跑 */
function runPipeline(episode: Episode, firstStage: EpisodeStatus = 'parsing'): void {
  let step = Math.max(PIPELINE.indexOf(firstStage), 0)
  const tick = (): void => {
    const current = findEpisode(episode.id)
    if (!current) return
    const next = PIPELINE[step]
    if (!next) return

    if (next === 'analyzing' && failOnce.has(current.id)) {
      // 演示失败态：第一次推进到「深度解读」时失败，重试后不再失败
      failOnce.delete(current.id)
      current.status = 'failed'
      current.stage_label = '失败'
      current.progress = 35
      current.error = '论文解析失败：没能从来源中提取到可读正文（Mock 模式模拟的失败，点「重新生成」即可恢复）'
      current.updated_at = nowIso()
      persist()
      return
    }

    const def = stageDef(next)
    current.status = next
    current.stage_label = def.label
    current.progress = def.progress
    current.error = null
    current.updated_at = nowIso()
    persist()

    if (next === 'completed') {
      void ensureAudio(current)
      return
    }
    step += 1
    schedule(current.id, tick)
  }
  schedule(episode.id, tick)
}

/** 刷新页面后，未完成的任务从当前阶段的下一步继续跑 */
function resumePipeline(episode: Episode): void {
  const currentIndex = PIPELINE.indexOf(episode.status)
  const next = PIPELINE[currentIndex + 1]
  if (!next) return
  runPipeline(episode, next)
}

// ---------------------------------------------------------------------------
// 音频
// ---------------------------------------------------------------------------

function ensureAudio(episode: Episode): Promise<void> {
  if (episode.audio_url) return Promise.resolve()
  const running = audioJobs.get(episode.id)
  if (running) return running
  const job = renderAudioFor(episode).finally(() => {
    audioJobs.delete(episode.id)
  })
  audioJobs.set(episode.id, job)
  return job
}

async function renderAudioFor(episode: Episode): Promise<void> {
  try {
    const speakers = (episode.script?.segments ?? []).map((segment) => segment.speaker)
    const { blob, durationSec } = await renderMockAudio({
      durationSec: MOCK_AUDIO_SEC,
      speakers: speakers.length ? speakers : ['A', 'B'],
    })
    const current = findEpisode(episode.id)
    if (!current) return
    if (current.audio_url) URL.revokeObjectURL(current.audio_url)
    const url = URL.createObjectURL(blob)
    current.audio_url = url
    current.audio_duration_sec = durationSec
    current.audio_bytes = blob.size
    current.updated_at = nowIso()
    trackObjectUrl(current.id, url)
    persist()
  } catch (error) {
    console.warn('[mock] 音频合成失败，播放器将不可用', error)
  }
}

// ---------------------------------------------------------------------------
// 视频（视频解读播客）
// ---------------------------------------------------------------------------

/**
 * 组装一次演示视频的元数据。用第一篇内置论文的脚本做画面素材
 * （字幕条用的是真实脚本句子，看起来才像「视频解读」而不是随便一块黑板）。
 */
async function renderSharedVideo(): Promise<VideoInfo | null> {
  try {
    const paper = MOCK_PAPERS[0] ?? genericPaper()
    const result = await renderMockVideo({
      title: paper.meta.title ?? '示例论文',
      segments: paper.script,
      durationSec: MOCK_VIDEO_SEC,
    })
    if (!result) {
      // 浏览器不支持 canvas.captureStream + MediaRecorder：降级为「没有视频」，
      // 详情页的视频区整块不渲染（不是破播放器，也不报错）
      console.warn('[mock] 当前浏览器不支持现场录制演示视频，视频区将不显示')
      return null
    }
    const url = URL.createObjectURL(result.blob)
    sharedVideoUrl = url
    sharedVideoBlob = result.blob
    sharedVideo = {
      url,
      duration_sec: result.durationSec,
      scene_count: result.sceneCount,
      bytes: result.blob.size,
      // 刚录出来就是「跟当前配图一致」的状态
      stale: false,
    }
    return sharedVideo
  } catch (error) {
    console.warn('[mock] 演示视频生成失败，视频区将不显示', error)
    return null
  }
}

/** 取共享的演示视频（并发调用共用同一个渲染任务） */
function ensureSharedVideo(): Promise<VideoInfo | null> {
  if (sharedVideo) return Promise.resolve(sharedVideo)
  if (!videoJob) {
    videoJob = renderSharedVideo().finally(() => {
      videoJob = null
    })
  }
  return videoJob
}

/** 完成态的单集才有视频；录制中的任务给 null（与真实后端的时序一致） */
async function ensureVideo(episode: Episode): Promise<void> {
  if (episode.status !== 'completed') return
  const info = await ensureSharedVideo()
  const current = findEpisode(episode.id)
  // 已经有视频就别覆盖：那一份可能是「配图改过、画面已过时」的副本（stale=true），
  // 覆盖回共享的 fresh 版本会让提示条凭空消失。刷新页面后 video 本来就是 null，
  // 走不到这条分支，所以「刷新即重置」的行为不变。
  if (info && current && !current.video) current.video = info
}

/**
 * 配图被人工校正过 → 视频里的画面就跟不上了（真实后端同样会置 video.stale=true）。
 *
 * 必须**换成新对象**：所有单集共用同一个 sharedVideo，原地改 stale 会把别的单集一起标脏。
 */
function markVideoStale(episode: Episode): void {
  if (!episode.video) return
  episode.video = { ...episode.video, stale: true }
}

/** 把 Blob URL 的内容取回来（重新合成时用它造一个新 URL；失败返回 null，降级为沿用旧 URL） */
async function blobFromUrl(url: string): Promise<Blob | null> {
  try {
    if (!url.startsWith('blob:')) return null
    const response = await fetch(url)
    if (!response.ok) return null
    const blob = await response.blob()
    return blob.size > 0 ? blob : null
  } catch {
    return null
  }
}

/**
 * 用现有素材重新合成视频（契约 §2：POST /video/rebuild）。
 *
 * Mock 里没有真实的「把配图烘焙进 MP4」这一步（画面是 Canvas 画的占位场景），
 * 所以不重录，只复刻契约语义：等待 → 视频 URL 换新（内容/URL 一起变，缓存失效）
 * → video.stale 归位 false。没有视频时抛 409，与真实后端一致。
 */
export async function rebuildVideo(id: string): Promise<Episode> {
  ensureLoaded()
  await delay(60)
  const episode = requireEpisode(id)
  if (!episode.video) throw new ApiError('这一集还没有视频，无法重新合成', 409)

  await delay(MOCK_VIDEO_REBUILD_MS)

  const blob = sharedVideoBlob ?? (await blobFromUrl(episode.video.url))
  // 拿不到内容（浏览器不支持 fetch blob: 之类）时沿用旧 URL：stale 仍然归位，
  // 只是「URL 变了」这条没兑现 —— 演示里看不到差别，但不该因此报错
  const url = blob ? trackObjectUrl(id, URL.createObjectURL(blob)) : episode.video.url
  episode.video = { ...episode.video, url, stale: false }
  episode.updated_at = nowIso()
  persist()
  return copyEpisode(episode)
}

// ---------------------------------------------------------------------------
// 持久化（只存元数据；音频与配图都在每次加载时重新现场生成，避免顶爆 localStorage 配额）
// ---------------------------------------------------------------------------

function persist(): void {
  try {
    const payload = {
      version: 1,
      episodes: episodes.map((episode) => ({
        ...episode,
        audio_url: null,
        audio_duration_sec: null,
        audio_bytes: null,
        // 配图是 Canvas / SVG 现场生成的（data URL + Blob URL），不落盘，加载时按 id 重算
        cover_url: null,
        cover_width: null,
        cover_height: null,
        figures: [],
        illustration: null,
        // 视频是 MediaRecorder 现场录的 Blob URL，同样不落盘（存进去刷新后就是失效 URL）
        video: null,
      })),
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    // 隐私模式或配额不足：忽略，内存态仍然可用
  }
}

function normalizeStored(raw: unknown): Episode | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Partial<Episode>
  if (typeof value.id !== 'string' || !value.id) return null
  if (typeof value.title !== 'string') return null
  const sourceType = (value.source_type ?? 'text') as SourceType
  const meta = value.paper_meta ?? null
  return {
    id: value.id,
    title: value.title,
    source_type: sourceType,
    source_ref: value.source_ref ?? null,
    status: (value.status ?? 'queued') as EpisodeStatus,
    stage_label: value.stage_label ?? '排队中',
    progress: typeof value.progress === 'number' ? value.progress : 0,
    error: value.error ?? null,
    options: { ...DEFAULT_OPTIONS, ...(value.options ?? {}) },
    paper_meta: meta,
    analysis: value.analysis ?? null,
    script: value.script ?? null,
    // 配图重新现场生成（seed 用 id，所以和刷新前是同一张图）
    ...buildArtworkFor(value.id, sourceType, meta ?? { title: value.title } as PaperMeta),
    // 视频同样是现场生成物，加载后按需重录（这里先置 null，见 ensureLoaded / ensureVideo）
    video: null,
    audio_url: null,
    audio_duration_sec: null,
    audio_bytes: null,
    created_at: value.created_at ?? nowIso(),
    updated_at: value.updated_at ?? nowIso(),
  }
}

function seed(): void {
  const seeds: Array<{
    index: number
    sourceType: SourceType
    sourceRef: string | null
    options: EpisodeOptionsInput
    hoursAgo: number
  }> = [
    {
      index: 0,
      sourceType: 'pdf',
      sourceRef: 'attention-is-all-you-need.pdf',
      options: { duration_min: 5, level: 'intro' },
      hoursAgo: 74,
    },
    {
      index: 1,
      sourceType: 'url',
      sourceRef: 'https://arxiv.org/abs/1512.03385',
      options: { duration_min: 10, level: 'advanced' },
      hoursAgo: 20,
    },
    {
      index: 2,
      sourceType: 'text',
      sourceRef: null,
      options: { duration_min: 3, level: 'expert' },
      hoursAgo: 0.4,
    },
  ]

  seeds.forEach((seedDef) => {
    const paper = MOCK_PAPERS[seedDef.index]
    if (!paper) return
    const created = new Date(Date.now() - seedDef.hoursAgo * 3600000).toISOString()
    const episode = createEpisodeRecord({
      title: paper.meta.title,
      sourceType: seedDef.sourceType,
      sourceRef: seedDef.sourceRef,
      options: seedDef.options,
      paper,
      createdAt: created,
    })
    const def = stageDef('completed')
    episode.status = 'completed'
    episode.stage_label = def.label
    episode.progress = def.progress
    episode.updated_at = created
  })
}

function ensureLoaded(): void {
  if (loaded) return
  loaded = true

  let restored: Episode[] = []
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed: unknown = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && 'episodes' in parsed) {
        const list = (parsed as { episodes?: unknown }).episodes
        if (Array.isArray(list)) {
          restored = list.map(normalizeStored).filter((item): item is Episode => item !== null)
        }
      }
    }
  } catch {
    restored = []
  }

  if (restored.length) {
    episodes.push(...restored)
  } else {
    seed()
  }

  episodes.forEach((episode) => {
    if (episode.status === 'completed') {
      void ensureAudio(episode)
    } else if (episode.status !== 'failed') {
      resumePipeline(episode)
    }
  })

  // 演示视频是实时录制的：这里先在后台开录（不 await），用户点进详情页时通常已经好了。
  // 直接深链到详情页的极端情况由 getEpisode 里的 await 兜住。
  if (episodes.some((episode) => episode.status === 'completed')) void ensureSharedVideo()

  persist()
}

// ---------------------------------------------------------------------------
// 导出物：文本 / Markdown（mock 下用 Blob URL 代替后端下载地址）
// ---------------------------------------------------------------------------

function buildScriptText(episode: Episode): string {
  const segments = episode.script?.segments ?? []
  // 与真实后端 /script.txt 的输出格式保持一致：标题 + 分隔线 + 空行分隔的逐段脚本
  const header = [`《${episode.paper_meta?.title ?? episode.title}》`, '双人播客脚本', '='.repeat(40), '']
  const body = segments.map((segment) => `【主播${segment.speaker}】${segment.text}`)
  return [...header, ...body.flatMap((line) => [line, ''])].join('\n').trimEnd()
}

function buildAnalysisMarkdown(episode: Episode): string {
  const meta = episode.paper_meta
  const analysis = episode.analysis
  const lines: string[] = []

  lines.push(`# ${meta?.title ?? episode.title}`, '')
  if (meta?.authors?.length) lines.push(`**作者**：${meta.authors.join(', ')}`)
  if (meta?.year) lines.push(`**发表**：${meta.venue ?? meta.year}`)
  if (meta?.arxiv_id) {
    lines.push(`**arXiv**：[${meta.arxiv_id}](https://arxiv.org/abs/${meta.arxiv_id})`)
  }
  if (meta?.keywords?.length) lines.push(`**关键词**：${meta.keywords.join(', ')}`)
  lines.push('')

  if (!analysis) {
    lines.push('_解读尚未生成。_')
    return lines.join('\n')
  }

  const section = (title: string, body: string | null | undefined): void => {
    if (!body) return
    lines.push(`## ${title}`, '', body, '')
  }
  const listSection = (title: string, items: string[] | null | undefined): void => {
    if (!items?.length) return
    lines.push(`## ${title}`, '', ...items.map((item) => `- ${item}`), '')
  }

  section('摘要', meta?.abstract)
  section('研究背景', analysis.background)
  listSection('核心创新点', analysis.innovations)
  section('研究方法', analysis.method)
  section('实验结果', analysis.experiments)
  section('核心结论', analysis.conclusion)
  listSection('存在不足', analysis.limitations)
  section('行业应用价值', analysis.value)
  listSection('未来研究方向', analysis.future)

  return lines.join('\n').trimEnd()
}

function textBlobUrl(content: string, type: string): string {
  const blob = new Blob([content], { type })
  return URL.createObjectURL(blob)
}

// ---------------------------------------------------------------------------
// 对外接口（与 real.ts 一一对应）
// ---------------------------------------------------------------------------

export async function getHealth(): Promise<HealthPayload> {
  ensureLoaded()
  await delay(90)
  return { status: 'ok', version: '0.1.0-mock', modes: { llm: 'mock', tts: 'mock' } }
}

export async function getOptions(): Promise<OptionsPayload> {
  await delay(60)
  return OPTIONS
}

export async function createEpisodeFromFile(input: CreateFileInput): Promise<Episode> {
  ensureLoaded()
  await delay(240)
  if (!input.file) throw new ApiError('请选择要上传的 PDF 文件', 400)
  if (!/\.pdf$/i.test(input.file.name)) throw new ApiError('仅支持 .pdf 文件', 400)
  if (input.file.size > 40 * 1024 * 1024) throw new ApiError('文件过大（Mock 模式上限 40MB）', 400)

  const title = titleFromFilename(input.file.name)
  const episode = createEpisodeRecord({
    title,
    sourceType: 'pdf',
    sourceRef: input.file.name,
    options: input.options,
    shouldFail: FAIL_DEMO.test(input.file.name),
  })
  runPipeline(episode)
  persist()
  return { ...episode }
}

export async function createEpisodeFromUrl(input: CreateUrlInput): Promise<Episode> {
  ensureLoaded()
  await delay(240)
  const url = input.url.trim()
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    throw new ApiError('链接格式不正确，请填写完整的 http(s) 地址', 400)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ApiError('仅支持 http / https 链接', 400)
  }

  const arxivId = extractArxivId(url)
  const known = arxivId ? paperByArxivId(arxivId) : undefined
  const title = known?.meta.title ?? titleFromUrl(url)
  const episode = createEpisodeRecord({
    title,
    sourceType: 'url',
    sourceRef: url,
    options: input.options,
    paper: known,
    shouldFail: FAIL_DEMO.test(url),
  })
  runPipeline(episode)
  persist()
  return { ...episode }
}

export async function createEpisodeFromText(input: CreateTextInput): Promise<Episode> {
  ensureLoaded()
  await delay(240)
  const text = input.text.trim()
  if (text.length < 20) throw new ApiError('请粘贴至少 20 个字的论文正文', 400)

  const episode = createEpisodeRecord({
    title: titleFromText(text),
    sourceType: 'text',
    sourceRef: null,
    options: input.options,
    shouldFail: FAIL_DEMO.test(text.slice(0, 400)),
  })
  runPipeline(episode)
  persist()
  return { ...episode }
}

/**
 * 契约 §1：列表项省略 analysis / script（置 null）、figures（置 []）、illustration / video（置 null），
 * 但保留 cover_url 一族，因为列表卡片要显示封面缩略图。
 * 这里显式挑字段而不是「spread 再删」，这样一旦 Episode 新增字段，类型检查会提醒我们同步。
 */
function toSummary(episode: Episode): EpisodeSummary {
  return {
    id: episode.id,
    title: episode.title,
    source_type: episode.source_type,
    source_ref: episode.source_ref,
    status: episode.status,
    stage_label: episode.stage_label,
    progress: episode.progress,
    error: episode.error,
    options: episode.options,
    paper_meta: episode.paper_meta,
    cover_url: episode.cover_url,
    cover_width: episode.cover_width,
    cover_height: episode.cover_height,
    audio_url: episode.audio_url,
    audio_duration_sec: episode.audio_duration_sec,
    audio_bytes: episode.audio_bytes,
    created_at: episode.created_at,
    updated_at: episode.updated_at,
  }
}

export async function listEpisodes(params: ListEpisodesParams = {}): Promise<ListEpisodesResult> {
  ensureLoaded()
  await delay(200)

  const limit = Math.min(Math.max(params.limit ?? 20, 1), 100)
  const offset = Math.max(params.offset ?? 0, 0)
  const keyword = params.q?.trim().toLowerCase() ?? ''

  let items = episodes.slice()
  if (params.status) items = items.filter((episode) => episode.status === params.status)
  if (keyword) items = items.filter((episode) => episode.title.toLowerCase().includes(keyword))
  items.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))

  const total = items.length
  const page = items.slice(offset, offset + limit).map(toSummary)
  return { items: page, total }
}

export async function getEpisode(id: string): Promise<Episode> {
  ensureLoaded()
  await delay(140)
  const episode = requireEpisode(id)
  if (episode.status === 'completed' && !episode.audio_url) {
    await ensureAudio(episode)
  }
  // 与真实后端一致：详情接口才带 video（列表接口不带）
  await ensureVideo(episode)
  return { ...episode }
}

export async function deleteEpisode(id: string): Promise<void> {
  ensureLoaded()
  await delay(160)
  const index = episodes.findIndex((episode) => episode.id === id)
  if (index < 0) throw new ApiError('单集不存在或已被删除', 404)
  clearSchedule(id)
  failOnce.delete(id)
  const [removed] = episodes.splice(index, 1)
  if (removed?.audio_url) URL.revokeObjectURL(removed.audio_url)
  revokeObjectUrls(id)
  persist()
}

export async function retryEpisode(id: string): Promise<Episode> {
  ensureLoaded()
  await delay(180)
  const episode = requireEpisode(id)
  if (episode.status !== 'failed') throw new ApiError('仅失败的任务可以重新生成', 409)

  failOnce.delete(id)
  clearSchedule(id)
  episode.status = 'queued'
  episode.stage_label = '排队中'
  episode.progress = 0
  episode.error = null
  episode.updated_at = nowIso()
  persist()
  runPipeline(episode)
  return { ...episode }
}

// ---------------------------------------------------------------------------
// 人工校正配图（契约 §2：rotate / delete）
// ---------------------------------------------------------------------------

/**
 * 把配图真正旋转 90°（Canvas 重画），返回**内容已变**的新 URL。
 *
 * Mock 里的配图是 Canvas 现场画出来的 data URL，所以这里不能只换个 query 参数糊弄，
 * 而是真的把像素转过去：新的 data URL 内容不同，URL 自然就变了，
 * 与真实后端「转完 URL 上 `?v=` 版本号变化、浏览器重新取图」的效果一致。
 * 环境没有 Canvas / 图读不出来时退回原 URL（此时仍会对调 width/height）。
 */
function rotateDataUrl(url: string, direction: FigureRotateDirection): Promise<string> {
  return new Promise((resolve) => {
    if (!url || typeof document === 'undefined' || typeof Image === 'undefined') {
      resolve(url)
      return
    }
    const image = new Image()
    image.onload = () => {
      try {
        const width = image.naturalWidth
        const height = image.naturalHeight
        if (!width || !height) {
          resolve(url)
          return
        }
        const canvas = document.createElement('canvas')
        // 转 90°：画布宽高互换
        canvas.width = height
        canvas.height = width
        const ctx = canvas.getContext('2d')
        if (!ctx) {
          resolve(url)
          return
        }
        ctx.translate(canvas.width / 2, canvas.height / 2)
        ctx.rotate(direction === 'cw' ? Math.PI / 2 : -Math.PI / 2)
        ctx.drawImage(image, -width / 2, -height / 2)
        resolve(canvas.toDataURL('image/png'))
      } catch {
        resolve(url)
      }
    }
    image.onerror = () => resolve(url)
    image.src = url
  })
}

/**
 * 复制一份 Episode，避免调用方拿到的 figures / video 与内存态共用同一个对象。
 * video 尤其重要：它是全站共用的共享对象，直接漏出去会让外部误改到别的单集。
 */
function copyEpisode(episode: Episode): Episode {
  return {
    ...episode,
    figures: episode.figures.map((figure) => ({ ...figure })),
    video: episode.video ? { ...episode.video } : null,
  }
}

export async function rotateFigure(
  id: string,
  figureId: string,
  direction: FigureRotateDirection,
): Promise<Episode> {
  ensureLoaded()
  await delay(200)
  const episode = requireEpisode(id)
  const figure = episode.figures.find((item) => item.id === figureId)
  if (!figure) throw new ApiError('配图不存在', 404)

  const nextUrl = await rotateDataUrl(figure.url, direction)
  figure.url = nextUrl
  // 宽高对调（新图的内容已经转过来了，字段必须跟着变，否则前端按旧比例留位会错）
  const width = figure.width
  figure.width = figure.height
  figure.height = width
  // 配图变了，已生成的视频里还是旧画面（真实后端同样会置 video.stale=true）
  markVideoStale(episode)
  episode.updated_at = nowIso()
  persist()
  return copyEpisode(episode)
}

export async function deleteFigure(id: string, figureId: string): Promise<Episode> {
  ensureLoaded()
  await delay(160)
  const episode = requireEpisode(id)
  const index = episode.figures.findIndex((item) => item.id === figureId)
  if (index < 0) throw new ApiError('配图不存在', 404)

  // 配图是每次加载重算的 data URL，没有 Blob URL 需要回收，内存里移除即可
  episode.figures.splice(index, 1)
  markVideoStale(episode)
  episode.updated_at = nowIso()
  persist()
  return copyEpisode(episode)
}

export function scriptTxtUrl(id: string): string {
  ensureLoaded()
  const episode = findEpisode(id)
  if (!episode?.script) return ''
  return trackObjectUrl(id, textBlobUrl(buildScriptText(episode), 'text/plain;charset=utf-8'))
}

export function analysisMdUrl(id: string): string {
  ensureLoaded()
  const episode = findEpisode(id)
  if (!episode?.analysis) return ''
  return trackObjectUrl(id, textBlobUrl(buildAnalysisMarkdown(episode), 'text/markdown;charset=utf-8'))
}

/** 仅供调试：清空 mock 数据（localStorage + 内存） */
export function resetMockStore(): void {
  episodes.length = 0
  timers.forEach((timer) => window.clearTimeout(timer))
  timers.clear()
  audioJobs.clear()
  videoJob = null
  sharedVideo = null
  sharedVideoBlob = null
  if (sharedVideoUrl) {
    URL.revokeObjectURL(sharedVideoUrl)
    sharedVideoUrl = null
  }
  failOnce.clear()
  objectUrls.forEach((set) => set.forEach((url) => URL.revokeObjectURL(url)))
  objectUrls.clear()
  loaded = false
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // 忽略
  }
  ensureLoaded()
}

const adapter: ApiAdapter = {
  mode,
  getHealth,
  getOptions,
  createEpisodeFromFile,
  createEpisodeFromUrl,
  createEpisodeFromText,
  listEpisodes,
  getEpisode,
  deleteEpisode,
  retryEpisode,
  rotateFigure,
  deleteFigure,
  rebuildVideo,
  scriptTxtUrl,
  analysisMdUrl,
}

export default adapter
