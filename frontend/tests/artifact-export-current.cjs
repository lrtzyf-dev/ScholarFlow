// Real local Vite, FastAPI and browser downloads; isolated synthetic data only.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
const hash = data => crypto.createHash('sha256').update(data).digest('hex')
async function port() {
  const server = net.createServer()
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const value = server.address().port
  await new Promise(resolve => server.close(resolve))
  return value
}
async function until(fn, label) {
  const end = Date.now() + 50000
  while (!(await fn())) {
    assert(Date.now() < end, label)
    await pause(80)
  }
}
async function sources() {
  const result = {}
  async function walk(folder, extensions) {
    for (const entry of await fs.readdir(folder, {withFileTypes: true})) {
      const file = path.join(folder, entry.name)
      if (entry.isDirectory()) await walk(file, extensions)
      else if (extensions.some(ext => file.endsWith(ext))) result[path.relative(root, file).replaceAll('\\', '/')] = hash(await fs.readFile(file))
    }
  }
  await walk(path.join(root, 'backend/app'), ['.py'])
  await walk(path.join(root, 'frontend/src'), ['.vue', '.ts', '.css'])
  for (const name of ['backend/tests/artifact_export_ui_server.py', 'frontend/tests/artifact-export-current.cjs', 'frontend/vite.config.ts'])
    result[name] = hash(await fs.readFile(path.join(root, name)))
  return result
}
async function main() {
  const parent = path.join(root, 'verification/artifact-export-ui-current')
  await fs.mkdir(parent, {recursive: true})
  const out = await fs.mkdtemp(path.join(parent, 'run-'))
  const scratchParent = path.join(root, 'verification/test-temp-current/artifact-export-ui')
  await fs.mkdir(scratchParent, {recursive: true})
  const isolated = await fs.mkdtemp(path.join(scratchParent, 'run-'))
  const backendPort = await port(), base = `http://127.0.0.1:${backendPort}`
  const checks = [], downloads = [], errors = [], external = [], posts = [], routeErrors = []
  let backend, vite, browser, page, log = ''
  const report = {status: 'failed', started_at: new Date().toISOString(), out, isolated, checks, downloads,
    provider_requests: 0, browser_errors: errors, external_requests: external, route_errors: routeErrors, before: await sources(),
    limits: ['Synthetic local browser/API checks only; no provider or user library',
      'DOCX/XLSX parsers and browser downloads do not establish native Word/Excel rendering or semantic quality']}
  const json = async (url, body, method) => {
    const response = await fetch(base + url, {method: method || (body ? 'POST' : 'GET'),
      headers: body ? {'Content-Type': 'application/json'} : undefined, body: body ? JSON.stringify(body) : undefined})
    assert.equal(response.status, 200, await response.clone().text())
    return response.json()
  }
  await fs.writeFile(path.join(out, 'started.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({started: out}))
  try {
    const env = {...process.env, DATA_DIR: path.join(isolated, 'data'), APPDATA: path.join(isolated, 'appdata'),
      LOCALAPPDATA: path.join(isolated, 'localappdata'), TMP: isolated, TEMP: isolated, LLM_API_KEY: '',
      EMBEDDING_API_KEY: '', LLM_DEMO_MODE: '1', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false', PYTHONUTF8: '1'}
    backend = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'), ['-m', 'tests.artifact_export_ui_server', String(backendPort)],
      {cwd: path.join(root, 'backend'), env, windowsHide: true})
    backend.stdout.on('data', data => {log += data})
    backend.stderr.on('data', data => {log += data})
    await until(async () => {
      if (backend.exitCode !== null) throw Error(log)
      try { return (await fetch(base + '/api/health')).ok } catch { return false }
    }, 'isolated API startup')
    const seeded = await json('/__test__/artifact-export-seed', {})
    const {createServer} = await import('vite')
    vite = await createServer({root: path.join(root, 'frontend'), configFile: path.join(root, 'frontend/vite.config.ts'), logLevel: 'error',
      server: {host: '127.0.0.1', port: await port(), strictPort: true, hmr: false,
        proxy: {'/api': {target: base, ws: true, changeOrigin: false}}}})
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({headless: true})
    const context = await browser.newContext({viewport: {width: 1440, height: 1050}, acceptDownloads: true})
    await context.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.origin === ui || ['blob:', 'data:'].includes(url.protocol)) return route.continue()
      external.push(url.origin + url.pathname)
      return route.abort()
    })
    context.on('request', request => {
      if (request.method() === 'POST') posts.push({url: new URL(request.url()).pathname, data: request.postData()})
    })
    page = await context.newPage()
    page.setDefaultTimeout(25000)
    page.on('pageerror', error => errors.push(error.message))
    const artifactUrl = id => `${ui}/artifacts/${id}?conv=${seeded.conversation_id}`
    const observe = () => json('/__test__/artifact-export-observe')
    const exportsFor = (snapshot, id, revision) => snapshot.exports.filter(item => item.artifact_id === id && (!revision || item.revision_id === revision))
    const exportPostCount = () => posts.filter(item => /\/exports$/.test(item.url)).length
    const fixed = () => page.locator('section[aria-label="固定导出文件"]')
    const loaded = async title => {
      await page.getByRole('heading', {name: title, exact: true}).waitFor()
      await fixed().getByRole('heading', {name: '此版本的固定导出文件', exact: true}).waitFor()
    }
    const download = async (label, item) => {
      const link = fixed().getByRole('link', {name: '下载 ' + label, exact: true})
      await link.waitFor()
      const href = await link.getAttribute('href')
      assert(href.includes(`/exports/${item.id}/download`))
      const waiting = page.waitForEvent('download')
      await link.click()
      const transfer = await waiting
      const destination = path.join(out, `${item.id}-${item.format}.${item.format === 'bundle' ? 'zip' : item.format}`)
      await transfer.saveAs(destination)
      const bytes = await fs.readFile(destination)
      const response = await fetch(base + href)
      assert.equal(response.status, 200)
      assert.deepEqual(Buffer.from(await response.arrayBuffer()), bytes)
      assert.equal(response.headers.get('X-Export-SHA256'), hash(bytes))
      assert.equal(response.headers.get('Content-Length'), String(bytes.length))
      assert.equal(hash(bytes), item.sha256)
      const parsed = await json(`/__test__/artifact-export-inspect/${item.id}`)
      assert.equal(parsed.sha256, hash(bytes))
      downloads.push({export_id: item.id, revision_id: item.revision_id, format: item.format, sha256: hash(bytes), bytes: bytes.length, file: destination})
      return parsed
    }

    await page.goto(artifactUrl(seeded.document.id))
    await loaded('浏览器固定文档')
    const initial = await observe(), requestIds = [], committed = []
    let lose = true
    const docPost = `**/api/artifacts/${seeded.document.id}/exports`
    await page.route(docPost, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      requestIds.push(route.request().postDataJSON().request_id)
      const response = await route.fetch()
      assert.equal(response.status(), 200)
      committed.push(await response.json())
      if (lose) {lose = false; return route.abort('failed')}
      return route.fulfill({response})
    })
    await page.getByRole('button', {name: '生成 Word 文件', exact: true}).click()
    await page.getByRole('alert').waitFor()
    await page.getByRole('button', {name: '生成 Word 文件', exact: true}).click()
    await fixed().getByRole('link', {name: '下载 Word', exact: true}).waitFor()
    assert.equal(requestIds.length, 2)
    assert.equal(requestIds[0], requestIds[1])
    assert.equal(committed[0].id, committed[1].id)
    let snapshot = await observe()
    const documentExport = exportsFor(snapshot, seeded.document.id, seeded.document.v2)
    assert.equal(documentExport.length, 1)
    const doc = await download('Word', documentExport[0])
    assert(doc.paragraphs.some(value => value.includes('固定内容乙')))
    assert.deepEqual(doc.tables, [[['项目', '数值'], ['B', '7']]])
    assert.deepEqual(snapshot.artifacts, initial.artifacts)
    assert.deepEqual(snapshot.revisions, initial.revisions)
    assert.deepEqual(snapshot.computations, [])
    checks.push('real DOCX generation and browser download preserve selected v2 content; lost committed response retries the same request/export/bytes exactly once')
    await page.unroute(docPost)
    const readOnlyBefore = await observe(), postsBefore = exportPostCount()
    await page.reload()
    await loaded('浏览器固定文档')
    await fixed().getByRole('link', {name: '下载 Word', exact: true}).waitFor()
    await page.getByLabel('历史版本', {exact: true}).selectOption(seeded.document.v1)
    await until(async () => (await page.getByLabel('成果正文', {exact: true}).inputValue()).includes('固定内容甲'), 'saved historical v1 loaded')
    assert(await page.getByLabel('成果正文', {exact: true}).getAttribute('readonly') !== null)
    assert.deepEqual(await observe(), readOnlyBefore)
    assert.equal(exportPostCount(), postsBefore)
    checks.push('reload and historical-version browsing perform no export POST and leave saved revisions, pending changes, computations and export bytes unchanged')

    let held = false, release, handled
    const hold = new Promise(resolve => {release = resolve})
    const complete = new Promise(resolve => {handled = resolve})
    await page.route(docPost, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      try {
        const response = await route.fetch()
        assert.equal(response.status(), 200)
        held = true
        await hold
        await route.fulfill({response})
      } catch (error) {
        routeErrors.push(String(error.stack || error))
      } finally {handled()}
    })
    await page.getByRole('button', {name: '生成 Word 文件', exact: true}).click()
    await until(() => held, 'old-version export committed while response held')
    await page.evaluate(async ({id, cid}) => {
      await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push(`/artifacts/${id}?conv=${cid}`)
    }, {id: seeded.other.id, cid: seeded.conversation_id})
    await loaded('切换目标文档')
    release()
    await complete
    await page.unroute(docPost)
    assert.deepEqual(routeErrors, [])
    await pause(200)
    assert.equal(await fixed().getByRole('link', {name: '下载 Word', exact: true}).count(), 0)
    assert(!(await page.locator('.artifact-page').innerText()).includes('已保存 v1 的Word文件'))
    snapshot = await observe()
    assert.equal(exportsFor(snapshot, seeded.document.id, seeded.document.v1).length, 1)
    assert.equal(exportsFor(snapshot, seeded.other.id).length, 0)
    checks.push('a committed late v1 export response cannot populate a different artifact view or its saved download links')

    await page.goto(artifactUrl(seeded.analysis.id))
    await loaded('缺值与文字安全分析')
    const resultByFormat = {}
    for (const [format, button, label] of [['xlsx', '生成 XLSX 文件', 'XLSX'], ['svg', '生成 SVG 文件', 'SVG'], ['bundle', '生成复算包', '复算包']]) {
      await page.getByRole('button', {name: button, exact: true}).click()
      await fixed().getByRole('link', {name: '下载 ' + label, exact: true}).waitFor()
      const current = exportsFor(await observe(), seeded.analysis.id, seeded.analysis.revision).find(value => value.format === format)
      assert(current)
      resultByFormat[format] = await download(label, current)
      const meta = await json(`/api/artifacts/${seeded.analysis.id}/exports/${current.id}?conversation_id=${seeded.conversation_id}`)
      assert.equal(meta.validation.native_open, 'not_assessed')
      assert.equal(meta.validation.semantic, 'not_assessed')
    }
    const workbook = resultByFormat.xlsx
    assert.deepEqual(workbook.sheets, ['RawData', 'NumericData', 'Statistics', 'Provenance'])
    assert.deepEqual(workbook.raw.slice(1).map(row => row[0]), [{value: '=1+1', type: 's'}, {value: '+literal', type: 's'}, {value: '@literal', type: 's'}])
    assert.equal(workbook.numeric[2][1], null)
    assert.equal(workbook.numeric[3][2], null)
    assert.equal(workbook.formula_count, 0)
    assert.equal(workbook.native_chart_count, 0)
    assert.equal(workbook.statistics.find(row => row[0] === 'A')[2], 1)
    assert.equal(resultByFormat.svg.root, '{http://www.w3.org/2000/svg}svg')
    assert.deepEqual(resultByFormat.svg.forbidden_elements, [])
    const bundle = resultByFormat.bundle
    assert.equal(bundle.crc_error, null)
    assert.equal(bundle.manifest.recorded_execution, false)
    assert.deepEqual(bundle.file_hashes, bundle.manifest.files)
    assert(bundle.data.includes('+literal,,4,6'))
    assert.equal(bundle.calculation.stats.A.missing, 1)
    assert.equal(bundle.calculation.points[1].values.A, null)
    assert.deepEqual((await observe()).computations, [])
    checks.push('actual XLSX/SVG/ZIP downloads preserve literal formula-like text, missing-value gaps and all fixed file hashes without inventing recomputation or native-chart acceptance')
    await page.screenshot({path: path.join(out, 'analysis-fixed-files.png'), fullPage: true})

    const openRequirements = async target => {
      const detail = target.locator('.task-requirements')
      await detail.locator('> summary').click()
      await until(async () => !(await detail.getByRole('button', {name: '添加要求或限制', exact: true}).isDisabled()), 'requirements loaded')
      return detail
    }
    const goalPath = `/api/goal/${seeded.goal_id}/requirements`
    const goalSnapshot = () => json(`${goalPath}?conversation_id=${seeded.conversation_id}`)
    await page.goto(`${ui}/chat?conv=${seeded.conversation_id}`)
    let detail = await openRequirements(page)
    await detail.getByRole('button', {name: '添加要求或限制', exact: true}).click()
    await detail.getByLabel('要求描述', {exact: true}).fill('核对固定分析工作簿')
    await detail.getByLabel('验收方式', {exact: true}).selectOption('artifact_export_result')
    await detail.getByLabel('固定导出格式', {exact: true}).selectOption('xlsx')
    await detail.getByLabel('指定成果编号（可选）', {exact: true}).fill(seeded.analysis.id)
    await detail.getByLabel('指定版本编号（可选，须同时指定成果）', {exact: true}).fill(seeded.analysis.revision)
    await detail.getByLabel('要求已有真实成功复算记录', {exact: true}).check()
    await detail.getByRole('button', {name: '纳入任务要求', exact: true}).click()
    let requirement = detail.locator('[data-requirement-id]').filter({hasText: '核对固定分析工作簿'})
    await requirement.waitFor()
    await requirement.getByRole('button', {name: '修改此要求', exact: true}).click()
    assert.equal(await detail.getByLabel('固定导出格式', {exact: true}).inputValue(), 'xlsx')
    assert.equal(await detail.getByLabel('指定成果编号（可选）', {exact: true}).inputValue(), seeded.analysis.id)
    assert.equal(await detail.getByLabel('指定版本编号（可选，须同时指定成果）', {exact: true}).inputValue(), seeded.analysis.revision)
    assert(await detail.getByLabel('要求已有真实成功复算记录', {exact: true}).isChecked())
    await detail.getByLabel('要求描述', {exact: true}).fill('保留目标与复算条件的新要求')
    await detail.getByRole('button', {name: '保存此要求的新版本', exact: true}).click()
    requirement = detail.locator('[data-requirement-id]').filter({hasText: '保留目标与复算条件的新要求'})
    await requirement.waitFor()
    const expectedContract = (await goalSnapshot()).requirements.find(row => row.title === '保留目标与复算条件的新要求').tool_arguments.result_contract
    assert.equal(expectedContract.format, 'xlsx')
    assert.equal(expectedContract.requires_recorded_execution, true)
    assert.equal(expectedContract.artifact_id, seeded.analysis.id)
    assert.equal(expectedContract.revision_id, seeded.analysis.revision)
    const secondPage = await context.newPage()
    secondPage.setDefaultTimeout(25000)
    secondPage.on('pageerror', error => errors.push(error.message))
    await secondPage.goto(`${ui}/chat?conv=${seeded.conversation_id}`)
    const stale = await openRequirements(secondPage)
    await stale.locator('[data-requirement-id]').filter({hasText: '保留目标与复算条件的新要求'}).getByRole('button', {name: '修改此要求', exact: true}).click()
    await stale.getByLabel('要求描述', {exact: true}).fill('CAS冲突后保留的固定导出要求')
    await detail.getByRole('button', {name: '添加要求或限制', exact: true}).click()
    await detail.getByLabel('要求描述', {exact: true}).fill('另一窗口补充核对')
    await detail.getByRole('button', {name: '纳入任务要求', exact: true}).click()
    await detail.locator('[data-requirement-id]').filter({hasText: '另一窗口补充核对'}).waitFor()
    await stale.getByRole('button', {name: '保存此要求的新版本', exact: true}).click()
    await stale.getByRole('alert').filter({hasText: '其他窗口更新'}).waitFor()
    assert.equal(await stale.getByLabel('要求描述', {exact: true}).inputValue(), 'CAS冲突后保留的固定导出要求')
    assert.equal(await stale.getByLabel('固定导出格式', {exact: true}).inputValue(), 'xlsx')
    assert.equal(await stale.getByLabel('指定版本编号（可选，须同时指定成果）', {exact: true}).inputValue(), seeded.analysis.revision)
    assert(await stale.getByLabel('要求已有真实成功复算记录', {exact: true}).isChecked())
    await stale.getByRole('button', {name: '刷新要求', exact: true}).click()
    await until(async () => !(await stale.getByRole('button', {name: '保存此要求的新版本', exact: true}).isDisabled()), 'CAS explicit refresh')
    await stale.getByRole('button', {name: '保存此要求的新版本', exact: true}).click()
    await stale.locator('[data-requirement-id]').filter({hasText: 'CAS冲突后保留的固定导出要求'}).waitFor()
    const afterConflict = (await goalSnapshot()).requirements.find(row => row.title === 'CAS冲突后保留的固定导出要求')
    assert.deepEqual(afterConflict.tool_arguments.result_contract, expectedContract)
    assert.equal(afterConflict.status, 'open')
    await secondPage.screenshot({path: path.join(out, 'fixed-export-goal.png'), fullPage: true})
    await secondPage.close()
    checks.push('the real Goal form persists format, optional fixed artifact/revision and recomputation condition through replacement and two-window CAS conflict without marking a file semantically complete')

    await json(`/api/conversations/${seeded.conversation_id}/archive`, {archived: true, expected_revision: 0}, 'PUT')
    const archivedBefore = await observe(), archivedPosts = exportPostCount()
    await page.goto(artifactUrl(seeded.document.id))
    await loaded('浏览器固定文档')
    await page.getByText('此会话已归档，可下载已有固定文件；恢复会话后才能生成新文件。', {exact: true}).waitFor()
    assert(await page.getByRole('button', {name: '生成 Word 文件', exact: true}).isDisabled())
    await download('Word', documentExport[0])
    await page.reload()
    await loaded('浏览器固定文档')
    assert.equal(exportPostCount(), archivedPosts)
    assert.deepEqual(await observe(), archivedBefore)
    await page.screenshot({path: path.join(out, 'archived-fixed-docx.png'), fullPage: true})
    checks.push('archived browsing and reloading retain working same-byte downloads while new file generation stays disabled and no export POST is dispatched')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    assert.deepEqual(routeErrors, [])
    report.status = 'passed'
  } catch (error) {
    report.error = String(error.stack || error)
    if (page) await page.screenshot({path: path.join(out, 'failure.png'), fullPage: true}).catch(() => {})
    throw error
  } finally {
    report.finished_at = new Date().toISOString()
    report.after = await sources()
    report.changed_during_run = [...new Set([...Object.keys(report.before), ...Object.keys(report.after)])].filter(name => report.before[name] !== report.after[name])
    report.post_requests = posts
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2))
    await fs.writeFile(path.join(out, 'server.log'), log)
    if (browser) await browser.close()
    if (vite) await vite.close()
    if (backend && backend.exitCode === null) await new Promise(resolve => {backend.once('exit', resolve); backend.kill()})
    console.log(JSON.stringify({status: report.status, checks: checks.length, downloads: downloads.length, changed_during_run: report.changed_during_run, report: path.join(out, 'report.json')}))
  }
}
main().catch(error => {console.error(error); process.exitCode = 1})
