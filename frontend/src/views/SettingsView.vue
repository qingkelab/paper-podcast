<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { IS_MOCK } from '../api'
import { useMetaStore } from '../stores/meta'
import { DEFAULT_PREFERENCES, usePreferencesStore } from '../stores/preferences'
import { LEVEL_LABELS } from '../utils/stages'

const meta = useMetaStore()
const prefs = usePreferencesStore()

/** 表单是偏好的本地副本：点「保存偏好」才写入 localStorage */
const form = ref({ ...prefs.preferences })
const savedAt = ref<number | null>(null)
const refreshing = ref(false)
const mockNotice = ref<string | null>(null)
const mockBusy = ref(false)

const adapterFile = computed(() => (IS_MOCK ? 'src/api/mock.ts（纯浏览器离线）' : 'src/api/real.ts（fetch /api）'))
const savedText = computed(() =>
  savedAt.value ? new Date(savedAt.value).toLocaleTimeString() : '',
)
const previewText = computed(() => {
  const voiceA = meta.options.voices.find((voice) => voice.id === form.value.voice_a)
  const voiceB = meta.options.voices.find((voice) => voice.id === form.value.voice_b)
  return `${form.value.duration_min} 分钟 · ${LEVEL_LABELS[form.value.level]} · ${
    voiceA?.label ?? form.value.voice_a
  } + ${voiceB?.label ?? form.value.voice_b}`
})

function save(): void {
  prefs.update({ ...form.value })
  prefs.markSaved()
  savedAt.value = Date.now()
}

function applyDefaults(): void {
  form.value = { ...DEFAULT_PREFERENCES }
  save()
}

async function refreshHealth(): Promise<void> {
  refreshing.value = true
  await meta.load(true)
  refreshing.value = false
}

async function resetMockData(): Promise<void> {
  if (!IS_MOCK) return
  mockBusy.value = true
  try {
    const module = await import('../api/mock')
    module.resetMockStore()
    mockNotice.value = '已重置 Mock 数据：3 篇示例论文已恢复，音频会在后台重新合成。'
  } finally {
    mockBusy.value = false
  }
}

function modeBadge(mode: string | undefined): string {
  if (mode === 'mock') return 'badge badge--accent'
  return 'badge badge--completed'
}

onMounted(() => {
  void meta.load()
})
</script>

<template>
  <div class="container page">
    <header class="page__head">
      <p class="eyebrow">Settings · 设置</p>
      <h1 class="page-title">默认偏好与运行状态</h1>
      <p class="page-subtitle">
        这里保存的时长 / 难度 / 音色会作为首页导入表单的默认值，保存在浏览器 localStorage 里，不会上传到后端。
      </p>
    </header>

    <section class="section" style="margin-top: 0">
      <div class="section__head">
        <h2 class="section__title">默认播客参数</h2>
        <span class="section__hint">契约 §5 默认值：5 分钟 · 入门 · 大义先生 + 米仔同学</span>
      </div>

      <div class="card card--pad">
        <div class="form-grid">
          <div class="field">
            <span class="field__label">时长</span>
            <div class="chips">
              <button
                v-for="option in meta.options.durations"
                :key="option.value"
                type="button"
                class="chip"
                :class="{ 'is-active': form.duration_min === option.value }"
                @click="form.duration_min = option.value"
              >
                {{ option.label }}
              </button>
            </div>
          </div>

          <div class="field">
            <span class="field__label">讲解难度</span>
            <div class="chips">
              <button
                v-for="option in meta.options.levels"
                :key="option.value"
                type="button"
                class="chip"
                :class="{ 'is-active': form.level === option.value }"
                @click="form.level = option.value"
              >
                {{ option.label }}
              </button>
            </div>
          </div>
        </div>

        <hr class="divider" />

        <div class="form-grid">
          <div class="field">
            <label class="field__label" for="settings-voice-a">主播A（主讲）</label>
            <select id="settings-voice-a" v-model="form.voice_a" class="select">
              <optgroup
                v-for="group in [
                  { label: '男声', items: meta.maleVoices },
                  { label: '女声', items: meta.femaleVoices },
                ]"
                :key="group.label"
                :label="group.label"
              >
                <option v-for="voice in group.items" :key="voice.id" :value="voice.id">
                  {{ voice.label }}
                </option>
              </optgroup>
            </select>
          </div>

          <div class="field">
            <label class="field__label" for="settings-voice-b">主播B（提问）</label>
            <select id="settings-voice-b" v-model="form.voice_b" class="select">
              <optgroup
                v-for="group in [
                  { label: '女声', items: meta.femaleVoices },
                  { label: '男声', items: meta.maleVoices },
                ]"
                :key="group.label"
                :label="group.label"
              >
                <option v-for="voice in group.items" :key="voice.id" :value="voice.id">
                  {{ voice.label }}
                </option>
              </optgroup>
            </select>
          </div>
        </div>

        <hr class="divider" />

        <div class="row row--between">
          <p class="section__hint" style="margin: 0">当前默认：{{ previewText }}</p>
          <div class="row">
            <button type="button" class="btn btn--ghost" @click="applyDefaults">
              恢复契约默认值
            </button>
            <button type="button" class="btn btn--primary" @click="save">保存偏好</button>
          </div>
        </div>

        <p v-if="savedText" class="section__hint" style="color: var(--success); margin: 12px 0 0">
          ✓ 已保存到本地（{{ savedText }}），首页会使用这些默认值。
        </p>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">后端运行状态</h2>
        <span class="section__hint">GET /api/health</span>
        <span class="spacer" />
        <button type="button" class="btn btn--sm btn--ghost" :disabled="refreshing" @click="refreshHealth">
          <span v-if="refreshing" class="spinner" aria-hidden="true" />
          {{ refreshing ? '检测中…' : '重新检测' }}
        </button>
      </div>

      <div class="card card--pad">
        <div class="meta-grid">
          <div class="meta-item">
            <div class="meta-item__label">前端适配器</div>
            <div class="meta-item__value">{{ adapterFile }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-item__label">接口前缀</div>
            <div class="meta-item__value">
              {{ IS_MOCK ? '—（不发起网络请求）' : '/api → http://127.0.0.1:8000' }}
            </div>
          </div>
          <div class="meta-item">
            <div class="meta-item__label">后端版本</div>
            <div class="meta-item__value">{{ meta.version ?? '未连接' }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-item__label">服务状态</div>
            <div class="meta-item__value">
              {{ meta.health?.status ?? (meta.loading ? '检测中…' : '未知') }}
            </div>
          </div>
        </div>

        <hr class="divider" />

        <div class="row" style="gap: 14px">
          <span class="meta-item__label">解读模型（llm）</span>
          <span :class="modeBadge(meta.modes?.llm)">{{ meta.modes?.llm ?? '未知' }}</span>
          <span class="meta-item__label" style="margin-left: 12px">语音合成（tts）</span>
          <span :class="modeBadge(meta.modes?.tts)">{{ meta.modes?.tts ?? '未知' }}</span>
        </div>

        <p class="section__hint" style="margin: 14px 0 0">
          `mock` 表示后端在本地模拟大模型 / 语音合成，`doubao` 表示接了真实的豆包服务。前端顶栏的「Mock
          模式」角标会据此显示。
        </p>

        <div v-if="meta.error" class="alert alert--error" style="margin-top: 16px">
          <span class="alert__icon" aria-hidden="true">!</span>
          <span class="alert__body">
            {{ meta.error }}
            <template v-if="!IS_MOCK">
              真实后端模式下请先启动 FastAPI 服务（默认 http://127.0.0.1:8000）。
            </template>
          </span>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section__head">
        <h2 class="section__title">数据与演示</h2>
      </div>

      <div class="card card--pad">
        <div class="row row--between">
          <div style="max-width: 62ch">
            <p style="margin: 0 0 6px">Mock 演示数据</p>
            <p class="section__hint" style="margin: 0">
              重置后会清空本机 Mock 数据（localStorage），恢复内置的 3 篇示例论文：Attention Is All You
              Need、ResNet、LoRA。真实后端模式下该操作不可用。
            </p>
          </div>
          <button
            type="button"
            class="btn"
            :disabled="!IS_MOCK || mockBusy"
            @click="resetMockData"
          >
            <span v-if="mockBusy" class="spinner" aria-hidden="true" />
            {{ IS_MOCK ? '重置 Mock 数据' : '仅 Mock 模式可用' }}
          </button>
        </div>

        <p v-if="mockNotice" class="section__hint" style="color: var(--success); margin: 14px 0 0">
          ✓ {{ mockNotice }}
        </p>

        <hr class="divider" />

        <div class="row row--between">
          <p class="section__hint" style="margin: 0">
            偏好设置存在 localStorage 的 <code>paper-podcast:preferences:v1</code>；
            Mock 数据存在 <code>paper-podcast:mock:episodes:v1</code>。
          </p>
          <RouterLink to="/library" class="btn btn--ghost">去看播客库</RouterLink>
        </div>
      </div>
    </section>
  </div>
</template>
