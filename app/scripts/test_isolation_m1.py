"""M1 adversarial gates; synthetic identities, temporary DBs, no personal CLI or Docker."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"
from fastapi import Depends, FastAPI
from starlette.requests import Request
import httpx
from carme.api.routes import build_router, require_token
from carme.approval import ApprovalOutcome
from carme.bus import EventBus
from carme.config import AgentSpec, AgentsConfig, BrowserConfig, Config, ModelsConfig, SandboxConfig
from carme.engines import (CliEngineError, PI_PACKAGE, PI_SECURITY_FLAGS, PI_VERSION,
                           build_argv, detect_cli_engines, run_cli_engine, validate_runtime_profile)
from carme.llm import LLMResponse, ToolCall, Usage
from carme.mcp import MCPError, MCPServer, _StdioTransport
from carme.runtime import Runtime
from carme.sandbox.base import SandboxError, SandboxSpec, make_sandbox
from carme.sandbox.local import LocalSandbox
from carme.sandbox.remote import ssh_args
from carme.security import bounded_output, child_env, explicit_child_values
from carme.store import Store
from carme.tools.base import Tool, ToolContext, ToolRegistry


def profile():
    return dict(id="fixture-pi", engine="pi", package_name=PI_PACKAGE, package_version=PI_VERSION,
                image_digest="sha256:" + "0" * 64, execution_mode="managed_bridge",
                inherit_user_config=False, native_tools=[], credential_ref="fixture-key",
                credential_kind="api_key", model="fixture/model", session_authority="carme",
                resume_personal_sessions=False)


class Gateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append(copy.deepcopy({"messages": messages, **kwargs}))
        value = next(self.responses)
        return await value() if callable(value) else value

    async def aclose(self):
        pass


class Echo(Tool):
    name = "echo"

    async def run(self, ctx, text="ok"):
        return text


class M1(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="carme-m1-test-")
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CARME_LOAD_ENV": "0", "CARME_CONFIG_DIR": str(self.root / "config"),
            "CARME_DATA_DIR": str(self.root), "CARME_SKILLS_DIR": str(self.root / "skills"),
            "CARME_SKILLS_CONFIG": str(self.root / "skills.yaml"), "CARME_MCP_CONFIG": str(self.root / "mcp.yaml")})
        self.env.start()
        self.store = Store(self.root / "test.db")
        self.config = Config(AgentsConfig(agents={"bot": AgentSpec("bot", "Fixture", entry=True,
            tools=["files", "exec", "memory", "team"], can_delegate=True)}), ModelsConfig(),
            SandboxConfig(), BrowserConfig(enabled=False), root=self.root, data_dir=self.root)
        self.runtime = Runtime(self.config, self.store, EventBus())

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        self.store.close()
        self.env.stop()
        self.tmp.cleanup()

    async def settle(self):
        while self.runtime._jobs:
            await asyncio.gather(*list(self.runtime._jobs.values()), return_exceptions=True)

    def app(self):
        app = FastAPI()
        app.state.store = self.store
        app.include_router(build_router(self.config, self.store, self.runtime),
                           dependencies=[Depends(require_token)])
        return app

    async def test_PI_profile_never_discovers_personal_defaults(self):
        personal = self.root / "synthetic-personal" / ".pi"
        personal.mkdir(parents=True)
        (personal / "settings.json").write_text('{"model":"personal-secret"}')
        good = profile()
        with patch.dict(os.environ, {"HOME": str(personal.parent), "PI_CODING_AGENT_DIR": str(personal),
                                    "NODE_OPTIONS": "--require personal.js"}), \
             patch("pathlib.Path.read_text", side_effect=AssertionError("personal read")), \
             patch("subprocess.run", side_effect=AssertionError("probe spawned")), \
             patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Pi spawned")):
            first = detect_cli_engines({"fixture-pi": good})
            os.environ["PI_CODING_AGENT_DIR"] = "/unapproved/changed"
            self.assertEqual(first, detect_cli_engines({"fixture-pi": good}))
            with self.assertRaisesRegex(CliEngineError, "runtime_profile_required"):
                await run_cli_engine("pi", "pwd")
            with self.assertRaisesRegex(CliEngineError, "container_runner_unavailable"):
                await run_cli_engine("pi", "pwd", profile=good)
        self.assertEqual(json.loads((personal / "settings.json").read_text()), {"model": "personal-secret"})

    async def test_PI_rejects_unsafe_profiles_and_harnesses(self):
        for changes in ({"inherit_user_config": True}, {"native_tools": ["bash"]},
                        {"resume_personal_sessions": True}, {"package_version": "latest"},
                        {"credential_ref": ""}, {"credential_kind": "oauth"},
                        {"model": ""}, {"image_digest": "latest"}, {"extra_flags": ["--continue"]}):
            with self.subTest(changes=changes), self.assertRaises(CliEngineError):
                validate_runtime_profile({**profile(), **changes})
        for engine in ("codex", "claude"):
            with self.assertRaises(CliEngineError):
                await run_cli_engine(engine, "pwd", profile=profile())
        argv = build_argv("pi", "/fixture/pi", "fixture/model", "")
        self.assertTrue(set(PI_SECURITY_FLAGS) <= set(argv))
        self.assertIn("--no-tools", argv)
        self.assertNotIn("--continue", argv)

    async def test_PI_env_allowlist_and_ssh_identity(self):
        malicious = {name: "not-in-child" for name in ("CARME_TOKEN", "OPENAI_API_KEY", "SSH_AUTH_SOCK",
            "NODE_OPTIONS", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "DOCKER_HOST", "HTTP_PROXY")}
        with patch.dict(os.environ, malicious):
            env = child_env(self.root / "child")
            self.assertFalse(set(malicious) & set(env))
        self.assertEqual(child_env(self.root, network={"HTTPS_PROXY": "http://fixture.invalid"})["HTTPS_PROXY"], "http://fixture.invalid")
        for name in malicious:
            if name in {"OPENAI_API_KEY", "HTTP_PROXY"}:
                continue  # Explicit MCP credential/network grants are allowed.
            with self.subTest(name=name), self.assertRaises(ValueError):
                explicit_child_values({name: "injection"})
        argv = ssh_args({"host": "fixture.invalid", "user": "fixture"})
        for arg in ("/dev/null", "IdentityAgent=none", "IdentityFile=none", "UserKnownHostsFile=/dev/null", "StrictHostKeyChecking=yes"):
            self.assertIn(arg, argv)

    async def test_EX_none_has_no_filesystem_or_spawn_side_effect(self):
        self.runtime.gateway = Gateway([LLMResponse(tool_calls=[ToolCall("a", "shell", {"command": "pwd"}),
            ToolCall("b", "write_file", {"path": "escape", "content": "x"})]), LLMResponse(text="denied")])
        with patch("pathlib.Path.mkdir", side_effect=AssertionError("mkdir")), \
             patch("asyncio.create_subprocess_exec", side_effect=AssertionError("spawn")), \
             patch("asyncio.create_subprocess_shell", side_effect=AssertionError("shell")):
            task = await self.runtime.submit("bot", "pwd and write file")
            await self.settle()
        history = self.runtime.gateway.calls[-1]["messages"]
        self.assertEqual(sum("target_unassigned" in str(m) for m in history), 2)
        self.assertEqual(json.loads(self.store.get_task(task)["meta"])["execution_target"], "none")
        self.assertFalse((self.root / "escape").exists())

    async def test_EX_unavailable_targets_never_fallback(self):
        self.config.isolation["targets"] = {"fixture": {}}
        self.config.agents.get("bot").execution_target_id = "fixture"
        for target, error in (("container", "container_runner_unavailable"),):
            self.config.agents.get("bot").execution_target = target
            with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("spawn")):
                task = await self.runtime.submit("bot", "pwd")
                await self.settle()
            self.assertEqual(self.store.get_task(task)["error"], error)
        self.config.agents.get("bot").execution_target = "macos"
        with self.assertRaisesRegex(ValueError, "target_unsupported"):
            await self.runtime.submit("bot", "pwd")
        with self.assertRaisesRegex(SandboxError, "container_runner_unavailable"):
            make_sandbox(SandboxSpec("bot", "task", "docker"))
        with self.assertRaisesRegex(SandboxError, "trusted_host_required"):
            LocalSandbox(SandboxSpec("bot", "task", "local", {"root": str(self.root / "never")}))
        self.assertFalse((self.root / "never").exists())

    async def test_EX_engine_independent_intersection_and_revocation(self):
        self.config.isolation = {"admin_tools": ["read_file", "recall"], "targets": {"fixture": {"tools": ["recall"]}}}
        spec = self.config.agents.get("bot")
        spec.execution_target_id = "fixture"
        policies = []
        for engine in ("api", "pi", "codex", "claude"):
            spec.engine = engine
            policies.append(self.runtime.policy_for("bot", target="none", node={}))
        self.assertTrue(all(p == policies[0] for p in policies))
        self.assertEqual(policies[0]["tools"], ["recall"])
        with patch.object(self.runtime, "_schedule"):
            task = await self.runtime.submit("bot", "read only")
        self.runtime.check_task_policy(task)
        spec.tools = []
        with self.assertRaisesRegex(RuntimeError, "permission_version_changed"):
            self.runtime.check_task_policy(task)

    async def test_EX_approval_binds_actual_parameters_and_target(self):
        ctx = ToolContext(self.config.agents.get("bot"), "task", self.store, node={"node_id": "a"},
                          extras={"policy": {"target": "ssh"}, "call": {"arguments": {"command": "pwd"}}})
        async def mutate(**kwargs):
            self.assertIn("action_digest", kwargs["detail"])
            ctx.node = {"node_id": "b"}
            return ApprovalOutcome(True, "fixture")
        ctx.approve = mutate
        result = await ctx.request_approval(kind="shell", summary="fixture", detail={"command": "pwd"})
        self.assertFalse(result.approved)
        self.assertIn("approval_binding_changed", result.note)

    async def test_MC_stdio_disabled_before_spawn(self):
        transport = _StdioTransport(MCPServer("fixture", command=sys.executable))
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("spawn")), \
             patch("tempfile.TemporaryDirectory", side_effect=AssertionError("mkdir")):
            with self.assertRaisesRegex(MCPError, "stdio_requires_isolated_executor"):
                await transport.start()
        await transport.close()

    async def test_SE_pairing_csrf_origin_revocation_and_key_rotation(self):
        with patch.dict(os.environ, {"CARME_TOKEN": "synthetic-admin", "CARME_PUBLIC_ORIGIN": "https://carme.test"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url="https://carme.test") as client:
                self.assertEqual((await client.get("/api/health?token=synthetic-admin")).status_code, 401)
                self.assertEqual((await client.post("/api/session")).status_code, 401)
                headers = {"Authorization": "Bearer synthetic-admin", "Origin": "https://carme.test"}
                paired = await client.post("/api/session", headers=headers)
                self.assertEqual(paired.status_code, 200, paired.text)
                cookie = paired.headers["set-cookie"]
                self.assertTrue(all(value in cookie.lower() for value in ("httponly", "secure", "samesite=strict")))
                cookie1, session1 = client.cookies.get("carme_session"), paired.json()
                self.assertEqual(round(session1["expires_at"] - session1["created_at"]), 604800)
                self.assertIn("max-age=604800", cookie.lower())
                self.assertNotIn("synthetic-admin", cookie)
                self.assertEqual((await client.get("/api/health")).status_code, 200)
                self.assertEqual((await client.post("/api/conversations", json={"agent_ids": ["bot"]})).status_code, 403)
                csrf = {"X-Carme-CSRF": session1["csrf"], "Origin": "https://carme.test"}
                self.assertEqual((await client.post("/api/conversations", json={"agent_ids": ["bot"]}, headers=csrf)).status_code, 201)
                self.assertEqual((await client.get("/api/health", headers={"Origin": "https://evil.test"})).status_code, 403)
                other = await client.post("/api/session", headers=headers)
                self.assertNotEqual(cookie1, client.cookies.get("carme_session"))
                revoke = await client.delete("/api/sessions/" + session1["id"], headers={"X-Carme-CSRF": other.json()["csrf"]})
                self.assertEqual(revoke.status_code, 200)
                self.assertIsNone(self.store.session(cookie1, "synthetic-admin"))
                os.environ["CARME_TOKEN"] = "rotated-admin"
                self.assertEqual((await client.get("/api/health")).status_code, 401)
        raw = self.store._query("SELECT * FROM browser_sessions")
        self.assertNotIn(cookie1, json.dumps(raw))

    async def test_SE_expiration_and_restart_are_server_side(self):
        with patch("carme.store.time.time", return_value=100):
            token, _ = self.store.create_session("fixture", ttl=10)
        reopened = Store(self.store.path)
        try:
            with patch("carme.store.time.time", return_value=109):
                self.assertIsNotNone(reopened.session(token, "fixture"))
            with patch("carme.store.time.time", return_value=110):
                self.assertIsNone(reopened.session(token, "fixture"))
        finally:
            reopened.close()

    async def test_SE_empty_auth_only_explicit_loopback_development(self):
        for dev, host, forwarded, expected in (("", "127.0.0.1", {}, 503), ("1", "127.0.0.1", {}, 200),
                ("1", "public.test", {}, 503), ("1", "127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, 503)):
            with self.subTest(dev=dev, host=host, forwarded=forwarded), patch.dict(os.environ,
                    {"CARME_TOKEN": "", "CARME_DEV_NO_AUTH": dev, "CARME_PUBLIC_ORIGIN": ""}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url="http://" + host) as client:
                    self.assertEqual((await client.get("/api/health", headers=forwarded)).status_code, expected)

    async def test_LIM_task_deadline_and_unknown_usage(self):
        async def slow():
            await asyncio.sleep(10)
        self.config.sandbox.limits["max_task_seconds"] = 1
        self.runtime.gateway = Gateway([slow])
        started = time.monotonic()
        task = await self.runtime.submit("bot", "wait forever")
        await self.settle()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.store.get_task(task)["error"], "task_deadline_exceeded")
        self.runtime.gateway = Gateway([LLMResponse(text="unknown", usage=Usage(cost_known=False, tokens_known=False))])
        task = await self.runtime.submit("bot", "unknown cost")
        await self.settle()
        self.assertFalse(self.store.get_task(task)["cost_known"])
        self.assertFalse(self.store.get_task(task)["tokens_known"])

    async def test_LIM_daily_admission_atomic_across_connections(self):
        stores = [Store(self.store.path) for _ in range(3)]
        def insert(index):
            try:
                return stores[index % 3].create_task("bot", "fixture", max_daily_tasks=5)
            except ValueError as exc:
                return str(exc)
        try:
            with ThreadPoolExecutor(max_workers=9) as executor:
                result = list(executor.map(insert, range(18)))
            self.assertEqual(result.count("max_daily_tasks_exceeded"), 13)
            with patch("carme.store.time.time", return_value=time.time() + 86401):
                self.store.create_task("bot", "next UTC day", max_daily_tasks=5)
        finally:
            for store in stores:
                store.close()

    async def test_LIM_deadline_rejects_pending_approval_before_spawn(self):
        spec = self.config.agents.get("bot")
        spec.execution_target = "ssh"
        spec.execution_target_id = "fixture"
        self.config.sandbox.nodes = [{"node_id": "fixture", "host": "fixture.invalid", "user": "fixture"}]
        self.config.sandbox.limits["max_task_seconds"] = 1
        self.runtime.gateway = Gateway([LLMResponse(tool_calls=[ToolCall("shell", "shell", {"command": "pwd"})])])
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("spawn before approval")):
            task = await self.runtime.submit("bot", "wait for approval")
            await self.settle()
        self.assertEqual(self.store.get_task(task)["error"], "task_deadline_exceeded")
        self.assertEqual(self.store.pending_approval_count(), 0)
        self.assertEqual(self.store._query("SELECT status FROM approvals")[0]["status"], "rejected")

    async def test_EX_delegated_scope_revocation_and_unknown_cost(self):
        self.config.agents.get("bot").tools = ["team", "recall"]
        self.config.agents.agents["child"] = AgentSpec("child", "Child", tools=["remember", "recall"])
        self.runtime.gateway = Gateway([
            LLMResponse(tool_calls=[ToolCall("d", "delegate", {"agent": "child", "goal": "fixture"})]),
            LLMResponse(text="child", usage=Usage(cost_known=False, tokens_known=False)), LLMResponse(text="done")])
        task = await self.runtime.submit("bot", "delegate")
        await self.settle()
        child = self.store.children_of(task)[0]
        self.assertEqual(json.loads(child["meta"])["policy"]["tools"], ["recall"])
        self.assertFalse(self.store.get_task(task)["cost_known"])
        self.assertIn("unknown", str(self.runtime.gateway.calls[-1]["messages"]))
        with patch.object(self.runtime, "_schedule"):
            parent = await self.runtime.submit("bot", "queued")
            queued = await self.runtime.submit("child", "queued child", parent_id=parent)
        self.runtime.check_task_policy(queued)
        self.config.agents.get("bot").tools = []
        with self.assertRaisesRegex(RuntimeError, "permission_version_changed"):
            self.runtime.check_task_policy(queued)

    async def test_SE_revocation_stops_sse_backlog(self):
        with patch.dict(os.environ, {"CARME_TOKEN": "synthetic-admin", "CARME_PUBLIC_ORIGIN": ""}):
            cookie, session = self.store.create_session("synthetic-admin")
            app = self.app()
            request = Request({"type": "http", "method": "GET", "path": "/api/events", "scheme": "http",
                "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 5000), "query_string": b"", "app": app,
                "headers": [(b"cookie", ("carme_session=" + cookie).encode())]})
            request.is_disconnected = AsyncMock(return_value=False)
            router = build_router(self.config, self.store, self.runtime)
            endpoint = next(route.endpoint for route in router.routes if route.path == "/api/events")
            stream = (await endpoint(request)).body_iterator
            await anext(stream)
            self.store.add_event("fixture-first", {})
            self.store.add_event("fixture-second", {})
            self.assertIn("fixture-first", await anext(stream))
            self.store.revoke_session(session["id"])
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
            self.assertEqual(self.runtime.bus.subscriber_count, 0)

    async def test_EX_config_reload_applies_admin_ceiling(self):
        with patch.object(self.runtime, "_schedule"):
            task = await self.runtime.submit("bot", "queued")
        fresh = copy.deepcopy(self.config)
        fresh.isolation = {"admin_tools": []}
        with patch.dict(os.environ, {"CARME_TOKEN": "synthetic-admin"}), patch("carme.config.load", return_value=fresh):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url="http://127.0.0.1") as client:
                result = await client.post("/api/config/reload", headers={"Authorization": "Bearer synthetic-admin"})
                self.assertEqual(result.status_code, 200, result.text)
        with self.assertRaisesRegex(RuntimeError, "permission_version_changed"):
            self.runtime.check_task_policy(task)

    async def test_LIM_registry_identity_count_output_and_empty_grant(self):
        registry = ToolRegistry()
        registry.register(Echo())
        ctx = ToolContext(self.config.agents.get("bot"), "task", self.store,
                          extras={"policy": {"tools": ["echo"], "max_tool_calls": 1, "max_output_bytes": 8}})
        self.assertIn("identity_override_denied", await registry.execute(ctx, "echo", {"task_id": "other"}))
        self.assertIn("output_limit_exceeded", await registry.execute(ctx, "echo", {"text": "x" * 100}))
        self.assertIn("tool_call_limit_exceeded", await registry.execute(ctx, "echo", {}))
        ctx.extras = {"policy": {}}
        self.assertIn("capability_denied", await registry.execute(ctx, "echo", {}))
        self.config.agents.get("bot").tools = ["memory"]
        self.config.sandbox.limits["max_tool_calls"] = 1
        self.runtime.gateway = Gateway([LLMResponse(tool_calls=[
            ToolCall("one", "remember", {"key": "allowed", "value": "one"}),
            ToolCall("two", "remember", {"key": "over-limit", "value": "two"})]), LLMResponse(text="done")])
        await self.runtime.submit("bot", "remember twice")
        await self.settle()
        self.assertEqual([item["key"] for item in self.store.recall("bot")], ["allowed"])
        self.assertIn("tool_call_limit_exceeded", str(self.runtime.gateway.calls[-1]["messages"]))

    async def test_LIM_configured_output_limit_reaches_subprocess(self):
        self.config.sandbox.limits["max_output_bytes"] = 8
        self.config.sandbox.modes["trusted-local"] = {"trusted_host": True, "root": str(self.root / "debug"),
                                                    "max_output_bytes": 65536}
        handle = self.runtime.sandboxes.handle("fixture", "task", "trusted-local")
        try:
            sandbox = await handle.get()
            with self.assertRaisesRegex(RuntimeError, "output_limit_exceeded"):
                await sandbox.exec("printf '123456789'", timeout=2)
        finally:
            await handle.release()

    async def test_LIM_streaming_output_and_process_tree_cleanup(self):
        marker = self.root / "child-survived"
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(
            "import time,pathlib; time.sleep(1.5); pathlib.Path(" + repr(str(marker)) + ").write_text('survived')") + "]); print('x'*30000,flush=True); time.sleep(20)"
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=child_env(self.root), start_new_session=True)
        with self.assertRaisesRegex(RuntimeError, "output_limit_exceeded"):
            await bounded_output(proc, timeout=4, limit=1024)
        await asyncio.sleep(1.7)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
