// Actual Vue + isolated HTTP API. All budgets are synthetic test fixtures.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, spawnSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) {
  const end = Date.now() + timeout
  while (!(await check())) { assert.ok(Date.now() < end, label); await delay(80) }
}
async function port() {
  const server = net.createServer()
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const value = server.address().port
  await new Promise(resolve => server.close(resolve))
  return value
}
async function main() {
  const output = path.join(root, 'verification/resource-governance/e2e')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'), LLM_API_KEY: '', EMBEDDING_API_KEY: '',
    LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  let backend, vite, browser, page, logs = ''
  const checks = [], errors = [], external = []
  const check = (name) => checks.push({ name, passed: true })
  try {
    const backendPort = await port()
    backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', d => { logs += d })
    backend.stderr.on('data', d => { logs += d })
    const base = 'http://127.0.0.1:' + backendPort
    const api = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
      const response = await fetch(base + '/api' + route, { method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) })
      assert.equal(response.status, 200, route + ': ' + await response.clone().text())
      return response.json()
    }
    await until(async () => {
      if (backend.exitCode !== null) throw Error(logs)
      try { return (await fetch(base + '/api/health')).ok } catch { return false }
    }, 'backend startup')
    const cid = (await api('/conversations', { title: '预算交互合成验收' })).id
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error', server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } } } })
    await vite.listen()
    const ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.on('pageerror', err => errors.push(err.message))
    page.setDefaultTimeout(15000)
    await page.goto(ui + '/chat?conv=' + cid)
    await page.locator('textarea.textarea').waitFor()
    const open = async () => {
      await page.locator('.resource-toggle').click()
      await page.getByLabel('下一任务费用预算', { exact: true }).waitFor()
    }
    await open()
    await page.getByLabel('下一任务费用预算', { exact: true }).fill('2')
    await page.getByLabel('每次请求预留估值', { exact: true }).fill('0.05')
    await page.getByLabel('下一任务请求执行时间', { exact: true }).fill('3')
    await page.getByRole('button', { name: '保存下一任务预算', exact: true }).click()
    await until(async () => (await api('/conversation-resources/' + cid)).revision === 1, 'save resource defaults')
    const saved = await api('/conversation-resources/' + cid)
    assert.equal(saved.policy.active_seconds_limit, 180)
    assert.equal(saved.policy.request_estimate, '0.05')
    check('Composer saves an opt-in money/time policy to the real API')
    await page.reload()
    await open()
    assert.equal(await page.getByLabel('下一任务费用预算', { exact: true }).inputValue(), '2')
    assert.equal(await page.getByLabel('下一任务请求执行时间', { exact: true }).inputValue(), '3')
    check('Reload preserves saved controls')
    await page.getByLabel('下一任务费用预算', { exact: true }).fill('0.01')
    await page.getByRole('button', { name: '保存下一任务预算', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: '单次预留估值不能高于' }).waitFor()
    assert.equal((await api('/conversation-resources/' + cid)).policy.amount_limit, '2')
    check('Invalid reservation estimate leaves the previous policy intact')
    await api('/conversation-resources/' + cid, { expected_revision: 1, active_seconds_limit: 240 }, 'PUT')
    await page.getByLabel('下一任务费用预算', { exact: true }).fill('3')
    await page.getByRole('button', { name: '保存下一任务预算', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: '已变化' }).waitFor()
    assert.equal(await page.getByLabel('下一任务费用预算', { exact: true }).inputValue(), '3')
    await page.getByRole('button', { name: '重新读取', exact: true }).click()
    await until(() => page.getByRole('alert').count().then(n => n === 0), 'read new revision')
    await page.getByRole('button', { name: '保存下一任务预算', exact: true }).click()
    await until(async () => (await api('/conversation-resources/' + cid)).revision === 3, 'retry resource save')
    check('Revision conflict preserves input and permits an explicit retry')
    const seed = spawnSync(python, ['-c', [
      'import sys',
      'from app.db import SessionLocal',
      'from app.runtime_models import RuntimeRun',
      'from app.resource_budget import create_budget,reserve,bind_call,finish',
      'with SessionLocal.begin() as db: db.add(RuntimeRun(id="budget-ui-run",kind="foreground",conversation_id=sys.argv[1],status="paused"))',
      'identity=create_budget(owner_run_id="budget-ui-run",amount_limit="2",request_estimate=".2",price_version="synthetic-fixture",active_seconds_limit=60)',
      'reservation=reserve(identity,run_id="budget-ui-run")',
      'bind_call(reservation,"synthetic-pending-call")',
      'finish(reservation,active_seconds=12)',
    ].join('\n'), cid], { cwd: path.join(root, 'backend'), env, windowsHide: true, encoding: 'utf8' })
    assert.equal(seed.status, 0, seed.stderr)
    await page.getByRole('button', { name: '关闭任务预算', exact: true }).click()
    await open()
    await page.getByText('1 次请求费用待核对', { exact: false }).waitFor()
    assert.equal((await api('/conversation-resources/' + cid)).budgets[0].reserved_amount, '0.2')
    await page.getByLabel('调整当前任务费用预算', { exact: true }).fill('4')
    await page.getByLabel('调整当前任务请求时间', { exact: true }).fill('5')
    await page.getByRole('button', { name: '调整该任务预算', exact: true }).click()
    await until(async () => (await api('/conversation-resources/' + cid)).budgets[0].amount_limit === '4', 'adjust same task')
    const current = await api('/conversation-resources/' + cid)
    assert.equal(current.policy.amount_limit, '3')
    assert.equal(current.budgets[0].reserved_amount, '0.2')
    assert.equal(current.budgets[0].active_seconds_limit, 300)
    check('Adjusting an existing task keeps unknown reservations and next-task defaults')
    await page.screenshot({ path: path.join(output, 'budget-desktop.png'), fullPage: true })
    await page.keyboard.press('Escape')
    await until(() => page.locator('.resource-toggle').evaluate(el => el === document.activeElement), 'Escape restores focus')
    await page.setViewportSize({ width: 390, height: 844 })
    await open()
    await until(async () => {
      const box = await page.locator('.resource-panel').boundingBox()
      return box && box.x >= 0 && box.x + box.width <= 391 && box.y >= 0
    }, 'narrow panel remains inside viewport')
    await page.screenshot({ path: path.join(output, 'budget-narrow.png'), fullPage: true })
    check('Keyboard close returns focus; narrow viewport keeps the panel reachable')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({
      success: true, checks, browser_errors: errors, external_requests: external,
      real_provider_calls: 0, fixture: 'isolated local database and synthetic pending charges',
    }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: false, checks,
      error: String(error), browser_errors: errors, external_requests: external, real_provider_calls: 0 }, null, 2))
    throw error
  } finally {
    if (browser) await browser.close()
    if (vite) await vite.close()
    if (backend && backend.exitCode === null) backend.kill()
  }
}
main().catch(error => { process.stderr.write(String(error)); process.exitCode = 1 })

