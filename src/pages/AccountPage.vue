<template>
  <q-header
    bg-sur-c-low
    text-on-sur
  >
    <q-toolbar>
      <q-btn
        flat
        dense
        round
        icon="sym_o_menu"
        @click="uiStateStore.mainDrawerOpen = !uiStateStore.mainDrawerOpen"
      />
      <q-toolbar-title>
        {{ $t('accountPage.accountTitle') }}
      </q-toolbar-title>
    </q-toolbar>
  </q-header>
  <q-page-container>
    <q-page :style-fn="pageFhStyle">
      <q-list
        pb-2
        v-if="state.isLoggedIn"
        max-w="1000px"
        mx-a
      >
        <q-item-label header>
          {{ $t('accountPage.infoHeader') }}
        </q-item-label>
        <q-item>
          <q-item-section>
            {{ $t('accountPage.emailLabel') }}
          </q-item-section>
          <q-item-section side>
            {{ state.email }}
          </q-item-section>
        </q-item>
        <template v-if="LitellmBaseURL">
          <q-separator spaced />
          <q-item-label header>
            {{ $t('accountPage.modelServicesHeader') }}
          </q-item-label>
          <q-item>
            <q-item-section>
              <q-item-label
                caption
                important:lh="1.5em"
              >
                {{ $t('accountPage.modelServicesDescription') }}
                <router-link
                  to="/model-pricing"
                  pri-link
                >
                  {{ $t('accountPage.modelPricingLink') }}
                </router-link>
              </q-item-label>
            </q-item-section>
          </q-item>
        </template>
        <q-separator spaced />
        <q-item
          clickable
          v-ripple
          @click="logout"
        >
          <q-item-section avatar>
            <q-icon name="sym_o_logout" />
          </q-item-section>
          <q-item-section>
            {{ $t('accountPage.logoutButton') }}
          </q-item-section>
        </q-item>
      </q-list>
    </q-page>
  </q-page-container>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useQuasar } from 'quasar'
import { useRouter } from 'vue-router'
import { useUiStateStore } from 'src/stores/ui-state'
import LoginDialog from 'src/components/LoginDialog.vue'
import { syncClient } from 'src/utils/sync-client'
import { LitellmBaseURL } from 'src/utils/config'
import { pageFhStyle } from 'src/utils/functions'

const state = syncClient.state
const router = useRouter()
const $q = useQuasar()
const uiStateStore = useUiStateStore()

onMounted(() => {
  if (!state.isLoggedIn) {
    router.replace('/')
    $q.dialog({ component: LoginDialog })
  }
})

async function logout() {
  await syncClient.logout()
  router.replace('/')
}
</script>
