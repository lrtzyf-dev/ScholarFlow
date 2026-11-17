<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

const router = useRouter()
import { getFutureWork, type FutureWorkResult } from '../api'

const data = ref<FutureWorkResult | null>(null)
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    data.value = await getFutureWork()
  } catch (err: any) {
    error.value = err?.response?.data?.detail || err?.message || String(err)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page analysis-page future-page">
    <div class="page-head">
      <div>
        <div class="page-title">未来方向</div>
        <div class="page-desc">从文献中的局限与展望，整理下一步值得追问的问题。</div>
      </div>
    </div>

    <div v-if="loading" class="card muted">加载中…</div>
    <div v-else-if="error" class="card err">{{ error }}</div>
    <div v-else-if="data!.total === 0" class="empty">
      未识别到 Future Work 内容。请在「文献库」上传含未来工作/展望章节的论文。
    </div>

    <template v-else>
      <div class="future-summary">
        <strong
          >{{ data!.open.length }} 个开放方向 <span>/</span
          >{{ data!.possibly_addressed.length }} 个可能已有进展</strong
        ><span>来自当前文献库的 Future Work</span>
      </div>
      <div class="section-head">
        <span class="dot open"></span>开放方向（{{ data!.open.length }}）
      </div>
      <div class="dir-list">
        <div v-for="d in data!.open" :key="d.paper_id + d.quote" class="card dir-card">
          <div class="dir-meta">
            <span class="paper">{{ d.paper_title }}</span>
            <span class="year">{{ d.year ?? '年份未知' }}</span>
            <button
              class="evidence-link"
              @click="router.push({ path: `/reader/${d.paper_id}`, query: { page: d.page } })"
            >
              查看原文 · 第 {{ d.page }} 页
            </button>
          </div>
          <p class="quote">{{ d.quote }}</p>
        </div>
      </div>

      <div v-if="data!.possibly_addressed.length" class="section-head addressed">
        <span class="dot addressed"></span>疑似已被后续工作处理（{{
          data!.possibly_addressed.length
        }}）
      </div>
      <div v-if="data!.possibly_addressed.length" class="dir-list">
        <div
          v-for="d in data!.possibly_addressed"
          :key="d.paper_id + d.quote"
          class="card dir-card addressed"
        >
          <div class="dir-meta">
            <span class="paper">{{ d.paper_title }}</span>
            <span class="year">{{ d.year ?? '年份未知' }}</span>
            <button
              class="evidence-link"
              @click="router.push({ path: `/reader/${d.paper_id}`, query: { page: d.page } })"
            >
              查看原文 · 第 {{ d.page }} 页
            </button>
          </div>
          <p class="quote">{{ d.quote }}</p>
          <div class="resolved">
            <span class="resolved-label">后续匹配：</span>
            <button
              v-for="r in d.resolved_by"
              :key="r.paper_id"
              class="resolved-item"
              @click="router.push(`/reader/${r.paper_id}`)"
            >
              {{ r.paper_title }}（{{ r.year }}）
            </button>
          </div>
          <p class="note">该判断为内容匹配的启发式结果，建议人工确认后再排除。</p>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.future-summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  border-top: 1px solid var(--fn-border-primary);
  border-bottom: 1px solid var(--fn-border-primary);
  padding: 17px 0;
  margin-bottom: 28px;
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.future-summary strong {
  color: var(--fn-text-primary);
  font-weight: 500;
}
.future-summary strong span {
  color: var(--fn-text-tertiary);
  margin: 0 13px;
}
.section-head {
  display: flex;
  gap: 9px;
  align-items: center;
  font-size: 13px;
  font-weight: 500;
  margin: 0 0 6px;
  color: var(--fn-text-primary);
}
.section-head.addressed {
  margin-top: 30px;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fn-brand);
}
.dot.addressed {
  background: var(--fn-warning-text);
}
.dir-list {
  display: flex;
  flex-direction: column;
  counter-reset: direction;
}
.dir-card {
  position: relative;
  padding: 23px 0 23px 50px;
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.dir-card::before {
  counter-increment: direction;
  content: counter(direction, decimal-leading-zero);
  position: absolute;
  top: 25px;
  left: 0;
  color: var(--fn-text-tertiary);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.dir-meta {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 11px;
}
.paper {
  font-weight: 500;
  color: var(--fn-text-primary);
  font-size: 14px;
  line-height: 1.7;
}
.year {
  color: var(--fn-text-tertiary);
}
.evidence-link {
  padding: 3px 6px;
  border: 0;
  border-radius: 4px;
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
  font-size: 10px;
  cursor: pointer;
}
.evidence-link:hover {
  background: var(--fn-bg-active);
}
.quote {
  font-size: 12px;
  line-height: 1.9;
  color: var(--fn-text-secondary);
  margin: 11px 0 0;
  max-width: 850px;
}
.resolved {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
}
.resolved-label {
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.resolved-item {
  border: 0;
  padding: 3px 6px;
  border-radius: 4px;
  color: var(--fn-warning-text);
  background: var(--fn-warning-bg);
  font-size: 11px;
  cursor: pointer;
  text-align: left;
}
.note {
  max-width: 680px;
  margin: 14px 0 0;
  padding: 10px 13px;
  border-radius: 5px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
  font-size: 10px;
  line-height: 1.9;
}
.err {
  color: var(--fn-danger);
}
@media (max-width: 700px) {
  .dir-card {
    padding-left: 36px;
  }
  .paper {
    font-size: 13px;
  }
}
</style>
