import { useLiveQuery, useLiveQueryWithDeps } from 'src/composables/live-query'
import type { ShallowRef } from 'vue'

export function observe<T, I = undefined>(
  querier: () => T | Promise<T>,
  options: { initialValue?: I } = {}
): ShallowRef<T | I | undefined> {
  return useLiveQuery<T, I>(querier, options)
}

export function observeWithDeps<T, I = undefined>(
  deps: unknown,
  querier: () => T | Promise<T>,
  options: { initialValue?: I } = {}
): ShallowRef<T | I | undefined> {
  return useLiveQueryWithDeps<T, I>(deps, querier, options)
}
