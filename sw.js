// Service worker Sunrise Space : l'application s'ouvre même avec un réseau faible.
// Les appels /api ne sont JAMAIS mis en cache (positions et statuts doivent être frais).
const CACHE = "sunrise-space-v21";
const SHELL = ["/", "/style.css", "/app.js", "/manifest.webmanifest", "/icon.svg",
               "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // Réseau d'abord (toujours la dernière version), cache en secours hors ligne.
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});

// Notifications push : s'affichent même application fermée (iOS exige d'en afficher une à chaque push).
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { /* message vide */ }
  // Demande de course : vibration longue (environ 10 s, Android) et notification qui reste affichée jusqu'au toucher.
  const urgent = !!d.urgent;
  const longBuzz = []; for (let i = 0; i < 9; i++) longBuzz.push(800, 300);
  e.waitUntil(Promise.all([
    self.registration.showNotification(d.title || "Sunrise Space", {
      body: d.body || "", tag: d.tag || "sunrise", renotify: true, requireInteraction: urgent,
      vibrate: urgent ? longBuzz : [200], icon: "/icon-192.png", badge: "/icon-192.png", data: { url: d.url || "/" },
    }),
    // application ouverte : on la prévient pour qu'elle actualise et fasse sonner sa sirène
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => list.forEach((c) => c.postMessage({ type: "push", urgent }))),
  ]));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) { if ("focus" in c) return c.focus(); }
    return self.clients.openWindow(url);
  }));
});
