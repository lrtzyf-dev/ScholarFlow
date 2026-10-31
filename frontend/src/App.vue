<script setup lang="ts">
import { RouterView, useRoute, useRouter } from 'vue-router'
import ProjectSidebar from './components/ProjectSidebar.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { pinConversation, type Conversation, type ConversationSort } from './api'
import { useConversationList } from './conversationList'
import { taskState } from './taskState'
import { usePinnedOrder } from './pinnedOrder'
import { conversationArchive, changeConversationArchive, retryConversationArchive } from './conversationArchive'
import { activeConversationId, desktopStateIssues, selectionStatus, retrySelection, sidebarState } from './desktopState'
import { ElMessage, ElNotification } from 'element-plus'
import {
  MessageSquareText,
  LibraryBig,
  BookOpenText,
  Share2,
  ListTodo,
  Settings2,
  Info,
  ChevronDown,
  Plus,
  Pin,
  PinOff,
  PanelLeftClose,
  PanelLeftOpen,
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
} from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const { pendingApprovals, runningTasks } = taskState
let stopTaskUpdates: (() => void) | undefined
const pinningConversations = ref(new Set<string>())
const sidebarSaveStatus = sidebarState.status
const conversationSort = computed({
  get: () => sidebarState.value.value.sort,
  set: (sort) => sidebarState.update({ sort }),
})
const sidebarDate = ref(new Date())
let sidebarTimer: ReturnType<typeof setInterval> | undefined
const sidebarConversations = useConversationList({ scope: ref('active'), q: ref(''),
  sort: computed<ConversationSort>(() => conversationSort.value === 'created_at' ? 'sidebar_created_at' : 'sidebar_updated_at'), enabled: ref(true) })
const { items: recentConversations, error: conversationsError, cursor: conversationsCursor,
  loadingMore: conversationsLoadingMore, moreError: conversationsMoreError } = sidebarConversations
const refreshRecentConversations = sidebarConversations.refresh
const currentConversationId = computed(() => String(route.query.conv || activeConversationId.value || ''))
const pinOrder = usePinnedOrder(recentConversations, pinningConversations, refreshRecentConversations, sidebarConversations.pinsComplete)
function setPinMenuEl(element: unknown) { pinOrder.menuEl = element as HTMLElement | null }


function conversationStatus(conversation: Conversation): string {
  const states = []
  if (conversation.approval_count) states.push(`${conversation.approval_count} 项待审批`)
  if (conversation.running_count) states.push(`${conversation.running_count} 项运行中`)
  if (conversation.finishing_count) states.push(`${conversation.finishing_count} 项正在结束`)
  if (conversation.unread_count) states.push(`${conversation.unread_count} 条未读回答`)
  return states.join('，')
}


function conversationTimestamp(conversation: Conversation, field: 'updated_at' | 'created_at' | 'pinned_at' = conversationSort.value): number {
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
  const ordered = recentConversations.value.filter((conversation) => !conversation.pinned_at)
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

  return [{ label: '已固定', conversations: pinned }, ...buckets].filter((group) => group.conversations.length)
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
  if (saved.archived_at) recentConversations.value = recentConversations.value.filter((item) => item.id !== saved.id)
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

function onConversationUpdated() {
  void refreshRecentConversations()
}

interface NavItem {
  key: string
  label: string
  icon: any
  badge?: string
  to?: string
}
interface NavSection {
  id: string
  title: string
  items: NavItem[]
}

const sections: NavSection[] = [
  {
    id: 'common',
    title: '常用',
    items: [
      { key: 'chat', label: '问答工作台', icon: MessageSquareText, to: '/chat' },
      { key: 'library', label: '文献库', icon: LibraryBig, to: '/library' },
      { key: 'reader', label: '版式阅读', icon: BookOpenText },
      { key: 'graph', label: '引用图谱', icon: Share2, to: '/graph' },
      { key: 'tasks', label: '后台任务', icon: ListTodo, to: '/tasks' },
    ],
  },
  {
    id: 'admin',
    title: '管理',
    items: [
      { key: 'settings', label: '模型与设置', icon: Settings2, to: '/settings' },
      { key: 'about', label: '关于', icon: Info, to: '/about' },
    ],
  },
]

/* ---------- 整体折叠 ---------- */
const collapsed = ref(localStorage.getItem('scholarflow:sidebar:collapsed') === '1')
watch(() => [route.fullPath, collapsed.value], () => { pinOrder.closeMenu(); pinOrder.cancelDrag() })
function toggleCollapsed() {
  collapsed.value = !collapsed.value
  localStorage.setItem('scholarflow:sidebar:collapsed', collapsed.value ? '1' : '0')
}

/* ---------- 分组折叠 ---------- */
const foldedGroups = ref<Record<string, boolean>>(
  JSON.parse(localStorage.getItem('scholarflow:sidebar-groups') || '{}'),
)
function toggleGroup(id: string) {
  foldedGroups.value[id] = !foldedGroups.value[id]
  localStorage.setItem('scholarflow:sidebar-groups', JSON.stringify(foldedGroups.value))
}

const activeKey = computed(() => {
  const p = route.path
  if (p.startsWith('/library')) return 'library'
  if (p.startsWith('/reader')) return 'reader'
  if (p.startsWith('/graph')) return 'graph'
  if (p.startsWith('/tasks')) return 'tasks'
  if (p.startsWith('/settings')) return 'settings'
  if (p.startsWith('/about')) return 'about'
  return 'chat'
})
const otherDraftIssues = computed(() => desktopStateIssues.value.filter((doc) =>
  activeKey.value !== 'chat' || doc.path !== `conversations/${encodeURIComponent(activeConversationId.value)}`,
))

function go(item: NavItem) {
  if (item.key === 'reader') {
    const last = localStorage.getItem('scholarflow:last-paper')
    if (last) router.push(`/reader/${last}`)
    else router.push('/library')
    return
  }
  if (item.to) router.push(item.to)
}

/* ---------- 全局键盘快捷键（跨页面生效） ---------- */
function isMod(e: KeyboardEvent): boolean {
  return e.ctrlKey || e.metaKey
}

function onGlobalKeydown(e: KeyboardEvent) {
  if (!isMod(e) || e.defaultPrevented) return
  const key = e.key.toLowerCase()
  if (key === 'k' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    searchConversations()
    return
  }
  // Ctrl/Cmd+B：折叠/展开侧边栏
  if (key === 'b' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    toggleCollapsed()
    return
  }
  // Ctrl/Cmd+, ：打开设置
  if (e.key === ',' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    if (activeKey.value !== 'settings') router.push('/settings')
    return
  }
  // Ctrl/Cmd+J：后台任务
  if (key === 'j' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    if (activeKey.value !== 'tasks') router.push('/tasks')
  }
}

onMounted(() => {
  stopTaskUpdates = taskState.startTaskUpdates((task) => {
    const labels: Record<string, string> = { done: '后台任务已完成', failed: '后台任务失败', cancelled: '后台任务已取消', interrupted: '后台任务已中断，可继续' }
    ElNotification({
      title: labels[task.status], message: task.title,
      type: task.status === 'failed' ? 'error' : 'info',
      onClick: () => { void router.push('/tasks') },
    })
    void refreshRecentConversations()
  })
  window.addEventListener('keydown', onGlobalKeydown)
  window.addEventListener('scholarflow:conversation-updated', onConversationUpdated)
  window.addEventListener('scholarflow:conversation-archive', onConversationArchived)
  window.addEventListener('focus', onSidebarFocus)
  sidebarTimer = setInterval(() => { sidebarDate.value = new Date() }, 60_000)
  void refreshRecentConversations()
})
onUnmounted(() => {
  stopTaskUpdates?.()
  window.removeEventListener('keydown', onGlobalKeydown)
  window.removeEventListener('scholarflow:conversation-updated', onConversationUpdated)
  window.removeEventListener('scholarflow:conversation-archive', onConversationArchived)
  window.removeEventListener('focus', onSidebarFocus)
  clearInterval(sidebarTimer)
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ collapsed }">
      <!-- 品牌 -->
      <div class="brand">
        <svg class="brand-logo" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4H11v15H6.5A2.5 2.5 0 0 0 4 16.5v-10Z" fill="var(--fn-brand)"/>
          <path d="M20 6.5A2.5 2.5 0 0 0 17.5 4H13v15h4.5a2.5 2.5 0 0 1 2.5 2.5v-15Z" fill="var(--fn-brand)" opacity="0.55"/>
          <path d="M12 5.5v12" stroke="#fff" stroke-width="1.2"/>
        </svg>
        <div v-if="!collapsed" class="brand-text">
          <div class="brand-name">研序 ScholarFlow</div>
          <div class="brand-sub">全流程科研助手</div>
        </div>
      </div>

      <!-- 导航 -->
      <nav class="nav-body">
        <template v-for="sec in sections" :key="sec.id">
          <div class="nav-group">
            <button
              v-if="!collapsed"
              class="nav-group-header"
              type="button"
              @click="toggleGroup(sec.id)"
            >
              <span class="nav-group-label">{{ sec.title }}</span>
              <ChevronDown
                :size="14"
                class="nav-group-chevron"
                :class="{ folded: foldedGroups[sec.id] }"
              />
            </button>
            <div v-show="collapsed || !foldedGroups[sec.id]" class="nav-group-items">
              <div
                v-for="item in sec.items"
                :key="item.key"
                class="nav-item-row"
              >
                <button
                  class="nav-item"
                  :class="{ active: activeKey === item.key }"
                  type="button"
                  :title="item.key === 'tasks' ? `${item.label}：${pendingApprovals} 项待审批，${runningTasks} 项运行中` : item.label"
                  @click="go(item)"
                >
                  <component :is="item.icon" :size="16" :stroke-width="1.8" class="nav-ico" />
                  <span v-if="!collapsed" class="nav-label">{{ item.label }}</span>
                  <span v-if="!collapsed && item.badge" class="nav-badge">{{ item.badge }}</span>
                  <span v-if="item.key === 'tasks' && (pendingApprovals || runningTasks)"
                        class="task-badge" :class="{ attention: pendingApprovals }"
                        :aria-label="pendingApprovals ? `${pendingApprovals} 项待审批` : `${runningTasks} 项运行中`">
                    {{ pendingApprovals || runningTasks }}
                  </span>
                </button>
                <div v-if="item.key === 'chat' && !collapsed" class="recent-conversations">
                  <ProjectSidebar :current-id="currentConversationId" />
                  <div class="recent-conversations-head">
                    <span>会话</span>
                    <div class="recent-conversations-actions">
                      <button type="button" aria-label="搜索会话" title="搜索会话（Ctrl+K）" @click.stop="searchConversations"><Search :size="14" /></button>
                      <button type="button" aria-label="新建会话" title="新建会话" @click.stop="startNewConversation">
                        <Plus :size="15" :stroke-width="1.8" />
                      </button>
                      <button type="button" @click.stop="openAllConversations">全部</button>
                    </div>
                  </div>
                  <select v-model="conversationSort" class="conversation-sort" aria-label="会话排序"
                          :disabled="sidebarSaveStatus === 'loading'" title="会话排序">
                    <option value="updated_at">最近更新</option>
                    <option value="created_at">最近创建</option>
                  </select>
                  <div v-if="sidebarSaveStatus === 'error'" class="sidebar-save-error" role="status">
                    排序尚未保存<button type="button" @click="sidebarState.retry()">重试</button>
                  </div>
                  <div class="conversation-list">
                    <section v-for="group in conversationDateGroups" :key="group.label" class="conversation-date-group">
                      <div class="conversation-date-label">{{ group.label }}</div>
                      <div v-for="conversation in group.conversations" :key="conversation.id"
                           class="recent-conversation-row" :data-conversation-id="conversation.id"
                           :class="{ active: currentConversationId === conversation.id, dragging: pinOrder.dragging === conversation.id,
                             'drop-before': pinOrder.dropTarget === conversation.id && pinOrder.dropEdge === 'before',
                             'drop-after': pinOrder.dropTarget === conversation.id && pinOrder.dropEdge === 'after' }"
                           @dragover="conversation.pinned_at && pinOrder.dragOver($event, conversation.id)"
                           @dragleave="pinOrder.dragLeave($event, conversation.id)"
                           @drop="conversation.pinned_at && pinOrder.drop($event, conversation.id)">
                        <button v-if="conversation.pinned_at" type="button" class="conversation-reorder"
                                :data-pin-handle-id="conversation.id" :draggable="!pinOrder.busy && pinOrder.pinned.length > 1"
                                :disabled="pinOrder.busy || pinOrder.pinned.length < 2"
                                :aria-label="`调整固定顺序 ${conversation.title}`" title="调整固定顺序"
                                aria-haspopup="menu" :aria-expanded="pinOrder.menu?.id === conversation.id"
                                @click.stop="pinOrder.openMenu($event, conversation.id)"
                                @keydown="pinOrder.handleKeydown($event, conversation.id)"
                                @dragstart="pinOrder.startDrag($event, conversation.id)" @dragend="pinOrder.cancelDrag">
                          <GripVertical :size="13" />
                        </button>
                        <button
                          type="button"
                          class="recent-conversation"
                          :class="{ active: currentConversationId === conversation.id, unread: conversation.unread_count }"
                          :title="[conversation.title, conversationStatus(conversation)].filter(Boolean).join(' · ')"
                          :aria-label="[conversation.title, conversationStatus(conversation)].filter(Boolean).join('，')"
                          @click="openRecentConversation(conversation)"
                        >
                          <span class="recent-conversation-title">{{ conversation.title || '新会话' }}</span>
                          <span v-if="conversation.approval_count" class="conversation-activity approval-state" aria-hidden="true"><ShieldAlert :size="13" /></span>
                          <span v-else-if="conversation.running_count" class="conversation-activity running-state" aria-hidden="true"><LoaderCircle :size="13" /></span>
                          <span v-else-if="conversation.finishing_count" class="conversation-activity" aria-hidden="true"><CircleStop :size="13" /></span>
                          <span v-else-if="conversation.draft_only" class="draft-badge">{{ conversation.interrupted_only ? '未完成' : '草稿' }}</span>
                          <span v-if="conversation.unread_count" class="conversation-unread" aria-hidden="true"></span>
                        </button>
                        <button type="button" class="conversation-pin" :class="{ pinned: conversation.pinned_at }"
                                :disabled="pinOrder.saving || pinningConversations.has(conversation.id)"
                                :aria-label="conversation.pinned_at ? `取消固定 ${conversation.title}` : `固定 ${conversation.title}`"
                                :title="conversation.pinned_at ? '取消固定会话' : '固定会话'" @click="toggleConversationPin(conversation)">
                          <PinOff v-if="conversation.pinned_at" :size="13" :stroke-width="1.8" />
                          <Pin v-else :size="13" :stroke-width="1.8" />
                        </button>
                        <button type="button" class="conversation-pin"
                                :disabled="pinOrder.busy || conversationArchive.pending.has(conversation.id) || Boolean(conversation.running_count || conversation.finishing_count)"
                                :aria-label="`归档 ${conversation.title}`" :title="conversation.running_count || conversation.finishing_count ? '任务结束后可归档' : '归档会话'"
                                @click="archiveConversation(conversation)">
                          <Archive :size="13" :stroke-width="1.8" />
                        </button>
                      </div>
                    </section>
                    <button v-if="conversationsCursor" class="conversation-load-more" type="button" :disabled="conversationsLoadingMore || sidebarConversations.loading.value"
                            @click="sidebarConversations.loadMore">{{ conversationsLoadingMore ? '正在加载…' : conversationsMoreError ? '重试加载更多会话' : '加载更多会话' }}</button>
                  </div>
                  <div v-if="conversationsError" class="sidebar-save-error" role="status">
                    会话加载失败<button type="button" @click="refreshRecentConversations">重试</button>
                  </div>
                  <div v-if="pinOrder.saving || pinOrder.error" class="sidebar-save-error" role="status" aria-live="polite">
                    {{ pinOrder.saving ? '正在保存固定顺序…' : pinOrder.error }}
                    <button v-if="pinOrder.canRetry && !pinOrder.saving" type="button" @click="pinOrder.retry">重试排序</button>
                    <button v-else-if="pinOrder.error && !pinOrder.saving" type="button" @click="pinOrder.dismissError" aria-label="关闭排序提示"><X :size="12" /></button>
                  </div>
                  <div v-else-if="!recentConversations.length" class="recent-conversations-empty">
                    尚无历史会话
                  </div>
                </div>
              </div>
            </div>
          </div>
        </template>
      </nav>

      <!-- 底部折叠 -->
      <div class="sidebar-foot">
        <button class="collapse-btn" type="button" :title="collapsed ? '展开' : '折叠'" @click="toggleCollapsed">
          <PanelLeftOpen v-if="collapsed" :size="16" :stroke-width="1.8"/>
          <PanelLeftClose v-else :size="16" :stroke-width="1.8"/>
        </button>
      </div>
    </aside>

    <main class="main">
      <div v-if="conversationArchive.error" class="state-banner archive-error" role="alert">
        <span>{{ conversationArchive.error }}</span>
        <button v-if="conversationArchive.retry" type="button" @click="retryConversationArchive">重试归档操作</button>
        <button type="button" aria-label="关闭归档提示" title="关闭提示" @click="conversationArchive.error = ''; conversationArchive.retry = null"><X :size="14" /></button>
      </div>
      <div v-if="otherDraftIssues.length" class="state-banner" role="status">
        <span>{{ otherDraftIssues.length }} 份草稿尚未保存或存在冲突。</span>
        <button v-for="doc in otherDraftIssues" :key="doc.path" type="button"
                @click="router.push({ path: '/chat', query: { conv: decodeURIComponent(doc.path.split('/').pop() || '') } })">查看草稿</button>
      </div>
      <div v-if="selectionStatus === 'error'" class="state-banner" role="status">
        <span>最近会话选择尚未保存。</span><button type="button" @click="retrySelection">重试</button>
      </div>
      <button v-if="pendingApprovals && activeKey !== 'tasks'" type="button"
              class="approval-banner" @click="router.push('/tasks')">
        <ShieldAlert :size="16" />
        <span>{{ pendingApprovals }} 项工具调用等待审批</span>
        <span class="approval-banner-action">查看任务</span>
      </button>
      <RouterView />
    </main>
  </div>
  <Teleport to="body">
    <div v-if="pinOrder.menu" :ref="setPinMenuEl" class="pin-order-menu" role="menu" aria-label="固定会话顺序"
         :style="{ left: `${pinOrder.menu.x}px`, top: `${pinOrder.menu.y}px` }" @click.stop @keydown="pinOrder.menuKeydown">
      <button type="button" role="menuitem" :disabled="!pinOrder.canMove(pinOrder.menu.id, 'top')" @click="pinOrder.move(pinOrder.menu.id, 'top')"><ArrowUpToLine :size="14" />移到首位</button>
      <button type="button" role="menuitem" :disabled="!pinOrder.canMove(pinOrder.menu.id, 'up')" @click="pinOrder.move(pinOrder.menu.id, 'up')"><ArrowUp :size="14" />上移</button>
      <button type="button" role="menuitem" :disabled="!pinOrder.canMove(pinOrder.menu.id, 'down')" @click="pinOrder.move(pinOrder.menu.id, 'down')"><ArrowDown :size="14" />下移</button>
      <button type="button" role="menuitem" :disabled="!pinOrder.canMove(pinOrder.menu.id, 'bottom')" @click="pinOrder.move(pinOrder.menu.id, 'bottom')"><ArrowDownToLine :size="14" />移到末位</button>
    </div>
  </Teleport>
</template>

<style scoped>
.conversation-load-more { border: 0; background: transparent; color: var(--fn-text-secondary); cursor: pointer; width: 100%; padding: 9px 6px; font-size: 12px; }
.conversation-load-more:disabled { cursor: wait; opacity: .6; }
.draft-badge { font-size: 10px; color: var(--fn-text-muted, #687180); flex-shrink: 0; }
.state-banner { display: flex; gap: 12px; align-items: center; padding: 8px 20px; font-size: 12px; background: #fff5df; color: #7a4b0b; }
.state-banner button { border: 0; background: transparent; color: inherit; text-decoration: underline; cursor: pointer; }
.app-shell {
  display: flex;
  height: 100%;
  overflow: hidden;
}

/* ---------- 侧边栏 ---------- */
.sidebar {
  padding: 0;
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--fn-bg-primary);
  border-right: 1px solid var(--fn-border-primary);
  transition: width 0.18s cubic-bezier(0.4, 0, 0.2, 1);
}
.sidebar.collapsed {
  width: 56px;
}

/* 品牌 */
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 14px 12px;
}
.brand-logo {
  width: 30px;
  height: 30px;
  flex-shrink: 0;
}
.sidebar.collapsed .brand {
  justify-content: center;
  padding: 14px 0 12px;
}
.brand-text {
  min-width: 0;
}
.brand-name {
  font-size: 13.5px;
  font-weight: 700;
  color: var(--fn-text-primary);
  white-space: nowrap;
  line-height: 1.25;
}
.brand-sub {
  font-size: 11px;
  color: var(--fn-text-tertiary);
  white-space: nowrap;
  margin-top: 1px;
}

/* 导航体 */
.nav-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 4px 10px;
}
.sidebar.collapsed .nav-body {
  padding: 4px 8px;
}
.nav-group {
  margin-bottom: 6px;
}
.nav-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 10px;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: var(--fn-radius-md);
}
.nav-group-header:hover {
  background: color-mix(in srgb, var(--fn-text-primary) 3%, transparent);
}
.nav-group-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--fn-text-tertiary);
  letter-spacing: 0;
  text-transform: uppercase;
}
.nav-group-chevron {
  color: var(--fn-text-tertiary);
  opacity: 0.4;
  transition: transform 0.2s ease;
}
.nav-group-chevron.folded {
  transform: rotate(-90deg);
}

.nav-item-row {
  display: flex;
  flex-direction: column;
  margin-bottom: 2px;
  border-radius: var(--fn-radius-md);
}
.recent-conversations {
  padding: 8px 4px 6px 25px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-height: clamp(210px, calc(100dvh - 480px), 420px);
}
.recent-conversations-head {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 24px;
  margin-bottom: 2px;
  color: var(--fn-text-tertiary);
  font-size: 11px;
}
.recent-conversations-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}
.recent-conversations-head button {
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
.recent-conversations-head button:hover {
  background: var(--fn-bg-active);
  color: var(--fn-brand);
}
.conversation-sort {
  flex-shrink: 0;
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
.conversation-list {
  flex: 1;
  min-height: 40px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
}
.sidebar-save-error {
  padding: 4px;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.sidebar-save-error button {
  border: 0;
  background: transparent;
  color: var(--fn-brand);
  font: inherit;
  cursor: pointer;
}
.conversation-date-group + .conversation-date-group .conversation-date-label {
  margin-top: 4px;
  border-top: 1px solid var(--fn-border-secondary);
  padding-top: 7px;
}
.conversation-date-label {
  padding: 6px 8px 3px;
  color: var(--fn-text-tertiary);
  font-size: 10px;
  line-height: 16px;
}
.recent-conversation-row {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: 6px;
}
.recent-conversation-row.dragging { opacity: .5; }
.recent-conversation-row.drop-before::before,
.recent-conversation-row.drop-after::after { content: ''; position: absolute; left: 0; right: 0; height: 2px; background: var(--fn-brand); pointer-events: none; }
.recent-conversation-row.drop-before::before { top: 0; }
.recent-conversation-row.drop-after::after { bottom: 0; }
.conversation-reorder { display: flex; align-items: center; justify-content: center; flex: 0 0 16px; width: 16px; height: 26px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--fn-text-tertiary); cursor: grab; }
.conversation-reorder:hover { color: var(--fn-brand); }
.conversation-reorder:active { cursor: grabbing; }
.conversation-reorder:disabled { opacity: .4; cursor: default; }
.pin-order-menu { position: fixed; width: 140px; z-index: 3000; padding: 4px; background: var(--fn-bg-primary); border: 1px solid var(--fn-border-primary); border-radius: 6px; box-shadow: var(--fn-shadow-md); }
.pin-order-menu button { display: flex; align-items: center; gap: 8px; width: 100%; height: 32px; padding: 0 8px; border: 0; border-radius: 4px; background: transparent; color: var(--fn-text-primary); font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.pin-order-menu button:hover { background: var(--fn-bg-active); }
.pin-order-menu button:disabled { color: var(--fn-text-tertiary); cursor: default; background: transparent; }
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
.conversation-pin:hover { color: var(--fn-brand); }
.conversation-pin:disabled { cursor: wait; opacity: 0.4; }
.recent-conversation {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  height: 30px;
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
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.recent-conversation.unread .recent-conversation-title { color: var(--fn-text-primary); font-weight: 600; }
.conversation-activity { display: flex; align-items: center; justify-content: center; flex: 0 0 14px; width: 14px; height: 14px; }
.approval-state { color: #a96412; }
.running-state { color: var(--fn-brand); }
.running-state svg { animation: conversation-running 1.2s linear infinite; }
.conversation-unread { flex: 0 0 6px; width: 6px; height: 6px; border-radius: 50%; background: var(--fn-brand); }
@keyframes conversation-running { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .running-state svg { animation: none; } }
.recent-conversations-empty {
  padding: 5px 8px;
  color: var(--fn-text-quaternary);
  font-size: 11px;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
  width: 100%;
  height: 40px;
  padding: 0 12px;
  border: none;
  border-radius: var(--fn-radius-md);
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
  text-align: left;
  transition: background var(--fn-transition-fast), color var(--fn-transition-fast);
}
.sidebar.collapsed .nav-item {
  justify-content: center;
  padding: 0;
}
.nav-item:hover {
  background: color-mix(in srgb, var(--fn-text-primary) 4%, transparent);
  color: var(--fn-text-primary);
}
.nav-item.active {
  background: var(--fn-bg-active);
  color: var(--fn-brand);
  font-weight: 600;
}
.nav-ico {
  flex-shrink: 0;
}
.nav-label {
  flex: 1;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.nav-badge {
  font-size: 9px;
  font-weight: 600;
  color: #fff;
  background: var(--fn-brand);
  padding: 1px 4px;
  border-radius: 2px;
  text-transform: uppercase;
}

/* 底部 */
.sidebar-foot {
  padding: 8px 10px;
  border-top: 1px solid var(--fn-border-secondary);
}
.sidebar.collapsed .sidebar-foot {
  padding: 8px 0;
  display: flex;
  justify-content: center;
}
.collapse-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 34px;
  border: none;
  border-radius: var(--fn-radius-md);
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: pointer;
}
.sidebar.collapsed .collapse-btn {
  width: 40px;
}
.collapse-btn:hover {
  background: color-mix(in srgb, var(--fn-text-primary) 6%, transparent);
  color: var(--fn-text-primary);
}

/* ---------- 主区 ---------- */
.main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  background: var(--fn-bg-layout);
}
.main > :last-child { flex: 1; min-height: 0; }
.task-badge { min-width: 18px; height: 18px; padding: 0 4px; border-radius: 4px; background: var(--fn-bg-active); color: var(--fn-brand); font-size: 11px; text-align: center; line-height: 18px; }
.task-badge.attention { background: #fff0ce; color: #855700; }
.sidebar.collapsed .task-badge { position: absolute; top: 0; right: 0; font-size: 10px; }
.approval-banner { display: flex; flex: none; align-items: center; gap: 8px; width: 100%; border: 0; border-bottom: 1px solid #e8d6a4; padding: 10px 20px; background: #fff8e5; color: #705119; font: inherit; font-size: 12px; text-align: left; cursor: pointer; }
.approval-banner svg { flex-shrink: 0; }
.approval-banner-action { margin-left: auto; white-space: nowrap; }
</style>
