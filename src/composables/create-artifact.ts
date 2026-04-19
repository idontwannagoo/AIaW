import { db } from 'src/utils/db'
import { syncClient } from 'src/utils/sync-client'
import { genId } from 'src/utils/functions'
import { Artifact, Workspace } from 'src/utils/types'
import { Ref } from 'vue'
import { useRouter } from 'vue-router'

export function useCreateArtifact(workspace: Ref<Workspace>) {
  const router = useRouter()
  async function createArtifact(props: Partial<Artifact> = {}) {
    const id = genId()
    const artifact = {
      id,
      name: 'new_artifact',
      versions: [{ date: new Date(), text: '' }],
      currIndex: 0,
      readable: true,
      writable: true,
      open: true,
      workspaceId: workspace.value.id,
      tmp: '',
      ...props
    }
    await db.artifacts.add(artifact)
    void syncClient.push('artifacts', 'put', artifact)
    router.push({ query: { artifactId: id } })
    return id
  }
  return { createArtifact }
}
