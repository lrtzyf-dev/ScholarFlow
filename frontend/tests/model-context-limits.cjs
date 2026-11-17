// Actual Vue editor -> real isolated API -> loopback-only synthetic provider.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const http = require('node:http')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, label)
    await pause(60)
  }
}
async function port() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const value = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return value
}
async function main() {
  const output = path.join(root, 'verification/model-context-limits/e2e')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const checks = [],
    errors = [],
    external = [],
    calls = []
  const check = (name) => checks.push({ name, passed: true })
  let backend,
    vite,
    browser,
    provider,
    logs = ''
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    LLM_DEMO_MODE: '1',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  try {
    provider = http.createServer(async (req, res) => {
      const chunks = []
      for await (const chunk of req) chunks.push(chunk)
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
      calls.push({ path: req.url, body })
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(
        JSON.stringify(
          req.url.endsWith('/models')
            ? { data: [{ id: 'Model-CaSe', context_window_tokens: 42, max_output_tokens: 1 }] }
            : {
                choices: [{ message: { role: 'assistant', content: 'OK' }, finish_reason: 'stop' }],
              }
        )
      )
    })
    await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
    const providerBase = 'http://127.0.0.1:' + provider.address().port + '/v1'
    const base = 'http://127.0.0.1:' + (await port())
    backend = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', new URL(base).port],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
    )
    backend.stdout.on('data', (d) => {
      logs += d
    })
    backend.stderr.on('data', (d) => {
      logs += d
    })
    const api = async (route) => {
      const response = await fetch(base + '/api' + route)
      assert.equal(response.status, 200, await response.clone().text())
      return response.json()
    }
    await until(async () => {
      if (backend.exitCode !== null) throw Error(logs)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'API startup')
    const { createServer } = await import('vite')
    vite = await createServer({
      root: path.join(root, 'frontend'),
      configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error',
      server: {
        host: '127.0.0.1',
        port: await port(),
        strictPort: true,
        hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false } },
      },
    })
    await vite.listen()
    const ui = 'http://127.0.0.1:' + vite.httpServer.address().port
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    await page.goto(ui + '/settings')
    const manager = page.locator('.model-profiles-manager')
    await manager.locator('.profile-add').click()
    const editor = page.locator('.profile-editor')
    await editor.getByLabel('配置名称', { exact: true }).fill('上下文配置验收')
    await editor.getByLabel('接口地址', { exact: true }).fill(providerBase)
    await editor.getByLabel('模型 ID', { exact: true }).fill('Model-CaSe')
    await editor.getByLabel('上下文窗口（tokens）', { exact: true }).fill('8192')
    await editor.getByLabel('最大输出（tokens）', { exact: true }).fill('8192')
    await editor.getByRole('button', { name: '保存模型', exact: true }).click()
    await editor.getByRole('alert').filter({ hasText: '必须小于' }).waitFor()
    assert.ok(!(await api('/model-profiles')).profiles.some((p) => p.name === '上下文配置验收'))
    check(
      'Equal output/window is rejected in the actual editor without saving or contacting provider'
    )
    await editor.getByLabel('最大输出（tokens）', { exact: true }).fill('512')
    await editor.getByRole('button', { name: '获取模型列表', exact: true }).click()
    await editor.getByText('已获取1个模型', { exact: true }).waitFor()
    assert.equal(
      await editor.getByLabel('上下文窗口（tokens）', { exact: true }).inputValue(),
      '8192'
    )
    assert.equal(await editor.getByLabel('最大输出（tokens）', { exact: true }).inputValue(), '512')
    check('Discovery does not infer capacity from provider model-list metadata')
    await editor.getByRole('button', { name: '保存模型', exact: true }).click()
    await editor.waitFor({ state: 'hidden' })
    const profile = (await api('/model-profiles')).profiles.find((p) => p.name === '上下文配置验收')
    assert.equal(profile.context_window_tokens, 8192)
    assert.equal(profile.max_output_tokens, 512)
    assert.equal(profile.context_limits_verified, false)
    const row = manager.locator(`[data-profile-id="${profile.id}"]`)
    await row.getByText(/上下文 8,192 tokens/).waitFor()
    await row.getByRole('button', { name: '测试连接', exact: true }).click()
    await manager.getByText(/基础对话连接成功/).waitFor()
    const request = calls.find((c) => c.path.endsWith('/chat/completions'))
    assert.equal(request.body.max_tokens, 512)
    check(
      'Saved model reaches real backend and actual loopback provider wire with configured output cap'
    )
    await row.getByRole('button', { name: '编辑 上下文配置验收', exact: true }).click()
    assert.equal(
      await editor.getByLabel('上下文窗口（tokens）', { exact: true }).inputValue(),
      '8192'
    )
    await editor.getByLabel('最大输出（tokens）', { exact: true }).fill('2.5')
    await editor.getByRole('button', { name: '保存模型', exact: true }).click()
    await editor.getByRole('alert').filter({ hasText: '整数' }).waitFor()
    await editor.getByLabel('上下文窗口（tokens）', { exact: true }).fill('32768')
    await editor.getByLabel('最大输出（tokens）', { exact: true }).fill('2048')
    await editor.getByRole('button', { name: '保存模型', exact: true }).click()
    await editor.waitFor({ state: 'hidden' })
    const updated = (await api('/model-profiles')).profiles.find((p) => p.id === profile.id)
    assert.equal(updated.context_window_tokens, 32768)
    assert.equal(updated.max_output_tokens, 2048)
    assert.equal(updated.version, 2)
    await page.screenshot({ path: path.join(output, 'saved-limits-desktop.png'), fullPage: true })
    check('Editing rejects fractional input and persists a valid new version')
    await page.setViewportSize({ width: 390, height: 844 })
    await row.getByRole('button', { name: '编辑 上下文配置验收', exact: true }).click()
    const windowInput = editor.getByLabel('上下文窗口（tokens）', { exact: true })
    const outputInput = editor.getByLabel('最大输出（tokens）', { exact: true })
    await outputInput.scrollIntoViewIfNeeded()
    const a = await windowInput.boundingBox(),
      b = await outputInput.boundingBox()
    assert.ok(b.y > a.y + a.height, 'narrow inputs should stack')
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    await page.screenshot({ path: path.join(output, 'limits-editor-mobile.png'), fullPage: true })
    await editor.getByLabel('配置名称', { exact: true }).focus()
    await page.keyboard.press('Shift+Tab')
    assert.equal(
      await editor
        .getByRole('button', { name: '关闭模型表单', exact: true })
        .evaluate((node) => node === document.activeElement),
      true
    )
    await page.keyboard.press('Shift+Tab')
    assert.equal(
      await editor
        .getByRole('button', { name: '保存模型', exact: true })
        .evaluate((node) => node === document.activeElement),
      true
    )
    await page.keyboard.press('Escape')
    await editor.waitFor({ state: 'hidden' })
    check('390px layout has no horizontal overflow and keyboard trap/Escape remain usable')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          checks,
          browser_errors: errors,
          external_requests: external,
          real_provider_calls: 0,
          synthetic_loopback_requests: calls.length,
          chat_output_cap: request.body.max_tokens,
        },
        null,
        2
      )
    )
    console.log(
      JSON.stringify({ checks: checks.length, errors: errors.length, real_provider_calls: 0 })
    )
  } catch (error) {
    await fs.writeFile(path.join(output, 'failure.log'), String(error.stack || error) + '\n' + logs)
    throw error
  } finally {
    await browser?.close()
    await vite?.close()
    if (backend && backend.exitCode === null) {
      backend.kill()
      await Promise.race([new Promise((resolve) => backend.once('exit', resolve)), pause(10000)])
    }
    if (provider) await new Promise((resolve) => provider.close(resolve))
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
