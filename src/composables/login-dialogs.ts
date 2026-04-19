import { Dialog, useQuasar } from 'quasar'
import { watch } from 'vue'
import { useI18n } from 'vue-i18n'

import LoginDialog from 'src/components/LoginDialog.vue'
import { isSyncEnabled, syncClient } from 'src/utils/sync-client'

export function useLoginDialogs() {
  if (!isSyncEnabled()) return
  const $q = useQuasar()
  const { t } = useI18n()
  let previouslyLoggedIn = syncClient.state.isLoggedIn
  watch(
    () => syncClient.state.isLoggedIn,
    next => {
      if (next && !previouslyLoggedIn) {
        $q.notify({ message: t('login.loggedIn', { email: syncClient.state.email }), color: 'positive' })
      }
      previouslyLoggedIn = next
    }
  )
}

export function openLoginDialog(initialMode: 'login' | 'register' = 'login') {
  return Dialog.create({
    component: LoginDialog,
    componentProps: { initialMode }
  })
}
