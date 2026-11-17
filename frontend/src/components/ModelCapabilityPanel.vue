<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import {
  previewCapabilities,
  probeCapability,
  cancelCapability,
  cleanupCapability,
  type CapabilityName,
  type CapabilityPreview,
  type CapabilityResult,
} from '../modelCapabilitiesApi'

const props = defineProps<{ profileId: string; profileVersion: number }>()
const emit = defineEmits<{ tested: [] }>()
const preview = ref<CapabilityPreview | null>(null)
const busy = ref<CapabilityName | ''>('')
const loading = ref(false)
const error = ref('')
const latest = ref<CapabilityResult | null>(null)
const active = ref<{ profileId: string; diagnosticId: string } | null>(null)
const cancelling = ref(false)
const cancelDetail = ref('')
const cleaning = ref('')
let epoch = 0

function statusLabel(status?: string) {
  return status === 'supported' ? '已验证' : status === 'unsupported' ? '不支持' : '未确认'
}
function costs(result: CapabilityResult) {
  if (!result.request_count) return '本项未发送远端请求。'
  const cost = result.cost_summary
  if (!cost) return '费用尚未确认，请以供应商账单为准。'
  if (cost.synthetic_call_count && !cost.real_call_count) return '合成接口测试，未产生真实供应商请求。'
  const values: string[] = []
  for (const [label, totals] of [['按已记录价格估算', cost.pricing.complete_estimates], ['部分用量估算', cost.pricing.partial_estimates], ['用户核定', cost.pricing.user_confirmed]] as const) {
    for (const [currency, amount] of Object.entries(totals)) values.push(`${label} ${amount} ${currency}`)
  }
  if (cost.unknown_amount_calls) values.push(`${cost.unknown_amount_calls} 次请求费用未知`)
  return values.length ? values.join('；') + '。请以供应商账单核对。' : '尚无可确认费用；未将未知费用记为零。'
}
function failure(reason: unknown) {
  const value = reason as { response?: { status?: number; data?: { detail?: unknown } } }
  const detail = value.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (value.response?.status === 409) return '模型配置已变化，请刷新诊断后重新测试。'
  return typeof detail === 'string' ? detail : '诊断未完成，请刷新后重试。'
}
async function load() {
  const request = ++epoch
  preview.value = null
  latest.value = null
  error.value = ''
  busy.value = ''
  cancelDetail.value = ''
  loading.value = true
  try {
    const result = await previewCapabilities(props.profileId)
    if (request === epoch) preview.value = result
  } catch (reason) {
    if (request === epoch) error.value = failure(reason)
  } finally {
    if (request === epoch) loading.value = false
  }
}
async function test(capability: CapabilityName) {
  if (busy.value || !preview.value) return
  const request = epoch
  const target = preview.value
  const diagnosticId = 'capability-' + crypto.randomUUID().replaceAll('-', '')
  active.value = { profileId: target.profile_id, diagnosticId }
  busy.value = capability
  cancelDetail.value = ''
  error.value = ''
  try {
    const result = await probeCapability(target.profile_id, capability, target.expected_config, diagnosticId)
    if (request !== epoch) return
    latest.value = result
    if (result.failure_kind === 'configuration_changed') {
      error.value = '配置在测试期间变化，本结果未写入当前模型。请刷新后重试。'
      preview.value = null
      return
    }
    preview.value = {
      ...target,
      capabilities: result.failure_kind === 'cancelled' ? target.capabilities : { ...target.capabilities, [capability]: result.status },
      results: result.failure_kind === 'cancelled' ? target.results : { ...target.results, [capability]: result },
    }
    if (result.remote_file && ['upload_pending', 'available', 'cleanup_pending'].includes(result.remote_file.state)) {
      preview.value.cleanup_operations = [...(preview.value.cleanup_operations || []), { diagnostic_id: diagnosticId, remote_file: result.remote_file }]
    }
    emit('tested')
  } catch (reason) {
    if (request === epoch) error.value = failure(reason)
  } finally {
    if (active.value?.diagnosticId === diagnosticId) active.value = null
    if (request === epoch) busy.value = ''
  }
}
async function cancelTest() {
  const target = active.value
  if (!target || cancelling.value) return
  const request = epoch
  cancelling.value = true
  try {
    const result = await cancelCapability(target.profileId, target.diagnosticId)
    if (request === epoch) cancelDetail.value = result.detail
  } catch (reason) { if (request === epoch) error.value = failure(reason) }
  finally { cancelling.value = false }
}
async function clean(diagnosticId: string) {
  if (!preview.value || cleaning.value) return
  const request = epoch
  const profileId = preview.value.profile_id
  cleaning.value = diagnosticId
  try {
    await cleanupCapability(profileId, diagnosticId)
    if (request === epoch && preview.value) preview.value.cleanup_operations = preview.value.cleanup_operations.filter(v => v.diagnostic_id !== diagnosticId)
  } catch (reason) { if (request === epoch) error.value = failure(reason) }
  finally { cleaning.value = '' }
}
watch(() => [props.profileId, props.profileVersion], () => {
  if (active.value) void cancelTest()
  active.value = null
  void load()
}, { immediate: true })
onBeforeUnmount(() => { if (active.value) void cancelTest(); epoch++ })
</script>

<template>
  <section class="capability-panel" aria-label="模型分项诊断">
    <header><strong>分项诊断</strong><button type="button" :disabled="Boolean(busy) || loading" @click="load">刷新</button></header>
    <p>打开此面板不会调用模型。点击测试后发送合成材料，可能产生费用；各项结果独立记录。</p>
    <p v-if="loading" role="status">正在读取本地配置…</p>
    <p v-if="error" class="capability-error" role="alert">{{ error }}</p>
    <p v-if="active" role="status">正在执行主动测试。<button type="button" :disabled="cancelling" @click="cancelTest">{{ cancelling ? '正在取消…' : '取消本项测试' }}</button></p>
    <p v-if="cancelDetail" role="status">{{ cancelDetail }}</p>
    <div v-if="preview" class="capability-list">
      <div v-for="probe in preview.probes" :key="probe.capability" class="capability-row">
        <div><strong>{{ probe.label }}</strong><small>{{ probe.sends }} · 最多 {{ probe.max_requests }} 次请求</small></div>
        <span :class="['capability-state', preview.capabilities[probe.capability]]">
          {{ preview.results[probe.capability] ? statusLabel(preview.capabilities[probe.capability]) : preview.capabilities[probe.capability] === 'supported' ? '已验证' : '未检测' }}
        </span>
        <button type="button" :disabled="Boolean(busy)" :aria-label="`测试${probe.label}`" @click="test(probe.capability)">
          {{ busy === probe.capability ? '测试中…' : '测试' }}
        </button>
      </div>
    </div>
    <p v-if="latest" role="status" class="capability-result">{{ latest.detail }} 已记录 {{ latest.request_count }} 次请求。</p>
    <p v-if="latest" class="capability-cost">{{ costs(latest) }}</p>
    <small v-if="latest?.endpoint" class="capability-binding">{{ latest.endpoint }} · {{ latest.model_id }} · 配置 {{ latest.profile_version }} · {{ latest.tested_at }}</small>
    <div v-if="preview?.cleanup_operations?.length" class="cleanup-list">
      <p>以下合成测试文件的远端删除尚未确认。过期时间为上传时请求的期限，请以供应商实际状态为准。</p>
      <p v-for="item in preview.cleanup_operations" :key="item.diagnostic_id">
        {{ item.remote_file.id || '上传结果未知，远端文件编号未确认' }}
        <button v-if="item.remote_file.id" type="button" :disabled="Boolean(busy) || Boolean(cleaning)" @click="clean(item.diagnostic_id)">清理测试文件</button>
        <span v-else>请在供应商控制台核对。</span>
      </p>
    </div>
    <small>诊断通过不代表所有复杂任务都已验证。未返回有效结果、网络失败和限流均保留为“未确认”。</small>
  </section>
</template>

<style scoped>
.capability-panel { margin-top: 10px; padding: 12px; border: 1px solid var(--fn-border-primary, #dfe5ef); border-radius: 6px; color: var(--fn-text-primary, #172b4d); min-width: 0; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
p, small { font-size: 11px; line-height: 1.6; color: var(--fn-text-secondary, #64748b); }
p { margin: 8px 0; }
strong { font-size: 12px; font-weight: 600; }
button { font: inherit; font-size: 11px; padding: 5px 8px; border: 1px solid var(--fn-border-primary, #dfe5ef); border-radius: 5px; background: transparent; color: inherit; cursor: pointer; flex-shrink: 0; }
button:disabled { opacity: .55; cursor: default; }
.capability-list { margin: 8px 0; }
.capability-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 10px; padding: 8px 0; border-top: 1px solid var(--fn-border-primary, #dfe5ef); }
.capability-row small { display: block; }
.capability-state { font-size: 11px; white-space: nowrap; color: var(--fn-text-secondary, #64748b); }
.capability-state.supported { color: #24734a; }
.capability-state.unsupported, .capability-error { color: #b42318; }
.capability-result { overflow-wrap: anywhere; }
.capability-binding { display: block; overflow-wrap: anywhere; margin: 8px 0; }
@media (max-width: 700px) { .capability-row { gap: 6px; } }
</style>
