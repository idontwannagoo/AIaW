import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { AvatarImage } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import { subscribeAuthChange } from '../auth-events'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'

// Stage 3 / 批次-3b — server-routed avatar_images.
//
// id-PK envelope (mirrors providers / assistants). The wrinkle: AvatarImage
// has an ArrayBuffer field (`contentBuffer`) which is not JSON-serializable.
// At the wire boundary we encode it base64 → string before PUT and decode
// back to ArrayBuffer when applying server rows to the cache. The IDB row
// stays the same shape the rest of the app expects.
//
// < 64KB rows ride inline in JSONB. Stage 4 hard-pre-2 will move ≥ 64KB to
// object storage with `{type:'ref',url,sha256}` references; until then this
// is just inline.

interface WireAvatarImage {
  id: string
  contentBuffer: string // base64
  mimeType: string
}

interface AvatarImageRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: WireAvatarImage | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

function bufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  // String.fromCharCode chunked to avoid blowing the stack on large inputs.
  let binary = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(
      null,
      Array.from(bytes.subarray(i, i + CHUNK))
    )
  }
  return btoa(binary)
}

function base64ToBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

function encodeForWire(row: AvatarImage): WireAvatarImage {
  return {
    id: row.id,
    contentBuffer: bufferToBase64(row.contentBuffer),
    mimeType: row.mimeType
  }
}

function decodeFromWire(wire: WireAvatarImage): AvatarImage {
  return {
    id: wire.id,
    contentBuffer: base64ToBuffer(wire.contentBuffer),
    mimeType: wire.mimeType
  }
}

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<AvatarImageRow>('avatar_images', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.avatarImages.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          await db.avatarImages.put(decodeFromWire(e.row.data))
        }
        if (e.rev > lastVersion) lastVersion = e.rev
      } catch (err) {
        console.warn('[avatar-images.server] realtime apply failed', err)
      }
    })()
  })
}

// Bug 1/2/3/4 fix — see providers.server.ts for the rationale.
subscribeAuthChange(() => {
  if (realtimeUnsubscribe) {
    try { realtimeUnsubscribe() } catch { /* ignore */ }
    realtimeUnsubscribe = null
  }
  inflight = null
  lastVersion = 0
})

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const cached = await db.avatarImages.count()
      if (cached === 0) lastVersion = 0

      const rows = await http.get<AvatarImageRow[]>('/api/v1/avatar-images', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      await db.transaction('rw', db.avatarImages, async () => {
        for (const row of rows) {
          if (row.deleted) {
            await db.avatarImages.delete(row.id)
          } else if (row.data) {
            await db.avatarImages.put(decodeFromWire(row.data))
          }
          if (row.version > lastVersion) lastVersion = row.version
        }
      })
    } finally {
      inflight = null
    }
  })()
  return inflight
}

function shallowMerge<T>(base: T, changes: Partial<T> | Record<string, unknown>): T {
  return { ...(base as object), ...(changes as object) } as T
}

export const serverAvatarImagesRepository: Repository<AvatarImage, string> = (() => {
  const cache = createDexieRepository<AvatarImage, string>(
    () => db.avatarImages as Table<AvatarImage, string>
  )

  async function putOne(value: AvatarImage): Promise<string> {
    ensureRealtimeSubscription()
    const row = await http.put<AvatarImageRow>(
      `/api/v1/avatar-images/${value.id}`,
      encodeForWire(value)
    )
    if (row.data) await db.avatarImages.put(decodeFromWire(row.data))
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<AvatarImageRow>(`/api/v1/avatar-images/${id}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.avatarImages.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<AvatarImageRow>(`/api/v1/avatar-images/${id}`)
        if (row.deleted || !row.data) return undefined
        const decoded = decodeFromWire(row.data)
        await db.avatarImages.put(decoded)
        if (row.version > lastVersion) lastVersion = row.version
        return decoded
      } catch (e) {
        if (e instanceof HttpError && e.status === 404) return undefined
        throw e
      }
    },
    async list() { await pull(); return cache.list() },
    async find(spec) { await pull(); return cache.find(spec) },
    async findFirst(spec) { await pull(); return cache.findFirst(spec) },
    async findKeys(spec) { await pull(); return cache.findKeys(spec) },
    async count(spec) { await pull(); return cache.count(spec) },

    add: putOne,
    put: putOne,
    async bulkPut(values) {
      let last = ''
      for (const v of values) last = await putOne(v)
      return last
    },
    async update(id, changes) {
      const current = await cache.get(id)
      if (!current) return 0
      await putOne(shallowMerge(current, changes))
      return 1
    },
    delete: deleteOne,
    async bulkDelete(ids) {
      for (const id of ids) await deleteOne(id)
    },
    async deleteWhere(spec: QuerySpec<AvatarImage>) {
      await pull()
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<AvatarImage>, changes) {
      await pull()
      const rows = await cache.find(spec)
      for (const r of rows) await putOne(shallowMerge(r, changes))
      return rows.length
    },
    async modifyAll(predicate, changes) {
      await pull()
      const all = await cache.list()
      const matches = all.filter(predicate)
      for (const r of matches) await putOne(shallowMerge(r, changes))
      return matches.length
    },

    observeList: ((options) => {
      ensureRealtimeSubscription()
      return cache.observeList(options)
    }) as typeof cache.observeList,
    observeFind: ((spec, options) => {
      ensureRealtimeSubscription()
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
