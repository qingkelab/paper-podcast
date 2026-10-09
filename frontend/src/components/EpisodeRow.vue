<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import type { EpisodeSummary } from '../api'
import StatusBadge from './StatusBadge.vue'
import { formatDateTime, formatRelative } from '../utils/format'
import {
  isRunning,
  isShared,
  languageLabels,
  metaParts,
  paperLine,
} from '../utils/episodeDisplay'

/**
 * 播客库的**列表视图**：一行一条。
 *
 * 为什么要有它：卡片视图一行只放得下 3 条，攒到十几集以后「找到上周那期」
 * 全靠滚动；而且卡片里为了好看留了很多空白。列表视图把每条压到一行、
 * 关键信息对齐成固定列，一屏能扫十几条 —— 两者解决的是不同场景，
 * 所以做成可切换而不是互相取代。
 */
const props = withDefaults(
  defineProps<{
    episode: EpisodeSummary
    /** 所属专辑名字（列表页用 albums 接口补上） */
    albumTitle?: string | null
    /** 专辑详情页里不显示「加入专辑」（那一集本来就在这张专辑里） */
    showAlbumAction?: boolean
  }>(),
  { albumTitle: null, showAlbumAction: true },
)

const emit = defineEmits<{
  (event: 'delete', episode: EpisodeSummary): void
  (event: 'retry', episode: EpisodeSummary): void
  (event: 'album', episode: EpisodeSummary): void
}>()

const isDone = computed(() => props.episode.status === 'completed')
const shared = computed(() => isShared(props.episode))
const target = computed(() => ({
  name: isDone.value ? 'episode' : 'task',
  params: { id: props.episode.id },
}))

/**
 * 元信息**压成一行**：来源 · 难度 · 时长 · 体积 · 语言 · 作者/年份/arXiv 号 · 专辑。
 *
 * 列表视图的价值就是「一屏扫十几条」，所以宁可这一行超长省略、
 * 悬停看全（`title`），也不为了「一行放不下就换行」把每条撑到 3 行 ——
 * 那样它和卡片视图就没区别了。
 */
const meta = computed(() => {
  const parts = metaParts(props.episode)
  const languages = languageLabels(props.episode)
  if (languages.length) parts.push(languages.join(' + '))
  const paper = paperLine(props.episode)
  if (paper) parts.push(paper)
  if (shared.value) parts.push('已分享')
  if (props.albumTitle) parts.push(`专辑 · ${props.albumTitle}`)
  return parts.join(' · ')
})

const thumbFailed = ref(false)
const thumbUrl = computed(() => (thumbFailed.value ? null : props.episode.cover_url || null))
</script>

<template>
  <article class="episode-row" :class="{ 'is-running': isRunning(props.episode) }">
    <RouterLink
      :to="target"
      class="episode-row__thumb"
      :class="{ 'is-empty': !thumbUrl }"
      tabindex="-1"
      aria-hidden="true"
    >
      <img
        v-if="thumbUrl"
        :src="thumbUrl"
        alt=""
        loading="lazy"
        decoding="async"
        @error="thumbFailed = true"
      />
      <span v-else class="episode-row__thumb-glyph">◫</span>
    </RouterLink>

    <div class="episode-row__main">
      <div class="episode-row__head">
        <!-- 同卡片：只有进行中/失败才挂角标 -->
        <StatusBadge v-if="!isDone" :status="episode.status" :label="episode.stage_label" />
        <RouterLink :to="target" class="episode-row__title" :title="episode.title">
          {{ episode.title }}
        </RouterLink>
      </div>
      <p class="episode-row__meta" :title="meta">{{ meta }}</p>
      <div v-if="isRunning(props.episode)" class="progress progress--thin">
        <div class="progress__bar" :style="{ width: `${episode.progress}%` }" />
      </div>
      <p v-if="episode.error" class="episode-row__error">{{ episode.error }}</p>
    </div>

    <div class="episode-row__side">
      <span class="episode-row__when" :title="formatDateTime(episode.created_at)">
        {{ formatRelative(episode.created_at) }}
      </span>
      <div class="episode-row__actions">
        <RouterLink :to="target" class="btn btn--sm btn--primary">
          {{ isDone ? '播放' : '进度' }}
        </RouterLink>
        <button
          v-if="episode.status === 'failed'"
          type="button"
          class="btn btn--sm"
          @click="emit('retry', episode)"
        >
          重试
        </button>
        <button
          v-if="props.showAlbumAction"
          type="button"
          class="btn btn--sm btn--ghost"
          @click="emit('album', episode)"
        >
          {{ albumTitle ? '改专辑' : '加入专辑' }}
        </button>
        <button
          type="button"
          class="btn btn--sm btn--danger"
          title="删除这一集"
          @click="emit('delete', episode)"
        >
          删除
        </button>
      </div>
    </div>
  </article>
</template>
