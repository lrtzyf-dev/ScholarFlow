<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { ElNotification } from 'element-plus'
import axios from 'axios'
import { ShieldAlert, X } from 'lucide-vue-next'
import ResearchSidebar from './components/ResearchSidebar.vue'
import TopBar from './components/TopBar.vue'
import CommandPalette from './components/CommandPalette.vue'
import { findActiveZone, findActiveView, type NavView } from './navConfig'
import { taskState } from './taskState'
import { conversationArchive, retryConversationArchive } from './conversationArchive'
import {
  activeConversationId,
  desktopStateIssues,
  retrySelection,
  selectionStatus,
} from './desktopState'
import { getSettings, listModelProfiles } from './api'
import { changeReadingTextScale } from './readingZoom'
import { globalDefaultModelStatus, unknownModelStatus, type ModelStatus } from './modelStatus'

const route = useRoute()
const router = useRouter()
const { pendingApprovals } = taskState
let stopTaskUpdates: (() => void) | undefined

/* ---------- 当前分区 / 视图 ---------- */
const activeZone = computed(() => findActiveZone(route.path))
const activeView = computed(() => findActiveView(route.path))
const activeViewName = computed(() => route.name === 'source-reader' ? 'source-reader' : activeView.value?.name || 'chat')
const researchContext = ref({
  title: '新建研究',
  projectName: '',
  sourceCount: 0,
  sourcesOpen: false,
  modelStatus: unknownModelStatus(),
})
function updateResearchContext(event: Event) {
  const detail = (event as CustomEvent).detail
  if (!detail || typeof detail !== 'object') return
  researchContext.value = {
    title: typeof detail.title === 'string' && detail.title ? detail.title : '新建研究',
    projectName: typeof detail.projectName === 'string' ? detail.projectName : '',
    sourceCount: Number.isFinite(detail.sourceCount) ? Math.max(0, detail.sourceCount) : 0,
    sourcesOpen: Boolean(detail.sourcesOpen),
    modelStatus: detail.modelStatus && ['api', 'mock', 'unknown'].includes(detail.modelStatus.kind)
      ? detail.modelStatus as ModelStatus : unknownModelStatus(),
  }
}
function toggleSources() {
  window.dispatchEvent(new Event('scholarflow:toggle-sources'))
}
// Register before the child view mounts so its initial context reaches the top bar.
window.addEventListener('scholarflow:research-context', updateResearchContext)

/* ---------- 导航折叠 ---------- */
const panelCollapsed = ref(localStorage.getItem('scholarflow:panel:collapsed') === '1')
const compactMedia = window.matchMedia('(max-width: 760px)')
const compactViewport = ref(compactMedia.matches)
const mobilePanelOpen = ref(false)
const panelHidden = computed(() =>
  compactViewport.value ? !mobilePanelOpen.value : panelCollapsed.value
)
function syncCompactViewport(event: MediaQueryListEvent) {
  compactViewport.value = event.matches
  mobilePanelOpen.value = false
}
watch(
  () => route.fullPath,
  () => {
    mobilePanelOpen.value = false
  }
)
function setCollapsed(value: boolean) {
  const moveFocus = document.activeElement?.closest(
    value ? '.research-sidebar' : '.top-bar .bar-icon'
  )
  if (compactViewport.value) mobilePanelOpen.value = !value
  else {
    panelCollapsed.value = value
    localStorage.setItem('scholarflow:panel:collapsed', value ? '1' : '0')
  }
  if (moveFocus) {
    void nextTick(() => {
      document.querySelector<HTMLElement>(value ? '.top-bar .bar-icon' : '.research-brand')?.focus()
    })
  }
}
function togglePanel() {
  setCollapsed(!panelHidden.value)
}
function expandPanel() {
  if (panelHidden.value) setCollapsed(false)
}

async function startNewConversation() {
  mobilePanelOpen.value = false
  if (route.path !== '/chat') await router.push('/chat')
  await nextTick()
  window.dispatchEvent(new Event('scholarflow:new-conversation'))
}

/* ---------- 命令面板 ---------- */
const paletteOpen = ref(false)
function openPalette() {
  paletteOpen.value = true
}
function closePalette() {
  paletteOpen.value = false
}

/* ---------- 导航 ---------- */
function navigate(view: NavView) {
  paletteOpen.value = false
  mobilePanelOpen.value = false
  if (view.special === 'reader') {
    const last = localStorage.getItem('scholarflow:last-paper')
    if (last) router.push(`/reader/${last}`)
    else router.push('/library')
    return
  }
  if (view.to) router.push(view.to)
}

/* ---------- 在线 / 离线状态 ---------- */
const globalModelStatus = ref(unknownModelStatus())
const modelStatus = computed(() => activeViewName.value === 'chat'
  ? researchContext.value.modelStatus : globalModelStatus.value)
async function loadStatus() {
  try {
    const [settings, profiles] = await Promise.all([getSettings(), listModelProfiles()])
    globalModelStatus.value = globalDefaultModelStatus({ ...profiles, legacyCredentialError: Boolean(settings.credential_errors?.llm_api_key) })
  } catch {
    globalModelStatus.value = unknownModelStatus()
  }
}

/* ---------- 全局快捷键 ---------- */
function isMod(e: KeyboardEvent): boolean {
  return e.ctrlKey || e.metaKey
}
function onGlobalKeydown(e: KeyboardEvent) {
  const closeMobilePanel = e.key === 'Escape' && compactViewport.value && mobilePanelOpen.value
  if ((!isMod(e) && !closeMobilePanel) || e.defaultPrevented || e.isComposing || e.keyCode === 229)
    return
  if (
    [
      ...document.querySelectorAll<HTMLElement>(
        '[aria-modal="true"], dialog[open], .modal-mask, .el-overlay'
      ),
    ].some((element) => element.getClientRects().length > 0)
  )
    return
  const target = e.target instanceof HTMLElement ? e.target : null
  const editingText = Boolean(
    target?.isContentEditable ||
      target?.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]')
  )
  if (!e.altKey && !editingText && ['chat', 'reader', 'source-reader', 'compare', 'translate'].includes(activeViewName.value)) {
    const direction: -1 | 0 | 1 | null =
      e.key === '0' || e.code === 'Numpad0'
        ? 0
        : e.key === '+' || e.key === '=' || e.code === 'NumpadAdd'
          ? 1
          : e.key === '-' || e.code === 'NumpadSubtract'
            ? -1
            : null
    if (direction !== null) {
      e.preventDefault()
      if (activeViewName.value === 'source-reader') {
        window.dispatchEvent(new CustomEvent('scholarflow:source-zoom', { detail: { direction } }))
      } else if (activeViewName.value === 'reader') {
        window.dispatchEvent(
          new CustomEvent('scholarflow:reader-zoom', { detail: { direction } })
        )
      } else {
        changeReadingTextScale(direction)
      }
      return
    }
  }
  if (closeMobilePanel) {
    e.preventDefault()
    setCollapsed(true)
    return
  }
  const key = e.key.toLowerCase()
  if (key === 'k' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    paletteOpen.value = true
    return
  }
  if (key === 'b' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    togglePanel()
    return
  }
  if (key === 'o' && e.shiftKey && !e.altKey) {
    e.preventDefault()
    void startNewConversation()
    return
  }
  if (e.key === ',' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    if (activeViewName.value !== 'settings') router.push('/settings')
    return
  }
  if (key === 'j' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    if (activeViewName.value !== 'tasks') router.push('/tasks')
  }
}

/* ---------- 草稿 / 冲突横幅 ---------- */
const otherDraftIssues = computed(() =>
  desktopStateIssues.value.filter(
    (doc) =>
      activeViewName.value !== 'chat' ||
      doc.path !== `conversations/${encodeURIComponent(activeConversationId.value)}`
  )
)

// 关闭归档错误横幅：清空错误信息与重试操作
function dismissArchiveError() {
  conversationArchive.error = ''
  conversationArchive.retry = null
}

// 打开某份草稿对应的会话
function openDraft(doc: { path: string }) {
  router.push({
    path: '/chat',
    query: { conv: decodeURIComponent(doc.path.split('/').pop() || '') },
  })
}

onMounted(() => {
  stopTaskUpdates = taskState.startTaskUpdates(async (task) => {
    // Durable claims suppress repeated notifications across reconnects/windows;
    // the same preference also controls the unified center's unread indicators.
    let item: {status:string;title:string}|null = null
    try { item = (await axios.post('/api/run-center/notification', {item_id:'background:' + task.id})).data.item } catch { return }
    if (!item) return
    const labels: Record<string, string> = {
      completed: '后台任务已完成',
      partial: '后台任务部分完成',
      blocked: '后台任务需要处理',
      ended: '本轮已结束，目标待核对',
      failed: '后台任务失败',
      cancelled: '后台任务已取消',
      interrupted: '后台任务已中断，可继续',
    }
    ElNotification({
      title: labels[item.status] || '后台任务状态已更新',
      message: task.title,
      type: task.status === 'failed' ? 'error' : 'info',
      onClick: () => {
        void router.push('/tasks')
      },
    })
  })
  window.addEventListener('keydown', onGlobalKeydown)
  window.addEventListener('focus', loadStatus)
  window.addEventListener('scholarflow:model-profiles-updated', loadStatus)
  compactMedia.addEventListener('change', syncCompactViewport)
  void loadStatus()
})

onUnmounted(() => {
  stopTaskUpdates?.()
  window.removeEventListener('keydown', onGlobalKeydown)
  window.removeEventListener('focus', loadStatus)
  window.removeEventListener('scholarflow:model-profiles-updated', loadStatus)
  compactMedia.removeEventListener('change', syncCompactViewport)
  window.removeEventListener('scholarflow:research-context', updateResearchContext)
})
</script>

<template>
  <div class="app-shell">
    <ResearchSidebar
      v-show="!panelHidden"
      :active-view-name="activeViewName"
      @navigate="navigate"
      @collapse="togglePanel"
      @open-palette="openPalette"
      @new-conversation="startNewConversation"
    />

    <button
      v-if="compactViewport && mobilePanelOpen"
      type="button"
      class="mobile-scrim"
      aria-label="关闭导航"
      @click="setCollapsed(true)"
    />

    <div class="workspace" :inert="compactViewport && mobilePanelOpen ? true : undefined">
      <TopBar
        :view-label="
          activeViewName === 'chat' ? researchContext.title : activeView?.label || String(route.meta.title || '问答工作台')
        "
        :section-label="
          activeViewName === 'chat'
            ? researchContext.projectName
              ? `项目 / ${researchContext.projectName}`
              : '普通对话'
            : activeZone.label
        "
        :model-label="modelStatus.label"
        :model-title="modelStatus.title"
        :panel-collapsed="panelHidden"
        :is-chat="activeViewName === 'chat'"
        :source-count="researchContext.sourceCount"
        :sources-open="researchContext.sourcesOpen"
        @open-palette="openPalette"
        @expand-panel="expandPanel"
        @toggle-sources="toggleSources"
        @new-conversation="startNewConversation"
      />

      <main class="main">
        <div v-if="conversationArchive.error" class="state-banner archive-error" role="alert">
          <span>{{ conversationArchive.error }}</span>
          <button type="button" @click="retryConversationArchive">重试归档操作</button>
          <button
            type="button"
            aria-label="关闭归档提示"
            title="关闭提示"
            @click="dismissArchiveError"
          >
            <X :size="14" />
          </button>
        </div>

        <div v-if="otherDraftIssues.length" class="state-banner" role="status">
          <span>{{ otherDraftIssues.length }} 份草稿尚未保存或存在冲突。</span>
          <button
            v-for="doc in otherDraftIssues"
            :key="doc.path"
            type="button"
            @click="openDraft(doc)"
          >
            查看草稿
          </button>
        </div>

        <div v-if="selectionStatus === 'error'" class="state-banner" role="status">
          <span>最近会话选择尚未保存。</span>
          <button type="button" @click="retrySelection">重试</button>
        </div>

        <button
          v-if="pendingApprovals && activeViewName !== 'tasks'"
          type="button"
          class="approval-banner"
          @click="router.push('/tasks')"
        >
          <ShieldAlert :size="16" />
          <span>{{ pendingApprovals }} 项工具调用等待审批</span>
          <span class="approval-banner-action">查看任务</span>
        </button>

        <RouterView />
      </main>
    </div>
  </div>

  <CommandPalette :open="paletteOpen" @close="closePalette" @navigate="navigate" />
</template>

<style scoped>
.app-shell {
  display: flex;
  height: 100%;
  overflow: hidden;
}
.workspace {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  height: 100%;
}
.main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--fn-bg-layout);
}
.main > :last-child {
  flex: 1;
  min-height: 0;
}
.state-banner {
  flex: none;
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 8px 20px;
  font-size: 12px;
  background: #fff5df;
  color: #7a4b0b;
}
.state-banner button {
  border: 0;
  background: transparent;
  color: inherit;
  text-decoration: underline;
  cursor: pointer;
}
.approval-banner {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 0;
  border-bottom: 1px solid #e8d6a4;
  padding: 10px 20px;
  background: #fff8e5;
  color: #705119;
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.approval-banner-action {
  margin-left: auto;
  white-space: nowrap;
}
.mobile-scrim {
  position: fixed;
  inset: 0;
  z-index: 29;
  border: 0;
  padding: 0;
  background: var(--overlay);
}
@media (min-width: 1700px) {
  .app-shell {
    --sidebar-width: 260px;
    --inspector-width: 350px;
  }
}
@media (max-width: 1250px) {
  .app-shell {
    --sidebar-width: 224px;
    --inspector-width: 290px;
  }
}
@media (max-width: 760px) {
  .app-shell {
    --sidebar-width: 250px;
  }
}
</style>
