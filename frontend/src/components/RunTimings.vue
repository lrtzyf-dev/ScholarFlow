<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { RunTiming } from '../resourceApi'

const props = defineProps<{ timings: RunTiming[]; activeRunId?: string }>()
const selected = ref('')
watch(
  () => [props.activeRunId, props.timings.map((item) => item.run_id).join(',')],
  () => {
    if (!props.timings.some((item) => item.run_id === selected.value))
      selected.value = props.activeRunId || props.timings[0]?.run_id || ''
  },
  { immediate: true }
)
const current = computed(() => props.timings.find((item) => item.run_id === selected.value))
function duration(seconds?: number | null) {
  if (seconds === null) return '时钟变化，无法可靠对应'
  if (seconds === undefined) return '未记录'
  return seconds >= 60 ? `${(seconds / 60).toFixed(2)} 分钟` : `${seconds.toFixed(2)} 秒`
}
const fields = computed(
  () =>
    [
      ['任务经过时间', current.value?.elapsed_wall_seconds],
      ['已确认任务时间', current.value?.confirmed_task_wall_seconds],
      ['运行时间（扣除请求排队）', current.value?.active_wall_seconds],
      ['排队等待', current.value?.queue_wall_seconds],
      ['审批等待', current.value?.approval_wait_seconds],
      ['用户等待（任务暂停）', current.value?.user_wait_seconds],
      ['离线或尚未确认', current.value?.offline_or_unknown_seconds],
      ['请求执行窗口累计', current.value?.request_execution_sum_seconds],
      ['请求执行窗口实际覆盖', current.value?.request_execution_wall_seconds],
    ] as const
)
</script>

<template>
  <section class="run-timings" aria-label="任务耗时明细">
    <label v-if="timings.length">
      查看任务
      <select v-model="selected" aria-label="查看耗时的任务">
        <option v-for="(item, index) in timings" :key="item.run_id" :value="item.run_id">
          {{ item.run_id === activeRunId ? '当前任务' : `第 ${index + 1} 个最近任务` }} ·
          {{ item.run_id }}
        </option>
      </select>
    </label>
    <p v-if="!current">此会话还没有任务耗时记录。</p>
    <p v-else-if="!current.available">{{ current.reason }}</p>
    <template v-else>
      <dl>
        <div v-for="[label, seconds] in fields" :key="label">
          <dt>{{ label }}</dt>
          <dd>{{ duration(seconds) }}</dd>
        </div>
      </dl>
      <p>
        {{ current.request_windows }} 个请求窗口；{{ current.queue_only_requests }}
        个请求只进入排队，未取得执行窗口。
      </p>
      <p v-if="current.duplicate_call_windows" role="status">
        {{ current.duplicate_call_windows }} 个窗口关联了重复调用
        ID；累计用时已按调用去重，需结合调用记录核对。
      </p>
      <p>
        并发请求累计时间可能大于任务经过时间，两者不能相加。请求窗口从取得额度到请求收尾，包含准备和处理时间；不代表服务商的计算时间。
      </p>
      <p>
        仅统计已保存确认的区间，通常每 5
        秒确认一次。重启间隙与未确认尾部列为未知，刷新不会推算为运行时间。已确认任务时间合并重叠区间；排队指标与任务阶段可能重叠。
      </p>
      <p v-if="current.clock_adjustment_detected" role="status">
        检测到系统时钟调整，无法可靠计算任务经过时间和跨阶段覆盖；该差额不会算成离线时间。请求累计和单段等待仍按单调时钟保留。
      </p>
    </template>
  </section>
</template>

<style scoped>
.run-timings {
  line-height: 1.6;
}
label {
  display: grid;
  gap: 6px;
}
select {
  width: 100%;
  min-width: 0;
  padding: 6px;
  border: 1px solid var(--border-color, #d4d4d8);
  border-radius: 5px;
  color: inherit;
  background: var(--bg-secondary, #fafafa);
  font: inherit;
}
dl {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin: 14px 0;
}
dt,
p {
  color: var(--text-secondary, #666);
  font-size: 12px;
}
dd {
  margin: 3px 0 0;
  font-variant-numeric: tabular-nums;
}
select:focus-visible {
  outline: 2px solid var(--accent-color, #4875be);
  outline-offset: 2px;
}
@media (max-width: 480px) {
  dl {
    grid-template-columns: 1fr;
  }
}
</style>
