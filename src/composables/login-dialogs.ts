import { useQuasar, type QVueGlobals } from 'quasar'
import { authSource, dexieAuthSource } from 'src/data'
import type { AuthSource } from 'src/data'
import { watch } from 'vue'
import { dialogOptions } from 'src/utils/values'
import { useI18n } from 'vue-i18n'
import { BackendAuth } from 'src/utils/config'

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
  // Stage 1.5 双写窗口：BACKEND_AUTH=true 时主 authSource 是 backend，
  // 但 dexieAuthSource 仍可独立调用（AccountPage 的"原 Dexie 账号"入口），
  // 需要并行绑定其 userInteraction 才能弹出 email/OTP 对话框。
  if (BackendAuth && dexieAuthSource !== authSource) {
    bindSource(dexieAuthSource, $q, t)
  }
}
