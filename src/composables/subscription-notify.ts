import { until } from '@vueuse/core'
import { useQuasar } from 'quasar'
import { useUserDataStore } from 'src/stores/user-data'
import { authSource } from 'src/data'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

export function useSubscriptionNotify() {
  if (!authSource.enabled) return
  const user = authSource.user
  const router = useRouter()
  const $q = useQuasar()
  const store = useUserDataStore()
  const { data } = store
  const { t } = useI18n()
  function subscribe() {
    router.push('/account')
  }
  function notify() {
    if (!user.value?.isLoggedIn) return
    if (user.value.license?.type === 'eval') {
      if (user.value.license.evalDaysLeft <= 0) {
        if (data.evalExpiredNotified) return
        $q.notify({
          message: t('subscriptionNotify.evalExpired'),
          color: 'negative',
          actions: [{
            label: t('subscriptionNotify.ok'),
            handler: subscribe,
            textColor: 'on-err'
          }]
        })
        data.evalExpiredNotified = true
      } else if (user.value.license.evalDaysLeft <= 1) {
        $q.notify({
          message: t('subscriptionNotify.evalExpiring'),
          color: 'inv-sur',
          textColor: 'inv-on-sur',
          actions: [{
            label: t('subscriptionNotify.subscribe'),
            handler: subscribe,
            textColor: 'inv-pri'
          }]
        })
      }
    } else if (user.value.license?.type === 'prod') {
      const { validUntil } = user.value.license
      if (validUntil < new Date()) {
        if (data.prodExpiredNotifiedTimestamp === validUntil.getTime()) return
        $q.notify({
          message: t('subscriptionNotify.prodExpired'),
          color: 'negative',
          actions: [{
            label: t('subscriptionNotify.renewal'),
            handler: subscribe,
            textColor: 'on-err'
          }]
        })
        data.prodExpiredNotifiedTimestamp = validUntil.getTime()
      } else if (validUntil.getTime() - Date.now() <= 1000 * 60 * 60 * 24 * 2) {
        $q.notify({
          message: t('subscriptionNotify.prodExpiring'),
          color: 'inv-sur',
          textColor: 'inv-on-sur',
          actions: [{
            label: t('subscriptionNotify.renewal'),
            handler: subscribe,
            textColor: 'inv-pri'
          }]
        })
      }
    }
  }
  until(() => store.ready).toBeTruthy().then(notify)
}
