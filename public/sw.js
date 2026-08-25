/**
 * Upstox Options Bot - Service Worker
 * Version: 2.2.0
 * 
 * SECURITY & SAFETY RULES:
 * 1. NEVER cache /api/* responses, tokens, credentials, or live order/trade data.
 * 2. NEVER intercept WebSocket traffic.
 * 3. Network-First for HTML navigation so updates load immediately.
 * 4. Cache static UI shell (JS/CSS/Icons) with automatic version cleanup.
 * 5. Strictly ignore unsupported URL schemes (chrome-extension://, moz-extension://, etc.).
 */

const CACHE_NAME = 'upstox-bot-v2.2.0';
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/manifest.webmanifest',
  '/favicon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-512-maskable.png',
  '/icons/apple-touch-icon.png',
  '/icons/favicon-32x32.png',
  '/icons/favicon-16x16.png',
  '/icons/icon.svg',
];

// Helper: check if request is cacheable (valid HTTP/HTTPS GET)
function isCacheable(request) {
  return request.method === 'GET' && (request.url.startsWith('http://') || request.url.startsWith('https://'));
}

// Install: pre-cache critical app shell and activate immediately
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn('[SW] Pre-cache partial warning:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

// Activate: delete outdated caches and claim clients
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name.startsWith('upstox-bot-') && name !== CACHE_NAME)
          .map((name) => {
            console.log('[SW] Purging legacy cache:', name);
            return caches.delete(name);
          })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch: enforce strict security boundaries and protocol validation
self.addEventListener('fetch', (event) => {
  // 0. Only handle HTTP and HTTPS requests; ignore chrome-extension://, moz-extension://, etc.
  if (!isCacheable(event.request)) {
    return;
  }

  let url;
  try {
    url = new URL(event.request.url);
  } catch {
    return;
  }

  // 1. CRITICAL SECURITY: Never cache API calls, auth routes, WebSocket endpoints
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/health') ||
    url.pathname.includes('/auth') ||
    url.pathname.includes('/token') ||
    url.pathname.includes('/trade') ||
    url.pathname.includes('/order') ||
    url.pathname.includes('/ws')
  ) {
    // Pure network passthrough — no cache lookup or storage
    return;
  }

  // 2. Navigation requests (HTML pages): Network-First with Cache Fallback
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.status === 200 && isCacheable(event.request)) {
            const copy = response.clone();
            caches.open(CACHE_NAME)
              .then((cache) => cache.put(event.request, copy))
              .catch(() => {});
          }
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(event.request);
          if (cached) return cached;
          const indexShell = await caches.match('/index.html');
          if (indexShell) return indexShell;
          return new Response('Offline - Upstox Options Bot', {
            status: 503,
            statusText: 'Service Unavailable Offline',
            headers: { 'Content-Type': 'text/plain' },
          });
        })
    );
    return;
  }

  // 3. Static assets (JS/CSS/Images/Fonts): Stale-While-Revalidate
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      const fetchPromise = fetch(event.request)
        .then((networkResponse) => {
          if (
            networkResponse &&
            networkResponse.status === 200 &&
            (networkResponse.type === 'basic' || networkResponse.type === 'cors') &&
            isCacheable(event.request)
          ) {
            const copy = networkResponse.clone();
            caches.open(CACHE_NAME)
              .then((cache) => cache.put(event.request, copy))
              .catch(() => {});
          }
          return networkResponse;
        })
        .catch(() => {
          return cachedResponse;
        });

      return cachedResponse || fetchPromise;
    })
  );
});

// Listen for messages from frontend (e.g. skipWaiting trigger for instant update)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

