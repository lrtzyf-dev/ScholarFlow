const assert = require('node:assert/strict')
const stateMock = require('./desktop-state-mock.cjs')()
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'
const output = path.resolve(__dirname, '../../verification')
const makeTask = (id, title, recovery) => ({ id, title, type: 'agent', status: 'interrupted',
  progress: 0.5, plan: [{ title: 'Search', status: 'done' }, { title: 'Write', status: 'pending' }],
  events: [{ kind: 'tool_result', name: 'retained search' }], approvals: [], result: null,
  error: '', conversation_id: 'c_a', recovery })

async function main() {
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = [], requests = []
  let resumeRelease, resumeStatus = 200
  let snapshot = [
    makeTask('uncertain', 'Interrupted report write', { available: true, step: 2, completed_tools: 3,
      pending_operation: { kind: 'tool', name: 'write_report', arguments: { path: 'reports/research-report.md', overwrite: true } } }),
    makeTask('ready', 'Ready to continue', { available: true, step: 3, completed_tools: 4, pending_operation: null }),
    makeTask('legacy', 'Old task without checkpoint', { available: false, reason: '此任务没有执行检查点，可选择从头重新运行' }),
  ]
  page.on('pageerror', (e) => errors.push(e.message))
  await page.addInitScript(() => {
    window.sources = []
    window.EventSource = class extends EventTarget {
      constructor() { super(); window.sources.push(this); setTimeout(() => this.onopen?.(), 0) }
      close() { this.closed = true }
    }
    window.emitTasks = (tasks) => window.sources.forEach((source) => {
      if (!source.closed) source.dispatchEvent(new MessageEvent('tasks', { data: JSON.stringify({ tasks }) }))
    })
  })
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const request = route.request(), url = new URL(request.url())
    const json = (body, status = 200) => route.fulfill({ json: body, status })
    if (url.pathname === '/api/tasks') return json({ tasks: snapshot })
    if (url.pathname === '/api/conversations') return json({ conversations: [] })
    const match = url.pathname.match(/^\/api\/tasks\/(\w+)\/(resume|restart)$/)
    if (match) {
      const [, id, action] = match
      requests.push({ id, action, method: request.method(), body: request.postData() ? request.postDataJSON() : null })
      if (action === 'resume') {
        await new Promise((resolve) => { resumeRelease = resolve })
        if (resumeStatus !== 200) return json({ detail: '该步骤已不需要处理，请刷新任务状态' }, resumeStatus)
        snapshot = snapshot.map((t) => t.id === id ? { ...t, status: 'running' } : t)
        return json(snapshot.find((t) => t.id === id))
      }
      const next = { ...structuredClone(snapshot.find((t) => t.id === id)), id: 'new_task', title: 'New run', status: 'pending', recovery: { available: false } }
      snapshot = [next, ...snapshot]
      return json(next)
    }
    return json({})
  })
  const waitFor = async (predicate, label) => {
    const start = Date.now()
    while (!await predicate()) {
      assert.ok(Date.now() - start < 8000, `Timed out: ${label}`)
      await page.waitForTimeout(40)
    }
  }
  const card = (title) => page.locator('.task-card').filter({ has: page.locator('.task-title', { hasText: title }) })
  try {
    await page.goto(`${baseURL}/tasks`)
    const uncertain = card('Interrupted report write')
    await uncertain.getByText('工具 · write_report', { exact: true }).waitFor()
    assert.match(await uncertain.textContent(), /是否已完成无法确认/)
    assert.match(await uncertain.locator('.task-recovery pre').textContent(), /research-report.md/)
    assert.equal(await uncertain.getByRole('button', { name: '从检查点继续', exact: true }).count(), 0)
    assert.equal(await card('Old task without checkpoint').getByRole('button', { name: '从检查点继续', exact: true }).count(), 0)
    assert.equal(await card('Old task without checkpoint').getByRole('button', { name: '从头重新运行', exact: true }).count(), 1)
    await page.screenshot({ path: path.join(output, 'task-recovery-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 1100, height: 700 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    for (const button of await uncertain.locator('.task-actions button').all()) {
      const bounds = await button.boundingBox()
      assert.ok(bounds && bounds.x > 0 && bounds.x + bounds.width <= 1100)
    }
    await page.screenshot({ path: path.join(output, 'task-recovery-small-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 1440, height: 900 })
    const ready = card('Ready to continue')
    await ready.getByRole('button', { name: '从检查点继续', exact: true }).click()
    await waitFor(() => requests.length === 1 && resumeRelease, 'resume request')
    assert.deepEqual(requests[0], { id: 'ready', action: 'resume', method: 'POST', body: {} })
    assert.equal(await ready.getByRole('button', { name: '从检查点继续', exact: true }).isDisabled(), true)
    assert.equal(await ready.getByRole('button', { name: '从头重新运行', exact: true }).isDisabled(), true)
    resumeRelease()
    await ready.getByText('运行中', { exact: true }).waitFor()
    assert.match(await ready.textContent(), /retained search/)
    assert.match(await ready.textContent(), /50%/)
    assert.equal(await ready.getByRole('button', { name: '从检查点继续', exact: true }).count(), 0)

    await uncertain.getByRole('button', { name: '跳过这一步', exact: true }).click()
    await waitFor(() => requests.length === 2, 'skip request')
    assert.deepEqual(requests[1].body, { resolution: 'skip' })
    resumeRelease()
    await uncertain.getByText('运行中', { exact: true }).waitFor()

    snapshot[0].status = 'interrupted'
    await page.evaluate((tasks) => window.emitTasks(tasks), snapshot)
    await uncertain.getByRole('button', { name: '重试这一步', exact: true }).click()
    await waitFor(() => requests.length === 3, 'retry request')
    assert.deepEqual(requests[2].body, { resolution: 'retry' })
    resumeStatus = 409
    resumeRelease()
    await page.getByText('该步骤已不需要处理，请刷新任务状态', { exact: true }).waitFor()
    await waitFor(() => uncertain.getByRole('button', { name: '重试这一步', exact: true }).isEnabled(), 'decision restored after conflict')

    await card('Old task without checkpoint').getByRole('button', { name: '从头重新运行', exact: true }).click()
    await card('New run').getByText('排队中', { exact: true }).waitFor()
    assert.equal(await card('Old task without checkpoint').count(), 1, 'restart retains original task')
    assert.deepEqual(requests[3], { id: 'legacy', action: 'restart', method: 'POST', body: null })
    assert.deepEqual(errors, [])
    console.log('PASS: checkpoint continue, duplicate submission guard, unknown-result skip/retry, conflict refresh, separate restart keeps original task, legacy task boundary, 1440/1100 desktop layout')
  } finally {
    resumeRelease?.()
    await browser.close()
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
