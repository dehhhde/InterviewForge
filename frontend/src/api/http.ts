import axios, { AxiosError, type AxiosRequestConfig } from 'axios'

export interface ApiFailure extends Error {
  status?: number
  code: string
  details: Record<string, unknown> | null
  /** 没读到响应（断网 / 被拦 / 服务没起），与"服务回了错误码"不是一回事 */
  network: boolean
}

interface ErrorBody {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown> | null
  }
}

const baseURL = import.meta.env.VITE_API_BASE?.trim() || '/api/v1'

export const http = axios.create({
  baseURL,
  timeout: 180_000,
})

function validationProblems(details: Record<string, unknown> | null): string {
  const problems = details?.problems
  if (!Array.isArray(problems)) return ''
  return problems
    .map((problem) => {
      if (!problem || typeof problem !== 'object') return ''
      const item = problem as { loc?: unknown; msg?: unknown }
      const loc = Array.isArray(item.loc) ? item.loc.join('.') : ''
      return [loc, String(item.msg ?? '')].filter(Boolean).join(': ')
    })
    .filter(Boolean)
    .join('；')
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ErrorBody>) => {
    const failure = new Error() as ApiFailure
    failure.code = 'request_failed'
    failure.details = null
    failure.network = false

    if (!error.response) {
      failure.network = true
      failure.code = 'network_error'
      failure.message = error.code === 'ECONNABORTED'
        ? '请求超时：服务可能还在跑，先刷新列表确认结果，别直接重试。'
        : `没拿到 ${baseURL} 的响应：确认后端服务在运行。`
      return Promise.reject(failure)
    }

    const body = error.response.data?.error
    failure.status = error.response.status
    failure.code = body?.code ?? 'request_failed'
    failure.details = body?.details ?? null

    const problems = validationProblems(failure.details)
    failure.message = [
      body?.message ?? `请求失败（HTTP ${error.response.status}）`,
      problems ? `· ${problems}` : '',
    ]
      .filter(Boolean)
      .join(' ')
    return Promise.reject(failure)
  },
)

export function isApiFailure(error: unknown): error is ApiFailure {
  return error instanceof Error && 'network' in error && 'code' in error
}

export function apiBase(): string {
  return baseURL
}

/**
 * 写端点在网络失败时的措辞：请求可能已经执行并落库，直接重试就是第二份数据。
 */
export function writeFailureHint(
  error: unknown,
  action: string,
  retryCost: string,
): string {
  if (isApiFailure(error) && error.network) {
    return `结果未知：浏览器没读到回执，但服务可能已经执行了（${retryCost}）。先刷新列表确认，别直接重试。`
  }
  return action
}

export async function get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await http.get<T>(url, config)
  return response.data
}

export async function post<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const response = await http.post<T>(url, data, config)
  return response.data
}

export async function put<T>(url: string, data?: unknown): Promise<T> {
  const response = await http.put<T>(url, data)
  return response.data
}

export async function del(url: string): Promise<void> {
  await http.delete(url)
}
