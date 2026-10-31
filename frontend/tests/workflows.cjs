// Run against a local frontend with PLAYWRIGHT_MODULE pointing to Playwright if needed.
const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs/promises')
const stateMock = require('./desktop-state-mock.cjs')()
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'
const output = path.resolve(__dirname, '../../verification')
const isoDaysAgo = (days) => {
  const date = new Date()
  date.setDate(date.getDate() - days)
  return date.toISOString()
}
const conversations = [
  { id: 'c_a', title: 'Conversation A', message_count: 1, created_at: isoDaysAgo(40), updated_at: isoDaysAgo(0) },
  { id: 'c_b', title: 'Conversation B', message_count: 1, created_at: isoDaysAgo(1), updated_at: isoDaysAgo(1) },
  { id: 'c_c', title: 'Conversation C', message_count: 1, created_at: isoDaysAgo(4), updated_at: isoDaysAgo(4) },
  { id: 'c_d', title: 'Conversation D', message_count: 1, created_at: isoDaysAgo(40), updated_at: isoDaysAgo(40) },
]
const approval = (id) => ({ approval_id: id, tool_name: 'write_report', arguments: { path: 'reports/long-file-name.md', overwrite: false }, risk: 'high', reason: 'Write the research report' })
const richAnswer = 'Answer c_a\n\n## 研究结论\n\n- **结果一**：保留证据溯源 [^e1]\n- 结果二：按方法对比实验\n\n| 方法 | 准确率 | 样本数 |\n| --- | --- | --- |\n| 基线 | 82% | 1200 |\n| 改进方法 | 91% | 1200 |\n\n```python\nprint("research result")\n# [^e1] remains code\n```\n\n[来源](https://example.com)\n\n<script>window.markdownInjected = true</script>'
const task = { id: 'task_a', type: 'agent', title: 'Research task with a long title for responsive layout verification', status: 'running', progress: 0.5, plan: [{ title: 'Search papers', status: 'done' }, { title: 'Write report', status: 'in_progress' }], events: [{ kind: 'status', text: 'Waiting for approval' }], approvals: [approval('apv_one')], result: null, error: '', conversation_id: 'c_a', updated_at: '2026-10-30T08:00:00' }

async function main() {
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  let snapshot = [structuredClone(task)]
  let decisionStatus = 200
  const decisions = []
  const streams = []
  let pinStatus = 200
  await page.addInitScript(() => {
    localStorage.setItem('scholarflow_conversation_id', 'c_a')
    window.taskSources = []
    window.EventSource = class extends EventTarget {
      constructor(url) {
        super()
        this.url = url
        window.taskSources.push(this)
        setTimeout(() => this.onopen?.(new Event('open')), 0)
      }
      close() { this.closed = true }
    }
    window.emitTasks = (tasks) => {
      for (const source of window.taskSources) {
        if (!source.closed) source.dispatchEvent(new MessageEvent('tasks', { data: JSON.stringify({ tasks }) }))
      }
    }
  })
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const request = route.request()
    const url = new URL(request.url())
    const json = (body, status = 200) => route.fulfill({ status, json: body })
    if (url.pathname === '/api/foreground-runs') {
      if (request.method() === 'GET') return json({ runs: [] })
      if (request.method() === 'POST') return json({ id: request.postDataJSON().id, revision: 1, status: 'running' })
    }
    if (url.pathname.startsWith('/api/foreground-runs/')) {
      if (request.method() === 'PATCH') return json({ revision: 2, status: 'running' })
      if (request.method() === 'DELETE') return json({ dismissed: true })
    }
    if (url.pathname === '/api/tasks') return json({ tasks: snapshot })
    if (url.pathname === '/api/approvals/decide') {
      decisions.push(request.postDataJSON())
      snapshot[0].approvals = []
      await json(decisionStatus === 200 ? { decision: 'approve' } : { detail: 'Expired' }, decisionStatus)
      await page.evaluate((tasks) => window.emitTasks(tasks), snapshot)
      return
    }
    if (url.pathname === '/api/conversations') return json({ conversations })
    if (url.pathname === '/api/conversations/page') return json({ conversations, total: conversations.length, pinned_total: conversations.filter((c) => c.pinned_at).length, next_cursor: null })
    if (url.pathname.endsWith('/activity') && url.pathname.startsWith('/api/conversations/')) {
      const item = conversations.find((c) => c.id === url.pathname.split('/')[3])
      return json(item || { detail: 'Not found' }, item ? 200 : 404)
    }
    if (/^\/api\/conversations\/c_[a-d]\/pin$/.test(url.pathname)) {
      if (pinStatus !== 200) return json({ detail: 'Injected pin failure' }, pinStatus)
      const item = conversations.find((conversation) => conversation.id === url.pathname.split('/')[3])
      item.pinned_at = request.postDataJSON().pinned ? new Date().toISOString() : null
      return json({ id: item.id, pinned_at: item.pinned_at })
    }
    if (url.pathname === '/api/conversations/usage/summary') return json({ turns: 2, conversations: 2, usage: { prompt_tokens: 30, completion_tokens: 13, total_tokens: 43 } })
    if (/^\/api\/conversations\/c_[ab]$/.test(url.pathname)) {
      const id = url.pathname.split('/').pop()
      return json({ id, title: id, messages: [{ id: `qa_${id}`, question: `Question ${id}`, answer: id === 'c_a' ? richAnswer : `Answer ${id}`, evidence: id === 'c_a' ? [{ id: 'e1', paper_id: 'p_one', paper_title: 'Evidence source', page: 1, quote: 'Original source', score: 0.9 }] : [], skills: [], refusal: 0, latency_ms: 1, usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } }] })
    }
    if (url.pathname === '/api/chat/stream') {
      let release
      const ready = new Promise((resolve) => { release = resolve })
      streams.push({ release })
      await ready
      try {
        await route.fulfill({ contentType: 'text/event-stream', body: 'data: {"kind":"answer","answer":"Finished answer","evidence":[],"qa_id":"qa_new"}\n\ndata: {"kind":"done"}\n\n' })
      } catch { /* A switched conversation aborts its old request. */ }
      return
    }
    if (url.pathname === '/api/papers') return json([])
    if (url.pathname === '/api/skills') return json({ skills: [] })
    if (url.pathname === '/api/mcp/servers') return json({ servers: [] })
    if (url.pathname === '/api/health') return json({ status: 'ok', llm_mode: 'mock', paper_count: 0 })
    if (url.pathname.startsWith('/api/goal')) return json({ current: null, goals: [] })
    if (url.pathname === '/api/settings') return json({ approval_mode: 'auto', reasoning_effort: 'medium', memory_recall: true })
    return json({})
  })
  const emit = async () => page.evaluate((tasks) => window.emitTasks(tasks), snapshot)
  const waitFor = async (predicate, label) => {
    const start = Date.now()
    while (!await predicate()) {
      assert.ok(Date.now() - start < 8000, `Timed out: ${label}`)
      await page.waitForTimeout(40)
    }
  }
  const composer = page.locator('textarea').first()
  const command = async (text) => { await composer.fill(text); await composer.press('Enter') }
  const startTurn = async () => {
    const count = streams.length
    await command('A question that waits for the server')
    await waitFor(() => streams.length > count, 'chat request')
    return streams[count]
  }
  try {
    await page.goto(`${baseURL}/tasks`)
    await page.getByRole('button', { name: '新建会话', exact: true }).click()
    await page.waitForFunction(() => localStorage.getItem('scholarflow_conversation_id') !== 'c_a')
    await page.waitForURL((url) => url.pathname === '/chat' && !url.searchParams.has('conv'))
    await page.goto(`${baseURL}/chat?conv=c_a`)
    await page.getByText('Answer c_a', { exact: true }).waitFor()
    assert.deepEqual(await page.locator('.conversation-date-label').allTextContents(), ['今天', '昨天', '过去 7 天', '更早'])
    assert.equal(await page.locator('.recent-conversation').count(), 4, 'sidebar includes all sessions, grouped by date')
    const oldest = page.locator('.recent-conversation-row').filter({ hasText: 'Conversation D' })
    await oldest.hover()
    pinStatus = 500
    await oldest.getByRole('button', { name: '固定 Conversation D', exact: true }).click()
    await page.getByText('Injected pin failure', { exact: true }).waitFor()
    assert.equal(await page.locator('.conversation-date-label').filter({ hasText: '已固定' }).count(), 0)
    pinStatus = 200
    await oldest.getByRole('button', { name: '固定 Conversation D', exact: true }).click()
    await waitFor(async () => (await page.locator('.conversation-date-label').first().textContent()) === '已固定', 'pin group appears')
    assert.equal(await page.locator('.recent-conversation-title').first().textContent(), 'Conversation D')
    await page.locator('.approval-banner').waitFor()
    assert.equal(await page.locator('.el-notification').count(), 0, 'initial history should not create completion toasts')
    await page.locator('.approval-banner').click()
    await page.getByRole('button', { name: '本次运行允许', exact: true }).waitFor()
    assert.match(await page.locator('.task-approval pre').textContent(), /long-file-name/)
    await page.screenshot({ path: path.join(output, 'tasks-desktop.png'), fullPage: true })
    await page.getByRole('button', { name: '本次运行允许', exact: true }).click()
    await waitFor(() => decisions.length === 1, 'approval decision')
    assert.deepEqual(decisions[0], { approval_id: 'apv_one', decision: 'approve', scope: 'session' })
    await waitFor(async () => await page.locator('.task-approval').count() === 0, 'approval removed')
    snapshot[0].approvals = [approval('apv_deny')]
    await emit()
    await page.getByRole('button', { name: '拒绝', exact: true }).click()
    await waitFor(() => decisions.length === 2, 'denial')
    assert.equal(decisions[1].decision, 'deny')
    snapshot[0].approvals = [approval('apv_expired')]
    decisionStatus = 404
    await emit()
    await page.getByRole('button', { name: '允许一次', exact: true }).click()
    await page.getByText('该审批已结束，任务状态已刷新').waitFor()
    assert.equal(await page.locator('.task-approval').count(), 0)
    decisionStatus = 200
    snapshot[0].approvals = [approval('apv_desktop')]
    await emit()
    await page.setViewportSize({ width: 1100, height: 700 })
    assert.deepEqual(await page.locator('.conversation-date-label').allTextContents(), ['已固定', '今天', '昨天', '过去 7 天'])
    await page.getByRole('button', { name: '新建会话', exact: true }).waitFor()
    await page.locator('.el-message').last().waitFor({ state: 'hidden' })
    await page.screenshot({ path: path.join(output, 'tasks-small-desktop.png'), fullPage: true })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'minimum desktop window fits')
    for (const box of await page.locator('.task-actions button').all()) {
      const bounds = await box.boundingBox()
      if (bounds) assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 1101, 'desktop action fits')
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.locator('.nav-item').filter({ hasText: '问答工作台' }).click()
    await page.getByText('Answer c_a', { exact: true }).waitFor()
    assert.equal(await page.evaluate(() => window.taskSources.length), 1, 'one SSE connection across routes')
    assert.equal(await page.locator('.markdown-answer h2').textContent(), '研究结论')
    assert.equal(await page.locator('.markdown-answer tbody tr').count(), 2)
    assert.equal(await page.locator('.markdown-citation').count(), 1)
    assert.equal(await page.locator('.markdown-answer script').count(), 0)
    assert.equal(await page.evaluate(() => window.markdownInjected), undefined)
    assert.equal(await page.getByText('引用闸门未通过', { exact: true }).count(), 0, 'missing historical gate is not a failed gate')
    await page.screenshot({ path: path.join(output, 'chat-richtext-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 1100, height: 700 })
    await page.screenshot({ path: path.join(output, 'chat-small-desktop.png'), fullPage: true })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'chat fits minimum desktop window')
    const send = await page.getByRole('button', { name: '发送', exact: true }).boundingBox()
    assert.ok(send && send.x + send.width <= 1101, 'send control fits desktop')
    await page.setViewportSize({ width: 1440, height: 900 })
    const first = await startTurn()
    await command('/hel')
    await command('/usage')
    assert.match(await page.locator('.queued-command-list').textContent(), /\/help.*\/usage/)
    await composer.fill('Keep this new draft')
    first.release()
    await page.locator('.el-message-box__title').filter({ hasText: '斜杠命令' }).waitFor()
    assert.equal(await composer.inputValue(), 'Keep this new draft')
    await page.getByRole('button', { name: '关闭', exact: true }).click()
    await page.locator('.el-message-box__title').filter({ hasText: '已记录用量' }).waitFor()
    const usageDialog = page.locator('.el-message-box').filter({ has: page.locator('.el-message-box__title', { hasText: '已记录用量' }) })
    assert.match(await usageDialog.locator('.el-message-box__message').textContent(), /43/)
    assert.equal(await composer.inputValue(), 'Keep this new draft')
    await usageDialog.getByRole('button', { name: '关闭', exact: true }).click()
    const second = await startTurn()
    await command('/help')
    await page.locator('.recent-conversation').filter({ hasText: 'Conversation B' }).click()
    await page.getByText('Answer c_b', { exact: true }).waitFor()
    second.release()
    await page.waitForTimeout(200)
    assert.equal(await page.locator('.queued-commands').count(), 0)
    assert.equal(await page.locator('.el-message-box:visible').count(), 0, 'old queue cannot run in another conversation')
    const third = await startTurn()
    await command('/help')
    await page.locator('.queued-commands button').click()
    await composer.fill('Preserved after queue clear')
    third.release()
    await page.getByText('Finished answer', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), 'Preserved after queue clear')
    assert.equal(await page.locator('.el-message-box:visible').count(), 0)
    snapshot[0].approvals = []
    snapshot[0].status = 'done'
    snapshot[0].progress = 1
    await emit()
    await page.getByText('后台任务已完成', { exact: true }).waitFor()
    await emit()
    assert.equal(await page.locator('.el-notification').count(), 1, 'no duplicate completion notification')
    await page.locator('.el-message').last().waitFor({ state: 'hidden' })
    await page.screenshot({ path: path.join(output, 'chat-desktop.png'), fullPage: true })
    await page.getByRole('button', { name: '新建会话', exact: true }).click()
    await page.getByText('Answer c_b', { exact: true }).waitFor({ state: 'detached' })
    assert.equal(new URL(page.url()).searchParams.has('conv'), false, 'sidebar new conversation clears the selected session')
    const sortSaved = page.waitForResponse((response) => response.url().endsWith('/api/desktop-state/sidebar') && response.request().method() === 'PUT')
    await page.getByRole('combobox', { name: '会话排序', exact: true }).selectOption('created_at')
    assert.equal((await sortSaved).status(), 200)
    await page.reload()
    await waitFor(async () => (await page.getByRole('combobox', { name: '会话排序', exact: true }).inputValue()) === 'created_at', 'sort restored after reload')
    assert.deepEqual(await page.locator('.conversation-date-label').allTextContents(), ['已固定', '昨天', '过去 7 天', '更早'])
    assert.equal(await page.locator('.recent-conversation-title').first().textContent(), 'Conversation D')
    await page.screenshot({ path: path.join(output, 'sidebar-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 1100, height: 700 })
    await page.screenshot({ path: path.join(output, 'sidebar-small-desktop.png'), fullPage: true })
    const aboutBounds = await page.getByRole('button', { name: '关于', exact: true }).boundingBox()
    assert.ok(aboutBounds && aboutBounds.y + aboutBounds.height <= 700, 'navigation remains visible with independently scrolling sessions')
    await page.getByRole('button', { name: '取消固定 Conversation D', exact: true }).click()
    await waitFor(async () => await page.locator('.conversation-date-label').filter({ hasText: '已固定' }).count() === 0, 'unpin returns session to its date group')
    assert.deepEqual(errors, [])
    console.log('PASS: sidebar date groups, cross-route new conversation, pin failure/retry, pin/unpin, persisted sort and bounded session scrolling at 1440/1100, approvals, cross-route SSE, Markdown, command queue, drafts, conversation switch, completion notification deduplication')
  } finally {
    for (const stream of streams) stream.release()
    await browser.close()
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
