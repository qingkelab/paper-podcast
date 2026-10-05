<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  IS_MOCK,
  analysisMdUrl,
  audioUrl,
  downloadUrl,
  errorMessage,
  getEpisode,
  isApiError,
  retryEpisode,
  scriptTxtUrl,
} from '../api'
import type { Episode } from '../api'
import AudioPlayer from '../components/AudioPlayer.vue'
import AnalysisView from '../components/AnalysisView.vue'
import ScriptView from '../components/ScriptView.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatBytes, formatDateTime, formatDuration } from '../utils/format'
import { LEVEL_LABELS, SOURCE_LABELS } from '../utils/stages'

const route = useRoute()
const router = useRouter()

const id = computed(() => String(route.params.id ?? ''))
const episode = ref<Episode | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const notFound = ref(false)
const retrying = ref(false)
const downloadError = ref<string | null>(null)

let audioRetryTimer: number | undefined

const isCompleted = computed(() => episode.value?.status === 'completed')
const isFailed = computed(() => episode.value?.status === 'failed')
const paper = computed(() => episode.value?.paper_meta ?? null)
const audioSrc = computed(() => (episode.value ? audioUrl(episode.value) : null))
const arxivUrl = computed(() =>
  paper.value?.arxiv_id ? `https://arxiv.org/abs/${paper.value.arxiv_id}` : null,
)
/** 失败原因既可能来自 episode.error，也可能来自网络异常 */
const failureText = computed(() => episode.value?.error ?? error.value)

const metaItems = computed(() => {
  const value = paper.value
  if (!value) return []
  const items: Array<{ label: string; value: string }> = []
  if (value.authors?.length) items.push({ label: '作者', value: value.authors.join('、') })
  if (value.year) items.push({ label: '年份', value: String(value.year) })
  if (value.venue) items.push({ label: '会议 / 期刊', value: value.venue })
  if (value.arxiv_id) items.push({ label: 'arXiv', value: value.arxiv_id })
  return items
})

function scheduleAudioRefetch(): void {
  if (audioRetryTimer !== undefined) return
  // Mock 模式下音频是异步合成的，还没好就再拉一次
  audioRetryTimer = window.setTimeout(() => {
    audioRetryTimer = undefined
    void load(true)
  }, 1200)
}

async function load(silent = false): Promise<void> {
  if (!silent) loading.value = true
  error.value = null
  try {
    const value = await getEpisode(id.value)
    episode.value = value
    notFound.value = false
    if (value.status === 'completed') {
      if (!value.audio_url) scheduleAudioRefetch()
    } else if (value.status !== 'failed') {
      // 还在生成中：直接去进度页看轮询
      void router.replace({ name: 'task', params: { id: id.value } })
    }
  } catch (cause) {
    if (isApiError(cause) && cause.status === 404) {
      notFound.value = true
      error.value = cause.message
    } else {
      error.value = errorMessage(cause, '读取播客详情失败')
    }
  } finally {
    loading.value = false
  }
}

function safeName(): string {
  const title = episode.value?.title ?? 'paper-podcast'
  return title.replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 60) || 'paper-podcast'
}

function downloadScript(): void {
  downloadError.value = null
  const url = scriptTxtUrl(id.value)
  if (!url) {
    downloadError.value = '脚本还没有生成，暂时无法下载'
    return
  }
  downloadUrl(url, `${safeName()}-脚本.txt`)
}

function downloadAnalysis(): void {
  downloadError.value = null
  const url = analysisMdUrl(id.value)
  if (!url) {
    downloadError.value = '结构化解读还没有生成，暂时无法下载'
    return
  }
  downloadUrl(url, `${safeName()}-解读.md`)
}

async function retry(): Promise<void> {
  retrying.value = true
  error.value = null
  try {
    await retryEpisode(id.value)
    await router.replace({ name: 'task', params: { id: id.value } })
  } catch (cause) {
    error.value = errorMessage(cause, '重新生成失败')
  } finally {
    retrying.value = false
  }
}

function scrollToSection(section: 'script' | 'analysis'): void {
  document.getElementById(`section-${section}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

onMounted(() => {
  void load()
})

onBeforeUnmount(() => {
  if (audioRetryTimer !== undefined) window.clearTimeout(audioRetryTimer)
})
</script>

<template>
  <div class="container page">
    <div v-if="loading && !episode" class="card card--pad">
      <div class="skeleton" style="width: 55%; height: 26px; margin-bottom: 16px" />
      <div class="skeleton" style="width: 30%; margin-bottom: 26px" />
      <div class="skeleton" style="width: 100%; height: 92px" />
    </div>

    <div v-else-if="notFound" class="card card--pad">
      <div class="empty">
        <p class="empty__title">找不到这条播客</p>
        <p style="margin-bottom: 18px">{{ error }}</p>
        <RouterLink to="/library" class="btn btn--primary">回到播客库</RouterLink>
      </div>
    </div>

    <template v-else-if="episode">
      <header style="margin-bottom: 26px">
        <p class="eyebrow">Episode · 播客详情</p>
        <div class="row row--between" style="align-items: flex-start">
          <div style="min-width: 0; max-width: 70ch">
            <h1 class="page-title" style="margin-bottom: 12px">{{ episode.title }}</h1>
            <div class="row" style="gap: 12px">
              <StatusBadge :status="episode.status" :label="episode.stage_label" />
              <span class="section__hint">
                {{ SOURCE_LABELS[episode.source_type] }} · 目标 {{ episode.options.duration_min }} 分钟 ·
                {{ LEVEL_LABELS[episode.options.level] }} · 生成于 {{ formatDateTime(episode.created_at) }}
              </span>
            </div>
          </div>
          <RouterLink to="/library" class="btn btn--ghost">播客库</RouterLink>
        </div>
      </header>

      <div v-if="failureText" class="alert alert--error" style="margin-bottom: 20px">
        <span class="alert__icon" aria-hidden="true">!</span>
        <span class="alert__body">
          <span v-if="isFailed" class="alert__title">这条播客生成失败</span>
          <span>{{ failureText }}</span>
        </span>
      </div>

      <div v-if="isFailed" class="row" style="margin-bottom: 22px">
        <button type="button" class="btn btn--primary" :disabled="retrying" @click="retry">
          <span v-if="retrying" class="spinner" aria-hidden="true" />
          {{ retrying ? '正在重新排队…' : '重新生成' }}
        </button>
        <RouterLink :to="{ name: 'task', params: { id } }" class="btn btn--ghost">
          查看生成进度
        </RouterLink>
      </div>

      <div v-if="IS_MOCK" class="alert alert--accent" style="margin-bottom: 22px">
        <span class="alert__icon" aria-hidden="true">◈</span>
        <span class="alert__body">
          <span class="alert__title">离线 Mock 模式</span>
          本页的解读与脚本来自内置示例数据；音频由 Web Audio API 现场合成的正弦波生成（约 36 秒），
          用于验证播放器、倍速与拖动进度条。
        </span>
      </div>

      <template v-if="isCompleted">
        <AudioPlayer
          :src="audioSrc"
          :fallback-duration="episode.audio_duration_sec"
          :title="episode.title"
        />

        <div class="row" style="margin-top: 18px; gap: 10px">
          <button type="button" class="btn btn--sm" @click="downloadScript">↓ 下载脚本（txt）</button>
          <button type="button" class="btn btn--sm" @click="downloadAnalysis">↓ 下载解读（md）</button>
          <button type="button" class="btn btn--sm btn--ghost" @click="scrollToSection('script')">
            跳到脚本
          </button>
          <button type="button" class="btn btn--sm btn--ghost" @click="scrollToSection('analysis')">
            跳到解读
          </button>
          <span class="spacer" />
          <span v-if="episode.audio_bytes" class="file-pill__size">
            音频 {{ formatBytes(episode.audio_bytes) }} · 时长
            {{ formatDuration(episode.audio_duration_sec) }}
          </span>
        </div>

        <p v-if="downloadError" class="section__hint" style="color: var(--danger); margin-top: 10px">
          {{ downloadError }}
        </p>
      </template>

      <section v-else class="card card--pad" style="margin-bottom: 22px">
        <div class="row row--between">
          <div>
            <p class="analysis-card__title">{{ episode.stage_label }}（{{ episode.progress }}%）</p>
            <p class="section__hint" style="margin: 6px 0 0">
              这条播客还没有生成完成，音频与脚本暂时不可用。
            </p>
          </div>
          <RouterLink :to="{ name: 'task', params: { id } }" class="btn btn--primary">
            去进度页
          </RouterLink>
        </div>
      </section>

      <section v-if="paper" class="section" style="margin-top: 34px">
        <div class="section__head">
          <h2 class="section__title">论文元信息</h2>
          <span class="section__hint">{{ episode.source_ref ?? '—' }}</span>
        </div>
        <div class="card card--pad">
          <div class="meta-grid">
            <div class="meta-item">
              <div class="meta-item__label">论文标题</div>
              <div class="meta-item__value">{{ paper.title ?? episode.title }}</div>
            </div>
            <div v-for="item in metaItems" :key="item.label" class="meta-item">
              <div class="meta-item__label">{{ item.label }}</div>
              <div class="meta-item__value">
                <a
                  v-if="item.label === 'arXiv' && arxivUrl"
                  :href="arxivUrl"
                  target="_blank"
                  rel="noopener"
                >
                  {{ item.value }} ↗
                </a>
                <template v-else>{{ item.value }}</template>
              </div>
            </div>
          </div>

          <template v-if="paper.keywords?.length">
            <hr class="divider" />
            <div class="tag-list">
              <span v-for="keyword in paper.keywords" :key="keyword" class="tag">{{ keyword }}</span>
            </div>
          </template>

          <template v-if="paper.abstract">
            <hr class="divider" />
            <div class="meta-item__label">摘要</div>
            <p class="analysis-card__body" style="margin: 6px 0 0">{{ paper.abstract }}</p>
          </template>
        </div>
      </section>

      <section id="section-script" class="section">
        <div class="section__head">
          <h2 class="section__title">播客脚本</h2>
          <span class="section__hint">双人对谈 · 逐段展示，按说话人配色</span>
        </div>
        <div class="card card--pad">
          <ScriptView
            :script="episode.script"
            :voice-a="episode.options.voice_a"
            :voice-b="episode.options.voice_b"
          />
        </div>
      </section>

      <section id="section-analysis" class="section">
        <div class="section__head">
          <h2 class="section__title">结构化解读</h2>
          <span class="section__hint">
            背景 → 创新点 → 方法 → 实验 → 结论 → 不足 → 价值 → 未来
          </span>
        </div>
        <AnalysisView :analysis="episode.analysis" />
      </section>
    </template>
  </div>
</template>
