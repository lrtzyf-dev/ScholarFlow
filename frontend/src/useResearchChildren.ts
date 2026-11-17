import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import axios from 'axios'
import {
  grantResearchChildRetry,
  listResearchChildren,
  researchChildError,
} from './researchChildApi'
import type {
  ResearchChild,
  ResearchChildGrant,
  ResearchChildGrantReceipt,
} from './researchChildTypes'

const storageKey = (cid: string, parent: string) =>
  `scholarflow:research-child-grant:${cid}:${parent}`
const attemptPattern = /^[a-f0-9]{32}$/
const hashPattern = /^[a-f0-9]{64}$/
function identity(child: ResearchChild | null) {
  return child
    ? JSON.stringify([
        child.conversation_id,
        child.parent_run_id,
        child.parent_revision,
        child.parent_attempt_id,
        child.run_id,
        child.attempt_id,
        child.revision,
        child.pending_sha256,
        child.requires_unknown_confirmation,
        child.unknown_retry_approved,
      ])
    : ''
}
function eligible(child: ResearchChild | null): child is ResearchChild & {
  parent_revision: number
  parent_attempt_id: string
  pending_sha256: string
} {
  return (
    !!child &&
    child.requires_unknown_confirmation &&
    !child.unknown_retry_approved &&
    !['queued', 'running'].includes(child.status) &&
    Number.isSafeInteger(child.parent_revision) &&
    (child.parent_revision ?? 0) >= 1 &&
    attemptPattern.test(child.parent_attempt_id || '') &&
    attemptPattern.test(child.attempt_id) &&
    hashPattern.test(child.pending_sha256 || '')
  )
}
function validPending(value: ResearchChildGrant | null, cid: string, parent: string) {
  return (
    value &&
    value.conversation_id === cid &&
    value.parent_run_id === parent &&
    typeof value.child_run_id === 'string' &&
    /^research_[a-f0-9]{32}$/.test(value.child_run_id) &&
    Number.isSafeInteger(value.expected_parent_revision) &&
    value.expected_parent_revision >= 1 &&
    attemptPattern.test(value.expected_parent_attempt_id) &&
    attemptPattern.test(value.expected_child_attempt_id) &&
    hashPattern.test(value.pending_sha256) &&
    typeof value.request_id === 'string' &&
    /^[A-Za-z0-9_-]{8,96}$/.test(value.request_id) &&
    value.confirm_duplicate_cost === true
  )
}
function readPending(cid: string, parent: string): ResearchChildGrant[] {
  try {
    const prefix = storageKey(cid, parent)
    const keys = Array.from({ length: localStorage.length }, (_, index) =>
      localStorage.key(index)
    ).filter((key): key is string => !!key && (key === prefix || key.startsWith(prefix + ':')))
    const found = new Map<string, ResearchChildGrant>()
    for (const key of keys) {
      try {
        const value = JSON.parse(localStorage.getItem(key) || 'null')
        if (validPending(value, cid, parent)) found.set(value.request_id, value)
      } catch {
        /* A corrupt neighbour must not hide another recoverable request. */
      }
    }
    return [...found.values()]
  } catch {
    return []
  }
}

export function useResearchChildren(conversation: Ref<string>, parent: Ref<string>) {
  const items = ref<ResearchChild[]>([]),
    selectedId = ref(''),
    consentIdentity = ref('')
  const loading = ref(false),
    submitting = ref(false),
    error = ref(''),
    loadError = ref(''),
    notice = ref('')
  const pendingGrants = ref<ResearchChildGrant[]>([])
  const pendingGrant = computed(() => pendingGrants.value[0] || null)
  const selected = computed(
    () => items.value.find((child) => child.run_id === selectedId.value) || null
  )
  const acknowledged = computed({
    get: () => eligible(selected.value) && consentIdentity.value === identity(selected.value),
    set: (checked) => {
      consentIdentity.value = checked ? identity(selected.value) : ''
    },
  })
  const canGrant = computed(
    () => eligible(selected.value) && acknowledged.value && !pendingGrant.value && !submitting.value
  )
  let epoch = 0,
    sequence = 0,
    disposed = false,
    request: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  const current = (token: number, cid: string, run: string) =>
    !disposed && epoch === token && conversation.value === cid && parent.value === run
  function schedule() {
    if (timer) clearTimeout(timer)
    if (!disposed)
      timer = setTimeout(() => {
        if (document.hidden || submitting.value) schedule()
        else void load()
      }, 5000)
  }
  async function load() {
    const cid = conversation.value,
      run = parent.value,
      token = epoch,
      serial = ++sequence
    if (!cid || !run) return
    request?.abort()
    request = new AbortController()
    loading.value = true
    try {
      const value = await listResearchChildren(cid, run, request.signal)
      if (!current(token, cid, run) || serial !== sequence) return
      // The query is scoped, and rows must retain that same authoritative pair.
      if (value.items.some((child) => child.conversation_id !== cid || child.parent_run_id !== run))
        throw new Error('Scoped child response mismatch')
      items.value = value.items
      if (!items.value.some((child) => child.run_id === selectedId.value))
        selectedId.value = pendingGrant.value?.child_run_id || items.value[0]?.run_id || ''
      loadError.value = ''
    } catch (cause) {
      if (current(token, cid, run) && serial === sequence && !axios.isCancel(cause))
        loadError.value = researchChildError(cause)
    } finally {
      if (current(token, cid, run) && serial === sequence) {
        loading.value = false
        schedule()
      }
    }
  }
  function select(id: string) {
    if (!submitting.value) {
      selectedId.value = id
      consentIdentity.value = ''
      notice.value = error.value = ''
    }
  }
  function clearPending(captured: ResearchChildGrant) {
    const prefix = storageKey(captured.conversation_id, captured.parent_run_id)
    localStorage.removeItem(prefix + ':' + captured.request_id)
    // Only remove a legacy slot if it still belongs to this exact request.
    const legacy = localStorage.getItem(prefix)
    try {
      if (legacy && JSON.parse(legacy)?.request_id === captured.request_id)
        localStorage.removeItem(prefix)
    } catch {
      /* Keep a malformed legacy record without hiding valid request receipts. */
    }
    syncPending()
  }
  function syncPending(event?: StorageEvent) {
    const prefix = storageKey(conversation.value, parent.value)
    if (event?.key && event.key !== prefix && !event.key.startsWith(prefix + ':')) return
    pendingGrants.value = readPending(conversation.value, parent.value)
  }
  async function submit(captured: ResearchChildGrant) {
    const token = epoch,
      cid = captured.conversation_id,
      run = captured.parent_run_id
    if (submitting.value || !current(token, cid, run)) return
    submitting.value = true
    error.value = ''
    notice.value = ''
    let saved = false
    try {
      const receipt: ResearchChildGrantReceipt = await grantResearchChildRetry(captured)
      if (!current(token, cid, run)) return
      if (
        receipt.child_run_id !== captured.child_run_id ||
        receipt.parent_run_id !== run ||
        receipt.pending_sha256 !== captured.pending_sha256 ||
        !receipt.grant_id
      )
        throw new Error('Grant response mismatch')
      clearPending(captured)
      saved = true
      consentIdentity.value = ''
      notice.value = '许可已保存，仅针对刚才核对的未知调用。请从原任务继续；此处尚未派发请求。'
    } catch (cause) {
      if (!current(token, cid, run)) return
      const status = (cause as { response?: { status?: number } })?.response?.status
      if (status && status >= 400 && status < 500) {
        // A definitive rejection grants nothing; a fresh identity requires new consent.
        try {
          clearPending(captured)
        } catch {
          /* Same exact request stays retryable. */
        }
        consentIdentity.value = ''
      }
      error.value = researchChildError(cause)
    } finally {
      if (current(token, cid, run)) {
        submitting.value = false
        await load()
        const latest = items.value.find((child) => child.run_id === captured.child_run_id)
        if (
          current(token, cid, run) &&
          saved &&
          latest &&
          (latest.attempt_id !== captured.expected_child_attempt_id ||
            latest.pending_sha256 !== captured.pending_sha256)
        )
          notice.value =
            '原许可回执已找回，但当前调用已变化。该许可不批准新的未知调用，请重新核对。'
      }
    }
  }
  async function grant() {
    syncPending()
    const child = selected.value
    if (!canGrant.value || !eligible(child)) return
    const captured: ResearchChildGrant = {
      conversation_id: conversation.value,
      parent_run_id: parent.value,
      child_run_id: child.run_id,
      expected_parent_revision: child.parent_revision,
      expected_parent_attempt_id: child.parent_attempt_id,
      expected_child_attempt_id: child.attempt_id,
      pending_sha256: child.pending_sha256,
      request_id: crypto.randomUUID(),
      confirm_duplicate_cost: true,
    }
    try {
      localStorage.setItem(
        storageKey(captured.conversation_id, captured.parent_run_id) + ':' + captured.request_id,
        JSON.stringify(captured)
      )
    } catch {
      error.value = '无法保存本次许可请求，尚未提交。请检查本机存储后重试。'
      return
    }
    syncPending()
    await submit(captured)
  }
  async function retryPending(requestId?: string) {
    syncPending()
    const pending =
      pendingGrants.value.find((item) => item.request_id === requestId) ||
      (!requestId ? pendingGrant.value : null)
    if (pending) await submit({ ...pending })
  }
  window.addEventListener('storage', syncPending)
  watch(
    [conversation, parent],
    () => {
      ++epoch
      ++sequence
      request?.abort()
      if (timer) clearTimeout(timer)
      items.value = []
      selectedId.value = ''
      consentIdentity.value = ''
      loading.value = submitting.value = false
      error.value = loadError.value = notice.value = ''
      syncPending()
      void load()
    },
    { immediate: true }
  )
  onBeforeUnmount(() => {
    disposed = true
    ++epoch
    ++sequence
    request?.abort()
    window.removeEventListener('storage', syncPending)
    if (timer) clearTimeout(timer)
  })
  return {
    items,
    selected,
    selectedId,
    loading,
    submitting,
    error: computed(() => error.value || loadError.value),
    notice,
    acknowledged,
    canGrant,
    pendingGrant,
    pendingGrants,
    select,
    load,
    grant,
    retryPending,
  }
}
