import { ApiError } from './error'
import { renderMockAudio } from './mockAudio'
import { renderMockVideo } from './mockVideo'
import { buildArtwork } from './mockArt'
import type { MockArtwork } from './mockArt'
import { MOCK_PAPERS } from './mockPapers'
import type { MockPaper, MockScriptSegment } from './mockPapers'
import { englishContentFor } from './mockPapersEn'
import {
  DEFAULT_LANGUAGES,
  sanitizeLanguages,
} from '../utils/language'
import type {
  Analysis,
  ApiAdapter,
  CreateFileInput,
  CreateTextInput,
  CreateUrlInput,
  Episode,
  EpisodeLanguage,
  EpisodeOptions,
  EpisodeOptionsInput,
  EpisodeStatus,
  EpisodeSummary,
  EpisodeVersion,
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
import { isEpisodeLanguage } from './types'
import { STAGES } from '../utils/stages'
import { countWords } from '../utils/format'

/**
 * Mock 适配器：纯浏览器离线运行（GitHub Pages 演示用）。
 * 完整复刻 docs/API.md 的接口语义：
 * - POST /api/episodes 之后在内存里推进状态机（每阶段约 700ms）；
 * - 音频用 Web Audio API 现场合成的 WAV Blob URL 代替；
 * - 内置 3 篇真实论文（Attention Is All You Need / ResNet / LoRA）+ 1 个通用模板。
 *
 * 双语（契约 §1「双语版本（bilingual）」）：同一集的 `versions.zh` 与 `versions.en`
 * 各有**真的两套**脚本与解读（英文版素材在 `mockPapersEn.ts`，不是同一份文本换个 key），
 * 配图则两版共用（都在顶层，见 mockArt.ts）。音频/视频是同一个 Blob 的两个独立 URL ——
 * 反正 Mock 里它们只是占位（正弦波 / Canvas 录制），但 URL 必须能区分，
 * 否则「切语言换地址」这件事在演示里根本看不出来。
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
 * 现场录制的演示视频。录制是真实时间的，所以全站只录一次、所有单集共用同一份 Blob。
 *
 * 双语：中英两版**共用这一份录制结果**（Mock 里画面是 Canvas 现画的占位场景，
 * 没有「按语言重新渲染画面」这回事），但每语言各持一个独立 URL —— 契约里
 * `versions[lang].video.url` 是不同地址，切语言就该换地址。
 *
 * 注意：这些 URL **不能**登记进 objectUrls（那是按单集回收的）——否则删掉某一集时
 * 会把其他集正在用的 URL 一起 revoke 掉，视频就成了破播放器。
 */
let sharedVideo: VideoInfo | null = null
let sharedVideoUrl: string | null = null
/** 录制出来的原始 Blob：重新合成时用它造一个新的 Blob URL（内容一样，URL 必须变） */
let sharedVideoBlob: Blob | null = null
let videoJob: Promise<VideoInfo | null> | null = null

/** 每个语言一个 URL（内容同一份 Blob） */
const sharedVideoUrls = new Map<EpisodeLanguage, string>()

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

/** 论文元信息：契约 §1 里两版**通常一致**（标题作者本身就是英文），所以两种语言共用同一个对象 */
function metaFor(paper: MockPaper, title: string): PaperMeta {
  return { ...paper.meta, title: title || paper.meta.title }
}

function cloneAnalysis(analysis: MockPaper['analysis']): Analysis {
  return {
    background: analysis.background,
    innovations: [...analysis.innovations],
    method: analysis.method,
    experiments: analysis.experiments,
    conclusion: analysis.conclusion,
    limitations: [...analysis.limitations],
    value: analysis.value,
    future: [...analysis.future],
  }
}

/**
 * 某个语言版本的正文素材：中文来自 `mockPapers.ts`，英文来自 `mockPapersEn.ts`。
 *
 * 元信息（契约 §1 的 `paper_meta`）两版**共用标题 / 作者 / 年份 / 会议 / arXiv 编号**
 * —— 那些本来就是论文自己的英文信息；但摘要与关键词是分语言的：
 * 中文版给中文摘要，英文版给英文摘要，否则「English 版」里会摆着一段中文。
 */
function planContent(paper: MockPaper, title: string, language: EpisodeLanguage): ContentPlan {
  const base = metaFor(paper, title)
  if (language === 'en') {
    const english = englishContentFor(paper)
    return {
      meta: { ...base, abstract: english.meta.abstract, keywords: [...english.meta.keywords] },
      analysis: cloneAnalysis(english.analysis),
      segments: english.script.map((segment) => ({ ...segment })),
    }
  }
  return {
    meta: base,
    analysis: cloneAnalysis(paper.analysis),
    segments: paper.script.map((segment) => ({ ...segment })),
  }
}

function buildScript(plan: ContentPlan, language: EpisodeLanguage): Script {
  const segments: ScriptSegment[] = plan.segments.map((segment, index) => ({
    speaker: segment.speaker,
    text: segment.text,
    round: index,
  }))
  const wordCount = countWords(segments.map((segment) => segment.text).join(''))
  // 时长按后端同一套语速常量估算（中文 350 字/分、英文 141 词/分）。
  // 别再写死 duration_min * 60 —— 那会让英文版显示「1173 词 / 预估 05:00」，
  // 一个自相矛盾的数字就这么摆在公开演示页上。
  const rate = language === 'en' ? 141 : 350
  return {
    segments,
    word_count: wordCount,
    est_duration_sec: Math.max(Math.round((wordCount / rate) * 60), 1),
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

/** 新建时要求的语言版本（契约 §1：`options.languages` 默认 `["zh"]`；一个都没有时同样退回默认） */
function requestedLanguages(options?: EpisodeOptionsInput): EpisodeLanguage[] {
  const list = sanitizeLanguages(options?.languages)
  return list.length ? list : [...DEFAULT_LANGUAGES]
}

/** 组装一个语言版本（音频/视频是异步产物，先留空，由 ensureAudio / ensureVideo 填） */
function buildVersion(language: EpisodeLanguage, plan: ContentPlan): EpisodeVersion {
  return {
    language,
    script: buildScript(plan, language),
    analysis: plan.analysis,
    paper_meta: plan.meta,
    audio_url: null,
    audio_duration_sec: null,
    audio_bytes: null,
    video: null,
  }
}

/** 主语言（契约 §1：顶层字段 mirror 的那一版） */
function primaryLanguage(episode: Episode): EpisodeLanguage {
  if (isEpisodeLanguage(episode.language)) return episode.language
  const first = episode.languages?.[0]
  if (isEpisodeLanguage(first)) return first
  return DEFAULT_LANGUAGES[0] ?? 'zh'
}

/**
 * 本集**真的有内容**的语言版本，顺序 = `languages` 的顺序（契约：生成顺序）。
 *
 * 只认 `versions` 里存在的语言：`languages` 说有的语言但 `versions` 里没有，
 * 切过去只会看到主语言的内容，那种「假切换」不如不提供。
 */
function episodeLanguages(episode: Episode): EpisodeLanguage[] {
  const versions = episode.versions
  if (!versions) return []
  const ordered = sanitizeLanguages(episode.languages).filter((language) => versions[language])
  // languages 缺失/脏掉的极端情况：按 versions 的 key 补齐，别让版本白白藏着
  ;(Object.keys(versions) as EpisodeLanguage[]).forEach((language) => {
    if (versions[language] && !ordered.includes(language)) ordered.push(language)
  })
  return ordered
}

/** 取某个语言版本；`lang` 缺失或这一集没有该版本时返回 null（调用方退回顶层字段） */
function versionFor(episode: Episode, lang?: EpisodeLanguage | null): EpisodeVersion | null {
  if (!lang || !episode.versions) return null
  return episode.versions[lang] ?? null
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
  const created =
    input.createdAt ?? new Date(Date.now() - (input.createdDaysAgo ?? 0) * 86400000).toISOString()
  const id = makeId()
  const title = input.title || metaFor(paper, input.title).title || '未命名论文'

  // 只生成被要求的语言版本（没要求英文就不做英文素材，跟真实后端一致）
  const languages = requestedLanguages(input.options)
  const versions: Partial<Record<EpisodeLanguage, EpisodeVersion>> = {}
  languages.forEach((language) => {
    versions[language] = buildVersion(language, planContent(paper, title, language))
  })

  const primary = languages[0] ?? DEFAULT_LANGUAGES[0] ?? 'zh'
  const primaryVersion = versions[primary] ?? buildVersion(primary, planContent(paper, title, primary))
  versions[primary] = primaryVersion

  const episode: Episode = {
    id,
    title,
    source_type: input.sourceType,
    source_ref: input.sourceRef,
    status: 'queued',
    stage_label: '排队中',
    progress: 0,
    error: null,
    options,
    // 顶层字段镜像主语言那一版（契约 §1）：老代码只读顶层也照样显示主语言
    paper_meta: primaryVersion.paper_meta,
    analysis: primaryVersion.analysis,
    script: primaryVersion.script,
    language: primary,
    languages,
    versions,
    // 配图（契约 §1 的新字段）：全部现场生成，见 mockArt.ts
    ...buildArtworkFor(id, input.sourceType, primaryVersion.paper_meta ?? metaFor(paper, title)),
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

/**
 * 顶层音频字段镜像主语言那一版（契约 §1）。
 * 只在主语言的版本确实有音频时才覆盖，避免把已经填好的顶层值抹成 null。
 */
function mirrorPrimaryAudio(episode: Episode): void {
  const version = versionFor(episode, primaryLanguage(episode))
  if (!version) return
  episode.audio_url = version.audio_url
  episode.audio_duration_sec = version.audio_duration_sec
  episode.audio_bytes = version.audio_bytes
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
    // 中英两版共用同一份 Blob（省一次实时合成 —— Mock 里本来就只是一段正弦波），
    // 但**URL 必须各自独立**：契约里 versions[lang].audio_url 是两个不同的资源地址，
    // 共用同一个 URL 的话「切语言换音源」这件事在演示里根本看不出来。
    const targets = episodeLanguages(current)
    targets.forEach((language) => {
      const version = versionFor(current, language)
      if (!version) return
      if (version.audio_url) URL.revokeObjectURL(version.audio_url)
      version.audio_url = trackObjectUrl(current.id, URL.createObjectURL(blob))
      version.audio_duration_sec = durationSec
      version.audio_bytes = blob.size
    })
    if (targets.length) {
      mirrorPrimaryAudio(current)
    } else {
      // 老数据（没有 versions）：只有顶层字段可写
      if (current.audio_url) URL.revokeObjectURL(current.audio_url)
      current.audio_url = trackObjectUrl(current.id, URL.createObjectURL(blob))
      current.audio_duration_sec = durationSec
      current.audio_bytes = blob.size
    }
    current.updated_at = nowIso()
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
    revokeSharedVideoUrls()
    sharedVideoBlob = result.blob
    sharedVideoUrl = URL.createObjectURL(result.blob)
    sharedVideo = {
      url: sharedVideoUrl,
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

function revokeSharedVideoUrls(): void {
  sharedVideoUrls.forEach((url) => URL.revokeObjectURL(url))
  sharedVideoUrls.clear()
  if (sharedVideoUrl) {
    URL.revokeObjectURL(sharedVideoUrl)
    sharedVideoUrl = null
  }
}

/**
 * 某个语言版本的视频地址：内容共用同一份 Blob，URL 按语言各一个。
 *
 * 返回的对象每次都是新的（避免多集之间共用同一个可变对象：某一集被标成
 * `stale=true` 时不能连带把别的集一起标脏）。
 */
function videoInfoFor(language: EpisodeLanguage): VideoInfo | null {
  if (!sharedVideo) return null
  let url = sharedVideoUrls.get(language)
  if (!url) {
    if (!sharedVideoBlob) return { ...sharedVideo }
    url = URL.createObjectURL(sharedVideoBlob)
    sharedVideoUrls.set(language, url)
  }
  return { ...sharedVideo, url }
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

/**
 * 完成态的单集才有视频；录制中的任务给 null（与真实后端的时序一致）。
 *
 * 每个语言版本各填一份（URL 不同），顶层字段镜像主语言那一版。
 */
async function ensureVideo(episode: Episode): Promise<void> {
  if (episode.status !== 'completed') return
  const languages = episodeLanguages(episode)
  const missing = languages.filter((language) => !versionFor(episode, language)?.video)
  // 老数据（没有 versions）只有顶层字段可写；version 化的数据缺哪个语言就补哪个
  if (!missing.length && episode.video) return

  await ensureSharedVideo()
  const current = findEpisode(episode.id)
  if (!current) return

  episodeLanguages(current).forEach((language) => {
    const version = versionFor(current, language)
    if (!version || version.video) return
    const info = videoInfoFor(language)
    if (info) version.video = info
  })

  // 顶层镜像主语言。**已经有视频就别覆盖**：那一份可能是「配图改过、画面已过时」的副本
  // （stale=true），覆盖回 fresh 版本会让提示条凭空消失。
  if (!current.video) {
    const info = videoInfoFor(primaryLanguage(current))
    if (info) current.video = info
  }
}

/**
 * 配图被人工校正过 → 视频里的画面就跟不上了（真实后端同样会置 video.stale=true）。
 *
 * 必须**换成新对象**：所有单集共用同一个 sharedVideo，原地改 stale 会把别的单集一起标脏。
 * 有语言版本时每个版本各标一份（每个版本的 video 本来就是独立对象），
 * 顶层字段（= 主语言那一版）同样标上。
 */
function markVideoStale(episode: Episode): void {
  if (episode.versions) {
    episodeLanguages(episode).forEach((language) => {
      const version = versionFor(episode, language)
      if (version?.video) version.video = { ...version.video, stale: true }
    })
  }
  if (episode.video) episode.video = { ...episode.video, stale: true }
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
 * 用现有素材重新合成视频（契约 §2：POST /video/rebuild?lang=xx）。
 *
 * Mock 里没有真实的「把配图烘焙进 MP4」这一步（画面是 Canvas 画的占位场景），
 * 所以不重录，只复刻契约语义：等待 → 该语言版本的视频 URL 换新（内容/URL 一起变，缓存失效）
 * → video.stale 归位 false。没有视频时抛 409，语言版本不存在时抛 404，与真实后端一致。
 */
export async function rebuildVideo(id: string, lang?: EpisodeLanguage): Promise<Episode> {
  ensureLoaded()
  await delay(60)
  const episode = requireEpisode(id)

  // 契约 §2：`lang` 传了不存在的语言 → 404。省略时重新合成主语言那一版。
  if (lang && !versionFor(episode, lang)) {
    throw new ApiError(`这一集没有 ${lang} 语言版本，无法重新合成`, 404)
  }
  const target = lang ?? primaryLanguage(episode)
  const version = versionFor(episode, target)
  const currentVideo = version ? version.video : episode.video
  if (!currentVideo) throw new ApiError('这一集还没有视频，无法重新合成', 409)

  await delay(MOCK_VIDEO_REBUILD_MS)

  const blob = sharedVideoBlob ?? (await blobFromUrl(currentVideo.url))
  // 拿不到内容（浏览器不支持 fetch blob: 之类）时沿用旧 URL：stale 仍然归位，
  // 只是「URL 变了」这条没兑现 —— 演示里看不到差别，但不该因此报错
  const url = blob ? trackObjectUrl(id, URL.createObjectURL(blob)) : currentVideo.url
  const next: VideoInfo = { ...currentVideo, url, stale: false }
  if (version) version.video = next
  else episode.video = next
  // 顶层字段始终镜像主语言那一版（重新合成的正是主语言时，两边要同步）
  if (target === primaryLanguage(episode)) episode.video = { ...next }
  episode.updated_at = nowIso()
  persist()
  return copyEpisode(episode)
}

// ---------------------------------------------------------------------------
// 持久化（只存元数据；音频与配图都在每次加载时重新现场生成，避免顶爆 localStorage 配额）
// ---------------------------------------------------------------------------

/**
 * 落盘前把「现场生成物」清掉：音频与视频都是 Blob URL，存进去刷新后就是失效地址。
 * 配图同样是现场生成的（Canvas / SVG），所以连 cover / figures / illustration 一起置空。
 * 语言版本里的 script / analysis / paper_meta 是**要留**的（那是真正的内容）。
 */
function stripVersionsForStorage(versions: Episode['versions']): Episode['versions'] {
  if (!versions) return null
  const result: Partial<Record<EpisodeLanguage, EpisodeVersion>> = {}
  Object.entries(versions).forEach(([key, version]) => {
    if (!version || !isEpisodeLanguage(key)) return
    result[key] = {
      ...version,
      audio_url: null,
      audio_duration_sec: null,
      audio_bytes: null,
      video: null,
    }
  })
  return Object.keys(result).length ? result : null
}

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
        versions: stripVersionsForStorage(episode.versions),
      })),
    }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    // 隐私模式或配额不足：忽略，内存态仍然可用
  }
}

/** 把落盘的语言版本恢复成完整结构（音频/视频等现场生成物留空，加载后按需重算） */
function normalizeStoredVersions(raw: unknown): Episode['versions'] {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const result: Partial<Record<EpisodeLanguage, EpisodeVersion>> = {}
  Object.entries(raw as Record<string, unknown>).forEach(([key, value]) => {
    if (!isEpisodeLanguage(key) || !value || typeof value !== 'object') return
    const version = value as Partial<EpisodeVersion>
    result[key] = {
      language: key,
      script: version.script ?? null,
      analysis: version.analysis ?? null,
      paper_meta: version.paper_meta ?? null,
      audio_url: null,
      audio_duration_sec: null,
      audio_bytes: null,
      video: null,
    }
  })
  return Object.keys(result).length ? result : null
}

function normalizeStored(raw: unknown): Episode | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Partial<Episode>
  if (typeof value.id !== 'string' || !value.id) return null
  if (typeof value.title !== 'string') return null
  const sourceType = (value.source_type ?? 'text') as SourceType
  const versions = normalizeStoredVersions(value.versions)
  /**
   * 双语功能上线前存下的记录没有 versions / languages：这时如实保留「没有语言版本」，
   * 详情页会走降级路径（只有顶层字段、不显示切换器）——和真实后端的老数据完全一致。
   */
  const storedLanguages = sanitizeLanguages(value.languages)
  const languages = storedLanguages.length
    ? storedLanguages
    : versions
      ? (Object.keys(versions) as EpisodeLanguage[])
      : [...DEFAULT_LANGUAGES]
  const language = isEpisodeLanguage(value.language)
    ? value.language
    : versions
      ? languages[0]
      : undefined
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
    language: language ?? undefined,
    languages,
    versions,
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
  /**
   * 内置示例刻意覆盖两种形态：
   *   - 前两篇是**双语**（`languages: ['zh','en']`）→ 详情页会出现语言切换器；
   *   - 第三篇只有中文（`languages: ['zh']`）→ 不显示切换器，用来演示「只有一种语言」的降级。
   */
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
      options: { duration_min: 5, level: 'intro', languages: ['zh', 'en'] },
      hoursAgo: 74,
    },
    {
      index: 1,
      sourceType: 'url',
      sourceRef: 'https://arxiv.org/abs/1512.03385',
      options: { duration_min: 10, level: 'advanced', languages: ['zh', 'en'] },
      hoursAgo: 20,
    },
    {
      index: 2,
      sourceType: 'text',
      sourceRef: null,
      options: { duration_min: 3, level: 'expert', languages: ['zh'] },
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

/**
 * 取某语言的下载内容：`lang` 指向的版本存在就用它，否则退回顶层字段（= 主语言）。
 * 与契约 §2「`?lang=` 省略时取主语言」的行为一致。
 */
function downloadContentFor(
  episode: Episode,
  lang?: EpisodeLanguage,
): { meta: PaperMeta | null; script: Script | null; analysis: Analysis | null } {
  const version = versionFor(episode, lang)
  return {
    meta: version?.paper_meta ?? episode.paper_meta,
    script: version?.script ?? episode.script,
    analysis: version?.analysis ?? episode.analysis,
  }
}

function buildScriptText(episode: Episode, meta: PaperMeta | null, script: Script | null): string {
  const segments = script?.segments ?? []
  // 与真实后端 /script.txt 的输出格式保持一致：标题 + 分隔线 + 空行分隔的逐段脚本
  const header = [`《${meta?.title ?? episode.title}》`, '双人播客脚本', '='.repeat(40), '']
  const body = segments.map((segment) => `【主播${segment.speaker}】${segment.text}`)
  return [...header, ...body.flatMap((line) => [line, ''])].join('\n').trimEnd()
}

function buildAnalysisMarkdown(
  episode: Episode,
  meta: PaperMeta | null,
  analysis: Analysis | null,
): string {
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
  return copyEpisode(episode)
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
  return copyEpisode(episode)
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
  return copyEpisode(episode)
}

/**
 * 契约 §1：列表项省略 analysis / script（置 null）、figures（置 []）、illustration / video（置 null），
 * 但保留 cover_url 一族，因为列表卡片要显示封面缩略图。
 * `versions` 同样不进列表（它内部装的就是 script / analysis / video 这些大字段）；
 * `language` / `languages` 保留，列表上要显示语言角标时用得到。
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
    language: episode.language ?? null,
    languages: episode.languages ?? null,
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
  return copyEpisode(episode)
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
  return copyEpisode(episode)
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
 * 复制一份 Episode，避免调用方拿到的 figures / video / versions 与内存态共用同一个对象。
 * video 尤其重要：它是全站共用的共享对象，直接漏出去会让外部误改到别的单集。
 */
function copyEpisode(episode: Episode): Episode {
  return {
    ...episode,
    figures: episode.figures.map((figure) => ({ ...figure })),
    video: episode.video ? { ...episode.video } : null,
    versions: episode.versions
      ? Object.fromEntries(
          Object.entries(episode.versions).map(([language, version]) => [
            language,
            version
              ? {
                  ...version,
                  video: version.video ? { ...version.video } : null,
                  script: version.script
                    ? { ...version.script, segments: version.script.segments.map((s) => ({ ...s })) }
                    : null,
                }
              : version,
          ]),
        )
      : episode.versions,
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

export function scriptTxtUrl(id: string, lang?: EpisodeLanguage): string {
  ensureLoaded()
  const episode = findEpisode(id)
  if (!episode) return ''
  const { meta, script } = downloadContentFor(episode, lang)
  if (!script) return ''
  return trackObjectUrl(id, textBlobUrl(buildScriptText(episode, meta, script), 'text/plain;charset=utf-8'))
}

export function analysisMdUrl(id: string, lang?: EpisodeLanguage): string {
  ensureLoaded()
  const episode = findEpisode(id)
  if (!episode) return ''
  const { meta, analysis } = downloadContentFor(episode, lang)
  if (!analysis) return ''
  return trackObjectUrl(
    id,
    textBlobUrl(buildAnalysisMarkdown(episode, meta, analysis), 'text/markdown;charset=utf-8'),
  )
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
  // 每种语言各一个共享 URL，一起回收（见 videoInfoFor）
  revokeSharedVideoUrls()
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
