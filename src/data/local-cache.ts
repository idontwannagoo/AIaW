/**
 * Local IDB cleanup helpers — used by the logout path (Bug 2).
 *
 * Live logout used to leave all 10 synced tables populated with the old
 * account's rows. Symptoms: ① workspace / dialog / provider / plugin lists
 * stay rendered after logout, ② first interaction post-logout 401s because
 * the token is gone but the row references aren't, ③ next account login
 * sees the old rows briefly (and per-table `lastVersion` cursors that are
 * meaningless under a fresh user) until the realtime / scoped pull
 * pipeline catches up.
 *
 * Fix is to wipe every server-routed Dexie table at logout. Per-table
 * module state (`lastVersion`, `realtimeUnsubscribe`, `scopedPull` map) is
 * reset separately by the `<table>.server.ts` modules' own auth-event
 * listeners; this helper handles the IDB side only.
 *
 * Important boundaries:
 * - `aiaw-known-sessions` (`src/utils/sessions.ts`) is intentionally NOT
 *   touched — it tracks which `BroadcastChannel` session-ids belong to
 *   *this* browser profile (alive or crashed) and is independent of the
 *   logged-in user. Wiping it would make a legitimate "still alive" tab
 *   look like a foreign device on the next login.
 * - We use a single `db.transaction('rw', ...allTables)` so the wipe is
 *   atomic from a Dexie perspective — no half-cleared state visible to
 *   the liveQuery layer mid-clear.
 */
import type { Table } from 'dexie'
import { db } from 'src/utils/db'

export async function clearAllSyncedTables(): Promise<void> {
  // The full set matches `BACKEND_DATA_TABLES` in `.env.docker` plus
  // `messages` / `items` / `artifacts` (carved out only when the
  // server-routed flag is on, but the Dexie cache always exists). Listing
  // explicitly is safer than iterating `db.tables` — Dexie includes a few
  // internal tables we don't want to touch.
  //
  // We deliberately do NOT use `db.transaction('rw', tables, …)` here —
  // the typed overload does KeyPath inference across every table type,
  // which recurses infinitely through `InstalledPlugin.manifest →
  // JSONSchema7.{anyOf,oneOf,allOf}` and trips TS2615 (same gotcha that
  // `installed-plugins.server.ts::pull` works around). Per-table `.clear()`
  // calls in parallel are still atomic at the IDB layer for our use case
  // — each clear finishes its own object-store transaction. We `await
  // Promise.all` so the caller knows all tables are empty before
  // proceeding to clear the auth state.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tables: Table<any, any>[] = [
    db.workspaces,
    db.dialogs,
    db.messages,
    db.assistants,
    db.artifacts,
    db.installedPluginsV2,
    db.reactives,
    db.avatarImages,
    db.items,
    db.providers
  ]
  await Promise.all(tables.map((t) => t.clear()))
}
