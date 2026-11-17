<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, nextTick, ref, shallowRef, watch } from 'vue'
import {
  ChevronLeft,
  ChevronRight,
  Link,
  SquarePen,
  ArrowUpRight,
  PanelRightClose,
  PanelRightOpen,
  FileText,
  ZoomIn,
  ZoomOut,
  RotateCcw,
} from 'lucide-vue-next'
import { useRoute, useRouter } from 'vue-router'
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { localSessionHeaders } from '../localSession'
import { getPaper, paperFileUrl, getCitations, type Paper, type CitationData } from '../api'
import { activeConversationId } from '../desktopState'
import { openLibrarySource } from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'
import { openFixedLibrary, createSourceLocator } from '../researchApi'
import { ensureSourceConversation } from '../sourceApi'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

interface Rect {
  x: number
  y: number
  w: number
  h: number
}
interface Mark extends Rect {
  nums: number[]
}

const route = useRoute()
const router = useRouter()
const paperId = computed(() => route.params.id as string)
const PDF_ZOOM_KEY = 'scholarflow:reader-zoom'
const MIN_PDF_ZOOM = 0.6
const MAX_PDF_ZOOM = 2
const PDF_ZOOM_STEP = 0.1

const paper = ref<Paper | null>(null)
// shallowRef：pdfjs 文档对象含类私有字段，不能被 Vue 深度响应式 Proxy 包裹
const pdfDoc = shallowRef<any>(null)
const pageNum = ref(1)
const pageCount = ref(0)
const pageWidth = ref(0)
const pageHeight = ref(0)
const scale = ref(1.3)
const pdfZoom = ref(readPdfZoom())
const canvasRef = ref<HTMLCanvasElement | null>(null)
const error = ref('')
const urlHighlight = ref<Rect | null>(null)
const jumpHighlight = ref<Rect | null>(null)
const jumpSource = ref<{ page: number; bbox: number[] } | null>(null)
const marks = ref<Mark[]>([])
const citationData = ref<CitationData | null>(null)
const showPanel = ref(false)
const showSidebar = ref(true)
const loading = ref(true)
const stageRef = ref<HTMLDivElement | null>(null)
const highlightRef = ref<HTMLDivElement | null>(null)
let renderTask: any = null
let renderVersion = 0
let loadVersion = 0
let stageObserver: ResizeObserver | null = null
let resizeTimer: ReturnType<typeof setTimeout> | null = null

const TARGET_WIDTH = 670
const MARK_RE = /[\[（(](\d+(?:\s*[,\-–—]\s*\d+)*)[\]）)]/g

function readPdfZoom() {
  const stored = Number(localStorage.getItem(PDF_ZOOM_KEY))
  return Number.isFinite(stored) && stored > 0
    ? Math.min(MAX_PDF_ZOOM, Math.max(MIN_PDF_ZOOM, stored))
    : 1
}

function setPdfZoom(direction: -1 | 0 | 1) {
  const next =
    direction === 0
      ? 1
      : Math.min(MAX_PDF_ZOOM, Math.max(MIN_PDF_ZOOM, pdfZoom.value + direction * PDF_ZOOM_STEP))
  pdfZoom.value = Number(next.toFixed(1))
  localStorage.setItem(PDF_ZOOM_KEY, String(pdfZoom.value))
  void renderPage()
}

function onReaderZoom(event: Event) {
  const detail = (event as CustomEvent<{ direction: -1 | 0 | 1 }>).detail
  if (detail && [-1, 0, 1].includes(detail.direction)) setPdfZoom(detail.direction)
}

function parseBbox(): number[] | null {
  const raw = route.query.bbox
  if (typeof raw !== 'string' || !raw) return null
  const parts = raw.split(',').map(Number)
  return parts.length === 4 && parts.every(Number.isFinite) ? parts : null
}

// 后端 bbox 为 pdfplumber 坐标：显示方向、左上角原点、PDF 点坐标 [x0, top, x1, bottom]。
// pdfjs 的 convertToViewportRectangle 需要未旋转的 PDF 原生坐标（左下角原点、y 向上）。
// 这里用 scale=1 的显示 viewport 的 convertToPdfPoint 做逆转换，由 pdfjs 统一处理
// y 翻转与页面旋转，不手写固定高度，因此对不同页面尺寸、多页、旋转页面都通用。
function toPdfRect(bbox: number[], page: any): number[] {
  const displayVp = page.getViewport({ scale: 1 })
  const [x0, top, x1, bottom] = bbox
  const tl = displayVp.convertToPdfPoint(x0, top)
  const br = displayVp.convertToPdfPoint(x1, bottom)
  return [
    Math.min(tl[0], br[0]),
    Math.min(tl[1], br[1]),
    Math.max(tl[0], br[0]),
    Math.max(tl[1], br[1]),
  ]
}

function parseMarkNums(inner: string): number[] {
  const nums: number[] = []
  for (const part of inner.split(',')) {
    const t = part.trim()
    if (!t) continue
    if (/[\-–—]/.test(t)) {
      const ends = t.split(/[\-–—]/).map(Number)
      if (ends.length === 2 && ends[0] <= ends[1]) {
        for (let i = ends[0]; i <= ends[1]; i++) nums.push(i)
      }
    } else {
      const n = Number(t)
      if (!Number.isNaN(n)) nums.push(n)
    }
  }
  return nums
}

// 计算 item 内从 start 起 len 个字符在视口中的矩形（按字符宽度比例估算）
function itemCharRect(viewport: any, item: any, start: number, len: number): Rect {
  const tx = item.transform
  const total = Math.max(1, item.str.length)
  const charW = item.width / total
  const x0 = tx[4] + charW * start
  const x1 = tx[4] + charW * (start + len)
  const h = item.height || Math.abs(tx[3]) || 8
  const yBase = tx[5]
  const p1 = viewport.convertToViewportPoint(x0, yBase + h)
  const p2 = viewport.convertToViewportPoint(x1, yBase)
  return {
    x: Math.min(p1[0], p2[0]),
    y: Math.min(p1[1], p2[1]),
    w: Math.abs(p2[0] - p1[0]),
    h: Math.abs(p2[1] - p1[1]),
  }
}

async function buildMarkLayer(page: any, viewport: any) {
  const valid = new Set(citationData.value?.references.map((r) => r.ref_index) || [])
  if (valid.size === 0) {
    return []
  }
  const content = await page.getTextContent()
  const result: Mark[] = []
  for (const item of content.items) {
    const str: string = item.str || ''
    MARK_RE.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = MARK_RE.exec(str))) {
      const nums = parseMarkNums(m[1]).filter((n) => valid.has(n))
      if (nums.length === 0) continue
      const rect = itemCharRect(viewport, item, m.index, m[0].length)
      result.push({ ...rect, nums })
    }
  }
  return result
}

async function renderPage() {
  const doc = pdfDoc.value
  if (!doc || !canvasRef.value) return
  const current = ++renderVersion
  if (renderTask) {
    renderTask.cancel()
    try {
      await renderTask.promise
    } catch {
      /* superseded render */
    }
  }
  const page = await doc.getPage(pageNum.value)
  if (current !== renderVersion) return
  const base = page.getViewport({ scale: 1 })
  const available = stageRef.value ? stageRef.value.clientWidth - 56 : TARGET_WIDTH
  const fitScale = Math.min(1.8, Math.max(240, Math.min(TARGET_WIDTH, available)) / base.width)
  scale.value = fitScale * pdfZoom.value
  const viewport = page.getViewport({ scale: scale.value })
  const canvas = canvasRef.value
  const ctx = canvas.getContext('2d')!
  canvas.width = viewport.width
  canvas.height = viewport.height
  pageWidth.value = viewport.width
  pageHeight.value = viewport.height
  renderTask = page.render({ canvasContext: ctx, viewport })
  try {
    await renderTask.promise
  } catch (err: any) {
    if (err?.name === 'RenderingCancelledException') return
    throw err
  }
  if (current !== renderVersion) return

  const bbox = pageNum.value === Number(route.query.page || 1) ? parseBbox() : null
  if (bbox) {
    const rect = viewport.convertToViewportRectangle(toPdfRect(bbox, page))
    urlHighlight.value = {
      x: Math.min(rect[0], rect[2]),
      y: Math.min(rect[1], rect[3]),
      w: Math.abs(rect[2] - rect[0]),
      h: Math.abs(rect[3] - rect[1]),
    }
  } else {
    urlHighlight.value = null
  }
  const jump = jumpSource.value
  if (jump && jump.page === pageNum.value) {
    const rect = viewport.convertToViewportRectangle(toPdfRect(jump.bbox, page))
    jumpHighlight.value = {
      x: Math.min(rect[0], rect[2]),
      y: Math.min(rect[1], rect[3]),
      w: Math.abs(rect[2] - rect[0]),
      h: Math.abs(rect[3] - rect[1]),
    }
  } else jumpHighlight.value = null
  const nextMarks = await buildMarkLayer(page, viewport)
  if (current === renderVersion) marks.value = nextMarks
}

async function goto(page: number) {
  if (!Number.isInteger(page) || page < 1 || page > pageCount.value) return
  jumpSource.value = null
  jumpHighlight.value = null
  pageNum.value = page
  await renderPage()
}

// 正文标记 -> 参考文献
function jumpToReference(nums: number[]) {
  const ref = citationData.value?.references.find((r) => nums.includes(r.ref_index))
  if (!ref || ref.page == null) return
  jumpTo(ref.page, ref.bbox)
}

// 参考文献条目 -> 正文首次引用处
function jumpToInline(num: number) {
  const loc = citationData.value?.inline_citations[String(num)]?.[0]
  if (!loc) return
  jumpTo(loc.page, loc.bbox)
}

async function jumpTo(page: number, bbox: number[] | null) {
  if (!pdfDoc.value || !Number.isInteger(page) || page < 1 || page > pageCount.value) return
  jumpSource.value = bbox?.length === 4 && bbox.every(Number.isFinite) ? { page, bbox } : null
  pageNum.value = page
  await renderPage()
  await nextTick()
  stageRef.value
    ?.querySelector('.bbox-highlight.jump')
    ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

function gotoChat() {
  router.push({ path: '/chat', query: { paper: paperId.value } })
}

async function locateEvidence() {
  if (!parseBbox()) return
  await goto(Math.min(Math.max(1, Number(route.query.page) || 1), pageCount.value))
  await nextTick()
  highlightRef.value?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

async function loadPaper() {
  const current = ++loadVersion
  const id = paperId.value
  loading.value = true
  error.value = ''
  paper.value = null
  marks.value = []
  citationData.value = null
  urlHighlight.value = null
  jumpHighlight.value = null
  jumpSource.value = null
  pageCount.value = 0
  ++renderVersion
  renderTask?.cancel()
  const oldDoc = pdfDoc.value
  pdfDoc.value = null
  if (oldDoc) await oldDoc.destroy()
  try {
    localStorage.setItem('scholarflow:last-paper', id)
    const metadata = await getPaper(id)
    if (current !== loadVersion) return
    if (route.query.classic !== '1') {
      const cid =
        typeof route.query.conversation === 'string'
          ? route.query.conversation
          : activeConversationId.value || `reading_${crypto.randomUUID()}`
      await ensureSourceConversation(cid)
      const fixed = await openFixedLibrary(id, cid)
      if (current !== loadVersion) return
      let locator: string | undefined
      if (route.query.page) {
        const box =
          typeof route.query.bbox === 'string' ? route.query.bbox.split(',').map(Number) : undefined
        const value = await createSourceLocator(fixed.revision_id, cid, {
          kind: 'pdf_page',
          page_index: Math.max(0, Number(route.query.page) - 1),
          ...(box?.length === 4 ? { bbox: box } : {}),
        })
        locator = value.locator_id
      }
      if (current !== loadVersion) return
      await router.replace({
        path: `/sources/${fixed.revision_id}`,
        query: { conversation: cid, paper: id, ...(locator ? { locator } : {}) },
      })
      return
    }
    if (metadata.source_revision_id) {
      const cid = activeConversationId.value
      const source = await openLibrarySource(id, cid)
      if (current !== loadVersion) return
      await router.replace(sourceReaderLocation(source.revision_id, source.conversation_id || cid))
      return
    }
    paper.value = metadata
    const [data, citations] = await Promise.all([
      pdfjsLib.getDocument({ url: paperFileUrl(id), httpHeaders: localSessionHeaders(paperFileUrl(id)), isEvalSupported: false }).promise,
      getCitations(id).catch(() => null),
    ])
    if (current !== loadVersion) {
      await data.destroy()
      return
    }
    pdfDoc.value = data
    citationData.value = citations
    pageCount.value = data.numPages
    pageNum.value = Math.min(Math.max(1, Number(route.query.page) || 1), data.numPages)
    await nextTick()
    await renderPage()
  } catch (err: any) {
    if (current === loadVersion) error.value = err?.message || '原文加载失败'
  } finally {
    if (current === loadVersion) loading.value = false
  }
}

onMounted(() => {
  window.addEventListener('scholarflow:reader-zoom', onReaderZoom)
  stageObserver = new ResizeObserver(() => {
    if (resizeTimer) clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => {
      if (pdfDoc.value) renderPage().catch(() => {})
    }, 100)
  })
  if (stageRef.value) stageObserver.observe(stageRef.value)
  loadPaper()
})
watch(() => route.params.id, loadPaper)
watch(
  () => [route.query.page, route.query.bbox],
  async () => {
    if (!pdfDoc.value) return
    await goto(Math.min(Math.max(1, Number(route.query.page) || 1), pageCount.value))
  }
)
onBeforeUnmount(() => {
  window.removeEventListener('scholarflow:reader-zoom', onReaderZoom)
  ++loadVersion
  ++renderVersion
  if (resizeTimer) clearTimeout(resizeTimer)
  stageObserver?.disconnect()
  renderTask?.cancel()
  pdfDoc.value?.destroy()
})
</script>

<template>
  <div class="page reader-page" :class="{ 'sidebar-closed': !showSidebar }">
    <div class="reader-main">
      <div class="reader-tools">
        <span class="reader-name" :title="paper?.title">{{ paper?.title || '版式阅读' }}</span>
        <button
          class="reader-icon"
          aria-label="上一页"
          title="上一页"
          :disabled="loading || pageNum <= 1"
          @click="goto(pageNum - 1)"
        >
          <ChevronLeft :size="14" />
        </button>
        <span class="page-count">{{ pageNum }} / {{ pageCount || '—' }}</span>
        <button
          class="reader-icon"
          aria-label="下一页"
          title="下一页"
          :disabled="loading || pageNum >= pageCount"
          @click="goto(pageNum + 1)"
        >
          <ChevronRight :size="14" />
        </button>
        <button
          class="reader-icon"
          aria-label="回到引用高亮"
          title="回到引用高亮"
          :disabled="!parseBbox() || loading"
          @click="locateEvidence"
        >
          <Link :size="15" />
        </button>
        <button
          class="reader-icon"
          aria-label="缩小原文"
          title="缩小原文"
          :disabled="pdfZoom <= MIN_PDF_ZOOM"
          @click="setPdfZoom(-1)"
        >
          <ZoomOut :size="15" />
        </button>
        <span class="reader-zoom-value" aria-live="polite">{{ Math.round(pdfZoom * 100) }}%</span>
        <button
          class="reader-icon"
          aria-label="放大原文"
          title="放大原文"
          :disabled="pdfZoom >= MAX_PDF_ZOOM"
          @click="setPdfZoom(1)"
        >
          <ZoomIn :size="15" />
        </button>
        <button
          class="reader-icon"
          aria-label="恢复适合窗口"
          title="恢复适合窗口"
          @click="setPdfZoom(0)"
        >
          <RotateCcw :size="14" />
        </button>
        <button
          class="reader-icon"
          aria-label="对这篇文献提问"
          title="对这篇文献提问"
          @click="gotoChat"
        >
          <SquarePen :size="15" />
        </button>
        <button
          class="reader-icon sidebar-toggle"
          :aria-label="showSidebar ? '收起引用与资料' : '展开引用与资料'"
          :title="showSidebar ? '收起引用与资料' : '展开引用与资料'"
          @click="showSidebar = !showSidebar"
        >
          <PanelRightClose v-if="showSidebar" :size="15" /><PanelRightOpen v-else :size="15" />
        </button>
      </div>
      <div ref="stageRef" class="paper-stage">
        <div v-if="error" class="reader-state error" role="alert">
          {{ error }}<button class="el-button" @click="loadPaper">重新加载</button>
        </div>
        <div v-if="loading && !error" class="reader-state" role="status">正在加载原文…</div>
        <div v-show="!error" class="canvas-wrap">
          <div class="pdf-page" :style="{ width: pageWidth + 'px', height: pageHeight + 'px' }">
            <canvas ref="canvasRef"></canvas>
            <div
              v-if="urlHighlight"
              ref="highlightRef"
              class="bbox-highlight"
              :style="{
                left: urlHighlight.x + 'px',
                top: urlHighlight.y + 'px',
                width: urlHighlight.w + 'px',
                height: urlHighlight.h + 'px',
              }"
            ></div>
            <div
              v-if="jumpHighlight"
              class="bbox-highlight jump"
              :style="{
                left: jumpHighlight.x + 'px',
                top: jumpHighlight.y + 'px',
                width: jumpHighlight.w + 'px',
                height: jumpHighlight.h + 'px',
              }"
            ></div>
            <button
              v-for="(mk, i) in marks"
              :key="i"
              class="cite-mark"
              :style="{
                left: mk.x + 'px',
                top: mk.y + 'px',
                width: mk.w + 'px',
                height: mk.h + 'px',
              }"
              :title="`跳转到参考文献 ${mk.nums.join(', ')}`"
              :aria-label="`跳转到参考文献 ${mk.nums.join(', ')}`"
              @click="jumpToReference(mk.nums)"
            ></button>
          </div>
        </div>
      </div>
    </div>
    <aside v-if="showSidebar" class="reader-side">
      <h2>引用与资料</h2>
      <span class="current-tag">当前文献</span>
      <div class="detail-title">{{ paper?.title || '正在读取文献信息' }}</div>
      <p class="paper-metadata">
        <template v-if="paper?.authors">{{ paper.authors }}<br /></template
        >{{ paper?.page_count || pageCount || '—' }} 页 · PDF 原文
      </p>
      <div v-if="paper?.abstract" class="quote">{{ paper.abstract }}</div>
      <button v-if="parseBbox()" class="source-navigation" @click="locateEvidence">
        回到选中片段 <ArrowUpRight :size="12" />
      </button>
      <div class="side-divider"></div>
      <div class="section-label">来源</div>
      <a :href="paperFileUrl(paperId)" target="_blank" rel="noopener noreferrer" class="source-link"
        ><FileText :size="13" />打开原始 PDF <ArrowUpRight :size="12"
      /></a>
      <button class="el-button continue-ask" @click="gotoChat">
        <SquarePen :size="14" />继续提问
      </button>
      <div class="side-divider"></div>
      <button class="reference-toggle" :aria-expanded="showPanel" @click="showPanel = !showPanel">
        参考文献 <span>{{ citationData?.references.length || 0 }}</span
        ><ChevronRight :size="13" :class="{ expanded: showPanel }" />
      </button>
      <div v-if="showPanel" class="ref-list">
        <button
          v-for="reference in citationData?.references || []"
          :key="reference.ref_index"
          class="reference-row"
          :title="
            citationData?.inline_citations[String(reference.ref_index)]?.length
              ? '定位正文引用'
              : '未识别到正文引用位置'
          "
          :disabled="!citationData?.inline_citations[String(reference.ref_index)]?.length"
          @click="jumpToInline(reference.ref_index)"
        >
          <strong>[{{ reference.ref_index }}]</strong> {{ reference.raw_text }}
        </button>
        <div v-if="!citationData?.references.length" class="ref-empty">未识别到参考文献条目。</div>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.reader-page {
  width: 100%;
  max-width: none;
  height: 100%;
  min-height: 0;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  overflow: hidden;
  background: var(--fn-bg-secondary);
}
.reader-page.sidebar-closed {
  grid-template-columns: minmax(0, 1fr);
}
.reader-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.reader-tools {
  height: 49px;
  flex-shrink: 0;
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 0 24px;
  border-bottom: 1px solid var(--fn-border-primary);
  font-size: 11px;
  background: var(--fn-bg-primary);
}
.reader-name {
  margin-right: auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fn-text-primary);
}
.reader-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 26px;
  height: 28px;
  border: 0;
  border-radius: 5px;
  background: none;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.reader-icon:hover {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.reader-icon:disabled {
  opacity: 0.35;
  cursor: default;
}
.reader-zoom-value {
  min-width: 34px;
  font-size: 10px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.page-count {
  white-space: nowrap;
  color: var(--fn-text-secondary);
  font-variant-numeric: tabular-nums;
}
.paper-stage {
  padding: 28px;
  overflow: auto;
  flex: 1;
  min-height: 0;
  background: var(--sidebar, var(--fn-bg-secondary));
}
.pdf-page {
  position: relative;
  background: white;
  margin: 0 auto;
  box-shadow: 0 2px 12px #1b315a08;
  flex: none;
}
.pdf-page canvas {
  display: block;
}
.reader-side {
  background: var(--fn-bg-primary);
  border-left: 1px solid var(--fn-border-primary);
  padding: 24px;
  overflow: auto;
  min-width: 0;
}
.reader-side h2 {
  font-size: calc(13px * var(--reading-text-scale, 1));
  font-weight: 600;
  margin: 0 0 20px;
}
.current-tag {
  display: inline-flex;
  padding: 3px 6px;
  font-size: 10px;
  color: var(--fn-text-secondary);
  background: var(--fn-bg-secondary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
}
.detail-title {
  font-size: calc(12px * var(--reading-text-scale, 1));
  font-weight: 500;
  line-height: 1.8;
  margin: 22px 0 12px;
  overflow-wrap: anywhere;
}
.paper-metadata {
  font-size: calc(11px * var(--reading-text-scale, 1));
  line-height: 1.8;
  color: var(--fn-text-secondary);
  margin: 0;
}
.quote {
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.9;
  color: var(--fn-text-secondary);
  border-left: 2px solid var(--fn-border-strong);
  padding-left: 13px;
  margin: 20px 0;
}
.source-navigation {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 20px;
  font-size: 11px;
  color: var(--fn-brand);
  border: 0;
  padding: 0;
  text-align: left;
  background: none;
  cursor: pointer;
}
.side-divider {
  height: 1px;
  background: var(--fn-border-primary);
  margin: 22px 0;
}
.section-label {
  color: var(--fn-text-tertiary);
  font-size: 10px;
  margin-bottom: 13px;
}
.source-link {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--fn-brand);
  text-decoration: none;
}
.continue-ask {
  width: 100%;
  margin-top: 18px;
  gap: 7px;
}
.reference-toggle {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  border: 0;
  padding: 0;
  background: none;
  color: var(--fn-text-primary);
  text-align: left;
  font-size: 11px;
  cursor: pointer;
}
.reference-toggle span {
  color: var(--fn-text-tertiary);
}
.reference-toggle svg {
  margin-left: auto;
  transition: transform 0.15s;
}
.reference-toggle svg.expanded {
  transform: rotate(90deg);
}
.reference-row {
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  padding: 15px 0;
  background: none;
  text-align: left;
  color: var(--fn-text-secondary);
  font-size: calc(11px * var(--reading-text-scale, 1));
  line-height: 1.8;
  cursor: pointer;
  overflow-wrap: anywhere;
}
.reference-row strong {
  color: var(--fn-text-primary);
  font-weight: 500;
}
.reference-row:hover:not(:disabled) {
  color: var(--fn-brand);
}
.reference-row:disabled {
  cursor: default;
}
.ref-empty {
  margin-top: 16px;
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.cite-mark {
  position: absolute;
  border: 0;
  background: rgba(53, 92, 246, 0.18);
  border-radius: 3px;
  cursor: pointer;
  padding: 0;
}
.cite-mark:hover {
  background: rgba(53, 92, 246, 0.4);
}
.bbox-highlight {
  position: absolute;
  border: 2px solid var(--fn-brand);
  background: rgba(53, 92, 246, 0.12);
  pointer-events: none;
}
.reader-state {
  padding: 25px;
  text-align: center;
  color: var(--fn-text-secondary);
  font-size: 12px;
  line-height: 1.8;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.error {
  color: var(--fn-danger);
}
@media (max-width: 1250px) {
  .reader-page {
    grid-template-columns: minmax(0, 1fr) 240px;
  }
  .reader-side {
    padding: 20px;
  }
}
@media (max-width: 1050px) {
  .reader-page {
    position: relative;
    grid-template-columns: minmax(0, 1fr);
  }
  .reader-side {
    position: absolute;
    top: 49px;
    right: 0;
    bottom: 0;
    z-index: 5;
    width: 280px;
    box-shadow: -5px 0 30px #1b315a0c;
  }
}
@media (max-width: 760px) {
  .reader-tools {
    padding: 0 12px;
    gap: 4px;
    font-size: 10px;
  }
  .paper-stage {
    padding: 16px 12px;
  }
}
@media (max-width: 420px) {
  .reader-tools {
    padding: 0 8px;
  }
  .reader-icon {
    width: 22px;
  }
  .reader-zoom-value {
    min-width: 30px;
  }
  .reader-name {
    display: none;
  }
}
</style>
