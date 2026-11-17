import axios from 'axios'
import type { ResearchRun, ResearchRunSummary } from './researchRunApi'

export type QualityLabel = 'substantive' | 'neutral' | 'passing' | 'not_located'
export interface QualityEvidence {
  id: string
  revision_id: string
  locator_id: string
  quote: string
  location: Record<string, unknown>
  match_method: string
  semantic_support: 'unverified'
}
export interface QualityItem {
  ref_index: number
  raw_text: string
  label: QualityLabel
  score: number
  located_contexts: number
  sample_limited: boolean
  reference_entry_in_source: boolean
  signals: string[]
  evidence: QualityEvidence[]
}
export interface QualityOutput {
  title: string
  revision_id: string
  raw_sha256: string
  results: QualityItem[]
  summary: Record<QualityLabel, number>
  progress: { completed: number; total: number }
  note: string
}
export interface QualityInputs { paper_id: string }
export interface QualityRun extends Omit<ResearchRun, 'inputs' | 'result'> {
  inputs: QualityInputs
  result: QualityOutput | null
  resume_blocked_reason: string
}
export interface QualityCreate extends QualityInputs {
  conversation_id: string
  run_id: string
  request_id: string
}
const http = axios.create({ baseURL: '/api/writing/quality/runs' })
export const listQualityRuns = (cid: string): Promise<{ items: ResearchRunSummary[] }> =>
  http.get('', { params: { conversation_id: cid } }).then(r => r.data)
export const getQualityRun = (id: string, cid: string, signal?: AbortSignal): Promise<QualityRun> =>
  http.get(`/${encodeURIComponent(id)}`, { params: { conversation_id: cid }, signal }).then(r => r.data)
export const createQualityRun = (body: QualityCreate): Promise<QualityRun> => http.post('', body).then(r => r.data)
export const resumeQualityRun = (id: string, cid: string, revision: number, request: string, retry: boolean): Promise<QualityRun> =>
  http.post(`/${encodeURIComponent(id)}/resume`, { conversation_id: cid, expected_revision: revision, request_id: request, retry_unknown: retry }).then(r => r.data)
export const cancelQualityRun = (id: string, cid: string): Promise<QualityRun> =>
  http.post(`/${encodeURIComponent(id)}/cancel`, { conversation_id: cid }).then(r => r.data)
