import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch, type Ref } from 'vue'
import { reorderConversationPins, type Conversation } from './api'

type Direction = 'top' | 'up' | 'down' | 'bottom'
type OrderChange = { ids: string[]; expected: string[]; focusId?: string }

function pinTime(value: string) {
  return Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`) || 0
}

export function usePinnedOrder(conversations: Ref<Conversation[]>, pinning: Ref<Set<string>>, refresh: () => Promise<void>, complete: Ref<boolean> = ref(true)) {
  const pinned = computed(() => conversations.value.filter((conversation) => conversation.pinned_at).sort((a, b) => {
    if (a.pin_position != null && b.pin_position != null && a.pin_position !== b.pin_position) return a.pin_position - b.pin_position
    if ((a.pin_position == null) !== (b.pin_position == null)) return a.pin_position == null ? -1 : 1
    return pinTime(b.pinned_at!) - pinTime(a.pinned_at!) || (a.id < b.id ? 1 : a.id === b.id ? 0 : -1)
  }))
  const saving = ref(false)
  const busy = computed(() => saving.value || pinning.value.size > 0 || !complete.value)
  const error = ref('')
  const menu = ref<{ id: string; x: number; y: number } | null>(null)
  const menuEl = ref<HTMLElement | null>(null)
  const dragging = ref<string | null>(null)
  const dropTarget = ref<string | null>(null)
  const dropEdge = ref<'before' | 'after'>('before')
  let dragExpected: string[] = []
  let pending: OrderChange | null = null
  const canRetry = ref(false)
  let disposed = false
  watch(pinned, (items) => {
    if (menu.value && !items.some((item) => item.id === menu.value!.id)) closeMenu()
  })

  function focusHandle(id: string) {
    void nextTick(() => {
      const handle = [...document.querySelectorAll<HTMLButtonElement>('[data-pin-handle-id]')]
        .find((button) => button.dataset.pinHandleId === id)
      handle?.focus()
    })
  }

  function closeMenu(restoreFocus = false) {
    const id = menu.value?.id
    menu.value = null
    if (restoreFocus && id) focusHandle(id)
  }

  function cancelDrag() {
    dragging.value = null
    dropTarget.value = null
    dragExpected = []
  }

  async function save(change: OrderChange) {
    if (busy.value || disposed) return
    pending = change
    canRetry.value = false
    saving.value = true
    error.value = ''
    try {
      await reorderConversationPins(change.ids, change.expected)
      pending = null
      if (!disposed) await refresh()
    } catch (failure: any) {
      if (disposed) return
      const status = failure?.response?.status
      if (status === 409 || status === 422) {
        pending = null
        error.value = status === 409 ? '固定会话已在其他窗口变更，请重新排序' : '固定顺序无效，请重新排序'
        await refresh()
      } else {
        error.value = '固定顺序尚未保存'
        canRetry.value = true
      }
    } finally {
      saving.value = false
      if (!disposed && change.focusId) focusHandle(change.focusId)
    }
  }

  function canMove(id: string, direction: Direction) {
    const index = pinned.value.findIndex((conversation) => conversation.id === id)
    return !busy.value && index >= 0 && (direction === 'up' || direction === 'top' ? index > 0 : index < pinned.value.length - 1)
  }

  function move(id: string, direction: Direction) {
    if (!canMove(id, direction)) return
    const expected = pinned.value.map((conversation) => conversation.id)
    const index = expected.indexOf(id)
    const destination = direction === 'top' ? 0 : direction === 'bottom' ? expected.length - 1 : index + (direction === 'up' ? -1 : 1)
    const ids = expected.filter((cid) => cid !== id)
    ids.splice(destination, 0, id)
    closeMenu()
    void save({ ids, expected, focusId: id })
  }

  function openMenu(event: Event, id: string) {
    if (busy.value || pinned.value.length < 2) return
    if (menu.value?.id === id) { closeMenu(); return }
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
    menu.value = { id, x: Math.max(8, Math.min(rect.right + 4, innerWidth - 148)), y: Math.max(8, Math.min(rect.top, innerHeight - 160)) }
    void nextTick(() => menuEl.value?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus())
  }

  function handleKeydown(event: KeyboardEvent, id: string) {
    const directions: Record<string, Direction> = { ArrowUp: 'up', ArrowDown: 'down', Home: 'top', End: 'bottom' }
    const direction = directions[event.key]
    if (event.altKey && direction) { event.preventDefault(); move(id, direction) }
    else if (event.key === 'ArrowDown' && !event.altKey) { event.preventDefault(); openMenu(event, id) }
  }

  function menuKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); return }
    if (event.key === 'Tab') { closeMenu(); return }
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const buttons = [...menuEl.value!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
      : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
    buttons[next]?.focus()
  }

  function startDrag(event: DragEvent, id: string) {
    if (busy.value || pinned.value.length < 2 || !event.dataTransfer) { event.preventDefault(); return }
    closeMenu()
    dragging.value = id
    dragExpected = pinned.value.map((conversation) => conversation.id)
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', id)
  }

  function edgeAt(event: DragEvent) {
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
    return event.clientY < rect.top + rect.height / 2 ? 'before' : 'after'
  }

  function dragOver(event: DragEvent, target: string) {
    if (!dragging.value || dragging.value === target || busy.value) return
    event.preventDefault()
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
    dropTarget.value = target
    dropEdge.value = edgeAt(event)
  }

  function dragLeave(event: DragEvent, target: string) {
    const row = event.currentTarget as HTMLElement
    if (dropTarget.value === target && !row.contains(event.relatedTarget as Node | null)) dropTarget.value = null
  }

  function drop(event: DragEvent, target: string) {
    if (!dragging.value || busy.value) return
    event.preventDefault()
    const id = dragging.value
    const expected = dragExpected.slice()
    const ids = expected.filter((cid) => cid !== id)
    const index = ids.indexOf(target)
    const after = edgeAt(event) === 'after'
    cancelDrag()
    if (index < 0) return
    ids.splice(index + (after ? 1 : 0), 0, id)
    if (ids.some((cid, i) => cid !== expected[i])) void save({ ids, expected })
  }

  const outsideClick = (event: MouseEvent) => { if (!menuEl.value?.contains(event.target as Node)) closeMenu() }
  const onBlur = () => { closeMenu(); cancelDrag() }
  onMounted(() => {
    document.addEventListener('click', outsideClick)
    window.addEventListener('blur', onBlur)
  })
  onUnmounted(() => {
    disposed = true
    document.removeEventListener('click', outsideClick)
    window.removeEventListener('blur', onBlur)
  })
  return reactive({
    pinned, saving, busy, error, canRetry, menu, menuEl, dragging, dropTarget, dropEdge,
    canMove, move, openMenu, closeMenu, handleKeydown, menuKeydown, startDrag, dragOver, dragLeave, drop, cancelDrag,
    retry: () => { if (pending) void save(pending) },
    dismissError: () => { error.value = ''; canRetry.value = false; pending = null },
  })
}
