import { computed, ref, shallowRef } from 'vue'
import type { GitRequest } from './projectGitApi'
import { emptySearchOptions, type SearchOptions } from './projectSearchApi'

export interface DraftImage { media_type: string; data: string }
export interface ReadingPosition { following: boolean; top: number; anchor_id: string | null; offset: number }
export interface EditorDraft { project_id: string; path: string; operation: 'update' | 'create'; sha256: string | null; content: string }
export interface ConversationState {
  workspace_revision?: number
  editor?: EditorDraft | null
  file_paths?: string[]
  text: string; execution_mode: 'execute' | 'plan'; paper_id: string | null; skills: string[]; images: DraftImage[]; reading: ReadingPosition
}
type Status = 'loading' | 'saved' | 'saving' | 'pending' | 'error' | 'conflict' | 'deleted'
interface Snapshot<T> { revision: number; value: T }

function localRead(key: string) {
  try { return localStorage.getItem(key) } catch { return null }
}
function localWrite(key: string, value: string) {
  try {
    if (value) localStorage.setItem(key, value)
    else localStorage.removeItem(key)
  } catch { /* The database status remains the source of truth. */ }
}
function validSnapshot<T>(data: any): data is Snapshot<T> {
  return Number.isInteger(data?.revision) && data.revision >= 0 && data.value && typeof data.value === 'object'
}

// One serialized writer per document, surviving route changes. CAS also protects
// against another application process sharing the same SQLite data directory.
class Document<T> {
  readonly value
  readonly status = ref<Status>('loading')
  readonly error = ref('')
  readonly restored = ref(0)
  readonly ready: Promise<void>
  private revision = 0
  private loaded = false
  private generation = 0
  private dirty = false
  private timer: ReturnType<typeof setTimeout> | undefined
  private writing: Promise<void> | null = null
  private remote: Snapshot<T> | null = null
  private forgotten = false

  get persisted() { return this.revision > 0 }
  get savedRevision() { return this.revision }

  constructor(readonly path: string, initial: T, private selection = false) {
    this.value = shallowRef<T>(initial)
    this.ready = this.load()
  }

  private async request(method: string, payload?: unknown, keepalive = false): Promise<Snapshot<T>> {
    const body = payload === undefined ? undefined : JSON.stringify(payload)
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 5000)
    try {
      const response = await fetch(`/api/desktop-state/${this.path}`, {
        method, headers: { 'Content-Type': 'application/json' }, body,
        // Browsers cap the combined keepalive budget at 64 KiB. Large images
        // are saved while the window is open, with an acknowledged status.
        keepalive: keepalive && new Blob([body || '']).size < 60_000,
        signal: controller.signal,
      })
      const data = await response.json()
      if (!response.ok) throw Object.assign(new Error(data.detail?.message || data.detail || '保存失败'), { status: response.status, snapshot: data.detail })
      if (!validSnapshot<T>(data)) throw new Error('本机存储返回无效数据')
      return data
    } finally { clearTimeout(timeout) }
  }

  private async load() {
    try {
      const saved = await this.request('GET')
      if (this.forgotten) return
      this.loaded = true
      this.revision = saved.revision
      if (this.dirty && saved.revision > 0 && !this.selection) {
        this.remote = saved
        this.status.value = 'conflict'
      } else if ((saved.revision > 0 || (saved.value as any).workspace_revision > 0) && !this.dirty) {
        this.value.value = saved.value
        this.restored.value++
        this.status.value = 'saved'
      } else {
        this.status.value = this.dirty ? 'pending' : 'saved'
        if (this.dirty) void this.flush()
      }
    } catch (error: any) {
      this.status.value = error.status === 410 ? 'deleted' : 'error'
      this.error.value = error.message || '读取本机存储失败'
    }
  }

  update(value: T) {
    if (this.forgotten || this.status.value === 'deleted') return
    this.value.value = value
    this.generation++
    this.dirty = true
    if (!['error', 'conflict', 'loading'].includes(this.status.value)) this.status.value = 'pending'
    clearTimeout(this.timer)
    this.timer = setTimeout(() => { void this.flush() }, 250)
  }

  async flush(keepalive = false): Promise<void> {
    clearTimeout(this.timer)
    if (this.writing) { await this.writing; if (this.dirty) await this.flush(keepalive); return }
    if (!this.loaded || !this.dirty || this.forgotten || ['error', 'conflict', 'deleted'].includes(this.status.value)) return
    this.writing = this.save(keepalive)
    await this.writing
    this.writing = null
    if (this.dirty && this.status.value === 'pending') await this.flush(keepalive)
  }

  private async save(keepalive: boolean) {
    const generation = this.generation
    const value = this.value.value
    this.status.value = 'saving'
    try {
      const saved = await this.request('PUT', { revision: this.revision, value }, keepalive)
      if (this.forgotten) return
      this.revision = saved.revision
      this.dirty = generation !== this.generation
      this.status.value = this.dirty ? 'pending' : 'saved'
      this.error.value = ''
    } catch (error: any) {
      if (this.forgotten) return
      if (error.status === 409 && validSnapshot<T>(error.snapshot)) {
        this.remote = error.snapshot
        // A response may be lost after a successful commit. Recognize our own
        // exact snapshot so retrying it doesn't report a false conflict.
        if (this.selection || JSON.stringify(error.snapshot.value) === JSON.stringify(value)) {
          this.revision = error.snapshot.revision
          this.dirty = this.selection || generation !== this.generation
          this.status.value = this.dirty ? 'pending' : 'saved'
          return
        }
        this.status.value = 'conflict'
        this.error.value = error.message
      } else {
        this.status.value = error.status === 410 ? 'deleted' : 'error'
        this.error.value = error.message || '保存失败'
      }
    }
  }

  async retry() {
    if (this.status.value === 'conflict' || this.status.value === 'deleted') return
    if (!this.loaded) await this.load()
    else this.status.value = this.dirty ? 'pending' : 'saved'
    await this.flush()
  }

  async resolveConflict(keepLocal: boolean) {
    if (!this.remote) return
    this.revision = this.remote.revision
    if (!keepLocal) { this.value.value = this.remote.value; this.dirty = false; this.restored.value++ }
    else if (this.path.startsWith('conversations/')) {
      const local = this.value.value as ConversationState, remote = this.remote.value as ConversationState
      if ((local.workspace_revision || 0) !== (remote.workspace_revision || 0)) {
        // Keep the chosen text/editor draft, but never reinterpret relative
        // references from an earlier checkout. The editor has an explicit pid.
        this.value.value = { ...local, workspace_revision: remote.workspace_revision || 0, file_paths: remote.file_paths || [] } as T
        this.restored.value++
      }
    }
    this.remote = null
    this.status.value = this.dirty ? 'pending' : 'saved'
    await this.flush()
  }

  async refresh() {
    await this.ready
    if (this.writing) await this.writing
    if (this.forgotten || this.status.value === 'deleted') return
    try {
      const saved = await this.request('GET')
      if (this.forgotten || saved.revision < this.revision) return
      if (this.dirty) {
        if (saved.revision > this.revision) {
          this.remote = saved; this.status.value = 'conflict'
          this.error.value = '另一窗口已更新草稿或切换工作目录，请核对要保留的内容'
        }
      } else {
        this.revision = saved.revision; this.value.value = saved.value
        this.loaded = true; this.remote = null; this.status.value = 'saved'; this.error.value = ''
        this.restored.value++
      }
    } catch (error: any) {
      this.status.value = error.status === 410 ? 'deleted' : 'error'
      this.error.value = error.message || '刷新草稿失败'
    }
  }

  forget() { this.forgotten = true; this.dirty = false; clearTimeout(this.timer) }
}

const documents = new Map<string, Document<ConversationState>>()
const gitDocuments = new Map<string, Document<{ message: string; pending: GitRequest | null }>>()
const searchDocuments = new Map<string, Document<SearchOptions>>()
const worktreeDocuments = new Map<string, Document<import('./worktreeApi').WorktreeDraft>>()
export function projectWorktreeState(id: string) {
  let doc = worktreeDocuments.get(id)
  if (!doc) {
    doc = new Document(`projects/${encodeURIComponent(id)}/worktrees`, { name: '', ref: 'HEAD', branch: '', pending: null } as import('./worktreeApi').WorktreeDraft)
    worktreeDocuments.set(id, doc)
  }
  return doc
}
export function projectSearchState(id: string) {
  let doc = searchDocuments.get(id)
  if (!doc) {
    doc = new Document(`projects/${encodeURIComponent(id)}/search`, emptySearchOptions())
    searchDocuments.set(id, doc)
  }
  return doc
}
export function projectGitState(id: string) {
  let doc = gitDocuments.get(id)
  if (!doc) {
    doc = new Document(`projects/${encodeURIComponent(id)}/git`, { message: '', pending: null } as { message: string; pending: GitRequest | null })
    gitDocuments.set(id, doc)
  }
  return doc
}
const documentList = shallowRef<Document<ConversationState>[]>([])
const selection = new Document<{ conversation_id: string | null }>('selection', { conversation_id: null }, true)
export const sidebarState = new Document<{ sort: 'updated_at' | 'created_at' }>('sidebar', { sort: 'updated_at' }, true)
export const activeConversationId = ref('')
export const desktopStateIssues = computed(() => documentList.value.filter((doc) => ['error', 'conflict'].includes(doc.status.value)))
export const selectionStatus = selection.status
export const retrySelection = () => selection.retry()

export function emptyConversationState(): ConversationState {
  return { text: '', execution_mode: 'execute', paper_id: null, skills: [], images: [], reading: { following: true, top: 0, anchor_id: null, offset: 0 } }
}
export function conversationState(id: string) {
  let doc = documents.get(id)
  if (!doc) {
    const initial = emptyConversationState()
    initial.text = localRead(`scholarflow_draft:${id}`) || ''
    doc = new Document<ConversationState>(`conversations/${encodeURIComponent(id)}`, initial)
    documents.set(id, doc)
    documentList.value = [...documents.values()]
    // Import same-origin legacy text only into a document that has never been
    // saved. Existing database drafts always win over the compatibility cache.
    void doc.ready.then(() => {
      if (doc!.status.value === 'saved' && doc!.value.value === initial && initial.text) doc!.update(initial)
    })
  }
  return doc
}
export function cacheDraftText(id: string, text: string) { localWrite(`scholarflow_draft:${id}`, text) }
export function selectConversation(id: string) {
  activeConversationId.value = id
  localWrite('scholarflow_conversation_id', id)
  selection.update({ conversation_id: id })
  void selection.flush()
}
export function forgetConversation(id: string) {
  documents.get(id)?.forget()
  documents.delete(id)
  documentList.value = [...documents.values()]
  cacheDraftText(id, '')
}
export function flushDesktopState(keepalive = false) {
  return Promise.all([selection.flush(keepalive), sidebarState.flush(keepalive), ...[...documents.values()].map((doc) => doc.flush(keepalive)), ...[...gitDocuments.values()].map(doc => doc.flush(keepalive)), ...[...searchDocuments.values()].map(doc => doc.flush(keepalive)), ...[...worktreeDocuments.values()].map(doc => doc.flush(keepalive))])
}
export async function initializeDesktopState() {
  await selection.ready
  const legacy = localRead('scholarflow_conversation_id')
  const saved = selection.value.value.conversation_id
  let id = saved || (!selection.persisted && legacy) || `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
  activeConversationId.value = id
  await conversationState(id).ready
  if (conversationState(id).status.value === 'deleted') {
    forgetConversation(id)
    id = `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
    await conversationState(id).ready
  }
  selectConversation(id)
  window.addEventListener('pagehide', () => { void flushDesktopState(true) })
  window.addEventListener('online', () => { void selection.retry(); void sidebarState.retry(); for (const doc of documents.values()) void doc.retry(); for (const doc of gitDocuments.values()) void doc.retry(); for (const doc of searchDocuments.values()) void doc.retry(); for (const doc of worktreeDocuments.values()) void doc.retry() })
}
