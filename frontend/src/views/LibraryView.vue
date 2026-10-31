<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listPapers, importSamples, uploadPaper, getGraph, type Paper } from '../api'

const router = useRouter()
const papers = ref<Paper[]>([])
const edgeCount = ref(0)
const loading = ref(false)
const uploading = ref(false)
const loadingSamples = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const keyword = ref('')
const statusFilter = ref('all')
const sortKey = ref('recent')

async function loadSamples() {
  loadingSamples.value = true
  try {
    const result = await importSamples()
    ElMessage.success(`已加载 ${result.length} 篇示例论文并完成索引`)
    await refresh()
  } catch (err: any) {
    ElMessage.error(err?.message || '示例加载失败')
  } finally {
    loadingSamples.value = false
  }
}

async function refresh() {
  loading.value = true
  try {
    papers.value = await listPapers()
    try {
      const g = await getGraph()
      edgeCount.value = g.edges?.length || 0
    } catch {
      edgeCount.value = 0
    }
  } finally {
    loading.value = false
  }
}

async function onPick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    ElMessage.warning('仅支持 PDF 文件')
    return
  }
  uploading.value = true
  try {
    const paper = await uploadPaper(file)
    if (paper.duplicate) {
      ElMessage.info(`该文献已在库中（《${paper.title}》），未重复入库`)
    } else if (paper.status === 'failed') {
      ElMessage.error(`解析失败：${paper.error}`)
    } else {
      ElMessage.success(`入库完成：${paper.page_count} 页，已建立全文与向量索引`)
    }
    await refresh()
  } catch (err: any) {
    ElMessage.error(err?.message || '上传失败')
  } finally {
    uploading.value = false
    if (input) input.value = ''
  }
}

// ---- 统计 ----
const totalPages = computed(() =>
  papers.value.reduce((s, p) => s + (p.page_count || 0), 0),
)
const parsedCount = computed(() => papers.value.filter((p) => p.status === 'parsed').length)
const processingCount = computed(() => papers.value.filter((p) => p.status === 'processing').length)

// ---- 过滤 + 排序 ----
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  let list = papers.value.filter((p) => {
    if (statusFilter.value !== 'all' && p.status !== statusFilter.value) return false
    if (kw) {
      const hay = `${p.title} ${p.authors} ${p.abstract || ''}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
  list = [...list]
  if (sortKey.value === 'recent') {
    list.sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at))
  } else if (sortKey.value === 'pages') {
    list.sort((a, b) => b.page_count - a.page_count)
  } else if (sortKey.value === 'title') {
    list.sort((a, b) => (a.title || '').localeCompare(b.title || ''))
  }
  return list
})

// 封面配色（按 id 稳定取色）
const COVERS = [
  ['#0a59f7', '#5b8def'],
  ['#7c3aed', '#a78bfa'],
  ['#0d9488', '#2dd4bf'],
  ['#ea580c', '#fb923c'],
  ['#db2777', '#f472b6'],
  ['#2563eb', '#60a5fa'],
]
function cover(id: string) {
  let h = 0
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0
  return COVERS[h % COVERS.length]
}

function statusBadge(p: Paper) {
  if (p.status === 'parsed') return { text: '已解析', cls: 'ok' }
  if (p.status === 'failed') return { text: '解析失败', cls: 'fail' }
  return { text: '解析中', cls: 'mock' }
}

function fmtDate(s: string) {
  const d = new Date(s)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

onMounted(refresh)
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">文献库</div>
        <div class="page-desc">上传论文 PDF，自动完成解析、切块、全文索引与向量索引</div>
      </div>
      <div class="toolbar">
        <button class="el-button" :disabled="loadingSamples" @click="loadSamples">
          {{ loadingSamples ? '加载中…' : '加载示例论文' }}
        </button>
        <button class="el-button el-button--primary" :disabled="uploading" @click="fileInput?.click()">
          {{ uploading ? '上传解析中…' : '上传 PDF' }}
        </button>
        <input ref="fileInput" type="file" accept="application/pdf" style="display: none" @change="onPick" />
      </div>
    </div>

    <div v-if="loading" class="card muted">加载中…</div>
    <div v-else-if="papers.length === 0" class="empty">
      文献库为空。点击「加载示例论文」即可载入两篇示例并体验完整流程，或点击「上传 PDF」导入自己的论文。
    </div>

    <template v-else>
      <!-- 统计 -->
      <div class="lib-stats">
        <div class="lib-stat">
          <div class="lib-stat-top">
            <span class="lib-stat-label">文献总数</span>
            <span class="lib-stat-ico" style="background:#eef3ff;color:#0a59f7">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
            </span>
          </div>
          <div class="lib-stat-val">{{ papers.length }}</div>
          <div class="lib-stat-trend">解析中 {{ processingCount }} 篇</div>
        </div>
        <div class="lib-stat">
          <div class="lib-stat-top">
            <span class="lib-stat-label">总页数</span>
            <span class="lib-stat-ico" style="background:#f0ebff;color:#7c3aed">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            </span>
          </div>
          <div class="lib-stat-val">{{ totalPages }}</div>
          <div class="lib-stat-trend">平均每篇 {{ papers.length ? (totalPages / papers.length).toFixed(1) : 0 }} 页</div>
        </div>
        <div class="lib-stat">
          <div class="lib-stat-top">
            <span class="lib-stat-label">已解析</span>
            <span class="lib-stat-ico" style="background:#ecfdf5;color:#10b981">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6L9 17l-5-5"/></svg>
            </span>
          </div>
          <div class="lib-stat-val">{{ parsedCount }}</div>
          <div class="lib-stat-trend">完成解析与索引</div>
        </div>
        <div class="lib-stat">
          <div class="lib-stat-top">
            <span class="lib-stat-label">引用连边</span>
            <span class="lib-stat-ico" style="background:#e6faf6;color:#0d9488">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>
            </span>
          </div>
          <div class="lib-stat-val">{{ edgeCount }}</div>
          <div class="lib-stat-trend">局域图谱已生成</div>
        </div>
      </div>

      <!-- 工具栏 -->
      <div class="lib-toolbar">
        <div class="lib-search">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
          <input v-model="keyword" placeholder="搜索标题、作者、关键词…" />
        </div>
        <select v-model="statusFilter" class="lib-select">
          <option value="all">全部状态</option>
          <option value="parsed">已解析</option>
          <option value="processing">解析中</option>
          <option value="failed">解析失败</option>
        </select>
        <select v-model="sortKey" class="lib-select">
          <option value="recent">最近导入</option>
          <option value="pages">页数最多</option>
          <option value="title">标题排序</option>
        </select>
      </div>

      <!-- 卡片网格 -->
      <div v-if="filtered.length === 0" class="empty">没有匹配的论文，试试调整关键词或筛选条件。</div>
      <div v-else class="lib-grid">
        <div v-for="p in filtered" :key="p.id" class="lib-paper">
          <div class="lib-cover" :style="{ background: `linear-gradient(135deg, ${cover(p.id)[0]}, ${cover(p.id)[1]})` }">
            <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
            <span class="lib-pill" :class="statusBadge(p).cls">{{ statusBadge(p).text }}</span>
          </div>
          <div class="lib-body">
            <div class="lib-title">{{ p.title }}</div>
            <div v-if="p.authors" class="lib-authors">{{ p.authors }}</div>
            <div v-if="p.abstract" class="lib-abstract">{{ p.abstract }}</div>
            <div v-if="p.error" class="lib-error">{{ p.error }}</div>
            <div class="lib-meta">
              <span>{{ p.page_count }} 页</span>
              <span class="lib-dot"></span>
              <span>{{ fmtDate(p.created_at) }} 导入</span>
            </div>
            <div class="lib-actions">
              <button class="lib-pa primary" @click="router.push(`/reader/${p.id}`)">阅读</button>
              <button class="lib-pa" @click="router.push({ path: '/chat', query: { paper: p.id } })">提问</button>
              <button class="lib-pa" @click="router.push('/graph')">图谱</button>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lib-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin-bottom: 18px;
}
.lib-stat {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: var(--fn-radius-lg);
  padding: 14px 16px;
  box-shadow: var(--fn-shadow-sm);
}
.lib-stat-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.lib-stat-label {
  font-size: 12.5px;
  color: var(--fn-text-tertiary);
}
.lib-stat-ico {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.lib-stat-ico svg {
  width: 16px;
  height: 16px;
}
.lib-stat-val {
  font-size: 23px;
  font-weight: 700;
  margin-top: 8px;
  letter-spacing: -0.02em;
}
.lib-stat-trend {
  font-size: 11.5px;
  margin-top: 2px;
  color: var(--fn-text-tertiary);
}
.lib-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
}
.lib-search {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 38px;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 0 13px;
}
.lib-search svg {
  width: 16px;
  height: 16px;
  color: var(--fn-text-tertiary);
  flex-shrink: 0;
}
.lib-search input {
  border: none;
  outline: none;
  flex: 1;
  font-size: 13.5px;
  background: transparent;
}
.lib-select {
  height: 38px;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 0 12px;
  font-size: 13px;
  color: var(--fn-text-secondary);
  cursor: pointer;
  outline: none;
}
.lib-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}
.lib-paper {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: var(--fn-shadow-sm);
  transition: transform var(--fn-transition-fast), box-shadow var(--fn-transition-fast), border-color var(--fn-transition-fast);
  display: flex;
  flex-direction: column;
}
.lib-paper:hover {
  border-color: var(--fn-brand);
  box-shadow: var(--fn-shadow-md);
  transform: translateY(-2px);
}
.lib-cover {
  height: 84px;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}
.lib-cover svg {
  width: 40px;
  height: 40px;
  opacity: 0.92;
}
.lib-pill {
  position: absolute;
  top: 10px;
  right: 10px;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 20px;
  border: 1px solid transparent;
}
.lib-pill.ok {
  background: var(--fn-success-bg);
  color: var(--fn-success);
  border-color: var(--fn-success-border);
}
.lib-pill.mock {
  background: var(--fn-warning-bg);
  color: var(--fn-warning-text);
  border-color: var(--fn-warning-border);
}
.lib-pill.fail {
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
  border-color: var(--fn-danger-border);
}
.lib-body {
  padding: 14px 16px 15px;
  flex: 1;
  display: flex;
  flex-direction: column;
}
.lib-title {
  font-size: 14.5px;
  font-weight: 600;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.lib-authors {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin-top: 5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.lib-abstract {
  font-size: 12.5px;
  color: var(--fn-text-secondary);
  line-height: 1.55;
  margin-top: 9px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.lib-error {
  font-size: 12px;
  color: var(--fn-danger);
  margin-top: 8px;
}
.lib-meta {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--fn-border-primary);
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
}
.lib-dot {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--fn-text-quaternary);
}
.lib-actions {
  display: flex;
  gap: 6px;
  margin-top: 11px;
}
.lib-pa {
  flex: 1;
  height: 30px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  border-radius: 8px;
  font-size: 12px;
  color: var(--fn-text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--fn-transition-fast);
}
.lib-pa:hover {
  border-color: var(--fn-brand);
  color: var(--fn-brand);
}
.lib-pa.primary {
  background: var(--fn-bg-brand-light);
  border-color: transparent;
  color: var(--fn-brand);
  font-weight: 600;
}
@media (max-width: 1200px) {
  .lib-grid { grid-template-columns: repeat(2, 1fr); }
  .lib-stats { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 760px) {
  .lib-grid { grid-template-columns: 1fr; }
  .lib-stats { grid-template-columns: 1fr; }
}
</style>
