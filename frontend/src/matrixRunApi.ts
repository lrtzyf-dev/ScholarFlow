import axios from 'axios'
import type { MatrixDocument } from './researchApi'
import type { ResearchRun, ResearchRunSummary } from './researchRunApi'

export interface MatrixInputs {
  matrix_id: string
  expected_version: number
  field_keys?: string[]
  paper_ids?: string[]
}
export interface MatrixRunOutput {
  matrix_id: string
  base_version: number
  document: MatrixDocument
  progress: { completed: number; total: number }
  applied_version: number | null
  apply_conflict: { message?: string; kind?: string } | null
  method: string
  method_details?: Record<string, unknown>
}
export interface MatrixRun extends Omit<ResearchRun, 'inputs' | 'result'> {
  attempt_id: string
  updated_at: string
  inputs: MatrixInputs
  result: MatrixRunOutput | null
  resume_blocked_reason: string
}
export interface MatrixRunCreate extends MatrixInputs {
  run_id: string
  request_id: string
  conversation_id: string
}
const http = axios.create({ baseURL: '/api/research/matrix-runs' })
export const listMatrixRuns = (
  cid: string,
  matrixId?: string
): Promise<{ items: ResearchRunSummary[] }> =>
  http
    .get<{ items: (MatrixRun & { title?: string })[] }>('', {
      params: { conversation_id: cid, matrix_id: matrixId || undefined },
    })
    .then((r) => ({
      items: r.data.items.map((value) => ({
        run_id: value.run_id,
        title: value.title || '矩阵 ' + value.inputs.matrix_id,
        status: value.status,
        outcome: value.outcome,
        updated_at: value.updated_at,
      })),
    }))
export const getMatrixRun = (id: string, cid: string, signal?: AbortSignal): Promise<MatrixRun> =>
  http
    .get(`/${encodeURIComponent(id)}`, { params: { conversation_id: cid }, signal })
    .then((r) => r.data)
export const createMatrixRun = (body: MatrixRunCreate): Promise<MatrixRun> =>
  http.post('', body).then((r) => r.data)
export const resumeMatrixRun = (
  id: string,
  cid: string,
  revision: number,
  request: string,
  retry: boolean
): Promise<MatrixRun> =>
  http
    .post(`/${encodeURIComponent(id)}/resume`, {
      conversation_id: cid,
      expected_revision: revision,
      request_id: request,
      retry_unknown: retry,
    })
    .then((r) => r.data)
export const cancelMatrixRun = (
  id: string,
  cid: string,
  revision: number,
  attemptId: string
): Promise<MatrixRun> =>
  http
    .post(`/${encodeURIComponent(id)}/cancel`, {
      conversation_id: cid,
      expected_revision: revision,
      expected_attempt_id: attemptId,
    })
    .then((r) => r.data)
export const applyMatrixRun = (
  id: string,
  cid: string,
  version: number,
  request: string
): Promise<MatrixRun> =>
  http
    .post(`/${encodeURIComponent(id)}/apply`, {
      conversation_id: cid,
      expected_matrix_version: version,
      request_id: request,
    })
    .then((r) => r.data)
