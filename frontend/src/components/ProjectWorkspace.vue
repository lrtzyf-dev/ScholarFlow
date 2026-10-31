<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { ElDialog } from 'element-plus'
import { FolderOpen, FileText, ChevronRight, ArrowLeft, RefreshCw, Plus, X, Check } from 'lucide-vue-next'
import { addProject, listProjects, bindChatProject, listProjectFiles, readProjectFile, initProjectRules, getProjectRules, projectError, type Project, type ProjectEntry, type FilePage } from '../projectApi'
import { listChanges, statusLabel, operationLabel, type FileChange } from '../fileChangeApi'
import { conversationState } from '../desktopState'
import FileChangeReview from './FileChangeReview.vue'
import ProjectFileEditor from './ProjectFileEditor.vue'
import ProjectGit from './ProjectGit.vue'
import ProjectSearch from './ProjectSearch.vue'
import ProjectCommands from './ProjectCommands.vue'
import ProjectWorktrees from './ProjectWorktrees.vue'

const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ conversationId: string; project: Project | null; files: string[]; disabled: boolean }>()
const emit = defineEmits<{ bound: [project: Project]; files: [files: string[]]; newChat: [project: Project]; switched: [] }>()
const projects = ref<Project[]>([]), selected = ref<Project | null>(null)
const pathInput = ref(''), directory = ref(''), query = ref(''), error = ref(''), notice = ref('')
const loading = ref(false), saving = ref(false), reading = ref(false)
const entries = ref<ProjectEntry[]>([]), nextOffset = ref<number | null>(null), limited = ref(false)
const preview = ref<FilePage | null>(null), rules = ref<{ path: string; text: string }[]>([])
const tab = ref<'files' | 'rules' | 'changes' | 'git' | 'search' | 'commands' | 'worktrees'>('files')
const changes = ref<FileChange[]>([]), changeOffset = ref<number | null>(null), changesLoading = ref(false)
const reviewId = ref<string | null>(null), showEditor = ref(false), editPath = ref<string | null>(null)
const editorDraft = computed(() => conversationState(props.conversationId).value.value.editor)
function edit(path: string | null) { editPath.value = path; showEditor.value = true }
async function loadChanges(offset = 0) {
  if (!selected.value) return
  const id = selected.value.id
  changesLoading.value = true; error.value = ''
  try { const result = await listChanges(id, offset); if (id === selected.value?.id) { changes.value = offset ? [...changes.value, ...result.changes] : result.changes; changeOffset.value = result.next_offset } }
  catch (e) { if (id === selected.value?.id) error.value = projectError(e) }
  finally { changesLoading.value = false }
}
let epoch = 0, readEpoch = 0
const isCurrent = computed(() => selected.value?.id === props.project?.id)
const canAttach = computed(() => isCurrent.value && !props.disabled)
const nativePicker = ref(false)
function pickerReady() { nativePicker.value = Boolean((window as any).pywebview?.api?.choose_project_folder) }
function blockShortcuts(event: KeyboardEvent) { if (event.ctrlKey || event.metaKey) event.stopPropagation() }
onMounted(() => { pickerReady(); window.addEventListener('pywebviewready', pickerReady) })
onUnmounted(() => { epoch++; readEpoch++; window.removeEventListener('pywebviewready', pickerReady) })

function clear() { epoch++; readEpoch++; entries.value = []; preview.value = null; rules.value = []; directory.value = ''; query.value = ''; nextOffset.value = null; error.value = ''; notice.value = ''; loading.value = reading.value = false }
async function loadProjects() {
  error.value = ''
  try { projects.value = await listProjects(); if (selected.value) selected.value = projects.value.find(p => p.id === selected.value?.id) || selected.value } catch (e) { error.value = projectError(e) }
}
async function choose(project: Project) {
  clear(); selected.value = project; tab.value = 'files'
  await browse('')
}
async function browse(path = directory.value, offset = 0) {
  if (!selected.value) return
  const id = selected.value.id, request = ++epoch
  loading.value = true; error.value = ''; readEpoch++; reading.value = false; preview.value = null
  try {
    const page = await listProjectFiles(id, path, offset, query.value)
    if (request !== epoch) return
    entries.value = offset ? [...entries.value, ...page.entries] : page.entries
    directory.value = page.path; nextOffset.value = page.next_offset; limited.value = page.limited
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
function enter(path: string) { query.value = ''; void browse(path) }
function back() { enter(directory.value.split('/').slice(0, -1).join('/')) }
async function read(entry: ProjectEntry, offset = 0) {
  if (entry.kind === 'directory') { enter(entry.path); return }
  if (!selected.value) return
  const request = ++readEpoch, id = selected.value.id
  reading.value = true; error.value = ''
  try { const page = await readProjectFile(id, entry.path, offset); if (request === readEpoch) preview.value = page }
  catch (e) { if (request === readEpoch) { preview.value = null; error.value = projectError(e) } }
  finally { if (request === readEpoch) reading.value = false }
}
function toggle(path: string) {
  if (!canAttach.value) return
  if (props.files.includes(path)) emit('files', props.files.filter(f => f !== path))
  else if (props.files.length < 8) emit('files', [...props.files, path])
  else error.value = '每轮最多引用 8 个项目文件'
}
async function add() {
  if (saving.value || !pathInput.value.trim()) return
  saving.value = true; error.value = ''
  try { const project = await addProject(pathInput.value); window.dispatchEvent(new Event('scholarflow:projects-updated')); await loadProjects(); pathInput.value = ''; await choose(project) }
  catch (e) { error.value = projectError(e) }
  finally { saving.value = false }
}
async function pick() {
  try { const path = await (window as any).pywebview.api.choose_project_folder(); if (path) { pathInput.value = path; await add() } }
  catch (e) { error.value = projectError(e) }
}
async function bind() {
  if (!selected.value || saving.value || props.disabled) return
  saving.value = true; error.value = ''
  const cid = props.conversationId
  try { const result = await bindChatProject(cid, selected.value.id); if (cid === props.conversationId) { emit('bound', result.project); notice.value = '已关联当前会话' } }
  catch (e) { error.value = projectError(e) }
  finally { saving.value = false }
}
async function loadRules() {
  if (!selected.value) return
  const request = ++epoch, id = selected.value.id
  loading.value = true; error.value = ''
  try { const result = await getProjectRules(id); if (request === epoch) rules.value = result.rules }
  catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
async function initRules() {
  if (!selected.value || saving.value || !isCurrent.value || props.disabled) return
  saving.value = true; error.value = ''
  const id = selected.value.id
  try {
    const result = await initProjectRules(id)
    if (id === selected.value?.id) { notice.value = result.created ? '已创建 AGENTS.md' : 'AGENTS.md 已存在'; tab.value = 'rules'; await loadRules() }
  } catch (e) { error.value = projectError(e) }
  finally { saving.value = false }
}
watch(visible, async value => {
  if (!value) { clear(); return }
  clear(); selected.value = null
  await loadProjects()
  if (!visible.value) return
  const initial = projects.value.find(p => p.id === props.project?.id) || projects.value[0]
  if (initial) await choose(initial)
})
watch(() => props.conversationId, () => { if (visible.value) visible.value = false })
watch(tab, value => { if (value === 'rules') void loadRules(); if (value === 'changes') void loadChanges() })
</script>

<template>
  <ElDialog v-model="visible" title="项目文件" width="920px" top="6vh" class="project-dialog" :close-on-click-modal="false" destroy-on-close>
    <div class="workspace" @keydown="blockShortcuts">
      <aside class="project-list">
        <div class="section-title"><span>本地项目</span><button aria-label="刷新项目" title="刷新项目" @click="loadProjects"><RefreshCw :size="14" /></button></div>
        <div class="project-items">
          <button v-for="project in projects" :key="project.id" class="project-item" :class="{ selected: project.id === selected?.id }" :title="project.root_path" @click="choose(project)">
            <FolderOpen :size="16" /><span><strong>{{ project.name }}</strong><small>{{ project.root_path }}</small></span><Check v-if="project.id === props.project?.id" :size="13" />
          </button>
          <p v-if="!projects.length" class="empty">暂无项目</p>
        </div>
        <form class="add-project" @submit.prevent="add">
          <label for="project-path">文件夹路径</label>
          <input id="project-path" v-model="pathInput" placeholder="D:\Projects\my-project" :disabled="saving" />
          <div><button v-if="nativePicker" type="button" :disabled="saving" @click="pick"><FolderOpen :size="14" />选择文件夹</button><button :disabled="saving || !pathInput.trim()"><Plus :size="14" />添加</button></div>
        </form>
      </aside>
      <section class="project-main">
        <template v-if="selected">
          <header class="project-heading"><div><strong>{{ selected.name }}</strong><small :title="selected.root_path">{{ selected.root_path }}</small></div>
            <button v-if="!isCurrent && !props.project" :disabled="saving || disabled || !selected.available" @click="bind">关联当前会话</button>
            <button :disabled="saving || !selected.available" @click="emit('newChat', selected)"><Plus :size="14" />新建会话</button>
          </header>
          <div class="project-tabs" role="tablist" aria-label="项目视图"><button role="tab" :aria-selected="tab === 'files'" @click="tab = 'files'">文件</button><button role="tab" :aria-selected="tab === 'search'" @click="tab = 'search'">搜索</button><button role="tab" :aria-selected="tab === 'changes'" @click="tab = 'changes'">变更</button><button role="tab" :aria-selected="tab === 'git'" @click="tab = 'git'">版本控制</button><button role="tab" :aria-selected="tab === 'worktrees'" @click="tab = 'worktrees'">工作树</button><button role="tab" :aria-selected="tab === 'commands'" @click="tab = 'commands'">命令</button><button role="tab" :aria-selected="tab === 'rules'" @click="tab = 'rules'">目录规范</button></div>
          <div v-if="canAttach && tab === 'files'" class="edit-toolbar"><button v-if="editorDraft" @click="edit(null)">继续编辑草稿</button><button v-else @click="edit(null)"><Plus :size="13" />新建文件</button><button v-if="preview && !editorDraft" @click="edit(preview.path)">编辑此文件</button></div>
          <div v-if="tab === 'files'" class="project-files">
            <form class="file-toolbar" @submit.prevent="browse(directory)"><button type="button" title="上一级" aria-label="上一级" :disabled="!directory || loading" @click="back"><ArrowLeft :size="15" /></button><button type="button" @click="enter('')">根目录</button><span :title="directory">{{ directory || '/' }}</span><input v-model="query" aria-label="筛选当前目录" placeholder="筛选文件名" /><button :disabled="loading" title="刷新文件" aria-label="刷新文件"><RefreshCw :size="14" /></button></form>
            <div class="file-content">
              <div class="file-list" :aria-busy="loading">
                <div v-for="entry in entries" :key="entry.path" class="file-row">
                  <input v-if="entry.kind === 'file'" type="checkbox" :aria-label="`引用 ${entry.path}`" :checked="isCurrent && files.includes(entry.path)" :disabled="!canAttach || reading" @change="toggle(entry.path)" />
                  <span v-else class="folder-spacer" />
                  <button :title="entry.path" @click="read(entry)"><component :is="entry.kind === 'directory' ? FolderOpen : FileText" :size="15" /><span>{{ entry.name }}</span><ChevronRight v-if="entry.kind === 'directory'" :size="13" /></button>
                </div>
                <p v-if="loading" role="status" class="empty">正在读取…</p><p v-else-if="!entries.length" class="empty">没有匹配的文件</p>
                <button v-if="nextOffset !== null" :disabled="loading" @click="browse(directory, nextOffset)">加载更多</button>
                <p v-if="limited" class="empty">目录条目超过 10,000，请进入子目录查看</p>
              </div>
              <section class="file-preview" :aria-busy="reading">
                <template v-if="preview"><header><span :title="preview.path">{{ preview.path }}</span><button title="关闭预览" aria-label="关闭预览" @click="preview = null"><X :size="14" /></button></header><pre>{{ preview.text }}</pre><footer><span>{{ preview.offset + 1 }}–{{ preview.offset + preview.text.length }} / {{ preview.total_chars }} 字符</span><button :disabled="preview.offset === 0 || reading" @click="read({ name: '', path: preview.path, kind: 'file' }, Math.max(0, preview.offset - 8000))">上一段</button><button :disabled="preview.next_offset === null || reading" @click="read({ name: '', path: preview.path, kind: 'file' }, preview.next_offset!)">下一段</button><button v-if="canAttach" :disabled="reading" @click="toggle(preview.path)">{{ files.includes(preview.path) ? '取消引用' : '引用文件' }}</button></footer></template>
                <p v-else class="empty">{{ reading ? '正在读取…' : '未选择文件' }}</p>
              </section>
            </div>
          </div>
          <div v-else-if="tab === 'changes'" class="change-history"><button :disabled="changesLoading" @click="loadChanges()"><RefreshCw :size="14" />刷新变更</button><button v-for="change in changes" :key="change.id" class="change-item" @click="reviewId = change.id"><span><strong>{{ change.path }}</strong><small>{{ operationLabel(change.operation) }} · {{ change.summary || '文件修改' }}</small></span><span>{{ statusLabel(change.status) }}</span></button><p v-if="!changes.length && !changesLoading" class="empty">暂无文件变更</p><button v-if="changeOffset !== null" :disabled="changesLoading" @click="loadChanges(changeOffset)">加载更早变更</button></div>
          <ProjectSearch v-else-if="tab === 'search'" :project="selected" :can-attach="canAttach" :files="files" @toggle="toggle" />
          <ProjectGit v-else-if="tab === 'git'" :project="selected" />
          <ProjectWorktrees v-else-if="tab === 'worktrees'" :key="selected.id" :project="selected" :conversation-id="conversationId" :current-project-id="props.project?.id || null" :disabled="disabled" @new-chat="emit('newChat', $event)" @changed="loadProjects" @switched="emit('switched')" />
          <ProjectCommands v-else-if="tab === 'commands'" :project-id="selected.id" />
          <div v-else class="project-rules"><div class="rules-toolbar"><button :disabled="loading" @click="loadRules"><RefreshCw :size="14" />刷新</button><button v-if="isCurrent" :disabled="saving || disabled" @click="initRules"><Plus :size="14" />创建 AGENTS.md</button></div><article v-for="rule in rules" :key="rule.path"><strong>{{ rule.path }}</strong><pre>{{ rule.text }}</pre></article><p v-if="!rules.length && !loading" class="empty">暂无目录规范</p></div>
        </template>
        <p v-else class="empty">未选择项目</p>
      </section>
    </div>
    <div v-if="error" role="alert" class="project-error">{{ error }}</div><div v-if="notice" role="status" class="project-notice">{{ notice }}</div>
    <template #footer><span class="selection-count">本轮引用 {{ files.length }} / 8 个文件</span><button class="done-button" @click="visible = false">完成</button></template>
  </ElDialog>
  <ProjectFileEditor v-if="selected" v-model="showEditor" :project="selected" :conversation-id="conversationId" :path="editPath" @changed="loadChanges()" />
  <FileChangeReview v-model="reviewId" :conversation-id="isCurrent ? conversationId : undefined" @changed="loadChanges()" />
</template>

<style scoped>
.workspace { display:grid; grid-template-columns:220px minmax(0,1fr); height:410px; color:var(--fn-text-primary,#202124); border-top:1px solid var(--fn-border,#ddd); border-bottom:1px solid var(--fn-border,#ddd); }
.edit-toolbar { display:flex; gap:6px; padding:6px 0; }
.change-history { overflow:auto; min-height:0; padding:8px 0; } .change-item { display:flex; justify-content:space-between; width:100%; text-align:left; margin-top:6px; padding:10px; } .change-item>span:first-child { flex:1; min-width:0; } .change-item strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .change-item>span:last-child { flex-shrink:0; font-size:11px; }
button { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--fn-border,#ddd); background:transparent; color:inherit; border-radius:4px; padding:6px 8px; font:inherit; font-size:12px; cursor:pointer; }
button:hover { background:var(--fn-bg-hover,#f2f3f4); } button:disabled { opacity:.45; cursor:default; } button:focus-visible,input:focus-visible { outline:2px solid #346ee8; outline-offset:1px; }
input:not([type=checkbox]) { min-width:0; width:100%; box-sizing:border-box; border:1px solid var(--fn-border,#ddd); border-radius:4px; padding:7px; font:inherit; font-size:12px; color:inherit; background:var(--fn-bg-main,#fff); }
.project-list { display:flex; flex-direction:column; min-height:0; padding:12px 12px 12px 0; border-right:1px solid var(--fn-border,#ddd); }
.section-title { display:flex; justify-content:space-between; align-items:center; font-size:12px; font-weight:600; margin-bottom:8px; } .section-title button { border:0; }
.project-items { flex:1; min-height:0; overflow:auto; } .project-item { display:flex; width:100%; text-align:left; border:0; padding:9px 7px; margin-bottom:3px; } .project-item svg { flex-shrink:0; } .project-item.selected { background:var(--fn-bg-active,#edf2fd); } .project-item span { flex:1; min-width:0; } strong { font-weight:600; } .project-item strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } small { display:block; font-size:11px; color:var(--fn-text-secondary,#6b7280); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:4px; }
.add-project { display:flex; flex-direction:column; gap:7px; padding-top:12px; } .add-project label { font-size:12px; } .add-project>div { display:flex; gap:6px; justify-content:flex-end; }
.project-main { display:flex; flex-direction:column; min-width:0; min-height:0; padding:12px 0 0 14px; } .project-heading { display:flex; align-items:center; gap:7px; min-height:40px; } .project-heading>div { flex:1; min-width:0; } .project-heading>div strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .project-heading>button { flex-shrink:0; }
.project-tabs { display:flex; gap:18px; padding-top:10px; border-bottom:1px solid var(--fn-border,#ddd); } .project-tabs button { border:0; border-radius:0; border-bottom:2px solid transparent; padding:8px 2px; } .project-tabs button[aria-selected=true] { border-bottom-color:#346ee8; color:#245dcc; }
.project-files { display:flex; flex-direction:column; flex:1; min-height:0; } .file-toolbar { display:flex; align-items:center; gap:4px; padding:8px 0; } .file-toolbar>span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; } .file-toolbar input { max-width:155px; } .file-toolbar>button { flex-shrink:0; }
.file-content { display:grid; grid-template-columns:minmax(170px, 40%) minmax(0,1fr); flex:1; min-height:0; } .file-list { min-height:0; overflow:auto; border-right:1px solid var(--fn-border,#ddd); padding-right:8px; } .file-row { display:flex; gap:4px; align-items:center; min-height:31px; } .file-row>input,.folder-spacer { width:14px; height:14px; margin:0 3px; flex-shrink:0; } .file-row button { flex:1; min-width:0; border:0; padding:6px 4px; } .file-row button svg { flex-shrink:0; color:var(--fn-text-secondary,#666); } .file-row button span { flex:1; text-align:left; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.file-preview { min-width:0; min-height:0; display:flex; flex-direction:column; padding-left:10px; } .file-preview header { display:flex; gap:4px; align-items:center; font-size:11px; } .file-preview header>span { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .file-preview header button { border:0; } pre { margin:8px 0; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:11px/1.65 Consolas,monospace; tab-size:4; } .file-preview pre { flex:1; min-height:0; } .file-preview footer { display:flex; flex-wrap:wrap; align-items:center; gap:4px; font-size:10px; padding:6px 0; } .file-preview footer span { flex-basis:100%; color:var(--fn-text-secondary,#666); } .file-preview footer button { padding:4px 6px; font-size:11px; }
.project-rules { min-height:0; overflow:auto; padding-top:10px; } .rules-toolbar { display:flex; gap:6px; } .project-rules article { margin-top:12px; font-size:12px; } .empty { margin:20px 6px; color:var(--fn-text-secondary,#666); font-size:12px; text-align:center; } .project-error { color:#b42318; font-size:12px; padding-top:8px; overflow-wrap:anywhere; } .project-notice { color:#237a44; font-size:12px; padding-top:8px; } .selection-count { color:var(--fn-text-secondary,#666); font-size:12px; margin-right:14px; } .done-button { background:#245dcc; color:#fff; border:0; padding:7px 18px; }
</style>
