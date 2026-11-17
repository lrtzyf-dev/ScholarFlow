import { shallowReactive } from 'vue'
import type { SourceRevision } from './sourceApi'

export interface SourceUpdateDraft {
  file?: File
  local?: { projectId: string; workspaceRevision: number; path: string }
  expectedVersion: number
  baseline: SourceRevision
  requestId: string
  conflict: boolean
  persisted?: boolean
}

interface SourceUpdateState {
  pending: SourceUpdateDraft | null
  saving: boolean
  lastSaved: SourceRevision | null
  loaded: boolean
  persistedRevision: number
}

// The scoped view cache is hydrated from database staging, including original
// bytes, after a renderer/app restart. It never imports a draft into the library.
const updates = new Map<string, SourceUpdateState>()
export function sourceUpdateState(conversationId: string, sourceId: string): SourceUpdateState {
  const key = JSON.stringify([conversationId, sourceId])
  let state = updates.get(key)
  if (!state) {
    state = shallowReactive<SourceUpdateState>({
      pending: null,
      saving: false,
      lastSaved: null,
      loaded: false,
      persistedRevision: 0,
    })
    updates.set(key, state)
  }
  return state
}
