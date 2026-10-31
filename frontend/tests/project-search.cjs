// Bundled real ripgrep, real HTTP/SQLite and desktop search/preview/reference flow.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn, execFileSync } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(r => setTimeout(r, ms))
async function until(fn, label) { const end = Date.now()+25000; while (!await fn()) { assert.ok(Date.now()<end,label); await pause(80) } }
async function main() {
  const data = await fs.mkdtemp(path.join(root,'verification/runtime/project-search-'))
  const folder = path.join(data,'source'), src=path.join(folder,'src');await fs.mkdir(src,{recursive:true})
  const source = Array.from({length:650},(_,i)=>`line ${i} context 中文 data`).join('\n')+'\n中文 😀 needleLocate <img src=x onerror=alert(1)>\nALPHA alpha alpha_extra\n'
  await fs.writeFile(path.join(src,'main.py'),source)
  await fs.writeFile(path.join(src,'wide.py'),'x'.repeat(2400)+' farAwayMatch '+'y'.repeat(1000))
  await fs.writeFile(path.join(folder,'many.txt'),Array.from({length:85},(_,i)=>`needle ${i}`).join('\n'))
  await fs.writeFile(path.join(folder,'.gitignore'),'ignored.txt\n')
  await fs.writeFile(path.join(folder,'ignored.txt'),'needleIgnored')
  await fs.writeFile(path.join(folder,'.hidden.txt'),'needleHidden')
  const env={...process.env,DATA_DIR:data,LLM_API_KEY:'',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'}
  const browser=await chromium.launch({headless:true}), errors=[]
  let child,base,log='',page,release=()=>{}
  async function start() {
    const socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r))
    const port=socket.address().port;await new Promise(r=>socket.close(r))
    child=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)],{cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']})
    child.stdout.on('data',s=>log+=s);child.stderr.on('data',s=>log+=s);base=`http://127.0.0.1:${port}`
    await until(async()=>{if(child.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server')
  }
  function stop(){if(child&&child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child=null}
  async function api(url,body,method='POST') {const response=await fetch(base+'/api'+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await response.json();assert.ok(response.ok,`${url}: ${JSON.stringify(result)}`);return result}
  async function open(cid) {
    page=await browser.newPage({viewport:{width:1440,height:900}});page.on('pageerror',e=>errors.push(e.message))
    await page.goto(base+'/chat?conv='+cid);await page.locator('.project-context-button').click()
    const panel=page.getByRole('dialog',{name:'项目文件',exact:true});await panel.getByRole('tab',{name:'搜索',exact:true}).click()
    await until(()=>panel.getByLabel('搜索项目内容',{exact:true}).isEnabled(),'search draft loaded')
    return panel
  }
  async function query(panel, text) {await panel.getByLabel('搜索项目内容',{exact:true}).fill(text);await panel.getByRole('button',{name:'搜索',exact:true}).click();await until(async()=>!await panel.getByRole('button',{name:'取消搜索',exact:true}).count(),'search complete')}
  async function screenshots(dialog, label) {
    for(const [width,height,size] of [[1440,900,'desktop'],[1100,700,'small-desktop']]) {
      await page.setViewportSize({width,height});await pause(100)
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
      assert.ok(await dialog.evaluate(el=>el.getBoundingClientRect().bottom<=innerHeight))
      await page.screenshot({path:path.join(root,`verification/project-search-${label}-${size}.png`),animations:'disabled'})
    }
  }
  try {
    await start()
    const project=await api('/projects',{path:folder}), cid=(await api('/conversations',{title:'Project search verification',project_id:project.id})).id
    let panel=await open(cid)
    await query(panel,'needleLocate')
    await panel.locator('.search-summary').filter({hasText:'1 个匹配行，1 个文件'}).waitFor()
    assert.equal(await panel.locator('.search-snippet mark').textContent(),'needleLocate')
    assert.equal(await panel.locator('.search-snippet img').count(),0)
    await screenshots(panel,'results')
    await page.route('**/api/projects/*/file**', async route=>{await pause(650);await route.continue()}, {times:1})
    await panel.getByRole('button',{name:'打开 src/main.py:651',exact:true}).click()
    let preview=page.getByRole('dialog',{name:'搜索文件预览',exact:true})
    await preview.locator('pre mark').filter({hasText:'needleLocate'}).waitFor()
    assert.ok(await preview.locator('pre').textContent().then(s=>s.includes('中文 😀 needleLocate')))
    assert.match(await preview.locator('.preview-pages>span').textContent(), new RegExp(`/ ${Array.from(source).length} 字符$`))
    const counters=(await preview.locator('.preview-pages>span').textContent()).match(/(\d+)–(\d+) \/ (\d+)/)
    assert.equal(Number(counters[2]),Number(counters[3]),'Unicode count agrees at end of file')
    assert.equal(await preview.locator('img').count(),0)
    await until(()=>preview.locator('pre mark').evaluate(el=>{const a=el.getBoundingClientRect(),b=el.closest('pre').getBoundingClientRect();return a.top>=b.top&&a.bottom<=b.bottom}), 'matched line is scrolled into view')
    await screenshots(preview,'preview')
    await preview.getByRole('button',{name:'引用文件',exact:true}).click()
    await preview.locator('.el-dialog__headerbtn').focus();await page.keyboard.press('Shift+Tab')
    assert.ok(await preview.evaluate(el=>el.contains(document.activeElement)))
    await page.keyboard.press('Escape');await preview.waitFor({state:'hidden'})
    assert.equal(await panel.getByLabel('引用搜索文件 src/main.py',{exact:true}).isChecked(),true)
    await query(panel,'farAwayMatch')
    await panel.getByRole('button',{name:'打开 src/wide.py:1',exact:true}).click()
    await until(()=>preview.locator('pre mark').evaluate(el=>{const a=el.getBoundingClientRect(),b=el.closest('pre').getBoundingClientRect();return a.left>=b.left&&a.right<=b.right}), 'long-line match is horizontally visible')
    await page.keyboard.press('Escape');await preview.waitFor({state:'hidden'})
    await query(panel,'needleLocate')
    // Real on-disk changes invalidate opening and pagination; no stale text is shown.
    await fs.writeFile(path.join(src,'main.py'),source+'external edit\n')
    await panel.getByRole('button',{name:'打开 src/main.py:651',exact:true}).click()
    await preview.getByRole('alert').filter({hasText:'文件内容已变化'}).waitFor()
    assert.equal(await preview.locator('pre').count(),0)
    await preview.getByRole('button',{name:'重新搜索',exact:true}).click();await preview.waitFor({state:'hidden'})
    await until(async()=>await panel.locator('.search-hit').count()===1,'refreshed hit')
    await query(panel,'needle ')
    assert.equal(await panel.locator('.search-hit').count(),40)
    await panel.getByRole('button',{name:'加载更多匹配',exact:true}).click()
    await until(async()=>await panel.locator('.search-hit').count()===80,'next result page')
    assert.equal(new Set(await panel.locator('.search-hit').evaluateAll(es=>es.map(e=>e.getAttribute('aria-label')))).size,80)
    await panel.getByRole('button',{name:'刷新搜索',exact:true}).click()
    await until(async()=>await panel.locator('.search-hit').count()===40,'new first page')
    await fs.appendFile(path.join(folder,'many.txt'),'\nneedle external')
    await panel.getByRole('button',{name:'加载更多匹配',exact:true}).click()
    await panel.getByRole('alert').filter({hasText:'搜索结果已变化'}).waitFor()
    assert.equal(await panel.locator('.search-hit').count(),40)
    // Failure is actionable and an older response cannot replace a newer query.
    await page.route('**/api/projects/*/search',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'搜索测试暂时不可用'})}))
    await query(panel,'missing')
    await panel.getByRole('alert').filter({hasText:'搜索测试暂时不可用'}).waitFor()
    await page.unroute('**/api/projects/*/search')
    await query(panel,'needleLocate')
    await panel.locator('.search-hit').waitFor()
    const gate=new Promise(r=>release=r);let captured=false
    await page.route('**/api/projects/*/search',async route=>{
      if(route.request().postDataJSON().query!=='obsolete')return route.continue()
      const response=await route.fetch();captured=true;await gate
      try{await route.fulfill({response})}catch{}
    })
    await panel.getByLabel('搜索项目内容',{exact:true}).fill('obsolete');await panel.getByRole('button',{name:'搜索',exact:true}).click()
    await until(()=>captured,'held old query')
    await query(panel,'needleLocate');release();await pause(150)
    assert.equal(await panel.locator('.search-hit').count(),1)
    await page.unroute('**/api/projects/*/search')
    // Engine options, invalid regex, include/exclude and explicit ignored files.
    await query(panel,'ALPHA')
    assert.equal(await panel.locator('.search-snippet mark').count(),3)
    await panel.getByRole('button',{name:'区分大小写',exact:true}).click();await query(panel,'ALPHA')
    assert.equal(await panel.locator('.search-snippet mark').count(),1)
    await panel.getByRole('button',{name:'区分大小写',exact:true}).click()
    await panel.getByRole('button',{name:'整词匹配',exact:true}).click();await query(panel,'alpha')
    assert.equal(await panel.locator('.search-snippet mark').count(),2)
    await panel.getByRole('button',{name:'整词匹配',exact:true}).click()
    await panel.getByRole('button',{name:'正则表达式',exact:true}).click();await query(panel,'[')
    await panel.getByRole('alert').filter({hasText:'regex parse error'}).waitFor()
    await panel.getByRole('button',{name:'正则表达式',exact:true}).click()
    await panel.getByRole('button',{name:'搜索范围',exact:true}).click()
    await query(panel,'needleIgnored');assert.equal(await panel.locator('.search-hit').count(),0)
    await panel.getByLabel('忽略规则中的文件',{exact:true}).check();await query(panel,'needleIgnored')
    assert.equal(await panel.locator('.search-hit').count(),1)
    await panel.getByLabel('忽略规则中的文件',{exact:true}).uncheck()
    // Shared CAS state cannot silently overwrite another window's search.
    await until(async()=> (await api(`/desktop-state/projects/${project.id}/search`)).value.query==='needleIgnored','search options saved')
    const saved=await api(`/desktop-state/projects/${project.id}/search`)
    await api(`/desktop-state/projects/${project.id}/search`,{revision:saved.revision,value:{...saved.value,query:'remote choice'}},'PUT')
    await panel.getByLabel('搜索项目内容',{exact:true}).fill('local choice')
    await panel.getByRole('alert').filter({hasText:'另一窗口已修改搜索条件'}).waitFor()
    await panel.getByRole('button',{name:'载入已保存版本',exact:true}).click()
    await until(async()=>await panel.getByLabel('搜索项目内容',{exact:true}).inputValue()==='remote choice','resolve search conflict')
    await panel.getByLabel('搜索目录',{exact:true}).fill('src')
    await panel.getByLabel('包含文件',{exact:true}).fill('**/*.py')
    await query(panel,'needleLocate')
    await until(async()=>{const s=await api(`/desktop-state/projects/${project.id}/search`);return s.value.path==='src'&&s.value.include==='**/*.py'&&s.value.query==='needleLocate'},'restart search state')
    await panel.getByRole('button',{name:'完成',exact:true}).click()
    await page.locator('.project-file-chip').filter({hasText:'src/main.py'}).waitFor()
    await page.close();stop();const oldBase=base;await start();assert.notEqual(base,oldBase)
    panel=await open(cid)
    assert.equal(await panel.getByLabel('搜索项目内容',{exact:true}).inputValue(),'needleLocate')
    assert.equal(await panel.getByLabel('搜索目录',{exact:true}).inputValue(),'src')
    assert.equal(await panel.getByLabel('包含文件',{exact:true}).inputValue(),'**/*.py')
    await panel.getByRole('button',{name:'搜索',exact:true}).click();await panel.locator('.search-hit').waitFor()
    assert.equal(await panel.locator('.search-hit').count(),1)
    assert.deepEqual(errors,[])
    console.log(JSON.stringify({passed:true,data,viewports:['1440x900','1100x700'],checks:'recursive content, line/Unicode preview, escaped HTML, references, stale file/page, paging, failed/retried/late requests, regex/case/whole word, scope, ignore option, draft CAS, fresh-port restart'}))
  } finally {release();await browser.close();stop()}
}
main().catch(error=>{console.error(error);process.exitCode=1})
