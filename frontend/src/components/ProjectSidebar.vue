<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { FolderOpen, Plus, RefreshCw } from 'lucide-vue-next'
import { createConversation, lookupConversations, type Conversation } from '../api'
import {
  listProjects,
  listProjectConversations,
  addProject,
  projectError,
  type Project,
} from '../projectApi'
import { taskState } from '../taskState'
const router = useRouter()
const props = defineProps<{ currentId: string }>()
const projects = ref<Project[]>([]),
  expanded = ref(''),
  rows = ref<Conversation[]>([])
const cursor = ref<string | null>(null),
  error = ref(''),
  busy = ref(false),
  creating = ref(false)
const adding = ref(false),
  folder = ref('')
const nativePicker = ref(false)
type DesktopWindow = Window & {
  pywebview?: { api?: { choose_project_folder?: () => Promise<string | null> } }
}
let epoch = 0,
  disposed = false
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let refreshQueued = false
async function load() {
  try {
    const result = await listProjects()
    if (!disposed) {
      projects.value = result
      error.value = ''
    }
  } catch (e) {
    if (!disposed) error.value = projectError(e)
  }
}
async function chats(id: string, more = false) {
  const request = ++epoch,
    next = more ? cursor.value : undefined
  // A metadata refresh must keep pages the user has already expanded.
  const retained = !more && rows.value.length > 40 ? rows.value.map((chat) => chat.id) : []
  busy.value = true
  error.value = ''
  try {
    const reads = []
    for (let offset = 0; offset < retained.length; offset += 100)
      reads.push(lookupConversations(retained.slice(offset, offset + 100), 'active'))
    const [result, older] = await Promise.all([
      listProjectConversations(id, next || undefined),
      Promise.all(reads),
    ])
    if (request !== epoch || disposed) return
    const previous = more ? rows.value : older.flatMap((page) => page.conversations)
    rows.value = [
      ...new Map(
        [...previous, ...result.conversations]
          .filter((chat) => chat.project_id === id)
          .map((chat) => [chat.id, chat])
      ).values(),
    ].sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id.localeCompare(a.id))
    if (more || !retained.length) cursor.value = result.next_cursor
  } catch (e) {
    if (request === epoch && !disposed) error.value = projectError(e)
  } finally {
    if (request === epoch && !disposed) {
      busy.value = false
      if (refreshQueued) {
        refreshQueued = false
        scheduleRefresh()
      }
    }
  }
}
function scheduleRefresh() {
  if (disposed || refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = undefined
    void load()
    if (!expanded.value) return
    if (busy.value) refreshQueued = true
    else void chats(expanded.value)
  }, 180)
}
function toggle(id: string) {
  epoch++
  busy.value = false
  refreshQueued = false
  expanded.value = expanded.value === id ? '' : id
  rows.value = []
  cursor.value = null
  error.value = ''
  if (expanded.value) void chats(id)
}
async function newChat(project: Project) {
  if (creating.value) return
  creating.value = true
  error.value = ''
  try {
    const chat = await createConversation(undefined, project.id)
    await router.push({ path: '/chat', query: { conv: chat.id } })
    if (expanded.value !== project.id) {
      rows.value = []
      cursor.value = null
    }
    expanded.value = project.id
    await chats(project.id)
    window.dispatchEvent(new Event('scholarflow:conversation-updated'))
  } catch (e) {
    error.value = projectError(e)
  } finally {
    creating.value = false
  }
}
async function pickFolder() {
  if (creating.value) return
  try {
    const path = await (window as DesktopWindow).pywebview?.api?.choose_project_folder?.()
    if (path) {
      folder.value = path
      await add()
    }
  } catch (e) {
    error.value = projectError(e)
  }
}
function detectNativePicker() {
  nativePicker.value = Boolean((window as DesktopWindow).pywebview?.api?.choose_project_folder)
}
async function add() {
  if (!folder.value.trim() || creating.value) return
  creating.value = true
  error.value = ''
  try {
    const project = await addProject(folder.value)
    folder.value = ''
    adding.value = false
    await load()
    rows.value = []
    cursor.value = null
    expanded.value = project.id
    await chats(project.id)
    window.dispatchEvent(new Event('scholarflow:projects-updated'))
  } catch (e) {
    error.value = projectError(e)
  } finally {
    creating.value = false
  }
}
watch(taskState.conversationVersion, scheduleRefresh)
onMounted(() => {
  detectNativePicker()
  void load()
  window.addEventListener('scholarflow:projects-updated', scheduleRefresh)
  window.addEventListener('scholarflow:conversation-updated', scheduleRefresh)
  window.addEventListener('pywebviewready', detectNativePicker)
})
onUnmounted(() => {
  disposed = true
  epoch++
  clearTimeout(refreshTimer)
  window.removeEventListener('scholarflow:projects-updated', scheduleRefresh)
  window.removeEventListener('scholarflow:conversation-updated', scheduleRefresh)
  window.removeEventListener('pywebviewready', detectNativePicker)
})
</script>
<template>
  <section class="sidebar-projects" aria-label="项目">
    <header>
      <span>项目</span
      ><button class="project-refresh" title="刷新项目" aria-label="刷新项目列表" @click="load">
        <RefreshCw :size="12" /></button
      ><button
        title="添加项目"
        aria-label="添加项目"
        :aria-expanded="adding"
        @click="adding = !adding"
      >
        <Plus :size="14" />
      </button>
    </header>
    <form v-if="adding" @submit.prevent="add">
      <button
        v-if="nativePicker"
        type="button"
        :disabled="creating"
        aria-label="选择本地文件夹"
        title="选择本地文件夹"
        @click="pickFolder"
      >
        <FolderOpen :size="15" />
      </button>
      <input v-model="folder" aria-label="项目文件夹路径" placeholder="文件夹绝对路径" /><button
        :disabled="creating || !folder.trim()"
        aria-label="保存项目"
        title="保存项目"
      >
        <Plus :size="14" />
      </button>
    </form>
    <div class="sidebar-project-scroll">
      <div v-for="project in projects" :key="project.id" class="sidebar-project">
        <div class="sidebar-project-row">
          <button
            class="project-expand"
            :aria-expanded="expanded === project.id"
            :title="project.root_path"
            @click="toggle(project.id)"
          >
            <svg class="folder-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M3 6h7l2 3h9v11H3zM3 6V4h7l2 2" /></svg
            ><span>{{ project.name }}</span></button
          ><button
            class="project-add-chat"
            :disabled="creating || !project.available"
            :title="`在 ${project.name} 新建会话`"
            :aria-label="`在 ${project.name} 新建会话`"
            @click="newChat(project)"
          >
            <Plus :size="14" />
          </button>
        </div>
        <div v-if="expanded === project.id" class="project-chats" :aria-busy="busy">
          <button
            v-for="chat in rows"
            :key="chat.id"
            :class="{ active: chat.id === props.currentId }"
            :title="chat.title"
            @click="router.push({ path: '/chat', query: { conv: chat.id } })"
          >
            <span>{{ chat.title }}</span
            ><small v-if="chat.unread_count">{{ chat.unread_count }}</small>
          </button>
          <span v-if="busy && !rows.length" role="status" class="project-empty">正在载入…</span
          ><span v-else-if="!rows.length" class="project-empty">暂无会话</span>
          <button v-if="cursor" :disabled="busy" @click="chats(project.id, true)">加载更多</button>
        </div>
      </div>
      <span v-if="!projects.length && !adding && !error" class="project-empty">暂无项目</span>
    </div>
    <div v-if="error" role="alert" class="project-error">
      {{ error }}<button @click="expanded ? chats(expanded) : load()">重试</button>
    </div>
  </section>
</template>
<style scoped>
.sidebar-projects {
  display: flex;
  flex-direction: column;
  min-height: 56px;
  max-height: 230px;
  padding: 0 4px;
  flex-shrink: 0;
}
header,
.sidebar-project-row {
  display: flex;
  align-items: center;
  gap: 2px;
}
header {
  padding: 0 8px 7px;
  font-size: 12px;
  color: var(--fn-text-secondary, #666);
}
header > span {
  flex: 1;
}
button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: inherit;
  background: transparent;
  border: 0;
  border-radius: 4px;
  padding: 5px;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
button:hover,
.active {
  background: var(--fn-bg-active, #eee);
}
button:disabled {
  opacity: 0.4;
  cursor: default;
}
button:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
.sidebar-project-scroll {
  max-height: 150px;
  overflow: auto;
}
.project-expand {
  flex: 1;
  min-width: 0;
  text-align: left;
  min-height: 37px;
  padding: 9px 8px;
  gap: 11px;
  font-size: 14px;
  color: var(--nav-text);
}
.folder-icon {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.65;
  stroke-linecap: round;
  stroke-linejoin: round;
  color: var(--nav-icon);
}
.project-refresh,
.project-add-chat {
  opacity: 0;
}
header:hover .project-refresh,
header:focus-within .project-refresh,
.sidebar-project-row:hover .project-add-chat,
.sidebar-project-row:focus-within .project-add-chat {
  opacity: 1;
}
.project-expand span,
.project-chats button span {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}
svg {
  flex-shrink: 0;
}
.folded {
  transform: rotate(-90deg);
}
.project-chats {
  padding: 2px 0 5px 25px;
  margin-left: 14px;
  border-left: 1px solid var(--nav-line, #d7e0ef);
}
.project-chats > button {
  display: flex;
  width: 100%;
  text-align: left;
  min-height: 30px;
  font-size: 12px;
}
.project-chats small {
  color: var(--fn-brand);
}
.project-empty {
  display: block;
  color: var(--fn-text-secondary, #666);
  font-size: 11px;
  padding: 5px;
}
.project-error {
  font-size: 11px;
  color: var(--fn-danger);
  overflow-wrap: anywhere;
}
form {
  display: flex;
  gap: 4px;
  margin: 4px 0 8px;
}
input {
  width: 100%;
  min-width: 0;
  padding: 5px;
  border: 1px solid var(--fn-border, #ddd);
  border-radius: 4px;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 11px;
}
.sidebar-project-scroll {
  min-height: 0;
  max-height: none;
  overflow: auto;
  scrollbar-gutter: stable;
}
header,
form {
  flex-shrink: 0;
}
</style>
