import axios from 'axios'
import type { ComparisonData, ComparisonCell, ComparisonEvidence } from './api'
import type { SourceLocator } from './sourceApi'

const http = axios.create({ baseURL: '/api/research' })
export const openFixedLibrary = (
  paperId: string,
  conversation_id: string
): Promise<{ revision_id: string; conversation_id: string }> =>
  http.post(`/library/${paperId}/open`, { conversation_id }).then((r) => r.data)
export const statusLabels: Record<string, string> = {
  supported: '支持',
  conflict: '冲突',
  not_found: '未找到',
  not_processed: '未处理',
  unverified: '未核验',
}
export interface MatrixField {
  key: string
  label: string
  query: string
}
export interface MatrixCell extends Omit<ComparisonCell, 'status'> {
  status: string
  extraction_status: string
  verification_status: string
  manual: null | {
    value: string
    note: string
    status: string
    original_quote: string
    actor: string
    at: string
  }
  extraction_history: unknown[]
  extraction_candidate?: null | {
    evidence: ComparisonEvidence[]
    quote?: string
    extraction_status: string
    verification_status: string
    method?: string
    run_id?: string
    requires_review: boolean
    sha256?: string
  }
}
export interface MatrixDocument extends Omit<ComparisonData, 'rows' | 'dimensions'> {
  rows: { key: string; label: string; cells: Record<string, MatrixCell> }[]
  dimensions: MatrixField[]
  schema_version: number
}
export interface Matrix {
  id: string
  title: string
  version: number
  conversation_id: string
  document: MatrixDocument
  updated_at: string
}
export const listMatrices = (
  conversation_id: string
): Promise<{ items: Omit<Matrix, 'document'>[] }> =>
  http.get('/matrices', { params: { conversation_id } }).then((r) => r.data)
export const getMatrix = (id: string, conversation_id: string): Promise<Matrix> =>
  http.get(`/matrices/${id}`, { params: { conversation_id } }).then((r) => r.data)
export const createMatrix = (input: {
  conversation_id: string
  request_id?: string
  title: string
  paper_ids: string[]
  fields: MatrixField[]
}): Promise<Matrix> => http.post('/matrices', input).then((r) => r.data)
export const updateMatrix = (
  id: string,
  input: {
    conversation_id: string
    expected_version: number
    title: string
    paper_ids: string[]
    fields: MatrixField[]
  }
): Promise<Matrix> => http.put(`/matrices/${id}`, input).then((r) => r.data)
export const extractMatrix = (
  matrix: Matrix,
  fields?: string[],
  papers?: string[]
): Promise<Matrix> =>
  http
    .post(`/matrices/${matrix.id}/extract`, {
      conversation_id: matrix.conversation_id,
      expected_version: matrix.version,
      field_keys: fields,
      paper_ids: papers,
    })
    .then((r) => r.data)
export const editMatrixCell = (
  matrix: Matrix,
  field_key: string,
  paper_id: string,
  input: {
    value: string
    note: string
    status: string
    use_candidate?: boolean
    candidate_sha256?: string
  }
): Promise<Matrix> =>
  http
    .patch(`/matrices/${matrix.id}/cell`, {
      conversation_id: matrix.conversation_id,
      expected_version: matrix.version,
      field_key,
      paper_id,
      value: input.value,
      note: input.note,
      status: input.status,
      use_candidate: input.use_candidate || false,
      candidate_sha256: input.use_candidate ? input.candidate_sha256 : undefined,
    })
    .then((r) => r.data)
export const exportMatrix = (matrix: Matrix) =>
  http
    .get(`/matrices/${matrix.id}/export`, { params: { conversation_id: matrix.conversation_id } })
    .then((r) => r.data)
export const importMatrix = (conversation_id: string, data: unknown): Promise<Matrix> =>
  http.post('/matrices/import', { conversation_id, package: data }).then((r) => r.data)
export interface ReadingState {
  version: number
  position: {
    page_index?: number
    line_start?: number
    offset?: number
    sheet?: string
    scroll_top?: number
    zoom?: number
    locator_id?: string
  }
  user_read: boolean
}
export const getReading = (revision: string, conversation_id: string): Promise<ReadingState> =>
  http.get(`/reading/${revision}`, { params: { conversation_id } }).then((r) => r.data)
export const saveReading = (
  revision: string,
  conversation_id: string,
  state: ReadingState,
  userRead?: boolean
): Promise<ReadingState> =>
  http
    .put(`/reading/${revision}`, {
      conversation_id,
      expected_version: state.version,
      position: state.position,
      user_read: userRead,
    })
    .then((r) => r.data)
export interface Annotation {
  id: string
  revision_id: string
  locator_id: string
  version: number
  kind: string
  body: string
  selected_text: string
  locator?: SourceLocator
  anchor_status?: string
}
export const getAnnotations = (
  revision: string,
  conversation_id: string
): Promise<{ items: Annotation[] }> =>
  http.get(`/reading/${revision}/annotations`, { params: { conversation_id } }).then((r) => r.data)
export const createAnnotation = (
  revision: string,
  conversation_id: string,
  locator_id: string,
  kind: string,
  body: string,
  selected_text = ''
): Promise<Annotation> =>
  http
    .post(`/reading/${revision}/annotations`, {
      conversation_id,
      locator_id,
      kind,
      body,
      selected_text,
    })
    .then((r) => r.data)
export const editAnnotation = (
  annotation: Annotation,
  conversation_id: string,
  body: string,
  deleted = false
): Promise<Annotation> =>
  http
    .patch(`/annotations/${annotation.id}`, {
      conversation_id,
      expected_version: annotation.version,
      body,
      deleted,
    })
    .then((r) => r.data)
export const createSourceLocator = (
  revision: string,
  conversation_id: string,
  input: Record<string, unknown>
): Promise<SourceLocator> =>
  axios
    .post(`/api/sources/revisions/${revision}/locators`, { conversation_id, ...input })
    .then((r) => r.data)
export interface RelationDetail {
  id: string
  version: number
  source_paper_id: string
  manual: boolean
  extracted: Record<string, any>
  effective: Record<string, any>
  history: { version: number; action: string; actor: string; reason: string; created_at: string }[]
}
export const getRelation = (id: string): Promise<RelationDetail> =>
  http.get(`/relations/${id}`).then((r) => r.data)
export const reviewRelation = (
  item: RelationDetail,
  action: string,
  reason: string,
  target?: string,
  relation_type?: string,
  restore_version?: number
) =>
  http
    .patch(`/relations/${item.id}`, {
      expected_version: item.version,
      action,
      reason,
      target,
      relation_type,
      restore_version,
    })
    .then((r) => r.data)
export const locateRelation = (
  id: string,
  conversation_id: string,
  context_index?: number
): Promise<{ revision_id: string; locator_id: string }> =>
  http.post(`/relations/${id}/locate`, { conversation_id, context_index }).then((r) => r.data)
export const graphPage = (
  options: { offset?: number; limit?: number; focus?: string; relation_type?: string } = {},
  signal?: AbortSignal
) => axios.get('/api/graph', { params: options, signal }).then((r) => r.data)
