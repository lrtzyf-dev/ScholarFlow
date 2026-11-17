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
  const output = path.join(root, 'verification/chat-compact')
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
    'frontend/tests/chat-compact.cjs',
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
  const conversation = { id: 'c_compact_ui', title: '紧凑过程验证', message_count: 0 }
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
  const entrance = () => turn().locator('.run-process-toggle')
  const details = () => turn().locator('.run-process-details')
  const feed = (events, close = false) =>
    page.evaluate(({ events, close }) => window.feedCompact(events, close), { events, close })
  let currentQuestion = ''
  let currentTrace = []
  const basic = [
    {
      kind: 'runtime_config',
      text: '本轮模型与权限配置已绑定',
      runtime_config: { model_id: 'Synthetic-Compact' },
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
  try {
    await page.goto(`${base}/chat?conv=${conversation.id}`)
    await until(() => page.locator('textarea.textarea').isEnabled(), 'composer ready')

    // Ordinary knowledge and translation take the same event-driven UI path.
    await start('能用通俗的话解释什么是置信区间吗？')
    await feed([{ kind: 'answer_delta', text: '置信区间用于表达估计结果的不确定性。' }])
    await until(
      () => turn().locator('.markdown-answer').isVisible(),
      'plain answer streams directly'
    )
    assert.equal(await turn().locator('.thinking:visible, .todo:visible').count(), 0)
    assert.equal(await turn().locator('.run-process-details:visible').count(), 0)
    await finish('置信区间用于表达估计结果的不确定性。')
    assert.equal(
      await entrance().count(),
      0,
      'basic telemetry does not leave a completed process entrance'
    )
    checks.push(
      'ordinary knowledge streams its answer directly and basic telemetry creates no completed process panel'
    )
    await page.screenshot({ path: path.join(output, 'ordinary-answer.png'), fullPage: true })

    await start('Please translate: The results are consistent across datasets.', [
      { kind: 'thinking_delta', text: 'COMPACT_TRANSLATION_THOUGHT' },
    ])
    await feed([{ kind: 'answer_delta', text: '这些结果在不同数据集之间保持一致。' }])
    await until(() => entrance().isVisible(), 'one reasoning entrance')
    await assertClosed()
    assert.equal(await turn().locator('.markdown-answer').isVisible(), true)
    await entrance().click()
    assert.equal(await details().isVisible(), true)
    assert.match(await details().innerText(), /COMPACT_TRANSLATION_THOUGHT/)
    assert.doesNotMatch(
      await details().innerText(),
      /本轮模型与权限配置已绑定|正在理解当前请求|第1轮思考/
    )
    assert.equal(
      await details().locator('.thinking-toggle, .process-toggle').count(),
      0,
      'no nested process entrances'
    )
    await entrance().click()
    await feed([{ kind: 'thinking_delta', text: ' COMPACT_MORE_THOUGHT' }])
    await assertClosed()
    currentTrace.push({
      kind: 'thinking',
      text: 'COMPACT_TRANSLATION_THOUGHT COMPACT_MORE_THOUGHT',
    })
    await finish('这些结果在不同数据集之间保持一致。')
    await assertClosed()
    checks.push(
      'real reasoning has one initially collapsed entrance, opens manually, and new tokens/completion do not reopen it'
    )

    await start('列出当前可用技能并说明用途。', [
      { kind: 'tool_call', name: 'list_skills', arguments: {} },
      { kind: 'tool_result', name: 'list_skills', preview: '合成技能目录' },
    ])
    await feed([{ kind: 'answer_delta', text: '这是合成测试提供的技能目录。' }])
    await until(() => entrance().isVisible(), 'single tool process entrance')
    await assertClosed()
    await finish('这是合成测试提供的技能目录。')
    await assertClosed()
    await entrance().click()
    assert.match(await details().innerText(), /查看技能库/)
    assert.equal(
      await turn().locator('.todo').count(),
      0,
      'a tool operation alone creates no task plan'
    )
    await entrance().click()
    checks.push('a single real tool event remains inspectable without inventing a task plan')

    const plan = [
      { title: '核对实验记录', phase: 'read', status: 'done' },
      { title: '整理后续行动', phase: 'write', status: 'in_progress' },
    ]
    const taskEvents = [
      { kind: 'thinking', text: 'COMPACT_TASK_THOUGHT' },
      { kind: 'plan', steps: plan },
      { kind: 'tool_call', name: 'list_skills', arguments: {} },
      { kind: 'tool_result', name: 'list_skills', preview: '合成技能目录' },
    ]
    await start('读取提供的实验记录，整理待办并说明下一步。', taskEvents)
    await feed([{ kind: 'answer_delta', text: '下一步先复核样本量，再补充对照实验。' }])
    await until(() => entrance().isVisible(), 'task process entrance')
    await assertClosed()
    assert.equal(await turn().locator('.todo:visible').count(), 0)
    await entrance().click()
    assert.match(await details().innerText(), /核对实验记录/)
    assert.match(await details().innerText(), /COMPACT_TASK_THOUGHT/)
    assert.match(await details().innerText(), /查看技能库/)
    assert.equal(await turn().locator('.run-process-toggle').count(), 1)
    await details().scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'expanded-real-process.png'), fullPage: true })
    await finish('下一步先复核样本量，再补充对照实验。', {
      plan_steps: plan.map((step) => ({ ...step, status: 'done' })),
    })
    assert.equal(
      await entrance().getAttribute('aria-expanded'),
      'true',
      'completion retains a user-opened process'
    )
    assert.equal(await details().isVisible(), true)
    await entrance().click()
    await assertClosed()
    await page.screenshot({
      path: path.join(output, 'completed-process-collapsed.png'),
      fullPage: true,
    })
    checks.push(
      'actual plan, thinking and tool history share one expandable area and retain the user expansion choice through completion'
    )

    await start('请把实验方案保存到工作目录。', [
      { kind: 'thinking', text: 'COMPACT_APPROVAL_THOUGHT' },
      {
        kind: 'approval_request',
        approval_id: 'ap_compact',
        tool_name: 'edit_project_file',
        arguments: { path: 'experiment-plan.txt', content: '合成计划' },
        risk: 'guarded',
        reason: '需要用户确认写入文件',
      },
    ])
    await until(
      () => turn().locator('.approval-card').isVisible(),
      'approval visible outside process'
    )
    await assertClosed()
    assert.equal(await details().locator('.approval-card').count(), 0)
    const approve = turn().getByRole('button', { name: '批准一次', exact: true })
    assert.equal(await approve.isVisible(), true)
    assert.equal(await approve.isEnabled(), true)
    await page.screenshot({
      path: path.join(output, 'approval-outside-collapsed-process.png'),
      fullPage: true,
    })
    await approve.click()
    await until(() => decisions.length === 1, 'approval action reaches synthetic endpoint')
    assert.equal(decisions[0].decision, 'approve')
    await feed([{ kind: 'approval_result', approval_id: 'ap_compact', decision: 'approve' }])
    await until(
      async () => (await turn().locator('.approval-card').count()) === 0,
      'approval resolved'
    )
    await finish('合成实验方案已保存。')
    await assertClosed()
    checks.push(
      'approval stays visible and actionable outside the collapsed process, then resolves normally'
    )

    await page.reload()
    await until(
      async () => (await page.locator('.answer-brand').count()) === messages.length,
      'history restored'
    )
    assert.equal(await page.locator('.turn').count(), messages.length)
    for (const item of await page.locator('.turn').all()) {
      assert.ok((await item.locator('.run-process-toggle').count()) <= 1)
      assert.equal(await item.locator('.run-process-details:visible').count(), 0)
    }
    assert.equal(await page.locator('.turn').first().locator('.run-process-toggle').count(), 0)
    checks.push(
      'reloaded completed history has no duplicate entrances and defaults to collapsed actual process'
    )
    assert.deepEqual(errors, [])
    const report = {
      ok: true,
      verified_at: new Date().toISOString(),
      source_sha256: hashes,
      verification: 'real Vue UI, synthetic API state and public SSE fixtures',
      real_provider_calls: 0,
      synthetic_sse_runs: syntheticSseRuns,
      completed_qa_records: messages.length,
      synthetic_approval_decisions: decisions.length,
      checks,
      screenshots: [
        'ordinary-answer.png',
        'expanded-real-process.png',
        'completed-process-collapsed.png',
        'approval-outside-collapsed-process.png',
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
