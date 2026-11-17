import assert from 'node:assert/strict'
import test from 'node:test'
import { durableTaskResumeRequest } from '../src/taskResume.ts'

function storage() {
  const values = new Map()
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key) }
}
test('lost background acknowledgement preserves original request, CAS and choice', () => {
  const local = storage()
  const first = durableTaskResumeRequest(local, 'A', 'run-A', 3, 'retry', () => 'resume_stable_001')
  const retry = durableTaskResumeRequest(local, 'A', 'run-A', 9, 'retry', () => assert.fail('duplicate'))
  assert.deepEqual(first, retry)
  assert.equal(retry.request.expected_run_revision, 3)
  assert.throws(() => durableTaskResumeRequest(local, 'A', 'run-A', 9, 'skip', () => assert.fail()), /原选择/)
  local.removeItem(first.key)
  assert.equal(durableTaskResumeRequest(local, 'A', 'run-A', 9, 'skip', () => 'resume_stable_002').request.expected_run_revision, 9)
})
test('different conversations and runs never share admissions', () => {
  const local = storage()
  const a = durableTaskResumeRequest(local, 'A', 'run-A', 1, undefined, () => 'resume_stable_001')
  const b = durableTaskResumeRequest(local, 'B', 'run-A', 1, undefined, () => 'resume_stable_002')
  const c = durableTaskResumeRequest(local, 'A', 'run-B', 1, undefined, () => 'resume_stable_003')
  assert.notEqual(a.key, b.key); assert.notEqual(a.key, c.key)
})
test('missing version and damaged prior receipt stop before creating an identity', () => {
  const local = storage()
  for (const revision of [undefined, null, 0, -1, 1.1, NaN])
    assert.throws(() => durableTaskResumeRequest(local, 'A', 'run-A', revision, undefined, () => assert.fail()), /版本/)
  local.setItem('scholarflow:pending-task-resume:A:run-A', '{damaged')
  assert.throws(() => durableTaskResumeRequest(local, 'A', 'run-A', 1, undefined, () => assert.fail()), /无法核对/)
})
test('only request identity and CAS are forwarded from durable storage', () => {
  const local = storage()
  local.setItem('scholarflow:pending-task-resume:A:run-A', JSON.stringify({
    resume_request_id: 'resume_stable_001', expected_run_revision: 4,
    conversation_id: 'other', resolution: undefined, question: 'untrusted', retry_unknown: true,
  }))
  assert.deepEqual(durableTaskResumeRequest(local, 'A', 'run-A', 9, undefined, () => assert.fail()).request,
    { resume_request_id: 'resume_stable_001', expected_run_revision: 4 })
})
