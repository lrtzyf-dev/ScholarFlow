import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { ensureSourceConversation, sourceError } from './sourceApi'
import type { ResearchRunSummary } from './researchRunApi'
import {
  applyMatrixRun,
  cancelMatrixRun,
  createMatrixRun,
  getMatrixRun,
  listMatrixRuns,
  resumeMatrixRun,
  type MatrixInputs,
  type MatrixRun,
  type MatrixRunCreate,
} from './matrixRunApi'

const pendingKey = (cid: string) => 'scholarflow:matrix-pending-create:' + cid
const identity = (value: MatrixRun | null) =>
  value
    ? JSON.stringify([
        value.run_id,
        value.revision,
        value.attempt_id,
        value.attempt_number,
        value.requires_unknown_confirmation,
      ])
    : ''
function statusOf(err: unknown) {
  return (err as { response?: { status?: number } })?.response?.status
}
function savedCreate(cid: string): MatrixRunCreate | null {
  try {
    const value = JSON.parse(localStorage.getItem(pendingKey(cid)) || 'null')
    return value?.conversation_id === cid &&
      /^research_[a-f0-9]{32}$/.test(value.run_id) &&
      typeof value.request_id === 'string' &&
      typeof value.matrix_id === 'string' &&
      Number.isInteger(value.expected_version)
      ? value
      : null
  } catch {
    return null
  }
}

export function useMatrixRuns(
  conversation: Ref<string>,
  restore: (value: MatrixRun) => void | Promise<void>
) {
  const job = ref<MatrixRun | null>(null),
    jobs = ref<ResearchRunSummary[]>([]),
    selected = ref(''),
    error = ref(''),
    submitting = ref(false),
    consent = ref('')
  const pendingCreate = ref<MatrixRunCreate | null>(null)
  const rejectedCreate = ref('')
  const loading = computed(() => !!job.value && ['queued', 'running'].includes(job.value.status))
  // Consent belongs to exactly the unknown state the user inspected.
  const retryUnknown = computed({
    get: () => !!job.value?.requires_unknown_confirmation && consent.value === identity(job.value),
    set: (checked: boolean) => {
      consent.value = checked ? identity(job.value) : ''
    },
  })
  let epoch = 0,
    observation = 0,
    historySequence = 0,
    failures = 0,
    observedId = '',
    disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined, request: AbortController | undefined
  let pendingResume: {
    id: string
    revision: number
    attempt: string
    request: string
    retry: boolean
  } | null = null
  let pendingApply: { id: string; version: number; request: string } | null = null
  function stop() {
    ++observation
    if (timer) clearTimeout(timer)
    timer = undefined
    request?.abort()
    request = undefined
  }
  function reset() {
    ++epoch
    ++historySequence
    stop()
    failures = 0
    observedId = ''
    job.value = null
    jobs.value = []
    selected.value = ''
    error.value = ''
    submitting.value = false
    consent.value = ''
    pendingResume = null
    pendingApply = null
    pendingCreate.value = savedCreate(conversation.value)
    rejectedCreate.value = ''
  }
  function clearCreate(value: MatrixRun) {
    if (
      pendingCreate.value?.run_id !== value.run_id ||
      pendingCreate.value.conversation_id !== value.conversation_id
    )
      return
    try {
      localStorage.removeItem(pendingKey(value.conversation_id))
    } catch {
      /* Retaining the same idempotent request is safe. */
    }
    pendingCreate.value = null
    rejectedCreate.value = ''
  }
  function accept(value: MatrixRun, restoreInput = false) {
    if (value.conversation_id !== conversation.value) return
    if (job.value?.run_id === value.run_id && value.revision < job.value.revision) return
    if (identity(value) !== identity(job.value)) consent.value = ''
    if (pendingResume?.id === value.run_id && value.attempt_id !== pendingResume.attempt)
      pendingResume = null
    if (pendingApply?.id === value.run_id && value.result?.applied_version) pendingApply = null
    job.value = value
    selected.value = value.run_id
    clearCreate(value)
    if (restoreInput) void restore(value)
  }
  async function read(id: string, restoreInput = false) {
    if (!id || disposed) return
    const token = epoch,
      cid = conversation.value
    stop()
    observedId = id
    const observed = observation
    request = new AbortController()
    try {
      const value = await getMatrixRun(id, cid, request.signal)
      if (
        token !== epoch ||
        observed !== observation ||
        cid !== conversation.value ||
        value.run_id !== id
      )
        return
      error.value = ''
      failures = 0
      accept(value, restoreInput)
      if (loading.value && !submitting.value) timer = setTimeout(() => void read(id), 1000)
    } catch (err) {
      const code = (err as { code?: string })?.code
      if (token !== epoch || observed !== observation || code === 'ERR_CANCELED') return
      error.value = sourceError(err)
      const status = statusOf(err)
      if ((!status || status >= 500) && !submitting.value)
        timer = setTimeout(
          () => void read(id, restoreInput),
          Math.min(1000 * 2 ** failures++, 10000)
        )
    }
  }
  async function history(requested = '', latest = false) {
    const token = epoch,
      cid = conversation.value,
      sequence = ++historySequence,
      observed = observation
    try {
      await ensureSourceConversation(cid)
      if (token !== epoch || submitting.value || sequence !== historySequence) return
      const value = await listMatrixRuns(cid)
      if (token !== epoch || submitting.value || sequence !== historySequence) return
      jobs.value = value.items
      const id = requested || (latest ? value.items[0]?.run_id : '')
      if (id && observed === observation) await read(id, true)
    } catch (err) {
      if (token === epoch && sequence === historySequence) error.value = sourceError(err)
    }
  }
  async function mutate(
    action: () => Promise<MatrixRun>,
    target: string,
    done?: () => void,
    rejected?: (status?: number) => void,
    restoreInput = false
  ) {
    if (submitting.value || disposed) return
    const token = epoch,
      cid = conversation.value
    submitting.value = true
    error.value = ''
    stop()
    try {
      const value = await action()
      if (token !== epoch || cid !== conversation.value) return
      done?.()
      consent.value = ''
      accept(value, restoreInput)
      const list = await listMatrixRuns(cid)
      if (token === epoch && cid === conversation.value) jobs.value = list.items
    } catch (err) {
      if (token === epoch && cid === conversation.value) {
        rejected?.(statusOf(err))
        error.value = sourceError(err)
      }
    } finally {
      if (token === epoch && cid === conversation.value) {
        submitting.value = false
        void read(target)
      }
    }
  }
  function retryCreate() {
    const captured = pendingCreate.value
    if (!captured || captured.conversation_id !== conversation.value || submitting.value) return
    return mutate(
      async () => {
        await ensureSourceConversation(captured.conversation_id)
        return createMatrixRun(captured)
      },
      captured.run_id,
      undefined,
      (status) => {
        if (status && status >= 400 && status < 500) rejectedCreate.value = captured.run_id
      },
      true
    )
  }
  function create(inputs: MatrixInputs) {
    if (loading.value || submitting.value || pendingCreate.value) return
    const captured: MatrixRunCreate = {
      ...structuredClone(inputs),
      conversation_id: conversation.value,
      run_id: 'research_' + crypto.randomUUID().replaceAll('-', ''),
      request_id: crypto.randomUUID(),
    }
    try {
      localStorage.setItem(pendingKey(captured.conversation_id), JSON.stringify(captured))
    } catch {
      error.value = '待确认任务编号无法保存，提取尚未发送。请检查本地存储后重试。'
      return
    }
    pendingCreate.value = captured
    return retryCreate()
  }
  async function releaseRejectedCreate() {
    const captured = pendingCreate.value,
      token = epoch
    if (!captured || rejectedCreate.value !== captured.run_id || submitting.value) return
    submitting.value = true
    try {
      const existing = await getMatrixRun(captured.run_id, captured.conversation_id)
      if (token === epoch) accept(existing, true)
    } catch (err) {
      if (token !== epoch) return
      if (statusOf(err) === 404) {
        try {
          localStorage.removeItem(pendingKey(captured.conversation_id))
        } catch {
          error.value = '待确认记录未能清理，请检查本地存储后重试。'
          return
        }
        pendingCreate.value = null
        rejectedCreate.value = ''
        error.value = ''
      } else error.value = sourceError(err)
    } finally {
      if (token === epoch) submitting.value = false
    }
  }
  function resume() {
    const value = job.value,
      cid = conversation.value
    if (
      !value?.can_resume ||
      submitting.value ||
      (value.requires_unknown_confirmation && !retryUnknown.value)
    )
      return
    if (
      !pendingResume ||
      pendingResume.id !== value.run_id ||
      pendingResume.revision !== value.revision ||
      pendingResume.attempt !== value.attempt_id ||
      pendingResume.retry !== retryUnknown.value
    )
      pendingResume = {
        id: value.run_id,
        revision: value.revision,
        attempt: value.attempt_id,
        request: crypto.randomUUID(),
        retry: retryUnknown.value,
      }
    const captured = pendingResume
    return mutate(
      () => resumeMatrixRun(captured.id, cid, captured.revision, captured.request, captured.retry),
      captured.id,
      () => {
        pendingResume = null
      }
    )
  }
  function apply(version: number) {
    const value = job.value,
      cid = conversation.value
    if (!value?.result || loading.value || submitting.value) return
    if (!pendingApply || pendingApply.id !== value.run_id)
      pendingApply = { id: value.run_id, version, request: crypto.randomUUID() }
    const captured = pendingApply
    return mutate(
      () => applyMatrixRun(captured.id, cid, captured.version, captured.request),
      captured.id,
      () => {
        pendingApply = null
      },
      (status) => {
        if (status && status >= 400 && status < 500) pendingApply = null
      }
    )
  }
  function cancel() {
    const value = job.value,
      cid = conversation.value
    if (value && !submitting.value)
      return mutate(
        () => cancelMatrixRun(value.run_id, cid, value.revision, value.attempt_id),
        value.run_id
      )
  }
  function select() {
    if (submitting.value) return
    const id = selected.value
    ++epoch
    stop()
    pendingResume = null
    pendingApply = null
    consent.value = ''
    error.value = ''
    job.value = null
    return read(id, true)
  }
  function inspectPending() {
    const value = pendingCreate.value
    if (!value || submitting.value) return
    ++epoch
    stop()
    consent.value = ''
    job.value = null
    selected.value = value.run_id
    return read(value.run_id, true)
  }
  function refresh() {
    if (submitting.value) return
    error.value = ''
    return job.value
      ? read(job.value.run_id)
      : observedId
        ? read(observedId, true)
        : history('', true)
  }
  watch(conversation, reset, { flush: 'sync' })
  onBeforeUnmount(() => {
    disposed = true
    ++epoch
    stop()
  })
  return {
    job,
    jobs,
    selected,
    error,
    submitting,
    retryUnknown,
    loading,
    pendingCreate,
    rejectedCreate,
    releaseRejectedCreate,
    reset,
    history,
    create,
    retryCreate,
    resume,
    apply,
    cancel,
    select,
    refresh,
    inspectPending,
  }
}
