const CACHE_NAME = 'morphe-patcher-shell-v1';
const SCOPE_URL = self.registration.scope;
const SCOPE_PATH = new URL(SCOPE_URL).pathname;
const SHELL_URL = new URL('./', SCOPE_URL).href;
const APP_RESOURCES = [
  SHELL_URL,
  new URL('manifest.webmanifest', SCOPE_URL).href,
  new URL('icons/app-icon.svg', SCOPE_URL).href
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_RESOURCES)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter(key => key.startsWith('morphe-patcher-shell-') && key !== CACHE_NAME)
      .map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(SCOPE_PATH)) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(SHELL_URL, response.clone());
        }
        return response;
      } catch (error) {
        return (await caches.match(request)) || (await caches.match(SHELL_URL)) || Response.error();
      }
    })());
    return;
  }

  const isAppResource = url.pathname === new URL('manifest.webmanifest', SCOPE_URL).pathname
    || url.pathname.startsWith(new URL('icons/', SCOPE_URL).pathname);
  if (!isAppResource) return;

  event.respondWith((async () => {
    const cached = await caches.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) (await caches.open(CACHE_NAME)).put(request, response.clone());
    return response;
  })());
});
