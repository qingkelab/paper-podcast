<script setup lang="ts">
/**
 * 产品首页。
 *
 * 产品介绍页的说服力来自**看见产物本身**，不是把功能列成一张表。
 * 所以整页的骨架是「一屏主张 → 一屏真东西 → 怎么做 → 为什么不一样」：
 * 第二屏直接读库里最近一期已完成的播客，把真实的视频、音频、脚本、解读、配图摆出来。
 *
 * 素材取不到时（空库 / 后端不可用 / 只有排队中的任务）整屏隐藏，
 * 页面退化成纯介绍，**不会出现空播放器或破图**。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { IS_MOCK, getEpisode, getShowcase, isEpisodeLanguage, listEpisodes } from '../api'
import type {
  Analysis,
  EpisodeLanguage,
  Figure,
  Illustration,
  ScriptSegment,
  ShareView,
} from '../api'
import LanguageSwitch from '../components/LanguageSwitch.vue'
import { useMetaStore } from '../stores/meta'
import { useSessionStore } from '../stores/session'
import ShareToXButton from '../components/ShareToXButton.vue'
import { formatDuration } from '../utils/format'
import { languageLabel } from '../utils/language'
import { buildProductShareText } from '../utils/shareX'

const router = useRouter()
const meta = useMetaStore()
const session = useSessionStore()

/** 一个语言版本的展示数据。切换器一按就换的就是这一坨。 */
type ShowcaseVersion = {
  language: EpisodeLanguage
  videoUrl: string | null
  audioUrl: string | null
  /** 视频优先，没有就用音频时长 */
  durationSec: number | null
  segments: ScriptSegment[]
  analysis: Analysis | null
  illustration: Illustration | null
}

type Showcase = {
  episodeId: string
  title: string
  venue: string | null
  year: number | null
  /** 封面（论文首页）和论文原图是**跨语言共用**的，不随切换器变 */
  posterUrl: string | null
  figures: Figure[]
  /** 这一集实际产出了哪些语言，顺序 = 契约里的生成顺序 */
  order: EpisodeLanguage[]
  primaryLanguage: EpisodeLanguage
  versions: Partial<Record<EpisodeLanguage, ShowcaseVersion>>
}

const showcase = ref<Showcase | null>(null)
const showcaseLoading = ref(true)
/** 详情（视频 / 配图 / 脚本 / 解读）是否已经补齐 */
const showcaseDetailReady = ref(false)
/** 成品展示区当前展示的语言版本 */
const showcaseLanguage = ref<EpisodeLanguage | null>(null)
/**
 * 首屏视频**自己**的语言开关。
 *
 * 刻意和 `showcaseLanguage` 分开：首屏那个只换首屏那段视频，
 * 不动下面展示区的视频/音频/脚本/解读。两处共用一个状态的话，
 * 在首屏切一下语言会把下面整块也换掉，不是「只切换视频」。
 */
const heroLanguage = ref<EpisodeLanguage | null>(null)
/** 首屏视觉里的播放入口：点击后真的就地播起来，而不是只做个样子 */
const heroPlaying = ref(false)

const videoRef = ref<HTMLVideoElement | null>(null)

/**
 * 取一期的真实产物。
 *
 * **分两阶段**，因为第二阶段可能很慢：真实后端下详情响应要带上视频/音频/配图，
 * 而离线演示（Mock）里视频是 MediaRecorder **实时录制**的（还有 6 秒素材要录）。
 * 等它回来才渲染的话，首屏会在骨架上停很久。
 * 所以先用列表项（标题 / 封面 / 时长 / 语言）把页面画出来，详情回来后再补视频与配图。
 */
/**
 * 把公开视图（`GET /api/showcase`，免登录）转成展示区需要的数据。
 *
 * 为什么要用这条路：V2 起 `GET /api/episodes` 需要登录，未登录访客在 auth 模式下
 * 会拿到 401 —— 产品介绍页就再也看不到真东西了。后端为此留了免登录的 `/api/showcase`：
 * 登录了给你自己最新一期，没登录就给最近一期**公开分享**的。
 * 它返回的是 ShareView（字段与 Episode 不同名但语义一致），所以在这里做一次映射。
 */
function showcaseFromShare(view: ShareView): Showcase {
  const order = (view.languages ?? []).filter(isEpisodeLanguage)
  const primary = isEpisodeLanguage(view.language) ? view.language : (order[0] ?? 'zh')
  const list = order.length ? order : [primary]
  const versions: Partial<Record<EpisodeLanguage, ShowcaseVersion>> = {}
  list.forEach((language) => {
    const version = view.versions?.[language] ?? null
    const surface = language === primary ? view : null
    versions[language] = {
      language,
      videoUrl: version?.video?.url ?? surface?.video?.url ?? null,
      audioUrl: version?.audio_url ?? surface?.audio_url ?? null,
      durationSec:
        version?.video?.duration_sec ??
        version?.audio_duration_sec ??
        surface?.video?.duration_sec ??
        surface?.audio_duration_sec ??
        null,
      segments: (version?.script?.segments ?? surface?.script?.segments ?? [])
        .filter((segment) => !segment.brand)
        .slice(0, 4),
      analysis: version?.analysis ?? surface?.analysis ?? null,
      illustration: version?.illustration ?? surface?.illustration ?? null,
    }
  })
  return {
    episodeId: `share:${view.token ?? 'showcase'}`,
    title: view.title,
    venue: view.paper_meta?.venue ?? null,
    year: view.paper_meta?.year ?? null,
    posterUrl: view.cover_url ?? null,
    figures: view.figures ?? [],
    order: list,
    primaryLanguage: primary,
    versions,
  }
}

/** 未登录（auth 模式）时走免登录通道；其余情况保持原来的两阶段加载 */
async function loadPublicShowcase(): Promise<boolean> {
  try {
    const view = await getShowcase()
    const mapped = showcaseFromShare(view)
    showcase.value = mapped
    showcaseLanguage.value = mapped.primaryLanguage
    heroLanguage.value = mapped.primaryLanguage
    showcaseDetailReady.value = true
    return true
  } catch {
    // 404 = 还没有公开分享的成品：这不是错误，展示区整块隐藏即可
    return false
  }
}

async function loadShowcase(): Promise<void> {
  showcaseLoading.value = true
  try {
    // 先等会话探测完（先 health 后 me，契约 §3）：它决定「该走哪条数据通道」
    await session.ensureInit()
    if (session.isAuthMode && !session.isLoggedIn) {
      await loadPublicShowcase()
      return
    }

    const list = await listEpisodes({ status: 'completed', limit: 12 })
    const candidates = list.items.filter((item) => item.status === 'completed')
    // 优先挑多语言的：一屏就能把「视频 + 双语 + 切换器」三件事讲清楚
    const picked =
      candidates.find((item) => item.languages && item.languages.length > 1) ?? candidates[0]
    if (!picked) return

    // 列表接口刻意不带 versions（它内部装的就是脚本/解读/视频这些大字段），
    // 所以第一阶段只能用顶层字段：封面、标题、主语言音频。
    const order = (picked.languages ?? []).filter(isEpisodeLanguage)
    const primary = isEpisodeLanguage(picked.language)
      ? picked.language
      : (order[0] ?? 'zh')

    showcase.value = {
      episodeId: picked.id,
      title: picked.title,
      venue: picked.paper_meta?.venue ?? null,
      year: picked.paper_meta?.year ?? null,
      posterUrl: picked.cover_url,
      figures: [],
      order: order.length ? order : [primary],
      primaryLanguage: primary,
      versions: {
        [primary]: {
          language: primary,
          videoUrl: null,
          audioUrl: picked.audio_url,
          durationSec: picked.audio_duration_sec,
          segments: [],
          analysis: null,
          illustration: null,
        },
      },
    }
    showcaseLanguage.value = primary
    heroLanguage.value = primary
    showcaseLoading.value = false

    const full = await getEpisode(picked.id)
    if (!showcase.value || showcase.value.episodeId !== full.id) return

    const fullPrimary = isEpisodeLanguage(full.language) ? full.language : primary
    const fullOrder = (full.languages ?? []).filter(isEpisodeLanguage)
    const order2 = fullOrder.length ? fullOrder : [fullPrimary]

    // 每个语言版本都留一份：切换器一按就换，不再请求接口
    const versions: Partial<Record<EpisodeLanguage, ShowcaseVersion>> = {}
    order2.forEach((language) => {
      const version = full.versions?.[language] ?? null
      const script = version?.script ?? (language === fullPrimary ? full.script : null)
      const video = version?.video ?? (language === fullPrimary ? full.video : null)
      versions[language] = {
        language,
        videoUrl: video?.url ?? null,
        audioUrl:
          version?.audio_url ?? (language === fullPrimary ? full.audio_url : null),
        durationSec:
          video?.duration_sec ??
          version?.audio_duration_sec ??
          (language === fullPrimary ? full.audio_duration_sec : null),
        // 品牌片头片尾不是论文正文，展示脚本片段时要排掉
        segments: (script?.segments ?? []).filter((segment) => !segment.brand).slice(0, 4),
        analysis: version?.analysis ?? (language === fullPrimary ? full.analysis : null),
        // 信息图按语言各一份（图上写着字）；老数据只有顶层那一份，退回顶层
        illustration: version?.illustration ?? full.illustration ?? null,
      }
    })

    showcase.value = {
      ...showcase.value,
      venue: full.paper_meta?.venue ?? showcase.value.venue,
      year: full.paper_meta?.year ?? showcase.value.year,
      posterUrl: full.cover_url ?? showcase.value.posterUrl,
      figures: full.figures ?? [],
      order: order2,
      primaryLanguage: fullPrimary,
      versions,
    }
    // 主语言可能变了（老数据没有 language 字段时由 languages[0] 推断）
    showcaseLanguage.value = fullPrimary
    heroLanguage.value = fullPrimary
    showcaseDetailReady.value = true
  } catch {
    // 首页不允许因为取素材失败而坏掉：整屏静默隐藏即可
    showcase.value = null
  } finally {
    showcaseLoading.value = false
  }
}

/**
 * 可切换的语言。**只列出真的有产物的**：
 * 加载完之前不显示切换器，免得点过去是一片空白。
 */
const showcaseLanguages = computed<EpisodeLanguage[]>(() => {
  const item = showcase.value
  if (!item || !showcaseDetailReady.value) return []
  return item.order.filter((language) => {
    const version = item.versions[language]
    return Boolean(version && (version.videoUrl || version.audioUrl))
  })
})

const activeVersion = computed<ShowcaseVersion | null>(() => {
  const item = showcase.value
  const language = showcaseLanguage.value
  if (!item || !language) return null
  return item.versions[language] ?? null
})

/** 首屏那一版（和展示区相互独立） */
const heroVersion = computed<ShowcaseVersion | null>(() => {
  const item = showcase.value
  const language = heroLanguage.value
  if (!item || !language) return null
  return item.versions[language] ?? null
})

function selectShowcaseLanguage(language: EpisodeLanguage): void {
  if (language === showcaseLanguage.value) return
  if (!showcaseLanguages.value.includes(language)) return
  showcaseLanguage.value = language
}

function selectHeroLanguage(language: EpisodeLanguage): void {
  if (language === heroLanguage.value) return
  if (!showcaseLanguages.value.includes(language)) return
  heroLanguage.value = language
}

// 首屏换了语言就把「正在播」的状态丢掉：两个语言是两份文件，视频元素会重建，
// 留着播放按钮的隐藏状态会让人以为还在放同一段
watch(heroLanguage, () => {
  heroPlaying.value = false
})

function languageSwitchHint(language: EpisodeLanguage): string {
  return `切换到${languageLabel(language)}版视频`
}

function showcaseSwitchHint(language: EpisodeLanguage): string {
  return `切换到${languageLabel(language)}版（视频 / 音频 / 脚本 / 解读 / 信息图 一起换）`
}

function playHero(): void {
  heroPlaying.value = true
  void videoRef.value?.play().catch(() => {
    /* 自动播放被拦时用户还能点原生控件，不额外处理 */
  })
}

/**
 * 滚到成品展示区。
 *
 * 不用 `<a href="#showcase">`：那依赖 `html { scroll-behavior: smooth }`，
 * 而全局平滑滚动会让路由切换时的回到顶部也变成动画，观感很怪。
 */
function scrollToShowcase(): void {
  document.getElementById('showcase')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// ---- 下面这些全部取自「当前语言那一版」，切换器一按就跟着换 ----

const activeLanguage = computed(() => showcaseLanguage.value)
const episodeDuration = computed(() => activeVersion.value?.durationSec ?? null)

/** 成品区左栏的视频 */
const videoUrl = computed(() => activeVersion.value?.videoUrl ?? null)
/** 首屏那一段视频（只跟着首屏自己的开关走） */
const heroVideoUrl = computed(() => heroVersion.value?.videoUrl ?? null)
const heroDuration = computed(() => heroVersion.value?.durationSec ?? null)
const audioUrl = computed(() => activeVersion.value?.audioUrl ?? null)

const analysisPreview = computed(() => {
  const analysis = activeVersion.value?.analysis
  if (!analysis) return []
  return [
    { label: '研究背景', text: analysis.background },
    { label: '核心结论', text: analysis.conclusion },
  ].filter((row) => Boolean(row.text && row.text.trim()))
})

const scriptSegments = computed(() => activeVersion.value?.segments ?? [])
const illustration = computed(() => activeVersion.value?.illustration ?? null)

/** 这一集有哪些语言（给标签用）；只有一种时不显示切换器 */
const languageNamesText = computed(() =>
  (showcase.value?.order ?? []).map((language) => languageLabel(language)).join(' / '),
)

/** 首屏的短事实：每条都能在这页上被验证，不是口号 */
const PROOF = computed(() => [
  { label: '中英双语', hint: '同一集内嵌两版' },
  { label: '竖版视频', hint: '936 × 1210 白底' },
  { label: '逐段配图', hint: '图文对不上就现场生成' },
  { label: '人工可校正', hint: '配图方向一键转' },
])

const OUTPUTS = [
  {
    icon: '▶',
    title: '视频解读播客',
    body: '配图和音频合成一个竖版 MP4，画面按脚本逐段切换，字幕与音频轮次精确对齐。',
  },
  {
    icon: '♪',
    title: '双人对谈音频',
    body: '主播 A 讲解、主播 B 追问质疑。开头结尾是社区自己的品牌话术，没有罐头背景乐。',
  },
  {
    icon: '¶',
    title: '逐段脚本',
    body: '完整双人对谈脚本，可导出 .txt。字幕就是从它逐段生成的，听完能对着文字核对。',
  },
  {
    icon: '◫',
    title: '结构化解读',
    body: '背景、创新点、方法、实验、结论、不足、价值、未来方向八个板块，可导出 .md。',
  },
]

const PIPELINE = [
  { step: '01', title: '解析论文', body: 'PDF / 链接 / 文本统一成干净正文；按图注提取论文原图，渲染首页当封面。' },
  { step: '02', title: '深度解读', body: '一次调用产出八个板块的结构化解读，作为脚本的输入。' },
  { step: '03', title: '生成脚本', body: '改编成双人口语对谈，时长按实测语速换算成字数硬约束，超了会精简。' },
  { step: '04', title: '合成音频', body: '流式语音合成，取服务端返回的轮次时间戳当时间轴 —— 不用猜语速。' },
  { step: '05', title: '合成视频', body: '为每一段选好配图，逐帧渲染后与音频合轨，输出完整 MP4。' },
]

const WHY = [
  {
    icon: '◑',
    title: '双人对谈，不是念稿',
    body: '主播 B 代表听众追问、质疑、要求举例。脚本按口语文风约束写成，禁用「综上所述」这类书面语。',
  },
  {
    icon: '◧',
    title: '图文必须相符',
    body: '配图由模型为每一段单独指定；对不上论文原图的段落，会现场生成一张只讲这段内容的示意图。',
  },
  {
    icon: '⇄',
    title: '中英双语，共用配图',
    body: '同一份 PDF 换语言不换图，但脚本、解读、信息图、音频、视频各出一份，各用各的音色。',
  },
  {
    icon: '↻',
    title: '判错了能一键改回来',
    body: '「图正不正」最终只能靠人眼，所以配图可以手动旋转或删除，改完视频可以重新合成。',
  },
]

const MEASURED = [
  { value: '±10%', label: '时长落点', hint: '选 3 分钟得到约 3.2 分钟' },
  { value: '<0.12s', label: '音视频时长误差', hint: '逐帧比对，不是估计值' },
  { value: '×2', label: '双语成本', hint: '解读/脚本/图/音频/视频各跑两遍' },
  { value: '300', label: 'TTS 单轮上限（字符）', hint: '超了会被接口直接拒绝' },
]

const FAQ = [
  {
    q: '没有 API 密钥能跑吗？',
    a: '能。缺哪个密钥，哪一步就自动降级为 Mock：解读和脚本用内置示例数据，音频是占位音。整条链路可以完整走通，用来联调前端或做演示。',
  },
  {
    q: '和「AI 论文总结」有什么不一样？',
    a: '产出的是双人对谈播客而不是一篇文章。B 代表听众追问质疑，脚本按口语文风写成，可以直接听；同时给出视频、音频、逐段脚本和结构化解读四种形态。',
  },
  {
    q: '双语是一份内容翻两遍吗？',
    a: '不是。英文版有独立的解读、脚本、信息图和音色，用英文的 prompt 生成；只有封面和论文原图共用 —— 同一份 PDF，换个语言不该换图。',
  },
  {
    q: '配图为什么是 SVG 而不是 HTML 截图？',
    a: '实测 HTML → 图片这条路在本机不可用（Chrome headless 会挂死、Playwright 装浏览器超时），而且截图产物是死图。SVG 不依赖浏览器就能栅格化，还原生支持 SMIL 动画。',
  },
  {
    q: '有哪些还没做到？',
    a: '时长是估算不是硬保证（靠生成后的扩写/精简兜底）；链接抓取遇到知网这类要登录的站点会失败，扫描版 PDF 需要先 OCR；音频存在本地磁盘，多实例部署要换成对象存储；界面文案目前只有中文。',
  },
]

const REPO_URL = 'https://github.com/qingkelab/paper-podcast'

/**
 * 「分享这个项目到 X」。
 *
 * 地址指的是**首页**，而不是当前页：用户在首页底部点它，就该分享首页 ——
 * 分享 `/create` 或某个带查询串的地址，别人点开看到的是一个空表单，很怪。
 * 用 `router.resolve` + `new URL(href, location.href)` 拼，这样 hash 模式
 * 与子路径部署（GitHub Pages）都对（同 EpisodeView 里分享链接的处理）。
 */
const projectShareText = computed(() => {
  // router 在 setup 阶段就取好：useRouter() 内部是 inject，
  // 放进 computed 里执行会脱离 setup 上下文（只在渲染时才第一次调用，会直接报错）
  const resolved = router.resolve({ name: 'landing' })
  const href = typeof window === 'undefined' ? resolved.href : new URL(resolved.href, window.location.href).href
  return buildProductShareText(href)
})

onMounted(() => {
  void meta.load()
  void loadShowcase()
})
</script>

<template>
  <div class="lp">
    <!-- ============================ 首屏 ============================ -->
    <section class="lp-hero">
      <div class="lp-hero__bg" aria-hidden="true"></div>
      <div class="lp-container lp-hero__inner">
        <div class="lp-hero__copy rise">
          <p class="lp-eyebrow">
            <span class="lp-eyebrow__dot" aria-hidden="true"></span>
            论文 → 播客 · 双人对谈
            <span v-if="meta.showMockBadge" class="lp-eyebrow__tag">离线演示</span>
          </p>

          <h1 class="lp-hero__title">
            你的论文，<br />
            值得被<span class="lp-accent">听懂</span>一遍
          </h1>

          <p class="lp-hero__lede">
            上传一篇论文，自动生成一期双人对谈的解读播客：先出结构化解读，再写成口语脚本，
            配上论文原图和生成信息图，最后合成竖版视频。中文英文各一版，切换器一键换。
          </p>

          <div class="lp-hero__cta">
            <RouterLink to="/create" class="btn btn--primary btn--lg">生成一期播客</RouterLink>
            <button
              v-if="showcase"
              type="button"
              class="btn btn--ghost btn--lg"
              @click="scrollToShowcase"
            >
              先看一期成品
            </button>
            <RouterLink v-else to="/library" class="btn btn--ghost btn--lg">看看播客库</RouterLink>
          </div>

          <ul class="lp-proof">
            <li v-for="item in PROOF" :key="item.label" class="lp-proof__item">
              <span class="lp-proof__label">{{ item.label }}</span>
              <span class="lp-proof__hint">{{ item.hint }}</span>
            </li>
          </ul>
        </div>

        <!-- 首屏视觉：真实封面 + 就地可播的真实视频（自带语言开关，见 .lp-shot__lang） -->
        <div class="lp-hero__visual rise">
          <div class="lp-shot" :class="{ 'is-playing': heroPlaying }">
            <video
              v-if="heroVideoUrl"
              :key="`hero-${heroLanguage ?? 'zh'}`"
              ref="videoRef"
              class="lp-shot__video"
              :poster="showcase?.posterUrl ?? undefined"
              :src="heroVideoUrl"
              preload="none"
              playsinline
              controls
            ></video>
            <img
              v-else-if="showcase?.posterUrl"
              class="lp-shot__video"
              :src="showcase.posterUrl"
              alt="论文 PDF 首页渲染出的封面"
            />
            <div v-else class="lp-shot__placeholder" aria-hidden="true">
              <span class="lp-shot__placeholder-mark">播</span>
            </div>

            <button
              v-if="heroVideoUrl && !heroPlaying"
              type="button"
              class="lp-shot__play"
              aria-label="在首页直接播放这一期的视频解读"
              @click="playHero"
            >
              <span class="lp-shot__play-icon" aria-hidden="true">▶</span>
              <span class="lp-shot__play-text">播放这一期</span>
            </button>

            <span v-if="showcase && !heroPlaying" class="lp-shot__badge">936 × 1210</span>

            <!-- 首屏自己的语言开关：只换上面这段视频，不动下面的成品展示区 -->
            <LanguageSwitch
              v-if="showcaseLanguages.length > 1"
              class="lp-shot__lang"
              overlay
              :languages="showcaseLanguages"
              :model-value="heroLanguage"
              :hint="languageSwitchHint"
              @update:model-value="selectHeroLanguage"
            />
          </div>

          <!-- 浮动信息卡：把「这一期到底产出了什么」摊开给人看 -->
          <div v-if="showcase" class="lp-float lp-float--audio">
            <span class="lp-float__icon" aria-hidden="true">♪</span>
            <span class="lp-float__body">
              <span class="lp-float__title">
                {{ heroLanguage ? languageLabel(heroLanguage) : '' }}版 · 双人对谈
              </span>
              <span class="lp-float__meta">
                {{ formatDuration(heroDuration) }}<template v-if="showcaseLanguages.length > 1">
                  · 另有 {{ showcaseLanguages.length - 1 }} 个语言版本</template
                >
              </span>
            </span>
          </div>

          <div v-if="showcase && showcase.figures.length" class="lp-float lp-float--figures">
            <span class="lp-float__icon" aria-hidden="true">◧</span>
            <span class="lp-float__body">
              <span class="lp-float__title">配图 {{ showcase.figures.length }} 张</span>
              <span class="lp-float__meta">论文原图 + 生成信息图</span>
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- ========================= 四种产物 ========================= -->
    <section class="lp-section lp-section--tight">
      <div class="lp-container">
        <header class="lp-head">
          <p class="lp-head__eyebrow">Output</p>
          <h2 class="lp-head__title">一次生成，四种形态</h2>
          <p class="lp-head__lede">同一份脚本，既是能听的节目，也是能查的文字。</p>
        </header>
        <div class="lp-grid lp-grid--4">
          <article v-for="item in OUTPUTS" :key="item.title" class="lp-card">
            <span class="lp-card__icon" aria-hidden="true">{{ item.icon }}</span>
            <h3 class="lp-card__title">{{ item.title }}</h3>
            <p class="lp-card__body">{{ item.body }}</p>
          </article>
        </div>
      </div>
    </section>

    <!-- ===================== 真实成品展示 ===================== -->
    <section v-if="showcaseLoading || showcase" id="showcase" class="lp-section lp-show">
      <div class="lp-container">
        <div class="lp-show__head">
          <header class="lp-head lp-head--left">
            <p class="lp-head__eyebrow">Showcase</p>
            <h2 class="lp-head__title">看一期真正的成品</h2>
            <p class="lp-head__lede">
              下面所有东西都来自库里真实生成的一期，不是示意图：视频、音频、脚本、解读、
              配图都是它的产物。中英两版各有独立的脚本和音视频 —— 这里的按钮换的是这一整块；
              首屏视频上的那个按钮只换首屏那段视频。
            </p>
          </header>

          <!-- 语言切换器：只有这一集真的产出了多个语言版本时才出现 -->
          <LanguageSwitch
            v-if="showcaseLanguages.length > 1"
            class="lp-show__lang"
            label="语言版本"
            :languages="showcaseLanguages"
            :model-value="activeLanguage"
            :hint="showcaseSwitchHint"
            @update:model-value="selectShowcaseLanguage"
          />
        </div>

        <div v-if="showcaseLoading" class="lp-show__loading">
          <span class="skeleton" style="height: 320px"></span>
        </div>

        <p v-if="showcase && !showcaseDetailReady" class="lp-show__pending">
          正在载入这一期的视频、配图与脚本…
        </p>

        <div v-else-if="showcase" class="lp-show__grid">
          <!-- 视频 -->
          <div class="lp-show__panel lp-show__panel--player">
            <div class="lp-show__panel-head">
              <span class="lp-chip">视频解读播客</span>
              <span v-if="episodeDuration" class="lp-show__meta">{{ formatDuration(episodeDuration) }}</span>
            </div>
            <video
              v-if="videoUrl"
              :key="activeLanguage ?? 'zh'"
              class="lp-show__video"
              :src="videoUrl"
              :poster="showcase.posterUrl ?? undefined"
              preload="none"
              playsinline
              controls
            ></video>
            <img
              v-else-if="showcase.posterUrl"
              class="lp-show__video"
              :src="showcase.posterUrl"
              alt="这一期的封面"
            />
            <p class="lp-show__note">竖版 936×1210，画面逐段切换，字幕按主播分色。</p>
          </div>

          <!-- 右栏：这一期的信息 + 音频 + 脚本 + 解读 -->
          <div class="lp-show__panel lp-show__panel--side">
            <RouterLink class="lp-show__title" :to="`/episode/${showcase.episodeId}`">
              {{ showcase.title }}
            </RouterLink>
            <div class="lp-show__tags">
              <span v-if="showcase.venue" class="lp-chip">
                {{ showcase.venue }}
              </span>
              <span v-if="showcase.year" class="lp-chip">
                {{ showcase.year }}
              </span>
              <span class="lp-chip lp-chip--accent">{{ languageNamesText }}</span>
            </div>

            <div class="lp-show__audio">
              <span class="lp-show__audio-label" aria-hidden="true">
                ♪ {{ activeLanguage ? languageLabel(activeLanguage) : '' }}版音频
              </span>
              <audio
                v-if="audioUrl"
                :key="`audio-${activeLanguage ?? 'zh'}`"
                class="lp-show__audio-el"
                :src="audioUrl"
                preload="none"
                controls
              ></audio>
            </div>

            <div v-if="scriptSegments.length" class="lp-show__block">
              <p class="lp-show__block-title">脚本片段</p>
              <ul class="lp-script">
                <li
                  v-for="(segment, index) in scriptSegments"
                  :key="index"
                  class="lp-script__row"
                  :class="`lp-script__row--${segment.speaker.toLowerCase()}`"
                >
                  <span class="lp-script__who">{{ segment.speaker }}</span>
                  <span class="lp-script__text">{{ segment.text }}</span>
                </li>
              </ul>
            </div>

            <div v-if="analysisPreview.length" class="lp-show__block">
              <p class="lp-show__block-title">解读片段</p>
              <div v-for="row in analysisPreview" :key="row.label" class="lp-analysis">
                <span class="lp-analysis__label">{{ row.label }}</span>
                <p class="lp-analysis__text">{{ row.text }}</p>
              </div>
            </div>
          </div>

          <!-- 配图三件套 -->
          <div class="lp-show__panel lp-show__panel--art">
            <div class="lp-show__panel-head">
              <span class="lp-chip">配图</span>
              <span class="lp-show__meta">封面 / 论文原图 / 生成信息图</span>
            </div>
            <div class="lp-art">
              <figure v-if="showcase.posterUrl" class="lp-art__item">
                <img class="lp-art__img" :src="showcase.posterUrl" alt="论文 PDF 第一页渲染出的封面" />
                <figcaption class="lp-art__cap">
                  <strong>封面</strong>论文 PDF 第一页整页渲染，也就是视频的画幅来源。
                </figcaption>
              </figure>

              <figure v-for="figure in showcase.figures.slice(0, 3)" :key="figure.id" class="lp-art__item">
                <img class="lp-art__img" :src="figure.url" :alt="figure.caption || figure.label" />
                <figcaption class="lp-art__cap">
                  <strong>{{ figure.label }}</strong>按图注从 PDF 里定位并渲染出的原图。
                </figcaption>
              </figure>

              <figure v-if="illustration" class="lp-art__item">
                <!-- SVG 用 <object> 引：<img> 里 SMIL 动画不一定会跑 -->
                <object
                  class="lp-art__img lp-art__img--svg"
                  type="image/svg+xml"
                  :data="illustration.svg_url"
                  :aria-label="'模型生成的信息图'"
                ></object>
                <figcaption class="lp-art__cap">
                  <strong>生成信息图</strong>模型画的 SVG，带 SMIL 动画，正在动。
                </figcaption>
              </figure>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ========================= 流水线 ========================= -->
    <section class="lp-section">
      <div class="lp-container">
        <header class="lp-head">
          <p class="lp-head__eyebrow">Pipeline</p>
          <h2 class="lp-head__title">五步，串行跑完</h2>
          <p class="lp-head__lede">每一步的产物都落库，失败可以单独重试，不用从头再来。</p>
        </header>
        <ol class="lp-flow">
          <li v-for="item in PIPELINE" :key="item.step" class="lp-flow__item">
            <span class="lp-flow__num" aria-hidden="true">{{ item.step }}</span>
            <h3 class="lp-flow__title">{{ item.title }}</h3>
            <p class="lp-flow__body">{{ item.body }}</p>
          </li>
        </ol>
      </div>
    </section>

    <!-- ======================= 为什么不一样 ======================= -->
    <section class="lp-section">
      <div class="lp-container">
        <header class="lp-head">
          <p class="lp-head__eyebrow">Why</p>
          <h2 class="lp-head__title">和「论文总结」不一样的地方</h2>
        </header>
        <div class="lp-grid lp-grid--2">
          <article v-for="item in WHY" :key="item.title" class="lp-why">
            <span class="lp-why__icon" aria-hidden="true">{{ item.icon }}</span>
            <div>
              <h3 class="lp-why__title">{{ item.title }}</h3>
              <p class="lp-why__body">{{ item.body }}</p>
            </div>
          </article>
        </div>
      </div>
    </section>

    <!-- ========================= 实测数字 ========================= -->
    <section class="lp-section lp-section--tight">
      <div class="lp-container">
        <div class="lp-stats">
          <div v-for="item in MEASURED" :key="item.label" class="lp-stat">
            <div class="lp-stat__value">{{ item.value }}</div>
            <div class="lp-stat__label">{{ item.label }}</div>
            <p class="lp-stat__hint">{{ item.hint }}</p>
          </div>
        </div>
        <p class="lp-stats__foot">
          这些数字来自真实合成结果，不是估计：语速常量由成片反推，视频时长误差是逐帧比对出来的。
          想核实可以直接读仓库里的 AGENTS.md。
        </p>
      </div>
    </section>

    <!-- =========================== FAQ =========================== -->
    <section class="lp-section">
      <div class="lp-container lp-faq-wrap">
        <header class="lp-head lp-head--left">
          <p class="lp-head__eyebrow">FAQ</p>
          <h2 class="lp-head__title">常见问题</h2>
        </header>
        <div class="lp-faq">
          <details v-for="item in FAQ" :key="item.q" class="lp-faq__item">
            <summary class="lp-faq__q">{{ item.q }}</summary>
            <p class="lp-faq__a">{{ item.a }}</p>
          </details>
        </div>
      </div>
    </section>

    <!-- ========================= 底部 CTA ========================= -->
    <section class="lp-cta">
      <div class="lp-cta__bg" aria-hidden="true"></div>
      <div class="lp-container lp-cta__inner">
        <h2 class="lp-cta__title">现在就把手边那篇论文丢进来</h2>
        <p class="lp-cta__lede">
          没有 API 密钥也能先跑一遍：解读和脚本会用内置示例数据，整条链路照样走得通。
        </p>
        <div class="lp-cta__actions">
          <RouterLink to="/create" class="btn btn--primary btn--lg">生成一期播客</RouterLink>
          <!-- 觉得有用就顺手发一条：预填好文案，用户确认后自己发（我们不代发） -->
          <ShareToXButton :text="projectShareText" label="分享到 X" size="md" />
          <a class="btn btn--ghost btn--lg" :href="REPO_URL" target="_blank" rel="noopener noreferrer">
            GitHub 仓库
          </a>
        </div>
        <p v-if="IS_MOCK" class="lp-cta__note">
          你现在看的是离线演示站：用 <code>VITE_USE_MOCK=1</code> 构建，
          没有后端也能点通完整流程，但产出的是内置示例内容。
        </p>
      </div>
    </section>
  </div>
</template>
