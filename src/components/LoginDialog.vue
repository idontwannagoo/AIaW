<template>
  <q-dialog
    ref="dialogRef"
    @hide="onDialogHide"
    persistent
  >
    <q-card min-w="360px">
      <q-card-section>
        <div class="text-h6">
          {{ mode === 'register' ? $t('login.registerTitle') : $t('login.loginTitle') }}
        </div>
      </q-card-section>
      <q-card-section
        py-0
        px-2
      >
        <div px-2>
          <q-input
            v-model="email"
            type="email"
            :label="$t('login.emailLabel')"
            autofocus
            :error="!!emailError"
            :error-message="emailError"
            @update:model-value="emailError = ''"
          />
          <q-input
            v-model="password"
            :type="showPassword ? 'text' : 'password'"
            :label="$t('login.passwordLabel')"
            :error="!!passwordError"
            :error-message="passwordError"
            @update:model-value="passwordError = ''"
            @keyup.enter="submit"
          >
            <template #append>
              <q-icon
                :name="showPassword ? 'sym_o_visibility_off' : 'sym_o_visibility'"
                class="cursor-pointer"
                @click="showPassword = !showPassword"
              />
            </template>
          </q-input>
          <div
            v-if="mode === 'register'"
            text-xs
            text-on-sur-var
            mt-2
            v-html="$t('login.privacyPolicy')"
          />
          <div
            v-if="generalError"
            text-err
            text-xs
            mt-2
          >
            {{ generalError }}
          </div>
        </div>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn
          flat
          color="primary"
          :label="mode === 'register' ? $t('login.switchToLogin') : $t('login.switchToRegister')"
          @click="toggleMode"
          :disable="loading"
        />
        <q-space />
        <q-btn
          flat
          :label="$t('login.cancel')"
          @click="onDialogCancel"
          :disable="loading"
        />
        <q-btn
          unelevated
          color="primary"
          :loading
          :label="mode === 'register' ? $t('login.register') : $t('login.login')"
          @click="submit"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useDialogPluginComponent } from 'quasar'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { syncClient } from 'src/utils/sync-client'

const props = withDefaults(defineProps<{ initialMode?: 'login' | 'register' }>(), {
  initialMode: 'login'
})
defineEmits([...useDialogPluginComponent.emits])
const { dialogRef, onDialogHide, onDialogOK, onDialogCancel } = useDialogPluginComponent()
const { t } = useI18n()

const mode = ref<'login' | 'register'>(props.initialMode)
const email = ref('')
const password = ref('')
const showPassword = ref(false)
const loading = ref(false)
const emailError = ref('')
const passwordError = ref('')
const generalError = ref('')

function toggleMode() {
  mode.value = mode.value === 'register' ? 'login' : 'register'
  generalError.value = ''
}

function validate(): boolean {
  emailError.value = ''
  passwordError.value = ''
  if (!email.value || !/.+@.+\..+/.test(email.value)) {
    emailError.value = t('login.invalidEmail')
    return false
  }
  if (!password.value || password.value.length < 6) {
    passwordError.value = t('login.invalidPassword')
    return false
  }
  return true
}

async function submit() {
  if (!validate()) return
  loading.value = true
  generalError.value = ''
  try {
    if (mode.value === 'register') {
      await syncClient.register(email.value.trim(), password.value)
    } else {
      await syncClient.login(email.value.trim(), password.value)
    }
    onDialogOK()
  } catch (err) {
    const msg = (err as Error).message || ''
    if (msg.includes('409') || msg.toLowerCase().includes('already')) {
      generalError.value = t('login.emailAlreadyRegistered')
    } else if (msg.includes('401') || msg.toLowerCase().includes('invalid')) {
      generalError.value = t('login.invalidCredentials')
    } else {
      generalError.value = msg || t('login.unknownError')
    }
  } finally {
    loading.value = false
  }
}
</script>
