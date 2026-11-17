<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { SourceDocument, SourceRevision, SourceSheet } from '../sourceApi'
import { sourceError } from '../sourceApi'
import {
  previewCsv,
  interpretCsv,
  computeTable,
  tableHistory,
  tableBundleUrl,
  type CsvOptions,
  type CsvPreview,
  type TableComputation,
} from '../tableApi'
const props = defineProps<{
  record: SourceRevision
  document: SourceDocument | null
  conversationId: string
}>()
const emit = defineEmits<{ reinterpreted: [value: SourceRevision] }>()
const busy = ref(false),
  error = ref(''),
  notice = ref(''),
  preview = ref<CsvPreview | null>(null)
const csv = ref<CsvOptions>({ encoding: 'auto', delimiter: 'auto', header_row: 1 })
const history = ref<TableComputation[]>([]),
  selected = ref<TableComputation | null>(null),
  visibleOutputs = ref(100)
const config = ref<TableComputation['config']>({
  sheet: '',
  cell_range: 'A1:A1',
  absolute_tolerance: 1e-9,
  relative_tolerance: 1e-9,
})
const sheetList = computed(() =>
  (props.document?.sheets || []).filter((s): s is SourceSheet => typeof s !== 'string')
)
const candidate = computed(() =>
  preview.value?.candidates.find(
    (c) =>
      c.encoding ===
      (csv.value.encoding === 'auto' ? preview.value?.recommended_encoding : csv.value.encoding)
  )
)
const isCsv = computed(() => props.record.media_type === 'text/csv')
const isCurrent = computed(
  () =>
    !props.record.current_revision_id ||
    props.record.current_revision_id === props.record.revision_id
)
let generation = 0,
  interpretRequest = '',
  computeRequest = '',
  computeSnapshot = ''
watch(
  () => [props.record.revision_id, props.conversationId],
  async () => {
    const token = ++generation
    busy.value = false
    preview.value = null
    selected.value = null
    history.value = []
    error.value = ''
    notice.value = ''
    interpretRequest = ''
    computeRequest = ''
    computeSnapshot = ''
    csv.value = {
      encoding: (props.document?.table_metadata?.encoding || 'auto') as CsvOptions['encoding'],
      delimiter: (props.document?.table_metadata?.delimiter || 'auto') as CsvOptions['delimiter'],
      header_row: props.document?.table_metadata?.header_row || 1,
    }
    if (props.document?.format === 'excel') {
      config.value.sheet = sheetList.value[0]?.name || ''
      config.value.cell_range = `A1:${column(Math.min(sheetList.value[0]?.column_count || 1, 20))}${Math.min(sheetList.value[0]?.row_count || 1, 20)}`
      try {
        const rows = await tableHistory(props.record.revision_id, props.conversationId)
        if (token === generation) {
          history.value = rows.items
          selected.value = rows.items[0] || null
        }
      } catch (e) {
        if (token === generation) error.value = sourceError(e)
      }
    }
  },
  { immediate: true }
)
function column(index: number) {
  let value = ''
  while (index) {
    index--
    value = String.fromCharCode(65 + (index % 26)) + value
    index = Math.floor(index / 26)
  }
  return value
}
function changed() {
  preview.value = null
  interpretRequest = ''
  notice.value = ''
}
async function inspect() {
  const token = generation
  busy.value = true
  error.value = ''
  try {
    const result = await previewCsv(props.record.revision_id, props.conversationId, csv.value)
    if (token === generation) preview.value = result
  } catch (e) {
    if (token === generation) error.value = sourceError(e)
  } finally {
    if (token === generation) busy.value = false
  }
}
async function interpret() {
  if (!candidate.value || !isCurrent.value || busy.value) return
  const token = generation
  busy.value = true
  error.value = ''
  interpretRequest ||= `csv_interpret_${crypto.randomUUID()}`
  try {
    const item = await interpretCsv(
      props.record.revision_id,
      props.conversationId,
      { ...csv.value },
      props.record.version,
      interpretRequest
    )
    if (token === generation) {
      notice.value = '新解析版本已保存，旧引用仍指向原版本。'
      emit('reinterpreted', item)
    }
  } catch (e) {
    if (token === generation) error.value = sourceError(e)
  } finally {
    if (token === generation) busy.value = false
  }
}
async function calculate() {
  if (!props.document?.fingerprint || busy.value) return
  const token = generation,
    snapshot = JSON.stringify(config.value)
  if (computeSnapshot !== snapshot) {
    computeRequest = `table_compute_${crypto.randomUUID()}`
    computeSnapshot = snapshot
  }
  busy.value = true
  error.value = ''
  try {
    const item = await computeTable(
      props.record.revision_id,
      props.conversationId,
      props.document.fingerprint,
      { ...config.value },
      computeRequest
    )
    if (token === generation) {
      selected.value = item
      visibleOutputs.value = 100
      history.value = [item, ...history.value.filter((r) => r.id !== item.id)]
    }
  } catch (e) {
    if (token === generation) error.value = sourceError(e)
  } finally {
    if (token === generation) busy.value = false
  }
}
const comparison: Record<string, string> = {
  matches: '与缓存一致（缓存未验证）',
  mismatch: '与缓存不一致',
  missing_or_nonnumeric: '无可比较数值缓存',
}
function reasonLabel(reason?: string) {
  if (reason?.startsWith('unsupported_function:'))
    return `暂不支持此函数：${reason.slice(reason.indexOf(':') + 1)}`
  const labels: Record<string, string> = {
    blank_dependency_not_imputed: '依赖单元格为空，未填零',
    external_reference_unsupported: '外部工作簿引用未计算',
    circular_reference: '存在循环引用',
    error_cell_dependency: '依赖单元格包含Excel错误，未忽略错误继续计算',
    unsupported_formula_representation: '数组或数据表等特殊公式暂不复算，原表达式和范围已保留',
    text_date_boolean_or_range_not_numeric: '依赖包含文本、日期或布尔值，未强制转数字',
    aggregate_has_no_numeric_data: '聚合范围没有可用数值',
    division_by_zero: '存在除零运算',
    zero_to_zero_undefined: '零的零次方没有可靠数值，保留为未解决',
    unknown_sheet: '引用工作表不在此原件中',
    reference_outside_retained_sheet: '引用超出此固定工作表',
    dependency_depth_exceeded: '依赖层数超过本次核查范围',
    operation_budget_exceeded: '本次计算范围过大，请缩小范围核查',
    formula_nesting_limit: '公式嵌套过深，保留为未解决',
    nonfinite_numeric_value: '计算结果不是有限数值',
  }
  return labels[reason || ''] || '此公式超出当前核查范围，保留为未解决'
}
onBeforeUnmount(() => {
  generation++
})
</script>
<template>
  <details class="table-details">
    <summary>表格解析与数值核查</summary>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <ul v-if="document?.table_metadata?.warnings">
      <li v-for="message in document.table_metadata.warnings" :key="message">{{ message }}</li>
    </ul>
    <template v-if="isCsv">
      <p>
        当前编码 {{ document?.table_metadata?.encoding || '尚未确认' }}，分隔符
        {{
          document?.table_metadata?.delimiter || '尚未确认'
        }}。预览不会改写原件；保存选项会创建新解析版本。
      </p>
      <form @submit.prevent="inspect" class="table-config">
        <label
          >CSV 编码<select aria-label="CSV 编码" v-model="csv.encoding" @change="changed">
            <option value="auto">自动（BOM / UTF-8）</option>
            <option value="utf-8-sig">UTF-8</option>
            <option value="utf-16">UTF-16（带 BOM）</option>
            <option value="gb18030">GB18030 / GBK</option>
            <option value="cp1252">Windows-1252</option>
          </select></label
        >
        <label
          >CSV 分隔符<select aria-label="CSV 分隔符" v-model="csv.delimiter" @change="changed">
            <option value="auto">自动检测</option>
            <option value="comma">逗号</option>
            <option value="semicolon">分号</option>
            <option value="tab">制表符</option>
            <option value="pipe">竖线</option>
          </select></label
        >
        <label
          >表头记录<input
            v-model.number="csv.header_row"
            type="number"
            min="1"
            max="100000"
            step="1"
            required
            @input="changed"
        /></label>
        <button type="submit" :disabled="busy">预览此原件</button>
      </form>
      <div v-if="preview" class="table-preview">
        <p>
          仅预览前 10 条记录、20 列。请核对中文、引号与列分隔；编码可严格解码不等于文字一定正确。
        </p>
        <p v-if="!candidate">当前编码无法确认，请选择一种可读取且文字正确的编码后重新预览。</p>
        <div v-else class="table-overflow">
          <table>
            <tbody>
              <tr v-for="(row, index) in candidate.rows" :key="index">
                <td v-for="(value, col) in row" :key="col">{{ value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <button :disabled="busy || !candidate || !isCurrent" @click="interpret">
          用此选项保存新解析版本
        </button>
        <p v-if="!isCurrent">旧版本保持可核对，请先切换到当前版本再调整解析选项。</p>
      </div>
    </template>
    <template v-else>
      <p>
        日期基准 {{ document?.table_metadata?.date_epoch || '旧提取未记录' }}；公式
        {{ document?.table_metadata?.formula_count ?? '未知' }} 个，其中
        {{ document?.table_metadata?.formula_missing_cache_count ?? '未知' }}
        个没有已保存结果。日期以 ISO 显示，百分比保留原数值和格式。
      </p>
      <ul>
        <li v-for="sheet in sheetList" :key="sheet.name">
          {{ sheet.name }} · {{ sheet.row_count }} 行 × {{ sheet.column_count }} 列 ·
          {{
            sheet.state === 'visible'
              ? '可见'
              : sheet.state === 'hidden' || sheet.state === 'veryHidden'
                ? '隐藏工作表'
                : '状态未记录'
          }}<span v-if="sheet.merged_ranges?.length">
            · 合并 {{ sheet.merged_ranges.length }} 区域</span
          ><span v-if="sheet.headers?.duplicate_header_labels?.length">
            · 重复表头：{{ sheet.headers.duplicate_header_labels.join('、') }}</span
          >
        </li>
      </ul>
      <p>
        本地核查支持数值四则运算、幂、百分号和 SUM / AVERAGE / MIN / MAX /
        COUNT，可跨表引用。缓存不参与计算；不执行宏、外部链接或任意代码，不转换单位。超出支持范围保留为未解决。
      </p>
      <form @submit.prevent="calculate" class="table-config">
        <label
          >核查工作表<select v-model="config.sheet">
            <option v-for="sheet in sheetList" :key="sheet.name" :value="sheet.name">
              {{ sheet.name }}
            </option>
          </select></label
        >
        <label
          >公式范围（最多 10000 格）<input
            v-model="config.cell_range"
            required
            placeholder="A1:D20"
        /></label>
        <label
          >绝对容差<input
            v-model.number="config.absolute_tolerance"
            type="number"
            min="0"
            max="1"
            step="any"
            required
        /></label>
        <label
          >相对容差<input
            v-model.number="config.relative_tolerance"
            type="number"
            min="0"
            max="1"
            step="any"
            required
        /></label>
        <button :disabled="busy || !document?.table_metadata" type="submit">
          实际计算并保存记录
        </button>
      </form>
      <label v-if="history.length"
        >已保存核查记录<select
          :value="selected?.id"
          @change="
            selected =
              history.find((r) => r.id === ($event.target as HTMLSelectElement).value) || null
          "
        >
          <option v-for="item in history" :key="item.id" :value="item.id">
            {{ item.created_at }} · {{ item.config.sheet }}!{{ item.config.cell_range }}
          </option>
        </select></label
      >
      <section v-if="selected" aria-label="表格复算结果">
        <p>
          已计算 {{ selected.result.computed_count }} /
          {{ selected.result.formula_count }} 个公式，未解决
          {{ selected.result.unresolved_count }} 个。<span
            v-if="selected.result.status === 'no_formulas'"
            >所选范围没有公式。</span
          >
        </p>
        <a :href="tableBundleUrl(record.revision_id, conversationId, selected.id)" download
          >下载原件、核查结果与可复算脚本</a
        >
        <div class="table-overflow">
          <table>
            <thead>
              <tr>
                <th>位置与公式</th>
                <th>已保存缓存</th>
                <th>实际计算</th>
                <th>核查状态</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in selected.result.outputs.slice(0, visibleOutputs)"
                :key="item.sheet + item.cell"
              >
                <td>
                  {{ item.sheet }}!{{ item.cell }}<br /><code>{{ item.formula }}</code>
                </td>
                <td>{{ item.cached_value ?? '无' }}</td>
                <td>{{ item.value ?? '未解决' }}</td>
                <td>
                  {{
                    item.status === 'computed'
                      ? comparison[item.cache_comparison || '']
                      : reasonLabel(item.reason)
                  }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <button
          v-if="selected.result.outputs.length > visibleOutputs"
          @click="visibleOutputs += 100"
        >
          再显示 100 个
        </button>
      </section>
    </template>
  </details>
</template>
<style scoped>
.table-details {
  padding: 10px 18px;
  border-bottom: 1px solid #dfe5f1;
  background: #fff;
  color: #203c65;
  font-size: 12px;
  max-height: 55vh;
  overflow: auto;
  flex-shrink: 0;
}
.table-details summary {
  cursor: pointer;
  font-weight: 600;
}
.table-details p,
.table-details li {
  line-height: 1.7;
}
.table-config {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: end;
  margin: 12px 0;
}
.table-config label,
.table-details > label {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.table-config input,
.table-details select {
  padding: 6px;
  border: 1px solid #cad6e7;
  border-radius: 5px;
  max-width: 100%;
  min-width: 0;
}
.table-details button {
  padding: 7px 10px;
  border: 1px solid #cad6e7;
  background: #f5f8ff;
  border-radius: 5px;
  cursor: pointer;
}
.table-details button:disabled {
  opacity: 0.5;
  cursor: default;
}
.table-overflow {
  overflow: auto;
  max-width: 100%;
  margin: 10px 0;
}
.table-details table {
  border-collapse: collapse;
}
.table-details td,
.table-details th {
  padding: 7px;
  border: 1px solid #dfe5f1;
  white-space: pre-wrap;
  min-width: 90px;
  overflow-wrap: anywhere;
}
.table-details :focus-visible {
  outline: 2px solid #355cf6;
  outline-offset: 2px;
}
@media (max-width: 480px) {
  .table-config {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }
  .table-config label {
    min-width: 0;
  }
}
</style>
