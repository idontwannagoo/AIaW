<template>
  <q-dialog
    ref="dialogRef"
    @hide="onDialogHide"
  >
    <q-card min-w="320px">
      <q-card-section>
        <div class="text-h6">
          {{ $t('exportDataDialog.title') }}
        </div>
      </q-card-section>
      <q-card-section
        py-0
      >
        <div my-2>
          <q-checkbox
            v-model="removeUserMark"
            :label="$t('exportDataDialog.removeUserMark')"
          />
        </div>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn
          flat
          color="primary"
          :label="$t('exportDataDialog.cancel')"
          @click="onDialogCancel"
        />
        <q-btn
          flat
          color="primary"
          :label="$t('exportDataDialog.export')"
          @click="exportData"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { exportDB } from 'dexie-export-import'
import { useDialogPluginComponent, useQuasar } from 'quasar'
import { db, schema } from 'src/utils/db'
import { exportFile } from 'src/utils/platform-api'
import { downloadBinary } from 'src/utils/file-storage'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const removeUserMark = ref(false)

defineEmits([
  ...useDialogPluginComponent.emits
])

const $q = useQuasar()

const { dialogRef, onDialogHide, onDialogOK, onDialogCancel } = useDialogPluginComponent()

async function backfillTransform(table: string, value: Record<string, unknown>) {
  const BINARY_TABLES = new Set(['avatarImages', 'items'])
  if (BINARY_TABLES.has(table) && value.fileKey && !value.contentBuffer) {
    try {
      value = { ...value, contentBuffer: await downloadBinary(value.fileKey as string) }
    } catch (err) {
      console.warn('[export] binary backfill failed', value.fileKey, err)
    }
  }
  if (removeUserMark.value) {
    return { value: { ...value, owner: 'unauthorized', realmId: 'unauthorized' } }
  }
  return { value }
}

function exportData() {
  const filter = removeUserMark.value ? (table: string) => Object.keys(schema).includes(table) : undefined
  exportDB(db, { filter, transform: backfillTransform }).then(async blob => {
    await exportFile('aiaw_user_db.json', blob)
    onDialogOK()
  }).catch(err => {
    console.error(err)
    $q.notify({
      message: t('exportDataDialog.exportFailed'),
      color: 'negative'
    })
  })
}

</script>
