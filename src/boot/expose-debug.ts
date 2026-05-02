import { boot } from 'quasar/wrappers'
import { db } from 'src/utils/db'
import { authSource, dexieAuthSource } from 'src/data/auth'
import { repos } from 'src/data'

declare global {
  interface Window {
    __db__?: typeof db
    __authSource__?: typeof authSource
    __dexieAuthSource__?: typeof dexieAuthSource
    __repos__?: typeof repos
    __exposeDebugReady__?: true
  }
}

export default boot(() => {
  if (String(process.env.EXPOSE_DB) !== 'true') return
  if (typeof window === 'undefined') return
  window.__db__ = db
  window.__authSource__ = authSource
  window.__dexieAuthSource__ = dexieAuthSource
  window.__repos__ = repos
  window.__exposeDebugReady__ = true
  console.warn('[expose-debug] window.__db__ / __authSource__ / __repos__ exposed — test/dev only')
})
