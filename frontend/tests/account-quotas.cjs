// Real Vue / isolated HTTP policy, zero external provider requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, label) {
  const end = Date.now() + 45000
  while (!(await fn())) {
    assert.ok(Date.now() < end, label)
    await pause(100)
  }
}
async function freePort() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}
async function main() {
  const output = path.join(root, 'verification/account-quotas/ui')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
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
  const python = path.join(root, 'backend/.venv/Scripts/python.exe')
  let backend,
    browser,
    vite,
    page,
    backendLog = ''
  const errors = [],
    external = [],
    checks = []
  try {
    const port = await freePort(),
      base = `http://127.0.0.1:${port}`
    const start = async () => {
      backend = spawn(
        python,
        ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
        {
          cwd: path.join(root, 'backend'),
          env,
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'pipe'],
        }
      )
      backend.stdout.on('data', (v) => (backendLog += v))
      backend.stderr.on('data', (v) => (backendLog += v))
      await until(async () => {
        if (backend.exitCode !== null) throw Error(backendLog)
        try {
          return (await fetch(base + '/api/health')).ok
        } catch {
          return false
        }
      }, 'backend startup')
    }
    const stop = async () => {
      const child = backend
      if (!child || child.exitCode !== null) return
      await new Promise((resolve) => {
        child.once('exit', resolve)
        child.kill()
      })
      backend = undefined
    }
    await start()
    const api = async (url, body, method = 'POST') => {
      const response = await fetch(base + '/api' + url, {
        method: body ? method : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`)
      return response.json()
    }
    const first = await api('/model-profiles', {
      name: '前台模型',
      base_url: 'https://quota.example.test/v1',
      model_id: 'synthetic-one',
      api_key: 'synthetic-ui-key-one',
    })
    const second = await api('/model-profiles', {
      name: '后台模型',
      base_url: 'https://quota.example.test/v1',
      model_id: 'synthetic-two',
      api_key: 'synthetic-ui-key-two',
    })
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
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(ui + '/settings')
    const panel = page.getByRole('region', { name: '账号配额与并发', exact: true })
    await panel.getByLabel('分组名称', { exact: true }).fill('科研账号')
    await panel.getByLabel('接口来源', { exact: true }).fill('https://quota.example.test')
    await panel.getByLabel('最大并发', { exact: true }).fill('2')
    await panel.getByRole('button', { name: '创建账号组', exact: true }).click()
    await panel.getByRole('article', { name: '账号组 科研账号', exact: true }).waitFor()
    let current = await api('/account-quotas'),
      target = current.groups[0]
    for (const model of [first, second]) {
      const row = panel.getByRole('article', { name: `连接 ${model.name}`, exact: true })
      await row.getByLabel('目标账号组', { exact: true }).selectOption(target.id)
      await row.getByRole('button', { name: '审阅归组', exact: true }).click()
      await panel
        .getByRole('region', { name: '确认账号归组', exact: true })
        .getByRole('button', { name: '确认归组', exact: true })
        .click()
      await until(
        async () =>
          !(await panel.getByRole('region', { name: '确认账号归组', exact: true }).count()),
        'binding saved'
      )
    }
    current = await api('/account-quotas')
    assert.ok(
      current.accounts
        .filter((a) => ['profile:' + first.id, 'profile:' + second.id].includes(a.id))
        .every((a) => a.group_id === target.id)
    )
    checks.push({
      name: 'actual_Vue_explicit_group_two_credentials_without_provider_calls',
      passed: true,
    })
    const group = panel.getByRole('article', { name: '账号组 科研账号', exact: true })
    await group.getByLabel('并发数', { exact: true }).fill('4')
    await group.getByRole('button', { name: '保存账号组', exact: true }).click()
    await until(
      async () => (await api('/account-quotas')).groups[0].concurrency === 4,
      'limit saved'
    )
    await panel.screenshot({ path: path.join(output, 'explicit-account-groups.png') })
    checks.push({ name: 'actual_Vue_concurrency_save_persistent_policy', passed: true })
    const other = await context.newPage()
    other.on('pageerror', (e) => errors.push(e.message))
    await other.goto(ui + '/settings')
    const otherPanel = other.getByRole('region', { name: '账号配额与并发', exact: true })
    await otherPanel.getByRole('article', { name: '账号组 科研账号', exact: true }).waitFor()
    current = await api('/account-quotas')
    await api(
      `/account-quotas/groups/${target.id}`,
      { expected_revision: current.revision, name: target.name, concurrency: 8, state: 'active' },
      'PUT'
    )
    const otherGroup = otherPanel.getByRole('article', { name: '账号组 科研账号', exact: true })
    await otherGroup.getByLabel('并发数', { exact: true }).fill('16')
    await otherGroup.getByRole('button', { name: '保存账号组', exact: true }).click()
    await otherPanel.getByRole('alert').filter({ hasText: '其他窗口修改' }).waitFor()
    assert.equal((await api('/account-quotas')).groups[0].concurrency, 8)
    await otherPanel.screenshot({ path: path.join(output, 'two-window-cas-conflict.png') })
    checks.push({
      name: 'actual_two_window_stale_revision_conflict_does_not_overwrite_limit',
      passed: true,
    })
    await otherPanel.getByRole('button', { name: '刷新配额设置', exact: true }).click()
    await until(
      async () => (await otherGroup.getByLabel('并发数', { exact: true }).inputValue()) === '8',
      'refresh current policy'
    )
    await otherPanel.getByLabel('分组名称', { exact: true }).fill('更正账号组')
    await otherPanel.getByLabel('接口来源', { exact: true }).fill('https://quota.example.test')
    await otherPanel.getByLabel('最大并发', { exact: true }).fill('4')
    await otherPanel.getByRole('button', { name: '创建账号组', exact: true }).click()
    await otherPanel.getByRole('article', { name: '账号组 更正账号组', exact: true }).waitFor()
    current = await api('/account-quotas')
    const moved = current.groups.find((g) => g.name === '更正账号组')
    await otherGroup.getByLabel('并发数', { exact: true }).fill('12')
    const account = otherPanel.getByRole('article', { name: '连接 前台模型', exact: true })
    await account.getByLabel('目标账号组', { exact: true }).selectOption(moved.id)
    await account.getByRole('button', { name: '审阅迁移', exact: true }).click()
    await otherPanel
      .getByRole('region', { name: '确认账号归组', exact: true })
      .getByRole('button', { name: '确认归组', exact: true })
      .click()
    await until(
      async () =>
        (await api('/account-quotas')).accounts.find((a) => a.id === 'profile:' + first.id)
          .group_id === moved.id,
      'explicit migration'
    )
    assert.equal(await otherGroup.getByLabel('并发数', { exact: true }).inputValue(), '12')
    assert.equal(
      (await api('/account-quotas')).groups.find((g) => g.id === target.id).concurrency,
      8
    )
    checks.push({ name: 'actual_Vue_explicit_idle_account_migration', passed: true })
    const saved = await api('/account-quotas')
    await stop()
    await start()
    await other.reload()
    await otherPanel.getByRole('article', { name: '账号组 更正账号组', exact: true }).waitFor()
    const restored = await api('/account-quotas')
    assert.equal(restored.revision, saved.revision)
    assert.equal(restored.accounts.find((a) => a.id === 'profile:' + first.id).group_id, moved.id)
    checks.push({
      name: 'actual_backend_restart_preserves_policy_and_account_mapping',
      passed: true,
    })
    await other.setViewportSize({ width: 760, height: 950 })
    const limit = otherPanel.getByLabel('最大并发', { exact: true })
    await limit.focus()
    await other.keyboard.press('ArrowUp')
    assert.equal(await limit.inputValue(), '3')
    await other.keyboard.press('ArrowDown')
    assert.equal(await limit.inputValue(), '2')
    assert.ok(
      await other.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)
    )
    await otherPanel.screenshot({ path: path.join(output, 'narrow-account-quotas.png') })
    checks.push({ name: '760px_keyboard_labeled_controls_no_document_overflow', passed: true })
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          passed: true,
          checks,
          browser_errors: errors,
          external_requests: external,
          real_provider_calls: 0,
          user_credentials_used: false,
          scope:
            'Synthetic configured credentials; real Vue, HTTP policy, two windows and backend restart. No provider HTTP requests.',
        },
        null,
        2
      )
    )
  } catch (error) {
    if (page)
      await page
        .screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
        .catch(() => {})
    await fs.writeFile(
      path.join(output, 'failure.json'),
      JSON.stringify({ error: String(error), backendLog, errors, external }, null, 2)
    )
    throw error
  } finally {
    await browser?.close()
    await vite?.close()
    backend?.kill()
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
