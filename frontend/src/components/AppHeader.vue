<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useMetaStore } from '../stores/meta'
import { useSessionStore } from '../stores/session'

const meta = useMetaStore()
const session = useSessionStore()
const route = useRoute()
const router = useRouter()

const links = [
  { to: '/', label: '首页' },
  { to: '/create', label: '开始生成' },
  { to: '/library', label: '播客库' },
  { to: '/albums', label: '专辑' },
  { to: '/settings', label: '设置' },
]

const busy = ref(false)

/**
 * 账号区（契约 §1「开放模式」+ 后端约定「开放模式要留创建账号入口」）。
 *
 * - 开放模式（库里还没有任何账号）：**不显示登录/登出**（没有账号可登，登录入口只会误导人），
 *   但保留一个「创建账号」的入口 —— 专辑和分享需要一个归属者，
 *   藏掉它就意味着用户点「新建专辑」拿到 401 之后无处可去。
 * - 需要登录的模式：登录后显示展示名 + 登出，未登录显示登录入口。
 */
const showAccount = computed(() => session.isAuthMode || session.isOpenMode)
const isOpenMode = computed(() => session.isOpenMode)

async function logout(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    await session.logout()
    // 契约 §3：登出后回到 /login（带上当前路径，方便重新登录后接着看）
    await router.push({ name: 'login', query: { redirect: route.fullPath } })
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <header class="app-header">
    <div class="app-header__inner">
      <RouterLink to="/" class="brand">
        <span class="brand__mark" aria-hidden="true">播</span>
        <span class="brand__text">
          <span class="brand__name">论文解读播客</span>
          <span class="brand__tagline">Paper → Podcast</span>
        </span>
      </RouterLink>

      <nav class="app-nav" aria-label="主导航">
        <RouterLink
          v-for="link in links"
          :key="link.to"
          :to="link.to"
          class="nav-link"
          exact-active-class="is-active"
        >
          {{ link.label }}
        </RouterLink>
      </nav>

      <span v-if="meta.showMockBadge" class="badge--mock" :title="meta.mockDetail">
        <span aria-hidden="true">◈</span> Mock 模式
      </span>
      <RouterLink
        v-else-if="meta.error"
        to="/settings"
        class="badge badge--failed"
        title="点击前往设置页查看后端状态"
      >
        后端未连接
      </RouterLink>

      <!-- 账号区：登录后显示展示名 + 登出；未登录时给一个登录入口 -->
      <div v-if="showAccount" class="account">
        <template v-if="isOpenMode && !session.isLoggedIn">
          <RouterLink
            :to="{ name: 'login' }"
            class="btn btn--sm btn--ghost"
            title="开放模式下所有功能都能直接用；创建账号是为了使用个人专辑与分享"
          >
            创建账号
          </RouterLink>
        </template>
        <template v-else-if="session.isLoggedIn">
          <span class="account__name" :title="`已登录：${session.user?.username}`">
            <span class="account__dot" aria-hidden="true" />
            {{ session.displayName }}
          </span>
          <button type="button" class="btn btn--sm btn--ghost" :disabled="busy" @click="logout">
            {{ busy ? '登出中…' : '登出' }}
          </button>
        </template>
        <RouterLink v-else :to="{ name: 'login', query: { redirect: route.fullPath } }" class="btn btn--sm btn--primary">
          登录
        </RouterLink>
      </div>
    </div>
  </header>
</template>
