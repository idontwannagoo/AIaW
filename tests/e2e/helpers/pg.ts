// Direct Postgres assertions — for cases where "what does the server actually
// have?" is the bar (Stage 4 cascade, deleted_at vs returned tombstone, etc.).
//
// Pool is created lazily and reused for the whole test run; closeAll() should
// be called from globalTeardown if we add one (currently we leave the proc
// to clean up pgPool via process exit since the test run is short-lived).
import pg from 'pg'
import { PG_DSN } from './env'

let _pool: pg.Pool | null = null

export function pgPool(): pg.Pool {
  if (_pool) return _pool
  _pool = new pg.Pool({ connectionString: PG_DSN, max: 4, idleTimeoutMillis: 5_000 })
  return _pool
}

export async function pgQuery<T extends pg.QueryResultRow = pg.QueryResultRow>(
  sql: string,
  params: unknown[] = []
): Promise<T[]> {
  const r = await pgPool().query<T>(sql, params as unknown[])
  return r.rows
}

// Helper allow-list: only call with table names hard-coded in test code.
// SQL parameters are still parameterized; the table name is interpolated.
const TABLE_RE = /^[a-z_][a-z0-9_]*$/i
function assertTable(t: string): void {
  if (!TABLE_RE.test(t)) throw new Error(`pg helper: bad table name ${JSON.stringify(t)}`)
}

export async function expectRowExists(
  table: string,
  id: string,
  userId: string
): Promise<boolean> {
  assertTable(table)
  const r = await pgQuery(
    `SELECT 1 FROM ${table} WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL`,
    [id, userId]
  )
  return r.length > 0
}

export async function countByUser(
  table: string,
  userId: string,
  includeDeleted = false
): Promise<number> {
  assertTable(table)
  const where = includeDeleted
    ? 'WHERE user_id = $1'
    : 'WHERE user_id = $1 AND deleted_at IS NULL'
  const r = await pgQuery<{ c: string }>(
    `SELECT count(*)::text AS c FROM ${table} ${where}`,
    [userId]
  )
  return parseInt(r[0].c, 10)
}

export async function pgClose(): Promise<void> {
  if (_pool) {
    await _pool.end()
    _pool = null
  }
}
