import { defineStore } from 'pinia'
import { useLiveQuery } from 'src/composables/live-query'
import { db } from 'src/utils/db'
import { genId } from 'src/utils/functions'
import { Folder, Workspace } from 'src/utils/types'
import { DefaultWsIndexContent } from 'src/utils/templates'
import { useI18n } from 'vue-i18n'
import { syncClient } from 'src/utils/sync-client'

export const useWorkspacesStore = defineStore('workspaces', () => {
  const workspaces = useLiveQuery(() => db.workspaces.toArray(), { initialValue: [] as Workspace[] })
  const { t } = useI18n()
  async function addWorkspace(props: Partial<Workspace>) {
    const workspace = {
      id: genId(),
      name: t('stores.workspaces.newWorkspace'),
      avatar: { type: 'icon', icon: 'sym_o_deployed_code' },
      type: 'workspace',
      parentId: '$root',
      prompt: '',
      indexContent: DefaultWsIndexContent,
      vars: {},
      listOpen: {
        assistants: true,
        artifacts: false,
        dialogs: true
      },
      ...props
    } as Workspace
    await db.workspaces.add(workspace)
    void syncClient.push('workspaces', 'put', workspace)
    return workspace.id
  }

  async function addFolder(props: Partial<Folder>) {
    const folder = {
      id: genId(),
      name: t('stores.workspaces.newFolder'),
      avatar: { type: 'icon', icon: 'sym_o_folder' },
      type: 'folder',
      parentId: '$root',
      ...props
    }
    await db.workspaces.add(folder)
    void syncClient.push('workspaces', 'put', folder)
    return folder.id
  }

  async function updateItem(id: string, changes) {
    const result = await db.workspaces.update(id, changes)
    void syncClient.push('workspaces', 'put', id)
    return result
  }

  async function putItem(workspace: Workspace) {
    const result = await db.workspaces.put(workspace)
    void syncClient.push('workspaces', 'put', workspace)
    return result
  }

  async function deleteItem(id) {
    const keys = await db.workspaces.where('parentId').equals(id).primaryKeys()
    for (const key of keys) {
      await deleteItem(key)
    }
    const dialogIds = await db.dialogs.where('workspaceId').equals(id).primaryKeys()
    const assistantIds = await db.assistants.where('workspaceId').equals(id).primaryKeys()
    const artifactIds = await db.artifacts.where('workspaceId').equals(id).primaryKeys()
    await db.transaction('rw', [db.workspaces, db.dialogs, db.messages, db.items, db.assistants, db.artifacts], async () => {
      for (const dialogId of dialogIds) {
        await db.messages.where('dialogId').equals(dialogId).delete()
        await db.items.where('dialogId').equals(dialogId).delete()
      }
      await db.dialogs.where('workspaceId').equals(id).delete()
      await db.assistants.where('workspaceId').equals(id).delete()
      await db.artifacts.where('workspaceId').equals(id).delete()
      await db.workspaces.delete(id)
    })
    void syncClient.push('workspaces', 'delete', id)
    for (const dialogId of dialogIds) {
      void syncClient.push('dialogs', 'delete', dialogId)
    }
    for (const aId of assistantIds) {
      void syncClient.push('assistants', 'delete', aId)
    }
    for (const aId of artifactIds) {
      void syncClient.push('artifacts', 'delete', aId)
    }
    return true
  }

  return {
    workspaces,
    addWorkspace,
    addFolder,
    updateItem,
    putItem,
    deleteItem
  }
})
