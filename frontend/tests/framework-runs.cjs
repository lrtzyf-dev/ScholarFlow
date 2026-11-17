// Real Vue, actual backend processes/restarts; synthetic metered provider only.
const assert = require('node:assert/strict'),
  fs = require('node:fs/promises'),
  path = require('node:path'),
  net = require('node:net')
const { spawn } = require('node:child_process'),
  { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'),
  pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, label) {
  const end = Date.now() + 45000
  while (!(await fn())) {
    assert.ok(Date.now() < end, label)
    await pause(80)
  }
}
async function port() {
  const server = net.createServer()
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const p = server.address().port
  await new Promise((r) => server.close(r))
  return p
}
async function main() {
  const out = path.join(root, 'verification/framework-runs/ui')
  await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-')),
    control = path.join(isolated, 'control')
  await fs.mkdir(control)
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    DB_NAME: 'framework-runs.db',
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    FRAMEWORK_RUN_CONTROL: control,
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
  const errors = [],
    external = [],
    checks = []
  const receipts = async () => {
    try {
      return (await fs.readFile(path.join(control, 'synthetic-calls.jsonl'), 'utf8'))
        .trim()
        .split('\n')
        .filter(Boolean)
        .map(JSON.parse)
    } catch {
      return []
    }
  }
  const release = (phase) =>
    fs.writeFile(path.join(control, 'release-' + phase), 'synthetic release')
  const hold = (phase) =>
    fs.unlink(path.join(control, 'release-' + phase)).catch((e) => {
      if (e.code !== 'ENOENT') throw e
    })
  try {
    const p = await port(),
      base = `http://127.0.0.1:${p}`
    const start = async () => {
      backend = spawn(
        path.join(root, 'backend/.venv/Scripts/python.exe'),
        ['-m', 'tests.framework_run_ui_server', String(p)],
        {
          cwd: path.join(root, 'backend'),
          env,
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'pipe'],
        }
      )
      backend.stdout.on('data', (v) => (log += v))
      backend.stderr.on('data', (v) => (log += v))
      await until(async () => {
        if (backend.exitCode !== null) throw Error(log)
        try {
          return (await fetch(base + '/api/health')).ok
        } catch {
          return false
        }
      }, 'backend startup')
    }
    const stop = async () => {
      const proc = backend
      if (!proc || proc.exitCode !== null) return
      await new Promise((resolve) => {
        proc.once('exit', resolve)
        proc.kill()
      })
      backend = undefined
    }
    await start()
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
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } },
      },
    })
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(ui + '/framework')
    await page.getByLabel('研究想法', { exact: true }).fill('Persistent first attention framework')
    await page.getByRole('button', { name: '生成框架', exact: true }).click()
    await until(async () => (await receipts()).length === 1, 'first generation running')
    await page.goto(ui + '/library')
    await page.goto(ui + '/framework')
    await page.getByText('Framework-Bound-Case', { exact: false }).waitFor()
    assert.equal(
      await page.getByLabel('研究想法', { exact: true }).inputValue(),
      'Persistent first attention framework'
    )
    assert.equal((await receipts()).length, 1)
    checks.push({
      name: 'leaving_and_returning_keeps_same_background_run_and_bound_input_model',
      passed: true,
    })
    await release('generation')
    await until(async () => (await receipts()).length === 2, 'support review running')
    await page.reload()
    await page.locator('.framework-run-state strong').filter({ hasText: '复核论点' }).waitFor()
    await release('review')
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    await page.screenshot({ path: path.join(out, 'persistent-framework.png'), fullPage: true })
    checks.push({ name: 'reload_during_review_reattaches_and_delivers_saved_result', passed: true })
    await stop()
    await start()
    await page.reload()
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 2)
    checks.push({
      name: 'actual_backend_restart_restores_completed_result_without_model_replay',
      passed: true,
    })
    await hold('generation')
    await hold('review')
    await page
      .getByLabel('研究想法', { exact: true })
      .fill('Interrupted second attention framework')
    await page.getByRole('button', { name: '生成框架', exact: true }).click()
    await until(async () => (await receipts()).length === 3, 'second unknown generation running')
    await stop()
    await start()
    await page.reload()
    await page
      .getByRole('checkbox', { name: '我已核对上次调用，确认重试未确认调用可能再次产生费用' })
      .waitFor()
    await pause(450)
    assert.equal((await receipts()).length, 3, 'Restart must not blindly replay unknown request')
    const retry = page.getByRole('button', { name: '重试未确认调用', exact: true })
    assert.equal(await retry.isDisabled(), true)
    await page
      .getByRole('checkbox', { name: '我已核对上次调用，确认重试未确认调用可能再次产生费用' })
      .check()
    await release('generation')
    await retry.click()
    await until(
      async () => (await receipts()).length === 5,
      'explicit retry generation and support request'
    )
    await release('review')
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    checks.push({
      name: 'killed_unknown_request_requires_explicit_confirmation_then_resumes_same_task',
      passed: true,
    })
    await hold('generation')
    await hold('review')
    await page.getByLabel('研究想法', { exact: true }).fill('Cancelled third attention framework')
    await page.getByRole('button', { name: '生成框架', exact: true }).click()
    await until(async () => (await receipts()).length === 6, 'third generation running')
    await page.getByRole('button', { name: '停止此任务', exact: true }).click()
    await release('generation')
    await page.getByRole('button', { name: '继续未完成任务', exact: true }).waitFor()
    assert.equal((await receipts()).length, 6)
    await page.getByRole('button', { name: '继续未完成任务', exact: true }).click()
    await until(async () => (await receipts()).length === 7, 'cancel resume only support review')
    await release('review')
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    checks.push({
      name: 'cancel_after_paid_generation_preserves_draft_and_resume_does_not_regenerate',
      passed: true,
    })
    await page.goto(ui + '/tasks')
    const center = page.getByRole('region', { name: '统一运行与待办' })
    await center.getByLabel('视图', { exact: false }).selectOption('all')
    const taskItem = center.locator('li').filter({ hasText: 'Cancelled third attention framework' })
    await taskItem.getByRole('button', { name: '查看并处理', exact: true }).click()
    await page.waitForURL(/\/framework\?run=/)
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    assert.equal(
      await page.getByLabel('研究想法', { exact: true }).inputValue(),
      'Cancelled third attention framework'
    )
    assert.equal((await receipts()).length, 7)
    checks.push({
      name: 'unified_run_center_opens_saved_framework_without_model_replay',
      passed: true,
    })
    await page
      .getByLabel('研究想法', { exact: true })
      .fill('Unsubmitted next draft survives reload')
    await page.goto(ui + '/framework')
    await page.locator('.framework-run-state').waitFor()
    assert.equal(
      await page.getByLabel('研究想法', { exact: true }).inputValue(),
      'Unsubmitted next draft survives reload'
    )
    let rejected = false
    await page.route('**/api/writing/framework/runs/*?conversation_id=*', (route) => {
      if (!rejected && route.request().method() === 'GET') {
        rejected = true
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Synthetic observation interruption' }),
        })
      }
      return route.continue()
    })
    await page.reload()
    await page.getByRole('button', { name: '重新载入任务状态', exact: true }).click()
    await page.locator('.framework-run-state').waitFor()
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    assert.equal(await page.getByRole('alert').count(), 0)
    assert.equal((await receipts()).length, 7)
    checks.push({
      name: 'unsubmitted_draft_preserved_and_failed_observation_reloaded_without_new_calls',
      passed: true,
    })
    await page.setViewportSize({ width: 760, height: 950 })
    await page.screenshot({ path: path.join(out, 'narrow-framework-run.png'), fullPage: true })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    checks.push({
      name: '760px_persistent_run_and_result_controls_no_document_overflow',
      passed: true,
    })
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    await fs.writeFile(
      path.join(out, 'report.json'),
      JSON.stringify(
        {
          passed: true,
          checks,
          browser_errors: errors,
          external_requests: external,
          real_provider_calls: 0,
          synthetic_metered_chat_calls: (await receipts()).length,
          scope:
            'Real Vue and actual process interruption/restarts with fixed generated text and MockTransport; not real model quality or installed Windows acceptance.',
        },
        null,
        2
      )
    )
  } catch (error) {
    await page?.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(
      path.join(out, 'failure.json'),
      JSON.stringify(
        { error: String(error), log, errors, external, receipts: await receipts() },
        null,
        2
      )
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
