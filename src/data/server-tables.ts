// Stage 1+ allowlist of tables that have a backend `Repository` implementation.
// Used by repositories/index.ts to gate the BACKEND_DATA_TABLES env so a typo
// can't silently bypass Dexie, and by src/utils/db.ts to compute the set of
// tables to exclude from dexie-cloud-addon's sync hooks (`unsyncedTables`).
//
// Stage 3+ adds tables here as their server.ts repositories land.
export const SERVER_CAPABLE_TABLES = new Set<string>(['providers'])
