"""Disposable server with one controlled write blocked across shutdown/crash."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from app.agent import loop
from app.config import DATA_DIR
from app.llm import LLMResponse
from app.main import app


class Model:
    def stream_chat(self, messages, tools=None):
        if not any(m['role'] == 'tool' for m in messages):
            yield 'final', LLMResponse('', [dict(id='write', name='write_probe', arguments={})])
        else:
            yield 'final', LLMResponse('Controlled verification result; skipped operations remain unknown.')


def write_probe(*args, **kwargs):
    with (DATA_DIR / 'effects.log').open('a', encoding='utf-8') as target:
        target.write('write\n')
        target.flush()
    deadline = time.monotonic() + 45
    while not (DATA_DIR / 'release').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('probe was not released')
        time.sleep(0.02)
    return json.dumps({'written': True})


loop.get_llm = lambda: Model()
loop.make_plan = lambda *args: [dict(title='Write', phase='write', status='pending')]
loop.execute_tool = write_probe
loop.hooks_store.run_hooks = lambda *a, **k: dict(ran=0, blocked=False, feedback='')
loop.settings.approval_mode = 'full_access'
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(sys.argv[1]),
                                      log_level='warning', access_log=False, timeout_graceful_shutdown=1))


@app.post('/_verification/stop')
def stop():
    server.should_exit = True
    return {'stopping': True}


# The product's catch-all static mount must remain behind this test-only route.
app.router.routes.insert(0, app.router.routes.pop())
server.run()
if sys.argv[2] == 'linger':
    (DATA_DIR / 'service-stopped').write_text('stopped', encoding='ascii')
    while True:
        time.sleep(0.1)
