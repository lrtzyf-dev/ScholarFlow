// Stateful CAS mock for workflow tests. The separate desktop-state.cjs suite
// verifies the real database and independent application processes/ports.
module.exports = function desktopStateMock() {
  const documents = new Map()
  return async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (/^\/api\/conversations\/[^/]+\/read$/.test(path)) {
      await route.fulfill({ json: { message_ids: request.postDataJSON().message_ids } })
      return true
    }
    if (!path.startsWith('/api/desktop-state/')) return false
    const fallback = path.endsWith('/selection') ? { conversation_id: null } : path.endsWith('/sidebar') ? { sort: 'updated_at' } : {
      text: '', paper_id: null, skills: [], images: [], reading: { following: true, top: 0, anchor_id: null, offset: 0 },
    }
    const current = documents.get(path) || { revision: 0, value: fallback }
    if (request.method() === 'GET') await route.fulfill({ json: current })
    else {
      const body = request.postDataJSON()
      if (body.revision !== current.revision) await route.fulfill({ status: 409, json: { detail: current } })
      else {
        const next = { revision: current.revision + 1, value: body.value }
        documents.set(path, next)
        await route.fulfill({ json: next })
      }
    }
    return true
  }
}
