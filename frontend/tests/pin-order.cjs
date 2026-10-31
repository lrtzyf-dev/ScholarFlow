// Real SQLite, SSE, two desktop windows and a cold restart on another port.
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
  while (!await fn()) { assert.ok(Date.now() < deadline, `Timed out: ${label}`); await pause(40) }
}
async function freePort() {
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
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
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/pin-order-'))
  const env = { ...process.env, DATA_DIR: data, LLM_API_KEY: '', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false' }
  execFileSync(python, ['-c', `
from datetime import datetime, timedelta
from app.db import init_db, SessionLocal
from app.models import Conversation
init_db()
with SessionLocal() as db:
    for i, name in enumerate(('a', 'b', 'c', 'd')):
        db.add(Conversation(id='c_' + name, title='Research ' + name.upper(),
            pinned_at=datetime.utcnow() + timedelta(seconds=i) if i < 3 else None,
            pin_position=2-i if i < 3 else None))
    db.commit()
`], { cwd: path.join(root, 'backend'), env, windowsHide: true })
  const browser = await chromium.launch({ headless: true })
  let server = await launch(env)
  const errors = []
  let releaseOrder
  const request = async (url, body, method = 'PUT') => {
    const response = await fetch(`${server.base}${url}`, body === undefined ? undefined : {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    assert.equal(response.status, 200, `${url}: ${await response.clone().text()}`)
    return response.json()
  }
  const newPage = async (viewport) => {
    const context = await browser.newContext({ viewport })
    const page = await context.newPage()
    page.on('pageerror', (error) => errors.push(error.message))
    return page
  }
  const row = (page, id) => page.locator(`.recent-conversation-row[data-conversation-id="${id}"]`)
  const handle = (page, id) => page.locator(`[data-pin-handle-id="${id}"]`)
  const order = (page) => page.locator('[data-pin-handle-id]').evaluateAll((nodes) => nodes.map((node) => node.dataset.pinHandleId))
  const expectOrder = async (page, ids) => waitFor(async () => JSON.stringify(await order(page)) === JSON.stringify(ids), `order ${ids}`)
  const moveWithKey = async (page, id, key) => {
    await waitFor(async () => !await handle(page, id).isDisabled(), 'ordering controls ready')
    await handle(page, id).press(key)
  }
  try {
    const page = await newPage({ width: 1440, height: 900 })
    const other = await newPage({ width: 1100, height: 700 })
    await page.goto(`${server.base}/library`)
    await other.goto(`${server.base}/library`)
    await expectOrder(page, ['c_c', 'c_b', 'c_a'])
    await expectOrder(other, ['c_c', 'c_b', 'c_a'])
    await row(page, 'c_d').hover()
    await row(page, 'c_d').getByRole('button', { name: '固定 Research D', exact: true }).click()
    await expectOrder(other, ['c_d', 'c_c', 'c_b', 'c_a'])
    await row(page, 'c_d').getByRole('button', { name: '取消固定 Research D', exact: true }).click()
    await expectOrder(page, ['c_c', 'c_b', 'c_a'])

    await moveWithKey(page, 'c_a', 'Alt+ArrowUp')
    await expectOrder(page, ['c_c', 'c_a', 'c_b'])
    await expectOrder(other, ['c_c', 'c_a', 'c_b'])
    await waitFor(async () => await handle(page, 'c_a').evaluate((button) => button === document.activeElement), 'keyboard focus restored after move')
    await handle(page, 'c_a').click()
    const menu = page.getByRole('menu', { name: '固定会话顺序' })
    await menu.waitFor()
    await menu.getByRole('menuitem', { name: '移到首位', exact: true }).press('End')
    assert.equal(await menu.getByRole('menuitem', { name: '移到末位', exact: true }).evaluate((button) => button === document.activeElement), true)
    await page.screenshot({ path: path.join(root, 'verification/pin-order-desktop.png'), fullPage: true })
    await menu.getByRole('menuitem', { name: '移到末位', exact: true }).press('Enter')
    await expectOrder(page, ['c_c', 'c_b', 'c_a'])
    await waitFor(async () => !await handle(page, 'c_a').isDisabled(), 'drag controls ready')
    await handle(page, 'c_a').dragTo(row(page, 'c_c'), { targetPosition: { x: 70, y: 2 } })
    await expectOrder(page, ['c_a', 'c_c', 'c_b'])
    await expectOrder(other, ['c_a', 'c_c', 'c_b'])
    await waitFor(async () => !await handle(other, 'c_b').isDisabled(), 'second window drag controls ready')
    await handle(other, 'c_b').dragTo(row(other, 'c_a'), { targetPosition: { x: 70, y: 28 } })
    await expectOrder(page, ['c_a', 'c_b', 'c_c'])

    let mode = 'fail', orderRequests = 0
    await page.route('**/api/conversations/pins/order', async (route) => {
      orderRequests++
      if (mode === 'fail') return route.fulfill({ status: 503, json: { detail: 'Injected save failure' } })
      if (mode === 'lost') {
        mode = 'normal'
        const committed = await route.fetch()
        assert.equal(committed.status(), 200)
        return route.abort('failed')
      }
      if (mode === 'hold') await new Promise((resolve) => { releaseOrder = resolve })
      await route.continue()
    })
    await moveWithKey(page, 'c_c', 'Alt+ArrowUp')
    await page.getByText('固定顺序尚未保存', { exact: false }).waitFor()
    await expectOrder(page, ['c_a', 'c_b', 'c_c'])
    mode = 'normal'
    await page.getByRole('button', { name: '重试排序', exact: true }).click()
    await expectOrder(other, ['c_a', 'c_c', 'c_b'])
    await expectOrder(page, ['c_a', 'c_c', 'c_b'])
    mode = 'lost'
    await moveWithKey(page, 'c_b', 'Alt+Home')
    await page.getByRole('button', { name: '重试排序', exact: true }).waitFor()
    await expectOrder(other, ['c_b', 'c_a', 'c_c'])
    await page.getByRole('button', { name: '重试排序', exact: true }).click()
    await waitFor(async () => await page.getByRole('button', { name: '重试排序', exact: true }).count() === 0, 'lost response idempotent retry')

    // A request based on an old order must not overwrite the other window.
    mode = 'hold'
    const before = orderRequests
    await moveWithKey(page, 'c_c', 'Alt+ArrowUp')
    await waitFor(() => releaseOrder, 'held reorder request')
    assert.equal(await handle(page, 'c_b').isDisabled(), true)
    assert.equal(await row(page, 'c_a').getByRole('button', { name: '取消固定 Research A', exact: true }).isDisabled(), true)
    await handle(page, 'c_b').evaluate((button) => button.click())
    assert.equal(orderRequests, before + 1, 'one pending reorder request')
    await request('/api/conversations/pins/order', { conversation_ids: ['c_c', 'c_b', 'c_a'], expected_order: ['c_b', 'c_a', 'c_c'] })
    await expectOrder(other, ['c_c', 'c_b', 'c_a'])
    releaseOrder()
    releaseOrder = undefined
    await page.getByText('固定会话已在其他窗口变更，请重新排序', { exact: true }).waitFor()
    await expectOrder(page, ['c_c', 'c_b', 'c_a'])
    assert.equal(await page.getByRole('button', { name: '重试排序', exact: true }).count(), 0)
    await page.getByRole('button', { name: '关闭排序提示', exact: true }).click()

    await moveWithKey(page, 'c_c', 'Alt+End')
    await waitFor(() => releaseOrder, 'held order during delete')
    const response = await fetch(`${server.base}/api/conversations/c_c`, { method: 'DELETE' })
    assert.equal(response.status, 200)
    releaseOrder()
    releaseOrder = undefined
    await page.getByText('固定会话已在其他窗口变更，请重新排序', { exact: true }).waitFor()
    await expectOrder(other, ['c_b', 'c_a'])
    assert.equal(await row(other, 'c_c').count(), 0)
    mode = 'normal'
    await handle(other, 'c_a').click()
    const smallMenu = other.getByRole('menu', { name: '固定会话顺序' })
    await smallMenu.waitFor()
    assert.equal(await smallMenu.getByRole('menuitem', { name: '下移', exact: true }).isDisabled(), true)
    await other.screenshot({ path: path.join(root, 'verification/pin-order-small-desktop.png'), fullPage: true })
    await smallMenu.getByRole('menuitem', { name: '上移', exact: true }).press('Escape')
    assert.equal(await smallMenu.count(), 0)
    assert.equal(await handle(other, 'c_a').evaluate((button) => button === document.activeElement), true)
    for (const current of [page, other]) assert.ok(await current.evaluate(() => document.documentElement.scrollWidth <= innerWidth))

    await page.context().close()
    await other.context().close()
    const firstPort = server.port
    await stop(server)
    server = await launch(env)
    assert.notEqual(server.port, firstPort)
    const restored = await newPage({ width: 1100, height: 700 })
    await restored.goto(`${server.base}/library`)
    await expectOrder(restored, ['c_b', 'c_a'])
    await moveWithKey(restored, 'c_a', 'Alt+Home')
    await expectOrder(restored, ['c_a', 'c_b'])
    await restored.reload()
    await expectOrder(restored, ['c_a', 'c_b'])
    const rows = (await request('/api/conversations')).conversations
    assert.equal(rows.find((item) => item.id === 'c_a').pin_position, 0)
    assert.equal(rows.find((item) => item.id === 'c_b').pin_position, 1)
    assert.equal(rows.some((item) => item.id === 'c_c'), false)
    assert.deepEqual(errors, [])
    console.log('PASS: real drag/drop before/after, keyboard and menu ordering/focus/boundaries, pin/unpin, SQLite/SSE two-window sync, save failure/retry, lost response idempotency, duplicate guard, concurrent reorder/delete conflict, cold restart/new origin, refresh persistence, desktop bounds')
  } finally {
    releaseOrder?.()
    await browser.close()
    await stop(server)
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
