"""Actual executor and durable SQLite; all review/rewriter outputs synthetic."""
from copy import deepcopy
import json
import re

import pytest

from app import cancel_store, run_lifecycle
from app.agent import loop, source_attribution
from app.agent.checkpoint import AgentState
from app.llm import LLMResponse
from tests.test_citation_answer_rewrite_execution_review import setup, saved, run, cleanup, CORRECTED
from tests.test_citation_local_cleanup_execution import CleanupModel, EXTRA
from tests.test_citation_repair_review import _current_checkpoint
from tests.test_citation_table_context_current import fixed_table, REAL_CLAIM
from tests.test_research_children import parent
from tests.test_rewritten_quality_execution import preserve_known_review


@pytest.mark.parametrize('quality', ['passed','invalid','failed'])
def test_sentence_cleanup_rechecks_original_goal_without_rebuying_rewrite(db, monkeypatch, quality):
    model = CleanupModel(quality=quality, rewrite_output=CORRECTED + EXTRA)
    monkeypatch.setattr(source_attribution, 'review_source_attribution', preserve_known_review)
    cid, binding, checkpoint = setup(db, monkeypatch, model)
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db, binding['run_id'], value)
            db.commit()
        answer = next(value for kind,value in run(db,cid,binding,checkpoint,persist) if kind=='answer')
        assert answer['refusal'] == 0 and answer['answer'] == CORRECTED
        assert answer['completed'] is (quality == 'passed')
        assert len(model.rewrites) == 1 and len(model.audits) >= 3
        rows = [v for k,v in saved(db,binding)['source_attribution_reviews'].items() if k.startswith('citation_local_cleanup/')]
        assert len(rows) == 1 and rows[0]['protocol'] == 'exact_rejected_sentence_cleanup_v1'
        if quality != 'passed':
            assert answer['outcome'] == 'partial'
    finally:
        cleanup(binding)


class TableModel:
    mode = 'api'
    def __init__(self, quality='passed'):
        self.rewrites, self.audits, self.quality = [], [], quality
        self.quality_reviews = []
        self._runtime_config = {'mode':'api','base_url':'https://d04.example.test/v1','model_id':'synthetic-d04'}
    def chat(self, messages, **kwargs):
        system, payload = messages[0]['content'], messages[-1]['content']
        if '语义修订后最终答案的独立质量核验员' in system:
            self.quality_reviews.append(deepcopy(messages))
            return LLMResponse('{"quality_issues":[]}' if self.quality=='passed' else 'invalid JSON')
        if system == '你是严谨的引用审计员，只输出 JSON。':
            self.audits.append(deepcopy(messages))
            pairs = re.findall(r'\[论断 (\d+)\] ([^\r\n]+)',payload)
            supported = '引用证据 e2（' in payload and '引用证据 e3（' in payload
            return LLMResponse(json.dumps({'results':[{'index':int(i),'supported':supported,
                'reason':'Synthetic per-claim table context check','support_kind':'source',
                'process_scope':'none','supports':[]} for i,_ in pairs]}))
        self.rewrites.append(deepcopy(messages))
        assert len(self.rewrites)==1, 'must not buy second semantic rewrite'
        # Keeping the already cited wording exercises already_applied adoption,
        # as well as the normal changed-answer path.
        return LLMResponse(REAL_CLAIM)


def table_setup(db, monkeypatch, quality='passed'):
    cid, evidence = fixed_table(db)
    question = '说明表格随访人数'
    binding = parent(db,cid=cid,question=question)
    model = TableModel(quality)
    monkeypatch.setattr(loop,'get_llm',lambda:model)
    monkeypatch.setattr(loop.settings,'memory_capture',False)
    monkeypatch.setattr(loop.hooks_store,'run_hooks',lambda *a,**k:{'ran':0,'blocked':False})
    state = AgentState(question=question,conversation_id=cid,phase='finalize',answer=REAL_CLAIM,
        evidence=evidence,evidence_counter=5,runtime_config={**binding,'mode':'api'},
        source_revision_ids=binding.get('source_revision_ids',[]),goal_id=binding['goal_id'],
        attempt_id=binding['attempt_id'],writer_token=binding['writer_token'],
        messages=[{'role':'user','content':question}])
    checkpoint = _current_checkpoint(db,state)
    run_lifecycle.save_checkpoint(db,binding['run_id'],checkpoint)
    db.commit()
    return cid,binding,checkpoint,model,question


@pytest.mark.parametrize('quality',['passed','invalid'])
def test_table_context_precedes_full_audit_and_does_not_skip_goal_quality(db,monkeypatch,quality):
    cid,binding,checkpoint,model,question=table_setup(db,monkeypatch,quality)
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db,binding['run_id'],value)
            db.commit()
        events=list(loop.run_agent(db,question,conversation_id=cid,run_id=binding['run_id'],checkpoint=checkpoint,save_checkpoint=persist))
        answer=next(v for k,v in events if k=='answer')
        assert answer['refusal']==0 and '[^e5][^e2][^e3]' in answer['answer']
        assert answer['audit']['status']=='passed' and len(model.audits)==2
        assert len(model.rewrites)==len(model.quality_reviews)==1
        assert answer['completed'] is (quality=='passed')
        assert any(k.startswith('citation_table_context/') for k in saved(db,binding)['source_attribution_reviews'])
    finally:
        cleanup(binding)


def test_cancel_after_table_context_checkpoint_restores_without_new_rewrite(db,monkeypatch):
    cid,binding,checkpoint,model,question=table_setup(db,monkeypatch)
    cancelled=[]
    try:
        def persist(value):
            run_lifecycle.save_checkpoint(db,binding['run_id'],value)
            db.commit()
            if any(k.startswith('citation_table_context/') for k in value['source_attribution_reviews']) and not cancelled:
                cancelled.append(True)
                cancel_store.request_cancel(binding['run_id'])
        events=list(loop.run_agent(db,question,conversation_id=cid,run_id=binding['run_id'],checkpoint=checkpoint,save_checkpoint=persist))
        assert any(k=='cancelled' for k,_ in events)
        checkpoint=saved(db,binding)
        assert '[^e5][^e2][^e3]' in checkpoint['answer'] and not model.quality_reviews
        cancel_store.clear(binding['run_id'])
        resumed=list(loop.run_agent(db,question,conversation_id=cid,run_id=binding['run_id'],checkpoint=checkpoint,save_checkpoint=persist))
        answer=next(v for k,v in resumed if k=='answer')
        assert answer['completed'] and len(model.rewrites)==len(model.quality_reviews)==1
    finally:
        cleanup(binding)
