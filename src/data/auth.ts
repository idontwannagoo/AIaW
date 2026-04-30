import { useObservable } from '@vueuse/rxjs'
import { db } from 'src/utils/db'
import { DexieDBURL } from 'src/utils/config'
import { shallowRef, type ShallowRef } from 'vue'
import type { AuthSource, CloudUser, UserInteraction } from './types'

const enabled = !!DexieDBURL

const userRef: ShallowRef<CloudUser | null | undefined> = enabled
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ? (useObservable(db.cloud.currentUser as any) as unknown as ShallowRef<CloudUser | null | undefined>)
  : shallowRef(null)

const interactionRef: ShallowRef<UserInteraction | null | undefined> = enabled
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ? (useObservable(db.cloud.userInteraction as any) as unknown as ShallowRef<UserInteraction | null | undefined>)
  : shallowRef(null)

export const authSource: AuthSource = {
  enabled,
  user: userRef,
  userInteraction: interactionRef,
  async login() {
    if (!enabled) return
    await db.cloud.login()
  },
  async logout() {
    if (!enabled) return
    await db.cloud.logout()
  },
  async sync() {
    if (!enabled) return
    await db.cloud.sync()
  },
  onReady(fn) {
    db.on('ready', () => {
      fn()
    })
  },
  currentToken() {
    if (!enabled) return undefined
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (db.cloud.currentUser as any)?.value?.accessToken
  }
}
