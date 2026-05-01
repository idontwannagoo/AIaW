import { BackendApiBaseURL } from 'src/utils/config'
import { authSource } from './auth'

export class HttpError extends Error {
  status: number
  detail?: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'HttpError'
    this.status = status
    this.detail = detail
  }
}

type QueryValue = string | number | boolean | null | undefined
export interface RequestOptions {
  query?: Record<string, QueryValue>
  headers?: Record<string, string>
  signal?: AbortSignal
  // Internal: skip the 401 → refresh → retry flow. Set when the request *is*
  // the retry, or when the caller is the auth flow itself (refresh/login).
  skipAuthRetry?: boolean
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  if (!BackendApiBaseURL) {
    throw new Error('http.ts called without BACKEND_DATA_API_URL configured')
  }
  const base = `${BackendApiBaseURL}${path}`
  if (!query) return base
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue
    params.append(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

async function readError(res: Response): Promise<HttpError> {
  let detail: unknown
  let message = `${res.status} ${res.statusText}`
  try {
    const body = await res.json()
    detail = body?.detail ?? body
    if (typeof body?.detail === 'string') message = body.detail
  } catch { /* non-JSON body */ }
  return new HttpError(res.status, message, detail)
}

async function doFetch<T>(
  method: string,
  path: string,
  body: unknown,
  opts: RequestOptions
): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers ?? {}) }
  let payload: string | undefined
  if (body !== undefined) {
    headers['Content-Type'] = headers['Content-Type'] ?? 'application/json'
    payload = JSON.stringify(body)
  }
  const token = authSource.currentToken()
  if (token && !headers.Authorization) headers.Authorization = `Bearer ${token}`

  const res = await fetch(buildUrl(path, opts.query), {
    method,
    headers,
    body: payload,
    signal: opts.signal
  })

  if (res.status === 401 && !opts.skipAuthRetry) {
    const refreshed = await authSource.tryRefresh()
    if (refreshed) {
      return doFetch<T>(method, path, body, { ...opts, skipAuthRetry: true })
    }
    throw await readError(res)
  }

  if (!res.ok) throw await readError(res)
  if (res.status === 204) return undefined as T
  return await res.json() as T
}

export const http = {
  get<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    return doFetch<T>('GET', path, undefined, opts)
  },
  post<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
    return doFetch<T>('POST', path, body, opts)
  },
  put<T>(path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
    return doFetch<T>('PUT', path, body, opts)
  },
  delete<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    return doFetch<T>('DELETE', path, undefined, opts)
  }
}
