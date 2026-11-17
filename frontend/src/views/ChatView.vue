<script setup lang="ts">
import ProviderResearchReport from '../components/ProviderResearchReport.vue'
import HistoryRevisionDialog from '../components/HistoryRevisionDialog.vue'
import HistoryRevisionBanner from '../components/HistoryRevisionBanner.vue'
import OnboardingGuide from '../components/OnboardingGuide.vue'
import type { HistoryRevision } from '../historyRevisionsApi'
import {
  onMounted,
  onUnmounted,
  ref,
  shallowRef,
  reactive,
  computed,
  nextTick,
  watch,
  defineAsyncComponent,
} from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, ElTimeline, ElTimelineItem } from 'element-plus'
import {
  FileText,
  PenLine,
  BarChart3,
  ListChecks,
  Languages,
  Lightbulb,
  Paperclip,
  ArrowUp,
  ArrowDown,
  Square,
  ChevronRight,
  BookOpen,
  Wand2,
  X,
  Check,
  Brain,
  Download,
  ShieldCheck,
  PlayCircle,
  History,
  Pencil,
  Trash2,
  Coins,
  Copy,
  RotateCcw,
  GitFork,
  Archive,
  ArchiveRestore,
  FolderOpen,
  TerminalSquare,
  Plus,
  ArrowUpRight,
} from 'lucide-vue-next'
import {
  conversationArchive,
  changeConversationArchive,
  retryConversationArchive,
} from '../conversationArchive'
import MarkdownAnswer from '../components/MarkdownAnswer.vue'
import SaveArtifactButton from '../components/SaveArtifactButton.vue'
import TaskRequirements from '../components/TaskRequirements.vue'
import ConversationControls from '../components/ConversationControls.vue'
import ConversationSources from '../components/ConversationSources.vue'
import DiagnosticsPanel from '../components/DiagnosticsPanel.vue'
import SourceReferences from '../components/SourceReferences.vue'
import { normalizeSourceIds, sourceReaderLocation, evidenceSourceKey } from '../sourceSelection'
import ConversationMemoryPanel from '../components/ConversationMemory.vue'
import ProjectWorkspace from '../components/ProjectWorkspace.vue'
const ProjectTerminal = defineAsyncComponent(() => import('../components/ProjectTerminal.vue'))
const showTerminal = ref(false),
  terminalLoaded = ref(false)
watch(showTerminal, (visible) => {
  if (visible) terminalLoaded.value = true
})
import FileChangeReview from '../components/FileChangeReview.vue'
import ProjectCommandReview from '../components/ProjectCommandReview.vue'
import GitOperationReview from '../components/GitOperationReview.vue'
import {
  gitOperationEvents,
  gitActionLabels,
  gitStatusLabels,
  gitApprovalSummary,
  type GitOperationTarget,
} from '../projectGitApi'
const gitReviewTarget = ref<GitOperationTarget | null>(null)
import { commandEvents, commandStatus } from '../projectCommandApi'
const commandReviewId = ref<string | null>(null)
const fileReviewId = ref<string | null>(null)
function turnChanges(turn: { events: AgentEvent[] }) {
  return [
    ...new Map(
      turn.events
        .filter((e) => e.kind === 'project_change' && e.change_id)
        .map((e) => [e.change_id, e])
    ).values(),
  ]
}
import {
  getChatProject,
  listProjectFiles,
  initProjectRules,
  type Project,
  projectError,
} from '../projectApi'
import {
  activeConversationId,
  conversationState,
  selectConversation,
  forgetConversation,
  cacheDraftText,
  flushDesktopState,
  type ConversationState,
} from '../desktopState'
import { isActiveTask, taskState } from '../taskState'
import { useAnswerReadState } from '../answerReadState'
import { useConversationList } from '../conversationList'
import {
  processEvents,
  processPresentation,
  processThinkingParts,
  publicProcessEntries,
  publicToolArgument,
} from '../chatProcess'
import {
  applyChatRunEvent,
  applyChatRunPause,
  citationReviewIssues,
  deduplicateChatRunDisplays,
  type ChatRunPause,
  restoreChatRunTrace,
} from '../chatRunObserver'
import { durableResumeRequest } from '../chatResume'
import { conversationModelStatus } from '../modelStatus'
import { listConversationPage, getConversationSummary, getPlanClaims } from '../api'
import {
  pagePapers,
  getPaper,
  listSkills,
  streamChat,
  observeChatRun,
  cancelChatRun,
  listRunInputs,
  listConversationRunInputs,
  submitRunInput,
  cancelRunInput,
  importPaperFromUrl,
  decideApproval,
  reviewDraft,
  finalCheck,
  exportAs,
  startBackgroundTask,
  listBackgroundTasks,
  cancelBackgroundTask,
  getConversation,
  createConversation,
  renameConversation,
  deleteConversation,
  previewConversationDeletion,
  forkConversation,
  getSettings,
  saveSettings,
  getHealth,
  listMcpServers,
  createForegroundRun,
  listForegroundRuns,
  dismissForegroundRun,
  getConversationUsage,
  getGlobalUsage,
  getGoal,
  setGoal,
  pauseGoal,
  resumeGoal,
  completeGoal,
  cancelGoal,
  type AgentEvent,
  type RunInput,
  type RunInputKind,
  type RunInputQueue,
  type Evidence,
  type Paper,
  type Skill,
  type TodoStep,
  type ApprovalRequest,
  type SubagentResult,
  type ReviewResult,
  type FinalCheckResult,
  type Conversation,
  type ModelSettings,
  type McpServer,
  type TokenUsage,
  type Goal,
  type BackgroundTask,
  type ConversationArchiveState,
  type ExecutionMode,
  type ExecutionOptions,
  type RuntimeConfiguration,
} from '../api'

const route = useRoute()
const router = useRouter()

const papers = ref<Paper[]>([])
const paperId = ref<string | null>((route.query.paper as string) || null)
const paperQuery=ref(''),paperChoiceCount=ref(0),paperLibraryCount=ref(0),paperChoiceError=ref('')
let paperChoiceRequest:AbortController|null=null,paperChoiceGeneration=0,paperChoiceTimer:ReturnType<typeof setTimeout>|undefined
async function loadPaperChoices(){
  const token=++paperChoiceGeneration;paperChoiceRequest?.abort();paperChoiceRequest=new AbortController()
  try{
    const result=await pagePapers({q:paperQuery.value,limit:40},paperChoiceRequest.signal)
    if(token!==paperChoiceGeneration)return
    const current=paperId.value&&!result.items.some(p=>p.id===paperId.value)?await getPaper(paperId.value).catch(()=>null):null
    if(token!==paperChoiceGeneration)return
    papers.value=current?[current,...result.items]:result.items;paperChoiceCount.value=result.total;paperLibraryCount.value=result.statistics.papers;paperChoiceError.value=''
  }catch(e:any){if(token===paperChoiceGeneration&&e?.code!=='ERR_CANCELED')paperChoiceError.value='文献范围读取失败，请重试'}
}
watch(paperQuery,()=>{paperChoiceGeneration++;paperChoiceRequest?.abort();if(paperChoiceTimer)clearTimeout(paperChoiceTimer);paperChoiceTimer=setTimeout(()=>void loadPaperChoices(),180)})
watch(paperId,()=>{if(paperId.value&&!papers.value.some(p=>p.id===paperId.value))void loadPaperChoices()})
onUnmounted(()=>{paperChoiceGeneration++;paperChoiceRequest?.abort();if(paperChoiceTimer)clearTimeout(paperChoiceTimer)})
const draft = ref('')
function thinkingOf(turn: Turn) {
  return processThinkingParts(turn.thinkingParts, turn.liveThinking, turn.events)
}
function toggleProcess(turn: Turn) {
  turn.processExpanded = !turn.processExpanded
  if (turn.processExpanded) {
    followingOutput.value = false
    void nextTick(() => saveReadingPosition())
  }
}
function toggleToolDetails(turn: Turn, id: string) {
  turn.expandedToolEvents = { ...turn.expandedToolEvents, [id]: !turn.expandedToolEvents?.[id] }
  if (turn.expandedToolEvents[id]) {
    followingOutput.value = false
    void nextTick(() => saveReadingPosition())
  }
}
function foregroundStatusLabel(turn: Turn): string {
  if (turn.interruptedStatus === 'paused') {
    return ({
      context_capacity: '上下文容量不足，进度已保存。',
      resource_budget: '预算检查未通过，进度已保存。',
      no_progress: '连续执行未取得新结果，进度已保存。',
      step_limit: '本阶段执行轮数已用完，进度已保存。',
      skill_changed: '技能已变更，进度已保存。',
      memory_changed: '会话记忆已变更，进度已保存。',
    }[turn.pauseReason || ''] || '任务已暂停，进度已保存；可以继续当前任务。')
  }
  return (
    (
      {
        running_elsewhere: '本轮仍在后台运行，进度和结果会自动同步。',
        finishing: '正在等待服务端确认本轮已结束…',
        interrupted: '连接中断，已保留最后一次保存的部分回答。',
        cancelled: '本轮已停止，已保留部分回答。',
        failed: '本轮失败，已保留部分回答。',
      } as Record<string, string>
    )[turn.interruptedStatus || ''] || ''
  )
}
function checkpointPhaseLabel(phase: string): string {
  return (
    (
      {
        setup: '准备中',
        thinking: '推理中',
        answer: '生成回答',
        finalize: '核查引用',
        critic: '核查引用',
        review_progress: '核查引用',
        stopping: '正在停止',
        disconnecting: '连接中断',
        budget_paused: '等待继续执行',
        context_paused: '等待调整上下文容量后继续',
        resource_paused: '等待调整费用或时间预算后继续',
        no_progress_paused: '等待调整任务要求后继续',
        skill_paused: '等待确认技能变更后继续',
        memory_paused: '等待确认会话记忆变更后继续',
        paused: '等待继续',
        setup_abandoned: '发送已取消',
      } as Record<string, string>
    )[phase] ||
    KIND_LABEL[phase] ||
    phase
  )
}

function citationReviewing(turn: Turn): boolean {
  const active = turn.streaming || ['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || '')
  return active && Boolean(
    turn.reviewProgress ||
      ['finalize', 'critic', 'audit', 'retry', 'review_progress'].includes(turn.checkpointPhase)
  )
}

function citationReviewProgressText(turn: Turn): string {
  const progress = turn.reviewProgress
  if (!progress) return ''
  const message = typeof progress.message === 'string' ? progress.message :
    typeof progress.text === 'string' ? progress.text : ''
  if (message) return message
  if (Number.isFinite(progress.completed) && Number.isFinite(progress.total))
    return `已核查 ${progress.completed}/${progress.total} 条论断`
  return ''
}

// ---- 技能选择 ----
const skillLib = ref<Skill[]>([])
const selectedSkills = ref<string[]>([])
const showSkillPicker = ref(false)
const skillPickerEl = ref<HTMLElement | null>(null)
const composerMenuOpen = ref(false)
const composerMenuEl = ref<HTMLElement | null>(null)
const composerAddButton = ref<HTMLButtonElement | null>(null)

function closeComposerMenu(restoreFocus = false) {
  composerMenuOpen.value = false
  if (restoreFocus) composerAddButton.value?.focus()
}
async function toggleComposerMenu() {
  showSkillPicker.value = false
  if (composerMenuOpen.value) {
    closeComposerMenu(true)
    return
  }
  composerMenuOpen.value = true
  await nextTick()
  composerMenuEl.value?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
}
function composerMenuKeys(event: KeyboardEvent) {
  const buttons = [
    ...(composerMenuEl.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || []),
  ]
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeComposerMenu(true)
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    event.preventDefault()
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? buttons.length - 1
          : (index + (event.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length
    buttons[next]?.focus()
  } else if (event.key === 'Tab') closeComposerMenu(true)
}
async function useComposerAction(action: 'files' | 'sources' | 'images' | 'review' | 'skills' | 'background') {
  closeComposerMenu(true)
  if (action === 'files') showWorkspace.value = true
  else if (action === 'sources') conversationSources.value?.show()
  else if (action === 'images') pickImage()
  else if (action === 'review') openReview()
  else if (action === 'background') void askInBackground()
  else {
    showSkillPicker.value = true
    await nextTick()
    skillPickerEl.value?.querySelector<HTMLButtonElement>('.skill-pop button')?.focus()
  }
}
function closeSkillPicker() {
  showSkillPicker.value = false
  composerAddButton.value?.focus()
}

// ---- 多格式导出 ----
const exportMenuFor = ref<number | null>(null)
const EXPORT_FORMATS = [
  { key: 'md', label: 'Markdown (.md)' },
  { key: 'docx', label: 'Word (.docx)' },
  { key: 'bibtex', label: '参考文献 BibTeX (.bib)' },
  { key: 'ris', label: '参考文献 RIS (.ris)' },
] as const
function toggleExportMenu(ti: number) {
  exportMenuFor.value = exportMenuFor.value === ti ? null : ti
}
async function doExport(turn: Turn, format: string) {
  exportMenuFor.value = null
  try {
    await exportAs(turn.question, turn.answer, turn.evidence, format as any)
    ElMessage.success('已开始导出，请确认文件保存位置')
  } catch (e: any) {
    ElMessage.error(e?.message || '导出失败')
  }
}

// ---- 一键审我的稿件 ----
const showReviewModal = ref(false)
const reviewDraftText = ref('')
const reviewResult = ref<ReviewResult | null>(null)
const reviewing = ref(false)
const reviewFile = ref<HTMLInputElement | null>(null)

function openReview() {
  showReviewModal.value = true
  reviewResult.value = null
}
function onReviewFile(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    reviewDraftText.value = String(reader.result)
  }
  reader.readAsText(file)
  ;(e.target as HTMLInputElement).value = ''
}
async function runReview() {
  if (!reviewDraftText.value.trim()) {
    ElMessage.warning('请先粘贴或导入稿件')
    return
  }
  reviewing.value = true
  reviewResult.value = null
  try {
    reviewResult.value = await reviewDraft(reviewDraftText.value)
  } catch (e: any) {
    ElMessage.error(e?.message || '审稿失败')
  } finally {
    reviewing.value = false
  }
}

// ---- 多模态图片附件 ----
const pendingImages = ref<{ media_type: string; data: string; preview: string }[]>([])
const pendingSourceIds = ref<string[]>([])
const sourceUploading = ref(false)
const conversationSources = ref<InstanceType<typeof ConversationSources> | null>(null)
const imageInput = ref<HTMLInputElement | null>(null)
const imageReaders = new Map<FileReader, number>()
const readingImages = ref(0)
function cancelImageReads() {
  for (const reader of imageReaders.keys()) reader.abort()
  imageReaders.clear()
  readingImages.value = 0
}

function pickImage() {
  imageInput.value?.click()
}
function onImagePicked(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (!files) return
  const epoch = conversationEpoch
  const cid = conversationId.value
  let reservedBytes =
    pendingImages.value.reduce((sum, image) => sum + (image.data.length * 3) / 4, 0) +
    [...imageReaders.values()].reduce((sum, size) => sum + size, 0)
  let reservedCount = pendingImages.value.length + imageReaders.size
  for (const file of Array.from(files)) {
    if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)) {
      ElMessage.warning('请选择 PNG、JPEG、WebP 或 GIF 图片')
      continue
    }
    if (
      file.size > 5 * 1024 * 1024 ||
      reservedBytes + file.size > 8 * 1024 * 1024 ||
      reservedCount >= 4
    ) {
      ElMessage.warning('最多 4 张图片，单张不超过 5 MB，合计不超过 8 MB')
      continue
    }
    reservedBytes += file.size
    reservedCount++
    const reader = new FileReader()
    imageReaders.set(reader, file.size)
    readingImages.value = imageReaders.size
    reader.onload = () => {
      imageReaders.delete(reader)
      readingImages.value = imageReaders.size
      if (disposed || epoch !== conversationEpoch || cid !== conversationId.value) return
      const result = reader.result as string
      const comma = result.indexOf(',')
      pendingImages.value.push({
        media_type: file.type,
        data: result.slice(comma + 1),
        preview: result,
      })
      void activeState.value.flush()
    }
    reader.onerror = () => {
      imageReaders.delete(reader)
      readingImages.value = imageReaders.size
      if (!disposed && epoch === conversationEpoch) ElMessage.error('图片读取失败，请重新添加')
    }
    reader.readAsDataURL(file)
  }
  ;(e.target as HTMLInputElement).value = ''
}
function removeImage(i: number) {
  pendingImages.value.splice(i, 1)
}

function toggleSkill(name: string) {
  const i = selectedSkills.value.indexOf(name)
  if (i >= 0) selectedSkills.value.splice(i, 1)
  else selectedSkills.value.push(name)
}
function removeSkill(name: string) {
  const i = selectedSkills.value.indexOf(name)
  if (i >= 0) selectedSkills.value.splice(i, 1)
}
function skillNameOf(n: string) {
  return skillLib.value.find((s) => s.name === n)?.title || n
}

/**
 * 解析问题中的 `$技能名`：仅当匹配真实技能名时才剥离该标记并登记为附加技能；
 * 非技能名（如 $100）原样保留。返回干净文本与需附加的技能名。
 */
function extractDollarSkills(text: string): { clean: string; skills: string[] } {
  const known = new Set(skillLib.value.map((s) => s.name))
  const found = new Set<string>()
  const stripped = text.replace(/\$([a-zA-Z0-9_-]+)/g, (whole, name: string) => {
    if (known.has(name)) {
      found.add(name)
      return ''
    }
    return whole
  })
  const clean = stripped
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\s*\n\s*/g, '\n')
    .trim()
  return { clean, skills: [...found] }
}

// ---- 统一 @ mention（论文/技能/MCP，方向键 + Tab/回车补全）----
const composerEl = ref<HTMLTextAreaElement | null>(null)
const showMention = ref(false)
const mentionIndex = ref(0)
const mentionQuery = ref('')
const mentionAt = ref(-1) // draft 中 @ 的字符位置
const mcpServers = ref<McpServer[]>([])

interface MentionItem {
  type: 'paper' | 'skill' | 'mcp' | 'file'
  label: string
  hint: string
  apply: () => void
}

const mentionItems = computed<MentionItem[]>(() => {
  const items: MentionItem[] = []
  for (const file of mentionFiles.value)
    items.push({
      type: 'file',
      label: file,
      hint: '文件 · 引用到本轮',
      apply: () => {
        if (!selectedFiles.value.includes(file) && selectedFiles.value.length < 8)
          selectedFiles.value.push(file)
      },
    })
  if (project.value)
    items.push({
      type: 'file',
      label: '浏览项目文件',
      hint: project.value.name,
      apply: () => {
        showWorkspace.value = true
      },
    })
  for (const p of papers.value) {
    items.push({
      type: 'paper',
      label: p.title || '未命名论文',
      hint: '论文 · 设为检索范围',
      apply: () => {
        paperId.value = p.id
      },
    })
  }
  for (const s of skillLib.value) {
    items.push({
      type: 'skill',
      label: s.title,
      hint: '技能 · 附加到本次提问',
      apply: () => {
        if (!selectedSkills.value.includes(s.name)) selectedSkills.value.push(s.name)
      },
    })
  }
  for (const m of mcpServers.value) {
    if (!m.enabled) continue
    items.push({
      type: 'mcp',
      label: m.name,
      hint: `MCP · ${m.tool_count} 个工具`,
      apply: () => {},
    })
  }
  const q = mentionQuery.value.toLowerCase()
  const filtered = q ? items.filter((i) => i.label.toLowerCase().includes(q)) : items
  return filtered.slice(0, 8)
})

function detectMention() {
  const el = composerEl.value
  if (!el) {
    showMention.value = false
    return
  }
  const pos = el.selectionStart ?? draft.value.length
  const before = draft.value.slice(0, pos)
  const m = before.match(/(^|[\s])@([\w一-鿿.\-]*)$/)
  if (m) {
    mentionQuery.value = m[2]
    mentionAt.value = pos - m[2].length - 1
    mentionIndex.value = 0
    showMention.value = true
  } else {
    showMention.value = false
  }
}

function mentionTypeLabel(t: string): string {
  return (
    ({ paper: '论文', skill: '技能', mcp: 'MCP', file: '文件' } as Record<string, string>)[t] || t
  )
}

function selectMention(item: MentionItem) {
  const el = composerEl.value
  if (!el) return
  const pos = el.selectionStart ?? draft.value.length
  const insert = '@' + item.label
  const start = mentionAt.value
  const next = draft.value.slice(0, start) + insert + draft.value.slice(pos)
  draft.value = next
  item.apply()
  showMention.value = false
  nextTick(() => {
    const caret = start + insert.length
    el.selectionStart = caret
    el.selectionEnd = caret
    el.focus()
  })
}

// ---- 斜杠命令系统 ----
const showSlash = ref(false)
const slashIndex = ref(0)
interface QueuedCommand {
  name: string
  args: string
  raw: string
  epoch: number
}
const queuedSlashCommands = ref<QueuedCommand[]>([])
let conversationEpoch = 0
let foregroundHandoffController: AbortController | null = null
let conversationLoadController:AbortController|null=null
const historyWindow=ref<{total:number;offset:number;limit:number|null;previous_offset:number|null;next_offset:number|null}|null>(null)
const historyPageError=ref('')
let flushingCommands = false
let disposed = false
const runtimeSettings = ref<ModelSettings | null>(null)
const healthInfo = ref<any>(null)
const mcpRuntime = ref<McpServer[]>([])
const showStatus = ref(false)
const showMemory = ref(false)
const memoryPanel = ref<InstanceType<typeof ConversationMemoryPanel> | null>(null)
function openMemory(update = false) {
  return memoryPanel.value?.open(update)
}
function openConversationMemory() {
  void openMemory()
}
async function showMemorySource(id: string) {
  await nextTick()
  const node = [...(scrollEl.value?.querySelectorAll<HTMLElement>('[data-record-id]') || [])].find(
    (el) => el.dataset.recordId === id
  )
  if (node) {
    followingOutput.value = false
    node.scrollIntoView({ block: 'center' })
    node.focus({ preventScroll: true })
  } else ElMessage.info('原始记录尚未载入，请刷新当前会话后查看')
}
const statusUsage = ref<{ turns: number; usage: TokenUsage } | null>(null)
// ---- 配置诊断（doctor）----
const showDiagnostics = ref(false)
function openDiagnostics() {
  showDiagnostics.value = true
}

// ---- 持久目标 /goal ----
const showGoal = ref(false)
const currentGoal = ref<Goal | null>(null)
const goalHistory = ref<Goal[]>([])
const goalDraft = ref('')
const goalBusy = ref(false)

async function loadGoal() {
  const r = await getGoal()
  currentGoal.value = r.current
  goalHistory.value = r.goals
}

async function openGoal() {
  await loadGoal()
  goalDraft.value = currentGoal.value?.description || ''
  showGoal.value = true
}

// 关闭状态面板并打开目标管理
async function manageGoal() {
  showStatus.value = false
  await openGoal()
}

// 关闭归档错误横幅：清空错误信息与重试操作
function dismissArchiveError() {
  conversationArchive.error = ''
  conversationArchive.retry = null
}

async function _applyGoal(fn: () => Promise<any>) {
  goalBusy.value = true
  try {
    const r = await fn()
    currentGoal.value = r.current
    goalHistory.value = r.goals
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '操作失败')
  } finally {
    goalBusy.value = false
  }
}

function onCreateGoal() {
  const desc = goalDraft.value.trim()
  if (!desc) {
    ElMessage.warning('请输入目标描述')
    return
  }
  _applyGoal(() => setGoal(desc)).then(() => {
    goalDraft.value = ''
  })
}
async function setGoalFromCommand(description: string) {
  await _applyGoal(() => setGoal(description))
  goalDraft.value = ''
  showGoal.value = false
  ElMessage.success('持久目标已更新')
}
function onPauseGoal() {
  _applyGoal(pauseGoal)
}
function onResumeGoal() {
  _applyGoal(resumeGoal)
}
function onCompleteGoal() {
  _applyGoal(completeGoal)
}
function onCancelGoal() {
  _applyGoal(cancelGoal)
}

function goalStatusLabel(s: string): string {
  const labels: Record<string, string> = {
    active: '进行中',
    paused: '已暂停',
    done: '已完成',
    cancelled: '已取消',
  }
  return labels[s] || s
}

const slashQuery = computed(() => {
  const d = draft.value
  if (!d.startsWith('/')) return ''
  const first = d.split(/\s/)[0]
  return first.slice(1).toLowerCase()
})

interface SlashCommand {
  name: string
  desc: string
  run: () => unknown
}

const commandRegistry = computed<SlashCommand[]>(() => {
  const registry: SlashCommand[] = [
    {
      name: 'plan',
      desc: '切换计划模式；可附任务内容',
      run: () => {
        executionMode.value = 'plan'
      },
    },
    {
      name: 'execute',
      desc: '切换执行模式',
      run: () => {
        executionMode.value = 'execute'
      },
    },
    {
      name: 'new',
      desc: '开始一个新会话（清空当前对话）',
      run: () => {
        newConversation()
        ElMessage.success('已开始新会话')
      },
    },
    {
      name: 'clear',
      desc: '清空当前对话并开始新会话，保留历史记录',
      run: () => {
        newConversation()
        ElMessage.success('当前对话已清空')
      },
    },
    { name: 'history', desc: '查看并恢复历史会话', run: openHistoryPanel },
    { name: 'resume', desc: '从历史列表恢复一个会话', run: openHistoryPanel },
    { name: 'fork', desc: '从当前会话创建分支，保留原会话', run: forkCurrentConversation },
    { name: 'rename', desc: '重命名当前会话', run: renameCurrentConversation },
    { name: 'copy', desc: '复制最近一条完整回答', run: copyLatestAnswer },
    { name: 'recap', desc: '查看最近八轮会话的问答摘录与证据数', run: openRecap },
    { name: 'compact', desc: '压缩较早对话并查看本机摘要与来源', run: () => openMemory(true) },
    { name: 'init', desc: '在当前项目创建 AGENTS.md，保留已有文件', run: initCurrentProject },
    {
      name: 'files',
      desc: '浏览并引用本地项目文件',
      run: () => {
        showWorkspace.value = true
      },
    },
    { name: 'usage', desc: '查看问答与摘要的已记录 token 用量', run: openUsage },
    { name: 'ps', desc: '查看后台进程（运行状态，可取消）', run: openPs },
    { name: 'stop', desc: '停止最近一个运行中的后台任务', run: stopLatest },
    { name: 'tasks', desc: '打开后台任务页', run: () => router.push('/tasks') },
    { name: 'goal', desc: '设置/管理跨轮持久目标（可暂停、恢复、完成）', run: openGoal },
    { name: 'status', desc: '查看运行状态面板（模型/审批/记忆等）', run: openStatus },
    { name: 'debug', desc: '本地配置诊断：数据库、磁盘、模型状态；可主动测试接口', run: openDiagnostics },
    { name: 'debug-config', desc: '查看配置诊断', run: openDiagnostics },
    { name: 'approval', desc: '切换审批模式：只读 → 自动 → 完全访问', run: cycleApproval },
    { name: 'reasoning', desc: '切换推理强度：low → medium → high', run: cycleReasoning },
    { name: 'memory', desc: '开/关记忆（历史上下文加载）', run: toggleMemory },
    { name: 'settings', desc: '打开设置页', run: () => router.push('/settings') },
    { name: 'help', desc: '查看全部斜杠命令说明', run: openHelp },
  ]
  return registry
})

const slashCommands = computed<SlashCommand[]>(() => {
  const registry = commandRegistry.value
  const q = slashQuery.value
  const list = q ? registry.filter((c) => c.name.includes(q) || c.desc.includes(q)) : registry
  return list
})

function openHelp() {
  const lines = commandRegistry.value.map((command) => `/${command.name}  ${command.desc}`)
  return ElMessageBox.alert(lines.join('\n'), '斜杠命令', {
    confirmButtonText: '关闭',
    customClass: 'slash-help-dialog',
  })
}

function openRecap() {
  if (!turns.value.length) {
    ElMessage.info('当前会话还没有可回顾的内容')
    return
  }
  const lines = turns.value.slice(-8).map((turn, index) => {
    const answer = (turn.answer || turn.liveAnswer || '回答仍在生成').replace(/\s+/g, ' ').trim()
    const evidence = turn.evidence.length ? ` · ${turn.evidence.length} 条证据` : ''
    return `${index + 1}. ${turn.question}\n   ${answer.slice(0, 180)}${answer.length > 180 ? '…' : ''}${evidence}`
  })
  return ElMessageBox.alert(lines.join('\n\n'), '最近问答摘录 /recap', {
    confirmButtonText: '关闭',
    customClass: 'slash-help-dialog recap-dialog',
  })
}

async function openUsage() {
  try {
    const result = await getGlobalUsage()
    const {
      prompt_tokens: prompt,
      completion_tokens: completion,
      total_tokens: total,
    } = result.usage
    return ElMessageBox.alert(
      `会话数：${result.conversations}\n问答轮次：${result.turns}\n输入 tokens：${prompt}\n输出 tokens：${completion}\n合计 tokens：${total}\n摘要调用：${result.compaction?.calls || 0} 次（${result.compaction?.estimated_calls || 0} 次估算，${result.compaction?.unknown_calls || 0} 次未知）\n摘要用量独立保留，问答用量仍随历史记录统计；不是完整计费账单。`,
      '已记录用量 /usage',
      { confirmButtonText: '关闭', customClass: 'slash-help-dialog' }
    )
  } catch (e: any) {
    ElMessage.error(e?.message || '读取用量失败')
  }
}

async function openStatus() {
  const [s, h, m] = await Promise.all([getSettings(), getHealth(), listMcpServers()])
  runtimeSettings.value = s
  healthInfo.value = h
  mcpRuntime.value = m
  loadGoal().catch(() => {})
  statusUsage.value = null
  if (conversationId.value) {
    try {
      const u = await getConversationUsage(conversationId.value)
      statusUsage.value = { turns: u.turns, usage: u.usage }
    } catch {
      /* 无记录时忽略 */
    }
  }
  showStatus.value = true
}

async function cycleApproval() {
  if (await conversationControls.value?.cycleApproval()) ElMessage.success('已更新当前会话权限')
}

async function cycleReasoning() {
  if (await conversationControls.value?.cycleReasoning())
    ElMessage.success('已更新当前会话推理强度')
}

async function toggleMemory() {
  // memory_recall 为后端布尔运行时字段（ModelSettings 类型未列出，做一次扩展转换）
  const current = (runtimeSettings.value as any)?.memory_recall
  const next = current === false ? true : false
  const saved = await saveSettings({ memory_recall: next } as Partial<ModelSettings>)
  runtimeSettings.value = saved
  ElMessage.success(next ? '记忆已开启' : '记忆已关闭')
}

async function runSlashCommand(cmd: SlashCommand) {
  const raw = draft.value.trim()
  const match = raw.match(/^\/([^\s]+)(?:\s+([\s\S]*))?$/)
  const args = match?.[2]?.trim() || ''
  draft.value = ''
  showSlash.value = false
  if ((turns.value.some((turn) => turn.streaming) || flushingCommands) && cmd.name !== 'stop') {
    queuedSlashCommands.value.push({
      name: cmd.name,
      args,
      raw: `/${cmd.name}${args ? ` ${args}` : ''}`,
      epoch: conversationEpoch,
    })
    ElMessage.info(`已排队 /${cmd.name}，当前回答完成后执行`)
    return
  }
  await executeSlashCommand(cmd, args)
}

async function executeSlashCommand(cmd: SlashCommand, args: string) {
  try {
    if ((cmd.name === 'plan' || cmd.name === 'execute') && args) {
      executionMode.value = cmd.name === 'plan' ? 'plan' : 'execute'
      if (flushingCommands)
        await ask(args, {
          execution_mode: executionMode.value,
          workspace_revision: workspaceRevision.value,
          preserveDraft: true,
          images: [],
        })
      else {
        draft.value = args
        await ask()
      }
    } else if (cmd.name === 'goal' && args) await setGoalFromCommand(args)
    else if (cmd.name === 'rename' && args) await renameCurrentConversation(args)
    else if (cmd.name === 'resume' && args) await resumeConversationArg(args)
    else if (cmd.name === 'fork' && args) await forkCurrentConversation(args)
    else await cmd.run()
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') {
      ElMessage.error(e?.response?.data?.detail || e?.message || '命令执行失败')
    }
  }
}

async function flushQueuedSlashCommands() {
  if (flushingCommands || disposed) return
  flushingCommands = true
  try {
    while (
      queuedSlashCommands.value.length &&
      !disposed &&
      route.path === '/chat' &&
      !turns.value.some((turn) => turn.streaming)
    ) {
      const queued = queuedSlashCommands.value.shift()!
      if (queued.epoch !== conversationEpoch) continue
      const command = commandRegistry.value.find((item) => item.name === queued.name)
      if (command) await executeSlashCommand(command, queued.args)
    }
  } finally {
    flushingCommands = false
  }
}

function clearQueuedSlashCommands() {
  queuedSlashCommands.value = []
}

// ---- /ps 后台进程面板 & /stop 取消 ----
const showPs = ref(false)
const psTasks = ref<BackgroundTask[]>([])
const psLoading = ref(false)

function psStatusLabel(s: string): string {
  const map: Record<string, string> = {
    pending: '等待中',
    running: '运行中',
    done: '已完成',
    failed: '失败',
    cancelled: '已取消',
    interrupted: '已中断',
  }
  return map[s] || s
}

async function openPs() {
  showPs.value = true
  psLoading.value = true
  try {
    const r = await listBackgroundTasks()
    psTasks.value = r.tasks || []
  } catch (e: any) {
    ElMessage.error(e?.message || '加载后台任务失败')
  } finally {
    psLoading.value = false
  }
}

async function stopPsTask(t: BackgroundTask) {
  try {
    await cancelBackgroundTask(t.id)
    ElMessage.success('已请求取消：' + (t.title || t.id))
    const r = await listBackgroundTasks()
    psTasks.value = r.tasks || []
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '取消失败')
  }
}

async function stopLatest() {
  if (foregroundRunActive.value) {
    await stop()
    return
  }
  try {
    const r = await listBackgroundTasks()
    const active = (r.tasks || []).find(isActiveTask)
    if (!active) {
      ElMessage.info('当前没有运行中的后台任务')
      openPs()
      return
    }
    await cancelBackgroundTask(active.id)
    ElMessage.success('已停止：' + (active.title || active.id))
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '停止失败')
  }
}

function onComposerKeydown(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return
  if (showMention.value && mentionItems.value.length) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      mentionIndex.value = (mentionIndex.value + 1) % mentionItems.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      mentionIndex.value =
        (mentionIndex.value - 1 + mentionItems.value.length) % mentionItems.value.length
      return
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault()
      const item = mentionItems.value[mentionIndex.value]
      if (item) selectMention(item)
      return
    }
    if (e.key === 'Escape') {
      showMention.value = false
      return
    }
  }
  if (showSlash.value && slashCommands.value.length) {
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault()
      const cmd = slashCommands.value[slashIndex.value]
      if (cmd) draft.value = `/${cmd.name}`
      slashIndex.value = 0
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      slashIndex.value = (slashIndex.value + 1) % slashCommands.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      slashIndex.value =
        (slashIndex.value - 1 + slashCommands.value.length) % slashCommands.value.length
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      const cmd = slashCommands.value[slashIndex.value]
      if (cmd) runSlashCommand(cmd)
      return
    }
    if (e.key === 'Escape') {
      showSlash.value = false
      return
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submitComposer()
  }
}

function onDraftInput() {
  const d = draft.value
  const isSlash = d.startsWith('/') && !d.startsWith('//')
  showSlash.value = isSlash
  slashIndex.value = 0
  detectMention()
}

// 全局（窗口级）聊天快捷键
function onChatGlobalKeydown(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229 || e.defaultPrevented) return
  if (showMemory.value || showWorkspace.value) return
  const mod = e.ctrlKey || e.metaKey
  if (
    mod &&
    [
      ...document.querySelectorAll<HTMLElement>(
        '[aria-modal="true"], dialog[open], .modal-mask, .el-overlay'
      ),
    ].some((element) => element.getClientRects().length > 0)
  )
    return
  const key = e.key.toLowerCase()
  // Ctrl/Cmd+L：聚焦输入框（Ctrl+K 由外壳打开命令面板）
  if (mod && key === 'l' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    nextTick(() => composerEl.value?.focus())
    return
  }
  // Ctrl/Cmd+Enter：发送
  if (mod && e.key === 'Enter' && !e.altKey) {
    e.preventDefault()
    if (draft.value.trim()) submitComposer()
    return
  }
  // Ctrl/Cmd+Alt+N：新会话
  if (mod && e.altKey && key === 'n') {
    e.preventDefault()
    newConversation()
    ElMessage.success('已开始新会话')
    return
  }
  // Esc：关闭最上层弹窗（无弹窗时不处理）
  if (key === 'escape' && !mod) {
    // Element Plus owns its modal stack and returns focus to its trigger.
    // Do not also close a parent hand-built panel under a nested dialog.
    if ([...document.querySelectorAll<HTMLElement>('.el-overlay')].some(element => element.getClientRects().length > 0)) return
    if (showHistory.value) showHistory.value = false
    else if (showReviewModal.value) showReviewModal.value = false
    else if (showPs.value) showPs.value = false
    else if (showGoal.value) showGoal.value = false
    else if (showDiagnostics.value) showDiagnostics.value = false
    else if (showStatus.value) showStatus.value = false
  }
}

function onDocClick(e: MouseEvent) {
  if (composerMenuOpen.value && !skillPickerEl.value?.contains(e.target as Node)) {
    closeComposerMenu()
  }
  if (
    showSkillPicker.value &&
    skillPickerEl.value &&
    !skillPickerEl.value.contains(e.target as Node)
  ) {
    showSkillPicker.value = false
  }
  if (exportMenuFor.value !== null && !(e.target as HTMLElement).closest('.export-menu')) {
    exportMenuFor.value = null
  }
}

interface ExternalPaper {
  title: string
  authors: string
  year: string | number
  url: string
  pdf: string
  source: string
  status: 'idle' | 'importing' | 'done' | 'fail'
}

interface Turn extends ChatRunPause {
  sourceRevisionIds?: string[]
  runRevision?: number
  attemptId?: string | null
  goalId?: string | null
  resumeKind?: 'continue_missing' | 'checkpoint' | 'resources' | null
  runInputs?: RunInput[]
  inputQueueRevision?: number
  processExpanded?: boolean
  expandedToolEvents?: Record<string, boolean>
  approvalNotice?: string
  runtimeConfig?: RuntimeConfiguration | null
  workspaceRevision?: number
  projectId?: string | null
  filePaths?: string[]
  question: string
  executionMode: ExecutionMode
  reviewedPlanId: string | null
  planRunId: string | null
  taskOutcome?: 'planned' | 'completed' | 'partial' | 'blocked'
  incompleteSteps?: { title: string; phase: string; status: string }[]
  answer: string
  evidence: Evidence[]
  reviewProgress?: {
    status?: string
    phase?: string
    message?: string
    text?: string
    completed?: number
    total?: number
    [key: string]: unknown
  } | null
  gate: any
  audit: any
  events: AgentEvent[]
  streaming: boolean
  cancelled: boolean
  refusal: number
  latency: number
  skills: string[]
  error: string
  liveThinking: string
  liveAnswer: string
  thinkingParts: string[]
  externalPapers: ExternalPaper[]
  todo: TodoStep[]
  approvals: ApprovalRequest[]
  subagents: SubagentResult[]
  finalCheck: FinalCheckResult | null
  images: { media_type: string; data: string }[]
  recordId: string | null
  usage: TokenUsage | null
  runId: string | null
  localRun: boolean
  checkpointId: string | null
  checkpointRevision: number
  createdAt: number
  paperId: string | null
  paperTitle?: string | null
  checkpointPhase: string
  checkpointError: string
  resumeAvailable: boolean
  eventCursor?: number
  historyTruncated?: boolean
  interruptedStatus:
    'running_elsewhere' | 'finishing' | 'paused' | 'interrupted' | 'cancelled' | 'failed' | null
}
const turns = ref<Turn[]>([])
const conversationControls = ref<InstanceType<typeof ConversationControls> | null>(null)
const conversationOptionsReady = ref(false)
const activeRuntimeConfig = computed(
  () =>
    turns.value.find(
      (turn) =>
        turn.streaming || ['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || '')
    )?.runtimeConfig || null
)
const chatModelStatus = computed(() => conversationModelStatus({
  running: foregroundRunActive.value,
  runtime: activeRuntimeConfig.value,
  ready: conversationOptionsReady.value,
  options: conversationControls.value?.options || null,
  legacyCredentialError: Boolean(runtimeSettings.value?.credential_errors?.llm_api_key),
  legacyCredentialChecked: runtimeSettings.value !== null,
}))
const executionMode = ref<ExecutionMode>('execute')
const started = computed(() => turns.value.length > 0)
const foregroundSubmitting = ref(false)
let foregroundSubmissionId: string | null = null
const foregroundRunActive = computed(() =>
  turns.value.some(
    (turn) =>
      turn.streaming ||
      Boolean(
        turn.checkpointId &&
        ['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || '')
      )
  )
)
let foregroundPollTimer: ReturnType<typeof setTimeout> | null = null

const conversationId = ref(activeConversationId.value)
const project = ref<Project | null>(null)
let skillsRequest = 0
async function refreshScopedSkills() {
  const request = ++skillsRequest
  const scoped = await listSkills(project.value?.id).catch(() => [])
  if (request !== skillsRequest) return
  skillLib.value = scoped.filter(s => !s.disabled && s.trusted !== false)
  selectedSkills.value = selectedSkills.value.filter(name => skillLib.value.some(s => s.name === name))
}
watch(() => project.value?.id, () => { void refreshScopedSkills() })
const workspaceRevision = ref(0)
const projectLoading = ref(true),
  projectLoadError = ref(''),
  showWorkspace = ref(false)
const selectedFiles = ref<string[]>([]),
  mentionFiles = ref<string[]>([])
let projectRequest = 0
async function refreshProject() {
  const cid = conversationId.value,
    request = ++projectRequest
  projectLoading.value = true
  projectLoadError.value = ''
  project.value = null
  mentionFiles.value = []
  try {
    const data = await getChatProject(cid)
    if (request !== projectRequest || cid !== conversationId.value) return
    project.value = data.project
    workspaceRevision.value = data.workspace_revision
    const document = conversationState(cid)
    await document.ready
    if ((document.value.value.workspace_revision || 0) !== data.workspace_revision)
      await document.refresh()
    if (request !== projectRequest || cid !== conversationId.value) return
    if (data.project?.available) {
      const page = await listProjectFiles(data.project.id)
      if (request === projectRequest)
        mentionFiles.value = page.entries.filter((e) => e.kind === 'file').map((e) => e.path)
    }
  } catch (error) {
    if (request === projectRequest) projectLoadError.value = projectError(error)
  } finally {
    if (request === projectRequest) projectLoading.value = false
  }
}
function boundProject(value: Project) {
  project.value = value
  selectedFiles.value = []
  projectLoadError.value = ''
  void refreshProject()
  notifyConversationsChanged()
}
async function switchedProject() {
  await activeState.value.refresh()
  await refreshProject()
  notifyConversationsChanged()
}
async function initCurrentProject() {
  if (!project.value || projectLoading.value) {
    showWorkspace.value = true
    return
  }
  if (
    foregroundRunActive.value ||
    foregroundSubmitting.value ||
    backgroundSubmitting.value ||
    conversationArchived.value
  )
    return
  try {
    const result = await initProjectRules(project.value.id)
    ElMessage.success(result.created ? '已在当前项目创建 AGENTS.md' : '当前项目已有 AGENTS.md')
    showWorkspace.value = true
  } catch (error) {
    ElMessage.error(projectError(error))
  }
}
async function newProjectChat(value: Project) {
  try {
    const result = await createConversation(undefined, value.id)
    showWorkspace.value = false
    await loadConversation(result.id)
    void router.replace({ path: '/chat', query: { conv: result.id } })
    notifyConversationsChanged()
  } catch (error) {
    ElMessage.error(projectError(error))
  }
}
watch(
  conversationId,
  () => {
    void refreshProject()
  },
  { immediate: true }
)
const archiveState = ref<ConversationArchiveState>({
  id: conversationId.value,
  archived_at: null,
  archive_revision: 0,
})
const conversationArchived = computed(
  () => archiveState.value.id === conversationId.value && Boolean(archiveState.value.archived_at)
)
function applyArchiveState(saved: ConversationArchiveState) {
  if (saved.id !== conversationId.value) return
  if (
    archiveState.value.id !== saved.id ||
    saved.archive_revision >= archiveState.value.archive_revision
  )
    archiveState.value = { ...saved }
}
const currentSummary = ref<Conversation | null>(null)
watch(
  [
    () => currentSummary.value?.project_id,
    () => currentSummary.value?.workspace_revision,
    projectLoading,
  ],
  ([id, revision, loading]) => {
    if (
      currentSummary.value?.id === conversationId.value &&
      id !== undefined &&
      ((revision || 0) > workspaceRevision.value ||
        ((revision || 0) === workspaceRevision.value && id !== (project.value?.id || null))) &&
      !loading &&
      !projectLoadError.value
    )
      void refreshProject()
  }
)
const currentSummaryError = ref(false)
let summaryRequest = 0
async function refreshCurrentSummary() {
  const id = conversationId.value,
    request = ++summaryRequest
  if (!id) return
  try {
    const summary = await getConversationSummary(id)
    if (request !== summaryRequest || id !== conversationId.value || disposed) return
    currentSummary.value = summary
    currentSummaryError.value = false
    applyArchiveState({
      id: summary.id,
      archived_at: summary.archived_at || null,
      archive_revision: summary.archive_revision || 0,
    })
  } catch (error: any) {
    if (request === summaryRequest && !disposed)
      currentSummaryError.value = error?.response?.status !== 404
  }
}
watch(
  [taskState.conversationVersion, conversationId],
  () => {
    void refreshCurrentSummary()
  },
  { immediate: true }
)
const activeState = shallowRef(conversationState(conversationId.value))
const stateLoading = ref(true)
const saveStatus = computed(() => activeState.value.status.value)
const announcedDrafts = new Map<string, boolean>()
watch(saveStatus, (status) => {
  const state = activeState.value.value.value
  const hasDraft = Boolean(state.text.trim() || state.images.length || state.source_revision_ids?.length)
  if (status === 'saved' && announcedDrafts.get(conversationId.value) !== hasDraft) {
    announcedDrafts.set(conversationId.value, hasDraft)
    notifyConversationsChanged()
  }
})
let applyingState = false
let restoringReading = false
function applyDraftState(value: ConversationState) {
  applyingState = true
  draft.value = value.text
  executionMode.value = value.execution_mode === 'plan' ? 'plan' : 'execute'
  paperId.value = value.paper_id
  selectedSkills.value = [...value.skills]
  selectedFiles.value = [...(value.file_paths || [])]
  pendingSourceIds.value = normalizeSourceIds(value.source_revision_ids)
  pendingImages.value = value.images.map((image) => ({
    ...image,
    preview: `data:${image.media_type};base64,${image.data}`,
  }))
  applyingState = false
}
// 从阅读器"对这篇提问"进入时带 ?paper=<id>，该单篇限定需优先于缓存恢复
const urlPaperId = route.query.paper ? String(route.query.paper) : ''
applyDraftState(activeState.value.value.value)
if (urlPaperId) paperId.value = urlPaperId
watch(
  () => activeState.value.restored.value,
  () => {
    applyDraftState(activeState.value.value.value)
    // 状态异步恢复会把 paperId 重置为缓存值，这里重新保留 URL 的单篇限定
    if (urlPaperId) paperId.value = urlPaperId
  },
  { flush: 'sync' }
)
watch(
  [draft, paperId, selectedSkills, pendingImages, executionMode, selectedFiles, pendingSourceIds],
  () => {
    if (applyingState) return
    cacheDraftText(conversationId.value, draft.value)
    activeState.value.update({
      ...activeState.value.value.value,
      text: draft.value,
      paper_id: paperId.value,
      skills: [...selectedSkills.value],
      execution_mode: executionMode.value,
      file_paths: [...selectedFiles.value],
      source_revision_ids: [...pendingSourceIds.value],
      images: pendingImages.value.map(({ media_type, data }) => ({ media_type, data })),
    })
  },
  { deep: true, flush: 'sync' }
)

function attachSource(value: { conversationId: string; revisionId: string }) {
  const state = conversationState(value.conversationId)
  const ids = normalizeSourceIds([...(state.value.value.source_revision_ids || []), value.revisionId])
  if (value.conversationId === conversationId.value) pendingSourceIds.value = ids
  else state.update({ ...state.value.value, source_revision_ids: ids })
  void state.flush()
}
function capturedProjectSource(value: { conversationId: string; path: string }) {
  const state = conversationState(value.conversationId)
  if (value.conversationId === conversationId.value) selectedFiles.value = selectedFiles.value.filter((path) => path !== value.path)
  else state.update({ ...state.value.value, file_paths: (state.value.value.file_paths || []).filter((path) => path !== value.path) })
  void state.flush()
}

async function resolveDraftConflict(keepLocal: boolean) {
  const doc = activeState.value
  await doc.resolveConflict(keepLocal)
  if (doc === activeState.value && !keepLocal) applyDraftState(doc.value.value)
}

function activateConversation(id: string) {
  cancelImageReads()
  if (id !== conversationId.value) {
    archiveState.value = { id, archived_at: null, archive_revision: 0 }
    saveReadingPosition()
    void activeState.value.flush()
    loadedMessageCount = 0
    remoteLoadAttempt = ''
    remoteAnswerError.value = false
  }
  conversationId.value = id
  activeState.value = conversationState(id)
  selectConversation(id)
  applyDraftState(activeState.value.value.value)
}

function newConversation() {
  historyWindow.value=null
  historyPageError.value=''
  conversationEpoch += 1
  restoringReading = false
  clearQueuedSlashCommands()
  detachCurrentRun()
  const id = `c_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
  activateConversation(id)
  stateLoading.value = true
  void activeState.value.ready.then(() => {
    if (!disposed && conversationId.value === id) stateLoading.value = false
  })
  turns.value = []
  clearForegroundPoll()
  scrollToBottom(true)
  const query = { ...route.query, conv: undefined, q: undefined, history: undefined }
  void router.replace({ path: '/chat', query })
  notifyConversationsChanged()
}

function notifyConversationsChanged() {
  window.dispatchEvent(new Event('scholarflow:conversation-updated'))
}

// ---- 历史会话管理 ----
const showHistory = ref(false)
const convSearch = ref('')
const historyScope = ref<'active' | 'archived'>('active')
const historyList = useConversationList({
  scope: historyScope,
  q: convSearch,
  sort: ref('updated_at'),
  enabled: showHistory,
})
const {
  items: convList,
  loading: historyLoading,
  error: historyError,
  cursor: historyCursor,
  loadingMore: historyLoadingMore,
  moreError: historyMoreError,
  total: historyTotal,
} = historyList
const historyDrawer = ref<HTMLElement | null>(null)
let historyReturnFocus: HTMLElement | null = null
const renamingId = ref<string | null>(null)
const renamingText = ref('')
// 切换历史范围（进行中/已归档），同时取消正在进行的重命名
function selectHistoryScope(scope: 'active' | 'archived') {
  historyScope.value = scope
  renamingId.value = null
}
const filteredConv = computed(() => {
  return convList.value
    .filter((c) => Boolean(c.archived_at) === (historyScope.value === 'archived'))
    .sort(
      (a, b) => (b.updated_at || '').localeCompare(a.updated_at || '') || b.id.localeCompare(a.id)
    )
})
function onConversationArchive(event: Event) {
  const saved = (event as CustomEvent<ConversationArchiveState>).detail
  const item = convList.value.find((c) => c.id === saved.id)
  if (item)
    Object.assign(item, saved, saved.archived_at ? { pinned_at: null, pin_position: null } : {})
  applyArchiveState(saved)
}
window.addEventListener('scholarflow:conversation-archive', onConversationArchive)
onUnmounted(() =>
  window.removeEventListener('scholarflow:conversation-archive', onConversationArchive)
)
async function changeArchive(c: {
  id: string
  archived_at?: string | null
  archive_revision?: number
}) {
  const saved = await changeConversationArchive(c.id, !c.archived_at, c.archive_revision || 0)
  if (saved) {
    applyArchiveState(saved)
    ElMessage.success(saved.archived_at ? '已归档会话' : '已恢复会话')
  }
}
const refreshHistory = historyList.refresh
const matchLabels = {
  title: '标题',
  id: '会话编号',
  question: '问题',
  answer: '回答',
  draft: '草稿',
  interrupted: '未完成回答',
}
function matchParts(value: string) {
  const needle = convSearch.value.trim().toLowerCase(),
    at = value.toLowerCase().indexOf(needle)
  return at < 0 || !needle
    ? [value, '', '']
    : [value.slice(0, at), value.slice(at, at + needle.length), value.slice(at + needle.length)]
}
function historyKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    showHistory.value = false
    event.preventDefault()
    return
  }
  if (event.key !== 'Tab' || !historyDrawer.value) return
  const controls = [
    ...historyDrawer.value.querySelectorAll<HTMLElement>(
      'button:not(:disabled):not([tabindex="-1"]), input:not(:disabled)'
    ),
  ]
  const first = controls[0],
    last = controls.at(-1)
  if (event.shiftKey && document.activeElement === first) {
    last?.focus()
    event.preventDefault()
  } else if (!event.shiftKey && document.activeElement === last) {
    first?.focus()
    event.preventDefault()
  }
}
function historyTabKeydown(event: KeyboardEvent) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  historyScope.value =
    event.key === 'Home'
      ? 'active'
      : event.key === 'End'
        ? 'archived'
        : historyScope.value === 'active'
          ? 'archived'
          : 'active'
  void nextTick(() =>
    historyDrawer.value?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus()
  )
}
watch(showHistory, (visible) => {
  if (!visible) historyReturnFocus?.focus()
})
function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}
async function openHistoryPanel(focusSearch: unknown = false) {
  historyReturnFocus = document.activeElement as HTMLElement | null
  showHistory.value = true
  await nextTick()
  historyDrawer.value
    ?.querySelector<HTMLElement>(
      focusSearch === true ? '.hist-search-input' : '[role="tab"][aria-selected="true"]'
    )
    ?.focus()
}
async function loadConversation(id: string, page?:{offset?:number;latest?:boolean}) {
  clearForegroundPoll()
  saveReadingPosition()
  const epoch = ++conversationEpoch
  clearQueuedSlashCommands()
  detachCurrentRun()
  const loadController=new AbortController()
  conversationLoadController=loadController
  stateLoading.value = true
  try {
    // Read runs first: a run may commit its QA while navigation is loading.
    // Reading history first can miss both the unfinished run and its new QA.
    const [foregroundResponse] = await Promise.all([
      listForegroundRuns(id,loadController.signal),
      conversationState(id).ready,
    ])
    const reading=conversationState(id).value.value.reading
    const window={limit:30,...(page?.offset!==undefined?{offset:page.offset}:page?.latest?{}:{anchor:typeof route.query.record==='string'?route.query.record:!reading.following?reading.anchor_id||undefined:undefined})}
    let data = await getConversation(id,loadController.signal,window)
    let foregroundRuns = foregroundResponse.runs
    if (disposed || epoch !== conversationEpoch) return
    if (
      data.messages.some((message) => message.answer) &&
      foregroundRuns.some((run) => run.status === 'running' && !run.display_snapshot?.result?.qa_id)
    ) {
      // A task can finish after the first run list, before history is read.
      // Confirm run identity once; a completed run and its QA commit atomically.
      // Keep the list snapshot as fallback if this brief read cannot complete.
      const controller = new AbortController()
      foregroundHandoffController = controller
      const timeout = setTimeout(() => controller.abort(), 800)
      try {
        const latest = await listForegroundRuns(id, controller.signal)
        const latestIds = new Set(latest.runs.map((run) => run.id))
        if (foregroundRuns.some((run) => run.status === 'running' && !latestIds.has(run.id)))
          data = await getConversation(id, controller.signal,window)
        foregroundRuns = latest.runs
      } catch {
        // The read-only SSE observer will reconcile by qa_id when it reconnects.
      } finally {
        clearTimeout(timeout)
        if (foregroundHandoffController === controller) foregroundHandoffController = null
      }
    }
    if (disposed || epoch !== conversationEpoch) return
    const restored: Turn[] = data.messages
      .filter((m) => m.answer)
      .map((m) =>
        reactive({
          question: m.question,
          goalId: m.goal_id,
          runtimeConfig: m.runtime_config || null,
          executionMode: m.execution_mode || 'execute',
          reviewedPlanId: m.reviewed_plan_id || null,
          planRunId: m.plan_run_id || null,
          answer: m.answer,
          evidence: m.evidence || [],
          gate: null,
          audit: null,
          events: (m.trace || []).filter((e) =>
            ['project_change', 'project_command', 'git_operation'].includes(e.kind)
          ),
          streaming: false,
          cancelled: false,
          refusal: m.refusal,
          latency: m.latency_ms,
          skills: m.skills || [],
          error: '',
          liveThinking: '',
          liveAnswer: '',
          thinkingParts: [],
          externalPapers: [],
          todo: m.plan_steps || [],
          approvals: [],
          subagents: [],
          finalCheck: null,
          images: m.plan_images || [],
          filePaths: m.file_paths || [],
          sourceRevisionIds: normalizeSourceIds(m.source_revision_ids),
          projectId: m.project_id,
          workspaceRevision: m.workspace_revision || 0,
          recordId: m.id,
          usage: m.usage || null,
          runId: null,
          localRun: false,
          checkpointId: null,
          checkpointRevision: 0,
          createdAt: Date.parse(m.created_at || '') || 0,
          paperId: m.paper_id || null,
          checkpointPhase: '',
          checkpointError: '',
          resumeAvailable: false,
          interruptedStatus: null,
        })
      )
    const recovered: Turn[] = foregroundRuns.map((run) =>
      reactive({
        runRevision: run.run_revision,
        attemptId: run.attempt_id,
        goalId: run.goal_id,
        resumeKind: run.resume_kind,
        question: run.question,
        runtimeConfig: run.runtime_config || null,
        executionMode: run.execution_mode || 'execute',
        reviewedPlanId: run.reviewed_plan_id || null,
        planRunId: null,
        answer: '',
        evidence: [],
        gate: null,
        audit: null,
        events: [],
        streaming: false,
        cancelled: run.status === 'cancelled',
        refusal: 0,
        latency: 0,
        skills: run.skills || [],
        error: run.error || '',
        liveThinking: '',
        liveAnswer: run.partial_answer || '',
        thinkingParts: [],
        externalPapers: [],
        todo: [],
        approvals: run.approvals || [],
        subagents: [],
        finalCheck: null,
        images: run.images || [],
        filePaths: run.file_paths || [],
        sourceRevisionIds: normalizeSourceIds(run.source_revision_ids),
        projectId: run.project_id,
        workspaceRevision: run.workspace_revision || 0,
        recordId: null,
        usage: null,
        runId: run.id,
        localRun: false,
        checkpointId: run.id,
        checkpointRevision: run.revision,
        createdAt: Date.parse(run.created_at || '') || 0,
        paperId: run.paper_id,
        checkpointPhase: run.phase || '',
        checkpointError: '',
        resumeAvailable: run.resume_available,
        interruptedStatus:
          run.status === 'running'
            ? 'running_elsewhere'
            : run.status === 'paused'
              ? 'paused'
              : run.status === 'interrupted' ||
                  run.status === 'cancelled' ||
                  run.status === 'failed'
                ? run.status
                : null,
      })
    )
    const historical = data.messages.filter((message) => message.answer)
    restored.forEach((turn, index) => {
      turn.events = []
      restoreChatRunTrace(turn, historical[index].trace || [])
    })
    recovered.forEach((turn, index) => {
      const run = foregroundRuns[index]
      const snapshot = run.display_snapshot
      if (snapshot) applyChatRunEvent(turn, snapshot)
      if (run.status === 'paused' && (run.resource_pause || run.resume_reason || !turn.pauseReason))
        applyChatRunPause(turn, run)
    })
    activateConversation(id)
    applyArchiveState({
      id,
      archived_at: data.archived_at || null,
      archive_revision: data.archive_revision || 0,
    })
    loadedMessageCount = data.history_window?.total ?? data.messages.length
    historyWindow.value=data.history_window||null
    historyPageError.value=''
    turns.value = deduplicateChatRunDisplays([...restored, ...recovered]).sort(
      (a, b) => a.createdAt - b.createdAt
    )
    for (const turn of recovered) {
      if (foregroundRuns.find((run) => run.id === turn.checkpointId)?.status === 'running')
        startRunObserver(turn, id, epoch)
    }
    showHistory.value = false
    const query = { ...route.query, conv: id, q: undefined, history: undefined,record:page?undefined:route.query.record,run:page?undefined:route.query.run }
    if(page){
      const current=activeState.value.value.value
      activeState.value.update({...current,reading:{following:!!page.latest,top:0,anchor_id:null,offset:0}})
    }
    void router.replace({ path: '/chat', query })
    await nextTick()
    await restoreReadingPosition(epoch)
    await focusRequestedTurn()
    scheduleForegroundPoll()
  } catch(error){
    if(loadController.signal.aborted&&(disposed||epoch!==conversationEpoch))return
    throw error
  } finally {
    if(conversationLoadController===loadController)conversationLoadController=null
    if (!disposed && epoch === conversationEpoch) stateLoading.value = false
  }
}
async function loadHistoryPage(offset?:number){
  try{await loadConversation(conversationId.value,offset===undefined?{latest:true}:{offset})}
  catch{historyPageError.value='历史页读取失败；当前已显示内容保留，请重试。'}
}

function clearForegroundPoll() {
  if (foregroundPollTimer !== null) clearTimeout(foregroundPollTimer)
  foregroundPollTimer = null
}

function scheduleForegroundPoll(delay = 2500) {
  clearForegroundPoll()
  if (
    disposed ||
    !turns.value.some(
      (turn) =>
        turn.checkpointId &&
        ['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || '')
    )
  )
    return
  const cid = conversationId.value
  const epoch = conversationEpoch
  foregroundPollTimer = setTimeout(() => {
    void refreshForegroundRuns(cid, epoch)
  }, delay)
}

async function refreshForegroundRuns(cid: string, epoch: number) {
  try {
    const { runs } = await listForegroundRuns(cid)
    if (disposed || cid !== conversationId.value || epoch !== conversationEpoch) return
    const byId = new Map(runs.map((run) => [run.id, run]))
    let completed = false
    for (const turn of turns.value) {
      if (!turn.checkpointId || turn.streaming) continue
      const run = byId.get(turn.checkpointId)
      if (!run) {
        if (['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || ''))
          completed = true
        continue
      }
      turn.checkpointRevision = run.revision
      turn.runRevision = run.run_revision
      if (!run.display_snapshot) turn.attemptId = run.attempt_id
      turn.goalId = run.goal_id
      turn.resumeKind = run.resume_kind
      turn.runtimeConfig = run.runtime_config || turn.runtimeConfig
      turn.checkpointPhase = run.phase || turn.checkpointPhase
      turn.resumeAvailable = run.resume_available
      turn.approvals = run.approvals || []
      turn.checkpointError = ''
      turn.error = run.error || ''
      if (run.display_snapshot) applyRunDisplay(turn, run.display_snapshot)
      else turn.liveAnswer = run.partial_answer || ''
      if (run.status === 'paused') {
        // Older checkpoints may only retain the reason in their public events.
        if (run.resource_pause || run.resume_reason || !turn.pauseReason)
          applyChatRunPause(turn, run)
      }
      if (run.status === 'running') turn.interruptedStatus = 'running_elsewhere'
      else if (run.status === 'paused') {
        turn.interruptedStatus = 'paused'
        turn.cancelled = false
      } else if (
        run.status === 'interrupted' ||
        run.status === 'cancelled' ||
        run.status === 'failed'
      ) {
        turn.interruptedStatus = run.status
        turn.cancelled = run.status === 'cancelled'
      }
      if (run.status === 'running' && !stoppingRun.value) startRunObserver(turn, cid, epoch)
    }
    if (!foregroundRunActive.value) stoppingRun.value = false
    if (completed) {
      await loadConversation(cid)
      return
    }
  } catch {
    // Keep the recovery state visible and retry when the local API is reachable.
  }
  scheduleForegroundPoll()
}

async function retryInterrupted(turn: Turn) {
  if (
    foregroundRunActive.value ||
    !turn.checkpointId ||
    !['interrupted', 'cancelled', 'failed'].includes(turn.interruptedStatus || '')
  )
    return
  try {
    await ElMessageBox.confirm(
      '重新提问会重新执行本轮操作。若上次已调用外部写入工具，可能造成重复操作。',
      '确认重新提问',
      { confirmButtonText: '重新提问', cancelButtonText: '取消', type: 'warning' }
    )
    await ask(turn.question, {
      workspace_revision: turn.workspaceRevision || 0,
      paperId: turn.paperId,
      skills: [...turn.skills],
      images: turn.images.map((image) => ({ ...image })),
      file_paths: turn.filePaths || [],
      source_revision_ids: [...(turn.sourceRevisionIds || [])],
      preserveDraft: true,
      execution_mode: turn.executionMode,
      reviewed_plan_id: turn.reviewedPlanId,
      retry_plan_run_id: turn.reviewedPlanId ? turn.runId : null,
    })
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.message || '重新提问失败')
  }
}

async function continuePaused(turn: Turn) {
  if (
    foregroundRunActive.value ||
    !turn.checkpointId ||
    turn.interruptedStatus !== 'paused' ||
    !turn.resumeAvailable ||
    conversationArchived.value
  )
    return
  await ask(turn.question, { resumeTurn: turn, preserveDraft: true })
}

async function removeInterrupted(turn: Turn) {
  if (
    !turn.checkpointId ||
    !['paused', 'interrupted', 'cancelled', 'failed'].includes(turn.interruptedStatus || '')
  )
    return
  try {
    await dismissForegroundRun(turn.checkpointId)
    turns.value = turns.value.filter((item) => item !== turn)
    scheduleForegroundPoll()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || '移除中断记录失败')
  }
}

async function restoreSavedConversation() {
  const id = conversationId.value
  if (!id) return
  try {
    if (await getConversationSummary(id)) {
      await loadConversation(id)
    }
  } catch (e: any) {
    if (e?.response?.status !== 404) ElMessage.error(e?.message || '恢复最近会话失败')
  }
}

watch(
  () => route.query.conv,
  async (value) => {
    if (!value) return
    const id = String(value)
    if (id === conversationId.value) return
    try {
      await loadConversation(id)
    } catch (e: any) {
      ElMessage.error(e?.response?.data?.detail || e?.message || '恢复会话失败')
    }
  }
)

watch(
  () => route.query.history,
  (value) => {
    if (value !== '1' && value !== 'search') return
    void openHistoryPanel(value === 'search')
    void router.replace({ path: '/chat', query: { ...route.query, history: undefined } })
  }
)
async function switchConversation(c: Conversation) {
  try {
    if (c.id !== conversationId.value) await loadConversation(c.id)
    else showHistory.value = false
    if (c.match_record_id || c.match_run_id) {
      await nextTick()
      const turn = [...(scrollEl.value?.querySelectorAll<HTMLElement>('.turn') || [])].find((el) =>
        c.match_record_id
          ? el.dataset.recordId === c.match_record_id
          : el.dataset.runId === c.match_run_id
      )
      if (turn && scrollEl.value) {
        followingOutput.value = false
        scrollEl.value.scrollTop +=
          turn.getBoundingClientRect().top - scrollEl.value.getBoundingClientRect().top - 16
        turn.focus({ preventScroll: true })
        saveReadingPosition()
      }
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '恢复会话失败')
  }
}
async function resumeConversationArg(query: string) {
  const result = await listConversationPage({ scope: 'all', q: query })
  const normalized = query.trim().toLowerCase()
  const conversation =
    result.conversations.find(
      (item) => item.id.toLowerCase() === normalized || item.title.toLowerCase() === normalized
    ) ||
    result.conversations.find((item) => item.title.toLowerCase().includes(normalized)) ||
    result.conversations[0]
  if (!conversation) {
    ElMessage.warning(`没有找到会话「${query}」`)
    return
  }
  await switchConversation(conversation)
}
async function forkFromTurn(turn: Turn) {
  if (!conversationId.value || !turn.recordId) {
    ElMessage.warning('请先保存该会话，再进行分叉')
    return
  }
  try {
    const res = await forkConversation(conversationId.value, turn.recordId)
    notifyConversationsChanged()
    ElMessage.success(`已分叉为「${res.title}」`)
    await loadConversation(res.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '分叉失败')
  }
}
async function forkWhole(c: Conversation) {
  try {
    const res = await forkConversation(c.id, null)
    notifyConversationsChanged()
    ElMessage.success(`已分叉为「${res.title}」`)
    showHistory.value = false
    await loadConversation(res.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '分叉失败')
  }
}
async function forkCurrentConversation(title?: string) {
  if (!conversationId.value || !turns.value.some((turn) => turn.recordId)) {
    ElMessage.warning('完成至少一轮对话后才能分叉')
    return
  }
  try {
    const res = await forkConversation(conversationId.value, null, title || undefined)
    notifyConversationsChanged()
    ElMessage.success(`已分叉为「${res.title}」`)
    await loadConversation(res.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '分叉失败')
  }
}
async function renameCurrentConversation(titleArg?: string) {
  if (!conversationId.value || !turns.value.some((turn) => turn.recordId)) {
    ElMessage.info('完成至少一轮对话后即可命名会话')
    return
  }
  const id = conversationId.value
  let currentTitle = convList.value.find((conversation) => conversation.id === id)?.title || ''
  if (!currentTitle) {
    try {
      currentTitle = (await getConversationSummary(id)).title || ''
    } catch {
      // 新会话尚未进入历史列表时使用空标题。
    }
  }
  if (titleArg?.trim()) {
    const title = titleArg.trim()
    await renameConversation(id, title)
    notifyConversationsChanged()
    const conversation = convList.value.find((item) => item.id === id)
    if (conversation) conversation.title = title
    ElMessage.success('会话已重命名')
    return
  }
  try {
    const { value } = await ElMessageBox.prompt('为当前会话设置名称', '重命名会话', {
      inputValue: currentTitle,
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValidator: (value) => Boolean(value?.trim()) || '标题不能为空',
    })
    const title = value.trim()
    await renameConversation(id, title)
    notifyConversationsChanged()
    const conversation = convList.value.find((item) => item.id === id)
    if (conversation) conversation.title = title
    ElMessage.success('会话已重命名')
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(e?.message || '重命名失败')
  }
}
function startRename(c: Conversation) {
  renamingId.value = c.id
  renamingText.value = c.title
}
async function confirmRename(c: Conversation) {
  const title = renamingText.value.trim()
  if (!title) {
    renamingId.value = null
    return
  }
  try {
    await renameConversation(c.id, title)
    notifyConversationsChanged()
    c.title = title
  } catch (e: any) {
    ElMessage.error(e?.message || '重命名失败')
  } finally {
    renamingId.value = null
  }
}
async function removeConversation(c: Conversation) {
  try {
    const preview = await previewConversationDeletion(c.id)
    if (!preview.can_delete) {
      await ElMessageBox.alert('此会话仍有未确认清除的 DeepSeek 云文件。请打开会话资料中的云文件管理，核查并清理后再删除。', '云文件需要处理')
      return
    }
    await ElMessageBox.confirm(`将永久删除“${c.title}”的会话、附件固定原文、运行记录、笔记和研究成果，共 ${preview.total_records} 条记录。已入文献库的文献、其他分支、项目文件和操作日志、已有备份仍保留。此操作不能撤销。`, '删除会话及关联资料', {
      confirmButtonText: '永久删除', cancelButtonText: '保留会话', type: 'warning',
    })
    await deleteConversation(c.id)
    forgetConversation(c.id)
    notifyConversationsChanged()
    convList.value = convList.value.filter((x) => x.id !== c.id)
    if (c.id === conversationId.value) {
      newConversation()
    }
    ElMessage.success('已删除会话')
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return
    const detail = e?.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : detail?.message || e?.message || '删除失败')
  }
}

async function importExternal(turn: Turn, paper: ExternalPaper) {
  paper.status = 'importing'
  try {
    const res = await importPaperFromUrl(paper.pdf, paper.title)
    if (res?.status === 'failed') throw new Error(res.error || '解析失败')
    paper.status = 'done'
    await loadPaperChoices()
    if (res?.duplicate) {
      ElMessage.info('该文献已在库中，未重复入库')
    } else if (res?.version_duplicate) {
      const incoming = res.incoming_version == null ? '未知' : `v${res.incoming_version}`
      const existing = res.existing_version == null ? '未知' : `v${res.existing_version}`
      const recommended =
        res.recommend === 'incoming' ? `新导入的 ${incoming}` : `库中已有的 ${existing}`
      await ElMessageBox.alert(
        '检测到同一论文的不同版本，两版记录均已保留、未自动替换。\n' +
          `· 库中已有版本：${existing}\n` +
          `· 本次导入版本：${incoming}\n` +
          `· 建议采用：${recommended}\n` +
          '可在文献库中手动选择保留哪一版。',
        '发现同论文不同版本',
        { confirmButtonText: '知道了', type: 'info' }
      )
    } else {
      ElMessage.success('已入库并完成解析，可在本地检索')
    }
  } catch (err: any) {
    paper.status = 'fail'
    ElMessage.error(err?.message || '入库失败')
  }
}

const decidingApprovals = ref(new Set<string>())
async function handleApproval(
  turn: Turn,
  apv: ApprovalRequest,
  decision: 'approve' | 'deny',
  scope: 'once' | 'session'
) {
  if (decidingApprovals.value.has(apv.approval_id)) return
  decidingApprovals.value.add(apv.approval_id)
  try {
    await decideApproval(apv.approval_id, decision, scope)
    // 审批卡从待处理列表移除（结果由 approval_result 事件展示）
    turn.approvals = turn.approvals.filter((a) => a.approval_id !== apv.approval_id)
    if (decision === 'deny') ElMessage.info('已拒绝该操作')
  } catch (err: any) {
    if (err?.response?.status === 404) {
      turn.approvals = turn.approvals.filter((a) => a.approval_id !== apv.approval_id)
      ElMessage.info('该审批已结束')
    } else ElMessage.error(err?.response?.data?.detail || err?.message || '审批提交失败')
  } finally {
    decidingApprovals.value.delete(apv.approval_id)
  }
}

let abortCtrl: AbortController | null = null
let activeRunId: string | null = null
let activeRunDetach: (() => void) | null = null
const stoppingRun = ref(false)
const runObservers = new Map<string, { controller: AbortController; turn: Turn }>()

function applyRunDisplay(turn: Turn, event: AgentEvent) {
  const applied = applyChatRunEvent(turn, event)
  if (!applied.applied) return applied
  if (event.kind === 'snapshot') turn.checkpointError = ''
  if (event.kind === 'approval_result' && event.decision === 'superseded')
    turn.approvalNotice = '权限已更新，原审批作废，正在按当前权限重新检查。'
  if (applied.answerReceived) {
    if (turn.recordId) turns.value = deduplicateChatRunDisplays(turns.value, turn)
    notifyConversationsChanged()
  }
  scrollToBottom()
  return applied
}

function startRunObserver(turn: Turn, cid: string, epoch: number) {
  const runId = turn.checkpointId || turn.runId
  const isCurrentView = () =>
    !disposed &&
    cid === conversationId.value &&
    epoch === conversationEpoch &&
    turns.value.includes(turn)
  if (!runId || !isCurrentView() || (turn.localRun && turn.streaming) || stoppingRun.value) return
  const previous = runObservers.get(runId)
  if (previous?.turn === turn) return
  previous?.controller.abort()
  const observer = { controller: new AbortController(), turn }
  runObservers.set(runId, observer)
  turn.streaming = true
  turn.localRun = false
  let terminal = false
  void (async () => {
    try {
      await observeChatRun(
        runId,
        cid,
        turn.eventCursor || 0,
        (event) => {
          if (!isCurrentView() || observer.controller.signal.aborted) return
          if (event.run_id && event.run_id !== runId) return
          if (event.conversation_id && event.conversation_id !== cid) return
          const result = applyRunDisplay(turn, event)
          if (result.applied && result.terminal) terminal = true
        },
        observer.controller.signal
      )
    } catch (error: any) {
      if (!isCurrentView() || observer.controller.signal.aborted) return
      if (error?.status === 410) {
        terminal = true
        turn.interruptedStatus = 'interrupted'
        turn.error = error?.message || '该会话已删除'
      } else if (error?.status === 404) {
        turn.interruptedStatus = 'finishing'
      } else {
        turn.checkpointError = '实时连接中断，正在重新同步服务端进度。'
      }
    } finally {
      if (runObservers.get(runId) === observer) runObservers.delete(runId)
      if (!isCurrentView() || observer.controller.signal.aborted) return
      turn.streaming = false
      if (
        !terminal &&
        !['paused', 'cancelled', 'failed', 'interrupted'].includes(turn.interruptedStatus || '')
      )
        turn.interruptedStatus =
          turn.interruptedStatus === 'finishing' ? 'finishing' : 'running_elsewhere'
      if (!foregroundRunActive.value) stoppingRun.value = false
      if (!terminal || turn.interruptedStatus === 'finishing') scheduleForegroundPoll(800)
      if (!foregroundRunActive.value && queuedSlashCommands.value.length)
        await flushQueuedSlashCommands()
    }
  })()
}

function detachCurrentRun() {
  // Navigation only detaches this view. The server owns the running task.
  conversationLoadController?.abort()
  conversationLoadController=null
  foregroundHandoffController?.abort()
  foregroundHandoffController = null
  activeRunDetach?.()
  for (const observer of runObservers.values()) observer.controller.abort()
  runObservers.clear()
  activeRunDetach = null
  abortCtrl = null
  activeRunId = null
  stoppingRun.value = false
  foregroundSubmitting.value = false
  foregroundSubmissionId = null
}

const QUICK_CARDS = [
  {
    title: '总结一篇文献',
    desc: '提炼研究问题、方法与结论',
    prompt: '请总结这篇文献的研究问题、核心方法、主要结论与不足。',
    color: '#0a59f7',
    icon: FileText,
  },
  {
    title: '起草综述',
    desc: '按主题组织多篇文献',
    prompt: '请基于库内文献，按研究主题梳理该领域的研究脉络，起草一份文献综述。',
    color: '#7c3aed',
    icon: PenLine,
  },
  {
    title: '解释图表',
    desc: '解读图表与实验数据',
    prompt: '请解释论文中的关键图表：横纵坐标含义、数据趋势与结论。',
    color: '#0d9488',
    icon: BarChart3,
  },
  {
    title: '制定研究计划',
    desc: '拆解研究步骤与时间线',
    prompt: '请帮我把研究课题拆解为可执行的研究步骤，并给出时间安排。',
    color: '#d97706',
    icon: ListChecks,
  },
  {
    title: '翻译润色',
    desc: '学术英语翻译与润色',
    prompt: '请将下面的中文内容翻译为地道的学术英语，并说明润色要点。',
    color: '#2563eb',
    icon: Languages,
  },
  {
    title: '头脑风暴',
    desc: '发散研究方向与选题',
    prompt: '请围绕该领域的研究空白，头脑风暴 5 个有创新性且可行的研究选题。',
    color: '#db2777',
    icon: Lightbulb,
  },
]

const KIND_LABEL: Record<string, string> = {
  project_change: '文件变更',
  git_operation: 'Git 操作',
  execution_mode: '模式',
  policy_block: '操作限制',
  status: '状态',
  plan: '规划',
  step: '推理',
  tool_call: '工具调用',
  tool_result: '工具结果',
  critic: '引用闸门',
  retry: '重写',
  refuse: '拒答',
  answer: '最终答案',
  done: '完成',
  skill_attach: '技能加载',
  skill_loaded: '技能版本',
  thinking: '思考',
  commentary: '进展',
  error: '错误',
  thinking_delta: '思考',
  answer_delta: '生成',
  memory: '记忆',
  audit: '审计',
  goal: '目标',
  hook: '钩子',
  paper_scope: '检索范围',
}
function paperTitleOf(turn: Pick<Turn, 'paperId' | 'paperTitle'>): string {
  return (
    turn.paperTitle ||
    (turn.paperId
      ? papers.value.find((paper) => paper.id === turn.paperId)?.title || turn.paperId
      : '')
  )
}
function toolLabel(name: string): string {
  if (name === 'read_project_command_output') return '回查命令输出'
  if (name === 'run_project_command') return '运行项目命令'
  if (name === 'git_project_write') return '修改项目 Git'
  if (name === 'edit_project_file') return '修改项目文件'
  if (name === 'read_conversation_history') return '回查历史原文'
  if (name === 'retrieve_evidence') return '检索原文证据'
  if (name === 'list_papers') return '列出文献'
  if (name === 'list_skills') return '查看技能库'
  if (name === 'load_skill') return '加载技能'
  return name
}

function verdictLabel(v: string): string {
  return (
    (
      {
        accept: '建议接收',
        minor_revision: '小修',
        major_revision: '大修',
        undetermined: '待定',
      } as Record<string, string>
    )[v] || v
  )
}
function severityLabel(s: string): string {
  return ({ high: '严重', mid: '中等', low: '轻微' } as Record<string, string>)[s] || s
}

async function executePlan(turn: Turn) {
  if (
    turn.executionMode !== 'plan' ||
    !turn.todo.length ||
    !turn.recordId ||
    turn.planRunId ||
    turn.refusal ||
    foregroundRunActive.value ||
    foregroundSubmitting.value
  )
    return
  await ask('执行已审阅计划：' + turn.question, {
    workspace_revision: turn.workspaceRevision || 0,
    file_paths: turn.filePaths || [],
    source_revision_ids: [...(turn.sourceRevisionIds || [])],
    execution_mode: 'execute',
    reviewed_plan_id: turn.recordId,
    preserveDraft: true,
    paperId: turn.paperId,
    skills: [...turn.skills],
    images: [...turn.images],
  })
}

async function refreshPlanClaims(cid: string) {
  try {
    const ids=turns.value.filter(turn=>turn.executionMode==='plan'&&turn.recordId).map(turn=>turn.recordId!)
    if(!ids.length)return
    const batches:ReturnType<typeof getPlanClaims>[]=[]
    for(let index=0;index<ids.length;index+=100)batches.push(getPlanClaims(cid,ids.slice(index,index+100)))
    const results=await Promise.all(batches)
    if (cid !== conversationId.value) return
    const claims = new Map<string,string|null>(results.flatMap(result=>result.items.map(m=>[m.id,m.plan_run_id] as const)))
    for (const turn of turns.value)
      if (turn.recordId && claims.has(turn.recordId)) turn.planRunId = claims.get(turn.recordId)!
  } catch {
    /* A failed refresh leaves the original plan visible. Admission still guards duplicates. */
  }
}

function readComposerMode(raw: string) {
  const command = raw.match(/^\/(plan|execute)(?:\s+([\s\S]*))?$/i)
  if (!command) return raw
  executionMode.value = command[1].toLowerCase() === 'plan' ? 'plan' : 'execute'
  showSlash.value = false
  if (!command[2]?.trim()) draft.value = ''
  return command[2]?.trim() || ''
}

function localComposerCommand() {
  const name = draft.value
    .trim()
    .match(/^\/([^\s]+)/)?.[1]
    ?.toLowerCase()
  return name && name !== 'plan' && name !== 'execute'
    ? commandRegistry.value.find((command) => command.name === name)
    : undefined
}

// User inputs belong to the server-side run, never to this view's SSE observer.
const runInputMode = ref<RunInputKind>('steer')
const runInputError = ref('')
const runInputSyncError = ref('')
const runInputLoading = ref(false)
const inputBusyRuns = reactive(new Set<string>())
const conversationInputQueues = reactive(new Map<string, RunInputQueue>())
const locatedQueuedChildren = new Set<string>()
watch(conversationId, () => {
  conversationInputQueues.clear()
  locatedQueuedChildren.clear()
})
async function durableInputIdentity(signature: string) {
  const bytes = new TextEncoder().encode(signature)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  const hash = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('')
  const key = `scholarflow:pending-run-input:${hash}`
  const inputId = localStorage.getItem(key) || `input_${crypto.randomUUID()}`
  localStorage.setItem(key, inputId)
  return { key, inputId }
}
const inputTargetTurn = computed(() => {
  const active = turns.value.find(
    (turn) =>
      turn.streaming || ['running_elsewhere', 'finishing'].includes(turn.interruptedStatus || '')
  )
  return active || [...turns.value].reverse().find((turn) => turn.checkpointId)
})
const inputTargetId = computed(
  () => inputTargetTurn.value?.checkpointId || inputTargetTurn.value?.runId || ''
)
const runInputSubmitting = computed(() => inputBusyRuns.has(inputTargetId.value))
const visibleRunInputs = computed(() => {
  const newest = new Map<string, RunInput>()
  for (const input of [
    ...[...conversationInputQueues.values()].flatMap((queue) => queue.inputs),
    ...turns.value.flatMap((turn) => turn.runInputs || []),
  ]) {
    const previous = newest.get(input.input_id)
    if (!previous || previous.revision <= input.revision) newest.set(input.input_id, input)
  }
  return [...newest.values()].sort(
    (a, b) => a.created_at.localeCompare(b.created_at) || a.input_id.localeCompare(b.input_id)
  )
})

function applyInputQueue(turn: Turn | undefined, queue: RunInputQueue) {
  if (queue.revision < (conversationInputQueues.get(queue.run_id)?.revision || 0)) return
  conversationInputQueues.set(queue.run_id, queue)
  if (turn && queue.revision >= (turn.inputQueueRevision || 0)) {
    turn.runInputs = queue.inputs
    turn.inputQueueRevision = queue.revision
  }
}

watch(
  [inputTargetId, conversationId],
  ([, cid], _previous, onCleanup) => {
    runInputMode.value = 'steer'
    runInputError.value = ''
    runInputSyncError.value = ''
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    onCleanup(() => {
      live = false
      if (timer) clearTimeout(timer)
    })
    if (!cid) {
      runInputLoading.value = false
      return
    }
    async function sync() {
      if (!live || disposed) return
      runInputLoading.value = true
      try {
        const result = await listConversationRunInputs(cid)
        if (!live || disposed) return
        for (const queue of result.queues)
          applyInputQueue(turns.value.find((turn) => turn.checkpointId === queue.run_id), queue)
        runInputSyncError.value = ''
        const child = result.queues.flatMap((queue) => queue.inputs).find((input) =>
          input.status === 'dispatched' && input.child_run_id &&
          !locatedQueuedChildren.has(input.child_run_id) &&
          !turns.value.some((turn) => turn.checkpointId === input.child_run_id)
        )
        if (child?.child_run_id && !stateLoading.value && !foregroundSubmitting.value) {
          locatedQueuedChildren.add(child.child_run_id)
          await loadConversation(cid)
        }
      } catch (error: any) {
        if (live && !disposed && foregroundRunActive.value)
          runInputSyncError.value =
            error?.response?.data?.detail || error?.message || '补充消息同步失败，可重试'
      } finally {
        if (live && !disposed) {
          runInputLoading.value = false
          if (
            foregroundRunActive.value ||
            visibleRunInputs.value.some((input) => input.status === 'queued')
          )
            timer = setTimeout(sync, 1500)
        }
      }
    }
    void sync()
  },
  { immediate: true }
)

function runInputStatus(input: RunInput) {
  if (input.status === 'applied')
    return `已生效${input.goal_revision ? ` · 目标修订 ${input.goal_revision}` : ''}`
  if (input.status === 'cancelled') return '已撤销'
  if (input.status === 'rejected') return input.reason || '未应用'
  if (input.status === 'dispatched') return input.reason.includes('待核实')
    ? input.reason : '已自动提交下一轮，可查看执行进度'
  return input.reason || (input.kind === 'next_turn'
    ? '已排队；前一轮完整完成后自动开始' : '已接收，等待安全边界生效')
}

async function locateQueuedRun(input: RunInput) {
  if (!input.child_run_id || stateLoading.value) return
  const cid = conversationId.value
  locatedQueuedChildren.add(input.child_run_id)
  await loadConversation(cid)
  if (cid !== conversationId.value) return
  await nextTick()
  const turn = turns.value.find((entry) => entry.checkpointId === input.child_run_id ||
    (entry.question === input.content && entry.createdAt >= Date.parse(input.created_at)))
  const index = turn ? turns.value.indexOf(turn) : -1
  if (index >= 0) scrollEl.value?.querySelectorAll<HTMLElement>('.turn')[index]
    ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

async function cancelQueuedRunInput(input: RunInput) {
  const turn = turns.value.find((entry) => entry.checkpointId === input.run_id)
  if (inputBusyRuns.has(input.run_id)) return
  const cid = conversationId.value
  const epoch = conversationEpoch
  inputBusyRuns.add(input.run_id)
  try {
    const queue = await cancelRunInput(
      input.run_id,
      input.input_id,
      cid,
      conversationInputQueues.get(input.run_id)?.revision || turn?.inputQueueRevision || 0
    )
    if (cid === conversationId.value && epoch === conversationEpoch) applyInputQueue(turn, queue)
  } catch (error: any) {
    if (cid === conversationId.value && epoch === conversationEpoch)
      runInputError.value =
        error?.response?.data?.detail || error?.message || '撤销失败，请同步后重试'
  } finally {
    inputBusyRuns.delete(input.run_id)
  }
}

async function submitComposer() {
  if (!foregroundRunActive.value) {
    await ask()
    return
  }
  const command = localComposerCommand()
  if (command) {
    await runSlashCommand(command)
    return
  }
  const turn = inputTargetTurn.value
  const runId = inputTargetId.value
  const content = draft.value.trim()
  if (
    !turn ||
    !runId ||
    !content ||
    inputBusyRuns.has(runId) ||
    stoppingRun.value ||
    runInputLoading.value ||
    stateLoading.value ||
    foregroundSubmitting.value ||
    conversationArchived.value
  )
    return
  if (sourceUploading.value) {
    runInputError.value = '资料正在保存，请稍候再发送。'
    return
  }
  if (pendingImages.value.length || selectedFiles.value.length) {
    runInputError.value = '运行中补充支持文字和已保存的资料；图片请保留至下一轮，项目文件可先在会话资料中保存固定版本。'
    return
  }
  const cid = conversationId.value
  const epoch = conversationEpoch
  const workspace = workspaceRevision.value
  const kind = runInputMode.value
  const submittedDraft = draft.value
  const submittedSources = [...pendingSourceIds.value]
  const submittedState = activeState.value
  const signature = JSON.stringify([runId, cid, workspace, kind, content, submittedSources])
  inputBusyRuns.add(runId)
  runInputError.value = ''
  const isCurrent = () => !disposed && cid === conversationId.value && epoch === conversationEpoch
  try {
    const { inputId, key } = await durableInputIdentity(signature)
    const queue = await submitRunInput(runId, {
      input_id: inputId,
      conversation_id: cid,
      kind,
      content,
      expected_revision: turn.inputQueueRevision || 0,
      workspace_revision: workspace,
      source_revision_ids: submittedSources,
    })
    if (isCurrent()) {
      applyInputQueue(turn, queue)
      if (draft.value === submittedDraft) draft.value = ''
      if (JSON.stringify(pendingSourceIds.value) === JSON.stringify(submittedSources)) pendingSourceIds.value = []
    } else if (submittedState.value.value.text === submittedDraft) {
      submittedState.update({ ...submittedState.value.value, text: '',
        source_revision_ids: JSON.stringify(submittedState.value.value.source_revision_ids || []) === JSON.stringify(submittedSources) ? [] : submittedState.value.value.source_revision_ids })
      cacheDraftText(cid, '')
    }
    await nextTick()
    await submittedState.flush()
    if (submittedState.status.value === 'saved') localStorage.removeItem(key)
  } catch (error: any) {
    if (isCurrent()) {
      runInputError.value =
        error?.response?.data?.detail ||
        error?.message ||
        '消息尚未确认接收，输入已保留；可重试同一条消息'
      if (error?.response?.status === 409) {
        try {
          applyInputQueue(turn, await listRunInputs(runId, cid))
        } catch {
          /* retain draft */
        }
      }
    }
  } finally {
    inputBusyRuns.delete(runId)
  }
}

async function ask(
  text?: string,
  context?: ExecutionOptions & {
    paperId?: string | null
    skills?: string[]
    images?: { media_type: string; data: string }[]
    preserveDraft?: boolean
    resumeTurn?: Turn
  }
) {
  const resumeTurn = context?.resumeTurn
  const command = !context && text === undefined ? localComposerCommand() : undefined
  if (command) {
    await runSlashCommand(command)
    return
  }
  if (!resumeTurn && (projectLoading.value || projectLoadError.value)) {
    ElMessage.warning('项目上下文尚未就绪，请重试载入')
    return
  }
  if (
    !resumeTurn &&
    (activeState.value.value.value.workspace_revision || 0) !== workspaceRevision.value
  ) {
    ElMessage.warning('工作目录已切换，请先处理草稿冲突')
    return
  }
  if (!resumeTurn && context && (context.workspace_revision ?? 0) !== workspaceRevision.value) {
    ElMessage.warning('此轮属于切换前的工作目录，请核对原内容后重新提问或规划')
    return
  }
  if (
    stateLoading.value ||
    conversationArchived.value ||
    conversationArchive.pending.has(conversationId.value)
  )
    return
  if (readingImages.value || sourceUploading.value) {
    ElMessage.info('附件正在保存，请稍候')
    return
  }
  if (foregroundRunActive.value) {
    ElMessage.info('当前会话仍有前台轮次运行，请等待结束')
    return
  }
  if (foregroundSubmitting.value || (abortCtrl && turns.value.some((t) => t.streaming))) return
  const optionsConversation = conversationId.value
  const optionsEpoch = conversationEpoch
  if (!resumeTurn && !(await conversationControls.value?.ensureReady())) {
    if (optionsConversation === conversationId.value)
      ElMessage.warning('会话模型或权限尚未就绪，请先处理输入区提示')
    return
  }
  if (
    disposed ||
    optionsConversation !== conversationId.value ||
    optionsEpoch !== conversationEpoch
  )
    return
  if (foregroundSubmitting.value || foregroundRunActive.value) return
  const raw = resumeTurn
    ? resumeTurn.question
    : context
      ? (text ?? draft.value).trim()
      : readComposerMode((text ?? draft.value).trim())
  if (!raw) return
  const { clean, skills: dollarSkills } = extractDollarSkills(raw)
  const q = clean || raw // 仅输入 $技能 时回退为原文，避免空问题
  const attached = resumeTurn
    ? [...resumeTurn.skills]
    : context?.skills
      ? [...context.skills]
      : [...new Set([...selectedSkills.value, ...dollarSkills])]
  const images = resumeTurn
    ? resumeTurn.images.map((image) => ({ ...image }))
    : context?.images
      ? context.images.map((image) => ({ ...image }))
      : pendingImages.value.map((p) => ({ media_type: p.media_type, data: p.data }))
  const selectedPaperId = resumeTurn
    ? resumeTurn.paperId
    : context?.paperId === undefined
      ? paperId.value
      : context.paperId
  const options: ExecutionOptions = resumeTurn
    ? {
        workspace_revision: resumeTurn.workspaceRevision,
        project_id: resumeTurn.projectId,
        file_paths: resumeTurn.filePaths || [],
        source_revision_ids: [...(resumeTurn.sourceRevisionIds || [])],
        execution_mode: resumeTurn.executionMode,
        reviewed_plan_id: resumeTurn.reviewedPlanId,
        retry_plan_run_id: resumeTurn.reviewedPlanId ? resumeTurn.runId : null,
      }
    : {
        workspace_revision: workspaceRevision.value,
        project_id: project.value?.id || null,
        file_paths: context?.file_paths || [...selectedFiles.value],
        source_revision_ids: normalizeSourceIds(context?.source_revision_ids || pendingSourceIds.value),
        execution_mode: context?.execution_mode || executionMode.value,
        reviewed_plan_id: context?.reviewed_plan_id,
        retry_plan_run_id: context?.retry_plan_run_id,
      }
  const runId = resumeTurn?.checkpointId || `chat_${crypto.randomUUID()}`
  const cid = conversationId.value
  const epoch = conversationEpoch
  const submittedState = activeState.value
  const draftPayload = (value: ConversationState) =>
    JSON.stringify({ text: value.text, images: value.images, file_paths: value.file_paths, source_revision_ids: value.source_revision_ids })
  const submittedDraft = draftPayload(submittedState.value.value)
  const isCurrentView = () =>
    !disposed && epoch === conversationEpoch && cid === conversationId.value
  foregroundSubmissionId = runId
  foregroundSubmitting.value = true
  const releaseSubmission = () => {
    if (foregroundSubmissionId === runId) {
      foregroundSubmitting.value = false
      foregroundSubmissionId = null
    }
  }
  let resumeReceipt: ReturnType<typeof durableResumeRequest> | undefined
  if (resumeTurn) {
    try {
      const latest = (await listForegroundRuns(cid)).runs.find((run) => run.id === runId)
      if (!latest) throw new Error('执行记录已更新，请重新载入后核对恢复状态')
      resumeTurn.runRevision = latest.run_revision
      resumeReceipt = durableResumeRequest(localStorage, cid, runId, latest.run_revision,
        () => `resume_${crypto.randomUUID()}`)
    } catch (error: any) {
      if (isCurrentView()) ElMessage.error(error?.message || '无法保存恢复请求，尚未启动执行')
      releaseSubmission()
      return
    }
    if (!isCurrentView()) {
      releaseSubmission()
      return
    }
  }
  let createdRuntime: RuntimeConfiguration | null = null
  if (!resumeTurn) {
    try {
      const admitted = await createForegroundRun({
        id: runId,
        conversation_id: cid,
        question: q,
        paper_id: selectedPaperId,
        skills: attached,
        images,
        ...options,
      })
      createdRuntime = admitted.runtime_config || null
      conversationControls.value?.noteRunAdmission(createdRuntime)
      if (isCurrentView()) void conversationControls.value?.syncOptions()
    } catch (error: any) {
      if (isCurrentView()) {
        ElMessage.error(
          error?.response?.data?.detail || error?.message || '前台检查点创建失败，输入已保留'
        )
        if (context?.reviewed_plan_id) void refreshPlanClaims(cid)
      }
      if (foregroundSubmissionId === runId) {
        foregroundSubmitting.value = false
        foregroundSubmissionId = null
      }
      return
    }
  }
  if (foregroundSubmissionId === runId) {
    foregroundSubmitting.value = false
    foregroundSubmissionId = null
  }
  if (
    !resumeTurn &&
    !context?.preserveDraft &&
    draftPayload(submittedState.value.value) === submittedDraft
  ) {
    if (isCurrentView()) {
      draft.value = ''
      pendingImages.value = []
      selectedFiles.value = []
      pendingSourceIds.value = []
      void submittedState.flush()
    } else {
      // Clear only the submitted conversation's unchanged draft, never a new view's input.
      submittedState.update({ ...submittedState.value.value, text: '', images: [], file_paths: [], source_revision_ids: [] })
      cacheDraftText(cid, '')
      void submittedState.flush()
    }
  }
  const preservePreviousAnswer = Boolean(resumeTurn?.recordId && resumeTurn.answer)
  const turn: Turn =
    (resumeTurn && preservePreviousAnswer ? reactive({
      ...resumeTurn, answer: '', recordId: null, evidence: [], reviewProgress: null, gate: null, audit: null,
      liveAnswer: '', liveThinking: '', thinkingParts: [], events: [], todo: [], approvals: [],
      subagents: [], externalPapers: [], finalCheck: null, usage: null, eventCursor: 0,
      taskOutcome: undefined, incompleteSteps: [], attemptId: null, createdAt: Date.now(),
    }) : resumeTurn) ||
    reactive({
      question: q,
      runtimeConfig: createdRuntime,
      answer: '',
      evidence: [],
      reviewProgress: null,
      gate: null,
      audit: null,
      executionMode: options.execution_mode || 'execute',
      reviewedPlanId: options.reviewed_plan_id || null,
      planRunId: null,
      events: [],
      streaming: true,
      cancelled: false,
      refusal: 0,
      latency: 0,
      skills: attached,
      error: '',
      liveThinking: '',
      liveAnswer: '',
      thinkingParts: [],
      externalPapers: [],
      todo: [],
      approvals: [],
      subagents: [],
      finalCheck: null,
      images,
      recordId: null,
      usage: null,
      runId,
      localRun: true,
      checkpointId: runId,
      checkpointRevision: 1,
      createdAt: Date.now(),
      paperId: selectedPaperId,
      paperTitle: selectedPaperId
        ? papers.value.find((paper) => paper.id === selectedPaperId)?.title || selectedPaperId
        : null,
      filePaths: options.file_paths,
      sourceRevisionIds: options.source_revision_ids,
      projectId: options.project_id,
      workspaceRevision: options.workspace_revision,
      checkpointPhase: 'setup',
      checkpointError: '',
      resumeAvailable: false,
      interruptedStatus: null,
    })
  if (resumeTurn) {
    if (preservePreviousAnswer) {
      // Keep the committed QA as its own historical display while the new
      // attempt produces a new answer. Its source/evidence remain untouched.
      resumeTurn.checkpointId = null
      resumeTurn.runId = null
      resumeTurn.resumeAvailable = false
      resumeTurn.interruptedStatus = null
      resumeTurn.streaming = false
      resumeTurn.localRun = false
      if (isCurrentView()) turns.value.push(turn)
    }
    turn.streaming = true
    turn.localRun = true
    turn.cancelled = false
    turn.error = ''
    turn.resumeAvailable = false
    turn.interruptedStatus = null
    turn.pauseReason = ''
    turn.pauseMessage = ''
    turn.resourcePause = null
    turn.checkpointError = ''
    turn.liveThinking = ''
    turn.eventCursor = 0
    turn.attemptId = null
  } else if (isCurrentView()) {
    turns.value.push(turn)
  }
  if (options.reviewed_plan_id && isCurrentView()) {
    const source = turns.value.find((item) => item.recordId === options.reviewed_plan_id)
    if (source) source.planRunId = runId
  }
  const controller = new AbortController()
  let connected = false
  let detached = !isCurrentView()
  const detachObserver = () => {
    detached = true
    turn.streaming = false
    turn.localRun = false
    turn.interruptedStatus = 'running_elsewhere'
    // Let an already submitted POST reach the server before detaching its reader.
    if (connected) controller.abort()
  }
  if (isCurrentView()) {
    activeRunId = runId
    stoppingRun.value = false
    abortCtrl = controller
    activeRunDetach = detachObserver
  }
  let receivedAnswer = false
  await nextTick()
  if (isCurrentView()) scrollToBottom(true)
  else detachObserver()

  try {
    await streamChat(
      q,
      selectedPaperId,
      (ev) => {
        if (detached || !isCurrentView()) return
        if (stoppingRun.value && (ev.kind === 'thinking_delta' || ev.kind === 'answer_delta'))
          return
        const applied = applyRunDisplay(turn, ev)
        if (applied.answerReceived) receivedAnswer = true
      },
      controller.signal,
      attached.length ? attached : undefined,
      cid,
      images.length ? images : undefined,
      runId,
      options,
      resumeTurn ? { resume_checkpoint: true, additional_steps: 6, ...resumeReceipt?.request } : {},
      () => {
        connected = true
        if (resumeReceipt) localStorage.removeItem(resumeReceipt.key)
        if (detached || !isCurrentView()) detachObserver()
      }
    )
  } catch (err: any) {
    if (resumeReceipt && [400, 403, 404, 409, 410, 422].includes(err?.status))
      localStorage.removeItem(resumeReceipt.key)
    if (!detached && isCurrentView() && err?.name !== 'AbortError') {
      if (err?.status) {
        turn.error = err?.message || '问答请求失败'
        ElMessage.error(turn.error)
      } else {
        turn.checkpointError = '实时连接中断，正在重新同步服务端进度。'
      }
    }
  } finally {
    turn.streaming = false
    if (!receivedAnswer && !['paused', 'cancelled'].includes(turn.interruptedStatus || '')) {
      turn.localRun = false
      turn.interruptedStatus = 'running_elsewhere'
    }
    if (activeRunDetach === detachObserver) {
      activeRunDetach = null
      if (abortCtrl === controller) abortCtrl = null
      if (activeRunId === runId) {
        activeRunId = null
        stoppingRun.value = false
      }
    }
    if (isCurrentView()) scrollToBottom()
    if (isCurrentView() && (!receivedAnswer || turn.interruptedStatus === 'running_elsewhere')) {
      if (!['paused', 'cancelled', 'failed', 'interrupted'].includes(turn.interruptedStatus || ''))
        startRunObserver(turn, cid, epoch)
      scheduleForegroundPoll(800)
    }
    if (
      !disposed &&
      epoch === conversationEpoch &&
      !turns.value.some((item) => item.streaming) &&
      queuedSlashCommands.value.length
    ) {
      await flushQueuedSlashCommands()
    }
  }
}

async function stop() {
  if (stoppingRun.value) return
  const turn = turns.value.find(
    (item) =>
      item.streaming ||
      (item.checkpointId &&
        ['running_elsewhere', 'finishing'].includes(item.interruptedStatus || ''))
  )
  const runId = turn?.runId || turn?.checkpointId
  if (!runId) return
  const cid = conversationId.value
  const epoch = conversationEpoch
  stoppingRun.value = true
  if (turn) {
    turn.checkpointPhase = 'stopping'
    turn.checkpointError = ''
  }
  try {
    await cancelChatRun(runId)
    if (
      !disposed &&
      cid === conversationId.value &&
      epoch === conversationEpoch &&
      !turn?.streaming
    )
      scheduleForegroundPoll(0)
  } catch (e: any) {
    if (!disposed && cid === conversationId.value && epoch === conversationEpoch) {
      stoppingRun.value = false
      ElMessage.error(e?.message || '停止失败，请重试')
    }
  }
}

const backgroundSubmitting = ref(false)
async function askInBackground() {
  const command = localComposerCommand()
  if (command) {
    await runSlashCommand(command)
    return
  }
  if (projectLoading.value || projectLoadError.value) {
    ElMessage.warning('项目上下文尚未就绪，请重试载入')
    return
  }
  if (
    stateLoading.value ||
    backgroundSubmitting.value ||
    conversationArchived.value ||
    conversationArchive.pending.has(conversationId.value)
  )
    return
  if (pendingImages.value.length || readingImages.value || sourceUploading.value) {
    ElMessage.info('图片附件请使用前台发送')
    return
  }
  const raw = readComposerMode(draft.value.trim())
  if (!raw) return
  const readyConversation = conversationId.value
  const readyEpoch = conversationEpoch
  if (!(await conversationControls.value?.ensureReady())) {
    if (readyConversation === conversationId.value)
      ElMessage.warning('会话模型或权限尚未就绪，请先处理输入区提示')
    return
  }
  if (disposed || readyConversation !== conversationId.value || readyEpoch !== conversationEpoch)
    return
  const { clean, skills: dollarSkills } = extractDollarSkills(raw)
  const q = clean || raw
  const attached = [...new Set([...selectedSkills.value, ...dollarSkills])]
  const epoch = conversationEpoch
  const submittedDraft = draft.value
  const submittedSourceIds = [...pendingSourceIds.value]
  backgroundSubmitting.value = true
  try {
    await startBackgroundTask(q, paperId.value, attached, conversationId.value, {
      execution_mode: executionMode.value,
      project_id: project.value?.id || null,
      file_paths: [...selectedFiles.value],
      source_revision_ids: submittedSourceIds,
      workspace_revision: activeState.value.value.value.workspace_revision || 0,
    })
    if (!disposed && epoch === conversationEpoch && draft.value === submittedDraft && JSON.stringify(pendingSourceIds.value) === JSON.stringify(submittedSourceIds)) {
      draft.value = ''
      selectedFiles.value = []
      pendingSourceIds.value = []
      void activeState.value.flush()
      void router.push('/tasks')
    }
    ElMessage.success('已在后台开始，可在「后台任务」页查看')
  } catch (e: any) {
    ElMessage.error(e?.message || '发起失败')
  } finally {
    backgroundSubmitting.value = false
  }
}

const scrollEl = ref<HTMLElement | null>(null)
const answerReadingEnabled = computed(
  () =>
    !stateLoading.value &&
    !showWorkspace.value &&
    !showMemory.value &&
    !showHistory.value &&
    !showReviewModal.value &&
    !showStatus.value &&
    !showDiagnostics.value &&
    !showGoal.value &&
    !showPs.value
)
const readState = useAnswerReadState(scrollEl, conversationId, answerReadingEnabled)
const readReceiptError = readState.error
let loadedMessageCount = 0
let remoteLoadAttempt = ''
const remoteAnswerError = ref(false)
function refreshRemoteAnswers() {
  if (!answerReadingEnabled.value || foregroundRunActive.value) return
  const cid = conversationId.value
  const epoch = conversationEpoch
  const count = currentSummary.value?.id === cid ? currentSummary.value.message_count : 0
  remoteLoadAttempt = `${cid}:${count}`
  remoteAnswerError.value = false
  void loadConversation(cid).catch(() => {
    if (!disposed && cid === conversationId.value && conversationEpoch === epoch + 1)
      remoteAnswerError.value = true
  })
}
watch(
  () => [
    currentSummary.value?.id === conversationId.value ? currentSummary.value.message_count : 0,
    foregroundRunActive.value,
    !answerReadingEnabled.value,
  ],
  ([count, active, blocked]) => {
    if (
      disposed ||
      active ||
      blocked ||
      Number(count) <=
        Math.max(loadedMessageCount, turns.value.filter((turn) => turn.recordId).length)
    )
      return
    if (remoteLoadAttempt !== `${conversationId.value}:${count}`) refreshRemoteAnswers()
  }
)
const followingOutput = ref(true)
let scrollFrame: number | null = null
let lastScrollTop = 0
function saveReadingPosition() {
  const el = scrollEl.value
  if (!el || !started.value || restoringReading || stateLoading.value) return
  const top = el.getBoundingClientRect().top
  const anchor = [...el.querySelectorAll<HTMLElement>('.turn[data-record-id]')].find(
    (node) => node.getBoundingClientRect().bottom > top
  )
  const reading = {
    following: followingOutput.value,
    top: el.scrollTop,
    anchor_id: anchor?.dataset.recordId || null,
    offset: anchor ? top - anchor.getBoundingClientRect().top : 0,
  }
  const previous = activeState.value.value.value.reading
  if (
    previous.following === reading.following &&
    previous.top === reading.top &&
    previous.anchor_id === reading.anchor_id &&
    previous.offset === reading.offset
  )
    return
  activeState.value.update({ ...activeState.value.value.value, reading })
}
async function restoreReadingPosition(epoch: number) {
  if (scrollFrame !== null) {
    cancelAnimationFrame(scrollFrame)
    scrollFrame = null
  }
  restoringReading = true
  await nextTick()
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  if (disposed || epoch !== conversationEpoch) return
  const el = scrollEl.value
  const reading = activeState.value.value.value.reading
  followingOutput.value = reading.following
  if (el) {
    const anchor = [...el.querySelectorAll<HTMLElement>('.turn[data-record-id]')].find(
      (node) => node.dataset.recordId === reading.anchor_id
    )
    el.scrollTop = reading.following
      ? el.scrollHeight
      : anchor
        ? el.scrollTop +
          anchor.getBoundingClientRect().top -
          el.getBoundingClientRect().top +
          reading.offset
        : reading.top
    lastScrollTop = el.scrollTop
  }
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  if (epoch === conversationEpoch) restoringReading = false
}
function onChatScroll() {
  const el = scrollEl.value
  if (!el || restoringReading || stateLoading.value) return
  if (el.scrollHeight - el.scrollTop - el.clientHeight <= 64) followingOutput.value = true
  else if (el.scrollTop < lastScrollTop - 1) followingOutput.value = false
  // A delayed scroll event can arrive after output grew below the viewport.
  // Only an upward move should detach a reader who was following output.
  lastScrollTop = el.scrollTop
  saveReadingPosition()
}
function scrollToBottom(force = false) {
  const epoch = conversationEpoch
  if (force) {
    followingOutput.value = true
    lastScrollTop = 0
  }
  void nextTick(() => {
    if (disposed || epoch !== conversationEpoch || scrollFrame !== null) return
    scrollFrame = requestAnimationFrame(() => {
      scrollFrame = null
      if (epoch === conversationEpoch && scrollEl.value && followingOutput.value) {
        scrollEl.value.scrollTop = scrollEl.value.scrollHeight
        lastScrollTop = scrollEl.value.scrollTop
        saveReadingPosition()
      }
    })
  })
}

function openEvidence(turn: Turn, eid?: string) {
  if (!eid) return
  const ev = turn.evidence.find((e) => e.id === eid)
  if (!ev) return
  if (ev.revision_id) {
    void router.push(sourceReaderLocation(ev.revision_id, conversationId.value, ev.locator_id))
    return
  }
  localStorage.setItem('scholarflow:last-paper', ev.paper_id)
  router.push({
    path: `/reader/${ev.paper_id}`,
    query: { page: ev.page, bbox: ev.bbox ? ev.bbox.join(',') : undefined },
  })
}

function onCardClick(prompt: string) {
  draft.value = prompt
}

// Display actual answer evidence, including sources selected from earlier turns.
const sourcesOpen = ref(started.value)
const sourceTab = ref<'sources' | 'outputs' | 'execution'>('sources')
const inspectedTurnIndex = ref<number | null>(null)
const selectedEvidence = ref('')
const inspectedTurn = computed(
  () => turns.value[inspectedTurnIndex.value ?? turns.value.length - 1]
)
const sourceItems = computed(() => {
  const evidence = inspectedTurn.value?.evidence || []
  if (evidence.length) {
    const grouped = new Map<string, Evidence>()
    for (const ev of evidence) {
      const key = evidenceSourceKey(ev)
      if (!grouped.has(key) || ev.id === selectedEvidence.value)
        grouped.set(key, ev)
    }
    return [...grouped.values()]
  }
  const scope = paperId.value ? papers.value.filter((p) => p.id === paperId.value) : papers.value
  return scope.map(
    (p) =>
      ({
        id: '',
        paper_id: p.id,
        paper_title: p.title,
        page: 1,
        quote: p.abstract || '',
        bbox: null,
        score: 0,
        chunk_id: '',
      }) as Evidence
  )
})
function selectEvidence(turn: Turn, eid?: string) {
  if (!eid) return
  inspectedTurnIndex.value = turns.value.indexOf(turn)
  selectedEvidence.value = eid
  sourceTab.value = 'sources'
  sourcesOpen.value = true
  const evidence = turn.evidence.find((item) => item.id === eid)
  if (evidence?.revision_id || evidence?.paper_id) openEvidence(turn, eid)
}
function openSource(ev: Evidence) {
  if (ev.id && inspectedTurn.value) openEvidence(inspectedTurn.value, ev.id)
  else if (ev.revision_id) void router.push(sourceReaderLocation(ev.revision_id, conversationId.value, ev.locator_id))
  else router.push(`/reader/${ev.paper_id}`)
}
function toggleSources() {
  sourcesOpen.value = !sourcesOpen.value
}
function publishResearchContext() {
  window.dispatchEvent(
    new CustomEvent('scholarflow:research-context', {
      detail: {
        title:
          currentSummary.value?.title || (started.value ? turns.value[0]?.question : '新建研究'),
        projectName: project.value?.name || '',
        sourceCount: sourceItems.value.length,
        sourcesOpen: sourcesOpen.value,
        modelStatus: chatModelStatus.value,
      },
    })
  )
}
watch(started, (value) => {
  sourcesOpen.value = value
  inspectedTurnIndex.value = null
})
watch(conversationId, () => {
  inspectedTurnIndex.value = null
  selectedEvidence.value = ''
  composerMenuOpen.value = false
  showSkillPicker.value = false
})
watch([sourcesOpen, sourceItems, currentSummary, project, started, chatModelStatus], publishResearchContext)

async function copyAnswer(turn: Turn) {
  try {
    await navigator.clipboard.writeText(turn.answer)
    ElMessage.success('答案已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动选择文本')
  }
}

function copyLatestAnswer() {
  const latest = [...turns.value].reverse().find((turn) => turn.answer)
  if (!latest) {
    ElMessage.info('当前会话还没有完整回答')
    return
  }
  return copyAnswer(latest)
}

async function regenerate(turn: Turn) {
  if (!turn.recordId || !conversationId.value) return
  revisionTarget.value = {conversationId: conversationId.value, recordId: turn.recordId, question: turn.question}
}

const revisionTarget = ref<{conversationId:string;recordId:string;question:string}|null>(null)
async function revisionCreated(revision: HistoryRevision) {
  revisionTarget.value = null
  notifyConversationsChanged()
  await loadConversation(revision.branch_conversation_id)
  ElMessage.success('修订分支草稿已保存，确认输入后发送')
}

async function focusRequestedTurn() {
  if (String(route.query.conv || '') !== conversationId.value) return
  const record = String(route.query.record || ''), run = String(route.query.run || '')
  if (!record && !run) return
  await nextTick()
  const turn = [...(scrollEl.value?.querySelectorAll<HTMLElement>('.turn') || [])].find(el => record ? el.dataset.recordId === record : el.dataset.runId === run)
  if (turn && scrollEl.value) {
    followingOutput.value = false
    scrollEl.value.scrollTop += turn.getBoundingClientRect().top - scrollEl.value.getBoundingClientRect().top - 16
    turn.focus({preventScroll:true})
    saveReadingPosition()
  }
}
watch(() => [route.query.record, route.query.run], () => { void focusRequestedTurn() })

async function runFinalCheck(turn: Turn) {
  if (!turn.answer) {
    ElMessage.warning('还没有可检查的答案')
    return
  }
  turn.finalCheck = null
  try {
    turn.finalCheck = await finalCheck(turn.answer, turn.evidence)
    if (turn.finalCheck.status === 'passed') ElMessage.success('定稿自检通过')
    else ElMessage.warning('自检发现待处理项，请查看报告')
  } catch (e: any) {
    ElMessage.error(e?.message || '自检失败')
  }
}

onMounted(async () => {
  void getSettings().then((settings) => { if (!disposed) runtimeSettings.value = settings }).catch(() => {})
  window.addEventListener('scholarflow:toggle-sources', toggleSources)
  window.addEventListener('scholarflow:open-memory', openConversationMemory)
  publishResearchContext()
  window.addEventListener('pagehide', detachCurrentRun)
  window.addEventListener('scholarflow:new-conversation', newConversation)
  try {
    await loadPaperChoices()
    try {
      await refreshScopedSkills()
    } catch {
      skillLib.value = []
    }
    try {
      mcpServers.value = await listMcpServers()
    } catch {
      mcpServers.value = []
    }
    document.addEventListener('click', onDocClick)
    window.addEventListener('keydown', onChatGlobalKeydown)
    if (route.query.history === '1' || route.query.history === 'search') {
      const focusSearch = route.query.history === 'search'
      void router.replace({ path: '/chat', query: { ...route.query, history: undefined } })
      await restoreSavedConversation()
      await openHistoryPanel(focusSearch)
      return
    }
    if (route.query.conv) {
      try {
        await loadConversation(String(route.query.conv))
      } catch (e: any) {
        ElMessage.error(e?.response?.data?.detail || e?.message || '恢复会话失败')
      }
      return
    }
    const q = route.query.q as string | undefined
    if (q) {
      stateLoading.value = false
      const query = { ...route.query, q: undefined }
      void router.replace({ path: '/chat', query })
      await ask(q)
    } else {
      await restoreSavedConversation()
    }
  } finally {
    if (!disposed) stateLoading.value = false
  }
})

onUnmounted(() => {
  window.removeEventListener('scholarflow:toggle-sources', toggleSources)
  window.removeEventListener('scholarflow:open-memory', openConversationMemory)
  window.removeEventListener('scholarflow:new-conversation', newConversation)
  clearForegroundPoll()
  saveReadingPosition()
  void flushDesktopState()
  cancelImageReads()
  disposed = true
  conversationEpoch += 1
  clearQueuedSlashCommands()
  detachCurrentRun()
  if (scrollFrame !== null) cancelAnimationFrame(scrollFrame)
  window.removeEventListener('pagehide', detachCurrentRun)
  document.removeEventListener('click', onDocClick)
  window.removeEventListener('keydown', onChatGlobalKeydown)
})
</script>

<template>
  <div
    class="chat-page"
    :class="{
      'terminal-open': showTerminal && project,
      'is-new': !started,
      'sources-open': sourcesOpen,
    }"
  >
    <div v-if="currentSummaryError" class="conversation-state-warning" role="status">
      会话状态更新失败，显示上次结果。<button type="button" @click="refreshCurrentSummary">
        重试会话状态
      </button>
    </div>
    <div v-if="conversationArchived" class="archive-notice" role="status">
      <Archive :size="16" />
      <span>已归档</span>
      <button
        type="button"
        :disabled="conversationArchive.pending.has(conversationId)"
        @click="changeArchive(archiveState)"
      >
        <ArchiveRestore :size="14" />恢复会话
      </button>
    </div>
    <HistoryRevisionBanner :conversation-id="conversationId" />
    <HistoryRevisionDialog :target="revisionTarget" @close="revisionTarget = null" @created="revisionCreated" />
    <!-- 消息区 / 欢迎页 -->
    <div class="chat-scroll-wrap">
      <div ref="scrollEl" class="chat-scroll" @scroll.passive="onChatScroll">
        <OnboardingGuide :conversation-id="conversationId" :force="route.query.guide === '1'" @configure="conversationControls?.openSetup()" @sources="conversationSources?.show()" @compose="value => { draft = draft.trim() ? draft : value }" @diagnostics="openDiagnostics" @close="router.replace({path:'/chat',query:{...route.query,guide:undefined}})" />
        <!-- 欢迎页 -->
        <div v-if="!started" class="welcome">
          <div class="welcome-inner">
            <h1 class="welcome-title">
              <img class="welcome-mark" src="/app-icon.png" alt="研序" />
              <span>{{ project?.name || '新的研究' }}</span>
            </h1>
            <p class="welcome-subtitle">检索文献、梳理证据，继续你的研究。</p>

            <div class="quick-section">
              <span class="quick-section-title">快速开始</span>
              <div class="quick-cards">
                <button
                  v-for="card in QUICK_CARDS.slice(0, 3)"
                  :key="card.title"
                  type="button"
                  class="quick-card"
                  @click="onCardClick(card.prompt)"
                >
                  <span class="quick-card-icon">
                    <component :is="card.icon" :size="18" :stroke-width="1.9" />
                  </span>
                  <span class="quick-card-body">
                    <span class="quick-card-title">{{ card.title }}</span>
                    <span class="quick-card-desc">{{ card.desc }}</span>
                  </span>
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- 消息流 -->
        <div v-else class="messages">
          <nav v-if="historyWindow && (historyWindow.previous_offset!==null || historyWindow.next_offset!==null)" class="history-pagination" aria-label="会话历史分页">
            <button :disabled="stateLoading || historyWindow.previous_offset===null" @click="historyWindow.previous_offset!==null && loadHistoryPage(historyWindow.previous_offset)">查看更早历史</button>
            <span>已载入第 {{historyWindow.offset+1}}–{{Math.min(historyWindow.offset+(historyWindow.limit||30),historyWindow.total)}} 轮，共 {{historyWindow.total}} 轮；运行中输出继续显示。</span>
            <button :disabled="stateLoading || historyWindow.next_offset===null" @click="historyWindow.next_offset!==null && loadHistoryPage(historyWindow.next_offset)">查看较新历史</button>
            <button v-if="historyWindow.next_offset!==null" :disabled="stateLoading" @click="loadHistoryPage()">查看最新历史</button>
          </nav>
          <p v-if="historyPageError" role="alert">{{historyPageError}}</p>
          <div
            v-for="(turn, ti) in turns"
            :key="turn.recordId || turn.checkpointId || ti"
            class="turn"
            tabindex="-1"
            :data-run-id="turn.checkpointId || undefined"
            :data-record-id="turn.recordId || undefined"
          >
            <!-- 用户问题 -->
            <div class="user-row">
              <div class="user-col">
                <div v-if="turn.images?.length" class="turn-images">
                  <img
                    v-for="(im, ii) in turn.images"
                    :key="ii"
                    :src="`data:${im.media_type};base64,${im.data}`"
                    alt="已发送图片"
                  />
                </div>
                <div class="user-bubble">{{ turn.question }}</div>
                <div v-if="turn.paperId" class="turn-paper-scope" :title="turn.paperId">
                  <BookOpen :size="11" />
                  <span>本轮检索范围：{{ paperTitleOf(turn) }}</span>
                </div>
                <div v-if="turn.filePaths?.length" class="turn-skills" aria-label="本轮引用文件">
                  <span v-for="file in turn.filePaths" :key="file" class="turn-file" :title="file"
                    ><FileText :size="11" /><span>{{ file }}</span></span
                  >
                </div>
                <SourceReferences :ids="turn.sourceRevisionIds || []" :conversation-id="conversationId" />
                <div v-if="turn.skills.length" class="turn-skills">
                  <span v-for="sn in turn.skills" :key="sn" class="turn-skill">
                    <Wand2 :size="10" :stroke-width="2.2" /> {{ skillNameOf(sn) }}
                  </span>
                </div>
              </div>
            </div>

            <!-- One compact disclosure for actual thinking, plans and tool work. -->
            <div v-if="processPresentation(turn).visible" class="run-process">
              <button
                type="button"
                class="thinking-toggle process-toggle run-process-toggle"
                :class="{ active: processPresentation(turn).active }"
                :disabled="!processPresentation(turn).expandable"
                :aria-expanded="
                  processPresentation(turn).expandable ? Boolean(turn.processExpanded) : undefined
                "
                :aria-controls="
                  processPresentation(turn).expandable ? `run-process-${ti}` : undefined
                "
                @click="toggleProcess(turn)"
              >
                <Brain
                  :size="14"
                  :stroke-width="1.8"
                  :class="{ pulse: processPresentation(turn).active }"
                />
                <span class="thinking-label">{{ processPresentation(turn).label }}</span>
                <span v-if="processPresentation(turn).summary" class="run-process-summary">{{
                  processPresentation(turn).summary
                }}</span>
                <ChevronRight
                  v-if="processPresentation(turn).expandable"
                  :size="13"
                  :class="{ open: turn.processExpanded }"
                  class="thinking-chev"
                />
              </button>
              <div
                v-if="turn.processExpanded && processPresentation(turn).expandable"
                :id="`run-process-${ti}`"
                class="process-body run-process-details"
              >
                <section v-if="thinkingOf(turn).length" class="thinking-body">
                  <div
                    v-for="(part, i) in thinkingOf(turn)"
                    :key="i"
                    class="process-thought"
                    :data-thought-kind="part.kind"
                  >
                    <h4 class="run-process-heading">思考</h4>
                    <p class="thinking-p">{{ part.text }}</p>
                  </div>
                </section>
                <p v-if="turn.historyTruncated" class="process-truncated" role="status">
                  较早的部分过程记录已截断，以下显示保留的输出。
                </p>

                <!-- 待办清单（任务规划） -->
                <div
                  v-if="turn.todo.length && (turn.executionMode !== 'plan' || !turn.answer)"
                  class="todo"
                >
                  <div class="todo-head">
                    <ListChecks :size="14" />
                    {{ turn.executionMode === 'plan' ? '待执行步骤' : '任务计划' }}
                    <span class="todo-count"
                      >{{ turn.todo.filter((s) => s.status === 'done').length }}/{{
                        turn.todo.length
                      }}</span
                    >
                  </div>
                  <div class="todo-list">
                    <div v-for="(s, si) in turn.todo" :key="si" class="todo-item" :class="s.status">
                      <span class="todo-mark">
                        <svg
                          v-if="s.status === 'done'"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="3"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                        >
                          <path d="M20 6L9 17l-5-5" />
                        </svg>
                        <span v-else-if="s.status === 'in_progress'" class="todo-spin"></span>
                      </span>
                      <span class="todo-title">{{ s.title }}</span>
                    </div>
                  </div>
                </div>

                <div v-if="turnChanges(turn).length" class="turn-file-changes">
                  <button
                    v-for="change in turnChanges(turn)"
                    :key="String(change.change_id)"
                    type="button"
                    @click="fileReviewId = String(change.change_id)"
                  >
                    <FileText :size="14" />查看差异 · {{ change.path }}
                  </button>
                </div>
                <div
                  v-if="commandEvents(turn.events).length"
                  class="turn-file-changes turn-commands"
                >
                  <button
                    v-for="command in commandEvents(turn.events)"
                    :key="command.command_id"
                    type="button"
                    @click="commandReviewId = command.command_id"
                  >
                    <TerminalSquare :size="14" /><span>命令与输出 · {{ command.summary }}</span
                    ><small v-if="command.status !== 'proposed'"
                      >{{ commandStatus(command.status)
                      }}<template v-if="command.exit_code != null">
                        · {{ command.exit_code }}</template
                      ></small
                    >
                  </button>
                </div>
                <!-- 审批卡（高风险工具） -->
                <div
                  v-if="gitOperationEvents(turn.events).length"
                  class="turn-file-changes turn-git-operations"
                >
                  <button
                    v-for="operation in gitOperationEvents(turn.events)"
                    :key="operation.operation_id"
                    type="button"
                    @click="
                      gitReviewTarget = {
                        id: operation.operation_id,
                        project_id: operation.project_id,
                      }
                    "
                  >
                    <GitFork :size="14" /><span
                      >查看 Git 操作 · {{ gitActionLabels[operation.action] }}</span
                    ><small>{{ gitStatusLabels[operation.status] }}</small>
                  </button>
                </div>
                <!-- 子 agent 并行结果 -->
                <div v-if="turn.subagents.length" class="subagents">
                  <div class="subagents-head">
                    <ListChecks :size="15" />
                    <span>{{ turn.subagents.length }} 个专题子 agent 已并行完成</span>
                  </div>
                  <div class="subagent-grid">
                    <div v-for="(sa, sai) in turn.subagents" :key="sai" class="subagent-card">
                      <div class="subagent-topic">{{ sa.topic }}</div>
                      <div class="subagent-findings">{{ sa.findings }}</div>
                    </div>
                  </div>
                </div>

                <section v-if="processEvents(turn.events).length" class="process-log">
                  <h4 class="run-process-heading">工具与处理记录</h4>
                  <div class="process-log-body">
                    <el-timeline>
                      <el-timeline-item
                        v-for="(ev, i) in processEvents(turn.events)"
                        :key="i"
                        :timestamp="ev.time"
                        placement="top"
                      >
                        <div class="process-line">
                          <span class="kind-badge">{{ KIND_LABEL[ev.kind] || ev.kind }}</span>
                          <template v-if="ev.kind === 'tool_call'"
                            >调用「{{ toolLabel(ev.name) }}」：{{
                              ev.arguments?.query || ''
                            }}</template
                          >
                          <template
                            v-else-if="ev.kind === 'critic' && ev.gate?.status !== 'not_applicable'"
                          >
                            闸门{{ ev.gate?.passed ? '通过' : '未通过' }}：{{
                              ev.gate?.marked_claims
                            }}/{{ ev.gate?.total_claims }} 论断有证据
                          </template>
                          <template v-else>{{ ev.text }}</template>
                        </div>
                      </el-timeline-item>
                    </el-timeline>
                  </div>
                </section>
              </div>
            </div>

            <TaskRequirements v-if="turn.goalId" :key="turn.goalId" :goal-id="turn.goalId" :conversation-id="conversationId" :read-only="conversationArchived" />

            <div v-if="publicProcessEntries(turn.events).length" class="public-progress">
              <template v-for="entry in publicProcessEntries(turn.events)" :key="entry.id">
                <div
                  v-if="entry.type === 'commentary'"
                  class="public-progress-item public-commentary"
                  :data-event-seq="entry.event.seq"
                >
                  <MarkdownAnswer :text="entry.text" />
                </div>
                <ProviderResearchReport v-else-if="entry.type === 'provider_report'" :report="entry.event.report" />
                <div
                  v-else
                  class="public-progress-item public-tool"
                  :data-event-seq="entry.event.seq"
                >
                  <button
                    type="button"
                    class="public-tool-toggle"
                    :aria-expanded="Boolean(turn.expandedToolEvents?.[entry.id])"
                    :aria-controls="`public-tool-${ti}-${entry.id}`"
                    @click="toggleToolDetails(turn, entry.id)"
                  >
                    <ChevronRight
                      :size="13"
                      :class="{ open: turn.expandedToolEvents?.[entry.id] }"
                    />
                    <TerminalSquare :size="13" />
                    <span class="public-tool-name">{{
                      toolLabel(entry.event.name || entry.event.tool_name || '工具调用')
                    }}</span>
                    <span v-if="publicToolArgument(entry.event)" class="public-tool-argument">{{
                      publicToolArgument(entry.event)
                    }}</span>
                    <span class="public-tool-status">{{
                      entry.result
                        ? '已返回'
                        : turn.streaming || turn.interruptedStatus === 'running_elsewhere'
                          ? '运行中…'
                          : '调用记录'
                    }}</span>
                  </button>
                  <div
                    v-if="turn.expandedToolEvents?.[entry.id]"
                    :id="`public-tool-${ti}-${entry.id}`"
                    class="public-tool-details"
                  >
                    <template v-if="entry.event.arguments">
                      <h4 class="run-process-heading">调用参数</h4>
                      <pre>{{ JSON.stringify(entry.event.arguments, null, 2) }}</pre>
                    </template>
                    <template v-if="entry.result">
                      <h4 class="run-process-heading">返回记录</h4>
                      <pre>{{
                        entry.result.preview ||
                        entry.result.text ||
                        JSON.stringify(entry.result, null, 2)
                      }}</pre>
                    </template>
                    <p v-else class="public-tool-pending">
                      {{
                        turn.streaming || turn.interruptedStatus === 'running_elsewhere'
                          ? '等待工具返回结果。'
                          : '当前记录中没有工具返回结果。'
                      }}
                    </p>
                  </div>
                </div>
              </template>
            </div>

            <p v-if="turn.approvalNotice" class="approval-notice" role="status">
              {{ turn.approvalNotice }}
            </p>
            <div v-if="turn.approvals.length" class="approval">
              <div v-for="apv in turn.approvals" :key="apv.approval_id" class="approval-card">
                <div class="approval-top">
                  <ShieldCheck :size="16" />
                  <span>需要批准：{{ toolLabel(apv.tool_name) }}</span>
                  <span class="approval-risk">{{
                    apv.risk === 'guarded' ? '需确认' : apv.risk
                  }}</span>
                </div>
                <div class="approval-reason">{{ apv.reason }}</div>
                <pre class="approval-arguments">{{
                  apv.tool_name === 'git_project_write'
                    ? gitApprovalSummary(apv.arguments)
                    : JSON.stringify(apv.arguments, null, 2)
                }}</pre>
                <div class="approval-actions">
                  <button
                    v-if="apv.tool_name === 'edit_project_file' && apv.arguments.change_id"
                    type="button"
                    class="apv-btn once"
                    @click="fileReviewId = String(apv.arguments.change_id)"
                  >
                    查看文件差异
                  </button>
                  <button
                    v-if="apv.tool_name === 'run_project_command' && apv.arguments.command_id"
                    type="button"
                    class="apv-btn once"
                    @click="commandReviewId = String(apv.arguments.command_id)"
                  >
                    查看命令记录
                  </button>
                  <button
                    v-if="
                      apv.tool_name === 'git_project_write' &&
                      apv.arguments.operation_id &&
                      apv.arguments.project_id
                    "
                    type="button"
                    class="apv-btn once"
                    @click="
                      gitReviewTarget = {
                        id: String(apv.arguments.operation_id),
                        project_id: String(apv.arguments.project_id),
                      }
                    "
                  >
                    查看 Git 审阅
                  </button>
                  <button
                    type="button"
                    class="apv-btn deny"
                    :disabled="decidingApprovals.has(apv.approval_id)"
                    @click="handleApproval(turn, apv, 'deny', 'once')"
                  >
                    拒绝
                  </button>
                  <button
                    type="button"
                    class="apv-btn once"
                    :disabled="decidingApprovals.has(apv.approval_id)"
                    @click="handleApproval(turn, apv, 'approve', 'once')"
                  >
                    批准一次
                  </button>
                  <button
                    v-if="conversationControls?.options?.approval_mode !== 'always'"
                    type="button"
                    class="apv-btn session"
                    :disabled="decidingApprovals.has(apv.approval_id)"
                    @click="handleApproval(turn, apv, 'approve', 'session')"
                  >
                    本次运行允许
                  </button>
                </div>
              </div>
            </div>

            <!-- 错误提示 -->
            <div v-if="turn.error && !turn.answer" class="turn-error">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="8" x2="12" y2="12" />
                <line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <span>{{ turn.error }}</span>
            </div>

            <div
              v-if="turn.checkpointId && turn.interruptedStatus"
              class="foreground-recovery"
              role="status"
            >
              <div class="foreground-recovery-copy">
                <strong>{{ foregroundStatusLabel(turn) }}</strong>
                <span v-if="turn.checkpointError">检查点保存失败：{{ turn.checkpointError }}</span>
                <span v-else-if="turn.interruptedStatus === 'paused' && turn.pauseMessage">{{ turn.pauseMessage }}</span>
                <span
                  v-else-if="
                    turn.checkpointPhase &&
                    ['running_elsewhere', 'finishing', 'paused', 'interrupted'].includes(
                      turn.interruptedStatus
                    )
                  "
                  >阶段：{{ checkpointPhaseLabel(turn.checkpointPhase) }}</span
                >
              </div>
              <div class="foreground-recovery-actions">
                <button
                  v-if="turn.interruptedStatus === 'paused' && turn.resumeAvailable"
                  type="button"
                  :disabled="foregroundSubmitting || foregroundRunActive || conversationArchived"
                  @click="continuePaused(turn)"
                >
                  {{ turn.pauseReason === 'step_limit' ? '继续当前任务（+6轮）' : '继续当前任务' }}
                </button>
                <button
                  v-if="['interrupted', 'cancelled', 'failed'].includes(turn.interruptedStatus)"
                  type="button"
                  :disabled="foregroundSubmitting || foregroundRunActive || conversationArchived"
                  @click="retryInterrupted(turn)"
                >
                  重新提问
                </button>
                <button
                  v-if="
                    ['paused', 'interrupted', 'cancelled', 'failed'].includes(
                      turn.interruptedStatus
                    )
                  "
                  type="button"
                  class="quiet"
                  :disabled="foregroundSubmitting || foregroundRunActive"
                  @click="removeInterrupted(turn)"
                >
                  移除记录
                </button>
              </div>
            </div>

            <!-- 答案：流式逐字生成中 -->
            <div v-if="!turn.answer && turn.liveAnswer" class="assistant answer-draft">
              <div v-if="citationReviewing(turn)" class="answer-review-status" role="status">
                <strong>正文已生成，正在核查引用</strong>
                <span v-if="citationReviewProgressText(turn)">{{ citationReviewProgressText(turn) }}</span>
              </div>
              <MarkdownAnswer
                :text="turn.liveAnswer"
                :evidence-ids="turn.evidence.map((ev) => ev.id)"
                @evidence="selectEvidence(turn, $event)"
              />
            </div>

            <!-- 答案：最终 -->
            <div
              v-if="turn.cancelled && !turn.liveAnswer && !turn.answer"
              class="turn-cancelled"
              role="status"
            >
              已停止
            </div>
            <div v-if="stoppingRun && turn.streaming" class="turn-cancelled" role="status">
              正在停止…
            </div>
            <div v-if="turn.answer" class="assistant answer-final">
              <div class="answer-brand"><img src="/app-icon.png" alt="研序" />研序</div>
              <div class="answer-head">
                <span
                  v-if="turn.executionMode === 'plan' && turn.todo.length"
                  class="gate-badge plan-badge"
                  >{{ turn.planRunId ? '计划已提交执行' : '计划 · 待审阅' }}</span
                >
                <span v-else-if="turn.reviewedPlanId" class="gate-badge plan-badge"
                  >按已审阅计划执行</span
                >
                <span v-if="turn.refusal" class="gate-badge refuse">已拒答</span>
                <span v-else-if="turn.taskOutcome === 'partial'" class="gate-badge warn"
                  >部分完成 · {{ turn.incompleteSteps?.length || 0 }} 项待处理</span
                >
                <span v-else-if="turn.taskOutcome === 'blocked'" class="gate-badge warn"
                  >任务受阻，尚未完成</span
                >
                <span
                  v-else-if="
                    turn.gate?.status !== 'not_applicable' &&
                    turn.gate?.passed &&
                    turn.evidence.length
                  "
                  class="gate-badge ok"
                  >来源已关联 · {{ turn.gate.marked_claims }}/{{
                    turn.gate.total_claims
                  }}
                  条论断带引用</span
                >
                <span
                  v-else-if="
                    turn.gate?.status !== 'not_applicable' && turn.gate && turn.evidence.length
                  "
                  class="gate-badge none"
                  >引用闸门未通过</span
                >
                <span
                  v-if="
                    turn.audit?.status !== 'not_applicable' &&
                    (turn.audit?.status === 'unverified' || turn.audit?.verified === false)
                  "
                  class="gate-badge warn"
                  >{{ turn.audit?.mode === 'source_attribution' ? '材料依据尚未核验' : '语义审计未核验 · 引用支撑未经确认' }}</span
                >
                <span v-else-if="turn.audit?.status === 'failed'" class="gate-badge warn"
                  >{{ turn.audit?.mode === 'source_attribution' ? '部分材料结论尚缺依据' : '语义审计发现不支持项' }}</span
                >
                <span
                  v-else-if="turn.audit?.passed === true && turn.audit?.verified === true && !citationReviewIssues(turn).length"
                  class="gate-badge ok"
                  >来源支撑已核验</span
                >
              </div>
              <p v-if="citationReviewIssues(turn).length" class="answer-review-summary" role="status">
                正文已保留，{{ citationReviewIssues(turn).length }} 项来源问题列在正文下方；相关句子尚未全部核实。
              </p>
              <MarkdownAnswer
                :text="turn.answer"
                :read-record-id="turn.recordId || undefined"
                :evidence-ids="turn.evidence.map((ev) => ev.id)"
                @evidence="selectEvidence(turn, $event)"
              />
              <details v-if="citationReviewIssues(turn).length" class="answer-review-issues">
                <summary>查看 {{ citationReviewIssues(turn).length }} 项来源核查问题</summary>
                <section v-if="citationReviewIssues(turn).some((issue) => issue.status === 'unsupported')">
                  <h4>原文不支持或缺少来源</h4>
                  <ul>
                    <li v-for="(issue, index) in citationReviewIssues(turn).filter((item) => item.status === 'unsupported')" :key="`unsupported-${index}`">
                      <span>{{ issue.text }}</span>
                      <small v-if="issue.reason">{{ issue.reason }}</small>
                    </li>
                  </ul>
                </section>
                <section v-if="citationReviewIssues(turn).some((issue) => issue.status === 'unverified')">
                  <h4>待核验（不代表原文已否定）</h4>
                  <ul>
                    <li v-for="(issue, index) in citationReviewIssues(turn).filter((item) => item.status === 'unverified')" :key="`unverified-${index}`">
                      <span>{{ issue.text }}</span>
                      <small v-if="issue.reason">{{ issue.reason }}</small>
                    </li>
                  </ul>
                </section>
              </details>
              <div class="answer-actions">
                <SaveArtifactButton v-if="!turn.streaming && turn.answer" :title="turn.question" :content="turn.answer" :evidence="turn.evidence" :conversation-id="conversationId" :disabled="conversationArchived" />
                <button
                  v-if="
                    turn.executionMode === 'plan' &&
                    turn.todo.length &&
                    !turn.refusal &&
                    turn.recordId &&
                    !turn.planRunId
                  "
                  class="execute-plan"
                  type="button"
                  :disabled="
                    turn.streaming ||
                    foregroundRunActive ||
                    foregroundSubmitting ||
                    stateLoading ||
                    conversationArchived ||
                    conversationArchive.pending.has(conversationId)
                  "
                  @click="executePlan(turn)"
                >
                  <PlayCircle :size="14" />执行此计划
                </button>
                <button
                  v-if="turn.executionMode === 'plan' && turn.todo.length && turn.recordId"
                  class="aa"
                  type="button"
                  title="刷新执行状态"
                  aria-label="刷新执行状态"
                  @click="refreshPlanClaims(conversationId)"
                >
                  <History :size="14" />
                </button>
                <button
                  class="aa"
                  type="button"
                  title="复制回答"
                  aria-label="复制回答"
                  @click="copyAnswer(turn)"
                >
                  <Copy :size="14" />
                </button>
                <button
                  v-if="!turn.reviewedPlanId"
                  class="aa"
                  type="button"
                  title="重新生成"
                  aria-label="重新生成"
                  @click="regenerate(turn)"
                >
                  <RotateCcw :size="14" />
                </button>
                <button v-if="turn.recordId" class="aa" type="button" title="编辑问题并创建修订分支" aria-label="编辑问题并创建修订分支" @click="regenerate(turn)"><PenLine :size="14" /></button>
                <button
                  class="aa"
                  type="button"
                  @click="forkFromTurn(turn)"
                  title="从此回答分叉"
                  aria-label="从此回答分叉"
                >
                  <GitFork :size="14" />
                </button>
                <button
                  class="aa"
                  type="button"
                  title="定稿自检"
                  aria-label="定稿自检"
                  @click="runFinalCheck(turn)"
                >
                  <ShieldCheck :size="14" />
                </button>
                <div class="export-menu">
                  <button
                    class="aa"
                    type="button"
                    title="导出回答"
                    aria-label="导出回答"
                    @click="toggleExportMenu(ti)"
                  >
                    <Download :size="14" />
                  </button>
                  <div v-if="exportMenuFor === ti" class="export-pop">
                    <button
                      v-for="f in EXPORT_FORMATS"
                      :key="f.key"
                      type="button"
                      class="export-opt"
                      @click="doExport(turn, f.key)"
                    >
                      {{ f.label }}
                    </button>
                  </div>
                </div>
                <span
                  v-if="turn.runtimeConfig?.model_id"
                  class="aa-model"
                  :title="`${turn.runtimeConfig.profile_name || ''} · ${turn.runtimeConfig.model_id} · ${turn.runtimeConfig.base_url || ''}`"
                  >{{ turn.runtimeConfig.profile_name || turn.runtimeConfig.model_id }}</span
                >
                <span class="aa-latency">{{ turn.latency }} ms</span>
                <span
                  v-if="turn.usage"
                  class="aa-usage"
                  :title="`输入 ${turn.usage.prompt_tokens} · 输出 ${turn.usage.completion_tokens}`"
                >
                  <Coins :size="13" />
                  {{ turn.usage.total_tokens }} tokens
                </span>
              </div>
              <!-- 定稿自检报告 -->
              <div
                v-if="turn.finalCheck"
                class="fc-report"
                :class="{
                  ok: turn.finalCheck.status === 'passed',
                  pending: turn.finalCheck.status === 'unverified',
                }"
              >
                <div class="fc-head">
                  <ShieldCheck :size="15" />
                  <span>{{ turn.finalCheck.summary }}</span>
                </div>
                <div class="fc-checks">
                  <div v-for="(c, ci) in turn.finalCheck.checks" :key="ci" class="fc-check">
                    <span
                      class="fc-dot"
                      :class="{
                        pass: c.status === 'passed',
                        fail: c.status === 'failed',
                        unverified: c.status === 'unverified',
                      }"
                    ></span>
                    <span class="fc-name">{{ c.name }}</span>
                    <span class="fc-detail">{{ c.detail }}</span>
                  </div>
                </div>
                <div v-if="turn.finalCheck.must_fix.length" class="fc-fix">
                  <div class="fc-fix-title">待处理清单</div>
                  <div v-for="(f, fi) in turn.finalCheck.must_fix" :key="fi" class="fc-fix-item">
                    <span class="fc-fix-check">{{ f.check }}</span>
                    <span>{{ f.item }}</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- 证据 -->
            <div v-if="turn.evidence.length && !sourcesOpen" class="evidence-list">
              <div
                v-for="ev in turn.evidence"
                :key="ev.id"
                class="evidence-card"
                @click="selectEvidence(turn, ev.id)"
              >
                <div class="evidence-meta">
                  <span class="cite-chip">{{ ev.id }}</span>
                  <BookOpen :size="13" />
                  <span class="ev-title">{{ ev.paper_title }}</span>
                  <span>· 第 {{ ev.page }} 页 · 相关度 {{ ev.score }}</span>
                </div>
                <div class="ev-quote">{{ ev.quote }}</div>
              </div>
            </div>

            <!-- 外部检索结果：一键入库 -->
            <div v-if="turn.externalPapers.length" class="external-list">
              <div class="external-head">
                <Download :size="14" />
                外部检索结果（开放 PDF，可一键入库）
              </div>
              <div v-for="(p, pi) in turn.externalPapers" :key="pi" class="external-card">
                <div class="external-info">
                  <div class="external-title">{{ p.title }}</div>
                  <div class="external-meta">
                    {{ p.authors }}<span v-if="p.year"> · {{ p.year }}</span> · 来源 {{ p.source }}
                  </div>
                </div>
                <button
                  class="import-btn"
                  :class="p.status"
                  :disabled="p.status === 'importing' || p.status === 'done'"
                  @click="importExternal(turn, p)"
                >
                  <template v-if="p.status === 'idle' || p.status === 'fail'">
                    {{ p.status === 'fail' ? '重试' : '入库' }}
                  </template>
                  <template v-else-if="p.status === 'importing'">下载中…</template>
                  <template v-else>已入库</template>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <button
        v-if="started && !followingOutput"
        class="jump-to-latest"
        type="button"
        title="回到最新消息"
        aria-label="回到最新消息"
        @click="scrollToBottom(true)"
      >
        <ArrowDown :size="16" />
        回到最新消息
      </button>
    </div>

    <!-- 输入框 -->
    <div class="composer">
      <div class="input-wrapper">
        <div
          v-if="project || projectLoading || selectedFiles.length || projectLoadError"
          class="project-context-row"
        >
          <button
            type="button"
            class="project-context-button"
            :disabled="stateLoading || projectLoading"
            :title="project?.root_path || '项目文件'"
            @click="showWorkspace = true"
          >
            <FolderOpen :size="14" /><span>{{
              projectLoading ? '正在载入项目…' : project?.name || '项目文件'
            }}</span>
          </button>
          <button
            v-if="project"
            type="button"
            class="terminal-toggle"
            aria-label="切换项目终端"
            title="终端"
            :aria-pressed="showTerminal"
            @click="showTerminal = !showTerminal"
          >
            <TerminalSquare :size="14" />
          </button>
          <span v-if="project && !project.available" class="project-unavailable">目录不可访问</span>
          <span v-if="projectLoadError" class="project-unavailable"
            >{{ projectLoadError }} <button @click="refreshProject">重试</button></span
          >
          <button
            v-for="file in selectedFiles"
            :key="file"
            type="button"
            class="project-file-chip"
            :title="file"
            :aria-label="`移除引用 ${file}`"
            @click="selectedFiles = selectedFiles.filter((f) => f !== file)"
          >
            <FileText :size="12" /><span>{{ file }}</span
            ><X :size="12" />
          </button>
        </div>
        <ConversationSources
          ref="conversationSources"
          :conversation-id="conversationId"
          :source-ids="pendingSourceIds"
          :project-id="project?.id"
          :workspace-revision="workspaceRevision"
          :project-files="selectedFiles"
          :disabled="stateLoading || foregroundSubmitting || conversationArchived"
          @attach="attachSource"
          @captured="capturedProjectSource"
          @remove="pendingSourceIds = pendingSourceIds.filter((id) => id !== $event)"
          @busy="sourceUploading = $event"
        />
        <textarea
          ref="composerEl"
          v-model="draft"
          :disabled="
            stateLoading ||
            foregroundSubmitting ||
            conversationArchived ||
            conversationArchive.pending.has(conversationId)
          "
          maxlength="200000"
          class="textarea"
          rows="1"
          :placeholder="
            foregroundRunActive ? '补充当前任务，或选择排队下一轮…' : '描述研究问题，或添加文献…'
          "
          @keydown="onComposerKeydown"
          @click="onDraftInput"
          @input="onDraftInput"
        ></textarea>

        <!-- 斜杠命令面板 -->
        <div v-if="showSlash" class="slash-panel">
          <div v-if="slashCommands.length === 0" class="slash-empty">没有匹配的命令</div>
          <button
            v-for="(c, i) in slashCommands"
            :key="c.name"
            type="button"
            class="slash-item"
            :class="{ active: i === slashIndex }"
            @mouseenter="slashIndex = i"
            @click="runSlashCommand(c)"
          >
            <span class="slash-name">/{{ c.name }}</span>
            <span class="slash-desc">{{ c.desc }}</span>
          </button>
        </div>

        <!-- 统一 @ mention 面板 -->
        <div v-if="showMention" class="mention-panel">
          <div v-if="mentionItems.length === 0" class="mention-empty">
            没有匹配「{{ mentionQuery }}」的文件/论文/技能/MCP
          </div>
          <button
            v-for="(it, i) in mentionItems"
            :key="it.type + it.label"
            type="button"
            class="mention-item"
            :class="{ active: i === mentionIndex }"
            @mouseenter="mentionIndex = i"
            @click="selectMention(it)"
          >
            <span class="mention-type" :class="it.type">{{ mentionTypeLabel(it.type) }}</span>
            <span class="mention-label">{{ it.label }}</span>
            <span class="mention-hint">{{ it.hint }}</span>
          </button>
        </div>

        <!-- 待发送图片预览 -->
        <div v-if="pendingImages.length" class="image-previews">
          <div v-for="(img, ii) in pendingImages" :key="ii" class="image-preview">
            <img :src="img.preview" alt="待发送图片" />
            <button type="button" class="image-preview-x" @click="removeImage(ii)">
              <X :size="11" :stroke-width="2.6" />
            </button>
          </div>
        </div>
        <input
          ref="imageInput"
          type="file"
          accept="image/*"
          multiple
          style="display: none"
          @change="onImagePicked"
        />
        <!-- 已选技能 chips -->
        <div v-if="selectedSkills.length" class="skill-chips">
          <span v-for="sn in selectedSkills" :key="sn" class="skill-chip">
            <Wand2 :size="11" :stroke-width="2" />
            {{ skillNameOf(sn) }}
            <button type="button" class="chip-x" @click="removeSkill(sn)">
              <X :size="11" :stroke-width="2.4" />
            </button>
          </span>
        </div>
        <div
          v-if="visibleRunInputs.length"
          class="run-input-list"
          aria-label="当前任务的补充与排队消息"
        >
          <div
            v-for="input in visibleRunInputs"
            :key="input.input_id"
            class="run-input-item"
            :data-status="input.status"
          >
            <div class="run-input-copy">
              <span class="run-input-kind">{{
                input.kind === 'steer' ? '补充当前任务' : '排队下一轮'
              }}</span>
              <span class="run-input-content">{{ input.content }}</span>
              <span class="run-input-state" role="status">{{ runInputStatus(input) }}</span>
            </div>
            <button
              v-if="input.status === 'dispatched' && input.child_run_id"
              type="button"
              @click="locateQueuedRun(input)"
            >查看执行</button>
            <button
              v-if="input.status === 'queued'"
              type="button"
              :disabled="runInputSubmitting"
              :aria-label="`撤销${input.kind === 'steer' ? '补充' : '排队消息'}：${input.content.slice(0, 30)}`"
              @click="cancelQueuedRunInput(input)"
            >
              撤销
            </button>
          </div>
        </div>
        <p v-if="runInputError || runInputSyncError" class="run-input-error" role="alert">
          {{ runInputError || runInputSyncError }}
        </p>
        <div v-if="queuedSlashCommands.length" class="queued-commands" role="status">
          <span>已排队 {{ queuedSlashCommands.length }} 条命令</span>
          <span class="queued-command-list">{{
            queuedSlashCommands.map((command) => command.raw).join(' · ')
          }}</span>
          <button type="button" @click="clearQueuedSlashCommands">清空</button>
        </div>
        <div class="actions-row" :class="{ 'with-run-input': foregroundRunActive }">
          <div class="tools">
            <div ref="skillPickerEl" class="composer-add-wrap">
              <button
                ref="composerAddButton"
                type="button"
                class="composer-add"
                aria-label="添加资料与工具"
                title="添加资料与工具"
                aria-haspopup="menu"
                aria-controls="composer-add-menu"
                :aria-expanded="composerMenuOpen || showSkillPicker"
                @click="toggleComposerMenu"
                @keydown.down.prevent="toggleComposerMenu"
              >
                <Plus :size="18" />
              </button>
              <div
                v-if="composerMenuOpen"
                id="composer-add-menu"
                ref="composerMenuEl"
                class="composer-add-menu"
                role="menu"
                aria-label="添加资料与工具"
                @keydown="composerMenuKeys"
              >
                <button
                  type="button"
                  role="menuitem"
                  :disabled="stateLoading || projectLoading"
                  @click="useComposerAction('files')"
                >
                  <FolderOpen :size="15" />项目文件
                </button>
                <button type="button" role="menuitem" @click="useComposerAction('images')">
                  <Paperclip :size="15" />添加图片或截图
                </button>
                <button type="button" role="menuitem" :disabled="stateLoading || conversationArchived" @click="useComposerAction('sources')">
                  <FileText :size="15" />上传或选择会话资料<span v-if="pendingSourceIds.length" class="composer-menu-count">{{ pendingSourceIds.length }}</span>
                </button>
                <button type="button" role="menuitem" @click="useComposerAction('review')">
                  <ShieldCheck :size="15" />审稿件
                </button>
                <button type="button" role="menuitem" @click="useComposerAction('skills')">
                  <Wand2 :size="15" />选择技能<span
                    v-if="selectedSkills.length"
                    class="composer-menu-count"
                    >{{ selectedSkills.length }}</span
                  >
                </button>
                <button
                  v-if="!foregroundRunActive"
                  type="button"
                  role="menuitem"
                  :disabled="
                    !draft.trim() ||
                    foregroundSubmitting ||
                    foregroundRunActive ||
                    conversationArchived ||
                    conversationArchive.pending.has(conversationId)
                  "
                  @click="useComposerAction('background')"
                >
                  <PlayCircle :size="15" />后台运行
                </button>
              </div>
              <div
                v-if="showSkillPicker"
                class="skill-pop"
                role="dialog"
                aria-label="选择技能"
                @keydown.esc.stop.prevent="closeSkillPicker"
              >
                <div class="skill-pop-head">
                  <span>选择技能（可多选）</span>
                  <button
                    v-if="selectedSkills.length"
                    type="button"
                    class="skill-clear"
                    @click="selectedSkills = []"
                  >
                    清空
                  </button>
                  <button
                    type="button"
                    class="skill-clear"
                    aria-label="关闭技能选择"
                    @click="closeSkillPicker"
                  >
                    <X :size="14" />
                  </button>
                </div>
                <div class="skill-pop-list">
                  <button
                    v-for="s in skillLib"
                    :key="s.name"
                    type="button"
                    class="skill-opt"
                    :class="{ sel: selectedSkills.includes(s.name) }"
                    :aria-pressed="selectedSkills.includes(s.name)"
                    @click="toggleSkill(s.name)"
                  >
                    <span class="skill-opt-check">
                      <Check v-if="selectedSkills.includes(s.name)" :size="11" :stroke-width="3" />
                    </span>
                    <span class="skill-opt-body">
                      <span class="skill-opt-name">{{ s.title }}</span>
                      <span class="skill-opt-desc">{{ s.description }}</span>
                    </span>
                  </button>
                  <div v-if="!skillLib.length" class="skill-pop-empty">暂无可用技能</div>
                </div>
              </div>
            </div>
            <select
              v-model="executionMode"
              class="composer-mode"
              aria-label="工作模式"
              :disabled="
                stateLoading || foregroundSubmitting || foregroundRunActive || conversationArchived
              "
            >
              <option value="execute">执行</option>
              <option value="plan">计划</option>
            </select>
            <ConversationControls
              ref="conversationControls"
              :conversation-id="conversationId"
              :disabled="stateLoading || foregroundSubmitting || conversationArchived"
              :running="foregroundRunActive"
              :active-runtime="activeRuntimeConfig"
              @ready="conversationOptionsReady = $event"
            />
            <select v-model="paperId" class="scope-select" title="检索范围">
              <option :value="null">全部文献 {{ paperLibraryCount }}</option>
              <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
            </select>
            <input v-if="paperLibraryCount>40 || paperQuery" v-model="paperQuery" class="scope-search" aria-label="检索文献范围" placeholder="检索要限定的文献" :title="`显示前 40 篇候选，共 ${paperChoiceCount} 篇匹配；选全部文献时检索整个库`" />
            <button v-if="paperChoiceError" @click="loadPaperChoices">{{paperChoiceError}}</button>
          </div>
          <div class="input-actions">
            <label v-if="foregroundRunActive" class="run-input-mode">
              <select
                v-model="runInputMode"
                aria-label="运行中输入用途"
                :disabled="runInputSubmitting"
              >
                <option value="steer">补充当前任务</option>
                <option value="next_turn">排队下一轮</option>
              </select>
            </label>
            <button
              v-if="foregroundRunActive"
              class="run-input-send"
              type="button"
              :aria-label="runInputMode === 'steer' ? '发送补充' : '保存排队消息'"
              :disabled="
                !draft.trim() ||
                runInputSubmitting ||
                sourceUploading ||
                runInputLoading ||
                stateLoading ||
                stoppingRun ||
                conversationArchived
              "
              @click="submitComposer()"
            >
              {{
                runInputSubmitting ? '发送中…' : runInputMode === 'steer' ? '发送补充' : '保存排队'
              }}
            </button>
            <button
              v-if="!foregroundRunActive"
              class="send-btn"
              type="button"
              title="发送"
              aria-label="发送"
              @click="submitComposer()"
              :disabled="
                !draft.trim() ||
                !conversationOptionsReady ||
                sourceUploading ||
                foregroundSubmitting ||
                conversationArchived ||
                conversationArchive.pending.has(conversationId)
              "
            >
              <ArrowUp :size="16" :stroke-width="2.2" />
            </button>
            <button
              v-if="foregroundRunActive"
              class="stop-btn"
              type="button"
              :title="stoppingRun ? '正在停止' : '停止生成'"
              aria-label="停止生成"
              :disabled="stoppingRun"
              @click="stop()"
            >
              <Square :size="13" :stroke-width="2" fill="currentColor" />
            </button>
          </div>
        </div>
      </div>
      <div
        class="draft-save-state"
        :class="{ attention: ['error', 'conflict', 'deleted'].includes(saveStatus) }"
        role="status"
      >
        <span v-if="stateLoading">正在恢复会话…</span>
        <template v-else-if="saveStatus === 'conflict'">
          <span
            >{{
              activeState.error.value || '另一个窗口已更新草稿，本窗口内容已保留。'
            }}
            工作目录切换后需重新选择文件引用。</span
          >
          <button type="button" @click="resolveDraftConflict(true)">保留本窗口草稿</button>
          <button type="button" @click="resolveDraftConflict(false)">载入已保存草稿</button>
        </template>
        <template v-else-if="saveStatus === 'error'">
          <span :title="activeState.error.value">草稿尚未保存，请暂勿关闭窗口。</span>
          <button type="button" @click="activeState.retry()">重试保存</button>
        </template>
        <span v-else-if="saveStatus === 'deleted'"
          >此会话已在其他窗口删除，请复制草稿后新建会话。</span
        >
        <span v-else-if="readingImages">正在读取图片…</span>
        <span v-else>{{ saveStatus === 'saved' ? '草稿已保存在本机' : '正在保存草稿…' }}</span>
      </div>
      <div v-if="readReceiptError" class="draft-save-state attention" role="status">
        已读状态尚未保存 <button type="button" @click="readState.retry">重试</button>
      </div>
      <div v-if="remoteAnswerError" class="draft-save-state attention" role="status">
        新回答载入失败
        <button
          type="button"
          :disabled="stateLoading || foregroundRunActive"
          @click="refreshRemoteAnswers"
        >
          重试载入
        </button>
      </div>
      <p class="disclaimer">Enter 发送 · Shift+Enter 换行</p>
      <div v-if="!started" class="welcome-shortcuts">
        <button type="button" @click="router.push('/library')">
          <Download :size="15" />导入文献
        </button>
        <button type="button" @click="onCardClick('帮我梳理这几篇论文的研究问题')">
          <FileText :size="15" />梳理文献
        </button>
        <button type="button" @click="router.push('/compare')">
          <BarChart3 :size="15" />对比研究
        </button>
      </div>
    </div>

    <aside
      v-if="sourcesOpen"
      id="research-inspector"
      class="research-inspector"
      aria-label="研究资料"
    >
      <div class="inspector-tabs" role="tablist" aria-label="研究上下文">
        <button
          v-for="tab in ['sources', 'outputs', 'execution'] as const"
          :key="tab"
          type="button"
          role="tab"
          :aria-selected="sourceTab === tab"
          :class="{ active: sourceTab === tab }"
          @click="sourceTab = tab"
        >
          {{ { sources: '资料', outputs: '产出', execution: '执行' }[tab] }}
        </button>
        <button
          class="inspector-close"
          type="button"
          aria-label="关闭研究资料"
          @click="sourcesOpen = false"
        >
          <X :size="15" />
        </button>
      </div>
      <div
        class="inspector-content"
        role="tabpanel"
        :aria-label="{ sources: '资料', outputs: '产出', execution: '执行' }[sourceTab]"
      >
        <template v-if="sourceTab === 'sources'">
          <p class="source-intro">
            {{ inspectedTurn?.evidence.length ? '本次回答关联' : '当前可用' }}
            {{ sourceItems.length }} 份资料<br />点击引用编号，查看对应的原文版本。
          </p>
          <article
            v-for="(ev, index) in sourceItems"
            :key="evidenceSourceKey(ev)"
            class="source-item"
            :class="{ active: ev.id && selectedEvidence === ev.id }"
          >
            <div class="source-number">
              <span>{{ index + 1 }}</span
              ><FileText :size="12" /><span class="source-type">{{ ev.revision_id ? '会话资料' : '论文' }}</span>
            </div>
            <button type="button" class="source-title" @click="openSource(ev)">
              {{ ev.source_title || ev.paper_title }}
            </button>
            <div class="source-meta">
              {{ ev.revision_id ? '固定来源版本' : papers.find((p) => p.id === ev.paper_id)?.authors || '本地文献'
              }}<span v-if="ev.id && ev.page"> · 第 {{ ev.page }} 页</span>
            </div>
            <p v-if="ev.quote" class="source-summary">{{ ev.quote }}</p>
            <div
              v-if="inspectedTurn?.evidence.filter((item) => evidenceSourceKey(item) === evidenceSourceKey(ev)).length"
              class="source-evidence-links"
            >
              <button
                v-for="item in inspectedTurn.evidence.filter(
                  (item) => evidenceSourceKey(item) === evidenceSourceKey(ev)
                )"
                :key="item.id"
                type="button"
                @click="openEvidence(inspectedTurn, item.id)"
              >
                {{ item.id }} · {{ item.revision_id ? '查看引用位置' : `第 ${item.page} 页` }}
              </button>
            </div>
            <button class="source-link" type="button" @click="openSource(ev)">
              打开阅读 <ArrowUpRight :size="13" />
            </button>
          </article>
          <p v-if="!sourceItems.length" class="source-intro">
            导入论文后，文献与回答引用将在这里显示。
          </p>
        </template>
        <template v-else-if="sourceTab === 'outputs'">
          <p class="source-intro">当前研究的回答与可导出内容。</p>
          <article
            v-for="(turn, index) in turns.filter((t) => t.answer)"
            :key="turn.recordId || index"
            class="source-item"
          >
            <div class="source-number">
              {{ index + 1 }} · {{ turn.executionMode === 'plan' ? '研究计划' : '研究回答' }}
            </div>
            <p class="output-title">{{ turn.question }}</p>
            <button type="button" class="source-link" @click="copyAnswer(turn)">
              <Copy :size="13" />复制回答
            </button>
            <button type="button" class="source-link" @click="doExport(turn, 'md')">
              <Download :size="13" />导出 Markdown
            </button>
          </article>
          <p v-if="!turns.some((t) => t.answer)" class="source-intro">
            生成回答后，可在这里复制或导出。
          </p>
        </template>
        <template v-else>
          <p class="source-intro">
            {{ inspectedTurn?.streaming ? '研究正在进行' : '最近一轮的执行记录' }}
          </p>
          <ol class="inspector-steps">
            <li v-for="(step, index) in inspectedTurn?.todo || []" :key="index">
              <span>{{ step.title }}</span
              ><small>{{
                step.status === 'done'
                  ? '已完成'
                  : step.status === 'in_progress'
                    ? '进行中'
                    : '待执行'
              }}</small>
            </li>
          </ol>
          <p v-if="!inspectedTurn?.todo.length" class="source-intro">发送问题后显示研究步骤。</p>
          <button type="button" class="source-link" @click="router.push('/tasks')">
            查看后台任务 <ArrowUpRight :size="13" />
          </button>
        </template>
      </div>
    </aside>

    <ProjectTerminal
      v-if="project && terminalLoaded"
      :key="project.id"
      :project="project"
      :visible="showTerminal"
      @hide="showTerminal = false"
    />
    <!-- 一键审我的稿件 弹窗 -->
    <div v-if="showReviewModal" class="modal-mask" @click.self="showReviewModal = false">
      <div class="modal review-modal">
        <div class="modal-head">
          <span>一键审我的稿件</span>
          <button class="modal-x" @click="showReviewModal = false"><X :size="16" /></button>
        </div>
        <div class="modal-body">
          <div class="review-toolbar">
            <button class="rv-file-btn" type="button" @click="reviewFile?.click()">
              导入 .txt/.md 文件
            </button>
            <input
              ref="reviewFile"
              type="file"
              accept=".txt,.md,.markdown,text/plain"
              style="display: none"
              @change="onReviewFile"
            />
            <button class="rv-run-btn" type="button" :disabled="reviewing" @click="runReview">
              {{ reviewing ? '审稿中…' : '开始审稿' }}
            </button>
          </div>
          <textarea
            v-model="reviewDraftText"
            class="review-input"
            rows="9"
            placeholder="把你的综述或草稿粘贴到这里，然后点击「开始审稿」……"
          ></textarea>

          <div v-if="reviewResult" class="review-result">
            <div class="rv-overall">
              <span class="rv-verdict" :class="reviewResult.verdict">{{
                verdictLabel(reviewResult.verdict)
              }}</span>
              <span>{{ reviewResult.overall }}</span>
            </div>
            <div v-if="reviewResult.stats" class="rv-stats">
              <span
                >引用覆盖率：{{
                  Math.round((reviewResult.stats.citation_coverage || 0) * 100)
                }}%</span
              >
              <span
                >有引用论断：{{ reviewResult.stats.cited_sentences }}/{{
                  reviewResult.stats.substantive_sentences
                }}</span
              >
              <span>参考文献：{{ reviewResult.stats.reference_count }}</span>
            </div>
            <div v-if="reviewResult.strengths.length" class="rv-strengths">
              <div class="rv-block-title">优点</div>
              <ul>
                <li v-for="(s, i) in reviewResult.strengths" :key="i">{{ s }}</li>
              </ul>
            </div>
            <div class="rv-block-title">问题与修改建议（{{ reviewResult.issues.length }}）</div>
            <div
              v-for="(iss, i) in reviewResult.issues"
              :key="i"
              class="rv-issue"
              :class="iss.severity"
            >
              <div class="rv-issue-head">
                <span class="rv-sev">{{ severityLabel(iss.severity) }}</span>
                <span class="rv-dim">{{ iss.dimension }}</span>
                <span class="rv-issue-title">{{ iss.title }}</span>
              </div>
              <div class="rv-issue-detail">{{ iss.detail }}</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <ConversationMemoryPanel
      ref="memoryPanel"
      v-model="showMemory"
      :conversation-id="conversationId"
      :archived="conversationArchived"
      @source="showMemorySource"
    />
    <FileChangeReview
      v-model="fileReviewId"
      :conversation-id="conversationId"
      :readonly="conversationArchived"
    />
    <ProjectCommandReview v-model="commandReviewId" />
    <GitOperationReview
      v-if="gitReviewTarget"
      :key="gitReviewTarget.id"
      :operation="gitReviewTarget"
      @close="gitReviewTarget = null"
    />
    <ProjectWorkspace
      v-model="showWorkspace"
      :conversation-id="conversationId"
      :project="project"
      :files="selectedFiles"
      :disabled="
        stateLoading ||
        foregroundSubmitting ||
        foregroundRunActive ||
        backgroundSubmitting ||
        conversationArchived
      "
      @bound="boundProject"
      @switched="switchedProject"
      @files="selectedFiles = $event"
      @new-chat="newProjectChat"
    />
    <!-- 历史会话抽屉（右侧） -->
    <div
      v-if="showHistory"
      class="hist-mask"
      @click.self="showHistory = false"
      @keydown="historyKeydown"
    >
      <div
        ref="historyDrawer"
        class="hist-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="历史会话"
      >
        <div class="hist-head">
          <span class="hist-title">
            <History :size="16" :stroke-width="1.9" />
            历史会话
          </span>
          <button
            class="modal-x"
            aria-label="关闭历史会话"
            title="关闭"
            @click="showHistory = false"
          >
            <X :size="16" />
          </button>
        </div>
        <div class="hist-tabs" role="tablist" aria-label="会话状态" @keydown="historyTabKeydown">
          <button
            v-for="scope in ['active', 'archived'] as const"
            :key="scope"
            :id="`history-tab-${scope}`"
            type="button"
            role="tab"
            :aria-selected="historyScope === scope"
            :tabindex="historyScope === scope ? 0 : -1"
            aria-controls="history-panel"
            @click="selectHistoryScope(scope)"
          >
            <History v-if="scope === 'active'" :size="14" /><Archive v-else :size="14" />{{
              scope === 'active' ? '会话' : '已归档'
            }}
          </button>
        </div>
        <div
          v-if="conversationArchive.error"
          class="hist-load-error archive-operation-error"
          role="alert"
        >
          <span>{{ conversationArchive.error }}</span>
          <button v-if="conversationArchive.retry" type="button" @click="retryConversationArchive">
            重试归档操作
          </button>
          <button
            type="button"
            aria-label="关闭归档提示"
            title="关闭提示"
            @click="dismissArchiveError"
          >
            <X :size="14" />
          </button>
        </div>
        <div class="hist-search">
          <input
            v-model="convSearch"
            class="hist-search-input"
            aria-label="搜索会话标题或正文"
            placeholder="搜索标题、问题或回答…"
            maxlength="200"
          />
          <span class="hist-result-count" role="status">{{
            historyLoading ? '正在查找…' : `${historyTotal} 个会话`
          }}</span>
        </div>
        <div
          id="history-panel"
          class="hist-list"
          role="tabpanel"
          :aria-labelledby="`history-tab-${historyScope}`"
          :aria-busy="historyLoading"
        >
          <div v-if="historyLoading" class="hist-empty" role="status">正在加载…</div>
          <div v-if="historyError" class="hist-load-error" role="alert">
            会话加载失败<button type="button" @click="refreshHistory">重试加载</button>
          </div>
          <div
            v-for="c in filteredConv"
            :key="c.id"
            class="hist-item"
            :data-history-id="c.id"
            :class="{ active: c.id === conversationId }"
          >
            <template v-if="renamingId === c.id">
              <div class="hist-rename">
                <input
                  ref="renameInput"
                  v-model="renamingText"
                  class="hist-rename-input"
                  @keydown.enter="confirmRename(c)"
                  @keydown.esc="renamingId = null"
                />
                <button class="hist-rename-ok" @click="confirmRename(c)">
                  <Check :size="13" :stroke-width="2.4" />
                </button>
              </div>
            </template>
            <template v-else>
              <button class="hist-item-main" @click="switchConversation(c)">
                <span class="hist-item-title">{{ c.title }}</span>
                <span v-if="c.match_text && c.match_field" class="hist-match"
                  ><span class="hist-match-source">{{ matchLabels[c.match_field] }} · </span
                  >{{ matchParts(c.match_text)[0] }}<mark>{{ matchParts(c.match_text)[1] }}</mark
                  >{{ matchParts(c.match_text)[2] }}</span
                >
                <span class="hist-item-meta">
                  {{
                    c.draft_only
                      ? c.interrupted_only
                        ? '未完成回答'
                        : '未发送草稿'
                      : `${c.message_count} 条问答`
                  }}
                  · {{ formatTime(c.archived_at || c.updated_at) }}
                </span>
              </button>
              <div class="hist-item-actions">
                <button
                  v-if="c.message_count"
                  class="hist-act"
                  title="分叉整段会话"
                  aria-label="分叉整段会话"
                  @click="forkWhole(c)"
                >
                  <GitFork :size="13" :stroke-width="1.9" />
                </button>
                <button
                  class="hist-act"
                  :title="
                    c.archived_at
                      ? '恢复会话'
                      : c.running_count || c.finishing_count
                        ? '任务结束后可归档'
                        : '归档会话'
                  "
                  :aria-label="c.archived_at ? '恢复会话' : '归档会话'"
                  :disabled="
                    conversationArchive.pending.has(c.id) ||
                    (!c.archived_at && Boolean(c.running_count || c.finishing_count))
                  "
                  @click="changeArchive(c)"
                >
                  <ArchiveRestore v-if="c.archived_at" :size="13" /><Archive v-else :size="13" />
                </button>
                <button
                  class="hist-act"
                  title="重命名"
                  aria-label="重命名"
                  :disabled="conversationArchive.pending.has(c.id)"
                  @click="startRename(c)"
                >
                  <Pencil :size="13" :stroke-width="1.9" />
                </button>
                <button
                  class="hist-act danger"
                  title="删除"
                  aria-label="删除"
                  :disabled="conversationArchive.pending.has(c.id)"
                  @click="removeConversation(c)"
                >
                  <Trash2 :size="13" :stroke-width="1.9" />
                </button>
              </div>
            </template>
          </div>
          <button
            v-if="historyCursor"
            class="hist-load-more"
            type="button"
            :disabled="historyLoading || historyLoadingMore"
            @click="historyList.loadMore"
          >
            {{
              historyLoadingMore
                ? '正在加载…'
                : historyMoreError
                  ? '重试加载更多会话'
                  : '加载更多会话'
            }}
          </button>
          <div v-if="!filteredConv.length && !historyLoading && !historyError" class="hist-empty">
            {{
              convSearch.trim()
                ? '没有匹配的会话'
                : historyScope === 'archived'
                  ? '暂无已归档会话'
                  : '暂无历史会话'
            }}
          </div>
        </div>
      </div>
    </div>

    <!-- 状态面板 -->
    <div v-if="showStatus" class="status-mask" @click.self="showStatus = false">
      <div class="status-modal">
        <div class="status-head">
          <span class="status-title">
            <ShieldCheck :size="16" :stroke-width="1.9" />
            运行状态 /status
          </span>
          <button class="modal-x" @click="showStatus = false"><X :size="16" /></button>
        </div>

        <div v-if="runtimeSettings" class="status-body">
          <div class="status-section">
            <div class="status-section-title">模型</div>
            <div class="status-grid">
              <span class="k">对话模型</span><span class="v">{{ runtimeSettings.llm_model }}</span>
              <span class="k">接口地址</span
              ><span class="v">{{ runtimeSettings.llm_base_url }}</span>
              <span class="k">嵌入模型</span
              ><span class="v">{{ runtimeSettings.embedding_model || '—' }}</span>
            </div>
          </div>

          <div class="status-section">
            <div class="status-section-title">持久目标</div>
            <div v-if="currentGoal" class="status-goal" :class="currentGoal.status">
              <span class="goal-status-tag">{{ goalStatusLabel(currentGoal.status) }}</span>
              <span class="status-goal-desc">{{ currentGoal.description }}</span>
            </div>
            <div v-else class="status-empty">暂无持久目标（输入 /goal 设置）</div>
            <button class="btn-ghost status-goal-btn" @click="manageGoal">管理目标</button>
          </div>

          <div class="status-section">
            <div class="status-section-title">运行控制</div>
            <div class="status-grid">
              <span class="k">审批模式</span
              ><span class="v">{{ runtimeSettings.approval_mode || 'read_only' }}</span>
              <span class="k">推理强度</span
              ><span class="v">{{ runtimeSettings.reasoning_effort || 'medium' }}</span>
              <span class="k">记忆加载</span
              ><span class="v">{{
                (runtimeSettings as any).memory_recall === false ? '关闭' : '开启'
              }}</span>
              <span class="k">记忆生成</span
              ><span class="v">{{
                (runtimeSettings as any).memory_capture === false ? '关闭' : '开启'
              }}</span>
              <span class="k">桌面通知</span
              ><span class="v">{{
                (runtimeSettings as any).desktop_notifications === false ? '关闭' : '开启'
              }}</span>
            </div>
          </div>

          <div class="status-section">
            <div class="status-section-title">MCP 与系统</div>
            <div class="status-grid">
              <span class="k">MCP 服务器</span>
              <span class="v"
                >{{ mcpRuntime.filter((x) => x.status === 'connected').length }} 个已连接</span
              >
              <span class="k">健康状态</span><span class="v">{{ healthInfo?.status || '—' }}</span>
              <span class="k">会话 ID</span><span class="v mono">{{ conversationId }}</span>
            </div>
          </div>

          <div class="status-section">
            <div class="status-section-title">Token 用量（本会话累计）</div>
            <div v-if="statusUsage" class="status-grid">
              <span class="k">问答轮次</span><span class="v">{{ statusUsage.turns }}</span>
              <span class="k">输入 tokens</span
              ><span class="v">{{ statusUsage.usage.prompt_tokens }}</span>
              <span class="k">输出 tokens</span
              ><span class="v">{{ statusUsage.usage.completion_tokens }}</span>
              <span class="k">合计 tokens</span
              ><span class="v">{{ statusUsage.usage.total_tokens }}</span>
            </div>
            <div v-else class="status-empty">暂无用量记录</div>
          </div>

          <div class="status-section">
            <div class="status-section-title">键盘快捷键</div>
            <div class="shortcut-grid">
              <span class="k">Ctrl/⌘ + K</span><span class="v">搜索功能、论文、会话</span>
              <span class="k">Ctrl/⌘ + L</span><span class="v">聚焦输入框</span>
              <span class="k">Ctrl/⌘ + Enter</span><span class="v">发送问题</span>
              <span class="k">Ctrl/⌘ + B</span><span class="v">折叠侧边栏</span>
              <span class="k">Ctrl/⌘ + ,</span><span class="v">打开设置</span>
              <span class="k">Ctrl/⌘ + J</span><span class="v">后台任务</span>
              <span class="k">Ctrl/⌘ + Alt + N</span><span class="v">新会话</span>
              <span class="k">Esc</span><span class="v">关闭弹窗</span>
            </div>
          </div>

          <div class="status-quick">
            <button class="btn-ghost" @click="cycleApproval">切换审批</button>
            <button class="btn-ghost" @click="cycleReasoning">切换推理</button>
            <button class="btn-ghost" @click="toggleMemory">切换记忆</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 配置诊断 doctor -->
    <div v-if="showDiagnostics" class="diag-mask" @click.self="showDiagnostics = false">
      <DiagnosticsPanel :conversation-id="conversationId" :run-id="turns.findLast(turn => turn.checkpointId)?.checkpointId || undefined" @close="showDiagnostics = false" />
    </div>

    <!-- 持久目标 /goal -->
    <div v-if="showGoal" class="goal-mask" @click.self="showGoal = false">
      <div class="goal-modal">
        <div class="status-head">
          <span class="status-title">
            <ListChecks :size="16" :stroke-width="1.9" />
            持久目标 /goal
          </span>
          <button class="modal-x" @click="showGoal = false"><X :size="16" /></button>
        </div>

        <!-- 当前目标 -->
        <div v-if="currentGoal" class="goal-current" :class="currentGoal.status">
          <div class="goal-current-head">
            <span class="goal-status-tag">{{ goalStatusLabel(currentGoal.status) }}</span>
          </div>
          <div class="goal-current-desc">{{ currentGoal.description }}</div>
          <div class="goal-current-actions">
            <button
              v-if="currentGoal.status === 'active'"
              type="button"
              class="btn-mini"
              :disabled="goalBusy"
              @click="onPauseGoal"
            >
              暂停
            </button>
            <button
              v-if="currentGoal.status === 'paused'"
              type="button"
              class="btn-mini"
              :disabled="goalBusy"
              @click="onResumeGoal"
            >
              恢复
            </button>
            <button type="button" class="btn-mini" :disabled="goalBusy" @click="onCompleteGoal">
              标记完成
            </button>
            <button type="button" class="btn-mini ghost" :disabled="goalBusy" @click="onCancelGoal">
              取消目标
            </button>
          </div>
        </div>

        <!-- 新建目标 -->
        <div class="goal-new">
          <label class="goal-new-label">
            {{ currentGoal ? '设置新目标（将取代当前目标）' : '设置新的跨轮目标' }}
          </label>
          <textarea
            v-model="goalDraft"
            rows="3"
            placeholder="例如：完成一份覆盖近三年顶会的文献综述，引用不少于 20 篇……"
          ></textarea>
          <div class="goal-new-actions">
            <button
              type="button"
              class="btn-mini primary"
              :disabled="goalBusy"
              @click="onCreateGoal"
            >
              {{ goalBusy ? '处理中…' : '保存目标' }}
            </button>
          </div>
        </div>

        <!-- 历史 -->
        <div v-if="goalHistory.length" class="goal-history">
          <div class="goal-history-title">历史目标（{{ goalHistory.length }}）</div>
          <div v-for="g in goalHistory" :key="g.id" class="goal-history-item" :class="g.status">
            <span class="goal-dot"></span>
            <span class="goal-history-desc">{{ g.description }}</span>
            <span class="goal-history-status">{{ goalStatusLabel(g.status) }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 后台进程 /ps -->
    <div v-if="showPs" class="ps-mask" @click.self="showPs = false">
      <div class="ps-modal">
        <div class="status-head">
          <span class="status-title">
            <PlayCircle :size="16" :stroke-width="1.9" />
            后台进程 /ps
          </span>
          <button class="modal-x" @click="showPs = false"><X :size="16" /></button>
        </div>

        <div v-if="psLoading" class="ps-empty">加载中…</div>
        <div v-else-if="!psTasks.length" class="ps-empty">暂无后台任务</div>
        <div v-else class="ps-list">
          <div v-for="t in psTasks" :key="t.id" class="ps-item">
            <div class="ps-item-main">
              <div class="ps-item-title">{{ t.title || t.id }}</div>
              <div class="ps-item-meta">
                <span class="ps-status-tag" :class="t.status">{{
                  t.executing && !['pending', 'running'].includes(t.status)
                    ? '正在结束'
                    : psStatusLabel(t.status)
                }}</span>
                <span v-if="t.progress" class="ps-progress"
                  >{{ Math.round(t.progress * 100) }}%</span
                >
              </div>
            </div>
            <button
              v-if="isActiveTask(t)"
              type="button"
              class="btn-mini ghost"
              @click="stopPsTask(t)"
            >
              停止
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scope-search { width:150px;max-width:100%;border:1px solid var(--fn-border-primary);border-radius:5px;padding:4px 7px;font-size:11px;background:var(--fn-bg-primary);color:var(--fn-text-primary); }
.history-pagination { display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:18px;font-size:12px;color:var(--fn-text-secondary); }
.history-pagination button { background:var(--fn-bg-primary);color:var(--fn-text-primary);border:1px solid var(--fn-border-primary);border-radius:6px;padding:6px 9px;cursor:pointer; }
.history-pagination button:focus-visible { outline:2px solid var(--fn-brand);outline-offset:2px; }
.history-pagination button:disabled { opacity:.5;cursor:default; }
.conversation-state-warning {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 8px 24px;
  font-size: 12px;
  background: #fff5df;
  color: #7a4b0b;
}
.conversation-state-warning button {
  color: inherit;
  border: 0;
  background: none;
  cursor: pointer;
  text-decoration: underline;
}
.hist-result-count {
  display: block;
  margin-top: 8px;
  font-size: 12px;
  color: var(--fn-text-secondary);
}
.hist-match {
  color: var(--fn-text-secondary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.hist-match mark {
  background: #fff1bb;
  color: #514015;
  border-radius: 2px;
}
.hist-match-source {
  color: var(--fn-text-muted);
}
.hist-load-more {
  width: 100%;
  padding: 12px;
  border: 0;
  background: transparent;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.hist-load-more:disabled {
  cursor: wait;
  opacity: 0.6;
}
.draft-save-state {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-height: 16px;
  margin: 5px 2px 0;
  font-size: 11px;
  color: var(--fn-text-muted, #687180);
}
.draft-save-state.attention {
  color: #8a5410;
}
.draft-save-state button {
  font: inherit;
  color: inherit;
  border: 0;
  background: transparent;
  text-decoration: underline;
  cursor: pointer;
}
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--fn-bg-primary);
}
.chat-scroll-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
}
.chat-scroll {
  height: 100%;
  overflow-y: auto;
}
.jump-to-latest {
  position: absolute;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 20px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  box-shadow: 0 2px 8px #00000012;
  cursor: pointer;
  font-size: 12px;
  z-index: 2;
}

/* ---------- 欢迎页 ---------- */
.welcome {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px 20px;
}
.welcome-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 100%;
  max-width: 960px;
  gap: 12px;
}
.terminal-open .welcome {
  padding: 16px 20px;
}
.terminal-open .hero {
  width: 84px;
}
.terminal-open .quick-section {
  display: none;
}
.hero {
  width: 140px;
  height: auto;
  margin-bottom: 4px;
}
.welcome-title {
  font-size: 26px;
  font-weight: 600;
  margin: 0;
  color: var(--fn-text-primary);
  line-height: 1.4;
  text-align: center;
}
.welcome-subtitle {
  font-size: 13.5px;
  margin: 0;
  color: var(--fn-text-secondary);
  line-height: 1.6;
  text-align: center;
  max-width: 560px;
}
.quick-section {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 26px;
}
.quick-section-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--fn-text-tertiary);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.quick-cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
@media (max-width: 900px) {
  .quick-cards {
    grid-template-columns: repeat(2, 1fr);
  }
}
.quick-card {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 12px 14px;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  cursor: pointer;
  text-align: left;
  transition:
    border-color 0.16s ease,
    box-shadow 0.16s ease,
    transform 0.16s ease;
}
.quick-card:hover {
  border-color: var(--fn-brand);
  box-shadow: 0 3px 12px rgba(10, 89, 247, 0.1);
  transform: translateY(-1px);
}
.quick-card-icon {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.quick-card-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.quick-card-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--fn-text-primary);
  white-space: nowrap;
}
.quick-card-desc {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---------- 消息流 ---------- */
.messages {
  max-width: 960px;
  margin: 0 auto;
  padding: 24px 32px 12px;
}
.turn {
  margin-bottom: 28px;
}
.user-row {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 14px;
}
.turn-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
  max-width: 80%;
}
.turn-images img {
  width: 96px;
  height: 96px;
  object-fit: cover;
  border-radius: 10px;
  border: 1px solid var(--fn-border-primary);
}
.user-bubble {
  max-width: 80%;
  background: var(--fn-bg-active);
  color: var(--fn-text-primary);
  padding: 10px 14px;
  border-radius: 8px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 14px;
  line-height: 1.6;
}
.turn-paper-scope {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 80%;
  color: var(--fn-brand);
  font-size: 11px;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.turn-paper-scope svg {
  flex-shrink: 0;
}

/* 错误提示 */
.turn-error {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid #fca5a5;
  border-radius: 10px;
  background: #fef2f2;
  color: #dc2626;
  font-size: 12.5px;
  line-height: 1.6;
}
.turn-error svg {
  width: 16px;
  height: 16px;
  flex: 0 0 16px;
  margin-top: 1px;
}
.turn-error span {
  min-width: 0;
  overflow-wrap: anywhere;
}
.foreground-recovery {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 12px 0;
  padding: 10px 12px;
  border: 1px solid var(--fn-border-secondary, #e5e7eb);
  border-left: 3px solid #c58a19;
  background: var(--fn-bg-secondary, #f8f9fb);
  color: var(--fn-text-secondary, #596273);
  font-size: 12px;
}
.foreground-recovery-copy {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  overflow-wrap: anywhere;
}
.foreground-recovery-copy strong {
  color: var(--fn-text-primary, #252b36);
  font-weight: 600;
}
.foreground-recovery-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.foreground-recovery-actions button {
  min-height: 30px;
  padding: 0 9px;
  border: 1px solid var(--fn-border-primary, #d5d9e0);
  border-radius: 4px;
  background: var(--fn-bg-primary, #fff);
  color: var(--fn-text-primary, #252b36);
  font: inherit;
  cursor: pointer;
}
.foreground-recovery-actions button.quiet {
  color: var(--fn-text-secondary, #596273);
}
.foreground-recovery-actions button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.turn-cancelled {
  padding: 8px 0;
  color: var(--fn-text-secondary);
  font-size: 13px;
}
.answer-review-status {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px 10px;
  margin: 0 0 10px;
  color: var(--fn-text-secondary);
  font-size: 12px;
}
.answer-review-status strong {
  color: var(--fn-brand);
  font-weight: 600;
}
.answer-review-issues {
  margin: 12px 0;
  padding: 12px 14px;
  border: 1px solid var(--fn-warning-border, #e5bd70);
  border-left: 4px solid var(--fn-warning, #b87809);
  border-radius: 6px;
  background: var(--fn-warning-bg, #fff8e8);
  color: var(--fn-text-primary, #252b36);
  font-size: 12px;
  line-height: 1.65;
}
.answer-review-summary {
  margin: 8px 0 12px;
  color: var(--fn-warning-text, #835400);
  font-size: 12px;
  line-height: 1.65;
}
.answer-review-issues > summary {
  display: block;
  color: var(--fn-warning-text, #835400);
  cursor: pointer;
  font-weight: 600;
}
.answer-review-issues[open] > summary {
  margin-bottom: 8px;
}
.answer-review-issues section + section {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--fn-warning-border, #e5bd70);
}
.answer-review-issues h4 {
  margin: 0 0 4px;
  font-size: 12px;
}
.answer-review-issues ul {
  margin: 0;
  padding-left: 18px;
}
.answer-review-issues li + li {
  margin-top: 6px;
}
.answer-review-issues small {
  display: block;
  color: var(--fn-text-secondary, #596273);
}
.stop-btn:disabled {
  opacity: 0.55;
  cursor: wait;
}

/* 思考过程 */
.thinking {
  margin-bottom: 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  overflow: hidden;
  background: linear-gradient(180deg, rgba(10, 89, 247, 0.035), transparent);
}
.process-truncated {
  margin: 8px 0;
  color: var(--fn-text-tertiary);
  font-size: 12px;
  line-height: 1.6;
}
.thinking-toggle {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: transparent;
  color: var(--fn-text-secondary);
  font-size: 12.5px;
  cursor: pointer;
}
.thinking-toggle .brain-icon,
.thinking-toggle svg {
  color: var(--fn-brand);
}
.thinking-label {
  font-weight: 500;
}
.thinking-chev {
  margin-left: auto;
  transition: transform 0.18s ease;
  color: var(--fn-text-tertiary);
}
.thinking-chev.open {
  transform: rotate(90deg);
}
.thinking-body {
  padding: 2px 14px 10px;
  border-top: 1px solid var(--fn-border-secondary);
}
.thinking-p {
  margin: 8px 0 0;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--fn-text-secondary);
  white-space: pre-wrap;
}
.thinking-toggle svg.pulse {
  animation: think-pulse 1.4s ease-in-out infinite;
}
@keyframes think-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}

/* ---- 待办清单 ---- */
.todo {
  margin-bottom: 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  overflow: hidden;
  background: var(--fn-bg-secondary);
}
.todo-head {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 12px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-secondary);
  border-bottom: 1px solid var(--fn-border-secondary);
}
.todo-head svg {
  color: var(--fn-brand);
}
.todo-count {
  margin-left: auto;
  font-weight: 500;
  color: var(--fn-text-tertiary);
}
.todo-list {
  padding: 8px 12px;
}
.todo-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 4px 0;
  font-size: 12.5px;
  color: var(--fn-text-tertiary);
}
.todo-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  border: 1.5px solid var(--fn-border-primary);
  border-radius: 50%;
}
.todo-item.done .todo-mark {
  border-color: var(--fn-brand);
  background: var(--fn-brand);
  color: #fff;
}
.todo-mark svg {
  width: 9px;
  height: 9px;
}
.todo-item.done .todo-title {
  color: var(--fn-text-secondary);
  text-decoration: line-through;
  text-decoration-color: var(--fn-text-tertiary);
}
.todo-item.in_progress {
  color: var(--fn-text-primary);
}
.todo-item.in_progress .todo-mark {
  border-color: var(--fn-brand);
}
.todo-spin {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1.5px solid var(--fn-brand);
  border-top-color: transparent;
  animation: todo-spin 0.7s linear infinite;
}
@keyframes todo-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 审批卡 ---- */
.approval {
  margin-bottom: 12px;
}
.turn-file-changes {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.turn-file-changes button {
  display: flex;
  gap: 6px;
  align-items: center;
  max-width: 100%;
  overflow-wrap: anywhere;
  padding: 8px 12px;
  font: inherit;
  font-size: 12px;
  border: 1px solid var(--fn-border);
  border-radius: 6px;
  background: var(--fn-bg-main);
  color: var(--fn-text-primary);
  cursor: pointer;
}
.turn-commands button > svg {
  flex-shrink: 0;
}
.turn-commands button > span {
  min-width: 0;
  text-align: left;
}
.turn-commands small {
  flex-shrink: 0;
  color: var(--fn-text-secondary);
}
.approval-card {
  border: 1px solid #f0b429;
  border-radius: 10px;
  background: rgba(240, 180, 41, 0.06);
  overflow: hidden;
}
.approval-top {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 12px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.approval-top svg {
  color: #d97706;
}
.approval-risk {
  margin-left: auto;
  padding: 1px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 500;
  color: #b45309;
  background: rgba(240, 180, 41, 0.18);
}
.approval-reason {
  padding: 0 12px 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--fn-text-secondary);
  word-break: break-all;
}
.approval-arguments {
  margin: 0 12px 10px;
  padding: 8px;
  max-height: 160px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font:
    12px/1.5 Consolas,
    'Cascadia Code',
    monospace;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 4px;
}
.approval-actions {
  display: flex;
  gap: 8px;
  padding: 9px 12px;
  border-top: 1px solid rgba(240, 180, 41, 0.25);
}
.apv-btn {
  flex: 1;
  padding: 7px 10px;
  border-radius: 7px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-primary);
  font-size: 12px;
  font-weight: 500;
  color: var(--fn-text-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
}
.apv-btn:hover {
  border-color: var(--fn-brand);
}
.apv-btn:disabled {
  opacity: 0.55;
  cursor: wait;
}
.apv-btn.deny {
  color: #dc2626;
}
.apv-btn.deny:hover {
  border-color: #dc2626;
  background: rgba(220, 38, 38, 0.05);
}
.apv-btn.session {
  background: var(--fn-brand);
  border-color: var(--fn-brand);
  color: #fff;
}

/* 子 agent 并行结果 */
.subagents {
  margin-bottom: 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  background: var(--fn-bg-secondary);
  overflow: hidden;
}
.subagents-head {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 10px 14px;
  font-size: 13px;
  font-weight: 600;
  color: var(--fn-text-primary);
  border-bottom: 1px solid var(--fn-border-primary);
}
.subagent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 10px;
  padding: 12px 14px;
}
.subagent-card {
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  background: var(--fn-bg-primary);
  padding: 10px 12px;
}
.subagent-topic {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-brand);
  margin-bottom: 6px;
}
.subagent-findings {
  font-size: 12px;
  line-height: 1.6;
  color: var(--fn-text-secondary);
  white-space: pre-wrap;
}

.process {
  margin-bottom: 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  overflow: hidden;
}
.process-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
  font-size: 12.5px;
  cursor: pointer;
}
.process-toggle svg {
  transition: transform 0.18s ease;
}
.process-toggle svg.open {
  transform: rotate(90deg);
}
.process-body {
  padding: 12px 16px 4px;
  border-top: 1px solid var(--fn-border-secondary);
}
.process-line {
  font-size: 12.5px;
  color: var(--fn-text-secondary);
  line-height: 1.6;
}
.kind-badge {
  display: inline-block;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--fn-bg-tertiary);
  color: var(--fn-text-secondary);
  margin-right: 6px;
}

.run-process {
  margin: 0 0 12px;
  min-width: 0;
}
.run-process-toggle {
  width: auto;
  max-width: 100%;
  min-height: 28px;
  padding: 3px 0;
  gap: 7px;
  border: 0;
  background: transparent;
  color: var(--fn-text-secondary);
  text-align: left;
  line-height: 1.6;
}
.run-process-toggle:disabled {
  cursor: default;
  opacity: 1;
}
.run-process-toggle:hover:not(:disabled) {
  color: var(--fn-text-primary);
}
.run-process-toggle:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 3px;
  border-radius: 3px;
}
.run-process-toggle svg {
  color: currentColor;
  flex-shrink: 0;
}
.run-process-toggle .thinking-label {
  flex-shrink: 0;
  font-weight: 400;
}
.run-process-toggle .thinking-chev {
  margin-left: 0;
}
.run-process-summary {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fn-text-tertiary);
  font-size: 11px;
}
.run-process-details {
  margin-top: 7px;
  padding: 0 0 0 14px;
  border-top: 0;
  border-left: 1px solid var(--fn-border-secondary);
  overflow-wrap: anywhere;
}
.run-process-details .thinking-body {
  padding: 0;
  border: 0;
}
.run-process-heading {
  margin: 0 0 5px;
  color: var(--fn-text-tertiary);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.6;
}
.process-thought + .process-thought,
.process-log,
.run-process-details .subagents {
  margin-top: 14px;
}
.run-process-details .thinking-p {
  margin-top: 0;
}
.run-process-details .todo {
  margin: 14px 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}
.run-process-details .todo-head {
  padding: 0;
  border: 0;
  font-weight: 500;
}
.run-process-details .todo-list {
  padding: 6px 0 0;
}
.run-process-details .subagents {
  padding: 0;
  border: 0;
  background: transparent;
}
.run-process-details .subagents-head {
  padding: 0 0 8px;
}
.public-progress {
  min-width: 0;
  margin: 6px 0 20px;
}
.public-progress-item + .public-progress-item {
  margin-top: 14px;
}
.public-commentary :deep(.markdown-answer > :first-child) {
  margin-top: 0;
}
.public-commentary :deep(.markdown-answer > :last-child) {
  margin-bottom: 0;
}
.public-tool-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
  min-height: 28px;
  padding: 3px 0;
  border: 0;
  background: transparent;
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 12px;
  line-height: 1.6;
  text-align: left;
  cursor: pointer;
}
.public-tool-toggle > svg {
  flex-shrink: 0;
  color: var(--fn-text-tertiary);
}
.public-tool-toggle > svg:first-child {
  transition: transform 0.18s ease;
}
.public-tool-toggle > svg.open {
  transform: rotate(90deg);
}
.public-tool-toggle:hover {
  color: var(--fn-text-primary);
}
.public-tool-toggle:focus-visible {
  outline: 2px solid var(--fn-brand);
  outline-offset: 3px;
  border-radius: 3px;
}
.public-tool-name,
.public-tool-argument {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.public-tool-name {
  max-width: 50%;
}
.public-tool-argument {
  flex: 1;
  color: var(--fn-text-tertiary);
}
.public-tool-status {
  flex-shrink: 0;
  margin-left: auto;
  color: var(--fn-text-tertiary);
  font-size: 11px;
}
.public-tool-details {
  margin: 5px 0 0 6px;
  padding: 8px 12px;
  border-left: 1px solid var(--fn-border-secondary);
  background: var(--fn-bg-secondary);
}
.public-tool-details pre {
  max-height: 300px;
  overflow: auto;
  margin: 0 0 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--fn-text-secondary);
  font-size: 12px;
  line-height: 1.65;
}
.public-tool-details pre:last-child {
  margin-bottom: 0;
}
.public-tool-pending {
  margin: 0;
  color: var(--fn-text-tertiary);
  font-size: 12px;
}
.process-log-body {
  padding: 8px 0 0;
}
@media (max-width: 560px) {
  .run-process-toggle {
    gap: 5px;
  }
  .run-process-details {
    padding-left: 10px;
  }
  .run-process-details .subagent-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (prefers-reduced-motion: reduce) {
  .run-process-toggle svg.pulse {
    animation: none;
  }
}

.assistant {
  margin-bottom: 12px;
  min-width: 0;
  padding: 4px 0;
}
.answer-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.answer-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 13px;
  padding-top: 11px;
  border-top: 1px solid var(--fn-border-secondary);
  flex-wrap: wrap;
}
.aa {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 30px;
  width: 30px;
  justify-content: center;
  padding: 0;
  border: none;
  background: transparent;
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--fn-text-tertiary);
  cursor: pointer;
  transition: all var(--fn-transition-fast);
}
.aa svg {
  width: 14px;
  height: 14px;
}
.aa:hover {
  background: var(--fn-bg-secondary);
  color: var(--fn-brand);
}
.aa-latency {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--fn-text-quaternary);
}
.aa-model {
  max-width: 170px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-left: 8px;
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.approval-notice {
  margin: 8px 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--fn-text-secondary);
}
.aa-usage {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-left: 10px;
  font-size: 11.5px;
  color: var(--fn-text-quaternary);
}
/* 多格式导出菜单 */
.export-menu {
  position: relative;
}
.export-pop {
  position: absolute;
  bottom: 100%;
  left: 0;
  margin-bottom: 6px;
  min-width: 200px;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  box-shadow: var(--fn-shadow-md);
  padding: 5px;
  z-index: 50;
}
.export-opt {
  display: block;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border: none;
  border-radius: 7px;
  background: transparent;
  font-size: 12.5px;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.export-opt:hover {
  background: var(--fn-bg-active);
  color: var(--fn-text-primary);
}
/* 定稿自检报告 */
.fc-report {
  margin-top: 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 11px;
  background: var(--fn-bg-secondary);
  padding: 12px 14px;
}
.fc-report.ok {
  border-color: #86efac;
  background: #f0fdf4;
}
.fc-report.pending {
  border-color: #facc15;
  background: #fffbeb;
}
.fc-head {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 10px;
}
.fc-checks {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.fc-check {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
}
.fc-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
  align-self: center;
}
.fc-dot.pass {
  background: #16a34a;
}
.fc-dot.fail {
  background: #dc2626;
}
.fc-dot.unverified {
  background: #d97706;
}
.fc-name {
  font-weight: 600;
  flex-shrink: 0;
  min-width: 96px;
}
.fc-detail {
  color: var(--fn-text-secondary);
  line-height: 1.5;
}
.fc-fix {
  margin-top: 11px;
  border-top: 1px dashed var(--fn-border-primary);
  padding-top: 9px;
}
.fc-fix-title {
  font-size: 12.5px;
  font-weight: 600;
  margin-bottom: 6px;
}
.fc-fix-item {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--fn-text-secondary);
}
.fc-fix-check {
  flex-shrink: 0;
  font-size: 10.5px;
  color: var(--fn-text-tertiary);
  min-width: 110px;
}
.gate-badge {
  font-size: 11.5px;
  padding: 2px 8px;
  border-radius: 999px;
}
.gate-badge.ok {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.gate-badge.refuse {
  background: var(--fn-warning-bg);
  color: var(--fn-warning-text);
}
.gate-badge.none {
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
}
.gate-badge.live {
  background: rgba(10, 89, 247, 0.1);
  color: var(--fn-brand);
}
.gate-badge.warn {
  background: var(--fn-warning-bg);
  color: var(--fn-warning-text);
}
.latency {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
}
.answer-text {
  font-size: 14px;
  line-height: 1.75;
  color: var(--fn-text-primary);
}
.answer-text.streaming {
  white-space: pre-wrap;
}
.answer-text .caret {
  color: var(--fn-brand);
  animation: caret-blink 1s step-end infinite;
  font-weight: 300;
}
@keyframes caret-blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}
.cite-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  min-width: 22px;
  height: 20px;
  padding: 0 5px;
  margin: 0 2px;
  border-radius: 5px;
  background: var(--fn-bg-active);
  color: var(--fn-brand);
  cursor: pointer;
  vertical-align: 1px;
}
.cite-chip:hover {
  background: var(--fn-brand);
  color: #fff;
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.evidence-card {
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  transition:
    border-color 0.16s ease,
    box-shadow 0.16s ease;
}
.evidence-card:hover {
  border-color: var(--fn-brand);
  box-shadow: 0 2px 8px rgba(10, 89, 247, 0.08);
}
.evidence-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.ev-title {
  color: var(--fn-text-secondary);
  font-weight: 500;
}
.ev-quote {
  font-size: 13px;
  color: var(--fn-text-secondary);
  line-height: 1.6;
}

/* ---------- 外部检索结果 / 一键入库 ---------- */
.external-list {
  margin-top: 14px;
  border-top: 1px solid #eceef2;
  padding-top: 12px;
}
.external-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #4a5160;
  margin-bottom: 10px;
}
.external-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  border: 1px solid #e6e8ee;
  border-radius: 10px;
  padding: 11px 14px;
  margin-bottom: 8px;
  background: #fff;
}
.external-info {
  min-width: 0;
}
.external-title {
  font-size: 13.5px;
  font-weight: 600;
  color: #1f2430;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.external-meta {
  font-size: 12px;
  color: #8a909c;
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.import-btn {
  flex-shrink: 0;
  border: 1px solid #0a59f7;
  background: #0a59f7;
  color: #fff;
  border-radius: 8px;
  padding: 7px 16px;
  font-size: 12.5px;
  cursor: pointer;
  transition: all 0.15s;
}
.import-btn:hover {
  background: #094ad0;
}
.import-btn:disabled {
  cursor: default;
}
.import-btn.done {
  background: #e9f7ee;
  border-color: #bfe6cd;
  color: #1a9e54;
}
.import-btn.fail {
  background: #fff;
  border-color: #e0a4a4;
  color: #d04545;
}

/* ---------- 会话工具条 ---------- */
.conv-bar {
  display: flex;
  justify-content: flex-end;
  padding: 6px 24px 0;
  background: #f5f7fa;
}
.conv-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid #d9dce3;
  background: #fff;
  color: #4a5160;
  border-radius: 8px;
  padding: 5px 12px;
  font-size: 12.5px;
  cursor: pointer;
  transition: all 0.15s;
}
.conv-btn svg {
  width: 13px;
  height: 13px;
}
.conv-btn:hover {
  border-color: #0a59f7;
  color: #0a59f7;
}

/* ---------- 历史会话抽屉 ---------- */
.hist-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.32);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}
.hist-drawer {
  width: 440px;
  max-width: 90vw;
  height: 100%;
  background: var(--fn-bg-primary);
  display: flex;
  flex-direction: column;
  box-shadow: -12px 0 40px rgba(15, 23, 42, 0.18);
  animation: hist-slide 0.2s ease;
}
@keyframes hist-slide {
  from {
    transform: translateX(24px);
    opacity: 0.6;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}
.hist-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 15px 16px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.hist-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.hist-search {
  padding: 12px 16px 8px;
}
.archive-notice {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
  min-height: 42px;
  padding: 6px 24px;
  border-bottom: 1px solid var(--fn-border-primary);
  color: var(--fn-text-secondary);
  font-size: 13px;
}
.archive-notice span {
  flex: 1;
}
.archive-notice button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 28px;
  border: 0;
  border-radius: 4px;
  padding: 4px 8px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
  cursor: pointer;
}
.archive-notice button:disabled,
.hist-act:disabled {
  opacity: 0.4;
  cursor: default;
}
.hist-tabs {
  display: flex;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.hist-tabs button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 40px;
  min-width: 78px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.hist-tabs button[aria-selected='true'] {
  color: var(--fn-brand);
  border-bottom-color: var(--fn-brand);
}
.hist-load-error {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 20px 0;
  color: var(--fn-text-secondary);
  font-size: 13px;
}
.hist-load-error button {
  border: 0;
  background: transparent;
  color: var(--fn-brand);
  cursor: pointer;
}
.archive-operation-error {
  justify-content: flex-start;
  flex-wrap: wrap;
  padding: 10px 16px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.archive-operation-error span {
  flex: 1 1 180px;
}
.hist-search-input {
  width: 100%;
  height: 34px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  padding: 0 12px;
  font-size: 13px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
  box-sizing: border-box;
  outline: none;
}
.hist-search-input:focus {
  border-color: var(--fn-brand);
}
.hist-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 10px 16px;
}
.hist-item {
  display: flex;
  align-items: center;
  gap: 4px;
  border-radius: 6px;
  padding: 4px 6px 4px 8px;
  margin-bottom: 2px;
}
.hist-item:hover {
  background: var(--fn-bg-secondary);
}
.hist-item.active {
  background: var(--fn-bg-active);
}
.hist-item-main {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  text-align: left;
  cursor: pointer;
  padding: 6px 4px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.hist-item-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--fn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.hist-item-meta {
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.hist-item-actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.14s;
}
.hist-item:hover .hist-item-actions,
.hist-item:focus-within .hist-item-actions {
  opacity: 1;
}
.hist-act {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.hist-act:hover {
  background: var(--fn-bg-tertiary);
  color: var(--fn-brand);
}
.hist-act.danger:hover {
  color: #dc2626;
  background: rgba(220, 38, 38, 0.08);
}
.hist-rename {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 2px;
}
.hist-rename-input {
  flex: 1;
  min-width: 0;
  height: 32px;
  border: 1px solid var(--fn-brand);
  border-radius: 7px;
  padding: 0 9px;
  font-size: 13px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  outline: none;
}
.hist-rename-ok {
  width: 30px;
  height: 30px;
  border: none;
  border-radius: 7px;
  background: var(--fn-brand);
  color: #fff;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.hist-empty {
  padding: 40px 20px;
  text-align: center;
  font-size: 12.5px;
  color: var(--fn-text-tertiary);
}

/* ---------- 输入框 ---------- */
.execution-mode {
  display: inline-flex;
  flex-shrink: 0;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  padding: 2px;
  gap: 2px;
}
.execution-mode button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  width: 58px;
  height: 26px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.execution-mode button.selected {
  background: var(--fn-bg-active);
  color: var(--fn-brand);
}
.execution-mode button:disabled {
  opacity: 0.55;
  cursor: default;
}
.execute-plan {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 30px;
  padding: 4px 10px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 6px;
  background: var(--fn-bg-active);
  color: var(--fn-brand);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.execute-plan:disabled {
  opacity: 0.5;
  cursor: default;
}
.plan-badge {
  background: var(--fn-bg-active);
  color: var(--fn-brand);
}
.composer {
  flex-shrink: 0;
  padding: 12px 32px 16px;
  background: var(--fn-bg-primary);
}
.input-wrapper {
  position: relative;
  max-width: 960px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 20px;
  padding: 10px 12px 8px 16px;
  transition:
    border-color 0.12s ease,
    box-shadow 0.12s ease;
}
.input-wrapper:focus-within {
  border-color: var(--fn-brand);
  box-shadow: 0 0 0 2px var(--fn-bg-active);
}

/* ---------- 斜杠命令面板 ---------- */
.slash-panel {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + 8px);
  max-height: 320px;
  overflow-y: auto;
  background: var(--fn-bg-primary, #fff);
  border: 1px solid var(--fn-border-primary);
  border-radius: 14px;
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.14);
  padding: 6px;
  z-index: 40;
}
.slash-empty {
  padding: 16px;
  font-size: 12.5px;
  color: var(--fn-text-tertiary);
  text-align: center;
}
.slash-item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 9px 12px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: none;
  cursor: pointer;
  text-align: left;
}
.slash-item.active {
  background: var(--fn-bg-active);
}
.slash-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
  flex: 0 0 auto;
}
.slash-desc {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---------- 统一 @ mention ---------- */
.mention-panel {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + 8px);
  max-height: 320px;
  overflow-y: auto;
  background: var(--fn-bg-primary, #fff);
  border: 1px solid var(--fn-border-primary);
  border-radius: 14px;
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.14);
  padding: 6px;
  z-index: 45;
}
.mention-empty {
  padding: 16px;
  font-size: 12.5px;
  color: var(--fn-text-tertiary);
  text-align: center;
}
.mention-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: none;
  cursor: pointer;
  text-align: left;
}
.mention-item.active {
  background: var(--fn-bg-active);
}
.mention-type {
  flex: 0 0 auto;
  font-size: 10.5px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 6px;
  color: #fff;
}
.mention-type.paper {
  background: #0a59f7;
}
.mention-type.skill {
  background: #16a34a;
}
.mention-type.mcp {
  background: #c87f1a;
}
.mention-type.file {
  background: #53637b;
}
.project-context-row {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 7px;
}
.project-context-button,
.project-file-chip,
.terminal-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  max-width: 230px;
  height: 26px;
  background: transparent;
  color: var(--fn-text-secondary);
  border: 1px solid var(--fn-border);
  border-radius: 4px;
  font: inherit;
  font-size: 11px;
  padding: 3px 7px;
  cursor: pointer;
}
.project-context-button span,
.project-file-chip span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.project-context-button svg,
.project-file-chip svg {
  flex-shrink: 0;
}
.project-context-button:hover,
.project-file-chip:hover,
.terminal-toggle:hover {
  background: var(--fn-bg-hover);
}
.project-unavailable {
  color: #b42318;
  font-size: 11px;
}
.mention-label {
  flex: 1;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.mention-hint {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--fn-text-tertiary);
  white-space: nowrap;
}

/* ---------- 状态面板 ---------- */
.status-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 80;
}
.status-modal {
  width: 560px;
  max-width: calc(100vw - 40px);
  max-height: calc(100vh - 60px);
  overflow-y: auto;
  background: var(--fn-bg-primary, #fff);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
}
.status-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--fn-border-primary);
}
.status-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.status-body {
  padding: 8px 20px 18px;
}
.status-section {
  padding: 12px 0;
}
.status-section-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--fn-text-tertiary);
  letter-spacing: 0.04em;
  margin-bottom: 8px;
}
.status-grid {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 8px 14px;
}
.status-grid .k {
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.status-grid .v {
  font-size: 12.5px;
  color: var(--fn-text-primary);
  word-break: break-all;
}
.status-grid .v.mono {
  font-family: ui-monospace, monospace;
  font-size: 11.5px;
}
.shortcut-grid {
  display: grid;
  grid-template-columns: 132px 1fr;
  gap: 7px 14px;
}
.shortcut-grid .k {
  font-size: 11.5px;
  font-family: ui-monospace, monospace;
  color: var(--fn-text-secondary);
}
.shortcut-grid .v {
  font-size: 12.5px;
  color: var(--fn-text-primary);
}
.status-empty {
  font-size: 12px;
  color: var(--fn-text-quaternary);
}
.status-goal {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 9px;
}
.status-goal-desc {
  font-size: 12.5px;
  color: var(--fn-text-primary, #1f2430);
  line-height: 1.5;
}
.status-goal-btn {
  margin-top: 4px;
}
.diag-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 82;
}
.diag-modal {
  width: 600px;
  max-width: calc(100vw - 40px);
  max-height: calc(100vh - 60px);
  overflow-y: auto;
  background: var(--fn-bg-primary, #fff);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
}

/* ---------- 持久目标 /goal ---------- */
.goal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 82;
}
.goal-modal {
  width: 600px;
  max-width: calc(100vw - 40px);
  max-height: calc(100vh - 60px);
  overflow-y: auto;
  background: var(--fn-bg-primary, #fff);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
  padding: 18px 20px 20px;
}
.goal-current {
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  padding: 14px;
  margin-bottom: 16px;
  background: var(--fn-bg-secondary, #f7f8fa);
}
.goal-current.active {
  border-color: var(--fn-brand);
}
.goal-current-head {
  margin-bottom: 8px;
}
.goal-status-tag {
  font-size: 11px;
  font-weight: 700;
  padding: 2px 9px;
  border-radius: 20px;
  background: var(--fn-brand);
  color: #fff;
}
.goal-current.paused .goal-status-tag {
  background: #f59e0b;
}
.goal-current.done .goal-status-tag {
  background: #16a34a;
}
.goal-current.cancelled .goal-status-tag {
  background: #9ca3af;
}
.goal-current-desc {
  font-size: 14px;
  line-height: 1.6;
  color: var(--fn-text-primary, #1f2430);
  margin-bottom: 12px;
}
.goal-current-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.goal-new-label {
  display: block;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-secondary);
  margin-bottom: 7px;
}
.goal-new textarea {
  width: 100%;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
  background: var(--fn-bg-primary, #fff);
  color: var(--fn-text-primary, #1f2430);
  box-sizing: border-box;
}
.goal-new-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
.btn-mini.primary {
  background: var(--fn-brand);
  color: #fff;
  border-color: var(--fn-brand);
}
.goal-history {
  margin-top: 18px;
}
.goal-history-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--fn-text-secondary);
  margin-bottom: 9px;
}
.goal-history-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 0;
  border-bottom: 1px solid var(--fn-border-secondary);
}
.goal-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: #9ca3af;
}
.goal-history-item.active .goal-dot {
  background: var(--fn-brand);
}
.goal-history-item.paused .goal-dot {
  background: #f59e0b;
}
.goal-history-item.done .goal-dot {
  background: #16a34a;
}
.goal-history-desc {
  flex: 1;
  font-size: 12.5px;
  color: var(--fn-text-primary, #1f2430);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.goal-history-status {
  font-size: 11px;
  color: var(--fn-text-tertiary);
  flex-shrink: 0;
}

/* ---------- 后台进程 /ps ---------- */
.ps-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 83;
}
.ps-modal {
  width: 560px;
  max-width: calc(100vw - 40px);
  max-height: calc(100vh - 60px);
  overflow-y: auto;
  background: var(--fn-bg-primary, #fff);
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
  padding: 18px 20px 20px;
}
.ps-empty {
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
  color: var(--fn-text-tertiary);
}
.ps-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ps-item {
  display: flex;
  align-items: center;
  gap: 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  padding: 12px 14px;
}
.ps-item-main {
  flex: 1;
  min-width: 0;
}
.ps-item-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--fn-text-primary, #1f2430);
  margin-bottom: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ps-item-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ps-status-tag {
  font-size: 11px;
  font-weight: 700;
  padding: 2px 9px;
  border-radius: 20px;
  background: var(--fn-brand);
  color: #fff;
}
.ps-status-tag.pending {
  background: #9ca3af;
}
.ps-status-tag.done {
  background: #16a34a;
}
.ps-status-tag.failed {
  background: #dc2626;
}
.ps-status-tag.cancelled {
  background: #9ca3af;
}
.ps-progress {
  font-size: 11.5px;
  color: var(--fn-text-secondary);
}

.diag-loading {
  padding: 44px;
  text-align: center;
  font-size: 13px;
  color: var(--fn-text-tertiary);
}
.diag-body {
  padding: 16px 20px 18px;
}
.diag-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 14px;
  border-radius: 12px;
  margin-bottom: 14px;
}
.diag-summary.ok {
  background: #e9f9ef;
}
.diag-summary.warn {
  background: #fdf3e3;
}
.diag-summary.error {
  background: #fdecec;
}
.diag-badge {
  font-size: 13px;
  font-weight: 700;
}
.diag-summary.ok .diag-badge {
  color: #16a34a;
}
.diag-summary.warn .diag-badge {
  color: #c87f1a;
}
.diag-summary.error .diag-badge {
  color: #dc2626;
}
.diag-counts {
  font-size: 12px;
  color: var(--fn-text-secondary);
}
.diag-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--fn-text-quaternary);
}
.diag-list {
  display: flex;
  flex-direction: column;
  gap: 11px;
}
.diag-item {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.diag-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  margin-top: 5px;
  flex-shrink: 0;
}
.diag-item.ok .diag-dot {
  background: #16a34a;
}
.diag-item.warn .diag-dot {
  background: #f59e0b;
}
.diag-item.error .diag-dot {
  background: #dc2626;
}
.diag-content {
  flex: 1;
}
.diag-label {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.diag-detail {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  line-height: 1.6;
  margin-top: 2px;
}
.diag-rerun {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
.status-quick {
  display: flex;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--fn-border-primary);
}
.textarea {
  border: none;
  outline: none;
  background: transparent;
  resize: none;
  width: 100%;
  font-size: 14px;
  line-height: 1.6;
  min-height: 44px;
  max-height: 160px;
  font-family: inherit;
  color: var(--fn-text-primary);
}
.textarea::placeholder {
  color: var(--fn-text-placeholder);
}
.actions-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 8px;
  border-top: 1px solid var(--fn-border-secondary);
  margin-top: 6px;
}
.tools {
  display: flex;
  align-items: center;
  gap: 8px;
}
.scope-select {
  height: 32px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 0 8px;
  background: var(--fn-bg-primary);
  font-size: 12px;
  color: var(--fn-text-secondary);
  max-width: 200px;
}

/* ---- 技能选择 ---- */
.skill-picker {
  position: relative;
}
.skill-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--fn-transition-fast);
}
.skill-btn:hover,
.skill-btn.active {
  border-color: var(--fn-brand);
  color: var(--fn-brand);
  background: var(--fn-bg-active);
}
.skill-btn-n {
  background: var(--fn-brand);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  min-width: 15px;
  height: 15px;
  padding: 0 4px;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.skill-pop {
  position: absolute;
  bottom: 38px;
  left: 0;
  width: 320px;
  max-width: 80vw;
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  box-shadow: 0 10px 34px rgba(20, 30, 60, 0.16);
  z-index: 60;
  overflow: hidden;
}
.skill-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 13px;
  font-size: 12px;
  font-weight: 600;
  border-bottom: 1px solid var(--fn-border-secondary);
}
.skill-clear {
  border: none;
  background: none;
  color: var(--fn-brand);
  font-size: 11px;
  cursor: pointer;
}
.skill-pop-list {
  max-height: 260px;
  overflow-y: auto;
  padding: 6px;
}
.skill-opt {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.skill-opt:hover {
  background: var(--fn-bg-secondary);
}
.skill-opt.sel {
  background: var(--fn-bg-active);
}
.skill-opt-check {
  flex: none;
  width: 17px;
  height: 17px;
  margin-top: 1px;
  border: 1.5px solid var(--fn-border-primary);
  border-radius: 5px;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
}
.skill-opt.sel .skill-opt-check {
  background: var(--fn-brand);
  border-color: var(--fn-brand);
}
.skill-opt-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.skill-opt-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.skill-opt-desc {
  font-size: 11px;
  color: var(--fn-text-tertiary);
  line-height: 1.5;
}
.skill-pop-empty {
  padding: 18px;
  text-align: center;
  font-size: 12px;
  color: var(--fn-text-tertiary);
}

/* 已选技能 chips（输入框内） */
.skill-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-top: 8px;
}
.skill-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 5px 0 9px;
  background: var(--fn-bg-active);
  color: var(--fn-brand);
  font-size: 11px;
  font-weight: 500;
  border-radius: 7px;
}
.chip-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--fn-brand);
  cursor: pointer;
}
.chip-x:hover {
  background: rgba(10, 89, 247, 0.16);
}

/* 用户问题下的技能标记 */
.user-col {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 5px;
}
.turn-skills {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  justify-content: flex-end;
}
.turn-skill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--fn-brand);
  background: var(--fn-bg-active);
  padding: 1px 7px;
  border-radius: 999px;
}
.turn-file {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 260px;
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.turn-file svg {
  flex-shrink: 0;
}
.turn-file span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.input-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 999px;
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: pointer;
}
.icon-btn:hover {
  color: var(--fn-brand);
  background: rgba(10, 89, 247, 0.08);
}
.icon-btn.active {
  color: var(--fn-brand);
  background: rgba(10, 89, 247, 0.1);
}
.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
/* 待发送图片预览 */
.image-previews {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 14px 0;
}
.image-preview {
  position: relative;
  width: 56px;
  height: 56px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--fn-border-primary);
}
.image-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.image-preview-x {
  position: absolute;
  top: 2px;
  right: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  cursor: pointer;
}
.bg-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-secondary);
  cursor: pointer;
  transition:
    color 0.15s ease,
    border-color 0.15s ease;
}
.bg-btn:hover:not(:disabled) {
  color: var(--fn-brand);
  border-color: var(--fn-brand);
}
.bg-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.send-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  background: linear-gradient(135deg, var(--fn-brand), #4d83f9);
  color: #fff;
  cursor: pointer;
  transition:
    transform 0.15s ease,
    box-shadow 0.15s ease;
}
.send-btn:hover:not(:disabled) {
  transform: scale(1.05);
  box-shadow: 0 4px 14px var(--fn-brand-shadow);
}
.send-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.stop-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.disclaimer {
  max-width: 960px;
  margin: 8px auto 0;
  font-size: 10.5px;
  color: var(--fn-text-tertiary);
  text-align: center;
}

.run-input-list {
  margin: 8px 0;
  max-height: 180px;
  overflow-y: auto;
  border-top: 1px solid var(--fn-border-primary);
}
.run-input-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 2px;
  border-bottom: 1px solid var(--fn-border-primary);
  font-size: 11px;
}
.run-input-copy {
  min-width: 0;
  flex: 1;
}
.run-input-kind {
  color: var(--fn-text-secondary);
  margin-right: 8px;
}
.run-input-content {
  overflow-wrap: anywhere;
}
.run-input-state {
  display: block;
  margin-top: 3px;
  color: var(--fn-text-tertiary);
}
.run-input-item button {
  border: 0;
  background: transparent;
  color: var(--fn-brand);
  cursor: pointer;
  font: inherit;
}
.run-input-error {
  margin: 6px 0;
  color: var(--fn-danger, #b42318);
  font-size: 12px;
}
.run-input-mode select,
.run-input-send {
  flex-shrink: 0;
  white-space: nowrap;
  min-height: 30px;
  max-width: 140px;
  border: 1px solid var(--fn-border-primary);
  border-radius: var(--fn-radius-md);
  background: var(--fn-bg-primary);
  color: var(--fn-text-secondary);
  font: inherit;
  font-size: 11px;
}
.run-input-mode select {
  padding: 4px;
}
.run-input-mode {
  flex-shrink: 0;
}
.actions-row.with-run-input {
  flex-wrap: wrap;
}
.actions-row.with-run-input .tools {
  flex: 1 1 100%;
}
.actions-row.with-run-input .input-actions {
  margin-left: auto;
  flex-shrink: 0;
}
.run-input-send {
  padding: 4px 8px;
  cursor: pointer;
}
.run-input-send:disabled,
.run-input-item button:disabled {
  opacity: 0.5;
  cursor: default;
}
@media (max-width: 700px) {
  .input-actions {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .run-input-mode select {
    max-width: 112px;
  }
}
.queued-commands {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 960px;
  margin: 8px auto 0;
  padding: 6px 10px;
  border: 1px solid var(--fn-border-primary);
  border-radius: var(--fn-radius-md);
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
  font-size: 11px;
}
.queued-command-list {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  color: var(--fn-text-tertiary);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queued-commands button {
  flex-shrink: 0;
  border: 0;
  background: transparent;
  color: var(--fn-brand);
  font: inherit;
  cursor: pointer;
}

/* ---------- 弹窗 ---------- */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.42);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}
.modal {
  width: 100%;
  max-width: 760px;
  max-height: 88vh;
  background: var(--fn-bg-primary);
  border-radius: 14px;
  box-shadow: 0 18px 60px rgba(15, 23, 42, 0.28);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  font-size: 15px;
  font-weight: 600;
  border-bottom: 1px solid var(--fn-border-primary);
}
.modal-x {
  border: none;
  background: transparent;
  color: var(--fn-text-tertiary);
  cursor: pointer;
  display: inline-flex;
}
.modal-x:hover {
  color: var(--fn-text-primary);
}
.modal-body {
  padding: 16px 18px;
  overflow-y: auto;
}
.review-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 12px;
}
.rv-file-btn,
.rv-run-btn {
  padding: 8px 14px;
  border-radius: 9px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-secondary);
  color: var(--fn-text-secondary);
}
.rv-run-btn {
  margin-left: auto;
  background: var(--fn-brand);
  border-color: var(--fn-brand);
  color: #fff;
}
.rv-run-btn:disabled {
  opacity: 0.55;
  cursor: wait;
}
.review-input {
  width: 100%;
  padding: 11px 13px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  background: var(--fn-bg-secondary);
  font-size: 13px;
  line-height: 1.65;
  resize: vertical;
  font-family: inherit;
}
.review-input:focus {
  outline: none;
  border-color: var(--fn-brand);
}
.review-result {
  margin-top: 16px;
}
.rv-overall {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  font-size: 13px;
  line-height: 1.65;
  color: var(--fn-text-secondary);
  background: var(--fn-bg-secondary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 12px 14px;
}
.rv-verdict {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--fn-bg-active);
  color: var(--fn-text-primary);
}
.rv-verdict.major_revision {
  background: #fee2e2;
  color: #b91c1c;
}
.rv-verdict.minor_revision {
  background: #fef3c7;
  color: #b45309;
}
.rv-verdict.accept {
  background: #dcfce7;
  color: #15803d;
}
.rv-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin: 12px 0 4px;
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.rv-block-title {
  font-size: 13px;
  font-weight: 600;
  margin: 14px 0 8px;
}
.rv-strengths ul {
  margin: 0;
  padding-left: 18px;
}
.rv-strengths li {
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--fn-text-secondary);
}
.rv-issue {
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 9px;
  background: var(--fn-bg-secondary);
}
.rv-issue.high {
  border-left: 3px solid #dc2626;
}
.rv-issue.mid {
  border-left: 3px solid #d97706;
}
.rv-issue.low {
  border-left: 3px solid #64748b;
}
.rv-issue-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rv-sev {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--fn-bg-active);
}
.rv-issue.high .rv-sev {
  background: #fee2e2;
  color: #b91c1c;
}
.rv-issue.mid .rv-sev {
  background: #fef3c7;
  color: #b45309;
}
.rv-issue.low .rv-sev {
  background: #e2e8f0;
  color: #475569;
}
.rv-dim {
  font-size: 11px;
  color: var(--fn-text-tertiary);
}
.rv-issue-title {
  font-size: 13px;
  font-weight: 600;
}
.rv-issue-detail {
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--fn-text-secondary);
  margin-top: 6px;
}
/* Supplied reference: quiet research canvas and a single composite input border. */
.is-new:not(.terminal-open) {
  justify-content: center;
  padding-bottom: 7vh;
  overflow-y: auto;
}
.is-new:not(.terminal-open) .chat-scroll-wrap {
  flex: 0 0 auto;
}
.is-new:not(.terminal-open) .chat-scroll {
  height: auto;
  overflow: visible;
}
.is-new:has(.journey):not(.terminal-open) {
  justify-content: flex-start;
  padding-bottom: 0;
}
.is-new:has(.journey):not(.terminal-open) .chat-scroll-wrap {
  flex: 1 1 auto;
}
.is-new:has(.journey):not(.terminal-open) .chat-scroll {
  height: 100%;
  overflow-y: auto;
}
.welcome {
  min-height: 0;
  padding: 32px 32px 20px;
}
.welcome-inner {
  max-width: 720px;
  align-items: flex-start;
  gap: 10px;
}
.welcome-title {
  font-size: 28px;
  font-weight: 500;
  letter-spacing: -0.7px;
  text-align: left;
}
.welcome-subtitle {
  font-size: 14px;
  text-align: left;
  color: var(--fn-text-secondary);
}
.quick-section {
  margin-top: 15px;
}
.quick-section-title {
  display: none;
}
.quick-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.quick-card {
  padding: 6px 9px;
  border: 0;
  border-radius: 6px;
  background: var(--fn-bg-secondary);
  gap: 6px;
}
.quick-card:hover {
  box-shadow: none;
  transform: none;
  background: var(--fn-bg-active);
}
.quick-card-icon {
  width: 22px;
  height: 22px;
  background: transparent;
  color: var(--fn-text-secondary);
}
.quick-card-title {
  font-size: 12px;
  font-weight: 400;
}
.quick-card-desc {
  display: none;
}
.composer {
  padding: 12px 32px 20px;
}
.input-wrapper,
.project-context-row,
.draft-save-state {
  width: 100%;
  max-width: 720px;
  margin-left: auto;
  margin-right: auto;
}
.input-wrapper {
  border: 1px solid var(--input-border);
  border-radius: var(--composer-radius);
  padding: 14px 14px 10px;
  box-shadow: 0 1px 3px #1b233808;
}
.input-wrapper:hover {
  border-color: var(--input-hover);
}
.input-wrapper:focus-within {
  border-color: var(--input-focus);
  box-shadow: var(--input-focus-shadow);
}
.textarea {
  min-height: 48px;
  font-size: 14px;
  line-height: 1.8;
}
.textarea:focus,
.textarea:focus-visible {
  border: 0;
  outline: none;
  box-shadow: none;
}
.messages {
  max-width: 808px;
  padding: 36px 44px 24px;
}
.user-bubble {
  max-width: 100%;
  padding: 15px 19px;
  background: var(--user-bg);
  border-radius: 14px 14px 3px 14px;
  line-height: 1.85;
}
.send-btn {
  border: 0;
  border-radius: 8px;
  background: var(--composer-action);
}
.send-btn:hover:not(:disabled) {
  transform: none;
  box-shadow: none;
  background: var(--composer-action-hover);
}
.send-btn:disabled {
  background: var(--fn-bg-tertiary);
  color: var(--fn-text-tertiary);
  opacity: 1;
}
@media (max-width: 1000px) {
  .composer {
    padding: 12px 24px 16px;
  }
  .messages {
    padding: 28px 24px;
  }
  .welcome {
    padding: 24px;
  }
}
@media (max-height: 720px) {
  .is-new:not(.terminal-open) {
    justify-content: flex-start;
    padding-bottom: 0;
  }
  .welcome {
    padding-top: 24px;
  }
}

/* Reference workbench: conversation plus a 320px research inspector. */
.chat-page {
  position: relative;
}
.chat-page.sources-open {
  padding-right: var(--inspector-width, 320px);
}
.research-inspector {
  position: absolute;
  inset: 0 0 0 auto;
  width: var(--inspector-width, 320px);
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--source-surface);
  z-index: 5;
}
.inspector-tabs {
  height: 55px;
  display: flex;
  align-items: stretch;
  gap: 20px;
  padding: 0 22px;
  border-bottom: 1px solid var(--border);
  flex: none;
}
.inspector-tabs > button {
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 0;
  background: none;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
}
.inspector-tabs > button.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
}
.inspector-tabs .inspector-close {
  margin-left: auto;
}
.inspector-content {
  padding: 22px;
  overflow: auto;
  flex: 1;
  min-height: 0;
}
.source-intro {
  font-size: 12px;
  line-height: 1.9;
  color: var(--muted);
  margin: 0 0 20px;
}
.source-item {
  padding: 16px 11px;
  margin: 0 -11px 5px;
  border-radius: 8px;
}
.source-item.active {
  background: var(--accent-soft);
}
.source-number {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--faint);
  font-size: 10px;
  margin-bottom: 10px;
}
.source-type {
  margin-left: auto;
}
.source-title {
  border: 0;
  padding: 0;
  background: none;
  text-align: left;
  color: var(--text);
  font-size: 13px;
  line-height: 1.7;
  font-weight: 500;
  cursor: pointer;
}
.source-title:hover {
  color: var(--primary);
}
.source-meta {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.8;
  margin-top: 7px;
}
.source-summary {
  font-size: 12px;
  line-height: 1.9;
  color: var(--muted);
  margin: 13px 0;
  overflow-wrap: anywhere;
}
.source-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  margin: 6px 14px 6px 0;
  border: 0;
  background: none;
  color: var(--primary);
  font-size: 11px;
  cursor: pointer;
}
.source-link:hover {
  text-decoration: underline;
}
.source-evidence-links {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.source-evidence-links button {
  border: 1px solid var(--border);
  background: white;
  border-radius: 4px;
  padding: 3px 6px;
  color: var(--muted);
  font-size: 10px;
  cursor: pointer;
}
.output-title {
  font-size: 13px;
  line-height: 1.7;
  margin: 0 0 10px;
}
.inspector-steps {
  padding-left: 20px;
  color: var(--muted);
  font-size: 12px;
}
.inspector-steps li {
  margin: 16px 0;
  line-height: 1.8;
}
.inspector-steps small {
  display: block;
  font-size: 10px;
  color: var(--faint);
}
.answer-brand {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 18px;
}
.answer-brand img {
  width: 22px;
  height: 22px;
  object-fit: contain;
}
.answer-head {
  margin-bottom: 12px;
}
.quick-section {
  display: none;
}
.welcome {
  padding-bottom: 12px;
}
.welcome-inner {
  max-width: 680px;
}
.welcome-title {
  font-size: 27px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.welcome-mark {
  width: 32px;
  height: 32px;
  object-fit: contain;
  flex-shrink: 0;
}
.composer {
  padding: 12px 28px 20px;
}
.input-wrapper,
.project-context-row,
.draft-save-state {
  max-width: 680px;
}
.is-new .input-wrapper,
.is-new .project-context-row,
.is-new .draft-save-state {
  max-width: 680px;
}
.disclaimer {
  max-width: 680px;
  margin: 6px auto 0;
  text-align: left;
  font-size: 10px;
  color: var(--faint);
}
.draft-save-state:not(.attention) {
  display: none;
}
.welcome-shortcuts {
  display: flex;
  justify-content: center;
  flex-wrap: wrap;
  gap: 22px;
  margin: 22px auto 0;
}
.welcome-shortcuts button {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 0;
  padding: 5px 0;
  background: none;
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
}
.welcome-shortcuts button:hover {
  color: var(--text);
  text-decoration: underline;
}
.actions-row {
  border-top: 0;
  gap: 8px;
}
.scope-select {
  max-width: 140px;
  border: 0;
  background: transparent;
  font-size: 11px;
  padding: 0 4px;
}
.tools {
  flex-wrap: wrap;
  gap: 4px;
}
.skill-btn {
  border: 0;
  background: transparent;
  padding: 0 5px;
  height: 28px;
  font-size: 11px;
}
.execution-mode {
  border: 0;
  background: transparent;
}
.execution-mode button {
  border: 0;
  padding: 4px 5px;
  font-size: 11px;
}
.project-context-row {
  margin-bottom: 8px;
}
.project-context-row {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}
.composer-add {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  width: 28px;
  height: 28px;
  padding: 0;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}
.composer-add-wrap {
  position: relative;
  flex-shrink: 0;
}
.composer-add:hover,
.composer-add[aria-expanded='true'] {
  border-radius: 6px;
  background: var(--subtle);
  color: var(--text);
}
.composer-add-menu {
  position: absolute;
  bottom: 38px;
  left: 0;
  z-index: 60;
  min-width: 210px;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow-menu);
}
.composer-add-menu button {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  border: 0;
  border-radius: 5px;
  padding: 9px 10px;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.composer-add-menu button:hover:not(:disabled),
.composer-add-menu button:focus-visible {
  background: var(--accent-soft);
  color: var(--primary);
}
.composer-add-menu button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.composer-menu-count {
  margin-left: auto;
  color: var(--muted);
}
.composer-mode {
  border: 0;
  background: transparent;
  padding: 4px;
  color: var(--muted);
  font-size: 12px;
  max-width: 70px;
}
.project-context-button {
  border: 0;
  color: var(--primary);
  padding: 0;
}
.input-actions {
  gap: 3px;
}
.conv-bar {
  padding: 4px 28px;
  font-size: 11px;
}
@media (max-width: 1050px) {
  .chat-page.sources-open {
    padding-right: 0;
  }
  .research-inspector {
    width: 320px;
    box-shadow: -5px 0 30px #1b315a0c;
  }
}
</style>
