<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ChevronDown, Check, Cpu, Plus, Search, Settings2, ShieldCheck, X } from 'lucide-vue-next'
import ModelProfileEditor from './ModelProfileEditor.vue'
import ModelProfilesManager from './ModelProfilesManager.vue'
import ConversationResources from './ConversationResources.vue'
import ConversationExecutionEnvironment from './ConversationExecutionEnvironment.vue'
import {
  getConversationOptions,
  saveConversationOptions,
  listModelProfiles,
  type ApprovalMode,
  type ConversationOptions,
  type ModelProfile,
  type RuntimeConfiguration,
} from '../api'

const props = defineProps<{
  conversationId: string
  disabled?: boolean
  running?: boolean
  activeRuntime?: RuntimeConfiguration | null
}>()
const emit = defineEmits<{ ready: [value: boolean] }>()
const options = ref<ConversationOptions | null>(null)
const profiles = ref<ModelProfile[]>([])
const loading = ref(true)
const saving = ref(false)
const resourceSaving = ref(false)
const environmentSaving = ref(false)
const error = ref('')
const notice = ref('')
const failedPatch = ref<{
  model_profile_id?: string | null
  approval_mode?: ApprovalMode
  reasoning_effort?: string | null
} | null>(null)
const modelMenu = ref(false)
const query = ref('')
const editorOpen = ref(false)
const editingProfile = ref<ModelProfile | null>(null)
const initialModelValues = ref<{ name: string; base_url: string; model_id: string }>()
const managerOpen = ref(false)
const root = ref<HTMLElement | null>(null)
const modelButton = ref<HTMLButtonElement | null>(null)
const modelSearch = ref<HTMLInputElement | null>(null)
const managerDialog = ref<HTMLElement | null>(null)
let disposed = false
let epoch = 0
let pending: Promise<boolean> | null = null
const currentModel = computed(() => options.value?.effective_model)
const capabilityNotes = computed(() => {
  if (props.running && props.activeRuntime?.capability_notices) return props.activeRuntime.capability_notices
  const notes: string[] = []
  if (currentModel.value?.capabilities.tools === 'unsupported') notes.push('当前模型不支持工具调用，仍可普通聊天；读取资料或执行操作时请选支持工具的模型。')
  if (currentModel.value?.capabilities.streaming === 'unsupported') notes.push('当前模型不支持流式输出，将在完整回答返回后显示。')
  return notes
})
const disabled = computed(() =>
  Boolean(props.disabled || loading.value || saving.value || !options.value)
)
const ready = computed(
  () =>
    !loading.value &&
    !saving.value &&
    !resourceSaving.value &&
    !environmentSaving.value &&
    Boolean(options.value) &&
    !error.value &&
    !options.value?.model_error
)
watch(ready, (value) => emit('ready', value), { immediate: true })
const groups = computed(() => {
  const grouped = new Map<string, ModelProfile[]>()
  const needle = query.value.toLocaleLowerCase().trim()
  for (const profile of profiles.value) {
    if (
      !profile.enabled ||
      (needle &&
        !`${profile.name} ${profile.model_id} ${profile.base_url}`
          .toLocaleLowerCase()
          .includes(needle))
    )
      continue
    let host = '本机离线'
    try {
      host = new URL(profile.base_url).host
    } catch {
      /* Offline legacy configuration. */
    }
    const items = grouped.get(host) || []
    items.push(profile)
    grouped.set(host, items)
  }
  return [...grouped].map(([label, items]) => ({ label, items }))
})
const selectedId = computed(
  () => options.value?.model_profile_id || options.value?.effective_model?.id
)
const reasoningAvailable = computed(
  () =>
    currentModel.value?.capabilities.reasoning === 'supported' ||
    Boolean(currentModel.value?.reasoning_effort)
)
function failureMessage(reason: unknown) {
  const failure = reason as {
    response?: { status?: number; data?: { detail?: unknown } }
    message?: string
  }
  if (failure.response?.status === 409)
    return '会话设置已在其他窗口更新。本次选择已保留，可载入最新设置或重试保存'
  const detail = failure.response?.data?.detail
  return typeof detail === 'string' ? detail : failure.message || '会话设置尚未保存，请重试'
}
async function load(): Promise<boolean> {
  const request = ++epoch
  const cid = props.conversationId
  loading.value = true
  error.value = ''
  notice.value = ''
  failedPatch.value = null
  modelMenu.value = false
  try {
    const [saved, catalog] = await Promise.all([getConversationOptions(cid), listModelProfiles()])
    if (disposed || request !== epoch || cid !== props.conversationId) return false
    options.value = saved
    profiles.value = catalog.profiles
    return !saved.model_error
  } catch (reason) {
    if (!disposed && request === epoch) {
      options.value = null
      error.value = failureMessage(reason)
    }
    return false
  } finally {
    if (!disposed && request === epoch) loading.value = false
  }
}
async function save(patch: {
  model_profile_id?: string | null
  approval_mode?: ApprovalMode
  reasoning_effort?: string | null
}): Promise<boolean> {
  if (disabled.value || !options.value) return false
  const request = epoch
  const cid = props.conversationId
  saving.value = true
  error.value = ''
  const operation = (async () => {
    try {
      const saved = await saveConversationOptions(cid, {
        expected_revision: options.value!.revision,
        ...patch,
      })
      if (disposed || request !== epoch || cid !== props.conversationId) return false
      options.value = saved
      failedPatch.value = null
      return !saved.model_error
    } catch (reason) {
      if (!disposed && request === epoch) {
        error.value = failureMessage(reason)
        failedPatch.value = patch
      }
      return false
    } finally {
      if (!disposed && request === epoch) saving.value = false
    }
  })()
  pending = operation
  const result = await operation
  if (pending === operation) pending = null
  return result
}
async function ensureReady(): Promise<boolean> {
  const cid = props.conversationId,
    request = epoch
  if (pending) await pending
  if (disposed || cid !== props.conversationId || request !== epoch) return false
  return ready.value
}
async function retrySave() {
  const patch = failedPatch.value
  if (!patch) return
  const cid = props.conversationId
  if ((await load()) && cid === props.conversationId) await save(patch)
}
async function cycleApproval() {
  if (disabled.value || !options.value) return false
  const modes: ApprovalMode[] = ['always', 'auto', 'full_access']
  return save({
    approval_mode: modes[(modes.indexOf(options.value.approval_mode) + 1) % modes.length],
  })
}
async function cycleReasoning() {
  if (disabled.value || !options.value) return false
  if (!reasoningAvailable.value) {
    notice.value = '当前模型未声明支持推理强度，可在模型配置中确认能力和参数'
    return false
  }
  const choices = ['', 'low', 'medium', 'high', 'max']
  const current = options.value.reasoning_effort ?? currentModel.value?.reasoning_effort ?? ''
  return save({ reasoning_effort: choices[(choices.indexOf(current) + 1) % choices.length] })
}
async function openModelMenu() {
  if (disabled.value) return
  modelMenu.value = !modelMenu.value
  query.value = ''
  if (modelMenu.value) {
    await nextTick()
    modelSearch.value?.focus()
  }
}
async function selectModel(profile: ModelProfile) {
  modelMenu.value = false
  await save({ model_profile_id: profile.id, reasoning_effort: null })
  modelButton.value?.focus()
}
function showEditor() {
  editingProfile.value = null
  initialModelValues.value = undefined
  modelMenu.value = false
  editorOpen.value = true
}
function configureModel() {
  const profile = currentModel.value
  if (!profile || profile.mode !== 'unconfigured') return
  modelMenu.value = false
  if (profile.id === 'legacy-default') {
    editingProfile.value = null
    initialModelValues.value = {
      name: profile.model_id || '默认模型',
      base_url: profile.base_url,
      model_id: profile.model_id,
    }
  } else {
    editingProfile.value = profile
    initialModelValues.value = undefined
  }
  editorOpen.value = true
}
function showManager() {
  modelMenu.value = false
  managerOpen.value = true
}
async function added(profile: ModelProfile) {
  const cid = props.conversationId
  const request = epoch
  profiles.value = [...profiles.value.filter((item) => item.id !== profile.id), profile]
  await save({ model_profile_id: profile.id, reasoning_effort: null })
  if (!disposed && request === epoch && cid === props.conversationId) void refreshProfiles()
}
async function refreshProfiles() {
  if (disposed) return
  const cid = props.conversationId
  const request = epoch
  try {
    const [catalog, saved] = await Promise.all([listModelProfiles(), getConversationOptions(cid)])
    if (
      disposed ||
      request !== epoch ||
      cid !== props.conversationId ||
      saving.value ||
      saved.revision < (options.value?.revision || 0)
    )
      return
    profiles.value = catalog.profiles
    options.value = saved
  } catch {
    /* The next explicit reload shows connection failures. */
  }
}
function noteRunAdmission(runtime: RuntimeConfiguration | null) {
  const capturedRevision = Number(runtime?.policy_revision)
  if (!Number.isInteger(capturedRevision) || capturedRevision < 0 || !options.value) return
  if (capturedRevision > options.value.revision) {
    options.value = { ...options.value, revision: capturedRevision }
  }
}
function outside(event: MouseEvent) {
  if (!root.value?.contains(event.target as Node)) modelMenu.value = false
}
function menuKeys(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation()
    modelMenu.value = false
    modelButton.value?.focus()
    return
  }
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return
  const buttons = [
    ...(root.value?.querySelectorAll<HTMLButtonElement>(
      '.conversation-model-menu button:not(:disabled)'
    ) || []),
  ]
  if (!buttons.length) return
  event.preventDefault()
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const next =
    current < 0
      ? event.key === 'ArrowDown'
        ? 0
        : buttons.length - 1
      : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
  buttons[next]?.focus()
}
function managerKeys(event: KeyboardEvent) {
  if (editorOpen.value || event.key !== 'Tab') return
  const elements = [
    ...(managerDialog.value?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled)'
    ) || []),
  ]
  const first = elements[0],
    last = elements[elements.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  }
  if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
watch(
  () => props.conversationId,
  () => {
    options.value = null
    saving.value = false
    editorOpen.value = managerOpen.value = false
    void load()
  },
  { immediate: true }
)
watch(managerOpen, async (open) => {
  if (open) {
    await nextTick()
    managerDialog.value?.querySelector<HTMLButtonElement>('button')?.focus()
  } else modelButton.value?.focus()
})
watch(editorOpen, async (open) => {
  if (!open) {
    await nextTick()
    modelButton.value?.focus()
  }
})
onMounted(() => {
  window.addEventListener('click', outside)
  window.addEventListener('focus', refreshProfiles)
  window.addEventListener('scholarflow:model-profiles-updated', refreshProfiles)
})
onUnmounted(() => {
  disposed = true
  epoch++
  window.removeEventListener('click', outside)
  window.removeEventListener('focus', refreshProfiles)
  window.removeEventListener('scholarflow:model-profiles-updated', refreshProfiles)
})
defineExpose({
  openSetup: () => currentModel.value?.mode === 'unconfigured' ? configureModel() : showManager(),
  ensureReady,
  cycleApproval,
  cycleReasoning,
  options,
  syncOptions: refreshProfiles,
  noteRunAdmission,
})
</script>

<template>
  <div ref="root" class="conversation-controls">
    <div class="conversation-control-row">
      <div class="conversation-model-wrap">
        <button
          ref="modelButton"
          type="button"
          class="conversation-model-button"
          aria-label="选择会话模型"
          aria-haspopup="dialog"
          :aria-expanded="modelMenu"
          :disabled="disabled"
          :title="currentModel ? `${currentModel.name} · ${currentModel.model_id}` : '选择模型'"
          @click="openModelMenu"
        >
          <Cpu :size="14" /><span>{{
            loading ? '载入模型…' : currentModel?.name || '选择模型'
          }}</span
          ><ChevronDown :size="12" />
        </button>
        <div
          v-if="modelMenu"
          class="conversation-model-menu"
          role="dialog"
          aria-label="会话模型"
          @keydown="menuKeys"
        >
          <label class="conversation-model-search"
            ><Search :size="14" /><input
              ref="modelSearch"
              v-model="query"
              placeholder="搜索模型或服务商"
              aria-label="搜索会话模型"
          /></label>
          <div class="conversation-model-list">
            <div v-for="group in groups" :key="group.label">
              <div class="model-group-label">{{ group.label }}</div>
              <button
                v-for="profile in group.items"
                :key="profile.id"
                type="button"
                class="conversation-model-option"
                :aria-pressed="selectedId === profile.id"
                @click="selectModel(profile)"
              >
                <span
                  ><strong>{{ profile.name }}</strong
                  ><small>{{ profile.model_id || '离线模型' }}</small
                  ><small v-if="profile.mode === 'unconfigured'">待配置密钥</small
                  ><small v-else-if="profile.mode === 'mock'">本机示例响应；选择后进入演示</small
                  ><small v-else-if="profile.capabilities.tools === 'unsupported'"
                    >不支持工具调用</small
                  ><small v-else-if="profile.capabilities.tools === 'unknown'"
                    >工具能力未检测</small
                  ></span
                ><Check v-if="selectedId === profile.id" :size="15" />
              </button>
            </div>
            <p v-if="!groups.length" class="control-note">没有匹配的可用模型</p>
          </div>
          <div class="conversation-model-menu-actions">
            <button type="button" @click="showEditor"><Plus :size="14" />添加模型</button
            ><button type="button" @click="showManager"><Settings2 :size="14" />管理模型</button>
          </div>
        </div>
      </div>
      <label class="conversation-permission"
        ><ShieldCheck :size="13" /><select
          aria-label="会话权限"
          :disabled="disabled"
          :value="options?.approval_mode || 'auto'"
          @change="
            save({ approval_mode: ($event.target as HTMLSelectElement).value as ApprovalMode })
          "
        >
          <option value="always">逐次审批</option>
          <option value="auto">智能审批</option>
          <option value="full_access">全部权限</option>
        </select></label
      >
      <select
        v-if="reasoningAvailable"
        class="conversation-reasoning"
        aria-label="会话推理强度"
        :disabled="disabled"
        :value="options?.reasoning_effort ?? ''"
        @change="save({ reasoning_effort: ($event.target as HTMLSelectElement).value })"
      >
        <option value="">模型默认</option>
        <option
          v-for="value in ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']"
          :key="value"
          :value="value"
        >
          {{ value }}
        </option>
      </select>
      <span v-if="saving" class="control-note" role="status">正在保存…</span>
      <ConversationResources :conversation-id="conversationId" :disabled="props.disabled" :running="running"
        @busy="resourceSaving = $event"
        :active-budget-id="typeof activeRuntime?.resource_budget_id === 'string' ? activeRuntime.resource_budget_id : null" />
      <ConversationExecutionEnvironment :conversation-id="conversationId" :disabled="props.disabled" :running="running" @busy="environmentSaving=$event" />
    </div>
    <div v-if="running" class="conversation-running-config">
      本轮使用 {{ activeRuntime?.profile_name || activeRuntime?.model_id || '已绑定模型'
      }}<span>模型选择用于下一条消息；权限从下一次工具调用生效</span>
    </div>
    <div v-if="notice" class="control-note" role="status">{{ notice }}</div>
    <div v-for="item in capabilityNotes" :key="item" class="control-note" role="status">{{ item }}</div>
    <div v-if="currentModel?.mode === 'mock' && !running" class="control-note" role="status">
      离线演示使用本机示例响应。选择已配置的接口模型后使用真实模型。
    </div>
    <div v-if="error || options?.model_error" class="conversation-options-error" role="alert">
      {{ error || options?.model_error }}
      <button
        v-if="currentModel?.mode === 'unconfigured'"
        type="button"
        :disabled="saving || loading"
        @click="configureModel"
      >
        配置当前模型
      </button>
      <button type="button" :disabled="saving || loading" @click="load">重新载入</button
      ><button v-if="failedPatch" type="button" :disabled="saving || loading" @click="retrySave">
        重试保存我的选择
      </button>
    </div>
    <ModelProfileEditor
      v-model="editorOpen"
      :profile="editingProfile"
      :initial-values="initialModelValues"
      @saved="added"
    />
    <Teleport to="body"
      ><div v-if="managerOpen" class="profile-manager-mask" @mousedown.self="managerOpen = false">
        <section
          ref="managerDialog"
          class="profile-manager-dialog"
          role="dialog"
          aria-modal="true"
          aria-label="管理模型"
          @keydown.esc.stop="managerOpen = false"
          @keydown="managerKeys"
        >
          <button
            type="button"
            class="profile-manager-close"
            aria-label="关闭模型管理"
            @click="managerOpen = false"
          >
            <X :size="18" /></button
          ><ModelProfilesManager />
        </section></div
    ></Teleport>
  </div>
</template>

<style scoped>
.conversation-controls {
  min-width: 0;
  flex: 1 1 auto;
  color: var(--fn-text-secondary, #64748b);
}
.conversation-control-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-height: 30px;
}
button,
input,
select {
  font: inherit;
}
button,
select {
  color: inherit;
  cursor: pointer;
}
.conversation-model-wrap {
  position: relative;
  min-width: 0;
  max-width: 220px;
}
.conversation-model-button {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 5px 7px;
  min-height: 29px;
  max-width: 100%;
  border: 1px solid transparent;
  background: transparent;
  border-radius: 6px;
  font-size: 12px;
}
.conversation-model-button > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conversation-model-button svg {
  flex-shrink: 0;
}
.conversation-model-button:hover,
.conversation-permission:hover {
  background: var(--fn-bg-secondary, #f3f5f9);
}
.conversation-permission {
  display: flex;
  align-items: center;
  gap: 2px;
  border-radius: 6px;
  padding-left: 4px;
  flex-shrink: 0;
}
.conversation-permission select,
.conversation-reasoning {
  min-height: 29px;
  border: 0;
  background: transparent;
  font-size: 12px;
  max-width: 112px;
}
.conversation-reasoning {
  max-width: 90px;
}
.conversation-model-menu {
  position: absolute;
  bottom: calc(100% + 9px);
  left: 0;
  width: 310px;
  max-width: calc(100vw - 40px);
  background: var(--fn-bg-primary, #fff);
  color: var(--fn-text-primary, #172b4d);
  border: 1px solid var(--fn-border-primary, #e0e5ee);
  border-radius: 9px;
  box-shadow: 0 10px 35px #172b4d20;
  z-index: 70;
  overflow: hidden;
}
.conversation-model-search {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px;
  border: 1px solid var(--fn-border-primary, #e0e5ee);
  border-radius: 6px;
  padding: 7px 9px;
}
.conversation-model-search input {
  min-width: 0;
  width: 100%;
  border: 0;
  background: transparent;
  color: inherit;
  outline: 0;
  font-size: 12px;
}
.conversation-model-list {
  max-height: 270px;
  overflow: auto;
  padding: 0 6px 7px;
}
.model-group-label {
  font-size: 10px;
  color: var(--fn-text-secondary, #64748b);
  padding: 9px 8px 5px;
}
.conversation-model-option {
  width: 100%;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
  padding: 9px 8px;
  border: 0;
  background: transparent;
  border-radius: 5px;
  text-align: left;
}
.conversation-model-option:hover,
.conversation-model-option[aria-pressed='true'] {
  background: var(--fn-bg-active, #edf2ff);
}
.conversation-model-option span {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.conversation-model-option strong {
  font-size: 12px;
  font-weight: 500;
}
.conversation-model-option small {
  font-size: 10px;
  color: var(--fn-text-secondary, #64748b);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conversation-model-option svg {
  flex-shrink: 0;
  color: var(--fn-brand, #365cff);
}
.conversation-model-menu-actions {
  display: flex;
  border-top: 1px solid var(--fn-border-primary, #e0e5ee);
}
.conversation-model-menu-actions button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 0;
  padding: 10px;
  background: transparent;
  font-size: 12px;
}
.conversation-model-menu-actions button:hover {
  background: var(--fn-bg-secondary, #f3f5f9);
}
.control-note,
.conversation-running-config {
  font-size: 10px;
  color: var(--fn-text-secondary, #64748b);
}
.control-note {
  margin: 8px;
}
.conversation-running-config {
  line-height: 1.6;
  padding: 2px 6px;
}
.conversation-running-config span {
  margin-left: 8px;
}
.conversation-options-error {
  font-size: 11px;
  color: #b42318;
  padding: 4px 6px;
  line-height: 1.6;
}
.conversation-options-error button {
  border: 0;
  background: transparent;
  text-decoration: underline;
  color: inherit;
}
.profile-manager-mask {
  position: fixed;
  inset: 0;
  z-index: 2500;
  background: #14213d50;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 20px;
}
.profile-manager-dialog {
  position: relative;
  width: 680px;
  max-width: 100%;
  max-height: calc(100vh - 40px);
  overflow: auto;
  padding: 30px 24px 24px;
  border-radius: 12px;
  background: var(--fn-bg-primary, #fff);
  border: 1px solid var(--fn-border-primary, #e0e5ee);
}
.profile-manager-close {
  position: absolute;
  top: 6px;
  right: 7px;
  border: 0;
  background: transparent;
  padding: 5px;
  display: flex;
  color: var(--fn-text-secondary, #64748b);
}
button:disabled,
select:disabled {
  opacity: 0.5;
  cursor: default;
}
:is(button, select):focus-visible {
  outline: 2px solid var(--fn-brand, #365cff);
  outline-offset: 2px;
}
@media (max-width: 1150px) {
  .conversation-model-wrap {
    max-width: 165px;
  }
  .conversation-control-row {
    gap: 3px;
  }
}
@media (max-width: 600px) {
  .conversation-model-wrap {
    max-width: 145px;
  }
  .conversation-running-config span {
    display: block;
    margin-left: 0;
  }
}
</style>
