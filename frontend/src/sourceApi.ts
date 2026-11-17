import axios from 'axios'

const http = axios.create({ baseURL: '/api/sources' })
export const ensureSourceConversation = (conversationId: string) =>
  http.post('/ensure-conversation', { conversation_id: conversationId }).then((r) => r.data)
export const openLibrarySource = (
  paperId: string,
  conversationId: string,
  libraryRevisionId?: string
): Promise<SourceRevision & { conversation_id?: string }> =>
  http
    .post(`/library/${encodeURIComponent(paperId)}/open`, { conversation_id: conversationId, library_revision_id: libraryRevisionId })
    .then((r) => r.data)
export const getLibraryVersions = (paperId:string,conversationId:string):Promise<{items:{revision_id:string;title:string;created_at:string;parser:string;primary:boolean}[]}> =>
  http.get(`/library/${encodeURIComponent(paperId)}/versions`,{params:{conversation_id:conversationId}}).then(r=>r.data)
export interface SourceRevision {
  source_id: string
  revision_id: string
  title: string
  version: number
  kind: string
  media_type: string
  raw_size?: number
  current_version?: number
  current_revision_id?: string
  available_views?: string[]
  parse_status?: string
  document_format?: string
  library_paper_id?: string | null
  text_origin?: string
  created_at?: string
  raw_sha256?: string
  origin_ref?: string | null
  library_import_performed?: boolean
  parse_error?: string
}
export interface SourceBlock {
  kind: string
  text: string
  paragraph_index?: number
  block_id?: string
  part_name?: string
  xml_path?: string
  part_label?: string
  section_kind?: string
  table_row?: number
  table_column?: number
  column_span?: number
  vertical_merge?: string | null
  table_depth?: number
  hyperlinks?: { text: string; target?: string | null; anchor?: string | null; external: boolean; visited: boolean }[]
  images?: { title: string; description: string; image_sha256?: string | null; pixels_understood: boolean; ocr_performed: boolean }[]
  sheet?: string
  cell_range?: string
  row_start?: number
  row_end?: number
  line_start?: number
  line_end?: number
  values?: string[][]
  cells?: SourceCell[]
}
export interface SourceCell {
  cell?: string
  address?: string
  value: string | number | boolean | null
  raw_value?: string | number | boolean | null
  value_kind?: string
  number_format?: string
  percent_format?: boolean
  date_format?: boolean
  formula?: string
  formula_representation?: 'scalar' | 'array' | 'dataTable' | 'unsupported'
  formula_attributes?: Record<string, string | boolean | number>
  formula_storage_kind?: string
  stored_formula_text?: string | null
  formula_xml_attributes?: Record<string, string>
  formula_origin?: string
  special_formula_range?: string
  special_formula_anchor?: string
  special_formula_type?: string
  special_formula_member?: boolean
  cached_value?: string | number | boolean | null
  cache_state?: string
  merged_range?: string
  merged_anchor?: string
  stored_xml_value?: string | null
  missing_marker_candidate?: boolean
}
export interface SourceSheet {
  name: string
  row_count?: number
  column_count?: number
  state?: string
  formula_count?: number
  merged_ranges?: string[]
  hidden_rows?: number[]
  headers?: { duplicate_header_labels?: string[]; blank_header_columns?: string[] }
}
export interface SourceTableMetadata {
  encoding?: string
  delimiter?: string
  header_row?: number
  date_epoch?: string
  formula_count?: number
  formula_missing_cache_count?: number
  duplicate_header_labels?: string[]
  warnings?: string[]
}
export interface SourceDocument {
  revision_id: string
  format: string
  blocks: SourceBlock[]
  sheets?: (string | SourceSheet)[]
  fingerprint?: string
  table_metadata?: SourceTableMetadata
  truncated?: boolean
  offset?: number
  next_offset?: number | null
  total_blocks?: number
  sheet?: string | null
  extraction_scope?: string
  omitted_content?: string[]
}
export interface SourceLocator {
  locator_id: string
  revision_id: string
  kind: 'text_lines' | 'pdf_page' | 'word_paragraphs' | 'word_block' | 'sheet_cells'
  quote: string
  location: {
    line_start?: number
    line_end?: number
    page_index?: number
    bbox?: number[] | null
    paragraph_start?: number
    paragraph_end?: number
    block_id?: string
    part_name?: string
    xml_path?: string
    part_label?: string
    sheet?: string
    cell_range?: string
  }
}
export const sourceFileUrl = (revisionId: string, conversationId: string) =>
  `/api/sources/revisions/${encodeURIComponent(revisionId)}/file?${new URLSearchParams({ conversation_id: conversationId })}`
export const listSources = (
  conversationId: string,
  offset = 0
): Promise<{ items: SourceRevision[]; next_offset: number | null }> =>
  http
    .get('', { params: { conversation_id: conversationId, offset, limit: 100 } })
    .then((r) => r.data)
export const getSourceRevision = (
  revisionId: string,
  conversationId: string
): Promise<SourceRevision> =>
  http
    .get(`/revisions/${encodeURIComponent(revisionId)}`, {
      params: { conversation_id: conversationId },
    })
    .then((r) => r.data)
export const uploadSource = (
  file: File,
  conversationId: string,
  requestId: string,
  update?: { source_id: string; expected_version: number }
): Promise<SourceRevision> => {
  const body = new FormData()
  body.append('file', file)
  body.append('conversation_id', conversationId)
  body.append('request_id', requestId)
  if (update) {
    body.append('source_id', update.source_id)
    body.append('expected_version', String(update.expected_version))
  }
  return http.post('/upload', body).then((r) => r.data)
}
export const captureProjectSource = (input: {
  conversation_id: string
  request_id: string
  project_id: string
  workspace_revision: number
  path: string
  source_id?: string
  expected_version?: number
}): Promise<SourceRevision> => http.post('/capture-project', input).then((r) => r.data)
export interface SourceHistory {
  source_id: string
  current_version: number
  current_revision_id: string
  items: SourceRevision[]
  next_before_version: number | null
}
export const getSourceHistory = (
  sourceId: string,
  conversationId: string,
  beforeVersion?: number
): Promise<SourceHistory> =>
  http
    .get(`/${encodeURIComponent(sourceId)}/history`, {
      params: { conversation_id: conversationId, before_version: beforeVersion, limit: 20 },
    })
    .then((r) => r.data)
export const getSourceDocument = (
  revisionId: string,
  conversationId: string,
  options: { offset?: number; sheet?: string; locator_id?: string } = {}
): Promise<SourceDocument> =>
  http
    .get(`/revisions/${encodeURIComponent(revisionId)}/document`, {
      params: { conversation_id: conversationId, ...options },
    })
    .then((r) => r.data)
export const getSourceText = (revisionId: string, conversationId: string, lineStart = 1) =>
  http
    .get(`/revisions/${encodeURIComponent(revisionId)}/text`, {
      params: { conversation_id: conversationId, line_start: lineStart, line_limit: 200 },
    })
    .then(
      (r) =>
        r.data as {
          text: string
          line_start: number
          line_end: number
          total_lines: number
          next_line: number | null
        }
    )
export const getSourceLocator = (
  revisionId: string,
  locatorId: string,
  conversationId: string
): Promise<SourceLocator> =>
  http
    .get(`/revisions/${encodeURIComponent(revisionId)}/locators/${encodeURIComponent(locatorId)}`, {
      params: { conversation_id: conversationId },
    })
    .then((r) => r.data)
export const importSourcesToLibrary = (
  conversationId: string,
  revisionIds: string[],
  requestId: string
): Promise<{
  items: { revision_id: string; status: string; paper_id?: string; error?: string }[]
}> =>
  http
    .post('/library-import', {
      conversation_id: conversationId,
      revision_ids: revisionIds,
      request_id: requestId,
    })
    .then((r) => r.data)
export function sourceError(error: any): string {
  const detail = error?.response?.data?.detail
  return typeof detail === 'string' ? detail : error?.message || '资料操作失败，请重试'
}
