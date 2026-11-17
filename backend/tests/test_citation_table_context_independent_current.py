"""Independent real DOCX boundaries; no production or author-test mutation."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4

import pytest
from docx import Document

from app.agent import citation_table_context as target
from app.models import Conversation
from app.rag.citation import MARKER_RE, citation_gate, parse_claims


ROOT = Path(__file__).resolve().parents[2]
FROZEN_REPORT = ROOT / 'verification/real-model-20261116/current-marker-feedback-doc04/acceptance-report.json'
REAL_CLAIM = '因此，“Completed follow-up”（完成随访）的计数为 36 人[^e4][^e5]。'
VALUES = ('Follow-up participants', 'Count', 'Completed follow-up', '36')


def independent_word(db, *, variant=None, rows=2, columns=2, table_count=1, cid=None, values=VALUES,
                     header_table=False):
    """Fresh actual upload, retained OOXML, extraction and authorized locators."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.source_documents import read_document, add_document_locator
    from app.source_runtime import conversation_scope, freeze_sources

    if cid is None:
        cid = 'd04-table-independent-' + uuid4().hex
        db.add(Conversation(id=cid))
        db.commit()
    doc = Document()
    for text in ('Fabricated acceptance protocol, not real patient data.',
                 'Question: describe sleep duration in an observational follow-up cohort.',
                 'Method: questionnaire, no randomization or intervention.'):
        doc.add_paragraph(text)
    for table_index in range(table_count):
        table = doc.add_table(rows=rows, cols=columns)
        for row in range(rows):
            for column in range(columns):
                index = row * columns + column
                table.cell(row, column).text = (values[index] if rows * columns == 4
                    else f'T{table_index}-R{row + 1}-C{column + 1}')
        cell = table.cell(0, 0)
        if variant == 'empty_extra_paragraph':
            cell.add_paragraph('')
        elif variant == 'whitespace_extra_paragraph':
            cell.add_paragraph('   ')
        elif variant == 'empty_nested_table':
            cell.add_table(rows=1, cols=1)
        elif variant == 'empty_cell':
            cell.text = ''
        elif variant == 'text_extra_paragraph':
            cell.add_paragraph('Second paragraph, independently read.')
        elif variant == 'merged':
            cell.merge(table.cell(0, 1))
    if header_table:
        from docx.shared import Inches
        header = doc.sections[0].header.add_table(rows=2, cols=2, width=Inches(6))
        for index, value in enumerate(VALUES):
            header.cell(index // 2, index % 2).text = value
    payload = BytesIO()
    doc.save(payload)
    with TestClient(app) as client:
        response = client.post('/api/sources/upload', files={
            'file': ('independent-protocol.docx', payload.getvalue())}, data={
            'conversation_id': cid, 'request_id': uuid4().hex})
    assert response.status_code == 200, response.text
    revision = response.json()['revision_id']
    freeze_sources(db, cid, [revision], {'conversation_id': cid})
    scope = conversation_scope(db, cid, writing=False)
    document = read_document(db, revision, scope=scope)
    paragraphs = [block for block in document['blocks'] if 'paragraph_index' in block]
    paragraph = add_document_locator(db, revision, scope=scope,
        kind='word_paragraphs', paragraph_start=1, paragraph_end=min(5, len(paragraphs)))
    evidence = [{'id': 'e1', 'source_id': document['source_id'],
        'revision_id': revision, 'locator_id': paragraph['locator_id'],
        'quote': paragraph['quote'], 'source_location': deepcopy(paragraph['location'])}]
    for block in document['blocks']:
        if not block.get('table_path'):
            continue
        locator = add_document_locator(db, revision, scope=scope,
            kind='word_block', block_id=block['block_id'])
        evidence.append({'id': f'e{len(evidence) + 1}', 'source_id': document['source_id'],
            'revision_id': revision, 'locator_id': locator['locator_id'],
            'quote': locator['quote'], 'source_location': deepcopy(locator['location'])})
    db.commit()
    return cid, evidence


def test_frozen_full_doc04_rewrite_adds_exact_header_context_only(db):
    cid, evidence = independent_word(db)
    report = json.loads(FROZEN_REPORT.read_text(encoding='utf-8'))
    answer = report['cases'][0]['result']['turns'][0]['public_citation_drafts'][-1]['answer']
    assert answer.count(REAL_CLAIM) == 1
    result = target.table_context_candidate(db, answer, evidence, conversation_id=cid)
    assert result['changed'], result
    expected = answer.replace(REAL_CLAIM, REAL_CLAIM.replace('[^e5]', '[^e5][^e2][^e3]'))
    assert result['answer'] == expected
    assert MARKER_RE.sub('', expected).encode('utf-8') == MARKER_RE.sub('', answer).encode('utf-8')
    before_claims = parse_claims(answer, [entry['id'] for entry in evidence])
    after_claims = parse_claims(expected, [entry['id'] for entry in evidence])
    expected_claims = deepcopy(before_claims)
    matching = [claim for claim in expected_claims if claim['claim'] == MARKER_RE.sub('', REAL_CLAIM)]
    assert len(matching) == 1
    matching[0]['evidence_ids'] += ['e2', 'e3']
    assert after_claims == expected_claims
    assert citation_gate(answer, [e['id'] for e in evidence]) == citation_gate(expected, [e['id'] for e in evidence])
    assert result['semantic_verification'] == 'not_performed'
    assert len(result['edits']) == 1


@pytest.mark.parametrize('variant', ['empty_extra_paragraph', 'whitespace_extra_paragraph', 'empty_nested_table'])
def test_unextracted_empty_structure_cannot_certify_single_complete_cell(db, variant):
    cid, evidence = independent_word(db, variant=variant)
    result = target.table_context_candidate(db, REAL_CLAIM, evidence, conversation_id=cid)
    assert result['changed'] is False, {
        'variant': variant, 'actual_answer': result['answer'],
        'accepted_groups': target.verified_small_tables(db, evidence, conversation_id=cid)}
    assert result['answer'] == REAL_CLAIM


def production_audit(db, answer, evidence, cid, *, rejected=()):
    """A real audit pipeline with an explicitly synthetic one-call auditor."""
    import re
    from app.agent.audit import audit_answer
    from app.llm import LLMResponse

    class IndependentAuditor:
        mode = 'api'
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            indices = [int(value) for value in re.findall(r'^\[论断 (\d+)\]', messages[-1]['content'], re.M)]
            return LLMResponse(json.dumps({'results': [{'index': index, 'supported': index not in rejected,
                'reason': 'Synthetic verdict for this exact request, not scientific acceptance.',
                'support_kind': 'source', 'process_scope': 'none', 'supports': []}
                for index in indices]}))

    model = IndependentAuditor()
    value = audit_answer(db, answer, evidence, llm=model, messages=[], conversation_id=cid,
        audit_journal={}, question='按固定表格解释随访计数。')
    assert model.calls == 1 and value['verified'] and value['passed'] is (not rejected), value
    return value


def test_preservation_accepts_actual_current_audit_without_rephrasing_claims(db):
    from app.agent.citation_context_preservation import build_preservation_feedback
    cid, evidence = independent_word(db)
    audit = production_audit(db, REAL_CLAIM, evidence, cid)
    value = build_preservation_feedback(db, REAL_CLAIM, evidence, audit, conversation_id=cid)
    assert value['available'] is True
    assert value['supported_current_draft'] == audit['claims']
    assert value['rewrite_verdict'] == 'not_performed'


def test_prior_audit_cannot_be_rebound_to_different_authorized_evidence(db):
    from app.agent.citation_context_preservation import build_preservation_feedback
    cid, original = independent_word(db)
    audit = production_audit(db, REAL_CLAIM, original, cid)
    _, replacement = independent_word(db, cid=cid, values=VALUES[:-1] + ('99',))
    assert original[-1]['quote'] == '36' and replacement[-1]['quote'] == '99'
    assert original[-1]['revision_id'] != replacement[-1]['revision_id']
    value = build_preservation_feedback(db, REAL_CLAIM, replacement, audit, conversation_id=cid)
    assert value['available'] is False, value
    assert not value.get('supported_current_draft')


def test_prior_audit_cannot_be_rebound_to_changed_full_answer_context(db):
    from app.agent.citation_context_preservation import build_preservation_feedback
    cid, evidence = independent_word(db)
    audit = production_audit(db, REAL_CLAIM, evidence, cid)
    changed = '这是完全不同的上下文，不能假定旧稿审查覆盖本稿。\n\n' + REAL_CLAIM
    assert parse_claims(changed, [e['id'] for e in evidence]) == parse_claims(REAL_CLAIM, [e['id'] for e in evidence])
    value = build_preservation_feedback(db, changed, evidence, audit, conversation_id=cid)
    assert value['available'] is False, value
    assert not value.get('supported_current_draft')


def assert_refused(db, cid, answer, evidence):
    from app.source_store import SourceError
    try:
        result = target.table_context_candidate(db, answer, evidence, conversation_id=cid)
    except SourceError:
        return
    assert result['changed'] is False, result
    assert result['answer'] == answer


def test_distinct_evidence_ids_for_same_locator_do_not_cover_missing_cell(db):
    cid, evidence = independent_word(db)
    duplicate = deepcopy(evidence[1])
    duplicate['id'] = 'e3'
    evidence[2] = duplicate
    assert len({e['id'] for e in evidence}) == len(evidence)
    assert evidence[1]['locator_id'] == evidence[2]['locator_id']
    assert_refused(db, cid, REAL_CLAIM, evidence)


def test_authorized_cells_from_different_revisions_cannot_form_one_table(db):
    cid, original = independent_word(db)
    _, other = independent_word(db, cid=cid, values=VALUES[:-1] + ('99',))
    mixed = deepcopy(original)
    mixed[2] = deepcopy(other[2])
    assert mixed[2]['quote'] == original[2]['quote'] == 'Count'
    assert mixed[2]['revision_id'] != mixed[1]['revision_id']
    assert_refused(db, cid, REAL_CLAIM, mixed)


def test_two_same_text_tables_choose_only_the_seeded_table(db):
    cid, evidence = independent_word(db, table_count=2)
    answer = '该表这一行的合成计数为36[^e8][^e9]。'
    result = target.table_context_candidate(db, answer, evidence, conversation_id=cid)
    assert result['changed']
    assert result['answer'] == answer.replace('[^e9]', '[^e9][^e6][^e7]')
    assert all(eid not in result['edits'][0]['inserted_markers'] for eid in ('[^e2]', '[^e3]'))
    assert_refused(db, cid, '不同表的合成数值不能合为同表[^e4][^e9]。', evidence)


def test_body_and_header_cells_cannot_be_mixed_despite_same_text(db):
    cid, evidence = independent_word(db, header_table=True)
    body = [entry for entry in evidence if entry['source_location'].get('part_name') == 'word/document.xml']
    header = [entry for entry in evidence if entry['source_location'].get('part_name', '').startswith('word/header')]
    assert len(body) == len(header) == 4
    answer = f"不能把正文及页眉数值合为同一表[^${body[-1]['id']}][^${header[-1]['id']}]。".replace('^$', '^')
    assert_refused(db, cid, answer, evidence)
    seed = ''.join(f"[^{entry['id']}]" for entry in body[-2:])
    valid = '正文中的合成计数为36' + seed + '。'
    result = target.table_context_candidate(db, valid, evidence, conversation_id=cid)
    assert result['changed']
    assert result['edits'][0]['inserted_markers'] == ''.join(f"[^{entry['id']}]" for entry in body[:2])


@pytest.mark.parametrize('field,value', [
    ('raw_sha256', 'f' * 64), ('part_name', 'word/header99.xml'),
    ('table_path', '/document/body/tbl[2]'), ('xml_path', '/document/body/tbl[1]/tr[9]/tc[1]/p[1]'),
    ('block_id', 'wb_' + '0' * 32), ('table_row', 9), ('table_column', 9),
    ('extraction_fingerprint', '0' * 64),
])
def test_rehashed_forged_locator_still_cannot_authorize_context(db, field, value):
    from app.source_models import SourceLocator
    from app.source_store import _locator_hash
    cid, evidence = independent_word(db)
    row = db.get(SourceLocator, evidence[1]['locator_id'])
    location = deepcopy(row.location)
    location[field] = value
    fingerprint = _locator_hash(row.revision_id, row.kind, location, row.quote)
    # Explicit isolated-database corruption injection. Ordinary ORM writes are
    # forbidden by the existing immutable source event and never reach target.
    db.execute(SourceLocator.__table__.update().where(SourceLocator.id == row.id).values(
        location=location, fingerprint=fingerprint))
    db.commit()
    db.expire_all()
    # A self-consistent locator digest alone must not certify fixed cell data.
    assert_refused(db, cid, REAL_CLAIM, evidence)


def test_ordinary_orm_cannot_mutate_the_fixed_locator_before_context_review(db):
    from app.source_models import ImmutableSourceError, SourceLocator
    cid, evidence = independent_word(db)
    row = db.get(SourceLocator, evidence[1]['locator_id'])
    original = deepcopy(row.location)
    row.location = {**original, 'table_row': 99}
    with pytest.raises(ImmutableSourceError):
        db.commit()
    db.rollback()
    assert db.get(SourceLocator, evidence[1]['locator_id']).location == original
    assert target.table_context_candidate(db, REAL_CLAIM, evidence, conversation_id=cid)['changed']


@pytest.mark.parametrize('variant', ['empty_cell', 'text_extra_paragraph', 'merged'])
def test_cell_ambiguity_refuses_instead_of_inventing_context(db, variant):
    cid, evidence = independent_word(db, variant=variant)
    assert_refused(db, cid, REAL_CLAIM, evidence)


@pytest.mark.parametrize('rows,columns,seeds,expected_added', [
    (3, 3, 1, 8), (2, 5, 1, 0), (4, 4, 8, 8), (4, 4, 7, 0), (4, 5, 12, 0),
])
def test_maximum_eight_new_markers_and_sixteen_cells_are_real_boundaries(db, rows, columns, seeds, expected_added):
    cid, evidence = independent_word(db, rows=rows, columns=columns)
    seed_entries = evidence[-seeds:]
    answer = '这里只报告已读取的合成表格内容' + ''.join(f"[^{entry['id']}]" for entry in seed_entries) + '。'
    result = target.table_context_candidate(db, answer, evidence, conversation_id=cid)
    assert result['changed'] is bool(expected_added), result
    if expected_added:
        actual_added = MARKER_RE.findall(result['edits'][0]['inserted_markers'])
        assert len(actual_added) == expected_added
        assert MARKER_RE.sub('', answer).encode('utf-8') == MARKER_RE.sub('', result['answer']).encode('utf-8')
    else:
        assert result['answer'] == answer


def test_following_list_inheritance_is_not_silently_broadened(db):
    cid, evidence = independent_word(db)
    answer = REAL_CLAIM + '\n\n- 这句继承原有引用，但不能静默得到其他表头引用。'
    before = parse_claims(answer, [e['id'] for e in evidence])
    assert before[-1]['evidence_ids'] == ['e4', 'e5']
    result = target.table_context_candidate(db, answer, evidence, conversation_id=cid)
    assert result['changed'] is False
    assert result['answer'] == answer
    assert parse_claims(result['answer'], [e['id'] for e in evidence]) == before


def test_repeated_literal_and_prose_claims_are_not_guessed_by_substring(db):
    cid, evidence = independent_word(db)
    answer = '```text\n' + REAL_CLAIM + '\n```\n\n' + REAL_CLAIM
    assert answer.count(REAL_CLAIM) == 2
    assert_refused(db, cid, answer, evidence)


@pytest.mark.parametrize('journal,record', [
    ('source_attribution_reviews', {'review_dispatch': 'pending'}),
    ('source_attribution_reviews', {'error_code': 'dispatch_outcome_unknown'}),
    ('citation_audit_batches', {'batches': {'a': {'state': 'pending'}}}),
    ('citation_audit_batches', {'batches': {'a': {'state': 'complete', 'result': {'error_code': 'dispatch_outcome_unknown'}}}}),
])
def test_pending_or_unknown_survives_json_restart_and_input_protocol_change(db, journal, record):
    from app.agent.checkpoint import AgentState
    cid, evidence = independent_word(db)
    state = AgentState(question='按当前固定表格解释计数。', conversation_id=cid,
        answer=REAL_CLAIM, evidence=evidence, provider_context={'opaque': 'same-private-context'})
    getattr(state, journal)['legacy/' + 'a' * 64] = deepcopy(record)
    restored = AgentState.model_validate(json.loads(json.dumps(state.snapshot(), ensure_ascii=False)))
    baseline = deepcopy(restored.snapshot())
    result = target.apply_table_context(restored, db=db, input_key='b' * 64,
        persist=lambda: pytest.fail('Unknown charged state must not be written or reset'), before_review=lambda: None)
    assert not result['changed'] and result['reason'] == 'dispatch_outcome_unknown'
    assert restored.snapshot() == baseline


def test_failed_persist_rolls_back_every_field_and_success_is_restart_idempotent(db):
    from app.agent.checkpoint import AgentState
    cid, evidence = independent_word(db)
    state = AgentState(question='解释随访计数。', conversation_id=cid, answer=REAL_CLAIM,
        evidence=evidence, provider_context={'private': {'opaque': 'unchanged-on-failure'}},
        usage={'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18},
        applied_input_ids=['fixed-user-input'])
    baseline = deepcopy(state.snapshot())

    def fail():
        state.usage['total_tokens'] += 999
        state.trace.append({'test_only_mutation': True})
        raise OSError('Independent checkpoint failure after candidate mutation')

    with pytest.raises(OSError):
        target.apply_table_context(state, db=db, input_key='c' * 64, persist=fail, before_review=lambda: None)
    assert state.snapshot() == baseline
    saved = []
    result = target.apply_table_context(state, db=db, input_key='c' * 64,
        persist=lambda: saved.append(deepcopy(state.snapshot())), before_review=lambda: None)
    assert result['changed'] and len(saved) == 1
    assert saved[0]['usage'] == baseline['usage'] and saved[0]['evidence'] == baseline['evidence']
    assert saved[0]['calls'] == baseline['calls'] and saved[0]['operations'] == baseline['operations']
    assert state.provider_context == {}
    restored = AgentState.model_validate(json.loads(json.dumps(saved[0], ensure_ascii=False)))
    after = deepcopy(restored.snapshot())
    for key in ('c' * 64, 'd' * 64):
        repeated = target.apply_table_context(restored, db=db, input_key=key,
            persist=lambda: pytest.fail('Annotation must not be repeated after restart'), before_review=lambda: None)
        assert not repeated['changed'] and restored.snapshot() == after


def test_cancel_after_successful_persist_preserves_the_committed_annotation(db):
    from app.agent.checkpoint import AgentState
    cid, evidence = independent_word(db)
    state = AgentState(question='解释当前表格。', conversation_id=cid, answer=REAL_CLAIM, evidence=evidence)
    saved, checked = [], []

    def before():
        checked.append(True)
        if len(checked) == 2:
            raise RuntimeError('Independent cancellation after durable annotation')

    with pytest.raises(RuntimeError, match='after durable annotation'):
        target.apply_table_context(state, db=db, input_key='e' * 64,
            persist=lambda: saved.append(deepcopy(state.snapshot())), before_review=before)
    assert len(saved) == 1 and state.snapshot() == saved[0]
    restored = AgentState.model_validate(saved[0])
    assert not target.apply_table_context(restored, db=db, input_key='e' * 64,
        persist=lambda: pytest.fail('Cannot repersist after committed cancellation'), before_review=lambda: None)['changed']


def test_current_audit_cannot_survive_server_metadata_policy_change(db, monkeypatch):
    from app import source_word_table_shapes
    from app.agent.citation_context_preservation import build_preservation_feedback
    cid, evidence = independent_word(db)
    audit = production_audit(db, REAL_CLAIM, evidence, cid)
    original = source_word_table_shapes.verified_word_table_shape

    def unknown_shape(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(complete=False, shape_kind='unknown', reason='Independent authority policy change')
        result.pop('row_count', None)
        result.pop('column_count', None)
        return result

    monkeypatch.setattr(source_word_table_shapes, 'verified_word_table_shape', unknown_shape)
    value = build_preservation_feedback(db, REAL_CLAIM, evidence, audit, conversation_id=cid)
    assert value['available'] is False, value


def test_unbound_legacy_audit_is_not_self_certified_by_the_builder(db):
    from app.agent.citation_context_preservation import build_preservation_feedback
    cid, evidence = independent_word(db)
    audit = production_audit(db, REAL_CLAIM, evidence, cid)
    legacy_fields = {'passed', 'status', 'unsupported', 'unverified', 'mode', 'verified',
        'claims', 'factual_claims', 'unsupported_ratio', 'unverified_count', 'usage',
        'known_failed_count', 'unknown_count', 'batch_count', 'new_usage', 'cumulative_usage'}
    legacy = {key: deepcopy(value) for key, value in audit.items() if key in legacy_fields}
    assert legacy['claims'] == audit['claims'] and legacy['verified']
    value = build_preservation_feedback(db, REAL_CLAIM, evidence, legacy, conversation_id=cid)
    assert value['available'] is False, value
    assert not value.get('supported_current_draft')


def test_exact_supported_list_excludes_real_current_rejection_without_rewording(db):
    from app.agent.citation_context_preservation import build_preservation_feedback
    cid, evidence = independent_word(db)
    answer = REAL_CLAIM + '\n\n该文档还证明所有参与者已经痊愈。[^e1]'
    audit = production_audit(db, answer, evidence, cid, rejected=(1,))
    assert audit['known_failed_count'] == 1 and audit['unknown_count'] == 0
    value = build_preservation_feedback(db, answer, evidence, audit, conversation_id=cid)
    assert value['available'] is True
    assert value['supported_current_draft'] == audit['claims'][:1]
    assert value['supported_current_draft'][0]['text'] == MARKER_RE.sub('', REAL_CLAIM)
    assert value['rewrite_verdict'] == 'not_performed'
    assert '所有参与者已经痊愈' not in json.dumps(value['supported_current_draft'], ensure_ascii=False)


@pytest.mark.parametrize('kind,expected', [
    ('incomplete', 'scope_review_incomplete'), ('unknown', 'review_dispatch_unknown'),
    ('known_rejection', 'source_semantic_rejected'), ('semantic_unknown', 'semantic_review_unverified'),
])
def test_review_failure_diagnostics_do_not_invent_source_or_provider_facts(kind, expected):
    from app.agent.execution import _citation_refusal_diagnostic
    gate = {'passed': False, 'unsupported': ['范围尚未完成的合成候选。'], 'invalid_markers': []}
    audit = {'passed': True, 'verified': True, 'status': 'passed', 'known_failed_count': 0,
        'unknown_count': 0, 'unsupported': []}
    if kind == 'incomplete':
        gate.update(scope_review_failure='output_incomplete', error='SYNTHETIC_PRIVATE_LENGTH_GUESS')
    elif kind == 'unknown':
        gate.update(error_code='dispatch_outcome_unknown', error='SYNTHETIC_PRIVATE_KEY')
    elif kind == 'known_rejection':
        gate['scope_review_failure'] = 'output_incomplete'
        audit.update(passed=False, status='failed', known_failed_count=1,
            unsupported=[{'text': '当前确证不支持的原句。', 'reason': 'Synthetic known rejection'}])
    else:
        audit.update(passed=False, verified=False, status='unverified', unknown_count=1)
    result = _citation_refusal_diagnostic(gate, audit)
    assert result['reason_code'] == expected
    assert expected in result['reason_codes']
    text = json.dumps(result, ensure_ascii=False)
    assert 'SYNTHETIC_PRIVATE' not in text
    assert not any(phrase in result['answer'] for phrase in ('原材料不足', '原文没有报告', 'max_tokens', 'length', 'content_filter'))
    if kind == 'incomplete':
        assert '范围审核未完整返回' in result['answer']
    elif kind == 'known_rejection':
        assert 'scope_review_incomplete' in result['reason_codes']
