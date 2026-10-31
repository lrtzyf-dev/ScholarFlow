// Real Windows Git, desktop browser, persistent archive journal and restart.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn,execFileSync}=require('node:child_process')
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(fn,label){const end=Date.now()+30000;while(!await fn()){assert.ok(Date.now()<end,label);await pause(100)}}
async function main(){
  const data=await fs.mkdtemp(path.join(root,'verification/runtime/worktree-archive-')),folder=path.join(data,'归档来源 中文')
  await fs.mkdir(folder)
  const git=(cwd,...args)=>execFileSync('git',['-C',cwd,...args],{encoding:'utf8',windowsHide:true}).trim()
  git(folder,'init','-b','main');git(folder,'config','user.name','Archive Test');git(folder,'config','user.email','archive@example.invalid');git(folder,'config','commit.gpgsign','false')
  await fs.writeFile(path.join(folder,'source.txt'),'original\n');git(folder,'add','.');git(folder,'commit','-m','Initial')
  let child,base,log='',page
  const env={...process.env,DATA_DIR:data,LLM_API_KEY:'',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'}
  async function start(){
    const server=net.createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r))
    child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']})
    child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s);base=`http://127.0.0.1:${port}`
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server ready')
  }
  function stop(){if(child&&child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child=null}
  async function api(url,body){const r=await fetch(base+'/api'+url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const v=await r.json();assert.ok(r.ok,JSON.stringify(v));return v}
  const browser=await chromium.launch({headless:true}),errors=[]
  const setup=p=>p.on('pageerror',e=>errors.push(e.message))
  try{
    await start()
    const project=await api('/projects',{path:folder}),cid=(await api('/conversations',{title:'归档桌面验收',project_id:project.id})).id
    const target=`/projects/${project.id}/worktrees`
    const wt=await api(target,{request_id:'archive_desktop',name:'归档 中文 😀 <img src=x>',ref:'HEAD',branch:'feature/archive'})
    const checkout=wt.path,original=git(checkout,'rev-parse','HEAD')
    await fs.writeFile(path.join(checkout,'source.txt'),'staged version\n');git(checkout,'add','source.txt')
    const bytes=Buffer.from([0x00,0xff,0x0d,0x0a,0x41])
    await fs.writeFile(path.join(checkout,'source.txt'),bytes)
    await fs.writeFile(path.join(checkout,'new 中文.txt'),'new 😀\r\n')
    await fs.writeFile(path.join(checkout,'.gitignore'),'private/\n');await fs.mkdir(path.join(checkout,'private'))
    await fs.writeFile(path.join(checkout,'private/token.txt'),'kept outside Git')
    page=await browser.newPage({viewport:{width:1440,height:900}});setup(page)
    async function open(p=page){await p.goto(base+'/chat?conv='+cid);await p.locator('.project-context-button').click();await p.getByRole('dialog',{name:'项目文件',exact:true}).getByRole('tab',{name:'工作树',exact:true}).click()}
    const panel=()=>page.getByLabel('项目工作树',{exact:true}),review=()=>page.getByRole('dialog',{name:'归档工作树',exact:true})
    await open();await panel().getByRole('button',{name:'审阅归档',exact:true}).click()
    await review().waitFor();assert.ok((await review().innerText()).includes('1 个被忽略文件'))
    assert.equal(await review().locator('img').count(),0)
    for(const[width,height,size]of[[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(200)
      const box=await review().locator('.el-dialog').boundingBox();assert.ok(box.x>=0&&box.y>=0&&box.x+box.width<=width&&box.y+box.height<=height,JSON.stringify(box))
      const button=review().getByRole('button',{name:'保存快照并归档',exact:true})
      assert.ok(await button.evaluate(el=>{const b=el.getBoundingClientRect();return el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2))}))
      await page.screenshot({path:path.join(root,`verification/worktree-archive-review-${size}.png`),animations:'disabled'})
    }
    // A file changed after review must keep the original checkout and require a new review.
    await fs.writeFile(path.join(checkout,'late.txt'),'after review')
    await review().getByRole('button',{name:'保存快照并归档',exact:true}).click()
    await review().getByRole('alert').filter({hasText:'审阅后已变化'}).waitFor()
    assert.equal(git(checkout,'rev-parse','HEAD'),original)
    await review().getByRole('button',{name:'取消',exact:true}).click()
    const other=await browser.newPage({viewport:{width:1100,height:700}});setup(other);await open(other)
    await panel().getByRole('button',{name:'审阅归档',exact:true}).click()
    // Lost archive response: closing the old review and checking the journal reveals its result.
    let lost=false
    await page.route(`**/api${target}/${wt.id}/archive`,async route=>{if(!lost){lost=true;await route.fetch();await route.abort('failed')}else await route.continue()})
    await review().getByRole('button',{name:'保存快照并归档',exact:true}).click()
    await review().getByRole('alert').waitFor();await review().getByRole('button',{name:'取消',exact:true}).click()
    await panel().getByRole('button',{name:'已移除',exact:true}).click()
    await panel().getByRole('button',{name:'恢复工作树',exact:true}).waitFor()
    const archived=await api(target+'/'+wt.id);assert.equal(archived.status,'removed');assert.equal(archived.archive_info.ignored,1)
    await assert.rejects(fs.stat(checkout));assert.equal(git(folder,'rev-parse','feature/archive'),original)
    await other.getByLabel('项目工作树',{exact:true}).getByRole('button',{name:'已移除',exact:true}).click()
    await other.getByRole('button',{name:'恢复工作树',exact:true}).waitFor();await other.close()
    const download=page.waitForEvent('download');await panel().getByRole('link',{name:'下载本机备份',exact:true}).click()
    const downloaded=await download;await downloaded.saveAs(path.join(data,'downloaded-backup.zip'))
    assert.equal((await fs.readFile(path.join(data,'downloaded-backup.zip'))).subarray(0,2).toString(),'PK')
    const oldBase=base;await page.close();stop();await start();assert.notEqual(base,oldBase)
    page=await browser.newPage({viewport:{width:1100,height:700}});setup(page);await open()
    await panel().getByRole('button',{name:'已移除',exact:true}).click()
    await panel().getByRole('button',{name:'恢复工作树',exact:true}).click()
    await panel().getByRole('button',{name:'在此新建会话',exact:true}).waitFor()
    assert.deepEqual(await fs.readFile(path.join(checkout,'source.txt')),bytes)
    assert.equal(await fs.readFile(path.join(checkout,'new 中文.txt'),'utf8'),'new 😀\r\n')
    assert.equal(await fs.readFile(path.join(checkout,'private/token.txt'),'utf8'),'kept outside Git')
    assert.equal(await fs.readFile(path.join(checkout,'late.txt'),'utf8'),'after review')
    assert.equal(git(checkout,'branch','--show-current'),'')
    assert.equal(git(checkout,'show',archived.archive_info.index_head+':source.txt'),'staged version')
    assert.ok(!git(checkout,'ls-tree','-r','--name-only','HEAD').includes('private/token.txt'))
    assert.equal((await api(target)).worktrees.length,1);assert.deepEqual(errors,[])
    console.log('PASS: reviewed archive, stale-file rejection, staged/working/binary/Unicode/ignored preservation, unchanged source branch, lost-response journal recovery, two-window update, ZIP download and different-port restore; 1440x900/1100x700')
  }catch(e){if(page&&!page.isClosed())await page.screenshot({path:path.join(root,'verification/worktree-archive-failure.png')});throw e}
  finally{await browser.close();stop();await fs.writeFile(path.join(data,'test-server.log'),log)}
}
main().catch(e=>{console.error(e);process.exitCode=1})
