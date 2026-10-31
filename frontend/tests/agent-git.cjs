// Real Windows Git repositories; deterministic model, otherwise real agent/HTTP/SQLite/SSE.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn,execFileSync}=require('node:child_process')
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(fn,label){const end=Date.now()+30000;while(!await fn()){assert.ok(Date.now()<end,label);await pause(100)}}
async function main(){
  const data=await fs.mkdtemp(path.join(root,'verification/runtime/agent-git-')),folder=path.join(data,'项目 中文')
  await fs.mkdir(folder)
  const git=(...args)=>execFileSync('git',['-C',folder,...args],{encoding:'utf8',windowsHide:true}).trim()
  git('init','-b','main');git('config','user.name','Desktop Test');git('config','user.email','test@example.invalid');git('config','commit.gpgsign','false')
  const file=path.join(folder,'源文件.txt');await fs.writeFile(file,'original\n');git('add','.');git('commit','-m','Initial')
  await fs.writeFile(file,'修改内容 中文 😀 <img src=x onerror=alert(1)>\n')
  let child,base,log='',page
  async function start(){
    const server=net.createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r))
    child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),[path.join(root,'backend/tests/agent_git_server.py'),String(port)],{cwd:path.join(root,'backend'),env:{...process.env,DATA_DIR:data,LLM_API_KEY:'',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'},windowsHide:true,stdio:['ignore','pipe','pipe']})
    child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s);base=`http://127.0.0.1:${port}`
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server ready')
  }
  function stop(){if(child&&child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child=null}
  async function api(url,body){const r=await fetch(base+'/api'+url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();assert.ok(r.ok,JSON.stringify(v));return v}
  const browser=await chromium.launch({headless:true}),errors=[]
  function setup(p){p.on('pageerror',e=>errors.push(e.message))}
  try{
    await start()
    const project=await api('/projects',{path:folder}),cid=(await api('/conversations',{title:'模型 Git 桌面审阅',project_id:project.id})).id
    const history=()=>api(`/projects/${project.id}/git/operations`)
    page=await browser.newPage({viewport:{width:1440,height:900}});setup(page)
    const review=()=>page.getByRole('dialog',{name:/Git .*审阅/})
    await page.goto(base+'/chat?conv='+cid)
    await page.locator('textarea.textarea').fill('GIT_STAGE')
    await page.getByRole('button',{name:'发送',exact:true}).click()
    await page.locator('.approval-card').getByRole('button',{name:'查看 Git 审阅',exact:true}).click()
    await review().getByLabel('Git 操作差异',{exact:true}).filter({hasText:'+修改内容'}).waitFor()
    assert.equal(git('diff','--cached'),'')
    assert.equal(await review().getByRole('button',{name:'确认暂存',exact:true}).count(),0)
    assert.equal(await review().locator('img').count(),0)
    for(const[width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(180)
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      const b=await review().locator('.el-dialog').boundingBox();assert.ok(b.x>=0&&b.y>=0&&b.x+b.width<=width&&b.y+b.height<=height,JSON.stringify({width,height,b}))
      const c=await review().getByRole('button',{name:'关闭',exact:true}).boundingBox();assert.ok(c&&c.y+c.height<=height)
      await page.screenshot({path:path.join(root,`verification/agent-git-review-${size}.png`),animations:'disabled'})
    }
    await review().getByRole('button',{name:'关闭',exact:true}).click()
    await page.locator('.approval-card').getByRole('button',{name:'批准一次',exact:true}).click()
    await until(async()=>(await api('/conversations/'+cid)).messages.length===1,'foreground stage completed')
    assert.ok(git('diff','--cached').includes('+修改内容'))
    await page.reload();await page.locator('.turn-git-operations button').click()
    await review().getByText('已完成',{exact:true}).waitFor();await review().getByRole('button',{name:'关闭',exact:true}).click()
    // Denial is persistent and cannot be confirmed from the manual project UI.
    const denyCid=(await api('/conversations',{title:'拒绝取消暂存',project_id:project.id})).id
    const deny=await api('/tasks/start',{question:'GIT_UNSTAGE',conversation_id:denyCid,project_id:project.id})
    await page.goto(base+'/tasks')
    await page.locator('.task-approval').getByRole('button',{name:'查看 Git 审阅',exact:true}).click()
    await review().getByText('待确认',{exact:true}).waitFor();await review().getByRole('button',{name:'关闭',exact:true}).click()
    await page.locator('.task-approval').getByRole('button',{name:'拒绝',exact:true}).click()
    await until(async()=>(await api('/tasks/'+deny.id)).status==='done','background denial completed')
    assert.ok(git('diff','--cached').includes('+修改内容'))
    assert.ok((await history()).operations.some(row=>row.action==='unstage'&&row.status==='cancelled'))
    // Background commit is approved in another browser context; the open review refreshes.
    const commitCid=(await api('/conversations',{title:'后台提交审阅',project_id:project.id})).id
    const commit=await api('/tasks/start',{question:'GIT_COMMIT',conversation_id:commitCid,project_id:project.id})
    await page.locator('.task-approval').getByRole('button',{name:'查看 Git 审阅',exact:true}).click()
    await review().getByText('提交中文 😀 <img src=x onerror=alert(1)>',{exact:true}).waitFor()
    assert.equal(git('rev-list','--count','HEAD'),'1')
    const other=await browser.newPage();setup(other);await other.goto(base+'/tasks')
    await other.locator('.task-approval').getByRole('button',{name:'允许一次',exact:true}).click()
    await review().getByText('已完成',{exact:true}).waitFor()
    assert.equal(git('rev-list','--count','HEAD'),'2');assert.equal(await review().locator('img').count(),0)
    await other.close();await review().getByRole('button',{name:'关闭',exact:true}).click()
    await until(async()=>(await api('/tasks/'+commit.id)).status==='done','commit completed')
    // A changed worktree invalidates the reviewed stage operation.
    await fs.writeFile(file,'before review\n')
    const conflictCid=(await api('/conversations',{title:'变动冲突',project_id:project.id})).id
    const conflict=await api('/tasks/start',{question:'GIT_STAGE',conversation_id:conflictCid,project_id:project.id})
    await until(async()=>(await api('/tasks/'+conflict.id)).approvals?.length,'conflict approval')
    await fs.writeFile(file,'external edit\n')
    await page.locator('.task-approval').getByRole('button',{name:'允许一次',exact:true}).click()
    await until(async()=>(await api('/tasks/'+conflict.id)).status==='done','conflict recorded')
    assert.equal((await history()).operations[0].status,'conflict');assert.equal(git('diff','--cached'),'')
    // Restart while a review waits: old permission is cancelled; completed history persists.
    const waitingCid=(await api('/conversations',{title:'重启待审批',project_id:project.id})).id
    const waiting=await api('/tasks/start',{question:'GIT_STAGE',conversation_id:waitingCid,project_id:project.id})
    await until(async()=>(await api('/tasks/'+waiting.id)).approvals?.length,'restart approval')
    const waitingId=(await history()).operations[0].id,oldBase=base
    await page.close();stop();await start();assert.notEqual(base,oldBase)
    assert.equal((await api(`/projects/${project.id}/git/operations/${waitingId}`)).status,'cancelled')
    assert.equal(git('rev-list','--count','HEAD'),'2');assert.equal(git('diff','--cached'),'')
    page=await browser.newPage({viewport:{width:1100,height:700}});setup(page)
    await page.goto(base+'/chat?conv='+commitCid);await page.locator('.turn-git-operations button').click()
    await review().getByText('已完成',{exact:true}).waitFor()
    assert.deepEqual(errors,[])
    console.log('PASS: agent Git desktop approval/review, real index/commit, deny, conflict, escaped Unicode, two windows, 1440x900/1100x700 and different-port restart without replay')
  }catch(e){if(page&&!page.isClosed())await page.screenshot({path:path.join(root,'verification/agent-git-failure.png')});throw e}
  finally{await browser.close();stop();await fs.writeFile(path.join(data,'test-server.log'),log)}
}
main().catch(e=>{console.error(e);process.exitCode=1})
