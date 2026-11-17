<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { listPriceVersions, createPriceVersion, disablePriceVersion, pricingError, type PriceVersion } from '../pricingApi'
const versions = ref<PriceVersion[]>([]), units = ref<string[]>([]), error = ref(''), notice = ref(''), busy = ref(false)
const expanded = ref(false)
const form = reactive({ name: '', endpoint: '', service: 'chat', model_id: '', profile_id: '', currency: 'USD',
  source_reference: '', complete_contract: false, informational: 'total_tokens',
  components: [{ unit: 'prompt_tokens', subtract: '', rate: '', per: '1000000' }, { unit: 'completion_tokens', subtract: '', rate: '', per: '1000000' }] })
const split = (text: string) => text.split(/[,，\s]+/).filter(Boolean)
async function load() {
  try { const result = await listPriceVersions(); versions.value = result.items; units.value = result.units }
  catch (e) { error.value = pricingError(e) }
}
async function save() {
  busy.value = true; error.value = notice.value = ''
  try {
    await createPriceVersion({ name: form.name, endpoint: form.endpoint, service: form.service, model_id: form.model_id || null,
      profile_id: form.profile_id || null, currency: form.currency, source_reference: form.source_reference, complete_contract: form.complete_contract,
      informational_units: split(form.informational), components: form.components.map(c => ({ unit: c.unit, subtract_units: split(c.subtract), rate: c.rate, per: c.per })) })
    await load(); notice.value = '新价目已启用，只应用于后续调用。已开始的调用仍保留原价目版本。'
  } catch (e) { error.value = pricingError(e) }
  finally { busy.value = false }
}
async function disable(id: string) {
  busy.value = true
  try { await disablePriceVersion(id); await load() } catch (e) { error.value = pricingError(e) } finally { busy.value = false }
}
function copy(version: PriceVersion) {
  Object.assign(form, { ...version, name: version.name + ' · 修订', model_id: version.model_id || '', profile_id: version.profile_id || '',
    complete_contract: Boolean(version.complete_contract), informational: version.informational_units.join(', '),
    components: version.components.map(c => ({ unit: c.unit, subtract: c.subtract_units.join(', '), rate: c.rate, per: c.per })) })
}
onMounted(load)
</script>
<template>
  <section class="pricing-settings">
    <button class="heading" type="button" :aria-expanded="expanded" @click="expanded = !expanded">调用价目与计费覆盖 {{ expanded ? '−' : '+' }}</button>
    <p>费用估算需要明确价目和完整用量。这里不预置猜测的官方价格；币种不自动换算，账单金额可在会话预算中核对。</p>
    <div v-if="expanded">
      <div v-for="version in versions" :key="version.id" class="version">
        <strong>{{ version.name }}</strong><span>{{ version.active ? '后续调用使用' : '已停用，历史保留' }}</span>
        <small>{{ version.model_id || '无模型服务' }} · {{ version.service }} · {{ version.currency }} · {{ version.complete_contract ? '已声明完整覆盖' : '部分价目' }}</small>
        <small>{{ version.endpoint }}</small>
        <div><button type="button" @click="copy(version)">复制为新版本</button><button v-if="version.active" type="button" :disabled="busy" @click="disable(version.id)">停用</button></div>
      </div>
      <form @submit.prevent="save">
        <h3>新增价目版本</h3>
        <label>价目名称<input v-model="form.name" required aria-label="价目名称" /></label>
        <label>完整请求端点<input v-model="form.endpoint" required type="url" aria-label="计价请求端点" placeholder="https://api.example.com/v1/chat/completions" /></label>
        <label>服务名称<input v-model="form.service" required aria-label="计价服务" placeholder="chat（包含流式）、embedding 或调用明细中的服务名" /></label>
        <label>精确模型 ID<input v-model="form.model_id" aria-label="计价模型" placeholder="保留模型 ID 大小写；非模型服务可留空" /></label>
        <label>限定模型档案 ID（可选）<input v-model="form.profile_id" aria-label="计价模型档案" placeholder="留空适用于此端点和模型的所有档案" /></label>
        <label>币种<select v-model="form.currency" aria-label="价目币种"><option>USD</option><option>CNY</option><option>EUR</option></select></label>
        <label>价目依据<input v-model="form.source_reference" required aria-label="价目依据" placeholder="公开价目日期、合同版本或不含凭证的链接" /></label>
        <fieldset v-for="(component, index) in form.components" :key="index">
          <legend>计费项 {{ index + 1 }}</legend>
          <label>用量单位<select v-model="component.unit" :aria-label="`计费单位 ${index + 1}`"><option v-for="unit in units" :key="unit">{{ unit }}</option></select></label>
          <label>价格<input v-model="component.rate" required inputmode="decimal" :aria-label="`单价 ${index + 1}`" /></label>
          <label>每多少单位<input v-model="component.per" required inputmode="decimal" :aria-label="`计价基数 ${index + 1}`" /></label>
          <label>从总量扣除的分项（可选）<input v-model="component.subtract" :aria-label="`扣除单位 ${index + 1}`" placeholder="如单独计价缓存时填 prompt_cache_hit_tokens" /></label>
          <button type="button" :disabled="form.components.length === 1" @click="form.components.splice(index, 1)">删除此计费项</button>
        </fieldset>
        <button type="button" @click="form.components.push({ unit: 'tool_search', subtract: '', rate: '', per: '1' })">添加计费项</button>
        <label>仅作为统计、不重复收费的单位<input v-model="form.informational" aria-label="非计费统计单位" placeholder="用逗号分隔；需依据价目判断" /></label>
        <label class="checkbox"><input v-model="form.complete_contract" type="checkbox" aria-label="确认价目完整覆盖" />我已核对：此价目涵盖该服务此次调用的全部费用项目。</label>
        <p>只有已声明完整覆盖、实际用量齐全、无未知收费单位的成功调用，才自动把预算预留替换为价目估算。估算仍需与账单核对。</p>
        <button type="submit" :disabled="busy">保存新价目版本</button>
      </form>
    </div>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>
<style scoped>
.pricing-settings { border-top: 1px solid var(--border-color, #dce4ee); padding: 20px 0; margin-top: 20px; font-size: 13px; }
.heading { font-size: 15px; font-weight: 600; background: transparent; border: none; padding: 0; color: inherit; }
p, small { color: var(--text-secondary, #66758a); line-height: 1.65; }
label, small { display: block; margin: 10px 0; overflow-wrap: anywhere; }
input, select { display: block; width: 100%; box-sizing: border-box; padding: 9px; border: 1px solid var(--border-color, #dce4ee); border-radius: 7px; font: inherit; background: transparent; color: inherit; }
.checkbox { display: flex; gap: 8px; align-items: flex-start; }.checkbox input { width: auto; }
fieldset, .version { padding: 14px; margin: 12px 0; border: 1px solid var(--border-color, #dce4ee); border-radius: 8px; min-width: 0; }
.version span { margin-left: 10px; font-size: 12px; } button { cursor: pointer; border: 1px solid var(--border-color, #dce4ee); background: var(--bg-secondary, #f5f7fa); color: inherit; padding: 7px 10px; border-radius: 6px; margin-right: 8px; } button:disabled { opacity: .5; }
</style>
