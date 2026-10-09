import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { IS_MOCK } from '../api'

/**
 * 路由与契约 §3 一一对应。
 * Mock 模式（GitHub Pages 离线演示）用 hash 历史：静态托管下刷新 /library 不会 404。
 * 真实后端模式用 history 模式，URL 就是 /library 这样的干净路径。
 *
 * `meta.public`：**免登录**页面。路由守卫与全局 401 处理都看它 ——
 * 首页是产品介绍页（登录前就要能看），`/share/:token` 是公开分享链接（契约要求免登录），
 * 两者都绝不能把人踹到登录页。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'landing',
    component: () => import('../views/LandingView.vue'),
    // 产品介绍页：登录前就能看（登录后展示区换成自己的成品）
    meta: { title: '论文解读 AI 播客', public: true },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/LoginView.vue'),
    meta: { title: '登录 / 注册', public: true },
  },
  {
    // 导入表单从首页搬到 /create：首页要专心做产品介绍，不能被表单割成两半
    path: '/create',
    name: 'create',
    component: () => import('../views/CreateView.vue'),
    meta: { title: '生成新播客' },
  },
  {
    path: '/task/:id',
    name: 'task',
    component: () => import('../views/TaskView.vue'),
    meta: { title: '生成进度' },
  },
  {
    path: '/library',
    name: 'library',
    component: () => import('../views/LibraryView.vue'),
    meta: { title: '播客库' },
  },
  {
    path: '/albums',
    name: 'albums',
    component: () => import('../views/AlbumsView.vue'),
    meta: { title: '我的专辑' },
  },
  {
    path: '/albums/:id',
    name: 'album',
    component: () => import('../views/AlbumView.vue'),
    meta: { title: '专辑详情' },
  },
  {
    path: '/episode/:id',
    name: 'episode',
    component: () => import('../views/EpisodeView.vue'),
    meta: { title: '播客详情' },
  },
  {
    // 公开分享页（契约 §2.7）：**免登录**，且只读 /api/share/{token}/…
    path: '/share/:token',
    name: 'share',
    component: () => import('../views/ShareView.vue'),
    meta: { title: '分享的播客', public: true },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('../views/SettingsView.vue'),
    meta: { title: '设置' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('../views/NotFoundView.vue'),
    meta: { title: '页面不存在', public: true },
  },
]

const router = createRouter({
  history: IS_MOCK ? createWebHashHistory() : createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  // 首页就是产品本身，标题不重复拼品牌名
  document.title = to.name === 'landing' ? '论文解读 AI 播客' : title ? `${title} · 论文解读 AI 播客` : '论文解读 AI 播客'
})

export default router
