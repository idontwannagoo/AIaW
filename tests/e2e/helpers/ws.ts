// WS frame capture. Buffers all received frames so specs can assert on the
// stream without racing the realtime layer.
import type { Page, WebSocket } from '@playwright/test'

export interface WsFrame {
  payload: string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  parsed?: any
  receivedAt: number
}

export interface WsCapture {
  readonly frames: WsFrame[]
  readonly ws: WebSocket | null
  waitForFrame(
    predicate: (f: WsFrame) => boolean,
    timeoutMs?: number
  ): Promise<WsFrame>
  expectFrame(predicate: (f: WsFrame) => boolean): WsFrame
}

export function captureWs(page: Page): WsCapture {
  const state = {
    frames: [] as WsFrame[],
    ws: null as WebSocket | null
  }
  page.on('websocket', (ws) => {
    state.ws = ws
    ws.on('framereceived', (event) => {
      const payload =
        typeof event.payload === 'string'
          ? event.payload
          : event.payload.toString('utf-8')
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let parsed: any
      try { parsed = JSON.parse(payload) } catch { /* binary or non-json */ }
      state.frames.push({ payload, parsed, receivedAt: Date.now() })
    })
  })
  return {
    get frames() { return state.frames },
    get ws() { return state.ws },
    async waitForFrame(predicate, timeoutMs = 5_000) {
      const start = Date.now()
      while (Date.now() - start < timeoutMs) {
        const hit = state.frames.find(predicate)
        if (hit) return hit
        await new Promise(resolve => setTimeout(resolve, 50))
      }
      throw new Error(
        `waitForFrame timed out after ${timeoutMs}ms (saw ${state.frames.length} frames)`
      )
    },
    expectFrame(predicate) {
      const hit = state.frames.find(predicate)
      if (!hit) {
        throw new Error(
          `expectFrame: no frame matched (had ${state.frames.length} frames)`
        )
      }
      return hit
    }
  }
}
