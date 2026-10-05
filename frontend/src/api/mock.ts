import { ApiError } from './error'
import { renderMockAudio } from './mockAudio'
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
  HealthPayload,
  ListEpisodesParams,
  ListEpisodesResult,
  OptionsPayload,
  PaperMeta,
  Script,
  ScriptSegment,
  SourceType,
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

  const episode: Episode = {
    id: makeId(),
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
// 持久化（只存元数据，音频在每次加载时重新合成）
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
  return {
    id: value.id,
    title: value.title,
    source_type: (value.source_type ?? 'text') as SourceType,
    source_ref: value.source_ref ?? null,
    status: (value.status ?? 'queued') as EpisodeStatus,
    stage_label: value.stage_label ?? '排队中',
    progress: typeof value.progress === 'number' ? value.progress : 0,
    error: value.error ?? null,
    options: { ...DEFAULT_OPTIONS, ...(value.options ?? {}) },
    paper_meta: value.paper_meta ?? null,
    analysis: value.analysis ?? null,
    script: value.script ?? null,
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
  persist()
}

// ---------------------------------------------------------------------------
// 导出物：文本 / Markdown（mock 下用 Blob URL 代替后端下载地址）
// ---------------------------------------------------------------------------

function buildScriptText(episode: Episode): string {
  const segments = episode.script?.segments ?? []
  return segments
    .map((segment) => `【主播${segment.speaker}】${segment.text}`)
    .join('\n')
}

function buildAnalysisMarkdown(episode: Episode): string {
  const meta = episode.paper_meta
  const analysis = episode.analysis
  const lines: string[] = []

  lines.push(`# ${meta?.title ?? episode.title}`, '')
  const metaBits: string[] = []
  if (meta?.authors?.length) metaBits.push(`**作者**：${meta.authors.join('、')}`)
  if (meta?.year) metaBits.push(`**年份**：${meta.year}`)
  if (meta?.venue) metaBits.push(`**会议/期刊**：${meta.venue}`)
  if (meta?.arxiv_id) metaBits.push(`**arXiv**：[${meta.arxiv_id}](https://arxiv.org/abs/${meta.arxiv_id})`)
  if (meta?.keywords?.length) metaBits.push(`**关键词**：${meta.keywords.join('、')}`)
  if (metaBits.length) {
    lines.push(...metaBits.map((bit) => `- ${bit}`), '')
  }
  if (meta?.abstract) lines.push(`> ${meta.abstract}`, '')

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

  section('研究背景', analysis.background)
  listSection('创新点', analysis.innovations)
  section('研究方法', analysis.method)
  section('实验结果', analysis.experiments)
  section('核心结论', analysis.conclusion)
  listSection('存在不足', analysis.limitations)
  section('行业价值', analysis.value)
  listSection('未来方向', analysis.future)

  lines.push('---', '', `由「论文解读 AI 播客」生成 · 单集 ${episode.id}`)
  return lines.join('\n')
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
  const page = items.slice(offset, offset + limit).map((episode) => ({
    // 契约 §1：列表接口省略 analysis / script
    ...episode,
    analysis: null,
    script: null,
  }))
  return { items: page, total }
}

export async function getEpisode(id: string): Promise<Episode> {
  ensureLoaded()
  await delay(140)
  const episode = requireEpisode(id)
  if (episode.status === 'completed' && !episode.audio_url) {
    await ensureAudio(episode)
  }
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
  scriptTxtUrl,
  analysisMdUrl,
}

export default adapter
