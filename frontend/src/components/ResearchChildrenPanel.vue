<script setup lang="ts">
import { toRef } from 'vue'
import { useResearchChildren } from '../useResearchChildren'

const props = defineProps<{ runId: string; conversationId: string }>()
const emit = defineEmits<{ parent: []; changed: [] }>()
const children = useResearchChildren(toRef(props, 'conversationId'), toRef(props, 'runId'))
const {
  items,
  selected,
  loading,
  submitting,
  error,
  notice,
  acknowledged,
  canGrant,
  pendingGrant,
  pendingGrants,
} = children
const kinds: Record<string, string> = {
  matrix: '证据矩阵',
  framework: '论文框架',
  translation: '学术翻译',
  quality: '引用讨论分析',
}
const states: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  ended: '本轮已结束',
  interrupted: '已中断',
  stopped: '已停止',
  failed: '失败',
  completed: '已完成',
  partial: '部分完成',
  blocked: '受阻',
  open: '目标待完成',
  cancelled: '已停止',
}
function money(values?: Record<string, string>) {
  return values && Object.keys(values).length
    ? Object.entries(values)
        .map(([currency, amount]) => `${amount} ${currency}`)
        .join('；')
    : '暂无'
}
async function grant() {
  const run = props.runId
  await children.grant()
  if (props.runId === run) emit('changed')
}
async function recover(requestId: string) {
  const run = props.runId
  await children.retryPending(requestId)
  if (props.runId === run) emit('changed')
}
</script>

<template>
  <section class="research-children" aria-label="科研子任务与费用确认">
    <header>
      <h3>科研子任务</h3>
      <button :disabled="loading || submitting" @click="children.load">刷新科研子任务</button>
    </header>
    <p v-if="loading" role="status">正在读取科研子任务…</p>
    <div
      v-for="pending in pendingGrants"
      :key="pending.request_id"
      :data-grant-request="pending.request_id"
      class="pending"
      role="status"
    >
      <strong>原许可的提交结果待确认</strong>
      <p>
        子任务 {{ pending.child_run_id }} · 父任务修订
        {{ pending.expected_parent_revision }}。核对会重发同一个许可编号，不会创建新的执行请求。
      </p>
      <button :disabled="submitting" @click="recover(pending.request_id)">核对原许可结果</button>
    </div>
    <p v-if="!loading && !items.length && !error">此任务尚无矩阵、框架、翻译或引用分析子任务。</p>
    <ol>
      <li v-for="child in items" :key="child.run_id">
        <button
          class="child"
          :aria-pressed="selected?.run_id === child.run_id"
          :disabled="submitting"
          @click="children.select(child.run_id)"
        >
          <strong>{{ kinds[child.kind] || child.kind }}</strong>
          <span
            >{{ states[child.status] || child.status }} ·
            {{ states[child.outcome] || child.outcome || '目标待核对' }}</span
          >
          <span
            >第 {{ child.attempt_number }} 次尝试<span v-if="child.requires_unknown_confirmation">
              · 有调用结果未知</span
            ></span
          >
        </button>
      </li>
    </ol>
    <div v-if="selected" class="detail">
      <p>
        所属父任务：<code>{{ selected.parent_run_id }}</code
        ><br />子任务：<code>{{ selected.run_id }}</code>
      </p>
      <p>
        已记录 {{ selected.usage.call_count }} 次调用；其中
        {{ selected.usage.unknown_usage_calls }} 次用量未知。
      </p>
      <dl>
        <dt>用户按账单核对</dt>
        <dd>{{ money(selected.usage.pricing?.user_confirmed) }}</dd>
        <dt>按完整价目估算</dt>
        <dd>{{ money(selected.usage.pricing?.complete_estimates) }}</dd>
        <dt>仅部分费用估算</dt>
        <dd>{{ money(selected.usage.pricing?.partial_estimates) }}</dd>
      </dl>
      <p>
        {{ selected.usage.unknown_amount_calls ?? selected.usage.pricing?.unknown_calls ?? 0 }}
        次调用金额未知。以上估算不是供应商结算账单，未知不等于免费。
      </p>
      <details>
        <summary>用量与父子绑定</summary>
        <p>
          供应商回传用量：{{ JSON.stringify(selected.usage.actual_usage) }}<br />估算用量：{{
            JSON.stringify(selected.usage.estimated_usage || {})
          }}
        </p>
        <p>
          父目标 {{ selected.parent_goal_id }} · 父修订 {{ selected.parent_revision }}<br />父尝试
          {{ selected.parent_attempt_id }}<br />子尝试 {{ selected.attempt_id }}
        </p>
      </details>
      <p v-if="selected.error">{{ selected.error }}</p>
      <div v-if="selected.requires_unknown_confirmation" class="confirmation">
        <p>
          上次调用可能已经执行或计费。允许重试可能再次产生费用；只授权此子任务、此尝试及当前未知调用，不改变其他权限或预算。
        </p>
        <p v-if="selected.unknown_retry_approved" role="status">
          此未知调用已有有效的单次许可。许可会在原任务恢复时核对并消费。
        </p>
        <template v-else>
          <label
            ><input
              v-model="acknowledged"
              type="checkbox"
              :disabled="
                submitting || !!pendingGrant || ['queued', 'running'].includes(selected.status)
              "
            />我已核对并确认此调用可能重复计费，允许本次重试</label
          >
          <button :disabled="!canGrant" @click="grant">保存本次单次许可</button>
        </template>
      </div>
      <p v-else-if="selected.delivery_verified">
        子任务的已保存交付已通过结构与固定来源检查；主任务仍须核对完整目标。
      </p>
      <p v-else>子任务由所属父任务控制。已有部分结果会保留，返回内容不代表主任务已经完成。</p>
      <button :disabled="submitting" @click="emit('parent')">回到原任务继续</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
  </section>
</template>

<style scoped>
.research-children {
  margin: 12px 0 18px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 14px;
  min-width: 0;
  color: var(--fn-text-primary);
}
header {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
h3 {
  font-size: 15px;
  margin: 0;
}
p {
  font-size: 12px;
  line-height: 1.7;
  color: var(--fn-text-secondary);
  overflow-wrap: anywhere;
}
button {
  font: inherit;
  font-size: 12px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  color: inherit;
  border-radius: 6px;
  padding: 7px 10px;
  cursor: pointer;
  max-width: 100%;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
ol {
  list-style: none;
  padding: 0;
}
.child {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  text-align: left;
  width: 100%;
  margin: 5px 0;
  overflow-wrap: anywhere;
}
.child span {
  font-size: 12px;
}
.child[aria-pressed='true'] {
  border-color: var(--fn-brand);
}
.pending,
.confirmation {
  background: var(--fn-bg-secondary);
  padding: 12px;
  border-radius: 6px;
}
.pending strong {
  font-size: 13px;
}
.detail {
  border-top: 1px solid var(--fn-border-primary);
  padding-top: 6px;
}
label {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  font-size: 12px;
  line-height: 1.7;
  margin: 10px 0;
}
code {
  font-size: 11px;
  overflow-wrap: anywhere;
}
dl {
  display: grid;
  grid-template-columns: minmax(100px, 1fr) minmax(0, 2fr);
  font-size: 12px;
  gap: 7px;
}
dd {
  margin: 0;
  overflow-wrap: anywhere;
}
summary {
  font-size: 12px;
  cursor: pointer;
}
button:focus-visible,
input:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
[role='alert'] {
  color: #b42318;
}
@media (max-width: 600px) {
  .research-children {
    padding: 10px;
  }
  .child {
    display: grid;
  }
  .pending,
  .confirmation {
    padding: 9px;
  }
  dl {
    grid-template-columns: 1fr;
  }
  dd {
    margin-bottom: 7px;
  }
}
</style>
