import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { BackendApiBaseURL, BackendDataTables } from 'src/utils/config'
import type {
  Workspace, Folder, Dialog, Message, Assistant, Artifact,
  StoredReactive, InstalledPlugin, AvatarImage, StoredItem, CustomProvider
} from 'src/utils/types'
import { createDexieRepository } from './dexie'
import { serverProvidersRepository } from './providers.server'
import { serverReactivesRepository } from './reactives.server'
import type { Repository } from '../types'

const SERVER_CAPABLE_TABLES = new Set(['providers', 'reactives'])

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const t = <T, K extends string = string>(getter: () => any): (() => Table<T, K>) => () => getter() as Table<T, K>

function routeToServer(table: string): boolean {
  return !!BackendApiBaseURL && SERVER_CAPABLE_TABLES.has(table) && BackendDataTables.has(table)
}

const dexieProviders = createDexieRepository<CustomProvider, string>(t(() => db.providers)) as Repository<CustomProvider, string>
const dexieReactives = createDexieRepository<StoredReactive, string>(t(() => db.reactives)) as Repository<StoredReactive, string>

export const repos = {
  workspaces: createDexieRepository<Workspace | Folder, string>(t(() => db.workspaces)) as Repository<Workspace | Folder, string>,
  dialogs: createDexieRepository<Dialog, string>(t(() => db.dialogs)) as Repository<Dialog, string>,
  messages: createDexieRepository<Message, string>(t(() => db.messages)) as Repository<Message, string>,
  assistants: createDexieRepository<Assistant, string>(t(() => db.assistants)) as Repository<Assistant, string>,
  artifacts: createDexieRepository<Artifact, string>(t(() => db.artifacts)) as Repository<Artifact, string>,
  installedPlugins: createDexieRepository<InstalledPlugin, string>(t(() => db.installedPluginsV2)) as Repository<InstalledPlugin, string>,
  reactives: routeToServer('reactives') ? serverReactivesRepository : dexieReactives,
  avatarImages: createDexieRepository<AvatarImage, string>(t(() => db.avatarImages)) as Repository<AvatarImage, string>,
  items: createDexieRepository<StoredItem, string>(t(() => db.items)) as Repository<StoredItem, string>,
  providers: routeToServer('providers') ? serverProvidersRepository : dexieProviders
}

export type Repos = typeof repos
