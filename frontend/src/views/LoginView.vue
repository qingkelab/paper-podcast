<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { IS_MOCK, errorMessage as apiErrorMessage } from '../api'
import { useSessionStore } from '../stores/session'

/**
 * 登录 / 注册（契约 §2.5）。一页两态：同一个表单，切换 tab 决定提交到 login 还是 register。
 *
 * 三条容易被忽略的规则，都在这里兑现：
 * - **开放模式（`mode === "open"`）不强制登录**：库里一个用户都没有时所有功能直接可用，
 *   这里**只留注册**（`创建第一个账号`）—— 登录无从可登（一个账号都还没有），
 *   但专辑和分享需要一个归属者，所以入口必须在：否则用户点「新建专辑」会拿到 401 却无处可去。
 *   那种情况下不显示注册入口，等于把一条路修到一半就断了。
 * - 注册即登录（后端会直接下发会话 cookie），所以成功后不必再走一次登录。
 * - **redirect 只接受站内路径**：`?redirect=//evil.com` 是经典的开放重定向，必须掐掉。
 */
const route = useRoute()
const router = useRouter()
const session = useSessionStore()

type Mode = 'login' | 'register'

/** 开放模式下没有「登录」这回事（库里还没有账号），默认落在注册那一态 */
const mode = ref<Mode>('login')
const username = ref('')
const password = ref('')
const displayName = ref('')
const signupCode = ref('')
const busy = ref(false)
const error = ref<string | null>(null)

/** 登录过期 / 被挡下来时给出的说明，不让用户莫名其妙地落在这一页 */
const reason = computed(() => {
  if (route.query.reason === 'expired') return '登录状态已失效，请重新登录。'
  if (!route.query.redirect) return null
  // 开放模式下被挡到这里，原因不是「会话过期」而是「这一项需要归属者」，
  // 文案要跟着变 —— 否则用户会去找一个根本不存在的登录状态
  if (session.isOpenMode) {
    return '这一项功能需要一个账号（专辑与分享都归属于创建者）。创建第一个账号后会自动回到刚才的页面。'
  }
  return '请先登录，登录后会回到刚才的页面。'
})

/** 只接受站内路径：`//host` 与 `http://host` 一律忽略，避免开放重定向 */
const redirectTarget = computed(() => {
  const raw = route.query.redirect
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' || !value) return null
  if (!value.startsWith('/') || value.startsWith('//')) return null
  return value
})

const canSubmit = computed(() => {
  if (busy.value) return false
  if (username.value.trim().length < 3 || password.value.length < 8) return false
  return true
})

const submitLabel = computed(() => (mode.value === 'login' ? '登录' : '注册并登录'))

function switchMode(next: Mode): void {
  if (mode.value === next) return
  mode.value = next
  error.value = null
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  busy.value = true
  error.value = null
  try {
    // 开放模式下只有注册这一条路（库里还没有账号，登录无从可登）
    if (mode.value === 'login' && !session.isOpenMode) {
      await session.login({ username: username.value.trim(), password: password.value })
    } else {
      await session.register({
        username: username.value.trim(),
        password: password.value,
        display_name: displayName.value.trim(),
        signup_code: signupCode.value.trim(),
      })
    }
    password.value = ''
    await router.replace(redirectTarget.value ?? '/library')
  } catch (cause) {
    // 后端把所有失败都收敛成中文 detail（「用户名或口令不正确」/「口令至少 8 位」/「邀请码不正确」），
    // 直接显示它，不要自己再编一套文案
    error.value = apiErrorMessage(cause, '操作失败，请稍后重试')
  } finally {
    busy.value = false
  }
}

async function logoutHere(): Promise<void> {
  busy.value = true
  try {
    await session.logout()
    error.value = null
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void session.ensureInit().then(() => {
    if (session.isOpenMode) mode.value = 'register'
  })
})
</script>

<template>
  <div class="container page">
    <header class="page__head">
      <p class="eyebrow">Account · 账号</p>
      <h1 class="page-title">登录论文解读播客</h1>
      <p class="page-subtitle">
        账号用来区分「谁的播客」：登录后播客库只显示你自己的单集，别人拿到分享链接才能看到你指定公开的那一期。
      </p>
    </header>

    <div v-if="session.isOpenMode" class="card card--pad auth-card">
      <div class="alert alert--accent" style="margin-bottom: 20px">
        <span class="alert__icon" aria-hidden="true">◈</span>
        <span class="alert__body">
          <span class="alert__title">当前是开放模式（库里还没有任何账号）</span>
          浏览、生成播客都不需要登录，直接用就行。但「个人专辑」和「一键分享」需要一个归属者，
          所以想用这两项功能，先创建第一个账号 —— 它还会认领库里原有的无主单集
          （V1 时代留下的数据不会变成谁也看不到的孤儿）。
        </span>
      </div>

      <form @submit.prevent="submit">
        <div class="field">
          <label class="field__label" for="auth-username">用户名</label>
          <input
            id="auth-username"
            v-model="username"
            class="input"
            type="text"
            autocomplete="username"
            autocapitalize="off"
            spellcheck="false"
            placeholder="3-32 位字母、数字、下划线或连字符"
          />
        </div>

        <div class="field">
          <label class="field__label" for="auth-password">口令</label>
          <input
            id="auth-password"
            v-model="password"
            class="input"
            type="password"
            autocomplete="new-password"
            placeholder="至少 8 位"
          />
        </div>

        <div class="field">
          <label class="field__label" for="auth-display">展示名（可选）</label>
          <input
            id="auth-display"
            v-model="displayName"
            class="input"
            type="text"
            autocomplete="nickname"
            placeholder="分享页上作者一栏显示的名字；留空则用用户名"
          />
        </div>

        <div class="field">
          <label class="field__label" for="auth-phone-code">邀请码（可选）</label>
          <input
            id="auth-phone-code"
            v-model="signupCode"
            class="input"
            type="text"
            autocomplete="off"
            placeholder="后端设了 SIGNUP_CODE 时才需要填"
          />
        </div>

        <div v-if="error" class="alert alert--error" style="margin-top: 18px">
          <span class="alert__icon" aria-hidden="true">!</span>
          <span class="alert__body">{{ error }}</span>
        </div>

        <div class="row" style="margin-top: 22px; justify-content: space-between">
          <RouterLink to="/" class="btn btn--ghost">先不用账号，回首页</RouterLink>
          <button type="submit" class="btn btn--primary" :disabled="!canSubmit">
            <span v-if="busy" class="spinner" aria-hidden="true" />
            {{ busy ? '正在创建…' : '创建账号' }}
          </button>
        </div>
      </form>

      <p v-if="IS_MOCK" class="field__hint" style="margin-top: 18px">
        离线 Mock 模式下已经自动登录了演示账号（用户名 <code>guo</code>，口令
        <code>demo1234</code>），所以这里通常用不到。
      </p>
    </div>

    <div v-else class="card card--pad auth-card">
      <div v-if="reason" class="alert alert--warning" style="margin-bottom: 20px">
        <span class="alert__icon" aria-hidden="true">!</span>
        <span class="alert__body">{{ reason }}</span>
      </div>

      <!-- 已经登录了再来看这一页：说清楚现状，别让人以为还要再登一次 -->
      <div v-if="session.isLoggedIn" class="alert alert--info" style="margin-bottom: 20px">
        <span class="alert__icon" aria-hidden="true">i</span>
        <span class="alert__body">
          当前已登录为 <strong>{{ session.displayName }}</strong>（{{ session.user?.username }}）。
          你可以在下面切换到另一个账号。
          <button type="button" class="btn btn--sm btn--ghost" style="margin-left: 10px" :disabled="busy" @click="logoutHere">
            登出
          </button>
        </span>
      </div>

      <div class="tabs" role="tablist" aria-label="登录或注册">
        <button
          type="button"
          role="tab"
          class="tab"
          :class="{ 'is-active': mode === 'login' }"
          :aria-selected="mode === 'login'"
          @click="switchMode('login')"
        >
          登录
        </button>
        <button
          type="button"
          role="tab"
          class="tab"
          :class="{ 'is-active': mode === 'register' }"
          :aria-selected="mode === 'register'"
          @click="switchMode('register')"
        >
          注册
        </button>
      </div>

      <form @submit.prevent="submit">
        <div class="field">
          <label class="field__label" for="auth-username">用户名</label>
          <input
            id="auth-username"
            v-model="username"
            class="input"
            type="text"
            autocomplete="username"
            autocapitalize="off"
            spellcheck="false"
            placeholder="3-32 位字母、数字、下划线或连字符"
          />
        </div>

        <div class="field">
          <label class="field__label" for="auth-password">口令</label>
          <input
            id="auth-password"
            v-model="password"
            class="input"
            type="password"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            placeholder="至少 8 位"
          />
        </div>

        <template v-if="mode === 'register'">
          <div class="field">
            <label class="field__label" for="auth-display">展示名（可选）</label>
            <input
              id="auth-display"
              v-model="displayName"
              class="input"
              type="text"
              autocomplete="nickname"
              placeholder="分享页上作者一栏显示的名字；留空则用用户名"
            />
          </div>
          <div class="field">
            <label class="field__label" for="auth-code">邀请码（可选）</label>
            <input
              id="auth-code"
              v-model="signupCode"
              class="input"
              type="text"
              autocomplete="off"
              placeholder="后端设了 SIGNUP_CODE 时才需要填"
            />
            <span class="field__hint">
              服务端没有配置邀请码时留空即可；配置了的话不填/填错会返回「邀请码不正确」。
            </span>
          </div>
        </template>

        <div v-if="error" class="alert alert--error" style="margin-top: 18px">
          <span class="alert__icon" aria-hidden="true">!</span>
          <span class="alert__body">{{ error }}</span>
        </div>

        <div class="row" style="margin-top: 22px; justify-content: flex-end">
          <button type="submit" class="btn btn--primary" :disabled="!canSubmit">
            <span v-if="busy" class="spinner" aria-hidden="true" />
            {{ busy ? '正在处理…' : submitLabel }}
          </button>
        </div>
      </form>

      <p v-if="IS_MOCK" class="field__hint" style="margin-top: 18px">
        离线 Mock 模式下已经自动登录了演示账号（用户名 <code>guo</code>，口令
        <code>demo1234</code>）。登出后受保护页面会被挡回这一页，用这对凭据可以再登回来；
        也可以直接注册一个新账号（Mock 里任何合法用户名都能注册）。
      </p>
      <p v-else class="field__hint" style="margin-top: 18px">
        会话存在 httpOnly cookie（<code>pp_session</code>）里，有效期 30 天，前端拿不到 token。
      </p>
    </div>
  </div>
</template>
