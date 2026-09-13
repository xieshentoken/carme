"""HTTP-route regressions for CLI-only Bot saves and stale API references."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"

from carme import config as config_module  # noqa: E402
from carme.api.routes import build_router, require_token  # noqa: E402
from carme.bus import EventBus  # noqa: E402
from carme.runtime import Runtime  # noqa: E402
from carme.store import Store  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="carme-agent-engine-routes-") as directory:
        root = Path(directory)
        config_module.CONFIG_DIR = root / "config"
        config_module.ENV_FILE = root / ".env"
        config_module.CONFIG_DIR.mkdir()
        (root / "config" / "models.yaml").write_text(yaml.safe_dump({
            "providers": {}, "tiers": {}, "models": [], "budget": {"daily_usd": 0},
        }), encoding="utf-8")
        (root / "config" / "agents.yaml").write_text(yaml.safe_dump({"agents": {
            "legacy": {
                "name": "Legacy", "title": "fixture", "prompt": "fixture",
                "entry": True, "tier": "missing-tier", "model": "old/missing",
                "effort": "high", "sandbox": "none", "tools": [],
            },
        }}), encoding="utf-8")
        config = config_module.load(reload=True)
        store = Store(root / "data" / "carme.db")
        runtime = Runtime(config, store, EventBus())
        app = FastAPI()
        app.include_router(build_router(config, store, runtime), dependencies=[Depends(require_token)])
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://carme.test")
        try:
            cli_only = await client.post("/api/agents", json={
                "id": "cli_only", "name": "CLI Only", "prompt": "fixture",
                "engine": "codex", "engine_model": "gpt-test", "engine_effort": "low",
                "tools": [], "sandbox": "none",
            })
            assert cli_only.status_code == 201, cli_only.text
            saved = cli_only.json()["agent"]
            assert saved["engine"] == "codex" and saved["engine_effort"] == "low"

            switched = await client.patch("/api/agents/legacy", json={
                "engine": "codex", "engine_model": "gpt-test", "engine_effort": "low",
            })
            assert switched.status_code == 200, switched.text
            saved = switched.json()["agent"]
            assert saved["model"] == "old/missing" and saved["effort"] == "high"
            assert saved["engine_effort"] == "low"

            task_id = store.create_task("cli_only", "unknown-cost fixture")
            store.add_task_usage(task_id, 0, 0, cost_known=False, tokens_known=False)
            listed = (await client.get("/api/tasks")).json()["tasks"]
            task = next(item for item in listed if item["id"] == task_id)
            assert task["cost_known"] is False and task["tokens_known"] is False
        finally:
            await client.aclose()
            await runtime.shutdown()
            store.close()
    print("PASS: CLI-only saves skip API model/tier/effort validation and preserve stale API settings")


if __name__ == "__main__":
    asyncio.run(main())
