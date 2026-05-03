/**
 * Stage 4.5 / Step 8 — `GET /api/v1/bootstrap` client wrapper.
 *
 * Single round-trip first-screen hydration helper for the router guard
 * (`router/index.ts`). Returns the parsed response on success, or `null`
 * on timeout / network error / server error so callers can fall back to
 * the legacy progressive-load path without a try/catch dance.
 *
 * Why null-on-failure instead of throw: the only caller is the router
 * guard, and it must not block route resolution on a backend hiccup. A
 * null tells the guard to render with whatever IDB has cached and let
 * the per-table `pull()` paths backfill in the background. Errors are
 * logged once at warn level so dev console makes the fallback visible.
 *
 * Wire shape mirrors `src-backend/data/routers/bootstrap.py` — see that
 * module's docstring for the canonical contract. The applier
 * (`bootstrap-apply.ts`) is the single consumer that knows the field
 * shape; this module is intentionally schema-thin.
 */
import { http, HttpError } from './http'

const DEFAULT_TIMEOUT_MS = 2000

// Wire envelope shapes — mirror the backend `_envelope_id` / `_envelope_kv`
// helpers. We type them loosely here (`unknown` for `data`) and let the
// per-table apply paths (which already own decode logic) cast as needed.
export interface IdEnvelope {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: unknown
}

export interface KvEnvelope {
  key: string
  version: number
  updated_at: string
  deleted: boolean
  data: unknown
}

export interface BootstrapResponse {
  schema_version: number
  workspaces: IdEnvelope[]
  dialogs: IdEnvelope[]
  providers: IdEnvelope[]
  assistants: IdEnvelope[]
  installed_plugins: KvEnvelope[]
  reactives: KvEnvelope[]
  avatar_images: IdEnvelope[]
  messages_recent: IdEnvelope[]
}

export interface FetchBootstrapOptions {
  timeoutMs?: number
}

/**
 * Fetch the bootstrap response with a hard timeout. Returns null on
 * timeout, network failure, or non-2xx — never throws.
 */
export async function fetchBootstrap(
  options: FetchBootstrapOptions = {}
): Promise<BootstrapResponse | null> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  // AbortSignal.timeout is the platform primitive (Chrome 103+, FF 100+,
  // Safari 16+ — well above our supported floor). Falling back to
  // setTimeout + AbortController would work but adds plumbing for a code
  // path the supported browsers all hit.
  const signal = AbortSignal.timeout(timeoutMs)
  try {
    const body = await http.get<BootstrapResponse>('/api/v1/bootstrap', {
      signal
    })
    return body
  } catch (e) {
    // Bucket the error categories so the dev console flags the fallback
    // path with the actual cause:
    //   - DOMException name='TimeoutError' → AbortSignal.timeout fired
    //   - DOMException name='AbortError'   → caller cancelled
    //   - HttpError                         → backend 4xx/5xx
    //   - else                              → network / fetch internal
    if (e instanceof DOMException && e.name === 'TimeoutError') {
      console.warn(
        `[bootstrap] fetch timed out after ${timeoutMs}ms; ` +
          'falling back to progressive load'
      )
    } else if (e instanceof HttpError) {
      console.warn(
        `[bootstrap] fetch failed with HTTP ${e.status}; ` +
          'falling back to progressive load',
        e
      )
    } else if (e instanceof DOMException && e.name === 'AbortError') {
      // Caller cancelled — silent (no fallback log needed).
    } else {
      console.warn(
        '[bootstrap] fetch failed (network); falling back to progressive load',
        e
      )
    }
    return null
  }
}
