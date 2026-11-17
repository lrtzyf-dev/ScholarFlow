// Actual Vue, HTTP, process cancellation/restarts; synthetic metered transport.
const assert = require('node:assert/strict'),
  fs = require('node:fs/promises'),
  path = require('node:path'),
  net = require('node:net')
const { spawn } = require('node:child_process'),
  { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'),
  pause = (ms) => new Promise((r) => setTimeout(r, ms))
async function port() {
  const s = net.createServer()
  await new Promise((r) => s.listen(0, '127.0.0.1', r))
  const p = s.address().port
  await new Promise((r) => s.close(r))
  return p
}
async function until(fn, label) {
  const deadline = Date.now() + 45000
  while (!(await fn())) {
    assert(Date.now() < deadline, label)
    await pause(70)
  }
}
async function main() {
  const out = path.join(root, 'verification/translation-runs/ui')
  await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-')),
    control = path.join(isolated, 'control')
  await fs.mkdir(control)
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    DB_NAME: 'translation-runs.db',
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    TRANSLATION_RUN_CONTROL: control,
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    LLM_DEMO_MODE: '1',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
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
  const release = (n) => fs.writeFile(path.join(control, 'release-' + n), 'synthetic release')
  const p = await port(),
    base = `http://127.0.0.1:${p}`
  let backend,
    vite,
    browser,
    page,
    log = ''
  const checks = [],
    errors = [],
    external = []
  const start = async () => {
    backend = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'tests.translation_run_ui_server', String(p)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
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
    }, 'server startup')
  }
  const stop = async () => {
    const proc = backend
    if (proc && proc.exitCode === null)
      await new Promise((r) => {
        proc.once('exit', r)
        proc.kill()
      })
    backend = null
  }
  const api = async (url, body) => {
    const r = await fetch(base + '/api' + url, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    assert.equal(r.status, 200, await r.clone().text())
    return r.json()
  }
  try {
    await start()
    const cid = (await api('/conversations', { title: 'Durable translation source' })).id
    const upload = async (text, request, previous) => {
      const form = new FormData()
      form.set('file', new Blob([text]), 'fixed.md')
      form.set('conversation_id', cid)
      form.set('request_id', request)
      if (previous) {
        form.set('source_id', previous.source_id)
        form.set('expected_version', previous.version)
      }
      const r = await fetch(base + '/api/sources/upload', { method: 'POST', body: form })
      assert.equal(r.status, 200, await r.clone().text())
      return r.json()
    }
    const original = await upload(
      'First synthetic paragraph.\n\nSecond synthetic paragraph.',
      'translation-run-source'
    )
    const loc = await api(`/sources/revisions/${original.revision_id}/locators`, {
      conversation_id: cid,
      kind: 'text_lines',
      line_start: 1,
      line_end: 3,
    })
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
      const u = new URL(route.request().url())
      if (u.origin === ui || ['data:', 'blob:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(
      `${ui}/translate?conversation=${cid}&revision=${original.revision_id}&locator=${loc.locator_id}`
    )
    await until(
      async () => (await page.getByLabel('中文原文', { exact: true }).inputValue()) === loc.quote,
      'fixed source loaded'
    )
    await page.getByRole('button', { name: '对照翻译', exact: true }).click()
    const created = page.waitForResponse(
      (r) => r.url().endsWith('/api/translation/runs') && r.request().method() === 'POST'
    )
    await page.getByRole('button', { name: '生成对照', exact: true }).click()
    const first = await (await created).json()
    await until(async () => (await receipts()).length === 1, 'first translation unit active')
    await upload(
      'Newer source should not replace the frozen input.',
      'translation-run-new',
      original
    )
    const savedUrl = `${ui}/translate?conversation=${cid}&run=${first.run_id}`
    await page.goto(ui + '/library')
    await page.goto(savedUrl)
    await page.getByText('Translation-UI-Bound-Case', { exact: false }).waitFor()
    assert.equal(await page.getByLabel('中文原文', { exact: true }).inputValue(), loc.quote)
    assert.equal((await receipts()).length, 1)
    checks.push({
      name: 'leaving_and_returning_reattaches_fixed_source_and_model_without_replay',
      passed: true,
    })
    await release(1)
    await until(async () => (await receipts()).length === 2, 'second unit active')
    await page.getByText('已保存 1/2 段', { exact: false }).waitFor()
    await page.reload()
    await page
      .locator('.pane')
      .filter({ hasText: 'Synthetic translation: First synthetic paragraph.' })
      .waitFor()
    checks.push({
      name: 'completed_units_remain_visible_during_reload_and_next_unit',
      passed: true,
    })
    await stop()
    await start()
    await page.reload()
    const confirm = page.getByRole('checkbox', {
      name: '我已核对上次调用，确认重试未确认调用可能再次产生费用',
    })
    await confirm.waitFor()
    await pause(350)
    assert.equal((await receipts()).length, 2)
    const retry = page.getByRole('button', { name: '重试未确认调用', exact: true })
    assert(await retry.isDisabled())
    await release(3)
    await confirm.check()
    await retry.click()
    await page.getByText('已保存 2/2 段', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 3)
    assert(
      (await page.locator('.pane').last().innerText()).includes(
        'Synthetic translation: First synthetic paragraph.'
      )
    )
    const persisted = await api(`/translation/runs/${first.run_id}?conversation_id=${cid}`)
    assert.equal(persisted.result.source_evidence[0].revision_id, original.revision_id)
    assert.equal(persisted.result.semantic_verification, 'not_assessed')
    checks.push({
      name: 'actual_unknown_request_restart_requires_ack_then_only_retries_unfinished_unit',
      passed: true,
    })
    await page.getByRole('button', { name: '保存为可编辑成果', exact: true }).click()
    await page.getByRole('link', { name: '编辑已保存成果', exact: true }).click()
    assert((await page.getByLabel('成果正文', { exact: true }).inputValue()).includes('[T1]'))
    checks.push({
      name: 'durable_translation_delivery_preserves_fixed_T1_source_and_honest_semantics',
      passed: true,
    })
    await page.goto(savedUrl)
    await page.getByRole('button', { name: '转为自由文本', exact: true }).click()
    await page
      .getByLabel('中文原文', { exact: true })
      .fill('Cancelled first unit.\n\nCancelled second unit.')
    await page.getByRole('button', { name: '生成对照', exact: true }).click()
    await until(async () => (await receipts()).length === 4, 'cancellable unit active')
    await page.getByRole('button', { name: '停止此翻译', exact: true }).click()
    await release(4)
    await page.getByRole('button', { name: '继续未完成翻译', exact: true }).waitFor()
    assert.equal((await receipts()).length, 4)
    await release(5)
    await page.getByRole('button', { name: '继续未完成翻译', exact: true }).click()
    await page.getByText('已保存 2/2 段', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 5)
    checks.push({
      name: 'cancelled_known_response_is_saved_and_resume_only_completes_next_unit',
      passed: true,
    })
    await stop()
    await start()
    await page.reload()
    await page.getByText('已保存 2/2 段', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 5)
    checks.push({
      name: 'completed_translation_survives_actual_process_restart_without_replay',
      passed: true,
    })
    const resumedRunId = new URL(page.url()).searchParams.get('run')
    assert(resumedRunId && resumedRunId !== first.run_id)
    await page.goto(ui + '/tasks')
    const center = page.getByRole('region', { name: '统一运行与待办' })
    await center.getByLabel('视图', { exact: false }).selectOption('all')
    await center
      .locator(`li[data-run-center-id$="${resumedRunId}"]`)
      .getByRole('button', { name: '查看并处理', exact: true })
      .click()
    await page.waitForURL(/\/translate\?.*run=/)
    await page.getByText('已保存 2/2 段', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 5)
    checks.push({ name: 'unified_run_center_opens_existing_translation_result', passed: true })
    let rejected = false
    await page.route('**/api/translation/runs/*?conversation_id=*', (route) => {
      if (!rejected && route.request().method() === 'GET') {
        rejected = true
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Synthetic initial observation failure' }),
        })
      }
      return route.continue()
    })
    await page.reload()
    await page.getByRole('button', { name: '重新载入翻译状态', exact: true }).click()
    await page.getByText('已保存 2/2 段', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 5)
    checks.push({
      name: 'failed_initial_observation_refreshes_the_requested_run_without_replay',
      passed: true,
    })
    await page.getByRole('button', { name: '整篇翻译', exact: true }).click()
    await page.getByLabel('中文原文', { exact: true }).fill('Lost create response fixed input.')
    let lostBody
    await page.route('**/api/translation/runs', async (route) => {
      if (route.request().method() === 'POST' && !lostBody) {
        lostBody = route.request().postDataJSON()
        await route.fetch()
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Synthetic lost response after admission' }),
        })
      }
      return route.continue()
    })
    await page.getByRole('button', { name: '翻译为英文', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: 'Synthetic lost response' }).waitFor()
    await until(
      async () => (await receipts()).length === 6,
      'accepted request despite lost response'
    )
    const replay = page.waitForResponse(
      (r) => r.url().endsWith('/api/translation/runs') && r.request().method() === 'POST'
    )
    await page.getByRole('button', { name: '翻译为英文', exact: true }).click()
    const replayed = await (await replay).json()
    assert.equal(replayed.run_id, lostBody.run_id)
    assert.equal(replayed.replayed, true)
    await release(6)
    await page.getByText('已保存 1/1 段', { exact: false }).waitFor()
    await page.waitForURL(new RegExp(lostBody.run_id))
    await page.reload()
    await page.getByText('已保存 1/1 段', { exact: false }).waitFor()
    assert.equal((await receipts()).length, 6)
    checks.push({
      name: 'lost_create_response_reuses_request_identity_and_reload_opens_the_newest_run',
      passed: true,
    })
    await page.goto(savedUrl)
    await page.getByText('已保存 2/2 段', { exact: false }).waitFor()
    await page.setViewportSize({ width: 760, height: 950 })
    await page.screenshot({ path: path.join(out, 'narrow-translation-run.png'), fullPage: true })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2))
    checks.push({
      name: '760px_translation_status_and_parallel_result_no_horizontal_document_overflow',
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
            'Actual Vue/HTTP and process interruptions with synthetic MockTransport; not natural translation quality or installed/native release acceptance.',
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
