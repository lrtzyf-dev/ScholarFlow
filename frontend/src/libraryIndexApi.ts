import axios from 'axios'

const http = axios.create({ baseURL: '/api/library-index' })
export interface LibraryIndexOperation {
  id: string
  run_id: string
  target_revision_id: string
  status: string
  error: string
  index_mode: 'text' | 'semantic'
  prepared_count: number
  prepared_batches?: number
  prepared_result_available: boolean
  published_build_id: string | null
  dimension: number
  usage?: {
    call_count: number
    actual_usage: Record<string, number>
    estimated_usage: Record<string, number>
    unknown_usage_calls?: number
  }
}
export interface LibraryIndexState {
  paper_id: string
  version: number
  primary_revision_id: string
  indexed_revision_id: string | null
  current_build_id: string | null
  index_mode: string
  chunk_count: number
  embedding_matches_current: boolean
  dimension: number | null
  operations: LibraryIndexOperation[]
}
export interface IndexChoice {
  conversation_id: string
  revision_id: string
  expected_version: number
  expected_primary_id: string
}
export interface IndexBuild extends IndexChoice {
  request_id: string
  set_primary: boolean
  index_mode: 'text' | 'semantic'
}
export const getLibraryIndex = (
  paperId: string,
  conversationId: string,
  signal?: AbortSignal
): Promise<LibraryIndexState> =>
  http
    .get(`/${encodeURIComponent(paperId)}/state`, {
      params: { conversation_id: conversationId },
      signal,
    })
    .then((r) => r.data)
export const selectLibraryPrimary = (paperId: string, body: IndexChoice) =>
  http.post(`/${encodeURIComponent(paperId)}/primary`, body).then((r) => r.data)
export const buildLibraryIndex = (
  paperId: string,
  body: IndexBuild
): Promise<LibraryIndexOperation> =>
  http.post(`/${encodeURIComponent(paperId)}/builds`, body).then((r) => r.data)
export const getLibraryIndexBuild = (
  id: string,
  conversationId: string,
  signal?: AbortSignal
): Promise<LibraryIndexOperation> =>
  http
    .get(`/builds/${encodeURIComponent(id)}`, {
      params: { conversation_id: conversationId },
      signal,
    })
    .then((r) => r.data)
export const resumeLibraryIndex = (id: string, body: Omit<IndexChoice, 'revision_id'>) =>
  http.post(`/builds/${encodeURIComponent(id)}/resume`, body).then((r) => r.data)
export const cancelLibraryIndex = (id: string, conversationId: string) =>
  http
    .post(`/builds/${encodeURIComponent(id)}/cancel`, { conversation_id: conversationId })
    .then((r) => r.data)
