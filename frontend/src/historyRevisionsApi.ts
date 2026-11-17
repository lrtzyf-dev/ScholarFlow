import axios from 'axios'
export interface HistoryRevision {
  id: string
  source_conversation_id: string
  source_record_id: string
  branch_conversation_id: string
  mode: 'answer_only' | 'new_task'
  revised_question: string
  submitted_question: string | null
  run_id: string | null
  origin_deleted: boolean
}
export const createHistoryRevision = (cid: string, record: string, question: string, mode: HistoryRevision['mode'], requestId: string) =>
  axios.post<HistoryRevision>(`/api/conversations/${encodeURIComponent(cid)}/revisions`, {record_id: record, question, mode, request_id: requestId}).then(r => r.data)
export const getHistoryRevision = (cid: string) => axios.get<{revision: HistoryRevision | null}>(`/api/conversations/${encodeURIComponent(cid)}/revision-origin`).then(r => r.data.revision)
