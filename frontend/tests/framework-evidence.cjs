// Real Vue + isolated real API; only the model is synthetic. No external requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((r) => setTimeout(r, ms))
async function freePort() {
  const s = net.createServer()
  await new Promise((r) => s.listen(0, '127.0.0.1', r))
  const p = s.address().port
  await new Promise((r) => s.close(r))
  return p
}
async function main() {
  const out = path.join(root, 'verification/framework-evidence/ui')
  await fs.mkdir(out, { recursive: true })
  const data = await fs.mkdtemp(path.join(out, 'isolated-'))
  const port = await freePort(),
    base = `http://127.0.0.1:${port}`
  const env = {
    ...process.env,
    DATA_DIR: data,
    LLM_API_KEY: '',
    LLM_DEMO_MODE: '1',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  const server = spawn(
    path.join(root, 'backend/.venv/Scripts/python.exe'),
    ['-m', 'tests.framework_ui_server', String(port)],
    { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let log = '',
    vite,
    browser,
    page
  server.stdout.on('data', (b) => (log += b))
  server.stderr.on('data', (b) => (log += b))
  const checks = [],
    errors = [],
    external = []
  let result = { status: 'failed', provider_requests: 0 }
  try {
    const deadline = Date.now() + 45000
    while (true) {
      if (server.exitCode !== null) throw Error(log)
      try {
        if ((await fetch(base + '/api/health')).ok) break
      } catch {}
      assert(Date.now() < deadline, 'startup timeout')
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
    page.on('pageerror', (e) => errors.push(e.message))
    page.setDefaultTimeout(20000)
    await page.goto(ui + '/framework')
    await page.getByLabel('研究想法', { exact: true }).fill('attention synthetic research')
    const generated = page.waitForResponse(
      (r) => r.url().endsWith('/api/writing/framework/runs') && r.request().method() === 'POST'
    )
    await page.getByRole('button', { name: '生成框架', exact: true }).click()
    const response = await generated
    assert.equal(response.status(), 200, await response.text())
    const created = await response.json()
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    const saved = await fetch(
      base +
        `/api/writing/framework/runs/${created.run_id}?conversation_id=${encodeURIComponent(created.conversation_id)}`
    ).then((r) => r.json())
    const value = saved.result
    assert.equal(value.semantic_verification, 'model_assessed')
    assert.equal(value.verified, false)
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    checks.push('real generation displays separate structure, provenance and model support status')
    await page.screenshot({ path: path.join(out, 'framework.png'), fullPage: true })
    const link = page.getByRole('link', { name: '原文 F1E1', exact: true })
    assert((await link.getAttribute('href')).includes('locator='))
    await link.click()
    await page
      .getByText('Attention methods were evaluated on synthetic data.', { exact: true })
      .waitFor()
    checks.push('framework excerpt opens the fixed original at its server locator')
    await page.goto(ui + '/framework')
    await page.locator('.framework-run-state').waitFor()
    await page.getByText('模型复核：证据不足', { exact: false }).waitFor()
    checks.push('window draft survives visiting an original and reloading the writing page')
    await page.getByRole('button', { name: '保存为可编辑成果', exact: true }).click()
    await page.getByRole('link', { name: '编辑已保存成果', exact: true }).click()
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    await page.reload()
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    assert((await page.getByLabel('成果正文', { exact: true }).inputValue()).includes('[F1E1]'))
    await page.locator('a').filter({ hasText: 'Synthetic writing source' }).first().waitFor()
    checks.push('editable artifact keeps source/locator IDs and citation markers across reload')
    await page.setViewportSize({ width: 760, height: 900 })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2))
    checks.push('760px artifact page has no horizontal document overflow')
    await page.goto(ui + '/framework')
    let releaseEnsure,
      ensureSeen = false
    const heldEnsure = new Promise((resolve) => {
      releaseEnsure = resolve
    })
    await page.route('**/api/sources/ensure-conversation', async (route) => {
      ensureSeen = true
      await heldEnsure
      await route.continue()
    })
    const writingPosts = []
    page.on('request', (r) => {
      if (r.url().endsWith('/api/writing/framework/runs') && r.method() === 'POST')
        writingPosts.push(r.postDataJSON())
    })
    await page.getByLabel('研究想法', { exact: true }).fill('Original conversation A idea')
    await page.getByRole('button', { name: '生成框架', exact: true }).click()
    const raceDeadline = Date.now() + 5000
    while (!ensureSeen) {
      assert(Date.now() < raceDeadline)
      await pause(20)
    }
    const previousCid = await page.evaluate(async () => {
      const m = await import('/src/desktopState.ts')
      const old = m.activeConversationId.value
      m.activeConversationId.value = 'framework-race-b'
      return old
    })
    await page.getByLabel('研究想法', { exact: true }).fill('Different conversation B idea')
    releaseEnsure()
    const submittedDeadline = Date.now() + 5000
    while (!writingPosts.length) {
      assert(Date.now() < submittedDeadline)
      await pause(20)
    }
    assert.equal(writingPosts.length, 1)
    assert.equal(writingPosts[0].conversation_id, previousCid)
    assert.equal(writingPosts[0].idea, 'Original conversation A idea')
    assert.equal(
      await page.getByLabel('研究想法', { exact: true }).inputValue(),
      'Different conversation B idea'
    )
    assert.equal(await page.locator('.framework-run-state').count(), 0)
    await page.unroute('**/api/sources/ensure-conversation')
    await page.evaluate(async (cid) => {
      const m = await import('/src/desktopState.ts')
      m.activeConversationId.value = cid
    }, previousCid)
    assert.equal(
      await page.getByLabel('研究想法', { exact: true }).inputValue(),
      'Original conversation A idea'
    )
    await page.evaluate(async () => {
      const m = await import('/src/desktopState.ts')
      m.activeConversationId.value = 'framework-race-b'
    })
    assert.equal(
      await page.getByLabel('研究想法', { exact: true }).inputValue(),
      'Different conversation B idea'
    )
    checks.push(
      'delayed ensure dispatches only the originally authorized A input and preserves both conversation drafts'
    )
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    result = {
      status: 'passed',
      checks,
      provider_requests: 0,
      browser_errors: errors,
      external_requests: external,
    }
  } catch (e) {
    result.error = String(e.stack || e)
    if (page)
      await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    throw e
  } finally {
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(result, null, 2))
    await fs.writeFile(path.join(out, 'server.log'), log)
    if (browser) await browser.close()
    if (vite) await vite.close()
    server.kill()
  }
}
main().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
