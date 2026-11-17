"""Single rewrite receives exact currently checked facts, never stale verdicts."""
from copy import deepcopy
import importlib
import json

import pytest

from app.rag.citation import parse_claims
from tests.test_citation_table_context_current import fixed_table, REAL_CLAIM


def module():
    return importlib.import_module('app.agent.citation_context_preservation')


def audit_for(answer, evidence, db, cid):
    claims = [{'text': c['claim'], 'evidence_ids': c['evidence_ids']}
        for c in parse_claims(answer, [e['id'] for e in evidence])]
    audit = {'passed': True, 'status': 'passed', 'verified': True, 'mode': 'llm',
        'claims': claims, 'factual_claims': len(claims), 'known_failed_count': 0,
        'unknown_count': 0, 'unverified_count': 0, 'unsupported': [], 'unverified': []}
    from app.agent.citation_scope import verified_source_metadata
    return module().bind_audit_preservation(answer, evidence,
        verified_source_metadata(db,evidence,cid),audit)


def test_exact_current_supported_claims_and_read_small_table_reach_feedback(db):
    cid, evidence = fixed_table(db)
    audit = audit_for(REAL_CLAIM, evidence, db, cid)
    value = module().build_preservation_feedback(db, REAL_CLAIM, evidence, audit, conversation_id=cid)
    assert value['available']
    assert value['supported_current_draft'] == audit['claims']
    assert [c['id'] for c in value['read_table_context'][0]['cells']] == ['e2','e3','e4','e5']
    assert value['rewrite_verdict'] == 'not_performed'
    assert module().preservation_feedback_matches(value, REAL_CLAIM, evidence, audit)
    assert '新的执行指令' in value['instructions']


@pytest.mark.parametrize('field,value', [('verified',False),('mode','lexical'),('unknown_count',1),
    ('unverified_count',1),('claims',[]),('factual_claims',True),('known_failed_count',True),
    ('error_code','dispatch_outcome_unknown')])
def test_unknown_stale_or_incomplete_audit_does_not_claim_supported_facts(db, field, value):
    cid, evidence = fixed_table(db)
    audit = audit_for(REAL_CLAIM, evidence, db, cid)
    audit[field] = value
    result = module().build_preservation_feedback(db, REAL_CLAIM, evidence, audit, conversation_id=cid)
    assert result['available'] is False
    assert not result.get('supported_current_draft')


def test_snapshot_rejects_changed_answer_evidence_verdict_or_payload(db):
    cid, evidence = fixed_table(db)
    audit = audit_for(REAL_CLAIM, evidence, db, cid)
    value = module().build_preservation_feedback(db, REAL_CLAIM, evidence, audit, conversation_id=cid)
    changed_evidence = deepcopy(evidence)
    changed_evidence[0]['quote'] += 'changed'
    for answer, entries, result in ((REAL_CLAIM+'\n\n新句。', evidence, audit),
        (REAL_CLAIM, changed_evidence, audit), (REAL_CLAIM, evidence, {**audit,'verified':False})):
        assert not module().preservation_feedback_matches(value, answer, entries, result)
    value['supported_current_draft'][0]['text'] = '伪造事实'
    assert not module().preservation_feedback_matches(value, REAL_CLAIM, evidence, audit)


def test_known_failed_current_claim_is_excluded_and_history_is_not_new_verdict(db):
    cid, evidence = fixed_table(db)
    answer = REAL_CLAIM + '\n\n这项研究已经证明因果关系。[^e4]'
    audit = audit_for(answer, evidence, db, cid)
    audit.update(passed=False, status='failed', known_failed_count=1,
        unsupported=[{'text': audit['claims'][1]['text'], 'reason':'未支持因果关系'}],
        prior_known_failures=[{'claims':[{'text':'旧稿错误','evidence_ids':['e4']}]}])
    from app.agent.citation_scope import verified_source_metadata
    audit = module().bind_audit_preservation(answer,evidence,verified_source_metadata(db,evidence,cid),audit)
    value = module().build_preservation_feedback(db, answer, evidence, audit, conversation_id=cid)
    assert value['available'] and value['supported_current_draft'] == audit['claims'][:1]
    assert '旧稿错误' not in json.dumps(value, ensure_ascii=False)
