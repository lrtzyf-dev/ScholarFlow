import axios from 'axios'
export interface SourceDifference {
  left: {
    revision_id: string
    version: number
    title: string
    format: string
    omitted_content: string[]
  }
  right: {
    revision_id: string
    version: number
    title: string
    format: string
    omitted_content: string[]
  }
  rows: {
    status: 'unchanged' | 'removed' | 'added'
    text: string
    label: string
    kind: string
    old_unit: number | null
    new_unit: number | null
    row: number
  }[]
  offset: number
  next_offset: number | null
  total_rows: number
  counts: { unchanged: number; removed: number; added: number }
  coarse_regions: boolean
  raw_bytes_identical: boolean
}
export const compareSourceVersions = (
  conversationId: string,
  leftId: string,
  rightId: string,
  offset = 0,
  changesOnly = true
): Promise<SourceDifference> =>
  axios
    .get('/api/source-versions/compare', {
      params: {
        conversation_id: conversationId,
        left_id: leftId,
        right_id: rightId,
        offset,
        limit: 80,
        changes_only: changesOnly,
      },
    })
    .then((r) => r.data)

export interface PersistedSourceDraft {
  revision: number
  state: string
  draft: {
    kind: 'upload' | 'local'
    local: { project_id: string; workspace_revision: number; path: string } | null
    filename: string
    media_type: string
    raw_size: number
    raw_sha256: string
    baseline_revision_id: string
    expected_version: number
    request_id: string
  } | null
}
const draftUrl = (sourceId: string) =>
  `/api/source-versions/${encodeURIComponent(sourceId)}/update-draft`
export const getSourceUpdateDraft = (
  cid: string,
  sourceId: string
): Promise<PersistedSourceDraft> =>
  axios.get(draftUrl(sourceId), { params: { conversation_id: cid } }).then((r) => r.data)
export const getSourceUpdateFile = (
  cid: string,
  sourceId: string,
  revision: number
): Promise<Blob> =>
  axios
    .get(`${draftUrl(sourceId)}/file`, {
      params: { conversation_id: cid, revision },
      responseType: 'blob',
    })
    .then((r) => r.data)
export const saveSourceUpdateDraft = (
  cid: string,
  sourceId: string,
  revision: number,
  baselineId: string,
  requestId: string,
  file: File
): Promise<PersistedSourceDraft> => {
  const body = new FormData()
  body.append('conversation_id', cid)
  body.append('expected_revision', String(revision))
  body.append('baseline_revision_id', baselineId)
  body.append('request_id', requestId)
  body.append('file', file)
  return axios.put(draftUrl(sourceId), body).then((r) => r.data)
}
export const discardSourceUpdateDraft = (
  cid: string,
  sourceId: string,
  revision: number
): Promise<PersistedSourceDraft> =>
  axios
    .post(`${draftUrl(sourceId)}/discard`, { conversation_id: cid, expected_revision: revision })
    .then((r) => r.data)

export const saveLocalUpdateDraft = (
  cid: string,
  sourceId: string,
  revision: number,
  baselineId: string,
  requestId: string,
  local: { projectId: string; workspaceRevision: number; path: string }
): Promise<PersistedSourceDraft> =>
  axios
    .put(`${draftUrl(sourceId)}/local`, {
      conversation_id: cid,
      expected_revision: revision,
      baseline_revision_id: baselineId,
      request_id: requestId,
      project_id: local.projectId,
      workspace_revision: local.workspaceRevision,
      path: local.path,
    })
    .then((r) => r.data)
