const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) {
  const end = Date.now() + timeout
  while (!(await check())) { assert.ok(Date.now() < end, label); await delay(80) }
}
async function port() {
  const s = net.createServer(); await new Promise(resolve => s.listen(0, '127.0.0.1', resolve))
  const p = s.address().port; await new Promise(resolve => s.close(resolve)); return p
}
async function main() {
  const output = path.join(root, 'verification/mcp-user-oauth/e2e')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'), LLM_API_KEY: '', EMBEDDING_API_KEY: '',
    LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  let backend, vite, browser, page, logs = ''
  const checks = [], errors = [], external = []
  const check = name => checks.push({ name, passed: true })
  try {
    const backendPort = await port()
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'uvicorn', 'mcp_oauth_ui_fixture:app', '--app-dir', 'tests', '--host', '127.0.0.1', '--port', String(backendPort)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', d => { logs += d }); backend.stderr.on('data', d => { logs += d })
    const base = 'http://127.0.0.1:' + backendPort
    const api = async (route, body, method = body === undefined ? 'GET' : 'POST') => {
      const response = await fetch(base + '/api' + route, { method, headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) })
      assert.equal(response.status, 200, route + ': ' + await response.clone().text()); return response.json()
    }
    await until(async () => { if (backend.exitCode !== null) throw Error(logs); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'startup')
    const dirA = path.join(isolated, 'project-a'), dirB = path.join(isolated, 'project-b')
    await fs.mkdir(dirA); await fs.mkdir(dirB)
    const a = await api('/projects', { path: dirA, name: '项目 A 技能测试' }), b = await api('/projects', { path: dirB, name: '项目 B 技能测试' })
    await api('/skills?project_id=' + a.id, { name: 'shared-review', title: '同名审阅', content: '仅项目 A 的操作清单' })
    await api('/skills?project_id=' + b.id, { name: 'shared-review', title: '同名审阅', content: '仅项目 B 的操作清单' })
    await api('/mcp/servers', { name: 'OAuth 界面测试', transport: 'http', enabled: true,
      url: base + '/oauth-fixture/mcp', oauth: { flow: 'authorization_code' } }, 'PUT')
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false } } } })
    await vite.listen()
    const ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.hostname === '127.0.0.1' || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname); return route.abort()
    })
    page = await context.newPage(); page.on('pageerror', e => errors.push(e.message)); page.setDefaultTimeout(15000)
    await page.goto(ui + '/settings')
    await page.getByRole('button', { name: '技能库', exact: true }).click()
    await page.getByLabel('技能范围').selectOption(a.id)
    await page.getByRole('button').filter({ hasText: '同名审阅' }).click()
    assert.equal(await page.locator('.skill-body').innerText(), '仅项目 A 的操作清单')
    await page.getByLabel('技能范围').selectOption(b.id)
    await page.getByRole('button').filter({ hasText: '同名审阅' }).click()
    assert.equal(await page.locator('.skill-body').innerText(), '仅项目 B 的操作清单')
    check('Two project scopes show different bodies for the same skill name')
    await page.getByRole('button', { name: '禁用技能', exact: true }).click()
    await until(async () => (await api('/skills/shared-review?project_id=' + b.id)).disabled, 'disable scoped skill')
    assert.equal((await api('/skills/shared-review?project_id=' + a.id)).disabled, false)
    check('Disabling project B does not affect project A')
    await fs.writeFile(path.join(env.DATA_DIR, 'project_skills/projects', a.id, 'shared-review/SKILL.md'), '新版本：项目 A 修订后清单', 'utf8')
    await page.getByLabel('技能范围').selectOption(a.id)
    await page.getByRole('button').filter({ hasText: '同名审阅' }).click().catch(async () => { await page.getByRole('button').filter({ hasText: 'shared-review' }).click() })
    await page.getByRole('button', { name: '已阅读，信任此版本', exact: true }).click()
    await until(async () => (await api('/skills/shared-review?project_id=' + a.id)).trusted, 'review changed bytes')
    await page.screenshot({ path: path.join(output, 'project-skill-review.png'), fullPage: true })
    check('Changed bytes require review of the exact revision in settings')
    await page.getByRole('button', { name: '编辑正文', exact: true }).click()
    await page.getByLabel('编辑技能正文').fill('通过界面编辑的项目 A 清单')
    await page.getByRole('button', { name: '保存并信任新版本', exact: true }).click()
    await until(async () => (await api('/skills/shared-review?project_id=' + a.id)).content === '通过界面编辑的项目 A 清单', 'edit skill body')
    check('Editing owned skill content creates a reviewed version with compare-and-swap')
    await page.getByRole('button', { name: 'MCP 服务器', exact: true }).click()
    const card = page.locator('.mcp-row').filter({ hasText: 'OAuth 界面测试' })
    await card.getByRole('button', { name: '登录授权', exact: true }).click()
    await card.getByRole('link', { name: /前往.*登录/ }).waitFor()
    await card.getByRole('button', { name: '取消授权', exact: true }).click()
    await until(async () => (await api('/mcp/servers/' + encodeURIComponent('OAuth 界面测试') + '/authorization')).state === 'revoked', 'cancel login')
    check('Cancellation closes the pending authorization and revokes local state')
    await card.getByRole('button', { name: '登录授权', exact: true }).click()
    const popupPromise = page.waitForEvent('popup')
    await card.getByRole('link', { name: /前往.*登录/ }).click()
    const popup = await popupPromise
    await popup.getByRole('link', { name: '同意本地测试授权' }).click()
    await until(async () => (await api('/mcp/servers/' + encodeURIComponent('OAuth 界面测试') + '/authorization')).state === 'authorized', 'local auth callback')
    await popup.close()
    await card.getByRole('button', { name: '撤销授权', exact: true }).waitFor()
    await card.getByTitle('连接', { exact: true }).click()
    await until(async () => (await api('/mcp/servers')).servers.find(s => s.name === 'OAuth 界面测试').status === 'connected', 'connect authorized MCP from UI')
    await card.getByText('1 个工具', { exact: true }).waitFor()
    const discovered = (await api('/mcp/discovered-tools')).tools.find(t => t.server === 'OAuth 界面测试')
    assert.ok(discovered)
    const invoked = await api('/test-fixture/mcp-tool', { name: discovered.name })
    assert.equal(invoked.content, 'local fixture', JSON.stringify(invoked))
    check('Authorized connection discovers and invokes the actual MCP fixture tool')
    await page.screenshot({ path: path.join(output, 'oauth-authorized.png'), fullPage: true })
    check('Browser authorization returns through the fixed loopback handler and persists the grant')
    await page.reload()
    await page.getByRole('button', { name: 'MCP 服务器', exact: true }).click()
    await card.getByRole('button', { name: '撤销授权', exact: true }).click()
    await until(async () => (await api('/mcp/servers/' + encodeURIComponent('OAuth 界面测试') + '/authorization')).state === 'revoked', 'revoke login')
    assert.ok((await api('/test-fixture/mcp-tool', { name: discovered.name })).error)
    assert.ok(!(await api('/mcp/discovered-tools')).tools.some(t => t.server === 'OAuth 界面测试'))
    check('Reload retains grant status; local and remote revoke are reachable')
    await page.setViewportSize({ width: 390, height: 844 })
    await card.scrollIntoViewIfNeeded()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: path.join(output, 'oauth-narrow.png'), fullPage: true })
    check('Narrow viewport retains accessible authorization controls')
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: true, checks, browser_errors: errors, external_requests: external, real_provider_calls: 0 }, null, 2))
    process.stdout.write(JSON.stringify({ success: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (e) {
    await fs.writeFile(path.join(output, 'fixture-backend.log'), logs)
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ success: false, checks, error: String(e), browser_errors: errors, external_requests: external }, null, 2))
    throw e
  } finally { if (browser) await browser.close(); if (vite) await vite.close(); if (backend && backend.exitCode === null) backend.kill() }
}
main().catch(e => { process.stderr.write(String(e)); process.exitCode = 1 })
