"""Fresh-process synthetic continuation for test_native_stage_renewal only."""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import loop, tools as tools_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.llm import LLMResponse  # noqa: E402


def main():
    state = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    name = 'mcp__synthetic__write_once'
    arguments = {'path': 'result.txt', 'text': 'confirmed result'}
    requests, writes, checkpoints = [], [], []
    class Model:
        def stream_chat(self, messages, tools=None):
            requests.append(deepcopy(messages))
            yield 'final', (LLMResponse('', tool_calls=[{'id': 'fresh-process-id', 'name': name, 'arguments': arguments}])
                if len(requests) == 1 else LLMResponse('Preserved result.'))
    settings.approval_mode = 'full_access'
    loop.hooks_store.run_hooks = lambda *a, **k: {'ran': 0, 'blocked': False, 'feedback': ''}
    loop.get_tool_specs = lambda: [{'name': name, 'parameters': {'type': 'object'}}]
    loop.tool_risk = tools_module.tool_risk = lambda tool: 'write' if tool == name else 'safe'
    loop.get_llm = lambda: Model()
    loop.execute_tool = lambda *a, **k: writes.append('unsafe-repeat') or '{"status":"completed"}'
    with SessionLocal() as db:
        events = list(loop.run_agent(db, state['question'], checkpoint=state,
            save_checkpoint=lambda s: checkpoints.append(deepcopy(s))))
    result = {'ok': len(requests) == 2 and not writes and any(k == 'answer' for k, _ in events),
        'requests': len(requests), 'writes': writes, 'generation': checkpoints[-1]['memory_generation'],
        'phase': checkpoints[-1]['phase'], 'opaque_forwarded': any('native-private-signature-' in json.dumps(m) for m in requests),
        'duplicate_blocked': any(k == 'tool_result' and 'not_replayed' in value['preview'] for k, value in events),
        'context_stage_progress': checkpoints[-1]['context_stage_progress'], 'real_provider_calls': 0}
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if not result['ok'] or result['opaque_forwarded'] or not result['duplicate_blocked']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
