// Render the real chat page with synthetic history and SSE responses only.
// No backend, user configuration, credentials or model API is accessed.
const assert = require('node:assert/strict')
const path = require('node:path')
const net = require('node:net')
const http = require('node:http')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const stateMock = require('./desktop-state-mock.cjs')()

async function main() {
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  const apiFallback = http.createServer((_req, res) => res.writeHead(204).end())
  await new Promise((resolve) => apiFallback.listen(0, '127.0.0.1', resolve))
  const { createServer } = await import('vite')
  const root = path.resolve(__dirname, '..')
  const server = await createServer({
    root,
    configFile: path.join(root, 'vite.config.ts'),
    logLevel: 'error',
    server: {
      host: '127.0.0.1',
      port,
      strictPort: true,
      hmr: false,
      proxy: { '/api': { target: `http://127.0.0.1:${apiFallback.address().port}` } },
    },
  })
  await server.listen()
  const base = `http://127.0.0.1:${server.httpServer.address().port}`
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  const conversation = { id: 'c_response_states', title: '对话响应状态测试', message_count: 2 }
  const messages = [
    {
      id: 'qa_empty_plan',
      question: '你好',
      answer: '你好！有什么可以帮助你？',
      execution_mode: 'plan',
      plan_steps: [],
      evidence: [],
      refusal: 0,
      skills: [],
      latency_ms: 1,
    },
    {
      id: 'qa_real_plan',
      question: '制定文献整理计划',
      answer: '先确定整理范围，再检索相关文献。',
      execution_mode: 'plan',
      plan_steps: [{ title: '确定整理范围', phase: 'analyze', status: 'pending' }],
      evidence: [],
      refusal: 0,
      skills: [],
      latency_ms: 1,
    },
  ]
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget {
      close() {}
    }
    window.answerStreams = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, options) => {
      if (String(input) !== '/api/chat/stream') return originalFetch(input, options)
      const stream = new ReadableStream({
        start(controller) {
          window.answerStreams.push(controller)
          options.signal?.addEventListener(
            'abort',
            () => controller.error(new DOMException('Aborted', 'AbortError')),
            { once: true }
          )
        },
      })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
    window.feedAnswer = (events) => {
      const controller = window.answerStreams.at(-1)
      for (const event of events)
        controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      controller.close()
    }
  })
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const request = route.request()
    const url = new URL(request.url())
    const json = (value) => route.fulfill({ json: value })
    if (url.pathname === '/api/conversations/c_response_states')
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
      return json({ revision: 1, approval_mode: 'auto', effective_model: null })
    if (url.pathname === '/api/model-profiles')
      return json({ profiles: [], default_profile_id: null })
    if (url.pathname === '/api/tasks') return json({ tasks: [] })
    if (url.pathname === '/api/skills') return json({ skills: [] })
    if (url.pathname === '/api/mcp/servers') return json({ servers: [] })
    if (['/api/papers', '/api/projects'].includes(url.pathname)) return json([])
    if (url.pathname === '/api/settings')
      return json({ approval_mode: 'auto', reasoning_effort: 'medium', memory_recall: true })
    if (url.pathname.startsWith('/api/goal')) return json({ current: null, goals: [] })
    return json({})
  })
  const turns = page.locator('.turn')
  const executeButton = (turn) => turn.getByRole('button', { name: '执行此计划', exact: true })
  const evidence = [
    { id: 'e1', paper_id: 'p_test', paper_title: '测试原文', page: 1, quote: '测试证据' },
  ]
  async function submit(question, result, events = []) {
    const before = await page.evaluate(() => window.answerStreams.length)
    await page.locator('textarea.textarea').fill(question)
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await page.waitForFunction((count) => window.answerStreams.length > count, before)
    await page.evaluate(
      (items) => window.feedAnswer(items),
      [
        ...events,
        {
          kind: 'answer',
          answer: '合成测试回答',
          qa_id: `qa_stream_${before}`,
          evidence: [],
          refusal: 0,
          latency_ms: 1,
          outcome: 'completed',
          completed: true,
          plan_steps: [],
          ...result,
        },
        { kind: 'done', text: '完成' },
      ]
    )
    const turn = turns.last()
    await turn.locator('.markdown-answer').waitFor()
    await page.getByRole('button', { name: '发送', exact: true }).waitFor({ state: 'visible' })
    return turn
  }
  try {
    await page.goto(base + '/chat?conv=c_response_states')
    await turns.nth(1).locator('.markdown-answer').waitFor()
    const empty = turns.nth(0)
    assert.equal(await executeButton(empty).count(), 0, 'empty persisted plans cannot be executed')
    assert.equal(
      await empty.locator('.plan-badge').count(),
      0,
      'social answers are not awaiting review'
    )
    assert.equal(await empty.getByRole('button', { name: '刷新执行状态' }).count(), 0)
    const real = turns.nth(1)
    assert.equal(await executeButton(real).count(), 1, 'a real plan retains its execution action')
    assert.equal(
      await executeButton(real).isVisible(),
      true,
      'plan execution stays accessible outside collapsed process'
    )
    assert.equal(
      await executeButton(real).evaluate((button) => !!button.closest('.run-process-details')),
      false
    )
    assert.equal(await real.locator('.plan-badge').innerText(), '计划 · 待审阅')
    assert.equal(await real.getByRole('button', { name: '刷新执行状态' }).count(), 1)

    const notApplicable = {
      gate: { status: 'not_applicable', passed: true, total_claims: 0, marked_claims: 0 },
      audit: { status: 'not_applicable', passed: false, verified: false },
    }
    const social = await submit('/plan 你好', notApplicable)
    assert.equal(
      await executeButton(social).count(),
      0,
      'a streamed social answer has no executable plan'
    )
    assert.equal(await social.locator('.answer-head .gate-badge').count(), 0)
    const noEvidence = await submit('/execute 普通知识测试', {
      gate: { passed: true, total_claims: 0, marked_claims: 0 },
      audit: { status: 'passed', passed: true, verified: true },
    })
    assert.equal(
      await noEvidence.locator('.answer-head .gate-badge.ok').count(),
      0,
      'no evidence is not a passed citation check'
    )
    const skipped = await submit('/execute 引用不适用状态测试', { ...notApplicable, evidence }, [
      { kind: 'critic', text: '本轮无需引用校验', gate: notApplicable.gate },
    ])
    assert.equal(
      await skipped.locator('.answer-head .gate-badge').count(),
      0,
      'not_applicable overrides legacy boolean fields'
    )
    assert.equal(
      await skipped.locator('.run-process-toggle').count(),
      0,
      'not_applicable citation telemetry creates no completed process entrance'
    )
    const grounded = await submit('/execute 正常引用检查', {
      evidence,
      gate: { status: 'passed', passed: true, total_claims: 1, marked_claims: 1 },
      audit: { status: 'unverified', passed: false, verified: false },
    })
    assert.match(await grounded.locator('.gate-badge.ok').innerText(), /引用闸门通过/)
    assert.match(await grounded.locator('.gate-badge.warn').innerText(), /语义审计未核验/)
    assert.deepEqual(errors, [])
    console.log(
      'PASS: history and SSE empty plans, ordinary chat, non-applicable citation states, and normal audit badges'
    )
  } finally {
    await browser.close()
    await server.close()
    apiFallback.closeAllConnections()
    await new Promise((resolve) => apiFallback.close(resolve))
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
