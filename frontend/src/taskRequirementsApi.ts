import axios from 'axios'

export type ResultContract = {
  kind: 'manual' | 'answer_terms' | 'command_result' | 'artifact_result' | 'artifact_export_result' | 'policy'
  contains?: string[]
  excludes?: string[]
  command?: string
  path?: string
  min_fixed_sources?: number
  rule?: string
  format?: 'docx' | 'xlsx' | 'svg' | 'bundle'
  artifact_id?: string
  revision_id?: string
  requires_recorded_execution?: boolean
}
export interface TaskRequirement {
  id: string
  title: string
  status: string
  origin: string
  required: boolean
  source_message_id: string
  tool_arguments: { result_contract?: ResultContract; contract?: { kind: string } } | null
  evidence: { kind: string; passed?: boolean; semantic_verification?: string; reference?: string;
    export_id?: string; artifact_id?: string; revision_id?: string; payload_sha256?: string; payload_size?: number; format?: string }[]
}
export interface RequirementSnapshot {
  id: string
  revision: number
  outcome: string
  description: string
  requirements: TaskRequirement[]
  revisions: {
    revision: number
    source: string
    action: string
    reason: string
    created_at: string | null
    changes: { request?: { action: string; title?: string; reason?: string } }
  }[]
  input_coverage: { candidates: { candidate_id: string; quote: string; quoted_context: boolean }[] }
}
export interface RequirementChange {
  conversation_id: string
  expected_revision: number
  request_id: string
  action: 'add' | 'replace' | 'cancel' | 'confirm' | 'reopen' | 'blocked'
  title?: string
  contract?: ResultContract
  requirement_id?: string
  candidate_id?: string
  reason?: string
}
export const getTaskRequirements = (
  id: string,
  conversation: string
): Promise<RequirementSnapshot> =>
  axios
    .get(`/api/goal/${encodeURIComponent(id)}/requirements`, {
      params: { conversation_id: conversation },
    })
    .then((r) => r.data)
export const changeTaskRequirements = (
  id: string,
  body: RequirementChange
): Promise<RequirementSnapshot> =>
  axios.post(`/api/goal/${encodeURIComponent(id)}/requirements`, body).then((r) => r.data)
