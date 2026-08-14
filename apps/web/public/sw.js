const CACHE_VERSION = 'workama-shell-v1'
const SHELL_CACHE = CACHE_VERSION
const SHELL_URLS = ['/', '/index.html', '/manifest.webmanifest']
const API_PATH_PREFIXES = ['/api/', '/v1/']

function isSameOrigin(url) {
  return url.origin === self.location.origin
}

function isApiRequest(url) {
  return API_PATH_PREFIXES.some((prefix) => url.pathname.startsWith(prefix))
}

function isStaticShellRequest(request, url) {
  if (request.method !== 'GET' || !isSameOrigin(url) || isApiRequest(url)) return false
  return url.pathname === '/' ||
    url.pathname === '/index.html' ||
    url.pathname === '/manifest.webmanifest' ||
    url.pathname.startsWith('/assets/')
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith('workama-shell-') && key !== SHELL_CACHE)
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting()
})

async function networkFirstNavigation(request) {
  try {
    return await fetch(request)
  } catch {
    return caches.match('/index.html')
  }
}

async function cacheFirstStatic(request) {
  const cached = await caches.match(request)
  if (cached) return cached

  const response = await fetch(request)
  if (response.ok) {
    const cache = await caches.open(SHELL_CACHE)
    await cache.put(request, response.clone())
  }
  return response
}

self.addEventListener('fetch', (event) => {
  const { request } = event
  const url = new URL(request.url)

  if (request.mode === 'navigate' && isSameOrigin(url) && !isApiRequest(url)) {
    event.respondWith(networkFirstNavigation(request))
    return
  }

  if (isStaticShellRequest(request, url)) event.respondWith(cacheFirstStatic(request))
})
