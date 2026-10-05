/** 统一的接口错误类型：mock 与 real 抛出的错误形状一致，页面只需处理 detail 文案。 */
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status = 0) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

/** 把任意异常转成可展示的中文文案 */
export function errorMessage(error: unknown, fallback = '操作失败，请稍后重试'): string {
  if (isApiError(error)) return error.message || fallback
  if (error instanceof Error) return error.message || fallback
  if (typeof error === 'string' && error) return error
  return fallback
}
