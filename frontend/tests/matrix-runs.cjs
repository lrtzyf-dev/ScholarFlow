// Real Vue and isolated HTTP/SQLite; controlled local timing, no provider calls.
const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'), pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) { const end = Date.now() + timeout; while (!(await check())) { assert.ok(Date.now() < end, label); await pause(80) } }
async function port() { const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const result = server.address().port; await new Promise(resolve => server.close(resolve)); return result }
async function main() {
  const out = path.join(root, 'verification/matrix-runs/frontend'); await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1', TEMP: isolated, TMP: isolated }
  const checks = [], errors = [], external = [], mutations = [], apiErrors = []
  const check = name => checks.push({ name, passed: true })
  let backend, vite, browser, page, logs = '', base, ui
  async function start() {
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'matrix_frontend_fixture:app', '--app-dir', 'tests', '--host', '127.0.0.1', '--port', new URL(base).port],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', data => { logs += data }); backend.stderr.on('data', data => { logs += data })
    await until(async () => { if (backend.exitCode !== null) throw Error(logs); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'isolated backend startup')
  }
  async function stop() { if (backend && backend.exitCode === null) { const done = new Promise(resolve => backend.once('exit', resolve)); backend.kill(); await done } }
  const api = async (route, body, method) => {
    const response = await fetch(base + '/api' + route, { method: method || (body === undefined ? 'GET' : 'POST'), headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000) })
    assert.equal(response.status, 200, route + ': ' + await response.clone().text() + (response.status >= 500 ? '\n' + logs.slice(-8000) : '')); return response.json()
  }
  const run = value => api('/research/matrix-runs/' + value.run_id + '?conversation_id=' + value.cid)
  const matrix = value => api('/research/matrices/' + value.matrix_id + '?conversation_id=' + value.cid)
  const route = (value, withRun = true) => '/compare?conversation=' + value.cid + '&matrix=' + value.matrix_id + (withRun ? '&run=' + value.run_id : '')
  const navigate = async (target, value) => { await target.evaluate(async url => { const { default: router } = await import('/src/router.ts'); await router.push(url); await new Promise(resolve => requestAnimationFrame(resolve)) }, value) }
  try {
    base = 'http://127.0.0.1:' + await port(); await start()
    const seed = await api('/test-fixture/matrix/seed', {})
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false } } } })
    await vite.listen(); ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', item => { const url = new URL(item.request().url()); if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return item.continue(); external.push(url.origin + url.pathname); return item.abort() })
    page = await context.newPage(); page.setDefaultTimeout(20000); page.on('pageerror', error => errors.push(error.message))
    page.on('response', async response => { if (response.status() >= 400 && response.url().includes('/api/research')) apiErrors.push({ url: response.url(), status: response.status(), body: await response.text().catch(() => '') }) })
    page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/api/research/matrix-runs')) mutations.push({ url: new URL(request.url()).pathname, body: request.postDataJSON() }) })
    await page.goto(ui + route(seed.A))
    await page.getByLabel('矩阵名称', { exact: true }).waitFor(); assert.equal(await page.getByLabel('矩阵名称', { exact: true }).inputValue(), '矩阵 A')
    let releaseA, startedA = false
    const blockedA = new Promise(resolve => { releaseA = resolve })
    const slowA = async item => { const response = await item.fetch(); startedA = true; await blockedA; await item.fulfill({ response }).catch(() => {}) }
    await page.route('**/api/research/matrices/' + seed.A.matrix_id + '?*', slowA)
    await page.getByRole('button', { name: '查看当前矩阵', exact: true }).click()
    await until(() => startedA, 'old matrix read entered controlled delay')
    await navigate(page, route(seed.B))
    await until(async () => (await page.getByLabel('矩阵名称', { exact: true }).inputValue()) === '矩阵 B', 'same component accepts conversation B')
    releaseA(); await pause(150); await page.unroute('**/api/research/matrices/' + seed.A.matrix_id + '?*', slowA)
    assert.equal(await page.getByLabel('矩阵名称', { exact: true }).inputValue(), '矩阵 B'); assert.ok(page.url().includes(seed.B.cid))
    check('Conversation A late response cannot replace conversation B through same-component query navigation')

    await page.getByLabel('矩阵名称', { exact: true }).fill('用户尚未保存的名称')
    await page.getByText(/提取字段（/).click(); await page.getByLabel('字段 1 名称', { exact: true }).fill('用户尚未保存的字段')
    await api('/test-fixture/matrix/' + seed.B.run_id + '/execute', {})
    await until(async () => (await page.getByRole('region', { name: '证据提取任务' }).innerText()).includes('提取结果已合并保存'), 'applied run observed')
    await pause(150)
    assert.equal(await page.getByLabel('矩阵名称', { exact: true }).inputValue(), '用户尚未保存的名称')
    assert.equal(await page.getByLabel('字段 1 名称', { exact: true }).inputValue(), '用户尚未保存的字段')
    check('Run polling and applied-version reload preserve unsaved title and schema field drafts')

    // Response loss after the actual server POST, followed by failed GET and a
    // switch to another matrix. The exact pending create survives page/server restart.
    await navigate(page, route(seed.B, false)); await page.getByLabel('矩阵名称', { exact: true }).waitFor()
    let lostCreate, hideNew = true, createPosts = 0
    const createLoss = async item => { createPosts++; lostCreate = item.request().postDataJSON(); await item.fetch(); await item.abort() }
    const hideCreated = async item => { if (hideNew && lostCreate && new URL(item.request().url()).pathname.endsWith('/' + lostCreate.run_id)) return item.abort(); return item.continue() }
    await page.route('**/api/research/matrix-runs/*?*', hideCreated)
    await page.route('**/api/research/matrix-runs', createLoss)
    await until(async () => (await page.getByLabel('已保存提取任务', { exact: true }).locator('option').count()) >= 2 && !(await page.getByRole('button', { name: '开始对比', exact: true }).isDisabled()), 'new route inputs and task history ready')
    await page.getByRole('button', { name: '开始对比', exact: true }).click()
    await page.getByRole('status', { name: '待确认的原提取任务' }).waitFor()
    await until(async () => !!lostCreate && !(await page.getByRole('button', { name: '重试保存原任务', exact: true }).isDisabled()), 'create reached server and its reply was lost')
    assert.ok(/^research_[a-f0-9]{32}$/.test(lostCreate.run_id))
    await page.unroute('**/api/research/matrix-runs', createLoss)
    const beforeRetry = await api('/test-fixture/matrix/stats')
    const other = await api('/research/matrices', { conversation_id: seed.B.cid, request_id: 'other-' + Date.now(), title: '矩阵 B 另一份', paper_ids: seed.B.papers, fields: [{ key: 'method', label: '独立字段', query: 'method' }] })
    await navigate(page, route({ ...seed.B, matrix_id: other.id }, false))
    await until(async () => (await page.getByLabel('矩阵名称', { exact: true }).inputValue()) === '矩阵 B 另一份', 'different matrix selected during pending create')
    assert.ok(await page.getByRole('button', { name: '开始对比', exact: true }).isDisabled())
    assert.ok((await page.getByRole('status', { name: '待确认的原提取任务' }).innerText()).includes(lostCreate.matrix_id))
    await stop(); await start(); await page.reload()
    await page.getByRole('status', { name: '待确认的原提取任务' }).waitFor()
    const retryBodies = []
    const captureRetry = async item => { retryBodies.push(item.request().postDataJSON()); await item.continue() }
    await page.route('**/api/research/matrix-runs', captureRetry)
    await page.getByRole('button', { name: '重试保存原任务', exact: true }).click()
    await until(() => retryBodies.length === 1, 'explicit original create retry')
    hideNew = false
    await until(async () => (await page.getByLabel('矩阵名称', { exact: true }).inputValue()) === '矩阵 B', 'original matrix restored after its exact request acknowledged')
    assert.deepEqual(retryBodies[0], lostCreate)
    const afterRetry = await api('/test-fixture/matrix/stats')
    assert.equal(afterRetry.runs, beforeRetry.runs); assert.equal(afterRetry.attempts, beforeRetry.attempts)
    assert.equal(createPosts, 1)
    await page.unroute('**/api/research/matrix-runs', captureRetry); await page.unroute('**/api/research/matrix-runs/*?*', hideCreated)
    check('Lost create reply keeps exact request and origin across another matrix, browser reload and backend restart without another run or attempt')

    const unknown = (await api('/test-fixture/matrix/seed', {})).A
    await api('/test-fixture/matrix/' + unknown.run_id + '/interrupt', { unknown: true })
    await navigate(page, route(unknown)); const unknownCheck = page.getByRole('checkbox', { name: /我已核对上次调用/ })
    await unknownCheck.check()
    const oldUnknown = await run(unknown); let resumeBody
    const loseResume = async item => { resumeBody = item.request().postDataJSON(); await item.fetch(); await item.abort() }
    await page.route('**/api/research/matrix-runs/' + unknown.run_id + '/resume', loseResume)
    await page.getByRole('button', { name: '继续缺失单元格', exact: true }).click()
    await until(async () => (await run(unknown)).attempt_id !== oldUnknown.attempt_id, 'resume accepted despite reply loss')
    await api('/test-fixture/matrix/' + unknown.run_id + '/interrupt', { unknown: true })
    await page.getByRole('button', { name: '刷新提取状态', exact: true }).click()
    await until(async () => await unknownCheck.isVisible() && !(await unknownCheck.isChecked()), 'new unknown attempt requires fresh acknowledgement')
    assert.ok(await page.getByRole('button', { name: '继续缺失单元格', exact: true }).isDisabled())
    assert.equal(resumeBody.expected_revision, oldUnknown.revision)
    await page.unroute('**/api/research/matrix-runs/' + unknown.run_id + '/resume', loseResume)
    check('A lost resume reply cannot carry the old unknown-call checkbox into a later attempt')

    const merge = (await api('/test-fixture/matrix/seed', {})).A
    const baseMatrix = await matrix(merge)
    await api('/research/matrices/' + merge.matrix_id + '/cell', { conversation_id: merge.cid, expected_version: baseMatrix.version, field_key: 'method', paper_id: merge.papers[0], value: '原人工结论保留', status: 'not_found', note: '原人工核验依据' }, 'PATCH')
    const extracted = await api('/test-fixture/matrix/' + merge.run_id + '/execute', {})
    assert.equal(extracted.outcome, 'partial'); assert.ok(extracted.result.apply_conflict)
    await navigate(page, route(merge))
    await page.getByRole('button', { name: '合并到当前矩阵并保留人工校正', exact: true }).waitFor()
    const loseApply = async item => { await item.fetch(); await item.abort() }
    await page.route('**/api/research/matrix-runs/' + merge.run_id + '/apply', loseApply)
    await page.getByRole('button', { name: '合并到当前矩阵并保留人工校正', exact: true }).click()
    await until(async () => !!(await run(merge)).result.applied_version, 'lost apply response recovered by observation')
    await page.unroute('**/api/research/matrix-runs/' + merge.run_id + '/apply', loseApply)
    await page.getByRole('button', { name: '查看当前矩阵', exact: true }).click()
    await page.getByText('原人工结论保留', { exact: true }).waitFor()
    const candidate = page.locator('.matrix-candidate').first(); await candidate.locator('summary').click()
    await candidate.locator('[data-candidate-locator-id]').first().waitFor()
    const saved = await matrix(merge), cell = saved.document.rows[0].cells[merge.papers[0]]
    assert.equal(cell.manual.value, '原人工结论保留'); assert.equal(cell.status, 'not_found'); assert.equal(cell.evidence.length, 0)
    assert.ok(cell.extraction_candidate.evidence.length); assert.equal(cell.extraction_candidate.verification_status, 'unverified')
    await page.screenshot({ path: path.join(out, 'matrix-candidate-review.png'), fullPage: true })
    const fixedCandidate = cell.extraction_candidate.evidence[0]
    await candidate.locator('[data-candidate-locator-id]').first().click()
    await page.waitForURL(new RegExp('/sources/' + fixedCandidate.revision_id))
    await page.locator('[data-source-highlight="true"]').first().waitFor()
    const fixedLocator = await api('/sources/revisions/' + fixedCandidate.revision_id + '/locators/' + fixedCandidate.locator_id + '?conversation_id=' + merge.cid)
    assert.equal(fixedLocator.quote, fixedCandidate.quote)
    await navigate(page, route(merge)); await page.getByRole('button', { name: '查看当前矩阵', exact: true }).click()
    await page.getByText('原人工结论保留', { exact: true }).waitFor()
    check('A separate candidate citation opens and highlights the exact fixed original locator')
    await page.locator('.matrix-edit').first().click()
    const adopt = page.getByRole('checkbox', { name: '本次人工核验采用已查看的新候选证据', exact: true })
    assert.ok(!(await adopt.isChecked()))
    await page.getByLabel('校正内容', { exact: true }).fill('查看新证据后作出的人工结论')
    await page.getByLabel('核验状态', { exact: true }).selectOption('supported'); await adopt.check()
    await page.getByRole('button', { name: '保存校正', exact: true }).click()
    await until(async () => !(await page.getByLabel('校正内容', { exact: true }).count()), 'explicit candidate review saved')
    const adopted = (await matrix(merge)).document.rows[0].cells[merge.papers[0]]
    assert.equal(adopted.manual.value, '查看新证据后作出的人工结论'); assert.equal(adopted.status, 'supported'); assert.ok(adopted.evidence.length)
    assert.ok(!adopted.extraction_candidate)
    check('Lost apply response preserves manual evidence while a separate fixed candidate requires explicit hash-bound review')
    await page.screenshot({ path: path.join(out, 'matrix-desktop.png'), fullPage: true })

    const stopped = (await api('/test-fixture/matrix/seed', {})).A
    await navigate(page, route(stopped)); await page.getByRole('button', { name: '停止提取', exact: true }).waitFor()
    let delayedCancel, releaseCancel, cancelStarted = false
    const cancelGate = new Promise(resolve => { releaseCancel = resolve })
    const slowCancel = async item => { delayedCancel = item.request().postDataJSON(); cancelStarted = true; await cancelGate; await item.continue() }
    await page.route('**/api/research/matrix-runs/' + stopped.run_id + '/cancel', slowCancel)
    await page.getByRole('button', { name: '停止提取', exact: true }).click()
    await until(() => cancelStarted, 'old window cancellation delayed before HTTP dispatch')
    await api('/test-fixture/matrix/' + stopped.run_id + '/interrupt', {})
    const second = await context.newPage(); second.setDefaultTimeout(20000); second.on('pageerror', error => errors.push(error.message))
    await second.goto(ui + route(stopped)); await second.getByRole('button', { name: '继续缺失单元格', exact: true }).click()
    await second.getByRole('button', { name: '停止提取', exact: true }).waitFor()
    const newer = await run(stopped); assert.notEqual(newer.attempt_id, delayedCancel.expected_attempt_id)
    releaseCancel()
    await page.getByRole('region', { name: '证据提取任务' }).getByRole('alert').waitFor()
    assert.equal((await run(stopped)).attempt_id, newer.attempt_id); assert.equal((await run(stopped)).status, newer.status)
    await page.unroute('**/api/research/matrix-runs/' + stopped.run_id + '/cancel', slowCancel); await second.close()
    check('An old window delayed cancellation carries revision and attempt and cannot stop a new attempt resumed in another window')

    const rejected = (await api('/test-fixture/matrix/seed', {})).A
    await api('/test-fixture/matrix/' + rejected.run_id + '/interrupt', {})
    await navigate(page, route(rejected, false))
    await until(async () => !(await page.getByRole('button', { name: '开始对比', exact: true }).isDisabled()), 'new input ready for controlled CAS failure')
    const countsBeforeReject = await api('/test-fixture/matrix/stats')
    const rejectCreate = async item => {
      const current = await matrix(rejected)
      await api('/research/matrices/' + rejected.matrix_id, { conversation_id: rejected.cid, expected_version: current.version,
        title: current.title, paper_ids: rejected.papers, fields: current.document.dimensions }, 'PUT')
      await item.continue()
    }
    await page.route('**/api/research/matrix-runs', rejectCreate)
    await page.getByRole('button', { name: '开始对比', exact: true }).click()
    await page.getByRole('button', { name: '核实未创建并解除待确认', exact: true }).waitFor()
    await until(async () => !(await page.getByRole('button', { name: '核实未创建并解除待确认', exact: true }).isDisabled()), 'definitive creation rejection returned')
    await page.getByRole('button', { name: '核实未创建并解除待确认', exact: true }).click()
    await until(async () => !(await page.getByRole('status', { name: '待确认的原提取任务' }).count()), '404 confirms rejected request never created a run')
    assert.equal((await api('/test-fixture/matrix/stats')).runs, countsBeforeReject.runs)
    await page.unroute('**/api/research/matrix-runs', rejectCreate)
    check('A definitive create rejection can be released only after exact-run 404 verification, without leaving an unrecoverable pending request')

    await navigate(page, route(merge)); await page.getByLabel('矩阵名称', { exact: true }).waitFor()
    await page.setViewportSize({ width: 390, height: 844 })
    const region = page.getByRole('region', { name: '证据提取任务' }); await region.scrollIntoViewIfNeeded()
    const box = await region.boundingBox(); assert.ok(box && box.x >= 0 && box.x + box.width <= 391)
    const createBox = await page.getByRole('button', { name: '开始对比', exact: true }).boundingBox()
    assert.ok(createBox && createBox.x >= 0 && createBox.x + createBox.width <= 391, 'start action is not clipped on narrow screens')
    await page.screenshot({ path: path.join(out, 'matrix-narrow.png'), fullPage: true })
    check('Saved matrix task controls remain within a 390px viewport')
    await page.setViewportSize({ width: 1440, height: 1000 })
    await navigate(page, '/tasks'); await page.locator('.center-filters select').first().selectOption('all')
    const centerItem = page.locator('[data-run-center-id="run:' + merge.run_id + '"]')
    await centerItem.getByText('证据矩阵', { exact: true }).waitFor()
    await centerItem.getByRole('button', { name: '查看并处理', exact: true }).click()
    await page.waitForURL(new RegExp('/compare\\?.*run=' + merge.run_id))
    await until(async () => (await page.getByLabel('矩阵名称', { exact: true }).inputValue()) === '矩阵 A', 'run center restores exact matrix task')
    check('Unified run center opens the exact matrix run in CompareView with its conversation')
    assert.equal((await api('/test-fixture/matrix/stats')).ledger_calls, 0)
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify({ success: true, checks, browser_errors: errors, external_requests: external, mutations, real_provider_calls: 0,
      scope: 'Real Vue/HTTP/SQLite plus controlled local timing and pending checkpoints; no live model or embedding provider.' }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (error) { if (page) await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {}); await fs.writeFile(path.join(out, 'report.json'), JSON.stringify({ success: false, checks, error: String(error), browser_errors: errors, external_requests: external, mutations, api_errors: apiErrors }, null, 2)); throw error }
  finally { if (browser) await browser.close(); if (vite) await vite.close(); await stop() }
}
main().catch(error => { process.stderr.write(String(error)); process.exitCode = 1 })
