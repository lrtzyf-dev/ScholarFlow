const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'), delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) { const end = Date.now() + timeout; while (!(await check())) { assert.ok(Date.now() < end, label); await delay(80) } }
async function port() { const s = net.createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r)); const p = s.address().port; await new Promise(r => s.close(r)); return p }
async function main() {
  const output = path.join(root, 'verification/pricing-settlement/e2e'); await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  let backend, vite, browser, page, logs = ''
  const checks = [], errors = [], external = [], check = name => checks.push({ name, passed: true })
  try {
    const base = 'http://127.0.0.1:' + await port()
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'pricing_ui_fixture:app', '--app-dir', 'tests', '--host', '127.0.0.1', '--port', new URL(base).port],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', d => { logs += d }); backend.stderr.on('data', d => { logs += d })
    const api = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
      const response = await fetch(base + '/api' + route, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      assert.equal(response.status, 200, route + ': ' + await response.clone().text()); return response.json()
    }
    await until(async () => { if (backend.exitCode !== null) throw Error(logs); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'startup')
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false } } } })
    await vite.listen(); const ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', route => { const url = new URL(route.request().url()); if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue(); external.push(url.origin + url.pathname); return route.abort() })
    page = await context.newPage(); page.on('pageerror', e => errors.push(e.message)); page.setDefaultTimeout(15000)
    await page.goto(ui + '/settings')
    await page.getByRole('button', { name: /调用价目与计费覆盖/ }).click()
    await page.getByLabel('价目名称', { exact: true }).fill('本地合成价格 v1')
    await page.getByLabel('计价请求端点').fill('https://pricing.test/v1/chat/completions')
    await page.getByLabel('计价模型', { exact: true }).fill('Exact-Model')
    await page.getByLabel('价目依据', { exact: true }).fill('synthetic-fixture-price-v1')
    await page.getByLabel('单价 1', { exact: true }).fill('2'); await page.getByLabel('计价基数 1', { exact: true }).fill('1000')
    await page.getByLabel('单价 2', { exact: true }).fill('4'); await page.getByLabel('计价基数 2', { exact: true }).fill('1000')
    await page.getByLabel('确认价目完整覆盖').check()
    await page.getByRole('button', { name: '保存新价目版本', exact: true }).click()
    await until(async () => (await api('/pricing/versions')).items.length === 1, 'save complete tariff')
    check('Actual settings saves an explicit immutable tariff and coverage declaration')
    const seed = await api('/test-fixture/pricing/seed', {})
    let budget = (await api('/conversation-resources/' + seed.conversation_id)).budgets[0]
    assert.equal(budget.estimated_spent_amount, '0.4'); assert.equal(budget.confirmed_spent_amount, '0'); assert.equal(budget.reserved_amount, '0')
    check('Complete actual-usage receipt automatically settles only as an estimate')
    await page.goto(ui + '/chat?conv=' + seed.conversation_id)
    await page.locator('textarea.textarea').waitFor(); await page.locator('.resource-toggle').click()
    await page.getByText('调用费用明细与账单核对', { exact: true }).click()
    await page.locator('.call-row').first().click()
    await page.getByLabel('实际账单金额').fill('0.3')
    await page.getByLabel('账单核对依据').fill('invoice-row-1')
    await page.getByRole('button', { name: '保存账单核对', exact: true }).click()
    await until(async () => (await api('/conversation-resources/' + seed.conversation_id)).budgets[0].confirmed_spent_amount === '0.3', 'confirm invoice')
    budget = (await api('/conversation-resources/' + seed.conversation_id)).budgets[0]
    assert.equal(budget.estimated_spent_amount, '0'); assert.equal(budget.spent_amount, '0.3'); assert.equal(budget.remaining_amount, '2.7')
    check('Invoice confirmation updates budget by delta and preserves its own source')
    const detail = await api('/pricing/conversations/' + seed.conversation_id + '/calls/' + seed.call_id)
    assert.equal(detail.confirmations.length, 1)
    await page.screenshot({ path: path.join(output, 'cost-confirmation.png'), fullPage: true })
    await page.getByLabel('实际账单金额').fill('0.25'); await page.getByLabel('账单核对依据').fill('draft-stale-edit')
    await api('/pricing/conversations/' + seed.conversation_id + '/calls/' + seed.call_id + '/confirm', { expected_revision: detail.pricing.revision, amount: '0.2', currency: 'USD', reference: 'other-window-confirmation' })
    await page.getByRole('button', { name: '保存账单核对', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: '已变化' }).waitFor()
    assert.equal(await page.getByLabel('实际账单金额').inputValue(), '0.25')
    assert.equal((await api('/conversation-resources/' + seed.conversation_id)).budgets[0].spent_amount, '0.2')
    check('Concurrent invoice edits reject stale revisions and keep the draft')
    await page.getByRole('button', { name: '重新读取此调用', exact: true }).click()
    await page.setViewportSize({ width: 390, height: 844 })
    await until(async () => { const box = await page.locator('.resource-panel').boundingBox(); return box && box.x >= 0 && box.x + box.width <= 391 }, 'narrow costs panel')
    await page.screenshot({ path: path.join(output, 'cost-narrow.png'), fullPage: true })
    check('Cost details and confirmation stay reachable in a narrow panel')
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: true, checks, browser_errors: errors, external_requests: external, real_provider_calls: 0,
      fixture: 'Synthetic local usage receipts, never a real provider invoice' }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (e) { if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}); await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: false, checks, error: String(e), browser_errors: errors, external_requests: external }, null, 2)); throw e }
  finally { if (browser) await browser.close(); if (vite) await vite.close(); if (backend && backend.exitCode === null) backend.kill() }
}
main().catch(e => { process.stderr.write(String(e)); process.exitCode = 1 })
