<template>
  <q-layout view="lHr LpR lFf">
    <!--
      Stage 4.5 / Step 8 — bootstrap fallback banner.
      Only shows when the first-screen `GET /api/v1/bootstrap` failed /
      timed out and the legacy progressive load took over. Wrapped in
      q-header so q-layout's view="lHr ..." routes it above the drawer +
      content; dismiss button clears the session flag so subsequent
      navigations don't re-show it within the same browser tab.
    -->
    <q-header
      v-if="showBootstrapFallback"
      bordered
    >
      <q-banner
        inline-actions
        class="bg-warning text-white"
        dense
        data-test-id="bootstrap-fallback-banner"
      >
        {{ t('mainLayout.bootstrapFallback') }}
        <template #action>
          <q-btn
            flat
            dense
            icon="sym_o_close"
            :aria-label="t('mainLayout.bootstrapFallbackDismiss')"
            @click="dismissBootstrapFallback"
          />
        </template>
      </q-banner>
    </q-header>
    <q-drawer
      v-model="uiStore.mainDrawerOpen"
      show-if-above
      :width="locale.startsWith('zh') ? 250 : 270"
      :breakpoint="1200"
      bg-sur-c
      flex
      flex-col
    >
      <div
        text-xl
        px-4
        pt-4
      >
        <svg
          fill-on-sur-var
          h="24px"
          viewBox="0 0 636 86"
          cursor-pointer
          @click="notifyVersion"
        >
          <use
            xlink:href="/banner.svg#default"
          />
        </svg>
      </div>
      <q-separator spaced />
      <div
        px-4
        py-2
        text-sec
      >
        {{ t('mainLayout.workspace', 4) }}
      </div>
      <workspace-nav mt-2 />
      <q-list
        mt-a
        mb-2
      >
        <q-item
          clickable
          to="/assistants"
          active-class="route-active"
          item-rd
        >
          <q-item-section avatar>
            <q-icon name="sym_o_robot_2" />
          </q-item-section>
          <q-item-section>
            {{ t('mainLayout.assistants') }}
          </q-item-section>
        </q-item>
        <q-item
          clickable
          to="/plugins"
          active-class="route-active"
          item-rd
        >
          <q-item-section avatar>
            <q-icon name="sym_o_extension" />
          </q-item-section>
          <q-item-section>
            {{ t('mainLayout.plugins') }}
          </q-item-section>
        </q-item>
        <q-item
          clickable
          to="/settings"
          active-class="route-active"
          item-rd
        >
          <q-item-section avatar>
            <q-icon name="sym_o_settings" />
          </q-item-section>
          <q-item-section>
            {{ t('mainLayout.settings') }}
          </q-item-section>
        </q-item>
        <q-separator spaced />
        <div
          px-2
          flex
          text-on-sur-var
          items-center
        >
          <account-btn
            v-if="BackendAuth"
            flat
            no-caps
          />
          <q-btn
            v-else
            flat
            dense
            round
            icon="sym_o_book_2"
            :title="t('mainLayout.usageGuide')"
            href="https://docs.aiaw.app/usage/"
            target="_blank"
          />
          <q-space />
          <dark-switch-btn />
          <q-btn
            flat
            dense
            round
            icon="sym_o_more_vert"
          >
            <q-menu>
              <q-list>
                <menu-item
                  icon="sym_o_book_2"
                  :label="t('mainLayout.usageGuide')"
                  href="https://docs.aiaw.app/usage/"
                  target="_blank"
                />
                <q-item
                  clickable
                  v-close-popup
                  min-h-0
                  href="https://github.com/NitroRCr/AIaW"
                  target="_blank"
                >
                  <q-item-section
                    avatar
                    min-w-0
                  >
                    <q-avatar
                      icon="svguse:/svg/github.svg#icon"
                      size="20px"
                      font-size="20px"
                    />
                  </q-item-section>
                  <q-item-section>GitHub</q-item-section>
                </q-item>
                <menu-item
                  v-if="IsWeb"
                  icon="sym_o_download"
                  :label="t('mainLayout.localClient')"
                  href="https://github.com/NitroRCr/AIaW/releases/latest"
                  target="_blank"
                />
                <menu-item
                  v-else
                  icon="sym_o_web"
                  :label="t('mainLayout.webVersion')"
                  href="https://aiaw.app"
                  target="_blank"
                />
              </q-list>
            </q-menu>
          </q-btn>
        </div>
      </q-list>
    </q-drawer>
    <router-view />
  </q-layout>
</template>

<script setup>
import WorkspaceNav from 'src/components/WorkspaceNav.vue'
import { useUiStateStore } from 'src/stores/ui-state'
import { useRoute } from 'vue-router'
import AccountBtn from 'src/components/AccountBtn.vue'
import DarkSwitchBtn from 'src/components/DarkSwitchBtn.vue'
import MenuItem from 'src/components/MenuItem.vue'
import { BackendAuth } from 'src/utils/config'
import { useQuasar } from 'quasar'
import version from 'src/version.json'
import { useI18n } from 'vue-i18n'
import { ref, onMounted } from 'vue'
import { useOpenLastWorkspace } from 'src/composables/open-last-workspace'
import { IsWeb } from 'src/utils/platform-api'
import { bootstrapDidFallback, clearBootstrapFallback } from 'src/router'

defineOptions({
  name: 'MainLayout'
})

const uiStore = useUiStateStore()
const route = useRoute()

const { openLastWorkspace } = useOpenLastWorkspace()
route.path === '/' && openLastWorkspace()

// Stage 4.5 / Step 8 — bootstrap fallback banner state. The router guard
// sets a sessionStorage flag when `GET /api/v1/bootstrap` failed/timed
// out; we mirror that into a Vue ref on mount so the banner participates
// in the normal reactive render path. Dismiss = clear both the ref and
// the flag (subsequent navigations stay quiet for the rest of the tab
// session).
const showBootstrapFallback = ref(false)
onMounted(() => {
  if (bootstrapDidFallback()) showBootstrapFallback.value = true
})
function dismissBootstrapFallback() {
  showBootstrapFallback.value = false
  clearBootstrapFallback()
}

const { t, locale } = useI18n()
const $q = useQuasar()
function notifyVersion() {
  $q.notify({
    message: `${t('mainLayout.currentVersion')}: ${version.version}`,
    color: 'inv-sur',
    textColor: 'inv-on-sur',
    actions: [{
      label: t('mainLayout.changeLog'),
      handler: () => {
        window.open('https://github.com/NitroRCr/AIaW/releases', '_blank')
      },
      textColor: 'inv-pri'
    }]
  })
}
</script>
