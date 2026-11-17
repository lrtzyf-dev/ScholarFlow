import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  processEvents,
  processSummary,
  processPresentation,
  processThinkingParts,
  publicProcessEntries,
  publicToolArgument,
} from '../src/chatProcess.ts'

function turn(fields = {}) {
  return {
    events: [],
    thinkingParts: [],
    liveThinking: '',
    todo: [],
    subagents: [],
    streaming: false,
    interruptedStatus: null,
    approvals: [],
    cancelled: false,
    error: '',
    ...fields,
  }
}

test('streaming fragments and internal rounds never inflate the compact summary', () => {
  const events = [
    { kind: 'runtime_config', text: 'model-A' },
    { kind: 'status', text: 'Agent 启动' },
    ...Array.from({ length: 8 }, (_, round) => [
      { kind: 'step', text: `第 ${round + 1} 轮思考` },
      ...Array.from({ length: 100 }, () => ({ kind: 'thinking_delta', text: '片段' })),
      { kind: 'thinking', text: '完整思考' },
      { kind: 'tool_call', name: 'retrieve_evidence' },
      { kind: 'tool_result', name: 'retrieve_evidence', preview: '检索结果' },
    ]).flat(),
    ...Array.from({ length: 23 }, () => ({ kind: 'answer_delta', text: '片段' })),
    { kind: 'error', text: '连接中断' },
  ]
  assert.equal(processSummary(events), '8 次工具调用')
  assert.equal(processEvents(events).length, 1)
  assert.equal(publicProcessEntries(events).length, 8)
  assert.equal(processEvents(events).at(-1).kind, 'error')
})

test('process details preserve actual tool work, errors, refusal and custom events', () => {
  const events = [
    { kind: 'tool_call', name: 'read_paper' },
    { kind: 'tool_result', name: 'read_paper', preview: '文本' },
    { kind: 'error', text: '连接失败' },
    { kind: 'refuse', text: '没有可用证据' },
    { kind: 'cancelled', text: '已停止' },
    { kind: 'custom_provider_event', text: '服务商结果' },
  ]
  const copy = structuredClone(events)
  assert.deepEqual(processEvents(events), events.slice(2))
  assert.equal(publicProcessEntries(events).length, 1)
  assert.deepEqual(events, copy)
  assert.equal(processSummary(events), '1 次工具调用')
})

test('configuration, lifecycle telemetry and inapplicable citation checks create no completed panel', () => {
  const events = [
    { kind: 'runtime_config', text: 'model-A' },
    { kind: 'execution_mode', text: 'execute' },
    { kind: 'status', text: '准备回答' },
    { kind: 'paper_scope', text: '文献范围' },
    { kind: 'step', text: '第1轮思考' },
    { kind: 'critic', gate: { status: 'not_applicable' } },
    { kind: 'audit', audit: { status: 'not_applicable' } },
    { kind: 'answer', answer: '你好，有什么可以帮你？' },
    { kind: 'done' },
    { kind: 'stream_end' },
    { kind: 'task_outcome', outcome: 'completed', completed: true },
  ]
  assert.deepEqual(processEvents(events), [])
  assert.equal(processSummary(events), '')
  assert.equal(processPresentation(turn({ events })).visible, false)
})

test('pending plain reply shows one compact non-expandable status and disappears when complete', () => {
  const pending = turn({ streaming: true, events: [{ kind: 'status', text: '连接中' }] })
  assert.deepEqual(processPresentation(pending), {
    active: true,
    expandable: false,
    visible: true,
    label: '正在处理…',
    summary: '',
  })
  pending.streaming = false
  assert.equal(processPresentation(pending).visible, false)
})

test('real thoughts, a real plan, or retained details make a single expandable disclosure', () => {
  for (const fields of [
    { liveThinking: '分析当前问题' },
    { thinkingParts: ['保留的思考'] },
    { todo: [{ status: 'in_progress' }] },
    { subagents: [{ topic: '主题' }] },
    { historyTruncated: true },
  ]) {
    const current = turn(fields)
    const view = processPresentation(current)
    assert.equal(view.visible, true)
    assert.equal(view.expandable, true)
    assert.ok(['思考过程', '过程记录'].includes(view.label))
    assert.equal(current.processExpanded, undefined)
  }
})

test('summary uses actual plan progress and tool invocations without invented stages or round counts', () => {
  const current = turn({
    streaming: true,
    liveThinking: '比较证据',
    todo: [{ status: 'done' }, { status: 'in_progress' }],
    events: [
      { kind: 'step', text: '第99轮' },
      { kind: 'tool_call' },
      { kind: 'tool_result' },
      { kind: 'tool_call' },
    ],
  })
  const view = processPresentation(current)
  assert.equal(view.label, '思考中…')
  assert.equal(view.summary, '1/2 项步骤 · 2 次工具调用')
})

test('approvals and interrupted outcomes never claim successful completion', () => {
  for (const [fields, label] of [
    [{ approvals: [{}], streaming: true }, '等待批准'],
    [{ cancelled: true }, '已停止'],
    [{ interruptedStatus: 'failed' }, '处理失败'],
    [{ interruptedStatus: 'paused' }, '已暂停'],
    [{ interruptedStatus: 'interrupted' }, '连接中断'],
    [{ taskOutcome: 'partial' }, '部分完成'],
    [{ taskOutcome: 'blocked' }, '任务受阻'],
  ])
    assert.equal(processPresentation(turn({ thinkingParts: ['实际过程'], ...fields })).label, label)
})

test('background-running state keeps its compact activity indicator after detachment', () => {
  assert.equal(processPresentation(turn({ interruptedStatus: 'running_elsewhere' })).active, true)
  assert.equal(processPresentation(turn({ interruptedStatus: 'finishing' })).label, '正在处理…')
})

test('only an explicit saved completion outcome claims completion in retained process state', () => {
  assert.equal(processPresentation(turn({ thinkingParts: ['旧过程'] })).label, '思考过程')
  assert.equal(processPresentation(turn({ todo: [{ status: 'done' }] })).label, '过程记录')
  assert.equal(
    processPresentation(turn({ thinkingParts: ['实际过程'], taskOutcome: 'completed' })).label,
    '已完成'
  )
})

test('public model preambles and reasoning retain their different meanings, including repeated text', () => {
  const parts = processThinkingParts(
    ['推理内容', '我将查阅这份材料', '相同文本', '相同文本'],
    '继续思考',
    [
      { kind: 'thinking', text: '推理内容' },
      { kind: 'thinking', text: '我将查阅这份材料', preamble: true },
      { kind: 'thinking', text: '相同文本' },
      { kind: 'thinking', text: '相同文本', preamble: true },
    ]
  )
  assert.deepEqual(
    parts.map((part) => part.kind),
    ['thinking', 'thinking', 'thinking']
  )
  assert.equal(parts[1].text, '相同文本')
  assert.equal(parts.at(-1).text, '继续思考')
})

test('dedicated thought, plan and approval sections are not repeated in the tool timeline', () => {
  assert.deepEqual(
    processEvents([
      { kind: 'thinking', text: '真实思考' },
      { kind: 'plan', steps: [] },
      { kind: 'todo_update', steps: [] },
      { kind: 'subagents' },
      { kind: 'approval_request' },
      { kind: 'approval_result' },
      { kind: 'tool_result', preview: '保留结果' },
    ]),
    []
  )
})

test('public commentary and real tool actions preserve the received order with results attached once', () => {
  const events = [
    { kind: 'runtime_config' },
    { kind: 'commentary', seq: 2, text: '我先检查实际材料。' },
    { kind: 'tool_call', seq: 3, name: 'read_file', arguments: { path: 'research.md' } },
    { kind: 'tool_result', seq: 4, name: 'read_file', preview: '找到实际差异' },
    { kind: 'thinking', seq: 5, text: '原始reasoning' },
    { kind: 'commentary', seq: 6, text: '这里有一处差异，我会再检查引用。' },
    { kind: 'tool_call', seq: 7, name: 'retrieve_evidence', arguments: { query: '实际目标' } },
    { kind: 'tool_result', seq: 8, name: 'retrieve_evidence', preview: '原文' },
    { kind: 'answer', seq: 9, answer: '最终答复' },
  ]
  const saved = structuredClone(events)
  const entries = publicProcessEntries(events)
  assert.deepEqual(
    entries.map((entry) => entry.type),
    ['commentary', 'tool', 'commentary', 'tool']
  )
  assert.deepEqual(
    entries.map((entry) => entry.event.seq),
    [2, 3, 6, 7]
  )
  assert.equal(entries[1].result.seq, 4)
  assert.equal(entries[3].result.seq, 8)
  assert.deepEqual(events, saved)
  assert.equal(processPresentation(turn({ streaming: true, events: [events[2]] })).visible, false)
})

test('legacy preambles remain public commentary, absent from both legacy and normalized thinking parts', () => {
  const events = [
    { kind: 'thinking', text: '相同文本' },
    { kind: 'thinking', text: '公开前导', preamble: true },
    { kind: 'thinking', text: '相同文本', preamble: true },
    { kind: 'tool_call', name: 'read_file' },
  ]
  const oldParts = processThinkingParts(['相同文本', '公开前导', '相同文本'], '', events)
  const normalizedParts = processThinkingParts(['相同文本'], '', events)
  assert.deepEqual(oldParts, [{ text: '相同文本', kind: 'thinking' }])
  assert.deepEqual(normalizedParts, oldParts)
  assert.deepEqual(
    publicProcessEntries(events).map((entry) => entry.type),
    ['commentary', 'commentary', 'tool']
  )
})

test('adjacent legacy and new encodings of the same commentary render once, repeated later updates survive', () => {
  const events = [
    { kind: 'thinking', preamble: true, text: '检查材料' },
    { kind: 'status', text: '系统遥测' },
    { kind: 'commentary', text: '检查材料' },
    { kind: 'tool_call', name: 'read_file' },
    { kind: 'tool_result', name: 'read_file' },
    { kind: 'commentary', text: '检查材料' },
  ]
  assert.deepEqual(
    publicProcessEntries(events).map((entry) => entry.type),
    ['commentary', 'tool', 'commentary']
  )
})

test('truncated histories still show an unpaired result and tool arguments use only actual supplied values', () => {
  const events = [{ kind: 'tool_result', name: 'read_file', preview: '留存的返回值' }]
  const entries = publicProcessEntries(events)
  assert.equal(entries.length, 1)
  assert.equal(entries[0].result, events[0])
  assert.equal(
    publicToolArgument({ kind: 'tool_call', arguments: { query: '实际查询' } }),
    '实际查询'
  )
  assert.equal(publicToolArgument({ kind: 'tool_call', arguments: { count: 99 } }), '')
  assert.equal(
    publicToolArgument({ kind: 'tool_call', arguments: { command: 'a'.repeat(250) } }).length,
    180
  )
})
