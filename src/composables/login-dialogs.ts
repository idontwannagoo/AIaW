import { useQuasar, type QVueGlobals } from 'quasar'
import { authSource } from 'src/data'
import type { AuthSource } from 'src/data'
import { watch } from 'vue'
import { dialogOptions } from 'src/utils/values'
import { useI18n } from 'vue-i18n'

function bindSource(source: AuthSource, $q: QVueGlobals, t: (k: string, p?: Record<string, unknown>) => string) {
  if (!source.enabled) return
  const userInteraction = source.userInteraction
  const user = source.user
  let loginNotify = false

  watch(userInteraction, interaction => {
    if (!interaction) return
    if (interaction.type === 'email') {
      $q.dialog({
        title: t('login.register'),
        message: t('login.privacyPolicy'),
        html: true,
        prompt: {
          model: '',
          type: 'email',
          label: 'Email'
        },
        cancel: true,
        ok: t('login.next'),
        noRouteDismiss: true,
        ...dialogOptions
      }).onOk(email => {
        interaction.onSubmit({ email })
      })
    } else if (interaction.type === 'otp') {
      $q.dialog({
        title: t('login.otp'),
        message: t('login.enterOtp'),
        prompt: {
          model: '',
          type: 'text',
          label: t('login.otp')
        },
        cancel: true,
        persistent: true,
        noRouteDismiss: true,
        ...dialogOptions
      }).onOk(otp => {
        interaction.onSubmit({ otp })
        loginNotify = true
      }).onCancel(() => {
        interaction.onCancel()
      })
    } else if (interaction.type === 'logout-confirmation') {
      $q.dialog({
        title: t('login.logout'),
        message: t('login.confirmLogout'),
        cancel: true,
        ok: t('login.logout'),
        persistent: true,
        ...dialogOptions
      }).onOk(() => {
        interaction.onSubmit({})
      }).onCancel(() => {
        interaction.onCancel()
      })
    } else if (interaction.type === 'message-alert') {
      for (const alert of interaction.alerts) {
        $q.notify({
          message: alert.message,
          color: alert.type === 'info' ? undefined : 'negative'
        })
      }
    }
  })
  watch(() => user.value?.isLoggedIn, isLoggedIn => {
    isLoggedIn && loginNotify && $q.notify(t('login.loggedIn', { email: user.value.email }))
    loginNotify = false
  })
}

export function useLoginDialogs() {
  const $q = useQuasar()
  const { t } = useI18n()
  bindSource(authSource, $q, t)
}
