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

async function buildBinaryBackfillMap(): Promise<Map<string, ArrayBuffer>> {
  const map = new Map<string, ArrayBuffer>()
  for (const tableName of ['avatarImages', 'items'] as const) {
    const rows = await db.table(tableName).toArray() as { id: string; contentBuffer?: ArrayBuffer; fileKey?: string }[]
    for (const row of rows) {
      if (!row.contentBuffer && row.fileKey) {
        try {
          map.set(row.id, await downloadBinary(row.fileKey))
        } catch (err) {
          console.warn('[export] binary backfill failed', row.id, err)
        }
      }
    }
  }
  return map
}

function exportData() {
  const BINARY_TABLES = new Set(['avatarImages', 'items'])
  buildBinaryBackfillMap().then(binaryMap => {
    const filter = removeUserMark.value ? (table: string) => Object.keys(schema).includes(table) : undefined
    const transform = (table: string, value: Record<string, unknown>) => {
      let v = value
      if (BINARY_TABLES.has(table) && !v.contentBuffer && binaryMap.has(v.id as string)) {
        v = { ...v, contentBuffer: binaryMap.get(v.id as string) }
      }
      if (removeUserMark.value) {
        v = { ...v, owner: 'unauthorized', realmId: 'unauthorized' }
      }
      return { value: v }
    }
    return exportDB(db, { filter, transform })
  }).then(async blob => {
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
