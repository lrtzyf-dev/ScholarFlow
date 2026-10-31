<script setup lang="ts">
import { ref, watch, computed, onUnmounted } from 'vue'
import { ElDialog } from 'element-plus'
import { GitBranch, GitCommitHorizontal, RefreshCw, FileDiff, FileText, Plus, Minus, Check } from 'lucide-vue-next'
import { gitStatus, gitDiff, gitHistory, gitPrepare, gitOperations, type GitEntry, type GitStatus, type GitDiff, type GitCommit, type GitOperation, type GitRequest } from '../projectGitApi'
import { projectError, type Project } from '../projectApi'
import { projectGitState } from '../desktopState'
import GitOperationReview from './GitOperationReview.vue'

const props = defineProps<{ project: Project }>()
const status = ref<GitStatus | null>(null), entries = ref<GitEntry[]>([]), commits = ref<GitCommit[]>([])
const view = ref<'changes' | 'history' | 'operations'>('changes')
const operations = ref<GitOperation[]>([]), operationNext = ref<number | null>(null)
const review = ref<GitOperation | null>(null), preparing = ref(false), mutationError = ref('')
const draft = computed(() => projectGitState(props.project.id))
const message = computed({ get: () => draft.value.value.value.message, set: message => draft.value.update({ ...draft.value.value.value, message }) })
const draftStatus = computed(() => draft.value.status.value)
const pending = computed(() => draft.value.value.value.pending)
const mutationDisabled = computed(() => preparing.value || !!pending.value || loading.value || draftStatus.value !== 'saved')
const actions = { stage: '暂存', unstage: '取消暂存', commit: '提交' }
const operationStatuses = { prepared: '待确认', running: '执行中', applied: '已完成', failed: '失败', interrupted: '结果未确认', conflict: '内容已变化', cancelled: '已取消' }
const loading = ref(false), error = ref(''), diffLoading = ref(false), diffError = ref('')
const selected = ref<{ path: string; version: GitDiff['version'] } | null>(null)
const detail = ref<GitDiff | null>(null)
const diffLines = computed(() => (detail.value?.patch || '').split('\n').map(line => ({
  line, kind: line.startsWith('+++') || line.startsWith('---') ? 'header' : line.startsWith('+') ? 'add'
    : line.startsWith('-') ? 'remove' : line.startsWith('@@') ? 'hunk' : 'context',
})))
let epoch = 0, diffEpoch = 0

async function refresh(offset = 0) {
  const request = ++epoch, projectId = props.project.id
  loading.value = true; error.value = ''
  try {
    const result = await gitStatus(projectId, offset, offset ? status.value?.snapshot : undefined)
    if (request !== epoch) return
    status.value = result
    entries.value = offset ? [...entries.value, ...result.entries] : result.entries
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
async function showHistory() {
  const request = ++epoch, projectId = props.project.id
  loading.value = true; error.value = ''
  try { const result = await gitHistory(projectId); if (request === epoch) commits.value = result.commits }
  catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
async function showOperations(offset = 0) {
  const request = ++epoch, projectId = props.project.id
  loading.value = true; error.value = ''
  try {
    const result = await gitOperations(projectId, offset)
    if (request === epoch) { operations.value = offset ? [...operations.value, ...result.operations] : result.operations; operationNext.value = result.next_offset }
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
function refreshView() { return view.value === 'history' ? showHistory() : view.value === 'operations' ? showOperations() : refresh() }
async function prepareOperation(action?: GitRequest['action'], paths: string[] | null = null) {
  if (preparing.value) return
  const projectId = props.project.id, doc = draft.value
  preparing.value = true; mutationError.value = ''
  try {
    await doc.ready
    if (action) doc.update({ ...doc.value.value, pending: { request_id: crypto.randomUUID(), action, paths, message: action === 'commit' ? message.value : '' } })
    await doc.flush()
    if (doc.status.value !== 'saved' || !doc.value.value.pending) throw new Error('请先解决本机草稿保存问题')
    const result = await gitPrepare(projectId, doc.value.value.pending)
    doc.update({ ...doc.value.value, pending: null }); await doc.flush()
    if (props.project.id === projectId) review.value = result
  } catch (e) { if (props.project.id === projectId) mutationError.value = projectError(e) }
  finally { if (props.project.id === projectId) preparing.value = false }
}
function operationChanged(row: GitOperation) {
  const wasApplied = review.value?.status === 'applied'
  review.value = row
  if (row.status === 'applied' && !wasApplied) {
    if (row.action === 'commit' && message.value === row.message) draft.value.update({ ...draft.value.value.value, message: '' })
    commits.value = []
    void refreshView()
  }
}
async function showDiff(path: string, version: GitDiff['version'], offset = 0) {
  const request = ++diffEpoch, projectId = props.project.id
  if (!offset) { selected.value = { path, version }; detail.value = null }
  diffError.value = ''; diffLoading.value = true
  try {
    const result = await gitDiff(projectId, path, version, offset, offset ? detail.value?.snapshot : undefined)
    if (request === diffEpoch) {
      if (offset && detail.value) {
        result.patch = result.patch === null ? null : (detail.value.patch || '') + result.patch
        result.preview = result.preview === null ? null : (detail.value.preview || '') + result.preview
      }
      detail.value = result
    }
  }
  catch (e) { if (request === diffEpoch) diffError.value = projectError(e) }
  finally { if (request === diffEpoch) diffLoading.value = false }
}
watch(() => props.project.id, () => { epoch++; diffEpoch++; selected.value = null; review.value = null; status.value = null; entries.value = []; commits.value = []; operations.value = []; preparing.value = false; mutationError.value = ''; view.value = 'changes'; void refresh() }, { immediate: true })
watch(view, () => { void refreshView() })
onUnmounted(() => { epoch++; diffEpoch++ })
function closeDiff() { selected.value = null; detail.value = null; diffEpoch++ }
</script>

<template>
  <div class="git-panel" @keydown="event => { if (event.ctrlKey || event.metaKey) event.stopPropagation() }">
    <header class="git-head">
      <div class="git-identity"><GitBranch :size="15" /><strong>{{ status?.available ? (status.branch || `detached ${status.head || ''}`) : '版本控制' }}</strong><span v-if="status?.linked_worktree">工作树</span></div>
      <button type="button" title="刷新 Git 状态" aria-label="刷新 Git 状态" :disabled="loading" @click="refreshView()"><RefreshCw :size="15" /></button>
    </header>
    <div v-if="status?.available" class="git-tabs" role="tablist" aria-label="Git 视图">
      <button role="tab" :aria-selected="view === 'changes'" @click="view = 'changes'">更改 <span>{{ status.total }}</span></button>
      <button role="tab" :aria-selected="view === 'history'" @click="view = 'history'">提交记录</button>
      <button role="tab" :aria-selected="view === 'operations'" @click="view = 'operations'">操作记录</button>
    </div>
    <p v-if="mutationError" role="alert" class="git-error">{{ mutationError }}</p>
    <div v-if="pending" class="git-recovery"><span>有未确认的{{ actions[pending.action] }}请求</span><button :disabled="preparing" @click="prepareOperation()">恢复审阅</button><button :disabled="preparing" @click="draft.update({ ...draft.value.value, pending: null })">清除请求</button></div>
    <div v-if="draftStatus === 'error' || draftStatus === 'conflict'" class="git-recovery" role="alert"><span>{{ draftStatus === 'conflict' ? '另一窗口已修改 Git 草稿' : 'Git 草稿保存失败' }}</span><template v-if="draftStatus === 'conflict'"><button @click="draft.resolveConflict(false)">载入已保存版本</button><button @click="draft.resolveConflict(true)">保留本窗口版本</button></template><button v-else @click="draft.retry()">重试保存</button></div>
    <p v-if="error" role="alert" class="git-error">{{ error }}</p>
    <p v-else-if="status && !status.available" class="git-empty">{{ status.reason }}</p>
    <div v-else-if="view === 'changes'" class="git-changes" :aria-busy="loading">
      <div v-if="status?.available" class="git-write-tools"><span>更改</span><button title="暂存全部更改" aria-label="暂存全部更改" :disabled="mutationDisabled || !status.unstaged_total" @click="prepareOperation('stage')"><Plus :size="14" /></button><button title="取消全部暂存" aria-label="取消全部暂存" :disabled="mutationDisabled || !status.staged_total" @click="prepareOperation('unstage')"><Minus :size="14" /></button></div>
      <div class="git-list">
      <div v-for="entry in entries" :key="entry.path" class="git-row">
        <FileText :size="14" /><span class="git-path" :title="entry.path"><strong>{{ entry.path }}</strong><small v-if="entry.original_path">{{ entry.original_path }} →</small></span>
        <span v-if="entry.conflict" class="git-conflict">冲突</span>
        <button v-if="entry.staged" @click="showDiff(entry.path, 'staged')">已暂存</button>
        <button v-if="entry.unstaged" @click="showDiff(entry.path, 'unstaged')">未暂存</button>
        <button v-if="entry.untracked" @click="showDiff(entry.path, 'untracked')">未跟踪</button>
        <button v-if="entry.unstaged || entry.untracked" :aria-label="`暂存 ${entry.path}`" title="暂存文件" :disabled="mutationDisabled || entry.conflict" @click="prepareOperation('stage', [entry.path])"><Plus :size="14" /></button>
        <button v-if="entry.staged" :aria-label="`取消暂存 ${entry.path}`" title="取消暂存文件" :disabled="mutationDisabled || entry.conflict" @click="prepareOperation('unstage', [entry.path])"><Minus :size="14" /></button>
      </div>
      <p v-if="status?.available && !entries.length && !loading" class="git-empty">工作区没有更改</p>
      <button v-if="status && status.next_offset !== null" class="load-more" :disabled="loading" @click="refresh(status.next_offset!)">加载更多</button>
      </div>
      <div v-if="status?.available" class="git-commit-form"><label for="git-commit-message">提交说明</label><textarea id="git-commit-message" v-model="message" maxlength="10000" rows="2" :disabled="draftStatus === 'loading' || preparing" /><div><span>{{ draftStatus === 'saved' ? '已保存到本机' : draftStatus === 'saving' || draftStatus === 'pending' ? '正在保存…' : '' }}</span><button :disabled="mutationDisabled || !message.trim() || !status.staged_total" @click="prepareOperation('commit')"><Check :size="14" />审阅提交</button></div></div>
    </div>
    <div v-else-if="view === 'history'" class="git-list" :aria-busy="loading">
      <div v-for="commit in commits" :key="commit.id" class="commit-row"><GitCommitHorizontal :size="14" /><span><strong>{{ commit.subject }}</strong><small>{{ commit.short_id }} · {{ new Date(commit.date).toLocaleString('zh-CN') }}</small></span></div>
      <p v-if="!commits.length && !loading" class="git-empty">暂无提交记录</p>
    </div>
    <div v-else class="git-list" :aria-busy="loading"><button v-for="operation in operations" :key="operation.id" class="operation-row" @click="review = operation"><span><strong>{{ actions[operation.action] }} · {{ operation.paths.length }} 个文件</strong><small>{{ operation.message || operation.paths.join(', ') }}</small></span><span>{{ operationStatuses[operation.status] }}</span></button><p v-if="!operations.length && !loading" class="git-empty">暂无 Git 操作</p><button v-if="operationNext !== null" class="load-more" :disabled="loading" @click="showOperations(operationNext!)">加载更多操作</button></div>
  </div>
  <GitOperationReview v-if="review" :key="review.id" :operation="review" @close="review = null" @changed="operationChanged" />
  <ElDialog :model-value="!!selected" title="Git 差异" width="940px" top="5vh" append-to-body :close-on-click-modal="false" @update:model-value="value => { if (!value) closeDiff() }">
    <div class="git-diff">
      <header><FileDiff :size="16" /><strong>{{ selected?.path }}</strong><span>{{ selected?.version === 'staged' ? '已暂存' : selected?.version === 'unstaged' ? '未暂存' : '未跟踪' }}</span><button v-if="selected" aria-label="刷新 Git 差异" title="刷新 Git 差异" :disabled="diffLoading" @click="showDiff(selected.path, selected.version)"><RefreshCw :size="14" /></button></header>
      <p v-if="diffError" role="alert" class="git-error">{{ diffError }}</p>
      <div class="git-patch" tabindex="0" aria-label="Git 文件差异" :aria-busy="diffLoading">
        <p v-if="diffLoading" class="git-empty">正在读取差异…</p>
        <template v-else-if="detail?.patch !== null && detail"><div v-for="(line, index) in diffLines" :key="index" class="patch-line" :class="line.kind"><code>{{ line.line }}</code></div><p v-if="!detail.patch" class="git-empty">当前范围没有文本差异</p></template>
        <pre v-else-if="detail?.preview !== null && detail">{{ detail.preview }}</pre>
      </div>
      <p v-if="detail?.binary" class="git-hint">此文件包含二进制差异</p>
      <button v-if="detail && detail.next_offset !== null" :disabled="diffLoading" @click="showDiff(detail.path, detail.version, detail.next_offset!)">加载更多差异</button>
    </div>
    <template #footer><button class="git-close" @click="closeDiff">关闭</button></template>
  </ElDialog>
</template>

<style scoped>
.git-panel { display:flex; flex-direction:column; min-height:0; flex:1; color:var(--fn-text-primary,#202124); }
.git-changes { display:flex; flex-direction:column; flex:1; min-height:0; } .git-changes>.git-list { flex:1; }
.git-write-tools { display:flex; align-items:center; gap:6px; padding:7px 0; font-size:12px; } .git-write-tools>span { flex:1; color:var(--fn-text-secondary); }
.git-recovery { display:flex; align-items:center; flex-wrap:wrap; gap:6px; font-size:12px; padding:8px 0; color:#a13d18; }
.git-commit-form { flex:none; border-top:1px solid var(--fn-border,#ddd); padding-top:8px; margin-top:8px; } .git-commit-form label { display:block; font-size:12px; margin-bottom:5px; } .git-commit-form textarea { box-sizing:border-box; width:100%; min-height:52px; max-height:100px; resize:vertical; border:1px solid var(--fn-border,#ddd); border-radius:4px; padding:7px; color:inherit; background:var(--fn-bg-primary,#fff); font-family:inherit; font-size:12px; line-height:1.5; } .git-commit-form>div { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-top:5px; } .git-commit-form>div>span { color:var(--fn-text-secondary); font-size:11px; }
.operation-row { display:flex; width:100%; text-align:left; border:0; border-bottom:1px solid var(--fn-border,#ddd); border-radius:0; padding:9px 2px; } .operation-row>span:first-child { flex:1; min-width:0; } .operation-row strong,.operation-row small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:400; } .operation-row small { margin-top:4px; color:var(--fn-text-secondary); font-size:11px; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; flex:none; padding:6px 8px; border:1px solid var(--fn-border,#ddd); border-radius:4px; color:inherit; background:transparent; font:inherit; font-size:12px; cursor:pointer; }
button:hover { background:var(--fn-bg-hover,#f2f3f4); } button:disabled { opacity:.45; cursor:default; } button:focus-visible { outline:2px solid #346ee8; outline-offset:2px; }
.git-head { display:flex; align-items:center; gap:8px; padding:10px 0 7px; } .git-head>button { border:0; } .git-identity { flex:1; display:flex; align-items:center; gap:7px; min-width:0; font-size:12px; } .git-identity strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .git-identity span { font-size:11px; color:var(--fn-text-secondary); }
.git-tabs { display:flex; gap:16px; border-bottom:1px solid var(--fn-border,#ddd); } .git-tabs button { border:0; border-radius:0; border-bottom:2px solid transparent; padding:8px 2px; } .git-tabs button[aria-selected=true] { border-bottom-color:#346ee8; color:#245dcc; } .git-tabs span { color:var(--fn-text-secondary); }
.git-list { min-height:0; overflow:auto; } .git-row,.commit-row { display:flex; align-items:center; gap:7px; min-height:36px; padding:5px 2px; border-bottom:1px solid var(--fn-border,#ddd); font-size:12px; } .git-row svg,.commit-row svg { flex:none; color:var(--fn-text-secondary); } .git-path,.commit-row>span { flex:1; min-width:0; } .git-path strong,.commit-row strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:500; } .git-path small,.commit-row small { display:block; color:var(--fn-text-secondary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px; } .git-row button { font-size:11px; padding:4px 6px; } .git-conflict { color:#b42318; font-size:11px; } .git-empty { font-size:12px; color:var(--fn-text-secondary,#666); text-align:center; margin:20px 8px; } .git-error { color:#b42318; font-size:12px; overflow-wrap:anywhere; } .load-more { margin:8px; }
.git-diff { color:var(--fn-text-primary,#202124); } .git-diff header { display:flex; align-items:center; gap:8px; font-size:13px; margin-bottom:12px; } .git-diff header strong { flex:1; min-width:0; overflow-wrap:anywhere; } .git-diff header span { color:var(--fn-text-secondary); font-size:12px; } .git-patch { height:min(52vh,450px); overflow:auto; border:1px solid var(--fn-border,#ddd); border-radius:5px; } .patch-line { min-width:max-content; padding:0 10px; font:12px/20px Consolas,monospace; white-space:pre; } .patch-line.add { color:#155b31; background:#e8f6ec; } .patch-line.remove { color:#9a2525; background:#fbe9e8; } .patch-line.hunk,.patch-line.header { color:var(--fn-text-secondary); background:var(--fn-bg-secondary); } .git-patch pre { margin:0; padding:10px; white-space:pre; font:12px/1.65 Consolas,monospace; } .git-hint { font-size:12px; color:var(--fn-text-secondary); } .git-close { margin-left:auto; }
</style>
