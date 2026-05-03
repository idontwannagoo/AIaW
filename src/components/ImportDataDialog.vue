<template>
  <q-dialog
    ref="dialogRef"
    persistent
    @hide="onDialogHide"
  >
    <q-card min-w="360px">
      <q-card-section>
        <div class="text-h6">
          {{ $t('importDataDialog.title') }}
        </div>
        <div
          text-on-sur-var
          text-sm
          mt-1
        >
          {{ $t('importDataDialog.description') }}
        </div>
      </q-card-section>
      <q-card-section
        py-0
        px-2
      >
        <div px-2>
          <q-file
            v-model="file"
            :label="$t('importDataDialog.fileLabel')"
            :disable="phase !== 'idle'"
            accept=".json,application/json"
            dense
          />
        </div>
        <div
          v-if="phase !== 'idle'"
          mt-3
          px-2
        >
          <div
            text-sm
            mb-1
          >
            {{ phaseLabel }}
          </div>
          <q-linear-progress
            :value="progressFraction"
            :indeterminate="phase === 'creating' || phase === 'completing'"
            size="md"
            rounded
            color="primary"
          />
          <div
            text-xs
            text-on-sur-var
            mt-1
            data-testid="import-upload-stats"
          >
            <span v-if="phase === 'uploading'">
              {{ $t('importDataDialog.uploadStats', {
                uploaded: formatBytes(uploadedBytes),
                total: formatBytes(totalBytes),
                eta: etaText
              }) }}
            </span>
            <span v-else-if="phase === 'creating'">
              {{ $t('importDataDialog.creating') }}
            </span>
            <span v-else-if="phase === 'completing'">
              {{ $t('importDataDialog.completing') }}
            </span>
          </div>
        </div>
        <div
          v-if="errorMessage"
          mt-3
          px-2
          text-sm
          text-err
          data-testid="import-error"
        >
          {{ errorMessage }}
        </div>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn
          flat
          color="primary"
          :label="$t('importDataDialog.cancel')"
          :disable="phase === 'completing'"
          @click="cancel"
        />
        <q-btn
          unelevated
          color="primary"
          :label="$t('importDataDialog.import')"
          :loading="phase !== 'idle'"
          :disable="!file || phase !== 'idle'"
          data-testid="import-start-button"
          @click="startImport"
        />
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>

<script setup lang="ts">
import { useDialogPluginComponent, useQuasar } from 'quasar'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  cancelImport,
  completeImport,
  createImportJob,
  uploadParts
} from 'src/data/import-client'
import { reloadActiveImportJob } from 'src/composables/import-job'

defineEmits([
  ...useDialogPluginComponent.emits
])

const { t } = useI18n()
const $q = useQuasar()
const { dialogRef, onDialogHide, onDialogOK, onDialogCancel } = useDialogPluginComponent()

type Phase = 'idle' | 'creating' | 'uploading' | 'completing'

const file = ref<File | null>(null)
const phase = ref<Phase>('idle')
const errorMessage = ref<string>('')

const uploadedBytes = ref(0)
const totalBytes = ref(0)
const completedParts = ref(0)
const totalParts = ref(0)
// Updated on every onProgress tick. We use it together with `tickStart` /
// `tickStartBytes` to estimate ETA from a sliding window — instantaneous
// ETA from the very first tick would be wildly off because of TLS handshake
// latency etc. The sliding sample resets every 5 ticks.
const tickStart = ref(0)
const tickStartBytes = ref(0)
const ticksSinceReset = ref(0)
const etaSeconds = ref<number | null>(null)

let activeJobId: string | null = null
let abortController: AbortController | null = null

const progressFraction = computed(() => {
  if (totalBytes.value === 0) return 0
  return Math.min(1, uploadedBytes.value / totalBytes.value)
})

const phaseLabel = computed(() => {
  switch (phase.value) {
    case 'creating': return t('importDataDialog.phaseCreating')
    case 'uploading': return t('importDataDialog.phaseUploading')
    case 'completing': return t('importDataDialog.phaseCompleting')
    default: return ''
  }
})

const etaText = computed(() => {
  if (etaSeconds.value === null) return t('importDataDialog.etaCalculating')
  const s = Math.max(0, Math.round(etaSeconds.value))
  if (s < 60) return t('importDataDialog.etaSeconds', { s })
  const m = Math.floor(s / 60)
  const rs = s % 60
  return t('importDataDialog.etaMinutes', { m, s: rs })
})

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function onProgress(uploaded: number, total: number, completed: number, parts: number): void {
  uploadedBytes.value = uploaded
  totalBytes.value = total
  completedParts.value = completed
  totalParts.value = parts

  // ETA sliding window — re-anchor every 5 ticks so we don't carry stale
  // throughput across long pauses (e.g. retry backoff). First tick just
  // anchors; we get an estimate starting at tick 2.
  const now = Date.now()
  if (ticksSinceReset.value === 0) {
    tickStart.value = now
    tickStartBytes.value = uploaded
    ticksSinceReset.value = 1
    return
  }
  ticksSinceReset.value++
  const elapsed = now - tickStart.value
  const deltaBytes = uploaded - tickStartBytes.value
  if (elapsed > 0 && deltaBytes > 0) {
    const bps = deltaBytes / (elapsed / 1000)
    const remaining = total - uploaded
    etaSeconds.value = remaining > 0 ? remaining / bps : 0
  }
  if (ticksSinceReset.value >= 5) {
    tickStart.value = now
    tickStartBytes.value = uploaded
    ticksSinceReset.value = 1
  }
}

async function startImport(): Promise<void> {
  if (!file.value) return
  errorMessage.value = ''
  uploadedBytes.value = 0
  totalBytes.value = file.value.size
  completedParts.value = 0
  totalParts.value = 0
  etaSeconds.value = null
  ticksSinceReset.value = 0

  abortController = new AbortController()

  try {
    phase.value = 'creating'
    const created = await createImportJob(file.value)
    activeJobId = created.jobId

    phase.value = 'uploading'
    const parts = await uploadParts(file.value, created.jobId, {
      signal: abortController.signal,
      onProgress
    })

    phase.value = 'completing'
    await completeImport(created.jobId, parts)

    // The import job is now in the queued/parsing pipeline. Tell the global
    // composable to refetch the active job — it'll attach a realtime sub
    // and the AccountPage banner picks up the rest.
    await reloadActiveImportJob()

    $q.notify({
      message: t('importDataDialog.uploadDoneNotice'),
      color: 'positive',
      timeout: 6000
    })
    activeJobId = null
    abortController = null
    onDialogOK()
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    if (msg.includes('aborted') || (e instanceof DOMException && e.name === 'AbortError')) {
      // User-initiated cancel — already handled by cancel(), don't notify.
      return
    }
    console.error('[ImportDataDialog] import failed', e)
    errorMessage.value = t('importDataDialog.uploadFailed', { message: msg })
    phase.value = 'idle'
    abortController = null
    // Best-effort cleanup of the half-uploaded job server-side. We don't
    // await — failure shouldn't block the UI from accepting another attempt.
    if (activeJobId) {
      void cancelImport(activeJobId).catch(() => { /* ignore */ })
      activeJobId = null
    }
  }
}

async function cancel(): Promise<void> {
  if (phase.value === 'idle') {
    onDialogCancel()
    return
  }
  // Mid-upload cancel: abort the in-flight fetches first so uploadParts
  // throws AbortError out of `startImport`'s try, then DELETE the server-
  // side job to release the multipart slot + soft-delete any imported rows.
  abortController?.abort()
  if (activeJobId) {
    try { await cancelImport(activeJobId) } catch { /* ignore */ }
    activeJobId = null
  }
  phase.value = 'idle'
  errorMessage.value = ''
  onDialogCancel()
}
</script>
