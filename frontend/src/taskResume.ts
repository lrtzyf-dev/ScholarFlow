/** A background resume is the same durable admission after a lost reply. */
export function durableTaskResumeRequest(
  storage: Pick<Storage, 'getItem' | 'setItem'>,
  conversationId: string,
  runId: string,
  expectedRevision: number | null | undefined,
  resolution: 'retry' | 'skip' | undefined,
  newIdentity: () => string
) {
  if (!Number.isSafeInteger(expectedRevision) || (expectedRevision ?? 0) < 1)
    throw new Error('执行版本尚未载入，请刷新任务后再继续')
  const key = `scholarflow:pending-task-resume:${conversationId}:${runId}`
  const raw = storage.getItem(key)
  if (raw) {
    let value: Record<string, unknown>
    try { value = JSON.parse(raw) } catch { throw new Error('上次恢复记录无法核对，尚未启动新的恢复请求') }
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        typeof value.resume_request_id !== 'string' || !/^[A-Za-z0-9_-]{8,96}$/.test(value.resume_request_id) ||
        !Number.isSafeInteger(value.expected_run_revision) || Number(value.expected_run_revision) < 1 ||
        ![undefined, 'retry', 'skip'].includes(value.resolution as undefined | 'retry' | 'skip'))
      throw new Error('上次恢复记录无法核对，尚未启动新的恢复请求')
    if (value.resolution !== resolution)
      throw new Error('上次恢复结果尚未确认，请先按原选择重试以核对状态')
    return { key, request: { resume_request_id: value.resume_request_id,
      expected_run_revision: value.expected_run_revision as number }, resolution }
  }
  const request = { resume_request_id: newIdentity(), expected_run_revision: expectedRevision as number }
  storage.setItem(key, JSON.stringify({ ...request, resolution }))
  return { key, request, resolution }
}
