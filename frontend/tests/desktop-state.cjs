// Real offline server, SQLite, two process launches, two origins, no storageState.
const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs/promises')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII=', 'base64')

async function waitFor(fn, label) {
  const deadline = Date.now() + 15000
  while (!await fn()) { assert.ok(Date.now() < deadline, `Timed out: ${label}`); await pause(50) }
}
async function freePort() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}
async function launch(env) {
  const port = await freePort()
  const child = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = ''
  child.stdout.on('data', (chunk) => { log += chunk })
  child.stderr.on('data', (chunk) => { log += chunk })
  const base = `http://127.0.0.1:${port}`
  try {
    await waitFor(async () => {
      if (child.exitCode !== null) throw new Error(log)
      try { return (await fetch(`${base}/api/health`)).ok } catch { return false }
    }, 'isolated server ready')
  } catch (error) { child.kill(); throw error }
  return { base, child, port }
}
async function stop(server) {
  const ended = new Promise((resolve) => server.child.once('exit', resolve))
  server.child.kill()
  await ended
  await waitFor(async () => { try { await fetch(`${server.base}/api/health`); return false } catch { return true } }, 'own server port released')
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/desktop-state-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false' }
  execFileSync(python, ['-c', `
from app.db import init_db, SessionLocal
from app.models import Conversation, QARecord, Paper
init_db()
with SessionLocal() as db:
    db.add(Paper(id='p_state', title='State verification paper', status='parsed'))
    for name in ('a', 'b'):
        db.add(Conversation(id='c_' + name, title='Conversation ' + name.upper()))
        db.add(QARecord(id='qa_' + name, conversation_id='c_' + name,
            question='Question ' + name, answer='\\n\\n'.join('Paragraph %d. ' % i + 'Reading position verification. ' * 8 for i in range(60))))
    db.commit()
`], { cwd: path.join(root, 'backend'), env, windowsHide: true })
  const browser = await chromium.launch({ headless: true })
  const errors = []
  let server = await launch(env)
  const opened = []
  const newPage = async (size = { width: 1440, height: 900 }) => {
    const context = await browser.newContext({ viewport: size })
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    opened.push(context)
    return page
  }
  const get = async (url) => (await fetch(`${server.base}${url}`)).json()
  const doc = (id) => get(`/api/desktop-state/conversations/${id}`)
  const composer = (page) => page.locator('textarea').first()
  const saved = async (page, text) => {
    await waitFor(async () => await composer(page).inputValue() === text && /草稿已保存在本机/.test(await page.locator('.draft-save-state').textContent()), 'acknowledged draft save')
  }
  const switchTo = async (page, name) => {
    await page.locator('.recent-conversation').filter({ hasText: `Conversation ${name.toUpperCase()}` }).click()
    await page.getByText(`Question ${name}`, { exact: true }).waitFor()
    await waitFor(async () => await composer(page).isEnabled(), 'conversation state ready')
  }
  try {
    let page = await newPage()
    await page.goto(`${server.base}/chat?conv=c_a`)
    await page.getByText('Question a', { exact: true }).waitFor()
    await composer(page).fill('A draft across desktop launches')
    await page.locator('.scope-select').selectOption('p_state')
    await page.getByRole('button', { name: '选择技能', exact: true }).click()
    const firstSkill = page.locator('.skill-opt').first()
    await firstSkill.click()
    await page.getByRole('button', { name: '选择技能', exact: true }).click()
    await page.locator('input[type=file][accept*="image"]').setInputFiles({ name: 'one.png', mimeType: 'image/png', buffer: png })
    await waitFor(async () => await page.locator('.image-preview').count() === 1, 'image added')
    await page.locator('.chat-scroll').evaluate((el) => { el.scrollTop = 425; el.dispatchEvent(new Event('scroll')) })
    await saved(page, 'A draft across desktop launches')
    const original = (await doc('c_a')).value
    assert.equal(original.images.length, 1)
    assert.equal(original.paper_id, 'p_state')
    assert.equal(original.skills.length, 1)
    assert.equal(original.reading.following, false)
    await switchTo(page, 'b')
    assert.equal(await composer(page).inputValue(), '')
    assert.equal(await page.locator('.image-preview').count(), 0)
    assert.equal(await page.locator('.scope-select').inputValue(), '全库检索')
    assert.equal(await page.locator('.skill-chip').count(), 0)
    await composer(page).fill('B separate draft')
    await saved(page, 'B separate draft')
    await switchTo(page, 'a')
    await saved(page, 'A draft across desktop launches')
    assert.equal(await page.locator('.image-preview').count(), 1)
    await waitFor(async () => Math.abs(await page.locator('.chat-scroll').evaluate((el) => el.scrollTop) - original.reading.top) < 5, 'restored reading position on switch')
    // A new conversation has no backend Conversation row until it is sent.
    await composer(page).press('Control+Alt+n')
    await waitFor(() => page.locator('.welcome').isVisible(), 'new conversation')
    await composer(page).fill('Unsent new conversation survives a different port')
    await page.locator('input[type=file][accept*="image"]').setInputFiles({ name: 'new.png', mimeType: 'image/png', buffer: png })
    await waitFor(async () => await page.locator('.image-preview').count() === 1, 'new draft image')
    await saved(page, 'Unsent new conversation survives a different port')
    await waitFor(async () => !['c_a', 'c_b'].includes((await get('/api/desktop-state/selection')).value.conversation_id), 'recent selection acknowledged')
    const selection = await get('/api/desktop-state/selection')
    const newId = selection.value.conversation_id
    assert.ok(!['c_a', 'c_b'].includes(newId))
    const listed = (await get('/api/conversations')).conversations
    assert.equal(listed.length, 3)
    assert.equal(listed.find((item) => item.id === newId).draft_only, true)
    const draftRow = page.locator('.recent-conversation-row').filter({ hasText: 'Unsent new conversation survives a different port' })
    await draftRow.hover()
    await draftRow.getByRole('button', { name: '固定 Unsent new conversation survives a different port', exact: true }).click()
    await waitFor(async () => (await get('/api/conversations')).conversations.find((item) => item.id === newId).pinned_at, 'draft pin saved to SQLite')
    await page.getByRole('combobox', { name: '会话排序', exact: true }).selectOption('created_at')
    await waitFor(async () => (await get('/api/desktop-state/sidebar')).value.sort === 'created_at', 'sidebar order acknowledged')
    await page.context().close()
    const firstPort = server.port
    await stop(server)
    server = await launch(env)
    assert.notEqual(server.port, firstPort, 'different browser origin')
    page = await newPage()
    await page.goto(`${server.base}/chat`)
    await saved(page, 'Unsent new conversation survives a different port')
    assert.equal(await page.getByRole('combobox', { name: '会话排序', exact: true }).inputValue(), 'created_at')
    assert.equal(await page.locator('.conversation-date-label').first().textContent(), '已固定')
    assert.match(await page.locator('.recent-conversation-row').first().textContent(), /Unsent new conversation survives a different port/)
    assert.equal(await page.locator('.image-preview').count(), 1)
    assert.equal(await page.evaluate(() => Object.keys(localStorage).filter((key) => key.startsWith('scholarflow_draft:')).length), 0, 'fresh origin did not restore from localStorage')
    await switchTo(page, 'a')
    assert.equal(await composer(page).inputValue(), 'A draft across desktop launches')
    assert.equal(await page.locator('.scope-select').inputValue(), 'p_state')
    assert.equal(await page.locator('.skill-chip').count(), 1)
    assert.equal(await page.locator('.image-preview').count(), 1)
    await waitFor(async () => Math.abs(await page.locator('.chat-scroll').evaluate((el) => el.scrollTop) - original.reading.top) < 5, 'reading position after process restart')

    const other = await newPage({ width: 1100, height: 700 })
    await other.goto(`${server.base}/chat?conv=c_a`)
    await other.getByText('Question a', { exact: true }).waitFor()
    await saved(other, 'A draft across desktop launches')
    await composer(other).fill('Saved by the other desktop window')
    await saved(other, 'Saved by the other desktop window')
    await composer(page).fill('My local conflicting draft is preserved')
    await page.getByRole('button', { name: '保留本窗口草稿', exact: true }).waitFor()
    assert.equal(await composer(page).inputValue(), 'My local conflicting draft is preserved')
    assert.equal((await doc('c_a')).value.text, 'Saved by the other desktop window')
    await page.screenshot({ path: path.join(root, 'verification/draft-conflict-desktop.png'), fullPage: true })
    await page.getByRole('button', { name: '保留本窗口草稿', exact: true }).click()
    await saved(page, 'My local conflicting draft is preserved')
    await composer(other).fill('Second conflicting window')
    await other.getByRole('button', { name: '载入已保存草稿', exact: true }).waitFor()
    await other.screenshot({ path: path.join(root, 'verification/draft-conflict-small-desktop.png'), fullPage: true })
    await other.getByRole('button', { name: '载入已保存草稿', exact: true }).click()
    await saved(other, 'My local conflicting draft is preserved')

    let fail = true
    await other.route('**/api/desktop-state/conversations/c_a', async (route) => {
      if (fail && route.request().method() === 'PUT') return route.fulfill({ status: 500, json: { detail: 'Injected disk failure' } })
      return route.continue()
    })
    await composer(other).fill('Keep this text through a save failure')
    await other.getByRole('button', { name: '重试保存', exact: true }).waitFor()
    assert.equal((await doc('c_a')).value.text, 'My local conflicting draft is preserved')
    fail = false
    await other.getByRole('button', { name: '重试保存', exact: true }).click()
    await saved(other, 'Keep this text through a save failure')
    assert.equal((await doc('c_a')).value.text, 'Keep this text through a save failure')
    const recovery = await newPage()
    let readFailure = true
    let loseAcknowledgement = false
    await recovery.route('**/api/desktop-state/conversations/c_b', async (route) => {
      if (readFailure && route.request().method() === 'GET') return route.fulfill({ status: 503, json: { detail: 'Injected read failure' } })
      if (loseAcknowledgement && route.request().method() === 'PUT') {
        loseAcknowledgement = false
        const committed = await route.fetch()
        assert.equal(committed.status(), 200)
        return route.abort('failed')
      }
      return route.continue()
    })
    await recovery.goto(`${server.base}/chat?conv=c_b`)
    await recovery.getByText('Question b', { exact: true }).waitFor()
    await recovery.getByRole('button', { name: '重试保存', exact: true }).waitFor()
    assert.equal(await composer(recovery).inputValue(), '')
    readFailure = false
    await recovery.getByRole('button', { name: '重试保存', exact: true }).click()
    await saved(recovery, 'B separate draft')
    loseAcknowledgement = true
    await composer(recovery).fill('Saved but its HTTP response was lost')
    await recovery.getByRole('button', { name: '重试保存', exact: true }).waitFor()
    assert.equal((await doc('c_b')).value.text, 'Saved but its HTTP response was lost')
    await recovery.getByRole('button', { name: '重试保存', exact: true }).click()
    await saved(recovery, 'Saved but its HTTP response was lost')
    assert.equal(await recovery.getByRole('button', { name: '保留本窗口草稿', exact: true }).count(), 0)

    // Delay FileReader completion until after the user switches conversation.
    await recovery.evaluate(() => {
      const NativeReader = window.FileReader
      window.FileReader = class {
        constructor() { this.reader = new NativeReader() }
        readAsDataURL(file) {
          this.reader.onload = () => { this.result = this.reader.result; setTimeout(() => this.onload?.(), 500) }
          this.reader.readAsDataURL(file)
        }
        abort() { this.reader.abort() }
      }
    })
    await recovery.locator('input[type=file][accept*="image"]').setInputFiles({ name: 'delayed.png', mimeType: 'image/png', buffer: png })
    await switchTo(recovery, 'a')
    await pause(650)
    assert.equal(await recovery.locator('.image-preview').count(), 1, 'late B attachment did not leak into A')
    assert.equal((await doc('c_b')).value.images.length, 0)
    for (const current of [page, other]) assert.ok(await current.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    assert.deepEqual(errors, [])
    console.log('PASS: independent processes/ports, fresh contexts, persisted pins/sort, unsent draft entry, image/context isolation, reading restore, CAS conflicts/resolution, save and read failure/retry, lost response idempotency, stale FileReader, desktop sizes')
  } finally {
    for (const context of opened) await context.close().catch(() => {})
    await browser.close()
    await stop(server)
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
