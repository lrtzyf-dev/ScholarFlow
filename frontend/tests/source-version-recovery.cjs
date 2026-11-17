// Actual Vue + isolated SQLite; two process/origin lifetimes, no provider calls.
const assert = require('node:assert/strict'),
  fs = require('node:fs/promises'),
  path = require('node:path'),
  net = require('node:net')
const { spawn } = require('node:child_process'),
  { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'),
  pause = (ms) => new Promise((r) => setTimeout(r, ms))
const ownershipHarness = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>
<body><div id="app"></div><script type="module">
import { createApp, h, ref, nextTick } from '/node_modules/.vite/deps/vue.js';
import { createRouter, createMemoryHistory } from '/node_modules/.vite/deps/vue-router.js';
import SourceVersions from '/src/components/SourceVersions.vue';
const target = ref(null);
const router = createRouter({ history: createMemoryHistory(), routes: [{path:'/',component:{render:()=>null}}] });
createApp({render:()=>target.value ? h(SourceVersions,{record:target.value.record,conversationId:target.value.cid}) : null}).use(router).mount('#app');
window.setSourceTarget=async(value)=>{target.value=value;await nextTick()};
</script></body></html>`
async function port() {
  const s = net.createServer()
  await new Promise((r) => s.listen(0, '127.0.0.1', r))
  const p = s.address().port
  await new Promise((r) => s.close(r))
  return p
}
async function until(check, message) {
  const end = Date.now() + 45000
  while (!(await check())) {
    assert(Date.now() < end, message)
    await pause(100)
  }
}
async function main() {
  const out = path.join(root, 'verification/source-versions/recovery-ui')
  await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-'))
  let backend,
    vite,
    browser,
    page,
    base,
    ui,
    log = ''
  const errors = [],
    external = [],
    checks = []
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    LLM_API_KEY: '',
    LLM_DEMO_MODE: '1',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  async function start() {
    base = `http://127.0.0.1:${await port()}`
    backend = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', base.split(':').at(-1)],
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
    }, 'backend ready')
    const { createServer } = await import('vite')
    vite = await createServer({
      root: path.join(root, 'frontend'),
      configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error',
      plugins: [
        {
          name: 'source-version-ownership-acceptance',
          configureServer(server) {
            server.middlewares.use('/__source-version-ownership', async (_req, res, next) => {
              try {
                res.setHeader('Content-Type', 'text/html; charset=utf-8')
                res.end(
                  await server.transformIndexHtml('/__source-version-ownership', ownershipHarness)
                )
              } catch (error) {
                next(error)
              }
            })
          },
        },
      ],
      server: {
        host: '127.0.0.1',
        port: await port(),
        strictPort: true,
        hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } },
      },
    })
    await vite.listen()
    ui = `http://127.0.0.1:${vite.httpServer.address().port}`
  }
  async function stop() {
    await vite?.close()
    vite = null
    if (backend) {
      const p = backend
      backend = null
      p.kill()
      if (p.exitCode === null) await new Promise((r) => p.once('exit', r))
    }
  }
  async function api(url, body) {
    const r = await fetch(
      base + '/api' + url,
      body
        ? {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          }
        : undefined
    )
    assert(r.ok, await r.clone().text())
    return r.json()
  }
  async function upload(cid, text, previous) {
    const f = new FormData()
    f.append('conversation_id', cid)
    f.append('request_id', `fixture-${crypto.randomUUID()}`)
    f.append('file', new Blob([text]), 'paper.md')
    if (previous) {
      f.append('source_id', previous.source_id)
      f.append('expected_version', previous.version)
    }
    const r = await fetch(base + '/api/sources/upload', { method: 'POST', body: f })
    assert(r.ok, await r.clone().text())
    return r.json()
  }
  async function newPage() {
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } })
    await context.route('**/*', (route) => {
      const u = new URL(route.request().url())
      if (u.origin === ui || ['data:', 'blob:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname)
      return route.abort()
    })
    const p = await context.newPage()
    p.setDefaultTimeout(20000)
    p.on('pageerror', (e) => errors.push(e.message))
    return p
  }
  async function choose(p, text, name = 'pending.md') {
    const [chooser] = await Promise.all([
      p.waitForEvent('filechooser'),
      p.getByRole('button', { name: '更新此资料', exact: true }).click(),
    ])
    await chooser.setFiles({ name, mimeType: 'text/markdown', buffer: Buffer.from(text) })
    await p
      .getByText('待更新原件已暂存在本机，重启后可恢复；尚未创建新版本或加入文献库。', {
        exact: true,
      })
      .waitFor()
  }
  try {
    await start()
    browser = await chromium.launch({ headless: true })
    page = await newPage()
    const cid = (await api('/conversations', { title: 'Fixed version acceptance' })).id
    const one = await upload(cid, '# Method\nSAME_CONTEXT\nOLD_FIXED_RESULT\n')
    await upload(cid, '# Method\nSAME_CONTEXT\nNEW_FIXED_RESULT\n', one)
    const location = `/sources/${one.revision_id}?conversation=${cid}`
    await page.goto(ui + location)
    await page.getByRole('button', { name: '版本历史', exact: true }).click()
    await page
      .locator('.version-row[data-version="1"]')
      .getByRole('button', { name: '与最新 v2 比较', exact: true })
      .click()
    const comparison = page.getByRole('region', { name: '固定版本差异', exact: true })
    await comparison
      .locator('[data-diff-status=removed]')
      .getByText('OLD_FIXED_RESULT', { exact: true })
      .waitFor()
    await comparison
      .locator('[data-diff-status=added]')
      .getByText('NEW_FIXED_RESULT', { exact: true })
      .waitFor()
    await comparison.getByLabel('只看变化').uncheck()
    await comparison
      .locator('[data-diff-status=unchanged]')
      .getByText('SAME_CONTEXT', { exact: true })
      .waitFor()
    await page.setViewportSize({ width: 760, height: 1000 })
    assert(await comparison.evaluate((e) => e.scrollWidth <= e.clientWidth + 1))
    await page.screenshot({ path: path.join(out, 'fixed-version-comparison.png'), fullPage: true })
    checks.push(
      'Actual Vue shows fixed v1 removals and v2 additions, full unchanged text and no 760px overflow'
    )
    await choose(page, '# Pending\nPERSISTED_SELECTION_BEFORE_RESTART\n')
    const staged = await api(
      `/source-versions/${one.source_id}/update-draft?conversation_id=${cid}`
    )
    assert.equal(staged.draft.expected_version, 2)
    const oldOrigin = ui
    await page.context().close()
    await stop()
    await start()
    assert.notEqual(ui, oldOrigin)
    page = await newPage()
    await page.goto(ui + location)
    await page
      .getByText('已恢复待更新资料。核对版本后可保存新版本，或取消更新。', { exact: true })
      .waitFor()
    assert.equal(
      (await api(`/sources/${one.source_id}/history?conversation_id=${cid}`)).current_version,
      2
    )
    assert.deepEqual(await api('/papers'), [])
    checks.push(
      'Independent backend process and fresh renderer origin recover staged original bytes without creating v3 or importing a paper'
    )
    // Another renderer edits the staging area while this one retains its file.
    const second = await newPage()
    await second.goto(ui + location)
    await second
      .getByText('已恢复待更新资料。核对版本后可保存新版本，或取消更新。', { exact: true })
      .waitFor()
    await choose(second, '# Other window\nREMOTE_DRAFT_SELECTION\n', 'other.md')
    await page.getByRole('button', { name: '保存新版本', exact: true }).click()
    await page.getByText(/另一窗口的暂存：other.md/).waitFor()
    await page.getByRole('button', { name: '保留本窗口文件并替换暂存', exact: true }).click()
    await page.getByRole('button', { name: '保存新版本', exact: true }).click()
    await until(
      async () =>
        (await api(`/sources/${one.source_id}/history?conversation_id=${cid}`)).current_version ===
        3,
      'explicit staged draft publish'
    )
    const latest = (await api(`/sources?conversation_id=${cid}`)).items[0]
    assert.match(
      (await api(`/sources/revisions/${latest.revision_id}/text?conversation_id=${cid}`)).text,
      /PERSISTED_SELECTION_BEFORE_RESTART/
    )
    assert.equal(
      (await api(`/source-versions/${one.source_id}/update-draft?conversation_id=${cid}`)).state,
      'consumed'
    )
    checks.push(
      'Two renderer CAS conflict retains local original; explicit review publishes only the selected bytes and consumes staging atomically'
    )
    await page.reload()
    await page.getByRole('button', { name: '更新此资料', exact: true }).waitFor()
    await choose(page, 'CANCEL_ME_AFTER_RESTART')
    await page.getByRole('button', { name: '取消更新', exact: true }).click()
    await until(
      async () =>
        (await api(`/source-versions/${one.source_id}/update-draft?conversation_id=${cid}`))
          .state === 'cancelled',
      'cancel original staging'
    )
    await page.reload()
    await page.getByRole('button', { name: '更新此资料', exact: true }).waitFor()
    assert.equal(await page.locator('[aria-label="确认更新资料"]').count(), 0)
    checks.push(
      'Explicit cancellation clears staged original and does not resurrect on renderer reload'
    )
    const otherCid = (await api('/conversations', { title: 'Picker ownership B' })).id
    const otherRecord = await upload(otherCid, 'SOURCE_B_UNCHANGED')
    const ownershipPage = await newPage()
    await ownershipPage.goto(ui + '/__source-version-ownership')
    await ownershipPage.waitForFunction(() => typeof window.setSourceTarget === 'function')
    await ownershipPage.evaluate((value) => window.setSourceTarget(value), { cid, record: latest })
    const [heldChooser] = await Promise.all([
      ownershipPage.waitForEvent('filechooser'),
      ownershipPage.getByRole('button', { name: '更新此资料', exact: true }).click(),
    ])
    await ownershipPage.locator('input[type=file]').evaluate((input) => {
      input.dataset.testIdentity = 'same-picker'
    })
    await ownershipPage.evaluate((value) => window.setSourceTarget(value), {
      cid: otherCid,
      record: otherRecord,
    })
    await ownershipPage
      .locator(`.source-versions[data-source-id="${otherRecord.source_id}"]`)
      .waitFor()
    assert.equal(
      await ownershipPage.locator('input[type=file]').getAttribute('data-test-identity'),
      'same-picker'
    )
    await heldChooser.setFiles({
      name: 'belongs-to-A.md',
      mimeType: 'text/markdown',
      buffer: Buffer.from('PICKED_FOR_A_ONLY'),
    })
    await until(
      async () =>
        (await api(`/source-versions/${one.source_id}/update-draft?conversation_id=${cid}`))
          .state === 'pending',
      'picker retains original A target'
    )
    const targetA = await api(
      `/source-versions/${one.source_id}/update-draft?conversation_id=${cid}`
    )
    assert.equal(targetA.draft.filename, 'belongs-to-A.md')
    const targetB = await api(
      `/source-versions/${otherRecord.source_id}/update-draft?conversation_id=${otherCid}`
    )
    assert.equal(targetB.draft, null)
    assert.equal(await ownershipPage.locator('[aria-label="确认更新资料"]').count(), 0)
    assert.equal(await ownershipPage.getByText(/belongs-to-A/).count(), 0)
    await ownershipPage.evaluate((value) => window.setSourceTarget(value), { cid, record: latest })
    await ownershipPage.getByText(/待保存：belongs-to-A.md/).waitFor()
    checks.push(
      'Same Vue component and unchanged file input retain picker ownership A when props switch to B before selection; only A is staged and B UI remains clean'
    )
    await ownershipPage.context().close()
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    await fs.writeFile(
      path.join(out, 'report.json'),
      JSON.stringify(
        {
          status: 'passed',
          checks,
          provider_requests: 0,
          external_requests: external,
          browser_errors: errors,
          actual_backend_restart: true,
          fresh_renderer_origin: true,
        },
        null,
        2
      )
    )
  } catch (err) {
    await page?.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(
      path.join(out, 'report.json'),
      JSON.stringify(
        {
          status: 'failed',
          checks,
          error: String(err),
          provider_requests: 0,
          browser_errors: errors,
          external_requests: external,
        },
        null,
        2
      )
    )
    throw err
  } finally {
    await browser?.close()
    await stop()
    await fs.writeFile(path.join(out, 'backend.log'), log)
  }
}
main().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
