/**
 * @file  Dashawy Service Worker
 * @module  sw
 *
 * Caching strategy:
 *   - HTML pages     → network-first, cache fallback (always tries fresh)
 *   - CSS / JS files → network-first with cache fallback (BUSTS on update)
 *   - Images         → cache-first (they rarely change)
 *   - API calls      → network-only (never cached)
 *   - Uploads        → network-only
 *
 * Note on CSS / JS: these are deliberately NOT cache-first even though they are
 * the classic candidate for it. RUNTIME_CACHE deliberately outlives a version
 * bump (activate keeps it), so a cache-first asset is never evicted — it would
 * serve the previous release's JS and CSS to every returning user until the
 * cache name itself changed. Network-first with a `cache: 'no-store'` fetch
 * gets the same instant updates offline users would get from a hard refresh,
 * which is the behaviour the previous "need a hard refresh" bug report wanted.
 *
 * Version: bump CACHE_VERSION on every release to force a clean cache.
 */

// v12 also drops the runtime cache: earlier releases could have cached HTML
// pages before they were moved behind Django's template engine, and those old
// pages contain raw template markers that the browser then requested as URLs.
const CACHE_VERSION = 'dashawy-v12-2026-10-28';
const RUNTIME_CACHE = 'dashawy-runtime-v4';

const PRECACHE_URLS = ['/', '/index.html', '/offline.html', '/manifest.webmanifest'];

// ------------------------------------------------------------
// Install: pre-cache the shell
// ------------------------------------------------------------
// NOTE: skipWaiting() is deliberately NOT called here. The new worker must stay
// in "waiting" so the page can show the "new version available" banner and only
// swap when the user clicks it (it posts SKIP_WAITING, handled below). Calling
// skipWaiting() here would activate immediately and the page's
// controllerchange handler would reload silently — exactly the behaviour the
// banner is meant to avoid, and it would also discard in-progress form input.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      cache.addAll(PRECACHE_URLS).catch((err) => {
        console.warn('[sw] precache partial failure:', err);
      })
    )
  );
});

// ------------------------------------------------------------
// Activate: delete ALL old caches and take control immediately
// ------------------------------------------------------------
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== CACHE_VERSION && k !== RUNTIME_CACHE)
            .map((k) => {
              console.log('[sw] deleting old cache:', k);
              return caches.delete(k);
            })
        )
      )
      .then(() => self.clients.claim())
  );
});

// ------------------------------------------------------------
// Message: allow pages to force an update
// ------------------------------------------------------------
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// ------------------------------------------------------------
// Fetch strategy
// ------------------------------------------------------------
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin GET requests
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;

  // Never cache API or uploads
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/uploads/')) {
    return;
  }

  // Never cache the SW itself
  if (url.pathname === '/sw.js') return;

  const isHtml = req.headers.get('accept')?.includes('text/html');
  const isAsset =
    url.pathname.endsWith('.css') ||
    url.pathname.endsWith('.js') ||
    url.pathname.endsWith('.webmanifest');

  // ---------- HTML: network-first ----------
  if (isHtml) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(RUNTIME_CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((r) => r || caches.match('/offline.html')))
    );
    return;
  }

  // ---------- CSS / JS / manifest: network-first with cache fallback ----------
  // This is the FIX for the "need hard refresh" problem.
  if (isAsset) {
    event.respondWith(
      fetch(req, { cache: 'no-store' })
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(RUNTIME_CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  // ---------- Images and everything else: cache-first ----------
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(RUNTIME_CACHE).then((c) => c.put(req, copy));
        }
        return res;
      });
    })
  );
});

// ------------------------------------------------------------
// Push notifications
// ------------------------------------------------------------
self.addEventListener('push', (event) => {
  // No data at all is a valid push event (an encrypted-but-empty payload), and
  // dropping it silently is correct: there is nothing to show.
  if (!event.data) return;

  let data = {};
  try {
    data = event.data.json();
  } catch (e) {
    // Some push services deliver plain text; showing the text beats nothing.
    try {
      data = { body: event.data.text() };
    } catch (err) {
      data = {};
    }
  }

  event.waitUntil(
    self.registration.showNotification(data.title || 'دشاوي', {
      body: data.body || '',
      icon: '/dashawy-brand/icon-192.png',
      badge: '/dashawy-brand/icon-192.png',
      dir: 'rtl',
      lang: 'ar',
      // The server sends a per-request tag ("new-request-42"). Reusing the tag
      // makes a newer notification REPLACE the older one for the same event
      // instead of stacking duplicates — but only when the tag is non-empty,
      // otherwise every push would overwrite the previous, unrelated one.
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      // The server sets this on the prompts that need an answer — the new
      // request, and the rating request after a job is finished. It keeps the
      // banner on screen until the customer acts instead of letting it fade
      // into the tray. It is a Notification option, not part of the push
      // payload, so it has to be lifted across here or it does nothing.
      requireInteraction: Boolean(data.requireInteraction),
      vibrate: data.requireInteraction ? [120, 60, 120] : undefined,
      data: { url: data.url || '/' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const raw = (event.notification.data && event.notification.data.url) || '/';
  // Only ever navigate within our own origin: a payload is data from the
  // network, and openWindow() on an arbitrary absolute URL would happily ship
  // the user to a third party.
  let target = raw;
  try {
    const parsed = new URL(raw, self.location.origin);
    target = parsed.origin === self.location.origin ? parsed.pathname + parsed.search : '/';
  } catch (e) {
    target = '/';
  }

  event.waitUntil(
    (async () => {
      const wanted = new URL(target, self.location.origin).pathname;
      // includeUncontrolled: a tab opened before this worker took control still
      // counts as "the app is open" and should be focused rather than duplicated.
      const windows = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      });

      for (const client of windows) {
        if (new URL(client.url).pathname === wanted) {
          // Already on the right page: just surface it.
          if (client.focus) await client.focus();
          return;
        }
      }

      // A tab is open, but on another page. Reuse it and navigate, so the user
      // gets one window instead of a new tab next to the old one.
      if (windows.length > 0 && windows[0].navigate) {
        await windows[0].focus();
        await windows[0].navigate(target);
        return;
      }

      await self.clients.openWindow(target);
    })()
  );
});
