import Dexie from 'dexie'
import { reactive, readonly } from 'vue'

import {
  SyncApiBaseURL,
  SyncAuthBaseURL,
  SyncEnabled,
  SyncFilesBaseURL,
  SyncWsURL
} from './config'
import { db } from './db'
import {
  ALL_SYNC_ENTITIES,
  EntityChangeListener,
  EntityOut,
  OutboxEntry,
  SyncEntity,
  WsMessage
} from './sync-types'

const TOKEN_KEY = 'aiaw.sync.jwt'
const EMAIL_KEY = 'aiaw.sync.email'
const SINCE_KEY_PREFIX = 'aiaw.sync.since.' // per-entity high-water-mark
const OUTBOX_REACTIVE_KEY = '#sync-outbox'
const SEQ_REACTIVE_KEY = '#sync-outbox-seq'

interface AuthState {
  token: string | null
  email: string | null
  isLoggedIn: boolean
}

class HttpError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
  }
}

const state: AuthState = reactive({
  token: localStorage.getItem(TOKEN_KEY),
  email: localStorage.getItem(EMAIL_KEY),
  isLoggedIn: !!localStorage.getItem(TOKEN_KEY)
})

const listeners = new Set<EntityChangeListener>()
let ws: WebSocket | null = null
let wsReconnectTimer: ReturnType<typeof setTimeout> | null = null
let flushPromise: Promise<void> | null = null
let online = typeof navigator === 'undefined' ? true : navigator.onLine

function setAuth(token: string | null, email: string | null) {
  state.token = token
  state.email = email
  state.isLoggedIn = !!token
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
  if (email) {
    localStorage.setItem(EMAIL_KEY, email)
  } else {
    localStorage.removeItem(EMAIL_KEY)
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (state.token) headers.Authorization = `Bearer ${state.token}`
  const resp = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  if (resp.status === 401) {
    setAuth(null, null)
    throw new HttpError(401, 'Unauthorized')
  }
  if (!resp.ok) {
    const text = await resp.text().catch(() => resp.statusText)
    throw new HttpError(resp.status, text || resp.statusText)
  }
  if (resp.status === 204) return undefined as T
  const ct = resp.headers.get('content-type') || ''
  return (ct.includes('application/json') ? await resp.json() : (await resp.text())) as T
}

/* ---------- outbox ---------- */

async function readOutbox(): Promise<OutboxEntry[]> {
  const rec = await db.reactives.get(OUTBOX_REACTIVE_KEY)
  return (rec?.value as OutboxEntry[]) || []
}

async function writeOutbox(entries: OutboxEntry[]): Promise<void> {
  await db.reactives.put({ key: OUTBOX_REACTIVE_KEY, value: entries })
}

async function nextSeq(): Promise<number> {
  const rec = await db.reactives.get(SEQ_REACTIVE_KEY)
  const next = ((rec?.value as number | undefined) ?? 0) + 1
  await db.reactives.put({ key: SEQ_REACTIVE_KEY, value: next })
  return next
}

async function enqueue(entry: Omit<OutboxEntry, 'seq' | 'tries' | 'createdAt'>): Promise<void> {
  const seq = await nextSeq()
  const entries = await readOutbox()
  // Dedup: last write wins for same entity+id same op.
  const filtered = entries.filter(e => !(e.entity === entry.entity && e.id === entry.id))
  filtered.push({ ...entry, seq, tries: 0, createdAt: Date.now() })
  await writeOutbox(filtered)
  void flush()
}

/* ---------- flush outbox ---------- */

async function flush(): Promise<void> {
  if (!state.isLoggedIn || !SyncEnabled || !online) return
  if (flushPromise) return flushPromise
  flushPromise = (async () => {
    try {
      while (true) {
        const entries = await readOutbox()
        if (entries.length === 0) break
        const entry = entries[0]
        try {
          if (entry.op === 'put') {
            await request('PUT', `${SyncApiBaseURL}/${entry.entity}/${encodeURIComponent(entry.id)}`, {
              id: entry.id,
              data: entry.payload ?? {}
            })
          } else {
            await request('DELETE', `${SyncApiBaseURL}/${entry.entity}/${encodeURIComponent(entry.id)}`)
          }
          const remaining = (await readOutbox()).filter(e => e.seq !== entry.seq)
          await writeOutbox(remaining)
        } catch (err) {
          if (err instanceof HttpError && err.status === 401) {
            break
          }
          const all = await readOutbox()
          const updated = all.map(e => (e.seq === entry.seq ? { ...e, tries: e.tries + 1 } : e))
          await writeOutbox(updated)
          console.warn('[sync] outbox entry failed', entry, err)
          break
        }
      }
    } finally {
      flushPromise = null
    }
  })()
  return flushPromise
}

/* ---------- since markers ---------- */

function sinceFor(entity: SyncEntity): string | null {
  return localStorage.getItem(SINCE_KEY_PREFIX + entity)
}

function setSince(entity: SyncEntity, iso: string): void {
  const prev = sinceFor(entity)
  if (!prev || new Date(iso) > new Date(prev)) {
    localStorage.setItem(SINCE_KEY_PREFIX + entity, iso)
  }
}

function clearSinceAll(): void {
  for (const e of ALL_SYNC_ENTITIES) {
    localStorage.removeItem(SINCE_KEY_PREFIX + e)
  }
}

/* ---------- apply remote row ---------- */

function localPrimaryKey(entity: SyncEntity): 'id' | 'key' {
  return entity === 'installedPluginsV2' || entity === 'reactives' ? 'key' : 'id'
}

async function applyRemote(entity: SyncEntity, rows: EntityOut[]): Promise<void> {
  if (!rows.length) return
  const table = db.table(entity)
  const pk = localPrimaryKey(entity)
  await db.transaction('rw', table, async () => {
    for (const row of rows) {
      if (row.deleted_at) {
        const key = pk === 'key' ? row.id : row.id
        await table.delete(key)
        continue
      }
      const obj = { ...row.data } as Record<string, unknown>
      obj[pk] = row.id
      await table.put(obj)
    }
  })
  for (const row of rows) {
    setSince(entity, row.updated_at)
  }
}

/* ---------- fetch helpers ---------- */

const NON_OUTBOX_RESTRICTED_KEYS = new Set([OUTBOX_REACTIVE_KEY, SEQ_REACTIVE_KEY])

function payloadForPush(entity: SyncEntity, value: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
  if (!value) return undefined
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(value)) {
    if (k === 'contentBuffer') continue // binary stays local; fileKey is synced
    if (k === 'owner' || k === 'realmId' || k === '$ts') continue
    out[k] = v
  }
  return out
}

/* ---------- WebSocket ---------- */

function wsUrl(): string {
  const loc = window.location
  if (/^wss?:/i.test(SyncWsURL)) return `${SyncWsURL}?token=${encodeURIComponent(state.token!)}`
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  const path = SyncWsURL.startsWith('/') ? SyncWsURL : `/${SyncWsURL}`
  return `${proto}//${loc.host}${path}?token=${encodeURIComponent(state.token!)}`
}

function notifyListeners(msg: WsMessage): void {
  for (const fn of listeners) {
    try {
      fn(msg)
    } catch (err) {
      console.warn('[sync] listener error', err)
    }
  }
}

function connectWs(): void {
  if (!state.isLoggedIn || !SyncEnabled) return
  try {
    ws = new WebSocket(wsUrl())
  } catch (err) {
    console.warn('[sync] ws open failed', err)
    scheduleWsReconnect()
    return
  }
  ws.addEventListener('message', async ev => {
    try {
      const msg = JSON.parse(ev.data) as WsMessage
      await handleWsMessage(msg)
    } catch (err) {
      console.warn('[sync] ws parse error', err)
    }
  })
  ws.addEventListener('close', () => {
    ws = null
    scheduleWsReconnect()
  })
  ws.addEventListener('error', () => {
    try {
      ws?.close()
    } catch {
      /* ignore */
    }
  })
}

function scheduleWsReconnect(): void {
  if (!state.isLoggedIn) return
  if (wsReconnectTimer) return
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null
    connectWs()
  }, 3000)
}

async function handleWsMessage(msg: WsMessage): Promise<void> {
  // Fetch the single row to keep things simple.
  try {
    const rows = await request<EntityOut[]>(
      'GET',
      `${SyncApiBaseURL}/${msg.entity}?since=${encodeURIComponent(new Date(new Date(msg.updated_at).getTime() - 1).toISOString())}&limit=50`
    )
    const match = rows.find(r => r.id === msg.id)
    if (match) await applyRemote(msg.entity, [match])
  } catch (err) {
    console.warn('[sync] handleWsMessage fetch failed', err)
  }
  notifyListeners(msg)
}

/* ---------- bootstrap ---------- */

const SINCE_FETCH_BATCH = 500

async function fetchEntityFull(entity: SyncEntity): Promise<void> {
  let since = sinceFor(entity)
  while (true) {
    const qs = new URLSearchParams()
    if (since) qs.set('since', since)
    qs.set('limit', String(SINCE_FETCH_BATCH))
    const rows = await request<EntityOut[]>('GET', `${SyncApiBaseURL}/${entity}?${qs}`)
    if (!rows.length) break
    await applyRemote(entity, rows)
    const last = rows[rows.length - 1]
    since = last.updated_at
    if (rows.length < SINCE_FETCH_BATCH) break
  }
}

async function bootstrapAfterLogin(): Promise<void> {
  // Pull small/eager tables. Messages & items are lazy per-dialog.
  const eager: SyncEntity[] = [
    'workspaces',
    'assistants',
    'artifacts',
    'avatarImages',
    'installedPluginsV2',
    'reactives',
    'providers',
    'dialogs'
  ]
  for (const e of eager) {
    try {
      await fetchEntityFull(e)
    } catch (err) {
      console.warn(`[sync] bootstrap ${e} failed`, err)
    }
  }
}

/* ---------- public API ---------- */

async function init(): Promise<void> {
  if (!SyncEnabled) return
  if (typeof window !== 'undefined') {
    window.addEventListener('online', () => {
      online = true
      void flush()
      connectWs()
    })
    window.addEventListener('offline', () => {
      online = false
    })
  }
  if (state.isLoggedIn) {
    try {
      await request('GET', `${SyncAuthBaseURL}/me`)
    } catch (err) {
      if (err instanceof HttpError && err.status === 401) return
      console.warn('[sync] /auth/me failed; continuing offline', err)
    }
    connectWs()
    void bootstrapAfterLogin()
    void flush()
  }
}

async function register(email: string, password: string): Promise<void> {
  const resp = await request<{ token: string; email: string }>(
    'POST',
    `${SyncAuthBaseURL}/register`,
    { email, password }
  )
  setAuth(resp.token, resp.email)
  connectWs()
  await bootstrapAfterLogin()
  await flush()
}

async function login(email: string, password: string): Promise<void> {
  const resp = await request<{ token: string; email: string }>(
    'POST',
    `${SyncAuthBaseURL}/login`,
    { email, password }
  )
  setAuth(resp.token, resp.email)
  connectWs()
  await bootstrapAfterLogin()
  await flush()
}

async function logout(): Promise<void> {
  setAuth(null, null)
  try {
    ws?.close()
  } catch {
    /* ignore */
  }
  ws = null
  clearSinceAll()
  await writeOutbox([])
}

async function push(
  entity: SyncEntity,
  op: 'put' | 'delete',
  idOrPayload: string | unknown
): Promise<void> {
  if (!SyncEnabled) return
  const pk = localPrimaryKey(entity)
  const payloadObj = idOrPayload as Record<string, unknown>
  let id: string
  let payload: Record<string, unknown> | undefined
  if (op === 'delete') {
    id = typeof idOrPayload === 'string' ? idOrPayload : String(payloadObj[pk])
  } else {
    if (typeof idOrPayload === 'string') {
      // fallback: read from local DB
      const local = await db.table(entity).get(idOrPayload)
      if (!local) return
      payload = payloadForPush(entity, local as Record<string, unknown>)
      id = idOrPayload
    } else {
      id = String(payloadObj[pk])
      payload = payloadForPush(entity, payloadObj)
    }
  }
  if (!id) return
  if (entity === 'reactives' && NON_OUTBOX_RESTRICTED_KEYS.has(id)) return
  await enqueue({ entity, op, id, payload })
}

async function pushAll(): Promise<void> {
  if (!SyncEnabled) return
  for (const entity of ALL_SYNC_ENTITIES) {
    const pk = localPrimaryKey(entity)
    const rows = await db.table(entity).toArray()
    for (const row of rows) {
      const id = String((row as Record<string, unknown>)[pk])
      if (!id) continue
      if (entity === 'reactives' && NON_OUTBOX_RESTRICTED_KEYS.has(id)) continue
      const payload = payloadForPush(entity, row as Record<string, unknown>)
      await enqueue({ entity, op: 'put', id, payload })
    }
  }
  await flush()
}

async function fetchDialogsOfWorkspace(workspaceId: string): Promise<void> {
  if (!state.isLoggedIn) return
  const qs = new URLSearchParams({ workspaceId, limit: String(SINCE_FETCH_BATCH) })
  const since = sinceFor('dialogs')
  if (since) qs.set('since', since)
  try {
    const rows = await request<EntityOut[]>('GET', `${SyncApiBaseURL}/dialogs?${qs}`)
    await applyRemote('dialogs', rows)
  } catch (err) {
    console.warn('[sync] fetchDialogsOfWorkspace failed', err)
  }
}

async function fetchMessagesOfDialog(dialogId: string): Promise<void> {
  if (!state.isLoggedIn) return
  const qs = new URLSearchParams({ dialogId, limit: String(SINCE_FETCH_BATCH) })
  try {
    let since: string | undefined
    while (true) {
      if (since) qs.set('since', since)
      else qs.delete('since')
      const rows = await request<EntityOut[]>('GET', `${SyncApiBaseURL}/messages?${qs}`)
      if (!rows.length) break
      await applyRemote('messages', rows)
      if (rows.length < SINCE_FETCH_BATCH) break
      since = rows[rows.length - 1].updated_at
    }
  } catch (err) {
    console.warn('[sync] fetchMessagesOfDialog failed', err)
  }
}

async function fetchItemsOfDialog(dialogId: string): Promise<void> {
  if (!state.isLoggedIn) return
  const qs = new URLSearchParams({ dialogId, limit: String(SINCE_FETCH_BATCH) })
  try {
    let since: string | undefined
    while (true) {
      if (since) qs.set('since', since)
      else qs.delete('since')
      const rows = await request<EntityOut[]>('GET', `${SyncApiBaseURL}/items?${qs}`)
      if (!rows.length) break
      await applyRemote('items', rows)
      if (rows.length < SINCE_FETCH_BATCH) break
      since = rows[rows.length - 1].updated_at
    }
  } catch (err) {
    console.warn('[sync] fetchItemsOfDialog failed', err)
  }
}

function onEntityChanged(listener: EntityChangeListener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/* ---------- files (used by file-storage.ts) ---------- */

async function signPut(key: string, size: number, mimeType: string): Promise<{ url: string; headers: Record<string, string> }> {
  return request<{ url: string; method: string; headers: Record<string, string> }>(
    'POST',
    `${SyncFilesBaseURL}/sign-put`,
    { key, size, mime_type: mimeType }
  )
}

async function signGet(key: string): Promise<string> {
  const resp = await request<{ url: string }>('GET', `${SyncFilesBaseURL}/sign-get/${encodeURIComponent(key)}`)
  return resp.url
}

export const syncClient = {
  state: readonly(state),
  init,
  register,
  login,
  logout,
  push,
  pushAll,
  fetchDialogsOfWorkspace,
  fetchMessagesOfDialog,
  fetchItemsOfDialog,
  onEntityChanged,
  signPut,
  signGet
}

export type SyncClient = typeof syncClient

export function isSyncEnabled(): boolean {
  return SyncEnabled
}

// Expose for debugging.
if (typeof window !== 'undefined') {
  (window as unknown as { __syncClient?: unknown }).__syncClient = syncClient
}

// Ensure Dexie Table proxy works when entity is unknown type.
void Dexie
