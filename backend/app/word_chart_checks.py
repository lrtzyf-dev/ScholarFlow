"""Read-only native Word chart checks against the fixed CSV and config.

The expected OOXML contract here does not call the chart renderer, load its
caches as inputs, run an analysis program, or rewrite the saved file.
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from xml.etree import ElementTree as ET

C = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
WP = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
CHART_PATH = 'word/charts/chart1.xml'
BOOK_PATH = 'word/embeddings/ScholarFlow-chart-data.xlsx'


def _node(tag, attributes=None, text='', children=(), namespace=C):
    return ('{' + namespace + '}' + tag, tuple(sorted((k, str(v)) for k, v in (attributes or {}).items())),
        text, tuple(children))


def _actual(node):
    # Formatting whitespace between XML elements is irrelevant; text in value
    # leaves (including whitespace-only category labels) is significant.
    return (node.tag, tuple(sorted(node.attrib.items())), node.text or '' if not len(node) else '',
        tuple(_actual(child) for child in node))


def _title(text):
    return _node('title', children=[_node('tx', children=[_node('rich', children=[
        _node('bodyPr', namespace=A), _node('lstStyle', namespace=A),
        _node('p', namespace=A, children=[_node('r', namespace=A, children=[_node('t', text=text, namespace=A)])])])]),
        _node('overlay', {'val': 0})])


def _ref(tag, kind, formula, values):
    children = ([_node('formatCode', text='General')] if kind == 'num' else [])
    children += [_node('ptCount', {'val': len(values)})]
    children += [_node('pt', {'idx': i}, children=[_node('v', text=str(value))])
        for i, value in enumerate(values) if value is not None]
    return _node(tag, children=[_node(kind + 'Ref', children=[_node('f', text=formula),
        _node(kind + 'Cache', children=children)])])


def _axis(kind, identifier, cross, position, unit=None, radar=False):
    scaling = [_node('orientation', {'val': 'minMax'})]
    if radar:
        scaling += [_node('max', {'val': 1}), _node('min', {'val': 0})]
    nodes = [_node('axId', {'val': identifier}), _node('scaling', children=scaling),
        _node('delete', {'val': 0}), _node('axPos', {'val': position})]
    if kind == 'valAx':
        nodes += [_node('majorGridlines'), _title(unit), _node('numFmt', {'formatCode': 'General', 'sourceLinked': 0})]
    nodes += [_node('majorTickMark', {'val': 'none'}), _node('minorTickMark', {'val': 'none'}),
        _node('tickLblPos', {'val': 'nextTo'}), _node('crossAx', {'val': cross}), _node('crosses', {'val': 'autoZero'})]
    nodes += ([_node('auto', {'val': 1}), _node('lblAlgn', {'val': 'ctr'}), _node('lblOffset', {'val': 100})]
        if kind == 'catAx' else [_node('crossBetween', {'val': 'between'})])
    return _node(kind, children=nodes)


def _expected_data(rev, numeric, statistics):
    config = rev.analysis
    names = config['numeric_columns']
    if config['chart_type'] == 'radar':
        categories = list(names)
        rows = [row for row in numeric[1:31] if all(v is not None for v in row[1:])]
        stats = {row[0]: row for row in statistics[1:]}
        series = []
        for row in rows:
            normalized = []
            for index, name in enumerate(names, 1):
                span = stats[name][5] - stats[name][3]
                if not math.isfinite(span):
                    raise ValueError('nonfinite radar range')
                normalized.append((row[index] - stats[name][3]) / span if span else .5)
            series.append((row[0], normalized))
        unit = 'normalized [0,1]'
    else:
        categories = [row[0] for row in numeric[1:201]]
        series = [(name, [row[i] for row in numeric[1:201]]) for i, name in enumerate(names, 1)]
        unit = config.get('units', {}).get(names[0], 'unspecified')
    chart_rows = [['category', *[name for name, _ in series]]]
    chart_rows += [[category, *[values[i] for _, values in series]] for i, category in enumerate(categories)]
    return categories, series, unit, chart_rows


def _expected_chart(config, categories, series, unit):
    from openpyxl.utils import get_column_letter
    kind = config['chart_type']
    graph = ([_node('barDir', {'val': 'col'}), _node('grouping', {'val': 'clustered'})] if kind == 'bar'
        else [_node('grouping', {'val': 'standard'})] if kind == 'line'
        else [_node('radarStyle', {'val': 'marker'})])
    graph.append(_node('varyColors', {'val': 0}))
    for i, (name, values) in enumerate(series):
        col = get_column_letter(i + 2)
        nodes = [_node('idx', {'val': i}), _node('order', {'val': i}),
            _ref('tx', 'str', f"'ChartData'!${col}$1", [name])]
        if kind != 'bar':
            nodes.append(_node('marker', children=[_node('symbol', {'val': 'circle'}), _node('size', {'val': 4})]))
        nodes += [_ref('cat', 'str', f"'ChartData'!$A$2:$A${len(categories) + 1}", categories),
            _ref('val', 'num', f"'ChartData'!${col}$2:${col}${len(categories) + 1}", values)]
        if kind == 'line':
            nodes.append(_node('smooth', {'val': 0}))
        graph.append(_node('ser', children=nodes))
    if kind == 'bar':
        graph += [_node('gapWidth', {'val': 150}), _node('overlap', {'val': 0})]
    if kind == 'line':
        graph += [_node('marker', {'val': 1}), _node('smooth', {'val': 0})]
    graph += [_node('axId', {'val': 1001}), _node('axId', {'val': 1002})]
    plot = [_node('layout'), _node({'bar': 'barChart', 'line': 'lineChart', 'radar': 'radarChart'}[kind], children=graph),
        _axis('catAx', 1001, 1002, 'b'), _axis('valAx', 1002, 1001, 'l', unit, kind == 'radar')]
    chart = [_title(config.get('title', 'Data chart')), _node('autoTitleDeleted', {'val': 0}), _node('plotArea', children=plot),
        _node('legend', children=[_node('legendPos', {'val': 'b'}), _node('layout'), _node('overlay', {'val': 0})]),
        _node('plotVisOnly', {'val': 1}), _node('dispBlanksAs', {'val': 'gap'}), _node('showDLblsOverMax', {'val': 0})]
    return _node('chartSpace', children=[_node('lang', {'val': 'zh-CN'}), _node('chart', children=chart),
        _node('externalData', {'{' + R + '}id': 'rIdWorkbook'}, children=[_node('autoUpdate', {'val': 0})])])


def _relations(raw, expected):
    root = ET.fromstring(raw)
    if (root.tag != '{' + PKG + '}Relationships' or root.attrib
            or len(root) != len(expected) or any(n.tag != '{' + PKG + '}Relationship' for n in root)
            or len({n.get('Id') for n in root}) != len(root)
            or {tuple(sorted(n.attrib.items())) for n in root} != {tuple(sorted(n.items())) for n in expected}):
        raise ValueError('Office relationship set differs from generated safe contract')


def _embedded_book(raw, sheets):
    from .artifact_export_formats import _zip, _read_xlsx
    with _zip(raw, {'xl/workbook.xml', '_rels/.rels', 'xl/_rels/workbook.xml.rels'}) as archive:
        expected = {'docProps/app.xml', 'docProps/core.xml', 'xl/theme/theme1.xml', 'xl/styles.xml',
            '_rels/.rels', 'xl/workbook.xml', 'xl/_rels/workbook.xml.rels', '[Content_Types].xml'}
        expected |= {f'xl/worksheets/sheet{i}.xml' for i in range(1, len(sheets) + 1)}
        if set(archive.namelist()) != expected:
            raise ValueError('unexpected embedded workbook package member')
        for name in archive.namelist():
            xml = archive.read(name)
            if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise ValueError('embedded workbook XML entities')
            root = ET.fromstring(xml)
            if any(n.tag.rsplit('}', 1)[-1] in {'externalReference', 'definedName', 'oleObject',
                    'hyperlink', 'f', 'connection', 'legacyDrawing', 'drawing'} for n in root.iter()):
                raise ValueError('unsupported embedded workbook active/formula content')
        _relations(archive.read('_rels/.rels'), [
            {'Id': 'rId1', 'Type': R + '/officeDocument', 'Target': 'xl/workbook.xml'},
            {'Id': 'rId2', 'Type': PKG + '/metadata/core-properties', 'Target': 'docProps/core.xml'},
            {'Id': 'rId3', 'Type': R + '/extended-properties', 'Target': 'docProps/app.xml'}])
        rels = [{'Id': f'rId{i}', 'Type': R + '/worksheet', 'Target': f'/xl/worksheets/sheet{i}.xml'}
            for i in range(1, len(sheets) + 1)]
        rels += [{'Id': f'rId{len(sheets)+1}', 'Type': R + '/styles', 'Target': 'styles.xml'},
            {'Id': f'rId{len(sheets)+2}', 'Type': R + '/theme', 'Target': 'theme/theme1.xml'}]
        _relations(archive.read('xl/_rels/workbook.xml.rels'), rels)
        if any('macro' in n.get('ContentType', '').lower() for n in ET.fromstring(archive.read('[Content_Types].xml'))):
            raise ValueError('macro workbook content type')
    _read_xlsx(raw, sheets)


def _body_expected(row, rev, numeric, statistics):
    """Independent visible-body contract, never invokes the producer."""
    limit = 30 if rev.analysis['chart_type'] == 'radar' else 200
    paragraphs = [('Heading2', '固定数据与图表'),
        (None, f'Word chart displays the first {limit} rows; the embedded workbook retains all input rows. Missing values remain gaps.'),
        (None, '可编辑数值表显示前200行；描述统计覆盖全部数据。完整原始数据、数值、单位和来源保存在图表的嵌入工作簿。'),
        (None, '成果：' + row.id + '；版本：' + rev.id + '；v' + str(rev.version)),
        (None, '内容 SHA256：' + rev.content_sha256 + '；清单 SHA256：' + rev.manifest_sha256),
        (None, '分析配置：' + json.dumps(rev.analysis, ensure_ascii=False, sort_keys=True))]
    if rev.analysis['chart_type'] == 'radar':
        paragraphs.append((None, 'Radar axes use per-column min/max normalization (constant columns: 0.5); incomplete rows are omitted. Original values and units remain in the embedded workbook.'))
    paragraphs += [(None, '单位：' + '；'.join(name + '=' + rev.analysis.get('units', {}).get(name, 'unspecified')
        for name in rev.analysis['numeric_columns'])), ('Heading2', '数值数据（前200行）')]
    text_rows = lambda rows: [[str(v) if v is not None else '' for v in row] for row in rows]
    return paragraphs, text_rows(numeric[:201]), text_rows(statistics)


def verify(payload, row, rev, title, numeric, statistics, sheets):
    from .artifact_export_formats import _zip
    from .artifact_export_content_checks import _actual_docx, _expected_docx
    categories, series, unit, chart_rows = _expected_data(rev, numeric, statistics)
    with _zip(payload, {'word/document.xml', CHART_PATH, BOOK_PATH, 'word/charts/_rels/chart1.xml.rels'}) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    # Only the single generated chart/workbook pair may extend the ordinary
    # python-docx package. No external links, macro, OLE, altChunk or extras.
    permitted = {'[Content_Types].xml', '_rels/.rels', 'docProps/core.xml', 'docProps/app.xml',
        'word/document.xml', 'word/_rels/document.xml.rels', 'word/styles.xml', 'word/stylesWithEffects.xml',
        'word/settings.xml', 'word/webSettings.xml', 'word/fontTable.xml', 'word/theme/theme1.xml',
        'customXml/item1.xml', 'customXml/_rels/item1.xml.rels', 'customXml/itemProps1.xml',
        'word/numbering.xml', 'docProps/thumbnail.jpeg', CHART_PATH, BOOK_PATH, 'word/charts/_rels/chart1.xml.rels'}
    if set(members) != permitted:
        raise ValueError('unsupported Word chart package members')
    for name, raw in members.items():
        if name.endswith(('.xml', '.rels')):
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('Office XML entities')
            root = ET.fromstring(raw)
            if name.endswith('.rels') and any(n.get('TargetMode') == 'External' for n in root):
                raise ValueError('external Office relationship')
    _relations(members['word/charts/_rels/chart1.xml.rels'], [{'Id': 'rIdWorkbook', 'Type': R + '/package',
        'Target': '../embeddings/ScholarFlow-chart-data.xlsx'}])
    _relations(members['_rels/.rels'], [
        {'Id': 'rId3', 'Type': PKG + '/metadata/core-properties', 'Target': 'docProps/core.xml'},
        {'Id': 'rId4', 'Type': R + '/extended-properties', 'Target': 'docProps/app.xml'},
        {'Id': 'rId1', 'Type': R + '/officeDocument', 'Target': 'word/document.xml'},
        {'Id': 'rId2', 'Type': PKG + '/metadata/thumbnail', 'Target': 'docProps/thumbnail.jpeg'}])
    _relations(members['customXml/_rels/item1.xml.rels'], [{'Id': 'rId1', 'Type': R + '/customXmlProps', 'Target': 'itemProps1.xml'}])
    _relations(members['word/_rels/document.xml.rels'], [
        {'Id': 'rId3', 'Type': R + '/styles', 'Target': 'styles.xml'},
        {'Id': 'rId4', 'Type': 'http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects', 'Target': 'stylesWithEffects.xml'},
        {'Id': 'rId5', 'Type': R + '/settings', 'Target': 'settings.xml'},
        {'Id': 'rId6', 'Type': R + '/webSettings', 'Target': 'webSettings.xml'},
        {'Id': 'rId7', 'Type': R + '/fontTable', 'Target': 'fontTable.xml'},
        {'Id': 'rId8', 'Type': R + '/theme', 'Target': 'theme/theme1.xml'},
        {'Id': 'rId1', 'Type': R + '/customXml', 'Target': '../customXml/item1.xml'},
        {'Id': 'rId2', 'Type': R + '/numbering', 'Target': 'numbering.xml'},
        {'Id': 'rIdSFChart', 'Type': R + '/chart', 'Target': 'charts/chart1.xml'}])
    types = ET.fromstring(members['[Content_Types].xml'])
    if any('macro' in n.get('ContentType', '').lower() or 'oleobject' in n.get('ContentType', '').lower() for n in types):
        raise ValueError('active Office content type')
    for path, mime in [(CHART_PATH, 'application/vnd.openxmlformats-officedocument.drawingml.chart+xml'),
            (BOOK_PATH, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')]:
        entries = [n for n in types if n.get('PartName') == '/' + path]
        if len(entries) != 1 or entries[0].get('ContentType') != mime:
            raise ValueError('chart content type mismatch')
    if _actual(ET.fromstring(members[CHART_PATH])) != _expected_chart(rev.analysis, categories, series, unit):
        raise ValueError('chart type/axes/series/cache/formula differs from fixed CSV/config')
    _embedded_book(members[BOOK_PATH], {**sheets, 'ChartData': chart_rows})
    # Validate the complete visible body and frozen references, allowing only
    # the exact chart drawing in its own final paragraph.
    document = ET.fromstring(members['word/document.xml'])
    drawings = document.findall('.//{' + W + '}drawing')
    if len(drawings) != 1:
        raise ValueError('expected exactly one editable chart drawing')
    drawing = drawings[0]
    expected_drawing = _node('drawing', namespace=W, children=[_node('inline', namespace=WP,
        attributes={'distT': 0, 'distB': 0, 'distL': 0, 'distR': 0}, children=[
            _node('extent', namespace=WP, attributes={'cx': 5900000, 'cy': 3500000}),
            _node('docPr', namespace=WP, attributes={'id': 1, 'name': 'ScholarFlow editable chart'}),
            _node('cNvGraphicFramePr', namespace=WP), _node('graphic', namespace=A, children=[
                _node('graphicData', namespace=A, attributes={'uri': C}, children=[_node('chart',
                    attributes={'{' + R + '}id': 'rIdSFChart'})])])])])
    if _actual(drawing) != expected_drawing:
        raise ValueError('unsupported chart drawing')
    for parent in document.iter():
        if drawing in list(parent):
            parent.remove(drawing)
            break
    members['word/document.xml'] = ET.tostring(document, encoding='utf-8')
    stripped = io.BytesIO()
    with zipfile.ZipFile(stripped, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    expected = _expected_docx(title, '', rev.evidence)
    paragraphs, values, stats = _body_expected(row, rev, numeric, statistics)
    expected += [{'paragraph': {'style': style, 'parts': [text] if text else []}} for style, text in paragraphs]
    def table(rows):
        return {'table': [[[{'style': None, 'parts': [value] if value else []}] for value in cells] for cells in rows]}
    expected += [table(values), {'paragraph': {'style': 'Heading2', 'parts': ['描述统计（全部数据）']}}, table(stats),
        {'paragraph': {'style': 'Heading2', 'parts': ['可编辑图表']}}, {'paragraph': {'style': None, 'parts': []}}]
    if _actual_docx(stripped.getvalue()) != expected:
        raise ValueError('analysis Word body/table/source differs from fixed inputs')
    return {'native_editable_chart': True, 'chart_type': rev.analysis['chart_type'], 'chart_series': len(series),
        'chart_categories': len(categories), 'embedded_workbook_checked': True, 'missing_policy': 'gap',
        'fixed_input_total_rows': len(numeric) - 1, 'table_visible_rows': min(200, len(numeric) - 1),
        'native_open': 'not_assessed', 'render': 'not_assessed'}
