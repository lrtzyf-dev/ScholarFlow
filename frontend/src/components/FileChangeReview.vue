<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { RefreshCw, Download, RotateCcw } from 'lucide-vue-next'
import { getChange, getChangeContent, decideChange, undoChange, statusLabel, operationLabel, type ChangeDetail } from '../fileChangeApi'
import { projectError } from '../projectApi'
const id = defineModel<string | null>({ default: null })
const props = defineProps<{ conversationId?: string; readonly?: boolean }>()
const emit = defineEmits<{ changed: [id: string] }>()
const detail = ref<ChangeDetail | null>(null), error = ref(''), busy = ref(false), loading = ref(false)
const version = ref('diff'), content = ref<string | null>(null)
let epoch = 0
async function load(offset = 0) {
  if (!id.value) return
  const request = ++epoch, target = id.value
  loading.value = true; error.value = ''
  try {
    const data = await getChange(target, offset)
    if (request !== epoch) return
    if (offset && detail.value) data.diff.lines = [...detail.value.diff.lines, ...data.diff.lines]
    detail.value = data
    if (version.value !== 'diff') await loadContent()
  } catch (e) { if (request === epoch) error.value = projectError(e) }
  finally { if (request === epoch) loading.value = false }
}
async function loadContent() {
  if (!id.value || version.value === 'diff') return
  const target = id.value, selected = version.value
  content.value = null; error.value = ''
  try { const data = await getChangeContent(target, selected); if (id.value === target && selected === version.value) content.value = data.content }
  catch (e) { if (id.value === target && selected === version.value) error.value = projectError(e) }
}
async function act(action: 'apply' | 'reject' | 'undo') {
  if (!detail.value || busy.value) return
  busy.value = true; error.value = ''
  const target = detail.value.id
  try {
    if (action === 'undo') { const reverse = await undoChange(target, props.conversationId!); id.value = reverse.id }
    else await decideChange(target, detail.value.revision, action)
    emit('changed', target); await load()
  } catch (e) {
    await load()
    if ((action === 'apply' && detail.value?.status === 'applied') || (action === 'reject' && detail.value?.status === 'rejected')) emit('changed', target)
    else error.value = projectError(e)
  }
  finally { busy.value = false }
}
watch(id, value => { epoch++; detail.value = null; version.value = 'diff'; content.value = null; error.value = ''; if (value) void load() })
watch(version, () => void loadContent())
function close() { if (!busy.value) id.value = null }
</script>
<template>
  <ElDialog :model-value="!!id" title="文件差异" width="960px" top="5vh" append-to-body class="file-change-dialog" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @update:model-value="value => { if (!value) close() }">
    <div class="change-review" @keydown="event => { if (event.ctrlKey || event.metaKey) event.stopPropagation() }">
      <header v-if="detail"><div><strong>{{ detail.path }}</strong><small>{{ operationLabel(detail.operation) }} · {{ statusLabel(detail.status) }}</small></div><span class="diff-count"><b>+{{ detail.diff.added }}</b><em>−{{ detail.diff.removed }}</em></span><button aria-label="刷新差异" :disabled="loading || busy" @click="load()"><RefreshCw :size="14" /></button></header>
      <p v-if="detail?.summary" class="summary">{{ detail.summary }}</p>
      <div class="review-toolbar"><label>查看<select v-model="version" aria-label="差异视图"><option value="diff">差异</option><option value="before">修改前</option><option value="after">修改后</option><option value="current">当前磁盘内容</option></select></label><a v-if="id" :href="`/api/project-changes/${id}/patch`" download><Download :size="14" />导出补丁</a></div>
      <div v-if="detail" class="format-info">{{ detail.diff.before_format?.bytes ?? 0 }} → {{ detail.diff.after_format?.bytes ?? 0 }} 字节<span v-if="detail.diff.before_format?.bom || detail.diff.after_format?.bom"> · UTF-8 BOM</span><span v-if="detail.diff.after_format && !detail.diff.after_format.final_newline"> · 文件末尾无换行</span><span v-if="detail.diff.after_format?.crlf"> · CRLF</span></div>
      <div class="diff-scroll" tabindex="0" aria-label="文件差异内容" :aria-busy="loading">
        <template v-if="version === 'diff' && detail"><div v-for="(line, index) in detail.diff.lines" :key="index" class="diff-line" :class="line.kind"><span class="line-number">{{ line.old }}</span><span class="line-number">{{ line.new }}</span><span class="sign">{{ line.kind === 'add' ? '+' : line.kind === 'remove' ? '−' : '' }}</span><code>{{ line.text }}</code></div><p v-if="!detail.diff.lines.length" class="empty">内容无行级差异，请核对编码和换行格式</p><button v-if="detail.diff.next_offset !== null" :disabled="loading" @click="load(detail.diff.next_offset!)">加载更多差异</button></template>
        <pre v-else-if="version !== 'diff'">{{ content === null ? '文件不存在或正在读取…' : content }}</pre>
        <p v-else class="empty">正在读取差异…</p>
      </div>
      <p v-if="detail?.error" class="change-notice">{{ detail.error }}</p><p v-if="error" role="alert" class="change-error">{{ error }}</p>
      <p v-if="detail?.run_active" class="change-notice">此修改由正在运行的任务处理，请返回任务卡片批准或拒绝。</p>
    </div>
    <template #footer><div class="review-actions"><button :disabled="busy" @click="close">关闭</button><template v-if="detail && !readonly && !detail.run_active"><button v-if="['proposed', 'conflict'].includes(detail.status)" :disabled="busy" @click="act('reject')">放弃修改</button><button v-if="detail.status === 'applied' && conversationId" :disabled="busy" @click="act('undo')"><RotateCcw :size="14" />生成撤销差异</button><button v-if="['proposed', 'conflict'].includes(detail.status)" class="primary" :disabled="busy" @click="act('apply')">{{ busy ? '正在处理…' : '应用修改' }}</button></template></div></template>
  </ElDialog>
</template>
<style scoped>
.change-review { color:var(--fn-text-primary); } header { display:flex; align-items:center; gap:12px; } header>div { flex:1; min-width:0; } header strong { display:block; font-size:14px; overflow-wrap:anywhere; } small { display:block; color:var(--fn-text-secondary); margin-top:5px; } button,a { display:inline-flex; gap:6px; align-items:center; font:inherit; font-size:12px; color:inherit; background:transparent; border:1px solid var(--fn-border); border-radius:5px; padding:6px 10px; cursor:pointer; text-decoration:none; } button:disabled { opacity:.45; cursor:default; } button:focus-visible,a:focus-visible,select:focus-visible { outline:2px solid #346ee8; outline-offset:2px; } .diff-count { display:flex; gap:10px; font:12px Consolas,monospace; } .diff-count b { color:#237a44; } .diff-count em { color:#b42318; font-style:normal; } .summary { font-size:12px; margin:10px 0; overflow-wrap:anywhere; } .review-toolbar { display:flex; align-items:center; justify-content:space-between; gap:10px; margin:12px 0; font-size:12px; } select { margin-left:8px; padding:5px; color:inherit; background:var(--fn-bg-main); border:1px solid var(--fn-border); border-radius:4px; } .format-info { font-size:11px; color:var(--fn-text-secondary); margin-bottom:8px; } .diff-scroll { height:min(43vh,440px); overflow:auto; border:1px solid var(--fn-border); border-radius:5px; background:var(--fn-bg-main); } .diff-line { display:flex; min-width:max-content; min-height:20px; font:12px/20px Consolas,monospace; } .diff-line.add { background:#e8f6ec; color:#155b31; } .diff-line.remove { background:#fbe9e8; color:#9a2525; } .diff-line.hunk { background:var(--fn-bg-secondary); color:var(--fn-text-secondary); } .line-number { display:inline-block; flex:none; width:42px; text-align:right; padding-right:8px; user-select:none; opacity:.65; } .sign { width:20px; text-align:center; flex:none; user-select:none; } code { padding-right:18px; white-space:pre; tab-size:4; font:inherit; } pre { margin:0; padding:12px; white-space:pre; font:12px/1.7 Consolas,monospace; } .empty,.change-notice { color:var(--fn-text-secondary); font-size:12px; overflow-wrap:anywhere; } .empty { padding:12px; } .change-error { color:#b42318; font-size:12px; overflow-wrap:anywhere; } .review-actions { display:flex; gap:8px; justify-content:flex-end; } .primary { color:white; background:#245dcc; border-color:#245dcc; }
</style>
