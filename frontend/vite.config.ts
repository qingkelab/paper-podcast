import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 说明：
// - 开发态把 /api 转发到本地 FastAPI 后端（http://127.0.0.1:8000）。
// - VITE_USE_MOCK=1 时前端完全离线运行，不发起任何 /api 请求（见 src/api/index.ts）。
//
// ⚠️ base 必须按模式区分，不能固定成 './'：
//   - Mock 构建走 hash 路由（/#/library），页面永远在站点根路径下，
//     相对路径没问题，而且能直接丢到 GitHub Pages 的任意子路径。
//   - 真实构建走 history 路由（/library、/episode/xxx）。此时如果还用相对路径，
//     浏览器在 /episode/xxx 上会把 ./assets/x.js 解析成 /episode/assets/x.js，
//     资源 404 → 整站白屏。所有深链接（刷新页面、分享链接）都会挂。
const useMock = process.env.VITE_USE_MOCK === '1'

export default defineConfig({
  base: useMock ? './' : '/',
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
