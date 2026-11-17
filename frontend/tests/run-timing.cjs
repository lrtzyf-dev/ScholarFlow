// Real Vue + isolated FastAPI, seeded monotonic duration goldens, no providers.
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
async function freePort() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}
async function main() {
  const output = path.join(root, 'verification/run-timing/ui')
  await fs.mkdir(output, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    LLM_DEMO_MODE: '1',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  const python = path.join(root, 'backend/.venv/Scripts/python.exe')
  const seed = `from app.db import init_db,SessionLocal
init_db()
from app.models import Conversation
from app.runtime_models import RuntimeRun
from app import run_timing as t,run_lifecycle as l
from app.resource_budget import create_budget
from app.timeutil import utcnow
from datetime import timedelta
from types import SimpleNamespace
clock=[utcnow(),100.]
t.utcnow=lambda:clock[0]
t.time=SimpleNamespace(monotonic=lambda:clock[1])
l.utcnow=t.utcnow
def advance(n):
 clock[0]+=timedelta(seconds=n);clock[1]+=n
d=SessionLocal();d.add(Conversation(id='timing-ui',title='合成耗时验收'));d.commit()
a,_=l.admit_attempt(d,'ui-timing','timing-ui','合成耗时验收');d.commit()
advance(2);l.save_checkpoint(d,'ui-timing',{'attempt_id':a.id,'writer_token':a.writer_token,'phase':'model'});d.commit()
q1=t.begin_request({'attempt_id':a.id},'ui-timing','chat');advance(3);t.request_boundary(q1,'acquired');t.request_boundary(q1,'bind','ui-call-one')
advance(1);q2=t.begin_request({'attempt_id':a.id},'ui-timing','subtask');t.request_boundary(q2,'acquired');t.request_boundary(q2,'bind','ui-call-two')
advance(3);t.request_boundary(q1,'closed');advance(1);t.request_boundary(q2,'closed')
t.phase(d,'ui-timing',a.id,a.writer_token,'approval_wait');d.commit();advance(4)
t.phase(d,'ui-timing',a.id,a.writer_token,'run_active');d.commit();advance(1)
l.finish_attempt(d,'ui-timing',a.id,a.writer_token,'paused');d.commit();advance(5);t.tick()
l.finish_attempt(d,'ui-timing',a.id,a.writer_token,'done');d.commit()
d.add(RuntimeRun(id='legacy-timing-ui',kind='foreground',conversation_id='timing-ui',status='ended'));d.commit()
b,_=l.admit_attempt(d,'clock-timing-ui','timing-ui','合成调时验收');d.commit()
l.save_checkpoint(d,'clock-timing-ui',{'attempt_id':b.id,'writer_token':b.writer_token,'phase':'model'});d.commit()
advance(2);clock[0]+=timedelta(hours=1)
l.finish_attempt(d,'clock-timing-ui',b.id,b.writer_token,'done');d.commit();d.close()
create_budget(owner_run_id='ui-timing')
`
  const seedFile = path.join(isolated, 'seed.py')
  await fs.writeFile(seedFile, seed)
  let backend,
    browser,
    vite,
    page,
    backendLog = ''
  const errors = [],
    external = [],
    checks = []
  const runPython = (args) =>
    new Promise((resolve, reject) => {
      const child = spawn(python, args, { cwd: path.join(root, 'backend'), env, windowsHide: true })
      let log = ''
      child.stdout.on('data', (v) => (log += v))
      child.stderr.on('data', (v) => (log += v))
      child.on('exit', (code) => (code === 0 ? resolve(log) : reject(Error(log))))
    })
  try {
    await runPython([
      '-c',
      `exec(compile(open(${JSON.stringify(seedFile)},encoding='utf-8').read(),'seed','exec'))`,
    ])
    const port = await freePort(),
      base = `http://127.0.0.1:${port}`
    const start = async () => {
      backend = spawn(
        python,
        ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
        {
          cwd: path.join(root, 'backend'),
          env,
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'pipe'],
        }
      )
      backend.stdout.on('data', (v) => (backendLog += v))
      backend.stderr.on('data', (v) => (backendLog += v))
      await until(async () => {
        if (backend.exitCode !== null) throw Error(backendLog)
        try {
          return (await fetch(base + '/api/health')).ok
        } catch {
          return false
        }
      }, 'backend startup')
    }
    const stop = async () => {
      const child = backend
      if (!child || child.exitCode !== null) return
      await new Promise((resolve) => {
        child.once('exit', resolve)
        child.kill()
      })
      backend = undefined
    }
    await start()
    const api = async () => {
      const response = await fetch(base + '/api/conversation-resources/timing-ui')
      assert.equal(response.status, 200)
      return response.json()
    }
    const original = await api(),
      golden = original.timings.find((r) => r.run_id === 'ui-timing')
    assert.equal(golden.elapsed_wall_seconds, 20)
    assert.equal(golden.request_execution_sum_seconds, 8)
    assert.equal(golden.request_execution_wall_seconds, 5)
    assert.equal(golden.approval_wait_seconds, 4)
    assert.equal(golden.user_wait_seconds, 5)
    assert.equal(golden.queue_wall_seconds, 5)
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
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    page = await context.newPage()
    page.on('pageerror', (e) => errors.push(e.message))
    page.setDefaultTimeout(20000)
    const open = async () => {
      await page.getByRole('button', { name: '任务预算', exact: true }).click()
      await page.locator('summary').filter({ hasText: '任务耗时明细' }).click()
    }
    await page.goto(ui + '/chat?conv=timing-ui')
    await open()
    const panel = page.getByRole('region', { name: '任务耗时明细', exact: true })
    const value = async (label) =>
      panel
        .locator('dl div')
        .filter({ has: page.locator('dt').filter({ hasText: new RegExp('^' + label + '$') }) })
        .locator('dd')
        .innerText()
    assert.equal(await value('任务经过时间'), '20.00 秒')
    assert.equal(await value('请求执行窗口累计'), '8.00 秒')
    assert.equal(await value('请求执行窗口实际覆盖'), '5.00 秒')
    await panel.screenshot({ path: path.join(output, 'separate-request-sum-and-task-wall.png') })
    checks.push({
      name: 'real_Vue_persisted_queue_wait_request_sum_and_wall_goldens',
      passed: true,
    })
    const selection = panel.getByLabel('查看耗时的任务', { exact: true })
    await selection.selectOption('legacy-timing-ui')
    await panel.getByText('此历史任务尚无分段耗时记录', { exact: true }).waitFor()
    await selection.selectOption('ui-timing')
    checks.push({ name: 'legacy_run_shows_unavailable_without_inference', passed: true })
    await selection.selectOption('clock-timing-ui')
    assert.equal(await value('任务经过时间'), '时钟变化，无法可靠对应')
    assert.equal(await value('离线或尚未确认'), '时钟变化，无法可靠对应')
    await panel.getByRole('status').filter({ hasText: '检测到系统时钟调整' }).waitFor()
    await panel.screenshot({ path: path.join(output, 'clock-uncertain-without-false-offline.png') })
    await selection.selectOption('ui-timing')
    checks.push({
      name: 'clock_adjustment_shows_uncertain_wall_without_false_offline_duration',
      passed: true,
    })
    await stop()
    await start()
    await page.reload()
    await open()
    assert.equal(await value('任务经过时间'), '20.00 秒')
    const after = (await api()).timings.find((r) => r.run_id === 'ui-timing')
    assert.equal(after.request_execution_sum_seconds, golden.request_execution_sum_seconds)
    checks.push({ name: 'actual_backend_restart_keeps_closed_durations_unchanged', passed: true })
    await page.setViewportSize({ width: 760, height: 950 })
    await selection.focus()
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)
    )
    await panel.screenshot({ path: path.join(output, 'narrow-keyboard-timing.png') })
    checks.push({ name: '760px_keyboard_selection_no_document_overflow', passed: true })
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
          user_credentials_used: false,
          scope:
            'Real Vue, HTTP and backend restart against isolated monotonic synthetic timing goldens; not supplier latency qualification.',
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
      JSON.stringify({ error: String(error), backendLog, errors, external }, null, 2)
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
