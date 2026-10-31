import { reactive } from 'vue'
import { setConversationArchived, type ConversationArchiveState } from './api'
import { conversationState } from './desktopState'
import { taskState } from './taskState'

type Operation = { id: string; archived: boolean; revision: number }
export const conversationArchive = reactive({
  pending: new Set<string>(),
  error: '',
  retry: null as Operation | null,
})

export async function changeConversationArchive(id: string, archived: boolean, revision: number): Promise<ConversationArchiveState | null> {
  if (conversationArchive.pending.has(id)) return null
  conversationArchive.pending.add(id)
  conversationArchive.error = ''
  conversationArchive.retry = null
  try {
    const doc = conversationState(id)
    await doc.ready
    await doc.flush()
    const saved = await setConversationArchived(id, archived, revision)
    window.dispatchEvent(new CustomEvent('scholarflow:conversation-archive', { detail: saved }))
    window.dispatchEvent(new Event('scholarflow:conversation-updated'))
    await taskState.refreshTasks()
    return saved
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    conversationArchive.error = (typeof detail === 'string' ? detail : detail?.message) ||
      (archived ? '归档失败，请重试' : '恢复失败，请重试')
    const status = error?.response?.status
    if (!status || status >= 500) conversationArchive.retry = { id, archived, revision }
    else await taskState.refreshTasks()
    return null
  } finally {
    conversationArchive.pending.delete(id)
  }
}

export function retryConversationArchive() {
  const op = conversationArchive.retry
  if (op) void changeConversationArchive(op.id, op.archived, op.revision)
}
