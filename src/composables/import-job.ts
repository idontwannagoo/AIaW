/**
 * Stage 4.5 / Step 7 — global ImportJob state composable.
 *
 * One singleton subscription per logged-in user that:
 *   1. on user-login transition (`authSource.user` flipping from null → set),
 *      hits `GET /api/v1/import/jobs?status=active` once
 *   2. if an active job exists, subscribes to realtime events for it
 *   3. exposes the live status as a reactive `Ref<ImportJobStatus | null>`
 *      that any component (AccountPage banner, dialog launchers, etc.) can
 *      read without each one re-doing the GET / subscribe dance
 *
 * Why a composable + module-level state vs a Pinia store: this is a single
 * cross-cutting state value with no nested domain. A store would add ceremony
 * without buying anything; a composable returning a `Ref` is enough.
 *
 * The composable is safe to call from any component — every call returns the
 * same shared `Ref`. The boot file (`boot/import-job.ts`) ensures the watcher
 * is wired exactly once at app startup.
 */
import { ref, watch, type Ref } from 'vue'
import { authSource } from 'src/data/auth'
import { BackendApiBaseURL } from 'src/utils/config'
import {
  getActiveImportJob,
  subscribeImportStatus,
  type ImportJobStatus
} from 'src/data/import-client'

const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled'])

const activeJob = ref<ImportJobStatus | null>(null)
let unsubscribe: (() => void) | null = null
let bootstrapped = false

function clearSubscription() {
  if (unsubscribe) {
    try { unsubscribe() } catch { /* ignore */ }
    unsubscribe = null
  }
}

function attachSubscription(jobId: string) {
  clearSubscription()
  unsubscribe = subscribeImportStatus(jobId, (status) => {
    activeJob.value = status
    // Drop the subscription as soon as the job hits a terminal state — we
    // keep the final snapshot visible for one tick (so the UI can show
    // "done!" / "failed: …") then a subsequent boot will refetch and clear
    // it. Components that want to dismiss the card explicitly can call
    // `clearActiveImportJob()`.
    if (TERMINAL_STATUSES.has(status.status)) {
      clearSubscription()
    }
  })
}

async function refreshActiveJob(): Promise<void> {
  if (!BackendApiBaseURL) {
    activeJob.value = null
    return
  }
  if (!authSource.enabled) {
    activeJob.value = null
    return
  }
  try {
    const job = await getActiveImportJob()
    activeJob.value = job
    if (job && !TERMINAL_STATUSES.has(job.status)) {
      attachSubscription(job.job_id)
    } else {
      clearSubscription()
    }
  } catch (e) {
    // GET ?status=active failures are non-fatal — we just won't show the
    // banner. Silent fallback is intentional: a 401 / network blip should
    // not put a "your import failed" banner on screen.
    console.warn('[import-job] failed to fetch active job', e)
    activeJob.value = null
    clearSubscription()
  }
}

function setupOnce() {
  if (bootstrapped) return
  bootstrapped = true
  if (!BackendApiBaseURL || !authSource.enabled) return
  // Re-poll on login transitions (null → user, or userA → userB). On logout
  // (user → null) drop everything — the realtime channel will close itself
  // when auth disappears, but the local state needs an explicit clear.
  watch(
    () => authSource.user.value?.userId ?? null,
    (next, prev) => {
      if (next === prev) return
      if (!next) {
        activeJob.value = null
        clearSubscription()
        return
      }
      void refreshActiveJob()
    },
    { immediate: true }
  )
}

/**
 * Returns the shared reactive ref for the current user's active import job
 * (or null if none). Idempotent: calling this from N components yields the
 * same ref + the same single subscription.
 */
export function useActiveImportJob(): Ref<ImportJobStatus | null> {
  setupOnce()
  return activeJob
}

/**
 * Dismiss the current active job from the global state — used e.g. when the
 * user clicks "X" on a terminal-state banner. Doesn't touch the server.
 */
export function clearActiveImportJob(): void {
  activeJob.value = null
  clearSubscription()
}

/**
 * Force a refetch of the active job. Called after the dialog completes the
 * upload (so the banner picks up the just-created job without waiting for
 * the next reactive tick) or after cancel.
 */
export async function reloadActiveImportJob(): Promise<void> {
  setupOnce()
  await refreshActiveJob()
}
