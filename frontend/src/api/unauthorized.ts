import type { ApiError } from './error'

/**
 * 「受保护接口返回 401」的全局通知通道（契约 §3）。
 *
 * 为什么单独一个模块：**两个适配器都要能在 401 时通知会话层**。
 * real.ts 收到 HTTP 401，mock.ts 是自己抛 401（演示登录/登出流程也要能跳登录页）——
 * 各自维护一份回调的话，Mock 演示站上这条路径就是坏的。
 *
 * 这里只做转交，不认识 vue-router：真正的跳转在 stores/session.ts 里。
 */
type UnauthorizedHandler = (error: ApiError) => void

let handler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(next: UnauthorizedHandler | null): void {
  handler = next
}

/** 由适配器在抛出 401 之前调用。没有注册处理器时是空操作（例如单测里）。 */
export function notifyUnauthorized(error: ApiError): void {
  handler?.(error)
}
