"""Isolated browser-test server: real I/O/SSE/SQLite, deterministic model only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent import loop
from app.llm import LLMResponse
from app.main import app
import uvicorn

class Editor:
    def stream_chat(self, messages, tools=None):
        if not any(m['role'] == 'tool' for m in messages):
            question = next((m['content'] for m in reversed(messages) if m['role']=='user'), '')
            name = 'background.py' if 'background' in str(question) else 'agent.py'
            yield 'final', LLMResponse('', [{'id':'file-edit','name':'edit_project_file','arguments':{
                'path':name,'operation':'create','content':'print("agent file")\n','summary':'Create a project script'}}])
        else:
            yield 'final', LLMResponse('文件操作已处理，可在变更记录中查看结果。')

loop.get_llm = Editor
loop.make_plan = lambda *a: []
loop.settings.memory_recall = False
loop.settings.memory_capture = False
loop.settings.approval_mode = 'read_only'
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]))
