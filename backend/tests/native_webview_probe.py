"""Isolated real Edge WebView2 probe of launcher, instance session and native bridge.

No installed app/user directory is opened. The native folder picker is replaced
by a deterministic fixture result; its OS UI is intentionally not exercised.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'verification/local-instance/native-webview'


def redact(value):
    return re.sub(r'(sf-launch=|__sf_ticket=|ScholarFlow |Bearer )[A-Za-z0-9_-]{32,}', r'\1[redacted]', str(value))


def child(runtime, phase):
    runtime = Path(runtime).absolute()
    data = runtime / 'data'
    for key, value in {'DATA_DIR': data, 'APPDATA': runtime / 'appdata', 'LOCALAPPDATA': runtime / 'localappdata',
        'WEBVIEW2_USER_DATA_FOLDER': runtime / 'webview-profile', 'LLM_API_KEY': '', 'EMBEDDING_API_KEY': '',
        'LLM_DEMO_MODE': '1', 'REDIS_URL': '', 'DESKTOP_NOTIFICATIONS': 'false', 'PYTHONUTF8': '1'}.items():
        os.environ[key] = str(value)
    from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
    DotEnvSettingsSource._read_env_files = lambda self: {}
    sys.path.insert(0, str(ROOT / 'backend'))
    sys.path.insert(0, str(ROOT / 'desktop'))
    import launcher
    import webview

    class ForeignPage(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
            self.wfile.write(b'<!doctype html><title>Isolated foreign-origin fixture</title><p>Native bridge origin test</p>')
        def log_message(self, *args):
            pass

    foreign = ThreadingHTTPServer(('127.0.0.1', 0), ForeignPage)
    threading.Thread(target=foreign.serve_forever, daemon=True).start()
    checks, failures = [], []
    holder = {'picker_calls': 0}
    real_create, real_start = webview.create_window, webview.start
    project = runtime / 'project'; project.mkdir(exist_ok=True)

    def create(*args, **kwargs):
        kwargs.update(hidden=True, focus=False)
        window = real_create(*args, **kwargs)
        holder['window'], holder['api'] = window, kwargs['js_api']
        holder['origin'] = urlsplit(args[1] if len(args) > 1 else kwargs['url'])._replace(path='', query='', fragment='').geturl()
        def choose_fixture(*a, **k):
            holder['picker_calls'] += 1
            return [str(project)]
        window.create_file_dialog = choose_fixture
        return window

    def evaluate(script, timeout=25):
        done, result = threading.Event(), []
        def callback(value):
            result.append(value);done.set()
        # Resolving the promise through pywebview is itself part of this probe.
        holder['window'].evaluate_js('(async()=>{try{return {ok:true,value:await (async()=>{' + script + '})()}}catch(e){return {ok:false,error:String(e.message||e)}}})()', callback)
        if not done.wait(timeout):
            raise AssertionError('Native JavaScript callback timed out')
        if not result[0].get('ok'):
            raise AssertionError(redact(result[0].get('error')))
        return result[0].get('value')

    def until(script, timeout=50):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if evaluate(script, timeout=8):
                    return
            except Exception:
                pass
            time.sleep(.15)
        raise AssertionError('Native page readiness condition timed out')

    def check(value, label):
        if not value:
            raise AssertionError(label)
        checks.append(label)

    def exercise():
        window = holder['window']
        try:
            until("return !!document.querySelector('textarea.textarea') && !!window.pywebview?.api?.choose_project_folder")
            state = evaluate("const r=await fetch('/api/local-session/status');const s=await r.json();return {required:s.required,authenticated:s.authenticated,hash:location.hash,cookies:document.cookie,stored:!!sessionStorage.getItem('scholarflow.desktop-session.v1'),ua:navigator.userAgent}")
            check(state['required'] and state['authenticated'] and state['stored'], 'Real WebView2 production page authenticates through the launcher handoff')
            check(state['hash'] == '' and not state['cookies'], 'Launch fragment is removed and no ambient authentication cookie is present')
            holder['user_agent'] = state['ua']
            selected = evaluate('return await window.pywebview.api.choose_project_folder()')
            check(selected == str(project) and holder['picker_calls'] == 1, 'Actual pywebview bridge invokes DesktopApi on the trusted origin (picker result synthetic)')
            status = evaluate("return (await fetch('/api/settings')).status")
            check(status == 200, 'Actual WebView fetch reaches the authenticated settings API')
            status = evaluate("return await new Promise((resolve,reject)=>{const x=new XMLHttpRequest();x.open('GET','/api/settings');x.onload=()=>resolve(x.status);x.onerror=()=>reject(Error('XHR failed'));x.send()})")
            check(status == 200, 'Actual WebView XMLHttpRequest carries origin-scoped authentication')
            # First process persists a project; the restart process verifies it
            # through the actual public API before creating another terminal.
            if phase == 'first':
                item = evaluate("const r=await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:" + json.dumps(str(project)) + ",name:'Native WebView fixture'})});if(!r.ok)throw Error('Project admission failed '+r.status);return r.json()")
                project_id = item['id']
            else:
                items = evaluate("return (await fetch('/api/projects')).json()")
                item = next((r for r in items if r['name'] == 'Native WebView fixture'), None)
                check(item is not None, 'Second real launcher instance reopens persisted isolated project after clean shutdown')
                project_id = item['id']
            # Create a real scoped cmd PTY and exercise the actual WS admission
            # + path-bound one-use ticket + output acknowledgement protocol.
            ws_result = evaluate("""
const base='/api/projects/'+PID+'/terminals';
const response=await fetch(base,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:'native_fixture_'+PHASE,shell:'cmd'})});
if(!response.ok)throw Error('Terminal admission failed '+response.status);
const terminal=await response.json();const path=base+'/'+terminal.id;
try{
 const attach=await (await fetch(path+'/connect',{method:'POST'})).json();
 const ticket=await (await fetch('/api/local-session/ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:path+'/stream',kind:'websocket'})})).json();
 const address=location.origin.replace('http:','ws:')+path+'/stream?__sf_ticket='+ticket.ticket;
 const success=await new Promise((resolve,reject)=>{
  const socket=new WebSocket(address);let output='',sent=false;const timer=setTimeout(()=>{socket.close();reject(Error('Native PTY WS timed out'))},20000);
  socket.onopen=()=>socket.send(JSON.stringify({token:attach.token,after:0}));
  socket.onerror=()=>{clearTimeout(timer);reject(Error('Native WS failed'))};
  socket.onmessage=e=>{const msg=JSON.parse(e.data);
    if(msg.type==='ready'&&!sent){sent=true;socket.send(JSON.stringify({type:'input',data:'echo SCHOLARFLOW_NATIVE_WS_2026\\r'}))}
    if(msg.type==='output'){output+=msg.data;socket.send(JSON.stringify({type:'ack',sequence:msg.sequence,response:msg.data.includes('\\u001b[6n')?'\\u001b[1;1R':''}));
      if(output.includes('SCHOLARFLOW_NATIVE_WS_2026')){clearTimeout(timer);socket.close();resolve(true)}}
    if(msg.type==='error'){clearTimeout(timer);socket.close();reject(Error(msg.message))}
  }
 });
 const replayDenied=await new Promise(resolve=>{const s=new WebSocket(address);const timer=setTimeout(()=>{s.close();resolve(false)},5000);s.onopen=()=>{clearTimeout(timer);s.close();resolve(false)};s.onerror=()=>{clearTimeout(timer);resolve(true)}});
 return {success,replayDenied};
}finally{await fetch(path+'/stop',{method:'POST'})}
""".replace('PID', json.dumps(project_id)).replace('PHASE', json.dumps(phase)), timeout=35)
            check(ws_result['success'] and ws_result['replayDenied'], 'Real WebView WebSocket consumes one-use ticket, runs scoped PTY output and rejects ticket replay')
            ticket_result = evaluate("const t=await (await fetch('/api/local-session/ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target:'/api/release',kind:'download'})})).json();const a=await fetch(t.url);const b=await fetch(t.url);return {first:a.status,replay:b.status}")
            check(ticket_result == {'first': 200, 'replay': 401}, 'Real WebView resource ticket permits one exact GET and rejects replay')
            old_epoch = holder['api']._document_epoch
            window.evaluate_js('location.reload()')
            until("const r=await fetch('/api/local-session/status');const s=await r.json();return s.authenticated===true && !!document.querySelector('textarea.textarea') && location.hash===''")
            check(holder['api']._document_epoch > old_epoch, 'Native refresh advances bridge document epoch and keeps authenticated output available')
            window.load_url(f'http://127.0.0.1:{foreign.server_port}/')
            until("return document.title==='Isolated foreign-origin fixture' && !!window.pywebview?.api?.choose_project_folder")
            blocked = evaluate('return await window.pywebview.api.choose_project_folder()')
            check(blocked is None and holder['picker_calls'] == 1, 'Foreign-origin real WebView page cannot open DesktopApi picker')
            window.load_url(holder['origin'] + '/chat')
            until("const r=await fetch('/api/local-session/status');const s=await r.json();return s.authenticated===true && !!document.querySelector('textarea.textarea')")
            check(holder['api']._trusted_page(), 'Returning to the trusted WebView origin restores scoped API access')
            from app.local_session import instance_session
            holder['old_auth'] = {b'host': holder['origin'].removeprefix('http://'), b'authorization': 'ScholarFlow ' + instance_session._token}
        except BaseException as error:
            failures.append(redact(type(error).__name__ + ': ' + str(error)))
        finally:
            window.destroy()

    def start(*args, **kwargs):
        kwargs.update(gui='edgechromium', private_mode=True, storage_path=str(runtime / 'webview-profile'), debug=False)
        return real_start(exercise, **kwargs)

    webview.create_window, webview.start = create, start
    try:
        exit_code = launcher.main()
        from app.local_session import instance_session
        check(exit_code == 0 and not instance_session.active, 'Actual launcher shutdown invalidates the local instance session')
        if holder.get('old_auth'):
            check(not instance_session.authenticated(holder['old_auth']), 'Pre-close authorization is rejected after the actual launcher exits')
        check(holder['api']._closed and holder['api'].choose_project_folder() is None, 'Actual closing event invalidates the native bridge')
        from app.runtime_owner import RuntimeOwner
        replacement = RuntimeOwner(data)
        check(replacement.reserve_desktop(), 'A clean subsequent owner can acquire the isolated data-directory lock')
        replacement.finish_shutdown(idle=True)
    except BaseException as error:
        failures.append(redact(type(error).__name__ + ': ' + str(error)))
    finally:
        foreign.shutdown();foreign.server_close()
    result = {'phase': phase, 'passed': len(checks), 'checks': checks, 'failures': failures,
        'engine': 'Edge WebView2', 'user_agent': holder.get('user_agent'), 'native_hidden_window': True,
        'native_folder_picker_ui_tested': False, 'real_provider_calls': 0, 'installation_performed': False}
    (OUTPUT / (phase + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if failures else 0


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--child',choices=['first','restart']);parser.add_argument('--runtime',type=Path)
    args=parser.parse_args();OUTPUT.mkdir(parents=True,exist_ok=True)
    if args.child:
        return child(args.runtime,args.child)
    runtime_root=ROOT/'verification/runtime';runtime_root.mkdir(exist_ok=True)
    runtime=Path(tempfile.mkdtemp(prefix='native-webview-',dir=runtime_root))
    results=[]
    for phase in ('first','restart'):
        process=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--child',phase,'--runtime',str(runtime)],
            cwd=ROOT,env={**os.environ,'PYTHONUTF8':'1'},capture_output=True,timeout=180)
        (OUTPUT/(phase+'.log')).write_text(redact(process.stdout.decode('utf-8',errors='replace')+process.stderr.decode('utf-8',errors='replace')),encoding='utf-8')
        path=OUTPUT/(phase+'.json')
        if not path.is_file():
            raise RuntimeError('Native fixture failed before producing a report; inspect the redacted phase log')
        result=json.loads(path.read_text(encoding='utf-8'));results.append(result)
        if process.returncode or result['failures']:
            raise RuntimeError('Native fixture failed: '+str(result['failures']))
    files=['desktop/launcher.py','backend/app/local_session.py','backend/app/main.py','frontend/src/localSession.ts','frontend/src/main.ts','frontend/dist/release-build.json']
    report={'passed':sum(r['passed'] for r in results),'phases':results,'real_provider_calls':0,'installation_performed':False,
        'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files},
        'scope':'Actual hidden Edge WebView2 running current source launcher and production UI; synthetic project and picker return; no native installer or manual-user acceptance'}
    (OUTPUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'phases':len(results),'real_provider_calls':0}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
