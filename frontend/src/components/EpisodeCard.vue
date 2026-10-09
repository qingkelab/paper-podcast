<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import type { EpisodeSummary } from '../api'
import StatusBadge from './StatusBadge.vue'
import { formatRelative } from '../utils/format'
import {
  durationText,
  isRunning,
  isShared,
  keywordsOf,
  languageLabels,
  metaParts,
  paperLine,
} from '../utils/episodeDisplay'

// 列表中后端不返回 figures / illustration，所以这里只依赖 EpisodeSummary
const props = withDefaults(
  defineProps<{
    episode: EpisodeSummary
    /** 首屏可见的卡片用 eager，别让 LCP 图卡在懒加载上 */
    priority?: boolean
    /** 这一集所属专辑的名字（列表页用 albums 接口补上；null = 不在任何专辑里） */
    albumTitle?: string | null
    /**
     * 专辑详情页里这一集本来就在这张专辑里，「加入专辑」是多余动作，
     * 而且那里没人监听这个事件 —— 按钮点了没反应比没有按钮更糟。
     */
    showAlbumAction?: boolean
  }>(),
  { priority: false, albumTitle: null, showAlbumAction: true },
)

const emit = defineEmits<{
  (event: 'delete', episode: EpisodeSummary): void
  (event: 'retry', episode: EpisodeSummary): void
  /** 「加入专辑 / 移出专辑」（契约 §2.6）：弹窗由列表页统一管，卡片只负责发起 */
  (event: 'album', episode: EpisodeSummary): void
}>()

const isDone = computed(() => props.episode.status === 'completed')
const shared = computed(() => isShared(props.episode))
const target = computed(() => ({
  name: isDone.value ? 'episode' : 'task',
  params: { id: props.episode.id },
}))

const meta = computed(() => metaParts(props.episode).join(' · '))
const paper = computed(() => paperLine(props.episode))
const languages = computed(() => languageLabels(props.episode))
// 2 个就够「这一集讲什么」了：3 个在 340px 宽的卡片里会换行，白占 35px
const keywords = computed(() => keywordsOf(props.episode, 2))
const when = computed(() => formatRelative(props.episode.created_at))
/** 悬停时能看全的完整时长说明（卡片上只显示 mm:ss） */
const durationHint = computed(() =>
  props.episode.audio_duration_sec ? `音频时长 ${durationText(props.episode)}` : `计划时长 ${durationText(props.episode)}`,
)

/** 封面可能为 null（如 text 来源），也可能 404：两种都退化成占位色块，绝不显示破图 */
const thumbFailed = ref(false)
const thumbUrl = computed(() => {
  if (thumbFailed.value) return null
  return props.episode.cover_url || null
})
</script>

<template>
  <article class="episode-card" :class="{ 'is-running': isRunning(props.episode) }">
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
        <!-- 状态与时间放标题**上方**：标题长度不可控，挤在同一行时
             长标题会把状态角标压成一团，短标题又让角标飘在中间 -->
        <div class="episode-card__kicker">
          <!-- 已完成不挂角标：库里绝大多数都是「已完成」，
               每条都喊一遍等于没有信息；只有进行中/失败才需要一眼看见 -->
          <StatusBadge v-if="!isDone" :status="episode.status" :label="episode.stage_label" />
          <span class="episode-card__when">{{ when }}</span>
        </div>
        <RouterLink :to="target" class="episode-card__title" :title="episode.title">
          {{ episode.title }}
        </RouterLink>
        <p class="episode-card__meta" :title="durationHint">{{ meta }}</p>
      </div>
    </div>

    <!-- 论文出处：作者 / 会议 / 年份 / arXiv 号。取不到整行不渲染，
         不留一条「—」占着位置 -->
    <p v-if="paper" class="episode-card__paper">{{ paper }}</p>

    <div v-if="keywords.length" class="episode-card__keywords">
      <span v-for="word in keywords" :key="word" class="chip chip--quiet">{{ word }}</span>
    </div>

    <!-- V2：归属与可见性，一眼看得出这一集在哪张专辑、有没有对外分享 -->
    <div v-if="shared || albumTitle || languages.length" class="episode-card__badges">
      <span v-for="name in languages" :key="name" class="badge badge--neutral">{{ name }}</span>
      <span v-if="shared" class="badge badge--completed">已分享</span>
      <span v-if="albumTitle" class="badge badge--accent">专辑 · {{ albumTitle }}</span>
    </div>

    <div v-if="isRunning(props.episode)" class="progress progress--thin">
      <div class="progress__bar" :style="{ width: `${episode.progress}%` }" />
    </div>

    <p v-if="episode.error" class="episode-card__error">{{ episode.error }}</p>

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
      <button
        v-if="props.showAlbumAction"
        type="button"
        class="btn btn--sm btn--ghost"
        @click="emit('album', episode)"
      >
        {{ albumTitle ? '改专辑' : '加入专辑' }}
      </button>
      <span class="spacer" />
      <button
        type="button"
        class="btn btn--sm btn--danger"
        title="删除这一集"
        @click="emit('delete', episode)"
      >
        删除
      </button>
    </div>
  </article>
</template>
