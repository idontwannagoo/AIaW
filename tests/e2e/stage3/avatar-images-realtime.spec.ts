// Stage 3 / 批次-3b — avatar_images realtime fan-out / fallback / quiescence.
// id-PK envelope. The wrinkle: AvatarImage.contentBuffer is ArrayBuffer in
// IndexedDB, base64 string on the wire. The server.ts decodes back to
// ArrayBuffer when applying realtime events, so dumpTable on the receiver
// sees an ArrayBuffer (Playwright auto-serializes via the structured clone
// algorithm; we compare lengths via byteLength).
//
// 4 cases:
//   case1 ws double-tab: A put / update / delete propagates to B within 1.5s
//   case2 ws reconnect catch-up after 30s offline window
//   case3 providers-rest: no propagation without reload
//   case4 baseline: no backend traffic, no WS connections

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable } from '../helpers/db'
import { putAvatarImage, deleteAvatarImage } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'
import { openContextsForUsers } from '../helpers/tabs'
import { setOffline } from '../helpers/net'

function uniqEmail(): string {
  return `stage3-avt-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

interface TestUser { email: string; password: string; accessForBackend: string }

async function setupTestUser(): Promise<TestUser> {
  const email = uniqEmail()
  const password = 'test-password-123'
  const reg = await registerViaApi(email, password)
  return { email, password, accessForBackend: reg.access_token }
}

async function freshSession(user: TestUser): Promise<TokenPair> {
  return loginApi(user.email, user.password)
}

async function bootSession(page: Page, pair: TokenPair): Promise<void> {
  await injectAuth(page, pair)
  page.on('pageerror', (e) => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  await page.waitForFunction(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const auth = (window as any).__authSource__
    return !!(auth && auth.currentToken && auth.currentToken())
  }, undefined, { timeout: 10_000 })
}

async function activateAvatarObserver(page: Page): Promise<void> {
  await page.evaluate(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(window as any).__stage3_avt_obs__ = (window as any).__repos__.avatarImages.observeList()
  })
  await page.evaluate(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.avatarImages.list()
  })
}

async function pollUntil<T>(
  fn: () => Promise<T>,
  predicate: (v: T) => boolean,
  withinMs: number,
  pollMs = 50
): Promise<{ ok: boolean; last: T; elapsedMs: number }> {
  const start = Date.now()
  let last: T = await fn()
  while (Date.now() - start < withinMs) {
    last = await fn()
    if (predicate(last)) return { ok: true, last, elapsedMs: Date.now() - start }
    await new Promise(resolve => setTimeout(resolve, pollMs))
  }
  return { ok: false, last, elapsedMs: Date.now() - start }
}

// Inline-page helper: builds the avatar row inside page.evaluate so the
// ArrayBuffer is created in the page realm (Playwright won't structured-clone
// a Node-side ArrayBuffer through to the page reliably).
async function pagePutAvatar(
  page: Page,
  id: string,
  size: number,
  fillByte: number
): Promise<void> {
  await page.evaluate(async ({ id, size, fillByte }) => {
    const buf = new ArrayBuffer(size)
    const view = new Uint8Array(buf)
    for (let i = 0; i < size; i++) view[i] = fillByte
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await (window as any).__repos__.avatarImages.put({
      id, contentBuffer: buf, mimeType: 'image/png'
    })
  }, { id, size, fillByte })
}

// Cross-tab structural-equality check that tolerates ArrayBuffer round-trip.
async function avatarEqualOnB(
  pageB: Page,
  id: string,
  expectedSize: number,
  expectedByte: number,
  withinMs = 2_000
): Promise<{ ok: boolean; lastSize?: number; lastFirstByte?: number }> {
  const start = Date.now()
  let lastSize: number | undefined
  let lastFirstByte: number | undefined
  while (Date.now() - start < withinMs) {
    const result = await pageB.evaluate(async (id) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const row = await (window as any).__db__.avatarImages.get(id)
      if (!row) return null
      const buf: ArrayBuffer = row.contentBuffer
      const view = new Uint8Array(buf)
      return { size: buf.byteLength, firstByte: view[0] ?? -1 }
    }, id)
    if (result) {
      lastSize = result.size
      lastFirstByte = result.firstByte
      if (result.size === expectedSize && result.firstByte === expectedByte) {
        return { ok: true, lastSize, lastFirstByte }
      }
    }
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  return { ok: false, lastSize, lastFirstByte }
}

test.describe('stage3 batch-3b avatar_images realtime', () => {
  test('case1 ws double-tab: A put / update / delete propagates to B within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateAvatarObserver(pageA)
      await activateAvatarObserver(pageB)

      const id = `avt-${Math.random().toString(36).slice(2, 10)}`

      // Initial: 256 bytes filled with 0x42
      await pagePutAvatar(pageA, id, 256, 0x42)

      const seeded = await avatarEqualOnB(pageB, id, 256, 0x42, 2_000)
      expect(
        seeded.ok,
        `B should see avatar ${id} with size=256 firstByte=0x42 within 2s; ` +
          `lastSize=${seeded.lastSize} lastFirstByte=${seeded.lastFirstByte}`
      ).toBe(true)

      // Update: 512 bytes filled with 0xAA
      await pagePutAvatar(pageA, id, 512, 0xAA)

      const updated = await avatarEqualOnB(pageB, id, 512, 0xAA, 1_500)
      expect(
        updated.ok,
        `B should see updated avatar size=512 firstByte=0xAA within 1.5s; ` +
          `lastSize=${updated.lastSize} lastFirstByte=${updated.lastFirstByte}`
      ).toBe(true)

      // Delete
      await pageA.evaluate(async (id) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.avatarImages.delete(id)
      }, id)
      const removed = await pollUntil(
        () => dumpTable<{ id: string }>(pageB, 'avatarImages'),
        rows => !rows.some(r => r.id === id),
        1_500
      )
      expect(
        removed.ok,
        `avatar ${id} should be gone from B within 1.5s after A delete; ` +
          `last B ids: ${JSON.stringify(removed.last.map(r => r.id))}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case2 ws reconnect catch-up: B offline 30s while A writes 3 ops, B converges within 1.5s', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'realtime-ws', 'realtime-ws profile only')
    test.slow()

    // Use base64 helper to keep this case deterministic across the wire
    const b64 = (n: number, byte: number): string => {
      const arr = new Array(n).fill(String.fromCharCode(byte)).join('')
      return Buffer.from(arr, 'binary').toString('base64')
    }

    const user = await setupTestUser()
    const accessForBackend = user.accessForBackend
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await activateAvatarObserver(pageA)
      await activateAvatarObserver(pageB)

      const idCreate = `avt-c-${Math.random().toString(36).slice(2, 10)}`
      const idUpdate = `avt-u-${Math.random().toString(36).slice(2, 10)}`
      const idDelete = `avt-d-${Math.random().toString(36).slice(2, 10)}`

      await putAvatarImage(accessForBackend, idUpdate, {
        id: idUpdate, contentBuffer: b64(64, 0x11), mimeType: 'image/png'
      })
      await putAvatarImage(accessForBackend, idDelete, {
        id: idDelete, contentBuffer: b64(64, 0x22), mimeType: 'image/png'
      })
      const seededU = await avatarEqualOnB(pageB, idUpdate, 64, 0x11, 2_500)
      const seededD = await avatarEqualOnB(pageB, idDelete, 64, 0x22, 2_500)
      expect(seededU.ok, `pre-seed B should have avatar ${idUpdate}`).toBe(true)
      expect(seededD.ok, `pre-seed B should have avatar ${idDelete}`).toBe(true)

      await setOffline(ctxB, true)
      await putAvatarImage(accessForBackend, idCreate, {
        id: idCreate, contentBuffer: b64(64, 0x33), mimeType: 'image/png'
      })
      await putAvatarImage(accessForBackend, idUpdate, {
        id: idUpdate, contentBuffer: b64(64, 0x55), mimeType: 'image/png'
      })
      await deleteAvatarImage(accessForBackend, idDelete)
      await new Promise(resolve => setTimeout(resolve, 30_000))

      await setOffline(ctxB, false)

      // Wait for create + update + delete to converge.
      const start = Date.now()
      let pass = false
      let snapshot = ''
      while (Date.now() - start < 1_500) {
        const rows = await pageB.evaluate(async () => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const all = await (window as any).__db__.avatarImages.toArray()
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          return all.map((r: any) => ({
            id: r.id,
            size: r.contentBuffer.byteLength,
            firstByte: new Uint8Array(r.contentBuffer)[0] ?? -1
          }))
        })
        snapshot = JSON.stringify(rows)
        const c = rows.find(r => r.id === idCreate)
        const u = rows.find(r => r.id === idUpdate)
        const d = rows.find(r => r.id === idDelete)
        if (c && u && u.firstByte === 0x55 && !d) { pass = true; break }
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      expect(
        pass,
        `B did not converge within 1.5s after reconnect; last B rows: ${snapshot}`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case3 providers-rest no-realtime: A change is invisible to B until reload', async ({ browser }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')

    const user = await setupTestUser()
    const [ctxA, ctxB] = await openContextsForUsers(browser, 2)
    try {
      const pageA = await ctxA.newPage()
      const pageB = await ctxB.newPage()
      await bootSession(pageA, await freshSession(user))
      await bootSession(pageB, await freshSession(user))
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.avatarImages.list()
      })

      const id = `avt-rest-${Math.random().toString(36).slice(2, 10)}`
      await pagePutAvatar(pageA, id, 64, 0x11)

      await new Promise(resolve => setTimeout(resolve, 1_500))
      const beforeReload = await dumpTable<{ id: string }>(pageB, 'avatarImages')
      expect(
        beforeReload.some(r => r.id === id),
        `providers-rest must NOT propagate without reload; B saw ${id} ` +
          `(rows=${JSON.stringify(beforeReload.map(r => r.id))})`
      ).toBe(false)

      await injectAuth(pageB, await freshSession(user))
      await pageB.reload()
      await exposeReady(pageB)
      await pageB.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      await pageB.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.avatarImages.list()
      })
      const afterReload = await dumpTable<{ id: string }>(pageB, 'avatarImages')
      expect(
        afterReload.some(r => r.id === id),
        `after reload + list(), B should pull ${id} from server ` +
          `(rows=${JSON.stringify(afterReload.map(r => r.id))})`
      ).toBe(true)
    } finally {
      await ctxA.close()
      await ctxB.close()
    }
  })

  test('case4 baseline byte-identical: zero backend traffic and zero websocket connections', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'baseline', 'baseline profile only')

    const backendCalls: string[] = []
    const wsUrls: string[] = []
    page.on('request', (req) => {
      if (req.url().includes('127.0.0.1:9011')) backendCalls.push(req.url())
    })
    page.on('websocket', (ws) => { wsUrls.push(ws.url()) })

    await page.goto('/')
    await exposeReady(page)
    const avatars = await dumpTable(page, 'avatarImages')
    expect(Array.isArray(avatars)).toBe(true)
    await page.waitForTimeout(1_000)

    expect(
      backendCalls,
      `baseline must not call 127.0.0.1:9011; saw ${backendCalls.length}: ${JSON.stringify(backendCalls)}`
    ).toEqual([])
    expect(wsUrls, `baseline must not open any ws; saw ${JSON.stringify(wsUrls)}`).toEqual([])
  })
})
