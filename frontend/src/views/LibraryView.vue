<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus,
  Search,
  BookOpen,
  SquarePen,
  ArrowUpRight,
  Network,
  ChartNoAxesCombined,
} from 'lucide-vue-next'
import { pagePapers, importSamples, uploadPaper, reparsePaper, type Paper } from '../api'
import { activeConversationId, conversationState } from '../desktopState'
import { openLibrarySource, sourceError } from '../sourceApi'
import { normalizeSourceIds, sourceReaderLocation, librarySourceTypeLabel } from '../sourceSelection'
import BibliographyImport from '../components/BibliographyImport.vue'
import LibraryIndexVersions from '../components/LibraryIndexVersions.vue'

const router = useRouter()
const papers = ref<Paper[]>([])
const edgeCount = ref(0)
const libraryCount=ref(0),totalPages=ref(0),parsedCount=ref(0),matchedCount=ref(0),offset=ref(0),nextOffset=ref<number|null>(null)
const pageSize=40
let request:AbortController|null=null,loadGeneration=0,searchTimer:ReturnType<typeof setTimeout>|undefined
const loading = ref(false)
const loadError = ref('')
const uploading = ref(false)
const loadingSamples = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const openingPaper = ref('')
const bibliographyOpen = ref(false)
const fixedVersions=ref<Record<string,boolean>>({})
function showFixedVersions(paper:Paper){fixedVersions.value[paper.id]=!fixedVersions.value[paper.id]}
async function openPaper(paper: Paper, ask = false, libraryRevisionId?:string) {
  if (paper.status === 'metadata_only') {
    ElMessage.info('此记录仅有书目信息，尚未取得全文。请在 Zotero 条目与附件中保存并附加原件。')
    bibliographyOpen.value = true
    return
  }
  if (!paper.source_revision_id && !libraryRevisionId) {
    void router.push(ask ? { path: '/chat', query: { paper: paper.id } } : `/reader/${paper.id}`)
    return
  }
  if (openingPaper.value) return
  openingPaper.value = paper.id
  try {
    const cid = activeConversationId.value
    const source = await openLibrarySource(paper.id, cid, libraryRevisionId)
    if (ask) {
      const state = conversationState(cid)
      await state.ready
      state.update({ ...state.value.value, paper_id: null, source_revision_ids: normalizeSourceIds([...(state.value.value.source_revision_ids || []), source.revision_id]) })
      await state.flush()
      void router.push({ path: '/chat', query: { conv: cid } })
    } else void router.push(sourceReaderLocation(source.revision_id, source.conversation_id || cid))
  } catch (error) { ElMessage.error(sourceError(error)) }
  finally { openingPaper.value = '' }
}

const keyword = ref('')
const statusFilter = ref('all')
const sortKey = ref('recent')
const selected = ref(new Set<string>())
const allVisibleSelected = computed(
  () => filtered.value.length > 0 && filtered.value.every((paper) => selected.value.has(paper.id))
)
const someVisibleSelected = computed(() =>
  filtered.value.some((paper) => selected.value.has(paper.id))
)
function togglePaper(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}
function toggleVisible(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const next = new Set(selected.value)
  for (const paper of filtered.value) {
    if (checked) next.add(paper.id)
    else next.delete(paper.id)
  }
  selected.value = next
}
function openComparison() {
  if (selected.value.size < 2) {
    ElMessage.info('请至少选择 2 篇已解析文献')
    return
  }
  router.push({
    path: '/compare',
    query: { papers: [...selected.value] },
  })
}

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

async function refresh(at=offset.value) {
  const token=++loadGeneration
  request?.abort();request=new AbortController()
  loading.value = true
  loadError.value = ''
  try {
    const data=await pagePapers({q:keyword.value,status:statusFilter.value,sort:sortKey.value,offset:at,limit:pageSize},request.signal)
    if(token!==loadGeneration)return
    if(at>0&&!data.items.length&&data.total){await refresh(Math.max(0,Math.floor((data.total-1)/pageSize)*pageSize));return}
    papers.value=data.items;offset.value=data.offset;nextOffset.value=data.next_offset;matchedCount.value=data.total
    libraryCount.value=data.statistics.papers;totalPages.value=data.statistics.pages;parsedCount.value=data.statistics.parsed;edgeCount.value=data.statistics.internal_references
  } catch (err: any) {
    if(token===loadGeneration&&err?.code!=='ERR_CANCELED')loadError.value = err?.message || '文献库加载失败，请重试'
  } finally {
    if(token===loadGeneration)loading.value = false
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
    } else if (paper.version_duplicate) {
      const incoming = paper.incoming_version == null ? '未知' : `v${paper.incoming_version}`
      const existing = paper.existing_version == null ? '未知' : `v${paper.existing_version}`
      const recommended =
        paper.recommend === 'incoming' ? `本次上传的 ${incoming}` : `库中已有的 ${existing}`
      await ElMessageBox.alert(
        '检测到同一论文的不同版本，两版记录均已保留、未自动替换。\n' +
          `· 库中已有版本：${existing}\n` +
          `· 本次上传版本：${incoming}\n` +
          `· 建议采用：${recommended}\n` +
          '可在文献库中手动选择保留哪一版。',
        '发现同论文不同版本',
        { confirmButtonText: '知道了', type: 'info' }
      )
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

// ---- 重新解析 / 重试 OCR ----
const reparsing = ref<Record<string, boolean>>({})
async function reparse(p: Paper) {
  reparsing.value[p.id] = true
  try {
    const updated = await reparsePaper(p.id)
    if (updated.status === 'parsed') {
      ElMessage.success('重新解析成功，已建立索引')
    } else if (updated.status === 'needs_ocr') {
      ElMessage.warning('仍未识别到有效正文：' + (updated.error || '待 OCR'))
    } else {
      ElMessage.info(`当前状态：${updated.status}`)
    }
    await refresh()
  } catch (err: any) {
    ElMessage.error(err?.message || '重新解析失败')
  } finally {
    reparsing.value[p.id] = false
  }
}

const filtered = computed(() => papers.value)
watch([keyword,statusFilter,sortKey],()=>{request?.abort();loadGeneration++;if(searchTimer)clearTimeout(searchTimer);searchTimer=setTimeout(()=>void refresh(0),180)})
onBeforeUnmount(()=>{loadGeneration++;request?.abort();if(searchTimer)clearTimeout(searchTimer)})

function statusBadge(p: Paper) {
  if (p.status === 'metadata_only') return { text: '仅书目 · 无全文', cls: 'mock' }
  if (p.status === 'parsed') return { text: '已解析', cls: 'ok' }
  if (p.status === 'failed') return { text: '解析失败', cls: 'fail' }
  if (p.status === 'needs_ocr') return { text: '待 OCR', cls: 'mock' }
  return { text: '解析中', cls: 'mock' }
}

function fmtDate(s: string) {
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? '—' : `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`
}

onMounted(()=>void refresh())
</script>

<template>
  <div class="page library-page">
    <div class="page-head">
      <div>
        <h1 class="page-title">文献库</h1>
        <p class="page-desc">阅读、整理，以及下一次研究的起点。</p>
      </div>
      <div class="toolbar">
        <button class="el-button" @click="bibliographyOpen = !bibliographyOpen">书目 / Zotero</button>
        <button class="el-button" :disabled="loadingSamples || uploading" @click="loadSamples">
          {{ loadingSamples ? '加载中…' : '加载示例论文' }}
        </button>
        <button
          class="el-button el-button--primary"
          :disabled="uploading"
          @click="fileInput?.click()"
        >
          <Plus :size="16" />{{ uploading ? '上传解析中…' : '导入文献' }}
        </button>
        <input ref="fileInput" type="file" accept="application/pdf,.pdf" hidden @change="onPick" />
      </div>
    </div>
    <BibliographyImport v-if="bibliographyOpen" @changed="refresh(0)" />
    <div v-if="loadError" class="library-error" role="alert">
      {{ loadError }} <button class="el-button" @click="refresh()">重新加载</button>
    </div>
    <div class="library-stats" aria-label="文献统计">
      <span
        ><strong>{{ libraryCount }}</strong> 篇文献</span
      ><span>·</span><span>{{ parsedCount }} 篇已解析</span><span>·</span
      ><span>{{ totalPages }} 页 · {{ edgeCount }} 条已匹配引用记录</span>
    </div>
    <div class="lib-toolbar">
      <label class="lib-search">
        <Search :size="16" /><input
          v-model="keyword"
          aria-label="搜索文献"
          placeholder="搜索文献、作者…"
        />
      </label>
      <select v-model="statusFilter" class="lib-select" aria-label="按解析状态筛选">
        <option value="all">全部状态</option>
        <option value="parsed">已解析</option>
        <option value="processing">解析中</option>
        <option value="needs_ocr">待 OCR</option>
        <option value="failed">解析失败</option>
        <option value="metadata_only">仅书目 · 无全文</option>
      </select>
      <select v-model="sortKey" class="lib-select" aria-label="文献排序">
        <option value="recent">最近导入</option>
        <option value="pages">页数最多</option>
        <option value="title">标题排序</option>
      </select>
    </div>
    <div v-if="loading" class="empty" role="status">正在读取文献库…</div>
    <div v-else-if="!libraryCount && !loadError" class="library-empty">
      <BookOpen :size="36" :stroke-width="1.3" />
      <h2>让研究从一篇文献开始</h2>
      <p>上传 PDF 后即可阅读原文、检索证据并提问，也可以先加载示例论文体验。</p>
      <button
        class="el-button el-button--primary"
        :disabled="uploading"
        @click="fileInput?.click()"
      >
        导入第一篇文献
      </button>
    </div>
    <div v-else-if="!filtered.length && !loadError" class="empty">
      没有匹配的论文，试试调整关键词或筛选条件。
    </div>
    <div v-else-if="filtered.length" class="library-table-wrap">
      <table class="library-table">
        <thead>
          <tr>
            <th class="selection-cell">
              <input
                type="checkbox"
                aria-label="全选当前文献"
                :checked="allVisibleSelected"
                :indeterminate="someVisibleSelected && !allVisibleSelected"
                @change="toggleVisible"
              />
            </th>
            <th>文献名称</th>
            <th class="date-cell">导入日期</th>
            <th>状态</th>
            <th class="pages-cell">页数</th>
            <th><span class="sr-label">文献操作</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in filtered" :key="p.id" :class="{ selected: selected.has(p.id) }">
            <td class="selection-cell">
              <input
                type="checkbox"
                :aria-label="`选择 ${p.title}`"
                :checked="selected.has(p.id)"
                @change="togglePaper(p.id)"
              />
            </td>
            <td class="paper-cell">
              <div class="pdf-icon" aria-hidden="true">{{ p.status === 'metadata_only' ? '书目' : librarySourceTypeLabel(p) }}</div>
              <div class="paper-info">
                <button class="paper-title" :disabled="!!openingPaper" @click="openPaper(p)">
                  {{ p.title }}
                </button>
                <div v-if="p.authors" class="paper-authors">{{ p.authors }}</div>
                <div v-if="p.error" class="paper-error">{{ p.error }}</div>
                <LibraryIndexVersions v-if="fixedVersions[p.id]" :paper-id="p.id" :conversation-id="activeConversationId" @open="openPaper(p,false,$event)" @changed="refresh()" />
              </div>
            </td>
            <td class="paper-date date-cell">{{ fmtDate(p.created_at) }}</td>
            <td>
              <span class="paper-status" :class="statusBadge(p).cls"
                ><i></i>{{ statusBadge(p).text }}</span
              >
            </td>
            <td class="paper-number pages-cell">{{ p.page_count || '—' }}</td>
            <td>
              <div class="paper-actions">
                <button v-if="p.status!=='metadata_only'" class="retry-button" @click="showFixedVersions(p)">固定版本</button>
                <button
                  class="paper-action"
                  :aria-label="`阅读 ${p.title}`"
                  title="阅读原文"
                  @click="openPaper(p)"
                >
                  <ArrowUpRight :size="15" />
                </button>
                <button
                  class="paper-action"
                  :aria-label="`针对 ${p.title} 提问`"
                  title="针对文献提问"
                  @click="openPaper(p, true)"
                >
                  <SquarePen :size="15" />
                </button>
                <button
                  v-if="p.status === 'needs_ocr' || p.status === 'failed'"
                  class="retry-button"
                  :disabled="reparsing[p.id]"
                  @click="reparse(p)"
                >
                  {{ reparsing[p.id] ? '解析中…' : '重新解析' }}
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <nav v-if="matchedCount" class="library-pagination" aria-label="文献分页">
      <button class="el-button" :disabled="loading || offset===0" @click="refresh(Math.max(0,offset-pageSize))">上一页文献</button>
      <span>第 {{offset+1}}–{{offset+papers.length}} 篇，共 {{matchedCount}} 篇</span>
      <button class="el-button" :disabled="loading || nextOffset===null" @click="nextOffset!==null && refresh(nextOffset)">下一页文献</button>
    </nav>
    <div v-if="libraryCount" class="library-footer">
      <span
        >已选择 {{ selected.size }} 篇<template v-if="keyword || statusFilter !== 'all'">
          · 当前筛选 {{ matchedCount }} 篇</template
        ></span
      >
      <div class="toolbar">
        <button v-if="selected.size" class="el-button" @click="selected=new Set()">清除跨页选择</button>
        <button class="el-button" :disabled="selected.size < 2" @click="openComparison">
          <ChartNoAxesCombined :size="14" />跨文献对比</button
        ><button class="el-button" @click="router.push('/graph')">
          <Network :size="15" />查看图谱
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.library-pagination { display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap;margin:16px 0;font-size:12px; }
.page-title,
.page-desc {
  margin-bottom: 0;
  margin-top: 0;
}
.page-desc {
  margin-top: 8px;
}
.toolbar .el-button {
  gap: 7px;
}
.library-stats {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  color: var(--fn-text-secondary);
  font-size: 12px;
  margin-bottom: 26px;
}
.library-stats strong {
  font-size: 15px;
  color: var(--fn-text-primary);
  font-weight: 600;
  margin-right: 3px;
}
.lib-toolbar {
  display: flex;
  gap: 12px;
  padding: 12px 0;
  border-top: 1px solid var(--fn-border-primary);
  border-bottom: 1px solid var(--fn-border-primary);
}
.lib-search {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 1;
  min-width: 160px;
  color: var(--fn-text-tertiary);
}
.lib-search input {
  border: 0;
  background: transparent;
  width: 100%;
  padding: 8px 0;
  font-size: 13px;
  color: var(--fn-text-primary);
  outline: none;
}
.lib-search:focus-within {
  box-shadow: 0 1px var(--fn-brand);
}
.lib-select {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  padding: 7px 10px;
  color: var(--fn-text-secondary);
  font-size: 12px;
}
.library-table-wrap {
  overflow-x: auto;
}
.library-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}
.library-table th {
  font-size: 11px;
  font-weight: 500;
  color: var(--fn-text-tertiary);
  padding: 14px 12px;
  border-bottom: 1px solid var(--fn-border-primary);
  white-space: nowrap;
}
.library-table td {
  padding: 20px 12px;
  border-bottom: 1px solid var(--fn-border-primary);
  vertical-align: middle;
}
.library-table tbody tr:hover {
  background: var(--fn-bg-secondary);
}
.library-table .paper-cell {
  min-width: 260px;
  max-width: 620px;
}
.pdf-icon {
  float: left;
  margin: 2px 14px 0 0;
  width: 29px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--fn-border-primary);
  border-radius: 3px;
  font:
    8px ui-monospace,
    monospace;
  color: var(--fn-text-secondary);
  flex-shrink: 0;
}
.paper-info {
  overflow: hidden;
}
.paper-title {
  border: 0;
  padding: 0;
  background: transparent;
  text-align: left;
  color: var(--fn-text-primary);
  font-size: 13px;
  line-height: 1.65;
  font-weight: 500;
  cursor: pointer;
}
.paper-title:hover {
  color: var(--fn-brand);
}
.paper-authors,
.paper-abstract {
  margin-top: 5px;
  color: var(--fn-text-secondary);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.paper-abstract {
  color: var(--fn-text-tertiary);
  max-width: 560px;
}
.paper-error {
  color: var(--fn-danger);
  font-size: 12px;
  margin-top: 6px;
}
.paper-number,
.paper-date {
  color: var(--fn-text-secondary);
  font-size: 12px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.paper-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
}
.paper-action {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.paper-action:hover {
  color: var(--fn-brand);
  background: var(--fn-bg-active);
}
.retry-button {
  border: 0;
  background: transparent;
  color: var(--fn-brand);
  white-space: nowrap;
  cursor: pointer;
}
.library-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 0;
  color: var(--fn-text-tertiary);
  font-size: 12px;
}
.library-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 70px 24px;
  color: var(--fn-text-secondary);
}
.library-empty svg {
  color: var(--fn-brand);
}
.library-empty h2 {
  font-size: 18px;
  font-weight: 500;
  color: var(--fn-text-primary);
  margin: 20px 0 0;
}
.library-empty p {
  font-size: 13px;
  margin: 12px 0 24px;
  max-width: 440px;
  line-height: 1.8;
}
.library-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
  margin-bottom: 16px;
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
}
.sr-label {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
@media (max-width: 1000px) {
  .lib-toolbar {
    flex-wrap: wrap;
  }
  .lib-search {
    min-width: 100%;
  }
  .library-table .date-cell,
  .library-table .pages-cell {
    display: none;
  }
  .pdf-icon {
    display: none;
  }
}

.library-table .selection-cell {
  width: 28px;
  padding-left: 0;
  padding-right: 10px;
}
.selection-cell input {
  width: 13px;
  height: 13px;
  margin: 0;
  accent-color: var(--fn-brand);
  cursor: pointer;
}
.library-table tbody tr.selected {
  background: var(--fn-bg-brand-light);
}
.paper-status {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.paper-status i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--fn-text-tertiary);
}
.paper-status.ok i {
  background: var(--fn-success);
}
.paper-status.fail {
  color: var(--fn-danger);
}
.paper-status.fail i {
  background: var(--fn-danger);
}
.paper-status.mock i {
  background: var(--fn-warning);
}
.library-footer {
  margin-top: 8px;
  padding: 12px 0;
  border-top: 1px solid var(--fn-border-primary);
}
.library-footer .el-button {
  min-height: 30px;
  height: 30px;
  padding: 0 10px;
  font-size: 11px;
}
.library-table th {
  padding-top: 12px;
  padding-bottom: 12px;
}
.library-table td {
  padding-top: 22px;
  padding-bottom: 22px;
}
.library-table .paper-cell {
  min-width: 250px;
}
.lib-search {
  max-width: 430px;
  margin-right: auto;
}
@media (max-width: 760px) {
  .library-footer {
    flex-wrap: wrap;
  }
  .library-table .paper-cell {
    min-width: 200px;
  }
}
</style>
