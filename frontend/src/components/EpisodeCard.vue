<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { Episode } from '../api'
import StatusBadge from './StatusBadge.vue'
import { SOURCE_LABELS } from '../utils/stages'
import { formatBytes, formatDuration, formatRelative } from '../utils/format'

const props = defineProps<{
  episode: Episode
}>()

const emit = defineEmits<{
  (event: 'delete', episode: Episode): void
  (event: 'retry', episode: Episode): void
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
</script>

<template>
  <article class="episode-card">
    <div class="episode-card__top">
      <div style="min-width: 0">
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
