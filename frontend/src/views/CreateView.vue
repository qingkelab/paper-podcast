<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  IS_MOCK,
  createEpisodeBatch,
  createEpisodeFromFile,
  createEpisodeFromText,
  createEpisodeFromUrl,
  errorMessage,
  getUsage,
} from '../api'
import type {
  BatchResult,
  Episode,
  EpisodeLanguage,
  EpisodeOptionsInput,
  LevelValue,
  UsagePayload,
} from '../api'
import { useMetaStore } from '../stores/meta'
import { usePreferencesStore } from '../stores/preferences'
import { formatBytes } from '../utils/format'
import { LANGUAGE_OPTIONS, languagesText } from '../utils/language'
import { LEVEL_LABELS } from '../utils/stages'

type TabKey = 'pdf' | 'url' | 'text'

const router = useRouter()
const meta = useMetaStore()
const prefs = usePreferencesStore()

const TABS: Array<{ key: TabKey; label: string; icon: string }> = [
  { key: 'pdf', label: 'PDF 上传', icon: '⇪' },
  { key: 'url', label: '链接导入', icon: '⌘' },
  { key: 'text', label: '文本粘贴', icon: '¶' },
]

/** Mock 模式下的示例论文，点一下就能跑完整流程 */
const SAMPLES = [
  { title: 'Attention Is All You Need', url: 'https://arxiv.org/abs/1706.03762' },
  { title: 'Deep Residual Learning（ResNet）', url: 'https://arxiv.org/abs/1512.03385' },
  { title: 'LoRA: Low-Rank Adaptation', url: 'https://arxiv.org/abs/2106.09685' },
]

/** 契约 §2.8：一次最多 20 篇 */
const MAX_BATCH = 20
/** 批量里「文本」一篇的最短长度（真实后端 `_batch_from_json` 的要求） */
const BATCH_TEXT_MIN = 200

const active = ref<TabKey>('pdf')
const dragging = ref(false)
/** V2：一次可以选多个 PDF（多选 → 批量接口；单选 → 走原来的单篇接口） */
const files = ref<File[]>([])
const urlText = ref('')
const text = ref('')
const submitting = ref(false)
const error = ref<string | null>(null)
const picker = ref<HTMLInputElement | null>(null)

/**
 * 生成配额（`GET /api/usage`）。有配额却不告诉用户还剩多少，等于让人撞 429 才知道 ——
 * 尤其是批量提交，一口气填 10 条才发现没额度了。
 *
 * 这是 V2 后期新增的接口（docs/API.md 的冻结版里还没有），所以**fail-open**：
 * 读不到就整块不显示，绝不因为它把表单挡住。
 */
const usage = ref<UsagePayload | null>(null)

/** 批量提交的结果：成功与失败**分开显示**（契约 §2.8「单项失败不影响其他项」） */
const batchResult = ref<BatchResult | null>(null)
/** 单篇成功后的跳转目标（还没跳之前先显示结果，便于批量时逐个点进去看） */
const lastCreated = ref<Episode | null>(null)

// 参数区：初值来自 localStorage 里的偏好设置（契约 §5 默认值）
const duration = ref<number>(prefs.preferences.duration_min)
const level = ref<LevelValue>(prefs.preferences.level)
const voiceA = ref<string>(prefs.preferences.voice_a)
const voiceB = ref<string>(prefs.preferences.voice_b)

/**
 * 要生成哪些语言版本（契约 §1：新建时用 `options.languages` 指定，多选、至少一个）。
 *
 * 至少留一个：一个都不选时提交会变成「生成一集没有任何语言的播客」，
 * 所以最后一个被选中的语言点不掉（按钮会给出说明，而不是静默无效）。
 */
const languages = ref<EpisodeLanguage[]>([...prefs.preferences.languages])

const languagesHint = computed(() => {
  if (languages.value.length > 1) {
    return `将生成 ${languagesText(languages.value)} 两版：脚本、解读、音频、视频各自独立，配图共用一份`
  }
  return `只生成${languagesText(languages.value)}一版（详情页不会出现语言切换器）`
})

function toggleLanguage(language: EpisodeLanguage): void {
  if (languages.value.includes(language)) {
    if (languages.value.length === 1) return // 至少留一个
    setLanguages(languages.value.filter((item) => item !== language))
    return
  }
  // 保持契约里的顺序（中文在前、英文在后），而不是按点击先后
  setLanguages(
    LANGUAGE_OPTIONS.map((option) => option.value).filter(
      (value) => value === language || languages.value.includes(value),
    ),
  )
}

/**
 * 语言选择同时写回偏好（localStorage）：下次打开表单就是上次选的那几种语言。
 * 时长 / 难度 / 音色仍然只在设置页保存 —— 那是原有行为，这次不动。
 */
function setLanguages(next: EpisodeLanguage[]): void {
  languages.value = next
  prefs.update({ languages: next })
}

const durations = computed(() => meta.options.durations)
const levels = computed(() => meta.options.levels)

const recommendedPartner = computed(() => {
  const current = meta.options.voices.find((voice) => voice.id === voiceA.value)
  if (!current) return null
  return meta.options.voices.find((voice) => voice.pair === current.pair && voice.id !== current.id) ?? null
})

// ---------------------------------------------------------------------------
// V2：一次多篇（契约 §2.8）
// ---------------------------------------------------------------------------

/** 链接：一行一条 */
const urls = computed(() =>
  urlText.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean),
)

/**
 * 文本：用**单独一行**的 `===`（或 `---`）分隔多篇。
 * 为什么要有分隔符：后端 `texts` 是「一个元素一篇论文」的数组，
 * 如果整段只当一篇，粘贴两篇论文会变成一篇混在一起的解读。
 */
function splitTexts(raw: string): string[] {
  return raw
    .split(/\r?\n[ \t]*[-=]{3,}[ \t]*\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const texts = computed(() => splitTexts(text.value))

/** 这一次提交会创建几篇 */
const itemCount = computed(() => {
  if (active.value === 'pdf') return files.value.length
  if (active.value === 'url') return urls.value.length
  return texts.value.length
})

const tooMany = computed(() => itemCount.value > MAX_BATCH)

/** 批量文本里太短的那些（后端要求 ≥200 字，这里提前说清楚，免得白等一轮） */
const shortTexts = computed(() =>
  active.value === 'text' ? texts.value.filter((item) => item.length < BATCH_TEXT_MIN) : [],
)

/** 配额文案：remaining === null（或 limit <= 0）表示服务端没设上限 */
const usageText = computed(() => {
  const value = usage.value
  if (!value || value.limit <= 0) return null
  return `最近 24 小时已生成 ${value.used} / ${value.limit} 期，还能生成 ${value.remaining ?? 0} 期`
})

/** 这一批会超配额：提前说清楚，别让人提交完才吃一个 429 */
const overQuota = computed(() => {
  const value = usage.value
  if (!value || value.limit <= 0 || value.remaining === null) return false
  return itemCount.value > value.remaining
})

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (!languages.value.length) return false // 至少选一个语言版本
  if (tooMany.value) return false
  if (active.value === 'pdf') return files.value.length > 0
  if (active.value === 'url') return urls.value.length > 0
  return texts.value.length > 0 && text.value.trim().length >= 20
})

const submitLabel = computed(() => {
  const count = itemCount.value
  const many = count > 1
  if (active.value === 'pdf') {
    if (!count) return '请先选择 PDF 文件'
    return many ? `上传并生成 ${count} 篇播客` : '上传并生成播客'
  }
  if (active.value === 'url') {
    if (!count) return '抓取链接并生成播客'
    return many ? `抓取并生成 ${count} 篇播客` : '抓取链接并生成播客'
  }
  if (!count) return '解读这段文本并生成播客'
  return many ? `解读这 ${count} 篇并生成播客` : '解读这段文本并生成播客'
})

function options(): EpisodeOptionsInput {
  return {
    duration_min: duration.value,
    level: level.value,
    voice_a: voiceA.value,
    voice_b: voiceB.value,
    // 契约 §1：新建时用 options.languages 指定要哪些版本
    languages: [...languages.value],
  }
}

function openPicker(): void {
  picker.value?.click()
}

function acceptFiles(candidates: FileList | File[] | null | undefined): void {
  error.value = null
  batchResult.value = null
  if (!candidates) return
  const list = Array.from(candidates)
  const accepted: File[] = []
  const rejected: string[] = []
  list.forEach((candidate) => {
    if (/\.pdf$/i.test(candidate.name)) accepted.push(candidate)
    else rejected.push(candidate.name)
  })
  if (rejected.length) {
    error.value = `已跳过非 PDF 文件：${rejected.join('、')}`
  }
  // 同名文件只留一个（重复点选同一批文件是很常见的误操作）
  const merged = [...files.value]
  accepted.forEach((item) => {
    if (!merged.some((exist) => exist.name === item.name && exist.size === item.size)) merged.push(item)
  })
  if (merged.length > MAX_BATCH) {
    error.value = `一次最多 ${MAX_BATCH} 篇，已保留前 ${MAX_BATCH} 个文件`
  }
  files.value = merged.slice(0, MAX_BATCH)
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement
  acceptFiles(input.files)
  // 清掉 input 的值：否则再次选同一批文件时 change 不会触发
  input.value = ''
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  acceptFiles(event.dataTransfer?.files)
}

function removeFile(index: number): void {
  files.value = files.value.filter((_, position) => position !== index)
}

function clearFiles(): void {
  files.value = []
}

function resetResult(): void {
  batchResult.value = null
  lastCreated.value = null
  error.value = null
}

/**
 * 提交：一篇走原来的单篇接口（行为完全不变），多篇走批量接口。
 *
 * 单篇与批量是**两个接口**（契约 §2.1 / §2.8），批量的返回结构不一样
 * （`{ created, failed, total }`），所以不能用同一条路径糊过去。
 */
async function submit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = null
  batchResult.value = null
  try {
    if (itemCount.value === 1) {
      let episode: Episode
      if (active.value === 'pdf') {
        const single = files.value[0]
        if (!single) throw new Error('请先选择 PDF 文件')
        episode = await createEpisodeFromFile({ file: single, options: options() })
      } else if (active.value === 'url') {
        episode = await createEpisodeFromUrl({ url: urls.value[0] ?? '', options: options() })
      } else {
        episode = await createEpisodeFromText({ text: texts.value[0] ?? text.value.trim(), options: options() })
      }
      lastCreated.value = episode
      await router.push({ name: 'task', params: { id: episode.id } })
      return
    }

    // 多篇：批量接口，**单项失败不影响其他项**，结果分开显示
    const result =
      active.value === 'pdf'
        ? await createEpisodeBatch({ files: files.value, options: options() })
        : active.value === 'url'
          ? await createEpisodeBatch({ urls: urls.value, options: options() })
          : await createEpisodeBatch({ texts: texts.value, options: options() })
    batchResult.value = result
    // 配额已经变了（这一批扣掉了），重新取一次，别让提示停在旧数字上
    void loadUsage()
    // 只把**失败的那些**留在输入框里（成功的已经入队，留着很容易被再提交一遍，
    // 白烧一次模型和语音合成）。后端会把 ref 截断到 120 字符，所以按前缀比。
    const failedRefs = new Set(result.failed.map((item) => item.ref))
    if (active.value === 'url') {
      urlText.value = urls.value.filter((item) => failedRefs.has(item.slice(0, 120))).join('\n')
    } else if (active.value === 'pdf') {
      const failedNames = new Set(result.failed.map((item) => item.ref))
      files.value = files.value.filter((item) => failedNames.has(item.name))
    }
  } catch (cause) {
    error.value = errorMessage(cause, '创建任务失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

async function runSample(sample: { url: string }): Promise<void> {
  active.value = 'url'
  urlText.value = sample.url
  await submit()
}

async function loadUsage(): Promise<void> {
  try {
    usage.value = await getUsage()
  } catch {
    // 老后端没有这个接口 / 未登录 / 开放模式：当作「没有配额信息」，界面不显示这一行
    usage.value = null
  }
}

onMounted(() => {
  void meta.load()
  void loadUsage()
})
</script>

<template>
  <div class="container page">
    <header class="page__head">
      <p class="eyebrow">Create · 生成新播客</p>
      <h1 class="page-title">选好来源和参数，剩下的交给流水线</h1>
      <p class="page-subtitle">
        上传 PDF、贴一条 arXiv 链接，或者直接粘贴论文正文。系统会依次完成「解析论文 → 深度解读 → 生成脚本 →
        合成音频 → 合成视频」，产出可播放的双人对谈播客、逐段脚本与结构化解读。
        <strong>一次可以提交多篇</strong>：PDF 多选、链接一行一条，队列会逐篇处理。
      </p>
    </header>

    <div v-if="meta.error" class="alert alert--info" style="margin-bottom: 22px">
      <span class="alert__icon" aria-hidden="true">i</span>
      <span class="alert__body">{{ meta.error }}（下方参数已使用契约默认值）</span>
    </div>

    <div class="tabs" role="tablist" aria-label="导入方式">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        type="button"
        role="tab"
        class="tab"
        :class="{ 'is-active': active === tab.key }"
        :aria-selected="active === tab.key"
        @click="active = tab.key; resetResult()"
      >
        <span class="tab__icon" aria-hidden="true">{{ tab.icon }}</span>{{ tab.label }}
      </button>
    </div>

    <div class="card card--pad">
      <!-- PDF：多选 -->
      <div v-if="active === 'pdf'">
        <div
          class="dropzone"
          :class="{ 'is-over': dragging }"
          role="button"
          tabindex="0"
          aria-label="选择或拖拽 PDF 文件（可多选）"
          @click="openPicker"
          @keydown.enter.prevent="openPicker"
          @keydown.space.prevent="openPicker"
          @dragover.prevent="dragging = true"
          @dragenter.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop"
        >
          <div class="dropzone__icon" aria-hidden="true">⇪</div>
          <p class="dropzone__title">把 PDF 拖到这里，或点击选择文件（可多选）</p>
          <p class="dropzone__hint">仅支持 .pdf，单篇建议不超过 40MB，一次最多 {{ MAX_BATCH }} 篇</p>
        </div>
        <input
          ref="picker"
          class="audio-hidden"
          type="file"
          accept="application/pdf,.pdf"
          multiple
          @change="onPick"
        />

        <div v-if="files.length" class="row row--between" style="margin-top: 14px">
          <span class="section__hint" style="margin: 0">
            已选择 {{ files.length }} 个文件{{ files.length > 1 ? '（将走批量接口）' : '' }}
          </span>
          <button type="button" class="btn btn--sm btn--ghost" @click="clearFiles">清空</button>
        </div>
        <div v-if="files.length" class="stack" style="margin-top: 10px">
          <div v-for="(item, index) in files" :key="`${item.name}-${index}`" class="file-pill">
            <span aria-hidden="true">📄</span>
            <span class="file-pill__name">{{ item.name }}</span>
            <span class="file-pill__size">{{ formatBytes(item.size) }}</span>
            <span class="spacer" />
            <button type="button" class="btn btn--sm btn--ghost" @click="removeFile(index)">移除</button>
          </div>
        </div>
      </div>

      <!-- URL：一行一条 -->
      <div v-else-if="active === 'url'" class="field">
        <label class="field__label" for="url-input">论文链接（一行一条，可多条）</label>
        <textarea
          id="url-input"
          v-model="urlText"
          class="textarea"
          rows="5"
          spellcheck="false"
          placeholder="https://arxiv.org/abs/1706.03762&#10;https://arxiv.org/pdf/1512.03385"
        />
        <span class="field__hint">
          每一行一条链接；填 1 条走单篇接口，2 条以上自动走批量接口。
          抓不到的那条会单独报错，不影响其他条入队。
          后端会真的去抓取，链接不可达时那一条会进「失败」列表。
        </span>
        <span v-if="urls.length > 1" class="field__hint">当前识别到 {{ urls.length }} 条链接。</span>
      </div>

      <!-- 文本：用 === 分隔多篇 -->
      <div v-else class="field">
        <label class="field__label" for="text-input">论文正文</label>
        <textarea
          id="text-input"
          v-model="text"
          class="textarea"
          rows="8"
          placeholder="把摘要与正文粘贴到这里（建议包含方法与实验部分）…&#10;&#10;粘贴多篇时，用单独一行的 === 分隔&#10;&#10;===&#10;&#10;第二篇论文的正文…"
        />
        <span class="field__hint">
          单篇至少 20 字；<strong>批量（用 === 分隔多篇）时每篇至少 {{ BATCH_TEXT_MIN }} 字</strong>，
          这是后端批量接口的硬要求。
        </span>
        <span v-if="texts.length > 1" class="field__hint">
          当前识别到 {{ texts.length }} 篇（单篇分别生成，互不影响）。
        </span>
        <span v-if="shortTexts.length" class="field__hint" style="color: var(--danger)">
          有 {{ shortTexts.length }} 篇不足 {{ BATCH_TEXT_MIN }} 字，提交后这些会被后端判为失败
          （其余照样入队）。
        </span>
      </div>

      <div v-if="IS_MOCK" class="alert alert--accent" style="margin-top: 20px">
        <span class="alert__icon" aria-hidden="true">◈</span>
        <span class="alert__body">
          <span class="alert__title">离线 Mock 模式</span>
          当前没有连接后端：示例论文可以直接一键跑通，任意 PDF / 链接 / 文本会套用内置示例解读。
          批量里主机名带 <code>invalid</code> / <code>unreachable</code> 的链接会被模拟成「抓不到」，
          用来演示「单项失败不影响其他项」。
        </span>
      </div>

      <div v-if="IS_MOCK" class="chips" style="margin-top: 14px">
        <button
          v-for="sample in SAMPLES"
          :key="sample.url"
          type="button"
          class="chip"
          :disabled="submitting"
          @click="runSample(sample)"
        >
          ▶ {{ sample.title }}
        </button>
      </div>
    </div>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">播客参数</h2>
        <span class="section__hint">这里的初值来自设置页保存的偏好，本次提交的每一篇都用同一套参数</span>
      </div>

      <div class="card card--pad">
        <div class="form-grid">
          <div class="field">
            <span class="field__label">时长</span>
            <div class="chips">
              <button
                v-for="option in durations"
                :key="option.value"
                type="button"
                class="chip"
                :class="{ 'is-active': duration === option.value }"
                @click="duration = option.value"
              >
                {{ option.label }}
              </button>
            </div>
          </div>

          <div class="field">
            <span class="field__label">讲解难度</span>
            <div class="chips">
              <button
                v-for="option in levels"
                :key="option.value"
                type="button"
                class="chip"
                :class="{ 'is-active': level === option.value }"
                @click="level = option.value"
              >
                {{ option.label }}
              </button>
            </div>
          </div>
        </div>

        <hr class="divider" />

        <!-- 语言版本：多选、至少一个（契约 §1「新建时用 options.languages 指定要哪些版本」） -->
        <div class="field">
          <span class="field__label">语言版本（至少选一个）</span>
          <div class="chips" role="group" aria-label="要生成的语言版本">
            <button
              v-for="option in LANGUAGE_OPTIONS"
              :key="option.value"
              type="button"
              class="chip"
              :class="{ 'is-active': languages.includes(option.value) }"
              :aria-pressed="languages.includes(option.value)"
              :title="
                languages.length === 1 && languages.includes(option.value)
                  ? '至少保留一个语言版本'
                  : option.label
              "
              @click="toggleLanguage(option.value)"
            >
              {{ option.label }}
            </button>
          </div>
          <span class="field__hint">{{ languagesHint }}</span>
        </div>

        <hr class="divider" />

        <div class="form-grid">
          <div class="field">
            <label class="field__label" for="voice-a">主播A（主讲）</label>
            <select id="voice-a" v-model="voiceA" class="select">
              <optgroup label="男声">
                <option v-for="voice in meta.maleVoices" :key="voice.id" :value="voice.id">
                  {{ voice.label }}
                </option>
              </optgroup>
              <optgroup label="女声">
                <option v-for="voice in meta.femaleVoices" :key="voice.id" :value="voice.id">
                  {{ voice.label }}
                </option>
              </optgroup>
            </select>
          </div>

          <div class="field">
            <label class="field__label" for="voice-b">主播B（提问）</label>
            <select id="voice-b" v-model="voiceB" class="select">
              <optgroup label="女声">
                <option v-for="voice in meta.femaleVoices" :key="voice.id" :value="voice.id">
                  {{ voice.label }}
                </option>
              </optgroup>
              <optgroup label="男声">
                <option v-for="voice in meta.maleVoices" :key="voice.id" :value="voice.id">
                  {{ voice.label }}
                </option>
              </optgroup>
            </select>
            <span v-if="recommendedPartner" class="field__hint">
              推荐搭配：{{ recommendedPartner.label.split('（')[0] }}
            </span>
          </div>
        </div>
      </div>
    </section>

    <div v-if="error" class="alert alert--error" style="margin-top: 22px">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">{{ error }}</span>
    </div>

    <div v-if="tooMany" class="alert alert--error" style="margin-top: 22px">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">
        一次最多 {{ MAX_BATCH }} 篇，当前有 {{ itemCount }} 篇。请分批提交。
      </span>
    </div>

    <div v-if="overQuota" class="alert alert--warning" style="margin-top: 14px">
      <span class="alert__icon" aria-hidden="true">!</span>
      <span class="alert__body">
        当前有 {{ itemCount }} 篇，但配额只剩 {{ usage?.remaining }} 篇 ——
        超出的部分会被后端拒绝（返回 429），建议减少篇数或等配额重置。
      </span>
    </div>

    <!-- 批量结果：成功与失败**分开显示**（契约 §2.8） -->
    <section v-if="batchResult" class="section" aria-live="polite">
      <div class="section__head">
        <h2 class="section__title">批量提交结果</h2>
        <span class="section__hint">共 {{ batchResult.total }} 篇</span>
        <span class="spacer" />
        <button type="button" class="btn btn--sm btn--ghost" @click="resetResult">关闭</button>
      </div>

      <div class="batch-grid">
        <div class="card card--pad">
          <div class="row row--between" style="margin-bottom: 12px">
            <span class="badge badge--completed">成功入队 {{ batchResult.created.length }} 篇</span>
          </div>
          <div v-if="!batchResult.created.length" class="section__hint">没有成功入队的条目。</div>
          <ul v-else class="batch-list">
            <li v-for="episode in batchResult.created" :key="episode.id" class="batch-list__item">
              <span class="batch-list__title">{{ episode.title }}</span>
              <span class="badge badge--running">{{ episode.stage_label }}</span>
              <span class="spacer" />
              <RouterLink
                :to="{ name: 'task', params: { id: episode.id } }"
                class="btn btn--sm btn--ghost"
              >
                查看进度
              </RouterLink>
            </li>
          </ul>
        </div>

        <div class="card card--pad">
          <div class="row row--between" style="margin-bottom: 12px">
            <span class="badge badge--failed">失败 {{ batchResult.failed.length }} 篇</span>
          </div>
          <div v-if="!batchResult.failed.length" class="section__hint">
            这一批全部成功入队。
          </div>
          <ul v-else class="batch-list">
            <li v-for="(item, index) in batchResult.failed" :key="index" class="batch-list__item">
              <span class="batch-list__title" :title="item.ref">{{ item.ref || '（空）' }}</span>
              <span class="batch-list__reason">{{ item.reason }}</span>
            </li>
          </ul>
          <p class="section__hint" style="margin: 12px 0 0">
            失败的条目只有它自己没入队，上面的条目照常处理。
          </p>
        </div>
      </div>
    </section>

    <div class="row row--between" style="margin-top: 26px">
      <p class="section__hint" style="margin: 0">
        <template v-if="usageText">{{ usageText }} · </template>
        当前参数：{{ duration }} 分钟 · {{ LEVEL_LABELS[level] }} · {{ languagesText(languages) }} ·
        {{ meta.options.voices.find((voice) => voice.id === voiceA)?.label ?? voiceA }} +
        {{ meta.options.voices.find((voice) => voice.id === voiceB)?.label ?? voiceB }}
        <template v-if="itemCount > 1"> · 本次 {{ itemCount }} 篇（批量）</template>
      </p>
      <button type="button" class="btn btn--primary btn--lg" :disabled="!canSubmit" @click="submit">
        <span v-if="submitting" class="spinner" aria-hidden="true" />
        {{ submitting ? '正在创建任务…' : submitLabel }}
      </button>
    </div>
  </div>
</template>
