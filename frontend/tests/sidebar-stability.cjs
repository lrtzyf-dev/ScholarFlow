// Isolated SQLite/HTTP/SSE: project-only grouping and stable layout under live invalidations.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label) {
  const deadline = Date.now() + 18000
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(40)
  }
}
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/sidebar-stability-'))
  const env = {
    ...process.env,
    DATA_DIR: data,
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
  }
  execFileSync(
    python,
    [
      '-X',
      'utf8',
      '-c',
      `
from datetime import timedelta
from pathlib import Path
from app.db import init_db, SessionLocal
from app.models import Conversation
from app.timeutil import utcnow
from app import workspace
init_db()
with SessionLocal() as db:
    root = Path(${JSON.stringify(data).replaceAll('\\\\', '/')})
    large = workspace.register(db, str(root), 'Large project')
    small_dir = root / 'small'
    small_dir.mkdir()
    small = workspace.register(db, str(small_dir), 'Small project')
    at = utcnow()
    for i in range(85):
        db.add(Conversation(id=f'c_plain_{i:03}', title=f'Plain chat {i:03}',
          created_at=at-timedelta(minutes=200-i), updated_at=at-timedelta(minutes=200-i),
          pinned_at=at if i < 2 else None, pin_position=0 if i == 0 else 2 if i == 1 else None))
        db.add(Conversation(id=f'c_project_{i:03}', title=f'Project chat {i:03}', project_id=large.id,
          created_at=at-timedelta(minutes=i), updated_at=at-timedelta(minutes=i),
          pinned_at=at if i == 0 else None, pin_position=1 if i == 0 else None))
    db.add(Conversation(id='c_small', title='Small project chat', project_id=small.id))
    db.add(Conversation(id='c_orphan', title='Unavailable project chat', project_id='missing-project'))
    db.add(Conversation(id='c_bindable', title='Move into project'))
    db.commit()
`,
    ],
    { cwd: path.join(root, 'backend'), env, windowsHide: true }
  )
  const reservation = net.createServer()
  await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve))
  const port = reservation.address().port
  await new Promise((resolve) => reservation.close(resolve))
  const child = spawn(
    python,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
    { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let log = ''
  child.stdout.on('data', (chunk) => {
    log += chunk
  })
  child.stderr.on('data', (chunk) => {
    log += chunk
  })
  const base = `http://127.0.0.1:${port}`
  const request = async (url, body, method = 'PUT') => {
    const result = await fetch(
      base + '/api' + url,
      body === undefined
        ? undefined
        : {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          }
    )
    assert.equal(result.status, 200, await result.clone().text())
    return result.json()
  }
  const browser = await chromium.launch({ headless: true })
  try {
    await until(async () => {
      if (child.exitCode !== null) throw Error(log)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'isolated server')
    const projects = await request('/projects')
    const small = projects.find((project) => project.name === 'Small project')
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const other = await browser.newPage({ viewport: { width: 1100, height: 700 } })
    const errors = []
    for (const view of [page, other]) view.on('pageerror', (error) => errors.push(error.message))
    const rows = (view) => view.locator('.recent-conversation-row')
    await Promise.all([page.goto(base + '/library'), other.goto(base + '/library')])
    await until(
      async () => (await rows(page).count()) === 40 && (await rows(other).count()) === 40,
      'full ordinary first page'
    )
    assert.equal(await rows(page).filter({ hasText: 'Project chat' }).count(), 0)
    assert.equal(await rows(page).filter({ hasText: 'Unavailable project' }).count(), 0)
    assert.equal(await page.locator('[data-pin-handle-id]').count(), 2, 'project pins excluded')
    await page.locator('[data-pin-handle-id="c_plain_001"]').press('Alt+ArrowUp')
    await until(async () => {
      const order = await page
        .locator('[data-pin-handle-id]')
        .evaluateAll((nodes) => nodes.map((n) => n.dataset.pinHandleId))
      return order.join(',') === 'c_plain_001,c_plain_000'
    }, 'ordinary pin reorder')
    const all = (await request('/conversations')).conversations
    assert.equal(
      all.find((chat) => chat.id === 'c_project_000').pin_position,
      1,
      'hidden project pin position preserved'
    )
    await page.locator('.conversation-load-more').click()
    await until(async () => (await rows(page).count()) === 80, 'ordinary second page')
    await request('/conversations/c_bindable/project', { project_id: small.id })
    await until(
      async () =>
        (await rows(page).filter({ hasText: 'Move into project' }).count()) === 0 &&
        (await rows(other).filter({ hasText: 'Move into project' }).count()) === 0,
      'binding sync removes ordinary row'
    )
    await page.locator('.project-expand').filter({ hasText: 'Large project' }).click()
    await until(
      async () => (await page.locator('.project-chats > button[title]').count()) === 40,
      'project first page'
    )
    await page.locator('.project-chats > button').filter({ hasText: '加载更多' }).click()
    await until(
      async () => (await page.locator('.project-chats > button[title]').count()) === 80,
      'project second page'
    )
    await request('/conversations/c_project_000', { title: 'Project chat 000 updated' })
    await page.locator('.project-chats > button[title="Project chat 000 updated"]').waitFor()
    assert.equal(
      await page.locator('.project-chats > button[title]').count(),
      80,
      'metadata refresh retains expanded project page'
    )
    await page.locator('.project-expand').filter({ hasText: 'Small project' }).click()
    await until(
      async () => (await page.locator('.project-chats > button[title]').count()) === 2,
      'small project expanded'
    )
    let projectRefreshes = 0
    await page.route(`**/api/projects/${small.id}/conversations*`, async (route) => {
      projectRefreshes++
      await pause(200)
      await route.continue()
    })
    await page.bringToFront()
    const measurements = []
    for (const viewport of [
      { width: 1440, height: 900 },
      { width: 1100, height: 700 },
    ]) {
      await page.setViewportSize(viewport)
      await page.evaluate(() => {
        window.sidebarSamples = []
        window.sidebarBusySamples = 0
        const sample = () => {
          const selectors = [
            '.research-sidebar',
            '.research-projects',
            '.ctx-conversations',
            '.research-nav',
            '.sidebar-footer',
          ]
          window.sidebarSamples.push(
            selectors.map((selector) => {
              const box = document.querySelector(selector).getBoundingClientRect()
              return [box.x, box.y, box.width, box.height]
            })
          )
          if (document.querySelector('.project-chats[aria-busy="true"]'))
            window.sidebarBusySamples++
        }
        sample()
        window.sidebarSampleTimer = setInterval(sample, 16)
      })
      for (let i = 0; i < 5; i++) {
        await request('/conversations/c_small', { title: 'Small project chat' })
        await pause(420)
      }
      const { samples, busySamples } = await page.evaluate(() => {
        clearInterval(window.sidebarSampleTimer)
        return { samples: window.sidebarSamples, busySamples: window.sidebarBusySamples }
      })
      assert.ok(samples.length > 20, 'captured multiple layout frames')
      assert.ok(busySamples > 5, 'sampled delayed metadata refreshes while busy')
      const delta = Math.max(
        ...samples.flatMap((sample) =>
          sample.flatMap((box, index) =>
            box.map((value, dimension) => Math.abs(value - samples[0][index][dimension]))
          )
        )
      )
      assert.ok(delta < 1, `sidebar geometry stable during refresh; maximum change ${delta}px`)
      measurements.push({
        ...viewport,
        samples: samples.length,
        busySamples,
        maximumShiftPx: delta,
      })
      await page.screenshot({
        path: path.join(root, `verification/sidebar-stability-${viewport.width}.png`),
        animations: 'disabled',
      })
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    }
    assert.ok(projectRefreshes >= 6, 'real SSE invalidations refresh project metadata')
    const normalIds = (
      await request('/conversations/page?scope=active&project_scope=unassigned&limit=100')
    ).conversations.map((chat) => chat.id)
    assert.equal(normalIds.length, 85)
    assert.ok(normalIds.every((id) => id.startsWith('c_plain_')))
    const search = await request('/conversations/page?q=Project%20chat&scope=all')
    assert.ok(search.total >= 85, 'global history/search retains project conversations')
    assert.deepEqual(errors, [])
    console.log(
      'PASS: ordinary/project grouping, bounded pagination, cross-window binding, project page retention, hidden-pin preservation, live layout stability',
      JSON.stringify(measurements)
    )
  } finally {
    await browser.close()
    const ended = new Promise((resolve) => child.once('exit', resolve))
    child.kill()
    await ended
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
