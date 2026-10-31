import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderMarkdown, renderAnswer } from '../src/markdown.ts'
import { tableText } from '../src/answerExport.ts'

test('renders structured research answers as headings, tables, lists and code', () => {
  const html = renderMarkdown('## Results\n\n- **First**\n- Second\n\n| Method | Score |\n| --- | --- |\n| A | 10 |\n\n```python\nprint("ok")\n```')
  for (const tag of ['h2', 'ul', 'strong', 'table', 'th', 'td', 'pre', 'code']) {
    assert.match(html, new RegExp(`<${tag}[ >]`))
  }
})

test('citations link only to available evidence and remain literal inside code or links', () => {
  const html = renderMarkdown('Evidence [^e1], missing [^e2].\n\n`[^e1]`\n\n```text\n[^e1]\n```\n\n[Linked [^e1]](https://example.com)', ['e1'])
  assert.equal((html.match(/data-evidence-id=/g) || []).length, 1)
  assert.match(html, /data-evidence-id="e1"/)
  assert.match(html, /\[\^e2\]/)
  assert.match(html, /<code>\[\^e1\]<\/code>/)
  assert.doesNotMatch(html, /<a[^>]*>[^<]*<button/)
})

test('HTML and executable links cannot enter answer DOM', () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)">\n\n<script>alert(1)</script>\n\n[bad](javascript:alert%281%29)\n\n[local](file:///secret)')
  assert.doesNotMatch(html, /<(script|img)[ >]/)
  assert.doesNotMatch(html, /href="(?:javascript|file):/)
  assert.match(html, /&lt;script&gt;/)
})

test('external links isolate their browsing context', () => {
  const html = renderMarkdown('[Source](https://example.com)')
  assert.match(html, /target="_blank"/)
  assert.match(html, /rel="noopener noreferrer"/)
})

test('code controls copy parsed text without highlighting markup and nested blocks keep their own indexes', () => {
  const result = renderAnswer('```python\ndef hello():\n    return "<script>[^e1]</script>"\n```\n\n> ```unknown-language\n> <img onerror=alert(1)>\n> ```\n\n    indented = 1\n', ['e1'])
  assert.deepEqual(result.codeBlocks, ['def hello():\n    return "<script>[^e1]</script>"\n', '<img onerror=alert(1)>\n', 'indented = 1\n'])
  assert.match(result.html, /class="hljs-keyword"/)
  assert.deepEqual([...result.html.matchAll(/data-code-actions="(\d+)"/g)].map(m => m[1]), ['0', '1', '2'])
  assert.doesNotMatch(result.html, /<(?:script|img)\b/)
  assert.doesNotMatch(result.html, /data-evidence-id=/)
  const hostile = renderAnswer('```"><img\n<script>alert(1)</script>\n```')
  assert.doesNotMatch(hostile.html, /<(?:script|img)\b/)
  const long = renderAnswer('```python\n' + 'x'.repeat(100001) + '\n```')
  assert.equal(long.codeBlocks[0].length, 100002)
  assert.doesNotMatch(long.html, /class="hljs-\w+"/)
})

test('tables export visible cell information with formatting, links, formulas and evidence preserved as text', () => {
  const result = renderAnswer('| 名称 | 值 |\n| --- | --- |\n| **实验 A** | [来源](https://example.com) [^e1] |\n| `代码` | $x^2$ |\n\n| 单元格 |\n| --- |\n| ![图注](https://example.com/a.png) |', ['e1'])
  assert.deepEqual(result.tables, [[['名称', '值'], ['实验 A', '来源 [^e1]'], ['代码', 'x^2']], [['单元格'], ['图注']]])
  assert.deepEqual([...result.html.matchAll(/data-table-actions="(\d+)"/g)].map(m => m[1]), ['0', '1'])
  assert.equal(tableText([['a,b', 'say "yes"', 'two\nlines', 'a\tb']], 'csv'), '"a,b","say ""yes""","two\nlines",a\tb')
  assert.equal(tableText([['a,b', 'a\tb', 'say "yes"']], 'tsv'), 'a,b\t"a\tb"\t"say ""yes"""')
  assert.equal(tableText([['=1+1', '+SUM(A1)', ' @SUM(A1)', '-cmd', '-1.5', '0', '普通中文']], 'csv'), "'=1+1,'+SUM(A1),' @SUM(A1),'-cmd,-1.5,0,普通中文")
  assert.equal(tableText([['=1+1']], 'tsv'), '=1+1')
})

test('math supports dollar and bracket delimiters while code and prices remain literal', () => {
  const html = renderMarkdown('行内 $x^2$ 和 \\(y_1\\)。\n\n$$\n\\frac{1}{2}\n$$\n\n\\[z^2\\]\n\n`$literal$` and $5 and $10')
  assert.equal((html.match(/class="katex"/g) || []).length, 4)
  assert.match(html, /<code>\$literal\$<\/code>/)
  assert.match(html, /\$5 and \$10/)
  assert.match(html, /<math\b/)
})

test('invalid and untrusted TeX does not inject links, images, HTML or unbounded macros', () => {
  const html = renderMarkdown(String.raw`$\unknown{<img src=x onerror=alert(1)>}$

$\href{javascript:alert(1)}{click}$

$\includegraphics{https://example.com/track}$

$\htmlClass{injected}{text}$

$\def\a{\a}\a$`)
  assert.doesNotMatch(html, /<(?:img|script)\b|href="javascript:|class="injected"/)
  assert.match(html, /&lt;img/)
  assert.match(html, /katex-error/)
})
