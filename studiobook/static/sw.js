// Service worker: keeps the app shell available offline. Pages always come from the network when online.
const CACHE = "studiobook-v4";
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/static/app.css", "/static/app.js", "/offline"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") { return; }
  const url = new URL(request.url);
  if (url.origin !== location.origin) { return; }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(fetch(request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE).then((c) => c.put(request, copy));
      return response;
    }).catch(() => caches.match(request)));
  } else if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/offline")));
  }
});
