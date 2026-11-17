import axios from 'axios'
import type { ParallelResult, TranslateResult, TranslationSourceRef } from './api'
import type { ResearchRun, ResearchRunSummary } from './researchRunApi'

export interface TranslationInputs {
  text: string
  source: string
  target: string
  display_mode: 'whole' | 'parallel'
  source_ref: TranslationSourceRef | null
}
export type TranslationOutput = TranslateResult &
  ParallelResult & {
    conversation_id: string
    progress: { completed_units: number; total_units: number }
    partial: boolean
  }
export interface TranslationRun extends Omit<ResearchRun, 'inputs' | 'result'> {
  inputs: TranslationInputs
  result: TranslationOutput | null
  resume_blocked_reason: string
}
export interface TranslationCreate extends TranslationInputs {
  conversation_id: string
  run_id: string
  request_id: string
}
const http = axios.create({ baseURL: '/api/translation/runs' })
export const listTranslationRuns = (cid: string): Promise<{ items: ResearchRunSummary[] }> =>
  http.get('', { params: { conversation_id: cid } }).then((r) => r.data)
export const getTranslationRun = (
  id: string,
  cid: string,
  signal?: AbortSignal
): Promise<TranslationRun> =>
  http
    .get(`/${encodeURIComponent(id)}`, { params: { conversation_id: cid }, signal })
    .then((r) => r.data)
export const createTranslationRun = (body: TranslationCreate): Promise<TranslationRun> =>
  http.post('', body).then((r) => r.data)
export const resumeTranslationRun = (
  id: string,
  cid: string,
  revision: number,
  request: string,
  retry: boolean
): Promise<TranslationRun> =>
  http
    .post(`/${encodeURIComponent(id)}/resume`, {
      conversation_id: cid,
      expected_revision: revision,
      request_id: request,
      retry_unknown: retry,
    })
    .then((r) => r.data)
export const cancelTranslationRun = (id: string, cid: string): Promise<TranslationRun> =>
  http.post(`/${encodeURIComponent(id)}/cancel`, { conversation_id: cid }).then((r) => r.data)
