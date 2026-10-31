// Real desktop browser, backend, Git files/commits, persisted drafts and restart.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn,execFileSync}=require('node:child_process')
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(fn,label){const end=Date.now()+30000;while(!await fn()){assert.ok(Date.now()<end,label);await pause(100)}}
async function main(){
  const data=await fs.mkdtemp(path.join(root,'verification/runtime/worktrees-')),folder=path.join(data,'来源项目 中文')
  await fs.mkdir(folder)
  const git=(cwd,...args)=>execFileSync('git',['-C',cwd,...args],{encoding:'utf8',windowsHide:true}).trim()
  git(folder,'init','-b','main');git(folder,'config','user.name','Desktop Test');git(folder,'config','user.email','test@example.invalid');git(folder,'config','commit.gpgsign','false')
  await fs.writeFile(path.join(folder,'source.txt'),'original\n');git(folder,'add','.');git(folder,'commit','-m','Initial')
  await fs.writeFile(path.join(folder,'source.txt'),'parent remains dirty\n')
  let child,base,log='',page
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
    const project=await api('/projects',{path:folder}),cid=(await api('/conversations',{title:'工作树桌面验收',project_id:project.id})).id
    const listing=()=>api(`/projects/${project.id}/worktrees`)
    page=await browser.newPage({viewport:{width:1440,height:900}});setup(page)
    const workspace=()=>page.getByRole('dialog',{name:'项目文件',exact:true})
    const panel=()=>page.getByLabel('项目工作树',{exact:true})
    const row=()=>panel().locator('.worktree-row').first()
    async function open(p=page,conversation=cid){await p.goto(base+'/chat?conv='+conversation);await p.locator('.project-context-button').click();await p.getByRole('dialog',{name:'项目文件',exact:true}).getByRole('tab',{name:'工作树',exact:true}).click()}
    await open()
    await panel().getByRole('button',{name:'创建工作树',exact:true}).click()
    await panel().getByLabel('工作树名称',{exact:true}).fill('导出修复 中文 😀 <img src=x>')
    await panel().getByLabel('新分支（可选）',{exact:true}).fill('feature/desktop')
    for(const[width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(220)
      const b=await workspace().locator('.el-dialog').boundingBox();assert.ok(b.x>=0&&b.y>=0&&b.x+b.width<=width&&b.y+b.height<=height,JSON.stringify(b))
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      await panel().getByRole('button',{name:'创建独立目录',exact:true}).scrollIntoViewIfNeeded()
      const button=await panel().getByRole('button',{name:'创建独立目录',exact:true}).boundingBox();assert.ok(button&&button.y+button.height<=height)
      assert.ok(await panel().getByRole('button',{name:'创建独立目录',exact:true}).evaluate(el=>{const b=el.getBoundingClientRect();return el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2))}))
      await page.screenshot({path:path.join(root,`verification/worktree-create-${size}.png`),animations:'disabled'})
    }
    // Server creates the checkout but the HTTP response is lost. Same saved request recovers it.
    let lost=false
    await page.route(`**/api/projects/${project.id}/worktrees`,async route=>{
      if(route.request().method()==='POST'&&!lost){lost=true;await route.fetch();await route.abort('failed')}else await route.continue()
    })
    await panel().getByRole('button',{name:'创建独立目录',exact:true}).click()
    await panel().getByRole('button',{name:'查询或继续创建',exact:true}).waitFor()
    await until(async()=>(await listing()).worktrees.length===1,'created before lost response')
    await panel().getByRole('button',{name:'查询或继续创建',exact:true}).click()
    await panel().getByText('工作树已创建，可在此开始独立会话。',{exact:true}).waitFor()
    await page.unroute(`**/api/projects/${project.id}/worktrees`)
    const worktree=(await listing()).worktrees[0],checkout=worktree.path
    assert.equal((await listing()).worktrees.length,1)
    assert.equal((await fs.readFile(path.join(checkout,'source.txt'),'utf8')).trim(),'original')
    assert.equal(await fs.readFile(path.join(folder,'source.txt'),'utf8'),'parent remains dirty\n')
    assert.equal(git(checkout,'branch','--show-current'),'feature/desktop')
    assert.equal(await panel().locator('img').count(),0)
    // New chat is bound to checkout while the original chat keeps its project.
    await row().getByRole('button',{name:'在此新建会话',exact:true}).click()
    await workspace().waitFor({state:'hidden'})
    const worktreeCid=new URL(page.url()).searchParams.get('conv');assert.ok(worktreeCid&&worktreeCid!==cid)
    assert.equal((await api('/conversations/'+worktreeCid+'/project')).project.id,worktree.project_id)
    assert.equal((await api('/conversations/'+cid+'/project')).project.id,project.id)
    await open(page,worktreeCid)
    // Unsaved files and changes after removal review both refuse deletion.
    const unsaved=path.join(checkout,'unsaved.txt');await fs.writeFile(unsaved,'keep')
    await row().getByRole('button',{name:'审阅移除',exact:true}).click()
    await panel().getByRole('alert').filter({hasText:'未提交、未跟踪或被忽略'}).waitFor()
    await fs.unlink(unsaved)
    await row().getByRole('button',{name:'审阅移除',exact:true}).click()
    const removal=()=>page.getByRole('dialog',{name:'移除工作树',exact:true})
    await removal().waitFor()
    await fs.writeFile(path.join(checkout,'source.txt'),'worktree changes\n')
    await removal().getByRole('button',{name:'确认移除工作目录',exact:true}).click()
    await removal().getByRole('alert').filter({hasText:'未提交、未跟踪或被忽略'}).waitFor()
    await removal().getByRole('button',{name:'取消',exact:true}).click()
    git(checkout,'add','.');git(checkout,'commit','-m','Preserve my work')
    const head=git(checkout,'rev-parse','HEAD')
    const other=await browser.newPage({viewport:{width:1100,height:700}});setup(other);await open(other)
    await row().getByRole('button',{name:'审阅移除',exact:true}).click()
    await removal().getByRole('button',{name:'确认移除工作目录',exact:true}).click()
    await panel().getByRole('button',{name:'恢复工作树',exact:true}).waitFor()
    await assert.rejects(fs.stat(checkout))
    assert.equal(git(folder,'rev-parse','refs/scholarflow/worktrees/'+worktree.id),head)
    await other.getByLabel('项目工作树',{exact:true}).getByRole('button',{name:'已移除',exact:true}).click()
    await other.getByRole('button',{name:'恢复工作树',exact:true}).waitFor()
    await other.close()
    // A saved creation draft and removed checkout survive a new process/port.
    await open()
    await panel().getByRole('button',{name:'创建工作树',exact:true}).click()
    await panel().getByLabel('工作树名称',{exact:true}).fill('下一轮工作树')
    await until(async()=>(await api(`/desktop-state/projects/${project.id}/worktrees`)).value.name==='下一轮工作树','draft persisted')
    const oldBase=base;await page.close();stop();await start();assert.notEqual(base,oldBase)
    page=await browser.newPage({viewport:{width:1100,height:700}});setup(page);await open()
    await panel().getByRole('button',{name:'创建工作树',exact:true}).click()
    assert.equal(await panel().getByLabel('工作树名称',{exact:true}).inputValue(),'下一轮工作树')
    await panel().getByRole('button',{name:'取消',exact:true}).click()
    await panel().getByRole('button',{name:'已移除',exact:true}).click()
    await panel().getByRole('button',{name:'恢复工作树',exact:true}).click()
    await panel().getByRole('button',{name:'在此新建会话',exact:true}).waitFor()
    assert.equal(git(checkout,'rev-parse','HEAD'),head)
    assert.equal((await fs.readFile(path.join(checkout,'source.txt'),'utf8')).replaceAll('\r\n','\n'),'worktree changes\n')
    assert.equal((await api('/conversations/'+worktreeCid+'/project')).project.available,true)
    assert.equal((await listing()).worktrees.length,1)
    for(const[width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(200)
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      await page.screenshot({path:path.join(root,`verification/worktree-list-${size}.png`),animations:'disabled'})
    }
    assert.deepEqual(errors,[])
    console.log('PASS: real Git worktree creation/isolation, lost-response deduplication, new chat binding, dirty/stale removal refusal, saved HEAD, two-window updates, draft/new-port restart and restore; 1440x900/1100x700')
  }catch(e){if(page&&!page.isClosed())await page.screenshot({path:path.join(root,'verification/worktrees-failure.png')});throw e}
  finally{await browser.close();stop();await fs.writeFile(path.join(data,'test-server.log'),log)}
}
main().catch(e=>{console.error(e);process.exitCode=1})
