<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { ArrowRight, FolderOpen } from 'lucide-vue-next'
import { decideWorkspaceSwitch, type WorkspaceSwitch } from '../workspaceSwitchApi'
import { projectError } from '../projectApi'
const review = defineModel<WorkspaceSwitch | null>({ required: true })
const emit = defineEmits<{ switched: []; changed: [] }>()
const busy = ref(false), error = ref('')
const open = computed({ get: () => !!review.value, set: (value: boolean) => { if (!value && !busy.value) review.value = null } })
watch(() => review.value?.id, () => { error.value = '' })
function containTab(event: KeyboardEvent) {
  if (event.key !== 'Tab' || event.altKey || event.ctrlKey || event.metaKey) return
  const container = event.currentTarget as HTMLElement
  const controls = [...container.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]')]
    .filter(el => el.tabIndex >= 0 && el.getClientRects().length)
  const first = controls[0], last = controls[controls.length - 1]
  // Capture the boundary before the dialog's pointer-origin focus handler runs.
  if (!first || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
    event.preventDefault(); event.stopPropagation()
    ;(event.shiftKey ? last : first)?.focus()
  }
}
async function decide(action: 'apply' | 'cancel') {
  if (!review.value || busy.value) return
  const row = review.value
  busy.value = true; error.value = ''
  try {
    const result = await decideWorkspaceSwitch(row.conversation_id, row.id, action)
    review.value = result
    emit('changed')
    if (result.status === 'applied') emit('switched')
    if (result.status === 'cancelled') review.value = null
  } catch (e) { error.value = projectError(e) }
  finally { busy.value = false }
}
</script>

<template>
  <ElDialog v-model="open" title="切换会话工作目录" width="720px" top="8vh" append-to-body :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @keydown.capture="containTab">
    <div v-if="review" class="switch-review" @keydown="e => { if (e.ctrlKey || e.metaKey) e.stopPropagation() }">
      <p class="intro">同一会话将在目标目录继续。聊天记录、文字与图片草稿保留。</p>
      <div class="locations">
        <section v-for="(scope, index) in [review.snapshot.source, review.snapshot.target]" :key="scope.project_id">
          <small>{{ index ? '切换到' : '当前来源' }}</small>
          <h3><FolderOpen :size="16" />{{ scope.name }}</h3>
          <p class="path">{{ scope.root_path }}</p>
          <p>{{ scope.branch || '分离 HEAD' }} · {{ scope.head.slice(0, 12) }}</p>
          <p>改动 {{ scope.changes }} 项 · 已暂存 {{ scope.staged }} · 未暂存 / 未跟踪 {{ scope.unstaged }}</p>
        </section>
        <ArrowRight class="direction" :size="18" />
      </div>
      <ul>
        <li>各目录的提交、文件、暂存区和终端保持原归属。此操作不搬运 Git 改动。</li>
        <li>旧计划和中断任务保留供查看；切换后请重新规划或发起任务，即使返回原目录也需重新发起。</li>
        <li>文件引用将清空，请在目标目录重新选择。<span v-if="review.snapshot.file_paths.length">当前引用：{{ review.snapshot.file_paths.join('、') }}</span></li>
        <li v-if="review.snapshot.editor">编辑草稿「{{ review.snapshot.editor.path || '未命名文件' }}」仍属于原项目，返回该项目后可继续编辑。</li>
      </ul>
      <p v-if="review.status === 'applied'" class="success" role="status">此切换已完成。后续操作以会话当前工作目录为准。</p>
      <p v-else-if="review.status !== 'prepared'" role="status">此审阅已取消或过期，请关闭后重新审阅。</p>
      <p v-if="error" class="error" role="alert">{{ error }} 若响应丢失，可再次确认；同一记录只执行一次。</p>
    </div>
    <template #footer>
      <button :disabled="busy" @click="open = false">{{ review?.status === 'prepared' ? '稍后处理' : '关闭' }}</button>
      <button v-if="review?.status === 'prepared'" :disabled="busy" @click="decide('cancel')">取消切换</button>
      <button v-if="review?.status === 'prepared'" class="primary" :disabled="busy" @click="decide('apply')">{{ busy ? '正在核对…' : '确认切换工作目录' }}</button>
    </template>
  </ElDialog>
</template>

<style scoped>
.switch-review { max-height:64vh; overflow:auto; color:var(--fn-text-primary); font-size:13px; line-height:1.65; }
.intro { margin:0 0 14px; } .locations { position:relative; display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:32px; }
.locations section { min-width:0; padding:12px; border:1px solid var(--fn-border-primary); border-radius:6px; }
small { color:var(--fn-text-secondary); } h3 { display:flex; gap:7px; align-items:center; font-size:14px; margin:7px 0; overflow-wrap:anywhere; } h3 svg { flex-shrink:0; }
p { margin:6px 0; } .path { font:11px/1.6 Consolas,monospace; overflow-wrap:anywhere; color:var(--fn-text-secondary); }
.direction { position:absolute; top:50%; left:calc(50% - 9px); } ul { margin:16px 0 0; padding-left:20px; } li { margin:8px 0; overflow-wrap:anywhere; }
.error { color:#b42318; } .success { color:#237a44; } button { margin-left:8px; font:inherit; font-size:12px; padding:7px 11px; border:1px solid var(--fn-border-primary); border-radius:5px; color:var(--fn-text-primary); background:transparent; cursor:pointer; }
button:disabled { opacity:.5; cursor:default; } button:focus-visible { outline:2px solid #346ee8; outline-offset:2px; } .primary { background:#245dcc; color:white; border-color:#245dcc; }
</style>
