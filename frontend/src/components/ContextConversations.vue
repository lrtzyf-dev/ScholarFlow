<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { pinConversation, type Conversation, type ConversationSort } from '../api'
import { useConversationList } from '../conversationList'
import { usePinnedOrder } from '../pinnedOrder'
import { conversationArchive, changeConversationArchive } from '../conversationArchive'
import { activeConversationId, sidebarState } from '../desktopState'
import { ElMessage } from 'element-plus'
import {
  Plus,
  Pin,
  PinOff,
  ShieldAlert,
  LoaderCircle,
  CircleStop,
  GripVertical,
  ArrowUp,
  ArrowDown,
  ArrowUpToLine,
  ArrowDownToLine,
  X,
  Archive,
  Search,
  MoreHorizontal,
} from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const controlsOpen = ref(false)
let stopTaskUpdates: (() => void) | undefined
const pinningConversations = ref(new Set<string>())
const sidebarSaveStatus = sidebarState.status
const conversationSort = computed({
  get: () => sidebarState.value.value.sort,
  set: (sort) => sidebarState.update({ sort }),
})
const sidebarDate = ref(new Date())
let sidebarTimer: ReturnType<typeof setInterval> | undefined
const sidebarConversations = useConversationList({
  scope: ref('active'),
  q: ref(''),
  sort: computed<ConversationSort>(() =>
    conversationSort.value === 'created_at' ? 'sidebar_created_at' : 'sidebar_updated_at'
  ),
  enabled: ref(true),
  projectScope: ref('unassigned'),
})
const {
  items: recentConversations,
  error: conversationsError,
  cursor: conversationsCursor,
  loadingMore: conversationsLoadingMore,
  moreError: conversationsMoreError,
} = sidebarConversations
const refreshRecentConversations = sidebarConversations.refresh
const currentConversationId = computed(() =>
  String(route.query.conv || activeConversationId.value || '')
)
const pinOrder = usePinnedOrder(
  recentConversations,
  pinningConversations,
  refreshRecentConversations,
  sidebarConversations.pinsComplete,
  'unassigned'
)
function setPinMenuEl(element: unknown) {
  pinOrder.menuEl = element as HTMLElement | null
}

function conversationStatus(conversation: Conversation): string {
  const states = []
  if (conversation.approval_count) states.push(`${conversation.approval_count} 项待审批`)
  if (conversation.running_count) states.push(`${conversation.running_count} 项运行中`)
  if (conversation.finishing_count) states.push(`${conversation.finishing_count} 项正在结束`)
  if (conversation.unread_count) states.push(`${conversation.unread_count} 条未读回答`)
  return states.join('，')
}

function conversationTimestamp(
  conversation: Conversation,
  field: 'updated_at' | 'created_at' | 'pinned_at' = conversationSort.value
): number {
  const value = conversation[field] || conversation.updated_at || conversation.created_at
  if (!value) return 0
  const timestamp = Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(timestamp) ? 0 : timestamp
}

const conversationDateGroups = computed(() => {
  const now = sidebarDate.value
  const today = Math.floor(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()) / 86_400_000)
  const labels = ['今天', '昨天', '过去 7 天', '过去 30 天', '更早']
  const buckets = labels.map((label) => ({ label, conversations: [] as Conversation[] }))
  const pinned = pinOrder.pinned
  const ordered = recentConversations.value
    .filter((conversation) => !conversation.pinned_at)
    .sort((a, b) => conversationTimestamp(b) - conversationTimestamp(a))

  for (const conversation of ordered) {
    const timestamp = conversationTimestamp(conversation)
    const date = timestamp ? new Date(timestamp) : null
    const day = date
      ? Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86_400_000)
      : Number.NEGATIVE_INFINITY
    const age = today - day
    const index = age <= 0 ? 0 : age === 1 ? 1 : age <= 7 ? 2 : age <= 30 ? 3 : 4
    buckets[index].conversations.push(conversation)
  }

  return [{ label: '已固定', conversations: pinned }, ...buckets].filter(
    (group) => group.conversations.length
  )
})

async function toggleConversationPin(conversation: Conversation) {
  if (pinOrder.saving || pinningConversations.value.has(conversation.id)) return
  pinOrder.closeMenu()
  pinOrder.cancelDrag()
  pinningConversations.value.add(conversation.id)
  try {
    const saved = await pinConversation(conversation.id, !conversation.pinned_at)
    conversation.pinned_at = saved.pinned_at
    conversation.pin_position = saved.pin_position
    await refreshRecentConversations()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '固定会话失败，请重试')
  } finally {
    pinningConversations.value.delete(conversation.id)
  }
}

function archiveConversation(conversation: Conversation) {
  if (pinOrder.busy) return
  pinOrder.closeMenu()
  pinOrder.cancelDrag()
  void changeConversationArchive(conversation.id, true, conversation.archive_revision || 0)
}

function onConversationArchived(event: Event) {
  const saved = (event as CustomEvent).detail
  if (saved.archived_at)
    recentConversations.value = recentConversations.value.filter((item) => item.id !== saved.id)
  else void refreshRecentConversations()
}

function onSidebarFocus() {
  sidebarDate.value = new Date()
  void refreshRecentConversations()
}

function openRecentConversation(conversation: Conversation) {
  void router.push({ path: '/chat', query: { conv: conversation.id } })
}

function openAllConversations() {
  void router.push({ path: '/chat', query: { history: '1' } })
}

function searchConversations() {
  void router.push({ path: '/chat', query: { history: 'search' } })
}

async function startNewConversation() {
  if (route.path !== '/chat') await router.push('/chat')
  await nextTick()
  window.dispatchEvent(new Event('scholarflow:new-conversation'))
}

watch(
  () => route.fullPath,
  () => {
    pinOrder.closeMenu()
    pinOrder.cancelDrag()
  }
)

onMounted(() => {
  window.addEventListener('scholarflow:conversation-archive', onConversationArchived)
  window.addEventListener('focus', onSidebarFocus)
  sidebarTimer = setInterval(() => {
    sidebarDate.value = new Date()
  }, 60_000)
})
onUnmounted(() => {
  stopTaskUpdates?.()
  window.removeEventListener('scholarflow:conversation-archive', onConversationArchived)
  window.removeEventListener('focus', onSidebarFocus)
  clearInterval(sidebarTimer)
})
</script>

<template>
  <div class="ctx-conversations">
    <div class="ctx-head">
      <span>最近研究</span>
      <button
        class="context-more"
        type="button"
        aria-label="研究会话选项"
        title="研究会话选项"
        :aria-expanded="controlsOpen"
        @click="controlsOpen = !controlsOpen"
      >
        <MoreHorizontal :size="16" />
      </button>
    </div>
    <div v-if="controlsOpen" class="ctx-actions">
      <button type="button" aria-label="搜索会话" title="搜索会话" @click="searchConversations">
        <Search :size="14" />
      </button>
      <button type="button" aria-label="新建会话" title="新建会话" @click="startNewConversation">
        <Plus :size="15" :stroke-width="1.8" />
      </button>
      <button type="button" @click="openAllConversations">全部</button>
    </div>
    <select
      v-if="controlsOpen"
      v-model="conversationSort"
      class="conversation-sort"
      aria-label="会话排序"
      :disabled="sidebarSaveStatus === 'loading'"
      title="会话排序"
    >
      <option value="updated_at">最近更新</option>
      <option value="created_at">最近创建</option>
    </select>
    <div v-if="sidebarSaveStatus === 'error'" class="ctx-error" role="status">
      排序尚未保存<button type="button" @click="sidebarState.retry()">重试</button>
    </div>
    <div class="conversation-list">
      <section
        v-for="group in conversationDateGroups"
        :key="group.label"
        class="conversation-date-group"
      >
        <div v-if="controlsOpen || group.label === '已固定'" class="conversation-date-label">
          {{ group.label }}
        </div>
        <div
          v-for="conversation in group.conversations"
          :key="conversation.id"
          class="recent-conversation-row"
          :data-conversation-id="conversation.id"
          :class="{
            active: currentConversationId === conversation.id,
            dragging: pinOrder.dragging === conversation.id,
            'drop-before':
              pinOrder.dropTarget === conversation.id && pinOrder.dropEdge === 'before',
            'drop-after': pinOrder.dropTarget === conversation.id && pinOrder.dropEdge === 'after',
          }"
          @dragover="conversation.pinned_at && pinOrder.dragOver($event, conversation.id)"
          @dragleave="pinOrder.dragLeave($event, conversation.id)"
          @drop="conversation.pinned_at && pinOrder.drop($event, conversation.id)"
        >
          <button
            v-if="conversation.pinned_at"
            type="button"
            class="conversation-reorder"
            :data-pin-handle-id="conversation.id"
            :draggable="!pinOrder.busy && pinOrder.pinned.length > 1"
            :disabled="pinOrder.busy || pinOrder.pinned.length < 2"
            :aria-label="`调整固定顺序 ${conversation.title}`"
            title="调整固定顺序"
            aria-haspopup="menu"
            :aria-expanded="pinOrder.menu?.id === conversation.id"
            @click.stop="pinOrder.openMenu($event, conversation.id)"
            @keydown="pinOrder.handleKeydown($event, conversation.id)"
            @dragstart="pinOrder.startDrag($event, conversation.id)"
            @dragend="pinOrder.cancelDrag"
          >
            <GripVertical :size="13" />
          </button>
          <button
            type="button"
            class="recent-conversation"
            :class="{
              active: currentConversationId === conversation.id,
              unread: conversation.unread_count,
            }"
            :title="
              [conversation.title, conversationStatus(conversation)].filter(Boolean).join(' · ')
            "
            :aria-label="
              [conversation.title, conversationStatus(conversation)].filter(Boolean).join('，')
            "
            @click="openRecentConversation(conversation)"
          >
            <span class="recent-conversation-title">{{ conversation.title || '新会话' }}</span>
            <span
              v-if="conversation.approval_count"
              class="conversation-activity approval-state"
              aria-hidden="true"
              ><ShieldAlert :size="13"
            /></span>
            <span
              v-else-if="conversation.running_count"
              class="conversation-activity running-state"
              aria-hidden="true"
              ><LoaderCircle :size="13"
            /></span>
            <span
              v-else-if="conversation.finishing_count"
              class="conversation-activity"
              aria-hidden="true"
              ><CircleStop :size="13"
            /></span>
            <span v-else-if="conversation.draft_only" class="draft-badge">{{
              conversation.interrupted_only ? '未完成' : '草稿'
            }}</span>
            <span
              v-if="conversation.unread_count"
              class="conversation-unread"
              aria-hidden="true"
            ></span>
          </button>
          <button
            type="button"
            class="conversation-pin"
            :class="{ pinned: conversation.pinned_at }"
            :disabled="pinOrder.saving || pinningConversations.has(conversation.id)"
            :aria-label="
              conversation.pinned_at
                ? `取消固定 ${conversation.title}`
                : `固定 ${conversation.title}`
            "
            :title="conversation.pinned_at ? '取消固定会话' : '固定会话'"
            @click="toggleConversationPin(conversation)"
          >
            <PinOff v-if="conversation.pinned_at" :size="13" :stroke-width="1.8" />
            <Pin v-else :size="13" :stroke-width="1.8" />
          </button>
          <button
            type="button"
            class="conversation-pin"
            :disabled="
              pinOrder.busy ||
              conversationArchive.pending.has(conversation.id) ||
              Boolean(conversation.running_count || conversation.finishing_count)
            "
            :aria-label="`归档 ${conversation.title}`"
            :title="
              conversation.running_count || conversation.finishing_count
                ? '任务结束后可归档'
                : '归档会话'
            "
            @click="archiveConversation(conversation)"
          >
            <Archive :size="13" :stroke-width="1.8" />
          </button>
        </div>
      </section>
      <button
        v-if="conversationsCursor"
        class="conversation-load-more"
        type="button"
        :disabled="conversationsLoadingMore || sidebarConversations.loading.value"
        @click="sidebarConversations.loadMore"
      >
        {{
          conversationsLoadingMore
            ? '正在加载…'
            : conversationsMoreError
              ? '重试加载更多会话'
              : '加载更多会话'
        }}
      </button>
    </div>
    <div v-if="conversationsError" class="ctx-error" role="status">
      会话加载失败<button type="button" @click="refreshRecentConversations">重试</button>
    </div>
    <div
      v-if="pinOrder.saving || pinOrder.error"
      class="ctx-error"
      role="status"
      aria-live="polite"
    >
      {{ pinOrder.saving ? '正在保存固定顺序…' : pinOrder.error }}
      <button v-if="pinOrder.canRetry && !pinOrder.saving" type="button" @click="pinOrder.retry">
        重试排序
      </button>
      <button
        v-else-if="pinOrder.error && !pinOrder.saving"
        type="button"
        @click="pinOrder.dismissError"
        aria-label="关闭排序提示"
      >
        <X :size="12" />
      </button>
    </div>
    <div v-else-if="!recentConversations.length" class="recent-conversations-empty">
      尚无历史会话
    </div>
  </div>

  <Teleport to="body">
    <div
      v-if="pinOrder.menu"
      :ref="setPinMenuEl"
      class="pin-order-menu"
      role="menu"
      aria-label="固定会话顺序"
      :style="{ left: `${pinOrder.menu.x}px`, top: `${pinOrder.menu.y}px` }"
      @click.stop
      @keydown="pinOrder.menuKeydown"
    >
      <button
        type="button"
        role="menuitem"
        :disabled="!pinOrder.canMove(pinOrder.menu.id, 'top')"
        @click="pinOrder.move(pinOrder.menu.id, 'top')"
      >
        <ArrowUpToLine :size="14" />移到首位
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="!pinOrder.canMove(pinOrder.menu.id, 'up')"
        @click="pinOrder.move(pinOrder.menu.id, 'up')"
      >
        <ArrowUp :size="14" />上移
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="!pinOrder.canMove(pinOrder.menu.id, 'down')"
        @click="pinOrder.move(pinOrder.menu.id, 'down')"
      >
        <ArrowDown :size="14" />下移
      </button>
      <button
        type="button"
        role="menuitem"
        :disabled="!pinOrder.canMove(pinOrder.menu.id, 'bottom')"
        @click="pinOrder.move(pinOrder.menu.id, 'bottom')"
      >
        <ArrowDownToLine :size="14" />移到末位
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.ctx-conversations {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 0 4px;
}
.ctx-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 22px;
  margin-bottom: 7px;
  color: var(--fn-text-secondary);
  font-size: 12px;
  font-weight: 400;
  padding-left: 8px;
}
.ctx-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 8px 4px;
}
.ctx-actions button {
  border: 0;
  background: transparent;
  font: inherit;
  font-size: 11px;
  padding: 4px 6px;
  cursor: pointer;
  color: var(--fn-text-secondary);
}
.context-more {
  opacity: 0;
}
.ctx-head:hover .context-more,
.ctx-head:focus-within .context-more {
  opacity: 1;
}
.ctx-head button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 26px;
  height: 26px;
  padding: 0 6px;
  border: 0;
  background: transparent;
  color: var(--fn-text-tertiary);
  border-radius: 5px;
  font: inherit;
  cursor: pointer;
}
.ctx-head button:hover {
  background: var(--fn-bg-active);
  color: var(--fn-brand);
}
.conversation-sort {
  flex: none;
  width: 100%;
  height: 26px;
  padding: 0 4px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}
.ctx-error {
  padding: 4px;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.ctx-error button {
  border: 0;
  background: transparent;
  color: var(--fn-brand);
  font: inherit;
  cursor: pointer;
}
.conversation-list {
  flex: 1;
  min-height: 40px;
  max-height: 38vh;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-gutter: stable;
}
.conversation-date-group + .conversation-date-group {
  margin-top: 0;
}
.conversation-date-label {
  padding: 6px 8px 3px;
  color: var(--fn-text-tertiary);
  font-size: 11px;
  line-height: 16px;
}
.recent-conversation-row {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: 6px;
}
.recent-conversation-row.dragging {
  opacity: 0.5;
}
.recent-conversation-row.drop-before::before,
.recent-conversation-row.drop-after::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--fn-brand);
  pointer-events: none;
}
.recent-conversation-row.drop-before::before {
  top: 0;
}
.recent-conversation-row.drop-after::after {
  bottom: 0;
}
.conversation-reorder {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 16px;
  width: 16px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: grab;
}
.conversation-reorder:hover {
  color: var(--fn-brand);
}
.conversation-reorder:active {
  cursor: grabbing;
}
.conversation-reorder:disabled {
  opacity: 0.4;
  cursor: default;
}
.pin-order-menu {
  position: fixed;
  width: 140px;
  z-index: 3000;
  padding: 4px;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  box-shadow: var(--fn-shadow-md);
}
.pin-order-menu button {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 32px;
  padding: 0 8px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fn-text-primary);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.pin-order-menu button:hover {
  background: var(--fn-bg-active);
}
.pin-order-menu button:disabled {
  color: var(--fn-text-tertiary);
  cursor: default;
  background: transparent;
}
.recent-conversation-row:hover,
.recent-conversation-row.active {
  background: var(--fn-bg-active);
}
.conversation-pin {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 24px;
  width: 24px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: pointer;
  opacity: 0;
}
.recent-conversation-row:hover .conversation-pin,
.recent-conversation-row:focus-within .conversation-pin,
.conversation-pin.pinned {
  opacity: 1;
}
.conversation-pin:hover {
  color: var(--fn-brand);
}
.conversation-pin:disabled {
  cursor: wait;
  opacity: 0.4;
}
.recent-conversation {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  height: 34px;
  padding: 0 8px;
  overflow: hidden;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--fn-text-secondary);
  text-align: left;
  cursor: pointer;
}
.recent-conversation:hover,
.recent-conversation.active {
  background: var(--fn-bg-active);
  color: var(--fn-text-primary);
}
.recent-conversation-title {
  flex: 1;
  min-width: 0;
  display: block;
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.recent-conversation.unread .recent-conversation-title {
  color: var(--fn-text-primary);
  font-weight: 600;
}
.conversation-activity {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
}
.approval-state {
  color: #a96412;
}
.running-state {
  color: var(--fn-brand);
}
.running-state svg {
  animation: conversation-running 1.2s linear infinite;
}
.conversation-unread {
  flex: 0 0 6px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fn-brand);
}
@keyframes conversation-running {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .running-state svg {
    animation: none;
  }
}
.recent-conversations-empty {
  padding: 5px 8px;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.conversation-load-more {
  border: 0;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
  width: 100%;
  padding: 9px 6px;
  font-size: 12px;
}
.conversation-load-more:disabled {
  cursor: wait;
  opacity: 0.6;
}
.draft-badge {
  font-size: 10px;
  color: var(--fn-text-muted, #687180);
  flex-shrink: 0;
}
</style>
