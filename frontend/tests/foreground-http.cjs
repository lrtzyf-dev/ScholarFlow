// Real HTTP lifecycle checks, restricted to the isolated offline preview.
const assert = require('node:assert/strict')
const { randomUUID } = require('node:crypto')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'

async function main() {
  const health = await (await fetch(`${baseURL}/api/health`)).json()
  assert.equal(health.llm_mode, 'mock', 'Requires an isolated offline backend')
  const post = (path, body) => fetch(`${baseURL}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(12000),
  })
  const create = async () => (await (await post('/api/conversations', { title: '[Verification] Foreground lifecycle' })).json()).id
  const open = async (cid, runId) => {
    const response = await post('/api/chat/stream', { question: 'Offline cancellation verification', conversation_id: cid, run_id: runId })
    assert.equal(response.status, 200)
    assert.equal(response.headers.get('x-run-id'), runId)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    return {
      reader,
      async nextEvent() {
        while (true) {
          const boundary = buffer.indexOf('\n\n')
          if (boundary >= 0) {
            const frame = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            const line = frame.split('\n').find((s) => s.startsWith('data: '))
            if (line) return JSON.parse(line.slice(6))
          } else {
            const chunk = await reader.read()
            if (chunk.done) return null
            buffer += decoder.decode(chunk.value, { stream: true })
          }
        }
      },
    }
  }

  const cid = await create()
  const runId = `chat_${randomUUID()}`
  const stream = await open(cid, runId)
  let event
  do { event = await stream.nextEvent(); assert.ok(event) } while (!event.kind.endsWith('_delta'))
  assert.equal((await fetch(`${baseURL}/api/conversations/${cid}`, { method: 'DELETE' })).status, 409)
  assert.equal((await post(`/api/chat/runs/${runId}/cancel`)).status, 200)
  const kinds = []
  while ((event = await stream.nextEvent())) kinds.push(event.kind)
  assert.ok(kinds.includes('cancelled'))
  assert.ok(!kinds.includes('answer'))
  assert.ok(!kinds.includes('done'))
  assert.equal((await (await post(`/api/chat/runs/${runId}/cancel`)).json()).status, 'cancelled')
  const detail = await (await fetch(`${baseURL}/api/conversations/${cid}`)).json()
  assert.deepEqual(detail.messages, [], 'cancelled answer must not be saved')
  assert.equal((await fetch(`${baseURL}/api/conversations/${cid}`, { method: 'DELETE' })).status, 200)
  assert.equal((await post('/api/chat/stream', { question: 'stale', conversation_id: cid })).status, 410)
  assert.equal((await post('/api/tasks/start', { question: 'stale', conversation_id: cid })).status, 410)

  const earlyCid = await create(), earlyId = `chat_${randomUUID()}`
  await post(`/api/chat/runs/${earlyId}/cancel`)
  const early = await open(earlyCid, earlyId)
  const earlyKinds = []
  while ((event = await early.nextEvent())) earlyKinds.push(event.kind)
  assert.deepEqual(earlyKinds, ['run', 'cancelled'])
  assert.equal((await post('/api/chat/stream', { question: 'duplicate', run_id: earlyId })).status, 409)
  await fetch(`${baseURL}/api/conversations/${earlyCid}`, { method: 'DELETE' })

  const disconnectCid = await create(), disconnectId = `chat_${randomUUID()}`
  const disconnect = await open(disconnectCid, disconnectId)
  do { event = await disconnect.nextEvent(); assert.ok(event) } while (!event.kind.endsWith('_delta'))
  await disconnect.reader.cancel()
  let deletion
  for (let i = 0; i < 40; i++) {
    deletion = await fetch(`${baseURL}/api/conversations/${disconnectCid}`, { method: 'DELETE' })
    if (deletion.status === 200) break
    assert.equal(deletion.status, 409)
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  assert.equal(deletion.status, 200, 'disconnect must release the active execution')
  assert.equal((await (await post(`/api/chat/runs/${disconnectId}/cancel`)).json()).status, 'cancelled')
  console.log('PASS: real HTTP stream cancellation, no saved answer, active deletion blocked, tombstone rejects stale work, early cancel, duplicate ID, disconnect cleanup')
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
