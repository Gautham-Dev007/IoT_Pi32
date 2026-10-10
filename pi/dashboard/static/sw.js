const CACHE = 'iothub-v2.19';
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png', '/badge-72.png', '/qrcode.js'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok && SHELL.includes(url.pathname)) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(url.pathname, copy)); }
      return r;
    }).catch(() => caches.match(url.pathname === '/' || e.request.mode === 'navigate' ? '/' : url.pathname))
  );
});

const b64 = s => Uint8Array.from(atob((s + '='.repeat((4 - s.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'IoT Hub', {
    body: d.body || '', icon: '/icon-192.png', badge: '/badge-72.png', tag: d.tag || undefined, renotify: !!d.tag,
    data: { url: d.url || '/', answer: d.answer || null }, actions: (d.actions || []).slice(0, 2),
    silent: !!d.silent, requireInteraction: d.level === 'crit', timestamp: (d.ts || Date.now() / 1000) * 1000,
  }));
});
self.addEventListener('notificationclick', e => {
  const n = e.notification, d = n.data || {};
  n.close();
  if (e.action && d.answer) {
    e.waitUntil(fetch(`/api/rain/${d.answer.id}/answer?sig=${encodeURIComponent(d.answer.sig)}&a=${encodeURIComponent(e.action)}`, { method: 'POST' })
      .then(r => r.json())
      .then(j => self.registration.showNotification('Thanks!', { body: j.thanks || 'Noted.', icon: '/icon-192.png', badge: '/badge-72.png', tag: 'thanks', silent: true }))
      .catch(() => self.clients.openWindow(d.url || '/')));
    return;
  }
  const url = new URL(d.url || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.startsWith(self.location.origin) && 'focus' in c) { c.postMessage({ type: 'open', url }); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
self.addEventListener('pushsubscriptionchange', e => {
  e.waitUntil((async () => {
    const info = await fetch('/api/push').then(r => r.json());
    if (!info.key) return;
    const sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(info.key) });
    await fetch('/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscription: sub.toJSON(), replaces: e.oldSubscription ? e.oldSubscription.endpoint : null }) });
  })());
});
