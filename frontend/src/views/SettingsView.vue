<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import {
  IS_MOCK,
  changePassword as apiChangePassword,
  errorMessage,
  getUsage,
} from '../api'
import type { EpisodeLanguage, UsagePayload } from '../api'
import { useMetaStore } from '../stores/meta'
import { useSessionStore } from '../stores/session'
import { DEFAULT_PREFERENCES, usePreferencesStore } from '../stores/preferences'
import { LANGUAGE_OPTIONS, languagesText } from '../utils/language'
import { LEVEL_LABELS } from '../utils/stages'

const meta = useMetaStore()
const prefs = usePreferencesStore()
const session = useSessionStore()

// --- 生成配额（契约 §2.9：GET /api/usage） -----------------------------------
// 有配额却不告诉用户还剩多少，等于让人撞 429 才知道。生成页也会显示这一行，
// 但设置页是「运行状态」的地方，放一份方便随时查（尤其是配额被用完之后）。
const usage = ref<UsagePayload | null>(null)
const usageError = ref<string | null>(null)

const usageText = computed(() => {
  const value = usage.value
  if (!value) return null
  if (value.limit <= 0) return '服务端没有设置配额（不限量）'
  const reset = value.resets_at ? `，${value.resets_at} 之后释放` : ''
  return `最近 24 小时已生成 ${value.used} 期，上限 ${value.limit} 期，还能生成 ${value.remaining ?? 0} 期${reset}`
})

async function loadUsage(): Promise<void> {
  usageError.value = null
  try {
    usage.value = await getUsage()
  } catch (cause) {
    // 老后端没有这个接口 / 未登录：不当成错误刷红，只是不显示这一行
    usage.value = null
    usageError.value = errorMessage(cause, '')
  }
}

// --- 修改口令（契约 §2.5：必须带旧口令） -------------------------------------
const passwordForm = ref({ current_password: '', new_password: '', confirm_password: '' })
const passwordBusy = ref(false)
const passwordError = ref<string | null>(null)
const passwordNotice = ref<string | null>(null)

const canChangePassword = computed(() => {
  if (passwordBusy.value) return false
  if (!session.isLoggedIn) return false
  if (passwordForm.value.current_password.length < 1) return false
  if (passwordForm.value.new_password.length < 8) return false
  return passwordForm.value.new_password === passwordForm.value.confirm_password
})

async function submitPassword(): Promise<void> {
  if (!canChangePassword.value) return
  passwordBusy.value = true
  passwordError.value = null
  passwordNotice.value = null
  try {
    await apiChangePassword({
      current_password: passwordForm.value.current_password,
      new_password: passwordForm.value.new_password,
    })
    passwordForm.value = { current_password: '', new_password: '', confirm_password: '' }
    passwordNotice.value = '口令已更新。其他设备上的会话已经失效，这台设备仍然保持着登录。'
  } catch (cause) {
    passwordError.value = errorMessage(cause, '修改口令失败')
  } finally {
    passwordBusy.value = false
  }
}

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
  return `${form.value.duration_min} 分钟 · ${LEVEL_LABELS[form.value.level]} · ${languagesText(
    form.value.languages,
  )} · ${voiceA?.label ?? form.value.voice_a} + ${voiceB?.label ?? form.value.voice_b}`
})

/** 语言版本：多选、至少一个（与首页表单同一套规则，契约 §1） */
function toggleLanguage(language: EpisodeLanguage): void {
  const current = form.value.languages
  if (current.includes(language)) {
    if (current.length === 1) return // 至少留一个
    form.value.languages = current.filter((item) => item !== language)
    return
  }
  form.value.languages = LANGUAGE_OPTIONS.map((option) => option.value).filter(
    (value) => value === language || current.includes(value),
  )
}

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

async function logoutHere(): Promise<void> {
  await session.logout()
  passwordNotice.value = null
  passwordError.value = null
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
  void session.ensureInit().then(() => {
    if (session.isLoggedIn) void loadUsage()
  })
})
</script>

<template>
  <div class="container page">
    <header class="page__head">
      <p class="eyebrow">Settings · 设置</p>
      <h1 class="page-title">默认偏好与运行状态</h1>
      <p class="page-subtitle">
        这里保存的时长 / 难度 / 语言版本 / 音色会作为首页导入表单的默认值，保存在浏览器
        localStorage 里，不会上传到后端。
      </p>
    </header>

    <section class="section" style="margin-top: 0">
      <div class="section__head">
        <h2 class="section__title">账号</h2>
        <span class="section__hint">POST /api/auth/password</span>
      </div>

      <div class="card card--pad">
        <div v-if="session.isOpenMode" class="section__hint">
          当前是开放模式（后端库里还没有任何账号），因此没有账号可改 ——
          第一个账号建好之后，应用会自动切换成「需要登录」。
        </div>

        <template v-else-if="session.isLoggedIn">
          <div class="meta-grid">
            <div class="meta-item">
              <div class="meta-item__label">展示名</div>
              <div class="meta-item__value">{{ session.displayName }}</div>
            </div>
            <div class="meta-item">
              <div class="meta-item__label">用户名</div>
              <div class="meta-item__value">{{ session.user?.username }}</div>
            </div>
            <div class="meta-item">
              <div class="meta-item__label">用户 ID</div>
              <div class="meta-item__value"><code>{{ session.user?.id }}</code></div>
            </div>
            <div class="meta-item">
              <div class="meta-item__label">注册时间</div>
              <div class="meta-item__value">{{ session.user?.created_at }}</div>
            </div>
          </div>

          <hr class="divider" />

          <h3 class="analysis-card__title" style="margin-bottom: 6px">生成配额</h3>
          <p class="section__hint" style="margin: 0 0 12px">
            窗口是最近 24 小时的滑动窗口（不是自然日）；单篇扣 1，批量按篇数扣。
          </p>
          <div class="meta-grid">
            <div class="meta-item">
              <div class="meta-item__label">最近 24 小时用量</div>
              <div class="meta-item__value">
                {{
                  usage
                    ? `${usage.used} / ${usage.limit > 0 ? usage.limit : '不限'}`
                    : usageError
                      ? '读取失败'
                      : '读取中…'
                }}
              </div>
            </div>
            <div class="meta-item">
              <div class="meta-item__label">还能生成</div>
              <div class="meta-item__value">
                {{ usage && usage.limit > 0 ? `${usage.remaining ?? 0} 期` : '—' }}
              </div>
            </div>
            <div class="meta-item">
              <div class="meta-item__label">配额释放时间</div>
              <div class="meta-item__value">{{ usage?.resets_at ?? '—' }}</div>
            </div>
          </div>
          <p v-if="usageText" class="section__hint" style="margin: 12px 0 0">{{ usageText }}</p>

          <hr class="divider" />

          <h3 class="analysis-card__title" style="margin-bottom: 6px">修改口令</h3>
          <p class="section__hint" style="margin: 0 0 16px">
            必须填当前口令。改完会删除其他设备上的会话，只保留这一台 ——
            否则「明明改了密码，别处还挂着旧会话」就等于没改。
          </p>

          <div class="form-grid">
            <div class="field">
              <label class="field__label" for="pwd-current">当前口令</label>
              <input
                id="pwd-current"
                v-model="passwordForm.current_password"
                class="input"
                type="password"
                autocomplete="current-password"
              />
            </div>
            <div class="field">
              <label class="field__label" for="pwd-new">新口令（至少 8 位）</label>
              <input
                id="pwd-new"
                v-model="passwordForm.new_password"
                class="input"
                type="password"
                autocomplete="new-password"
              />
            </div>
            <div class="field">
              <label class="field__label" for="pwd-confirm">再输一次新口令</label>
              <input
                id="pwd-confirm"
                v-model="passwordForm.confirm_password"
                class="input"
                type="password"
                autocomplete="new-password"
              />
              <span
                v-if="
                  passwordForm.confirm_password &&
                  passwordForm.confirm_password !== passwordForm.new_password
                "
                class="field__hint"
                style="color: var(--danger)"
              >
                两次输入的新口令不一致
              </span>
            </div>
          </div>

          <div v-if="passwordError" class="alert alert--error" style="margin-top: 16px">
            <span class="alert__icon" aria-hidden="true">!</span>
            <span class="alert__body">{{ passwordError }}</span>
          </div>
          <p v-if="passwordNotice" class="section__hint" style="color: var(--success); margin-top: 16px">
            ✓ {{ passwordNotice }}
          </p>

          <div class="row row--between" style="margin-top: 18px">
            <button type="button" class="btn btn--ghost" @click="logoutHere">登出</button>
            <button
              type="button"
              class="btn btn--primary"
              :disabled="!canChangePassword"
              @click="submitPassword"
            >
              <span v-if="passwordBusy" class="spinner" aria-hidden="true" />
              {{ passwordBusy ? '正在修改…' : '修改口令' }}
            </button>
          </div>
        </template>

        <div v-else class="section__hint">
          未登录。
          <RouterLink :to="{ name: 'login' }" class="btn btn--sm btn--primary" style="margin-left: 10px">
            去登录
          </RouterLink>
        </div>
      </div>
    </section>

    <section class="section">
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

        <div class="field">
          <span class="field__label">语言版本（至少选一个）</span>
          <div class="chips" role="group" aria-label="默认要生成的语言版本">
            <button
              v-for="option in LANGUAGE_OPTIONS"
              :key="option.value"
              type="button"
              class="chip"
              :class="{ 'is-active': form.languages.includes(option.value) }"
              :aria-pressed="form.languages.includes(option.value)"
              :title="
                form.languages.length === 1 && form.languages.includes(option.value)
                  ? '至少保留一个语言版本'
                  : option.label
              "
              @click="toggleLanguage(option.value)"
            >
              {{ option.label }}
            </button>
          </div>
          <span class="field__hint">
            中英两版共用同一份配图，脚本、解读、音频、视频各自独立。只选一个语言时，详情页不会出现切换器。
          </span>
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
              {{ IS_MOCK ? '—（不发起任何网络请求）' : 'api 前缀 → http://127.0.0.1:8000' }}
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
            偏好设置（含关键词高亮开关）存在 localStorage 的
            <code>paper-podcast:preferences:v1</code>；Mock 数据存在
            <code>paper-podcast:mock:episodes:v3</code>
            （schema 版本号变化就意味着老数据会重新播种，避免看到旧语义的缓存）。
          </p>
          <RouterLink to="/library" class="btn btn--ghost">去看播客库</RouterLink>
        </div>
      </div>
    </section>
  </div>
</template>
