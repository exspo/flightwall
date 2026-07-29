// Caches the app shell so a home-screen launch paints instantly and survives
// the laptop being briefly unreachable. Aircraft data is never cached - stale
// traffic is worse than no traffic.

const CACHE = "flightwall-v1";
const SHELL = [
  ".",
  "index.html",
  "styles.css",
  "app.js",
  "led.js",
  "data/airlines.json",
  "manifest.webmanifest",
  "icons/icon-180.png",
  "icons/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.includes("/api/")) return; // always live

  // Network first, so an updated board reaches the phone on the next launch,
  // with the cache as the offline fallback.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match("index.html")))
  );
});
