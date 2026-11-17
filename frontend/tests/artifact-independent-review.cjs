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
  const output = path.join(root, 'verification/research-persistence/artifact-review')
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
    env: { ...process.env, DATA_DIR: data, APPDATA: path.join(data,'appdata'), LOCALAPPDATA: path.join(data,'localappdata'), LLM_API_KEY: '', LLM_DEMO_MODE: '1', EMBEDDING_API_KEY: '', REDIS_URL: '', DESKTOP_NOTIFICATIONS: 'false' },
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
    if (url.startsWith('http') && !/^http:\/\/127\.0\.0\.1:/.test(url)) { external.push(url); return route.abort() }
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
  let vite
  try {
    await waitReady()
    const {createServer}=await import('vite')
    vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',server:{host:'127.0.0.1',port:0,hmr:false,proxy:{'/api':{target:base,changeOrigin:false}}}})
    await vite.listen()
    const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    const cases=[]
    for(const [index,accept,section] of [[0,true,false],[1,false,false],[2,true,true],[3,false,true]]) {
      const original='# Original\nA\n\n# Untouched\nKeep exact tail.\n'
      const item=await api('/artifacts',{request_id:`independent-draft-${index}-123`,title:'Draft loss probe',content:original})
      await page.goto(`${ui}/artifacts/${item.id}`)
      await page.getByLabel('成果正文',{exact:true}).waitFor()
      if(section) await page.getByLabel('修改范围',{exact:true}).selectOption({label:'Original'})
      await page.getByLabel('成果正文',{exact:true}).fill('# Revision\nB submitted\n\n')
      await page.getByRole('button',{name:'保存修改草稿并查看差异'}).click()
      await page.getByRole('button',{name:'接受修改',exact:true}).waitFor()
      const newer='# Revision\nC newer unsent draft\n\n'
      await page.getByLabel('成果正文',{exact:true}).fill(newer)
      await page.getByRole('button',{name:accept?'接受修改':'拒绝修改',exact:true}).click()
      await page.getByText(accept?'已保存为新版本，旧版本仍可查看。':'已拒绝修改，正文保持原版本。',{exact:false}).waitFor()
      const visible=await page.getByLabel('成果正文',{exact:true}).inputValue()
      const stored=await page.evaluate(id=>localStorage.getItem(`scholarflow:artifact-draft:local:${id}`),item.id)
      const saved=(await api(`/artifacts/${item.id}`,undefined)).revision.content
      cases.push({accept,section,newer_draft_preserved_in_editor:visible===newer,newer_draft_preserved_in_storage:stored?.includes('C newer unsent draft')||false,saved_body:saved,untouched_tail_preserved:!section||saved.endsWith('# Untouched\nKeep exact tail.\n')})
      await page.reload()
      await page.getByLabel('成果正文',{exact:true}).waitFor()
      cases.at(-1).newer_draft_survives_reload=(await page.getByLabel('成果正文',{exact:true}).inputValue())===newer
    }
    const cid=(await api('/conversations',{title:'Independent citations'})).id
    const form=new FormData();form.set('file',new Blob(['First fixed passage.\nSecond fixed passage.\n']),'citations.md');form.set('conversation_id',cid);form.set('request_id','citation-review-upload')
    const uploaded=await fetch(base+'/api/sources/upload',{method:'POST',body:form});assert.equal(uploaded.status,200)
    const source=await uploaded.json(), evidence=[]
    for(const [id,line] of [['e7',1],['e12',2]]) {
      const loc=await api(`/sources/revisions/${source.revision_id}/locators`,{conversation_id:cid,kind:'text_lines',line_start:line,line_end:line})
      evidence.push({id,revision_id:source.revision_id,locator_id:loc.locator_id})
    }
    const item=await api('/artifacts',{request_id:'independent-citation-123',title:'Citation probe',content:'First conclusion[^e7]. Second conclusion[^e12].',conversation_id:cid,evidence})
    const citationChecks=[]
    for(const ev of evidence){
      await page.goto(`${ui}/artifacts/${item.id}?conv=${cid}`)
      await page.getByRole('button',{name:`查看证据 ${ev.id}`,exact:true}).click()
      await page.waitForURL(new RegExp(`/sources/${source.revision_id}`))
      citationChecks.push(new URL(page.url()).searchParams.get('locator')===ev.locator_id)
    }
    const md=await (await fetch(`${base}/api/artifacts/${item.id}/download?format=md&conversation_id=${cid}`)).text()
    result={status:'observed',provider_requests:0,browser_errors:errors,draft_cases:cases,citation_targets_exact: citationChecks,markdown_retains_e7_definition:md.includes('[^e7]:'),markdown_retains_e12_definition:md.includes('[^e12]:')}
    await page.screenshot({path:path.join(output,'draft-after-decision.png'),fullPage:true})
  }finally{
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify(result,null,2))
    await browser.close();await vite?.close();server.kill();await fs.writeFile(path.join(output,'backend.log'),log)
  }
  console.log(JSON.stringify(result))
}
main().catch(e=>{console.error(e);process.exitCode=1})
