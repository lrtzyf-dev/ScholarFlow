<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { TranslationRun } from '../translationRunApi'
import { useTranslationRuns } from '../useTranslationRuns'
import { activeConversationId } from '../desktopState'
import SaveArtifactButton from '../components/SaveArtifactButton.vue'
import { getSourceLocator, getSourceRevision, sourceError, type SourceRevision } from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'

const route = useRoute(),
  router = useRouter()
const mode = ref<'whole' | 'parallel'>('whole')
const sourceText = ref('')
const sourceLoading = ref(false),
  sourceLoadError = ref('')
const fixedSource = ref<{
  revision: SourceRevision
  locatorId: string
  conversationId: string
} | null>(null)
let epoch = 0,
  sourceEpoch = 0
let restoring = false,
  detaching = false
const conversation = computed(() =>
  typeof route.query.conversation === 'string'
    ? route.query.conversation
    : activeConversationId.value
)
const runs = useTranslationRuns(conversation, restoreRun)
const { job, jobs, selected, error, submitting, retryUnknown, loading } = runs
watch(
  () => job.value?.run_id,
  (id) => {
    if (id && route.query.run !== id)
      void router.replace({
        path: '/translate',
        query: { conversation: conversation.value, run: id },
      })
  }
)
const result = computed(() => (mode.value === 'whole' ? job.value?.result || null : null))
const parallel = computed(() => (mode.value === 'parallel' ? job.value?.result || null : null))
const requestedFixed = computed(() =>
  ['revision', 'locator'].some((key) => route.query[key] !== undefined)
)
const runHeadline = computed(() =>
  !job.value
    ? ''
    : job.value.status === 'stopped'
      ? '已停止，已完成段落保留'
      : job.value.status === 'interrupted'
        ? '执行中断，可核对后继续'
        : job.value.status === 'paused'
          ? '资源限制已暂停，段落进度保留'
          : job.value.stage === 'completed'
            ? '译文已保存，仍需人工校核'
            : job.value.stage === 'partial'
              ? '已有部分结果，翻译尚未完成'
              : job.value.stage === 'restoring'
                ? '正在恢复已保存段落'
                : job.value.stage === 'translating'
                  ? '正在翻译，离页后继续'
                  : '正在准备固定原文'
)
const currentResult = computed(() => (mode.value === 'whole' ? result.value : parallel.value))
const evidence = computed(() => currentResult.value?.source_evidence || [])
const artifactContent = computed(() => {
  const translated =
    mode.value === 'whole'
      ? result.value?.translation
      : parallel.value?.units.map((u) => u.target || '').join('\n\n')
  if (!translated || currentResult.value?.mode !== 'llm') return ''
  return (
    (currentResult.value?.partial ? '# 学术译文（部分完成）\n\n' : '# 学术译文\n\n') +
    translated +
    (evidence.value.length
      ? '\n\n原文来源：[T1]。译文未经过人工校核，来源留存不代表科学结论已核验。'
      : '\n\n原文为用户输入，译文未经过人工校核。')
  )
})
function invalidate() {
  ++epoch
  runs.reset(true)
}
const draftKey = () => `scholarflow:translation-draft:${conversation.value}`
function saveDraft() {
  try {
    sessionStorage.setItem(draftKey(), JSON.stringify({ text: sourceText.value, mode: mode.value }))
  } catch {
    /* Server results remain durable. */
  }
}
function restoreDraft() {
  try {
    const value = JSON.parse(sessionStorage.getItem(draftKey()) || 'null')
    if (typeof value?.text === 'string' && value.text.length <= 1000000)
      sourceText.value = value.text
    if (value?.mode === 'whole' || value?.mode === 'parallel') mode.value = value.mode
  } catch {
    /* A malformed local draft does not hide saved tasks. */
  }
}
async function setFixed(
  revision: string,
  locator: string,
  cid: string,
  token: number,
  setText = true
) {
  sourceLoading.value = true
  try {
    const [record, position] = await Promise.all([
      getSourceRevision(revision, cid),
      getSourceLocator(revision, locator, cid),
    ])
    if (token !== sourceEpoch) return
    if (setText) {
      restoring = true
      sourceText.value = position.quote
      restoring = false
    }
    fixedSource.value = { revision: record, locatorId: locator, conversationId: cid }
  } catch (err) {
    if (token === sourceEpoch) sourceLoadError.value = sourceError(err)
  } finally {
    if (token === sourceEpoch) sourceLoading.value = false
  }
}
function restoreRun(value: TranslationRun) {
  const token = ++sourceEpoch
  restoring = true
  sourceText.value = value.inputs.text
  mode.value = value.inputs.display_mode
  fixedSource.value = null
  sourceLoadError.value = ''
  sourceLoading.value = false
  restoring = false
  const ref = value.inputs.source_ref
  if (ref) void setFixed(ref.revision_id, ref.locator_id, value.conversation_id, token, false)
}
watch(
  () => [conversation.value, route.query.revision, route.query.locator, route.query.run],
  async () => {
    invalidate()
    const token = ++sourceEpoch
    fixedSource.value = null
    sourceLoadError.value = ''
    sourceLoading.value = false
    restoring = true
    if (!detaching) sourceText.value = ''
    if (!requestedFixed.value && !detaching) restoreDraft()
    restoring = false
    detaching = false
    const runId = typeof route.query.run === 'string' ? route.query.run : ''
    void runs.history(
      runId,
      !runId && !requestedFixed.value && !sourceText.value.trim(),
      (value) =>
        !requestedFixed.value &&
        value.inputs.text === sourceText.value &&
        value.inputs.display_mode === mode.value
    )
    if (!requestedFixed.value) return
    restoring = true
    sourceText.value = ''
    restoring = false
    const { revision, locator, conversation } = route.query
    if (
      typeof revision !== 'string' ||
      typeof locator !== 'string' ||
      typeof conversation !== 'string' ||
      !/^[a-f0-9]{32}$/.test(revision) ||
      !/^[a-f0-9]{32}$/.test(locator) ||
      !/^[\w-]{1,64}$/.test(conversation)
    ) {
      sourceLoadError.value = '固定来源链接不完整，请回到阅读器重新选择原文。'
      return
    }
    await setFixed(revision, locator, conversation, token)
  },
  { immediate: true }
)

async function detachSource() {
  const query = { ...route.query }
  delete query.revision
  delete query.locator
  delete query.run
  query.conversation = conversation.value
  detaching = true
  invalidate()
  fixedSource.value = null
  saveDraft()
  await router.replace({ query })
}

const leftScroll = ref<HTMLElement | null>(null)
const rightScroll = ref<HTMLElement | null>(null)
let syncing = false

function run() {
  if (
    loading.value ||
    submitting.value ||
    sourceLoading.value ||
    sourceLoadError.value ||
    !sourceText.value.trim()
  )
    return
  const fixed = fixedSource.value
  const key = draftKey()
  runs.create(
    {
      text: sourceText.value,
      source: 'Chinese',
      target: 'English',
      display_mode: mode.value,
      source_ref: fixed
        ? { revision_id: fixed.revision.revision_id, locator_id: fixed.locatorId }
        : null,
    },
    () => {
      try {
        sessionStorage.removeItem(key)
      } catch {
        /* No credentials are stored in the draft. */
      }
    }
  )
}

// 双栏同步滚动：一侧滚动时按比例同步另一侧
function syncFrom(side: 'left' | 'right') {
  if (syncing || !leftScroll.value || !rightScroll.value) return
  syncing = true
  const source = side === 'left' ? leftScroll.value : rightScroll.value
  const target = side === 'left' ? rightScroll.value : leftScroll.value
  const extent = source.scrollHeight - source.clientHeight
  target.scrollTop =
    extent > 0 ? (source.scrollTop / extent) * (target.scrollHeight - target.clientHeight) : 0
  requestAnimationFrame(() => {
    syncing = false
  })
}
watch(
  [sourceText, mode],
  () => {
    if (!restoring) {
      invalidate()
      saveDraft()
    }
  },
  { flush: 'sync' }
)
onBeforeUnmount(() => {
  ++epoch
  ++sourceEpoch
})
</script>

<template>
  <div class="page writing-page translate-page">
    <div class="page-head">
      <div>
        <div class="page-title">中译英</div>
        <div class="page-desc">
          保持术语一致，在原文与译文之间核对表达。使用本次会话模型，固定原文与术语表；离页后继续执行。
        </div>
      </div>
      <div class="toolbar">
        <div class="view-switch">
          <button
            :class="{ active: mode === 'whole' }"
            :disabled="loading || submitting"
            @click="mode = 'whole'"
          >
            整篇翻译
          </button>
          <button
            :class="{ active: mode === 'parallel' }"
            :disabled="loading || submitting"
            @click="mode = 'parallel'"
          >
            对照翻译
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="requestedFixed || fixedSource"
      class="translation-provenance"
      aria-label="翻译固定来源"
    >
      <p v-if="sourceLoading" role="status">正在读取固定版本原文…</p>
      <p v-else-if="sourceLoadError" class="err" role="alert">{{ sourceLoadError }}</p>
      <template v-else-if="fixedSource">
        <RouterLink
          :to="
            sourceReaderLocation(
              fixedSource.revision.revision_id,
              fixedSource.conversationId,
              fixedSource.locatorId
            )
          "
        >
          原文：{{ fixedSource.revision.title }} · 版本 {{ fixedSource.revision.version }}
        </RouterLink>
        <p>选段已绑定固定版本，后续更新文件不会改变这里的原文。来源可追溯，译文仍需校核。</p>
      </template>
      <button
        type="button"
        class="el-button"
        :disabled="sourceLoading || loading || submitting"
        @click="detachSource"
      >
        转为自由文本
      </button>
    </div>

    <div class="translation-provenance translation-run" aria-label="翻译运行记录">
      <label v-if="jobs.length"
        >已保存的翻译任务
        <select v-model="selected" :disabled="submitting" @change="runs.select">
          <option value="" disabled>选择已保存的任务</option>
          <option v-for="saved in jobs" :key="saved.run_id" :value="saved.run_id">
            {{ saved.title }} · {{ new Date(saved.updated_at).toLocaleString() }}
          </option>
        </select>
      </label>
      <template v-if="job">
        <strong>{{ runHeadline }}</strong>
        <p>
          模型：{{ job.model.profile_name }} · {{ job.model.model_id }} · 第{{
            job.attempt_number
          }}次执行
        </p>
        <p v-if="job.result">
          已保存 {{ job.result.progress.completed_units }}/{{ job.result.progress.total_units }}
          段；来源留存不代表译文语义已核验。
        </p>
        <ol>
          <li v-for="(event, index) in job.events.slice(-3)" :key="`${index}:${event.at}`">
            {{ event.text }}
          </li>
        </ol>
        <p v-if="job.error" class="err">{{ job.error }}</p>
        <p v-if="job.resource_pause" class="err">{{ job.resource_pause.message }}</p>
        <p v-if="job.resume_blocked_reason">{{ job.resume_blocked_reason }}</p>
        <template v-if="job.can_resume">
          <label v-if="job.requires_unknown_confirmation"
            ><input
              v-model="retryUnknown"
              type="checkbox"
            />我已核对上次调用，确认重试未确认调用可能再次产生费用</label
          >
          <button
            class="el-button"
            :disabled="submitting || (job.requires_unknown_confirmation && !retryUnknown)"
            @click="runs.resume"
          >
            {{ job.requires_unknown_confirmation ? '重试未确认调用' : '继续未完成翻译' }}
          </button>
        </template>
        <button v-if="loading" class="el-button" :disabled="submitting" @click="runs.cancel">
          停止此翻译
        </button>
      </template>
      <p v-else>提交后逐段保存结果，重新打开可从任务记录恢复；未知接口结果不会自动重发。</p>
      <p v-if="error" class="err" role="alert">{{ error }}</p>
      <button v-if="error" class="el-button" :disabled="submitting" @click="runs.refresh">
        重新载入翻译状态
      </button>
    </div>

    <div class="trans-layout" :class="{ 'parallel-mode': mode === 'parallel' }">
      <div class="card trans-col">
        <div class="col-head">
          <label for="translation-source">中文原文</label><span class="language">Chinese</span>
        </div>
        <textarea
          id="translation-source"
          v-model="sourceText"
          :readonly="requestedFixed || !!fixedSource"
          :disabled="loading || submitting"
          class="trans-textarea"
          placeholder="输入或粘贴需要翻译的中文摘要、段落或全文…"
          spellcheck="false"
        ></textarea>
        <div class="source-footer">
          <span>{{ sourceText.length }} 字符</span><span>公式与代码段保留</span>
        </div>
        <button
          class="el-button el-button--primary translate-btn"
          :disabled="
            loading || submitting || sourceLoading || !!sourceLoadError || !sourceText.trim()
          "
          @click="run"
        >
          {{ loading || submitting ? '翻译中…' : mode === 'parallel' ? '生成对照' : '翻译为英文' }}
        </button>
      </div>

      <!-- 整篇翻译 -->
      <div v-if="mode === 'whole'" class="card trans-col">
        <div class="col-head">
          英文译文 <span class="language">English</span>
          <span v-if="result" class="mode-badge" :class="result.mode">
            {{ result.mode === 'llm' ? '模型生成' : '离线模式' }}
          </span>
        </div>
        <div v-if="!result" class="placeholder">点击翻译按钮后，译文将显示在这里。</div>
        <template v-else>
          <div v-if="result.translation" class="result-text">{{ result.translation }}</div>
          <div v-else class="offline-box">
            <p class="offline-msg">{{ result.message }}</p>
          </div>
          <div v-if="result.glossary_hits.length" class="glossary-box">
            <div class="glossary-title">术语表命中（{{ result.glossary_hits.length }}）</div>
            <div class="term-list">
              <div v-for="h in result.glossary_hits" :key="h.source" class="term-row">
                <span class="term-zh">{{ h.source }}</span>
                <span class="term-arrow">→</span>
                <span class="term-en">{{ h.target }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 对照翻译 -->
      <div v-else class="card parallel-card">
        <div class="col-head">
          中英对照
          <span v-if="parallel" class="mode-badge" :class="parallel.mode">
            {{ parallel.mode === 'llm' ? '模型生成' : '离线模式' }}
          </span>
        </div>
        <div v-if="!parallel" class="placeholder">
          点击「生成对照」后，逐段对齐的双栏将显示在这里。
        </div>
        <div v-else class="parallel-grid">
          <div ref="leftScroll" class="pane" @scroll="syncFrom('left')">
            <div
              v-for="u in parallel.units"
              :key="u.index"
              class="unit"
              :class="{ preserve: u.preserve }"
            >
              {{ u.source }}
            </div>
          </div>
          <div ref="rightScroll" class="pane" @scroll="syncFrom('right')">
            <template v-for="u in parallel.units" :key="u.index">
              <div v-if="u.preserve" class="unit preserve">{{ u.target }}</div>
              <div v-else-if="u.target" class="unit">{{ u.target }}</div>
              <div v-else class="unit offline-unit">
                {{
                  parallel.mode === 'offline'
                    ? '（离线模式，配置模型后新建翻译）'
                    : '（此段尚未完成，已完成段落保留）'
                }}
              </div>
            </template>
          </div>
        </div>
      </div>
    </div>
    <div v-if="artifactContent" class="translation-delivery">
      <p v-if="evidence.length">原文来源已固定，科学结论支持程度未核验；模型翻译仍需人工校核。</p>
      <SaveArtifactButton
        :key="`${epoch}:${job?.run_id}:${job?.attempt_number}`"
        title="学术译文"
        :content="artifactContent"
        :evidence="evidence"
        :conversation-id="job?.conversation_id"
      />
    </div>
  </div>
</template>

<style scoped>
.translation-provenance,
.translation-delivery {
  margin: 0 0 20px;
  padding: 16px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  overflow-wrap: anywhere;
  color: var(--fn-text-secondary);
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.8;
}
.translation-provenance a {
  color: var(--fn-brand);
}
.translation-run strong {
  display: block;
  margin: 12px 0;
}
.translation-run select {
  max-width: 100%;
  width: 100%;
  padding: 8px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.translation-run label {
  display: block;
}
.translation-run input[type='checkbox'] {
  margin-right: 6px;
}
.translation-run button {
  margin: 6px 8px 0 0;
}
.translation-delivery {
  margin-top: 20px;
}
.trans-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 24px;
  align-items: stretch;
  min-height: 480px;
}
.trans-col,
.parallel-card {
  min-width: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: none;
}
.col-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 16px 19px;
  border-bottom: 1px solid var(--fn-border-primary);
  font-size: calc(12px * var(--reading-text-scale, 1));
  font-weight: 600;
  color: var(--fn-text-primary);
}
.language {
  margin-left: auto;
  font-size: calc(11px * var(--reading-text-scale, 1));
  font-weight: 400;
  color: var(--fn-text-secondary);
}
.mode-badge {
  font-size: calc(10px * var(--reading-text-scale, 1));
  font-weight: 400;
  padding: 3px 6px;
  border-radius: 4px;
}
.mode-badge.llm {
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
}
.mode-badge.offline {
  color: var(--fn-text-secondary);
  background: var(--fn-bg-secondary);
}
.view-switch {
  display: flex;
  gap: 25px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.view-switch button {
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 0 0 11px;
  background: transparent;
  color: var(--fn-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.view-switch button.active {
  color: var(--fn-brand);
  border-bottom-color: var(--fn-brand);
}
.trans-textarea {
  flex: 1;
  width: calc(100% - 40px);
  margin: 20px;
  padding: 8px;
  min-height: 300px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  font: inherit;
  font-size: calc(13px * var(--reading-text-scale, 1));
  line-height: 2;
  resize: vertical;
}
.trans-textarea:focus {
  outline: none;
  border-color: var(--fn-brand);
}
.source-footer {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 18px;
  border-top: 1px solid var(--fn-border-primary);
  font-size: calc(10px * var(--reading-text-scale, 1));
  color: var(--fn-text-tertiary);
}
.translate-btn {
  margin: 0 18px 16px;
  align-self: flex-end;
}
.placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 340px;
  flex: 1;
  padding: 25px;
  text-align: center;
  color: var(--fn-text-secondary);
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.8;
}
.result-text {
  flex: 1;
  padding: 20px;
  font-size: calc(13px * var(--reading-text-scale, 1));
  line-height: 2;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--fn-text-primary);
}
.offline-box {
  margin: 20px;
  padding: 14px;
  background: var(--fn-bg-secondary);
  border-radius: 5px;
}
.offline-msg {
  margin: 0;
  font-size: calc(12px * var(--reading-text-scale, 1));
  line-height: 1.8;
  color: var(--fn-text-secondary);
}
.glossary-box {
  margin: 0 20px 20px;
  padding-top: 17px;
  border-top: 1px solid var(--fn-border-primary);
}
.glossary-title {
  margin-bottom: 12px;
  font-size: calc(11px * var(--reading-text-scale, 1));
  color: var(--fn-text-secondary);
}
.term-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 24px;
}
.term-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: calc(11px * var(--reading-text-scale, 1));
  line-height: 1.8;
}
.term-zh {
  color: var(--fn-text-primary);
}
.term-arrow {
  color: var(--fn-text-tertiary);
}
.term-en {
  color: var(--fn-text-secondary);
}
.parallel-mode {
  grid-template-columns: minmax(0, 1fr);
}
.parallel-mode .trans-textarea {
  min-height: 140px;
}
.parallel-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  min-height: 360px;
  max-height: 650px;
}
.pane {
  overflow-y: auto;
  padding: 0;
}
.pane + .pane {
  border-left: 1px solid var(--fn-border-primary);
}
.unit {
  padding: 20px 21px;
  min-height: 80px;
  border-bottom: 1px solid var(--fn-border-primary);
  font-size: calc(13px * var(--reading-text-scale, 1));
  line-height: 1.95;
  color: var(--fn-text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.unit.preserve {
  font-family: ui-monospace, monospace;
  font-size: calc(12px * var(--reading-text-scale, 1));
  color: var(--fn-text-secondary);
  background: var(--fn-bg-secondary);
}
.offline-unit {
  color: var(--fn-text-tertiary);
  font-style: italic;
}
.err {
  font-size: 12px;
  line-height: 1.8;
  color: var(--fn-danger);
  margin: 0 18px 16px;
}
@media (max-width: 1000px) {
  .trans-layout {
    gap: 18px;
  }
  .trans-textarea {
    width: calc(100% - 24px);
    margin: 12px;
  }
}
@media (max-width: 800px) {
  .trans-layout {
    grid-template-columns: 1fr;
  }
  .trans-textarea {
    min-height: 245px;
  }
  .unit {
    padding: 16px 12px;
    font-size: calc(12px * var(--reading-text-scale, 1));
  }
}
</style>
