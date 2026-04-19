import { AvatarImage, StoredItem } from 'src/utils/types'
import { onUnmounted, Ref, ref, watch } from 'vue'
import { ensureLocalBuffer } from 'src/utils/file-storage'

const objectURLs: {
  [id: string]: { url: string, active: number }
} = {}

export function useFileURL(file: Ref<StoredItem | AvatarImage>) {
  const url = ref(null)
  function mount(item: StoredItem | AvatarImage) {
    const { id, contentBuffer, mimeType } = item
    if (objectURLs[id]) {
      objectURLs[id].active++
      url.value = objectURLs[id].url
      return
    }
    if (contentBuffer) {
      const blob = new Blob([contentBuffer], { type: mimeType })
      url.value = URL.createObjectURL(blob)
      objectURLs[id] = { url: url.value, active: 1 }
    } else {
      // No local buffer — try to fetch from S3 via fileKey.
      const table: 'avatarImages' | 'items' = 'dialogId' in item ? 'items' : 'avatarImages'
      ensureLocalBuffer(table, id).then(buffer => {
        if (!buffer) return
        if (objectURLs[id]) {
          objectURLs[id].active++
          url.value = objectURLs[id].url
          return
        }
        const blob = new Blob([buffer], { type: mimeType })
        url.value = URL.createObjectURL(blob)
        objectURLs[id] = { url: url.value, active: 1 }
      }).catch(err => {
        console.warn('[file-url] remote fetch failed', id, err)
      })
    }
  }
  function unmount({ id }) {
    if (!objectURLs[id]) return
    objectURLs[id].active--
    if (objectURLs[id].active === 0) {
      URL.revokeObjectURL(objectURLs[id].url)
      delete objectURLs[id]
    }
  }
  watch(file, (to, from) => {
    to && mount(to)
    from && unmount(from)
  }, { immediate: true })
  onUnmounted(() => {
    file.value && unmount(file.value)
  })
  return url
}
