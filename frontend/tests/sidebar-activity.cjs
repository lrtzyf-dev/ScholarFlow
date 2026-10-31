// Real SQLite and SSE; focus/visibility gates and network faults are controlled.
const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs/promises')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function waitFor(fn, label) {
  const deadline = Date.now() + 15000
  while (!await fn()) { assert.ok(Date.now() < deadline, `Timed out: ${label}`); await pause(50) }
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/sidebar-activity-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false', MOCK_STREAM_DELAY: '0.04' }
  execFileSync(python, ['-c', `
from datetime import datetime, timedelta
from app.db import init_db, SessionLocal
from app.models import Conversation, QARecord, DesktopState
init_db()
with SessionLocal() as db:
    db.add(Conversation(id='c_read', title='Evidence review'))
    for i in range(3):
        db.add(QARecord(id='qa_read_%d' % i, conversation_id='c_read', question='Evidence question %d' % i,
            answer='\\n\\n'.join('Evidence paragraph %d. ' % j + 'Verification reading content. ' * 7 for j in range(24)),
            created_at=datetime.utcnow() + timedelta(seconds=i)))
    db.add(DesktopState(key='conversation:c_read', value={'text':'', 'paper_id':None, 'skills':[], 'images':[],
        'reading':{'following':False, 'top':0, 'anchor_id':None, 'offset':0}}))
    db.commit()
`], { cwd: path.join(root, 'backend'), env, windowsHide: true })
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  const child = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = ''
  child.stdout.on('data', (chunk) => { log += chunk })
  child.stderr.on('data', (chunk) => { log += chunk })
  const base = `http://127.0.0.1:${port}`
  const request = async (url, body, method = 'POST') => {
    const response = await fetch(`${base}${url}`, body === undefined ? undefined : {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`)
    return response.json()
  }
  const summaries = async () => (await request('/api/conversations')).conversations
  const unread = async () => (await summaries()).find((item) => item.id === 'c_read').unread_count
  const browser = await chromium.launch({ headless: true })
  const errors = []
  const pages = []
  let releaseReceipt
  try {
    await waitFor(async () => {
      if (child.exitCode !== null) throw new Error(log)
      try { return (await fetch(`${base}/api/health`)).ok } catch { return false }
    }, 'isolated offline backend')
    assert.equal((await request('/api/health')).llm_mode, 'mock')
    for (const viewport of [{ width: 1440, height: 900 }, { width: 1100, height: 700 }]) {
      const context = await browser.newContext({ viewport })
      const page = await context.newPage()
      pages.push(page)
      page.on('pageerror', (error) => errors.push(error.message))
      await page.addInitScript(() => {
        window.readerFocused = false
        window.readerVisible = true
        document.hasFocus = () => window.readerFocused
        Object.defineProperty(document, 'visibilityState', { get: () => window.readerVisible ? 'visible' : 'hidden' })
        window.sourceCount = 0
        const NativeEventSource = window.EventSource
        window.EventSource = class extends NativeEventSource {
          constructor(...args) { super(...args); window.sourceCount++ }
        }
      })
    }
    const [reader, other] = pages
    const row = (page, title = 'Evidence review') => page.locator('.recent-conversation-row').filter({ hasText: title })
    await reader.goto(`${base}/chat?conv=c_read`)
    await other.goto(`${base}/library`)
    await reader.getByText('Evidence question 0', { exact: true }).waitFor()
    await row(other).locator('.conversation-unread').waitFor()
    await pause(450)
    assert.equal(await unread(), 3, 'unfocused window does not mark displayed answers read')
    await reader.evaluate(() => {
      window.readerFocused = true; window.readerVisible = false
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await pause(450)
    assert.equal(await unread(), 3, 'hidden window does not mark answers read')
    let readRequests = 0, failRead = true
    await reader.route('**/api/conversations/c_read/read', async (route) => {
      readRequests++
      if (failRead) return route.fulfill({ status: 503, json: { detail: 'Injected read receipt failure' } })
      await route.continue()
    })
    await reader.getByRole('button', { name: '历史会话', exact: true }).click()
    await reader.locator('.hist-mask').waitFor()
    await reader.evaluate(() => { window.readerVisible = true; document.dispatchEvent(new Event('visibilitychange')) })
    await pause(450)
    assert.equal(await unread(), 3, 'a history overlay blocks read receipts for the underlying conversation')
    await reader.locator('.hist-mask .modal-x').click()
    await reader.getByText('已读状态尚未保存', { exact: false }).waitFor()
    assert.equal(await unread(), 3, 'failed acknowledgement keeps unread state')
    failRead = false
    await reader.locator('.draft-save-state').filter({ hasText: '已读状态尚未保存' }).getByRole('button', { name: '重试', exact: true }).click()
    await waitFor(async () => await unread() === 2, 'only first visible answer acknowledged')
    await waitFor(async () => /2 条未读回答/.test(await row(other).locator('.recent-conversation').getAttribute('aria-label')), 'read receipt sync over SSE to another window')
    await reader.evaluate(() => { window.readerFocused = false })

    // Pin/rename/new-run events update the second window without focus or reload.
    await request('/api/conversations/c_read/pin', { pinned: true }, 'PUT')
    await waitFor(async () => await row(other).locator('.conversation-pin.pinned').count() === 1, 'remote pin sync')
    await request('/api/conversations/c_read', { title: 'Evidence review updated' }, 'PUT')
    await row(other, 'Evidence review updated').waitFor()
    const run = await request('/api/foreground-runs', {
      id: 'chat_sidebar_live_001', conversation_id: 'c_foreground', question: 'Foreground evidence verification',
    })
    await row(other, 'Foreground evidence verification').locator('.running-state').waitFor()
    await other.screenshot({ path: path.join(root, 'verification/sidebar-activity-small-desktop.png'), fullPage: true, animations: 'disabled' })
    await reader.screenshot({ path: path.join(root, 'verification/sidebar-activity-desktop.png'), fullPage: true, animations: 'disabled' })
    await request(`/api/foreground-runs/${run.id}`, { revision: run.revision, status: 'cancelled' }, 'PATCH')
    await waitFor(async () => await row(other, 'Foreground evidence verification').locator('.running-state').count() === 0, 'foreground terminal state sync')

    const task = await request('/api/tasks/start', { question: 'Background evidence verification' })
    await row(other, 'Background evidence verification').waitFor()
    await waitFor(async () => {
      const current = await request(`/api/tasks/${task.id}`)
      return current.status === 'done' && !current.executing
    }, 'real offline background answer')
    await row(other, 'Background evidence verification').locator('.conversation-unread').waitFor()
    const detail = await request(`/api/conversations/${task.conversation_id}`)
    assert.equal(detail.messages.length, 1)
    const singleSource = await other.evaluate(() => window.sourceCount)
    assert.equal(singleSource, 1)

    // A delayed receipt is scoped to the observed ID even while a new answer arrives.
    await reader.unroute('**/api/conversations/c_read/read')
    let receiptBody
    await reader.route('**/api/conversations/c_read/read', async (route) => {
      receiptBody = route.request().postDataJSON()
      await new Promise((resolve) => { releaseReceipt = resolve })
      await route.continue()
    })
    await reader.locator('[data-read-record-id="qa_read_1"]').evaluate((el) => el.scrollIntoView({ block: 'start' }))
    await reader.evaluate(() => { window.readerFocused = true; dispatchEvent(new Event('focus')) })
    await waitFor(() => releaseReceipt, 'held receipt for second answer')
    assert.deepEqual(receiptBody.message_ids, ['qa_read_1'])
    await reader.evaluate(() => { window.readerFocused = false })
    let failRemoteLoad = true, remoteLoadRequests = 0
    await reader.route('**/api/conversations/c_read', async (route) => {
      remoteLoadRequests++
      if (failRemoteLoad) return route.fulfill({ status: 503, json: { detail: 'Injected remote-answer load failure' } })
      await route.continue()
    })
    const newTask = await request('/api/tasks/start', { question: 'Concurrent answer during read acknowledgement', conversation_id: 'c_read' })
    await waitFor(async () => (await request(`/api/tasks/${newTask.id}`)).status === 'done', 'concurrent answer delivered')
    assert.equal(await unread(), 3)
    releaseReceipt()
    releaseReceipt = undefined
    await waitFor(async () => await unread() === 2, 'late receipt leaves unseen and concurrent answers unread')
    await reader.unroute('**/api/conversations/c_read/read')
    await reader.getByText('新回答载入失败', { exact: false }).waitFor()
    await pause(400)
    assert.equal(remoteLoadRequests, 1, 'failed remote delivery load does not spin requests')
    failRemoteLoad = false
    await reader.getByRole('button', { name: '重试载入', exact: true }).click()
    await waitFor(async () => await reader.locator('[data-read-record-id]').count() === 4, 'open conversation loads remotely delivered answer')
    assert.equal(await reader.evaluate(() => window.sourceCount), 1, 'route changes retain one SSE connection')
    assert.ok(readRequests >= 2)

    await other.reload()
    await waitFor(async () => /2 条未读回答/.test(await row(other, 'Evidence review updated').locator('.recent-conversation').getAttribute('aria-label')), 'read state persists after refresh')
    for (const page of pages) assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    assert.deepEqual(errors, [])
    console.log('PASS: real SQLite/SSE, visibility/focus/overlay gates, viewport-only read IDs, save failure/retry, cross-window unread/pin/rename/run sync, reachable background task, concurrent answer vs late receipt, open-chat delivery, refresh persistence, one SSE, desktop sizes')
  } finally {
    releaseReceipt?.()
    await browser.close()
    const ended = new Promise((resolve) => child.once('exit', resolve))
    child.kill()
    await ended
    await waitFor(async () => { try { await fetch(`${base}/api/health`); return false } catch { return true } }, 'isolated server port released')
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
