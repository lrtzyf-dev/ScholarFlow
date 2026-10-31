// Actual SQLite/SSE/HTTP with two desktop viewports and a cold origin restart.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, name) {
  const deadline = Date.now() + 18000
  while (!await fn()) { assert.ok(Date.now() < deadline, `Timed out: ${name}`); await pause(60) }
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/plan-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', MOCK_STREAM_DELAY: '0.002' }
  let child, base, log = ''
  async function launch() {
    const reservation = net.createServer()
    await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
    const port = reservation.address().port
    await new Promise((resolve) => reservation.close(resolve))
    child = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
      cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    })
    child.stdout.on('data', (s) => { log += s })
    child.stderr.on('data', (s) => { log += s })
    base = `http://127.0.0.1:${port}`
    await until(async () => { if (child.exitCode !== null) throw new Error(log); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'server startup')
  }
  function stop() {
    if (!child || child.exitCode !== null) return
    execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    child = null
  }
  async function api(url, body, method = 'POST') {
    const response = await fetch(base + '/api' + url, body === undefined ? {} : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    const value = await response.json()
    assert.ok(response.ok, `${url}: ${response.status} ${JSON.stringify(value)}`)
    return value
  }
  const browser = await chromium.launch({ headless: true })
  const errors = [], submissions = []
  try {
    await launch()
    const cid = (await api('/conversations', { title: '计划模式桌面验收' })).id
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const other = await browser.newPage({ viewport: { width: 1100, height: 700 } })
    for (const p of [page, other]) {
      p.on('pageerror', (e) => errors.push(e.message))
      p.on('request', (r) => { if (r.url().endsWith('/chat/stream')) submissions.push(r.postDataJSON()) })
    }
    await page.goto(`${base}/chat?conv=${cid}`)
    const modes = (p) => p.getByRole('group', { name: '工作模式' })
    await modes(page).getByRole('button', { name: '计划', exact: true }).click()
    await until(async () => (await api(`/desktop-state/conversations/${cid}`)).value.execution_mode === 'plan', 'persisted plan selection')
    await page.reload()
    await until(async () => await modes(page).getByRole('button', { name: '计划', exact: true }).getAttribute('aria-pressed') === 'true', 'mode restored')
    await page.locator('textarea.textarea').fill('/execute')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    assert.equal(submissions.length, 0, 'a bare mode command does not start a run')
    assert.equal(await modes(page).getByRole('button', { name: '执行', exact: true }).getAttribute('aria-pressed'), 'true')
    await page.locator('input[type="file"][accept="image/*"]').setInputFiles({
      name: 'reference.png', mimeType: 'image/png',
      buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZkAAAAASUVORK5CYII=', 'base64'),
    })
    await page.locator('.image-preview img').waitFor()
    await page.locator('textarea.textarea').fill('/plan 先规划一篇关于注意力机制的综述')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await page.getByRole('button', { name: '执行此计划', exact: true }).waitFor()
    await until(async () => await page.getByRole('button', { name: '执行此计划', exact: true }).isEnabled(), 'plan delivered')
    assert.equal(submissions.length, 1)
    assert.equal(submissions[0].execution_mode, 'plan')
    let history = await api(`/conversations/${cid}`)
    assert.equal(history.messages.length, 1)
    const planId = history.messages[0].id
    assert.ok(history.messages[0].plan_steps.every((s) => s.status === 'pending'))
    assert.equal(history.messages[0].plan_run_id, null)
    assert.equal(history.messages[0].plan_images.length, 1)
    await page.reload()
    await page.getByRole('button', { name: '执行此计划', exact: true }).waitFor()
    await other.goto(`${base}/chat?conv=${cid}`)
    await other.getByRole('button', { name: '执行此计划', exact: true }).waitFor()
    await other.route(`**/api/conversations/${cid}`, (route) => route.fulfill({ json: history }))
    await page.locator('textarea.textarea').fill('这条草稿应在审阅与执行后保留')
    for (const [p, name] of [[page, 'desktop'], [other, 'small-desktop']]) {
      await p.screenshot({ path: path.join(root, `verification/plan-${name}.png`), fullPage: true, animations: 'disabled' })
      const layout = await p.evaluate(() => {
        const bad = [...document.querySelectorAll('.execution-mode button, .execute-plan, .tools')].filter((el) => el.scrollWidth > el.clientWidth + 2).map((el) => el.className)
        return { width: document.documentElement.scrollWidth, viewport: innerWidth, bad }
      })
      assert.ok(layout.width <= layout.viewport, JSON.stringify(layout))
      assert.deepEqual(layout.bad, [])
    }
    await page.getByRole('button', { name: '执行此计划', exact: true }).click()
    await until(async () => (await api(`/conversations/${cid}`)).messages.length === 2, 'reviewed plan execution')
    assert.equal(await page.locator('textarea.textarea').inputValue(), '这条草稿应在审阅与执行后保留')
    assert.equal(submissions.length, 2)
    assert.equal(submissions[1].execution_mode, 'execute')
    assert.equal(submissions[1].reviewed_plan_id, planId)
    assert.deepEqual(submissions[1].images, history.messages[0].plan_images, 'history retains the planned image context')
    // The stale second window still shows its original action; the database rejects it.
    let duplicateStatus
    await other.route('**/api/foreground-runs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      const response = await route.fetch()
      duplicateStatus = response.status()
      await other.unroute(`**/api/conversations/${cid}`)
      await route.fulfill({ response })
    })
    await other.getByRole('button', { name: '执行此计划', exact: true }).click()
    await until(async () => await other.getByRole('button', { name: '执行此计划', exact: true }).count() === 0, 'stale claim refreshed')
    assert.equal(duplicateStatus, 409, 'stale plan admission was actually rejected by the backend')
    await other.unroute('**/api/foreground-runs')
    assert.equal((await api(`/conversations/${cid}`)).messages.length, 2)
    assert.equal(submissions.length, 2, 'second window must not submit another stream')
    // A failed transport leaves a durable failed run and an explicit retry.
    const failureCid = (await api('/conversations', { title: '计划执行失败与重试' })).id
    await page.goto(`${base}/chat?conv=${failureCid}`)
    await page.locator('textarea.textarea').fill('/plan 规划验证步骤')
    await page.locator('textarea.textarea').press('Enter')
    await until(async () => await page.getByRole('button', { name: '执行此计划', exact: true }).isEnabled().catch(() => false), 'slash plan')
    const sourceId = (await api(`/conversations/${failureCid}`)).messages[0].id
    let failedRun
    await page.route('**/api/chat/stream', async (route) => {
      failedRun = route.request().postDataJSON().run_id
      return route.fulfill({ status: 503, json: { detail: 'Injected transport failure' } })
    })
    await page.getByRole('button', { name: '执行此计划', exact: true }).click()
    await until(async () => Boolean(failedRun), 'injected stream failure')
    await until(async () => {
      const run = (await api(`/foreground-runs?conversation_id=${failureCid}`)).runs.find((r) => r.id === failedRun)
      const response = await fetch(base + `/api/foreground-runs/${failedRun}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: run.revision, status: 'failed', error: 'Injected transport failure' }),
      })
      assert.ok(response.ok || response.status === 409)
      return response.ok
    }, 'fault committed after periodic checkpoint')
    await until(async () => await page.getByRole('button', { name: '重新提问', exact: true }).isEnabled().catch(() => false), 'failed retry action')
    await page.unroute('**/api/chat/stream')
    await page.locator('textarea.textarea').fill('重试时保留的草稿')
    await page.getByRole('button', { name: '重新提问', exact: true }).click()
    await page.locator('.el-message-box').getByRole('button', { name: '重新提问', exact: true }).click()
    await until(async () => (await api(`/conversations/${failureCid}`)).messages.length === 2, 'explicit reviewed retry')
    const retry = submissions.at(-1)
    assert.equal(retry.reviewed_plan_id, sourceId)
    assert.equal(retry.retry_plan_run_id, failedRun)
    assert.equal(retry.execution_mode, 'execute')
    assert.equal(await page.locator('textarea.textarea').inputValue(), '重试时保留的草稿')
    // Background planning is visibly a plan and can be opened for review.
    const bgCid = (await api('/conversations', { title: '后台规划' })).id
    await page.goto(`${base}/chat?conv=${bgCid}`)
    await page.locator('textarea.textarea').fill('/plan 规划后台文献整理')
    await page.locator('button.bg-btn').click()
    await page.getByText('计划已生成', { exact: true }).waitFor()
    await page.getByRole('button', { name: '查看对话', exact: true }).first().click()
    await page.getByRole('button', { name: '执行此计划', exact: true }).waitFor()
    await page.locator('textarea.textarea').fill('跨端口保留计划模式和草稿')
    await until(async () => (await api(`/desktop-state/conversations/${bgCid}`)).value.text === '跨端口保留计划模式和草稿', 'cold draft saved')
    const before = base
    await page.context().close()
    await other.context().close()
    stop()
    await launch()
    assert.notEqual(base, before)
    const cold = await browser.newPage({ viewport: { width: 1100, height: 700 } })
    cold.on('pageerror', (e) => errors.push(e.message))
    await cold.goto(base + '/chat')
    await until(async () => await cold.locator('textarea.textarea').inputValue() === '跨端口保留计划模式和草稿', 'cold draft restored')
    assert.equal(await modes(cold).getByRole('button', { name: '计划', exact: true }).getAttribute('aria-pressed'), 'true')
    await cold.getByRole('button', { name: '执行此计划', exact: true }).waitFor()
    await cold.locator('textarea.textarea').fill('/execute 核对执行命令参数')
    await cold.locator('textarea.textarea').press('Enter')
    await until(async () => (await api(`/conversations/${bgCid}`)).messages.length === 2, 'execute command argument')
    const last = (await api(`/conversations/${bgCid}`)).messages.at(-1)
    assert.equal(last.question, '核对执行命令参数')
    assert.equal(last.execution_mode, 'execute')
    assert.deepEqual(errors, [])
    console.log('PASS: plan restrictions contract, review/execute, two-window claims, failed retry, background review, cold mode/draft restore; 1440x900 and 1100x700')
  } finally {
    await browser.close()
    stop()
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1 })
