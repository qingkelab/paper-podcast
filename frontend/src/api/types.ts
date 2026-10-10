/**
 * 数据模型 —— 严格对齐 docs/API.md（冻结版 v2）§1。
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
  /** 主语言（契约 §1）；老后端不返回 */
  language?: EpisodeLanguage
  /** 要产出哪些语言版本 */
  languages?: EpisodeLanguage[]
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
  /** 画幅：竖版（936×1210，默认）/ 横版（1920×1080）。老后端不返回，缺失按竖版处理 */
  orientation?: VideoOrientation
  /**
   * 视频**第一帧**的静帧地址，给页面上的 `<video poster>` 用。
   *
   * 为什么需要它：封面标题是**画进画面的**，而 `<video>` 在播放前显示的是 poster。
   * 用论文首页（`cover_url`）顶替的话，那一页上**没有标题** —— 页面上就看不出
   * 封面长什么样（实测被用户当成「没有看到标题」）。老视频没有这个字段，前端退回用封面。
   */
  poster_url?: string | null
  /**
   * 封面上的大字标题（爆款标题，逐语言一份）。
   *
   * 空串表示「没有自定义过」——视频封面会退回显示论文原题。用户可以在单集页改它，
   * **改完要重新合成视频才生效**（标题是烘焙进 MP4 的画面，不是播放器上的浮层）。
   */
  hook?: string
  /**
   * 封面标题改过、但**还没重新合成**（视频里烘的还是旧标题）。
   *
   * 为什么要有它：`stale` 说的是**配图**（旋转/删除过论文原图），提示文案和处置完全不同。
   * 标题只是一行字，用户要的是「重新合成一下就生效」—— 而这个按钮平时只挂在配图过时的
   * 提示条里，所以改标题之后必须**就地**给出入口（否则界面上会出现一句
   * 「点重新合成视频后生效」，而那个按钮根本不在页面上）。
   */
  title_pending?: boolean
  /**
   * 这份画面是 `'html'`（Chrome 无头）还是 `'svg'`（resvg 保底）画的。
   *
   * 两条渲染路径同时在（没装 Chrome 就自动降级），所以「同一篇论文在两台机器上出的
   * 画面不一样」只能靠它分辨。界面上不显示，排查时看接口返回。
   */
  renderer?: string | null
}

/**
 * 视频画幅。竖版跟着论文首页的比例（手机全屏好看），横版 16:9（投屏、B 站、X）。
 * 两者可以同时存在：横版是**额外产出**的一份，不会顶掉竖版。
 */
export type VideoOrientation = 'portrait' | 'landscape'

export const VIDEO_ORIENTATIONS: ReadonlyArray<{ value: VideoOrientation; label: string }> = [
  { value: 'portrait', label: '竖版' },
  { value: 'landscape', label: '横版' },
]

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
  /** 横版（1920×1080）。没生成过就是 null —— 竖版与横版可以并存 */
  video_landscape?: VideoInfo | null
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
  /** 横版（1920×1080）。没生成过就是 null —— 竖版与横版可以并存 */
  video_landscape?: VideoInfo | null
  audio_url: string | null
  audio_duration_sec: number | null
  audio_bytes: number | null
  created_at: string
  updated_at: string

  // --- 归属与可见性（契约 §1「账号、归属与可见性（V2）」） ---------------------

  /**
   * 可见性。`public` 时任何人拿到 `share_token` 都能看（免登录）。
   * 标成可选：老后端 / 老落盘数据里没有这两个字段，缺失一律当「私有、无分享」处理。
   */
  visibility?: Visibility
  /** 分享随机串；只有 visibility=public 时才有值（契约 §2.7） */
  share_token?: string | null
  /** 所属专辑 id；不在任何专辑里时为 null（契约 §2.6） */
  album_id?: string | null

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

/**
 * 播客库的排序方式（契约 §2.5 `sort` 参数）。
 * 后端是**封闭白名单**，传别的值会 400，所以这里也用联合类型把它钉住。
 */
export type EpisodeSort =
  | 'created_desc'
  | 'created_asc'
  | 'updated_desc'
  | 'title_asc'
  | 'duration_desc'

/**
 * 列表筛选用的状态：7 个真实状态，外加伪状态 `running`（所有非终态）。
 * 见契约 §2.5 —— 用户脑子里的分类是「在跑的 / 完成的 / 失败的」。
 */
export type EpisodeStatusFilter = EpisodeStatus | 'running'

export interface ListEpisodesParams {
  limit?: number
  offset?: number
  status?: EpisodeStatusFilter
  q?: string
  /** 缺省时后端按 `created_desc` 排（新的在前） */
  sort?: EpisodeSort
}

export interface ListEpisodesResult {
  items: EpisodeSummary[]
  total: number
}

/**
 * 可见性（契约 §1）。
 * `private` 只有作者能看，`public` 任何人拿到 share_token 都能看。
 */
export type Visibility = 'private' | 'public'

/** 无归属（V1 时代留下的数据）/ 有归属用户，用于提示 */
export type HealthMode = 'open' | 'auth'

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
  /**
   * 契约 §2「GET /api/health」：`"open"`（库里还没有任何用户，免登录直接用）
   * 或 `"auth"`（需要登录）。老后端不返回这个字段，缺失时按 `"auth"` 之外的
   * 「不知道」处理 —— 见 stores/session.ts 的判定，避免误把用户挡在门外。
   */
  mode?: HealthMode
}

// ---------------------------------------------------------------------------
// V2：账号与会话（契约 §2.5）
// ---------------------------------------------------------------------------

/** 契约 §2.5「用户信息」。口令永不返回。 */
export interface User {
  id: string
  username: string
  display_name: string
  created_at: string
}

export interface RegisterInput {
  username: string
  password: string
  display_name?: string
  /** 服务端设了 SIGNUP_CODE 时必填且必须一致 */
  signup_code?: string
}

export interface LoginInput {
  username: string
  password: string
}

export interface PasswordChangeInput {
  current_password: string
  new_password: string
}

/**
 * 生成配额（`GET /api/usage`）。**这是 V2 后期新增的接口**，文档可能还没写进去，
 * 所以前端一律 fail-open：拿不到就不显示额度提示，绝不因为这一个接口挡住生成表单。
 *
 * `remaining === null` 表示服务端没设配额（等于不限量）。
 */
export interface UsagePayload {
  /** 最近 24 小时已生成几期（滑动窗口） */
  used: number
  /** 上限；0 表示不限 */
  limit: number
  remaining: number | null
  resets_at: string | null
}

// ---------------------------------------------------------------------------
// V2：个人专辑（契约 §2.6）
// ---------------------------------------------------------------------------

export interface Album {
  id: string
  title: string
  description: string | null
  episode_count: number
  /** 专辑里最新一集的封面；没有单集时为 null */
  cover_url: string | null
  created_at: string
  updated_at: string
}

/** 详情接口附带里面的单集（只含 include_large=false 的摘要） */
export interface AlbumDetail extends Album {
  episodes: EpisodeSummary[]
}

export interface AlbumWriteInput {
  title?: string
  description?: string | null
}

// ---------------------------------------------------------------------------
// V2：一键分享（契约 §2.7）
// ---------------------------------------------------------------------------

/**
 * 公开视图。**不是完整 Episode**：不含 options / source_ref / raw_text /
 * error / id / user_id，作者也只给展示名。
 */
export interface ShareVersion {
  language: EpisodeLanguage
  paper_meta: PaperMeta | null
  analysis: Analysis | null
  script: Script | null
  illustration: Illustration | null
  audio_url: string | null
  audio_duration_sec: number | null
  audio_bytes: number | null
  video: VideoInfo | null
  /** 横版（1920×1080）。没生成过就是 null —— 竖版与横版可以并存 */
  video_landscape?: VideoInfo | null
}

export interface ShareAuthor {
  /** 展示名（可为空时后端回退到 username） */
  display_name: string
}

export interface ShareView {
  token: string | null
  title: string
  paper_meta: PaperMeta | null
  language: EpisodeLanguage
  languages: EpisodeLanguage[]
  /** 按语言索引，与 Episode.versions 同形（但只含公开字段） */
  versions: Partial<Record<EpisodeLanguage, ShareVersion>>
  cover_url: string | null
  cover_width?: number | null
  cover_height?: number | null
  figures: Figure[]
  illustration: Illustration | null
  video: VideoInfo | null
  /** 横版（1920×1080）。没生成过就是 null —— 竖版与横版可以并存 */
  video_landscape?: VideoInfo | null
  audio_url: string | null
  audio_duration_sec: number | null
  audio_bytes: number | null
  script: Script | null
  analysis: Analysis | null
  author?: ShareAuthor | null
  created_at: string
}

// ---------------------------------------------------------------------------
// V2：批量生成（契约 §2.8）
// ---------------------------------------------------------------------------

export interface BatchFailure {
  /** 出错的来源（链接 / 文件名） */
  ref: string
  reason: string
}

export interface BatchResult {
  /** 成功入队的单集（摘要形态） */
  created: EpisodeSummary[]
  /** 失败项：**单项失败不影响其他项**，所以这里要逐条给出原因 */
  failed: BatchFailure[]
  total: number
}

/** 批量生成的入参：三种来源各走一套（PDF 多文件 / 多条链接 / 多篇文本） */
export interface BatchFilesInput {
  files: File[]
  options?: EpisodeOptionsInput
}

export interface BatchUrlsInput {
  urls: string[]
  options?: EpisodeOptionsInput
}

export interface BatchTextsInput {
  texts: string[]
  options?: EpisodeOptionsInput
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
  /**
   * 批量生成（契约 §2.8）：一次提交多篇，串行入队。
   * **单项失败不影响其他项** —— 抓不到的进 `failed` 并给出原因，能用的照常入队。
   * 上限 20 篇/次（超了后端返回 400）。
   */
  createEpisodeBatch(
    input: BatchFilesInput | BatchUrlsInput | BatchTextsInput,
  ): Promise<BatchResult>
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
   * 它复用上次的画面分配、不调用模型，所以只要一分钟上下（实测 40~95 秒），
   * 而且画面不会因为「我只转了一张图」就全变。返回更新后的完整 Episode
   * （`video.url` 带上了新的 `?v=` 版本号，`video.stale` 归位为 false）。
   *
   * `lang` 指定要重新合成哪个语言版本，省略时后端取主语言：
   * 中英两版的视频是各自独立的产物，只该重做用户当前在看的那一版。
   */
  rebuildVideo(
    id: string,
    lang?: EpisodeLanguage,
    orientation?: VideoOrientation,
  ): Promise<Episode>
  /**
   * 改封面上的标题（契约 §2：PATCH /cover）。
   *
   * - `headline`：封面上的大字（爆款标题），逐语言一份；传空串 = 清掉自定义、退回论文原题
   * - `paperTitle`：封面下方那行小字的论文原题（也是这一集的名字），跨语言共用；
   *   省略 = 不动
   *
   * **只写数据、不重新合成**：调用方改完要再调 `rebuildVideo` 才看得到新标题。
   * 分开两个接口是因为重新合成要几十秒，用户可能想先把标题改满意了再合成一次。
   */
  updateCover(
    id: string,
    input: { headline?: string; paperTitle?: string; lang?: EpisodeLanguage },
  ): Promise<Episode>
  /** 脚本 txt 的下载地址（mock 下是 Blob URL）。`lang` 省略时取主语言。 */
  scriptTxtUrl(id: string, lang?: EpisodeLanguage): string
  /** 结构化解读 md 的下载地址（mock 下是 Blob URL）。`lang` 省略时取主语言。 */
  analysisMdUrl(id: string, lang?: EpisodeLanguage): string

  // --- V2：账号与会话（契约 §2.5） -----------------------------------------

  /** `GET /api/auth/me`；未登录时抛 401 的 ApiError（**不**触发全局跳登录，由调用方决定） */
  getMe(): Promise<User>
  register(input: RegisterInput): Promise<User>
  login(input: LoginInput): Promise<User>
  /** 幂等：未登录也返回成功 */
  logout(): Promise<void>
  changePassword(input: PasswordChangeInput): Promise<void>

  /** 生成配额（可能不存在于老后端：调用方必须 fail-open） */
  getUsage(): Promise<UsagePayload>

  // --- V2：个人专辑（契约 §2.6） -------------------------------------------

  listAlbums(): Promise<Album[]>
  createAlbum(input: AlbumWriteInput): Promise<Album>
  getAlbum(id: string): Promise<AlbumDetail>
  updateAlbum(id: string, input: AlbumWriteInput): Promise<Album>
  /** 只删专辑，**不删里面的单集**（单集的 album_id 置空） */
  deleteAlbum(id: string): Promise<void>
  /** 批量加入（幂等），返回更新后的专辑详情 */
  addAlbumEpisodes(id: string, episodeIds: string[]): Promise<AlbumDetail>
  /** 从专辑里移出一集，返回更新后的专辑详情 */
  removeAlbumEpisode(id: string, episodeId: string): Promise<AlbumDetail>

  // --- V2：一键分享（契约 §2.7） -------------------------------------------

  /** 开启分享；share_token 已有则**保持不变**（重复点不会让已发出的链接失效） */
  enableShare(id: string): Promise<Episode>
  /** 关闭分享：visibility 置 private、share_token 置 null（链接立即失效） */
  disableShare(id: string): Promise<Episode>
  /** 换一个新 token（旧链接立即失效） */
  resetShare(id: string): Promise<Episode>
  /** 公开视图（**免登录**）；token 失效或那一集不再公开时抛 404 */
  getShare(token: string): Promise<ShareView>
  /** 首页展示用的一期（**免登录**）：登录了取自己最新的，没登录取公开分享的 */
  getShowcase(): Promise<ShareView>
  /** 公开页的脚本 / 解读下载地址（mock 下是 Blob URL） */
  shareScriptTxtUrl(token: string, lang?: EpisodeLanguage): string
  shareAnalysisMdUrl(token: string, lang?: EpisodeLanguage): string
}
