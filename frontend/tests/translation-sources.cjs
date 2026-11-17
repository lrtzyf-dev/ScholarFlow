// Real Vue/HTTP, synthetic translation model; no network or user files.
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
  const result = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return result
}
async function until(fn, message) {
  const deadline = Date.now() + 45000
  while (!(await fn())) {
    assert(Date.now() < deadline, message)
    await pause(50)
  }
}
async function main() {
  const out = path.join(root, 'verification/translation-sources/ui')
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
  const start = async () => {
    backend = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'tests.translation_ui_server', String(backendPort)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
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
    }, 'backend start')
  }
  const stop = async () => {
    if (backend && backend.exitCode === null)
      await new Promise((resolve) => {
        backend.once('exit', resolve)
        backend.kill()
      })
    backend = null
  }
  try {
    await start()
    const api = async (url, body) => {
      const response = await fetch(base + '/api' + url, {
        method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`)
      return response.json()
    }
    const cid = (await api('/conversations', { title: 'Fixed translation' })).id
    const upload = async (text, request, previous) => {
      const form = new FormData()
      form.set('file', new Blob([text]), '原文.md')
      form.set('conversation_id', cid)
      form.set('request_id', request)
      if (previous) {
        form.set('source_id', previous.source_id)
        form.set('expected_version', previous.version)
      }
      const response = await fetch(base + '/api/sources/upload', { method: 'POST', body: form })
      assert.equal(response.status, 200, await response.clone().text())
      return response.json()
    }
    const old = await upload('Old immutable passage.\nOther line.', 'translation-ui-old')
    const locator = await api(`/sources/revisions/${old.revision_id}/locators`, {
      conversation_id: cid,
      kind: 'text_lines',
      line_start: 1,
      line_end: 1,
    })
    const newer = await upload('New immutable passage.\nOther line.', 'translation-ui-new', old)
    const locatorB = await api(`/sources/revisions/${newer.revision_id}/locators`, {
      conversation_id: cid,
      kind: 'text_lines',
      line_start: 1,
      line_end: 1,
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
    const context = await browser.newContext({ viewport: { width: 1365, height: 950 } })
    await context.route('**/*', (route) => {
      const u = new URL(route.request().url())
      if (u.origin === ui || ['blob:', 'data:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(
      `${ui}/sources/${old.revision_id}?conversation=${cid}&locator=${locator.locator_id}`
    )
    const toTranslate = page.getByRole('link', { name: '翻译此固定选段', exact: true })
    await toTranslate.click()
    const source = page.getByLabel('中文原文', { exact: true })
    await until(async () => (await source.inputValue()) === locator.quote, 'load fixed quotation')
    assert((await source.getAttribute('readonly')) !== null)
    await page.getByRole('link', { name: '原文：原文.md · 版本 1', exact: true }).waitFor()
    await page.getByRole('button', { name: '翻译为英文', exact: true }).click()
    await page
      .locator('.result-text')
      .filter({ hasText: 'Synthetic translation: Old immutable passage.' })
      .waitFor()
    await page
      .getByText('原文来源已固定，科学结论支持程度未核验；模型翻译仍需人工校核。', { exact: true })
      .waitFor()
    checks.push(
      'reader selection binds immutable old version; actual translation retains locator and honest support state'
    )
    await page.screenshot({ path: path.join(out, 'fixed-translation.png'), fullPage: true })
    await page.getByRole('button', { name: '保存为可编辑成果', exact: true }).click()
    await page.getByRole('link', { name: '编辑已保存成果', exact: true }).click()
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    const artifactUrl = page.url()
    assert((await page.getByLabel('成果正文', { exact: true }).inputValue()).includes('[T1]'))
    await stop()
    await start()
    await page.reload()
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    await page.locator('a').filter({ hasText: '原文.md' }).first().waitFor()
    checks.push(
      'editable translation artifact preserves fixed evidence across actual backend restart'
    )
    const target = (revision, location) =>
      `/translate?conversation=${cid}&revision=${revision}&locator=${location}`
    await page.goto(ui + target(old.revision_id, locator.locator_id))
    await until(async () => (await source.inputValue()) === locator.quote, 'old source restored')
    await page.getByRole('button', { name: '转为自由文本', exact: true }).click()
    await until(async () => (await source.getAttribute('readonly')) === null, 'explicit detachment')
    assert.equal(await source.inputValue(), locator.quote)
    await source.fill('Free edited source')
    await page.getByRole('button', { name: '翻译为英文', exact: true }).click()
    await page
      .locator('.result-text')
      .filter({ hasText: 'Synthetic translation: Free edited source' })
      .waitFor()
    await page.getByRole('button', { name: '保存为可编辑成果', exact: true }).click()
    await page.getByRole('link', { name: '编辑已保存成果', exact: true }).click()
    assert(
      (await page.getByLabel('成果正文', { exact: true }).inputValue()).includes('原文为用户输入')
    )
    checks.push(
      'explicit free-text detachment permits edits without retaining false fixed-source attribution'
    )
    await page.goto(ui + '/translate')
    let release,
      seen = false
    const hold = new Promise((resolve) => (release = resolve))
    await page.route('**/api/translation/runs', async (route) => {
      if (route.request().method() !== 'POST' || route.request().postDataJSON().text !== 'Source A')
        return route.continue()
      const response = await route.fetch()
      seen = true
      await hold
      await route.fulfill({ response })
    })
    await source.fill('Source A')
    await page.getByRole('button', { name: '翻译为英文', exact: true }).click()
    await until(() => seen, 'held translation')
    const otherCid = (await api('/conversations', { title: 'Other translation' })).id
    await page.evaluate(async (url) => {
      await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push(url)
    }, `/translate?conversation=${otherCid}`)
    await source.fill('Source B')
    await page.getByRole('button', { name: '对照翻译', exact: true }).click()
    await page.getByRole('button', { name: '生成对照', exact: true }).click()
    await page.locator('.pane').filter({ hasText: 'Synthetic translation: Source B' }).waitFor()
    release()
    await pause(300)
    assert.equal(await page.locator('.result-text').count(), 0)
    assert.equal(
      await page.getByRole('button', { name: '生成对照', exact: true }).isDisabled(),
      false
    )
    await page.unroute('**/api/translation/runs')
    checks.push(
      'late whole-text create response cannot overwrite another conversation input or its newer parallel operation'
    )
    await page.goto(ui + '/translate')
    await source.waitFor()
    let releaseSource,
      readSeen = false
    const sourceHold = new Promise((resolve) => (releaseSource = resolve))
    await page.route(
      `**/api/sources/revisions/${old.revision_id}/locators/${locator.locator_id}?**`,
      async (route) => {
        const response = await route.fetch()
        readSeen = true
        await sourceHold
        await route.fulfill({ response })
      }
    )
    // Use the mounted app's router; importing the source module independently
    // can create a second singleton when Vite stamped its original URL.
    const navigate = async (url) =>
      page.evaluate(async (value) => {
        await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push(value)
      }, url)
    await navigate(target(old.revision_id, locator.locator_id))
    await until(() => readSeen, 'held source A')
    await navigate(target(newer.revision_id, locatorB.locator_id))
    await until(async () => (await source.inputValue()) === locatorB.quote, 'source B loaded')
    releaseSource()
    await pause(300)
    assert.equal(await source.inputValue(), locatorB.quote)
    assert.equal(
      await page.getByRole('button', { name: '翻译为英文', exact: true }).isDisabled(),
      false
    )
    await page.unroute(
      `**/api/sources/revisions/${old.revision_id}/locators/${locator.locator_id}?**`
    )
    checks.push(
      'same mounted view ignores source A arriving after source B, preserving B loading and provenance'
    )
    await page.setViewportSize({ width: 760, height: 950 })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: path.join(out, 'narrow-translation.png'), fullPage: true })
    checks.push('760px fixed-source controls have no document horizontal overflow')
    await page.goto(artifactUrl)
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    report = {
      status: 'passed',
      checks,
      provider_requests: 0,
      browser_errors: errors,
      external_requests: external,
      limits: [
        'Synthetic model; formal UI uses durable runs, legacy synchronous endpoints remain deprecated. No installed/native or natural-model quality acceptance.',
      ],
    }
  } catch (e) {
    report.error = String(e.stack || e)
    if (page)
      await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    throw e
  } finally {
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
    await fs.writeFile(path.join(out, 'server.log'), log)
    if (browser) await browser.close()
    if (vite) await vite.close()
    await stop()
  }
}
main().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
