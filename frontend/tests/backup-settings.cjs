// Real Vue, HTTP backend, SQLite archives and process restarts. Synthetic data
// and a deliberately invalid service credential only; zero provider calls.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const crypto = require('node:crypto')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(check, label, timeout = 30000) {
  const end = Date.now() + timeout
  while (!(await check())) {
    assert.ok(Date.now() < end, `Timed out: ${label}`)
    await pause(150)
  }
}
async function freePort() {
  const server = net.createServer()
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise((resolve) => server.close(resolve))
  return port
}
async function main() {
  const output = path.join(root, 'verification/backup-settings')
  await fs.mkdir(output, { recursive: true })
  const tracked = [
    'frontend/tests/backup-settings.cjs',
    'frontend/src/components/BackupSettingsCard.vue',
    'frontend/src/backupApi.ts',
    'backend/app/data_backup.py',
    'backend/app/backup_service.py',
    'backend/app/api/backups.py',
  ]
  const hashes = async () =>
    Object.fromEntries(
      await Promise.all(
        tracked.map(async (file) => [
          file,
          crypto
            .createHash('sha256')
            .update(await fs.readFile(path.join(root, file)))
            .digest('hex'),
        ])
      )
    )
  const before = await hashes()
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const env = {
    ...process.env,
    DATA_DIR: path.join(isolated, 'data'),
    APPDATA: path.join(isolated, 'appdata'),
    LOCALAPPDATA: path.join(isolated, 'localappdata'),
    LLM_API_KEY: '',
    EMBEDDING_API_KEY: '',
    REDIS_URL: '',
    DESKTOP_NOTIFICATIONS: 'false',
    PYTHONUTF8: '1',
  }
  const port = await freePort(),
    base = `http://127.0.0.1:${port}`
  let backend,
    vite,
    browser,
    log = ''
  const checks = [],
    errors = []
  async function startBackend() {
    backend = spawn(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)],
      { cwd: path.join(root, 'backend'), env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }
    )
    backend.stdout.on('data', (b) => {
      log += b
    })
    backend.stderr.on('data', (b) => {
      log += b
    })
    await until(
      async () => {
        if (backend.exitCode !== null) throw Error(log.slice(-6000))
        try {
          return (await fetch(base + '/api/health')).ok
        } catch {
          return false
        }
      },
      'backend ready',
      40000
    )
  }
  async function stopBackend() {
    if (backend && backend.exitCode === null && backend.signalCode === null) {
      backend.kill()
      await until(
        () => backend.exitCode !== null || backend.signalCode !== null,
        'owned backend stopped'
      )
    }
  }
  async function api(url, body, method = body === undefined ? 'GET' : 'POST') {
    const response = await fetch(base + '/api' + url, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    assert.ok(response.ok, `${url}: ${response.status} ${await response.clone().text()}`)
    return response.json()
  }
  async function job(kind) {
    let selected
    await until(
      async () => {
        selected = (await api('/backups')).jobs.find((j) => j.kind === kind)
        if (selected?.status === 'failed') throw Error(selected.error)
        return selected?.status === 'succeeded'
      },
      `completed ${kind}`,
      40000
    )
    return selected
  }
  try {
    await startBackend()
    const profile = await api('/model-profiles', {
      name: 'Synthetic credential exclusion fixture',
      base_url: 'http://127.0.0.1:1/v1',
      model_id: 'Synthetic-Backup',
      api_key: 'generated-local-backup-profile-credential',
      reasoning_effort: '',
    })
    await api(
      '/settings',
      {
        llm_base_url: 'http://127.0.0.1:1/v1',
        llm_model: 'Synthetic-Backup-Legacy',
        llm_api_key: 'generated-local-backup-legacy-credential',
        embedding_base_url: 'http://127.0.0.1:1/v1',
        embedding_model: 'Synthetic-Backup-Embedding',
        embedding_api_key: 'generated-local-backup-embedding-credential',
        work_rules: 'SYNTHETIC_NON_CREDENTIAL_SETTING_MUST_SURVIVE',
      },
      'PUT'
    )
    const initialTools = await api('/service-tools/deepseek')
    await api(
      '/service-tools/deepseek',
      {
        expected_version: initialTools.version,
        enabled_services: ['search'],
        api_key: 'generated-local-backup-tool-credential',
      },
      'PUT'
    )
    const original = await api('/conversations', { title: '应从完整备份保留的会话' })
    const upload = new FormData()
    upload.append('conversation_id', original.id)
    upload.append('request_id', 'backup-ui-synthetic-source')
    upload.append(
      'file',
      new Blob(['Fixed source line 1\nFixed source line 2']),
      'fixed-source.txt'
    )
    const sourceResponse = await fetch(base + '/api/sources/upload', {
      method: 'POST',
      body: upload,
    })
    assert.ok(sourceResponse.ok)
    const source = await sourceResponse.json()
    const locator = await api(`/sources/revisions/${source.revision_id}/locators`, {
      conversation_id: original.id,
      kind: 'text_lines',
      line_start: 2,
      line_end: 2,
    })
    const { createServer } = await import('vite')
    vite = await createServer({
      root: path.join(root, 'frontend'),
      configFile: path.join(root, 'frontend/vite.config.ts'),
      logLevel: 'error',
      server: {
        host: '127.0.0.1',
        port: await freePort(),
        strictPort: true,
        hmr: false,
        proxy: { '/api': { target: base, changeOrigin: false, ws: true } },
      },
    })
    await vite.listen()
    const ui = `http://127.0.0.1:${vite.httpServer.address().port}`
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
      acceptDownloads: true,
    })
    page.setDefaultTimeout(12000)
    page.on('pageerror', (e) => errors.push(e.message))
    async function dataSettings() {
      await page.goto(ui + '/settings')
      await page.getByRole('button', { name: '数据与存储', exact: true }).click()
      await page.getByRole('heading', { name: '备份与恢复', exact: true }).waitFor()
    }
    await dataSettings()
    assert.equal((await api('/backups')).credential_portability, 'credentials_excluded_reconfigure')
    await page
      .getByTestId('backup-current-credential-policy')
      .filter({ hasText: '排除应用凭证存储' })
      .waitFor()
    await page.getByRole('button', { name: '创建完整备份', exact: true }).click()
    const backup = await job('create')
    assert.equal(backup.result.credential_portability, 'credentials_excluded_reconfigure')
    await page.getByRole('link', { name: /下载已校验备份/ }).waitFor()
    const downloadPromise = page.waitForEvent('download')
    await page
      .getByRole('link', { name: /下载已校验备份/ })
      .first()
      .click()
    const downloaded = await downloadPromise
    const backupPath = path.join(output, 'synthetic-verified.sfbak')
    await downloaded.saveAs(backupPath)
    assert.equal((await fs.stat(backupPath)).size, backup.result.size)
    const manifestPolicy = execFileSync(
      path.join(root, 'backend/.venv/Scripts/python.exe'),
      [
        '-c',
        "import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); print(json.loads(z.read('manifest.json')).get('credential_portability', ''))",
        backupPath,
      ],
      { encoding: 'utf8', windowsHide: true }
    ).trim()
    assert.equal(manifestPolicy, 'credentials_excluded_reconfigure')
    checks.push('UI created, validated and downloaded a real complete application backup')
    const extra = await api('/conversations', { title: '备份之后新增的会话' })
    await page.locator('input[type=file][accept=".sfbak"]').setInputFiles(backupPath)
    await page.getByRole('heading', { name: '恢复预览', exact: true }).waitFor()
    assert.equal(
      (await job('prepare')).result.credential_portability,
      'credentials_excluded_reconfigure'
    )
    await page
      .getByTestId('restore-credential-policy')
      .filter({ hasText: '恢复后需要重新配置' })
      .waitFor()
    await page.getByText(/权限切换为只读模式/).waitFor()
    await page.screenshot({ path: path.join(output, 'restore-preview.png'), fullPage: true })
    await page.getByRole('checkbox', { name: /我已核对/ }).check()
    await page.getByRole('button', { name: '确认恢复并等待重启', exact: true }).click()
    await until(async () => !!(await api('/backups')).pending_restore, 'restore confirmed')
    assert.equal((await fetch(base + '/api/settings')).status, 423)
    await stopBackend()
    await startBackend()
    const after = await api('/conversations')
    assert.ok(after.conversations.some((c) => c.id === original.id))
    assert.ok(!after.conversations.some((c) => c.id === extra.id))
    const reference = await api(
      `/sources/revisions/${source.revision_id}/locators/${locator.locator_id}?conversation_id=${original.id}`
    )
    assert.equal(reference.quote.trim(), 'Fixed source line 2')
    checks.push(
      'Confirmed restore waited for restart, restored conversations and fixed source locator'
    )
    const restoredSettings = await api('/settings')
    assert.equal(restoredSettings.llm_api_key_set, false)
    assert.equal(restoredSettings.embedding_api_key_set, false)
    assert.equal(restoredSettings.work_rules, 'SYNTHETIC_NON_CREDENTIAL_SETTING_MUST_SURVIVE')
    assert.equal(restoredSettings.approval_mode, 'read_only')
    assert.equal(
      (await api('/model-profiles')).profiles.find((p) => p.id === profile.id).api_key_set,
      false
    )
    assert.equal((await api('/service-tools/deepseek')).api_key_set, false)
    checks.push(
      'Ordinary export manifest/status/preview exclude application-stored authentication; restore retains non-secret settings and originals, clears synthetic model/embedding/tool keys, and uses read-only mode'
    )
    await dataSettings()
    await page.getByRole('button', { name: '准备回退到恢复前', exact: true }).click()
    await page.getByRole('heading', { name: '恢复前数据回退预览', exact: true }).waitFor()
    assert.equal(
      (await job('rollback')).result.credential_portability,
      'protected_credentials_require_original_os_user'
    )
    await page
      .getByTestId('restore-credential-policy')
      .filter({ hasText: '此本机回退副本保留恢复前' })
      .waitFor()
    await page.screenshot({ path: path.join(output, 'rollback-preview.png'), fullPage: true })
    await page.getByRole('checkbox', { name: /我已核对/ }).check()
    await page.getByRole('button', { name: '确认恢复并等待重启', exact: true }).click()
    await until(async () => !!(await api('/backups')).pending_restore, 'rollback confirmed')
    await stopBackend()
    await startBackend()
    assert.ok((await api('/conversations')).conversations.some((c) => c.id === extra.id))
    assert.equal((await api('/settings')).llm_api_key_set, true)
    assert.equal((await api('/settings')).embedding_api_key_set, true)
    assert.equal(
      (await api('/model-profiles')).profiles.find((p) => p.id === profile.id).api_key_set,
      true
    )
    assert.equal((await api('/service-tools/deepseek')).api_key_set, true)
    checks.push('Actual second process restart restored pre-restore data through rollback')
    checks.push(
      'Private local rollback preview accurately preserves protected synthetic credentials for the original Windows user'
    )
    await dataSettings()
    await page.screenshot({ path: path.join(output, 'rollback-complete.png'), fullPage: true })
    await page.getByRole('button', { name: /模型/ }).filter({ hasText: '模型' }).first().click()
    const card = page.locator('.tools-card')
    await card.getByRole('heading', { name: 'DeepSeek 联网与文档工具' }).waitFor()
    await card.locator('input[type=password]').fill('synthetic-no-network-tool-credential')
    await card.getByRole('checkbox', { name: /联网搜索/ }).check()
    await card.getByRole('button', { name: '保存独立工具配置', exact: true }).click()
    await card.getByRole('status').waitFor()
    let config = await api('/service-tools/deepseek')
    assert.deepEqual(config.enabled_services, ['search'])
    assert.equal(config.api_key_set, true)
    assert.ok(!JSON.stringify(config).includes('synthetic-no-network'))
    await page.screenshot({
      path: path.join(output, 'deepseek-tools-settings.png'),
      fullPage: true,
    })
    await card.getByRole('button', { name: '撤销工具密钥', exact: true }).click()
    await card.getByRole('button', { name: '确认撤销', exact: true }).click()
    await until(
      async () => !(await api('/service-tools/deepseek')).api_key_set,
      'tool credential revoked'
    )
    checks.push(
      'Actual service settings save/revoke passed without revealing credential or calling provider'
    )
    assert.deepEqual(errors, [])
    const finalHashes = await hashes()
    assert.deepEqual(
      finalHashes,
      before,
      'backup source changed during E2E; rerun final stable snapshot'
    )
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          passed: true,
          verified_at: new Date().toISOString(),
          checks,
          page_errors: errors,
          real_provider_calls: 0,
          backend_process_starts: 3,
          data_scope: 'isolated_synthetic_only',
          credential_export_policy: manifestPolicy,
          source_sha256: finalHashes,
        },
        null,
        2
      )
    )
    console.log(JSON.stringify({ passed: true, checks, real_provider_calls: 0 }))
  } catch (error) {
    await fs.writeFile(
      path.join(output, 'report.json'),
      JSON.stringify(
        {
          passed: false,
          checks,
          error: error.message,
          page_errors: errors,
          real_provider_calls: 0,
        },
        null,
        2
      )
    )
    await fs.writeFile(path.join(output, 'backend.log'), log)
    throw error
  } finally {
    if (browser) await browser.close()
    if (vite) await vite.close()
    await stopBackend()
  }
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
