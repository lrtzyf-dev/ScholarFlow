export interface ResearchChildUsage {
  call_count: number
  actual_usage: Record<string, number>
  estimated_usage?: Record<string, number>
  unknown_usage_calls: number
  unknown_amount_calls?: number
  amount_complete?: boolean
  pricing?: {
    complete_estimates: Record<string, string>
    partial_estimates: Record<string, string>
    user_confirmed: Record<string, string>
    unknown_calls: number
  }
}

export interface ResearchChild {
  run_id: string
  kind: string
  conversation_id: string
  revision: number
  attempt_id: string
  attempt_number: number
  status: string
  outcome: string
  stage: string
  error: string
  parent_run_id: string
  parent_goal_id: string
  parent_revision: number | null
  parent_attempt_id: string | null
  parent_status: string | null
  link_id: string
  input_sha256: string
  pending_sha256: string | null
  requires_unknown_confirmation: boolean
  unknown_retry_approved: boolean
  retry_grant_id: string | null
  managed_by_parent: true
  can_resume: false
  resume_from: 'parent_run'
  delivery_verified: boolean
  usage: ResearchChildUsage
  updated_at: string
}

export interface ResearchChildGrant {
  conversation_id: string
  parent_run_id: string
  child_run_id: string
  expected_parent_revision: number
  expected_parent_attempt_id: string
  expected_child_attempt_id: string
  pending_sha256: string
  request_id: string
  confirm_duplicate_cost: true
}

export interface ResearchChildGrantReceipt {
  grant_id: string
  child_run_id: string
  parent_run_id: string
  pending_sha256: string
  replayed: boolean
  message: string
}
