<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import type { Episode, Figure } from '../api'
import AudioPlayer from '../components/AudioPlayer.vue'
import AnalysisView from '../components/AnalysisView.vue'
import FigureLightbox from '../components/FigureLightbox.vue'
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

// ---------------------------------------------------------------------------
// 封面（hero）：按 cover_width / cover_height 预留宽高比，加载时不会跳布局
// ---------------------------------------------------------------------------

const coverFailed = ref(false)
const coverLoaded = ref(false)
const coverSrc = computed(() => {
  if (coverFailed.value) return null
  return episode.value?.cover_url || null
})
/** 契约保证这两个字段可同时为 null；缺失时退回 A4 纸的比例（0.773），照样预留得住 */
const coverRatio = computed(() => {
  const width = episode.value?.cover_width
  const height = episode.value?.cover_height
  if (width && height) return `${width} / ${height}`
  return '1 / 1.294'
})

// ---------------------------------------------------------------------------
// 生成信息图：必须用 <object type="image/svg+xml"> 才能播 SMIL 动画
// ---------------------------------------------------------------------------

const illustration = computed(() => episode.value?.illustration ?? null)
const illustrationSvg = computed(() => illustration.value?.svg_url || null)
const illustrationPng = computed(() => illustration.value?.png_url || null)
const illustrationRatio = computed(() => {
  const width = illustration.value?.width ?? 16
  const height = illustration.value?.height ?? 9
  return `${width} / ${height}`
})

function downloadIllustration(): void {
  const url = illustrationPng.value
  if (!url) return
  downloadUrl(url, `${safeName()}-信息图.png`)
}

// ---------------------------------------------------------------------------
// 论文原图画廊 + 灯箱
// ---------------------------------------------------------------------------

const figures = computed<Figure[]>(() => episode.value?.figures ?? [])
const lightboxIndex = ref<number | null>(null)
/** 加载失败的图：退化成占位卡，不留破图 */
const brokenFigures = ref<string[]>([])

function isBroken(figure: Figure): boolean {
  return brokenFigures.value.includes(figure.id)
}

function markBroken(figureId: string): void {
  if (!brokenFigures.value.includes(figureId)) brokenFigures.value = [...brokenFigures.value, figureId]
}

function openFigure(index: number): void {
  lightboxIndex.value = index
}

function navigateFigure(delta: number): void {
  const count = figures.value.length
  const current = lightboxIndex.value
  if (!count || current === null) return
  lightboxIndex.value = (current + delta + count) % count
}

// /episode/:id → /episode/:other 命中同一个路由记录，组件会被复用、onMounted 不会重跑。
// 不监听 id 的话，页面会继续显示上一集的封面与配图（URL 已经变了），所以这里必须重新拉数据。
watch(id, () => {
  coverFailed.value = false
  coverLoaded.value = false
  lightboxIndex.value = null
  brokenFigures.value = []
  episode.value = null
  void load()
})

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

    <!-- 非 404 的读取失败（后端没起来 / 网络抖动）：给个明确的重试入口，不要留白屏 -->
    <div v-else-if="error" class="card card--pad">
      <div class="empty">
        <p class="empty__title">暂时读不到这条播客</p>
        <p style="margin-bottom: 18px">{{ error }}</p>
        <div class="row" style="justify-content: center">
          <button type="button" class="btn btn--primary" @click="load()">重新加载</button>
          <RouterLink to="/library" class="btn btn--ghost">回到播客库</RouterLink>
        </div>
      </div>
    </div>

    <template v-else-if="episode">
      <!-- 封面 hero：放在标题上方。cover_url 为 null 或加载失败时整块不渲染（不留空白、不留破图） -->
      <figure v-if="coverSrc" class="hero">
        <div class="hero__art" :style="{ aspectRatio: coverRatio }">
          <img class="hero__glow" :src="coverSrc" alt="" aria-hidden="true" decoding="async" />
          <img
            class="hero__img"
            :class="{ 'is-loaded': coverLoaded }"
            :src="coverSrc"
            :alt="`《${episode.title}》论文首页渲染图`"
            decoding="async"
            @load="coverLoaded = true"
            @error="coverFailed = true"
          />
        </div>
        <figcaption class="hero__caption">
          <span class="badge badge--neutral">论文首页</span>
          <span class="section__hint">
            封面取自 PDF 第一页整页渲染<template v-if="episode.cover_width && episode.cover_height">
              · {{ episode.cover_width }}×{{ episode.cover_height }}</template
            >
          </span>
        </figcaption>
      </figure>

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
          封面、论文原图与信息图也全部由 Canvas / 内联 SVG 现场生成，不请求任何网络资源。
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

      <!-- 模型生成的信息图：用 <object> 引用，SVG 里的 SMIL 动画才会播放；内嵌 <img> 作降级 -->
      <section v-if="illustration" class="section">
        <div class="section__head">
          <h2 class="section__title">生成信息图</h2>
          <span class="section__hint">
            模型把这篇论文提炼成一页图解<template v-if="illustration.source === 'fallback'">
              （后端兜底模板）</template
            >
          </span>
        </div>
        <div class="illus card card--pad">
          <div class="illus__stage" :style="{ aspectRatio: illustrationRatio }">
            <object
              v-if="illustrationSvg"
              class="illus__media"
              :data="illustrationSvg"
              type="image/svg+xml"
              :aria-label="`《${episode.title}》生成信息图`"
            >
              <img
                v-if="illustrationPng"
                class="illus__media"
                :src="illustrationPng"
                :alt="`《${episode.title}》生成信息图（静态 PNG 降级）`"
              />
            </object>
            <img
              v-else-if="illustrationPng"
              class="illus__media"
              :src="illustrationPng"
              :alt="`《${episode.title}》生成信息图`"
            />
          </div>
          <div class="row illus__foot">
            <span class="badge badge--accent">SMIL 动画</span>
            <span class="section__hint">
              用 <code>&lt;object type="image/svg+xml"&gt;</code> 引用，SVG 内的动画才会播放
              （用 <code>&lt;img&gt;</code> 引用时部分浏览器不会跑）
            </span>
            <span class="spacer" />
            <span v-if="illustration.width && illustration.height" class="file-pill__size">
              {{ illustration.width }}×{{ illustration.height }}
            </span>
            <button
              v-if="illustrationPng"
              type="button"
              class="btn btn--sm btn--ghost"
              @click="downloadIllustration"
            >
              ↓ 下载 PNG
            </button>
          </div>
        </div>
      </section>

      <!-- 论文原图画廊：figures 为空数组时整块不渲染 -->
      <section v-if="figures.length" class="section">
        <div class="section__head">
          <h2 class="section__title">论文原图</h2>
          <span class="section__hint">
            从 PDF 按「Figure N:」图注提取 · 共 {{ figures.length }} 张 · 点击放大
          </span>
        </div>
        <div class="figure-grid">
          <button
            v-for="(figure, index) in figures"
            :key="figure.id"
            type="button"
            class="figure-card"
            @click="openFigure(index)"
          >
            <span class="figure-card__frame">
              <img
                v-if="!isBroken(figure)"
                :src="figure.url"
                :alt="figure.caption"
                loading="lazy"
                decoding="async"
                @error="markBroken(figure.id)"
              />
              <span v-else class="figure-card__broken">
                <span class="figure-card__broken-glyph" aria-hidden="true">◫</span>
                图片暂时加载不出来
              </span>
            </span>
            <span class="figure-card__body">
              <span class="figure-card__label">{{ figure.label }}</span>
              <span class="figure-card__caption" :title="figure.caption">{{ figure.caption }}</span>
            </span>
          </button>
        </div>
      </section>

      <section v-if="paper" class="section">
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

      <FigureLightbox
        :figures="figures"
        :open-index="lightboxIndex"
        :title="episode.title"
        @close="lightboxIndex = null"
        @navigate="navigateFigure"
      />
    </template>
  </div>
</template>
