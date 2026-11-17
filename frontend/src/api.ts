import axios from 'axios'
import { readChatEventStream, type ChatRunSnapshot, type ResourcePause } from './chatRunObserver'

export interface Paper {
  source_revision_id?: string | null
  source_conversation_id?: string | null
  source_media_type?: string | null
  document_format?: string | null
  id: string
  title: string
  authors: string
  abstract?: string
  page_count: number
  status: string
  error: string
  created_at: string
  duplicate?: boolean
  version_duplicate?: boolean
  existing_id?: string | null
  incoming_version?: number | null
  existing_version?: number | null
  newer?: boolean
  recommend?: 'incoming' | 'existing' | null
}

export interface Evidence {
  source_id?: string
  revision_id?: string
  locator_id?: string
  source_title?: string
  source_kind?: string
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

export type RunInputKind = 'steer' | 'next_turn'
export interface RunInput {
  source_revision_ids?: string[]
  input_id: string
  run_id: string
  conversation_id: string
  kind: RunInputKind
  content: string
  workspace_revision: number
  revision: number
  status: 'queued' | 'applied' | 'cancelled' | 'rejected' | 'dispatched'
  goal_revision: number | null
  attempt_id: string | null
  reason: string
  child_run_id?: string | null
  runtime_config?: RuntimeConfiguration | null
  created_at: string
  updated_at: string
}
export interface RunInputQueue {
  run_id: string
  conversation_id: string
  revision: number
  inputs: RunInput[]
  next_turn_dispatch: 'manual' | 'automatic'
}

export const listRunInputs = (runId: string, conversationId: string): Promise<RunInputQueue> =>
  http
    .get(`/chat/runs/${encodeURIComponent(runId)}/inputs`, {
      params: { conversation_id: conversationId },
    })
    .then((response) => response.data)

export const listConversationRunInputs = (
  conversationId: string
): Promise<{ conversation_id: string; queues: RunInputQueue[] }> =>
  http
    .get('/chat/runs/inputs', { params: { conversation_id: conversationId } })
    .then((response) => response.data)

export const submitRunInput = (
  runId: string,
  input: {
    input_id: string
    conversation_id: string
    kind: RunInputKind
    content: string
    expected_revision: number
    workspace_revision: number
    source_revision_ids?: string[]
  }
): Promise<RunInputQueue & { input: RunInput }> =>
  http
    .post(`/chat/runs/${encodeURIComponent(runId)}/inputs`, input)
    .then((response) => response.data)

export const cancelRunInput = (
  runId: string,
  inputId: string,
  conversationId: string,
  expectedRevision: number
): Promise<RunInputQueue & { input: RunInput }> =>
  http
    .delete(`/chat/runs/${encodeURIComponent(runId)}/inputs/${encodeURIComponent(inputId)}`, {
      params: { conversation_id: conversationId, expected_revision: expectedRevision },
    })
    .then((response) => response.data)

export type ExecutionMode = 'execute' | 'plan'
export interface ExecutionOptions {
  source_revision_ids?: string[]
  workspace_revision?: number
  project_id?: string | null
  file_paths?: string[]
  execution_mode?: ExecutionMode
  reviewed_plan_id?: string | null
  retry_plan_run_id?: string | null
}

export interface ChatResumeOptions {
  resume_checkpoint?: boolean
  additional_steps?: number
  resume_request_id?: string
  expected_run_revision?: number
}

// ---- 待办步骤 / 审批 ----
export interface TodoStep {
  title: string
  phase: string
  status: 'pending' | 'in_progress' | 'done'
  acceptance?: string
  evidence?: Record<string, unknown>[]
  tool_name?: string
  tool_arguments?: Record<string, unknown>
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
  scope: 'once' | 'session'
) =>
  http.post('/approvals/decide', { approval_id: approvalId, decision, scope }).then((r) => r.data)

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
  passed: boolean | null
  status: 'passed' | 'failed' | 'unverified'
  summary: string
  checks: {
    name: string
    passed: boolean
    status: 'passed' | 'failed' | 'unverified'
    detail: string
  }[]
  must_fix: FinalCheckItem[]
}
export const finalCheck = (answer: string, evidence: any[]): Promise<FinalCheckResult> =>
  http.post('/final-check', { answer, evidence }).then((r) => r.data)

export interface BackgroundTask {
  run_revision?: number | null
  attempt_id?: string | null
  goal_id?: string | null
  runtime_config?: RuntimeConfiguration | null
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
    pending_operation?: {
      kind: 'tool' | 'hook'
      name: string
      arguments: Record<string, unknown>
    } | null
  }
  result: {
    answer: string
    evidence: any[]
    completed?: boolean
    outcome?: 'planned' | 'completed' | 'partial' | 'blocked'
    incomplete_steps?: { title: string; phase: string; status: string }[]
  } | null
  error: string
  conversation_id: string | null
  updated_at: string
}
export const startBackgroundTask = (
  question: string,
  paperId: string | null,
  skills: string[],
  conversationId: string | null,
  options: ExecutionOptions = {}
): Promise<BackgroundTask> =>
  http
    .post('/tasks/start', {
      question,
      paper_id: paperId,
      skills,
      conversation_id: conversationId,
      ...options,
    })
    .then((r) => r.data)
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
export const resumeBackgroundTask = (
  id: string,
  resolution?: 'retry' | 'skip',
  admission?: { resume_request_id: string; expected_run_revision: number }
): Promise<BackgroundTask> =>
  http.post(`/tasks/${id}/resume`, { ...(resolution ? { resolution } : {}), ...admission }).then((r) => r.data)
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
  format: 'md' | 'docx' | 'bibtex' | 'ris'
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
  const ext =
    format === 'bibtex' ? 'bib' : format === 'docx' ? 'docx' : format === 'ris' ? 'ris' : 'md'
  const name = m ? decodeURIComponent(m[1]) : `ScholarFlow.${ext}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.style.display = 'none'
  document.body.appendChild(a)
  try {
    a.click()
  } finally {
    a.remove()
    // Native download handlers may consume the blob after the click returns.
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }
}
export interface ImportResult {
  paper_id: string
  existing_id?: string
  title: string
  status: string
  error: string
  duplicate: boolean
  version_duplicate?: boolean
  incoming_version?: number | null
  existing_version?: number | null
  newer?: boolean
  recommend?: 'incoming' | 'existing'
}

export const listPapers = (): Promise<Paper[]> => http.get('/papers').then((r) => r.data)
export const pagePapers = (
  options: { q?: string; status?: string; sort?: string; offset?: number; limit?: number } = {},
  signal?: AbortSignal
): Promise<{
  items: Paper[]
  total: number
  offset: number
  limit: number
  next_offset: number | null
  statistics: { papers: number; pages: number; parsed: number; internal_references: number }
}> => http.get('/papers/page', { params: options, signal }).then((r) => r.data)
export const importPaperFromUrl = (pdf_url: string, title: string): Promise<ImportResult> =>
  http.post('/papers/import-from-url', { pdf_url, title }).then((r) => r.data)
export const getPaper = (id: string): Promise<Paper> =>
  http.get(`/papers/${id}`).then((r) => r.data)
export const getPage = (id: string, page: number) =>
  http.get(`/papers/${id}/pages/${page}`).then((r) => r.data)
export interface GraphNode {
  id: string
  title: string
  page_count: number
  pagerank: number
  in_centrality: number
  out_centrality: number
  member_ids: string[]
  version_count: number
  published_at?: string | null
}
export interface GraphEdge {
  source: string
  target: string
  via: string
  match_type: string
}
export interface GraphRankingItem {
  id: string
  title: string
  pagerank: number
}
export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  ranking: GraphRankingItem[]
}
export const getGraph = (paperIds?: string[]): Promise<GraphData> =>
  http
    .get('/graph', { params: paperIds?.length ? { paper_ids: paperIds.join(',') } : {} })
    .then((r) => r.data)
export const reparsePaper = (id: string): Promise<Paper> =>
  http.post(`/papers/${id}/reparse`).then((r) => r.data)
export type DiagramFormat = 'mermaid' | 'dot'
export interface DiagramData {
  format: DiagramFormat
  script: string
}
export const getDiagram = (format: DiagramFormat): Promise<DiagramData> =>
  http.get('/graph/diagram', { params: { fmt: format } }).then((r) => r.data)

export interface CsvStats {
  count: number
  min: number
  max: number
  mean: number
}
export interface CsvParseResult {
  columns: string[]
  rows: string[][]
  numeric_columns: string[]
  stats: Record<string, CsvStats>
  row_count: number
  truncated: boolean
}
export interface CaptionResult {
  caption: string
  mode: 'template' | 'llm'
}
export const parseCsv = (content: string): Promise<CsvParseResult> =>
  http.post('/visualization/parse', { content }).then((r) => r.data)
export const generateCaption = (data: CsvParseResult, chartType: string): Promise<CaptionResult> =>
  http.post('/visualization/caption', { data, chart_type: chartType }).then((r) => r.data)

export interface GlossaryHit {
  source: string
  target: string
}
export interface TranslationSourceRef {
  revision_id: string
  locator_id: string
}
export interface TranslationEvidence extends TranslationSourceRef {
  id: string
  title: string
  version: number
  quote: string
  conversation_id: string
  raw_sha256: string
  fingerprint: string
  semantic_verification: 'not_assessed'
  verified: false
}
export interface TranslationProvenance {
  source_evidence?: TranslationEvidence[]
  provenance_status?: 'fixed_source' | 'user_text'
  semantic_verification?: 'not_assessed'
  model_selection?: 'global_default'
}
export interface TranslationOptions {
  conversation_id?: string
  source_ref?: TranslationSourceRef
}
export interface TranslateResult extends TranslationProvenance {
  translation: string | null
  mode: 'llm' | 'offline'
  glossary_hits: GlossaryHit[]
  message?: string
}
export const translateText = (
  text: string,
  options: TranslationOptions = {}
): Promise<TranslateResult> => http.post('/translate', { text, ...options }).then((r) => r.data)

export interface ParallelUnit {
  index: number
  source: string
  target: string | null
  preserve: boolean
  glossary: GlossaryHit[]
}
export interface ParallelResult extends TranslationProvenance {
  mode: 'llm' | 'offline'
  units: ParallelUnit[]
}
export const translateParallel = (
  text: string,
  options: TranslationOptions = {}
): Promise<ParallelResult> =>
  http.post('/translate-parallel', { text, ...options }).then((r) => r.data)

export type QualityLabel = 'substantive' | 'neutral' | 'passing' | 'unmentioned'
export interface CitationQualityItem {
  ref_index: number
  label: QualityLabel
  score: number
  occurrences: number
  signals: string[]
  raw_text: string
}
export interface CitationQualityResult {
  paper_id: string
  results: CitationQualityItem[]
  summary: Record<string, number>
}
export const getCitationQuality = (
  paperId: string,
  label?: QualityLabel
): Promise<CitationQualityResult> =>
  http.get(`/papers/${paperId}/citation-quality`, { params: { label } }).then((r) => r.data)

export interface FutureDirection {
  paper_id: string
  paper_title: string
  year: number | null
  quote: string
  page: number
  bbox: number[] | null
  status: 'open' | 'possibly_addressed'
  resolved_by: { paper_id: string; paper_title: string; year: number }[]
}
export interface FutureWorkResult {
  open: FutureDirection[]
  possibly_addressed: FutureDirection[]
  total: number
}
export const getFutureWork = (): Promise<FutureWorkResult> =>
  http.get('/papers/future-work/analysis').then((r) => r.data)

export interface CitationLocation {
  page: number
  bbox: number[] | null
}
export interface ReferenceItem {
  ref_index: number
  raw_text: string
  page: number | null
  bbox: number[] | null
}
export interface CitationData {
  references: ReferenceItem[]
  inline_citations: Record<string, CitationLocation[]>
}
export const getCitations = (id: string): Promise<CitationData> =>
  http.get(`/papers/${id}/citations`).then((r) => r.data)

export interface ComparisonEvidence {
  id: string
  chunk_id: string
  paper_id: string
  paper_title: string
  page: number
  bbox: number[] | null
  quote: string
  score: number
  source_id?: string
  revision_id?: string
  locator_id?: string
  source_title?: string
  source_kind?: string
  source_version?: number
  source_sha256?: string
  source_media_type?: string | null
  locator_kind?: string
  source_location?: {
    line_start?: number
    line_end?: number
    paragraph_start?: number
    paragraph_end?: number
    sheet?: string
    cell_range?: string
    [key: string]: unknown
  }
  location_status?: 'fixed_source' | 'pdf_page' | 'unavailable'
  verification_status?: 'unverified' | 'supported' | 'conflict'
}
export interface ComparisonPaper {
  id: string
  title: string
  authors: string
  year: string
  page_count: number
  content_sha256?: string
}
export interface ComparisonCell {
  evidence: ComparisonEvidence[]
  quote: string
  status?: 'unverified' | 'not_found'
}
export interface ComparisonRow {
  key: string
  label: string
  cells: Record<string, ComparisonCell>
}
export interface ComparisonData {
  papers: ComparisonPaper[]
  dimensions: { key: string; label: string }[]
  rows: ComparisonRow[]
  conversation_id?: string | null
  comparison_method?: 'retrieved_passages'
}
export const comparePapers = (
  paper_ids: string[],
  dimensions?: string[],
  conversation_id?: string
): Promise<ComparisonData> =>
  http.post('/papers/compare', { paper_ids, dimensions, conversation_id }).then((r) => r.data)
export const paperFileUrl = (id: string) => `/api/papers/${id}/file`

export interface SampleItem {
  name: string
  title: string
  imported: boolean
}
export const listSamples = (): Promise<SampleItem[]> => http.get('/samples').then((r) => r.data)
export const importSamples = (): Promise<Paper[]> =>
  http.post('/samples/import').then((r) => r.data)

export interface ModelSettings {
  credential_errors?: Record<string, string>
  llm_api_key: string
  llm_api_key_set?: boolean
  llm_base_url: string
  llm_model: string
  embedding_api_key: string
  embedding_api_key_set?: boolean
  embedding_base_url: string
  embedding_model: string
  approval_mode?: string
  work_rules?: string
  reasoning_effort?: string
  max_agent_steps?: number
  llm_mode?: string
  embedding_mode?: string
  desktop_notifications?: boolean
  memory_recall?: boolean
  memory_capture?: boolean
}
export const getSettings = (): Promise<ModelSettings> => http.get('/settings').then((r) => r.data)
export const saveSettings = (payload: Partial<ModelSettings>): Promise<ModelSettings> =>
  http.put('/settings', payload).then((r) => r.data)
export const testConnection = (payload: {
  llm_base_url: string
  llm_api_key?: string
  llm_model: string
  reasoning_effort?: string
}): Promise<{ ok: boolean; message: string }> =>
  http.post('/settings/test', payload).then((r) => r.data)

export interface ProviderModel {
  id: string
  owned_by?: string
}
export interface ModelDiscoveryResult {
  ok: boolean
  message: string
  models: ProviderModel[]
}
export const discoverModels = (payload: {
  llm_base_url: string
  llm_api_key?: string
}): Promise<ModelDiscoveryResult> => http.post('/settings/models', payload).then((r) => r.data)

export type ApprovalMode = 'always' | 'auto' | 'full_access'
export type ModelCapability = 'unknown' | 'supported' | 'unsupported'
export interface ModelProfile {
  id: string
  name: string
  base_url: string
  model_id: string
  reasoning_effort: string
  context_window_tokens?: number
  max_output_tokens?: number
  context_limits_source?: 'configured' | 'compatibility_default'
  context_limits_verified?: boolean
  enabled: boolean
  version: number
  api_key_set: boolean
  mode?: 'api' | 'mock' | 'unconfigured'
  configuration_error?: string
  builtin?: boolean
  capabilities: Record<'text' | 'streaming' | 'tools' | 'vision' | 'reasoning' | 'structured' | 'usage' | 'native_tools' | 'files', ModelCapability>
}
export interface RuntimeConfiguration {
  capabilities?: ModelProfile['capabilities']
  capability_notices?: string[]
  profile_id?: string | null
  profile_name?: string
  model_id?: string
  base_url?: string
  reasoning_effort?: string
  approval_mode?: ApprovalMode
  policy_revision?: number
  [key: string]: unknown
}
export interface ConversationOptions {
  revision: number
  model_profile_id: string | null
  approval_mode: ApprovalMode
  reasoning_effort: string | null
  effective_model: ModelProfile | null
  model_error?: string
}
export interface ModelProfileInput {
  name: string
  base_url: string
  model_id: string
  reasoning_effort?: string
  context_window_tokens?: number
  max_output_tokens?: number
  api_key?: string
  enabled?: boolean
}
export const listModelProfiles = (): Promise<{
  profiles: ModelProfile[]
  default_profile_id: string | null
}> => http.get('/model-profiles').then((r) => r.data)
export const createModelProfile = (input: ModelProfileInput): Promise<ModelProfile> =>
  http.post('/model-profiles', input).then((r) => r.data)
export const updateModelProfile = (
  id: string,
  input: Partial<ModelProfileInput> & { expected_version: number }
): Promise<ModelProfile> =>
  http.put(`/model-profiles/${encodeURIComponent(id)}`, input).then((r) => r.data)
export const deleteModelProfile = (id: string, expectedVersion: number): Promise<unknown> =>
  http
    .delete(`/model-profiles/${encodeURIComponent(id)}`, {
      params: { expected_version: expectedVersion },
    })
    .then((r) => r.data)
export const setDefaultModelProfile = (id: string | null): Promise<unknown> =>
  http.put('/model-profiles/default', { model_profile_id: id }).then((r) => r.data)
export const discoverProfileModels = (id: string): Promise<ModelDiscoveryResult> =>
  http.post(`/model-profiles/${encodeURIComponent(id)}/models`, {}).then((r) => r.data)
export const discoverNewProfileModels = (input: {
  base_url: string
  api_key: string
}): Promise<ModelDiscoveryResult> =>
  http.post('/model-profiles/discover', input).then((r) => r.data)
export const testModelProfile = (
  id: string
): Promise<{
  ok: boolean
  message: string
  capabilities: ModelProfile['capabilities']
}> => http.post(`/model-profiles/${encodeURIComponent(id)}/test`, {}).then((r) => r.data)
export const getConversationOptions = (id: string): Promise<ConversationOptions> =>
  http.get(`/conversation-options/${encodeURIComponent(id)}`).then((r) => r.data)
export const saveConversationOptions = (
  id: string,
  input: {
    expected_revision: number
    model_profile_id?: string | null
    approval_mode?: ApprovalMode
    reasoning_effort?: string | null
  }
): Promise<ConversationOptions> =>
  http.put(`/conversation-options/${encodeURIComponent(id)}`, input).then((r) => r.data)

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
export const getAgentsMd = (): Promise<AgentsMdMeta> => http.get('/agents-md').then((r) => r.data)
export const saveGlobalAgentsMd = (global_rules: string): Promise<AgentsMdMeta> =>
  http.put('/agents-md', { global_rules }).then((r) => r.data)
export const initAgentsMd = (overwrite: boolean): Promise<{ created: boolean } & AgentsMdMeta> =>
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
export const getGoal = (): Promise<GoalPayload> => http.get('/goal').then((r) => r.data)
export const setGoal = (description: string): Promise<GoalPayload> =>
  http.post('/goal', { description }).then((r) => r.data)
export const pauseGoal = (): Promise<GoalPayload> => http.post('/goal/pause').then((r) => r.data)
export const resumeGoal = (): Promise<GoalPayload> => http.post('/goal/resume').then((r) => r.data)
export const completeGoal = (): Promise<GoalPayload> =>
  http.post('/goal/complete').then((r) => r.data)
export const cancelGoal = (): Promise<GoalPayload> => http.post('/goal/cancel').then((r) => r.data)

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
  auth_mode?: string
  user_oauth?: McpUserAuthorization
}
export interface McpUserAuthorization {
  state: string
  message?: string
  authorization_url?: string
  expires_at?: number
  deadline?: number
  refreshing?: boolean
}
export const mcpAuthorization = (name: string): Promise<McpUserAuthorization> =>
  http.get(`/mcp/servers/${encodeURIComponent(name)}/authorization`).then((r) => r.data)
export const mcpAuthorizationAction = (
  name: string,
  action: string
): Promise<McpUserAuthorization & { message?: string }> =>
  http.post(`/mcp/servers/${encodeURIComponent(name)}/authorization/${action}`).then((r) => r.data)
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
  oauth?: Record<string, string>
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
  trusted?: boolean
  revision?: string
  project_id?: string | null
  overrides?: { tier: string; revision: string }[]
}
export const listSkills = (projectId?: string | null): Promise<Skill[]> =>
  http.get('/skills', { params: { project_id: projectId || undefined } }).then((r) => r.data.skills)
export const listLegacySkills = (): Promise<Skill[]> =>
  http.get('/skills').then((r) => r.data.legacy || [])
export const getSkill = (
  name: string,
  projectId?: string | null
): Promise<Skill & { content: string }> =>
  http
    .get(`/skills/${encodeURIComponent(name)}`, { params: { project_id: projectId || undefined } })
    .then((r) => r.data)
export const setSkillDisabled = (name: string, disabled: boolean, projectId?: string | null) =>
  http
    .post(
      '/skills/disabled',
      { name, disabled },
      { params: { project_id: projectId || undefined } }
    )
    .then((r) => r.data)
export const trustSkill = (name: string, revision: string, projectId?: string | null) =>
  http
    .post(
      `/skills/${encodeURIComponent(name)}/trust`,
      { revision },
      { params: { project_id: projectId || undefined } }
    )
    .then((r) => r.data)
export const migrateLegacySkill = (name: string, revision: string, projectId: string) =>
  http
    .post(
      `/skills/legacy/${encodeURIComponent(name)}/migrate`,
      { revision },
      { params: { project_id: projectId } }
    )
    .then((r) => r.data)
export const updateSkill = (
  name: string,
  payload: { expected_revision: string; title: string; description: string; content: string },
  projectId?: string | null
): Promise<Skill & { content: string }> =>
  http
    .put(`/skills/${encodeURIComponent(name)}`, payload, {
      params: { project_id: projectId || undefined },
    })
    .then((r) => r.data)
export const createSkill = (
  payload: {
    name: string
    title: string
    description: string
    content: string
  },
  projectId?: string | null
): Promise<Skill> =>
  http
    .post('/skills', payload, { params: { project_id: projectId || undefined } })
    .then((r) => r.data)
export const deleteSkill = (name: string, projectId?: string | null) =>
  http
    .delete(`/skills/${encodeURIComponent(name)}`, {
      params: { project_id: projectId || undefined },
    })
    .then((r) => r.data)

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
  matcherValue?: string
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
  goal_id?: string | null
  source_revision_ids?: string[]
  runtime_config?: RuntimeConfiguration | null
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
export const listConversations = (
  scope: 'active' | 'archived' | 'all' = 'active'
): Promise<{ conversations: Conversation[] }> =>
  http
    .get('/conversations', { params: scope === 'active' ? undefined : { scope } })
    .then((r) => r.data)
export interface ConversationPage {
  conversations: Conversation[]
  next_cursor: string | null
  total: number
  pinned_total: number
}
export type ConversationScope = 'active' | 'archived' | 'all'
export type ConversationProjectScope = 'all' | 'unassigned'
export type ConversationSort =
  'updated_at' | 'created_at' | 'sidebar_updated_at' | 'sidebar_created_at'
export const listConversationPage = (options: {
  scope: ConversationScope
  project_scope?: ConversationProjectScope
  q?: string
  sort?: ConversationSort
  cursor?: string | null
}): Promise<ConversationPage> =>
  http.get('/conversations/page', { params: options }).then((r) => r.data)
export const lookupConversations = (
  ids: string[],
  scope: ConversationScope = 'all',
  q = '',
  projectScope: ConversationProjectScope = 'all'
): Promise<ConversationPage> =>
  http
    .post('/conversations/lookup', { ids, scope, q, project_scope: projectScope })
    .then((r) => r.data)
export const getConversationSummary = (id: string): Promise<Conversation> =>
  http.get(`/conversations/${encodeURIComponent(id)}/activity`).then((r) => r.data)
export interface ConversationArchiveState {
  id: string
  archived_at: string | null
  archive_revision: number
}
export const setConversationArchived = (
  id: string,
  archived: boolean,
  expectedRevision: number
): Promise<ConversationArchiveState> =>
  http
    .put(`/conversations/${encodeURIComponent(id)}/archive`, {
      archived,
      expected_revision: expectedRevision,
    })
    .then((r) => r.data)
export const getConversation = (
  id: string,
  signal?: AbortSignal,
  window?: { limit?: number; offset?: number; anchor?: string }
): Promise<{
  id: string
  title: string
  messages: ConversationMessage[]
  archived_at?: string | null
  archive_revision?: number
  history_window?: {
    total: number
    offset: number
    limit: number | null
    previous_offset: number | null
    next_offset: number | null
  }
}> =>
  http
    .get(`/conversations/${encodeURIComponent(id)}`, { signal, params: window })
    .then((r) => r.data)
export const markConversationRead = (
  id: string,
  messageIds: string[]
): Promise<{ message_ids: string[] }> =>
  http
    .post(`/conversations/${encodeURIComponent(id)}/read`, { message_ids: messageIds })
    .then((r) => r.data)
export const getPlanClaims = (
  id: string,
  recordIds: string[]
): Promise<{ items: { id: string; plan_run_id: string | null }[] }> =>
  http
    .post(`/conversations/${encodeURIComponent(id)}/plan-claims`, { record_ids: recordIds })
    .then((r) => r.data)
export const createConversation = (
  title?: string,
  projectId?: string
): Promise<{ id: string; title: string }> =>
  http
    .post('/conversations', { title: title || '新会话', project_id: projectId })
    .then((r) => r.data)
export const renameConversation = (id: string, title: string) =>
  http.put(`/conversations/${encodeURIComponent(id)}`, { title }).then((r) => r.data)
export const pinConversation = (
  id: string,
  pinned: boolean
): Promise<{ id: string; pinned_at: string | null; pin_position?: number | null }> =>
  http.put(`/conversations/${encodeURIComponent(id)}/pin`, { pinned }).then((r) => r.data)
export const reorderConversationPins = (
  ids: string[],
  expectedOrder: string[],
  projectScope: ConversationProjectScope = 'all'
): Promise<{ conversation_ids: string[] }> =>
  http
    .put('/conversations/pins/order', {
      conversation_ids: ids,
      expected_order: expectedOrder,
      project_scope: projectScope,
    })
    .then((r) => r.data)
export const deleteConversation = (id: string) =>
  http.delete(`/conversations/${encodeURIComponent(id)}`).then((r) => r.data)
export const previewConversationDeletion = (
  id: string
): Promise<{
  total_records: number
  can_delete: boolean
  cloud_file_blockers: unknown[]
  retained_project_journals: Record<string, number>
}> => http.get(`/conversations/${encodeURIComponent(id)}/deletion-preview`).then((r) => r.data)
export const forkConversation = (
  id: string,
  afterMessageId?: string | null,
  title?: string
): Promise<{ id: string; title: string; message_count: number }> =>
  http
    .post(`/conversations/${encodeURIComponent(id)}/fork`, {
      after_message_id: afterMessageId || null,
      title: title || null,
    })
    .then((r) => r.data)

export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}
export const getConversationUsage = (
  id: string
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
export const compactConversation = (
  id: string,
  expectedRevision: number,
  force = false
): Promise<ConversationMemory> =>
  http
    .post(`/conversations/${encodeURIComponent(id)}/memory/compact`, {
      expected_revision: expectedRevision,
      force,
    })
    .then((r) => r.data)
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
    method: 'POST',
    keepalive: true,
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
  resume: ChatResumeOptions = {},
  onStarted?: () => void
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
      ...resume,
    }),
    signal,
  })
  if (!resp.ok || !resp.body) {
    const error = await resp.json().catch(() => ({}))
    throw Object.assign(
      new Error(typeof error.detail === 'string' ? error.detail : `问答请求失败：${resp.status}`),
      { status: resp.status }
    )
  }

  onStarted?.()
  await readChatEventStream(resp.body, onEvent, signal)
}

/** Observe an existing execution; this never submits or resumes model work. */
export async function observeChatRun(
  runId: string,
  conversationId: string,
  after: number,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const params = new URLSearchParams({ conversation_id: conversationId, after: String(after || 0) })
  const response = await fetch(`/api/chat/runs/${encodeURIComponent(runId)}/events?${params}`, {
    headers: { Accept: 'text/event-stream' },
    cache: 'no-store',
    signal,
  })
  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => ({}))
    throw Object.assign(
      new Error(typeof data.detail === 'string' ? data.detail : '实时进度连接失败'),
      {
        status: response.status,
      }
    )
  }
  await readChatEventStream(response.body, onEvent, signal)
}

export interface ForegroundRunCheckpoint {
  source_revision_ids?: string[]
  run_revision?: number
  attempt_id?: string | null
  goal_id?: string | null
  resume_kind?: 'continue_missing' | 'checkpoint' | 'resources' | null
  resume_reason?: string
  resource_pause?: ResourcePause | null
  runtime_config?: RuntimeConfiguration | null
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
  status: 'running' | 'paused' | 'interrupted' | 'cancelled' | 'failed' | 'completed' | 'dismissed'
  resume_available: boolean
  error: string
  created_at: string
  updated_at: string
  approvals?: ApprovalRequest[]
  display_snapshot?: ChatRunSnapshot | null
  event_cursor?: number
}

export const createForegroundRun = (
  run: Pick<
    ForegroundRunCheckpoint,
    'id' | 'conversation_id' | 'question' | 'paper_id' | 'skills' | 'images'
  > &
    ExecutionOptions
) => http.post('/foreground-runs', run).then((response) => response.data)

export const patchForegroundRun = (
  id: string,
  revision: number,
  patch: {
    partial_answer?: string
    phase?: string
    status?: 'running' | 'interrupted' | 'cancelled' | 'failed'
    error?: string
  },
  keepalive = false
): Promise<ForegroundRunCheckpoint> => {
  const body = JSON.stringify({ revision, ...patch })
  return fetch(`/api/foreground-runs/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body,
    keepalive: keepalive && new Blob([body]).size < 60_000,
  }).then(async (response) => {
    const data = await response.json()
    if (!response.ok)
      throw Object.assign(new Error(data.detail?.message || data.detail || '前台检查点保存失败'), {
        status: response.status,
        data,
      })
    return data
  })
}

export const listForegroundRuns = (
  conversationId: string,
  signal?: AbortSignal
): Promise<{ runs: ForegroundRunCheckpoint[] }> =>
  http
    .get('/foreground-runs', { params: { conversation_id: conversationId }, signal })
    .then((response) => response.data)

export const dismissForegroundRun = (id: string) =>
  http.delete(`/foreground-runs/${encodeURIComponent(id)}`).then((response) => response.data)

// ---- arXiv 追踪与推荐 ----
export interface RecommendationOut {
  id: string
  arxiv_id: string | null
  title: string
  authors: string
  abstract: string
  published_at: string | null
  url: string
  github_url: string | null
  score: number
  display_score: number
  reasons: string[]
  feedback: 'up' | 'down' | null
}
export interface SubscriptionOut {
  id: string
  topic: string
  interval_hours: number
  active: boolean
  last_run_at: string | null
  last_status: string
  recommendations: RecommendationOut[]
}
export const listSubscriptions = (): Promise<SubscriptionOut[]> =>
  http.get('/tracking/subscriptions').then((r) => r.data)
export const createSubscription = (
  topic: string,
  interval_hours: number
): Promise<SubscriptionOut> =>
  http.post('/tracking/subscriptions', { topic, interval_hours }).then((r) => r.data)
export const deleteSubscription = (id: string): Promise<{ deleted: string }> =>
  http.delete(`/tracking/subscriptions/${encodeURIComponent(id)}`).then((r) => r.data)
export const toggleSubscription = (id: string): Promise<SubscriptionOut> =>
  http.post(`/tracking/subscriptions/${encodeURIComponent(id)}/toggle`).then((r) => r.data)
export const refreshSubscription = (
  id: string
): Promise<{ result: { status: string; added: number }; subscription: SubscriptionOut }> =>
  http.post(`/tracking/subscriptions/${encodeURIComponent(id)}/refresh`).then((r) => r.data)
export const giveFeedback = (recId: string, feedback: 'up' | 'down'): Promise<RecommendationOut> =>
  http
    .post(`/tracking/recommendations/${encodeURIComponent(recId)}/feedback`, { feedback })
    .then((r) => r.data)

// ---- 无幻觉论文框架 ----
export interface FrameworkReference {
  ref_index: number
  paper_id: string
  title: string
  authors: string
  arxiv_id: string | null
  url: string
  published_at: string | null
  relevance: number
  evidence: FrameworkEvidence[]
}
export interface FrameworkEvidence {
  id: string
  paper_id: string
  source_id: string
  revision_id: string
  locator_id: string
  source_title: string
  quote: string
  excerpt_truncated: boolean
  verification: string
}
export interface FrameworkBullet {
  text: string
  citations: number[]
  evidence_ids?: string[]
  provenance_status?: string
  support_status?: string
  support_review?: { explanation: string; evidence_ids: string[]; reviewer: string }
}
export interface FrameworkSection {
  key: string
  title: string
  bullets: FrameworkBullet[]
}
export interface FrameworkOut {
  idea: string
  mode: 'llm' | 'offline'
  verified: boolean
  structure_valid: boolean
  semantic_verification: string
  conversation_id?: string | null
  sections: FrameworkSection[]
  references: FrameworkReference[]
  warnings: string[]
}
export const generateFramework = (
  idea: string,
  max_sources = 8,
  conversation_id?: string
): Promise<FrameworkOut> =>
  http.post('/writing/framework', { idea, max_sources, conversation_id }).then((r) => r.data)
