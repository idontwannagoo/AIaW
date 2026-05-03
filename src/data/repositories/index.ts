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
import { serverAssistantsRepository } from './assistants.server'
import { serverInstalledPluginsRepository } from './installed-plugins.server'
import { serverAvatarImagesRepository } from './avatar-images.server'
import { serverWorkspacesRepository } from './workspaces.server'
import { serverDialogsRepository } from './dialogs.server'
import { serverItemsRepository } from './items.server'
import { serverArtifactsRepository } from './artifacts.server'
import type { Repository } from '../types'

const SERVER_CAPABLE_TABLES = new Set([
  'providers',
  'reactives',
  'assistants',
  // Frontend names match Dexie table names; backend maps these to its own
  // snake_case tables (`installed_plugins` / `avatar_images`).
  'installedPlugins',
  'avatarImages',
  'workspaces',
  'dialogs',
  'items',
  'artifacts'
])

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const t = <T, K extends string = string>(getter: () => any): (() => Table<T, K>) => () => getter() as Table<T, K>

function routeToServer(table: string): boolean {
  return !!BackendApiBaseURL && SERVER_CAPABLE_TABLES.has(table) && BackendDataTables.has(table)
}

const dexieProviders = createDexieRepository<CustomProvider, string>(t(() => db.providers)) as Repository<CustomProvider, string>
const dexieReactives = createDexieRepository<StoredReactive, string>(t(() => db.reactives)) as Repository<StoredReactive, string>
const dexieAssistants = createDexieRepository<Assistant, string>(t(() => db.assistants)) as Repository<Assistant, string>
const dexieInstalledPlugins = createDexieRepository<InstalledPlugin, string>(t(() => db.installedPluginsV2)) as Repository<InstalledPlugin, string>
const dexieAvatarImages = createDexieRepository<AvatarImage, string>(t(() => db.avatarImages)) as Repository<AvatarImage, string>
const dexieWorkspaces = createDexieRepository<Workspace | Folder, string>(t(() => db.workspaces)) as Repository<Workspace | Folder, string>

const dexieDialogs = createDexieRepository<Dialog, string>(t(() => db.dialogs)) as Repository<Dialog, string>
const dexieItems = createDexieRepository<StoredItem, string>(t(() => db.items)) as Repository<StoredItem, string>
const dexieArtifacts = createDexieRepository<Artifact, string>(t(() => db.artifacts)) as Repository<Artifact, string>

export const repos = {
  workspaces: routeToServer('workspaces') ? serverWorkspacesRepository : dexieWorkspaces,
  dialogs: routeToServer('dialogs') ? serverDialogsRepository : dexieDialogs,
  messages: createDexieRepository<Message, string>(t(() => db.messages)) as Repository<Message, string>,
  assistants: routeToServer('assistants') ? serverAssistantsRepository : dexieAssistants,
  artifacts: routeToServer('artifacts') ? serverArtifactsRepository : dexieArtifacts,
  installedPlugins: routeToServer('installedPlugins') ? serverInstalledPluginsRepository : dexieInstalledPlugins,
  reactives: routeToServer('reactives') ? serverReactivesRepository : dexieReactives,
  avatarImages: routeToServer('avatarImages') ? serverAvatarImagesRepository : dexieAvatarImages,
  items: routeToServer('items') ? serverItemsRepository : dexieItems,
  providers: routeToServer('providers') ? serverProvidersRepository : dexieProviders
}

export type Repos = typeof repos
