/**
 * 语言版本的展示与本地记忆（契约 §1「双语版本（bilingual）」）。
 *
 * 这里只放**前端自己的**东西：标签文案、语言数组的清洗、以及「这一集上次看的是哪种语言」
 * 的 localStorage 记忆。语言版本本身的数据一律来自后端 Episode.languages / Episode.versions。
 */
import { isEpisodeLanguage } from '../api/types'
import type { EpisodeLanguage } from '../api/types'

export interface LanguageOption {
  value: EpisodeLanguage
  /** 完整名字，用于切换器按钮与提示 */
  label: string
  /** 极短标记，用于下载文件名这类地方（`-zh` / `-en`） */
  short: string
}

/** 契约目前只定义中英两种；顺序 = 新建表单里的展示顺序 */
export const LANGUAGE_OPTIONS: readonly LanguageOption[] = [
  { value: 'zh', label: '中文', short: 'zh' },
  { value: 'en', label: 'English', short: 'en' },
]

/** 新建表单与偏好设置的默认值（契约 §1：`options.languages` 默认 `["zh"]`） */
export const DEFAULT_LANGUAGES: EpisodeLanguage[] = ['zh']

export function languageLabel(value: EpisodeLanguage | null | undefined): string {
  const option = LANGUAGE_OPTIONS.find((item) => item.value === value)
  return option?.label ?? '未知语言'
}

export function languageShort(value: EpisodeLanguage | null | undefined): string {
  const option = LANGUAGE_OPTIONS.find((item) => item.value === value)
  return option?.short ?? 'xx'
}

/** 例如「中文 + English」，用于视频角标、元信息这类要一句话说清的地方 */
export function languagesText(languages: readonly EpisodeLanguage[]): string {
  if (!languages.length) return '—'
  return languages.map((lang) => languageLabel(lang)).join(' + ')
}

/**
 * 把任意来源（localStorage / 后端）的语言数组洗干净：
 * 去掉认不出来的取值、去重；**顺序保持原样**（契约：顺序 = 生成顺序）。
 */
export function sanitizeLanguages(raw: unknown): EpisodeLanguage[] {
  if (!Array.isArray(raw)) return []
  const result: EpisodeLanguage[] = []
  raw.forEach((item) => {
    if (isEpisodeLanguage(item) && !result.includes(item)) result.push(item)
  })
  return result
}

// ---------------------------------------------------------------------------
// 「这一集上次看的是哪种语言」
//
// 为什么按单集分别记：同一个人可能中文那集看中文、英文那集看英文，
// 用一个全局开关会互相覆盖。这里用 episodeId → language 的小映射。
// ---------------------------------------------------------------------------

const STORAGE_KEY = 'paper-podcast:episode-language:v1'
/** 只记最近这么多集：再往前的记录没人会回头查，留着只会把 localStorage 越写越大 */
const MAX_ENTRIES = 60

type Picks = Record<string, EpisodeLanguage>

function readPicks(): Picks {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const picks: Picks = {}
    Object.entries(parsed as Record<string, unknown>).forEach(([id, value]) => {
      if (id && isEpisodeLanguage(value)) picks[id] = value
    })
    return picks
  } catch {
    // 隐私模式 / 脏数据：当作没有记录
    return {}
  }
}

function writePicks(picks: Picks): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(picks))
  } catch {
    // 写不进去就算了，语言记忆丢了不影响功能
  }
}

/** 取这一集上次选的语言；没有记录或记录已失效时返回 null，由调用方决定退回到哪一版 */
export function loadEpisodeLanguage(episodeId: string): EpisodeLanguage | null {
  if (!episodeId) return null
  return readPicks()[episodeId] ?? null
}

/** 记住这一集选了哪种语言（调用方必须先确认这一集真的有这个版本） */
export function rememberEpisodeLanguage(episodeId: string, language: EpisodeLanguage): void {
  if (!episodeId) return
  const picks = readPicks()
  // 先删再写：让刚用过的这个 key 排到对象末尾，超上限时被淘汰的是最久没碰过的
  delete picks[episodeId]
  picks[episodeId] = language
  const ids = Object.keys(picks)
  if (ids.length > MAX_ENTRIES) {
    ids.slice(0, ids.length - MAX_ENTRIES).forEach((id) => {
      delete picks[id]
    })
  }
  writePicks(picks)
}
