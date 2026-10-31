// Real SQLite and SSE, two desktop windows, failures and a cold restart.
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
  const deadline = Date.now() + 18000
  while (!await fn()) { assert.ok(Date.now() < deadline, `Timed out: ${label}`); await pause(50) }
}
async function launch(env, previousPort) {
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  if (port === previousPort) return launch(env, previousPort)
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
    }, 'isolated backend')
  } catch (error) { child.kill(); throw error }
  return { child, port, base }
}
async function stop(server) {
  if (server.child.exitCode !== null) return
  const ended = new Promise((resolve) => server.child.once('exit', resolve))
  server.child.kill()
  await ended
  await waitFor(async () => { try { await fetch(`${server.base}/api/health`); return false } catch { return true } }, 'own port released')
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/archive-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false' }
  execFileSync(python, ['-X', 'utf8', '-c', `
from datetime import datetime
from app.db import init_db, SessionLocal
from app.models import Conversation, QARecord, TaskRecord
init_db()
with SessionLocal() as db:
    for cid, title in (('c_archive', 'Archive workflow verification'), ('c_other', 'Literature review')):
        db.add(Conversation(id=cid, title=title, pinned_at=datetime.utcnow(), pin_position=0 if cid == 'c_archive' else 1))
        db.add(QARecord(id='qa_' + cid, conversation_id=cid, question='Which records are retained?',
            answer='Messages, evidence and task results remain available after archiving.',
            evidence=[], read_at=datetime.utcnow()))
    db.add(TaskRecord(id='task_retained', type='agent', title='Completed research', conversation_id='c_archive', status='done', result={'answer': 'Retained result'}))
    db.commit()
`], { cwd: path.join(root, 'backend'), env, windowsHide: true })
  const browser = await chromium.launch({ headless: true })
  let server = await launch(env)
  let releaseArchive
  const errors = []
  const request = async (url, body, method = 'PUT', expected = 200) => {
    const response = await fetch(`${server.base}${url}`, body === undefined ? undefined : {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    assert.equal(response.status, expected, `${url}: ${await response.clone().text()}`)
    return response.json()
  }
  const archive = (id, archived, revision, expected) => request(`/api/conversations/${id}/archive`, { archived, expected_revision: revision }, 'PUT', expected)
  const sidebar = (page, id) => page.locator(`.recent-conversation-row[data-conversation-id="${id}"]`)
  const historyRow = (page, id) => page.locator(`[data-history-id="${id}"]`)
  const openHistory = async (page, archived = true) => {
    await page.getByRole('button', { name: '全部', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '历史会话' })
    await dialog.waitFor()
    await dialog.getByRole('tab', { name: archived ? '已归档' : '会话', exact: true }).click()
    return dialog
  }
  const newPage = async (viewport) => {
    const context = await browser.newContext({ viewport })
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    return page
  }
  const noOverflow = async (page) => {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    const notice = page.locator('.archive-notice')
    if (await notice.count()) assert.equal(await notice.evaluate((el) => el.scrollWidth <= el.clientWidth), true)
    const dialog = page.getByRole('dialog', { name: '历史会话' })
    if (await dialog.count()) assert.equal(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth), true)
  }
  try {
    const originalDraft = { text: 'Unsent analysis remains intact', paper_id: null, skills: [], images: [], reading: { following: true, top: 0, anchor_id: null, offset: 0 } }
    await request('/api/desktop-state/conversations/c_archive', { revision: 0, value: originalDraft })
    const page = await newPage({ width: 1440, height: 900 })
    const other = await newPage({ width: 1100, height: 700 })
    await page.goto(`${server.base}/chat?conv=c_archive`)
    await other.goto(`${server.base}/chat?conv=c_archive`)
    await waitFor(async () => await page.locator('.textarea').inputValue() === originalDraft.text, 'draft restored')
    await sidebar(page, 'c_archive').hover()
    await sidebar(page, 'c_archive').getByRole('button', { name: '归档 Archive workflow verification', exact: true }).click()
    await other.locator('.archive-notice').waitFor()
    assert.equal(await other.locator('.textarea').isDisabled(), true)
    assert.equal(await other.locator('.send-btn').isDisabled(), true)
    assert.equal(await other.locator('.textarea').inputValue(), originalDraft.text)
    await waitFor(async () => await sidebar(other, 'c_archive').count() === 0, 'removed from both sidebars')
    assert.equal((await request('/api/tasks/task_retained')).result.answer, 'Retained result')
    const detail = await request('/api/conversations/c_archive')
    assert.equal(detail.messages.length, 1)
    assert.equal(detail.archive_revision, 1)
    await noOverflow(page)
    await page.screenshot({ path: path.join(root, 'verification/archive-desktop.png'), fullPage: true })
    await noOverflow(other)
    await other.screenshot({ path: path.join(root, 'verification/archive-small-desktop.png'), fullPage: true })

    const dialog = await openHistory(page)
    await historyRow(page, 'c_archive').waitFor()
    await dialog.getByRole('tab', { name: '已归档', exact: true }).press('ArrowLeft')
    assert.equal(await dialog.getByRole('tab', { name: '会话', exact: true }).getAttribute('aria-selected'), 'true')
    assert.equal(await historyRow(page, 'c_archive').count(), 0)
    await dialog.getByRole('tab', { name: '会话', exact: true }).press('End')
    await historyRow(page, 'c_archive').waitFor()
    const closeHistory = dialog.getByRole('button', { name: '关闭历史会话', exact: true })
    await closeHistory.focus()
    await closeHistory.press('Shift+Tab')
    assert.equal(await historyRow(page, 'c_archive').getByRole('button', { name: '删除', exact: true }).evaluate((el) => document.activeElement === el), true, 'dialog traps keyboard focus')
    await historyRow(page, 'c_archive').waitFor()
    await dialog.locator('.hist-search-input').fill('not-found')
    await dialog.getByText('没有匹配的会话', { exact: true }).waitFor()
    await dialog.locator('.hist-search-input').fill('')
    await historyRow(page, 'c_archive').hover()
    await waitFor(async () => await historyRow(page, 'c_archive').locator('.hist-item-actions').evaluate((el) => getComputedStyle(el).opacity === '1'), 'history actions visible')
    await page.screenshot({ path: path.join(root, 'verification/archive-history-desktop.png'), fullPage: true })
    await historyRow(page, 'c_archive').getByRole('button', { name: '恢复会话', exact: true }).click()
    await waitFor(async () => await other.locator('.archive-notice').count() === 0, 'restore reaches current conversation')
    await waitFor(async () => await sidebar(other, 'c_archive').count() === 1, 'restore reaches sidebar')
    assert.equal(await other.locator('.textarea').isDisabled(), false)
    assert.equal(await sidebar(other, 'c_archive').locator('[data-pin-handle-id]').count(), 0, 'restored conversation stays unpinned')
    await dialog.getByText('暂无已归档会话', { exact: true }).waitFor()
    await dialog.press('Escape')
    assert.equal(await page.getByRole('button', { name: '全部', exact: true }).evaluate((el) => document.activeElement === el), true, 'focus returns to opener')

    let failHistory = true
    await page.route('**/api/conversations/page?*', (route) => failHistory
      ? route.fulfill({ status: 503, json: { detail: 'Injected history failure' } }) : route.continue())
    await openHistory(page)
    await dialog.getByRole('button', { name: '重试加载', exact: true }).waitFor()
    failHistory = false
    await dialog.getByRole('button', { name: '重试加载', exact: true }).click()
    await waitFor(async () => await dialog.getByRole('button', { name: '重试加载', exact: true }).count() === 0, 'history retry')
    await dialog.press('Escape')

    let mode = 'fail', archiveRequests = 0
    await page.route('**/api/conversations/c_archive/archive', async (route) => {
      archiveRequests++
      if (mode === 'fail') return route.fulfill({ status: 503, json: { detail: 'Injected archive failure' } })
      if (mode === 'lost') {
        mode = 'normal'
        assert.equal((await route.fetch()).status(), 200)
        return route.abort('failed')
      }
      if (mode === 'hold') await new Promise((resolve) => { releaseArchive = resolve })
      await route.continue()
    })
    await openHistory(page, false)
    await historyRow(page, 'c_archive').hover()
    await historyRow(page, 'c_archive').getByRole('button', { name: '归档会话', exact: true }).click()
    await page.locator('.archive-operation-error').getByRole('button', { name: '重试归档操作', exact: true }).waitFor()
    assert.equal(await sidebar(other, 'c_archive').count(), 1, 'failure retains conversation')
    mode = 'normal'
    await page.locator('.archive-operation-error').getByRole('button', { name: '重试归档操作', exact: true }).click()
    await other.locator('.archive-notice').waitFor()
    await dialog.press('Escape')
    await other.locator('.archive-notice').getByRole('button', { name: '恢复会话', exact: true }).click()
    await waitFor(async () => await sidebar(page, 'c_archive').count() === 1, 'ready for lost response')
    mode = 'lost'
    await sidebar(page, 'c_archive').hover()
    await sidebar(page, 'c_archive').getByRole('button', { name: '归档 Archive workflow verification', exact: true }).click()
    await page.locator('.archive-error').getByRole('button', { name: '重试归档操作', exact: true }).waitFor()
    await other.locator('.archive-notice').waitFor()
    const beforeRetry = (await request('/api/conversations/c_archive')).archive_revision
    await page.locator('.archive-error').getByRole('button', { name: '重试归档操作', exact: true }).click()
    await waitFor(async () => await page.locator('.archive-error').count() === 0, 'idempotent retry')
    assert.equal((await request('/api/conversations/c_archive')).archive_revision, beforeRetry)
    await other.locator('.archive-notice').getByRole('button', { name: '恢复会话', exact: true }).click()
    await waitFor(async () => await sidebar(page, 'c_archive').count() === 1, 'ready for stale request')

    mode = 'hold'
    const beforeHold = archiveRequests
    const revision = (await request('/api/conversations/c_archive')).archive_revision
    await sidebar(page, 'c_archive').hover()
    await sidebar(page, 'c_archive').getByRole('button', { name: '归档 Archive workflow verification', exact: true }).click()
    await waitFor(() => releaseArchive, 'held archive')
    const heldButton = sidebar(page, 'c_archive').getByRole('button', { name: '归档 Archive workflow verification', exact: true })
    assert.equal(await heldButton.isDisabled(), true)
    await heldButton.evaluate((button) => button.click())
    assert.equal(archiveRequests, beforeHold + 1, 'no duplicate submission')
    await archive('c_archive', true, revision)
    await archive('c_archive', false, revision + 1)
    releaseArchive(); releaseArchive = undefined; mode = 'normal'
    await page.locator('.archive-error').getByText('会话归档状态已在其他窗口变更，请刷新后重试', { exact: true }).waitFor()
    assert.equal(await page.locator('.archive-error').getByRole('button', { name: '重试归档操作' }).count(), 0)
    assert.equal((await request('/api/conversations/c_archive')).archived_at, null)
    await page.locator('.archive-error').getByRole('button', { name: '关闭归档提示' }).click()

    await request('/api/foreground-runs', { id: 'chat_archive_active', question: 'Foreground admission', conversation_id: 'c_archive' }, 'POST')
    await waitFor(async () => await sidebar(page, 'c_archive').getByRole('button', { name: '归档 Archive workflow verification', exact: true }).isDisabled(), 'running archive action disabled')
    await archive('c_archive', true, revision + 2, 409)
    await request('/api/foreground-runs/chat_archive_active', { revision: 1, status: 'interrupted', partial_answer: 'Preserved foreground checkpoint' }, 'PATCH')
    const archived = await archive('c_archive', true, revision + 2)
    await other.locator('.archive-notice').waitFor()
    const smallDialog = await openHistory(other)
    await historyRow(other, 'c_archive').waitFor()
    await historyRow(other, 'c_archive').hover()
    await waitFor(async () => await historyRow(other, 'c_archive').locator('.hist-item-actions').evaluate((el) => getComputedStyle(el).opacity === '1'), 'small history actions visible')
    await waitFor(async () => await other.locator('.el-message').count() === 0, 'notifications settled')
    await noOverflow(other)
    await other.screenshot({ path: path.join(root, 'verification/archive-history-small-desktop.png'), fullPage: true })
    await smallDialog.press('Escape')
    await page.context().close()
    await other.context().close()
    const previousPort = server.port
    await stop(server)
    server = await launch(env, previousPort)
    const restored = await newPage({ width: 1100, height: 700 })
    await restored.goto(server.base)
    await restored.locator('.archive-notice').waitFor()
    assert.equal(await restored.locator('.textarea').inputValue(), originalDraft.text, 'draft survives a different port and browser context')
    assert.equal((await request('/api/conversations/c_archive')).archive_revision, archived.archive_revision)
    assert.equal((await request('/api/tasks/task_retained')).result.answer, 'Retained result')
    await restored.locator('.archive-notice').getByRole('button', { name: '恢复会话', exact: true }).click()
    await waitFor(async () => await restored.locator('.archive-notice').count() === 0, 'restore after cold restart')
    assert.equal((await request('/api/foreground-runs?conversation_id=c_archive')).runs[0].partial_answer, 'Preserved foreground checkpoint')
    await archive('c_archive', true, archived.archive_revision + 1)
    const finalDialog = await openHistory(restored)
    await historyRow(restored, 'c_archive').hover()
    await historyRow(restored, 'c_archive').getByRole('button', { name: '删除', exact: true }).click()
    await waitFor(async () => await historyRow(restored, 'c_archive').count() === 0, 'delete archived conversation')
    await archive('c_archive', false, archived.archive_revision + 2, 410)
    await finalDialog.press('Escape')
    assert.deepEqual(errors, [])
    console.log('Archive: retention, read-only, tabs, two-window SSE, failures/retry, lost response, stale requests, active guard, cold restart and deletion passed at 1440x900 and 1100x700.')
  } finally {
    releaseArchive?.()
    await browser.close()
    await stop(server)
    const resolved = path.resolve(data)
    assert.ok(resolved.startsWith(path.resolve(root, 'verification/runtime') + path.sep))
    await fs.rm(resolved, { recursive: true, force: true })
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
