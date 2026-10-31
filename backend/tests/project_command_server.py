"""Isolated desktop verification: deterministic model, real commands/HTTP/SQLite."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent import loop
from app.llm import LLMResponse
from app.main import app
import uvicorn


class CommandModel:
    def stream_chat(self, messages, tools=None):
        if any(m['role'] == 'tool' for m in messages):
            yield 'final', LLMResponse('命令结果已保存，可打开命令记录核对输出与退出码。')
            return
        question = str(next((m['content'] for m in reversed(messages) if m['role'] == 'user'), ''))
        if 'WAIT_COMMAND' in question:
            command = "[Console]::WriteLine('RUNNING 中文 😀'); Start-Sleep 60; [IO.File]::WriteAllText((Join-Path $PWD 'never.txt'), 'bad')"
        elif 'LARGE_COMMAND' in question:
            command = "[Console]::WriteLine('HEAD 中文 😀'); 1..1400 | ForEach-Object { [Console]::WriteLine(('line {0} 中文 😀 <img src=x onerror=alert(1)>' -f $_)) }; [Console]::WriteLine('TAIL 中文 😀')"
        else:
            command = "[IO.File]::AppendAllText((Join-Path $PWD 'actual.txt'), '中文 😀'); [Console]::WriteLine('DONE 中文 😀 <img src=x onerror=alert(1)>')"
        yield 'final', LLMResponse('', [{'id': 'project-command', 'name': 'run_project_command', 'arguments': {
            'command': command, 'summary': '桌面命令验收', 'timeout_seconds': 120}}])


loop.get_llm = CommandModel
loop.make_plan = lambda *a: []
loop.settings.memory_recall = False
loop.settings.memory_capture = False
loop.settings.approval_mode = 'read_only'
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]))
