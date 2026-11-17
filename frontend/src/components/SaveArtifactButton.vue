<script setup lang="ts">
import { ref, watch } from 'vue'
import { artifactRequestId, createArtifact, type Artifact } from '../artifactApi'

const props = defineProps<{ title: string; content: string; evidence?: unknown[]; conversationId?: string | null;
  kind?: 'document' | 'analysis'; analysis?: object; disabled?: boolean }>()
const busy = ref(false), error = ref(''), saved = ref<Artifact | null>(null)
let requestId = artifactRequestId(), epoch = 0
watch(() => JSON.stringify([props.title, props.content, props.evidence, props.analysis, props.conversationId]), () => {
  ++epoch; saved.value = null; error.value = ''; requestId = artifactRequestId()
})
async function save() {
  const token = epoch
  busy.value = true; error.value = ''
  try {
    const result = await createArtifact({ request_id: requestId, title: props.title.slice(0,240) || '研究成果', content: props.content,
      evidence: props.evidence || [], conversation_id: props.conversationId || undefined, kind: props.kind || 'document', analysis: props.analysis })
    if (token === epoch) saved.value = result
  } catch (err: any) {
    if (token === epoch) {
      const detail = err?.response?.data?.detail
      error.value = typeof detail === 'string' ? detail : err?.message || '保存失败，请重试。'
    }
  } finally { busy.value = false }
}
</script>
<template>
  <span class="save-artifact">
    <RouterLink v-if="saved" :to="{name:'artifact',params:{id:saved.id},query:saved.conversation_id?{conv:saved.conversation_id}:{}}">编辑已保存成果</RouterLink>
    <button v-else type="button" class="el-button" :disabled="busy || disabled" @click="save">{{ busy ? '保存中…' : kind === 'analysis' ? '保存数据和复算配置' : '保存为可编辑成果' }}</button>
    <span v-if="error" role="alert" class="err">{{ error }}</span>
  </span>
</template>
<style scoped>
.save-artifact { display:inline-flex; flex-wrap:wrap; align-items:center; gap:10px; }
.err { color:var(--fn-danger-text, #b42318); font-size:12px; }
a { color:var(--fn-brand); font-size:12px; }
</style>
