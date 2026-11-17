import type { ComparisonData, ComparisonEvidence } from './api'

export function comparisonLocationLabel(evidence: ComparisonEvidence): string {
  const location = evidence.source_location || {}
  if (evidence.locator_kind === 'sheet_cells')
    return [location.sheet, location.cell_range].filter(Boolean).join(' · ') || '单元格位置未提供'
  if (evidence.locator_kind === 'word_paragraphs')
    return `段落 ${location.paragraph_start}${location.paragraph_end !== location.paragraph_start ? `–${location.paragraph_end}` : ''}`
  if (evidence.locator_kind === 'word_block')
    return `${location.part_label || 'Word 内容部件'}${location.table_row ? ` · 逻辑行 ${location.table_row}，列 ${location.table_column}` : ''}（固定结构位置）`
  if (evidence.locator_kind === 'text_lines')
    return `第 ${location.line_start}${location.line_end !== location.line_start ? `–${location.line_end}` : ''} 行`
  return evidence.page > 0 ? `第 ${evidence.page} 页` : '定位不可用'
}

export function comparisonEvidenceRoute(
  evidence: ComparisonEvidence,
  conversationId?: string | null
) {
  if (evidence.revision_id || evidence.locator_id) {
    if (!evidence.revision_id || !evidence.locator_id || !conversationId) return null
    return {
      path: `/sources/${encodeURIComponent(evidence.revision_id)}`,
      query: { conversation: conversationId, locator: evidence.locator_id } as Record<
        string,
        string
      >,
    }
  }
  if (evidence.location_status === 'unavailable' || !evidence.paper_id || !(evidence.page > 0))
    return null
  const query: Record<string, string> = { page: String(evidence.page) }
  if (evidence.bbox?.length === 4 && evidence.bbox.every(Number.isFinite))
    query.bbox = evidence.bbox.join(',')
  return { path: `/reader/${encodeURIComponent(evidence.paper_id)}`, query }
}

function csvField(value: unknown): string {
  let text = value == null ? '' : String(value)
  // Quoting alone does not stop a spreadsheet from executing an imported formula.
  if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r]/.test(text)) text = "'" + text
  return `"${text.replace(/"/g, '""')}"`
}

/** One row per evidence, including empty cells, with portable source identity.
 * Local app links are explicitly labelled: moving a CSV does not move originals.
 */
export function comparisonCsv(data: ComparisonData): string {
  const rows: unknown[][] = [
    [
      '维度',
      '文献ID',
      '文献标题',
      '单元格状态',
      '引用原句',
      '核验状态',
      '来源类型',
      '来源ID',
      '固定版本ID',
      '版本号',
      '原件SHA256',
      '定位ID',
      '位置类型',
      '位置描述',
      '页码',
      'PDF区域',
      '结构位置JSON',
      '本机引用路径（需原数据）',
      '字段版本',
      '人工校正内容',
      '人工核验状态',
      '人工备注',
      '提取状态',
    ],
  ]
  for (const row of data.rows) {
    for (const paper of data.papers) {
      const cell = row.cells[paper.id]
      const durable = cell as typeof cell & { manual?: {value:string;status:string;note:string}; extraction_status?:string }
      const review = [(data as ComparisonData & {schema_version?:number}).schema_version ?? '',durable?.manual?.value || '',durable?.manual?.status || '',durable?.manual?.note || '',durable?.extraction_status || '']
      if (!cell?.evidence.length) {
        rows.push([
          row.label,
          paper.id,
          paper.title,
          cell?.status === 'not_found' ? '未检索到 (not_found)' : cell?.status || '未检索到',
          '',
          '未核验',
          '',
          '',
          '',
          '',
          paper.content_sha256 || '',
          '',
          '',
          '',
          '',
          '',
          '',
          '',
          ...review,
        ])
        continue
      }
      for (const evidence of cell.evidence) {
        const target = comparisonEvidenceRoute(evidence, data.conversation_id)
        const link = target ? target.path + '?' + new URLSearchParams(target.query).toString() : ''
        rows.push([
          row.label,
          paper.id,
          paper.title,
          cell.status || 'unverified',
          evidence.quote,
          evidence.verification_status || 'unverified',
          evidence.source_media_type || '',
          evidence.source_id || '',
          evidence.revision_id || '',
          evidence.source_version ?? '',
          evidence.source_sha256 || paper.content_sha256 || '',
          evidence.locator_id || '',
          evidence.locator_kind || (evidence.page > 0 ? 'pdf_page' : ''),
          comparisonLocationLabel(evidence),
          evidence.page > 0 ? evidence.page : '',
          evidence.bbox ? JSON.stringify(evidence.bbox) : '',
          evidence.source_location ? JSON.stringify(evidence.source_location) : '',
          link,
          ...review,
        ])
      }
    }
  }
  return '\ufeff' + rows.map((row) => row.map(csvField).join(',')).join('\r\n')
}
