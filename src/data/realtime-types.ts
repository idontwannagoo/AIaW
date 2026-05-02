/**
 * Shared types for the realtime transport layer (Stage 2 / Step 3-5).
 *
 * Three transports — WS / SSE / poll — implement the same `TransportConn`
 * interface. The active singleton is chosen by `RealtimeTransport` env var:
 * `'ws' | 'sse' | 'poll' | 'auto'` (and empty = transport disabled).
 *
 * `RealtimeEvent` is the unified event shape all transports emit through
 * subscribe callbacks; cache writers (e.g. `providers.server.ts`) only see
 * this shape and don't care which transport produced it.
 */

export interface RealtimeEvent<T = unknown> {
  type: 'event'
  table: string
  op: 'put' | 'delete'
  id: string
  row?: T | null
  rev: number
}

export type TransportName = 'ws' | 'sse' | 'poll'

export interface TransportConn {
  /** Which underlying wire we're currently using. */
  readonly transport: TransportName
  /**
   * Subscribe to a table. Returns an unsubscribe function. Optional `since`
   * lets the auto-router carry over the highest seen rev when demoting from
   * one transport to another so the new transport doesn't re-replay state
   * the listener has already absorbed.
   */
  subscribe<T = unknown>(
    table: string,
    onEvent: (e: RealtimeEvent<T>) => void,
    since?: number
  ): () => void
  /** Optional: signal that auth state may have changed and the connection
   *  should re-attempt if it had given up. */
  wakeIfIdle?(): void
  /** Optional: tear down the connection and stop reconnect attempts. */
  shutdown?(): void
}
