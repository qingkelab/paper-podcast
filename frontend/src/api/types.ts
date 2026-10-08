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
  /**
   * "intro" / "outro" 表示这是社区品牌话术，不是论文正文（契约 §1）。
   * 老后端不返回这个字段。视频层靠它把片尾渲染成品牌卡，
   * 首页展示脚本片段时要把它排除掉 —— 否则露出来的第一句是「这里是青稞社区」。
   */
  brand?: 'intro' | 'outro' | null
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
  /**
   * 视频里的画面已经跟不上当前配图（契约 §2「video.stale 字段」）。
   *
   * 为什么会有这个字段：视频是把配图**烘焙进 MP4** 的，用户旋转/删除过配图以后，
   * 已经生成的视频里还是旧画面 —— 校正就白做了。后端比较存下来的素材版本号与当前文件，
   * 不一致（以及「没有版本记录的旧视频」这种无法证明一致的情况）就置 true。
   *
   * 可选：老后端可能不返回这个字段。**只有严格为 true 才提示**，缺失/undefined 一律
   * 当作「没问题」处理 —— 拿不准的时候宁可少提示，也不要平白吓唬用户。
   */
  stale?: boolean
}

/**
 * 语言版本标识（契约 §1「双语版本（bilingual）」）。
 *
 * `versions` 的 key 就是这两个值，`languages` 里列出的也是它们；
 * 契约目前只定义 `zh` / `en`，所以这里收成字面量联合 —— 视图层拿到后端数据后
 * 一律用 `isEpisodeLanguage()` 收窄，不认识的取值会被当成「没有这个版本」。
 */
export type EpisodeLanguage = 'zh' | 'en'

/** 运行时收窄：后端将来新增语言时不会让页面崩，只会认不出来 */
export function isEpisodeLanguage(value: unknown): value is EpisodeLanguage {
  return value === 'zh' || value === 'en'
}

/**
 * 单个语言版本（契约 §1「双语版本」）。
 *
 * 为什么会有它：同一集可以同时有中文和英文两版，讲的是同一篇论文，
 * **配图共用**（封面 / 论文原图 / 信息图都在 Episode 顶层，不在这里），
 * 但脚本、解读、音频、视频各语言独立 —— 图不用重做，声音必须分语言。
 */
export interface EpisodeVersion {
  language: EpisodeLanguage
  /** 该语言的脚本 */
  script: Script | null
  /** 该语言的结构化解读 */
  analysis: Analysis | null
  /** 该语言的论文元信息（标题作者本身多为英文，两版通常一致） */
  paper_meta: PaperMeta | null
  /**
   * 该语言的信息图（契约 §1）。
   * 信息图**按语言各出一份**（图上写着字），封面和论文原图才是跨语言共用的。
   */
  illustration?: Illustration | null
  /** 该语言的音频（契约 §2：GET /episodes/{id}/audio?lang=xx） */
  audio_url: string | null
  audio_duration_sec: number | null
  audio_bytes: number | null
  /** 该语言的视频解读，无则 null */
  video: VideoInfo | null
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

  // --- 双语版本（契约 §1「双语版本（bilingual）」） --------------------------
  //
  // 上面的**顶层字段 mirror 主语言那一版**（`language` 指的那一版），所以老代码
  // 只读顶层字段照样能跑。新代码应当优先读 `versions`。
  //
  // 这三个字段都标成可选：老数据（双语功能上线前生成的单集）没有它们，
  // 老后端也不会返回。缺失时一律退回顶层字段，并且**不显示语言切换器**。

  /** 主语言；缺省时按 `languages[0]` 推断 */
  language?: EpisodeLanguage | null
  /** 本集已有的语言版本，顺序 = 生成顺序（先默认语言，后其他） */
  languages?: EpisodeLanguage[] | null
  /** 按语言索引；没有的版本不出现（所以是 Partial） */
  versions?: Partial<Record<EpisodeLanguage, EpisodeVersion>> | null
}

/**
 * 列表接口返回的 Episode（契约 §1）：
 * 省略 analysis / script（置为 null）、figures（置为 []）、illustration / video（置为 null）。
 * 但 cover_url / cover_width / cover_height 在列表里保留，列表卡片要显示封面缩略图。
 *
 * `versions` 同样不放进列表项：它内部装的就是 script / analysis / video 这些大字段，
 * 带上它就等于列表接口白白拖着多份正文。`language` / `languages` 保留（只有一个字符串数组，
 * 列表上要显示「中文 / English」这类角标时用得到）。
 */
export type EpisodeSummary = Omit<
  Episode,
  'analysis' | 'script' | 'figures' | 'illustration' | 'video' | 'versions'
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
  /**
   * 这个音色属于哪一种语言版本。
   * 英文版不能用中文音色（虽然也能出声，但口音很明显），
   * 所以说话人名字要按当前语言去对应语言的音色里取。
   * 后端老版本不返回这个字段，缺省按中文处理。
   */
  language?: EpisodeLanguage
}

/** 服务端支持的语言（新建时选要产出哪些版本） */
export interface LanguageOption {
  value: EpisodeLanguage
  label: string
}

export interface OptionsPayload {
  durations: DurationOption[]
  levels: LevelOption[]
  voices: VoiceOption[]
  /** 后端老版本不返回，做可选处理 */
  languages?: LanguageOption[]
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
  /**
   * 要生成哪些语言版本（契约 §1「新建时用 options.languages 指定要哪些版本」）。
   * 省略时后端按 `["zh"]` 处理。multipart 上传提交的是逗号分隔字符串（`zh,en`），
   * 由 real.ts 负责转换，调用方一律传数组。
   */
  languages?: EpisodeLanguage[]
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
  /**
   * 用现有素材**重新合成视频**（契约 §2：POST /video/rebuild?lang=xx）。
   *
   * 为什么需要它：视频是把配图烘焙进 MP4 的，人工校正配图后已生成的视频里还是旧画面。
   * 它复用上次的画面分配、不调用模型，所以只要十几秒（实测约 11 秒），
   * 而且画面不会因为「我只转了一张图」就全变。返回更新后的完整 Episode
   * （`video.url` 带上了新的 `?v=` 版本号，`video.stale` 归位为 false）。
   *
   * `lang` 指定要重新合成哪个语言版本，省略时后端取主语言：
   * 中英两版的视频是各自独立的产物，只该重做用户当前在看的那一版。
   */
  rebuildVideo(id: string, lang?: EpisodeLanguage): Promise<Episode>
  /** 脚本 txt 的下载地址（mock 下是 Blob URL）。`lang` 省略时取主语言。 */
  scriptTxtUrl(id: string, lang?: EpisodeLanguage): string
  /** 结构化解读 md 的下载地址（mock 下是 Blob URL）。`lang` 省略时取主语言。 */
  analysisMdUrl(id: string, lang?: EpisodeLanguage): string
}
