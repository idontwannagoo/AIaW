// Multi-tab / multi-user context factories. Stage 2-4 sync specs need
// "user A in tab1, tab2" and "A vs B" patterns; centralizing here keeps spec
// bodies short.
import type { Browser, BrowserContext, Page } from '@playwright/test'

export interface TabBundle {
  context: BrowserContext
  pages: Page[]
}

export async function openTabsForUser(
  browser: Browser,
  n: number
): Promise<TabBundle> {
  const context = await browser.newContext()
  const pages: Page[] = []
  for (let i = 0; i < n; i++) pages.push(await context.newPage())
  return { context, pages }
}

export async function openContextsForUsers(
  browser: Browser,
  count: number
): Promise<BrowserContext[]> {
  const ctxs: BrowserContext[] = []
  for (let i = 0; i < count; i++) ctxs.push(await browser.newContext())
  return ctxs
}

export async function closeBundle(b: TabBundle): Promise<void> {
  await b.context.close()
}
