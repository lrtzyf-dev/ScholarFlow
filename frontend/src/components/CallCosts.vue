<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { listPricedCalls, getPricedCall, confirmCallCharge, pricingError, priceStatus, type PricedCall } from '../pricingApi'
const props = defineProps<{ conversationId: string }>()
const emit = defineEmits<{ changed: [] }>()
const items = ref<PricedCall[]>([]), selected = ref<PricedCall | null>(null), busy = ref(false), error = ref(''), notice = ref('')
const amount = ref(''), currency = ref('USD'), reference = ref(''), offset = ref(0), more = ref(false)
let epoch = 0, selectionEpoch = 0, disposed = false
async function load() {
  const current = ++epoch, cid = props.conversationId
  try { const value = await listPricedCalls(cid, offset.value); if (!disposed && current === epoch && cid === props.conversationId) { items.value = value.items; more.value = value.has_more } }
  catch (e) { if (!disposed && current === epoch && cid === props.conversationId) error.value = pricingError(e) }
}
async function open(call: PricedCall) {
  if (busy.value) return
  const cid = props.conversationId, current = ++selectionEpoch
  try {
    const value = await getPricedCall(cid, call.id)
    if (disposed || cid !== props.conversationId || current !== selectionEpoch) return
    selected.value = value; amount.value = ''; reference.value = ''; currency.value = value.pricing.currency || 'USD'; notice.value = error.value = ''
  } catch (e) { if (!disposed && cid === props.conversationId && current === selectionEpoch) error.value = pricingError(e) }
}
async function confirm() {
  if (!selected.value || busy.value) return
  const target = selected.value, cid = props.conversationId, current = ++selectionEpoch
  busy.value = true; error.value = notice.value = ''
  try {
    await confirmCallCharge(cid, target.id, { expected_revision: target.pricing.revision, amount: amount.value, currency: currency.value, reference: reference.value })
    if (disposed || cid !== props.conversationId || current !== selectionEpoch) return
    const value = await getPricedCall(cid, target.id)
    if (disposed || cid !== props.conversationId || current !== selectionEpoch) return
    selected.value = value
    notice.value = '已记录用户账单核对，保留修改历史，并更新同币种任务预算。'; emit('changed'); await load()
  } catch (e) { if (!disposed && cid === props.conversationId && current === selectionEpoch) error.value = pricingError(e) }
  finally { if (!disposed && cid === props.conversationId && current === selectionEpoch) busy.value = false }
}
watch(() => props.conversationId, () => { ++epoch; ++selectionEpoch; selected.value = null; items.value = []; busy.value = false; amount.value = reference.value = error.value = notice.value = ''; offset.value = 0; void load() }, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++epoch; ++selectionEpoch })
</script>
<template>
  <section class="call-costs" aria-label="调用费用明细">
    <p>每次模型、工具、重试、摘要调用单独记录。供应商回传用量不等于账单已确认；子任务共享父任务预算。</p>
    <button type="button" @click="load">刷新调用明细</button>
    <p v-if="!items.length">此会话暂无真实调用账目。</p>
    <button v-for="call in items" :key="call.id" type="button" class="call-row" :disabled="busy" @click="open(call)">
      <strong>{{ call.model_id || call.service }} · {{ call.service }}</strong><span>{{ priceStatus[call.pricing.status] || call.pricing.status }}</span>
      <span>{{ call.pricing.amount === null ? '费用未知' : `${call.pricing.amount} ${call.pricing.currency}` }} · {{ call.state }}</span>
    </button>
    <div class="pages"><button :disabled="offset === 0" @click="offset -= 20; load()">上一页</button><button :disabled="!more" @click="offset += 20; load()">下一页</button></div>
    <div v-if="selected" class="detail">
      <h4>调用 {{ selected.id.slice(0, 12) }}</h4>
      <p>{{ selected.endpoint }}<br />{{ selected.pricing.tariff_name || '请求开始时未匹配到价目' }}</p>
      <p>用量来源：{{ selected.usage_source === 'actual' ? '供应商回传' : selected.usage_source === 'estimated' ? '估算' : '未知' }}</p>
      <dl><template v-for="(count, unit) in selected.actual_usage" :key="unit"><dt>{{ unit }}</dt><dd>{{ count }}</dd></template></dl>
      <p v-if="selected.pricing.details?.missing_units?.length">缺失用量：{{ selected.pricing.details.missing_units.join('、') }}</p>
      <p v-if="selected.pricing.details?.unpriced_units?.length">未计价单位：{{ selected.pricing.details.unpriced_units.join('、') }}</p>
      <p v-if="selected.pricing.unknown_units?.length">未知单位：{{ selected.pricing.unknown_units.join('、') }}</p>
      <details v-if="selected.tariff"><summary>本次冻结的价目</summary><p v-for="item in selected.tariff.components" :key="item.unit">{{ item.unit }}：{{ item.rate }} {{ selected.tariff.currency }} / {{ item.per }} 单位；扣除：{{ item.subtract_units.join('、') || '无' }}</p></details>
      <form v-if="selected.state !== 'started'" @submit.prevent="confirm">
        <h4>依据账单核对</h4>
        <label>此次实际账单金额<input v-model="amount" required inputmode="decimal" aria-label="实际账单金额" /></label>
        <label>账单币种<select v-model="currency" aria-label="账单币种"><option>USD</option><option>CNY</option><option>EUR</option></select></label>
        <label>核对依据<input v-model="reference" required aria-label="账单核对依据" placeholder="账单日期/行号或供应商请求编号" /></label>
        <button type="submit" :disabled="busy">保存账单核对</button>
        <button type="button" :disabled="busy" @click="open(selected)">重新读取此调用</button>
      </form>
      <details v-if="selected.confirmations?.length"><summary>账单核对历史</summary><p v-for="entry in selected.confirmations" :key="entry.revision">{{ entry.amount }} {{ entry.currency }} · {{ entry.reference }}</p></details>
    </div>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>
<style scoped>
.call-costs { font-size: 12px; overflow-wrap: anywhere; }.call-costs p { line-height: 1.6; }
button { cursor: pointer; background: transparent; color: inherit; border: 1px solid var(--border-color, #dce4ee); border-radius: 6px; padding: 7px 9px; font: inherit; }
button:disabled { opacity: .5; }.call-row { display: flex; flex-direction: column; text-align: left; width: 100%; margin: 8px 0; gap: 4px; }.pages { display: flex; justify-content: space-between; margin: 10px 0; }
.detail { border-top: 1px solid var(--border-color, #dce4ee); }label { display: block; margin: 10px 0; }input, select { width: 100%; box-sizing: border-box; padding: 7px; font: inherit; background: transparent; color: inherit; border: 1px solid var(--border-color, #dce4ee); border-radius: 6px; }
dl { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px; }dd { margin: 0; }
</style>
