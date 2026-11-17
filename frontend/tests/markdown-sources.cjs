// Real Vue + isolated local backend + synthetic SSE. No real keys or network services.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')

const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label, timeout = 30000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(50)
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
  const output = path.join(root, 'verification/markdown-sources/e2e')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const fixture = path.join(isolated, 'fixtures')
  await fs.mkdir(fixture)
  const target = '结论：**MARKDOWN_LOCATOR_TARGET**，样本数 42。'
  const lines = Array.from({ length: 230 }, (_, index) => `Markdown source line ${index + 1}`)
  lines[0] = '# Synthetic Markdown source'
  lines[1] = '<script>window.__MARKDOWN_EXECUTED__ = true</script>'
  lines[2] = '![external image](https://markdown-safety.invalid/markdown-image.png)'
  lines[3] = '<img src="https://markdown-safety.invalid/html-image.png" onerror="window.__MARKDOWN_EXECUTED__=true">'
  lines[4] = '[unsafe link](javascript:window.__MARKDOWN_EXECUTED__=true)'
  lines[5] = '```javascript'
  lines[6] = 'window.__MARKDOWN_EXECUTED__ = true'
  lines[7] = '```'
  lines[204] = target
  const markdown = '\uFEFF' + lines.join('\r\n')
  const appendix = '# Appendix\n- Markdown alias remains source data.\n'
  const projectOriginal = '# Local project Markdown\nLOCAL_MARKDOWN_FIXED_ORIGINAL\n'
  await fs.writeFile(path.join(fixture, 'research.MD'), markdown, 'utf8')
  await fs.writeFile(path.join(fixture, 'appendix.markdown'), appendix, 'utf8')
  await fs.writeFile(path.join(fixture, 'notes.txt'), 'UNSELECTED_TEXT_MUST_NOT_ENTER_LIBRARY', 'utf8')
  await fs.writeFile(path.join(fixture, 'project-note.md'), projectOriginal, 'utf8')
  const tracked = [
    'frontend/tests/markdown-sources.cjs',
    'frontend/src/views/ChatView.vue',
    'frontend/src/views/SourceReaderView.vue',
    'frontend/src/components/ConversationSources.vue',
    'frontend/src/components/SourceReferences.vue',
    'frontend/src/sourceSelection.ts',
    'frontend/src/sourceApi.ts',
    'frontend/src/views/LibraryView.vue',
    'backend/app/api/sources.py',
    'backend/app/source_capture.py',
    'backend/app/source_parsing.py',
    'backend/app/source_documents.py',
    'backend/app/source_store.py',
    'backend/app/source_library.py',
    'backend/app/source_runtime.py',
    'backend/app/source_tools.py',
    'backend/app/agent/execution.py',
    'backend/app/agent/audit.py',
  ]
  const hashes = async () => Object.fromEntries(await Promise.all(tracked.map(async (name) => [
    name, crypto.createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex'),
  ])))
  const before = await hashes()
  const calls = [], providerErrors = [], browserErrors = [], externalRequests = [], checks = []
  let original, backend, vite, browser, page, backendLog = '', successful = false
  const provider = http.createServer(async (req, res) => {
    try {
      assert.equal(req.url, '/v1/chat/completions')
      let raw = ''
      for await (const part of req) raw += part
      const payload = JSON.parse(raw)
      if (payload.stream !== true) {
        const audit = String(payload.messages.at(-1)?.content || '')
        const sections = [...audit.matchAll(/\[论断 (\d+)\] ([^\n]+)([\s\S]*?)(?=\[论断 \d+\]|只输出 JSON|$)/g)]
        assert.ok(sections.length, 'unexpected non-stream synthetic request')
        const results = sections.map((match) => ({ index: Number(match[1]),
          supported: match[2].includes('MARKDOWN_LOCATOR_TARGET') && match[3].includes('MARKDOWN_LOCATOR_TARGET'),
          reason: '合成来源标识逐字匹配',
        }))
        calls.push({ kind: 'synthetic_audit', model: payload.model })
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ choices: [{ index: 0, message: { role: 'assistant', content: JSON.stringify({ results }) }, finish_reason: 'stop' }],
          usage: { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20 } }))
        return
      }
      const index = calls.filter((call) => call.kind === 'synthetic_stream').length
      calls.push({ kind: 'synthetic_stream', index, model: payload.model })
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
      const frame = (delta, finish_reason = null) => res.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] })}\n\n`)
      if (index === 0) {
        assert.ok(original, 'source revision not prepared')
        frame({ tool_calls: [{ index: 0, id: 'synthetic_markdown_line_read', type: 'function', function: {
          name: 'read_attached_source', arguments: JSON.stringify({ source_revision_id: original.revision_id, line_start: 205, line_end: 205 }),
        } }] })
        frame({}, 'tool_calls')
      } else {
        assert.ok(payload.messages.some((message) => message.role === 'tool' && String(message.content).includes('MARKDOWN_LOCATOR_TARGET')))
        frame({ content: 'Markdown 原文包含 MARKDOWN_LOCATOR_TARGET [^e1]。' })
        frame({}, 'stop')
      }
      res.write(`data: ${JSON.stringify({ choices: [], usage: { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20 } })}\n\n`)
      res.end('data: [DONE]\n\n')
    } catch (err) {
      providerErrors.push(err.stack || err.message)
      if (!res.headersSent) res.writeHead(500)
      res.end('synthetic fixture failure')
    }
  })
  try {
    await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
    const port = await freePort()
    const env = { ...process.env, DATA_DIR: path.join(isolated, 'data'),
      DB_NAME: 'markdown-smoke.db', VECTOR_NAME: 'markdown-smoke.npy',
      APPDATA: path.join(isolated, 'appdata'), LOCALAPPDATA: path.join(isolated, 'localappdata'),
      LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
    backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    backend.stdout.on('data', (chunk) => { backendLog += chunk })
    backend.stderr.on('data', (chunk) => { backendLog += chunk })
    const base = `http://127.0.0.1:${port}`
    const api = async (apiPath, body, method = body === undefined ? 'GET' : 'POST') => {
      const response = await fetch(base + '/api' + apiPath, { method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20000) })
      assert.equal(response.status, 200, `${method} ${apiPath}: ${await response.clone().text()}`)
      return response.json()
    }
    await until(async () => {
      if (backend.exitCode !== null) throw Error(backendLog)
      try { return (await fetch(base + '/api/health')).ok } catch { return false }
    }, 'isolated backend', 45000)
    const profile = await api('/model-profiles', { name: 'Synthetic Markdown provider',
      base_url: `http://127.0.0.1:${provider.address().port}/v1`, model_id: 'Synthetic-Markdown',
      api_key: 'local-generated-markdown-fixture', reasoning_effort: '' })
    await api('/model-profiles/default', { model_profile_id: profile.id }, 'PUT')
    const cid = (await api('/conversations', { title: 'Markdown 资料闭环验收' })).id
    const opts = await api(`/conversation-options/${cid}`)
    await api(`/conversation-options/${cid}`, { expected_revision: opts.revision, model_profile_id: profile.id,
      approval_mode: 'full_access', reasoning_effort: '' }, 'PUT')
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error', server: { host: '127.0.0.1', port: await freePort(), strictPort: true, hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } } } })
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || url.protocol === 'data:' || url.protocol === 'blob:') return route.continue()
      externalRequests.push({ origin: url.origin, path: url.pathname })
      return route.abort()
    })
    await context.addInitScript(() => { window.__MARKDOWN_EXECUTED__ = false })
    page = await context.newPage()
    page.setDefaultTimeout(12000)
    page.on('pageerror', (err) => browserErrors.push(err.message))
    const composer = page.locator('textarea.textarea')
    const open = async (id = cid) => {
      await page.goto(`${ui}/chat?conv=${id}`)
      await until(() => composer.isEnabled(), 'composer ready')
    }
    const manager = async () => {
      await page.getByRole('button', { name: '添加资料与工具', exact: true }).click()
      await page.getByRole('menuitem', { name: /上传或选择会话资料/ }).click()
      await page.getByRole('dialog', { name: '会话资料' }).waitFor()
    }
    const closeManager = () => page.getByRole('button', { name: '完成', exact: true }).click()
    const history = async () => (await api(`/conversations/${cid}`)).messages
    await open()
    await manager()
    assert.match(await page.locator('.conversation-sources input[type=file]').getAttribute('accept'), /\.md/)
    await page.locator('.conversation-sources input[type=file]').setInputFiles(
      ['research.MD', 'appendix.markdown', 'notes.txt'].map((name) => path.join(fixture, name)))
    await until(async () => (await api(`/sources?conversation_id=${cid}`)).items.length === 3, 'Markdown and mixed sources uploaded')
    await until(async () => (await api(`/desktop-state/conversations/${cid}`)).value.source_revision_ids?.length === 3, 'source draft persisted')
    const sources = (await api(`/sources?conversation_id=${cid}`)).items
    original = sources.find((item) => item.title === 'research.MD')
    const alias = sources.find((item) => item.title === 'appendix.markdown')
    assert.equal(original.media_type, 'text/markdown')
    assert.equal(alias.media_type, 'text/markdown')
    await page.locator(`.source-record[data-revision-id="${original.revision_id}"]`).getByText(/Markdown/).waitFor()
    assert.equal((await api('/papers')).length, 0)
    await page.screenshot({ path: path.join(output, 'markdown-upload-manager.png'), fullPage: true })
    await closeManager()
    await page.reload()
    await until(() => composer.isEnabled(), 'draft reload')
    assert.equal(await page.locator('.source-attachment').count(), 3)
    checks.push('MD uppercase and .markdown alias upload as text/markdown; mixed selections persist after reload and no file automatically enters the library')

    await page.locator('.source-attachment-open').filter({ hasText: 'research.MD' }).click()
    await page.waitForURL(/\/sources\//)
    await page.locator('.source-text-line pre').filter({ hasText: '<script>' }).waitFor()
    await page.getByText(/Markdown.*原文|Markdown.*源码|Markdown.*行/).first().waitFor()
    assert.equal(await page.locator('.source-document-scroll script,.source-document-scroll img,.source-document-scroll iframe').count(), 0)
    assert.equal(await page.evaluate(() => window.__MARKDOWN_EXECUTED__), false)
    assert.deepEqual(externalRequests, [])
    await page.screenshot({ path: path.join(output, 'markdown-safe-raw-reader.png'), fullPage: true })
    const fileResponse = await fetch(`${base}/api/sources/revisions/${original.revision_id}/file?conversation_id=${cid}`)
    assert.equal(fileResponse.status, 200)
    assert.deepEqual(Buffer.from(await fileResponse.arrayBuffer()), Buffer.from(markdown, 'utf8'))
    checks.push('Markdown source is shown as inert original lines; headings, fences and HTML stay literal, scripts and images do not become DOM nodes, external resources are not fetched, downloaded bytes retain BOM and CRLF')
    await page.getByRole('button', { name: '返回会话', exact: true }).click()
    await until(() => composer.isEnabled(), 'return to chat')
    const update = new FormData()
    update.append('file', new Blob(['# New version\nNEW_MARKDOWN_MUST_NOT_REPLACE_OLD_CITATION\n'], { type: 'text/markdown' }), 'research.MD')
    update.append('conversation_id', cid)
    update.append('request_id', 'markdown_update_' + crypto.randomUUID())
    update.append('source_id', original.source_id)
    update.append('expected_version', '1')
    const response = await fetch(base + '/api/sources/upload', { method: 'POST', body: update })
    assert.equal(response.status, 200)
    const newVersion = await response.json()
    assert.equal(newVersion.version, 2)
    await composer.fill('MARKDOWN_CITATION_V1')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await until(async () => (await history()).some((item) => item.question === 'MARKDOWN_CITATION_V1'), 'Markdown cited answer', 45000)
    const answer = (await history()).find((item) => item.question === 'MARKDOWN_CITATION_V1')
    assert.equal(answer.evidence.length, 1)
    assert.equal(answer.evidence[0].revision_id, original.revision_id)
    assert.equal(answer.evidence[0].locator_id.length, 32)
    await page.locator('button[data-evidence-id="e1"]').first().click()
    await page.waitForURL(/\/sources\//)
    await page.locator('[data-source-highlight="true"]').getByText(target, { exact: true }).waitFor()
    assert.equal(await page.locator('[data-source-highlight="true"] .source-line-number').innerText(), '205')
    await page.getByText(/存在更新版本/).waitFor()
    assert.equal(await page.getByText('NEW_MARKDOWN_MUST_NOT_REPLACE_OLD_CITATION', { exact: false }).count(), 0)
    await page.screenshot({ path: path.join(output, 'markdown-v1-line205-citation.png'), fullPage: true })
    await page.reload()
    await page.locator('[data-source-highlight="true"]').getByText(target, { exact: true }).waitFor()
    const initialZoom = await page.locator('.source-zoom-controls span').innerText()
    await page.keyboard.press('Control+=')
    await until(async () => (await page.locator('.source-zoom-controls span').innerText()) !== initialZoom, 'Markdown source zoom')
    await page.keyboard.press('Control+0')
    checks.push('A real synthetic tool read creates e1 for exact Markdown line 205; opening and reloading the citation keeps v1 and its raw Markdown despite uploaded v2; keyboard zoom works')
    await page.getByRole('button', { name: '返回会话', exact: true }).click()
    await until(() => composer.isEnabled(), 'return before explicit import')
    await manager()
    // Sending consumes the draft selection; the manager then lists the current v2.
    // Import that visible revision through the actual UI, while v1 remains cited above.
    const currentRow = page.locator(`.source-record[data-revision-id="${newVersion.revision_id}"]`)
    await currentRow.getByLabel('选择加入文献库 research.MD', { exact: true }).check()
    await page.getByRole('button', { name: /将所选 1 份加入文献库/ }).click()
    await until(async () => (await api('/papers')).length === 1, 'only chosen Markdown imported')
    const papers = await api('/papers')
    assert.equal(papers[0].title, 'research.MD')
    assert.equal(papers[0].source_media_type, 'text/markdown')
    assert.equal(papers[0].source_revision_id, newVersion.revision_id)
    await page.screenshot({ path: path.join(output, 'markdown-explicit-library-import.png'), fullPage: true })
    await closeManager()
    await page.goto(ui + '/library')
    await until(async () => (await page.locator('tr').filter({ has: page.getByRole('button', { name: 'research.MD', exact: true }) }).locator('.pdf-icon').innerText()) === 'MD', 'library Markdown type badge')
    await page.screenshot({ path: path.join(output, 'markdown-library-type.png'), fullPage: true })
    await page.getByRole('button', { name: 'research.MD', exact: true }).click()
    await page.waitForURL(/\/sources\//)
    await page.locator('.source-text-line pre').getByText('# New version', { exact: true }).waitFor()
    const libraryFile = page.locator('.source-reader-header a[download]')
    assert.ok(await libraryFile.getAttribute('href'))
    checks.push('Only the explicitly checked current v2 Markdown revision enters the library; .markdown alias, TXT and cited v1 remain unimported; API preserves text/markdown and the library shows the MD type badge; library opening uses the retained original reader')

    const project = await api('/projects', { path: fixture })
    const projectCid = (await api('/conversations', { title: 'Markdown 本地固定版本验收' })).id
    await api(`/conversations/${projectCid}/project`, { project_id: project.id }, 'PUT')
    await open(projectCid)
    await composer.fill('@project-note')
    await page.locator('.mention-item').filter({ hasText: 'project-note.md' }).click()
    await page.locator('.project-file-chip').filter({ hasText: 'project-note.md' }).waitFor()
    await manager()
    await page.getByRole('button', { name: /保存所选项目文件的固定版本/ }).click()
    await until(async () => (await api(`/sources?conversation_id=${projectCid}`)).items.length === 1, 'local Markdown captured')
    const local = (await api(`/sources?conversation_id=${projectCid}`)).items[0]
    assert.equal(local.media_type, 'text/markdown')
    await fs.writeFile(path.join(fixture, 'project-note.md'), '# Updated local Markdown\nMUTATED_LOCAL_FILE\n', 'utf8')
    assert.equal((await api(`/sources/revisions/${local.revision_id}/text?conversation_id=${projectCid}`)).text, projectOriginal)
    await page.screenshot({ path: path.join(output, 'markdown-local-project-capture.png'), fullPage: true })
    await closeManager()
    await page.locator('.source-attachment-open').filter({ hasText: 'project-note.md' }).click()
    await page.getByText('LOCAL_MARKDOWN_FIXED_ORIGINAL', { exact: true }).waitFor()
    assert.equal(await page.getByText('MUTATED_LOCAL_FILE', { exact: true }).count(), 0)
    checks.push('Authorized project .md is captured through the actual project file selector; editing the original file later leaves the saved Markdown source and reader unchanged')
    assert.equal(await page.evaluate(() => window.__MARKDOWN_EXECUTED__), false)
    assert.deepEqual(externalRequests, [])
    assert.deepEqual(providerErrors, [])
    assert.deepEqual(browserErrors, [])
    const after = await hashes()
    assert.deepEqual(after, before, 'source changed during the run; rerun once frozen')
    const report = { ok: true, verified_at: new Date().toISOString(), data_isolated: true,
      real_provider_calls: 0, external_browser_requests: externalRequests,
      synthetic_provider_calls: calls, source_sha256_at_start: before, source_sha256_at_end: after,
      source_unchanged: true, checks, ocr_or_rendered_markdown_claimed: false,
      markdown_original_sha256: crypto.createHash('sha256').update(Buffer.from(markdown, 'utf8')).digest('hex'),
      limitations: ['Source content is preserved and displayed as raw Markdown lines, not a rendered Markdown preview.',
        'The manager lists the current revision once a sent draft is consumed; a cited older revision remains readable but has no separate discoverable import button in this run.',
        'Synthetic local model verifies application wiring, not real model understanding.',
        'This browser run is not a packaged Windows/WebView release acceptance.'] }
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
    successful = true
    console.log(JSON.stringify({ ok: true, checks: checks.length, synthetic_provider_calls: calls.length,
      real_provider_calls: 0, external_browser_requests: externalRequests.length, hashed_files: tracked.length }, null, 2))
  } catch (err) {
    await page?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    if (page) await fs.writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText()).catch(() => {})
    await fs.writeFile(path.join(output, 'failure-report.json'), JSON.stringify({ ok: false, checks,
      error: String(err.stack || err), browserErrors, providerErrors, externalRequests,
      synthetic_provider_calls: calls, real_provider_calls: 0 }, null, 2))
    throw err
  } finally {
    await browser?.close()
    await vite?.close()
    if (backend) {
      backend.kill()
      await new Promise((resolve) => backend.exitCode !== null ? resolve() : backend.once('exit', resolve))
    }
    provider.closeAllConnections()
    if (provider.listening) await new Promise((resolve) => provider.close(resolve))
    await fs.writeFile(path.join(output, 'backend.log'), backendLog)
    const relative = path.relative(output, isolated)
    assert.ok(!relative.startsWith('..') && !path.isAbsolute(relative) && relative.startsWith('isolated-'))
    await fs.rm(isolated, { recursive: true, force: true, maxRetries: 8, retryDelay: 250 })
    if (successful) await fs.writeFile(path.join(output, 'cleanup.json'), JSON.stringify({
      isolated_data_removed: true, own_backend_stopped: true, own_vite_stopped: true,
      own_browser_closed: true, own_synthetic_provider_stopped: true }, null, 2))
  }
}
main().catch((err) => { console.error(err); process.exitCode = 1 })
