// Actual Vue, HTTP backend and generated fixed PDF/Markdown; no provider calls.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, message) {
  const end = Date.now() + 45000
  while (!(await fn())) { assert.ok(Date.now() < end, message); await pause(100) }
}
async function port() {
  const s = net.createServer(); await new Promise((r) => s.listen(0, '127.0.0.1', r))
  const value = s.address().port; await new Promise((r) => s.close(r)); return value
}
async function main() {
  const output = path.join(root, 'verification/graph-citation-context/ui')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const python = path.join(root, 'backend/.venv/Scripts/python.exe')
  const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'), DB_NAME: 'graph.db',
    APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '', EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  const run = (code, args = []) => new Promise((resolve, reject) => {
    const p = spawn(python, ['-c', code, ...args], { cwd: path.join(root, 'backend'), env, windowsHide: true })
    let error = ''; p.stderr.on('data', (v) => (error += v)); p.on('error', reject)
    p.on('exit', (c) => c === 0 ? resolve() : reject(Error(error)))
  })
  await run("from pathlib import Path\nfrom reportlab.pdfgen import canvas\nimport sys\np=Path(sys.argv[1]);c=canvas.Canvas(str(p/'body.pdf'));c.drawString(40,750,'Actual fixed PDF evidence uses prior research [1].');c.showPage();c.drawString(40,750,'References');c.drawString(40,730,'[1] Smith, J. Fixed PDF Target Research. 2020.');c.save();(p/'body.md').write_text('# Body\\nThe fixed Markdown follows Smith (2020) for comparison.\\n## References\\nSmith, J. Fixed Markdown Target Research. 2020.\\n',encoding='utf-8')", [isolated])
  let backend, vite, browser, page, backendLog = ''
  const errors = [], external = [], checks = []
  try {
    const backendPort = await port(), base = `http://127.0.0.1:${backendPort}`
    backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', (v) => (backendLog += v)); backend.stderr.on('data', (v) => (backendLog += v))
    await until(async () => { if (backend.exitCode !== null) throw Error(backendLog)
      try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'backend startup')
    const api = async (url, body) => {
      const response = await fetch(base + '/api' + url, { method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined })
      assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`); return response.json()
    }
    const cid = (await api('/conversations', { title: 'Fixed citation source' })).id
    const ids = []
    for (const extension of ['pdf', 'md']) {
      const form = new FormData(); form.set('file', new Blob([await fs.readFile(path.join(isolated, 'body.' + extension))]), 'body.' + extension)
      form.set('conversation_id', cid); form.set('request_id', 'graph-body-' + extension)
      const response = await fetch(base + '/api/sources/upload', { method: 'POST', body: form })
      assert.equal(response.status, 200, await response.clone().text())
      const source = await response.json()
      const imported = await api('/sources/library-import', { conversation_id: cid,
        revision_ids: [source.revision_id], request_id: 'graph-library-' + extension })
      assert.ok(['imported', 'existing'].includes(imported.items[0].status), JSON.stringify(imported))
      ids.push(imported.items[0].paper_id)
    }
    await run("import sys,json\nfrom sqlalchemy import delete\nfrom app.db import SessionLocal\nfrom app.models import Paper,Reference\nids=json.loads(sys.argv[1])\nwith SessionLocal() as db:\n for i,pid in enumerate(ids):\n  title=['Fixed PDF Target Research','Fixed Markdown Target Research'][i];db.execute(delete(Reference).where(Reference.paper_id==pid));target=Paper(title=title,status='parsed',content_hash='synthetic-target-'+str(i));db.add(target);db.flush();db.add(Reference(paper_id=pid,ref_index=1,raw_text='[1] Smith, J. '+title+'. 2020.',title=title,page=2 if i==0 else None))\n db.commit()", [JSON.stringify(ids)])
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: { host: '127.0.0.1', port: await port(), strictPort: true, hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } } } })
    await vite.listen(); const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true }); const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
    await context.route('**/*', (route) => { const u = new URL(route.request().url())
      if (u.origin === ui || ['data:', 'blob:'].includes(u.protocol)) return route.continue()
      external.push(u.origin + u.pathname); return route.abort() })
    page = await context.newPage(); page.setDefaultTimeout(25000); page.on('pageerror', (e) => errors.push(e.message))
    for (const [extension, title, quote] of [['pdf', 'Fixed PDF Target Research', 'Actual fixed PDF evidence uses prior research [1].'],
      ['md', 'Fixed Markdown Target Research', 'The fixed Markdown follows Smith (2020) for comparison.']]) {
      await page.goto(ui + '/graph')
      await page.getByRole('button', { name: '查看关系依据：' + title, exact: true }).dispatchEvent('click')
      const panel = page.locator('.relation-evidence'); await panel.waitFor()
      assert.ok((await panel.innerText()).includes(quote))
      assert.equal(await panel.locator('.citation-context').count(), 1)
      await page.screenshot({ path: path.join(output, extension + '-context.png'), fullPage: true })
      await panel.getByRole('button', { name: '查看这处正文引用', exact: true }).click()
      await page.waitForURL(/\/sources\/.*locator=/)
      if (extension === 'pdf') await page.locator('.source-pdf-highlight').first().waitFor()
      else assert.ok((await page.locator('[data-source-highlight=true]').first().innerText()).includes('Smith (2020)'))
      await page.screenshot({ path: path.join(output, extension + '-original.png'), fullPage: true })
      const oldUrl = page.url(); await page.reload()
      if (extension === 'pdf') await page.locator('.source-pdf-highlight').first().waitFor()
      else await page.locator('[data-source-highlight=true]').first().waitFor()
      assert.equal(page.url(), oldUrl)
      checks.push(extension + ' body quote opens its fixed original locator and survives reload')
    }
    await page.setViewportSize({ width: 760, height: 900 }); await page.goto(ui + '/graph')
    await page.getByRole('button', { name: '查看关系依据：Fixed Markdown Target Research', exact: true }).dispatchEvent('click')
    await page.locator('.citation-context').waitFor()
    await page.locator('.citation-context').first().scrollIntoViewIfNeeded()
    assert.ok(await page.locator('.relation-evidence').evaluate((e) => e.scrollWidth <= e.clientWidth + 2))
    await page.screenshot({ path: path.join(output, 'narrow-context.png'), fullPage: true })
    checks.push('760px relation evidence remains readable with no horizontal overflow')
    assert.deepEqual(errors, []); assert.deepEqual(external, [])
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ status: 'passed', checks,
      real_provider_calls: 0, browser_errors: errors, external_requests: external, data_isolated: true,
      scope: 'Actual Vue and generated originals, not packaged Windows or semantic accuracy certification' }, null, 2))
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: String(error), backendLog, errors, external }, null, 2))
    throw error
  } finally {
    await browser?.close(); await vite?.close(); backend?.kill()
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
