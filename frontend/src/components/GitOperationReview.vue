<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { ElDialog } from 'element-plus'
import { Check, RefreshCw, X } from 'lucide-vue-next'
import { gitOperation, gitDecide, gitActionLabels as labels, gitStatusLabels as statuses, type GitOperation, type GitOperationTarget } from '../projectGitApi'
import { projectError } from '../projectApi'

const props = defineProps<{ operation: GitOperation | GitOperationTarget }>()
const emit = defineEmits<{ close: []; changed: [operation: GitOperation] }>()
const current = ref<GitOperation | null>('status' in props.operation ? props.operation : null)
const busy = ref(false), error = ref(''), uncertain = ref(false)
let alive = true, timer: ReturnType<typeof setTimeout> | undefined
function accept(row: GitOperation) {
  if (!alive) return
  current.value = row; uncertain.value = false; emit('changed', row)
  clearTimeout(timer)
  if (row.status === 'running' || (row.agent_owned && row.status === 'prepared')) timer = setTimeout(() => void reload(), 1500)
}
async function reload() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { accept(await gitOperation(props.operation.project_id, props.operation.id)) }
  catch (e) { if (alive) { error.value = projectError(e); uncertain.value = true } }
  finally { if (alive) busy.value = false }
}
async function decide(cancel = false) {
  if (busy.value || uncertain.value || !current.value || current.value.agent_owned) return
  busy.value = true; error.value = ''; clearTimeout(timer)
  try { accept(await gitDecide(current.value, cancel)) }
  catch (e) { if (alive) { error.value = projectError(e); uncertain.value = true } }
  finally { if (alive) busy.value = false }
}
onMounted(() => { void reload() })
onUnmounted(() => { alive = false; clearTimeout(timer) })
</script>

<template>
  <ElDialog :model-value="true" :title="current ? `Git ${labels[current.action]}审阅` : 'Git 操作审阅'" width="900px" top="4vh" append-to-body :close-on-click-modal="false" @update:model-value="value => { if (!value) emit('close') }">
    <div class="git-operation" @keydown="event => { if (event.ctrlKey || event.metaKey) event.stopPropagation() }">
      <p v-if="error" role="alert" class="operation-error">{{ error }}</p>
      <p v-if="!current" role="status">{{ busy ? '正在读取 Git 操作记录…' : '记录暂时无法读取。' }}<button v-if="!busy" @click="reload">重试</button></p>
      <template v-if="current">
      <div class="operation-meta"><strong>{{ current.branch?.replace('refs/heads/', '') || 'detached HEAD' }}</strong><span>{{ current.head?.slice(0, 8) || '首次提交前' }}</span><span>{{ statuses[current.status] }}</span><button aria-label="刷新 Git 操作" title="刷新 Git 操作" :disabled="busy" @click="reload"><RefreshCw :size="15" /></button></div>
      <p v-if="current.author" class="operation-author">{{ current.author }}</p>
      <pre v-if="current.message" class="operation-message">{{ current.message }}</pre>
      <div class="operation-paths" aria-label="操作文件"><span v-for="path in current.paths" :key="path">{{ path }}</span></div>
      <p v-if="current.action === 'commit'" class="operation-note">提交已暂存内容。本机 Git hooks 与签名配置生效，hooks 可能调整内容或提交说明。</p>
      <p v-else class="operation-note">{{ current.action === 'stage' ? '将所选工作区更改加入暂存区。' : '从暂存区移除所选更改，保留工作区文件。' }}</p>
      <p v-if="current.agent_owned && current.status === 'prepared'" class="operation-note">由模型发起。请返回所属聊天或后台任务卡片批准或拒绝；此处仅供审阅。</p>
      <p v-if="current.error" role="alert" class="operation-error">{{ current.error }}</p>
      <p v-if="uncertain" class="operation-error">响应未确认，请先刷新操作状态。</p>
      <pre class="operation-patch" tabindex="0" aria-label="Git 操作差异">{{ current.patch || '无文本差异' }}</pre>
      <pre v-if="current.output" class="operation-output" aria-label="Git 输出">{{ current.output }}</pre>
      <p v-if="current.result_head" class="operation-result">HEAD {{ current.result_head.slice(0, 12) }}</p>
      </template>
    </div>
    <template #footer>
      <button v-if="current?.status === 'prepared' && !current.agent_owned" :disabled="busy || uncertain" @click="decide(true)"><X :size="14" />取消操作</button>
      <button @click="emit('close')">关闭</button>
      <button v-if="current?.status === 'prepared' && !current.agent_owned" class="operation-confirm" :disabled="busy || uncertain" @click="decide()"><Check :size="14" />{{ busy ? '处理中…' : `确认${labels[current.action]}` }}</button>
    </template>
  </ElDialog>
</template>

<style scoped>
.git-operation { color:var(--fn-text-primary,#202124); max-height:calc(92vh - 150px); overflow:auto; }
button { display:inline-flex; align-items:center; justify-content:center; gap:5px; border:1px solid var(--fn-border,#ddd); border-radius:4px; padding:6px 10px; color:var(--fn-text-primary,#202124); background:transparent; font:inherit; font-size:12px; cursor:pointer; }
button:hover { background:var(--fn-bg-hover,#f2f3f4); } button:disabled { opacity:.45; cursor:default; } button:focus-visible { outline:2px solid #346ee8; outline-offset:2px; }
.operation-meta { display:flex; align-items:center; gap:12px; font-size:12px; flex-wrap:wrap; } .operation-meta strong { overflow-wrap:anywhere; } .operation-meta button { margin-left:auto; } .operation-meta span,.operation-author,.operation-note,.operation-result { color:var(--fn-text-secondary,#666); font-size:12px; }
.operation-paths { display:flex; flex-wrap:wrap; gap:4px 14px; max-height:70px; overflow:auto; margin:10px 0; font:12px/1.5 Consolas,monospace; } .operation-paths span { overflow-wrap:anywhere; }
.operation-message { white-space:pre-wrap; overflow-wrap:anywhere; padding:8px 0; font-family:inherit; font-size:13px; line-height:1.5; border-bottom:1px solid var(--fn-border,#ddd); }
.operation-patch { margin:10px 0; padding:10px; height:260px; overflow:auto; border:1px solid var(--fn-border,#ddd); border-radius:4px; font:12px/20px Consolas,monospace; color:var(--fn-text-primary,#202124); }
.operation-output { max-height:100px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:12px/1.5 Consolas,monospace; } .operation-error { color:#b42318; font-size:12px; overflow-wrap:anywhere; }
.operation-confirm { background:#245dcc; color:white; border-color:#245dcc; } .operation-confirm:hover { background:#1c4fae; }
</style>
