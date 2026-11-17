<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { compareSourceVersions, type SourceDifference } from '../sourceVersionApi'
import { sourceError } from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'
const props = defineProps<{ conversationId: string; leftId: string; rightId: string }>()
const result = ref<SourceDifference | null>(null),
  changesOnly = ref(true),
  busy = ref(false),
  error = ref('')
const labels = { unchanged: '未变', removed: '旧版删除', added: '新版增加' }
let generation = 0
async function load(offset = 0) {
  const token = ++generation
  busy.value = true
  error.value = ''
  try {
    const value = await compareSourceVersions(
      props.conversationId,
      props.leftId,
      props.rightId,
      offset,
      changesOnly.value
    )
    if (token === generation) result.value = value
  } catch (e) {
    if (token === generation) error.value = sourceError(e)
  } finally {
    if (token === generation) busy.value = false
  }
}
watch(
  () => [props.conversationId, props.leftId, props.rightId, changesOnly.value],
  () => {
    result.value = null
    void load()
  },
  { immediate: true }
)
onBeforeUnmount(() => {
  ++generation
})
</script>
<template>
  <section class="source-diff" aria-label="固定版本差异">
    <header>
      <h4>固定版本差异</h4>
      <label><input v-model="changesOnly" type="checkbox" />只看变化</label>
    </header>
    <p v-if="error" role="alert">
      {{ error }} <button :disabled="busy" @click="load()">重试</button>
    </p>
    <p v-if="busy" role="status">正在比较固定版本…</p>
    <template v-if="result">
      <div class="diff-links">
        <RouterLink :to="sourceReaderLocation(result.left.revision_id, conversationId)"
          >阅读旧版 v{{ result.left.version }}</RouterLink
        >
        <RouterLink :to="sourceReaderLocation(result.right.revision_id, conversationId)"
          >阅读新版 v{{ result.right.version }}</RouterLink
        >
      </div>
      <p>
        旧版删除 {{ result.counts.removed }} 单元 · 新版增加 {{ result.counts.added }} 单元 · 未变
        {{ result.counts.unchanged }} 单元
      </p>
      <p>比较已保存的提取文本和结构；长段按片段分页。版式和科研含义须结合原件核对。</p>
      <p v-if="!result.raw_bytes_identical && !result.counts.added && !result.counts.removed">
        提取内容相同，原件字节不同；原件的格式、元数据或未提取内容可能有变化。
      </p>
      <p v-if="result.coarse_regions">
        部分重复内容无法可靠对齐，已完整列为旧版删除与新版增加，请结合上下文核对。
      </p>
      <details v-if="result.left.omitted_content.length || result.right.omitted_content.length">
        <summary>提取范围与未比较内容</summary>
        <p>旧版：{{ result.left.omitted_content.join('、') || '未声明遗漏' }}</p>
        <p>新版：{{ result.right.omitted_content.join('、') || '未声明遗漏' }}</p>
      </details>
      <ol class="diff-rows">
        <li
          v-for="row in result.rows"
          :key="row.row"
          :class="row.status"
          :data-diff-status="row.status"
        >
          <div class="diff-position">
            <strong>{{ labels[row.status] }}</strong
            ><span>{{ row.label }}</span>
          </div>
          <details v-if="row.kind === 'structure'">
            <summary>结构记录</summary>
            <pre>{{ row.text }}</pre>
          </details>
          <pre v-else>{{ row.text || '（空行）' }}</pre>
        </li>
      </ol>
      <p v-if="!result.total_rows">这两个版本的提取内容没有变化。</p>
      <footer>
        <button
          v-if="result.offset"
          :disabled="busy"
          @click="load(Math.max(0, result.offset - 80))"
        >
          上一页
        </button>
        <span v-if="result.total_rows"
          >第 {{ result.offset + 1 }}–{{ result.offset + result.rows.length }} 单元，共
          {{ result.total_rows }} 单元</span
        >
        <button
          v-if="result.next_offset !== null"
          :disabled="busy"
          @click="load(result.next_offset!)"
        >
          下一页
        </button>
      </footer>
    </template>
  </section>
</template>
<style scoped>
.source-diff {
  margin: 12px 0;
  padding: 14px;
  border: 1px solid var(--fn-border-primary, #dce3f2);
  background: var(--fn-bg-primary, white);
  color: var(--fn-text-primary, #243448);
  font-size: 12px;
  line-height: 1.65;
}
header,
footer,
.diff-links,
.diff-position {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
header {
  justify-content: space-between;
}
h4 {
  margin: 0;
  font-size: 14px;
}
p {
  overflow-wrap: anywhere;
}
.diff-links a {
  text-decoration: underline;
}
.diff-rows {
  list-style: none;
  padding: 0;
  margin: 12px 0;
}
.diff-rows li {
  padding: 10px 12px;
  border-left: 3px solid #8b949e;
  margin: 4px 0;
}
.diff-rows .added {
  background: #eef8f1;
  border-color: #278044;
}
.diff-rows .removed {
  background: #fff0f0;
  border-color: #b64040;
}
.diff-position {
  color: #455a70;
  font-size: 11px;
  overflow-wrap: anywhere;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  margin: 6px 0;
}
button,
label,
summary {
  cursor: pointer;
}
button {
  padding: 5px 9px;
}
label {
  display: flex;
  gap: 5px;
  align-items: center;
}
button:focus-visible,
input:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--fn-brand, #365fa5);
  outline-offset: 2px;
}
</style>
