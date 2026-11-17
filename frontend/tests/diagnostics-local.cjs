// Real Vue + isolated HTTP; no real model credentials or public-provider calls.
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
  const deadline = Date.now() + 30000
  while (!(await check())) {
    assert.ok(Date.now() < deadline, label)
    await pause(60)
  }
}
async function main() {
  const output = path.join(root, 'verification/diagnostics-local')
  const runtime = path.join(root, 'verification/runtime')
  await fs.mkdir(output, { recursive: true })
  await fs.mkdir(runtime, { recursive: true })
  const data = await fs.mkdtemp(path.join(runtime, 'diagnostics-'))
  const files = [
    'backend/app/diagnostic_probes.py',
    'backend/app/diagnostics.py',
    'backend/app/api/diagnostics.py',
    'backend/app/pipeline/embeddings.py',
    'frontend/src/components/DiagnosticsPanel.vue',
    'frontend/src/diagnosticApi.ts',
  ]
  async function hashes() {
    const result = {}
    for (const name of files)
      result[name] = crypto
        .createHash('sha256')
        .update(await fs.readFile(path.join(root, name)))
        .digest('hex')
    return result
  }
  const before = await hashes()
  const calls = []
  const provider = http.createServer(async (req, res) => {
    const chunks = []
    for await (const chunk of req) chunks.push(chunk)
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    calls.push({ path: req.url, model: body.model, body })
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(
      JSON.stringify(
        req.url.endsWith('/embeddings')
          ? {
              data: [{ index: 0, embedding: [0.2, 0.3] }],
              usage: { prompt_tokens: 2, total_tokens: 2 },
            }
          : {
              choices: [{ message: { role: 'assistant', content: 'OK' }, finish_reason: 'stop' }],
              usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
            }
      )
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
        LLM_DEMO_MODE: '1',
        EMBEDDING_BASE_URL: providerBase,
        EMBEDDING_API_KEY: 'synthetic-local-only',
        EMBEDDING_MODEL: 'Embed-CaSe',
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
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const errors = [],
    external = [],
    checks = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/*', async (route) => {
    const url = route.request().url()
    if (url.startsWith('http') && !url.startsWith(base)) {
      external.push(url)
      await route.abort()
      return
    }
    await route.continue()
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
  let cid
  page.on('response', (response) => {
    const match = response.url().match(/\/api\/conversation-options\/([\w-]+)$/)
    if (match) cid = match[1]
  })
  try {
    await until(async () => {
      if (server.exitCode !== null) throw Error(log)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'API startup')
    const profile = await api('/model-profiles', {
      name: 'Diagnostic local',
      base_url: providerBase,
      model_id: 'Chat-CaSe',
      api_key: '',
    })
    await api('/model-profiles/default', { model_profile_id: profile.id }, 'PUT')
    await page.goto(base + '/chat')
    await until(() => Boolean(cid), 'observe conversation')
    const composer = page.locator('textarea').first()
    await composer.fill('/debug')
    await composer.press('Enter')
    const panel = page.getByRole('dialog', { name: '配置诊断' })
    await panel.getByRole('button', { name: '测试模型接口（可能收费）', exact: true }).waitFor()
    assert.equal(calls.length, 0)
    await panel.getByRole('button', { name: '重新运行本地检查' }).click()
    await panel.getByText('Chat-CaSe', { exact: false }).first().waitFor()
    assert.equal(calls.length, 0)
    checks.push('Opening and refreshing local inspection sends zero provider requests')
    await panel.getByRole('button', { name: '测试模型接口（可能收费）', exact: true }).click()
    await panel.getByText('模型基础对话：测试成功', { exact: true }).waitFor()
    assert.equal(calls.length, 1)
    assert.equal(calls[0].model, 'Chat-CaSe')
    assert.deepEqual(calls[0].body.messages, [{ role: 'user', content: 'Reply with OK.' }])
    checks.push('Explicit chat test sends only fixed prompt and displays one durable receipt')
    await panel.getByRole('button', { name: '测试 Embedding（可能收费）', exact: true }).click()
    await panel.getByText('Embedding：测试成功', { exact: true }).waitFor()
    assert.equal(calls.length, 2)
    assert.equal(calls[1].model, 'Embed-CaSe')
    checks.push('Explicit embedding test uses actual shared adapter and displays its receipt')
    await api(
      `/model-profiles/${profile.id}`,
      { expected_version: profile.version, model_id: 'Changed-CaSe' },
      'PUT'
    )
    await panel.getByRole('button', { name: '测试模型接口（可能收费）', exact: true }).click()
    await panel.getByRole('alert').waitFor()
    assert.ok((await panel.getByRole('alert').innerText()).includes('配置已变化'))
    assert.equal(calls.length, 2)
    checks.push('Changed configuration rejects stale target before HTTP')
    await panel.getByRole('button', { name: '重新运行本地检查' }).click()
    await panel.getByText('Changed-CaSe', { exact: false }).first().waitFor()
    await page.setViewportSize({ width: 760, height: 850 })
    assert.equal(await panel.evaluate((el) => el.scrollWidth <= el.clientWidth + 1), true)
    await page.screenshot({ path: path.join(output, 'diagnostic-narrow.png'), fullPage: true })
    await panel.evaluate((el) => {
      el.scrollTop = el.scrollHeight
    })
    await page.screenshot({ path: path.join(output, 'diagnostic-actions.png'), fullPage: true })
    await panel.getByRole('button', { name: '关闭诊断' }).click()
    assert.equal(await panel.count(), 0)
    checks.push('Narrow panel fits and closes without sending requests')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    assert.deepEqual(await hashes(), before)
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          ok: true,
          checks,
          errors,
          external,
          synthetic_provider_calls: calls.length,
          real_provider_calls: 0,
          sourceHashes: before,
          calls: calls.map(({ path, model }) => ({ path, model })),
          screenshot: 'diagnostic-narrow.png',
        },
        null,
        2
      )
    )
    console.log(
      `PASS diagnostics UI: ${checks.length} checks, ${calls.length} local synthetic calls`
    )
  } catch (error) {
    await page
      .screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
      .catch(() => {})
    await fs.writeFile(
      path.join(output, 'failure.json'),
      JSON.stringify({ error: String(error), errors, external, log: log.slice(-3000) }, null, 2)
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
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
