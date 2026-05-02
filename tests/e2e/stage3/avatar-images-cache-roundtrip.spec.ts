// Stage 3 / 批次-3b — avatar_images IndexedDB cache <→ server roundtrip.
// id-PK table with binary contentBuffer. 2 cases:
//   case1: clear IDB → reload → server data re-pulled, contentBuffer round-trips
//   case2: write + reload → list() does not spam ?since=0

import { test, expect, type Page } from '@playwright/test'
import { exposeReady, dumpTable, clearAll } from '../helpers/db'
import { putAvatarImage } from '../helpers/backend'
import { registerViaApi, loginApi, injectAuth, type TokenPair } from '../helpers/auth'

function uniqEmail(): string {
  return `stage3-avt-cache-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
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

function b64(n: number, byte: number): string {
  const arr = new Array(n).fill(String.fromCharCode(byte)).join('')
  return Buffer.from(arr, 'binary').toString('base64')
}

async function readAvatarMeta(
  page: Page,
  id: string
): Promise<{ size: number; firstByte: number } | null> {
  return page.evaluate(async (id) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const row = await (window as any).__db__.avatarImages.get(id)
    if (!row) return null
    const buf: ArrayBuffer = row.contentBuffer
    const view = new Uint8Array(buf)
    return { size: buf.byteLength, firstByte: view[0] ?? -1 }
  }, id)
}

test.describe('stage3 batch-3b avatar_images cache roundtrip', () => {
  test('case1 cleared IndexedDB → reload → server data re-pulled and contentBuffer survives roundtrip', async ({ browser }, testInfo) => {
    test.skip(
      !['providers-rest', 'realtime-ws', 'realtime-auto'].includes(testInfo.project.name),
      'cache-roundtrip needs a server-routed profile'
    )

    const user = await setupTestUser()
    // Unique IDs per profile run — single-column PK clashes otherwise.
    const id1 = `cache-avt-1-${Math.random().toString(36).slice(2, 10)}`
    const id2 = `cache-avt-2-${Math.random().toString(36).slice(2, 10)}`
    await putAvatarImage(user.accessForBackend, id1, {
      id: id1, contentBuffer: b64(128, 0xAB), mimeType: 'image/png'
    })
    await putAvatarImage(user.accessForBackend, id2, {
      id: id2, contentBuffer: b64(256, 0xCD), mimeType: 'image/jpeg'
    })

    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await bootSession(page, await freshSession(user))
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.avatarImages.list()
      })

      const seeded1 = await readAvatarMeta(page, id1)
      const seeded2 = await readAvatarMeta(page, id2)
      expect(
        seeded1?.size === 128 && seeded1?.firstByte === 0xAB,
        `seed pull ${id1}: ${JSON.stringify(seeded1)}`
      ).toBe(true)
      expect(
        seeded2?.size === 256 && seeded2?.firstByte === 0xCD,
        `seed pull ${id2}: ${JSON.stringify(seeded2)}`
      ).toBe(true)

      await clearAll(page, ['avatarImages'])
      const wiped = await dumpTable<{ id: string }>(page, 'avatarImages')
      expect(wiped, 'IDB clear should empty avatarImages cache').toEqual([])

      await injectAuth(page, await freshSession(user))
      await page.reload()
      await exposeReady(page)
      await page.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.avatarImages.list()
      })

      const r1 = await readAvatarMeta(page, id1)
      const r2 = await readAvatarMeta(page, id2)
      expect(
        r1?.size === 128 && r1?.firstByte === 0xAB &&
          r2?.size === 256 && r2?.firstByte === 0xCD,
        `cache should be re-pulled after clear+reload (r1=${JSON.stringify(r1)} r2=${JSON.stringify(r2)})`
      ).toBe(true)
    } finally {
      await ctx.close()
    }
  })

  test('case2 write + reload → list() does not re-fetch full ?since=0', async ({ browser }, testInfo) => {
    test.skip(
      !['providers-rest', 'realtime-ws', 'realtime-auto'].includes(testInfo.project.name),
      'cache-roundtrip needs a server-routed profile'
    )

    const user = await setupTestUser()
    const ctx = await browser.newContext()
    try {
      const page = await ctx.newPage()
      await bootSession(page, await freshSession(user))

      // Unique IDs per profile run — single-column PK clashes otherwise.
      const w1 = `cache-avt-w1-${Math.random().toString(36).slice(2, 10)}`
      const w2 = `cache-avt-w2-${Math.random().toString(36).slice(2, 10)}`
      await page.evaluate(async ({ w1, w2 }) => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const r = (window as any).__repos__.avatarImages
        function makeBuf(size: number, byte: number): ArrayBuffer {
          const buf = new ArrayBuffer(size)
          const view = new Uint8Array(buf)
          for (let i = 0; i < size; i++) view[i] = byte
          return buf
        }
        await r.put({ id: w1, contentBuffer: makeBuf(64, 0x10), mimeType: 'image/png' })
        await r.put({ id: w2, contentBuffer: makeBuf(64, 0x20), mimeType: 'image/png' })
      }, { w1, w2 })
      const seeded1 = await readAvatarMeta(page, w1)
      const seeded2 = await readAvatarMeta(page, w2)
      expect(
        seeded1?.firstByte === 0x10 && seeded2?.firstByte === 0x20,
        `cache should hold both written rows (r1=${JSON.stringify(seeded1)} r2=${JSON.stringify(seeded2)})`
      ).toBe(true)

      await injectAuth(page, await freshSession(user))
      await page.reload()
      await exposeReady(page)
      await page.waitForFunction(() => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const auth = (window as any).__authSource__
        return !!(auth && auth.currentToken && auth.currentToken())
      }, undefined, { timeout: 10_000 })

      const reqs: string[] = []
      page.on('request', (req) => {
        if (req.url().includes('/api/v1/avatar-images')) reqs.push(req.url())
      })
      await page.evaluate(async () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        await (window as any).__repos__.avatarImages.list()
      })

      const r1 = await readAvatarMeta(page, w1)
      const r2 = await readAvatarMeta(page, w2)
      expect(
        r1?.firstByte === 0x10 && r2?.firstByte === 0x20,
        `cache should retain rows after reload (r1=${JSON.stringify(r1)} r2=${JSON.stringify(r2)})`
      ).toBe(true)
      expect(
        reqs.length,
        `expected ≤ 1 avatar-images request on cached list, saw ${reqs.length}: ${JSON.stringify(reqs)}`
      ).toBeLessThanOrEqual(1)
    } finally {
      await ctx.close()
    }
  })
})
