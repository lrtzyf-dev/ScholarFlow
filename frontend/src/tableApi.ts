import axios from 'axios'
import type { SourceRevision } from './sourceApi'

export interface CsvOptions {
  encoding: 'auto' | 'utf-8-sig' | 'utf-16' | 'gb18030' | 'cp1252'
  delimiter: 'auto' | 'comma' | 'semicolon' | 'tab' | 'pipe'
  header_row: number
}
export interface CsvPreview {
  candidates: { encoding: string; delimiter: string; rows: string[][] }[]
  recommended_encoding: string | null
  raw_sha256: string
}
export interface TableComputation {
  id: string
  revision_id: string
  created_at: string
  execution_performed: boolean
  config: {
    sheet: string
    cell_range: string
    absolute_tolerance: number
    relative_tolerance: number
  }
  result: {
    status: string
    formula_count: number
    computed_count: number
    unresolved_count: number
    outputs: {
      sheet: string
      cell: string
      formula: string
      status: string
      value: number | null
      cached_value?: unknown
      cache_comparison?: string
      reason?: string
    }[]
  }
}
const root = (rid: string) => `/api/sources/revisions/${encodeURIComponent(rid)}`
export const previewCsv = (
  rid: string,
  conversationId: string,
  options: CsvOptions
): Promise<CsvPreview> =>
  axios
    .post(root(rid) + '/csv-preview', { conversation_id: conversationId, ...options })
    .then((r) => r.data)
export const interpretCsv = (
  rid: string,
  conversationId: string,
  options: CsvOptions,
  expectedVersion: number,
  requestId: string
): Promise<SourceRevision> =>
  axios
    .post(root(rid) + '/csv-interpret', {
      conversation_id: conversationId,
      ...options,
      expected_version: expectedVersion,
      request_id: requestId,
    })
    .then((r) => r.data)
export const tableHistory = (
  rid: string,
  conversationId: string
): Promise<{ items: TableComputation[] }> =>
  axios
    .get(root(rid) + '/table-computations', { params: { conversation_id: conversationId } })
    .then((r) => r.data)
export const computeTable = (
  rid: string,
  conversationId: string,
  fingerprint: string,
  config: TableComputation['config'],
  requestId: string
): Promise<TableComputation> =>
  axios
    .post(root(rid) + '/table-computations', {
      conversation_id: conversationId,
      expected_fingerprint: fingerprint,
      request_id: requestId,
      ...config,
    })
    .then((r) => r.data)
export const tableBundleUrl = (rid: string, conversationId: string, computationId: string) =>
  root(rid) +
  `/table-computations/${encodeURIComponent(computationId)}/bundle?${new URLSearchParams({ conversation_id: conversationId })}`
