<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import { compactConversation, cancelCompaction, getConversationMemory, type ConversationMemory } from '../api'

const props = defineProps<{ conversationId: string; archived: boolean }>()
const visible = defineModel<boolean>({ default: false })
const emit = defineEmits<{ source: [id: string] }>()
const state = ref<ConversationMemory | null>(null)
const loading = ref(false), saving = ref(false), stopping = ref(false)
const error = ref(''), notice = ref('')
let epoch = 0, timer: ReturnType<typeof setTimeout> | undefined
const busy = computed(() => saving.value || state.value?.busy)
const modeLabel = computed(() => ({ model: '模型摘要', extractive: '离线摘录', degraded: '模型失败后的摘录', mixed: '混合摘要' }[state.value?.mode || ''] || '尚未压缩'))
const readableSummary = computed(() => {
  const sources = state.value?.sources || []
  const label = (id: string) => { const index = sources.findIndex((item) => item.id === id); return index < 0 ? '历史来源' : `来源 ${index + 1}` }
  return (state.value?.summary || '').replace(/\[历史记录:([^\]]+)\]/g, (_, id) => `（${label(id)}）`)
    .replace(/\[历史证据:([^:\]]+):([^\]]+)\]/g, (_, id, evidence) => `（${label(id)} · ${evidence}）`)
})

function reset() {
  epoch++
  clearTimeout(timer)
  state.value = null
  loading.value = saving.value = stopping.value = false
  error.value = notice.value = ''
}
function updatedTime(iso: string) {
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`).toLocaleString()
}
function blockAppShortcuts(event: KeyboardEvent) {
  if (event.ctrlKey || event.metaKey) event.stopPropagation()
}
async function load() {
  const generation = epoch, cid = props.conversationId
  loading.value = true
  error.value = ''
  try {
    const result = await getConversationMemory(cid)
    if (generation !== epoch) return
    state.value = result
    if (!result.busy && !saving.value) stopping.value = false
    clearTimeout(timer)
    if (result.busy && visible.value) timer = setTimeout(() => void load(), 2000)
  } catch (e: any) {
    if (generation === epoch) error.value = e?.response?.data?.detail || '读取摘要失败，请重试'
  } finally {
    if (generation === epoch) loading.value = false
  }
}
async function update(force = false) {
  if (!state.value || busy.value || loading.value || props.archived) return
  const generation = epoch, cid = props.conversationId, revision = state.value.revision
  clearTimeout(timer)
  saving.value = true
  stopping.value = false
  error.value = notice.value = ''
  try {
    await compactConversation(cid, revision, force)
    if (generation !== epoch) return
    notice.value = '摘要已保存在本机，原始聊天保持完整。'
    await load()
  } catch (e: any) {
    if (generation !== epoch) return
    await load()
    error.value = e?.response?.data?.detail || '更新请求未确认，请先重新载入状态再重试'
  } finally {
    if (generation === epoch) { saving.value = false; stopping.value = false }
  }
}
async function stop() {
  const generation = epoch
  stopping.value = true
  try { await cancelCompaction(props.conversationId) }
  catch { if (generation === epoch) { error.value = '停止请求失败，请重试'; stopping.value = false } }
}
async function open(updateNow = false) {
  reset()
  visible.value = true
  await nextTick()
  const generation = epoch
  await load()
  if (generation === epoch && updateNow && state.value?.pending_turns) await update()
}
defineExpose({ open })
watch(() => props.conversationId, () => { visible.value = false; reset() })
watch(visible, (value) => { if (!value) reset() })
onUnmounted(reset)
</script>

<template>
  <ElDialog v-model="visible" title="会话记忆" width="min(760px, calc(100vw - 64px))" top="8vh" class="memory-dialog" :close-on-click-modal="false" @keydown="blockAppShortcuts">
    <div class="memory-content" :aria-busy="loading || saving">
      <p class="memory-intro">较早对话保存为摘要，最近四轮保留原文。重要事实和引用可从来源回查；压缩不会删除聊天。</p>
      <p v-if="error" class="memory-error" role="alert">{{ error }} <button type="button" :disabled="loading || saving" @click="load">重新载入</button></p>
      <p v-if="loading && !state" role="status">正在读取本机摘要…</p>
      <template v-if="state">
        <div class="memory-stats"><span>{{ state.covered_turns }} 轮已压缩</span><span>{{ state.pending_turns }} 轮待压缩</span><span>{{ state.recent_turns }} 轮原文</span></div>
        <p v-if="!state.recall_enabled" class="memory-note">使用记忆已关闭：可整理摘要，但后续回答不会载入它，也不能调用历史回查工具。</p>
        <p v-if="state.stale" class="memory-note">原始历史已变化，旧摘要已停用；更新后重新生成。</p>
        <p v-if="state.mode && state.mode !== 'model'" class="memory-note">{{ modeLabel }}：包含截取内容，可能省略重要细节。接入模型后可重新生成。</p>
        <div class="memory-summary-head"><strong>{{ modeLabel }}</strong><span v-if="state.updated_at">{{ updatedTime(state.updated_at) }}</span></div>
        <pre v-if="state.summary" class="memory-summary">{{ readableSummary }}</pre>
        <p v-else class="memory-empty">{{ state.total_turns <= 4 ? '当前可用历史不超过四轮，保留原文即可。' : '还没有可用摘要。压缩后会保存在本机，供后续回答复用。' }}</p>
        <details v-if="state.sources.length" class="memory-sources">
          <summary>回查来源（{{ state.sources.length }} 轮）</summary>
          <div class="memory-source-list">
            <button v-for="(source, index) in state.sources" :key="source.id" type="button" @click="visible = false; emit('source', source.id)"><span>{{ index + 1 }}. {{ source.question || '未命名问题' }}</span><small>{{ source.evidence_count }} 条证据</small></button>
          </div>
        </details>
        <p class="memory-cost">摘要调用 {{ state.usage.calls }} 次 · 已记录 {{ state.usage.usage.total_tokens }} tokens（{{ state.usage.provider_calls }} 次服务商返回，{{ state.usage.estimated_calls }} 次估算，{{ state.usage.unknown_calls }} 次用量未知）。离线摘录不调用模型。</p>
      </template>
      <p v-if="notice" role="status">{{ notice }}</p>
      <p v-if="busy" role="status">{{ stopping ? '正在停止，等待当前模型请求返回…' : '正在更新摘要，可关闭面板后继续使用；已完成批次会保存。' }}</p>
    </div>
    <template #footer>
      <div class="memory-actions">
        <button v-if="busy" type="button" :disabled="stopping" @click="stop">{{ stopping ? '正在停止…' : '停止更新' }}</button>
        <button v-if="state?.summary" type="button" :disabled="busy || loading || archived" @click="update(true)">重新生成摘要</button>
        <button type="button" class="memory-primary" :disabled="!state?.pending_turns || busy || loading || archived" @click="update()">{{ archived ? '请先恢复会话' : '压缩较早对话' }}</button>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.memory-content { max-height: 63vh; overflow-y: auto; color: var(--fn-text-primary); line-height: 1.65; }
.memory-intro, .memory-cost { color: var(--fn-text-secondary); }
.memory-intro { margin-top: 0; }
.memory-stats { display: flex; gap: 24px; padding: 12px 0; border-block: 1px solid var(--fn-border-primary); font-variant-numeric: tabular-nums; }
.memory-note { padding: 10px 12px; background: var(--fn-bg-secondary, #f7f7f8); border-left: 3px solid var(--fn-brand); }
.memory-summary-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-top: 16px; }
.memory-summary-head span, .memory-cost { font-size: 12px; }
.memory-summary { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; }
.memory-error { color: #b42318; }
.memory-empty { color: var(--fn-text-secondary); padding-block: 20px; }
.memory-sources summary { cursor: pointer; padding: 8px 0; }
.memory-source-list { max-height: 180px; overflow-y: auto; }
.memory-source-list button { display: flex; justify-content: space-between; gap: 20px; text-align: left; width: 100%; border: 0; border-bottom: 1px solid var(--fn-border-primary); padding: 8px; background: transparent; }
.memory-source-list button span { overflow-wrap: anywhere; }
.memory-source-list small { flex-shrink: 0; color: var(--fn-text-secondary); }
.memory-actions { display: flex; justify-content: flex-end; gap: 10px; }
button { font: inherit; color: inherit; cursor: pointer; }
.memory-actions button, .memory-error button { border: 1px solid var(--fn-border-primary); border-radius: 6px; background: white; padding: 7px 12px; }
.memory-actions .memory-primary { background: var(--fn-brand); color: white; border-color: var(--fn-brand); }
button:disabled { opacity: .55; cursor: default; }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--fn-brand); outline-offset: 2px; }
</style>
