"""隔离浏览器回归：Access 302/200 不污染 SW 缓存，离线仍读取真实应用壳。"""
from __future__ import annotations

import asyncio
from pathlib import Path
import http.server
import os
import re
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SW = (ROOT / "web" / "sw.js").read_text(encoding="utf-8")
CACHE_NAME = re.search(r'const CACHE = "([^"]+)";', SW).group(1)
APP_SHELL = """<!doctype html><html><head><meta name=\"carme-app-shell\" content=\"1\"><title>Carme shell</title></head><body><main>Carme shell</main></body></html>"""
ACCESS_LOGIN = """<!doctype html><html><head><title>Access login</title></head><body><main>Access login HTML</main><script>window.__accessLogin = true;</script></body></html>"""
MANIFEST = '{"name":"Carme","start_url":"/"}'
ICON_192 = (ROOT / "web" / "icon-192.png").read_bytes()
APPLE_ICON = (ROOT / "web" / "apple-touch-icon.png").read_bytes()
FAVICON = (ROOT / "web" / "favicon.png").read_bytes()


class ReviewServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address):
        super().__init__(address, ReviewHandler)
        self.mode = "good"


class ReviewHandler(http.server.BaseHTTPRequestHandler):
    server: ReviewServer

    def send_body(self, body: str | bytes, content_type: str, status: int = 200) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/sw.js":
            self.send_body(SW, "application/javascript; charset=utf-8")
            return
        if path == "/":
            if self.server.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/access-login")
                self.end_headers()
            elif self.server.mode in {"login200", "sse-login"}:
                self.send_body(ACCESS_LOGIN, "text/html; charset=utf-8")
            else:
                self.send_body(APP_SHELL, "text/html; charset=utf-8")
            return
        if path == "/access-login":
            self.send_body(ACCESS_LOGIN, "text/html; charset=utf-8")
            return
        if path == "/manifest.webmanifest":
            self.send_body(MANIFEST, "application/manifest+json")
            return
        if path == "/icon-192.png":
            self.send_body(ICON_192, "image/png")
            return
        if path == "/apple-touch-icon.png":
            self.send_body(APPLE_ICON, "image/png")
            return
        if path == "/favicon.png":
            self.send_body(FAVICON, "image/png")
            return
        if path in {"/api/ping", "/api/conversations/c/attachments/f/download", "/api/events"}:
            if path == "/api/events" and self.server.mode == "sse-login":
                self.send_body(ACCESS_LOGIN, "text/html; charset=utf-8")
                return
            self.send_response(302)
            self.send_header("Location", "https://access.example.invalid/login")
            self.end_headers()
            return
        self.send_body("not found", "text/plain; charset=utf-8", 404)

    def log_message(self, *_args):
        pass


async def run() -> int:
    if os.getenv("CARME_CLOUDFLARE_BROWSER_TEST") != "1":
        print("SKIP: set CARME_CLOUDFLARE_BROWSER_TEST=1 to run isolated Chromium checks")
        return 0
    from playwright.async_api import async_playwright

    server = ReviewServer(("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            server.shutdown()
            print(f"BLOCKED: Chromium 无法在当前执行环境启动：{type(exc).__name__}: {str(exc).splitlines()[0]}")
            return 2
        context = await browser.new_context()
        page = await context.new_page()

        async def clear_service_worker_state() -> None:
            await page.evaluate("""async () => {
              for (const registration of await navigator.serviceWorker.getRegistrations()) await registration.unregister();
              for (const key of await caches.keys()) await caches.delete(key);
            }""")

        async def register_service_worker() -> dict:
            return await page.evaluate("""async (cacheName) => {
              try {
                const registration = await navigator.serviceWorker.register('/sw.js', {updateViaCache: 'none'});
                await new Promise(resolve => setTimeout(resolve, 500));
                const cache = await caches.open(cacheName);
                return {
                  active: Boolean(registration.active),
                  state: registration.active?.state || registration.waiting?.state || registration.installing?.state || 'none',
                  keys: (await cache.keys()).map(request => new URL(request.url).pathname),
                };
              } catch (error) {
                return {error: String(error)};
              }
            }""", CACHE_NAME)

        async def cached_shell() -> str:
            return await page.evaluate("""async () => {
              const response = await caches.match('/');
              return response ? await response.text() : '';
            }""")

        try:
            server.mode = "login200"
            await page.goto(base, wait_until="domcontentloaded")
            await clear_service_worker_state()
            failed = await register_service_worker()
            check("200 Access 登录 HTML 不安装/写入 shell 缓存", "/" not in failed.get("keys", []), str(failed))

            server.mode = "redirect"
            await page.goto(base, wait_until="domcontentloaded")
            await clear_service_worker_state()
            failed = await register_service_worker()
            check("302 → Access 登录不安装/写入 shell 缓存", "/" not in failed.get("keys", []), str(failed))

            server.mode = "good"
            await page.goto(base, wait_until="domcontentloaded")
            await clear_service_worker_state()
            installed = await register_service_worker()
            shell = await cached_shell()
            check("真实 Carme shell 可安装并缓存", "/" in installed.get("keys", []) and "carme-app-shell" in shell, str(installed))

            server.mode = "redirect"
            await page.goto(base, wait_until="domcontentloaded")
            preserved = await cached_shell()
            check("后续 302 登录页不会覆盖原 shell", "carme-app-shell" in preserved and "Access login HTML" not in preserved)

            server.mode = "login200"
            await page.goto(base, wait_until="domcontentloaded")
            preserved = await cached_shell()
            check("后续 200 登录 HTML 不会覆盖原 shell", "carme-app-shell" in preserved and "Access login HTML" not in preserved)

            redirect_results = await page.evaluate("""async () => {
              const result = {};
              for (const path of ['/api/ping', '/api/conversations/c/attachments/f/download']) {
                try {
                  const response = await fetch(path, {redirect: 'manual', credentials: 'same-origin'});
                  result[path] = {type: response.type, status: response.status};
                } catch (error) {
                  result[path] = {error: String(error)};
                }
              }
              return result;
            }""")
            redirect_ok = all(
                item.get("type") == "opaqueredirect" or item.get("status") == 0
                for item in redirect_results.values()
            )
            check("API 与附件跨域 302 以可识别的手动重定向返回", redirect_ok, str(redirect_results))

            server.mode = "sse-login"
            sse_error = await page.evaluate("""async () => await new Promise(resolve => {
              const source = new EventSource('/api/events');
              const timer = setTimeout(() => { source.close(); resolve(false); }, 2500);
              source.onerror = () => { clearTimeout(timer); source.close(); resolve(true); };
            })""")
            check("SSE 收到 Access HTML 后触发可恢复错误", bool(sse_error))

            server.mode = "good"
            await context.set_offline(True)
            await page.goto(base, wait_until="domcontentloaded", timeout=5000)
            offline_body = await page.locator("body").inner_text()
            check("断网导航回退到已缓存 Carme shell", "Carme shell" in offline_body)
            await context.set_offline(False)

            app_source = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
            check("应用提供可操作的重新打开保护入口", "重新打开保护入口" in app_source and "reopenAccessEntry" in app_source)
        finally:
            await context.set_offline(False)
            await context.close()
            await browser.close()
            server.shutdown()

    passed = sum(ok for _, ok, _ in checks)
    print(f"通过 {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
