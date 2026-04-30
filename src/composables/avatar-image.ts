import { Ref, ref, watch } from 'vue'
import { repos } from 'src/data'
import { useFileURL } from './file-url'
import { AvatarImage } from 'src/utils/types'

export function useAvatarImage(imageId: Ref<string>) {
  const image = ref<AvatarImage>(null)
  watch(imageId, to => {
    if (to) {
      repos.avatarImages.get(to).then(i => {
        image.value = i
      })
    } else {
      image.value = null
    }
  }, { immediate: true })
  return useFileURL(image)
}
