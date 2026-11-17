<script setup lang="ts">
import axios from 'axios'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  changeTaskRequirements,
  getTaskRequirements,
  type RequirementChange,
  type RequirementSnapshot,
  type ResultContract,
  type TaskRequirement,
} from '../taskRequirementsApi'
import { sourceError } from '../sourceApi'
import { savedArtifactExportDownloadUrl } from '../artifactApi'

const props = defineProps<{ goalId: string; conversationId: string; readOnly?: boolean }>()
const opened = ref(false),
  busy = ref(false),
  error = ref(''),
  snapshot = ref<RequirementSnapshot | null>(null)
const formOpen = ref(false),
  editing = ref<string | null>(null),
  candidate = ref<string | null>(null)
const title = ref(''),
  kind = ref<ResultContract['kind']>('manual'),
  contains = ref(''),
  excludes = ref(''),
  command = ref(''),
  path = ref(''),
  sourceCount = ref(0),
  rule = ref('no_file_write')
const exportFormat = ref<NonNullable<ResultContract['format']>>('docx'),
  artifactId = ref(''), revisionId = ref(''), recordedExecution = ref(false)
const decision = ref<{ id: string; action: 'confirm' | 'blocked' } | null>(null),
  reason = ref('')
let epoch = 0,
  pending: { signature: string; body: RequirementChange } | null = null
const mutationLocked = computed(() => busy.value || props.readOnly)
const visible = computed(
  () =>
    snapshot.value?.requirements.filter(
      (r) => r.origin !== 'model_plan' && !['cancelled', 'superseded'].includes(r.status)
    ) || []
)
const candidates = computed(
  () => snapshot.value?.input_coverage.candidates.filter((c) => !c.quoted_context) || []
)
const retired = computed(
  () =>
    snapshot.value?.requirements.filter((r) => ['cancelled', 'superseded'].includes(r.status)) || []
)
const history = computed(
  () =>
    snapshot.value?.revisions
      .filter((r) => r.source === 'user')
      .slice()
      .reverse() || []
)
const actions: Record<string, string> = {
  create: '建立任务',
  add: '补充要求',
  replace: '修改要求',
  cancel: '取消要求或解除限制',
  confirm: '人工确认',
  blocked: '记录受阻',
  reopen: '重新核对',
  user_input: '补充任务输入',
}
const status: Record<string, string> = {
  open: '待核对',
  completed: '已满足',
  blocked: '受阻',
  active_constraint: '持续限制',
  cancelled: '已取消',
  superseded: '已替换',
}
const labels: Record<string, string> = {
  manual: '人工核对',
  answer_terms: '回答必含/禁含词条',
  command_result: '指定命令与完整输出',
  artifact_result: '已保存成果与来源',
  artifact_export_result: '固定导出文件',
  policy: '操作限制',
}
function method(row: TaskRequirement) {
  const explicit = row.tool_arguments?.result_contract
  if (explicit) return labels[explicit.kind]
  if (row.tool_arguments?.contract) return '原话中的数量与结构条件'
  return '执行回执或人工核对'
}
function exportProof(row: TaskRequirement) {
  const proof = row.evidence.slice().reverse().find(e => e.kind === 'artifact_export_acceptance')
  return row.status === 'completed' && proof?.passed && proof.export_id && proof.artifact_id ? proof : null
}
const rules: Record<string, string> = {
  no_file_read: '不读取文件',
  no_file_write: '不修改用户文件',
  no_commands: '不启动命令或脚本',
  no_external_tools: '仅使用内置本地工具',
}
watch(
  () => [props.goalId, props.conversationId],
  () => {
    ++epoch
    snapshot.value = null
    busy.value = false
    error.value = ''
    pending = null
    formOpen.value = false
    decision.value = null
    if (opened.value) void load()
  }
)
watch(
  () => props.readOnly,
  (value) => {
    if (value) {
      formOpen.value = false
      decision.value = null
      pending = null
    }
  }
)
async function load() {
  const token = ++epoch,
    goal = props.goalId,
    cid = props.conversationId
  busy.value = true
  try {
    const value = await getTaskRequirements(goal, cid)
    if (token === epoch) {
      snapshot.value = value
      error.value = ''
    }
  } catch (err) {
    if (token === epoch) error.value = sourceError(err)
  } finally {
    if (token === epoch) busy.value = false
  }
}
function toggle(event: Event) {
  opened.value = (event.target as HTMLDetailsElement).open
  if (opened.value && !snapshot.value && !busy.value) void load()
}
function edit(row?: TaskRequirement, source?: { candidate_id: string; quote: string }) {
  if (props.readOnly) return
  editing.value = row?.id || null
  candidate.value = source?.candidate_id || null
  const spec = row?.tool_arguments?.result_contract || { kind: 'manual' }
  title.value = row?.title || source?.quote || ''
  kind.value = spec.kind
  contains.value = (spec.contains || []).join('\n')
  excludes.value = (spec.excludes || []).join('\n')
  command.value = spec.command || ''
  path.value = spec.path || ''
  sourceCount.value = spec.min_fixed_sources || 0
  rule.value = spec.rule || 'no_file_write'
  exportFormat.value = spec.format || 'docx'
  artifactId.value = spec.artifact_id || ''
  revisionId.value = spec.revision_id || ''
  recordedExecution.value = spec.requires_recorded_execution || false
  formOpen.value = true
  decision.value = null
  error.value = ''
}
const terms = (text: string) =>
  text
    .split('\n')
    .map((t) => t.trim())
    .filter(Boolean)
function chooseDecision(row: TaskRequirement, action: 'confirm' | 'blocked') {
  decision.value = { id: row.id, action }
  reason.value = ''
}
function specification(): ResultContract {
  if (kind.value === 'manual') return { kind: 'manual' }
  if (kind.value === 'policy') return { kind: 'policy', rule: rule.value }
  const spec: ResultContract = {
    kind: kind.value,
    contains: terms(contains.value),
    excludes: terms(excludes.value),
  }
  if (kind.value === 'command_result') {
    spec.command = command.value
    spec.path = path.value
  }
  if (kind.value === 'artifact_result' || kind.value === 'artifact_export_result') spec.min_fixed_sources = sourceCount.value
  if (kind.value === 'artifact_export_result') {
    spec.format = exportFormat.value
    if (artifactId.value.trim()) spec.artifact_id = artifactId.value.trim()
    if (revisionId.value.trim()) spec.revision_id = revisionId.value.trim()
    spec.requires_recorded_execution = exportFormat.value !== 'docx' && recordedExecution.value
  }
  return spec
}
async function change(
  values: Omit<RequirementChange, 'conversation_id' | 'expected_revision' | 'request_id'>
) {
  if (!snapshot.value || busy.value || props.readOnly) return
  const signature = JSON.stringify(values)
  if (!pending || pending.signature !== signature)
    pending = {
      signature,
      body: {
        ...values,
        conversation_id: props.conversationId,
        expected_revision: snapshot.value.revision,
        request_id: crypto.randomUUID(),
      },
    }
  const body = pending.body,
    token = ++epoch,
    goal = props.goalId
  busy.value = true
  error.value = ''
  try {
    const value = await changeTaskRequirements(goal, body)
    if (token !== epoch) return
    snapshot.value = value
    pending = null
    formOpen.value = false
    decision.value = null
  } catch (err) {
    if (token !== epoch) return
    error.value = sourceError(err)
    if (axios.isAxiosError(err) && err.response?.status === 409) pending = null
  } finally {
    if (token === epoch) busy.value = false
  }
}
function save() {
  void change({
    action: editing.value ? 'replace' : 'add',
    title: title.value,
    contract: specification(),
    ...(editing.value ? { requirement_id: editing.value } : {}),
    ...(candidate.value ? { candidate_id: candidate.value } : {}),
  })
}
onBeforeUnmount(() => {
  ++epoch
})
</script>

<template>
  <details class="task-requirements" @toggle="toggle">
    <summary>
      任务要求与验收<span v-if="snapshot"> · {{ visible.length }} 项</span>
    </summary>
    <section v-if="opened" aria-label="任务要求与验收">
      <p>
        原话、任务要求和执行计划分别保留。修改单项要求会保存历史；不会自动重新执行任务。来源留存、命令执行和科学结论核验分别记录。
      </p>
      <p v-if="readOnly">本会话已归档，任务要求仅供查阅；恢复会话后可以修改。</p>
      <div class="requirements-actions">
        <button type="button" :disabled="busy" @click="load">刷新要求</button
        ><button type="button" :disabled="mutationLocked || !snapshot" @click="edit()">
          添加要求或限制</button
        ><small v-if="snapshot">修订 {{ snapshot.revision }}</small>
      </div>
      <p v-if="error" role="alert" class="requirements-error">{{ error }}</p>
      <ul>
        <li v-for="row in visible" :key="row.id" :data-requirement-id="row.id">
          <div class="requirement-heading">
            <strong>{{ row.title }}</strong
            ><span>{{ status[row.status] || row.status }}</span>
          </div>
          <p>
            {{ method(row)
            }}<span v-if="row.evidence.some((e) => e.semantic_verification === 'not_assessed')">
              · 结构检查不证明语义正确</span
            ><span v-if="row.evidence.at(-1)?.kind === 'user_confirmation'"> · 用户已核对</span>
          </p>
          <p v-if="exportProof(row)">
            <a :href="savedArtifactExportDownloadUrl(exportProof(row)!.artifact_id!, exportProof(row)!.export_id!, conversationId)" download>下载已验收文件</a>
            · {{ exportProof(row)!.format }} · {{ exportProof(row)!.payload_size }} 字节
            <br />SHA256：{{ exportProof(row)!.payload_sha256 }}
            <br />结构检查不证明语义或原生软件显示正确。
          </p>
          <div class="requirements-actions">
            <button type="button" :disabled="mutationLocked" @click="edit(row)">修改此要求</button>
            <button
              v-if="
                row.tool_arguments?.result_contract?.kind !== 'policy' && row.status !== 'completed'
              "
              type="button"
              :disabled="mutationLocked"
              @click="chooseDecision(row, 'confirm')"
            >
              核对完成
            </button>
            <button
              v-if="
                row.tool_arguments?.result_contract?.kind !== 'policy' && row.status !== 'blocked'
              "
              type="button"
              :disabled="mutationLocked"
              @click="chooseDecision(row, 'blocked')"
            >
              记录受阻
            </button>
            <button
              type="button"
              :disabled="mutationLocked"
              @click="
                change({
                  action: 'cancel',
                  requirement_id: row.id,
                  reason: '用户在任务要求中明确取消此项',
                })
              "
            >
              {{
                row.tool_arguments?.result_contract?.kind === 'policy' ? '解除此限制' : '取消此要求'
              }}
            </button>
          </div>
        </li>
      </ul>
      <form
        v-if="decision"
        class="requirement-form"
        @submit.prevent="change({ action: decision.action, requirement_id: decision.id, reason })"
      >
        <label
          >核对依据或受阻原因<textarea v-model="reason" required maxlength="4000"></textarea>
        </label>
        <div class="requirements-actions">
          <button type="submit" :disabled="mutationLocked">
            {{ decision.action === 'confirm' ? '确认此要求已满足' : '保存受阻原因' }}</button
          ><button type="button" @click="decision = null">返回</button>
        </div>
      </form>
      <form v-if="formOpen" class="requirement-form" @submit.prevent="save">
        <label>要求描述<textarea v-model="title" required maxlength="4000"></textarea></label>
        <label
          >验收方式<select v-model="kind" aria-label="验收方式">
            <option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option>
          </select></label
        >
        <template v-if="['answer_terms', 'command_result', 'artifact_result', 'artifact_export_result'].includes(kind)">
          <label
            >必须出现的词条（每行一条，逐字匹配）<textarea v-model="contains"></textarea>
          </label>
          <label
            >不得出现的词条（每行一条，逐字匹配）<textarea v-model="excludes"></textarea>
          </label>
          <p>词条和数量检查只证明所列结构条件，不能代替事实、翻译质量或科学支持核验。</p>
        </template>
        <template v-if="kind === 'command_result'"
          ><label>待验收的完整命令<textarea v-model="command" required></textarea></label
          ><label>项目内启动目录<input v-model="path" placeholder="留空为项目根目录" /></label>
          <p>
            只接受本任务的实际命令记录和完整输出。退出码为0不能证明文件或研究结论符合要求；成果需另设验收项。
          </p></template
        >
        <label v-if="kind === 'artifact_result' || kind === 'artifact_export_result'"
          >至少保留的固定来源位置数量<input
            v-model.number="sourceCount"
            type="number"
            min="0"
            max="500"
        /></label>
        <template v-if="kind === 'artifact_export_result'">
          <label>文件格式<select v-model="exportFormat" aria-label="固定导出格式"><option value="docx">Word（DOCX）</option><option value="xlsx">分析工作簿（XLSX）</option><option value="svg">矢量图（SVG）</option><option value="bundle">复算包（ZIP）</option></select></label>
          <label>指定成果编号（可选）<input v-model="artifactId" pattern="[a-f0-9]{32}" /></label>
          <label>指定版本编号（可选，须同时指定成果）<input v-model="revisionId" pattern="[a-f0-9]{32}" /></label>
          <label v-if="exportFormat !== 'docx'"><input v-model="recordedExecution" type="checkbox" />要求已有真实成功复算记录</label>
          <p>只接受本任务实际导出的固定文件。词条检查针对固定正文；文件可解析不代表原生软件显示、内容语义或研究结论正确。</p>
        </template>
        <template v-if="kind === 'policy'"
          ><label
            >限制范围<select v-model="rule" aria-label="限制范围">
              <option v-for="(label, key) in rules" :key="key" :value="key">{{ label }}</option>
            </select></label
          >
          <p>
            限制在后续工具调用前核对，已执行操作不能撤回。内置本地工具模式仍会连接当前模型；禁止修改用户文件时，应用仍保存任务、缓存和回执。原话中的既有限制需用补充消息明确解除。
          </p></template
        >
        <div class="requirements-actions">
          <button type="submit" :disabled="mutationLocked">
            {{ editing ? '保存此要求的新版本' : '纳入任务要求' }}</button
          ><button type="button" @click="formOpen = false">返回</button>
        </div>
      </form>
      <details v-if="candidates.length">
        <summary>原话候选（待核对含义）</summary>
        <ul>
          <li v-for="item in candidates" :key="item.candidate_id">
            <p>{{ item.quote }}</p>
            <button type="button" :disabled="mutationLocked" @click="edit(undefined, item)">
              从此原话补充要求
            </button>
          </li>
        </ul>
      </details>
      <p v-if="snapshot && !visible.length">尚无独立验收项。普通问答可以直接完成，无需创建计划。</p>
      <details v-if="history.length || retired.length" class="requirements-history">
        <summary>修改历史</summary>
        <ul>
          <li v-for="row in retired" :key="row.id">
            <strong>{{ row.title }}</strong> · {{ status[row.status] || row.status }}
          </li>
          <li v-for="revision in history" :key="revision.revision">
            修订 {{ revision.revision }} ·
            {{ actions[revision.changes.request?.action || revision.action] || '用户更新' }}
            <p v-if="revision.changes.request?.title">{{ revision.changes.request.title }}</p>
            <p v-if="revision.changes.request?.reason || revision.reason">
              {{ revision.changes.request?.reason || revision.reason }}
            </p>
          </li>
        </ul>
      </details>
    </section>
  </details>
</template>

<style scoped>
.task-requirements {
  margin: 12px 0;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
  color: var(--fn-text-primary);
  overflow-wrap: anywhere;
}
summary {
  cursor: pointer;
  line-height: 1.8;
}
p {
  color: var(--fn-text-secondary);
  line-height: 1.8;
}
ul {
  list-style: none;
  padding: 0;
}
li {
  padding: 12px 0;
  border-bottom: 1px solid var(--fn-border-primary);
}
.requirement-heading,
.requirements-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
.requirement-heading {
  justify-content: space-between;
}
.requirement-heading strong {
  flex: 1;
  min-width: 0;
}
.requirement-form {
  padding: 12px 0;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 10px 0;
}
button,
input,
textarea,
select {
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  padding: 7px 10px;
  font: inherit;
  max-width: 100%;
  box-sizing: border-box;
}
textarea {
  width: 100%;
  min-height: 70px;
  resize: vertical;
}
button {
  cursor: pointer;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.requirements-error {
  color: var(--fn-danger);
}
button:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 2px;
}
</style>
