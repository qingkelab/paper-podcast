<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { errorMessage, getEpisode, isApiError, retryEpisode } from '../api'
import type { Episode } from '../api'
import StageTimeline from '../components/StageTimeline.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatDuration, formatRelative } from '../utils/format'
import { SOURCE_LABELS } from '../utils/stages'

const route = useRoute()
const router = useRouter()

const POLL_MS = 1500

const id = computed(() => String(route.params.id ?? ''))
const episode = ref<Episode | null>(null)
const error = ref<string | null>(null)
const notFound = ref(false)
const retrying = ref(false)

let timer: number | undefined
let inFlight = false
let navigated = false

const isFailed = computed(() => episode.value?.status === 'failed')
/** 失败原因既可能来自接口返回的 episode.error，也可能来自网络异常 */
const failureText = computed(() => episode.value?.error ?? error.value)
const progress = computed(() => episode.value?.progress ?? 0)
const stageLabel = computed(() => episode.value?.stage_label ?? '排队中')

const elapsed = computed(() => {
  const created = episode.value?.created_at
  if (!created) return '—'
  const started = Date.parse(created)
  if (Number.isNaN(started)) return '—'
  return formatDuration((Date.now() - started) / 1000)
})

function stopPolling(): void {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

function onCompleted(): void {
  stopPolling()
  if (navigated) return
  navigated = true
  // 让用户看到 100% 再跳转
  window.setTimeout(() => {
    void router.replace({ name: 'episode', params: { id: id.value } })
  }, 800)
}

async function poll(): Promise<void> {
  if (inFlight || !id.value) return
  inFlight = true
  try {
    const value = await getEpisode(id.value)
    episode.value = value
    error.value = null
    notFound.value = false
    if (value.status === 'completed') onCompleted()
    else if (value.status === 'failed') stopPolling()
  } catch (cause) {
    if (isApiError(cause) && cause.status === 404) {
      notFound.value = true
      error.value = cause.message
      stopPolling()
    } else {
      // 网络抖动不清空已有数据，等下一次轮询
      error.value = errorMessage(cause, '读取任务状态失败，正在重试…')
    }
  } finally {
    inFlight = false
  }
}

function startPolling(): void {
  stopPolling()
  if (!id.value) return
  void poll()
  timer = window.setInterval(() => {
    void poll()
  }, POLL_MS)
}

async function retry(): Promise<void> {
  if (!id.value) return
  retrying.value = true
  error.value = null
  try {
    episode.value = await retryEpisode(id.value)
    navigated = false
    startPolling()
  } catch (cause) {
    error.value = errorMessage(cause, '重新生成失败')
  } finally {
    retrying.value = false
  }
}

onMounted(() => {
  startPolling()
})

onBeforeUnmount(() => {
  stopPolling()
})

watch(id, () => {
  navigated = false
  startPolling()
})
</script>

<template>
  <div class="container page">
    <p class="eyebrow">Task · 生成进度</p>

    <div v-if="notFound" class="card card--pad">
      <div class="empty">
        <p class="empty__title">找不到这个任务</p>
        <p style="margin-bottom: 18px">它可能已经被删除，或者链接里的 id 不正确。</p>
        <RouterLink to="/library" class="btn btn--primary">回到播客库</RouterLink>
      </div>
    </div>

    <template v-else>
      <header class="row row--between" style="align-items: flex-start; margin-bottom: 22px">
        <div style="min-width: 0">
          <h1 class="page-title" style="margin-bottom: 10px">
            {{ episode?.title ?? '正在创建任务…' }}
          </h1>
          <div class="row" style="gap: 10px">
            <StatusBadge
              v-if="episode"
              :status="episode.status"
              :label="episode.stage_label"
            />
            <span v-else class="badge badge--neutral"><span class="badge__dot" />加载中</span>
            <span v-if="episode" class="section__hint">
              {{ SOURCE_LABELS[episode.source_type] }} · {{ episode.options.duration_min }} 分钟 ·
              创建于 {{ formatRelative(episode.created_at) }}
            </span>
          </div>
        </div>
        <RouterLink to="/library" class="btn btn--ghost">播客库</RouterLink>
      </header>

      <div class="card card--pad">
        <div class="row row--between" style="margin-bottom: 12px">
          <span class="analysis-card__title" :style="isFailed ? 'color: var(--danger)' : ''">
            {{ stageLabel }}
          </span>
          <span class="file-pill__size">{{ progress }}% · 已用时 {{ elapsed }}</span>
        </div>

        <div class="progress">
          <div
            class="progress__bar"
            :style="{ width: `${progress}%`, background: isFailed ? 'var(--danger)' : undefined }"
          />
        </div>

        <p class="section__hint" style="margin: 14px 0 0">
          每 1.5 秒自动刷新一次；任务完成后会自动跳转到播放页。
        </p>
      </div>

      <div v-if="failureText" class="alert alert--error" style="margin-top: 20px">
        <span class="alert__icon" aria-hidden="true">!</span>
        <span class="alert__body">
          <span v-if="isFailed" class="alert__title">生成失败</span>
          <span>{{ failureText }}</span>
        </span>
      </div>

      <div v-if="isFailed" class="row" style="margin-top: 16px">
        <button type="button" class="btn btn--primary" :disabled="retrying" @click="retry">
          <span v-if="retrying" class="spinner" aria-hidden="true" />
          {{ retrying ? '正在重新排队…' : '重新生成' }}
        </button>
        <RouterLink to="/" class="btn btn--ghost">换一篇论文</RouterLink>
      </div>

      <section class="section">
        <div class="section__head">
          <h2 class="section__title">阶段时间线</h2>
          <span class="section__hint">解析 → 深度解读 → 播客脚本 → 音频合成</span>
        </div>
        <div class="card card--pad">
          <StageTimeline
            :status="episode?.status ?? 'queued'"
            :progress="progress"
          />
        </div>
      </section>

      <section v-if="episode?.source_ref" class="section">
        <div class="section__head">
          <h2 class="section__title">来源</h2>
        </div>
        <div class="panel">
          <span class="episode-card__ref">{{ episode.source_ref }}</span>
        </div>
      </section>
    </template>
  </div>
</template>
