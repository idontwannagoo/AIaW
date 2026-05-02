/**
 * Stage 2 / Step 3 — single multiplexed WS to the backend's `/api/v1/stream`.
 *
 * One connection per app instance, fan-out to multiple table subscribers via
 * refcount. Reconnects with exponential backoff + jitter and resumes each
 * table's subscription with the last seen `rev` so server SQL replay closes
 * any gap. Token-expired closes (4001) trigger an auth refresh before the
 * next connect attempt.
 *
 * The class is also driven by the auto-router (Step 5): `ready(timeoutMs)`
 * exposes a one-shot promise so the router can demote to SSE if WS can't
 * open within a budget, and `failed()` exposes the post-init failure signal.
 */
import { BackendApiBaseURL } from 'src/utils/config'
import { authSource } from './auth'
import type { RealtimeEvent, TransportConn } from './realtime-types'

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

export interface WsTransportOptions {
  /** Max consecutive reconnect attempts before declaring the transport dead.
   *  Used by the auto-router; default `Infinity` keeps stand-alone WS-only
   *  profile behavior (retry forever). */
  maxReconnectAttempts?: number
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

export class WsTransport implements TransportConn {
  readonly transport = 'ws' as const

  private ws: WebSocket | null = null
  private subs = new Map<string, TableBucket>()
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private explicitlyClosed = false
  private _state: RealtimeState = 'idle'
  private maxReconnectAttempts: number
  private deadlySignaled = false
  private readyResolvers: Array<{ resolve: () => void; reject: (e: Error) => void }> = []
  private failureListeners: Set<(reason: string) => void> = new Set()

  constructor(opts: WsTransportOptions = {}) {
    this.maxReconnectAttempts = opts.maxReconnectAttempts ?? Number.POSITIVE_INFINITY
  }

  get state(): RealtimeState { return this._state }

  /**
   * Resolves on the next successful `open`; rejects on timeout, explicit
   * shutdown, or after `maxReconnectAttempts` attempts. Used by the
   * auto-router to bail to the next transport.
   */
  ready(timeoutMs = 5_000): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this._state === 'open') {
        resolve()
        return
      }
      let settled = false
      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        reject(new Error('ws ready timeout'))
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
    } else if (since !== undefined && since > bucket.lastRev) {
      bucket.lastRev = since
    }
    const cb = onEvent as (e: RealtimeEvent) => void
    bucket.listeners.add(cb)
    if (bucket.listeners.size === 1) {
      this.ensureConnected()
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
   * given up. No-op if connecting / open / closed-on-purpose.
   */
  wakeIfIdle(): void {
    if (this._state !== 'idle') return
    if (this.subs.size === 0) return
    this.reconnectAttempt = 0
    this.deadlySignaled = false
    this.ensureConnected()
  }

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
    this.rejectReady(new Error('ws shut down'))
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
      try { fn(reason) } catch (e) { console.error('[ws] failure listener', e) }
    }
    this.rejectReady(new Error(reason))
  }

  private ensureConnected(): void {
    if (!BackendApiBaseURL) return
    if (this._state === 'open' || this._state === 'connecting') return
    const token = authSource.currentToken()
    if (!token) {
      this._state = 'idle'
      return
    }
    this.explicitlyClosed = false
    this._state = 'connecting'
    const url = `${wsUrlFromHttp(BackendApiBaseURL)}/api/v1/stream`
    let ws: WebSocket
    try {
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
    for (const table of this.subs.keys()) this.sendSubscribe(table)
    this.resolveReady()
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
    if (this.reconnectAttempt >= this.maxReconnectAttempts) {
      this._state = 'idle'
      this.signalFailure(`ws max reconnect attempts (${this.maxReconnectAttempts}) reached`)
      return
    }
    this._state = 'reconnecting'
    this.reconnectAttempt += 1
    const base = Math.min(RECONNECT_BASE_MS * 2 ** (this.reconnectAttempt - 1), RECONNECT_CAP_MS)
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
