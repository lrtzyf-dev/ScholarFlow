import axios from 'axios'
import type { DiagnosticReport } from './api'

const http = axios.create({ baseURL: '/api/debug' })
export interface ProbeTarget {
  service: 'llm' | 'embedding'
  label: string
  model_id: string
  endpoint: string
  mode: string
  available: boolean
  expected_config: string
  detail: string
}
export interface LocalReport extends DiagnosticReport {
  scope: 'local'
  remote_requests: number
  conversation_id: string | null
  probe_targets: ProbeTarget[]
}
export interface ProbeResult {
  diagnostic_id: string
  service: string
  status: string
  detail: string
  target: ProbeTarget
  calls: { call_id: string; state: string; usage_source: string; amount: string | null }[]
}
export const inspectLocal = (conversationId?: string): Promise<LocalReport> =>
  http
    .get('/config', { params: { conversation_id: conversationId || undefined } })
    .then((r) => r.data)
export const testRemote = (
  target: ProbeTarget,
  conversationId: string | null,
  context: {run_id?:string;source_revision_id?:string} = {}
): Promise<ProbeResult> =>
  http
    .post('/probe', {
      service: target.service,
      expected_config: target.expected_config,
      conversation_id: conversationId,
      ...context,
    })
    .then((r) => r.data)
