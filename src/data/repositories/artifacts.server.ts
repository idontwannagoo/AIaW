import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { Artifact, ArtifactVersion } from 'src/utils/types'
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

// Stage 4 / 批次-4d — server-routed artifacts. id-PK envelope mirrors
// dialogs.server.ts. Unique to artifacts is the `versions` field, which
// can grow large (markdown documents, code snippets, multi-revision
// history). Per the Stage 4 plan we route the bytes through blob-client:
//
//   JSON.stringify(versions) < 64KB → inline `versions` array on the wire
//   JSON.stringify(versions) >= 64KB → spill to wire field `versionsBlob`
//                                     (AttachmentEnvelope ref to blob
//                                     store), wire `versions = []`.
//
// The IDB row stays the shape the rest of the app expects (full
// ArtifactVersion[] with Date objects). On the wire, dates roundtrip as
// ISO strings inside JSON; we revive them in `decodeFromWire`.
//
// Note on scope: the frontend `Artifact` type only has `workspaceId` (no
// `dialogId`). Plan 2026-05-03 修订 originally listed double-scope
// (workspaceId + dialogId) but that doesn't match the source-of-truth
// shape — see models/artifact.py header. We follow the same precedent
// the 4c items refresh set: code wins over stale plan text. Single-scope
// pull keyed on `workspaceId`, mirroring dialogs.server.ts.
//
// Cascade-on-workspace-delete: routers/workspaces.py publishes a stream
// of `delete` events for every artifact under the workspace (alongside
// the dialogs / items batch under one cascade_version) before the
// workspace's own tombstone event. Live tabs receive each event and
// reapply the same idempotent delete path.
//
// Ref-mode failure tolerance: like items.server.ts, materialization may
// hit the network (presigned URL fetch). If that fails we still write
// the row to IDB but leave `versions = []`; the UI re-fetches on demand.

interface WireArtifactVersion {
  date: string // ISO string (Date is JSON-stringified)
  text: string
}

interface WireArtifact {
  id: string
  name: string
  workspaceId: string
  versions: WireArtifactVersion[]
  versionsBlob?: AttachmentEnvelope // ref-mode payload (when versions>=64KB)
  currIndex: number
  readable: boolean
  writable: boolean
  open: boolean
  language?: string
  tmp: string
}

interface ArtifactRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: WireArtifact | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

function utf8ByteLength(s: string): number {
  // TextEncoder is the only correct way to count utf-8 bytes; .length is
  // utf-16 code units. The threshold check needs real byte size because
  // that's what the backend stores / what fetchBlob downloads.
  return new TextEncoder().encode(s).length
}

async function encodeForWire(artifact: Artifact): Promise<WireArtifact> {
  const versionsJson = JSON.stringify(
    artifact.versions.map((v) => ({
      date: v.date instanceof Date ? v.date.toISOString() : v.date,
      text: v.text
    }))
  )
  const wire: WireArtifact = {
    id: artifact.id,
    name: artifact.name,
    workspaceId: artifact.workspaceId,
    versions: [], // overwritten below in inline path
    currIndex: artifact.currIndex,
    readable: artifact.readable,
    writable: artifact.writable,
    open: artifact.open,
    language: artifact.language,
    tmp: artifact.tmp
  }
  if (utf8ByteLength(versionsJson) < BLOB_INLINE_MAX_BYTES) {
    // Inline: keep ISO string `date` on the wire (JSON-safe). The decode
    // path revives Date objects.
    wire.versions = JSON.parse(versionsJson) as WireArtifactVersion[]
    return wire
  }
  // Ref: serializeAttachment will route to PUT /api/v1/blobs since the
  // bytes exceed BLOB_INLINE_MAX_BYTES. application/json content type so
  // the blob store can serve it back with a useful MIME for debugging.
  const bytes = new TextEncoder().encode(versionsJson)
  wire.versionsBlob = await serializeAttachment(bytes, 'application/json')
  // wire.versions stays []; decode path checks versionsBlob first.
  return wire
}

async function decodeFromWire(wire: WireArtifact): Promise<Artifact> {
  let versions: ArtifactVersion[] = []
  if (wire.versionsBlob) {
    try {
      const blob = await materializeAttachment(wire.versionsBlob)
      const text = await blob.text()
      const parsed = JSON.parse(text) as WireArtifactVersion[]
      versions = parsed.map((v) => ({ date: new Date(v.date), text: v.text }))
    } catch (err) {
      // Don't crash the apply pipeline on a single bad ref — leave
      // versions empty and let the UI retry on demand. Plan 2026-05-03
      // 修订 calls this out explicitly (same pattern as items.server.ts
      // for contentBuffer).
      console.warn(
        '[artifacts.server] materializeAttachment failed; row written with empty versions',
        err
      )
    }
  } else if (wire.versions) {
    versions = wire.versions.map((v) => ({
      date: new Date(v.date),
      text: v.text
    }))
  }
  return {
    id: wire.id,
    name: wire.name,
    workspaceId: wire.workspaceId,
    versions,
    currIndex: wire.currIndex,
    readable: wire.readable,
    writable: wire.writable,
    open: wire.open,
    language: wire.language,
    tmp: wire.tmp
  }
}

const scopedPull = createScopedPull<WireArtifact>({
  tableName: 'artifacts',
  scopeField: 'workspaceId',
  fetchFn: async (workspaceId, since) => {
    const rows = await http.get<ArtifactRow[]>('/api/v1/artifacts', {
      query: { workspaceId, since }
    })
    if (!rows.length) return { maxVersion: 0 }
    // Decode (which may hit network for ref-mode rows) BEFORE entering
    // the dexie transaction — Dexie auto-aborts if a callback awaits on
    // a non-Dexie promise.
    const prepared = await Promise.all(
      rows.map(async (row) => {
        if (row.deleted) return { row, decoded: null as Artifact | null }
        if (row.data) return { row, decoded: await decodeFromWire(row.data) }
        return { row, decoded: null as Artifact | null }
      })
    )
    let maxVersion = 0
    await db.transaction('rw', db.artifacts, async () => {
      for (const { row, decoded } of prepared) {
        if (row.deleted) {
          await db.artifacts.delete(row.id)
        } else if (decoded) {
          await db.artifacts.put(decoded)
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
  realtimeUnsubscribe = realtime.subscribe<ArtifactRow>('artifacts', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.artifacts.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          const decoded = await decodeFromWire(e.row.data)
          await db.artifacts.put(decoded)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
        // Forward to scopedPull AFTER cache write so scope cursors only
        // advance for events that have actually been applied locally.
        scopedPull.applyEvent(e)
      } catch (err) {
        console.warn('[artifacts.server] realtime apply failed', err)
      }
    })()
  })
}

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const cached = await db.artifacts.count()
      if (cached === 0) {
        lastVersion = 0
        scopedPull.reset()
      }

      const rows = await http.get<ArtifactRow[]>('/api/v1/artifacts', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      const prepared = await Promise.all(
        rows.map(async (row) => {
          if (row.deleted) return { row, decoded: null }
          if (row.data) return { row, decoded: await decodeFromWire(row.data) }
          return { row, decoded: null }
        })
      )
      await db.transaction('rw', db.artifacts, async () => {
        for (const { row, decoded } of prepared) {
          if (row.deleted) {
            await db.artifacts.delete(row.id)
          } else if (decoded) {
            await db.artifacts.put(decoded)
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

async function pullForSpec(spec: QuerySpec<Artifact> | undefined): Promise<void> {
  const scopeId = extractScopeId(
    spec?.where as Record<string, unknown> | undefined,
    'workspaceId'
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

export const serverArtifactsRepository: Repository<Artifact, string> = (() => {
  const cache = createDexieRepository<Artifact, string>(
    () => db.artifacts as Table<Artifact, string>
  )

  async function putOne(value: Artifact): Promise<string> {
    ensureRealtimeSubscription()
    const wire = await encodeForWire(value)
    const row = await http.put<ArtifactRow>(`/api/v1/artifacts/${value.id}`, wire)
    if (row.data) {
      const decoded = await decodeFromWire(row.data)
      await db.artifacts.put(decoded)
    }
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<ArtifactRow>(`/api/v1/artifacts/${id}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.artifacts.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<ArtifactRow>(`/api/v1/artifacts/${id}`)
        if (row.deleted || !row.data) return undefined
        const decoded = await decodeFromWire(row.data)
        await db.artifacts.put(decoded)
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
      // Sequential — multiple artifacts spilling versions to ref-mode at
      // once would race the (user_id, sha256) blob_refs unique constraint
      // for shared sha256s, and serial keeps wire calls in lockstep with
      // realtime echoes (same reasoning as items.server.ts).
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
    async deleteWhere(spec: QuerySpec<Artifact>) {
      await pullForSpec(spec)
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<Artifact>, changes) {
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
        console.warn('[artifacts.server] observeFind initial pull failed', err)
      })
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
