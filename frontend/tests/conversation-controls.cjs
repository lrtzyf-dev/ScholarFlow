// Real isolated SQLite + local provider HTTP + desktop browser controls.
// Only generated test credentials are used; no paid API or live data is touched.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const http = require('node:http')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const python = path.join(root, 'backend/.venv/Scripts/python.exe')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label) {
  const deadline = Date.now() + 18000
  while (!(await check())) {
    assert.ok(Date.now() < deadline, `Timed out: ${label}`)
    await pause(30)
  }
}
async function main() {
  const output = path.join(root, 'verification/conversation-controls')
  await fs.mkdir(output, { recursive: true })
  await fs.mkdir(path.join(root, 'verification/runtime'), { recursive: true })
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/conversation-controls-'))
  const env = {
    ...process.env,
    DATA_DIR: data,
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    MOCK_STREAM_DELAY: '0.008',
  }
  execFileSync(
    python,
    [
      '-X',
      'utf8',
      '-c',
      `
from app.db import init_db, SessionLocal
from app.models import Conversation, Paper, QARecord
from pathlib import Path
init_db()
with SessionLocal() as db:
    for name in ('a', 'b', 'fresh'):
        db.add(Conversation(id='c_controls_'+name, title='Controls '+name.upper()))
    db.add(QARecord(id='qa_zoom_controls', conversation_id='c_controls_a',
        question='Reading zoom fixture', answer='## Reading scale\\n\\nSynthetic readable answer text.'))
    db.add(Paper(id='paper_zoom_controls', title='Synthetic PDF zoom fixture',
        authors='Local test', abstract='A synthetic abstract for zoom verification.',
        pdf_path=str(Path('app/samples/ScholarFlow-demo.pdf').resolve()),
        page_count=1, status='parsed'))
    db.add(Paper(id='paper_compare_zoom_controls', title='Synthetic comparison zoom fixture',
        authors='Local test', abstract='A second synthetic paper for comparison zoom verification.',
        pdf_path=str(Path('app/samples/ScholarFlow-demo.pdf').resolve()),
        page_count=1, status='parsed'))
    db.commit()
`,
    ],
    { cwd: path.join(root, 'backend'), env, windowsHide: true }
  )
  let releaseDiscovery
  const providerCalls = []
  const provider = http.createServer(async (req, res) => {
    providerCalls.push({ path: req.url, auth: req.headers.authorization || '' })
    if (req.url === '/SlowCase/models')
      await new Promise((resolve) => {
        releaseDiscovery = resolve
      })
    if (req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(
        JSON.stringify({
          data: [
            {
              id: req.url === '/SlowCase/models' ? 'STALE-Model' : 'CaseSensitive-Model',
              owned_by: 'Local test provider',
            },
          ],
        })
      )
      return
    }
    res.writeHead(400, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ error: { message: 'Generated local test rejection' } }))
  })
  await new Promise((resolve) => provider.listen(0, '127.0.0.1', resolve))
  const providerBase = `http://127.0.0.1:${provider.address().port}`
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
        : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    )
    assert.equal(result.status, 200, await result.clone().text())
    return result.json()
  }
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  let holdFreshOptionsRead = false
  let freshOptionsReadHeld = false
  let releaseFreshOptionsRead
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/conversation-options/c_controls_fresh', async (route) => {
    if (route.request().method() === 'GET' && holdFreshOptionsRead) {
      holdFreshOptionsRead = false
      freshOptionsReadHeld = true
      await new Promise((resolve) => {
        releaseFreshOptionsRead = resolve
      })
    }
    await route.continue()
  })
  const modelButton = page.getByRole('button', { name: '选择会话模型', exact: true })
  const permission = page.getByRole('combobox', { name: '会话权限', exact: true })
  const composer = page.locator('textarea').first()
  async function selectModel(name) {
    await modelButton.click()
    await page.locator('.conversation-model-option').filter({ hasText: name }).click()
    await until(
      async () =>
        !(await modelButton.isDisabled()) && (await modelButton.textContent()).includes(name),
      `selected ${name}`
    )
  }
  async function openEditor() {
    await modelButton.click()
    await page
      .locator('.conversation-model-menu')
      .getByRole('button', { name: '添加模型', exact: true })
      .click()
    await page.getByRole('dialog', { name: '添加模型', exact: true }).waitFor()
  }
  try {
    await until(async () => {
      if (child.exitCode !== null) throw Error(log)
      try {
        return (await fetch(base + '/api/health')).ok
      } catch {
        return false
      }
    }, 'isolated server')
    const a = await request(
      '/model-profiles',
      {
        name: 'Research A',
        base_url: providerBase + '/ProviderA',
        model_id: 'SameCase-Model',
        api_key: 'generated-controls-key-a',
        reasoning_effort: 'high',
      },
      'POST'
    )
    const b = await request(
      '/model-profiles',
      {
        name: 'Research B',
        base_url: providerBase + '/ProviderB',
        model_id: 'SameCase-Model',
        api_key: 'generated-controls-key-b',
      },
      'POST'
    )
    const spare = await request(
      '/model-profiles',
      { name: 'Spare Model', base_url: providerBase + '/Spare', model_id: 'Spare-Model' },
      'POST'
    )
    assert.notEqual(a.id, b.id, 'same model ID on distinct endpoints remains distinct')
    assert.equal(JSON.stringify(a).includes('generated-controls-key-a'), false)
    await request('/conversation-options/c_controls_a', {
      expected_revision: 0,
      model_profile_id: a.id,
      approval_mode: 'always',
    })
    await request('/conversation-options/c_controls_b', {
      expected_revision: 0,
      model_profile_id: b.id,
      approval_mode: 'full_access',
    })
    await page.goto(base + '/chat?conv=c_controls_a')
    await until(
      async () =>
        (await modelButton.textContent()).includes('Research A') &&
        !(await modelButton.isDisabled()),
      'conversation A options'
    )
    const answerBody = page.locator('.markdown-answer').first()
    await answerBody.waitFor()
    const baseAnswerSize = Number.parseFloat(
      await answerBody.evaluate((el) => getComputedStyle(el).fontSize)
    )
    await page.keyboard.press('Control+Shift+=')
    await until(
      async () =>
        Number.parseFloat(await answerBody.evaluate((el) => getComputedStyle(el).fontSize)) >
        baseAnswerSize,
      'keyboard enlarges answer text'
    )
    const enlargedAnswerSize = Number.parseFloat(
      await answerBody.evaluate((el) => getComputedStyle(el).fontSize)
    )
    assert.ok(enlargedAnswerSize > baseAnswerSize * 1.05)
    assert.equal(
      await page.evaluate(() => localStorage.getItem('scholarflow:reading-text-scale')),
      '1.1'
    )
    await page.reload()
    await answerBody.waitFor()
    assert.equal(
      Number.parseFloat(await answerBody.evaluate((el) => getComputedStyle(el).fontSize)),
      enlargedAnswerSize,
      'reading text zoom survives navigation reload'
    )
    await page.keyboard.press('Control+0')
    await until(
      async () =>
        Number.parseFloat(await answerBody.evaluate((el) => getComputedStyle(el).fontSize)) ===
        baseAnswerSize,
      'keyboard restores answer text size'
    )
    await page.keyboard.press('Control+Minus')
    await until(
      async () =>
        Number.parseFloat(await answerBody.evaluate((el) => getComputedStyle(el).fontSize)) <
        baseAnswerSize,
      'keyboard reduces answer text'
    )
    await page.keyboard.press('Control+0')
    await until(
      async () =>
        Number.parseFloat(await answerBody.evaluate((el) => getComputedStyle(el).fontSize)) ===
        baseAnswerSize,
      'reset restores the default scale'
    )
    assert.equal(await permission.inputValue(), 'always')
    await composer.fill('保留这条尚未发送的研究问题')
    await modelButton.click()
    await page.getByRole('textbox', { name: '搜索会话模型' }).fill('Research B')
    assert.equal(await page.locator('.conversation-model-option').count(), 1)
    assert.equal(
      await page
        .locator('.conversation-model-option')
        .textContent()
        .then((text) => text.includes('工具能力未检测')),
      true
    )
    await page.getByRole('textbox', { name: '搜索会话模型' }).press('ArrowDown')
    assert.equal(
      await page.evaluate(() =>
        document.activeElement.classList.contains('conversation-model-option')
      ),
      true
    )
    await page.keyboard.press('Escape')
    assert.equal(await modelButton.evaluate((node) => node === document.activeElement), true)
    await selectModel('Research B')
    assert.equal((await request('/conversation-options/c_controls_a')).model_profile_id, b.id)
    assert.equal((await request('/conversation-options/c_controls_b')).approval_mode, 'full_access')
    await permission.selectOption('auto')
    await until(
      async () => (await request('/conversation-options/c_controls_a')).approval_mode === 'auto',
      'permission persistence'
    )
    assert.equal(await composer.inputValue(), '保留这条尚未发送的研究问题')
    await page.reload()
    await until(
      async () =>
        (await modelButton.textContent()).includes('Research B') &&
        !(await modelButton.isDisabled()),
      'reload model'
    )
    assert.equal(await permission.inputValue(), 'auto')
    assert.equal(await composer.inputValue(), '保留这条尚未发送的研究问题')
    // A conflict preserves intent and requires an explicit retry against latest revision.
    const latest = await request('/conversation-options/c_controls_a')
    await request('/conversation-options/c_controls_a', {
      expected_revision: latest.revision,
      approval_mode: 'full_access',
    })
    await permission.selectOption('always')
    await page.getByRole('button', { name: '重试保存我的选择' }).waitFor()
    assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isDisabled(), true)
    await page.getByRole('button', { name: '重试保存我的选择' }).click()
    await until(
      async () =>
        (await request('/conversation-options/c_controls_a')).approval_mode === 'always' &&
        !(await modelButton.isDisabled()),
      'conflict retry'
    )
    // Quick-add validation failure/cancel keeps the composer untouched, clears secrets.
    await openEditor()
    let dialog = page.getByRole('dialog', { name: '添加模型', exact: true })
    await dialog.getByLabel('配置名称', { exact: true }).fill('Invalid test')
    await dialog.getByLabel('接口地址', { exact: true }).fill('javascript:invalid')
    await dialog.getByLabel('API Key', { exact: true }).fill('generated-cancel-key')
    await dialog.locator('#profile-model-id').fill('Test-ID')
    await dialog.getByRole('button', { name: '保存模型', exact: true }).click()
    await dialog.getByRole('alert').waitFor()
    assert.equal(await composer.inputValue(), '保留这条尚未发送的研究问题')
    await page.keyboard.press('Escape')
    assert.equal(await dialog.count(), 0)
    assert.equal(await modelButton.evaluate((node) => node === document.activeElement), true)
    await openEditor()
    dialog = page.getByRole('dialog', { name: '添加模型', exact: true })
    assert.equal(await dialog.getByLabel('API Key', { exact: true }).inputValue(), '')
    await dialog.getByLabel('配置名称', { exact: true }).fill('Quick Added')
    await dialog.getByLabel('接口地址', { exact: true }).fill(providerBase + '/SlowCase')
    await dialog.getByLabel('API Key', { exact: true }).fill('generated-discovery-key')
    await dialog.getByRole('button', { name: '获取模型列表', exact: true }).click()
    await until(() => Boolean(releaseDiscovery), 'old provider discovery waiting')
    await dialog.getByLabel('接口地址', { exact: true }).fill(providerBase + '/FreshCase')
    releaseDiscovery()
    await until(
      async () =>
        !(await dialog.getByRole('button', { name: '获取模型列表', exact: true }).isDisabled()),
      'updated provider discovery available'
    )
    await dialog.getByRole('button', { name: '获取模型列表', exact: true }).click()
    await dialog.getByRole('combobox', { name: '从接口模型列表选择' }).waitFor()
    assert.equal(await dialog.textContent().then((text) => text.includes('STALE-Model')), false)
    await dialog
      .getByRole('combobox', { name: '从接口模型列表选择' })
      .selectOption('CaseSensitive-Model')
    assert.equal(await dialog.locator('#profile-model-id').inputValue(), 'CaseSensitive-Model')
    await dialog.getByRole('button', { name: '保存模型', exact: true }).click()
    await until(
      async () =>
        !(await modelButton.isDisabled()) &&
        (await modelButton.textContent()).includes('Quick Added'),
      'quick-add selects saved model'
    )
    assert.equal(await composer.inputValue(), '保留这条尚未发送的研究问题')
    assert.equal(
      providerCalls.some(
        (call) =>
          call.path === '/FreshCase/models' && call.auth === 'Bearer generated-discovery-key'
      ),
      true
    )
    const quick = (await request('/model-profiles')).profiles.find(
      (profile) => profile.name === 'Quick Added'
    )
    assert.equal(quick.model_id, 'CaseSensitive-Model')
    // The running round retains its snapshot while the next-message model changes.
    await selectModel('Research A')
    const run = await request(
      '/foreground-runs',
      {
        id: 'chat_controls_snapshot',
        conversation_id: 'c_controls_a',
        question: 'Synthetic admitted round',
      },
      'POST'
    )
    await page.reload()
    await page.locator('.conversation-running-config').waitFor()
    assert.equal(
      await page
        .locator('.conversation-running-config')
        .textContent()
        .then((text) => text.includes('Research A')),
      true
    )
    await selectModel('Research B')
    assert.equal(
      await page
        .locator('.conversation-running-config')
        .textContent()
        .then((text) => text.includes('Research A')),
      true
    )
    assert.equal(
      (await request('/foreground-runs?conversation_id=c_controls_a')).runs[0].runtime_config
        .model_id,
      'SameCase-Model'
    )
    assert.equal(
      (await request('/foreground-runs?conversation_id=c_controls_a')).runs[0].runtime_config
        .profile_id,
      a.id
    )
    for (const viewport of [
      { width: 1440, height: 900 },
      { width: 1100, height: 700 },
    ]) {
      await page.setViewportSize(viewport)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        'no document overflow'
      )
      for (const control of [
        modelButton,
        permission,
        page.getByRole('button', { name: '停止生成', exact: true }),
      ]) {
        const box = await control.boundingBox()
        assert.ok(
          box &&
            box.x >= 0 &&
            box.x + box.width <= viewport.width &&
            box.y >= 0 &&
            box.y + box.height <= viewport.height,
          'composer controls remain visible'
        )
      }
      await page.screenshot({ path: path.join(output, `composer-${viewport.width}.png`) })
      await modelButton.click()
      const menu = await page.locator('.conversation-model-menu').boundingBox()
      assert.ok(
        menu && menu.x >= 0 && menu.x + menu.width <= viewport.width && menu.y >= 0,
        'model menu fits viewport'
      )
      await page.screenshot({ path: path.join(output, `model-menu-${viewport.width}.png`) })
      await page.keyboard.press('Escape')
    }
    await request(
      '/foreground-runs/chat_controls_snapshot',
      { revision: run.revision, status: 'cancelled' },
      'PATCH'
    )
    // Different conversation options survive navigation and pending UI mutations.
    await page
      .locator('.recent-conversation-row')
      .filter({ hasText: 'Controls B' })
      .locator('button')
      .first()
      .click()
    await until(
      async () => (await permission.inputValue()) === 'full_access',
      'conversation B independent permission'
    )
    assert.equal((await request('/conversation-options/c_controls_a')).approval_mode, 'always')
    // Settings manager shares the catalog; disabled entries cannot be selected.
    await page.goto(base + '/settings')
    await page.locator(`[data-profile-id="${spare.id}"]`).waitFor()
    await page
      .locator(`[data-profile-id="${spare.id}"]`)
      .getByRole('button', { name: '停用 Spare Model', exact: true })
      .click()
    await page
      .locator(`[data-profile-id="${spare.id}"]`)
      .getByText('已停用', { exact: true })
      .waitFor()
    await page
      .locator(`[data-profile-id="${b.id}"]`)
      .getByRole('button', { name: '设为默认', exact: true })
      .click()
    await page.locator(`[data-profile-id="${b.id}"]`).getByText('默认', { exact: true }).waitFor()
    await page
      .locator(`[data-profile-id="${b.id}"]`)
      .getByRole('button', { name: '测试连接', exact: true })
      .click()
    await page
      .locator('.model-profiles-manager')
      .getByRole('alert')
      .filter({ hasText: 'HTTP 400' })
      .waitFor()
    // Explicit key clearing is available without revealing a saved key.
    await page
      .locator(`[data-profile-id="${a.id}"]`)
      .getByRole('button', { name: '编辑 Research A', exact: true })
      .click()
    const edit = page.getByRole('dialog', { name: '编辑模型', exact: true })
    assert.equal(await edit.getByLabel('API Key', { exact: true }).inputValue(), '')
    await edit.getByRole('checkbox', { name: '清除已保存密钥，使用无密钥接口' }).check()
    assert.equal(await edit.getByLabel('API Key', { exact: true }).isDisabled(), true)
    await edit.getByRole('button', { name: '保存模型', exact: true }).click()
    await until(
      async () =>
        (await request('/model-profiles')).profiles.find((profile) => profile.id === a.id)
          .api_key_set === false,
      'explicit saved-key clearing'
    )
    await page.screenshot({ path: path.join(output, 'model-settings-1100.png') })
    await request('/model-profiles/default', { model_profile_id: 'legacy-default' })
    // A first run freezes inherited revision 0 as revision 1. Selection stays
    // usable while the post-admission GET is held, and its CAS uses revision 1.
    await page.goto(base + '/chat?conv=c_controls_fresh')
    await until(async () => !(await modelButton.isDisabled()), 'fresh conversation revision zero')
    assert.equal((await request('/conversation-options/c_controls_fresh')).revision, 0)
    await composer.fill('首次运行后立即变更会话权限')
    holdFreshOptionsRead = true
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await until(() => freshOptionsReadHeld, 'first-run revision refresh held')
    await until(
      async () => (await page.getByRole('button', { name: '停止生成', exact: true }).count()) === 1,
      'first run active'
    )
    await permission.selectOption('always')
    try {
      await until(async () => {
        const current = await request('/conversation-options/c_controls_fresh')
        return current.revision === 2 && current.approval_mode === 'always'
      }, 'first-run immediately followed by permission update')
    } catch {
      throw new Error(
        JSON.stringify({
          current: await request('/conversation-options/c_controls_fresh'),
          permission: await permission.inputValue(),
          error: await page
            .locator('.conversation-options-error')
            .textContent()
            .catch(() => ''),
        })
      )
    }
    releaseFreshOptionsRead()
    await until(
      async () => !(await page.getByRole('button', { name: '停止生成', exact: true }).count()),
      'synthetic first run completes'
    )
    await page.goto(base + '/chat?conv=c_controls_b')
    await until(async () => !(await modelButton.isDisabled()), 'return conversation')
    await modelButton.click()
    assert.equal(
      await page.locator('.conversation-model-option').filter({ hasText: 'Spare Model' }).count(),
      0
    )
    await page.keyboard.press('Escape')
    await page.goto(base + '/reader/paper_zoom_controls?page=1&bbox=100,100,180,120')
    const pdfCanvas = page.locator('.pdf-page canvas')
    await pdfCanvas.waitFor()
    await page.locator('.bbox-highlight').waitFor()
    const initialCanvasWidth = await pdfCanvas.evaluate((canvas) => canvas.width)
    const initialHighlightWidth = Number.parseFloat(
      await page.locator('.bbox-highlight').evaluate((el) => getComputedStyle(el).width)
    )
    await page.keyboard.press('Control+Shift+=')
    await until(
      async () => (await pdfCanvas.evaluate((canvas) => canvas.width)) > initialCanvasWidth,
      'keyboard enlarges rendered PDF'
    )
    await until(
      async () =>
        Number.parseFloat(
          await page.locator('.bbox-highlight').evaluate((el) => getComputedStyle(el).width)
        ) > initialHighlightWidth,
      'citation highlight follows PDF rerender'
    )
    const zoomedCanvasWidth = await pdfCanvas.evaluate((canvas) => canvas.width)
    assert.ok(zoomedCanvasWidth > initialCanvasWidth)
    const zoomedHighlightWidth = Number.parseFloat(
      await page.locator('.bbox-highlight').evaluate((el) => getComputedStyle(el).width)
    )
    assert.ok(zoomedHighlightWidth > initialHighlightWidth, 'citation highlight follows PDF zoom')
    assert.equal(await page.locator('.reader-zoom-value').textContent(), '110%')
    assert.equal(await page.evaluate(() => localStorage.getItem('scholarflow:reader-zoom')), '1.1')
    await page.getByRole('button', { name: '恢复适合窗口', exact: true }).click()
    await until(
      async () => (await page.locator('.reader-zoom-value').textContent()) === '100%',
      'PDF reset updates displayed zoom'
    )
    await until(
      async () =>
        (await pdfCanvas.evaluate((canvas) => canvas.width)) === initialCanvasWidth &&
        Math.abs(
          Number.parseFloat(
            await page.locator('.bbox-highlight').evaluate((el) => getComputedStyle(el).width)
          ) - initialHighlightWidth
        ) <= 1,
      'PDF reset returns to fit scale'
    )
    const resetHighlightWidth = Number.parseFloat(
      await page.locator('.bbox-highlight').evaluate((el) => getComputedStyle(el).width)
    )
    assert.ok(Math.abs(resetHighlightWidth - initialHighlightWidth) <= 1)
    assert.equal(await page.locator('.reader-zoom-value').textContent(), '100%')
    for (const viewport of [
      { width: 1100, height: 700 },
      { width: 390, height: 780 },
    ]) {
      await page.setViewportSize(viewport)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        `reader controls fit ${viewport.width}px viewport`
      )
      for (const control of [
        page.getByRole('button', { name: '缩小原文', exact: true }),
        page.getByRole('button', { name: '放大原文', exact: true }),
        page.getByRole('button', { name: '恢复适合窗口', exact: true }),
      ]) {
        const bounds = await control.boundingBox()
        assert.ok(
          bounds && bounds.x >= 0 && bounds.x + bounds.width <= viewport.width,
          `reader zoom control fits ${viewport.width}px viewport`
        )
      }
      await page.screenshot({
        path: path.join(output, `reader-zoom-${viewport.width}.png`),
        fullPage: false,
      })
    }
    await page.setViewportSize({ width: 1440, height: 900 })

    await page.goto(base + '/compare')
    const compareTitle = page.locator('.select-title').first()
    await compareTitle.waitFor()
    const baseCompareTitleSize = Number.parseFloat(
      await compareTitle.evaluate((el) => getComputedStyle(el).fontSize)
    )
    await page.locator('.select-item input').nth(0).check()
    await page.locator('.select-item input').nth(1).check()
    await page.getByRole('button', { name: '开始对比' }).click()
    const comparisonTable = page.locator('.compare-table')
    await comparisonTable.waitFor()
    const comparisonEmptyCell = page.locator('.ev-none').first()
    const baseComparisonTextSize = Number.parseFloat(
      await comparisonEmptyCell.evaluate((el) => getComputedStyle(el).fontSize)
    )
    const baseComparisonTableSize = Number.parseFloat(
      await comparisonTable.evaluate((el) => getComputedStyle(el).fontSize)
    )
    await page.locator('.page-title').click()
    await page.keyboard.press('Control+Shift+=')
    await until(
      async () =>
        Number.parseFloat(
          await comparisonEmptyCell.evaluate((el) => getComputedStyle(el).fontSize)
        ) > baseComparisonTextSize &&
        Number.parseFloat(await comparisonTable.evaluate((el) => getComputedStyle(el).fontSize)) >
          baseComparisonTableSize,
      'keyboard enlarges cross-paper comparison table text'
    )
    assert.equal(
      await page.evaluate(() => localStorage.getItem('scholarflow:reading-text-scale')),
      '1.1'
    )
    const comparisonScroller = page.locator('.table-card')
    for (const viewport of [
      { width: 1100, height: 700 },
      { width: 390, height: 780 },
    ]) {
      await page.setViewportSize(viewport)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        `comparison results fit ${viewport.width}px viewport`
      )
    }
    assert.ok(
      (await comparisonScroller.evaluate((el) => el.scrollWidth)) >
        (await comparisonScroller.evaluate((el) => el.clientWidth)),
      'narrow comparison table scrolls inside its result area'
    )
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.reload()
    await compareTitle.waitFor()
    await until(
      async () =>
        Number.parseFloat(await compareTitle.evaluate((el) => getComputedStyle(el).fontSize)) >
        baseCompareTitleSize,
      'comparison reading scale persists after reload'
    )
    await page.locator('.page-title').click()
    await page.keyboard.press('Control+Minus')
    await until(
      async () =>
        Number.parseFloat(await compareTitle.evaluate((el) => getComputedStyle(el).fontSize)) ===
        baseCompareTitleSize,
      'keyboard reduces cross-paper comparison text'
    )
    await page.keyboard.press('Control+0')
    for (const viewport of [
      { width: 1100, height: 700 },
      { width: 390, height: 780 },
    ]) {
      await page.setViewportSize(viewport)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        `comparison page fits ${viewport.width}px viewport`
      )
    }
    await page.setViewportSize({ width: 1440, height: 900 })

    await page.goto(base + '/translate')
    await page.getByRole('button', { name: '对照翻译' }).click()
    await page.locator('#translation-source').fill('这是用于验证阅读缩放的本地测试段落。')
    await page.getByRole('button', { name: '生成对照' }).click()
    const translationUnit = page.locator('.parallel-grid .unit').first()
    await translationUnit.waitFor()
    const baseTranslationUnitSize = Number.parseFloat(
      await translationUnit.evaluate((el) => getComputedStyle(el).fontSize)
    )
    const sourceInput = page.locator('#translation-source')
    const baseSourceInputSize = Number.parseFloat(
      await sourceInput.evaluate((el) => getComputedStyle(el).fontSize)
    )
    await page.locator('.parallel-card .col-head').click()
    await page.keyboard.press('Control+Shift+=')
    await until(
      async () =>
        Number.parseFloat(await translationUnit.evaluate((el) => getComputedStyle(el).fontSize)) >
          baseTranslationUnitSize &&
        Number.parseFloat(await sourceInput.evaluate((el) => getComputedStyle(el).fontSize)) >
          baseSourceInputSize,
      'keyboard enlarges translation source and result text'
    )
    assert.equal(
      await page.evaluate(() => localStorage.getItem('scholarflow:reading-text-scale')),
      '1.1'
    )
    for (const viewport of [
      { width: 1100, height: 700 },
      { width: 390, height: 780 },
    ]) {
      await page.setViewportSize(viewport)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        `parallel translation fits ${viewport.width}px viewport`
      )
      const [sourceBounds, translationBounds] = await Promise.all([
        page.locator('.parallel-grid .pane').nth(0).boundingBox(),
        page.locator('.parallel-grid .pane').nth(1).boundingBox(),
      ])
      assert.ok(
        sourceBounds &&
          translationBounds &&
          sourceBounds.x + sourceBounds.width <= translationBounds.x + 1 &&
          translationBounds.x + translationBounds.width <= viewport.width,
        `parallel columns do not overlap at ${viewport.width}px`
      )
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    await sourceInput.focus()
    const scaleBeforeEditingShortcut = await page.evaluate(() =>
      localStorage.getItem('scholarflow:reading-text-scale')
    )
    const editingShortcutWasIgnored = await page.evaluate(() =>
      document.activeElement.dispatchEvent(
        new KeyboardEvent('keydown', { key: '-', ctrlKey: true, bubbles: true, cancelable: true })
      )
    )
    assert.equal(editingShortcutWasIgnored, true)
    assert.equal(
      await page.evaluate(() => localStorage.getItem('scholarflow:reading-text-scale')),
      scaleBeforeEditingShortcut,
      'editing shortcuts do not change the reading scale'
    )
    await page.reload()
    const reloadedSourceInput = page.locator('#translation-source')
    await reloadedSourceInput.waitFor()
    assert.ok(
      Number.parseFloat(await reloadedSourceInput.evaluate((el) => getComputedStyle(el).fontSize)) >
        baseSourceInputSize,
      'translation reading scale persists after reload'
    )
    await page.locator('.page-title').click()
    await page.keyboard.press('Control+Minus')
    await until(
      async () =>
        Number.parseFloat(
          await reloadedSourceInput.evaluate((el) => getComputedStyle(el).fontSize)
        ) === baseSourceInputSize,
      'keyboard reduces translation text'
    )
    await page.keyboard.press('Control+0')
    for (const viewport of [
      { width: 1100, height: 700 },
      { width: 390, height: 780 },
    ]) {
      await page.setViewportSize(viewport)
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        `translation page fits ${viewport.width}px viewport`
      )
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(base + '/graph')
    await page.keyboard.press('Control+Shift+=')
    assert.equal(
      await page.evaluate(() => localStorage.getItem('scholarflow:reading-text-scale')),
      '1',
      'graph shortcut does not change the text reading scale'
    )
    const publicState = await page.evaluate(() =>
      JSON.stringify({
        local: { ...localStorage },
        session: { ...sessionStorage },
        text: document.body.textContent,
      })
    )
    for (const secret of [
      'generated-controls-key-a',
      'generated-controls-key-b',
      'generated-cancel-key',
      'generated-discovery-key',
    ])
      assert.equal(
        publicState.includes(secret),
        false,
        'no plaintext key in browser storage or visible messages'
      )
    assert.deepEqual(errors, [], 'no browser runtime exceptions')
    await fs.writeFile(
      path.join(output, 'result.json'),
      JSON.stringify(
        {
          passed: true,
          checks: [
            'independent conversation model/permission persistence',
            'case-sensitive provider/model identifiers',
            'keyboard and Escape focus',
            'CAS retry preserves user intent',
            'quick add validation/cancel/draft preservation',
            'stale discovery response discarded',
            'quick add discovery and selection',
            'active snapshot differs from next-message model',
            '1440x900 and 1100x700 visible controls/no overflow',
            'settings default/disable/error/explicit key clearing',
            'chat text zoom keyboard/persistence/reset',
            'PDF zoom keyboard/buttons/reset and citation highlight alignment',
            'comparison and translation text zoom in/out/persistence/reset',
            'text editing shortcuts do not change reading scale',
            'comparison table horizontal scrolling and translation column layout at 1100px and 390px',
            'graph shortcut leaves the text reading scale unchanged',
            'reader zoom controls fit 1100px and 390px viewports',
            'no plaintext key storage or page errors',
          ],
          screenshots: [
            'composer-1440.png',
            'model-menu-1440.png',
            'composer-1100.png',
            'model-menu-1100.png',
            'model-settings-1100.png',
            'reader-zoom-1100.png',
            'reader-zoom-390.png',
          ],
        },
        null,
        2
      )
    )
    console.log(
      'PASS conversation controls: isolated HTTP, saved models, permissions, conflict recovery, quick add, discovery races, active snapshot, settings, keyboard, two viewport sizes'
    )
  } finally {
    releaseDiscovery?.()
    releaseFreshOptionsRead?.()
    await browser.close()
    child.kill()
    await new Promise((resolve) =>
      child.exitCode !== null ? resolve() : child.once('exit', resolve)
    )
    provider.closeAllConnections()
    await new Promise((resolve) => provider.close(resolve))
    await fs
      .rm(data, { recursive: true, force: true, maxRetries: 12, retryDelay: 100 })
      .catch((error) => console.warn('Isolated test data retained for inspection:', error.code))
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
