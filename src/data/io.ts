import { exportDB, importInto, type ExportOptions, type ImportOptions } from 'dexie-export-import'
import { db, schema } from 'src/utils/db'

export function exportData(options: ExportOptions = {}): Promise<Blob> {
  return exportDB(db, options)
}

export function importData(file: Blob, options: ImportOptions = {}): Promise<void> {
  return importInto(db, file, options) as unknown as Promise<void>
}

export { schema as exportSchema }
