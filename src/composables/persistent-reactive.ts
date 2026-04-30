import { watch, reactive, toRaw, ref } from 'vue'
import { repos } from 'src/data'
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
  watch(source, newVal => {
    if (newVal === 'initial') return
    flag = true
    if (newVal) {
      ready.value = true
      Object.assign(val, newVal.value)
    } else {
      repos.reactives.add({ key, value })
    }
  })
  return [val, ready] as const
}
