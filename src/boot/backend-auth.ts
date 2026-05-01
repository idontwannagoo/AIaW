import { boot } from 'quasar/wrappers'
import { Dialog } from 'quasar'
import BackendLoginDialog from 'src/components/BackendLoginDialog.vue'
import { registerBackendLoginDialog } from 'src/data/auth'

export default boot(() => {
  // Bridge BackendAuthSource.login() → Quasar Dialog. Done in a boot file so
  // the AuthSource module stays decoupled from Vue/Quasar runtime concerns.
  registerBackendLoginDialog(() => new Promise((resolve, reject) => {
    Dialog.create({ component: BackendLoginDialog })
      .onOk((pair: unknown) => resolve(pair as never))
      .onCancel(() => reject(new Error('login cancelled')))
  }))
})
