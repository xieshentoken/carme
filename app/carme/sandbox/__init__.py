"""沙箱层：统一出口 + 并发闸门。

8GB 的机器上，同时开两个容器 + 一个浏览器，内存就爆了。
所以这里用一个全局信号量把「同时活着的沙箱数」卡死，
这个阀门比任何调优都管用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from copy import deepcopy
from typing import AsyncIterator

from ..config import Config
from .base import ExecResult, Sandbox, SandboxError, SandboxSpec, make_sandbox

log = logging.getLogger("carme.sandbox")


class SandboxManager:
    """负责造沙箱、限并发、保证一定被回收。"""

    def __init__(self, config: Config, bus=None, execution=None) -> None:
        self.config = config
        self.bus = bus
        self.execution = execution
        self._semaphore = asyncio.Semaphore(config.sandbox.max_concurrent_sandbox)
        self._live: set[Sandbox] = set()

    def _settings_for(self, mode: str) -> dict:
        settings = dict(self.config.sandbox.mode(mode))
        # local 模式的 max_output_bytes 在 modes.local 里，docker 模式没有，取个交集
        return settings

    def _make(self, agent_id: str, task_id: str, mode: str, node: dict | None = None) -> Sandbox:
        if node == {} or (node is None and mode not in {"docker", "trusted-local"}):
            raise SandboxError("target_unassigned: execution target is required")
        settings = deepcopy(node) if node is not None else self._settings_for(mode)
        ceiling = self.config.sandbox.limit("max_output_bytes", 65536)
        settings["max_output_bytes"] = min(ceiling, int(settings.get("max_output_bytes", ceiling)))
        if settings["max_output_bytes"] <= 0:
            raise SandboxError("invalid_limit:max_output_bytes")
        spec = SandboxSpec(
            agent_id=agent_id,
            task_id=task_id,
            mode="remote" if node is not None else ("local" if mode == "trusted-local" else mode),
            settings=settings,
        )
        return make_sandbox(spec, execution=self.execution)

    async def _prepare(self, agent_id: str, task_id: str, mode: str, node: dict | None = None) -> Sandbox:
        """执行环境失败直接上报；指定执行电脑后绝不在后端自动续跑。"""
        sandbox = self._make(agent_id, task_id, mode, node)
        try:
            await sandbox.setup()
            return sandbox
        except BaseException:
            with contextlib.suppress(Exception):
                await sandbox.teardown()
            raise

    @contextlib.asynccontextmanager
    async def acquire(self, agent_id: str, task_id: str, mode: str | None = None,
                      *, node: dict | None = None) -> AsyncIterator[Sandbox]:
        resolved = "remote" if node is not None else (mode or self.config.sandbox.default_mode)

        async with self._semaphore:
            sandbox = await self._prepare(agent_id, task_id, resolved, node)
            self._live.add(sandbox)
            try:
                yield sandbox
            finally:
                try:
                    await sandbox.teardown()
                except Exception as exc:  # noqa: BLE001 - 清理失败不该影响主流程
                    log.warning("沙箱回收失败 %s：%s", sandbox.label, exc)
                self._live.discard(sandbox)

    def handle(
        self,
        agent_id: str,
        task_id: str,
        mode: str | None = None,
        on_fallback=None,
        *, node: dict | None = None,
    ) -> "SandboxHandle":
        return SandboxHandle(self, agent_id, task_id, mode, on_fallback, node=node)

    async def probe_node(self, node_id: str | None = None) -> dict:
        """显式连接测试：只读 SSH 和 Chrome 状态，不建工作目录、不打开页面。"""
        from .remote import RemoteSandbox
        import json
        node = None
        try:
            node = self.config.sandbox.resolve_node(node_id)
            sandbox = RemoteSandbox(SandboxSpec("probe", "healthcheck", "remote", node))
        except (ValueError, SandboxError) as exc:
            return {"ok": False, "node_id": node_id, "status": "unconfigured", "error": str(exc)}
        try:
            system = await sandbox._ssh("uname -s && uname -m", timeout=node["connect_timeout"] + 5)
            if not system.ok:
                return {"ok": False, "node_id": node["node_id"], "status": "offline",
                        "error": system.stderr.strip()[:300], "ssh": {"ok": False}}
            port = int(node["browser"].get("cdp_port", 9222))
            response = await sandbox._ssh(
                f"curl --noproxy '*' -fsS --max-time 3 http://127.0.0.1:{port}/json/version", timeout=5)
            try:
                browser_ok = response.ok and bool(json.loads(response.stdout).get("webSocketDebuggerUrl"))
            except (ValueError, AttributeError):
                browser_ok = False
            return {"ok": bool(browser_ok), "node_id": node["node_id"], "status": "online",
                    "ssh": {"ok": True, "detail": system.stdout.strip()},
                    "browser": {"ok": bool(browser_ok), "error": "" if browser_ok else "SSH 已连接，Chrome CDP 未就绪"},
                    "desktop": {"ok": False, "status": "not_implemented"}}
        except Exception as exc:
            return {"ok": False, "node_id": node["node_id"], "status": "offline", "error": str(exc)[:300]}
        finally:
            with contextlib.suppress(Exception):
                await sandbox.teardown()

    async def probe(self, mode: str | None = None, agent_id: str = "probe") -> dict:
        """健康检查：这个模式现在能不能用。界面上的「节点状态」就靠它。"""
        resolved = mode or self.config.sandbox.default_mode
        settings = self._settings_for(resolved)
        spec = SandboxSpec(agent_id=agent_id, task_id="healthcheck", mode=resolved, settings=settings)
        try:
            sandbox = make_sandbox(spec)
        except SandboxError as exc:
            return {"ok": False, "mode": resolved, "error": str(exc)}
        try:
            await sandbox.setup()
            return await sandbox.health()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "mode": resolved, "error": str(exc)[:500]}
        finally:
            with contextlib.suppress(Exception):
                await sandbox.teardown()

    @property
    def live_count(self) -> int:
        return len(self._live)


class SandboxHandle:
    """按需占用沙箱的句柄。

    为什么需要它 —— 一个真实会踩的坑：
        主控占用唯一的沙箱槽位 → 派活给工程师 → 工程师也要抢这个槽位
        → 主控等工程师、工程师等槽位 → 死锁。

    解法是把「占用」和「持有」拆开：
      · 第一次真的要跑命令时才去申请槽位；
      · 等待下属执行期间主动释放，因为这段时间根本用不到沙箱；
      · 下次再要时自动重新申请。

    在只有 1 个沙箱槽位的低配机器上，这是让多层协作能跑起来的关键。

    显式本地开发模式可以配置降级；绑定了执行电脑的任务永不降级。
    """

    def __init__(
        self,
        manager: SandboxManager,
        agent_id: str,
        task_id: str,
        mode: str | None,
        on_fallback=None,
        *, node: dict | None = None,
    ) -> None:
        self._manager = manager
        self._agent_id = agent_id
        self._task_id = task_id
        self._mode = mode
        self._node = deepcopy(node)
        self._on_fallback = on_fallback
        self._cm = None
        self._sandbox: Sandbox | None = None
        # 降级发生后记下来，界面上要能看到「这次其实是在本机跑的」
        self.fallback_note: str = ""

    async def get(self) -> Sandbox:
        if self._sandbox is None:
            self._cm = self._manager.acquire(self._agent_id, self._task_id, self._mode, node=self._node)
            try:
                self._sandbox = await self._cm.__aenter__()
            except BaseException:
                await self._discard_cm()
                raise
        return self._sandbox

    async def _discard_cm(self) -> None:
        if self._cm is not None:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)
            self._cm = None
            self._sandbox = None

    async def release(self) -> None:
        """放开槽位。正在执行命令时不要调。"""
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            finally:
                self._cm = None
                self._sandbox = None

    @property
    def held(self) -> bool:
        return self._sandbox is not None

    async def __aenter__(self) -> "SandboxHandle":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.release()
        return False


__all__ = [
    "SandboxManager",
    "SandboxHandle",
    "Sandbox",
    "SandboxSpec",
    "SandboxError",
    "ExecResult",
    "make_sandbox",
]
