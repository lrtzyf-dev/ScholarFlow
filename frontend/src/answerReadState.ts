import { nextTick, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { markConversationRead } from './api'

export function useAnswerReadState(root: Ref<HTMLElement | null>, conversationId: Ref<string>, enabled: Ref<boolean>) {
  const error = ref(false)
  const acknowledged = new Map<string, Set<string>>()
  let observer: IntersectionObserver | undefined
  let mutations: MutationObserver | undefined
  let observed = new Set<Element>()
  let timer: ReturnType<typeof setTimeout> | undefined
  let inFlight = false
  let disposed = false
  let retryDelay = 1000
  let generation = 0

  function canRead() {
    return !disposed && enabled.value && document.visibilityState === 'visible' && document.hasFocus()
      && ![...document.querySelectorAll<HTMLElement>('[aria-modal="true"]')].some((modal) =>
        modal.getClientRects().length > 0 && getComputedStyle(modal).visibility !== 'hidden')
  }

  function visibleIds(): string[] {
    const container = root.value
    if (!container || !canRead()) return []
    const bounds = container.getBoundingClientRect()
    const known = acknowledged.get(conversationId.value)
    return [...container.querySelectorAll<HTMLElement>('[data-read-record-id]')].filter((node) => {
      if (known?.has(node.dataset.readRecordId!)) return false
      const rect = node.getBoundingClientRect()
      return Math.min(rect.bottom, bounds.bottom, innerHeight) - Math.max(rect.top, bounds.top, 0) >= 20
        && Math.min(rect.right, bounds.right, innerWidth) > Math.max(rect.left, bounds.left, 0)
    }).map((node) => node.dataset.readRecordId!).slice(0, 100)
  }

  function schedule(delay = 250) {
    if (disposed || timer !== undefined) return
    timer = setTimeout(() => { timer = undefined; void flush() }, delay)
  }

  async function flush() {
    if (inFlight) return
    const ids = visibleIds()
    if (!ids.length) return
    const cid = conversationId.value
    inFlight = true
    let delay = 250
    try {
      const receipt = await markConversationRead(cid, ids)
      const known = acknowledged.get(cid) || new Set<string>()
      for (const id of receipt.message_ids) if (ids.includes(id)) known.add(id)
      acknowledged.set(cid, known)
      retryDelay = 1000
      if (!receipt.message_ids.length) delay = 10000
      if (cid === conversationId.value) error.value = false
    } catch {
      if (cid === conversationId.value) error.value = true
      delay = retryDelay
      retryDelay = Math.min(retryDelay * 2, 10000)
    } finally {
      inFlight = false
      if (visibleIds().length) schedule(delay)
    }
  }

  function observeAnswers() {
    if (!root.value) return
    const nodes = new Set(root.value.querySelectorAll('[data-read-record-id]'))
    for (const node of observed) if (!nodes.has(node)) observer?.unobserve(node)
    for (const node of nodes) if (!observed.has(node)) observer?.observe(node)
    observed = nodes
    schedule()
  }

  watch([root, conversationId, enabled], async () => {
    const currentGeneration = ++generation
    observer?.disconnect()
    mutations?.disconnect()
    observed.clear()
    error.value = false
    await nextTick()
    if (disposed || currentGeneration !== generation || !root.value || !enabled.value) return
    observer = new IntersectionObserver(() => schedule(), { root: root.value, threshold: [0, 0.01] })
    mutations = new MutationObserver(observeAnswers)
    mutations.observe(root.value, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-read-record-id'] })
    observeAnswers()
  }, { flush: 'post', immediate: true })

  const onFocus = () => schedule()
  onMounted(() => {
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    root.value?.addEventListener('scroll', onFocus, { passive: true })
  })
  onUnmounted(() => {
    disposed = true
    observer?.disconnect()
    mutations?.disconnect()
    clearTimeout(timer)
    window.removeEventListener('focus', onFocus)
    document.removeEventListener('visibilitychange', onFocus)
    root.value?.removeEventListener('scroll', onFocus)
  })
  return { error, retry: () => { clearTimeout(timer); timer = undefined; schedule(0) } }
}
