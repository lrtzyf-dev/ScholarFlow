<template>
  <div class="tasks-page">
    <RunCenterPanel @background="focusBackgroundTask" />
    <StorageReviewReminder />
    <div class="tasks-head">
      <div class="tasks-title">
        <h2>后台任务</h2>
        <p>
          {{ runningTasks }} 项进行中<span v-if="pendingApprovals">
            · {{ pendingApprovals }} 项等待审批</span
          >
        </p>
      </div>
      <button class="connection-status" type="button" title="刷新任务状态" @click="load">
        <span></span>{{ connectionLabel }}
      </button>
    </div>

    <div class="task-tabs" role="tablist" aria-label="任务视图" @keydown="onTabKeydown">
      <button
        v-for="tab in taskTabs"
        :key="tab.value"
        type="button"
        role="tab"
        :aria-selected="taskView === tab.value"
        :tabindex="taskView === tab.value ? 0 : -1"
        :class="{ selected: taskView === tab.value }"
        @click="taskView = tab.value"
      >
        {{ tab.label
        }}<span>{{
          tab.value === 'active'
            ? runningTasks
            : tab.value === 'history'
              ? historyTotal
              : runningTasks + historyTotal
        }}</span>
      </button>
    </div>
    <div v-if="loadError" class="task-error" role="alert">{{ loadError }}</div>
    <div v-if="!visibleTasks.length && !loadError" class="empty">
      <div>{{ taskView === 'active' ? '暂时没有进行中的任务' : '暂无后台任务' }}</div>
      <p>发起研究后，可以在这里查看进度。</p>
    </div>

    <div v-else class="task-list">
      <div v-for="t in visibleTasks" :key="t.id" :id="'background-task-' + t.id" class="task-card" tabindex="-1">
        <div class="task-top">
          <span class="task-symbol" aria-hidden="true">
            <LockKeyhole v-if="t.approvals?.length" :size="19" />
            <CircleAlert v-else-if="t.status === 'done' && t.result?.completed === false" :size="19" />
            <Check v-else-if="t.status === 'done'" :size="19" />
            <Clock3 v-else :size="19" />
          </span>
          <div class="task-heading-text">
            <div class="task-title">{{ t.title || '未命名任务' }}</div>
            <p>
              {{ t.execution_mode === 'plan' ? '研究计划' : '研究任务'
              }}<span v-if="t.plan?.length"> · {{ t.plan.length }} 个步骤</span>
            </p>
          </div>
          <span class="task-status" :class="t.approvals?.length ? 'awaiting' : t.result?.completed === false ? 'partial' : t.status">{{
            t.approvals?.length
              ? '等待审批'
              : t.executing && !['pending', 'running'].includes(t.status)
                ? '正在结束'
                : t.execution_mode === 'plan' && t.status === 'done'
                  ? '计划已生成'
                  : t.status === 'done' && t.result?.completed === false
                    ? '部分完成'
                  : statusLabel(t.status)
          }}</span>
        </div>

        <!-- 进度条 -->
        <div class="task-progress">
          <div class="tp-bar">
            <div class="tp-fill" :style="{ width: Math.round(t.progress * 100) + '%' }"></div>
          </div>
        </div>
        <div class="task-progress-caption">
          <span>{{ progressCaption(t) }}</span
          ><span>{{ Math.round(t.progress * 100) }}%</span>
        </div>

        <!-- 待办步骤 -->
        <div v-if="t.plan && t.plan.length" class="task-plan">
          <div v-for="(s, i) in t.plan" :key="i" class="plan-step" :class="s.status">
            <span class="step-dot"
              ><Check v-if="s.status === 'done'" :size="10" /><template v-else>{{
                i + 1
              }}</template></span
            >
            <span class="step-text">{{ s.title || s.text }}</span>
          </div>
        </div>

        <div v-for="approval in t.approvals" :key="approval.approval_id" class="task-approval">
          <div class="approval-heading">
            <ShieldAlert :size="16" /><strong>{{ approval.tool_name }}</strong
            ><span>{{ approval.risk }}</span>
          </div>
          <p v-if="approval.reason">{{ approval.reason }}</p>
          <pre>{{
            approval.tool_name === 'git_project_write'
              ? gitApprovalSummary(approval.arguments)
              : JSON.stringify(approval.arguments, null, 2)
          }}</pre>
          <div class="task-actions">
            <button
              v-if="approval.tool_name === 'edit_project_file' && approval.arguments.change_id"
              class="task-btn"
              @click="
                openApprovalFileReview(approval.arguments.change_id, t.conversation_id || undefined)
              "
            >
              查看文件差异
            </button>
            <button
              v-if="approval.tool_name === 'run_project_command' && approval.arguments.command_id"
              class="task-btn"
              @click="commandReviewId = String(approval.arguments.command_id)"
            >
              查看命令记录
            </button>
            <button
              v-if="
                approval.tool_name === 'git_project_write' &&
                approval.arguments.operation_id &&
                approval.arguments.project_id
              "
              class="task-btn"
              @click="
                gitReviewTarget = {
                  id: String(approval.arguments.operation_id),
                  project_id: String(approval.arguments.project_id),
                }
              "
            >
              查看 Git 审阅
            </button>
            <button
              class="task-btn"
              type="button"
              :disabled="deciding.has(approval.approval_id)"
              @click="decide(approval.approval_id, 'approve', 'once')"
            >
              <Check :size="14" />允许一次
            </button>
            <button
              class="task-btn"
              type="button"
              :disabled="deciding.has(approval.approval_id)"
              @click="decide(approval.approval_id, 'approve', 'session')"
            >
              <ShieldCheck :size="14" />本次运行允许
            </button>
            <button
              class="task-btn cancel-btn"
              type="button"
              :disabled="deciding.has(approval.approval_id)"
              @click="decide(approval.approval_id, 'deny', 'once')"
            >
              <X :size="14" />拒绝
            </button>
          </div>
        </div>

        <div class="task-actions">
          <button
            v-for="change in changeEvents(t.events || [])"
            :key="change.change_id"
            class="task-btn"
            @click="openChangeFileReview(change.change_id, t.conversation_id || undefined)"
          >
            查看差异 · {{ change.path }}
          </button>
        </div>
        <div class="task-actions">
          <button
            v-for="command in commandEvents(t.events || [])"
            :key="command.command_id"
            class="task-btn"
            @click="commandReviewId = command.command_id"
          >
            命令与输出 · {{ command.summary }}
          </button>
        </div>
        <div class="task-actions">
          <button
            v-for="operation in gitOperationEvents(t.events || [])"
            :key="operation.operation_id"
            class="task-btn"
            @click="
              gitReviewTarget = { id: operation.operation_id, project_id: operation.project_id }
            "
          >
            查看 Git 操作 · {{ gitActionLabels[operation.action] }}
          </button>
        </div>
        <!-- 结果 -->
        <div v-if="t.result && t.result.answer" class="task-result">
          <div class="result-label">
            {{ t.execution_mode === 'plan' ? '待审阅计划' : '最终答案' }}
          </div>
          <div class="result-answer">{{ t.result.answer }}</div>
        </div>

        <!-- 错误 -->
        <div
          v-if="t.error"
          class="task-error"
          :class="{ 'task-notice': t.status === 'interrupted' }"
        >
          {{ t.error }}
        </div>

        <div
          v-if="!isActive(t) && t.recovery?.available"
          class="task-recovery"
          :class="{ uncertain: t.recovery.pending_operation }"
        >
          <p>
            已保留 {{ t.recovery.completed_tools || 0 }} 项工具结果 · 第 {{ t.recovery.step }} 轮
          </p>
          <template v-if="!isActive(t) && t.recovery.pending_operation">
            <strong
              >{{ t.recovery.pending_operation.kind === 'hook' ? '钩子' : '工具' }} ·
              {{ t.recovery.pending_operation.name }}</strong
            >
            <p>
              应用在这一步执行期间中断，是否已完成无法确认。请检查执行结果后选择；重试可能重复操作。
            </p>
            <pre>{{ JSON.stringify(t.recovery.pending_operation.arguments, null, 2) }}</pre>
            <div class="task-actions">
              <button
                class="task-btn"
                type="button"
                :disabled="resuming.has(t.id)"
                @click="resume(t, 'retry')"
              >
                重试这一步
              </button>
              <button
                class="task-btn"
                type="button"
                :disabled="resuming.has(t.id)"
                @click="resume(t, 'skip')"
              >
                跳过这一步
              </button>
            </div>
          </template>
        </div>
        <p
          v-else-if="!isActive(t) && t.status !== 'done' && t.recovery?.reason"
          class="recovery-reason"
        >
          {{ t.recovery.reason }}
        </p>

        <div class="task-bottom">
          <details v-if="t.events && t.events.length" class="task-events">
            <summary>
              执行记录 <span>{{ t.events.length }}</span>
            </summary>
            <div v-for="(e, i) in t.events.slice(-6)" :key="i" class="event-line">
              <span class="event-kind">{{ eventLabel(e.kind) }}</span>
              <span>{{ e.name || e.text }}</span>
            </div>
          </details>
          <div class="task-actions">
            <button
              v-if="isActive(t)"
              class="task-btn cancel-btn"
              type="button"
              :disabled="cancelling.has(t.id)"
              @click="cancel(t)"
            >
              <Square :size="13" />
              {{ cancelling.has(t.id) ? '取消中…' : '取消任务' }}
            </button>
            <button
              v-if="!isActive(t) && t.recovery?.available && !t.recovery.pending_operation"
              class="task-btn"
              type="button"
              :disabled="resuming.has(t.id)"
              @click="resume(t)"
            >
              <Play :size="13" />从检查点继续
            </button>
            <button
              v-if="!isActive(t)"
              class="task-btn"
              type="button"
              :disabled="resuming.has(t.id)"
              @click="restart(t)"
            >
              <RotateCcw :size="13" />
              从头重新运行
            </button>
            <button
              v-if="t.result && t.result.answer"
              class="task-btn"
              type="button"
              @click="goChat(t)"
            >
              <MessageSquareText :size="13" />
              查看对话
            </button>
          </div>
        </div>
      </div>
    </div>
    <div
      v-if="taskView !== 'active' && (historyCursor || historyError)"
      class="task-history-footer"
    >
      <p v-if="historyError" role="alert">{{ historyError }}</p>
      <button type="button" class="task-btn" :disabled="historyLoading" @click="loadMoreHistory">
        <ChevronDown :size="14" />{{
          historyLoading ? '加载中…' : historyError ? '重试加载' : '加载更早任务'
        }}
      </button>
    </div>
    <FileChangeReview v-model="fileReviewId" :conversation-id="fileReviewConversation" />
    <ProjectCommandReview v-model="commandReviewId" />
    <GitOperationReview
      v-if="gitReviewTarget"
      :key="gitReviewTarget.id"
      :operation="gitReviewTarget"
      @close="gitReviewTarget = null"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import RunCenterPanel from '../components/RunCenterPanel.vue'
import StorageReviewReminder from '../components/StorageReviewReminder.vue'
import { useRouter } from 'vue-router'
import {
  resumeBackgroundTask,
  restartBackgroundTask,
  cancelBackgroundTask,
  decideApproval,
  getBackgroundTask,
  type BackgroundTask,
} from '../api'
import { ElMessage } from 'element-plus'
import {
  CircleAlert,
  Check,
  X,
  RotateCcw,
  Square,
  Clock3,
  LockKeyhole,
  ShieldAlert,
  ShieldCheck,
  MessageSquareText,
  Play,
  ChevronDown,
} from 'lucide-vue-next'
import { isActiveTask, taskState } from '../taskState'
import { durableTaskResumeRequest } from '../taskResume'
import FileChangeReview from '../components/FileChangeReview.vue'
import ProjectCommandReview from '../components/ProjectCommandReview.vue'
import GitOperationReview from '../components/GitOperationReview.vue'
import {
  gitOperationEvents,
  gitActionLabels,
  gitApprovalSummary,
  type GitOperationTarget,
} from '../projectGitApi'
const gitReviewTarget = ref<GitOperationTarget | null>(null)
import { commandEvents } from '../projectCommandApi'
const commandReviewId = ref<string | null>(null)
const fileReviewId = ref<string | null>(null),
  fileReviewConversation = ref<string>()
// 打开审批项的文件差异：记录变更 ID 与所属会话
function openApprovalFileReview(changeId: unknown, conversationId?: string) {
  fileReviewId.value = String(changeId)
  fileReviewConversation.value = conversationId || undefined
}
// 打开事件日志中的文件差异
function openChangeFileReview(changeId: string, conversationId?: string) {
  fileReviewId.value = changeId
  fileReviewConversation.value = conversationId || undefined
}
function changeEvents(events: any[]) {
  return [
    ...new Map(
      events.filter((e) => e.kind === 'project_change' && e.change_id).map((e) => [e.change_id, e])
    ).values(),
  ]
}

const router = useRouter()
const {
  allTasks,
  connection,
  runningTasks,
  pendingApprovals,
  loadError,
  reloadTasks: load,
  historyCursor,
  historyTotal,
  historyLoading,
  historyError,
  loadMoreHistory,
} = taskState
const taskTabs = [
  { value: 'active', label: '进行中' },
  { value: 'history', label: '历史' },
  { value: 'all', label: '全部' },
] as const
const taskView = ref<'all' | 'active' | 'history'>('active')
const focusedTask = ref<BackgroundTask|null>(null)
async function focusBackgroundTask(id:string) {
  try {
    focusedTask.value = await getBackgroundTask(id)
    taskView.value = 'all'
    await nextTick()
    const card = document.getElementById('background-task-' + id)
    card?.scrollIntoView({block:'center',behavior:'smooth'})
    card?.focus({preventScroll:true})
  } catch { ElMessage.error('该后台任务无法读取，请刷新状态') }
}
const visibleTasks = computed(() =>
  (focusedTask.value && !allTasks.value.some(task => task.id === focusedTask.value?.id) ? [focusedTask.value, ...allTasks.value] : allTasks.value).filter(
    (task) =>
      taskView.value === 'all' ||
      (taskView.value === 'active' ? isActiveTask(task) : !isActiveTask(task))
  )
)
function onTabKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const index = taskTabs.findIndex((tab) => tab.value === taskView.value)
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? taskTabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + taskTabs.length) % taskTabs.length
  taskView.value = taskTabs[next].value
  const buttons = (event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>(
    '[role="tab"]'
  )
  buttons[next]?.focus()
}
onMounted(() => {
  void load()
})
onUnmounted(taskState.resetTaskHistory)
const connectionLabel = computed(
  () =>
    ({
      connecting: '正在连接',
      connected: '实时连接',
      reconnecting: '重新连接中',
      polling: '定时刷新',
    })[connection.value]
)
const cancelling = ref<Set<string>>(new Set())
const resuming = ref<Set<string>>(new Set())
const deciding = ref<Set<string>>(new Set())
async function decide(id: string, decision: 'approve' | 'deny', scope: 'once' | 'session') {
  if (deciding.value.has(id)) return
  deciding.value.add(id)
  try {
    await decideApproval(id, decision, scope)
    ElMessage.success(decision === 'approve' ? '已允许' : '已拒绝')
  } catch (e: any) {
    ElMessage.error(
      e?.response?.status === 404
        ? '该审批已结束，任务状态已刷新'
        : e?.response?.data?.detail || '审批失败'
    )
  } finally {
    await load()
    deciding.value.delete(id)
  }
}
function statusLabel(s: string) {
  return (
    {
      pending: '排队中',
      running: '运行中',
      done: '已完成',
      failed: '失败',
      paused: '已暂停',
      cancelled: '已取消',
      interrupted: '已中断',
    }[s] || s
  )
}
function progressCaption(task: {
  status: string
  plan?: { status?: string; title?: string; text?: string }[]
  result?: { completed?: boolean; incomplete_steps?: { title: string }[] } | null
}) {
  if (task.status === 'done' && task.result?.completed === false) {
    return `本轮已结束 · ${task.result.incomplete_steps?.length || 0} 项待处理`
  }
  if (task.status === 'done') return '全部步骤已完成'
  if (task.status === 'cancelled') return '任务已停止'
  if (task.status === 'failed' || task.status === 'interrupted') return '保留当前进度'
  const current = task.plan?.find((step) => step.status === 'in_progress')
  return current?.title || current?.text || statusLabel(task.status)
}
const isActive = isActiveTask
function eventLabel(k: string) {
  return (
    {
      tool_call: '调用',
      tool_result: '结果',
      status: '状态',
      subagents: '子任务',
      project_command: '命令',
      project_change: '文件',
      git_operation: 'Git 操作',
      error: '错误',
      cancelled: '取消',
      approval_request: '待审批',
      approval_result: '审批',
      hook: '钩子',
      resume: '恢复',
      interrupted: '中断',
    }[k] || k
  )
}
async function resume(t: BackgroundTask, resolution?: 'retry' | 'skip') {
  if (resuming.value.has(t.id)) return
  resuming.value.add(t.id)
  let receipt: ReturnType<typeof durableTaskResumeRequest> | undefined
  try {
    receipt = durableTaskResumeRequest(localStorage, t.conversation_id || '', t.id,
      t.run_revision, resolution, () => `task_resume_${crypto.randomUUID()}`)
    await resumeBackgroundTask(t.id, receipt.resolution, receipt.request)
    localStorage.removeItem(receipt.key)
    ElMessage.success('已从检查点继续')
    await load()
  } catch (e: any) {
    // A definitive rejection has not admitted this request. A lost reply/5xx
    // keeps the original identity and original resolution for safe replay.
    const status = e?.response?.status
    if (receipt && status >= 400 && status < 500 && status !== 408 && status !== 429)
      localStorage.removeItem(receipt.key)
    ElMessage.error(e?.response?.data?.detail || e?.message || '操作失败')
    await load()
  } finally {
    resuming.value.delete(t.id)
  }
}
async function restart(t: { id: string }) {
  if (resuming.value.has(t.id)) return
  resuming.value.add(t.id)
  try {
    await restartBackgroundTask(t.id)
    ElMessage.success('已创建新任务，从头重新运行')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '操作失败')
  } finally {
    resuming.value.delete(t.id)
  }
}
async function cancel(t: { id: string }) {
  if (cancelling.value.has(t.id)) return
  cancelling.value.add(t.id)
  try {
    await cancelBackgroundTask(t.id)
    ElMessage.success('已发送取消请求，任务将在当前步骤后停止')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.message || '取消失败')
  } finally {
    cancelling.value.delete(t.id)
  }
}
function goChat(t: { conversation_id: string | null }) {
  router.push({ path: '/chat', query: { conv: t.conversation_id || undefined } })
}
</script>

<style scoped>
.tasks-page {
  font-size: 14px;
  line-height: 1.6;
  width: 100%;
  height: 100%;
  margin: 0 auto;
  padding: 38px 42px 28px;
  max-width: 1180px;
  overflow-y: auto;
  background: var(--fn-bg-primary);
}
.tasks-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 29px;
}
.tasks-title h2 {
  margin: 0 0 8px;
  font-size: 24px;
  line-height: 1.4;
  font-weight: 600;
  letter-spacing: 0;
}
.tasks-title p {
  margin: 0;
  line-height: 1.8;
  font-size: 13px;
  color: var(--fn-text-tertiary);
}
.task-tabs {
  display: flex;
  gap: 24px;
  margin-bottom: 4px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.task-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 44px;
  padding: 10px 0 13px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.task-tabs button.selected {
  color: var(--fn-brand);
  border-bottom-color: var(--fn-brand);
}
.task-tabs span {
  color: var(--fn-text-tertiary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.task-history-footer {
  padding: 18px 0;
  text-align: center;
  font-size: 13px;
  color: var(--fn-text-secondary);
}
.refresh-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  border-radius: 8px;
  padding: 7px 13px;
  font-size: 13px;
  cursor: pointer;
}
.refresh-btn svg {
  width: 15px;
  height: 15px;
}
.empty {
  text-align: center;
  padding: 66px 24px;
  color: var(--muted);
}
.empty svg {
  width: 46px;
  height: 46px;
  margin-bottom: 12px;
}
.empty div {
  font-size: 15px;
  margin-bottom: 8px;
  color: var(--text);
  font-weight: 500;
}
.empty p {
  font-size: 12.5px;
  margin: 0;
}
.task-list {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.task-card {
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  border-radius: 0;
  background: var(--fn-bg-primary);
  padding: 28px 0;
}
.task-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 13px;
  margin-bottom: 20px;
}
.task-title {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: 15px;
  font-weight: 500;
}
.task-status {
  flex-shrink: 0;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 5px;
  border: 1px solid var(--fn-border-primary);
}
.task-status.awaiting {
  background: var(--fn-warning-bg);
  color: var(--fn-warning-text);
}
.task-status.running {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.task-status.done {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.task-status.partial {
  background: var(--fn-warning-bg);
  color: var(--fn-warning-text);
}
.task-status.failed {
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
}
.task-status.cancelled {
  background: #f3f4f6;
  color: #6b7280;
}
.task-status.pending {
  background: var(--fn-bg-secondary);
  color: var(--fn-text-tertiary);
}
.task-status.interrupted {
  background: var(--fn-warning-bg);
  color: var(--fn-warning-text);
}
.task-progress {
  margin: 0;
}
.tp-bar {
  flex: 1;
  height: 3px;
  border-radius: 999px;
  background: var(--fn-bg-secondary);
  overflow: hidden;
}
.tp-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--fn-brand);
  transition: width 0.4s;
}
.tp-pct {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
  min-width: 36px;
  text-align: right;
}
.task-plan {
  display: flex;
  flex-wrap: wrap;
  gap: 19px;
  margin: 20px 0;
}
.plan-step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.step-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  font-size: 9px;
  border-radius: 50%;
  border: 1px solid var(--fn-border-primary);
}
.plan-step.in_progress .step-dot {
  border-color: var(--fn-brand);
  background: var(--fn-brand);
  color: white;
}
.plan-step.done {
  color: var(--fn-text-secondary);
}
.plan-step.done .step-dot {
  border-color: var(--success-border);
  background: var(--success-bg);
  color: var(--success);
}
.task-events {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
}
.event-line {
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: var(--fn-text-secondary);
  line-height: 1.5;
}
.event-kind {
  flex-shrink: 0;
  font-size: 10.5px;
  color: var(--fn-text-tertiary);
  min-width: 34px;
  padding-top: 1px;
}
.task-result {
  border-top: 1px solid var(--fn-border-secondary);
  padding-top: 10px;
}
.result-label {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
  margin-bottom: 5px;
}
.result-answer {
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  max-height: 220px;
  overflow: auto;
}
.task-error {
  font-size: 12.5px;
  color: var(--fn-danger);
  background: #fef2f2;
  border-radius: 8px;
  padding: 8px 11px;
  margin-bottom: 10px;
}
.task-notice,
.task-recovery.uncertain {
  background: #fff9ed;
  color: #775719;
}
.task-recovery {
  padding: 10px 12px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
  border-radius: 8px;
  font-size: 12.5px;
  line-height: 1.6;
}
.task-recovery.uncertain {
  border-color: #efdcb6;
}
.task-recovery p {
  margin: 0 0 6px;
}
.task-recovery p:last-child {
  margin-bottom: 0;
}
.task-recovery pre {
  max-height: 180px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.recovery-reason {
  color: var(--fn-text-secondary);
  font-size: 12.5px;
}
.task-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.task-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 12.5px;
  cursor: pointer;
}
.task-btn svg {
  width: 13px;
  height: 13px;
}
.task-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.cancel-btn {
  color: var(--fn-danger);
}
.cancel-btn:hover:not(:disabled) {
  border-color: #fecaca;
  background: #fef2f2;
}
.task-approval {
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-secondary);
  margin: 23px 0;
  padding: 18px 20px;
}
.approval-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--fn-warning-text);
  font-size: 13px;
  overflow-wrap: anywhere;
}
.approval-heading strong {
  min-width: 0;
}
.approval-heading span {
  margin-left: auto;
  font-size: 11px;
}
.task-approval p {
  font-size: 12px;
  color: var(--fn-text-secondary);
  overflow-wrap: anywhere;
}
.task-approval pre {
  margin: 10px 0;
  padding: 10px;
  max-height: 180px;
  overflow: auto;
  background: var(--fn-bg-secondary);
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.event-line > span:last-child,
.step-text,
.result-answer {
  min-width: 0;
  overflow-wrap: anywhere;
}
.task-symbol {
  display: grid;
  place-items: center;
  flex: 0 0 34px;
  height: 34px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
}
.task-heading-text {
  min-width: 0;
  flex: 1;
}
.task-heading-text p {
  margin: 5px 0 0;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.task-btn:hover:not(:disabled),
.refresh-btn:hover {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.task-actions:empty {
  display: none;
}
@media (max-width: 1100px) {
  .tasks-page {
    padding: 30px 28px;
  }
  .task-plan {
    gap: 13px;
  }
}
@media (max-width: 760px) {
  .tasks-page {
    padding: 25px 20px;
  }
  .tasks-head {
    gap: 12px;
    align-items: flex-start;
  }
  .tasks-title p {
    line-height: 1.8;
  }
  .task-card {
    padding: 23px 0;
  }
  .task-top {
    gap: 10px;
  }
  .task-title {
    font-size: 13px;
  }
  .task-symbol {
    flex-basis: 30px;
    height: 30px;
  }
  .task-status {
    padding: 3px 6px;
    font-size: 10px;
  }
  .task-plan {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 11px;
  }
  .task-approval {
    padding: 15px;
  }
}

.connection-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 8px 0 0;
  border: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: 12px;
  white-space: nowrap;
}
.connection-status > span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}
.task-progress-caption {
  display: flex;
  justify-content: space-between;
  margin-top: 8px;
  color: var(--muted);
  font-size: 10px;
}
.task-status.running,
.task-status.pending,
.task-status.cancelled {
  background: transparent;
  color: var(--muted);
}
.task-bottom {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-top: 19px;
  gap: 14px;
}
.task-bottom > .task-actions {
  margin: 0 0 0 auto;
  justify-content: flex-end;
}
.task-events {
  display: block;
  max-width: 68%;
  margin: 0;
  color: var(--muted);
  font-size: 11px;
}
.task-events summary {
  cursor: pointer;
  padding: 6px 0;
}
.task-events summary span {
  margin-left: 6px;
  color: var(--muted);
  font-size: 10px;
}
.task-events .event-line {
  padding-left: 16px;
  font-size: 10px;
  line-height: 2.1;
}
.task-events .event-kind {
  font-size: 10px;
}
.task-btn {
  min-height: 30px;
  padding: 4px 10px;
  font-size: 12px;
}
@media (max-width: 760px) {
  .task-bottom {
    flex-wrap: wrap;
  }
  .task-events {
    width: 100%;
    max-width: none;
  }
}
</style>
