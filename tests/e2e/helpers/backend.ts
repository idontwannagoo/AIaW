// Thin REST helper that bypasses the UI and seeds data straight into the
// backend on 9011. Used for "B is offline; meanwhile A writes via backend"
// patterns where the UI would slow setup down.
import { BACKEND_URL } from './env'

export interface BackendClient {
  get<T = unknown>(path: string): Promise<T>
  put<T = unknown>(path: string, body: unknown): Promise<T>
  post<T = unknown>(path: string, body: unknown): Promise<T>
  del<T = unknown>(path: string): Promise<T>
}

async function handle<T>(r: Response, method: string, path: string): Promise<T> {
  if (!r.ok) {
    throw new Error(
      `Expected 2xx from ${method} ${path}, got ${r.status}: ${await r.text()}`
    )
  }
  if (r.status === 204) return undefined as T
  return r.json() as Promise<T>
}

export function backendClient(token: string): BackendClient {
  const headers = {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json'
  }
  return {
    get: <T>(p: string) =>
      fetch(`${BACKEND_URL}${p}`, { headers }).then(r => handle<T>(r, 'GET', p)),
    put: <T>(p: string, body: unknown) =>
      fetch(`${BACKEND_URL}${p}`, {
        method: 'PUT',
        headers,
        body: JSON.stringify(body)
      }).then(r => handle<T>(r, 'PUT', p)),
    post: <T>(p: string, body: unknown) =>
      fetch(`${BACKEND_URL}${p}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(body)
      }).then(r => handle<T>(r, 'POST', p)),
    del: <T>(p: string) =>
      fetch(`${BACKEND_URL}${p}`, { method: 'DELETE', headers }).then(r =>
        handle<T>(r, 'DELETE', p)
      )
  }
}

// Convenience wrappers — purely the most-used shapes; specs may inline
// backendClient() when they need something exotic.
export interface ProviderRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

export async function putProvider(
  token: string,
  id: string,
  data: Record<string, unknown>
): Promise<ProviderRow> {
  return backendClient(token).put(`/api/v1/providers/${id}`, data)
}

export async function listProviders(
  token: string,
  since = 0
): Promise<ProviderRow[]> {
  return backendClient(token).get(`/api/v1/providers?since=${since}`)
}

export async function deleteProvider(
  token: string,
  id: string
): Promise<ProviderRow> {
  return backendClient(token).del(`/api/v1/providers/${id}`)
}

// Stage 3 / 批次-3a — KV-shaped reactives endpoint. Distinct envelope from
// providers (`key` instead of `id`, `data` carries only the value blob).
export interface ReactiveRow {
  key: string
  version: number
  updated_at: string
  deleted: boolean
  data: unknown
}

export async function putReactive(
  token: string,
  key: string,
  value: unknown
): Promise<ReactiveRow> {
  return backendClient(token).put(
    `/api/v1/reactives/${encodeURIComponent(key)}`,
    value
  )
}

export async function getReactive(
  token: string,
  key: string
): Promise<ReactiveRow> {
  return backendClient(token).get(
    `/api/v1/reactives/${encodeURIComponent(key)}`
  )
}

export async function listReactives(
  token: string,
  since = 0
): Promise<ReactiveRow[]> {
  return backendClient(token).get(`/api/v1/reactives?since=${since}`)
}

export async function deleteReactive(
  token: string,
  key: string
): Promise<ReactiveRow> {
  return backendClient(token).del(
    `/api/v1/reactives/${encodeURIComponent(key)}`
  )
}
