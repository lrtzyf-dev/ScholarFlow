// Actual Vue + isolated API. No private documents, model keys or paid calls.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function main() {
  const output = path.join(root, 'verification/artifact-delivery')
  await fs.mkdir(output, { recursive: true })
  const data = await fs.mkdtemp(path.join(require('node:os').tmpdir(), 'sf-artifacts-'))
  const reserve = net.createServer()
  await new Promise(resolve => reserve.listen(0, '127.0.0.1', resolve))
  const port = reserve.address().port
  await new Promise(resolve => reserve.close(resolve))
  const base = `http://127.0.0.1:${port}`
  const server = spawn(path.join(root, 'backend/.venv/Scripts/python.exe'),
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: path.join(root, 'backend'), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, DATA_DIR: data, LLM_API_KEY: '', LLM_DEMO_MODE: '1', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false' },
  })
  let log = ''
  server.stdout.on('data', part => { log += part })
  server.stderr.on('data', part => { log += part })
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1360, height: 1000 } })
  const page = await context.newPage()
  const errors = [], external = [], checks = []
  context.on('page', p => p.on('pageerror', err => errors.push(err.message)))
  page.on('pageerror', err => errors.push(err.message))
  await context.route('**/*', route => {
    const url = route.request().url()
    if (url.startsWith('http') && !url.startsWith(base)) { external.push(url); return route.abort() }
    return route.continue()
  })
  async function api(route, body, method = 'POST') {
    const response = await fetch(base + '/api' + route, body === undefined ? undefined : {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    assert.equal(response.status, 200, await response.clone().text())
    return response.json()
  }
  const waitReady = async () => {
    const deadline = Date.now() + 30000
    while (Date.now() < deadline) {
      if (server.exitCode !== null) throw Error(log)
      try { if ((await fetch(base + '/api/health')).ok) return } catch {}
      await pause(100)
    }
    throw Error('API did not start: ' + log)
  }
  let result = { status: 'failed', checks, provider_requests: 0 }
  try {
    await waitReady()
    await page.goto(base + '/artifacts')
    await page.getByLabel('文档标题', { exact: false }).fill('可回退科研文档')
    await page.getByRole('button', { name: '新建文档', exact: true }).click()
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    const id = page.url().split('/artifacts/')[1].split('?')[0]
    const original = '# 前言🚀\n人工内容：保留 αβ 和原始标点。\n\n# 方法\n旧方法\n\n# 结果\n原始结论\n'
    await page.getByLabel('成果正文', { exact: true }).fill(original)
    await page.getByRole('button', { name: '保存修改草稿并查看差异' }).click()
    await page.getByRole('region', { name: '修改差异' }).waitFor()
    assert.equal((await api(`/artifacts/${id}`, undefined)).revision.content, '')
    await page.reload()
    await page.getByRole('button', { name: /[a-f0-9]{8}/ }).first().click()
    await page.getByRole('button', { name: '接受修改', exact: true }).click()
    await page.getByText('已保存为新版本，旧版本仍可查看。', { exact: true }).waitFor()
    assert.equal((await api(`/artifacts/${id}`, undefined)).revision.content, original)
    checks.push('create, durable pending draft, reload, explicit acceptance')
    await page.getByLabel('修改范围', { exact: true }).selectOption({ label: '方法' })
    assert.equal(await page.getByLabel('成果正文', { exact: true }).inputValue(), '# 方法\n旧方法\n\n')
    await page.getByLabel('成果正文', { exact: true }).fill('# 方法\n新方法\n\n')
    await page.getByRole('button', { name: '保存修改草稿并查看差异' }).click()
    await page.getByRole('button', { name: '接受修改', exact: true }).click()
    await page.getByText('已保存为新版本，旧版本仍可查看。', { exact: true }).waitFor()
    const updated = await api(`/artifacts/${id}`, undefined)
    assert.equal(updated.revision.content, original.replace('旧方法', '新方法'))
    checks.push('Unicode code-point section range preserves other chapters')
    const other = await context.newPage()
    await other.goto(base + `/artifacts/${id}`)
    await other.getByLabel('成果正文', { exact: true }).fill(updated.revision.content + '\n另一窗口人工内容\n')
    await other.getByRole('button', { name: '保存修改草稿并查看差异' }).click()
    await other.getByRole('button', { name: '接受修改', exact: true }).waitFor()
    await page.getByLabel('成果正文', { exact: true }).fill(updated.revision.content + '\n第一窗口内容\n')
    await page.getByRole('button', { name: '保存修改草稿并查看差异' }).click()
    await page.getByRole('button', { name: '接受修改', exact: true }).click()
    await page.getByText('已保存为新版本，旧版本仍可查看。', { exact: true }).waitFor()
    await other.getByRole('button', { name: '接受修改', exact: true }).click()
    await other.getByRole('alert').filter({ hasText: '双方版本和草稿均已保留' }).waitFor()
    const pending = await api(`/artifacts/${id}/changes`, undefined)
    assert.ok(pending.items.some(change => change.conflict && change.content.includes('另一窗口人工内容')))
    checks.push('concurrent edit conflict retains both saved version and draft')
    await other.reload()
    await other.getByRole('button', { name: /版本冲突/ }).click()
    assert.ok(await other.getByRole('button', { name: '接受修改', exact: true }).isDisabled())
    await other.getByRole('button', { name: '拒绝修改', exact: true }).click()
    await other.getByText('已拒绝修改，正文保持原版本。', { exact: true }).waitFor()
    checks.push('conflict shown after reload; rejection keeps saved content')
    await page.getByLabel('历史版本', { exact: true }).selectOption({ label: `v2 · ${updated.revision.parent_revision_id.slice(0,8)}` })
    await page.getByRole('button', { name: '恢复为新版本' }).click()
    await page.getByRole('button', { name: '接受修改', exact: true }).click()
    await page.getByText('已保存为新版本，旧版本仍可查看。', { exact: true }).waitFor()
    const restored = await api(`/artifacts/${id}`, undefined)
    assert.equal(restored.revision.content, original)
    assert.equal(restored.version, 5)
    checks.push('restore appends version and retains complete original content')
    await page.screenshot({ path: path.join(output, 'document-editor.png'), fullPage: true })
    await page.goto(base + '/data')
    await page.getByLabel('CSV 数据').fill('method,score\nA,81\nB,87\nC,\n')
    await page.getByRole('button', { name: '解析并绘图' }).click()
    await page.getByLabel('score 的单位').fill('%')
    await page.getByRole('button', { name: '保存数据和复算配置' }).click()
    await page.getByRole('link', { name: '编辑已保存成果', exact: true }).click()
    await page.getByRole('button', { name: '执行复算' }).click()
    await page.getByText('已执行保存的复算脚本，运行记录已保存。', { exact: true }).waitFor()
    const analysisId = page.url().split('/artifacts/')[1].split('?')[0]
    const computations = await api(`/artifacts/${analysisId}/computations`, undefined)
    assert.equal(computations.items[0].result.stats.score.mean, 84)
    assert.equal(computations.items[0].result.stats.score.missing, 1)
    const download = await Promise.all([page.waitForEvent('download'), page.getByRole('link', { name: '下载复算包' }).click()])
    await download[0].saveAs(path.join(output, 'analysis-reproduction.zip'))
    checks.push('data page saves exact CSV/units/config, executes script, downloads package')
    await page.screenshot({ path: path.join(output, 'analysis-reproduction.png'), fullPage: true })
    await page.setViewportSize({ width: 760, height: 900 })
    await page.goto(base + `/artifacts/${id}`)
    await page.getByLabel('成果正文', { exact: true }).waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2))
    await page.screenshot({ path: path.join(output, 'narrow-editor.png'), fullPage: true })
    checks.push('760px editor has no horizontal document overflow')
    assert.deepEqual(errors, [])
    assert.deepEqual(external, [])
    result = { status: 'passed', checks, provider_requests: 0, browser_errors: errors, external_requests: external }
  } catch (error) {
    result.error = String(error.stack || error)
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    throw error
  } finally {
    await fs.writeFile(path.join(output, 'browser-report.json'), JSON.stringify(result, null, 2))
    await fs.writeFile(path.join(output, 'server.log'), log)
    await browser.close()
    server.kill()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
