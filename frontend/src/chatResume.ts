/** Persist an admission identity before sending, including across a lost reply/reload. */
export function durableResumeRequest(
  storage: Pick<Storage, 'getItem' | 'setItem'>,
  conversationId: string,
  runId: string,
  expectedRevision: number | undefined,
  newIdentity: () => string
) {
  const key = `scholarflow:pending-resume:${conversationId}:${runId}`
  const saved = storage.getItem(key)
  if (saved) {
    try {
      const value = JSON.parse(saved)
      if (typeof value.resume_request_id === 'string' &&
          /^[A-Za-z0-9_-]{8,96}$/.test(value.resume_request_id) &&
          (value.expected_run_revision === undefined ||
            Number.isSafeInteger(value.expected_run_revision) && value.expected_run_revision >= 0))
        return { key, request: { resume_request_id: value.resume_request_id as string,
          expected_run_revision: value.expected_run_revision as number | undefined } }
    } catch { /* Replace a damaged local receipt before sending. */ }
  }
  const request = { resume_request_id: newIdentity(), expected_run_revision: expectedRevision }
  storage.setItem(key, JSON.stringify(request))
  return { key, request }
}
