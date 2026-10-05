<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import type { EpisodeSummary } from '../api'
import StatusBadge from './StatusBadge.vue'
import { SOURCE_LABELS } from '../utils/stages'
import { formatBytes, formatDuration, formatRelative } from '../utils/format'

// 列表中后端不返回 figures / illustration，所以这里只依赖 EpisodeSummary
const props = withDefaults(
  defineProps<{
    episode: EpisodeSummary
    /** 首屏可见的卡片用 eager，别让 LCP 图卡在懒加载上 */
    priority?: boolean
  }>(),
  { priority: false },
)

const emit = defineEmits<{
  (event: 'delete', episode: EpisodeSummary): void
  (event: 'retry', episode: EpisodeSummary): void
}>()

const isDone = computed(() => props.episode.status === 'completed')
const target = computed(() => ({
  name: isDone.value ? 'episode' : 'task',
  params: { id: props.episode.id },
}))

const durationText = computed(() => {
  const seconds = props.episode.audio_duration_sec
  if (seconds) return formatDuration(seconds)
  return `${props.episode.options.duration_min} 分钟`
})

const refText = computed(() => props.episode.source_ref ?? '—')

/** 封面可能为 null（如 text 来源），也可能 404：两种都退化成占位色块，绝不显示破图 */
const thumbFailed = ref(false)
const thumbUrl = computed(() => {
  if (thumbFailed.value) return null
  return props.episode.cover_url || null
})
</script>

<template>
  <article class="episode-card">
    <div class="episode-card__top">
      <RouterLink
        :to="target"
        class="episode-card__thumb"
        :class="{ 'is-empty': !thumbUrl }"
        tabindex="-1"
        aria-hidden="true"
      >
        <img
          v-if="thumbUrl"
          :src="thumbUrl"
          alt=""
          :loading="props.priority ? 'eager' : 'lazy'"
          :fetchpriority="props.priority ? 'high' : 'auto'"
          decoding="async"
          @error="thumbFailed = true"
        />
        <span v-else class="episode-card__thumb-glyph">◫</span>
      </RouterLink>

      <div class="episode-card__head-text">
        <RouterLink :to="target" class="episode-card__title">{{ episode.title }}</RouterLink>
        <div class="episode-card__meta">
          <span>{{ SOURCE_LABELS[episode.source_type] }}</span>
          <span>{{ durationText }}</span>
          <span>{{ formatRelative(episode.created_at) }}</span>
        </div>
      </div>

      <StatusBadge :status="episode.status" :label="episode.stage_label" />
    </div>

    <div class="episode-card__ref" :title="refText">{{ refText }}</div>

    <div v-if="episode.status !== 'completed' && episode.status !== 'failed'" class="progress progress--thin">
      <div class="progress__bar" :style="{ width: `${episode.progress}%` }" />
    </div>

    <div v-if="episode.error" class="episode-card__ref" style="color: var(--danger)">
      {{ episode.error }}
    </div>

    <div class="episode-card__actions">
      <RouterLink :to="target" class="btn btn--sm btn--primary">
        {{ isDone ? '播放与解读' : '查看进度' }}
      </RouterLink>
      <button
        v-if="episode.status === 'failed'"
        type="button"
        class="btn btn--sm"
        @click="emit('retry', episode)"
      >
        重新生成
      </button>
      <span class="spacer" />
      <span v-if="episode.audio_bytes" class="file-pill__size">{{ formatBytes(episode.audio_bytes) }}</span>
      <button type="button" class="btn btn--sm btn--danger" @click="emit('delete', episode)">
        删除
      </button>
    </div>
  </article>
</template>
