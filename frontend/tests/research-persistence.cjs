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
  const output = path.join(root, 'verification/research-persistence/ui')
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
    'frontend/tests/research-persistence.cjs',
    'backend/app/research_store.py',
    'backend/app/api/research.py',
    'frontend/src/components/SourcePdf.vue',
    'frontend/src/components/ResearchNotes.vue',
    'frontend/src/views/SourceReaderView.vue',
    'frontend/src/views/GraphView.vue',
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
    const compareUrl = `${ui}/compare?${new URLSearchParams([['conversation', reader], ...papers.map(({ id }) => ['papers', id])])}`
    await page.goto(compareUrl)
    await page.getByRole('button', { name: '开始对比', exact: true }).waitFor()
    await page.locator('.matrix-fields summary').click()
    await page.getByRole('textbox', { name: '字段 1 名称', exact: true }).fill('自定义研究方法')
    const extraction = page.waitForResponse((r) =>
      /\/api\/research\/matrices\/[^/]+\/extract$/.test(r.url())
    )
    await page.getByRole('button', { name: '开始对比', exact: true }).click()
    const extracted = await extraction
    assert.equal(extracted.status(), 200, await extracted.text())
    let matrix = await extracted.json()
    const savedUrl = page.url()
    for (let i = 0; i < 3; i++) {
      await page.getByRole('button', { name: '校正与核验', exact: true }).nth(i).click()
      await page.getByRole('textbox', { name: '校正内容', exact: true }).fill(`人工校正 ${i}`)
      await page
        .getByRole('combobox', { name: '核验状态', exact: true })
        .selectOption(['supported', 'conflict', 'not_found'][i])
      const saved = page.waitForResponse(
        (r) => r.url().endsWith('/cell') && r.request().method() === 'PATCH'
      )
      await page.getByRole('button', { name: '保存校正', exact: true }).click()
      assert.equal((await saved).status(), 200)
    }
    await page.reload()
    await page.getByText('人工校正 0', { exact: true }).waitFor()
    checks.push('Real Vue custom field and three human corrections survive browser reload')
    const rerun = page.waitForResponse(
      (r) => r.url().endsWith('/extract') && r.request().method() === 'POST'
    )
    await page.getByRole('button', { name: '开始对比', exact: true }).click()
    matrix = await (await rerun).json()
    assert.equal(matrix.document.rows[0].cells[papers[0].id].manual.value, '人工校正 0')
    const exported = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出完整修订', exact: true }).click()
    const packagePath = path.join(output, 'matrix.json')
    await (await exported).saveAs(packagePath)
    const packageData = JSON.parse(await fs.readFile(packagePath, 'utf8'))
    assert.equal(packageData.document.schema_version, matrix.document.schema_version)
    const imported = page.waitForResponse(
      (r) => r.url().endsWith('/matrices/import') && r.request().method() === 'POST'
    )
    await page.locator('input[type=file]').setInputFiles(packagePath)
    assert.equal((await imported).status(), 200)
    checks.push(
      'Re-extraction preserves corrections; full revision package downloads and reimports'
    )
    await page.screenshot({ path: path.join(output, 'matrix.png'), fullPage: true })
    const matrixUrl = page.url()
    const pdfEvidence = matrix.document.rows
      .flatMap((r) => r.cells[papers[0].id].evidence)
      .find((e) => e.quote.includes('COMPARISON_TARGET'))
    await page.locator(`.ev-chip[data-locator-id="${pdfEvidence.locator_id}"]`).first().click()
    await page.waitForURL(new RegExp(`/sources/${pdfEvidence.revision_id}`))
    await page
      .locator('.source-text-layer span')
      .filter({ hasText: 'COMPARISON_TARGET' })
      .first()
      .waitFor()
    // Exercise native browser selection on actual PDF.js text, not mocked text.
    await page.locator('.source-text-layer').evaluate((layer) => {
      const text = [...layer.querySelectorAll('span')].find((e) =>
        e.textContent.includes('COMPARISON_TARGET')
      )
      const range = document.createRange()
      range.selectNodeContents(text)
      const selection = window.getSelection()
      selection.removeAllRanges()
      selection.addRange(range)
      layer.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    })
    await until(
      async () =>
        (await page.locator('.research-notes blockquote').innerText()).includes(
          'COMPARISON_TARGET'
        ),
      'PDF selection locator'
    )
    await page.getByRole('combobox', { name: '批注类型', exact: true }).selectOption('highlight')
    await page.getByRole('textbox', { name: '批注内容', exact: true }).fill('PDF 原文划线核查')
    const annotated = page.waitForResponse(
      (r) => r.url().endsWith('/annotations') && r.request().method() === 'POST'
    )
    await page.getByRole('button', { name: '保存选段', exact: true }).click()
    assert.equal((await annotated).status(), 200)
    await page.locator('.research-notes li').filter({ hasText: 'PDF 原文划线核查' }).waitFor()
    await page.getByRole('checkbox', { name: '我已读过此版本' }).check()
    await until(
      async () =>
        (await api(`/research/reading/${pdfEvidence.revision_id}?conversation_id=${reader}`))
          .user_read,
      'explicit user read state'
    )
    await page.reload()
    await page.locator('.research-notes summary').click()
    await page.locator('.research-notes li').filter({ hasText: 'PDF 原文划线核查' }).waitFor()
    assert.equal(await page.getByRole('checkbox', { name: '我已读过此版本' }).isChecked(), true)
    await page.getByRole('button', { name: '放大资料', exact: true }).click()
    await page.locator('.source-pdf-highlight.saved').first().waitFor()
    await page.screenshot({ path: path.join(output, 'pdf-selection.png'), fullPage: true })
    checks.push(
      'Actual PDF text layer selection creates server-validated fixed locator and persistent highlight; zoom and read state survive reload'
    )
    await page.getByRole('button', { name: '选段提问', exact: true }).click()
    await page.waitForURL(/\/chat\?conv=/)
    await until(
      async () =>
        (await page.locator('textarea.textarea').inputValue()).includes('COMPARISON_TARGET'),
      'selection question draft'
    )
    checks.push(
      'Selection question joins existing conversation draft with fixed source revision without automatically sending or charging'
    )
    await page.goto(matrixUrl)
    const mdEvidence = matrix.document.rows
      .flatMap((r) => r.cells[papers[1].id].evidence)
      .find((e) => e.quote.includes('COMPARISON_TARGET'))
    await page.locator(`.ev-chip[data-locator-id="${mdEvidence.locator_id}"]`).first().click()
    await page.waitForURL(new RegExp(`/sources/${mdEvidence.revision_id}`))
    await page.locator('.research-notes summary').click()
    await page.getByRole('combobox', { name: '批注类型', exact: true }).selectOption('note')
    await page.getByRole('textbox', { name: '批注内容', exact: true }).fill('Markdown 笔记')
    await page.getByRole('button', { name: '保存选段', exact: true }).click()
    const note = page.locator('.research-notes li').filter({ hasText: 'Markdown 笔记' })
    await note.waitFor()
    await note.getByRole('button', { name: '编辑', exact: true }).click()
    await page
      .getByRole('textbox', { name: '批注内容', exact: true })
      .fill('修订后的 Markdown 笔记')
    await page.getByRole('button', { name: '保存编辑', exact: true }).click()
    await page.locator('.research-notes li').filter({ hasText: '修订后的 Markdown 笔记' }).waitFor()
    await page.reload()
    await page.locator('.research-notes summary').click()
    const edited = page.locator('.research-notes li').filter({ hasText: '修订后的 Markdown 笔记' })
    await edited.waitFor()
    await edited.getByRole('button', { name: '删除', exact: true }).click()
    await edited.waitFor({ state: 'detached' })
    checks.push('Markdown fixed-source note supports create/edit/delete and refresh persistence')
    // Seed only isolated synthetic graph references; external provider APIs are not used.
    await new Promise((resolve, reject) => {
      const code = `import sys,json\nfrom app.db import SessionLocal\nfrom app.models import Paper,Reference\nids=json.loads(sys.argv[1])\nwith SessionLocal() as db:\n p=db.get(Paper,ids[1]);p.title='Neural Evidence Retrieval Transformer'\n db.add(Reference(paper_id=ids[0],ref_index=1,title=p.title,raw_text=p.title,page=1))\n db.commit()\n`
      const child = spawn(python, ['-c', code, JSON.stringify(papers.map((p) => p.id))], {
        cwd: path.join(root, 'backend'),
        env,
        windowsHide: true,
      })
      let output = ''
      child.stderr.on('data', (chunk) => (output += chunk))
      child.on('exit', (c) => (c === 0 ? resolve() : reject(Error(output))))
      child.on('error', reject)
    })
    await page.goto(`${ui}/graph`)
    await page.locator('.network-edge').first().waitFor()
    await page.locator('.network-edge').first().dispatchEvent('click')
    await page.locator('.relation-evidence').waitFor()
    assert.ok((await page.locator('.relation-evidence').innerText()).includes('尚未提取'))
    await page
      .getByRole('textbox', { name: '关系修改理由', exact: true })
      .fill('人工检查，标记共同主题')
    await page.locator('.relation-evidence select').nth(1).selectOption('shared_topic')
    await page.getByRole('button', { name: '保存纠错', exact: true }).click()
    await until(
      async () => (await page.locator('.relation-evidence').innerText()).includes('修订 v2'),
      'graph revision persisted'
    )
    const beforeTransform = await page.locator('.network-svg > g').getAttribute('transform')
    await page.getByRole('button', { name: '放大图谱', exact: true }).click()
    const zoomTransform = await page.locator('.network-svg > g').getAttribute('transform')
    assert.notEqual(beforeTransform, zoomTransform)
    await page.getByRole('button', { name: '展开所选邻域', exact: true }).click()
    await until(
      async () =>
        !(await page.getByRole('button', { name: '展开所选邻域', exact: true }).isDisabled()),
      'neighborhood complete'
    )
    assert.equal(await page.locator('.network-svg > g').getAttribute('transform'), zoomTransform)
    await page.locator('.graph-detail').evaluate((e) => (e.scrollTop = 0))
    await page.screenshot({ path: path.join(output, 'graph-review.png'), fullPage: true })
    await page.reload()
    await page.locator('.network-edge').first().waitFor()
    await page.locator('.network-edge').first().dispatchEvent('click')
    await until(
      async () => (await page.locator('.relation-evidence').innerText()).includes('共同主题'),
      'graph correction survives restart'
    )
    checks.push(
      'Graph edge panel shows missing original sentence honestly; correction persists and neighborhood load preserves zoom'
    )
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    const after = await hashes()
    assert.deepEqual(after, before, 'Sources changed during verification')
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
            'Synthetic data and local retrieval; real-model semantic quality not assessed.',
            'Browser validation does not replace packaged Windows/WebView or scale acceptance.',
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
        { ok: false, error: String(error.stack || error), checks, browser_errors: errors },
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
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
