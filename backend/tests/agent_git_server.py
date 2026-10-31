"""Desktop fixture: real agent/SQLite/HTTP/Git, deterministic model tool requests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent import loop
from app.llm import LLMResponse
from app.main import app
import uvicorn


class GitModel:
    def stream_chat(self, messages, tools=None):
        if any(m['role'] == 'tool' for m in messages):
            yield 'final', LLMResponse('Git 操作结果已保存，请打开操作记录核对。')
            return
        question = str(next(m['content'] for m in reversed(messages) if m['role'] == 'user'))
        action = 'commit' if 'GIT_COMMIT' in question else 'unstage' if 'GIT_UNSTAGE' in question else 'stage'
        args = {'action': action, **({'message': '提交中文 😀 <img src=x onerror=alert(1)>'} if action == 'commit' else {'paths': ['源文件.txt']})}
        yield 'final', LLMResponse('', [{'id': 'git-fixture', 'name': 'git_project_write', 'arguments': args}])


loop.get_llm = GitModel
loop.make_plan = lambda *a: []
loop.settings.memory_recall = False
loop.settings.memory_capture = False
loop.settings.approval_mode = 'read_only'
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]))
