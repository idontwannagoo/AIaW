/**
 * Stage 4 硬前置 2 — frontend helper for the `/api/v1/blobs` endpoint.
 *
 * What this module owns:
 *  - The 64KB inline-vs-ref decision (`BLOB_INLINE_MAX_BYTES`).
 *  - The wire-format envelope `BlobRef = {type:'ref', url, sha256, size, content_type}`
 *    that Stage 4 主体批次 (artifacts / messages) will store inside row data
 *    in lieu of the actual bytes once a blob crosses the threshold.
 *  - Multipart upload via `fetch` + `FormData` (single-shot for now; Stage 4.5
 *    ImportJob multipart 5-part protocol will live in `import-client.ts`).
 *  - Caching presigned URL fetches in IndexedDB is *not* this module's job —
 *    that's row-cache territory and is wired per-table when artifacts /
 *    messages land. This file is the tower-of-bytes-only, kept narrow.
 *
 * The helper does NOT touch any Repository / Dexie table itself. Callers
 * decide where to put the resulting envelope (inline `Blob` for inline mode,
 * or `BlobRef` for ref mode).
 */

// 64KB. Below = inline base64 in row, above = ref envelope. Hard-aligned
// with backend `BLOB_INLINE_MAX_BYTES`; if you change one change both. We
// don't read the env here because that requires a build-time inline through
// quasar.config.js + `String(process.env.…)` coercion every call site, and
// the threshold is part of the wire format anyway — moving it without a
// coordinated backend change breaks dedup/import.
export const BLOB_INLINE_MAX_BYTES = 64 * 1024

export interface BlobRef {
  type: 'ref'
  url: string
  sha256: string
  size: number
  content_type: string
}

export interface InlineBlob {
  type: 'inline'
  data: string // base64
  content_type: string
  size: number
}

export type AttachmentEnvelope = BlobRef | InlineBlob

interface UploadBlobResponse {
  sha256: string
  size: number
  content_type: string
  url: string
  ref: BlobRef
  deduped: boolean
}

function bytesOf(input: Blob | ArrayBuffer | Uint8Array): number {
  if (input instanceof Blob) return input.size
  if (input instanceof ArrayBuffer) return input.byteLength
  return input.byteLength
}

function asBlob(input: Blob | ArrayBuffer | Uint8Array, contentType: string): Blob {
  if (input instanceof Blob) return input
  // Wrap ArrayBuffer / Uint8Array in a Blob without copying when possible.
  // Modern browsers don't copy BufferSource into Blob — they hold a reference.
  return new Blob([input], { type: contentType || 'application/octet-stream' })
}

/**
 * Upload a blob to the backend. Returns the ref envelope (sha256 + signed URL)
 * the caller embeds in row data. The endpoint dedupes by sha256 server-side,
 * so calling this twice with identical bytes is cheap.
 */
export async function putBlob(
  input: Blob | ArrayBuffer | Uint8Array,
  contentType?: string
): Promise<BlobRef> {
  const blob = asBlob(input, contentType ?? 'application/octet-stream')
  const fd = new FormData()
  // Filename is irrelevant — the server ignores it. Pin it to a stable token
  // so DevTools / server logs aren't littered with "blob".
  fd.append('file', blob, 'aiaw-blob')
  // We bypass http.ts's JSON Content-Type defaulting by using fetch directly:
  // FormData sets multipart boundary headers itself, and JSON serialization
  // would corrupt binary data. Auth header is still pulled from authSource.
  const res = await fetchWithAuth('/api/v1/blobs', { method: 'POST', body: fd })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`putBlob failed: ${res.status} ${text}`)
  }
  const body = await res.json() as UploadBlobResponse
  return body.ref
}

/**
 * Download blob bytes from a ref. The presigned URL is the credential, no
 * Authorization header needed. Returns a `Blob` with the recorded content_type
 * so callers can pipe it into `<img>` / `URL.createObjectURL` / FileSaver
 * without further plumbing.
 */
export async function fetchBlob(ref: BlobRef): Promise<Blob> {
  const res = await fetch(ref.url)
  if (!res.ok) {
    throw new Error(`fetchBlob failed: ${res.status} ${ref.sha256}`)
  }
  const buf = await res.arrayBuffer()
  return new Blob([buf], { type: ref.content_type })
}

/**
 * Decide inline vs ref based on size. Below the threshold the bytes are
 * encoded as base64 and returned in an `InlineBlob` envelope (no network).
 * At/above the threshold the helper uploads to backend and returns a ref.
 *
 * The 64KB threshold is *inclusive of inline*: 65536 bytes still goes to
 * blob store. This matches the Stage 4 plan: "≥ 64KB 走 multipart".
 */
export async function serializeAttachment(
  input: Blob | ArrayBuffer | Uint8Array,
  contentType?: string
): Promise<AttachmentEnvelope> {
  const size = bytesOf(input)
  const ct = contentType ||
    (input instanceof Blob ? input.type : '') ||
    'application/octet-stream'
  if (size < BLOB_INLINE_MAX_BYTES) {
    return {
      type: 'inline',
      data: await toBase64(input),
      content_type: ct,
      size
    }
  }
  return await putBlob(input, ct)
}

/**
 * Inverse of `serializeAttachment`: given an envelope (inline or ref), produce
 * a `Blob` ready for UI consumption. Inline path is local-only; ref path hits
 * the network — caller decides whether to cache.
 */
export async function materializeAttachment(env: AttachmentEnvelope): Promise<Blob> {
  if (env.type === 'inline') {
    const bin = atob(env.data)
    const buf = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i)
    return new Blob([buf], { type: env.content_type })
  }
  return await fetchBlob(env)
}

// ---- internals -------------------------------------------------------------

async function fetchWithAuth(path: string, init: Parameters<typeof fetch>[1]): Promise<Response> {
  // Lazy import to avoid a circular dep with auth.ts which itself imports
  // http.ts; we only need the token getter here. Stable module structure
  // wins over a tiny perf cost.
  const { authSource } = await import('./auth')
  const { BackendApiBaseURL } = await import('src/utils/config')
  if (!BackendApiBaseURL) {
    throw new Error('blob-client called without BACKEND_DATA_API_URL configured')
  }
  const token = authSource.currentToken()
  const headers = new Headers(init.headers)
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  // First attempt
  let res = await fetch(`${BackendApiBaseURL}${path}`, { ...init, headers })
  if (res.status === 401) {
    const refreshed = await authSource.tryRefresh()
    if (refreshed) {
      const token2 = authSource.currentToken()
      const h2 = new Headers(init.headers)
      if (token2) h2.set('Authorization', `Bearer ${token2}`)
      res = await fetch(`${BackendApiBaseURL}${path}`, { ...init, headers: h2 })
    }
  }
  return res
}

async function toBase64(input: Blob | ArrayBuffer | Uint8Array): Promise<string> {
  const buf = input instanceof Blob
    ? new Uint8Array(await input.arrayBuffer())
    : input instanceof ArrayBuffer
      ? new Uint8Array(input)
      : input
  // btoa needs a binary string; chunk to keep stack happy on big buffers.
  let out = ''
  const chunk = 0x8000
  for (let i = 0; i < buf.length; i += chunk) {
    out += String.fromCharCode(...buf.subarray(i, i + chunk))
  }
  return btoa(out)
}
