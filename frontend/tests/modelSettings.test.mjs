import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  connectionTestPayload,
  MODEL_PRESETS,
  findModelPreset,
  modelDiscoveryPayload,
  needsNewApiKey,
  needsNewEmbeddingApiKey,
  normalizeBaseUrl,
  normalizeModelConnection,
  validateModelConnection,
  validateAgentSteps,
} from '../src/modelSettings.ts'

test('task round budget accepts integers and rejects empty, fractional and out-of-range input', () => {
  for (const value of [1, 6, 24, 128]) assert.equal(validateAgentSteps(value), value)
  for (const value of ['', '24', null, true, 0, -1, 129, 1.5, NaN, Infinity]) {
    assert.throws(() => validateAgentSteps(value), /1–128/)
  }
})

test('normalizes scheme and host while retaining path and encoded character case', () => {
  assert.equal(
    normalizeBaseUrl('  HTTPS://API.Example.COM:443/Proxy/V1/%2FToken///  '),
    'https://api.example.com/Proxy/V1/%2FToken'
  )
  assert.equal(normalizeBaseUrl(' HTTP://LOCALHOST:11434/v1/ '), 'http://localhost:11434/v1')
  assert.equal(normalizeBaseUrl(' https://[::1]:8080/V1/ '), 'https://[::1]:8080/V1')
})

test('model IDs and API keys are trimmed without losing case', () => {
  assert.deepEqual(
    normalizeModelConnection({
      llm_base_url: ' HTTPS://API.Example.COM/V1/ ',
      llm_api_key: '  sk-AbCdEf  ',
      llm_model: '  Qwen/Qwen2.5-7B-Instruct  ',
      reasoning_effort: ' HIGH ',
    }),
    {
      llm_base_url: 'https://api.example.com/V1',
      llm_api_key: 'sk-AbCdEf',
      llm_model: 'Qwen/Qwen2.5-7B-Instruct',
      reasoning_effort: 'high',
    }
  )
})

test('preset recognition tolerates URL casing and slash but preserves path sensitivity', () => {
  assert.equal(findModelPreset(' HTTPS://API.SILICONFLOW.CN/v1/ '), '硅基流动')
  assert.equal(findModelPreset('https://api.siliconflow.cn/V1'), 'compatible')
  assert.equal(findModelPreset('https://api.deepseek.com/'), 'DeepSeek')
  assert.equal(findModelPreset('not a url'), 'compatible')
})

test('never silently reuses a saved key for another origin', () => {
  const saved = { llm_base_url: 'https://api.example.com/v1', llm_api_key_set: true }
  const next = (url, key = '') => ({ llm_base_url: url, llm_api_key: key })
  assert.equal(needsNewApiKey(saved, next('HTTPS://API.EXAMPLE.COM:443/OtherPath/')), false)
  assert.equal(needsNewApiKey(saved, next('https://other.example.com/v1')), true)
  assert.equal(needsNewApiKey(saved, next('http://api.example.com/v1')), true)
  assert.equal(needsNewApiKey(saved, next('https://api.example.com:8443/v1')), true)
  assert.equal(needsNewApiKey(saved, next('https://other.example.com/v1', '   ')), true)
  assert.equal(needsNewApiKey(saved, next('https://other.example.com/v1', 'sk-NewKey')), false)
  assert.equal(
    needsNewApiKey({ ...saved, llm_api_key_set: false }, next('http://localhost:8000')),
    false
  )
})

test('rejects ambiguous URLs and embedded credentials', () => {
  for (const url of [
    'example.com/v1',
    'file:///tmp/key',
    'https://example.com/v1?key=secret',
    'https://example.com/v1#anchor',
    'https://user:secret@example.com/v1',
    'https://@example.com/v1',
    'https://example.com/my path',
    'https://example.com\\other/v1',
    'https://example.com/%0d%0Asecret',
    'https://example.com/%7fsecret',
  ]) {
    assert.throws(() => normalizeBaseUrl(url))
  }
})

test('strips only the exact standard endpoint suffix and preserves all other path case', () => {
  assert.equal(
    normalizeBaseUrl(' HTTPS://API.Example.COM/Proxy/V1/chat/completions/ '),
    'https://api.example.com/Proxy/V1'
  )
  assert.equal(
    normalizeBaseUrl('https://example.com/Proxy/V1/embeddings/', 'embeddings'),
    'https://example.com/Proxy/V1'
  )
  assert.throws(() => normalizeBaseUrl('https://example.com/Proxy/V1/CHAT/Completions'), /大小写/)
  assert.throws(
    () => normalizeBaseUrl('https://example.com/Proxy/V1/EMBEDDINGS', 'embeddings'),
    /大小写/
  )
  assert.throws(() => normalizeBaseUrl('https://example.com/Proxy/V1/RESPONSES/'), /Responses/)
  assert.equal(findModelPreset('HTTPS://API.SILICONFLOW.CN/v1/chat/completions/'), '硅基流动')
  for (const endpoint of ['CHAT/Completions', 'responses']) {
    assert.match(
      validateModelConnection(
        {
          llm_base_url: `https://example.com/v1/${endpoint}`,
          llm_model: 'Model',
          llm_api_key: '',
          reasoning_effort: '',
        },
        null
      ),
      /Base URL/
    )
  }
})

test('omits a blank test key so the server reuses the saved credential', () => {
  const connection = normalizeModelConnection({
    llm_base_url: 'HTTPS://API.EXAMPLE.COM/v1/chat/completions',
    llm_api_key: '   ',
    llm_model: ' Model-ID ',
    reasoning_effort: ' HIGH ',
  })
  assert.deepEqual(connectionTestPayload(connection), {
    llm_base_url: 'https://api.example.com/v1',
    llm_model: 'Model-ID',
    reasoning_effort: 'high',
  })
  assert.deepEqual(connectionTestPayload({ ...connection, llm_api_key: 'sk-NewKEY' }), {
    llm_base_url: 'https://api.example.com/v1',
    llm_model: 'Model-ID',
    reasoning_effort: 'high',
    llm_api_key: 'sk-NewKEY',
  })
})

test('rejects internal CR/LF and control characters in connection fields', () => {
  const connection = {
    llm_base_url: 'https://example.com/v1',
    llm_api_key: 'sk-Key',
    llm_model: 'Model-ID',
    reasoning_effort: 'high',
  }
  for (const field of Object.keys(connection)) {
    for (const control of ['\r', '\n', '\u0000', '\u007f', '\t']) {
      assert.throws(
        () => normalizeModelConnection({ ...connection, [field]: `prefix${control}suffix` }),
        /控制字符/
      )
    }
  }
})

test('protects saved embedding keys when explicit or inherited endpoint origins change', () => {
  const saved = {
    llm_base_url: 'https://old.example.com/v1',
    embedding_base_url: '',
    embedding_api_key_set: true,
  }
  const next = { ...saved, llm_base_url: 'https://new.example.com/v1', embedding_api_key: '' }
  assert.equal(needsNewEmbeddingApiKey(saved, next), true)
  assert.equal(
    needsNewEmbeddingApiKey(saved, { ...next, embedding_base_url: 'https://old.example.com/v2' }),
    false
  )
  assert.equal(
    needsNewEmbeddingApiKey(saved, { ...next, embedding_api_key: 'sk-EmbeddingKey' }),
    false
  )
  assert.equal(needsNewEmbeddingApiKey({ ...saved, embedding_api_key_set: false }, next), false)
})

test('allows explicitly clearing saved keys for a keyless local model', () => {
  const saved = { llm_base_url: 'https://api.example.com/v1', llm_api_key_set: true }
  const connection = normalizeModelConnection({
    llm_base_url: ' HTTP://LOCALHOST:11434/v1/ ',
    llm_model: ' Local/Model-ID ',
    llm_api_key: '',
    reasoning_effort: '',
  })
  assert.equal(needsNewApiKey(saved, connection, true), false)
  assert.equal(validateModelConnection(connection, saved, true), '')
  assert.deepEqual(connectionTestPayload(connection, true), {
    ...connection,
    llm_api_key: '',
  })
  assert.equal(
    needsNewEmbeddingApiKey(
      { ...saved, embedding_base_url: '', embedding_api_key_set: true },
      { ...connection, embedding_base_url: '', embedding_api_key: '' },
      true
    ),
    false
  )
})

test('discovers models before choosing a model and retains endpoint and credential case', () => {
  assert.deepEqual(
    modelDiscoveryPayload(
      {
        llm_base_url: ' HTTPS://API.Example.COM/Proxy/V1/chat/completions/ ',
        llm_api_key: ' sk-AbCdEf ',
        llm_model: '',
        reasoning_effort: '',
      },
      null
    ),
    { llm_base_url: 'https://api.example.com/Proxy/V1', llm_api_key: 'sk-AbCdEf' }
  )
  assert.equal(MODEL_PRESETS.find((preset) => preset.name === 'DeepSeek').model, 'deepseek-flash')
})

test('model discovery reuses only same-origin saved credentials and supports explicit key removal', () => {
  const saved = { llm_base_url: 'https://api.example.com/v1', llm_api_key_set: true }
  assert.deepEqual(
    modelDiscoveryPayload(
      { llm_base_url: 'HTTPS://API.EXAMPLE.COM:443/OtherPath/', llm_api_key: ' ' },
      saved
    ),
    { llm_base_url: 'https://api.example.com/OtherPath' }
  )
  const changed = { llm_base_url: 'http://localhost:11434/v1', llm_api_key: '' }
  assert.throws(() => modelDiscoveryPayload(changed, saved), /重新填写/)
  assert.deepEqual(modelDiscoveryPayload(changed, saved, true), {
    llm_base_url: 'http://localhost:11434/v1',
    llm_api_key: '',
  })
  assert.deepEqual(modelDiscoveryPayload({ ...changed, llm_api_key: 'sk-NewKEY' }, saved), {
    llm_base_url: 'http://localhost:11434/v1',
    llm_api_key: 'sk-NewKEY',
  })
})

test('model discovery rejects missing endpoints and unsafe address or credential formats', () => {
  assert.throws(
    () => modelDiscoveryPayload({ llm_base_url: ' ', llm_api_key: '' }, null),
    /Base URL/
  )
  assert.throws(
    () =>
      modelDiscoveryPayload(
        { llm_base_url: 'https://user:secret@example.com', llm_api_key: '' },
        null
      ),
    /API Key/
  )
  assert.throws(
    () =>
      modelDiscoveryPayload(
        { llm_base_url: 'https://example.com', llm_api_key: 'key\nother' },
        null
      ),
    /控制字符/
  )
})
