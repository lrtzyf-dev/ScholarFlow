<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { FileText } from 'lucide-vue-next'
import { getSourceRevision, type SourceRevision } from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'
const props = defineProps<{ ids: string[]; conversationId: string }>()
const metadata = ref<Record<string, SourceRevision>>({}),
  router = useRouter()
watch(
  () => [props.ids, props.conversationId],
  async () => {
    const cid = props.conversationId
    await Promise.all(
      props.ids
        .filter((id) => !metadata.value[id])
        .map(async (id) => {
          try {
            const item = await getSourceRevision(id, cid)
            if (cid === props.conversationId) metadata.value = { ...metadata.value, [id]: item }
          } catch {
            /* Keep the fixed ID and a working retry link. */
          }
        })
    )
  },
  { immediate: true, deep: true }
)
</script>
<template>
  <div v-if="ids.length" class="turn-source-references" aria-label="本轮固定版本资料">
    <button
      v-for="id in ids"
      :key="id"
      type="button"
      @click="router.push(sourceReaderLocation(id, conversationId))"
    >
      <FileText :size="12" /><span>{{ metadata[id]?.title || '打开本轮资料' }}</span
      ><small v-if="metadata[id]">v{{ metadata[id].version }}</small>
    </button>
  </div>
</template>
<style scoped>
.turn-source-references {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 8px 0;
}
.turn-source-references button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  border: 1px solid #dce3f2;
  border-radius: 6px;
  background: #f8faff;
  color: #5e77a0;
  padding: 5px 7px;
  font-size: 11px;
  cursor: pointer;
}
.turn-source-references span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}
.turn-source-references small {
  font-size: 10px;
}
</style>
