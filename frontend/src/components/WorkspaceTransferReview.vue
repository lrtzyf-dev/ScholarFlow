<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { ArrowRight, FolderOpen, Download } from 'lucide-vue-next'
import { decideWorkspaceTransfer, workspaceTransferBackup, transferStatus, type WorkspaceTransfer } from '../workspaceTransferApi'
import { projectError } from '../projectApi'
const review = defineModel<WorkspaceTransfer | null>({ required: true })
const emit = defineEmits<{ switched: []; changed: [] }>()
const busy = ref(false), error = ref(''), rollbackReview = ref(false)
const body = ref<HTMLElement | null>(null)
let alive = true
onUnmounted(() => { alive = false })
const open = computed({ get: () => !!review.value, set: (value: boolean) => {
  if (value || busy.value) return
  const completed = !!review.value && ['applied', 'rolled_back'].includes(review.value.status)
  review.value = null
  if (completed) emit('switched')
} })
watch(() => review.value?.id, () => { error.value = ''; rollbackReview.value = false })
const pending = computed(() => review.value && ['applying', 'interrupted', 'rolling_back'].includes(review.value.status))
const gitState = computed(() => review.value?.review.mode === 'git_state')
watch([() => review.value?.status, rollbackReview, error], async () => {
  await nextTick()
  // In a short desktop window the recovery decision must be visible before the
  // user presses its confirmation button in the fixed footer.
  const feedback = body.value?.querySelectorAll<HTMLElement>('[data-feedback]')
  feedback?.[feedback.length - 1]?.scrollIntoView({ block: 'nearest' })
})
function containTab(event: KeyboardEvent) {
  if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return
  const controls = [...(event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>('button:not(:disabled),a[href],[tabindex]')].filter(el => el.tabIndex >= 0 && el.getClientRects().length)
  const first = controls[0], last = controls[controls.length - 1]
  if (!first || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
    event.preventDefault(); event.stopPropagation(); (event.shiftKey ? last : first)?.focus()
  }
}
async function decide(action: 'apply' | 'rollback' | 'cancel') {
  if (!review.value || busy.value) return
  const row = review.value
  busy.value = true; error.value = ''
  try {
    const result = await decideWorkspaceTransfer(row, action)
    if (!alive || review.value?.id !== row.id) return
    review.value = result; rollbackReview.value = false; emit('changed')
  } catch (e) { if (alive && review.value?.id === row.id) error.value = projectError(e) }
  finally { busy.value = false }
}
</script>

<template>
  <ElDialog v-model="open" :title="gitState ? '迁移 Git 状态并继续会话' : '迁移 Git 改动并继续会话'" width="760px" top="6vh" append-to-body :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @keydown.capture="containTab">
    <div v-if="review" ref="body" class="transfer-review" @keydown="e => { if (e.ctrlKey || e.metaKey) e.stopPropagation() }">
      <p class="intro">{{ gitState ? '把来源的分支、提交、暂存区和工作区状态交给目标目录，核对成功后在目标继续此会话。' : '把来源的暂存、未暂存和未跟踪文件迁移到目标目录，核对成功后在目标继续此会话。' }}</p>
      <div class="locations">
        <section v-for="(scope, index) in [review.review.source, review.review.target]" :key="scope.project_id">
          <small>{{ index ? (gitState ? '目标目录 · 接收来源分支' : '目标目录 · 接收改动') : (gitState ? '来源目录 · 转为分离 HEAD' : '来源目录 · 恢复干净状态') }}</small>
          <h3><FolderOpen :size="16" />{{ scope.name }}</h3>
          <p class="path">{{ scope.root_path }}</p>
          <p>{{ scope.branch || '分离 HEAD' }} · {{ scope.head.slice(0, 12) }}</p>
          <p>已暂存 {{ scope.staged }} · 未暂存 / 未跟踪 {{ scope.unstaged }}</p>
        </section>
        <ArrowRight class="direction" :size="18" />
      </div>
      <div v-if="review.review.blockers.length" role="alert" class="blockers"><strong>当前无法迁移</strong><p v-for="reason in review.review.blockers" :key="reason">{{ reason }}</p></div>
      <div class="changes" :aria-label="gitState ? '待同步文件' : '待迁移文件'">
        <p v-for="entry in review.review.changes" :key="entry.path"><span>{{ entry.original_path ? `${entry.original_path} → ` : '' }}{{ entry.path }}</span><small>{{ [entry.staged && '已暂存', entry.unstaged && '未暂存', entry.untracked && '未跟踪'].filter(Boolean).join(' · ') }}</small></p>
      </div>
      <p v-if="gitState">来源分支会交给目标，来源工作树转为目标原提交的分离 HEAD；不会隐式合并分歧提交。工作区和暂存区按审阅快照同步，被忽略文件留在原目录，重名则阻止迁移。</p>
      <p v-else>双方分支和提交保持原位。当前支持同提交且目标干净的迁移；被忽略文件留在原目录，重名则阻止迁移。</p>
      <details><summary>受影响会话（{{ review.review.affected.length }}）</summary><p v-for="chat in review.review.affected" :key="chat.id">{{ chat.title || '未命名会话' }}<small>{{ chat.id === review.conversation_id ? ' · 迁移到目标' : ' · 保持原目录' }}</small></p></details>
      <p>受影响会话的旧计划与旧提案将失效，文件引用清空。文字、图片和编辑草稿保留；编辑草稿仍属于原项目。请先结束双方及重叠目录中的任务和终端。</p>
      <p v-if="review.status === 'applied'" class="success" role="status" data-feedback>迁移完成，当前会话已切换到目标目录。</p>
      <p v-else-if="review.status === 'rolled_back'" class="success" role="status" data-feedback>迁移已撤回，双方已恢复迁移前的文件和暂存区，会话保留在来源。</p>
      <p v-else-if="review.status === 'cancelled'" role="status">迁移已取消。</p>
      <div v-if="pending" role="status" class="recovery" data-feedback><strong>{{ transferStatus[review.status] }}</strong><p>{{ review.error }}</p><p>已保留双侧备份。继续或撤回都会核对实际文件与索引；发现后来修改会保留现场并停止。来源和目标在处理完成前暂停应用内写入。</p></div>
      <p v-if="rollbackReview" class="recovery" role="alert" data-feedback>撤回会将本次已经迁移的文件与暂存区还原到迁移前。若有后来修改则停止，不覆盖新内容。会话仍留在来源目录。</p>
      <a v-if="review.backup_available" class="backup" :href="workspaceTransferBackup(review)" download><Download :size="14" />下载迁移前后文件与索引备份</a>
      <p v-if="review.backup_available"><small>索引恢复依赖原 Git 仓库及其中保留的快照引用；此 ZIP 不替代完整仓库备份。</small></p>
      <p v-if="error" class="error" role="alert" data-feedback>{{ error }} 若响应丢失，可再次查询或确认；同一迁移只完成一次。</p>
    </div>
    <template #footer>
      <button :disabled="busy" @click="open = false">{{ review?.status === 'prepared' ? '稍后处理' : '关闭' }}</button>
      <button v-if="review?.status === 'prepared'" :disabled="busy" @click="decide('cancel')">取消迁移</button>
      <button v-if="pending && !rollbackReview" :disabled="busy" @click="rollbackReview = true">审阅撤回</button>
      <button v-if="rollbackReview" :disabled="busy" @click="rollbackReview = false">保留迁移</button>
      <button v-if="rollbackReview" class="danger" :disabled="busy" @click="decide('rollback')">确认撤回迁移</button>
      <button v-else-if="review?.status === 'prepared' || pending" class="primary" :disabled="busy || !!review?.review.blockers.length" @click="decide('apply')">{{ busy ? '正在核对和迁移…' : pending ? '核对并继续迁移' : '备份并确认迁移' }}</button>
    </template>
  </ElDialog>
</template>

<style scoped>
.transfer-review { max-height:64vh; overflow:auto; color:var(--fn-text-primary); font-size:13px; line-height:1.65; }
p { margin:8px 0; overflow-wrap:anywhere; } .intro { margin:0 0 14px; } .locations { position:relative; display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:30px; }
.locations section { min-width:0; padding:12px; border:1px solid var(--fn-border-primary); border-radius:6px; } h3 { display:flex; gap:7px; align-items:center; font-size:14px; margin:7px 0; overflow-wrap:anywhere; } h3 svg { flex-shrink:0; }
small,summary { color:var(--fn-text-secondary); } summary { cursor:pointer; } .path { font:11px/1.6 Consolas,monospace; color:var(--fn-text-secondary); } .direction { position:absolute; top:50%; left:calc(50% - 9px); }
.changes { max-height:150px; overflow:auto; border:1px solid var(--fn-border-primary); margin:14px 0; padding:6px 10px; } .changes p { display:flex; justify-content:space-between; gap:14px; margin:5px 0; } .changes small { flex-shrink:0; }
.blockers,.recovery { padding:10px 12px; margin:12px 0; border:1px solid #dec99c; border-radius:5px; } .blockers strong,.error { color:#b42318; } .success { color:#237a44; }
.backup { display:inline-flex; gap:5px; align-items:center; color:var(--fn-brand); } button { margin-left:8px; font:inherit; font-size:12px; padding:7px 11px; border:1px solid var(--fn-border-primary); border-radius:5px; color:var(--fn-text-primary); background:transparent; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; } button:focus-visible,a:focus-visible,summary:focus-visible { outline:2px solid #346ee8; outline-offset:2px; } .primary { background:#245dcc; color:white; border-color:#245dcc; } .danger { color:#b42318; border-color:#d8a49e; }
</style>
