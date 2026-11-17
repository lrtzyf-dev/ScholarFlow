type ProcessEvent = {
  kind: string
  text?: string
  preamble?: boolean
  seq?: number
  name?: string
  tool_name?: string
  arguments?: Record<string, unknown>
  gate?: { status?: string }
  audit?: { status?: string }
}

// These events update other display state or carry transport/setup telemetry.
// They do not become extra process cards or repeat thinking/plan sections.
const HIDDEN_PROCESS_KINDS = new Set([
  'answer',
  'done',
  'thinking_delta',
  'answer_delta',
  'answer_reset',
  'runtime_config',
  'execution_mode',
  'status',
  'paper_scope',
  'step',
  'snapshot',
  'stream_end',
  'thinking',
  'plan',
  'todo_update',
  'subagents',
  'approval_request',
  'approval_result',
  'commentary',
  'tool_call',
  'tool_result',
  'task_outcome',
  'provider_report',
])

export type PublicProcessEntry<T extends ProcessEvent> =
  | { id: string; type: 'commentary'; text: string; event: T }
  | { id: string; type: 'tool'; event: T; result?: T }
  | { id: string; type: 'provider_report'; event: T }

/** Preserve the provider's public narration and real tool order without inventing stages. */
export function publicProcessEntries<T extends ProcessEvent>(events: readonly T[]) {
  const entries: PublicProcessEntry<T>[] = []
  events.forEach((event, index) => {
    const id = event.seq === undefined ? `event-${index}` : `seq-${event.seq}`
    if (
      (event.kind === 'commentary' || (event.kind === 'thinking' && event.preamble)) &&
      event.text?.trim()
    ) {
      const previous = entries[entries.length - 1]
      // Older adapters may publish both encodings for the same preamble.
      if (
        previous?.type === 'commentary' &&
        previous.text === event.text &&
        previous.event.kind !== event.kind
      )
        return
      entries.push({ id, type: 'commentary', text: event.text, event })
    } else if (event.kind === 'provider_report') entries.push({ id, type: 'provider_report', event })
    else if (event.kind === 'tool_call') entries.push({ id, type: 'tool', event })
    else if (event.kind === 'tool_result') {
      const name = event.name || event.tool_name
      for (let i = entries.length - 1; i >= 0; i--) {
        const entry = entries[i]
        if (entry.type !== 'tool' || entry.result) continue
        if (name && (entry.event.name || entry.event.tool_name) !== name) continue
        entry.result = event
        return
      }
      // A truncated history may retain a result whose call is no longer present.
      entries.push({ id, type: 'tool', event, result: event })
    }
  })
  return entries
}

export function publicToolArgument(event: ProcessEvent) {
  const args = event.arguments || {}
  for (const key of ['query', 'path', 'file_path', 'command', 'url']) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) return value.slice(0, 180)
  }
  return ''
}

export function processEvents<T extends ProcessEvent>(events: readonly T[]): T[] {
  return events.filter(
    (event) =>
      !HIDDEN_PROCESS_KINDS.has(event.kind) &&
      !(event.kind === 'critic' && event.gate?.status === 'not_applicable') &&
      !(event.kind === 'audit' && event.audit?.status === 'not_applicable')
  )
}

export function processSummary(events: readonly ProcessEvent[]): string {
  let toolCalls = 0
  for (const event of events) {
    if (event.kind === 'tool_call') toolCalls += 1
  }
  return toolCalls ? `${toolCalls} 次工具调用` : ''
}

export function processThinkingParts(
  parts: readonly string[],
  liveThinking: string,
  events: readonly ProcessEvent[]
) {
  const explanations = new Map<string, { thinking: number; preamble: number }>()
  for (const event of events) {
    if (event.kind !== 'thinking' || !event.text) continue
    const matches = explanations.get(event.text) || { thinking: 0, preamble: 0 }
    matches[event.preamble ? 'preamble' : 'thinking'] += 1
    explanations.set(event.text, matches)
  }
  const remaining = new Map<string, number>()
  for (const text of parts) remaining.set(text, (remaining.get(text) || 0) + 1)
  const result: { text: string; kind: 'thinking' }[] = []
  for (const text of parts) {
    const count = remaining.get(text) || 0
    remaining.set(text, count - 1)
    const match = explanations.get(text)
    if (match?.preamble && count > match.thinking) {
      match.preamble -= 1
      continue
    }
    result.push({ text, kind: 'thinking' })
  }
  if (liveThinking) result.push({ text: liveThinking, kind: 'thinking' })
  return result
}

interface ProcessDisplay {
  events: readonly ProcessEvent[]
  thinkingParts: readonly string[]
  liveThinking: string
  todo: readonly { status: string }[]
  subagents: readonly unknown[]
  streaming: boolean
  interruptedStatus: string | null
  approvals: readonly unknown[]
  cancelled: boolean
  error: string
  historyTruncated?: boolean
  taskOutcome?: string
}

/** Presentation follows actual emitted work, never the wording of the request. */
export function processPresentation(turn: ProcessDisplay) {
  const active =
    turn.streaming || ['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || '')
  const hasThinking = processThinkingParts(turn.thinkingParts, turn.liveThinking, turn.events).some(
    (part) => part.text.trim()
  )
  const expandable =
    hasThinking ||
    turn.todo.length > 0 ||
    turn.subagents.length > 0 ||
    processEvents(turn.events).length > 0 ||
    Boolean(turn.historyTruncated)
  let label = hasThinking ? '思考过程' : '过程记录'
  if (turn.approvals.length) label = '等待批准'
  else if (turn.cancelled || turn.interruptedStatus === 'cancelled') label = '已停止'
  else if (turn.error || turn.interruptedStatus === 'failed') label = '处理失败'
  else if (turn.interruptedStatus === 'paused') label = '已暂停'
  else if (turn.interruptedStatus === 'interrupted') label = '连接中断'
  else if (active) label = turn.liveThinking.trim() ? '思考中…' : '正在处理…'
  else if (turn.taskOutcome === 'partial') label = '部分完成'
  else if (turn.taskOutcome === 'blocked') label = '任务受阻'
  else if (turn.taskOutcome === 'completed') label = '已完成'
  else if (turn.taskOutcome === 'planned') label = '计划已生成'
  const summary = [
    turn.todo.length
      ? `${turn.todo.filter((step) => step.status === 'done').length}/${turn.todo.length} 项步骤`
      : '',
    processSummary(turn.events),
  ]
    .filter(Boolean)
    .join(' · ')
  return {
    active,
    expandable,
    visible: expandable || (active && publicProcessEntries(turn.events).length === 0),
    label,
    summary,
  }
}
