import assert from 'node:assert/strict'
import test from 'node:test'
import {
  comparisonLocationLabel,
  comparisonEvidenceRoute,
  comparisonCsv,
} from '../src/comparisonEvidence.ts'

const base = {
  id: 'e1',
  chunk_id: 'chunk',
  paper_id: 'paper',
  paper_title: 'Title',
  page: 0,
  bbox: null,
  quote: 'First line, "quoted"\nSecond line',
  score: 1,
  revision_id: 'fixed-v1',
  locator_id: 'line-205',
  source_id: 'source',
  source_version: 1,
  source_media_type: 'text/markdown',
  source_sha256: 'abc123',
  locator_kind: 'text_lines',
  source_location: { line_start: 205, line_end: 206 },
  location_status: 'fixed_source',
  verification_status: 'unverified',
}

test('fixed source target uses result scope and exact revision/locator, never PDF fallback', () => {
  assert.deepEqual(comparisonEvidenceRoute(base, 'original-scope'), {
    path: '/sources/fixed-v1',
    query: { conversation: 'original-scope', locator: 'line-205' },
  })
  assert.equal(comparisonEvidenceRoute(base, ''), null)
  assert.equal(comparisonEvidenceRoute({ ...base, locator_id: undefined, page: 9 }, 'scope'), null)
  assert.equal(comparisonEvidenceRoute({ ...base, revision_id: undefined }, 'scope'), null)
  assert.equal(comparisonEvidenceRoute({ paper_id: 'paper', page: 0 }), null)
  assert.deepEqual(comparisonEvidenceRoute({ paper_id: 'pdf', page: 2, bbox: [1, 2, 3, 4] }), {
    path: '/reader/pdf',
    query: { page: '2', bbox: '1,2,3,4' },
  })
})

test('labels preserve text, Word, sheet positions rather than invented pages', () => {
  assert.equal(comparisonLocationLabel(base), '第 205–206 行')
  assert.equal(
    comparisonLocationLabel({
      ...base,
      locator_kind: 'word_paragraphs',
      source_location: { paragraph_start: 12, paragraph_end: 12 },
    }),
    '段落 12'
  )
  assert.equal(
    comparisonLocationLabel({
      ...base,
      locator_kind: 'sheet_cells',
      source_location: { sheet: '结果', cell_range: 'B2:C2' },
    }),
    '结果 · B2:C2'
  )
  assert.equal(comparisonLocationLabel({ page: 4 }), '第 4 页')
  assert.equal(comparisonLocationLabel({ page: 0 }), '定位不可用')
})

test('CSV retains all evidence identities, exact quoted text, status, empty cells and local-link limitation', () => {
  const data = {
    conversation_id: 'original-scope',
    papers: [
      { id: 'paper', title: 'Title, "A"' },
      { id: 'empty', title: '=HYPERLINK("bad")' },
    ],
    rows: [
      {
        key: 'method',
        label: 'Method',
        cells: {
          paper: { status: 'unverified', evidence: [base], quote: base.quote },
          empty: { status: 'not_found', evidence: [], quote: '' },
        },
      },
    ],
  }
  const csv = comparisonCsv(data)
  assert.ok(csv.startsWith('\ufeff'))
  for (const value of [
    'fixed-v1',
    'line-205',
    'abc123',
    'text/markdown',
    'unverified',
    '未检索到',
    '本机引用路径（需原数据）',
    'conversation=original-scope',
    'locator=line-205',
  ])
    assert.ok(csv.includes(value), value)
  assert.ok(csv.includes('"First line, ""quoted""\nSecond line"'))
  assert.ok(csv.includes('"Title, ""A"""'))
  assert.ok(csv.includes('"\'=HYPERLINK('))
  assert.ok(csv.includes('"line_start""'))
})
