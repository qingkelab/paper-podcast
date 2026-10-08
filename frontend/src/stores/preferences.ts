import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import type { EpisodeLanguage, LevelValue } from '../api'
import { DEFAULT_LANGUAGES, sanitizeLanguages } from '../utils/language'

export interface EpisodePreferences {
  duration_min: number
  level: LevelValue
  voice_a: string
  voice_b: string
  /** 要生成的语言版本（契约 §1：新建时用 options.languages 指定，至少一个） */
  languages: EpisodeLanguage[]
}

const STORAGE_KEY = 'paper-podcast:preferences:v1'

/** 契约 §5 默认值 */
export const DEFAULT_PREFERENCES: EpisodePreferences = {
  duration_min: 5,
  level: 'intro',
  voice_a: 'zh_male_dayixiansheng_v2_saturn_bigtts',
  voice_b: 'zh_female_mizaitongxue_v2_saturn_bigtts',
  languages: [...DEFAULT_LANGUAGES],
}

const DURATIONS = [3, 5, 10]
const LEVELS: LevelValue[] = ['intro', 'advanced', 'expert']

function sanitize(raw: unknown): EpisodePreferences {
  const value = (raw ?? {}) as Partial<EpisodePreferences>
  const duration =
    typeof value.duration_min === 'number' && DURATIONS.includes(value.duration_min)
      ? value.duration_min
      : DEFAULT_PREFERENCES.duration_min
  const level =
    typeof value.level === 'string' && (LEVELS as string[]).includes(value.level)
      ? (value.level as LevelValue)
      : DEFAULT_PREFERENCES.level
  // 语言版本至少要留一个，否则首页表单会变成「一个都不选」的非法状态
  const languages = sanitizeLanguages(value.languages)
  return {
    duration_min: duration,
    level,
    voice_a: typeof value.voice_a === 'string' && value.voice_a ? value.voice_a : DEFAULT_PREFERENCES.voice_a,
    voice_b: typeof value.voice_b === 'string' && value.voice_b ? value.voice_b : DEFAULT_PREFERENCES.voice_b,
    languages: languages.length ? languages : [...DEFAULT_PREFERENCES.languages],
  }
}

function load(): EpisodePreferences {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULT_PREFERENCES, languages: [...DEFAULT_PREFERENCES.languages] }
    return sanitize(JSON.parse(raw))
  } catch {
    return { ...DEFAULT_PREFERENCES, languages: [...DEFAULT_PREFERENCES.languages] }
  }
}

function save(value: EpisodePreferences): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
  } catch {
    // 隐私模式下写不进去，忽略即可
  }
}

export const usePreferencesStore = defineStore('preferences', () => {
  const preferences = ref<EpisodePreferences>(load())
  /** 用户是否保存过偏好（设置页用来显示「已保存」） */
  const savedAt = ref<number | null>(null)

  watch(
    preferences,
    (value) => {
      save(value)
    },
    { deep: true },
  )

  const durationLabel = computed(() => `${preferences.value.duration_min} 分钟`)

  function update(patch: Partial<EpisodePreferences>): void {
    preferences.value = sanitize({ ...preferences.value, ...patch })
  }

  function reset(): void {
    preferences.value = { ...DEFAULT_PREFERENCES }
  }

  function markSaved(): void {
    savedAt.value = Date.now()
  }

  return { preferences, durationLabel, savedAt, update, reset, markSaved }
})
