// Actual Vue with isolated local backend; no model calls or container launches.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const {spawn} = require('node:child_process')
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function main(){
  const output=path.join(root,'verification/execution-environments/ui')
  await fs.mkdir(output,{recursive:true})
  const temp=await fs.mkdtemp(path.join(require('node:os').tmpdir(),'sf-environment-'))
  const reserved=net.createServer(); await new Promise(resolve=>reserved.listen(0,'127.0.0.1',resolve))
  const port=reserved.address().port; await new Promise(resolve=>reserved.close(resolve))
  const base=`http://127.0.0.1:${port}`
  const server=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{
    cwd:path.join(root,'backend'),windowsHide:true,stdio:['ignore','pipe','pipe'],
    env:{...process.env,DATA_DIR:path.join(temp,'data'),APPDATA:path.join(temp,'appdata'),LOCALAPPDATA:path.join(temp,'localappdata'),
      LLM_API_KEY:'',LLM_DEMO_MODE:'1',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'}})
  let log='',vite,page;server.stdout.on('data',chunk=>log+=chunk);server.stderr.on('data',chunk=>log+=chunk)
  const browser=await chromium.launch({headless:true})
  const context=await browser.newContext({viewport:{width:1360,height:1000}})
  const errors=[],external=[],checks=[]
  context.on('page',p=>p.on('pageerror',e=>errors.push(e.message)))
  await context.route('**/*',route=>{
    const url=route.request().url()
    if(url.startsWith('http')&&!/^http:\/\/127\.0\.0\.1:/.test(url)){external.push(url);return route.abort()}
    return route.continue()
  })
  async function api(route,body,method='POST'){
    const r=await fetch(base+'/api'+route,body===undefined?undefined:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    assert.equal(r.status,200,await r.clone().text());return r.json()
  }
  let report={status:'failed',checks,model_calls:0,container_runs:0}
  try{
    const deadline=Date.now()+30000
    while(Date.now()<deadline){try{if((await fetch(base+'/api/health')).ok)break}catch{}if(server.exitCode!==null)throw Error(log);await pause(100)}
    const {createServer}=await import('vite')
    vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',
      server:{host:'127.0.0.1',port:0,hmr:false,proxy:{'/api':{target:base,changeOrigin:false}}}})
    await vite.listen();const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    const a=await api('/conversations',{title:'执行环境A'}),b=await api('/conversations',{title:'执行环境B'})
    page=await context.newPage();await page.goto(`${ui}/chat?conv=${a.id}`)
    await page.getByLabel('会话执行环境',{exact:true}).click()
    const panel=page.locator('.environment-panel')
    await panel.getByLabel('命令执行方式',{exact:true}).selectOption('container')
    await panel.getByLabel('容器固定镜像',{exact:true}).fill('ubuntu:latest')
    await panel.getByRole('button',{name:'保存任务执行环境',exact:true}).click()
    await panel.getByRole('alert').waitFor()
    assert.equal(await panel.getByLabel('容器固定镜像',{exact:true}).inputValue(),'ubuntu:latest')
    assert.equal((await api(`/conversation-execution-environments/${a.id}`,undefined)).revision,0)
    checks.push('invalid unpinned image rejected and draft retained in actual UI')
    const image='example/research@sha256:'+'a'.repeat(64)
    await panel.getByLabel('容器固定镜像',{exact:true}).fill(image)
    await panel.getByRole('button',{name:'保存任务执行环境',exact:true}).click()
    await panel.getByText('已保存；新的任务按此环境运行，既有命令不会静默切换。',{exact:true}).waitFor()
    assert.equal((await api(`/conversation-execution-environments/${a.id}`,undefined)).mode,'container')
    await page.reload();await page.getByLabel('会话执行环境',{exact:true}).click()
    assert.equal(await panel.getByLabel('命令执行方式',{exact:true}).inputValue(),'container')
    checks.push('explicit container selection persists across reload without launching anything')
    await api(`/conversation-execution-environments/${a.id}`,{mode:'host',image:'',write_path:'',expected_revision:1},'PUT')
    await panel.getByLabel('容器输出目录',{exact:true}).fill('retained-output')
    await panel.getByRole('button',{name:'保存任务执行环境',exact:true}).click()
    await panel.getByRole('alert').waitFor()
    assert.equal(await panel.getByLabel('容器输出目录',{exact:true}).inputValue(),'retained-output')
    assert.equal((await api(`/conversation-execution-environments/${a.id}`,undefined)).mode,'host')
    checks.push('stale revision cannot overwrite another window and retains current input')
    const nextLoaded=page.waitForResponse(r=>r.url().includes(`/api/conversation-execution-environments/${b.id}`)&&r.request().method()==='GET')
    await page.goto(`${ui}/chat?conv=${b.id}`);await nextLoaded;await page.getByLabel('会话执行环境',{exact:true}).click()
    await panel.getByRole('button',{name:'保存任务执行环境',exact:true}).waitFor({state:'visible'})
    await page.waitForFunction(()=>!document.querySelector('.environment-panel button:last-of-type')?.disabled)
    assert.equal(await panel.getByLabel('命令执行方式',{exact:true}).inputValue(),'host')
    checks.push('switching conversations keeps execution preferences isolated')
    await page.screenshot({path:path.join(output,'environment-panel.png'),fullPage:true})
    await page.setViewportSize({width:650,height:900})
    const box=await panel.boundingBox();assert(box.x>=0&&box.x+box.width<=650)
    await page.screenshot({path:path.join(output,'environment-narrow.png'),fullPage:true})
    checks.push('environment panel remains inside narrow viewport')
    assert.deepEqual(errors,[]);assert.deepEqual(external,[])
    report={status:'passed',checks,model_calls:0,container_runs:0,browser_errors:errors,external_requests:external,
      limitation:'UI and admission only; no actual OS container isolation test'}
  }catch(e){report.error=e.stack;await page?.screenshot({path:path.join(output,'failure.png'),fullPage:true});throw e}
  finally{await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await browser.close();await vite?.close();server.kill();await fs.writeFile(path.join(output,'backend.log'),log)}
  console.log(JSON.stringify(report))
}
main().catch(e=>{console.error(e);process.exitCode=1})
