/** Per-origin (including port) desktop authentication; no authentication cookies. */
const storageKey = 'scholarflow.desktop-session.v1'
let sessionToken = ''
let installed = false
let navigationError: (message: string) => void = () => undefined
const ticketLinks = new WeakSet<HTMLAnchorElement>()

function localAPI(value: string | URL): URL | null {
  try {
    const url = new URL(String(value), window.location.href)
    if (url.protocol === 'ws:') url.protocol = 'http:'
    if (url.protocol === 'wss:') url.protocol = 'https:'
    return url.origin === window.location.origin && url.pathname.startsWith('/api/') && !url.username && !url.password
      ? url : null
  } catch { return null }
}

export function localSessionHeaders(value: string | URL): Record<string, string> {
  return sessionToken && localAPI(value) ? { Authorization: `ScholarFlow ${sessionToken}` } : {}
}

/** Native browser transports get a single-use ticket for one precise resource. */
export async function localSessionURL(value: string | URL, kind: 'download' | 'websocket' | 'events'): Promise<string> {
  if (!sessionToken) return String(value)
  const target = localAPI(value)
  if (!target || target.hash) throw new Error('无法授权其他地址的本地连接')
  const response = await fetch('/api/local-session/ticket', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: target.pathname + target.search, kind }),
    cache: 'no-store', redirect: 'error',
  })
  if (!response.ok) throw new Error('本地连接授权失败，请重试；若窗口已退出，请重新启动研序')
  const result = await response.json()
  if (typeof result.ticket !== 'string' || !/^[A-Za-z0-9_-]{64}$/.test(result.ticket) || result.kind !== kind)
    throw new Error('本地连接授权响应无效')
  // Build from the known target, never navigate to a URL supplied by a response.
  target.search += (target.search ? '&' : '?') + '__sf_ticket=' + result.ticket
  if (kind === 'websocket') target.protocol = target.protocol === 'https:' ? 'wss:' : 'ws:'
  return target.href
}

export async function openLocalDownload(value: string, options: { target?: string; filename?: string } = {}): Promise<void> {
  const link = document.createElement('a')
  link.href = await localSessionURL(value, 'download')
  link.rel = 'noopener noreferrer'
  if (options.target) link.target = options.target
  if (options.filename !== undefined) link.setAttribute('download', options.filename)
  ticketLinks.add(link)
  document.body.append(link)
  link.click()
  link.remove()
}

function installTransports(): void {
  if (installed) return
  installed = true
  const originalFetch = window.fetch.bind(window)
  window.fetch = (input, init) => {
    const value = input instanceof Request ? input.url : String(input)
    if (!sessionToken || !localAPI(value)) return originalFetch(input, init)
    const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined))
    headers.set('Authorization', `ScholarFlow ${sessionToken}`)
    return originalFetch(input, { ...init, headers, redirect: 'error' })
  }
  // Axios instances and PDF.js may use XHR. Only the exact application origin
  // receives the standard Authorization header (browsers strip it on redirects).
  const urls = new WeakMap<XMLHttpRequest, string>()
  const explicitAuthorization = new WeakSet<XMLHttpRequest>()
  const originalOpen = XMLHttpRequest.prototype.open
  const originalSend = XMLHttpRequest.prototype.send
  const originalSetHeader = XMLHttpRequest.prototype.setRequestHeader
  XMLHttpRequest.prototype.open = function (method: string, url: string | URL, ...args: unknown[]) {
    urls.set(this, String(url))
    explicitAuthorization.delete(this)
    return Reflect.apply(originalOpen, this, [method, url, ...args])
  } as typeof originalOpen
  XMLHttpRequest.prototype.setRequestHeader = function (name: string, value: string) {
    if (name.toLowerCase() === 'authorization') explicitAuthorization.add(this)
    return originalSetHeader.call(this, name, value)
  }
  XMLHttpRequest.prototype.send = function (body?: Document | XMLHttpRequestBodyInit | null) {
    if (sessionToken && localAPI(urls.get(this) || '') && !explicitAuthorization.has(this))
      originalSetHeader.call(this, 'Authorization', `ScholarFlow ${sessionToken}`)
    return originalSend.call(this, body)
  }
  document.addEventListener('click', event => {
    const anchor = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[href]') : null
    if (!anchor || event.defaultPrevented || !sessionToken || !localAPI(anchor.href)) return
    if (ticketLinks.has(anchor)) { ticketLinks.delete(anchor); return }
    event.preventDefault()
    void openLocalDownload(anchor.href, {
      target: event.ctrlKey || event.metaKey || event.shiftKey ? '_blank' : anchor.target,
      filename: anchor.hasAttribute('download') ? anchor.getAttribute('download') || '' : undefined,
    }).catch(() => navigationError('下载未能开始，请重试；若窗口已退出，请重新启动研序'))
  }, true)
}

/** Remove the one-use launch fragment before importing/starting the router. */
export async function initializeLocalSession(onNavigationError?: (message: string) => void): Promise<void> {
  navigationError = onNavigationError || navigationError
  const fragment = window.location.hash
  const match = /^#sf-launch=([A-Za-z0-9_-]{64})$/.exec(fragment)
  if (fragment.startsWith('#sf-launch=')) {
    history.replaceState(history.state, '', window.location.pathname + window.location.search)
    if (!match) throw new Error('Invalid desktop launch handoff')
    const result = await fetch('/api/local-session/bootstrap', {
      method: 'POST', headers: { Authorization: `Bearer ${match[1]}` }, cache: 'no-store',
      redirect: 'error', referrerPolicy: 'no-referrer',
    })
    if (!result.ok) throw new Error('Desktop launch handoff was not accepted')
    const state = await result.json()
    if (typeof state.token !== 'string' || !/^[A-Za-z0-9_-]{64}$/.test(state.token)) throw new Error('Invalid desktop session')
    sessionToken = state.token
    sessionStorage.setItem(storageKey, sessionToken)
  } else {
    const stored = sessionStorage.getItem(storageKey) || ''
    sessionToken = /^[A-Za-z0-9_-]{64}$/.test(stored) ? stored : ''
  }
  const result = await fetch('/api/local-session/status', {
    headers: localSessionHeaders('/api/local-session/status'), cache: 'no-store', redirect: 'error',
  })
  if (!result.ok) throw new Error('Desktop instance is unavailable')
  const state = await result.json()
  if (state.required === true && state.authenticated !== true) {
    sessionStorage.removeItem(storageKey)
    sessionToken = ''
    throw new Error('Desktop instance authentication required')
  }
  if (state.required !== true) {
    sessionStorage.removeItem(storageKey)
    sessionToken = ''
  } else installTransports()
}
