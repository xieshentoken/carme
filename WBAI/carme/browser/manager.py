"""浏览器管理器 —— 负责身份隔离、并发闸门、闲置回收。

三个必须处理的问题：

1. **同一个身份不能被两个任务同时用。**
   同一个 user_data_dir 开两个 Chromium 会直接损坏配置目录。
   所以每个 profile 一把锁。

2. **浏览器很吃内存，必须限量。**
   Chromium 一个实例 200-400MB。8GB 机器上并发给 1。

3. **它会一直占着内存不放。**
   所以有闲置回收：超过 idle_close_seconds 没动过就自动关掉。
   低配机器上这个比任何调优都管用。

另外它和沙箱一样有死锁风险：主控占着浏览器 → 派活给成员 → 成员也要浏览器 → 互等。
所以同样采用「按需占用 + 等待下属时释放」的懒加载模式。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import hashlib
import json
from copy import deepcopy
import time
from pathlib import Path
from typing import Any

from .session import BrowserError, BrowserSession

log = logging.getLogger("carme.browser")


class BrowserManager:
    def __init__(self, settings: dict[str, Any], root: Path) -> None:
        self.settings = settings
        self.root = root
        self.enabled = bool(settings.get("enabled", True))
        self.profiles: dict[str, dict] = settings.get("profiles") or {}
        self.safety: dict = settings.get("safety") or {}
        self.shots: dict = settings.get("screenshots") or {}

        self._sessions: dict[str, BrowserSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._limit = max(1, int(self.safety.get("max_concurrent_browser", 1)))
        self._semaphore = asyncio.Semaphore(self._limit)
        self._reaper: asyncio.Task | None = None

    # ---------------- 配置 ----------------

    def reconfigure(self, settings: dict[str, Any]) -> None:
        """热更新配置。

        改危险词、白名单、并发上限这类参数不该需要重启进程 ——
        重启会丢掉所有活着的浏览器和登录态，代价太大。
        已经开着的会话不受影响，新参数从下一次调用开始生效。
        """
        self.settings = settings
        self.enabled = bool(settings.get("enabled", True))
        self.profiles = settings.get("profiles") or {}
        self.safety = settings.get("safety") or {}
        self.shots = settings.get("screenshots") or {}
        limit = int(self.safety.get("max_concurrent_browser", 1))
        if limit != self._limit and not any(lock.locked() for lock in self._locks.values()):
            self._limit = max(1, limit)
            self._semaphore = asyncio.Semaphore(self._limit)
        log.info("浏览器配置已热更新（并发上限 %d）", max(1, limit))

    @property
    def default_profile(self) -> str:
        return self.settings.get("default_profile") or "default"

    def profile_settings(self, name: str) -> dict:
        """把全局设置和该身份的设置合起来。"""
        base = {
            "headless": self.settings.get("headless", True),
            "action_timeout_ms": self.safety.get("action_timeout_ms", 20000),
            "navigation_timeout_ms": self.safety.get("navigation_timeout_ms", 45000),
        }
        base.update(self.profiles.get(name) or {})
        if name not in self.profiles and name != self.default_profile:
            # 允许即用即建：没登记的身份用默认目录规则
            base["user_data_dir"] = f"./data/browser/{name}"
        return base

    def known_profiles(self) -> list[str]:
        names = list(self.profiles)
        if self.default_profile not in names:
            names.insert(0, self.default_profile)
        return names

    @property
    def screenshots_dir(self) -> Path:
        raw = Path(self.shots.get("dir", "./data/screenshots"))
        return raw if raw.is_absolute() else self.root / raw

    @property
    def screenshot_full_page(self) -> bool:
        return bool(self.shots.get("full_page", False))

    # ---------------- 会话获取 ----------------

    def _lock_for(self, profile: str) -> asyncio.Lock:
        if profile not in self._locks:
            self._locks[profile] = asyncio.Lock()
        return self._locks[profile]

    def _session_key(self, profile: str, node: dict | None = None) -> str:
        if node is None:
            return profile
        version = hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()[:12]
        return f"{node.get('node_id', 'unconfigured')}:{profile}:{version}"

    def _lock_key(self, profile: str, node: dict | None = None) -> str:
        # 实体 Mac 共享桌面，不同浏览器身份也必须共用节点锁。
        return f"node:{node.get('node_id', 'unconfigured')}" if node is not None else profile

    async def _get_session(self, profile: str, *, headless: bool | None = None,
                           node: dict | None = None) -> BrowserSession:
        key = self._session_key(profile, node)
        session = self._sessions.get(key)
        if session is None:
            settings = self.profile_settings(profile)
            if node is not None:
                settings["node"] = deepcopy(node)
            session = BrowserSession(profile, settings, self.root)
            self._sessions[key] = session
        try:
            await session.start(headless=headless)
        except BaseException:
            self._sessions.pop(key, None)
            raise
        self._ensure_reaper()
        return session

    def handle(self, *, profile: str | None = None, headless: bool | None = None,
               node: dict | None = None) -> "BrowserHandle":
        return BrowserHandle(self, profile or self.default_profile, headless, node=node)

    def has_session(self, profile: str, *, node: dict | None = None) -> bool:
        """这个身份当前有没有活着的浏览器。

        用来把「你还没打开任何页面」和「打开失败」区分开 ——
        前者是模型该先 web_open，后者是要排查的问题，提示语完全不同。
        """
        session = self._sessions.get(self._session_key(profile, node))
        return session is not None and session.started

    @property
    def live_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.started)

    @property
    def live_profiles(self) -> list[str]:
        return [name for name, s in self._sessions.items() if s.started]

    async def close_profile(self, profile: str, *, node: dict | None = None) -> None:
        async with self._lock_for(self._lock_key(profile, node)):
            await self._close_key(self._session_key(profile, node))

    async def _close_key(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()
            log.info("浏览器连接已关闭 session=%s", key)

    async def close_all(self) -> None:
        for profile in list(self._sessions):
            await self._close_key(profile)
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None

    # ---------------- 闲置回收 ----------------

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        idle_limit = float(self.safety.get("idle_close_seconds", 300))
        try:
            while True:
                await asyncio.sleep(30)
                for profile, session in list(self._sessions.items()):
                    lock = self._lock_for(self._lock_key(session.profile, session.node))
                    if session.idle_seconds > idle_limit and not lock.locked():
                        log.info(
                            "浏览器闲置 %.0fs 超过上限，自动关闭 profile=%s",
                            session.idle_seconds,
                            profile,
                        )
                        async with lock:
                            await self._close_key(profile)
                if not self._sessions:
                    return
        except asyncio.CancelledError:
            return

    # ---------------- 策略检查 ----------------

    def check_domain(self, url: str) -> str | None:
        """返回 None 表示放行，返回字符串表示拒绝理由。"""
        if not url:
            return None
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if not host:
            return None

        def matches(pattern: str) -> bool:
            pattern = pattern.lower().lstrip(".")
            return host == pattern or host.endswith("." + pattern)

        for blocked in self.safety.get("blocked_domains") or []:
            if matches(blocked):
                return f"域名 {host} 在禁止访问列表里（config/browser.yaml: safety.blocked_domains）"

        allowed = self.safety.get("allowed_domains") or []
        if allowed and not any(matches(a) for a in allowed):
            return (
                f"域名 {host} 不在允许访问列表里。"
                "要放开请在 config/browser.yaml 的 safety.allowed_domains 里加上它。"
            )
        return None

    def is_read_only(self, url: str) -> bool:
        if not url:
            return False
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        for pattern in self.safety.get("read_only_domains") or []:
            pattern = pattern.lower().lstrip(".")
            if host == pattern or host.endswith("." + pattern):
                return True
        return False

    def danger_hit(self, *texts: str) -> str | None:
        """判断这段文案是不是危险操作。命中的关键词原样返回。"""
        if not self.safety.get("require_confirmation", True):
            return None
        blob = " ".join(t for t in texts if t).lower()
        if not blob:
            return None
        for pattern in self.safety.get("dangerous_patterns") or []:
            if pattern.lower() in blob:
                return pattern
        return None

    # ---------------- 健康检查 ----------------

    def prune_screenshots(self) -> int:
        """只留最近 N 张截图。

        截图是这个系统里唯一会无声堆积的东西：一次任务十几张，
        每张几百 KB，跑一周就能吃掉几个 G。低配机器上必须清。
        """
        keep = int(self.shots.get("keep", 100))
        if keep <= 0:
            return 0
        directory = self.screenshots_dir
        if not directory.exists():
            return 0
        shots = sorted(
            (p for p in directory.glob("*.png") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for stale in shots[keep:]:
            try:
                stale.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            log.info("清理了 %d 张旧截图", removed)
        return removed

    async def probe(self, *, node: dict | None = None) -> dict:
        if not self.enabled:
            return {"ok": False, "error": "浏览器代操作在 config/browser.yaml 里被关掉了（enabled: false）"}
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            return {
                "ok": False,
                "error": "没装 playwright。执行：./.venv/bin/pip install playwright && ./.venv/bin/python -m playwright install chromium",
            }
        profile = self.default_profile
        try:
            async with self.handle(profile=profile, node=node) as handle:
                session = await handle.get()
                return {
                    "ok": True,
                    "profile": profile,
                    "headless": self.profile_settings(profile).get("headless", True),
                    "profiles": self.known_profiles(),
                    "url": session.page.url,
                    "node_id": node.get("node_id") if node else None,
                }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "profile": profile, "error": str(exc)[:500]}


class BrowserHandle:
    """按需占用浏览器，等待下属时释放 —— 和沙箱同一套思路。

    为什么不能简单地 async with 一把包住整个 Agent 运行：
        主控占着唯一的浏览器 → 派活给情报员 → 情报员也要浏览器 → 互等死锁。
    """

    def __init__(self, manager: BrowserManager, profile: str, headless: bool | None = None,
                 *, node: dict | None = None) -> None:
        self._manager = manager
        self._profile = profile
        self._headless = headless
        self._node = deepcopy(node)
        self._session: BrowserSession | None = None
        self._depth = 0

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def manager(self) -> BrowserManager:
        """工具层要拿它做策略判断（危险词、域名白名单）。"""
        return self._manager

    async def get(self) -> BrowserSession:
        if self._session is None:
            semaphore = self._manager._semaphore
            await semaphore.acquire()
            lock = None
            acquired = False
            try:
                lock = self._manager._lock_for(self._manager._lock_key(self._profile, self._node))
                await lock.acquire()
                acquired = True
                self._session = await self._manager._get_session(
                    self._profile, headless=self._headless, node=self._node
                )
                # 锁由 handle 持有，release() 时归还
                self._lock = lock
                self._semaphore = semaphore
            except BaseException:
                if acquired:
                    lock.release()
                semaphore.release()
                raise
        self._session.touch()
        return self._session

    async def release(self) -> None:
        if self._session is None:
            return
        session, self._session = self._session, None
        lock = getattr(self, "_lock", None)
        if lock is not None and lock.locked():
            lock.release()
        self._lock = None
        # 会话本身不关，留在管理器里复用（登录态要保住），只归还并发额度
        session.touch()
        self._semaphore.release()

    @property
    def held(self) -> bool:
        return self._session is not None

    async def __aenter__(self) -> "BrowserHandle":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.release()
        return False


__all__ = ["BrowserManager", "BrowserHandle", "BrowserError", "BrowserSession"]
