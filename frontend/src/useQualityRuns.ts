import { computed, onBeforeUnmount, ref, type Ref } from 'vue'
import { ensureSourceConversation, sourceError } from './sourceApi'
import type { ResearchRunSummary } from './researchRunApi'
import {
  cancelQualityRun,
  createQualityRun,
  getQualityRun,
  listQualityRuns,
  resumeQualityRun,
  type QualityRun,
  type QualityInputs,
  type QualityCreate,
} from './qualityRunApi'

export function useQualityRuns(
  conversation: Ref<string>,
  restore: (value: QualityRun) => void
) {
  const job = ref<QualityRun | null>(null),
    jobs = ref<ResearchRunSummary[]>([]),
    selected = ref(''),
    error = ref(''),
    submitting = ref(false),
    retryUnknown = ref(false)
  const loading = computed(() => !!job.value && ['queued', 'running'].includes(job.value.status))
  let epoch = 0,
    observation = 0,
    timer: ReturnType<typeof setTimeout> | undefined,
    request: AbortController | undefined
  let observedId = ''
  let pendingCreate: QualityCreate | null = null,
    pendingResume: { id: string; revision: number; request: string; retry: boolean } | null = null
  function stop() {
    ++observation
    if (timer) clearTimeout(timer)
    request?.abort()
  }
  function reset(keepHistory = false) {
    ++epoch
    stop()
    job.value = null
    selected.value = ''
    error.value = ''
    submitting.value = false
    retryUnknown.value = false
    pendingCreate = null
    pendingResume = null
    observedId = ''
    if (!keepHistory) jobs.value = []
  }
  function accept(value: QualityRun, restoreInput = false) {
    job.value = value
    selected.value = value.run_id
    if (restoreInput) restore(value)
  }
  async function read(
    id: string,
    restoreInput = false,
    shouldAccept?: (value: QualityRun) => boolean
  ) {
    const token = epoch,
      cid = conversation.value
    stop()
    observedId = id
    const observed = observation
    request = new AbortController()
    try {
      const value = await getQualityRun(id, cid, request.signal)
      if (token !== epoch || observed !== observation) return
      if (shouldAccept && !shouldAccept(value)) return
      error.value = ''
      accept(value, restoreInput)
      if (loading.value && !submitting.value) timer = setTimeout(() => void read(id), 1000)
    } catch (err) {
      const aborted =
        !!err && typeof err === 'object' && 'code' in err && err.code === 'ERR_CANCELED'
      if (token === epoch && observed === observation && !aborted) error.value = sourceError(err)
    }
  }
  async function history(
    requested = '',
    chooseLatest = false,
    canRestore?: (value: QualityRun) => boolean
  ) {
    const token = epoch,
      cid = conversation.value
    try {
      await ensureSourceConversation(cid)
      if (token !== epoch || submitting.value) return
      const value = await listQualityRuns(cid)
      if (token !== epoch || submitting.value) return
      jobs.value = value.items
      const id = requested || (chooseLatest || canRestore ? value.items[0]?.run_id : '')
      if (id) await read(id, true, !requested && !chooseLatest ? canRestore : undefined)
    } catch (err) {
      if (token === epoch) error.value = sourceError(err)
    }
  }
  async function mutate(action: () => Promise<QualityRun>, done?: () => void) {
    if (submitting.value) return
    const token = epoch
    submitting.value = true
    error.value = ''
    stop()
    try {
      const value = await action()
      if (token !== epoch) return
      done?.()
      retryUnknown.value = false
      accept(value)
      const history = await listQualityRuns(value.conversation_id)
      if (token !== epoch) return
      jobs.value = history.items
    } catch (err) {
      if (token === epoch) error.value = sourceError(err)
    } finally {
      if (token === epoch) {
        submitting.value = false
        if (job.value) void read(job.value.run_id)
      }
    }
  }
  function create(inputs: QualityInputs, done?: () => void) {
    if (loading.value || submitting.value) return
    const body = { ...inputs, conversation_id: conversation.value }
    if (
      !pendingCreate ||
      JSON.stringify({ ...pendingCreate, request_id: undefined, run_id: undefined }) !==
        JSON.stringify(body)
    )
      pendingCreate = {
        ...body,
        run_id: 'research_' + crypto.randomUUID().replaceAll('-', ''),
        request_id: crypto.randomUUID(),
      }
    const captured = pendingCreate
    void mutate(
      async () => {
        await ensureSourceConversation(captured.conversation_id)
        return createQualityRun(captured)
      },
      () => {
        pendingCreate = null
        done?.()
      }
    )
  }
  function resume() {
    const value = job.value,
      cid = conversation.value
    if (!value || (value.requires_unknown_confirmation && !retryUnknown.value)) return
    if (
      !pendingResume ||
      pendingResume.id !== value.run_id ||
      pendingResume.revision !== value.revision ||
      pendingResume.retry !== retryUnknown.value
    )
      pendingResume = {
        id: value.run_id,
        revision: value.revision,
        request: crypto.randomUUID(),
        retry: retryUnknown.value,
      }
    const captured = pendingResume
    void mutate(
      () =>
        resumeQualityRun(captured.id, cid, captured.revision, captured.request, captured.retry),
      () => {
        pendingResume = null
      }
    )
  }
  function cancel() {
    if (job.value) {
      const id = job.value.run_id,
        cid = conversation.value
      void mutate(() => cancelQualityRun(id, cid))
    }
  }
  function select() {
    ++epoch
    stop()
    submitting.value = false
    pendingResume = null
    retryUnknown.value = false
    error.value = ''
    void read(selected.value, true)
  }
  function refresh() {
    error.value = ''
    if (job.value) void read(job.value.run_id)
    else if (observedId) void read(observedId, true)
    else void history('', true)
  }
  onBeforeUnmount(() => {
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
    reset,
    history,
    create,
    resume,
    cancel,
    select,
    refresh,
  }
}
