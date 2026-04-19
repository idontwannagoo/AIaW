import { db } from 'src/utils/db'
import { syncClient } from 'src/utils/sync-client'
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
    const dialog = {
      id,
      workspaceId: workspace.value.id,
      name: t('createDialog.newDialog'),
      msgTree: { $root: [messageId], [messageId]: [] },
      msgRoute: [],
      msgBranchState: {},
      assistantId: workspace.value.defaultAssistantId,
      inputVars: {},
      ...props
    }
    const message = {
      id: messageId,
      dialogId: id,
      type: 'user' as const,
      contents: [{ type: 'user-message' as const, text: '', items: [] }],
      status: 'inputing' as const
    }
    await db.transaction('rw', db.dialogs, db.messages, () => {
      db.dialogs.add(dialog)
      db.messages.add(message)
    })
    void syncClient.push('dialogs', 'put', dialog)
    void syncClient.push('messages', 'put', message)
    router.push(`/workspaces/${workspace.value.id}/dialogs/${id}`)
  }
  return { createDialog }
}
