import assert from 'node:assert/strict'
import test from 'node:test'
import {
  applyChatRunEvent,
  applyChatRunPause,
  citationReviewIssues,
  deduplicateChatRunDisplays,
  readChatEventStream,
  restoreChatRunTrace,
} from '../src/chatRunObserver.ts'
import { renderAnswer } from '../src/markdown.ts'

test('final citation issues keep confirmed unsupported claims separate from unverified ones', () => {
  const issues = citationReviewIssues({
    audit: {
      unsupported: [{ text: '准确率为 98%', reason: '材料中没有该数值' }],
      unverified: [{ text: '模型使用该算法', reason: '相关摘录被截断' }],
    },
    gate: {
      unsupported: ['我没有检索到完整目录', '没有引用的结论'],
      scoped_review: { verified: true, decisions: [
        { kind: 'uncertain', requires_citation: true, text: '我没有检索到完整目录', reason: '缺少检索回执' },
        { kind: 'unsupported', requires_citation: true, text: '没有引用的结论', reason: '缺少来源' },
        { kind: 'process', requires_citation: false, text: '只读了一个文件', reason: '有过程回执' },
      ] },
    },
  })
  assert.deepEqual(issues, [
    { status: 'unsupported', text: '准确率为 98%', reason: '材料中没有该数值' },
    { status: 'unverified', text: '模型使用该算法', reason: '相关摘录被截断' },
    { status: 'unverified', text: '我没有检索到完整目录', reason: '缺少检索回执' },
    { status: 'unsupported', text: '没有引用的结论', reason: '缺少来源' },
  ])
  assert.deepEqual(citationReviewIssues({
    audit: null,
    gate: { unsupported: ['无法确认这条陈述'], scoped_review: { verified: true } },
  }), [{ status: 'unsupported', text: '无法确认这条陈述', reason: '' }])
  assert.deepEqual(citationReviewIssues({
    audit: null,
    gate: { unsupported: ['可能缺少来源'], scoped_review: { verified: false } },
  }), [{ status: 'unverified', text: '可能缺少来源', reason: '' }])
})

test('initial history and recovered snapshot merge immediately by exact QA ID', () => {
  const history = { ...display(), recordId: 'qa-A', answer: 'saved answer' }
  const recovered = display()
  applyChatRunEvent(
    recovered,
    snapshot({ result: { kind: 'answer', qa_id: 'qa-A', answer: 'saved answer' } })
  )
  const merged = deduplicateChatRunDisplays([history, recovered])
  assert.deepEqual(merged, [recovered])
})

test('authoritative delivery retains the observed object and never guesses identity from question text', () => {
  const observed = { ...display(), question: '相同的问题', recordId: 'qa-A' }
  const saved = { ...display(), question: '相同的问题', recordId: 'qa-A' }
  const another = { ...display(), question: '相同的问题', recordId: 'qa-B' }
  const unknown = { ...display(), question: '相同的问题', recordId: null }
  assert.deepEqual(deduplicateChatRunDisplays([observed, saved, another, unknown], observed), [
    observed,
    another,
    unknown,
  ])
})

function display() {
  return {
    answer: '',
    evidence: [],
    gate: null,
    audit: null,
    events: [],
    streaming: true,
    localRun: false,
    cancelled: false,
    refusal: 0,
    latency: 0,
    error: '',
    liveThinking: '',
    liveAnswer: '',
    thinkingParts: [],
    externalPapers: [],
    todo: [],
    approvals: [],
    subagents: [],
    recordId: null,
    usage: null,
    paperId: null,
    checkpointPhase: '',
    resumeAvailable: false,
    interruptedStatus: null,
  }
}

function snapshot(fields = {}) {
  return {
    kind: 'snapshot',
    run_id: 'run-A',
    conversation_id: 'chat-A',
    cursor: 5,
    partial_answer: '正文',
    thinking_parts: ['已完成思考'],
    thinking: '正在思考',
    events: [{ kind: 'tool_result', text: '已有工具结果' }],
    plan_steps: [],
    approvals: [],
    result: null,
    status: 'running',
    phase: 'answer',
    error: '',
    terminal: false,
    history_truncated: false,
    ...fields,
  }
}

test('snapshot replaces partial state, deduplicates old events, and accepts new output', () => {
  const turn = display()
  turn.liveAnswer = '旧输出'
  turn.events = [{ kind: 'tool_call', text: '旧调用' }]
  applyChatRunEvent(turn, snapshot())
  assert.equal(turn.liveAnswer, '正文')
  assert.equal(turn.liveThinking, '正在思考')
  assert.deepEqual(turn.thinkingParts, ['已完成思考'])
  assert.equal(turn.events.length, 1)
  assert.equal(turn.events[0].text, '已有工具结果')
  assert.equal(
    applyChatRunEvent(turn, { kind: 'answer_delta', seq: 5, text: '重复' }).applied,
    false
  )
  applyChatRunEvent(turn, { kind: 'answer_delta', seq: 6, text: '续写' })
  assert.equal(turn.liveAnswer, '正文续写')
  assert.equal(turn.events.length, 1)
  assert.equal(applyChatRunEvent(turn, snapshot({ cursor: 4 })).applied, false)
  assert.equal(
    applyChatRunEvent(turn, snapshot({ cursor: 6, partial_answer: '准确快照' })).applied,
    true
  )
  assert.equal(turn.liveAnswer, '准确快照')
})

test('streamed evidence makes existing draft citations available without ending the run', () => {
  const turn = display()
  applyChatRunEvent(turn, { kind: 'answer_delta', seq: 1, text: '原文支持的论断。[^e45]' })
  applyChatRunEvent(turn, {
    kind: 'evidence', seq: 2, evidence: [
      { id: 'e45', paper_id: 'paper-A', quote: '原文内容', chunk_id: 'chunk-A' },
      { id: 'javascript:bad', quote: '不是合法证据编号' },
      null,
    ],
  })
  assert.equal(turn.liveAnswer, '原文支持的论断。[^e45]')
  assert.deepEqual(turn.evidence.map((item) => item.id), ['e45'])
  assert.match(
    renderAnswer(turn.liveAnswer, turn.evidence.map((item) => item.id)).html,
    /data-evidence-id="e45"/
  )
  assert.equal(turn.checkpointPhase, 'answer', 'source delivery does not replace the actual phase')
  assert.equal(turn.streaming, true)
  assert.equal(turn.answer, '')
  applyChatRunEvent(turn, { kind: 'evidence', seq: 3, evidence: [
    { id: 'e45', paper_id: 'paper-A', quote: '更新后的原文', chunk_id: 'chunk-A' },
    { id: 'e46', paper_id: 'paper-A', quote: '另一片段', chunk_id: 'chunk-B' },
  ] })
  assert.equal(turn.evidence.length, 2)
  assert.equal(turn.evidence[0].quote, '更新后的原文')
  assert.equal(applyChatRunEvent(turn, { kind: 'evidence', seq: 2, evidence: [] }).applied, false)
  assert.equal(turn.evidence.length, 2)
})

test('citation review snapshots restore sources and progress before final answer delivery', () => {
  const turn = display()
  const evidence = { id: 'e45', paper_id: 'paper-A', quote: '原文', chunk_id: 'chunk-A' }
  applyChatRunEvent(turn, snapshot({
    cursor: 9, partial_answer: '正文。[^e45]', phase: 'finalize',
    events: [
      { kind: 'evidence', seq: 7, evidence: [evidence] },
      { kind: 'review_progress', seq: 9, status: 'running', completed: 1, total: 3 },
    ],
  }))
  assert.deepEqual(turn.evidence, [evidence])
  assert.equal(turn.reviewProgress.completed, 1)
  assert.equal(turn.reviewProgress.total, 3)
  assert.equal(turn.liveAnswer, '正文。[^e45]')
  assert.equal(turn.checkpointPhase, 'finalize')
  assert.equal(turn.streaming, true)
  applyChatRunEvent(turn, { kind: 'review_progress', seq: 10, progress: {
    status: 'running', completed: 2, total: 3,
  } })
  assert.equal(turn.reviewProgress.completed, 2)
  assert.equal(turn.checkpointPhase, 'finalize')
  applyChatRunEvent(turn, { kind: 'answer', seq: 11, answer: '已核查正文。[^e45]', evidence: [evidence] })
  assert.equal(turn.reviewProgress, null)
  assert.equal(turn.liveAnswer, '')
  assert.equal(turn.streaming, true, 'answer publication waits for the real terminal event')
  applyChatRunEvent(turn, { kind: 'done', seq: 12 })
  assert.equal(turn.streaming, false)
})

test('truncated snapshot source state survives missing older evidence events and new attempts clear it', () => {
  const turn = display()
  const evidence = { id: 'e1', paper_id: 'paper-A', quote: '原文', chunk_id: 'chunk-A' }
  applyChatRunEvent(turn, snapshot({
    attempt_id: 'old', cursor: 1005, history_truncated: true, events: [],
    evidence: [evidence], review_progress: { status: 'running', completed: 2, total: 4 },
  }))
  assert.deepEqual(turn.evidence, [evidence])
  assert.equal(turn.reviewProgress.completed, 2)
  applyChatRunEvent(turn, { kind: 'run', attempt_id: 'new', seq: 1 })
  assert.deepEqual(turn.evidence, [])
  assert.equal(turn.reviewProgress, null)
  assert.equal(applyChatRunEvent(turn, {
    kind: 'evidence', attempt_id: 'old', seq: 1006, evidence: [evidence],
  }).applied, false)
  assert.deepEqual(turn.evidence, [])
})

test('reset and thinking preamble remove draft answer without duplicating thoughts', () => {
  const turn = display()
  applyChatRunEvent(turn, { kind: 'answer_delta', text: '草稿' })
  applyChatRunEvent(turn, { kind: 'answer_reset' })
  assert.equal(turn.liveAnswer, '')
  applyChatRunEvent(turn, { kind: 'thinking_delta', text: '推理片段' })
  applyChatRunEvent(turn, { kind: 'thinking', text: '推理片段' })
  applyChatRunEvent(turn, { kind: 'answer_delta', text: '工具调用前言' })
  applyChatRunEvent(turn, { kind: 'thinking', text: '工具调用前言', preamble: true })
  assert.deepEqual(turn.thinkingParts, ['推理片段'])
  assert.equal(turn.liveThinking, '')
  assert.equal(turn.liveAnswer, '')
})

test('commentary confirms a streamed draft once without exposing it as raw reasoning', () => {
  const turn = display()
  applyChatRunEvent(turn, { kind: 'answer_delta', seq: 1, text: '我先检查实际材料。' })
  applyChatRunEvent(turn, {
    kind: 'commentary',
    seq: 2,
    text: '我先检查实际材料。',
    source: 'model',
    draft_to_commentary: true,
  })
  applyChatRunEvent(turn, { kind: 'tool_call', seq: 3, name: 'read_file' })
  assert.equal(turn.liveAnswer, '')
  assert.deepEqual(turn.thinkingParts, [])
  assert.equal(turn.events.filter((event) => event.kind === 'commentary').length, 1)
  assert.equal(
    applyChatRunEvent(turn, {
      kind: 'commentary',
      seq: 2,
      text: '我先检查实际材料。',
      draft_to_commentary: true,
    }).applied,
    false
  )
  applyChatRunEvent(turn, { kind: 'answer_delta', seq: 4, text: '最终正文' })
  assert.equal(turn.liveAnswer, '最终正文')
  assert.deepEqual(
    turn.events.map((event) => event.kind),
    ['commentary', 'tool_call']
  )
})

test('snapshot and saved trace preserve public commentary in original event order', () => {
  const trace = [
    { kind: 'commentary', seq: 1, text: '先检查证据。', draft_to_commentary: true },
    { kind: 'tool_call', seq: 2, name: 'read_file' },
    { kind: 'tool_result', seq: 3, name: 'read_file', preview: '实际材料' },
    { kind: 'commentary', seq: 4, text: '已发现差异，我会核对另一处。', draft_to_commentary: true },
  ]
  const observed = display()
  applyChatRunEvent(
    observed,
    snapshot({ cursor: 4, partial_answer: '后续正文', events: trace, thinking_parts: [] })
  )
  assert.deepEqual(observed.events, trace)
  assert.equal(observed.liveAnswer, '后续正文')
  const saved = display()
  saved.answer = '已保存最终答案'
  restoreChatRunTrace(saved, trace)
  assert.deepEqual(
    saved.events.map((event) => event.kind),
    trace.map((event) => event.kind)
  )
  assert.equal(saved.answer, '已保存最终答案')
  assert.deepEqual(saved.thinkingParts, [])
})

test('an empty malformed commentary cannot discard a visible answer draft', () => {
  const turn = display()
  turn.liveAnswer = '尚未确认的实际输出'
  applyChatRunEvent(turn, { kind: 'commentary', text: '', draft_to_commentary: true })
  assert.equal(turn.liveAnswer, '尚未确认的实际输出')
  applyChatRunEvent(turn, { kind: 'thinking', text: '', preamble: true })
  assert.equal(turn.liveAnswer, '尚未确认的实际输出')
})

test('final answer preserves an unfinished public thinking delta', () => {
  const turn = display()
  applyChatRunEvent(turn, { kind: 'thinking_delta', text: '最后一段公开思考' })
  const result = applyChatRunEvent(turn, { kind: 'answer', answer: '最终回答', qa_id: 'qa-A' })
  assert.equal(result.answerReceived, true)
  assert.equal(turn.answer, '最终回答')
  assert.equal(turn.recordId, 'qa-A')
  assert.equal(turn.liveThinking, '')
  assert.deepEqual(turn.thinkingParts, ['最后一段公开思考'])
})

test('overflow stream_end requests reconnection without treating execution as terminal', () => {
  const turn = display()
  applyChatRunEvent(turn, snapshot())
  const overflow = applyChatRunEvent(turn, {
    kind: 'stream_end',
    seq: 5,
    status: 'running',
    terminal: false,
  })
  assert.equal(overflow.applied, true)
  assert.equal(overflow.terminal, false)
  assert.equal(turn.streaming, true)
  const completed = applyChatRunEvent(turn, {
    kind: 'stream_end',
    seq: 5,
    status: 'completed',
    terminal: true,
  })
  assert.equal(completed.terminal, true)
  assert.equal(turn.streaming, false)
  assert.equal(turn.interruptedStatus, 'finishing')
})

test('paused, cancelled and failed snapshots preserve their actual terminal states', () => {
  for (const status of ['paused', 'cancelled', 'failed']) {
    const turn = display()
    applyChatRunEvent(
      turn,
      snapshot({ status, terminal: true, error: status === 'failed' ? '失败原因' : '' })
    )
    assert.equal(turn.interruptedStatus, status)
    assert.equal(turn.streaming, false)
    assert.equal(turn.answer, '')
    assert.deepEqual(turn.approvals, [])
    assert.equal(turn.cancelled, status === 'cancelled')
    if (status === 'paused') assert.equal(turn.resumeAvailable, true)
    if (status === 'failed') assert.equal(turn.error, '失败原因')
  }
})

test('live pauses retain the actual cause and explanation through stream end', () => {
  for (const [reason, kind, phase] of [
    ['context_capacity', 'context', 'context_paused'],
    ['resource_budget', 'budget', 'resource_paused'],
    ['no_progress', undefined, 'no_progress_paused'],
    ['step_limit', undefined, 'budget_paused'],
  ]) {
    const turn = display()
    applyChatRunEvent(turn, {
      kind: 'paused', seq: 1, reason, text: '服务端保存的具体原因',
      ...(kind ? { resource_pause: { kind, message: '具体资源限制', phase: 'model' } } : {}),
    })
    applyChatRunEvent(turn, { kind: 'stream_end', seq: 2, status: 'paused', terminal: true })
    assert.equal(turn.pauseReason, reason)
    assert.equal(turn.pauseMessage, kind ? '具体资源限制' : '服务端保存的具体原因')
    assert.equal(turn.checkpointPhase, phase)
    assert.equal(turn.interruptedStatus, 'paused')
  }
})

test('paused snapshots restore causes from public events or checkpoint resource metadata', () => {
  const turn = display()
  applyChatRunEvent(turn, snapshot({
    status: 'paused', terminal: true, phase: 'stream_end',
    events: [{ kind: 'paused', reason: 'context_capacity', text: '上下文过长' }],
  }))
  assert.equal(turn.pauseReason, 'context_capacity')
  assert.equal(turn.pauseMessage, '上下文过长')
  assert.equal(turn.checkpointPhase, 'context_paused')

  const recovered = display()
  applyChatRunEvent(recovered, snapshot({ status: 'paused', terminal: true, events: [] }))
  applyChatRunPause(recovered, {
    resume_reason: '预算说明', resource_pause: { kind: 'budget', message: '尚未设置调用价格' },
  })
  assert.equal(recovered.pauseReason, 'resource_budget')
  assert.equal(recovered.pauseMessage, '尚未设置调用价格')
  assert.equal(recovered.checkpointPhase, 'resource_paused')
})

test('unknown pauses stay generic and a resumed attempt clears its previous cause', () => {
  const turn = display()
  applyChatRunEvent(turn, { kind: 'paused', text: '未知原因', seq: 1, attempt_id: 'old' })
  assert.equal(turn.pauseReason, '')
  assert.equal(turn.checkpointPhase, 'paused')
  applyChatRunEvent(turn, snapshot({
    cursor: 1, attempt_id: 'new', status: 'running', terminal: false,
  }))
  assert.equal(turn.pauseMessage, '')
  assert.equal(turn.resourcePause, null)
  assert.equal(turn.interruptedStatus, null)
})

test('historical replay restores public process without reviving old approvals or replacing saved answer', () => {
  const turn = display()
  turn.answer = '数据库中的最终回答'
  restoreChatRunTrace(turn, [
    { kind: 'thinking_delta', text: '公开思考' },
    { kind: 'thinking', text: '公开思考' },
    { kind: 'tool_call', tool: 'read_file' },
    { kind: 'approval_request', approval_id: 'old' },
    { kind: 'approval_result', approval_id: 'old' },
    { kind: 'answer', answer: '旧回答' },
    { kind: 'done' },
  ])
  assert.equal(turn.answer, '数据库中的最终回答')
  assert.deepEqual(turn.thinkingParts, ['公开思考'])
  assert.deepEqual(turn.approvals, [])
  assert.equal(
    turn.events.some((event) => event.kind === 'tool_call'),
    true
  )
  assert.equal(
    turn.events.some((event) => event.kind === 'approval_request'),
    false
  )
  assert.equal(turn.streaming, false)
})

test('historical partial and blocked task outcomes remain accurate after reload', () => {
  for (const outcome of ['partial', 'blocked']) {
    const turn = display()
    turn.answer = '已保存的部分结果'
    const incomplete = [{ title: '尚未执行的验证', phase: 'verify', status: 'pending' }]
    restoreChatRunTrace(turn, [
      { kind: 'tool_call', name: 'read_file' },
      { kind: 'task_outcome', outcome, completed: false, incomplete_steps: incomplete },
    ])
    assert.equal(turn.taskOutcome, outcome)
    assert.deepEqual(turn.incompleteSteps, incomplete)
    assert.equal(turn.answer, '已保存的部分结果')
  }
})

test('snapshot restores actual outcome before a legacy answer payload and never invents old history completion', () => {
  const turn = display()
  applyChatRunEvent(
    turn,
    snapshot({
      events: [
        {
          kind: 'task_outcome',
          outcome: 'partial',
          completed: false,
          incomplete_steps: [{ title: '待验证', phase: 'verify', status: 'pending' }],
        },
      ],
      result: { kind: 'answer', answer: '保留的结果', qa_id: 'qa-A' },
    })
  )
  assert.equal(turn.taskOutcome, 'partial')
  assert.equal(turn.incompleteSteps.length, 1)
  const legacy = display()
  legacy.answer = '旧历史回答'
  restoreChatRunTrace(legacy, [{ kind: 'tool_result', name: 'read_file' }])
  assert.equal(legacy.taskOutcome, undefined)
})

test('timeline history is bounded and exposes truncation', () => {
  const turn = display()
  for (let index = 0; index < 1005; index++)
    applyChatRunEvent(turn, { kind: 'tool_result', seq: index + 1, text: String(index) })
  assert.equal(turn.events.length, 1000)
  assert.equal(turn.events[0].text, '5')
  assert.equal(turn.historyTruncated, true)
})

function streamChunks(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
}

test('SSE reader handles fragmented UTF-8, CRLF, multiline data and ignores malformed frames', async () => {
  const encoded = new TextEncoder().encode(
    ': heartbeat\r\n\r\ndata: null\r\n\r\ndata: {bad}\r\n\r\n' +
      'data: {"kind":"thinking_delta",\r\ndata: "text":"中文思考"}\r\n\r\n' +
      'data: {"kind":"answer_delta","text":"回答"}\n\n'
  )
  const events = []
  await readChatEventStream(
    streamChunks([...encoded].map((byte) => Uint8Array.of(byte))),
    (event) => events.push(event)
  )
  assert.deepEqual(events, [
    { kind: 'thinking_delta', text: '中文思考' },
    { kind: 'answer_delta', text: '回答' },
  ])
})

test('SSE reader does not swallow callback errors and releases reader lock', async () => {
  const body = streamChunks([new TextEncoder().encode('data: {"kind":"answer_delta"}\n\n')])
  await assert.rejects(
    readChatEventStream(body, () => {
      throw new Error('callback failed')
    }),
    /callback failed/
  )
  assert.equal(body.locked, false)
})

test('SSE reader aborts pending observation without requesting execution cancellation', async () => {
  let cancelled = false
  const body = new ReadableStream({
    cancel() {
      cancelled = true
    },
  })
  const controller = new AbortController()
  const result = readChatEventStream(
    body,
    () => assert.fail('no event expected'),
    controller.signal
  )
  controller.abort()
  await assert.rejects(result, { name: 'AbortError' })
  assert.equal(cancelled, true)
  assert.equal(body.locked, false)
})

test('SSE reader rejects oversized incomplete frames', async () => {
  const body = streamChunks([new Uint8Array(8_388_609).fill(65)])
  await assert.rejects(
    readChatEventStream(body, () => assert.fail('no event expected')),
    /实时进度帧过大/
  )
  assert.equal(body.locked, false)
})

test('steering events retain server receipt/application separately without changing output phase', () => {
  const turn = display()
  turn.checkpointPhase = 'tool_call'
  const input = {
    input_id: 'input-1',
    kind: 'steer',
    content: 'use B',
    revision: 1,
    status: 'queued',
  }
  applyChatRunEvent(turn, { kind: 'run_input', seq: 1, revision: 1, input })
  assert.equal(turn.runInputs[0].status, 'queued')
  assert.equal(turn.checkpointPhase, 'tool_call')
  applyChatRunEvent(turn, {
    kind: 'run_input',
    seq: 2,
    revision: 2,
    input: { ...input, revision: 2, status: 'applied', goal_revision: 4 },
  })
  assert.equal(turn.runInputs.length, 1)
  assert.equal(turn.runInputs[0].status, 'applied')
  assert.equal(turn.inputQueueRevision, 2)
  applyChatRunEvent(turn, { kind: 'run_input', seq: 3, revision: 1, input })
  assert.equal(turn.runInputs[0].status, 'applied', 'old receipt cannot undo application')
  assert.equal(turn.inputQueueRevision, 2)
})

test('reattachment snapshot restores input events and preserves partial answer', () => {
  const turn = display()
  applyChatRunEvent(
    turn,
    snapshot({
      partial_answer: 'still streaming',
      events: [
        {
          kind: 'run_input',
          revision: 4,
          input: {
            input_id: 'input-recovered',
            kind: 'next_turn',
            content: 'then compare',
            revision: 4,
            status: 'queued',
          },
        },
      ],
    })
  )
  assert.equal(turn.liveAnswer, 'still streaming')
  assert.equal(turn.runInputs[0].status, 'queued')
  assert.equal(turn.inputQueueRevision, 4)
})

test('new attempt snapshot can reset its cursor while stale old-attempt deltas are ignored', () => {
  const turn = display()
  applyChatRunEvent(turn, snapshot({ attempt_id: 'old-attempt', cursor: 80 }))
  applyChatRunEvent(turn, snapshot({ attempt_id: 'new-attempt', cursor: 2, partial_answer: 'new output' }))
  assert.equal(turn.attemptId, 'new-attempt')
  assert.equal(turn.eventCursor, 2)
  applyChatRunEvent(turn, { kind: 'answer_delta', attempt_id: 'old-attempt', seq: 81, text: 'stale' })
  assert.equal(turn.liveAnswer, 'new output')
  applyChatRunEvent(turn, { kind: 'answer_delta', attempt_id: 'new-attempt', seq: 3, text: ' continues' })
  assert.equal(turn.liveAnswer, 'new output continues')
})

test('dispatched input links its single child without claiming steering application', () => {
  const turn = display()
  applyChatRunEvent(turn, { kind: 'run_input', seq: 1, revision: 3, input: {
    input_id: 'input-next', kind: 'next_turn', revision: 3,
    status: 'dispatched', child_run_id: 'chat_child_stable_123',
  } })
  assert.equal(turn.runInputs[0].status, 'dispatched')
  assert.equal(turn.runInputs[0].child_run_id, 'chat_child_stable_123')
  assert.equal(turn.runInputs[0].goal_revision, undefined)
})
