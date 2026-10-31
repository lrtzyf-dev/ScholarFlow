import axios from 'axios'
import type { SwitchScope } from './workspaceSwitchApi'
const http = axios.create({ baseURL: '/api' })
export interface WorkspaceTransfer {
  id: string; conversation_id: string; source_project_id: string; target_project_id: string
  status: 'prepared' | 'applying' | 'interrupted' | 'rolling_back' | 'applied' | 'rolled_back' | 'cancelled'
  error: string; backup_available: boolean
  review: { mode: WorkspaceTransferMode; source: SwitchScope; target: SwitchScope; blockers: string[]; relation: string
    source_branch?: string | null; target_branch?: string | null; files: number
    affected: { id: string; title: string; project_id: string }[]
    changes: { path: string; original_path?: string; staged: boolean; unstaged: boolean; untracked: boolean }[] }
  progress: { cursor: number | null; total: number | null; phase: string | null }
}
const path = (cid: string) => `/conversations/${encodeURIComponent(cid)}/workspace-transfers`
export type WorkspaceTransferMode = 'changes' | 'git_state'
export const prepareWorkspaceTransfer = (cid: string, request: { request_id: string; target_project_id: string; workspace_revision: number; draft_revision: number; mode?: WorkspaceTransferMode }): Promise<WorkspaceTransfer> => http.post(path(cid), request).then(r => r.data)
export const listWorkspaceTransfers = (cid: string): Promise<{ transfers: WorkspaceTransfer[] }> => http.get(path(cid)).then(r => r.data)
export const decideWorkspaceTransfer = (row: WorkspaceTransfer, action: 'apply' | 'rollback' | 'cancel'): Promise<WorkspaceTransfer> => http.post(`${path(row.conversation_id)}/${row.id}/${action}`).then(r => r.data)
export const workspaceTransferBackup = (row: WorkspaceTransfer) => `/api${path(row.conversation_id)}/${row.id}/backup`
export const transferStatus = { prepared: '待确认', applying: '正在迁移', interrupted: '需要核对', rolling_back: '正在撤回', applied: '已迁移', rolled_back: '已撤回', cancelled: '已取消' }
