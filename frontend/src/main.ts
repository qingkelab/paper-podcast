import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { useSessionStore } from './stores/session'
import './styles/main.css'

const app = createApp(App)
const pinia = createPinia()

// 顺序很重要：
// 1) pinia 先装 —— 路由守卫里要读会话 store；
// 2) 会话守则在 app.use(router) **之前**装 —— Vue Router 一 install 就发起首次导航，
//    装晚了第一次进受保护页面不会被拦（刷新 /library 会先渲染再跳走）；
// 3) 探测（GET /api/health → GET /api/auth/me）与首屏渲染并行跑，不阻塞。
app.use(pinia)

const session = useSessionStore(pinia)
session.install(router)
void session.init()

app.use(router)
app.mount('#app')
