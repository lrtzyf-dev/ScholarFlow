import axios from 'axios'

export interface RestorePreview {
  id: string
  file_count: number
  total_bytes: number
  created_at: string
  adjustments: string[]
  rollback_of?: string
  credential_portability?: string
}
export interface BackupJob {
  id: string
  kind: string
  status: string
  created_at: string
  error: string
  progress: { completed_files: number; total_files: number } | null
  result:
    (Partial<RestorePreview> & { download_url?: string; size?: number; sha256?: string }) | null
}
export interface BackupState {
  jobs: BackupJob[]
  pending_restore: { id: string; status: string } | null
  last_restore: { id: string; status: string; restored_at: string } | null
  maintenance_reason: string
  credential_portability?: string
}
export const getBackups = async () => (await axios.get<BackupState>('/api/backups')).data
export const createBackup = async () => (await axios.post<BackupJob>('/api/backups/create')).data
export const prepareRollback = async () =>
  (await axios.post<BackupJob>('/api/backups/rollback/prepare')).data
export const prepareRestore = async (file: File) => {
  const body = new FormData()
  body.append('file', file)
  return (await axios.post<BackupJob>('/api/backups/restore/prepare', body)).data
}
export const confirmRestore = async (restoreId: string) =>
  (
    await axios.post<BackupJob>('/api/backups/restore/confirm', {
      restore_id: restoreId,
      confirm_replace: true,
    })
  ).data
export const cancelRestore = async () =>
  (await axios.post<BackupJob>('/api/backups/restore/cancel')).data
export function backupError(error: unknown) {
  if (axios.isAxiosError(error) && typeof error.response?.data?.detail === 'string')
    return error.response.data.detail
  return '操作未完成，请检查后端连接后重试。'
}
