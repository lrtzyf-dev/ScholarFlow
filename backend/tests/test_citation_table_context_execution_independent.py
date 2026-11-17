"""Independent executor interruption boundaries, explicit synthetic replies."""
from copy import deepcopy
import json
import re

import pytest

from app import run_lifecycle
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from tests.test_citation_answer_rewrite_execution_review import saved, cleanup
from tests.test_citation_repair_review import _current_checkpoint
from tests.test_citation_table_context_independent_current import independent_word, REAL_CLAIM
from tests.test_research_children import parent


class IndependentInterrupted(BaseException):
    pass


class IndependentTableReviewer:
    mode = 'api'

    def __init__(self, *, interrupt_quality=False):
        self.interrupt_quality = interrupt_quality
        self.audits, self.rewrites, self.quality = [], [], []
        self._runtime_config = {'mode': 'api', 'model_id': 'independent-d04-synthetic',
            'base_url': 'https://independent-d04.example.test/v1'}

    def chat(self, messages, **kwargs):
        system, payload = messages[0]['content'], messages[-1]['content']
        if '语义修订后最终答案的独立质量核验员' in system:
            self.quality.append(deepcopy(messages))
            if self.interrupt_quality:
                raise IndependentInterrupted('Independent loss after durable quality intent')
            return LLMResponse('{"quality_issues":[]}')
        if system == '你是严谨的引用审计员，只输出 JSON。':
            self.audits.append(deepcopy(messages))
            segments = re.findall(r'\[论断 (\d+)\] ([^\r\n]+)(.*?)(?=\n\[论断 |\Z)', payload, re.S)
            assert segments, payload
            return LLMResponse(json.dumps({'results': [{'index': int(index),
                'supported': all(f'引用证据 {eid}（' in context for eid in ('e2', 'e3', 'e4', 'e5')),
                'reason': 'Synthetic exact per-claim header-context check; no scientific certification.',
                'support_kind': 'source', 'process_scope': 'none', 'supports': []}
                for index, _, context in segments]}))
        self.rewrites.append(deepcopy(messages))
        assert len(self.rewrites) == 1, 'One semantic rewrite per user revision'
        return LLMResponse(REAL_CLAIM)


def prepare_independent_executor(db, monkeypatch, *, interrupt_quality=False):
    cid, evidence = independent_word(db)
    question = '按已有固定表格说明随访计数，保持其他用户要求。'
    binding = parent(db, cid=cid, question=question)
    model = IndependentTableReviewer(interrupt_quality=interrupt_quality)
    monkeypatch.setattr(loop, 'get_llm', lambda: model)
    monkeypatch.setattr(loop.settings, 'memory_capture', False)
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **kw: {'ran': 0, 'blocked': False})
    state = AgentState(question=question, conversation_id=cid, phase='finalize', answer=REAL_CLAIM,
        evidence=evidence, evidence_counter=5, runtime_config={**binding, 'mode': 'api'},
        source_revision_ids=binding.get('source_revision_ids', []), goal_id=binding['goal_id'],
        attempt_id=binding['attempt_id'], writer_token=binding['writer_token'],
        messages=[{'role': 'user', 'content': question}])
    checkpoint = _current_checkpoint(db, state)
    run_lifecycle.save_checkpoint(db, binding['run_id'], checkpoint)
    db.commit()
    return cid, binding, checkpoint, model, question


def executor(db, cid, binding, checkpoint, question, persist):
    return list(loop.run_agent(db, question, conversation_id=cid, run_id=binding['run_id'],
        checkpoint=checkpoint, save_checkpoint=persist))


def test_table_annotation_does_not_bypass_unknown_quality_after_protocol_upgrade(db, monkeypatch):
    from app.agent import rewritten_answer_quality
    cid, binding, checkpoint, model, question = prepare_independent_executor(db, monkeypatch, interrupt_quality=True)

    def persist(value):
        run_lifecycle.save_checkpoint(db, binding['run_id'], value)
        db.commit()

    try:
        with pytest.raises(IndependentInterrupted, match='quality intent'):
            executor(db, cid, binding, checkpoint, question, persist)
        checkpoint = json.loads(json.dumps(saved(db, binding), ensure_ascii=False))
        assert '[^e5][^e2][^e3]' in checkpoint['answer']
        assert len(model.rewrites) == len(model.quality) == 1
        annotation = [row for key, row in checkpoint['source_attribution_reviews'].items()
            if key.startswith('citation_table_context/')]
        assert len(annotation) == 1 and annotation[0]['annotation_only']
        assert any(key.startswith('source_review/') and row.get('review_dispatch') == 'pending'
            for key, row in checkpoint['source_attribution_reviews'].items())
        original = rewritten_answer_quality.quality_only_instructions()
        monkeypatch.setattr(rewritten_answer_quality, 'quality_only_instructions',
            lambda: original + ' Independent policy identity changed after unknown dispatch.')
        monkeypatch.setattr(rewritten_answer_quality, 'PROTOCOL', rewritten_answer_quality.PROTOCOL + 1)
        model.interrupt_quality = False
        events = executor(db, cid, binding, checkpoint, question, persist)
        answer = next(value for kind, value in events if kind == 'answer')
        assert answer['audit']['status'] == 'passed'
        assert answer['refusal'] == 0 and answer['outcome'] == 'partial' and not answer['completed']
        assert answer['answer_quality']['status'] == 'unverified'
        assert any(value.get('audit', {}).get('error_code') == 'dispatch_outcome_unknown'
            for _, value in events)
        assert len(model.rewrites) == len(model.quality) == 1
        assert len([key for key in saved(db, binding)['source_attribution_reviews']
            if key.startswith('citation_table_context/')]) == 1
    finally:
        cleanup(binding)


def test_failed_annotation_checkpoint_recovers_without_second_rewrite(db, monkeypatch):
    cid, binding, checkpoint, model, question = prepare_independent_executor(db, monkeypatch)
    rejected = []

    def fail_context(value):
        if any(key.startswith('citation_table_context/') for key in value['source_attribution_reviews']) and not rejected:
            rejected.append(True)
            raise IndependentInterrupted('Independent failure before annotation checkpoint commit')
        run_lifecycle.save_checkpoint(db, binding['run_id'], value)
        db.commit()

    try:
        with pytest.raises(IndependentInterrupted, match='before annotation checkpoint'):
            executor(db, cid, binding, checkpoint, question, fail_context)
        checkpoint = json.loads(json.dumps(saved(db, binding), ensure_ascii=False))
        assert checkpoint['answer'] == REAL_CLAIM
        assert not any(key.startswith('citation_table_context/') for key in checkpoint['source_attribution_reviews'])
        assert len(model.rewrites) == 1 and not model.quality

        def persist(value):
            run_lifecycle.save_checkpoint(db, binding['run_id'], value)
            db.commit()

        events = executor(db, cid, binding, checkpoint, question, persist)
        answer = next(value for kind, value in events if kind == 'answer')
        assert answer['completed'] and answer['refusal'] == 0
        assert '[^e5][^e2][^e3]' in answer['answer']
        assert len(model.rewrites) == len(model.quality) == 1
        assert len([key for key in saved(db, binding)['source_attribution_reviews']
            if key.startswith('citation_table_context/')]) == 1
    finally:
        cleanup(binding)
