<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { getHistoryRevision, type HistoryRevision } from '../historyRevisionsApi'
const props = defineProps<{conversationId: string}>()
const revision = ref<HistoryRevision | null>(null), error = ref(''), router = useRouter()
let epoch = 0
watch(() => props.conversationId, async cid => { const n = ++epoch; revision.value = null; error.value = ''; try { const value = await getHistoryRevision(cid); if(n === epoch) revision.value = value } catch { if(n === epoch) error.value = '修订来源暂时无法读取' } }, {immediate:true})
onBeforeUnmount(() => { epoch++ })
</script>
<template>
  <div v-if="revision" class="revision-banner" role="status">
    <span>修订分支 · {{ revision.mode === 'answer_only' ? '修订运行只读资料，不执行外部动作' : '新任务按当前权限执行' }}。{{ revision.origin_deleted ? '原会话已删除，分支资料独立保留。' : '原会话和已执行操作保留。' }}</span>
    <button v-if="!revision.origin_deleted" @click="router.push({path:'/chat', query:{conv:revision.source_conversation_id, record:revision.source_record_id}})">查看原消息</button>
  </div>
  <div v-else-if="error" class="revision-banner" role="status">{{ error }}</div>
</template>
<style scoped>
.revision-banner { display:flex; gap:10px; flex-wrap:wrap; align-items:center; padding:10px 18px; font-size:12px; color:var(--fn-text-secondary); border-bottom:1px solid var(--fn-border-primary); background:var(--fn-bg-secondary); } button { font:inherit; color:var(--fn-text-primary); border:1px solid var(--fn-border-primary); background:var(--fn-bg-primary); border-radius:6px; padding:5px 8px; cursor:pointer; }
</style>
