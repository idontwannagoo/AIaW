// syncRef debounce + suppress-source-during-edit guarantees.
//
// Why this lives in stage4_pre: the bug is structural across every
// server-routed table that backs an editable form. CustomProvider 是用户
// 实际报告的"输入名字时字会被吃掉"现场，但 AssistantView / WorkspaceSettings /
// PluginAdjust / EnablePluginsItems 全部用同一个 syncRef helper，修一处覆盖
// 全部。这条 spec 直接打 syncRef 行为契约，避免被特定页面 selector 漂移
// 牵动。
//
// Echo race recap: input v-model → syncRef(val) change → set() → store.put
// → http.put → server returns row → server.ts puts row.data into IDB →
// liveQuery → source watch in syncRef → val.value = newVal → input shows
// the *lagged* server echo, overwriting whatever the user has typed in
// the meantime. Network round-trip is hundreds of ms; rapid typing
// guarantees a stale echo per keystroke.
//
// What syncRef now guarantees:
//  1. debounceMs (default 200): N rapid val edits → ≤ 1 set() call
//  2. suppressSourceWhileEditingMs (default 1500): a source push that
//     arrives within Nms of the last local edit is *dropped*, so the
//     in-flight stale echo can't clobber the user's text.
//  3. After the suppression window passes with no further edits, source
//     pushes apply normally (genuine cross-device remote updates).
//  4. opt-out (debounceMs:0, suppressMs:0) preserves the legacy
//     immediate-write / immediate-source-apply semantics for callers that
//     need it (boolean toggles etc).

import { test, expect, type Page } from '@playwright/test'
import { exposeReady } from '../helpers/db'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyWindow = any

async function bootHarnessPage(page: Page): Promise<void> {
  page.on('pageerror', e => console.log('[page-error]', e.message))
  await page.goto('/')
  await exposeReady(page)
  await page.waitForFunction(
    () => typeof (window as AnyWindow).__syncRefHarness__ === 'function',
    undefined,
    { timeout: 10_000 }
  )
}

test.describe('stage4_pre syncRef anti-flicker', () => {
  test('debounce coalesces rapid edits into one set() call', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootHarnessPage(page)

    const result = await page.evaluate(async () => {
      const make = (window as AnyWindow).__syncRefHarness__
      const h = make('', { debounceMs: 100, suppressSourceWhileEditingMs: 1500 })
      // Simulate 6 keystrokes 30ms apart (200ms total) — debounce timer
      // resets each keystroke, so set() fires once 100ms after the last.
      const chars = ['O', 'p', 'e', 'n', 'A', 'I']
      let acc = ''
      for (const c of chars) {
        acc += c
        h.value = acc
        await new Promise(resolve => setTimeout(resolve, 30))
      }
      // Wait long enough for the debounce flush plus padding.
      await new Promise(resolve => setTimeout(resolve, 200))
      const setCallCount = h.setCallCount()
      const lastSet = h.setCalls[h.setCalls.length - 1]
      const finalValue = h.value
      h.cleanup()
      return { setCallCount, lastSet, finalValue }
    })

    expect(result.setCallCount).toBe(1)
    expect(result.lastSet).toBe('OpenAI')
    expect(result.finalValue).toBe('OpenAI')
  })

  test('source push during edit window is dropped (echo suppression)', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootHarnessPage(page)

    const result = await page.evaluate(async () => {
      const make = (window as AnyWindow).__syncRefHarness__
      const h = make('', { debounceMs: 50, suppressSourceWhileEditingMs: 1000 })
      // Simulate user typing "OpenAI"
      let acc = ''
      for (const c of ['O', 'p', 'e', 'n', 'A', 'I']) {
        acc += c
        h.value = acc
        await new Promise(resolve => setTimeout(resolve, 20))
      }
      // Wait for debounce flush
      await new Promise(resolve => setTimeout(resolve, 80))
      // Now simulate a delayed echo from server pushing the *first*
      // keystroke's value back — this is the bug scenario where a slow
      // round-trip echo arrives mid-typing.
      h.pushSource('O')
      // Tiny delay so the watch handler runs (Vue schedules watchers async).
      await new Promise(resolve => setTimeout(resolve, 10))
      const valAfterEcho = h.value
      h.cleanup()
      return { valAfterEcho }
    })

    expect(result.valAfterEcho).toBe('OpenAI')
  })

  test('source push after suppression window applies normally', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootHarnessPage(page)

    const result = await page.evaluate(async () => {
      const make = (window as AnyWindow).__syncRefHarness__
      const h = make('initial', { debounceMs: 50, suppressSourceWhileEditingMs: 200 })
      // Edit locally
      h.value = 'local-edit'
      await new Promise(resolve => setTimeout(resolve, 100)) // debounce flush
      // Wait past the suppression window
      await new Promise(resolve => setTimeout(resolve, 250))
      // A genuine remote update arrives — should apply
      h.pushSource('from-other-device')
      await new Promise(resolve => setTimeout(resolve, 10))
      const valAfter = h.value
      h.cleanup()
      return { valAfter }
    })

    expect(result.valAfter).toBe('from-other-device')
  })

  test('legacy mode (debounceMs:0, suppressMs:0) preserves immediate-apply', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootHarnessPage(page)

    const result = await page.evaluate(async () => {
      const make = (window as AnyWindow).__syncRefHarness__
      const h = make('', { debounceMs: 0, suppressSourceWhileEditingMs: 0 })
      // Two rapid edits should both fire set() immediately.
      h.value = 'A'
      await new Promise(resolve => setTimeout(resolve, 5))
      h.value = 'AB'
      await new Promise(resolve => setTimeout(resolve, 5))
      const callsAfterEdits = h.setCallCount()
      // Source push should apply immediately, no suppression.
      h.pushSource('remote')
      await new Promise(resolve => setTimeout(resolve, 10))
      const valAfter = h.value
      h.cleanup()
      return { callsAfterEdits, valAfter }
    })

    expect(result.callsAfterEdits).toBe(2)
    expect(result.valAfter).toBe('remote')
  })

  test('rapid edit + concurrent source echo: input survives, latest val flushed', async ({ page }, testInfo) => {
    // The high-fidelity bug repro: typing while echoes are interleaving.
    test.skip(testInfo.project.name !== 'providers-rest', 'providers-rest profile only')
    await bootHarnessPage(page)

    const result = await page.evaluate(async () => {
      const make = (window as AnyWindow).__syncRefHarness__
      const h = make('', { debounceMs: 80, suppressSourceWhileEditingMs: 1000 })
      const chars = ['O', 'p', 'e', 'n', 'A', 'I', ' ', 't', 'e', 's', 't']
      let acc = ''
      // Interleave each keystroke with a delayed source echo of an earlier
      // value (simulating server returning round-tripped writes).
      for (let i = 0; i < chars.length; i++) {
        acc += chars[i]
        h.value = acc
        // Schedule a stale echo to land 50ms later (still inside suppression).
        const stale = acc.slice(0, Math.max(1, i)) // strictly older
        setTimeout(() => h.pushSource(stale), 50)
        await new Promise(resolve => setTimeout(resolve, 30))
      }
      // Drain pending echoes + final debounce flush
      await new Promise(resolve => setTimeout(resolve, 200))
      const finalValue = h.value
      const lastSet = h.setCalls[h.setCalls.length - 1]
      h.cleanup()
      return { finalValue, lastSet, setCallCount: h.setCallCount() }
    })

    expect(result.finalValue).toBe('OpenAI test')
    expect(result.lastSet).toBe('OpenAI test')
    // Debounce must have collapsed at least 11 keystrokes to << 11 calls.
    expect(result.setCallCount).toBeLessThan(6)
  })
})
