/**
 * Stage 2 / Step 2 — abstraction over a per-table change stream.
 *
 * Both Dexie's local `liveQuery` and (Step 4) the server's WebSocket pub/sub
 * end up pushing changes for the *same* logical table. Lifting that into one
 * interface lets repositories pick a backing source without leaking the
 * underlying transport. `observeList` in `repositories/dexie.ts` reads via
 * `snapshot()`; the per-row `subscribe()` channel is what cache-backed server
 * repositories will use to mirror remote events into the local table.
 */
import { liveQuery, type Table } from 'dexie'

export interface ChangeEvent<T> {
  op: 'put' | 'delete'
  id: string
  row?: T
  rev?: number
}

export interface SyncSource<T> {
  /** Current full state of the table (for initial paint / cold reads). */
  snapshot(): Promise<T[]>
  /**
   * Subscribe to incremental change events. The first emission replays the
   * current state as a stream of `put` events so subscribers can rebuild
   * state regardless of when they joined. Returns an unsubscribe function.
   */
  subscribe(onChange: (e: ChangeEvent<T>) => void): () => void
}

/**
 * Wrap a Dexie `Table` as a `SyncSource`. Diffs successive `liveQuery`
 * snapshots into per-row events. Dexie returns fresh objects on each query so
 * we can't dedup by reference equality — every appearance counts as a `put`.
 * Sinks (e.g. the future remote-cache writer) must be idempotent.
 */
export function createDexieSyncSource<T, K extends string = string>(
  getTable: () => Table<T, K>,
  getKey: (row: T) => string = (row) => (row as unknown as { id: string }).id
): SyncSource<T> {
  return {
    snapshot: () => getTable().toArray(),
    subscribe(onChange) {
      let last = new Map<string, T>()
      let initial = true
      const sub = liveQuery(() => getTable().toArray()).subscribe({
        next(rows) {
          const next = new Map<string, T>()
          for (const row of rows) next.set(getKey(row), row)
          if (initial) {
            for (const [id, row] of next) onChange({ op: 'put', id, row })
            initial = false
          } else {
            for (const [id, row] of next) {
              onChange({ op: 'put', id, row })
            }
            for (const id of last.keys()) {
              if (!next.has(id)) onChange({ op: 'delete', id })
            }
          }
          last = next
        }
      })
      return () => sub.unsubscribe()
    }
  }
}
