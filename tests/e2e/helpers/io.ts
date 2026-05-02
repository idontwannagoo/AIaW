// Export / import helpers around the existing ExportDataDialog UI. Phase 4
// keeps the implementation minimal — Stage 5 spec is what really exercises
// these. The selector strategy intentionally falls back to button text;
// hardening to data-testid happens in Phase 5+ when we wire those into the
// Vue components.
import path from 'node:path'
import fs from 'node:fs'
import type { Page } from '@playwright/test'

const RESULTS_DIR = path.resolve('tests/.results')

export async function exportData(page: Page): Promise<string> {
  if (!fs.existsSync(RESULTS_DIR)) fs.mkdirSync(RESULTS_DIR, { recursive: true })
  const downloadPromise = page.waitForEvent('download', { timeout: 30_000 })
  const trigger = page
    .getByTestId('export-data-trigger')
    .or(page.getByRole('button', { name: /export data|导出数据/i }))
  await trigger.click()
  const download = await downloadPromise
  const out = path.join(
    RESULTS_DIR,
    `export-${Date.now()}-${download.suggestedFilename()}`
  )
  await download.saveAs(out)
  return out
}

export async function importData(page: Page, filePath: string): Promise<void> {
  if (!fs.existsSync(filePath)) {
    throw new Error(`importData: file not found: ${filePath}`)
  }
  const trigger = page
    .getByTestId('import-data-trigger')
    .or(page.getByRole('button', { name: /import data|导入数据/i }))
  await trigger.click()
  const input = page.locator('input[type="file"]').first()
  await input.setInputFiles(filePath)
}
