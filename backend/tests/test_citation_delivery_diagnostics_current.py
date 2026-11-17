"""Public review failures describe the stage, never invent missing science."""
import pytest

from app.agent.execution import _citation_refusal_diagnostic, _confirmed_scope_missing


@pytest.mark.parametrize('gate,audit,code,phrase', [
    ({'scope_review_failure':'output_incomplete'}, {}, 'scope_review_incomplete', '引用范围审核未完整返回'),
    ({'scope_review_failure':'json'}, {}, 'scope_review_unverified', '范围审核未取得完整可核验结果'),
    ({'error_code':'dispatch_outcome_unknown'}, {}, 'review_dispatch_unknown', '可能已计费'),
    ({}, {'known_failed_count':1}, 'source_semantic_rejected', '原文不支持'),
    ({'invalid_markers':['e9']}, {}, 'invalid_source_markers', '引用编号'),
    ({'scoped_review':{'verified':True},'unsupported':['FACT']}, {}, 'confirmed_missing_citations', '缺少来源引用'),
    ({}, {'status':'unverified','verified':False}, 'semantic_review_unverified', '语义审核尚未完成'),
    ({}, {}, 'citation_coverage_unresolved', '引用覆盖仍未通过'),
])
def test_each_unfinished_reason_has_an_explicit_safe_public_diagnosis(gate, audit, code, phrase):
    result = _citation_refusal_diagnostic(gate, audit)
    assert result['reason_code'] == code and code in result['reason_codes']
    assert phrase in result['answer']
    assert '证据不足' not in result['answer']


def test_multiple_causes_are_retained_and_private_exception_is_not_copied():
    gate = {'scope_review_failure':'output_incomplete', 'error_code':'PRIVATE_PROVIDER_BODY',
        'invalid_markers':['PRIVATE_SOURCE_NAME']}
    result = _citation_refusal_diagnostic(gate, {'known_failed_count':1})
    assert result['reason_codes'] == ['source_semantic_rejected','invalid_source_markers','scope_review_incomplete']
    assert 'PRIVATE' not in str(result)


def test_uncertain_scope_decision_is_not_a_confirmed_missing_citation():
    gate = {
        'unsupported': ['我没有检索到覆盖以下内容的片段，因此不作断言。'],
        'scoped_review': {
            'verified': True,
            'decisions': [{
                'index': 0,
                'text': '我没有检索到覆盖以下内容的片段，因此不作断言。',
                'requires_citation': True,
                'kind': 'uncertain',
            }],
        },
    }
    assert _confirmed_scope_missing(gate) == []


def test_typed_source_scope_decision_remains_confirmed_missing():
    gate = {
        'unsupported': ['该实验准确率为99%。'],
        'scoped_review': {
            'verified': True,
            'decisions': [{
                'index': 0,
                'text': '该实验准确率为99%。',
                'requires_citation': True,
                'kind': 'source',
            }],
        },
    }
    assert _confirmed_scope_missing(gate) == ['该实验准确率为99%。']
