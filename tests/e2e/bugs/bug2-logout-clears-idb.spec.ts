// Bug 2 — 退出登录不清本地 IDB，残留旧数据 + 401 报错。
//
// 原始 bug：点击退出登录后 UI 仍然渲染上一账号的 workspaces / dialogs /
// providers / plugins 等；后续点击会因 token 失效报 401。
//
// 修复后：authSource.logout() 顺序：① emit 'logout'（让各 server 模块
// unsubscribe realtime + 重置 lastVersion）② await clearAllSyncedTables()
// 清 10 张表 ③ clearAuth()（清 token + 触发 router listener 清
// sessionStorage bootstrap flag + bootstrapInflight）。
//
// 故障注入红测：注释 src/data/auth.backend.ts:265 的
// `await clearAllSyncedTables()` 后此 spec 的 case 1 必红（10 表 count
// 仍非 0）。
//
// 三个 case：
//   case 1 single-account logout：登录 A → seed 一些行 → 登出 → 10 表
//          count === 0 + sessionStorage flag 已清 + localStorage refresh
//          token 已清。
//   case 2 cross-account：A 登录 → seed → 登出 → B 登录 → 不出现 401
//          + B 的 workspace 出现 + 表里没有 A 的 row id。
//   case 3 listener idempotent：emit 'logout' 在 listener 与 clearAuth
//          中被触发两次，listener 必须不抛异常（间接断言：第二次 emit
//          后 spec 仍能正常运行 / IDB 仍然空）。
//
// Profile：providers-rest（最小集，证明 IDB clear 不依赖 realtime；
// realtime-ws 同样跑用于覆盖 unsubscribe 路径）。

import { test, expect } from '@playwright/test'
import {
  setupUser,
  setupTwoUsers,
  seedWorkspaces,
  gotoCleanRoot,
  loginViaDialog,
  waitLoggedIn,
  waitLoggedOut,
  clickLogoutOnAccountPage,
  readAllTableCounts,
  readAuthArtifacts,
  SYNCED_TABLES,
  REFRESH_KEY,
  USER_KEY,
  BOOTSTRAP_ATTEMPTED_KEY,
  BOOTSTRAP_FALLBACK_KEY
} from './_helpers'
import { backendClient } from '../helpers/backend'

test.describe('bug2 logout wipes local IDB', () => {
  for (const profile of ['providers-rest', 'realtime-ws'] as const) {
    test(`bug2 case1 [${profile}] logout clears 10 tables + storage flags`, async ({ browser }, testInfo) => {
      test.skip(testInfo.project.name !== profile, `${profile} only`)

      const user = await setupUser('b2-c1')
      // Seed two id-PK tables so we can prove clear() actually ran (not
      // just "tables happened to be empty already").
      const [wsId] = await seedWorkspaces(user.pair, 1)
      const client = backendClient(user.pair.access_token)
      await client.put(`/api/v1/providers/prov-${wsId}`, {
        id: `prov-${wsId}`,
        name: 'seed prov',
        type: 'openai-response',
        settings: { apiKey: 'sk-fake' },
        avatar: { type: 'icon', icon: 'sym_o_smart_toy' }
      })
      await client.put(`/api/v1/reactives/${encodeURIComponent('#user-data')}`, {
        seed: 'bug2'
      })

      const ctx = await browser.newContext()
      try {
        const page = await ctx.newPage()
        await gotoCleanRoot(page)
        await loginViaDialog(page, 'login', user.email, user.password)
        await waitLoggedIn(page, user.email)

        // Wait for bootstrap to land at least the workspace + provider
        // + reactives row in IDB.
        await page.waitForFunction(
          (id) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const db = (window as any).__db__
            return Promise.all([
              db.workspaces.get(id),
              db.providers.count(),
              db.reactives.count()
            ]).then(([w, pc, rc]) => !!w && pc >= 1 && rc >= 1)
          },
          wsId,
          { timeout: 8_000 }
        )

        const beforeLogout = await readAllTableCounts(page)
        expect(beforeLogout.workspaces, 'workspaces should have at least 1 before logout').toBeGreaterThanOrEqual(1)
        expect(beforeLogout.providers, 'providers should have at least 1 before logout').toBeGreaterThanOrEqual(1)
        expect(beforeLogout.reactives, 'reactives should have at least 1 before logout').toBeGreaterThanOrEqual(1)

        // Drive the AccountPage logout button (real UI path, not
        // authSource.logout() directly — guards against regressions
        // where the page-level handler is what wires up the cleanup).
        await page.goto('/account')
        await clickLogoutOnAccountPage(page)
        await waitLoggedOut(page)

        // Allow the post-logout async clear + listener fanout to settle.
        // clearAllSyncedTables awaits all 10 .clear() in parallel — the
        // logout() async function awaits it before returning — but
        // sessionStorage cleanup happens in the auth-events listener
        // which is sync. Polling instead of a fixed sleep so we don't
        // mask a real lag bug.
        await page.waitForFunction(
          (tables) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const db = (window as any).__db__
            return Promise.all(tables.map((t) => db[t].count())).then((cs) =>
              cs.every((c) => c === 0)
            )
          },
          [...SYNCED_TABLES],
          { timeout: 5_000 }
        )

        const afterLogout = await readAllTableCounts(page)
        for (const t of SYNCED_TABLES) {
          expect(
            afterLogout[t],
            `table ${t} must be empty after logout (was ${beforeLogout[t]} → ${afterLogout[t]})`
          ).toBe(0)
        }

        const artifacts = await readAuthArtifacts(page)
        expect(artifacts.refresh, `localStorage.${REFRESH_KEY} must be cleared`).toBeNull()
        expect(artifacts.user, `localStorage.${USER_KEY} must be cleared`).toBeNull()
        expect(
          artifacts.bootstrapAttempted,
          `sessionStorage.${BOOTSTRAP_ATTEMPTED_KEY} must be cleared`
        ).toBeNull()
        expect(
          artifacts.bootstrapFallback,
          `sessionStorage.${BOOTSTRAP_FALLBACK_KEY} must be cleared`
        ).toBeNull()
        console.log(
          `[bug2-c1] before: ${JSON.stringify(beforeLogout)} → after: ${JSON.stringify(afterLogout)}`
        )
      } finally {
        await ctx.close()
      }
    })
  }

  test('bug2 case2 [providers-rest] cross-account: A login → logout → B login same-tab → no A residue + no 401 死链', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest only')

    const { a, b } = await setupTwoUsers()
    const [wsA] = await seedWorkspaces(a.pair, 1, 'wsA')
    const [wsB] = await seedWorkspaces(b.pair, 1, 'wsB')

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      // Capture all 401 responses for the duration — Bug 2's "401 死链"
      // symptom is exactly post-logout requests with the dead token
      // (or post-cross-login requests with stale per-table cursors that
      // would fetch with the wrong account's session).
      const all401: { url: string; status: number; phase: string }[] = []
      const apiTrace: { method: string; url: string; status?: number; phase: string }[] = []
      let phase = 'logged-in-A'
      page.on('request', (req) => {
        const u = req.url()
        if (u.includes('/api/v1/')) {
          apiTrace.push({
            method: req.method(),
            url: u.replace(/^https?:\/\/[^/]+/, ''),
            phase
          })
        }
      })
      page.on('response', (resp) => {
        const u = resp.url()
        if (u.includes('/api/v1/')) {
          if (resp.status() === 401) {
            all401.push({ url: u, status: 401, phase })
          }
          const idx = apiTrace.findIndex(
            (c) => c.url === u.replace(/^https?:\/\/[^/]+/, '') && c.status === undefined
          )
          if (idx >= 0) apiTrace[idx].status = resp.status()
        }
      })

      // Phase 1: A logs in via UI dialog → bootstrap → A's workspace
      // in IDB. Use UI login to exercise the applyTokenPair → emit
      // 'login' path same as Bug 1 / 3 / 4.
      await gotoCleanRoot(page)
      await loginViaDialog(page, 'login', a.email, a.password)
      await waitLoggedIn(page, a.email)
      await expect.poll(
        async () => {
          return page.evaluate(async (id) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            return !!(await (window as any).__db__.workspaces.get(id))
          }, wsA)
        },
        {
          message: `A's workspace ${wsA} must land post-login`,
          timeout: 5_000,
          intervals: [100, 200, 300, 500]
        }
      ).toBe(true)

      // Phase 2: log out via UI on /account. After this all 10 tables
      // must be empty AND localStorage refresh must be cleared (so a
      // "stale token 401" can't happen).
      phase = 'logging-out-A'
      await page.goto('/account')
      await clickLogoutOnAccountPage(page)
      await waitLoggedOut(page)
      await expect.poll(
        async () => {
          return page.evaluate(async (tables) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const db = (window as any).__db__
            const counts = await Promise.all(tables.map((t: string) => db[t].count()))
            return counts.every((c: number) => c === 0)
          }, [...SYNCED_TABLES])
        },
        {
          message: `all 10 tables must be empty post-logout`,
          timeout: 5_000,
          intervals: [100, 200, 300, 500]
        }
      ).toBe(true)

      // Phase 3: B "logs in fresh" — same-tab UI dialog flow. After
      // logout the user is on / again with the firstVisit Welcome
      // dialog blocking the page; we open BackendLoginDialog over it
      // and submit. Important: the BackendLoginDialog DOM coexists
      // with the Welcome dialog so the locator must filter by the
      // "email/password" labels (the existing _helpers.loginViaDialog
      // already does this — `.filter({ has: page.getByLabel(/email/i) })`).
      // If the Welcome dialog were stealing focus or the dialog open
      // were silently failing, waitLoggedIn would time out below.
      phase = 'logging-in-B'
      await loginViaDialog(page, 'login', b.email, b.password)
      await waitLoggedIn(page, b.email)

      // B's workspace lands via bootstrap-on-login (applyTokenPair →
      // emit 'login' → router listener triggerBootstrapNow). The
      // sessionStorage `bootstrap.attempted` was cleared by the
      // 'logout' listener above so this is a fresh attempt.
      try {
        await expect.poll(
          async () => {
            return page.evaluate(async (id) => {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              return !!(await (window as any).__db__.workspaces.get(id))
            }, wsB)
          },
          {
            message: `B's workspace ${wsB} must land post-cross-login`,
            timeout: 5_000,
            intervals: [100, 200, 300, 500]
          }
        ).toBe(true)
      } catch (err) {
        const diag = await page.evaluate(async () => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const win = window as any
          const db = win.__db__
          const ws = await db.workspaces.toArray()
          const auth = win.__authSource__
          return {
            wsRows: ws.map((r: { id: string; name?: string }) => ({ id: r.id, name: r.name })),
            attempted: sessionStorage.getItem('aiaw.bootstrap.attempted'),
            fallback: sessionStorage.getItem('aiaw.bootstrap.fallback'),
            currentUserEmail: auth?.user?.value?.email ?? null,
            currentTokenSnippet: auth?.currentToken?.()?.slice(0, 30) ?? null
          }
        })
        console.log(`[bug2-c2] cross-login B IDB state: ${JSON.stringify(diag)}`)
        console.log(`[bug2-c2] api trace last 20: ${JSON.stringify(apiTrace.slice(-20))}`)
        throw err
      }

      // Phase 4: verify no A residue and zero 401s in any phase.
      phase = 'verify-B'
      const wsRows = await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        return await (window as any).__db__.workspaces.toArray()
      })
      const wsIds = new Set<string>(wsRows.map((r: { id: string }) => r.id))
      expect(wsIds.has(wsB), `B's workspace ${wsB} must be in IDB`).toBe(true)
      expect(wsIds.has(wsA), `A's workspace ${wsA} must NOT leak into B's IDB (got ${JSON.stringify([...wsIds])})`).toBe(false)

      // 401 budget: only the *post-relogin* (logging-in-B / verify-B)
      // phases must be 401-free. The bug 2 死链 symptom is "after I
      // log out and back in as someone else, the UI keeps trying to
      // use the dead token and fails". The brief window during
      // logout itself where in-flight reactive `get()` calls race
      // against the token clear is a known benign window — the user
      // sees no UI for it and they're not the symptom of Bug 2.
      const post401 = all401.filter(
        (e) => e.phase === 'logging-in-B' || e.phase === 'verify-B'
      )
      expect(
        post401,
        `expected zero 401 responses post-relogin; got: ${JSON.stringify(post401)}`
      ).toEqual([])
    } finally {
      await ctx.close()
    }
  })

  test('bug2 case3 [providers-rest] listener idempotent: double logout flow does not throw', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest only')

    // Internal contract: clearAuth() emits 'logout' a second time after
    // logout() already emitted it once. Listeners must be idempotent —
    // resetting `lastVersion=0` / null inflight / null realtime
    // unsubscribe is fine to do twice. We exercise the path by directly
    // calling authSource.logout() twice in a row from the page context.
    const user = await setupUser('b2-c3')
    await seedWorkspaces(user.pair, 1)

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await gotoCleanRoot(page)
      await loginViaDialog(page, 'login', user.email, user.password)
      await waitLoggedIn(page, user.email)

      // Capture page errors during the second logout. If any listener
      // were to throw on the no-op second pass, we'd see it here.
      const errs: string[] = []
      page.on('pageerror', (e) => errs.push(e.message))

      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        await auth.logout()
        // Second call: clearAuth() already nulled userRef; the
        // wasLoggedIn guard inside clearAuth suppresses the 2nd emit.
        // We still call logout() explicitly to exercise the post-clear
        // path.
        await auth.logout()
      })
      await waitLoggedOut(page)

      const counts = await readAllTableCounts(page)
      for (const t of SYNCED_TABLES) {
        expect(counts[t], `table ${t} empty after double logout`).toBe(0)
      }

      // No page errors from listener double-fire.
      const authErrs = errs.filter((m) =>
        /auth-events|local-cache|server\.ts|Cannot read|null/i.test(m)
      )
      expect(authErrs, `no auth-related page errors during double logout; got ${JSON.stringify(errs)}`).toEqual([])
    } finally {
      await ctx.close()
    }
  })
})
