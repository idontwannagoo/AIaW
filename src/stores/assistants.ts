import { defineStore } from 'pinia'
import { useLiveQuery } from 'src/composables/live-query'
import { db, defaultModelSettings } from 'src/utils/db'
import { defaultAvatar, genId } from 'src/utils/functions'
import { Assistant } from 'src/utils/types'
import { AssistantDefaultPrompt } from 'src/utils/templates'
import { useI18n } from 'vue-i18n'
import { syncClient } from 'src/utils/sync-client'

export const useAssistantsStore = defineStore('assistants', () => {
  const assistants = useLiveQuery(() => db.assistants.toArray(), { initialValue: [] as Assistant[] })
  const { t } = useI18n()
  async function add(props: Partial<Assistant> = {}) {
    const assistant = {
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
    }
    await db.assistants.add(assistant)
    void syncClient.push('assistants', 'put', assistant)
    return assistant.id
  }

  async function update(id: string, changes) {
    const result = await db.assistants.update(id, changes)
    void syncClient.push('assistants', 'put', id)
    return result
  }

  async function put(assistant: Assistant) {
    const result = await db.assistants.put(assistant)
    void syncClient.push('assistants', 'put', assistant)
    return result
  }

  async function delete_(id: string) {
    const result = await db.assistants.delete(id)
    void syncClient.push('assistants', 'delete', id)
    return result
  }

  return {
    assistants,
    add,
    update,
    put,
    delete: delete_
  }
})
