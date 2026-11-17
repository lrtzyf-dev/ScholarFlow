<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import DiagnosticHistory from './DiagnosticHistory.vue'
import {
  inspectLocal,
  testRemote,
  type LocalReport,
  type ProbeResult,
  type ProbeTarget,
} from '../diagnosticApi'

const props = defineProps<{ conversationId?: string; runId?:string; sourceRevisionId?:string }>()
defineEmits<{ close: [] }>()
const report = ref<LocalReport | null>(null)
const result = ref<ProbeResult | null>(null)
const loading = ref(false)
const probing = ref(false)
const error = ref('')
let epoch = 0
onUnmounted(() => {
  epoch++
})

async function inspect() {
  const token = ++epoch
  loading.value = true
  probing.value = false
  report.value = null
  result.value = null
  error.value = ''
  try {
    const value = await inspectLocal(props.conversationId)
    if (token === epoch) report.value = value
  } catch {
    if (token === epoch) error.value = '本地检查暂时不可用，请重试。'
  } finally {
    if (token === epoch) loading.value = false
  }
}
async function probe(target: ProbeTarget) {
  if (!report.value || probing.value || !target.available) return
  const token = epoch
  const cid = report.value.conversation_id
  probing.value = true
  error.value = ''
  result.value = null
  try {
    const value = await testRemote(target, cid, {run_id:props.runId,source_revision_id:props.sourceRevisionId})
    if (token === epoch) result.value = value
  } catch (cause: unknown) {
    if (token === epoch) {
      const status = (cause as { response?: { status?: number } })?.response?.status
      error.value =
        status === 409
          ? '配置已变化或不可用。请重新运行本地检查，确认目标后再测试。'
          : '测试请求未能完成；服务端可能已收到请求，请核查后再试。'
    }
  } finally {
    if (token === epoch) probing.value = false
  }
}
watch(() => props.conversationId, inspect, { immediate: true })
</script>

<template>
  <ElDialog :model-value="true" title="配置诊断" width="min(700px, calc(100vw - 32px))" append-to-body :close-on-click-modal="false" @close="$emit('close')">
  <section class="diagnostics-panel">
    <p class="muted">本地检查不会调用模型或 Embedding。接口测试由你单独启动。</p>
    <p v-if="loading" role="status">正在检查本地环境…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <div class="summary">
        {{ report.summary.ok }} 项正常 · {{ report.summary.warn }} 项提示 ·
        {{ report.summary.error }} 项异常
      </div>
      <div class="check-list">
        <article v-for="check in report.checks" :key="check.id" :class="check.status">
          <strong>{{ check.label }}</strong>
          <p>{{ check.detail }}</p>
        </article>
      </div>
      <h3>主动接口测试</h3>
      <p class="muted">
        仅发送固定测试文本，不发送会话或文献内容。每次点击会发起一次请求，可能产生费用；测试不会修改你的配置。
      </p>
      <article v-for="target in report.probe_targets" :key="target.service" class="target">
        <strong
          >{{ target.label }}<span v-if="target.model_id"> · {{ target.model_id }}</span></strong
        >
        <p v-if="target.endpoint" class="endpoint">{{ target.endpoint }}</p>
        <p v-if="!target.available">{{ target.detail }}</p>
        <button class="btn-ghost" :disabled="probing || !target.available" @click="probe(target)">
          {{ target.service === 'llm' ? '测试模型接口（可能收费）' : '测试 Embedding（可能收费）' }}
        </button>
      </article>
      <p v-if="probing" role="status">正在测试接口；关闭页面不会撤回已发送的请求。</p>
      <div v-if="result" :class="['probe-result', result.status]" role="status">
        <strong
          >{{ result.target.label }}：{{ result.status === 'ok' ? '测试成功' : '测试失败' }}</strong
        >
        <p>{{ result.detail }}</p>
        <p>已记录 {{ result.calls.length }} 次调用；费用以供应商账单为准。</p>
        <p v-for="call in result.calls" :key="call.call_id" class="muted">
          调用 {{ call.call_id }} · {{ call.usage_source === 'actual' ? '已收到用量' : '用量未知' }}
        </p>
      </div>
    </template>
    <DiagnosticHistory :conversation-id="conversationId" :run-id="runId" :source-revision-id="sourceRevisionId" :refresh-key="result?.diagnostic_id" @inspect="inspect" />
    <footer>
      <button class="btn-ghost" :disabled="loading || probing" @click="inspect">
        重新运行本地检查
      </button>
    </footer>
  </section>
  </ElDialog>
</template>

<style scoped>
.diagnostics-panel {
  width: 100%;
  max-height: 72vh;
  overflow: auto;
  background: var(--card);
  color: var(--text);
  padding: 4px;
  box-sizing: border-box;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  margin: 0;
}
h3 {
  font-size: 14px;
  margin: 20px 0 8px;
}
p {
  font-size: 13px;
  line-height: 1.6;
  margin: 6px 0;
  overflow-wrap: anywhere;
}
.muted {
  color: var(--text-2);
}
.summary {
  font-size: 13px;
  margin: 16px 0;
}
.check-list article,
.target {
  border-bottom: 1px solid var(--border);
  padding: 10px 0;
}
strong {
  font-size: 13px;
  overflow-wrap: anywhere;
}
.endpoint {
  font-family: monospace;
}
.error {
  color: var(--danger);
}
.warn {
  color: var(--warning);
}
.probe-result {
  padding: 12px 0;
}
footer {
  margin-top: 16px;
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-ghost {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--card);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  padding: 7px 10px;
  cursor: pointer;
}
.btn-ghost:hover:not(:disabled) {
  background: var(--accent-bg);
}
.btn-ghost:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
</style>
