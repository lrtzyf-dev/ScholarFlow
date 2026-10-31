// Real SQLite, bounded HTTP/SSE, two windows, delayed responses and desktop layout.
const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs/promises')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, label) {
  const deadline = Date.now() + 18000
  while (!await fn()) { assert.ok(Date.now() < deadline, `Timed out: ${label}`); await pause(50) }
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/search-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false' }
  execFileSync(python, ['-X', 'utf8', '-c', `
from datetime import datetime, timedelta
from app.db import init_db, SessionLocal
from app.models import Conversation, QARecord, DesktopState
init_db()
with SessionLocal() as db:
    at = datetime(2026,10,1)
    for i in range(125):
        cid = f'c_{i:03}'
        db.add(Conversation(id=cid, title=f'文献检索 {i:03}', created_at=at + timedelta(minutes=i), updated_at=at + timedelta(minutes=i)))
        db.add(QARecord(id=f'qa_{i:03}', conversation_id=cid, question='研究方法是什么？', answer='对照实验中的中文短语。100%_准确度。<img src=x onerror=alert(1)>', read_at=at, created_at=at))
    db.add(QARecord(id='qa_later', conversation_id='c_001', question='Follow up', answer=('Long later answer.\\n\\n' * 150), created_at=at + timedelta(days=1), read_at=at))
    db.add(Conversation(id='c_archived', title='已归档研究', archived_at=at))
    db.add(QARecord(id='qa_archived', conversation_id='c_archived', question='History', answer='归档中文短语', read_at=at))
    db.add(DesktopState(key='conversation:c_draft', value={'text':'草稿中的独特词句','images':[]}))
    db.commit()
`], { cwd: path.join(root, 'backend'), env, windowsHide: true })
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  const child = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let log = '', release
  child.stdout.on('data', (chunk) => { log += chunk })
  child.stderr.on('data', (chunk) => { log += chunk })
  const base = `http://127.0.0.1:${port}`
  const browser = await chromium.launch({ headless: true })
  try {
    await until(async () => { if (child.exitCode !== null) throw new Error(log); try { return (await fetch(`${base}/api/health`)).ok } catch { return false } }, 'server')
    const errors = [], requests = [], streams = []
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const other = await browser.newPage({ viewport: { width: 1100, height: 700 } })
    for (const p of [page, other]) {
      p.on('pageerror', (error) => errors.push(error.message))
      p.on('request', (request) => { requests.push(request.url()); if (request.url().includes('/tasks/events')) streams.push(request.url()) })
    }
    await page.goto(`${base}/chat?conv=c_001`)
    await other.goto(`${base}/library`)
    await until(async () => await page.locator('.recent-conversation-row').count() === 40, 'bounded first page')
    await page.locator('.conversation-load-more').click()
    await until(async () => await page.locator('.recent-conversation-row').count() === 80, 'sidebar second page')
    // The already loaded tail survives a change to the first page.
    await fetch(`${base}/api/conversations/c_124`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: 'Updated in another window' }) })
    await until(async () => await page.locator('[data-conversation-id="c_124"]').innerText().then((s) => s.includes('Updated')), 'rename SSE')
    assert.equal(await page.locator('.recent-conversation-row').count(), 80)
    await other.getByRole('button', { name: '搜索会话', exact: true }).click()
    const small = other.getByRole('dialog', { name: '历史会话' })
    await small.waitFor()
    assert.equal(await small.locator('input.hist-search-input').evaluate((el) => document.activeElement === el), true)
    await small.locator('input.hist-search-input').fill('草稿中的独特词句')
    await small.locator('[data-history-id="c_draft"]').waitFor()
    await small.press('Escape')
    await page.keyboard.press('Control+k')
    const dialog = page.getByRole('dialog', { name: '历史会话' })
    const input = dialog.locator('.hist-search-input')
    await input.fill('中文短语')
    await until(async () => await dialog.locator('.hist-item').count() === 40 && await dialog.locator('mark').first().innerText() === '中文短语', 'body results')
    assert.equal(await dialog.locator('.hist-result-count').innerText(), '125 个会话')
    assert.equal(await dialog.locator('.hist-match img').count(), 0)
    await page.screenshot({ path: path.join(root, 'verification/search-desktop.png'), fullPage: true, animations: 'disabled' })
    let failMore = true, moreCount = 0
    await page.route('**/api/conversations/page?*', async (route) => {
      if (new URL(route.request().url()).searchParams.has('cursor')) {
        moreCount++
        if (failMore) return route.fulfill({ status: 503, json: { detail: 'Injected page failure' } })
      }
      return route.continue()
    })
    await dialog.getByRole('button', { name: '加载更多会话', exact: true }).click()
    await dialog.getByRole('button', { name: '重试加载更多会话', exact: true }).waitFor()
    assert.equal(await dialog.locator('.hist-item').count(), 40)
    failMore = false
    await dialog.getByRole('button', { name: '重试加载更多会话', exact: true }).click()
    await until(async () => await dialog.locator('.hist-item').count() === 80, 'retry append')
    assert.equal(moreCount, 2)
    await page.unroute('**/api/conversations/page?*')
    // Refresh removes a deleted tail row without dropping the other loaded rows.
    await fetch(`${base}/api/conversations/c_060`, { method: 'DELETE' })
    await until(async () => await dialog.locator('.hist-result-count').innerText() === '124 个会话', 'deletion total reaches drawer')
    assert.equal(await dialog.locator('[data-history-id="c_060"]').count(), 0)
    assert.equal(await dialog.locator('.hist-item').count(), 79)
    let heldPage
    await page.route('**/api/conversations/page?*', async (route) => {
      if (new URL(route.request().url()).searchParams.has('cursor')) {
        moreCount++
        const response = await route.fetch()
        await new Promise((resolve) => { heldPage = resolve })
        return route.fulfill({ response })
      }
      return route.continue()
    })
    const beforeMore = moreCount
    await dialog.getByRole('button', { name: '加载更多会话', exact: true }).click()
    await until(async () => Boolean(heldPage), 'held page')
    assert.equal(await dialog.locator('.hist-load-more').isDisabled(), true)
    await fetch(`${base}/api/conversations/c_020`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: 'Moved while pagination was in flight' }) })
    await until(async () => await page.locator('[data-conversation-id="c_020"]').count() === 1, 'SSE invalidates held page')
    heldPage()
    await until(async () => await dialog.locator('[data-history-id="c_020"]').count() === 1, 'late page discarded and first page refreshed')
    assert.equal(moreCount, beforeMore + 1, 'double submission prevented')
    assert.equal(await dialog.locator('[data-history-id="c_019"]').count(), 0, 'late page cannot populate unseen stale rows')
    await page.unroute('**/api/conversations/page?*')
    // An old search response cannot replace the next query.
    await page.route('**/api/conversations/page?*', async (route) => {
      if (new URL(route.request().url()).searchParams.get('q') === '100%_') {
        const response = await route.fetch()
        await new Promise((resolve) => { release = resolve })
        return route.fulfill({ response })
      }
      return route.continue()
    })
    await input.fill('100%_')
    await until(async () => Boolean(release), 'held old query')
    await input.fill('c_001')
    release(); release = null
    await dialog.locator('[data-history-id="c_001"]').waitFor()
    assert.equal(await dialog.locator('.hist-item').count(), 1)
    await page.unroute('**/api/conversations/page?*')
    await input.fill('中文短语')
    await until(async () => await dialog.locator('.hist-item').count() === 40, 'search before scope switch')
    await dialog.getByRole('tab', { name: '已归档', exact: true }).click()
    await dialog.locator('[data-history-id="c_archived"]').waitFor()
    assert.equal(await dialog.locator('.hist-item').count(), 1)
    await dialog.locator('[data-history-id="c_archived"]').getByRole('button', { name: '恢复会话', exact: true }).click()
    await dialog.getByText('没有匹配的会话', { exact: true }).waitFor()
    await dialog.getByRole('tab', { name: '会话', exact: true }).click()
    await dialog.locator('[data-history-id="c_archived"]').waitFor()
    await input.fill('文献检索 001')
    await dialog.locator('[data-history-id="c_001"]').waitFor()
    await input.fill('中文短语')
    await until(async () => await dialog.locator('.hist-item').count() === 40, 'initial body search again')
    while (!await dialog.locator('[data-history-id="c_001"]').count()) {
      const before = await dialog.locator('.hist-item').count()
      await dialog.getByRole('button', { name: '加载更多会话', exact: true }).click()
      await until(async () => await dialog.locator('.hist-item').count() > before, 'find older match')
    }
    await dialog.locator('[data-history-id="c_001"] .hist-item-main').click()
    await until(async () => await page.locator('[data-record-id="qa_001"]').evaluate((el) => document.activeElement === el), 'focus matching old turn')
    await until(async () => await page.locator('[data-record-id="qa_001"]').evaluate((el) => el.getBoundingClientRect().top > 0), 'matching turn visible')
    await other.keyboard.press('Control+k')
    await small.locator('.hist-search-input').fill('中文短语')
    await until(async () => await small.locator('.hist-item').count() === 40, 'small search')
    await other.screenshot({ path: path.join(root, 'verification/search-small-desktop.png'), fullPage: true, animations: 'disabled' })
    for (const p of [page, other]) assert.equal(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    assert.equal(await small.evaluate((el) => el.scrollWidth <= el.clientWidth), true)
    assert.deepEqual(errors, [])
    assert.ok(!requests.some((url) => /^\/api\/conversations$/.test(new URL(url).pathname)), 'UI never requests full conversation lists')
    assert.equal(streams.length, 2)
    assert.ok(streams.every((url) => url.includes('conversation_mode=signal')))
    const snapshot = await (await fetch(`${base}/api/tasks?conversation_mode=signal`)).json()
    assert.ok(!('conversations' in snapshot))
    console.log('Search passed: body/draft/archived results, excerpts, literal escaping, pagination retry, stale query rejection, retained pages, two windows, match focus, bounded SSE, desktop sizes.')
  } finally {
    release?.()
    await browser.close()
    if (child.exitCode === null) { const ended = new Promise((resolve) => child.once('exit', resolve)); child.kill(); await ended }
    await until(async () => { try { await fetch(`${base}/api/health`); return false } catch { return true } }, 'own port released')
    const resolved = path.resolve(data)
    assert.ok(resolved.startsWith(path.resolve(root, 'verification/runtime') + path.sep))
    await fs.rm(resolved, { recursive: true, force: true })
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
