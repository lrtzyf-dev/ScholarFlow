import axios from 'axios'
const http = axios.create({ baseURL: '/api' })

export interface ProjectCommand {
  id: string; project_id: string; conversation_id: string; run_id: string; command: string; cwd: string
  path: string; summary: string; timeout_seconds: number; status: string; exit_code: number | null
  pid: number | null; error: string; revision: number; attempt: number; previous_id: string | null
  output_limited: boolean; total_chars: number; created_at: string; started_at: string | null; finished_at: string | null
}
export interface CommandDetail extends ProjectCommand { output: string; output_offset: number; next_offset: number | null }
export const commandStatus = (status: string) => ({ proposed: '待审批', running: '运行中', succeeded: '已完成', failed: '失败', cancelled: '已停止', timed_out: '超时', output_limit: '输出超限', interrupted: '结果未确认', rejected: '已拒绝', conflict: '未启动' }[status] || status)
export const commandActive = (status: string) => ['proposed', 'running'].includes(status)
export const listCommands = (id: string, offset = 0): Promise<{ commands: ProjectCommand[]; next_offset: number | null; total: number }> => http.get(`/projects/${id}/commands`, { params: { offset } }).then(r => r.data)
export const getCommand = (id: string, tail = true, offset = 0): Promise<CommandDetail> => http.get(`/project-commands/${id}`, { params: { tail, offset } }).then(r => r.data)
export const stopCommand = (id: string): Promise<ProjectCommand> => http.post(`/project-commands/${id}/stop`).then(r => r.data)

export function commandEvents(events: Record<string, any>[]) {
  const commands = new Map<string, Record<string, any>>()
  for (const event of events) if (event.kind === 'project_command' && event.command_id) {
    commands.set(event.command_id, { ...commands.get(event.command_id), ...event })
  }
  return [...commands.values()]
}
