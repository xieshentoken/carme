"""Cloudflare named Tunnel 配置与鉴权边界测试；不接 Cloudflare、不重启 8899。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from carme.api.routes import (  # noqa: E402
    _cloudflare_hostname,
    _cloudflare_origin_matches,
    _read_cloudflare_config,
)

import yaml  # noqa: E402
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from carme import config as config_module  # noqa: E402
from carme.api.routes import build_router  # noqa: E402
from carme.bus import EventBus  # noqa: E402
from carme.store import Store  # noqa: E402


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "cloudflared" / "carme-tunnel.sh"


def write_config(root: Path, **changes) -> tuple[Path, Path]:
    credentials = root / "tunnel-credentials.json"
    credentials.write_text("{}\n", encoding="utf-8")
    raw = {
        "tunnel": "carme",
        "credentials-file": str(credentials),
        "protocol": "http2",
        "ingress": [
            {
                "hostname": "carme.example.com",
                "service": "http://127.0.0.1:8899",
                "originRequest": {
                    "access": {
                        "required": True,
                        "teamName": "carme-team",
                        "audTag": ["audience-test"],
                    }
                },
            },
            {"service": "http_status:404"},
        ],
    }
    raw.update(changes)
    config_path = root / "carme-tunnel.yml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return config_path, credentials


class CloudflareConfigTests(unittest.TestCase):
    def test_valid_config_requires_real_credentials_and_access_fields(self):
        with tempfile.TemporaryDirectory(prefix="carme-cloudflare-test-") as directory:
            config_path, credentials = write_config(Path(directory))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CARME_CLOUDFLARE_HOSTNAME", None)
                result = _read_cloudflare_config(config_path)
            self.assertTrue(result["valid"])
            self.assertTrue(result["credentials_present"])
            self.assertEqual(result["hostname"], "carme.example.com")
            credentials.unlink()
            self.assertFalse(_read_cloudflare_config(config_path)["credentials_present"])

    def test_invalid_origin_access_protocol_and_hostname_are_not_accepted(self):
        with tempfile.TemporaryDirectory(prefix="carme-cloudflare-test-") as directory:
            root = Path(directory)
            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

            raw["ingress"][0]["service"] = "http://localhost:8899"
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            self.assertFalse(_read_cloudflare_config(config_path)["origin_target_ok"])

            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"][0]["originRequest"]["access"]["required"] = False
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            self.assertFalse(_read_cloudflare_config(config_path)["access_configured"])

            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"][0]["path"] = "/foo"
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            self.assertFalse(_read_cloudflare_config(config_path)["origin_target_ok"])

            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"].insert(1, deepcopy(raw["ingress"][0]))
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            duplicate = _read_cloudflare_config(config_path)
            self.assertFalse(duplicate["origin_target_ok"])
            self.assertFalse(duplicate["structural_valid"])

            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"].insert(0, {"path": "/api/.*", "service": "http://127.0.0.1:8899"})
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            missing_hostname = _read_cloudflare_config(config_path)
            self.assertFalse(missing_hostname["origin_target_ok"])
            self.assertFalse(missing_hostname["valid"])

            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"] = [raw["ingress"][1], raw["ingress"][0]]
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            self.assertFalse(_read_cloudflare_config(config_path)["fallback_present"])

            config_path, _ = write_config(root, protocol="udp")
            self.assertFalse(_read_cloudflare_config(config_path)["structural_valid"])

            config_path, _ = write_config(root)
            with patch.dict(os.environ, {"CARME_CLOUDFLARE_HOSTNAME": "other.example.com"}):
                result = _read_cloudflare_config(config_path)
            self.assertTrue(result["hostname_mismatch"])
            self.assertFalse(result["hostname_configured"])

    def test_hostname_and_origin_validation_is_strict(self):
        self.assertEqual(_cloudflare_hostname("Carme.Example.COM."), "carme.example.com")
        self.assertEqual(_cloudflare_hostname("carme..example.com"), "")
        self.assertEqual(_cloudflare_hostname("https://carme.example.com"), "")
        self.assertTrue(_cloudflare_origin_matches("http://127.0.0.1:8899/"))
        self.assertFalse(_cloudflare_origin_matches("http://localhost:8899"))
        self.assertFalse(_cloudflare_origin_matches("http://127.0.0.1:8899/api"))
        self.assertFalse(_cloudflare_origin_matches("http://user@127.0.0.1:8899"))

    def test_session_cookie_is_token_bound_without_revealing_token(self):
        with tempfile.TemporaryDirectory(prefix="carme-session-test-") as directory:
            store = Store(Path(directory) / "session.db")
            try:
                first, _ = store.create_session("synthetic-token")
                second, _ = store.create_session("synthetic-token")
                self.assertNotEqual(first, second)
                self.assertIsNotNone(store.session(first, "synthetic-token"))
                self.assertIsNone(store.session(first, "different-token"))
                self.assertNotIn("synthetic-token", first)
            finally:
                store.close()

    def test_put_rejects_nonexclusive_ingress_without_mutating_bytes(self):
        class RuntimeStub:
            bus = EventBus()

            async def _emit(self, *_args, **_kwargs):
                return None

        async def call_put(config_path: Path, credentials: Path) -> httpx.Response:
            cfg = config_module.Config(config_module.AgentsConfig(), config_module.ModelsConfig(), config_module.SandboxConfig())
            store = Store(config_path.parent / "test.db")
            try:
                app = FastAPI()
                app.include_router(build_router(cfg, store, RuntimeStub()))
                body = {
                    "tunnel": "carme-new",
                    "credentials_file": str(credentials),
                    "hostname": "carme.example.com",
                    "protocol": "http2",
                    "team_name": "carme-team",
                    "audience_tag": "audience-test",
                }
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    return await client.put("/api/cloudflare/config", json=body)
            finally:
                store.close()

        with tempfile.TemporaryDirectory(prefix="carme-cloudflare-put-") as directory:
            root = Path(directory)
            cases = []
            config_path, credentials = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"].insert(1, deepcopy(raw["ingress"][0]))
            cases.append(raw)
            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"][0]["path"] = "/foo"
            cases.append(raw)
            config_path, _ = write_config(root)
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"][0]["originRequest"]["access"]["audTag"].append("second-audience")
            cases.append(raw)
            for index, raw in enumerate(cases):
                config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
                before = config_path.read_bytes()
                with patch.dict(os.environ, {"CARME_CLOUDFLARE_CONFIG": str(config_path)}, clear=False):
                    response = asyncio.run(call_put(config_path, credentials))
                self.assertEqual(response.status_code, 409, f"case {index}: {response.text}")
                self.assertEqual(config_path.read_bytes(), before, f"case {index} mutated the rejected file")


class CloudflareScriptTests(unittest.TestCase):
    def test_script_syntax_status_and_empty_token_guard(self):
        syntax = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        with tempfile.TemporaryDirectory(prefix="carme-cloudflare-script-") as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            cloudflared = fake_bin / "cloudflared"
            cloudflared.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            cloudflared.chmod(cloudflared.stat().st_mode | stat.S_IXUSR)
            env_file = root / ".env"
            env_file.write_text("CARME_TOKEN=\"\"\n", encoding="utf-8")
            config_path, _ = write_config(root)
            env = os.environ.copy()
            env.pop("CARME_TOKEN", None)
            env.update({
                "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                "CARME_ENV_FILE": str(env_file),
                "CARME_CLOUDFLARE_CONFIG": str(config_path),
                "CARME_CLOUDFLARE_HOSTNAME": "carme.example.com",
                "CARME_CLOUDFLARE_PID_FILE": str(root / "cloudflared.pid"),
                "CARME_CLOUDFLARE_LOG_FILE": str(root / "cloudflared.log"),
            })
            result = subprocess.run([str(SCRIPT), "check"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("未检测到 CARME_TOKEN", result.stderr)

            env_file.write_text("CARME_TOKEN='synthetic token'\n", encoding="utf-8")
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            raw["ingress"][0]["path"] = "/foo"
            config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
            result = subprocess.run([str(SCRIPT), "check"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("必须恰好有一个", result.stderr)
            self.assertNotIn("synthetic token", result.stdout + result.stderr)

            write_config(root)
            env_file.write_text("export CARME_TOKEN='synthetic token'\n", encoding="utf-8")
            result = subprocess.run([str(SCRIPT), "check"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cloudflare ingress 已验证", result.stdout)
            self.assertIn("8899", result.stderr)
            self.assertNotIn("synthetic token", result.stdout + result.stderr)

            env_file.write_text("CARME_TOKEN=first\nCARME_TOKEN='synthetic token'\n", encoding="utf-8")
            result = subprocess.run([str(SCRIPT), "check"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cloudflare ingress 已验证", result.stdout)
            self.assertNotIn("synthetic token", result.stdout + result.stderr)

            env_file.write_text("CARME_TOKEN=first\nCARME_TOKEN=\n", encoding="utf-8")
            result = subprocess.run([str(SCRIPT), "check"], env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("未检测到 CARME_TOKEN", result.stderr)


if __name__ == "__main__":
    unittest.main()
