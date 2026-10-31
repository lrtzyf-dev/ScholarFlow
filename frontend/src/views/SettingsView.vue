<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  getHealth,
  getSettings,
  saveSettings,
  testConnection,
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
  type McpServer,
  type McpServerConfig,
  type Skill,
  type HookRun,
  type McpDiscoveredTool,
  type McpToolPolicy,
  type AgentsMdMeta,
} from '../api'
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
const activeTab = ref('model')
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
  desktop_notifications: true,
  memory_recall: true,
  memory_capture: true,
})

const REASONING_EFFORTS = [
  { key: '', label: '不传（模型默认）', desc: '不发送 reasoning_effort，兼容性最好' },
  { key: 'minimal', label: 'minimal', desc: '思考最少，最快最省' },
  { key: 'low', label: 'low', desc: '轻度推理' },
  { key: 'medium', label: 'medium', desc: '均衡（多数推理模型默认）' },
  { key: 'high', label: 'high', desc: '深度推理，最慢最强' },
]
const saving = ref(false)
const message = ref('')
const error = ref('')

const presets = [
  { name: '阿里百炼（Qwen）', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-flash', emodel: 'text-embedding-v3' },
  { name: '硅基流动', base: 'https://api.siliconflow.cn/v1', model: 'Qwen/Qwen2.5-7B-Instruct', emodel: 'BAAI/bge-m3' },
  { name: 'DeepSeek', base: 'https://api.deepseek.com', model: 'deepseek-chat', emodel: '' },
]

function applyPreset(p: (typeof presets)[number]) {
  form.llm_base_url = p.base
  form.llm_model = p.model
  if (p.emodel) {
    form.embedding_base_url = p.base
    form.embedding_model = p.emodel
  }
}

function isPresetActive(p: (typeof presets)[number]) {
  return form.llm_base_url === p.base
}

const testing = ref(false)
const testResult = ref<{ ok: boolean; message: string } | null>(null)
async function onTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await testConnection({
      llm_base_url: form.llm_base_url,
      llm_api_key: form.llm_api_key,
      llm_model: form.llm_model,
    })
  } catch (e: any) {
    testResult.value = { ok: false, message: e?.message || '测试失败' }
  } finally {
    testing.value = false
  }
}

async function refresh() {
  health.value = await getHealth()
  loaded.value = await getSettings()
  form.llm_base_url = loaded.value.llm_base_url
  form.llm_model = loaded.value.llm_model
  form.embedding_base_url = loaded.value.embedding_base_url
  form.embedding_model = loaded.value.embedding_model
  form.approval_mode = (loaded.value as any).approval_mode || 'auto'
  form.work_rules = (loaded.value as any).work_rules || ''
  form.reasoning_effort = (loaded.value as any).reasoning_effort || ''
  form.desktop_notifications = (loaded.value as any).desktop_notifications !== false
  form.memory_recall = (loaded.value as any).memory_recall !== false
  form.memory_capture = (loaded.value as any).memory_capture !== false
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
  authMode: 'none' as 'none' | 'bearer' | 'oauth',
  headersText: '',
  bearerToken: '',
  oauthClientId: '',
  oauthClientSecret: '',
  oauthTokenUrl: '',
  oauthScope: '',
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
}

async function loadMcp() {
  mcpServers.value = await listMcpServers()
}

function editMcpServer(s: McpServer) {
  mcpForm.editing = true
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
    const args = mcpForm.argsText.trim()
      ? mcpForm.argsText.trim().split(/\s+/)
      : []
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
    }
    if (mcpForm.authMode === 'oauth' && mcpForm.oauthTokenUrl.trim()) {
      cfg.oauth = {
        client_id: mcpForm.oauthClientId.trim(),
        client_secret: mcpForm.oauthClientSecret.trim(),
        token_url: mcpForm.oauthTokenUrl.trim(),
        scope: mcpForm.oauthScope.trim(),
      }
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
  const [toolsRes, polRes] = await Promise.all([
    listDiscoveredTools(),
    getMcpPolicy(),
  ])
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

async function loadSkills() {
  skills.value = await listSkills()
}

async function openSkill(s: Skill) {
  skillBusy.value = true
  try {
    activeSkill.value = await getSkill(s.name)
  } finally {
    skillBusy.value = false
  }
}

function closeSkill() {
  activeSkill.value = null
}

function tierLabel(tier?: string) {
  if (tier === 'builtin') return { text: '内置', cls: 'builtin' }
  if (tier === 'user') return { text: '用户', cls: 'user' }
  if (tier === 'project') return { text: '项目', cls: 'project' }
  return { text: tier || '', cls: '' }
}

async function toggleSkillDisabled(s: Skill) {
  const next = !s.disabled
  await setSkillDisabled(s.name, next)
  await loadSkills()
  if (activeSkill.value && activeSkill.value.name === s.name) {
    activeSkill.value = await getSkill(s.name)
  }
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
    await createSkill({ ...skillForm })
    skillFormMsg.value = '用户技能已创建，可在聊天中用 $' + skillForm.name + ' 显式调用。'
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
    await deleteSkill(s.name)
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

onMounted(() => {
  refresh()
  loadMcp()
  loadSkills()
  loadHooks()
  loadToolPolicy()
  loadAgentsMd()
})

async function onSave() {
  saving.value = true
  message.value = ''
  error.value = ''
  try {
    const payload: Record<string, string | boolean> = {
      llm_base_url: form.llm_base_url,
      llm_model: form.llm_model,
      embedding_base_url: form.embedding_base_url,
      embedding_model: form.embedding_model,
      approval_mode: form.approval_mode,
      work_rules: form.work_rules,
      reasoning_effort: form.reasoning_effort,
      desktop_notifications: form.desktop_notifications,
      memory_recall: form.memory_recall,
      memory_capture: form.memory_capture,
    }
    if (form.llm_api_key) payload.llm_api_key = form.llm_api_key
    if (form.embedding_api_key) payload.embedding_api_key = form.embedding_api_key
    await saveSettings(payload)
    await refresh()
    form.llm_api_key = ''
    form.embedding_api_key = ''
    message.value = '设置已保存并立即生效。'
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
      <div class="settings-title">应用设置</div>
      <div class="settings-desc">模型接口、运行状态与数据存储配置</div>
    </div>

    <!-- 选项卡 -->
    <div class="tabs">
      <button
        v-for="t in tabs"
        :key="t.key"
        type="button"
        class="tab"
        :class="{ active: activeTab === t.key }"
        @click="activeTab = t.key"
      >
        <component :is="t.icon" :size="15" :stroke-width="1.9"/>
        <span>{{ t.label }}</span>
      </button>
    </div>

    <div class="tab-content">
      <!-- 模型接口 -->
      <div v-show="activeTab === 'model'" class="tab-pane">
        <div class="card">
          <div class="card-head-row">
            <div>
              <div class="card-title">大模型接口（OpenAI 兼容）</div>
              <div class="card-hint">支持阿里百炼、硅基流动、DeepSeek、本地 vLLM 等任何 OpenAI 兼容端点</div>
            </div>
            <span class="conn-status" :class="health && health.llm_mode === 'mock' ? 'offline' : 'online'">
              <span class="pulse"></span>
              {{ health && health.llm_mode === 'mock' ? '离线模式' : '云端已连接' }}
            </span>
          </div>
          <div class="preset-row">
            <button
              v-for="p in presets"
              :key="p.name"
              class="btn-preset"
              :class="{ active: isPresetActive(p) }"
              @click="applyPreset(p)"
            >{{ p.name }}</button>
          </div>
          <div class="form-grid">
            <label>接口地址</label>
            <input v-model="form.llm_base_url" placeholder="https://api.example.com/v1" />
            <label>API Key</label>
            <input v-model="form.llm_api_key" type="password" :placeholder="loaded?.llm_api_key_set ? '已配置（留空则不修改）' : '留空则离线运行' " />
            <label>模型名称</label>
            <input v-model="form.llm_model" placeholder="qwen-flash" />
          </div>
          <div v-if="testResult" class="test-result" :class="testResult.ok ? 'ok' : 'fail'">
            {{ testResult.message }}
          </div>
          <div class="card-foot-row">
            <button class="btn-ghost" :disabled="testing" @click="onTest">
              {{ testing ? '测试中…' : '测试连接' }}
            </button>
          </div>
        </div>

        <div class="card">
          <div class="card-title">Embedding 向量化（可选）</div>
          <div class="form-grid">
            <label>接口地址</label>
            <input v-model="form.embedding_base_url" placeholder="留空则沿用大模型接口地址" />
            <label>API Key</label>
            <input v-model="form.embedding_api_key" type="password" :placeholder="loaded?.llm_api_key_set ? '已配置（留空则不修改）' : 'sk-xxxxxxxx'" />
            <label>模型名称</label>
            <input v-model="form.embedding_model" placeholder="text-embedding-v3" />
          </div>
          <div class="card-hint" style="margin-top:12px">
            不填任何 Key 也能完整运行：解析、混合检索、引用闸门、SSE 过程面板、图谱均可用；仅答案文字由离线模板生成。
          </div>

          <!-- 审批模式（对应 Codex 三级） -->
          <div class="approval-mode">
            <label class="am-label">工具审批模式</label>
            <div class="am-options">
              <button
                v-for="m in APPROVAL_MODES"
                :key="m.key"
                type="button"
                class="am-option"
                :class="{ active: form.approval_mode === m.key }"
                @click="form.approval_mode = m.key"
              >
                <span class="am-title">{{ m.label }}</span>
                <span class="am-desc">{{ m.desc }}</span>
              </button>
            </div>
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
            <p class="wr-hint">仅对支持 reasoning_effort 的推理模型生效（如 Qwen3、DeepSeek、o 系列）；选「不传」可兼容所有模型。</p>
          </div>

          <!-- 分层工作规范（AGENTS.md，对标 Codex） -->
          <div class="work-rules agents-md">
            <div class="am-head">
              <label class="am-label">工作规范（分层 AGENTS.md）</label>
              <span v-if="agentsMeta" class="am-path" :title="agentsMeta.global_path">全局文件：{{ agentsMeta.global_path }}</span>
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
                <button type="button" class="btn-mini" :disabled="globalSaving" @click="saveGlobalRules">
                  {{ globalSaving ? '保存中…' : '保存全局规范' }}
                </button>
                <button type="button" class="btn-mini ghost" :disabled="initBusy" @click="onInitAgents(false)">
                  创建全局脚手架
                </button>
                <button type="button" class="btn-mini ghost" :disabled="initBusy" @click="onInitAgents(true)">
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

            <p class="wr-hint">此处两层对所有会话生效。真实项目规范请在聊天的「项目文件 → 目录规范」查看；也可用 /init 在当前项目创建 AGENTS.md。每层上限 32 KiB。</p>
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
              ><span class="knob"></span></button>
            </div>
            <div class="toggle-row">
              <div class="toggle-text">
                <span class="toggle-name">使用记忆</span>
                <span class="toggle-desc">提问时载入本会话历史上下文（含早期对话压缩摘要）</span>
              </div>
              <button
                type="button"
                class="switch"
                :class="{ on: form.memory_recall }"
                role="switch"
                :aria-checked="form.memory_recall"
                @click="form.memory_recall = !form.memory_recall"
              ><span class="knob"></span></button>
            </div>
            <div class="toggle-row">
              <div class="toggle-text">
                <span class="toggle-name">生成记忆</span>
                <span class="toggle-desc">让本轮问答进入后续会话的记忆；关闭后不影响本次记录保存</span>
              </div>
              <button
                type="button"
                class="switch"
                :class="{ on: form.memory_capture }"
                role="switch"
                :aria-checked="form.memory_capture"
                @click="form.memory_capture = !form.memory_capture"
              ><span class="knob"></span></button>
            </div>
          </div>
        </div>

        <div class="save-row">
          <button class="btn-primary" :disabled="saving" @click="onSave">{{ saving ? '保存中…' : '保存设置' }}</button>
          <span v-if="message" class="ok-msg">{{ message }}</span>
          <span v-if="error" class="err-msg">{{ error }}</span>
        </div>
      </div>

      <!-- MCP 服务器 -->
      <div v-show="activeTab === 'mcp'" class="tab-pane">
        <div class="card">
          <div class="card-head-row">
            <div>
              <div class="card-title">MCP 服务器（Model Context Protocol）</div>
              <div class="card-hint">连接后，外部工具会自动出现在 Agent 工具集中（如 arxiv 论文检索、网络抓取等）</div>
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
                <span class="mcp-badge" :class="statusLabel(s).cls">{{ statusLabel(s).text }}</span>
              </div>
              <div class="mcp-row-sub">
                <span class="mcp-tag">{{ s.transport }}</span>
                <span class="mcp-tools">{{ s.tool_count }} 个工具</span>
                <span v-if="s.transport === 'http'" class="mcp-detail">{{ s.url }}</span>
                <span v-else-if="s.transport === 'builtin'" class="mcp-detail">随软件本体运行，无需外部环境</span>
                <span v-else class="mcp-detail">{{ s.command }}</span>
              </div>
              <div v-if="s.error" class="mcp-row-err">{{ s.error }}</div>
            </div>
            <div class="mcp-actions">
              <!-- 内置：只允许启停，不可编辑/删除 -->
              <button v-if="s.builtin" class="icon-btn" :title="s.enabled ? '停用' : '启用'" @click="onToggleMcp(s)">
                <Power :size="14" :class="s.enabled ? 'on' : ''"/>
              </button>
              <template v-else>
                <button v-if="s.status !== 'connected'" class="icon-btn" title="连接" @click="onConnectMcp(s)">
                  <Power :size="14"/>
                </button>
                <button v-else class="icon-btn" title="断开" @click="onDisconnectMcp(s)">
                  <Power :size="14" class="on"/>
                </button>
                <button class="icon-btn" title="编辑" @click="editMcpServer(s)">
                  <Pencil :size="14"/>
                </button>
                <button class="icon-btn danger" title="删除" @click="onRemoveMcp(s)">
                  <Trash2 :size="14"/>
                </button>
              </template>
            </div>
          </div>
        </div>

        <!-- 工具级管控 -->
        <div class="card">
          <div class="card-title">工具级管控（白/黑名单 · 单工具审批 · 超时）</div>
          <div class="card-hint" style="margin-bottom:12px">
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
                ><span class="knob"></span></button>
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
          <div v-if="policyMsg" class="test-result ok" style="margin-top:12px">{{ policyMsg }}</div>
          <div v-if="policyErr" class="test-result fail" style="margin-top:12px">{{ policyErr }}</div>
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
            >stdio（本地子进程）</button>
            <button
              class="btn-preset"
              :class="{ active: mcpForm.transport === 'http' }"
              @click="mcpForm.transport = 'http'"
            >streamable-http（远程）</button>
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
                <button type="button" class="btn-preset sm"
                        :class="{ active: mcpForm.authMode === 'none' }"
                        @click="mcpForm.authMode = 'none'">无</button>
                <button type="button" class="btn-preset sm"
                        :class="{ active: mcpForm.authMode === 'bearer' }"
                        @click="mcpForm.authMode = 'bearer'">静态 Token</button>
                <button type="button" class="btn-preset sm"
                        :class="{ active: mcpForm.authMode === 'oauth' }"
                        @click="mcpForm.authMode = 'oauth'">OAuth</button>
              </div>

              <template v-if="mcpForm.authMode !== 'none'">
                <label>自定义请求头</label>
                <textarea v-model="mcpForm.headersText" rows="2"
                          placeholder="每行一个，KEY=VALUE，如 X-Org=lab"></textarea>
              </template>

              <template v-if="mcpForm.authMode === 'bearer'">
                <label>访问 Token</label>
                <input v-model="mcpForm.bearerToken" placeholder="Bearer token（不含 Bearer 前缀）" />
              </template>

              <template v-if="mcpForm.authMode === 'oauth'">
                <label>Token 端点</label>
                <input v-model="mcpForm.oauthTokenUrl" placeholder="https://id.example.com/oauth/token" />
                <label>Client ID</label>
                <input v-model="mcpForm.oauthClientId" placeholder="客户端标识" />
                <label>Client Secret</label>
                <input v-model="mcpForm.oauthClientSecret" type="password" placeholder="客户端密钥" />
                <label>Scope（可选）</label>
                <input v-model="mcpForm.oauthScope" placeholder="如 read.write" />
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
              <textarea v-model="mcpForm.envText" rows="3" placeholder="每行一个，KEY=VALUE，如 SEMANTIC_SCHOLAR_API_KEY=xxx"></textarea>
            </template>
          </div>

          <div v-if="mcpMsg" class="test-result ok" style="margin-top:12px">{{ mcpMsg }}</div>
          <div v-if="mcpErr" class="test-result fail" style="margin-top:12px">{{ mcpErr }}</div>

          <div class="card-foot-row">
            <button class="btn-primary" :disabled="mcpBusy" @click="saveMcpServer">
              {{ mcpBusy ? '处理中…' : mcpForm.editing ? '保存并连接' : '添加并连接' }}
            </button>
            <button v-if="mcpForm.editing" class="btn-ghost" @click="resetMcpForm">取消编辑</button>
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
                三级技能按优先级叠加，同名高优先级覆盖；禁用后不自动加载，聊天中仍可用 $名称 显式调用。
              </div>
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
                <span class="mcp-badge" :class="tierLabel(s.tier).cls">{{ tierLabel(s.tier).text }}</span>
                <button
                  type="button"
                  class="switch sm"
                  :class="{ on: !s.disabled }"
                  :title="s.disabled ? '已禁用，点击启用' : '已启用，点击禁用'"
                  @click="toggleSkillDisabled(s)"
                ><span class="knob"></span></button>
              </div>
              <button type="button" class="skill-card-main" :disabled="skillBusy" @click="openSkill(s)">
                <div class="skill-card-name">{{ s.title }}</div>
                <div class="skill-card-desc">{{ s.description }}</div>
                <div class="skill-card-open">{{ s.disabled ? '已禁用 · 查看指南' : '查看指南' }}</div>
              </button>
            </div>
          </div>

          <div v-else class="skill-detail">
            <div class="skill-detail-head">
              <div class="card-title">
                {{ activeSkill.title }}
                <span class="mcp-badge" :class="tierLabel(activeSkill.tier).cls">{{ tierLabel(activeSkill.tier).text }}</span>
              </div>
              <div class="skill-detail-actions">
                <button class="btn-ghost" @click="toggleSkillDisabled(activeSkill)">
                  {{ activeSkill.disabled ? '启用技能' : '禁用技能' }}
                </button>
                <button
                  v-if="activeSkill.tier === 'user'"
                  class="btn-ghost danger-text"
                  @click="removeUserSkill(activeSkill)"
                >删除</button>
                <button class="btn-ghost" @click="closeSkill">返回列表</button>
              </div>
            </div>
            <pre class="skill-body">{{ activeSkill.content }}</pre>
          </div>
        </div>

        <!-- 新建用户技能 -->
        <div class="card">
          <div class="card-title">新建用户技能</div>
          <div class="card-hint" style="margin-bottom:12px">
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
            <textarea v-model="skillForm.content" rows="6" placeholder="第一步……；第二步……"></textarea>
          </div>
          <div v-if="skillFormMsg" class="test-result ok" style="margin-top:12px">{{ skillFormMsg }}</div>
          <div v-if="skillFormErr" class="test-result fail" style="margin-top:12px">{{ skillFormErr }}</div>
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
              <div class="card-hint">在 Agent 关键事件点自动执行命令：入库后打标签、压缩前备份、完成后通知等</div>
            </div>
          </div>

          <div v-for="event in hookEvents" :key="event" class="hook-event-block">
            <div class="hook-event-head">
              <span class="hook-event-name">{{ event }}</span>
              <span v-if="hookDecisionEvents.includes(event)" class="hook-decision-tag">可阻断</span>
            </div>
            <div v-if="!hookConfig[event] || !hookConfig[event].length" class="hook-empty">未配置</div>
            <template v-else>
              <div v-for="(group, gi) in hookConfig[event]" :key="gi" class="hook-group">
                <div class="hook-matcher">matcher：{{ group.matcher || '.*（全部）' }}</div>
                <div v-for="(h, hi) in group.hooks" :key="hi" class="hook-item">
                  <code class="hook-cmd">{{ h.command }}</code>
                  <code v-if="h.commandWindows" class="hook-cmd win">Windows：{{ h.commandWindows }}</code>
                  <button class="icon-btn danger" title="删除" @click="removeHook(event, gi, hi)">
                    <Trash2 :size="13"/>
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
              <Plus :size="14" style="vertical-align:-2px"/> 添加
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
              <div class="hook-test-meta">退出码：{{ r.exit_code }}<span v-if="r.timed_out">（超时）</span></div>
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
            <span class="muted">大模型</span>
            <span class="badge" :class="health.llm_mode === 'mock' ? 'mock' : 'ok'">
              {{ health.llm_mode === 'mock' ? '离线脚本模型' : '真实大模型' }}
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
        <div class="card">
          <div class="card-title"><HardDrive :size="15" style="vertical-align:-2px"/> 本地优先存储</div>
          <p class="para">
            研序采用本地优先（local-first）设计：论文、解析结果、向量索引与问答记录均保存在本机的
            应用数据目录（<code>%APPDATA%\ScholarFlow</code>），不会上传到第三方服务器。
          </p>
          <div class="store-list">
            <div class="store-item"><FileText :size="15"/><span>论文原件（PDF）</span></div>
            <div class="store-item"><Database :size="15"/><span>SQLite 数据库（scholarflow.db）</span></div>
            <div class="store-item"><Activity :size="15"/><span>向量索引（vectors.npz）</span></div>
          </div>
          <p class="para">
            卸载或删除该目录即可清除全部数据；重新打开程序会自动重建。
          </p>
        </div>
      </div>

      <!-- 合规说明 -->
      <div v-show="activeTab === 'compliance'" class="tab-pane">
        <div class="card">
          <div class="card-title">开源与许可</div>
          <p class="para">
            界面设计参考开源项目 <strong>Octop</strong>（MIT License，Copyright © 2026 Octop），
            布局与组件经修改后使用，未使用其名称、商标及吉祥物。
          </p>
          <p class="para">
            样例字体 <strong>Noto Sans SC</strong> 基于 SIL Open Font License 1.1（© 2014–2021 Adobe）。
            其余依赖均为 MIT / BSD / Apache 2.0 等宽松许可。
          </p>
          <button class="btn-ghost" @click="router.push('/about')">查看完整关于与许可证</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  height: 100%;
  overflow-y: auto;
  background: var(--fn-bg-layout);
}
.settings-head {
  padding: 22px 28px 0;
}
.settings-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--fn-text-primary);
}
.settings-desc {
  font-size: 13px;
  color: var(--fn-text-tertiary);
  margin-top: 2px;
}

/* 选项卡 */
.tabs {
  display: flex;
  gap: 4px;
  padding: 16px 28px 0;
  border-bottom: 1px solid var(--fn-border-primary);
}
.tab {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 14px;
  border: none;
  background: transparent;
  color: var(--fn-text-secondary);
  font-size: 13px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.tab:hover {
  color: var(--fn-text-primary);
}
.tab.active {
  color: var(--fn-brand);
  border-bottom-color: var(--fn-brand);
  font-weight: 600;
}

.tab-content {
  padding: 20px 28px 28px;
}
.tab-pane {
  max-width: 820px;
}

.card {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 14px;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--fn-text-primary);
  margin-bottom: 4px;
}
.card-hint {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin-bottom: 0;
}
.approval-mode { margin-top: 16px; }
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
.am-option:hover { border-color: var(--fn-brand); }
.am-option.active {
  border-color: var(--fn-brand);
  background: rgba(10, 89, 247, 0.06);
}
.am-title { font-size: 12.5px; font-weight: 600; color: var(--fn-text-primary); }
.am-desc { font-size: 11px; line-height: 1.5; color: var(--fn-text-tertiary); }
@media (max-width: 720px) {
  .am-options { grid-template-columns: 1fr; }
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
.effort-option:hover { border-color: var(--fn-brand); }
.effort-option.active {
  border-color: var(--fn-brand);
  background: var(--fn-bg-brand-light);
}
.effort-label { font-size: 12px; font-weight: 600; color: var(--fn-text-primary); }
.effort-desc { font-size: 10px; line-height: 1.45; color: var(--fn-text-tertiary); }
@media (max-width: 720px) {
  .effort-options { grid-template-columns: repeat(2, 1fr); }
}
.work-rules { margin-top: 16px; }
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
.work-rules textarea:focus { outline: none; border-color: var(--fn-brand); }
.wr-hint { font-size: 11.5px; color: var(--fn-text-tertiary); margin: 6px 0 0; }

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
.am-layer { margin-top: 12px; }
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
.am-bytes.over { color: var(--fn-danger); font-weight: 600; }
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
  background: linear-gradient(135deg, var(--fn-brand), #4d83f9);
  color: #fff;
  font-size: 11.5px;
  cursor: pointer;
}
.btn-mini:hover { box-shadow: 0 3px 10px var(--fn-brand-shadow); }
.btn-mini:disabled { opacity: 0.55; cursor: default; box-shadow: none; }
.btn-mini.ghost {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  color: var(--fn-text-secondary);
}
.btn-mini.ghost:hover { border-color: var(--fn-brand); color: var(--fn-brand); box-shadow: none; }
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
.toggle-text { display: flex; flex-direction: column; gap: 3px; }
.toggle-name { font-size: 13px; font-weight: 600; color: var(--fn-text-primary); }
.toggle-desc { font-size: 11.5px; color: var(--fn-text-tertiary); line-height: 1.5; }
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
  transition: background 0.18s ease, border-color 0.18s ease;
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
.switch.on .knob { transform: translateX(18px); }
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
  grid-template-columns: 90px 1fr;
  gap: 10px 14px;
  align-items: center;
}
.form-grid label {
  font-size: 13px;
  color: var(--fn-text-secondary);
}
.form-grid input {
  height: 36px;
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
  background: linear-gradient(135deg, var(--fn-brand), #4d83f9);
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
.tp-name-box { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
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
.tp-controls { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
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
.switch.sm { width: 36px; height: 20px; }
.switch.sm .knob { width: 14px; height: 14px; }
.switch.sm.on .knob { transform: translateX(16px); }

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
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}
.skill-card {
  text-align: left;
  border: 1px solid var(--fn-border-primary);
  border-radius: 10px;
  background: var(--fn-bg-primary);
  padding: 12px 14px;
  transition: all var(--fn-transition-fast);
}
.skill-card:hover {
  border-color: var(--fn-brand);
  box-shadow: 0 3px 12px var(--fn-brand-shadow);
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
.hook-group { margin-top: 6px; }
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
.hook-test-out.err { color: var(--fn-danger); }
.form-grid select {
  height: 36px;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  padding: 0 9px;
  font-size: 13px;
  background: var(--fn-bg-primary);
  color: var(--fn-text-primary);
}
.form-grid select:focus { outline: none; border-color: var(--fn-brand); }
</style>
