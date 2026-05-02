/**
 * Stage 2 / Step 5 — poll fallback transport.
 *
 * Hits the existing Stage 1 `GET /api/v1/<table>?since=<rev>` endpoint on a
 * fixed interval and synthesizes `RealtimeEvent`s from the diff. There's no
 * "live" channel — convergence is bounded by the poll interval.
 *
 * Subscribe semantics match the other transports (refcount per table, single
 * shared timer per table). Slow clients don't cost the server anything beyond
 * the increment list size since `since=` keeps payloads small.
 */
import { BackendApiBaseURL } from 'src/utils/config'
import { http, HttpError } from './http'
import type { RealtimeEvent, TransportConn } from './realtime-types'

interface ProviderRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Record<string, unknown> | null
}

interface TableState {
  listeners: Set<(e: RealtimeEvent) => void>
  lastRev: number
  timer: ReturnType<typeof setInterval> | null
  inflight: boolean
}

const POLL_INTERVAL_MS = 5_000

// Mapping table → REST resource path. Mirrors what the WS / SSE transports
// know via SERIALIZERS — at Step 5 only providers is server-routed.
const TABLE_RESOURCE: Record<string, string> = {
  providers: '/api/v1/providers'
}

export class PollTransport implements TransportConn {
  readonly transport = 'poll' as const

  private tables = new Map<string, TableState>()
  private explicitlyClosed = false
  private failureListeners: Set<(reason: string) => void> = new Set()

  ready(): Promise<void> {
    // Poll is "always ready" — the first tick is what verifies the backend
    // is reachable, but we don't gate subscribe() on it. Resolve immediately
    // so the auto-router treats this as the always-available terminal step.
    return Promise.resolve()
  }

  onFailure(cb: (reason: string) => void): () => void {
    this.failureListeners.add(cb)
    return () => this.failureListeners.delete(cb)
  }

  subscribe<T = unknown>(
    table: string,
    onEvent: (e: RealtimeEvent<T>) => void,
    since?: number
  ): () => void {
    if (!TABLE_RESOURCE[table]) {
      console.warn(`[poll] unknown table ${table}, no events will fire`)
    }
    let state = this.tables.get(table)
    if (!state) {
      state = {
        listeners: new Set(),
        lastRev: since ?? 0,
        timer: null,
        inflight: false
      }
      this.tables.set(table, state)
      this.startPolling(table)
    } else if (since !== undefined && since > state.lastRev) {
      state.lastRev = since
    }
    const cb = onEvent as (e: RealtimeEvent) => void
    state.listeners.add(cb)
    return () => {
      const s = this.tables.get(table)
      if (!s) return
      s.listeners.delete(cb)
      if (s.listeners.size === 0) {
        this.stopPolling(table)
        this.tables.delete(table)
      }
    }
  }

  shutdown(): void {
    this.explicitlyClosed = true
    for (const t of [...this.tables.keys()]) this.stopPolling(t)
    this.tables.clear()
  }

  wakeIfIdle(): void {
    // No idle state. Force-tick the active tables so the user-just-logged-in
    // path doesn't have to wait a full interval for the first delta.
    for (const t of this.tables.keys()) void this.tick(t)
  }

  private startPolling(table: string): void {
    if (!BackendApiBaseURL) return
    const state = this.tables.get(table)
    if (!state) return
    if (state.timer != null) return
    // Kick off immediately so the first tick happens synchronously rather
    // than after one full interval.
    void this.tick(table)
    state.timer = setInterval(() => { void this.tick(table) }, POLL_INTERVAL_MS)
  }

  private stopPolling(table: string): void {
    const state = this.tables.get(table)
    if (!state) return
    if (state.timer != null) {
      clearInterval(state.timer)
      state.timer = null
    }
  }

  private async tick(table: string): Promise<void> {
    const state = this.tables.get(table)
    if (!state) return
    if (state.inflight) return
    if (this.explicitlyClosed) return
    const path = TABLE_RESOURCE[table]
    if (!path) return
    state.inflight = true
    try {
      const rows = await http.get<ProviderRow[]>(path, {
        query: { since: state.lastRev }
      })
      if (!rows.length) return
      for (const row of rows) {
        // Match the WS / SSE envelope contract: `row` carries the full
        // `ProviderRow` (`{id, version, updated_at, deleted, data}`) when op
        // is `put`, null on delete. Cache writers like providers.server.ts
        // unwrap `e.row.data` — passing the unwrapped provider here would
        // make `e.row.data` undefined and silently drop cache writes.
        const evt: RealtimeEvent<ProviderRow | null> = {
          type: 'event',
          table,
          op: row.deleted ? 'delete' : 'put',
          id: row.id,
          row: row.deleted ? null : row,
          rev: row.version
        }
        if (evt.rev > state.lastRev) state.lastRev = evt.rev
        for (const fn of state.listeners) {
          try { fn(evt as unknown as RealtimeEvent) } catch (e) {
            console.error('[poll] listener threw', e)
          }
        }
      }
    } catch (e) {
      // Don't tear down on transient failures (offline, 5xx); log + retry on
      // next tick. 401 is handled by http.ts auth retry already.
      if (!(e instanceof HttpError)) {
        console.warn('[poll] tick failed', e)
      } else if (e.status >= 500) {
        console.warn('[poll] server', e.status, e.message)
      } else if (e.status === 401) {
        // After http.ts's refresh + retry still 401 → auth source is dead.
        for (const fn of this.failureListeners) {
          try { fn('poll 401') } catch { /* ignore */ }
        }
      } else {
        console.warn('[poll] tick error', e.status, e.message)
      }
    } finally {
      state.inflight = false
    }
  }
}
