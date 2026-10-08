import { ApiError } from './error'
import { sanitizeLanguages } from '../utils/language'
import type {
  ApiAdapter,
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
  OptionsPayload,
} from './types'

/**
 * 真实后端适配器：全部请求打到 /api（开发态由 vite.config.ts 的 proxy 转发到 127.0.0.1:8000）。
 */

const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, init)
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
    throw new ApiError(detail, response.status)
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
  return request<HealthPayload>('/health')
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
