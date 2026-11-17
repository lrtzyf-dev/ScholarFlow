<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { isAxiosError } from 'axios'
import {
  compactConversation,
  cancelCompaction,
  getConversationMemory,
  type ConversationMemory,
} from '../api'
import { getMemoryItems, editMemoryItem, type MemoryItems, type MemoryItem } from '../memoryApi'

const props = defineProps<{ conversationId: string; archived: boolean }>()
const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ source: [id: string] }>()
const state = ref<ConversationMemory | null>(null)
const items = ref<MemoryItems | null>(null)
const offset = ref(0),
  editing = ref(''),
  editText = ref(''),
  mutating = ref('')
const pendingDelete = ref('')
const loading = ref(false),
  saving = ref(false),
  stopping = ref(false)
const error = ref(''),
  notice = ref('')
let epoch = 0,
  loadRequest = 0,
  timer: ReturnType<typeof setTimeout> | undefined
const busy = computed(() => saving.value || state.value?.busy)
const modeLabel = computed(
  () =>
    ({
      model: '模型摘要',
      extractive: '离线摘录',
      degraded: '模型失败后的摘录',
      mixed: '混合摘要',
    })[state.value?.mode || ''] || '尚未压缩'
)
const readableSummary = computed(() => {
  const sources = state.value?.sources || []
  const label = (id: string) => {
    const index = sources.findIndex((item) => item.id === id)
    return index < 0 ? '历史来源' : `来源 ${index + 1}`
  }
  return (state.value?.summary || '')
    .replace(/\[历史记录:([^\]]+)\]/g, (_, id) => `（${label(id)}）`)
    .replace(
      /\[历史证据:([^:\]]+):([^\]]+)\]/g,
      (_, id, evidence) => `（${label(id)} · ${evidence}）`
    )
})

function reset() {
  epoch++
  clearTimeout(timer)
  state.value = null
  items.value = null
  offset.value = 0
  editing.value = editText.value = mutating.value = pendingDelete.value = ''
  loading.value = saving.value = stopping.value = false
  error.value = notice.value = ''
}
function updatedTime(iso: string) {
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`).toLocaleString()
}
function blockAppShortcuts(event: KeyboardEvent) {
  if (event.ctrlKey || event.metaKey) event.stopPropagation()
}
function memoryError(value: unknown, fallback: string) {
  const detail = isAxiosError(value) ? value.response?.data?.detail : null
  return typeof detail === 'string' ? detail : fallback
}
async function load() {
  const generation = epoch,
    cid = props.conversationId,
    request = ++loadRequest
  loading.value = true
  error.value = ''
  try {
    const [result, governance] = await Promise.all([
      getConversationMemory(cid),
      getMemoryItems(cid, offset.value),
    ])
    if (generation !== epoch || request !== loadRequest) return
    state.value = result
    items.value = governance
    if (!result.busy && !saving.value) stopping.value = false
    clearTimeout(timer)
    if (result.busy && visible.value) timer = setTimeout(() => void load(), 2000)
  } catch (e: unknown) {
    if (generation === epoch && request === loadRequest)
      error.value = memoryError(e, '读取记忆失败，请重试')
  } finally {
    if (generation === epoch && request === loadRequest) loading.value = false
  }
}
function stateLabel(value: string) {
  return (
    { included: '参与记忆', excluded: '已排除', deleted: '已删除记忆', not_captured: '未加入记忆' }[
      value
    ] || value
  )
}
function beginEdit(item: MemoryItem) {
  editing.value = item.id
  editText.value = item.text
  pendingDelete.value = ''
}
async function changeItem(item: MemoryItem, action: 'edit' | 'include' | 'exclude' | 'delete') {
  if (!items.value || mutating.value || props.archived) return
  const generation = epoch,
    cid = props.conversationId,
    revision = items.value.revision
  const text = editText.value
  mutating.value = item.id
  error.value = notice.value = ''
  try {
    await editMemoryItem(cid, item.id, revision, action, action === 'edit' ? text : undefined)
    if (generation !== epoch) return
    editing.value = pendingDelete.value = ''
    notice.value = '记忆已更新，原始聊天保留。后续模型请求将使用更新后的记忆。'
    await load()
  } catch (e: unknown) {
    if (generation !== epoch) return
    await load()
    if (generation !== epoch) return
    error.value = memoryError(e, '记忆操作未确认，请重新载入后重试')
    // Keep the user's unsaved replacement across a concurrent-window conflict.
    if (action === 'edit') {
      editing.value = item.id
      editText.value = text
    }
  } finally {
    if (generation === epoch) mutating.value = ''
  }
}
async function pageItems(next: number) {
  if (loading.value || mutating.value) return
  offset.value = next
  editing.value = pendingDelete.value = ''
  await load()
}
async function update(force = false) {
  if (!state.value || busy.value || loading.value || props.archived) return
  const generation = epoch,
    cid = props.conversationId,
    revision = state.value.revision
  clearTimeout(timer)
  saving.value = true
  stopping.value = false
  error.value = notice.value = ''
  try {
    await compactConversation(cid, revision, force)
    if (generation !== epoch) return
    notice.value = '摘要已保存在本机，原始聊天保持完整。'
    await load()
  } catch (e: unknown) {
    if (generation !== epoch) return
    await load()
    if (generation !== epoch) return
    error.value = memoryError(e, '更新请求未确认，请先重新载入状态再重试')
  } finally {
    if (generation === epoch) {
      saving.value = false
      stopping.value = false
    }
  }
}
async function stop() {
  const generation = epoch
  stopping.value = true
  try {
    await cancelCompaction(props.conversationId)
  } catch {
    if (generation === epoch) {
      error.value = '停止请求失败，请重试'
      stopping.value = false
    }
  }
}
async function open(updateNow = false) {
  reset()
  visible.value = true
  const generation = epoch
  await nextTick()
  if (generation !== epoch) return
  await load()
  if (generation === epoch && updateNow && state.value?.pending_turns) await update()
}
// 选择某条历史来源：关闭弹窗并通知父组件定位
function pickSource(id: string) {
  visible.value = false
  emit('source', id)
}
defineExpose({ open })
watch(
  () => props.conversationId,
  () => {
    visible.value = false
    reset()
  }
)
watch(visible, (value) => {
  if (!value) reset()
})
onUnmounted(reset)
</script>

<template>
  <ElDialog
    v-model="visible"
    title="会话记忆"
    width="min(760px, calc(100vw - 64px))"
    top="8vh"
    class="memory-dialog"
    :close-on-click-modal="false"
    @keydown="blockAppShortcuts"
  >
    <div class="memory-content" :aria-busy="loading || saving">
      <p class="memory-intro">
        逐条查看和调整模型可使用的历史。排除或删除记忆不会删除原始聊天；较早对话保存为摘要，最近四轮使用原文。
      </p>
      <p v-if="error" class="memory-error" role="alert">
        {{ error }}
        <button type="button" :disabled="loading || saving" @click="load">重新载入</button>
      </p>
      <p v-if="loading && !state" role="status">正在读取本机摘要…</p>
      <section v-if="items" class="memory-governance" aria-label="逐条记忆管理">
        <details class="memory-loads" open>
          <summary>最近任务实际载入的记忆</summary>
          <p v-if="!items.loads.length" class="memory-empty">本会话尚无任务载入回执。</p>
          <details
            v-for="(receipt, index) in items.loads"
            :key="receipt.run_id"
            class="memory-load"
          >
            <summary>
              {{ index === 0 ? '最近任务' : `历史任务 ${index + 1}` }} ·
              {{ updatedTime(receipt.updated_at) }} · {{ receipt.entries.length }} 条
              <span>{{ receipt.stale ? '记忆已变更，旧回执已失效' : '与当前记忆一致' }}</span>
            </summary>
            <p class="memory-cost">载入版本 {{ receipt.generation }} · 任务 {{ receipt.run_id }}</p>
            <p v-if="!receipt.entries.length">本次没有载入历史记忆。</p>
            <div class="memory-source-list">
              <button
                v-for="(entry, n) in receipt.entries"
                :key="entry.id"
                type="button"
                @click="pickSource(entry.id)"
              >
                <span
                  >聊天记录 {{ n + 1 }} ·
                  {{ items.items.find((row) => row.id === entry.id)?.question || entry.id }}</span
                >
                <small
                  >{{ entry.mode === 'summary' ? '摘要' : '原文'
                  }}{{ entry.edited ? ' · 已编辑' : '' }}</small
                >
              </button>
            </div>
          </details>
        </details>
        <div class="memory-summary-head">
          <strong>记忆条目（{{ items.total }} 条聊天）</strong
          ><span>策略版本 {{ items.revision }}</span>
        </div>
        <p v-if="!items.items.length" class="memory-empty">暂无可管理的历史聊天。</p>
        <article
          v-for="item in items.items"
          :key="item.id"
          class="memory-item"
          :data-memory-id="item.id"
        >
          <div class="memory-item-head">
            <button type="button" class="memory-original" @click="pickSource(item.id)">
              {{ item.question || '查看原始聊天' }}</button
            ><small>{{ stateLabel(item.state) }}{{ item.edited ? ' · 已编辑' : '' }}</small>
          </div>
          <template v-if="editing === item.id">
            <label :for="`memory-edit-${item.id}`">用于后续回答的记忆文本</label>
            <textarea
              :id="`memory-edit-${item.id}`"
              v-model="editText"
              rows="6"
              :disabled="!!mutating"
            />
            <p class="memory-cost">
              {{ editText.length }} / 12000 字。此处修改仅影响记忆，原始聊天不变。
            </p>
            <div class="memory-item-actions">
              <button
                type="button"
                :disabled="!!mutating || !editText.trim() || editText.length > 12000 || archived"
                @click="changeItem(item, 'edit')"
              >
                保存记忆</button
              ><button type="button" :disabled="!!mutating" @click="editing = ''">取消编辑</button>
            </div>
          </template>
          <template v-else>
            <details v-if="item.text">
              <summary>查看记忆内容</summary>
              <pre class="memory-summary">{{ item.text }}</pre>
            </details>
            <p v-else class="memory-cost">已从记忆移除。你仍可查看原始聊天，也可明确重新加入。</p>
            <div class="memory-item-actions">
              <button
                v-if="item.state !== 'deleted'"
                type="button"
                :disabled="!!mutating || archived"
                @click="beginEdit(item)"
              >
                编辑记忆
              </button>
              <button
                v-if="item.state === 'included'"
                type="button"
                :disabled="!!mutating || archived"
                @click="changeItem(item, 'exclude')"
              >
                排除记忆
              </button>
              <button
                v-else
                type="button"
                :disabled="!!mutating || archived"
                @click="changeItem(item, 'include')"
              >
                重新加入记忆
              </button>
              <button
                v-if="item.state !== 'deleted'"
                type="button"
                :disabled="!!mutating || archived"
                @click="pendingDelete = item.id"
              >
                删除记忆
              </button>
            </div>
            <div
              v-if="pendingDelete === item.id"
              class="memory-delete-confirm"
              role="group"
              aria-label="确认删除记忆"
            >
              <span>删除此条记忆，保留原始聊天。摘要及后续请求将撤回该条内容。</span>
              <button
                type="button"
                :disabled="!!mutating || archived"
                @click="changeItem(item, 'delete')"
              >
                确认删除记忆</button
              ><button type="button" :disabled="!!mutating" @click="pendingDelete = ''">
                取消
              </button>
            </div>
          </template>
        </article>
        <div v-if="items.total > 20" class="memory-pagination">
          <button
            type="button"
            :disabled="offset === 0 || loading || !!mutating"
            @click="pageItems(Math.max(0, offset - 20))"
          >
            上一页</button
          ><span>{{ offset + 1 }}–{{ Math.min(offset + 20, items.total) }} / {{ items.total }}</span
          ><button
            type="button"
            :disabled="items.next_offset === null || loading || !!mutating"
            @click="pageItems(items.next_offset!)"
          >
            下一页
          </button>
        </div>
      </section>
      <template v-if="state">
        <details class="memory-compaction">
          <summary>历史摘要与用量</summary>
          <div class="memory-stats">
            <span>{{ state.covered_turns }} 轮已压缩</span
            ><span>{{ state.pending_turns }} 轮待压缩</span
            ><span>{{ state.recent_turns }} 轮原文</span>
          </div>
          <p v-if="!state.recall_enabled" class="memory-note">
            使用记忆已关闭：可整理摘要，但后续回答不会载入它，也不能调用历史回查工具。
          </p>
          <p v-if="state.stale" class="memory-note">
            原始历史已变化，旧摘要已停用；更新后重新生成。
          </p>
          <p v-if="state.mode && state.mode !== 'model'" class="memory-note">
            {{ modeLabel }}：包含截取内容，可能省略重要细节。接入模型后可重新生成。
          </p>
          <div class="memory-summary-head">
            <strong>{{ modeLabel }}</strong
            ><span v-if="state.updated_at">{{ updatedTime(state.updated_at) }}</span>
          </div>
          <pre v-if="state.summary" class="memory-summary">{{ readableSummary }}</pre>
          <p v-else class="memory-empty">
            {{
              state.total_turns <= 4
                ? '当前可用历史不超过四轮，保留原文即可。'
                : '还没有可用摘要。压缩后会保存在本机，供后续回答复用。'
            }}
          </p>
          <details v-if="state.sources.length" class="memory-sources">
            <summary>回查来源（{{ state.sources.length }} 轮）</summary>
            <div class="memory-source-list">
              <button
                v-for="(source, index) in state.sources"
                :key="source.id"
                type="button"
                @click="pickSource(source.id)"
              >
                <span>{{ index + 1 }}. {{ source.question || '未命名问题' }}</span
                ><small>{{ source.evidence_count }} 条证据</small>
              </button>
            </div>
          </details>
          <p class="memory-cost">
            摘要调用 {{ state.usage.calls }} 次 · 已记录
            {{ state.usage.usage.total_tokens }} tokens（{{
              state.usage.provider_calls
            }}
            次服务商返回，{{ state.usage.estimated_calls }} 次估算，{{ state.usage.unknown_calls }}
            次用量未知）。离线摘录不调用模型。
          </p>
        </details>
      </template>
      <p v-if="notice" role="status">{{ notice }}</p>
      <p v-if="busy" role="status">
        {{
          stopping
            ? '正在停止，等待当前模型请求返回…'
            : '正在更新摘要，可关闭面板后继续使用；已完成批次会保存。'
        }}
      </p>
    </div>
    <template #footer>
      <div class="memory-actions">
        <button type="button" :disabled="loading || !!mutating" @click="load">刷新记忆</button>
        <button v-if="busy" type="button" :disabled="stopping" @click="stop">
          {{ stopping ? '正在停止…' : '停止更新' }}
        </button>
        <button
          v-if="state?.summary"
          type="button"
          :disabled="busy || loading || !!mutating || archived"
          @click="update(true)"
        >
          重新生成摘要
        </button>
        <button
          type="button"
          class="memory-primary"
          :disabled="!state?.pending_turns || busy || loading || !!mutating || archived"
          @click="update()"
        >
          {{ archived ? '请先恢复会话' : '压缩较早对话' }}
        </button>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.memory-content {
  max-height: 63vh;
  overflow-y: auto;
  color: var(--fn-text-primary);
  line-height: 1.65;
}
.memory-intro,
.memory-cost {
  color: var(--fn-text-secondary);
}
.memory-intro {
  margin-top: 0;
}
.memory-stats {
  display: flex;
  gap: 24px;
  padding: 12px 0;
  border-block: 1px solid var(--fn-border-primary);
  font-variant-numeric: tabular-nums;
}
.memory-note {
  padding: 10px 12px;
  background: var(--fn-bg-secondary, #f7f7f8);
  border-left: 3px solid var(--fn-brand);
}
.memory-summary-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-top: 16px;
}
.memory-summary-head span,
.memory-cost {
  font-size: 12px;
}
.memory-summary {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
}
.memory-error {
  color: #b42318;
}
.memory-empty {
  color: var(--fn-text-secondary);
  padding-block: 20px;
}
.memory-sources summary {
  cursor: pointer;
  padding: 8px 0;
}
.memory-source-list {
  max-height: 180px;
  overflow-y: auto;
}
.memory-source-list button {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  text-align: left;
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  padding: 8px;
  background: transparent;
}
.memory-source-list button span {
  overflow-wrap: anywhere;
}
.memory-source-list small {
  flex-shrink: 0;
  color: var(--fn-text-secondary);
}
.memory-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
button {
  font: inherit;
  color: inherit;
  cursor: pointer;
}
.memory-actions button,
.memory-error button,
.memory-item-actions button,
.memory-delete-confirm button,
.memory-pagination button {
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  background: white;
  padding: 7px 12px;
}
.memory-loads > summary,
.memory-compaction > summary {
  padding: 10px 0;
  cursor: pointer;
  font-weight: 600;
}
.memory-load {
  border-top: 1px solid var(--fn-border-primary);
  padding: 8px 0;
}
.memory-load summary {
  cursor: pointer;
}
.memory-load summary span {
  display: block;
  color: var(--fn-text-secondary);
  font-size: 12px;
}
.memory-item {
  padding: 14px 0;
  border-bottom: 1px solid var(--fn-border-primary);
}
.memory-item-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.memory-item-head small {
  flex-shrink: 0;
  color: var(--fn-text-secondary);
}
.memory-original {
  border: 0;
  padding: 0;
  text-align: left;
  background: none;
  overflow-wrap: anywhere;
  color: var(--fn-brand);
}
.memory-item details > summary {
  cursor: pointer;
  padding: 8px 0;
}
.memory-item pre {
  max-height: 240px;
  overflow-y: auto;
}
.memory-item-actions,
.memory-pagination {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.memory-pagination {
  justify-content: space-between;
  align-items: center;
}
.memory-item label {
  display: block;
  margin-top: 12px;
}
.memory-item textarea {
  box-sizing: border-box;
  width: 100%;
  margin-top: 6px;
  padding: 8px;
  resize: vertical;
  font: inherit;
  color: inherit;
  background: var(--fn-bg-primary, white);
  border: 1px solid var(--fn-border-primary);
}
.memory-delete-confirm {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
  padding: 10px;
  background: var(--fn-bg-secondary, #f7f7f8);
}
.memory-delete-confirm span {
  flex-basis: 100%;
}
@media (max-width: 600px) {
  .memory-item-head {
    display: block;
  }
  .memory-item-head small {
    display: block;
  }
  .memory-actions {
    flex-wrap: wrap;
  }
}
.memory-actions .memory-primary {
  background: var(--fn-brand);
  color: white;
  border-color: var(--fn-brand);
}
button:disabled {
  opacity: 0.55;
  cursor: default;
}
button:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
</style>
