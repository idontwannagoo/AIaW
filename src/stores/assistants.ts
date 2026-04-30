import { defineStore } from 'pinia'
import { repos } from 'src/data'
import { defaultModelSettings } from 'src/utils/db'
import { defaultAvatar, genId } from 'src/utils/functions'
import { Assistant } from 'src/utils/types'
import { AssistantDefaultPrompt } from 'src/utils/templates'
import { useI18n } from 'vue-i18n'

export const useAssistantsStore = defineStore('assistants', () => {
  const assistants = repos.assistants.observeList<Assistant[]>({ initialValue: [] })
  const { t } = useI18n()
  async function add(props: Partial<Assistant> = {}) {
    return await repos.assistants.add({
      name: t('stores.assistants.newAssistant'),
      id: genId(),
      avatar: defaultAvatar('AI'),
      workspaceId: '$root',
      prompt: '',
      promptTemplate: AssistantDefaultPrompt,
      promptVars: [],
      provider: null,
      model: null,
      modelSettings: { ...defaultModelSettings },
      plugins: {},
      promptRole: 'system',
      stream: true,
      ...props
    } as Assistant)
  }

  async function update(id: string, changes) {
    return await repos.assistants.update(id, changes)
  }

  async function put(assistant: Assistant) {
    return await repos.assistants.put(assistant)
  }

  async function delete_(id: string) {
    return await repos.assistants.delete(id)
  }

  return {
    assistants,
    add,
    update,
    put,
    delete: delete_
  }
})
