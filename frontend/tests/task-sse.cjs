const assert = require('node:assert/strict')
const baseURL = process.env.SCHOLARFLOW_TEST_URL || 'http://127.0.0.1:8766'

async function main() {
  const health = await (await fetch(`${baseURL}/api/health`)).json()
  assert.equal(health.llm_mode, 'mock', 'This smoke test requires an isolated offline backend')
  const controller = new AbortController()
  const deadline = setTimeout(() => controller.abort(), 12000)
  let reader
  try {
    const response = await fetch(`${baseURL}/api/tasks/events`, { signal: controller.signal })
    assert.equal(response.status, 200)
    assert.match(response.headers.get('content-type'), /text\/event-stream/)
    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    const snapshots = []
    async function nextSnapshot() {
      while (true) {
        const boundary = buffer.indexOf('\n\n')
        if (boundary >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const data = frame.split('\n').find((line) => line.startsWith('data: '))
          if (data) {
            const snapshot = JSON.parse(data.slice(6)).tasks
            snapshots.push(snapshot)
            return snapshot
          }
        } else {
          const chunk = await reader.read()
          assert.equal(chunk.done, false, 'SSE must stay open')
          buffer += decoder.decode(chunk.value, { stream: true })
        }
      }
    }
    await nextSnapshot()
    const start = await fetch(`${baseURL}/api/tasks/start`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: 'Offline task lifecycle verification', title: '[Verification] Offline SSE lifecycle' }),
    })
    assert.equal(start.status, 200)
    const task = await start.json()
    let finished
    while (!finished) {
      const snapshot = await nextSnapshot()
      finished = snapshot.find((item) => item.id === task.id && ['done', 'failed', 'cancelled'].includes(item.status))
    }
    assert.equal(finished.status, 'done')
    assert.equal(finished.progress, 1)
    assert.ok(finished.result.answer)
    assert.ok(snapshots.length >= 2)
    console.log(`PASS: live HTTP SSE received ${snapshots.length} snapshots and completed an offline background task`)
  } finally {
    clearTimeout(deadline)
    controller.abort()
    await reader?.cancel().catch(() => {})
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1 })
