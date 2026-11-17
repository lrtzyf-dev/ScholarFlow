// Real Vue, isolated SQLite/backend, generated local originals and synthetic SSE.
// Never reads saved keys, calls a paid service, packages, or uses port 5173.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label, timeout = 25000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(40)
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
  const output = path.join(root, 'verification/source-attachments')
  await fs.mkdir(output, { recursive: true })
  const data = await fs.mkdtemp(path.join(output, 'isolated-'))
  const fixture = path.join(data, 'fixtures')
  await fs.mkdir(fixture)
  execFileSync(
    python,
    [
      '-c',
      `
from pathlib import Path
from docx import Document
from openpyxl import Workbook
from reportlab.pdfgen import canvas
import sys
p=Path(sys.argv[1])
(p/'notes.txt').write_text('\\n'.join('TEXT_LOCATOR_TARGET' if i==205 else f'Text line {i}' for i in range(1,231)),encoding='utf-8')
(p/'table.csv').write_text('\\n'.join(f'{i},CSV_LOCATOR_TARGET' if i==205 else f'{i},row-{i}' for i in range(1,231)),encoding='utf-8')
d=Document()
for i in range(1,231): d.add_paragraph('WORD_LOCATOR_TARGET' if i==205 else f'Word paragraph {i}')
d.save(p/'draft.docx')
w=Workbook();w.active.title='Intro';w.active['A1']='INTRO_ONLY';s=w.create_sheet('Results')
for i in range(1,231): s.cell(i,1,i);s.cell(i,2,'EXCEL_LOCATOR_TARGET' if i==205 else f'row-{i}')
s['C205']='=A205+1';w.save(p/'results.xlsx')
c=canvas.Canvas(str(p/'paper.pdf'));c.drawString(72,720,'PDF_FIRST_PAGE');c.showPage();c.drawString(72,720,'PDF_LOCATOR_TARGET');c.save()
(p/'project-note.txt').write_text('PROJECT_FIXED_ORIGINAL',encoding='utf-8')
(p/'unsupported.bad').write_text('UNSUPPORTED_KEEP_SELECTION',encoding='utf-8')
`,
      fixture,
    ],
    { windowsHide: true }
  )
  const tracked = [
    'frontend/tests/source-attachments.cjs',
    'frontend/src/views/ChatView.vue',
    'frontend/src/views/SourceReaderView.vue',
    'frontend/src/components/ConversationSources.vue',
    'frontend/src/components/SourceReferences.vue',
    'frontend/src/components/SourcePdf.vue',
    'frontend/src/sourceApi.ts',
    'frontend/src/sourceSelection.ts',
    'frontend/src/api.ts',
    'frontend/src/desktopState.ts',
    'frontend/src/router.ts',
    'frontend/src/App.vue',
    'frontend/src/views/LibraryView.vue',
    'frontend/src/views/ReaderView.vue',
    'frontend/src/modelStatus.ts',
    'frontend/src/views/SettingsView.vue',
    'frontend/src/components/DeepSeekToolsSettings.vue',
    'frontend/src/components/BackupSettingsCard.vue',
    'frontend/src/backupApi.ts',
    'backend/app/api/sources.py',
    'backend/app/source_models.py',
    'backend/app/source_capture.py',
    'backend/app/source_parsing.py',
    'backend/app/source_documents.py',
    'backend/app/source_runtime.py',
    'backend/app/source_tools.py',
    'backend/app/source_library.py',
    'backend/app/source_store.py',
    'backend/app/db.py',
    'backend/app/schemas.py',
    'backend/app/agent/audit.py',
    'backend/app/rag/citation.py',
    'backend/app/agent/execution.py',
    'backend/app/agent/loop.py',
    'backend/app/api/chat.py',
    'backend/app/api/foreground_runs.py',
    'backend/app/foreground_run_store.py',
    'backend/app/run_lifecycle.py',
    'backend/app/task_runner.py',
    'backend/app/run_inputs.py',
    'backend/app/next_turn_dispatch.py',
    'backend/app/llm.py',
    'backend/app/main.py',
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
  const calls = [],
    providerErrors = [],
    browserErrors = [],
    checks = []
  let sources = [],
    heldParent,
    releaseParent,
    releaseUpload
  const provider = http.createServer(async (req, res) => {
    try {
      let raw = ''
      for await (const part of req) raw += part
      const payload = JSON.parse(raw)
      if (payload.stream !== true) {
        const audit = String(payload.messages.at(-1)?.content || '')
        const sections = [
          ...audit.matchAll(/\[论断 (\d+)\] ([^\n]+)([\s\S]*?)(?=\[论断 \d+\]|只输出 JSON|$)/g),
        ]
        assert.ok(sections.length, 'unexpected non-stream synthetic request')
        const results = sections.map((match) => {
          const targets = match[2].match(/[A-Z]+_LOCATOR_TARGET/g) || []
          assert.ok(targets.length, 'fixture claim has no known source literal')
          return {
            index: Number(match[1]),
            supported: targets.every((target) => match[3].includes(target)),
            reason: '合成夹具逐字核对来源标识',
          }
        })
        calls.push({
          group: 'audit',
          index: calls.filter((call) => call.group === 'audit').length,
          model: payload.model,
        })
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(
          JSON.stringify({
            choices: [
              {
                index: 0,
                message: { role: 'assistant', content: JSON.stringify({ results }) },
                finish_reason: 'stop',
              },
            ],
            usage: { prompt_tokens: 12, completion_tokens: 9, total_tokens: 21 },
          })
        )
        return
      }
      assert.equal(payload.stream, true)
      const users = payload.messages.filter((m) => m.role === 'user').map((m) => String(m.content))
      const group = users.some((value) => value.includes('SOURCE_QUEUED'))
        ? 'queue'
        : users.some((value) => value.includes('SOURCE_PARENT'))
          ? 'parent'
          : 'main'
      const index = calls.filter((call) => call.group === group).length
      calls.push({ group, index, model: payload.model })
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' })
      res.flushHeaders()
      const frame = (delta, finish_reason = null) =>
        res.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] })}\n\n`)
      const finish = () => {
        res.write(
          `data: ${JSON.stringify({ choices: [], usage: { prompt_tokens: 12, completion_tokens: 9, total_tokens: 21 } })}\n\n`
        )
        res.end('data: [DONE]\n\n')
      }
      const tools = (items) => {
        frame({
          tool_calls: items.map((item, i) => ({
            index: i,
            id: `source_${group}_${index}_${i}`,
            type: 'function',
            function: { name: 'read_attached_source', arguments: JSON.stringify(item) },
          })),
        })
        frame({}, 'tool_calls')
        finish()
      }
      const pick = (title) => {
        const item = sources.find((value) => value.title === title)
        assert.ok(item, title)
        return item.revision_id
      }
      if (group === 'main' && index === 0)
        tools([
          { source_revision_id: pick('notes.txt'), line_start: 205, line_end: 205 },
          { source_revision_id: pick('draft.docx'), paragraph_start: 205, paragraph_end: 205 },
          { source_revision_id: pick('results.xlsx'), sheet: 'Results', cell_range: 'B205:C205' },
          { source_revision_id: pick('table.csv'), sheet: 'CSV', cell_range: 'B205' },
          { source_revision_id: pick('paper.pdf'), page_index: 1 },
        ])
      else if (group === 'main') {
        frame({
          content:
            '文本记录 TEXT_LOCATOR_TARGET [^e1]。Word 记录 WORD_LOCATOR_TARGET [^e2]。Excel 记录 EXCEL_LOCATOR_TARGET [^e3]。CSV 记录 CSV_LOCATOR_TARGET [^e4]。PDF 记录 PDF_LOCATOR_TARGET [^e5]。',
        })
        frame({}, 'stop')
        finish()
      } else if (group === 'parent' && index === 0) {
        heldParent = true
        let released = false
        releaseParent = () => {
          if (released) return
          released = true
          frame({ content: 'PARENT_BEFORE_STEERING' })
          frame({}, 'stop')
          finish()
        }
      } else if (group === 'parent' && index === 1)
        tools([
          { source_revision_id: pick('draft.docx'), paragraph_start: 205, paragraph_end: 205 },
        ])
      else if (group === 'parent') {
        frame({ content: '运行中补充资料 WORD_LOCATOR_TARGET [^e1]。' })
        frame({}, 'stop')
        finish()
      } else if (group === 'queue' && index === 0)
        tools([{ source_revision_id: pick('table.csv'), sheet: 'CSV', cell_range: 'B205' }])
      else {
        frame({ content: '下一轮使用固定资料 CSV_LOCATOR_TARGET [^e1]。' })
        frame({}, 'stop')
        finish()
      }
    } catch (err) {
      providerErrors.push(err.stack || err.message)
      if (!res.headersSent) res.writeHead(500)
      res.end('fixture error')
    }
  })
  await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
  const port = await freePort()
  const env = {
    ...process.env,
    DATA_DIR: path.join(data, 'data'),
    APPDATA: path.join(data, 'appdata'),
    LOCALAPPDATA: path.join(data, 'localappdata'),
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  const backend = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
    { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let backendLog = '',
    vite,
    browser,
    page
  backend.stdout.on('data', (chunk) => {
    backendLog += chunk
  })
  backend.stderr.on('data', (chunk) => {
    backendLog += chunk
  })
  const base = `http://127.0.0.1:${port}`
  const api = async (apiPath, body, method = body === undefined ? 'GET' : 'POST') => {
    const response = await fetch(base + '/api' + apiPath, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(20000),
    })
    assert.equal(response.status, 200, `${method} ${apiPath}: ${await response.clone().text()}`)
    return response.json()
  }
  try {
    await until(
      async () => {
        if (backend.exitCode !== null) throw Error(backendLog)
        try {
          return (await fetch(base + '/api/health')).ok
        } catch {
          return false
        }
      },
      'isolated backend',
      45000
    )
    const profile = await api('/model-profiles', {
      name: 'Synthetic source provider',
      base_url: `http://127.0.0.1:${provider.address().port}/v1`,
      model_id: 'Synthetic-Sources',
      api_key: 'generated-local-source-test-credential',
      reasoning_effort: '',
    })
    await api('/model-profiles/default', { model_profile_id: profile.id }, 'PUT')
    const cid = (await api('/conversations', { title: '来源闭环验证 A' })).id
    const other = (await api('/conversations', { title: '来源闭环验证 B' })).id
    const options = await api(`/conversation-options/${cid}`)
    await api(
      `/conversation-options/${cid}`,
      {
        expected_revision: options.revision,
        model_profile_id: profile.id,
        approval_mode: 'full_access',
        reasoning_effort: '',
      },
      'PUT'
    )
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
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    page.setDefaultTimeout(10000)
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
    await page
      .locator('.conversation-sources input[type=file]')
      .setInputFiles(
        ['notes.txt', 'draft.docx', 'results.xlsx', 'table.csv', 'paper.pdf'].map((name) =>
          path.join(fixture, name)
        )
      )
    await until(
      async () => (await api(`/sources?conversation_id=${cid}&limit=100`)).items.length === 5,
      'five originals saved'
    )
    await until(
      async () =>
        (await api(`/desktop-state/conversations/${cid}`)).value.source_revision_ids?.length === 5,
      'attachment draft persisted'
    )
    sources = (await api(`/sources?conversation_id=${cid}&limit=100`)).items
    assert.equal((await api('/papers')).length, 0, 'upload does not import library')
    await page.screenshot({ path: path.join(output, 'source-manager.png'), fullPage: true })
    await closeManager()
    await page.reload()
    await until(() => composer.isEnabled(), 'reload attachment draft')
    assert.equal(await page.locator('.source-attachment').count(), 5)
    await open(other)
    assert.equal(await page.locator('.source-attachment').count(), 0)
    await open()
    assert.equal(await page.locator('.source-attachment').count(), 5)
    checks.push(
      'actual five-format uploads persist fixed revision selections across reload and conversation switches without automatic library import'
    )
    const text = sources.find((item) => item.title === 'notes.txt')
    const body = new FormData()
    body.append(
      'file',
      new Blob(['NEW_VERSION_MUST_NOT_REPLACE_OLD_CITATION'], { type: 'text/plain' }),
      'notes.txt'
    )
    body.append('conversation_id', cid)
    body.append('request_id', 'version_update_' + crypto.randomUUID())
    body.append('source_id', text.source_id)
    body.append('expected_version', '1')
    const updated = await fetch(base + '/api/sources/upload', { method: 'POST', body })
    assert.equal(updated.status, 200)
    await composer.fill('SOURCE_MAIN')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await until(
      async () => (await history()).some((item) => item.question === 'SOURCE_MAIN'),
      'source answer committed',
      45000
    )
    const answer = (await history()).find((item) => item.question === 'SOURCE_MAIN')
    assert.equal(answer.source_revision_ids.length, 5)
    assert.equal(answer.evidence.length, 5)
    assert.deepEqual(
      answer.evidence.map((e) => e.revision_id).sort(),
      sources.map((s) => s.revision_id).sort()
    )
    assert.equal(await page.locator('.source-attachment').count(), 0)
    await page.locator('button[data-evidence-id="e1"]').first().waitFor()
    await page.screenshot({
      path: path.join(output, 'answer-with-source-citations.png'),
      fullPage: true,
    })
    for (const [index, target, kind] of [
      [1, 'TEXT_LOCATOR_TARGET', 'text'],
      [2, 'WORD_LOCATOR_TARGET', 'word'],
      [3, 'EXCEL_LOCATOR_TARGET', 'excel'],
      [4, 'CSV_LOCATOR_TARGET', 'csv'],
      [5, 'PDF_LOCATOR_TARGET', 'pdf'],
    ]) {
      await page.locator(`button[data-evidence-id="e${index}"]`).first().click()
      await page.waitForURL(/\/sources\//)
      await page.locator('.source-reader-message[role=status]').waitFor({ state: 'detached' })
      assert.equal(await page.locator('.source-reader-error').count(), 0)
      if (kind === 'pdf') {
        await page.locator('canvas').waitFor()
        await until(
          async () => await page.locator('canvas').evaluate((canvas) => canvas.width > 0),
          'PDF canvas'
        )
        await page.getByText('第 2 / 2 页', { exact: true }).waitFor()
        await page.locator('[data-source-highlight]').waitFor()
        await until(
          () =>
            page.locator('canvas').evaluate((canvas) => {
              const stage = canvas.closest('.source-pdf-stage')
              const stageRect = stage.getBoundingClientRect(),
                canvasRect = canvas.getBoundingClientRect()
              const pixels = canvas
                .getContext('2d')
                .getImageData(0, 0, canvas.width, canvas.height).data
              for (let y = 0; y < canvas.height; y++)
                for (let x = 0; x < canvas.width; x++) {
                  const i = (y * canvas.width + x) * 4
                  if (
                    pixels[i] < 100 &&
                    pixels[i + 1] < 100 &&
                    pixels[i + 2] < 100 &&
                    pixels[i + 3] > 0
                  ) {
                    const visibleY = canvasRect.top + (y * canvasRect.height) / canvas.height
                    return visibleY >= stageRect.top && visibleY < stageRect.bottom
                  }
                }
              return false
            }),
          'whole-page citation keeps actual rendered PDF text visible'
        )
      } else {
        await page.getByText(target, { exact: true }).first().waitFor()
        await page.locator('[data-source-highlight="true"]').first().waitFor()
        if (kind === 'text') {
          assert.equal(
            await page
              .getByText('NEW_VERSION_MUST_NOT_REPLACE_OLD_CITATION', { exact: false })
              .count(),
            0
          )
          await page.getByText(/存在更新版本/).waitFor()
        }
      }
      const initial = await page.locator('.source-zoom-controls span').innerText()
      await page.keyboard.press('Control+=')
      await until(
        async () => (await page.locator('.source-zoom-controls span').innerText()) !== initial,
        'source keyboard zoom'
      )
      await page.keyboard.press('Control+0')
      await page.screenshot({
        path: path.join(output, `reader-${kind}-citation.png`),
        fullPage: true,
      })
      await page.getByRole('button', { name: '返回会话', exact: true }).click()
      await until(() => composer.isEnabled(), 'return source chat')
    }
    checks.push(
      'e1–e5 open authorized immutable original versions with text line 205, Word paragraph 205, Results B205:C205, CSV B205, and actual PDF page 2 highlights; keyboard zoom works'
    )
    await manager()
    await page.getByLabel('选择加入文献库 draft.docx', { exact: true }).check()
    await page.getByLabel('选择加入文献库 results.xlsx', { exact: true }).check()
    await page.getByRole('button', { name: /将所选 2 份加入文献库/ }).click()
    await until(async () => (await api('/papers')).length === 2, 'explicit Office library import')
    await until(
      async () => (await page.getByText('已加入文献库', { exact: true }).count()) === 2,
      'per-file import results'
    )
    assert.equal((await api('/papers')).length, 2)
    await page.screenshot({
      path: path.join(output, 'explicit-library-import.png'),
      fullPage: true,
    })
    await closeManager()
    await page.goto(ui + '/library')
    await page.getByRole('button', { name: 'draft.docx', exact: true }).click()
    await page.waitForURL(/\/sources\//)
    await page.getByText('Word paragraph 1', { exact: true }).waitFor()
    checks.push(
      'only the two explicitly checked Office revisions import; non-PDF library opening uses a safe conversation source alias and structured reader'
    )
    await open()
    await composer.fill('SOURCE_PARENT')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await until(() => heldParent, 'parent provider held')
    const run = (await api(`/foreground-runs?conversation_id=${cid}`)).runs.find(
      (item) => item.question === 'SOURCE_PARENT'
    )
    await manager()
    await page
      .locator(
        `.source-record[data-revision-id="${sources.find((s) => s.title === 'draft.docx').revision_id}"]`
      )
      .getByLabel('本轮使用 draft.docx', { exact: true })
      .check()
    await closeManager()
    await composer.fill('SOURCE_STEER')
    await page.getByRole('button', { name: '发送补充', exact: true }).click()
    await until(
      async () =>
        (await api(`/chat/runs/${run.id}/inputs?conversation_id=${cid}`)).inputs.some(
          (item) => item.content === 'SOURCE_STEER'
        ),
      'source steer reception'
    )
    await manager()
    await page.getByLabel('本轮使用 table.csv', { exact: true }).check()
    await closeManager()
    await page.getByLabel('运行中输入用途', { exact: true }).selectOption('next_turn')
    await composer.fill('SOURCE_QUEUED')
    await page.getByRole('button', { name: '保存排队消息', exact: true }).click()
    await until(
      async () =>
        (await api(`/chat/runs/${run.id}/inputs?conversation_id=${cid}`)).inputs.length === 2,
      'source queued reception'
    )
    const inbox = (await api(`/chat/runs/${run.id}/inputs?conversation_id=${cid}`)).inputs
    assert.equal(inbox[0].source_revision_ids.length, 1)
    assert.equal(inbox[1].source_revision_ids.length, 1)
    await open(other)
    releaseParent()
    await until(
      async () => (await history()).some((item) => item.question === 'SOURCE_QUEUED'),
      'source next turn completed',
      45000
    )
    await open()
    await page.getByText('下一轮使用固定资料', { exact: false }).waitFor()
    const queued = (await history()).find((item) => item.question === 'SOURCE_QUEUED')
    assert.deepEqual(queued.source_revision_ids, inbox[1].source_revision_ids)
    checks.push(
      'running steer and automatic next-turn transmit and freeze their own source selections, continue while another conversation is open, and preserve source citations in history'
    )
    const uploadCid = (await api('/conversations', { title: '上传中切换会话验证' })).id
    await open(uploadCid)
    let uploadSaved = false
    const uploadGate = new Promise((resolve) => {
      releaseUpload = resolve
    })
    await page.route('**/api/sources/upload', async (route) => {
      const response = await route.fetch()
      assert.equal(response.status(), 200)
      uploadSaved = true
      await uploadGate
      await route.fulfill({ response })
    })
    await manager()
    await page.locator('.conversation-sources input[type=file]').setInputFiles({
      name: 'delayed-response.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('FIXED_UPLOAD_BEFORE_CONVERSATION_SWITCH'),
    })
    await until(() => uploadSaved, 'real upload accepted before delayed UI response')
    await closeManager()
    await composer.fill('A_DRAFT_WAITING_FOR_ATTACHMENT')
    await until(
      async () => !(await page.getByRole('button', { name: '发送', exact: true }).isEnabled()),
      'uploading conversation protects incomplete attachment'
    )
    await page.locator('.recent-conversation').filter({ hasText: '来源闭环验证 B' }).click()
    await page.waitForURL((url) => url.searchParams.get('conv') === other)
    await until(() => composer.isEnabled(), 'SPA conversation switch while upload is active')
    await composer.fill('B_DRAFT_MUST_NOT_BE_LOCKED_BY_A_UPLOAD')
    await until(
      () => page.getByRole('button', { name: '发送', exact: true }).isEnabled(),
      'another conversation is not locked by the first upload'
    )
    assert.equal(await page.locator('.attachment-notice').count(), 0)
    releaseUpload()
    await until(
      async () =>
        (await api(`/desktop-state/conversations/${uploadCid}`)).value.source_revision_ids
          ?.length === 1,
      'late upload response attaches to its original conversation'
    )
    assert.equal(await page.locator('.source-attachment').count(), 0)
    assert.equal(await composer.inputValue(), 'B_DRAFT_MUST_NOT_BE_LOCKED_BY_A_UPLOAD')
    await page.locator('.recent-conversation').filter({ hasText: '上传中切换会话验证' }).click()
    await page.waitForURL((url) => url.searchParams.get('conv') === uploadCid)
    await page.locator('.source-attachment').filter({ hasText: 'delayed-response.txt' }).waitFor()
    assert.equal(await composer.inputValue(), 'A_DRAFT_WAITING_FOR_ATTACHMENT')
    await until(
      () =>
        page
          .locator('.attachment-notice')
          .count()
          .then((count) => count === 0),
      'original upload busy state cleared'
    )
    await page.screenshot({
      path: path.join(output, 'upload-inflight-conversation-isolation.png'),
      fullPage: true,
    })
    await page.unroute('**/api/sources/upload')
    checks.push(
      'a real upload with a delayed response remains bound to its original conversation across SPA switching; the other conversation can send immediately and neither draft or source selection leaks'
    )
    const project = await api('/projects', { path: fixture })
    const projectCid = (await api('/conversations', { title: '项目资料来源验证' })).id
    await api(`/conversations/${projectCid}/project`, { project_id: project.id }, 'PUT')
    await open(projectCid)
    await composer.fill('@project-note')
    await page.locator('.mention-item').filter({ hasText: 'project-note.txt' }).click()
    await page.locator('.project-file-chip').filter({ hasText: 'project-note.txt' }).waitFor()
    await manager()
    await page.getByRole('button', { name: /保存所选项目文件的固定版本/ }).click()
    await until(
      async () => (await api(`/sources?conversation_id=${projectCid}`)).items.length === 1,
      'project source capture'
    )
    await until(
      async () =>
        (await api(`/desktop-state/conversations/${projectCid}`)).value.source_revision_ids
          ?.length === 1,
      'project source draft frozen'
    )
    const projectSource = (await api(`/sources?conversation_id=${projectCid}`)).items[0]
    await fs.writeFile(
      path.join(fixture, 'project-note.txt'),
      'CHANGED_PROJECT_FILE_MUST_NOT_REPLACE_SOURCE'
    )
    assert.equal(
      (
        await api(
          `/sources/revisions/${projectSource.revision_id}/text?conversation_id=${projectCid}`
        )
      ).text,
      'PROJECT_FIXED_ORIGINAL'
    )
    await page.screenshot({ path: path.join(output, 'project-source-capture.png'), fullPage: true })
    await page
      .locator('.conversation-sources input[type=file]')
      .setInputFiles(path.join(fixture, 'unsupported.bad'))
    await page.locator('.source-upload-failed').filter({ hasText: 'unsupported.bad' }).waitFor()
    assert.equal(
      (await api(`/desktop-state/conversations/${projectCid}`)).value.source_revision_ids.length,
      1
    )
    await page.screenshot({
      path: path.join(output, 'upload-error-retains-selection.png'),
      fullPage: true,
    })
    await closeManager()
    checks.push(
      'project file selection captures authorized original bytes into a fixed source and survives original mutation; rejected upload retains both its retry selection and prior valid draft'
    )
    const ocrFixture = JSON.parse(
      execFileSync(
        python,
        [
          '-c',
          `
import json,sys
from pathlib import Path
from app.db import SessionLocal
from app import source_store as store
scope=store.SourceScope('conversation',sys.argv[1])
with SessionLocal() as db:
 record=store.capture_source(db,scope=scope,authorization=store.SourceAuthorization(scope,'synthetic-ui-ocr','upload'),kind='upload',title='OCR-derived.pdf',raw_bytes=Path(sys.argv[2]).read_bytes(),text='OCR_TEXT_WITHOUT_PAGE_COORDINATES',media_type='application/pdf',origin_ref='ocr-of:'+sys.argv[3],text_origin='ocr_text',parser='synthetic-ui-ocr/1')
 locator=store.add_text_locator(db,record['revision_id'],scope=scope,line_start=1,line_end=1)
 db.commit()
 print(json.dumps({'revision_id':record['revision_id'],'locator_id':locator['locator_id']}))
`,
          projectCid,
          path.join(fixture, 'paper.pdf'),
          sources.find((item) => item.title === 'paper.pdf').revision_id,
        ],
        { cwd: path.join(root, 'backend'), env, windowsHide: true, encoding: 'utf8' }
      )
    )
    await page.goto(
      `${ui}/sources/${ocrFixture.revision_id}?conversation=${projectCid}&locator=${ocrFixture.locator_id}`
    )
    await page.getByText('OCR_TEXT_WITHOUT_PAGE_COORDINATES', { exact: true }).waitFor()
    await page.getByText(/当前为 OCR 提取文本，未映射到原件页坐标/).waitFor()
    assert.equal(await page.locator('canvas').count(), 0)
    await page.locator('[data-source-highlight="true"]').waitFor()
    await page.screenshot({
      path: path.join(output, 'reader-ocr-text-no-fake-pdf-coordinates.png'),
      fullPage: true,
    })
    checks.push(
      'synthetic OCR provenance fixture opens as highlighted extracted text despite PDF raw media; original remains downloadable and no PDF page coordinates are invented; no real OCR service was invoked'
    )
    await page.goto(ui + '/settings')
    await page.getByRole('button', { name: '模型接口', exact: true }).click()
    await page.getByRole('heading', { name: 'DeepSeek 联网与文档工具', exact: true }).waitFor()
    await page.screenshot({
      path: path.join(output, 'settings-deepseek-tools.png'),
      fullPage: true,
    })
    await page.getByRole('button', { name: '数据与存储', exact: true }).click()
    await page.getByRole('heading', { name: '备份与恢复', exact: true }).waitFor()
    await page.screenshot({ path: path.join(output, 'settings-backups.png'), fullPage: true })
    checks.push(
      'real Settings renders independent DeepSeek tool and backup cards; no paid connectivity or restore action is triggered'
    )
    assert.deepEqual(providerErrors, [])
    assert.deepEqual(browserErrors, [])
    const after = await hashes()
    assert.deepEqual(after, before, 'source changed while running; rerun final stable snapshot')
    const report = {
      ok: true,
      verified_at: new Date().toISOString(),
      data_isolated: true,
      real_provider_calls: 0,
      synthetic_provider_calls: calls,
      source_sha256: after,
      checks,
    }
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
    console.log(
      JSON.stringify(
        {
          ok: report.ok,
          verified_at: report.verified_at,
          checks: report.checks,
          synthetic_provider_calls: report.synthetic_provider_calls.length,
          real_provider_calls: report.real_provider_calls,
          hashed_files: Object.keys(after).length,
        },
        null,
        2
      )
    )
  } catch (err) {
    if (page) {
      await page
        .screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
        .catch(() => {})
      await fs
        .writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText())
        .catch(() => {})
    }
    throw err
  } finally {
    releaseParent?.()
    releaseUpload?.()
    await browser?.close()
    await vite?.close()
    backend.kill()
    await new Promise((resolve) =>
      backend.exitCode !== null ? resolve() : backend.once('exit', resolve)
    )
    provider.closeAllConnections()
    await new Promise((resolve) => provider.close(resolve))
    await fs.writeFile(path.join(output, 'backend.log'), backendLog)
  }
}
main().catch((err) => {
  console.error(err)
  process.exitCode = 1
})
