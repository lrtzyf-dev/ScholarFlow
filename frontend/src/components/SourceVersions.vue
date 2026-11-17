<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { History, Upload, BookOpen } from 'lucide-vue-next'
import {
  captureProjectSource,
  getSourceHistory,
  getSourceRevision,
  importSourcesToLibrary,
  sourceError,
  uploadSource,
  type SourceRevision,
} from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'
import { sourceUpdateState } from '../sourceUpdateDrafts'
import SourceVersionDiff from './SourceVersionDiff.vue'
import {
  getSourceUpdateDraft,
  getSourceUpdateFile,
  saveSourceUpdateDraft,
  saveLocalUpdateDraft,
  discardSourceUpdateDraft,
  type PersistedSourceDraft,
} from '../sourceVersionApi'

const props = defineProps<{
  record: SourceRevision
  conversationId: string
  disabled?: boolean
  projectId?: string | null
  workspaceRevision?: number
  allowSelect?: boolean
}>()
const emit = defineEmits<{ updated: [record: SourceRevision]; select: [record: SourceRevision] }>()
const router = useRouter()
const opened = ref(false),
  loading = ref(false),
  importBusy = ref(false),
  error = ref(''),
  notice = ref('')
const versions = ref<SourceRevision[]>([]),
  nextVersion = ref<number | null>(null)
const head = ref<SourceRevision | null>(null)
const compareFrom = ref<SourceRevision | null>(null)
const remoteDraft = ref<PersistedSourceDraft | null>(null)
const imported = ref<Record<string, boolean>>({})
const picker = ref<HTMLInputElement | null>(null)
const updateState = computed(() => sourceUpdateState(props.conversationId, props.record.source_id))
const pending = computed({
  get: () => updateState.value.pending,
  set: (value) => {
    updateState.value.pending = value
  },
})
const busy = computed(() => importBusy.value || updateState.value.saving)
const importRequests = new Map<string, string>()
let generation = 0
let pickerContext: {
  token: number
  conversationId: string
  sourceId: string
  baseline: SourceRevision
  state: ReturnType<typeof sourceUpdateState>
} | null = null
const localPath = computed(() => {
  const prefix = props.projectId ? `project:${props.projectId}:` : ''
  return prefix && props.record.kind === 'local' && props.record.origin_ref?.startsWith(prefix)
    ? props.record.origin_ref.slice(prefix.length)
    : ''
})
const mayUpdate = computed(() => props.record.kind === 'upload' || Boolean(localPath.value))
watch(
  () => [props.conversationId, props.record.source_id],
  () => {
    generation++
    opened.value = Boolean(pending.value)
    loading.value = false
    importBusy.value = false
    versions.value = []
    head.value = null
    compareFrom.value = null
    remoteDraft.value = null
    error.value = ''
    notice.value = ''
    if (opened.value) void history()
    if (['upload', 'local'].includes(props.record.kind)) void restoreUpdateDraft()
  },
  { immediate: true }
)
onBeforeUnmount(() => {
  generation++
})
watch(
  () => updateState.value.lastSaved,
  (item) => {
    if (!item) return
    notice.value = `已保存 v${item.version}。本轮资料选择和旧引用仍保留原版本；可在历史列表中另选新版本。`
    emit('updated', item)
    if (opened.value) void history()
  }
)
function isImported(record: SourceRevision) {
  return Boolean(record.library_paper_id || imported.value[record.revision_id])
}
function date(value?: string) {
  return value ? new Date(value).toLocaleString() : '保存时间未记录'
}
function size(value?: number) {
  return typeof value === 'number' ? `${(value / 1024).toFixed(1)} KB` : ''
}
function parseLabel(value?: string) {
  return value === 'text_available'
    ? '文本可读'
    : value === 'pdf_on_demand'
      ? 'PDF 按页读取'
      : '原件可下载'
}
async function history(more = false) {
  const token = generation,
    cid = props.conversationId,
    sourceId = props.record.source_id
  loading.value = true
  error.value = ''
  if (!more) head.value = null
  try {
    const page = await getSourceHistory(
      sourceId,
      cid,
      more ? nextVersion.value || undefined : undefined
    )
    if (token !== generation) return
    versions.value = more
      ? [
          ...versions.value,
          ...page.items.filter(
            (item) => !versions.value.some((old) => old.revision_id === item.revision_id)
          ),
        ]
      : page.items
    nextVersion.value = page.next_before_version
    if (!more)
      head.value = page.items.find((item) => item.revision_id === page.current_revision_id) || null
  } catch (err) {
    if (token === generation) error.value = sourceError(err)
  } finally {
    if (token === generation) loading.value = false
  }
}
async function toggleHistory() {
  opened.value = !opened.value
  if (opened.value) await history()
}
async function prepareUpdate() {
  const token = generation
  opened.value = true
  await history()
  if (token !== generation || !head.value || props.disabled || busy.value) return
  if (props.record.kind === 'upload') {
    pickerContext = {
      token,
      conversationId: props.conversationId,
      sourceId: props.record.source_id,
      baseline: head.value,
      state: updateState.value,
    }
    picker.value?.click()
  } else {
    pending.value = {
      local: {
        projectId: props.projectId!,
        workspaceRevision: props.workspaceRevision || 0,
        path: localPath.value,
      },
      expectedVersion: head.value.version,
      baseline: head.value,
      requestId: `project_update_${crypto.randomUUID()}`,
      conflict: false,
    }
    const state = updateState.value
    state.saving = true
    try {
      await persistPending(state, props.conversationId, props.record.source_id)
    } catch (err) {
      if (token === generation) error.value = sourceError(err)
    } finally {
      state.saving = false
    }
  }
}
async function recoveredPending(saved: PersistedSourceDraft, cid: string, sourceId: string) {
  const draft = saved.draft
  if (!draft) return null
  const baseline = await getSourceRevision(draft.baseline_revision_id, cid)
  const local = draft.local
    ? {
        projectId: draft.local.project_id,
        workspaceRevision: draft.local.workspace_revision,
        path: draft.local.path,
      }
    : undefined
  const file =
    draft.kind === 'upload'
      ? new File([await getSourceUpdateFile(cid, sourceId, saved.revision)], draft.filename, {
          type: draft.media_type,
        })
      : undefined
  return {
    file,
    local,
    expectedVersion: draft.expected_version,
    baseline,
    requestId: draft.request_id,
    conflict: false,
    persisted: true,
  }
}
async function restoreUpdateDraft() {
  const state = updateState.value,
    cid = props.conversationId,
    sourceId = props.record.source_id,
    token = generation
  if (state.loaded || state.saving) return
  state.saving = true
  try {
    const saved = await getSourceUpdateDraft(cid, sourceId)
    state.persistedRevision = saved.revision
    if (saved.draft && !state.pending) {
      state.pending = await recoveredPending(saved, cid, sourceId)
      if (token === generation) {
        opened.value = true
        notice.value = '已恢复待更新资料。核对版本后可保存新版本，或取消更新。'
        void history()
      }
    }
    state.loaded = true
  } catch (err) {
    if (token === generation) error.value = `待更新文件恢复未完成：${sourceError(err)}`
  } finally {
    state.saving = false
  }
}
async function persistPending(
  state: ReturnType<typeof sourceUpdateState>,
  cid: string,
  sourceId: string
) {
  const work = state.pending
  if (!work || (!work.file && !work.local)) return
  let saved: PersistedSourceDraft
  try {
    saved = work.file
      ? await saveSourceUpdateDraft(
          cid,
          sourceId,
          state.persistedRevision,
          work.baseline.revision_id,
          work.requestId,
          work.file
        )
      : await saveLocalUpdateDraft(
          cid,
          sourceId,
          state.persistedRevision,
          work.baseline.revision_id,
          work.requestId,
          work.local!
        )
  } catch (err) {
    if (
      (err as { response?: { status?: number } })?.response?.status === 409 &&
      cid === props.conversationId &&
      sourceId === props.record.source_id
    ) {
      const latest = await getSourceUpdateDraft(cid, sourceId)
      if (cid === props.conversationId && sourceId === props.record.source_id)
        remoteDraft.value = latest
    }
    throw err
  }
  state.persistedRevision = saved.revision
  if (state.pending?.requestId === work.requestId) state.pending = { ...work, persisted: true }
}
async function picked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const target = pickerContext
  pickerContext = null
  if (!file || !target) return
  const { state, conversationId: cid, sourceId, token, baseline } = target
  state.pending = {
    file,
    expectedVersion: baseline.version,
    baseline,
    requestId: `source_update_${crypto.randomUUID()}`,
    conflict: false,
  }
  if (token === generation) {
    error.value = ''
    notice.value = ''
  }
  state.saving = true
  try {
    await persistPending(state, cid, sourceId)
  } catch (err) {
    if (token === generation) error.value = `原件仍在本窗口，暂存未完成：${sourceError(err)}`
  } finally {
    state.saving = false
  }
}
async function update() {
  const state = updateState.value,
    work = state.pending,
    original = props.record,
    cid = props.conversationId,
    token = generation
  if (!work || busy.value || props.disabled || work.conflict) return
  if (!work.file && !work.local) return
  state.saving = true
  error.value = ''
  try {
    await persistPending(state, cid, original.source_id)
    const item = work.file
      ? await uploadSource(work.file, cid, work.requestId, {
          source_id: original.source_id,
          expected_version: work.expectedVersion,
        })
      : await captureProjectSource({
          conversation_id: cid,
          request_id: work.requestId,
          project_id: work.local!.projectId,
          workspace_revision: work.local!.workspaceRevision,
          path: work.local!.path,
          source_id: original.source_id,
          expected_version: work.expectedVersion,
        })
    if (state.pending?.requestId === work.requestId) state.pending = null
    state.lastSaved = item
    if (work.file || work.local) {
      try {
        state.persistedRevision = (await getSourceUpdateDraft(cid, original.source_id)).revision
      } catch {
        state.loaded = false
      }
    }
  } catch (err: unknown) {
    const message = sourceError(err)
    if ((err as { response?: { status?: number } })?.response?.status === 409) {
      if (state.pending?.requestId === work.requestId) state.pending = { ...work, conflict: true }
      if (token !== generation) return
      await history()
      if (token !== generation) return
      error.value = `${message}。待更新文件仍保留，请核对版本后决定是否重试。`
    } else if (token === generation) error.value = message
  } finally {
    state.saving = false
  }
}
async function useRemoteDraft() {
  if (busy.value) return
  const state = updateState.value,
    cid = props.conversationId,
    sourceId = props.record.source_id,
    token = generation
  state.saving = true
  try {
    const saved = await getSourceUpdateDraft(cid, sourceId)
    const restored = await recoveredPending(saved, cid, sourceId)
    state.pending = restored
    state.persistedRevision = saved.revision
    state.loaded = true
    if (token === generation) {
      remoteDraft.value = null
      error.value = ''
      notice.value = '已载入另一窗口的暂存状态。'
    }
  } catch (err) {
    if (token === generation) error.value = sourceError(err)
  } finally {
    state.saving = false
  }
}
function keepLocalDraft() {
  if (!remoteDraft.value || busy.value) return
  updateState.value.persistedRevision = remoteDraft.value.revision
  if (pending.value) pending.value = { ...pending.value, conflict: false, persisted: false }
  remoteDraft.value = null
  error.value = ''
  notice.value = '已选择保留本窗口文件；点击保存新版本时，将替换另一窗口的待更新暂存。'
}
async function cancelUpdate() {
  const state = updateState.value,
    cid = props.conversationId,
    sourceId = props.record.source_id,
    token = generation
  if (busy.value) return
  state.saving = true
  try {
    if (state.pending?.file || state.pending?.local)
      state.persistedRevision = (
        await discardSourceUpdateDraft(cid, sourceId, state.persistedRevision)
      ).revision
    state.pending = null
  } catch (err) {
    if (token === generation) error.value = sourceError(err)
  } finally {
    state.saving = false
  }
}
function rebase() {
  if (!pending.value || !head.value) return
  pending.value = {
    ...pending.value,
    expectedVersion: head.value.version,
    baseline: head.value,
    requestId: `source_update_${crypto.randomUUID()}`,
    conflict: false,
    persisted: false,
  }
  error.value = ''
}
async function importVersion(item: SourceRevision) {
  const token = generation,
    cid = props.conversationId
  if (busy.value || props.disabled || isImported(item)) return
  importBusy.value = true
  error.value = ''
  const signature = `${cid}:${item.revision_id}`
  let requestId = importRequests.get(signature)
  if (!requestId) {
    requestId = `library_${crypto.randomUUID()}`
    importRequests.set(signature, requestId)
  }
  try {
    const result = await importSourcesToLibrary(cid, [item.revision_id], requestId)
    if (token !== generation) return
    const receipt = result.items.find((row) => row.revision_id === item.revision_id)
    if (!receipt?.paper_id || receipt.error) throw Error(receipt?.error || '此版本尚未导入，请重试')
    imported.value = { ...imported.value, [item.revision_id]: true }
    notice.value = `已将 v${item.version} 加入文献库，其他版本未随此操作入库。`
    window.dispatchEvent(new Event('scholarflow:library-updated'))
    emit('updated', { ...item, library_paper_id: receipt.paper_id })
  } catch (err) {
    if (token === generation) error.value = sourceError(err)
  } finally {
    if (token === generation) importBusy.value = false
  }
}
</script>

<template>
  <div class="source-versions" :data-source-id="record.source_id">
    <div class="version-actions">
      <button type="button" :aria-expanded="opened" @click="toggleHistory">
        <History :size="14" />版本历史
      </button>
      <button
        v-if="mayUpdate"
        type="button"
        :disabled="disabled || busy || loading"
        @click="prepareUpdate"
      >
        <Upload :size="14" />更新此资料
      </button>
      <button
        type="button"
        :disabled="disabled || busy || isImported(record)"
        @click="importVersion(record)"
      >
        <BookOpen :size="14" />{{
          isImported(record) ? `v${record.version} 已入库` : `将 v${record.version} 加入文献库`
        }}
      </button>
    </div>
    <input
      ref="picker"
      type="file"
      accept=".pdf,.txt,.md,.markdown,.csv,.docx,.xlsx"
      hidden
      @change="picked"
    />
    <p v-if="notice" class="version-notice" role="status">{{ notice }}</p>
    <p v-if="error" class="version-error" role="alert">{{ error }}</p>
    <section v-if="opened" class="version-panel" aria-label="资料版本历史">
      <p>历史版本保留原件与引用。选择阅读、用于本轮或入库均只针对指定版本。</p>
      <p v-if="record.kind === 'local' && !localPath">
        更新本地资料时，请返回所属项目会话重新采集此文件。
      </p>
      <div v-if="pending" class="version-update" aria-label="确认更新资料">
        <strong>将此资料更新为新版本</strong>
        <span
          >原版本：v{{ pending.expectedVersion }}；当前最新：v{{ head?.version }} ·
          {{ head?.title }}</span
        >
        <span v-if="pending.conflict"
          >更新前原件：{{ pending.baseline.title }} · {{ size(pending.baseline.raw_size) }} ·
          {{ pending.baseline.raw_sha256?.slice(0, 16) }}</span
        >
        <span
          >待保存：{{ pending.file?.name || pending.local?.path }}
          {{ pending.file ? size(pending.file.size) : '（重新读取项目文件）' }}</span
        >
        <span v-if="head?.raw_sha256">当前原件指纹：{{ head.raw_sha256.slice(0, 16) }}</span>
        <span>只添加新版本，已有引用和本轮选中版本不会自动替换。</span>
        <span v-if="pending.file && pending.persisted"
          >待更新原件已暂存在本机，重启后可恢复；尚未创建新版本或加入文献库。</span
        >
        <span v-else-if="pending.file">待更新原件仅在本窗口，暂存成功后才能保证重启恢复。</span>
        <span v-else
          >项目更新选择{{
            pending.persisted ? '已在本机保存，重启后可恢复' : '尚未持久保存'
          }}。文件会在确认保存新版本时重新读取，请核对路径和当前工作区。</span
        >
        <div v-if="remoteDraft" class="draft-conflict" role="status">
          <p>
            另一窗口的暂存：{{ remoteDraft.draft?.filename || '已取消或已保存' }}；{{
              remoteDraft.draft ? size(remoteDraft.draft.raw_size) : ''
            }}。本窗口文件仍保留。
          </p>
          <button :disabled="busy" @click="useRemoteDraft">使用另一窗口的暂存</button>
          <button :disabled="busy" @click="keepLocalDraft">保留本窗口文件并替换暂存</button>
        </div>
        <div class="version-actions">
          <button
            v-if="pending.conflict"
            type="button"
            :disabled="busy || loading || !head"
            @click="rebase"
          >
            以当前 v{{ head?.version }} 为基础重新确认
          </button>
          <button v-else type="button" :disabled="busy || disabled" @click="update">
            {{ busy ? '正在保存…' : '保存新版本' }}
          </button>
          <button type="button" :disabled="busy" @click="cancelUpdate">取消更新</button>
        </div>
      </div>
      <p v-if="loading" role="status">正在读取版本…</p>
      <article
        v-for="item in versions"
        :key="item.revision_id"
        class="version-row"
        :data-version="item.version"
      >
        <div>
          <strong>v{{ item.version }} · {{ item.title }}</strong>
          <span v-if="item.revision_id === item.current_revision_id" class="version-tag">最新</span>
          <span v-if="item.revision_id === record.revision_id" class="version-tag"
            >当前所查看版本</span
          >
          <small
            >{{ date(item.created_at) }} · {{ size(item.raw_size) }} ·
            {{ parseLabel(item.parse_status) }}</small
          >
          <small :title="item.raw_sha256"
            >原件 SHA-256：{{ item.raw_sha256?.slice(0, 16) || '未记录' }}</small
          >
        </div>
        <div class="version-actions">
          <button
            v-if="head && item.revision_id !== head.revision_id"
            type="button"
            @click="compareFrom = compareFrom?.revision_id === item.revision_id ? null : item"
          >
            与最新 v{{ head.version }} 比较
          </button>
          <button
            type="button"
            @click="router.push(sourceReaderLocation(item.revision_id, conversationId))"
          >
            阅读 v{{ item.version }}
          </button>
          <button
            v-if="allowSelect"
            type="button"
            :disabled="disabled"
            @click="emit('select', item)"
          >
            本轮使用 v{{ item.version }}
          </button>
          <button
            type="button"
            :disabled="disabled || busy || isImported(item)"
            @click="importVersion(item)"
          >
            {{ isImported(item) ? '已加入文献库' : `将 v${item.version} 入库` }}
          </button>
        </div>
      </article>
      <SourceVersionDiff
        v-if="compareFrom && head"
        :key="`${conversationId}:${compareFrom.revision_id}:${head.revision_id}`"
        :conversation-id="conversationId"
        :left-id="compareFrom.revision_id"
        :right-id="head.revision_id"
      />
      <button v-if="nextVersion" type="button" :disabled="loading" @click="history(true)">
        加载更早版本
      </button>
      <button type="button" :disabled="loading" @click="history()">刷新版本</button>
    </section>
  </div>
</template>

<style scoped>
.source-versions {
  width: 100%;
  font-size: 12px;
  color: #365483;
}
.version-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  align-items: center;
}
.source-versions button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid #dce3f2;
  border-radius: 6px;
  background: #fff;
  color: #365483;
  padding: 6px 9px;
  cursor: pointer;
  font-size: 12px;
}
.source-versions button:disabled {
  opacity: 0.5;
  cursor: default;
}
.source-versions button:focus-visible {
  outline: 2px solid #365fa5;
  outline-offset: 2px;
}
.version-panel {
  margin-top: 9px;
  padding: 12px;
  border: 1px solid #dce3f2;
  border-radius: 8px;
  background: #f7f9ff;
  max-height: 410px;
  overflow: auto;
}
.version-panel > p {
  margin: 0 0 9px;
  line-height: 1.6;
}
.version-update {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 12px;
  border: 1px solid #b8c9e7;
  border-radius: 6px;
  background: #fff;
  margin-bottom: 10px;
  overflow-wrap: anywhere;
}
.version-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 11px 0;
  border-bottom: 1px solid #dce3f2;
}
.version-row > div {
  min-width: 0;
  max-width: 100%;
  overflow-wrap: anywhere;
}
.version-row small {
  display: block;
  color: #586d8c;
  margin-top: 5px;
  overflow-wrap: anywhere;
}
.version-tag {
  display: inline-block;
  margin-left: 6px;
  padding: 2px 5px;
  background: #e3eaf7;
  border-radius: 4px;
  font-size: 11px;
}
.version-notice {
  color: #315e48;
  line-height: 1.6;
  margin: 8px 0;
}
.version-error {
  color: #a33c35;
  line-height: 1.6;
  margin: 8px 0;
}
</style>
