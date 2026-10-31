import axios from 'axios'

export interface Paper {
  id: string
  title: string
  authors: string
  abstract?: string
  page_count: number
  status: string
  error: string
  created_at: string
  duplicate?: boolean
}

export interface Evidence {
  id: string
  chunk_id: string
  paper_id: string
  paper_title: string
  page: number
  bbox: number[] | null
  quote: string
  score: number
}

export interface AnswerEvent {
  kind: string
  answer: string
  evidence: Evidence[]
  evidence_ids: string[]
  gate: { passed: boolean; total_claims: number; marked_claims: number; unsupported: string[] }
  refusal: number
  latency_ms: number
}

export type AgentEvent = { kind: string; time?: string; text?: string; [key: string]: any }

export type ExecutionMode = 'execute' | 'plan'
export interface ExecutionOptions {
  workspace_revision?: number
  project_id?: string | null
  file_paths?: string[]
  execution_mode?: ExecutionMode
  reviewed_plan_id?: string | null
  retry_plan_run_id?: string | null
}

// ---- 待办步骤 / 审批 ----
export interface TodoStep {
  title: string
  phase: string
  status: 'pending' | 'in_progress' | 'done'
}
export interface ApprovalRequest {
  approval_id: string
  tool_name: string
  arguments: Record<string, any>
  risk: string
  reason: string
}

export interface SubagentResult {
  topic: string
  findings: string
  evidence: any[]
}
export const decideApproval = (
  approvalId: string,
  decision: 'approve' | 'deny',
  scope: 'once' | 'session',
) => http.post('/approvals/decide', { approval_id: approvalId, decision, scope }).then((r) => r.data)

const http = axios.create({ baseURL: '/api' })

export const getHealth = () => http.get('/health').then((r) => r.data)
export interface ReviewIssue {
  severity: string
  dimension: string
  title: string
  detail: string
}
export interface ReviewResult {
  overall: string
  issues: ReviewIssue[]
  strengths: string[]
  verdict: string
  mode: string
  stats?: Record<string, any>
}
export const reviewDraft = (draft: string): Promise<ReviewResult> =>
  http.post('/review', { draft }).then((r) => r.data)

export interface FinalCheckItem {
  check: string
  item: string
}
export interface FinalCheckResult {
  passed: boolean
  summary: string
  checks: { name: string; passed: boolean; detail: string }[]
  must_fix: FinalCheckItem[]
}
export const finalCheck = (answer: string, evidence: any[]): Promise<FinalCheckResult> =>
  http.post('/final-check', { answer, evidence }).then((r) => r.data)

export interface BackgroundTask {
  id: string
  type: string
  execution_mode?: ExecutionMode
  reviewed_plan_id?: string | null
  title: string
  status: string
  executing?: boolean
  progress: number
  plan: any[] | null
  events: any[] | null
  approvals?: ApprovalRequest[]
  recovery?: {
    available: boolean
    phase?: string
    step?: number
    completed_tools?: number
    reason?: string
    pending_operation?: { kind: 'tool' | 'hook'; name: string; arguments: Record<string, unknown> } | null
  }
  result: { answer: string; evidence: any[] } | null
  error: string
  conversation_id: string | null
  updated_at: string
}
export const startBackgroundTask = (
  question: string,
  paperId: string | null,
  skills: string[],
  conversationId: string | null,
  options: ExecutionOptions = {},
): Promise<BackgroundTask> =>
  http.post('/tasks/start', {
    question, paper_id: paperId, skills, conversation_id: conversationId, ...options,
  }).then((r) => r.data)
export interface TaskSnapshot {
  tasks: BackgroundTask[]
  conversations?: Conversation[]
  conversation_epoch?: string
  next_cursor?: string | null
  history_total?: number
  history_epoch?: string
}
export const listBackgroundTasks = (): Promise<TaskSnapshot> =>
  http.get('/tasks', { params: { conversation_mode: 'signal' } }).then((r) => r.data)
export const listTaskHistory = (cursor: string): Promise<TaskSnapshot> =>
  http.get('/tasks/history', { params: { cursor } }).then((r) => r.data)
export const getBackgroundTask = (id: string): Promise<BackgroundTask> =>
  http.get(`/tasks/${id}`).then((r) => r.data)
export const resumeBackgroundTask = (id: string, resolution?: 'retry' | 'skip'): Promise<BackgroundTask> =>
  http.post(`/tasks/${id}/resume`, resolution ? { resolution } : {}).then((r) => r.data)
export const restartBackgroundTask = (id: string): Promise<BackgroundTask> =>
  http.post(`/tasks/${id}/restart`).then((r) => r.data)
export const cancelBackgroundTask = (id: string): Promise<BackgroundTask> =>
  http.post(`/tasks/${id}/cancel`).then((r) => r.data)

export interface DiagnosticCheck {
  id: string
  label: string
  status: string
  detail: string
}
export interface DiagnosticReport {
  overall: string
  summary: { ok: number; warn: number; error: number }
  checks: DiagnosticCheck[]
  generated_at: string
  data_dir: string
}
export const getDebugConfig = (): Promise<DiagnosticReport> =>
  http.get('/debug/config').then((r) => r.data)

export async function exportAs(
  question: string,
  answer: string,
  evidence: any[],
  format: 'md' | 'docx' | 'bibtex' | 'ris',
): Promise<void> {
  const resp = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, answer, evidence, format }),
  })
  if (!resp.ok) throw new Error('导出失败')
  const blob = await resp.blob()
  const disposition = resp.headers.get('Content-Disposition') || ''
  const m = disposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i)
  const ext = format === 'bibtex' ? 'bib' : format === 'docx' ? 'docx' : format === 'ris' ? 'ris' : 'md'
  const name = m ? decodeURIComponent(m[1]) : `ScholarFlow.${ext}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}
export const listPapers = (): Promise<Paper[]> => http.get('/papers').then((r) => r.data)
export const importPaperFromUrl = (pdf_url: string, title: string) =>
  http.post('/papers/import-from-url', { pdf_url, title }).then((r) => r.data)
export const getPaper = (id: string): Promise<Paper> => http.get(`/papers/${id}`).then((r) => r.data)
export const getPage = (id: string, page: number) =>
  http.get(`/papers/${id}/pages/${page}`).then((r) => r.data)
export const getGraph = () => http.get('/graph').then((r) => r.data)
export const paperFileUrl = (id: string) => `/api/papers/${id}/file`

export interface SampleItem {
  name: string
  title: string
  imported: boolean
}
export const listSamples = (): Promise<SampleItem[]> =>
  http.get('/samples').then((r) => r.data)
export const importSamples = (): Promise<Paper[]> =>
  http.post('/samples/import').then((r) => r.data)

export interface ModelSettings {
  llm_api_key: string
  llm_api_key_set?: boolean
  llm_base_url: string
  llm_model: string
  embedding_api_key: string
  embedding_base_url: string
  embedding_model: string
  approval_mode?: string
  work_rules?: string
  reasoning_effort?: string
  llm_mode?: string
  embedding_mode?: string
}
export const getSettings = (): Promise<ModelSettings> =>
  http.get('/settings').then((r) => r.data)
export const saveSettings = (payload: Partial<ModelSettings>): Promise<ModelSettings> =>
  http.put('/settings', payload).then((r) => r.data)
export const testConnection = (payload: {
  llm_base_url: string
  llm_api_key: string
  llm_model: string
}): Promise<{ ok: boolean; message: string }> =>
  http.post('/settings/test', payload).then((r) => r.data)

// ---- 分层 AGENTS.md 工作规范 ----
export interface AgentsMdMeta {
  global_rules: string
  project_rules: string
  merged: string
  global_exists: boolean
  global_path: string
  max_bytes: number
  global_bytes: number
  project_bytes: number
}
export const getAgentsMd = (): Promise<AgentsMdMeta> =>
  http.get('/agents-md').then((r) => r.data)
export const saveGlobalAgentsMd = (global_rules: string): Promise<AgentsMdMeta> =>
  http.put('/agents-md', { global_rules }).then((r) => r.data)
export const initAgentsMd = (
  overwrite: boolean,
): Promise<{ created: boolean } & AgentsMdMeta> =>
  http.post('/agents-md/init', { overwrite }).then((r) => r.data)

// ---- 持久目标 /goal ----
export interface Goal {
  id: string
  description: string
  status: 'active' | 'paused' | 'done' | 'cancelled'
  note?: string
  created_at: string
  updated_at: string
  completed_at?: string | null
}
export interface GoalPayload {
  current: Goal | null
  goals: Goal[]
}
export const getGoal = (): Promise<GoalPayload> =>
  http.get('/goal').then((r) => r.data)
export const setGoal = (description: string): Promise<GoalPayload> =>
  http.post('/goal', { description }).then((r) => r.data)
export const pauseGoal = (): Promise<GoalPayload> =>
  http.post('/goal/pause').then((r) => r.data)
export const resumeGoal = (): Promise<GoalPayload> =>
  http.post('/goal/resume').then((r) => r.data)
export const completeGoal = (): Promise<GoalPayload> =>
  http.post('/goal/complete').then((r) => r.data)
export const cancelGoal = (): Promise<GoalPayload> =>
  http.post('/goal/cancel').then((r) => r.data)

export async function uploadPaper(file: File): Promise<Paper> {
  const fd = new FormData()
  fd.append('file', file)
  const r = await http.post('/papers/upload', fd)
  return r.data
}

// ---- MCP 服务器管理 ----
export interface McpServer {
  name: string
  transport: string
  enabled: boolean
  builtin: boolean
  status: string
  error: string
  tool_count: number
  url: string
  command: string
}
export interface McpServerConfig {
  name: string
  transport: string
  enabled: boolean
  command: string
  args: string[]
  env: Record<string, string>
  cwd: string
  url: string
  headers?: Record<string, string>
  bearer_token?: string
  oauth?: { client_id: string; client_secret: string; token_url: string; scope: string }
}
export const listMcpServers = (): Promise<McpServer[]> =>
  http.get('/mcp/servers').then((r) => r.data.servers)
export const upsertMcpServer = (cfg: McpServerConfig): Promise<any> =>
  http.put('/mcp/servers', cfg).then((r) => r.data)
export const connectMcpServer = (name: string) =>
  http.post('/mcp/servers/connect', { name }).then((r) => r.data)
export const disconnectMcpServer = (name: string) =>
  http.post('/mcp/servers/disconnect', { name }).then((r) => r.data)
export const setMcpEnabled = (name: string, enabled: boolean) =>
  http.post('/mcp/servers/enabled', { name, enabled }).then((r) => r.data)
export const removeMcpServer = (name: string) =>
  http.delete(`/mcp/servers/${encodeURIComponent(name)}`).then((r) => r.data)

// ---- MCP 工具级管控 ----
export interface McpDiscoveredTool {
  name: string
  description: string
  server: string
}
export interface McpToolPolicy {
  enabled_tools: string[]
  disabled_tools: string[]
  tool_rules: Record<string, { approval?: string; timeout?: number }>
}
export const listDiscoveredTools = (): Promise<{ tools: McpDiscoveredTool[] }> =>
  http.get('/mcp/discovered-tools').then((r) => r.data)
export const getMcpPolicy = (): Promise<{ policy: McpToolPolicy }> =>
  http.get('/mcp/policy').then((r) => r.data)
export const saveMcpPolicy = (policy: McpToolPolicy): Promise<{ policy: McpToolPolicy }> =>
  http.put('/mcp/policy', { policy }).then((r) => r.data)

// ---- 技能库 ----
export interface Skill {
  name: string
  title: string
  description: string
  tier?: string
  disabled?: boolean
}
export const listSkills = (): Promise<Skill[]> =>
  http.get('/skills').then((r) => r.data.skills)
export const getSkill = (name: string): Promise<Skill & { content: string }> =>
  http.get(`/skills/${encodeURIComponent(name)}`).then((r) => r.data)
export const setSkillDisabled = (name: string, disabled: boolean) =>
  http.post('/skills/disabled', { name, disabled }).then((r) => r.data)
export const createSkill = (payload: {
  name: string
  title: string
  description: string
  content: string
}): Promise<Skill> => http.post('/skills', payload).then((r) => r.data)
export const deleteSkill = (name: string) =>
  http.delete(`/skills/${encodeURIComponent(name)}`).then((r) => r.data)

// ---- 生命周期钩子 ----
export interface HookResult {
  command: string
  exit_code: number | null
  stdout: string
  stderr: string
  timed_out: boolean
  blocking: boolean
}
export interface HookRun {
  event: string
  ran: number
  results: HookResult[]
  blocked: boolean
  feedback: string
}
export const getHookEvents = (): Promise<{ events: string[]; decision_events: string[] }> =>
  http.get('/hooks/events').then((r) => r.data)
export const getHooks = (): Promise<{ config: Record<string, any[]> }> =>
  http.get('/hooks').then((r) => r.data)
export const saveHooks = (config: Record<string, any[]>) =>
  http.put('/hooks', { config }).then((r) => r.data)
export const testHook = (
  event: string,
  payload?: Record<string, any>,
  matcherValue?: string,
): Promise<HookRun> =>
  http.post('/hooks/test', { event, payload, matcher_value: matcherValue }).then((r) => r.data)

// ---- 会话历史 ----
export interface Conversation {
  workspace_revision?: number
  project_id?: string | null
  match_field?: 'title' | 'id' | 'question' | 'answer' | 'draft' | 'interrupted' | null
  match_text?: string | null
  match_record_id?: string | null
  match_run_id?: string | null
  archived_at?: string | null
  archive_revision?: number
  draft_only?: boolean
  interrupted_only?: boolean
  pinned_at?: string | null
  pin_position?: number | null
  unread_count?: number
  running_count?: number
  finishing_count?: number
  approval_count?: number
  id: string
  title: string
  message_count: number
  created_at: string
  updated_at: string
}
export interface ConversationMessage {
  workspace_revision?: number
  project_id?: string | null
  file_paths?: string[]
  id: string
  question: string
  paper_id?: string | null
  answer: string
  execution_mode?: ExecutionMode
  reviewed_plan_id?: string | null
  plan_run_id?: string | null
  plan_steps?: TodoStep[]
  plan_images?: ChatImage[]
  evidence: Evidence[]
  claims: any[]
  trace: any[]
  skills: string[]
  refusal: number
  latency_ms: number
  usage?: TokenUsage | null
  created_at: string
}
export const listConversations = (scope: 'active' | 'archived' | 'all' = 'active'): Promise<{ conversations: Conversation[] }> =>
  http.get('/conversations', { params: scope === 'active' ? undefined : { scope } }).then((r) => r.data)
export interface ConversationPage {
  conversations: Conversation[]
  next_cursor: string | null
  total: number
  pinned_total: number
}
export type ConversationScope = 'active' | 'archived' | 'all'
export type ConversationSort = 'updated_at' | 'created_at' | 'sidebar_updated_at' | 'sidebar_created_at'
export const listConversationPage = (options: { scope: ConversationScope; q?: string; sort?: ConversationSort; cursor?: string | null }): Promise<ConversationPage> =>
  http.get('/conversations/page', { params: options }).then((r) => r.data)
export const lookupConversations = (ids: string[], scope: ConversationScope = 'all', q = ''): Promise<ConversationPage> =>
  http.post('/conversations/lookup', { ids, scope, q }).then((r) => r.data)
export const getConversationSummary = (id: string): Promise<Conversation> =>
  http.get(`/conversations/${encodeURIComponent(id)}/activity`).then((r) => r.data)
export interface ConversationArchiveState {
  id: string
  archived_at: string | null
  archive_revision: number
}
export const setConversationArchived = (id: string, archived: boolean, expectedRevision: number): Promise<ConversationArchiveState> =>
  http.put(`/conversations/${encodeURIComponent(id)}/archive`, { archived, expected_revision: expectedRevision }).then((r) => r.data)
export const getConversation = (id: string): Promise<{ id: string; title: string; messages: ConversationMessage[]; archived_at?: string | null; archive_revision?: number }> =>
  http.get(`/conversations/${encodeURIComponent(id)}`).then((r) => r.data)
export const markConversationRead = (id: string, messageIds: string[]): Promise<{ message_ids: string[] }> =>
  http.post(`/conversations/${encodeURIComponent(id)}/read`, { message_ids: messageIds }).then((r) => r.data)
export const createConversation = (title?: string, projectId?: string): Promise<{ id: string; title: string }> =>
  http.post('/conversations', { title: title || '新会话', project_id: projectId }).then((r) => r.data)
export const renameConversation = (id: string, title: string) =>
  http.put(`/conversations/${encodeURIComponent(id)}`, { title }).then((r) => r.data)
export const pinConversation = (id: string, pinned: boolean): Promise<{ id: string; pinned_at: string | null; pin_position?: number | null }> =>
  http.put(`/conversations/${encodeURIComponent(id)}/pin`, { pinned }).then((r) => r.data)
export const reorderConversationPins = (ids: string[], expectedOrder: string[]): Promise<{ conversation_ids: string[] }> =>
  http.put('/conversations/pins/order', { conversation_ids: ids, expected_order: expectedOrder }).then((r) => r.data)
export const deleteConversation = (id: string) =>
  http.delete(`/conversations/${encodeURIComponent(id)}`).then((r) => r.data)
export const forkConversation = (
  id: string,
  afterMessageId?: string | null,
  title?: string,
): Promise<{ id: string; title: string; message_count: number }> =>
  http.post(`/conversations/${encodeURIComponent(id)}/fork`, {
    after_message_id: afterMessageId || null,
    title: title || null,
  }).then((r) => r.data)

export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}
export const getConversationUsage = (
  id: string,
): Promise<{ conversation_id: string; turns: number; usage: TokenUsage }> =>
  http.get(`/conversations/${encodeURIComponent(id)}/usage`).then((r) => r.data)
export interface GlobalUsage {
  turns: number
  conversations: number
  usage: TokenUsage
  compaction?: CompactionUsage
}
export const getGlobalUsage = (): Promise<GlobalUsage> =>
  http.get('/conversations/usage/summary').then((r) => r.data)

export interface CompactionUsage {
  calls: number
  unknown_calls: number
  provider_calls: number
  estimated_calls: number
  usage: TokenUsage
}
export interface ConversationMemory {
  conversation_id: string
  revision: number
  summary: string
  mode: 'model' | 'extractive' | 'degraded' | 'mixed' | null
  stale: boolean
  total_turns: number
  covered_turns: number
  pending_turns: number
  recent_turns: number
  sources: { id: string; question: string; evidence_count: number }[]
  updated_at: string | null
  recall_enabled: boolean
  usage: CompactionUsage
  busy: boolean
}
export const getConversationMemory = (id: string): Promise<ConversationMemory> =>
  http.get(`/conversations/${encodeURIComponent(id)}/memory`).then((r) => r.data)
export const compactConversation = (id: string, expectedRevision: number, force = false): Promise<ConversationMemory> =>
  http.post(`/conversations/${encodeURIComponent(id)}/memory/compact`, { expected_revision: expectedRevision, force }).then((r) => r.data)
export const cancelCompaction = (id: string) =>
  http.post(`/conversations/${encodeURIComponent(id)}/memory/cancel`, {}).then((r) => r.data)

/**
 * SSE 流式问答。
 * onEvent 收到解析后的事件；signal 用于取消（AbortController）。
 */
export interface ChatImage {
  media_type: string
  data: string
}

export async function cancelChatRun(runId: string): Promise<{ run_id: string; status: string }> {
  const response = await fetch(`/api/chat/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST', keepalive: true,
  })
  if (!response.ok) throw new Error(`停止请求失败：${response.status}`)
  return response.json()
}

export async function streamChat(
  question: string,
  paperId: string | null,
  onEvent: (ev: AgentEvent) => void,
  signal?: AbortSignal,
  skills?: string[],
  conversationId?: string | null,
  images?: ChatImage[],
  runId?: string,
  options: ExecutionOptions = {},
): Promise<void> {
  const resp = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      question,
      paper_id: paperId,
      skills: skills && skills.length ? skills : null,
      conversation_id: conversationId || null,
      images: images && images.length ? images : null,
      run_id: runId || null,
      ...options,
    }),
    signal,
  })
  if (!resp.ok || !resp.body) {
    const error = await resp.json().catch(() => ({}))
    throw new Error(typeof error.detail === 'string' ? error.detail : `问答请求失败：${resp.status}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() || ''
    for (const frame of frames) {
      const dataLine = frame.split('\n').find((l) => l.startsWith('data: '))
      if (dataLine) {
        try {
          onEvent(JSON.parse(dataLine.slice(6)))
        } catch {
          // 忽略半帧
        }
      }
    }
  }
}

export interface ForegroundRunCheckpoint {
  workspace_revision?: number
  project_id?: string | null
  file_paths?: string[]
  id: string
  conversation_id: string
  revision: number
  question: string
  execution_mode?: ExecutionMode
  reviewed_plan_id?: string | null
  paper_id: string | null
  skills: string[]
  images: ChatImage[]
  partial_answer: string
  phase: string
  status: 'running' | 'interrupted' | 'cancelled' | 'failed' | 'completed' | 'dismissed'
  error: string
  created_at: string
  updated_at: string
  approvals?: ApprovalRequest[]
}

export const createForegroundRun = (run: Pick<ForegroundRunCheckpoint,
  'id' | 'conversation_id' | 'question' | 'paper_id' | 'skills' | 'images'> & ExecutionOptions) =>
  http.post('/foreground-runs', run).then((response) => response.data)

export const patchForegroundRun = (id: string, revision: number, patch: {
  partial_answer?: string; phase?: string; status?: 'running' | 'interrupted' | 'cancelled' | 'failed'; error?: string
}, keepalive = false): Promise<ForegroundRunCheckpoint> => {
  const body = JSON.stringify({ revision, ...patch })
  return fetch(`/api/foreground-runs/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body,
    keepalive: keepalive && new Blob([body]).size < 60_000,
  }).then(async (response) => {
    const data = await response.json()
    if (!response.ok) throw Object.assign(new Error(data.detail?.message || data.detail || '前台检查点保存失败'), { status: response.status, data })
    return data
  })
}

export const listForegroundRuns = (conversationId: string): Promise<{ runs: ForegroundRunCheckpoint[] }> =>
  http.get('/foreground-runs', { params: { conversation_id: conversationId } }).then((response) => response.data)

export const dismissForegroundRun = (id: string) =>
  http.delete(`/foreground-runs/${encodeURIComponent(id)}`).then((response) => response.data)
