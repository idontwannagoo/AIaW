import { BackendAuth } from 'src/utils/config'
import type { AuthSource } from './types'
import { dexieAuthSource } from './auth.dexie'
import { backendAuthSource } from './auth.backend'

// Stage 1.5: BACKEND_AUTH=true picks the self-hosted JWT source. Otherwise
// keep the Stage 0 Dexie-backed implementation. Both shapes are AuthSource
// so consumers don't branch.
export const authSource: AuthSource = BackendAuth ? backendAuthSource : dexieAuthSource
// Stage 1.5 双写窗口：BACKEND_AUTH=true 时 dexieAuthSource 仍可被独立调用
// （用于继续访问尚未迁移到 backend 的表的原 Dexie Cloud 账号）。两套登录态独立。
export { dexieAuthSource } from './auth.dexie'
export { registerBackendLoginDialog } from './auth.backend'
