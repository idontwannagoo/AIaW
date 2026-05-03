// Stage 4 / 批次-4e — streaming PUT throttle for messages.
//
// Background: the streaming token loop in DialogView.vue::stream calls
// `repos.messages.update(id, { contents })` on every `text-delta` /
// `reasoning-delta` chunk emitted by the AI SDK. Token streams arrive at
// ~10–30Hz; coupled with full-envelope HTTP PUT to backend + WS event
// fan-out, the historical 50ms quasar `throttle` was already barely
// enough for single-tab use. With server-routed messages and inline-
// envelope realtime each PUT round-trips through PG + the broker's
// per-event queue (close-on-overflow at maxsize=200), so a long
// assistant reply can blow the queue.
//
// This helper batches updates with a triple trigger — first to fire
// wins:
//
//   ① 200ms time window — `lastFlushAt` + 200 <= now
//   ② 1KB text accumulated since the last flush
//   ③ sentence boundary at the end of the latest enqueued contents
//      (matches `[。？！.?!\n]$` of the cumulative assistant message text)
//
// On any of those, the helper invokes the caller-provided `flush(value)`
// with the latest enqueued envelope. There is no delta protocol on the
// wire — every flush PUTs the full current message envelope (the server
// has no notion of partial updates and keeping the wire format uniform
// with non-streaming PUTs is the whole point of "inline envelope
// streaming sync").
//
// Non-streaming PUTs MUST NOT go through this helper — DialogView.vue
// keeps `repos.messages.update()` for one-off updates so they remain
// synchronous from the caller's perspective.
//
// At the end of a streaming session (the `done` event in the AI SDK
// loop or the `catch` branch after an abort), the caller invokes
// `await stop()` which forces a final flush + tears down the timer. The
// caller MUST await `stop()` before performing any "finalize" PUT (e.g.
// `repos.messages.update(id, { status: 'default', usage })`) so the
// final envelope has consistent bytes across all subscribers.
//
// Test hooks: `__messageStreamFlush__` on the window (EXPOSE_DB-gated)
// exposes the factory and the active flusher's stats so specs can:
//   - Construct a flusher with shorter window for quick tests
//   - Read flush count / bytes-since / etc. to assert batching behavior
//   - Force-flush from outside the streaming loop

export const STREAM_FLUSH_INTERVAL_MS = 200
export const STREAM_FLUSH_BYTE_THRESHOLD = 1024
const SENTENCE_BOUNDARY = /[。？！.?!\n]\s*$/

export interface MessageStreamFlushOptions<T> {
  /**
   * Persist the latest enqueued value. Called with the most recent
   * envelope passed to enqueue() — partial intermediate values are
   * dropped on purpose (newest wins, batched flush). Errors propagate
   * to the caller of stop() / next enqueue().
   */
  flush: (value: T) => Promise<void>
  /**
   * Optional override for the time-window trigger (default 200ms).
   * Tests use a smaller value to keep specs fast.
   */
  intervalMs?: number
  /**
   * Optional override for the byte-accumulation trigger (default 1024).
   */
  byteThreshold?: number
  /**
   * Caller-provided byte-counter for the chunk being enqueued. The
   * default is "0" which means only triggers ① and ③ ever fire — that's
   * the correct fallback when the caller doesn't have a cheap way to
   * measure delta bytes. DialogView passes `chunkText.length` for utf-16
   * code units (close enough; the threshold is heuristic).
   */
  chunkBytesOf?: (value: T) => number
  /**
   * Caller-provided "sentence-boundary tail" extractor; receives the
   * latest enqueued value and returns the trailing text to match
   * SENTENCE_BOUNDARY against. Returning '' disables trigger ③.
   */
  boundaryTextOf?: (value: T) => string
}

export interface MessageStreamFlush<T> {
  /**
   * Enqueue a new value as the "current" snapshot. Schedules a flush
   * unless one of the immediate triggers (byte threshold / boundary)
   * fires, in which case it flushes synchronously after a microtask.
   */
  enqueue(value: T, opts?: { chunkBytes?: number }): void
  /**
   * Force a flush now if there's a pending value, then resolve. Safe to
   * call when nothing is pending (no-op).
   */
  flushNow(): Promise<void>
  /**
   * Final flush + tear down. Always await this before any post-stream
   * PUTs the caller wants applied serially (e.g. status='default').
   */
  stop(): Promise<void>
  /** Test hook: number of flush() invocations since construction. */
  flushCount(): number
  /** Test hook: cumulative chunk bytes counted since last flush. */
  bytesPending(): number
  /** Test hook: ms since last flush (or since construction if none). */
  msSinceLastFlush(): number
}

export function createMessageStreamFlush<T>(
  opts: MessageStreamFlushOptions<T>
): MessageStreamFlush<T> {
  const intervalMs = opts.intervalMs ?? STREAM_FLUSH_INTERVAL_MS
  const byteThreshold = opts.byteThreshold ?? STREAM_FLUSH_BYTE_THRESHOLD
  const chunkBytesOf = opts.chunkBytesOf ?? (() => 0)
  const boundaryTextOf = opts.boundaryTextOf ?? (() => '')

  let pending: T | null = null
  let bytesAccumulated = 0
  let lastFlushAt = Date.now()
  let timer: ReturnType<typeof setTimeout> | null = null
  let inflight: Promise<void> | null = null
  let flushes = 0
  let stopped = false

  function clearTimer(): void {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  function scheduleTimer(delay: number): void {
    clearTimer()
    timer = setTimeout(() => {
      timer = null
      void doFlush()
    }, Math.max(0, delay))
  }

  async function doFlush(): Promise<void> {
    if (pending === null) return
    if (inflight) {
      // Coalesce: while an inflight flush is in progress, the latest
      // pending value will be picked up on the next scheduled tick. We
      // don't fire a parallel PUT — that would race the realtime echo +
      // breach the "newest wins" contract.
      return inflight
    }
    const snapshot = pending
    pending = null
    bytesAccumulated = 0
    lastFlushAt = Date.now()
    flushes += 1
    inflight = (async () => {
      try {
        await opts.flush(snapshot)
      } finally {
        inflight = null
      }
    })()
    await inflight
    // If new chunks arrived during the inflight PUT, fire the next
    // window with whatever delay is left. This keeps the cadence stable
    // even when the network round-trip is longer than intervalMs.
    if (pending !== null && !stopped) {
      scheduleTimer(intervalMs)
    }
  }

  function enqueue(value: T, hint?: { chunkBytes?: number }): void {
    if (stopped) {
      // Stopped flushers ignore further enqueues — caller should have
      // awaited stop() first. We don't throw because that would cascade
      // to crashes inside the streaming loop on shutdown races.
      return
    }
    pending = value
    bytesAccumulated += hint?.chunkBytes ?? chunkBytesOf(value)

    // Trigger ② — byte threshold reached.
    if (bytesAccumulated >= byteThreshold) {
      void doFlush()
      return
    }
    // Trigger ③ — sentence boundary on the tail of the latest snapshot.
    const tail = boundaryTextOf(value)
    if (tail && SENTENCE_BOUNDARY.test(tail)) {
      void doFlush()
      return
    }
    // Trigger ① — schedule the next time-window flush. We compute the
    // remaining time so successive enqueues don't keep pushing the
    // deadline forward.
    const sinceLast = Date.now() - lastFlushAt
    const remaining = intervalMs - sinceLast
    if (timer === null) scheduleTimer(remaining)
  }

  async function flushNow(): Promise<void> {
    clearTimer()
    await doFlush()
  }

  async function stop(): Promise<void> {
    stopped = true
    clearTimer()
    await doFlush()
    // Wait for any in-flight flush triggered concurrently to drain.
    if (inflight) await inflight
  }

  function flushCount(): number { return flushes }
  function bytesPending(): number { return bytesAccumulated }
  function msSinceLastFlush(): number { return Date.now() - lastFlushAt }

  return { enqueue, flushNow, stop, flushCount, bytesPending, msSinceLastFlush }
}
