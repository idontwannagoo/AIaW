import {
  createMemoryHistory,
  createRouter,
  createWebHashHistory,
  createWebHistory
} from 'vue-router'
import routes from './routes'
import { authSource } from 'src/data/auth'
import { BackendApiBaseURL } from 'src/utils/config'
import { fetchBootstrap } from 'src/data/bootstrap-client'
import { applyBootstrap } from 'src/data/bootstrap-apply'

/*
 * If not building with SSR mode, you can
 * directly export the Router instantiation;
 *
 * The function below can be async too; either use
 * async/await or return a Promise which resolves
 * with the Router instance.
 */

const createHistory = process.env.SERVER
  ? createMemoryHistory
  : (process.env.VUE_ROUTER_MODE === 'history' ? createWebHistory : createWebHashHistory)

const router = createRouter({
  scrollBehavior: () => ({ left: 0, top: 0 }),
  routes,

  // Leave this as is and make changes in quasar.conf.js instead!
  // quasar.conf.js -> build -> vueRouterMode
  // quasar.conf.js -> build -> publicPath
  history: createHistory(process.env.VUE_ROUTER_BASE)
})

// Stage 4.5 / Step 8 — first-screen bootstrap guard.
//
// Runs once per browser session: on the first authenticated navigation
// after the router resolves a route, fetch `GET /api/v1/bootstrap` and
// apply it to IDB before letting the navigation through. This eliminates
// the "blank workspaces list" flash on a cold-cache login (workspaces
// are visible within ~200ms instead of waiting for per-table pulls to
// fan out from each store mount).
//
// Failure / timeout (2s) → fall through. The legacy progressive load
// (each `<table>.server.ts::pull()` running on first observe / list
// call) still works; we just lose the single-shot speedup. The
// `bootstrap-failed` flag below is a hint to MainLayout to surface a
// banner ("部分数据稍后加载").
//
// Once-per-session: localStorage is wrong because we want the bootstrap
// to re-run on every fresh browser tab. sessionStorage is right —
// scoped to the tab, cleared on close. The flag carries a status string
// so a fallback in tab A doesn't trick tab B (different session) into
// skipping its own bootstrap.
//
// Skip conditions:
//   - BACKEND_DATA_API_URL empty (Stage 0 / pure Dexie deploy) — no
//     endpoint to call, the legacy path is the only path.
//   - User not logged in — the guard would 401 instantly. We let the
//     route through; on subsequent login the next navigation re-tries.
//   - Already attempted this session (sessionStorage flag set) — the
//     applied IDB rows are still good; per-table realtime / scoped pulls
//     are now in charge of incremental updates.

const SESSION_FLAG_KEY = 'aiaw.bootstrap.attempted'
const FALLBACK_FLAG_KEY = 'aiaw.bootstrap.fallback'

function readSessionFlag(key: string): string | null {
  try { return sessionStorage.getItem(key) } catch { return null }
}

function writeSessionFlag(key: string, value: string): void {
  try { sessionStorage.setItem(key, value) } catch { /* ignore */ }
}

let bootstrapInflight: Promise<void> | null = null

async function runBootstrapOnce(): Promise<void> {
  if (bootstrapInflight) return bootstrapInflight
  if (readSessionFlag(SESSION_FLAG_KEY)) return
  bootstrapInflight = (async () => {
    try {
      const response = await fetchBootstrap()
      if (!response) {
        // Mark fallback so the layout can show the banner. We still set
        // the attempted flag to avoid hammering the endpoint on every
        // navigation; the legacy progressive load takes over.
        writeSessionFlag(FALLBACK_FLAG_KEY, '1')
      } else {
        const counts = await applyBootstrap(response)
        // One-line summary for the dev console — useful when debugging
        // the "first paint felt slow" complaint.
        console.info('[bootstrap] applied', counts)
      }
    } finally {
      writeSessionFlag(SESSION_FLAG_KEY, '1')
      bootstrapInflight = null
    }
  })()
  return bootstrapInflight
}

router.beforeEach(async (_to, _from, next) => {
  // Fast path: bootstrap not applicable for this build / state.
  if (!BackendApiBaseURL) return next()
  if (!authSource.enabled) return next()
  if (readSessionFlag(SESSION_FLAG_KEY)) return next()
  // No user yet (cold tab before login completes) — let the route render;
  // the next navigation after login will trigger bootstrap.
  const user = authSource.user.value
  if (!user) return next()
  await runBootstrapOnce()
  next()
})

// Stage 4.5 / Step 8 — surface the fallback / status to MainLayout (banner)
// without dragging vue-router internals into the layout component.
export function bootstrapDidFallback(): boolean {
  return readSessionFlag(FALLBACK_FLAG_KEY) === '1'
}

export function clearBootstrapFallback(): void {
  try { sessionStorage.removeItem(FALLBACK_FLAG_KEY) } catch { /* ignore */ }
}

// Test hook: reset both flags so a spec can re-run bootstrap inside a
// single browser tab without forcing a real reload. Behind EXPOSE_DB
// just like `expose-debug.ts` — see that boot file's preconditions.
export function _resetBootstrapForTests(): void {
  try {
    sessionStorage.removeItem(SESSION_FLAG_KEY)
    sessionStorage.removeItem(FALLBACK_FLAG_KEY)
  } catch { /* ignore */ }
  bootstrapInflight = null
}

export default router
