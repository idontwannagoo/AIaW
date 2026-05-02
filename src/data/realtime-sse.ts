/**
 * Stage 2 / Step 5 — SSE downgrade transport.
 *
 * Mirrors `WsTransport` but rides a single GET stream against
 * `/api/v1/stream/sse`. EventSource itself can't send custom headers, so we
 * use the npm `eventsource` package (v3+) which lets us inject a `fetch`
 * impl and add `Authorization: Bearer <token>` ourselves. Browsers + Node
 * both accept this lib.
 *
 * Subscribe semantics match WS: refcount per table, single shared connection,
 * the EventSource is rebuilt whenever the active table set changes.
 */
import { EventSource as EventSourcePolyfill, type FetchLike } from 'eventsource'
import { BackendApiBaseURL } from 'src/utils/config'
import { authSource } from './auth'
import type { RealtimeEvent, TransportConn } from './realtime-types'

interface TableBucket {
  listeners: Set<(e: RealtimeEvent) => void>
  lastRev: number
}

const RECONNECT_BASE_MS = 500
const RECONNECT_CAP_MS = 8_000

export class SseTransport implements TransportConn {
  readonly transport = 'sse' as const

  private es: EventSourcePolyfill | null = null
  private subs = new Map<string, TableBucket>()
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private explicitlyClosed = false
  private opened = false
  private maxReconnectAttempts: number
  private deadlySignaled = false
  private readyResolvers: Array<{ resolve: () => void; reject: (e: Error) => void }> = []
  private failureListeners: Set<(reason: string) => void> = new Set()

  constructor(opts: { maxReconnectAttempts?: number } = {}) {
    this.maxReconnectAttempts = opts.maxReconnectAttempts ?? Number.POSITIVE_INFINITY
  }

  ready(timeoutMs = 5_000): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.opened) { resolve(); return }
      let settled = false
      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        reject(new Error('sse ready timeout'))
      }, timeoutMs)
      this.readyResolvers.push({
        resolve: () => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          resolve()
        },
        reject: (e) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          reject(e)
        }
      })
    })
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
    let bucket = this.subs.get(table)
    if (!bucket) {
      bucket = { listeners: new Set(), lastRev: since ?? 0 }
      this.subs.set(table, bucket)
      this.reopen()
    } else if (since !== undefined && since > bucket.lastRev) {
      bucket.lastRev = since
    }
    const cb = onEvent as (e: RealtimeEvent) => void
    bucket.listeners.add(cb)
    return () => {
      const b = this.subs.get(table)
      if (!b) return
      b.listeners.delete(cb)
      if (b.listeners.size === 0) {
        this.subs.delete(table)
        if (this.subs.size === 0) this.shutdown()
        else this.reopen()
      }
    }
  }

  wakeIfIdle(): void {
    // SSE has no idle state distinct from "connecting"; the EventSource will
    // reattempt on its own. But if we explicitly shut down due to no token
    // and the user just logged in, recreate the connection.
    if (!this.es && this.subs.size > 0 && !this.explicitlyClosed) {
      this.reopen()
    }
  }

  shutdown(): void {
    this.explicitlyClosed = true
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.es) {
      try { this.es.close() } catch { /* ignore */ }
      this.es = null
    }
    this.opened = false
    this.rejectReady(new Error('sse shut down'))
  }

  private rejectReady(err: Error): void {
    const pending = this.readyResolvers
    this.readyResolvers = []
    for (const r of pending) r.reject(err)
  }

  private resolveReady(): void {
    const pending = this.readyResolvers
    this.readyResolvers = []
    for (const r of pending) r.resolve()
  }

  private signalFailure(reason: string): void {
    if (this.deadlySignaled) return
    this.deadlySignaled = true
    for (const fn of this.failureListeners) {
      try { fn(reason) } catch (e) { console.error('[sse] failure listener', e) }
    }
    this.rejectReady(new Error(reason))
  }

  private buildUrl(): string {
    const tables = [...this.subs.keys()].join(',')
    const since = Math.max(0, ...[...this.subs.values()].map(b => b.lastRev))
    const base = `${BackendApiBaseURL}/api/v1/stream/sse`
    const params = new URLSearchParams({ tables, since: String(since) })
    return `${base}?${params.toString()}`
  }

  private reopen(): void {
    if (this.explicitlyClosed) return
    if (!BackendApiBaseURL) return
    if (this.subs.size === 0) return
    if (this.es) {
      try { this.es.close() } catch { /* ignore */ }
      this.es = null
    }
    this.opened = false
    const customFetch: FetchLike = (url, init) => {
      const headers = new Headers(init?.headers)
      const token = authSource.currentToken()
      if (token) headers.set('Authorization', `Bearer ${token}`)
      return fetch(url, { ...init, headers }) as ReturnType<FetchLike>
    }
    let es: EventSourcePolyfill
    try {
      es = new EventSourcePolyfill(this.buildUrl(), { fetch: customFetch })
    } catch (e) {
      console.warn('[sse] construct failed', e)
      this.scheduleReconnect()
      return
    }
    this.es = es
    es.addEventListener('open', () => {
      this.opened = true
      this.reconnectAttempt = 0
      this.resolveReady()
    })
    es.addEventListener('error', (ev) => {
      // EventSource v3 emits ErrorEvent with optional `code` for HTTP errors.
      // 401 = auth dead; let the http layer's refresh + reconnect handle it
      // by tearing down and rebuilding (which picks up the fresh token).
      const code = (ev as { code?: number }).code
      if (this.opened) {
        // We had at least one good open — treat as transient, rebuild after
        // backoff. EventSource auto-reconnect would fire its own GET without
        // running our customFetch closure capture, so rebuild manually.
        this.opened = false
        this.scheduleReconnect()
      } else {
        // Never connected; this is a real failure. Auto-router demotes.
        this.scheduleReconnect()
        if (code === 401) this.signalFailure('sse 401')
      }
    })
    es.addEventListener('event', (ev) => this.handleEvent((ev as MessageEvent).data, (ev as MessageEvent).lastEventId))
    es.addEventListener('replay-done', (ev) => this.handleReplayDone((ev as MessageEvent).data))
    es.addEventListener('error-frame' as 'error', (ev) => {
      // Custom server-sent `event: error` frames; native `error` listener
      // above only sees transport-level errors. But EventSource folds the
      // `error` event name into the same channel, so this listener is
      // unreachable in practice — kept for clarity.
      console.warn('[sse] server error frame', (ev as MessageEvent).data)
    })
  }

  private handleEvent(rawData: string, lastEventId: string): void {
    let parsed: { table?: string; op?: string; id?: string; row?: unknown; rev?: number } | undefined
    try { parsed = JSON.parse(rawData) } catch {
      console.warn('[sse] bad json in event', rawData)
      return
    }
    if (!parsed?.table || !parsed.id || !parsed.op) return
    const bucket = this.subs.get(parsed.table)
    if (!bucket) return
    const rev = typeof parsed.rev === 'number'
      ? parsed.rev
      : (lastEventId ? Number(lastEventId) : 0)
    const evt: RealtimeEvent = {
      type: 'event',
      table: parsed.table,
      op: parsed.op as 'put' | 'delete',
      id: parsed.id,
      row: parsed.row as unknown,
      rev
    }
    if (evt.rev > bucket.lastRev) bucket.lastRev = evt.rev
    for (const fn of bucket.listeners) {
      try { fn(evt) } catch (e) { console.error('[sse] listener threw', e) }
    }
  }

  private handleReplayDone(rawData: string): void {
    let parsed: { table?: string; rev?: number } | undefined
    try { parsed = JSON.parse(rawData) } catch { return }
    if (!parsed?.table) return
    const bucket = this.subs.get(parsed.table)
    if (bucket && typeof parsed.rev === 'number' && parsed.rev > bucket.lastRev) {
      bucket.lastRev = parsed.rev
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer != null) return
    if (this.explicitlyClosed) return
    if (this.subs.size === 0) return
    if (this.reconnectAttempt >= this.maxReconnectAttempts) {
      this.signalFailure(`sse max reconnect attempts (${this.maxReconnectAttempts}) reached`)
      return
    }
    this.reconnectAttempt += 1
    const base = Math.min(RECONNECT_BASE_MS * 2 ** (this.reconnectAttempt - 1), RECONNECT_CAP_MS)
    const jitter = base * 0.2 * (Math.random() * 2 - 1)
    const delay = Math.max(0, base + jitter)
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.reopen()
    }, delay)
  }
}
