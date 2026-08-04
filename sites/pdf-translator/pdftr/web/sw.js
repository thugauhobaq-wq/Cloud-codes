/* Service worker: приложение открывается с домашнего экрана мгновенно и не
   показывает «нет сети», когда её нет.

   Кэшируется только оболочка — разметка, стили, скрипт, иконки. Запросы к /api/
   не кэшируются никогда: список задач и прогресс должны быть настоящими, а
   показать вчерашний прогресс хуже, чем не показать никакого. */

const VERSION = "pdftr-1";
const SHELL = [
  "/",
  "/app.js",
  "/style.css",
  "/manifest.webmanifest",
  "/icon.svg",
  "/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((n) => n !== VERSION).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/share") return;

  // Переходы: сначала сеть, иначе — сохранённая оболочка. Так свежая версия
  // приложения приезжает сразу, а без сети открывается хотя бы список.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put("/", copy)).catch(() => {});
          return response;
        })
        .catch(() => caches.match("/").then((hit) => hit || fetch(request)))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(
      (hit) =>
        hit ||
        fetch(request).then((response) => {
          if (response.ok && response.type === "basic") {
            const copy = response.clone();
            caches.open(VERSION).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
    )
  );
});
