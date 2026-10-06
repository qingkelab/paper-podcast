<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  IS_MOCK,
  analysisMdUrl,
  audioUrl,
  deleteFigure,
  downloadUrl,
  errorMessage,
  getEpisode,
  isApiError,
  retryEpisode,
  rotateFigure,
  scriptTxtUrl,
} from '../api'
import type { Analysis, Episode, Figure, FigureRotateDirection, VideoInfo } from '../api'
import AudioPlayer from '../components/AudioPlayer.vue'
import AnalysisView from '../components/AnalysisView.vue'
import FigureGallery from '../components/FigureGallery.vue'
import FigureLightbox from '../components/FigureLightbox.vue'
import PaperMetaSection from '../components/PaperMetaSection.vue'
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
const audioSrc = computed(() => (episode.value ? audioUrl(episode.value) : null))
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
// 视频解读播客（契约 §2：竖版 936×1210，H.264 + AAC）
// ---------------------------------------------------------------------------

/** 契约 §2 固定画幅：论文首页是 935×1210，宽度取 936 是因为 H.264 要求宽高都能被 2 整除 */
const VIDEO_ASPECT = '936 / 1210'
const VIDEO_NOMINAL_SIZE = '936×1210'

const video = computed<VideoInfo | null>(() => episode.value?.video ?? null)
/** 音频播放器的公开方法（pause），用于音视频互斥 */
const audioRef = ref<InstanceType<typeof AudioPlayer> | null>(null)
const videoEl = ref<HTMLVideoElement | null>(null)
/** 加载成功后用元素读到的真实尺寸/时长覆盖契约值（读不到就退回契约值） */
const videoSize = ref<{ width: number; height: number } | null>(null)
const videoMediaDuration = ref(0)
/** video.url 打不开（404 / 编码不支持）：整块换成提示，绝不显示破播放器 */
const videoFailed = ref(false)

const videoDuration = computed(() =>
  videoMediaDuration.value > 0 ? videoMediaDuration.value : (video.value?.duration_sec ?? null),
)
const videoSizeLabel = computed(() => {
  const size = videoSize.value
  return size ? `${size.width}×${size.height}` : VIDEO_NOMINAL_SIZE
})

/**
 * 音频播放器何时出场。
 *
 * 页面有两种展示形态（见模板）：
 *   - 有视频（episode.video 非 null）：视频为主。视频自带音轨，独立音频播放器会让人
 *     不知道该点哪个，所以整个不渲染 —— 互斥逻辑此时自然不会触发。
 *   - 没有视频：完全保持改造前的样子，音频播放器照旧。
 * 另外视频地址真的打不开时（videoFailed）把音频播放器顶上来，兑现降级提示里
 * 「音频、脚本与解读都不受影响」这句话；此时双向互斥依旧生效。
 */
const audioVisible = computed(() => !video.value || videoFailed.value)

function downloadVideo(): void {
  const url = video.value?.url
  if (!url) return
  downloadUrl(url, `${safeName()}-视频解读.mp4`)
}

function onVideoMetadata(): void {
  const el = videoEl.value
  if (!el) return
  const value = el.duration
  if (Number.isFinite(value) && value > 0) videoMediaDuration.value = value
  if (el.videoWidth > 0 && el.videoHeight > 0) {
    videoSize.value = { width: el.videoWidth, height: el.videoHeight }
  }
}

function onVideoError(): void {
  videoFailed.value = true
}

/** 重试：把 <video> 整个重建（v-if 切回来），比调 load() 更干净 */
function retryVideo(): void {
  videoFailed.value = false
  videoMediaDuration.value = 0
  videoSize.value = null
}

function resetVideoState(): void {
  videoEl.value?.pause()
  videoFailed.value = false
  videoMediaDuration.value = 0
  videoSize.value = null
}

// --- 音视频互斥：同一页面上两路声音绝不能同时响 -----------------------------

/**
 * 兜底：把页面上除「刚开播的那个元素」之外的 media 全部暂停。
 * 主路径是下面的双向事件（视频 play → 音频 pause()，音频 play → 视频 pause()），
 * 这层兜底是为了事件链路万一没接上时也不出现两路声音。
 */
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
// 论文原图画廊 + 灯箱
// ---------------------------------------------------------------------------

const figures = computed<Figure[]>(() => episode.value?.figures ?? [])
const lightboxIndex = ref<number | null>(null)

function openFigure(index: number): void {
  lightboxIndex.value = index
}

function navigateFigure(delta: number): void {
  const count = figures.value.length
  const current = lightboxIndex.value
  if (!count || current === null) return
  lightboxIndex.value = (current + delta + count) % count
}

// --- 人工校正配图（契约 §2：rotate / delete，都返回更新后的完整 Episode） -------

/**
 * 正在写操作的配图 id。非 null 时所有相关按钮都禁用——既是 loading 反馈，
 * 也是防连点：旋转/删除不是幂等操作（转两下就是 180°），重复请求必须挡掉。
 */
const figureBusyId = ref<string | null>(null)
/** 具体在做什么，用来决定哪个按钮显示 spinner（灯箱里的三个按钮共用 pending 状态） */
const figureAction = ref<FigureRotateDirection | 'delete' | null>(null)
/** 上一次校正操作的失败原因：留在灯箱里，不要静默失败 */
const figureError = ref<string | null>(null)

/**
 * 本次会话内被人工校正过的配图 —— 后端**没有**记录「是否人工改过」这个字段，
 * 所以只在组件内存里记（episodeId + figureId 作为 key），刷新页面就没了。
 * 绝不把它伪造成后端字段。
 */
const correctedFigureKeys = ref(new Set<string>())

function correctedKey(figureId: string): string {
  return `${id.value}:${figureId}`
}

const correctedFigureIds = computed(() =>
  figures.value
    .filter((figure) => correctedFigureKeys.value.has(correctedKey(figure.id)))
    .map((figure) => figure.id),
)

/** 灯箱当前那张（删除后要按 id 找到对应配图，不要用按钮传进来的索引） */
const lightboxFigure = computed<Figure | null>(() => {
  const index = lightboxIndex.value
  if (index === null) return null
  return figures.value[index] ?? null
})

// --- 保存反馈：保存是即时的，没有「保存」按钮，必须给明确的短暂提示 ------------

const toast = ref<{ kind: 'ok' | 'error'; text: string } | null>(null)
let toastTimer: number | undefined

function showToast(kind: 'ok' | 'error', text: string): void {
  if (toastTimer !== undefined) window.clearTimeout(toastTimer)
  toast.value = { kind, text }
  toastTimer = window.setTimeout(
    () => {
      toastTimer = undefined
      toast.value = null
    },
    kind === 'ok' ? 2000 : 4500,
  )
}

async function rotateFigureBy(figureId: string, direction: FigureRotateDirection): Promise<void> {
  if (figureBusyId.value) return // 连点保护：上一次还没回来就不发新请求
  figureBusyId.value = figureId
  figureAction.value = direction
  figureError.value = null
  try {
    const updated = await rotateFigure(id.value, figureId, direction)
    // 就地刷新：返回的 Episode 里 figures[].url 已经带上新的 ?v= 版本号，
    // 覆盖上去以后灯箱与画廊都会重新拉图（不会继续显示缓存里的旧图）
    episode.value = updated
    correctedFigureKeys.value.add(correctedKey(figureId))
    showToast('ok', direction === 'cw' ? '已保存 · 顺时针 90°' : '已保存 · 逆时针 90°')
  } catch (cause) {
    const message = errorMessage(cause, '校正配图失败')
    figureError.value = message
    showToast('error', message)
  } finally {
    figureBusyId.value = null
    figureAction.value = null
  }
}

async function removeFigure(figure: Figure): Promise<void> {
  if (figureBusyId.value) return
  figureBusyId.value = figure.id
  figureAction.value = 'delete'
  figureError.value = null
  try {
    const updated = await deleteFigure(id.value, figure.id)
    episode.value = updated
    correctedFigureKeys.value.delete(correctedKey(figure.id))
    // 这张图没了，灯箱停在旧索引上会指向别的图（甚至越界），直接关掉
    lightboxIndex.value = null
    showToast('ok', `已删除 ${figure.label}`)
  } catch (cause) {
    const message = errorMessage(cause, '删除配图失败')
    figureError.value = message
    showToast('error', message)
  } finally {
    figureBusyId.value = null
    figureAction.value = null
  }
}

function rotateFromLightbox(direction: FigureRotateDirection): void {
  const figure = lightboxFigure.value
  if (figure) void rotateFigureBy(figure.id, direction)
}

function deleteFromLightbox(): void {
  const figure = lightboxFigure.value
  if (figure) void removeFigure(figure)
}

/** 画廊卡片上的快捷「↻」：只转顺时针，需要逆时针或删除就点开大图 */
function rotateFromGallery(figure: Figure): void {
  void rotateFigureBy(figure.id, 'cw')
}

// ---------------------------------------------------------------------------
// 折叠区（只有「有视频」形态才用得上）
// ---------------------------------------------------------------------------

/** 收起的 <details> 里元素高度是 0，靠 scrollIntoView 会滚到错的位置，所以先展开再滚 */
const scriptFold = ref<HTMLDetailsElement | null>(null)
const analysisFold = ref<HTMLDetailsElement | null>(null)

/**
 * 「结构化解读」的板块数。
 * 判据和 AnalysisView 里的 CARDS 过滤保持一致（只数有内容的板块）；
 * 那个列表是组件内部的，这里如果要跟着改，两处记得一起动。
 */
const ANALYSIS_FIELDS: Array<keyof Analysis> = [
  'background',
  'innovations',
  'method',
  'experiments',
  'conclusion',
  'limitations',
  'value',
  'future',
]

const scriptSegments = computed(() => episode.value?.script?.segments.length ?? 0)
const scriptWords = computed(() => episode.value?.script?.word_count ?? 0)
const analysisCards = computed(() => {
  const analysis = episode.value?.analysis
  if (!analysis) return 0
  return ANALYSIS_FIELDS.filter((key) => {
    const value = analysis[key]
    if (Array.isArray(value)) return value.some((item) => Boolean(item && item.trim()))
    return typeof value === 'string' && Boolean(value.trim())
  }).length
})

function toggleFold(fold: HTMLDetailsElement | null, open: boolean): void {
  if (fold) fold.open = open
}

/**
 * 回车键开合折叠区。
 *
 * <summary> 原生只认空格（实测 Chrome 里 Enter 不触发开合，Firefox/Safari 认），
 * 所以这里补一个 Enter 处理让两边一致。用 .prevent 先把默认行为掐掉：
 * 在原生认 Enter 的浏览器里，默认行为会再合成一次 click，不掐掉就会「开→关」白折腾一下。
 */
function onSummaryEnter(event: KeyboardEvent): void {
  const fold = (event.currentTarget as HTMLElement | null)?.closest('details')
  if (fold) fold.open = !fold.open
}

// /episode/:id → /episode/:other 命中同一个路由记录，组件会被复用、onMounted 不会重跑。
// 不监听 id 的话，页面会继续显示上一集的封面与配图（URL 已经变了），所以这里必须重新拉数据。
watch(id, () => {
  coverFailed.value = false
  coverLoaded.value = false
  lightboxIndex.value = null
  // 换了一集：校正相关的状态必须清掉（「已人工校正」是按 episodeId+figureId 记的，
  // 留着上一集的标记会张冠李戴）
  correctedFigureKeys.value = new Set<string>()
  figureBusyId.value = null
  figureAction.value = null
  figureError.value = null
  if (toastTimer !== undefined) {
    window.clearTimeout(toastTimer)
    toastTimer = undefined
  }
  toast.value = null
  resetVideoState()
  episode.value = null
  void load()
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

/**
 * 下载播客音频（mp3）。
 *
 * 只有「有视频」形态才需要它：那种形态下独立音频播放器被去掉了，页面里再没有第二个
 * 能拿到音频文件的入口（视频那个播放器不便另存为 mp3）。无视频形态保持原样——
 * 音频播放器就在页面上，且下载行不动才符合「不改动无视频页面」的要求。
 */
function downloadAudio(): void {
  downloadError.value = null
  const url = audioSrc.value
  if (!url) {
    downloadError.value = '音频还没有生成，暂时无法下载'
    return
  }
  downloadUrl(url, `${safeName()}-播客音频.mp3`)
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

/**
 * 「跳到脚本 / 跳到解读」：目标在收起的折叠区里时高度是 0，直接 scrollIntoView 会滚错，
 * 所以先展开、等布局落定（nextTick）再滚。无视频形态下目标就是普通区块，行为同上。
 */
function scrollToSection(section: 'script' | 'analysis' | 'figures'): void {
  if (section === 'script') toggleFold(scriptFold.value, true)
  else if (section === 'analysis') toggleFold(analysisFold.value, true)
  void nextTick(() => {
    document.getElementById(`section-${section}`)?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    })
  })
}

onMounted(() => {
  void load()
})

onBeforeUnmount(() => {
  if (audioRetryTimer !== undefined) window.clearTimeout(audioRetryTimer)
  if (toastTimer !== undefined) window.clearTimeout(toastTimer)
  // 离开详情页时别把声音带走（音频侧由 AudioPlayer 自己暂停）
  videoEl.value?.pause()
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
      <!--
        封面 hero：只有「没有视频」时才渲染。
        有视频解读时视频片头几秒就是论文首页整页，再放一张同样的图纯属重复，
        还会把真正的主角（视频）挤到首屏之外。
        cover_url 为 null 或加载失败时整块不渲染（不留空白、不留破图）。
      -->
      <figure v-if="!video && coverSrc" class="hero">
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
          视频解读由 Canvas + MediaRecorder 现场录制一段 6 秒占位片（浏览器需要真实录制时间，
          首次进入详情页会稍等一两秒），同样是占位，不是真实产物。
        </span>
      </div>

      <!--
        ================= 有视频的形态：视频为主 =================
        可见区块与顺序：标题 → 下载按钮行 → 视频播放区 → 论文元信息 → 三个折叠区。
        视频排在元信息之前：元信息卡片有 400+ px，挡在中间会让视频掉到首屏之外，
        那样就不叫「视频为主」了（元信息挪到视频之后，见下面的 PaperMetaSection）。
      -->

      <!--
        下载按钮行：视频 / 脚本 / 解读一起给，跳转按钮会先展开对应折叠区再滚过去。
        视频没能加载（videoFailed）时整行让位给下面的音频形态，避免同页出现两排一样的按钮。
        体积/时长这类元信息单独占一行（见下面的 .video-meta）：按钮数量以后还会变，
        靠 flex 挤出来的换行不稳。
      -->
      <template v-if="video && isCompleted && !videoFailed">
        <div class="row video-actions">
          <button type="button" class="btn btn--sm btn--primary" @click="downloadVideo">
            ↓ 下载视频（mp4）
          </button>
          <button type="button" class="btn btn--sm" @click="downloadAudio">↓ 下载音频（mp3）</button>
          <button type="button" class="btn btn--sm" @click="downloadScript">↓ 下载脚本（txt）</button>
          <button type="button" class="btn btn--sm" @click="downloadAnalysis">↓ 下载解读（md）</button>
          <button type="button" class="btn btn--sm btn--ghost" @click="scrollToSection('figures')">
            跳到原图
          </button>
          <button type="button" class="btn btn--sm btn--ghost" @click="scrollToSection('script')">
            跳到脚本
          </button>
          <button type="button" class="btn btn--sm btn--ghost" @click="scrollToSection('analysis')">
            跳到解读
          </button>
        </div>

        <p class="video-meta">
          <span v-if="IS_MOCK" class="badge badge--neutral">演示模式：视频为占位</span>
          <span class="file-pill__size">
            视频 {{ formatDuration(videoDuration) }} · {{ videoSizeLabel }}<template v-if="video.bytes">
              · {{ formatBytes(video.bytes) }}</template
            >
          </span>
        </p>

        <p v-if="downloadError" class="section__hint" style="color: var(--danger); margin-top: 10px">
          {{ downloadError }}
        </p>
      </template>

      <!--
        视频解读播客：video 为 null 时整块不渲染（不留空标题、不留空白块）。
        位置紧跟标题与下载行：视频是主产物，不该被脚本、解读这些长内容挤到下面。
      -->
      <section v-if="video" id="section-video" class="section section--lead">
        <div class="section__head">
          <h2 class="section__title">视频解读播客</h2>
          <span class="section__hint">
            竖版 {{ videoSizeLabel }} · 画面按脚本逐段切换、与音频轮次对齐 · 字幕按主播分色
          </span>
        </div>

        <div class="vplayer card card--pad">
          <div class="vplayer__body">
            <!--
              竖版 936×1210（比例约 0.773）：用 aspect-ratio 按契约比例预留位置
              （加载前后不跳布局），max-height 兜住矮屏不至于把页面撑爆，
              object-fit: contain 保证画面不变形、不裁切。
            -->
            <div class="vplayer__stage" :style="{ aspectRatio: VIDEO_ASPECT }">
              <video
                v-if="!videoFailed"
                ref="videoEl"
                class="vplayer__media"
                :src="video.url"
                :poster="coverSrc ?? undefined"
                controls
                playsinline
                preload="metadata"
                :aria-label="`《${episode.title}》视频解读播客`"
                @loadedmetadata="onVideoMetadata"
                @play="onVideoPlay"
                @error="onVideoError"
              />
              <!-- 降级：地址不可用（404 / 编码不支持 / 还在合成）时不给破播放器 -->
              <div v-else class="vplayer__broken">
                <span class="vplayer__broken-glyph" aria-hidden="true">▶</span>
                <p class="vplayer__broken-title">视频暂时加载不出来</p>
                <p class="section__hint" style="margin-bottom: 14px">
                  视频地址不可用，可能还在合成中或已被清理。脚本、解读与原图都不受影响。
                </p>
                <button type="button" class="btn btn--sm" @click="retryVideo">重新加载视频</button>
              </div>
            </div>

            <div class="vplayer__side">
              <div class="meta-grid">
                <div class="meta-item">
                  <div class="meta-item__label">时长</div>
                  <div class="meta-item__value">{{ formatDuration(videoDuration) }}</div>
                </div>
                <div class="meta-item">
                  <div class="meta-item__label">画幅</div>
                  <div class="meta-item__value">{{ videoSizeLabel }}</div>
                </div>
                <div v-if="video.scene_count" class="meta-item">
                  <div class="meta-item__label">画面</div>
                  <div class="meta-item__value">{{ video.scene_count }} 段</div>
                </div>
                <div v-if="video.bytes" class="meta-item">
                  <div class="meta-item__label">体积</div>
                  <div class="meta-item__value">{{ formatBytes(video.bytes) }}</div>
                </div>
              </div>

              <p class="vplayer__note">
                片头显示论文首页，正文按内容相关性切换论文原图，片尾显示生成的信息图；
                每段画面底部有字幕条，主播 A / 主播 B 用不同颜色区分。
              </p>
              <p class="section__hint">
                视频自带音轨（H.264 + AAC），不需要另外播放音频<template v-if="videoFailed"
                  >；视频没能加载，已把音频播放器放到下方，两者互斥，不会同时出声</template
                >。论文原图、脚本与解读都在下面的折叠区里，点标题展开。
              </p>
            </div>
          </div>
        </div>
      </section>

      <!--
        论文元信息（有视频形态）：排在视频之后。
        这样首屏就是标题 + 下载行 + 视频播放器；元信息卡片 400+ px，放在视频前面会把
        视频顶到首屏之外。无视频形态下走下面那份 `v-if="!video"` 的原位，行为不变。
      -->
      <PaperMetaSection v-if="video" :episode="episode" />

      <!--
        ================= 没有视频的形态：与改造前完全一致 =================
        音频播放器 → 下载行 → 信息图 → 原图 → 元信息 → 脚本 → 解读。
        顺序刻意保持原样；视频加载失败（videoFailed）时也走这里把音频顶上来。
      -->
      <template v-if="isCompleted && audioVisible">
        <AudioPlayer
          ref="audioRef"
          :src="audioSrc"
          :fallback-duration="episode.audio_duration_sec"
          :title="episode.title"
          @play="onAudioPlay"
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

      <section v-if="!isCompleted" class="card card--pad" style="margin-bottom: 22px">
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

      <!--
        模型生成的信息图：用 <object> 引用，SVG 里的 SMIL 动画才会播放；内嵌 <img> 作降级。
        有视频时不渲染：这张图已经在视频片尾用过了，再来一遍就是重复内容。
      -->
      <section v-if="illustration && !video" class="section">
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

      <!--
        论文原图画廊：figures 为空数组时整块不渲染。
        有视频时收进折叠区（这些图视频里已经用过一遍），标题上带张数，收起也看得出内容。
        注意：灯箱的触发按钮就在折叠区里，展开后按钮是真实尺寸，点击照常打开灯箱。
      -->
      <details v-if="video && figures.length" id="section-figures" class="section fold">
        <!--
          aria-label 不是多余的：实测 Chrome 里 <summary> 的可访问名字只认「直接子文本节点」，
          标题一旦包在 <h2>/<span> 里就变成空名字（屏幕阅读器只会念「按钮」）。
          所以这里显式给名字，并且和可见标题保持一致（含数量）。
        -->
        <summary
          class="fold__summary"
          :aria-label="`论文原图（${figures.length} 张）：从 PDF 图注提取，展开后点击任意一张放大；方向由程序自动判定，可能出错，可以手动旋转纠正或删除`"
          @keydown.enter.prevent="onSummaryEnter"
        >
          <h2 class="section__title">论文原图（{{ figures.length }} 张）</h2>
          <span class="section__hint">
            从 PDF 按「Figure N:」图注提取 · 展开后点击放大 · 方向可手动校正
          </span>
          <span class="fold__state" aria-hidden="true">
            <span class="fold__state-closed">展开</span>
            <span class="fold__state-open">收起</span>
          </span>
        </summary>
        <div class="fold__body">
          <FigureGallery
            :figures="figures"
            :busy-id="figureBusyId"
            :corrected-ids="correctedFigureIds"
            @open="openFigure"
            @rotate="rotateFromGallery"
          />
        </div>
      </details>

      <section v-else-if="figures.length" class="section">
        <div class="section__head">
          <h2 class="section__title">论文原图</h2>
          <span class="section__hint">
            从 PDF 按「Figure N:」图注提取 · 共 {{ figures.length }} 张 · 点击放大
          </span>
        </div>
        <FigureGallery
          :figures="figures"
          :busy-id="figureBusyId"
          :corrected-ids="correctedFigureIds"
          @open="openFigure"
          @rotate="rotateFromGallery"
        />
      </section>

      <!-- 论文元信息：没有视频时的原位（有视频时上面已经渲染过一次） -->
      <PaperMetaSection v-if="!video" :episode="episode" />

      <!--
        播客脚本：有视频时默认收起（脚本 2000+ px，是页面变长的头号元凶）。
        用原生 <details>：键盘 Enter/Space 可开，天然带展开态语义，摘要行给出段数与字数。
      -->
      <details v-if="video && episode.script" id="section-script" ref="scriptFold" class="section fold">
        <summary
          class="fold__summary"
          :aria-label="`播客脚本（${scriptSegments} 段 · ${scriptWords} 字）：双人对谈，逐段展示，按说话人配色`"
          @keydown.enter.prevent="onSummaryEnter"
        >
          <h2 class="section__title">播客脚本（{{ scriptSegments }} 段 · {{ scriptWords }} 字）</h2>
          <span class="section__hint">双人对谈 · 逐段展示，按说话人配色</span>
          <span class="fold__state" aria-hidden="true">
            <span class="fold__state-closed">展开</span>
            <span class="fold__state-open">收起</span>
          </span>
        </summary>
        <div class="fold__body">
          <div class="card card--pad">
            <ScriptView
              :script="episode.script"
              :voice-a="episode.options.voice_a"
              :voice-b="episode.options.voice_b"
            />
          </div>
        </div>
      </details>

      <section v-else-if="!video" id="section-script" class="section">
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

      <!--
        结构化解读：有视频时同样默认收起，摘要行给出板块数。
      -->
      <details
        v-if="video && episode.analysis"
        id="section-analysis"
        ref="analysisFold"
        class="section fold"
      >
        <summary
          class="fold__summary"
          :aria-label="`结构化解读（${analysisCards} 个板块）：背景、创新点、方法、实验、结论、不足、价值、未来`"
          @keydown.enter.prevent="onSummaryEnter"
        >
          <h2 class="section__title">结构化解读（{{ analysisCards }} 个板块）</h2>
          <span class="section__hint">
            背景 → 创新点 → 方法 → 实验 → 结论 → 不足 → 价值 → 未来
          </span>
          <span class="fold__state" aria-hidden="true">
            <span class="fold__state-closed">展开</span>
            <span class="fold__state-open">收起</span>
          </span>
        </summary>
        <div class="fold__body">
          <AnalysisView :analysis="episode.analysis" />
        </div>
      </details>

      <section v-else-if="!video" id="section-analysis" class="section">
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
        :pending="figureAction"
        :error="figureError"
        :corrected-ids="correctedFigureIds"
        @close="lightboxIndex = null"
        @navigate="navigateFigure"
        @rotate="rotateFromLightbox"
        @delete="deleteFromLightbox"
      />

      <!--
        保存反馈：校正没有「保存」按钮，操作一发生就已经落库了，
        所以必须给一个短暂但明确的结果提示，失败时更不能静默。
        Teleport 到 body 并压在灯箱（z-index 70）上面：在灯箱里转图时也要看得见。
      -->
      <Teleport to="body">
        <Transition name="toast">
          <div
            v-if="toast"
            class="toast"
            :class="toast.kind === 'error' ? 'toast--error' : 'toast--ok'"
            role="status"
            aria-live="polite"
          >
            <span class="toast__icon" aria-hidden="true">{{ toast.kind === 'error' ? '!' : '✓' }}</span>
            {{ toast.text }}
          </div>
        </Transition>
      </Teleport>
    </template>
  </div>
</template>
