"""Native editable Word charts, backed by a plain embedded XLSX workbook.

Creation only: saved exports are checked by word_chart_checks without calling
this renderer or executing the exported analysis program.
"""
from __future__ import annotations

import io
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
CHART_REL = 'rIdSFChart'
BOOK_REL = 'rIdWorkbook'
POLICY = 'Word chart displays the first {limit} rows; the embedded workbook retains all input rows. Missing values remain gaps.'
RADAR_POLICY = 'Radar axes use per-column min/max normalization (constant columns: 0.5); incomplete rows are omitted. Original values and units remain in the embedded workbook.'


def chart_data(config, numeric, statistics):
    """Map the existing preview contract to worksheet categories and series."""
    names = config['numeric_columns']
    limit = 30 if config['chart_type'] == 'radar' else 200
    visible = numeric[1:limit + 1]
    if config['chart_type'] == 'radar':
        stats = {row[0]: row for row in statistics[1:]}
        complete = [row for row in visible if all(value is not None for value in row[1:])]
        series = []
        for index, row in enumerate(complete):
            values = []
            for ni, name in enumerate(names, 1):
                low, high = stats[name][3], stats[name][5]
                denominator = high - low
                if not math.isfinite(denominator):
                    raise ValueError('Radar range exceeds finite chart precision')
                values.append((row[ni] - low) / denominator if denominator else 0.5)
            series.append((row[0], values))
        categories = list(names)
        unit = 'normalized [0,1]'
    else:
        categories = [row[0] for row in visible]
        series = [(name, [row[index] for row in visible]) for index, name in enumerate(names, 1)]
        unit = config.get('units', {}).get(names[0], 'unspecified')
    # An empty radar preview is valid: keep the axes and data sheet, no invented
    # zero-valued series. Word chart caches and workbook agree on zero series.
    rows = [['category', *[name for name, _ in series]]]
    rows += [[category, *[values[index] for _, values in series]] for index, category in enumerate(categories)]
    return {'categories': categories, 'series': series, 'rows': rows, 'limit': limit, 'unit': unit}


def _el(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, '{' + C + '}' + tag, {key: str(value) for key, value in attrs.items()})
    node.text = text
    return node


def _title(parent, text):
    title = _el(parent, 'title')
    rich = _el(_el(title, 'tx'), 'rich')
    ET.SubElement(rich, '{' + A + '}bodyPr')
    ET.SubElement(rich, '{' + A + '}lstStyle')
    paragraph = ET.SubElement(rich, '{' + A + '}p')
    run = ET.SubElement(paragraph, '{' + A + '}r')
    ET.SubElement(run, '{' + A + '}t').text = text
    _el(title, 'overlay', val=0)


def _ref(parent, kind, formula, values):
    ref = _el(parent, kind + 'Ref')
    _el(ref, 'f', formula)
    cache = _el(ref, kind + 'Cache')
    if kind == 'num':
        _el(cache, 'formatCode', 'General')
    _el(cache, 'ptCount', val=len(values))
    for index, value in enumerate(values):
        if value is not None:
            _el(_el(cache, 'pt', idx=index), 'v', str(value))


def _axis(parent, kind, axis_id, cross_id, position, title=None, normalized=False):
    axis = _el(parent, kind)
    _el(axis, 'axId', val=axis_id)
    scaling = _el(axis, 'scaling')
    _el(scaling, 'orientation', val='minMax')
    if normalized:
        _el(scaling, 'max', val=1)
        _el(scaling, 'min', val=0)
    _el(axis, 'delete', val=0)
    _el(axis, 'axPos', val=position)
    if kind == 'valAx':
        _el(axis, 'majorGridlines')
        if title:
            _title(axis, title)
        _el(axis, 'numFmt', formatCode='General', sourceLinked=0)
    _el(axis, 'majorTickMark', val='none')
    _el(axis, 'minorTickMark', val='none')
    _el(axis, 'tickLblPos', val='nextTo')
    _el(axis, 'crossAx', val=cross_id)
    _el(axis, 'crosses', val='autoZero')
    if kind == 'catAx':
        _el(axis, 'auto', val=1)
        _el(axis, 'lblAlgn', val='ctr')
        _el(axis, 'lblOffset', val=100)
    else:
        _el(axis, 'crossBetween', val='between')


def chart_xml(config, data):
    from openpyxl.utils import get_column_letter
    root = ET.Element('{' + C + '}chartSpace')
    _el(root, 'lang', val='zh-CN')
    chart = _el(root, 'chart')
    _title(chart, config.get('title', 'Data chart'))
    _el(chart, 'autoTitleDeleted', val=0)
    plot = _el(chart, 'plotArea')
    _el(plot, 'layout')
    kind = config['chart_type']
    graph = _el(plot, {'bar': 'barChart', 'line': 'lineChart', 'radar': 'radarChart'}[kind])
    if kind == 'bar':
        _el(graph, 'barDir', val='col')
        _el(graph, 'grouping', val='clustered')
        _el(graph, 'varyColors', val=0)
    elif kind == 'line':
        _el(graph, 'grouping', val='standard')
        _el(graph, 'varyColors', val=0)
    else:
        _el(graph, 'radarStyle', val='marker')
        _el(graph, 'varyColors', val=0)
    count = len(data['categories'])
    for index, (name, values) in enumerate(data['series']):
        column = get_column_letter(index + 2)
        series = _el(graph, 'ser')
        _el(series, 'idx', val=index)
        _el(series, 'order', val=index)
        _ref(_el(series, 'tx'), 'str', f"'ChartData'!${column}$1", [name])
        if kind in ('line', 'radar'):
            marker = _el(series, 'marker')
            _el(marker, 'symbol', val='circle')
            _el(marker, 'size', val=4)
        _ref(_el(series, 'cat'), 'str', f"'ChartData'!$A$2:$A${count + 1}", data['categories'])
        _ref(_el(series, 'val'), 'num', f"'ChartData'!${column}$2:${column}${count + 1}", values)
        if kind == 'line':
            _el(series, 'smooth', val=0)
    if kind == 'bar':
        _el(graph, 'gapWidth', val=150)
        _el(graph, 'overlap', val=0)
    if kind == 'line':
        _el(graph, 'marker', val=1)
        _el(graph, 'smooth', val=0)
    _el(graph, 'axId', val=1001)
    _el(graph, 'axId', val=1002)
    _axis(plot, 'catAx', 1001, 1002, 'b')
    _axis(plot, 'valAx', 1002, 1001, 'l', data['unit'], kind == 'radar')
    legend = _el(chart, 'legend')
    _el(legend, 'legendPos', val='b')
    _el(legend, 'layout')
    _el(legend, 'overlay', val=0)
    _el(chart, 'plotVisOnly', val=1)
    _el(chart, 'dispBlanksAs', val='gap')
    _el(chart, 'showDLblsOverMax', val=0)
    external = _el(root, 'externalData', **{'{' + R + '}id': BOOK_REL})
    _el(external, 'autoUpdate', val=0)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def body_contract(row, rev, title, numeric, statistics):
    """Plain text/table blocks after the normal title and frozen references."""
    limit = 30 if rev.analysis['chart_type'] == 'radar' else 200
    paragraphs = [
        ('Heading2', '固定数据与图表'),
        (None, POLICY.format(limit=limit)),
        (None, '可编辑数值表显示前200行；描述统计覆盖全部数据。完整原始数据、数值、单位和来源保存在图表的嵌入工作簿。'),
        (None, '成果：' + row.id + '；版本：' + rev.id + '；v' + str(rev.version)),
        (None, '内容 SHA256：' + rev.content_sha256 + '；清单 SHA256：' + rev.manifest_sha256),
        (None, '分析配置：' + __import__('json').dumps(rev.analysis, ensure_ascii=False, sort_keys=True)),
    ]
    if rev.analysis['chart_type'] == 'radar':
        paragraphs.append((None, RADAR_POLICY))
    paragraphs += [(None, '单位：' + '；'.join(name + '=' + rev.analysis.get('units', {}).get(name, 'unspecified')
                     for name in rev.analysis['numeric_columns'])),
        ('Heading2', '数值数据（前200行）')]
    def text_rows(rows):
        return [[str(value) if value is not None else '' for value in values] for values in rows]
    return paragraphs, text_rows(numeric[:201]), text_rows(statistics)


def build_analysis_docx(row, rev, title, raw, numeric, statistics, sheets, papers):
    from docx import Document
    from docx.oxml import parse_xml
    from .exporter import build_docx
    from .artifact_export_formats import _write_xlsx
    data = chart_data(rev.analysis, numeric, statistics)
    document = Document(io.BytesIO(build_docx(title, '', papers)))
    paragraphs, values, stats = body_contract(row, rev, title, numeric, statistics)
    for style, text in paragraphs:
        document.add_paragraph(text, style='Heading 2' if style == 'Heading2' else style)
    for index, rows in enumerate((values, stats)):
        if index:
            document.add_heading('描述统计（全部数据）', level=2)
        table = document.add_table(rows=0, cols=len(rows[0]))
        table.style = 'Table Grid'
        for ri, cells in enumerate(rows):
            for cell, value in zip(table.add_row().cells, cells):
                cell.text = value
                if ri == 0:
                    for run in cell.paragraphs[0].runs:
                        run.bold = True
    document.add_heading('可编辑图表', level=2)
    drawing = f'''<w:r xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:c="{C}" xmlns:r="{R}"><w:drawing>
      <wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="5900000" cy="3500000"/>
      <wp:docPr id="1" name="ScholarFlow editable chart"/><wp:cNvGraphicFramePr/>
      <a:graphic><a:graphicData uri="{C}"><c:chart r:id="{CHART_REL}"/></a:graphicData></a:graphic>
      </wp:inline></w:drawing></w:r>'''
    document.add_paragraph()._p.append(parse_xml(drawing))
    output = io.BytesIO()
    document.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    rels = ET.fromstring(members['word/_rels/document.xml.rels'])
    ET.SubElement(rels, '{' + PKG + '}Relationship', Id=CHART_REL, Type=R + '/chart', Target='charts/chart1.xml')
    members['word/_rels/document.xml.rels'] = ET.tostring(rels, encoding='utf-8', xml_declaration=True)
    chart_rels = ET.Element('{' + PKG + '}Relationships')
    ET.SubElement(chart_rels, '{' + PKG + '}Relationship', Id=BOOK_REL, Type=R + '/package',
        Target='../embeddings/ScholarFlow-chart-data.xlsx')
    members['word/charts/_rels/chart1.xml.rels'] = ET.tostring(chart_rels, encoding='utf-8', xml_declaration=True)
    types = ET.fromstring(members['[Content_Types].xml'])
    ET.SubElement(types, '{' + CT + '}Override', PartName='/' + CHART_PATH,
        ContentType='application/vnd.openxmlformats-officedocument.drawingml.chart+xml')
    ET.SubElement(types, '{' + CT + '}Override', PartName='/' + BOOK_PATH,
        ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    members['[Content_Types].xml'] = ET.tostring(types, encoding='utf-8', xml_declaration=True)
    members[CHART_PATH] = chart_xml(rev.analysis, data)
    members[BOOK_PATH] = _write_xlsx({**sheets, 'ChartData': data['rows']})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()
