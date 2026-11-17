"""Independent adversarial checks for exact emphasis citation insertion.

All judgments and model responses are fabricated; no real model calls.
"""
from collections import Counter
from copy import deepcopy
import json

import pytest

from app.agent import audit
from app.agent import citation_marker_repair as repair
from app.llm import LLMResponse
from app.rag.citation import MARKER_RE, citation_gate, parse_claims


EVIDENCE = [{'id': 'e1', 'quote': 'PUBLIC_FIXTURE: twelve of twenty synthetic records were relevant.'}]
FACT = '附件记载20条合成记录中12条相关。'


class Reply:
    mode = 'api'
    def __init__(self, texts=(), *, indices=None, raw=None):
        self.texts, self.indices, self.raw, self.calls = texts, indices, raw, []
    def chat(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        value = self.raw if self.raw is not None else json.dumps({'edits': [
            {'candidate_index': (self.indices or list(range(len(self.texts))))[i],
             'text': text, 'evidence_ids': ['e1']} for i, text in enumerate(self.texts)]}, ensure_ascii=False)
        return LLMResponse(value)


def scope(answer, kinds=None, required=None):
    base = citation_gate(answer, ['e1']); out = deepcopy(base)
    kinds = kinds or ['source'] * len(base['unsupported'])
    required = required if required is not None else [True] * len(base['unsupported'])
    assert len(kinds) == len(required) == len(base['unsupported'])
    out['unsupported'] = [text for i, text in enumerate(base['unsupported']) if required[i]]
    out['scoped_review'] = {'protocol': repair.PROTOCOL, 'verified': True,
        'original_total_claims': base['total_claims'], 'original_marked_claims': base['marked_claims'],
        'original_unsupported_count': len(base['unsupported']), 'decisions': [
            {'index': i, 'text': text, 'kind': kinds[i], 'requires_citation': required[i]}
            for i, text in enumerate(base['unsupported'])]}
    return out


def unchanged(answer, result):
    assert not result['changed'] and result['answer'] == answer and not result['edits'], result
    assert result['semantic_verification'] == 'not_performed'
    assert 'passed' not in result and 'verified' not in result


@pytest.mark.parametrize('text', [
    '***' + FACT + '***', '****' + FACT + '****', '**' + FACT + '__',
    '_**' + FACT + '**_', '__' + FACT + '**', '**' + FACT + '*',
    '\\**' + FACT + '**', '**' + FACT + '\\**', '`**' + FACT + '**`',
    '**' + FACT + '额外的第二句。**', '“**' + FACT + '**”', '> **' + FACT + '**',
])
def test_fake_unbalanced_literal_or_quoted_whole_wrappers_cannot_expand_span(text):
    unchanged(text, repair.repair_citation_markers(text, EVIDENCE, scope(text), Reply([text])))


def test_quoted_or_code_repeated_prefix_is_not_resolved_by_guessing():
    for answer in [
        '引述“**' + FACT + '**”之后再分析。',
        '```md\n**' + FACT + '**\n```\n\n**' + FACT + '**',
        '**' + FACT + '**\n\n**' + FACT + '**',
    ]:
        unchanged(answer, repair.repair_citation_markers(answer, EVIDENCE, scope(answer), Reply(['**' + FACT + '**'])))


def test_repeated_eighty_character_prefix_stays_ambiguous():
    stem = '**' + '合成附件记录' * 18
    first, second = stem + '第一种结果。**', stem + '第二种结果。**'
    answer = first + '\n\n' + second
    gate = scope(answer)
    assert gate['unsupported'][0] == gate['unsupported'][1]
    model = Reply([first])
    unchanged(answer, repair.repair_citation_markers(answer, EVIDENCE, gate, model))
    assert not model.calls


def test_unicode_crlf_inner_markup_insertion_preserves_all_original_codepoints():
    for wrapper in ('**', '__'):
        first = wrapper + '🙂样本α的记录量为`20`，*相关项*为12。' + wrapper
        neighbor = '邻居保留组合字符e\u0301及全角括号（附注）。[^e1]'
        answer = first + '\r\n\r\n' + neighbor + '  \r\n'
        before = citation_gate(answer, ['e1'])
        result = repair.repair_citation_markers(answer, EVIDENCE, scope(answer), Reply([first]))
        assert result['changed'], result
        assert result['answer'] == first[:-2] + '[^e1]' + wrapper + '\r\n\r\n' + neighbor + '  \r\n'
        assert MARKER_RE.sub('', result['answer']) == MARKER_RE.sub('', answer)
        after = citation_gate(result['answer'], ['e1'])
        assert after['total_claims'] == before['total_claims'] and after['passed']
        assert len(parse_claims(result['answer'], ['e1'])) == 2
        assert result['semantic_verification'] == 'not_performed'


@pytest.mark.parametrize('kind,requires', [('mixed', True), ('uncertain', True),
    ('source', True), ('process', False)])
def test_intro_marker_cannot_hide_unedited_substantive_list_item(kind, requires):
    intro, item = '**' + FACT + '**', '- 尚未核对的第二项实际结果为999。'
    answer = intro + '\n' + item
    gate = scope(answer, kinds=['source', kind], required=[True, requires])
    unchanged(answer, repair.repair_citation_markers(answer, EVIDENCE, gate, Reply([intro])))


def test_two_explicit_source_edits_keep_every_inherited_fact_in_audit_inputs():
    intro, item = '**' + FACT + '**', '- 第二项保留原始限定条件。'
    answer = intro + '\n' + item
    result = repair.repair_citation_markers(answer, EVIDENCE, scope(answer), Reply([intro, item]))
    assert result['changed'] and len(result['edits']) == 2
    assert citation_gate(result['answer'], ['e1'])['passed']
    claims = parse_claims(result['answer'], ['e1'])
    assert len(claims) == 2 and all(c['evidence_ids'] == ['e1'] for c in claims)
    assert any('第二项' in c['claim'] for c in claims)


@pytest.mark.parametrize('kind', ['mixed', 'uncertain', 'process', 'independent'])
def test_old_source_receipt_cannot_be_replayed_under_a_different_current_kind(kind):
    text = '**' + FACT + '**'
    result = repair.repair_citation_markers(text, EVIDENCE, scope(text), Reply([text]))
    assert result['changed']
    unchanged(text, repair.reuse_citation_marker_annotation(text, EVIDENCE, scope(text, [kind]), result))


def test_cached_offsets_marker_or_text_tamper_are_rejected_without_rewriting():
    text = '__' + FACT + '__'
    gate = scope(text)
    original = repair.repair_citation_markers(text, EVIDENCE, gate, Reply([text]))
    assert original['changed']
    for field, value in [('insert_at', len(text)), ('insert_at', True), ('start', 1),
            ('end', len(text) - 2), ('text_sha256', '0' * 64),
            ('inserted_markers', '[^e1][^e1]'), ('evidence_ids', ['e1', 'e1'])]:
        corrupted = deepcopy(original); corrupted['edits'][0][field] = value
        unchanged(text, repair.reuse_citation_marker_annotation(text, EVIDENCE, gate, corrupted))
    corrupted = deepcopy(original); corrupted['answer'] += '增加的内容'
    unchanged(text, repair.reuse_citation_marker_annotation(text, EVIDENCE, gate, corrupted))


def test_marker_success_does_not_gain_factual_approval_or_lose_false_claim():
    text = '**附件记载9999项全部有效。**'
    result = repair.repair_citation_markers(text, EVIDENCE, scope(text), Reply([text]))
    assert result['changed'] and citation_gate(result['answer'], ['e1'])['passed']
    assert result['semantic_verification'] == 'not_performed' and 'passed' not in result
    claims = parse_claims(result['answer'], ['e1'])
    assert len(claims) == 1 and '9999' in claims[0]['claim']
    rejected = audit.audit_answer(None, result['answer'], EVIDENCE, llm=Reply(raw=json.dumps({'results': [
        {'index': 0, 'supported': False, 'reason': 'Synthetic contradiction remains after marker insertion.'}]})))
    assert rejected['status'] == 'failed' and not rejected['passed'] and rejected['known_failed_count'] == 1
    assert len(rejected['claims']) == 1 and '9999' in rejected['unsupported'][0]['text']


def test_substantive_underscore_names_and_quantities_are_still_claims():
    samples = ['MODEL_ID为deepseek_flash。', '__字段_1__的值为0。', '___数据为1。', 'x_y=12。']
    for text in samples:
        gate = citation_gate(text, ['e1'])
        assert gate['total_claims'] == 1 and gate['unsupported']
        assert parse_claims(text + '[^e1]', ['e1'])
    assert citation_gate('__\n**\n___\n***', ['e1'])['total_claims'] == 0


def test_emphasis_unknown_annotation_dispatch_stays_unknown_across_scope_replay():
    from app.agent.checkpoint import AgentState
    from app.agent.execution import _citation_marker_annotation
    text = '**' + FACT + '**'; state = AgentState(question='核对附件', answer=text, evidence=EVIDENCE)
    model, durable = Reply([text]), []
    def fail_after_pending():
        durable.append(state.snapshot()); raise OSError('fabricated journal failure')
    with pytest.raises(OSError, match='fabricated journal failure'):
        _citation_marker_annotation(state, scope(text), model, persist=fail_after_pending,
            before_review=lambda: None, on_usage=lambda usage: None)
    restored = AgentState.model_validate(durable[-1]); keys = set(restored.source_attribution_reviews)
    assert len(keys) == 1 and next(iter(keys)).startswith('citation_marker/')
    result = _citation_marker_annotation(restored, scope(text, ['process']), model,
        persist=lambda: None, before_review=lambda: None, on_usage=lambda usage: None)
    assert not model.calls and result['error_code'] == 'dispatch_outcome_unknown'
    assert restored.answer == text and set(restored.source_attribution_reviews) == keys


def test_emphasis_committed_receipt_with_tampered_insert_offset_cannot_apply_or_rebuy():
    from app.agent.checkpoint import AgentState
    from app.agent.execution import _citation_marker_annotation
    text = '__' + FACT + '__'; state = AgentState(question='核对附件', answer=text, evidence=EVIDENCE)
    model, durable = Reply([text]), []
    def stop_after_result():
        if any(value.get('changed') for value in state.source_attribution_reviews.values()):
            raise RuntimeError('fabricated cancellation after saved annotation')
    with pytest.raises(RuntimeError, match='fabricated cancellation'):
        _citation_marker_annotation(state, scope(text), model, persist=lambda: durable.append(state.snapshot()),
            before_review=stop_after_result, on_usage=lambda usage: None)
    restored = AgentState.model_validate(durable[-1])
    assert len(model.calls) == 1 and restored.answer == text
    receipt = next(iter(restored.source_attribution_reviews.values()))
    receipt['edits'][0]['insert_at'] = len(text)
    result = _citation_marker_annotation(restored, scope(text), model,
        persist=lambda: None, before_review=lambda: None, on_usage=lambda usage: None)
    unchanged(text, result)
    assert restored.answer == text and len(model.calls) == 1
