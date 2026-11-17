<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { localSessionHeaders } from '../localSession'
pdfjs.GlobalWorkerOptions.workerSrc = workerUrl
const props = defineProps<{
  url: string
  pageIndex?: number
  bbox?: number[] | null
  zoom: number
  scrollTop?: number
  annotations?: { locator?: { location: { page_index?: number; bbox?: number[] | null } } }[]
}>()
const emit = defineEmits<{
  rendered: [value: { page_index: number }]
  position: [value: { page_index: number; scroll_top: number }]
  selection: [value: { text: string; input: { kind: string; page_index: number; bbox: number[] } }]
}>()
const textLayerElement = ref<HTMLElement | null>(null)
const textAvailable = ref(true)
const annotationRects = ref<{ left: number; top: number; width: number; height: number }[]>([])
let textLayer: any
let currentScale = 1
const canvas = ref<HTMLCanvasElement | null>(null),
  stage = ref<HTMLElement | null>(null)
const doc = shallowRef<any>(null),
  page = ref(1),
  count = ref(0),
  loading = ref(true),
  error = ref('')
const width = ref(0),
  height = ref(0),
  highlight = ref<{ left: number; top: number; width: number; height: number } | null>(null)
let generation = 0,
  renderGeneration = 0,
  loadingTask: any,
  renderTask: any,
  observer: ResizeObserver | undefined
async function render() {
  const current = ++renderGeneration,
    pdf = doc.value
  if (!pdf || !canvas.value) return
  if (renderTask) {
    renderTask.cancel()
    try {
      await renderTask.promise
    } catch {
      /* superseded */
    }
  }
  try {
    const item = await pdf.getPage(page.value)
    if (current !== renderGeneration) return
    const base = item.getViewport({ scale: 1 })
    const scale =
      (Math.max(240, Math.min(850, (stage.value?.clientWidth || 900) - 50)) / base.width) *
      props.zoom
    const viewport = item.getViewport({ scale })
    currentScale = scale
    const target = canvas.value
    if (!target || current !== renderGeneration) return
    const ratio = Math.min(2, window.devicePixelRatio || 1)
    width.value = viewport.width
    height.value = viewport.height
    target.width = viewport.width * ratio
    target.height = viewport.height * ratio
    renderTask = item.render({
      canvasContext: target.getContext('2d')!,
      viewport,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    })
    await renderTask.promise
    if (current !== renderGeneration) return
    emit('rendered', {page_index:page.value - 1})
    if (textLayerElement.value) {
      textLayer?.cancel()
      textLayerElement.value.replaceChildren()
      textLayerElement.value.style.setProperty('--scale-factor', String(scale))
      const content = await item.getTextContent()
      if (current !== renderGeneration) return
      textAvailable.value = content.items.some((i: any) => i.str?.trim())
      textLayer = new pdfjs.TextLayer({
        textContentSource: content,
        container: textLayerElement.value,
        viewport,
      })
      await textLayer.render()
      if (current !== renderGeneration) return
    }
    annotationRects.value = (props.annotations || []).flatMap((a) => {
      const loc = a.locator?.location
      if (loc?.page_index !== page.value - 1 || loc.bbox?.length !== 4) return []
      return [
        {
          left: loc.bbox[0] * scale,
          top: loc.bbox[1] * scale,
          width: (loc.bbox[2] - loc.bbox[0]) * scale,
          height: (loc.bbox[3] - loc.bbox[1]) * scale,
        },
      ]
    })
    highlight.value = null
    if (page.value === (props.pageIndex ?? 0) + 1 && props.bbox?.length === 4) {
      const [x0, top, x1, bottom] = props.bbox
      const a = base.convertToPdfPoint(x0, top),
        b = base.convertToPdfPoint(x1, bottom)
      const rect = viewport.convertToViewportRectangle([
        Math.min(a[0], b[0]),
        Math.min(a[1], b[1]),
        Math.max(a[0], b[0]),
        Math.max(a[1], b[1]),
      ])
      highlight.value = {
        left: Math.min(rect[0], rect[2]),
        top: Math.min(rect[1], rect[3]),
        width: Math.abs(rect[2] - rect[0]),
        height: Math.abs(rect[3] - rect[1]),
      }
      await nextTick()
      const host = stage.value,
        marker = host?.querySelector<HTMLElement>('[data-source-highlight]')
      if (host && marker) {
        const hostRect = host.getBoundingClientRect(),
          markerRect = marker.getBoundingClientRect()
        // A whole-page citation must begin at the page top. Centering a tall
        // highlight otherwise scrolls the first paragraphs out of view.
        const topInset =
          markerRect.height > host.clientHeight * 0.6
            ? 24
            : Math.max(24, (host.clientHeight - markerRect.height) / 2)
        host.scrollTo({
          top: Math.max(0, host.scrollTop + markerRect.top - hostRect.top - topInset),
          left: Math.max(
            0,
            host.scrollLeft +
              markerRect.left -
              hostRect.left -
              Math.max(24, (host.clientWidth - markerRect.width) / 2)
          ),
        })
      }
    }
  } catch (err: any) {
    if (err?.name !== 'RenderingCancelledException' && current === renderGeneration)
      error.value = err?.message || 'PDF 页面渲染失败'
  }
}
async function load() {
  const current = ++generation
  loading.value = true
  error.value = ''
  doc.value = null
  loadingTask?.destroy()
  try {
    loadingTask = pdfjs.getDocument({ url: props.url, httpHeaders: localSessionHeaders(props.url), isEvalSupported: false })
    const pdf = await loadingTask.promise
    if (current !== generation) {
      await pdf.destroy()
      return
    }
    doc.value = pdf
    count.value = pdf.numPages
    page.value = Math.min(count.value, Math.max(1, (props.pageIndex ?? 0) + 1))
    loading.value = false
    await nextTick()
    await render()
    if (stage.value && props.scrollTop != null && !props.bbox)
      stage.value.scrollTop = props.scrollTop
    observer?.disconnect()
    observer = new ResizeObserver(() => {
      void render()
    })
    if (stage.value) observer.observe(stage.value)
  } catch (err: any) {
    if (current === generation) error.value = err?.message || 'PDF 原件无法读取'
  } finally {
    if (current === generation) loading.value = false
  }
}
watch(
  () => props.url,
  () => {
    void load()
  },
  { immediate: true }
)
watch(
  () => [props.zoom, props.annotations],
  () => {
    void render()
  }
)
watch(
  () => [props.pageIndex, props.bbox],
  () => {
    page.value = Math.min(count.value || 1, Math.max(1, (props.pageIndex ?? 0) + 1))
    void render()
  }
)
function go(value: number) {
  if (value >= 1 && value <= count.value) {
    page.value = value
    void render()
    if (stage.value) stage.value.scrollTop = 0
    emit('position', { page_index: page.value - 1, scroll_top: 0 })
  }
}
function selectedText() {
  const selection = window.getSelection()
  const layer = textLayerElement.value
  if (!selection || !layer || !selection.rangeCount || selection.isCollapsed) return
  const range = selection.getRangeAt(0)
  if (!layer.contains(range.startContainer) || !layer.contains(range.endContainer)) return
  const rect = range.getBoundingClientRect(),
    root = layer.getBoundingClientRect()
  const bbox = [
    Math.max(0, (rect.left - root.left) / currentScale),
    Math.max(0, (rect.top - root.top) / currentScale),
    Math.min(width.value / currentScale, (rect.right - root.left) / currentScale),
    Math.min(height.value / currentScale, (rect.bottom - root.top) / currentScale),
  ]
  emit('selection', {
    text: selection.toString(),
    input: { kind: 'pdf_page', page_index: page.value - 1, bbox },
  })
}
onBeforeUnmount(() => {
  generation++
  renderGeneration++
  observer?.disconnect()
  renderTask?.cancel()
  textLayer?.cancel()
  loadingTask?.destroy()
})
</script>
<template>
  <div class="source-pdf">
    <div class="source-page-controls">
      <button type="button" aria-label="上一页" :disabled="page <= 1" @click="go(page - 1)">
        <ChevronLeft :size="16" /></button
      ><span>第 {{ page }} / {{ count }} 页</span
      ><button type="button" aria-label="下一页" :disabled="page >= count" @click="go(page + 1)">
        <ChevronRight :size="16" />
      </button>
    </div>
    <p v-if="loading" role="status">正在读取固定版本 PDF…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="!loading && !textAvailable" role="status">
      此页没有可选择的文字层。可以查看原件；需要选段时请先使用已配置的 OCR，并核对识别结果。
    </p>
    <div
      ref="stage"
      class="source-pdf-stage"
      @scroll="emit('position', { page_index: page - 1, scroll_top: stage?.scrollTop || 0 })"
    >
      <div
        v-show="doc"
        class="source-pdf-paper"
        :style="{ width: `${width}px`, height: `${height}px` }"
      >
        <canvas ref="canvas" :style="{ width: `${width}px`, height: `${height}px` }"></canvas>
        <div
          v-for="(rect, index) in annotationRects"
          :key="index"
          class="source-pdf-highlight saved"
          :style="{
            left: `${rect.left}px`,
            top: `${rect.top}px`,
            width: `${rect.width}px`,
            height: `${rect.height}px`,
          }"
        ></div>
        <div
          ref="textLayerElement"
          class="source-text-layer"
          @mouseup="selectedText"
          @keyup="selectedText"
        ></div>
        <div
          v-if="highlight"
          data-source-highlight
          class="source-pdf-highlight"
          :style="{
            left: `${highlight.left}px`,
            top: `${highlight.top}px`,
            width: `${highlight.width}px`,
            height: `${highlight.height}px`,
          }"
        ></div>
      </div>
    </div>
  </div>
</template>
<style scoped>
.source-text-layer {
  position: absolute;
  text-align: initial;
  inset: 0;
  overflow: clip;
  opacity: 1;
  line-height: 1;
  text-size-adjust: none;
  forced-color-adjust: none;
  transform-origin: 0 0;
  z-index: 2;
}
.source-text-layer :deep(span),
.source-text-layer :deep(br) {
  color: transparent;
  position: absolute;
  white-space: pre;
  cursor: text;
  transform-origin: 0 0;
}
.source-text-layer :deep(> :not(.markedContent)),
.source-text-layer :deep(.markedContent span:not(.markedContent)) {
  z-index: 1;
}
.source-text-layer :deep(span.markedContent) {
  top: 0;
  height: 0;
}
.source-text-layer :deep(::selection) {
  background: #4b8cfb66;
}
.source-pdf-highlight.saved {
  background: #ffdc593f;
  border: 0;
  border-bottom: 2px solid #c39427;
}
.source-pdf {
  min-height: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  flex: 1;
}
.source-page-controls {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 15px;
  padding: 10px;
  font-size: 12px;
  color: #667d9f;
}
.source-page-controls button {
  background: #fff;
  border: 1px solid #dce3f2;
  color: #365483;
  border-radius: 5px;
  padding: 4px;
  display: flex;
}
.source-page-controls button:disabled {
  opacity: 0.4;
}
.source-pdf-stage {
  overflow: auto;
  min-height: 0;
  min-width: 0;
  padding: 24px;
  flex: 1;
  background: #edf1f8;
}
.source-pdf-paper {
  position: relative;
  max-width: none;
  margin: auto;
  box-shadow: 0 2px 12px #26477321;
}
.source-pdf-highlight {
  position: absolute;
  background: #ffdc5957;
  border: 2px solid #d39a11;
  pointer-events: none;
  box-sizing: border-box;
}
.source-pdf > p {
  padding: 12px 24px;
  font-size: 13px;
}
</style>
