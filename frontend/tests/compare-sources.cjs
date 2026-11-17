// Real Vue, real isolated HTTP backend and synthetic local originals. No model API.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label, timeout = 45000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(80)
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
  const output = path.join(root, 'verification/compare-sources')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const fixtures = path.join(isolated, 'fixtures')
  await fs.mkdir(fixtures)
  const python = path.join(root, 'backend/.venv/Scripts/python.exe')
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    DB_NAME: 'compare.db',
    VECTOR_NAME: 'compare.npy',
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '',
    LLM_DEMO_MODE: '1',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  const fixtureCode = `
from pathlib import Path
from docx import Document
from openpyxl import Workbook
from reportlab.pdfgen import canvas
import sys
p=Path(sys.argv[1])
target='The method uses COMPARISON_TARGET for evaluation.'
(p/'sample.md').write_text('# Preface\\n'+target+'\\n',encoding='utf-8')
(p/'sample.txt').write_text('Preface\\n'+target+'\\n',encoding='utf-8')
(p/'sample.csv').write_text('Method,Result\\nCOMPARISON_TARGET method,42\\n',encoding='utf-8')
d=Document();d.add_paragraph('Preface');d.add_paragraph(target);d.save(p/'sample.docx')
b=Workbook();b.active.title='Results';b.active.append(['Method','Result']);b.active.append(['COMPARISON_TARGET method',42]);b.save(p/'sample.xlsx');b.close()
c=canvas.Canvas(str(p/'sample.pdf'));c.drawString(40,750,target);c.save()
`
  await new Promise((resolve, reject) => {
    const child = spawn(python, ['-c', fixtureCode, fixtures], {
      cwd: root,
      env,
      windowsHide: true,
    })
    let error = ''
    child.stderr.on('data', (chunk) => {
      error += chunk
    })
    child.on('error', reject)
    child.on('exit', (code) => (code === 0 ? resolve() : reject(Error(error))))
  })
  const tracked = [
    'backend/app/agent/paper_compare.py',
    'frontend/src/comparisonEvidence.ts',
    'frontend/src/views/CompareView.vue',
    'frontend/tests/compare-sources.cjs',
  ]
  const hashes = async () =>
    Object.fromEntries(
      await Promise.all(
        tracked.map(async (name) => [
          name,
          crypto
            .createHash('sha256')
            .update(await fs.readFile(path.join(root, name)))
            .digest('hex'),
        ])
      )
    )
  const before = await hashes()
  let backend,
    vite,
    browser,
    page,
    backendLog = ''
  const errors = [],
    external = [],
    checks = []
  try {
    const port = await freePort()
    backend = spawn(
      python,
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
    )
    backend.stdout.on('data', (chunk) => {
      backendLog += chunk
    })
    backend.stderr.on('data', (chunk) => {
      backendLog += chunk
    })
    const base = `http://127.0.0.1:${port}`
    const api = async (url, body) => {
      const response = await fetch(base + '/api' + url, {
        method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(30000),
      })
      assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`)
      return response.json()
    }
    await until(async () => {
      if (backend.exitCode !== null) throw Error(backendLog)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'backend startup')
    const origin = (await api('/conversations', { title: 'Comparison synthetic original' })).id
    const reader = (await api('/conversations', { title: 'Comparison synthetic reader' })).id
    const papers = [],
      revisions = []
    for (const extension of ['pdf', 'md', 'txt', 'docx', 'xlsx', 'csv']) {
      const form = new FormData()
      form.set(
        'file',
        new Blob([await fs.readFile(path.join(fixtures, `sample.${extension}`))]),
        `sample.${extension}`
      )
      form.set('conversation_id', origin)
      form.set('request_id', `compare-upload-${extension}`)
      const response = await fetch(base + '/api/sources/upload', { method: 'POST', body: form })
      assert.equal(response.status, 200, await response.clone().text())
      const source = await response.json()
      revisions.push(source)
      const imported = await api('/sources/library-import', {
        conversation_id: origin,
        revision_ids: [source.revision_id],
        request_id: `compare-import-${extension}`,
      })
      assert.ok(
        ['imported', 'existing'].includes(imported.items[0].status),
        JSON.stringify(imported)
      )
      papers.push({ id: imported.items[0].paper_id, extension })
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
    const context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
      acceptDownloads: true,
    })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (error) => errors.push(error.message))
    await page.goto(`${ui}/chat?conv=${reader}`)
    await until(() => page.locator('textarea.textarea').isEnabled(), 'conversation hydration')
    const compareUrl = `${ui}/compare?${new URLSearchParams(papers.map(({ id }) => ['papers', id]))}`
    const compare = async () => {
      await page.goto(compareUrl)
      const response = page.waitForResponse(
        (r) => r.url().endsWith('/api/papers/compare') && r.request().method() === 'POST'
      )
      await page.getByRole('button', { name: '开始对比', exact: true }).click()
      const result = await response
      assert.equal(result.status(), 200, await result.text())
      await page.locator('.comparison-notice').waitFor()
      return result.json()
    }
    let result = await compare()
    assert.ok(result.conversation_id && result.conversation_id !== origin)
    await page.screenshot({ path: path.join(output, 'comparison-six-formats.png'), fullPage: true })
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出 CSV', exact: true }).click()
    const downloaded = await download
    const csvPath = path.join(output, 'comparison-evidence.csv')
    await downloaded.saveAs(csvPath)
    const csv = await fs.readFile(csvPath, 'utf8')
    assert.ok(csv.includes('核验状态') && csv.includes('固定版本ID') && csv.includes('原件SHA256'))
    assert.ok(
      csv.includes('text/markdown') &&
        csv.includes('word_paragraphs') &&
        csv.includes('sheet_cells')
    )
    checks.push(
      'Actual UI comparison and CSV download retain mixed source identity, hashes, typed locations and unverified state'
    )
    for (const paper of papers) {
      const evidence = result.rows
        .flatMap((row) => row.cells[paper.id].evidence)
        .find((item) => item.quote.includes('COMPARISON_TARGET'))
      assert.ok(evidence, `${paper.extension} target evidence`)
      const button =
        paper.extension === 'pdf'
          ? page
              .locator(`.ev-chip[data-paper-id="${paper.id}"]`)
              .filter({ hasText: 'COMPARISON_TARGET' })
              .first()
          : page.locator(`.ev-chip[data-locator-id="${evidence.locator_id}"]`).first()
      await button.click()
      if (paper.extension === 'pdf') {
        await page.waitForURL(new RegExp(`/reader/${paper.id}`))
        await page.locator('canvas').first().waitFor()
        assert.equal(new URL(page.url()).searchParams.get('page'), String(evidence.page))
      } else {
        await page.waitForURL(new RegExp(`/sources/${evidence.revision_id}`))
        assert.equal(new URL(page.url()).searchParams.get('locator'), evidence.locator_id)
        assert.equal(new URL(page.url()).searchParams.get('conversation'), result.conversation_id)
        await page
          .locator('[data-source-highlight="true"]')
          .filter({ hasText: 'COMPARISON_TARGET' })
          .first()
          .waitFor()
        await page.reload()
        await page
          .locator('[data-source-highlight="true"]')
          .filter({ hasText: 'COMPARISON_TARGET' })
          .first()
          .waitFor()
      }
      await page.screenshot({
        path: path.join(output, `locator-${paper.extension}.png`),
        fullPage: true,
      })
      checks.push(
        `${paper.extension} comparison opens exact original location; fixed source survives reader reload`
      )
      result = await compare()
    }
    // Updating the original attachment must not retarget already imported v1 evidence.
    const markdown = revisions.find((revision) => revision.media_type === 'text/markdown')
    const form = new FormData()
    form.set('file', new Blob(['The method uses REPLACED_VERSION.\n']), 'sample.md')
    form.set('conversation_id', origin)
    form.set('request_id', 'compare-update-md')
    form.set('source_id', markdown.source_id)
    form.set('expected_version', '1')
    const update = await fetch(base + '/api/sources/upload', { method: 'POST', body: form })
    assert.equal(update.status, 200, await update.clone().text())
    result = await compare()
    const mdPaper = papers.find((paper) => paper.extension === 'md')
    const old = result.rows
      .flatMap((row) => row.cells[mdPaper.id].evidence)
      .find((evidence) => evidence.quote.includes('COMPARISON_TARGET'))
    await page.locator(`.ev-chip[data-locator-id="${old.locator_id}"]`).first().click()
    await page
      .locator('[data-source-highlight="true"]')
      .filter({ hasText: 'COMPARISON_TARGET' })
      .first()
      .waitFor()
    assert.ok(!(await page.locator('body').innerText()).includes('REPLACED_VERSION'))
    checks.push('Updating attachment v2 does not retarget the imported v1 comparison citation')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    const after = await hashes()
    assert.deepEqual(after, before, 'Comparison sources changed during verification')
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          ok: true,
          verified_at: new Date().toISOString(),
          checks,
          browser_errors: errors,
          external_browser_requests: external,
          real_provider_calls: 0,
          data_isolated: true,
          source_sha256: after,
          limitations: [
            'Synthetic material and local retrieval; no real model quality assessment.',
            'Browser validation does not replace packaged Windows/WebView acceptance.',
            'Legacy library PDFs retain genuine page/bbox; non-PDFs use scoped fixed revision/locator.',
            'Comparison remains retrieved candidate passages, not a persistent editable evidence matrix.',
          ],
        },
        null,
        2
      )
    )
    console.log(JSON.stringify({ ok: true, checks: checks.length, real_provider_calls: 0 }))
  } catch (error) {
    await page
      ?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      .catch(() => {})
    await fs.writeFile(
      path.join(output, 'failure-report.json'),
      JSON.stringify(
        {
          ok: false,
          error: String(error.stack || error),
          checks,
          browser_errors: errors,
          external_browser_requests: external,
        },
        null,
        2
      )
    )
    throw error
  } finally {
    await browser?.close()
    await vite?.close()
    if (backend) {
      backend.kill()
      await new Promise((resolve) =>
        backend.exitCode !== null ? resolve() : backend.once('exit', resolve)
      )
    }
    await fs.writeFile(path.join(output, 'backend.log'), backendLog)
    // Preserve isolated synthetic originals for reproducibility; never touch app data.
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
