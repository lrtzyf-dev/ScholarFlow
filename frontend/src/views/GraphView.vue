<script setup lang="ts">
import { computed, onMounted, ref, nextTick, onBeforeUnmount,watch } from 'vue'
import { useRouter } from 'vue-router'
import { FileText } from 'lucide-vue-next'
import {
  getRelation,
  reviewRelation,
  locateRelation,
  graphPage,
  type RelationDetail,
} from '../researchApi'
import { activeConversationId } from '../desktopState'
import { ensureSourceConversation, sourceError } from '../sourceApi'
import {
  getDiagram,
  pagePapers,
  getPaper,
  type DiagramFormat,
  type GraphNode,
  type GraphEdge,
  type GraphRankingItem,
  type Paper,
} from '../api'

const router = useRouter()
const mmdEl = ref<HTMLDivElement | null>(null)
const nodes = ref<GraphNode[]>([])
type ResearchEdge = GraphEdge & {
  relation_id?: string
  relation_version?: number
  relation_type?: string
  status?: string
}
const edges = ref<ResearchEdge[]>([])
const ranking = ref<GraphRankingItem[]>([])
const papers = ref<Paper[]>([])
const targetSearch=ref(''),paperSearchError=ref(''),targetTotal=ref(0),selectedPaper=ref<Paper|null>(null)
let graphRequest:AbortController|null=null,graphGeneration=0,paperRequest:AbortController|null=null,paperGeneration=0,selectedGeneration=0,paperTimer:ReturnType<typeof setTimeout>|undefined
async function findTargetPapers(){
  const token=++paperGeneration;paperRequest?.abort();paperRequest=new AbortController()
  try{const result=await pagePapers({q:targetSearch.value,limit:40},paperRequest.signal);if(token===paperGeneration){papers.value=result.items;targetTotal.value=result.total;paperSearchError.value=''}}catch(e:any){if(token===paperGeneration&&e?.code!=='ERR_CANCELED')paperSearchError.value='目标文献检索失败，请重试'}
}
watch(targetSearch,()=>{paperRequest?.abort();paperGeneration++;if(paperTimer)clearTimeout(paperTimer);paperTimer=setTimeout(()=>void findTargetPapers(),180)})
const loaded = ref(false)
const error = ref('')
const graphNotice=ref('')
const selectedId = ref<string | null>(null)
const viewMode = ref<'graph' | 'list' | 'diagram'>('graph')
const graphZoom = ref(1)
const diagramFormat = ref<DiagramFormat>('mermaid')
const diagramScript = ref('')
const diagramLoading = ref(false)
const diagramError = ref('')
const showCode = ref(false)
const copyMessage = ref('')
let diagramRequest = 0
const selectedRelation = ref<RelationDetail | null>(null),
  relationError = ref(''),
  reason = ref(''),
  relationTarget = ref(''),
  relationType = ref('uncertain'),
  relationBusy = ref(false)
const graphFilter = ref(''),
  nextOffset = ref<number | null>(null),
  totalNodes = ref(0),
  pageLoading = ref(false),
  focus = ref('')
const relationLabels: Record<string, string> = {
  citation: '引用',
  similar: '相似',
  shared_topic: '共同主题',
  uncertain: '待核验匹配',
}
const pan = ref({ x: 0, y: 0 })
function resetView() { graphZoom.value=1; pan.value={x:0,y:0} }
const manualPositions = ref<Record<string, { x: number; y: number }>>({})
const savedPositions = new Map<string, { x: number; y: number }>()
let drag: {
  id: string | null
  x: number
  y: number
  ox: number
  oy: number
  scale: number
  svg: SVGSVGElement
} | null = null
function pointerDown(event: PointerEvent) {
  if (event.button !== 0) return
  const svg = event.currentTarget as SVGSVGElement,
    node = (event.target as Element).closest('[data-node-id]') as HTMLElement | null
  const id = node?.dataset.nodeId || null,
    at = id ? positionedNodes.value.find((n) => n.id === id) : null
  drag = {
    id,
    x: event.clientX,
    y: event.clientY,
    ox: at?.x || pan.value.x,
    oy: at?.y || pan.value.y,
    scale: 800 / svg.getBoundingClientRect().width,
    svg,
  }
  svg.setPointerCapture(event.pointerId)
}
function pointerMove(event: PointerEvent) {
  if (!drag) return
  const factor = drag.scale / (drag.id ? graphZoom.value : 1),
    x = drag.ox + (event.clientX - drag.x) * factor,
    y = drag.oy + (event.clientY - drag.y) * factor
  if (drag.id) manualPositions.value = { ...manualPositions.value, [drag.id]: { x, y } }
  else pan.value = { x, y }
}
function pointerUp() {
  drag = null
}
async function openRelation(edge: ResearchEdge) {
  if (!edge.relation_id) return
  relationError.value = ''
  try {
    const detail = await getRelation(edge.relation_id)
    selectedRelation.value = detail
    relationTarget.value = detail.effective.target
    relationType.value = detail.effective.relation_type
    reason.value = ''
  } catch (e) {
    relationError.value = sourceError(e)
  }
}
async function changeRelation(action: string, restore?: number) {
  if (!selectedRelation.value || relationBusy.value) return
  relationBusy.value = true
  relationError.value = ''
  try {
    await reviewRelation(
      selectedRelation.value,
      action,
      reason.value,
      action === 'correct' ? relationTarget.value : undefined,
      action === 'correct' ? relationType.value : undefined,
      restore
    )
    selectedRelation.value = await getRelation(selectedRelation.value.id)
    await loadGraph(false)
  } catch (e) {
    relationError.value = sourceError(e)
  } finally {
    relationBusy.value = false
  }
}
async function openRelationSource(contextIndex?: number) {
  if (!selectedRelation.value) return
  try {
    const cid = activeConversationId.value || `graph_${crypto.randomUUID()}`
    await ensureSourceConversation(cid)
    const loc = await locateRelation(selectedRelation.value.id, cid, contextIndex)
    await router.push({
      path: `/sources/${loc.revision_id}`,
      query: { conversation: cid, locator: loc.locator_id },
    })
  } catch (e) {
    relationError.value = sourceError(e)
  }
}
async function loadGraph(append = false, neighborhood?: string) {
  const token=++graphGeneration
  graphRequest?.abort();graphRequest=new AbortController()
  pageLoading.value = true
  error.value=''
  graphNotice.value=''
  if (neighborhood !== undefined) focus.value = neighborhood
  try {
    const data = await graphPage({
      limit: 100,
      offset: append ? nextOffset.value || 0 : 0,
      focus: focus.value || undefined,
      relation_type: graphFilter.value || undefined,
    },graphRequest.signal)
    if(token!==graphGeneration)return
    const merged = new Map((append ? nodes.value : []).map((n) => [n.id, n]))
    data.nodes.forEach((n: GraphNode) => merged.set(n.id, n))
    nodes.value = [...merged.values()]
    const relations = new Map(
      (append ? edges.value : []).map((e) => [e.relation_id || `${e.source}:${e.target}`, e])
    )
    data.edges.forEach((e: ResearchEdge) =>
      relations.set(e.relation_id || `${e.source}:${e.target}`, e)
    )
    edges.value = [...relations.values()]
    ranking.value = append ? [] : data.ranking
    nextOffset.value = data.next_offset
    totalNodes.value = data.total_nodes
    if (!selectedId.value) selectedId.value = nodes.value[0]?.id || null
  } catch (e:any) {
    if(token===graphGeneration&&e?.code!=='ERR_CANCELED'){
      if(nodes.value.length)graphNotice.value=sourceError(e)
      else error.value = sourceError(e)
    }
  } finally {
    if(token===graphGeneration){pageLoading.value = false;loaded.value=true}
  }
}
function cancelGraph(){graphGeneration++;graphRequest?.abort();pageLoading.value=false;loaded.value=true;graphNotice.value='已取消等待图谱；保留当前视图。服务器已开始的本地计算可能仍会结束。'}

const orderedNodes = computed(() => [...nodes.value].sort((a, b) => b.pagerank - a.pagerank))
const graphRanking = computed(() => (ranking.value.length ? ranking.value : orderedNodes.value))
const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedId.value) || null)
watch(selectedId,async id=>{const token=++selectedGeneration;selectedPaper.value=null;if(!id)return;try{const value=await getPaper(id);if(token===selectedGeneration)selectedPaper.value=value}catch{/* A merged node may not retain its original paper. */}})
const indegree = computed(() => {
  const counts: Record<string, number> = {}
  for (const edge of edges.value) counts[edge.target] = (counts[edge.target] || 0) + 1
  return counts
})
const outdegree = computed(() => {
  const counts: Record<string, number> = {}
  for (const edge of edges.value) counts[edge.source] = (counts[edge.source] || 0) + 1
  return counts
})
const relatedNodes = computed(() => {
  const ids = new Set(
    edges.value.flatMap((edge) =>
      edge.source === selectedId.value
        ? [edge.target]
        : edge.target === selectedId.value
          ? [edge.source]
          : []
    )
  )
  return orderedNodes.value.filter((node) => ids.has(node.id))
})

// Reuse the supplied UI's geometry; the node identities and every edge come from the backend.
const referencePositions = [
  [390, 255],
  [215, 105],
  [595, 110],
  [655, 300],
  [535, 435],
  [265, 430],
  [110, 290],
  [395, 65],
]
const graphSize = computed(() => ({ width: 800, height: 550 }))
const positionedNodes = computed(() =>
  orderedNodes.value.map((node, index) => {
    let x: number, y: number
    if (nodes.value.length <= 8) {
      ;[x, y] = referencePositions[index]
    } else if (!index) {
      x = graphSize.value.width / 2
      y = graphSize.value.height / 2
    } else {
      const ring = Math.floor((index - 1) / 12) + 1
      const count = Math.min(12, nodes.value.length - 1 - (ring - 1) * 12)
      const angle = -Math.PI / 2 + (((index - 1) % 12) * Math.PI * 2) / count
      x = graphSize.value.width / 2 + Math.cos(angle) * ring * 210
      y = graphSize.value.height / 2 + Math.sin(angle) * ring * 210
    }
    if (!savedPositions.has(node.id)) savedPositions.set(node.id, { x, y })
    const position = manualPositions.value[node.id] || savedPositions.get(node.id)!
    return { ...node, ...position, radius: index === 0 ? 37 : 26, main: index === 0 }
  })
)
const renderedEdges = computed(() => {
  const positions = new Map(positionedNodes.value.map((node) => [node.id, node]))
  return edges.value.flatMap((edge, index) => {
    const source = positions.get(edge.source),
      target = positions.get(edge.target)
    if (!source || !target) return []
    const dx = target.x - source.x,
      dy = target.y - source.y,
      length = Math.hypot(dx, dy)
    const path =
      length === 0
        ? `M ${source.x + source.radius * 0.7} ${source.y - source.radius * 0.7} C ${source.x + 70} ${source.y - 90}, ${source.x - 70} ${source.y - 90}, ${source.x - source.radius * 0.7} ${source.y - source.radius * 0.7}`
        : `M ${source.x + (dx / length) * source.radius} ${source.y + (dy / length) * source.radius} L ${target.x - (dx / length) * (target.radius + 2)} ${target.y - (dy / length) * (target.radius + 2)}`
    return [
      {
        ...edge,
        key: index,
        path,
        highlighted: edge.source === selectedId.value || edge.target === selectedId.value,
      },
    ]
  })
})
const graphTransform = computed(() => {
  const x = graphSize.value.width / 2,
    y = graphSize.value.height / 2
  return `translate(${pan.value.x} ${pan.value.y}) translate(${x} ${y}) scale(${graphZoom.value}) translate(${-x} ${-y})`
})

function initials(title: string) {
  const words = title.trim().split(/\s+/)
  return (
    (words.length > 1 ? words.map((word) => word[0]).join('') : title)
      .replace(/[^A-Za-z0-9\u4e00-\u9fa5]/g, '')
      .slice(0, 3) || '文献'
  )
}
function shortTitle(title: string) {
  return title.length > 27 ? `${title.slice(0, 25)}…` : title
}
function year(node: GraphNode) {
  return node.published_at?.slice(0, 4) || '年份未知'
}
function zoom(direction: number) {
  graphZoom.value = Math.min(3, Math.max(0.15, graphZoom.value + direction * 0.15))
}
function selectNode(id: string) {
  selectedId.value = id
}

async function loadDiagram(fmt: DiagramFormat) {
  const request = ++diagramRequest
  diagramLoading.value = true
  diagramError.value = ''
  copyMessage.value = ''
  try {
    const data = await getDiagram(fmt)
    if (request !== diagramRequest) return
    diagramScript.value = data.script
    diagramLoading.value = false
    if (fmt === 'mermaid') {
      await nextTick()
      const { default: mermaid } = await import('mermaid')
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral' })
      const { svg } = await mermaid.render(`mmd-${Date.now()}`, data.script)
      if (request === diagramRequest && mmdEl.value) mmdEl.value.innerHTML = svg
    }
  } catch (err) {
    if (request === diagramRequest)
      diagramError.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (request === diagramRequest) diagramLoading.value = false
  }
}
async function enterDiagram() {
  viewMode.value = 'diagram'
  await loadDiagram(diagramFormat.value)
}
async function switchFormat(fmt: DiagramFormat) {
  diagramFormat.value = fmt
  await loadDiagram(fmt)
}
async function copyCode() {
  try {
    await navigator.clipboard.writeText(diagramScript.value)
    copyMessage.value = '已复制'
  } catch {
    copyMessage.value = '复制失败，请选中代码后复制。'
    showCode.value = true
  }
}

onMounted(async () => {
  await Promise.allSettled([loadGraph(),findTargetPapers()])
})
onBeforeUnmount(() => {
  ++diagramRequest
  graphGeneration++;paperGeneration++;selectedGeneration++;graphRequest?.abort();paperRequest?.abort();if(paperTimer)clearTimeout(paperTimer)
})
</script>

<template>
  <div class="page analysis-page graph-page">
    <div class="page-head">
      <div>
        <h1 class="page-title">引用图谱</h1>
        <p class="page-desc">沿着引用关系，找到关键文献。</p>
      </div>
      <div class="relation-controls">
        <button v-if="pageLoading" @click="cancelGraph">取消等待图谱</button>
        <select v-model="graphFilter" aria-label="关系类型筛选" @change="loadGraph(false)">
          <option value="">全部关系</option>
          <option v-for="(label, key) in relationLabels" :key="key" :value="key">
            {{ label }}
          </option></select
        ><button @click="loadGraph(false, '')">全部节点</button
        ><button
          :disabled="!selectedId || pageLoading"
          @click="selectedId && loadGraph(false, selectedId)"
        >
          展开所选邻域</button
        ><button v-if="nextOffset !== null" :disabled="pageLoading" @click="loadGraph(true)">
          继续加载（{{ nodes.length }}/{{ totalNodes }}）
        </button>
      </div>
    </div>
    <p v-if="graphNotice" role="status">{{graphNotice}}</p>
    <div v-if="!loaded" class="graph-empty">加载中…</div>
    <div v-else-if="error" class="graph-empty error" role="alert">{{ error }}</div>
    <div v-else-if="!nodes.length" class="graph-empty">
      暂无可展示的文献。请先在「文献库」上传并解析论文。
    </div>
    <template v-else>
      <div class="graph-toolbar">
        <div class="graph-segment" role="group" aria-label="图谱呈现方式">
          <button
            :class="{ active: viewMode === 'graph' }"
            :aria-pressed="viewMode === 'graph'"
            @click="viewMode = 'graph'"
          >
            关系网络
          </button>
          <button
            :class="{ active: viewMode === 'list' }"
            :aria-pressed="viewMode === 'list'"
            @click="viewMode = 'list'"
          >
            基石论文榜
          </button>
        </div>
        <div class="graph-toolbar-meta">
          <button
            class="diagram-link"
            :class="{ active: viewMode === 'diagram' }"
            @click="enterDiagram"
          >
            拓扑图脚本</button
          ><span>{{ nodes.length }} 篇文献 · {{ renderedEdges.length }} 条关系</span>
        </div>
      </div>
      <div v-if="viewMode === 'diagram'" class="diagram-card">
        <div class="diagram-toolbar">
          <div class="graph-segment">
            <button
              :class="{ active: diagramFormat === 'mermaid' }"
              @click="switchFormat('mermaid')"
            >
              Mermaid</button
            ><button :class="{ active: diagramFormat === 'dot' }" @click="switchFormat('dot')">
              Graphviz DOT
            </button>
          </div>
          <div class="diagram-actions">
            <button class="el-button" @click="showCode = !showCode">
              {{ showCode ? '隐藏代码' : '查看代码' }}</button
            ><button
              class="el-button"
              :disabled="diagramLoading || !diagramScript"
              @click="copyCode"
            >
              复制代码
            </button>
          </div>
        </div>
        <p v-if="copyMessage" class="diagram-copy" role="status">{{ copyMessage }}</p>
        <div v-if="diagramLoading" class="diagram-state">生成中…</div>
        <div v-else-if="diagramError" class="diagram-state error">渲染失败：{{ diagramError }}</div>
        <template v-else
          ><div v-if="diagramFormat === 'mermaid'" ref="mmdEl" class="mermaid-render"></div>
          <div v-else class="diagram-state">
            Graphviz DOT 脚本需在 Graphviz 环境渲染，可复制后用 dot 工具生成图片。
          </div>
          <pre v-if="showCode" class="code-block">{{ diagramScript }}</pre>
        </template>
      </div>
      <div v-else class="graph-layout">
        <div class="graph-network">
          <template v-if="viewMode === 'graph'">
            <svg
              class="network-svg"
              :viewBox="`0 0 ${graphSize.width} ${graphSize.height}`"
              role="img"
              aria-label="文献引用关系网络"
              @pointerdown="pointerDown"
              @pointermove="pointerMove"
              @pointerup="pointerUp"
              @pointercancel="pointerUp"
            >
              <defs>
                <marker
                  id="sf-graph-arrow"
                  viewBox="0 0 8 8"
                  refX="7"
                  refY="4"
                  markerWidth="5"
                  markerHeight="5"
                  orient="auto-start-reverse"
                >
                  <path d="M0 0 8 4 0 8" fill="#c2cde0" />
                </marker>
              </defs>
              <g :transform="graphTransform">
                <path
                  v-for="edge in renderedEdges"
                  :key="edge.key"
                  :d="edge.path"
                  class="network-edge"
                  :class="{
                    highlighted: edge.highlighted,
                    uncertain: edge.relation_type !== 'citation',
                  }"
                  :marker-end="
                    edge.relation_type === 'citation' ? 'url(#sf-graph-arrow)' : undefined
                  "
                  tabindex="0"
                  role="button"
                  :aria-label="`查看关系依据：${edge.via}`"
                  @click.stop="openRelation(edge)"
                  @keydown.enter.prevent="openRelation(edge)"
                >
                  <title>
                    {{ relationLabels[edge.relation_type || 'uncertain'] }} · {{ edge.via }}
                  </title>
                </path>
                <g
                  v-for="node in positionedNodes"
                  :key="node.id"
                  class="network-node"
                  :data-node-id="node.id"
                  :class="{ main: node.main, selected: node.id === selectedId }"
                  :transform="`translate(${node.x} ${node.y})`"
                  tabindex="0"
                  role="button"
                  :aria-label="`查看 ${node.title}`"
                  :aria-pressed="node.id === selectedId"
                  @click="selectNode(node.id)"
                  @keydown.enter.prevent="selectNode(node.id)"
                  @keydown.space.prevent="selectNode(node.id)"
                >
                  <title>{{ node.title }}</title>
                  <circle :r="node.radius" />
                  <text class="node-initial" text-anchor="middle" dominant-baseline="central">
                    {{ initials(node.title) }}
                  </text>
                  <text class="node-title" text-anchor="middle" :y="node.radius + 22">
                    {{ shortTitle(node.title) }}
                  </text>
                  <text class="node-year" text-anchor="middle" :y="node.radius + 39">
                    {{ year(node) }}
                  </text>
                </g>
              </g>
            </svg>
            <div class="network-controls">
              <button aria-label="缩小图谱" @click="zoom(-1)">−</button
              ><button aria-label="放大图谱" @click="zoom(1)">＋</button
              ><button
                aria-label="重置图谱"
                @click="resetView"
              >
                重置
              </button>
            </div>
            <div class="network-legend">
              <span><i></i>文献</span><span>虚线为待核验或非引用关系 · 可拖动节点与视图</span>
            </div>
          </template>
          <div v-else class="graph-list">
            <button
              v-for="(node, index) in graphRanking"
              :key="node.id"
              :class="{ active: node.id === selectedId }"
              @click="selectNode(node.id)"
            >
              <span class="rank">{{ String(index + 1).padStart(2, '0') }}</span
              ><span>{{ node.title }}</span
              ><span class="metric">{{ node.pagerank.toFixed(3) }}</span>
            </button>
          </div>
        </div>
        <aside v-if="selectedNode" class="graph-detail">
          <section v-if="selectedRelation" class="relation-evidence">
            <h3>关系依据 · 修订 v{{ selectedRelation.version }}</h3>
            <p>
              {{ relationLabels[selectedRelation.effective.relation_type] }} ·
              {{
                selectedRelation.effective.status === 'excluded'
                  ? '已排除'
                  : selectedRelation.manual
                    ? '人工核验'
                    : '未核验'
              }}
            </p>
            <strong>参考条目</strong>
            <blockquote>{{ selectedRelation.extracted.reference_entry }}</blockquote>
            <p>{{ selectedRelation.extracted.matching_basis }}</p>
            <strong>正文引用原句</strong>
            <p v-if="!selectedRelation.extracted.citation_contexts?.length">
              {{ selectedRelation.extracted.sentence_status === 'source_unavailable'
                ? '尚未提取：固定原件或提取不可用，未以参考条目替代原句。'
                : '尚未提取到支持样式的正文引用标记，未以参考条目替代原句。' }}
            </p>
            <div v-for="(context, index) in selectedRelation.extracted.citation_contexts || []"
              :key="context.locator_id" class="citation-context">
              <blockquote>{{ context.quote }}</blockquote>
              <p>{{ context.match_method === 'author_year' ? '作者与年份标记' : '编号标记' }} ·
                {{ context.location.page_number ? `正文第 ${context.location.page_number} 页` :
                   context.location.line_start ? `第 ${context.location.line_start} 行` :
                   context.location.paragraph_start ? `第 ${context.location.paragraph_start} 段` :
                   `${context.location.sheet} · ${context.location.cell_range}` }} · 固定来源版本</p>
              <button @click="openRelationSource(Number(index))">查看这处正文引用</button>
            </div>
            <p>{{ selectedRelation.extracted.context_note || '引用位置与参考条目匹配均需核对，不代表结论已证实。' }}</p>
            <p>
              精确位置：{{
                selectedRelation.extracted.reference_page
                  ? `参考条目第 ${selectedRelation.extracted.reference_page} 页`
                  : '缺失'
              }}
            </p>
            <button
              :disabled="!selectedRelation.extracted.reference_page"
              @click="openRelationSource()"
            >
              查看原文条目
            </button>
            <label>检索目标文献<input v-model="targetSearch" placeholder="按标题或作者检索" /></label>
            <p v-if="paperSearchError" role="alert">{{paperSearchError}} <button @click="findTargetPapers">重试检索</button></p>
            <p v-else>显示 {{papers.length}} / {{targetTotal}} 篇；可输入关键词缩小范围。</p>
            <label
              >目标文献<select v-model="relationTarget">
                <option v-if="relationTarget && !papers.some(p=>p.id===relationTarget)" :value="relationTarget">当前目标：{{relationTarget}}</option>
                <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
              </select></label
            >
            <label
              >关系类型<select v-model="relationType">
                <option v-for="(label, key) in relationLabels" :key="key" :value="key">
                  {{ label }}
                </option>
              </select></label
            >
            <label
              >修改理由<textarea
                v-model="reason"
                aria-label="关系修改理由"
                rows="2"
                maxlength="2000"
              />
            </label>
            <div class="relation-controls">
              <button :disabled="relationBusy || !reason.trim()" @click="changeRelation('confirm')">
                确认</button
              ><button
                :disabled="relationBusy || !reason.trim()"
                @click="changeRelation('exclude')"
              >
                排除</button
              ><button
                :disabled="relationBusy || !reason.trim()"
                @click="changeRelation('correct')"
              >
                保存纠错</button
              ><button :disabled="relationBusy || !reason.trim()" @click="changeRelation('reset')">
                恢复抽取结果
              </button>
            </div>
            <p v-if="relationError" role="alert">{{ relationError }}</p>
            <details>
              <summary>修订历史（{{ selectedRelation.history.length }}）</summary>
              <p v-for="r in selectedRelation.history" :key="r.version">
                v{{ r.version }} · {{ r.actor }} · {{ r.reason || r.action }}
                <button
                  :disabled="
                    relationBusy || !reason.trim() || r.version === selectedRelation.version
                  "
                  @click="changeRelation('restore', r.version)"
                >
                  恢复此修订
                </button>
              </p>
            </details>
          </section>
          <div class="detail-kicker"><i></i>当前文献</div>
          <h3>{{ selectedNode.title }}</h3>
          <p class="detail-author">
            <template v-if="selectedPaper?.authors">{{ selectedPaper.authors }}<br /></template
            >{{ year(selectedNode) }} · {{ selectedNode.page_count }} 页<template
              v-if="selectedNode.version_count > 1"
              ><br />已合并 {{ selectedNode.version_count }} 个版本</template
            >
          </p>
          <div class="detail-stats">
            <div>
              <strong>{{ indegree[selectedNode.id] || 0 }}</strong
              ><span>被引用</span>
            </div>
            <div>
              <strong>{{ outdegree[selectedNode.id] || 0 }}</strong
              ><span>引用</span>
            </div>
            <div>
              <strong>{{ selectedNode.pagerank.toFixed(2) }}</strong
              ><span>中心性</span>
            </div>
          </div>
          <p class="related-title">相关文献</p>
          <button
            v-for="node in relatedNodes"
            :key="node.id"
            class="related-item"
            @click="selectNode(node.id)"
          >
            {{ node.title }}
          </button>
          <p v-if="!relatedNodes.length" class="related-empty">暂未匹配到库内引用关系。</p>
          <button class="el-button open-paper" @click="router.push(`/reader/${selectedNode.id}`)">
            <FileText :size="14" />打开原文
          </button>
        </aside>
      </div>
    </template>
  </div>
</template>

<style scoped>
.relation-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.relation-controls button,
.relation-controls select,
.relation-evidence button,
.relation-evidence select,
.relation-evidence textarea {
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
  padding: 6px 8px;
  max-width: 100%;
}
.relation-evidence {
  padding-bottom: 20px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.relation-evidence label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin: 12px 0;
}
.relation-evidence blockquote {
  margin: 8px 0;
  padding-left: 12px;
  border-left: 2px solid var(--fn-brand);
  white-space: pre-wrap;
}
.network-svg {
  touch-action: none;
}
.network-edge {
  cursor: pointer;
  pointer-events: stroke;
}
.network-edge.uncertain {
  stroke-dasharray: 6 4;
}
.graph-page .page-head {
  align-items: flex-start;
  gap: 24px;
}
.graph-page .page-title {
  margin: 0 0 8px;
  line-height: 1.35;
  letter-spacing: 0;
}
.graph-page .page-desc {
  margin: 0;
  font-size: 14px;
  line-height: 1.7;
}
.graph-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 15px;
  gap: 15px;
}
.graph-segment {
  display: inline-flex;
  gap: 3px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  padding: 3px;
  background: var(--fn-bg-secondary);
}
.graph-segment button {
  padding: 6px 12px;
  border: 0;
  background: transparent;
  color: var(--fn-text-secondary);
  font-size: 12px;
  line-height: 1.5;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
}
.graph-segment button.active {
  background: var(--fn-bg-primary);
  color: var(--fn-brand);
  box-shadow: 0 1px 3px #0000000a;
}
.graph-toolbar-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.diagram-link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--fn-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.diagram-link:hover,
.diagram-link.active {
  color: var(--fn-brand);
}
.graph-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 252px;
  grid-template-rows: minmax(0, 1fr);
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  overflow: hidden;
  min-height: 520px;
  height: 540px;
}
.graph-network {
  position: relative;
  background: var(--fn-bg-primary);
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.network-svg {
  display: block;
  width: 100%;
  height: 520px;
  max-height: 100%;
  touch-action: none;
}
.network-edge {
  stroke: #d5dff0;
  stroke-width: 1.2;
  fill: none;
}
.network-edge.highlighted {
  stroke: #66748c;
  stroke-width: 1.7;
}
.network-node {
  cursor: pointer;
  outline: none;
}
.network-node circle {
  fill: var(--fn-bg-primary);
  stroke: #ced8ea;
  stroke-width: 1.3;
  transition:
    fill 0.15s,
    stroke 0.15s;
}
.network-node.main circle {
  fill: var(--fn-bg-brand-light);
  stroke: #ccd8f0;
}
.network-node.selected circle {
  fill: var(--fn-brand);
  stroke: var(--fn-brand);
}
.network-node text {
  fill: var(--fn-text-secondary);
  font-family: inherit;
  font-size: 12px;
}
.network-node.selected .node-title {
  fill: var(--fn-brand);
  font-weight: 500;
}
.network-node .node-initial {
  font-size: 15px;
  font-weight: 500;
}
.network-node.selected .node-initial {
  fill: #fff;
}
.network-node .node-year {
  font-size: 9px;
  fill: var(--fn-text-tertiary);
}
.network-node:focus-visible circle {
  stroke: var(--fn-brand);
  stroke-width: 3;
}
.network-node:hover circle {
  stroke: var(--fn-brand);
  stroke-width: 2;
}
.network-controls {
  position: absolute;
  left: 18px;
  bottom: 17px;
  display: flex;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  overflow: hidden;
  background: var(--fn-bg-primary);
  box-shadow: 0 2px 5px #00000003;
}
.network-controls button {
  width: 32px;
  height: 30px;
  border: 0;
  background: none;
  font-size: 17px;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.network-controls button + button {
  border-left: 1px solid var(--fn-border-primary);
}
.network-controls button:hover {
  background: var(--fn-bg-brand-light);
}
.network-controls button:last-child {
  font-size: 12px;
  width: 45px;
}
.network-legend {
  position: absolute;
  bottom: 25px;
  right: 18px;
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: var(--fn-text-tertiary);
  align-items: center;
}
.network-legend i {
  width: 7px;
  height: 7px;
  background: var(--fn-bg-brand-light);
  border: 1px solid #ced8ea;
  display: inline-block;
  border-radius: 50%;
  margin-right: 4px;
}
.graph-detail {
  padding: 24px 21px;
  border-left: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  min-width: 0;
  min-height: 0;
  max-height: 100%;
  overflow-y: auto;
}
.detail-kicker {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin-bottom: 14px;
  display: flex;
  align-items: center;
  gap: 7px;
}
.detail-kicker i {
  display: block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fn-brand);
}
.graph-detail h3 {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.7;
  margin: 0 0 11px;
  overflow-wrap: anywhere;
}
.detail-author {
  color: var(--fn-text-tertiary);
  font-size: 12px;
  line-height: 1.8;
  margin: 0 0 24px;
}
.detail-stats {
  display: flex;
  gap: 22px;
  border-top: 1px solid var(--fn-border-primary);
  border-bottom: 1px solid var(--fn-border-primary);
  padding: 17px 0;
  margin-bottom: 22px;
}
.detail-stats strong {
  display: block;
  font-size: 20px;
  line-height: 1.4;
  font-weight: 450;
  margin-bottom: 4px;
  font-variant-numeric: tabular-nums;
}
.detail-stats span {
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.related-title {
  font-size: 12px;
  color: var(--fn-text-secondary);
  margin: 0 0 9px;
}
.related-item {
  display: block;
  width: 100%;
  text-align: left;
  padding: 9px 0;
  font-size: 12px;
  line-height: 1.7;
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  color: var(--fn-text-secondary);
  background: none;
  cursor: pointer;
}
.related-item:last-of-type {
  border-bottom: 0;
}
.related-item:hover {
  color: var(--fn-brand);
}
.related-empty {
  font-size: 12px;
  line-height: 1.7;
  color: var(--fn-text-tertiary);
}
.open-paper {
  margin-top: 20px;
  width: 100%;
  justify-content: center;
  gap: 7px;
}
.graph-list {
  padding: 15px 24px;
  height: 100%;
  max-height: 100%;
  overflow: auto;
}
.graph-list button {
  display: flex;
  align-items: center;
  gap: 13px;
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  text-align: left;
  background: transparent;
  padding: 16px 0;
  cursor: pointer;
  font-size: 13px;
  color: var(--fn-text-secondary);
  line-height: 1.7;
}
.graph-list button.active {
  color: var(--fn-brand);
}
.graph-list .rank {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  min-width: 18px;
}
.graph-list button span:nth-child(2) {
  flex: 1;
}
.graph-list .metric {
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.graph-empty {
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 65px 24px;
  text-align: center;
  color: var(--fn-text-secondary);
  font-size: 13px;
  line-height: 1.9;
}
.diagram-card {
  padding: 20px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
}
.diagram-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}
.diagram-actions {
  display: flex;
  gap: 8px;
}
.mermaid-render {
  display: flex;
  justify-content: center;
  overflow: auto;
}
.mermaid-render :deep(svg) {
  max-width: 100%;
  height: auto;
}
.diagram-state {
  padding: 50px 20px;
  color: var(--fn-text-secondary);
  font-size: 12px;
  text-align: center;
}
.diagram-copy {
  font-size: 11px;
  color: var(--fn-text-secondary);
}
.code-block {
  padding: 14px 16px;
  background: var(--fn-bg-secondary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
}
.error {
  color: var(--fn-danger);
}
@media (max-width: 1200px) {
  .graph-layout {
    grid-template-columns: minmax(0, 1fr) 225px;
  }
  .detail-stats {
    gap: 18px;
  }
}
@media (max-width: 960px) {
  .graph-layout {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto;
    height: auto;
  }
  .graph-network {
    height: 520px;
  }
  .graph-detail {
    border-left: 0;
    border-top: 1px solid var(--fn-border-primary);
    max-height: none;
    overflow-y: visible;
  }
  .detail-stats {
    max-width: 400px;
  }
  .open-paper {
    width: auto;
  }
  .graph-toolbar {
    flex-wrap: wrap;
  }
}
@media (max-width: 640px) {
  .graph-network,
  .network-svg {
    min-height: 410px;
    height: 410px;
  }
  .graph-layout {
    min-height: 410px;
  }
  .network-legend {
    font-size: 8px;
    right: 10px;
    gap: 5px;
  }
  .network-controls {
    left: 10px;
  }
  .graph-toolbar-meta {
    gap: 10px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .network-node circle {
    transition: none;
  }
}
</style>
