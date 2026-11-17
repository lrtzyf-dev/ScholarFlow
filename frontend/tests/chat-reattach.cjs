// Real Vue + isolated SQLite/backend + a controlled local OpenAI-compatible
// stream. Only generated credentials and synthetic text are used.
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
const MAIN = 'STREAM_REATTACH_MAIN'
const RESET = 'STREAM_REATTACH_RESET'
const STOP = 'STREAM_REATTACH_STOP'
const HANDOFF = 'STREAM_REATTACH_HANDOFF'
const PROGRESS = 'STREAM_REATTACH_PROGRESS'

async function until(check, label, timeout = 20000) {
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
  const output = path.join(root, 'verification/chat-reattach')
  await fs.mkdir(output, { recursive: true })
  const sourceFiles = [
    'frontend/src/views/ChatView.vue',
    'frontend/src/api.ts',
    'frontend/src/chatRunObserver.ts',
    'frontend/src/chatProcess.ts',
    'backend/app/api/chat.py',
    'backend/app/chat_events.py',
    'backend/app/api/foreground_runs.py',
    'backend/app/models.py',
    'backend/app/db.py',
    'backend/app/agent/execution.py',
    'backend/app/agent/loop.py',
    'backend/app/agent/planner.py',
    'backend/app/agent/checkpoint.py',
    'backend/app/agent/prompts.py',
    'backend/app/task_runner.py',
    'backend/app/run_lifecycle.py',
    'backend/app/run_inputs.py',
    'backend/app/next_turn_dispatch.py',
    'backend/app/local_api_guard.py',
    'backend/app/llm.py',
    'backend/app/call_ledger.py',
    'frontend/tests/chat-reattach.cjs',
  ]
  const sourceHashes = Object.fromEntries(
    await Promise.all(
      sourceFiles.map(async (name) => [
        name,
        crypto
          .createHash('sha256')
          .update(await fs.readFile(path.join(root, name)))
          .digest('hex'),
      ])
    )
  )
  const data = await fs.mkdtemp(path.join(output, 'isolated-'))
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
  const providerCalls = []
  const pending = new Map()
  const providerErrors = []
  const makeStream = (res, key) => {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    })
    res.flushHeaders()
    const state = { closed: false, reasoning: '', answer: '' }
    res.on('close', () => {
      state.closed = true
    })
    const frame = (value) => {
      if (!state.closed) res.write(`data: ${JSON.stringify(value)}\n\n`)
    }
    const controls = {
      key,
      state,
      thinking(text) {
        state.reasoning += text
        frame({ choices: [{ delta: { reasoning_content: text } }] })
      },
      answer(text) {
        state.answer += text
        frame({ choices: [{ delta: { content: text } }] })
      },
      tool(name, args) {
        frame({
          choices: [
            {
              delta: {
                tool_calls: [
                  {
                    index: 0,
                    id: `call_${name}_${providerCalls.length}`,
                    type: 'function',
                    function: { name, arguments: JSON.stringify(args) },
                  },
                ],
              },
            },
          ],
        })
      },
      finish() {
        if (state.closed) return
        frame({ choices: [], usage: { prompt_tokens: 5, completion_tokens: 4, total_tokens: 9 } })
        res.end('data: [DONE]\n\n')
        state.closed = true
      },
    }
    pending.set(key, controls)
    return controls
  }
  const provider = http.createServer(async (req, res) => {
    try {
      let body = ''
      for await (const chunk of req) body += chunk
      const payload = JSON.parse(body)
      const question = [...payload.messages].reverse().find((m) => m.role === 'user')?.content
      const original = payload.messages.find((m) => m.role === 'user')?.content
      const name = [MAIN, RESET, STOP, HANDOFF, PROGRESS].find((q) => String(original).includes(q))
      assert.ok(name, `Unexpected local provider request ${String(question).slice(0, 100)}`)
      assert.equal(payload.stream, true)
      providerCalls.push({ name, stream: true, at: Date.now() })
      if (name === PROGRESS) {
        const results = payload.messages.filter((message) => message.role === 'tool')
        if (!results.length) makeStream(res, PROGRESS + ':first')
        else if (results.length === 1) makeStream(res, PROGRESS + ':second')
        else makeStream(res, PROGRESS + ':final')
      } else if (name !== RESET) {
        makeStream(res, name)
      } else {
        const tools = payload.messages.filter((m) => m.role === 'tool')
        const hasList = payload.messages.some(
          (m) =>
            m.role === 'assistant' &&
            m.tool_calls?.some((call) => call.function?.name === 'list_skills')
        )
        const oldDraft = payload.messages.some(
          (m) => m.role === 'assistant' && String(m.content).includes('RESET_OLD_DRAFT')
        )
        if (!tools.length) {
          const stream = makeStream(res, RESET + ':plan')
          stream.tool('update_plan', {
            steps: [
              {
                title: '读取技能目录',
                phase: 'read',
                tool_name: 'list_skills',
                tool_arguments: {},
              },
              { title: '整理并交付说明', phase: 'write' },
            ],
          })
          stream.finish()
        } else if (hasList) {
          makeStream(res, RESET + ':final')
        } else if (oldDraft) {
          const stream = makeStream(res, RESET + ':read')
          stream.tool('list_skills', {})
          stream.finish()
        } else {
          makeStream(res, RESET + ':draft')
        }
      }
    } catch (error) {
      providerErrors.push(error.stack || error.message)
      if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ error: { message: 'Synthetic provider fixture error' } }))
    }
  })
  await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
  const port = await freePort()
  const backend = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
    { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let backendLog = ''
  backend.stdout.on('data', (chunk) => {
    backendLog += chunk
  })
  backend.stderr.on('data', (chunk) => {
    backendLog += chunk
  })
  const base = `http://127.0.0.1:${port}`
  let vite, browser, page
  const errors = []
  const checks = []
  const observerAborts = []
  const request = async (apiPath, body, method = body === undefined ? 'GET' : 'POST') => {
    const response = await fetch(base + '/api' + apiPath, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(15000),
    })
    assert.equal(response.status, 200, `${method} ${apiPath}: ${await response.clone().text()}`)
    return response.json()
  }
  const runs = async (cid) => (await request(`/foreground-runs?conversation_id=${cid}`)).runs
  const history = async (cid) => (await request(`/conversations/${cid}`)).messages
  const lastRun = async (cid) => (await runs(cid)).at(-1)
  const providerControl = async (key) => {
    await until(() => pending.has(key), 'provider stream ' + key)
    return pending.get(key)
  }
  const observe = async (runId, cid) => {
    const abort = new AbortController()
    observerAborts.push(abort)
    const response = await fetch(
      `${base}/api/chat/runs/${runId}/events?conversation_id=${cid}&after=0`,
      { signal: abort.signal }
    )
    assert.equal(response.status, 200)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    return {
      abort,
      async next() {
        while (true) {
          const boundary = buffer.indexOf('\n\n')
          if (boundary >= 0) {
            const block = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            const line = block.split('\n').find((item) => item.startsWith('data:'))
            if (line) return JSON.parse(line.slice(5).trim())
          } else {
            const chunk = await reader.read()
            if (chunk.done) return null
            buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r\n/g, '\n')
          }
        }
      },
    }
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
      'isolated backend startup',
      30000
    )
    const profile = await request('/model-profiles', {
      name: 'Reattach synthetic provider',
      base_url: `http://127.0.0.1:${provider.address().port}/v1`,
      model_id: 'Synthetic-Reattach-Model',
      api_key: 'generated-local-reattach-credential',
      reasoning_effort: '',
    })
    await request('/model-profiles/default', { model_profile_id: profile.id }, 'PUT')
    const conversations = {}
    for (const name of ['A', 'B', 'Reset', 'Stop', 'Handoff', 'Progress']) {
      const item = await request('/conversations', { title: '重入验证 ' + name })
      conversations[name] = item.id
      const options = await request(`/conversation-options/${item.id}`)
      await request(
        `/conversation-options/${item.id}`,
        {
          expected_revision: options.revision,
          model_profile_id: profile.id,
          approval_mode: 'full_access',
          reasoning_effort: '',
        },
        'PUT'
      )
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
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    page = await context.newPage()
    page.setDefaultTimeout(5000)
    page.on('pageerror', (error) => errors.push(error.message))
    const composer = page.locator('textarea.textarea')
    const turn = () => page.locator('.turn').last()
    const answerText = () =>
      turn().locator('.assistant > .markdown-answer').innerText({ timeout: 500 })
    const thinkingText = () => turn().locator('.thinking-body').innerText({ timeout: 500 })
    const readText = async (getter) => {
      try {
        return await getter()
      } catch {
        return ''
      }
    }
    const collapsedProcess = async () => {
      await until(
        () => turn().locator('.run-process-toggle').isVisible(),
        'actual process entrance'
      )
      assert.equal(await turn().locator('.run-process-toggle').count(), 1)
      assert.equal(
        await turn().locator('.run-process-toggle').getAttribute('aria-expanded'),
        'false'
      )
      assert.equal(await turn().locator('.run-process-details').isVisible(), false)
    }
    const openProcess = async () => {
      await collapsedProcess()
      await turn().locator('.run-process-toggle').click()
      assert.equal(await turn().locator('.run-process-details').isVisible(), true)
    }
    const switchTo = async (name) => {
      await page
        .locator('.recent-conversation')
        .filter({ hasText: '重入验证 ' + name })
        .click()
      await until(
        () => new URL(page.url()).searchParams.get('conv') === conversations[name],
        'switch ' + name
      )
      await until(() => composer.isEnabled(), 'composer restored ' + name)
    }
    const open = async (name) => {
      await page.goto(`${ui}/chat?conv=${conversations[name]}`)
      await until(() => composer.isEnabled(), 'composer ready ' + name)
    }
    const submit = async (question) => {
      await composer.fill(question)
      await page.getByRole('button', { name: '发送', exact: true }).click()
    }
    await open('A')
    await submit(MAIN)
    const mainStream = await providerControl(MAIN)
    mainStream.thinking('A_THOUGHT_FIRST ')
    mainStream.answer('A_ANSWER_FIRST ')
    await openProcess()
    await until(
      async () => (await readText(thinkingText)).includes('A_THOUGHT_FIRST'),
      'initial thought'
    )
    await until(
      async () => (await readText(answerText)).includes('A_ANSWER_FIRST'),
      'initial answer'
    )
    const run = await lastRun(conversations.A)
    assert.ok(run?.id)
    await switchTo('B')
    mainStream.thinking('A_THOUGHT_WHILE_AWAY ')
    mainStream.answer('A_ANSWER_WHILE_AWAY ')
    await until(
      async () => (await lastRun(conversations.A))?.partial_answer.includes('A_ANSWER_WHILE_AWAY'),
      'detached output persisted'
    )
    const otherObserver = await observe(run.id, conversations.A)
    const snapshot = await otherObserver.next()
    assert.equal(snapshot.kind, 'snapshot')
    assert.ok(JSON.stringify(snapshot).includes('A_THOUGHT_WHILE_AWAY'))
    const reenteredAt = Date.now()
    await switchTo('A')
    await collapsedProcess()
    await until(
      async () => (await readText(answerText)).includes('A_ANSWER_WHILE_AWAY'),
      'answer visible while process is collapsed'
    )
    await openProcess()
    await until(
      async () => (await readText(thinkingText)).includes('A_THOUGHT_WHILE_AWAY'),
      'replayed thought'
    )
    await until(
      async () => (await readText(answerText)).includes('A_ANSWER_WHILE_AWAY'),
      'replayed answer'
    )
    const replayLatencyMs = Date.now() - reenteredAt
    assert.equal((await history(conversations.A)).length, 0, 'not waiting for a final answer')
    assert.equal(providerCalls.filter((call) => call.name === MAIN).length, 1)
    assert.equal((await readText(answerText)).split('A_ANSWER_FIRST').length - 1, 1)
    assert.equal((await readText(thinkingText)).split('A_THOUGHT_FIRST').length - 1, 1)
    mainStream.thinking('A_THOUGHT_AFTER_RETURN ')
    mainStream.answer('A_ANSWER_AFTER_RETURN ')
    await until(
      async () => (await readText(thinkingText)).includes('A_THOUGHT_AFTER_RETURN'),
      'tail thought before final'
    )
    await until(
      async () => (await readText(answerText)).includes('A_ANSWER_AFTER_RETURN'),
      'tail answer before final'
    )
    assert.equal((await history(conversations.A)).length, 0)
    await page.screenshot({
      path: path.join(output, 'reattached-after-return.png'),
      fullPage: true,
    })
    checks.push('switch A/B/A replays thought and answer, then streams more before final')
    await page.reload()
    await openProcess()
    await until(
      async () => (await readText(thinkingText)).includes('A_THOUGHT_AFTER_RETURN'),
      'refresh restores thoughts'
    )
    await until(
      async () => (await readText(answerText)).includes('A_ANSWER_AFTER_RETURN'),
      'refresh restores answer'
    )
    mainStream.thinking('A_THOUGHT_AFTER_RELOAD ')
    mainStream.answer('A_ANSWER_AFTER_RELOAD ')
    await until(
      async () => (await readText(answerText)).includes('A_ANSWER_AFTER_RELOAD'),
      'refresh continues live output'
    )
    await until(
      async () => (await readText(thinkingText)).includes('A_THOUGHT_AFTER_RELOAD'),
      'refresh continues live thought'
    )
    assert.equal(providerCalls.filter((call) => call.name === MAIN).length, 1)
    await page.screenshot({
      path: path.join(output, 'reattached-before-final.png'),
      fullPage: true,
    })
    checks.push('page refresh resumes live observation without another model request')
    otherObserver.abort.abort()
    mainStream.finish()
    await until(async () => (await history(conversations.A)).length === 1, 'one final QA committed')
    await turn().locator('.answer-brand').waitFor()
    const finalA = (await history(conversations.A))[0]
    assert.equal(finalA.answer, mainStream.state.answer)
    assert.equal(providerCalls.filter((call) => call.name === MAIN).length, 1)
    assert.equal(
      await page.locator('.turn').count(),
      1,
      'replayed and final answer are the same turn'
    )
    checks.push('simultaneous observer does not start another inference or duplicate final QA')

    // Produce a real answer_reset: a synthetic model first registers a read
    // step, prematurely answers, then actually performs the missing safe read.
    await open('Reset')
    await submit(RESET)
    const draft = await providerControl(RESET + ':draft')
    draft.thinking('RESET_DRAFT_THOUGHT ')
    draft.answer('RESET_OLD_DRAFT ')
    await until(
      async () => (await readText(answerText)).includes('RESET_OLD_DRAFT'),
      'old draft visible'
    )
    await switchTo('B')
    draft.finish()
    const replacement = await providerControl(RESET + ':final')
    replacement.thinking('RESET_REPLACEMENT_THOUGHT ')
    replacement.answer('RESET_NEW_ANSWER ')
    await until(
      async () => (await lastRun(conversations.Reset))?.partial_answer.includes('RESET_NEW_ANSWER'),
      'replacement snapshot persisted'
    )
    await switchTo('Reset')
    await openProcess()
    await until(
      async () => (await readText(answerText)).includes('RESET_NEW_ANSWER'),
      'replacement replayed'
    )
    assert.ok(
      !(await readText(answerText)).includes('RESET_OLD_DRAFT'),
      'answer_reset clears draft across re-entry'
    )
    assert.ok((await readText(thinkingText)).includes('RESET_REPLACEMENT_THOUGHT'))
    await until(
      async () =>
        (await turn().locator('.public-tool').allTextContents()).join('\n').includes('查看技能库'),
      'tool history restored'
    )
    assert.ok(
      (await lastRun(conversations.Reset)).display_snapshot.events.some(
        (event) => event.kind === 'tool_call' && event.name === 'list_skills'
      ),
      'safe read tool stored in display snapshot'
    )
    await page.screenshot({ path: path.join(output, 'reattached-after-reset.png'), fullPage: true })
    const resetCalls = providerCalls.filter((call) => call.name === RESET).length
    replacement.finish()
    await until(async () => (await history(conversations.Reset)).length === 1, 'reset one final QA')
    assert.equal((await history(conversations.Reset))[0].answer, 'RESET_NEW_ANSWER ')
    assert.equal(providerCalls.filter((call) => call.name === RESET).length, resetCalls)
    checks.push(
      'answer_reset and tool progress survive detached generation without stale draft concatenation'
    )

    // Exercise actual provider content+tool_calls. These public preambles are
    // promoted to commentary by the executor, not fabricated frontend events.
    await open('Progress')
    await submit(PROGRESS)
    const progressFirst = await providerControl(PROGRESS + ':first')
    progressFirst.thinking('RAW_PROGRESS_FIRST ')
    progressFirst.answer('PUBLIC_PROGRESS_FIRST ')
    progressFirst.tool('list_skills', {})
    progressFirst.finish()
    const progressSecond = await providerControl(PROGRESS + ':second')
    await until(
      async () => (await turn().locator('.public-commentary').count()) === 1,
      'provider preamble promoted to public progress'
    )
    await collapsedProcess()
    assert.equal(await turn().locator('.public-commentary').isVisible(), true)
    assert.equal(await turn().locator('.answer-draft').count(), 0)
    await switchTo('B')
    progressSecond.thinking('RAW_PROGRESS_SECOND ')
    progressSecond.answer('PUBLIC_PROGRESS_SECOND ')
    progressSecond.tool('read_conversation_history', { query: 'synthetic-progress' })
    progressSecond.finish()
    const progressFinal = await providerControl(PROGRESS + ':final')
    progressFinal.thinking('RAW_PROGRESS_FINAL ')
    progressFinal.answer('FINAL_PROGRESS_FIRST ')
    await until(
      async () =>
        (await lastRun(conversations.Progress))?.partial_answer.includes('FINAL_PROGRESS_FIRST'),
      'progress final draft persisted while detached'
    )
    const progressOrder = async () =>
      turn()
        .locator('.public-commentary, .public-tool')
        .evaluateAll((items) =>
          items.map((item) =>
            item.classList.contains('public-commentary') ? 'commentary' : 'tool'
          )
        )
    const assertPublicProgress = async () => {
      await until(
        async () => (await turn().locator('.public-tool').count()) === 2,
        'two public tool rows restored'
      )
      assert.deepEqual(await progressOrder(), ['commentary', 'tool', 'commentary', 'tool'])
      for (const item of await turn().locator('.public-commentary, .public-tool').all())
        assert.equal(
          await item.isVisible(),
          true,
          'progress and tools stay public while raw reasoning is collapsed'
        )
      for (const token of ['PUBLIC_PROGRESS_FIRST', 'PUBLIC_PROGRESS_SECOND'])
        assert.equal(
          (await turn().innerText()).split(token).length - 1,
          1,
          'one public preamble per model response'
        )
    }
    await switchTo('Progress')
    await collapsedProcess()
    await assertPublicProgress()
    await until(
      async () => (await readText(answerText)).includes('FINAL_PROGRESS_FIRST'),
      'final output visible after public progress'
    )
    assert.equal(
      (await history(conversations.Progress)).length,
      0,
      'public progress restores before final completion'
    )
    await openProcess()
    const rawReasoning = await readText(thinkingText)
    for (const token of ['RAW_PROGRESS_FIRST', 'RAW_PROGRESS_SECOND', 'RAW_PROGRESS_FINAL'])
      assert.ok(rawReasoning.includes(token))
    assert.ok(
      !rawReasoning.includes('PUBLIC_PROGRESS_'),
      'public preambles do not masquerade as raw reasoning'
    )
    await turn().locator('.run-process-toggle').click()
    progressFinal.thinking('RAW_PROGRESS_AFTER_RETURN ')
    progressFinal.answer('FINAL_PROGRESS_AFTER_RETURN ')
    await until(
      async () => (await readText(answerText)).includes('FINAL_PROGRESS_AFTER_RETURN'),
      'final output continues live after progress replay'
    )
    await assertPublicProgress()
    await page.reload()
    await collapsedProcess()
    await assertPublicProgress()
    await until(
      async () => (await readText(answerText)).includes('FINAL_PROGRESS_AFTER_RETURN'),
      'refresh retains progress and current final draft'
    )
    progressFinal.answer('FINAL_PROGRESS_AFTER_RELOAD ')
    await until(
      async () => (await readText(answerText)).includes('FINAL_PROGRESS_AFTER_RELOAD'),
      'refresh continues final draft stream'
    )
    assert.equal(
      providerCalls.filter((call) => call.name === PROGRESS).length,
      3,
      'observation never repeats model/tool rounds'
    )
    await turn().scrollIntoViewIfNeeded()
    await page.screenshot({
      path: path.join(output, 'reattached-public-progress.png'),
      fullPage: true,
    })
    progressFinal.finish()
    await until(
      async () => (await history(conversations.Progress)).length === 1,
      'public progress final QA saved once'
    )
    await turn().locator('.answer-final').waitFor()
    await collapsedProcess()
    await assertPublicProgress()
    const progressQA = (await history(conversations.Progress))[0]
    assert.equal(progressQA.answer, progressFinal.state.answer)
    assert.equal(progressQA.trace.filter((event) => event.kind === 'commentary').length, 2)
    assert.equal(providerCalls.filter((call) => call.name === PROGRESS).length, 3)
    checks.push(
      'real provider preambles and tools replay publicly in order across re-entry and refresh, continue live, and never duplicate raw reasoning or model calls'
    )

    // Pin the real HTTP handoff: a list request takes a running snapshot, then
    // generation commits its QA before navigation fetches that conversation.
    // Delay observers so a duplicate cannot disappear before the assertion.
    await open('Handoff')
    await submit(HANDOFF)
    const handoff = await providerControl(HANDOFF)
    handoff.thinking('HANDOFF_THOUGHT ')
    handoff.answer('HANDOFF_FINAL_ANSWER ')
    await until(
      async () => (await readText(answerText)).includes('HANDOFF_FINAL_ANSWER'),
      'handoff initial stream'
    )
    const handoffRun = await lastRun(conversations.Handoff)
    await switchTo('B')
    let handoffListCaptured = false
    let handoffEvidence
    let observerHeld = false
    let observerReleased = false
    let releaseHandoffObserver
    const observerGate = new Promise((resolve) => {
      releaseHandoffObserver = resolve
    })
    const observerPattern = `**/api/chat/runs/${handoffRun.id}/events?**`
    const observerHandler = async (route) => {
      observerHeld = true
      await observerGate
      observerReleased = true
      await route.continue()
    }
    const listPattern = '**/api/foreground-runs?**'
    const listHandler = async (route) => {
      const query = new URL(route.request().url()).searchParams
      if (handoffListCaptured || query.get('conversation_id') !== conversations.Handoff) {
        await route.continue()
        return
      }
      const response = await route.fetch()
      const body = await response.json()
      const staleRun = body.runs.find((run) => run.id === handoffRun.id)
      assert.equal(staleRun?.status, 'running', 'captured an actual running foreground response')
      handoffListCaptured = true
      handoff.finish()
      await until(
        async () => (await history(conversations.Handoff)).length === 1,
        'handoff QA commits before history read'
      )
      await route.fulfill({ response })
    }
    await page.route(observerPattern, observerHandler)
    await page.route(listPattern, listHandler)
    try {
      await switchTo('Handoff')
      assert.equal(handoffListCaptured, true)
      const handoffQA = (await history(conversations.Handoff))[0]
      assert.equal(handoffQA.answer, 'HANDOFF_FINAL_ANSWER ')
      assert.equal(
        await page.locator('.turn').count(),
        1,
        'one turn before delayed observer receives its snapshot'
      )
      assert.equal(
        await page.locator('.user-bubble').filter({ hasText: HANDOFF }).count(),
        1,
        'one user question across list/history handoff'
      )
      assert.equal(
        await page.locator('.markdown-answer').count(),
        1,
        'one answer across list/history handoff'
      )
      assert.equal(await answerText(), 'HANDOFF_FINAL_ANSWER')
      const observerFinished = observerHeld
        ? page.waitForEvent('requestfinished', {
            predicate: (request) =>
              request.url().includes(`/api/chat/runs/${handoffRun.id}/events?`),
          })
        : null
      releaseHandoffObserver()
      if (observerHeld) {
        await until(() => observerReleased, 'handoff observer released')
        await observerFinished
        await page.evaluate(() => new Promise(requestAnimationFrame))
      }
      await until(
        async () => await turn().locator('.answer-brand').isVisible(),
        'handoff answer stays final'
      )
      assert.equal(await page.locator('.turn').count(), 1)
      assert.equal(await page.locator('.user-bubble').filter({ hasText: HANDOFF }).count(), 1)
      assert.equal(await page.locator('.markdown-answer').count(), 1)
      assert.equal(providerCalls.filter((call) => call.name === HANDOFF).length, 1)
      assert.equal((await history(conversations.Handoff)).length, 1)
      handoffEvidence = {
        actual_running_list_captured: handoffListCaptured,
        observer_request_started: observerHeld,
        final_qa_count: 1,
        displayed_turn_count: 1,
        displayed_question_count: 1,
        displayed_answer_count: 1,
        model_inferences: 1,
      }
      await page.screenshot({
        path: path.join(output, 'reattached-completion-handoff.png'),
        fullPage: true,
      })
      checks.push(
        'completion between running-list snapshot and history load displays one question/answer without relying on observer arrival or another inference'
      )
    } finally {
      releaseHandoffObserver()
      await page.unroute(listPattern, listHandler)
      await page.unroute(observerPattern, observerHandler)
    }

    await open('Stop')
    await submit(STOP)
    const stopping = await providerControl(STOP)
    stopping.thinking('STOP_INITIAL_THOUGHT ')
    stopping.answer('STOP_INITIAL_ANSWER ')
    await until(
      async () => (await readText(answerText)).includes('STOP_INITIAL_ANSWER'),
      'stop partial'
    )
    const stopRun = await lastRun(conversations.Stop)
    await switchTo('B')
    await switchTo('Stop')
    await collapsedProcess()
    await until(
      async () => (await readText(answerText)).includes('STOP_INITIAL_ANSWER'),
      'stop reattached'
    )
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    // Release the local provider so cooperative cancellation can observe the
    // stop boundary. The response must not be delivered as a completed answer.
    stopping.answer('STOP_MUST_NOT_APPEAR_AFTER_CANCEL ')
    stopping.finish()
    await until(
      async () => (await lastRun(conversations.Stop))?.status === 'cancelled',
      'explicit stop after reattach'
    )
    assert.equal((await history(conversations.Stop)).length, 0)
    assert.equal(providerCalls.filter((call) => call.name === STOP).length, 1)
    assert.equal((await request(`/chat/runs/${stopRun.id}/cancel`, {}, 'POST')).status, 'cancelled')
    checks.push('explicit stop after re-entry cancels the existing execution and saves no final QA')
    await switchTo('B')
    assert.equal(await page.locator('.turn').count(), 0)
    assert.equal((await history(conversations.B)).length, 0)
    checks.push('conversation B never receives conversation A thinking, text or final records')
    assert.deepEqual(errors, [])
    assert.deepEqual(providerErrors, [])
    const report = {
      ok: true,
      verified_at: new Date().toISOString(),
      source_sha256: sourceHashes,
      data_isolated: true,
      real_provider_calls: 0,
      synthetic_provider_calls: providerCalls.length,
      main_inferences: 1,
      reset_inferences: resetCalls,
      stop_inferences: 1,
      handoff_inferences: 1,
      progress_inferences: 3,
      replay_latency_ms: replayLatencyMs,
      completion_handoff: handoffEvidence,
      checks,
      screenshots: [
        'reattached-after-return.png',
        'reattached-before-final.png',
        'reattached-after-reset.png',
        'reattached-public-progress.png',
        'reattached-completion-handoff.png',
      ],
    }
    await fs.rm(path.join(output, 'failure.png'), { force: true })
    await fs.rm(path.join(output, 'failure.txt'), { force: true })
    await fs.writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(report, null, 2))
  } catch (error) {
    if (page)
      await page
        .screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
        .catch(() => {})
    if (page)
      await fs
        .writeFile(path.join(output, 'failure.txt'), await page.locator('body').innerText())
        .catch(() => {})
    throw error
  } finally {
    for (const abort of observerAborts) abort.abort()
    for (const item of pending.values()) item.finish()
    await browser?.close()
    await vite?.close()
    backend.kill()
    await new Promise((resolve) =>
      backend.exitCode !== null ? resolve() : backend.once('exit', resolve)
    )
    provider.closeAllConnections()
    await new Promise((resolve) => provider.close(resolve))
    await fs.writeFile(path.join(output, 'backend.log'), backendLog)
    // Keep isolated test data for reproducible inspection; it contains only
    // synthetic content/credentials and is never copied into a release.
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
