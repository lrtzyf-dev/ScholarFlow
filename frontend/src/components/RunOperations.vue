<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { getRunOperations, reconcileRunOperation, type RunOperations, type RunOperation, type Reconciliation } from '../operationApi'
import ResearchChildrenPanel from './ResearchChildrenPanel.vue'
const props = defineProps<{ runId: string; conversationId?: string | null }>()
const emit = defineEmits<{ changed: []; parent: [] }>()
const data = ref<RunOperations | null>(null), chosen = ref<RunOperation | null>(null)
const reference = ref(''), acknowledged = ref(false), busy = ref(false), saving = ref(false), error = ref(''), notice = ref('')
const labels = { executed: '用户记录已执行', not_executed: '用户记录未执行', unknown: '仍无法确认' }
let generation = 0, disposed = false, requestId = '', requestKey = ''
function failure(value: unknown) { const detail = (value as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail; return typeof detail === 'string' ? detail : '操作记录未能读取或保存，请重试' }
async function load() {
  if (saving.value) return
  const token = ++generation, run = props.runId
  busy.value = true
  try { const value = await getRunOperations(run); if (!disposed && token === generation && run === props.runId) { data.value = value; error.value = ''; chosen.value = null } }
  catch (cause) { if (!disposed && token === generation) error.value = failure(cause) }
  finally { if (!disposed && token === generation) busy.value = false }
}
function select(item: RunOperation) { chosen.value = item; reference.value = ''; acknowledged.value = false; requestId = ''; notice.value = '' }
async function resolve(action: Reconciliation['action']) {
  const item = chosen.value, current = data.value, run = props.runId
  if (!item || !current?.can_reconcile || saving.value || !reference.value.trim() || !acknowledged.value) return
  const key = JSON.stringify([run, current.parent.revision, current.parent.attempt_id, item.operation_key, item.signature, action, reference.value.trim()])
  if (key !== requestKey || !requestId) { requestKey = key; requestId = crypto.randomUUID() }
  saving.value = true; error.value = notice.value = ''
  try {
    await reconcileRunOperation(run, { request_id: requestId, expected_parent_revision: current.parent.revision,
      expected_attempt_id: current.parent.attempt_id, operation_key: item.operation_key, signature: item.signature, action, reference: reference.value.trim() })
    if (disposed || props.runId !== run) return
    notice.value = action === 'not_executed' ? '已记录未执行及单次重试许可。回到原任务恢复后，仍会检查当前模型、来源、权限与预算。' : action === 'executed' ? '已记录用户核对结论，保留成果核验缺项，不再自动重复该操作。' : '已记录仍无法确认，保留缺项及不重发围栏。'
    saving.value = false; await load(); emit('changed')
  } catch (cause) { if (!disposed && props.runId === run) error.value = failure(cause) }
  finally { if (!disposed && props.runId === run) saving.value = false }
}
watch(() => props.runId, () => { generation++; chosen.value = null; data.value = null; reference.value = notice.value = error.value = ''; saving.value = false; requestId = ''; void load() })
onMounted(() => { void load() })
onBeforeUnmount(() => { disposed = true; generation++ })
</script>
<template>
  <section class="run-operations" aria-label="原任务操作回执与核对">
    <ResearchChildrenPanel v-if="conversationId" :run-id="runId" :conversation-id="conversationId" @parent="emit('parent')" @changed="emit('changed')" />
    <header><h3>结果待核对的操作</h3><button :disabled="busy || saving" @click="load">刷新操作</button></header>
    <p v-if="data">{{ data.notice }}</p>
    <p v-if="data?.parent.active">任务仍在运行。请先停止并等待结束后核对，全部权限模式也不会自动确认。</p>
    <p v-if="data && !data.items.length">当前没有记录为结果未知的操作。</p>
    <ol v-if="data"><li v-for="item in data.items" :key="item.operation_key">
      <button class="operation" :aria-pressed="chosen?.operation_key === item.operation_key" :disabled="saving" @click="select(item)"><strong>{{ item.tool_name }}</strong><span>{{ labels[item.reported_state] }}<template v-if="item.retry_consumed"> · 单次许可已消耗</template></span></button>
      <p>{{ item.operation_key }} · {{ item.status === 'resolved' ? '已有后续真实结果' : '成果仍需核验' }}</p>
    </li></ol>
    <div v-if="chosen" class="review">
      <h4>{{ chosen.tool_name }}</h4><pre>{{ JSON.stringify(chosen.arguments, null, 2) }}</pre><p>{{ chosen.error }}</p>
      <p>请核对磁盘成果或外部服务记录。“已执行”不表示成果正确；“未执行”只允许这一个操作重试一次，可能再次产生费用。</p>
      <label>核对依据<textarea v-model="reference" :disabled="saving" maxlength="2000" aria-label="操作核对依据" placeholder="填写已查看的成果、外部请求记录或无法确认的原因" /></label>
      <label class="ack"><input v-model="acknowledged" :disabled="saving" type="checkbox" />我已核对并理解此声明不代表供应商验证</label>
      <div class="actions"><button v-for="(label, action) in { executed: '记录已执行', not_executed: '确认未执行并允许重试一次', unknown: '记录仍无法确认' }" :key="action" :disabled="saving || !data?.can_reconcile || chosen.status !== 'open' || !acknowledged || !reference.trim()" @click="resolve(action)">{{ label }}</button></div>
      <details v-if="chosen.reconciliations.length"><summary>核对历史（保留原声明）</summary><p v-for="receipt in chosen.reconciliations" :key="receipt.id">{{ labels[receipt.action] }}：{{ receipt.reference }} · 用户核对，供应商未验证</p></details>
    </div>
    <button v-if="data?.items.length" :disabled="saving" @click="emit('parent')">回到原任务处理恢复</button>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>
<style scoped>
.run-operations{border-top:1px solid var(--fn-border-primary);padding:14px 0;margin-top:12px;color:var(--fn-text-primary);font-family:inherit}header,.actions{display:flex;gap:8px;justify-content:space-between;align-items:center;flex-wrap:wrap}h3{font-size:15px;margin:0}h4{font-size:13px}p{font-size:12px;line-height:1.7;color:var(--fn-text-secondary);overflow-wrap:anywhere}button,textarea{font:inherit;font-size:12px;color:inherit;background:var(--fn-bg-primary);border:1px solid var(--fn-border-primary);border-radius:6px;padding:7px 10px}button{cursor:pointer}button:disabled{opacity:.5;cursor:default}ol{list-style:none;padding:0}li{padding:8px 0;border-bottom:1px solid var(--fn-border-primary)}.operation{display:flex;justify-content:space-between;gap:12px;text-align:left;width:100%;flex-wrap:wrap}.operation[aria-pressed=true]{border-color:var(--fn-brand)}.operation span{font-size:12px}.review{padding:12px;background:var(--fn-bg-secondary);border-radius:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:200px;overflow:auto;font-size:12px}label{display:grid;gap:6px;font-size:12px;margin:10px 0}textarea{box-sizing:border-box;width:100%;min-height:72px;resize:vertical}.ack{display:flex;align-items:flex-start}summary{font-size:12px;cursor:pointer}button:focus-visible,textarea:focus-visible{outline:2px solid var(--fn-brand);outline-offset:2px}[role=alert]{color:#b42318}@media(max-width:600px){.review{padding:8px}.actions{display:grid;justify-content:stretch}}
</style>
