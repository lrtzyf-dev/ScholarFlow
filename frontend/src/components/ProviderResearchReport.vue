<script setup lang="ts">
import { computed } from 'vue'
import MarkdownAnswer from './MarkdownAnswer.vue'
const props = defineProps<{ report: any }>()
const citations = computed(() => (Array.isArray(props.report?.citations) ? props.report.citations : []).filter((v: any) => {
  try { const u = new URL(v.url); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password } catch { return false }
}))
</script>

<template>
  <details class="provider-report">
    <summary>{{ report?.protocol === 'openai_chat_files' ? 'DeepSeek 云文件读取结果' : 'DeepSeek 联网研究结果' }} · {{ citations.length }} 处供应商引用</summary>
    <p class="note">{{ report?.verification_note || '以下链接由供应商返回，未独立核验原文。' }} 研序的最终结论会在任务完成后汇总。</p>
    <MarkdownAnswer :text="typeof report?.answer === 'string' ? report.answer : ''" />
    <ol v-if="citations.length" aria-label="供应商引用来源">
      <li v-for="(citation, index) in citations" :key="index">
        <a :href="citation.url" target="_blank" rel="noopener noreferrer">{{ citation.title || citation.url }}</a>
        <blockquote v-if="citation.excerpt">{{ citation.excerpt }}</blockquote>
        <small>对应研究回答片段；未声明是网页逐字原文。</small>
      </li>
    </ol>
  </details>
</template>

<style scoped>
.provider-report { margin: 10px 0; padding: 10px 12px; border: 1px solid var(--fn-border-primary); border-radius: 8px; font-size: 13px; overflow-wrap: anywhere; }
summary { cursor: pointer; line-height: 1.6; } .note,small { color: var(--fn-text-secondary); font-size: 12px; line-height: 1.7; }
li { margin: 12px 0; } a { color: var(--fn-brand); } blockquote { margin: 6px 0; padding-left: 10px; border-left: 2px solid var(--fn-border-primary); }
</style>
