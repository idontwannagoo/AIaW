import { defineStore } from 'pinia'
import { repos, runTx } from 'src/data'
import { genId } from 'src/utils/functions'
import { Folder, Workspace } from 'src/utils/types'
import { DefaultWsIndexContent } from 'src/utils/templates'
import { useI18n } from 'vue-i18n'

export const useWorkspacesStore = defineStore('workspaces', () => {
  const workspaces = repos.workspaces.observeList<(Workspace | Folder)[]>({ initialValue: [] })
  const { t } = useI18n()
  async function addWorkspace(props: Partial<Workspace>) {
    return await repos.workspaces.add({
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
    } as Workspace)
  }

  async function addFolder(props: Partial<Folder>) {
    return await repos.workspaces.add({
      id: genId(),
      name: t('stores.workspaces.newFolder'),
      avatar: { type: 'icon', icon: 'sym_o_folder' },
      type: 'folder',
      parentId: '$root',
      ...props
    } as Folder)
  }

  async function updateItem(id: string, changes) {
    return await repos.workspaces.update(id, changes)
  }

  async function putItem(workspace: Workspace) {
    return await repos.workspaces.put(workspace)
  }

  async function deleteItem(id) {
    const keys = await repos.workspaces.findKeys({ where: { parentId: id } })
    for (const key of keys) {
      await deleteItem(key)
    }
    await runTx(['workspaces', 'dialogs', 'messages', 'items', 'assistants', 'artifacts'], async () => {
      const dialogIds = await repos.dialogs.findKeys({ where: { workspaceId: id } })
      for (const dialogId of dialogIds) {
        await repos.messages.deleteWhere({ where: { dialogId } })
        await repos.items.deleteWhere({ where: { dialogId } })
      }
      await repos.dialogs.deleteWhere({ where: { workspaceId: id } })
      await repos.assistants.deleteWhere({ where: { workspaceId: id } })
      await repos.artifacts.deleteWhere({ where: { workspaceId: id } })
      await repos.workspaces.delete(id)
    })
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
