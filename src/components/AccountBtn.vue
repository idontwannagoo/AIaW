<template>
  <q-btn
    icon="sym_o_account_circle"
    @click="onClick"
    :class="{ 'route-active': $route.path === '/account' }"
    :label="user?.isLoggedIn ? $t('accountBtn.account') : $t('accountBtn.login')"
  />
</template>

<script setup lang="ts">
import { authSource } from 'src/data'
import { useRouter } from 'vue-router'

const user = authSource.user

const router = useRouter()
function onClick() {
  if (user.value?.isLoggedIn) {
    router.push('/account')
  } else {
    authSource.login()
  }
}
</script>

<style scoped>

</style>
