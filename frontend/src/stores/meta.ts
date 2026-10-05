import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { IS_MOCK, errorMessage, getHealth, getOptions } from '../api'
import type { HealthPayload, LevelOption, OptionsPayload } from '../api'

/** 后端不可用时的兜底选项（等于契约 §2 的默认返回值），保证表单永远可用 */
const FALLBACK_OPTIONS: OptionsPayload = {
  durations: [
    { value: 3, label: '3 分钟' },
    { value: 5, label: '5 分钟' },
    { value: 10, label: '10 分钟' },
  ],
  levels: [
    { value: 'intro', label: '入门' },
    { value: 'advanced', label: '进阶' },
    { value: 'expert', label: '专业' },
  ],
  voices: [
    {
      id: 'zh_male_dayixiansheng_v2_saturn_bigtts',
      label: '大义先生（男声·学术沉稳）',
      gender: 'male',
      pair: 'mizai-dayi',
    },
    {
      id: 'zh_female_mizaitongxue_v2_saturn_bigtts',
      label: '米仔同学（女声·清亮好奇）',
      gender: 'female',
      pair: 'mizai-dayi',
    },
  ],
}

export const useMetaStore = defineStore('meta', () => {
  const options = ref<OptionsPayload>(FALLBACK_OPTIONS)
  const health = ref<HealthPayload | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const loaded = ref(false)

  /** 后端返回的 llm / tts 是否为 mock */
  const modes = computed(() => health.value?.modes ?? null)
  const isMockBackend = computed(
    () => modes.value?.llm === 'mock' || modes.value?.tts === 'mock',
  )
  /** 顶栏角标：无论前端离线 mock 还是后端 mock，都提示「Mock 模式」 */
  const showMockBadge = computed(() => IS_MOCK || isMockBackend.value)
  const mockDetail = computed(() => {
    if (IS_MOCK) return '仅前端 · 内置示例数据'
    if (!health.value) return ''
    return `LLM：${health.value.modes.llm} · TTS：${health.value.modes.tts}`
  })
  const version = computed(() => health.value?.version ?? null)
  const maleVoices = computed(() => options.value.voices.filter((voice) => voice.gender === 'male'))
  const femaleVoices = computed(() =>
    options.value.voices.filter((voice) => voice.gender === 'female'),
  )
  const levels = computed<LevelOption[]>(() =>
    options.value.levels.length ? options.value.levels : FALLBACK_OPTIONS.levels,
  )

  async function load(force = false): Promise<void> {
    if (loading.value) return
    if (loaded.value && !force) return
    loading.value = true
    error.value = null
    const [optionsResult, healthResult] = await Promise.allSettled([getOptions(), getHealth()])
    if (optionsResult.status === 'fulfilled') {
      const value = optionsResult.value
      options.value = {
        durations: value.durations?.length ? value.durations : FALLBACK_OPTIONS.durations,
        levels: value.levels?.length ? value.levels : FALLBACK_OPTIONS.levels,
        voices: value.voices?.length ? value.voices : FALLBACK_OPTIONS.voices,
      }
    }
    if (healthResult.status === 'fulfilled') {
      health.value = healthResult.value
    }
    const failures = [optionsResult, healthResult].filter((item) => item.status === 'rejected')
    if (failures.length) {
      const first = failures[0] as PromiseRejectedResult
      error.value = IS_MOCK ? null : errorMessage(first.reason, '无法读取后端配置')
    }
    loaded.value = true
    loading.value = false
  }

  return {
    options,
    health,
    modes,
    version,
    loading,
    error,
    isMockBackend,
    showMockBadge,
    mockDetail,
    maleVoices,
    femaleVoices,
    levels,
    load,
  }
})
