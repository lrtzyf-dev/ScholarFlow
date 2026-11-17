<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref } from 'vue'
// 按需引入 echarts，只注册本视图用到的图表/组件，避免全量包撑大 vendor chunk
import * as echarts from 'echarts/core'
import { BarChart, LineChart, RadarChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  RadarComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  LineChart,
  RadarChart,
  GridComponent,
  LegendComponent,
  RadarComponent,
  TooltipComponent,
  CanvasRenderer,
])
import { parseCsv, generateCaption, type CaptionResult, type CsvParseResult } from '../api'
import SaveArtifactButton from '../components/SaveArtifactButton.vue'

type ChartType = 'bar' | 'line' | 'radar'

const chartEl = ref<HTMLDivElement | null>(null)
const csvText = ref('')
const filename = ref('')
const parsed = ref<CsvParseResult | null>(null)
const chartType = ref<ChartType>('bar')
const categoryColumn = ref('')
const selectedNumeric = ref<string[]>([])
const caption = ref<CaptionResult | null>(null)
const error = ref('')
const loading = ref(false)
const units = ref<Record<string, string>>({})
const savedCsv = ref('')
const analysisConfig = computed(() => ({ category_column: categoryColumn.value, numeric_columns: selectedNumeric.value,
  chart_type: chartType.value, missing_policy: 'gap', units: Object.fromEntries(selectedNumeric.value.map((name) => [name, units.value[name] || 'unspecified'])),
  title: filename.value || '实验数据图表' }))
const finiteValue = (value: string | undefined) => {
  if (!value?.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}
let chart: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null
let captionRequest = 0
const chartColors = ['#355cf6', '#7699eb', '#a8bef1', '#526b96', '#bccbeb']

const categoryIndex = computed(() =>
  parsed.value ? parsed.value.columns.indexOf(categoryColumn.value) : -1
)

async function onFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  try {
    csvText.value = await file.text()
    filename.value = file.name
  } catch {
    error.value = '无法读取 CSV 文件，请直接粘贴内容。'
  }
}

async function doParse() {
  loading.value = true
  error.value = ''
  try {
    const result = await parseCsv(csvText.value)
    if (result.columns.length === 0) throw new Error('CSV 内容为空')
    parsed.value = result
    savedCsv.value = csvText.value
    // 默认类别轴：第一个非数值列，否则第一列
    const nonNumeric = result.columns.find((c) => !result.numeric_columns.includes(c))
    categoryColumn.value = nonNumeric || result.columns[0]
    selectedNumeric.value = [...result.numeric_columns]
    await nextTick()
    renderChart()
    await loadCaption()
  } catch (err: any) {
    error.value = err?.response?.data?.detail || err?.message || String(err)
  } finally {
    loading.value = false
  }
}

async function onConfigChange() {
  error.value = ''
  try {
    renderChart()
    await loadCaption()
  } catch (err: any) {
    error.value = err?.response?.data?.detail || err?.message || '图表更新失败，请重试。'
  }
}

// 切换图表类型并重新渲染、刷新图注
async function selectChartType(type: ChartType) {
  chartType.value = type
  await onConfigChange()
}

function renderChart() {
  if (!chart || !parsed.value) return
  const rows = parsed.value.rows
  const numerics = selectedNumeric.value
  const catIdx = categoryIndex.value

  if (chartType.value === 'radar') {
    chart.setOption(
      {
        color: chartColors,
        textStyle: { fontFamily: 'Inter, "Noto Sans SC", sans-serif', color: '#66748c' },
        tooltip: {},
        legend: { bottom: 0, type: 'scroll' },
        radar: {
          indicator: numerics.map((c) => ({
            name: c,
            max: parsed.value!.stats[c]?.max || 1,
          })),
        },
        series: [
          {
            type: 'radar',
            data: rows.map((r) => ({
              name: r[catIdx],
              value: numerics.map((c) => finiteValue(r[parsed.value!.columns.indexOf(c)])),
            })),
          },
        ],
      },
      true
    )
    return
  }

  chart.setOption(
    {
      color: chartColors,
      textStyle: { fontFamily: 'Inter, "Noto Sans SC", sans-serif', color: '#66748c' },
      tooltip: { trigger: 'axis' },
      legend: { bottom: 0, type: 'scroll' },
      grid: { left: 50, right: 24, top: 30, bottom: 60 },
      xAxis: { type: 'category', data: rows.map((r) => r[catIdx]), axisLabel: { rotate: 20 } },
      yAxis: { type: 'value' },
      series: numerics.map((c) => ({
        name: c,
        type: chartType.value,
        data: rows.map((r) => finiteValue(r[parsed.value!.columns.indexOf(c)])),
        smooth: false,
      })),
    },
    true
  )
}

const chartTypeLabel = computed(
  () => ({ bar: 'bar chart', line: 'line chart', radar: 'radar chart' })[chartType.value]
)

async function loadCaption() {
  if (!parsed.value) return
  const request = ++captionRequest
  caption.value = null
  const nextCaption = await generateCaption(parsed.value, chartTypeLabel.value)
  if (request === captionRequest) caption.value = nextCaption
}

onMounted(() => {
  chart = echarts.init(chartEl.value!)
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartEl.value!)
})
onBeforeUnmount(() => {
  ++captionRequest
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div class="page writing-page data-page">
    <div class="page-head">
      <div>
        <div class="page-title">数据图表</div>
        <div class="page-desc">导入实验数据，调整图表与学术图注。</div>
      </div>
    </div>

    <div class="layout">
      <div class="card input-card">
        <div class="editor-heading">数据源<span>CSV</span></div>
        <div class="editor-body">
          <textarea
            v-model="csvText"
            class="csv-input"
            aria-label="CSV 数据"
            placeholder="粘贴 CSV 内容，首行为列名。"
            spellcheck="false"
          ></textarea>
          <div class="input-actions">
            <label class="el-button file-btn">
              选择文件
              <input type="file" accept=".csv,text/csv" hidden @change="onFile" />
            </label>
            <button
              class="el-button el-button--primary"
              :disabled="loading || !csvText.trim()"
              @click="doParse"
            >
              {{ loading ? '解析中…' : '解析并绘图' }}
            </button>
          </div>
          <p class="data-hint">{{ filename || '首行为列名，支持双引号内的逗号与换行。' }}</p>
          <p v-if="error" class="err" role="alert">{{ error }}</p>

          <template v-if="parsed">
            <div class="sec-h">类别轴</div>
            <select v-model="categoryColumn" class="sel" @change="onConfigChange">
              <option v-for="c in parsed.columns" :key="c" :value="c">{{ c }}</option>
            </select>

            <div class="sec-h">数值列</div>
            <div class="check-list">
              <label v-for="c in parsed.numeric_columns" :key="c" class="check-row">
                <input
                  type="checkbox"
                  :value="c"
                  v-model="selectedNumeric"
                  @change="onConfigChange()"
                />
                <span>{{ c }}</span>
                <input v-model="units[c]" type="text" maxlength="80" :aria-label="`${c} 的单位`" placeholder="单位，例如 ms、%" />
              </label>
            </div>
            <p v-if="parsed.truncated" class="warn">数据超过上限，仅展示前 10000 行。</p>
            <p class="data-hint">
              已识别 {{ parsed.rows.length }} 行，{{ parsed.columns.length }} 列。
            </p>
          </template>
        </div>
      </div>

      <div class="card chart-card">
        <div class="chart-heading">
          <h2>图表预览</h2>
          <div class="view-switch">
            <button :class="{ active: chartType === 'bar' }" @click="selectChartType('bar')">
              柱状
            </button>
            <button :class="{ active: chartType === 'line' }" @click="selectChartType('line')">
              折线
            </button>
            <button :class="{ active: chartType === 'radar' }" @click="selectChartType('radar')">
              雷达
            </button>
          </div>
        </div>
        <SaveArtifactButton v-if="parsed && selectedNumeric.length" :title="filename || '实验数据图表'" :content="savedCsv" kind="analysis" :analysis="analysisConfig" :disabled="loading" />
        <div class="chart-stage">
          <div
            ref="chartEl"
            class="chart-box"
            :class="{ hidden: !parsed || !selectedNumeric.length }"
          ></div>
          <div v-if="!parsed || !selectedNumeric.length" class="chart-empty">
            {{ parsed ? '选择至少一个数值列以显示图表。' : '导入 CSV 数据后，在这里预览图表。' }}
          </div>
        </div>
        <div v-if="caption" class="caption-box">
          <div class="caption-head">
            <span class="caption-label">Figure Caption</span>
            <span class="caption-mode" :class="caption.mode">
              {{ caption.mode === 'llm' ? '模型生成' : '离线模板' }}
            </span>
          </div>
          <p class="caption-text">{{ caption.caption }}</p>
          <p v-if="caption.mode === 'template'" class="caption-note">
            当前未配置大模型，图注由真实统计数据经模板生成；配置模型后可生成更自然的学术图注。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 28px;
  align-items: start;
}
.input-card {
  padding: 0;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: none;
}
.editor-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 19px;
  border-bottom: 1px solid var(--fn-border-primary);
  font-size: 12px;
  font-weight: 600;
}
.editor-heading span {
  font-weight: 400;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.editor-body {
  padding: 20px;
}
.sec-h {
  font-size: 12px;
  color: var(--fn-text-primary);
  font-weight: 500;
  margin: 22px 0 10px;
}
.csv-input {
  width: 100%;
  min-height: 235px;
  border: 1px solid var(--fn-border-input);
  border-radius: 6px;
  padding: 10px 11px;
  font:
    11px/1.85 ui-monospace,
    Consolas,
    monospace;
  resize: vertical;
  white-space: pre;
  overflow: auto;
  color: var(--fn-text-primary);
  background: var(--fn-bg-primary);
}
.input-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 20px;
}
.file-btn {
  cursor: pointer;
}
.sel {
  width: 100%;
  padding: 8px 9px;
  border: 1px solid var(--fn-border-input);
  border-radius: 6px;
  font-size: 12px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.check-list {
  display: flex;
  gap: 10px 15px;
  flex-wrap: wrap;
}
.check-row {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 11px;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.check-row input {
  width: 13px;
  height: 13px;
  accent-color: var(--fn-brand);
  margin: 0;
}
.chart-card {
  min-width: 0;
  padding: 8px 0 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.chart-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.chart-heading h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.view-switch {
  display: flex;
  padding: 3px;
  background: var(--fn-bg-secondary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
}
.view-switch button {
  border: 0;
  background: transparent;
  border-radius: 4px;
  padding: 5px 9px;
  color: var(--fn-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.view-switch button.active {
  color: var(--fn-brand);
  background: var(--fn-bg-primary);
  box-shadow: 0 1px 3px #0000000a;
}
.chart-stage {
  position: relative;
  padding-top: 28px;
}
.chart-box {
  height: 370px;
  width: 100%;
}
.chart-box.hidden {
  visibility: hidden;
}
.chart-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  text-align: center;
  padding: 25px;
  color: var(--fn-text-secondary);
  font-size: 12px;
}
.caption-box {
  margin-top: 18px;
  padding-top: 20px;
  border-top: 1px solid var(--fn-border-primary);
}
.caption-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.caption-label {
  color: var(--fn-text-secondary);
  font-size: 10px;
}
.caption-mode {
  font-size: 10px;
  padding: 3px 6px;
  border-radius: 4px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
}
.caption-mode.llm {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.caption-text {
  font-size: 12px;
  line-height: 1.85;
  color: var(--fn-text-secondary);
  margin: 10px 0 0;
}
.caption-note,
.data-hint {
  font-size: 11px;
  line-height: 1.8;
  color: var(--fn-text-tertiary);
  margin: 8px 0 0;
}
.err {
  color: var(--fn-danger);
  font-size: 12px;
  line-height: 1.8;
  margin-top: 12px;
}
.warn {
  color: var(--fn-warning-text);
  font-size: 11px;
  margin-top: 8px;
}
@media (max-width: 1200px) {
  .layout {
    grid-template-columns: 280px minmax(0, 1fr);
    gap: 24px;
  }
  .editor-body {
    padding: 17px;
  }
}
@media (max-width: 900px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .csv-input {
    min-height: 160px;
  }
  .chart-box {
    height: 320px;
  }
}
</style>
