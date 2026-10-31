import axios from 'axios'
const http = axios.create({ baseURL: '/api' })
export interface SwitchScope { project_id: string; name: string; root_path: string; branch: string | null; head: string; changes: number; staged: number; unstaged: number }
export interface WorkspaceSwitch {
  id: string; conversation_id: string; source_project_id: string; target_project_id: string; workspace_revision: number
  status: 'prepared' | 'applied' | 'cancelled' | 'superseded'; created_at: string
  snapshot: { source: SwitchScope; target: SwitchScope; draft_revision: number; file_paths: string[]; editor: { project_id: string; path: string } | null }
}
const path = (cid: string) => `/conversations/${encodeURIComponent(cid)}/workspace-switches`
export const prepareWorkspaceSwitch = (cid: string, request: { request_id: string; target_project_id: string; workspace_revision: number; draft_revision: number }): Promise<WorkspaceSwitch> => http.post(path(cid), request).then(r => r.data)
export const listWorkspaceSwitches = (cid: string): Promise<{ switches: WorkspaceSwitch[] }> => http.get(path(cid)).then(r => r.data)
export const decideWorkspaceSwitch = (cid: string, id: string, action: 'apply' | 'cancel'): Promise<WorkspaceSwitch> => http.post(`${path(cid)}/${id}/${action}`).then(r => r.data)
