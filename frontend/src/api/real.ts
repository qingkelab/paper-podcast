import { ApiError } from './error'
import { notifyUnauthorized, setUnauthorizedHandler as registerUnauthorizedHandler } from './unauthorized'
import { sanitizeLanguages } from '../utils/language'
import type {
  Album,
  AlbumDetail,
  AlbumWriteInput,
  ApiAdapter,
  BatchFilesInput,
  BatchResult,
  BatchTextsInput,
  BatchUrlsInput,
  CreateFileInput,
  CreateTextInput,
  CreateUrlInput,
  Episode,
  EpisodeLanguage,
  EpisodeOptionsInput,
  FigureRotateDirection,
  HealthPayload,
  ListEpisodesParams,
  ListEpisodesResult,
  LoginInput,
  OptionsPayload,
  PasswordChangeInput,
  RegisterInput,
  ShareView,
  UsagePayload,
  User,
} from './types'

/**
 * 真实后端适配器：全部请求打到 /api（开发态由 vite.config.ts 的 proxy 转发到 127.0.0.1:8000）。
 */

const BASE = '/api'

/**
 * 全局 401 处理（契约 §3「登录态与路由」）。
 *
 * 为什么要有它：会话 cookie 是 30 天有效的 httpOnly cookie，**随时可能过期或被服务端删掉**
 * （改口令会删掉其他设备的会话）。只在启动时判一次登录态的话，用户会在一次点击之后
 * 收到一句「需要登录」的红字，却不知道该去哪里 —— 得让他落到登录页。
 *
 * `api/real.ts` 不认识 vue-router，所以真正的跳转由 stores/session.ts 通过
 * `api/unauthorized.ts` 注册进来（mock 走同一条通道）。
 */
export function setUnauthorizedHandler(handler: ((error: ApiError) => void) | null): void {
  registerUnauthorizedHandler(handler)
}

interface RequestOptions extends RequestInit {
  /**
   * 401 时不触发全局跳登录。
   * `/auth/me`、`/auth/login`、`/auth/password` 必须带上它：
   * 「没登录」对前两个来说是正常结果，不能反过来把人踹去登录页（会成环）。
   */
  skipAuthRedirect?: boolean
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { skipAuthRedirect, ...rest } = init ?? {}
  let response: Response
  try {
    // credentials 默认 same-origin：开发态走 Vite proxy 是同源，生产态后端同源托管，
    // 都带得上 pp_session。显式写出来是为了避免以后有人改成跨域部署时静默丢 cookie。
    response = await fetch(`${BASE}${path}`, { credentials: 'same-origin', ...rest })
  } catch {
    throw new ApiError('无法连接后端服务，请确认 http://127.0.0.1:8000 已启动', 0)
  }

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const body: unknown = await response.json()
      if (body && typeof body === 'object' && 'detail' in body) {
        const raw = (body as { detail: unknown }).detail
        if (typeof raw === 'string' && raw) detail = raw
        else if (raw) detail = JSON.stringify(raw)
      }
    } catch {
      // 响应不是 JSON，保留默认文案
    }
    const error = new ApiError(detail, response.status)
    // 401 统一交给会话层处理（清空登录态 + 跳登录页）；白名单里的接口除外
    if (response.status === 401 && !skipAuthRedirect) notifyUnauthorized(error)
    throw error
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function normalizeOptions(options?: EpisodeOptionsInput): Required<EpisodeOptionsInput> {
  return {
    duration_min: options?.duration_min ?? 5,
    level: options?.level ?? 'intro',
    voice_a: options?.voice_a ?? 'zh_male_dayixiansheng_v2_saturn_bigtts',
    voice_b: options?.voice_b ?? 'zh_female_mizaitongxue_v2_saturn_bigtts',
    // 契约 §1：默认只生成主语言；空数组也退回默认，免得后端收到「一个语言都不要」
    languages: sanitizeLanguages(options?.languages).length
      ? sanitizeLanguages(options?.languages)
      : ['zh'],
  }
}

/** 契约 §2：资源接口用 `?lang=` 取对应语言，省略时取主语言 */
function langQuery(lang?: EpisodeLanguage): string {
  return lang ? `?lang=${encodeURIComponent(lang)}` : ''
}

export const mode = 'real' as const

export function getHealth(): Promise<HealthPayload> {
  return request<HealthPayload>('/health', { skipAuthRedirect: true })
}

export function getOptions(): Promise<OptionsPayload> {
  return request<OptionsPayload>('/options')
}

export function createEpisodeFromFile(input: CreateFileInput): Promise<Episode> {
  const { duration_min, level, voice_a, voice_b, languages } = normalizeOptions(input.options)
  const form = new FormData()
  form.append('file', input.file)
  form.append('duration_min', String(duration_min))
  form.append('level', level)
  form.append('voice_a', voice_a)
  form.append('voice_b', voice_b)
  // 契约 §2：multipart 的 languages 是**逗号分隔字符串**（如 zh,en），不是数组
  form.append('languages', languages.join(','))
  // 不要手动设置 Content-Type，交给浏览器带上 multipart boundary
  return request<Episode>('/episodes', { method: 'POST', body: form })
}

export function createEpisodeFromUrl(input: CreateUrlInput): Promise<Episode> {
  const { duration_min, level, voice_a, voice_b, languages } = normalizeOptions(input.options)
  return request<Episode>('/episodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_type: 'url',
      url: input.url,
      // 契约 §2：JSON 形态的 languages 是数组
      options: { duration_min, level, voice_a, voice_b, languages },
    }),
  })
}

export function createEpisodeFromText(input: CreateTextInput): Promise<Episode> {
  const { duration_min, level, voice_a, voice_b, languages } = normalizeOptions(input.options)
  return request<Episode>('/episodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_type: 'text',
      text: input.text,
      options: { duration_min, level, voice_a, voice_b, languages },
    }),
  })
}

/**
 * 批量生成（契约 §2.8）。
 * PDF 走 multipart（`files` 字段**出现多次**），链接 / 文本走 JSON（`urls` / `texts` 数组）。
 */
export function createEpisodeBatch(
  input: BatchFilesInput | BatchUrlsInput | BatchTextsInput,
): Promise<BatchResult> {
  if ('files' in input) {
    const { duration_min, level, voice_a, voice_b, languages } = normalizeOptions(input.options)
    const form = new FormData()
    input.files.forEach((file) => form.append('files', file))
    form.append('duration_min', String(duration_min))
    form.append('level', level)
    form.append('voice_a', voice_a)
    form.append('voice_b', voice_b)
    form.append('languages', languages.join(','))
    return request<BatchResult>('/episodes/batch', { method: 'POST', body: form })
  }

  const { duration_min, level, voice_a, voice_b, languages } = normalizeOptions(input.options)
  const body = 'urls' in input
    ? { source_type: 'url', urls: input.urls }
    : { source_type: 'text', texts: input.texts }
  return request<BatchResult>('/episodes/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...body,
      options: { duration_min, level, voice_a, voice_b, languages },
    }),
  })
}

export function listEpisodes(params: ListEpisodesParams = {}): Promise<ListEpisodesResult> {
  const query = new URLSearchParams()
  query.set('limit', String(params.limit ?? 20))
  query.set('offset', String(params.offset ?? 0))
  if (params.status) query.set('status', params.status)
  if (params.q) query.set('q', params.q)
  return request<ListEpisodesResult>(`/episodes?${query.toString()}`)
}

export function getEpisode(id: string): Promise<Episode> {
  return request<Episode>(`/episodes/${encodeURIComponent(id)}`)
}

export function deleteEpisode(id: string): Promise<void> {
  return request<void>(`/episodes/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function retryEpisode(id: string): Promise<Episode> {
  return request<Episode>(`/episodes/${encodeURIComponent(id)}/retry`, { method: 'POST' })
}

/**
 * 人工校正配图方向（契约 §2）：`cw` 顺时针 90° / `ccw` 逆时针 90°。
 * 返回更新后的完整 Episode —— 注意它的 figures[].url 上带着新的 `?v=` 版本号，
 * 直接把返回的 Episode 覆盖到页面状态上，浏览器就会重新拉图（不会继续用缓存的旧图）。
 */
export function rotateFigure(
  id: string,
  figureId: string,
  direction: FigureRotateDirection,
): Promise<Episode> {
  return request<Episode>(
    `/episodes/${encodeURIComponent(id)}/figures/${encodeURIComponent(figureId)}/rotate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction }),
    },
  )
}

/** 删除一张配图（契约 §2）。返回更新后的完整 Episode。 */
export function deleteFigure(id: string, figureId: string): Promise<Episode> {
  return request<Episode>(
    `/episodes/${encodeURIComponent(id)}/figures/${encodeURIComponent(figureId)}`,
    { method: 'DELETE' },
  )
}

/**
 * 用现有素材重新合成视频（契约 §2：POST /video/rebuild）。
 *
 * 音频、脚本、解读都不动，只重新渲染画面并编码；**复用上次的画面分配**，不再调用模型，
 * 所以实测约 11 秒（5 分钟片长）—— 这个请求本来就该慢，不要给它加超时。
 * 返回更新后的完整 Episode：`video.url` 上带着新的 `?v=` 版本号（内容变了，缓存自动失效），
 * `video.stale` 归位为 false。没有视频时后端返回 409，由 ApiError 带到界面上。
 */
export function rebuildVideo(id: string, lang?: EpisodeLanguage): Promise<Episode> {
  return request<Episode>(
    `/episodes/${encodeURIComponent(id)}/video/rebuild${langQuery(lang)}`,
    { method: 'POST' },
  )
}

export function scriptTxtUrl(id: string, lang?: EpisodeLanguage): string {
  return `${BASE}/episodes/${encodeURIComponent(id)}/script.txt${langQuery(lang)}`
}

export function analysisMdUrl(id: string, lang?: EpisodeLanguage): string {
  return `${BASE}/episodes/${encodeURIComponent(id)}/analysis.md${langQuery(lang)}`
}

// ---------------------------------------------------------------------------
// V2：账号与会话（契约 §2.5）
//
// 会话走 httpOnly cookie（pp_session），前端**拿不到也不该拿** token：
// 所以这里没有任何 Authorization 头，全靠浏览器自动带上 cookie。
// ---------------------------------------------------------------------------

export function getMe(): Promise<User> {
  // 401 = 没登录，是正常结果：绝不能触发全局跳登录（那会在启动时成环）
  return request<User>('/auth/me', { skipAuthRedirect: true }).then((payload) => unwrapUser(payload))
}

export function register(input: RegisterInput): Promise<User> {
  return request<{ user?: User } & User>('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: input.username,
      password: input.password,
      display_name: input.display_name ?? '',
      signup_code: input.signup_code ?? '',
    }),
    // 登录/注册本身的 401/403 是「口令错 / 邀请码不对」，要显示在表单上，不是跳转信号
    skipAuthRedirect: true,
  }).then((payload) => unwrapUser(payload))
}

export function login(input: LoginInput): Promise<User> {
  return request<{ user?: User } & User>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: input.username, password: input.password }),
    skipAuthRedirect: true,
  }).then((payload) => unwrapUser(payload))
}

export function logout(): Promise<void> {
  return request<void>('/auth/logout', { method: 'POST', skipAuthRedirect: true })
}

export function changePassword(input: PasswordChangeInput): Promise<void> {
  return request<void>('/auth/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      current_password: input.current_password,
      new_password: input.new_password,
    }),
    // 当前口令错是 401：要显示在表单上（而不是被当成「会话失效」跳登录页）
    skipAuthRedirect: true,
  })
}

/**
 * 契约里 `/auth/*` 返回的是裸 User（`{"id":…,"username":…}`），
 * 但 §2.5 的 register/login 示例写的是 `{ "user": User }`。两种都认：
 * 有 `user` 字段就用它，否则整包当 User —— 契约在这点上有歧义，前端兼容两边。
 */
function unwrapUser(payload: (User & { user?: User }) | null | undefined): User {
  const candidate = payload && typeof payload === 'object' && payload.user ? payload.user : payload
  if (!candidate || typeof candidate !== 'object' || typeof candidate.id !== 'string') {
    throw new ApiError('后端返回的账号信息无法识别', 0)
  }
  return candidate as User
}

/**
 * 生成配额（`GET /api/usage`）。有配额却不告诉用户还剩多少，等于让人撞 429 才知道。
 * 这个接口比 docs/API.md 的冻结版新，**调用方要 fail-open**（404 就当没有配额）。
 */
export function getUsage(): Promise<UsagePayload> {
  return request<UsagePayload>('/usage', { skipAuthRedirect: true })
}

// ---------------------------------------------------------------------------
// V2：个人专辑（契约 §2.6）
// ---------------------------------------------------------------------------

export function listAlbums(): Promise<Album[]> {
  return request<Album[]>('/albums')
}

export function createAlbum(input: AlbumWriteInput): Promise<Album> {
  return request<Album>('/albums', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: input.title ?? '', description: input.description ?? null }),
  })
}

export function getAlbum(id: string): Promise<AlbumDetail> {
  return request<AlbumDetail>(`/albums/${encodeURIComponent(id)}`)
}

export function updateAlbum(id: string, input: AlbumWriteInput): Promise<Album> {
  const body: Record<string, unknown> = {}
  if (input.title !== undefined) body.title = input.title
  if (input.description !== undefined) body.description = input.description
  return request<Album>(`/albums/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function deleteAlbum(id: string): Promise<void> {
  return request<void>(`/albums/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function addAlbumEpisodes(id: string, episodeIds: string[]): Promise<AlbumDetail> {
  return request<AlbumDetail>(`/albums/${encodeURIComponent(id)}/episodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ episode_ids: episodeIds }),
  })
}

export function removeAlbumEpisode(id: string, episodeId: string): Promise<AlbumDetail> {
  return request<AlbumDetail>(
    `/albums/${encodeURIComponent(id)}/episodes/${encodeURIComponent(episodeId)}`,
    { method: 'DELETE' },
  )
}

// ---------------------------------------------------------------------------
// V2：一键分享（契约 §2.7）
// ---------------------------------------------------------------------------

export function enableShare(id: string): Promise<Episode> {
  return request<Episode>(`/episodes/${encodeURIComponent(id)}/share`, { method: 'POST' })
}

export function disableShare(id: string): Promise<Episode> {
  return request<Episode>(`/episodes/${encodeURIComponent(id)}/share`, { method: 'DELETE' })
}

export function resetShare(id: string): Promise<Episode> {
  return request<Episode>(`/episodes/${encodeURIComponent(id)}/share/reset`, { method: 'POST' })
}

/** 公开视图：**免登录**，所以 404（链接失效）不能被当成「会话过期」 */
export function getShare(token: string): Promise<ShareView> {
  return request<ShareView>(`/share/${encodeURIComponent(token)}`, { skipAuthRedirect: true })
}

/** 首页展示用的一期：同样免登录 */
export function getShowcase(): Promise<ShareView> {
  return request<ShareView>('/showcase', { skipAuthRedirect: true })
}

export function shareScriptTxtUrl(token: string, lang?: EpisodeLanguage): string {
  return `${BASE}/share/${encodeURIComponent(token)}/script.txt${langQuery(lang)}`
}

export function shareAnalysisMdUrl(token: string, lang?: EpisodeLanguage): string {
  return `${BASE}/share/${encodeURIComponent(token)}/analysis.md${langQuery(lang)}`
}

const adapter: ApiAdapter = {
  mode,
  getHealth,
  getOptions,
  createEpisodeFromFile,
  createEpisodeFromUrl,
  createEpisodeFromText,
  createEpisodeBatch,
  listEpisodes,
  getEpisode,
  deleteEpisode,
  retryEpisode,
  rotateFigure,
  deleteFigure,
  rebuildVideo,
  scriptTxtUrl,
  analysisMdUrl,
  getMe,
  register,
  login,
  logout,
  changePassword,
  getUsage,
  listAlbums,
  createAlbum,
  getAlbum,
  updateAlbum,
  deleteAlbum,
  addAlbumEpisodes,
  removeAlbumEpisode,
  enableShare,
  disableShare,
  resetShare,
  getShare,
  getShowcase,
  shareScriptTxtUrl,
  shareAnalysisMdUrl,
}

export default adapter
