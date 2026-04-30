import { db } from 'src/utils/db'

export type TableName =
  | 'workspaces'
  | 'dialogs'
  | 'messages'
  | 'assistants'
  | 'artifacts'
  | 'installedPluginsV2'
  | 'reactives'
  | 'avatarImages'
  | 'items'
  | 'providers'

export function runTx<R>(scopes: TableName[], fn: () => Promise<R> | R): Promise<R> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tables = scopes.map(name => (db as any)[name])
  return db.transaction('rw', tables, fn)
}
