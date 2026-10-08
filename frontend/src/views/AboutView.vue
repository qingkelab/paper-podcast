<script setup lang="ts">
/**
 * 项目介绍页。
 *
 * 刻意不写成营销页：这个项目的可信度来自**实测数字**和**诚实的限制说明**，
 * 所以页面上凡是涉及性能/时长/成本的数字都标了来源，已知限制单独一节列出。
 * 全部内容都是静态的，离线演示（GitHub Pages）与真实后端下看到的一模一样。
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { IS_MOCK } from '../api'
import { useMetaStore } from '../stores/meta'

const meta = useMetaStore()

const REPO_URL = 'https://github.com/qingkelab/paper-podcast'
const DEMO_URL = 'https://qingkelab.github.io/paper-podcast/'

const isMockBadge = computed(() => meta.showMockBadge)
const version = computed(() => meta.version)
const modes = computed(() => meta.modes)

/** 顶部概览数字。都是这个仓库里能核实的东西，不是宣传口径。 */
const HIGHLIGHTS = [
  { value: '5', unit: '步', label: '流水线', hint: '解析 → 解读 → 脚本 → 音频 → 视频' },
  { value: '3', unit: '种', label: '配图', hint: '论文首页 / 论文原图 / 生成信息图' },
  { value: '2', unit: '版', label: '语言', hint: '同一集内嵌中英，切换器一键换' },
  { value: '320', unit: '项', label: '测试', hint: '后端 pytest 全绿，改完必须过' },
]

/** 一集播客会产出的东西。每项都注明「用户拿到的是什么」。 */
const OUTPUTS = [
  {
    icon: '▶',
    title: '视频解读播客',
    body: '把配图和音频烘焙成一个竖版 MP4，画幅就是论文首页尺寸。画面按脚本逐段切换，底部字幕按主播分色。',
    facts: ['936 × 1210 · 白底', '字幕与音频轮次精确对齐', '片尾是社区品牌卡 + 关注引导'],
  },
  {
    icon: '♪',
    title: '双人对谈音频',
    body: '主播 A 讲解、主播 B 追问质疑，带语气起伏的双人对话。开头结尾是社区自己的品牌话术，没有罐头背景乐。',
    facts: ['豆包播客 TTS（WebSocket 流式）', '中英各自配对音色', '支持 Range 请求，可拖进度条'],
  },
  {
    icon: '¶',
    title: '逐段脚本',
    body: '完整的双人对谈脚本，可用于校对、复用或二次剪辑。字幕就是从它逐段生成的，所以听完能对着文字核对。',
    facts: ['每段一个发言轮次', '可导出 .txt', '双语时按语言各导一份'],
  },
  {
    icon: '◫',
    title: '结构化解读',
    body: '八个板块的论文解读：背景、创新点、方法、实验、结论、不足、价值、未来方向。它先于脚本生成，是脚本的输入。',
    facts: ['JSON 结构化，可直接接下游', '可导出 .md', '反 AI 腔负面清单约束文风'],
  },
]

/** 流水线。写清每步「做什么 / 用什么 / 会失败在哪」。 */
const PIPELINE = [
  {
    step: '01',
    title: '解析论文',
    input: 'PDF / arXiv 链接 / 粘贴文本',
    body: '抽出干净正文并截断，同时渲染封面和论文原图。配图不是调 get_images 拿位图 —— 论文里的图多是矢量图形，必须按图注定位再渲染区域，否则几乎一张都提不到。',
    notes: ['参考文献与页眉页脚会被切掉', '图注在图下方、表注在表上方', '整体转了的图会自动摆正'],
  },
  {
    step: '02',
    title: '深度解读',
    input: '清洗后的正文',
    body: '一次调用产出八个板块的结构化解读。刻意和脚本分成两步：一次性输出「解读+脚本」时，模型的注意力会跑到文风上，解读深度明显下降。',
    notes: ['JSON 容错解析（模型常包 markdown 围栏）', '附上并发一次信息图生成'],
  },
  {
    step: '03',
    title: '生成脚本',
    input: '结构化解读',
    body: '把解读改编成双人口语对谈。时长靠字数控制：把目标分钟数按实测语速换算成字数区间写进 prompt 并声明为硬约束，生成后再检查一次，不达标补一次扩写或精简。',
    notes: ['反 AI 腔负面清单（禁用「综上所述」这类词）', '超长发言会被切分，规避 TTS 单轮上限'],
  },
  {
    step: '04',
    title: '合成音频',
    input: '逐段脚本',
    body: '豆包播客 TTS 走 WebSocket 长连，音频按轮次流式返回后自行拼接，并拿服务端返回的轮次时间戳当时间轴 —— 不用猜语速。',
    notes: ['收到 150 后必须立刻发 FinishSession，否则永远等不到 152', '实测语速：中文 350 字/分、英文 141 词/分'],
  },
  {
    step: '05',
    title: '合成视频',
    input: '脚本 + 时间轴 + 配图',
    body: '为每一段选图：能对上论文原图的用原图，对不上的现场生成一张只讲这段内容的示意图，最后才用中性图兜底。然后渲染画面、编码。',
    notes: ['配图逐段指定，避免「聊到别的话题画面还没换」', '分两步编码，实测音视频时长差 < 0.12 秒'],
  },
]

/** 关键设计决策。每条都是「踩过坑之后定下来的」。 */
const DECISIONS = [
  {
    title: '两段式生成，而不是一次到位',
    body: '先出结构化解读，再基于解读写脚本。任一步失败可以单独重试，每步的约束也更紧。',
  },
  {
    title: '时长按字数控，不按感觉',
    body: '语速常量是从真实合成结果反推的，不是「中文播客常识」。当前实测落点约 ±10%。',
  },
  {
    title: '图文必须相符',
    body: '配图由模型**为每一段**指定，不是「从第 N 段开始一直用到下一张图」——后者中间话题变了图却没变。',
  },
  {
    title: '判错了要能一键改回来',
    body: '「图正不正」最终只能靠人眼，所以有手动旋转/删除接口。改完视频会提示可以重新合成。',
  },
  {
    title: '静态资源 URL 带版本号',
    body: '配图/封面/音频的 URL 是固定的，内容却会变。只按 URL 长缓存会让人一直看到旧图——踩过。',
  },
  {
    title: '中英双语共用配图、分开声音',
    body: '同一份 PDF 换语言不该换图，但文字相关的（脚本/解读/信息图/音频/视频）各出一份。',
  },
  {
    title: '老数据不改也能用',
    body: '顶层字段永远镜像主语言那一版。双语之前生成的集没有语言版本记录，接口把顶层字段当成主语言回填。',
  },
  {
    title: '视频合成分两步编码',
    body: '一条命令把幻灯片和音频一起编码无法对齐长度：实测会多 2.4 秒或少 12 秒。先出纯视频轨再封音频。',
  },
]

const STACK = [
  { layer: '前端', value: 'Vue 3 + Vite + TypeScript + Pinia' },
  { layer: '后端', value: 'Python 3.13 + FastAPI' },
  { layer: '存储', value: 'SQLite（标准库 sqlite3，WAL 模式，无 ORM）' },
  { layer: '大模型', value: 'DeepSeek（默认）/ 豆包方舟，OpenAI 兼容' },
  { layer: '语音合成', value: '豆包语音播客 PodcastTTS（WebSocket）' },
  { layer: '图像与视频', value: 'PyMuPDF + resvg + ffmpeg' },
]

/** 实测数据。每条都注明是怎么量出来的，避免变成「据说」。 */
const MEASURED = [
  {
    label: '时长落点',
    value: '约 ±10%',
    detail: '选 3 分钟得到约 3.2 分钟。语速常量（中文 350 字/分、英文 141 词/分）由真实合成结果反推。',
  },
  {
    label: '视频时长误差',
    value: '< 0.12 秒',
    detail: '两步编码后的 MP4 与音频轨逐帧比对（182.904s vs 183.020s）。',
  },
  {
    label: '双语成本',
    value: '约 2 倍',
    detail: '解读、脚本、信息图、音频、视频各跑两遍。实测一期 3 分钟双语约 5 分钟。',
  },
  {
    label: 'TTS 单轮上限',
    value: '300 字符',
    detail: '超了直接报 40000010。英文一段 50 个词就有 300 字符，中文 120 字撞不到。',
  },
]

const LIMITS = [
  '时长是估算，不是硬保证——模型对字数预算的遵守程度不稳定，靠生成后的扩写/精简兜底。',
  '音色目录是硬编码的官方配对音色，接真实账号前建议按控制台核对一遍。',
  '知网等需要登录的站点抓不到链接，扫描版 PDF 需要先做 OCR。',
  '音频存在本地磁盘，多实例部署要换成对象存储；断点续传的协议支持但客户端还没实现。',
  '界面文案目前只有中文——英文版是「内容可以听英文」，不是完整的产品本地化。',
]

const FAQ = [
  {
    q: '和「AI 论文总结」有什么不一样？',
    a: '产出的是双人对谈播客而不是一篇文章。主播 B 代表听众追问、质疑、要求举例，脚本按口语文风约束写成，可以直接听。另外它同时给出视频、音频、逐段脚本和结构化解读四种形态。',
  },
  {
    q: '没有 API 密钥能跑吗？',
    a: '能。缺哪个密钥，哪一步就自动降级为 Mock：解读和脚本用内置示例数据，音频是占位音。整条链路可以完整走通，用来联调前端或做演示。',
  },
  {
    q: '为什么配图是 SVG 而不是 HTML 截图？',
    a: '实测 HTML → 图片这条路线在本机不可用（Chrome headless 会挂死、Playwright 装浏览器超时），而且截图产物是死图。SVG 不依赖浏览器就能栅格化，还原生支持 SMIL 动画。',
  },
  {
    q: '双语是一份内容翻两遍吗？',
    a: '不是。英文版有独立的解读、脚本、信息图和音色，用英文的 prompt 写；只有封面和论文原图是共用的——同一份 PDF，换个语言不该换图。',
  },
]
</script>

<template>
  <div class="container page">
    <header class="page__head">
      <p class="eyebrow">About · 项目介绍</p>
      <h1 class="page-title">一篇论文进去，一期双人播客出来</h1>
      <p class="page-subtitle">
        把论文变成能听的节目：PDF / arXiv 链接 / 粘贴文本进来，依次完成解析、深度解读、
        双人口语脚本、语音合成，最后把配图和音频合成为一个完整的视频解读播客。
        中英双语、逐段配图、人工校正方向，都在里面。
      </p>
      <div class="page__actions">
        <RouterLink to="/" class="btn btn--primary">导入一篇论文</RouterLink>
        <RouterLink to="/library" class="btn btn--ghost">看已有的播客</RouterLink>
      </div>
    </header>

    <!-- 运行时状态：放在最前面，让人一眼知道现在看到的是什么 -->
    <div class="alert alert--info" style="margin-bottom: 24px">
      <span class="alert__icon" aria-hidden="true">i</span>
      <span class="alert__body">
        <template v-if="IS_MOCK">
          当前是<strong>离线演示</strong>：页面完全跑在浏览器里，用内置示例数据，
          点得通完整流程但不会真的生成新播客。
        </template>
        <template v-else-if="modes">
          已连上后端：文本解读 {{ modes.llm }} · 音频合成 {{ modes.tts }}<template v-if="version">
            · 版本 {{ version }}</template
          >。
        </template>
        <template v-else>后端状态未取到，页面上的参数会使用契约默认值。</template>
      </span>
    </div>

    <div class="stat-grid">
      <div v-for="item in HIGHLIGHTS" :key="item.label" class="stat">
        <div class="stat__value">
          {{ item.value }}<span class="stat__unit">{{ item.unit }}</span>
        </div>
        <div class="stat__label">{{ item.label }}</div>
        <p class="stat__hint">{{ item.hint }}</p>
      </div>
    </div>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">一集会产出什么</h2>
        <span class="section__hint">四种形态，来自同一次生成</span>
      </div>
      <div class="analysis-grid">
        <article v-for="item in OUTPUTS" :key="item.title" class="analysis-card">
          <div class="analysis-card__head">
            <span class="analysis-card__index" aria-hidden="true">{{ item.icon }}</span>
            <h3 class="analysis-card__title">{{ item.title }}</h3>
          </div>
          <div class="analysis-card__body">
            <p>{{ item.body }}</p>
          </div>
          <ul class="fact-list">
            <li v-for="fact in item.facts" :key="fact">{{ fact }}</li>
          </ul>
        </article>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">流水线</h2>
        <span class="section__hint">串行执行，每步的产物都落库，可单独重试</span>
      </div>
      <ol class="flow">
        <li v-for="item in PIPELINE" :key="item.step" class="flow__item">
          <div class="flow__marker" aria-hidden="true">{{ item.step }}</div>
          <div class="flow__body">
            <div class="flow__head">
              <h3 class="flow__title">{{ item.title }}</h3>
              <span class="badge badge--neutral">{{ item.input }}</span>
            </div>
            <p class="flow__text">{{ item.body }}</p>
            <ul class="fact-list fact-list--tight">
              <li v-for="note in item.notes" :key="note">{{ note }}</li>
            </ul>
          </div>
        </li>
      </ol>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">关键设计决策</h2>
        <span class="section__hint">每条都是踩过坑之后定下来的</span>
      </div>
      <div class="decision-grid">
        <div v-for="item in DECISIONS" :key="item.title" class="card card--pad decision">
          <h3 class="decision__title">{{ item.title }}</h3>
          <p class="decision__body">{{ item.body }}</p>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">实测数据</h2>
        <span class="section__hint">数字都注明是怎么量出来的</span>
      </div>
      <div class="card card--pad">
        <table class="data-table">
          <thead>
            <tr>
              <th scope="col">项目</th>
              <th scope="col">数值</th>
              <th scope="col">怎么得到的</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in MEASURED" :key="row.label">
              <th scope="row">{{ row.label }}</th>
              <td class="data-table__value">{{ row.value }}</td>
              <td>{{ row.detail }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">技术栈</h2>
        <span class="section__hint">刻意保持依赖少、能离线跑</span>
      </div>
      <div class="card card--pad">
        <div class="kv">
          <div v-for="row in STACK" :key="row.layer" class="kv__row">
            <span class="kv__key">{{ row.layer }}</span>
            <span class="kv__value">{{ row.value }}</span>
          </div>
        </div>
        <hr class="divider" />
        <p class="section__hint" style="margin: 0">
          存储刻意用标准库 sqlite3 而不是 ORM：整个应用只有一张表，ORM 带来的迁移负担大于收益。
          前端不依赖任何 UI 组件库和字体 CDN，样式是手写的深色设计系统，离线也能完整渲染。
        </p>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">已知限制</h2>
        <span class="section__hint">这些是真的还没做到，不是谦虚</span>
      </div>
      <div class="card card--pad">
        <ul class="fact-list">
          <li v-for="item in LIMITS" :key="item">{{ item }}</li>
        </ul>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">常见问题</h2>
      </div>
      <div class="faq">
        <details v-for="item in FAQ" :key="item.q" class="faq__item">
          <summary class="faq__q">{{ item.q }}</summary>
          <p class="faq__a">{{ item.a }}</p>
        </details>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">跑起来</h2>
        <span class="section__hint">零密钥也能启动，缺哪一步就降级为 Mock</span>
      </div>
      <div class="card card--pad">
        <pre class="code-block"><code>git clone {{ REPO_URL }}.git
cd paper-podcast

# 系统依赖：视频合成需要 ffmpeg（不是 Python 包）
brew install ffmpeg        # Linux: apt install ffmpeg

# 后端
cd backend
uv venv --python 3.13 ../.venv
uv pip install --python ../.venv/bin/python -r requirements.txt
../.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# 前端（另开一个终端）
cd frontend && pnpm install && pnpm dev</code></pre>
        <div class="row row--between" style="margin-top: 16px">
          <p class="section__hint" style="margin: 0">
            前端构建产物放在 <code>frontend/dist</code> 时，后端启动会自动托管它，单端口即可访问。
          </p>
          <div class="row">
            <a class="btn btn--ghost" :href="REPO_URL" target="_blank" rel="noopener noreferrer">
              GitHub 仓库
            </a>
            <a class="btn btn--ghost" :href="DEMO_URL" target="_blank" rel="noopener noreferrer">
              离线演示站
            </a>
          </div>
        </div>
      </div>
      <div v-if="isMockBadge" class="alert alert--info" style="margin-top: 18px">
        <span class="alert__icon" aria-hidden="true">◈</span>
        <span class="alert__body">
          你现在看的就是离线演示站。它用 <code>VITE_USE_MOCK=1</code> 构建，
          没有后端也能点通「导入 → 进度 → 脚本 → 播放器 → 配图 → 视频区」，
          但产出的是内置示例内容。
        </span>
      </div>
    </section>
  </div>
</template>
