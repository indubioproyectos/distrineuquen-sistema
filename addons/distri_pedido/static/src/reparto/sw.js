/* Service worker of the delivery app. Served by /reparto/sw.js, which fills
 * in the version and the shell list. Caches the app shell so the app opens
 * without signal: the app files, the style shared with the packing screen
 * (distri_ui.css) and the self-hosted Figtree font. The API (/reparto/api/...)
 * is never cached: the route and the outbox live in IndexedDB. */
const VERSION = "__VERSION__";
const SHELL = __SHELL__;
const CACHE = "reparto-" + VERSION;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { credentials: "same-origin" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        // also the old "reparto-fonts" (Google Fonts, no longer used)
        .filter((key) => key.startsWith("reparto-") && key !== CACHE)
        .map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);

  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/reparto/api/")) return;

  // The shell: network first (picks up a new version and an expired
  // session), the cached copy without signal.
  if (request.mode === "navigate" && url.pathname.replace(/\/$/, "") === "/reparto") {
    event.respondWith(
      fetch(request)
        .then((res) => {
          if (res.ok && !res.redirected) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put("/reparto", copy));
          }
          return res;
        })
        .catch(() => caches.match("/reparto"))
    );
    return;
  }

  if (SHELL.includes(url.pathname + url.search) || url.pathname === "/reparto/manifest.webmanifest") {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
