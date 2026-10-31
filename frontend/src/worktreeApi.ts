import axios from 'axios'
const http = axios.create({ baseURL: '/api' })
export interface WorktreeRequest { request_id: string; name: string; ref: string; branch: string }
export interface WorktreeDraft { name: string; ref: string; branch: string; pending: WorktreeRequest | null }
export interface Worktree {
  id: string; source_project_id: string; project_id: string | null; name: string; path: string
  base_ref: string; base_head: string; branch: string; revision: number
  status: 'creating' | 'ready' | 'removing' | 'archiving' | 'removed' | 'restoring' | 'interrupted' | 'failed'
  saved_head: string | null; error: string; output: string
  removal_snapshot: { head: string; branch: string | null; identity: string; kind?: 'archive';
    files?: number; bytes?: number; ignored?: number; preview?: Array<{ path: string; ignored: boolean }> } | null
  archive_info: { id: string; head: string; index_head: string; original_head: string; phase: string;
    files: number; bytes: number; ignored: number } | null
  available?: boolean; current_branch?: string | null; current_head?: string
}
export interface WorktreeListing {
  source_project_id: string; source_name: string; source_path: string; error: string; worktrees: Worktree[]
  external: Array<{ worktree: string; HEAD: string; branch?: string; locked?: string; prunable?: string }>
}
export interface WorktreeArchiveSnapshot {
  id: string; head: string; ref: string; backup: string; current: boolean
  backup_present: boolean; backup_valid: boolean; sha256: string | null
  files: number | null; bytes: number | null; ignored: number | null; created_at: string | null
}
export const worktreeStatus = { creating: '创建中', ready: '可使用', removing: '移除中', archiving: '正在归档', removed: '已移除', restoring: '恢复中', interrupted: '需要核对', failed: '未创建' }
export const listWorktrees = (project: string): Promise<WorktreeListing> => http.get(`/projects/${project}/worktrees`, { timeout: 20000 }).then(r => r.data)
export const createWorktree = (project: string, request: WorktreeRequest): Promise<Worktree> => http.post(`/projects/${project}/worktrees`, request, { timeout: 150000 }).then(r => r.data)
export const reviewWorktreeRemoval = (project: string, id: string): Promise<Worktree> => http.post(`/projects/${project}/worktrees/${id}/review-removal`, {}, { timeout: 30000 }).then(r => r.data)
export const reviewWorktreeArchive = (project: string, id: string): Promise<Worktree> => http.post(`/projects/${project}/worktrees/${id}/review-archive`, {}, { timeout: 90000 }).then(r => r.data)
export const worktreeBackupUrl = (project: string, id: string) => `/api/projects/${project}/worktrees/${id}/archive-backup`
export const listWorktreeArchiveHistory = (project: string, id: string): Promise<{ worktree_id: string; current_id: string | null; snapshots: WorktreeArchiveSnapshot[] }> =>
  http.get(`/projects/${project}/worktrees/${id}/archive-history`, { timeout: 30000 }).then(r => r.data)
export const deleteWorktreeArchiveSnapshot = (project: string, id: string, snapshotId: string): Promise<{ id: string; deleted: boolean }> =>
  http.delete(`/projects/${project}/worktrees/${id}/archive-history/${encodeURIComponent(snapshotId)}`, { timeout: 30000 }).then(r => r.data)
export const worktreeAction = (project: string, item: Worktree, action: 'remove' | 'archive' | 'restore' | 'reconcile'): Promise<Worktree> =>
  http.post(`/projects/${project}/worktrees/${item.id}/${action}`, { revision: item.revision }, { timeout: 150000 }).then(r => r.data)
