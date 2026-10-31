import axios from 'axios'
const http = axios.create({ baseURL: '/api', timeout: 15000 })
export interface TerminalSession {
  id: string; project_id: string; shell: 'powershell' | 'cmd'; path: string; cwd: string; pid: number
  status: 'running' | 'stopped' | 'exited'; exit_code: number | null; error: string
  rows: number; cols: number; created_at: number; connected: boolean
}
export interface TerminalRequest { request_id: string; shell: 'powershell' | 'cmd'; path: string; cols: number; rows: number }
export const terminalBase = (project: string) => `/projects/${encodeURIComponent(project)}/terminals`
export const listTerminals = (project: string): Promise<TerminalSession[]> => http.get(terminalBase(project)).then(r => r.data.sessions)
export const createTerminal = (project: string, request: TerminalRequest): Promise<TerminalSession> => http.post(terminalBase(project), request).then(r => r.data)
export const terminalToken = (project: string, id: string): Promise<string> => http.post(`${terminalBase(project)}/${id}/connect`).then(r => r.data.token)
export const stopTerminal = (project: string, id: string) => http.post(`${terminalBase(project)}/${id}/stop`)
export const removeTerminal = (project: string, id: string) => http.delete(`${terminalBase(project)}/${id}`)
