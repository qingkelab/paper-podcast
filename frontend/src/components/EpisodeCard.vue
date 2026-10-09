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
    /** 这一集所属专辑的名字（列表页用 albums 接口补上；null = 不在任何专辑里） */
    albumTitle?: string | null
  }>(),
  { priority: false, albumTitle: null },
)

const emit = defineEmits<{
  (event: 'delete', episode: EpisodeSummary): void
  (event: 'retry', episode: EpisodeSummary): void
  /** 「加入专辑 / 移出专辑」（契约 §2.6）：弹窗由列表页统一管，卡片只负责发起 */
  (event: 'album', episode: EpisodeSummary): void
}>()

const isDone = computed(() => props.episode.status === 'completed')
/** 已经公开分享（契约 §2.7）：列表上给个角标，免得忘了哪一期是对外可看的 */
const isShared = computed(
  () => (props.episode.visibility ?? 'private') === 'public' && Boolean(props.episode.share_token),
)
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

    <!-- V2：归属与可见性，一眼看得出这一集在哪张专辑、有没有对外分享 -->
    <div v-if="isShared || albumTitle" class="episode-card__meta" style="margin-top: 8px">
      <span v-if="isShared" class="badge badge--completed">已分享</span>
      <span v-if="albumTitle" class="badge badge--accent">专辑 · {{ albumTitle }}</span>
    </div>

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
      <button type="button" class="btn btn--sm btn--ghost" @click="emit('album', episode)">
        {{ albumTitle ? '改专辑' : '加入专辑' }}
      </button>
      <span class="spacer" />
      <span v-if="episode.audio_bytes" class="file-pill__size">{{ formatBytes(episode.audio_bytes) }}</span>
      <button type="button" class="btn btn--sm btn--danger" @click="emit('delete', episode)">
        删除
      </button>
    </div>
  </article>
</template>
