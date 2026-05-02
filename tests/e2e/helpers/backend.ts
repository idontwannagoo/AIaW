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

// Stage 3 / 批次-3b — assistants endpoint (id-PK, mirrors providers).
export interface AssistantRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

export async function putAssistant(
  token: string,
  id: string,
  data: Record<string, unknown>
): Promise<AssistantRow> {
  return backendClient(token).put(`/api/v1/assistants/${id}`, data)
}

export async function deleteAssistant(
  token: string,
  id: string
): Promise<AssistantRow> {
  return backendClient(token).del(`/api/v1/assistants/${id}`)
}

// Stage 3 / 批次-3b — installed_plugins endpoint (KV-shaped, mirrors reactives
// envelope but `data` carries the full plugin row not a value blob).
export interface InstalledPluginRow {
  key: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

export async function putInstalledPlugin(
  token: string,
  key: string,
  data: Record<string, unknown>
): Promise<InstalledPluginRow> {
  return backendClient(token).put(
    `/api/v1/installed-plugins/${encodeURIComponent(key)}`,
    data
  )
}

export async function deleteInstalledPlugin(
  token: string,
  key: string
): Promise<InstalledPluginRow> {
  return backendClient(token).del(
    `/api/v1/installed-plugins/${encodeURIComponent(key)}`
  )
}

// Stage 3 / 批次-3b — avatar_images endpoint (id-PK; client serializes
// ArrayBuffer → base64 before PUT, server is opaque JSON).
export interface AvatarImageRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: { id: string; contentBuffer: string; mimeType: string } | null
}

export async function putAvatarImage(
  token: string,
  id: string,
  data: { id: string; contentBuffer: string; mimeType: string }
): Promise<AvatarImageRow> {
  return backendClient(token).put(`/api/v1/avatar-images/${id}`, data)
}

export async function deleteAvatarImage(
  token: string,
  id: string
): Promise<AvatarImageRow> {
  return backendClient(token).del(`/api/v1/avatar-images/${id}`)
}

// Stage 4 / 批次-4a — workspaces endpoint (id-PK, mirrors providers /
// assistants envelope; the row's `data` carries Workspace | Folder).
export interface WorkspaceRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

export async function putWorkspace(
  token: string,
  id: string,
  data: Record<string, unknown>
): Promise<WorkspaceRow> {
  return backendClient(token).put(`/api/v1/workspaces/${id}`, data)
}

export async function listWorkspaces(
  token: string,
  since = 0
): Promise<WorkspaceRow[]> {
  return backendClient(token).get(`/api/v1/workspaces?since=${since}`)
}

export async function deleteWorkspace(
  token: string,
  id: string,
  cascade = true
): Promise<WorkspaceRow> {
  return backendClient(token).del(
    `/api/v1/workspaces/${id}?cascade=${cascade}`
  )
}

// Stage 4 / 批次-4b — dialogs endpoint (id-PK; data carries the full
// Dialog row, workspace_id is server-side promoted to its own column +
// FK).
export interface DialogRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

export async function putDialog(
  token: string,
  id: string,
  data: Record<string, unknown>
): Promise<DialogRow> {
  return backendClient(token).put(`/api/v1/dialogs/${id}`, data)
}

export async function listDialogs(
  token: string,
  since = 0
): Promise<DialogRow[]> {
  return backendClient(token).get(`/api/v1/dialogs?since=${since}`)
}

export async function deleteDialog(
  token: string,
  id: string
): Promise<DialogRow> {
  return backendClient(token).del(`/api/v1/dialogs/${id}`)
}

// Stage 4 / 批次-4c — items endpoint (id-PK; data carries the StoredItem
// row, dialog_id is server-side promoted to its own column + FK to
// dialogs. `data.contentBuffer`, when present, is an
// `AttachmentEnvelope` from blob-client.ts — see items.server.ts).
export interface ItemRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

export async function putItem(
  token: string,
  id: string,
  data: Record<string, unknown>
): Promise<ItemRow> {
  return backendClient(token).put(`/api/v1/items/${id}`, data)
}

export async function listItems(
  token: string,
  since = 0
): Promise<ItemRow[]> {
  return backendClient(token).get(`/api/v1/items?since=${since}`)
}

export async function deleteItem(
  token: string,
  id: string
): Promise<ItemRow> {
  return backendClient(token).del(`/api/v1/items/${id}`)
}
