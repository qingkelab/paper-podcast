import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { Router } from 'vue-router'
import {
  changePassword as apiChangePassword,
  getHealth,
  getMe,
  isApiError,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
  setUnauthorizedHandler,
} from '../api'
import type {
  HealthMode,
  HealthPayload,
  LoginInput,
  PasswordChangeInput,
  RegisterInput,
  User,
} from '../api'

/**
 * 会话与运行模式（契约 §1「开放模式」+ §2.5「账号与会话」+ §3「登录态与路由」）。
 *
 * 两件事放在同一个 store 里是**刻意**的：
 *   - `mode` 来自 `GET /api/health`，它决定「要不要登录」；
 *   - `user` 来自 `GET /api/auth/me`。
 * 启动顺序必须是**先 health 拿 mode，再 me 判登录态**（契约 §3）：
 * 库里一个用户都没有时（`open`）根本不请求 `/auth/me`，
 * 否则开放模式下的正常访问会被一个 401 搅成「登录页」。
 */
export const useSessionStore = defineStore('session', () => {
  /**
   * 运行模式。`unknown` = 还没拿到 / 后端不可用 ——
   * 这时**一律不拦人**：拿不准要不要登录的时候，宁可让页面自己去报错，
   * 也不要因为探测失败就把用户锁在登录页外面。
   */
  const mode = ref<HealthMode | 'unknown'>('unknown')
  const health = ref<HealthPayload | null>(null)
  const user = ref<User | null>(null)
  /** 首次探测（health + me）是否已经跑完 */
  const ready = ref(false)
  const probing = ref(false)
  /** 后端探测失败的原因（不阻断页面，只用于显示） */
  const error = ref<string | null>(null)
  /**
   * 「这个功能需要账号」的提示（开放模式或未登录时点了专辑 / 分享这类操作）。
   *
   * 为什么要有它：开放模式只是「不强制登录」，不是「全部功能都能用」——
   * 专辑和分享需要一个归属者。这种时候**不能把人踹去 /login**（开放模式本来就不该自动跳），
   * 也**不能把后端的 `{"detail": "需要登录"}` 原样弹给用户**（那是一句给开发看的英文式报错，
   * 用户不知道该做什么）。正确做法是就地给一句中文说明 + 一个去 /login 的入口。
   */
  const accountPrompt = ref<string | null>(null)

  let pending: Promise<void> | null = null
  let routerRef: Router | null = null

  const isOpenMode = computed(() => mode.value === 'open')
  const isAuthMode = computed(() => mode.value === 'auth')
  const isLoggedIn = computed(() => user.value !== null)
  /** auth 模式且没登录 —— 受保护页面此时必须被挡到 /login */
  const needsLogin = computed(() => isAuthMode.value && !user.value)
  /** 展示名：为空时回退到用户名（契约 §2.5） */
  const displayName = computed(() => user.value?.display_name?.trim() || user.value?.username || '')

  /** 契约 §3：先 health 取 mode，再 auth/me 判登录态 */
  async function init(force = false): Promise<void> {
    if (probing.value && pending) return pending
    if (ready.value && !force) return
    probing.value = true
    pending = (async () => {
      try {
        // 1) 先拿 mode（这个接口免登录，且是唯一能告诉前端「要不要登录」的接口）
        const payload = await getHealth()
        health.value = payload
        mode.value = payload.mode ?? 'open'
        error.value = null
      } catch (cause) {
        // 后端不可用：模式未知，不拦人（页面自己会显示连接失败）
        health.value = null
        mode.value = 'unknown'
        error.value = isApiError(cause) ? cause.message : '无法读取后端状态'
        ready.value = true
        return
      }

      // 2) 开放模式不请求 /auth/me（契约 §3：这时后端还没建账号，也没有登录这回事）
      if (mode.value === 'open') {
        user.value = null
        ready.value = true
        return
      }

      try {
        user.value = await getMe()
      } catch (cause) {
        // 401 = 没登录，是正常结果，不是错误
        user.value = null
        if (isApiError(cause) && cause.status !== 401) error.value = cause.message
      }
      ready.value = true
    })().finally(() => {
      probing.value = false
    })
    return pending
  }

  /** 让路由守卫/组件拿到「探测已经跑完」的那个承诺 */
  function ensureInit(): Promise<void> {
    if (ready.value) return Promise.resolve()
    return init()
  }

  /**
   * 装上路由守卫与全局 401 处理。必须在 `app.use(router)` **之前**调用：
   * Vue Router 在 install 时就会发起首次导航，装晚了第一次进入受保护页面不会被拦。
   */
  function install(router: Router): void {
    routerRef = router

    router.beforeEach(async (to) => {
      // 探测本身要花一次往返：先等它跑完再判，否则刷新受保护页面时会闪一下 404/首屏
      await ensureInit()
      // 免登录页面：首页（产品介绍）、登录页、公开分享页、404
      if (to.meta.public) return true
      if (needsLogin.value) {
        return { name: 'login', query: { redirect: to.fullPath } }
      }
      return true
    })

    // 任意受保护接口返回 401（cookie 过期 / 服务端删了会话 / 改了口令）都落到登录页，
    // 而不是在某次点击之后只给一句「需要登录」的报错 —— 那样用户不知道下一步做什么。
    setUnauthorizedHandler(() => {
      if (isAuthMode.value) user.value = null
      // 开放模式：**不自动跳 /login**（库里还没有账号，跳过去只会让人以为被挡在门外）。
      // 需要账号的操作由调用方调 requireAccount()，就地给中文提示 + 登录入口。
      if (isOpenMode.value) return
      const current = routerRef?.currentRoute.value
      if (!current || current.meta.public || current.name === 'login') return
      void routerRef?.push({
        name: 'login',
        query: { redirect: current.fullPath, reason: 'expired' },
      })
    })
  }

  /** 这个错误是不是「需要账号」（401） */
  function isAccountRequired(cause: unknown): boolean {
    return isApiError(cause) && cause.status === 401
  }

  /**
   * 标记「这一项需要账号」。由专辑 / 分享这些需要归属者的操作调用，
   * 页面顶部（AccountPrompt 组件）会据此给中文说明 + 去 /login 的入口。
   */
  function requireAccount(action: string): void {
    accountPrompt.value = `${action}需要一个归属者，创建账号后即可使用（浏览、生成播客这些功能不受影响）。`
  }

  function clearAccountPrompt(): void {
    accountPrompt.value = null
  }

  async function login(input: LoginInput): Promise<User> {
    const account = await apiLogin(input)
    user.value = account
    clearAccountPrompt()
    // 登录成功说明后端就在 auth 模式（能登录就说明有账号）
    if (mode.value !== 'auth') mode.value = 'auth'
    ready.value = true
    return account
  }

  async function register(input: RegisterInput): Promise<User> {
    const account = await apiRegister(input)
    // 注册即登录（契约 §2.5）
    user.value = account
    clearAccountPrompt()
    // 注册成功就意味着库里已经有账号了：模式从 open 变为 auth
    mode.value = 'auth'
    ready.value = true
    return account
  }

  /** 登出：清本地状态。**跳转由调用方决定**（契约：登出后回到 /login） */
  async function logout(): Promise<void> {
    try {
      await apiLogout()
    } finally {
      user.value = null
      clearAccountPrompt()
    }
  }

  async function changePassword(input: PasswordChangeInput): Promise<void> {
    await apiChangePassword(input)
    // 服务端会删掉其他会话、只留当前这一个，所以这里不需要重新登录
    await init(true)
  }

  return {
    mode,
    health,
    user,
    ready,
    probing,
    error,
    accountPrompt,
    isOpenMode,
    isAuthMode,
    isLoggedIn,
    needsLogin,
    displayName,
    init,
    ensureInit,
    install,
    isAccountRequired,
    requireAccount,
    clearAccountPrompt,
    login,
    register,
    logout,
    changePassword,
  }
})
