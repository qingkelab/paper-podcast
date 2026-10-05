import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 说明：
// - `base: './'` 让构建产物使用相对路径，可以直接丢到 GitHub Pages 的任意子路径下（离线演示）。
// - 开发态把 /api 转发到本地 FastAPI 后端（http://127.0.0.1:8000）。
// - VITE_USE_MOCK=1 时前端完全离线运行，不发起任何 /api 请求（见 src/api/index.ts）。
export default defineConfig({
  base: './',
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
