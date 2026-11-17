// Controlled streaming checks for the Windows desktop chat workflow.
const assert = require('node:assert/strict')
const path = require('node:path')
const { createHash } = require('node:crypto')
const stateMock = require('./desktop-state-mock.cjs')()
const fs = require('node:fs/promises')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'
const output = path.resolve(
  process.env.SCHOLARFLOW_TEST_OUTPUT || path.join(__dirname, '../../verification')
)
const conversations = [
  { id: 'c_a', title: 'Conversation A', message_count: 1 },
  { id: 'c_b', title: 'Conversation B', message_count: 1 },
]
const longAnswer = Array.from(
  { length: 36 },
  (_, i) => `第 ${i + 1} 段研究记录。阅读历史消息时，新输出应保留当前位置。`
).join('\n\n')

async function main() {
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = [],
    cancellations = []
  const foregroundRuns = new Map()
  const inputQueues = new Map()
  let modelStatusScenario = 'named'
  const namedProfile = { id: 'controlled_named_api', name: 'Independent API',
    base_url: 'https://example.test/v1', model_id: 'CaseSensitive-Selected', reasoning_effort: '',
    enabled: true, version: 1, api_key_set: true,
    capabilities: { text: 'unknown', streaming: 'unknown', tools: 'unknown', vision: 'unknown', reasoning: 'unknown' } }
  let loseInputAcknowledgement = false
  const completedMessages = new Map()
  const checkpointPatches = []
  const remoteRunId = 'chat_remote_12345678'
  foregroundRuns.set(remoteRunId, {
    id: remoteRunId,
    conversation_id: 'c_b',
    revision: 1,
    question: 'Run active in another desktop window',
    paper_id: null,
    skills: [],
    images: [],
    partial_answer: 'Shared partial output.',
    phase: 'answer',
    status: 'running',
    resume_available: false,
    error: '',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    approvals: [
      {
        approval_id: 'remote_approval',
        tool_name: 'write_file',
        arguments: { path: 'report.md' },
        risk: 'guarded',
        reason: 'Save reviewed report',
      },
    ],
    runtime_config: { profile_id: 'frozen_other_profile', model_id: 'Frozen-Current', mode: 'api' },
  })
  let foregroundCreates = 0
  let foregroundCreateStatus = 200
  let cancelStatus = 200
  let holdForegroundCreate = false,
    releaseForegroundCreate
  let releaseApproval,
    approvalDecisions = 0
  page.on('pageerror', (e) => errors.push(e.message))
  await page.addInitScript(() => {
    localStorage.setItem('scholarflow_conversation_id', 'c_a')
    window.EventSource = class extends EventTarget {
      close() {}
    }
    window.chatStreams = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, options) => {
      if (String(input) !== '/api/chat/stream') return originalFetch(input, options)
      const request = JSON.parse(options.body)
      const stream = new ReadableStream({
        start(controller) {
          const entry = { request, controller, closed: false }
          window.chatStreams.push(entry)
          options.signal?.addEventListener(
            'abort',
            () => {
              entry.closed = true
              controller.error(new DOMException('Aborted', 'AbortError'))
            },
            { once: true }
          )
        },
      })
      if (window.holdChatResponse)
        await new Promise((resolve) => {
          window.releaseChatResponse = resolve
        })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
    window.feedChat = (index, events, close = false) => {
      const entry = window.chatStreams[index]
      if (entry.closed) return
      for (const event of events) {
        entry.controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      }
      if (close) {
        entry.closed = true
        entry.controller.close()
      }
    }
  })
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const request = route.request()
    const url = new URL(request.url())
    const json = (body, status = 200) => route.fulfill({ status, json: body })
    if (url.pathname === '/api/model-profiles') return json({ profiles: [namedProfile], default_profile_id: namedProfile.id })
    if (url.pathname.startsWith('/api/conversation-options/')) return json({
      revision: 1, model_profile_id: modelStatusScenario === 'legacy-error' ? 'legacy-default' : namedProfile.id,
      approval_mode: 'auto', reasoning_effort: null,
      effective_model: modelStatusScenario === 'legacy-error'
        ? { ...namedProfile, id: 'legacy-default' } : namedProfile,
      model_error: '',
    })
    if (url.pathname === '/api/chat/runs/inputs') return json({
      conversation_id: url.searchParams.get('conversation_id'),
      queues: [...inputQueues.values()].filter((queue) =>
        queue.conversation_id === url.searchParams.get('conversation_id')),
    })
    const inbox = url.pathname.match(/^\/api\/chat\/runs\/([^/]+)\/inputs(?:\/([^/]+))?$/)
    if (inbox) {
      const run = foregroundRuns.get(inbox[1])
      const payload = request.method() === 'POST' ? request.postDataJSON() : null
      const cid = payload?.conversation_id || url.searchParams.get('conversation_id')
      if (!run || run.conversation_id !== cid) return json({ detail: 'wrong conversation' }, 404)
      const queue = inputQueues.get(run.id) || {
        run_id: run.id,
        conversation_id: cid,
        revision: 0,
        inputs: [],
        next_turn_dispatch: 'automatic',
      }
      inputQueues.set(run.id, queue)
      if (request.method() === 'GET') return json(queue)
      if (request.method() === 'POST') {
        const existing = queue.inputs.find((item) => item.input_id === payload.input_id)
        if (existing) return json({ ...queue, input: existing })
        if (payload.expected_revision !== queue.revision)
          return json({ detail: 'queue revision changed' }, 409)
        const item = {
          ...payload,
          run_id: run.id,
          status: 'queued',
          revision: ++queue.revision,
          goal_revision: null,
          reason: '',
          created_at: new Date().toISOString(),
        }
        queue.inputs.push(item)
        if (loseInputAcknowledgement) {
          loseInputAcknowledgement = false
          return json({ detail: '接收回执丢失，输入已保留' }, 500)
        }
        return json({ ...queue, input: item })
      }
      const item = queue.inputs.find((entry) => entry.input_id === inbox[2])
      if (!item || item.status !== 'queued') return json({ detail: 'cannot cancel' }, 409)
      if (Number(url.searchParams.get('expected_revision')) !== queue.revision)
        return json({ detail: 'queue revision changed' }, 409)
      item.status = 'cancelled'
      item.revision = ++queue.revision
      return json({ ...queue, input: item })
    }
    const cancel = url.pathname.match(/^\/api\/chat\/runs\/([^/]+)\/cancel$/)
    if (cancel) {
      cancellations.push({ id: cancel[1], method: request.method() })
      if (cancelStatus === 200 && foregroundRuns.has(cancel[1])) {
        const run = foregroundRuns.get(cancel[1])
        run.status = 'cancelled'
        run.revision += 1
      }
      return json({ status: 'cancelling' }, cancelStatus)
    }
    if (url.pathname === '/api/approvals/decide') {
      approvalDecisions++
      const body = request.postDataJSON()
      foregroundRuns.get(remoteRunId).approvals = []
      if (body.approval_id === 'expired_remote_approval')
        return json({ detail: 'Already resolved elsewhere' }, 404)
      await new Promise((resolve) => {
        releaseApproval = resolve
      })
      return json(body)
    }
    if (url.pathname === '/api/foreground-runs') {
      if (request.method() === 'GET') {
        const conversationId = url.searchParams.get('conversation_id')
        return json({
          runs: [...foregroundRuns.values()].filter(
            (run) =>
              run.conversation_id === conversationId &&
              !['completed', 'dismissed'].includes(run.status)
          ),
        })
      }
      if (request.method() === 'POST') {
        if (foregroundCreateStatus !== 200)
          return json({ detail: '检查点创建失败' }, foregroundCreateStatus)
        const body = request.postDataJSON()
        const now = new Date().toISOString()
        const run = {
          ...body,
          revision: 1,
          partial_answer: '',
          phase: 'setup',
          status: 'running',
          resume_available: false,
          error: '',
          created_at: now,
          updated_at: now,
          runtime_config: { profile_id: namedProfile.id, model_id: namedProfile.model_id, mode: 'api' },
        }
        foregroundRuns.set(run.id, run)
        foregroundCreates += 1
        if (holdForegroundCreate)
          await new Promise((resolve) => {
            releaseForegroundCreate = resolve
          })
        return json({ id: run.id, revision: 1, status: 'running', updated_at: now, runtime_config: run.runtime_config })
      }
    }
    const foregroundPatch = url.pathname.match(/^\/api\/foreground-runs\/([^/]+)$/)
    if (foregroundPatch && request.method() === 'PATCH') {
      checkpointPatches.push({ id: foregroundPatch[1], body: request.postDataJSON() })
      const run = foregroundRuns.get(foregroundPatch[1])
      if (!run) return json({ detail: 'missing run' }, 404)
      const body = request.postDataJSON()
      if (run.status !== 'running' || body.revision !== run.revision) {
        return route.fulfill({
          status: 409,
          json: { detail: { message: 'conflict', current: run } },
        })
      }
      Object.assign(run, body, { revision: run.revision + 1, updated_at: new Date().toISOString() })
      return json(run)
    }
    if (foregroundPatch && request.method() === 'DELETE') {
      const run = foregroundRuns.get(foregroundPatch[1])
      if (run) {
        run.status = 'dismissed'
        run.revision += 1
      }
      return json({ dismissed: foregroundPatch[1] })
    }
    if (url.pathname === '/api/conversations') return json({ conversations })
    if (/^\/api\/conversations\/c_[ab]\/project$/.test(url.pathname))
      return json({ project: null, workspace_revision: 0 })
    if (url.pathname === '/api/projects') return json([])
    if (url.pathname === '/api/conversations/page')
      return json({
        conversations,
        total: conversations.length,
        pinned_total: 0,
        next_cursor: null,
      })
    if (url.pathname.endsWith('/activity') && url.pathname.startsWith('/api/conversations/')) {
      const item = conversations.find((c) => c.id === url.pathname.split('/')[3])
      return json(item || { detail: 'Not found' }, item ? 200 : 404)
    }
    if (/^\/api\/conversations\/c_[ab]$/.test(url.pathname)) {
      const id = url.pathname.split('/').pop()
      return json({
        id,
        title: id,
        messages: [
          {
            id: `qa_${id}`,
            question: `Question ${id}`,
            answer: id === 'c_a' ? longAnswer : 'Answer B',
            evidence: [],
            skills: [],
            refusal: 0,
            latency_ms: 1,
          },
          ...(completedMessages.get(id) || []),
        ],
      })
    }
    if (url.pathname === '/api/tasks') return json({ tasks: [] })
    if (url.pathname === '/api/papers') return json([])
    if (url.pathname === '/api/skills') return json({ skills: [] })
    if (url.pathname === '/api/mcp/servers') return json({ servers: [] })
    if (url.pathname === '/api/settings')
      return json({ approval_mode: 'auto', reasoning_effort: 'medium', memory_recall: true,
        llm_api_key_set: modelStatusScenario === 'legacy-error', llm_model: 'Legacy-Default',
        credential_errors: modelStatusScenario === 'legacy-error' ? { llm_api_key: 'Protected credential unavailable' } : {} })
    if (url.pathname.startsWith('/api/goal')) return json({ current: null, goals: [] })
    return json({})
  })
  const waitFor = async (predicate, label) => {
    const start = Date.now()
    while (!(await predicate())) {
      assert.ok(Date.now() - start < 8000, `Timed out: ${label}`)
      await page.waitForTimeout(40)
    }
  }
  const composer = page.locator('textarea').first()
  const switchTo = async (title) => {
    await page.locator('.recent-conversation').filter({ hasText: title }).click()
    await page
      .getByText(title === 'Conversation A' ? 'Question c_a' : 'Question c_b', { exact: true })
      .waitFor()
    await waitFor(() => composer.isEnabled(), 'conversation restored')
  }
  const command = async (text) => {
    await composer.fill(text)
    await composer.press('Enter')
  }
  const feed = async (index, events, close = false) => {
    // The server owns partial output even after a browser observer disconnects.
    const runId = await page.evaluate((i) => window.chatStreams[i]?.request.run_id, index)
    const saved = foregroundRuns.get(runId)
    if (saved) {
      for (const event of events) {
        if (event.kind === 'answer_delta') saved.partial_answer += event.text
        if (event.kind === 'answer') saved.partial_answer = event.answer
        if (event.kind === 'cancelled') saved.status = 'cancelled'
        if (event.kind === 'paused') {
          saved.status = 'paused'
          saved.resume_available = true
        }
      }
      saved.revision += 1
    }
    await page.evaluate(({ index, events, close }) => window.feedChat(index, events, close), {
      index,
      events,
      close,
    })
  }
  const completeDetached = (runId, answer) => {
    const run = foregroundRuns.get(runId)
    run.status = 'completed'
    run.partial_answer = answer
    run.revision += 1
    const messages = completedMessages.get(run.conversation_id) || []
    messages.push({
      id: `qa_${runId}`,
      question: run.question,
      answer,
      evidence: [],
      skills: [],
      refusal: 0,
      latency_ms: 1,
      created_at: new Date().toISOString(),
    })
    completedMessages.set(run.conversation_id, messages)
  }
  const bottomDistance = () =>
    page.locator('.chat-scroll').evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)
  try {
    await page.goto(`${baseURL}/chat?conv=c_a`)
    await page.getByText('Question c_a', { exact: true }).waitFor()
    await page.locator('.model-mark').filter({ hasText: '使用接口模型' }).waitFor()
    assert.match(await page.locator('.model-mark').getAttribute('title'), /当前会话选择：CaseSensitive-Selected/)
    await composer.fill('Draft A survives switching and reload')
    await switchTo('Conversation B')
    assert.match(await page.locator('.model-mark').getAttribute('title'), /本轮绑定：Frozen-Current/)
    assert.equal(await composer.inputValue(), '')
    await composer.fill('Draft B belongs to B')
    await page.getByText('本轮仍在后台运行，进度和结果会自动同步。', { exact: true }).waitFor()
    assert.equal(
      await page.getByRole('button', { name: '重新提问', exact: true }).count(),
      0,
      'active remote run has no retry or remove controls'
    )
    assert.equal(
      await page.getByRole('button', { name: '发送', exact: true }).count(),
      0,
      'active remote run blocks a second foreground submission'
    )
    assert.equal(
      await page.getByText('生成已结束', { exact: true }).count(),
      0,
      'a remote live run is not labelled finished'
    )
    assert.equal(await page.getByLabel('运行中输入用途', { exact: true }).inputValue(), 'steer')
    await composer.fill('改用B，保留已完成的表格')
    const supplement = page.getByRole('button', { name: '发送补充', exact: true })
    await supplement.click()
    await page.getByText('已接收，等待安全边界生效', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), '')
    assert.equal(foregroundCreates, 0, 'steering does not admit another foreground execution')
    const inbox = inputQueues.get(remoteRunId)
    inbox.inputs[0].status = 'applied'
    inbox.inputs[0].goal_revision = 3
    inbox.inputs[0].revision = ++inbox.revision
    await page.getByText('已生效 · 目标修订 3', { exact: true }).waitFor()
    await page.getByLabel('运行中输入用途', { exact: true }).selectOption('next_turn')
    await composer.fill('下一轮比较两篇论文')
    await composer.press('Enter')
    await page.getByText('已排队；前一轮完整完成后自动开始', { exact: true }).waitFor()
    assert.equal(foregroundCreates, 0, 'queued next turn never claims a new execution started')
    await page.reload()
    await page.getByText('已生效 · 目标修订 3', { exact: true }).waitFor()
    await page.getByText('已排队；前一轮完整完成后自动开始', { exact: true }).waitFor()
    await page
      .getByRole('button', { name: '撤销排队消息：下一轮比较两篇论文', exact: true })
      .click()
    await page.getByText('已撤销', { exact: true }).waitFor()
    await composer.fill('回执丢失也只接收一次')
    loseInputAcknowledgement = true
    await page.getByRole('button', { name: '发送补充', exact: true }).click()
    await page.getByText('接收回执丢失，输入已保留', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), '回执丢失也只接收一次')
    const receivedId = inbox.inputs.at(-1).input_id
    await page.getByRole('button', { name: '发送补充', exact: true }).click()
    await waitFor(
      async () => (await composer.inputValue()) === '',
      'idempotent input retry acknowledged'
    )
    assert.equal(inbox.inputs.filter((item) => item.input_id === receivedId).length, 1)
    assert.equal(inbox.inputs.length, 3)
    await composer.fill('Draft B belongs to B')
    await page.locator('.approval-arguments').filter({ hasText: 'report.md' }).waitFor()
    await page.screenshot({
      path: path.join(output, 'foreground-approval-desktop.png'),
      fullPage: true,
    })
    await page.setViewportSize({ width: 1100, height: 700 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({
      path: path.join(output, 'foreground-approval-small-desktop.png'),
      fullPage: true,
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    const approve = page.getByRole('button', { name: '批准一次', exact: true })
    await approve.click()
    await waitFor(() => releaseApproval, 'approval decision request held')
    assert.equal(await approve.isDisabled(), true)
    assert.equal(await page.getByRole('button', { name: '拒绝', exact: true }).isDisabled(), true)
    await approve.evaluate((button) => button.click())
    assert.equal(approvalDecisions, 1, 'remote approval is submitted only once')
    releaseApproval()
    releaseApproval = undefined
    await waitFor(
      async () => (await page.locator('.approval-card').count()) === 0,
      'remote approval acknowledged'
    )
    foregroundRuns.get(remoteRunId).approvals = [
      {
        approval_id: 'expired_remote_approval',
        tool_name: 'write_file',
        arguments: {},
        risk: 'guarded',
        reason: 'Expired approval',
      },
    ]
    await page.getByText('Expired approval', { exact: true }).waitFor()
    await page.getByRole('button', { name: '批准一次', exact: true }).click()
    await page.getByText('该审批已结束', { exact: true }).waitFor()
    assert.equal(await page.locator('.approval-card').count(), 0)
    foregroundRuns.get(remoteRunId).status = 'completed'
    await switchTo('Conversation A')
    assert.equal(await composer.inputValue(), 'Draft A survives switching and reload')
    await page.reload()
    await page.getByText('Question c_a', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), 'Draft A survives switching and reload')
    await waitFor(async () => (await bottomDistance()) <= 64, 'restored chat follows bottom')

    await command('Start a cancellable turn')
    await waitFor(() => page.evaluate(() => window.chatStreams.length === 1), 'stream started')
    const firstId = await page.evaluate(() => window.chatStreams[0].request.run_id)
    assert.match(firstId, /^chat_[\w-]+$/)
    assert.ok(foregroundRuns.has(firstId), 'a durable checkpoint exists before streaming starts')
    assert.equal(foregroundCreates, 1)
    assert.equal(
      await page.evaluate(() => localStorage.getItem('scholarflow_draft:c_a')),
      null,
      'sent draft removed'
    )
    await feed(0, [{ kind: 'answer_delta', text: 'Partial answer before stopping.\n\n' }])
    await page.getByText('Partial answer before stopping.', { exact: true }).waitFor()
    await waitFor(
      () => foregroundRuns.get(firstId)?.partial_answer.includes('Partial answer before stopping.'),
      'partial answer checkpoint saved'
    )
    await waitFor(async () => (await bottomDistance()) <= 64, 'new content follows bottom')
    await page.locator('.chat-scroll').evaluate((el) => {
      el.scrollTop = 100
      el.dispatchEvent(new Event('scroll'))
    })
    await page.getByRole('button', { name: '回到最新消息', exact: true }).waitFor()
    const readingPosition = await page.locator('.chat-scroll').evaluate((el) => el.scrollTop)
    await feed(0, [{ kind: 'answer_delta', text: longAnswer }])
    await page.waitForTimeout(120)
    assert.ok(
      Math.abs(
        (await page.locator('.chat-scroll').evaluate((el) => el.scrollTop)) - readingPosition
      ) < 5,
      'streaming preserves reading position'
    )
    await page.screenshot({ path: path.join(output, 'chat-reading-desktop.png'), fullPage: true })
    await page.getByRole('button', { name: '回到最新消息', exact: true }).click()
    await waitFor(async () => (await bottomDistance()) <= 64, 'jump to latest')

    cancelStatus = 500
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    await page.getByText('停止请求失败：500', { exact: true }).waitFor()
    await waitFor(
      () => page.getByRole('button', { name: '停止生成', exact: true }).isEnabled(),
      'stop can retry after network failure'
    )
    cancelStatus = 200
    await command('/help')
    await composer.fill('Next draft remains after stopping')
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    await waitFor(() => cancellations.length === 2, 'server stop requested')
    assert.deepEqual(cancellations, [
      { id: firstId, method: 'POST' },
      { id: firstId, method: 'POST' },
    ])
    assert.equal(
      await page.getByRole('button', { name: '停止生成', exact: true }).isDisabled(),
      true
    )
    await page.getByText('正在停止…', { exact: true }).waitFor()
    await feed(0, [{ kind: 'answer_delta', text: 'Late output must be ignored' }])
    assert.equal(await page.getByText('Late output must be ignored').count(), 0)
    await feed(0, [{ kind: 'cancelled', text: 'Stopped by user' }], true)
    await page.getByText('已停止', { exact: true }).waitFor()
    await page.getByRole('button', { name: '重新提问', exact: true }).waitFor()
    await page.locator('.el-message-box__title').filter({ hasText: '斜杠命令' }).waitFor()
    await page.getByRole('button', { name: '关闭', exact: true }).click()
    assert.equal(await composer.inputValue(), 'Next draft remains after stopping')
    assert.match(await page.locator('.markdown-answer').last().textContent(), /Partial answer/)
    await page.getByRole('button', { name: '重新提问', exact: true }).click()
    await page
      .locator('.el-message-box')
      .getByText('重新提问会重新执行本轮操作。若上次已调用外部写入工具，可能造成重复操作。', {
        exact: true,
      })
      .waitFor()
    await page.locator('.el-message-box').getByRole('button', { name: '取消', exact: true }).click()
    assert.equal(
      foregroundCreates,
      1,
      'canceling the duplicate-operation warning does not resubmit'
    )
    await page.reload()
    await page.getByText('Question c_a', { exact: true }).waitFor()
    await page.getByText('本轮已停止，已保留部分回答。', { exact: true }).waitFor()
    await page.getByRole('button', { name: '重新提问', exact: true }).waitFor()
    assert.match(
      await page.locator('.markdown-answer').last().textContent(),
      /Partial answer before stopping/
    )
    const recoveryBar = page.locator('.foreground-recovery')
    await recoveryBar.scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'foreground-recovery-desktop.png') })
    await page.setViewportSize({ width: 1100, height: 700 })
    await recoveryBar.scrollIntoViewIfNeeded()
    const recoveryBounds = await recoveryBar.boundingBox()
    assert.ok(
      recoveryBounds && recoveryBounds.x >= 0 && recoveryBounds.x + recoveryBounds.width <= 1100
    )
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: path.join(output, 'foreground-recovery-small-desktop.png') })
    await page.setViewportSize({ width: 1440, height: 900 })

    await command('Second turn continues after switching conversations')
    await waitFor(
      () => page.evaluate(() => window.chatStreams.length === 1),
      'second stream started after reload'
    )
    const secondId = await page.evaluate(() => window.chatStreams[0].request.run_id)
    assert.notEqual(firstId, secondId)
    await feed(0, [{ kind: 'answer_delta', text: 'A keeps its partial output before switching.' }])
    await composer.fill('New draft in A')
    await switchTo('Conversation B')
    assert.equal(
      cancellations.some((entry) => entry.id === secondId),
      false
    )
    assert.equal(foregroundRuns.get(secondId).status, 'running')
    assert.equal(await composer.inputValue(), 'Draft B belongs to B')
    assert.equal(
      await page.evaluate(() => window.chatStreams[0].closed),
      true,
      'old browser stream aborted'
    )

    await command('Independent task in B while A continues')
    await waitFor(
      () => page.evaluate(() => window.chatStreams.length === 2),
      'B starts independently'
    )
    const independentId = await page.evaluate(() => window.chatStreams[1].request.run_id)
    assert.equal(foregroundRuns.get(independentId).conversation_id, 'c_b')
    await page.locator('button.research-nav').filter({ hasText: '文献库' }).click()
    await page.waitForURL('**/library')
    assert.equal(
      await page.evaluate(() => window.chatStreams[1].closed),
      true,
      'module navigation detaches local B stream'
    )
    assert.equal(
      cancellations.some((entry) => entry.id === independentId),
      false
    )
    assert.equal(foregroundRuns.get(independentId).status, 'running')
    await page.goto(`${baseURL}/chat?conv=c_a`)
    await page.getByText('Question c_a', { exact: true }).waitFor()
    foregroundRuns.get(secondId).partial_answer = 'A continued on the server while B was open.'
    foregroundRuns.get(secondId).revision += 1

    await page.setViewportSize({ width: 1100, height: 700 })
    await switchTo('Conversation A')
    await page.getByText('A continued on the server while B was open.', { exact: true }).waitFor()
    assert.equal(
      cancellations.some((entry) => [secondId, independentId].includes(entry.id)),
      false
    )
    assert.equal(await composer.inputValue(), 'New draft in A')

    await page.locator('button.research-nav').filter({ hasText: '文献库' }).click()
    await page.waitForURL('**/library')
    assert.equal(foregroundRuns.get(secondId).status, 'running', 'leaving chat does not stop A')
    assert.equal(
      cancellations.some((entry) => entry.id === secondId),
      false
    )
    await page.goto(`${baseURL}/chat?conv=c_a`)
    await page.getByText('A continued on the server while B was open.', { exact: true }).waitFor()
    foregroundRuns.get(secondId).partial_answer =
      'A latest progress after returning from another module.'
    foregroundRuns.get(secondId).revision += 1
    await page
      .getByText('A latest progress after returning from another module.', { exact: true })
      .waitFor()
    completeDetached(secondId, 'Detached task A completed exactly once.')
    await page.getByText('Detached task A completed exactly once.', { exact: true }).waitFor()
    assert.equal(
      await page.getByText('Detached task A completed exactly once.', { exact: true }).count(),
      1
    )
    assert.equal(
      await page.getByText('Independent task in B while A continues', { exact: true }).count(),
      0
    )

    await switchTo('Conversation B')
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    await waitFor(
      () => cancellations.some((entry) => entry.id === independentId),
      'explicitly stop detached B'
    )
    assert.equal(
      cancellations.some((entry) => entry.id === secondId),
      false
    )
    await page.getByText('本轮已停止，已保留部分回答。', { exact: true }).waitFor()
    await switchTo('Conversation A')
    await page.locator('.chat-scroll').evaluate((el) => {
      el.scrollTop = 100
      el.dispatchEvent(new Event('scroll'))
    })
    const jump = page.getByRole('button', { name: '回到最新消息', exact: true })
    await jump.waitFor()
    const bounds = await jump.boundingBox()
    assert.ok(
      bounds && bounds.x > 0 && bounds.x + bounds.width <= 1100 && bounds.y + bounds.height < 700
    )
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await waitFor(
      async () => (await page.locator('.el-message').count()) === 0,
      'temporary messages dismissed'
    )
    await page.screenshot({
      path: path.join(output, 'chat-reading-small-desktop.png'),
      fullPage: true,
    })
    const createsBeforeFailure = foregroundCreates
    foregroundCreateStatus = 500
    await composer.fill('Keep this draft when checkpoint creation fails')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await page.getByText('检查点创建失败', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), 'Keep this draft when checkpoint creation fails')
    assert.equal(
      foregroundCreates,
      createsBeforeFailure,
      'failed checkpoint creation starts no chat stream'
    )

    foregroundCreateStatus = 200
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await waitFor(
      () => page.evaluate(() => window.chatStreams.length === 1),
      'budgeted stream started'
    )
    const pausedRunId = await page.evaluate(() => window.chatStreams[0].request.run_id)
    await feed(
      0,
      [
        {
          kind: 'answer',
          answer: 'Saved budget summary',
          evidence: [],
          budget_exhausted: true,
          stop_reason: 'step_limit',
        },
        { kind: 'paused', text: '预算已用完，检查点已保存' },
        { kind: 'done' },
      ],
      true
    )
    await page.getByText('预算已用完，检查点已保存；可以继续当前任务。', { exact: true }).waitFor()
    assert.equal(
      foregroundCreates,
      createsBeforeFailure + 1,
      'a fresh question creates exactly one new checkpoint'
    )

    const pausedRun = foregroundRuns.get(pausedRunId)
    pausedRun.status = 'paused'
    pausedRun.phase = 'budget_paused'
    pausedRun.resume_available = true
    pausedRun.partial_answer = 'Saved budget summary'
    pausedRun.revision += 1
    pausedRun.run_revision = 19
    pausedRun.attempt_id = 'attempt_before_resume'
    pausedRun.display_snapshot = {
      kind: 'snapshot', run_id: pausedRunId, conversation_id: pausedRun.conversation_id,
      attempt_id: pausedRun.attempt_id, cursor: 70, partial_answer: '',
      thinking_parts: [], thinking: '', events: [], plan_steps: [], approvals: [],
      phase: 'budget_paused', status: 'paused', terminal: true, error: '',
      result: { kind: 'answer', qa_id: 'qa_partial_saved', answer: 'Saved budget summary',
        completed: false, outcome: 'partial', evidence: [], budget_exhausted: true },
    }
    await page.reload()
    await page.getByText('预算已用完，检查点已保存；可以继续当前任务。', { exact: true }).waitFor()
    assert.match(
      await page.locator('.markdown-answer').last().textContent(),
      /Saved budget summary/
    )
    const continueButton = page.getByRole('button', { name: '继续当前任务（+6轮）', exact: true })
    await continueButton.click()
    await waitFor(
      () => page.evaluate(() => window.chatStreams.length === 1),
      'resume stream started'
    )
    const resumedRequest = await page.evaluate(() => window.chatStreams[0].request)
    assert.equal(resumedRequest.run_id, pausedRunId, 'resume reuses the checkpoint run ID')
    assert.equal(resumedRequest.question, pausedRun.question, 'resume reuses the saved question')
    assert.equal(resumedRequest.resume_checkpoint, true)
    assert.equal(resumedRequest.additional_steps, 6)
    assert.match(resumedRequest.resume_request_id, /^resume_[A-Za-z0-9_-]+$/)
    assert.equal(resumedRequest.expected_run_revision, 19)
    assert.equal(
      foregroundCreates,
      createsBeforeFailure + 1,
      'resume does not create another foreground checkpoint'
    )
    await feed(
      0,
      [
        { kind: 'answer_delta', text: 'Continuation completed.' },
        {
          kind: 'answer',
          answer: 'Continuation completed.',
          evidence: [],
          budget_exhausted: false,
        },
        { kind: 'done' },
      ],
      true
    )
    await page.getByText('Continuation completed.', { exact: true }).waitFor()
    assert.equal(await page.getByText('Saved budget summary', { exact: true }).count(), 1,
      'a new attempt preserves the previously committed partial QA')
    assert.equal(
      await page.getByRole('button', { name: '继续当前任务（+6轮）', exact: true }).count(),
      0,
      'completed continuation removes the paused action'
    )

    // Navigation while checkpoint admission is pending must not abandon the send.
    foregroundRuns.get(pausedRunId).status = 'completed'
    holdForegroundCreate = true
    await command('A starts even when navigation beats checkpoint admission')
    await waitFor(() => releaseForegroundCreate, 'foreground checkpoint response held')
    const pendingRun = [...foregroundRuns.values()].find(
      (run) => run.question === 'A starts even when navigation beats checkpoint admission'
    )
    assert.ok(pendingRun)
    await switchTo('Conversation B')
    holdForegroundCreate = false
    releaseForegroundCreate()
    releaseForegroundCreate = undefined
    await waitFor(
      () =>
        page.evaluate(
          (id) => window.chatStreams.some((entry) => entry.request.run_id === id),
          pendingRun.id
        ),
      'admitted A run starts after navigation'
    )
    assert.equal(
      cancellations.some((entry) => entry.id === pendingRun.id),
      false
    )
    pendingRun.partial_answer = 'A admission race retained the original task.'
    pendingRun.revision += 1
    await switchTo('Conversation A')
    await page.getByText('A admission race retained the original task.', { exact: true }).waitFor()
    completeDetached(pendingRun.id, 'Admission race task completed exactly once.')
    await page.getByText('Admission race task completed exactly once.', { exact: true }).waitFor()
    assert.equal(
      await page.getByText('Admission race task completed exactly once.', { exact: true }).count(),
      1
    )

    await page.evaluate(() => {
      window.holdChatResponse = true
    })
    await command('A starts before the observation response arrives')
    await waitFor(
      () => page.evaluate(() => Boolean(window.releaseChatResponse)),
      'observation response held'
    )
    const responsePending = await page.evaluate(() => window.chatStreams.at(-1).request)
    await switchTo('Conversation B')
    await composer.fill('B draft typed while A response is pending')
    assert.equal(
      await page.evaluate(() => window.chatStreams.at(-1).closed),
      false,
      'pending send is not aborted before server admission'
    )
    await page.evaluate(() => {
      window.holdChatResponse = false
      window.releaseChatResponse()
      window.releaseChatResponse = null
    })
    await waitFor(
      () => page.evaluate(() => window.chatStreams.at(-1).closed),
      'admitted detached observation aborted'
    )
    assert.equal(await composer.inputValue(), 'B draft typed while A response is pending')
    assert.equal(
      cancellations.some((entry) => entry.id === responsePending.run_id),
      false
    )
    const admittedRun = foregroundRuns.get(responsePending.run_id)
    admittedRun.partial_answer = 'A continues after the observation response race.'
    admittedRun.revision += 1
    await switchTo('Conversation A')
    await page
      .getByText('A continues after the observation response race.', { exact: true })
      .waitFor()
    completeDetached(responsePending.run_id, 'Observation response race completed exactly once.')
    await page
      .getByText('Observation response race completed exactly once.', { exact: true })
      .waitFor()
    assert.equal(
      await page
        .getByText('Observation response race completed exactly once.', { exact: true })
        .count(),
      1
    )
    // A server-dispatched child is recovered and located without another POST.
    const childId = 'chat_next_controlled_child_12345678'
    const childRun = { ...admittedRun, id: childId, question: 'Automatically queued follow-up',
      partial_answer: 'The queued child is streaming.', status: 'running', revision: 1,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString() }
    foregroundRuns.set(childId, childRun)
    inputQueues.set(admittedRun.id, { run_id: admittedRun.id, conversation_id: 'c_a', revision: 2,
      next_turn_dispatch: 'automatic', inputs: [{ input_id: 'input_auto_child_123',
        run_id: admittedRun.id, conversation_id: 'c_a', kind: 'next_turn',
        content: childRun.question, status: 'dispatched', child_run_id: childId, revision: 2,
        reason: '', created_at: childRun.created_at }] })
    const createsBeforeChildRecovery = foregroundCreates
    await page.reload()
    await page.getByText('已自动提交下一轮，可查看执行进度', { exact: true }).waitFor()
    await page.getByText('The queued child is streaming.', { exact: true }).waitFor()
    await page.getByRole('button', { name: '查看执行', exact: true }).click()
    await page.getByText('The queued child is streaming.', { exact: true }).waitFor()
    assert.equal(foregroundCreates, createsBeforeChildRecovery,
      'recovering/locating a dispatched child creates no foreground execution')
    await page.goto(`${baseURL}/library`)
    await page.locator('.model-mark').filter({ hasText: '离线演示' }).waitFor()
    assert.match(await page.locator('.model-mark').getAttribute('title'), /全局默认配置/)
    modelStatusScenario = 'legacy-error'
    await page.goto(`${baseURL}/chat?conv=c_b`)
    await page.locator('.model-mark').filter({ hasText: '配置待检查' }).waitFor()
    assert.deepEqual(
      checkpointPatches,
      [],
      'browser observers never overwrite server-owned execution progress'
    )
    assert.deepEqual(errors, [])
    const sourceHashes = {}
    for (const file of ['src/views/ChatView.vue', 'src/api.ts', 'src/chatRunObserver.ts', 'src/chatResume.ts', 'src/modelStatus.ts', 'src/App.vue', 'src/components/TopBar.vue']) {
      sourceHashes[file] = createHash('sha256').update(await fs.readFile(path.join(__dirname, '..', file))).digest('hex')
    }
    await fs.writeFile(path.join(output, 'run-input-interactions.json'), JSON.stringify({
      passed: true, environment: 'real Vue browser with controlled HTTP/SSE; no provider calls',
      url: baseURL, sourceHashes, checks: [
        'active run defaults to steer; received and applied states are distinct',
        'queued next turn explains automatic dispatch, persists across reload and can be cancelled',
        'dispatched child is restored and located without another execution request',
        'resume sends a stable request ID and expected runtime revision; old partial QA is preserved',
        'top bar follows selected profile and frozen runtime; outside chat uses global settings; undecryptable key shows configuration check',
        'lost acknowledgement keeps draft and retries same input ID exactly once',
        'steering and next-turn storage create no additional foreground execution',
        'existing navigation, approvals, stop, draft, concurrency and reading behavior remain covered',
      ],
    }, null, 2))
    console.log(
      'PASS: durable steering/next-turn state, reload, cancellation, idempotent lost-ack retry; navigation without cancellation, concurrent conversations, server-owned progress, final result exactly once, explicit stop, admission races, paused continuation, drafts, approvals, reading position and desktop bounds'
    )
  } catch (error) {
    await page.screenshot({
      path: path.join(output, 'chat-switch-regression-failure.png'),
      fullPage: true,
    })
    console.error('Controlled workflow failure context:', {
      foregroundCreates,
      cancellations,
      streams: await page.evaluate(() =>
        window.chatStreams?.map((entry) => ({ request: entry.request, closed: entry.closed }))
      ),
      visible: (await page.locator('body').innerText()).slice(-3500),
    })
    throw error
  } finally {
    releaseApproval?.()
    releaseForegroundCreate?.()
    await browser.close()
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
