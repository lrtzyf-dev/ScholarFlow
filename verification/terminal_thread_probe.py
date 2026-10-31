"""Bounded diagnostic for terminal teardown and subsequent Python threads."""
import faulthandler
import json
import os
from pathlib import Path
import sys
import threading
import time

root = Path(__file__).resolve().parents[1]
os.chdir(root / 'backend')
sys.path.insert(0, str(root / 'backend'))
faulthandler.enable()


def inspect_threads():
    for _ in range(3):
        time.sleep(10)
        rows = []
        for ident, frame in sys._current_frames().items():
            while frame:
                if frame.f_code.co_name in ('start', '_bootstrap_inner', 'run'):
                    item = frame.f_locals.get('self')
                    if isinstance(item, threading.Thread):
                        rows.append({'frame': frame.f_code.co_name, 'ident': ident,
                                     'object': id(item), 'thread_ident': item.ident,
                                     'name': item.name, 'event': id(item._started),
                                     'started': item._started.is_set(),
                                     'waiters': [lock.locked() for lock in item._started._cond._waiters]})
                frame = frame.f_back
        with (root / 'verification' / 'terminal-thread-state.log').open('a', encoding='utf-8') as output:
            output.write('THREAD_STATE ' + json.dumps(rows) + '\n')
    faulthandler.dump_traceback()
    os._exit(124)


if __name__ == '__main__':
    threading.Thread(target=inspect_threads, daemon=True).start()
    import pytest
    sys.exit(pytest.main(['-vv', '--keep-duplicates', '-o', 'addopts=', '-k',
                         'test_http_origin_validation_and_lost_response_retry',
                         *(['tests/test_terminals.py'] * 20)]))
