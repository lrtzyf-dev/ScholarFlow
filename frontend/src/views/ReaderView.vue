<script setup lang="ts">
import { onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { getPaper, paperFileUrl, type Paper } from '../api'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

const route = useRoute()
const router = useRouter()
const paperId = route.params.id as string

const paper = ref<Paper | null>(null)
// shallowRef：pdfjs 文档对象含类私有字段，不能被 Vue 深度响应式 Proxy 包裹
const pdfDoc = shallowRef<any>(null)
const pageNum = ref(1)
const pageCount = ref(0)
const pageWidth = ref(0)
const pageHeight = ref(0)
const scale = ref(1.3)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const error = ref('')
const highlight = ref<{ x: number; y: number; w: number; h: number } | null>(null)

const TARGET_WIDTH = 860

function parseBbox(): number[] | null {
  const raw = route.query.bbox as string | undefined
  if (!raw) return null
  const parts = raw.split(',').map(Number)
  return parts.length === 4 && parts.every((n) => !Number.isNaN(n)) ? parts : null
}

async function renderPage() {
  const doc = pdfDoc.value
  if (!doc || !canvasRef.value) return
  const page = await doc.getPage(pageNum.value)
  const base = page.getViewport({ scale: 1 })
  scale.value = Math.min(1.8, TARGET_WIDTH / base.width)
  const viewport = page.getViewport({ scale: scale.value })
  const canvas = canvasRef.value
  const ctx = canvas.getContext('2d')!
  canvas.width = viewport.width
  canvas.height = viewport.height
  pageWidth.value = viewport.width
  pageHeight.value = viewport.height
  await page.render({ canvasContext: ctx, viewport }).promise

  const bbox = pageNum.value === Number(route.query.page || 1) ? parseBbox() : null
  if (bbox) {
    const rect = viewport.convertToViewportRectangle(bbox)
    const x = Math.min(rect[0], rect[2])
    const y = Math.min(rect[1], rect[3])
    highlight.value = {
      x,
      y,
      w: Math.abs(rect[2] - rect[0]),
      h: Math.abs(rect[3] - rect[1]),
    }
  } else {
    highlight.value = null
  }
}

async function goto(page: number) {
  if (page < 1 || page > pageCount.value) return
  pageNum.value = page
  await renderPage()
}

function gotoChat() {
  router.push({ path: '/chat', query: { paper: paperId } })
}

onMounted(async () => {
  try {
    localStorage.setItem('scholarflow:last-paper', paperId)
    paper.value = await getPaper(paperId)
    const data = await pdfjsLib.getDocument(paperFileUrl(paperId)).promise
    pdfDoc.value = data
    pageCount.value = data.numPages
    pageNum.value = Math.min(Math.max(1, Number(route.query.page) || 1), data.numPages)
    await renderPage()
  } catch (err: any) {
    error.value = err?.message || '原文加载失败'
  }
})

watch(pageNum, () => {})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title" style="max-width: 560px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
          {{ paper?.title || '版式阅读' }}
        </div>
        <div class="page-desc">PDF 原生渲染，问答证据可通过 bbox 定位到原文位置（高亮蓝框）</div>
      </div>
      <div class="toolbar">
        <button class="el-button" :disabled="pageNum <= 1" @click="goto(pageNum - 1)">上一页</button>
        <span class="muted">{{ pageNum }} / {{ pageCount }}</span>
        <button class="el-button" :disabled="pageNum >= pageCount" @click="goto(pageNum + 1)">下一页</button>
        <button class="el-button el-button--primary" @click="gotoChat">对这篇提问</button>
      </div>
    </div>

    <div v-if="error" class="empty">{{ error }}</div>
    <div v-else class="reader-wrap">
      <div class="pdf-page" :style="{ width: pageWidth + 'px', height: pageHeight + 'px' }">
        <canvas ref="canvasRef"></canvas>
        <div
          v-if="highlight"
          class="bbox-highlight"
          :style="{ left: highlight.x + 'px', top: highlight.y + 'px', width: highlight.w + 'px', height: highlight.h + 'px' }"
        ></div>
      </div>
    </div>
  </div>
</template>
