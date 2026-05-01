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
  async waitForFirstSync() {
    if (!enabled) return
    await new Promise<void>(resolve => {
      let done = false
      let sub: { unsubscribe(): void } | undefined
      const finish = () => {
        if (done) return
        done = true
        try { sub?.unsubscribe() } catch { /* ignore */ }
        clearTimeout(timer)
        resolve()
      }
      try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        sub = (db.cloud.syncState as any).subscribe((state: { phase: string }) => {
          if (
            state.phase === 'in-sync' ||
            state.phase === 'error' ||
            state.phase === 'offline'
          ) finish()
        })
      } catch {
        finish()
      }
      const timer = setTimeout(finish, 5000)
    })
  },
  currentToken() {
    if (!enabled) return undefined
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return (db.cloud.currentUser as any)?.value?.accessToken
  }
}
