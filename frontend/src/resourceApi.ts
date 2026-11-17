import axios from 'axios'
const http = axios.create({ baseURL: '/api' })

export interface ResourcePolicy {
  currency: string
  amount_limit: string | null
  request_estimate: string | null
  active_seconds_limit: number | null
  price_version?: string | null
}
export interface ResourceBudget {
  id: string
  owner_run_id: string
  currency: string
  revision: number
  amount_limit: string | null
  spent_amount: string
  estimated_spent_amount?: string
  confirmed_spent_amount?: string
  reserved_amount: string
  remaining_amount: string | null
  active_seconds_limit: number | null
  active_seconds: number
  pending_reconciliation: number
  time_scope: string
  remote_hard_cap: boolean
}
export interface ResourceSettings {
  revision: number
  policy: ResourcePolicy
  budgets: ResourceBudget[]
  timings?: RunTiming[]
  queue: {
    id: string
    state: 'queued' | 'active'
    reason: string | null
    wait_seconds: number | null
  }[]
}
export interface RunTiming {
  run_id: string
  attempt_id?: string
  available: boolean
  reason?: string
  elapsed_wall_seconds?: number | null
  confirmed_task_wall_seconds?: number | null
  active_wall_seconds?: number | null
  queue_wall_seconds?: number | null
  approval_wait_seconds?: number
  user_wait_seconds?: number
  offline_or_unknown_seconds?: number | null
  request_execution_sum_seconds?: number
  request_execution_wall_seconds?: number | null
  request_windows?: number
  duplicate_call_windows?: number
  queue_only_requests?: number
  open_segments?: number
  last_confirmed_at?: string
  clock_adjustment_detected?: boolean
}
export const getResources = (cid: string): Promise<ResourceSettings> =>
  http.get('/conversation-resources/' + encodeURIComponent(cid)).then((r) => r.data)
export const saveResources = (
  cid: string,
  policy: ResourcePolicy,
  revision: number
): Promise<Pick<ResourceSettings, 'revision' | 'policy'>> => {
  const { price_version: _version, ...input } = policy
  return http
    .put('/conversation-resources/' + encodeURIComponent(cid), {
      ...input,
      amount_limit: input.amount_limit === null ? null : String(input.amount_limit),
      request_estimate: input.request_estimate === null ? null : String(input.request_estimate),
      expected_revision: revision,
    })
    .then((r) => r.data)
}
export const increaseResources = (
  cid: string,
  budget: ResourceBudget,
  amount: string | null,
  seconds: number | null
): Promise<ResourceBudget> =>
  http
    .put(
      '/conversation-resources/' +
        encodeURIComponent(cid) +
        '/budgets/' +
        encodeURIComponent(budget.id),
      {
        expected_revision: budget.revision,
        amount_limit: amount === null ? null : String(amount),
        active_seconds_limit: seconds,
      }
    )
    .then((r) => r.data)
