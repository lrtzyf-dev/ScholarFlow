// Real Vue + isolated backend/SQLite + a held local synthetic SSE provider.
// No paid API, live credential, existing server or port 5173 is used.
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
const names = ['NEXT_TURN_PARENT', 'NEXT_TURN_CHILD_ONE', 'NEXT_TURN_CHILD_TWO',
  'NEXT_TURN_STOP_PARENT', 'NEXT_TURN_STOP_CHILD']

async function until(check, label, timeout = 25000) {
  const deadline = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(35)
  }
}
async function freePort() {
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  return port
}

async function main() {
  const output = path.join(root, 'verification/chat-next-turn')
  await fs.mkdir(output, { recursive: true })
  const files = ['frontend/tests/chat-next-turn.cjs', 'frontend/src/views/ChatView.vue',
    'frontend/src/api.ts', 'frontend/src/chatRunObserver.ts', 'frontend/src/chatResume.ts',
    'frontend/src/modelStatus.ts', 'frontend/src/App.vue', 'frontend/src/components/TopBar.vue',
    'backend/app/api/chat.py', 'backend/app/api/run_inputs.py', 'backend/app/run_inputs.py',
    'backend/app/next_turn_dispatch.py', 'backend/app/agent/execution.py', 'backend/app/run_lifecycle.py',
    'backend/app/agent/loop.py', 'backend/app/chat_events.py', 'backend/app/main.py', 'backend/app/db.py',
    'backend/app/llm.py', 'backend/app/task_runner.py', 'backend/app/call_ledger.py',
    'backend/app/source_models.py', 'backend/app/source_store.py', 'backend/app/api/sources.py']
  const hashes = async () => Object.fromEntries(await Promise.all(files.map(async (name) =>
    [name, crypto.createHash('sha256').update(await fs.readFile(path.join(root, name))).digest('hex')]
  )))
  const before = await hashes()
  const data = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = { ...process.env, DATA_DIR: path.join(data, 'data'), APPDATA: path.join(data, 'appdata'),
    LOCALAPPDATA: path.join(data, 'localappdata'), LLM_API_KEY: '', EMBEDDING_API_KEY: '',
    REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1' }
  const calls = [], pending = new Map(), providerErrors = [], errors = [], checks = []
  const provider = http.createServer(async (req, res) => {
    try {
      let body = ''
      for await (const chunk of req) body += chunk
      const payload = JSON.parse(body)
      const question = [...payload.messages].reverse().find((message) =>
        message.role === 'user' && names.some((name) => String(message.content).includes(name)))?.content
      const name = names.find((candidate) => String(question).trim() === candidate)
      assert.ok(name, `Unexpected synthetic request: ${String(question).slice(0, 160)}`)
      assert.equal(payload.stream, true)
      calls.push({ name, model: payload.model, at: Date.now() })
      assert.equal(calls.filter((call) => call.name === name).length, 1, `${name} requested only once`)
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' })
      res.flushHeaders()
      let closed = false
      res.on('close', () => { closed = true })
      const frame = (value) => { if (!closed) res.write(`data: ${JSON.stringify(value)}\n\n`) }
      pending.set(name, {
        thinking(text) { frame({ choices: [{ delta: { reasoning_content: text } }] }) },
        answer(text) { frame({ choices: [{ delta: { content: text } }] }) },
        finish() {
          if (closed) return
          frame({ choices: [], usage: { prompt_tokens: 9, completion_tokens: 7, total_tokens: 16 } })
          res.end('data: [DONE]\n\n')
          closed = true
        },
      })
    } catch (error) {
      providerErrors.push(error.stack || error.message)
      if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: { message: 'Synthetic fixture rejected an unexpected request' } }))
    }
  })
  await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
  const port = await freePort()
  const backend = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
    { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let backendLog = ''
  backend.stdout.on('data', (value) => { backendLog += value })
  backend.stderr.on('data', (value) => { backendLog += value })
  const base = `http://127.0.0.1:${port}`
  let vite, browser, page
  const api = async (apiPath, body, method = body === undefined ? 'GET' : 'POST') => {
    const response = await fetch(base + '/api' + apiPath, { method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(15000) })
    assert.equal(response.status, 200, `${method} ${apiPath}: ${await response.clone().text()}`)
    return response.json()
  }
  const inbox = (id, cid) => api(`/chat/runs/${id}/inputs?conversation_id=${cid}`)
  const runs = async (cid) => (await api(`/foreground-runs?conversation_id=${cid}`)).runs
  const history = async (cid) => (await api(`/conversations/${cid}`)).messages
  const control = async (name) => {
    await until(() => pending.has(name), `provider ${name}`)
    return pending.get(name)
  }
  const readSnapshot = async (id, cid) => {
    const abort = new AbortController()
    const response = await fetch(`${base}/api/chat/runs/${id}/events?conversation_id=${cid}`, { signal: abort.signal })
    assert.equal(response.status, 200)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let text = ''
    try {
      while (!text.includes('\n\n')) {
        const chunk = await reader.read()
        assert.equal(chunk.done, false)
        text += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n')
      }
      const line = text.split('\n').find((row) => row.startsWith('data:'))
      return JSON.parse(line.slice(5).trim())
    } finally {
      abort.abort()
      await reader.cancel().catch(() => {})
    }
  }
  try {
    await until(async () => {
      if (backend.exitCode !== null) throw Error(backendLog)
      try { return (await fetch(base + '/api/health')).ok } catch { return false }
    }, 'isolated backend', 35000)
    const profile = await api('/model-profiles', { name: 'Synthetic next-turn provider',
      base_url: `http://127.0.0.1:${provider.address().port}/v1`, model_id: 'Synthetic-Next-Turn',
      api_key: 'generated-local-next-turn-credential', reasoning_effort: '' })
    await api('/model-profiles/default', { model_profile_id: profile.id }, 'PUT')
    const conversations = {}
    for (const name of ['A', 'B', 'Stop']) {
      const item = await api('/conversations', { title: `自动下一轮验证 ${name}` })
      conversations[name] = item.id
      const options = await api(`/conversation-options/${item.id}`)
      await api(`/conversation-options/${item.id}`, { expected_revision: options.revision,
        model_profile_id: profile.id, approval_mode: 'full_access', reasoning_effort: '' }, 'PUT')
    }
    const { createServer } = await import('vite')
    vite = await createServer({ root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error', server: { host: '127.0.0.1', port: await freePort(), strictPort: true, hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } } } })
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    page.setDefaultTimeout(7000)
    page.on('pageerror', (error) => errors.push(error.message))
    const composer = page.locator('textarea.textarea')
    const open = async (name) => {
      await page.goto(`${ui}/chat?conv=${conversations[name]}`)
      await until(() => composer.isEnabled(), `composer ${name}`)
    }
    const submit = async (name) => {
      await composer.fill(name)
      await page.getByRole('button', { name: '发送', exact: true }).click()
      await control(name)
    }
    const enqueue = async (name, id, cid) => {
      await page.getByLabel('运行中输入用途', { exact: true }).selectOption('next_turn')
      await composer.fill(name)
      for (let retry = 0; retry < 3; retry++) {
        const button = page.getByRole('button', { name: '保存排队消息', exact: true })
        await until(() => button.isEnabled(), 'queue submit ready')
        await button.click()
        await until(async () => (await inbox(id, cid)).inputs.some((row) => row.content === name) ||
          await page.locator('.run-input-error').count() > 0, 'queue reception')
        if ((await inbox(id, cid)).inputs.some((row) => row.content === name)) break
        assert.ok(retry < 2, 'queue could not synchronize its CAS revision')
        await pause(1700)
      }
      await until(async () => (await composer.inputValue()) === '', 'queue clears only acknowledged draft')
      await pause(1700)
    }

    await open('A')
    await submit(names[0])
    const parentStream = await control(names[0])
    parentStream.thinking('PARENT_PUBLIC_THOUGHT ')
    parentStream.answer('PARENT_VISIBLE_BEFORE_QUEUE ')
    const parent = (await runs(conversations.A)).find((row) => row.question === names[0])
    assert.ok(parent)
    await enqueue(names[1], parent.id, conversations.A)
    await enqueue(names[2], parent.id, conversations.A)
    assert.equal(calls.length, 1)
    assert.equal((await inbox(parent.id, conversations.A)).inputs.length, 2)
    await open('B')
    await page.reload()
    parentStream.answer('PARENT_FULLY_COMPLETED ')
    parentStream.finish()
    const childOne = await control(names[1])
    assert.equal(calls.filter((call) => call.name === names[2]).length, 0)
    childOne.thinking('CHILD_ONE_THOUGHT_WHILE_AWAY ')
    childOne.answer('CHILD_ONE_OUTPUT_WHILE_AWAY ')
    await until(async () => (await history(conversations.A)).length === 1, 'parent QA committed')
    const firstQueue = await inbox(parent.id, conversations.A)
    const childOneId = firstQueue.inputs[0].child_run_id
    assert.equal(firstQueue.inputs[0].status, 'dispatched')
    assert.equal(firstQueue.inputs[1].status, 'queued')
    const beforeObserve = calls.length
    for (let count = 0; count < 3; count++) {
      const snapshot = await readSnapshot(childOneId, conversations.A)
      assert.equal(snapshot.kind, 'snapshot')
    }
    assert.equal(calls.length, beforeObserve, 'GET observers do not submit model requests')
    checks.push('two UI-queued inputs persist while page leaves and reloads; server starts the first child once')

    await open('A')
    await page.getByText('CHILD_ONE_OUTPUT_WHILE_AWAY', { exact: false }).waitFor()
    await page.getByText('PARENT_FULLY_COMPLETED', { exact: false }).waitFor()
    const process = page.locator('.turn').last().locator('.run-process-toggle')
    await process.click()
    await page.getByText('CHILD_ONE_THOUGHT_WHILE_AWAY', { exact: false }).waitFor()
    await page.reload()
    await page.getByText('CHILD_ONE_OUTPUT_WHILE_AWAY', { exact: false }).waitFor()
    childOne.answer('CHILD_ONE_OUTPUT_AFTER_RETURN ')
    await page.getByText('CHILD_ONE_OUTPUT_AFTER_RETURN', { exact: false }).waitFor()
    await page.screenshot({ path: path.join(output, 'child-one-reattached-live.png'), fullPage: true })
    assert.equal((await history(conversations.A)).length, 1, 'child output is visible before its final QA')
    assert.equal(calls.filter((call) => call.name === names[1]).length, 1)
    checks.push('return and refresh restore child live output, then show new deltas before final answer')
    childOne.answer('CHILD_ONE_COMPLETED ')
    childOne.finish()
    const childTwo = await control(names[2])
    childTwo.thinking('CHILD_TWO_PUBLIC_THOUGHT ')
    childTwo.answer('CHILD_TWO_RUNNING_AUTOMATICALLY ')
    await page.getByText('CHILD_TWO_RUNNING_AUTOMATICALLY', { exact: false }).waitFor({ timeout: 15000 })
    assert.equal((await history(conversations.A)).length, 2)
    const secondQueue = await inbox(parent.id, conversations.A)
    assert.deepEqual(secondQueue.inputs.map((row) => row.status), ['dispatched', 'dispatched'])
    assert.notEqual(secondQueue.inputs[0].child_run_id, secondQueue.inputs[1].child_run_id)
    childTwo.answer('CHILD_TWO_COMPLETED ')
    childTwo.finish()
    await until(async () => (await history(conversations.A)).length === 3, 'three completed QAs')
    await page.getByText('CHILD_TWO_COMPLETED', { exact: false }).waitFor()
    assert.deepEqual(calls.map((call) => call.name), names.slice(0, 3))
    await page.screenshot({ path: path.join(output, 'automatic-chain-completed.png'), fullPage: true })
    checks.push('second child starts only after first full success; all three QAs appear without a continue click')

    await open('Stop')
    await submit(names[3])
    const stopping = await control(names[3])
    stopping.answer('STOP_PARENT_PARTIAL_OUTPUT ')
    const stopParent = (await runs(conversations.Stop)).find((row) => row.question === names[3])
    await enqueue(names[4], stopParent.id, conversations.Stop)
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    stopping.finish()
    await until(async () => (await runs(conversations.Stop)).some((row) => row.id === stopParent.id && row.status === 'cancelled'), 'parent stopped')
    await pause(1800)
    assert.equal(calls.filter((call) => call.name === names[4]).length, 0)
    const stoppedQueue = await inbox(stopParent.id, conversations.Stop)
    assert.equal(stoppedQueue.inputs[0].status, 'queued')
    assert.match(stoppedQueue.inputs[0].reason, /停止|尚未完整/)
    for (let count = 0; count < 2; count++) await readSnapshot(stopParent.id, conversations.Stop)
    assert.equal(calls.length, 4)
    await page.reload()
    await page.getByText('前一轮已停止，下一轮已保留', { exact: true }).waitFor()
    await page.screenshot({ path: path.join(output, 'stopped-parent-retains-queue.png'), fullPage: true })
    checks.push('stopped parent retains its next input without executing it; re-entry and GET remain read-only')
    assert.deepEqual(errors, [])
    assert.deepEqual(providerErrors, [])
    const after = await hashes()
    assert.deepEqual(after, before, 'source changed during test; rerun against a stable snapshot')
    const report = { ok: true, verified_at: new Date().toISOString(), data_isolated: true,
      real_provider_calls: 0, synthetic_provider_calls: calls, source_sha256: after, checks,
      screenshots: ['child-one-reattached-live.png', 'automatic-chain-completed.png', 'stopped-parent-retains-queue.png'] }
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report, null, 2))
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
      await fs.writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText()).catch(() => {})
    }
    throw error
  } finally {
    for (const stream of pending.values()) stream.finish()
    await browser?.close()
    await vite?.close()
    backend.kill()
    await new Promise((resolve) => backend.exitCode !== null ? resolve() : backend.once('exit', resolve))
    provider.closeAllConnections()
    await new Promise((resolve) => provider.close(resolve))
    await fs.writeFile(path.join(output, 'backend.log'), backendLog)
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
