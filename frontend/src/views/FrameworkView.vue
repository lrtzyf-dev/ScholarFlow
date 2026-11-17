<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { FrameworkOut } from '../api'
import {
  cancelFrameworkRun,
  createFrameworkRun,
  getFrameworkRun,
  listFrameworkRuns,
  resumeFrameworkRun,
  type ResearchCreate,
  type ResearchRun,
  type ResearchRunSummary,
} from '../researchRunApi'
import SaveArtifactButton from '../components/SaveArtifactButton.vue'
import { activeConversationId } from '../desktopState'
import { ensureSourceConversation, sourceError } from '../sourceApi'
import { sourceReaderLocation } from '../sourceSelection'

const idea = ref('')
const maxSources = ref(8)
const loading = ref(false)
const error = ref('')
const result = ref<FrameworkOut | null>(null)
const activeRef = ref<number | null>(null)
const route = useRoute()
const router = useRouter()
const job = ref<ResearchRun | null>(null),
  jobs = ref<ResearchRunSummary[]>([]),
  selectedRun = ref('')
const submitting = ref(false),
  retryUnknown = ref(false)
watch(
  () => job.value?.run_id,
  (id) => {
    if (id && route.query.run !== id)
      void router.replace({ path: '/framework', query: { run: id } })
  }
)
const stageLabels: Record<string, string> = {
  queued: '等待执行',
  preparing: '准备固定输入',
  generating: '生成章节',
  reviewing: '复核论点',
  completed: '结果已保存',
  partial: '结果已保存，仍有待补齐内容',
  restoring: '恢复已保存结果',
}
const runHeadline = computed(() =>
  !job.value
    ? ''
    : job.value.status === 'stopped'
      ? '已停止，已保存结果保留'
      : job.value.status === 'interrupted'
        ? '执行中断，核对后可继续'
        : job.value.status === 'paused'
          ? '已暂停，草稿与进度已保存'
          : stageLabels[job.value.stage] || job.value.stage
)
let pollTimer: ReturnType<typeof setTimeout> | undefined, readRequest: AbortController | undefined
let pendingCreate: ResearchCreate | null = null,
  pendingResume: { runId: string; revision: number; requestId: string; retry: boolean } | null =
    null
const frameworkMarkdown = computed(() =>
  result.value
    ? `> 写作草稿。结构和来源定位检查不等于科研结论认证；模型复核结果仍需人工审阅。\n\n${result.value.sections
        .map(
          (section) =>
            `## ${section.title}\n\n${section.bullets.map((bullet) => `- ${bullet.text}${(bullet.evidence_ids?.length ? bullet.evidence_ids : bullet.citations.map((id) => `R${id}`)).map((id) => ` [${id}]`).join('')}${bullet.support_status ? `\n  ${supportLabels[bullet.support_status] || '论点待核验'}${bullet.support_review ? `：${bullet.support_review.explanation}` : ''}` : ''}`).join('\n')}\n`
        )
        .join('\n')}`
    : ''
)
const locatedEvidence = computed(
  () => result.value?.references.flatMap((item) => item.evidence || []) || []
)
const frameworkEvidence = computed(() => [
  ...locatedEvidence.value,
  ...(result.value?.references.map((item) => ({
    id: `R${item.ref_index}`,
    paper_id: item.paper_id,
    paper_title: item.title,
    url: item.url,
  })) || []),
])
const evidenceById = computed(() => new Map(locatedEvidence.value.map((item) => [item.id, item])))
const supportLabels: Record<string, string> = {
  model_supported: '模型复核：片段支持',
  model_conflict: '模型复核：存在冲突',
  model_insufficient: '模型复核：证据不足',
  unverified: '论点待核验',
}
let requestEpoch = 0
let observationEpoch = 0
let restoringDraft = false
const draftKey = () => `scholarflow:framework-draft:${activeConversationId.value}`
function restoreDraft() {
  restoringDraft = true
  try {
    const raw = sessionStorage.getItem(draftKey())
    idea.value = ''
    result.value = null
    maxSources.value = 8
    error.value = ''
    if (!raw || raw.length > 1_000_000) return
    const saved = JSON.parse(raw)
    if (typeof saved.idea === 'string') idea.value = saved.idea.slice(0, 10000)
    if (Number.isInteger(saved.maxSources) && saved.maxSources >= 1 && saved.maxSources <= 20)
      maxSources.value = saved.maxSources
    if (
      saved.result?.conversation_id === activeConversationId.value &&
      Array.isArray(saved.result.sections) &&
      Array.isArray(saved.result.references)
    )
      result.value = saved.result
  } catch {
    /* A stored draft cannot prevent opening the writing page. */
  } finally {
    restoringDraft = false
  }
}
restoreDraft()
watch(
  [idea, maxSources, result],
  () => {
    if (restoringDraft) return
    try {
      const value = JSON.stringify({
        idea: idea.value,
        maxSources: maxSources.value,
        result: result.value,
      })
      if (value.length <= 1_000_000) sessionStorage.setItem(draftKey(), value)
    } catch {
      /* The explicit save action remains available if window storage is full. */
    }
  },
  { deep: true, flush: 'sync' }
)
watch(activeConversationId, () => {
  ++requestEpoch
  stopPolling()
  loading.value = false
  submitting.value = false
  job.value = null
  jobs.value = []
  selectedRun.value = ''
  retryUnknown.value = false
  pendingCreate = null
  pendingResume = null
  restoreDraft()
  if (route.query.run) {
    const query = { ...route.query }
    delete query.run
    void router.replace({ query })
  } else void loadHistory()
})
onBeforeUnmount(() => {
  ++requestEpoch
  stopPolling()
})
function stopPolling() {
  ++observationEpoch
  if (pollTimer) clearTimeout(pollTimer)
  readRequest?.abort()
}
function accept(value: ResearchRun, restoreInput = false) {
  job.value = value
  selectedRun.value = value.run_id
  result.value = value.result
  loading.value = ['queued', 'running'].includes(value.status)
  if (restoreInput) {
    idea.value = value.inputs.idea
    maxSources.value = value.inputs.max_sources
  }
}
async function readRun(id: string, restoreInput = false) {
  const token = requestEpoch,
    cid = activeConversationId.value
  stopPolling()
  const observation = observationEpoch
  readRequest = new AbortController()
  try {
    const value = await getFrameworkRun(id, cid, readRequest.signal)
    if (token !== requestEpoch || observation !== observationEpoch) return
    error.value = ''
    accept(value, restoreInput || ['queued', 'running'].includes(value.status))
    if (loading.value && !submitting.value) pollTimer = setTimeout(() => void readRun(id), 1200)
  } catch (err) {
    const cancelled =
      !!err && typeof err === 'object' && 'code' in err && err.code === 'ERR_CANCELED'
    if (token === requestEpoch && observation === observationEpoch && !cancelled)
      error.value = sourceError(err)
  }
}
async function loadHistory() {
  const token = requestEpoch,
    cid = activeConversationId.value
  try {
    await ensureSourceConversation(cid)
    if (token !== requestEpoch || submitting.value) return
    const history = await listFrameworkRuns(cid)
    if (token !== requestEpoch || submitting.value) return
    jobs.value = history.items
    const requested = typeof route.query.run === 'string' ? route.query.run : ''
    const id =
      requested ||
      (history.items.some((item) => item.run_id === selectedRun.value)
        ? selectedRun.value
        : history.items[0]?.run_id)
    if (id) await readRun(id, Boolean(requested) || !idea.value.trim())
  } catch (err) {
    if (token === requestEpoch) error.value = sourceError(err)
  }
}
function refreshObservation() {
  error.value = ''
  if (job.value) void readRun(job.value.run_id)
  else void loadHistory()
}
async function mutate(action: () => Promise<ResearchRun>, completed?: () => void) {
  if (submitting.value) return
  const token = requestEpoch
  submitting.value = true
  error.value = ''
  stopPolling()
  try {
    const value = await action()
    if (token !== requestEpoch) return
    completed?.()
    retryUnknown.value = false
    accept(value)
    const history = await listFrameworkRuns(value.conversation_id)
    if (token !== requestEpoch) return
    jobs.value = history.items
  } catch (err) {
    if (token === requestEpoch) error.value = sourceError(err)
  } finally {
    if (token === requestEpoch) {
      submitting.value = false
      if (job.value) void readRun(job.value.run_id)
    }
  }
}
function generate() {
  if (!idea.value.trim() || loading.value || submitting.value) return
  const body = {
    idea: idea.value.trim(),
    max_sources: maxSources.value,
    conversation_id: activeConversationId.value,
  }
  if (
    !pendingCreate ||
    pendingCreate.idea !== body.idea ||
    pendingCreate.max_sources !== body.max_sources ||
    pendingCreate.conversation_id !== body.conversation_id
  )
    pendingCreate = {
      ...body,
      run_id: `research_${crypto.randomUUID().replaceAll('-', '')}`,
      request_id: crypto.randomUUID(),
    }
  const request = pendingCreate
  void mutate(
    async () => {
      await ensureSourceConversation(request.conversation_id)
      return createFrameworkRun(request)
    },
    () => {
      pendingCreate = null
    }
  )
}
function resume() {
  if (!job.value || (job.value.requires_unknown_confirmation && !retryUnknown.value)) return
  const value = job.value,
    cid = activeConversationId.value
  if (
    !pendingResume ||
    pendingResume.runId !== value.run_id ||
    pendingResume.revision !== value.revision ||
    pendingResume.retry !== retryUnknown.value
  )
    pendingResume = {
      runId: value.run_id,
      revision: value.revision,
      requestId: crypto.randomUUID(),
      retry: retryUnknown.value,
    }
  const request = pendingResume
  void mutate(
    () =>
      resumeFrameworkRun(request.runId, cid, request.revision, request.requestId, request.retry),
    () => {
      pendingResume = null
    }
  )
}
function cancel() {
  if (job.value) {
    const id = job.value.run_id,
      cid = activeConversationId.value
    void mutate(() => cancelFrameworkRun(id, cid))
  }
}
function selectSavedRun() {
  requestEpoch++
  stopPolling()
  pendingResume = null
  retryUnknown.value = false
  error.value = ''
  void readRun(selectedRun.value, true)
}
watch(
  () => route.query.run,
  () => {
    requestEpoch++
    stopPolling()
    submitting.value = false
    pendingCreate = null
    pendingResume = null
    retryUnknown.value = false
    void loadHistory()
  }
)
onMounted(() => void loadHistory())

function focusRef(index: number) {
  activeRef.value = index
  const el = document.getElementById(`ref-${index}`)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
</script>

<template>
  <div class="page writing-page framework-page">
    <div class="page-head">
      <div>
        <div class="page-title">论文框架</div>
        <div class="page-desc">从研究想法开始，组织章节、论点与参考文献。引用来自当前文献库。</div>
      </div>
    </div>

    <div class="writing-workspace">
      <div class="card input-card">
        <div class="editor-heading">研究设定<span>可溯源写作</span></div>
        <div class="editor-body">
          <label for="framework-idea" class="field-label">研究想法</label>
          <textarea
            id="framework-idea"
            v-model="idea"
            :disabled="loading || submitting"
            class="idea-input"
            rows="7"
            placeholder="描述你的研究想法，例如：用多智能体规划提升学术文献检索的可解释性…"
          ></textarea>
          <div class="input-row">
            <label class="source-label">
              参考文献上限
              <select
                v-model.number="maxSources"
                class="source-select"
                :disabled="loading || submitting"
              >
                <option v-for="count in 20" :key="count" :value="count">{{ count }}</option>
              </select>
            </label>
            <button
              class="el-button el-button--primary"
              :disabled="loading || submitting || !idea.trim()"
              @click="generate"
            >
              {{ loading || submitting ? '任务进行中…' : '生成框架' }}
            </button>
          </div>
          <p class="input-hint">
            使用当前会话模型，固定本次研究想法与文献版本。离开页面不会停止；结果保存后可重新打开。
          </p>
          <label v-if="jobs.length" class="field-label"
            >已保存的框架任务
            <select v-model="selectedRun" :disabled="submitting" @change="selectSavedRun">
              <option v-for="saved in jobs" :key="saved.run_id" :value="saved.run_id">
                {{ saved.title }} · {{ new Date(saved.updated_at).toLocaleString() }}
              </option>
            </select>
          </label>
          <div v-if="job" class="framework-run-state" aria-live="polite">
            <strong>{{ runHeadline }}</strong>
            <p>
              模型：{{ job.model.profile_name }} · {{ job.model.model_id }} · 第
              {{ job.attempt_number }} 次执行
            </p>
            <p v-if="job.source_count">
              已固定 {{ job.source_count }} 篇候选文献，引用位置随结果保留。
            </p>
            <ol>
              <li v-for="(event, index) in job.events.slice(-4)" :key="`${index}:${event.at}`">
                {{ event.text }}
              </li>
            </ol>
            <p v-if="job.error" class="err">{{ job.error }}</p>
            <p v-if="job.resource_pause" class="err">{{ job.resource_pause.message }}</p>
            <template v-if="job.can_resume">
              <label v-if="job.requires_unknown_confirmation" class="unknown-retry"
                ><input
                  v-model="retryUnknown"
                  type="checkbox"
                />我已核对上次调用，确认重试未确认调用可能再次产生费用</label
              >
              <button
                class="el-button"
                :disabled="submitting || (job.requires_unknown_confirmation && !retryUnknown)"
                @click="resume"
              >
                {{ job.requires_unknown_confirmation ? '重试未确认调用' : '继续未完成任务' }}
              </button>
            </template>
            <button v-if="loading" class="el-button" :disabled="submitting" @click="cancel">
              停止此任务
            </button>
            <p class="input-hint">
              记录 {{ job.usage.call_count }} 次供应商调用<span
                v-if="job.usage.actual_usage.total_tokens != null"
                >，回执 {{ job.usage.actual_usage.total_tokens }} tokens</span
              >；缺少回执或价格时不按零费用计算。
            </p>
          </div>
          <p v-if="error" class="err" role="alert">{{ error }}</p>
          <button v-if="error" class="el-button" :disabled="submitting" @click="refreshObservation">
            重新载入任务状态
          </button>
        </div>
      </div>

      <div v-if="result" class="result">
        <SaveArtifactButton
          :title="result.idea || idea"
          :content="frameworkMarkdown"
          :evidence="frameworkEvidence"
          :conversation-id="result.conversation_id"
        />
        <div class="badge-row">
          <span class="mode-badge" :class="result.mode">
            {{ result.mode === 'llm' ? '模型生成' : '离线骨架' }}
          </span>
          <span class="warn-badge">{{
            result.structure_valid ? '结构已检查 · 论点支持待核验' : '结构未通过 · 请检查'
          }}</span>
        </div>
        <p v-for="(w, i) in result.warnings" :key="i" class="warning-text">{{ w }}</p>

        <!-- 框架章节 -->
        <div v-for="section in result.sections" :key="section.key" class="card section-card">
          <div class="section-title">{{ section.title }}</div>
          <ul v-if="section.bullets.length" class="bullet-list">
            <li v-for="(bullet, i) in section.bullets" :key="i" class="bullet">
              <span>{{ bullet.text }}</span>
              <span class="citation-group">
                <button
                  v-for="cite in bullet.citations"
                  :key="cite"
                  class="cite-chip"
                  :class="{ active: activeRef === cite }"
                  @click="focusRef(cite)"
                >
                  [{{ cite }}]
                </button>
              </span>
              <RouterLink
                v-for="id in bullet.evidence_ids || []"
                :key="id"
                class="cite-chip"
                :to="
                  sourceReaderLocation(
                    evidenceById.get(id)!.revision_id,
                    result.conversation_id!,
                    evidenceById.get(id)!.locator_id
                  )
                "
              >
                原文 {{ id }}
              </RouterLink>
              <span
                v-if="bullet.citations.length && !bullet.evidence_ids?.length"
                class="evidence-state"
                >未定位原文</span
              >
              <p v-if="bullet.support_status" class="evidence-state">
                {{ supportLabels[bullet.support_status] || '论点待核验'
                }}<span v-if="bullet.support_review">
                  · {{ bullet.support_review.explanation }}</span
                >
              </p>
            </li>
          </ul>
          <div v-else class="empty-bullet">本部分暂无要点</div>
        </div>

        <!-- 参考文献 -->
        <div class="card ref-card">
          <div class="section-title">参考文献与固定原文</div>
          <div v-if="result.references.length === 0" class="empty-bullet">
            本地文献库暂无已解析论文，请先在文献库上传并解析论文。
          </div>
          <ol class="ref-list">
            <li
              v-for="ref in result.references"
              :id="`ref-${ref.ref_index}`"
              :key="ref.ref_index"
              class="ref-item"
              :class="{ active: activeRef === ref.ref_index }"
            >
              <span class="ref-index">[{{ ref.ref_index }}]</span>
              <div class="ref-content">
                <span class="ref-title">{{ ref.title }}</span>
                <span class="ref-authors">{{ ref.authors }}</span>
                <span class="ref-meta">
                  <span v-if="ref.published_at">{{ ref.published_at }}</span>
                  <a v-if="ref.url" :href="ref.url" target="_blank" rel="noopener" class="ref-link"
                    >arXiv 原文</a
                  >
                  <span class="relevance">相关度 {{ ref.relevance.toFixed(2) }}</span>
                </span>
                <div v-for="item in ref.evidence || []" :key="item.id" class="source-excerpt">
                  <RouterLink
                    :to="
                      sourceReaderLocation(
                        item.revision_id,
                        result.conversation_id!,
                        item.locator_id
                      )
                    "
                    >查看原文 {{ item.id }}</RouterLink
                  >
                  <p>{{ item.quote }}{{ item.excerpt_truncated ? '…' : '' }}</p>
                </div>
              </div>
            </li>
          </ol>
        </div>
      </div>
      <div v-else class="outline-empty">
        <div class="empty-title">从一个研究问题开始</div>
        <p>描述你的研究想法，生成后将在这里展示章节要点、引用编号与文献来源。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.writing-workspace {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 28px;
  align-items: start;
}
.input-card {
  padding: 0;
  overflow: hidden;
  border-radius: 8px;
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
  font-size: 11px;
  font-weight: 400;
  color: var(--fn-text-secondary);
}
.editor-body {
  padding: 20px;
}
.field-label {
  display: block;
  margin-bottom: 10px;
  font-size: 12px;
  font-weight: 500;
}
.idea-input {
  width: 100%;
  border: 1px solid var(--fn-border-input);
  border-radius: 6px;
  padding: 10px 11px;
  font: inherit;
  font-size: 13px;
  line-height: 1.85;
  resize: vertical;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.input-row {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 20px;
  margin-top: 22px;
}
.source-label {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 10px;
  font-size: 12px;
  font-weight: 500;
  color: var(--fn-text-primary);
}
.source-select {
  padding: 8px 9px;
  border: 1px solid var(--fn-border-input);
  border-radius: 6px;
  font-size: 12px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.input-hint {
  margin: 14px 0 0;
  color: var(--fn-text-tertiary);
  font-size: 11px;
  line-height: 1.8;
}
.result {
  padding-top: 8px;
  min-width: 0;
}
.badge-row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 18px;
}
.mode-badge,
.warn-badge {
  font-size: 10px;
  padding: 3px 6px;
  border-radius: 4px;
}
.mode-badge.llm {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.mode-badge.offline {
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
}
.warn-badge {
  color: var(--fn-warning-text);
  background: var(--fn-warning-bg);
}
.warning-text {
  font-size: 11px;
  line-height: 1.8;
  color: var(--fn-warning-text);
  margin: 0 0 8px;
}
.section-card {
  padding: 19px 0 13px;
  margin: 0;
  border: 0;
  border-top: 1px solid var(--fn-border-primary);
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.section-title {
  font-size: 14px;
  line-height: 1.7;
  font-weight: 600;
  margin-bottom: 11px;
  color: var(--fn-text-primary);
}
.bullet-list {
  padding: 0 0 0 18px;
  margin: 0;
}
.bullet {
  font-size: 12px;
  line-height: 1.9;
  color: var(--fn-text-secondary);
}
.bullet + .bullet {
  margin-top: 5px;
}
.citation-group {
  display: inline-flex;
  gap: 4px;
  margin-left: 5px;
}
.cite-chip {
  padding: 1px 4px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
  font-size: 10px;
  cursor: pointer;
}
.cite-chip.active {
  color: #fff;
  background: var(--fn-brand);
}
.empty-bullet {
  color: var(--fn-text-tertiary);
  font-size: 12px;
}
.evidence-state {
  margin-left: 8px;
  font-size: 11px;
  color: var(--fn-warning-text);
}
.source-excerpt {
  border-left: 2px solid var(--fn-border-primary);
  padding-left: 10px;
  margin-top: 8px;
}
.source-excerpt p {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 5px 0;
}
.ref-card {
  margin-top: 21px;
  padding: 20px 0 0;
  border: 0;
  border-top: 1px solid var(--fn-border-primary);
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.ref-card .section-title {
  font-size: 11px;
  font-weight: 500;
  color: var(--fn-text-secondary);
}
.ref-list {
  padding: 0;
  margin: 0;
  list-style: none;
}
.ref-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 9px 8px;
  margin: 0 -8px;
  border: 1px solid transparent;
  border-radius: 4px;
}
.ref-item.active {
  border-color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
}
.ref-index {
  font-size: 10px;
  color: var(--fn-text-tertiary);
  line-height: 1.9;
  flex-shrink: 0;
}
.ref-content {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  font-size: 11px;
  line-height: 1.75;
}
.ref-title {
  color: var(--fn-text-primary);
  font-weight: 500;
}
.ref-authors,
.ref-meta {
  color: var(--fn-text-secondary);
}
.ref-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.ref-link {
  color: var(--fn-brand);
  text-decoration: none;
}
.outline-empty {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 380px;
  padding: 35px;
  text-align: center;
  border-top: 1px solid var(--fn-border-primary);
  border-bottom: 1px solid var(--fn-border-primary);
}
.empty-title {
  font-size: 17px;
  font-weight: 500;
  color: var(--fn-text-primary);
}
.outline-empty p {
  font-size: 12px;
  line-height: 1.9;
  color: var(--fn-text-secondary);
  max-width: 350px;
  margin: 12px auto 0;
}
.err {
  color: var(--fn-danger);
  font-size: 12px;
  line-height: 1.8;
  margin-top: 12px;
}
.framework-run-state {
  padding-top: 14px;
  margin-top: 14px;
  border-top: 1px solid var(--fn-border-primary);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.framework-run-state p {
  margin: 7px 0;
}
.framework-run-state ol {
  padding-left: 18px;
  margin: 10px 0;
  color: var(--fn-text-secondary);
}
.framework-run-state .el-button {
  margin: 8px 8px 0 0;
}
.unknown-retry {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin: 10px 0;
}
.field-label select {
  display: block;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  margin: 7px 0;
  padding: 7px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  font: inherit;
}
@media (max-width: 1200px) {
  .writing-workspace {
    grid-template-columns: 280px minmax(0, 1fr);
    gap: 24px;
  }
  .editor-body {
    padding: 17px;
  }
}
@media (max-width: 900px) {
  .writing-workspace {
    grid-template-columns: 1fr;
  }
  .outline-empty {
    min-height: 220px;
  }
}
</style>
