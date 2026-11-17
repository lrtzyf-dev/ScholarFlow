<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { getChildTree, getChildAttempts, getChildCalls, reconcileChild, type ChildTree, type ChildAttempts, type ChildCalls } from '../subtaskApi'
import { priceStatus } from '../pricingApi'
import RunOperations from './RunOperations.vue'
const props = defineProps<{ runId: string }>()
const emit = defineEmits<{ parent: []; changed: [] }>()
const data = ref<ChildTree | null>(null), detail = ref<ChildAttempts | null>(null)
const calls = ref<ChildCalls | null>(null), callAttempt = ref<number | null>(null)
const busy = ref(false), saving = ref(false), error = ref(''), notice = ref(''), reference = ref(''), acknowledged = ref(false)
const states: Record<string, string> = { pending: '待派发', running: '进行中', completed: '归纳已返回，待主任务核验', partial: '部分完成', blocked: '等待处理', failed: '失败', cancelled: '已停止', interrupted: '已中断', stale: '来源已变化' }
let generation = 0, detailGeneration = 0, callGeneration = 0, disposed = false, requestId = '', requestKey = ''
let timer: ReturnType<typeof setInterval> | undefined
function failure(e: unknown) {
  const message = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof message === 'string' ? message : '子任务记录读取或保存失败，请重试'
}
async function load(more = false) {
  if (saving.value || more && (busy.value || data.value?.next_offset == null)) return
  const token = ++generation, run = props.runId
  busy.value = true
  try {
    const value = await getChildTree(run, more ? data.value?.next_offset || 0 : 0)
    if (disposed || token !== generation || run !== props.runId) return
    data.value = more && data.value ? { ...value, items: [...data.value.items, ...value.items] } : value
    error.value = ''
  } catch (e) { if (!disposed && token === generation) error.value = failure(e) }
  finally { if (!disposed && token === generation) busy.value = false }
}
async function inspect(id: string, more = false) {
  if (saving.value) return
  const token = ++detailGeneration, run = props.runId
  try {
    const value = await getChildAttempts(id, more ? detail.value?.next_offset || 0 : 0)
    if (disposed || token !== detailGeneration || run !== props.runId) return
    detail.value = more && detail.value ? { ...value, items: [...detail.value.items, ...value.items] } : value
    if (!more) { reference.value = ''; acknowledged.value = false; requestId = ''; calls.value = null; callAttempt.value = null; callGeneration++ }
    error.value = ''
  } catch (e) { if (!disposed && token === detailGeneration) error.value = failure(e) }
}
async function inspectCalls(attempt: number, more = false) {
  if (!detail.value) return
  const id = detail.value.node.id, token = ++callGeneration
  try {
    const value = await getChildCalls(id, attempt, more ? calls.value?.next_offset || 0 : 0)
    if (disposed || token !== callGeneration || detail.value?.node.id !== id) return
    calls.value = more && calls.value ? { ...value, items: [...calls.value.items, ...value.items] } : value
    callAttempt.value = attempt
  } catch (e) { if (!disposed && token === callGeneration) error.value = failure(e) }
}
async function resolve(action: 'retry' | 'skip') {
  const current = detail.value, parent = data.value?.parent
  if (!current?.node.progress.pending || !parent || saving.value || !acknowledged.value || !reference.value.trim()) return
  const run = props.runId, node = current.node
  const key = JSON.stringify([node.id, node.attempt, parent.revision, current.node.progress.pending.fingerprint, action, reference.value.trim()])
  if (key !== requestKey || !requestId) { requestKey = key; requestId = crypto.randomUUID() }
  saving.value = true; error.value = notice.value = ''
  try {
    await reconcileChild(node.id, { request_id: requestId, expected_parent_revision: parent.revision, expected_attempt: node.attempt,
      operation_fingerprint: current.node.progress.pending.fingerprint, action, reference: reference.value.trim() })
    if (disposed || props.runId !== run) return
    notice.value = action === 'skip' ? '已记录跳过，该调用仍是结果未知；回到所属任务继续补齐。' : '已记录允许重试；请回到所属任务恢复，派发前仍会检查当前权限与预算。'
    saving.value = false
    await load(); await inspect(node.id); emit('changed')
  } catch (e) { if (!disposed && run === props.runId) error.value = failure(e) }
  finally { if (!disposed && run === props.runId) saving.value = false }
}
watch([reference, acknowledged], () => { requestId = '' })
watch(() => props.runId, () => { generation++; detailGeneration++; callGeneration++; calls.value = null; callAttempt.value = null; data.value = detail.value = null; error.value = notice.value = reference.value = ''; saving.value = false; acknowledged.value = false; requestId = ''; void load() })
onMounted(() => { void load(); timer = setInterval(() => { if (!document.hidden && !busy.value && !saving.value && data.value?.parent.active) void load() }, 5000) })
onBeforeUnmount(() => { disposed = true; generation++; detailGeneration++; callGeneration++; if (timer) clearInterval(timer) })
</script>
<template>
  <section class="subtask-tree" aria-label="持久子任务与尝试详情">
    <RunOperations :run-id="runId" :conversation-id="data?.parent.conversation_id" @parent="emit('parent')" @changed="emit('changed')" />
    <header><strong>子任务与执行记录</strong><button :disabled="busy || saving" @click="load()">刷新子任务</button></header>
    <p v-if="busy" role="status">正在读取执行记录…</p>
    <template v-if="data">
      <p>{{ data.parent.title }} · {{ data.total }} 个子任务</p>
      <p>{{ data.recovery.message }}</p>
      <button class="parent-link" @click="emit('parent')">回到所属任务处理恢复</button>
      <p v-if="!data.items.length">此任务尚无专题归纳子任务。普通短问答可以直接完成。</p>
      <ol class="child-list"><li v-for="node in data.items" :key="node.id">
        <button :aria-pressed="detail?.node.id === node.id" :disabled="saving" @click="inspect(node.id)"><strong>{{ node.topic }}</strong><span>{{ states[node.state] || node.state }}</span><span>第 {{ node.attempt }} 次尝试 · 已保存 {{ node.progress.completed_reads }} 次工具结果</span></button>
        <p v-if="node.progress.pending">结果待确认：{{ node.progress.pending.name }}</p>
      </li></ol>
      <button v-if="data.next_offset !== null" :disabled="busy || saving" @click="load(true)">更多子任务</button>
    </template>
    <div v-if="detail" class="child-detail">
      <h3>{{ detail.node.topic }}</h3><p v-if="detail.node.goal">目标：{{ detail.node.goal }}</p>
      <p>子任务与主任务共享预算；返回归纳不代表研究目标已经完成。</p>
      <form v-if="detail.node.progress.pending?.name.startsWith('mcp__')" class="reconciliation" @submit.prevent>
        <h4>核对未确认的外部调用</h4><p>{{ detail.node.progress.pending.name }}</p>
        <pre>{{ JSON.stringify(detail.node.progress.pending.arguments, null, 2) }}</pre>
        <p>请先查阅外部服务的结果或请求记录。选择重试可能再次产生请求和费用；跳过会保留缺项，不会生成成功证据。</p>
        <label>核对依据<textarea v-model="reference" aria-label="子任务核对依据" :disabled="saving" maxlength="2000" placeholder="已查阅的外部请求记录、结果或处理说明" /></label>
        <label class="check"><input v-model="acknowledged" type="checkbox" :disabled="saving" aria-label="确认已核对子任务外部操作" />我已核对外部状态并选择后续处理</label>
        <div class="actions"><button :disabled="saving || !data?.recovery.can_reconcile || !acknowledged || !reference.trim()" @click="resolve('retry')">记录允许重试</button><button :disabled="saving || !data?.recovery.can_reconcile || !acknowledged || !reference.trim()" @click="resolve('skip')">记录跳过并保留缺项</button></div>
      </form>
      <h4>各次尝试（{{ detail.total }}）</h4>
      <details v-for="attempt in detail.items" :key="attempt.number" class="attempt">
        <summary>第 {{ attempt.number }} 次 · {{ states[attempt.state] || attempt.state }} · {{ attempt.progress.completed_reads }} 次工具结果</summary>
        <p v-if="attempt.progress.tools.length">工具：{{ attempt.progress.tools.join('、') }}</p>
        <p v-if="attempt.progress.pending">本次尝试停在：{{ attempt.progress.pending.name }}。原始记录保留，后续核对另行列出。</p>
        <p v-if="attempt.result.findings" class="findings">{{ attempt.result.findings }}</p>
        <ul v-if="attempt.result.incomplete.length"><li v-for="(item, i) in attempt.result.incomplete" :key="i">{{ item }}</li></ul>
        <p v-if="attempt.progress.loaded_skills.length">本次加载技能：{{ attempt.progress.loaded_skills.map(s => s.name).join('、') }}</p>
        <button @click="inspectCalls(attempt.number)">查看本次调用与用量</button>
        <div v-if="callAttempt === attempt.number && calls" class="attempt-calls"><p>{{ calls.notice }}</p><p v-if="!calls.items.length">本次尝试尚无可归属的调用记录。</p><p v-for="call in calls.items" :key="call.id">{{ call.model_id || call.service }} · {{ call.service }} · {{ call.state }}<br />{{ priceStatus[call.pricing.status] || call.pricing.status }}：{{ call.pricing.amount ?? '金额未知' }} {{ call.pricing.currency || '' }}<br />用量（{{ call.usage_source === 'actual' ? '供应商回传' : call.usage_source === 'estimated' ? '估算' : '未知' }}）：{{ JSON.stringify(call.usage_source === 'actual' ? call.actual_usage : call.estimated_usage) }}</p><button v-if="calls.next_offset !== null" @click="inspectCalls(attempt.number, true)">更多调用</button></div>
      </details>
      <button v-if="detail.next_offset !== null" :disabled="saving" @click="inspect(detail.node.id, true)">更早的尝试</button>
      <details v-if="detail.resolutions.length"><summary>外部调用核对记录</summary><p v-for="receipt in detail.resolutions" :key="receipt.id">第 {{ receipt.attempt }} 次 · {{ receipt.action === 'retry' ? '用户允许重试' : '用户选择跳过' }}：{{ receipt.reference }}（人工核对）</p></details>
    </div>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>
<style scoped>
.subtask-tree{border:1px solid var(--fn-border-primary);border-radius:10px;padding:16px;margin:12px 0;background:var(--fn-bg-primary);color:var(--fn-text-primary);min-width:0}header,.actions{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}button,textarea{font:inherit;font-size:12px;color:inherit;border:1px solid var(--fn-border-primary);border-radius:6px;padding:7px 10px;background:var(--fn-bg-primary)}button{cursor:pointer}button:disabled{opacity:.5;cursor:default}p{font-size:12px;line-height:1.7;color:var(--fn-text-secondary);overflow-wrap:anywhere}h3{font-size:15px}h4{font-size:13px}ol{list-style:none;padding:0}.child-list li{border-left:2px solid var(--fn-border-primary);padding:4px 0 4px 12px;margin:8px 0}.child-list button{width:100%;text-align:left;display:flex;gap:8px;flex-wrap:wrap;overflow-wrap:anywhere}.child-list button[aria-pressed=true]{border-color:var(--fn-brand)}.child-list span{font-size:12px}.child-detail{border-top:1px solid var(--fn-border-primary);margin-top:16px}.reconciliation{padding:12px;background:var(--fn-bg-secondary);border-radius:8px}label{display:grid;gap:6px;font-size:12px;margin:10px 0}textarea{min-height:60px;resize:vertical;box-sizing:border-box;width:100%}.check{display:flex;align-items:flex-start}pre{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:180px;overflow:auto}.attempt{padding:10px 0;border-bottom:1px solid var(--fn-border-primary)}summary{cursor:pointer;font-size:12px;line-height:1.7}.findings{white-space:pre-wrap}ul{font-size:12px;line-height:1.7}button:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--fn-brand);outline-offset:2px}[role=alert]{color:#b42318}@media(max-width:600px){.subtask-tree{padding:10px}.child-list button{display:grid}}
</style>
