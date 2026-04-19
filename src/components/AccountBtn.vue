<template>
  <q-btn
    icon="sym_o_account_circle"
    @click="onClick"
    :class="{ 'route-active': $route.path === '/account' }"
    :label="state.isLoggedIn ? $t('accountBtn.account') : $t('accountBtn.login')"
  />
</template>

<script setup lang="ts">
import { useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import LoginDialog from 'src/components/LoginDialog.vue'
import { syncClient } from 'src/utils/sync-client'

const state = syncClient.state
const router = useRouter()
const $q = useQuasar()

function onClick() {
  if (state.isLoggedIn) {
    router.push('/account')
  } else {
    $q.dialog({ component: LoginDialog })
  }
}
</script>
