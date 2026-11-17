"""Exact fixed small-table context, never a semantic verdict or model call."""
from copy import deepcopy
from io import BytesIO
import importlib

import pytest
from docx import Document

from app.agent.checkpoint import AgentState
from app.models import Conversation
from app.rag.citation import MARKER_RE, citation_gate, parse_claims


REAL_CLAIM = '因此，“Completed follow-up”（完成随访）的计数为 36 人[^e4][^e5]。'
STRUCTURE = '正文表格共两行两列：第一行为表头，第二行为数据。'


def module():
    return importlib.import_module('app.agent.citation_table_context')


def fixed_table(db, *, rows=2, columns=2, merged=False, extra_paragraph=False, nested=False):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.source_documents import read_document, add_document_locator
    from app.source_runtime import conversation_scope, freeze_sources
    cid = 'd04-small-table'
    db.add(Conversation(id=cid))
    db.commit()
    doc = Document()
    doc.add_paragraph('Fabricated protocol, not patient data.')
    table = doc.add_table(rows=rows, cols=columns)
    for r in range(rows):
        for c in range(columns):
            table.cell(r, c).text = ('Follow-up participants', 'Count', 'Completed follow-up', '36')[r * columns + c] if rows * columns == 4 else f'CELL_{r}_{c}'
    if merged:
        table.cell(0, 0).merge(table.cell(0, 1))
    if extra_paragraph:
        table.cell(0, 0).add_paragraph('Additional text')
    if nested:
        table.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = 'NESTED'
    data = BytesIO()
    doc.save(data)
    with TestClient(app) as client:
        response = client.post('/api/sources/upload', files={'file': ('protocol.docx', data.getvalue())},
            data={'conversation_id': cid, 'request_id': 'd04-table'})
    assert response.status_code == 200, response.text
    revision = response.json()['revision_id']
    freeze_sources(db, cid, [revision], {'conversation_id': cid})
    scope = conversation_scope(db, cid, writing=False)
    document = read_document(db, revision, scope=scope)
    evidence = []
    for block in document['blocks']:
        if not block.get('table_path'):
            continue
        loc = add_document_locator(db, revision, scope=scope, kind='word_block', block_id=block['block_id'])
        evidence.append({'id': f'e{len(evidence)+2}', 'source_id': document['source_id'],
            'revision_id': revision, 'locator_id': loc['locator_id'], 'quote': loc['quote'],
            'source_location': deepcopy(loc['location'])})
    db.commit()
    return cid, evidence


def test_real_doc04_claim_adds_only_missing_same_table_context(db):
    cid, evidence = fixed_table(db)
    answer = REAL_CLAIM + '\n\n' + STRUCTURE
    result = module().table_context_candidate(db, answer, evidence, conversation_id=cid)
    assert result['changed'] is True
    assert result['answer'] == REAL_CLAIM.replace('[^e5]', '[^e5][^e2][^e3]') + '\n\n' + STRUCTURE
    assert MARKER_RE.sub('', result['answer']) == MARKER_RE.sub('', answer)
    assert citation_gate(result['answer'], ['e2','e3','e4','e5'])['unsupported'] == [STRUCTURE]
    assert result['semantic_verification'] == 'not_performed'
    assert len(result['edits']) == 1
    assert result['edits'][0]['inserted_markers'] == '[^e2][^e3]'


@pytest.mark.parametrize('options', [dict(merged=True), dict(extra_paragraph=True), dict(nested=True), dict(rows=5,columns=4)])
def test_ambiguous_incomplete_or_large_table_is_not_extended(db, options):
    cid, evidence = fixed_table(db, **options)
    result = module().table_context_candidate(db, REAL_CLAIM, evidence, conversation_id=cid)
    assert not result['changed'] and result['answer'] == REAL_CLAIM


def test_fake_answer_metadata_cannot_authorize_context_or_cross_scope(db):
    cid, evidence = fixed_table(db)
    for entry in evidence:
        entry['source_location'] = {'table_path': 'fake', 'part_name': 'fake.xml'}
        entry['table_shape'] = {'complete': True, 'row_count': 99, 'column_count': 99}
    assert module().table_context_candidate(db, REAL_CLAIM, evidence, conversation_id=cid)['changed']
    with pytest.raises(Exception):
        module().table_context_candidate(db, REAL_CLAIM, evidence, conversation_id='not-authorized')


def test_missing_duplicate_or_uncited_context_is_noop(db):
    cid, evidence = fixed_table(db)
    for subset in (evidence[1:], evidence + [deepcopy(evidence[0])]):
        result = module().table_context_candidate(db, REAL_CLAIM, subset, conversation_id=cid)
        assert not result['changed']
    text = MARKER_RE.sub('', REAL_CLAIM)
    assert not module().table_context_candidate(db, text, evidence, conversation_id=cid)['changed']


@pytest.mark.parametrize('wrapper', [lambda s: '```\n'+s+'\n```', lambda s: '> '+s,
    lambda s: '| '+s+' |', lambda s: '- '+s, lambda s: '**'+s+'**', lambda s: s+'\n'+s])
def test_literal_structured_emphasized_or_repeated_prose_is_noop(db, wrapper):
    cid, evidence = fixed_table(db)
    answer = wrapper(REAL_CLAIM)
    assert not module().table_context_candidate(db, answer, evidence, conversation_id=cid)['changed']


def test_context_adoption_is_durable_rollback_safe_and_idempotent(db):
    cid, evidence = fixed_table(db)
    state = AgentState(question='解释表格', conversation_id=cid, answer=REAL_CLAIM,
        evidence=evidence, provider_context={'old': 'replay'})
    original = deepcopy(state.snapshot())
    def fail():
        raise OSError('checkpoint unavailable')
    with pytest.raises(OSError):
        module().apply_table_context(state, db=db, input_key='a'*64, persist=fail, before_review=lambda: None)
    assert state.snapshot() == original
    saved = []
    result = module().apply_table_context(state, db=db, input_key='a'*64,
        persist=lambda: saved.append(deepcopy(state.snapshot())), before_review=lambda: None)
    assert result['changed'] and len(saved) == 1 and state.provider_context == {}
    assert state.snapshot() == saved[-1]
    assert not module().apply_table_context(state, db=db, input_key='a'*64,
        persist=lambda: pytest.fail('saved twice'), before_review=lambda: None)['changed']
    assert parse_claims(state.answer, ['e2','e3','e4','e5'])[0]['evidence_ids'] == ['e4','e5','e2','e3']
