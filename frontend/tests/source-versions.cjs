// Actual Vue and isolated HTTP backend. Synthetic files; no model requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn, spawnSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label, timeout = 30000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(60)
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
  const output = path.join(root, 'verification/source-versions/e2e')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const fixture = path.join(isolated, 'fixtures')
  await fs.mkdir(fixture)
  const md = path.join(fixture, 'research.md'),
    docx = path.join(fixture, 'paper.docx')
  const local = path.join(fixture, 'project-note.md')
  await fs.writeFile(md, '# Original\nMARKDOWN_V1_CITED_LINE\n')
  await fs.writeFile(local, '# Local original\nLOCAL_V1_FIXED\n')
  const word = (content) => {
    const result = spawnSync(
      python,
      [
        '-c',
        'from docx import Document; import sys; d=Document(); d.add_paragraph(sys.argv[2]); d.save(sys.argv[1])',
        docx,
        content,
      ],
      { windowsHide: true, encoding: 'utf8' }
    )
    assert.equal(result.status, 0, result.stderr)
  }
  word('WORD_V1_FIXED')
  const tracked = [
    'backend/app/api/sources.py',
    'backend/app/source_capture.py',
    'backend/app/source_store.py',
    'frontend/src/sourceApi.ts',
    'frontend/src/sourceUpdateDrafts.ts',
    'frontend/src/components/SourceVersions.vue',
    'frontend/src/components/ConversationSources.vue',
    'frontend/src/views/SourceReaderView.vue',
    'frontend/tests/source-versions.cjs',
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
  const before = await hashes(),
    checks = [],
    browserErrors = [],
    externalRequests = []
  let backend,
    vite,
    browser,
    page,
    backendLog = ''
  try {
    const port = await freePort()
    backend = spawn(
      python,
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
      {
        cwd: path.join(root, 'backend'),
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: {
          ...process.env,
          DATA_DIR: path.join(isolated, 'data'),
          DB_NAME: 'source-versions.db',
          VECTOR_NAME: 'source-versions.npy',
          APPDATA: path.join(isolated, 'appdata'),
          LOCALAPPDATA: path.join(isolated, 'localappdata'),
          LLM_API_KEY: '',
          LLM_DEMO_MODE: '1',
          EMBEDDING_API_KEY: '',
          REDIS_URL: '',
          DESKTOP_NOTIFICATIONS: 'false',
          PYTHONUTF8: '1',
        },
      }
    )
    backend.stdout.on('data', (chunk) => {
      backendLog += chunk
    })
    backend.stderr.on('data', (chunk) => {
      backendLog += chunk
    })
    const base = `http://127.0.0.1:${port}`
    const api = async (name, body, method = body === undefined ? 'GET' : 'POST') => {
      const response = await fetch(base + '/api' + name, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(20000),
      })
      assert.equal(response.status, 200, `${method} ${name}: ${await response.clone().text()}`)
      return response.json()
    }
    await until(
      async () => {
        if (backend.exitCode !== null) throw Error(backendLog)
        try {
          return (await fetch(base + '/api/health')).ok
        } catch {
          return false
        }
      },
      'backend ready',
      45000
    )
    const cid = (await api('/conversations', { title: '资料版本界面验收' })).id
    const secondCid = (await api('/conversations', { title: '另一会话' })).id
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
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
    await context.route('**/*', (route) => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['data:', 'blob:'].includes(url.protocol)) return route.continue()
      externalRequests.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.setDefaultTimeout(12000)
    page.on('pageerror', (err) => browserErrors.push(err.message))
    const open = async (id) => {
      await page.goto(`${ui}/chat?conv=${id}`)
      const composer = page.locator('textarea.textarea')
      await composer.waitFor()
      await until(() => composer.isEnabled(), 'conversation composer ready')
      await until(
        () => page.getByRole('button', { name: '添加资料与工具', exact: true }).isEnabled(),
        'conversation attachment controls ready'
      )
    }
    const manager = async () => {
      await page.getByRole('button', { name: '添加资料与工具', exact: true }).click()
      await page.getByRole('menuitem', { name: /上传或选择会话资料/ }).click()
      await page.getByRole('dialog', { name: '会话资料' }).waitFor()
    }
    const sourceList = () => api(`/sources?conversation_id=${cid}`)
    await open(cid)
    await manager()
    await page.locator('.conversation-sources > input[type=file]').setInputFiles([md, docx])
    await until(async () => (await sourceList()).items.length === 2, 'initial two uploads')
    const originals = (await sourceList()).items
    const md1 = originals.find((item) => item.title === 'research.md'),
      word1 = originals.find((item) => item.title === 'paper.docx')
    const locator = await api(`/sources/revisions/${md1.revision_id}/locators`, {
      conversation_id: cid,
      kind: 'text_lines',
      line_start: 2,
      line_end: 2,
    })
    const row = (item) => page.locator(`.source-record[data-revision-id="${item.revision_id}"]`)
    const updateUI = async (item, file) => {
      const target = row(item)
      const [chooser] = await Promise.all([
        page.waitForEvent('filechooser'),
        target.getByRole('button', { name: '更新此资料', exact: true }).click(),
      ])
      await chooser.setFiles(file)
      await target.getByRole('button', { name: '保存新版本', exact: true }).click()
      await until(
        async () =>
          (await sourceList()).items.find((source) => source.source_id === item.source_id)
            ?.version > item.version,
        'UI update saved'
      )
    }
    await fs.writeFile(md, '# New version\nMARKDOWN_V2_CHANGED\n')
    await updateUI(md1, md)
    word('WORD_V2_CHANGED')
    await updateUI(word1, docx)
    const updated = (await sourceList()).items
    const md2 = updated.find((item) => item.source_id === md1.source_id),
      word2 = updated.find((item) => item.source_id === word1.source_id)
    assert.equal(md2.version, 2)
    assert.equal(word2.version, 2)
    const draft = await api(`/desktop-state/conversations/${cid}`)
    assert.deepEqual(
      new Set(draft.value.source_revision_ids),
      new Set([md1.revision_id, word1.revision_id])
    )
    checks.push(
      'MD and DOCX v1→v2 are created by actual upload/update controls; source identities stay fixed and current draft remains explicitly bound to v1'
    )
    await page.goto(
      `${ui}/sources/${md1.revision_id}?conversation=${cid}&locator=${locator.locator_id}`
    )
    await page
      .locator('[data-source-highlight="true"]')
      .getByText('MARKDOWN_V1_CITED_LINE', { exact: true })
      .waitFor()
    await page.getByRole('button', { name: '将 v1 加入文献库', exact: true }).click()
    await page.getByRole('button', { name: 'v1 已入库', exact: true }).waitFor()
    const history = await api(`/sources/${md1.source_id}/history?conversation_id=${cid}`)
    assert.ok(history.items.find((item) => item.version === 1).library_paper_id)
    assert.equal(history.items.find((item) => item.version === 2).library_paper_id, null)
    await page.reload()
    await page.getByRole('button', { name: 'v1 已入库', exact: true }).waitFor()
    await page.getByRole('button', { name: '版本历史', exact: true }).click()
    await page.locator('.version-row[data-version="2"]').waitFor()
    await page.screenshot({
      path: path.join(output, 'old-version-import-and-history.png'),
      fullPage: true,
    })
    checks.push(
      'Old v1 citation still highlights its original line; reader imports only v1, preserves v2 unimported and displays both revisions after reload'
    )
    await open(cid)
    await manager()
    const conflictRow = row(md2)
    const [conflictChooser] = await Promise.all([
      page.waitForEvent('filechooser'),
      conflictRow.getByRole('button', { name: '更新此资料', exact: true }).click(),
    ])
    await fs.writeFile(md, '# User pending\nMARKDOWN_RETAINED_PENDING\n')
    await conflictChooser.setFiles(md)
    const simultaneous = new FormData()
    simultaneous.append('file', new Blob(['# Other window\nOTHER_WINDOW_V3\n']), 'research.md')
    simultaneous.append('conversation_id', cid)
    simultaneous.append('request_id', 'other-window-' + crypto.randomUUID())
    simultaneous.append('source_id', md1.source_id)
    simultaneous.append('expected_version', '2')
    const raced = await fetch(base + '/api/sources/upload', { method: 'POST', body: simultaneous })
    assert.equal(raced.status, 200)
    const racedVersion = await raced.json()
    await conflictRow.getByRole('button', { name: '保存新版本', exact: true }).click()
    await conflictRow
      .getByRole('button', { name: '以当前 v3 为基础重新确认', exact: true })
      .waitFor()
    assert.match(
      await conflictRow.locator('[aria-label="确认更新资料"]').innerText(),
      /research.md/
    )
    assert.equal(
      (await sourceList()).items.find((item) => item.source_id === md1.source_id).version,
      3
    )
    await page.screenshot({
      path: path.join(output, 'conflict-retains-pending-file.png'),
      fullPage: true,
    })
    await page.getByRole('button', { name: '关闭会话资料', exact: true }).click()
    await manager()
    await row(racedVersion)
      .getByRole('button', { name: '以当前 v3 为基础重新确认', exact: true })
      .waitFor()
    assert.match(
      await row(racedVersion).locator('[aria-label="确认更新资料"]').innerText(),
      /research.md/
    )
    await page.getByRole('button', { name: '关闭会话资料', exact: true }).click()
    let releaseOtherSources
    const heldSources = new Promise((resolve) => {
      releaseOtherSources = resolve
    })
    let holdOtherSources = true
    const holdSourceList = async (route) => {
      const request = new URL(route.request().url())
      if (holdOtherSources && request.searchParams.get('conversation_id') === secondCid)
        await heldSources
      await route.continue()
    }
    await page.route('**/api/sources?**', holdSourceList)
    await page.locator(`[data-conversation-id="${secondCid}"] .recent-conversation`).click()
    await page.waitForURL(`**/chat?conv=${secondCid}`)
    await manager()
    assert.equal(
      await page.locator('.source-record').count(),
      0,
      'no prior conversation rows while the new list is delayed'
    )
    assert.equal(
      await page.locator('[aria-label="确认更新资料"]').count(),
      0,
      'pending file is scoped to its original conversation'
    )
    holdOtherSources = false
    releaseOtherSources()
    await until(
      async () => !(await page.getByText('正在读取会话资料…', { exact: true }).count()),
      'second conversation list resolved'
    )
    await page.unroute('**/api/sources?**', holdSourceList)
    await page.getByRole('button', { name: '关闭会话资料', exact: true }).click()
    await page.locator(`[data-conversation-id="${cid}"] .recent-conversation`).click()
    await page.waitForURL(`**/chat?conv=${cid}`)
    await manager()
    const retainedRow = row(racedVersion)
    await retainedRow
      .getByRole('button', { name: '以当前 v3 为基础重新确认', exact: true })
      .waitFor()
    await retainedRow.getByRole('button', { name: '以当前 v3 为基础重新确认', exact: true }).click()
    await retainedRow.getByRole('button', { name: '保存新版本', exact: true }).click()
    await until(
      async () =>
        (await sourceList()).items.find((item) => item.source_id === md1.source_id).version === 4,
      'explicit conflict retry'
    )
    const final = (await sourceList()).items.find((item) => item.source_id === md1.source_id)
    assert.match(
      (await api(`/sources/revisions/${final.revision_id}/text?conversation_id=${cid}`)).text,
      /MARKDOWN_RETAINED_PENDING/
    )
    checks.push(
      'A real 409 retains the chosen file across dialog close/reopen and SPA conversation switches; only explicit retry in its original scope creates v4 and clears the draft'
    )
    await until(
      async () => !(await page.locator('[aria-label="确认更新资料"]').count()),
      'successful update clears all mounted pending views'
    )
    const [cancelChooser] = await Promise.all([
      page.waitForEvent('filechooser'),
      row(final).getByRole('button', { name: '更新此资料', exact: true }).click(),
    ])
    await cancelChooser.setFiles(md)
    await row(final).getByRole('button', { name: '取消更新', exact: true }).click()
    await page.getByRole('button', { name: '关闭会话资料', exact: true }).click()
    await manager()
    assert.equal(
      await page.locator('[aria-label="确认更新资料"]').count(),
      0,
      'explicit cancel clears retained files'
    )
    let historicalReads = 0
    const retryHistorical = async (route) => {
      historicalReads++
      if (historicalReads === 1)
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'synthetic temporary metadata failure' }),
        })
      else await route.continue()
    }
    const historicalRoute = `**/api/sources/revisions/${md1.revision_id}?**`
    await page.route(historicalRoute, retryHistorical)
    await open(cid)
    await until(async () => historicalReads > 0, 'historical metadata temporarily fails')
    await manager()
    await page
      .locator('.source-dialog-actions')
      .getByRole('button', { name: '刷新', exact: true })
      .click()
    await row(md1).waitFor()
    assert.ok(historicalReads >= 2, 'a failed historical metadata placeholder is retried')
    await page.unroute(historicalRoute, retryHistorical)
    checks.push(
      'A selected old revision recovers after a temporary metadata failure and explicit refresh'
    )
    await open(secondCid)
    await manager()
    assert.equal(await page.locator('.source-record').count(), 0)
    await open(cid)
    await manager()
    await row(final).getByRole('button', { name: '版本历史', exact: true }).click()
    await row(final)
      .locator('.version-row[data-version="1"]')
      .getByRole('button', { name: '本轮使用 v1', exact: true })
      .click()
    assert.ok(
      (await api(`/desktop-state/conversations/${cid}`)).value.source_revision_ids.includes(
        md1.revision_id
      )
    )
    checks.push(
      'Conversation switches do not leak history; choosing a historical version remains an explicit draft action'
    )
    const project = await api('/projects', { path: fixture })
    const projectCid = (await api('/conversations', { title: '项目文件版本更新' })).id
    await api(`/conversations/${projectCid}/project`, { project_id: project.id }, 'PUT')
    await open(projectCid)
    await page.locator('textarea.textarea').fill('@project-note')
    await page.locator('.mention-item').filter({ hasText: 'project-note.md' }).click()
    await manager()
    await page.getByRole('button', { name: /保存所选项目文件的固定版本/ }).click()
    await until(
      async () => (await api(`/sources?conversation_id=${projectCid}`)).items.length === 1,
      'local source captured'
    )
    const local1 = (await api(`/sources?conversation_id=${projectCid}`)).items[0]
    await fs.writeFile(local, '# New local version\nLOCAL_V2_CHANGED\n')
    await row(local1).getByRole('button', { name: '更新此资料', exact: true }).click()
    await row(local1).getByRole('button', { name: '保存新版本', exact: true }).click()
    await until(
      async () => (await api(`/sources?conversation_id=${projectCid}`)).items[0].version === 2,
      'local source updated in UI'
    )
    assert.match(
      (await api(`/sources/revisions/${local1.revision_id}/text?conversation_id=${projectCid}`))
        .text,
      /LOCAL_V1_FIXED/
    )
    checks.push(
      'Authorized project file can explicitly create a same-source v2 through the UI; original bytes and project/workspace binding remain fixed'
    )
    await page.setViewportSize({ width: 760, height: 820 })
    const local2 = (await api(`/sources?conversation_id=${projectCid}`)).items[0]
    await row(local2).getByRole('button', { name: '版本历史', exact: true }).click()
    await row(local2).locator('.version-row[data-version="1"]').waitFor()
    const panel = row(local2).locator('.version-panel')
    assert.equal(
      await panel.evaluate((element) => element.scrollWidth <= element.clientWidth + 1),
      true
    )
    await row(local2)
      .locator('.version-row[data-version="1"]')
      .getByRole('button', { name: '本轮使用 v1', exact: true })
      .click()
    await page.screenshot({
      path: path.join(output, 'narrow-version-controls.png'),
      fullPage: true,
    })
    checks.push(
      'At 760px window width the version panel has no horizontal overflow and historical selection remains operable'
    )
    const longName = 'a'.repeat(240) + '.md'
    await page.locator('.conversation-sources > input[type=file]').setInputFiles({
      name: longName,
      mimeType: 'text/markdown',
      buffer: Buffer.from('# Long filename\nFixed content\n'),
    })
    await until(
      async () =>
        (await api(`/sources?conversation_id=${projectCid}`)).items.some(
          (item) => item.title === longName
        ),
      'long legal filename uploaded'
    )
    const longRecord = (await api(`/sources?conversation_id=${projectCid}`)).items.find(
      (item) => item.title === longName
    )
    await row(longRecord).getByRole('button', { name: '版本历史', exact: true }).click()
    const longPanel = row(longRecord).locator('.version-panel')
    await longPanel.locator('.version-row').waitFor()
    assert.equal(
      await longPanel.evaluate((element) => element.scrollWidth <= element.clientWidth + 1),
      true,
      '240-character unbroken filename wraps without horizontal overflow'
    )
    await page.screenshot({ path: path.join(output, 'narrow-long-filename.png'), fullPage: true })
    checks.push('At 760px a legal 244-character unbroken filename wraps within the history panel')
    assert.deepEqual(browserErrors, [])
    assert.deepEqual(externalRequests, [])
    const after = await hashes()
    assert.deepEqual(after, before, 'owned source files changed during the run')
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          ok: true,
          verified_at: new Date().toISOString(),
          real_provider_calls: 0,
          model_requests: 0,
          data_isolated: true,
          checks,
          source_sha256: after,
          limitations: [
            'Browser HTTP acceptance; not packaged Windows/WebView validation.',
            'No real model quality, cloud OCR, or external services were tested.',
          ],
        },
        null,
        2
      )
    )
    console.log(JSON.stringify({ ok: true, checks, real_provider_calls: 0 }, null, 2))
  } catch (err) {
    await page
      ?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      .catch(() => {})
    if (page)
      await fs
        .writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText())
        .catch(() => {})
    throw err
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
main().catch((err) => {
  console.error(err)
  process.exitCode = 1
})
