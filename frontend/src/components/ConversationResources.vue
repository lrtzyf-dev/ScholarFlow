<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { Gauge, X } from 'lucide-vue-next'
import CallCosts from './CallCosts.vue'
import RunTimings from './RunTimings.vue'
import {
  getResources,
  saveResources,
  increaseResources,
  type ResourceSettings,
} from '../resourceApi'

const props = defineProps<{
  conversationId: string
  disabled?: boolean
  running?: boolean
  activeBudgetId?: string | null
}>()
const emit = defineEmits<{ busy: [value: boolean] }>()
const open = ref(false),
  loading = ref(false),
  saving = ref(false)
const data = ref<ResourceSettings | null>(null)
const error = ref(''),
  notice = ref('')
const amount = ref(''),
  estimate = ref(''),
  minutes = ref(''),
  currency = ref('USD')
const currentAmount = ref(''),
  currentMinutes = ref('')
const button = ref<HTMLButtonElement | null>(null),
  panel = ref<HTMLElement | null>(null)
const panelPosition = ref({ left: '16px', top: '16px', width: 'min(420px, 88vw)' })
let epoch = 0,
  disposed = false
let timer: ReturnType<typeof setInterval> | undefined
const budget = computed(
  () =>
    data.value?.budgets.find((item) => item.id === props.activeBudgetId) || data.value?.budgets[0]
)
const queued = computed(() => data.value?.queue.filter((item) => item.state === 'queued') || [])
const configured = computed(() =>
  Boolean(data.value?.policy.amount_limit || data.value?.policy.active_seconds_limit)
)
watch(saving, (value) => emit('busy', value), { immediate: true })

function failure(reason: unknown) {
  const detail = (reason as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : '预算设置未能保存或读取，请稍后重试'
}
async function load(fill = false) {
  const request = ++epoch,
    cid = props.conversationId
  loading.value = !data.value
  try {
    const value = await getResources(cid)
    if (disposed || request !== epoch || cid !== props.conversationId) return
    data.value = value
    if (fill) {
      currency.value = value.policy.currency
      amount.value = value.policy.amount_limit || ''
      estimate.value = value.policy.request_estimate || ''
      minutes.value = value.policy.active_seconds_limit
        ? String(value.policy.active_seconds_limit / 60)
        : ''
      currentAmount.value = budget.value?.amount_limit || ''
      currentMinutes.value = budget.value?.active_seconds_limit
        ? String(budget.value.active_seconds_limit / 60)
        : ''
    }
    error.value = ''
  } catch (reason) {
    if (!disposed && request === epoch) error.value = failure(reason)
  } finally {
    if (!disposed && request === epoch) loading.value = false
  }
}
async function saveDefaults() {
  if (!data.value || saving.value) return
  const cid = props.conversationId,
    request = ++epoch
  saving.value = true
  notice.value = error.value = ''
  try {
    const saved = await saveResources(
      cid,
      {
        currency: currency.value,
        amount_limit: amount.value || null,
        request_estimate: amount.value ? estimate.value || null : null,
        active_seconds_limit: minutes.value ? Number(minutes.value) * 60 : null,
      },
      data.value.revision
    )
    if (disposed || request !== epoch || cid !== props.conversationId) return
    data.value = { ...data.value, ...saved }
    notice.value = '已保存，应用于此会话下一次新任务。'
  } catch (reason) {
    if (!disposed && cid === props.conversationId) error.value = failure(reason)
  } finally {
    if (!disposed && cid === props.conversationId) saving.value = false
  }
}
async function increaseCurrent() {
  if (!budget.value || saving.value) return
  const cid = props.conversationId,
    target = budget.value,
    request = ++epoch
  saving.value = true
  notice.value = error.value = ''
  try {
    const saved = await increaseResources(
      cid,
      target,
      currentAmount.value || null,
      currentMinutes.value ? Number(currentMinutes.value) * 60 : null
    )
    if (disposed || request !== epoch || cid !== props.conversationId) return
    if (data.value)
      data.value.budgets = data.value.budgets.map((item) => (item.id === saved.id ? saved : item))
    notice.value = '任务预算已调整；已暂停的任务可以继续执行。'
    window.dispatchEvent(
      new CustomEvent('scholarflow:resource-budget-updated', {
        detail: { conversationId: cid, budgetId: saved.id },
      })
    )
  } catch (reason) {
    if (!disposed && cid === props.conversationId) error.value = failure(reason)
  } finally {
    if (!disposed && cid === props.conversationId) saving.value = false
  }
}
function close() {
  open.value = false
  void nextTick(() => button.value?.focus())
}
function positionPanel() {
  if (!open.value || !panel.value || !button.value) return
  const anchor = button.value.getBoundingClientRect(),
    height = panel.value.getBoundingClientRect().height
  const width = Math.min(420, window.innerWidth * 0.88)
  panelPosition.value = {
    width: width + 'px',
    left: Math.max(16, Math.min(anchor.right - width, window.innerWidth - width - 16)) + 'px',
    top: Math.max(16, Math.min(anchor.top - height - 8, window.innerHeight - height - 16)) + 'px',
  }
}
function globalKey(event: KeyboardEvent) {
  if (open.value && event.key === 'Escape') {
    event.preventDefault()
    close()
  }
}
watch(open, async (value) => {
  if (timer) clearInterval(timer)
  timer = undefined
  if (value) {
    const width = Math.min(420, window.innerWidth * 0.88)
    const right = button.value?.getBoundingClientRect().right || window.innerWidth - 16
    panelPosition.value = {
      width: width + 'px',
      top: '16px',
      left: Math.max(16, Math.min(right - width, window.innerWidth - width - 16)) + 'px',
    }
    await load(true)
    if (!open.value || disposed) return
    await nextTick()
    positionPanel()
    panel.value?.focus()
    timer = setInterval(() => {
      if (open.value && !saving.value && props.running) void load()
    }, 2000)
  }
})
watch(
  [data, amount, error, notice],
  () => {
    if (open.value) void nextTick(positionPanel)
  },
  { deep: true }
)
watch(
  () => props.conversationId,
  () => {
    epoch++
    data.value = null
    open.value = saving.value = false
    error.value = notice.value = ''
  }
)
onMounted(() => {
  window.addEventListener('resize', positionPanel)
  window.addEventListener('keydown', globalKey)
})
onUnmounted(() => {
  disposed = true
  epoch++
  if (timer) clearInterval(timer)
  window.removeEventListener('resize', positionPanel)
  window.removeEventListener('keydown', globalKey)
})
</script>

<template>
  <div class="conversation-resources">
    <button
      ref="button"
      type="button"
      class="resource-toggle"
      :disabled="disabled"
      :aria-expanded="open"
      aria-controls="conversation-resource-panel"
      @click="open = !open"
    >
      <Gauge :size="14" />{{ configured ? '已设预算' : '任务预算' }}
    </button>
    <section
      v-if="open"
      id="conversation-resource-panel"
      ref="panel"
      class="resource-panel"
      :style="panelPosition"
      tabindex="-1"
      aria-label="任务预算"
      @keydown.esc.stop="close"
    >
      <header>
        <strong>任务预算</strong
        ><button type="button" aria-label="关闭任务预算" @click="close"><X :size="16" /></button>
      </header>
      <p v-if="loading" role="status">正在读取预算…</p>
      <template v-if="data">
        <form @submit.prevent="saveDefaults">
          <h4>下一次新任务</h4>
          <p class="resource-hint">留空表示不设置该项预算；持续执行不受固定轮数限制。</p>
          <div class="resource-fields">
            <label
              >费用预算<input
                v-model="amount"
                aria-label="下一任务费用预算"
                type="number"
                min="0"
                step="0.01"
                placeholder="不设置"
                :disabled="saving"
            /></label>
            <label
              >币种<select v-model="currency" aria-label="预算币种" :disabled="saving">
                <option value="USD">USD</option>
                <option value="CNY">CNY</option>
              </select></label
            >
            <label v-if="amount"
              >每次请求预留估值<input
                v-model="estimate"
                aria-label="每次请求预留估值"
                type="number"
                min="0.000001"
                step="0.000001"
                required
                :disabled="saving"
            /></label>
            <label
              >请求执行时间（分钟）<input
                v-model="minutes"
                aria-label="下一任务请求执行时间"
                type="number"
                min="0.01"
                step="0.01"
                placeholder="不设置"
                :disabled="saving"
            /></label>
          </div>
          <p class="resource-hint">
            时间按模型与工具取得额度后的请求执行窗口累计，含请求准备与处理，不含排队和审批等待；并发窗口分别累计。费用预留使用你的估值；配置完整价目后，可按实际用量自动计算费用估算。
          </p>
          <button type="submit" class="resource-save" :disabled="saving || disabled">
            保存下一任务预算
          </button>
        </form>
        <div v-if="budget" class="resource-current">
          <h4>{{ budget.id === activeBudgetId ? '当前任务' : '最近任务' }}预算</h4>
          <dl>
            <div>
              <dt>已核对费用</dt>
              <dd>
                {{ budget.confirmed_spent_amount ?? budget.spent_amount }} {{ budget.currency }}
              </dd>
            </div>
            <div>
              <dt>按价目估算</dt>
              <dd>{{ budget.estimated_spent_amount ?? '0' }} {{ budget.currency }}</dd>
            </div>
            <div>
              <dt>预留金额</dt>
              <dd>{{ budget.reserved_amount }} {{ budget.currency }}</dd>
            </div>
            <div>
              <dt>剩余额度</dt>
              <dd>{{ budget.remaining_amount ?? '未设置' }}</dd>
            </div>
            <div>
              <dt>请求累计用时</dt>
              <dd>{{ (budget.active_seconds / 60).toFixed(2) }} 分钟</dd>
            </div>
          </dl>
          <p v-if="budget.pending_reconciliation" class="resource-hint">
            {{ budget.pending_reconciliation }}
            次请求费用待核对；已发出的请求与未知费用继续占用预留。
          </p>
          <form @submit.prevent="increaseCurrent">
            <div class="resource-fields">
              <label v-if="budget.amount_limit !== null"
                >调整费用预算<input
                  v-model="currentAmount"
                  aria-label="调整当前任务费用预算"
                  type="number"
                  min="0"
                  step="0.01"
                  :disabled="saving"
              /></label>
              <label
                >调整请求时间（分钟）<input
                  v-model="currentMinutes"
                  aria-label="调整当前任务请求时间"
                  type="number"
                  min="0.01"
                  step="0.01"
                  :disabled="saving"
              /></label>
            </div>
            <button type="submit" class="resource-save" :disabled="saving || disabled">
              调整该任务预算
            </button>
          </form>
          <p class="resource-hint">
            达到预算后停止新派发并保存进度。已派发远端请求的最终费用或耗时可能超出估值。
          </p>
        </div>
        <p v-if="queued.length" role="status">
          {{ queued.length }} 个请求正在等待{{
            queued.some((item) => item.reason === 'provider_retry_after')
              ? '服务商限流恢复'
              : '共享服务商额度'
          }}。
        </p>
        <details class="resource-cost-details">
          <summary>任务耗时明细</summary>
          <RunTimings :timings="data.timings || []" :active-run-id="budget?.owner_run_id" />
        </details>
        <details class="resource-cost-details">
          <summary>调用费用明细与账单核对</summary>
          <CallCosts :conversation-id="conversationId" @changed="load()" />
        </details>
      </template>
      <p v-if="error" class="resource-error" role="alert">
        {{ error }} <button type="button" :disabled="saving" @click="load()">重新读取</button>
      </p>
      <p v-if="notice" class="resource-hint" role="status">{{ notice }}</p>
    </section>
  </div>
</template>

<style scoped>
.conversation-resources {
  position: relative;
}
.resource-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: transparent;
  border: 1px solid var(--border-color, #e4e4e7);
  border-radius: 6px;
  padding: 5px 8px;
  color: inherit;
  font: inherit;
  cursor: pointer;
  font-size: 12px;
}
.resource-panel {
  position: fixed;
  box-sizing: border-box;
  z-index: 65;
  max-height: min(720px, calc(100vh - 32px));
  overflow: auto;
  padding: 16px;
  border: 1px solid var(--border-color, #e4e4e7);
  border-radius: 10px;
  background: var(--bg-primary, #fff);
  color: var(--text-primary, #242424);
  box-shadow: 0 8px 30px #0002;
  font-size: 13px;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
header button {
  border: 0;
  background: none;
  color: inherit;
  cursor: pointer;
}
h4 {
  margin: 16px 0 8px;
  font-size: 13px;
}
.resource-hint {
  color: var(--text-secondary, #666);
  line-height: 1.6;
  font-size: 12px;
  margin: 8px 0;
}
.resource-fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
label {
  display: grid;
  gap: 5px;
  min-width: 0;
}
input,
select {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  border: 1px solid var(--border-color, #d4d4d8);
  border-radius: 5px;
  padding: 6px 8px;
  background: var(--bg-secondary, #fafafa);
  color: inherit;
  font: inherit;
}
.resource-save {
  margin-top: 8px;
  padding: 7px 10px;
  border: 1px solid var(--border-color, #d4d4d8);
  border-radius: 5px;
  background: var(--bg-secondary, #fafafa);
  color: inherit;
  font: inherit;
  cursor: pointer;
}
.resource-current {
  border-top: 1px solid var(--border-color, #e4e4e7);
  margin-top: 16px;
}
dl {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin: 0;
}
dt {
  color: var(--text-secondary, #666);
  font-size: 12px;
}
dd {
  margin: 3px 0 0;
  font-variant-numeric: tabular-nums;
}
.resource-error {
  color: #b42318;
  line-height: 1.5;
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
button:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 2px solid var(--accent-color, #4875be);
  outline-offset: 2px;
}
</style>
