"""Actual-byte native Word charts; no native Office/rendering claims."""
from copy import deepcopy
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app import artifact_store as store
from app import artifact_exports, artifact_export_formats as formats
from app.artifact_models import ArtifactComputation
from app.main import app


def source(db, kind='bar', rows=None):
    return store.create(db, request_id=uuid4().hex, title='Native editable chart', kind='analysis',
        content=rows or 'group,a,b,c\n=1+1,-2,10,5\nB,,20,5\nC,4,30,5\n', evidence=[],
        analysis={'category_column': 'group', 'numeric_columns': ['a', 'b', 'c'],
            'chart_type': kind, 'units': {'a': 'kg', 'b': 'kg', 'c': 'kg'}})


def make(db, item, **overrides):
    return artifact_exports.create(db, artifact_id=item['id'], revision_id=item['current_revision_id'],
        conversation_id=None, request_id=uuid4().hex, format='docx', **overrides)


def rewrite(payload, mutate):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        members = {name: z.read(name) for name in z.namelist()}
    mutate(members)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in members.items():
            z.writestr(name, value)
    return output.getvalue()


@pytest.mark.parametrize('kind', ['bar', 'line', 'radar'])
def test_native_chart_api_and_legacy_share_contract(db, kind):
    from openpyxl import load_workbook
    item = source(db, kind)
    with TestClient(app) as client:
        response = client.post(f"/api/artifacts/{item['id']}/exports", json={
            'request_id': uuid4().hex, 'revision_id': item['current_revision_id'], 'format': 'docx'})
        assert response.status_code == 200, response.text
        receipt = response.json()
        saved = client.get(receipt['download_url'])
        assert saved.status_code == 200, saved.text
        old = client.get(f"/api/artifacts/{item['id']}/download", params={'format': 'docx',
            'revision_id': item['current_revision_id']})
        assert old.status_code == 200, old.text
    output = Path(__file__).resolve().parents[2] / 'verification/word-native-charts-current'
    output.mkdir(parents=True, exist_ok=True)
    # Preserve the review fixture while a separate native Office check has it
    # open; later tests must never replace that already reviewed byte payload.
    fixture = output / (kind + '.docx')
    if not fixture.exists():
        with fixture.open('xb') as stream:
            stream.write(saved.content)
    for payload in (saved.content, old.content):
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            chart = ET.fromstring(z.read('word/charts/chart1.xml'))
            book = load_workbook(io.BytesIO(z.read('word/embeddings/ScholarFlow-chart-data.xlsx')), data_only=False)
            assert book['RawData']['A2'].value == '=1+1'
            assert book['RawData']['A2'].data_type == 's'
            assert book['NumericData']['B3'].value is None
            assert book['Statistics']['G2'].value == 'kg'
            if kind == 'radar':
                # Complete first/third rows become series, axes become columns.
                assert book['ChartData']['B2'].value == 0
                assert book['ChartData']['B4'].value == .5
                assert book['ChartData']['C2'].value == 1
                assert book['ChartData'].max_column == 3
            else:
                assert book['ChartData']['B2'].value == -2
                assert book['ChartData']['B3'].value is None
            book.close()
            assert chart.find('.//{http://schemas.openxmlformats.org/drawingml/2006/chart}' +
                {'bar': 'barChart', 'line': 'lineChart', 'radar': 'radarChart'}[kind]) is not None
    detail = receipt['validation']['details']
    assert detail['native_editable_chart'] is True
    assert detail['native_open'] == detail['render'] == 'not_assessed'
    assert db.query(ArtifactComputation).count() == 0


@pytest.mark.parametrize('kind,limit', [('bar', 200), ('line', 200), ('radar', 30)])
def test_preview_limits_full_workbook_and_read_without_renderer(db, monkeypatch, kind, limit):
    from app import artifact_compute
    from openpyxl import load_workbook
    item = source(db, kind, 'group,a,b,c\n' + ''.join(f'{i},{i},{i+1},5\n' for i in range(205)))
    receipt = make(db, item)
    def forbidden(*a, **kw):
        pytest.fail('saved chart read rendered or ran a program')
    monkeypatch.setattr(formats, 'render', forbidden)
    monkeypatch.setattr(formats, 'execute', forbidden)
    monkeypatch.setattr(artifact_compute, 'execute', forbidden)
    monkeypatch.setattr('app.word_charts.build_analysis_docx', forbidden)
    first = artifact_exports.get(db, item['id'], receipt['export_id'], None).payload
    assert artifact_exports.get(db, item['id'], receipt['export_id'], None).payload == first
    with zipfile.ZipFile(io.BytesIO(first)) as z:
        book = load_workbook(io.BytesIO(z.read('word/embeddings/ScholarFlow-chart-data.xlsx')), read_only=True)
        assert book['RawData'].max_row == 206
        assert (book['ChartData'].max_column - 1 if kind == 'radar' else book['ChartData'].max_row - 1) == limit
        book.close()
    assert receipt['validation']['details']['table_visible_rows'] == 200
    assert db.query(ArtifactComputation).count() == 0


@pytest.mark.parametrize('damage', ['chart_value', 'chart_formula', 'chart_type', 'chart_relation',
    'doc_relation', 'extra_embedding', 'external_relationship', 'workbook_value', 'workbook_formula',
    'workbook_raw', 'workbook_provenance', 'workbook_extra', 'workbook_external', 'duplicate_relation',
    'root_relation', 'macro_type', 'body_value'])
def test_independent_fixed_input_validation_rejects_self_consistent_damage(db, damage):
    item = source(db)
    row = store.get(db, item['id'])
    rev = store.revision(db, row)
    payload, generation = formats.render(db, row, rev, row.title, 'docx')
    def mutate(members):
        chart = 'word/charts/chart1.xml'
        if damage in ('chart_value', 'chart_formula', 'chart_type'):
            replacements = {'chart_value': (b'>-2.0<', b'>999.0<'),
                'chart_formula': (b"$B$2:$B$4", b"$C$2:$C$4"),
                'chart_type': (b'barChart', b'lineChart')}
            before, after = replacements[damage]
            assert before in members[chart]
            members[chart] = members[chart].replace(before, after)
        elif damage == 'chart_relation':
            key = 'word/charts/_rels/chart1.xml.rels'
            members[key] = members[key].replace(b'ScholarFlow-chart-data.xlsx', b'wrong.xlsx')
        elif damage == 'doc_relation':
            key = 'word/_rels/document.xml.rels'
            members[key] = members[key].replace(b'charts/chart1.xml', b'charts/chart2.xml')
        elif damage == 'extra_embedding':
            members['word/embeddings/extra.xlsx'] = b'extra'
        elif damage == 'external_relationship':
            key = 'word/charts/_rels/chart1.xml.rels'
            root = ET.fromstring(members[key]); root[0].set('TargetMode', 'External')
            members[key] = ET.tostring(root)
        elif damage == 'duplicate_relation':
            key = 'word/_rels/document.xml.rels'
            root = ET.fromstring(members[key]); root.append(deepcopy(root[0])); members[key] = ET.tostring(root)
        elif damage == 'root_relation':
            key = '_rels/.rels'
            root = ET.fromstring(members[key]); root[0].set('Type', 'invalid'); members[key] = ET.tostring(root)
        elif damage == 'macro_type':
            key = '[Content_Types].xml'
            members[key] = members[key].replace(b'wordprocessingml.document.main+xml', b'ms-word.document.macroEnabled.main+xml')
        elif damage in ('workbook_extra', 'workbook_external'):
            key = 'word/embeddings/ScholarFlow-chart-data.xlsx'
            def damage_book(book_members):
                if damage == 'workbook_extra':
                    book_members['xl/unknown.xml'] = b'<unknown/>'
                else:
                    rel = 'xl/_rels/workbook.xml.rels'
                    root = ET.fromstring(book_members[rel]); root[0].set('TargetMode', 'External')
                    book_members[rel] = ET.tostring(root)
            members[key] = rewrite(members[key], damage_book)
        elif damage.startswith('workbook'):
            from openpyxl import load_workbook
            key = 'word/embeddings/ScholarFlow-chart-data.xlsx'
            book = load_workbook(io.BytesIO(members[key]))
            if damage == 'workbook_raw':
                book['RawData']['B2'] = '999'
            elif damage == 'workbook_provenance':
                book['Provenance']['B2'] = 'wrong-artifact'
            else:
                book['ChartData']['B2'] = '=999' if damage == 'workbook_formula' else 999
            out = io.BytesIO(); book.save(out); book.close(); members[key] = out.getvalue()
        elif damage == 'body_value':
            key = 'word/document.xml'
            assert b'>-2.0<' in members[key]
            members[key] = members[key].replace(b'>-2.0<', b'>999.0<')
    bad = rewrite(payload, mutate)
    generation = deepcopy(generation)
    if damage not in ('extra_embedding', 'chart_relation', 'doc_relation', 'macro_type', 'duplicate_relation'):
        generation['descriptor'] = formats._docx(bad, analysis_chart=True)
    with pytest.raises((ValueError, KeyError)):
        formats.inspect(db, row, rev, row.title, 'docx', bad, generation)


def test_saved_old_revision_survives_head_change_and_retry(db, monkeypatch):
    item = source(db, 'line')
    receipt = make(db, item)
    original = artifact_exports.get(db, item['id'], receipt['export_id'], None).payload
    row = store.get(db, item['id'])
    from app.artifact_models import ArtifactChange
    proposal = store.propose(db, row, request_id=uuid4().hex, base_revision_id=row.current_revision_id,
        content='group,a,b,c\nLater,100,200,300\n')
    store.decide(db, row, db.get(ArtifactChange, proposal['id']), accept=True,
        expected_revision_id=item['current_revision_id'])
    assert row.version == 2 and row.current_revision_id != item['current_revision_id']
    row.title = 'Renamed after export'
    db.commit()
    def forbidden(*a, **kw):
        pytest.fail('old export regenerated')
    monkeypatch.setattr(formats, 'render', forbidden)
    saved = artifact_exports.get(db, item['id'], receipt['export_id'], None)
    assert saved.title == 'Native editable chart' and saved.payload == original
    assert saved.revision_id == item['current_revision_id']


def test_radar_all_incomplete_keeps_empty_chart_and_original_data(db):
    item = source(db, 'radar', 'group,a,b,c\nA,1,,3\nB,,2,3\n')
    receipt = make(db, item)
    assert receipt['validation']['details']['chart_series'] == 0
    assert receipt['validation']['details']['chart_categories'] == 3
    assert receipt['validation']['details']['fixed_input_total_rows'] == 2


def test_fixed_source_revision_mismatch_refuses_read(db, monkeypatch):
    from sqlalchemy import update
    from app.artifact_models import ArtifactRevision
    from fastapi import HTTPException
    item = source(db)
    receipt = make(db, item)
    db.execute(update(ArtifactRevision).where(ArtifactRevision.id == item['current_revision_id']).values(
        content='group,a,b,c\nA,999,999,999\n'))
    db.commit()
    monkeypatch.setattr(formats, 'render', lambda *a, **k: pytest.fail('corruption regenerated file'))
    with pytest.raises(HTTPException) as exc:
        artifact_exports.get(db, item['id'], receipt['export_id'], None)
    assert exc.value.status_code == 409
