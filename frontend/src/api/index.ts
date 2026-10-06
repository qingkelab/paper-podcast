/**
 * API 适配器（契约 §4）：页面只从这里 import。
 * - VITE_USE_MOCK=1 → src/api/mock.ts（纯浏览器离线运行）
 * - 否则           → src/api/real.ts（真实 fetch，/api 前缀，开发态走 Vite proxy）
 */
import type { ApiAdapter, Episode } from './types'
import realAdapter from './real'
import mockAdapter from './mock'

/** 构建时由 VITE_USE_MOCK=1 决定 */
export const IS_MOCK: boolean = import.meta.env.VITE_USE_MOCK === '1'

const adapter: ApiAdapter = IS_MOCK ? mockAdapter : realAdapter

/** 当前使用的适配器（mode 字段可用于展示数据来源） */
export const api: ApiAdapter = adapter

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

/** 脚本 txt 下载地址（mock 下是 Blob URL） */
export function scriptTxtUrl(id: string) {
  return adapter.scriptTxtUrl(id)
}

/** 结构化解读 md 下载地址（mock 下是 Blob URL） */
export function analysisMdUrl(id: string) {
  return adapter.analysisMdUrl(id)
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
  Analysis,
  CreateFileInput,
  CreateTextInput,
  CreateUrlInput,
  DurationOption,
  Episode,
  EpisodeOptions,
  EpisodeOptionsInput,
  EpisodeStatus,
  EpisodeSummary,
  Figure,
  FigureRotateDirection,
  HealthPayload,
  Illustration,
  LevelOption,
  LevelValue,
  ListEpisodesParams,
  ListEpisodesResult,
  ModeValue,
  OptionsPayload,
  PaperMeta,
  Script,
  ScriptSegment,
  SourceType,
  VideoInfo,
  VoiceOption,
} from './types'
