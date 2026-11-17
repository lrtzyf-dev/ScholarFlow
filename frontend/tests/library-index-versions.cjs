// Actual Vue and isolated HTTP, fixed OOXML versions; zero external requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, label) {
  const end = Date.now() + 45000
  while (!(await fn())) {
    assert.ok(Date.now() < end, label)
    await pause(100)
  }
}
async function port() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const value = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return value
}
async function main() {
  const output = path.join(root, 'verification/library-index-versions/ui')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-')),
    python = path.join(root, 'backend/.venv/Scripts/python.exe')
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    DB_NAME: 'library-index.db',
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    LLM_DEMO_MODE: '1',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  const seedCode = `import json,sys
from pathlib import Path
from app.db import init_db,SessionLocal
from app.models import Conversation,Chunk
from app import source_store as sources,source_library
from app.pipeline.vectordb import NumpyVectorStore
from app.config import VECTOR_PATH
from tests.test_library_index_versions import versions
init_db()
with SessionLocal() as db:
 db.add(Conversation(id='index-owner',title='Fixed library versions'));db.commit()
 scope=sources.SourceScope('conversation','index-owner')
 pid,old,new,_=versions(db,(scope,NumpyVectorStore,VECTOR_PATH))
 chunk=db.query(Chunk).filter(Chunk.paper_id==pid).first()
 evidence=source_library.augment_library_evidence(db,[{'paper_id':pid,'chunk_id':chunk.id}],'index-owner')[0]
 db.commit()
 Path(sys.argv[1]).write_text(json.dumps({'paper_id':pid,'old':old,'new':new,'evidence':evidence}),encoding='utf-8')`
  await new Promise((resolve, reject) => {
    const proc = spawn(python, ['-c', seedCode, path.join(isolated, 'seed.json')], {
      cwd: path.join(root, 'backend'),
      env,
      windowsHide: true,
    })
    let error = ''
    proc.stderr.on('data', (data) => (error += data))
    proc.on('error', reject)
    proc.on('exit', (code) => (code === 0 ? resolve() : reject(Error(error))))
  })
  const seed = JSON.parse(await fs.readFile(path.join(isolated, 'seed.json'), 'utf8'))
  let backend,
    vite,
    browser,
    page,
    log = ''
  const errors = [],
    external = [],
    checks = []
  try {
    const backendPort = await port(),
      base = `http://127.0.0.1:${backendPort}`
    const start = async () => {
      backend = spawn(
        python,
        ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(backendPort)],
        {
          cwd: path.join(root, 'backend'),
          env,
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'pipe'],
        }
      )
      backend.stdout.on('data', (data) => (log += data))
      backend.stderr.on('data', (data) => (log += data))
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
    const api = async (url, body) => {
      const response = await fetch(base + '/api' + url, {
        method: body ? 'POST' : 'GET',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      })
      assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`)
      return response.json()
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
    const context = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
    await context.addInitScript(() =>
      localStorage.setItem('scholarflow_conversation_id', 'index-owner')
    )
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(20000)
    page.on('pageerror', (error) => errors.push(error.message))
    const panel = page.locator('.library-index-versions')
    await page.goto(ui + '/library')
    await page.getByRole('button', { name: '固定版本', exact: true }).click()
    await panel.getByText('切换默认阅读不会重建检索。', { exact: false }).waitFor()
    let state = await api(`/library-index/${seed.paper_id}/state?conversation_id=index-owner`)
    assert.equal(state.primary_revision_id, seed.old)
    assert.equal(state.indexed_revision_id, seed.old)
    const newRow = panel.locator('li').filter({ has: page.locator(`input[value="${seed.new}"]`) })
    await newRow.getByRole('button', { name: '设为默认阅读', exact: true }).click()
    await until(
      async () =>
        (await api(`/library-index/${seed.paper_id}/state?conversation_id=index-owner`))
          .primary_revision_id === seed.new,
      'primary version switched'
    )
    state = await api(`/library-index/${seed.paper_id}/state?conversation_id=index-owner`)
    assert.equal(state.indexed_revision_id, seed.old)
    checks.push({ name: 'actual_primary_choice_changes_reading_only', passed: true })
    await newRow.getByRole('radio').check()
    await panel.getByRole('button', { name: '重建所选版本索引', exact: true }).click()
    await panel.getByText('已提交', { exact: true }).waitFor()
    state = await api(`/library-index/${seed.paper_id}/state?conversation_id=index-owner`)
    assert.equal(state.indexed_revision_id, seed.new)
    assert.equal(state.index_mode, 'text')
    await page.screenshot({
      path: path.join(output, 'library-index-published.png'),
      fullPage: true,
    })
    checks.push({
      name: 'actual_background_text_rebuild_atomically_published_and_reported',
      passed: true,
    })
    await newRow.getByRole('button', { name: '阅读此版', exact: true }).click()
    await page.getByText('FOOTNOTE ORIGINAL', { exact: false }).first().waitFor()
    checks.push({ name: 'new_version_actual_reader_contains_newly_indexed_footnote', passed: true })
    await stop()
    await start()
    await page.goto(
      `${ui}/sources/${seed.evidence.revision_id}?conversation=index-owner&locator=${seed.evidence.locator_id}`
    )
    await page.locator('.source-document-block[data-source-highlight="true"]').first().waitFor()
    assert.ok(
      (
        await page
          .locator('.source-document-block[data-source-highlight="true"]')
          .first()
          .innerText()
      ).includes(seed.evidence.quote)
    )
    assert.equal(
      (
        await api(
          `/sources/revisions/${seed.evidence.revision_id}/document?conversation_id=index-owner`
        )
      ).parser,
      'python-docx/1'
    )
    checks.push({ name: 'actual_restart_keeps_old_evidence_locator_on_old_parse', passed: true })
    await page.goto(ui + '/library')
    await page.getByRole('button', { name: '固定版本', exact: true }).click()
    await panel.getByText('已提交', { exact: true }).waitFor()
    await page.setViewportSize({ width: 760, height: 950 })
    await page.screenshot({ path: path.join(output, 'narrow-library-index.png'), fullPage: true })
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)
    )
    checks.push({ name: '760px_library_controls_visible_without_document_overflow', passed: true })
    for (const outcome of ['error', 'success']) {
      await page.goto(`${ui}/tests/fixtures/library-index-scope.html?paper=${seed.paper_id}`)
      await panel.locator('li').first().waitFor()
      const beforeScope = await api(
        `/library-index/${seed.paper_id}/state?conversation_id=index-owner`
      )
      const targetA = beforeScope.primary_revision_id === seed.old ? seed.new : seed.old
      const targetB = beforeScope.primary_revision_id
      let releaseA,
        releaseB,
        sentA = false,
        sentB = false
      const holdA = new Promise((resolve) => (releaseA = resolve)),
        holdB = new Promise((resolve) => (releaseB = resolve))
      const pattern = `**/api/library-index/${seed.paper_id}/primary`
      const intercept = async (route) => {
        const body = route.request().postDataJSON()
        if (body.conversation_id === 'index-owner') {
          const response = await route.fetch(
            outcome === 'error'
              ? { postData: JSON.stringify({ ...body, expected_version: 999999 }) }
              : {}
          )
          assert.equal(response.status(), outcome === 'error' ? 409 : 200)
          sentA = true
          await holdA
          await route.fulfill({ response })
        } else {
          const response = await route.fetch()
          assert.equal(response.status(), 200)
          sentB = true
          await holdB
          await route.fulfill({ response })
        }
      }
      await page.route(pattern, intercept)
      await panel
        .locator('li')
        .filter({ has: page.locator(`input[value="${targetA}"]`) })
        .getByRole('button', { name: '设为默认阅读', exact: true })
        .click()
      await until(() => sentA, 'A provider response held')
      await page.evaluate(() => window.switchIndexScope('index-scope-other'))
      await panel.locator('li').first().waitFor()
      // Error A did not change the primary; make B choose the other version.
      const bTarget = outcome === 'error' ? targetA : targetB
      const bButton = panel
        .locator('li')
        .filter({ has: page.locator(`input[value="${bTarget}"]`) })
        .getByRole('button', { name: '设为默认阅读', exact: true })
      await bButton.click()
      await until(() => sentB, 'B provider response held')
      releaseA()
      await pause(400)
      assert.equal(
        await bButton.isDisabled(),
        true,
        'A late completion must not clear B busy state'
      )
      assert.equal(
        await panel.locator('[role="alert"]').count(),
        0,
        'A late failure must not appear in B'
      )
      assert.deepEqual(
        await page.evaluate(() => window.indexScopeChanges),
        [],
        'A completion must not emit B changes'
      )
      releaseB()
      await until(
        async () => (await page.evaluate(() => window.indexScopeChanges)).length === 1,
        'B own completion emitted'
      )
      assert.deepEqual(await page.evaluate(() => window.indexScopeChanges), ['index-scope-other'])
      await page.unroute(pattern, intercept)
    }
    checks.push({
      name: 'actual_vue_delayed_success_and_409_from_A_cannot_mutate_B_busy_error_or_change_event',
      passed: true,
    })
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          passed: true,
          checks,
          browser_errors: errors,
          external_requests: external,
          real_provider_calls: 0,
          synthetic_model_calls: 0,
          scope:
            'Actual Vue/HTTP and backend restart with generated fixed OOXML; not real-provider semantic quality or native Windows certification.',
        },
        null,
        2
      )
    )
  } catch (error) {
    await page
      ?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      .catch(() => {})
    await fs.writeFile(
      path.join(output, 'failure.json'),
      JSON.stringify({ error: String(error), log, errors, external }, null, 2)
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
