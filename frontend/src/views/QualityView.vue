<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listPapers, type Paper } from '../api'
import { activeConversationId } from '../desktopState'
import { sourceReaderLocation } from '../sourceSelection'
import { sourceError } from '../sourceApi'
import { useQualityRuns } from '../useQualityRuns'
import type { QualityLabel, QualityRun } from '../qualityRunApi'
import SaveArtifactButton from '../components/SaveArtifactButton.vue'

const route = useRoute(), router = useRouter()
const allPapers = ref<Paper[]>([]), paperId = ref(''), paperError = ref('')
const filter = ref<QualityLabel | 'all'>('all')
const conversation = computed(() => typeof route.query.conversation === 'string' ? route.query.conversation : activeConversationId.value)
const runs = useQualityRuns(conversation, restoreRun)
const { job, jobs, selected, error, submitting, loading } = runs
const parsedPapers = computed(() => allPapers.value.filter(p => p.status === 'parsed'))
const result = computed(() => job.value?.result || null)
const labels: Record<QualityLabel, string> = { substantive: '讨论信号较多', neutral: '一般讨论信号', passing: '讨论信号较少', not_located: '未定位原句' }
const keys: QualityLabel[] = ['substantive', 'neutral', 'passing', 'not_located']
const filtered = computed(() => !result.value ? [] : filter.value === 'all' ? result.value.results : result.value.results.filter(r => r.label === filter.value))
const evidence = computed(() => result.value?.results.flatMap(r => r.evidence) || [])
const artifactContent = computed(() => result.value ? '# 引用讨论信号分析：' + result.value.title + '\n\n' + result.value.note + '\n\n' + result.value.results.map(r => `## [${r.ref_index}] ${labels[r.label]}\n${r.raw_text}\n\n` + r.evidence.map(e => `${e.quote} [${e.id}]`).join('\n\n')).join('\n\n') : '')
const headline = computed(() => !job.value ? '' : loading.value ? '正在分析，离开页面后继续' : job.value.stage === 'completed' ? '固定版本分析已保存 · 科学结论尚未核验' : '分析尚未完成，已保存进度保留')
function restoreRun(value: QualityRun) { paperId.value = value.inputs.paper_id; filter.value = 'all' }
function selectPaper(id: string) { paperId.value = id; filter.value = 'all' }
function start() { if (paperId.value) runs.create({ paper_id: paperId.value }) }
watch(() => job.value?.run_id, id => {
  if (id && route.query.run !== id) void router.replace({ path: '/quality', query: { conversation: conversation.value, run: id } })
})
watch(() => [conversation.value, route.query.run], ([cid, rid], old) => {
  const requested = typeof rid === 'string' ? rid : ''
  if (old && cid === old[0] && requested === job.value?.run_id) return
  runs.reset(); paperId.value = ''; filter.value = 'all'
  void runs.history(requested, !requested)
}, { immediate: true })
onMounted(async () => {
  try { allPapers.value = await listPapers() } catch (err) { paperError.value = sourceError(err) }
})
</script>

<template>
  <div class="page analysis-page quality-page">
    <div class="page-head"><div><div class="page-title">引用含金量</div><div class="page-desc">按固定原文检查引用讨论信号，原句可跳转复核。</div></div></div>
    <div class="run-toolbar">
      <label>已保存分析 <select v-model="selected" aria-label="已保存分析" :disabled="submitting" @change="runs.select()"><option value="" disabled>选择历史任务</option><option v-for="item in jobs" :key="item.run_id" :value="item.run_id">{{ new Date(item.updated_at).toLocaleString() }} · {{ item.title.slice(0, 36) }}</option></select></label>
      <button :disabled="submitting" @click="runs.refresh()">刷新状态</button>
      <button v-if="loading" :disabled="submitting" @click="runs.cancel()">停止分析</button>
      <button v-else-if="job?.can_resume" :disabled="submitting" @click="runs.resume()">继续未完成分析</button>
      <span>本地规则分析 · 不调用模型或产生 API 费用</span>
    </div>
    <p v-if="error || paperError" class="error" role="alert">{{ error || paperError }}</p>
    <p v-if="job" class="run-notice" role="status">{{ headline }}<span v-if="result"> · {{ result.progress.completed }}/{{ result.progress.total }} 条</span></p>
    <p v-if="job?.error" class="error">{{ job.error }}</p>
    <p v-if="job?.resume_blocked_reason" class="muted">{{ job.resume_blocked_reason }}</p>
    <div class="layout">
      <div class="card paper-list">
        <div class="sec-h">选择论文</div>
        <button v-for="p in parsedPapers" :key="p.id" class="paper-row" :class="{ active: p.id === paperId }" :disabled="loading || submitting" @click="selectPaper(p.id)"><span class="paper-title">{{ p.title }}</span><span class="paper-meta">{{ p.page_count }} 页</span></button>
        <div v-if="!parsedPapers.length" class="muted">暂无已解析论文。</div>
        <button class="start-button" :disabled="!paperId || loading || submitting" @click="start">{{ submitting ? '保存任务中…' : '新建固定版本分析' }}</button>
      </div>
      <div class="card result-card">
        <div v-if="!result" class="muted center">{{ loading ? '正在准备固定原件…' : '选择论文并开始分析，或打开已保存的分析。' }}</div>
        <template v-else>
          <h2 class="result-title">{{ result.title }}</h2>
          <p class="method-note">{{ result.note }}</p>
          <div class="result-actions"><RouterLink :to="sourceReaderLocation(result.revision_id, conversation)">阅读本次固定原件</RouterLink><SaveArtifactButton :title="'引用讨论分析：' + result.title" :content="artifactContent" :evidence="evidence" :conversation-id="conversation" :disabled="loading || evidence.length > 500" /></div>
          <p v-if="evidence.length > 500" class="muted">本次超过单个成果500条来源，完整分析仍保存在此任务中。</p>
          <div class="summary-bar"><div v-for="key in keys" :key="key" class="sum-item"><b>{{ result.summary[key] || 0 }}</b><span>{{ labels[key] }}</span></div></div>
          <div class="filter-bar"><button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部 {{ result.results.length }}</button><button v-for="key in keys" :key="key" :class="{ active: filter === key }" @click="filter = key">{{ labels[key] }} {{ result.summary[key] || 0 }}</button></div>
          <div class="ref-list">
            <div v-for="r in filtered" :key="r.ref_index" class="ref-row">
              <div class="ref-top"><span class="ref-no">[{{ r.ref_index }}]</span><span class="ref-badge" :class="r.label">{{ labels[r.label] }}</span><span class="ref-meta">已定位 {{ r.located_contexts }} 处{{ r.sample_limited ? '（最多8处）' : '' }}</span></div>
              <div class="ref-text">{{ r.raw_text }}</div>
              <p v-if="!r.reference_entry_in_source" class="muted">参考条目未与当前固定原文逐字匹配，引用目标待人工核对。</p>
              <div v-if="r.signals.length" class="ref-signals"><span v-for="signal in r.signals" :key="signal" class="signal-chip">{{ signal }}</span></div>
              <details v-if="r.evidence.length" class="citation-passages"><summary>查看正文引用原句 · 科学支持未核验</summary><blockquote v-for="e in r.evidence" :key="e.id"><p>{{ e.quote }}</p><RouterLink :to="sourceReaderLocation(e.revision_id, conversation, e.locator_id)">{{ e.id }} · 跳转固定原句</RouterLink></blockquote></details>
            </div>
            <div v-if="!filtered.length" class="muted center">{{ result.progress.total === 0 ? '未提取到参考条目；未据此判断没有引用。' : '该筛选下没有参考条目。' }}</div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-toolbar,.result-actions { display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0 20px;font-size:12px; }
.run-toolbar label { display:flex;gap:8px;align-items:center;min-width:0; }
.run-toolbar select { max-width:min(420px,60vw); }
.run-toolbar button,.run-toolbar select,.start-button { padding:8px 10px;border:1px solid var(--fn-border-primary);border-radius:6px;background:var(--fn-bg-primary);color:var(--fn-text-primary);font:inherit;font-size:12px; }
button:disabled { opacity:.5;cursor:default; }
button:focus-visible,select:focus-visible,a:focus-visible,summary:focus-visible { outline:2px solid var(--fn-brand);outline-offset:2px; }
.run-toolbar span,.run-notice,.method-note,.muted { color:var(--fn-text-secondary);font-size:12px;line-height:1.8; }
.result-title { margin:0;font-size:16px;line-height:1.7;overflow-wrap:anywhere; }
.citation-passages { margin-top:12px;font-size:12px;line-height:1.8; }
.citation-passages summary { cursor:pointer;color:var(--fn-text-secondary); }
.citation-passages blockquote { margin:12px 0;padding:4px 12px;border-left:2px solid var(--fn-border-primary);overflow-wrap:anywhere; }
.citation-passages p { white-space:pre-wrap; }
a { color:var(--fn-brand); }

.layout {
  display: grid;
  grid-template-columns: 235px minmax(0, 1fr);
  min-height: 520px;
  border-top: 1px solid var(--fn-border-primary);
}
.paper-list {
  border: 0;
  border-right: 1px solid var(--fn-border-primary);
  border-radius: 0;
  padding: 22px 18px 0 0;
  background: transparent;
  box-shadow: none;
}
.sec-h {
  font-size: 11px;
  color: var(--fn-text-secondary);
  margin-bottom: 11px;
}
.paper-row {
  display: block;
  width: 100%;
  border: 0;
  background: transparent;
  text-align: left;
  padding: 12px 10px;
  margin-bottom: 4px;
  border-radius: 6px;
  cursor: pointer;
}
.paper-row:hover,
.paper-row.active {
  background: var(--fn-bg-secondary);
}
.paper-title {
  font-size: 12px;
  line-height: 1.7;
  color: var(--fn-text-secondary);
}
.paper-row.active .paper-title {
  color: var(--fn-brand);
}
.paper-meta {
  display: block;
  font-size: 10px;
  color: var(--fn-text-tertiary);
  margin-top: 4px;
}
.result-card {
  min-width: 0;
  display: flex;
  flex-direction: column;
  border: 0;
  border-radius: 0;
  padding: 23px 0 0 28px;
  box-shadow: none;
  background: transparent;
}
.center {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1;
  min-height: 240px;
  font-size: 13px;
  line-height: 1.8;
}
.summary-bar {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding-bottom: 24px;
  margin-bottom: 21px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.sum-item {
  padding: 0 20px;
  border-left: 1px solid var(--fn-border-primary);
}
.sum-item:first-child {
  padding-left: 0;
  border-left: 0;
}
.sum-item b {
  display: block;
  font-size: 25px;
  font-weight: 450;
  line-height: 1.2;
  color: var(--fn-text-primary);
  margin-bottom: 7px;
}
.sum-item span {
  font-size: 10px;
  color: var(--fn-text-tertiary);
  white-space: nowrap;
}
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 17px;
  margin-bottom: 11px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.filter-bar button {
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 0 0 12px;
  background: transparent;
  color: var(--fn-text-tertiary);
  font-size: 11px;
  cursor: pointer;
}
.filter-bar button.active {
  color: var(--fn-brand);
  border-bottom-color: var(--fn-brand);
}
.ref-list {
  display: flex;
  flex-direction: column;
}
.ref-row {
  padding: 18px 0;
  border-bottom: 1px solid var(--fn-border-primary);
}
.ref-top {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ref-no {
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.ref-badge {
  font-size: 10px;
  padding: 3px 6px;
  border-radius: 4px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
}
.ref-badge.substantive {
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
}
.ref-badge.passing {
  color: var(--fn-warning-text);
  background: var(--fn-warning-bg);
}
.ref-meta {
  margin-left: auto;
  font-size: 10px;
  color: var(--fn-text-tertiary);
}
.ref-text {
  font-size: 12px;
  line-height: 1.8;
  color: var(--fn-text-primary);
  margin-top: 8px;
  overflow-wrap: anywhere;
}
.ref-signals {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 9px;
}
.signal-chip {
  border-left: 1px solid var(--fn-border-primary);
  padding-left: 10px;
  font-size: 10px;
  color: var(--fn-text-secondary);
}
.signal-chip:first-child {
  border-left: 0;
  padding-left: 0;
}
.error {
  color: var(--fn-danger);
}
@media (max-width: 1100px) {
  .layout {
    grid-template-columns: 205px minmax(0, 1fr);
  }
  .result-card {
    padding-left: 22px;
  }
  .sum-item {
    padding: 0 12px;
  }
}
@media (max-width: 800px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .paper-list {
    display: flex;
    overflow-x: auto;
    gap: 6px;
    border-right: 0;
    border-bottom: 1px solid var(--fn-border-primary);
    padding: 14px 0;
  }
  .sec-h {
    display: none;
  }
  .paper-row {
    min-width: 180px;
    max-width: 210px;
  }
  .result-card {
    padding-left: 0;
  }
}
</style>
