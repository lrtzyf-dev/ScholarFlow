// Real HTTP/SQLite/Git and two independent desktop windows, with a lost response.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn,execFileSync}=require('node:child_process')
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(fn,label){const end=Date.now()+30000;while(!await fn()){assert.ok(Date.now()<end,label);await pause(100)}}
async function main(){
  const data=await fs.mkdtemp(path.join(root,'verification/runtime/workspace-switch-')),folder=path.join(data,'来源项目 中文')
  await fs.mkdir(folder)
  const git=(cwd,...args)=>execFileSync('git',['-C',cwd,...args],{encoding:'utf8',windowsHide:true}).trim()
  git(folder,'init','-b','main');git(folder,'config','user.name','Desktop Test');git(folder,'config','user.email','test@example.invalid');git(folder,'config','commit.gpgsign','false')
  await fs.writeFile(path.join(folder,'source.txt'),'original\n');git(folder,'add','.');git(folder,'commit','-m','Initial')
  let child,base,log='',page,other
  const env={...process.env,DATA_DIR:data,LLM_API_KEY:'',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'}
  async function start(){
    const server=net.createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r))
    child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']})
    child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s);base=`http://127.0.0.1:${port}`
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server ready')
  }
  function stop(){if(child&&child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child=null}
  async function api(url,body,method='POST'){const r=await fetch(base+'/api'+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();assert.ok(r.ok,JSON.stringify(v));return v}
  const browser=await chromium.launch({headless:true}),errors=[]
  function setup(p){p.on('pageerror',e=>errors.push(e.message))}
  try{
    await start()
    const project=await api('/projects',{path:folder}),cid=(await api('/conversations',{title:'会话工作目录切换验收',project_id:project.id})).id
    const tree=await api(`/projects/${project.id}/worktrees`,{request_id:'switch_tree_001',name:'目标工作树 <img src=x> 😀',ref:'HEAD'})
    assert.equal(tree.status,'ready')
    await fs.writeFile(path.join(folder,'source.txt'),'source staged\n');git(folder,'add','.')
    await fs.writeFile(path.join(tree.path,'source.txt'),'target remains different\n')
    const draftPath=`/desktop-state/conversations/${cid}`,state=await api(draftPath)
    await api(draftPath,{revision:state.revision,value:{...state.value,text:'文字草稿保留',file_paths:['source.txt'],
      editor:{project_id:project.id,path:'source.txt',operation:'update',sha256:(await api(`/projects/${project.id}/file?path=source.txt`)).sha256,content:'编辑草稿仍在来源项目'}}},'PUT')
    page=await browser.newPage({viewport:{width:1440,height:900}});setup(page)
    other=await browser.newPage({viewport:{width:1100,height:700}});setup(other)
    const workspace=()=>page.getByRole('dialog',{name:'项目文件',exact:true})
    const panel=()=>page.getByLabel('项目工作树',{exact:true})
    const review=()=>page.getByRole('dialog',{name:'切换会话工作目录',exact:true})
    const editor=p=>p.locator('textarea.textarea')
    async function open(){await page.locator('.project-context-button').click();await workspace().getByRole('tab',{name:'工作树',exact:true}).click()}
    await page.goto(base+'/chat?conv='+cid);await other.goto(base+'/chat?conv='+cid)
    await until(async()=>await editor(page).inputValue()==='文字草稿保留'&&await editor(other).inputValue()==='文字草稿保留','load same draft')
    // Keep the second window's unsaved text while the first switches checkout.
    await other.route('**/api'+draftPath,route=>route.request().method()==='PUT'?route.abort('failed'):route.continue())
    await editor(other).fill('另一窗口尚未保存的文字')
    await other.getByRole('button',{name:'重试保存',exact:true}).waitFor()
    await open()
    await panel().getByRole('button',{name:'切换当前会话到此目录',exact:true}).click()
    await review().waitFor()
    assert.equal(await review().locator('img').count(),0)
    assert.ok((await review().textContent()).includes('编辑草稿'))
    for(const[width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(180)
      const b=await review().locator('.el-dialog').boundingBox();assert.ok(b.x>=0&&b.y>=0&&b.x+b.width<=width&&b.y+b.height<=height,JSON.stringify(b))
      const button=review().getByRole('button',{name:'确认切换工作目录',exact:true})
      assert.ok(await button.evaluate(el=>{const b=el.getBoundingClientRect();return el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2))}))
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      await page.screenshot({path:path.join(root,`verification/workspace-switch-review-${size}.png`),animations:'disabled'})
    }
    let lost=false
    await page.bringToFront()
    await review().getByRole('button',{name:'确认切换工作目录',exact:true}).focus()
    for(let i=0;i<6;i++){
      await page.keyboard.press('Tab');await pause(50)
      const focus=await page.evaluate(()=>({active:document.activeElement.tagName,focused:document.hasFocus(),dialogs:[...document.querySelectorAll('[role=dialog]')].filter(el=>el.contains(document.activeElement)).map(el=>el.getAttribute('aria-label'))}))
      assert.ok(await review().evaluate(el=>el.contains(document.activeElement)),JSON.stringify({i,...focus}))
    }
    await page.keyboard.press('Escape');await review().waitFor({state:'hidden'})
    await panel().getByRole('button',{name:/^继续审阅：/}).click()
    await review().waitFor()
    await page.route('**/workspace-switches/*/apply',async route=>{if(!lost){lost=true;await route.fetch();await route.abort('failed')}else await route.continue()})
    await review().getByRole('button',{name:'确认切换工作目录',exact:true}).click()
    await review().getByRole('alert').waitFor()
    await until(async()=>(await api(`/conversations/${cid}/project`)).project.id===tree.project_id,'server committed switch')
    await review().getByRole('button',{name:'确认切换工作目录',exact:true}).click()
    await review().getByText('此切换已完成。后续操作以会话当前工作目录为准。',{exact:true}).waitFor()
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    await workspace().getByRole('button',{name:'完成',exact:true}).click()
    await until(async()=>(await page.locator('.project-context-button').textContent()).includes('目标工作树'),'first window current project')
    assert.equal(await editor(page).inputValue(),'文字草稿保留')
    assert.equal(await page.locator('.project-file-chip').count(),0)
    await other.getByRole('button',{name:'保留本窗口草稿',exact:true}).waitFor()
    assert.equal(await editor(other).inputValue(),'另一窗口尚未保存的文字')
    await other.unroute('**/api'+draftPath)
    await other.getByRole('button',{name:'保留本窗口草稿',exact:true}).click()
    await until(async()=>{const d=await api(draftPath);return d.value.text==='另一窗口尚未保存的文字'&&d.value.workspace_revision===1},'keep local text in new scope')
    const saved=await api(draftPath)
    assert.deepEqual(saved.value.file_paths,[]);assert.equal(saved.value.editor.project_id,project.id)
    assert.equal(saved.value.editor.content,'编辑草稿仍在来源项目')
    await other.close();await page.close();stop();await start()
    page=await browser.newPage({viewport:{width:1100,height:700}});setup(page)
    await page.goto(base+'/chat?conv='+cid)
    await until(async()=>await editor(page).inputValue()==='另一窗口尚未保存的文字','draft after new process and port')
    await open()
    await panel().getByRole('button',{name:'返回来源目录',exact:true}).click()
    await review().getByRole('button',{name:'确认切换工作目录',exact:true}).click()
    await review().getByText('此切换已完成。后续操作以会话当前工作目录为准。',{exact:true}).waitFor()
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    await workspace().getByRole('button',{name:'完成',exact:true}).click()
    await until(async()=>(await api(`/conversations/${cid}/project`)).workspace_revision===2,'returned with new revision')
    await open()
    await panel().locator('summary').filter({hasText:'当前会话的目录切换记录'}).click()
    await panel().locator('.switch-history button').last().scrollIntoViewIfNeeded()
    await page.screenshot({path:path.join(root,'verification/workspace-switch-history-small-desktop.png'),animations:'disabled'})
    await workspace().getByRole('tab',{name:'文件',exact:true}).click()
    await workspace().getByRole('button',{name:'继续编辑草稿',exact:true}).click()
    const fileEditor=page.getByRole('dialog',{name:'编辑项目文件',exact:true})
    await until(async()=>await fileEditor.getByLabel('文件编辑内容').inputValue()==='编辑草稿仍在来源项目','editor returns to original project')
    assert.equal(await fs.readFile(path.join(folder,'source.txt'),'utf8'),'source staged\n')
    assert.equal(await fs.readFile(path.join(tree.path,'source.txt'),'utf8'),'target remains different\n')
    assert.equal(git(folder,'show',':source.txt'),'source staged')
    assert.equal(git(tree.path,'diff','--cached'),'')
    assert.equal((await api(`/conversations/${cid}/workspace-switches`)).switches.length,2)
    // The same editor component must create a fresh proposal after another A-B-A.
    const changeReview=page.getByRole('dialog',{name:'文件差异',exact:true})
    async function previewEditor(){
      await fileEditor.getByRole('button',{name:'查看修改差异',exact:true}).click()
      await changeReview.waitFor();await changeReview.locator('.el-dialog__headerbtn').click()
      await fileEditor.getByRole('button',{name:'关闭并保留草稿',exact:true}).click()
    }
    await previewEditor()
    const firstChange=(await api(`/projects/${project.id}/changes`)).changes[0]
    await workspace().getByRole('tab',{name:'工作树',exact:true}).click()
    for(const button of ['切换当前会话到此目录','返回来源目录']){
      await panel().getByRole('button',{name:button,exact:true}).click()
      await review().getByRole('button',{name:'确认切换工作目录',exact:true}).click()
      await review().getByText('此切换已完成。后续操作以会话当前工作目录为准。',{exact:true}).waitFor()
      await review().getByRole('button',{name:'关闭',exact:true}).click()
    }
    await workspace().getByRole('tab',{name:'文件',exact:true}).click()
    await workspace().getByRole('button',{name:'继续编辑草稿',exact:true}).click()
    await previewEditor()
    const edits=(await api(`/projects/${project.id}/changes`)).changes
    assert.equal(edits.length,2);assert.notEqual(edits[0].id,firstChange.id)
    assert.equal((await api(`/conversations/${cid}/project`)).workspace_revision,4)
    assert.deepEqual(errors,[])
    console.log('PASS: 2 desktop sizes, real Git attachment/return, preserved files/index/editor/text, cleared references, two-window stale draft resolution, lost response idempotency, new process/port recovery, escaped names/history, Tab/Escape and fresh editor proposals after A-B-A')
  }catch(e){if(page&&!page.isClosed())await page.screenshot({path:path.join(root,'verification/workspace-switch-failure.png'),fullPage:true}).catch(()=>{});console.error(log.slice(-6000));throw e}
  finally{await browser.close();stop();await fs.writeFile(path.join(data,'server.log'),log)}
}
main().catch(e=>{console.error(e);process.exitCode=1})
