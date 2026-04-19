import { db } from './db'
import { isSyncEnabled, syncClient } from './sync-client'

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  const bytes = new Uint8Array(digest)
  let out = ''
  for (const b of bytes) out += b.toString(16).padStart(2, '0')
  return out
}

const urlCache = new Map<string, { url: string; expires: number }>()
const URL_TTL_MS = 50 * 60 * 1000

export async function uploadBinary(
  buffer: ArrayBuffer,
  mimeType: string
): Promise<{ key: string; size: number }> {
  if (!isSyncEnabled() || !syncClient.state.isLoggedIn) {
    return { key: '', size: buffer.byteLength }
  }
  const key = await sha256Hex(buffer)
  const signed = await syncClient.signPut(key, buffer.byteLength, mimeType)
  const resp = await fetch(signed.url, {
    method: 'PUT',
    headers: signed.headers,
    body: buffer
  })
  if (!resp.ok) throw new Error(`upload failed: ${resp.status}`)
  return { key, size: buffer.byteLength }
}

export async function downloadBinary(key: string): Promise<ArrayBuffer> {
  const url = await signedGetUrl(key)
  const resp = await fetch(url)
  if (!resp.ok) throw new Error(`download failed: ${resp.status}`)
  return await resp.arrayBuffer()
}

export async function signedGetUrl(key: string): Promise<string> {
  const cached = urlCache.get(key)
  if (cached && cached.expires > Date.now()) return cached.url
  const url = await syncClient.signGet(key)
  urlCache.set(key, { url, expires: Date.now() + URL_TTL_MS })
  return url
}

/**
 * Ensures a local row of `items` / `avatarImages` has its binary material.
 * Strategy: return contentBuffer if present; else fetch from server via fileKey,
 * cache back into the local row.
 */
export async function ensureLocalBuffer(
  table: 'items' | 'avatarImages',
  id: string
): Promise<ArrayBuffer | null> {
  const dex = db.table(table)
  const row = await dex.get(id) as { contentBuffer?: ArrayBuffer; fileKey?: string } | undefined
  if (!row) return null
  if (row.contentBuffer) return row.contentBuffer
  if (!row.fileKey) return null
  const buffer = await downloadBinary(row.fileKey)
  await dex.update(id, { contentBuffer: buffer })
  return buffer
}
