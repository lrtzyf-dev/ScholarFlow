// Actual Vue + isolated backend. No provider calls or user document access.
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const net = require('node:net')
const { spawn } = require('node:child_process')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '../..')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(fn, label) { const end = Date.now() + 45000; while (!(await fn())) { assert.ok(Date.now() < end, label); await pause(100) } }
async function port() { const server = net.createServer(); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); const value = server.address().port; await new Promise(resolve => server.close(resolve)); return value }
async function main() {
  const output = path.join(root, 'verification/complex-tables/ui')
  await fs.mkdir(output, {recursive: true})
  const isolated = await fs.mkdtemp(path.join(output, 'isolated-'))
  const python = path.join(root, 'backend/.venv/Scripts/python.exe')
  const env = {...process.env, DATA_DIR: path.join(isolated, 'data'), DB_NAME: 'tables.db', APPDATA: path.join(isolated,'appdata'), LOCALAPPDATA: path.join(isolated,'localappdata'), LLM_API_KEY:'', EMBEDDING_API_KEY:'', LLM_DEMO_MODE:'1', REDIS_URL:'', DESKTOP_NOTIFICATIONS:'false', PYTHONUTF8:'1'}
  await new Promise((resolve,reject) => {
    const process = spawn(python, ['-c', "from pathlib import Path\nimport sys\nfrom tests.complex_table_fixture import workbook_bytes\np=Path(sys.argv[1]);(p/'original.xlsx').write_bytes(workbook_bytes());(p/'original.csv').write_bytes('标题;值\\n中文;NA\\n'.encode('gb18030'))", isolated], {cwd:path.join(root,'backend'),env,windowsHide:true})
    let error=''; process.stderr.on('data',v=>error+=v);process.on('error',reject);process.on('exit',code=>code===0?resolve():reject(Error(error)))
  })
  let backend, vite, browser, page, backendLog=''
  const errors=[], external=[], checks=[]
  try {
    const backendPort=await port(), base=`http://127.0.0.1:${backendPort}`
    const start=async()=>{ backend=spawn(python,['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(backendPort)],{cwd:path.join(root,'backend'),env,windowsHide:true,stdio:['ignore','pipe','pipe']});backend.stdout.on('data',v=>backendLog+=v);backend.stderr.on('data',v=>backendLog+=v);await until(async()=>{if(backend.exitCode!==null)throw Error(backendLog);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'backend startup') }
    const stop=async()=>{const process=backend;if(!process||process.exitCode!==null)return;await new Promise(resolve=>{process.once('exit',resolve);process.kill()});backend=undefined}
    await start()
    const api=async(url,body)=>{const response=await fetch(base+'/api'+url,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined});assert.equal(response.status,200,`${url}: ${await response.clone().text()}`);return response.json()}
    const cid=(await api('/conversations',{title:'Table semantic verification'})).id
    const upload=async(ext)=>{const form=new FormData();form.set('file',new Blob([await fs.readFile(path.join(isolated,'original.'+ext))]),'original.'+ext);form.set('conversation_id',cid);form.set('request_id','table-ui-upload-'+ext);const response=await fetch(base+'/api/sources/upload',{method:'POST',body:form});assert.equal(response.status,200,await response.clone().text());return response.json()}
    const csv=await upload('csv'), excel=await upload('xlsx')
    const doc=await api(`/sources/revisions/${excel.revision_id}/document?conversation_id=${cid}`)
    const locator=await api(`/sources/revisions/${excel.revision_id}/locators`,{conversation_id:cid,kind:'sheet_cells',sheet:'实验 数据',cell_range:'B2:I3'})
    const {createServer}=await import('vite')
    vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',server:{host:'127.0.0.1',port:await port(),strictPort:true,hmr:false,proxy:{'/api':{target:base,changeOrigin:false,ws:true}}}})
    await vite.listen();const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    browser=await chromium.launch({headless:true})
    const context=await browser.newContext({viewport:{width:1400,height:1000}})
    await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===ui||['data:','blob:'].includes(url.protocol))return route.continue();external.push(url.origin+url.pathname);return route.abort()})
    page=await context.newPage();page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message))
    await page.goto(`${ui}/sources/${csv.revision_id}?conversation=${cid}`)
    await page.locator('.table-details > summary').click()
    await page.getByLabel('CSV 编码',{exact:true}).selectOption('gb18030')
    await page.getByLabel('CSV 分隔符',{exact:true}).selectOption('semicolon')
    await page.getByRole('button',{name:'预览此原件',exact:true}).click()
    await page.locator('.table-preview td').filter({hasText:'中文'}).waitFor()
    await page.getByRole('button',{name:'用此选项保存新解析版本',exact:true}).click()
    await until(async()=>!page.url().includes(csv.revision_id),'new immutable CSV interpretation navigation')
    await page.locator('.source-cells').filter({hasText:'中文'}).waitFor()
    const newId=new URL(page.url()).pathname.split('/').at(-1)
    const reinterpreted=await api(`/sources/revisions/${newId}/document?conversation_id=${cid}`)
    assert.equal(reinterpreted.table_metadata.encoding,'gb18030');assert.equal(reinterpreted.table_metadata.delimiter,'semicolon')
    assert.equal(reinterpreted.blocks[1].cells[1].missing,false)
    const originalResponse=await fetch(base+`/api/sources/revisions/${csv.revision_id}/file?conversation_id=${cid}`)
    assert.deepEqual(Buffer.from(await originalResponse.arrayBuffer()),await fs.readFile(path.join(isolated,'original.csv')))
    await page.screenshot({path:path.join(output,'csv-explicit-interpretation.png'),fullPage:true})
    checks.push({name:'retained_GB18030_CSV_preview_explicit_choice_new_revision_old_original_preserved',passed:true})
    await page.goto(`${ui}/sources/${excel.revision_id}?conversation=${cid}&locator=${locator.locator_id}`)
    await page.locator('.source-document-block[data-source-highlight="true"]').first().waitFor()
    assert.ok((await page.locator('.source-document-scroll').innerText()).includes('缓存：999（未验证，未复算）'))
    assert.ok((await page.locator('.source-document-scroll').innerText()).includes('百分比格式 0.0%'))
    assert.ok((await page.locator('.source-document-scroll').innerText()).includes('合并 H2:I3'))
    await page.locator('.table-details > summary').click()
    await page.getByLabel('公式范围（最多 10000 格）',{exact:true}).fill('E2:G3')
    await page.getByRole('button',{name:'实际计算并保存记录',exact:true}).click()
    const result=page.getByRole('region',{name:'表格复算结果',exact:true})
    await result.getByText('已计算 4 / 6 个公式，未解决 2 个。',{exact:true}).waitFor()
    await result.getByText('与缓存不一致',{exact:true}).waitFor()
    assert.ok((await result.innerText()).includes('依赖单元格为空，未填零'))
    assert.ok((await result.innerText()).includes('暂不支持此函数：INDIRECT'))
    const downloadEvent=page.waitForEvent('download')
    await result.getByRole('link',{name:'下载原件、核查结果与可复算脚本',exact:true}).click()
    const downloaded=await downloadEvent;await downloaded.saveAs(path.join(output,'actual-ui-reproduction.zip'))
    await page.screenshot({path:path.join(output,'formula-cache-recomputation.png'),fullPage:true})
    checks.push({name:'actual_Vue_formula_cache_mismatch_partial_results_and_reproduction_download',passed:true})
    const saved=await api(`/sources/revisions/${excel.revision_id}/table-computations?conversation_id=${cid}`)
    assert.equal(saved.items.length,1);assert.equal(saved.items[0].result.computed_count,4)
    await stop();await start()
    await page.reload();await page.locator('.table-details > summary').click()
    await page.getByRole('region',{name:'表格复算结果',exact:true}).getByText('已计算 4 / 6 个公式，未解决 2 个。',{exact:true}).waitFor()
    const after=await api(`/sources/revisions/${excel.revision_id}/document?conversation_id=${cid}`)
    assert.equal(after.fingerprint,doc.fingerprint)
    checks.push({name:'actual_backend_restart_preserves_original_fingerprint_and_execution_receipt',passed:true})
    await page.setViewportSize({width:760,height:950})
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1))
    await page.screenshot({path:path.join(output,'narrow-table-verification.png'),fullPage:true})
    checks.push({name:'760px_table_controls_without_document_horizontal_overflow',passed:true})
    assert.deepEqual(errors,[]);assert.deepEqual(external,[])
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify({passed:true,checks,browser_errors:errors,external_requests:external,real_provider_calls:0,synthetic_model_calls:0,scope:'Synthetic golden workbooks/CSV, real Vue HTTP and restart. This is not native Excel or full Excel formula support.'},null,2))
  }catch(error){if(page)await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});await fs.writeFile(path.join(output,'failure.json'),JSON.stringify({error:String(error),backendLog,errors,external},null,2));throw error}
  finally{await browser?.close();await vite?.close();backend?.kill()}
}
main().catch(error=>{console.error(error);process.exitCode=1})
