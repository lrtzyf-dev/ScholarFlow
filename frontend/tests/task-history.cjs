const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs/promises')
const stateMock = require('./desktop-state-mock.cjs')()
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'
const output = path.resolve(__dirname, '../../verification')
const task = (id, status = 'done') => ({ id, type: 'agent', title: id, status, progress: 1,
  plan: [], events: [], approvals: [], result: null, error: '', conversation_id: 'c_history',
  updated_at: new Date().toISOString() })
const archive = Array.from({ length: 65 }, (_, i) => task(`Archive ${String(64 - i).padStart(2, '0')}`))

async function main() {
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', (error) => errors.push(error.message))
  const active = task('Old active task', 'running')
  active.approvals = [{ approval_id: 'old_approval', tool_name: 'write_report', arguments: {}, risk: 'high', reason: '' }]
  const finishing = { ...task('Thread still finishing', 'interrupted'), executing: true }
  let snapshot = { tasks: [active, finishing, ...archive.slice(0, 30)], next_cursor: 'page2', history_total: 65, history_epoch: '0:' }
  let historyRequests = 0, failPage = true, releasePage
  await page.addInitScript(() => {
    window.sources = []
    window.EventSource = class extends EventTarget {
      constructor() { super(); window.sources.push(this); setTimeout(() => this.onopen?.(), 0) }
      close() { this.closed = true }
    }
    window.emitTasks = (payload) => window.sources.forEach((source) => {
      if (!source.closed) source.dispatchEvent(new MessageEvent('tasks', { data: JSON.stringify(payload) }))
    })
  })
  await page.route('**/api/**', async (route) => {
    if (await stateMock(route)) return
    const url = new URL(route.request().url())
    if (url.pathname === '/api/tasks') return route.fulfill({ json: snapshot })
    if (url.pathname === '/api/tasks/history') {
      historyRequests++
      const second = url.searchParams.get('cursor') === 'page2'
      if (failPage) return route.fulfill({ status: 503, json: { detail: 'Injected history failure' } })
      if (second) await new Promise((resolve) => { releasePage = resolve })
      return route.fulfill({ json: { tasks: second ? archive.slice(30, 60) : archive.slice(60),
        next_cursor: second ? 'page3' : null, history_total: 64 } })
    }
    if (url.pathname === '/api/conversations') return route.fulfill({ json: { conversations: [] } })
    if (url.pathname === '/api/conversations/page') return route.fulfill({ json: { conversations: [], total: 0, pinned_total: 0, next_cursor: null } })
    return route.fulfill({ json: {} })
  })
  const waitFor = async (fn, label) => {
    const end = Date.now() + 8000
    while (!await fn()) { assert.ok(Date.now() < end, `Timed out: ${label}`); await page.waitForTimeout(30) }
  }
  const card = (title) => page.locator('.task-card').filter({ has: page.locator('.task-title', { hasText: title }) })
  try {
    await page.goto(`${baseURL}/tasks`)
    await card('Old active task').getByText('等待审批', { exact: true }).waitFor()
    await page.getByRole('tab', { name: /^进行中/ }).click()
    assert.equal(await page.locator('.task-card').count(), 2)
    assert.equal(await card('Thread still finishing').getByText('正在结束', { exact: true }).count(), 1)
    assert.equal(await card('Thread still finishing').getByRole('button', { name: '从头重新运行' }).count(), 0)
    await page.screenshot({ path: path.join(output, 'tasks-active-desktop.png'), fullPage: true })
    await page.getByRole('tab', { name: /^进行中/ }).press('ArrowRight')
    assert.equal(await page.getByRole('tab', { name: /^历史/ }).getAttribute('aria-selected'), 'true')
    assert.equal(await page.locator('.task-card').count(), 30)
    await page.getByRole('button', { name: '加载更早任务', exact: true }).click()
    await page.getByText('无法加载更早的任务，请重试', { exact: true }).waitFor()
    assert.equal(await page.locator('.task-card').count(), 30, 'failed pagination preserves loaded history')
    failPage = false
    await page.getByRole('button', { name: '重试加载', exact: true }).click()
    await waitFor(() => releasePage, 'history request held')
    assert.equal(await page.getByRole('button', { name: '加载中…', exact: true }).isDisabled(), true)
    assert.equal(historyRequests, 2, 'one retry request')
    snapshot = { tasks: [{ ...active, status: 'done', approvals: [], result: { answer: 'Completed old task' } }, finishing,
      { ...archive[34], status: 'running' }, ...archive.slice(0, 29)], next_cursor: 'changed-page-boundary', history_total: 65 }
    await page.evaluate((payload) => window.emitTasks(payload), snapshot)
    releasePage()
    await page.getByRole('button', { name: '加载更早任务', exact: true }).waitFor()
    assert.equal(await card('Archive 35').count(), 1, 'SSE page shift retains the old boundary item')
    assert.equal(await page.getByRole('tab', { name: /^历史/ }).locator('span').textContent(), '65', 'newer SSE totals win over stale history response')
    assert.equal(await card('Archive 30').count(), 0, 'stale history cannot overwrite a running task')
    await page.getByRole('tab', { name: /^全部/ }).click()
    assert.equal(await card('Archive 30').getByText('运行中', { exact: true }).count(), 1)
    assert.equal(await card('Archive 30').count(), 1, 'same task only appears once')
    await page.getByRole('button', { name: '加载更早任务', exact: true }).click()
    await waitFor(async () => await page.locator('.task-card').count() === 67, 'all archive pages')
    assert.equal(await page.getByRole('button', { name: '加载更早任务', exact: true }).count(), 0)
    await page.evaluate((payload) => window.emitTasks(payload), snapshot)
    assert.equal(await page.locator('.el-notification').count(), 1, 'old completion notification deduplicated')
    snapshot.history_epoch = '1:deleted-conversation'
    snapshot.history_total = 64
    await page.evaluate((payload) => window.emitTasks(payload), snapshot)
    assert.equal(await card('Archive 00').count(), 0, 'deletion invalidates cached archive pages across windows')
    await page.locator('.tasks-page').evaluate((el) => { el.scrollTop = 0 })
    await page.setViewportSize({ width: 1100, height: 700 })
    await page.getByRole('tab', { name: /^进行中/ }).click()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    await page.screenshot({ path: path.join(output, 'tasks-active-small-desktop.png'), fullPage: true })
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await page.getByRole('tab', { name: /^历史/ }).click()
    await waitFor(async () => await page.locator('.task-card').count() === 30, 'refresh resets archive pages')
    snapshot.next_cursor = 'page2'
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    releasePage = null
    await page.getByRole('button', { name: '加载更早任务', exact: true }).click()
    await waitFor(() => releasePage, 'page before reset')
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    releasePage()
    await page.waitForTimeout(100)
    assert.equal(await page.locator('.task-card').count(), 30, 'response from a cleared archive cannot reinsert pages')
    assert.deepEqual(errors, [])
    console.log('PASS: active/finishing task views, keyboard tabs, history error/retry, cursor pagination, SSE boundary and totals, live-over-stale state, deletion invalidation, notification deduplication, concurrent refresh reset, desktop bounds')
  } finally {
    releasePage?.()
    await browser.close()
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
