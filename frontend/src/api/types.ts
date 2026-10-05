/**
 * 数据模型 —— 严格对齐 docs/API.md（冻结版 v1）§1。
 * 前端不得依赖契约之外的字段。
 */

export type SourceType = 'pdf' | 'url' | 'text'

export type EpisodeStatus =
  | 'queued'
  | 'parsing'
  | 'analyzing'
  | 'scripting'
  | 'synthesizing'
  | 'completed'
  | 'failed'

export type LevelValue = 'intro' | 'advanced' | 'expert'

export type ModeValue = 'mock' | 'doubao'

export interface EpisodeOptions {
  duration_min: number
  level: LevelValue
  voice_a: string
  voice_b: string
}

export interface PaperMeta {
  title: string | null
  authors: string[] | null
  abstract: string | null
  year: number | null
  venue: string | null
  arxiv_id: string | null
  keywords: string[] | null
}

/** 结构化论文解读 */
export interface Analysis {
  background: string | null
  innovations: string[] | null
  method: string | null
  experiments: string | null
  conclusion: string | null
  limitations: string[] | null
  value: string | null
  future: string[] | null
}

export interface ScriptSegment {
  speaker: 'A' | 'B'
  text: string
  round: number
}

export interface Script {
  segments: ScriptSegment[]
  word_count: number
  est_duration_sec: number
}

export interface Episode {
  id: string
  title: string
  source_type: SourceType
  source_ref: string | null
  status: EpisodeStatus
  stage_label: string
  progress: number
  error: string | null
  options: EpisodeOptions
  paper_meta: PaperMeta | null
  analysis: Analysis | null
  script: Script | null
  audio_url: string | null
  audio_duration_sec: number | null
  audio_bytes: number | null
  created_at: string
  updated_at: string
}

/** 列表接口返回的 Episode：analysis / script 为 null（契约 §1） */
export type EpisodeSummary = Episode

export interface ListEpisodesParams {
  limit?: number
  offset?: number
  status?: EpisodeStatus
  q?: string
}

export interface ListEpisodesResult {
  items: EpisodeSummary[]
  total: number
}

export interface DurationOption {
  value: number
  label: string
}

export interface LevelOption {
  value: LevelValue
  label: string
}

export interface VoiceOption {
  id: string
  label: string
  gender: string
  pair: string
}

export interface OptionsPayload {
  durations: DurationOption[]
  levels: LevelOption[]
  voices: VoiceOption[]
}

export interface HealthPayload {
  status: string
  version: string
  modes: {
    llm: ModeValue | string
    tts: ModeValue | string
  }
}

/** 创建任务的参数（三种来源统一成同一个 options 结构） */
export interface EpisodeOptionsInput {
  duration_min?: number
  level?: LevelValue
  voice_a?: string
  voice_b?: string
}

export interface CreateFileInput {
  file: File
  options?: EpisodeOptionsInput
}

export interface CreateUrlInput {
  url: string
  options?: EpisodeOptionsInput
}

export interface CreateTextInput {
  text: string
  options?: EpisodeOptionsInput
}

/**
 * 适配器契约：real.ts 与 mock.ts 必须同时满足这个接口（TS 会结构性检查），
 * 页面只通过 src/api/index.ts 调用它。
 */
export interface ApiAdapter {
  readonly mode: 'real' | 'mock'
  getHealth(): Promise<HealthPayload>
  getOptions(): Promise<OptionsPayload>
  createEpisodeFromFile(input: CreateFileInput): Promise<Episode>
  createEpisodeFromUrl(input: CreateUrlInput): Promise<Episode>
  createEpisodeFromText(input: CreateTextInput): Promise<Episode>
  listEpisodes(params?: ListEpisodesParams): Promise<ListEpisodesResult>
  getEpisode(id: string): Promise<Episode>
  deleteEpisode(id: string): Promise<void>
  retryEpisode(id: string): Promise<Episode>
  /** 脚本 txt 的下载地址（mock 下是 Blob URL） */
  scriptTxtUrl(id: string): string
  /** 结构化解读 md 的下载地址（mock 下是 Blob URL） */
  analysisMdUrl(id: string): string
}
