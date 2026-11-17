<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { createAnnotation, editAnnotation, getAnnotations, type Annotation } from '../researchApi'
import { sourceError, type SourceLocator } from '../sourceApi'
import { conversationState } from '../desktopState'
const props = defineProps<{
  revisionId: string
  conversationId: string
  locator: SourceLocator | null
  selectedText: string
}>()
const emit = defineEmits<{ annotations: [items: Annotation[]]; locate: [locator: SourceLocator] }>()
const items = ref<Annotation[]>([]),
  error = ref(''),
  body = ref(''),
  kind = ref('annotation'),
  busy = ref(false),
  editId = ref('')
const router = useRouter()
const expanded = ref(false)
function cancelEdit() { editId.value=''; body.value='' }
function beginEdit(item:Annotation) { editId.value=item.id; body.value=item.body }
let epoch = 0
async function load() {
  const token = ++epoch
  try {
    const r = await getAnnotations(props.revisionId, props.conversationId)
    if (token !== epoch) return
    items.value = r.items
    emit('annotations', r.items)
  } catch (e) {
    if (token === epoch) error.value = sourceError(e)
  }
}
watch(
  () => [props.revisionId, props.conversationId],
  () => {
    items.value = []
    body.value = ''
    editId.value = ''
    error.value = ''
    void load()
  },
  { immediate: true }
)
watch(
  () => props.selectedText,
  (value) => {
    if (value) expanded.value = true
  }
)
async function save() {
  if (busy.value || (!props.locator && !editId.value)) return
  const token = epoch
  busy.value = true
  error.value = ''
  try {
    const existing = items.value.find((a) => a.id === editId.value)
    if (existing) await editAnnotation(existing, props.conversationId, body.value)
    else
      await createAnnotation(
        props.revisionId,
        props.conversationId,
        props.locator!.locator_id,
        kind.value,
        body.value,
        props.selectedText
      )
    if (token !== epoch) return
    body.value = ''
    editId.value = ''
    await load()
  } catch (e) {
    if (token === epoch) error.value = sourceError(e)
  } finally {
    busy.value = false
  }
}
async function remove(item: Annotation) {
  try {
    await editAnnotation(item, props.conversationId, item.body, true)
    await load()
  } catch (e) {
    error.value = sourceError(e)
  }
}
async function ask() {
  if (!props.locator) return
  const cid = props.conversationId,
    rid = props.revisionId,
    loc = props.locator
  try {
    const draft = conversationState(cid)
    await draft.ready
    const current = draft.value.value
    const quote = props.selectedText || loc.quote
    draft.update({
      ...current,
      text: `${current.text ? current.text + '\n\n' : ''}请解释以下原文选段：\n${quote}\n\n来源版本：${rid}\n定位：${loc.locator_id}`,
      source_revision_ids: [...new Set([...(current.source_revision_ids || []), rid])],
    })
    await draft.flush()
    if (draft.status.value !== 'saved') throw Error('选段草稿尚未保存，请重试')
    await router.push({ path: '/chat', query: { conv: cid } })
  } catch (e) {
    error.value = sourceError(e)
  }
}
</script>
<template>
  <details
    class="research-notes"
    :open="expanded"
    @toggle="expanded = ($event.target as HTMLDetailsElement).open"
  >
    <summary>划线、批注与笔记 · {{ items.length }}</summary>
    <p v-if="error" role="alert">{{ error }}</p>
    <p class="note-hint">选择原文后保存；批注仅属于当前固定版本，其他版本不会自动套用旧锚点。</p>
    <form v-if="locator || editId" @submit.prevent="save">
      <blockquote v-if="locator">{{ (selectedText || locator.quote).slice(0, 400) }}</blockquote>
      <select v-if="!editId" v-model="kind" aria-label="批注类型">
        <option value="highlight">划线</option>
        <option value="annotation">批注</option>
        <option value="note">笔记</option>
      </select>
      <textarea
        v-model="body"
        aria-label="批注内容"
        placeholder="记录你的理解、疑问或笔记"
        maxlength="20000"
        rows="2"
      />
      <div class="note-actions">
        <button :disabled="busy" type="submit">{{ editId ? '保存编辑' : '保存选段' }}</button
        ><button type="button" :disabled="!locator" @click="ask">选段提问</button
        ><button
          v-if="editId"
          type="button"
          @click="cancelEdit"
        >
          取消编辑
        </button>
      </div>
    </form>
    <ul>
      <li v-for="item in items" :key="item.id">
        <button
          :disabled="!item.locator || item.anchor_status !== 'fixed_version'"
          @click="item.locator && emit('locate', item.locator)"
        >
          {{ { highlight: '划线', annotation: '批注', note: '笔记' }[item.kind] || item.kind }} ·
          {{ item.selected_text || item.locator?.quote?.slice(0, 100) || '锚点失效' }}
        </button>
        <p>{{ item.body }}</p>
        <small
          >修订 v{{ item.version }} ·
          {{ item.anchor_status === 'fixed_version' ? '固定版本' : '定位失效，不自动映射' }}</small
        >
        <div class="note-actions">
          <button
            @click="beginEdit(item)"
          >
            编辑</button
          ><button @click="remove(item)">删除</button>
        </div>
      </li>
    </ul>
  </details>
</template>
<style scoped>
.research-notes {
  padding: 12px 24px;
  background: var(--fn-bg-primary);
  border-bottom: 1px solid var(--fn-border-primary);
  max-height: 340px;
  overflow: auto;
  color: var(--fn-text-primary);
  font-size: 13px;
  flex-shrink: 0;
}
.note-hint,
small {
  color: var(--fn-text-secondary);
}
form {
  display: grid;
  gap: 8px;
}
textarea,
select,
button {
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
  padding: 7px 10px;
}
textarea {
  width: 100%;
  box-sizing: border-box;
}
blockquote {
  margin: 8px 0;
  padding: 8px 12px;
  border-left: 3px solid var(--fn-brand);
  white-space: pre-wrap;
}
.note-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
ul {
  list-style: none;
  padding: 0;
}
li {
  padding: 12px 0;
  border-top: 1px solid var(--fn-border-primary);
}
li p {
  white-space: pre-wrap;
}
li > button {
  max-width: 100%;
  text-align: left;
}
button:disabled {
  opacity: 0.5;
}
</style>
