// Service Worker للتقويم اليومي
// غيّر رقم النسخة كل ما عدّلت ملفات الموقع عشان يتحدث الكاش عند المستخدمين
const VERSION = 'v4';
const SHELL_CACHE = `calendar-shell-${VERSION}`;
const RUNTIME_CACHE = `calendar-runtime-${VERSION}`;

// ملفات التطبيق الأساسية (مسارات نسبية عشان تشتغل على GitHub Pages)
const SHELL_FILES = [
  './',
  './index.html',
  './manifest.json',
  './matches.json',
  './teams.json',
  './icon-192.png',
  './icon-512.png',
  './icon-maskable-512.png'
];

// مصادر خارجية نخزنها أول ما تنطلب (خط Cairo وشعارات الفرق)
const STATIC_HOSTS = [
  'fonts.googleapis.com',
  'fonts.gstatic.com',
  'upload.wikimedia.org',
  'a.espncdn.com',
  'commons.wikimedia.org'
];

// API أوقات الصلاة
const API_HOSTS = ['api.aladhan.com'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_FILES))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== SHELL_CACHE && k !== RUNTIME_CACHE)
            .map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

// الشبكة أولاً، وإذا فشلت نرجع للكاش
async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

// أوقات الصلاة: إذا ما فيه نت ولا نسخة لنفس اليوم، نعرض آخر أوقات محفوظة
// (الفرق بين يوم ويوم دقيقة أو دقيقتين تقريباً، أحسن من إنها تختفي)
async function prayerTimesFallback(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (err) {
    const exact = await cache.match(request);
    if (exact) return exact;
    // نفس المدينة فقط (نفس الإحداثيات)، عشان ما نعرض أوقات مدينة ثانية
    const wanted = new URL(request.url).searchParams;
    const keys = await cache.keys();
    const older = keys.filter((k) => {
      const u = new URL(k.url);
      return API_HOSTS.includes(u.hostname) &&
        u.searchParams.get('latitude') === wanted.get('latitude') &&
        u.searchParams.get('longitude') === wanted.get('longitude');
    });
    if (older.length) return cache.match(older[older.length - 1]);
    throw err;
  }
}

// الكاش أولاً (للخطوط والصور اللي ما تتغير)
async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  // الردود من دومين ثاني تكون opaque، ونخزنها عادي
  if (response && (response.ok || response.type === 'opaque')) {
    cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // أوقات الصلاة: الشبكة أولاً، وآخر نتيجة ناجحة احتياط
  if (API_HOSTS.includes(url.hostname)) {
    event.respondWith(prayerTimesFallback(request));
    return;
  }

  // الخطوط والشعارات: الكاش أولاً
  if (STATIC_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(request, RUNTIME_CACHE));
    return;
  }

  // ملفات الموقع نفسه: الشبكة أولاً عشان توصل التحديثات بسرعة
  if (url.origin === self.location.origin) {
    event.respondWith(
      networkFirst(request, SHELL_CACHE).catch(() => caches.match('./index.html'))
    );
  }
});
