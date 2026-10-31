<script setup lang="ts">
import { computed, onMounted, ref, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import cytoscape from 'cytoscape'
import { getGraph } from '../api'

const router = useRouter()
const container = ref<HTMLDivElement | null>(null)
const nodes = ref<any[]>([])
const edges = ref<any[]>([])
const loaded = ref(false)
const selectedId = ref<string | null>(null)
let cy: any = null

// 节点被引次数（入度）
const indegree = computed(() => {
  const m: Record<string, number> = {}
  for (const e of edges.value) m[e.target] = (m[e.target] || 0) + 1
  return m
})
const outdegree = computed(() => {
  const m: Record<string, number> = {}
  for (const e of edges.value) m[e.source] = (m[e.source] || 0) + 1
  return m
})

const selectedNode = computed(() => nodes.value.find((n) => n.id === selectedId.value) || null)
const refList = computed(() =>
  edges.value
    .filter((e) => e.source === selectedId.value)
    .map((e) => ({ via: e.via, other: nodes.value.find((n) => n.id === e.target) })),
)
const citedByList = computed(() =>
  edges.value
    .filter((e) => e.target === selectedId.value)
    .map((e) => ({ via: e.via, other: nodes.value.find((n) => n.id === e.source) })),
)

function nodeSize(id: string) {
  const d = indegree.value[id] || 0
  return 34 + Math.min(d, 5) * 7
}

function fitGraph() {
  if (!cy) return
  cy.fit(undefined, 60)
  if (cy.zoom() > 1.6) cy.zoom(1.6)
  cy.center()
}

function buildGraph() {
  if (!container.value) return
  cy = cytoscape({
    container: container.value,
    elements: [
      ...nodes.value.map((n) => ({
        data: {
          id: n.id,
          label: (n.title || '未命名').slice(0, 20),
          indeg: indegree.value[n.id] || 0,
        },
      })),
      ...edges.value.map((e, i) => ({
        data: { id: `e${i}`, source: e.source, target: e.target, via: e.via },
      })),
    ],
    style: [
      {
        selector: 'node',
        style: {
          'background-color': '#0a59f7',
          'border-color': '#ffffff',
          'border-width': 2,
          label: 'data(label)',
          color: '#4e5969',
          'font-size': 11.5,
          'font-weight': 500,
          'text-valign': 'bottom',
          'text-margin-y': 6,
          width: 'mapData(indeg, 0, 5, 36, 70)',
          height: 'mapData(indeg, 0, 5, 36, 70)',
        },
      },
      {
        selector: 'node:selected',
        style: {
          'background-color': '#0948d6',
          'border-color': '#bcd0ff',
          'border-width': 3,
          color: '#0948d6',
          'font-weight': 600,
        },
      },
      {
        selector: 'edge',
        style: {
          'curve-style': 'bezier',
          width: 1.5,
          'line-color': '#b6bdc9',
          'target-arrow-color': '#b6bdc9',
          'target-arrow-shape': 'triangle',
          'arrow-scale': 1,
        },
      },
      {
        selector: 'edge.hl',
        style: {
          'line-color': '#0a59f7',
          'target-arrow-color': '#0a59f7',
          width: 2,
        },
      },
      {
        selector: 'node.faded',
        style: { opacity: 0.35 },
      },
      {
        selector: 'edge.faded',
        style: { opacity: 0.2 },
      },
    ],
    layout: {
      name: 'cose',
      animate: false,
      padding: 50,
      nodeRepulsion: 12000,
      fit: false,
    } as any,
  })

  cy.on('tap', 'node', (e: any) => selectNode(e.target.id()))
  cy.on('tap', (e: any) => {
    if (e.target === cy) selectNode(null)
  })

  // 布局完成后再适应画布（此时容器已拿到真实尺寸）
  cy.ready(() => fitGraph())
  setTimeout(fitGraph, 100)

  // 容器尺寸变化（如折叠侧栏）时重适应
  if (typeof ResizeObserver !== 'undefined' && container.value) {
    const ro = new ResizeObserver(() => cy?.resize())
    ro.observe(container.value)
  }
}

function selectNode(id: string | null) {
  selectedId.value = id
  if (!cy) return
  cy.batch(() => {
    cy.elements().removeClass('faded')
    cy.edges().removeClass('hl')
    cy.nodes().unselect()
    if (id) {
      const n = cy.getElementById(id)
      n.select()
      const neighborhood = n.closedNeighborhood()
      cy.elements().not(neighborhood).addClass('faded')
      neighborhood.edges().addClass('hl')
    }
  })
}

function relayout() {
  if (!cy) return
  cy.layout({ name: 'cose', animate: true, padding: 40, nodeRepulsion: 12000 }).run()
}
function zoomIn() {
  cy?.zoom(cy.zoom() * 1.2)
}
function zoomOut() {
  cy?.zoom(cy.zoom() / 1.2)
}
function fit() {
  fitGraph()
}

onMounted(async () => {
  const data = await getGraph()
  nodes.value = data.nodes
  edges.value = data.edges
  loaded.value = true
  await nextTick()
  if (data.nodes.length === 0) return
  buildGraph()
})
</script>

<template>
  <div class="page graph-page">
    <div class="page-head">
      <div>
        <div class="page-title">局域引用图谱</div>
        <div class="page-desc">按参考文献标题匹配库内论文自动连边；节点大小反映被引次数，点击节点查看详情</div>
      </div>
      <div class="toolbar">
        <button class="el-button" @click="relayout">重新布局</button>
      </div>
    </div>

    <div v-if="!loaded" class="card muted">加载中…</div>
    <div v-else-if="nodes.length < 2" class="empty">
      图谱需要至少 2 篇论文入库。请在「文献库」上传多篇论文，系统会自动抽取参考文献并尝试匹配库内节点。
    </div>

    <div v-else class="graph-wrap">
      <div class="graph-canvas">
        <div ref="container" class="cy-container"></div>
        <div class="graph-tools">
          <button class="gt" title="放大" @click="zoomIn">+</button>
          <button class="gt" title="缩小" @click="zoomOut">−</button>
          <button class="gt" title="适应画布" @click="fit">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 9V5a1 1 0 0 1 1-1h4M20 9V5a1 1 0 0 0-1-1h-4M4 15v4a1 1 0 0 0 1 1h4M20 15v4a1 1 0 0 1-1 1h-4"/></svg>
          </button>
        </div>
        <div class="graph-legend">
          <div class="gl-title">图例</div>
          <div class="gl-row"><span class="gl-dot big"></span>核心节点（被引多）</div>
          <div class="gl-row"><span class="gl-dot"></span>普通论文</div>
          <div class="gl-row">
            <svg width="22" height="11"><path d="M1,6 L17,6" stroke="#0a59f7" stroke-width="1.6" marker-end="url(#leg-arrow)" /></svg>
            引用关系
          </div>
        </div>
        <svg width="0" height="0" style="position:absolute">
          <defs>
            <marker id="leg-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
              <path d="M0,0 L7,3 L0,6 z" fill="#0a59f7" />
            </marker>
          </defs>
        </svg>
      </div>

      <div class="graph-panel">
        <div v-if="!selectedNode" class="gp-empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>
          <p>点击任意节点查看该论文的页数、引用与被引详情</p>
        </div>
        <template v-else>
          <div class="gp-head">
            <div class="gp-kind">已选节点</div>
            <div class="gp-title">{{ selectedNode.title }}</div>
          </div>
          <div class="gp-sec">
            <div class="gp-meta"><span>页数</span><b>{{ selectedNode.page_count }} 页</b></div>
            <div class="gp-meta"><span>引用文献</span><b>{{ outdegree[selectedNode.id] || 0 }} 条</b></div>
            <div class="gp-meta"><span>被引（库内）</span><b>{{ indegree[selectedNode.id] || 0 }} 篇</b></div>
          </div>
          <div v-if="refList.length" class="gp-sec">
            <div class="gp-h">参考文献</div>
            <div v-for="(r, i) in refList" :key="i" class="gp-item" :class="{ linked: r.other }">
              <span class="gp-num">{{ i + 1 }}</span>
              <span>{{ r.other ? r.other.title : r.via }}<em v-if="r.other">（已匹配库内）</em></span>
            </div>
          </div>
          <div v-if="citedByList.length" class="gp-sec">
            <div class="gp-h">被这些论文引用</div>
            <div v-for="(r, i) in citedByList" :key="i" class="gp-item linked">
              <span class="gp-num">↑</span>
              <span>{{ r.other ? r.other.title : r.via }}</span>
            </div>
          </div>
          <div class="gp-sec">
            <button class="el-button el-button--primary" style="width:100%" @click="router.push(`/reader/${selectedNode.id}`)">
              打开版式阅读
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.graph-page {
  display: flex;
  flex-direction: column;
}
.graph-wrap {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 16px;
  flex: 1;
  min-height: 540px;
}
.graph-canvas {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 14px;
  box-shadow: var(--fn-shadow-sm);
  position: relative;
  overflow: hidden;
  min-height: 540px;
}
.cy-container {
  position: absolute;
  inset: 0;
}
.graph-tools {
  position: absolute;
  top: 12px;
  right: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  z-index: 5;
}
.gt {
  width: 34px;
  height: 34px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: var(--fn-text-secondary);
  box-shadow: var(--fn-shadow-sm);
  font-size: 18px;
  transition: all var(--fn-transition-fast);
}
.gt:hover {
  color: var(--fn-brand);
  border-color: var(--fn-brand);
}
.gt svg {
  width: 16px;
  height: 16px;
}
.graph-legend {
  position: absolute;
  bottom: 14px;
  left: 14px;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 10px 13px;
  box-shadow: var(--fn-shadow-sm);
  font-size: 11.5px;
  color: var(--fn-text-secondary);
  z-index: 5;
}
.gl-title {
  font-weight: 600;
  margin-bottom: 6px;
}
.gl-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.gl-dot {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: #0a59f7;
}
.gl-dot.big {
  width: 16px;
  height: 16px;
  background: #0948d6;
}
.graph-panel {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 14px;
  box-shadow: var(--fn-shadow-sm);
  overflow-y: auto;
  min-height: 540px;
}
.gp-empty {
  padding: 40px 24px;
  text-align: center;
  color: var(--fn-text-tertiary);
}
.gp-empty svg {
  width: 40px;
  height: 40px;
  margin: 0 auto 12px;
}
.gp-empty p {
  font-size: 12.5px;
  line-height: 1.7;
}
.gp-head {
  padding: 16px 17px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.gp-kind {
  font-size: 11px;
  color: var(--fn-text-tertiary);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.gp-title {
  font-size: 15px;
  font-weight: 700;
  margin-top: 6px;
  line-height: 1.4;
}
.gp-sec {
  padding: 14px 17px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.gp-h {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  font-weight: 600;
  margin-bottom: 10px;
}
.gp-meta {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  padding: 4px 0;
  color: var(--fn-text-secondary);
}
.gp-meta b {
  color: var(--fn-text-primary);
  font-weight: 600;
}
.gp-item {
  display: flex;
  gap: 9px;
  padding: 9px 0;
  border-bottom: 1px solid #f3f4f6;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--fn-text-secondary);
}
.gp-item:last-child {
  border-bottom: none;
}
.gp-num {
  width: 19px;
  height: 19px;
  border-radius: 6px;
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 1px;
}
.gp-item.linked {
  color: var(--fn-brand);
  font-weight: 500;
}
.gp-item em {
  font-style: normal;
  color: var(--fn-text-tertiary);
}
@media (max-width: 1000px) {
  .graph-wrap {
    grid-template-columns: 1fr;
  }
}
</style>
