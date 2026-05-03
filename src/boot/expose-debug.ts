import { boot } from 'quasar/wrappers'
import { effectScope, ref } from 'vue'
import { db } from 'src/utils/db'
import { authSource } from 'src/data/auth'
import { repos } from 'src/data'
import * as blobClient from 'src/data/blob-client'
import { syncRef, type SyncRefOptions } from 'src/composables/sync-ref'
import {
  createMessageStreamFlush,
  STREAM_FLUSH_INTERVAL_MS,
  STREAM_FLUSH_BYTE_THRESHOLD
} from 'src/composables/message-stream-flush'

// Test-only harness: wrap syncRef in an effectScope so onScopeDispose works
// outside a Vue component. Returns hooks the spec can drive: source ref to
// simulate server pushes, val ref to read the user-visible state, set spy
// counter, and cleanup() to tear down.
function syncRefHarness<T>(initialSource: T, options?: SyncRefOptions<T>) {
  const scope = effectScope()
  const source = ref(initialSource)
  const setCalls: T[] = []
  let val: { value: T } | undefined
  scope.run(() => {
    val = syncRef<T>(
      () => source.value,
      v => { setCalls.push(JSON.parse(JSON.stringify(v))) },
      options
    )
  })
  return {
    get value() { return val!.value },
    set value(v: T) { val!.value = v },
    pushSource(v: T) { source.value = v },
    setCalls,
    setCallCount: () => setCalls.length,
    cleanup: () => scope.stop()
  }
}

// Stage 4 / 批次-4e — streaming-flush test hook. Exposes the factory
// itself plus the production tuning constants so specs can construct a
// flusher with shorter window/threshold for fast tests, and assert that
// production code uses the documented defaults.
const messageStreamFlushHook = {
  create: createMessageStreamFlush,
  intervalMs: STREAM_FLUSH_INTERVAL_MS,
  byteThreshold: STREAM_FLUSH_BYTE_THRESHOLD
}

declare global {
  interface Window {
    __db__?: typeof db
    __authSource__?: typeof authSource
    __repos__?: typeof repos
    __blobClient__?: typeof blobClient
    __syncRefHarness__?: typeof syncRefHarness
    __messageStreamFlush__?: typeof messageStreamFlushHook
    __exposeDebugReady__?: true
  }
}

export default boot(() => {
  if (String(process.env.EXPOSE_DB) !== 'true') return
  if (typeof window === 'undefined') return
  window.__db__ = db
  window.__authSource__ = authSource
  window.__repos__ = repos
  window.__blobClient__ = blobClient
  window.__syncRefHarness__ = syncRefHarness
  window.__messageStreamFlush__ = messageStreamFlushHook
  window.__exposeDebugReady__ = true
  console.warn('[expose-debug] window.__db__ / __authSource__ / __repos__ / __blobClient__ / __syncRefHarness__ / __messageStreamFlush__ exposed — test/dev only')
})
