<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { IS_MOCK, createEpisodeFromFile, createEpisodeFromText, createEpisodeFromUrl, errorMessage } from '../api'
import type { Episode, EpisodeOptionsInput, LevelValue } from '../api'
import { useMetaStore } from '../stores/meta'
import { usePreferencesStore } from '../stores/preferences'
import { formatBytes } from '../utils/format'
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

const active = ref<TabKey>('pdf')
const dragging = ref(false)
const file = ref<File | null>(null)
const url = ref('')
const text = ref('')
const submitting = ref(false)
const error = ref<string | null>(null)
const picker = ref<HTMLInputElement | null>(null)

// 参数区：初值来自 localStorage 里的偏好设置（契约 §5 默认值）
const duration = ref<number>(prefs.preferences.duration_min)
const level = ref<LevelValue>(prefs.preferences.level)
const voiceA = ref<string>(prefs.preferences.voice_a)
const voiceB = ref<string>(prefs.preferences.voice_b)

const durations = computed(() => meta.options.durations)
const levels = computed(() => meta.options.levels)

const recommendedPartner = computed(() => {
  const current = meta.options.voices.find((voice) => voice.id === voiceA.value)
  if (!current) return null
  return meta.options.voices.find((voice) => voice.pair === current.pair && voice.id !== current.id) ?? null
})

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (active.value === 'pdf') return Boolean(file.value)
  if (active.value === 'url') return url.value.trim().length > 0
  return text.value.trim().length >= 20
})

const submitLabel = computed(() => {
  if (active.value === 'pdf') return file.value ? '上传并生成播客' : '请先选择 PDF 文件'
  if (active.value === 'url') return '抓取链接并生成播客'
  return '解读这段文本并生成播客'
})

function options(): EpisodeOptionsInput {
  return {
    duration_min: duration.value,
    level: level.value,
    voice_a: voiceA.value,
    voice_b: voiceB.value,
  }
}

function openPicker(): void {
  picker.value?.click()
}

function acceptFile(candidate: File | null | undefined): void {
  error.value = null
  if (!candidate) return
  if (!/\.pdf$/i.test(candidate.name)) {
    error.value = '仅支持 .pdf 文件'
    return
  }
  file.value = candidate
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement
  acceptFile(input.files?.[0] ?? null)
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  acceptFile(event.dataTransfer?.files?.[0] ?? null)
}

function clearFile(): void {
  file.value = null
  if (picker.value) picker.value.value = ''
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = null
  try {
    let episode: Episode
    if (active.value === 'pdf') {
      if (!file.value) throw new Error('请先选择 PDF 文件')
      episode = await createEpisodeFromFile({ file: file.value, options: options() })
    } else if (active.value === 'url') {
      episode = await createEpisodeFromUrl({ url: url.value.trim(), options: options() })
    } else {
      episode = await createEpisodeFromText({ text: text.value.trim(), options: options() })
    }
    await router.push({ name: 'task', params: { id: episode.id } })
  } catch (cause) {
    error.value = errorMessage(cause, '创建任务失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

async function runSample(sample: { url: string }): Promise<void> {
  active.value = 'url'
  url.value = sample.url
  await submit()
}

onMounted(() => {
  void meta.load()
})
</script>

<template>
  <div class="container page">
    <header class="page__head">
      <p class="eyebrow">Import · 导入论文</p>
      <h1 class="page-title">把一篇论文，变成一档双人播客</h1>
      <p class="page-subtitle">
        上传 PDF、贴一条 arXiv 链接，或者直接粘贴论文正文。系统会依次完成「解析论文 → 深度解读 → 生成脚本 →
        合成音频」，产出可播放的双人对谈播客、逐段脚本与结构化解读。
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
        @click="active = tab.key"
      >
        <span class="tab__icon" aria-hidden="true">{{ tab.icon }}</span>{{ tab.label }}
      </button>
    </div>

    <div class="card card--pad">
      <!-- PDF -->
      <div v-if="active === 'pdf'">
        <div
          class="dropzone"
          :class="{ 'is-over': dragging }"
          role="button"
          tabindex="0"
          aria-label="选择或拖拽 PDF 文件"
          @click="openPicker"
          @keydown.enter.prevent="openPicker"
          @keydown.space.prevent="openPicker"
          @dragover.prevent="dragging = true"
          @dragenter.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop"
        >
          <div class="dropzone__icon" aria-hidden="true">⇪</div>
          <p class="dropzone__title">把 PDF 拖到这里，或点击选择文件</p>
          <p class="dropzone__hint">仅支持 .pdf，单篇建议不超过 40MB</p>
        </div>
        <input
          ref="picker"
          class="audio-hidden"
          type="file"
          accept="application/pdf,.pdf"
          @change="onPick"
        />
        <div v-if="file" class="file-pill">
          <span aria-hidden="true">📄</span>
          <span class="file-pill__name">{{ file.name }}</span>
          <span class="file-pill__size">{{ formatBytes(file.size) }}</span>
          <span class="spacer" />
          <button type="button" class="btn btn--sm btn--ghost" @click="clearFile">移除</button>
        </div>
      </div>

      <!-- URL -->
      <div v-else-if="active === 'url'" class="field">
        <label class="field__label" for="url-input">论文链接</label>
        <input
          id="url-input"
          v-model="url"
          class="input"
          type="url"
          inputmode="url"
          placeholder="https://arxiv.org/abs/1706.03762"
          @keydown.enter="submit"
        />
        <span class="field__hint">
          支持 arXiv / 期刊页面 / PDF 直链；后端会抓取正文再解读。链接里带 fail-demo 字样可以演示失败与重试流程。
        </span>
      </div>

      <!-- 文本 -->
      <div v-else class="field">
        <label class="field__label" for="text-input">论文正文</label>
        <textarea
          id="text-input"
          v-model="text"
          class="textarea"
          placeholder="把摘要与正文粘贴到这里（建议包含方法与实验部分）…"
        />
        <span class="field__hint">{{ text.trim().length }} 字（至少 20 字）</span>
      </div>

      <div v-if="IS_MOCK" class="alert alert--accent" style="margin-top: 20px">
        <span class="alert__icon" aria-hidden="true">◈</span>
        <span class="alert__body">
          <span class="alert__title">离线 Mock 模式</span>
          当前没有连接后端：示例论文可以直接一键跑通，任意 PDF / 链接 / 文本会套用内置示例解读。
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
        <span class="section__hint">这里的初值来自设置页保存的偏好</span>
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

    <div class="row row--between" style="margin-top: 26px">
      <p class="section__hint" style="margin: 0">
        当前参数：{{ duration }} 分钟 · {{ LEVEL_LABELS[level] }} ·
        {{ meta.options.voices.find((voice) => voice.id === voiceA)?.label ?? voiceA }} +
        {{ meta.options.voices.find((voice) => voice.id === voiceB)?.label ?? voiceB }}
      </p>
      <button type="button" class="btn btn--primary btn--lg" :disabled="!canSubmit" @click="submit">
        <span v-if="submitting" class="spinner" aria-hidden="true" />
        {{ submitting ? '正在创建任务…' : submitLabel }}
      </button>
    </div>
  </div>
</template>
