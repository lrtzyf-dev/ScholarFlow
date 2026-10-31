import { computed, readonly, ref } from 'vue'
import { listBackgroundTasks, listTaskHistory, type BackgroundTask, type TaskSnapshot, type Conversation } from './api'

const tasks = ref<BackgroundTask[]>([])
const conversations = ref<Conversation[] | null>(null)
const conversationVersion = ref(0)
let conversationEpoch: string | undefined
const olderTasks = ref<BackgroundTask[]>([])
const historyCursor = ref<string | null>(null)
const historyTotal = ref(0)
const historyLoading = ref(false)
const historyError = ref('')
const allTasks = computed(() => {
  const liveIds = new Set(tasks.value.map((task) => task.id))
  return [...tasks.value, ...olderTasks.value.filter((task) => !liveIds.has(task.id))]
})
let historyExpanded = false
let historyGeneration = 0
let snapshotHistoryCursor: string | null = null
let historyEpoch: string | undefined
export const isActiveTask = (task: { status: string; executing?: boolean }) => task.executing || ['pending', 'running'].includes(task.status)
const connection = ref<'connecting' | 'connected' | 'reconnecting' | 'polling'>('connecting')
const loadError = ref('')
const pendingApprovals = computed(() => tasks.value.reduce((sum, task) => sum + (task.approvals?.length || 0), 0))
const runningTasks = computed(() => tasks.value.filter(isActiveTask).length)
let version = 0
let initialized = false
let refreshing: Promise<void> | null = null
let transition: ((task: BackgroundTask) => void) | undefined

function acceptSnapshot(payload: TaskSnapshot) {
  const snapshot = payload.tasks
  const previous = new Map(tasks.value.map((task) => [task.id, task.status]))
  const changes = initialized ? snapshot.filter((task) =>
    ['pending', 'running'].includes(previous.get(task.id) || '') &&
    ['done', 'failed', 'cancelled', 'interrupted'].includes(task.status),
  ) : []
  version += 1
  initialized = true
  tasks.value = snapshot
  if (Array.isArray(payload.conversations)) conversations.value = payload.conversations
  if (payload.conversation_epoch !== conversationEpoch || Array.isArray(payload.conversations)) {
    conversationEpoch = payload.conversation_epoch
    conversationVersion.value++
  }
  if (payload.history_epoch !== undefined) {
    if (historyEpoch !== undefined && historyEpoch !== payload.history_epoch) resetTaskHistory()
    historyEpoch = payload.history_epoch
  }
  const latest = new Map(snapshot.map((task) => [task.id, task]))
  olderTasks.value = olderTasks.value.map((task) => latest.get(task.id) || task)
  snapshotHistoryCursor = payload.next_cursor || null
  if (!historyExpanded) historyCursor.value = snapshotHistoryCursor
  historyTotal.value = payload.history_total ?? snapshot.filter((task) => !isActiveTask(task)).length
  loadError.value = ''
  for (const task of changes) transition?.(task)
}

async function refreshTasks() {
  if (refreshing) return refreshing
  const startedAt = version
  refreshing = (async () => {
    try {
      const result = await listBackgroundTasks()
      // An older HTTP response must not overwrite a newer SSE snapshot.
      if (startedAt === version) acceptSnapshot(result)
    } catch {
      loadError.value = '无法获取后台任务'
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

function resetTaskHistory() {
  historyGeneration++
  olderTasks.value = []
  historyCursor.value = snapshotHistoryCursor
  historyExpanded = false
  historyError.value = ''
  historyLoading.value = false
}

async function loadMoreHistory() {
  if (!historyCursor.value || historyLoading.value) return
  const generation = historyGeneration
  const startedAt = version
  const cursor = historyCursor.value
  const retained = new Map(olderTasks.value.map((task) => [task.id, task]))
  for (const task of tasks.value) if (!isActiveTask(task)) retained.set(task.id, task)
  olderTasks.value = [...retained.values()]
  historyExpanded = true
  historyLoading.value = true
  historyError.value = ''
  try {
    const result = await listTaskHistory(cursor)
    if (generation !== historyGeneration) return
    const merged = new Map(olderTasks.value.map((task) => [task.id, task]))
    for (const task of result.tasks) merged.set(task.id, task)
    olderTasks.value = [...merged.values()]
    historyExpanded = true
    historyCursor.value = result.next_cursor || null
    if (startedAt === version) historyTotal.value = result.history_total ?? historyTotal.value
  } catch {
    if (generation === historyGeneration) historyError.value = '无法加载更早的任务，请重试'
  } finally {
    if (generation === historyGeneration) historyLoading.value = false
  }
}

async function reloadTasks() {
  resetTaskHistory()
  await refreshTasks()
}

function startTaskUpdates(onTransition: (task: BackgroundTask) => void) {
  transition = onTransition
  initialized = false
  let events: EventSource | null = null
  if ('EventSource' in window) {
    events = new EventSource('/api/tasks/events?conversation_mode=signal')
    events.onopen = () => { connection.value = 'connected' }
    events.onerror = () => { connection.value = 'reconnecting' }
    events.addEventListener('tasks', (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data)
        if (Array.isArray(payload.tasks)) acceptSnapshot(payload)
      } catch {
        // Keep the last valid snapshot when a frame is malformed.
      }
    })
  } else {
    connection.value = 'polling'
  }
  void refreshTasks()
  const timer = window.setInterval(() => {
    if (connection.value !== 'connected') void refreshTasks()
  }, 10000)
  return () => {
    events?.close()
    window.clearInterval(timer)
    transition = undefined
    version += 1
  }
}

export const taskState = {
  conversationVersion: readonly(conversationVersion),
  conversations: readonly(conversations),
  tasks: readonly(tasks), connection: readonly(connection), loadError: readonly(loadError),
  allTasks, historyCursor: readonly(historyCursor), historyTotal: readonly(historyTotal),
  historyLoading: readonly(historyLoading), historyError: readonly(historyError),
  pendingApprovals, runningTasks, refreshTasks, reloadTasks, loadMoreHistory, resetTaskHistory, startTaskUpdates,
}
