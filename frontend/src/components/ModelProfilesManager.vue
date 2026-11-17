<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { Plus, Pencil, Power, Trash2 } from 'lucide-vue-next'
import ModelProfileEditor from './ModelProfileEditor.vue'
import ModelCapabilityPanel from './ModelCapabilityPanel.vue'
import {
  listModelProfiles,
  updateModelProfile,
  deleteModelProfile,
  setDefaultModelProfile,
  testModelProfile,
  type ModelProfile,
} from '../api'

const profiles = ref<ModelProfile[]>([])
const defaultId = ref<string | null>(null)
const loading = ref(true)
const busy = ref('')
const error = ref('')
const testMessage = ref('')
const testFailed = ref(false)
const editorOpen = ref(false)
const editing = ref<ModelProfile | null>(null)
const diagnosticId = ref('')
let disposed = false
let epoch = 0
function failureMessage(reason: unknown) {
  const failure = reason as {
    response?: { status?: number; data?: { detail?: unknown } }
    message?: string
  }
  if (failure.response?.status === 409) return '模型配置已在其他窗口更新，请刷新后重试'
  const detail = failure.response?.data?.detail
  return typeof detail === 'string' ? detail : failure.message || '操作失败，请重试'
}
async function load() {
  const request = ++epoch
  if (!profiles.value.length) loading.value = true
  try {
    const result = await listModelProfiles()
    if (disposed || request !== epoch) return
    profiles.value = result.profiles
    defaultId.value = result.default_profile_id
    error.value = ''
  } catch (reason) {
    if (!disposed && request === epoch) error.value = failureMessage(reason)
  } finally {
    if (!disposed && request === epoch) loading.value = false
  }
}
function edit(profile: ModelProfile | null = null) {
  editing.value = profile
  editorOpen.value = true
}
async function mutate(profile: ModelProfile, action: 'default' | 'toggle' | 'delete' | 'test') {
  if (busy.value) return
  busy.value = profile.id
  error.value = testMessage.value = ''
  try {
    if (action === 'default') await setDefaultModelProfile(profile.id)
    else if (action === 'toggle')
      await updateModelProfile(profile.id, {
        expected_version: profile.version,
        enabled: !profile.enabled,
      })
    else if (action === 'delete') await deleteModelProfile(profile.id, profile.version)
    else {
      const result = await testModelProfile(profile.id)
      testMessage.value = result.message
      testFailed.value = !result.ok
    }
    await load()
    if (action !== 'test') window.dispatchEvent(new Event('scholarflow:model-profiles-updated'))
  } catch (reason) {
    if (!disposed) error.value = failureMessage(reason)
  } finally {
    if (!disposed) busy.value = ''
  }
}
onMounted(() => {
  void load()
  window.addEventListener('scholarflow:model-profiles-updated', load)
})
onUnmounted(() => {
  disposed = true
  epoch++
  window.removeEventListener('scholarflow:model-profiles-updated', load)
})
</script>

<template>
  <section class="model-profiles-manager" aria-label="已添加模型">
    <header>
      <div>
        <h2>已添加模型</h2>
        <p>在这里管理模型，在会话输入框旁选择使用。默认模型用于未指定模型的新任务。</p>
      </div>
      <button type="button" class="profile-add" @click="edit()"><Plus :size="15" />添加模型</button>
    </header>
    <p v-if="loading" class="profile-note" role="status">正在载入模型…</p>
    <p v-if="error" class="profile-error" role="alert">
      {{ error }} <button type="button" @click="load">刷新列表</button>
    </p>
    <p
      v-if="testMessage"
      :class="testFailed ? 'profile-error' : 'profile-note'"
      :role="testFailed ? 'alert' : 'status'"
    >
      {{ testMessage }}
    </p>
    <div
      v-for="profile in profiles"
      :key="profile.id"
      class="profile-row"
      :class="{ disabled: !profile.enabled }"
      :data-profile-id="profile.id"
    >
      <div class="profile-row-info">
        <strong
          >{{ profile.name
          }}<span v-if="defaultId === profile.id" class="profile-default">默认</span
          ><span v-if="!profile.enabled" class="profile-default">已停用</span></strong
        ><span class="profile-model-id">{{ profile.model_id || '离线模型' }}</span
        ><span class="profile-address">{{ profile.base_url || '本机离线模式' }}</span
        ><small v-if="profile.mode !== 'mock'"
          >上下文 {{ (profile.context_window_tokens ?? 131072).toLocaleString() }} tokens · 最大输出
          {{ (profile.max_output_tokens ?? 8192).toLocaleString() }} tokens（{{
            profile.context_limits_source === 'configured'
              ? '配置值，未验证'
              : '兼容参考值，未验证'
          }}）</small
        ><small v-if="profile.id === 'legacy-default'"
          >沿用原模型设置；需自定义上下文和输出限制时，请添加独立模型档案。</small
        >
        <small v-if="profile.mode === 'unconfigured'">待配置密钥，尚不能发送模型请求。</small>
        <small v-else-if="profile.mode === 'mock'"
          >本机示例响应；主动选为默认或在会话中选择后使用。</small
        >
        <ModelCapabilityPanel
          v-if="diagnosticId === profile.id"
          :profile-id="profile.id"
          :profile-version="profile.version"
          @tested="load"
        />
      </div>
      <div class="profile-row-actions">
        <button
          v-if="defaultId !== profile.id"
          type="button"
          :disabled="Boolean(busy) || !profile.enabled"
          @click="mutate(profile, 'default')"
        >
          设为默认
        </button>
        <button
          v-if="profile.mode !== 'mock'"
          type="button"
          :disabled="Boolean(busy) || !profile.enabled"
          @click="mutate(profile, 'test')"
        >
          测试连接
        </button>
        <template v-if="profile.id !== 'legacy-default' && !profile.builtin">
          <button
            type="button"
            :disabled="Boolean(busy) || !profile.enabled || profile.mode === 'unconfigured'"
            :aria-expanded="diagnosticId === profile.id"
            @click="diagnosticId = diagnosticId === profile.id ? '' : profile.id"
          >
            能力诊断
          </button>
          <button
            type="button"
            :aria-label="`编辑 ${profile.name}`"
            :disabled="Boolean(busy)"
            @click="edit(profile)"
          >
            <Pencil :size="14" />
          </button>
          <button
            type="button"
            :aria-label="`${profile.enabled ? '停用' : '启用'} ${profile.name}`"
            :disabled="Boolean(busy)"
            @click="mutate(profile, 'toggle')"
          >
            <Power :size="14" />
          </button>
          <button
            type="button"
            :aria-label="`删除 ${profile.name}`"
            :disabled="Boolean(busy)"
            @click="mutate(profile, 'delete')"
          >
            <Trash2 :size="14" />
          </button>
        </template>
      </div>
    </div>
    <p v-if="!loading && !profiles.length && !error" class="profile-note">
      还没有模型配置。添加后即可在会话中选择。
    </p>
    <ModelProfileEditor v-model="editorOpen" :profile="editing" @saved="load" />
  </section>
</template>

<style scoped>
.model-profiles-manager {
  color: var(--fn-text-primary, #172b4d);
}
header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 16px;
}
h2 {
  margin: 0 0 7px;
  font-size: 18px;
}
header p,
.profile-note {
  color: var(--fn-text-secondary, #64748b);
  font-size: 12px;
  line-height: 1.6;
  margin: 0;
}
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  font: inherit;
  font-size: 12px;
  padding: 6px 9px;
  border: 1px solid var(--fn-border-primary, #dfe5ef);
  border-radius: 6px;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.profile-add {
  flex-shrink: 0;
  color: var(--fn-brand, #365cff);
}
.profile-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 0;
  border-top: 1px solid var(--fn-border-primary, #e5eaf2);
}
.profile-row-info {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.profile-row-info strong {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  font-size: 13px;
}
.profile-row-info small {
  font-size: 11px;
  color: var(--fn-text-secondary, #64748b);
}
.profile-model-id,
.profile-address {
  font-size: 12px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.profile-address {
  font-size: 11px;
  color: var(--fn-text-secondary, #64748b);
}
.profile-default {
  font-size: 10px;
  font-weight: 400;
  border: 1px solid var(--fn-border-primary, #dfe5ef);
  border-radius: 4px;
  padding: 1px 5px;
  color: var(--fn-text-secondary, #64748b);
}
.profile-row-actions {
  display: flex;
  flex-wrap: wrap;
  align-content: flex-start;
  justify-content: flex-end;
  gap: 5px;
  max-width: 270px;
  flex-shrink: 0;
}
.profile-error {
  font-size: 12px;
  color: #b42318;
  line-height: 1.6;
}
.disabled .profile-row-info {
  opacity: 0.55;
}
button:disabled {
  opacity: 0.45;
  cursor: default;
}
button:focus-visible {
  outline: 2px solid var(--fn-brand, #365cff);
  outline-offset: 2px;
}
@media (max-width: 600px) {
  .profile-row {
    flex-direction: column;
  }
  .profile-row-actions {
    justify-content: flex-start;
    max-width: none;
  }
}
</style>
