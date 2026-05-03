// Stage 4 / 硬前置 3 — scope-aware pull state for server-routed tables.
//
// Background: server-routed tables (`dialogs.server.ts` / `items.server.ts` /
// future `artifacts` / `messages`) historically pulled the user's full table
// on any read (`?since=lastVersion`). For tables that fan out per-workspace
// or per-dialog (items can be thousands per active user; messages tens of
// thousands), opening any single dialog forced a full-table fetch. That's
// the "Dexie Cloud full pull" behavior the migration was supposed to kill.
//
// This helper carves out *scoped* pull state. `<table>.server.ts` keeps its
// module-level `lastVersion` for full-table pulls (list / observeList /
// fallback paths) and additionally instantiates a ScopedPull for per-scope
// reads driven by `observeFind / find / findFirst / findKeys / count`. Each
// `(workspaceId | dialogId | …)` value gets its own monotonic lastVersion +
// inflight slot so concurrent component mounts coalesce into one fetch and
// later mounts hit "cache valid" state instead of re-pulling.
//
// Realtime events still arrive un-scoped (broker fans out all events for the
// authed user). `applyEvent` is the hook the realtime handler calls *after*
// it has applied the row to the local cache; we use it to bump the matching
// scope's lastVersion so the next scoped fetch starts at the right cursor.
//
// Design choices worth knowing:
//
// * scopeKey format = `<tableName>:<scopeField>:<scopeId>`. Including the
//   table name guarantees that `dialogs` keyed by `workspaceId` cannot
//   collide with `items` keyed by `dialogId` even if they share an id.
// * A single ScopedPull tracks one scope field per table (the natural FK).
//   Multi-field scopes (artifacts may want both workspaceId AND dialogId in
//   future) are out of scope for this helper; they'd need a richer scopeKey.
// * Cross-scope realtime: `applyEvent` reads `e.row.<scopeField>` to figure
//   out which scope this event belongs to. If the scope hasn't been pulled
//   yet (no entry in the map), we ignore the event — we don't speculatively
//   create scope entries from event-only data, since the scope would never
//   get a chance to "first pull" cleanly.

export interface ScopedEvent<TRow> {
  op: 'put' | 'delete'
  rev: number
  row?: { data?: TRow | null } | null
}

export interface ScopedPullOptions<TScopeId extends string = string> {
  /**
   * Used as the scopeKey prefix; must match the table name in `tableName` so
   * scopes from different tables can't accidentally share a key.
   */
  tableName: string
  /**
   * The single scope field on the wire row (e.g. 'workspaceId' for dialogs,
   * 'dialogId' for items). Used for both URL query construction and
   * `applyEvent` routing.
   */
  scopeField: string
  /**
   * Caller-provided fetcher. Receives the resolved scope query (e.g.
   * `{ workspaceId: 'ws1', since: 42 }`) and is responsible for: ① executing
   * the HTTP GET, ② decoding rows + applying them to the cache layer,
   * ③ returning the highest `version` it observed (or 0 if no rows). The
   * helper itself does NOT touch IndexedDB — that's the cache layer's job
   * and varies per table (items needs `materializeAttachment`, dialogs is
   * passthrough).
   */
  fetchFn: (
    scopeId: TScopeId,
    since: number
  ) => Promise<{ maxVersion: number }>
}

interface ScopeState {
  lastVersion: number
  inflight: Promise<void> | null
}

export interface ScopedPull<TRow, TScopeId extends string = string> {
  /**
   * Ensure a scope is pulled to its current cursor. Concurrent calls with
   * the same scopeId are coalesced into the inflight promise. After the
   * promise resolves the scope's lastVersion has been advanced to whatever
   * `fetchFn` reported.
   */
  pullScope(scopeId: TScopeId): Promise<void>
  /**
   * Read the current cursor for a scope (0 if never pulled).
   */
  lastVersionFor(scopeId: TScopeId): number
  /**
   * Notify the helper that a realtime event was just applied to the cache.
   * Bumps the matching scope's lastVersion if (a) the event's scope field
   * resolves to a known scope and (b) the event's rev is higher. Unknown
   * scopes are intentionally ignored — see the file header for rationale.
   */
  applyEvent(e: ScopedEvent<TRow>): void
  /**
   * Build the scopeKey that would be used internally for `scopeId`. Exposed
   * for tests / debugging / inter-helper composition; production callers
   * normally don't need this.
   */
  scopeKeyFor(scopeId: TScopeId): string
  /**
   * Drop all scope state. Used when the underlying IDB cache has been wiped
   * (e.g. after explicit cache-clear, logout, or schema upgrade) so the
   * next pullScope re-fetches from since=0 instead of blindly trusting the
   * stale cursor.
   */
  reset(): void
}

export function createScopedPull<TRow, TScopeId extends string = string>(
  opts: ScopedPullOptions<TScopeId>
): ScopedPull<TRow, TScopeId> {
  const states = new Map<string, ScopeState>()
  const scopeKeyFor = (scopeId: TScopeId): string =>
    `${opts.tableName}:${opts.scopeField}:${scopeId}`

  function getOrCreate(scopeId: TScopeId): ScopeState {
    const key = scopeKeyFor(scopeId)
    let s = states.get(key)
    if (!s) {
      s = { lastVersion: 0, inflight: null }
      states.set(key, s)
    }
    return s
  }

  function pullScope(scopeId: TScopeId): Promise<void> {
    const s = getOrCreate(scopeId)
    if (s.inflight) return s.inflight
    s.inflight = (async () => {
      try {
        const { maxVersion } = await opts.fetchFn(scopeId, s.lastVersion)
        if (maxVersion > s.lastVersion) s.lastVersion = maxVersion
      } finally {
        s.inflight = null
      }
    })()
    return s.inflight
  }

  function lastVersionFor(scopeId: TScopeId): number {
    return states.get(scopeKeyFor(scopeId))?.lastVersion ?? 0
  }

  function applyEvent(e: ScopedEvent<TRow>): void {
    // For 'put' events the scope field lives inside `row.data`; for
    // 'delete' events the wire row is null and we can't tell which scope
    // owned the deleted id. That's fine — a delete event the realtime
    // handler already applied to IDB doesn't need to bump scope cursors,
    // because the next scoped pull will see no rows past the cursor and
    // a re-pull would just return empty. The cursor bump matters for
    // 'put' so we can incrementally advance.
    if (e.op !== 'put') return
    const row = e.row?.data
    if (!row) return
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const scopeId = (row as any)[opts.scopeField] as TScopeId | undefined
    if (!scopeId) return
    const key = scopeKeyFor(scopeId)
    const s = states.get(key)
    // Don't speculatively create scope entries — see header.
    if (!s) return
    if (e.rev > s.lastVersion) s.lastVersion = e.rev
  }

  function reset(): void {
    states.clear()
  }

  return { pullScope, lastVersionFor, applyEvent, scopeKeyFor, reset }
}

/**
 * Best-effort extractor for a single scope field from a QuerySpec.where.
 * Returns `undefined` when the spec has no where clause, the scope field
 * isn't constrained, or the constraint isn't a single scalar (`{in: [...]}`
 * / array values are *not* treated as scoped — the caller falls back to a
 * full-table pull because we'd need N scoped pulls otherwise, and the most
 * common shape we want to optimize is `{ where: { dialogId: 'abc' } }`).
 */
export function extractScopeId<TScopeId extends string = string>(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  where: Record<string, any> | undefined,
  scopeField: string
): TScopeId | undefined {
  if (!where) return undefined
  const v = where[scopeField]
  if (typeof v === 'string' && v) return v as TScopeId
  return undefined
}
