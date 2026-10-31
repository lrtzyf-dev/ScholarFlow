<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { Archive, Download, GitBranch, GitFork, Plus, RefreshCw, RotateCcw, Trash2 } from 'lucide-vue-next'
import { listProjects, projectError, type Project } from '../projectApi'
import { conversationState, projectWorktreeState } from '../desktopState'
import { listWorkspaceSwitches, prepareWorkspaceSwitch, type WorkspaceSwitch } from '../workspaceSwitchApi'
import WorkspaceSwitchReview from './WorkspaceSwitchReview.vue'
import { listWorkspaceTransfers, prepareWorkspaceTransfer, transferStatus, type WorkspaceTransfer, type WorkspaceTransferMode } from '../workspaceTransferApi'
import WorkspaceTransferReview from './WorkspaceTransferReview.vue'
import { listWorktrees, createWorktree, reviewWorktreeRemoval, reviewWorktreeArchive, worktreeBackupUrl, worktreeAction, listWorktreeArchiveHistory, deleteWorktreeArchiveSnapshot, worktreeStatus, type Worktree, type WorktreeListing, type WorktreeArchiveSnapshot } from '../worktreeApi'

const props = defineProps<{ project: Project; conversationId: string; currentProjectId: string | null; disabled: boolean }>()
const emit = defineEmits<{ newChat: [project: Project]; changed: []; switched: [] }>()
const switchReview = ref<WorkspaceSwitch | null>(null), switches = ref<WorkspaceSwitch[]>([])
const transferReview = ref<WorkspaceTransfer | null>(null), transfers = ref<WorkspaceTransfer[]>([])
const canSwitch = computed(() => !!props.currentProjectId && !!data.value &&
  (props.currentProjectId === data.value.source_project_id || data.value.worktrees.some(row => row.project_id === props.currentProjectId)))
const data = ref<WorktreeListing | null>(null), error = ref(''), loadError = ref(''), notice = ref('')
const loading = ref(false), busy = ref(false), formOpen = ref(false), showRemoved = ref(false)
const review = ref<Worktree | null>(null), reviewError = ref('')
const archiveHistoryFor = ref<Worktree | null>(null), archiveHistory = ref<WorktreeArchiveSnapshot[]>([]), archiveHistoryLoading = ref(false), archiveHistoryError = ref(''), archiveHistoryBusy = ref('')
const doc = computed(() => projectWorktreeState(props.project.id))
const draft = computed(() => doc.value.value.value), draftStatus = computed(() => doc.value.status.value)
const rows = computed(() => (data.value?.worktrees || []).filter(row => showRemoved.value ? row.status === 'removed' : row.status !== 'removed'))
const unavailable = computed(() => !data.value || !!data.value.error)
let epoch = 0, alive = true, timer: ReturnType<typeof setTimeout> | undefined
function archiveBytes(value = 0) { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1024 / 1024).toFixed(2)} MiB` }
function edit(field: 'name' | 'ref' | 'branch', value: string) { doc.value.update({ ...draft.value, [field]: value }) }
function changed() { emit('changed'); window.dispatchEvent(new Event('scholarflow:projects-updated')) }
async function load() {
  clearTimeout(timer)
  const request = ++epoch, project = props.project.id, cid = props.conversationId
  loading.value = true
  try {
    const result = await listWorktrees(project)
    if (!alive || request !== epoch) return
    data.value = result; loadError.value = ''
    if (canSwitch.value) {
      const history = await listWorkspaceSwitches(cid)
      if (alive && request === epoch) switches.value = history.switches
      const moving = await listWorkspaceTransfers(cid)
      if (alive && request === epoch) transfers.value = moving.transfers
    } else { switches.value = []; transfers.value = [] }
  } catch (e) { if (alive && request === epoch) loadError.value = projectError(e) }
  finally {
    if (alive && request === epoch) {
      loading.value = false
      timer = setTimeout(() => void load(), data.value?.worktrees.some(r => ['creating', 'removing', 'archiving', 'restoring'].includes(r.status)) ? 2000 : 5000)
    }
  }
}
async function prepareSwitch(target: string | null, transfer: false | WorkspaceTransferMode = false) {
  if (!target || busy.value || props.disabled || !canSwitch.value) return
  busy.value = true; error.value = ''
  const cid = props.conversationId, document = conversationState(cid)
  try {
    await document.ready; await document.flush()
    if (document.status.value !== 'saved') throw Error('请先处理聊天或编辑草稿的保存问题')
    const request = { request_id: crypto.randomUUID(), target_project_id: target,
      workspace_revision: document.value.value.workspace_revision || 0, draft_revision: document.savedRevision }
    if (transfer) {
      const result = await prepareWorkspaceTransfer(cid, { ...request, mode: transfer })
      if (alive && cid === props.conversationId) transferReview.value = result
    } else {
      const result = await prepareWorkspaceSwitch(cid, request)
      if (alive && cid === props.conversationId) switchReview.value = result
    }
  } catch (e) { if (alive) error.value = projectError(e) }
  finally { if (alive) { busy.value = false; void load() } }
}
async function switched() {
  await conversationState(props.conversationId).refresh()
  if (!alive) return
  emit('switched'); changed(); await load()
}
async function create(retry = false) {
  if (busy.value) return
  const project = props.project.id, document = doc.value
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await document.ready
    if (!retry) document.update({ ...document.value.value, pending: { request_id: crypto.randomUUID(),
      name: draft.value.name.trim(), ref: draft.value.ref.trim() || 'HEAD', branch: draft.value.branch.trim() } })
    await document.flush()
    const request = document.value.value.pending
    if (document.status.value !== 'saved' || !request) throw new Error('请先解决工作树草稿的保存问题')
    const result = await createWorktree(project, request)
    if (document.value.value.pending?.request_id === request.request_id) {
      document.update({ ...document.value.value, pending: null }); await document.flush()
    }
    if (!alive || project !== props.project.id) return
    formOpen.value = false; showRemoved.value = false
    notice.value = result.status === 'ready' ? '工作树已创建，可在此开始独立会话。' : '操作结果需要核对，请查看下方工作树记录。'
    changed(); await load()
  } catch (e) { if (alive && project === props.project.id) error.value = projectError(e) }
  finally { if (alive && project === props.project.id) busy.value = false }
}
async function openProject(id: string | null) {
  if (!id || busy.value) return
  busy.value = true; error.value = ''
  try {
    const project = (await listProjects()).find(p => p.id === id)
    if (!project?.available) throw new Error(project?.error || '项目目录不可用，请刷新工作树')
    if (alive) emit('newChat', project)
  } catch (e) { if (alive) error.value = projectError(e) }
  finally { if (alive) busy.value = false }
}
async function prepareRemove(row: Worktree, archive = false) {
  if (busy.value) return
  busy.value = true; error.value = ''; reviewError.value = ''
  try {
    const result = await (archive ? reviewWorktreeArchive : reviewWorktreeRemoval)(props.project.id, row.id)
    if (alive) review.value = result
  } catch (e) { if (alive) error.value = projectError(e) }
  finally { if (alive) busy.value = false }
}
async function act(row: Worktree, action: 'remove' | 'archive' | 'restore' | 'reconcile') {
  if (busy.value) return
  busy.value = true; error.value = ''; reviewError.value = ''; notice.value = ''
  try {
    const result = await worktreeAction(props.project.id, row, action)
    if (!alive) return
    review.value = null
    if (result.status === 'interrupted') error.value = result.error
    else notice.value = action === 'archive' ? '工作树已归档。本地修改、原暂存版本和本机文件备份均已保存。' : action === 'remove' ? '工作目录已移除，提交和会话记录保留，可随时恢复。' : '工作树记录已恢复。旧的中断任务请核对后新建执行。'
    showRemoved.value = result.status === 'removed'; changed(); await load()
  } catch (e) {
    if (!alive) return
    if (action === 'remove' || action === 'archive') reviewError.value = projectError(e)
    else error.value = projectError(e)
    await load()
  } finally { if (alive) busy.value = false }
}
async function openArchiveHistory(row: Worktree) {
  if (busy.value || archiveHistoryLoading.value) return
  archiveHistoryFor.value = row; archiveHistory.value = []; archiveHistoryError.value = ''; archiveHistoryLoading.value = true
  try {
    const result = await listWorktreeArchiveHistory(props.project.id, row.id)
    if (archiveHistoryFor.value?.id === row.id) archiveHistory.value = result.snapshots
  } catch (e) { archiveHistoryError.value = projectError(e) }
  finally { archiveHistoryLoading.value = false }
}
async function removeArchiveSnapshot(snapshot: WorktreeArchiveSnapshot) {
  const row = archiveHistoryFor.value
  if (!row || snapshot.current || archiveHistoryBusy.value) return
  archiveHistoryBusy.value = snapshot.id; archiveHistoryError.value = ''
  try {
    await deleteWorktreeArchiveSnapshot(props.project.id, row.id, snapshot.id)
    archiveHistory.value = archiveHistory.value.filter(item => item.id !== snapshot.id)
    notice.value = '旧归档快照已清理。'
  } catch (e) { archiveHistoryError.value = projectError(e) }
  finally { archiveHistoryBusy.value = '' }
}
watch(() => [props.project.id, props.conversationId], () => {
  data.value = null; error.value = ''; notice.value = ''
  switchReview.value = null
  if (!transferReview.value || !['applied', 'rolled_back'].includes(transferReview.value.status)) transferReview.value = null
  switches.value = []; transfers.value = []
  void load()
}, { immediate: true })
onUnmounted(() => { alive = false; epoch++; clearTimeout(timer) })
</script>

<template>
  <section class="worktrees" aria-label="项目工作树">
    <header class="worktree-heading"><div><GitFork :size="16" /><strong>工作树</strong></div><div><button :disabled="loading" aria-label="刷新工作树" title="刷新工作树" @click="load"><RefreshCw :size="14" /></button><button :disabled="busy || unavailable || !!draft.pending" @click="formOpen = !formOpen"><Plus :size="14" />创建工作树</button></div></header>
    <p v-if="!formOpen" class="worktree-intro">在独立目录中并行开发，共享 Git 提交记录，各自保留文件和暂存区。</p>
    <div v-if="data && !formOpen" class="source-workspace"><span :title="data.source_path">来源：{{ data.source_name }}</span><div class="worktree-actions"><button v-if="canSwitch && currentProjectId !== data.source_project_id" :disabled="busy || disabled" @click="prepareSwitch(data.source_project_id)">返回来源目录</button><button v-if="canSwitch && currentProjectId !== data.source_project_id" :disabled="busy || disabled" @click="prepareSwitch(data.source_project_id, 'changes')">携带改动返回来源</button><button v-if="canSwitch && currentProjectId !== data.source_project_id" :disabled="busy || disabled" @click="prepareSwitch(data.source_project_id, 'git_state')">携带分支与提交返回来源</button><button :disabled="busy" @click="openProject(data.source_project_id)">在来源项目新建会话</button></div></div>
    <div v-if="!formOpen && switches.some(row => row.status === 'prepared')" class="worktree-recovery"><span>有待确认的目录切换</span><button v-for="row in switches.filter(row => row.status === 'prepared')" :key="row.id" @click="switchReview = row">继续审阅：{{ row.snapshot.target.name }}</button></div>
    <div v-if="!formOpen && transfers.some(row => ['prepared', 'applying', 'interrupted', 'rolling_back'].includes(row.status))" class="worktree-recovery"><span>有待处理的 Git 状态迁移</span><button v-for="row in transfers.filter(row => ['prepared', 'applying', 'interrupted', 'rolling_back'].includes(row.status))" :key="row.id" @click="transferReview = row">查看迁移：{{ row.review.target.name }} · {{ row.review.mode === 'git_state' ? '分支与提交' : '工作区改动' }} · {{ transferStatus[row.status] }}</button></div>
    <p v-if="error || loadError || data?.error" role="alert" class="worktree-error">{{ error || loadError || data?.error }}</p>
    <p v-if="notice" role="status" class="worktree-notice">{{ notice }}</p>
    <div v-if="draftStatus === 'error' || draftStatus === 'conflict'" class="worktree-recovery" role="alert"><span>{{ draftStatus === 'conflict' ? '另一窗口已修改工作树草稿' : '工作树草稿保存失败' }}</span><template v-if="draftStatus === 'conflict'"><button @click="doc.resolveConflict(false)">载入已保存版本</button><button @click="doc.resolveConflict(true)">保留本窗口版本</button></template><button v-else @click="doc.retry()">重试保存</button></div>
    <div v-if="draft.pending" class="worktree-recovery"><span>上次创建请求尚未确认</span><button :disabled="busy || draftStatus !== 'saved'" @click="create(true)">查询或继续创建</button><button :disabled="busy" @click="doc.update({ ...draft, pending: null })">清除请求</button><small>清除请求保留已有目录和操作记录。</small></div>
    <form v-if="formOpen" class="worktree-form" @submit.prevent="create()">
      <label>工作树名称<input :value="draft.name" maxlength="120" :disabled="busy || !!draft.pending" placeholder="例如：修复导出流程" @input="edit('name', ($event.target as HTMLInputElement).value)" /></label>
      <div class="worktree-fields"><label>起始提交或本地引用<input :value="draft.ref" maxlength="256" :disabled="busy || !!draft.pending" placeholder="HEAD" @input="edit('ref', ($event.target as HTMLInputElement).value)" /></label><label>新分支（可选）<input :value="draft.branch" maxlength="256" :disabled="busy || !!draft.pending" placeholder="留空使用分离 HEAD" @input="edit('branch', ($event.target as HTMLInputElement).value)" /></label></div>
      <p>从所选提交创建，不复制来源目录的未提交文件。不填写分支时使用分离 HEAD，仍可提交；移除后保留提交以供恢复。</p>
      <div class="worktree-form-actions"><span>{{ draftStatus === 'saved' ? '草稿已保存到本机' : '正在保存…' }}</span><button type="button" :disabled="busy" @click="formOpen = false">取消</button><button class="primary" :disabled="busy || draftStatus !== 'saved' || !draft.name.trim() || !!draft.pending">{{ busy ? '创建中…' : '创建独立目录' }}</button></div>
    </form>
    <div v-if="!formOpen" class="worktree-filters" role="group" aria-label="工作树状态筛选"><button :aria-pressed="!showRemoved" @click="showRemoved = false">工作目录</button><button :aria-pressed="showRemoved" @click="showRemoved = true">已移除</button></div>
    <div v-if="!formOpen" class="worktree-list" :aria-busy="loading">
      <article v-for="row in rows" :key="row.id" class="worktree-row" :data-worktree-id="row.id">
        <header><GitBranch :size="15" /><strong>{{ row.name }}</strong><span>{{ row.status === 'ready' && !row.available ? '目录不可用' : row.status === 'removed' && row.archive_info ? '已归档' : worktreeStatus[row.status] }}</span></header>
        <p class="worktree-path" :title="row.path">{{ row.path }}</p>
        <p class="worktree-meta">{{ (row.current_branch ?? row.branch) || '分离 HEAD' }} · {{ (row.current_head || row.saved_head || row.base_head).slice(0, 10) }}<span v-if="row.project_id === currentProjectId"> · 当前会话项目</span></p>
        <p v-if="row.error" class="worktree-error">{{ row.error }}</p>
        <p v-if="row.archive_info" class="worktree-meta">快照 {{ row.archive_info.head.slice(0, 10) }} · 原暂存版本 {{ row.archive_info.index_head.slice(0, 10) }} · {{ row.archive_info.files }} 个文件</p>
        <details v-if="row.output"><summary>Git 输出</summary><pre>{{ row.output }}</pre></details>
        <div class="worktree-actions"><template v-if="row.status === 'ready'"><button v-if="canSwitch && row.project_id !== currentProjectId" :disabled="busy || disabled || !row.available" @click="prepareSwitch(row.project_id)">切换当前会话到此目录</button><button v-if="canSwitch && row.project_id !== currentProjectId" :disabled="busy || disabled || !row.available" @click="prepareSwitch(row.project_id, 'changes')">携带 Git 改动迁移</button><button v-if="canSwitch && row.project_id !== currentProjectId" :disabled="busy || disabled || !row.available" @click="prepareSwitch(row.project_id, 'git_state')">携带分支与提交迁移</button><button :disabled="busy || !row.available" @click="openProject(row.project_id)"><Plus :size="13" />在此新建会话</button><button :disabled="busy || !row.available" @click="prepareRemove(row, true)"><Archive :size="13" />审阅归档</button><button :disabled="busy || !row.available" @click="prepareRemove(row)"><Trash2 :size="13" />审阅移除</button></template><button v-if="row.status === 'removed'" :disabled="busy" @click="act(row, 'restore')"><RotateCcw :size="13" />恢复工作树</button><button v-if="row.status === 'interrupted'" :disabled="busy" @click="act(row, 'reconcile')"><RefreshCw :size="13" />核对并恢复记录</button><a v-if="row.archive_info" :href="worktreeBackupUrl(project.id, row.id)" download="worktree-backup.zip"><Download :size="13" />下载本机备份</a><button v-if="row.archive_info" :disabled="busy" @click="openArchiveHistory(row)">查看快照历史</button></div>
      </article>
      <p v-if="!rows.length && !loading" class="worktree-empty">{{ showRemoved ? '没有已移除的工作树' : '尚未创建工作树' }}</p>
      <details v-if="!showRemoved && data?.external.length" class="external-worktrees"><summary>Git 中的其他工作目录（{{ data.external.length }}）</summary><p v-for="entry in data.external" :key="entry.worktree"><strong>{{ entry.branch?.replace('refs/heads/', '') || '分离 HEAD' }}</strong><span>{{ entry.worktree }}</span><small v-if="entry.locked !== undefined">Git 已锁定</small></p><small>此处只读显示；应用只移除自己创建的工作树。</small></details>
    </div>
    <details v-if="switches.length && !formOpen" class="switch-history"><summary>当前会话的目录切换记录（最近 {{ switches.length }} 条）</summary><button v-for="row in switches" :key="row.id" @click="switchReview = row">{{ row.snapshot.source.name }} → {{ row.snapshot.target.name }} · {{ { prepared: '待确认', applied: '已完成', cancelled: '已取消', superseded: '已过期' }[row.status] }}</button></details>
    <details v-if="transfers.length && !formOpen" class="switch-history"><summary>Git 状态迁移记录（最近 {{ transfers.length }} 条）</summary><button v-for="row in transfers" :key="row.id" @click="transferReview = row">{{ row.review.source.name }} → {{ row.review.target.name }} · {{ row.review.mode === 'git_state' ? '分支与提交' : '工作区改动' }} · {{ transferStatus[row.status] }}</button></details>
  </section>
  <WorkspaceSwitchReview v-model="switchReview" @switched="switched" @changed="load" />
  <WorkspaceTransferReview v-model="transferReview" @switched="switched" @changed="load" />
  <ElDialog :model-value="!!archiveHistoryFor" title="归档快照历史" width="700px" top="8vh" append-to-body :close-on-click-modal="false" @update:model-value="value => { if (!value && !archiveHistoryBusy) archiveHistoryFor = null }">
    <div class="archive-history-dialog">
      <p class="worktree-intro">保留每次归档生成的 Git 快照和本机 ZIP。当前恢复来源不能删除；损坏或缺失的 ZIP 会明确标记。</p>
      <p v-if="archiveHistoryLoading">正在读取快照历史…</p>
      <p v-else-if="archiveHistoryError" class="worktree-error" role="alert">{{ archiveHistoryError }} <button @click="archiveHistoryFor && openArchiveHistory(archiveHistoryFor)">重试</button></p>
      <div v-else-if="!archiveHistory.length" class="worktree-empty">暂无归档快照</div>
      <div v-else class="archive-history-list">
        <article v-for="snapshot in archiveHistory" :key="snapshot.id" class="archive-history-row">
          <div><strong>{{ snapshot.current ? '当前恢复来源' : '历史快照' }}</strong><span>{{ snapshot.created_at || '时间未知' }}</span><code>{{ snapshot.id }}</code></div>
          <p>{{ snapshot.files ?? '?' }} 个文件 · {{ snapshot.bytes == null ? '大小未知' : archiveBytes(snapshot.bytes) }} · {{ snapshot.backup_valid ? 'ZIP 校验通过' : snapshot.backup_present ? 'ZIP 损坏' : 'ZIP 缺失' }}</p>
          <button v-if="!snapshot.current" :disabled="!!archiveHistoryBusy || !snapshot.backup_valid" @click="removeArchiveSnapshot(snapshot)">{{ archiveHistoryBusy === snapshot.id ? '清理中…' : '清理此快照' }}</button>
        </article>
      </div>
    </div>
    <template #footer><button @click="archiveHistoryFor = null">关闭</button></template>
  </ElDialog>
  <ElDialog :model-value="!!review" :title="review?.removal_snapshot?.kind === 'archive' ? '归档工作树' : '移除工作树'" width="660px" top="6vh" append-to-body :close-on-click-modal="false" @update:model-value="value => { if (!value && !busy) review = null }">
    <div v-if="review" class="removal-review"><strong>{{ review.name }}</strong><p class="worktree-path">{{ review.path }}</p><template v-if="review.removal_snapshot?.kind === 'archive'"><p>保存 {{ review.removal_snapshot.files }} 个文件（{{ archiveBytes(review.removal_snapshot.bytes) }}），然后移除工作目录。恢复后，本地修改将包含在新的快照提交中；原分支和原暂存版本会保留。</p><p>{{ review.removal_snapshot.ignored }} 个被忽略文件仅放入本机备份，恢复时一并还原，不加入 Git 历史。备份保存在本机数据目录，依赖来源仓库；可下载另存。</p><div class="archive-files" aria-label="归档文件范围"><p v-for="item in review.removal_snapshot.preview" :key="item.path"><span>{{ item.path }}</span><small>{{ item.ignored ? '仅本机备份' : 'Git 快照和本机备份' }}</small></p></div><p v-if="(review.removal_snapshot.files || 0) > 100">这里只显示前 100 个文件，完整范围保存于备份清单。</p><p>确认前重新核对文件、暂存区和运行状态；归档失败保留快照并要求核对，不自动覆盖后来修改的文件。</p></template><template v-else><p>将移除此工作目录。提交 {{ review.removal_snapshot?.head.slice(0, 12) }}、Git 分支和会话记录会保留，可从“已移除”恢复。</p><p>确认前会再次检查文件和运行状态；存在未提交、未跟踪、被忽略的文件或活动任务时将拒绝移除。</p></template><p v-if="reviewError" role="alert" class="worktree-error">{{ reviewError }}</p></div>
    <template #footer><button :disabled="busy" @click="review = null">取消</button><button class="remove-confirm" :disabled="busy || !!reviewError" @click="review && act(review, review.removal_snapshot?.kind === 'archive' ? 'archive' : 'remove')">{{ busy ? '处理中…' : review?.removal_snapshot?.kind === 'archive' ? '保存快照并归档' : '确认移除工作目录' }}</button></template>
  </ElDialog>
</template>

<style scoped>
.worktrees { display:block; flex:1; min-height:0; padding:10px 4px 8px 0; overflow:auto; color:var(--fn-text-primary); }
.switch-history button { display:block; width:100%; text-align:left; margin-top:5px; overflow-wrap:anywhere; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; padding:6px 9px; border:1px solid var(--fn-border-primary); border-radius:4px; background:transparent; color:var(--fn-text-primary); font:inherit; font-size:12px; cursor:pointer; }
button:disabled { opacity:.45; cursor:default; } button:hover:not(:disabled) { background:var(--fn-bg-hover); } button:focus-visible,input:focus-visible,summary:focus-visible { outline:2px solid #346ee8; outline-offset:2px; }
.worktree-heading,.worktree-heading>div,.source-workspace,.worktree-form-actions,.worktree-row header { display:flex; align-items:center; gap:8px; } .worktree-heading { justify-content:space-between; font-size:13px; }
.worktree-intro,.worktree-form p,.removal-review p { margin:8px 0; font-size:12px; line-height:1.65; color:var(--fn-text-secondary); }
.source-workspace { justify-content:space-between; font-size:12px; padding-bottom:8px; } .source-workspace>span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.worktree-error { color:#b42318; font-size:12px; line-height:1.5; overflow-wrap:anywhere; margin:6px 0; } .worktree-notice { margin:8px 0; color:#237a44; font-size:12px; }
.worktree-recovery { display:flex; gap:6px; align-items:center; flex-wrap:wrap; padding:8px; border:1px solid #dec99c; font-size:12px; } .worktree-recovery small { width:100%; color:var(--fn-text-secondary); }
.worktree-form { padding:12px; border:1px solid var(--fn-border-primary); border-radius:5px; margin:6px 0; } .worktree-form label { display:flex; flex-direction:column; gap:5px; min-width:0; font-size:12px; } input { box-sizing:border-box; min-width:0; width:100%; padding:7px 9px; border:1px solid var(--fn-border-input); border-radius:4px; background:var(--fn-bg-primary); color:var(--fn-text-primary); font:inherit; }
.worktree-fields { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:10px; } .worktree-form-actions { justify-content:flex-end; } .worktree-form-actions>span { margin-right:auto; color:var(--fn-text-secondary); font-size:11px; } button.primary { background:#245dcc; color:white; }
.worktree-filters { display:flex; gap:4px; padding:8px 0; border-bottom:1px solid var(--fn-border-primary); } .worktree-filters button { border:0; } .worktree-filters button[aria-pressed=true] { background:var(--fn-bg-hover); color:#245dcc; }
.worktree-list { min-height:80px; } .worktree-row { padding:12px 2px; border-bottom:1px solid var(--fn-border-primary); } .worktree-row header { font-size:12px; } .worktree-row header strong { flex:1; overflow-wrap:anywhere; } .worktree-row header span { flex-shrink:0; color:var(--fn-text-secondary); font-size:11px; }
.worktree-path { font:11px/1.5 Consolas,monospace; overflow-wrap:anywhere; color:var(--fn-text-secondary); margin:7px 0; } .worktree-meta { font-size:11px; color:var(--fn-text-secondary); margin:7px 0; overflow-wrap:anywhere; } .worktree-actions { display:flex; flex-wrap:wrap; gap:6px; }
details { font-size:11px; margin:8px 0; } summary { cursor:pointer; color:var(--fn-text-secondary); } pre { max-height:100px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:11px/1.5 Consolas,monospace; } .external-worktrees p { display:flex; flex-direction:column; gap:4px; overflow-wrap:anywhere; } .worktree-empty { text-align:center; color:var(--fn-text-secondary); margin:24px 0; font-size:12px; }
.worktree-actions a { display:inline-flex; align-items:center; gap:5px; color:var(--fn-brand); font-size:12px; padding:6px 9px; text-decoration:none; border:1px solid var(--fn-border-primary); border-radius:4px; } .archive-files { max-height:160px; overflow:auto; border:1px solid var(--fn-border-primary); padding:6px 10px; } .archive-files p { display:flex; gap:12px; justify-content:space-between; margin:4px 0; } .archive-files span { overflow-wrap:anywhere; min-width:0; } .archive-files small { white-space:nowrap; }
.removal-review { max-height:55vh; overflow:auto; } .remove-confirm { color:#b42318; border-color:#d8a49e; margin-left:8px; }
.archive-history-dialog { max-height:60vh; overflow:auto; } .archive-history-list { display:grid; gap:8px; } .archive-history-row { border:1px solid var(--fn-border-primary); border-radius:5px; padding:10px; } .archive-history-row>div { display:flex; align-items:baseline; gap:9px; flex-wrap:wrap; } .archive-history-row strong { margin-right:auto; } .archive-history-row span,.archive-history-row code,.archive-history-row p { color:var(--fn-text-secondary); font-size:11px; } .archive-history-row code { overflow-wrap:anywhere; } .archive-history-row p { margin:6px 0; }
</style>
