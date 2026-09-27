"""浏览器会话 —— 一个身份一个常驻浏览器，一个任务一个标签页。

关键设计 1：**持久化用户目录**。
用 launch_persistent_context 而不是普通 launch，cookie / localStorage / 登录态
全部落在磁盘上。这样你登录一次，之后所有任务复用，不用反复登录 ——
这是「代操作」能不能日常用的分水岭。

关键设计 2：**按 ref 操作，不按选择器**。
快照时给元素打了 data-carme-ref，这里就靠它精确定位。
模型永远不需要写 CSS 选择器。

关键设计 3：**当前页属于任务，不属于浏览器**。
远端那个 Chrome 是你日常在用的浏览器：里面既有你自己的标签页，也有几个
Bot 各自的标签页。所以「当前页」不能挂在会话上 —— 挂上去就会出现两个问题：

    1. 两个任务同时动浏览器时会互相把对方的页面顶掉（A 打开页面，
       B 一动，A 的快照就指向 B 的页面了）；
    2. list_pages 会把你自己开着的私人标签页念给模型听。

这里的做法是把页面按 owner（一个任务）记账：

    _owned[owner] = [该任务的标签页...]，最后一个元素是它的「当前页」

- 认领（claim）与归还（release）都按 owner 记账，标签页归还后留在原地，
  下一次调用还是同一个页面，页面上的进度不会丢。
- 你自己原有的标签页在建会话那一刻就被标记成「不是我们的」，永远不进
  owner 列表 —— 模型既看不到，也关不掉。
- 我们自己的标签页总数有上限（max_pages）。超了就先关掉最久没用、且当时
  没被占用的那个：8GB 的机器上，内存比页面连续性更值钱。
"""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import socket
import subprocess
import time
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .snapshot import SNAPSHOT_JS, compact_snapshot, format_snapshot

log = logging.getLogger("carme.browser")


class BrowserError(RuntimeError):
    pass


# 让自动化特征不那么明显。不是要对抗风控，只是避免被误判成爬虫而影响正常使用。
STEALTH_JS = r"""
// webdriver 必须定义在 Navigator.prototype 上，不能当成 navigator 的自有属性：
// bot.sannysoft.com 的 WebDriver (New) 判定是
//   navigator.webdriver || _.has(navigator, "webdriver")  → failed
// 也就是说「navigator 上有这个自有属性」本身就失败。真实（非自动化）Chrome 的这个
// getter 在 Navigator.prototype 上、返回 false，所以两项都过。
try {
  Object.defineProperty(Navigator.prototype, 'webdriver',
                        { get: () => false, configurable: true });
} catch (e) { /* 原生属性不可重定义就别造自有属性，那反而会被判失败 */ }
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'], configurable: true });
(() => {
  // navigator.plugins 必须是一个真正的 PluginArray（5 个内置 PDF 插件），
  // 而不是 [1,2,3,4,5] 这种数组 —— 后者 instance of PluginArray 为 false，
  // 是检测器一眼就能看穿的造假。真实 Chrome：Array.isArray(plugins) === false。
  try {
    const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                   'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
    const plugins = { length: names.length, refresh: () => {} };
    for (let i = 0; i < names.length; i += 1) {
      const entry = { name: names[i], filename: 'internal-pdf-viewer',
                      description: 'Portable Document Format', length: 2 };
      if (typeof Plugin !== 'undefined') Object.setPrototypeOf(entry, Plugin.prototype);
      plugins[i] = entry;
      plugins[names[i]] = entry;
    }
    plugins.item = (i) => plugins[i] || null;
    plugins.namedItem = (n) => plugins[n] || null;
    if (typeof PluginArray !== 'undefined') Object.setPrototypeOf(plugins, PluginArray.prototype);

    const mimeTypes = { length: 2 };
    mimeTypes[0] = { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' };
    mimeTypes[1] = { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' };
    mimeTypes.item = (i) => mimeTypes[i] || null;
    mimeTypes.namedItem = (n) => mimeTypes[n] || null;
    if (typeof MimeTypeArray !== 'undefined') Object.setPrototypeOf(mimeTypes, MimeTypeArray.prototype);

    Object.defineProperty(Navigator.prototype, 'plugins',
                          { get: () => plugins, configurable: true });
    Object.defineProperty(Navigator.prototype, 'mimeTypes',
                          { get: () => mimeTypes, configurable: true });
  } catch (e) { /* 加固失败也要让页面正常跑 */ }
})();
window.chrome = window.chrome || { runtime: {} };
"""

# 容器里的 Chromium 是 --network=none，所有请求都由 Control 中继 fulfill。
# 但被 fulfill 的 3xx，Chromium 的跟跳请求会绕过 route 拦截去走真实网络栈，
# 在没有网络接口的容器里必然 ERR_INTERNET_DISCONNECTED（页面停在 chrome-error://）。
# 所以重定向一律在中继层跟到底，只把最终响应交给 Chromium。
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
MAX_RELAY_REDIRECTS = 6


_UA_VERSION: dict[str, str] = {}


def browser_version(executable: str) -> str:
    """浏览器自己报的版本号，用来拼一个不自相矛盾的 UA。"""
    if executable not in _UA_VERSION:
        version = ""
        try:
            output = subprocess.run([executable, "--version"], capture_output=True,
                                    text=True, timeout=15).stdout or ""
            found = re.search(r"\d+\.\d+\.\d+\.\d+", output)
            version = found.group(0) if found else ""
        except (OSError, subprocess.SubprocessError):
            version = ""
        if not version:
            log.warning("读不到 Chromium 版本，UA 先退回默认版本号：%s", executable)
            version = "131.0.0.0"
        _UA_VERSION[executable] = version
    return _UA_VERSION[executable]


def client_platform() -> dict[str, str]:
    """UA 里的平台片段 + 客户端提示里的平台 / 架构，两者必须同源。

    容器是 Linux aarch64：如果 UA 写 "Linux x86_64" 而 userAgentMetadata.architecture
    写 "arm"（或反过来），就等于新造了一个自相矛盾 —— 那比不加固更糟。
    """
    machine = platform.machine().lower()
    if platform.system() == "Darwin":
        return {"ua": "Macintosh; Intel Mac OS X 10_15_7", "platform": "macOS",
                "platform_version": "14.0.0",
                "architecture": "arm" if machine in {"arm64", "aarch64"} else "x86"}
    if machine in {"aarch64", "arm64"}:
        return {"ua": "X11; Linux aarch64", "platform": "Linux",
                "platform_version": "6.8.0", "architecture": "arm"}
    return {"ua": "X11; Linux x86_64", "platform": "Linux",
            "platform_version": "6.8.0", "architecture": "x86"}


def advertising_user_agent(executable: str) -> str:
    """无头运行时对外报的 UA —— 普通 Chrome，不带 HeadlessChrome。

    无头 Chromium 默认把自己报成 "HeadlessChrome"。小红书这类站点的风控只凭这个
    UA 就判风险：同一个出口 IP 下，普通 Chrome UA 能正常打开，HeadlessChrome 会被
    重定向到 error_code=300012「IP 存在风险，请切换可靠网络环境后重试」。出口本来
    就是机房 IP，再顶着 HeadlessChrome 等于自报家门。版本取浏览器自己报的那个，
    免得和 sec-ch-ua 对不上。
    """
    return ("Mozilla/5.0 (" + client_platform()["ua"] + ") AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{browser_version(executable)} Safari/537.36")


def client_hints(user_agent: str) -> dict[str, Any]:
    """按 UA 拼一份 CDP 的 userAgentMetadata，用来同时改掉 sec-ch-ua 与 userAgentData。

    Chromium 的客户端提示是网络栈自己生成的，跟启动时给的 UA 字符串各管各的：无头壳会
    把 "HeadlessChrome" 一直报下去，于是「UA 说 Chrome、sec-ch-ua 说 Headless」这种
    自相矛盾反而比单纯的无头更显眼。只改 UA 字符串修不了它 —— 必须走
    Emulation.setUserAgentOverride 并带上 userAgentMetadata，一次改掉两处。
    """
    found = re.search(r"Chrome/(\d+(?:\.\d+)*)", user_agent)
    full = found.group(1) if found else "131.0.0.0"
    major = full.split(".")[0]
    where = client_platform()
    return {
        "userAgent": user_agent,
        "userAgentMetadata": {
            "brands": [{"brand": "Google Chrome", "version": major},
                       {"brand": "Chromium", "version": major},
                       {"brand": "Not_A Brand", "version": "24"}],
            "fullVersionList": [{"brand": "Google Chrome", "version": full},
                                {"brand": "Chromium", "version": full},
                                {"brand": "Not_A Brand", "version": "24.0.0.0"}],
            "fullVersion": full,
            "platform": where["platform"],
            "platformVersion": where["platform_version"],
            "architecture": where["architecture"],
            "bitness": "64",
            "model": "",
            "mobile": False,
            "wow64": False,
        },
    }


class BrowserSession:
    def __init__(self, profile: str, settings: dict[str, Any], root: Path) -> None:
        self.profile = profile
        self.settings = settings
        self.root = root
        self.headless = bool(settings.get("headless", True))
        self.action_timeout = int(settings.get("action_timeout_ms", 20000))
        self.nav_timeout = int(settings.get("navigation_timeout_ms", 120000))
        self._route_error = ""  # Docker 中继最近一次被拒请求的原因，用于导航失败时的自诊断
        self._relayed = 0  # 已中继的请求数，用于超时诊断
        self._last_relay = ""
        self._route_seen = 0  # route 回调收到的请求数（含未中继就失败的）
        self._route_fail = 0
        self._redirects = 0  # 中继层已跟随的重定向跳数（容器里 Chromium 自己跟不了）
        self._timeline: list[str] = []
        # 我们自己最多留几个标签页。远端 Chrome 是常驻的，不设上限会一直涨。
        self.max_pages = max(1, int(settings.get("max_pages", 3)))

        self._playwright = None
        self._context = None
        self._page = None
        self._browser = None
        self._tunnel = None
        self.node = settings.get("node")
        # 客户端提示覆盖（sec-ch-ua / navigator.userAgentData）。见 client_hints()。
        self._hints: dict[str, Any] | None = None
        self._hinted: set[int] = set()
        self._cdp: list[Any] = []
        self.last_used = time.time()

        # ---------------- 标签页归属 ----------------
        # 一个 owner 对应一个任务的浏览现场；_owned[owner] 的最后一个元素是它的当前页。
        # 远端 Chrome 是账号级共享的：用户自己的标签页永远不进 _owned，也不列给模型。
        self._owned: dict[str, list[Any]] = {}
        self._seen: set[int] = set()      # 已认领过的页面 id（含启动时就存在的用户标签页）
        self._busy: set[str] = set()      # 正在被工具调用占用的 owner
        self._fresh: list[Any] = []       # 我们自己开的、当前没有主人的页面（可复用）
        self._created: list[Any] = []     # 本会话自己开的标签页（回收时只关这些）
        self._used_at: dict[int, float] = {}

    # ---------------- 生命周期 ----------------

    @property
    def started(self) -> bool:
        return self._context is not None and self._page is not None

    async def start(self, *, headless: bool | None = None) -> None:
        if self.started:
            return
        if self.node is not None and not (self.node.get("host") and self.node.get("user")):
            raise BrowserError("Bot 的执行电脑尚未配置；不会在后端 Mac 打开浏览器")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError(
                "浏览器代操作需要 playwright。安装：\n"
                "  ./.venv/bin/pip install playwright\n"
                "  ./.venv/bin/python -m playwright install chromium"
            ) from exc

        if self.node is not None:
            try:
                endpoint = await self._start_tunnel()
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=self.nav_timeout)
                contexts = self._browser.contexts
                if not contexts:
                    raise BrowserError("远程 Chrome 没有可用的持久化浏览器上下文")
                self._context = contexts[0]
                self._context.set_default_timeout(self.action_timeout)
                self._context.set_default_navigation_timeout(self.nav_timeout)
                self._bind_initial_page()
                return
            except BaseException as exc:
                await self.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BrowserError(f"执行电脑的 Chrome CDP 连接失败：{exc}") from exc

        user_dir = Path(self.settings.get("user_data_dir", f"./data/browser/{self.profile}"))
        if not user_dir.is_absolute():
            user_dir = self.root / user_dir
        user_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()

        viewport = self.settings.get("viewport") or {"width": 1366, "height": 900}
        launch_args = ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"]
        relay = self.settings.get('docker_relay')
        desktop = self.settings.get('desktop_runtime') is True
        if desktop:
            launch_args += ['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222', '--force-renderer-accessibility']
        if relay:
            launch_args += ['--disable-background-networking', '--disable-quic', '--disable-sync']
        if not (headless if headless is not None else self.headless):
            launch_args.append("--start-maximized")
        # 无头运行时别顶着 HeadlessChrome 出门（会被风控直接判风险）。
        # settings 里给了 user_agent 就以它为准。
        user_agent = self.settings.get("user_agent") or ""
        if not user_agent and (headless if headless is not None else self.headless):
            user_agent = advertising_user_agent(self._playwright.chromium.executable_path)


        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_dir),
                headless=headless if headless is not None else self.headless,
                user_agent=user_agent or None,
                channel=self.settings.get("channel") or None,
                executable_path='/software/chrome/chrome' if desktop else None,
                viewport=viewport,
                locale=self.settings.get("locale", "zh-CN"),
                timezone_id=self.settings.get("timezone", "Asia/Shanghai"),
                args=launch_args,
                ignore_https_errors=not bool(relay),
                chromium_sandbox=bool(relay),
                service_workers='block' if relay else 'allow',
                accept_downloads=False if relay else True,
            )
        except Exception as exc:  # noqa: BLE001
            await self.close()
            raise BrowserError(
                f"浏览器启动失败：{exc}\n"
                "常见原因：Chromium 没装（跑 ./.venv/bin/python -m playwright install chromium），"
                "或者用户目录被另一个进程占用（同一 profile 不能开两个）。"
            ) from exc
        # 客户端提示要在这里、任何导航之前装好：它按页面生效，装晚了首页请求就漏了。
        if user_agent:
            await self._install_client_hints(user_agent)


        if relay:
            import base64
            import contextlib
            from ..docker_browser import MAX_UPLOAD
            slots = asyncio.Semaphore(24)
            async def relay_follow(request):
                """一次请求经中继取回；3xx 在中继这层跟到底，不交给 Chromium。

                容器里 Chromium 是 --network=none：被 fulfill 的 3xx 会让它去走真实
                网络栈跟跳，结果必然是 ERR_INTERNET_DISCONNECTED，页面停在
                chrome-error://（实测复现）。所以重定向由中继自己跟进，每一跳都重新
                过 Control 的域名 / 公网 IP / 预算检查 —— 语义等价于浏览器自己跟跳，
                但不会漏出 route 拦截。

                跨主机跳转丢掉 cookie / authorization：Chromium 只为原 URL 决定了要带
                哪些凭据，跟着开放重定向把身份带到别的域就是凭据泄露。
                """
                method, url = request.method, request.url
                raw = request.post_data_buffer or b''
                if len(raw) > MAX_UPLOAD:
                    raise ValueError('browser_upload_limit')
                headers = await request.all_headers()
                body = base64.b64encode(raw).decode()
                cookies: list[str] = []

                def _send_cookies(hop: list[str]) -> None:
                    """同一主机继续跟跳时，把本跳的 Set-Cookie 带上。

                    真实浏览器的 cookie jar 就是这么做的；不这么做，
                    「GET / → 302 下发会话 cookie → 目标页」这类跳转会丢会话。
                    """
                    pairs = [item.split(';', 1)[0].strip() for item in hop]
                    pairs = [item for item in pairs if '=' in item]
                    if not pairs:
                        return
                    key = 'Cookie' if 'Cookie' in headers else 'cookie'
                    merged = '; '.join(pairs)
                    headers[key] = (headers[key] + '; ' + merged) if headers.get(key) else merged

                for _ in range(MAX_RELAY_REDIRECTS + 1):
                    result = await relay('browser_fetch', {'url': url, 'method': method,
                                                           'headers': headers, 'body': body})
                    if 'denied' in result:
                        raise ValueError(result['denied'])
                    # 浏览器跟跳时每一跳的 Set-Cookie 都会生效，这里一并收集（含末跳）。
                    hop = [str(v) for k, v in result['headers'] if k.lower() == 'set-cookie']
                    cookies += hop
                    status = int(result['status'])
                    location = next((str(v).strip() for k, v in result['headers']
                                     if k.lower() == 'location'), '')
                    if status not in REDIRECT_STATUS or not location:
                        return status, result['headers'], result['body'], cookies
                    followed = urljoin(url, location)
                    if urlsplit(followed).scheme not in {'http', 'https'}:
                        raise ValueError('browser_redirect_scheme_denied')
                    same_host = urlsplit(followed).netloc.lower() == urlsplit(url).netloc.lower()
                    if not same_host:
                        # 跨主机跳转丢掉凭据：Chromium 只为原 URL 决定了带哪些，跟着开放
                        # 重定向把身份带到别的域就是凭据泄露。
                        headers = {k: v for k, v in headers.items()
                                   if k.lower() not in {'cookie', 'authorization'}}
                    # 301/302/303 按规范降级成 GET 且不带 body；307/308 保留原方法与 body。
                    if status == 303 or (status in {301, 302} and method not in {'GET', 'HEAD'}):
                        method, body = 'GET', ''
                        headers = {k: v for k, v in headers.items()
                                   if k.lower() not in {'content-length', 'content-type'}}
                    if same_host:
                        _send_cookies(hop)
                    url = followed
                    self._redirects += 1
                raise ValueError('browser_redirect_limit')

            async def route(request_route):
                async with slots:
                    self._route_seen += 1
                    try:
                        status, raw_headers, raw_body, cookies = await relay_follow(request_route.request)
                        headers = {}
                        for key, value in raw_headers:
                            # set-cookie 按 CDP 约定用换行分隔；其它头若带换行（重复头拼接过）会让
                            # Chromium 的 fulfill 永久挂起 —— 只保留第一个并清掉控制字符。
                            if key.lower() == 'set-cookie':
                                headers[key] = headers[key] + '\n' + value if key in headers else value
                            elif key not in headers:
                                headers[key] = re.sub(r'[\r\n]+', ' ', str(value))
                        if cookies:
                            headers['set-cookie'] = '\n'.join(cookies)
                        await request_route.fulfill(status=status, headers=headers,
                            body=base64.b64decode(raw_body, validate=True))
                        self._relayed += 1
                        self._last_relay = request_route.request.url[:160]
                    except Exception as exc:  # noqa: BLE001
                        self._route_fail += 1
                        self._route_error = (f"最近被拒请求 {request_route.request.url[:160]}："
                                             f"{type(exc).__name__}: {exc}")[:400]
                        with contextlib.suppress(Exception):
                            await request_route.abort('blockedbyclient')
            await self._context.route('**/*', route)
            await self._context.route_web_socket('**/*', lambda ws: ws.close())

        if self.settings.get("stealth", True):
            await self._context.add_init_script(STEALTH_JS)

        self._context.set_default_timeout(self.action_timeout)
        self._context.set_default_navigation_timeout(self.nav_timeout)
        await self._ensure_initial_page()
        log.info("浏览器已启动 profile=%s headless=%s", self.profile, self.headless)

    async def _install_client_hints(self, user_agent: str) -> None:
        """给会话里每个页面下发一次 UA + 客户端提示覆盖，之后新开的页面自动跟上。

        CDP session 故意不 detach：覆盖是挂在会话上的，detach 会把覆盖一起收走。
        """
        self._hints = client_hints(user_agent)
        self._context.on("page", lambda page: asyncio.create_task(self._apply_client_hints(page)))
        for page in list(self._context.pages):
            await self._apply_client_hints(page)

    async def _apply_client_hints(self, page) -> None:
        """按 page 幂等；失败只记一条日志，绝不因为指纹加固而让浏览失败。"""
        if not self._hints or page is None or id(page) in self._hinted:
            return
        self._hinted.add(id(page))
        try:
            session = await self._context.new_cdp_session(page)
            await session.send("Emulation.setUserAgentOverride", dict(self._hints))
            self._cdp.append(session)
        except Exception as exc:  # noqa: BLE001
            self._hinted.discard(id(page))
            log.warning("客户端提示覆盖失败，这一页用浏览器默认值：%s", exc)


    def _bind_initial_page(self) -> None:
        """认清「哪些标签页是我们的」。

        本机模式：整个浏览器都是我们开的，初始页可以给任务用。
        远端模式：初始页是用户自己开着的，只登记、不认领 —— 模型看不到它。
        """
        pages = list(self._context.pages)
        if pages:
            for page in pages:
                self._seen.add(id(page))
            self._page = pages[0]
            if self.node is None:
                self._fresh.append(self._page)
            return
        # 一个标签页都没有（用户把窗口全关了）：我们开一个，它当然是我们的
        self._page = None  # 先置空，new_page 在 start 的 async 上下文里做
        raise BrowserError(
            "浏览器里一个标签页都没有。请先手动打开一个页面（或重开浏览器）再让 Bot 操作。"
        )

    async def _ensure_initial_page(self) -> None:
        """start 期不方便 await new_page 时的兜底：现场开一个我们自己的页面。"""
        if self._context.pages:
            self._bind_initial_page()
            return
        page = await self._context.new_page()
        self._page = page
        self._seen.add(id(page))
        self._created.append(page)
        self._fresh.append(page)

    async def _start_tunnel(self) -> str:
        from ..sandbox.remote import ssh_args
        from ..security import child_env
        browser = self.node.get("browser") or {}
        profiles = browser.get("profiles") or {}
        if self.profile != "default" and self.profile not in profiles:
            raise BrowserError(f"执行电脑尚未为浏览器身份 {self.profile} 配置独立 CDP 端口")
        target = {**browser, **(profiles.get(self.profile) or {})}
        host = target.get("cdp_host", "127.0.0.1")
        port = int(target.get("cdp_port", 9222))
        if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535:
            raise BrowserError("远程 CDP 只允许经 SSH 连接执行电脑的回环地址和有效端口")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            local_port = reservation.getsockname()[1]
        args = ssh_args(self.node)
        destination = args.pop()
        forward_host = f"[{host}]" if ":" in host else host
        self._tunnel_home = tempfile.TemporaryDirectory(prefix="carme-cdp-ssh-", dir="/tmp")
        self._tunnel = await asyncio.create_subprocess_exec(
            *args, "-o", "ExitOnForwardFailure=yes", "-N", "-L",
            f"127.0.0.1:{local_port}:{forward_host}:{port}", destination,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            env=child_env(self._tunnel_home.name), start_new_session=True)
        deadline = time.monotonic() + int(self.node.get("connect_timeout", 10)) + 2
        while time.monotonic() < deadline:
            if self._tunnel.returncode is not None:
                _, error = await self._tunnel.communicate()
                raise BrowserError(error.decode(errors="replace").strip()[:300] or "SSH 隧道已退出")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", local_port)
                writer.close()
                await writer.wait_closed()
                return f"http://127.0.0.1:{local_port}"
            except OSError:
                await asyncio.sleep(0.05)
        raise BrowserError("连接执行电脑的 SSH 隧道超时")

    async def close(self) -> None:
        # CDP 连接的是用户常驻 Chrome，禁止 close context/browser 杀掉远端现场。
        for closer in (
            lambda: self._context.close() if self._context and self.node is None else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                result = closer()
                if result is not None:
                    await result
            except Exception:  # noqa: BLE001
                pass
        self._context = None
        self._playwright = None
        self._page = None
        self._browser = None
        self._owned.clear()
        self._busy.clear()
        self._fresh.clear()
        self._created.clear()
        self._seen.clear()
        self._used_at.clear()
        if self._tunnel is not None:
            from ..engines import _stop_process_group
            await _stop_process_group(self._tunnel)
            self._tunnel = None
        if getattr(self, "_tunnel_home", None) is not None:
            self._tunnel_home.cleanup()
            self._tunnel_home = None

    @property
    def page(self):
        """会话自带的默认页。

        只给健康检查和旧调用用；工具层要走 claim()，拿到的才是「本任务自己的页」。
        """
        if self._page is None:
            raise BrowserError("浏览器还没启动")
        return self._page

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_used

    @property
    def busy(self) -> bool:
        """还有人占着这个会话（工具调用正在进行中）。"""
        return bool(self._busy)

    @property
    def owned_pages(self) -> int:
        return sum(len([p for p in pages if not p.is_closed()]) for pages in self._owned.values())

    # ---------------- 标签页归属 ----------------

    async def claim(self, owner: str) -> "BrowserPage":
        """把「本任务自己的那个页面」交给调用方。

        同一个 owner 反复调用拿到的是同一个页面（它自己的当前页），
        所以 web_open → web_click → web_snapshot 之间页面状态是连续的。
        """
        self.touch()
        if self._context is None:
            await self.start()
        if not self._context.pages:
            await self._ensure_initial_page()

        pages = [p for p in self._owned.get(owner, []) if not p.is_closed()]
        if not pages:
            pages.append(await self._next_page())
        if pages[-1].is_closed():  # 极少数：页面在我们眼皮底下被关掉
            pages = [p for p in pages if not p.is_closed()] or [await self._next_page()]
        self._owned[owner] = pages
        self._busy.add(owner)
        self._used_at[id(pages[-1])] = time.time()
        return BrowserPage(self, pages[-1], owner)

    def release(self, owner: str) -> None:
        """归还页面：只解除「占用」，标签页留在原地。"""
        self._busy.discard(owner)
        self.touch()

    async def _next_page(self):
        """给一个新 owner 分配页面：先用闲置的，其次新建，最后淘汰最旧的闲置页。"""
        while self._fresh:
            page = self._fresh.pop()
            if not page.is_closed():
                self._used_at[id(page)] = time.time()
                return page
        live = [p for p in self._created if not p.is_closed()]
        if len(live) < self.max_pages:
            page = await self._context.new_page()
            self._created.append(page)
            self._seen.add(id(page))
            self._used_at[id(page)] = time.time()
            return page
        if await self._evict_idle_page():
            page = await self._context.new_page()
            self._created.append(page)
            self._seen.add(id(page))
            self._used_at[id(page)] = time.time()
            return page
        raise BrowserError(
            f"浏览器标签页额度用满了（max_pages={self.max_pages}），而且这些页面都有人在用。"
            "等别的任务用完再试，或在 config/browser.yaml 里调大 safety.max_pages。"
        )

    async def _evict_idle_page(self) -> bool:
        """关掉最久没用、且当前没被占用的那个自有页面。"""
        candidates: list[tuple[float, str, Any]] = []
        for owner, pages in self._owned.items():
            if owner in self._busy:
                continue
            for page in pages:
                if not page.is_closed():
                    candidates.append((self._used_at.get(id(page), 0.0), owner, page))
        if not candidates:
            return False
        candidates.sort(key=lambda item: item[0])
        _, owner, page = candidates[0]
        self._owned[owner] = [p for p in self._owned.get(owner, []) if p is not page]
        self._created = [p for p in self._created if p is not page]
        self._seen.discard(id(page))
        self._used_at.pop(id(page), None)
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("标签页额度用满，关掉最久没用的自有页面 owner=%s", owner)
        return True

    async def _adopt_new_pages(self, owner: str) -> None:
        """把这次操作中新冒出来的页面收进 owner 名下。

        为什么需要：登录流程经常弹新窗口（OAuth 回调、扫码确认页），
        如果不管它，模型会以为「点了没反应」，然后在原页面上瞎试。
        新页面插在列表开头，不抢当前页 —— 看不看由模型决定。
        """
        if not owner or self._context is None:
            return
        for page in list(self._context.pages):
            if id(page) in self._seen:
                continue
            self._seen.add(id(page))
            self._created.append(page)
            self._used_at[id(page)] = time.time()
            self._owned.setdefault(owner, []).insert(0, page)
            log.info("收下一个新标签页 owner=%s url=%s", owner, page.url[:120])

    async def close_owned_pages(self) -> int:
        """关掉本会话自己开的所有标签页（回收会话前用）。

        绝不动用户自己的标签页 —— 那些从建会话起就没进过 _created。
        """
        closed = 0
        for page in [p for p in self._created if not p.is_closed()]:
            try:
                await page.close()
                closed += 1
            except Exception:  # noqa: BLE001
                pass
        self._created.clear()
        self._fresh.clear()
        self._owned.clear()
        self._used_at.clear()
        return closed

    # ---------------- 快照 ----------------

    async def snapshot(self, *, include_text: bool = True, page=None) -> dict[str, Any]:
        self.touch()
        try:
            return await (page or self.page).evaluate(SNAPSHOT_JS)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"读取页面失败：{exc}") from exc

    async def snapshot_text(self, *, include_text: bool = True, max_text: int = 6000, page=None) -> str:
        return format_snapshot(
            await self.snapshot(include_text=include_text, page=page),
            include_text=include_text,
            max_text=max_text,
        )

    # ---------------- 动作 ----------------

    async def goto(self, url: str, *, wait_until: str = "domcontentloaded", page=None,
                   owner: str = "") -> dict[str, Any]:
        self.touch()
        target = page or self.page
        # 新标签页可能是在我们下发覆盖之前就被创建并立刻导航的，这里补一次（幂等）。
        await self._apply_client_hints(target)
        # 只有「看起来像裸域名」的才补协议。
        # about: / data: / file: 这些是合法 URL，补成 https:// 反而会导航失败。
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url
        if self.settings.get('docker_relay'):
            from ..docker_browser import web_url
            url = web_url(url)
        self._route_error = ""
        warning = ""
        try:
            await target.goto(url, wait_until=wait_until)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "TimeoutError":
                warning = ("导航未在超时前完成（页面可能仍在加载，或部分资源被拒）；"
                           + self._relay_stats())
                log.warning("导航 %s 超时：%s", url, exc)
            elif self.settings.get('docker_relay'):
                raise BrowserError(f'Docker Browser 导航失败或出口拒绝；未执行宿主回退'
                                   f'（{type(exc).__name__}: {exc}；{self._relay_stats()}）') from exc
            else:
                log.warning("导航 %s 未完全成功：%s", url, exc)
        await self._settle(page=target, owner=owner)
        try:
            async with asyncio.timeout(20):
                snap = await self.snapshot(page=target)
        except TimeoutError:
            raise BrowserError(f'Docker Browser 页面读取超时；未执行宿主回退（{self._relay_stats()}）') from None
        if warning:
            snap["warning"] = warning
        return snap

    def _relay_stats(self) -> str:
        parts = [f"收到 {self._route_seen} 个请求，已中继 {self._relayed}，被拒 {self._route_fail}"]
        if self._redirects:
            parts.append(f"中继层已跟随重定向 {self._redirects} 跳")
        if self._last_relay:
            parts.append("最近：" + self._last_relay)
        if self._route_error:
            parts.append("首个错误：" + self._route_error)
        return "；".join(parts)

    async def click(self, ref: int, *, expect_navigation: bool = False, page=None,
                    owner: str = "") -> dict[str, Any]:
        self.touch()
        target = page or self.page
        locator = self._locator(ref, page=target)
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await locator.click(timeout=self.action_timeout)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(
                f"点击 [{ref}] 失败：{exc}\n"
                "可能是元素被遮挡、已被移除，或者需要先滚动到可见区域。"
                "建议重新 web_snapshot 拿最新编号再试。"
            ) from exc
        if expect_navigation:
            with_suppress = target.wait_for_load_state("domcontentloaded", timeout=self.nav_timeout)
            try:
                await with_suppress
            except Exception:  # noqa: BLE001
                pass
        await self._settle(page=target, owner=owner)
        return await self.snapshot(page=target)

    async def type_text(self, ref: int, text: str, *, clear: bool = True,
                        submit: bool = False, page=None, owner: str = "") -> dict[str, Any]:
        self.touch()
        target = page or self.page
        locator = self._locator(ref, page=target)
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        try:
            if clear:
                await locator.fill("", timeout=self.action_timeout)
            await locator.fill(text, timeout=self.action_timeout)
        except Exception as exc:  # noqa: BLE001
            # fill 对某些富文本框无效，退化成逐字输入
            try:
                await locator.click(timeout=8000)
                if clear:
                    await target.keyboard.press("Control+A")
                    await target.keyboard.press("Delete")
                await target.keyboard.type(text, delay=25)
            except Exception as exc2:  # noqa: BLE001
                raise BrowserError(
                    f"在 [{ref}] 输入失败：{exc2}（先尝试的 fill 也失败：{exc}）"
                ) from exc2
        if submit:
            await target.keyboard.press("Enter")
        await self._settle(page=target, owner=owner)
        return await self.snapshot(page=target)

    async def press(self, key: str, *, page=None, owner: str = "") -> dict[str, Any]:
        self.touch()
        target = page or self.page
        try:
            await target.keyboard.press(key)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"按键 {key!r} 失败：{exc}") from exc
        await self._settle(page=target, owner=owner)
        return await self.snapshot(page=target)

    async def scroll(self, *, direction: str = "down", amount: int = 700, page=None) -> dict[str, Any]:
        self.touch()
        target = page or self.page
        delta = amount if direction == "down" else -amount
        try:
            await target.mouse.wheel(0, delta)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"滚动失败：{exc}") from exc
        await asyncio.sleep(0.5)
        return await self.snapshot(page=target)

    async def wait_for(self, *, seconds: float = 2.0, page=None) -> dict[str, Any]:
        await asyncio.sleep(min(max(seconds, 0.2), 30.0))
        return await self.snapshot(page=page)

    async def screenshot(self, path: Path, *, full_page: bool = False, page=None) -> Path:
        self.touch()
        target = page or self.page
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await target.screenshot(path=str(path), full_page=full_page)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"截图失败：{exc}") from exc
        return path

    async def go_back(self, *, page=None, owner: str = "") -> dict[str, Any]:
        self.touch()
        target = page or self.page
        try:
            await target.go_back(wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"后退失败：{exc}") from exc
        await self._settle(page=target, owner=owner)
        return await self.snapshot(page=target)

    # ---------------- 标签页（只认自己名下的） ----------------

    async def list_pages(self, owner: str) -> list[dict[str, Any]]:
        """只列本 owner 自己的标签页。

        不列共享 Chrome 里的全部标签页，是因为那台浏览器上也有用户自己在用的
        窗口：把它们念给模型听，既没用，也等于把用户的浏览内容送进模型上下文。
        """
        pages = [p for p in self._owned.get(owner, []) if not p.is_closed()]
        self._owned[owner] = pages
        current = pages[-1] if pages else None
        out: list[dict[str, Any]] = []
        for i, page in enumerate(pages):
            try:
                out.append(
                    {
                        "index": i,
                        "url": page.url,
                        "title": await page.title(),
                        "current": page is current,
                    }
                )
            except Exception:  # noqa: BLE001
                out.append({"index": i, "url": "", "title": "", "current": page is current})
        return out

    async def switch_page(self, owner: str, index: int) -> dict[str, Any]:
        pages = [p for p in self._owned.get(owner, []) if not p.is_closed()]
        if index < 0 or index >= len(pages):
            raise BrowserError(
                f"没有第 {index} 个标签页（本任务当前共 {len(pages)} 个）。先 web_tabs 看一眼。"
            )
        target = pages.pop(index)
        pages.append(target)  # 被选中的那个成为当前页
        self._owned[owner] = pages
        self.touch()
        try:
            await target.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        try:
            await target.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        await self._settle(page=target, owner=owner)
        return await self.snapshot(page=target)

    async def close_page(self, owner: str, index: int) -> dict[str, Any]:
        """关掉本任务多余的标签页。低配机器上这个很实在 —— 每个页面都是一份内存。"""
        pages = [p for p in self._owned.get(owner, []) if not p.is_closed()]
        if index < 0 or index >= len(pages):
            raise BrowserError(f"没有第 {index} 个标签页")
        if len(pages) == 1:
            raise BrowserError("这是你唯一的标签页，关掉就没页面可操作了")
        target = pages.pop(index)
        self._owned[owner] = pages
        self._created = [p for p in self._created if p is not target]
        self._seen.discard(id(target))
        self._used_at.pop(id(target), None)
        self.touch()
        await target.close()
        return await self.snapshot(page=pages[-1])

    # ---------------- 内部 ----------------

    def _locator(self, ref: int, *, page=None):
        """按快照编号精确定位元素。

        编号是快照那一刻打的 data-carme-ref，所以它只在页面没变化时有效。
        找不到就明确告诉模型「重新快照」，而不是让它瞎猜。
        """
        try:
            ref_int = int(ref)
        except (TypeError, ValueError) as exc:
            raise BrowserError(f"ref 必须是数字，收到的是 {ref!r}") from exc
        if ref_int < 1:
            raise BrowserError(f"ref 从 1 开始，收到 {ref_int}")

        return (page or self.page).locator(f'[data-carme-ref="{ref_int}"]')

    async def _settle(self, *, extra: float = 0.35, page=None, owner: str = "") -> None:
        """等页面安静下来再拍快照。

        SPA 点一下要几百毫秒才渲染完，不等就拍会拿到旧内容 ——
        这是这类工具最常见的「明明点了却没反应」的根因。
        """
        target = page or self.page
        try:
            await target.wait_for_load_state("networkidle", timeout=3500)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(extra)
        if owner:
            await self._adopt_new_pages(owner)

    async def element_summary(self, ref: int, *, page=None) -> dict[str, Any]:
        """查一个编号当前对应的元素信息，用于执行前的安全判断。"""
        try:
            return await (page or self.page).evaluate(
                """(ref) => {
                    const el = document.querySelector('[data-carme-ref="' + ref + '"]');
                    if (!el) return null;
                    const tag = el.tagName.toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const isInput = tag === 'input' || tag === 'textarea';
                    const label = isInput
                      ? ((type === 'submit' || type === 'button') ? (el.getAttribute('value') || '') : '')
                      : (el.innerText || el.textContent || '');
                    return {
                      tag,
                      text: (label + ' ' +
                             (el.getAttribute('aria-label') || '') + ' ' +
                             (el.getAttribute('title') || '')).replace(/\\s+/g, ' ').trim().slice(0, 200),
                      href: el.href || '',
                      type
                    };
                }""",
                int(ref),
            )
        except Exception:  # noqa: BLE001
            return None

    async def page_context(self, *, page=None) -> dict[str, str]:
        """当前页面标题与网址，用于域名策略判断。"""
        target = page or self.page
        try:
            return {"url": target.url, "title": await target.title()}
        except Exception:  # noqa: BLE001
            return {"url": "", "title": ""}


    async def login_state(self, *, page=None) -> dict[str, Any]:
        """报告两件与站点无关的事实：当前网址、cookie 集合。

        这里刻意不做「登录成功了吗」的判断 —— 那是站点知识，交给工具层按 marker
        去比。会话只负责把事实取回来。
        """
        target = page or self.page
        url = ""
        try:
            url = target.url or ""
        except Exception:  # noqa: BLE001
            url = ""
        cookies: list[str] = []
        try:
            jar = await self._context.cookies()
            cookies = sorted(f"{item.get('name')}@{item.get('domain', '')}" for item in jar)
        except Exception:  # noqa: BLE001
            cookies = []
        return {"url": url, "cookies": cookies}

class BrowserPage:
    """一个 owner 的浏览现场：会话 + 它自己的那个标签页。

    工具层拿到的是这个对象。方法名与会话完全一致，所以 tools/web.py 的写法
    不用变，但并发时两个任务各操作自己的页面，不会互相顶掉对方。
    """

    def __init__(self, session: BrowserSession, page, owner: str) -> None:
        self._session = session
        self._page = page
        self.owner = owner

    @property
    def session(self) -> BrowserSession:
        return self._session

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def closed(self) -> bool:
        return self._page.is_closed()

    async def goto(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return await self._session.goto(url, page=self._page, owner=self.owner, **kwargs)

    async def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return await self._session.snapshot(page=self._page, **kwargs)

    async def snapshot_text(self, **kwargs: Any) -> str:
        return await self._session.snapshot_text(page=self._page, **kwargs)

    async def click(self, ref: int, **kwargs: Any) -> dict[str, Any]:
        return await self._session.click(ref, page=self._page, owner=self.owner, **kwargs)

    async def type_text(self, ref: int, text: str, **kwargs: Any) -> dict[str, Any]:
        return await self._session.type_text(ref, text, page=self._page, owner=self.owner, **kwargs)

    async def press(self, key: str, **kwargs: Any) -> dict[str, Any]:
        return await self._session.press(key, page=self._page, owner=self.owner, **kwargs)

    async def scroll(self, **kwargs: Any) -> dict[str, Any]:
        return await self._session.scroll(page=self._page, **kwargs)

    async def wait_for(self, **kwargs: Any) -> dict[str, Any]:
        return await self._session.wait_for(page=self._page, **kwargs)

    async def screenshot(self, path: Path, **kwargs: Any) -> Path:
        return await self._session.screenshot(path, page=self._page, **kwargs)

    async def go_back(self, **kwargs: Any) -> dict[str, Any]:
        return await self._session.go_back(page=self._page, owner=self.owner, **kwargs)

    async def element_summary(self, ref: int) -> dict[str, Any]:
        return await self._session.element_summary(ref, page=self._page)

    async def page_context(self) -> dict[str, str]:
        return await self._session.page_context(page=self._page)

    async def login_state(self) -> dict[str, Any]:
        return await self._session.login_state(page=self._page)


def brief(snap: dict[str, Any]) -> str:
    return compact_snapshot(snap)
