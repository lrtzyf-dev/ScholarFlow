import axios from 'axios'
import type {
  ResearchChild,
  ResearchChildGrant,
  ResearchChildGrantReceipt,
} from './researchChildTypes'

const http = axios.create({ baseURL: '/api/research/children', timeout: 30000 })
export const listResearchChildren = (
  conversationId: string,
  parentRunId: string,
  signal?: AbortSignal
) =>
  http
    .get<{ items: ResearchChild[] }>('', {
      params: { conversation_id: conversationId, parent_run_id: parentRunId },
      signal,
    })
    .then((response) => response.data)

export const grantResearchChildRetry = (
  grant: ResearchChildGrant
): Promise<ResearchChildGrantReceipt> => {
  const { child_run_id, ...body } = grant
  return http
    .post<ResearchChildGrantReceipt>(`/${encodeURIComponent(child_run_id)}/retry-grants`, body)
    .then((response) => response.data)
}

export function researchChildError(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof detail === 'string' ? detail : '科研子任务状态或许可未能确认，请刷新后重试。'
}
