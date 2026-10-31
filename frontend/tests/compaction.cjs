// Real SQLite/HTTP/SSE and isolated browser contexts; no external model requests.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, name) {
  const deadline = Date.now() + 18000
  while (!await fn()) { assert.ok(Date.now() < deadline, name); await pause(50) }
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/compact-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', MOCK_STREAM_DELAY: '0.002' }
  let child, base, log = ''
  async function start() {
    const reservation = net.createServer()
    await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
    const port = reservation.address().port
    await new Promise((resolve) => reservation.close(resolve))
    child = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    child.stdout.on('data', (s) => { log += s }); child.stderr.on('data', (s) => { log += s })
    base = `http://127.0.0.1:${port}`
    await until(async () => { if (child.exitCode !== null) throw new Error(log); try { return (await fetch(base + '/api/health')).ok } catch { return false } }, 'server ready')
  }
  function stop() {
    if (child && child.exitCode === null) execFileSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    child = null
  }
  const api = async (url) => (await fetch(base + '/api' + url)).json()
  execFileSync(python, ['-X', 'utf8', '-c', `
from datetime import datetime, timedelta
from app.db import init_db, SessionLocal
from app.models import Conversation, QARecord
init_db()
with SessionLocal() as db:
    db.add(Conversation(id='c_memory', title='长会话记忆验收'))
    db.add(Conversation(id='c_other', title='独立会话'))
    for i in range(18):
        db.add(QARecord(id='qa_memory_%02d' % i, conversation_id='c_memory', question='第 %d 轮：保存实验约束' % i,
            answer='样本温度 23.5 摄氏度；保留原始证据 [^e1]。<img src=x onerror=alert(1)>',
            evidence=[dict(id='e1', paper_title='实验记录', paper_id='p_memory', chunk_id='chunk_%d' % i, page=i+1, quote='样本温度 23.5 摄氏度。')],
            created_at=datetime(2026, 1, 1) + timedelta(minutes=i)))
    db.commit()
`], { cwd: path.join(root, 'backend'), env, windowsHide: true })
  const browser = await chromium.launch({ headless: true })
  const errors = []
  try {
    await start()
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const other = await browser.newPage({ viewport: { width: 1100, height: 700 } })
    for (const p of [page, other]) { p.on('pageerror', (e) => errors.push(e.message)); await p.goto(base + '/chat?conv=c_memory') }
    const dialog = (p) => p.getByRole('dialog', { name: '会话记忆', exact: true })
    await page.locator('textarea.textarea').fill('这条草稿在压缩后保留')
    await page.getByRole('button', { name: '会话记忆', exact: true }).click()
    await dialog(page).getByText('14 轮待压缩', { exact: true }).waitFor()
    await other.getByRole('button', { name: '会话记忆', exact: true }).click()
    await dialog(other).getByText('14 轮待压缩', { exact: true }).waitFor()
    await dialog(page).getByRole('button', { name: '压缩较早对话', exact: true }).click()
    await dialog(page).getByText('14 轮已压缩', { exact: true }).waitFor()
    assert.ok((await api('/conversations/c_memory/memory')).summary.includes('23.5'))
    assert.equal(await dialog(page).locator('.memory-summary img').count(), 0, 'summary is plain text, not executable HTML')
    // Old window sends the old revision; server rejects it and panel reloads.
    await dialog(other).getByRole('button', { name: '压缩较早对话', exact: true }).click()
    await dialog(other).getByRole('alert').filter({ hasText: '摘要已在其他窗口更新' }).waitFor()
    await dialog(other).getByText('14 轮已压缩', { exact: true }).waitFor()
    for (const [p, name] of [[page, 'desktop'], [other, 'small-desktop']]) {
      await p.bringToFront()
      await p.screenshot({ path: path.join(root, `verification/compaction-${name}.png`), animations: 'disabled' })
      assert.ok(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
      assert.ok(await dialog(p).evaluate((el) => el.getBoundingClientRect().bottom <= innerHeight))
      await dialog(p).locator('.el-dialog__headerbtn').focus()
      await p.keyboard.press('Shift+Tab')
      await until(async () => await dialog(p).evaluate((el) => el.contains(document.activeElement)), 'dialog retains keyboard focus')
    }
    await dialog(page).getByText('回查来源（14 轮）', { exact: true }).click()
    await dialog(page).getByRole('button', { name: '1. 第 0 轮：保存实验约束 1 条证据' }).click()
    await until(async () => !await dialog(page).isVisible(), 'source closes panel')
    await until(async () => await page.locator('[data-record-id="qa_memory_00"]').evaluate((el) => document.activeElement === el), 'source is focused')
    assert.equal(await page.locator('textarea.textarea').inputValue(), '这条草稿在压缩后保留')
    // Loading and mutation failures are visible and retryable.
    await page.route('**/api/conversations/c_memory/memory', (route) => route.fulfill({ status: 503, json: { detail: '测试读取失败' } }))
    await page.getByRole('button', { name: '会话记忆', exact: true }).click()
    await dialog(page).getByRole('alert').filter({ hasText: '测试读取失败' }).waitFor()
    await page.unroute('**/api/conversations/c_memory/memory')
    await dialog(page).getByRole('button', { name: '重新载入', exact: true }).click()
    await dialog(page).getByText('14 轮已压缩', { exact: true }).waitFor()
    await page.route('**/api/conversations/c_memory/memory/compact', (route) => route.fulfill({ status: 503, json: { detail: '测试更新失败' } }))
    await dialog(page).getByRole('button', { name: '重新生成摘要', exact: true }).click()
    await dialog(page).getByRole('alert').filter({ hasText: '测试更新失败' }).waitFor()
    await page.unroute('**/api/conversations/c_memory/memory/compact')
    await dialog(page).getByRole('button', { name: '重新生成摘要', exact: true }).click()
    await dialog(page).getByText('摘要已保存在本机，原始聊天保持完整。', { exact: true }).waitFor()
    await page.keyboard.press('Escape')
    // Slash command opens the same state without another model call when cached.
    const saved = await api('/conversations/c_memory/memory')
    await page.locator('textarea.textarea').fill('/compact')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await dialog(page).getByText('14 轮已压缩', { exact: true }).waitFor()
    assert.equal((await api('/conversations/c_memory/memory')).revision, saved.revision)
    await page.keyboard.press('Escape')
    await page.locator('textarea.textarea').fill('/compact')
    await page.getByTitle('后台运行（可在后台任务页查看）', { exact: true }).click()
    await dialog(page).getByText('14 轮已压缩', { exact: true }).waitFor()
    assert.equal((await api('/tasks')).tasks.length, 0, 'local commands must not become background tasks')
    await page.keyboard.press('Escape')
    await page.locator('textarea.textarea').fill('/compact')
    await page.locator('textarea.textarea').press('Enter')
    await dialog(page).getByText('14 轮已压缩', { exact: true }).waitFor()
    await page.keyboard.press('Control+Enter')
    assert.equal((await api('/conversations/c_memory')).messages.length, 18, 'dialog shortcut must not submit underlying composer')
    await page.keyboard.press('Escape')
    await page.locator('textarea.textarea').fill('冷启动继续使用摘要')
    await until(async () => (await api('/desktop-state/conversations/c_memory')).value.text === '冷启动继续使用摘要', 'saved draft')
    const oldBase = base
    stop(); await start(); assert.notEqual(base, oldBase)
    const fresh = await browser.newPage({ viewport: { width: 1100, height: 700 } })
    await fresh.goto(base + '/chat')
    await until(async () => await fresh.locator('textarea.textarea').inputValue() === '冷启动继续使用摘要', 'cold draft restored')
    await fresh.getByRole('button', { name: '会话记忆', exact: true }).click()
    await dialog(fresh).getByText('14 轮已压缩', { exact: true }).waitFor()
    assert.equal((await api('/conversations/c_memory/memory')).summary, saved.summary)
    assert.equal((await api('/conversations/c_memory')).messages.length, 18)
    assert.deepEqual(errors, [])
    console.log('PASS: persisted summaries, two-window revision conflicts, source focus, draft preservation, escaped text, failure/retry, /compact cache, cold origin restart; desktop 1440x900 and 1100x700')
  } finally { stop(); await browser.close() }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
