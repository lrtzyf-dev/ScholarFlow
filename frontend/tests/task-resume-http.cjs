const assert = require('node:assert/strict')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'
async function main() {
  assert.equal((await (await fetch(`${baseURL}/api/health`)).json()).llm_mode, 'mock')
  const post = async (path, body = {}) => {
    const response = await fetch(`${baseURL}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(12000) })
    assert.equal(response.status, 200, await response.clone().text())
    return response.json()
  }
  const get = async (id) => (await fetch(`${baseURL}/api/tasks/${id}`, { signal: AbortSignal.timeout(12000) })).json()
  const wait = async (id, predicate) => {
    const deadline = Date.now() + 12000
    while (Date.now() < deadline) {
      const task = await get(id)
      if (predicate(task)) return task
      await new Promise((resolve) => setTimeout(resolve, 10))
    }
    throw new Error('Task state timeout')
  }
  const conversation = await post('/api/conversations', { title: '[Verification] Checkpoint resume' })
  const task = await post('/api/tasks/start', { question: 'Offline resumable research verification', conversation_id: conversation.id })
  await wait(task.id, (item) => item.status === 'running' && item.recovery?.completed_tools >= 1)
  await post(`/api/tasks/${task.id}/cancel`)
  const stopped = await wait(task.id, (item) => item.status === 'cancelled')
  assert.ok(stopped.recovery.available)
  const tools = stopped.recovery.completed_tools
  const eventPrefix = stopped.events
  await post(`/api/tasks/${task.id}/resume`)
  const finished = await wait(task.id, (item) => item.status === 'done')
  assert.equal(finished.recovery.completed_tools, tools, 'resume must not rerun completed tool calls')
  assert.equal(finished.progress, 1)
  assert.ok(finished.events.some((e) => e.kind === 'resume'))
  assert.ok(eventPrefix.every((event) => finished.events.some((e) => JSON.stringify(e) === JSON.stringify(event))))
  let conversationDetail = await (await fetch(`${baseURL}/api/conversations/${conversation.id}`)).json()
  assert.equal(conversationDetail.messages.length, 1)
  const rerun = await post(`/api/tasks/${task.id}/restart`)
  assert.notEqual(rerun.id, task.id)
  await wait(rerun.id, (item) => item.status === 'done')
  assert.equal((await get(task.id)).status, 'done')
  conversationDetail = await (await fetch(`${baseURL}/api/conversations/${conversation.id}`)).json()
  assert.equal(conversationDetail.messages.length, 2)
  assert.equal((await fetch(`${baseURL}/api/conversations/${conversation.id}`, { method: 'DELETE' })).status, 200)
  console.log('PASS: real HTTP background cancel/resume retains tools and event history, saves one answer, explicit restart creates a separate run')
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
