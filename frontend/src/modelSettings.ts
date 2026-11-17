export interface ModelConnection {
  llm_base_url: string
  llm_api_key: string
  llm_model: string
  reasoning_effort: string
}

export interface ModelPreset {
  name: string
  base: string
  model: string
}

export const MODEL_PRESETS: ModelPreset[] = [
  {
    name: '阿里百炼（Qwen）',
    base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-flash',
  },
  {
    name: '硅基流动',
    base: 'https://api.siliconflow.cn/v1',
    model: 'Qwen/Qwen2.5-7B-Instruct',
  },
  { name: 'DeepSeek', base: 'https://api.deepseek.com', model: 'deepseek-flash' },
]

export function normalizeSingleLine(value: string, label: string): string {
  const trimmed = value.trim()
  // Match the backend's single-line field validation, including NUL and DEL.
  if (/[\x00-\x1f\x7f]/.test(trimmed)) throw new Error(`${label}不能包含换行或控制字符。`)
  return trimmed
}

/** Keep path spelling; remove only an exact, standard endpoint suffix when pasted. */
export function normalizeBaseUrl(value: string, endpoint = 'chat/completions'): string {
  const trimmed = normalizeSingleLine(value, '接口地址')
  if (!trimmed) return ''
  const parts = /^(https?):\/\/([^/?#]+)([^?#]*)$/i.exec(trimmed)
  if (!parts || /\s|\\/.test(trimmed)) {
    throw new Error('接口地址需为完整的 http:// 或 https:// Base URL，不含空格、查询参数或 #。')
  }
  if (/%(?:0[0-9a-f]|1[0-9a-f]|7f)/i.test(trimmed)) {
    throw new Error('接口地址不能包含编码后的控制字符。')
  }
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    throw new Error('接口地址格式无效，请核对服务提供方的 Base URL。')
  }
  if (parts[2].includes('@') || parsed.username || parsed.password) {
    throw new Error('请在 API Key 栏填写密钥，不要将用户名或密码放入接口地址。')
  }
  let path = parts[3].replace(/\/+$/, '')
  const suffix = `/${endpoint}`
  if (path.endsWith(suffix)) path = path.slice(0, -suffix.length).replace(/\/+$/, '')
  else if (path.toLowerCase().endsWith(suffix.toLowerCase())) {
    throw new Error(`接口路径区分大小写，标准 ${suffix} 尾缀应保持小写；请核对服务商的 Base URL。`)
  }
  if (endpoint === 'chat/completions' && /\/responses$/i.test(path)) {
    throw new Error('请填写 Chat Completions 服务的 Base URL；当前不支持 Responses 接口。')
  }
  return `${parsed.protocol}//${parsed.host}${path}`
}

export function validateAgentSteps(value: unknown): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > 128) {
    throw new Error('单次任务最多执行轮数必须是 1–128 之间的整数。')
  }
  return value
}

export function normalizeModelConnection(input: ModelConnection): ModelConnection {
  return {
    llm_base_url: normalizeBaseUrl(input.llm_base_url),
    llm_api_key: normalizeSingleLine(input.llm_api_key, 'API Key'),
    llm_model: normalizeSingleLine(input.llm_model, '模型名称'),
    reasoning_effort: normalizeSingleLine(input.reasoning_effort, '推理强度').toLowerCase(),
  }
}

export function connectionTestPayload(connection: ModelConnection, clearSavedKey = false) {
  const { llm_api_key, ...payload } = connection
  // Omission means reuse the saved credential. An explicit empty string means no key.
  return clearSavedKey
    ? { ...payload, llm_api_key: '' }
    : llm_api_key
      ? { ...payload, llm_api_key }
      : payload
}

/** Model discovery needs an endpoint and credential, before a model has been chosen. */
export function modelDiscoveryPayload(
  input: Pick<ModelConnection, 'llm_base_url' | 'llm_api_key'>,
  saved: { llm_base_url: string; llm_api_key_set?: boolean } | null,
  clearSavedKey = false
): { llm_base_url: string; llm_api_key?: string } {
  const connection = {
    llm_base_url: normalizeBaseUrl(input.llm_base_url),
    llm_api_key: normalizeSingleLine(input.llm_api_key, 'API Key'),
  }
  if (!connection.llm_base_url) throw new Error('请先填写模型接口的 Base URL。')
  if (needsNewApiKey(saved, connection, clearSavedKey)) {
    throw new Error('服务地址已更换，请重新填写该服务的 API Key 后获取模型列表。')
  }
  return clearSavedKey
    ? { llm_base_url: connection.llm_base_url, llm_api_key: '' }
    : connection.llm_api_key
      ? connection
      : { llm_base_url: connection.llm_base_url }
}

export function findModelPreset(value: string): string {
  try {
    const base = normalizeBaseUrl(value)
    return (
      MODEL_PRESETS.find((preset) => normalizeBaseUrl(preset.base) === base)?.name || 'compatible'
    )
  } catch {
    return 'compatible'
  }
}

export function needsNewApiKey(
  saved: { llm_base_url: string; llm_api_key_set?: boolean } | null,
  next: Pick<ModelConnection, 'llm_base_url' | 'llm_api_key'>,
  clearSavedKey = false
): boolean {
  if (clearSavedKey || !saved?.llm_api_key_set || next.llm_api_key.trim()) return false
  try {
    return (
      new URL(normalizeBaseUrl(saved.llm_base_url)).origin !==
      new URL(normalizeBaseUrl(next.llm_base_url)).origin
    )
  } catch {
    return true
  }
}

export function validateModelConnection(
  connection: ModelConnection,
  saved: { llm_base_url: string; llm_api_key_set?: boolean } | null,
  clearSavedKey = false
): string {
  if (!connection.llm_base_url) return '请先填写模型接口的 Base URL。'
  if (!connection.llm_model) return '请先填写服务提供方的模型 ID。'
  if (/\/responses\/?$/i.test(connection.llm_base_url)) {
    return '请填写 Chat Completions 服务的 Base URL；当前不支持 Responses 接口。'
  }
  if (/\/chat\/completions\/?$/i.test(connection.llm_base_url)) {
    return '接口路径区分大小写，请核对服务商的 Base URL；标准 /chat/completions 尾缀会自动移除。'
  }
  if (needsNewApiKey(saved, connection, clearSavedKey)) {
    return '接口地址已更换到其他服务地址，请重新填写该服务的 API Key 后再测试或保存。已保存的密钥不会自动用于新地址。'
  }
  return ''
}

export function needsNewEmbeddingApiKey(
  saved: {
    llm_base_url: string
    embedding_base_url: string
    embedding_api_key_set?: boolean
  } | null,
  next: { llm_base_url: string; embedding_base_url: string; embedding_api_key: string },
  clearSavedKey = false
): boolean {
  if (!saved) return false
  return needsNewApiKey(
    {
      llm_base_url: saved.embedding_base_url || saved.llm_base_url,
      llm_api_key_set: saved.embedding_api_key_set,
    },
    {
      llm_base_url: next.embedding_base_url.trim() || next.llm_base_url,
      llm_api_key: next.embedding_api_key,
    },
    clearSavedKey
  )
}
