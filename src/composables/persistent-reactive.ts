import { watch, reactive, toRaw, ref } from 'vue'
import { db } from 'src/utils/db'
import { syncClient } from 'src/utils/sync-client'
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
    db.reactives.put({ key, value: toRaw(val) })
    void syncClient.push('reactives', 'put', key)
  })
  const source = useLiveQuery(() => db.reactives.get(key), { initialValue: 'initial' as const })
  watch(source, newVal => {
    if (newVal === 'initial') return
    flag = true
    if (newVal) {
      ready.value = true
      Object.assign(val, newVal.value)
    } else {
      db.reactives.add({ key, value })
    }
  })
  return [val, ready] as const
}
