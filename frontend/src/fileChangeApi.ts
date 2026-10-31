import axios from 'axios'
const http = axios.create({ baseURL: '/api' })
export interface FileChange {
  id: string; project_id: string; conversation_id: string; run_id: string | null; path: string
  operation: 'update' | 'create' | 'delete'; summary: string; status: string; error: string; revision: number
  before_hash: string | null; after_hash: string | null; undo_of: string | null; created_at: string
}
export interface DiffLine { kind: 'hunk' | 'context' | 'add' | 'remove'; text: string; old: number | null; new: number | null }
interface Format { bom: boolean; final_newline: boolean; crlf: number; lf: number; bytes: number }
export interface ChangeDetail extends FileChange {
  run_active: boolean
  diff: { lines: DiffLine[]; added: number; removed: number; next_offset: number | null; total_lines: number; before_format: Format | null; after_format: Format | null }
}
export const statusLabel = (status: string) => ({ proposed: '待应用', applying: '正在应用', applied: '已应用', rejected: '已放弃', conflict: '有冲突', interrupted: '写入中断' }[status] || status)
export const operationLabel = (op: string) => ({ update: '修改', create: '新建', delete: '删除' }[op] || op)
export const editableFile = (project: string, path: string): Promise<{ content: string | null; sha256: string | null }> => http.get(`/projects/${project}/editable-file`, { params: { path } }).then(r => r.data)
export const prepareChange = (project: string, conversation_id: string, request_id: string, edit: { path: string; operation: string; expected_sha256?: string | null; content?: string | null; summary?: string }, workspace_revision = 0): Promise<FileChange> => http.post(`/projects/${project}/changes`, { conversation_id, request_id, edit, workspace_revision }).then(r => r.data)
export const listChanges = (project: string, offset = 0): Promise<{ changes: FileChange[]; next_offset: number | null; total: number }> => http.get(`/projects/${project}/changes`, { params: { offset } }).then(r => r.data)
export const getChange = (id: string, offset = 0): Promise<ChangeDetail> => http.get(`/project-changes/${id}`, { params: { offset } }).then(r => r.data)
export const getChangeContent = (id: string, version: string): Promise<{ content: string | null }> => http.get(`/project-changes/${id}/content`, { params: { version } }).then(r => r.data)
export const decideChange = (id: string, revision: number, action: 'apply' | 'reject'): Promise<FileChange> => http.post(`/project-changes/${id}/${action}`, { revision }).then(r => r.data)
export const undoChange = (id: string, conversation_id: string): Promise<FileChange> => http.post(`/project-changes/${id}/undo`, { conversation_id }).then(r => r.data)
