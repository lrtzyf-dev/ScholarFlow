import assert from 'node:assert/strict'
import test from 'node:test'
import {
  normalizeSourceIds,
  sourceReaderLocation,
  evidenceSourceKey,
  cellRangesOverlap,
  sourceTypeLabel,
  librarySourceTypeLabel,
} from '../src/sourceSelection.ts'

test('source selection is unique, bounded, and contains fixed revision IDs only', () => {
  const ids = Array.from({ length: 19 }, (_, n) => n.toString(16).padStart(32, '0'))
  assert.deepEqual(normalizeSourceIds(null), [])
  assert.deepEqual(normalizeSourceIds(['bad', ids[0], ids[0], 3, ...ids]), ids.slice(0, 16))
})
test('reader keeps source version, conversation scope, and locator separate', () => {
  const route = sourceReaderLocation('a'.repeat(32), 'conversation-one', 'locator-two')
  assert.equal(route.path, '/sources/' + 'a'.repeat(32))
  assert.deepEqual(route.query, { conversation: 'conversation-one', locator: 'locator-two' })
  assert.equal(evidenceSourceKey({ revision_id: 'one', paper_id: '' }), 'source:one')
  assert.notEqual(
    evidenceSourceKey({ revision_id: 'one' }),
    evidenceSourceKey({ revision_id: 'two' })
  )
  assert.equal(evidenceSourceKey({ paper_id: 'p1' }), 'paper:p1')
})
test('spreadsheet citation intersection handles multi-letter columns and rejects invalid ranges', () => {
  assert.equal(cellRangesOverlap('AA2:AC10', 'AB5'), true)
  assert.equal(cellRangesOverlap('A1:C9', 'B4:D12'), true)
  assert.equal(cellRangesOverlap('A1:C9', 'D4:E12'), false)
  assert.equal(cellRangesOverlap('A1', 'A10'), false)
  for (const invalid of ['A0', 'A-1', 'Z1:A1', 'A3:A1', 'A1:B2:C3', '', 'A1<script>'])
    assert.equal(cellRangesOverlap(invalid, 'A1'), false)
})
test('source labels describe actual formats', () => {
  assert.equal(
    sourceTypeLabel('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'Word'
  )
  assert.equal(
    sourceTypeLabel('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    'Excel'
  )
  assert.equal(sourceTypeLabel('text/csv'), 'CSV')
  assert.equal(sourceTypeLabel('application/pdf'), 'PDF')
  assert.equal(sourceTypeLabel('text/markdown'), 'Markdown')
  assert.equal(sourceTypeLabel('text/plain'), '文本')
})

test('library format uses retained media type and never calls a Markdown source PDF', () => {
  assert.equal(
    librarySourceTypeLabel({
      source_revision_id: 'md-v1',
      source_media_type: 'text/markdown',
      document_format: 'text',
    }),
    'MD'
  )
  assert.equal(librarySourceTypeLabel({}), 'PDF')
  assert.equal(
    librarySourceTypeLabel({ source_revision_id: 'legacy', document_format: 'word' }),
    'Word'
  )
  assert.equal(
    librarySourceTypeLabel({ source_revision_id: 'legacy', document_format: 'text' }),
    '文本'
  )
  assert.equal(librarySourceTypeLabel({ source_revision_id: 'unknown' }), '资料')
  assert.equal(
    librarySourceTypeLabel({ source_revision_id: 'unknown', document_format: '__proto__' }),
    '资料'
  )
})
