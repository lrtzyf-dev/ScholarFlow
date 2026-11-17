import type {
  AgentEvent,
  ApprovalRequest,
  Evidence,
  RuntimeConfiguration,
  SubagentResult,
  TodoStep,
  TokenUsage,
  RunInput,
} from './api'

export interface ResourcePause {
  kind: string
  message?: string
  phase?: string
}

export interface ChatRunPause {
  pauseReason?: string
  pauseMessage?: string
  resourcePause?: ResourcePause | null
}

export interface ChatRunSnapshot extends AgentEvent {
  kind: 'snapshot'
  run_id: string
  conversation_id: string
  cursor: number
  seq?: number
  partial_answer: string
  thinking_parts: string[]
  thinking: string
  events: AgentEvent[]
  evidence?: Evidence[]
  review_progress?: {
    status?: string
    phase?: string
    message?: string
    text?: string
    completed?: number
    total?: number
    [key: string]: unknown
  } | null
  plan_steps: TodoStep[]
  approvals: ApprovalRequest[]
  runtime_config?: RuntimeConfiguration | null
  result?: AgentEvent | null
  status: string
  phase: string
  error: string
  terminal: boolean
  history_truncated: boolean
  run_inputs?: RunInput[]
  input_revision?: number
  attempt_id?: string | null
  goal_id?: string | null
}

export interface ChatRunDisplay extends ChatRunPause {
  attemptId?: string | null
  goalId?: string | null
  runtimeConfig?: RuntimeConfiguration | null
  answer: string
  evidence: Evidence[]
  reviewProgress?: {
    status?: string
    phase?: string
    message?: string
    text?: string
    completed?: number
    total?: number
    [key: string]: unknown
  } | null
  gate: any
  audit: any
  events: AgentEvent[]
  streaming: boolean
  localRun: boolean
  cancelled: boolean
  refusal: number
  latency: number
  error: string
  liveThinking: string
  liveAnswer: string
  thinkingParts: string[]
  externalPapers: { pdf: string; [key: string]: any }[]
  todo: TodoStep[]
  approvals: ApprovalRequest[]
  subagents: SubagentResult[]
  recordId: string | null
  usage: TokenUsage | null
  paperId: string | null
  paperTitle?: string | null
  checkpointPhase: string
  resumeAvailable: boolean
  interruptedStatus:
    'running_elsewhere' | 'finishing' | 'paused' | 'interrupted' | 'cancelled' | 'failed' | null
  taskOutcome?: 'planned' | 'completed' | 'partial' | 'blocked'
  incompleteSteps?: { title: string; phase: string; status: string }[]
  eventCursor?: number
  historyTruncated?: boolean
  runInputs?: RunInput[]
  inputQueueRevision?: number
}

const DISPLAY_EVENT_LIMIT = 1000
const TEXT_DELTAS = new Set(['answer_delta', 'thinking_delta'])

export interface CitationReviewIssue {
  status: 'unsupported' | 'unverified'
  text: string
  reason: string
}

/** Keep confirmed problems distinct from claims the audit could not verify. */
export function citationReviewIssues(turn: Pick<ChatRunDisplay, 'audit' | 'gate'>): CitationReviewIssue[] {
  const issues: CitationReviewIssue[] = []
  const add = (status: CitationReviewIssue['status'], value: unknown) => {
    if (typeof value === 'string') {
      if (value.trim()) issues.push({ status, text: value.trim(), reason: '' })
      return
    }
    if (!value || typeof value !== 'object') return
    const item = value as Record<string, unknown>
    const text = [item.text, item.claim, item.claim_text].find((part) => typeof part === 'string')
    if (typeof text !== 'string' || !text.trim()) return
    const reason = typeof item.reason === 'string' ? item.reason : ''
    issues.push({ status, text: text.trim(), reason })
  }
  const audit = turn.audit && typeof turn.audit === 'object' ? turn.audit : {}
  if (Array.isArray(audit.unsupported))
    for (const item of audit.unsupported) add('unsupported', item)
  if (Array.isArray(audit.unverified))
    for (const item of audit.unverified) add('unverified', item)
  const decisions = turn.gate?.scoped_review?.decisions
  const missing = Array.isArray(turn.gate?.unsupported) ? turn.gate.unsupported : []
  if (Array.isArray(decisions)) {
    for (const decision of decisions) {
      if (!decision || typeof decision !== 'object' || decision.requires_citation !== true) continue
      if (decision.kind === 'uncertain') add('unverified', decision)
      else if ((turn.gate?.scoped_review?.verified === true || decision.kind === 'unsupported') && missing.includes(decision.text))
        add('unsupported', decision)
    }
  } else {
    for (const text of missing) add(
      turn.gate?.scoped_review?.verified === true ? 'unsupported' : 'unverified', text
    )
  }
  const unique = new Map<string, CitationReviewIssue>()
  for (const issue of issues) {
    const key = `${issue.status}\u0000${issue.text}`
    if (!unique.has(key)) unique.set(key, issue)
  }
  const resolved = [...unique.values()]
  const unsupported = new Set(
    resolved.filter((issue) => issue.status === 'unsupported').map((issue) => issue.text)
  )
  return resolved.filter((issue) => issue.status !== 'unverified' || !unsupported.has(issue.text))
}

/** Keep the server's pause cause across live events and checkpoint recovery. */
export function applyChatRunPause(
  turn: ChatRunPause & { checkpointPhase: string },
  source: { reason?: string; text?: string; error?: string; resume_reason?: string; resource_pause?: ResourcePause | null }
) {
  turn.resourcePause = source.resource_pause || null
  turn.pauseReason = source.reason || ({
    context: 'context_capacity',
    budget: 'resource_budget',
    skill_changed: 'skill_changed',
    memory_changed: 'memory_changed',
  }[turn.resourcePause?.kind || ''] || '')
  turn.pauseMessage = turn.resourcePause?.message || source.resume_reason || source.text || source.error || ''
  turn.checkpointPhase = ({
    context_capacity: 'context_paused',
    resource_budget: 'resource_paused',
    no_progress: 'no_progress_paused',
    step_limit: 'budget_paused',
    skill_changed: 'skill_paused',
    memory_changed: 'memory_paused',
  }[turn.pauseReason] || 'paused')
}

function clearPause(turn: ChatRunPause) {
  turn.pauseReason = ''
  turn.pauseMessage = ''
  turn.resourcePause = null
}

/** Join history and live delivery using the server's QA identity only. */
export function deduplicateChatRunDisplays<T extends { recordId: string | null }>(
  turns: T[],
  preferred?: T
): T[] {
  const byRecord = new Map<string, T>()
  for (const turn of turns) if (turn.recordId) byRecord.set(turn.recordId, turn)
  if (preferred?.recordId) byRecord.set(preferred.recordId, preferred)
  return turns.filter((turn) => !turn.recordId || byRecord.get(turn.recordId) === turn)
}

function applyAnswer(turn: ChatRunDisplay, ev: AgentEvent) {
  turn.answer = ev.answer || ''
  turn.recordId = ev.qa_id || turn.recordId || null
  turn.evidence = ev.evidence || []
  turn.reviewProgress = null
  turn.liveAnswer = ''
  if (turn.liveThinking) turn.thinkingParts.push(turn.liveThinking)
  turn.liveThinking = ''
  turn.gate = ev.gate || null
  turn.audit = ev.audit || null
  applyTaskOutcome(turn, ev)
  turn.refusal = ev.refusal || 0
  turn.latency = ev.latency_ms || 0
  turn.usage = ev.usage || null
  turn.error = ''
  turn.interruptedStatus = null
  clearPause(turn)
  if (ev.budget_exhausted || ev.stop_reason === 'step_limit') {
    turn.interruptedStatus = 'paused'
    applyChatRunPause(turn, { reason: 'step_limit' })
    turn.resumeAvailable = true
  }
  if (ev.plan_steps) turn.todo = ev.plan_steps
}

function mergeEvidence(turn: ChatRunDisplay, evidence: unknown) {
  if (!Array.isArray(evidence)) return
  const byId = new Map(turn.evidence.map((item) => [item.id, item]))
  for (const candidate of evidence) {
    if (!candidate || typeof candidate !== 'object') continue
    const item = candidate as Evidence
    // Preserve the renderer's citation allowlist during streamed delivery.
    if (typeof item.id !== 'string' || !/^e\d+$/.test(item.id)) continue
    byId.set(item.id, item)
  }
  turn.evidence = [...byId.values()]
}

function applyTaskOutcome(turn: ChatRunDisplay, event: AgentEvent) {
  if (['planned', 'completed', 'partial', 'blocked'].includes(event.outcome))
    turn.taskOutcome = event.outcome
  else if (event.completed === true) turn.taskOutcome = 'completed'
  if (Array.isArray(event.incomplete_steps)) turn.incompleteSteps = event.incomplete_steps
}

function applyStatus(turn: ChatRunDisplay, status: string, terminal: boolean) {
  turn.cancelled = status === 'cancelled'
  if (status === 'paused') {
    turn.interruptedStatus = 'paused'
    turn.resumeAvailable = true
  } else if (status === 'interrupted' || status === 'cancelled' || status === 'failed') {
    turn.interruptedStatus = status
    turn.approvals = []
  } else if (status === 'completed') {
    turn.interruptedStatus = turn.answer ? null : 'finishing'
    clearPause(turn)
  } else if (!terminal) {
    turn.interruptedStatus = null
    clearPause(turn)
  }
  if (terminal) {
    turn.streaming = false
    turn.localRun = false
    turn.approvals = []
  }
}

/** Apply only public output. A snapshot replaces display state at its cursor. */
export function applyChatRunEvent(turn: ChatRunDisplay, ev: AgentEvent) {
  if (ev.attempt_id && turn.attemptId && ev.attempt_id !== turn.attemptId) {
    if (!['snapshot', 'run'].includes(ev.kind))
      return { applied: false, answerReceived: false, terminal: false }
    turn.eventCursor = 0
    turn.evidence = []
    turn.reviewProgress = null
    clearPause(turn)
  }
  if (ev.attempt_id) turn.attemptId = ev.attempt_id
  if (ev.goal_id) turn.goalId = ev.goal_id
  const cursor = ev.kind === 'snapshot' ? ev.cursor : ev.seq
  if (Number.isSafeInteger(cursor) && cursor >= 0) {
    if (
      cursor < (turn.eventCursor || 0) ||
      (!['snapshot', 'stream_end'].includes(ev.kind) && cursor <= (turn.eventCursor || 0))
    )
      return { applied: false, answerReceived: false, terminal: false }
    turn.eventCursor = cursor
  }
  if (ev.kind === 'snapshot') {
    const snapshot = ev as ChatRunSnapshot
    turn.answer = ''
    turn.recordId = null
    turn.evidence = []
    turn.reviewProgress = null
    turn.gate = null
    turn.audit = null
    turn.taskOutcome = undefined
    turn.incompleteSteps = []
    turn.liveAnswer = snapshot.partial_answer || ''
    turn.thinkingParts = [...(snapshot.thinking_parts || [])]
    turn.liveThinking = snapshot.thinking || ''
    turn.events = [...(snapshot.events || [])].filter((item) => !TEXT_DELTAS.has(item.kind))
    turn.todo = [...(snapshot.plan_steps || [])]
    turn.approvals = [...(snapshot.approvals || [])]
    if (snapshot.run_inputs) turn.runInputs = [...snapshot.run_inputs]
    if (snapshot.input_revision !== undefined) turn.inputQueueRevision = snapshot.input_revision
    turn.runtimeConfig = snapshot.runtime_config || turn.runtimeConfig
    turn.error = snapshot.error || ''
    turn.checkpointPhase = snapshot.phase || turn.checkpointPhase
    turn.historyTruncated = Boolean(snapshot.history_truncated)
    turn.externalPapers = []
    turn.subagents = []
    mergeEvidence(turn, snapshot.evidence)
    if (snapshot.review_progress) turn.reviewProgress = { ...snapshot.review_progress }
    for (const event of turn.events) collectResultCards(turn, event)
    if (snapshot.result) applyAnswer(turn, snapshot.result)
    applyStatus(turn, snapshot.status, snapshot.terminal)
    if (snapshot.status === 'paused') {
      const pause = [...turn.events].reverse().find((event) => event.kind === 'paused')
      if (pause) applyChatRunPause(turn, pause)
      else if (snapshot.resource_pause || snapshot.resume_reason)
        applyChatRunPause(turn, snapshot)
      else if (!turn.pauseReason && !turn.pauseMessage)
        applyChatRunPause(turn, {
          reason: snapshot.phase === 'budget_paused' ? 'step_limit' : '',
          text: snapshot.error,
        })
    }
    return { applied: true, answerReceived: Boolean(snapshot.result), terminal: snapshot.terminal }
  }
  if (ev.runtime_config) turn.runtimeConfig = ev.runtime_config
  if (!TEXT_DELTAS.has(ev.kind) && ev.kind !== 'stream_end') {
    turn.events.push(ev)
    if (turn.events.length > DISPLAY_EVENT_LIMIT) {
      turn.events.splice(0, turn.events.length - DISPLAY_EVENT_LIMIT)
      turn.historyTruncated = true
    }
  }
  if (ev.kind === 'cancelled') {
    applyStatus(turn, 'cancelled', true)
  } else if (ev.kind === 'paused') {
    applyStatus(turn, 'paused', true)
    applyChatRunPause(turn, ev)
  } else if (ev.kind === 'error') {
    turn.error = ev.text || ''
  } else if (ev.kind === 'paper_scope') {
    turn.paperId = ev.paper_id || turn.paperId
    turn.paperTitle = ev.paper_title || turn.paperTitle
  } else if (ev.kind === 'thinking_delta') {
    turn.liveThinking += ev.text || ''
  } else if (ev.kind === 'answer_delta') {
    turn.liveAnswer += ev.text || ''
  } else if (ev.kind === 'answer_reset') {
    turn.liveAnswer = ''
    turn.reviewProgress = null
  } else if (ev.kind === 'commentary') {
    if (ev.draft_to_commentary && ev.text?.trim()) turn.liveAnswer = ''
  } else if (ev.kind === 'thinking') {
    if (ev.preamble) {
      if (ev.text?.trim()) turn.liveAnswer = ''
    } else {
      if (ev.text) turn.thinkingParts.push(ev.text)
      turn.liveThinking = ''
    }
  } else if ((ev.kind === 'plan' || ev.kind === 'todo_update') && ev.steps) {
    turn.todo = ev.steps
  } else if (ev.kind === 'approval_request') {
    if (!turn.approvals.some((approval) => approval.approval_id === ev.approval_id))
      turn.approvals.push({
        approval_id: ev.approval_id,
        tool_name: ev.tool_name,
        arguments: ev.arguments,
        risk: ev.risk,
        reason: ev.reason,
      })
  } else if (ev.kind === 'approval_result') {
    turn.approvals = turn.approvals.filter((approval) => approval.approval_id !== ev.approval_id)
  } else if (ev.kind === 'answer') {
    applyAnswer(turn, ev)
  } else if (ev.kind === 'stream_end') {
    applyStatus(turn, ev.status || 'interrupted', ev.terminal !== false)
  } else if (ev.kind === 'done') {
    turn.streaming = false
    turn.localRun = false
  }
  collectResultCards(turn, ev)
  if (!['answer', 'done', 'paused', 'stream_end', 'task_outcome', 'run_input', 'evidence', 'review_progress'].includes(ev.kind))
    turn.checkpointPhase =
      ev.kind === 'answer_delta'
        ? 'answer'
        : ev.kind === 'thinking_delta'
          ? 'thinking'
          : ev.kind.slice(0, 32)
  return {
    applied: true,
    answerReceived: ev.kind === 'answer',
    terminal:
      ['done', 'paused', 'cancelled'].includes(ev.kind) ||
      (ev.kind === 'stream_end' && ev.terminal !== false),
  }
}

function collectResultCards(turn: ChatRunDisplay, ev: AgentEvent) {
  if (ev.kind === 'evidence') mergeEvidence(turn, ev.evidence)
  if (ev.kind === 'review_progress') {
    const progress = ev.progress && typeof ev.progress === 'object' ? ev.progress : ev
    turn.reviewProgress = { ...progress }
  }
  if (ev.kind === 'run_input' && ev.input?.input_id) {
    const input = ev.input as RunInput
    const rows = turn.runInputs || []
    const previous = rows.find((row) => row.input_id === input.input_id)
    if (!previous || previous.revision <= input.revision) {
      turn.runInputs = [...rows.filter((row) => row.input_id !== input.input_id), input]
    }
    turn.inputQueueRevision = Math.max(turn.inputQueueRevision || 0, ev.revision || input.revision)
  }
  if (ev.kind === 'task_outcome') applyTaskOutcome(turn, ev)
  if (ev.kind === 'tool_result' && ev.external_papers?.length)
    for (const paper of ev.external_papers)
      if (!turn.externalPapers.some((item) => item.pdf === paper.pdf))
        turn.externalPapers.push({ ...paper, status: 'idle' })
  if (ev.kind === 'subagents') turn.subagents = ev.subagents || []
}

/** Historical trace is display-only; completed approvals must never return. */
export function restoreChatRunTrace(turn: ChatRunDisplay, events: AgentEvent[]) {
  for (const event of events) {
    if (
      [
        'answer',
        'answer_delta',
        'answer_reset',
        'done',
        'cancelled',
        'paused',
        'approval_request',
        'approval_result',
      ].includes(event.kind)
    )
      continue
    applyChatRunEvent(turn, { ...event, seq: undefined })
  }
  turn.approvals = []
  turn.streaming = false
  turn.localRun = false
  turn.interruptedStatus = null
}

/** Shared SSE reader for the initial POST and read-only reattachment GET. */
export async function readChatEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal
) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const abort = () => {
    void reader.cancel().catch(() => {})
  }
  signal?.addEventListener('abort', abort, { once: true })
  function emit(frame: string) {
    const data = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (!data) return
    let event: AgentEvent
    try {
      event = JSON.parse(data)
    } catch {
      return
    }
    if (!event || typeof event !== 'object' || typeof event.kind !== 'string') return
    onEvent(event)
  }
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const { done, value } = await reader.read()
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      if (buffer.length > 8_388_608) throw new Error('实时进度帧过大，请重新同步')
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        emit(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary + 2)
      }
      if (done) break
    }
  } finally {
    signal?.removeEventListener('abort', abort)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
