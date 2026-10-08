import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { IS_MOCK } from '../api'

/**
 * 路由与契约 §3 一一对应。
 * Mock 模式（GitHub Pages 离线演示）用 hash 历史：静态托管下刷新 /library 不会 404。
 * 真实后端模式用 history 模式，URL 就是 /library 这样的干净路径。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'landing',
    component: () => import('../views/LandingView.vue'),
    meta: { title: '论文解读 AI 播客' },
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
    path: '/episode/:id',
    name: 'episode',
    component: () => import('../views/EpisodeView.vue'),
    meta: { title: '播客详情' },
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
    meta: { title: '页面不存在' },
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
