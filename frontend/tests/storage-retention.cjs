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
async function main() {
  const out = path.join(root, 'verification/storage-retention/ui')
  await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-'))
  const base = `http://127.0.0.1:${await port()}`,
    env = {
      ...process.env,
      DATA_DIR: isolated,
      LLM_API_KEY: '',
      LLM_DEMO_MODE: '1',
      EMBEDDING_API_KEY: '',
      REDIS_URL: '',
      DESKTOP_NOTIFICATIONS: 'false',
      PYTHONUTF8: '1',
    }
  const backend = spawn(
    path.join(root, 'backend/.venv/Scripts/python.exe'),
    ['-m', 'tests.storage_ui_server', base.split(':').at(-1)],
    { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let log = '',
    vite,
    browser,
    page,
    result = { status: 'failed', provider_requests: 0 }
  const checks = [],
    errors = [],
    external = []
  backend.stdout.on('data', (b) => (log += b))
  backend.stderr.on('data', (b) => (log += b))
  try {
    const deadline = Date.now() + 45000
    while (true) {
      if (backend.exitCode !== null) throw Error(log)
      try {
        if ((await fetch(base + '/api/health')).ok) break
      } catch {}
      assert(Date.now() < deadline)
      await pause(100)
    }
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
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } })
    await context.route('**/*', (route) => {
      const u = new URL(route.request().url())
      if (u.origin === ui || ['data:', 'blob:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(ui + '/tasks')
    const maintenance = page.getByRole('region', { name: '数据维护提醒' })
    await maintenance.getByText('已到备份周期或尚无成功备份记录', { exact: true }).waitFor()
    await maintenance
      .getByRole('link', { name: '查看备份、保留策略与清理回执', exact: true })
      .click()
    await page.waitForURL('**/settings?tab=data')
    checks.push('run center shows actual maintenance reminders and links directly to data settings')
    await page.getByRole('heading', { name: '数据保留与空间', exact: true }).waitFor()
    const card = page.locator('.storage-settings')
    await card.getByLabel('归档会话审阅周期（天）').fill('30')
    await card.getByLabel('容量提醒阈值（MB）').fill('1')
    await card.getByRole('button', { name: '保存保留策略', exact: true }).click()
    await card
      .getByText('保留周期和容量提醒已保存。到期资料须逐项审阅后删除。', { exact: true })
      .waitFor()
    await page.reload()
    await page.getByRole('button', { name: '数据与存储', exact: true }).click()
    await card.getByLabel('归档会话审阅周期（天）').waitFor()
    assert.equal(await card.getByLabel('归档会话审阅周期（天）').inputValue(), '30')
    await card
      .getByText('已达到设定的容量提醒阈值，请审阅归档资料和备份副本。', { exact: true })
      .waitFor()
    checks.push('real Vue policy persists across reload and capacity reminder uses actual files')
    await card.locator('summary').filter({ hasText: '到期归档会话' }).click()
    const cloud = card
      .locator('li')
      .filter({ has: page.getByRole('link', { name: 'Synthetic cloud review', exact: true }) })
    await cloud.getByRole('button', { name: '预览清理', exact: true }).click()
    await card.getByRole('region', { name: '会话清理预览' }).waitFor()
    assert(
      await card.getByRole('button', { name: '确认删除此会话及关联资料', exact: true }).isDisabled()
    )
    checks.push('unknown cloud state blocks deletion in the actual inline review')
    await card.getByRole('button', { name: '取消', exact: true }).click()
    const old = card
      .locator('li')
      .filter({ has: page.getByRole('link', { name: 'Synthetic archived review', exact: true }) })
    await old.getByRole('button', { name: '预览清理', exact: true }).click()
    await card.getByRole('region', { name: '会话清理预览' }).waitFor()
    await card.getByRole('button', { name: '确认删除此会话及关联资料', exact: true }).click()
    await card.getByText('会话资料已逻辑删除，清理回执', { exact: false }).waitFor()
    await card.locator('summary').filter({ hasText: '清理回执（已载入' }).click()
    await card
      .getByText('不保存原文、标题、密钥或文件路径。逻辑删除不证明远端文件或历史备份已擦除。', {
        exact: false,
      })
      .waitFor()
    const receipts = await (await fetch(base + '/api/storage/cleanup-receipts')).json()
    assert.equal(receipts.items.length, 2)
    assert.equal(receipts.items.filter((item) => item.review_due).length, 1)
    await card
      .getByText('已有 1 条回执达到审阅周期，列表中标为“待审阅”。', { exact: true })
      .waitFor()
    await card.locator('summary').filter({ hasText: '待审阅' }).waitFor()
    assert(!JSON.stringify(receipts).includes('SYNTHETIC_PRIVATE'))
    assert.equal(receipts.items[0].document.remote_erasure_verified, false)
    const conversations = await (await fetch(base + '/api/conversations?limit=100')).json()
    assert(JSON.stringify(conversations).includes('storage-ui-active'))
    checks.push('inline reviewed cleanup keeps the active chat and saves a content-free receipt')
    checks.push('receipt review period displays due count and individual review marker')
    await page.screenshot({ path: path.join(out, 'storage-settings.png'), fullPage: true })
    await page.setViewportSize({ width: 760, height: 900 })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2))
    checks.push('760px retention form has no horizontal document overflow')
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
    backend.kill()
  }
}
main().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
