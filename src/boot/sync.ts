import { boot } from 'quasar/wrappers'
import { syncClient } from 'src/utils/sync-client'

export default boot(async () => {
  try {
    await syncClient.init()
  } catch (err) {
    console.warn('[sync] init failed', err)
  }
})
