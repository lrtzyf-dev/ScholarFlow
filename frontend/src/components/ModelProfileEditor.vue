<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import {
  createModelProfile,
  updateModelProfile,
  discoverNewProfileModels,
  discoverProfileModels,
  type ModelProfile,
  type ProviderModel,
} from '../api'
import { normalizeBaseUrl, normalizeSingleLine } from '../modelSettings'

const props = defineProps<{
  modelValue: boolean
  profile?: ModelProfile | null
  initialValues?: { name: string; base_url: string; model_id: string }
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: [profile: ModelProfile]
}>()
const form = reactive({
  name: '',
  base_url: '',
  model_id: '',
  api_key: '',
  reasoning_effort: '',
  context_window_tokens: '131072',
  max_output_tokens: '8192',
})
const error = ref('')
const busy = ref(false)
const discovering = ref(false)
const clearKey = ref(false)
const models = ref<ProviderModel[]>([])
const discoveryMessage = ref('')
const dialog = ref<HTMLElement | null>(null)
let epoch = 0
let discoveryEpoch = 0
let returnFocus: HTMLElement | null = null
const changedOrigin = computed(() => {
  try {
    return Boolean(
      props.profile && new URL(form.base_url).origin !== new URL(props.profile.base_url).origin
    )
  } catch {
    return false
  }
})
watch(
  () => props.modelValue,
  async (open) => {
    epoch++
    error.value = ''
    models.value = []
    discoveryMessage.value = ''
    busy.value = false
    discovering.value = false
    clearKey.value = false
    if (!open) {
      form.api_key = ''
      returnFocus?.focus()
      return
    }
    returnFocus = document.activeElement as HTMLElement | null
    Object.assign(form, {
      name: props.profile?.name || props.initialValues?.name || '',
      base_url: props.profile?.base_url || props.initialValues?.base_url || '',
      model_id: props.profile?.model_id || props.initialValues?.model_id || '',
      api_key: '',
      reasoning_effort: props.profile?.reasoning_effort || '',
      context_window_tokens: String(props.profile?.context_window_tokens ?? 131072),
      max_output_tokens: String(props.profile?.max_output_tokens ?? 8192),
    })
    await nextTick()
    dialog.value?.querySelector<HTMLInputElement>('input')?.focus()
  }
)
watch(
  () => [form.base_url, form.api_key, clearKey.value],
  () => {
    discoveryEpoch++
    models.value = []
    discoveryMessage.value = ''
    discovering.value = false
  }
)
function message(reason: unknown) {
  const failure = reason as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = failure.response?.data?.detail
  return typeof detail === 'string' ? detail : failure.message || '保存失败，请重试'
}
function close() {
  if (!busy.value) emit('update:modelValue', false)
}
function keys(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation()
    close()
  }
  if (event.key !== 'Tab') return
  const elements = [
    ...(dialog.value?.querySelectorAll<HTMLElement>(
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
async function discover() {
  if (discovering.value || busy.value) return
  const request = epoch
  const discovery = ++discoveryEpoch
  discovering.value = true
  error.value = discoveryMessage.value = ''
  models.value = []
  try {
    const base = normalizeBaseUrl(form.base_url)
    if (!base) throw new Error('请填写接口地址')
    const stored =
      props.profile && base === props.profile.base_url && !form.api_key.trim() && !clearKey.value
    const result = stored
      ? await discoverProfileModels(props.profile!.id)
      : await discoverNewProfileModels({ base_url: base, api_key: form.api_key.trim() })
    if (request !== epoch || discovery !== discoveryEpoch) return
    models.value = result.ok ? result.models : []
    discoveryMessage.value = result.message
  } catch (reason) {
    if (request === epoch && discovery === discoveryEpoch) error.value = message(reason)
  } finally {
    if (request === epoch && discovery === discoveryEpoch) discovering.value = false
  }
}
async function save() {
  if (busy.value) return
  const request = epoch
  error.value = ''
  try {
    const name = normalizeSingleLine(form.name, '配置名称')
    const base_url = normalizeBaseUrl(form.base_url)
    const model_id = normalizeSingleLine(form.model_id, '模型名称')
    if (!name || !base_url || !model_id) throw new Error('请填写配置名称、接口地址和模型 ID')
    const context_window_tokens = Number(form.context_window_tokens)
    const max_output_tokens = Number(form.max_output_tokens)
    if (
      !/^\d+$/.test(form.context_window_tokens) ||
      !Number.isSafeInteger(context_window_tokens) ||
      context_window_tokens < 1 ||
      context_window_tokens > 16777216
    )
      throw new Error('上下文窗口须为1至16777216的整数 token 数')
    if (
      !/^\d+$/.test(form.max_output_tokens) ||
      !Number.isSafeInteger(max_output_tokens) ||
      max_output_tokens < 1 ||
      max_output_tokens > 1048576
    )
      throw new Error('最大输出须为1至1048576的整数 token 数')
    if (max_output_tokens >= context_window_tokens)
      throw new Error('最大输出 token 数必须小于上下文窗口，需为输入保留空间')
    if (
      props.profile?.api_key_set &&
      changedOrigin.value &&
      !form.api_key.trim() &&
      !clearKey.value
    )
      throw new Error('接口来源已变更，请填写新来源密钥，或选择清除密钥使用无密钥接口')
    const input = {
      name,
      base_url,
      model_id,
      reasoning_effort: form.reasoning_effort,
      context_window_tokens,
      max_output_tokens,
      ...(!props.profile || form.api_key || clearKey.value
        ? { api_key: clearKey.value ? '' : normalizeSingleLine(form.api_key, 'API Key') }
        : {}),
    }
    busy.value = true
    const saved = props.profile
      ? await updateModelProfile(props.profile.id, {
          ...input,
          expected_version: props.profile.version,
        })
      : await createModelProfile(input)
    if (request !== epoch) return
    form.api_key = ''
    emit('saved', saved)
    emit('update:modelValue', false)
    window.dispatchEvent(new Event('scholarflow:model-profiles-updated'))
  } catch (reason) {
    if (request === epoch) error.value = message(reason)
  } finally {
    if (request === epoch) busy.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="profile-editor-mask" @mousedown.self="close">
      <section
        ref="dialog"
        class="profile-editor"
        role="dialog"
        aria-modal="true"
        aria-labelledby="profile-editor-title"
        @keydown="keys"
      >
        <header>
          <h2 id="profile-editor-title">{{ profile ? '编辑模型' : '添加模型' }}</h2>
          <button type="button" aria-label="关闭模型表单" :disabled="busy" @click="close">
            <X :size="18" />
          </button>
        </header>
        <form @submit.prevent="save">
          <label
            >配置名称<input
              v-model="form.name"
              autocomplete="off"
              maxlength="120"
              placeholder="便于区分的名称"
              :disabled="busy"
          /></label>
          <label
            >接口地址<input
              v-model="form.base_url"
              autocomplete="off"
              placeholder="https://api.example.com/v1"
              :disabled="busy"
          /></label>
          <label
            >API Key<input
              v-model="form.api_key"
              type="password"
              autocomplete="new-password"
              :placeholder="
                profile?.api_key_set ? '已保存，留空保持原密钥' : '填写密钥；无密钥接口可留空'
              "
              :disabled="busy || clearKey"
          /></label>
          <label v-if="profile?.api_key_set" class="profile-clear-key"
            ><input
              v-model="clearKey"
              type="checkbox"
              :disabled="busy"
            />清除已保存密钥，使用无密钥接口</label
          >
          <div class="profile-model-label">
            <label for="profile-model-id">模型 ID</label
            ><button
              type="button"
              :disabled="busy || discovering || !form.base_url.trim()"
              @click="discover"
            >
              {{ discovering ? '正在获取…' : '获取模型列表' }}
            </button>
          </div>
          <select
            v-if="models.length"
            aria-label="从接口模型列表选择"
            :value="models.some((m) => m.id === form.model_id) ? form.model_id : ''"
            @change="form.model_id = ($event.target as HTMLSelectElement).value"
          >
            <option disabled value="">选择模型，或在下方手动填写</option>
            <option v-for="model in models" :key="model.id" :value="model.id">
              {{ model.id }}
            </option>
          </select>
          <input
            id="profile-model-id"
            v-model="form.model_id"
            autocomplete="off"
            placeholder="服务商返回的原始模型 ID"
            :disabled="busy"
          />
          <p v-if="discoveryMessage" class="profile-hint" role="status">{{ discoveryMessage }}</p>
          <label
            >默认推理强度<select v-model="form.reasoning_effort" :disabled="busy">
              <option value="">模型默认（不传参数）</option>
              <option
                v-for="value in ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']"
                :key="value"
                :value="value"
              >
                {{ value }}
              </option>
            </select></label
          >
          <p class="profile-hint">
            模型 ID 保留大小写。推理参数需模型支持；获取列表不代表所有模型都支持 Agent 工具。
          </p>
          <div class="profile-token-limits">
            <label
              >上下文窗口（tokens）<input
                v-model="form.context_window_tokens"
                inputmode="numeric"
                autocomplete="off"
                :disabled="busy"
                aria-describedby="profile-token-limits-hint"
            /></label>
            <label
              >最大输出（tokens）<input
                v-model="form.max_output_tokens"
                inputmode="numeric"
                autocomplete="off"
                :disabled="busy"
                aria-describedby="profile-token-limits-hint"
            /></label>
          </div>
          <p id="profile-token-limits-hint" class="profile-hint">
            请按供应商文档填写实际限制；初始数值是兼容参考值，尚未验证。获取模型列表和能力诊断不会发现窗口。
            输出须小于窗口以保留输入空间；修改对新任务生效，本轮任务沿用已保存配置。
          </p>
          <p v-if="error" class="profile-error" role="alert">{{ error }}</p>
          <footer>
            <button type="button" :disabled="busy" @click="close">取消</button
            ><button type="submit" class="profile-primary" :disabled="busy">
              {{ busy ? '正在保存…' : '保存模型' }}
            </button>
          </footer>
        </form>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.profile-editor-mask {
  position: fixed;
  inset: 0;
  z-index: 3000;
  background: #14213d50;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.profile-editor {
  width: 480px;
  max-width: 100%;
  max-height: calc(100vh - 40px);
  overflow: auto;
  background: var(--fn-bg-primary, #fff);
  color: var(--fn-text-primary, #1d2b42);
  border: 1px solid var(--fn-border-primary, #dfe5ef);
  border-radius: 12px;
  box-shadow: 0 18px 65px #14213d26;
  padding: 22px;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}
h2 {
  font-size: 18px;
  margin: 0;
}
button,
input,
select {
  font: inherit;
}
button {
  border: 1px solid var(--fn-border-primary, #dfe5ef);
  color: inherit;
  background: transparent;
  border-radius: 6px;
  padding: 7px 10px;
  cursor: pointer;
}
header button {
  display: flex;
  border: 0;
  padding: 5px;
}
form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}
input,
select {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 9px 10px;
  border: 1px solid var(--fn-border-primary, #dfe5ef);
  border-radius: 6px;
  background: var(--fn-bg-primary, #fff);
  color: inherit;
  font-size: 13px;
}
.profile-model-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.profile-model-label button {
  font-size: 12px;
  padding: 4px 8px;
}
.profile-hint {
  font-size: 11px;
  color: var(--fn-text-secondary, #64748b);
  line-height: 1.6;
  margin: 0;
}
.profile-error {
  font-size: 12px;
  color: #b42318;
  margin: 0;
  line-height: 1.6;
}
footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 6px;
}
.profile-primary {
  color: #fff;
  background: var(--fn-brand, #365cff);
  border-color: var(--fn-brand, #365cff);
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
:is(button, input, select):focus-visible {
  outline: 2px solid var(--fn-brand, #365cff);
  outline-offset: 2px;
}
.profile-clear-key {
  flex-direction: row;
  align-items: center;
  line-height: 1.5;
}
.profile-clear-key input {
  width: auto;
  flex-shrink: 0;
  padding: 0;
}
.profile-token-limits {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
@media (max-width: 420px) {
  .profile-token-limits {
    grid-template-columns: 1fr;
  }
}
</style>
