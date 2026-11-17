import axios from 'axios'
const http = axios.create({ baseURL: '/api' })
export interface BibliographyEntry {
  id: string
  version: number
  position: number
  status: string
  error: string
  raw_entry: string
  metadata: Record<string, unknown>
  existing_metadata: Record<string, unknown> | null
  candidate_paper_id: string | null
  paper_id: string | null
  decision: string | null
}
export interface BibliographyBatch {
  id: string
  title: string
  format: string
  source_revision_id: string
  items: BibliographyEntry[]
}
export interface ZoteroBinding {
  id: string
  version: number
  user_id: string
  collection_key: string
  collection_name: string
  status: string
  cursor: number
  last_batch_id: string | null
  last_error: string
  access: { files?: boolean }
}
export interface ZoteroItem {
  id: string
  key: string
  version: number
  state: string
  title: string
  item_type: string
  parent_key: string | null
  content_type: string | null
  link_mode: string
  source_revision_id: string | null
  fulltext_status: string
}
export const bibliography = {
  preview: (input: unknown): Promise<BibliographyBatch> =>
    http.post('/bibliography/preview', input).then((r) => r.data),
  batches: (
    conversation_id: string
  ): Promise<{ items: { id: string; title: string; format: string }[] }> =>
    http.get('/bibliography/batches', { params: { conversation_id } }).then((r) => r.data),
  batch: (id: string, conversation_id: string): Promise<BibliographyBatch> =>
    http.get(`/bibliography/batches/${id}`, { params: { conversation_id } }).then((r) => r.data),
  entry: (id: string, operation: string, input: unknown): Promise<BibliographyEntry> =>
    http.post(`/bibliography/entries/${id}/${operation}`, input).then((r) => r.data),
  collections: (
    input: unknown
  ): Promise<{ items: { key: string; name: string }[]; next_start: number | null }> =>
    http.post('/zotero/collections', input).then((r) => r.data),
  connect: (input: unknown): Promise<ZoteroBinding> =>
    http.post('/zotero/bindings', input).then((r) => r.data),
  bindings: (conversation_id: string): Promise<{ items: ZoteroBinding[] }> =>
    http.get('/zotero/bindings', { params: { conversation_id } }).then((r) => r.data),
  state: (id: string, input: unknown): Promise<ZoteroBinding> =>
    http.post(`/zotero/bindings/${id}/state`, input).then((r) => r.data),
  sync: (
    id: string,
    input: unknown
  ): Promise<{ binding: ZoteroBinding; batch_id: string | null; changed_items: number }> =>
    http.post(`/zotero/bindings/${id}/sync`, input).then((r) => r.data),
  items: (
    id: string,
    conversation_id: string,
    offset = 0
  ): Promise<{ items: ZoteroItem[]; next_offset: number | null }> =>
    http
      .get(`/zotero/bindings/${id}/items`, { params: { conversation_id, offset } })
      .then((r) => r.data),
  file: (
    binding: string,
    item: string,
    operation: string,
    input: unknown
  ): Promise<{ revision_id?: string; paper_id?: string }> =>
    http.post(`/zotero/bindings/${binding}/items/${item}/${operation}`, input).then((r) => r.data),
  versions: (
    binding: string,
    item: string,
    conversation_id: string,
    offset = 0
  ): Promise<{
    items: { remote_version: number; source_revision_id: string }[]
    next_offset: number | null
  }> =>
    http
      .get(`/zotero/bindings/${binding}/items/${item}/versions`, {
        params: { conversation_id, offset },
      })
      .then((r) => r.data),
}
