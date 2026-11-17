<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Download, ZoomIn, ZoomOut, RotateCcw } from 'lucide-vue-next'
import SourcePdf from '../components/SourcePdf.vue'
import SourceVersions from '../components/SourceVersions.vue'
import SourceTableDetails from '../components/SourceTableDetails.vue'
import ResearchNotes from '../components/ResearchNotes.vue'
import DiagnosticsPanel from '../components/DiagnosticsPanel.vue'
import { recordCitationView } from '../onboardingApi'
import {
  getReading,
  saveReading,
  createSourceLocator,
  type ReadingState,
  type Annotation,
} from '../researchApi'
import {
  getSourceRevision,
  getSourceDocument,
  getSourceLocator,
  getSourceText,
  sourceFileUrl,
  sourceError,
  type SourceRevision,
  type SourceDocument,
  type SourceLocator,
  type SourceBlock,
} from '../sourceApi'
import { sourceTypeLabel, cellRangesOverlap, sourceReaderLocation } from '../sourceSelection'
const route = useRoute(),
  router = useRouter()
const revisionId = computed(() => String(route.params.revisionId || ''))
const conversationId = computed(() =>
  typeof route.query.conversation === 'string' ? route.query.conversation : ''
)
const record = ref<SourceRevision | null>(null),
  document = ref<SourceDocument | null>(null),
  locator = ref<SourceLocator | null>(null)
const loading = ref(false),
  error = ref(''),
  zoom = ref(1),
  textStart = ref(1),
  totalLines = ref(0),
  nextLine = ref<number | null>(null)
const textLines = ref<{ number: number; text: string }[]>([]),
  selectedSheet = ref('')
const reading = ref<ReadingState>({ version: 0, position: {}, user_read: false })
const readingError = ref(''),
  readingReady = ref(false)
const annotations = ref<Annotation[]>([]),
  selectedText = ref('')
const selectedLocator = ref<SourceLocator | null>(null)
const scrollElement = ref<HTMLElement | null>(null)
let saveTimer: ReturnType<typeof setTimeout> | undefined
let saving: Promise<void> | null = null
let dirty = false
let explicitRead: boolean | undefined
let readingRevision = '',
  readingConversation = ''
const observedCitationViews = new Set<string>()
const showDiagnostics = ref(false)
function noteCitationRendered(page?: {page_index:number}) {
  const value = locator.value
  if (!value || loading.value || error.value || (page && value.location.page_index !== page.page_index)) return
  const key = conversationId.value + ':' + value.locator_id
  if(observedCitationViews.has(key)) return
  observedCitationViews.add(key)
  void recordCitationView(conversationId.value,revisionId.value,value.locator_id).catch(()=>observedCitationViews.delete(key))
}
function positionChanged(position: ReadingState['position']) {
  if (!readingReady.value) return
  reading.value.position = { ...reading.value.position, ...position, zoom: zoom.value }
  dirty = true
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    void persistReading()
  }, 250)
}
async function persistReading() {
  if (saving) {
    await saving
    if (dirty) await persistReading()
    return
  }
  if (!dirty || !readingReady.value) return
  dirty = false
  const token = generation,
    rid = readingRevision,
    cid = readingConversation,
    snapshot = { ...reading.value, position: { ...reading.value.position } },
    mark = explicitRead
  explicitRead = undefined
  saving = (async () => {
    try {
      const result = await saveReading(rid, cid, snapshot, mark)
      if (token === generation) {
        reading.value.version = result.version
        reading.value.user_read = result.user_read
        readingError.value = ''
      }
    } catch (e) {
      if (token === generation) {
        readingError.value = sourceError(e)
        readingReady.value = false
      }
    }
  })()
  await saving
  saving = null
}
function toggleRead(event: Event) {
  explicitRead = (event.target as HTMLInputElement).checked
  positionChanged({})
  void persistReading()
}
async function captureSelection(selection: { text: string; input: Record<string, unknown> }) {
  const token = generation
  try {
    const value = await createSourceLocator(revisionId.value, conversationId.value, selection.input)
    if (token !== generation) return
    selectedLocator.value = value
    selectedText.value = selection.text
    readingError.value = ''
  } catch (e) {
    if (token === generation) readingError.value = sourceError(e)
  }
}
function selectDocumentText() {
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed || !selection.rangeCount || !scrollElement.value) return
  const range = selection.getRangeAt(0)
  if (
    !scrollElement.value.contains(range.startContainer) ||
    !scrollElement.value.contains(range.endContainer)
  )
    return
  const element = (node: Node) => (node.nodeType === 1 ? (node as Element) : node.parentElement)
  const start = element(range.startContainer)?.closest<HTMLElement>(
    '[data-source-line],[data-source-block]'
  )
  const end = element(range.endContainer)?.closest<HTMLElement>(
    '[data-source-line],[data-source-block]'
  )
  if (!start || !end) return
  let input: Record<string, unknown>
  if (start.dataset.sourceLine && end.dataset.sourceLine)
    input = {
      kind: 'text_lines',
      line_start: Number(start.dataset.sourceLine),
      line_end: Number(end.dataset.sourceLine),
    }
  else {
    const first = blocks.value[Number(start.dataset.sourceBlock)],
      last = blocks.value[Number(end.dataset.sourceBlock)]
    if (!first || !last) return
    if (first.block_id && first.block_id === last.block_id)
      input = { kind: 'word_block', block_id: first.block_id }
    else if (first.block_id || last.block_id) {
      readingError.value = '请在同一内容部件内选段；跨页眉、脚注或表格单元格的内容请分别保存批注。'
      return
    }
    else if (first.paragraph_index && last.paragraph_index)
      input = {
        kind: 'word_paragraphs',
        paragraph_start: first.paragraph_index,
        paragraph_end: last.paragraph_index,
      }
    else if (first.sheet && first.sheet === last.sheet && first.cell_range && last.cell_range)
      input = {
        kind: 'sheet_cells',
        sheet: first.sheet,
        cell_range: `${first.cell_range.split(':')[0]}:${last.cell_range.split(':').at(-1)}`,
      }
    else return
  }
  void captureSelection({ text: selection.toString(), input })
}
async function locateAnnotation(value: SourceLocator) {
  if (value.revision_id !== revisionId.value) {
    readingError.value = '批注不属于当前版本，不能自动映射'
    return
  }
  await router.replace({ query: { ...route.query, locator: value.locator_id } })
}
const isPdf = computed(
  () =>
    record.value?.media_type === 'application/pdf' &&
    record.value.text_origin !== 'ocr_text' &&
    locator.value?.kind !== 'text_lines'
)
const fileUrl = computed(() => sourceFileUrl(revisionId.value, conversationId.value))
const blocks = computed(() =>
  (document.value?.blocks || []).filter(
    (block) => !selectedSheet.value || block.sheet === selectedSheet.value
  )
)
const sheets = computed(() => [
  ...new Set(
    (document.value?.sheets || [])
      .map((sheet) => (typeof sheet === 'string' ? sheet : sheet.name))
      .filter((name): name is string => Boolean(name))
  ),
])
let generation = 0
let pageRequest = 0
async function loadText(start: number, token = generation) {
  const request = ++pageRequest
  const page = await getSourceText(revisionId.value, conversationId.value, start)
  if (token !== generation || request !== pageRequest) return
  textStart.value = page.line_start
  totalLines.value = page.total_lines
  nextLine.value = page.next_line
  textLines.value = page.text
    .split(/\r?\n/)
    .map((text, index) => ({ number: page.line_start + index, text }))
    .filter((line) => line.number <= page.line_end)
}
async function load() {
  const token = ++generation
  if (dirty) await persistReading()
  if (token !== generation) return
  pageRequest++
  loading.value = true
  error.value = ''
  record.value = null
  locator.value = null
  document.value = null
  textLines.value = []
  readingReady.value = false
  annotations.value = []
  selectedLocator.value = null
  selectedText.value = ''
  readingError.value = ''
  try {
    if (!conversationId.value) throw Error('缺少来源所属会话，请从会话资料或引用重新打开')
    const item = await getSourceRevision(revisionId.value, conversationId.value)
    if (token !== generation) return
    record.value = item
    const savedReading = await getReading(revisionId.value, conversationId.value)
    if (token !== generation) return
    reading.value = savedReading
    readingRevision = revisionId.value
    readingConversation = conversationId.value
    zoom.value = Math.max(0.6, Math.min(2.4, reading.value.position.zoom || 1))
    if (typeof route.query.locator === 'string') {
      const result = await getSourceLocator(
        revisionId.value,
        route.query.locator,
        conversationId.value
      )
      if (token !== generation) return
      locator.value = result
      selectedLocator.value = result
    }
    if (token !== generation) return
    if (!isPdf.value) {
      if (
        item.media_type.includes('wordprocessingml') ||
        item.media_type.includes('spreadsheetml') ||
        item.media_type === 'text/csv'
      ) {
        const result = item.available_views?.includes('document') ? await getSourceDocument(revisionId.value, conversationId.value, {
          locator_id: locator.value?.locator_id,
          offset: locator.value ? undefined : reading.value.position.offset,
          sheet: locator.value ? undefined : reading.value.position.sheet,
        }) : null
        if (token !== generation) return
        document.value = result
        selectedSheet.value = result?.sheet || ''
      } else
        await loadText(
          locator.value?.location.line_start || reading.value.position.line_start || 1,
          token
        )
    }
    if (token !== generation) return
    loading.value = false
    await nextTick()
    if (scrollElement.value && !locator.value)
      scrollElement.value.scrollTop = reading.value.position.scroll_top || 0
    readingReady.value = true
    if (!isPdf.value && locator.value && documentElement()) noteCitationRendered()
    window.setTimeout(() => {
      if (token === generation) documentElement()?.scrollIntoView({ block: 'center' })
    }, 30)
  } catch (err) {
    if (token === generation) error.value = sourceError(err)
  } finally {
    if (token === generation) loading.value = false
  }
}
function documentElement() {
  return window.document.querySelector('[data-source-highlight="true"]')
}
function versionUpdated(item: SourceRevision) {
  if (!record.value || record.value.source_id !== item.source_id) return
  if (record.value.revision_id === item.revision_id) record.value = { ...record.value, ...item }
  else
    record.value = {
      ...record.value,
      current_version: Math.max(
        record.value.current_version || record.value.version,
        item.current_version || item.version
      ),
      current_revision_id: item.current_revision_id || record.value.current_revision_id,
    }
}
function tableReinterpreted(item: SourceRevision) {
  versionUpdated(item)
  void router.push(sourceReaderLocation(item.revision_id, conversationId.value))
}
function highlightedLine(number: number) {
  const l = locator.value
  return (
    (l?.kind === 'text_lines' &&
      number >= (l.location.line_start || 0) &&
      number <= (l.location.line_end || 0)) ||
    annotations.value.some(
      (a) =>
        a.locator?.kind === 'text_lines' &&
        number >= (a.locator.location.line_start || 0) &&
        number <= (a.locator.location.line_end || 0)
    )
  )
}
function highlightedBlock(block: SourceBlock) {
  return [locator.value, ...annotations.value.map((a) => a.locator)].some((l) =>
    blockMatches(block, l)
  )
}
function blockMatches(block: SourceBlock, l: SourceLocator | null | undefined) {
  if (!l) return false
  if (l.kind === 'word_block') return block.block_id === l.location.block_id
  if (l.kind === 'word_paragraphs')
    return (
      (block.paragraph_index || 0) >= (l.location.paragraph_start || 0) &&
      (block.paragraph_index || 0) <= (l.location.paragraph_end || 0)
    )
  if (l.kind === 'sheet_cells')
    return (
      block.sheet === l.location.sheet &&
      (!l.location.cell_range ||
        block.cell_range === l.location.cell_range ||
        cellRangesOverlap(block.cell_range || '', l.location.cell_range))
    )
  if (l.kind === 'text_lines')
    return (
      (block.line_start || block.row_start || 0) <= (l.location.line_end || 0) &&
      (block.line_end || block.row_end || 0) >= (l.location.line_start || 0)
    )
  return false
}
function highlightedCell(block: SourceBlock, cell: string) {
  return [locator.value, ...annotations.value.map((a) => a.locator)].some(
    (l) =>
      l?.kind === 'sheet_cells' &&
      block.sheet === l.location.sheet &&
      cellRangesOverlap(cell, l.location.cell_range || '')
  )
}
async function changeDocumentPage(offset: number, sheet = selectedSheet.value) {
  const token = generation
  const request = ++pageRequest
  error.value = ''
  try {
    const result = await getSourceDocument(revisionId.value, conversationId.value, {
      offset,
      sheet: sheet || undefined,
    })
    if (token !== generation || request !== pageRequest) return
    document.value = result
    selectedSheet.value = sheet
    positionChanged({ offset, sheet, scroll_top: 0 })
  } catch (err) {
    if (token === generation) error.value = sourceError(err)
  }
}
function setZoom(direction: -1 | 0 | 1) {
  zoom.value =
    direction === 0
      ? 1
      : Math.max(0.6, Math.min(2.4, Number((zoom.value + direction * 0.1).toFixed(1))))
  positionChanged({ zoom: zoom.value })
}
function onZoom(event: Event) {
  const direction = (event as CustomEvent<{ direction: -1 | 0 | 1 }>).detail?.direction
  if ([-1, 0, 1].includes(direction)) setZoom(direction)
}
async function changeTextPage(start: number) {
  error.value = ''
  try {
    await loadText(start)
    positionChanged({ line_start: start, scroll_top: 0 })
  } catch (err) {
    error.value = sourceError(err)
  }
}
watch(
  () => [revisionId.value, conversationId.value, route.query.locator],
  () => {
    void load()
  },
  { immediate: true }
)
onMounted(() => window.addEventListener('scholarflow:source-zoom', onZoom))
onBeforeUnmount(() => {
  if (saveTimer) clearTimeout(saveTimer)
  void persistReading()
  generation++
  window.removeEventListener('scholarflow:source-zoom', onZoom)
})
</script>

<template>
  <div class="source-reader">
    <header class="source-reader-header">
      <button
        type="button"
        @click="router.push({ path: '/chat', query: { conv: conversationId } })"
      >
        <ArrowLeft :size="16" />返回会话
      </button>
      <div class="source-reader-title">
        <strong>{{ record?.title || '资料阅读' }}</strong
        ><small v-if="record"
          >{{ sourceTypeLabel(record.media_type) }} · 固定版本 v{{ record.version
          }}<template v-if="record.current_version && record.current_version > record.version">
            · 存在更新版本，当前仍显示引用原版</template
          ></small
        >
      </div>
      <div class="source-zoom-controls">
        <button type="button" aria-label="缩小资料" @click="setZoom(-1)">
          <ZoomOut :size="16" /></button
        ><span>{{ Math.round(zoom * 100) }}%</span
        ><button type="button" aria-label="放大资料" @click="setZoom(1)">
          <ZoomIn :size="16" /></button
        ><button type="button" aria-label="重置资料缩放" @click="setZoom(0)">
          <RotateCcw :size="14" />
        </button>
      </div>
      <a v-if="record" :href="fileUrl" download><Download :size="15" />下载原件</a>
    </header>
    <p v-if="loading" class="source-reader-message" role="status">正在读取固定版本资料…</p>
    <p v-if="error" class="source-reader-message source-reader-error" role="alert">
      {{ error }} <button type="button" @click="load">重试</button>
      <button type="button" @click="showDiagnostics=true">检查资料环境</button>
    </p>
    <DiagnosticsPanel v-if="showDiagnostics" :conversation-id="conversationId" :source-revision-id="revisionId" @close="showDiagnostics=false" />
    <template v-if="record && !loading && !error">
      <div class="source-version-tools">
        <button
          v-if="typeof route.query.paper === 'string'"
          @click="router.push({ path: `/reader/${route.query.paper}`, query: { classic: '1' } })"
        >
          查看文献参考条目
        </button>
        <label
          ><input
            type="checkbox"
            :checked="reading.user_read"
            @change="toggleRead"
          />我已读过此版本</label
        >
        <SourceVersions
          :record="record"
          :conversation-id="conversationId"
          @updated="versionUpdated"
        />
      </div>
      <p v-if="readingError" role="alert" class="source-reader-error">{{ readingError }}</p>
      <SourceTableDetails v-if="record.media_type === 'text/csv' || document?.format === 'excel'" :record="record" :document="document" :conversation-id="conversationId" @reinterpreted="tableReinterpreted" />
      <RouterLink v-if="selectedLocator && selectedLocator.quote.trim()" class="source-translate-link"
        :to="{ path: '/translate', query: { conversation: conversationId, revision: revisionId, locator: selectedLocator.locator_id } }">
        翻译此固定选段
      </RouterLink>
      <ResearchNotes
        :revision-id="revisionId"
        :conversation-id="conversationId"
        :locator="selectedLocator"
        :selected-text="selectedText"
        @annotations="annotations = $event"
        @locate="locateAnnotation"
      />
      <div class="source-version-notice">
        <span v-if="record.text_origin === 'ocr_text'"
          >当前为 OCR 提取文本，未映射到原件页坐标；请结合下载原件核对识别结果。</span
        >
        引用始终指向此原件版本，后续修改原文件不会改写这里的内容。<span
          v-if="document?.format === 'word'"
        >
          当前为结构提取视图，段落与表格行序号不代表原始页码。</span
        ><span v-if="document?.format === 'excel'"> 显示已保存的单元格和公式，未重新计算公式。</span
        ><span v-if="record.media_type === 'text/markdown'">
          Markdown 按原文行号定位，保留标题、列表和代码块标记；HTML
          与外部资源不会自动执行或加载。</span
        ><span v-if="locator"> 已定位本次引用。</span>
      </div>
      <SourcePdf
        v-if="isPdf"
        :key="revisionId"
        :url="fileUrl"
        :page-index="locator?.location.page_index ?? reading.position.page_index"
        :bbox="locator?.location.bbox"
        :zoom="zoom"
        :scroll-top="reading.position.scroll_top"
        :annotations="annotations"
        @position="positionChanged"
        @selection="captureSelection"
        @rendered="noteCitationRendered"
      />
      <div
        v-else
        ref="scrollElement"
        class="source-document-scroll"
        @mouseup="selectDocumentText"
        @keyup="selectDocumentText"
        @scroll="
          positionChanged({
            scroll_top: scrollElement?.scrollTop || 0,
            line_start: textStart,
            offset: document?.offset || 0,
            sheet: selectedSheet,
          })
        "
        :style="{ '--source-text-size': `${14 * zoom}px` }"
      >
        <p v-if="document?.format === 'word' && document.extraction_scope === 'body_and_referenced_ooxml_parts'" class="source-structure-notice">此版本包含正文、引用的页眉页脚、脚注尾注与表格。图片说明来自作者元数据；图像内容、图表与排版页码尚未解析。</p>
        <nav v-if="sheets.length" class="source-sheet-tabs" aria-label="工作表">
          <button
            v-for="sheet in sheets"
            :key="sheet"
            type="button"
            :class="{ active: selectedSheet === sheet }"
            @click="changeDocumentPage(0, sheet)"
          >
            {{ sheet }}</button
          ><button v-if="selectedSheet" type="button" @click="changeDocumentPage(0, '')">
            全部工作表
          </button>
        </nav>
        <div v-if="textLines.length" class="source-text-lines">
          <div
            v-for="line in textLines"
            :key="line.number"
            class="source-text-line"
            :data-source-line="line.number"
            :data-source-highlight="highlightedLine(line.number)"
          >
            <span class="source-line-number">{{ line.number }}</span>
            <pre>{{ line.text || ' ' }}</pre>
          </div>
          <div class="source-text-pagination">
            <button
              type="button"
              :disabled="textStart <= 1"
              @click="changeTextPage(Math.max(1, textStart - 200))"
            >
              上一段</button
            ><span>第 {{ textStart }}–{{ textLines.at(-1)?.number }} 行 / {{ totalLines }} 行</span
            ><button
              type="button"
              :disabled="!nextLine"
              @click="nextLine && changeTextPage(nextLine)"
            >
              下一段
            </button>
          </div>
        </div>
        <article
          v-for="(block, index) in blocks"
          :key="block.block_id || `${block.sheet || ''}-${block.paragraph_index || block.cell_range || index}`"
          class="source-document-block"
          :data-source-block="index"
          :data-source-highlight="highlightedBlock(block)"
        >
          <div class="source-block-label">
            <template v-if="block.block_id">
              {{ block.part_label }}<template v-if="block.table_row"> · 表格逻辑行 {{ block.table_row }}，列 {{ block.table_column }}<template v-if="(block.column_span || 1) > 1">，跨 {{ block.column_span }} 列</template><template v-if="block.vertical_merge">，{{ block.vertical_merge === 'restart' ? '纵向合并起点' : '纵向合并延续' }}</template><template v-if="block.table_depth">，嵌套 {{ block.table_depth }} 层</template></template>
              <small> · 固定结构位置（无排版页码）</small>
            </template><template v-else-if="block.paragraph_index"
              >{{ block.kind === 'word_table_row' ? '表格提取行' : '段落' }}
              {{ block.paragraph_index }}</template
            ><template v-else-if="block.sheet">{{ block.sheet }} · {{ block.cell_range }}</template
            ><template v-else-if="block.row_start"
              >第 {{ block.row_start
              }}{{ block.row_end !== block.row_start ? `–${block.row_end}` : '' }} 行</template
            >
          </div>
          <table v-if="block.values?.length">
            <tbody>
              <tr v-for="(row, ri) in block.values" :key="ri">
                <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
              </tr>
            </tbody>
          </table>
          <table v-else-if="block.cells?.length" class="source-cells">
            <thead>
              <tr>
                <th v-for="cell in block.cells" :key="cell.cell || cell.address" scope="col">
                  {{ cell.cell || cell.address }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td
                  v-for="cell in block.cells"
                  :key="cell.cell || cell.address"
                  :data-source-highlight="highlightedCell(block, cell.cell || cell.address || '')"
                >
                  {{ cell.value }}
                  <small v-if="cell.formula" class="source-cell-detail">缓存：{{ cell.cached_value ?? '未保存' }}（未验证，未复算）</small>
                  <small v-if="cell.formula_representation && cell.formula_representation !== 'scalar'" class="source-cell-detail">特殊公式 {{ cell.formula_representation }}；范围 {{ cell.formula_attributes?.ref ?? '未记录' }}，暂不自动复算</small>
                  <small v-if="cell.special_formula_member" class="source-cell-detail">属于特殊公式区域 {{ cell.special_formula_range }}，锚点 {{ cell.special_formula_anchor }}；保存值不作为独立复算输入</small>
                  <small v-if="cell.percent_format" class="source-cell-detail">百分比格式 {{ cell.number_format }}；上方为原数值</small>
                  <small v-if="cell.date_format" class="source-cell-detail">日期/时间格式；原序列 {{ cell.stored_xml_value ?? '未记录' }}</small>
                  <small v-if="cell.merged_range" class="source-cell-detail">合并 {{ cell.merged_range }}；原值在 {{ cell.merged_anchor }}</small>
                  <small v-if="cell.missing_marker_candidate" class="source-cell-detail">疑似缺值标记，仍保留为原文</small>
                </td>
              </tr>
            </tbody>
          </table>
          <pre v-else>{{ block.text }}</pre>
          <details v-if="block.block_id" class="source-part-metadata"><summary>查看原件结构定位</summary><code>{{ block.part_name }}{{ block.xml_path }}</code></details>
          <details v-if="block.hyperlinks?.length" class="source-part-metadata">
            <summary>原文链接 {{ block.hyperlinks.length }} 个（未访问）</summary>
            <p v-for="(link, linkIndex) in block.hyperlinks" :key="linkIndex">{{ link.text }} · {{ link.target || ('#' + (link.anchor || '')) }}</p>
          </details>
          <p v-for="(image, imageIndex) in block.images || []" :key="imageIndex" class="source-part-metadata">
            图片说明：{{ image.description || image.title || '原件未提供文字说明' }}。图像内容尚未识别，未执行 OCR。
          </p>
        </article>
        <div v-if="document" class="source-text-pagination">
          <button
            type="button"
            :disabled="!document.offset"
            @click="changeDocumentPage(Math.max(0, (document.offset || 0) - 200))"
          >
            上一段
          </button>
          <span
            >结构 {{ document.total_blocks ? (document.offset || 0) + 1 : 0 }}–{{
              (document.offset || 0) + document.blocks.length
            }}
            / {{ document.total_blocks || document.blocks.length }}</span
          >
          <button
            type="button"
            :disabled="document.next_offset == null"
            @click="document.next_offset != null && changeDocumentPage(document.next_offset)"
          >
            下一段
          </button>
        </div>
        <p v-if="document?.truncated" class="source-reader-message">
          当前结构视图达到读取范围。请按引用定位或下载原件查看其余内容。
        </p>
        <p v-if="!textLines.length && !blocks.length" class="source-reader-message">
          此版本没有可展示的结构内容，可以下载已保存原件。
        </p>
      </div>
    </template>
  </div>
</template>
<style scoped>
.source-cell-detail {display:block;font-size:11px;color:#526b96;white-space:normal;margin-top:5px}
.source-version-tools {
  padding: 12px 18px;
  border-bottom: 1px solid #e4e9f4;
}
.source-reader {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #f7f9fd;
  color: #203c65;
}
.source-reader-header {
  padding: 14px 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  background: #fff;
  border-bottom: 1px solid #dfe5f1;
}
.source-reader-header button,
.source-reader-header a,
.source-text-pagination button,
.source-sheet-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid #dce3f2;
  border-radius: 6px;
  background: #fff;
  padding: 7px 9px;
  color: #365483;
  text-decoration: none;
  cursor: pointer;
  font-size: 12px;
  white-space: nowrap;
}
.source-reader-title {
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1;
  min-width: 0;
}
.source-reader-title strong {
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-reader-title small {
  font-size: 11px;
  color: #7587a4;
}
.source-zoom-controls {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}
.source-zoom-controls button {
  padding: 5px;
}
.source-version-notice {
  font-size: 11px;
  line-height: 1.7;
  color: #7183a1;
  padding: 8px 24px;
  border-bottom: 1px solid #e7ebf4;
  background: #fff;
}
.source-reader-message {
  padding: 14px 24px;
  font-size: 13px;
}
.source-reader-error {
  color: #ae3e35;
}
.source-document-scroll {
  flex: 1;
  overflow: auto;
  padding: 24px;
  min-height: 0;
}
.source-text-lines,
.source-document-block {
  background: #fff;
  border: 1px solid #e0e6f1;
  max-width: 1080px;
  margin: 0 auto;
}
.source-text-line {
  display: flex;
  padding: 3px 12px;
  line-height: 1.8;
}
.source-line-number {
  font-size: 11px;
  width: 45px;
  flex: none;
  text-align: right;
  padding-right: 16px;
  color: #91a0b6;
  user-select: none;
}
.source-text-line pre,
.source-document-block pre {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  font-size: var(--source-text-size, 14px);
  line-height: 1.85;
  min-width: 0;
}
.source-text-pagination {
  display: flex;
  justify-content: center;
  gap: 20px;
  align-items: center;
  border-top: 1px solid #e1e7f2;
  padding: 14px;
  font-size: 12px;
}
.source-text-pagination button:disabled {
  opacity: 0.4;
  cursor: default;
}
.source-document-block {
  padding: 16px 20px;
  margin-bottom: 10px;
  overflow: auto;
}
.source-block-label {
  font-size: 11px;
  color: #7e90ab;
  margin-bottom: 8px;
  overflow-wrap: anywhere;
  user-select: none;
}
.source-part-metadata,
.source-structure-notice {
  color: #7183a1;
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
  user-select: none;
}
.source-document-block[data-source-highlight='true'],
.source-text-line[data-source-highlight='true'] {
  background: #fff3bf;
  box-shadow: inset 3px 0 #d69e16;
}
.source-document-block table {
  border-collapse: collapse;
  font-size: var(--source-text-size);
}
.source-document-block td {
  border: 1px solid #dfe5ef;
  padding: 7px 12px;
  white-space: pre-wrap;
  min-width: 60px;
}
.source-cells td[data-source-highlight='true'] {
  background: #ffe28b;
  box-shadow: inset 3px 0 #b98707;
}
.source-sheet-tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  max-width: 1080px;
  margin: 0 auto 15px;
}
.source-sheet-tabs .active {
  background: #edf0ff;
  border-color: #6676eb;
  color: #4257cd;
}
:focus-visible {
  outline: 2px solid #5266f5;
  outline-offset: 2px;
}
@media (max-width: 900px) {
  .source-reader-header {
    flex-wrap: wrap;
    gap: 10px;
  }
  .source-document-scroll {
    padding: 12px;
  }
  .source-version-notice {
    padding: 8px 12px;
  }
}

.source-cells th {
  font-size: 11px;
  color: #6981a4;
  text-align: left;
  font-weight: 500;
  padding: 6px 12px;
  background: #f4f7fc;
  border: 1px solid #dfe5ef;
}
</style>
