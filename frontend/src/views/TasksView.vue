<template>
  <div class="tasks-page">
    <div class="tasks-head">
      <div class="tasks-title">
        <h2>后台任务</h2>
        <p>{{ runningTasks }} 项运行中 · {{ pendingApprovals }} 项待审批 · {{ connectionLabel }}</p>
      </div>
      <button class="refresh-btn" type="button" @click="load">
        <RefreshCw :size="15" />
        刷新
      </button>
    </div>

    <div class="task-tabs" role="tablist" aria-label="任务视图" @keydown="onTabKeydown">
      <button v-for="tab in taskTabs" :key="tab.value" type="button" role="tab"
              :aria-selected="taskView === tab.value" :tabindex="taskView === tab.value ? 0 : -1"
              :class="{ selected: taskView === tab.value }" @click="taskView = tab.value">
        {{ tab.label }}<span>{{ tab.value === 'active' ? runningTasks : tab.value === 'history' ? historyTotal : runningTasks + historyTotal }}</span>
      </button>
    </div>
    <div v-if="loadError" class="task-error" role="alert">{{ loadError }}</div>
    <div v-if="!visibleTasks.length && !loadError" class="empty">
      <ListTodo :size="46" />
      <div>{{ taskView === 'active' ? '暂无进行中的任务' : '暂无后台任务' }}</div>
    </div>

    <div v-else class="task-list">
      <div v-for="t in visibleTasks" :key="t.id" class="task-card">
        <div class="task-top">
          <div class="task-title">{{ t.title || '未命名任务' }}</div>
          <span class="task-status" :class="t.approvals?.length ? 'awaiting' : t.status">{{ t.approvals?.length ? '等待审批' : t.executing && !['pending', 'running'].includes(t.status) ? '正在结束' : t.execution_mode === 'plan' && t.status === 'done' ? '计划已生成' : statusLabel(t.status) }}</span>
        </div>

        <!-- 进度条 -->
        <div class="task-progress">
          <div class="tp-bar"><div class="tp-fill" :style="{ width: Math.round(t.progress * 100) + '%' }"></div></div>
          <span class="tp-pct">{{ Math.round(t.progress * 100) }}%</span>
        </div>

        <!-- 待办步骤 -->
        <div v-if="t.plan && t.plan.length" class="task-plan">
          <div v-for="(s, i) in t.plan" :key="i" class="plan-step" :class="s.status">
            <span class="step-dot"></span>
            <span class="step-text">{{ s.title || s.text }}</span>
          </div>
        </div>

        <!-- 事件日志 -->
        <div v-if="t.events && t.events.length" class="task-events">
          <div v-for="(e, i) in t.events.slice(-6)" :key="i" class="event-line">
            <span class="event-kind">{{ eventLabel(e.kind) }}</span>
            <span>{{ e.name || e.text }}</span>
          </div>
        </div>

        <div v-for="approval in t.approvals" :key="approval.approval_id" class="task-approval">
          <div class="approval-heading"><ShieldAlert :size="16" /><strong>{{ approval.tool_name }}</strong><span>{{ approval.risk }}</span></div>
          <p v-if="approval.reason">{{ approval.reason }}</p>
          <pre>{{ approval.tool_name === 'git_project_write' ? gitApprovalSummary(approval.arguments) : JSON.stringify(approval.arguments, null, 2) }}</pre>
          <div class="task-actions">
            <button v-if="approval.tool_name === 'edit_project_file' && approval.arguments.change_id" class="task-btn" @click="fileReviewId = String(approval.arguments.change_id); fileReviewConversation = t.conversation_id || undefined">查看文件差异</button>
            <button v-if="approval.tool_name === 'run_project_command' && approval.arguments.command_id" class="task-btn" @click="commandReviewId = String(approval.arguments.command_id)">查看命令记录</button>
            <button v-if="approval.tool_name === 'git_project_write' && approval.arguments.operation_id && approval.arguments.project_id" class="task-btn" @click="gitReviewTarget = { id: String(approval.arguments.operation_id), project_id: String(approval.arguments.project_id) }">查看 Git 审阅</button>
            <button class="task-btn" type="button" :disabled="deciding.has(approval.approval_id)"
                    @click="decide(approval.approval_id, 'approve', 'once')"><Check :size="14" />允许一次</button>
            <button class="task-btn" type="button" :disabled="deciding.has(approval.approval_id)"
                    @click="decide(approval.approval_id, 'approve', 'session')"><ShieldCheck :size="14" />本次运行允许</button>
            <button class="task-btn cancel-btn" type="button" :disabled="deciding.has(approval.approval_id)"
                    @click="decide(approval.approval_id, 'deny', 'once')"><X :size="14" />拒绝</button>
          </div>
        </div>

        <div class="task-actions"><button v-for="change in changeEvents(t.events || [])" :key="change.change_id" class="task-btn" @click="fileReviewId = change.change_id; fileReviewConversation = t.conversation_id || undefined">查看差异 · {{ change.path }}</button></div>
        <div class="task-actions"><button v-for="command in commandEvents(t.events || [])" :key="command.command_id" class="task-btn" @click="commandReviewId = command.command_id">命令与输出 · {{ command.summary }}</button></div>
        <div class="task-actions"><button v-for="operation in gitOperationEvents(t.events || [])" :key="operation.operation_id" class="task-btn" @click="gitReviewTarget = { id: operation.operation_id, project_id: operation.project_id }">查看 Git 操作 · {{ gitActionLabels[operation.action] }}</button></div>
        <!-- 结果 -->
        <div v-if="t.result && t.result.answer" class="task-result">
          <div class="result-label">{{ t.execution_mode === 'plan' ? '待审阅计划' : '最终答案' }}</div>
          <div class="result-answer">{{ t.result.answer }}</div>
        </div>

        <!-- 错误 -->
        <div v-if="t.error" class="task-error" :class="{ 'task-notice': t.status === 'interrupted' }">{{ t.error }}</div>

        <div v-if="!isActive(t) && t.recovery?.available" class="task-recovery" :class="{ uncertain: t.recovery.pending_operation }">
          <p>已保留 {{ t.recovery.completed_tools || 0 }} 项工具结果 · 第 {{ t.recovery.step }} 轮</p>
          <template v-if="!isActive(t) && t.recovery.pending_operation">
            <strong>{{ t.recovery.pending_operation.kind === 'hook' ? '钩子' : '工具' }} · {{ t.recovery.pending_operation.name }}</strong>
            <p>应用在这一步执行期间中断，是否已完成无法确认。请检查执行结果后选择；重试可能重复操作。</p>
            <pre>{{ JSON.stringify(t.recovery.pending_operation.arguments, null, 2) }}</pre>
            <div class="task-actions">
              <button class="task-btn" type="button" :disabled="resuming.has(t.id)" @click="resume(t, 'retry')">重试这一步</button>
              <button class="task-btn" type="button" :disabled="resuming.has(t.id)" @click="resume(t, 'skip')">跳过这一步</button>
            </div>
          </template>
        </div>
        <p v-else-if="!isActive(t) && t.status !== 'done' && t.recovery?.reason" class="recovery-reason">{{ t.recovery.reason }}</p>

        <div class="task-actions">
          <button v-if="isActive(t)"
                  class="task-btn cancel-btn" type="button"
                  :disabled="cancelling.has(t.id)" @click="cancel(t)">
            <Square :size="13" />
            {{ cancelling.has(t.id) ? '取消中…' : '取消任务' }}
          </button>
          <button v-if="!isActive(t) && t.recovery?.available && !t.recovery.pending_operation"
                  class="task-btn" type="button" :disabled="resuming.has(t.id)" @click="resume(t)">
            <Play :size="13" />从检查点继续
          </button>
          <button v-if="!isActive(t)"
                  class="task-btn" type="button" :disabled="resuming.has(t.id)" @click="restart(t)">
            <RotateCcw :size="13" />
            从头重新运行
          </button>
          <button v-if="t.result && t.result.answer" class="task-btn" type="button"
                  @click="goChat(t)">
            <MessageSquareText :size="13" />
            查看对话
          </button>
        </div>
      </div>
    </div>
    <div v-if="taskView !== 'active' && (historyCursor || historyError)" class="task-history-footer">
      <p v-if="historyError" role="alert">{{ historyError }}</p>
      <button type="button" class="task-btn" :disabled="historyLoading" @click="loadMoreHistory">
        <ChevronDown :size="14" />{{ historyLoading ? '加载中…' : historyError ? '重试加载' : '加载更早任务' }}
      </button>
    </div>
    <FileChangeReview v-model="fileReviewId" :conversation-id="fileReviewConversation" />
    <ProjectCommandReview v-model="commandReviewId" />
    <GitOperationReview v-if="gitReviewTarget" :key="gitReviewTarget.id" :operation="gitReviewTarget" @close="gitReviewTarget = null" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  resumeBackgroundTask, restartBackgroundTask, cancelBackgroundTask, decideApproval,
} from '../api'
import { ElMessage } from 'element-plus'
import { Check, X, RefreshCw, RotateCcw, Square, ListTodo, ShieldAlert, ShieldCheck, MessageSquareText, Play, ChevronDown } from 'lucide-vue-next'
import { isActiveTask, taskState } from '../taskState'
import FileChangeReview from '../components/FileChangeReview.vue'
import ProjectCommandReview from '../components/ProjectCommandReview.vue'
import GitOperationReview from '../components/GitOperationReview.vue'
import { gitOperationEvents, gitActionLabels, gitApprovalSummary, type GitOperationTarget } from '../projectGitApi'
const gitReviewTarget = ref<GitOperationTarget | null>(null)
import { commandEvents } from '../projectCommandApi'
const commandReviewId = ref<string | null>(null)
const fileReviewId = ref<string | null>(null), fileReviewConversation = ref<string>()
function changeEvents(events: any[]) { return [...new Map(events.filter(e => e.kind === 'project_change' && e.change_id).map(e => [e.change_id, e])).values()] }

const router = useRouter()
const { allTasks, connection, runningTasks, pendingApprovals, loadError, reloadTasks: load,
  historyCursor, historyTotal, historyLoading, historyError, loadMoreHistory } = taskState
const taskTabs = [{ value: 'all', label: '全部' }, { value: 'active', label: '进行中' }, { value: 'history', label: '历史' }] as const
const taskView = ref<'all' | 'active' | 'history'>('all')
const visibleTasks = computed(() => allTasks.value.filter((task) =>
  taskView.value === 'all' || (taskView.value === 'active' ? isActiveTask(task) : !isActiveTask(task))))
function onTabKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const index = taskTabs.findIndex((tab) => tab.value === taskView.value)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? taskTabs.length - 1
    : (index + (event.key === 'ArrowRight' ? 1 : -1) + taskTabs.length) % taskTabs.length
  taskView.value = taskTabs[next].value
  const buttons = (event.currentTarget as HTMLElement).querySelectorAll<HTMLButtonElement>('[role="tab"]')
  buttons[next]?.focus()
}
onMounted(() => { void load() })
onUnmounted(taskState.resetTaskHistory)
const connectionLabel = computed(() => ({ connecting: '正在连接', connected: '实时连接', reconnecting: '重新连接中', polling: '定时刷新' })[connection.value])
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
    ElMessage.error(e?.response?.status === 404 ? '该审批已结束，任务状态已刷新' : e?.response?.data?.detail || '审批失败')
  } finally {
    await load()
    deciding.value.delete(id)
  }
}
function statusLabel(s: string) {
  return { pending: '排队中', running: '运行中', done: '已完成', failed: '失败',
           paused: '已暂停', cancelled: '已取消', interrupted: '已中断' }[s] || s
}
const isActive = isActiveTask
function eventLabel(k: string) {
  return { tool_call: '调用', tool_result: '结果', status: '状态', subagents: '子任务',
           project_command: '命令', project_change: '文件', git_operation: 'Git 操作',
           error: '错误', cancelled: '取消', approval_request: '待审批',
           approval_result: '审批', hook: '钩子', resume: '恢复', interrupted: '中断' }[k] || k
}
async function resume(t: { id: string }, resolution?: 'retry' | 'skip') {
  if (resuming.value.has(t.id)) return
  resuming.value.add(t.id)
  try {
    await resumeBackgroundTask(t.id, resolution)
    ElMessage.success('已从检查点继续')
    await load()
  } catch (e: any) {
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
.tasks-page { padding: 26px 30px; max-width: 980px; overflow-y: auto; }
.tasks-head { display: flex; align-items: flex-end; justify-content: space-between; margin-bottom: 22px; }
.tasks-title h2 { margin: 0 0 6px; font-size: 21px; }
.tasks-title p { margin: 0; font-size: 13px; color: var(--fn-text-tertiary); }
.task-tabs { display: flex; gap: 20px; margin-bottom: 20px; border-bottom: 1px solid var(--fn-border-primary); }
.task-tabs button { display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 2px; border: 0; border-bottom: 2px solid transparent; background: transparent; color: var(--fn-text-secondary); font: inherit; font-size: 13px; cursor: pointer; }
.task-tabs button.selected { color: var(--fn-brand); border-bottom-color: var(--fn-brand); }
.task-tabs span { color: var(--fn-text-tertiary); font-size: 11px; font-variant-numeric: tabular-nums; }
.task-history-footer { padding: 18px 0; text-align: center; font-size: 13px; color: var(--fn-text-secondary); }
.refresh-btn {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  border-radius: 8px; padding: 7px 13px; font-size: 13px; cursor: pointer;
}
.refresh-btn svg { width: 15px; height: 15px; }
.empty { text-align: center; padding: 70px 0; color: var(--fn-text-quaternary); }
.empty svg { width: 46px; height: 46px; margin-bottom: 12px; }
.empty div { font-size: 15px; margin-bottom: 6px; }
.empty p { font-size: 12.5px; margin: 0; }
.task-list { display: flex; flex-direction: column; gap: 14px; }
.task-card {
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
  padding: 16px 18px;
}
.task-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.task-title { min-width: 0; overflow-wrap: anywhere; font-size: 14.5px; font-weight: 600; }
.task-status { flex-shrink: 0; font-size: 11.5px; padding: 3px 10px; border-radius: 4px; }
.task-status.awaiting { background: #fff0ce; color: #855700; }
.task-status.running { background: #e6f0ff; color: #0a59f7; }
.task-status.done { background: #e9f9ef; color: #16a34a; }
.task-status.failed { background: #fdecec; color: #dc2626; }
.task-status.cancelled { background: #f3f4f6; color: #6b7280; }
.task-status.pending { background: var(--fn-bg-secondary); color: var(--fn-text-tertiary); }
.task-status.interrupted { background: #fff0ce; color: #855700; }
.task-progress { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.tp-bar { flex: 1; height: 6px; border-radius: 999px; background: var(--fn-bg-secondary); overflow: hidden; }
.tp-fill { height: 100%; border-radius: 999px; background: #0a59f7; transition: width .4s; }
.tp-pct { font-size: 11.5px; color: var(--fn-text-tertiary); min-width: 36px; text-align: right; }
.task-plan { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.plan-step { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--fn-text-tertiary); }
.step-dot { width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid var(--fn-border-strong); }
.plan-step.in_progress .step-dot { border-color: #0a59f7; background: #0a59f7; }
.plan-step.done { color: var(--fn-text-secondary); }
.plan-step.done .step-dot { border-color: #16a34a; background: #16a34a; }
.task-events { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.event-line { display: flex; gap: 8px; font-size: 12px; color: var(--fn-text-secondary); line-height: 1.5; }
.event-kind {
  flex-shrink: 0; font-size: 10.5px; color: var(--fn-text-tertiary);
  min-width: 34px; padding-top: 1px;
}
.task-result { border-top: 1px solid var(--fn-border-secondary); padding-top: 10px; }
.result-label { font-size: 11.5px; color: var(--fn-text-tertiary); margin-bottom: 5px; }
.result-answer { font-size: 13px; line-height: 1.7; white-space: pre-wrap; max-height: 220px; overflow: auto; }
.task-error { font-size: 12.5px; color: #dc2626; background: #fef2f2; border-radius: 8px; padding: 8px 11px; margin-bottom: 10px; }
.task-notice, .task-recovery.uncertain { background: #fff9ed; color: #775719; }
.task-recovery { padding: 10px 12px; border: 1px solid var(--fn-border-primary); background: var(--fn-bg-secondary); color: var(--fn-text-secondary); border-radius: 8px; font-size: 12.5px; line-height: 1.6; }
.task-recovery.uncertain { border-color: #efdcb6; }
.task-recovery p { margin: 0 0 6px; }
.task-recovery p:last-child { margin-bottom: 0; }
.task-recovery pre { max-height: 180px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.recovery-reason { color: var(--fn-text-secondary); font-size: 12.5px; }
.task-actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.task-btn {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  border-radius: 8px; padding: 6px 12px; font-size: 12.5px; cursor: pointer;
}
.task-btn svg { width: 13px; height: 13px; }
.task-btn:disabled { opacity: .55; cursor: default; }
.cancel-btn { color: #dc2626; }
.cancel-btn:hover:not(:disabled) { border-color: #fecaca; background: #fef2f2; }
.task-approval { border-top: 1px solid #e8d6a4; padding: 12px 0; }
.approval-heading { display: flex; align-items: center; gap: 8px; color: #855700; font-size: 13px; overflow-wrap: anywhere; }
.approval-heading strong { min-width: 0; }
.approval-heading span { margin-left: auto; font-size: 11px; }
.task-approval p { font-size: 12px; color: var(--fn-text-secondary); overflow-wrap: anywhere; }
.task-approval pre { margin: 10px 0; padding: 10px; max-height: 180px; overflow: auto; background: var(--fn-bg-secondary); font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.event-line > span:last-child, .step-text, .result-answer { min-width: 0; overflow-wrap: anywhere; }
</style>
