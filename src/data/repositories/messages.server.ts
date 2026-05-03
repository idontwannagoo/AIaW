import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { Message, MessageContent } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import {
  serializeAttachment,
  materializeAttachment,
  BLOB_INLINE_MAX_BYTES,
  type AttachmentEnvelope
} from '../blob-client'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'
import { createScopedPull, extractScopeId } from './scoped-pull'

// Stage 4 / 批次-4e — server-routed messages.
//
// id-PK envelope mirrors items.server.ts. The wrinkle: messages can grow
// large from streaming token accumulation (long assistant replies, tool
// outputs). Per the Stage 4 plan we route the bytes through blob-client
// when the JSON body exceeds the threshold:
//
//   JSON.stringify(message) < 64KB → inline `contents` array on the wire
//   JSON.stringify(message) >= 64KB → spill to wire field `contentsBlob`
//                                    (AttachmentEnvelope ref to blob
//                                    store), wire `contents = []`.
//
// The IDB row stays the shape the rest of the app expects (`Message`
// with full MessageContent[]). Bytes for ref-mode payloads live in the
// `blobs` table and are fetched by signed URL on the read path.
//
// Note: messages do NOT directly carry binary attachments. Image / file
// attachments referenced by UserMessageContent.items[] /
// AssistantToolContent.result[] live in the `items` table (4c) and the
// message contents only carry StoredItemId[]. So the inline/ref decision
// here is purely about message body text size, not attachment bytes.
//
// Mandatory dialog scope (Stage 4 / 硬前置 3 + 2026-05-03 修订): unlike
// items / artifacts which gracefully degrade to a full-table pull when
// the spec has no scope, messages MUST be pulled per-dialog. The
// `?dialogId=` query parameter is mandatory backend-side (422 otherwise).
// On the client, `pullForSpec` / `list` / `observeList` / `modifyAll`
// are cache-only when called without a dialogId scope (they print a
// warning and serve whatever scoped-pull / realtime / future-bootstrap
// have already populated). They do NOT throw because legitimate UI
// surfaces (`SearchDialog.vue` "search across all dialogs") use
// `repos.messages.list()` for best-effort cross-dialog reads, and
// throwing would crash search. Specs that want to detect the
// degradation can hook console.warn or assert via the
// `__messageStreamFlush__` debug hook.
//
// Cascade-on-dialog-delete + cascade-on-workspace-delete: routers/
// dialogs.py publishes `delete` events for every message under the
// dialog before the dialog's own tombstone event; routers/workspaces.py
// extends the same pattern to all dialogs in a workspace under one
// cascade_version. Live tabs receive each event and reapply the same
// idempotent delete path.
//
// Streaming throttle: the streaming token-by-token PUT path goes through
// `composables/message-stream-flush.ts` which batches calls under a
// 200ms window / 1KB / sentence-boundary triple-trigger before invoking
// `repos.messages.update()`. This module has no awareness of streaming
// state — it just receives flushed envelopes the same as any other PUT.

interface WireMessage {
  id: string
  type: 'user' | 'assistant'
  assistantId?: string
  dialogId: string
  contents: MessageContent[]
  contentsBlob?: AttachmentEnvelope // ref-mode payload (when JSON >= 64KB)
  status: 'pending' | 'streaming' | 'failed' | 'default' | 'inputing'
  generatingSession?: string
  error?: string
  warnings?: string[]
  // Vercel ai SDK LanguageModelUsage (kept loose to avoid pinning the
  // wire format to a specific SDK minor version; the IDB row keeps the
  // typed shape via the Message interface).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  usage?: any
  modelName?: string
}

interface MessageRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: WireMessage | null
}

let lastVersion = 0
let realtimeUnsubscribe: (() => void) | null = null

function utf8ByteLength(s: string): number {
  return new TextEncoder().encode(s).length
}

async function encodeForWire(message: Message): Promise<WireMessage> {
  const wire: WireMessage = {
    id: message.id,
    type: message.type,
    assistantId: message.assistantId,
    dialogId: message.dialogId,
    contents: message.contents,
    status: message.status,
    generatingSession: message.generatingSession,
    error: message.error,
    warnings: message.warnings,
    usage: message.usage,
    modelName: message.modelName
  }
  // Decide inline vs ref based on the size of the entire serialized row.
  // Spilling only `contents` (the dominant size driver) keeps the rest
  // of the envelope addressable / queryable while the heavy bytes live
  // in BlobStore.
  const serialized = JSON.stringify(wire)
  if (utf8ByteLength(serialized) < BLOB_INLINE_MAX_BYTES) {
    return wire
  }
  const contentsJson = JSON.stringify(message.contents)
  const bytes = new TextEncoder().encode(contentsJson)
  wire.contentsBlob = await serializeAttachment(bytes, 'application/json')
  wire.contents = []
  return wire
}

async function decodeFromWire(wire: WireMessage): Promise<Message> {
  let contents: MessageContent[] = wire.contents ?? []
  if (wire.contentsBlob) {
    try {
      const blob = await materializeAttachment(wire.contentsBlob)
      const text = await blob.text()
      contents = JSON.parse(text) as MessageContent[]
    } catch (err) {
      // Don't crash the apply pipeline on a single bad ref — leave
      // contents empty and let the UI retry on demand. Same pattern as
      // items.server.ts / artifacts.server.ts.
      console.warn(
        '[messages.server] materializeAttachment failed; row written with empty contents',
        err
      )
      contents = []
    }
  }
  return {
    id: wire.id,
    type: wire.type,
    assistantId: wire.assistantId,
    dialogId: wire.dialogId,
    contents,
    status: wire.status,
    generatingSession: wire.generatingSession,
    error: wire.error,
    warnings: wire.warnings,
    usage: wire.usage,
    modelName: wire.modelName
  }
}

const scopedPull = createScopedPull<WireMessage>({
  tableName: 'messages',
  scopeField: 'dialogId',
  fetchFn: async (dialogId, since) => {
    const rows = await http.get<MessageRow[]>('/api/v1/messages', {
      query: { dialogId, since }
    })
    if (!rows.length) return { maxVersion: 0 }
    // Decode (which may hit network for ref-mode rows) BEFORE entering
    // the dexie transaction — Dexie auto-aborts if a callback awaits on
    // a non-Dexie promise.
    const prepared = await Promise.all(
      rows.map(async (row) => {
        if (row.deleted) return { row, decoded: null as Message | null }
        if (row.data) return { row, decoded: await decodeFromWire(row.data) }
        return { row, decoded: null as Message | null }
      })
    )
    let maxVersion = 0
    await db.transaction('rw', db.messages, async () => {
      for (const { row, decoded } of prepared) {
        if (row.deleted) {
          await db.messages.delete(row.id)
        } else if (decoded) {
          await db.messages.put(decoded)
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
  realtimeUnsubscribe = realtime.subscribe<MessageRow>('messages', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.messages.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          const decoded = await decodeFromWire(e.row.data)
          await db.messages.put(decoded)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
        // Forward to scopedPull AFTER cache write so scope cursors only
        // advance for events that have actually been applied locally.
        scopedPull.applyEvent(e)
      } catch (err) {
        console.warn('[messages.server] realtime apply failed', err)
      }
    })()
  })
}

// Full-table pull is a no-go for messages — backend requires `?dialogId=`
// (returns 422 otherwise) because un-scoped messages reads are too
// expensive to serve. The two legitimate code paths that touch
// `repos.messages` un-scoped are:
//
//   1. SearchDialog.vue's "search across all dialogs" mode
//      (`repos.messages.list()`)
//   2. Future bootstrap retroactive backfill (Stage 4.5)
//
// Both are best-effort over the local IDB cache: we read whatever
// scoped-pull / realtime / bootstrap has already populated and surface a
// warning so the developer console makes it clear the result is
// cache-truncated. We deliberately do NOT issue a full-table HTTP fetch
// (would 422) and we do NOT throw (would crash legitimate UI code).
function warnFullTableUse(method: string): void {
  console.warn(
    `[messages.server] ${method} called without a dialogId scope — ` +
    'returning IDB-cached rows only; results may be incomplete. ' +
    'Use find({ where: { dialogId } }) for a fresh server pull.'
  )
}

async function pullForSpec(spec: QuerySpec<Message> | undefined): Promise<void> {
  const scopeId = extractScopeId(
    spec?.where as Record<string, unknown> | undefined,
    'dialogId'
  )
  if (!scopeId) {
    warnFullTableUse('pullForSpec')
    return
  }
  await scopedPull.pullScope(scopeId)
}

function shallowMerge<T>(base: T, changes: Partial<T> | Record<string, unknown>): T {
  return { ...(base as object), ...(changes as object) } as T
}

export const serverMessagesRepository: Repository<Message, string> = (() => {
  const cache = createDexieRepository<Message, string>(
    () => db.messages as Table<Message, string>
  )

  async function putOne(value: Message): Promise<string> {
    ensureRealtimeSubscription()
    const wire = await encodeForWire(value)
    const row = await http.put<MessageRow>(`/api/v1/messages/${value.id}`, wire)
    if (row.data) {
      const decoded = await decodeFromWire(row.data)
      await db.messages.put(decoded)
    }
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<MessageRow>(`/api/v1/messages/${id}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.messages.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<MessageRow>(`/api/v1/messages/${id}`)
        if (row.deleted || !row.data) return undefined
        const decoded = await decodeFromWire(row.data)
        await db.messages.put(decoded)
        if (row.version > lastVersion) lastVersion = row.version
        return decoded
      } catch (e) {
        if (e instanceof HttpError && e.status === 404) return undefined
        throw e
      }
    },
    // list() / observeList() have no scope so we cannot pull from server
    // (backend rejects with 422). Fall back to the IDB cache so legitimate
    // best-effort callers (SearchDialog "search all dialogs") still work
    // over previously-pulled rows; surface a warning so the truncation is
    // visible in the dev console.
    async list() { warnFullTableUse('list'); return cache.list() },
    async find(spec) { await pullForSpec(spec); return cache.find(spec) },
    async findFirst(spec) { await pullForSpec(spec); return cache.findFirst(spec) },
    async findKeys(spec) { await pullForSpec(spec); return cache.findKeys(spec) },
    async count(spec) { await pullForSpec(spec); return cache.count(spec) },

    add: putOne,
    put: putOne,
    async bulkPut(values) {
      // Sequential — multiple ref-mode messages racing into putBlob can
      // collide on the (user_id, sha256) blob_refs unique constraint, and
      // serial keeps the wire calls in lockstep with realtime echoes
      // (same reasoning as items.server.ts / artifacts.server.ts).
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
    async deleteWhere(spec: QuerySpec<Message>) {
      await pullForSpec(spec)
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<Message>, changes) {
      await pullForSpec(spec)
      const rows = await cache.find(spec)
      for (const r of rows) await putOne(shallowMerge(r, changes))
      return rows.length
    },
    // modifyAll: rewrite a field on every cached row. Cache-only (no
    // server backfill) for the same reason list() is cache-only.
    async modifyAll(predicate, changes) {
      warnFullTableUse('modifyAll')
      const all = await cache.list()
      const matches = all.filter(predicate)
      for (const r of matches) await putOne(shallowMerge(r, changes))
      return matches.length
    },

    observeList: ((options) => {
      // observeList is cache-only for the same reason as list() — we have
      // no full-table server pull. Live cross-tab updates still flow via
      // realtime events, so the observed ref reflects whatever is in IDB
      // at any moment.
      warnFullTableUse('observeList')
      return cache.observeList(options)
    }) as typeof cache.observeList,
    observeFind: ((spec, options) => {
      ensureRealtimeSubscription()
      // Kick off a scope-aware pull on mount so the first render isn't
      // empty for scopes we haven't seen yet. Errors are swallowed; the
      // observe ref will repopulate as the cache catches up via realtime.
      const initial = typeof spec === 'function' ? spec() : spec
      void pullForSpec(initial).catch((err) => {
        console.warn('[messages.server] observeFind initial pull failed', err)
      })
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
