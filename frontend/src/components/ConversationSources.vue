<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { FileText, Upload, X, BookOpen, FolderOpen, ArrowUpRight } from 'lucide-vue-next'
import {
  listSources,
  getSourceRevision,
  uploadSource,
  captureProjectSource,
  importSourcesToLibrary,
  ensureSourceConversation,
  sourceError,
  type SourceRevision,
} from '../sourceApi'
import { MAX_SOURCE_ATTACHMENTS, sourceReaderLocation, sourceTypeLabel } from '../sourceSelection'
import { normalizeSourceIds } from '../sourceSelection'
import { conversationState } from '../desktopState'
import SourceVersions from './SourceVersions.vue'
import ProviderFilesPanel from './ProviderFilesPanel.vue'

const props = defineProps<{
  conversationId: string
  sourceIds: string[]
  disabled?: boolean
  projectId?: string | null
  workspaceRevision: number
  projectFiles: string[]
}>()
const emit = defineEmits<{
  attach: [value: { conversationId: string; revisionId: string }]
  remove: [revisionId: string]
  busy: [value: boolean]
  captured: [value: { conversationId: string; path: string }]
}>()
const router = useRouter()
const opened = ref(false),
  picker = ref<HTMLInputElement | null>(null)
const dialog = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null
const records = ref<SourceRevision[]>([]),
  metadata = ref<Record<string, SourceRevision>>({})
const busyByConversation = ref<Record<string, number>>({})
const busyCount = computed(() => busyByConversation.value[props.conversationId] || 0)
const loading = ref(false),
  error = ref(''),
  importStatus = ref<Record<string, string>>({})
function beginBusy(cid: string) {
  busyByConversation.value = {
    ...busyByConversation.value,
    [cid]: (busyByConversation.value[cid] || 0) + 1,
  }
}
function endBusy(cid: string) {
  busyByConversation.value = {
    ...busyByConversation.value,
    [cid]: Math.max(0, (busyByConversation.value[cid] || 0) - 1),
  }
}
const librarySelection = ref<string[]>([])
const importRequests = new Map<string, string>()
const failed = ref<Record<string, { file: File; requestId: string; error: string }[]>>({})
const failedHere = computed(() => failed.value[props.conversationId] || [])
const selected = computed(() =>
  props.sourceIds.map(
    (id) =>
      metadata.value[id] || {
        revision_id: id,
        title: '正在读取资料信息…',
        version: 0,
        media_type: '',
        source_id: '',
        kind: '',
      }
  )
)
const visibleRecords = computed(() => [
  ...records.value,
  ...selected.value.filter(
    (item) =>
      item.version && !records.value.some((record) => record.revision_id === item.revision_id)
  ),
])
let generation = 0
watch(
  () => props.conversationId,
  () => {
    opened.value = false
    records.value = []
    error.value = ''
    librarySelection.value = []
    void refresh()
  },
  { immediate: true }
)
watch(
  () => props.sourceIds,
  () => {
    void loadSelected()
  },
  { deep: true, immediate: true }
)
watch(busyCount, (count) => emit('busy', count > 0))
watch(opened, async (open) => {
  if (open) {
    returnFocus = document.activeElement as HTMLElement
    await nextTick()
    dialog.value?.focus()
  } else returnFocus?.focus()
})
function dialogKeys(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    opened.value = false
    return
  }
  if (event.key !== 'Tab') return
  const controls = [
    ...(dialog.value?.querySelectorAll<HTMLElement>(
      'button:not(:disabled),input:not(:disabled),[href]'
    ) || []),
  ]
  const first = controls[0],
    last = controls.at(-1)
  if (
    event.shiftKey &&
    (document.activeElement === first || document.activeElement === dialog.value)
  ) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
function remember(item: SourceRevision) {
  metadata.value = { ...metadata.value, [item.revision_id]: item }
}
async function loadSelected() {
  const cid = props.conversationId
  await Promise.all(
    props.sourceIds
      .filter((id) => !metadata.value[id]?.version)
      .map(async (id) => {
        try {
          const item = await getSourceRevision(id, cid)
          remember(item)
        } catch {
          if (cid === props.conversationId)
            metadata.value = {
              ...metadata.value,
              [id]: {
                source_id: '',
                revision_id: id,
                title: '资料暂不可读取，选择仍保留',
                version: 0,
                media_type: '',
                kind: '',
              },
            }
        }
      })
  )
}
async function refresh() {
  const cid = props.conversationId,
    token = ++generation
  loading.value = true
  try {
    const items: SourceRevision[] = []
    let offset: number | null = 0
    while (offset !== null) {
      const page = await listSources(cid, offset)
      items.push(...page.items)
      offset = page.next_offset
    }
    if (cid !== props.conversationId || token !== generation) return
    records.value = items
    for (const item of items) remember(item)
    await loadSelected()
    error.value = ''
  } catch (err: any) {
    if (cid === props.conversationId && token === generation) {
      records.value = []
      error.value = err?.response?.status === 404 && !props.sourceIds.length ? '' : sourceError(err)
    }
  } finally {
    if (token === generation) loading.value = false
  }
}
function show() {
  opened.value = true
  void refresh()
}
function pick() {
  picker.value?.click()
}
function attach(item: SourceRevision, cid = props.conversationId) {
  remember(item)
  const state = conversationState(cid)
  const previous = normalizeSourceIds(state.value.value.source_revision_ids)
  if (!previous.includes(item.revision_id) && previous.length >= MAX_SOURCE_ATTACHMENTS) {
    error.value = `每轮最多选择 ${MAX_SOURCE_ATTACHMENTS} 份资料；已上传原件保留在资料列表中。`
    return
  }
  // Persist the selection even if navigation unmounts this component before
  // the upload response arrives. The parent event only updates the visible draft.
  state.update({
    ...state.value.value,
    source_revision_ids: normalizeSourceIds([...previous, item.revision_id]),
  })
  void state.flush()
  emit('attach', { conversationId: cid, revisionId: item.revision_id })
}
async function uploadFiles(files: { file: File; requestId: string }[], cid = props.conversationId) {
  beginBusy(cid)
  error.value = ''
  try {
    await ensureSourceConversation(cid)
    for (const item of files) {
      try {
        const record = await uploadSource(item.file, cid, item.requestId)
        failed.value = {
          ...failed.value,
          [cid]: (failed.value[cid] || []).filter((entry) => entry.requestId !== item.requestId),
        }
        attach(record, cid)
        if (cid === props.conversationId)
          records.value = [
            record,
            ...records.value.filter((entry) => entry.source_id !== record.source_id),
          ]
      } catch (err) {
        const message = sourceError(err)
        const previous = (failed.value[cid] || []).filter(
          (entry) => entry.requestId !== item.requestId
        )
        failed.value = { ...failed.value, [cid]: [...previous, { ...item, error: message }] }
        if (cid === props.conversationId) error.value = message
      }
    }
  } catch (err) {
    const message = sourceError(err)
    failed.value = {
      ...failed.value,
      [cid]: [
        ...(failed.value[cid] || []).filter(
          (entry) => !files.some((item) => item.requestId === entry.requestId)
        ),
        ...files.map((item) => ({ ...item, error: message })),
      ],
    }
    if (cid === props.conversationId) error.value = message
  } finally {
    endBusy(cid)
  }
}
function picked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = [...(input.files || [])].map((file) => ({
    file,
    requestId: `upload_${crypto.randomUUID()}`,
  }))
  input.value = ''
  if (files.length) void uploadFiles(files)
}
async function captureProjectFiles() {
  if (!props.projectId || !props.projectFiles.length) return
  const cid = props.conversationId,
    projectId = props.projectId,
    revision = props.workspaceRevision
  const files = [...props.projectFiles]
  beginBusy(cid)
  error.value = ''
  try {
    await ensureSourceConversation(cid)
    for (const path of files) {
      const item = await captureProjectSource({
        conversation_id: cid,
        request_id: `project_${crypto.randomUUID()}`,
        project_id: projectId,
        workspace_revision: revision,
        path,
      })
      attach(item, cid)
      const state = conversationState(cid)
      state.update({
        ...state.value.value,
        file_paths: (state.value.value.file_paths || []).filter(
          (selectedPath) => selectedPath !== path
        ),
      })
      void state.flush()
      emit('captured', { conversationId: cid, path })
      if (cid === props.conversationId)
        records.value = [
          item,
          ...records.value.filter((entry) => entry.source_id !== item.source_id),
        ]
    }
  } catch (err) {
    if (cid === props.conversationId) error.value = sourceError(err)
  } finally {
    endBusy(cid)
  }
}
function read(item: SourceRevision) {
  void router.push(sourceReaderLocation(item.revision_id, props.conversationId))
}
function sourceUpdated(item: SourceRevision) {
  remember(item)
  void refresh()
}
function toggleAttachment(item: SourceRevision, event: Event) {
  const input = event.target as HTMLInputElement
  if (input.checked) {
    attach(item)
    input.checked = normalizeSourceIds(
      conversationState(props.conversationId).value.value.source_revision_ids
    ).includes(item.revision_id)
  } else emit('remove', item.revision_id)
}
function toggleLibrary(id: string, checked: boolean) {
  librarySelection.value = checked
    ? [...new Set([...librarySelection.value, id])]
    : librarySelection.value.filter((value) => value !== id)
}
async function importSelected() {
  const cid = props.conversationId,
    ids = [...librarySelection.value]
  if (!ids.length) return
  beginBusy(cid)
  error.value = ''
  try {
    const signature = JSON.stringify([cid, [...ids].sort()])
    let requestId = importRequests.get(signature)
    if (!requestId) {
      requestId = `library_${crypto.randomUUID()}`
      importRequests.set(signature, requestId)
    }
    const result = await importSourcesToLibrary(cid, ids, requestId)
    if (cid !== props.conversationId) return
    for (const item of result.items) {
      const ok = Boolean(item.paper_id) && !item.error
      importStatus.value[item.revision_id] = ok ? '已加入文献库' : item.error || '未导入，请重试'
      if (ok)
        librarySelection.value = librarySelection.value.filter((id) => id !== item.revision_id)
    }
    window.dispatchEvent(new Event('scholarflow:library-updated'))
  } catch (err) {
    if (cid === props.conversationId) error.value = sourceError(err)
  } finally {
    endBusy(cid)
  }
}
defineExpose({ show, pick, captureProjectFiles })
</script>

<template>
  <div class="conversation-sources" data-testid="conversation-sources">
    <input
      ref="picker"
      type="file"
      multiple
      accept=".pdf,.txt,.md,.markdown,.csv,.docx,.xlsx"
      hidden
      @change="picked"
    />
    <div v-if="selected.length" class="source-attachments" aria-label="待发送资料">
      <div v-for="item in selected" :key="item.revision_id" class="source-attachment">
        <button
          class="source-attachment-open"
          type="button"
          :title="`阅读 ${item.title}`"
          @click="read(item)"
        >
          <FileText :size="13" /><span>{{ item.title }}</span
          ><small v-if="item.version">v{{ item.version }}</small>
        </button>
        <button
          type="button"
          :aria-label="`移除待发送资料 ${item.title}`"
          :disabled="disabled"
          @click="emit('remove', item.revision_id)"
        >
          <X :size="12" />
        </button>
      </div>
      <button type="button" class="source-manage" @click="show">
        管理资料 · {{ selected.length }}/{{ MAX_SOURCE_ATTACHMENTS }}
      </button>
    </div>
    <p v-if="busyCount" class="attachment-notice" role="status">正在保存资料原件，请稍候…</p>
    <p v-if="error && !opened" class="attachment-error" role="alert">
      {{ error }} <button type="button" @click="show">查看资料</button>
    </p>
    <div v-if="failedHere.length && !opened" class="attachment-error" role="alert">
      {{ failedHere.length }} 份资料尚未保存，原选择保留。<button
        type="button"
        :disabled="!!busyCount"
        @click="uploadFiles([...failedHere])"
      >
        重试上传
      </button>
    </div>
    <Teleport to="body">
      <div
        v-if="opened"
        class="source-dialog-mask"
        @click.self="opened = false"
        @keydown.esc="opened = false"
      >
        <section
          ref="dialog"
          class="source-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="source-dialog-title"
          tabindex="-1"
          @keydown="dialogKeys"
        >
          <header>
            <div>
              <h2 id="source-dialog-title">会话资料</h2>
              <p>选择本轮使用的固定版本。上传与加入文献库分别操作。</p>
            </div>
            <button type="button" aria-label="关闭会话资料" @click="opened = false">
              <X :size="18" />
            </button>
          </header>
          <div class="source-dialog-actions">
            <button type="button" :disabled="disabled || !!busyCount" @click="pick">
              <Upload :size="15" />上传资料（添加为新来源）
            </button>
            <button
              v-if="projectId && projectFiles.length"
              type="button"
              :disabled="disabled || !!busyCount"
              @click="captureProjectFiles"
            >
              <FolderOpen :size="15" />保存所选项目文件的固定版本（添加为新资料） ·
              {{ projectFiles.length }}
            </button>
            <button type="button" :disabled="loading" @click="refresh">刷新</button>
          </div>
          <p v-if="error" class="attachment-error" role="alert">{{ error }}</p>
          <div v-for="item in failedHere" :key="item.requestId" class="source-upload-failed">
            <span>{{ item.file.name }}：{{ item.error }}</span
            ><button type="button" :disabled="!!busyCount" @click="uploadFiles([item])">
              重试
            </button>
          </div>
          <ProviderFilesPanel :conversation-id="conversationId" :sources="visibleRecords" />
          <div class="source-dialog-list">
            <p v-if="loading" role="status">正在读取会话资料…</p>
            <p v-else-if="!visibleRecords.length">
              尚未添加资料。支持 PDF、TXT、Markdown（.md / .markdown）、CSV、Word 和 Excel。
            </p>
            <article
              v-for="item in visibleRecords"
              :key="item.revision_id"
              class="source-record"
              :data-revision-id="item.revision_id"
            >
              <label class="source-record-select"
                ><input
                  type="checkbox"
                  :checked="sourceIds.includes(item.revision_id)"
                  :disabled="disabled"
                  :aria-label="`本轮使用 ${item.title}`"
                  @change="toggleAttachment(item, $event)"
                /><span
                  ><strong>{{ item.title }}</strong
                  ><small
                    >{{ sourceTypeLabel(item.media_type) }} · 固定版本 v{{ item.version }}</small
                  ></span
                ></label
              >
              <button type="button" @click="read(item)">阅读 <ArrowUpRight :size="13" /></button>
              <label class="library-choice"
                ><input
                  type="checkbox"
                  :checked="librarySelection.includes(item.revision_id)"
                  :disabled="!!busyCount"
                  :aria-label="`选择加入文献库 ${item.title}`"
                  @change="
                    toggleLibrary(item.revision_id, ($event.target as HTMLInputElement).checked)
                  "
                />加入文献库</label
              >
              <span
                v-if="importStatus[item.revision_id]"
                class="source-import-result"
                role="status"
                >{{ importStatus[item.revision_id] }}</span
              >
              <SourceVersions
                :record="item"
                :conversation-id="conversationId"
                :disabled="disabled || !!busyCount"
                :project-id="projectId"
                :workspace-revision="workspaceRevision"
                allow-select
                @updated="sourceUpdated"
                @select="attach($event)"
              />
            </article>
          </div>
          <footer>
            <span>{{ sourceIds.length }} 份待发送资料；原件不会自动加入文献库。</span
            ><button
              type="button"
              :disabled="!librarySelection.length || !!busyCount"
              @click="importSelected"
            >
              <BookOpen :size="15" />将所选 {{ librarySelection.length }} 份加入文献库</button
            ><button type="button" @click="opened = false">完成</button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.source-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 16px;
}
.source-attachment {
  display: flex;
  align-items: center;
  border: 1px solid #dbe2f3;
  border-radius: 6px;
  background: #f7f9ff;
  max-width: 100%;
}
.source-attachment button,
.source-manage {
  border: 0;
  background: transparent;
  color: #365483;
  cursor: pointer;
}
.source-attachment-open {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  padding: 6px;
}
.source-attachment-open span {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-attachment small {
  color: #7183a4;
}
.source-attachment > button:last-child {
  padding: 5px;
}
.source-manage {
  font-size: 11px;
}
.attachment-notice,
.attachment-error {
  margin: 6px 16px;
  font-size: 12px;
  line-height: 1.6;
}
.attachment-notice {
  color: #657c9f;
}
.attachment-error {
  color: #a33c35;
}
.attachment-error button {
  color: inherit;
  text-decoration: underline;
  border: 0;
  background: transparent;
  cursor: pointer;
}
.source-dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 2100;
  background: #16305640;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.source-dialog {
  width: 760px;
  max-width: 100%;
  max-height: 85vh;
  background: #fff;
  border: 1px solid #dce3f2;
  border-radius: 12px;
  box-shadow: 0 12px 50px #14305b2b;
  display: flex;
  flex-direction: column;
  color: #203c65;
}
.source-dialog header {
  padding: 20px;
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid #e4e9f4;
  gap: 16px;
}
.source-dialog h2 {
  font-size: 17px;
  margin: 0 0 6px;
}
.source-dialog p {
  font-size: 12px;
  margin: 0;
  color: #7183a4;
}
.source-dialog button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 1px solid #dce3f2;
  background: #fff;
  border-radius: 6px;
  padding: 7px 10px;
  color: #365483;
  font-size: 12px;
  cursor: pointer;
}
.source-dialog button:disabled {
  opacity: 0.5;
  cursor: default;
}
.source-dialog header > button {
  border: 0;
  align-self: flex-start;
  padding: 2px;
}
.source-dialog-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 14px 20px;
}
.source-dialog-list {
  padding: 0 20px 20px;
  overflow: auto;
  min-height: 90px;
}
.source-record {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 0;
  border-bottom: 1px solid #e9edf5;
}
.source-record-select {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 180px;
  flex: 1;
}
.source-record-select span {
  display: flex;
  flex-direction: column;
  gap: 5px;
  overflow-wrap: anywhere;
}
.source-record-select strong {
  font-size: 13px;
  font-weight: 500;
}
.source-record-select small {
  font-size: 11px;
  color: #7183a4;
}
.library-choice {
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.source-import-result {
  width: 100%;
  font-size: 11px;
  color: #667b9f;
}
.source-dialog footer {
  border-top: 1px solid #e4e9f4;
  padding: 14px 20px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.source-dialog footer span {
  font-size: 11px;
  flex: 1;
  color: #7183a4;
}
.source-upload-failed {
  display: flex;
  justify-content: space-between;
  padding: 8px 20px;
  font-size: 12px;
  color: #a33c35;
}
.source-dialog :focus-visible {
  outline: 2px solid #5266f5;
  outline-offset: 2px;
}
</style>
