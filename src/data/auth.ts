import type { AuthSource } from './types'
import { backendAuthSource } from './auth.backend'

export const authSource: AuthSource = backendAuthSource
export { registerBackendLoginDialog } from './auth.backend'
