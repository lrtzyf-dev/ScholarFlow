// Actual Vue + isolated database and project. No personal data or model calls.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const {spawn} = require('node:child_process')
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function main() {
  const output = path.join(root, 'verification/artifact-delivery/publication-cleanup')
  await fs.mkdir(output, {recursive:true})
  const temporary = await fs.mkdtemp(path.join(require('node:os').tmpdir(), 'sf-publish-cleanup-'))
  const projectRoot = path.join(temporary,'project'); await fs.mkdir(projectRoot)
  const reserved = net.createServer()
  await new Promise(resolve => reserved.listen(0,'127.0.0.1',resolve))
  const port = reserved.address().port; await new Promise(resolve=>reserved.close(resolve))
  const base = `http://127.0.0.1:${port}`
  const server = spawn(path.join(root,'backend/.venv/Scripts/python.exe'),
    ['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)], {
    cwd:path.join(root,'backend'), windowsHide:true, stdio:['ignore','pipe','pipe'],
    env:{...process.env,DATA_DIR:path.join(temporary,'data'),APPDATA:path.join(temporary,'appdata'),LOCALAPPDATA:path.join(temporary,'localappdata'),
      LLM_API_KEY:'',LLM_DEMO_MODE:'1',EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false'},
  })
  let log='', vite, page
  server.stdout.on('data',part=>{log+=part}); server.stderr.on('data',part=>{log+=part})
  const browser = await chromium.launch({headless:true})
  const context = await browser.newContext({viewport:{width:1360,height:1000}})
  const errors=[], external=[], checks=[]
  context.on('page',p=>p.on('pageerror',err=>errors.push(err.message)))
  await context.route('**/*',route=>{
    const url=route.request().url()
    if(url.startsWith('http')&&!/^http:\/\/127\.0\.0\.1:/.test(url)){external.push(url);return route.abort()}
    return route.continue()
  })
  async function api(route,body,method='POST'){
    const response=await fetch(base+'/api'+route, body===undefined?undefined:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    assert.equal(response.status,200,await response.clone().text()); return response.json()
  }
  let result={status:'failed',checks,provider_requests:0}
  try{
    const deadline=Date.now()+30000
    while(Date.now()<deadline){try{if((await fetch(base+'/api/health')).ok)break}catch{} if(server.exitCode!==null)throw Error(log);await pause(100)}
    const {createServer}=await import('vite')
    vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',
      server:{host:'127.0.0.1',port:0,hmr:false,proxy:{'/api':{target:base,changeOrigin:false}}}})
    await vite.listen(); const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    const project=await api('/projects',{path:projectRoot})
    const conv=await api('/conversations',{title:'清理目标会话',project_id:project.id})
    const keep=await api('/conversations',{title:'保留的其他会话'})
    const item=await api('/artifacts',{conversation_id:conv.id,request_id:'publication-ui123',title:'固定科研成果',content:'# 结果\n待发布正文。\n'})
    page=await context.newPage()
    await page.goto(`${ui}/artifacts/${item.id}?conv=${conv.id}`)
    await page.getByLabel('项目内文件路径',{exact:true}).fill('reports/result.md')
    await page.getByRole('button',{name:'生成项目写入差异',exact:true}).click()
    const dialog=page.getByRole('dialog',{name:'文件差异',exact:true})
    await dialog.getByRole('button',{name:'应用修改',exact:true}).waitFor()
    await assert.rejects(fs.access(path.join(projectRoot,'reports/result.md')))
    await dialog.getByRole('button',{name:'应用修改',exact:true}).click()
    await dialog.getByRole('button',{name:'生成撤销差异',exact:true}).waitFor()
    assert.match(await fs.readFile(path.join(projectRoot,'reports/result.md'),'utf8'),/待发布正文/)
    checks.push('fixed revision publication requires actual diff acceptance before writing')
    await dialog.getByRole('button',{name:'生成撤销差异',exact:true}).click()
    await dialog.getByRole('button',{name:'应用修改',exact:true}).click()
    await dialog.getByRole('button',{name:'生成撤销差异',exact:true}).waitFor()
    await assert.rejects(fs.access(path.join(projectRoot,'reports/result.md')))
    checks.push('same project journal creates and applies exact undo')
    await page.screenshot({path:path.join(output,'publication-undo.png'),fullPage:true})
    await dialog.getByRole('button',{name:'关闭',exact:true}).click()
    await page.evaluate(({id,cid})=>localStorage.setItem(`scholarflow:artifact-draft:${cid}:${id}`,JSON.stringify({content:'未提交草稿',base:'old'})),{id:item.id,cid:conv.id})
    const form=new FormData(); form.append('conversation_id',conv.id);form.append('request_id','cleanup-upload-ui');form.append('file',new Blob(['会话私有原文'],{type:'text/markdown'}),'private.md')
    const uploaded=await fetch(base+'/api/sources/upload',{method:'POST',body:form});assert.equal(uploaded.status,200,await uploaded.clone().text())
    const source=(await uploaded.json()).revision_id
    await page.goto(`${ui}/chat?conv=${conv.id}&history=1`)
    const drawer=page.getByRole('dialog',{name:'历史会话',exact:true})
    const row=drawer.locator('.hist-item').filter({hasText:'清理目标会话'})
    await row.getByRole('button',{name:'删除',exact:true}).click()
    const confirm=page.getByRole('dialog',{name:'删除会话及关联资料',exact:true})
    await confirm.getByRole('button',{name:'保留会话',exact:true}).click()
    assert.equal((await api(`/artifacts/${item.id}?conversation_id=${conv.id}`,undefined)).id,item.id)
    checks.push('cancel destructive conversation confirmation preserves owned records')
    await row.getByRole('button',{name:'删除',exact:true}).click()
    await confirm.getByRole('button',{name:'永久删除',exact:true}).click()
    await page.getByText('已删除会话',{exact:true}).waitFor()
    assert.equal((await fetch(base+`/api/artifacts/${item.id}?conversation_id=${conv.id}`)).status,410)
    assert.equal((await fetch(base+`/api/sources/revisions/${source}?conversation_id=${conv.id}`)).status,410)
    assert.equal((await fetch(base+`/api/conversations/${keep.id}`)).status,200)
    assert.equal(await page.evaluate(({id,cid})=>localStorage.getItem(`scholarflow:artifact-draft:${cid}:${id}`),{id:item.id,cid:conv.id}),null)
    checks.push('confirmed deletion removes fixed attachments, documents and local drafts, preserving other chat and project journals')
    await page.screenshot({path:path.join(output,'conversation-deleted.png'),fullPage:true})
    assert.deepEqual(errors,[]); assert.deepEqual(external,[])
    result={status:'passed',checks,provider_requests:0,browser_errors:errors,external_requests:external}
  }catch(error){result.error=error.stack;await page?.screenshot({path:path.join(output,'failure.png'),fullPage:true});throw error}
  finally{await fs.writeFile(path.join(output,'report.json'),JSON.stringify(result,null,2));await browser.close();await vite?.close();server.kill();await fs.writeFile(path.join(output,'backend.log'),log)}
  console.log(JSON.stringify(result))
}
main().catch(error=>{console.error(error);process.exitCode=1})
