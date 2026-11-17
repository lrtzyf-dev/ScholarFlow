// Real Vue and isolated HTTP backend; official Zotero responses are synthetic.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn}=require('node:child_process'),{chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p}
async function until(fn){for(let i=0;i<600;i++){if(await fn())return;await pause(100)}throw Error('Startup timeout')}
async function main(){
  const output=path.join(root,'verification/bibliography/ui');await fs.mkdir(output,{recursive:true})
  const isolated=await fs.mkdtemp(path.join(output,'isolated-')),env={...process.env,DATA_DIR:path.join(isolated,'data'),
    APPDATA:path.join(isolated,'appdata'),LOCALAPPDATA:path.join(isolated,'localappdata'),LLM_API_KEY:'',LLM_DEMO_MODE:'1',
    EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false',PYTHONUTF8:'1'}
  const serverCode=`
import io,runpy,sys
from pytest import MonkeyPatch
from reportlab.pdfgen import canvas
from app import zotero_files,bibliography_network as net
mp=MonkeyPatch();module=runpy.run_path('tests/test_bibliography.py');state=module['zotero_fixture'](mp)
buffer=io.BytesIO();c=canvas.Canvas(buffer);c.drawString(50,750,'Synthetic PDF original for Zotero fixture');c.save()
mp.setattr(zotero_files,'download_pdf',lambda *a:buffer.getvalue())
def denied(*a,**k):raise RuntimeError('External network forbidden during UI test')
mp.setattr(net,'get_json',denied)
import uvicorn
uvicorn.run('app.main:app',host='127.0.0.1',port=int(sys.argv[1]),log_level='warning')
`
  let backend,vite,browser,page,log='';const errors=[],external=[],checks=[]
  try{
    const p=await port(),base=`http://127.0.0.1:${p}`
    backend=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-c',serverCode,String(p)],{cwd:path.join(root,'backend'),env,windowsHide:true})
    backend.stdout.on('data',c=>log+=c);backend.stderr.on('data',c=>log+=c)
    await until(async()=>{if(backend.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}})
    const {createServer}=await import('vite');vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',
      server:{host:'127.0.0.1',port:await port(),strictPort:true,hmr:false,proxy:{'/api':{target:base,changeOrigin:false,ws:true}}}})
    await vite.listen();const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:1000}})
    await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===ui||['data:','blob:'].includes(url.protocol))return route.continue();external.push(url.origin+url.pathname);return route.abort()})
    page=await context.newPage();page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message))
    await page.goto(ui+'/library');await page.getByRole('button',{name:'书目 / Zotero',exact:true}).click()
    await page.getByLabel('书目原始内容').fill('@article{one,title={UI imported reference},author={Researcher},year=2024,doi={10.1234/ui}}\n@article{bad,author={Missing title}}')
    const preview=page.waitForResponse(r=>r.url().endsWith('/bibliography/preview'))
    await page.getByRole('button',{name:'保存并预览书目',exact:true}).click();const res=await preview;assert.equal(res.status(),200,await res.text())
    const batch=await res.json();await page.getByText('条目缺少标题，不能创建未命名书目',{exact:true}).waitFor()
    const adopted=page.waitForResponse(r=>r.url().endsWith('/decision'))
    await page.getByRole('button',{name:'采纳书目',exact:true}).click();assert.equal((await adopted).status(),200)
    await page.locator('.library-table').getByText('仅书目 · 无全文',{exact:true}).waitFor()
    checks.push('BibTeX partial preview preserves invalid raw entry; valid item adopts as metadata-only')
    await page.reload();await page.getByRole('button',{name:'书目 / Zotero',exact:true}).click()
    await page.getByLabel('打开书目批次').selectOption(batch.id)
    await page.getByText('已采纳',{exact:true}).waitFor();checks.push('Bibliographic decisions survive refresh and reopening saved batch')
    await page.locator('.zotero-setup summary').click();await page.getByLabel('Zotero 用户 ID',{exact:true}).fill('123')
    await page.getByLabel('Zotero API 密钥',{exact:true}).fill('synthetic-zotero-test-key')
    await page.getByRole('button',{name:'读取资料集',exact:true}).click();await page.getByLabel('Zotero 资料集',{exact:true}).selectOption('COLL1234')
    await page.getByRole('button',{name:'保存连接',exact:true}).click();await page.locator('.zotero-binding').getByText('Selected collection',{exact:true}).waitFor()
    assert.equal(await page.getByLabel('Zotero API 密钥',{exact:true}).inputValue(),'')
    const synced=page.waitForResponse(r=>r.url().endsWith('/sync'))
    await page.getByRole('button',{name:'同步并预览',exact:true}).click();assert.equal((await synced).status(),200)
    await page.getByRole('button',{name:'采纳书目',exact:true}).click();await page.locator('.library-table').getByText('Zotero research',{exact:true}).waitFor()
    checks.push('Zotero collection selection, DPAPI connection, read-only sync and explicit parent adoption')
    await page.getByRole('button',{name:'保存此版 PDF',exact:true}).click();await page.getByRole('button',{name:'打开固定原件',exact:true}).waitFor()
    const fulltext=page.waitForResponse(r=>r.url().endsWith('/attach-fulltext'))
    await page.getByRole('button',{name:'附加全文到父书目',exact:true}).click();const attached=await fulltext;assert.equal(attached.status(),200,await attached.text())
    const adoptedPaper=(await attached.json()).paper_id
    await page.locator('.library-table').getByText('已解析',{exact:true}).waitFor()
    assert.equal(await page.locator('.library-table .paper-title').filter({hasText:'Zotero research'}).count(),1)
    checks.push('Selected PDF version saves to Source and attaches to same parent Paper without duplicate identity')
    await page.getByRole('button',{name:'查看已保存版本',exact:true}).click()
    await page.getByRole('button',{name:'原件 v5',exact:true}).waitFor()
    await page.screenshot({path:path.join(output,'bibliography.png'),fullPage:true})
    await page.getByRole('button',{name:'暂停连接',exact:true}).click();await page.getByRole('button',{name:'恢复连接',exact:true}).waitFor()
    await page.getByRole('button',{name:'恢复连接',exact:true}).click();await page.getByRole('button',{name:'暂停连接',exact:true}).waitFor()
    await page.getByRole('button',{name:'撤销本机连接',exact:true}).click();await page.getByText(/已撤销本机连接/).waitFor()
    assert.equal(await page.getByRole('button',{name:'同步并预览',exact:true}).isDisabled(),true)
    assert.equal(await page.locator('.library-table .paper-title').filter({hasText:'Zotero research'}).count(),1)
    checks.push('Pause/resume/revoke update connection while preserving adopted local papers')
    await page.getByRole('heading',{name:'书目与 Zotero',exact:true}).scrollIntoViewIfNeeded()
    await page.screenshot({path:path.join(output,'bibliography-top.png')})
    await page.setViewportSize({width:900,height:850})
    await page.screenshot({path:path.join(output,'bibliography-narrow.png')})
    // Verify the library remains independently usable when the import chat is
    // deliberately removed. The application must select a fresh live chat.
    const removed=await fetch(base+'/api/conversations/'+batch.conversation_id,{method:'DELETE'})
    assert.equal(removed.status,200,await removed.text())
    await page.reload();await page.locator('.library-table .paper-title').filter({hasText:'Zotero research'}).waitFor()
    const paperRow=page.locator('.library-table tbody tr').filter({hasText:'Zotero research'})
    await paperRow.getByRole('button',{name:'固定版本',exact:true}).click()
    const versionButton=paperRow.getByRole('button',{name:/固定版本 1 · 文献库主版本/})
    await versionButton.waitFor();await versionButton.click()
    await page.locator('canvas').first().waitFor()
    assert.ok(page.url().includes('/sources/'),'Independent fixed source opens after original conversation deletion')
    checks.push('Library fixed-version picker reopens saved PDF after original import conversation deletion')
    assert.deepEqual(errors,[]);assert.deepEqual(external,[])
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify({ok:true,verified_at:new Date().toISOString(),checks,browser_errors:errors,
      external_browser_requests:external,real_provider_calls:0,real_zotero_calls:0,data_isolated:true,
      limitations:['Zotero responses and PDF bytes are synthetic fixtures; no real Zotero credentials or live service verified.','No packaged Windows/WebView acceptance.']},null,2))
    console.log(JSON.stringify({ok:true,checks:checks.length,real_provider_calls:0,real_zotero_calls:0}))
  }catch(e){await page?.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});await fs.writeFile(path.join(output,'failure-report.json'),JSON.stringify({error:String(e.stack||e),checks,browser_errors:errors},null,2));throw e}
  finally{await browser?.close();await vite?.close();if(backend){backend.kill();await new Promise(r=>backend.exitCode!==null?r():backend.once('exit',r))}await fs.writeFile(path.join(output,'backend.log'),log)}
}
main().catch(e=>{console.error(e);process.exitCode=1})
