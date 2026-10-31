// Actual Windows commands and SQLite/HTTP/SSE; deterministic model requests only.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn,execFileSync}=require('node:child_process')
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(fn,label){const end=Date.now()+25000;while(!await fn()){assert.ok(Date.now()<end,label);await pause(80)}}
async function main(){
  const data=await fs.mkdtemp(path.join(root,'verification/runtime/commands-')),folder=path.join(data,'project 中文')
  await fs.mkdir(folder)
  let child,base,log='',page
  async function start(){
    const server=net.createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r))
    child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),[path.join(root,'backend/tests/project_command_server.py'),String(port)],{cwd:path.join(root,'backend'),env:{...process.env,DATA_DIR:data,LLM_API_KEY:'',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'},windowsHide:true,stdio:['ignore','pipe','pipe']})
    child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s);base=`http://127.0.0.1:${port}`
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server ready')
  }
  function stop(){if(child&&child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child=null}
  async function api(url,body,method='POST'){const r=await fetch(base+'/api'+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();assert.ok(r.ok,JSON.stringify(v));return v}
  const browser=await chromium.launch({headless:true}),errors=[]
  function setup(p){p.on('pageerror',e=>errors.push(e.message))}
  try{
    await start()
    const project=await api('/projects',{path:folder}),cid=(await api('/conversations',{title:'项目命令桌面验收',project_id:project.id})).id
    page=await browser.newPage({viewport:{width:1440,height:900}});setup(page)
    const review=()=>page.getByRole('dialog',{name:'命令与输出',exact:true})
    const history=()=>api(`/projects/${project.id}/commands`)
    await page.goto(base+'/chat?conv='+cid)
    await page.locator('textarea.textarea').fill('SHORT_COMMAND')
    await page.getByRole('button',{name:'发送',exact:true}).click()
    await page.locator('.approval-card').getByRole('button',{name:'查看命令记录',exact:true}).click()
    await review().getByText('待审批',{exact:true}).waitFor()
    assert.ok((await history()).commands[0].command.includes('actual.txt'))
    await assert.rejects(fs.stat(path.join(folder,'actual.txt')))
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    await page.locator('.approval-card').getByRole('button',{name:'批准一次',exact:true}).click()
    await until(async()=>(await api('/conversations/'+cid)).messages.length===1,'foreground completed')
    assert.equal(await fs.readFile(path.join(folder,'actual.txt'),'utf8'),'中文 😀')
    await page.reload()
    await page.locator('.turn-commands button').click()
    await review().getByText('退出码 0',{exact:true}).waitFor()
    await review().getByLabel('命令输出',{exact:true}).filter({hasText:'DONE 中文 😀'}).waitFor()
    assert.equal(await review().locator('img').count(),0)
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    // Background commands use the same review and live output, and stop the owning run.
    const waitCid=(await api('/conversations',{title:'后台运行与停止',project_id:project.id})).id
    const waitTask=await api('/tasks/start',{question:'WAIT_COMMAND',conversation_id:waitCid,project_id:project.id})
    await page.goto(base+'/tasks')
    await page.locator('.task-approval').getByRole('button',{name:'允许一次',exact:true}).click()
    await until(async()=>(await history()).commands.some(c=>c.status==='running'),'actually running')
    await page.getByRole('button',{name:'命令与输出 · 桌面命令验收',exact:true}).first().click()
    await review().getByLabel('命令输出',{exact:true}).filter({hasText:'RUNNING 中文 😀'}).waitFor()
    await review().getByRole('button',{name:'停止命令与本轮执行',exact:true}).click()
    await review().getByText('已停止',{exact:true}).waitFor()
    await assert.rejects(fs.stat(path.join(folder,'never.txt')))
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    await until(async()=>!(await api('/tasks/'+waitTask.id)).executing,'background stopped')
    // Large real output is paged, escaped, downloadable, and restored at a new origin.
    const largeCid=(await api('/conversations',{title:'分页命令输出',project_id:project.id})).id
    const largeTask=await api('/tasks/start',{question:'LARGE_COMMAND',conversation_id:largeCid,project_id:project.id})
    await until(async()=>(await api('/tasks/'+largeTask.id)).approvals?.length,'large approval')
    const approval=(await api('/tasks/'+largeTask.id)).approvals[0]
    await api('/approvals/decide',{approval_id:approval.approval_id,decision:'approve',scope:'once'})
    await until(async()=>(await api('/tasks/'+largeTask.id)).status==='done','large completed')
    const saved=(await history()).commands.find(c=>c.conversation_id===largeCid)
    assert.ok(saved.total_chars>16000)
    await page.goto(base+'/chat?conv='+largeCid)
    await page.locator('.project-context-button').click()
    const workspace=()=>page.getByRole('dialog',{name:'项目文件',exact:true})
    await workspace().getByRole('tab',{name:'命令',exact:true}).click()
    const rows=()=>workspace().locator('.command-row')
    await until(async()=>await rows().count()===3,'three persisted commands')
    await rows().first().click()
    await review().getByLabel('命令输出',{exact:true}).filter({hasText:'TAIL 中文 😀'}).waitFor()
    assert.ok(!(await review().getByLabel('命令输出',{exact:true}).textContent()).includes('HEAD 中文 😀'))
    await review().getByRole('button',{name:'从头阅读',exact:true}).click()
    await review().getByLabel('命令输出',{exact:true}).filter({hasText:'HEAD 中文 😀'}).waitFor()
    const before=await review().getByLabel('命令输出',{exact:true}).textContent()
    await review().getByRole('button',{name:'下一段',exact:true}).click()
    await until(async()=>await review().getByLabel('命令输出',{exact:true}).textContent()!==before,'next output page')
    assert.equal(await review().locator('img').count(),0)
    const download=await fetch(base+`/api/project-commands/${saved.id}/output`)
    const text=await download.text();assert.ok(text.includes('HEAD 中文 😀')&&text.includes('TAIL 中文 😀'))
    for(const[width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(180)
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      const bounds=await review().boundingBox();assert.ok(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=width&&bounds.y+bounds.height<=height)
      const close=await review().getByRole('button',{name:'关闭',exact:true}).boundingBox();assert.ok(close&&close.y+close.height<=height)
      await page.screenshot({path:path.join(root,`verification/project-commands-${size}.png`),animations:'disabled'})
    }
    // A late failed response from a previous selection must not overwrite the new one.
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    let held
    await page.route(`**/api/project-commands/${saved.id}?*`,async route=>{held=route;await pause(900);await route.fulfill({status:503,contentType:'application/json',body:'{"detail":"old failed response"}'})})
    await rows().first().click();await until(async()=>!!held,'request delayed')
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    await rows().last().click()
    await review().getByLabel('命令输出',{exact:true}).filter({hasText:'DONE 中文 😀'}).waitFor()
    await pause(1100);assert.ok(!(await review().textContent()).includes('old failed response'))
    await page.unroute(`**/api/project-commands/${saved.id}?*`)
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    // New process/port reads the saved record without running the old command.
    const oldBase=base;await page.close();stop();await start();assert.notEqual(base,oldBase)
    page=await browser.newPage({viewport:{width:1100,height:700}});setup(page)
    await page.goto(base+'/chat?conv='+largeCid)
    await page.locator('.turn-commands button').click()
    await review().getByLabel('命令输出',{exact:true}).filter({hasText:'TAIL 中文 😀'}).waitFor()
    assert.equal(await fs.readFile(path.join(folder,'actual.txt'),'utf8'),'中文 😀')
    assert.equal((await history()).total,3)
    assert.deepEqual(errors,[])
    console.log('PASS: real model-command approval, foreground/background history, live output/stop, Unicode/escaping, output paging/export, stale responses, both desktop sizes and new-port restart without replay')
  }catch(e){if(page&&!page.isClosed())await page.screenshot({path:path.join(root,'verification/project-commands-failure.png')});throw e}
  finally{await browser.close();stop();await fs.writeFile(path.join(data,'test-server.log'),log)}
}
main().catch(e=>{console.error(e);process.exitCode=1})
