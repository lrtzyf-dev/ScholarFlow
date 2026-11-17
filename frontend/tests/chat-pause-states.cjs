// Real Vue rendering with synthetic public events and recovery responses only.
// No backend, user configuration, credentials or model provider is accessed.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
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
      host: '127.0.0.1', port, strictPort: true, hmr: false,
      proxy: { '/api': { target: `http://127.0.0.1:${apiFallback.address().port}` } },
    },
  })
  await server.listen()
  const base = `http://127.0.0.1:${server.httpServer.address().port}`
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  page.setDefaultTimeout(7000)
  const output = path.join(root, '../verification/chat-pause-states')
  await fs.mkdir(output, { recursive: true })
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  const conversation = { id: 'c_pause_states', title: '暂停原因回归测试', message_count: 0 }
  let foregroundRuns = []
  await page.addInitScript(() => {
    window.EventSource = class extends EventTarget { close() {} }
    window.pauseStreams = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, options) => {
      if (String(input) !== '/api/chat/stream') return originalFetch(input, options)
      const stream = new ReadableStream({
        start(controller) {
          window.pauseStreams.push(controller)
          options.signal?.addEventListener('abort',
            () => controller.error(new DOMException('Aborted', 'AbortError')), { once: true })
        },
      })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
    window.feedPause = (events) => {
      const controller = window.pauseStreams.at(-1)
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
    if (url.pathname === '/api/conversations/c_pause_states')
      return json({ ...conversation, messages: [] })
    if (url.pathname === '/api/conversations/page')
      return json({ conversations: [conversation], total: 1, pinned_total: 0, next_cursor: null })
    if (url.pathname.endsWith('/activity')) return json(conversation)
    if (url.pathname.endsWith('/project')) return json({ project: null, workspace_revision: 0 })
    if (url.pathname === '/api/sources') return json({ items: [], next_offset: null })
    if (url.pathname === '/api/chat/runs/inputs')
      return json({ conversation_id: conversation.id, queues: [] })
    if (/^\/api\/chat\/runs\/[^/]+\/events$/.test(url.pathname))
      return route.fulfill({
        contentType: 'text/event-stream',
        body: 'data: {"kind":"stream_end","status":"running","terminal":false}\n\n',
      })
    if (url.pathname === '/api/papers/page')
      return json({ items: [], total: 0, statistics: { papers: 0 }, next_cursor: null })
    if (url.pathname === '/api/foreground-runs')
      return json(request.method() === 'GET'
        ? { runs: foregroundRuns }
        : { id: request.postDataJSON().id, revision: 1, status: 'running' })
    if (url.pathname.startsWith('/api/foreground-runs/'))
      return json({ revision: 2, status: 'paused' })
    if (url.pathname.startsWith('/api/conversation-options/'))
      return json({ revision: 1, approval_mode: 'auto', effective_model: null })
    if (url.pathname === '/api/model-profiles') return json({ profiles: [], default_profile_id: null })
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
  async function submitPause(event, beforeEvents = []) {
    const before = await page.evaluate(() => window.pauseStreams.length)
    await page.locator('textarea.textarea').fill(`合成暂停测试 ${before}`)
    await page.getByRole('button', { name: '发送', exact: true }).click()
    try {
      await page.waitForFunction((count) => window.pauseStreams.length > count, before)
    } catch (error) {
      console.error('Submission state:', await page.locator('body').innerText())
      console.error('Browser errors:', errors)
      throw error
    }
    await page.evaluate((items) => window.feedPause(items), [
      ...beforeEvents, { kind: 'paused', ...event },
      { kind: 'stream_end', status: 'paused', terminal: true },
    ])
    const turn = turns.last()
    await turn.locator('.foreground-recovery').waitFor()
    await page.getByRole('button', { name: '发送', exact: true }).waitFor({ state: 'visible' })
    return turn
  }
  const contextMessage = '保留用户要求后仍超过模型上下文窗口，请调整模型上下文设置后继续。'
  const contextEvent = {
    reason: 'context_capacity', text: contextMessage,
    resource_pause: { kind: 'context', message: contextMessage, phase: 'model' },
    resume_available: true,
  }
  async function checkContext(turn) {
    const panel = turn.locator('.foreground-recovery')
    assert.equal(await panel.locator('strong').innerText(), '上下文容量不足，进度已保存。')
    assert.ok((await panel.innerText()).includes(contextMessage), 'the backend explanation remains visible')
    assert.equal(await panel.getByRole('button', { name: '继续当前任务', exact: true }).count(), 1)
    assert.doesNotMatch(await panel.innerText(), /预算已用完|追加执行预算|\+6轮/)
  }
  function recovered(fields = {}) {
    return {
      id: 'chat_pause_recovered', conversation_id: conversation.id, revision: 2,
      run_revision: 3, question: '恢复上下文暂停任务', paper_id: null, skills: [], images: [],
      partial_answer: '', phase: 'stream_end', status: 'paused', error: contextMessage,
      resume_available: true, resume_kind: 'resources', resume_reason: contextMessage,
      resource_pause: contextEvent.resource_pause, created_at: '2026-11-16T11:19:07Z',
      updated_at: '2026-11-16T11:22:28Z', ...fields,
    }
  }
  try {
    await page.goto(base + '/chat?conv=c_pause_states')
    await page.locator('textarea.textarea').waitFor()
    const liveContext = await submitPause(contextEvent)
    await checkContext(liveContext)

    const budgetMessage = '本轮费用预算不足以发送下一请求，请调整预算后继续。'
    const budget = await submitPause({ reason: 'resource_budget', text: budgetMessage,
      resource_pause: { kind: 'budget', message: budgetMessage, phase: 'model' } })
    assert.equal(await budget.locator('.foreground-recovery strong').innerText(), '预算检查未通过，进度已保存。')
    assert.ok((await budget.locator('.foreground-recovery').innerText()).includes(budgetMessage))
    assert.doesNotMatch(await budget.locator('.foreground-recovery').innerText(), /\+6轮/)

    const noProgress = await submitPause({ reason: 'no_progress',
      text: '连续两个执行阶段没有取得新结果，请调整检索范围后继续。' })
    assert.equal(await noProgress.locator('.foreground-recovery strong').innerText(), '连续执行未取得新结果，进度已保存。')
    assert.doesNotMatch(await noProgress.locator('.foreground-recovery').innerText(), /预算已用完|追加执行预算/)

    const steps = await submitPause({ reason: 'step_limit', text: '本阶段执行轮数已用完。' })
    assert.equal(await steps.locator('.foreground-recovery strong').innerText(), '本阶段执行轮数已用完，进度已保存。')
    assert.equal(await steps.getByRole('button', { name: '继续当前任务（+6轮）', exact: true }).count(), 1)

    const unknown = await submitPause({ text: '任务等待人工检查后继续。' })
    assert.doesNotMatch(await unknown.locator('.foreground-recovery').innerText(), /预算已用完|追加执行预算|\+6轮/)
    assert.ok((await unknown.locator('.foreground-recovery').innerText()).includes('任务等待人工检查后继续。'))

    // Recovery metadata must work without any public display snapshot.
    foregroundRuns = [recovered()]
    await page.reload()
    await turns.first().locator('.foreground-recovery').waitFor()
    await checkContext(turns.first())

    // A public snapshot can carry the same cause in its paused event history.
    foregroundRuns = [recovered({
      resume_reason: undefined, resource_pause: undefined,
      display_snapshot: {
        kind: 'snapshot', run_id: 'chat_pause_recovered', conversation_id: conversation.id,
        cursor: 8, seq: 8, partial_answer: '', thinking_parts: [], thinking: '',
        events: [{ kind: 'paused', ...contextEvent, seq: 7 }], plan_steps: [], approvals: [],
        result: null, status: 'paused', phase: 'stream_end', error: contextMessage,
        terminal: true, history_truncated: false,
      },
    })]
    await page.reload()
    await turns.first().locator('.foreground-recovery').waitFor()
    await checkContext(turns.first())

    // Polling must retain the actual cause after an observer disconnects.
    foregroundRuns = [recovered({ status: 'running', error: '', resume_reason: '', resource_pause: null })]
    await page.reload()
    await page.getByText('本轮仍在后台运行，进度和结果会自动同步。', { exact: true }).waitFor()
    foregroundRuns = [recovered()]
    await page.getByText('上下文容量不足，进度已保存。', { exact: true }).waitFor()
    await checkContext(turns.first())

    foregroundRuns = []
    await page.reload()
    await page.locator('textarea.textarea').waitFor()
    const longError = '工具「list_project_files」执行失败：HTTPException: 400: 请使用项目内的相对路径，不能越过项目目录。' + 'unbroken_path_segment_'.repeat(30)
    const errorTurn = await submitPause(contextEvent, [{ kind: 'error', text: longError }])
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1100, height: 700 }]) {
      await page.setViewportSize(viewport)
      const box = errorTurn.locator('.turn-error')
      await box.scrollIntoViewIfNeeded()
      const geometry = await box.evaluate((element) => {
        const icon = element.querySelector('svg').getBoundingClientRect()
        const rect = element.getBoundingClientRect()
        return { iconWidth: icon.width, iconHeight: icon.height,
          scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
          right: rect.right, viewportWidth: window.innerWidth }
      })
      assert.equal(geometry.iconWidth, 16, 'error icon width stays bounded')
      assert.equal(geometry.iconHeight, 16, 'error icon height stays bounded')
      assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, 'long error text does not overflow its card')
      assert.ok(geometry.right <= geometry.viewportWidth, 'error card remains inside the viewport')
      await page.screenshot({ path: path.join(output, `error-and-context-${viewport.width}x${viewport.height}.png`) })
    }
    assert.deepEqual(errors, [])
    console.log('PASS: real Vue paused causes, resource metadata and snapshot recovery, unknown fallback, and 16px error icons at 1440x900 / 1100x700')
  } finally {
    await browser.close()
    await server.close()
    apiFallback.closeAllConnections()
    await new Promise((resolve) => apiFallback.close(resolve))
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
