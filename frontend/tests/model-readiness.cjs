// New-install readiness and explicit demo selection against an isolated real API.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const http = require('node:http')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label) {
  const expires = Date.now() + 25000
  while (!(await check())) {
    assert.ok(Date.now() < expires, `Timed out: ${label}`)
    await pause(50)
  }
}
async function main() {
  const output = path.join(root, 'verification/model-readiness')
  const runtime = path.join(root, 'verification/runtime')
  await fs.mkdir(output, { recursive: true })
  await fs.mkdir(runtime, { recursive: true })
  const data = await fs.mkdtemp(path.join(runtime, 'model-readiness-'))
  const calls = []
  const provider = http.createServer(async (req, res) => {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
    calls.push({
      path: req.url,
      authPresent: Boolean(req.headers.authorization),
      model: body.model,
    })
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(
      JSON.stringify({
        choices: [{ message: { role: 'assistant', content: 'ok' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
      })
    )
  })
  await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
  const providerBase = `http://127.0.0.1:${provider.address().port}/v1`
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  const base = `http://127.0.0.1:${port}`
  const server = spawn(
    path.join(root, 'backend/.venv/Scripts/python.exe'),
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
    {
      cwd: path.join(root, 'backend'),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        DATA_DIR: data,
        LLM_API_KEY: '',
        LLM_DEMO_MODE: '0',
        LLM_MODEL: 'deepseek-flash',
        LLM_BASE_URL: 'https://api.deepseek.com/v1',
        EMBEDDING_API_KEY: '',
        REDIS_URL: '',
        DESKTOP_NOTIFICATIONS: 'false',
      },
    }
  )
  let log = ''
  server.stdout.on('data', (chunk) => {
    log += chunk
  })
  server.stderr.on('data', (chunk) => {
    log += chunk
  })
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const pageErrors = []
  let conversationId = ''
  page.on('pageerror', (error) => pageErrors.push(error.message))
  page.on('request', (request) => {
    const match = new URL(request.url()).pathname.match(/^\/api\/conversation-options\/([^/]+)$/)
    if (request.method() === 'GET' && match) conversationId = decodeURIComponent(match[1])
  })
  const api = async (route, body, method = 'POST') => {
    const response = await fetch(
      base + '/api' + route,
      body === undefined
        ? undefined
        : {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          }
    )
    assert.equal(response.status, 200, await response.clone().text())
    return response.json()
  }
  try {
    await until(async () => {
      if (server.exitCode !== null) throw Error(log)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'isolated API startup')
    const initial = await api('/model-profiles')
    const chosen = initial.profiles.find((profile) => profile.id === initial.default_profile_id)
    assert.notEqual(
      chosen.id,
      'legacy-default',
      'new install creates an editable independent profile'
    )
    assert.equal(chosen.model_id, 'deepseek-flash')
    assert.equal(chosen.mode, 'unconfigured')
    assert.equal(chosen.api_key_set, false)
    await page.goto(base + '/chat')
    await page.getByRole('button', { name: '配置当前模型', exact: true }).waitFor()
    assert.ok(conversationId, 'observe the conversation created by the actual first-use UI')
    const composer = page.locator('textarea').first()
    await composer.fill('保留尚未发送的研究草稿')
    assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true)
    await page.screenshot({ path: path.join(output, 'new-install-pending.png'), fullPage: true })
    await page.getByRole('button', { name: '配置当前模型', exact: true }).click()
    const editor = page.locator('.profile-editor')
    await editor.waitFor()
    assert.equal(
      await editor.getByLabel('接口地址', { exact: true }).inputValue(),
      'https://api.deepseek.com/v1'
    )
    assert.equal(await editor.getByLabel('模型 ID', { exact: true }).inputValue(), 'deepseek-flash')
    await editor.getByRole('button', { name: '取消', exact: true }).click()
    assert.equal(await composer.inputValue(), '保留尚未发送的研究草稿')
    assert.equal(calls.length, 0, 'opening and cancelling configuration never probes')
    await page.getByRole('button', { name: '配置当前模型', exact: true }).click()
    await editor.getByLabel('配置名称', { exact: true }).fill('本机测试模型')
    await editor.getByLabel('接口地址', { exact: true }).fill(providerBase)
    await editor.getByLabel('模型 ID', { exact: true }).fill('Local-CaSe')
    await editor.getByRole('button', { name: '保存模型', exact: true }).click()
    await until(
      async () => !(await page.getByRole('button', { name: '发送', exact: true }).isDisabled()),
      'keyless local model ready'
    )
    assert.equal(await composer.inputValue(), '保留尚未发送的研究草稿')
    assert.equal(calls.length, 0, 'save does not send an implicit request')
    const local = await api(`/conversation-options/${conversationId}`)
    assert.equal(local.effective_model.mode, 'api')
    assert.equal(local.effective_model.api_key_set, false)
    const probe = await api(`/model-profiles/${chosen.id}/test`, {})
    assert.equal(probe.ok, true)
    assert.deepEqual(calls, [
      { path: '/v1/chat/completions', authPresent: false, model: 'Local-CaSe' },
    ])
    await page.getByRole('button', { name: '选择会话模型', exact: true }).click()
    await page
      .locator('.conversation-model-menu')
      .getByRole('button', { name: /离线演示（示例响应）/ })
      .click()
    await until(
      async () =>
        (await api(`/conversation-options/${conversationId}`)).model_profile_id === 'offline-demo',
      'explicit demo saved'
    )
    assert.equal(await composer.inputValue(), '保留尚未发送的研究草稿')
    assert.equal(
      (await api('/conversation-options/c_other')).model_profile_id,
      chosen.id,
      'demo selection remains conversation-local'
    )
    await page.reload()
    await until(
      async () =>
        (
          await page.getByRole('button', { name: '选择会话模型', exact: true }).textContent()
        ).includes('离线演示'),
      'explicit demo persists'
    )
    await page.screenshot({ path: path.join(output, 'explicit-demo-selected.png'), fullPage: true })
    assert.equal(calls.length, 1)
    assert.deepEqual(pageErrors, [])
    const sourceHashes = {}
    for (const name of [
      'backend/app/config.py',
      'backend/app/model_profiles.py',
      'backend/app/llm.py',
      'backend/app/model_readiness.py',
      'backend/app/main.py',
      'frontend/src/modelStatus.ts',
      'frontend/src/components/ConversationControls.vue',
      'frontend/src/components/ModelProfileEditor.vue',
    ]) {
      sourceHashes[name] = crypto
        .createHash('sha256')
        .update(await fs.readFile(path.join(root, name)))
        .digest('hex')
    }
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          ok: true,
          scenarios: [
            'empty install editable pending default',
            'no implicit mock or request',
            'configuration defaults and draft preserved',
            'keyless local API probe',
            'explicit conversation-local demo selection and reload',
          ],
          real_provider_calls: 0,
          synthetic_provider_calls: calls.length,
          calls,
          pageErrors,
          sourceHashes,
          screenshots: ['new-install-pending.png', 'explicit-demo-selected.png'],
          data_directory: data,
        },
        null,
        2
      )
    )
    console.log('PASS model readiness: real UI and isolated HTTP; no real-provider requests')
  } catch (error) {
    await page
      .screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      .catch(() => {})
    await fs.writeFile(
      path.join(output, 'failure.json'),
      JSON.stringify(
        {
          error: String(error),
          pageErrors,
          conversationId,
          visible_text: await page
            .locator('body')
            .innerText()
            .catch(() => ''),
        },
        null,
        2
      )
    )
    throw error
  } finally {
    await browser.close()
    server.kill()
    await new Promise((resolve) =>
      server.exitCode !== null ? resolve() : server.once('exit', resolve)
    )
    provider.closeAllConnections()
    await new Promise((resolve) => provider.close(resolve))
    // Retain isolated fixtures for inspection; never delete or touch user data.
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
