import axios from 'axios'
import type { PricedCall } from './pricingApi'

export interface ChildProgress {
  completed_reads: number
  tool_index: number
  queued_tools: number
  tools: string[]
  pending: { name: string; arguments: unknown; fingerprint: string } | null
  archived_results: number
  loaded_skills: { name: string; revision: string }[]
}
export interface ChildResult { findings: string; outcome: string | null; incomplete: string[]; semantic_verified: false; summary_is_completion_proof: false }
export interface ChildNode {
  id: string; parent_run_id: string; state: string; attempt: number; topic: string; goal: string
  budget_id: string | null; progress: ChildProgress; result: ChildResult; updated_at: string; cancel_requested: boolean
}
export interface ChildTree {
  parent: { id: string; kind: string; conversation_id: string | null; revision: number; status: string; active: boolean; title: string }
  recovery: { safe_children: boolean; reason: string; requires_parent_admission: true; can_reconcile: boolean; message: string }
  items: ChildNode[]; total: number; next_offset: number | null
}
export interface ChildAttempts {
  node: ChildNode
  items: { number: number; state: string; progress: ChildProgress; result: ChildResult; created_at: string; updated_at: string }[]
  total: number; next_offset: number | null
  resolutions: { id: string; attempt: number; action: string; reference: string; created_at: string; source: string; provider_verified: false }[]
}
export const getChildTree = (runId: string, offset = 0): Promise<ChildTree> =>
  axios.get('/api/subtasks/runs/' + encodeURIComponent(runId), { params: { offset } }).then(r => r.data)
export const getChildAttempts = (nodeId: string, offset = 0): Promise<ChildAttempts> =>
  axios.get('/api/subtasks/nodes/' + encodeURIComponent(nodeId) + '/attempts', { params: { offset } }).then(r => r.data)
export interface ChildCalls { items: PricedCall[]; next_offset: number | null; notice: string }
export const getChildCalls = (nodeId: string, attempt: number, offset = 0): Promise<ChildCalls> =>
  axios.get('/api/subtasks/nodes/' + encodeURIComponent(nodeId) + '/calls', { params: { attempt, offset } }).then(r => r.data)
export const reconcileChild = (nodeId: string, body: { request_id: string; expected_parent_revision: number; expected_attempt: number; operation_fingerprint: string; action: 'retry' | 'skip'; reference: string }) =>
  axios.post('/api/subtasks/nodes/' + encodeURIComponent(nodeId) + '/reconcile', body).then(r => r.data)
