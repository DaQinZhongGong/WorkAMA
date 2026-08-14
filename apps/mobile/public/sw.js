const CACHE_VERSION = 'workama-mobile-shell-v2'
const SHELL_URLS = ['/', '/index.html', '/manifest.webmanifest', '/chat', '/agents', '/knowledge', '/settings']
const API_PREFIXES = ['/api/', '/v1/']

function sameOrigin(url) { return url.origin === self.location.origin }
function isApi(url) { return API_PREFIXES.some((prefix) => url.pathname.startsWith(prefix)) }
function isStatic(request, url) {
  return request.method === 'GET' && sameOrigin(url) && !isApi(url) && (url.pathname === '/' || url.pathname === '/index.html' || url.pathname === '/manifest.webmanifest' || url.pathname.startsWith('/assets/') || url.pathname === '/chat' || url.pathname.startsWith('/chat/') || url.pathname === '/agents' || url.pathname === '/knowledge' || url.pathname === '/settings')
}

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_VERSION).then((cache) => cache.addAll(SHELL_URLS)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith('workama-mobile-shell-') && key !== CACHE_VERSION).map((key) => caches.delete(key)))).then(() => self.clients.claim()))
})

self.addEventListener('push', (event) => {
  const data = event.data?.json() ?? {}
  event.waitUntil(self.registration.showNotification(data.title ?? 'WorkAMA Mobile', {
    body: data.body ?? '',
    icon: data.icon ?? '/manifest.webmanifest',
  }))
})

let __testShowNotification = null

self.addEventListener('message', (event) => {
  if (event.data?.type === 'PWA_TEST_MOCK_NOTIFICATIONS') {
    __testShowNotification = async (title, options) => {
      event.source?.postMessage({ type: 'PWA_TEST_PUSH_RESULT', ok: true, mock: true, title, options })
    }
    return
  }
  if (event.data?.type === 'PWA_TEST_PUSH') {
    const show = __testShowNotification ?? self.registration.showNotification.bind(self.registration)
    event.waitUntil(show(event.data.title ?? 'Test', event.data.options ?? {})
      .then(() => { if (__testShowNotification) return; event.source?.postMessage({ type: 'PWA_TEST_PUSH_RESULT', ok: true }) })
      .catch((error) => event.source?.postMessage({ type: 'PWA_TEST_PUSH_RESULT', ok: false, error: String(error) })))
  }
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (event.request.mode === 'navigate' && sameOrigin(url) && !isApi(url)) {
    event.respondWith(fetch(event.request).catch(() => caches.match('/index.html')))
    return
  }
  if (!isStatic(event.request, url)) return
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request).then(async (response) => {
    if (response.ok) {
      const cache = await caches.open(CACHE_VERSION)
      await cache.put(event.request, response.clone())
    }
    return response
  })))
})
