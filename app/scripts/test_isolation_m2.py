"""M2 unit/adversarial layer. Docker evidence is in test_isolation_m2_docker.py."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_isolation_m1 as m1
import httpx
from carme.attachments import archive_binary, file_path
from carme.broker import Broker, validate_spec
from carme.execution import build_execution_router, encoded, signature
from carme import execution


class M2(m1.M1):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.key = secrets.token_hex(32).encode()
        key_file = self.root / "broker-key"
        key_file.write_bytes(self.key)
        self.config.isolation = {"broker": {"key_file": str(key_file), "instance_id": "fixture", "max_pi": 2},
            "targets": {"action": {"image_digest": "sha256:" + "0" * 64}}, "profiles": {"pi": m1.profile()}}
        spec = self.config.agents.get("bot")
        spec.execution_target, spec.execution_target_id, spec.runtime_profile = "container", "action", "pi"
        self.runtime.execution.broker_seen = time.time()
        _, meta = self.runtime._task_agent_snapshot("bot")
        self.run_id = self.store.create_task("bot", "fixture", meta=meta)
        self.store.set_task_status(self.run_id, "running")
        self.calls = []
        self.app_ = self.app()
        self.app_.include_router(build_execution_router(self.runtime.execution))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app_), base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        for job in list(self.runtime.execution.jobs.values()):
            job["future"].cancel()
        await self.client.aclose()
        await super().asyncTearDown()

    async def job(self):
        async def callback(name, arguments):
            self.calls.append((name, arguments))
            return "ok"
        task = asyncio.create_task(self.runtime.execution.submit(self.run_id, "pi",
            {"prompt": "fixture", "model": "fixture/model", "effort": "", "tools": [], "max_tool_calls": 4},
            profile=m1.profile(), callback=callback))
        await asyncio.sleep(0)
        return task, next(iter(self.runtime.execution.jobs.values()))

    async def broker(self, data, nonce=None):
        raw = encoded(data)
        nonce = nonce or f"{time.time()}:{secrets.token_hex(16)}"
        response = await self.client.post("/internal/broker", content=raw, headers={"X-Carme-Nonce": nonce,
            "X-Carme-Signature": signature(self.key, nonce, raw)})
        if response.status_code == 200:
            self.assertEqual(response.headers["X-Carme-Signature"], signature(self.key, nonce, response.content))
        return response

    async def worker(self, job, data, run_id=None):
        return await self.client.post(f"/internal/runs/{run_id or self.run_id}/messages", json=data,
            headers={"Authorization": "Bearer " + job["token"]})

    async def test_signed_broker_claim_replay_and_distinct_task_identity(self):
        task, job = await self.job()
        data = {"op": "claim", "health": {"pi": "ready", "action": "ready"}, "instance_id": "fixture", "capacity": ["pi"]}
        nonce = f"{time.time()}:{secrets.token_hex(16)}"
        claim = await self.broker(data, nonce)
        self.assertEqual(claim.status_code, 200)
        self.assertEqual((await self.broker(data, nonce)).status_code, 403)
        self.assertEqual((await self.client.post("/internal/broker", json=data,
            headers={"Authorization": "Bearer " + job["token"]})).status_code, 403)
        with patch.dict(os.environ, {"CARME_TOKEN": "synthetic-admin-distinct-key", "CARME_DEV_NO_AUTH": "0"}):
            self.assertEqual((await self.client.post("/api/session", headers={"Authorization": "Bearer " + job["token"]})).status_code, 401)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_task_scope_sequence_dedup_and_revocation(self):
        task, job = await self.job()
        message = {"sequence": 1, "event_id": "fixture-event", "kind": "tool", "payload": {"name": "read_file", "arguments": {"path": "x"}}}
        self.assertEqual((await self.worker(job, message, "another-run")).status_code, 403)
        for _ in range(2):
            self.assertEqual((await self.worker(job, message)).status_code, 200)
        self.assertEqual(len(self.calls), 1)
        mutated = copy.deepcopy(message); mutated["payload"]["arguments"]["path"] = "other"
        self.assertEqual((await self.worker(job, mutated)).status_code, 403)
        identity = {**message, "bot_id": "other"}
        self.assertEqual((await self.worker(job, identity)).status_code, 403)
        self.config.agents.get("bot").tools = []
        self.assertEqual((await self.worker(job, {**message, "sequence": 2, "event_id": "new"})).status_code, 403)
        token = job["token"]
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        job["token"] = token
        self.assertEqual((await self.worker(job, message)).status_code, 403)

    async def test_lease_completion_and_expiry_revoke(self):
        task, job = await self.job()
        claim = (await self.broker({"op": "claim", "health": {}, "instance_id": "fixture", "capacity": ["pi"]})).json()
        self.assertEqual(claim["spec"]["run_id"], self.run_id)
        wrong = await self.broker({"op": "renew", "job_id": claim["spec"]["job_id"], "lease": "wrong"})
        self.assertFalse(wrong.json()["active"])
        job["lease_until"] = time.time() - 1
        self.assertFalse((await self.broker({"op": "renew", "job_id": claim["spec"]["job_id"], "lease": claim["lease"]})).json()["active"])
        with self.assertRaisesRegex(RuntimeError, "broker_lease_expired"):
            await task

    def test_lease_window_outlives_a_stalled_host_and_outruns_its_renewal(self):
        """窗口必须能吸收一次宿主停顿；续约节奏要远快于窗口，续约调用超时要超过窗口。

        旧组合（窗口 12s、续约 2s、调用超时 10s）下，宿主一次内存压力停顿就会把工具已成功、
        结果已归档的任务判成 broker_lease_expired；这里把三个关系钉住，避免再退回那个组合。
        """
        self.assertGreaterEqual(execution.LEASE_SECONDS, 30)
        self.assertLessEqual(execution.LEASE_RENEW_SECONDS * 10, execution.LEASE_SECONDS)
        self.assertGreater(execution.LEASE_CALL_TIMEOUT, execution.LEASE_SECONDS)
        self.assertGreater(execution.LEASE_RETRY_SECONDS, execution.LEASE_CALL_TIMEOUT)
        self.assertLess(execution.STALL_SECONDS, execution.LEASE_SECONDS)
        # 容器内的看门狗（worker.py）只能靠 Broker 的心跳判断租约仍有效；它必须比 Broker
        # 放弃前的重试预算更宽，否则 Control 一停顿，容器就会把还在干活的自己杀掉
        # （实测表现为 worker_protocol_denied，而不是租约错误，真正的死因被掩盖）。
        from carme import worker
        self.assertGreater(worker.LEASE_SILENCE_SECONDS, execution.LEASE_RETRY_SECONDS)

    async def test_host_stall_credits_the_lease_instead_of_failing_a_live_job(self):
        """宿主停顿同时延迟本进程轮询与 Broker 续约，不能据此判定 Broker 已放弃。

        用 time.sleep 同步阻塞事件循环来复现宿主级停顿（真实故障里是内存压力/CPU 饥饿），
        然后要求两个判罚点都放行：Control 轮询不抛 broker_lease_expired，Broker 的 renew 与
        finish 仍 active，任务照常拿到结果。窗口被临时缩短，逻辑不改。
        """
        with patch.object(execution, "LEASE_SECONDS", 4):
            task, job = await self.job()
            claim = (await self.broker({"op": "claim", "health": {}, "instance_id": "fixture",
                                        "capacity": ["pi"]})).json()
            lease = claim["lease"]
            self.assertTrue(job["lease"] and job["lease_until"] > time.time())
            time.sleep(execution.LEASE_SECONDS + 2)
            self.assertLess(job["lease_until"], time.time() - 1)  # 墙钟上租约确实已过期
            await asyncio.sleep(0.05)  # 轮询任务先跑：它应当回补停顿而不是失败
            self.assertGreater(job["lease_until"], time.time())
            renewed = await self.broker({"op": "renew", "job_id": claim["spec"]["job_id"], "lease": lease})
            self.assertTrue(renewed.json()["active"])
            result = {"text": "fixture result after host stall"}
            finished = await self.broker({"op": "finish", "job_id": claim["spec"]["job_id"],
                                          "lease": lease, "result": result})
            self.assertTrue(finished.json()["active"])
            self.assertEqual(await task, result)

    async def test_stall_credit_never_outlives_the_task_deadline(self):
        """回补停顿不是无限期保活：任务 deadline 仍是绝对上界（即使 Broker 一直沉默）。"""
        with patch.object(execution, "LEASE_SECONDS", 4):
            task, job = await self.job()
            await self.broker({"op": "claim", "health": {}, "instance_id": "fixture", "capacity": ["pi"]})
            job["spec"]["deadline"] = time.time() - 1
            time.sleep(execution.LEASE_SECONDS + 2)
            await asyncio.sleep(0.05)
            with self.assertRaisesRegex(RuntimeError, "task_token_revoked"):
                await task


    async def test_broker_reissues_a_rejected_lease_request_but_never_a_claim(self):
        """Control 的 nonce 窗口会拒掉被停顿卡住的续约请求（403）。

        被拒请求从未进入处理器，所以续约类请求必须重签一次，否则工具已成功的任务会以
        HTTPStatusError 失败；claim 保持原样：它自己每 0.25s 重试，且 10s 超时不会过期。
        """
        broker = Broker({"home": str((self.root / "broker-home").resolve()), "instance_id": "fixture",
            "key_file": self.config.isolation["broker"]["key_file"], "docker_binary": "/bin/false",
            "docker_context": "fixture", "docker_config": str(self.root / "docker"),
            "control_url": "http://127.0.0.1:1234", "targets": ["action"],
            "images": {"pi": "sha256:" + "0" * 64, "action": "sha256:" + "0" * 64}})
        request = httpx.Request("POST", "http://127.0.0.1/internal/broker")
        rejected = httpx.Response(403, request=request)
        attempts = []

        async def flaky(data, timeout=None):
            attempts.append(data["op"])
            if len(attempts) == 1:
                raise httpx.HTTPStatusError("403", request=request, response=rejected)
            return {"active": True}

        broker.request = flaky
        self.assertEqual(await broker.call({"op": "renew", "job_id": "fixture", "lease": "fixture"}),
                         {"active": True})
        self.assertEqual(attempts, ["renew", "renew"])
        attempts.clear()
        with self.assertRaises(httpx.HTTPStatusError):
            await broker.call({"op": "claim"})
        self.assertEqual(attempts, ["claim"])

    async def test_missing_dedicated_credentials_prevents_worker_creation(self):
        with self.assertRaisesRegex(RuntimeError, "carme_auth_required"):
            await self.runtime.execution.pi(self.run_id, "fixture", m1.profile())
            await self.runtime.execution.pi(self.run_id, "fixture", m1.profile())
        self.assertFalse(self.runtime.execution.jobs)

    async def test_offline_broker_no_host_execution(self):
        self.runtime.execution.broker_seen = 0
        with self.assertRaisesRegex(RuntimeError, "container_runner_unavailable"):
            await self.runtime.execution.submit(self.run_id, "action", {"op": "exec"})
        self.assertFalse(self.runtime.execution.jobs)

    async def test_broker_rejects_injected_images_privileges_paths(self):
        task, job = await self.job()
        config = {"home": str(self.root / "dedicated"), "instance_id": "fixture", "key_file": self.config.isolation["broker"]["key_file"],
            "docker_binary": "/bin/false", "docker_context": "fixture", "docker_config": str(self.root / "docker"),
            "control_url": "http://127.0.0.1:1234", "targets": ["action"],
            "images": {"pi": "sha256:" + "0" * 64, "action": "sha256:" + "0" * 64}}
        for changes in ({"mount": "/"}, {"privileged": True}, {"network": "host"}, {"entrypoint": "sh"},
                        {"image_digest": "alpine:latest"}, {"run_id": "../escape"}, {"bot_id": "/tmp/x"},
                        {"target_id": "unknown"}, {"deadline": float("nan")}, {"max_output_bytes": True},
                        {"max_output_bytes": 999999999}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                validate_spec({**job["spec"], **changes}, config)
        broker = Broker(config)
        spec = {**job["spec"], "role": "action", "payload": {"op": "exec", "command": "true", "cwd": "/workspace", "timeout": 1}}
        name, args = broker.create_args(spec)
        self.assertIn("--network=none", args); self.assertIn("--read-only", args)
        self.assertNotIn("/var/run/docker.sock", " ".join(args))
        workspace = broker.home / "runtime/runs" / self.run_id / "workspace"
        workspace.rmdir(); workspace.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlink_mount_denied"):
            broker.create_args(spec)
        await broker.client.aclose()
        task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_artifact_exact_bytes_hash_acl_and_additive_schema(self):
        cid = self.store.create_conversation(["bot"], title="fixture")["id"]
        self.store._write("UPDATE tasks SET conversation_id=? WHERE id=?", (cid, self.run_id))
        raw = b"PK\x03\x04\x00\xff fixture binary"
        file = archive_binary(self.store, cid, "fixture.zip", raw, task_id=self.run_id)
        self.assertEqual(file_path(self.store, file["id"]).read_bytes(), raw)
        self.assertEqual(file["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(json.loads(file["provenance"])["status"], "unverified")
        with self.assertRaisesRegex(ValueError, "artifact_scope_denied"):
            archive_binary(self.store, "other", "fixture.zip", raw, task_id=self.run_id)
        for name in ("../secret.zip", "/tmp/secret.zip", "a\\b.zip"):
            with self.assertRaises(ValueError):
                archive_binary(self.store, cid, name, raw, task_id=self.run_id)

    async def test_concurrency_refuses_unsupported_pi_nesting(self):
        self.config.isolation["broker"]["max_pi"] = 1
        task, job = await self.job()
        with self.assertRaisesRegex(RuntimeError, "pi_concurrency_combination_unsupported"):
            await self.runtime.execution.submit(self.run_id, "pi", job["spec"]["payload"], profile=m1.profile())
        task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_control_rejects_local_cli_and_trusted_stdio_before_spawn(self):
        from carme.engines import _run_cli_process, build_argv
        from carme.mcp import MCPServer, _StdioTransport
        from carme.sandbox.local import LocalSandbox
        from carme.sandbox.base import SandboxSpec
        with patch.dict(os.environ, {"CARME_CONTAINER_CONTROL": "1"}), patch("asyncio.create_subprocess_exec", side_effect=AssertionError("Control spawned")):
            with self.assertRaisesRegex(RuntimeError, "local_execution_denied_in_control"):
                LocalSandbox(SandboxSpec("bot", "run", "local", {"trusted_host": True, "root": str(self.root/'denied')}))
            with self.assertRaisesRegex(RuntimeError, "stdio_denied_in_control"):
                await _StdioTransport(MCPServer("fixture", command=sys.executable, trusted_host=True)).start()
            with self.assertRaisesRegex(RuntimeError, "cli_process_denied_in_control"):
                await _run_cli_process("pi", "fixture", binary="/not-executed/pi")
        self.assertFalse((self.root/'denied').exists())
        argv = build_argv("pi", "/fixed/pi", "fixture/model")
        self.assertIn("--system-prompt", argv)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "")

    async def test_container_denies_unrestricted_control_http_tools(self):
        self.config.agents.get("bot").tools = ["browser", "files", "exec", "mcp"]
        policy = self.runtime.policy_for("bot", target="container", node={})
        self.assertNotIn("fetch_page", policy["tools"])
        self.assertNotIn("web_search", policy["tools"])
        self.assertEqual(policy["network"], "none")

    async def test_gateway_rejects_private_endpoints_redirect_configuration_and_personal_files(self):
        key = self.root / "dedicated-key"; key.write_text("synthetic-only")
        entry = {"protocol": "openai-completions", "base_url": "https://127.0.0.1/v1",
                 "models": ["fixture/model"], "key_file": str(key)}
        self.config.isolation["credentials"] = {"fixture-key": entry}
        task, job = await self.job()
        with patch.dict(os.environ, {"CARME_CREDENTIALS_DIR": str(self.root)}):
            for url in ("http://127.0.0.1/v1", "https://127.0.0.1/v1", "https://[::1]/v1", "https://169.254.169.254/v1", "https://user:pass@example.com/v1"):
                entry["base_url"] = url
                with self.subTest(url=url), self.assertRaises((RuntimeError, ValueError)):
                    await self.runtime.execution.model(job, {"body": {"model": "model", "messages": []}})
            entry["proxy_url"] = "http://localhost:1234"
            with self.assertRaisesRegex(RuntimeError, "credential_configuration_unsupported"):
                self.runtime.execution.credential(m1.profile())
            del entry["proxy_url"]
            personal = self.root / ".pi"; personal.mkdir(); (personal/'auth.json').write_text("not-read")
            entry["key_file"] = str(personal/'auth.json')
            with self.assertRaisesRegex(RuntimeError, "carme_auth_required"):
                self.runtime.execution.credential(m1.profile())
        task.cancel(); await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    # The M1 suite runs separately; do not count its inherited tests twice.
    suite = unittest.TestSuite(M2(name) for name in M2.__dict__ if name.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
