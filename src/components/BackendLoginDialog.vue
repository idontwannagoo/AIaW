<template>
  <q-dialog
    ref="dialogRef"
    persistent
    @hide="onDialogHide"
  >
    <q-card
      min-w="320px"
      max-w="400px"
    >
      <q-card-section>
        <div class="text-h6">
          {{ mode === 'login' ? t('backendLoginDialog.titleLogin') : t('backendLoginDialog.titleRegister') }}
        </div>
      </q-card-section>
      <q-card-section py-0>
        <q-input
          v-model="email"
          :label="t('backendLoginDialog.email')"
          type="email"
          autofocus
          :error="!!error"
          :error-message="error"
          @update:model-value="error = ''"
        />
        <q-input
          v-model="password"
          :label="t('backendLoginDialog.password')"
          :type="showPwd ? 'text' : 'password'"
          @keyup.enter="submit"
        >
          <template #append>
            <q-icon
              :name="showPwd ? 'sym_o_visibility_off' : 'sym_o_visibility'"
              class="cursor-pointer"
              @click="showPwd = !showPwd"
            />
          </template>
        </q-input>
        <q-input
          v-if="mode === 'register'"
          v-model="inviteCode"
          :label="t('backendLoginDialog.inviteCode')"
          :hint="t('backendLoginDialog.inviteCodeHint')"
          @keyup.enter="submit"
        />
      </q-card-section>
      <q-card-actions align="between">
        <q-btn
          flat
          dense
          color="primary"
          :label="mode === 'login' ? t('backendLoginDialog.switchToRegister') : t('backendLoginDialog.switchToLogin')"
          @click="toggleMode"
        />
        <div>
          <q-btn
            flat
            color="primary"
            :label="t('backendLoginDialog.cancel')"
            @click="onDialogCancel"
          />
          <q-btn
            color="primary"
            :loading="loading"
            :disable="!email || !password"
            :label="mode === 'login' ? t('backendLoginDialog.login') : t('backendLoginDialog.register')"
            @click="submit"
          />
        </div>
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useDialogPluginComponent } from 'quasar'
import { useI18n } from 'vue-i18n'
import { BackendApiBaseURL } from 'src/utils/config'

interface BackendUser {
  id: string
  email: string
  status: string
  linked_dexie_email?: string | null
  created_at: string
  last_login_at?: string | null
}

interface TokenPair {
  access_token: string
  refresh_token: string
  user: BackendUser
}

const { t } = useI18n()

defineEmits([...useDialogPluginComponent.emits])
const { dialogRef, onDialogHide, onDialogOK, onDialogCancel } = useDialogPluginComponent<TokenPair>()

const mode = ref<'login' | 'register'>('login')
const email = ref('')
const password = ref('')
const inviteCode = ref('')
const showPwd = ref(false)
const loading = ref(false)
const error = ref('')

function toggleMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
}

async function submit() {
  if (!email.value || !password.value) return
  if (mode.value === 'register' && password.value.length < 8) {
    error.value = t('backendLoginDialog.passwordMinLength')
    return
  }
  loading.value = true
  error.value = ''
  try {
    const path = mode.value === 'login' ? '/api/v1/auth/login' : '/api/v1/auth/register'
    const body: Record<string, string> = {
      email: email.value.trim().toLowerCase(),
      password: password.value
    }
    if (mode.value === 'register' && inviteCode.value) {
      body.invite_code = inviteCode.value.trim()
    }
    const res = await fetch(`${BackendApiBaseURL}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
    if (!res.ok) {
      let detail: string | undefined
      try { detail = (await res.json())?.detail } catch { /* ignore */ }
      error.value = detail || `${res.status} ${res.statusText}`
      return
    }
    const pair = await res.json() as TokenPair
    onDialogOK(pair)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
</script>
