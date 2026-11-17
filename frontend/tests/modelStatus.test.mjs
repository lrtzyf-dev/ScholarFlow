import assert from 'node:assert/strict'
import test from 'node:test'
import { conversationModelStatus, globalDefaultModelStatus } from '../src/modelStatus.ts'

const model = {
  id: 'named-api',
  model_id: 'CaseSensitive-Model',
  base_url: 'https://example.test/v1',
  enabled: true,
  api_key_set: true,
}
const options = { model_profile_id: model.id, effective_model: model }

test('named model status uses the selected conversation profile independently of legacy credentials', () => {
  const status = conversationModelStatus({
    running: false,
    runtime: null,
    ready: true,
    options,
    legacyCredentialError: true,
  })
  assert.equal(status.label, '使用接口模型')
  assert.match(status.title, /CaseSensitive-Model/)
  assert.doesNotMatch(status.title, /已连通|已就绪|在线/)
})

test('running status follows the frozen model while a different next-turn model is selected', () => {
  const status = conversationModelStatus({
    running: true,
    ready: true,
    options,
    runtime: { profile_id: 'old-frozen', model_id: 'Frozen-Case', mode: 'api' },
  })
  assert.match(status.title, /本轮绑定：Frozen-Case/)
  assert.doesNotMatch(status.title, /CaseSensitive-Model/)
})

test('unknown or invalid configuration and undecryptable legacy credentials never appear connected or mock', () => {
  for (const input of [
    { running: false, runtime: null, ready: false, options: null },
    { running: true, runtime: null, ready: true, options },
    {
      running: false,
      runtime: null,
      ready: true,
      options: { ...options, model_error: 'disabled' },
    },
    {
      running: false,
      runtime: null,
      ready: true,
      legacyCredentialError: true,
      options: {
        model_profile_id: 'legacy-default',
        effective_model: { ...model, id: 'legacy-default' },
      },
    },
  ])
    assert.equal(conversationModelStatus(input).label, '配置待检查')
})

test('only an explicit mock binding or selected demo profile displays offline demo', () => {
  assert.equal(
    conversationModelStatus({
      running: true,
      runtime: { mode: 'mock' },
      ready: false,
      options: null,
    }).label,
    '离线演示'
  )
  assert.equal(
    conversationModelStatus({
      running: false,
      runtime: null,
      ready: true,
      options: {
        model_profile_id: 'legacy-default',
        effective_model: { ...model, id: 'legacy-default', api_key_set: false },
      },
    }).label,
    '模型待配置'
  )
})

test('explicit demo and keyless local API profiles retain separate status', () => {
  for (const [profile, label] of [
    [{ ...model, id: 'offline-demo', mode: 'mock', api_key_set: false }, '离线演示'],
    [
      {
        ...model,
        id: 'legacy-default',
        mode: 'api',
        base_url: 'http://127.0.0.1:11434/v1',
        api_key_set: false,
      },
      '使用接口模型',
    ],
    [{ ...model, mode: 'unconfigured', api_key_set: false }, '模型待配置'],
  ]) {
    const status = conversationModelStatus({
      running: false,
      runtime: null,
      ready: true,
      options: { model_profile_id: profile.id, effective_model: profile },
    })
    assert.equal(status.label, label)
  }
})

test('non-chat pages show the named global default instead of unrelated legacy demo status', () => {
  const status = globalDefaultModelStatus({
    profiles: [model],
    default_profile_id: model.id,
    legacyCredentialError: true,
  })
  assert.equal(status.kind, 'api')
  assert.match(status.title, /全局默认配置.*CaseSensitive-Model/)
  assert.equal(
    globalDefaultModelStatus({ profiles: [model], default_profile_id: 'missing' }).kind,
    'unknown'
  )
})
