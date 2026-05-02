import Dexie, { type Table } from 'dexie'
import { defaultAvatar, genId } from './functions'
import { Workspace, Folder, Dialog, Message, Assistant, Artifact, StoredReactive, InstalledPlugin, AvatarImage, StoredItem, CustomProvider } from './types'
import { AssistantDefaultPrompt, ExampleWsIndexContent } from './templates'
import { i18n } from 'src/boot/i18n'

type Db = Dexie & {
  workspaces: Table<Workspace | Folder, string>
  dialogs: Table<Dialog, string>
  messages: Table<Message, string>
  assistants: Table<Assistant, string>
  artifacts: Table<Artifact, string>
  installedPluginsV2: Table<InstalledPlugin, string>
  reactives: Table<StoredReactive, string>
  avatarImages: Table<AvatarImage, string>
  items: Table<StoredItem, string>
  providers: Table<CustomProvider, string>
}

const db = new Dexie('data') as Db

const schema = {
  workspaces: 'id, type, parentId',
  dialogs: 'id, workspaceId',
  messages: 'id, type, dialogId',
  assistants: 'id, workspaceId',
  canvases: 'id, workspaceId', // deprecated
  artifacts: 'id, workspaceId',
  installedPluginsV2: 'key, id',
  reactives: 'key',
  avatarImages: 'id',
  items: 'id, type, dialogId',
  providers: 'id'
}
db.version(6).stores(schema)

const defaultModelSettings = {
  temperature: 0.6,
  topP: 1,
  presencePenalty: 0,
  frequencyPenalty: 0,
  maxSteps: 4,
  maxRetries: 1
}

const { t } = i18n.global

db.on.populate.subscribe(() => {
  db.on.ready.subscribe((db: Db) => {
    const initialWorkspaceId = genId()
    const initialAssistantId = genId()
    db.workspaces.add({
      id: initialWorkspaceId,
      name: t('db.exampleWorkspace'),
      avatar: { type: 'icon', icon: 'sym_o_menu_book' },
      type: 'workspace',
      parentId: '$root',
      prompt: '',
      defaultAssistantId: initialAssistantId,
      indexContent: ExampleWsIndexContent,
      vars: {},
      listOpen: {
        assistants: true,
        artifacts: false,
        dialogs: true
      }
    } as Workspace)
    db.assistants.add({
      id: initialAssistantId,
      name: t('db.defaultAssistant'),
      avatar: defaultAvatar('AI'),
      workspaceId: initialWorkspaceId,
      prompt: '',
      promptTemplate: AssistantDefaultPrompt,
      promptVars: [],
      provider: null,
      model: null,
      modelSettings: { ...defaultModelSettings },
      plugins: {},
      promptRole: 'system',
      stream: true
    })
    db.reactives.add({
      key: '#user-data',
      value: {
        lastWorkspaceId: initialWorkspaceId
      }
    })
  }, false)
})

// Migration
db.assistants.hook('reading', assistant => {
  if (!assistant) return assistant
  assistant.promptRole ??= 'system'
  assistant.stream ??= true
  // Migration to v1.8
  const { modelSettings } = assistant
  if (modelSettings && 'maxTokens' in modelSettings) {
    modelSettings.maxOutputTokens = modelSettings.maxTokens as number
    delete modelSettings.maxTokens
  }
  return assistant
})
// Migration to v1.4
db.workspaces.hook('reading', workspace => {
  if (workspace?.type === 'workspace') {
    workspace.listOpen ??= {
      assistants: true,
      artifacts: false,
      dialogs: true
    }
  }
  return workspace
})

db.messages.hook('reading', message => {
  if (!message) return message
  const usage = message.usage as any
  if (usage && 'promptTokens' in usage) {
    message.usage = {
      inputTokens: usage.promptTokens,
      outputTokens: usage.completionTokens,
      totalTokens: usage.totalTokens
    }
  }
  return message
})

export { schema, db, defaultModelSettings }
export type { Db }
