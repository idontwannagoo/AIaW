/**
 * Stage 4.5 / Step 7 — frontend helper for the ImportJob multipart upload
 * protocol. Zero deps, ~150 lines of pragmatic code.
 *
 * What this module owns:
 *  - Creating an ImportJob (`POST /api/v1/import/jobs`).
 *  - Driving the multipart upload: per-part presigned URL fetch +
 *    `PUT <upload_url>` with `Blob.slice()` (streamed, never materializes the
 *    whole file in RAM).
 *  - Persisting upload cursor (set of completed part numbers) to localStorage
 *    so a tab close / refresh / network drop can resume next session without
 *    re-uploading already-acked parts. Server-side state is the source of
 *    truth — we only resume if `GET /api/v1/import/jobs/<id>` confirms the
 *    job is still in `uploading` status.
 *  - Completing the job (`POST .../complete`) and cancelling
 *    (`DELETE .../<id>`).
 *  - Wrapping `realtime.subscribe('import_jobs', ...)` so callers don't have
 *    to filter by jobId themselves.
 *  - Querying for the current active job (`GET .../jobs?status=active`) used
 *    by the application boot to re-display progress on a fresh tab / device.
 *
 * This module deliberately does NOT touch any Repository / Dexie cache — the
 * import flow lives entirely on the server. The realtime channel + REST GET
 * is sufficient for the UI to render progress; the data the import lands
 * comes through the normal per-table realtime channels (workspaces / dialogs
 * / messages …) once Phase B/C/D start writing.
 */
import { http, HttpError } from './http'
import { realtime, type RealtimeEvent } from './realtime'

// 5MB part size (S3 minimum non-final part is 5MB; 5MB matches the spec
// exactly so anything ≥ 5MB / N + 1 parts is valid). Keep aligned with the
// backend `BLOB_MAX_UPLOAD_BYTES` ceiling — we don't enforce it here, the
// server caps part bodies.
export const IMPORT_PART_SIZE_BYTES = 5 * 1024 * 1024
// Promise pool concurrency. 4 is chosen to saturate a typical residential
// uplink (each part is bandwidth-limited; more parallel parts ≠ more total
// throughput once the link is full) without overwhelming the backend's
// per-job semaphore.
export const IMPORT_PART_CONCURRENCY = 4
// Per-part retry budget. Indexed so the caller knows the total worst-case
// time per part: 1s + 2s + 4s = 7s of waiting + 4 attempts of network time.
export const IMPORT_PART_RETRIES = 3

export interface CreateImportJobResult {
  jobId: string
  multipartUploadId: string
  rawObjectKey: string
}

export interface ImportPart {
  partNumber: number
  etag: string
}

export interface ImportJobStatus {
  job_id: string
  status:
    | 'queued'
    | 'uploading'
    | 'assembling'
    | 'parsing'
    | 'phase_b'
    | 'phase_c'
    | 'phase_d'
    | 'done'
    | 'failed'
    | 'cancelled'
  multipart_upload_id?: string | null
  raw_object_key?: string | null
  total_bytes?: number | null
  processed_bytes: number
  total_rows?: number | null
  processed_rows: number
  total_blobs?: number | null
  processed_blobs: number
  error_message?: string | null
  dead_letter: unknown[]
  created_at?: string | null
  updated_at?: string | null
}

export interface UploadPartsOptions {
  partSize?: number
  concurrency?: number
  signal?: AbortSignal
  onProgress?: (uploadedBytes: number, totalBytes: number, completedParts: number, totalParts: number) => void
}

interface CreateJobRequest {
  file_size: number
  content_type: string
}

interface CreateJobResponse {
  job_id: string
  multipart_upload_id: string
  raw_object_key: string
}

interface PartUrlResponse {
  upload_url: string
  part_number: number
  expires_at: number
}

// ---- localStorage cursor ---------------------------------------------------
//
// Key shape: `import.<jobId>.parts` → JSON Array<{partNumber:number,
// etag:string}>. We keep the etag too because the `complete` endpoint
// requires the full list, and a tab that crashed mid-flight has no other
// way to recover it (server only knows the etag was returned to the client).

const CURSOR_KEY_PREFIX = 'import.'
const CURSOR_KEY_SUFFIX = '.parts'

function cursorKey(jobId: string): string {
  return `${CURSOR_KEY_PREFIX}${jobId}${CURSOR_KEY_SUFFIX}`
}

function loadCursor(jobId: string): ImportPart[] {
  try {
    const raw = localStorage.getItem(cursorKey(jobId))
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Defensive: filter to well-shaped entries only. A localStorage entry
    // written by an old client could legitimately be malformed.
    return parsed.filter(
      (p): p is ImportPart =>
        p && typeof p.partNumber === 'number' && typeof p.etag === 'string'
    )
  } catch {
    return []
  }
}

function saveCursor(jobId: string, parts: ImportPart[]): void {
  try {
    localStorage.setItem(cursorKey(jobId), JSON.stringify(parts))
  } catch {
    // Out of storage / disabled — the worst case is the user re-uploads on
    // resume. Don't crash the upload over a cache write.
  }
}

function clearCursor(jobId: string): void {
  try { localStorage.removeItem(cursorKey(jobId)) } catch { /* ignore */ }
}

// ---- public API ------------------------------------------------------------

export async function createImportJob(file: File): Promise<CreateImportJobResult> {
  const body: CreateJobRequest = {
    file_size: file.size,
    content_type: file.type || 'application/json'
  }
  const res = await http.post<CreateJobResponse>('/api/v1/import/jobs', body)
  return {
    jobId: res.job_id,
    multipartUploadId: res.multipart_upload_id,
    rawObjectKey: res.raw_object_key
  }
}

export async function uploadParts(
  file: File,
  jobId: string,
  opts: UploadPartsOptions = {}
): Promise<ImportPart[]> {
  const partSize = opts.partSize ?? IMPORT_PART_SIZE_BYTES
  const concurrency = opts.concurrency ?? IMPORT_PART_CONCURRENCY
  const signal = opts.signal
  const onProgress = opts.onProgress

  if (file.size <= 0) {
    throw new Error('uploadParts: empty file')
  }
  const totalParts = Math.ceil(file.size / partSize)

  // Resume path: ask the server whether the job is still uploading. If it's
  // moved past uploading (assembling / parsing / done / cancelled / failed),
  // the cached cursor is stale and we must not blindly re-PUT — the parts
  // would 409 anyway, and we'd waste bytes. Treat anything non-uploading as
  // "this job is no longer accepting parts; caller should reconcile".
  const cursor = loadCursor(jobId)
  if (cursor.length > 0) {
    let serverStatus: ImportJobStatus | null = null
    try {
      serverStatus = await getImportJob(jobId)
    } catch (e) {
      // 404 = job gone server-side, drop cursor and start fresh upload from
      // part 1 (caller most likely has a brand-new jobId already though).
      if (e instanceof HttpError && e.status === 404) {
        clearCursor(jobId)
      } else {
        throw e
      }
    }
    if (serverStatus && serverStatus.status !== 'uploading') {
      // Already past uploading — the assembled multipart is owned by the
      // server now. Caller should treat this as "upload was completed by a
      // sibling tab"; we hand back the cursor so they can fast-forward.
      return [...cursor].sort((a, b) => a.partNumber - b.partNumber)
    }
  }

  const completed = new Map<number, string>()
  for (const p of cursor) completed.set(p.partNumber, p.etag)
  const totalBytes = file.size
  let uploadedBytes = computeUploadedBytes(completed, partSize, totalBytes, totalParts)

  // Build the list of part numbers still needing upload (skip cursor hits).
  const pending: number[] = []
  for (let i = 1; i <= totalParts; i++) {
    if (!completed.has(i)) pending.push(i)
  }

  // Initial progress tick so the UI shows "resumed at X%" not "0%".
  onProgress?.(uploadedBytes, totalBytes, completed.size, totalParts)

  if (pending.length === 0) {
    return [...completed.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([partNumber, etag]) => ({ partNumber, etag }))
  }

  // Promise-pool worker queue. Each worker drains the `pending` array atom-
  // ically (Array.prototype.shift is cheap for small arrays). We deliberately
  // avoid `Promise.all(pending.map(...))` because that fires every fetch at
  // once — a 200MB / 5MB = 40-part file would spawn 40 concurrent requests,
  // tanking the backend and the user's link.
  const errors: Error[] = []
  const queue = [...pending]
  const workers: Promise<void>[] = []
  const workerCount = Math.min(concurrency, pending.length)

  for (let w = 0; w < workerCount; w++) {
    workers.push((async () => {
      while (queue.length > 0) {
        if (signal?.aborted) {
          throw signal.reason ?? new DOMException('aborted', 'AbortError')
        }
        const partNumber = queue.shift()
        if (partNumber === undefined) return
        try {
          const etag = await uploadOnePart(file, jobId, partNumber, partSize, signal)
          const partBytes = bytesForPart(partNumber, partSize, totalBytes, totalParts)
          completed.set(partNumber, etag)
          uploadedBytes += partBytes
          // Persist cursor right after each successful part — even if the
          // tab crashes before completion we want the next session to skip
          // this part. Storage write is sync but fast enough not to block.
          saveCursor(jobId, [...completed.entries()].map(([n, e]) => ({ partNumber: n, etag: e })))
          onProgress?.(uploadedBytes, totalBytes, completed.size, totalParts)
        } catch (e) {
          errors.push(e instanceof Error ? e : new Error(String(e)))
          // Fail fast: tear down remaining work by emptying the queue. Other
          // workers will see queue.length === 0 and exit.
          queue.length = 0
          throw e
        }
      }
    })())
  }

  // Wait for all workers; surface the first error if any occurred.
  const settled = await Promise.allSettled(workers)
  const firstReject = settled.find(s => s.status === 'rejected')
  if (firstReject && firstReject.status === 'rejected') {
    throw firstReject.reason
  }
  if (errors.length > 0) {
    throw errors[0]
  }

  return [...completed.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([partNumber, etag]) => ({ partNumber, etag }))
}

export async function completeImport(jobId: string, parts: ImportPart[]): Promise<ImportJobStatus> {
  const body = {
    parts: parts.map(p => ({ part_number: p.partNumber, etag: p.etag }))
  }
  const res = await http.post<ImportJobStatus>(`/api/v1/import/jobs/${jobId}/complete`, body)
  // Once the server has assembled the upload, the per-part bytes / cursor
  // are not recoverable nor needed — drop the localStorage entry to keep
  // the namespace clean.
  clearCursor(jobId)
  return res
}

export async function cancelImport(jobId: string): Promise<void> {
  try {
    await http.delete<void>(`/api/v1/import/jobs/${jobId}`)
  } finally {
    // Even if the DELETE 404'd (job already gone), drop the cursor — there
    // is nothing for the client to do with it.
    clearCursor(jobId)
  }
}

export async function getImportJob(jobId: string): Promise<ImportJobStatus> {
  return await http.get<ImportJobStatus>(`/api/v1/import/jobs/${jobId}`)
}

export async function getActiveImportJob(): Promise<ImportJobStatus | null> {
  const res = await http.get<ImportJobStatus[]>('/api/v1/import/jobs', {
    query: { status: 'active' }
  })
  return res.length > 0 ? res[0] : null
}

/**
 * Subscribe to status changes for a single ImportJob. Wraps the realtime
 * channel and filters by jobId so callers don't have to.
 *
 * The realtime envelope for `import_jobs` follows the standard server-routed
 * table contract — `e.row` is `{id, version, updated_at, deleted, data}`
 * where `data` is the status snapshot. WS `data` uses `{id: <jobId>, ...}`
 * (matching the row primary key) while REST `GET /api/v1/import/jobs/<id>`
 * returns `{job_id: <jobId>, ...}` — the field name differs by historical
 * accident in `_envelope()` vs `_to_status()`. We normalize WS payloads to
 * `job_id`-keyed shape here so callers can use a single `ImportJobStatus`
 * interface across REST + WS.
 */
export function subscribeImportStatus(
  jobId: string,
  cb: (status: ImportJobStatus) => void
): () => void {
  interface ImportJobWireData {
    id?: string
    job_id?: string
    status: ImportJobStatus['status']
    multipart_upload_id?: string | null
    raw_object_key?: string | null
    total_bytes?: number | null
    processed_bytes: number
    total_rows?: number | null
    processed_rows: number
    total_blobs?: number | null
    processed_blobs: number
    error_message?: string | null
    dead_letter: unknown[]
    created_at?: string | null
    updated_at?: string | null
  }
  interface ImportJobEnvelope {
    id: string
    version: number
    updated_at: string
    deleted: boolean
    data: ImportJobWireData
  }
  return realtime.subscribe<ImportJobEnvelope>('import_jobs', (e: RealtimeEvent<ImportJobEnvelope>) => {
    if (e.id !== jobId) return
    if (e.op === 'delete') return
    if (!e.row || !e.row.data) return
    const wd = e.row.data
    const normalized: ImportJobStatus = {
      job_id: wd.job_id ?? wd.id ?? e.row.id ?? jobId,
      status: wd.status,
      multipart_upload_id: wd.multipart_upload_id ?? null,
      raw_object_key: wd.raw_object_key ?? null,
      total_bytes: wd.total_bytes ?? null,
      processed_bytes: wd.processed_bytes,
      total_rows: wd.total_rows ?? null,
      processed_rows: wd.processed_rows,
      total_blobs: wd.total_blobs ?? null,
      processed_blobs: wd.processed_blobs,
      error_message: wd.error_message ?? null,
      dead_letter: wd.dead_letter ?? [],
      created_at: wd.created_at ?? null,
      updated_at: wd.updated_at ?? null
    }
    try {
      cb(normalized)
    } catch (err) {
      // Don't let a bad subscriber crash the realtime channel.
      console.warn('[import-client] subscribeImportStatus callback threw', err)
    }
  })
}

// ---- internals -------------------------------------------------------------

function bytesForPart(
  partNumber: number,
  partSize: number,
  totalBytes: number,
  totalParts: number
): number {
  // Last part is short. All others are exactly partSize.
  if (partNumber < totalParts) return partSize
  // partNumber === totalParts: trailing bytes.
  return totalBytes - (totalParts - 1) * partSize
}

function computeUploadedBytes(
  completed: Map<number, string>,
  partSize: number,
  totalBytes: number,
  totalParts: number
): number {
  let sum = 0
  for (const partNumber of completed.keys()) {
    sum += bytesForPart(partNumber, partSize, totalBytes, totalParts)
  }
  return sum
}

async function uploadOnePart(
  file: File,
  jobId: string,
  partNumber: number,
  partSize: number,
  signal: AbortSignal | undefined
): Promise<string> {
  let lastErr: unknown
  // 4 attempts total: initial + 3 retries with exponential backoff.
  for (let attempt = 0; attempt <= IMPORT_PART_RETRIES; attempt++) {
    if (signal?.aborted) {
      throw signal.reason ?? new DOMException('aborted', 'AbortError')
    }
    if (attempt > 0) {
      const delayMs = Math.pow(2, attempt - 1) * 1000 // 1s / 2s / 4s
      await sleep(delayMs, signal)
    }
    try {
      // Mint a fresh presigned URL each attempt. URLs have a 1h TTL so within
      // the retry window this is cheap insurance against signature staleness
      // (e.g. retry happens at minute 59 of a long-running upload).
      const partUrl = await http.post<PartUrlResponse>(
        `/api/v1/import/jobs/${jobId}/parts/${partNumber}`,
        undefined,
        { signal }
      )
      // Slice the file lazily — Blob.slice returns a view, not a copy. fetch
      // will stream from it without materializing into RAM.
      const start = (partNumber - 1) * partSize
      const end = Math.min(start + partSize, file.size)
      const slice = file.slice(start, end)
      const res = await fetch(partUrl.upload_url, {
        method: 'PUT',
        body: slice,
        signal
      })
      if (!res.ok) {
        throw new Error(`part ${partNumber} PUT failed: ${res.status} ${res.statusText}`)
      }
      // LocalFs returns JSON `{etag, part_number, size}`. S3 returns 200 with
      // the etag in the `ETag` header (and no body). Support both so the same
      // client code path works against either backend.
      const etagHeader = res.headers.get('ETag') || res.headers.get('etag')
      let etag: string | null = null
      if (res.headers.get('content-type')?.includes('json')) {
        try {
          const body = await res.json() as { etag?: string }
          if (body && typeof body.etag === 'string') etag = body.etag
        } catch { /* fall through to header */ }
      }
      if (!etag && etagHeader) {
        // S3 wraps etag in quotes — strip them, the server's complete handler
        // doesn't care but consistency makes test expectations cleaner.
        etag = etagHeader.replace(/^"|"$/g, '')
      }
      if (!etag) {
        throw new Error(`part ${partNumber} PUT response missing etag`)
      }
      return etag
    } catch (e) {
      // AbortError is terminal — don't retry user cancellation.
      if (e instanceof DOMException && e.name === 'AbortError') throw e
      lastErr = e
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr))
}

function sleep(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException('aborted', 'AbortError'))
      return
    }
    const t = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(t)
      reject(signal?.reason ?? new DOMException('aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}
