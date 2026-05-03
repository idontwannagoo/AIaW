import { Ref, ref, watch, WatchSource, onScopeDispose } from 'vue'

export interface SyncRefOptions<T> {
  sourceDeep?: boolean
  valueDeep?: boolean
  initialValue?: T
  /**
   * Debounce the `set` callback by N ms — coalesces a burst of edits into a
   * single write. Critical for form inputs where every keystroke would
   * otherwise trigger a server round-trip.
   *
   * Default 200ms — fast enough that a stop-typing pause publishes within
   * one frame of the user noticing, slow enough that 5+ keystrokes/sec
   * collapse to ~1 PUT.
   *
   * Pass 0 to write immediately (e.g. boolean toggles).
   */
  debounceMs?: number
  /**
   * After a local edit fires, ignore source pushes for N ms. Prevents echo
   * loops where the server's response or WS broadcast lands mid-typing and
   * overwrites the input with a stale (round-trip-lagged) value.
   *
   * Default 1500ms — covers typical 200-500ms server round-trip with
   * comfortable margin while still allowing genuine cross-device updates
   * to arrive within ~1.5s of the local user finishing a thought.
   *
   * Pass 0 to disable suppression (read-then-write semantics).
   */
  suppressSourceWhileEditingMs?: number
}

export function syncRef<T>(
  source: WatchSource<T>,
  set: (value: T) => void,
  options?: SyncRefOptions<T>
) {
  const val = ref(options?.initialValue) as Ref<T>
  const debounceMs = options?.debounceMs ?? 200
  const suppressMs = options?.suppressSourceWhileEditingMs ?? 1500

  // True for the one upcoming val-watch tick we're about to trigger by
  // pushing source → val. Skips the round-trip back into `set`.
  let absorbingSourceEcho = false

  // Wall clock of the last *local* edit (as opposed to a source push). Used
  // to decide whether an arriving source value is likely an echo of a write
  // we just sent (suppress) or a genuine remote update (apply).
  let lastLocalEditAt = 0

  let pendingFlush: ReturnType<typeof setTimeout> | null = null
  const cancelPending = () => {
    if (pendingFlush) {
      clearTimeout(pendingFlush)
      pendingFlush = null
    }
  }

  watch(val, newVal => {
    if (absorbingSourceEcho) {
      absorbingSourceEcho = false
      return
    }
    // Stamp the edit moment now, not at flush — the suppression window must
    // start from the first keystroke so a still-in-flight earlier-PUT echo
    // can't slip through during the debounce wait.
    lastLocalEditAt = Date.now()

    if (debounceMs <= 0) {
      set(newVal)
      return
    }
    cancelPending()
    pendingFlush = setTimeout(() => {
      pendingFlush = null
      set(newVal)
    }, debounceMs)
  }, { deep: options?.valueDeep })

  watch(source, newVal => {
    // If the user is mid-edit, the arriving source value is overwhelmingly
    // likely to be an echo of one of our own recent writes (or even a stale
    // earlier write that round-tripped slowly). Drop it; the pending
    // debounce flush will publish the user's latest value anyway.
    if (
      suppressMs > 0 &&
      lastLocalEditAt > 0 &&
      Date.now() - lastLocalEditAt < suppressMs
    ) {
      return
    }
    absorbingSourceEcho = true
    val.value = newVal
  }, { immediate: true, deep: options?.sourceDeep })

  // Belt-and-suspenders: if the component scope tears down with a write
  // pending, flush synchronously so we don't drop the last keystroke.
  onScopeDispose(() => {
    if (pendingFlush) {
      clearTimeout(pendingFlush)
      pendingFlush = null
      set(val.value)
    }
  })

  return val
}
