/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" = 纯浏览器离线 Mock 模式；其它值 = 真实后端模式 */
  readonly VITE_USE_MOCK?: string
  /** 青稞解读服务地址；本地合并开发默认由 scripts/dev-all.sh 启动在 4781 端口 */
  readonly VITE_QINGKE_JIEDU_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
