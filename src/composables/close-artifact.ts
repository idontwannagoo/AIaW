import { useQuasar } from 'quasar'
import SaveDialog from 'src/components/SaveDialog.vue'
import { repos } from 'src/data'
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
      }).onOk((save: boolean) => {
        const changes = save ? saveArtifactChanges(artifact) : restoreArtifactChanges(artifact)
        repos.artifacts.update(artifact.id, { open: false, ...changes })
      })
    } else {
      repos.artifacts.update(artifact.id, { open: false })
    }
  }
  return { closeArtifact }
}
