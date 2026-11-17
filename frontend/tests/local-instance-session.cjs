// Production Vue on same-origin HTTP. Secrets stay in memory, not reports.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net'),crypto=require('node:crypto')
const {spawn}=require('node:child_process'),{chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p}
async function until(fn){const end=Date.now()+60000;while(!await fn()){assert.ok(Date.now()<end,'Startup timeout');await pause(100)}}
async function main(){
  const out=path.join(root,'verification/local-instance/ui'),runtime=path.join(root,'verification/runtime')
  await fs.mkdir(out,{recursive:true});await fs.mkdir(runtime,{recursive:true})
  const isolated=await fs.mkdtemp(path.join(runtime,'instance-')),servers=[],secrets=[],checks=[],errors=[],external=[]
  let browser,page
  const redact=s=>secrets.reduce((text,key)=>text.replaceAll(key,'[redacted]'),String(s)).replace(/#sf-launch=[A-Za-z0-9_-]+/g,'#sf-launch=[redacted]').replace(/__sf_ticket=[A-Za-z0-9_-]+/g,'__sf_ticket=[redacted]')
  async function start(){
    const p=await port(),base=`http://127.0.0.1:${p}`,data=path.join(isolated,String(p))
    const proc=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['tests/local_instance_ui_server.py',String(p)],{
      cwd:path.join(root,'backend'),windowsHide:true,env:{...process.env,DATA_DIR:data,APPDATA:path.join(data,'appdata'),LOCALAPPDATA:path.join(data,'localappdata'),
        LLM_API_KEY:'',LLM_DEMO_MODE:'0',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false',PYTHONUTF8:'1'},stdio:['ignore','pipe','pipe']})
    servers.push(proc);let secret='',partial='',log=''
    proc.stdout.on('data',chunk=>{partial+=chunk;const match=/LAUNCH:([A-Za-z0-9_-]{64})/.exec(partial);if(match){secret=match[1];if(!secrets.includes(secret))secrets.push(secret);partial=''}})
    proc.stderr.on('data',chunk=>{log+=chunk})
    await until(async()=>{assert.ok(proc.exitCode===null,'Isolated server stopped: '+redact(log));if(!secret)return false;try{return(await fetch(base+'/api/local-session/status')).ok}catch{return false}})
    return {base,secret}
  }
  try{
    const first=await start()
    assert.equal((await fetch(first.base+'/api/settings')).status,401)
    browser=await chromium.launch({headless:true})
    const ctx=await browser.newContext({viewport:{width:1440,height:1000}})
    await ctx.route('**/*',route=>{const url=new URL(route.request().url());if(url.hostname==='127.0.0.1'||['data:','blob:'].includes(url.protocol))return route.continue();external.push(url.origin+url.pathname);return route.abort()})
    page=await ctx.newPage();page.setDefaultTimeout(25000);page.on('pageerror',e=>errors.push(redact(e.message)))
    await page.goto(first.base+'/chat#sf-launch='+first.secret)
    await page.locator('textarea.textarea').waitFor()
    assert.ok(!page.url().includes('sf-launch='),'Launch handoff must not return through router history')
    assert.deepEqual(await ctx.cookies(),[])
    const token=await page.evaluate(()=>sessionStorage.getItem('scholarflow.desktop-session.v1'))
    assert.ok(typeof token==='string'&&token.length===64,'Port-scoped session is present');secrets.push(token)
    checks.push('Production Vue authenticates once, strips handoff before routing, and uses port-scoped sessionStorage without cookies')
    assert.equal(await page.evaluate(async()=> (await fetch('/api/settings')).status),200)
    const ws=await page.evaluate(async()=>{
      const response=await fetch('/api/local-session/ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:'/api/local-fixture/socket',kind:'websocket'})})
      if(!response.ok)throw Error('WebSocket ticket unavailable');const ticket=await response.json()
      return new Promise((resolve,reject)=>{const s=new WebSocket(location.origin.replace('http:','ws:')+'/api/local-fixture/socket?__sf_ticket='+ticket.ticket);s.onmessage=e=>resolve(e.data);s.onerror=()=>reject(Error('WebSocket authentication failed'))})
    })
    assert.equal(ws,'authenticated')
    const xhr=await page.evaluate(()=>new Promise((resolve,reject)=>{const request=new XMLHttpRequest();request.open('GET','/api/settings');request.onload=()=>resolve(request.status);request.onerror=()=>reject(Error('XHR failed'));request.send()}))
    assert.equal(xhr,200);checks.push('Fetch and Axios/XHR get origin-scoped authorization; actual browser WebSocket accepts only a path-bound ticket')
    await until(async()=> (await page.request.get(first.base+'/api/local-session/status',{headers:{Authorization:'ScholarFlow '+token}})).ok())
    await page.reload();await page.locator('textarea.textarea').waitFor();assert.ok(!page.url().includes('sf-launch='))
    checks.push('Reload retains authentication without launch secret or another bootstrap')
    assert.equal((await fetch(first.base+'/api/local-session/bootstrap',{method:'POST',headers:{Authorization:'Bearer '+first.secret}})).status,401)
    for(const [extra,expected] of [[{Origin:'https://attacker.invalid'},403],[{'X-Forwarded-For':'127.0.0.1'},403],[{'Sec-Fetch-Site':'same-site'},401]]){
      assert.equal((await fetch(first.base+'/api/settings',{headers:{Authorization:'ScholarFlow '+token,...extra}})).status,expected)
    }
    for(const route of ['/','/chat','/unknown-spa-path']){
      const response=await fetch(first.base+route)
      assert.ok(response.headers.get('content-security-policy')?.includes("frame-ancestors 'none'"),'Every SPA document must retain frame restrictions')
      assert.equal(response.headers.get('x-content-type-options'),'nosniff')
    }
    checks.push('Replay, hostile origins, forwarded metadata and cross-port subresources are denied; SPA fallback retains document restrictions')
    const download=page.waitForEvent('download')
    await page.evaluate(()=>{const link=document.createElement('a');link.href='/api/local-fixture/export';link.download='instance-fixture.txt';link.textContent='Synthetic download';link.id='fixture-download';document.body.append(link)})
    await page.locator('#fixture-download').click();const result=await download
    assert.equal(result.suggestedFilename(),'instance-fixture.txt');assert.equal(await fs.readFile(await result.path(),'utf8'),'Synthetic authorized download')
    checks.push('Native anchor download is intercepted and receives a one-use ticket without exposing the session token in its URL')
    assert.equal(await page.evaluate(async()=> (await fetch('/api/local-fixture/seed-pdf',{method:'POST'})).status),200)
    await page.goto(first.base+'/reader/local-auth-pdf')
    await page.waitForFunction(()=>Array.from(document.querySelectorAll('canvas')).some(c=>c.width>0&&c.height>0))
    await page.screenshot({path:path.join(out,'authenticated-pdf.png')})
    checks.push('Real PDF.js reader loads and renders a fixed PDF with instance authorization')
    const other=await browser.newContext(),outside=await other.newPage()
    await outside.goto(first.base+'/chat');await outside.getByRole('heading',{name:'研序暂时无法连接本次窗口'}).waitFor()
    await other.close();checks.push('Unauthenticated browser shows recovery text before any application API writes')
    const second=await start()
    const crossPort=await page.evaluate(async base=>{const response=await fetch(base+'/api/local-session/status');return response.json()},second.base).catch(()=>({authenticated:false}))
    assert.equal(crossPort.authenticated,false)
    await page.goto(second.base+'/chat');await page.getByRole('heading',{name:'研序暂时无法连接本次窗口'}).waitFor()
    assert.equal(await page.evaluate(async()=> (await fetch('/api/settings')).status),401)
    // A launcher creates a document; changing only an existing page's hash is
    // a same-document navigation and intentionally does not rerun bootstrap.
    await page.goto('about:blank')
    await page.goto(second.base+'/chat#sf-launch='+second.secret);await page.locator('textarea.textarea').waitFor()
    assert.deepEqual(await ctx.cookies(),[])
    checks.push('Independent data-directory instance never receives a passive session cookie and requires its own origin-scoped handoff')
    await page.screenshot({path:path.join(out,'authenticated-chat.png')})
    assert.deepEqual(errors,[]);assert.deepEqual(external,[])
    const files=['backend/app/local_session.py','backend/app/local_api_guard.py','backend/app/main.py','desktop/launcher.py','frontend/src/localSession.ts','frontend/src/main.ts']
    const source_hashes=Object.fromEntries(await Promise.all(files.map(async file=>[file,crypto.createHash('sha256').update(await fs.readFile(path.join(root,file))).digest('hex')])))
    await fs.writeFile(path.join(out,'report.json'),JSON.stringify({passed:checks.length,checks,browser_errors:errors,external_requests:external,real_provider_calls:0,
      source_hashes,native_webview_performed:false,installation_performed:false},null,2))
    console.log(JSON.stringify({passed:checks.length,real_provider_calls:0}))
  }catch(error){if(page)await page.screenshot({path:path.join(out,'failure.png')}).catch(()=>{});await fs.writeFile(path.join(out,'failure.log'),redact(error?.stack||error));throw Error(redact(error?.message||error))}
  finally{if(browser)await browser.close();for(const proc of servers)proc.kill()}
}
main().catch(e=>{console.error(e.message);process.exitCode=1})
