import { ApiError } from './error'
import type {
  ApiAdapter,
  CreateFileInput,
  CreateTextInput,
  CreateUrlInput,
  Episode,
  EpisodeOptionsInput,
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
  }
}

export const mode = 'real' as const

export function getHealth(): Promise<HealthPayload> {
  return request<HealthPayload>('/health')
}

export function getOptions(): Promise<OptionsPayload> {
  return request<OptionsPayload>('/options')
}

export function createEpisodeFromFile(input: CreateFileInput): Promise<Episode> {
  const { duration_min, level, voice_a, voice_b } = normalizeOptions(input.options)
  const form = new FormData()
  form.append('file', input.file)
  form.append('duration_min', String(duration_min))
  form.append('level', level)
  form.append('voice_a', voice_a)
  form.append('voice_b', voice_b)
  // 不要手动设置 Content-Type，交给浏览器带上 multipart boundary
  return request<Episode>('/episodes', { method: 'POST', body: form })
}

export function createEpisodeFromUrl(input: CreateUrlInput): Promise<Episode> {
  const { duration_min, level, voice_a, voice_b } = normalizeOptions(input.options)
  return request<Episode>('/episodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_type: 'url',
      url: input.url,
      options: { duration_min, level, voice_a, voice_b },
    }),
  })
}

export function createEpisodeFromText(input: CreateTextInput): Promise<Episode> {
  const { duration_min, level, voice_a, voice_b } = normalizeOptions(input.options)
  return request<Episode>('/episodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_type: 'text',
      text: input.text,
      options: { duration_min, level, voice_a, voice_b },
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

export function scriptTxtUrl(id: string): string {
  return `${BASE}/episodes/${encodeURIComponent(id)}/script.txt`
}

export function analysisMdUrl(id: string): string {
  return `${BASE}/episodes/${encodeURIComponent(id)}/analysis.md`
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
