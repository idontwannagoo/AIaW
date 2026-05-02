import { boot } from 'quasar/wrappers'
import { db } from 'src/utils/db'
import { authSource } from 'src/data/auth'
import { repos } from 'src/data'
import * as blobClient from 'src/data/blob-client'

declare global {
  interface Window {
    __db__?: typeof db
    __authSource__?: typeof authSource
    __repos__?: typeof repos
    __blobClient__?: typeof blobClient
    __exposeDebugReady__?: true
  }
}

export default boot(() => {
  if (String(process.env.EXPOSE_DB) !== 'true') return
  if (typeof window === 'undefined') return
  window.__db__ = db
  window.__authSource__ = authSource
  window.__repos__ = repos
  window.__blobClient__ = blobClient
  window.__exposeDebugReady__ = true
  console.warn('[expose-debug] window.__db__ / __authSource__ / __repos__ / __blobClient__ exposed — test/dev only')
})
