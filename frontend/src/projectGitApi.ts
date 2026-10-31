import axios from 'axios'
const http = axios.create({ baseURL: '/api' })

export interface GitEntry {
  path: string
  original_path?: string
  index: string
  worktree: string
  staged: boolean
  unstaged: boolean
  untracked: boolean
  conflict: boolean
}
export interface GitStatus {
  snapshot?: string
  available: boolean
  reason: string
  branch: string | null
  head: string | null
  linked_worktree: boolean
  entries: GitEntry[]
  total: number
  staged_total?: number
  unstaged_total?: number
  next_offset: number | null
}
export interface GitDiff {
  snapshot: string
  path: string
  version: 'staged' | 'unstaged' | 'untracked'
  patch: string | null
  preview: string | null
  next_offset: number | null
  binary: boolean
}
export interface GitCommit { id: string; short_id: string; date: string; subject: string }

export const gitStatus = (project: string, offset = 0, snapshot?: string): Promise<GitStatus> =>
  http.get(`/projects/${project}/git/status`, { params: { offset, snapshot } }).then(r => r.data)
export const gitDiff = (project: string, path: string, version: GitDiff['version'], offset = 0, snapshot?: string): Promise<GitDiff> =>
  http.get(`/projects/${project}/git/diff`, { params: { path, version, offset, snapshot } }).then(r => r.data)
export const gitHistory = (project: string): Promise<{ commits: GitCommit[] }> =>
  http.get(`/projects/${project}/git/history`).then(r => r.data)

export interface GitRequest {
  request_id: string
  action: 'stage' | 'unstage' | 'commit'
  paths: string[] | null
  message: string
}
export interface GitOperation {
  id: string; project_id: string; action: GitRequest['action']; paths: string[]; message: string
  status: 'prepared' | 'running' | 'applied' | 'failed' | 'interrupted' | 'conflict' | 'cancelled'
  revision: number; error: string; output: string; result_head: string | null
  created_at: string; updated_at: string
  patch?: string; branch?: string | null; head?: string | null; author?: string | null
  agent_owned?: boolean
}
export interface GitOperationTarget { id: string; project_id: string }
export const gitActionLabels = { stage: '暂存', unstage: '取消暂存', commit: '提交' }
export const gitStatusLabels = { prepared: '待确认', running: '执行中', applied: '已完成', failed: '失败', interrupted: '结果未确认', conflict: '内容已变化', cancelled: '已取消' }
export function gitApprovalSummary(arguments_: Record<string, any>): string {
  const paths = Array.isArray(arguments_.paths) ? arguments_.paths : []
  const action = gitActionLabels[arguments_.action as GitRequest['action']] || 'Git 操作'
  const branch = arguments_.branch?.replace('refs/heads/', '') || 'detached HEAD'
  return [`${action} · ${paths.length} 个文件 · ${branch}`,
    ...(arguments_.message ? [arguments_.message] : []), ...paths.slice(0, 8),
    ...(paths.length > 8 ? [`另有 ${paths.length - 8} 个文件，打开审阅查看完整范围。`] : [])].join('\n')
}
export interface GitOperationEvent {
  operation_id: string; project_id: string; action: GitRequest['action']; status: GitOperation['status']
}
export function gitOperationEvents(events: Array<{ kind: string; [key: string]: any }>): GitOperationEvent[] {
  return [...new Map(events.filter(e => e.kind === 'git_operation' && e.operation_id && e.project_id)
    .map(e => [e.operation_id, e as unknown as GitOperationEvent])).values()]
}
export const gitPrepare = (project: string, request: GitRequest): Promise<GitOperation> =>
  http.post(`/projects/${project}/git/operations`, request, { timeout: 30000 }).then(r => r.data)
export const gitOperations = (project: string, offset = 0): Promise<{ operations: GitOperation[]; total: number; next_offset: number | null }> =>
  http.get(`/projects/${project}/git/operations`, { params: { offset }, timeout: 15000 }).then(r => r.data)
export const gitOperation = (project: string, id: string): Promise<GitOperation> =>
  http.get(`/projects/${project}/git/operations/${id}`, { timeout: 15000 }).then(r => r.data)
export const gitDecide = (operation: GitOperation, cancel = false): Promise<GitOperation> =>
  http.post(`/projects/${operation.project_id}/git/operations/${operation.id}/${cancel ? 'cancel' : 'apply'}`, { revision: operation.revision }, { timeout: 75000 }).then(r => r.data)
