/* Only cache the application shell; never API responses or conversation data. */
const CACHE = "carme-web-v11";
// 与 index.html 的 ?v= 保持一致：换图标时两边一起加一，旧缓存会被 activate 清掉
const SHELL = [
  "/",
  "/manifest.webmanifest?v=3",
  "/favicon.png?v=3",
  "/apple-touch-icon.png?v=3",
  "/icon-192.png?v=3",
];

function sameUnredirectedResponse(request, response) {
  if (!response || !response.ok || response.redirected || response.type === "opaque" || response.type === "opaqueredirect") return false;
  const requestURL = new URL(request.url);
  const responseURL = new URL(response.url);
  return responseURL.origin === self.location.origin && responseURL.pathname === requestURL.pathname;
}

async function cacheable(request, response, shell) {
  if (!sameUnredirectedResponse(request, response)) return false;
  if ((response.headers.get("cache-control") || "").includes("no-store")) return false;
  const contentType = (response.headers.get("content-type") || "").toLowerCase();
  if (shell) {
    if (!contentType.includes("text/html")) return false;
    return (await response.clone().text()).includes('<meta name="carme-app-shell" content="1"');
  }
  if (contentType.includes("text/html")) return false;
  if (new URL(request.url).pathname === "/manifest.webmanifest") {
    return contentType.includes("manifest+json") || contentType.includes("application/json");
  }
  if (/\.(png)$/.test(new URL(request.url).pathname)) return contentType.includes("image/png");
  return true;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then(async (cache) => {
      for (const url of SHELL) {
        const request = new Request(url, { cache: "no-store", credentials: "same-origin" });
        const response = await fetch(request);
        if ((response.headers.get("cache-control") || "").includes("no-store")) continue;
        if (!(await cacheable(request, response, url === "/"))) throw new Error(`拒绝缓存非 Carme 响应: ${url}`);
        await cache.put(request, response.clone());
      }
    })
      .then(() => self.skipWaiting()),
  );
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("carme-") && key !== CACHE)
            .map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  // The gateway marks account-bearing HTML no-store; only legacy generic shells cache.
  const shell = request.mode === "navigate" && url.pathname === "/" && !url.search;
  const asset =
    url.pathname.startsWith("/assets/") ||
    /^(\/icon[^/]*\.png|\/favicon\.png|\/apple-touch-icon\.png)$/.test(url.pathname) ||
    url.pathname === "/manifest.webmanifest";
  if (!shell && !asset) return;
  event.respondWith(
    fetch(request).then(async (response) => {
        if (await cacheable(request, response, shell)) {
          const copy = response.clone();
          event.waitUntil(
            caches
              .open(CACHE)
              .then((cache) => cache.put(shell ? "/" : request, copy)),
          );
        }
        return response;
      })
      .catch(
        async () =>
          (await caches.match(shell ? "/" : request)) || Response.error(),
      ),
  );
});
