// Actual ChatView/Vue + isolated FastAPI; no provider or user data.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function port() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const value = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return value
}
async function until(fn, label) {
  const end = Date.now() + 45000
  while (!(await fn())) {
    assert(Date.now() < end, label)
    await pause(80)
  }
}
async function main() {
  const out = path.join(root, 'verification/task-requirements/ui')
  await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-'))
  const backendPort = await port(),
    base = `http://127.0.0.1:${backendPort}`
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
  let backend,
    vite,
    browser,
    page,
    log = ''
  const checks = [],
    errors = [],
    external = []
  let report = { status: 'failed', checks, provider_requests: 0 }
  const api = async (url, body) => {
    const response = await fetch(base + url, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    assert.equal(response.status, 200, await response.clone().text())
    return response.json()
  }
  try {
    backend = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'tests.requirements_ui_server', String(backendPort)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true }
    )
    backend.stdout.on('data', (b) => (log += b))
    backend.stderr.on('data', (b) => (log += b))
    await until(async () => {
      if (backend.exitCode !== null) throw Error(log)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'API start')
    const [a, b] = await api('/__test__/seed-requirements', {})
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
        proxy: { '/api': { target: base, ws: true, changeOrigin: false } },
      },
    })
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } })
    await context.route('**/*', (route) => {
      const u = new URL(route.request().url())
      if (u.origin === ui || ['blob:', 'data:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname)
      return route.abort()
    })
    const url = (item) => `${ui}/chat?conv=${item.conversation_id}`
    const goal = (item) => `/api/goal/${item.goal_id}/requirements`
    const snapshot = (item) => api(`${goal(item)}?conversation_id=${item.conversation_id}`)
    const open = async (target, readOnly = false) => {
      const detail = target.locator('.task-requirements')
      await detail.locator('> summary').click()
      await detail.getByRole('button', { name: '添加要求或限制', exact: true }).waitFor()
      await until(
        async () =>
          readOnly ||
          !(await detail.getByRole('button', { name: '添加要求或限制', exact: true }).isDisabled()),
        'requirements loaded'
      )
      return detail
    }
    const addManual = async (detail, title) => {
      await detail.getByRole('button', { name: '添加要求或限制', exact: true }).click()
      await detail.getByLabel('要求描述', { exact: true }).fill(title)
      await detail.getByRole('button', { name: '纳入任务要求', exact: true }).click()
    }
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(url(a))
    let detail = await open(page)
    await addManual(detail, '核对主要结论')
    const row = detail.locator('[data-requirement-id]').filter({ hasText: '核对主要结论' })
    await row.waitFor()
    const firstId = await row.getAttribute('data-requirement-id')
    await row.getByRole('button', { name: '核对完成', exact: true }).click()
    await detail.getByLabel('核对依据或受阻原因', { exact: true }).fill('已对照固定原文逐条检查')
    await detail.getByRole('button', { name: '确认此要求已满足', exact: true }).click()
    await row.getByText('用户已核对', { exact: false }).waitFor()
    await row.getByRole('button', { name: '修改此要求', exact: true }).click()
    await detail.getByLabel('要求描述', { exact: true }).fill('核对主要结论和局限')
    await detail.getByRole('button', { name: '保存此要求的新版本', exact: true }).click()
    const replaced = detail
      .locator('[data-requirement-id]')
      .filter({ hasText: '核对主要结论和局限' })
    await replaced.getByText('待核对', { exact: true }).waitFor()
    const replacement = (await snapshot(a)).requirements
    assert.equal(replacement.find((r) => r.id === firstId).status, 'superseded')
    assert.equal(replacement.find((r) => r.title === '核对主要结论和局限').evidence.length, 0)
    await detail.getByText('修改历史', { exact: true }).click()
    await detail.getByText('已替换', { exact: false }).waitFor()
    await detail.getByText('已对照固定原文逐条检查', { exact: true }).waitFor()
    checks.push(
      'historical answer exposes original scoped goal; manual review and isolated replacement preserve history without inheriting proof'
    )

    const second = await context.newPage()
    second.on('pageerror', (e) => errors.push(e.message))
    await second.goto(url(a))
    const secondDetail = await open(second)
    await secondDetail.getByRole('button', { name: '添加要求或限制', exact: true }).click()
    await secondDetail.getByLabel('要求描述', { exact: true }).fill('冲突中保留的草稿')
    await addManual(detail, '另一窗口新增')
    await detail
      .locator('[data-requirement-id]')
      .getByText('另一窗口新增', { exact: true })
      .waitFor()
    await secondDetail.getByRole('button', { name: '纳入任务要求', exact: true }).click()
    await secondDetail.getByRole('alert').filter({ hasText: '其他窗口更新' }).waitFor()
    assert.equal(
      await secondDetail.getByLabel('要求描述', { exact: true }).inputValue(),
      '冲突中保留的草稿'
    )
    await secondDetail.getByRole('button', { name: '刷新要求', exact: true }).click()
    await until(
      async () =>
        !(await secondDetail
          .getByRole('button', { name: '纳入任务要求', exact: true })
          .isDisabled()),
      'refresh completed'
    )
    await secondDetail.getByRole('button', { name: '纳入任务要求', exact: true }).click()
    await secondDetail
      .locator('[data-requirement-id]')
      .getByText('冲突中保留的草稿', { exact: true })
      .waitFor()
    await second.close()
    checks.push(
      'two real browser pages use CAS, retain conflicting draft, and allow explicit refresh then save'
    )

    await detail.getByRole('button', { name: '刷新要求', exact: true }).click()
    await until(
      async () =>
        !(await detail.getByRole('button', { name: '添加要求或限制', exact: true }).isDisabled()),
      'fresh before lost response'
    )
    const requestIds = []
    let loseOnce = true
    await page.route(`**${goal(a)}`, async (route) => {
      if (route.request().method() !== 'POST') return route.continue()
      requestIds.push(route.request().postDataJSON().request_id)
      const response = await route.fetch()
      if (loseOnce) {
        loseOnce = false
        return route.abort('failed')
      }
      return route.fulfill({ response })
    })
    await addManual(detail, '丢响应后幂等重试')
    await detail.getByRole('alert').waitFor()
    await detail.getByRole('button', { name: '纳入任务要求', exact: true }).click()
    await detail
      .locator('[data-requirement-id]')
      .getByText('丢响应后幂等重试', { exact: true })
      .waitFor()
    assert.equal(requestIds.length, 2)
    assert.equal(requestIds[0], requestIds[1])
    assert.equal(
      (await snapshot(a)).requirements.filter((r) => r.title === '丢响应后幂等重试').length,
      1
    )
    await page.unroute(`**${goal(a)}`)
    checks.push(
      'server committed but lost response retries the same operation id exactly once without duplicating requirements'
    )

    await detail.getByRole('button', { name: '添加要求或限制', exact: true }).click()
    await detail.getByLabel('要求描述', { exact: true }).fill('本轮禁止命令')
    await detail.getByLabel('验收方式', { exact: true }).selectOption('policy')
    await detail.getByLabel('限制范围', { exact: true }).selectOption('no_commands')
    await detail.getByRole('button', { name: '纳入任务要求', exact: true }).click()
    const policy = detail.locator('[data-requirement-id]').filter({ hasText: '本轮禁止命令' })
    await policy.getByText('持续限制', { exact: true }).waitFor()
    assert.equal(await policy.getByRole('button', { name: '核对完成', exact: true }).count(), 0)
    await policy.getByRole('button', { name: '解除此限制', exact: true }).click()
    await until(async () => (await policy.count()) === 0, 'explicit policy cancellation')
    checks.push(
      'policy remains an active restriction and requires explicit release rather than completion'
    )

    let seen = false,
      release
    const hold = new Promise((resolve) => (release = resolve))
    await page.route(`**${goal(a)}?**`, async (route) => {
      const response = await route.fetch()
      seen = true
      await hold
      await route.fulfill({ response })
    })
    await detail.getByRole('button', { name: '刷新要求', exact: true }).click()
    await until(() => seen, 'held old goal')
    await page.evaluate(async (cid) => {
      await document
        .querySelector('#app')
        .__vue_app__.config.globalProperties.$router.push(`/chat?conv=${cid}`)
    }, b.conversation_id)
    await page.locator('.turn[data-record-id="requirements-qa-b"]').waitFor()
    detail = await open(page)
    release()
    await pause(200)
    assert.equal(await detail.getByText('丢响应后幂等重试', { exact: true }).count(), 0)
    await page.unroute(`**${goal(a)}?**`)
    await page.setViewportSize({ width: 760, height: 1000 })
    const closeSources = page.getByRole('button', { name: '关闭研究资料', exact: true })
    if (await closeSources.isVisible()) await closeSources.click()
    await addManual(detail, '窄窗口键盘输入')
    await detail
      .locator('[data-requirement-id]')
      .getByText('窄窗口键盘输入', { exact: true })
      .waitFor()
    await page.keyboard.press('Tab')
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: path.join(out, 'narrow-requirements.png'), fullPage: true })
    checks.push(
      'late old-goal response cannot overwrite a different conversation; 760px keyboard controls have no page overflow'
    )
    await page.reload()
    detail = await open(page)
    await detail
      .locator('[data-requirement-id]')
      .getByText('窄窗口键盘输入', { exact: true })
      .waitFor()
    checks.push('page reload preserves amended requirement through actual persisted API')
    const archived = await fetch(`${base}/api/conversations/${b.conversation_id}/archive`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archived: true, expected_revision: 0 }),
    })
    assert.equal(archived.status, 200, await archived.clone().text())
    await page.reload()
    detail = await open(page, true)
    await detail
      .getByText('本会话已归档，任务要求仅供查阅；恢复会话后可以修改。', { exact: true })
      .waitFor()
    assert(await detail.getByRole('button', { name: '添加要求或限制', exact: true }).isDisabled())
    assert(await detail.getByRole('button', { name: '修改此要求', exact: true }).isDisabled())
    checks.push(
      'archived conversation retains history and evidence with mutation controls disabled before submission'
    )
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    report = {
      status: 'passed',
      checks,
      provider_requests: 0,
      browser_errors: errors,
      external_requests: external,
      limits: [
        'Synthetic historical runs, no real supplier, no installed desktop or full semantic acceptance claim.',
      ],
    }
  } catch (error) {
    report.error = String(error.stack || error)
    if (page)
      await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    throw error
  } finally {
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
    await fs.writeFile(path.join(out, 'server.log'), log)
    if (browser) await browser.close()
    if (vite) await vite.close()
    if (backend && backend.exitCode === null)
      await new Promise((resolve) => {
        backend.once('exit', resolve)
        backend.kill()
      })
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
