<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { listPapers, type ComparisonData, type ComparisonEvidence, type Paper } from '../api'
import { activeConversationId } from '../desktopState'
import { ensureSourceConversation } from '../sourceApi'
import { librarySourceTypeLabel } from '../sourceSelection'
import {
  createMatrix,
  updateMatrix,
  getMatrix,
  listMatrices,
  editMatrixCell,
  exportMatrix,
  importMatrix,
  statusLabels,
  type Matrix,
  type MatrixField,
  type MatrixCell,
  type MatrixDocument,
} from '../researchApi'
import { useMatrixRuns } from '../useMatrixRuns'
import type { MatrixRun } from '../matrixRunApi'
import {
  comparisonCsv,
  comparisonEvidenceRoute,
  comparisonLocationLabel,
} from '../comparisonEvidence'

const router = useRouter()
const route = useRoute()
const allPapers = ref<Paper[]>([])
const selected = ref<Set<string>>(new Set())
const loading = ref(false)
const loaded = ref(false)
const error = ref('')
const matrix = ref<Matrix | null>(null)
const matrixConversation = ref('')
const matrixTarget = ref('')
const runPreview = ref(false)
const runs = useMatrixRuns(matrixConversation, restoreRun)
const {
  job,
  jobs,
  selected: selectedRun,
  error: runError,
  submitting,
  loading: running,
  retryUnknown,
  pendingCreate,
} = runs
const displayDocument = computed<MatrixDocument | null>(() =>
  runPreview.value && job.value?.result ? job.value.result.document : matrix.value?.document || null
)
const result = computed(() => displayDocument.value as unknown as ComparisonData | null)
const busy = computed(() => loading.value || submitting.value || running.value)
const matrices = ref<Omit<Matrix, 'document'>[]>([])
const matrixTitle = ref('跨文献证据矩阵')
const fields = ref<MatrixField[]>([
  { key: 'method', label: '研究方法', query: 'method approach framework 方法 模型 框架' },
  { key: 'dataset', label: '数据集', query: 'dataset benchmark data 数据集 语料 基准' },
  { key: 'metrics', label: '评价指标', query: 'metrics evaluation score 指标 评估' },
  { key: 'conclusion', label: '主要结论', query: 'conclusion finding result 结论 发现' },
])
let createRequestId = crypto.randomUUID(),
  createSignature = ''
const filterStatus = ref('')
const editing = ref<{
  field: string
  paper: string
  value: string
  status: string
  note: string
  use_candidate: boolean
  candidate_sha256?: string
} | null>(null)
const importInput = ref<HTMLInputElement | null>(null)
const missingSelected = computed(() =>
  [...selected.value].filter((id) => !parsedPapers.value.some((p) => p.id === id))
)
const visibleRows = computed(() =>
  (displayDocument.value?.rows || []).filter(
    (row) =>
      !filterStatus.value || Object.values(row.cells).some((c) => c.status === filterStatus.value)
  )
)
let viewEpoch = 0,
  matrixEpoch = 0,
  listEpoch = 0,
  disposed = false
const ownRoutes = new Set<string>()
function routeKey(query: typeof route.query) {
  return JSON.stringify([
    query.conversation || '',
    query.matrix || '',
    query.run || '',
    query.papers || '',
  ])
}
function replaceQuery(query: typeof route.query) {
  const key = routeKey(query)
  if (key === routeKey(route.query) || disposed) return
  ownRoutes.add(key)
  void router.replace({ query }).finally(() => {
    ownRoutes.delete(key)
  })
}
function capture() {
  return { view: viewEpoch, matrix: matrixEpoch, cid: matrixConversation.value }
}
function current(token: ReturnType<typeof capture>, id = matrixTarget.value) {
  return (
    !disposed &&
    token.view === viewEpoch &&
    token.matrix === matrixEpoch &&
    token.cid === matrixConversation.value &&
    id === matrixTarget.value
  )
}
function schemaInput() {
  return {
    conversation_id: matrixConversation.value,
    title: matrixTitle.value,
    paper_ids: [...selected.value],
    fields: fields.value.map((field) => ({ ...field })),
  }
}
function acceptMatrix(value: Matrix, reveal = true, hydrateDraft = true) {
  if (value.conversation_id !== matrixConversation.value || value.id !== matrixTarget.value) return
  if (matrix.value?.id === value.id && matrix.value.version > value.version) return
  matrix.value = value
  if (reveal) runPreview.value = false
  if (hydrateDraft) {
    matrixTitle.value = value.title
    fields.value = value.document.dimensions.map((f) => ({ ...f }))
    selected.value = new Set(value.document.papers.map((p) => p.id))
  }
  void refreshMatrices()
  replaceQuery({ ...route.query, matrix: value.id, conversation: value.conversation_id })
}
function newMatrix() {
  ++matrixEpoch
  runs.reset()
  matrixTarget.value = ''
  matrix.value = null
  runPreview.value = false
  editing.value = null
  createSignature = ''
  createRequestId = crypto.randomUUID()
  replaceQuery({ conversation: matrixConversation.value })
}
async function refreshMatrices() {
  const token = capture(),
    sequence = ++listEpoch
  try {
    const value = await listMatrices(token.cid)
    if (current(token) && sequence === listEpoch) matrices.value = value.items
  } catch {
    /* Keep the current list; a failed read cannot change the editor. */
  }
}
async function openSaved(id: string, preview = false, fromRun = false) {
  if (!id) return
  ++matrixEpoch
  const changed = matrixTarget.value !== id
  matrixTarget.value = id
  if (changed) {
    matrix.value = null
    editing.value = null
  }
  if (!fromRun) {
    runs.reset()
    runPreview.value = false
    replaceQuery({
      ...route.query,
      conversation: matrixConversation.value,
      matrix: id,
      run: undefined,
    })
    void runs.history()
  }
  const token = capture()
  loading.value = true
  try {
    const value = await getMatrix(id, token.cid)
    if (!current(token, id)) return
    acceptMatrix(value, !preview, changed || !fromRun)
    if (preview) runPreview.value = true
  } catch (e: any) {
    if (current(token, id)) ElMessage.error(e?.response?.data?.detail || '矩阵打开失败')
  } finally {
    if (current(token, id)) loading.value = false
  }
}
async function saveSchema() {
  const token = capture(),
    base = matrix.value,
    input = schemaInput()
  await ensureSourceConversation(token.cid)
  if (!current(token)) return null
  const signature = JSON.stringify(input)
  if (signature !== createSignature) {
    createRequestId = crypto.randomUUID()
    createSignature = signature
  }
  const value = base
    ? await updateMatrix(base.id, { ...input, expected_version: base.version })
    : await createMatrix({ ...input, request_id: createRequestId })
  if (!current(token)) return null
  matrixTarget.value = value.id
  acceptMatrix(value, true, JSON.stringify(schemaInput()) === signature)
  return value
}
async function restoreRun(value: MatrixRun) {
  if (value.conversation_id !== matrixConversation.value || value.run_id !== job.value?.run_id)
    return
  runPreview.value = true
  editing.value = null
  replaceQuery({
    ...route.query,
    conversation: value.conversation_id,
    matrix: value.inputs.matrix_id,
    run: value.run_id,
  })
  if (matrixTarget.value === value.inputs.matrix_id && matrix.value) return
  const cid = value.conversation_id,
    rid = value.run_id
  ++matrixEpoch
  const changed = matrixTarget.value !== value.inputs.matrix_id
  matrixTarget.value = value.inputs.matrix_id
  if (changed) matrix.value = null
  const token = capture()
  try {
    const current = await getMatrix(value.inputs.matrix_id, cid)
    if (
      token.view === viewEpoch &&
      token.matrix === matrixEpoch &&
      matrixConversation.value === cid &&
      job.value?.run_id === rid
    )
      acceptMatrix(current, false, changed)
  } catch {
    // A retained run preview must remain accessible even if its editable
    // matrix has been removed. Source authorization is enforced by the API.
    if (current(token, value.inputs.matrix_id) && job.value?.run_id === rid)
      ElMessage.error('当前矩阵不可用，已保存的提取结果仍可查看')
  }
}
async function showCurrentMatrix() {
  if (!matrix.value) return
  const token = capture(),
    id = matrix.value.id
  try {
    const value = await getMatrix(id, token.cid)
    if (current(token, id)) acceptMatrix(value, true, false)
  } catch {
    if (current(token, id)) ElMessage.error('当前矩阵读取失败，未覆盖显示内容')
  }
}
async function applyResult() {
  const value = job.value
  if (!value?.result || busy.value) return
  const cid = matrixConversation.value
  const token = capture()
  try {
    const current = await getMatrix(value.inputs.matrix_id, cid)
    if (
      token.view !== viewEpoch ||
      token.matrix !== matrixEpoch ||
      cid !== matrixConversation.value ||
      value.run_id !== job.value?.run_id
    )
      return
    await runs.apply(current.version)
    if (
      token.view === viewEpoch &&
      token.matrix === matrixEpoch &&
      value.run_id === job.value?.run_id &&
      job.value.result?.applied_version
    ) {
      const updated = await getMatrix(value.inputs.matrix_id, cid)
      if (
        token.view === viewEpoch &&
        token.matrix === matrixEpoch &&
        matrixTarget.value === updated.id
      )
        acceptMatrix(updated, true, false)
    }
  } catch {
    if (current(token)) ElMessage.error('合并未完成，提取结果与人工校正保留，请刷新后核对')
  }
}
function editCell(field: string, paper: string, cell: MatrixCell) {
  editing.value = {
    field,
    paper,
    value: cell.manual?.value || '',
    status: cell.status,
    note: cell.manual?.note || '',
    use_candidate: false,
    candidate_sha256: cell.extraction_candidate?.sha256,
  }
}
function showRunPreview() {
  runPreview.value = true
  editing.value = null
}
async function saveCell() {
  if (!editing.value || !matrix.value || loading.value) return
  const token = capture(),
    base = matrix.value,
    draft = { ...editing.value },
    signature = JSON.stringify(editing.value)
  loading.value = true
  try {
    const value = await editMatrixCell(base, draft.field, draft.paper, draft)
    if (!current(token, base.id)) return
    acceptMatrix(value, true, false)
    if (signature === JSON.stringify(editing.value)) editing.value = null
  } catch (e: any) {
    if (current(token, base.id))
      ElMessage.error(e?.response?.data?.detail || '保存失败，修改尚未提交')
  } finally {
    if (current(token, base.id)) loading.value = false
  }
}
function download(data: string, name: string, type: string) {
  const url = URL.createObjectURL(new Blob([data], { type }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}
async function downloadPackage() {
  if (!matrix.value) return
  const token = capture(),
    base = matrix.value,
    title = matrixTitle.value
  try {
    const value = await exportMatrix(base)
    if (!current(token, base.id)) return
    download(JSON.stringify(value, null, 2), `${title}.json`, 'application/json')
  } catch {
    ElMessage.error('矩阵导出失败')
  }
}
async function importPackage(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const token = capture()
  try {
    if (file.size > 12000000) throw Error('矩阵文件超过12MB')
    const data = JSON.parse(await file.text())
    if (!current(token)) return
    await ensureSourceConversation(token.cid)
    if (!current(token)) return
    const value = await importMatrix(token.cid, data)
    if (!current(token)) return
    ++matrixEpoch
    runs.reset()
    matrixTarget.value = value.id
    editing.value = null
    acceptMatrix(value)
  } catch (e: any) {
    if (current(token)) ElMessage.error(e?.response?.data?.detail || e.message || '导入失败')
  } finally {
    if (importInput.value) importInput.value.value = ''
  }
}

const parsedPapers = computed(() => allPapers.value.filter((p) => p.status === 'parsed'))
const canCompare = computed(() => selected.value.size >= 2)

function toggle(id: string) {
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
  // 触发响应式更新
  selected.value = new Set(selected.value)
}

async function runCompare() {
  if (pendingCreate.value) {
    ElMessage.warning('请先核对下方原任务的创建结果')
    return
  }
  if (busy.value) return
  if (!canCompare.value) {
    ElMessage.warning('请至少选择 2 篇论文')
    return
  }
  loading.value = true
  const token = capture()
  try {
    const saved = await saveSchema()
    if (!saved || token.view !== viewEpoch || token.matrix !== matrixEpoch) return
    runPreview.value = true
    await runs.create({ matrix_id: saved.id, expected_version: saved.version })
  } catch (err: any) {
    if (token.view === viewEpoch && token.matrix === matrixEpoch)
      ElMessage.error(err?.response?.data?.detail || '对比失败，请重试')
  } finally {
    if (token.view === viewEpoch && token.matrix === matrixEpoch) loading.value = false
  }
}
watch(
  () => job.value?.run_id,
  (id) => {
    if (id && job.value?.conversation_id === matrixConversation.value && route.query.run !== id)
      replaceQuery({
        ...route.query,
        conversation: matrixConversation.value,
        matrix: job.value.inputs.matrix_id,
        run: id,
      })
  }
)
watch(
  () => job.value?.result?.applied_version,
  async (version) => {
    const value = job.value
    if (!version || !value || !matrixConversation.value) return
    const cid = matrixConversation.value,
      rid = value.run_id
    const token = capture(),
      id = value.inputs.matrix_id
    if (matrixTarget.value !== id) return
    try {
      const updated = await getMatrix(id, cid)
      if (current(token, id) && rid === job.value?.run_id) acceptMatrix(updated, false, false)
    } catch {
      /* The run retains its independent preview and receipt. */
    }
  }
)

function openEvidence(evidence: ComparisonEvidence) {
  const target = comparisonEvidenceRoute(evidence, result.value?.conversation_id)
  if (target) router.push(target)
  else ElMessage.warning('该证据缺少可核查的位置，请重新对比或检查来源状态')
}

function exportCsv() {
  if (!result.value) return
  const blob = new Blob([comparisonCsv(result.value)], {
    type: 'text/csv;charset=utf-8',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '文献对比.csv'
  a.click()
  URL.revokeObjectURL(url)
}

async function loadRoute() {
  const token = ++viewEpoch
  ++matrixEpoch
  ++listEpoch
  loaded.value = false
  loading.value = false
  error.value = ''
  matrix.value = null
  matrixTarget.value = ''
  editing.value = null
  runPreview.value = false
  matrices.value = []
  selected.value = new Set()
  createSignature = ''
  createRequestId = crypto.randomUUID()
  matrixTitle.value = '跨文献证据矩阵'
  fields.value = [
    { key: 'method', label: '研究方法', query: 'method approach framework 方法 模型 框架' },
    { key: 'dataset', label: '数据集', query: 'dataset benchmark data 数据集 语料 基准' },
    { key: 'metrics', label: '评价指标', query: 'metrics evaluation score 指标 评估' },
    { key: 'conclusion', label: '主要结论', query: 'conclusion finding result 结论 发现' },
  ]
  matrixConversation.value =
    typeof route.query.conversation === 'string'
      ? route.query.conversation
      : activeConversationId.value ||
        localStorage.getItem('scholarflow:matrix-conversation') ||
        `compare_${crypto.randomUUID()}`
  runs.reset()
  const cid = matrixConversation.value,
    requestedRun = typeof route.query.run === 'string' ? route.query.run : ''
  const requestedMatrix = typeof route.query.matrix === 'string' ? route.query.matrix : ''
  try {
    localStorage.setItem('scholarflow:matrix-conversation', cid)
    const papers = await listPapers()
    if (token !== viewEpoch || disposed) return
    allPapers.value = papers
    await ensureSourceConversation(cid)
    if (token !== viewEpoch || disposed) return
    await refreshMatrices()
    if (token !== viewEpoch || disposed) return
    if (requestedMatrix) await openSaved(requestedMatrix, !!requestedRun, true)
    if (token !== viewEpoch || disposed) return
    await runs.history(requestedRun)
    if (token !== viewEpoch || disposed || requestedMatrix || requestedRun) return
    const requested = Array.isArray(route.query.papers)
      ? route.query.papers
      : route.query.papers
        ? [route.query.papers]
        : []
    const ids = new Set(requested.filter((id): id is string => typeof id === 'string'))
    selected.value = ids
  } catch {
    if (token === viewEpoch && !disposed) error.value = '文献加载失败，请刷新后重试。'
  } finally {
    if (token === viewEpoch && !disposed) loaded.value = true
  }
}
watch(
  () => routeKey(route.query),
  (key) => {
    if (ownRoutes.delete(key)) return
    void loadRoute()
  },
  { immediate: true, flush: 'sync' }
)
onBeforeUnmount(() => {
  disposed = true
  ++viewEpoch
  ++matrixEpoch
  ++listEpoch
})
</script>

<template>
  <div class="page analysis-page compare-page">
    <div class="page-head">
      <div>
        <div class="page-title">跨文献对比</div>
        <div class="page-desc">把研究问题、方法与证据放在一起看，点击引用返回原文。</div>
      </div>
      <div class="toolbar">
        <button class="el-button" @click="importInput?.click()">导入矩阵</button>
        <input ref="importInput" type="file" accept=".json" hidden @change="importPackage" />
        <button v-if="matrix" class="el-button" @click="downloadPackage">导出完整修订</button>
        <button
          v-if="result"
          class="el-button"
          title="逐条导出原句、来源版本、位置与核验状态"
          @click="exportCsv"
        >
          导出 CSV
        </button>
        <button
          class="el-button el-button--primary"
          :disabled="!loaded || !canCompare || busy || !!pendingCreate"
          @click="runCompare"
        >
          {{ submitting ? '保存任务中…' : running ? '正在提取…' : '开始对比' }}
        </button>
      </div>
    </div>

    <section class="matrix-runs" aria-label="证据提取任务">
      <div
        v-if="pendingCreate"
        class="matrix-conflict"
        role="status"
        aria-label="待确认的原提取任务"
      >
        <p>
          原提取任务的创建结果待确认，属于矩阵 {{ pendingCreate.matrix_id }}、修订 v{{
            pendingCreate.expected_version
          }}。下方输入不会替换该请求。
        </p>
        <button class="el-button" :disabled="submitting" @click="runs.inspectPending()">
          查询原任务结果
        </button>
        <button class="el-button" :disabled="submitting" @click="runs.retryCreate()">
          重试保存原任务
        </button>
        <button
          v-if="runs.rejectedCreate.value === pendingCreate.run_id"
          class="el-button"
          :disabled="submitting"
          @click="runs.releaseRejectedCreate()"
        >
          核实未创建并解除待确认
        </button>
      </div>
      <div class="matrix-run-actions">
        <label
          >已保存提取任务
          <select
            v-model="selectedRun"
            aria-label="已保存提取任务"
            :disabled="submitting || !loaded"
            @change="runs.select()"
          >
            <option value="" disabled>选择历史任务</option>
            <option v-for="item in jobs" :key="item.run_id" :value="item.run_id">
              {{ new Date(item.updated_at).toLocaleString() }} · {{ item.title.slice(0, 42) }}
            </option>
          </select>
        </label>
        <button class="el-button" :disabled="submitting" @click="runs.refresh()">
          刷新提取状态
        </button>
        <button v-if="running" class="el-button" :disabled="submitting" @click="runs.cancel()">
          停止提取
        </button>
        <button
          v-else-if="job?.can_resume"
          class="el-button"
          :disabled="submitting || (job.requires_unknown_confirmation && !retryUnknown)"
          @click="runs.resume()"
        >
          继续缺失单元格
        </button>
        <button
          v-if="job?.result"
          class="el-button"
          :disabled="submitting || !loaded"
          @click="showRunPreview"
        >
          查看本次提取
        </button>
        <button
          v-if="matrix && runPreview"
          class="el-button"
          :disabled="submitting || !loaded"
          @click="showCurrentMatrix"
        >
          查看当前矩阵
        </button>
      </div>
      <p v-if="runError" class="error" role="alert">{{ runError }}</p>
      <p v-if="job" role="status">
        {{
          running
            ? '提取正在后台继续，离开页面后进度保留'
            : job.result?.applied_version
              ? '提取结果已合并保存'
              : job.stage === 'completed'
                ? '提取结果已保存'
                : '任务尚未完成，已取得结果保留'
        }}
        <span v-if="job.result">
          · {{ job.result.progress.completed }}/{{ job.result.progress.total }} 个单元格</span
        >
        · 接口调用 {{ job.usage.call_count }} 次
      </p>
      <p v-if="job?.error" class="error">{{ job.error }}</p>
      <p v-if="job?.resume_blocked_reason">{{ job.resume_blocked_reason }}</p>
      <label v-if="job?.requires_unknown_confirmation && !running" class="matrix-unknown">
        <input
          v-model="retryUnknown"
          type="checkbox"
        />我已核对上次调用，同意重试未确认调用（可能重复收费）
      </label>
      <div v-if="job?.result?.apply_conflict" class="matrix-conflict" role="status">
        <p>
          {{
            job.result.apply_conflict.message ||
            '矩阵已有新修订，提取结果独立保留，尚未覆盖当前矩阵。'
          }}
        </p>
        <button
          class="el-button"
          :disabled="busy || !!job.result.applied_version"
          @click="applyResult"
        >
          合并到当前矩阵并保留人工校正
        </button>
      </div>
      <p v-if="runPreview && job?.result" class="matrix-preview-note">
        当前显示本次提取的固定结果；人工修改请打开「当前矩阵」。来源已定位不等于结论已核验。
      </p>
    </section>

    <div v-if="!loaded" class="card muted">加载中…</div>
    <div v-else-if="error" class="empty error" role="alert">{{ error }}</div>
    <div v-else-if="parsedPapers.length < 2 && !matrix && !job?.result" class="empty">
      对比需要至少 2 篇已解析论文，请先在「文献库」上传并完成解析。
    </div>

    <template v-else>
      <div class="matrix-controls">
        <label
          >保存的矩阵
          <select
            aria-label="保存的矩阵"
            :value="matrix?.id || ''"
            @change="openSaved(($event.target as HTMLSelectElement).value)"
          >
            <option value="">选择矩阵</option>
            <option v-for="m in matrices" :key="m.id" :value="m.id">
              {{ m.title }} · v{{ m.version }}
            </option>
          </select></label
        >
        <button class="el-button" :disabled="busy" @click="newMatrix">新建矩阵</button>
        <label>名称 <input v-model="matrixTitle" maxlength="200" aria-label="矩阵名称" /></label>
        <span v-if="matrix"
          >修订 v{{ matrix.version }} · 字段版本 {{ matrix.document.schema_version }}</span
        >
      </div>
      <details class="matrix-fields">
        <summary>提取字段（{{ fields.length }}）</summary>
        <div v-for="(field, index) in fields" :key="field.key" class="matrix-field">
          <input
            v-model="field.label"
            :aria-label="`字段 ${index + 1} 名称`"
            placeholder="字段名称"
          />
          <input
            v-model="field.query"
            :aria-label="`字段 ${index + 1} 检索内容`"
            placeholder="用哪些词句查找证据"
          />
          <button :disabled="fields.length <= 1" @click="fields.splice(index, 1)">移除</button>
        </div>
        <button
          :disabled="fields.length >= 40"
          @click="fields.push({ key: `field_${Date.now()}`, label: '', query: '' })"
        >
          添加字段
        </button>
      </details>
      <p v-if="missingSelected.length" role="alert">
        所选资料中有 {{ missingSelected.length }} 份缺失或尚未解析：{{
          missingSelected.join('、')
        }}。处理前会完整保留选择，不会自动移除。
      </p>
      <div class="card select-card">
        <div class="select-h">
          <strong
            >选择文献 <span>{{ selected.size }} 篇</span></strong
          ><span>至少选择 2 篇 · 已解析文献</span>
        </div>
        <div class="select-grid">
          <label
            v-for="p in parsedPapers"
            :key="p.id"
            class="select-item"
            :class="{ active: selected.has(p.id) }"
          >
            <input type="checkbox" :checked="selected.has(p.id)" @change="toggle(p.id)" />
            <span class="select-info"
              ><span class="select-title">{{ p.title || '未命名论文' }}</span>
              <span class="select-meta"
                >{{ p.authors || '作者未提供' }} · {{ librarySourceTypeLabel(p)
                }}<template v-if="p.page_count > 0"> · {{ p.page_count }} 页</template></span
              ></span
            >
          </label>
        </div>
      </div>

      <div v-if="!result" class="empty compare-empty">
        选择至少两篇文献，开始比较研究问题、方法与评估方式。
      </div>
      <div v-if="result" class="card table-card">
        <label class="matrix-filter"
          >筛选状态
          <select v-model="filterStatus">
            <option value="">全部</option>
            <option v-for="(label, key) in statusLabels" :key="key" :value="key">
              {{ label }}
            </option>
          </select></label
        >
        <p class="comparison-notice" role="status">
          以下为按维度检索的原文片段，结论支持程度尚未核验；未检索到不代表文献中不存在。导出会保留来源版本与位置，原件不包含在
          CSV 中。
        </p>
        <table class="compare-table">
          <thead>
            <tr>
              <th class="dim-col">维度</th>
              <th v-for="p in result.papers" :key="p.id">
                <div class="th-title">{{ p.title }}</div>
                <div class="th-sub">
                  {{ p.year || '年份未提供'
                  }}<template v-if="p.page_count > 0"> · {{ p.page_count }} 页</template>
                </div>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in visibleRows" :key="row.key">
              <td class="dim-col">{{ row.label }}</td>
              <td v-for="p in result.papers" :key="p.id">
                <div class="cell">
                  <div class="matrix-cell-status">
                    {{ statusLabels[row.cells[p.id]?.status] || '未处理'
                    }}<span v-if="row.cells[p.id]?.manual"> · 人工核验</span>
                  </div>
                  <p v-if="row.cells[p.id]?.manual?.value" class="matrix-human-value">
                    {{ row.cells[p.id].manual?.value }}
                  </p>
                  <template v-if="row.cells[p.id]?.evidence.length">
                    <button
                      v-for="ev in row.cells[p.id].evidence"
                      :key="ev.id"
                      class="ev-chip"
                      :data-paper-id="p.id"
                      :data-revision-id="ev.revision_id"
                      :data-locator-id="ev.locator_id"
                      :disabled="!comparisonEvidenceRoute(ev, result.conversation_id)"
                      @click="openEvidence(ev)"
                    >
                      <span class="ev-page"
                        >{{ comparisonLocationLabel(ev)
                        }}<template v-if="ev.source_version">
                          · v{{ ev.source_version }}</template
                        ></span
                      >
                      <span class="ev-quote">{{ ev.quote }}</span>
                    </button>
                  </template>
                  <span v-else class="ev-none">{{
                    row.cells[p.id]?.status === 'not_processed'
                      ? '尚未提取'
                      : '未检索到证据，不代表不存在'
                  }}</span>
                  <button
                    v-if="!runPreview"
                    class="matrix-edit"
                    @click="editCell(row.key, p.id, row.cells[p.id])"
                  >
                    校正与核验
                  </button>
                  <p v-if="row.cells[p.id]?.manual?.note" class="matrix-note">
                    {{ row.cells[p.id].manual?.note }}
                  </p>
                  <details v-if="row.cells[p.id]?.extraction_candidate" class="matrix-candidate">
                    <summary>新候选待复核</summary>
                    <p>原人工结论及其证据保持原版本。以下为本次重新提取的候选，尚未核验。</p>
                    <button
                      v-for="ev in row.cells[p.id].extraction_candidate?.evidence || []"
                      :key="ev.id"
                      class="ev-chip"
                      :data-candidate-locator-id="ev.locator_id"
                      :disabled="!comparisonEvidenceRoute(ev, result.conversation_id)"
                      @click="openEvidence(ev)"
                    >
                      <span class="ev-page">{{ comparisonLocationLabel(ev) }}</span
                      ><span class="ev-quote">{{ ev.quote }}</span>
                    </button>
                    <p v-if="!row.cells[p.id].extraction_candidate?.evidence.length">
                      本次未检索到候选证据，不代表文献中不存在。
                    </p>
                  </details>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <form v-if="editing" class="matrix-editor" @submit.prevent="saveCell">
        <strong>人工校正 · 原始片段和修订历史会保留</strong>
        <label
          >校正内容<textarea
            v-model="editing.value"
            aria-label="校正内容"
            rows="4"
            maxlength="10000"
          />
        </label>
        <label
          >核验状态<select v-model="editing.status" aria-label="核验状态">
            <option v-for="(label, key) in statusLabels" :key="key" :value="key">
              {{ label }}
            </option>
          </select></label
        >
        <label>依据与备注<textarea v-model="editing.note" aria-label="核验备注" rows="2" /></label>
        <label v-if="editing.candidate_sha256" class="candidate-consent">
          <input
            v-model="editing.use_candidate"
            type="checkbox"
          />本次人工核验采用已查看的新候选证据
        </label>
        <div>
          <button class="el-button el-button--primary" :disabled="loading" type="submit">
            保存校正</button
          ><button class="el-button" type="button" @click="editing = null">取消</button>
        </div>
      </form>
    </template>
  </div>
</template>

<style scoped>
.matrix-runs {
  margin: 18px 0;
  padding: 14px 0;
  border-block: 1px solid var(--fn-border-primary);
}
.matrix-run-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.matrix-run-actions label {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  max-width: 100%;
  font-size: 12px;
}
.matrix-run-actions select {
  min-width: 0;
  max-width: min(360px, 100%);
  padding: 8px;
  color: var(--fn-text-primary);
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 5px;
}
.matrix-runs p {
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
  color: var(--fn-text-secondary);
}
.matrix-conflict {
  margin: 12px 0;
  padding: 12px;
  background: var(--fn-bg-secondary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
}
.matrix-conflict button {
  margin: 4px 8px 0 0;
}
.matrix-unknown {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  line-height: 1.7;
}
.candidate-consent {
  flex-direction: row !important;
  align-items: flex-start !important;
  font-size: 12px;
  line-height: 1.7;
}
.matrix-controls,
.matrix-field {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  margin: 12px 0;
}
.matrix-controls label,
.matrix-editor label {
  display: flex;
  gap: 8px;
  align-items: center;
}
.matrix-controls input,
.matrix-controls select,
.matrix-field input,
.matrix-filter select,
.matrix-editor textarea,
.matrix-editor select {
  color: var(--fn-text-primary);
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 5px;
  padding: 8px;
}
.matrix-field input:nth-child(2) {
  flex: 1;
  min-width: 240px;
}
.matrix-fields {
  margin: 16px 0;
  color: var(--fn-text-secondary);
}
.matrix-editor {
  position: fixed;
  right: 24px;
  bottom: 24px;
  width: min(520px, calc(100vw - 48px));
  box-sizing: border-box;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  box-shadow: 0 5px 24px #0002;
  padding: 20px;
  z-index: 15;
  display: grid;
  gap: 15px;
}
.matrix-editor label {
  align-items: stretch;
  flex-direction: column;
}
.matrix-cell-status {
  font-size: 12px;
  color: var(--fn-text-secondary);
  margin-bottom: 8px;
}
.matrix-candidate {
  margin-top: 12px;
  border-top: 1px solid var(--fn-border-primary);
  padding-top: 10px;
  font-size: 12px;
}
.matrix-candidate summary {
  cursor: pointer;
  color: var(--fn-text-primary);
}
.matrix-candidate p {
  color: var(--fn-text-secondary);
  line-height: 1.7;
}
.matrix-human-value,
.matrix-note {
  white-space: pre-wrap;
}
.matrix-edit {
  margin-top: 10px;
  background: transparent;
  border: 1px solid var(--fn-border-primary);
  padding: 6px 10px;
  color: var(--fn-text-primary);
}
.select-card {
  padding: 0;
  margin-bottom: 25px;
  border: 0;
  box-shadow: none;
  background: transparent;
}
.select-h {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
  padding: 16px 0;
  margin-bottom: 7px;
  border-top: 1px solid var(--fn-border-primary);
  border-bottom: 1px solid var(--fn-border-primary);
  font-size: calc(12px * var(--reading-text-scale, 1));
  color: var(--fn-text-secondary);
}
.select-h strong {
  font-size: calc(13px * var(--reading-text-scale, 1));
  font-weight: 500;
  color: var(--fn-text-primary);
}
.select-h strong span {
  margin-left: 8px;
  color: var(--fn-text-tertiary);
  font-weight: 400;
}
.select-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 24px;
}
.select-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  padding: 13px 0;
  border-bottom: 1px solid var(--fn-border-primary);
  cursor: pointer;
}
.select-item input {
  accent-color: var(--fn-brand);
  width: 14px;
  height: 14px;
  margin: 4px 0 0;
  flex-shrink: 0;
}
.select-info {
  min-width: 0;
}
.select-title {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.7;
  font-weight: 500;
  color: var(--fn-text-primary);
}
.select-item.active .select-title {
  color: var(--fn-brand);
}
.select-meta {
  display: block;
  font-size: calc(11px * var(--reading-text-scale, 1));
  line-height: 1.7;
  color: var(--fn-text-tertiary);
  margin-top: 3px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.table-card {
  padding: 0;
  overflow-x: auto;
  border-radius: 8px;
  box-shadow: none;
}
.comparison-notice {
  margin: 0;
  padding: 12px 20px;
  border-bottom: 1px solid var(--fn-border-primary);
  color: var(--fn-text-secondary);
  font-size: calc(11px * var(--reading-text-scale, 1));
  line-height: 1.7;
}
.ev-chip:disabled {
  cursor: default;
}
.compare-table {
  width: 100%;
  min-width: 650px;
  border-collapse: collapse;
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.7;
}
.compare-table th,
.compare-table td {
  vertical-align: top;
  text-align: left;
  padding: 20px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.compare-table th {
  padding: 17px 20px;
  background: var(--fn-bg-secondary);
  font-weight: 500;
}
.compare-table th + th,
.compare-table td + td {
  border-left: 1px solid var(--fn-border-primary);
  min-width: 210px;
}
.compare-table tr:last-child td {
  border-bottom: 0;
}
.dim-col {
  width: 125px;
  min-width: 125px;
  font-weight: 500;
  color: var(--fn-text-primary);
}
.th-title {
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.7;
  font-weight: 500;
}
.th-sub {
  font-size: calc(11px * var(--reading-text-scale, 1));
  color: var(--fn-text-tertiary);
  margin-top: 7px;
}
.cell {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.ev-chip {
  display: flex;
  flex-direction: column-reverse;
  align-items: flex-start;
  gap: 8px;
  padding: 0;
  border: 0;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.ev-page {
  padding: 3px 6px;
  border-radius: 4px;
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
  font-size: calc(10px * var(--reading-text-scale, 1));
}
.ev-quote {
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.8;
  color: var(--fn-text-secondary);
}
.ev-chip:hover .ev-page {
  background: var(--fn-bg-active);
}
.ev-chip:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 4px;
}
.ev-none {
  font-size: calc(12px * var(--reading-text-scale, 1));
  color: var(--fn-text-tertiary);
  font-style: italic;
}
.compare-empty {
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 65px 24px;
}
.error {
  color: var(--fn-danger);
}
@media (max-width: 1200px) {
  .select-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 760px) {
  .page-head {
    flex-wrap: wrap;
    gap: 12px;
    align-items: flex-start;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    max-width: 100%;
    min-width: 0;
  }
  .toolbar .el-button {
    margin-left: 0;
  }
  .matrix-run-actions label {
    flex-wrap: wrap;
    width: 100%;
  }
  .matrix-run-actions select {
    width: 100%;
    max-width: 100%;
  }
  .select-grid {
    grid-template-columns: 1fr;
  }
  .select-h {
    flex-wrap: wrap;
    gap: 8px;
  }
}
</style>
