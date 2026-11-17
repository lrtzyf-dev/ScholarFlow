// Actual Vue + isolated HTTP, generated OOXML; zero model/provider requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, label) {
  const end = Date.now() + 45000
  while (!(await fn())) { assert.ok(Date.now() < end, label); await pause(100) }
}
async function port() {
  const server = net.createServer(); await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const value = server.address().port; await new Promise((r) => server.close(r)); return value
}
async function main() {
  const output = path.join(root, 'verification/complex-docx/ui')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const python = path.join(root, 'backend/.venv/Scripts/python.exe')
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), DB_NAME: 'docx.db',
    APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  await new Promise((resolve, reject) => {
    const process = spawn(python, ['-c', "from pathlib import Path\nimport sys\nfrom tests.complex_docx_fixture import complex_docx\np=Path(sys.argv[1]);(p/'original.docx').write_bytes(complex_docx());(p/'updated.docx').write_bytes(complex_docx(variant='UPDATED'))", isolated],
      { cwd: path.join(root, 'backend'), env, windowsHide: true })
    let error = ''; process.stderr.on('data', (v) => error += v); process.on('error', reject)
    process.on('exit', (c) => c === 0 ? resolve() : reject(Error(error)))
  })
  let backend, vite, browser, page, backendLog = ''
  const errors = [], external = [], checks = []
  try {
    const backendPort = await port(), base = `http://127.0.0.1:${backendPort}`
    const start = async () => {
      backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort)],
        { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
      backend.stdout.on('data', (v) => backendLog += v); backend.stderr.on('data', (v) => backendLog += v)
      await until(async () => { if (backend.exitCode !== null) throw Error(backendLog)
        try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'backend startup')
    }
    const stop = async () => {
      const process = backend; if (!process || process.exitCode !== null) return
      await new Promise((resolve) => { process.once('exit', resolve); process.kill() }); backend = undefined
    }
    await start()
    const api = async (url, body) => {
      const response = await fetch(base + '/api' + url, { method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
      assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`); return response.json()
    }
    const cid = (await api('/conversations', { title: 'Complex Word fixed source' })).id
    const upload = async (name, request, extra = {}) => {
      const form = new FormData(); form.set('file', new Blob([await fs.readFile(path.join(isolated, name + '.docx'))]), name + '.docx')
      form.set('conversation_id', cid); form.set('request_id', request)
      for (const [key, value] of Object.entries(extra)) form.set(key, String(value))
      const response = await fetch(base + '/api/sources/upload', { method: 'POST', body: form })
      assert.equal(response.status, 200, await response.clone().text()); return response.json()
    }
    const source = await upload('original', 'docx-ui-original')
    const doc = await api(`/sources/revisions/${source.revision_id}/document?conversation_id=${cid}`)
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false, proxy: { '/api': { target: base, changeOrigin: false, ws: true } } } })
    await vite.listen(); const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname); return route.abort()
    })
    page = await context.newPage(); page.setDefaultTimeout(20000); page.on('pageerror', (e) => errors.push(e.message))
    const locations = []
    for (const phrase of ['HEADER_0 ORIGINAL', 'FOOTER ORIGINAL', 'FOOTNOTE ORIGINAL', 'ENDNOTE ORIGINAL', 'NESTED_TABLE ORIGINAL', 'AUTHOR_IMAGE_ALT ORIGINAL']) {
      const block = doc.blocks.find((b) => b.kind === 'word_part' && b.text.includes(phrase)); assert.ok(block)
      const loc = await api(`/sources/revisions/${source.revision_id}/locators`, { conversation_id: cid, kind: 'word_block', block_id: block.block_id })
      locations.push({ phrase, loc })
      await page.goto(`${ui}/sources/${source.revision_id}?conversation=${cid}&locator=${loc.locator_id}`)
      await page.locator('.source-document-block[data-source-highlight="true"]').first().waitFor()
      const highlighted = page.locator('.source-document-block[data-source-highlight="true"]').first()
      assert.ok((await highlighted.innerText()).includes(phrase))
      assert.ok((await highlighted.innerText()).includes('无排版页码'))
      if (phrase.startsWith('FOOTNOTE')) {
        await highlighted.locator('pre').evaluate((element) => {
          const range = document.createRange(); range.selectNodeContents(element)
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range)
          element.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
        })
        await page.getByRole('textbox', { name: '批注内容' }).fill('Complex Word note persists after restart')
        await page.getByRole('button', { name: '保存选段', exact: true }).click()
        await page.locator('.research-notes li').filter({ hasText: 'Complex Word note persists after restart' }).waitFor()
        await page.screenshot({ path: path.join(output, 'footnote-annotation.png'), fullPage: true })
      }
    }
    checks.push({ name: 'six_actual_part_locations_and_selection_annotation', passed: true })
    await page.goto(`${ui}/sources/${source.revision_id}?conversation=${cid}`)
    await page.getByText('原文链接 1 个（未访问）', { exact: true }).waitFor()
    await page.getByText('原文链接 1 个（未访问）', { exact: true }).click()
    assert.ok((await page.locator('.source-part-metadata').first().innerText()).includes('<script>unsafe</script>'))
    assert.equal(await page.locator('.source-part-metadata script').count(), 0)
    checks.push({ name: 'link_target_escaped_no_navigation_or_image_fetch', passed: true })
    await upload('updated', 'docx-ui-updated', { source_id: source.source_id, expected_version: 1 })
    await stop(); await start()
    const footnote = locations.find((v) => v.phrase.startsWith('FOOTNOTE'))
    await page.goto(`${ui}/sources/${source.revision_id}?conversation=${cid}&locator=${footnote.loc.locator_id}`)
    await page.locator('.source-document-block[data-source-highlight="true"]').first().waitFor()
    assert.ok((await page.locator('.source-document-block[data-source-highlight="true"]').first().innerText()).includes('FOOTNOTE ORIGINAL'))
    await page.locator('.research-notes summary').click()
    await page.locator('.research-notes li').filter({ hasText: 'Complex Word note persists after restart' }).waitFor()
    checks.push({ name: 'actual_backend_restart_old_revision_and_annotation_survive', passed: true })
    await page.setViewportSize({ width: 760, height: 950 })
    await page.screenshot({ path: path.join(output, 'narrow-fixed-footnote.png'), fullPage: true })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
    checks.push({ name: '760px_fixed_locator_visible_without_document_overflow', passed: true })
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ passed: true, checks, browser_errors: errors,
      external_requests: external, real_provider_calls: 0, synthetic_model_calls: 0,
      expected_restart_transport_interruptions: 'The intentional backend restart may reset SSE and an overlapping reading-state save; the reloaded page must restore its fixed source and annotations.',
      scope: 'Actual Vue/HTTP and backend restart, generated valid OOXML; not real research-document semantic accuracy or native Windows certification.' }, null, 2))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), backendLog, errors, external }, null, 2)); throw error
  } finally { await browser?.close(); await vite?.close(); backend?.kill() }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
