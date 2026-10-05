<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { deleteEpisode, errorMessage, listEpisodes, retryEpisode } from '../api'
import type { EpisodeStatus, EpisodeSummary } from '../api'
import EpisodeCard from '../components/EpisodeCard.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { FILTERABLE_STATUSES, STATUS_LABELS } from '../utils/stages'

const PAGE_SIZE = 12
const AUTO_REFRESH_MS = 5000

// 列表接口返回的是 EpisodeSummary（不含 analysis / script / figures / illustration）
const items = ref<EpisodeSummary[]>([])
const total = ref(0)
const loading = ref(true)
const loadingMore = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const keyword = ref('')
const status = ref<EpisodeStatus | 'all'>('all')
const confirmTarget = ref<EpisodeSummary | null>(null)
const deleting = ref(false)
const refreshing = ref(false)

let refreshTimer: number | undefined
let debounceTimer: number | undefined
let requestSeq = 0

const hasMore = computed(() => items.value.length < total.value)
const hasActiveTask = computed(() =>
  items.value.some((episode) => episode.status !== 'completed' && episode.status !== 'failed'),
)

function params(offset: number, limit: number) {
  return {
    offset,
    limit,
    status: status.value === 'all' ? undefined : status.value,
    q: keyword.value.trim() || undefined,
  }
}

async function fetchPage(offset: number, limit: number, silent = false): Promise<void> {
  // 用序号丢弃过期响应：静默刷新与用户操作可以并发，只有最新一次的返回会被采用
  const seq = ++requestSeq
  if (!silent) {
    error.value = null
    if (offset === 0) loading.value = true
    else loadingMore.value = true
  }
  try {
    const result = await listEpisodes(params(offset, limit))
    if (seq !== requestSeq) return
    if (offset === 0) items.value = result.items
    else items.value = [...items.value, ...result.items]
    total.value = result.total
    error.value = null
  } catch (cause) {
    if (seq !== requestSeq) return
    error.value = errorMessage(cause, '读取播客库失败')
  } finally {
    if (seq === requestSeq) {
      loading.value = false
      loadingMore.value = false
      refreshing.value = false
      syncAutoRefresh()
    }
  }
}

function currentLimit(): number {
  return Math.max(items.value.length, PAGE_SIZE)
}

async function reload(): Promise<void> {
  await fetchPage(0, currentLimit())
}

async function loadMore(): Promise<void> {
  await fetchPage(items.value.length, PAGE_SIZE)
}

async function manualRefresh(): Promise<void> {
  refreshing.value = true
  await reload()
}

function syncAutoRefresh(): void {
  if (hasActiveTask.value && refreshTimer === undefined) {
    refreshTimer = window.setInterval(() => {
      if (document.hidden) return
      void fetchPage(0, currentLimit(), true)
    }, AUTO_REFRESH_MS)
  } else if (!hasActiveTask.value && refreshTimer !== undefined) {
    window.clearInterval(refreshTimer)
    refreshTimer = undefined
  }
}

function onConfirmDelete(event: EpisodeSummary): void {
  confirmTarget.value = event
}

async function confirmDelete(): Promise<void> {
  const target = confirmTarget.value
  if (!target) return
  deleting.value = true
  try {
    await deleteEpisode(target.id)
    confirmTarget.value = null
    notice.value = `已删除《${target.title}》`
    await reload()
  } catch (cause) {
    error.value = errorMessage(cause, '删除失败')
    confirmTarget.value = null
  } finally {
    deleting.value = false
  }
}

async function onRetry(episode: EpisodeSummary): Promise<void> {
  notice.value = null
  try {
    await retryEpisode(episode.id)
    notice.value = `已把《${episode.title}》重新排队`
    await reload()
  } catch (cause) {
    error.value = errorMessage(cause, '重新生成失败')
  }
}

function clearFilters(): void {
  keyword.value = ''
  status.value = 'all'
}

watch([keyword, status], () => {
  if (debounceTimer !== undefined) window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(() => {
    void fetchPage(0, PAGE_SIZE)
  }, 320)
})

onMounted(() => {
  void fetchPage(0, PAGE_SIZE)
})

onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
  if (debounceTimer !== undefined) window.clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="container page">
    <header class="row row--between" style="align-items: flex-end; margin-bottom: 26px">
      <div>
        <p class="eyebrow">Library · 播客库</p>
        <h1 class="page-title" style="margin-bottom: 6px">已生成的播客</h1>
        <p class="page-subtitle" style="margin: 0">
          共 {{ total }} 条<template v-if="items.length">，当前显示 {{ items.length }} 条</template>
        </p>
      </div>
      <div class="row">
        <button type="button" class="btn btn--ghost" :disabled="refreshing" @click="manualRefresh">
          <span v-if="refreshing" class="spinner" aria-hidden="true" />
          {{ refreshing ? '刷新中…' : '刷新' }}
        </button>
        <RouterLink to="/" class="btn btn--primary">导入新论文</RouterLink>
      </div>
    </header>

    <div class="card card--pad" style="margin-bottom: 24px">
      <div class="form-grid">
        <div class="field">
          <label class="field__label" for="search">按标题搜索</label>
          <input
            id="search"
            v-model="keyword"
            class="input"
            type="search"
            placeholder="例如：Attention / LoRA"
          />
        </div>
        <div class="field">
          <label class="field__label" for="status-filter">按状态筛选</label>
          <select id="status-filter" v-model="status" class="select">
            <option value="all">全部状态</option>
            <option v-for="item in FILTERABLE_STATUSES" :key="item" :value="item">
              {{ STATUS_LABELS[item] }}
            </option>
          </select>
        </div>
      </div>
      <p v-if="hasActiveTask" class="section__hint" style="margin: 14px 0 0">
        有任务正在生成，页面每 5 秒会自动刷新一次状态。
      </p>
    </div>

    <div v-if="notice" class="alert alert--info" style="margin-bottom: 20px">
      <span class="alert__icon" aria-hidden="true">i</span>
      <span class="alert__body">{{ notice }}</span>
    </div>

    <div v-if="error" class="alert alert--error" style="margin-bottom: 20px">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">
        {{ error }}
        <button type="button" class="btn btn--sm btn--ghost" style="margin-left: 10px" @click="reload">
          重试
        </button>
      </span>
    </div>

    <div v-if="loading" class="episode-grid">
      <div v-for="index in 3" :key="index" class="episode-card">
        <div class="row" style="align-items: flex-start; flex-wrap: nowrap; gap: 14px">
          <div class="skeleton skeleton--thumb" />
          <div style="min-width: 0; flex: 1">
            <div class="skeleton" style="width: 74%; height: 20px; margin-bottom: 10px" />
            <div class="skeleton" style="width: 52%" />
          </div>
        </div>
        <div class="skeleton" style="width: 88%" />
      </div>
    </div>

    <div v-else-if="!items.length" class="empty">
      <template v-if="keyword || status !== 'all'">
        <p class="empty__title">没有符合条件的播客</p>
        <p style="margin-bottom: 18px">换个关键词，或把状态筛选改回「全部状态」。</p>
        <button type="button" class="btn btn--ghost" @click="clearFilters">清除筛选</button>
      </template>
      <template v-else>
        <p class="empty__title">播客库还是空的</p>
        <p style="margin-bottom: 18px">导入第一篇论文，几分钟后这里就会出现一条双人播客。</p>
        <RouterLink to="/" class="btn btn--primary">去导入论文</RouterLink>
      </template>
    </div>

    <template v-else>
      <div class="episode-grid">
        <EpisodeCard
          v-for="(episode, index) in items"
          :key="episode.id"
          :episode="episode"
          :priority="index < 3"
          @delete="onConfirmDelete"
          @retry="onRetry"
        />
      </div>

      <div class="row" style="justify-content: center; margin-top: 30px">
        <button
          v-if="hasMore"
          type="button"
          class="btn"
          :disabled="loadingMore"
          @click="loadMore"
        >
          <span v-if="loadingMore" class="spinner" aria-hidden="true" />
          {{ loadingMore ? '加载中…' : `加载更多（还有 ${total - items.length} 条）` }}
        </button>
        <span v-else class="section__hint">已经到底了 · 共 {{ total }} 条</span>
      </div>
    </template>

    <ConfirmDialog
      :open="confirmTarget !== null"
      title="删除这条播客？"
      :text="`《${confirmTarget?.title ?? ''}》的脚本、解读与音频都会被一起删掉，且无法恢复。`"
      confirm-text="删除"
      danger
      :busy="deleting"
      @confirm="confirmDelete"
      @cancel="confirmTarget = null"
    />
  </div>
</template>
