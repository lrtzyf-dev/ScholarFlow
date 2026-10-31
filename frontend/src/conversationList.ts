import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { listConversationPage, lookupConversations, type Conversation, type ConversationScope, type ConversationSort } from './api'
import { taskState } from './taskState'

// Each view owns its query and cursor. SSE invalidates loaded metadata only;
// it no longer sends every conversation to every window on every task update.
export function useConversationList(options: {
  scope: Ref<ConversationScope>; q: Ref<string>; sort: Ref<ConversationSort>; enabled: Ref<boolean>
}) {
  const items = ref<Conversation[]>([])
  const cursor = ref<string | null>(null)
  const total = ref(0)
  const pinnedTotal = ref(0)
  const loading = ref(false)
  const loadingMore = ref(false)
  const error = ref(false)
  const moreError = ref(false)
  const pinsComplete = computed(() => items.value.filter((item) => item.pinned_at).length >= pinnedTotal.value)
  let generation = 0
  let expanded = false
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let queued = false
  let inFlight: Promise<void> | null = null
  const query = () => ({ scope: options.scope.value, q: options.q.value.trim(), sort: options.sort.value })

  function schedule() {
    if (disposed || !options.enabled.value) return
    if (inFlight || loadingMore.value) { queued = true; return }
    if (!timer) timer = setTimeout(() => { timer = undefined; void refresh() }, 180)
  }

  async function refresh(): Promise<void> {
    if (disposed || !options.enabled.value) return
    if (inFlight) { await inFlight; return refresh() }
    clearTimeout(timer); timer = undefined
    const current = generation
    const version = taskState.conversationVersion.value
    const retained = expanded ? items.value.map((item) => item.id) : []
    const params = query()
    loading.value = true
    error.value = false
    inFlight = (async () => {
      try {
        const reads = []
        for (let offset = 0; offset < retained.length; offset += 100) {
          reads.push(lookupConversations(retained.slice(offset, offset + 100), params.scope, params.q))
        }
        const [first, older] = await Promise.all([listConversationPage(params), Promise.all(reads)])
        if (disposed || current !== generation) return
        if (version !== taskState.conversationVersion.value) { queued = true; return }
        const merged = new Map(older.flatMap((page) => page.conversations).map((item) => [item.id, item]))
        for (const item of first.conversations) merged.set(item.id, item)
        items.value = [...merged.values()]
        total.value = first.total
        pinnedTotal.value = first.pinned_total
        if (!expanded) cursor.value = first.next_cursor
      } catch {
        if (current === generation && !disposed) error.value = true
      } finally {
        inFlight = null
        if (current === generation) loading.value = false
        if (queued) { queued = false; schedule() }
      }
    })()
    await inFlight
    // Mutation callers restore focus after this promise. Wait until a newer
    // invalidation has also landed, or a subsequent keyed move would lose it.
    if (queued || timer) return refresh()
  }

  async function loadMore() {
    if (disposed || !cursor.value || loadingMore.value || loading.value) return
    const current = generation
    const version = taskState.conversationVersion.value
    const before = cursor.value
    loadingMore.value = true
    moreError.value = false
    try {
      const page = await listConversationPage({ ...query(), cursor: before })
      if (disposed || current !== generation) return
      if (version !== taskState.conversationVersion.value) { queued = true; return }
      const merged = new Map(items.value.map((item) => [item.id, item]))
      for (const item of page.conversations) merged.set(item.id, item)
      items.value = [...merged.values()]
      cursor.value = page.next_cursor
      total.value = page.total
      pinnedTotal.value = page.pinned_total
      expanded = true
    } catch {
      if (current === generation && !disposed) moreError.value = true
    } finally {
      if (current === generation) loadingMore.value = false
      if (queued) { queued = false; schedule() }
    }
  }

  watch([options.scope, options.q, options.sort, options.enabled], ([, q, , enabled], previous) => {
    generation++
    clearTimeout(timer); timer = undefined
    expanded = false
    cursor.value = null
    items.value = []
    total.value = pinnedTotal.value = 0
    loading.value = loadingMore.value = error.value = moreError.value = false
    queued = Boolean(enabled)
    if (enabled && !inFlight) {
      queued = false
      if (previous && q !== previous[1]) { loading.value = true; schedule() }
      else void refresh()
    }
  }, { immediate: true })
  watch(taskState.conversationVersion, schedule)
  window.addEventListener('scholarflow:conversation-updated', schedule)
  window.addEventListener('scholarflow:conversation-archive', schedule)
  onUnmounted(() => {
    disposed = true; generation++; clearTimeout(timer)
    window.removeEventListener('scholarflow:conversation-updated', schedule)
    window.removeEventListener('scholarflow:conversation-archive', schedule)
  })
  return { items, cursor, total, loading, loadingMore, error, moreError, pinsComplete, refresh, loadMore }
}
