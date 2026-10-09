/**
 * API 适配器（契约 §4）：页面只从这里 import。
 * - VITE_USE_MOCK=1 → src/api/mock.ts（纯浏览器离线运行）
 * - 否则           → src/api/real.ts（真实 fetch，/api 前缀，开发态走 Vite proxy）
 */
import type { ApiAdapter, Episode } from './types'
import realAdapter from './real'
import mockAdapter from './mock'
import { setUnauthorizedHandler as registerUnauthorizedHandler } from './unauthorized'

/** 构建时由 VITE_USE_MOCK=1 决定 */
export const IS_MOCK: boolean = import.meta.env.VITE_USE_MOCK === '1'

const adapter: ApiAdapter = IS_MOCK ? mockAdapter : realAdapter

/** 当前使用的适配器（mode 字段可用于展示数据来源） */
export const api: ApiAdapter = adapter

/**
 * 注册「任意受保护接口返回 401」时的处理函数（契约 §3）。
 *
 * 会话 cookie 随时可能失效（30 天到期 / 改口令会删掉其他设备的会话），
 * 只在启动时判一次登录态是不够的：用户会在某次点击后收到「需要登录」却无处可去。
 * 两个适配器都走这条通道 —— Mock 演示站也要能演示「登出后被挡在登录页」。
 */
export function setUnauthorizedHandler(handler: ((error: unknown) => void) | null): void {
  registerUnauthorizedHandler(handler as Parameters<typeof registerUnauthorizedHandler>[0])
}

export function getHealth() {
  return adapter.getHealth()
}

export function getOptions() {
  return adapter.getOptions()
}

export function createEpisodeFromFile(input: Parameters<ApiAdapter['createEpisodeFromFile']>[0]) {
  return adapter.createEpisodeFromFile(input)
}

export function createEpisodeFromUrl(input: Parameters<ApiAdapter['createEpisodeFromUrl']>[0]) {
  return adapter.createEpisodeFromUrl(input)
}

export function createEpisodeFromText(input: Parameters<ApiAdapter['createEpisodeFromText']>[0]) {
  return adapter.createEpisodeFromText(input)
}

/** 批量生成（契约 §2.8）：返回 { created, failed, total }，单项失败不影响其他项 */
export function createEpisodeBatch(input: Parameters<ApiAdapter['createEpisodeBatch']>[0]) {
  return adapter.createEpisodeBatch(input)
}

export function listEpisodes(params: Parameters<ApiAdapter['listEpisodes']>[0] = {}) {
  return adapter.listEpisodes(params)
}

export function getEpisode(id: string) {
  return adapter.getEpisode(id)
}

export function deleteEpisode(id: string) {
  return adapter.deleteEpisode(id)
}

export function retryEpisode(id: string) {
  return adapter.retryEpisode(id)
}

/** 人工校正配图方向（契约 §2）：返回更新后的完整 Episode */
export function rotateFigure(
  id: string,
  figureId: string,
  direction: Parameters<ApiAdapter['rotateFigure']>[2],
) {
  return adapter.rotateFigure(id, figureId, direction)
}

/** 删除一张配图（契约 §2）：返回更新后的完整 Episode */
export function deleteFigure(id: string, figureId: string) {
  return adapter.deleteFigure(id, figureId)
}

/**
 * 用现有素材重新合成视频（契约 §2：POST /video/rebuild?lang=xx）。
 * 复用上次的画面分配、不调用模型，实测约 11 秒 —— 调用方必须给 loading 反馈，别当它瞬间返回。
 * `lang` 指定重新合成哪个语言版本（中英两版的视频是两个独立产物），省略时取主语言。
 */
export function rebuildVideo(id: string, lang?: Parameters<ApiAdapter['rebuildVideo']>[1]) {
  return adapter.rebuildVideo(id, lang)
}

/** 脚本 txt 下载地址（mock 下是 Blob URL）。`lang` 省略时取主语言。 */
export function scriptTxtUrl(id: string, lang?: Parameters<ApiAdapter['scriptTxtUrl']>[1]) {
  return adapter.scriptTxtUrl(id, lang)
}

/** 结构化解读 md 下载地址（mock 下是 Blob URL）。`lang` 省略时取主语言。 */
export function analysisMdUrl(id: string, lang?: Parameters<ApiAdapter['analysisMdUrl']>[1]) {
  return adapter.analysisMdUrl(id, lang)
}

// ---------------------------------------------------------------------------
// V2：账号与会话（契约 §2.5）
// ---------------------------------------------------------------------------

export function getMe() {
  return adapter.getMe()
}

export function register(input: Parameters<ApiAdapter['register']>[0]) {
  return adapter.register(input)
}

export function login(input: Parameters<ApiAdapter['login']>[0]) {
  return adapter.login(input)
}

export function logout() {
  return adapter.logout()
}

export function changePassword(input: Parameters<ApiAdapter['changePassword']>[0]) {
  return adapter.changePassword(input)
}

/** 生成配额（`GET /api/usage`）：拿不到就当作「没有配额信息」，不要挡住表单 */
export function getUsage() {
  return adapter.getUsage()
}

// ---------------------------------------------------------------------------
// V2：个人专辑（契约 §2.6）
// ---------------------------------------------------------------------------

export function listAlbums() {
  return adapter.listAlbums()
}

export function createAlbum(input: Parameters<ApiAdapter['createAlbum']>[0]) {
  return adapter.createAlbum(input)
}

export function getAlbum(id: string) {
  return adapter.getAlbum(id)
}

export function updateAlbum(id: string, input: Parameters<ApiAdapter['updateAlbum']>[1]) {
  return adapter.updateAlbum(id, input)
}

export function deleteAlbum(id: string) {
  return adapter.deleteAlbum(id)
}

export function addAlbumEpisodes(id: string, episodeIds: string[]) {
  return adapter.addAlbumEpisodes(id, episodeIds)
}

export function removeAlbumEpisode(id: string, episodeId: string) {
  return adapter.removeAlbumEpisode(id, episodeId)
}

// ---------------------------------------------------------------------------
// V2：一键分享（契约 §2.7）
// ---------------------------------------------------------------------------

export function enableShare(id: string) {
  return adapter.enableShare(id)
}

export function disableShare(id: string) {
  return adapter.disableShare(id)
}

export function resetShare(id: string) {
  return adapter.resetShare(id)
}

/** 公开视图（免登录）。**这一条永远不会触发登录跳转** */
export function getShare(token: string) {
  return adapter.getShare(token)
}

/** 首页展示用的一期（免登录） */
export function getShowcase() {
  return adapter.getShowcase()
}

export function shareScriptTxtUrl(token: string, lang?: Parameters<ApiAdapter['shareScriptTxtUrl']>[1]) {
  return adapter.shareScriptTxtUrl(token, lang)
}

export function shareAnalysisMdUrl(token: string, lang?: Parameters<ApiAdapter['shareAnalysisMdUrl']>[1]) {
  return adapter.shareAnalysisMdUrl(token, lang)
}

/** 音频地址：契约里 Episode.audio_url 已经是可播放的 URL（无音频时为 null） */
export function audioUrl(episode: Pick<Episode, 'audio_url'>): string | null {
  return episode.audio_url
}

/** 触发浏览器下载（同时适用于后端 URL 与 mock 的 Blob URL） */
export function downloadUrl(url: string, filename: string): void {
  if (!url) return
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

export { ApiError, errorMessage, isApiError } from './error'
export type {
  Album,
  AlbumDetail,
  AlbumWriteInput,
  Analysis,
  BatchFailure,
  BatchFilesInput,
  BatchResult,
  BatchTextsInput,
  BatchUrlsInput,
  CreateFileInput,
  CreateTextInput,
  CreateUrlInput,
  DurationOption,
  Episode,
  EpisodeLanguage,
  EpisodeOptions,
  EpisodeOptionsInput,
  EpisodeStatus,
  EpisodeStatusFilter,
  EpisodeSummary,
  EpisodeSort,
  EpisodeVersion,
  Figure,
  FigureRotateDirection,
  HealthMode,
  HealthPayload,
  Illustration,
  LevelOption,
  LevelValue,
  ListEpisodesParams,
  ListEpisodesResult,
  LoginInput,
  ModeValue,
  OptionsPayload,
  PaperMeta,
  PasswordChangeInput,
  RegisterInput,
  Script,
  ScriptSegment,
  ShareAuthor,
  ShareVersion,
  ShareView,
  SourceType,
  UsagePayload,
  User,
  VideoInfo,
  Visibility,
  VoiceOption,
} from './types'
// 运行时函数：语言标识的收窄（老数据里可能是任意字符串）
export { isEpisodeLanguage } from './types'
