"""执行电脑绑定回归：临时配置与模拟 SSH/CDP，不接触真实节点、.env 或数据。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

_temporary = tempfile.TemporaryDirectory(prefix="carme-nodes-test-")
os.environ["CARME_CONFIG_DIR"] = _temporary.name
os.environ["CARME_LOAD_ENV"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from carme import config
from carme.browser.manager import BrowserManager
from carme.browser.session import BrowserError, BrowserSession
from carme.sandbox import SandboxManager
from carme.sandbox.base import ExecResult, SandboxError, SandboxSpec
from carme.sandbox.local import LocalSandbox
from carme.sandbox.remote import RemoteSandbox, remote_quote, ssh_args


NODE = {"node_id": "mbp2018", "name": "2018 MacBook", "host": "executor.invalid",
        "user": "runner", "identity_file": "~/.ssh/test_only", "root": "~/carme work",
        "browser": {"cdp_port": 9222}, "desktop": {"enabled": False}}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(_temporary.name) / "sandbox.yaml"
        self.path.write_text(yaml.safe_dump({"version": 2, "default_node_id": "mbp2018",
                             "nodes": [NODE], "limits": {"max_concurrent_sandbox": 1},
                             "custom": "${DO_NOT_EXPAND}",
                             "modes": {"remote": {"timeout_seconds": 300, "fallback": "local"}}}))

    def test_config_isolation_and_snapshot_survives_switch(self):
        with patch.object(config, "load_dotenv") as dotenv:
            before = config.load(reload=True).sandbox.resolve_node()
            dotenv.assert_not_called()
        self.assertEqual(before["root"], "~/carme work")
        replacement = {**NODE, "node_id": "replacement", "host": "next.invalid"}
        config.save_node(replacement)
        current = config.set_default_node("replacement")
        self.assertEqual(current.resolve_node()["host"], "next.invalid")
        self.assertEqual(before["host"], "executor.invalid")
        self.assertEqual(before["fallback"], "none")
        before["browser"]["cdp_port"] = 9999
        self.assertEqual(current.resolve_node("mbp2018")["browser"]["cdp_port"], 9222)
        raw = yaml.safe_load(self.path.read_text())
        self.assertEqual(raw["custom"], "${DO_NOT_EXPAND}")
        self.assertEqual(raw["modes"]["remote"]["timeout_seconds"], 300)
        self.assertEqual(len(current.list_nodes()), 2)

    def test_unconfigured_and_invalid_nodes(self):
        cfg = config.SandboxConfig(nodes=[{**NODE, "host": ""}], default_node_id="mbp2018")
        self.assertEqual(cfg.list_nodes()[0]["status"], "unconfigured")
        with self.assertRaises(ValueError):
            cfg.resolve_node()
        with self.assertRaises(ValueError):
            config.SandboxConfig(nodes=[NODE]).resolve_node()
        for data in ({**NODE, "password": "not-allowed"}, {**NODE, "host": "-oProxyCommand=anything"},
                     {**NODE, "host": "original@other.invalid"}, {**NODE, "user": "runner@other.invalid"},
                     {**NODE, "browser": {"cdp_host": "elsewhere.invalid"}},
                     {**NODE, "identity_file": "-----BEGIN PRIVATE KEY-----"}):
            with self.assertRaises(ValueError):
                config.save_node(data)

    def test_node_free_text_cannot_expose_environment(self):
        with patch.dict(os.environ, {"CARME_TEST_PRIVATE": "do-not-expose"}):
            current = config.save_node({**NODE, "name": "${CARME_TEST_PRIVATE}",
                                       "identity_file": "${CARME_TEST_PRIVATE}"})
            self.assertEqual(current.list_nodes()[0]["name"], "${CARME_TEST_PRIVATE}")
            self.assertEqual(current.resolve_node()["identity_file"], "${CARME_TEST_PRIVATE}")

    def test_mock_candidates_require_explicit_enable(self):
        provider = config.Provider("mock", "mock", "", "UNSET_MOCK_KEY", api_key_default="test")
        models = config.ModelsConfig(providers={"mock": provider}, tiers={"fast": ["mock/demo"]})
        self.assertEqual(models.candidates("fast"), [])
        models.allow_mock = True
        self.assertEqual(models.candidates("fast"), ["mock/demo"])


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_node_failure_cannot_fallback(self):
        cfg = config.Config(config.AgentsConfig(), config.ModelsConfig(),
                            config.SandboxConfig(default_mode="local", modes={"remote": {"fallback": "local"}, "local": {}}))
        manager = SandboxManager(cfg)
        fake = MagicMock()
        fake.setup = AsyncMock(side_effect=SandboxError("offline"))
        fake.teardown = AsyncMock()
        snapshot = deepcopy(NODE)
        handle = manager.handle("bot", "task", mode="local", node=snapshot)
        snapshot["host"] = "mutated.invalid"
        with patch("carme.sandbox.make_sandbox", return_value=fake) as make:
            with self.assertRaisesRegex(SandboxError, "offline"):
                await handle.get()
            self.assertEqual(make.call_count, 1)
            spec = make.call_args.args[0]
            self.assertEqual(spec.mode, "remote")
            self.assertEqual(spec.settings["host"], "executor.invalid")
        self.assertEqual(manager._semaphore._value, 1)

    async def test_empty_snapshot_blocks_local_execution(self):
        cfg = config.Config(config.AgentsConfig(), config.ModelsConfig(), config.SandboxConfig())
        with self.assertRaisesRegex(SandboxError, "不会改在后端"):
            await SandboxManager(cfg).handle("bot", "task", node={}).get()

    async def test_remote_paths_and_teardown(self):
        sandbox = RemoteSandbox(SandboxSpec("bot", "task", "remote", NODE))
        commands = []
        async def run(command, timeout):
            commands.append(command)
            return ExecResult(ok=True, exit_code=0, stdout="carme-ok")
        with patch.object(sandbox, "_ssh", side_effect=run):
            await sandbox.setup()
            await sandbox._run("pwd", None, 10)
            await sandbox.write("report.txt", "a$(not_a_command)")
        self.assertIn('"$HOME"/', commands[1])
        self.assertIn('"$HOME"/', commands[2])
        self.assertNotIn(str(Path.home()), "\n".join(commands))
        self.assertEqual(remote_quote("~/one two"), '"$HOME"/\'one two\'')
        sandbox._connected = True
        proc = types.SimpleNamespace(wait=AsyncMock(return_value=0))
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            await sandbox.teardown()
        args = spawn.call_args.args
        self.assertEqual(args[-3:], ("-O", "exit", "runner@executor.invalid"))
        self.assertIn("StrictHostKeyChecking=yes", ssh_args(NODE))

    async def test_probe_does_not_setup_or_create_workspace(self):
        cfg = config.Config(config.AgentsConfig(), config.ModelsConfig(),
                            config.SandboxConfig(nodes=[NODE], default_node_id="mbp2018"))
        responses = [ExecResult(ok=True, stdout="Darwin\narm64"),
                     ExecResult(ok=True, stdout='{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/test"}')]
        with patch.object(RemoteSandbox, "_ssh", AsyncMock(side_effect=responses)) as command, \
             patch.object(RemoteSandbox, "setup", AsyncMock(side_effect=AssertionError("must not setup"))):
            result = await SandboxManager(cfg).probe_node()
        self.assertTrue(result["ssh"]["ok"])
        self.assertTrue(result["browser"]["ok"])
        self.assertFalse(result["desktop"]["ok"])
        self.assertEqual(command.call_count, 2)


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.getenv("CARME_LIVE_BROWSER_TEST") == "1", "需显式启用隔离 Chromium 测试")
    async def test_live_snapshot_does_not_return_private_inputs(self):
        from playwright.async_api import async_playwright
        from carme.browser.snapshot import SNAPSHOT_JS
        import json
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<label>Password <input type="password" value="private-password"></label>'
                    '<input name="api_key" value="private-api-key">'
                    '<label>Credentials<textarea name="credentials">private-textarea</textarea></label><button>Continue</button>')
                snapshot = await page.evaluate(SNAPSHOT_JS)
                session = BrowserSession("test", {}, Path(_temporary.name))
                session._page = page
                summaries = [await session.element_summary(row["ref"]) for row in snapshot["elements"]]
                encoded = json.dumps([snapshot, summaries])
                for value in ("private-password", "private-api-key", "private-textarea"):
                    self.assertNotIn(value, encoded)
                self.assertTrue(any(row.get("filled") for row in snapshot["elements"]))
            finally:
                await browser.close()

    async def test_approval_cannot_apply_to_changed_element(self):
        from carme.tools.web import Guard, WebClickTool, WebTypeTool
        node = deepcopy(NODE)
        for tool, extra in ((WebClickTool(), {}), (WebTypeTool(), {"text": "confirm", "submit": True})):
            with self.subTest(tool=tool.name):
                session = types.SimpleNamespace(
                    page_context=AsyncMock(return_value={"url": "https://example.invalid/account", "title": "Delete"}),
                    element_summary=AsyncMock(side_effect=[{"text": "Delete record A"}, {"text": "Delete record B"}]),
                    click=AsyncMock(), type_text=AsyncMock())
                handle = MagicMock()
                handle.get = AsyncMock(return_value=session)
                handle.__aenter__ = AsyncMock(return_value=handle)
                handle.__aexit__ = AsyncMock(return_value=False)
                manager = MagicMock(enabled=True, default_profile="default")
                manager.handle.return_value = handle
                manager.is_read_only.return_value = False
                manager.check_domain.return_value = None
                manager.danger_hit.return_value = "delete"
                ctx = types.SimpleNamespace(browser_manager=manager, node=node)
                with patch("carme.tools.web._guard_danger", AsyncMock(return_value=Guard(hit="delete"))):
                    result = await tool.run(ctx, ref=1, **extra)
                self.assertIn("没有执行", result)
                session.click.assert_not_awaited()
                session.type_text.assert_not_awaited()
                for call in manager.handle.call_args_list:
                    self.assertEqual(call.kwargs["node"], node)

    async def test_remote_cdp_close_keeps_user_chrome_alive(self):
        page = MagicMock()
        context = MagicMock(pages=[page])
        context.close = AsyncMock()
        browser = MagicMock(contexts=[context])
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        playwright.chromium.launch_persistent_context = AsyncMock()
        playwright.stop = AsyncMock()
        fake_api = types.ModuleType("playwright.async_api")
        fake_api.async_playwright = lambda: types.SimpleNamespace(start=AsyncMock(return_value=playwright))
        session = BrowserSession("default", {"node": deepcopy(NODE)}, Path(_temporary.name))
        with patch.dict(sys.modules, {"playwright.async_api": fake_api}), \
             patch.object(session, "_start_tunnel", AsyncMock(return_value="http://127.0.0.1:12345")):
            await session.start()
            self.assertTrue(session.started)
            await session.close()
        playwright.chromium.connect_over_cdp.assert_awaited_once_with("http://127.0.0.1:12345", timeout=45000)
        playwright.chromium.launch_persistent_context.assert_not_awaited()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()
        playwright.stop.assert_awaited_once()

    async def test_unconfigured_browser_never_starts(self):
        session = BrowserSession("default", {"node": {}}, Path(_temporary.name))
        with patch.object(session, "_start_tunnel", AsyncMock()) as tunnel:
            with self.assertRaisesRegex(BrowserError, "不会在后端"):
                await session.start()
            tunnel.assert_not_awaited()

    async def test_failed_browser_releases_node_lock(self):
        manager = BrowserManager({}, Path(_temporary.name))
        with patch.object(manager, "_get_session", AsyncMock(side_effect=BrowserError("offline"))):
            with self.assertRaises(BrowserError):
                await manager.handle(node=NODE).get()
        self.assertEqual(manager._semaphore._value, 1)
        self.assertFalse(manager._lock_for(manager._lock_key("default", NODE)).locked())

    async def test_nodes_and_changed_connections_do_not_reuse_session(self):
        manager = BrowserManager({}, Path(_temporary.name))
        other = {**NODE, "node_id": "new"}
        edited = {**NODE, "host": "changed.invalid"}
        with patch.object(BrowserSession, "start", AsyncMock()), patch.object(manager, "_ensure_reaper"):
            first = await manager._get_session("default", node=NODE)
            self.assertIs(first, await manager._get_session("default", node=NODE))
            self.assertIsNot(first, await manager._get_session("default", node=other))
            self.assertIsNot(first, await manager._get_session("default", node=edited))
        self.assertEqual(manager._lock_key("default", NODE), manager._lock_key("work", NODE))
        self.assertNotEqual(manager._lock_key("default", NODE), manager._lock_key("default", other))

    async def test_tunnel_uses_loopback_and_bound_ssh_target(self):
        session = BrowserSession("default", {"node": NODE}, Path(_temporary.name))
        proc = types.SimpleNamespace(returncode=None, communicate=AsyncMock(return_value=(b"", b"")), terminate=MagicMock())
        writer = types.SimpleNamespace(close=MagicMock(), wait_closed=AsyncMock())
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn, \
             patch("asyncio.open_connection", AsyncMock(return_value=(None, writer))):
            endpoint = await session._start_tunnel()
            await session.close()
        args = spawn.call_args.args
        self.assertEqual(args[-1], "runner@executor.invalid")
        self.assertRegex(args[args.index("-L") + 1], r"^127\.0\.0\.1:\d+:127\.0\.0\.1:9222$")
        self.assertTrue(endpoint.startswith("http://127.0.0.1:"))
        proc.terminate.assert_called_once()


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    """共享根 + 每任务子目录：同根内互相可读，越出共享根仍拒。"""

    def setUp(self):
        self.root = Path(_temporary.name) / "workspaces"
        self.settings = {"root": str(self.root), "per_task_dir": True}

    def sandbox(self, agent_id: str, task_id: str) -> LocalSandbox:
        return LocalSandbox(SandboxSpec(agent_id, task_id, "local", dict(self.settings)))

    async def test_relative_paths_stay_in_task_dir(self):
        first = self.sandbox("bot-a", "task-1")
        await first.write("notes/a.txt", "hello")
        self.assertEqual((Path(first.workdir) / "notes/a.txt").read_text(), "hello")
        self.assertEqual(await first.read("notes/a.txt"), "hello")
        self.assertEqual(first.workspace, str(self.root.resolve()))
        self.assertEqual(first._resolve_cwd("notes"), str(Path(first.workdir) / "notes"))
        self.assertTrue(any(entry.endswith("notes") for entry in await first.ls(".")))

    async def test_cross_task_read_and_write_inside_workspace(self):
        first = self.sandbox("bot-a", "task-1")
        second = self.sandbox("bot-a", "task-2")
        self.assertNotEqual(first.workdir, second.workdir)
        # 显式完整路径 = 从共享根开始，可以写进另一个任务的目录
        relay = str(Path(second.workdir) / "relay.txt")
        await first.write(relay, "from task-1")
        self.assertEqual(await second.read("relay.txt"), "from task-1")
        # 相对路径仍然只落在自己目录：不会互相覆盖
        await second.write("relay.txt", "from task-2")
        self.assertEqual(await first.read(relay), "from task-2")
        self.assertEqual((Path(second.workdir) / "relay.txt").read_text(), "from task-2")
        self.assertFalse((Path(first.workdir) / "relay.txt").exists())

    async def test_outside_workspace_is_rejected_or_clamped(self):
        sandbox = self.sandbox("bot-a", "task-1")
        outside = Path(_temporary.name) / "outside.txt"
        with self.assertRaisesRegex(SandboxError, "workspace 之外"):
            await sandbox.write(str(outside), "nope")
        with self.assertRaises(SandboxError):
            await sandbox.write(str(Path(_temporary.name) / "escape.txt"), "nope")
        self.assertFalse(outside.exists())
        # cwd 越出共享根 → 拉回本任务目录，而不是去宿主别的目录执行
        self.assertEqual(sandbox._resolve_cwd(str(Path(_temporary.name))), sandbox.workdir)
        self.assertEqual(sandbox._resolve_cwd("../../.."), sandbox.workdir)
        # 共享根内的父层是允许的（接力任务常见）
        self.assertEqual(sandbox._resolve_cwd("../.."), str(self.root.resolve()))

    async def test_remote_relative_paths_bind_to_task_dir(self):
        sandbox = RemoteSandbox(SandboxSpec("bot", "task", "remote", NODE))
        task_dir = sandbox._remote_dir
        self.assertEqual(sandbox.workspace, "~/carme work")
        self.assertEqual(sandbox._abs("sub/f.txt"), f"{task_dir}/sub/f.txt")
        self.assertEqual(sandbox._abs("~/other/x.txt"), "~/other/x.txt")
        self.assertNotIn("..", sandbox._abs("../../escape.txt"))
        self.assertTrue(sandbox._abs("../../escape.txt").startswith(task_dir))

        commands = []

        async def run(command, timeout):
            commands.append(command)
            return ExecResult(ok=True, exit_code=0, stdout="carme-ok")

        with patch.object(sandbox, "_ssh", side_effect=run):
            await sandbox.setup()
            await sandbox._run("pwd", None, 10)
            await sandbox._run("pwd", "sub/dir", 10)
            await sandbox._run("pwd", "../../escape", 10)
        self.assertIn(f"cd {remote_quote(task_dir)} && pwd", commands[2])
        self.assertIn(f"cd {remote_quote(task_dir + '/sub/dir')} && pwd", commands[3])
        self.assertIn(f"cd {remote_quote(task_dir + '/escape')} && pwd", commands[4])


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        _temporary.cleanup()
