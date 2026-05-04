// Bug 6 — 对话页发送 / 编辑分支按钮延迟 0.5-1s。
//
// 现象：点 send / edit 后 UI 等 0.5-1s 才响应。Network 显示 3-4 个串行
// PUT 各 ~200ms。Bug 5 Round 2 拆 runTx → sequential await 后，server-
// routed `<table>.server.ts::putOne` 形态是 `await http.put` → `cache.put
// (decoded)`，意味着本地 IDB 缓存只在 HTTP 返回后才写 → liveQuery 直到
// HTTP 完成才 emit → UI 累计等待全部 PUT RTT 之和。
//
// 修复方向（与用户对齐：方案 1+3）：
//   1) `<table>.server.ts::putOne` 改 local-first：`db.<table>.put(value)`
//      在 `http.put` 之前 → liveQuery 立即 emit → UI 在 HTTP 等待期已经
//      看到新行；HTTP 返回后用 server canonical version 覆盖一次。
//   2) `DialogView.vue::appendMessage` 把 `messages.add` 与 `dialogs.update`
//      改为 `Promise.all` 并行（dialog cache 读 + msgTree 计算先做，无
//      HTTP 依赖；server 侧两表无外键）。单次 appendMessage 的 RTT
//      从 ~400ms 折叠到 ~200ms。
//
// ────────────────────────────────────────────────────────────────────────
// 本 spec 不通过 send 按钮点击来观测，原因：`send()` 在 `stream()` 之前
// 串了一句 `await repos.messages.update(target, { status: 'default' })`，
// 这一句会把后续 `appendMessage` 调用阻塞在第一个 HTTP 上，让「count
// 在 HTTP 完成前到 3」这种端到端断言无法在 sub-HTTP 时间内成立。该
// await 是 send 流程的固定语义（流前置状态翻转），不在本次 1+3 修复
// 范围内。本 spec 改为直接测 putOne 形态：从 page 上下文调
// `__repos__.messages.add(...)` 不 await，HTTP_DELAY=1500ms 路由拦截下，
// 断言 50ms 内 IDB 已有新行。这是方案 3 的核心不变量。
//
// 故障注入红测：把 `messages.server.ts::putOne` 内 `await db.messages.put
// (value)` 那一行删掉（恢复 HTTP-first） → `rm -rf tests/.builds/
// providers-rest tests/.builds/realtime-ws` → 此 spec 双 profile 红
// （IDB 在 50ms 内 `messages.get(id)` 仍是 undefined）→ 恢复 → 双
// profile 绿。
//
// Profile：
//   - providers-rest 必跑：BACKEND_AUTH + 全 10 张 backend 表都开但 realtime
//     关 —— 验本 spec 在「无 realtime push 重写」路径上的核心断言。
//   - realtime-ws 也跑：验同改动在 ws push 路径上不引入 race / regression。

import { test, expect } from '@playwright/test'
import {
  setupUser,
  seedWorkspaces,
  gotoCleanRoot,
  loginViaDialog,
  waitLoggedIn
} from './_helpers'

interface SeedIds {
  workspaceId: string
  dialogId: string
  inputingMessageId: string
}

const HTTP_DELAY_MS = 1500
const LOCAL_OBSERVE_WINDOW_MS = 200

// We seed a workspace + dialog + inputing message via direct backend API
// (no UI), then login and navigate. After page is settled, we install a
// page.route that delays /api/v1/messages and /api/v1/dialogs PUTs by
// HTTP_DELAY_MS. From inside the page context we then issue a single
// `__repos__.messages.add(...)` (NOT awaited) and assert that within
// LOCAL_OBSERVE_WINDOW_MS, the row already exists in IDB. Pre-fix this
// would be impossible (cache.put only runs after HTTP completes).

test.describe('bug6 dialog write optimistic', () => {
  for (const profile of ['providers-rest', 'realtime-ws'] as const) {
    test(`bug6 [${profile}] putOne writes IDB before HTTP completes (local-first)`, async ({ browser }, testInfo) => {
      test.skip(testInfo.project.name !== profile, `${profile} only`)
      test.setTimeout(60_000)

      const user = await setupUser('b6')
      const ctx = await browser.newContext()
      try {
        const page = await ctx.newPage()

        const allConsole: string[] = []
        page.on('console', (msg) => {
          allConsole.push(`[${msg.type()}] ${msg.text()}`)
        })
        page.on('pageerror', (e) => {
          allConsole.push(`[pageerror] ${e.message}`)
        })

        await page.addInitScript(() => {
          localStorage.setItem(
            'local-data',
            '__q_objt|' + JSON.stringify({
              lastReloadTimestamp: null,
              visited: true,
              language: 'en-US',
              ignoredUpdate: ''
            })
          )
        })

        // Seed a workspace + dialog + inputing message via direct backend
        // PUT (token from setupUser). The dialog/message details don't
        // matter to the assertion; we only need the IDs to exist server-
        // side so bootstrap pulls them into IDB.
        const [wsId] = await seedWorkspaces(user.pair, 1, 'b6-ws')
        const dialogId = `dlg-${Math.random().toString(36).slice(2, 8)}`
        const inputingMessageId = `msg-${Math.random().toString(36).slice(2, 8)}`
        const seed: SeedIds = { workspaceId: wsId, dialogId, inputingMessageId }

        // Direct backend PUT to seed dialog + message (mirrors backendClient
        // helper inline; we only need 2 calls).
        const { BACKEND_URL } = await import('../helpers/env')
        await fetch(`${BACKEND_URL}/api/v1/dialogs/${dialogId}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${user.pair.access_token}`
          },
          body: JSON.stringify({
            id: dialogId,
            workspaceId: wsId,
            name: 'bug6 seed dialog',
            assistantId: null,
            msgTree: { $root: [inputingMessageId], [inputingMessageId]: [] },
            msgRoute: [0],
            inputVars: {},
            modelOverride: null
          })
        })
        await fetch(`${BACKEND_URL}/api/v1/messages/${inputingMessageId}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${user.pair.access_token}`
          },
          body: JSON.stringify({
            id: inputingMessageId,
            type: 'user',
            dialogId,
            contents: [{ type: 'user-message', text: 'seed', items: [] }],
            status: 'inputing'
          })
        })

        await gotoCleanRoot(page)
        await loginViaDialog(page, 'login', user.email, user.password)
        await waitLoggedIn(page, user.email)

        // Wait for bootstrap to pull seeded data into IDB.
        await expect.poll(
          async () => page.evaluate(
            async ({ wsId, dlgId, msgId }) => {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              const db = (window as any).__db__
              if (!db) return false
              const [w, d, m] = await Promise.all([
                db.workspaces.get(wsId),
                db.dialogs.get(dlgId),
                db.messages.get(msgId)
              ])
              return !!w && !!d && !!m
            },
            { wsId: seed.workspaceId, dlgId: seed.dialogId, msgId: seed.inputingMessageId }
          ),
          {
            message: 'bootstrap must hydrate seeded ws + dialog + message within 5s post-login',
            timeout: 5_000,
            intervals: [100, 200, 300, 500]
          }
        ).toBe(true)

        // Wait a bit for realtime subs / observers to settle so any post-
        // login traffic doesn't get caught by the delay route.
        await page.waitForTimeout(800)

        // Install HTTP delay AFTER bootstrap. From now on every PUT to
        // /api/v1/messages or /api/v1/dialogs is held HTTP_DELAY_MS before
        // forwarding. With local-first putOne the spec proves that the
        // IDB cache reflects the new row before the held HTTP returns.
        const seenPutPaths: Array<{ path: string; tStart: number }> = []
        const tInstall = Date.now()
        await page.route(/\/api\/v1\/(messages|dialogs)\//, async (route) => {
          const req = route.request()
          if (req.method() === 'PUT') {
            seenPutPaths.push({
              path: new URL(req.url()).pathname,
              tStart: Date.now() - tInstall
            })
            await new Promise((resolve) => setTimeout(resolve, HTTP_DELAY_MS))
          }
          await route.continue()
        })

        // Now the core test. From page context:
        //   1) Generate a fresh message id.
        //   2) Call repos.messages.add(...) — but DO NOT await it.
        //   3) Sleep LOCAL_OBSERVE_WINDOW_MS.
        //   4) Read db.messages.get(newId) — must be defined (proves
        //      local-first put landed before HTTP completes).
        //   5) Await the original add promise to drain (will take ~HTTP_
        //      DELAY_MS more).
        const result = await page.evaluate(
          async ({ dlgId, observeMs }) => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const repos = (window as any).__repos__
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const db = (window as any).__db__
            if (!repos || !db) {
              return { error: '__repos__ or __db__ not exposed (EXPOSE_DB profile?)' }
            }

            const newId = `b6-test-${Math.random().toString(36).slice(2, 10)}`
            const tCallStart = performance.now()
            // Fire-and-forget the add. With local-first putOne, the
            // db.messages.put(value) inside putOne fires almost
            // immediately; only the http.put is held by the route
            // delay. With pre-fix HTTP-first, db.messages.put(decoded)
            // happens AFTER HTTP returns → undefined here.
            const addPromise = repos.messages.add({
              id: newId,
              dialogId: dlgId,
              type: 'user',
              contents: [{ type: 'user-message', text: 'b6 optimistic', items: [] }],
              status: 'default'
            })
            // Swallow the eventual settlement so it doesn't show up as
            // an unhandled rejection while we observe.
            const drained = addPromise.then(
              () => 'fulfilled' as const,
              (e: unknown) => `rejected: ${e instanceof Error ? e.message : String(e)}` as const
            )

            await new Promise((resolve) => setTimeout(resolve, observeMs))
            const tObserveAt = performance.now() - tCallStart
            const row = await db.messages.get(newId)
            const rowSnapshot = row ? {
              id: row.id,
              status: row.status,
              dialogId: row.dialogId,
              contentsLen: Array.isArray(row.contents) ? row.contents.length : -1
            } : null

            // Now wait for HTTP to actually finish so the test cleanup
            // doesn't leave a dangling fetch. Cap to 5s in case backend
            // misbehaves.
            const verdict = await Promise.race([
              drained,
              new Promise<'timeout'>((resolve) => setTimeout(() => resolve('timeout'), 5_000))
            ])

            return {
              error: null,
              newId,
              tObserveAt,
              rowExisted: row != null,
              rowSnapshot,
              addPromiseVerdict: verdict
            }
          },
          { dlgId: seed.dialogId, observeMs: LOCAL_OBSERVE_WINDOW_MS }
        )

        console.log(`[bug6][${profile}] result=${JSON.stringify(result)}`)
        console.log(`[bug6][${profile}] seenPutPaths=${JSON.stringify(seenPutPaths)}`)
        if (allConsole.length > 0) {
          console.log(`[bug6][${profile}] page console tail (last 10):`)
          for (const l of allConsole.slice(-10)) console.log(`  ${l}`)
        }

        if (result.error) {
          throw new Error(`spec setup failure: ${result.error}`)
        }

        // CORE ASSERTION — the row exists in IDB within
        // LOCAL_OBSERVE_WINDOW_MS, well before the HTTP_DELAY_MS-delayed
        // PUT response could have triggered any cache write. This is
        // only possible if putOne does `db.messages.put(value)` BEFORE
        // `await http.put(...)`.
        expect(
          result.rowExisted,
          `messages row must exist in IDB within ${LOCAL_OBSERVE_WINDOW_MS}ms (HTTP_DELAY=${HTTP_DELAY_MS}ms — proves local-first putOne). Snapshot=${JSON.stringify(result.rowSnapshot)} tObserveAt=${result.tObserveAt}ms`
        ).toBe(true)
        expect(result.rowSnapshot, 'row should have correct shape').toMatchObject({
          status: 'default',
          dialogId: seed.dialogId,
          contentsLen: 1
        })

        // Sanity — at least one PUT should have been intercepted by the
        // route delay (proves the route was active).
        expect(
          seenPutPaths.length,
          `at least one /messages/ PUT should have been intercepted; got=${JSON.stringify(seenPutPaths)}`
        ).toBeGreaterThanOrEqual(1)

        // Eventually the PUT should drain (`fulfilled`). 'timeout' would
        // mean the put never settled — surface that.
        expect(
          result.addPromiseVerdict,
          'add() should have eventually settled (HTTP delay + completion expected within 5s)'
        ).toBe('fulfilled')
      } finally {
        await ctx.close()
      }
    })
  }
})
