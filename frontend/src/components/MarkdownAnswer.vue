<script setup lang="ts">
import { computed, nextTick, ref, shallowRef, watch } from 'vue'
import { Check, Copy, Download } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import { renderAnswer } from '../markdown'
import { copyAnswerText, tableText } from '../answerExport'
import 'katex/dist/katex.min.css'

const props = withDefaults(defineProps<{ text: string; evidenceIds?: string[] }>(), { evidenceIds: () => [] })
const emit = defineEmits<{ evidence: [id: string] }>()
const rendered = computed(() => renderAnswer(props.text, props.evidenceIds))
const root = ref<HTMLElement>()
const controls = shallowRef<{ target: Element; kind: 'code' | 'table'; index: number }[]>([])
const copied = ref('')
const copying = ref('')
watch(rendered, async (_value, _previous, onCleanup) => {
  let current = true
  onCleanup(() => { current = false })
  copied.value = ''
  controls.value = []
  // v-html replaces its nodes on streamed updates; bind Vue controls afterwards.
  await nextTick()
  if (!current || !root.value) return
  controls.value = Array.from(root.value.querySelectorAll('[data-code-actions], [data-table-actions]'), target => ({
    target,
    kind: target.hasAttribute('data-code-actions') ? 'code' : 'table',
    index: Number(target.getAttribute('data-code-actions') ?? target.getAttribute('data-table-actions')),
  }))
}, { immediate: true, flush: 'post' })

async function copy(kind: 'code' | 'table', index: number) {
  if (copying.value) return
  const snapshot = rendered.value
  const key = `${kind}-${index}`
  const text = kind === 'code' ? snapshot.codeBlocks[index] : tableText(snapshot.tables[index], 'tsv')
  copied.value = ''
  copying.value = key
  try {
    await copyAnswerText(text)
    if (snapshot === rendered.value) copied.value = key
  } catch {
    ElMessage.error('复制失败，请重试或手动选择内容复制')
  } finally {
    copying.value = ''
  }
}
function exportTable(index: number) {
  const blob = new Blob(['\ufeff', tableText(rendered.value.tables[index], 'csv')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `表格-${index + 1}.csv`
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function onClick(event: MouseEvent) {
  const button = (event.target as Element).closest<HTMLButtonElement>('button[data-evidence-id]')
  if (button?.dataset.evidenceId && props.evidenceIds.includes(button.dataset.evidenceId)) {
    emit('evidence', button.dataset.evidenceId)
  }
}
</script>

<template>
  <div ref="root" class="markdown-answer" @click="onClick" v-html="rendered.html"></div>
  <Teleport v-for="control in controls" :key="`${control.kind}-${control.index}`" :to="control.target">
    <button type="button" class="answer-block-action" :title="copied === `${control.kind}-${control.index}` ? '已复制' : control.kind === 'code' ? '复制代码' : '复制表格'" :aria-label="control.kind === 'code' ? '复制代码' : '复制表格'" :disabled="!!copying" :aria-busy="copying === `${control.kind}-${control.index}`" @click="copy(control.kind, control.index)">
      <Check v-if="copied === `${control.kind}-${control.index}`" :size="14" /><Copy v-else :size="14" />
    </button>
    <button v-if="control.kind === 'table'" type="button" class="answer-block-action" title="导出 CSV" aria-label="导出 CSV" @click="exportTable(control.index)"><Download :size="14" /></button>
  </Teleport>
</template>

<style scoped>
.markdown-answer { min-width: 0; font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; }
.markdown-answer :deep(> :first-child) { margin-top: 0; }
.markdown-answer :deep(> :last-child) { margin-bottom: 0; }
.markdown-answer :deep(p) { margin: 0 0 12px; }
.markdown-answer :deep(h1), .markdown-answer :deep(h2), .markdown-answer :deep(h3) { font-size: 17px; line-height: 1.5; margin: 22px 0 10px; }
.markdown-answer :deep(h4), .markdown-answer :deep(h5), .markdown-answer :deep(h6) { font-size: 14px; margin: 18px 0 8px; }
.markdown-answer :deep(ul), .markdown-answer :deep(ol) { padding-left: 24px; margin: 8px 0 14px; }
.markdown-answer :deep(li + li) { margin-top: 4px; }
.markdown-answer :deep(li > p) { margin: 4px 0; }
.markdown-answer :deep(blockquote) { margin: 12px 0; padding: 2px 0 2px 14px; border-left: 3px solid var(--fn-border-primary); color: var(--fn-text-secondary); }
.markdown-answer :deep(blockquote p) { margin: 4px 0; }
.markdown-answer :deep(code) { font-family: Consolas, 'Cascadia Code', monospace; font-size: 12px; background: var(--fn-bg-secondary); border-radius: 4px; padding: 2px 4px; }
.markdown-answer :deep(pre) { margin: 12px 0; padding: 14px; overflow-x: auto; border: 1px solid var(--fn-border-primary); border-radius: 6px; background: var(--fn-bg-secondary); line-height: 1.6; }
.markdown-answer :deep(pre code) { padding: 0; background: transparent; white-space: pre; overflow-wrap: normal; }
.markdown-answer :deep(.markdown-code) { margin: 12px 0; border: 1px solid var(--fn-border-primary); border-radius: 6px; overflow: hidden; }
.markdown-answer :deep(.markdown-code pre) { margin: 0; border: 0; border-radius: 0; }
.markdown-answer :deep(.markdown-block-toolbar) { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 32px; padding: 2px 8px 2px 12px; background: var(--fn-bg-secondary); color: var(--fn-text-secondary); font: 11px/1.5 Consolas, 'Cascadia Code', monospace; }
.markdown-answer :deep(.markdown-block-toolbar > span:first-child) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.markdown-answer :deep([data-code-actions]), .markdown-answer :deep([data-table-actions]) { display: flex; flex: none; gap: 2px; }
.answer-block-action { display: inline-flex; align-items: center; justify-content: center; flex: none; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--fn-text-secondary); cursor: pointer; }
.answer-block-action:hover { background: var(--fn-bg-active); color: var(--fn-text-primary); }
.answer-block-action:focus-visible { outline: 2px solid var(--fn-brand); outline-offset: -2px; }
.answer-block-action:disabled { opacity: .5; cursor: wait; }
.markdown-answer :deep(.hljs-keyword), .markdown-answer :deep(.hljs-selector-tag), .markdown-answer :deep(.hljs-literal) { color: #a82446; }
.markdown-answer :deep(.hljs-string), .markdown-answer :deep(.hljs-attr), .markdown-answer :deep(.hljs-regexp) { color: #17623c; }
.markdown-answer :deep(.hljs-title), .markdown-answer :deep(.hljs-built_in), .markdown-answer :deep(.hljs-type) { color: #245dcc; }
.markdown-answer :deep(.hljs-number), .markdown-answer :deep(.hljs-symbol) { color: #79530b; }
.markdown-answer :deep(.hljs-comment), .markdown-answer :deep(.hljs-meta) { color: #636a73; }
.markdown-answer :deep(.katex-block) { max-width: 100%; overflow-x: auto; overflow-y: hidden; padding: 4px 0; }
.markdown-answer :deep(.katex-display) { margin: 8px 0; }
.markdown-answer :deep(.katex-error) { white-space: pre-wrap; overflow-wrap: anywhere; }
.markdown-answer :deep(.markdown-table-block) { margin: 12px 0; }
.markdown-answer :deep(.markdown-table-block .markdown-table) { margin: 0; }
.markdown-answer :deep(.markdown-table) { overflow-x: auto; margin: 12px 0; }
.markdown-answer :deep(table) { border-collapse: collapse; font-size: 13px; min-width: 100%; }
.markdown-answer :deep(th), .markdown-answer :deep(td) { border: 1px solid var(--fn-border-primary); padding: 7px 10px; text-align: left; min-width: 100px; }
.markdown-answer :deep(th) { background: var(--fn-bg-secondary); font-weight: 600; }
.markdown-answer :deep(img) { display: block; max-width: 100%; height: auto; border-radius: 4px; margin: 10px 0; }
.markdown-answer :deep(hr) { border: 0; border-top: 1px solid var(--fn-border-primary); margin: 20px 0; }
.markdown-answer :deep(a) { text-decoration: underline; text-underline-offset: 3px; }
.markdown-answer :deep(.markdown-citation) { display: inline-flex; align-items: center; justify-content: center; min-width: 24px; height: 20px; padding: 0 4px; margin: 0 2px; border: 1px solid var(--fn-border-primary); border-radius: 4px; background: var(--fn-bg-active); color: var(--fn-brand); font: inherit; font-size: 11px; line-height: 1; cursor: pointer; vertical-align: baseline; }
</style>
