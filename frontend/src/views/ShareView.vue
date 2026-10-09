<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  downloadUrl,
  errorMessage,
  getShare,
  isApiError,
  isEpisodeLanguage,
  shareAnalysisMdUrl,
  shareScriptTxtUrl,
} from '../api'
import type { EpisodeLanguage, Figure, ShareVersion, ShareView } from '../api'
import AnalysisView from '../components/AnalysisView.vue'
import AudioPlayer from '../components/AudioPlayer.vue'
import FigureGallery from '../components/FigureGallery.vue'
import FigureLightbox from '../components/FigureLightbox.vue'
import LanguageSwitch from '../components/LanguageSwitch.vue'
import ScriptView from '../components/ScriptView.vue'
import { usePreferencesStore } from '../stores/preferences'
import { formatBytes, formatDateTime, formatDuration } from '../utils/format'
import { hasKeywords } from '../utils/highlight'
import { languageLabel, languageShort } from '../utils/language'

/**
 * 公开分享页（契约 §2.7）。三条硬要求，逐条对应下面的实现：
 *
 * 1. **免登录**。路由标记成 `meta.public`（守卫放行），接口只走 `/api/share/{token}/…`
 *    —— 这一页里**没有**任何 `/api/episodes/…` 调用，所以它永远不会触发登录跳转。
 * 2. **失效链接要有明确的页面**。token 失效 / 那一集不再公开时后端返回 404，
 *    这里渲染「分享链接已失效」，而不是白屏。
 * 3. `?lang=` 切语言：切的是 `versions[lang]` 里的脚本 / 解读 / 音频 / 视频 / 信息图。
 *    封面与论文原图跨语言共用（同一份 PDF），所以不跟着切。
 */
const route = useRoute()
const router = useRouter()
const prefs = usePreferencesStore()

const token = computed(() => String(route.params.token ?? ''))
const share = ref<ShareView | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
/** 链接失效（404）：和「读不到」分开，这页的文案完全不一样 */
const expired = ref(false)

const audioRef = ref<InstanceType<typeof AudioPlayer> | null>(null)
const videoEl = ref<HTMLVideoElement | null>(null)
const videoFailed = ref(false)
const lightboxIndex = ref<number | null>(null)
const downloadError = ref<string | null>(null)

// ---------------------------------------------------------------------------
// 语言版本
// ---------------------------------------------------------------------------

/** 这一份公开视图真的有内容的语言版本（顺序 = 契约里的生成顺序） */
const languages = computed<EpisodeLanguage[]>(() => {
  const value = share.value
  if (!value) return []
  const declared = (value.languages ?? []).filter(isEpisodeLanguage)
  const available = declared.filter((language) => value.versions?.[language])
  ;(Object.keys(value.versions ?? {}) as EpisodeLanguage[]).forEach((language) => {
    if (value.versions?.[language] && !available.includes(language)) available.push(language)
  })
  return available
})

/**
 * 当前语言：来自 `?lang=`（这样分享出去的链接能带语言），
 * 非法或缺失时退回主语言 —— 非法取值不能让页面空掉。
 */
const activeLanguage = computed<EpisodeLanguage | null>(() => {
  const raw = route.query.lang
  const wanted = Array.isArray(raw) ? raw[0] : raw
  if (isEpisodeLanguage(wanted) && languages.value.includes(wanted)) return wanted
  const primary = share.value?.language
  if (isEpisodeLanguage(primary) && languages.value.includes(primary)) return primary
  return languages.value[0] ?? null
})

/** 当前语言的版本内容；没有 versions 时退回顶层字段（老数据） */
const activeVersion = computed<ShareVersion | null>(() => {
  const language = activeLanguage.value
  if (!language || !share.value?.versions) return null
  return share.value.versions[language] ?? null
})

const script = computed(() => activeVersion.value?.script ?? share.value?.script ?? null)
const analysis = computed(() => activeVersion.value?.analysis ?? share.value?.analysis ?? null)
const video = computed(() => activeVersion.value?.video ?? share.value?.video ?? null)
const audioSrc = computed(() => activeVersion.value?.audio_url ?? share.value?.audio_url ?? null)
const audioDuration = computed(
  () => activeVersion.value?.audio_duration_sec ?? share.value?.audio_duration_sec ?? null,
)
const audioBytes = computed(() => activeVersion.value?.audio_bytes ?? share.value?.audio_bytes ?? null)
const illustration = computed(() => activeVersion.value?.illustration ?? share.value?.illustration ?? null)
const paperMeta = computed(() => activeVersion.value?.paper_meta ?? share.value?.paper_meta ?? null)
const keywords = computed(() => paperMeta.value?.keywords ?? [])
const showHighlightSwitch = computed(() => hasKeywords(keywords.value))
const highlight = computed(() => prefs.preferences.highlight_keywords)

const figures = computed<Figure[]>(() => share.value?.figures ?? [])

function selectLanguage(language: EpisodeLanguage): void {
  if (language === activeLanguage.value) return
  // 语言写进地址栏：这样「切到英文再复制链接」给出去的就是英文版
  void router.replace({ query: { ...route.query, lang: language } })
}

const languageSuffix = computed(() =>
  languages.value.length > 1 && activeLanguage.value ? `-${languageShort(activeLanguage.value)}` : '',
)

// ---------------------------------------------------------------------------
// 播放互斥（视频自带音轨，两路声音不能同时响）
// ---------------------------------------------------------------------------

function pauseInactiveMedia(active: HTMLMediaElement | null): void {
  document.querySelectorAll<HTMLMediaElement>('audio, video').forEach((el) => {
    if (el !== active && !el.paused) el.pause()
  })
}

function onVideoPlay(event: Event): void {
  audioRef.value?.pause()
  pauseInactiveMedia(event.target as HTMLMediaElement)
}

function onAudioPlay(element: HTMLAudioElement): void {
  const el = videoEl.value
  if (el && !el.paused) el.pause()
  pauseInactiveMedia(element)
}

// ---------------------------------------------------------------------------
// 下载（公开通道：/api/share/{token}/script.txt 与 analysis.md）
// ---------------------------------------------------------------------------

function download(fn: (token: string, lang?: EpisodeLanguage) => string, suffix: string): void {
  downloadError.value = null
  const url = fn(token.value, activeLanguage.value ?? undefined)
  if (!url) {
    downloadError.value = '这个文件还没有生成，暂时无法下载'
    return
  }
  const base = (share.value?.title ?? 'paper-podcast').replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 60)
  downloadUrl(url, `${base}${suffix.replace('.', `${languageSuffix.value}.`)}`)
}

function navigateFigure(delta: number): void {
  const count = figures.value.length
  const current = lightboxIndex.value
  if (!count || current === null) return
  lightboxIndex.value = (current + delta + count) % count
}

// ---------------------------------------------------------------------------

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  expired.value = false
  videoFailed.value = false
  lightboxIndex.value = null
  try {
    share.value = await getShare(token.value)
  } catch (cause) {
    share.value = null
    // 404 = 链接失效（token 换了 / 作者关掉了分享）——这是**预期内**的结果，单独一种页面
    if (isApiError(cause) && cause.status === 404) {
      expired.value = true
      error.value = cause.message
    } else {
      error.value = errorMessage(cause, '读取分享内容失败')
    }
  } finally {
    loading.value = false
  }
}

watch(token, () => {
  void load()
})

onMounted(() => {
  void load()
})

onBeforeUnmount(() => {
  videoEl.value?.pause()
})
</script>

<template>
  <div class="container page">
    <div v-if="loading" class="card card--pad">
      <div class="skeleton" style="width: 50%; height: 26px; margin-bottom: 16px" />
      <div class="skeleton" style="width: 26%; margin-bottom: 26px" />
      <div class="skeleton" style="width: 100%; height: 92px" />
    </div>

    <!-- 失效链接：明确说清楚「为什么打不开」，并给出下一步（不能白屏） -->
    <div v-else-if="expired" class="card card--pad">
      <div class="empty">
        <p class="empty__title">分享链接已失效</p>
        <p style="margin-bottom: 6px">
          这一期播客可能已经被作者取消分享，或者链接已经换成了新的。
        </p>
        <p class="section__hint" style="margin-bottom: 18px">
          请向分享给你的人要一条新的链接。（token：{{ token }}）
        </p>
        <div class="row" style="justify-content: center">
          <button type="button" class="btn btn--ghost" @click="load">重新加载</button>
          <RouterLink to="/" class="btn btn--primary">看看这个产品是做什么的</RouterLink>
        </div>
      </div>
    </div>

    <div v-else-if="error" class="card card--pad">
      <div class="empty">
        <p class="empty__title">暂时读不到这份分享</p>
        <p style="margin-bottom: 18px">{{ error }}</p>
        <button type="button" class="btn btn--primary" @click="load">重新加载</button>
      </div>
    </div>

    <template v-else-if="share">
      <header style="margin-bottom: 26px">
        <p class="eyebrow">Shared · 由作者分享的公开链接</p>
        <div class="row row--between" style="align-items: flex-start">
          <div style="min-width: 0; max-width: 70ch">
            <h1 class="page-title" style="margin-bottom: 12px">{{ share.title }}</h1>
            <div class="row" style="gap: 10px; flex-wrap: wrap">
              <span class="badge badge--accent">
                作者：{{ share.author?.display_name || '未署名' }}
              </span>
              <span v-if="languages.length > 1" class="badge badge--neutral">
                {{ languages.map(languageLabel).join(' + ') }}
              </span>
              <span class="badge badge--neutral">公开分享</span>
              <span class="section__hint">生成于 {{ formatDateTime(share.created_at) }}</span>
            </div>
          </div>
          <div class="page__actions">
            <LanguageSwitch
              v-if="languages.length > 1"
              :languages="languages"
              :model-value="activeLanguage"
              label="语言"
              @update:model-value="selectLanguage"
            />
          </div>
        </div>
      </header>

      <p class="section__hint" style="margin-bottom: 20px">
        这是一个只读页面：不需要登录，也只能看到作者公开的这一期。
        脚本与解读里的关键词高亮可以在下面随时关掉。
      </p>

      <!-- 下载行 -->
      <div class="row" style="margin-bottom: 6px; flex-wrap: wrap">
        <button type="button" class="btn btn--sm" @click="download(shareScriptTxtUrl, '-脚本.txt')">
          ↓ 下载脚本（txt）
        </button>
        <button type="button" class="btn btn--sm" @click="download(shareAnalysisMdUrl, '-解读.md')">
          ↓ 下载解读（md）
        </button>
        <span class="spacer" />
        <label v-if="showHighlightSwitch" class="switch">
          <input v-model="prefs.preferences.highlight_keywords" type="checkbox" />
          关键词高亮
        </label>
      </div>
      <p v-if="downloadError" class="section__hint" style="color: var(--danger); margin-top: 8px">
        {{ downloadError }}
      </p>

      <!-- 视频解读 -->
      <section v-if="video" class="section">
        <div class="section__head">
          <h2 class="section__title">视频解读播客</h2>
          <span class="section__hint">
            竖版 936×1210 · 画面按脚本逐段切换
            <template v-if="languages.length > 1"> · 当前 {{ languageLabel(activeLanguage) }}版</template>
          </span>
        </div>
        <div class="card card--pad">
          <div class="vplayer__stage" style="aspect-ratio: 936 / 1210">
            <video
              v-if="!videoFailed"
              ref="videoEl"
              :key="video.url"
              class="vplayer__media"
              :src="video.url"
              :poster="share.cover_url ?? undefined"
              controls
              playsinline
              preload="metadata"
              :aria-label="`《${share.title}》视频解读播客（公开分享）`"
              @play="onVideoPlay"
              @error="videoFailed = true"
            />
            <div v-else class="vplayer__broken">
              <span class="vplayer__broken-glyph" aria-hidden="true">▶</span>
              <p class="vplayer__broken-title">视频暂时加载不出来</p>
              <p class="section__hint" style="margin: 0">音频、脚本与解读都不受影响。</p>
            </div>
          </div>
          <p class="video-meta">
            <span v-if="video.duration_sec" class="file-pill__size">
              视频 {{ formatDuration(video.duration_sec) }}<template v-if="video.bytes">
                · {{ formatBytes(video.bytes) }}</template
              >
            </span>
          </p>
        </div>
      </section>

      <!-- 音频（没有视频时是主角；有视频时播放器就不摆了，避免两路声音） -->
      <template v-if="!video || videoFailed">
        <AudioPlayer
          ref="audioRef"
          :src="audioSrc"
          :fallback-duration="audioDuration"
          :title="languages.length > 1 ? `${share.title}（${languageLabel(activeLanguage)}版）` : share.title"
          @play="onAudioPlay"
        />
        <p v-if="audioBytes" class="section__hint" style="margin-top: 10px">
          音频 {{ formatBytes(audioBytes) }} · 时长 {{ formatDuration(audioDuration) }}
        </p>
      </template>

      <!-- 封面（论文首页）与论文原图：跨语言共用，不随语言切换 -->
      <figure v-if="share.cover_url && !video" class="hero" style="margin-top: 28px">
        <div class="hero__art" :style="{ aspectRatio: share.cover_width && share.cover_height ? `${share.cover_width} / ${share.cover_height}` : '1 / 1.294' }">
          <img class="hero__img is-loaded" :src="share.cover_url" alt="论文首页渲染图" decoding="async" />
        </div>
        <figcaption class="hero__caption">
          <span class="badge badge--neutral">论文首页</span>
          <span class="section__hint">封面与论文原图跨语言共用，切换语言不会换图</span>
        </figcaption>
      </figure>

      <section v-if="illustration" class="section">
        <div class="section__head">
          <h2 class="section__title">生成信息图</h2>
          <span class="section__hint">模型把这篇论文提炼成一页图解 · 信息图跟随语言</span>
        </div>
        <div class="illus card card--pad">
          <div
            class="illus__stage"
            :style="{ aspectRatio: `${illustration.width || 16} / ${illustration.height || 9}` }"
          >
            <object
              v-if="illustration.svg_url"
              class="illus__media"
              :data="illustration.svg_url"
              type="image/svg+xml"
              :aria-label="`《${share.title}》生成信息图`"
            >
              <img
                v-if="illustration.png_url"
                class="illus__media"
                :src="illustration.png_url"
                alt="生成信息图（静态 PNG 降级）"
              />
            </object>
            <img
              v-else-if="illustration.png_url"
              class="illus__media"
              :src="illustration.png_url"
              alt="生成信息图"
            />
          </div>
        </div>
      </section>

      <section v-if="figures.length" id="section-figures" class="section">
        <div class="section__head">
          <h2 class="section__title">论文原图（{{ figures.length }} 张）</h2>
          <span class="section__hint">从 PDF 按「Figure N:」图注提取 · 点击放大</span>
        </div>
        <FigureGallery :figures="figures" readonly @open="lightboxIndex = $event" @rotate="() => {}" />
      </section>

      <section id="section-script" class="section">
        <div class="section__head">
          <h2 class="section__title">播客脚本</h2>
          <span class="section__hint">双人对谈 · 逐段展示</span>
        </div>
        <div class="card card--pad">
          <ScriptView
            :script="script"
            :language="activeLanguage ?? undefined"
            :keywords="keywords"
            :highlight="highlight"
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
        <AnalysisView :analysis="analysis" :keywords="keywords" :highlight="highlight" />
      </section>

      <FigureLightbox
        :figures="figures"
        :open-index="lightboxIndex"
        :title="share.title"
        readonly
        @close="lightboxIndex = null"
        @navigate="navigateFigure"
        @rotate="() => {}"
        @delete="() => {}"
      />
    </template>
  </div>
</template>
