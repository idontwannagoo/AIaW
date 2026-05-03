import { repos } from 'src/data'
import { genId } from 'src/utils/functions'
import { Dialog, Workspace } from 'src/utils/types'
import { Ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

export function useCreateDialog(workspace: Ref<Workspace>) {
  const router = useRouter()
  const { t } = useI18n()

  async function createDialog(props: Partial<Dialog> = {}) {
    const id = genId()
    const messageId = genId()
    // Bug 5 fix — sequential awaits, not a Dexie transaction.
    // dialogs / messages are server-routed; their `add` calls await
    // `http.put` internally → wrapping in `runTx` causes Dexie to throw
    // PrematureCommitError on the second op. Same compromise as
    // stores/workspaces.ts:54-61.
    await repos.dialogs.add({
      id,
      workspaceId: workspace.value.id,
      name: t('createDialog.newDialog'),
      msgTree: { $root: [messageId], [messageId]: [] },
      msgRoute: [],
      msgBranchState: {},
      assistantId: workspace.value.defaultAssistantId,
      inputVars: {},
      ...props
    } as Dialog)
    await repos.messages.add({
      id: messageId,
      dialogId: id,
      type: 'user',
      contents: [{
        type: 'user-message',
        text: '',
        items: []
      }],
      status: 'inputing'
    } as never)
    router.push(`/workspaces/${workspace.value.id}/dialogs/${id}`)
  }
  return { createDialog }
}
