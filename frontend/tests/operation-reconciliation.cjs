const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'), delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) { const end = Date.now() + timeout; while (!(await check())) { assert.ok(Date.now() < end, label); await delay(80) } }
async function port() { const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const value = server.address().port; await new Promise(resolve => server.close(resolve)); return value }
async function main() {
  const output = path.join(root, 'verification/side-effects/e2e'); await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1', TEMP: isolated, TMP: isolated }
  let backend, vite, browser, page, logs = ''
  const checks = [], errors = [], external = [], check = name => checks.push({ name, passed: true })
  try {
    const base = 'http://127.0.0.1:' + await port()
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'operation_ui_fixture:app', '--app-dir', 'tests', '--host', '127.0.0.1', '--port', new URL(base).port],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', data => { logs += data }); backend.stderr.on('data', data => { logs += data })
    const api = async (route, body) => {
      const response = await fetch(base + '/api' + route, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      assert.equal(response.status, 200, route + ': ' + await response.clone().text()); return response.json()
    }
    await until(async () => { if (backend.exitCode !== null) throw Error(logs); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'isolated backend startup')
    const seed = await api('/test-fixture/operations/seed', {})
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false } } } })
    await vite.listen(); const ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', route => { const url = new URL(route.request().url()); if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue(); external.push(url.origin + url.pathname); return route.abort() })
    page = await context.newPage(); page.setDefaultTimeout(15000); page.on('pageerror', error => errors.push(error.message))
    await page.goto(ui + '/tasks')
    await page.locator(`[data-run-center-id="foreground:${seed.run_id}"]`).getByRole('button', { name: '操作回执与核对', exact: true }).click()
    const panel = page.getByRole('region', { name: '原任务操作回执与核对' })
    await panel.getByRole('button', { name: /mcp__fixture__save_record/ }).click()
    assert.ok(!(await panel.innerText()).includes('fixture-hidden-secret'))
    await panel.getByText(/供应商未验证/).first().waitFor()
    check('Ordinary foreground run exposes its exact uncertain operation with redacted arguments')
    const permit = panel.getByRole('button', { name: '确认未执行并允许重试一次', exact: true })
    assert.ok(await permit.isDisabled())
    await panel.getByLabel('操作核对依据').fill('已检查本地合成服务记录，未找到执行记录')
    await panel.getByLabel('我已核对并理解此声明不代表供应商验证').check()
    await api('/test-fixture/operations/' + seed.run_id + '/revision', {})
    await permit.click()
    await panel.locator('[role="alert"]').filter({ hasText: '更新或接管' }).waitFor()
    assert.equal(await panel.getByLabel('操作核对依据').inputValue(), '已检查本地合成服务记录，未找到执行记录')
    check('CAS refusal preserves the user review draft and does not create permission')
    await panel.getByRole('button', { name: '刷新操作', exact: true }).click()
    await panel.getByRole('button', { name: /mcp__fixture__save_record/ }).click()
    await panel.getByLabel('操作核对依据').fill('重新核查合成记录，明确未执行；仅准许原操作一次')
    await panel.getByLabel('我已核对并理解此声明不代表供应商验证').check()
    await permit.click()
    await panel.getByText(/已记录未执行及单次重试许可/).waitFor()
    const saved = await api('/subtasks/runs/' + seed.run_id + '/operations')
    assert.equal(saved.items[0].reconciliations.length, 1); assert.equal(saved.items[0].reconciliations[0].provider_verified, false)
    assert.equal(saved.items[0].status, 'open'); assert.equal(saved.items[0].retry_consumed, false); assert.equal(saved.parent.active, false)
    check('User declaration records a single exact retry grant without starting a run or inventing success')
    await panel.getByRole('button', { name: /mcp__fixture__save_record/ }).click()
    await panel.getByText('核对历史（保留原声明）', { exact: true }).click()
    await panel.getByText(/重新核查合成记录，明确未执行/).waitFor()
    await page.screenshot({ path: path.join(output, 'operations-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await until(async () => { const box = await panel.boundingBox(); return box && box.x >= 0 && box.x + box.width <= 391 }, 'narrow operation panel')
    await page.screenshot({ path: path.join(output, 'operations-narrow.png'), fullPage: true })
    await panel.getByRole('button', { name: '记录仍无法确认', exact: true }).scrollIntoViewIfNeeded()
    for (const name of ['记录已执行', '确认未执行并允许重试一次', '记录仍无法确认']) {
      const box = await panel.getByRole('button', { name, exact: true }).boundingBox()
      assert.ok(box && box.x >= 0 && box.x + box.width <= 391, name + ' fits narrow layout')
    }
    await page.screenshot({ path: path.join(output, 'operations-narrow-actions.png'), fullPage: true })
    check('Original declaration history and actions remain readable at 390px')
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: true, checks, browser_errors: errors, external_requests: external, real_provider_calls: 0 }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (error) { if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}); await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: false, checks, error: String(error), browser_errors: errors, external_requests: external }, null, 2)); throw error }
  finally { if (browser) await browser.close(); if (vite) await vite.close(); if (backend && backend.exitCode === null) backend.kill() }
}
main().catch(error => { process.stderr.write(String(error)); process.exitCode = 1 })
