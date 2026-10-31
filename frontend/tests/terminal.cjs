// Real Windows shells, real WebSockets/HTTP and rendered desktop xterm.
const assert=require('node:assert/strict')
const fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn,execFileSync}=require('node:child_process')
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(fn,label){const end=Date.now()+25000;while(!await fn()){assert.ok(Date.now()<end,label);await pause(80)}}
async function main(){
  const data=await fs.mkdtemp(path.join(root,'verification/runtime/terminal-')),folder=path.join(data,'source 中文')
  await fs.mkdir(folder)
  const env={...process.env,DATA_DIR:data,LLM_API_KEY:'',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'}
  const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r))
  const child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']})
  let log='';child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s)
  const base=`http://127.0.0.1:${port}`,browser=await chromium.launch({headless:true}),errors=[]
  async function api(url,body,method='POST'){const r=await fetch(base+'/api'+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();assert.ok(r.ok,JSON.stringify(v));return v}
  try{
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server')
    const project=await api('/projects',{path:folder}),cid=(await api('/conversations',{title:'Desktop terminal verification',project_id:project.id})).id
    const sessions=()=>api(`/projects/${project.id}/terminals`).then(v=>v.sessions)
    const page=await browser.newPage({viewport:{width:1440,height:900}})
    page.on('pageerror',e=>errors.push(e.message))
    await page.addInitScript(()=>{const Original=window.WebSocket;window.testSockets=[];window.WebSocket=class extends Original{constructor(...args){super(...args);window.testSockets.push(this)}}})
    await page.goto(base+'/chat?conv='+cid)
    await page.getByRole('button',{name:'切换项目终端',exact:true}).click()
    const panel=page.getByRole('region',{name:'项目终端',exact:true})
    assert.equal((await sessions()).length,0)
    // A response lost after spawn must recover the same process on explicit retry.
    let lost=true
    await page.route('**/api/projects/*/terminals',async route=>{
      if(route.request().method()==='POST'&&lost){lost=false;const r=await route.fetch();assert.equal(r.status(),200);return route.abort('failed')}
      return route.continue()
    })
    await panel.getByRole('button',{name:'新建终端',exact:true}).first().click()
    await panel.getByRole('alert').waitFor()
    assert.equal((await sessions()).length,1)
    await panel.getByRole('button',{name:'重试创建终端',exact:true}).first().click()
    await panel.locator('.terminal-screen[data-connected=true]').waitFor()
    assert.equal((await sessions()).length,1)
    const first=(await sessions())[0],firstPid=first.pid
    const input=()=>panel.locator('.terminal-body:visible').getByLabel('终端输入',{exact:true})
    async function command(text){await input().focus();await page.keyboard.insertText(text);await page.keyboard.press('Enter')}
    await command("[IO.File]::WriteAllText((Join-Path $PWD 'unicode.txt'), '中文 😀'); [Console]::WriteLine('REPLAY_'+'ONCE')")
    await until(async()=>{try{return await fs.readFile(path.join(folder,'unicode.txt'),'utf8')==='中文 😀'}catch{return false}},'actual Unicode command wrote file')
    await command("[Console]::WriteLine([char]27+'[31mANSI_VISIBLE'+[char]27+'[0m'); Write-Output '<img src=x onerror=alert(1)>'")
    await until(async()=>await panel.locator('.xterm-screen').textContent().then(t=>t.includes('ANSI_VISIBLE')),'rendered output')
    assert.equal(await panel.locator('img').count(),0)
    // Closing only the socket replays output without restarting the shell or re-executing input.
    await page.evaluate(()=>window.testSockets.filter(s=>s.readyState===WebSocket.OPEN).forEach(s=>s.close()))
    await panel.locator('.terminal-screen[data-connected=false]').waitFor()
    await until(async()=>await panel.locator('.terminal-screen[data-connected=true]').count()===1,'automatic reconnect')
    await command("[IO.File]::WriteAllText((Join-Path $PWD 'after-reconnect.txt'), 'connected')")
    await until(async()=>{try{return await fs.readFile(path.join(folder,'after-reconnect.txt'),'utf8')==='connected'}catch{return false}},'reconnected input')
    assert.equal((await sessions())[0].pid,firstPid)
    await panel.getByRole('button',{name:'收起终端',exact:true}).click();await panel.waitFor({state:'hidden'})
    assert.equal((await sessions())[0].status,'running')
    await page.getByRole('button',{name:'切换项目终端',exact:true}).click()
    const wideCols=(await sessions())[0].cols
    for(const [width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(250)
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      const bounds=await panel.boundingBox();assert.ok(bounds.y>=0&&bounds.y+bounds.height<=height)
      assert.ok(await panel.locator('.terminal-screen:visible').evaluate(el=>el.clientWidth>500&&el.clientHeight>100))
      await page.screenshot({path:path.join(root,`verification/terminal-${size}.png`),animations:'disabled'})
    }
    await until(async()=>(await sessions())[0].cols<wideCols,'PTY is resized with desktop viewport')
    const separator=panel.getByRole('separator',{name:'终端面板高度',exact:true})
    const before=Number(await separator.getAttribute('aria-valuenow'));await separator.focus();await page.keyboard.press('ArrowUp')
    assert.equal(Number(await separator.getAttribute('aria-valuenow')),before+24)
    // Reload reconstructs the display while preserving the actual process and cwd.
    await page.reload();await page.getByRole('button',{name:'切换项目终端',exact:true}).click()
    await panel.locator('.terminal-screen[data-connected=true]').waitFor();assert.equal((await sessions())[0].pid,firstPid)
    await command("[IO.File]::WriteAllText((Join-Path $PWD 'after-reload.txt'), 'restored')")
    await until(async()=>{try{return await fs.readFile(path.join(folder,'after-reload.txt'),'utf8')==='restored'}catch{return false}},'input after history reconstruction')
    const other=await browser.newPage({viewport:{width:1100,height:700}})
    await other.goto(base+'/chat?conv='+cid);await other.getByRole('button',{name:'切换项目终端',exact:true}).click()
    await other.getByRole('alert').filter({hasText:'另一窗口'}).waitFor();await other.close()
    await panel.getByLabel('终端类型',{exact:true}).selectOption('cmd')
    await panel.getByRole('button',{name:'新建终端',exact:true}).first().click()
    await panel.getByRole('tabpanel',{name:'命令提示符 2',exact:true}).locator('.terminal-screen[data-connected=true]').waitFor()
    assert.equal((await sessions()).length,2)
    await command('echo cmd executed>cmd.txt')
    await until(async()=>{try{return(await fs.readFile(path.join(folder,'cmd.txt'),'utf8')).includes('cmd executed')}catch{return false}},'second shell command')
    await panel.getByRole('tab',{name:'命令提示符 2',exact:true}).focus();await page.keyboard.press('ArrowLeft')
    assert.equal(await panel.getByRole('tab',{name:'PowerShell 1',exact:true}).getAttribute('aria-selected'),'true')
    await page.keyboard.press('End')
    assert.equal(await panel.getByRole('tab',{name:'命令提示符 2',exact:true}).getAttribute('aria-selected'),'true')
    await panel.getByRole('button',{name:'关闭终端 2',exact:true}).click()
    await until(async()=>(await sessions()).length===1,'closing terminal removes process')
    await panel.getByRole('tab',{name:'PowerShell 1',exact:true}).click()
    await command("[IO.File]::WriteAllText((Join-Path $PWD 'sleep-started.txt'), 'yes'); Start-Sleep 60; [IO.File]::WriteAllText((Join-Path $PWD 'should-not-run.txt'), 'bad')")
    await until(async()=>{try{return await fs.readFile(path.join(folder,'sleep-started.txt'),'utf8')==='yes'}catch{return false}},'sleep command actually started')
    await panel.getByRole('button',{name:'中断终端命令',exact:true}).click()
    await until(async()=>await panel.locator('.terminal-body:visible .xterm-rows').textContent().then(t=>t.trimEnd().endsWith(`${folder}>`)),'shell prompt returned after interrupt')
    await command("[IO.File]::WriteAllText((Join-Path $PWD 'interrupted.txt'), 'ok')")
    await until(async()=>{try{return await fs.readFile(path.join(folder,'interrupted.txt'),'utf8')==='ok'}catch{return false}},'Ctrl-C interrupted running command')
    await assert.rejects(fs.stat(path.join(folder,'should-not-run.txt')))
    await panel.getByRole('button',{name:'结束终端进程',exact:true}).click()
    await until(async()=>(await sessions())[0].status!=='running','terminated session')
    await panel.getByRole('button',{name:'关闭终端 1',exact:true}).click()
    await until(async()=>(await sessions()).length===0,'all terminal sessions removed')
    assert.deepEqual(errors,[])
    console.log(JSON.stringify({passed:true,data,checks:'real ConPTY/xterm Unicode execution, lost create response, stream reconnect, hide/reload process retention, duplicate controller refusal, two shells/tabs, resize, Ctrl-C, stop/close and desktop layouts'}))
  }catch(error){
    for(const [index,page]of browser.contexts().flatMap(c=>c.pages()).entries()){
      await page.screenshot({path:path.join(root,`verification/terminal-debug-${index}.png`),animations:'disabled'})
      console.error((await page.locator('.xterm-rows').allTextContents().catch(()=>[])))
    }
    console.error(log.slice(-5000));throw error
  }finally{
    await browser.close()
    if(child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'})
  }
}
main().catch(e=>{console.error(e);process.exitCode=1})
