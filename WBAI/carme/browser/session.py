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
import re
import socket
import time
from pathlib import Path
from typing import Any

from .snapshot import SNAPSHOT_JS, compact_snapshot, format_snapshot

log = logging.getLogger("carme.browser")


class BrowserError(RuntimeError):
    pass


# 让自动化特征不那么明显。不是要对抗风控，只是避免被误判成爬虫而影响正常使用。
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


class BrowserSession:
    def __init__(self, profile: str, settings: dict[str, Any], root: Path) -> None:
        self.profile = profile
        self.settings = settings
        self.root = root
        self.headless = bool(settings.get("headless", True))
        self.action_timeout = int(settings.get("action_timeout_ms", 20000))
        self.nav_timeout = int(settings.get("navigation_timeout_ms", 45000))
        # 我们自己最多留几个标签页。远端 Chrome 是常驻的，不设上限会一直涨。
        self.max_pages = max(1, int(settings.get("max_pages", 3)))

        self._playwright = None
        self._context = None
        self._page = None
        self._browser = None
        self._tunnel = None
        self.node = settings.get("node")
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
        if not (headless if headless is not None else self.headless):
            launch_args.append("--start-maximized")

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_dir),
                headless=headless if headless is not None else self.headless,
                viewport=viewport,
                locale=self.settings.get("locale", "zh-CN"),
                timezone_id=self.settings.get("timezone", "Asia/Shanghai"),
                args=launch_args,
                ignore_https_errors=True,
            )
        except Exception as exc:  # noqa: BLE001
            await self.close()
            raise BrowserError(
                f"浏览器启动失败：{exc}\n"
                "常见原因：Chromium 没装（跑 ./.venv/bin/python -m playwright install chromium），"
                "或者用户目录被另一个进程占用（同一 profile 不能开两个）。"
            ) from exc

        if self.settings.get("stealth", True):
            await self._context.add_init_script(STEALTH_JS)

        self._context.set_default_timeout(self.action_timeout)
        self._context.set_default_navigation_timeout(self.nav_timeout)
        self._bind_initial_page()
        log.info("浏览器已启动 profile=%s headless=%s", self.profile, self.headless)

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
        self._tunnel = await asyncio.create_subprocess_exec(
            *args, "-o", "ExitOnForwardFailure=yes", "-N", "-L",
            f"127.0.0.1:{local_port}:{forward_host}:{port}", destination,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
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
            if self._tunnel.returncode is None:
                self._tunnel.terminate()
                try:
                    await asyncio.wait_for(self._tunnel.communicate(), timeout=3)
                except asyncio.TimeoutError:
                    self._tunnel.kill()
                    await self._tunnel.communicate()
            self._tunnel = None

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
        # 只有「看起来像裸域名」的才补协议。
        # about: / data: / file: 这些是合法 URL，补成 https:// 反而会导航失败。
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url
        try:
            await target.goto(url, wait_until=wait_until)
        except Exception as exc:  # noqa: BLE001
            # 超时不一定是失败，页面可能已经可用了
            log.warning("导航 %s 未完全成功：%s", url, exc)
        await self._settle(page=target, owner=owner)
        return await self.snapshot(page=target)

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


def brief(snap: dict[str, Any]) -> str:
    return compact_snapshot(snap)
