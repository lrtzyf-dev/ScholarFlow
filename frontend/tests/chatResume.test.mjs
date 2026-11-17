import assert from 'node:assert/strict'
import test from 'node:test'
import { durableResumeRequest } from '../src/chatResume.ts'

function storage() {
  const values = new Map()
  return { getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) }
}

test('lost resume acknowledgement and reload reuse the original CAS and request identity', () => {
  const local = storage()
  const first = durableResumeRequest(local, 'conversation-A', 'run-A', 4, () => 'resume_stable_001')
  const retry = durableResumeRequest(local, 'conversation-A', 'run-A', 8, () => assert.fail('no new request'))
  assert.deepEqual(first, retry)
  assert.equal(retry.request.expected_run_revision, 4)
  local.removeItem(first.key)
  const next = durableResumeRequest(local, 'conversation-A', 'run-A', 9, () => 'resume_stable_002')
  assert.equal(next.request.resume_request_id, 'resume_stable_002')
})

test('admission identities stay separated by conversation and run', () => {
  const local = storage()
  const first = durableResumeRequest(local, 'A', 'run-A', 1, () => 'resume_stable_001')
  const second = durableResumeRequest(local, 'B', 'run-A', 2, () => 'resume_stable_002')
  const third = durableResumeRequest(local, 'A', 'run-B', 3, () => 'resume_stable_003')
  assert.notEqual(first.key, second.key)
  assert.notEqual(first.key, third.key)
})

test('a persisted receipt exposes only the admission identity and expected revision', () => {
  const local = storage()
  local.setItem('scholarflow:pending-resume:A:run-A', JSON.stringify({
    resume_request_id: 'resume_stable_001', expected_run_revision: 3,
    question: 'unexpected', run_id: 'other', resume_checkpoint: false,
  }))
  assert.deepEqual(durableResumeRequest(local, 'A', 'run-A', 9, () => assert.fail()).request,
    { resume_request_id: 'resume_stable_001', expected_run_revision: 3 })
})
