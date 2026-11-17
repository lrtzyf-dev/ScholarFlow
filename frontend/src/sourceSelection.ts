export const MAX_SOURCE_ATTACHMENTS = 16
export function normalizeSourceIds(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [
    ...new Set(
      value.filter((id): id is string => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id))
    ),
  ].slice(0, MAX_SOURCE_ATTACHMENTS)
}
export function sourceReaderLocation(
  revisionId: string,
  conversationId: string,
  locatorId?: string | null
) {
  return {
    path: `/sources/${encodeURIComponent(revisionId)}`,
    query: { conversation: conversationId, locator: locatorId || undefined },
  }
}
export function evidenceSourceKey(evidence: { revision_id?: string; paper_id?: string }): string {
  return evidence.revision_id
    ? `source:${evidence.revision_id}`
    : `paper:${evidence.paper_id || ''}`
}
export function sourceTypeLabel(mediaType: string): string {
  if (mediaType === 'application/pdf') return 'PDF'
  if (mediaType.includes('wordprocessingml')) return 'Word'
  if (mediaType.includes('spreadsheetml')) return 'Excel'
  if (mediaType === 'text/csv') return 'CSV'
  if (mediaType === 'text/markdown') return 'Markdown'
  return '文本'
}

export function librarySourceTypeLabel(source: {
  source_revision_id?: string | null
  source_media_type?: string | null
  document_format?: string | null
}): string {
  if (source.source_media_type === 'text/markdown') return 'MD'
  if (source.source_media_type) return sourceTypeLabel(source.source_media_type)
  if (!source.source_revision_id) return 'PDF'
  switch (source.document_format) {
    case 'pdf':
      return 'PDF'
    case 'word':
      return 'Word'
    case 'excel':
      return 'Excel'
    case 'csv':
      return 'CSV'
    case 'text':
      return '文本'
    default:
      return '资料'
  }
}

export function cellRangesOverlap(a: string, b: string): boolean {
  function bounds(value: string) {
    const parts = value
      .toUpperCase()
      .split(':')
      .map((cell) => {
        const match = /^([A-Z]{1,3})([1-9]\d{0,6})$/.exec(cell)
        return match
          ? [
              match[1].split('').reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0),
              Number(match[2]),
            ]
          : null
      })
    if (!parts[0] || parts.length > 2 || parts.some((part) => !part)) return null
    const end = parts[1] || parts[0]
    return end[0] >= parts[0][0] && end[1] >= parts[0][1] ? [parts[0], end] : null
  }
  const x = bounds(a),
    y = bounds(b)
  return Boolean(
    x && y && x[0][0] <= y[1][0] && x[1][0] >= y[0][0] && x[0][1] <= y[1][1] && x[1][1] >= y[0][1]
  )
}
