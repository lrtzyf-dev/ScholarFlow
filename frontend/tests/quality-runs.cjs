// Real Vue, actual backend processes/restarts; synthetic metered provider only.
const assert = require('node:assert/strict'),
  fs = require('node:fs/promises'),
  path = require('node:path'),
  net = require('node:net')
const { spawn } = require('node:child_process'),
  { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..'),
  pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function until(fn, label) {
  const end = Date.now() + 45000
  while (!(await fn())) {
    assert.ok(Date.now() < end, label)
    await pause(80)
  }
}
async function port() {
  const server = net.createServer()
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const p = server.address().port
  await new Promise((r) => server.close(r))
  return p
}
async function main() {
  const out = path.join(root, 'verification/quality-runs/ui')
  await fs.mkdir(out, { recursive: true })
  const isolated = await fs.mkdtemp(path.join(out, 'isolated-')), control = path.join(isolated, 'control')
  await fs.mkdir(control)
  const env = { ...process.env, DATA_DIR:path.join(isolated,'data'), APPDATA:path.join(isolated,'appdata'),
    LOCALAPPDATA:path.join(isolated,'localappdata'), DB_NAME:'quality-ui.db', QUALITY_RUN_CONTROL:control,
    LLM_API_KEY:'', EMBEDDING_API_KEY:'', LLM_DEMO_MODE:'0', REDIS_URL:'', DESKTOP_NOTIFICATIONS:'false', PYTHONUTF8:'1' }
  let backend, vite, browser, page, log = ''
  const errors=[], external=[], checks=[]
  const p=await port(), base=`http://127.0.0.1:${p}`
  async function start() {
    backend=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','tests.quality_run_ui_server',String(p)],
      {cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']})
    backend.stdout.on('data',v=>log+=v);backend.stderr.on('data',v=>log+=v)
    await until(async()=>{if(backend.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'quality startup')
  }
  async function stop() { if(backend&&backend.exitCode===null)await new Promise(r=>{backend.once('exit',r);backend.kill()});backend=null }
  async function api(url,body){const r=await fetch(base+'/api'+url,{method:body?'POST':'GET',headers:body?{'content-type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});assert.equal(r.status,200,await r.clone().text());return r.json()}
  try {
    await start()
    const cid=(await api('/conversations',{title:'Local citation quality fixture'})).id
    const { createServer }=await import('vite')
    vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',
      server:{host:'127.0.0.1',port:await port(),strictPort:true,hmr:false,proxy:{'/api':{target:base,changeOrigin:false,ws:true}}}})
    await vite.listen();const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1400,height:1000}})
    await context.route('**/*',route=>{const u=new URL(route.request().url());if(u.origin===ui||['data:','blob:'].includes(u.protocol))return route.continue();external.push(u.origin+u.pathname);return route.abort()})
    page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)))
    await page.goto(ui+'/quality?conversation='+cid)
    await page.getByRole('button',{name:'Fixed citation signal fixture'}).click()
    await page.getByRole('button',{name:'新建固定版本分析',exact:true}).click()
    await page.getByText('1/2 条',{exact:false}).waitFor()
    await until(async()=>fs.stat(path.join(control,'second-started')).then(()=>true,()=>false),'second reference started')
    const savedURL=page.url(), rid=new URL(savedURL).searchParams.get('run')
    assert(rid);assert.equal((await api('/writing/quality/runs?conversation_id='+cid)).items.length,1)
    checks.push({name:'no_model_key_local_run_with_fixed_version_and_partial_result',passed:true})
    await page.goto(ui+'/settings');await page.goto(savedURL)
    await page.getByText('1/2 条',{exact:false}).waitFor()
    assert.equal((await api('/writing/quality/runs?conversation_id='+cid)).items.length,1)
    checks.push({name:'leave_return_observes_same_running_analysis',passed:true})
    await stop();await start();await page.reload()
    await page.getByRole('button',{name:'继续未完成分析',exact:true}).waitFor()
    assert.equal((await api('/writing/quality/runs/'+rid+'?conversation_id='+cid)).result.progress.completed,1)
    await fs.writeFile(path.join(control,'release-second'),'local fixture release')
    await page.getByRole('button',{name:'继续未完成分析',exact:true}).click()
    await page.getByText('固定版本分析已保存',{exact:false}).waitFor()
    await page.getByText('2/2 条',{exact:false}).waitFor()
    assert.equal((await api('/writing/quality/runs/'+rid+'?conversation_id='+cid)).attempt_number,2)
    checks.push({name:'actual_process_restart_resumes_only_missing_local_reference',passed:true})
    await page.locator('.citation-passages summary').first().click()
    const link=page.getByRole('link',{name:'Q2E1 · 跳转固定原句',exact:true})
    await link.click();await page.waitForURL(/\/sources\//)
    await page.locator('.source-text-line pre').filter({hasText:'Our method compares model performance [2].'}).waitFor({state:'visible'})
    checks.push({name:'citation_link_opens_fixed_original_locator',passed:true})
    await page.goto(savedURL)
    await page.getByRole('button',{name:'保存为可编辑成果',exact:true}).click()
    await page.getByRole('link',{name:'编辑已保存成果',exact:true}).waitFor()
    const artifacts=await api('/artifacts?conversation_id='+cid)
    assert.equal(artifacts.items.length,1)
    const detail=await api('/artifacts/'+artifacts.items[0].id+'?conversation_id='+cid)
    assert.equal(detail.revision.evidence.length,2)
    checks.push({name:'saved_editable_report_retains_both_original_quotes',passed:true})
    await page.screenshot({path:path.join(out,'fixed-quality.png'),fullPage:true})
    await page.setViewportSize({width:430,height:930})
    await page.screenshot({path:path.join(out,'narrow-quality.png'),fullPage:true})
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1))
    checks.push({name:'narrow_layout_has_no_horizontal_overflow',passed:true})
    assert.deepEqual(errors,[]);assert.deepEqual(external,[])
    await fs.writeFile(path.join(out,'report.json'),JSON.stringify({passed:true,checks,browser_errors:errors,external_requests:external,
      real_provider_calls:0,scope:'Actual Vue/HTTP, local original fixtures and process restart; not semantic scientific quality or installed release acceptance.'},null,2))
  } catch(error) {
    await page?.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{})
    await fs.writeFile(path.join(out,'failure.json'),JSON.stringify({error:String(error),log,errors,external},null,2));throw error
  } finally { await browser?.close();await vite?.close();backend?.kill() }
}
main().catch(error=>{console.error(error);process.exitCode=1})
