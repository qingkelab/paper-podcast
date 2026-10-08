<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Script } from '../api'
import { useMetaStore } from '../stores/meta'
import { formatDuration } from '../utils/format'

const props = defineProps<{
  script: Script | null
  voiceA?: string | null
  voiceB?: string | null
  /**
   * 这一版脚本的语言。`word_count` 是「时长预算的单位数」：
   * 中文数字符、英文数词，所以单位文案必须跟着换 ——
   * 英文版显示「427 字」是错的（那是 427 个词）。
   */
  language?: 'zh' | 'en'
}>()

const meta = useMetaStore()
const filter = ref<'all' | 'A' | 'B'>('all')

/** 字数单位。界面本身是中文的，所以英文版也用「词」而不是 words。 */
const unit = computed(() => (props.language === 'en' ? '词' : '字'))

/** 从 options 里把音色 id 换成短名字，例如「大义先生」 */
function shortVoiceName(id: string | null | undefined, fallback: string): string {
  if (!id) return fallback
  const voice = meta.options.voices.find((item) => item.id === id)
  if (!voice) return fallback
  return voice.label.split('（')[0] ?? fallback
}

const nameA = computed(() => shortVoiceName(props.voiceA, '主播A'))
const nameB = computed(() => shortVoiceName(props.voiceB, '主播B'))

const segments = computed(() => props.script?.segments ?? [])

const visible = computed(() =>
  filter.value === 'all'
    ? segments.value
    : segments.value.filter((segment) => segment.speaker === filter.value),
)

const stats = computed(() => {
  const script = props.script
  if (!script) return null
  const chars = script.segments.reduce((total, segment) => total + segment.text.length, 0)
  const aCount = script.segments.filter((segment) => segment.speaker === 'A').length
  return {
    rounds: Math.ceil(script.segments.length / 2),
    segments: script.segments.length,
    chars,
    wordCount: script.word_count,
    aCount,
    bCount: script.segments.length - aCount,
    est: formatDuration(script.est_duration_sec),
  }
})

function speakerName(speaker: string): string {
  return speaker === 'A' ? nameA.value : nameB.value
}
</script>

<template>
  <div>
    <div class="row row--between" style="margin-bottom: 16px">
      <div class="row" role="group" aria-label="按说话人筛选">
        <button
          type="button"
          class="chip"
          :class="{ 'is-active': filter === 'all' }"
          @click="filter = 'all'"
        >
          全部（{{ segments.length }}）
        </button>
        <button
          type="button"
          class="chip"
          :class="{ 'is-active': filter === 'A' }"
          @click="filter = 'A'"
        >
          {{ nameA }}（{{ stats?.aCount ?? 0 }}）
        </button>
        <button
          type="button"
          class="chip"
          :class="{ 'is-active': filter === 'B' }"
          @click="filter = 'B'"
        >
          {{ nameB }}（{{ stats?.bCount ?? 0 }}）
        </button>
      </div>

      <div v-if="stats" class="episode-card__meta">
        <span>{{ stats.segments }} 段</span>
        <span>约 {{ stats.wordCount }} {{ unit }}</span>
        <span>预估 {{ stats.est }}</span>
      </div>
    </div>

    <div v-if="!segments.length" class="empty">
      <p class="empty__title">脚本还没有生成</p>
      <p style="margin: 0">任务完成后即可在这里看到双人对谈脚本。</p>
    </div>

    <div v-else class="script">
      <article
        v-for="segment in visible"
        :key="`${segment.round}-${segment.speaker}`"
        class="segment"
        :class="segment.speaker === 'A' ? 'segment--a' : 'segment--b'"
      >
        <header class="segment__head">
          <span class="segment__speaker">
            <span class="segment__avatar" aria-hidden="true">
              {{ segment.speaker === 'A' ? '甲' : '乙' }}
            </span>
            {{ segment.speaker === 'A' ? nameA : nameB }}
          </span>
          <span class="segment__round">第 {{ segment.round + 1 }} 段 / 主播{{ segment.speaker }}</span>
        </header>
        <p class="segment__text">{{ segment.text }}</p>
      </article>
    </div>

    <p v-if="segments.length" class="section__hint" style="margin-top: 18px">
      主播A（{{ speakerName('A') }}）负责主讲，主播B（{{ speakerName('B') }}）负责提问与复述要点。
    </p>
  </div>
</template>
