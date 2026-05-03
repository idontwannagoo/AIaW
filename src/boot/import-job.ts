/**
 * Stage 4.5 / Step 7 — boot file for the global ImportJob state.
 *
 * Just touches the `useActiveImportJob()` getter at app startup so the
 * underlying watcher on `authSource.user` is wired before any UI mounts.
 * Without this boot file, the first component that calls `useActiveImportJob`
 * would only start listening from that point — meaning a fresh-tab login
 * wouldn't see the active job until the user navigates to a page that uses it.
 */
import { boot } from 'quasar/wrappers'
import { useActiveImportJob } from 'src/composables/import-job'
import { BackendApiBaseURL } from 'src/utils/config'

export default boot(() => {
  if (!BackendApiBaseURL) return
  // Side effect only — discard the returned ref. The composable wires its
  // watcher on first call and is idempotent on subsequent calls.
  useActiveImportJob()
})
