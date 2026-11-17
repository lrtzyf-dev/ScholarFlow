<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ensureSourceConversation, getLibraryVersions, sourceError } from '../sourceApi'
import {
  buildLibraryIndex,
  cancelLibraryIndex,
  getLibraryIndex,
  getLibraryIndexBuild,
  resumeLibraryIndex,
  selectLibraryPrimary,
  type IndexBuild,
  type LibraryIndexOperation,
  type LibraryIndexState,
} from '../libraryIndexApi'

const props = defineProps<{ paperId: string; conversationId: string }>()
const emit = defineEmits<{ open: [revisionId: string]; changed: [] }>()
const versions = ref<Awaited<ReturnType<typeof getLibraryVersions>>['items']>([])
const state = ref<LibraryIndexState | null>(null),
  operation = ref<LibraryIndexOperation | null>(null)
const selected = ref(''),
  mode = ref<'text' | 'semantic'>('text'),
  setPrimary = ref(false)
const busy = ref(false),
  error = ref(''),
  loading = ref(false)
let generation = 0,
  scopeEpoch = 0,
  timer: ReturnType<typeof setTimeout> | undefined,
  request: AbortController | undefined
let pending: IndexBuild | null = null
const running = computed(
  () => !!operation.value && ['queued', 'preparing'].includes(operation.value.status)
)
const resumable = computed(
  () => !!operation.value && ['prepared', 'partial', 'interrupted'].includes(operation.value.status)
)
const canStart = computed(
  () => !!state.value && !!selected.value && !busy.value && !running.value && !resumable.value
)
const labels: Record<string, string> = {
  queued: '等待准备',
  preparing: '正在准备',
  prepared: '结果已保存，等待提交',
  partial: '部分批次已保存',
  interrupted: '尚未提交，可继续',
  done: '已提交',
  cancelled: '已取消',
}
function versionLabel(id: string | null) {
  const index = versions.value.findIndex((item) => item.revision_id === id)
  return index < 0 ? '旧索引，解析版本未知' : `固定版本 ${index + 1}`
}
function stop() {
  if (timer) clearTimeout(timer)
  request?.abort()
}
async function load(initial = false) {
  const token = ++generation
  stop()
  request = new AbortController()
  const paperId = props.paperId,
    conversationId = props.conversationId
  if (initial) {
    loading.value = true
    error.value = ''
  }
  try {
    await ensureSourceConversation(conversationId)
    if (token !== generation) return
    const fixed = await getLibraryVersions(paperId, conversationId)
    if (token !== generation) return
    versions.value = fixed.items
    const value = await getLibraryIndex(paperId, conversationId, request.signal)
    if (token !== generation) return
    state.value = value
    if (!versions.value.some((item) => item.revision_id === selected.value))
      selected.value = value.primary_revision_id
    const latest = value.operations[0]
    const detail = latest
      ? await getLibraryIndexBuild(latest.id, conversationId, request.signal)
      : null
    if (token !== generation) return
    operation.value = detail
    if (running.value && !busy.value) timer = setTimeout(() => void load(), 1500)
  } catch (err) {
    const cancelled =
      !!err && typeof err === 'object' && 'code' in err && err.code === 'ERR_CANCELED'
    if (token === generation && !cancelled) error.value = sourceError(err)
  } finally {
    if (token === generation) loading.value = false
  }
}
async function mutate(action: () => Promise<unknown>, completed?: () => void) {
  if (busy.value) return
  const epoch = scopeEpoch
  busy.value = true
  error.value = ''
  stop()
  try {
    await action()
    if (epoch !== scopeEpoch) return
    completed?.()
    emit('changed')
  } catch (err) {
    if (epoch === scopeEpoch) error.value = sourceError(err)
  } finally {
    if (epoch === scopeEpoch) {
      busy.value = false
      await load()
    }
  }
}
function choosePrimary(id: string) {
  if (!state.value) return
  const value = state.value,
    paperId = props.paperId,
    conversationId = props.conversationId
  void mutate(() =>
    selectLibraryPrimary(paperId, {
      conversation_id: conversationId,
      revision_id: id,
      expected_version: value.version,
      expected_primary_id: value.primary_revision_id,
    })
  )
}
function build() {
  if (!canStart.value || !state.value) return
  const intent = {
    conversation_id: props.conversationId,
    revision_id: selected.value,
    expected_version: state.value.version,
    expected_primary_id: state.value.primary_revision_id,
    set_primary: setPrimary.value,
    index_mode: mode.value,
  }
  if (!pending || JSON.stringify({ ...pending, request_id: undefined }) !== JSON.stringify(intent))
    pending = { ...intent, request_id: crypto.randomUUID() }
  const body = pending,
    paperId = props.paperId
  void mutate(
    () => buildLibraryIndex(paperId, body),
    () => {
      pending = null
    }
  )
}
function resume() {
  if (!state.value || !operation.value) return
  const value = state.value,
    id = operation.value.id,
    conversationId = props.conversationId
  void mutate(() =>
    resumeLibraryIndex(id, {
      conversation_id: conversationId,
      expected_version: value.version,
      expected_primary_id: value.primary_revision_id,
    })
  )
}
function cancel() {
  if (!operation.value) return
  const id = operation.value.id,
    conversationId = props.conversationId
  void mutate(() => cancelLibraryIndex(id, conversationId))
}
watch(
  () => [props.paperId, props.conversationId],
  () => {
    scopeEpoch++
    state.value = null
    operation.value = null
    versions.value = []
    selected.value = ''
    pending = null
    busy.value = false
    error.value = ''
    void load(true)
  },
  { immediate: true }
)
onBeforeUnmount(() => {
  scopeEpoch++
  generation++
  stop()
})
</script>

<template>
  <section class="library-index-versions" aria-label="固定版本与检索索引">
    <p v-if="loading" role="status">正在核对固定版本与索引…</p>
    <template v-if="state">
      <dl class="version-summary">
        <div>
          <dt>默认阅读</dt>
          <dd>{{ versionLabel(state.primary_revision_id) }}</dd>
        </div>
        <div>
          <dt>当前检索</dt>
          <dd>{{ versionLabel(state.indexed_revision_id) }} · {{ state.chunk_count }} 段</dd>
        </div>
      </dl>
      <p class="version-hint">
        切换默认阅读不会重建检索。重建成功后统一更新索引，原有引用仍打开原固定版本。
      </p>
      <ul class="version-list">
        <li v-for="(item, index) in versions" :key="item.revision_id">
          <label
            ><input
              v-model="selected"
              type="radio"
              :name="`index-version-${paperId}`"
              :value="item.revision_id"
              :disabled="busy || running || resumable"
            />固定版本 {{ index + 1 }}</label
          >
          <span>{{ new Date(item.created_at).toLocaleDateString() }}</span>
          <span v-if="item.revision_id === state.primary_revision_id">默认阅读</span>
          <span v-if="item.revision_id === state.indexed_revision_id">当前检索</span>
          <button type="button" @click="emit('open', item.revision_id)">阅读此版</button>
          <button
            type="button"
            :disabled="busy || item.revision_id === state.primary_revision_id"
            @click="choosePrimary(item.revision_id)"
          >
            设为默认阅读
          </button>
        </li>
      </ul>
      <div class="index-controls">
        <label
          >检索方式
          <select v-model="mode" :disabled="busy || running || resumable">
            <option value="text">仅全文检索</option>
            <option value="semantic">全文与向量检索</option>
          </select></label
        >
        <label
          ><input
            v-model="setPrimary"
            type="checkbox"
            :disabled="busy || running || resumable"
          />同时设为默认阅读</label
        >
        <button type="button" :disabled="!canStart" @click="build">重建所选版本索引</button>
      </div>
      <p class="version-hint">
        {{
          mode === 'semantic'
            ? '使用当前 Embedding 配置；可能产生接口费用。已保存的批次会复用，模型或凭证变化时停止提交。'
            : '本地重建全文检索，不调用 Embedding 接口。'
        }}
      </p>
      <p
        v-if="state.index_mode === 'semantic' && !state.embedding_matches_current"
        class="index-error"
      >
        当前向量与 Embedding 配置不一致，检索仅使用全文匹配。可确认配置后重新准备。
      </p>
      <div v-if="operation" class="index-operation" aria-live="polite">
        <strong>{{ labels[operation.status] || operation.status }}</strong>
        <span v-if="operation.prepared_count"> · {{ operation.prepared_count }} 段原文已定位</span>
        <span v-if="operation.prepared_batches">
          · {{ operation.prepared_batches }} 个向量批次</span
        >
        <p v-if="operation.error" class="index-error">{{ operation.error }}</p>
        <p v-if="operation.usage" class="version-hint">
          本次记录 {{ operation.usage.call_count }} 次接口调用<span
            v-if="operation.usage.actual_usage.total_tokens != null"
            >，供应商回执 {{ operation.usage.actual_usage.total_tokens }} tokens</span
          >。{{
            operation.usage.call_count
              ? '费用以供应商账单及用量页为准；缺少回执时不按零费用显示。'
              : '尚无供应商接口用量记录。'
          }}
        </p>
        <button v-if="resumable" type="button" :disabled="busy" @click="resume">核对后继续</button>
        <button v-if="running || resumable" type="button" :disabled="busy" @click="cancel">
          取消此次重建
        </button>
      </div>
    </template>
    <p v-if="error" role="alert" class="index-error">{{ error }}</p>
    <button v-if="error" type="button" :disabled="busy || loading" @click="load(true)">
      重新核对
    </button>
  </section>
</template>

<style scoped>
.library-index-versions {
  padding: 14px 0;
  color: var(--text-primary, #202124);
  font-size: 13px;
  white-space: normal;
  min-width: 0;
}
.version-summary {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  margin: 0 0 10px;
}
.version-summary div {
  display: flex;
  gap: 8px;
}
.version-summary dt {
  color: var(--text-muted, #64748b);
}
.version-summary dd {
  margin: 0;
}
.version-hint {
  font-size: 12px;
  color: var(--text-muted, #64748b);
  line-height: 1.7;
  margin: 8px 0;
  overflow-wrap: anywhere;
}
.version-list {
  list-style: none;
  padding: 0;
  margin: 8px 0;
}
.version-list li {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 9px 0;
  border-top: 1px solid var(--border-color, #e5e7eb);
}
.version-list span {
  font-size: 12px;
  color: var(--text-muted, #64748b);
}
.version-list label,
.index-controls label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.index-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}
.index-controls select {
  max-width: 100%;
  padding: 4px;
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 4px;
  background: var(--bg-card, #fff);
  color: inherit;
}
button {
  font: inherit;
  padding: 5px 9px;
  border: 1px solid var(--border-color, #d1d5db);
  border-radius: 4px;
  background: var(--bg-card, #fff);
  color: inherit;
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
button:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 2px solid var(--primary-color, #3b6eea);
  outline-offset: 2px;
}
.index-operation {
  border-top: 1px solid var(--border-color, #e5e7eb);
  margin-top: 12px;
  padding-top: 10px;
}
.index-operation button + button {
  margin-left: 8px;
}
.index-error {
  color: var(--danger-color, #b42318);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
@media (max-width: 800px) {
  .version-summary {
    gap: 8px;
  }
  .index-controls {
    align-items: flex-start;
    flex-direction: column;
  }
  .version-list li {
    gap: 7px;
  }
}
</style>
