import type { ShallowRef } from 'vue'
import type { Table } from 'dexie'

export type WhereValue<V> = V | V[] | { in: V[] }

export interface QuerySpec<T = unknown> {
  where?: Partial<{ [K in keyof T]: WhereValue<T[K]> }> | Record<string, WhereValue<unknown>>
}

export interface Repository<T, K extends string = string> {
  table(): Table<T, K>

  get(id: K): Promise<T | undefined>
  list(): Promise<T[]>
  find(spec: QuerySpec<T>): Promise<T[]>
  findKeys(spec: QuerySpec<T>): Promise<K[]>
  findFirst(spec: QuerySpec<T>): Promise<T | undefined>
  count(spec?: QuerySpec<T>): Promise<number>

  add(value: T): Promise<K>
  put(value: T): Promise<K>
  bulkPut(values: T[]): Promise<K>
  update(id: K, changes: Partial<T> | Record<string, unknown>): Promise<number>
  delete(id: K): Promise<void>
  bulkDelete(ids: K[]): Promise<void>
  deleteWhere(spec: QuerySpec<T>): Promise<number>
  modifyWhere(spec: QuerySpec<T>, changes: Partial<T> | Record<string, unknown>): Promise<number>
  modifyAll(predicate: (value: T) => boolean, changes: Partial<T> | Record<string, unknown>): Promise<number>

  observeList<I = T[]>(options?: { initialValue?: I }): ShallowRef<T[] | I>
  observeFind<I = T[]>(
    spec: QuerySpec<T> | (() => QuerySpec<T>),
    options?: { initialValue?: I; deps?: unknown }
  ): ShallowRef<T[] | I>
  observeOne<I = T | undefined>(
    id: K | (() => K),
    options?: { initialValue?: I; deps?: unknown }
  ): ShallowRef<T | I | undefined>
}

export interface OrderHistoryEntry {
  orderId: string
  timestamp: string
  item: { type: string; amount: number | string }
}

export interface CloudUserData {
  apiKey?: string
  orderHistory?: OrderHistoryEntry[]
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any
}

export interface CloudUserLicense {
  type: 'eval' | 'prod' | 'client' | 'demo'
  status?: 'ok' | 'expired' | 'deactivated' | string
  evalDaysLeft?: number
  validUntil?: Date
}

export interface CloudUser {
  isLoggedIn: boolean
  email?: string
  userId?: string
  accessToken?: string
  license?: CloudUserLicense
  data?: CloudUserData
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any
}

export interface UserInteraction {
  type: 'email' | 'otp' | 'logout-confirmation' | 'message-alert' | string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  alerts?: { message: string; type?: string }[]
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  onSubmit: (data: any) => void
  onCancel: () => void
}

export interface AuthSource {
  enabled: boolean
  user: ShallowRef<CloudUser | null | undefined>
  userInteraction: ShallowRef<UserInteraction | null | undefined>
  login(): Promise<void>
  logout(): Promise<void>
  sync(): Promise<void>
  onReady(fn: () => void): void
  // Resolve when the first sync attempt has completed (in-sync / error / offline),
  // or after a safety timeout. No-op when sync is not enabled.
  waitForFirstSync(): Promise<void>
  // Stage 0 escape hatch: synchronous read of current user (used for bearer token in fetch)
  currentToken(): string | undefined
  // Stage 1 Step 4: trigger a token refresh (only meaningful for sources that
  // own a refresh-token flow). Returns true if a usable access token exists
  // after the attempt. Sources without their own refresh return false.
  tryRefresh(): Promise<boolean>
}
