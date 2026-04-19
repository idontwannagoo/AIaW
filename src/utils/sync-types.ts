export type SyncEntity =
  | 'workspaces'
  | 'dialogs'
  | 'messages'
  | 'assistants'
  | 'artifacts'
  | 'items'
  | 'avatarImages'
  | 'installedPluginsV2'
  | 'reactives'
  | 'providers'

export const ALL_SYNC_ENTITIES: SyncEntity[] = [
  'workspaces',
  'dialogs',
  'messages',
  'assistants',
  'artifacts',
  'items',
  'avatarImages',
  'installedPluginsV2',
  'reactives',
  'providers'
]

export interface EntityOut {
  id: string
  data: Record<string, unknown>
  updated_at: string
  deleted_at: string | null
}

export interface WsMessage {
  entity: SyncEntity
  id: string
  updated_at: string
  deleted_at?: string
}

export interface OutboxEntry {
  seq: number
  entity: SyncEntity
  op: 'put' | 'delete'
  id: string
  payload?: Record<string, unknown>
  tries: number
  createdAt: number
}

export type EntityChangeListener = (msg: WsMessage) => void
