<script setup lang="ts">
import { computed, ref, shallowRef, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { conversationState, type EditorDraft } from '../desktopState'
import { editableFile, prepareChange, getChange } from '../fileChangeApi'
import { projectError, type Project } from '../projectApi'
import FileChangeReview from './FileChangeReview.vue'
const visible = defineModel<boolean>({ default: false })
const props = defineProps<{ project: Project; conversationId: string; path: string | null }>()
const emit = defineEmits<{ changed: [] }>()
const doc = shallowRef(conversationState(props.conversationId)), error = ref(''), busy = ref(false), ready = ref(false)
const reviewId = ref<string | null>(null)
const draft = computed(() => doc.value.value.value.editor)
const status = computed(() => doc.value.status.value)
let requestId = '', fingerprint = '', submitted = ''
function update(field: 'path' | 'content', value: string) {
  if (!draft.value) return
  doc.value.update({ ...doc.value.value.value, editor: { ...draft.value, [field]: value } })
}
watch(visible, async value => {
  if (!value) { void doc.value.flush(); return }
  doc.value = conversationState(props.conversationId); ready.value = false; error.value = ''
  await doc.value.ready
  if (!visible.value) return
  if (draft.value) {
    if (draft.value.project_id !== props.project.id) { error.value = '另一个项目仍有编辑草稿，请在原项目处理后再打开文件'; return }
    ready.value = true; return
  }
  if (['conflict','deleted','error'].includes(status.value)) { error.value = '请先处理会话草稿的存储问题'; return }
  busy.value = true
  try {
    const file = props.path === null ? { content: '', sha256: null } : await editableFile(props.project.id, props.path)
    if (props.path !== null && file.content === null) throw Error('文件不存在，请刷新目录')
    const editor: EditorDraft = { project_id: props.project.id, path: props.path || '', operation: props.path === null ? 'create' : 'update', content: file.content || '', sha256: file.sha256 }
    doc.value.update({ ...doc.value.value.value, editor }); ready.value = true
  } catch (e) { error.value = projectError(e) }
  finally { busy.value = false }
})
async function prepare(remove = false) {
  if (!draft.value || busy.value) return
  busy.value = true; error.value = ''
  const value = { ...draft.value }
  const edit = { path: value.path, operation: remove ? 'delete' : value.operation, expected_sha256: value.sha256, content: remove ? null : value.content, summary: remove ? '删除项目文件' : '手动编辑项目文件' }
  const next = JSON.stringify([props.project.id, doc.value.value.value.workspace_revision || 0, edit])
  if (next !== fingerprint) { requestId = crypto.randomUUID(); fingerprint = next }
  try {
    await doc.value.flush()
    if (doc.value.status.value !== 'saved') throw Error('草稿尚未保存，请先解决保存问题')
    const change = await prepareChange(props.project.id, props.conversationId, requestId, edit, doc.value.value.value.workspace_revision || 0)
    submitted = JSON.stringify(value); reviewId.value = change.id
    emit('changed')
  } catch (e) { error.value = projectError(e) }
  finally { busy.value = false }
}
async function changed(id: string) {
  emit('changed')
  try {
    const change = await getChange(id)
    if (change.status === 'applied' && draft.value && submitted === JSON.stringify(draft.value)) {
      doc.value.update({ ...doc.value.value.value, editor: null }); await doc.value.flush()
      visible.value = false
    }
  } catch (e) { error.value = projectError(e) }
}
async function discard() {
  doc.value.update({ ...doc.value.value.value, editor: null }); await doc.value.flush()
  if (doc.value.status.value === 'saved') visible.value = false
}
</script>
<template>
  <ElDialog v-model="visible" title="编辑项目文件" width="940px" top="5vh" append-to-body :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy">
    <div class="file-editor" @keydown="event => { if (event.ctrlKey || event.metaKey) event.stopPropagation() }">
      <template v-if="ready && draft"><label class="path-label">项目相对路径<input :value="draft.path" :readonly="draft.operation !== 'create'" aria-label="编辑文件路径" placeholder="src/example.py" @input="update('path', ($event.target as HTMLInputElement).value)" /></label><p class="editor-caption">{{ props.project.name }} · {{ draft.operation === 'create' ? '新建文件' : '修改文件' }} · 修改后先查看差异再应用</p><textarea :value="draft.content" aria-label="文件编辑内容" spellcheck="false" @input="update('content', ($event.target as HTMLTextAreaElement).value)" /></template>
      <p v-else-if="busy">正在读取文件…</p>
      <div class="draft-status" role="status">{{ status === 'saved' ? '编辑草稿已保存到本机' : status === 'conflict' ? '另一窗口已更新会话草稿，请选择要保留的版本（包含聊天和文件编辑草稿）' : status === 'error' ? '草稿保存失败，当前内容仍在窗口中' : status === 'deleted' ? '会话已删除，不能保存此草稿' : '正在保存草稿…' }}<button v-if="status === 'error'" @click="doc.retry()">重试保存</button><template v-if="status === 'conflict'"><button @click="doc.resolveConflict(true)">保留本窗口草稿</button><button @click="doc.resolveConflict(false)">载入另一窗口草稿</button></template></div><p v-if="error" role="alert" class="editor-error">{{ error }}</p>
    </div>
    <template #footer><div class="editor-actions"><button :disabled="busy" @click="visible = false">关闭并保留草稿</button><button :disabled="busy || !ready || status !== 'saved'" @click="discard">丢弃编辑草稿</button><button v-if="draft?.operation === 'update'" :disabled="busy || !ready || status !== 'saved'" @click="prepare(true)">审阅删除文件</button><button class="primary" :disabled="busy || !ready || !draft?.path.trim() || status !== 'saved'" @click="prepare()">查看修改差异</button></div></template>
  </ElDialog>
  <FileChangeReview v-model="reviewId" :conversation-id="conversationId" @changed="changed" />
</template>
<style scoped>
.file-editor { color:var(--fn-text-primary); } .path-label { display:flex; align-items:center; gap:12px; font-size:12px; } input { flex:1; min-width:0; padding:8px; border:1px solid var(--fn-border); border-radius:5px; background:var(--fn-bg-main); color:inherit; } .editor-caption { font-size:12px; color:var(--fn-text-secondary); } textarea { display:block; width:100%; box-sizing:border-box; height:min(44vh,460px); resize:vertical; max-height:50vh; min-height:150px; white-space:pre; overflow:auto; padding:12px; background:var(--fn-bg-main); color:inherit; border:1px solid var(--fn-border); border-radius:5px; font:12px/1.7 Consolas,monospace; tab-size:4; } .draft-status { display:flex; flex-wrap:wrap; align-items:center; gap:8px; font-size:12px; margin-top:10px; color:var(--fn-text-secondary); } .editor-error { font-size:12px; color:#b42318; overflow-wrap:anywhere; } .editor-actions { display:flex; justify-content:flex-end; gap:8px; } button { font:inherit; font-size:12px; padding:7px 10px; border:1px solid var(--fn-border); border-radius:5px; background:transparent; color:inherit; cursor:pointer; } button:disabled { opacity:.45; cursor:default; } .primary { color:#fff; background:#245dcc; border-color:#245dcc; } button:focus-visible,input:focus-visible,textarea:focus-visible { outline:2px solid #346ee8; outline-offset:2px; }
</style>
