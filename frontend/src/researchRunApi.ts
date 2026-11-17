import axios from 'axios'
import type { FrameworkOut } from './api'

const http = axios.create({ baseURL: '/api/writing/framework/runs' })
export interface ResearchRun {
  run_id: string
  conversation_id: string
  revision: number
  attempt_number: number
  status: string
  outcome: string
  stage: string
  error: string
  inputs: { idea: string; max_sources: number }
  model: { model_id: string; profile_name: string; mode: string }
  events: { stage: string; text: string; at: string }[]
  result: FrameworkOut | null
  source_count: number
  can_resume: boolean
  requires_unknown_confirmation: boolean
  resource_pause: { kind: string; message: string } | null
  usage: { call_count: number; actual_usage: Record<string, number>; unknown_usage_calls: number }
}
export interface ResearchRunSummary {
  run_id: string
  title: string
  status: string
  outcome: string
  updated_at: string
}
export interface ResearchCreate {
  run_id: string
  request_id: string
  conversation_id: string
  idea: string
  max_sources: number
}
export const listFrameworkRuns = (
  cid: string,
  signal?: AbortSignal
): Promise<{ items: ResearchRunSummary[] }> =>
  http.get('', { params: { conversation_id: cid }, signal }).then((r) => r.data)
export const getFrameworkRun = (
  id: string,
  cid: string,
  signal?: AbortSignal
): Promise<ResearchRun> =>
  http
    .get(`/${encodeURIComponent(id)}`, { params: { conversation_id: cid }, signal })
    .then((r) => r.data)
export const createFrameworkRun = (body: ResearchCreate): Promise<ResearchRun> =>
  http.post('', body).then((r) => r.data)
export const resumeFrameworkRun = (
  id: string,
  cid: string,
  expected_revision: number,
  request_id: string,
  retry_unknown: boolean
): Promise<ResearchRun> =>
  http
    .post(`/${encodeURIComponent(id)}/resume`, {
      conversation_id: cid,
      expected_revision,
      request_id,
      retry_unknown,
    })
    .then((r) => r.data)
export const cancelFrameworkRun = (id: string, cid: string): Promise<ResearchRun> =>
  http.post(`/${encodeURIComponent(id)}/cancel`, { conversation_id: cid }).then((r) => r.data)
