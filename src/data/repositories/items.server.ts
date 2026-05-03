import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { StoredItem } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import {
  serializeAttachment,
  materializeAttachment,
  type AttachmentEnvelope
} from '../blob-client'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'
import { createScopedPull, extractScopeId } from './scoped-pull'

// Stage 4 / 批次-4c — server-routed items.
//
// id-PK envelope mirrors dialogs/workspaces. The wrinkle: StoredItem
// carries an optional `contentBuffer: ArrayBuffer` field (5MB PDF, image
// bytes, etc — items is the message-attachment carrier). On the wire,
// `contentBuffer` is serialized via `blob-client.serializeAttachment`
// which decides inline-vs-ref by the 64KB threshold:
//
//   < 64KB → { type:'inline', data:base64, content_type, size }
//   ≥ 64KB → { type:'ref',    url, sha256, size, content_type }
//
// Bytes for ref-mode attachments live in the `blobs` table and are
// fetched by signed URL on the read path. The IDB row stays the shape
// the rest of the app expects (StoredItem with ArrayBuffer).
//
// The server treats `data` as opaque JSON, so all the inline/ref logic
// is on this side. Per plan 2026-05-03 修订 the materialization path
// must tolerate fetchBlob failures — if the signed URL has expired or
// the network is flaky, we land the row in IDB with contentBuffer left
// undefined; the UI re-fetches on demand.
//
// Cascade-on-workspace-delete is handled server-side: routers/
// workspaces.py publishes a stream of `delete` events for every dialog
// AND every item under those dialogs before the workspace's own
// tombstone event (all sharing one cascade_version). Live tabs receive
// each event and reapply the same idempotent delete path.
//
// Stage 4 / 硬前置 3 — scoped pull. `observeFind / find / findFirst /
// findKeys / count` inspect `spec.where.dialogId`; if a single scalar
// dialogId is present, the read goes through `scopedPull` which fetches
// `?dialogId=Y&since=<scope-cursor>` and bumps that scope's cursor in
// isolation from the full-table cursor. Spec-less or multi-value reads
// fall back to the full-table `pull()` for compatibility (e.g. a legacy
// `repos.items.list()` call that wants to enumerate everything).

interface WireItem {
  id: string
  dialogId: string
  type: 'text' | 'file' | 'quote'
  references: number
  contentText?: string
  name?: string
  mimeType?: string
  contentBuffer?: AttachmentEnvelope
}

interface ItemRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: WireItem | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

async function encodeForWire(item: StoredItem): Promise<WireItem> {
  const wire: WireItem = {
    id: item.id,
    dialogId: item.dialogId,
    type: item.type,
    references: item.references,
    contentText: item.contentText,
    name: item.name,
    mimeType: item.mimeType
  }
  if (item.contentBuffer) {
    wire.contentBuffer = await serializeAttachment(
      item.contentBuffer,
      item.mimeType
    )
  }
  return wire
}

async function decodeFromWire(wire: WireItem): Promise<StoredItem> {
  const item: StoredItem = {
    id: wire.id,
    dialogId: wire.dialogId,
    type: wire.type,
    references: wire.references,
    contentText: wire.contentText,
    name: wire.name,
    mimeType: wire.mimeType
  }
  if (wire.contentBuffer) {
    try {
      const blob = await materializeAttachment(wire.contentBuffer)
      item.contentBuffer = await blob.arrayBuffer()
    } catch (err) {
      // Don't crash the apply pipeline on a single bad ref — leave
      // contentBuffer undefined and let the UI retry on demand. Plan
      // 2026-05-03 修订 calls this out explicitly.
      console.warn(
        '[items.server] materializeAttachment failed; row written without contentBuffer',
        err
      )
    }
  }
  return item
}

const scopedPull = createScopedPull<WireItem>({
  tableName: 'items',
  scopeField: 'dialogId',
  fetchFn: async (dialogId, since) => {
    const rows = await http.get<ItemRow[]>('/api/v1/items', {
      query: { dialogId, since }
    })
    if (!rows.length) return { maxVersion: 0 }
    // Decode (which may hit network for ref-mode rows) BEFORE entering
    // the dexie transaction — Dexie auto-aborts if a callback awaits on
    // a non-Dexie promise.
    const prepared = await Promise.all(
      rows.map(async (row) => {
        if (row.deleted) return { row, decoded: null as StoredItem | null }
        if (row.data) return { row, decoded: await decodeFromWire(row.data) }
        return { row, decoded: null as StoredItem | null }
      })
    )
    let maxVersion = 0
    await db.transaction('rw', db.items, async () => {
      for (const { row, decoded } of prepared) {
        if (row.deleted) {
          await db.items.delete(row.id)
        } else if (decoded) {
          await db.items.put(decoded)
        }
        if (row.version > maxVersion) maxVersion = row.version
        if (row.version > lastVersion) lastVersion = row.version
      }
    })
    return { maxVersion }
  }
})

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<ItemRow>('items', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.items.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          const decoded = await decodeFromWire(e.row.data)
          await db.items.put(decoded)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
        // Forward to scopedPull AFTER cache write so scope cursors only
        // advance for events that have actually been applied locally.
        scopedPull.applyEvent(e)
      } catch (err) {
        console.warn('[items.server] realtime apply failed', err)
      }
    })()
  })
}

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const cached = await db.items.count()
      if (cached === 0) {
        lastVersion = 0
        scopedPull.reset()
      }

      const rows = await http.get<ItemRow[]>('/api/v1/items', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      // Decode (which may hit network for ref-mode rows) BEFORE entering
      // the dexie transaction — Dexie auto-aborts if a callback awaits
      // on a non-Dexie promise.
      const prepared = await Promise.all(
        rows.map(async (row) => {
          if (row.deleted) return { row, decoded: null }
          if (row.data) return { row, decoded: await decodeFromWire(row.data) }
          return { row, decoded: null }
        })
      )
      await db.transaction('rw', db.items, async () => {
        for (const { row, decoded } of prepared) {
          if (row.deleted) {
            await db.items.delete(row.id)
          } else if (decoded) {
            await db.items.put(decoded)
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

async function pullForSpec(spec: QuerySpec<StoredItem> | undefined): Promise<void> {
  const scopeId = extractScopeId(
    spec?.where as Record<string, unknown> | undefined,
    'dialogId'
  )
  if (scopeId) {
    await scopedPull.pullScope(scopeId)
    return
  }
  await pull()
}

function shallowMerge<T>(base: T, changes: Partial<T> | Record<string, unknown>): T {
  return { ...(base as object), ...(changes as object) } as T
}

export const serverItemsRepository: Repository<StoredItem, string> = (() => {
  const cache = createDexieRepository<StoredItem, string>(
    () => db.items as Table<StoredItem, string>
  )

  async function putOne(value: StoredItem): Promise<string> {
    ensureRealtimeSubscription()
    const wire = await encodeForWire(value)
    const row = await http.put<ItemRow>(`/api/v1/items/${value.id}`, wire)
    if (row.data) {
      const decoded = await decodeFromWire(row.data)
      await db.items.put(decoded)
    }
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<ItemRow>(`/api/v1/items/${id}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.items.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<ItemRow>(`/api/v1/items/${id}`)
        if (row.deleted || !row.data) return undefined
        const decoded = await decodeFromWire(row.data)
        await db.items.put(decoded)
        if (row.version > lastVersion) lastVersion = row.version
        return decoded
      } catch (e) {
        if (e instanceof HttpError && e.status === 404) return undefined
        throw e
      }
    },
    async list() { await pull(); return cache.list() },
    async find(spec) { await pullForSpec(spec); return cache.find(spec) },
    async findFirst(spec) { await pullForSpec(spec); return cache.findFirst(spec) },
    async findKeys(spec) { await pullForSpec(spec); return cache.findKeys(spec) },
    async count(spec) { await pullForSpec(spec); return cache.count(spec) },

    add: putOne,
    put: putOne,
    async bulkPut(values) {
      // Sequential — multiple sha256s racing into putBlob can collide on
      // the (user_id, sha256) blob_refs unique constraint, and serial
      // also keeps the wire calls in lockstep with realtime echoes.
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
    async deleteWhere(spec: QuerySpec<StoredItem>) {
      await pullForSpec(spec)
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<StoredItem>, changes) {
      await pullForSpec(spec)
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
      // Kick off a scope-aware pull on mount so the first render isn't
      // empty for scopes we haven't seen yet. Errors are swallowed; the
      // observe ref will repopulate as the cache catches up via realtime.
      const initial = typeof spec === 'function' ? spec() : spec
      void pullForSpec(initial).catch((err) => {
        console.warn('[items.server] observeFind initial pull failed', err)
      })
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
