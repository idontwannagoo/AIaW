import { BackendAuth } from 'src/utils/config'
import type { AuthSource } from './types'
import { dexieAuthSource } from './auth.dexie'
import { backendAuthSource } from './auth.backend'

// Stage 1.5: BACKEND_AUTH=true picks the self-hosted JWT source. Otherwise
// keep the Stage 0 Dexie-backed implementation. Both shapes are AuthSource
// so consumers don't branch.
export const authSource: AuthSource = BackendAuth ? backendAuthSource : dexieAuthSource
export { registerBackendLoginDialog } from './auth.backend'
