// Real Git index/commits and durable desktop recovery, with no model calls.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(r => setTimeout(r, ms))
async function until(fn, name) { const end = Date.now()+25000; while (!await fn()) { assert.ok(Date.now()<end, name); await pause(80) } }
function git(cwd, ...args) { return execFileSync('git', args, { cwd, windowsHide:true, encoding:'utf8' }).trim() }
async function main() {
  const data = await fs.mkdtemp(path.join(root, 'verification/runtime/git-write-'))
  const folder = path.join(data, 'repository'); await fs.mkdir(folder)
  git(folder, 'init', '-b', 'main')
  for (const [key,value] of [['user.name','Verification'],['user.email','verification@example.invalid'],['commit.gpgsign','false']]) git(folder,'config',key,value)
  await fs.writeFile(path.join(folder, 'a.txt'), 'original\n')
  git(folder,'add','.'); git(folder,'commit','-m','Initial')
  await fs.writeFile(path.join(folder, 'a.txt'), 'stage this\n')
  const env = {...process.env, DATA_DIR:data, LLM_API_KEY:'', EMBEDDING_API_KEY:'', REDIS_URL:'', DESKTOP_NOTIFICATIONS:'false'}
  let child, base, log = '', page
  const browser = await chromium.launch({headless:true}), errors=[]
  async function start() {
    const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r))
    const port=socket.address().port;await new Promise(r=>socket.close(r))
    child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']})
    child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s);base=`http://127.0.0.1:${port}`
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server')
  }
  function stop(){if(child&&child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child=null}
  async function api(url,body,method='POST') {const response=await fetch(base+'/api'+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();assert.ok(response.ok,JSON.stringify(result));return result}
  async function open(cid, width=1440,height=900) {
    page=await browser.newPage({viewport:{width,height}});page.on('pageerror',e=>errors.push(e.message))
    await page.goto(base+'/chat?conv='+cid);await page.locator('.project-context-button').click()
    const panel=page.getByRole('dialog',{name:'项目文件',exact:true})
    await panel.getByRole('tab',{name:'版本控制',exact:true}).click()
    await panel.locator('.git-identity').filter({hasText:'main'}).waitFor()
    return panel
  }
  async function screenshots(review, name) {
    for(const [width,height,label] of [[1440,900,'desktop'],[1100,700,'small-desktop']]){
      await page.setViewportSize({width,height});await pause(100)
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      assert.ok(await review.evaluate(el=>el.getBoundingClientRect().bottom<=innerHeight))
      await page.screenshot({path:path.join(root,`verification/git-${name}-${label}.png`),animations:'disabled'})
    }
  }
  try {
    await start()
    const project=await api('/projects',{path:folder}), cid=(await api('/conversations',{title:'Git write verification',project_id:project.id})).id
    let panel=await open(cid)
    // Preparing never stages, and stale content is rejected at confirmation.
    await panel.getByRole('button',{name:'暂存 a.txt',exact:true}).click()
    let review=page.getByRole('dialog',{name:'Git 暂存审阅',exact:true})
    await review.getByText('+stage this',{exact:false}).waitFor()
    assert.equal(git(folder,'diff','--cached'),'')
    await screenshots(review,'stage-review')
    await fs.writeFile(path.join(folder,'a.txt'),'changed after review\n')
    await review.getByRole('button',{name:'确认暂存',exact:true}).click()
    await review.getByRole('alert').filter({hasText:'重新生成审阅'}).waitFor()
    assert.equal(git(folder,'diff','--cached'),'')
    await review.getByRole('button',{name:'关闭',exact:true}).click()
    // Lost prepare response keeps the same durable request id on recovery.
    let lostId
    await page.route('**/git/operations',async route=>{
      if(route.request().method()!=='POST')return route.continue()
      lostId=route.request().postDataJSON().request_id
      await route.fetch();await route.abort('failed')
    })
    await panel.getByRole('button',{name:'暂存 a.txt',exact:true}).click()
    await panel.getByRole('alert').filter({hasText:'Network Error'}).waitFor()
    await page.unroute('**/git/operations')
    await panel.getByRole('button',{name:'恢复审阅',exact:true}).click()
    review=page.getByRole('dialog',{name:'Git 暂存审阅',exact:true})
    await review.locator('.operation-patch').filter({hasText:'changed after review'}).waitFor()
    const rows=(await api(`/projects/${project.id}/git/operations`)).operations
    assert.equal(rows.length,2)
    assert.ok(lostId)
    // Lost apply response cannot cause a second write; refresh retrieves result.
    await page.route('**/git/operations/*/apply',async route=>{await route.fetch();await route.abort('failed')})
    await review.getByRole('button',{name:'确认暂存',exact:true}).click()
    await review.getByText('响应未确认，请先刷新操作状态。',{exact:true}).waitFor()
    assert.ok(await review.getByRole('button',{name:'确认暂存',exact:true}).isDisabled())
    await page.unroute('**/git/operations/*/apply')
    await review.getByRole('button',{name:'刷新 Git 操作',exact:true}).click()
    await review.locator('.operation-meta').filter({hasText:'已完成'}).waitFor()
    assert.ok(git(folder,'diff','--cached').includes('+changed after review'))
    await review.getByRole('button',{name:'关闭',exact:true}).click()
    await panel.getByRole('button',{name:'取消暂存 a.txt',exact:true}).click()
    review=page.getByRole('dialog',{name:'Git 取消暂存审阅',exact:true})
    await review.getByRole('button',{name:'确认取消暂存',exact:true}).click()
    await review.locator('.operation-meta').filter({hasText:'已完成'}).waitFor()
    assert.equal(git(folder,'diff','--cached'),'')
    assert.equal(await fs.readFile(path.join(folder,'a.txt'),'utf8'),'changed after review\n')
    await review.getByRole('button',{name:'关闭',exact:true}).click()
    await panel.getByRole('button',{name:'暂存全部更改',exact:true}).click()
    review=page.getByRole('dialog',{name:'Git 暂存审阅',exact:true})
    await review.getByRole('button',{name:'确认暂存',exact:true}).click()
    await review.locator('.operation-meta').filter({hasText:'已完成'}).waitFor()
    await review.getByRole('button',{name:'关闭',exact:true}).click()
    await panel.getByLabel('提交说明',{exact:true}).fill('桌面提交验证\n\nKeep working changes')
    await until(async()=> (await api(`/desktop-state/projects/${project.id}/git`)).value.message==='桌面提交验证\n\nKeep working changes','persisted message')
    // A fresh browser origin and port restore the commit draft and operation log.
    await page.close();stop();const oldBase=base;await start();assert.notEqual(base,oldBase)
    panel=await open(cid,1100,700)
    assert.equal(await panel.getByLabel('提交说明',{exact:true}).inputValue(),'桌面提交验证\n\nKeep working changes')
    await screenshots(panel,'write-panel')
    await panel.getByRole('button',{name:'审阅提交',exact:true}).click()
    review=page.getByRole('dialog',{name:'Git 提交审阅',exact:true})
    await review.getByText('Verification <verification@example.invalid>',{exact:true}).waitFor()
    await screenshots(review,'commit-review')
    await fs.writeFile(path.join(folder,'a.txt'),'working copy after review\n')
    await review.getByRole('button',{name:'确认提交',exact:true}).click()
    await review.locator('.operation-meta').filter({hasText:'已完成'}).waitFor()
    assert.equal(git(folder,'show','HEAD:a.txt'),'changed after review')
    assert.equal(git(folder,'rev-list','--count','HEAD'),'2')
    assert.equal(await fs.readFile(path.join(folder,'a.txt'),'utf8'),'working copy after review\n')
    await review.locator('.el-dialog__headerbtn').focus();await page.keyboard.press('Shift+Tab')
    assert.ok(await review.evaluate(el=>el.contains(document.activeElement)))
    await page.keyboard.press('Escape');await review.waitFor({state:'hidden'})
    assert.equal(await panel.getByLabel('提交说明',{exact:true}).inputValue(),'')
    await panel.getByRole('tab',{name:'操作记录',exact:true}).click()
    await panel.locator('.operation-row').filter({hasText:'桌面提交验证'}).click()
    review=page.getByRole('dialog',{name:'Git 提交审阅',exact:true})
    await review.locator('.operation-patch').filter({hasText:'changed after review'}).waitFor()
    assert.equal(await review.getByRole('button',{name:'确认提交',exact:true}).count(),0)
    assert.deepEqual(errors,[])
    console.log(JSON.stringify({passed:true,data,viewports:['1440x900','1100x700'],commits:2,checks:'stage/unstage/bulk/commit, stale review, lost prepare/apply, draft restart, operation history, focus/Escape, screenshots'}))
  } finally { await browser.close();stop() }
}
main().catch(error=>{console.error(error);process.exitCode=1})
