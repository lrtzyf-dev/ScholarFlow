// Controlled streaming checks for the Windows desktop chat workflow.
const assert = require('node:assert/strict')
const path = require('node:path')
const stateMock = require('./desktop-state-mock.cjs')()
const fs = require('node:fs/promises')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'
const output = path.resolve(__dirname, '../../verification')
const conversations = [
  { id: 'c_a', title: 'Conversation A', message_count: 1 },
  { id: 'c_b', title: 'Conversation B', message_count: 1 },
]
const longAnswer = Array.from({ length: 36 }, (_, i) => `第 ${i + 1} 段研究记录。阅读历史消息时，新输出应保留当前位置。`).join('\n\n')

async function main() {
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = [], cancellations = []
  const foregroundRuns = new Map()
  const remoteRunId = 'chat_remote_12345678'
  foregroundRuns.set(remoteRunId, {
    id: remoteRunId, conversation_id: 'c_b', revision: 1, question: 'Run active in another desktop window',
    paper_id: null, skills: [], images: [], partial_answer: 'Shared partial output.', phase: 'answer',
    status: 'running', error: '', created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    approvals: [{ approval_id: 'remote_approval', tool_name: 'write_file', arguments: { path: 'report.md' }, risk: 'guarded', reason: 'Save reviewed report' }],
  })
  let foregroundCreates = 0
  let foregroundCreateStatus = 200
  let cancelStatus = 200
  let releaseApproval, approvalDecisions = 0
  page.on('pageerror', (e) => errors.push(e.message))
  await page.addInitScript(() => {
    localStorage.setItem('scholarflow_conversation_id', 'c_a')
    window.EventSource = class extends EventTarget { close() {} }
    window.chatStreams = []
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, options) => {
      if (String(input) !== '/api/chat/stream') return originalFetch(input, options)
      const request = JSON.parse(options.body)
      const stream = new ReadableStream({
        start(controller) {
          const entry = { request, controller, closed: false }
          window.chatStreams.push(entry)
          options.signal?.addEventListener('abort', () => {
            entry.closed = true
            controller.error(new DOMException('Aborted', 'AbortError'))
          }, { once: true })
        },
      })
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } })
    }
    window.feedChat = (index, events, close = false) => {
      const entry = window.chatStreams[index]
      if (entry.closed) return
      for (const event of events) {
        entry.controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      }
      if (close) { entry.closed = true; entry.controller.close() }
    }
  })
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const request = route.request()
    const url = new URL(request.url())
    const json = (body, status = 200) => route.fulfill({ status, json: body })
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
      if (body.approval_id === 'expired_remote_approval') return json({ detail: 'Already resolved elsewhere' }, 404)
      await new Promise((resolve) => { releaseApproval = resolve })
      return json(body)
    }
    if (url.pathname === '/api/foreground-runs') {
      if (request.method() === 'GET') {
        const conversationId = url.searchParams.get('conversation_id')
        return json({ runs: [...foregroundRuns.values()].filter((run) =>
          run.conversation_id === conversationId && !['completed', 'dismissed'].includes(run.status)) })
      }
      if (request.method() === 'POST') {
        if (foregroundCreateStatus !== 200) return json({ detail: '检查点创建失败' }, foregroundCreateStatus)
        const body = request.postDataJSON()
        const now = new Date().toISOString()
        const run = { ...body, revision: 1, partial_answer: '', phase: 'setup', status: 'running', error: '', created_at: now, updated_at: now }
        foregroundRuns.set(run.id, run)
        foregroundCreates += 1
        return json({ id: run.id, revision: 1, status: 'running', updated_at: now })
      }
    }
    const foregroundPatch = url.pathname.match(/^\/api\/foreground-runs\/([^/]+)$/)
    if (foregroundPatch && request.method() === 'PATCH') {
      const run = foregroundRuns.get(foregroundPatch[1])
      if (!run) return json({ detail: 'missing run' }, 404)
      const body = request.postDataJSON()
      if (run.status !== 'running' || body.revision !== run.revision) {
        return route.fulfill({ status: 409, json: { detail: { message: 'conflict', current: run } } })
      }
      Object.assign(run, body, { revision: run.revision + 1, updated_at: new Date().toISOString() })
      return json(run)
    }
    if (foregroundPatch && request.method() === 'DELETE') {
      const run = foregroundRuns.get(foregroundPatch[1])
      if (run) { run.status = 'dismissed'; run.revision += 1 }
      return json({ dismissed: foregroundPatch[1] })
    }
    if (url.pathname === '/api/conversations') return json({ conversations })
    if (url.pathname === '/api/conversations/page') return json({ conversations, total: conversations.length, pinned_total: 0, next_cursor: null })
    if (url.pathname.endsWith('/activity') && url.pathname.startsWith('/api/conversations/')) {
      const item = conversations.find((c) => c.id === url.pathname.split('/')[3])
      return json(item || { detail: 'Not found' }, item ? 200 : 404)
    }
    if (/^\/api\/conversations\/c_[ab]$/.test(url.pathname)) {
      const id = url.pathname.split('/').pop()
      return json({ id, title: id, messages: [{ id: `qa_${id}`, question: `Question ${id}`, answer: id === 'c_a' ? longAnswer : 'Answer B', evidence: [], skills: [], refusal: 0, latency_ms: 1 }] })
    }
    if (url.pathname === '/api/tasks') return json({ tasks: [] })
    if (url.pathname === '/api/papers') return json([])
    if (url.pathname === '/api/skills') return json({ skills: [] })
    if (url.pathname === '/api/mcp/servers') return json({ servers: [] })
    if (url.pathname === '/api/settings') return json({ approval_mode: 'auto', reasoning_effort: 'medium', memory_recall: true })
    if (url.pathname.startsWith('/api/goal')) return json({ current: null, goals: [] })
    return json({})
  })
  const waitFor = async (predicate, label) => {
    const start = Date.now()
    while (!await predicate()) {
      assert.ok(Date.now() - start < 8000, `Timed out: ${label}`)
      await page.waitForTimeout(40)
    }
  }
  const composer = page.locator('textarea').first()
  const switchTo = async (title) => {
    await page.locator('.recent-conversation').filter({ hasText: title }).click()
    await page.getByText(title === 'Conversation A' ? 'Question c_a' : 'Question c_b', { exact: true }).waitFor()
    await waitFor(() => composer.isEnabled(), 'conversation restored')
  }
  const command = async (text) => { await composer.fill(text); await composer.press('Enter') }
  const feed = async (index, events, close = false) => page.evaluate(({ index, events, close }) => window.feedChat(index, events, close), { index, events, close })
  const bottomDistance = () => page.locator('.chat-scroll').evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)
  try {
    await page.goto(`${baseURL}/chat?conv=c_a`)
    await page.getByText('Question c_a', { exact: true }).waitFor()
    await composer.fill('Draft A survives switching and reload')
    await switchTo('Conversation B')
    assert.equal(await composer.inputValue(), '')
    await composer.fill('Draft B belongs to B')
    await page.getByText('此轮仍在另一个窗口运行，完成后会自动载入结果。', { exact: true }).waitFor()
    assert.equal(await page.locator('.foreground-recovery-actions').count(), 0, 'active remote run has no retry or remove controls')
    assert.equal(await page.getByRole('button', { name: '发送', exact: true }).count(), 0, 'active remote run blocks a second foreground submission')
    assert.equal(await page.getByText('生成已结束', { exact: true }).count(), 0, 'a remote live run is not labelled finished')
    await page.locator('.approval-arguments').filter({ hasText: 'report.md' }).waitFor()
    await page.screenshot({ path: path.join(output, 'foreground-approval-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 1100, height: 700 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: path.join(output, 'foreground-approval-small-desktop.png'), fullPage: true })
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
    await waitFor(async () => await page.locator('.approval-card').count() === 0, 'remote approval acknowledged')
    foregroundRuns.get(remoteRunId).approvals = [{ approval_id: 'expired_remote_approval', tool_name: 'write_file', arguments: {}, risk: 'guarded', reason: 'Expired approval' }]
    await page.getByText('Expired approval', { exact: true }).waitFor()
    await page.getByRole('button', { name: '批准一次', exact: true }).click()
    await page.getByText('该审批已结束', { exact: true }).waitFor()
    assert.equal(await page.locator('.approval-card').count(), 0)
    await switchTo('Conversation A')
    assert.equal(await composer.inputValue(), 'Draft A survives switching and reload')
    await page.reload()
    await page.getByText('Question c_a', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), 'Draft A survives switching and reload')
    await waitFor(async () => await bottomDistance() <= 64, 'restored chat follows bottom')

    await command('Start a cancellable turn')
    await waitFor(() => page.evaluate(() => window.chatStreams.length === 1), 'stream started')
    const firstId = await page.evaluate(() => window.chatStreams[0].request.run_id)
    assert.match(firstId, /^chat_[\w-]+$/)
    assert.ok(foregroundRuns.has(firstId), 'a durable checkpoint exists before streaming starts')
    assert.equal(foregroundCreates, 1)
    assert.equal(await page.evaluate(() => localStorage.getItem('scholarflow_draft:c_a')), null, 'sent draft removed')
    await feed(0, [{ kind: 'answer_delta', text: 'Partial answer before stopping.\n\n' }])
    await page.getByText('Partial answer before stopping.', { exact: true }).waitFor()
    await waitFor(() => foregroundRuns.get(firstId)?.partial_answer.includes('Partial answer before stopping.'), 'partial answer checkpoint saved')
    await waitFor(async () => await bottomDistance() <= 64, 'new content follows bottom')
    await page.locator('.chat-scroll').evaluate((el) => { el.scrollTop = 100; el.dispatchEvent(new Event('scroll')) })
    await page.getByRole('button', { name: '回到最新消息', exact: true }).waitFor()
    const readingPosition = await page.locator('.chat-scroll').evaluate((el) => el.scrollTop)
    await feed(0, [{ kind: 'answer_delta', text: longAnswer }])
    await page.waitForTimeout(120)
    assert.ok(Math.abs(await page.locator('.chat-scroll').evaluate((el) => el.scrollTop) - readingPosition) < 5, 'streaming preserves reading position')
    await page.screenshot({ path: path.join(output, 'chat-reading-desktop.png'), fullPage: true })
    await page.getByRole('button', { name: '回到最新消息', exact: true }).click()
    await waitFor(async () => await bottomDistance() <= 64, 'jump to latest')

    cancelStatus = 500
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    await page.getByText('停止请求失败：500', { exact: true }).waitFor()
    await waitFor(() => page.getByRole('button', { name: '停止生成', exact: true }).isEnabled(), 'stop can retry after network failure')
    cancelStatus = 200
    await command('/help')
    await composer.fill('Next draft remains after stopping')
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    await waitFor(() => cancellations.length === 2, 'server stop requested')
    assert.deepEqual(cancellations, [{ id: firstId, method: 'POST' }, { id: firstId, method: 'POST' }])
    assert.equal(await page.getByRole('button', { name: '停止生成', exact: true }).isDisabled(), true)
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
    await page.locator('.el-message-box').getByText('重新提问会重新执行本轮操作。若上次已调用外部写入工具，可能造成重复操作。', { exact: true }).waitFor()
    await page.locator('.el-message-box').getByRole('button', { name: '取消', exact: true }).click()
    assert.equal(foregroundCreates, 1, 'canceling the duplicate-operation warning does not resubmit')
    await page.reload()
    await page.getByText('Question c_a', { exact: true }).waitFor()
    await page.getByText('本轮已停止，已保留部分回答。', { exact: true }).waitFor()
    await page.getByRole('button', { name: '重新提问', exact: true }).waitFor()
    assert.match(await page.locator('.markdown-answer').last().textContent(), /Partial answer before stopping/)
    const recoveryBar = page.locator('.foreground-recovery')
    await recoveryBar.scrollIntoViewIfNeeded()
    await page.screenshot({ path: path.join(output, 'foreground-recovery-desktop.png') })
    await page.setViewportSize({ width: 1100, height: 700 })
    await recoveryBar.scrollIntoViewIfNeeded()
    const recoveryBounds = await recoveryBar.boundingBox()
    assert.ok(recoveryBounds && recoveryBounds.x >= 0 && recoveryBounds.x + recoveryBounds.width <= 1100)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: path.join(output, 'foreground-recovery-small-desktop.png') })
    await page.setViewportSize({ width: 1440, height: 900 })

    await command('Second turn, cancelled on conversation change')
    await waitFor(() => page.evaluate(() => window.chatStreams.length === 1), 'second stream started after reload')
    const secondId = await page.evaluate(() => window.chatStreams[0].request.run_id)
    assert.notEqual(firstId, secondId)
    await composer.fill('New draft in A')
    await switchTo('Conversation B')
    await waitFor(() => cancellations.some((entry) => entry.id === secondId), 'conversation switch sends server cancel')
    assert.equal(await composer.inputValue(), 'Draft B belongs to B')
    assert.equal(await page.evaluate(() => window.chatStreams[0].closed), true, 'old browser stream aborted')

    await page.setViewportSize({ width: 1100, height: 700 })
    await switchTo('Conversation A')
    assert.equal(await composer.inputValue(), 'New draft in A')
    await page.locator('.chat-scroll').evaluate((el) => { el.scrollTop = 100; el.dispatchEvent(new Event('scroll')) })
    const jump = page.getByRole('button', { name: '回到最新消息', exact: true })
    await jump.waitFor()
    const bounds = await jump.boundingBox()
    assert.ok(bounds && bounds.x > 0 && bounds.x + bounds.width <= 1100 && bounds.y + bounds.height < 700)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await waitFor(async () => await page.locator('.el-message').count() === 0, 'temporary messages dismissed')
    await page.screenshot({ path: path.join(output, 'chat-reading-small-desktop.png'), fullPage: true })
    foregroundCreateStatus = 500
    await composer.fill('Keep this draft when checkpoint creation fails')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await page.getByText('检查点创建失败', { exact: true }).waitFor()
    assert.equal(await composer.inputValue(), 'Keep this draft when checkpoint creation fails')
    assert.equal(foregroundCreates, 2, 'failed checkpoint creation starts no chat stream')
    assert.deepEqual(errors, [])
    console.log('PASS: foreground checkpoints, partial-answer persistence, recovery warning, remote-run lock and status, actionable remote approvals/arguments, duplicate guard/expiry, creation-failure draft retention, per-conversation drafts, reading position, cancellation, queued commands, desktop bounds')
  } finally {
    releaseApproval?.()
    await browser.close()
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
