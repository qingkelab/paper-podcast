/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" = 纯浏览器离线 Mock 模式；其它值 = 真实后端模式 */
  readonly VITE_USE_MOCK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
