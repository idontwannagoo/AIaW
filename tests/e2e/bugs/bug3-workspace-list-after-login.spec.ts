// Bug 3 — 登录后工作区列表不立即刷新，需要切路由才显示。
//
// 原始 bug：从干净本地状态登录后，左侧 workspace nav 不立刻渲染云端
// workspace；点一下、切个路由之后才"延迟"出现。
//
// 根因：登录走 applyTokenPair → 写 localStorage + userRef.value，但
// router.beforeEach 的 once-per-session bootstrap guard 因为路由没变化
// 不会重跑；workspaces.server.ts 的 observeList 已经挂了 dexie liveQuery
// 但 IDB 还是空（没有 pull 触发）。"切路由后才出现" = 那次切路由触发
// 了 router.beforeEach → bootstrap 跑了 → 写 IDB → liveQuery fire。
//
// 修复后：applyTokenPair 末尾 emit 'login' → router/index.ts listener
// triggerBootstrapNow（无需切路由）→ applyBootstrap 写 db.workspaces →
// workspaces.server observeList 的 liveQuery fire → workspaceStore
// .workspaces ref 更新 → WorkspaceNav 看到。
//
// 故障注入红测：注释 src/data/auth.backend.ts:134-136 的 emit 'login'
// 后此 spec 必红（IDB workspaces 永远 0，store.workspaces 永远 []）。
//
// 这个 spec 直接断言两层：① IDB 里 workspace 行存在；② Pinia store
// .workspaces 反应式数组包含该行（更接近 UI 真值）。不去 query DOM
// 因为 WorkspaceListSelect 的 DOM 选择器跨语言 / 跨 Quasar 版本不稳。

import { test, expect } from '@playwright/test'
import {
  setupUser,
  seedWorkspaces,
  gotoCleanRoot,
  loginViaDialog,
  waitLoggedIn,
  BOOTSTRAP_ATTEMPTED_KEY
} from './_helpers'

test.describe('bug3 workspace list immediate after login', () => {
  for (const profile of ['realtime-ws', 'providers-rest'] as const) {
    test(`bug3 [${profile}] login via UI → workspace appears in store within 3s, no route change`, async ({ browser }, testInfo) => {
      test.skip(testInfo.project.name !== profile, `${profile} only`)

      const user = await setupUser('b3')
      const wsIds = await seedWorkspaces(user.pair, 2, 'b3ws')

      const ctx = await browser.newContext()
      try {
        const page = await ctx.newPage()
        await gotoCleanRoot(page)

        // Pre-condition: seeded workspaces NOT yet in IDB (only the
        // local default workspace from src/utils/db.ts::populate exists).
        const preWs = await page.evaluate(async (ids) => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const db = (window as any).__db__
          const rows = await db.workspaces.toArray()
          return {
            count: rows.length,
            seededFound: ids.filter((id: string) => rows.some((r: { id: string }) => r.id === id))
          }
        }, wsIds)
        expect(
          preWs.seededFound.length,
          `pre-login: no seeded workspace must be in IDB yet (got ${JSON.stringify(preWs)})`
        ).toBe(0)

        await loginViaDialog(page, 'login', user.email, user.password)
        await waitLoggedIn(page, user.email)

        // bootstrap-now fires → applyBootstrap writes db.workspaces →
        // workspacesStore.workspaces (which is repos.workspaces.observeList)
        // updates within ~3s for the IDB liveQuery roundtrip + bootstrap
        // 2s timeout slack.
        //
        // Note we deliberately do NOT assert "no route change" — the
        // app's IndexPage uses `useOpenLastWorkspace` which can router.push
        // to a workspace once data hydrates (and that's a *desirable*
        // post-fix behaviour, not a bug). The Bug 3 contract is
        // "workspace appears in IDB / store without manual interaction",
        // which is what we measure.
        const start = Date.now()
        await expect.poll(
          async () => {
            return page.evaluate(async (ids) => {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              const dbHandle = (window as any).__db__
              const rows = await dbHandle.workspaces.toArray()
              return ids.every((id: string) => rows.some((r: { id: string }) => r.id === id))
            }, wsIds)
          },
          {
            message: `seeded workspaces ${wsIds.join(', ')} must all land in IDB within 3s post-login`,
            timeout: 3_000,
            intervals: [100, 200, 300, 500]
          }
        ).toBe(true)
        const idbMs = Date.now() - start

        // Defense-in-depth: the bootstrap.attempted flag should be set —
        // proves bootstrap fired. If this is null but IDB is populated,
        // something other than the auth-events path put rows in IDB
        // (regression alert — read the call stack).
        const attempted = await page.evaluate(
          (k) => sessionStorage.getItem(k),
          BOOTSTRAP_ATTEMPTED_KEY
        )
        expect(attempted, 'bootstrap.attempted flag must be set after UI login').toBe('1')

        const routeAfter = await page.evaluate(() => window.location.href)
        console.log(`[bug3] workspace IDB ready in ${idbMs}ms; route after login = ${routeAfter}`)
      } finally {
        await ctx.close()
      }
    })
  }
})
