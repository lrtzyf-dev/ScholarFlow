<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import MarkdownAnswer from '../components/MarkdownAnswer.vue'
import FileChangeReview from '../components/FileChangeReview.vue'
import { getChatProject } from '../projectApi'
import { editableFile } from '../fileChangeApi'
import { activeConversationId } from '../desktopState'
import { getConversationSummary } from '../api'
import { artifactChanges, artifactChangeDetail, artifactComputations, artifactHistory,
  artifactRequestId, createArtifact, decideArtifactChange, getArtifact, listArtifacts, proposeArtifactChange,
  recomputeArtifact, proposeArtifactPublication, createArtifactExport, listArtifactExports, savedArtifactExportDownloadUrl,
  type Artifact, type ArtifactChange, type ArtifactRevision, type ArtifactExport, type ArtifactExportFormat } from '../artifactApi'

const route = useRoute(), router = useRouter()
const cid = computed(() => typeof route.query.conv === 'string' ? route.query.conv : null)
const selectedId = computed(() => String(route.params.id || ''))
const items = ref<Artifact[]>([]), nextOffset = ref<number | null>(null)
const selected = ref<Artifact | null>(null), versions = ref<ArtifactRevision[]>([])
const nextVersion = ref<number | null>(null), changes = ref<ArtifactChange[]>([])
const selectedChange = ref<ArtifactChange | null>(null), computations = ref<Record<string, any>[]>([])
const content = ref(''), sectionIndex = ref(-1), title = ref(''), error = ref(''), notice = ref('')
const busy = ref(false), draftStorageError = ref(false), viewingVersion = ref('')
const publicationPath = ref(''), publicationChangeId = ref<string | null>(null)
const exports = ref<ArtifactExport[]>([]), nextExport = ref<number | null>(null), archived = ref(false)
const exportRequests = new Map<string, string>()
let publicationRequest = { signature: '', id: artifactRequestId() }
let epoch = 0
const currentRevision = computed(() => selected.value?.revision)
const historical = computed(() => !!currentRevision.value && currentRevision.value.id !== selected.value?.current_revision_id)
const draftKey = computed(() => `scholarflow:artifact-draft:${cid.value || 'local'}:${selectedId.value}`)
const draftRequestId = ref(artifactRequestId()), draftBase = ref('')
type LocalDraft = { content: string; section: number; base: string; request: string }
const draftSnapshot = (): LocalDraft => ({ content: content.value, section: sectionIndex.value,
  base: draftBase.value, request: draftRequestId.value })
let draftSignature = ''
const selectedSection = computed(() => selected.value?.sections?.find((s) => s.index === sectionIndex.value))
function message(err: any) {
  const detail = err?.response?.data?.detail
  return typeof detail === 'string' ? detail : detail?.message || err?.message || '操作失败，草稿已保留'
}
function clearDraft() {
  try { localStorage.removeItem(draftKey.value) } catch { draftStorageError.value = true }
}
watch([content, sectionIndex], () => {
  if (!selected.value || historical.value || !draftBase.value) return
  const signature = JSON.stringify([content.value, sectionIndex.value])
  if (signature !== draftSignature) { draftRequestId.value = artifactRequestId(); draftSignature = signature }
  try {
    localStorage.setItem(draftKey.value, JSON.stringify({ content: content.value, section: sectionIndex.value,
      base: draftBase.value, request: draftRequestId.value }))
  } catch { draftStorageError.value = true }
})
async function load(retainedDraft?: LocalDraft) {
  const token = ++epoch, id = selectedId.value, conversation = cid.value
  error.value = ''; notice.value = ''; selected.value = null; selectedChange.value = null
  changes.value = []; versions.value = []; computations.value = []; draftBase.value = ''
  exports.value = []; nextExport.value = null; archived.value = false
  busy.value = true
  try {
    if (!id) {
      const [page, scope] = await Promise.all([listArtifacts(conversation), conversation ? getConversationSummary(conversation) : Promise.resolve(null)])
      if (token !== epoch) return
      items.value = page.items; nextOffset.value = page.next_offset
      archived.value = !!scope?.archived_at
    } else {
      const item = await getArtifact(id, conversation, viewingVersion.value || undefined)
      if (token !== epoch) return
      const [history, pending, runs, savedExports, scope] = await Promise.all([
        artifactHistory(id, conversation), artifactChanges(id, conversation), artifactComputations(id, conversation, item.revision!.id),
        listArtifactExports(id, conversation, item.revision!.id), conversation ? getConversationSummary(conversation) : Promise.resolve(null)])
      if (token !== epoch) return
      selected.value = item; versions.value = history.items; changes.value = pending.items; computations.value = runs.items
      exports.value = savedExports.items.filter(e => e.revision_id === item.revision!.id)
      nextExport.value = savedExports.next_offset
      archived.value = !!scope?.archived_at
      nextVersion.value = history.next_before_version
      sectionIndex.value = -1; content.value = item.revision!.content; draftBase.value = item.revision!.id
      draftRequestId.value = artifactRequestId()
      if (!historical.value) {
        try {
          const stored = retainedDraft || JSON.parse(localStorage.getItem(draftKey.value) || 'null')
          if (stored && typeof stored.content === 'string' && typeof stored.base === 'string') {
            content.value = stored.content; sectionIndex.value = Number.isInteger(stored.section) ? stored.section : -1
            draftBase.value = stored.base; draftRequestId.value = stored.request || artifactRequestId()
            if (stored.base !== item.current_revision_id) notice.value = '此设备保留了旧版本上的编辑；提交时将保存草稿并提示冲突。'
          }
        } catch { draftStorageError.value = true }
      }
      draftSignature = JSON.stringify([content.value, sectionIndex.value])
    }
  } catch (err) { if (token === epoch) error.value = message(err) }
  finally { if (token === epoch) busy.value = false }
}
watch(() => route.fullPath, () => { viewingVersion.value = ''; publicationChangeId.value = null; publicationPath.value = ''; void load() }, { immediate: true })
async function guarded(action: () => Promise<void>) {
  const token = epoch
  busy.value = true; error.value = ''
  try { await action() } catch (err) { if (token === epoch) error.value = message(err) }
  finally { if (token === epoch) busy.value = false }
}
async function create() {
  await guarded(async () => {
    const item = await createArtifact({ request_id: artifactRequestId(), title: title.value, content: '', conversation_id: cid.value })
    await router.push({ name: 'artifact', params: { id: item.id }, query: cid.value ? { conv: cid.value } : {} })
  })
}
async function more() {
  const token = epoch
  const result = await listArtifacts(cid.value, nextOffset.value || 0)
  if (token === epoch) { items.value.push(...result.items); nextOffset.value = result.next_offset }
}
function chooseSection() {
  if (!currentRevision.value) return
  const section = selectedSection.value
  content.value = section ? Array.from(currentRevision.value.content).slice(section.start, section.end).join('') : currentRevision.value.content
  draftBase.value = currentRevision.value.id; draftRequestId.value = artifactRequestId()
}
async function submit() {
  const item = selected.value
  if (!item) return
  await guarded(async () => {
    const token = epoch, section = selectedSection.value
    const change = await proposeArtifactChange(item.id, { conversation_id: cid.value, request_id: draftRequestId.value,
      base_revision_id: draftBase.value, ...(section ? { section: { index: section.index, expected_sha256: section.sha256, content: content.value } } : { content: content.value }) })
    if (token !== epoch) return
    selectedChange.value = change
    changes.value = (await artifactChanges(item.id, cid.value)).items
    notice.value = '修改草稿已持久保存，请审阅差异后接受或拒绝。'
  })
}
async function decide(accept: boolean) {
  const item = selected.value, change = selectedChange.value
  if (!item || !change) return
  await guarded(async () => {
    const token = epoch
    await decideArtifactChange(item.id, change.id, { conversation_id: cid.value, accept, expected_revision_id: change.base_revision_id })
    if (token !== epoch) return
    // A decision concerns one submitted change. Text typed after submission
    // belongs to another draft, even when the request was still in flight.
    const draft = draftSnapshot(), section = selectedSection.value
    const points = Array.from(item.revision?.content || '')
    const draftBody = section ? points.slice(0, section.start).join('') + draft.content + points.slice(section.end).join('') : draft.content
    const sameDraft = !historical.value && draft.base === change.base_revision_id &&
      (!section || item.revision?.id === draft.base) && draftBody === change.content
    const retained = historical.value || sameDraft ? undefined : draft
    if (sameDraft) clearDraft()
    else if (retained) {
      try { localStorage.setItem(draftKey.value, JSON.stringify(draft)) } catch { draftStorageError.value = true }
    }
    viewingVersion.value = ''; await load(retained)
    notice.value = accept ? '已保存为新版本，旧版本仍可查看。' : '已拒绝修改，正文保持原版本。'
    if (retained) notice.value += ' 更新的未提交编辑已保留；接受旧修改后，请核对基版本再提交。'
  })
}
async function viewVersion(id: string) { viewingVersion.value = id; await load() }
async function restore() {
  const item = selected.value
  if (!item?.revision) return
  await guarded(async () => {
    const change = await proposeArtifactChange(item.id, { conversation_id: cid.value, request_id: artifactRequestId(),
      base_revision_id: item.current_revision_id, restored_from: item.revision!.id })
    if (selectedId.value === item.id) { selectedChange.value = change; notice.value = '恢复操作将创建新版本，请审阅后接受。' }
  })
}
async function moreDiff() {
  if (!selected.value || !selectedChange.value) return
  const token = epoch, change = selectedChange.value
  const page = await artifactChangeDetail(selected.value.id, change.id, cid.value, change.diff_next_offset || 0)
  if (token === epoch && selectedChange.value?.id === change.id)
    selectedChange.value = { ...page, diff: change.diff + page.diff }
}
async function moreVersions() {
  if (!selected.value || !nextVersion.value) return
  const token = epoch, page = await artifactHistory(selected.value.id, cid.value, nextVersion.value)
  if (token === epoch) { versions.value.push(...page.items); nextVersion.value = page.next_before_version }
}
async function compute() {
  if (!selected.value?.revision) return
  const item = selected.value, token = epoch
  await guarded(async () => {
    const result = await recomputeArtifact(item.id, cid.value, item.revision!.id)
    if (token === epoch) { computations.value.unshift({ ...result, result }); notice.value = '已执行保存的复算脚本，运行记录已保存。' }
  })
}
async function preparePublication() {
  const item = selected.value, conversation = cid.value, token = epoch
  if (!item?.revision || !conversation) return
  const revision = item.revision.id, path = publicationPath.value.trim()
  await guarded(async () => {
    const target = await getChatProject(conversation)
    if (!target.project?.available) throw new Error('请先在原会话关联可用项目文件夹，再写入成果。')
    const original = await editableFile(target.project.id, path)
    const input = { conversation_id: conversation, revision_id: revision, project_id: target.project.id,
      workspace_revision: target.workspace_revision, path, expected_file_sha256: original.sha256 }
    const signature = JSON.stringify(input)
    if (signature !== publicationRequest.signature) publicationRequest = { signature, id: artifactRequestId() }
    if (token !== epoch) return
    const change = await proposeArtifactPublication(item.id, { ...input, request_id: publicationRequest.id })
    if (token === epoch) { publicationChangeId.value = change.id; notice.value = '项目写入草稿已保存，请审阅文件差异后应用。' }
  })
}
const exportLabels: Record<ArtifactExportFormat, string> = {md: 'Markdown', docx: 'Word', xlsx: 'XLSX', svg: 'SVG', bundle: '复算包', manifest: '来源与版本清单'}
async function generateExport(format: ArtifactExportFormat) {
  const item = selected.value, revision = item?.revision?.id, conversation = cid.value, token = epoch
  if (!item || !revision || busy.value || archived.value) return
  const signature = JSON.stringify([conversation, item.id, revision, format]), storageKey = `scholarflow:artifact-export:${signature}`
  let requestId = exportRequests.get(signature)
  if (!requestId) {
    try { requestId = sessionStorage.getItem(storageKey) || undefined } catch { /* memory retry remains available */ }
    if (!requestId || !/^[a-f0-9]{32}$/.test(requestId)) requestId = artifactRequestId()
    exportRequests.set(signature, requestId)
    try { sessionStorage.setItem(storageKey, requestId) } catch { /* do not discard the in-memory operation ID */ }
  }
  await guarded(async () => {
    const saved = await createArtifactExport(item.id, {conversation_id: conversation, revision_id: revision, format, request_id: requestId!})
    if (token !== epoch || selected.value?.revision?.id !== revision) return
    exports.value = [saved, ...exports.value.filter(e => e.id !== saved.id)]
    notice.value = `已保存 v${item.revision!.version} 的${exportLabels[format]}文件，可从下方固定文件列表下载。`
  })
}
async function moreExports() {
  const item = selected.value, revision = item?.revision?.id, token = epoch
  if (!item || !revision || nextExport.value === null) return
  const page = await listArtifactExports(item.id, cid.value, revision, nextExport.value)
  if (token !== epoch) return
  exports.value.push(...page.items.filter(e => !exports.value.some(old => old.id === e.id)))
  nextExport.value = page.next_offset
}
const evidenceIds = computed(() => currentRevision.value?.evidence.map(ev => String(ev.id || '')).filter(id => /^e\d+$/.test(id)) || [])
function openEvidence(id: string) {
  const ev = currentRevision.value?.evidence.find(ev => ev.id === id)
  if (ev?.revision_id && cid.value) {
    void router.push({ name: 'source-reader', params: { revisionId: ev.revision_id },
      query: { conversation: cid.value, ...(ev.locator_id ? { locator: ev.locator_id } : {}) } })
  } else notice.value = '此条引用未保存可访问的固定来源版本，请查看下方来源清单。'
}
</script>

<template>
  <div class="page artifact-page">
    <div class="page-head"><div><div class="page-title">研究成果</div><div class="page-desc">编辑文档、审阅修改与恢复版本；图表保留数据和复算方法。</div></div></div>
    <p v-if="error" role="alert" class="err">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="draftStorageError" class="err">此设备无法保存未提交编辑，请先提交修改草稿再离开。</p>
    <template v-if="!selectedId">
      <nav class="toolbar" aria-label="成果范围">
        <RouterLink :to="{name:'artifacts'}">独立成果</RouterLink>
        <RouterLink v-if="activeConversationId" :to="{name:'artifacts',query:{conv:activeConversationId}}">当前会话成果</RouterLink>
        <span>{{ cid ? '当前会话' : '独立成果' }}</span>
      </nav>
      <form class="toolbar" @submit.prevent="create"><label>文档标题 <input v-model="title" maxlength="240" required :disabled="archived" /></label><button class="el-button" :disabled="busy || archived || !title.trim()">新建文档</button></form>
      <p v-if="!busy && !items.length">暂无保存的成果。可以新建文档，或从回答、论文框架和数据图表中保存。</p>
      <ul class="artifact-list"><li v-for="item in items" :key="item.id"><RouterLink :to="{name:'artifact',params:{id:item.id},query:cid?{conv:cid}:{}}">{{ item.title }}</RouterLink><span>{{ item.kind === 'analysis' ? '数据图表' : '文档' }} · v{{ item.version }}</span></li></ul>
      <button v-if="nextOffset !== null" class="el-button" @click="guarded(more)">加载更多</button>
    </template>
    <template v-else-if="selected && currentRevision">
      <div class="toolbar"><RouterLink :to="{name:'artifacts',query:cid?{conv:cid}:{}}">返回成果列表</RouterLink><h2>{{ selected.title }}</h2><span>查看 v{{ currentRevision.version }} / 当前 v{{ selected.version }}</span></div>
      <div class="toolbar">
        <label>历史版本 <select :value="currentRevision.id" aria-label="历史版本" @change="viewVersion(($event.target as HTMLSelectElement).value)"><option v-for="v in versions" :key="v.id" :value="v.id">v{{ v.version }} · {{ v.id.slice(0,8) }}</option></select></label>
        <button v-if="nextVersion" class="el-button" @click="guarded(moreVersions)">更早版本</button>
        <button v-if="historical" class="el-button" :disabled="busy || archived" @click="restore">恢复为新版本</button>
        <button v-if="selected.kind === 'document'" class="el-button" :disabled="busy || archived" @click="generateExport('md')">生成 Markdown 文件</button><button class="el-button" :disabled="busy || archived" @click="generateExport('docx')">生成 Word 文件</button><button class="el-button" :disabled="busy || archived" @click="generateExport('manifest')">生成来源与版本清单</button>
      </div>
      <p v-if="archived">此会话已归档，可下载已有固定文件；恢复会话后才能生成新文件。</p>
      <section aria-label="固定导出文件"><h3>此版本的固定导出文件</h3>
        <p v-if="!exports.length">尚未生成文件。生成后保存实际文件字节，重复下载保持同一份文件。</p>
        <ul><li v-for="saved in exports" :key="saved.id"><a :href="savedArtifactExportDownloadUrl(saved.artifact_id, saved.id, cid)" download>下载 {{ exportLabels[saved.format] }}</a> · {{ saved.payload_size }} 字节<br /><small>SHA256：{{ saved.payload_sha256 }}</small></li></ul>
        <button v-if="nextExport !== null" class="el-button" :disabled="busy" @click="guarded(moreExports)">更早导出文件</button>
        <p>文件结构检查不证明内容语义或原生软件显示正确。XLSX 保存固定数据和描述统计；复算包包含脚本，执行记录另行核对。</p>
      </section>
      <div v-if="selected.kind === 'document'" class="document-grid">
        <section><label class="field-label">修改范围 <select v-model.number="sectionIndex" :disabled="historical || archived || busy" aria-label="修改范围" @change="chooseSection"><option :value="-1">全文</option><option v-for="s in selected.sections" :key="s.index" :value="s.index">{{ s.title }}</option></select></label>
          <textarea v-model="content" aria-label="成果正文" :readonly="historical || archived" spellcheck="false" rows="20" />
          <button class="el-button el-button--primary" :disabled="busy || historical || archived" @click="submit">保存修改草稿并查看差异</button><p>未接受的草稿不计为已保存正文。局部修改仅替换所选章节及其子章节。</p>
        </section>
        <section aria-label="已保存正文预览"><h3>已保存版本</h3><MarkdownAnswer :text="currentRevision.content" :evidence-ids="evidenceIds" @evidence="openEvidence" /></section>
      </div>
      <section v-else><h3>固定数据和参数</h3><pre class="data-preview">{{ currentRevision.content }}</pre><pre>{{ JSON.stringify(currentRevision.analysis,null,2) }}</pre><div class="toolbar"><button class="el-button" :disabled="busy || archived" @click="compute">执行复算</button><button class="el-button" :disabled="busy || archived" @click="generateExport('xlsx')">生成 XLSX 文件</button><button class="el-button" :disabled="busy || archived" @click="generateExport('bundle')">生成复算包</button><button class="el-button" :disabled="busy || archived" @click="generateExport('svg')">生成 SVG 文件</button></div><p>复算包含 CSV、参数、Python 脚本、SVG、结果及哈希；缺值保留为空，不计为零。</p>
        <div v-for="run in computations" :key="run.id"><strong>{{ run.status === 'succeeded' ? '复算成功' : '复算失败' }}</strong><pre>{{ JSON.stringify(run.result || run.error,null,2) }}</pre></div>
      </section>
      <section v-if="changes.length"><h3>待审阅修改</h3><div class="toolbar"><button v-for="change in changes" :key="change.id" class="el-button" @click="selectedChange=change">{{ change.id.slice(0,8) }}{{ change.conflict ? ' · 版本冲突' : '' }}</button></div></section>
      <section v-if="cid && selected.kind === 'document'"><h3>写入关联项目</h3><p>将当前查看的固定版本及来源写为 Markdown 文件；先审阅差异，再应用，可通过项目日志撤销。</p><form class="toolbar" @submit.prevent="preparePublication"><label>项目内文件路径 <input v-model="publicationPath" placeholder="reports/研究报告.md" required :disabled="archived" /></label><button class="el-button" :disabled="busy || archived || !publicationPath.trim()">生成项目写入差异</button></form></section>
      <section v-if="selectedChange" aria-label="修改差异"><h3>修改差异</h3><p v-if="selectedChange.conflict" class="err">当前正文已有新版本。双方版本与草稿均已保留，请比较后重新编辑。</p><pre class="diff">{{ selectedChange.diff }}</pre><button v-if="selectedChange.diff_has_more" class="el-button" @click="guarded(moreDiff)">加载其余差异</button><div class="toolbar"><button class="el-button el-button--primary" :disabled="busy || archived || selectedChange.conflict || selectedChange.diff_has_more" @click="decide(true)">接受修改</button><button class="el-button" :disabled="busy || archived" @click="decide(false)">拒绝修改</button></div></section>
      <section v-if="currentRevision.evidence.length"><h3>此版本的引用来源</h3><ul><li v-for="(ev,i) in currentRevision.evidence" :key="i"><RouterLink v-if="ev.revision_id && cid" :to="{name:'source-reader',params:{revisionId:ev.revision_id},query:{conversation:cid,...(ev.locator_id?{locator:ev.locator_id}:{})}}">{{ ev.source_title }} · {{ ev.revision_id.slice(0,8) }}</RouterLink><span v-else>{{ ev.paper_title || '旧引用' }}（未固定版本）</span><p>{{ ev.quote }}</p><small>语义支持关系须人工核验</small></li></ul></section>
    </template>
    <p v-else-if="busy" role="status">正在读取成果…</p>
    <FileChangeReview v-model="publicationChangeId" :conversation-id="cid || undefined" />
  </div>
</template>

<style scoped>
.toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin:16px 0; }
.toolbar h2 { margin:0; font-size:18px; }
.artifact-list { list-style:none; padding:0; }
.artifact-list li { display:flex; flex-wrap:wrap; justify-content:space-between; gap:12px; padding:18px 0; border-bottom:1px solid var(--fn-border-primary); }
.artifact-list span, small { color:var(--fn-text-secondary); }
.document-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:24px; }
section { min-width:0; margin:20px 0; }
textarea { width:100%; box-sizing:border-box; resize:vertical; line-height:1.8; margin:12px 0; font:inherit; }
textarea,input,select { background:var(--fn-bg-primary); color:var(--fn-text-primary); border:1px solid var(--fn-border-input); border-radius:6px; padding:8px; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:480px; overflow:auto; padding:16px; background:var(--fn-bg-secondary); }
.diff { font-size:12px; }
p { line-height:1.8; }
a { color:var(--fn-brand); }
.err { color:var(--fn-danger-text, #b42318); }
@media(max-width:1000px) { .document-grid {grid-template-columns:minmax(0,1fr);} }
</style>
