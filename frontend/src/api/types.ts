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

/** 论文原图 / 原表（契约 §1：从 PDF 按「Figure N:」图注定位后渲染出的区域） */
export interface Figure {
  /** 形如 f1（图）/ t1（表） */
  id: string
  kind: 'figure' | 'table'
  /** 图注里的标签，如 "Figure 1" */
  label: string
  caption: string
  page: number | null
  /** 资源地址：GET /api/episodes/{id}/figures/{figure_id} */
  url: string
  width: number | null
  height: number | null
}

/**
 * 人工校正配图的旋转方向（契约 §2：POST /figures/{fid}/rotate）。
 * `cw` = 顺时针 90°，`ccw` = 逆时针 90°；PNG 转 90° 无损，转多了转回来即可。
 */
export type FigureRotateDirection = 'cw' | 'ccw'

/** 模型生成的论文信息图（契约 §2：svg_url 保留 SMIL 动画，需用 <object> 引用） */
export interface Illustration {
  png_url: string
  svg_url: string
  width: number | null
  height: number | null
  /** "model" = 大模型生成；"fallback" = 后端兜底模板 */
  source: 'model' | 'fallback' | string
}

/**
 * 视频解读播客（契约 §1 / §2：GET /api/episodes/{id}/video，video/mp4，H.264 + AAC）。
 * 画幅固定 936×1210（竖版，与论文 PDF 首页同尺寸）。
 */
export interface VideoInfo {
  /** 视频资源地址 */
  url: string
  /** 视频时长（秒） */
  duration_sec: number | null
  /** 画面段数：脚本每轮对应一个画面 */
  scene_count: number | null
  /** 文件体积（字节） */
  bytes: number | null
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
  /** 封面：PDF 第一页整页渲染图；无封面时 null */
  cover_url: string | null
  cover_width: number | null
  cover_height: number | null
  /** 论文原图，可能为空数组（一篇都没提取到） */
  figures: Figure[]
  /** 生成的信息图，可为 null */
  illustration: Illustration | null
  /** 视频解读播客，无则 null（未启用视频合成 / 合成失败 / 老数据） */
  video: VideoInfo | null
  audio_url: string | null
  audio_duration_sec: number | null
  audio_bytes: number | null
  created_at: string
  updated_at: string
}

/**
 * 列表接口返回的 Episode（契约 §1）：
 * 省略 analysis / script（置为 null）、figures（置为 []）、illustration / video（置为 null）。
 * 但 cover_url / cover_width / cover_height 在列表里保留，列表卡片要显示封面缩略图。
 */
export type EpisodeSummary = Omit<
  Episode,
  'analysis' | 'script' | 'figures' | 'illustration' | 'video'
>

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
  /**
   * 人工校正配图方向。
   *
   * 为什么需要它：论文配图千奇百怪，自动判定「图正不正」不可能总对，
   * 最终只能靠人眼看一眼 —— 觉得歪了就转一下。返回更新后的完整 Episode。
   * 图片文件变了，资源 URL 上的 `?v=<mtime>-<size>` 也跟着变，浏览器不会再用缓存的旧图。
   */
  rotateFigure(id: string, figureId: string, direction: FigureRotateDirection): Promise<Episode>
  /** 删掉一张不需要的配图（只从这一集移除，不删源 PDF）。返回更新后的完整 Episode。 */
  deleteFigure(id: string, figureId: string): Promise<Episode>
  /** 脚本 txt 的下载地址（mock 下是 Blob URL） */
  scriptTxtUrl(id: string): string
  /** 结构化解读 md 的下载地址（mock 下是 Blob URL） */
  analysisMdUrl(id: string): string
}
