<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { FolderOpen, Plus, ChevronDown, RefreshCw } from 'lucide-vue-next'
import { createConversation, type Conversation } from '../api'
import { listProjects, listProjectConversations, addProject, projectError, type Project } from '../projectApi'
import { taskState } from '../taskState'
const router = useRouter()
const props = defineProps<{ currentId: string }>()
const projects = ref<Project[]>([]), expanded = ref(''), rows = ref<Conversation[]>([])
const cursor = ref<string | null>(null), error = ref(''), busy = ref(false), creating = ref(false)
const adding = ref(false), folder = ref('')
let epoch = 0, disposed = false
async function load() {
  try { const result = await listProjects(); if (!disposed) { projects.value = result; error.value = '' } }
  catch (e) { if (!disposed) error.value = projectError(e) }
}
async function chats(id: string, more = false) {
  const request = ++epoch, next = more ? cursor.value : undefined
  busy.value = true; error.value = ''
  try {
    const result = await listProjectConversations(id, next || undefined)
    if (request !== epoch || disposed) return
    rows.value = more ? [...new Map([...rows.value, ...result.conversations].map(c => [c.id, c])).values()] : result.conversations
    cursor.value = result.next_cursor
  } catch (e) { if (request === epoch && !disposed) error.value = projectError(e) }
  finally { if (request === epoch && !disposed) busy.value = false }
}
function toggle(id: string) {
  epoch++; expanded.value = expanded.value === id ? '' : id; rows.value = []; cursor.value = null; error.value = ''
  if (expanded.value) void chats(id)
}
async function newChat(project: Project) {
  if (creating.value) return
  creating.value = true; error.value = ''
  try { const chat = await createConversation(undefined, project.id); await router.push({ path: '/chat', query: { conv: chat.id } }); expanded.value = project.id; await chats(project.id); window.dispatchEvent(new Event('scholarflow:conversation-updated')) }
  catch (e) { error.value = projectError(e) }
  finally { creating.value = false }
}
async function add() {
  if (!folder.value.trim() || creating.value) return
  creating.value = true; error.value = ''
  try { const project = await addProject(folder.value); folder.value = ''; adding.value = false; await load(); expanded.value = project.id; await chats(project.id) }
  catch (e) { error.value = projectError(e) }
  finally { creating.value = false }
}
watch(taskState.conversationVersion, () => { void load(); if (expanded.value) void chats(expanded.value) })
onMounted(() => { void load(); window.addEventListener('scholarflow:projects-updated', load) })
onUnmounted(() => { disposed = true; epoch++; window.removeEventListener('scholarflow:projects-updated', load) })
</script>
<template>
  <section class="sidebar-projects" aria-label="项目">
    <header><span>项目</span><button title="刷新项目" aria-label="刷新项目列表" @click="load"><RefreshCw :size="12" /></button><button title="添加项目" aria-label="添加项目" :aria-expanded="adding" @click="adding = !adding"><Plus :size="14" /></button></header>
    <form v-if="adding" @submit.prevent="add"><input v-model="folder" aria-label="项目文件夹路径" placeholder="文件夹绝对路径" /><button :disabled="creating || !folder.trim()" aria-label="保存项目" title="保存项目"><Plus :size="14" /></button></form>
    <div class="sidebar-project-scroll">
      <div v-for="project in projects" :key="project.id" class="sidebar-project">
        <div class="sidebar-project-row"><button class="project-expand" :aria-expanded="expanded === project.id" :title="project.root_path" @click="toggle(project.id)"><ChevronDown :size="12" :class="{ folded: expanded !== project.id }" /><FolderOpen :size="14" /><span>{{ project.name }}</span></button><button :disabled="creating || !project.available" :title="`在 ${project.name} 新建会话`" :aria-label="`在 ${project.name} 新建会话`" @click="newChat(project)"><Plus :size="14" /></button></div>
        <div v-if="expanded === project.id" class="project-chats">
          <button v-for="chat in rows" :key="chat.id" :class="{ active: chat.id === props.currentId }" :title="chat.title" @click="router.push({ path: '/chat', query: { conv: chat.id } })"><span>{{ chat.title }}</span><small v-if="chat.unread_count">{{ chat.unread_count }}</small></button>
          <span v-if="busy" role="status" class="project-empty">正在载入…</span><span v-else-if="!rows.length" class="project-empty">暂无会话</span>
          <button v-if="cursor" :disabled="busy" @click="chats(project.id, true)">加载更多</button>
        </div>
      </div>
      <span v-if="!projects.length && !adding && !error" class="project-empty">暂无项目</span>
    </div>
    <div v-if="error" role="alert" class="project-error">{{ error }}<button @click="expanded ? chats(expanded) : load()">重试</button></div>
  </section>
</template>
<style scoped>
.sidebar-projects { display:flex; flex-direction:column; min-height:56px; max-height:100px; padding:4px 3px 8px; border-bottom:1px solid var(--fn-border,#ddd); margin-bottom:8px; flex-shrink:0; }
header,.sidebar-project-row { display:flex; align-items:center; gap:2px; } header { padding:3px 5px 6px; font-size:11px; color:var(--fn-text-secondary,#666); } header>span { flex:1; }
button { display:inline-flex; align-items:center; gap:5px; color:inherit; background:transparent; border:0; border-radius:4px; padding:5px; font:inherit; font-size:12px; cursor:pointer; } button:hover,.active { background:var(--fn-bg-active,#eee); } button:disabled { opacity:.4; cursor:default; } button:focus-visible { outline:2px solid #346ee8; }
.sidebar-project-scroll { max-height:150px; overflow:auto; } .project-expand { flex:1; min-width:0; text-align:left; } .project-expand span,.project-chats button span { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; flex:1; min-width:0; } svg { flex-shrink:0; } .folded { transform:rotate(-90deg); } .project-chats { padding:2px 0 5px 25px; } .project-chats>button { display:flex; width:100%; text-align:left; } .project-chats small { color:#245dcc; } .project-empty { display:block; color:var(--fn-text-secondary,#666); font-size:11px; padding:5px; } .project-error { font-size:11px; color:#b42318; overflow-wrap:anywhere; } form { display:flex; gap:4px; margin:4px 0 8px; } input { width:100%; min-width:0; padding:5px; border:1px solid var(--fn-border,#ddd); border-radius:4px; background:transparent; color:inherit; font:inherit; font-size:11px; }
.sidebar-project-scroll { min-height:0; max-height:none; overflow:auto; }
header,form { flex-shrink:0; }
</style>
