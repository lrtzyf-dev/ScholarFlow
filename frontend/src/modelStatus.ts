import type { ConversationOptions, ModelProfile, RuntimeConfiguration } from './api'

export interface ModelStatus {
  kind: 'api' | 'mock' | 'unknown'
  label: string
  title: string
}
export const unknownModelStatus = (): ModelStatus => ({
  kind: 'unknown',
  label: '配置待检查',
  title: '模型配置尚未确认；此标识不代表接口连通性测试结果',
})

export const unconfiguredModelStatus = (model = ''): ModelStatus => ({
  kind: 'unknown',
  label: '模型待配置',
  title: `${model || '所选模型'}尚未配置密钥；添加模型密钥或明确选择离线演示后继续`,
})

export function configuredModelStatus(
  mode: 'api' | 'mock',
  model = '',
  scope = '当前配置'
): ModelStatus {
  return mode === 'api'
    ? {
        kind: 'api',
        label: '使用接口模型',
        title: `${scope}${model ? `：${model}` : ''}；接口可用性以实际请求或连接测试结果为准`,
      }
    : { kind: 'mock', label: '离线演示', title: `${scope}使用本机演示响应` }
}

export function conversationModelStatus(input: {
  running: boolean
  runtime: RuntimeConfiguration | null
  ready: boolean
  options: ConversationOptions | null
  legacyCredentialError?: boolean
  legacyCredentialChecked?: boolean
}): ModelStatus {
  const { runtime, options } = input
  if (input.running) {
    if (
      runtime?.profile_id === 'legacy-default' &&
      (input.legacyCredentialError || input.legacyCredentialChecked === false)
    )
      return unknownModelStatus()
    if (runtime?.profile_id === options?.model_profile_id && options?.model_error)
      return unknownModelStatus()
    return runtime?.mode === 'api' || runtime?.mode === 'mock'
      ? configuredModelStatus(runtime.mode, runtime.model_id || '', '本轮绑定')
      : unknownModelStatus()
  }
  const model = options?.effective_model
  if (model?.enabled && model.mode === 'unconfigured')
    return unconfiguredModelStatus(model.model_id)
  if (
    !input.ready ||
    options?.model_error ||
    !model?.enabled ||
    (model.id === 'legacy-default' &&
      (input.legacyCredentialError || input.legacyCredentialChecked === false))
  )
    return unknownModelStatus()
  if (model.mode === 'mock' || model.id === 'offline-demo')
    return configuredModelStatus('mock', model.model_id, '当前会话')
  // Old server responses without a readiness flag must not imply demo mode.
  if (model.id === 'legacy-default' && !model.api_key_set && !model.mode)
    return unconfiguredModelStatus(model.model_id)
  try {
    if (!['http:', 'https:'].includes(new URL(model.base_url).protocol)) return unknownModelStatus()
  } catch {
    return unknownModelStatus()
  }
  return configuredModelStatus('api', model.model_id, '当前会话选择')
}

export function globalDefaultModelStatus(input: {
  profiles: ModelProfile[]
  default_profile_id: string | null
  legacyCredentialError?: boolean
}): ModelStatus {
  const id = input.default_profile_id || 'legacy-default'
  const status = conversationModelStatus({
    running: false,
    runtime: null,
    ready: true,
    options: {
      revision: 0,
      model_profile_id: id,
      effective_model: input.profiles.find((profile) => profile.id === id) || null,
      approval_mode: 'always',
      reasoning_effort: null,
    },
    legacyCredentialChecked: true,
    legacyCredentialError: input.legacyCredentialError,
  })
  return {
    ...status,
    title: status.title.replace('当前会话选择', '全局默认配置').replace('当前会话', '全局默认配置'),
  }
}
