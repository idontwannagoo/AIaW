// Stage 4 硬前置 2 — frontend `blob-client.ts` browser-side smoke.
//
// What this proves:
//  - putBlob() runs in a real Chrome context with FormData multipart upload
//    + Bearer auth + dedup behavior matches the API tests.
//  - fetchBlob() can pull bytes via the presigned URL with no auth header.
//  - serializeAttachment() picks inline below 64KB, ref at/above.
//  - materializeAttachment() round-trips both branches back to a Blob with
//    the right content_type.
//
// Profiles:
//  - providers-rest: BACKEND_DATA_API_URL + BACKEND_AUTH set, no realtime.
//    The `/api/v1/blobs` endpoint doesn't depend on realtime so this profile
//    is sufficient — adding realtime profiles would just spin extra builds.
//  - baseline: BACKEND_DATA_API_URL absent → `putBlob` must throw the
//    "called without BACKEND_DATA_API_URL configured" guard.
//
// Why no end-to-end "row stores ref envelope" case here: that's Stage 4 主体
// 批次 (artifacts / messages). This spec keeps to the helper layer alone so
// regressions in the wire format show up as a focused signal.

import { test, expect, type Page } from '@playwright/test'
import { exposeReady } from '../helpers/db'
import { registerViaApi, injectAuth } from '../helpers/auth'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyWindow = any

function uniqEmail(): string {
  return `stage4pre-blob-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`
}

async function bootAuthedPage(page: Page): Promise<void> {
  const reg = await registerViaApi(uniqEmail())
  await injectAuth(page, reg)
  page.on('pageerror', e => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  await page.waitForFunction(
    () => {
      const auth = (window as AnyWindow).__authSource__
      return !!(auth && auth.currentToken && auth.currentToken())
    },
    undefined,
    { timeout: 10_000 }
  )
}

test.describe('stage4_pre blob-client', () => {
  test('putBlob round-trips small bytes + signed URL fetch works', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootAuthedPage(page)

    const result = await page.evaluate(async () => {
      const bc = (window as AnyWindow).__blobClient__
      const buf = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4, 5])
      const ref = await bc.putBlob(buf, 'image/png')
      const blob = await bc.fetchBlob(ref)
      const ab = await blob.arrayBuffer()
      return {
        ref,
        bytesLength: ab.byteLength,
        firstByte: new Uint8Array(ab)[0],
        lastByte: new Uint8Array(ab)[ab.byteLength - 1],
        materializedType: blob.type
      }
    })
    expect(result.ref.type).toBe('ref')
    expect(typeof result.ref.sha256).toBe('string')
    expect(result.ref.sha256.length).toBe(64)
    expect(result.ref.size).toBe(13)
    expect(result.ref.content_type).toBe('image/png')
    expect(result.ref.url).toMatch(/\/api\/v1\/blobs\/[0-9a-f]{64}\/data\?exp=\d+&sig=[0-9a-f]+/)
    expect(result.bytesLength).toBe(13)
    expect(result.firstByte).toBe(0x89)
    expect(result.lastByte).toBe(5)
    expect(result.materializedType).toBe('image/png')
  })

  test('putBlob dedup: same bytes → same sha256', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootAuthedPage(page)
    const out = await page.evaluate(async () => {
      const bc = (window as AnyWindow).__blobClient__
      const data = new TextEncoder().encode('dedup-from-browser-' + Math.random())
      const ref1 = await bc.putBlob(data, 'text/plain')
      const ref2 = await bc.putBlob(data, 'text/plain')
      return { sha1: ref1.sha256, sha2: ref2.sha256 }
    })
    expect(out.sha1).toBe(out.sha2)
  })

  test('serializeAttachment: <64KB → inline, >=64KB → ref', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootAuthedPage(page)
    const out = await page.evaluate(async () => {
      const bc = (window as AnyWindow).__blobClient__
      const small = new Uint8Array(1024)
      const justUnder = new Uint8Array(64 * 1024 - 1)
      const exact = new Uint8Array(64 * 1024)
      const big = new Uint8Array(64 * 1024 + 100)
      small.fill(0xAA)
      justUnder.fill(0xBB)
      exact.fill(0xCC)
      big.fill(0xDD)
      const a = await bc.serializeAttachment(small, 'application/octet-stream')
      const b = await bc.serializeAttachment(justUnder, 'application/octet-stream')
      const c = await bc.serializeAttachment(exact, 'application/octet-stream')
      const d = await bc.serializeAttachment(big, 'application/octet-stream')
      return {
        a: a.type,
        b: b.type,
        c: c.type,
        d: d.type,
        aSize: a.size,
        bSize: b.size,
        cSize: c.size,
        dSize: d.size,
        cSha: (c as { sha256?: string }).sha256
      }
    })
    expect(out.a).toBe('inline')
    expect(out.b).toBe('inline')
    expect(out.c).toBe('ref')
    expect(out.d).toBe('ref')
    expect(out.aSize).toBe(1024)
    expect(out.bSize).toBe(64 * 1024 - 1)
    expect(out.cSize).toBe(64 * 1024)
    expect(out.dSize).toBe(64 * 1024 + 100)
    expect(out.cSha).toMatch(/^[0-9a-f]{64}$/)
  })

  test('materializeAttachment: inline → Blob round-trip', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootAuthedPage(page)
    const out = await page.evaluate(async () => {
      const bc = (window as AnyWindow).__blobClient__
      const original = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
      const env = await bc.serializeAttachment(original, 'application/octet-stream')
      const blob = await bc.materializeAttachment(env)
      const ab = await blob.arrayBuffer()
      const rt = new Uint8Array(ab)
      return {
        type: env.type,
        eq: rt.length === original.length && rt.every((v, i) => v === original[i]),
        contentType: blob.type
      }
    })
    expect(out.type).toBe('inline')
    expect(out.eq).toBe(true)
    expect(out.contentType).toBe('application/octet-stream')
  })

  test('materializeAttachment: ref → fetch via signed URL', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootAuthedPage(page)
    const out = await page.evaluate(async () => {
      const bc = (window as AnyWindow).__blobClient__
      const bytes = new Uint8Array(100 * 1024)
      bytes.fill(0x77)
      const env = await bc.serializeAttachment(bytes, 'application/zip')
      const blob = await bc.materializeAttachment(env)
      const ab = await blob.arrayBuffer()
      const arr = new Uint8Array(ab)
      return {
        type: env.type,
        size: arr.length,
        sha256: (env as { sha256?: string }).sha256,
        firstByte: arr[0],
        lastByte: arr[arr.length - 1],
        contentType: blob.type,
        allEq: arr.every(v => v === 0x77)
      }
    })
    expect(out.type).toBe('ref')
    expect(out.size).toBe(100 * 1024)
    expect(out.sha256).toMatch(/^[0-9a-f]{64}$/)
    expect(out.allEq).toBe(true)
    expect(out.contentType).toBe('application/zip')
  })

  test('baseline: putBlob throws when BACKEND_DATA_API_URL is not configured', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'baseline', 'baseline profile only')
    await page.goto('/')
    await exposeReady(page)
    const errMsg = await page.evaluate(async () => {
      const bc = (window as AnyWindow).__blobClient__
      try {
        await bc.putBlob(new Uint8Array([1, 2, 3]), 'application/octet-stream')
        return null
      } catch (e) {
        return (e as Error).message
      }
    })
    expect(errMsg).not.toBeNull()
    expect(errMsg).toMatch(/BACKEND_DATA_API_URL/)
  })
})
