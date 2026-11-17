import axios from 'axios'

const http = axios.create({ baseURL: '/api/memory' })
export interface MemoryItem {
  id: string
  question: string
  state: 'included' | 'excluded' | 'deleted' | 'not_captured'
  edited: boolean
  text: string
  revision: number
}
export interface MemoryLoad {
  run_id: string
  attempt_id: string | null
  generation: number
  stale: boolean
  entries: { id: string; digest: string; mode: 'summary' | 'recent'; edited: boolean }[]
  updated_at: string
}
export interface MemoryItems {
  revision: number
  fingerprint: string
  items: MemoryItem[]
  total: number
  next_offset: number | null
  loads: MemoryLoad[]
}
export const getMemoryItems = (cid: string, offset = 0): Promise<MemoryItems> =>
  http
    .get(`/${encodeURIComponent(cid)}/items`, { params: { offset, limit: 20 } })
    .then((r) => r.data)
export const editMemoryItem = (
  cid: string,
  id: string,
  revision: number,
  action: 'edit' | 'include' | 'exclude' | 'delete',
  text?: string
) =>
  http
    .post(`/${encodeURIComponent(cid)}/items/${encodeURIComponent(id)}`, {
      expected_revision: revision,
      action,
      text,
    })
    .then((r) => r.data)
