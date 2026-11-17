import axios from 'axios'
export interface StoragePolicyValues {
  backup_reminder_days: number
  archived_review_days: number
  backup_review_days: number
  capacity_warning_bytes: number
  cleanup_receipt_review_days: number
}
export interface StoragePolicy {
  revision: number
  values: StoragePolicyValues
  automatic_deletion: false
}
export interface CleanupPreview {
  conversation_id: string
  total_records: number
  records: Record<string, number>
  can_delete: boolean
  cloud_file_blockers: { state: string; reason: string }[]
  storage_preview: { logical_payload_bytes: number; by_table: Record<string, number> }
  preserved: string[]
}
export interface CleanupReceipt {
  id: string
  created_at: string
  fingerprint: string
  review_due: boolean
  document: {
    total_records: number
    records: Record<string, number>
    storage_preview: {
      logical_payload_bytes: number
      categories?: { label: string; records: number; logical_payload_bytes: number }[]
    }
    remote_erasure_verified: false
    backups_erased: false
  }
}
export interface StorageStatus {
  policy: StoragePolicy
  measured_at: string
  storage: {
    application_file_bytes: number
    by_category: Record<string, number>
    recovery_file_bytes: number
    total_file_bytes: number
    skipped_entries: number
  }
  reminders: {
    backup_due: boolean
    last_successful_backup: string | null
    capacity_warning: boolean
    old_backup_count: number
    old_receipt_count: number
  }
  archived_review: { items: { id: string; title: string; archived_at: string }[]; more: boolean }
  backup_review: { id: string; size_bytes: number; age_days: number }[]
}
const http = axios.create({ baseURL: '/api/storage' })
export const getStorageStatus = (): Promise<StorageStatus> => http.get('').then((r) => r.data)
export const saveStoragePolicy = (
  expected_revision: number,
  values: StoragePolicyValues
): Promise<StoragePolicy> =>
  http.put('/policy', { expected_revision, ...values }).then((r) => r.data)
export const getCleanupReceipts = (
  cursor?: string
): Promise<{ items: CleanupReceipt[]; next_cursor: string | null }> =>
  http.get('/cleanup-receipts', { params: { cursor } }).then((r) => r.data)
