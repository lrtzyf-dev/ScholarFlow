const assert = require('node:assert/strict'), fs = require('node:fs/promises'), path = require('node:path'), net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'), delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) { const end = Date.now() + timeout; while (!(await check())) { assert.ok(Date.now() < end, label); await delay(80) } }
async function port() { const s = net.createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r)); const p = s.address().port; await new Promise(r => s.close(r)); return p }
async function main() {
  const output = path.join(root, 'verification/subtask-inspection/e2e'); await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  let backend, vite, browser, page, logs = ''
  const checks = [], errors = [], external = [], check = name => checks.push({ name, passed: true })
  try {
    const base = 'http://127.0.0.1:' + await port()
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'subtask_ui_fixture:app', '--app-dir', 'tests', '--host', '127.0.0.1', '--port', new URL(base).port],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', d => { logs += d }); backend.stderr.on('data', d => { logs += d })
    const api = async (route, body) => {
      const response = await fetch(base + '/api' + route, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
      assert.equal(response.status, 200, route + ': ' + await response.clone().text()); return response.json()
    }
    await until(async () => { if (backend.exitCode !== null) throw Error(logs); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'startup')
    const seed = await api('/test-fixture/subtasks/seed', {})
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false } } } })
    await vite.listen(); const ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', route => { const url = new URL(route.request().url()); if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue(); external.push(url.origin + url.pathname); return route.abort() })
    page = await context.newPage(); page.on('pageerror', e => errors.push(e.message)); page.setDefaultTimeout(15000)
    await page.goto(ui + '/tasks')
    const openTree = async () => {
      await page.locator(`[data-run-center-id="subtask:${seed.node_id}"]`).getByRole('button', { name: '子任务与尝试', exact: true }).click()
      await page.locator('.child-list button').first().click()
      await page.getByRole('heading', { name: '核对未确认的外部调用' }).waitFor()
    }
    await openTree()
    await page.getByText(/第 1 次 · 等待处理 · 0 次工具结果/).click()
    await page.getByText(/本次尝试停在：mcp__fixture__search/).waitFor()
    check('Run center exposes durable child and original attempt with unknown external receipt')
    await page.getByRole('button', { name: '查看本次调用与用量', exact: true }).click()
    await page.locator('.attempt-calls').getByText(/synthetic-child/).waitFor()
    assert.match(await page.locator('.attempt-calls').innerText(), /合成测试/)
    assert.match(await page.locator('.attempt-calls').innerText(), /prompt_tokens/)
    check('Per-attempt call facts show child identity and explicitly separate synthetic usage')
    await page.getByLabel('子任务核对依据').fill('外部请求日志已检查；此处为合成夹具')
    await page.getByLabel('确认已核对子任务外部操作').check()
    await api('/test-fixture/subtasks/' + seed.run_id + '/revision', {})
    await page.getByRole('button', { name: '记录跳过并保留缺项', exact: true }).click()
    await page.locator('.subtask-tree [role="alert"]').filter({ hasText: '已变化' }).waitFor()
    assert.equal(await page.getByLabel('子任务核对依据').inputValue(), '外部请求日志已检查；此处为合成夹具')
    check('Stale reconciliation is rejected and review draft remains intact')
    await page.getByRole('button', { name: '刷新子任务', exact: true }).click()
    await until(async () => !(await page.getByRole('button', { name: '刷新子任务', exact: true }).isDisabled()), 'refresh')
    await page.getByLabel('子任务核对依据').fill('再次核对后选择跳过，保留缺项')
    await page.getByRole('button', { name: '记录跳过并保留缺项', exact: true }).click()
    await page.getByText('已记录跳过，该调用仍是结果未知；回到所属任务继续补齐。', { exact: true }).waitFor()
    const tree = await api('/subtasks/runs/' + seed.run_id), detail = await api('/subtasks/nodes/' + seed.node_id + '/attempts')
    assert.equal(tree.recovery.safe_children, true); assert.equal(tree.parent.active, false); assert.equal(detail.node.attempt, 1)
    assert.equal(detail.resolutions.length, 1); assert.ok(detail.items[0].progress.pending)
    check('Explicit skip saves immutable user receipt while original attempt and parent admission remain unchanged')
    await page.getByText('外部调用核对记录', { exact: true }).click()
    await page.screenshot({ path: path.join(output, 'subtask-desktop.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await until(async () => { const box = await page.locator('.subtask-tree').boundingBox(); return box && box.x >= 0 && box.x + box.width <= 391 }, 'narrow tree')
    await page.screenshot({ path: path.join(output, 'subtask-narrow.png'), fullPage: true })
    check('Child details and manual reconciliation remain reachable at 390px')
    await page.getByRole('button', { name: '回到所属任务处理恢复', exact: true }).click()
    await until(async () => page.url().includes('/chat?') && page.url().includes(seed.run_id), 'route back to exact parent')
    assert.equal((await api('/subtasks/runs/' + seed.run_id)).parent.active, false)
    check('Recovery entry navigates to exact parent without starting an isolated child')
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: true, checks, browser_errors: errors, external_requests: external, real_provider_calls: 0 }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (e) { if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {}); await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: false, checks, error: String(e), browser_errors: errors, external_requests: external }, null, 2)); throw e }
  finally { if (browser) await browser.close(); if (vite) await vite.close(); if (backend && backend.exitCode === null) backend.kill() }
}
main().catch(e => { process.stderr.write(String(e)); process.exitCode = 1 })
