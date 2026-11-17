import axios from 'axios'

const http = axios.create({ baseURL: '/api/artifacts' })
export interface Artifact {
  id: string
  title: string
  kind: 'document' | 'analysis'
  conversation_id: string | null
  version: number
  current_revision_id: string
  revision?: ArtifactRevision
  sections?: { index: number; title: string; start: number; end: number; sha256: string }[]
}
export interface ArtifactRevision {
  id: string
  version: number
  content: string
  content_sha256: string
  manifest_sha256: string
  evidence: Record<string, any>[]
  analysis: Record<string, any>
  parent_revision_id?: string | null
}
export interface ArtifactChange {
  id: string
  status: string
  content: string
  conflict: boolean
  base_revision_id: string
  diff: string
  diff_total_lines: number
  diff_has_more: boolean
  diff_next_offset: number | null
  result_revision_id: string | null
}
export type ArtifactExportFormat = 'docx' | 'xlsx' | 'svg' | 'bundle' | 'md' | 'manifest'
export interface ArtifactExport {
  id: string
  export_id: string
  artifact_id: string
  revision_id: string
  format: ArtifactExportFormat
  payload_sha256: string
  payload_size: number
  filename: string
  download_url: string
  validation: {
    container_readable: boolean
    content_checked: boolean
    source_binding_checked: boolean
    computation_recorded: boolean
    native_open: string
    render: string
    semantic: string
    warnings?: string[]
  }
}
export const artifactRequestId = () => crypto.randomUUID().replaceAll('-', '')
const params = (conversationId?: string | null) => ({ conversation_id: conversationId || undefined })
export const createArtifact = (input: { request_id: string; title: string; kind?: 'document' | 'analysis'; content: string; evidence?: unknown[]; analysis?: object; conversation_id?: string | null }): Promise<Artifact> =>
  http.post('', input).then((r) => r.data)
export const listArtifacts = (conversationId?: string | null, offset = 0): Promise<{ items: Artifact[]; total: number; next_offset: number | null }> =>
  http.get('', { params: { ...params(conversationId), offset } }).then((r) => r.data)
export const getArtifact = (id: string, conversationId?: string | null, revisionId?: string): Promise<Artifact> =>
  http.get(`/${id}`, { params: { ...params(conversationId), revision_id: revisionId } }).then((r) => r.data)
export const artifactHistory = (id: string, conversationId?: string | null, beforeVersion?: number): Promise<{ items: ArtifactRevision[]; next_before_version: number | null }> =>
  http.get(`/${id}/revisions`, { params: { ...params(conversationId), before_version: beforeVersion } }).then((r) => r.data)
export const artifactChanges = (id: string, conversationId?: string | null): Promise<{ items: ArtifactChange[]; total: number }> =>
  http.get(`/${id}/changes`, { params: params(conversationId) }).then((r) => r.data)
export const proposeArtifactChange = (id: string, input: Record<string, unknown>): Promise<ArtifactChange> =>
  http.post(`/${id}/changes`, input).then((r) => r.data)
export const decideArtifactChange = (id: string, changeId: string, input: { conversation_id?: string | null; accept: boolean; expected_revision_id: string }): Promise<ArtifactChange> =>
  http.post(`/${id}/changes/${changeId}/decision`, input).then((r) => r.data)
export const artifactChangeDetail = (id: string, changeId: string, conversationId?: string | null, offset = 0): Promise<ArtifactChange> =>
  http.get(`/${id}/changes/${changeId}`, { params: { ...params(conversationId), offset } }).then((r) => r.data)
export const recomputeArtifact = (id: string, conversationId?: string | null, revisionId?: string): Promise<Record<string, any>> =>
  http.post(`/${id}/recompute`, params(conversationId), { params: { revision_id: revisionId } }).then((r) => r.data)
export const artifactComputations = (id: string, conversationId?: string | null, revisionId?: string): Promise<{ items: Record<string, any>[] }> =>
  http.get(`/${id}/computations`, { params: { ...params(conversationId), revision_id: revisionId } }).then((r) => r.data)
export const proposeArtifactPublication = (id: string, input: { conversation_id: string; request_id: string; revision_id: string; project_id: string; workspace_revision: number; path: string; expected_file_sha256: string | null }): Promise<{id: string}> =>
  http.post(`/${id}/publish-proposal`, input).then(r => r.data)
export function artifactDownloadUrl(id: string, format: string, conversationId?: string | null, revisionId?: string) {
  const query = new URLSearchParams({ format })
  if (conversationId) query.set('conversation_id', conversationId)
  if (revisionId) query.set('revision_id', revisionId)
  return `/api/artifacts/${encodeURIComponent(id)}/download?${query}`
}
export const createArtifactExport = (id: string, input: { request_id: string; revision_id: string; format: ArtifactExportFormat; conversation_id?: string | null }): Promise<ArtifactExport> =>
  http.post(`/${id}/exports`, input).then(r => r.data)
export const listArtifactExports = (id: string, conversationId?: string | null, revisionId?: string, offset = 0): Promise<{items: ArtifactExport[]; total: number; next_offset: number | null}> =>
  http.get(`/${id}/exports`, {params: {...params(conversationId), revision_id: revisionId, offset}}).then(r => r.data)
export const getArtifactExport = (id: string, exportId: string, conversationId?: string | null): Promise<ArtifactExport> =>
  http.get(`/${id}/exports/${exportId}`, {params: params(conversationId)}).then(r => r.data)
export function savedArtifactExportDownloadUrl(id: string, exportId: string, conversationId?: string | null) {
  const query = new URLSearchParams()
  if (conversationId) query.set('conversation_id', conversationId)
  return `/api/artifacts/${encodeURIComponent(id)}/exports/${encodeURIComponent(exportId)}/download${query.size ? `?${query}` : ''}`
}
