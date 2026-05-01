/**
 * Stage 2 / Step 3 — single multiplexed WS to the backend's `/api/v1/stream`.
 *
 * One connection per app instance, fan-out to multiple table subscribers via
 * refcount. Reconnects with exponential backoff + jitter and resumes each
 * table's subscription with the last seen `rev` so server SQL replay closes
 * any gap. Token-expired closes (4001) trigger an auth refresh before the
 * next connect attempt.
 *
 * Step 3 ships the wiring + a Console-accessible singleton (`window.aiawRealtime`).
 * Step 4 is what hooks `providers.server.ts` into this; until then nothing in
 * the UI depends on it.
 */
import { watch } from 'vue'
import { BackendApiBaseURL } from 'src/utils/config'
import { authSource } from './auth'
import type { ChangeEvent, SyncSource } from './sync-source'

export interface RealtimeEvent<T = unknown> {
  type: 'event'
  table: string
  op: 'put' | 'delete'
  id: string
  row?: T | null
  rev: number
}

export type RealtimeState =
  | 'idle' // never connected (no subscribers, or no token yet)
  | 'connecting' // WS handshake in flight
  | 'open' // ready, sending/receiving frames
  | 'reconnecting' // backoff scheduled
  | 'closed' // shut down explicitly (no subscribers)

interface TableBucket {
  listeners: Set<(e: RealtimeEvent) => void>
  lastRev: number
}

const RECONNECT_BASE_MS = 250
const RECONNECT_CAP_MS = 8_000
const RECONNECT_HARD_CAP_MS = 30_000
const CLOSE_TOKEN_EXPIRED = 4001

function wsUrlFromHttp(httpUrl: string): string {
  // http://host:port -> ws://host:port (https -> wss). Trim trailing slash so
  // we can append the path uniformly.
  return httpUrl.replace(/^http/i, 'ws').replace(/\/+$/, '')
}

class RealtimeConn {
  private ws: WebSocket | null = null
  private subs = new Map<string, TableBucket>()
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private explicitlyClosed = false
  private _state: RealtimeState = 'idle'

  get state(): RealtimeState { return this._state }

  /**
   * Subscribe to events for one table. The callback fires for each `event`
   * frame the server sends *and* once per replayed row right after the
   * (re)subscribe — Step 4's cache writer doesn't need to distinguish, since
   * `rev`-based LWW makes both idempotent.
   */
  subscribe<T = unknown>(table: string, onEvent: (e: RealtimeEvent<T>) => void): () => void {
    let bucket = this.subs.get(table)
    if (!bucket) {
      bucket = { listeners: new Set(), lastRev: 0 }
      this.subs.set(table, bucket)
    }
    const cb = onEvent as (e: RealtimeEvent) => void
    bucket.listeners.add(cb)
    if (bucket.listeners.size === 1) {
      this.ensureConnected()
      // Will only actually go on the wire if state===open; on(re)connect we
      // resubscribe everything in `subs`.
      this.sendSubscribe(table)
    }
    return () => {
      const b = this.subs.get(table)
      if (!b) return
      b.listeners.delete(cb)
      if (b.listeners.size === 0) {
        this.sendUnsubscribe(table)
        this.subs.delete(table)
        if (this.subs.size === 0) this.shutdown()
      }
    }
  }

  /**
   * Recover from `idle` when the auth source produces a token after we'd
   * given up (cold subscribe before login, or 4001 + refresh failure followed
   * by a manual re-login). Caller is the `authSource.user` watcher below.
   * No-op if we're already connecting / open / closed-on-purpose.
   */
  wakeIfIdle(): void {
    if (this._state !== 'idle') return
    if (this.subs.size === 0) return
    this.reconnectAttempt = 0
    this.ensureConnected()
  }

  /** Drop the connection and cancel any scheduled reconnect. Idempotent. */
  shutdown(): void {
    this.explicitlyClosed = true
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      try { this.ws.close(1000, 'idle') } catch { /* ignore */ }
      this.ws = null
    }
    this._state = 'closed'
  }

  private ensureConnected(): void {
    if (!BackendApiBaseURL) return
    if (this._state === 'open' || this._state === 'connecting') return
    const token = authSource.currentToken()
    if (!token) {
      // No token yet (user not logged in / refresh in flight). Sit in `idle`
      // until the next subscribe() call — typical Console-test path is
      // login-then-subscribe so this rarely matters in practice.
      this._state = 'idle'
      return
    }
    this.explicitlyClosed = false
    this._state = 'connecting'
    const url = `${wsUrlFromHttp(BackendApiBaseURL)}/api/v1/stream`
    let ws: WebSocket
    try {
      // Browser sends the subprotocol via Sec-WebSocket-Protocol; server
      // pulls the JWT out of `bearer.<token>`. Token in URL would leak into
      // proxy access logs, hence subprotocol.
      ws = new WebSocket(url, [`bearer.${token}`])
    } catch (e) {
      console.warn('[realtime] WS construct failed', e)
      this.scheduleReconnect()
      return
    }
    this.ws = ws
    ws.addEventListener('open', () => this.onOpen())
    ws.addEventListener('message', (e) => this.onMessage(typeof e.data === 'string' ? e.data : ''))
    ws.addEventListener('close', (e) => this.onClose(e.code, e.reason))
    ws.addEventListener('error', () => { /* close handler always follows */ })
  }

  private onOpen(): void {
    this._state = 'open'
    this.reconnectAttempt = 0
    // Resubscribe everything with each table's lastRev so server SQL replay
    // catches us up on whatever fired during the gap.
    for (const table of this.subs.keys()) this.sendSubscribe(table)
  }

  private onMessage(raw: string): void {
    if (!raw) return
    let msg: { type?: string; table?: string; rev?: number; op?: string; id?: string; row?: unknown; code?: string; message?: string } | undefined
    try { msg = JSON.parse(raw) } catch {
      console.warn('[realtime] bad json from server', raw)
      return
    }
    if (!msg || !msg.type) return
    switch (msg.type) {
      case 'ping':
        this.send({ type: 'pong' })
        return
      case 'event': {
        if (!msg.table || !msg.id || !msg.op) return
        const bucket = this.subs.get(msg.table)
        if (!bucket) return
        const evt: RealtimeEvent = {
          type: 'event',
          table: msg.table,
          op: msg.op as 'put' | 'delete',
          id: msg.id,
          row: msg.row as unknown,
          rev: typeof msg.rev === 'number' ? msg.rev : 0
        }
        if (evt.rev > bucket.lastRev) bucket.lastRev = evt.rev
        for (const fn of bucket.listeners) {
          try { fn(evt) } catch (e) { console.error('[realtime] listener threw', e) }
        }
        return
      }
      case 'replay-done': {
        if (!msg.table) return
        const bucket = this.subs.get(msg.table)
        if (bucket && typeof msg.rev === 'number' && msg.rev > bucket.lastRev) {
          bucket.lastRev = msg.rev
        }
        return
      }
      case 'error':
        console.warn('[realtime] server error', msg.code, msg.message)
    }
  }

  private onClose(code: number, reason: string): void {
    this.ws = null
    if (this.explicitlyClosed) {
      this._state = 'closed'
      return
    }
    if (code === CLOSE_TOKEN_EXPIRED) {
      // Server killed us because JWT exp passed. Refresh, then reconnect.
      console.info('[realtime] token expired (4001), refreshing')
      void authSource.tryRefresh().then((ok) => {
        if (ok) this.scheduleReconnect()
        else {
          console.warn('[realtime] refresh failed; staying idle until next subscribe')
          this._state = 'idle'
        }
      })
      return
    }
    if (this.subs.size === 0) {
      this._state = 'closed'
      return
    }
    console.info('[realtime] WS closed', code, reason, '— scheduling reconnect')
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer != null) return
    if (this.subs.size === 0) {
      this._state = 'closed'
      return
    }
    this._state = 'reconnecting'
    this.reconnectAttempt += 1
    const base = Math.min(RECONNECT_BASE_MS * 2 ** (this.reconnectAttempt - 1), RECONNECT_CAP_MS)
    // ±20% jitter so reconnect storms don't synchronize across tabs.
    const jitter = base * 0.2 * (Math.random() * 2 - 1)
    const delay = Math.min(RECONNECT_HARD_CAP_MS, Math.max(0, base + jitter))
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.ensureConnected()
    }, delay)
  }

  private sendSubscribe(table: string): void {
    if (this._state !== 'open') return
    const bucket = this.subs.get(table)
    this.send({ type: 'subscribe', table, since: bucket?.lastRev ?? 0 })
  }

  private sendUnsubscribe(table: string): void {
    if (this._state !== 'open') return
    this.send({ type: 'unsubscribe', table })
  }

  private send(obj: object): void {
    const ws = this.ws
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    try { ws.send(JSON.stringify(obj)) } catch (e) {
      console.warn('[realtime] send failed', e)
    }
  }
}

export const realtime = new RealtimeConn()

/**
 * SyncSource backed by REST snapshot + WS event stream.
 * `fetchSnapshot` is what cold reads / cache-rebuild call;
 * `subscribe` rides the shared WS via `realtime.subscribe`.
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

// Console hook for Step 3 manual verification. Production builds keep this
// global — it's cheap and useful for support debugging.
if (typeof window !== 'undefined') {
  ;(window as unknown as { aiawRealtime: RealtimeConn }).aiawRealtime = realtime
}

// Wake the singleton when the user (re)appears: covers (a) subscribe()
// called before login, (b) 4001 close + tryRefresh failure followed by a
// manual re-login, (c) refresh-token rotation across tabs that briefly
// nulls accessToken. Without this watch, `idle` is a terminal state until
// the next subscribe() call — meaning the UI silently stops receiving live
// events. The watch lives at module scope; the singleton has the same
// lifetime as the page so we don't need to stop it.
watch(
  () => authSource.user.value,
  (next, prev) => {
    if (next && !prev) realtime.wakeIfIdle()
  }
)
