const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path'),net=require('node:net'),os=require('node:os')
const {spawn}=require('node:child_process'),{chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright')
const root=path.resolve(__dirname,'../..'),pause=ms=>new Promise(r=>setTimeout(r,ms))
async function until(test,label){const end=Date.now()+60000;while(!await test()){assert.ok(Date.now()<end,label);await pause(50)}}
function stats(values){const sorted=[...values].sort((a,b)=>a-b);return {samples:values.length,min_ms:sorted[0],median_ms:sorted[Math.floor(sorted.length/2)],p95_ms:sorted[Math.ceil(sorted.length*.95)-1],max_ms:sorted.at(-1)}}
async function main(){
  const out=path.join(root,'verification/product-readiness/scale'),runtime=path.join(root,'verification/runtime');await fs.mkdir(out,{recursive:true});await fs.mkdir(runtime,{recursive:true})
  const report={status:'running',scope:'Synthetic metadata/graph/history projection, not full-document import, embedding retrieval, OCR, Windows native hosting or independent-user acceptance.',hardware:{platform:os.platform(),release:os.release(),cpu:os.cpus()[0]?.model,logical_cpus:os.cpus().length,total_memory_bytes:os.totalmem()},datasets:[],real_provider_calls:0,limitations:['API P95 has 20 samples per dataset. Browser navigation samples and scroll frames are listed separately.','Graph dataset size and loaded/rendered nodes are different quantities.','Browser heap after navigation and explicit GC is diagnostic only; it does not prove leak-free 7-day operation.']}
  const browser=await chromium.launch({headless:true,args:['--enable-precise-memory-info']})
  try{
    for(const [kind,size] of [['library',100],['library',500],['library',1000],['graph',100],['graph',500],['graph',2000],['history',100],['history',500],['history',2000]]){
      const data=await fs.mkdtemp(path.join(runtime,'product-scale-')),socket=net.createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r))
      const base=`http://127.0.0.1:${port}`,server=spawn(path.join(root,'backend/.venv/Scripts/python.exe'),['tests/product_scale_ui_server.py',String(port)],{cwd:path.join(root,'backend'),windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,DATA_DIR:data,LLM_API_KEY:'',LLM_DEMO_MODE:'0',EMBEDDING_API_KEY:'',REDIS_URL:''}})
      let log='';server.stdout.on('data',c=>log+=c);server.stderr.on('data',c=>log+=c)
      const page=await browser.newPage({viewport:{width:1400,height:1000}}),errors=[],requests=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))requests.push(u.pathname+u.search)})
      const api=async(route,body)=>{const r=await fetch(base+route,body===undefined?undefined:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert.equal(r.status,200,await r.clone().text());return r.json()}
      try{
        await until(async()=>{if(server.exitCode!==null)throw Error(log);try{return(await fetch(base+'/api/health')).ok}catch{return false}},'server ready')
        const dataset=await api('/__test__/seed-scale',{kind,size}),route=kind==='library'?'/api/papers/page?q=Projection&limit=40':kind==='graph'?'/api/graph?limit=100':'/api/conversations/scale-history?limit=30'
        const timings=[];let responseBytes=0
        for(let n=0;n<20;n++){const start=performance.now(),result=await api(route);timings.push(performance.now()-start);responseBytes=Buffer.byteLength(JSON.stringify(result))}
        const uiRoute=kind==='library'?'/library':kind==='graph'?'/graph':'/chat?conv=scale-history',selector=kind==='library'?'.library-table tbody tr':kind==='graph'?'.network-node':'.turn[data-record-id]',expected=kind==='library'?40:kind==='graph'?Math.min(100,size):30
        const navigation=[]
        for(let n=0;n<3;n++){const start=performance.now();await page.goto(base+uiRoute);await until(async()=>await page.locator(selector).count()===expected,'bounded view rendered');navigation.push(performance.now()-start)}
        const cdp=await page.context().newCDPSession(page);await cdp.send('HeapProfiler.enable');const heapBefore=(await cdp.send('Runtime.getHeapUsage')).usedSize
        const frames=await page.evaluate(async kind=>{const el=kind==='history'?document.querySelector('.chat-scroll'):document.querySelector('.page')||document.scrollingElement;const intervals=[];let previous=performance.now();for(let i=0;i<90;i++){await new Promise(r=>requestAnimationFrame(r));const now=performance.now();intervals.push(now-previous);previous=now;if(el)el.scrollTop=(el.scrollHeight-el.clientHeight)*(i%30)/29}return intervals},kind)
        const interactions=[]
        if(kind==='library'){
          await page.getByRole('button',{name:'下一页文献'}).click();await until(async()=>(await page.getByRole('navigation',{name:'文献分页'}).innerText()).includes('第 41–80'),'second page')
          await page.getByLabel('搜索文献',{exact:true}).fill('00042');await until(async()=>await page.locator(selector).count()===1,'server search')
          assert.ok(!requests.some(r=>r==='/api/papers'||r.startsWith('/api/graph')),'library avoids full-list and graph requests');interactions.push('page and literal server search')
        }else if(kind==='graph'&&size>100){
          await page.getByRole('button',{name:/继续加载/}).click();await until(async()=>await page.locator(selector).count()===200,'second graph page')
          const routePattern='**/api/graph?*';await page.route(routePattern,async r=>{await pause(600);if(!page.isClosed())await r.continue().catch(()=>{})})
          await page.getByRole('button',{name:'全部节点',exact:true}).click();await page.getByRole('button',{name:'取消等待图谱'}).click();await page.getByText(/已取消等待图谱/).waitFor();await page.unroute(routePattern)
          assert.ok(!requests.includes('/api/papers'),'graph does not load all papers');interactions.push('append 100 nodes','cancel view wait')
        }else if(kind==='history'){
          await page.getByRole('button',{name:'查看更早历史'}).click();await until(async()=>await page.locator(`[data-record-id="scale-qa-${String(size-60).padStart(5,'0')}"]`).count()===1,'earlier window')
          await page.getByRole('button',{name:'查看最新历史'}).click();await until(async()=>await page.locator(`[data-record-id="scale-qa-${String(size-1).padStart(5,'0')}"]`).count()===1,'latest window')
          await page.goto(base+'/chat?conv=scale-history&record=scale-qa-00025');await until(async()=>await page.locator('[data-record-id="scale-qa-00025"]').count()===1,'exact old record anchor')
          assert.ok(requests.filter(r=>/^\/api\/conversations\/scale-history(?:\?|$)/.test(r)).every(r=>r.includes('limit=30')),'history network windows are bounded');interactions.push('earlier and latest pages','exact persistent old record anchor')
        }
        await page.screenshot({path:path.join(out,`${kind}-${size}.png`),fullPage:true})
        await page.goto(base+'/settings');await page.getByRole('heading',{name:'设置',exact:true}).waitFor();await cdp.send('HeapProfiler.collectGarbage');const heapAfter=(await cdp.send('Runtime.getHeapUsage')).usedSize
        assert.deepEqual(errors,[]);assert.equal((await api('/__test__/provider-calls')).length,0)
        report.datasets.push({...dataset,api:stats(timings),response_bytes:responseBytes,browser_navigation:stats(navigation),rendered_initial:expected,scroll_frame_intervals:stats(frames),heap_bytes:{before_navigation:heapBefore,after_navigation_and_gc:heapAfter},interactions,errors,data_directory:data})
        await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(`PASS ${kind} ${size}; API P95 ${stats(timings).p95_ms.toFixed(1)} ms; rendered ${expected}`)
      }catch(e){await page.screenshot({path:path.join(out,`${kind}-${size}-failure.png`),fullPage:true}).catch(()=>{});await fs.writeFile(path.join(out,'failure.json'),JSON.stringify({kind,size,error:String(e),log,errors,text:await page.locator('body').innerText()},null,2));throw e}
      finally{await page.close();server.kill();await new Promise(r=>server.exitCode!==null?r():server.once('exit',r))}
    }
    report.status='passed_with_explicit_scope';await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2))
  }finally{await browser.close()}
}
main().catch(e=>{console.error(e);process.exitCode=1})
