/**
 * Stage 4.5 / Step 8 — apply a `BootstrapResponse` to the local IDB cache.
 *
 * This is the single consumer of the bootstrap wire shape on the client.
 * For each table it mirrors the same decode logic the corresponding
 * `<table>.server.ts::pull()` already uses (envelope → row business
 * shape → `db.<table>.put`), so a row arriving via bootstrap looks
 * byte-identical in IDB to the same row arriving via per-table pull or
 * a realtime event. That uniformity is load-bearing — once the data is
 * in IDB, the rest of the app (stores / liveQuery) can't tell which
 * code path put it there.
 *
 * Why we don't go through the `<table>.server.ts` repos directly: each
 * repo's `pull()` is keyed on the per-table `lastVersion` cursor and
 * issues a fresh HTTP `GET /api/v1/<table>?since=N`. Bootstrap *is* the
 * bulk pull, so re-doing the GET would defeat its purpose. We replicate
 * the decode + write logic here (single function per table, ~5-15
 * lines each — plan-allowed duplication for the "apply once at boot"
 * path).
 *
 * Order of writes: independent across tables, but for clarity we apply
 * structural tables (workspaces, dialogs) first so that a UI mid-render
 * sees a parent before its children if a liveQuery happens to fire on
 * the same tick. messages last because their decode (contentsBlob ref)
 * may hit the network and we want the rest of the cache populated even
 * if a single message decode races.
 *
 * Failure isolation: a single decode throw inside one table doesn't
 * abort the whole bootstrap. Each table is wrapped in its own try /
 * console.warn so the rest of the apply still completes — same posture
 * as the per-table realtime handlers (they swallow + warn on a single
 * bad row rather than tearing down the subscription).
 */
import { db } from 'src/utils/db'
import type {
  Assistant,
  AvatarImage,
  CustomProvider,
  Dialog,
  Folder,
  InstalledPlugin,
  Message,
  MessageContent,
  StoredReactive,
  Workspace
} from 'src/utils/types'
import {
  materializeAttachment,
  type AttachmentEnvelope
} from './blob-client'
import type { BootstrapResponse, IdEnvelope, KvEnvelope } from './bootstrap-client'

// Wire-only fields that messages.server.ts knows about. Re-declared here
// (not imported) so this module doesn't pull in `messages.server.ts`'s
// realtime singletons just to read a field name.
interface WireMessage {
  id: string
  type: 'user' | 'assistant'
  assistantId?: string
  dialogId: string
  contents: MessageContent[]
  contentsBlob?: AttachmentEnvelope
  status: 'pending' | 'streaming' | 'failed' | 'default' | 'inputing'
  generatingSession?: string
  error?: string
  warnings?: string[]
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  usage?: any
  modelName?: string
}

interface WireAvatarImage {
  id: string
  contentBuffer: string
  mimeType: string
}

function base64ToBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

function decodeAvatarImage(wire: WireAvatarImage): AvatarImage {
  return {
    id: wire.id,
    contentBuffer: base64ToBuffer(wire.contentBuffer),
    mimeType: wire.mimeType
  }
}

async function decodeMessage(wire: WireMessage): Promise<Message> {
  let contents: MessageContent[] = wire.contents ?? []
  if (wire.contentsBlob) {
    try {
      const blob = await materializeAttachment(wire.contentsBlob)
      const text = await blob.text()
      contents = JSON.parse(text) as MessageContent[]
    } catch (err) {
      // Mirror messages.server.ts behaviour: don't crash the apply, leave
      // contents empty and let the UI re-fetch on demand.
      console.warn(
        '[bootstrap-apply] message contentsBlob materialize failed; ' +
          'row written with empty contents',
        err
      )
      contents = []
    }
  }
  return {
    id: wire.id,
    type: wire.type,
    assistantId: wire.assistantId,
    dialogId: wire.dialogId,
    contents,
    status: wire.status,
    generatingSession: wire.generatingSession,
    error: wire.error,
    warnings: wire.warnings,
    usage: wire.usage,
    modelName: wire.modelName
  }
}

// Generic id-PK applier: mirrors the loop body in
// providers.server.ts::pull / workspaces.server.ts::pull / etc.
async function applyIdRows<T>(
  envelopes: IdEnvelope[],
  putRow: (row: T) => Promise<unknown>,
  deleteRow: (id: string) => Promise<unknown>,
  decoder?: (data: unknown) => T
): Promise<void> {
  for (const env of envelopes) {
    if (env.deleted) {
      await deleteRow(env.id)
    } else if (env.data != null) {
      const row = decoder ? decoder(env.data) : (env.data as T)
      await putRow(row)
    }
  }
}

async function applyKvRows<T>(
  envelopes: KvEnvelope[],
  putRow: (row: T) => Promise<unknown>,
  deleteRow: (key: string) => Promise<unknown>,
  decoder: (env: KvEnvelope) => T | null
): Promise<void> {
  for (const env of envelopes) {
    if (env.deleted) {
      await deleteRow(env.key)
    } else {
      const row = decoder(env)
      if (row != null) await putRow(row)
    }
  }
}

export interface ApplyResult {
  // Counts per table — useful for the dev console / smoke spec assertion
  // ("bootstrap put N workspaces").
  workspaces: number
  dialogs: number
  providers: number
  assistants: number
  installed_plugins: number
  reactives: number
  avatar_images: number
  messages_recent: number
}

/**
 * Apply a successful bootstrap response. Runs sequentially per table
 * (not in parallel — each table's putRow is already async/I/O bound on
 * IDB and we want the order documented in the module header). Returns
 * counts so callers can log a single-line summary.
 */
export async function applyBootstrap(
  response: BootstrapResponse
): Promise<ApplyResult> {
  const counts: ApplyResult = {
    workspaces: 0,
    dialogs: 0,
    providers: 0,
    assistants: 0,
    installed_plugins: 0,
    reactives: 0,
    avatar_images: 0,
    messages_recent: 0
  }

  // workspaces — id-PK, raw `data` is `Workspace | Folder`.
  try {
    await applyIdRows<Workspace | Folder>(
      response.workspaces,
      (row) => db.workspaces.put(row),
      (id) => db.workspaces.delete(id)
    )
    counts.workspaces = response.workspaces.length
  } catch (err) {
    console.warn('[bootstrap-apply] workspaces apply failed', err)
  }

  // dialogs — id-PK, raw `data` is `Dialog`.
  try {
    await applyIdRows<Dialog>(
      response.dialogs,
      (row) => db.dialogs.put(row),
      (id) => db.dialogs.delete(id)
    )
    counts.dialogs = response.dialogs.length
  } catch (err) {
    console.warn('[bootstrap-apply] dialogs apply failed', err)
  }

  // providers — id-PK, raw `data` is `CustomProvider`.
  try {
    await applyIdRows<CustomProvider>(
      response.providers,
      (row) => db.providers.put(row),
      (id) => db.providers.delete(id)
    )
    counts.providers = response.providers.length
  } catch (err) {
    console.warn('[bootstrap-apply] providers apply failed', err)
  }

  // assistants — id-PK, raw `data` is `Assistant`.
  try {
    await applyIdRows<Assistant>(
      response.assistants,
      (row) => db.assistants.put(row),
      (id) => db.assistants.delete(id)
    )
    counts.assistants = response.assistants.length
  } catch (err) {
    console.warn('[bootstrap-apply] assistants apply failed', err)
  }

  // installed_plugins — KV, raw `data` IS the InstalledPlugin row (mirrors
  // installed-plugins.server.ts::pull, NOT reactives).
  try {
    await applyKvRows<InstalledPlugin>(
      response.installed_plugins,
      (row) => db.installedPluginsV2.put(row),
      (key) => db.installedPluginsV2.delete(key),
      (env) => (env.data as InstalledPlugin | null)
    )
    counts.installed_plugins = response.installed_plugins.length
  } catch (err) {
    console.warn('[bootstrap-apply] installed_plugins apply failed', err)
  }

  // reactives — KV, but `data` is the value blob, not a wrapped row;
  // IDB shape is `{key, value}` (mirrors reactives.server.ts::pull).
  try {
    await applyKvRows<StoredReactive>(
      response.reactives,
      (row) => db.reactives.put(row),
      (key) => db.reactives.delete(key),
      (env) => ({ key: env.key, value: env.data } as StoredReactive)
    )
    counts.reactives = response.reactives.length
  } catch (err) {
    console.warn('[bootstrap-apply] reactives apply failed', err)
  }

  // avatar_images — id-PK, but data is base64-encoded WireAvatarImage and
  // must be decoded to ArrayBuffer before IDB write (mirrors
  // avatar-images.server.ts::decodeFromWire).
  try {
    await applyIdRows<AvatarImage>(
      response.avatar_images,
      (row) => db.avatarImages.put(row),
      (id) => db.avatarImages.delete(id),
      (data) => decodeAvatarImage(data as WireAvatarImage)
    )
    counts.avatar_images = response.avatar_images.length
  } catch (err) {
    console.warn('[bootstrap-apply] avatar_images apply failed', err)
  }

  // messages_recent — id-PK, but each row may carry a contentsBlob ref
  // that needs network materialization. We do the full decode (not cache
  // fast-path) so rows arriving via bootstrap end up identical in IDB to
  // rows arriving via scoped-pull. Decode is async per row; we still
  // process sequentially to keep ref-mode fetches from stampeding.
  try {
    let written = 0
    for (const env of response.messages_recent) {
      if (env.deleted) {
        await db.messages.delete(env.id)
        continue
      }
      if (env.data == null) continue
      const decoded = await decodeMessage(env.data as WireMessage)
      await db.messages.put(decoded)
      written++
    }
    counts.messages_recent = written
  } catch (err) {
    console.warn('[bootstrap-apply] messages_recent apply failed', err)
  }

  return counts
}
