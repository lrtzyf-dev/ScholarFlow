// Real Vue, isolated local API, no providers and no installation operations.
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net')
const {spawn}=require('node:child_process'),{chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p}
async function until(fn){for(let i=0;i<600;i++){if(await fn())return;await pause(100)}throw Error('Startup timeout')}
async function main(){
  const output=path.join(root,'verification/release-engineering/ui');await fs.mkdir(output,{recursive:true})
  const isolated=await fs.mkdtemp(path.join(output,'isolated-')),env={...process.env,DATA_DIR:path.join(isolated,'data'),
    APPDATA:path.join(isolated,'appdata'),LOCALAPPDATA:path.join(isolated,'localappdata'),LLM_API_KEY:'',LLM_DEMO_MODE:'1',
    EMBEDDING_API_KEY:'',REDIS_URL:'',DESKTOP_NOTIFICATIONS:'false',PYTHONUTF8:'1'}
  let backend,vite,browser,log='';const errors=[],external=[],checks=[]
  try{
    const p=await port(),base=`http://127.0.0.1:${p}`
    const code=`import sys
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
DotEnvSettingsSource._read_env_files=lambda self:{}
import uvicorn
uvicorn.run('app.main:app',host='127.0.0.1',port=int(sys.argv[1]),log_level='warning')`
    backend=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['-c',code,String(p)],{cwd:path.join(root,'backend'),env,windowsHide:true})
    backend.stdout.on('data',c=>log+=c);backend.stderr.on('data',c=>log+=c)
    await until(async()=>{if(backend.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}})
    const {createServer}=await import('vite');vite=await createServer({root:path.join(root,'frontend'),configFile:path.join(root,'frontend/vite.config.ts'),logLevel:'error',
      server:{host:'127.0.0.1',port:await port(),strictPort:true,hmr:false,proxy:{'/api':{target:base,changeOrigin:false,ws:true}}}})
    await vite.listen();const ui=`http://127.0.0.1:${vite.httpServer.address().port}`
    browser=await chromium.launch({headless:true});const context=await browser.newContext({viewport:{width:1440,height:1000}})
    await context.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===ui||['data:','blob:'].includes(url.protocol))return route.continue();external.push(url.origin+url.pathname);return route.abort()})
    const page=await context.newPage();page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message))
    const live=await (await fetch(base+'/api/release')).json()
    assert.equal(live.version,'0.1.0');assert.equal(live.publisher_signature,'not_verified')
    await page.goto(ui+'/about');await page.getByText('界面与服务版本一致。',{exact:true}).waitFor()
    assert.match(await page.locator('.release-card').innerText(),/0\.1\.0 · 开发版 · 构建 1/)
    assert.match(await page.locator('.release-card').innerText(),/发布者签名尚未验证/)
    checks.push('Real local release API agrees with compile-time frontend version; source checkout never claims verified signature')
    await page.locator('.release-card').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,'release-about.png')})
    await page.route('**/api/release',route=>route.fulfill({json:{...live,version:'9.9.9'}}))
    await page.getByRole('button',{name:'重新检查版本'}).click()
    await page.getByRole('alert').filter({hasText:'界面与服务版本不一致'}).waitFor()
    checks.push('Version mismatch shows actionable alert and does not keep the old success state')
    await page.unroute('**/api/release')
    await page.getByRole('button',{name:'重新检查版本'}).click();await page.getByText('界面与服务版本一致。',{exact:true}).waitFor()
    await page.setViewportSize({width:900,height:850});await page.locator('.release-card').scrollIntoViewIfNeeded()
    assert(await page.locator('.release-card').evaluate(e=>e.scrollWidth<=e.clientWidth))
    await page.screenshot({path:path.join(output,'release-narrow.png')})
    checks.push('900px layout wraps build identity without horizontal overflow')
    await page.goto(ui+'/settings');await page.getByRole('button',{name:'合规说明',exact:true}).click()
    await page.locator('.release-card:visible').getByText('界面与服务版本一致。',{exact:true}).waitFor()
    checks.push('Settings compliance tab exposes the same release information')
    assert.deepEqual(errors,[]);assert.deepEqual(external,[])
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify({passed:checks.length,checks,browser_errors:errors,external_requests:external,
      real_provider_calls:0,native_installation_performed:false},null,2))
  }catch(error){await fs.writeFile(path.join(output,'failure.log'),String(error)+'\n'+log);throw error}
  finally{if(browser)await browser.close();if(vite)await vite.close();if(backend)backend.kill()}
}
main().catch(e=>{console.error(e);process.exitCode=1})
