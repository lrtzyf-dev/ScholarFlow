// Real Vue/HTTP/SQLite ownership and grant endpoints; controlled unknowns, no live API.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'), pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(fn, label, timeout = 45000) { const end = Date.now() + timeout; while (!(await fn())) { assert.ok(Date.now() < end, label); await pause(80) } }
async function port() { const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const value = server.address().port; await new Promise(resolve => server.close(resolve)); return value }
async function main() {
  const out = path.join(root, 'verification/research-children/frontend'); await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1', TEMP: isolated, TMP: isolated }
  const checks = [], errors = [], external = [], mutations = []
  let backend, vite, browser, page, logs = '', base, ui
  const check = name => checks.push({ name, passed: true })
  async function start() {
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'research_child_frontend_fixture:app', '--app-dir', 'tests', '--host', '127.0.0.1', '--port', new URL(base).port],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', data => { logs += data }); backend.stderr.on('data', data => { logs += data })
    await until(async () => { if (backend.exitCode !== null) throw Error(logs); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'isolated backend startup')
  }
  async function stop() { if (backend && backend.exitCode === null) { const done = new Promise(resolve => backend.once('exit', resolve)); backend.kill(); await done } }
  async function api(route, body) {
    const response = await fetch(base + '/api' + route, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000) })
    assert.equal(response.status, 200, route + ': ' + await response.clone().text() + '\n' + logs.slice(-7000)); return response.json()
  }
  const seed = () => api('/test-fixture/research-child/seed', {})
  const stats = () => api('/test-fixture/research-child/stats')
  const advance = value => api('/test-fixture/research-child/' + value.child_id + '/advance', {})
  const region = target => target.getByRole('region', { name: '科研子任务与费用确认', exact: true })
  async function open(target, value, navigate = true) {
    if (navigate) await target.goto(ui + '/tasks')
    const row = target.locator('[data-run-center-id="run:' + value.child_id + '"]')
    await row.getByRole('button', { name: '科研子任务与恢复', exact: true }).click()
    await until(async () => (await region(target).innerText()).includes(value.parent_id), 'exact parent child panel')
    return region(target)
  }
  try {
    base = 'http://127.0.0.1:' + await port(); await start()
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false } } } })
    await vite.listen(); ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', item => { const url = new URL(item.request().url()); if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return item.continue(); external.push(url.origin + url.pathname); return item.abort() })
    async function newPage() { const target = await context.newPage(); target.setDefaultTimeout(20000); target.on('pageerror', error => errors.push(error.message)); target.on('request', request => { if (request.method() === 'POST') mutations.push({ url: new URL(request.url()).pathname, body: request.postDataJSON() }) }); return target }
    page = await newPage()
    let sample = await seed(), panel = await open(page, sample.A)
    const grantButton = () => region(page).getByRole('button', { name: '保存本次单次许可', exact: true })
    assert.ok(await grantButton().isDisabled()); assert.ok((await panel.innerText()).includes('未知不等于免费'))
    const before = await stats()
    await panel.getByRole('checkbox').check()
    let committed = false
    const lost = async item => { const response = await item.fetch(); assert.equal(response.status(), 200); committed = true; await item.abort('failed') }
    const grantPath = '**/api/research/children/' + sample.A.child_id + '/retry-grants'
    await page.route(grantPath, lost); await grantButton().click()
    await until(() => committed, 'grant committed before response loss')
    await panel.getByText('原许可的提交结果待确认', { exact: true }).waitFor()
    const original = mutations.find(item => item.url.endsWith('/retry-grants')).body
    assert.equal(original.parent_run_id, sample.A.parent_id); assert.equal(original.conversation_id, sample.A.cid)
    assert.match(original.expected_parent_attempt_id, /^[a-f0-9]{32}$/); assert.match(original.expected_child_attempt_id, /^[a-f0-9]{32}$/)
    assert.match(original.pending_sha256, /^[a-f0-9]{64}$/); assert.equal(original.confirm_duplicate_cost, true)
    assert.equal((await stats()).grants, before.grants + 1)
    await page.unroute(grantPath, lost); const posts = mutations.filter(item => item.url.endsWith('/retry-grants')).length
    await page.reload(); panel = await open(page, sample.A, false)
    await panel.getByText('原许可的提交结果待确认', { exact: true }).waitFor(); await pause(150)
    assert.equal(mutations.filter(item => item.url.endsWith('/retry-grants')).length, posts)
    await stop(); await start()
    await panel.getByRole('button', { name: '核对原许可结果', exact: true }).click()
    await panel.getByText(/许可已保存，仅针对/).waitFor()
    const replay = mutations.filter(item => item.url.endsWith('/retry-grants')).at(-1).body
    assert.deepEqual(replay, original); const after = await stats()
    assert.equal(after.grants, before.grants + 1); assert.equal(after.attempts, before.attempts); assert.equal(after.ledger_calls, 0)
    assert.ok(!mutations.some(item => item.url === '/api/chat/stream' || /\/tasks\/[^/]+\/resume$/.test(item.url)))
    check('Exact human grant requires confirmation; lost reply, reload and backend restart reuse one request without dispatching or adding an attempt')

    // Controlled local pending records model two tabs having persisted different
    // exact human requests. Resolving either must not delete its neighbour.
    const pendingOriginal = { ...original, child_run_id: sample.A.child_id }
    const sibling = { ...pendingOriginal, request_id: await page.evaluate(() => crypto.randomUUID()) }
    const localPrefix = 'scholarflow:research-child-grant:' + sample.A.cid + ':' + sample.A.parent_id
    const storagePeer = await newPage(); await storagePeer.goto(ui + '/tasks')
    await storagePeer.evaluate(({ key, one, two }) => {
      localStorage.setItem(key, '{broken legacy fixture')
      localStorage.setItem(key + ':' + one.request_id, JSON.stringify(one))
      localStorage.setItem(key + ':' + two.request_id, JSON.stringify(two))
    }, { key: localPrefix, one: pendingOriginal, two: sibling })
    await panel.locator('[data-grant-request="' + sibling.request_id + '"]').waitFor()
    await storagePeer.close()
    await page.reload(); panel = await open(page, sample.A, false)
    await panel.locator('[data-grant-request="' + original.request_id + '"]').getByRole('button', { name: '核对原许可结果', exact: true }).click()
    await until(async () => !(await panel.locator('[data-grant-request="' + original.request_id + '"]').count()), 'resolved request removed alone')
    assert.equal(await page.evaluate(key => JSON.parse(localStorage.getItem(key)).request_id, localPrefix + ':' + sibling.request_id), sibling.request_id)
    await panel.locator('[data-grant-request="' + sibling.request_id + '"]').getByRole('button', { name: '核对原许可结果', exact: true }).click()
    await until(async () => !(await panel.locator('[data-grant-request]').count()), 'remaining original request individually resolved')
    assert.equal((await stats()).attempts, before.attempts)
    check('Independent pending requests survive reload and resolving one never deletes another, even beside a malformed legacy record')
    await panel.getByRole('button', { name: '回到原任务继续', exact: true }).click()
    await until(() => new URL(page.url()).pathname === '/chat', 'return to owning foreground task')
    assert.equal(new URL(page.url()).searchParams.get('conv'), sample.A.cid); assert.equal(new URL(page.url()).searchParams.get('run'), sample.A.parent_id)
    check('Unified center and continue link retain the exact parent conversation and run')

    sample = await seed(); panel = await open(page, sample.A); await panel.getByRole('checkbox').check()
    await advance(sample.A); await panel.getByRole('button', { name: '刷新科研子任务', exact: true }).click()
    await until(async () => !(await panel.getByRole('checkbox').isChecked()), 'new attempt revokes checkbox')
    assert.ok(await grantButton().isDisabled())
    check('A newer child attempt or parent revision revokes old checkbox consent')

    sample = await seed(); panel = await open(page, sample.A); await panel.getByRole('checkbox').check()
    let release, entered = false
    const delayed = new Promise(resolve => { release = resolve })
    const oldPost = async item => { entered = true; await delayed; const response = await item.fetch(); await item.fulfill({ response }) }
    const stalePath = '**/api/research/children/' + sample.A.child_id + '/retry-grants'
    await page.route(stalePath, oldPost); await grantButton().click(); await until(() => entered, 'old grant waiting before CAS')
    const second = await newPage(); let other = await open(second, sample.A); await advance(sample.A)
    await other.getByRole('button', { name: '刷新科研子任务', exact: true }).click(); const counts = await stats()
    release(); await until(async () => (await panel.getByRole('alert').innerText()).includes('父执行已变化'), 'stale grant is rejected')
    assert.equal((await stats()).grants, counts.grants); await page.unroute(stalePath, oldPost)
    // The second window may still hold the same original pending receipt.
    if (await other.getByRole('button', { name: '核对原许可结果', exact: true }).count()) {
      await other.getByRole('button', { name: '核对原许可结果', exact: true }).click()
      await until(async () => !(await other.getByRole('button', { name: '核对原许可结果', exact: true }).count()), 'old pending cleared only after explicit rejection')
    }
    await other.getByRole('checkbox').check(); await other.getByRole('button', { name: '保存本次单次许可', exact: true }).click()
    await other.getByText(/许可已保存，仅针对/).waitFor(); assert.equal((await stats()).grants, counts.grants + 1); await second.close()
    check('A delayed grant from an old window cannot authorize a new child attempt; a new exact grant requires fresh human confirmation')

    sample = await seed(); panel = await open(page, sample.A)
    let releaseRead, readStarted = false
    const heldRead = new Promise(resolve => { releaseRead = resolve })
    const slowRead = async item => { if (new URL(item.request().url()).searchParams.get('parent_run_id') !== sample.A.parent_id) return item.continue(); const response = await item.fetch(); readStarted = true; await heldRead; await item.fulfill({ response }).catch(() => {}) }
    await page.route('**/api/research/children?*', slowRead); await panel.getByRole('button', { name: '刷新科研子任务', exact: true }).click()
    await until(() => readStarted, 'A read waiting'); panel = await open(page, sample.B, false)
    releaseRead(); await pause(120); await page.unroute('**/api/research/children?*', slowRead)
    assert.ok((await panel.innerText()).includes(sample.B.parent_id)); assert.ok(!(await panel.innerText()).includes(sample.A.child_id))
    check('Late child-list responses cannot replace another parent panel')

    sample = await seed(); panel = await open(page, sample.A); await panel.getByRole('checkbox').check()
    let releaseReply, replyReady = false
    const heldReply = new Promise(resolve => { releaseReply = resolve })
    const slowReply = async item => { const response = await item.fetch(); replyReady = true; await heldReply; await item.fulfill({ response }).catch(() => {}) }
    const latePath = '**/api/research/children/' + sample.A.child_id + '/retry-grants'
    await page.route(latePath, slowReply); await grantButton().click(); await until(() => replyReady, 'A grant committed and reply held')
    panel = await open(page, sample.B, false); releaseReply(); await pause(120); await page.unroute(latePath, slowReply)
    assert.ok(!(await panel.getByRole('checkbox').isChecked())); assert.ok(await grantButton().isDisabled())
    assert.ok(!(await panel.innerText()).includes('许可已保存，仅针对'))
    check('A late grant reply cannot show permission or checked consent for another parent')

    await panel.getByRole('checkbox').check()
    const beforeStorageFailure = mutations.filter(item => item.url.endsWith('/retry-grants')).length
    await page.evaluate(() => {
      window.__grantStorageDescriptor = Object.getOwnPropertyDescriptor(Storage.prototype, 'setItem')
      Object.defineProperty(Storage.prototype, 'setItem', { configurable: true, value(key, value) {
        if (key.startsWith('scholarflow:research-child-grant:')) throw new DOMException('controlled quota error', 'QuotaExceededError')
        return window.__grantStorageDescriptor.value.call(this, key, value)
      } })
    })
    await grantButton().click()
    await panel.getByRole('alert').filter({ hasText: '无法保存本次许可请求，尚未提交' }).waitFor()
    assert.equal(mutations.filter(item => item.url.endsWith('/retry-grants')).length, beforeStorageFailure)
    await panel.screenshot({ path: path.join(out, 'research-child-storage-failure.png') })
    await page.evaluate(() => Object.defineProperty(Storage.prototype, 'setItem', window.__grantStorageDescriptor))
    await panel.getByRole('checkbox').uncheck()
    await panel.locator('.child').click()
    check('A local receipt-storage failure prevents the grant POST and reports that nothing was submitted')

    const priced = async item => {
      const response = await item.fetch(), body = await response.json()
      for (const child of body.items) child.usage = {
        call_count: 9, actual_usage: { total_tokens: 123 }, estimated_usage: { total_tokens: 45 }, unknown_usage_calls: 2, unknown_amount_calls: 4,
        pricing: { user_confirmed: { USD: '1.11' }, complete_estimates: { CNY: '2.22' }, partial_estimates: { USD: '3.33' }, unknown_calls: 4 },
      }
      await item.fulfill({ response, json: body })
    }
    await page.route('**/api/research/children?*', priced)
    await panel.getByRole('button', { name: '刷新科研子任务', exact: true }).click()
    await until(async () => (await panel.innerText()).includes('2.22 CNY'), 'separate synthetic pricing groups displayed')
    const terms = await panel.locator('dt').allTextContents(), amounts = await panel.locator('dd').allTextContents()
    assert.deepEqual(terms, ['用户按账单核对', '按完整价目估算', '仅部分费用估算'])
    assert.deepEqual(amounts, ['1.11 USD', '2.22 CNY', '3.33 USD'])
    assert.match(await panel.innerText(), /4\s*次调用金额未知/)
    await page.unroute('**/api/research/children?*', priced)
    check('Controlled display sample keeps confirmed amounts, complete estimates, partial estimates and unknown amounts distinct by currency')

    await page.setViewportSize({ width: 390, height: 844 })
    const box = await panel.boundingBox(); assert.ok(box && box.x >= 0 && box.x + box.width <= 391)
    assert.ok(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1))
    check('Grant panel and exact-parent controls fit a 390px viewport')
    // Keep the tested width and increase capture height so the app's sticky
    // navigation does not obscure an over-height card during element capture.
    await page.setViewportSize({ width: 390, height: 1200 }); await panel.screenshot({ path: path.join(out, 'research-child-narrow.png') })
    await page.setViewportSize({ width: 1440, height: 1000 }); await panel.screenshot({ path: path.join(out, 'research-child-desktop.png') })
    assert.deepEqual(errors, []); assert.deepEqual(external, []); assert.equal((await stats()).ledger_calls, 0)
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify({ success: true, checks, browser_errors: errors, external_requests: external, mutations, real_provider_calls: 0,
      scope: 'Real Vue, production child grant/ownership HTTP and SQLite; controlled synthetic pending operations/replacement identity. No live model, no parent resume dispatched.' }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(out, 'failure.log'), String(error.stack || error) + '\n' + logs.slice(-16000)); throw error
  } finally { if (browser) await browser.close(); if (vite) await vite.close(); await stop() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
