import { watch, reactive, toRaw, ref } from 'vue'
import { repos, authSource } from 'src/data'
import { useLiveQuery } from './live-query'

export function persistentReactive<T extends object>(key: string, value: T) {
  const val = reactive(value)
  const ready = ref(false)
  let flag = false
  watch(val, () => {
    if (!ready.value) return
    if (flag) {
      flag = false
      return
    }
    repos.reactives.put({ key, value: toRaw(val) })
  })
  const source = useLiveQuery(() => repos.reactives.get(key), { initialValue: 'initial' as const })
  watch(source, async newVal => {
    if (newVal === 'initial') return
    if (newVal) {
      flag = true
      ready.value = true
      Object.assign(val, newVal.value)
      return
    }
    // Local row missing. With cloud sync enabled, eagerly writing defaults here
    // would race with sync and overwrite values stored on other devices (LWW
    // resolves toward the just-written local row). Wait for the first sync to
    // finish; if cloud delivers a value, this watcher re-fires with newVal != null.
    if (authSource.enabled) {
      await authSource.waitForFirstSync()
      const existing = await repos.reactives.get(key)
      if (existing) return
    }
    // No persisted value anywhere. Keep defaults in memory and mark ready;
    // only persist once the user actually mutates the object (handled by the
    // val watcher above). This avoids ever pushing default placeholders to the cloud.
    ready.value = true
  })
  return [val, ready] as const
}
