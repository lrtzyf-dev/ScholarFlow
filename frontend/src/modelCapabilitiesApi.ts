import axios from 'axios'
import type { ModelCapability } from './api'

export type CapabilityName = 'text' | 'streaming' | 'tools' | 'vision' | 'reasoning' | 'structured' | 'usage' | 'native_tools' | 'files'
export interface ProbeRemoteFile { state: string; id?: string; requested_ttl_seconds?: number; expires_at?: number; cleanup_note?: string }
export interface CapabilityResult {
  diagnostic_id: string
  profile_id: string
  profile_version: number
  model_id: string
  capability: CapabilityName
  status: ModelCapability
  failure_kind: string
  detail: string
  tested_at: string
  request_count: number
  endpoint?: string
  protocol?: string
  remote_file?: ProbeRemoteFile | null
  cost_summary?: {
    real_call_count: number
    synthetic_call_count: number
    unknown_amount_calls: number
    pricing: { complete_estimates: Record<string, string>; partial_estimates: Record<string, string>; user_confirmed: Record<string, string> }
  }
}
export interface CapabilityPreview {
  profile_id: string
  profile_version: number
  model_id: string
  expected_config: string
  remote_requests: number
  capabilities: Record<CapabilityName, ModelCapability>
  results: Partial<Record<CapabilityName, CapabilityResult>>
  cleanup_operations: { diagnostic_id: string; remote_file: ProbeRemoteFile }[]
  probes: { capability: CapabilityName; label: string; max_requests: number; sends: string }[]
}

const http = axios.create({ baseURL: '/api/model-profiles' })
export const previewCapabilities = (id: string): Promise<CapabilityPreview> =>
  http.get(`/${encodeURIComponent(id)}/capabilities`).then((response) => response.data)
export const probeCapability = (
  id: string,
  capability: CapabilityName,
  expectedConfig: string,
  diagnosticId: string
): Promise<CapabilityResult> =>
  http.post(`/${encodeURIComponent(id)}/capabilities`, {
    capability,
    expected_config: expectedConfig,
    diagnostic_id: diagnosticId,
  }).then((response) => response.data)
export const cancelCapability = (id: string, diagnosticId: string): Promise<{ detail: string }> =>
  http.post(`/${encodeURIComponent(id)}/capabilities/${encodeURIComponent(diagnosticId)}/cancel`).then(r => r.data)
export const cleanupCapability = (id: string, diagnosticId: string): Promise<{ remote_file: ProbeRemoteFile; request_count: number }> =>
  http.post(`/${encodeURIComponent(id)}/capabilities/${encodeURIComponent(diagnosticId)}/cleanup`).then(r => r.data)
