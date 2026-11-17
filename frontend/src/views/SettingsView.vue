<script setup lang="ts">
import ReleaseInfo from '../components/ReleaseInfo.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ModelProfilesManager from '../components/ModelProfilesManager.vue'
import BackupSettingsCard from '../components/BackupSettingsCard.vue'
import StorageSettingsCard from '../components/StorageSettingsCard.vue'
import DeepSeekToolsSettings from '../components/DeepSeekToolsSettings.vue'
import { listProjects, type Project } from '../projectApi'
import McpUserAuthorization from '../components/McpUserAuthorization.vue'
import PricingSettings from '../components/PricingSettings.vue'
import AccountQuotaSettings from '../components/AccountQuotaSettings.vue'
import {
  getHealth,
  getSettings,
  saveSettings,
  testConnection,
  discoverModels,
  listMcpServers,
  upsertMcpServer,
  connectMcpServer,
  disconnectMcpServer,
  setMcpEnabled,
  removeMcpServer,
  listSkills,
  getSkill,
  setSkillDisabled,
  createSkill,
  deleteSkill,
  trustSkill, listLegacySkills, migrateLegacySkill, updateSkill,
  getHookEvents,
  getHooks,
  saveHooks,
  testHook,
  listDiscoveredTools,
  getMcpPolicy,
  saveMcpPolicy,
  getAgentsMd,
  saveGlobalAgentsMd,
  initAgentsMd,
  type ModelSettings,
  type ProviderModel,
  type McpServer,
  type McpServerConfig,
  type Skill,
  type HookRun,
  type McpDiscoveredTool,
  type McpToolPolicy,
  type AgentsMdMeta,
} from '../api'
import {
  MODEL_PRESETS,
  connectionTestPayload,
  findModelPreset,
  modelDiscoveryPayload,
  needsNewApiKey,
  needsNewEmbeddingApiKey,
  normalizeBaseUrl,
  normalizeModelConnection,
  normalizeSingleLine,
  validateModelConnection,
  validateAgentSteps,
} from '../modelSettings'
import {
  Cpu,
  Activity,
  Database,
  ShieldCheck,
  HardDrive,
  FileText,
  Blocks,
  Plus,
  Trash2,
  Power,
  Pencil,
  GraduationCap,
  Zap,
} from 'lucide-vue-next'

const router = useRouter()
const settingsRoute = useRoute()
const activeTab = ref(settingsRoute.query.tab === 'data' ? 'data' : 'model')
watch(() => settingsRoute.query.tab, value => { if (value === 'data') activeTab.value = 'data' })
const loadError = ref('')
const tabs = [
  { key: 'model', label: '模型接口', icon: Cpu },
  { key: 'mcp', label: 'MCP 服务器', icon: Blocks },
  { key: 'skills', label: '技能库', icon: GraduationCap },
  { key: 'hooks', label: '生命周期钩子', icon: Zap },
  { key: 'status', label: '运行状态', icon: Activity },
  { key: 'data', label: '数据与存储', icon: Database },
  { key: 'compliance', label: '合规说明', icon: ShieldCheck },
]

const APPROVAL_MODES = [
  { key: 'read_only', label: '只读确认', desc: '外联/写入前均需确认，最稳妥' },
  { key: 'auto', label: '智能审批', desc: '高风险工具首次确认，可授权本会话' },
  { key: 'full_access', label: '全自动', desc: '全部自动执行，自担风险' },
]

const health = ref<any>(null)
const loaded = ref<ModelSettings | null>(null)
const credentialWarnings = computed(() => Object.values(loaded.value?.credential_errors || {}))
const loadingSettings = ref(true)
const form = reactive({
  llm_base_url: '',
  llm_api_key: '',
  llm_model: '',
  embedding_base_url: '',
  embedding_api_key: '',
  embedding_model: '',
  approval_mode: 'auto',
  work_rules: '',
  reasoning_effort: '',
  max_agent_steps: 24,
  desktop_notifications: true,
  memory_recall: true,
  memory_capture: true,
})

const REASONING_EFFORTS = [
  { key: '', label: '不传（模型默认）', desc: '不发送 reasoning_effort，兼容性最好' },
  { key: 'none', label: 'none', desc: '关闭推理（需模型支持）' },
  { key: 'minimal', label: 'minimal', desc: '思考最少，最快最省' },
  { key: 'low', label: 'low', desc: '轻度推理' },
  { key: 'medium', label: 'medium', desc: '均衡（多数推理模型默认）' },
  { key: 'high', label: 'high', desc: '深度推理，最慢最强' },
  { key: 'xhigh', label: 'xhigh', desc: '更深推理（需模型支持）' },
  { key: 'max', label: 'max', desc: '最高推理强度，可能增加耗时和费用（需模型支持）' },
]
const saving = ref(false)
const message = ref('')
const error = ref('')

const presets = MODEL_PRESETS
const presetChoice = ref('compatible')
function selectProvider(event: Event) {
  const choice = (event.target as HTMLSelectElement).value
  const preset = presets.find((item) => item.name === choice)
  if (preset) {
    form.llm_base_url = preset.base
    form.llm_model = preset.model
  }
  presetChoice.value = choice
}
const keyVisible = ref(false)
const clearModelKey = ref(false)
const clearEmbeddingKey = ref(false)
const testing = ref(false)
const testResult = ref<{ ok: boolean; message: string } | null>(null)
const fetchingModels = ref(false)
const discoveredModels = ref<ProviderModel[]>([])
const modelFilter = ref('')
const modelListResult = ref<{ ok: boolean; message: string } | null>(null)
const filteredModels = computed(() => {
  const query = modelFilter.value.trim().toLocaleLowerCase()
  return discoveredModels.value.filter((model) => model.id.toLocaleLowerCase().includes(query))
})
const selectedDiscoveredModel = computed(() =>
  filteredModels.value.some((model) => model.id === form.llm_model) ? form.llm_model : ''
)
const newKeyRequired = computed(() => needsNewApiKey(loaded.value, form, clearModelKey.value))
const newEmbeddingKeyRequired = computed(() =>
  needsNewEmbeddingApiKey(loaded.value, form, clearEmbeddingKey.value)
)
let testRequestSeq = 0
let modelListRequestSeq = 0
let formRevision = 0

function invalidateTest() {
  testRequestSeq += 1
  testing.value = false
  testResult.value = null
  message.value = ''
  error.value = ''
}

function invalidateModels() {
  modelListRequestSeq += 1
  fetchingModels.value = false
  discoveredModels.value = []
  modelFilter.value = ''
  modelListResult.value = null
}

watch(
  [() => form.llm_base_url, () => form.llm_api_key, clearModelKey, presetChoice],
  invalidateModels,
  { flush: 'sync' }
)

watch(
  form,
  () => {
    formRevision += 1
    invalidateTest()
  },
  { flush: 'sync' }
)
watch(presetChoice, invalidateTest, { flush: 'sync' })
watch(
  [clearModelKey, clearEmbeddingKey],
  () => {
    formRevision += 1
    invalidateTest()
  },
  { flush: 'sync' }
)
watch(
  () => form.llm_base_url,
  (value) => {
    if (presetChoice.value !== 'compatible' && findModelPreset(value) !== presetChoice.value) {
      presetChoice.value = 'compatible'
    }
  },
  { flush: 'sync' }
)

function modelSnapshot() {
  if (!loaded.value || loadingSettings.value) throw new Error('模型设置尚未加载完成，请稍后重试。')
  const snapshot = normalizeModelConnection(form)
  const validation = validateModelConnection(snapshot, loaded.value, clearModelKey.value)
  if (validation) throw new Error(validation)
  return snapshot
}

async function onTest() {
  if (testing.value || saving.value) return
  const requestSeq = ++testRequestSeq
  message.value = ''
  error.value = ''
  try {
    const snapshot = modelSnapshot()
    testing.value = true
    testResult.value = null
    const result = await testConnection(connectionTestPayload(snapshot, clearModelKey.value))
    if (requestSeq === testRequestSeq) testResult.value = result
  } catch (e: any) {
    if (requestSeq === testRequestSeq) {
      testResult.value = {
        ok: false,
        message: e?.response?.data?.detail || e?.message || '测试失败',
      }
    }
  } finally {
    if (requestSeq === testRequestSeq) testing.value = false
  }
}

async function onFetchModels() {
  if (fetchingModels.value || saving.value) return
  const requestSeq = ++modelListRequestSeq
  modelListResult.value = null
  discoveredModels.value = []
  modelFilter.value = ''
  try {
    if (!loaded.value || loadingSettings.value) {
      throw new Error('模型设置尚未加载完成，请稍后重试。')
    }
    const payload = modelDiscoveryPayload(form, loaded.value, clearModelKey.value)
    fetchingModels.value = true
    const result = await discoverModels(payload)
    if (requestSeq !== modelListRequestSeq) return
    discoveredModels.value = result.ok ? result.models : []
    modelListResult.value = { ok: result.ok, message: result.message }
  } catch (e) {
    if (requestSeq === modelListRequestSeq) {
      modelListResult.value = {
        ok: false,
        message: e instanceof Error ? e.message : '获取模型列表失败，请稍后重试。',
      }
    }
  } finally {
    if (requestSeq === modelListRequestSeq) fetchingModels.value = false
  }
}

function selectDiscoveredModel(event: Event) {
  const id = (event.target as HTMLSelectElement).value
  if (discoveredModels.value.some((model) => model.id === id)) form.llm_model = id
}

async function refreshHealth() {
  health.value = await getHealth()
}

function applySettings(settings: ModelSettings) {
  form.llm_base_url = settings.llm_base_url
  form.llm_model = settings.llm_model
  form.embedding_base_url = settings.embedding_base_url
  form.embedding_model = settings.embedding_model
  form.approval_mode = settings.approval_mode || 'auto'
  form.work_rules = settings.work_rules || ''
  form.reasoning_effort = settings.reasoning_effort || ''
  form.max_agent_steps = settings.max_agent_steps ?? 24
  form.desktop_notifications = settings.desktop_notifications !== false
  form.memory_recall = settings.memory_recall !== false
  form.memory_capture = settings.memory_capture !== false
}

async function refresh() {
  loadingSettings.value = true
  try {
    loaded.value = await getSettings()
    applySettings(loaded.value)
    presetChoice.value = findModelPreset(loaded.value.llm_base_url)
  } finally {
    loadingSettings.value = false
  }
}

// ---- MCP 服务器管理 ----
const mcpServers = ref<McpServer[]>([])
const mcpForm = reactive({
  editing: false,
  name: '',
  transport: 'stdio',
  enabled: true,
  command: '',
  argsText: '',
  envText: '',
  cwd: '',
  url: '',
  // 认证（仅 http 生效）
  authMode: 'none' as 'none' | 'bearer' | 'oauth' | 'user_oauth' | 'preserve',
  headersText: '',
  bearerToken: '',
  oauthClientId: '',
  oauthClientSecret: '',
  oauthTokenUrl: '',
  oauthScope: '',
  oauthIssuer: '',
})
const mcpBusy = ref(false)
const mcpMsg = ref('')
const mcpErr = ref('')

function resetMcpForm() {
  mcpForm.editing = false
  mcpForm.name = ''
  mcpForm.transport = 'stdio'
  mcpForm.enabled = true
  mcpForm.command = ''
  mcpForm.argsText = ''
  mcpForm.envText = ''
  mcpForm.cwd = ''
  mcpForm.url = ''
  mcpForm.authMode = 'none'
  mcpForm.headersText = ''
  mcpForm.bearerToken = ''
  mcpForm.oauthClientId = ''
  mcpForm.oauthClientSecret = ''
  mcpForm.oauthTokenUrl = ''
  mcpForm.oauthScope = ''
  mcpForm.oauthIssuer = ''
}

async function loadMcp() {
  mcpServers.value = await listMcpServers()
  // Refresh discovered schemas without overwriting unsaved policy edits.
  discoveredTools.value = (await listDiscoveredTools()).tools
}

function editMcpServer(s: McpServer) {
  mcpForm.editing = true
  mcpForm.authMode = 'preserve'
  mcpForm.name = s.name
  mcpForm.transport = s.transport
  mcpForm.enabled = s.enabled
  mcpForm.url = s.url
  mcpForm.command = s.command
  mcpForm.argsText = ''
  mcpForm.envText = ''
  mcpForm.cwd = ''
  mcpMsg.value = '正在编辑「' + s.name + '」，命令参数/环境变量需重新填写。'
}

function parseEnv(text: string): Record<string, string> {
  const env: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (!t || !t.includes('=')) continue
    const idx = t.indexOf('=')
    env[t.slice(0, idx).trim()] = t.slice(idx + 1).trim()
  }
  return env
}

async function saveMcpServer() {
  mcpBusy.value = true
  mcpMsg.value = ''
  mcpErr.value = ''
  try {
    const args = mcpForm.argsText.trim() ? mcpForm.argsText.trim().split(/\s+/) : []
    const cfg: McpServerConfig = {
      name: mcpForm.name.trim(),
      transport: mcpForm.transport,
      enabled: mcpForm.enabled,
      command: mcpForm.command,
      args,
      env: parseEnv(mcpForm.envText),
      cwd: mcpForm.cwd,
      url: mcpForm.url,
    }
    // 静态自定义头（key=value 每行一个）
    const staticHeaders = parseEnv(mcpForm.headersText)
    if (Object.keys(staticHeaders).length) cfg.headers = staticHeaders
    if (mcpForm.authMode === 'bearer' && mcpForm.bearerToken.trim()) {
      cfg.bearer_token = mcpForm.bearerToken.trim()
      cfg.oauth = {}
    }
    if (mcpForm.authMode === 'oauth' && mcpForm.oauthTokenUrl.trim()) {
      cfg.bearer_token = ''
      cfg.oauth = {
        client_id: mcpForm.oauthClientId.trim(),
        client_secret: mcpForm.oauthClientSecret.trim(),
        token_url: mcpForm.oauthTokenUrl.trim(),
        scope: mcpForm.oauthScope.trim(),
      }
    }
    if (mcpForm.authMode === 'user_oauth') {
      cfg.oauth = { flow: 'authorization_code', client_id: mcpForm.oauthClientId.trim(), issuer: mcpForm.oauthIssuer.trim(), scope: mcpForm.oauthScope.trim() }
      cfg.bearer_token = ''
    }
    if (mcpForm.authMode === 'none' && mcpForm.editing) {
      cfg.oauth = {}; cfg.bearer_token = ''; cfg.headers = {}
    }
    if (!cfg.name) {
      mcpErr.value = '请填写服务器名称'
      return
    }
    if (cfg.transport === 'stdio' && !cfg.command) {
      mcpErr.value = 'stdio 服务器需要填写启动命令'
      return
    }
    if (cfg.transport === 'http' && !cfg.url) {
      mcpErr.value = 'http 服务器需要填写地址'
      return
    }
    if (mcpForm.authMode === 'oauth' && !mcpForm.oauthTokenUrl.trim()) {
      mcpErr.value = 'OAuth 需要填写 Token 端点地址'
      return
    }
    const res = await upsertMcpServer(cfg)
    await loadMcp()
    resetMcpForm()
    if (res.status === 'connected') {
      mcpMsg.value = `「${cfg.name}」已连接，发现 ${res.tool_count} 个工具。`
    } else {
      mcpErr.value = `已保存，但连接失败：${res.error || '请检查配置'}`
    }
  } catch (e: any) {
    mcpErr.value = e?.response?.data?.detail || e?.message || '保存失败'
  } finally {
    mcpBusy.value = false
  }
}

async function onConnectMcp(s: McpServer) {
  mcpBusy.value = true
  try {
    const res = await connectMcpServer(s.name)
    if (res.status !== 'connected') {
      mcpErr.value = `连接失败：${res.error || '未知错误'}`
    }
    await loadMcp()
  } finally {
    mcpBusy.value = false
  }
}

async function onDisconnectMcp(s: McpServer) {
  mcpBusy.value = true
  try {
    await disconnectMcpServer(s.name)
    await loadMcp()
  } finally {
    mcpBusy.value = false
  }
}

async function onToggleMcp(s: McpServer) {
  mcpBusy.value = true
  try {
    await setMcpEnabled(s.name, !s.enabled)
    await loadMcp()
  } finally {
    mcpBusy.value = false
  }
}

async function onRemoveMcp(s: McpServer) {
  mcpBusy.value = true
  try {
    await removeMcpServer(s.name)
    await loadMcp()
    if (mcpForm.editing && mcpForm.name === s.name) resetMcpForm()
  } finally {
    mcpBusy.value = false
  }
}

function statusLabel(s: McpServer) {
  if (!s.enabled) return { text: '已停用', cls: 'off' }
  if (s.status === 'connected') return { text: '已连接', cls: 'on' }
  if (s.status === 'error') return { text: '连接错误', cls: 'err' }
  return { text: '未连接', cls: 'off' }
}

// ---- MCP 工具级管控 ----
const discoveredTools = ref<McpDiscoveredTool[]>([])
const toolPolicy = reactive<McpToolPolicy>({
  enabled_tools: [],
  disabled_tools: [],
  tool_rules: {},
})
const policyBusy = ref(false)
const policyMsg = ref('')
const policyErr = ref('')

async function loadToolPolicy() {
  const [toolsRes, polRes] = await Promise.all([listDiscoveredTools(), getMcpPolicy()])
  discoveredTools.value = toolsRes.tools
  const p = polRes.policy
  toolPolicy.enabled_tools = [...(p.enabled_tools || [])]
  toolPolicy.disabled_tools = [...(p.disabled_tools || [])]
  toolPolicy.tool_rules = JSON.parse(JSON.stringify(p.tool_rules || {}))
}

function isToolDisabled(name: string) {
  return toolPolicy.disabled_tools.includes(name)
}
function toggleTool(name: string) {
  if (isToolDisabled(name)) {
    toolPolicy.disabled_tools = toolPolicy.disabled_tools.filter((x) => x !== name)
  } else {
    toolPolicy.disabled_tools.push(name)
  }
}
function _ensureRule(name: string) {
  if (!toolPolicy.tool_rules[name]) toolPolicy.tool_rules[name] = {}
  return toolPolicy.tool_rules[name]
}
function getApproval(name: string) {
  return toolPolicy.tool_rules[name]?.approval || ''
}
function setApproval(name: string, val: string) {
  if (!val) {
    if (toolPolicy.tool_rules[name]) {
      delete toolPolicy.tool_rules[name].approval
    }
    return
  }
  _ensureRule(name).approval = val
}
function getTimeoutVal(name: string): string | number {
  const t = toolPolicy.tool_rules[name]?.timeout
  return t === undefined ? '' : t
}
function setTimeoutVal(name: string, val: string) {
  const trimmed = val.trim()
  if (!trimmed) {
    if (toolPolicy.tool_rules[name]) delete toolPolicy.tool_rules[name].timeout
    return
  }
  const n = Number(trimmed)
  if (n > 0 && n <= 600) _ensureRule(name).timeout = n
}

async function onSavePolicy() {
  policyBusy.value = true
  policyMsg.value = ''
  policyErr.value = ''
  try {
    // 剔除没有任何有效字段的空规则
    const cleanRules: McpToolPolicy['tool_rules'] = {}
    for (const [name, rule] of Object.entries(toolPolicy.tool_rules)) {
      const entry: { approval?: string; timeout?: number } = {}
      if (rule.approval) entry.approval = rule.approval
      if (rule.timeout) entry.timeout = rule.timeout
      if (Object.keys(entry).length) cleanRules[name] = entry
    }
    const res = await saveMcpPolicy({
      enabled_tools: [...toolPolicy.enabled_tools],
      disabled_tools: [...toolPolicy.disabled_tools],
      tool_rules: cleanRules,
    })
    const p = res.policy
    toolPolicy.tool_rules = JSON.parse(JSON.stringify(p.tool_rules || {}))
    policyMsg.value = '工具管控策略已保存，立即对 Agent 生效。'
  } catch (e: any) {
    policyErr.value = e?.response?.data?.detail || e?.message || '保存失败'
  } finally {
    policyBusy.value = false
  }
}

// ---- 技能库 ----
const skills = ref<Skill[]>([])
const activeSkill = ref<(Skill & { content: string }) | null>(null)
const skillBusy = ref(false)
const skillProject = ref('')
const skillProjects = ref<Project[]>([])
const legacySkills = ref<Skill[]>([])
const editingSkill = ref(false), skillDraft = ref('')
let skillLoadRequest = 0
let skillOpenRequest = 0
watch(skillProject, () => { ++skillOpenRequest; activeSkill.value = null; editingSkill.value = false; void loadSkills() })

async function loadSkills() {
  const request = ++skillLoadRequest, scope = skillProject.value
  const [loaded, projects, legacy] = await Promise.all([listSkills(scope), listProjects(), listLegacySkills()])
  if (request !== skillLoadRequest || scope !== skillProject.value) return
  skills.value = loaded; skillProjects.value = projects; legacySkills.value = legacy
}

async function openSkill(s: Skill) {
  const request = ++skillOpenRequest, scope = skillProject.value
  skillBusy.value = true
  try {
    const loaded = await getSkill(s.name, scope)
    if (request === skillOpenRequest && scope === skillProject.value) activeSkill.value = loaded
  } finally {
    skillBusy.value = false
  }
}

function closeSkill() {
  activeSkill.value = null
  editingSkill.value = false
}

async function saveSkillEdit() {
  const current = activeSkill.value, scope = skillProject.value
  if (!current?.revision) return
  skillBusy.value = true
  try {
    const changed = await updateSkill(current.name, { expected_revision: current.revision, title: current.title,
      description: current.description, content: skillDraft.value }, scope)
    if (scope === skillProject.value && activeSkill.value?.name === current.name) {
      activeSkill.value = changed; editingSkill.value = false
    }
    await loadSkills()
  } catch (e: any) { skillFormErr.value = e?.response?.data?.detail || '保存失败；编辑内容保留，请重新读取版本后处理' }
  finally { skillBusy.value = false }
}

function tierLabel(tier?: string) {
  if (tier === 'builtin') return { text: '内置', cls: 'builtin' }
  if (tier === 'user') return { text: '用户', cls: 'user' }
  if (tier === 'project') return { text: '项目', cls: 'project' }
  return { text: tier || '', cls: '' }
}

async function toggleSkillDisabled(s: Skill) {
  const next = !s.disabled
  await setSkillDisabled(s.name, next, skillProject.value)
  await loadSkills()
  if (activeSkill.value && activeSkill.value.name === s.name) {
    activeSkill.value = await getSkill(s.name, skillProject.value)
  }
}

async function reviewSkill() {
  const skill = activeSkill.value
  if (!skill?.revision) return
  try {
    await trustSkill(skill.name, skill.revision, skillProject.value)
    activeSkill.value = await getSkill(skill.name, skillProject.value)
    await loadSkills()
  } catch (e: any) { skillFormErr.value = e?.response?.data?.detail || '版本确认失败，请重新查看' }
}
async function migrateSkill(skill: Skill) {
  if (!skillProject.value || !skill.revision) return
  try {
    await migrateLegacySkill(skill.name, skill.revision, skillProject.value)
    await loadSkills()
    await openSkill(skill)
  } catch (e: any) { skillFormErr.value = e?.response?.data?.detail || '迁移失败' }
}

const skillForm = reactive({
  name: '',
  title: '',
  description: '',
  content: '',
})
const skillFormBusy = ref(false)
const skillFormMsg = ref('')
const skillFormErr = ref('')

async function submitNewSkill() {
  skillFormBusy.value = true
  skillFormMsg.value = ''
  skillFormErr.value = ''
  try {
    await createSkill({ ...skillForm }, skillProject.value)
    skillFormMsg.value = (skillProject.value ? '当前项目技能' : '用户技能') + '已创建，可在所属范围的聊天中用 $' + skillForm.name + ' 调用。'
    skillForm.name = ''
    skillForm.title = ''
    skillForm.description = ''
    skillForm.content = ''
    await loadSkills()
  } catch (e: any) {
    skillFormErr.value = e?.response?.data?.detail || e?.message || '创建失败'
  } finally {
    skillFormBusy.value = false
  }
}

async function removeUserSkill(s: Skill) {
  try {
    await deleteSkill(s.name, skillProject.value)
    await closeSkill()
    await loadSkills()
  } catch (e: any) {
    skillFormErr.value = e?.response?.data?.detail || e?.message || '删除失败'
  }
}

// ---- 生命周期钩子 ----
const hookEvents = ref<string[]>([])
const hookDecisionEvents = ref<string[]>([])
const hookConfig = reactive<Record<string, any[]>>({})
const hookBusy = ref(false)
const hookMsg = ref('')
const hookErr = ref('')
const hookTestResult = ref<HookRun | null>(null)
const hookTestEvent = ref('')
const hookAdd = reactive({ event: '', matcher: '', command: '', commandWindows: '' })

async function loadHooks() {
  const ev = await getHookEvents()
  hookEvents.value = ev.events
  hookDecisionEvents.value = ev.decision_events
  hookAdd.event = ev.events[0] || ''
  const data = await getHooks()
  for (const k of Object.keys(hookConfig)) delete hookConfig[k]
  Object.assign(hookConfig, data.config)
}

function addHook() {
  if (!hookAdd.event) {
    hookErr.value = '请选择事件'
    return
  }
  if (!hookAdd.command) {
    hookErr.value = '请填写命令'
    return
  }
  if (!hookConfig[hookAdd.event]) hookConfig[hookAdd.event] = []
  const hook: Record<string, string> = { type: 'command', command: hookAdd.command }
  if (hookAdd.commandWindows) hook.commandWindows = hookAdd.commandWindows
  if (hookAdd.matcher) hookConfig[hookAdd.event].push({ matcher: hookAdd.matcher, hooks: [hook] })
  else hookConfig[hookAdd.event].push({ matcher: '', hooks: [hook] })
  hookAdd.matcher = ''
  hookAdd.command = ''
  hookAdd.commandWindows = ''
  hookMsg.value = '已添加，记得点击「保存钩子配置」。'
}

function removeHook(event: string, gi: number, hi: number) {
  const group = hookConfig[event][gi]
  group.hooks.splice(hi, 1)
  if (!group.hooks.length) hookConfig[event].splice(gi, 1)
  if (!hookConfig[event].length) delete hookConfig[event]
}

async function onSaveHooks() {
  hookBusy.value = true
  hookMsg.value = ''
  hookErr.value = ''
  try {
    await saveHooks(hookConfig)
    hookMsg.value = '钩子配置已保存，将在后续任务中生效。'
  } catch (e: any) {
    hookErr.value = e?.response?.data?.detail || e?.message || '保存失败'
  } finally {
    hookBusy.value = false
  }
}

async function onTestHook() {
  if (!hookTestEvent.value) {
    hookErr.value = '请选择要测试的事件'
    return
  }
  hookBusy.value = true
  hookTestResult.value = null
  try {
    hookTestResult.value = await testHook(hookTestEvent.value, { test: true })
  } catch (e: any) {
    hookErr.value = e?.response?.data?.detail || e?.message || '测试失败'
  } finally {
    hookBusy.value = false
  }
}

onMounted(async () => {
  const results = await Promise.allSettled([
    refresh(),
    refreshHealth(),
    loadMcp(),
    loadSkills(),
    loadHooks(),
    loadToolPolicy(),
    loadAgentsMd(),
  ])
  const failed = results.filter((result) => result.status === 'rejected')
  if (failed.length) loadError.value = '部分配置加载失败，请检查服务连接后重新打开设置。'
})

async function onSave() {
  if (saving.value) return
  invalidateTest()
  message.value = ''
  error.value = ''
  try {
    const snapshot = modelSnapshot()
    if (newEmbeddingKeyRequired.value) {
      throw new Error(
        'Embedding 接口地址已更换到其他服务地址，请在高级设置中重新填写该服务的 API Key 后保存。'
      )
    }
    const draft = { ...form }
    const clearKeys = { model: clearModelKey.value, embedding: clearEmbeddingKey.value }
    const revision = formRevision
    const payload: Partial<ModelSettings> = {
      llm_base_url: snapshot.llm_base_url,
      llm_model: snapshot.llm_model,
      embedding_base_url: normalizeBaseUrl(form.embedding_base_url, 'embeddings'),
      embedding_model: normalizeSingleLine(form.embedding_model, 'Embedding 模型名称'),
      approval_mode: form.approval_mode,
      work_rules: form.work_rules,
      reasoning_effort: snapshot.reasoning_effort,
      max_agent_steps: validateAgentSteps(form.max_agent_steps),
      desktop_notifications: form.desktop_notifications,
      memory_recall: form.memory_recall,
      memory_capture: form.memory_capture,
    }
    if (clearKeys.model) payload.llm_api_key = ''
    else if (snapshot.llm_api_key) payload.llm_api_key = snapshot.llm_api_key
    const embeddingKey = normalizeSingleLine(form.embedding_api_key, 'Embedding API Key')
    if (clearKeys.embedding) payload.embedding_api_key = ''
    else if (embeddingKey) payload.embedding_api_key = embeddingKey
    saving.value = true
    const saved = await saveSettings(payload)
    const editedWhileSaving = formRevision !== revision
    loaded.value = saved
    if (!editedWhileSaving) applySettings(saved)
    if (form.llm_api_key === draft.llm_api_key) form.llm_api_key = ''
    if (form.embedding_api_key === draft.embedding_api_key) form.embedding_api_key = ''
    if (clearModelKey.value === clearKeys.model) clearModelKey.value = false
    if (clearEmbeddingKey.value === clearKeys.embedding) clearEmbeddingKey.value = false
    const success = editedWhileSaving
      ? '提交的设置已保存；保存期间的新修改尚未保存。'
      : '设置已保存并立即生效。'
    message.value = success
    void refreshHealth().catch(() => {
      if (message.value === success) message.value = `${success}运行状态暂未刷新，请稍后查看。`
    })
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || '保存失败'
  } finally {
    saving.value = false
  }
}

// ---- 分层 AGENTS.md 工作规范（全局层 + 项目层） ----
const agentsMeta = ref<AgentsMdMeta | null>(null)
const globalDraft = ref('')
const globalSaving = ref(false)
const globalMsg = ref('')
const globalErr = ref('')
const showMerged = ref(false)
const initBusy = ref(false)

function utf8Bytes(s: string): number {
  return new TextEncoder().encode(s || '').length
}

async function loadAgentsMd() {
  const meta = await getAgentsMd()
  agentsMeta.value = meta
  globalDraft.value = meta.global_rules
}

async function saveGlobalRules() {
  globalSaving.value = true
  globalMsg.value = ''
  globalErr.value = ''
  try {
    const meta = await saveGlobalAgentsMd(globalDraft.value)
    agentsMeta.value = meta
    globalDraft.value = meta.global_rules
    globalMsg.value = '全局规范已保存并立即生效。'
  } catch (e: any) {
    globalErr.value = e?.response?.data?.detail || e?.message || '保存失败'
  } finally {
    globalSaving.value = false
  }
}

async function onInitAgents(overwrite: boolean) {
  initBusy.value = true
  globalMsg.value = ''
  globalErr.value = ''
  try {
    const meta = await initAgentsMd(overwrite)
    agentsMeta.value = meta
    globalDraft.value = meta.global_rules
    globalMsg.value = meta.created
      ? '已生成 AGENTS.md 脚手架，可在此基础上修改。'
      : '全局 AGENTS.md 已存在，未覆盖（可点「覆盖重写」）。'
  } catch (e: any) {
    globalErr.value = e?.response?.data?.detail || e?.message || '生成失败'
  } finally {
    initBusy.value = false
  }
}
</script>

<template>
  <div class="settings-page">
    <!-- 顶部 -->
    <div class="settings-head">
      <h1 class="settings-title">设置</h1>
      <div class="settings-desc">管理模型、工具与本地工作偏好。</div>
      <button class="btn-secondary" type="button" @click="router.push({path:'/chat',query:{guide:'1'}})">重新打开使用引导</button>
    </div>

    <p v-if="loadError" class="load-error" role="alert">{{ loadError }}</p>
    <p v-for="warning in credentialWarnings" :key="warning" class="load-error" role="alert">{{ warning }}</p>
    <div class="settings-layout">
      <nav class="tabs" aria-label="设置类别">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          class="tab"
          :class="{ active: activeTab === t.key }"
          :aria-current="activeTab === t.key ? 'page' : undefined"
          @click="activeTab = t.key"
        >
          <span>{{ t.label }}</span>
        </button>
      </nav>

      <div class="tab-content">
        <!-- 模型接口 -->
        <div v-show="activeTab === 'model'" class="tab-pane">
          <div class="card"><ModelProfilesManager /><PricingSettings /><AccountQuotaSettings /></div>
          <DeepSeekToolsSettings />
          <div class="card model-main">
            <div class="settings-section-head">
              <h2>模型接口</h2>
              <p>连接支持 OpenAI Chat Completions 的兼容服务；暂不支持仅提供 Responses 的接口。</p>
            </div>
            <label class="settings-field"
              ><span>接口类型</span>
              <select :value="presetChoice" :disabled="loadingSettings" @change="selectProvider">
                <option value="compatible">OpenAI 兼容接口</option>
                <option v-for="preset in presets" :key="preset.name" :value="preset.name">
                  {{ preset.name }}
                </option>
              </select>
            </label>
            <label class="settings-field"
              ><span>接口地址</span>
              <input
                v-model="form.llm_base_url"
                placeholder="https://api.example.com/v1"
                autocomplete="off"
                inputmode="url"
              />
              <small
                >填写服务商的 Base URL，无需添加
                /chat/completions。协议和域名不区分大小写，路径按服务商原样填写。</small
              >
            </label>
            <div class="settings-field">
              <label for="model-api-key">API Key</label>
              <div class="settings-password">
                <input
                  id="model-api-key"
                  v-model="form.llm_api_key"
                  :disabled="clearModelKey"
                  :type="keyVisible ? 'text' : 'password'"
                  :placeholder="loaded?.llm_api_key_set ? '已配置（留空则不修改）' : '输入 API Key'"
                  autocomplete="off"
                />
                <button type="button" @click="keyVisible = !keyVisible">
                  {{ keyVisible ? '隐藏' : '显示' }}
                </button>
              </div>
              <small>粘贴完整密钥并保留大小写；已保存时留空保留。更换服务地址后请重新填写。</small>
              <span v-if="loaded?.llm_api_key_set" class="clear-key-option">
                <input id="clear-model-key" v-model="clearModelKey" type="checkbox" />
                <label for="clear-model-key">清除已保存密钥，保存后使用离线模型</label>
              </span>
              <small v-if="newKeyRequired" class="err-msg" role="alert"
                >服务地址已更换，请填写新服务的 API Key 后再测试或保存。</small
              >
            </div>
            <div class="settings-field">
              <div class="model-field-heading">
                <label for="model-name">模型名称</label>
                <button
                  type="button"
                  class="btn-ghost model-fetch-button"
                  :disabled="fetchingModels || saving || loadingSettings || !loaded"
                  @click="onFetchModels"
                >
                  {{ fetchingModels ? '获取中…' : '获取模型列表' }}
                </button>
              </div>
              <div v-if="discoveredModels.length" class="model-discovery-panel">
                <input
                  v-model="modelFilter"
                  type="search"
                  :placeholder="`筛选模型（共 ${discoveredModels.length} 个）`"
                  aria-label="筛选可用模型"
                  autocomplete="off"
                />
                <select
                  :value="selectedDiscoveredModel"
                  aria-label="选择可用模型"
                  :disabled="!filteredModels.length"
                  @change="selectDiscoveredModel"
                >
                  <option disabled value="">从获取的列表中选择模型</option>
                  <option v-for="model in filteredModels" :key="model.id" :value="model.id">
                    {{ model.id }}
                  </option>
                </select>
                <small v-if="!filteredModels.length"
                  >没有匹配的模型，可调整筛选或在下方手动填写。</small
                >
              </div>
              <small
                v-if="modelListResult"
                :class="{ 'err-msg': !modelListResult.ok }"
                :role="modelListResult.ok ? 'status' : 'alert'"
                >{{ modelListResult.message }}</small
              >
              <input
                id="model-name"
                v-model="form.llm_model"
                placeholder="从列表选择，或手动输入模型 ID"
                autocomplete="off"
              />
              <small
                >填写地址和密钥后可获取列表，选择时保留模型 ID
                原始大小写。服务商不提供列表时可手动填写；选定后请测试连接并保存。</small
              >
              <small v-if="discoveredModels.length"
                >列表可能包含向量、图像等模型；是否支持当前对话功能以测试连接结果为准。</small
              >
            </div>
            <div class="settings-subsection">
              <h3>任务审批</h3>
              <label class="settings-field"
                ><span>审批方式</span>
                <select v-model="form.approval_mode">
                  <option v-for="mode in APPROVAL_MODES" :key="mode.key" :value="mode.key">
                    {{ mode.label }}
                  </option>
                </select>
                <small>文件写入、命令执行等操作会在任务中显示审批详情。</small>
              </label>
            </div>
            <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'fail'">
              {{ testResult.message }}
            </div>
            <p v-if="message" class="ok-msg" role="status">{{ message }}</p>
            <p v-if="error" class="err-msg" role="alert">{{ error }}</p>
            <div class="settings-form-actions">
              <button
                class="btn-ghost"
                :disabled="testing || saving || loadingSettings || !loaded"
                @click="onTest"
              >
                {{ testing ? '测试中…' : '测试连接' }}
              </button>
              <button
                class="btn-primary"
                :disabled="saving || loadingSettings || !loaded"
                @click="onSave"
              >
                {{ saving ? '保存中…' : '保存配置' }}
              </button>
            </div>
          </div>
          <details class="advanced-settings">
            <summary>高级设置 <span>向量化、推理、工作规范与记忆</span></summary>
            <div class="card">
              <div class="card-title">Embedding 向量化（可选）</div>
              <div class="form-grid">
                <label>接口地址</label>
                <input v-model="form.embedding_base_url" placeholder="留空则沿用大模型接口地址" />
                <label>API Key</label>
                <input
                  v-model="form.embedding_api_key"
                  :disabled="clearEmbeddingKey"
                  type="password"
                  :placeholder="
                    loaded?.embedding_api_key_set
                      ? '已配置（留空则不修改）'
                      : '不填写则使用本地向量'
                  "
                />
                <label>模型名称</label>
                <input v-model="form.embedding_model" placeholder="text-embedding-v3" />
              </div>
              <div v-if="loaded?.embedding_api_key_set" class="clear-key-option">
                <input id="clear-embedding-key" v-model="clearEmbeddingKey" type="checkbox" />
                <label for="clear-embedding-key"
                  >清除已保存的 Embedding 密钥，保存后使用本地向量</label
                >
              </div>
              <p v-if="newEmbeddingKeyRequired" class="err-msg" role="alert">
                Embedding 服务地址已更换，请重新填写该服务的 API Key 后保存。
              </p>
              <div class="card-hint" style="margin-top: 12px">
                不填任何 Key 也能完整运行：解析、混合检索、引用闸门、SSE
                过程面板、图谱均可用；仅答案文字由离线模板生成。
              </div>

              <div class="approval-mode">
                <label class="am-label" for="max-agent-steps">每段执行轮数</label>
                <input
                  id="max-agent-steps"
                  v-model.number="form.max_agent_steps"
                  type="number"
                  min="1"
                  max="128"
                  step="1"
                  aria-describedby="max-agent-steps-help"
                />
                <p id="max-agent-steps-help" class="wr-hint">
                  默认 24 轮，可设置 1–128
                  轮，每轮可调用多个工具。每段结束后会自动保存检查点并继续；连续两个阶段没有新结果时会暂停，避免重复消耗。
                  设置较大的数值会减少检查点频率；保存后对新任务生效。
                </p>
              </div>

              <!-- 推理强度 -->
              <div class="approval-mode">
                <label class="am-label">推理强度（reasoning effort）</label>
                <div class="effort-options">
                  <button
                    v-for="e in REASONING_EFFORTS"
                    :key="e.key"
                    type="button"
                    class="effort-option"
                    :class="{ active: form.reasoning_effort === e.key }"
                    @click="form.reasoning_effort = e.key"
                  >
                    <span class="effort-label">{{ e.label }}</span>
                    <span class="effort-desc">{{ e.desc }}</span>
                  </button>
                </div>
                <p class="wr-hint">
                  可用值以服务商和模型文档为准。不确定是否支持 reasoning_effort
                  时选择「不传」，避免参数不兼容。
                </p>
              </div>

              <!-- 分层工作规范（AGENTS.md，对标 Codex） -->
              <div class="work-rules agents-md">
                <div class="am-head">
                  <label class="am-label">工作规范（分层 AGENTS.md）</label>
                  <span v-if="agentsMeta" class="am-path" :title="agentsMeta.global_path"
                    >全局文件：{{ agentsMeta.global_path }}</span
                  >
                </div>

                <!-- 项目层 -->
                <div class="am-layer">
                  <div class="am-layer-head">
                    <span class="am-layer-name">应用补充规范 · 所有会话通用</span>
                    <span class="am-bytes" :class="{ over: utf8Bytes(form.work_rules) > 32768 }">
                      {{ utf8Bytes(form.work_rules) }} / 32768 B
                    </span>
                  </div>
                  <textarea
                    v-model="form.work_rules"
                    rows="4"
                    placeholder="例如：统一使用 GB/T 7714 引用格式；正文用客观第三人称；图表须标注来源……（随下方「保存设置」一起生效）"
                  ></textarea>
                </div>

                <!-- 全局层 -->
                <div class="am-layer">
                  <div class="am-layer-head">
                    <span class="am-layer-name">全局层 · 所有项目通用</span>
                    <span class="am-bytes" :class="{ over: utf8Bytes(globalDraft) > 32768 }">
                      {{ utf8Bytes(globalDraft) }} / 32768 B
                    </span>
                  </div>
                  <textarea
                    v-model="globalDraft"
                    rows="6"
                    placeholder="全局通用的语言、引用、工作流程与禁忌规范……"
                  ></textarea>
                  <div class="am-actions">
                    <button
                      type="button"
                      class="btn-mini"
                      :disabled="globalSaving"
                      @click="saveGlobalRules"
                    >
                      {{ globalSaving ? '保存中…' : '保存全局规范' }}
                    </button>
                    <button
                      type="button"
                      class="btn-mini ghost"
                      :disabled="initBusy"
                      @click="onInitAgents(false)"
                    >
                      创建全局脚手架
                    </button>
                    <button
                      type="button"
                      class="btn-mini ghost"
                      :disabled="initBusy"
                      @click="onInitAgents(true)"
                    >
                      覆盖重写
                    </button>
                    <button type="button" class="btn-mini ghost" @click="showMerged = !showMerged">
                      {{ showMerged ? '收起合并预览' : '查看合并预览' }}
                    </button>
                  </div>
                  <span v-if="globalMsg" class="ok-msg">{{ globalMsg }}</span>
                  <span v-if="globalErr" class="err-msg">{{ globalErr }}</span>
                </div>

                <!-- 合并预览 -->
                <div v-if="showMerged" class="am-merged">
                  <pre>{{ agentsMeta?.merged || '（两层均为空，最终不会注入工作规范）' }}</pre>
                </div>

                <p class="wr-hint">
                  此处两层对所有会话生效。真实项目规范请在聊天的「项目文件 → 目录规范」查看；也可用
                  /init 在当前项目创建 AGENTS.md。每层上限 32 KiB。
                </p>
              </div>

              <!-- 通知与记忆开关（对应 Codex /memories 与桌面通知） -->
              <div class="toggle-list">
                <div class="toggle-row">
                  <div class="toggle-text">
                    <span class="toggle-name">桌面通知</span>
                    <span class="toggle-desc">后台任务完成、失败或需要审批时弹出 Windows 通知</span>
                  </div>
                  <button
                    type="button"
                    class="switch"
                    :class="{ on: form.desktop_notifications }"
                    role="switch"
                    :aria-checked="form.desktop_notifications"
                    @click="form.desktop_notifications = !form.desktop_notifications"
                  >
                    <span class="knob"></span>
                  </button>
                </div>
                <div class="toggle-row">
                  <div class="toggle-text">
                    <span class="toggle-name">使用记忆</span>
                    <span class="toggle-desc"
                      >提问时载入本会话历史上下文（含早期对话压缩摘要）</span
                    >
                  </div>
                  <button
                    type="button"
                    class="switch"
                    :class="{ on: form.memory_recall }"
                    role="switch"
                    :aria-checked="form.memory_recall"
                    @click="form.memory_recall = !form.memory_recall"
                  >
                    <span class="knob"></span>
                  </button>
                </div>
                <div class="toggle-row">
                  <div class="toggle-text">
                    <span class="toggle-name">生成记忆</span>
                    <span class="toggle-desc"
                      >让本轮问答进入后续会话的记忆；关闭后不影响本次记录保存</span
                    >
                  </div>
                  <button
                    type="button"
                    class="switch"
                    :class="{ on: form.memory_capture }"
                    role="switch"
                    :aria-checked="form.memory_capture"
                    @click="form.memory_capture = !form.memory_capture"
                  >
                    <span class="knob"></span>
                  </button>
                </div>
              </div>
            </div>

            <div class="save-row">
              <button
                class="btn-primary"
                :disabled="saving || loadingSettings || !loaded"
                @click="onSave"
              >
                {{ saving ? '保存中…' : '保存设置' }}
              </button>
              <span v-if="message" class="ok-msg">{{ message }}</span>
              <span v-if="error" class="err-msg">{{ error }}</span>
            </div>
          </details>
        </div>

        <!-- MCP 服务器 -->
        <div v-show="activeTab === 'mcp'" class="tab-pane">
          <div class="card">
            <div class="card-head-row">
              <div>
                <div class="card-title">MCP 服务器（Model Context Protocol）</div>
                <div class="card-hint">
                  连接后，外部工具会自动出现在 Agent 工具集中（如 arxiv 论文检索、网络抓取等）
                </div>
              </div>
            </div>

            <div v-if="mcpServers.length === 0" class="mcp-empty">
              尚未配置 MCP 服务器，在下方添加一个。
            </div>

            <div v-for="s in mcpServers" :key="s.name" class="mcp-row">
              <div class="mcp-row-main">
                <div class="mcp-row-title">
                  <span class="mcp-name">{{ s.name }}</span>
                  <span v-if="s.builtin" class="mcp-badge builtin">内置</span>
                  <span class="mcp-badge" :class="statusLabel(s).cls">{{
                    statusLabel(s).text
                  }}</span>
                </div>
                <div class="mcp-row-sub">
                  <span class="mcp-tag">{{ s.transport }}</span>
                  <span class="mcp-tools">{{ s.tool_count }} 个工具</span>
                  <span v-if="s.transport === 'http'" class="mcp-detail">{{ s.url }}</span>
                  <span v-else-if="s.transport === 'builtin'" class="mcp-detail"
                    >随软件本体运行，无需外部环境</span
                  >
                  <span v-else class="mcp-detail">{{ s.command }}</span>
                </div>
                <div v-if="s.error" class="mcp-row-err">{{ s.error }}</div>
                <McpUserAuthorization v-if="s.auth_mode === 'authorization_code'" :server="s" @changed="loadMcp" />
              </div>
              <div class="mcp-actions">
                <!-- 内置：只允许启停，不可编辑/删除 -->
                <button
                  v-if="s.builtin"
                  class="icon-btn"
                  :title="s.enabled ? '停用' : '启用'"
                  @click="onToggleMcp(s)"
                >
                  <Power :size="14" :class="s.enabled ? 'on' : ''" />
                </button>
                <template v-else>
                  <button
                    v-if="s.status !== 'connected'"
                    class="icon-btn"
                    title="连接"
                    @click="onConnectMcp(s)"
                  >
                    <Power :size="14" />
                  </button>
                  <button v-else class="icon-btn" title="断开" @click="onDisconnectMcp(s)">
                    <Power :size="14" class="on" />
                  </button>
                  <button class="icon-btn" title="编辑" @click="editMcpServer(s)">
                    <Pencil :size="14" />
                  </button>
                  <button class="icon-btn danger" title="删除" @click="onRemoveMcp(s)">
                    <Trash2 :size="14" />
                  </button>
                </template>
              </div>
            </div>
          </div>

          <!-- 工具级管控 -->
          <div class="card">
            <div class="card-title">工具级管控（白/黑名单 · 单工具审批 · 超时）</div>
            <div class="card-hint" style="margin-bottom: 12px">
              关闭开关即把该工具加入黑名单（对模型隐藏）；审批可强制每次确认或自动放行；超时限定单次调用最长秒数。
            </div>
            <div v-if="discoveredTools.length === 0" class="mcp-empty">
              暂无已连接的 MCP 工具，请先在上方连接服务器。
            </div>
            <div class="tool-policy-list">
              <div v-for="t in discoveredTools" :key="t.name" class="tool-policy-row">
                <div class="tp-head">
                  <button
                    type="button"
                    class="switch sm"
                    :class="{ on: !isToolDisabled(t.name) }"
                    :title="isToolDisabled(t.name) ? '已禁用，点击启用' : '已启用，点击禁用'"
                    @click="toggleTool(t.name)"
                  >
                    <span class="knob"></span>
                  </button>
                  <div class="tp-name-box">
                    <span class="tp-name">{{ t.name.split('__').slice(2).join('__') }}</span>
                    <span class="tp-qualified">{{ t.name }}</span>
                  </div>
                </div>
                <div class="tp-controls">
                  <select
                    :value="getApproval(t.name)"
                    class="tp-select"
                    @change="setApproval(t.name, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">审批：默认</option>
                    <option value="always">每次审批</option>
                    <option value="never">自动放行</option>
                  </select>
                  <input
                    :value="getTimeoutVal(t.name)"
                    class="tp-timeout"
                    type="number"
                    min="1"
                    max="600"
                    placeholder="超时秒"
                    @input="setTimeoutVal(t.name, ($event.target as HTMLInputElement).value)"
                  />
                </div>
              </div>
            </div>
            <div v-if="policyMsg" class="test-result ok" style="margin-top: 12px">
              {{ policyMsg }}
            </div>
            <div v-if="policyErr" class="test-result fail" style="margin-top: 12px">
              {{ policyErr }}
            </div>
            <div class="card-foot-row">
              <button class="btn-primary" :disabled="policyBusy" @click="onSavePolicy">
                {{ policyBusy ? '保存中…' : '保存管控策略' }}
              </button>
              <button class="btn-ghost" @click="loadToolPolicy">重新加载</button>
            </div>
          </div>

          <div class="card">
            <div class="card-title">
              {{ mcpForm.editing ? '编辑服务器' : '添加服务器' }}
            </div>
            <div class="mcp-transport-row">
              <button
                class="btn-preset"
                :class="{ active: mcpForm.transport === 'stdio' }"
                @click="mcpForm.transport = 'stdio'"
              >
                stdio（本地子进程）
              </button>
              <button
                class="btn-preset"
                :class="{ active: mcpForm.transport === 'http' }"
                @click="mcpForm.transport = 'http'"
              >
                streamable-http（远程）
              </button>
            </div>

            <div class="form-grid">
              <label>名称</label>
              <input v-model="mcpForm.name" placeholder="如 academic" />

              <template v-if="mcpForm.transport === 'http'">
                <label>地址</label>
                <input v-model="mcpForm.url" placeholder="http://localhost:8000/mcp" />

                <!-- 认证方式 -->
                <label>认证方式</label>
                <div class="mcp-auth-row">
                  <button v-if="mcpForm.editing" type="button" class="btn-preset sm" :class="{ active: mcpForm.authMode === 'preserve' }" @click="mcpForm.authMode = 'preserve'">保留原认证</button>
                  <button
                    type="button"
                    class="btn-preset sm"
                    :class="{ active: mcpForm.authMode === 'none' }"
                    @click="mcpForm.authMode = 'none'"
                  >
                    无
                  </button>
                  <button
                    type="button"
                    class="btn-preset sm"
                    :class="{ active: mcpForm.authMode === 'bearer' }"
                    @click="mcpForm.authMode = 'bearer'"
                  >
                    静态 Token
                  </button>
                  <button
                    type="button"
                    class="btn-preset sm"
                    :class="{ active: mcpForm.authMode === 'oauth' }"
                    @click="mcpForm.authMode = 'oauth'"
                  >
                    OAuth 客户端凭证
                  </button>
                  <button type="button" class="btn-preset sm" :class="{ active: mcpForm.authMode === 'user_oauth' }" @click="mcpForm.authMode = 'user_oauth'">OAuth 用户登录</button>
                </div>

                <template v-if="mcpForm.authMode !== 'none'">
                  <label>自定义请求头</label>
                  <textarea
                    v-model="mcpForm.headersText"
                    rows="2"
                    placeholder="每行一个，KEY=VALUE，如 X-Org=lab"
                  ></textarea>
                </template>

                <template v-if="mcpForm.authMode === 'bearer'">
                  <label>访问 Token</label>
                  <input
                    v-model="mcpForm.bearerToken"
                    placeholder="Bearer token（不含 Bearer 前缀）"
                  />
                </template>

                <template v-if="mcpForm.authMode === 'oauth'">
                  <label>Token 端点</label>
                  <input
                    v-model="mcpForm.oauthTokenUrl"
                    placeholder="https://id.example.com/oauth/token"
                  />
                  <label>Client ID</label>
                  <input v-model="mcpForm.oauthClientId" placeholder="客户端标识" />
                  <label>Client Secret</label>
                  <input
                    v-model="mcpForm.oauthClientSecret"
                    type="password"
                    placeholder="客户端密钥"
                  />
                  <label>Scope（可选）</label>
                  <input v-model="mcpForm.oauthScope" placeholder="如 read.write" />
                </template>
                <template v-if="mcpForm.authMode === 'user_oauth'">
                  <label>用户授权</label>
                  <div class="card-hint">使用浏览器登录与 PKCE S256。保存后在上方连接卡片点击“登录授权”；令牌保存在系统凭证保护中。默认自动发现并注册公开客户端。</div>
                  <label>预注册 Client ID（可选）</label><input v-model="mcpForm.oauthClientId" autocomplete="off" />
                  <label>预注册授权服务器 issuer</label><input v-model="mcpForm.oauthIssuer" placeholder="仅使用预注册 Client ID 时必填" />
                  <label>请求范围（可选）</label><input v-model="mcpForm.oauthScope" placeholder="由授权服务器展示并确认" />
                </template>
              </template>

              <template v-else>
                <label>启动命令</label>
                <input v-model="mcpForm.command" placeholder="如 uvx 或 python" />
                <label>参数</label>
                <input v-model="mcpForm.argsText" placeholder="空格分隔，如 academic-mcp" />
                <label>工作目录</label>
                <input v-model="mcpForm.cwd" placeholder="可选" />
                <label>环境变量</label>
                <textarea
                  v-model="mcpForm.envText"
                  rows="3"
                  placeholder="每行一个，KEY=VALUE，如 SEMANTIC_SCHOLAR_API_KEY=xxx"
                ></textarea>
              </template>
            </div>

            <div v-if="mcpMsg" class="test-result ok" style="margin-top: 12px">{{ mcpMsg }}</div>
            <div v-if="mcpErr" class="test-result fail" style="margin-top: 12px">{{ mcpErr }}</div>

            <div class="card-foot-row">
              <button class="btn-primary" :disabled="mcpBusy" @click="saveMcpServer">
                {{ mcpBusy ? '处理中…' : mcpForm.editing ? '保存并连接' : '添加并连接' }}
              </button>
              <button v-if="mcpForm.editing" class="btn-ghost" @click="resetMcpForm">
                取消编辑
              </button>
            </div>
          </div>
        </div>

        <!-- 技能库 -->
        <div v-show="activeTab === 'skills'" class="tab-pane">
          <div class="card">
            <div class="card-head-row">
              <div>
                <div class="card-title">技能库（内置 / 用户 / 项目）</div>
                <div class="card-hint">
                  项目技能只在所属项目生效。同名技能显示覆盖关系；禁用后不能通过 $名称 绕过，更新版本需重新审核。
                </div>
              </div>
            </div>

            <div class="skill-scope"><label for="skill-project">技能范围</label>
            <select id="skill-project" v-model="skillProject" aria-label="技能范围">
              <option value="">全局用户技能</option>
              <option v-for="p in skillProjects" :key="p.id" :value="p.id">{{ p.name }}</option>
            </select>
            </div>
            <div v-if="legacySkills.length" class="card-hint">
              旧共享项目技能尚未确定归属，不会自动加载。请选择项目后逐项复制，再审核正文。
              <div v-for="s in legacySkills" :key="s.name">
                {{ s.title }} <button :disabled="!skillProject" class="btn-ghost" @click="migrateSkill(s)">复制到所选项目并审核</button>
              </div>
            </div>

            <div v-if="!activeSkill" class="skill-grid">
              <div
                v-for="s in skills"
                :key="s.name"
                class="skill-card"
                :class="{ off: s.disabled }"
              >
                <div class="skill-card-top">
                  <span class="mcp-badge" :class="tierLabel(s.tier).cls">{{
                    tierLabel(s.tier).text
                  }}</span>
                  <button
                    type="button"
                    class="switch sm"
                    :class="{ on: !s.disabled }"
                    :title="s.disabled ? '已禁用，点击启用' : '已启用，点击禁用'"
                    @click="toggleSkillDisabled(s)"
                  >
                    <span class="knob"></span>
                  </button>
                </div>
                <button
                  type="button"
                  class="skill-card-main"
                  :disabled="skillBusy"
                  @click="openSkill(s)"
                >
                  <div class="skill-card-name">{{ s.title }}</div>
                  <div class="skill-card-desc">{{ s.description }}</div>
                  <div v-if="s.overrides?.length" class="skill-card-desc">覆盖：{{ s.overrides.map(v => tierLabel(v.tier).text).join('、') }}</div>
                  <div class="skill-card-open">
                    {{ s.disabled ? '已禁用 · 查看指南' : s.trusted ? '查看指南' : '新版本待审核' }} · {{ s.revision?.slice(0, 12) }}
                  </div>
                </button>
              </div>
            </div>

            <div v-else class="skill-detail">
              <div class="skill-detail-head">
                <div class="card-title">
                  {{ activeSkill.title }}
                  <span class="mcp-badge" :class="tierLabel(activeSkill.tier).cls">{{
                    tierLabel(activeSkill.tier).text
                  }}</span>
                </div>
                <div class="skill-detail-actions">
                  <button v-if="!editingSkill && (activeSkill.tier === 'project' || activeSkill.tier === 'user' && !skillProject)" class="btn-ghost" @click="editingSkill = true; skillDraft = activeSkill.content">编辑正文</button>
                  <button v-if="!activeSkill.trusted" class="btn-primary" @click="reviewSkill">已阅读，信任此版本</button>
                  <button class="btn-ghost" @click="toggleSkillDisabled(activeSkill)">
                    {{ activeSkill.disabled ? '启用技能' : '禁用技能' }}
                  </button>
                  <button
                    v-if="activeSkill.tier === 'project' || activeSkill.tier === 'user' && !skillProject"
                    class="btn-ghost danger-text"
                    @click="removeUserSkill(activeSkill)"
                  >
                    删除
                  </button>
                  <button class="btn-ghost" @click="closeSkill">返回列表</button>
                </div>
              </div>
              <template v-if="editingSkill">
                <textarea v-model="skillDraft" class="skill-body" aria-label="编辑技能正文" rows="12" style="width: 100%; box-sizing: border-box"></textarea>
                <button class="btn-primary" :disabled="skillBusy" @click="saveSkillEdit">保存并信任新版本</button>
                <button class="btn-ghost" :disabled="skillBusy" @click="editingSkill = false">取消编辑</button>
              </template>
              <pre v-else class="skill-body">{{ activeSkill.content }}</pre>
            </div>
          </div>

          <!-- 新建用户技能 -->
          <div class="card">
            <div class="card-title">{{ skillProject ? '新建当前项目技能' : '新建用户技能' }}</div>
            <div class="card-hint" style="margin-bottom: 12px">
              技能名仅允许字母/数字/下划线/连字符；正文写清该技能的操作步骤与注意事项。
            </div>
            <div class="form-grid">
              <label>技能名</label>
              <input v-model="skillForm.name" placeholder="如 my-review-checklist" />
              <label>标题</label>
              <input v-model="skillForm.title" placeholder="如 我的审稿清单" />
              <label>一句话描述</label>
              <input v-model="skillForm.description" placeholder="该技能适用的场景" />
              <label>正文（操作指南）</label>
              <textarea
                v-model="skillForm.content"
                rows="6"
                placeholder="第一步……；第二步……"
              ></textarea>
            </div>
            <div v-if="skillFormMsg" class="test-result ok" style="margin-top: 12px">
              {{ skillFormMsg }}
            </div>
            <div v-if="skillFormErr" class="test-result fail" style="margin-top: 12px">
              {{ skillFormErr }}
            </div>
            <div class="card-foot-row">
              <button class="btn-primary" :disabled="skillFormBusy" @click="submitNewSkill">
                {{ skillFormBusy ? '创建中…' : '创建技能' }}
              </button>
            </div>
          </div>
        </div>

        <!-- 生命周期钩子 -->
        <div v-show="activeTab === 'hooks'" class="tab-pane">
          <div class="card">
            <div class="card-head-row">
              <div>
                <div class="card-title">生命周期钩子（Hooks）</div>
                <div class="card-hint">
                  在 Agent 关键事件点自动执行命令：入库后打标签、压缩前备份、完成后通知等
                </div>
              </div>
            </div>

            <div v-for="event in hookEvents" :key="event" class="hook-event-block">
              <div class="hook-event-head">
                <span class="hook-event-name">{{ event }}</span>
                <span v-if="hookDecisionEvents.includes(event)" class="hook-decision-tag"
                  >可阻断</span
                >
              </div>
              <div v-if="!hookConfig[event] || !hookConfig[event].length" class="hook-empty">
                未配置
              </div>
              <template v-else>
                <div v-for="(group, gi) in hookConfig[event]" :key="gi" class="hook-group">
                  <div class="hook-matcher">matcher：{{ group.matcher || '.*（全部）' }}</div>
                  <div v-for="(h, hi) in group.hooks" :key="hi" class="hook-item">
                    <code class="hook-cmd">{{ h.command }}</code>
                    <code v-if="h.commandWindows" class="hook-cmd win"
                      >Windows：{{ h.commandWindows }}</code
                    >
                    <button class="icon-btn danger" title="删除" @click="removeHook(String(event), Number(gi), Number(hi))">
                      <Trash2 :size="13" />
                    </button>
                  </div>
                </div>
              </template>
            </div>
          </div>

          <div class="card">
            <div class="card-title">添加钩子</div>
            <div class="form-grid">
              <label>事件</label>
              <select v-model="hookAdd.event">
                <option v-for="event in hookEvents" :key="event" :value="event">{{ event }}</option>
              </select>
              <label>matcher</label>
              <input v-model="hookAdd.matcher" placeholder="可选，正则匹配工具名，如 retrieve_.*" />
              <label>命令</label>
              <input v-model="hookAdd.command" placeholder="如 python scripts/after.py" />
              <label>Windows 命令</label>
              <input v-model="hookAdd.commandWindows" placeholder="可选，Windows 上覆盖命令" />
            </div>
            <div class="card-foot-row">
              <button class="btn-ghost" @click="addHook">
                <Plus :size="14" style="vertical-align: -2px" /> 添加
              </button>
            </div>
          </div>

          <div class="card">
            <div class="card-title">测试触发</div>
            <div class="form-grid">
              <label>事件</label>
              <select v-model="hookTestEvent">
                <option value="" disabled>选择事件</option>
                <option v-for="event in hookEvents" :key="event" :value="event">{{ event }}</option>
              </select>
            </div>
            <div class="card-foot-row">
              <button class="btn-ghost" :disabled="hookBusy" @click="onTestHook">
                {{ hookBusy ? '执行中…' : '立即触发一次' }}
              </button>
            </div>
            <div v-if="hookTestResult" class="hook-test-result">
              <div class="hook-test-summary">
                执行 {{ hookTestResult.ran }} 条，阻断：{{ hookTestResult.blocked ? '是' : '否' }}
              </div>
              <div v-for="(r, i) in hookTestResult.results" :key="i" class="hook-test-item">
                <div class="hook-test-cmd">{{ r.command }}</div>
                <div class="hook-test-meta">
                  退出码：{{ r.exit_code }}<span v-if="r.timed_out">（超时）</span>
                </div>
                <pre v-if="r.stdout" class="hook-test-out">{{ r.stdout }}</pre>
                <pre v-if="r.stderr" class="hook-test-out err">{{ r.stderr }}</pre>
              </div>
            </div>
          </div>

          <div class="save-row">
            <button class="btn-primary" :disabled="hookBusy" @click="onSaveHooks">
              {{ hookBusy ? '保存中…' : '保存钩子配置' }}
            </button>
            <span v-if="hookMsg" class="ok-msg">{{ hookMsg }}</span>
            <span v-if="hookErr" class="err-msg">{{ hookErr }}</span>
          </div>
        </div>

        <!-- 运行状态 -->
        <div v-show="activeTab === 'status'" class="tab-pane">
          <div class="card">
            <div class="card-title">运行状态</div>
            <div v-if="health" class="status-grid">
              <span class="muted">服务状态</span>
              <span class="badge ok">{{ health.status }}</span>
              <span class="muted">默认模型配置</span>
              <span class="badge" :class="health.llm_mode === 'mock' || health.llm_mode === 'unconfigured' ? 'mock' : 'ok'">
                {{ health.llm_mode === 'unconfigured' ? '模型待配置' : health.llm_mode === 'mock' ? '离线演示' : '已配置接口（尚未测试连通性）' }}
              </span>
              <span class="muted">向量模型</span>
              <span class="badge" :class="health.embedding_mode === 'local' ? 'mock' : 'ok'">
                {{ health.embedding_mode === 'local' ? '本地哈希向量' : 'API Embedding' }}
              </span>
              <span class="muted">模型 ID</span><span>{{ health.llm_model }}</span>
              <span class="muted">已入库论文</span><span>{{ health.paper_count }} 篇</span>
            </div>
          </div>
        </div>

        <!-- 数据与存储 -->
        <div v-show="activeTab === 'data'" class="tab-pane">
          <BackupSettingsCard />
          <StorageSettingsCard v-if="activeTab === 'data'" />
          <div class="card">
            <div class="card-title">
              <HardDrive :size="15" style="vertical-align: -2px" /> 本地优先存储
            </div>
            <p class="para">
              研序采用本地优先（local-first）设计：论文、解析结果、向量索引与问答记录均保存在本机的
              应用数据目录（桌面版默认为
              <code>%APPDATA%\ScholarFlow</code
              >）。使用云端模型或向量接口时，相关文本会发送到你配置的服务。
            </p>
            <div class="store-list">
              <div class="store-item"><FileText :size="15" /><span>论文原件（PDF）</span></div>
              <div class="store-item">
                <Database :size="15" /><span>SQLite 数据库（scholarflow.db）</span>
              </div>
              <div class="store-item">
                <Activity :size="15" /><span>向量索引（vectors.npz）</span>
              </div>
            </div>
            <p class="para">卸载或删除该目录即可清除全部数据；重新打开程序会自动重建。</p>
          </div>
        </div>

        <!-- 合规说明 -->
        <div v-show="activeTab === 'compliance'" class="tab-pane">
          <ReleaseInfo />
          <div class="card">
            <div class="card-title">开源与许可</div>
            <p class="para">
              研序由张祎凡独立开发，项目原创代码著作权归作者所有。本项目为源码可见软件，使用及二次创作须事先取得作者书面同意（lrtzyf@gmail.com）。
            </p>
            <p class="para">
              本项目使用组件的许可范围见根目录 <code>LICENSES.md</code>。
            </p>
            <button class="btn-ghost" @click="router.push('/about')">查看完整关于与许可证</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.skill-scope { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 18px 0; }
.skill-scope select { min-width: 190px; max-width: 100%; padding: 8px 12px; border: 1px solid var(--border-color, #dce4ee); border-radius: 8px; color: inherit; background: var(--bg-secondary, #fff); font: inherit; }
.settings-page {
  font-size: 14px;
  line-height: 1.6;
  height: 100%;
  overflow-y: auto;
  width: 100%;
  max-width: 1180px;
  margin: 0 auto;
  padding: 38px 42px 28px;
  background: var(--fn-bg-primary);
}
.settings-head {
  margin-bottom: 29px;
}
.settings-title {
  margin: 0 0 8px;
  font-size: 24px;
  line-height: 1.4;
  font-weight: 600;
  letter-spacing: 0;
  color: var(--fn-text-primary);
}
.settings-desc {
  font-size: 13px;
  color: var(--muted);
  margin-top: 0;
  line-height: 1.8;
}

/* 与源码界面一致的设置分类栏 */
.settings-layout {
  display: grid;
  grid-template-columns: 168px minmax(0, 1fr);
  gap: 44px;
  border-top: 1px solid var(--fn-border-primary);
  padding-top: 26px;
  align-items: start;
}
.tabs {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.tab {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 7px;
  padding: 10px 12px;
  border: none;
  background: transparent;
  color: var(--fn-text-secondary);
  font-size: 12px;
  cursor: pointer;
  border-radius: 8px;
  text-align: left;
  white-space: nowrap;
}
.tab:hover {
  color: var(--fn-text-primary);
}
.tab.active {
  color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
  font-weight: 500;
}

.tab-content {
  min-width: 0;
  padding: 0 0 20px;
}
.tab-pane {
  max-width: 645px;
}

.card {
  background: var(--fn-bg-primary);
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  border-radius: 0;
  box-shadow: none;
  padding: 0 0 28px;
  margin-bottom: 28px;
}
.card-title {
  font-size: 17px;
  font-weight: 500;
  color: var(--fn-text-primary);
  margin-bottom: 4px;
}
.card-hint {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin-bottom: 0;
}
.approval-mode {
  margin-top: 16px;
}
.am-label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--fn-text-primary);
  margin-bottom: 9px;
}
.am-options {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
.am-option {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 11px 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 9px;
  background: var(--fn-bg-secondary);
  text-align: left;
  cursor: pointer;
  transition: all 0.15s ease;
}
.am-option:hover {
  border-color: var(--fn-brand);
}
.am-option.active {
  border-color: var(--fn-brand);
  background: rgba(10, 89, 247, 0.06);
}
.am-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.am-desc {
  font-size: 11px;
  line-height: 1.5;
  color: var(--fn-text-tertiary);
}
@media (max-width: 720px) {
  .am-options {
    grid-template-columns: 1fr;
  }
}
.effort-options {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 8px;
}
.effort-option {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 10px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 9px;
  background: var(--fn-bg-secondary);
  text-align: center;
  cursor: pointer;
  transition: all 0.15s ease;
}
.effort-option:hover {
  border-color: var(--fn-brand);
}
.effort-option.active {
  border-color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
}
.effort-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.effort-desc {
  font-size: 10px;
  line-height: 1.45;
  color: var(--fn-text-tertiary);
}
@media (max-width: 720px) {
  .effort-options {
    grid-template-columns: repeat(2, 1fr);
  }
}
.work-rules {
  margin-top: 16px;
}
.work-rules textarea {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 9px;
  background: var(--fn-bg-secondary);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--fn-text-primary);
  resize: vertical;
  font-family: inherit;
}
.work-rules textarea:focus {
  outline: none;
  border-color: var(--fn-brand);
}
.wr-hint {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
  margin: 6px 0 0;
}

/* 分层 AGENTS.md */
.am-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.am-path {
  font-size: 10.5px;
  color: var(--fn-text-tertiary);
  max-width: 60%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.am-layer {
  margin-top: 12px;
}
.am-layer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.am-layer-name {
  font-size: 12px;
  font-weight: 600;
  color: var(--fn-text-secondary);
}
.am-bytes {
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
  color: var(--fn-text-tertiary);
}
.am-bytes.over {
  color: var(--fn-danger);
  font-weight: 600;
}
.am-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.btn-mini {
  height: 28px;
  padding: 0 12px;
  border: none;
  border-radius: 7px;
  background: var(--fn-brand);
  color: #fff;
  font-size: 11.5px;
  cursor: pointer;
}
.btn-mini:hover {
  box-shadow: 0 3px 10px var(--fn-brand-shadow);
}
.btn-mini:disabled {
  opacity: 0.55;
  cursor: default;
  box-shadow: none;
}
.btn-mini.ghost {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  color: var(--fn-text-secondary);
}
.btn-mini.ghost:hover {
  border-color: var(--fn-brand);
  color: var(--fn-brand);
  box-shadow: none;
}
.am-merged {
  margin-top: 10px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 9px;
  background: var(--fn-bg-secondary);
  max-height: 260px;
  overflow: auto;
}
.am-merged pre {
  margin: 0;
  padding: 10px 12px;
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--fn-text-secondary);
  font-family: inherit;
}

/* 通知与记忆开关 */
.toggle-list {
  margin-top: 18px;
  border-top: 1px solid var(--fn-border-primary);
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.toggle-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.toggle-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.toggle-desc {
  font-size: 11.5px;
  color: var(--fn-text-tertiary);
  line-height: 1.5;
}
.switch {
  flex: 0 0 auto;
  width: 42px;
  height: 24px;
  border-radius: 999px;
  border: 1px solid var(--fn-border-primary);
  background: var(--fn-bg-tertiary, #d6dae2);
  position: relative;
  cursor: pointer;
  padding: 0;
  transition:
    background 0.18s ease,
    border-color 0.18s ease;
}
.switch .knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.22);
  transition: transform 0.18s ease;
}
.switch.on {
  background: var(--fn-brand);
  border-color: var(--fn-brand);
}
.switch.on .knob {
  transform: translateX(18px);
}
.card-head-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.card-foot-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}
.conn-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  padding: 3px 11px;
  border-radius: 999px;
  white-space: nowrap;
  flex-shrink: 0;
}
.conn-status .pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.conn-status.online {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.conn-status.offline {
  background: var(--fn-bg-tertiary);
  color: var(--fn-text-secondary);
}
.preset-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.btn-preset {
  height: 34px;
  padding: 0 15px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 9px;
  background: var(--fn-bg-primary);
  font-size: 13px;
  color: var(--fn-text-secondary);
  cursor: pointer;
  transition: all var(--fn-transition-fast);
}
.btn-preset:hover {
  border-color: var(--fn-brand);
  color: var(--fn-brand);
}
.btn-preset.active {
  border-color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
  font-weight: 600;
}
.test-result {
  margin-top: 12px;
  font-size: 12.5px;
  padding: 8px 12px;
  border-radius: 8px;
}
.test-result.ok {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.test-result.fail {
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
}
.form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  align-items: center;
}
.form-grid label {
  margin-top: 12px;
  font-size: 12px;
  color: var(--fn-text-secondary);
}
.form-grid input {
  min-height: 38px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 0 11px;
  font-size: 13px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.form-grid input:focus {
  outline: none;
  border-color: var(--fn-brand);
  box-shadow: 0 0 0 2px var(--fn-bg-active);
}
.save-row {
  display: flex;
  align-items: center;
  gap: 14px;
}
.btn-primary {
  height: 38px;
  padding: 0 20px;
  border: none;
  border-radius: 9px;
  background: var(--fn-brand);
  color: #fff;
  font-size: 13.5px;
  cursor: pointer;
}
.btn-primary:hover {
  box-shadow: 0 4px 14px var(--fn-brand-shadow);
}
.btn-ghost {
  height: 32px;
  padding: 0 13px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
  font-size: 12.5px;
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.btn-ghost:hover {
  border-color: var(--fn-brand);
  color: var(--fn-brand);
}
.ok-msg {
  color: var(--fn-success);
  font-size: 13px;
}
.err-msg {
  color: var(--fn-danger);
  font-size: 13px;
}

/* 状态 */
.status-grid {
  display: grid;
  grid-template-columns: 100px 1fr;
  row-gap: 12px;
  font-size: 13px;
}
.muted {
  color: var(--fn-text-tertiary);
}
.badge {
  display: inline-block;
  font-size: 12px;
  padding: 2px 9px;
  border-radius: 999px;
}
.badge.ok {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.badge.mock {
  background: var(--fn-bg-tertiary);
  color: var(--fn-text-secondary);
}

/* 数据 */
.para {
  font-size: 13px;
  color: var(--fn-text-secondary);
  line-height: 1.75;
  margin: 0 0 10px;
}
.para code {
  background: var(--fn-bg-tertiary);
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 12px;
}
.store-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 12px 0;
}
.store-item {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 13px;
  color: var(--fn-text-secondary);
}

/* MCP */
.mcp-empty {
  font-size: 13px;
  color: var(--fn-text-tertiary);
  padding: 14px 4px;
  text-align: center;
}

/* 工具级管控列表 */
.tool-policy-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tool-policy-row {
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--fn-bg-secondary);
}
.tp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
}
.tp-name-box {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.tp-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tp-qualified {
  font-size: 10.5px;
  color: var(--fn-text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tp-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}
.tp-select {
  padding: 6px 8px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  font-size: 11.5px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
}
.tp-timeout {
  width: 78px;
  padding: 6px 8px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  font-size: 11.5px;
  background: var(--fn-bg-secondary);
  color: var(--fn-text-primary);
}
/* 小尺寸开关 */
.switch.sm {
  width: 36px;
  height: 20px;
}
.switch.sm .knob {
  width: 14px;
  height: 14px;
}
.switch.sm.on .knob {
  transform: translateX(16px);
}

.mcp-transport-row {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}
.mcp-auth-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.btn-preset.sm {
  height: 30px;
  padding: 0 12px;
  font-size: 12.5px;
}
.mcp-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 4px;
  border-top: 1px solid var(--fn-border-primary);
}
.mcp-row:first-of-type {
  border-top: none;
}
.mcp-row-main {
  flex: 1;
  min-width: 0;
}
.mcp-row-title {
  display: flex;
  align-items: center;
  gap: 9px;
}
.mcp-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.mcp-badge {
  font-size: 11px;
  padding: 1px 9px;
  border-radius: 999px;
}
.mcp-badge.on {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.mcp-badge.err {
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
}
.mcp-badge.off {
  background: var(--fn-bg-tertiary);
  color: var(--fn-text-secondary);
}
.mcp-badge.builtin {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.mcp-badge.user {
  background: var(--fn-success-bg);
  color: var(--fn-success);
}
.mcp-badge.project {
  background: var(--fn-warning-bg);
  color: var(--fn-warning);
}

/* 技能库 */
.skill-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 0;
}
.skill-card {
  text-align: left;
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  border-radius: 0;
  background: var(--fn-bg-primary);
  padding: 19px 0;
  transition: all var(--fn-transition-fast);
}
.skill-card:hover {
  border-color: var(--fn-brand);
  box-shadow: none;
}
.skill-card.off {
  opacity: 0.55;
  background: var(--fn-bg-secondary);
}
.skill-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.skill-card-main {
  display: block;
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.skill-card-main:disabled {
  opacity: 0.6;
  cursor: wait;
}
.skill-card-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
  margin-bottom: 5px;
}
.skill-card-desc {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.skill-card-open {
  margin-top: 9px;
  font-size: 12px;
  color: var(--fn-brand);
  font-weight: 600;
}
.skill-detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}
.skill-detail-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}
.danger-text {
  color: var(--fn-danger);
}
.skill-body {
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--fn-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--fn-bg-layout);
  border: 1px solid var(--fn-border-primary);
  border-radius: 9px;
  padding: 14px 16px;
  margin: 0;
  max-height: 520px;
  overflow-y: auto;
}
.mcp-row-sub {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: 5px;
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.mcp-tag {
  background: var(--fn-bg-tertiary);
  padding: 1px 7px;
  border-radius: 5px;
}
.mcp-detail {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}
.mcp-row-err {
  margin-top: 5px;
  font-size: 11.5px;
  color: var(--fn-danger);
  line-height: 1.5;
}
.mcp-actions {
  display: flex;
  gap: 5px;
  flex-shrink: 0;
}
.icon-btn {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-secondary);
  cursor: pointer;
}
.icon-btn:hover {
  border-color: var(--fn-brand);
  color: var(--fn-brand);
}
.icon-btn .on {
  color: var(--fn-success);
}
.icon-btn.danger:hover {
  border-color: var(--fn-danger);
  color: var(--fn-danger);
}
.form-grid textarea {
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 8px 11px;
  font-size: 12.5px;
  font-family: inherit;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
  resize: vertical;
}
.form-grid textarea:focus {
  outline: none;
  border-color: var(--fn-brand);
  box-shadow: 0 0 0 2px var(--fn-bg-active);
}

/* 钩子 */
.hook-event-block {
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  padding: 11px 13px;
  margin-bottom: 10px;
}
.hook-event-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.hook-event-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--fn-text-primary);
}
.hook-decision-tag {
  font-size: 10.5px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--fn-danger-bg);
  color: var(--fn-danger);
}
.hook-empty {
  font-size: 12px;
  color: var(--fn-text-tertiary);
}
.hook-group {
  margin-top: 6px;
}
.hook-matcher {
  font-size: 11px;
  color: var(--fn-text-tertiary);
  margin-bottom: 5px;
}
.hook-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 5px;
}
.hook-cmd {
  font-size: 11.5px;
  background: var(--fn-bg-tertiary);
  padding: 3px 8px;
  border-radius: 6px;
  color: var(--fn-text-secondary);
  word-break: break-all;
}
.hook-cmd.win {
  background: var(--fn-bg-brand-light);
  color: var(--fn-brand);
}
.hook-test-result {
  margin-top: 12px;
  border-top: 1px solid var(--fn-border-primary);
  padding-top: 10px;
}
.hook-test-summary {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--fn-text-primary);
  margin-bottom: 8px;
}
.hook-test-item {
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 7px;
}
.hook-test-cmd {
  font-size: 12px;
  font-weight: 600;
  color: var(--fn-text-primary);
  word-break: break-all;
}
.hook-test-meta {
  font-size: 11px;
  color: var(--fn-text-tertiary);
  margin-top: 3px;
}
.hook-test-out {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--fn-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.hook-test-out.err {
  color: var(--fn-danger);
}
.form-grid select {
  min-height: 38px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 0 9px;
  font-size: 13px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.form-grid select:focus {
  outline: none;
  border-color: var(--fn-brand);
}
.settings-desc,
.card-hint,
.toggle-desc,
.para {
  line-height: 1.8;
}
.card-head-row {
  flex-wrap: wrap;
  margin-bottom: 24px;
}
.save-row {
  border-top: 1px solid var(--fn-border-primary);
  padding-top: 21px;
  flex-wrap: wrap;
}
.card-foot-row {
  flex-wrap: wrap;
}
.form-grid input,
.form-grid textarea,
.form-grid select {
  width: 100%;
  min-width: 0;
}
.load-error {
  padding: 10px 12px;
  color: var(--fn-danger);
  background: var(--fn-danger-bg);
  border-radius: 8px;
  font-size: 12px;
}
.am-options {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.effort-options {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}
.tool-policy-row {
  flex-wrap: wrap;
}
.tp-controls {
  margin-left: auto;
}
.skill-detail-head {
  flex-wrap: wrap;
}
@media (max-width: 1100px) {
  .settings-page {
    padding: 30px 28px;
  }
  .settings-layout {
    grid-template-columns: 145px minmax(0, 1fr);
    gap: 28px;
  }
  .tab {
    padding: 10px 8px;
    font-size: 12px;
  }
  .effort-options {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 760px) {
  .settings-page {
    padding: 25px 20px;
  }
  .settings-layout {
    display: block;
    padding-top: 12px;
  }
  .tabs {
    position: static;
    flex-direction: row;
    overflow-x: auto;
    padding-bottom: 13px;
    margin-bottom: 25px;
    border-bottom: 1px solid var(--fn-border-primary);
  }
  .tab {
    flex-shrink: 0;
  }
  .tab-pane {
    max-width: none;
  }
  .am-options {
    grid-template-columns: minmax(0, 1fr);
  }
  .effort-options {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.model-main {
  border: 0;
  margin-bottom: 0;
  padding-bottom: 0;
}
.settings-section-head {
  margin-bottom: 28px;
}
.settings-section-head h2 {
  margin: 0 0 8px;
  font-size: 17px;
  font-weight: 500;
  letter-spacing: -0.3px;
}
.settings-section-head p {
  margin: 0;
  font-size: 12px;
  line-height: 1.8;
  color: var(--muted);
}
.settings-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 23px;
}
.settings-field > span {
  font-size: 12px;
  color: var(--text);
}
.settings-field > label {
  font-size: 12px;
  color: var(--text);
}
.model-field-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}
.model-field-heading label {
  font-size: 12px;
  color: var(--text);
}
.model-fetch-button {
  flex-shrink: 0;
  height: auto;
  min-height: 30px;
  padding: 5px 10px;
  font-size: 11px;
}
.model-discovery-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 11px;
  background: var(--fn-bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.model-discovery-panel select {
  text-overflow: ellipsis;
}
.settings-field input,
.settings-field select {
  width: 100%;
  min-height: 38px;
  border: 1px solid var(--input-border);
  border-radius: var(--radius);
  padding: 9px 11px;
  color: var(--text);
  background: var(--surface);
  font: inherit;
  font-size: 12px;
  line-height: 1.6;
}
.settings-field small {
  color: var(--muted);
  font-size: 10px;
  line-height: 1.7;
}
.settings-field small.err-msg {
  color: var(--fn-danger);
}
.settings-field input:focus,
.settings-field select:focus {
  outline: 0;
  border-color: var(--input-focus);
  box-shadow: var(--input-focus-shadow);
}
.settings-password {
  position: relative;
}
.settings-password input {
  padding-right: 53px;
}
.settings-password button {
  position: absolute;
  top: 4px;
  bottom: 4px;
  right: 7px;
  padding: 0 6px;
  border: 0;
  background: none;
  color: var(--muted);
  font-size: 10px;
}
.clear-key-option {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}
.clear-key-option input,
.settings-field .clear-key-option input {
  width: 14px;
  min-height: 14px;
  height: 14px;
  margin: 0;
  padding: 0;
  accent-color: var(--primary);
}
.clear-key-option label {
  cursor: pointer;
}
.settings-subsection {
  margin-top: 30px;
  border-top: 1px solid var(--border);
  padding-top: 24px;
}
.settings-subsection h3 {
  margin: 0 0 18px;
  font-size: 13px;
  font-weight: 500;
}
.settings-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 28px;
  border-top: 1px solid var(--border);
  padding-top: 21px;
}
.settings-form-actions button {
  min-height: 36px;
  height: auto;
  border-radius: 8px;
  padding: 7px 13px;
  font-size: 13px;
  font-weight: 500;
}
.advanced-settings {
  margin-top: 28px;
  border-top: 1px solid var(--border);
  padding-top: 18px;
}
.advanced-settings summary {
  cursor: pointer;
  color: var(--text);
  font-size: 12px;
  margin-bottom: 24px;
}
.advanced-settings summary span {
  margin-left: 8px;
  color: var(--muted);
  font-size: 10px;
}
@media (max-width: 760px) {
  .tab {
    width: auto;
  }
}
</style>

