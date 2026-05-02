/**
 * Stage 2 / Step 5 — realtime transport dispatcher.
 *
 * Picks the active transport from `RealtimeTransport`:
 *   ''     → no transport, no subscription (Stage 1 behavior)
 *   'ws'   → `WsTransport`
 *   'sse'  → `SseTransport`
 *   'poll' → `PollTransport`
 *   'auto' → `AutoTransport` — tries ws → sse → poll, demoting on failure.
 *
 * Public API: `realtime.subscribe(table, onEvent, since?)`,
 * `realtime.transport`, `realtime.wakeIfIdle()`, `realtime.shutdown()`.
 *
 * Cache writers (`providers.server.ts`) only see the `TransportConn`
 * interface — they don't care which wire they're talking to.
 */
import { watch } from 'vue'
import { RealtimeTransport } from 'src/utils/config'
import { authSource } from './auth'
import { WsTransport } from './realtime-ws'
import { SseTransport } from './realtime-sse'
import { PollTransport } from './realtime-poll'
import type { ChangeEvent, SyncSource } from './sync-source'
import type { RealtimeEvent, TransportConn, TransportName } from './realtime-types'

export type { RealtimeEvent } from './realtime-types'
export type { RealtimeState } from './realtime-ws'

interface ActiveSubscription<T = unknown> {
  table: string
  onEvent: (e: RealtimeEvent<T>) => void
  unsubscribe: () => void
  lastRev: number
}

/**
 * Probes ws, then sse, then poll. On failure of one transport, demotes by
 * tearing down its subs, picking the next, and re-subscribing every active
 * listener. Once on poll we stay there — poll is the always-available
 * terminal step.
 */
class AutoTransport implements TransportConn {
  private current: TransportConn
  private active: Set<ActiveSubscription> = new Set()
  private demoting = false

  constructor() {
    this.current = this.makeWs()
    void this.probe()
  }

  get transport(): TransportName {
    return this.current.transport
  }

  subscribe<T = unknown>(
    table: string,
    onEvent: (e: RealtimeEvent<T>) => void,
    since?: number
  ): () => void {
    const sub: ActiveSubscription<T> = {
      table,
      onEvent,
      lastRev: since ?? 0,
      unsubscribe: () => { /* placeholder, overwritten right below */ }
    }
    this.active.add(sub as unknown as ActiveSubscription)
    sub.unsubscribe = this.current.subscribe<T>(table, (e) => {
      if (e.rev > sub.lastRev) sub.lastRev = e.rev
      onEvent(e)
    }, sub.lastRev)
    return () => {
      try { sub.unsubscribe() } catch { /* ignore */ }
      this.active.delete(sub as unknown as ActiveSubscription)
    }
  }

  wakeIfIdle(): void {
    this.current.wakeIfIdle?.()
  }

  shutdown(): void {
    for (const sub of this.active) {
      try { sub.unsubscribe() } catch { /* ignore */ }
    }
    this.active.clear()
    this.current.shutdown?.()
  }

  private makeWs(): WsTransport {
    const t = new WsTransport({ maxReconnectAttempts: 3 })
    t.onFailure((reason) => this.demote(t, reason))
    return t
  }

  private makeSse(): SseTransport {
    const t = new SseTransport({ maxReconnectAttempts: 2 })
    t.onFailure((reason) => this.demote(t, reason))
    return t
  }

  private makePoll(): PollTransport {
    const t = new PollTransport()
    return t
  }

  private async probe(): Promise<void> {
    // Each transport's `ready()` resolves when it's confirmed working;
    // rejects on timeout / explicit shutdown. We give WS 3s, SSE 5s before
    // demoting. Poll is the terminal step and always passes.
    if (this.current instanceof WsTransport) {
      const ws = this.current
      try {
        await ws.ready(3_000)
      } catch (e) {
        this.demote(ws, e instanceof Error ? e.message : 'ws probe failed')
      }
    } else if (this.current instanceof SseTransport) {
      const sse = this.current
      try {
        await sse.ready(5_000)
      } catch (e) {
        this.demote(sse, e instanceof Error ? e.message : 'sse probe failed')
      }
    }
  }

  private demote(failed: TransportConn, reason: string): void {
    if (this.demoting) return
    if (failed !== this.current) return
    this.demoting = true
    console.info(`[realtime] demoting ${failed.transport}: ${reason}`)

    // Tear down all listeners on the failed transport. We can't call
    // `failed.shutdown()` directly because each `subscribe` returned a
    // closure into it; the active set carries those closures.
    for (const sub of this.active) {
      try { sub.unsubscribe() } catch { /* ignore */ }
    }
    failed.shutdown?.()

    // Pick next.
    let next: TransportConn
    if (failed.transport === 'ws') next = this.makeSse()
    else next = this.makePoll()
    this.current = next

    // Re-subscribe everything against the new transport, carrying lastRev so
    // we don't replay-deliver events the listener already absorbed (LWW
    // makes duplicates harmless, but skipping them is cheaper).
    for (const sub of this.active) {
      const onEvent = (e: RealtimeEvent) => {
        if (e.rev > sub.lastRev) sub.lastRev = e.rev
        sub.onEvent(e)
      }
      sub.unsubscribe = next.subscribe(sub.table, onEvent, sub.lastRev)
    }

    this.demoting = false
    void this.probe()
  }
}

/**
 * Stub used when `RealtimeTransport === ''` — keeps the public API stable
 * for callers that always invoke `realtime.subscribe(...)` without checking
 * the env first. Subscriptions are no-ops.
 */
class NullTransport implements TransportConn {
  readonly transport = 'poll' as const // dummy; we stub subscribe to a no-op
  subscribe(): () => void { return () => { /* no-op */ } }
}

function pickTransport(): TransportConn {
  switch (RealtimeTransport) {
    case 'ws': return new WsTransport()
    case 'sse': return new SseTransport()
    case 'poll': return new PollTransport()
    case 'auto': return new AutoTransport()
    default: return new NullTransport()
  }
}

export const realtime: TransportConn = pickTransport()

/**
 * SyncSource backed by a REST snapshot + the chosen realtime transport.
 * Identical wire-shape to the Step 3 export — only the underlying transport
 * may differ.
 */
export function createRemoteSyncSource<T>(
  table: string,
  fetchSnapshot: () => Promise<T[]>
): SyncSource<T> {
  return {
    snapshot: fetchSnapshot,
    subscribe(onChange) {
      return realtime.subscribe<T>(table, (e) => {
        const out: ChangeEvent<T> = {
          op: e.op,
          id: e.id,
          rev: e.rev
        }
        if (e.op === 'put' && e.row != null) out.row = e.row
        onChange(out)
      })
    }
  }
}

// Console hook for manual verification (Step 3 used `realtime` directly,
// Step 5 adds `transport` so e2e specs can assert the active wire).
if (typeof window !== 'undefined') {
  ;(window as unknown as { aiawRealtime: TransportConn }).aiawRealtime = realtime
}

// Re-attach when auth state flips: covers cold subscribe-before-login,
// 4001 + refresh-failure followed by manual re-login, and refresh-token
// rotations across tabs that briefly null accessToken. Without this watch,
// `idle` is terminal until the next subscribe() call.
watch(
  () => authSource.user.value,
  (next, prev) => {
    if (next && !prev) realtime.wakeIfIdle?.()
  }
)
