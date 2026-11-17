// Real Vue and API/SQLite in an isolated data directory. No provider requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((r) => setTimeout(r, ms))
async function freePort() {
  const server = net.createServer()
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const port = server.address().port
  await new Promise((r) => server.close(r))
  return port
}
async function main() {
  const out = path.join(root, 'verification/memory-governance/ui')
  await fs.mkdir(out, { recursive: true })
  const data = await fs.mkdtemp(path.join(out, 'isolated-'))
  const port = await freePort(),
    base = `http://127.0.0.1:${port}`
  function startServer() {
    const serverProcess = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'tests.memory_governance_ui_server', String(port)],
      {
        cwd: path.join(root, 'backend'),
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          ...process.env,
          DATA_DIR: data,
          LLM_API_KEY: '',
          LLM_DEMO_MODE: '1',
          EMBEDDING_API_KEY: '',
          REDIS_URL: '',
          PYTHONUTF8: '1',
        },
      }
    )
    serverProcess.stdout.on('data', (b) => (log += b))
    serverProcess.stderr.on('data', (b) => (log += b))
    return serverProcess
  }
  let log = '',
    server = startServer(),
    vite,
    browser,
    page,
    second
  const checks = [],
    errors = [],
    external = []
  let result = { status: 'failed', provider_requests: 0 }
  try {
    const end = Date.now() + 45000
    while (true) {
      if (server.exitCode !== null) throw Error(log)
      try {
        if ((await fetch(base + '/api/health')).ok) break
      } catch {}
      assert(Date.now() < end, 'server startup timeout')
      await pause(100)
    }
    const { createServer } = await import('vite')
    vite = await createServer({
      root: path.join(root, 'frontend'),
      configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error',
      server: {
        host: '127.0.0.1',
        port: await freePort(),
        strictPort: true,
        hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } },
      },
    })
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1365, height: 960 } })
    await context.route('**/*', (route) => {
      const u = new URL(route.request().url())
      if (u.origin === ui || ['data:', 'blob:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname)
      return route.abort()
    })
    page = await context.newPage()
    second = await context.newPage()
    for (const p of [page, second]) {
      p.setDefaultTimeout(20000)
      p.on('pageerror', (e) => errors.push(e.message))
    }
    async function open(p) {
      await p.goto(ui + '/chat?conv=memory-ui')
      await p.locator('[data-record-id="qa_memory_22"]').waitFor()
      await p.evaluate(() => window.dispatchEvent(new Event('scholarflow:open-memory')))
      await p.getByRole('region', { name: '逐条记忆管理' }).waitFor()
      await p.locator('[data-memory-id="qa_memory_22"]').waitFor()
    }
    const item = (p) => p.locator('[data-memory-id="qa_memory_22"]')
    const dialog = (p) => p.getByRole('dialog', { name: '会话记忆' })
    const api = async (route, body, method = 'POST') => {
      const response = await page.request.fetch(
        ui + route,
        body === undefined ? {} : { method, data: body }
      )
      assert.equal(response.status(), 200, await response.text())
      return response.json()
    }
    await open(page)
    await dialog(page).getByText('最近任务实际载入的记忆').waitFor()
    await dialog(page).getByText('与当前记忆一致', { exact: true }).waitFor()
    checks.push('real memory panel displays persisted actual-load receipt')
    await item(page).getByRole('button', { name: '编辑记忆', exact: true }).click()
    await item(page)
      .getByLabel('用于后续回答的记忆文本', { exact: true })
      .fill('用户确认的新记忆内容')
    await item(page).getByRole('button', { name: '保存记忆', exact: true }).click()
    await dialog(page).getByText('记忆已变更，旧回执已失效', { exact: true }).waitFor()
    let values = await api('/api/memory/memory-ui/items')
    assert.equal(values.items[0].text, '用户确认的新记忆内容')
    let original = await api('/api/conversations/memory-ui')
    assert.equal(
      original.messages.find((m) => m.id === 'qa_memory_22').answer,
      '原始聊天内容 23，始终保留。'
    )
    checks.push('editing persists replacement without changing original chat')
    await open(second)
    await item(second).getByRole('button', { name: '编辑记忆', exact: true }).click()
    await item(second)
      .getByLabel('用于后续回答的记忆文本', { exact: true })
      .fill('另一个窗口尚未保存的草稿')
    await item(page).getByRole('button', { name: '排除记忆', exact: true }).click()
    await item(page)
      .getByText(/^已排除/)
      .waitFor()
    await item(second).getByRole('button', { name: '保存记忆', exact: true }).click()
    await dialog(second).getByRole('alert').filter({ hasText: '其他窗口' }).waitFor()
    assert.equal(
      await item(second).getByLabel('用于后续回答的记忆文本', { exact: true }).inputValue(),
      '另一个窗口尚未保存的草稿'
    )
    await item(second).getByRole('button', { name: '保存记忆', exact: true }).click()
    await item(second).getByRole('button', { name: '编辑记忆', exact: true }).waitFor()
    values = await api('/api/memory/memory-ui/items')
    assert.equal(values.items[0].text, '另一个窗口尚未保存的草稿')
    checks.push('two windows enforce CAS and retain unsaved text for explicit retry')
    await item(second).getByRole('button', { name: '删除记忆', exact: true }).click()
    await item(second).getByRole('button', { name: '确认删除记忆', exact: true }).click()
    await item(second).getByText('已删除记忆', { exact: true }).waitFor()
    const terminated = new Promise((resolve) => server.once('exit', resolve))
    server.kill()
    await terminated
    server = startServer()
    const restartDeadline = Date.now() + 45000
    while (true) {
      if (server.exitCode !== null) throw Error(log)
      try {
        if ((await fetch(base + '/api/health')).ok) break
      } catch {}
      assert(Date.now() < restartDeadline, 'backend restart timeout')
      await pause(100)
    }
    await open(page)
    await item(page).getByText('已删除记忆', { exact: true }).waitFor()
    values = await api('/api/memory/memory-ui/items')
    assert.equal(values.items[0].text, '')
    original = await api('/api/conversations/memory-ui')
    assert.equal(original.messages.length, 23)
    await dialog(page).screenshot({ path: path.join(out, 'memory-desktop.png') })
    checks.push('memory tombstone survives a real backend restart and original 23 records remain')
    await dialog(page).getByRole('button', { name: '下一页', exact: true }).click()
    await dialog(page).getByText('21–23 / 23', { exact: true }).waitFor()
    await dialog(page).getByRole('button', { name: '上一页', exact: true }).click()
    await item(page).getByRole('button', { name: '重新加入记忆', exact: true }).click()
    await item(page).getByText('参与记忆', { exact: true }).waitFor()
    checks.push('pagination works and only explicit include restores original memory')
    await page.setViewportSize({ width: 720, height: 1000 })
    await dialog(page).screenshot({ path: path.join(out, 'memory-narrow.png') })
    assert(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      'narrow viewport fits'
    )
    await api(
      '/api/conversations/memory-ui/archive',
      { archived: true, expected_revision: 0 },
      'PUT'
    )
    await open(page)
    assert(await item(page).getByRole('button', { name: '编辑记忆', exact: true }).isDisabled())
    checks.push('archived conversations show memory but disable mutations; narrow layout fits')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    result = {
      status: 'passed',
      checks,
      provider_requests: 0,
      browser_errors: errors,
      external_requests: external,
    }
  } catch (error) {
    result.error = String(error.stack || error)
    if (page)
      await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    throw error
  } finally {
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(result, null, 2))
    await fs.writeFile(path.join(out, 'server.log'), log)
    if (browser) await browser.close()
    if (vite) await vite.close()
    server.kill()
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
