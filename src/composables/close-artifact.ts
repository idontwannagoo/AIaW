import { useQuasar } from 'quasar'
import SaveDialog from 'src/components/SaveDialog.vue'
import { db } from 'src/utils/db'
import { syncClient } from 'src/utils/sync-client'
import { restoreArtifactChanges, saveArtifactChanges } from 'src/utils/functions'
import { Artifact } from 'src/utils/types'

export function useCloseArtifact() {
  const $q = useQuasar()
  function closeArtifact(artifact: Artifact) {
    if (artifact.tmp !== artifact.versions[artifact.currIndex].text) {
      $q.dialog({
        component: SaveDialog,
        componentProps: {
          name: artifact.name
        }
      }).onOk(async (save: boolean) => {
        const changes = save ? saveArtifactChanges(artifact) : restoreArtifactChanges(artifact)
        await db.artifacts.update(artifact.id, { open: false, ...changes })
        void syncClient.push('artifacts', 'put', artifact.id)
      })
    } else {
      db.artifacts.update(artifact.id, { open: false })
      void syncClient.push('artifacts', 'put', artifact.id)
    }
  }
  return { closeArtifact }
}
