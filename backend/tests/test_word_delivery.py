import io
import zipfile
import pytest
from xml.etree import ElementTree as ET

from docx import Document

from app.exporter import build_docx, build_markdown

SAMPLE = r'''## 可编辑结果

这是**加粗文字**以及 $x_i^2 + \frac{a}{b}$，来源 [e1]。

| 方法 | 准确率 |
| --- | --- |
| A | 81% |
| B | **87%** |

$$
\sqrt{x^2+y^2} = \sum_{i=1}^{n} \frac{i}{n}
$$

$$\begin{pmatrix}a & b \\ c & d\end{pmatrix}$$

- 第一项
- 第二项

```python
# literal code: $x_i$
print("table")
```
'''


def test_docx_has_actual_tables_math_runs_and_fixed_source():
    sources = [{"title": "合成研究笔记", "revision_id": "fixed-revision-123", "locator_id": "fixed-locator-456",
                "quote": "原文：模型 B 为 87%。", "verification": "source_frozen_semantics_unverified"}]
    raw = build_docx("研究报告", SAMPLE, sources)
    doc = Document(io.BytesIO(raw))
    assert len(doc.tables) == 1
    assert [[c.text for c in row.cells] for row in doc.tables[0].rows] == [["方法", "准确率"], ["A", "81%"], ["B", "87%"]]
    paragraphs = '\n'.join(p.text for p in doc.paragraphs)
    assert "fixed-revision-123" in paragraphs and "fixed-locator-456" in paragraphs
    assert "原文：模型 B 为 87%" in paragraphs
    assert '# literal code: $x_i$' in paragraphs
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        xml = archive.read('word/document.xml')
    root = ET.fromstring(xml)
    ns = {'m': 'http://schemas.openxmlformats.org/officeDocument/2006/math',
          'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    assert len(root.findall('.//m:oMath', ns)) == 3
    assert root.findall('.//m:f', ns)
    assert root.findall('.//m:rad', ns)
    assert root.findall('.//m:m', ns)
    assert root.findall('.//w:tblHeader', ns)
    assert '公式原文（未转换）' not in paragraphs
    assert 'SFMATH' not in paragraphs
    markdown = build_markdown('研究报告', SAMPLE, sources)
    assert 'fixed-revision-123' in markdown and 'fixed-locator-456' in markdown


def test_unsupported_math_is_explicit_and_original_not_lost():
    raw = build_docx('公式', '$\\phantom{x}$', [])
    assert '公式原文（未转换）' in '\n'.join(p.text for p in Document(io.BytesIO(raw)).paragraphs)


def test_sparse_evidence_ids_keep_separate_locators_and_word_bookmarks():
    sources = [{"id": identifier, "title": "同一论文", "revision_id": "same-version", "locator_id": locator,
                "quote": quote, "source_fingerprint": "source-hash", "verification": "source_frozen_semantics_unverified"}
               for identifier, locator, quote in [("e7", "line-1", "第一处原句"), ("e12", "line-8", "另一处原句")]]
    answer = '第一处 [^e7]，另一处 [^e12]。\n\n`[^e7]` 是代码字面。'
    markdown = build_markdown('非连续引用', answer, sources)
    assert '[^e7]: ' in markdown and '[^e12]: ' in markdown
    assert 'line-1' in markdown and 'line-8' in markdown
    with zipfile.ZipFile(io.BytesIO(build_docx('非连续引用', answer, sources))) as package:
        root = ET.fromstring(package.read('word/document.xml'))
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    anchors = [link.get('{' + ns['w'] + '}anchor') for link in root.findall('.//w:hyperlink', ns)]
    bookmarks = [item.get('{' + ns['w'] + '}name') for item in root.findall('.//w:bookmarkStart', ns)]
    assert anchors == bookmarks == ['SFRef_1', 'SFRef_2']
    assert '[e7]' in ''.join(root.itertext()) and '[e12]' in ''.join(root.itertext())
    assert '`' not in ''.join(root.itertext()) and '[^e7]' in ''.join(root.itertext())


@pytest.mark.parametrize('code', ['    print("$x+y$")\n', '```python\nprint("$x+y$")\n', '```python\nprint("$x+y$")\n````\n'])
def test_all_markdown_code_forms_keep_literal_formula_without_placeholder(code):
    raw = build_docx('代码字面保真', code, [])
    text = '\n'.join(p.text for p in Document(io.BytesIO(raw)).paragraphs)
    assert 'print("$x+y$")' in text
    assert 'SFMATH' not in text
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert b'<m:oMath' not in archive.read('word/document.xml')
