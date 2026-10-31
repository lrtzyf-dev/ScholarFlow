"""A model's tool name must be checked at execution, not just hidden in specs."""
import json

from app.agent import subagent
from app.llm import LLMResponse


def test_subagent_blocks_injected_write_and_unlisted_tools_before_dispatch(db, monkeypatch):
    names = ['git_project_write', 'run_project_command', 'import_paper_from_url', 'mcp__custom__write', 'invented_tool']
    dispatched = []
    monkeypatch.setattr(subagent, '_safe_tool_specs', lambda: [{'name': 'list_papers'}])
    monkeypatch.setattr(subagent, 'execute_tool', lambda db, name, args: dispatched.append(name) or '{"papers": []}')
    class Model:
        def chat(self, messages, tools=None, **kwargs):
            if tools is None:
                return LLMResponse('Only permitted reads completed.')
            if any(m['role'] == 'tool' for m in messages):
                values = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
                assert all(v['blocked_by_subagent_policy'] for v in values[:-1])
                assert values[-1] == {'papers': []}
                return LLMResponse('Done')
            return LLMResponse('', [{'id': str(i), 'name': name, 'arguments': {}} for i, name in enumerate([*names, 'list_papers'])])
    result = subagent.run_subagent(db, Model(), {'topic': 'Check read-only dispatch'})
    assert dispatched == ['list_papers']
    assert len(result['evidence']) == len(names) + 1
