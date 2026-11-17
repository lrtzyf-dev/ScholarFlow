// Real Vue UI with deterministic public SSE fixtures. All HTTP state and text
// are synthetic; no user backend, credentials, model provider or paid API runs.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const crypto = require('node:crypto')
const net = require('node:net')
const http = require('node:http')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const stateMock = require('./desktop-state-mock.cjs')()
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function until(check, label, timeout = 10000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(25)
  }
}

async function freePort() {
  const socket = net.createServer()
  await new Promise((resolve) => socket.listen(0, '127.0.0.1', resolve))
  const port = socket.address().port
  await new Promise((resolve) => socket.close(resolve))
  return port
}

async function main() {
  const output = path.join(root, 'verification/chat-progress')
  await fs.mkdir(output, { recursive: true })
  const sources = [
    'frontend/src/views/ChatView.vue',
    'frontend/src/chatProcess.ts',
    'frontend/src/chatRunObserver.ts',
    'backend/app/agent/execution.py',
    'backend/app/agent/loop.py',
    'backend/app/agent/planner.py',
    'backend/app/agent/checkpoint.py',
    'backend/app/agent/prompts.py',
    'backend/app/task_runner.py',
    'backend/app/chat_events.py',
    'frontend/tests/chat-progress.cjs',
  ]
  const hashes = Object.fromEntries(
    await Promise.all(
      sources.map(async (name) => [
        name,
        crypto
          .createHash('sha256')
          .update(await fs.readFile(path.join(root, name)))
          .digest('hex'),
      ])
    )
  )
  const { createServer } = await import('vite')
  // A reload/unload keepalive may outlive Playwright route interception. Its
  // fallback stays local and cannot reach the user's configured backend.
  const apiFallback = http.createServer((_req, res) => res.writeHead(204).end())
  await new Promise((resolve) => apiFallback.listen(0, '127.0.0.1', resolve))
  const vite = await createServer({
    root: path.join(root, 'frontend'),
    configFile: path.join(root, 'frontend/vite.config.ts'),
    logLevel: 'error',
    server: {
      host: '127.0.0.1',
      port: await freePort(),
      strictPort: true,
      hmr: false,
      proxy: { '/api': { target: `http://127.0.0.1:${apiFallback.address().port}` } },
    },
  })
  await vite.listen()
  const base = `http://127.0.0.1:${vite.httpServer.address().port}`
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.setDefaultTimeout(5000)
  const errors = []
  const decisions = []
  const checks = []
  let syntheticSseRuns = 0
  page.on('pageerror', (error) => errors.push(error.message))
  const conversation = { id: 'c_public_progress_ui', title: '公开进展顺序验证', message_count: 0 }
  const messages = []
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget {
      close() {}
    }
    window.compactStreams = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, options) => {
      if (String(input) !== '/api/chat/stream') return originalFetch(input, options)
      const stream = new ReadableStream({
        start(controller) {
          window.compactStreams.push(controller)
          options.signal?.addEventListener(
            'abort',
            () => controller.error(new DOMException('Aborted', 'AbortError')),
            { once: true }
          )
        },
      })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
    window.feedCompact = (events, close) => {
      const controller = window.compactStreams.at(-1)
      for (const event of events)
        controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      if (close) controller.close()
    }
  })
  await page.route('https://**', (route) => route.abort())
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const request = route.request()
    const url = new URL(request.url())
    const json = (value) => route.fulfill({ json: value })
    if (url.pathname === `/api/conversations/${conversation.id}`)
      return json({ ...conversation, messages })
    if (url.pathname === '/api/conversations/page')
      return json({ conversations: [conversation], total: 1, pinned_total: 0, next_cursor: null })
    if (url.pathname.endsWith('/activity')) return json(conversation)
    if (url.pathname.endsWith('/project')) return json({ project: null, workspace_revision: 0 })
    if (url.pathname === '/api/foreground-runs')
      return json(
        request.method() === 'GET'
          ? { runs: [] }
          : { id: request.postDataJSON().id, revision: 1, status: 'running' }
      )
    if (url.pathname.startsWith('/api/foreground-runs/'))
      return json({ revision: 2, status: 'completed' })
    if (url.pathname.startsWith('/api/conversation-options/'))
      return json({ revision: 1, approval_mode: 'always', effective_model: null })
    if (url.pathname === '/api/model-profiles')
      return json({ profiles: [], default_profile_id: null })
    if (url.pathname === '/api/approvals/decide') {
      decisions.push(request.postDataJSON())
      return json({ status: 'decided', decision: request.postDataJSON().decision })
    }
    if (url.pathname === '/api/tasks') return json({ tasks: [] })
    if (url.pathname === '/api/skills') return json({ skills: [] })
    if (url.pathname === '/api/mcp/servers') return json({ servers: [] })
    if (['/api/papers', '/api/projects'].includes(url.pathname)) return json([])
    if (url.pathname === '/api/settings')
      return json({ approval_mode: 'always', reasoning_effort: 'medium', memory_recall: true })
    if (url.pathname.startsWith('/api/goal')) return json({ current: null, goals: [] })
    return json({})
  })
  const turn = () => page.locator('.turn').last()
  const finalAnswer = () => turn().locator('.answer-final .markdown-answer')
  const entrance = () => turn().locator('.run-process-toggle')
  const details = () => turn().locator('.run-process-details')
  const feed = (events, close = false) =>
    page.evaluate(({ events, close }) => window.feedCompact(events, close), { events, close })
  let currentQuestion = ''
  let currentTrace = []
  const send = async (events) => {
    currentTrace.push(
      ...events.filter((event) => !['thinking_delta', 'answer_delta'].includes(event.kind))
    )
    await feed(events)
  }
  const basic = [
    {
      kind: 'runtime_config',
      text: '本轮模型与权限配置已绑定',
      runtime_config: { model_id: 'Synthetic-Progress' },
    },
    { kind: 'execution_mode', text: '执行模式' },
    { kind: 'status', text: '正在理解当前请求' },
    { kind: 'paper_scope', text: '当前没有指定文献' },
    { kind: 'step', text: '第1轮思考' },
  ]
  async function start(question, events = []) {
    currentQuestion = question
    currentTrace = [...basic, ...events]
    const before = await page.evaluate(() => window.compactStreams.length)
    await page.locator('textarea.textarea').fill(question)
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await page.waitForFunction((count) => window.compactStreams.length > count, before)
    syntheticSseRuns += 1
    await feed(currentTrace)
  }
  async function finish(answer, extra = {}) {
    const id = `qa_compact_${messages.length}`
    const payload = {
      kind: 'answer',
      qa_id: id,
      answer,
      evidence: [],
      refusal: 0,
      skills: [],
      latency_ms: 1,
      outcome: 'completed',
      completed: true,
      plan_steps: [],
      ...extra,
    }
    messages.push({
      id,
      question: currentQuestion,
      answer,
      execution_mode: 'execute',
      evidence: [],
      refusal: 0,
      skills: [],
      latency_ms: 1,
      trace: currentTrace,
      plan_steps: payload.plan_steps,
      ...extra,
    })
    conversation.message_count = messages.length
    await feed([payload, { kind: 'done', text: '完成' }], true)
    await turn().locator('.answer-brand').waitFor()
    await until(
      () => page.getByRole('button', { name: '发送', exact: true }).isVisible(),
      'ready after answer'
    )
  }
  async function assertClosed() {
    assert.equal(await entrance().count(), 1, 'only one process entrance per turn')
    assert.equal(await entrance().getAttribute('aria-expanded'), 'false')
    assert.equal(await details().isVisible(), false)
  }
  const visibleOrder = async () =>
    turn()
      .locator('.public-commentary, .public-tool')
      .evaluateAll((items) =>
        items.map((item) => ({
          kind: item.classList.contains('public-commentary') ? 'commentary' : 'tool',
          text: item.innerText,
        }))
      )
  const countText = (text, token) => text.split(token).length - 1
  const assertPublicOrder = async (comments, toolNames) => {
    const items = await visibleOrder()
    assert.deepEqual(
      items.map((item) => item.kind),
      ['commentary', 'tool', 'commentary', 'tool']
    )
    assert.ok(items[0].text.includes(comments[0]))
    assert.ok(items[1].text.includes(toolNames[0]))
    assert.ok(items[2].text.includes(comments[1]))
    assert.ok(items[3].text.includes(toolNames[1]))
    for (const item of await turn().locator('.public-commentary, .public-tool').all())
      assert.equal(
        await item.isVisible(),
        true,
        'public model progress and actions are visible without opening reasoning'
      )
  }
  const publicComments = ['我先核对可用技能目录。', '目录已确认，接着读取说明。']
  try {
    await page.goto(`${base}/chat?conv=${conversation.id}`)
    await until(() => page.locator('textarea.textarea').isEnabled(), 'composer ready')

    await start('请用一句话解释为什么要设置对照组。')
    await send([{ kind: 'answer_delta', text: '对照组帮助区分实验因素与其他因素的影响。' }])
    await until(
      () => turn().locator('.answer-draft .markdown-answer').isVisible(),
      'direct draft answer'
    )
    assert.equal(
      await turn().locator('.public-progress').count(),
      0,
      'no invented progress for a direct answer'
    )
    await finish('对照组帮助区分实验因素与其他因素的影响。')
    assert.equal(await turn().locator('.public-progress').count(), 0)
    assert.equal(await entrance().count(), 0)
    checks.push('a direct answer has no invented public announcement or completed telemetry panel')
    await page.screenshot({ path: path.join(output, 'direct-answer.png'), fullPage: true })

    await start('请查看可用技能并读取适合此任务的操作说明。', [
      { kind: 'thinking_delta', text: 'RAW_REASONING_ONLY' },
    ])
    await send([
      { kind: 'thinking', text: 'RAW_REASONING_ONLY' },
      { kind: 'answer_delta', text: publicComments[0] },
      { kind: 'commentary', text: publicComments[0], source: 'model', draft_to_commentary: true },
      { kind: 'tool_call', name: 'list_skills', arguments: {} },
      { kind: 'tool_result', name: 'list_skills', preview: '合成技能目录' },
      { kind: 'commentary', text: publicComments[1], source: 'model', draft_to_commentary: true },
      { kind: 'tool_call', name: 'load_skill', arguments: { name: 'synthetic_research' } },
      { kind: 'tool_result', name: 'load_skill', preview: '合成操作说明' },
    ])
    await until(
      async () => (await turn().locator('.public-tool').count()) === 2,
      'two public tool activities'
    )
    await assertPublicOrder(publicComments, ['查看技能库', '加载技能'])
    assert.equal(
      await turn().locator('.answer-draft').count(),
      0,
      'promoted preamble is no longer an answer draft'
    )
    await assertClosed()
    assert.equal(await turn().locator('.thinking-body:visible').count(), 0)
    assert.equal(await turn().locator('.public-tool-details:visible').count(), 0)
    await turn().locator('.public-tool-toggle').first().click()
    assert.equal(await turn().locator('.public-tool-details:visible').count(), 1)
    await turn().locator('.public-tool-toggle').first().click()
    checks.push(
      'actual model commentary and grouped tool activities appear in emitted order without opening the reasoning area'
    )
    await entrance().click()
    assert.match(await details().innerText(), /RAW_REASONING_ONLY/)
    for (const text of publicComments)
      assert.ok(
        !(await details().innerText()).includes(text),
        'public text is not repeated as raw reasoning'
      )
    await entrance().click()
    await send([{ kind: 'answer_delta', text: '已读取目录和说明，下面是最终整理。' }])
    await until(
      () => turn().locator('.answer-draft .markdown-answer').isVisible(),
      'final text streams after public progress'
    )
    await assertClosed()
    await finish('已读取目录和说明，下面是最终整理。')
    await assertClosed()
    await assertPublicOrder(publicComments, ['查看技能库', '加载技能'])
    assert.equal(await finalAnswer().innerText(), '已读取目录和说明，下面是最终整理。')
    assert.equal(
      await turn().evaluate((item) =>
        Boolean(
          item
            .querySelector('.public-progress')
            .compareDocumentPosition(item.querySelector('.answer-final')) &
          Node.DOCUMENT_POSITION_FOLLOWING
        )
      ),
      true
    )
    for (const text of publicComments) assert.equal(countText(await turn().innerText(), text), 1)
    await turn().scrollIntoViewIfNeeded()
    await page.screenshot({
      path: path.join(output, 'commentary-tools-final-order.png'),
      fullPage: true,
    })
    checks.push(
      'raw provider reasoning stays behind one default-collapsed control while final output follows the visible progress exactly once'
    )

    const legacyText = '我先检查实验方案里的对照设置。'
    await start('请检查实验方案里的对照设置。')
    await send([
      { kind: 'thinking', text: 'LEGACY_REAL_REASONING' },
      { kind: 'answer_delta', text: legacyText },
      { kind: 'thinking', text: legacyText, preamble: true },
      { kind: 'tool_call', name: 'read_conversation_history', arguments: { query: '对照设置' } },
      { kind: 'tool_result', name: 'read_conversation_history', preview: '合成历史片段' },
    ])
    await until(
      () => turn().locator('.public-commentary').isVisible(),
      'legacy preamble promoted to public progress'
    )
    await assertClosed()
    assert.equal(await turn().locator('.answer-draft').count(), 0)
    assert.equal(countText(await turn().innerText(), legacyText), 1)
    await entrance().click()
    assert.match(await details().innerText(), /LEGACY_REAL_REASONING/)
    assert.ok(!(await details().innerText()).includes(legacyText))
    await entrance().click()
    await finish('对照设置已核对，建议保留独立基线组。')
    assert.equal(countText(await turn().innerText(), legacyText), 1)
    checks.push(
      'legacy thinking+preamble remains visible public progress once and is excluded from raw reasoning and final-answer drafts'
    )
    await turn().scrollIntoViewIfNeeded()
    await page.screenshot({
      path: path.join(output, 'legacy-preamble-compatible.png'),
      fullPage: true,
    })

    await start('请将核对后的实验说明保存为文件。', [
      { kind: 'thinking', text: 'APPROVAL_PRIVATE_REASONING' },
      {
        kind: 'commentary',
        text: '内容已核对，写入文件前需要你批准。',
        source: 'model',
        draft_to_commentary: true,
      },
      { kind: 'tool_call', name: 'edit_project_file', arguments: { path: 'synthetic-plan.txt' } },
      {
        kind: 'approval_request',
        approval_id: 'ap_public_progress',
        tool_name: 'edit_project_file',
        arguments: { path: 'synthetic-plan.txt' },
        risk: 'guarded',
        reason: '确认写入合成文件',
      },
    ])
    await until(() => turn().locator('.approval-card').isVisible(), 'approval remains visible')
    await assertClosed()
    assert.equal(await details().locator('.approval-card').count(), 0)
    assert.equal(await turn().locator('.public-commentary').isVisible(), true)
    await turn().locator('.approval-card').scrollIntoViewIfNeeded()
    await page.screenshot({
      path: path.join(output, 'approval-public-progress.png'),
      fullPage: true,
    })
    await turn().getByRole('button', { name: '批准一次', exact: true }).click()
    await until(() => decisions.length === 1, 'one approval decision')
    await send([
      { kind: 'approval_result', approval_id: 'ap_public_progress', decision: 'approve' },
    ])
    await finish('合成实验说明已保存。')
    checks.push(
      'approval stays outside collapsed reasoning and visible public progress, and remains actionable'
    )

    const incomplete = [{ title: '读取尚未提供的实验记录', phase: 'read', status: 'pending' }]
    await start('请读取尚未提供的实验记录并总结。', [
      { kind: 'thinking', text: 'PARTIAL_RAW_REASONING' },
      {
        kind: 'commentary',
        text: '尚未拿到实验记录，当前只能保留已确认内容。',
        source: 'model',
        draft_to_commentary: true,
      },
    ])
    await send([
      { kind: 'task_outcome', outcome: 'partial', completed: false, incomplete_steps: incomplete },
    ])
    await finish('当前缺少实验记录，读取与总结尚未完成。', {
      outcome: 'partial',
      completed: false,
      incomplete_steps: incomplete,
      plan_steps: incomplete,
    })
    assert.match(await entrance().innerText(), /部分完成/)
    assert.match(await turn().locator('.answer-head').innerText(), /部分完成/)

    await page.reload()
    await until(
      async () => (await page.locator('.answer-final').count()) === messages.length,
      'final history restored'
    )
    const ordered = page.locator('.turn').nth(1)
    assert.deepEqual(
      await ordered
        .locator('.public-commentary, .public-tool')
        .evaluateAll((items) =>
          items.map((item) =>
            item.classList.contains('public-commentary') ? 'commentary' : 'tool'
          )
        ),
      ['commentary', 'tool', 'commentary', 'tool']
    )
    for (const text of publicComments) assert.equal(countText(await ordered.innerText(), text), 1)
    const legacy = page.locator('.turn').nth(2)
    assert.equal(countText(await legacy.innerText(), legacyText), 1)
    assert.match(await turn().locator('.run-process-toggle').innerText(), /部分完成/)
    assert.match(await turn().locator('.answer-head').innerText(), /部分完成/)
    assert.doesNotMatch(await turn().locator('.run-process-toggle').innerText(), /已完成/)
    for (const item of await page.locator('.turn').all()) {
      assert.ok((await item.locator('.run-process-toggle').count()) <= 1)
      assert.equal(await item.locator('.run-process-details:visible').count(), 0)
    }
    checks.push(
      'history reload restores public ordering and legacy preambles once, keeps reasoning collapsed, and retains actual partial outcome'
    )
    await turn().scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'partial-history-state.png'), fullPage: true })
    assert.deepEqual(errors, [])
    const report = {
      ok: true,
      verified_at: new Date().toISOString(),
      source_sha256: hashes,
      verification:
        'real Vue UI, synthetic API state and public SSE fixtures; not a real-provider quality test',
      real_provider_calls: 0,
      synthetic_sse_runs: syntheticSseRuns,
      completed_qa_records: messages.length,
      synthetic_approval_decisions: decisions.length,
      checks,
      screenshots: [
        'direct-answer.png',
        'commentary-tools-final-order.png',
        'legacy-preamble-compatible.png',
        'approval-public-progress.png',
        'partial-history-state.png',
      ],
    }
    await fs.rm(path.join(output, 'failure.png'), { force: true })
    await fs.rm(path.join(output, 'failure.txt'), { force: true })
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report, null, 2))
  } catch (error) {
    await page
      .screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      .catch(() => {})
    await fs
      .writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText())
      .catch(() => {})
    throw error
  } finally {
    await browser.close()
    await vite.close()
    apiFallback.closeAllConnections()
    await new Promise((resolve) => apiFallback.close(resolve))
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
