<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { createHistoryRevision, type HistoryRevision } from '../historyRevisionsApi'
const props = defineProps<{ target: {conversationId: string; recordId: string; question: string} | null }>()
const emit = defineEmits<{close: []; created: [revision: HistoryRevision]}>()
const question = ref(''), mode = ref<HistoryRevision['mode']>('answer_only'), busy = ref(false), error = ref('')
let requestId = '', requestPayload = ''
watch(() => props.target, value => { question.value = value?.question || ''; mode.value = 'answer_only'; error.value = ''; requestId = ''; requestPayload = '' })
async function create() {
  if (!props.target || busy.value || !question.value.trim()) return
  const target = props.target, payload = JSON.stringify([target, question.value.trim(), mode.value])
  if (payload !== requestPayload) { requestPayload = payload; requestId = crypto.randomUUID() }
  busy.value = true; error.value = ''
  try { const result = await createHistoryRevision(target.conversationId, target.recordId, question.value.trim(), mode.value, requestId); emit('created', result) }
  catch (e: any) { error.value = e?.response?.data?.detail || e.message || '创建修订失败' }
  finally { busy.value = false }
}
</script>
<template>
  <ElDialog :model-value="Boolean(target)" title="编辑问题与修订答案" width="620px" :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy" @close="emit('close')">
    <div class="revision-editor">
      <p>创建从这条问题开始的新分支。原会话及已执行操作保留，后续消息不会混入新答案。</p>
      <label>修订后的问题<textarea aria-label="修订后的问题" v-model="question" :disabled="busy" rows="7" maxlength="100000" /></label>
      <fieldset :disabled="busy"><legend>这次如何处理</legend>
        <label class="mode"><input type="radio" v-model="mode" value="answer_only" />仅修订答案（默认）<small>可读取资料；禁止写文件、命令、上传、付费工具及子任务。模型回答本身仍可能计费。</small></label>
        <label class="mode"><input type="radio" v-model="mode" value="new_task" />作为新任务执行<small>发送后按会话当前权限执行新动作；原任务的操作不会撤销，可能产生新的写入和费用。</small></label>
      </fieldset>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>
    <template #footer><button :disabled="busy" @click="emit('close')">取消</button><button :disabled="busy || !question.trim()" @click="create">{{ busy ? '正在创建…' : '创建分支草稿' }}</button></template>
  </ElDialog>
</template>
<style scoped>
.revision-editor { color: var(--fn-text-primary); line-height: 1.65; } textarea { display: block; box-sizing: border-box; width: 100%; resize: vertical; padding: 10px; border: 1px solid var(--fn-border-primary); border-radius: 8px; color: inherit; background: var(--fn-bg-primary); font: inherit; } fieldset { margin-top: 16px; border: 1px solid var(--fn-border-primary); border-radius: 8px; }.mode { display:block; margin: 12px 0; }.mode small { display:block; margin-left: 22px; color: var(--fn-text-secondary); }.error { color: var(--fn-danger, #b42318); } button { margin-left: 8px; padding:8px 14px; border:1px solid var(--fn-border-primary); background:var(--fn-bg-primary); color:var(--fn-text-primary); border-radius:7px; cursor:pointer; } button:disabled { opacity:.5; } button:focus-visible,textarea:focus-visible { outline:2px solid var(--fn-brand); outline-offset:2px; }
</style>
