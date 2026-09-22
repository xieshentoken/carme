"""持续会话/API/SSE 的隔离测试；不加载用户 .env、不连接模型、浏览器或 SSH。"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"

import httpx
import yaml
from fastapi import FastAPI
from starlette.requests import Request as HTTPRequest

from carme import config as config_module
from carme.api.routes import build_router
from carme.bus import EventBus
from carme.store import SCHEMA, Store
from carme.tools import build_registry


def test_store(root: Path) -> None:
    path = root / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("INSERT INTO tasks (id,agent_id,goal,result,created_at) VALUES ('legacy','a','old','keep',1)")
    connection.commit()
    connection.close()
    store = Store(path)
    assert store.get_task("legacy")["result"] == "keep"
    assert store.get_task("legacy")["conversation_id"] == ""
    conversation = store.create_conversation(["a", "b"], title="Team")
    cid = conversation["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda _: store.create_conversation_turn(cid, "a", "first", "request-1"), range(20)))
    assert sum(reply["created"] for reply in replies) == 1
    task_id = replies[0]["task_id"]
    assert len(store.list_conversation_messages(cid)) == 1
    store.add_message(task_id, "a", "tool", "private transcript", tool_name="shell")
    assert len(store.list_conversation_messages(cid)) == 1
    second = store.create_conversation_turn(cid, "a", "adjust", "request-2", active_task_id=task_id)
    assert second["steering"] and second["task_id"] == task_id
    store.finish_task(task_id, "finished")
    third = store.create_conversation_turn(cid, "a", "next", "request-3", active_task_id=task_id)
    assert not third["steering"] and third["task_id"] != task_id
    child = store.create_task("b", "child", parent_id=third["task_id"])
    assert store.list_conversations()[0]["active_agent_ids"] == []  # Queued is not executing.
    store.set_task_status(third["task_id"], "running")
    store.set_task_status(child, "running")
    assert set(store.list_conversations()[0]["active_agent_ids"]) == {"a", "b"}
    store.set_task_status(child, "waiting_approval")
    assert store.list_conversations()[0]["active_agent_ids"] == ["a"]
    store.set_task_status(third["task_id"], "cancelled")
    assert store.list_conversations()[0]["active_agent_ids"] == []
    approval = store.create_approval(task_id=child, agent_id="b", kind="shell", summary="check")
    assert store.list_conversation_approvals(cid)[0]["id"] == approval
    with ThreadPoolExecutor(max_workers=2) as pool:
        choices = list(pool.map(lambda _: store.decide_approval(approval, approved=True), range(2)))
    assert choices.count(True) == 1
    store.add_conversation_message(cid, "a", "assistant", "answer", task_id=task_id)
    store.close()
    store = Store(path)
    assert store.get_conversation(cid)["agent_ids"] == ["a", "b"]
    assert not store.create_conversation_turn(cid, "a", "first", "request-1")["created"]
    try:
        store.create_conversation_turn(cid, "a", "different", "request-1")
        raise AssertionError("Request reuse must be rejected")
    except ValueError:
        pass
    assert store.list_conversation_messages(cid)[-1]["content"] == "answer"
    before = store.list_conversation_messages(cid)
    store.remember('a', 'role_memory', 'keep')
    store.update_conversation(cid, {'pinned': True, 'folder': '研究', 'unread': True})
    assert store.list_conversations()[0]['pinned_at'] > 0
    store.update_conversation(cid, {'hidden': True})
    assert not store.list_conversations() and store.list_conversations('hidden')[0]['folder'] == '研究'
    try:
        store.update_conversation(cid, {'deleted': True})
        raise AssertionError('Active tasks must block deletion')
    except ValueError:
        pass
    store.finish_task(third['task_id'], 'done'); store.finish_task(child, 'done')
    store.update_conversation(cid, {'deleted': True})
    assert not store.list_conversations('hidden') and store.list_conversations('deleted')[0]['id'] == cid
    try:
        store.create_conversation_turn(cid, 'a', 'must not run', 'deleted-request')
        raise AssertionError('Deleted conversations cannot accept messages')
    except ValueError:
        pass
    store.close(); store = Store(path)
    restored = store.update_conversation(cid, {'deleted': False, 'pinned': False, 'unread': False})
    assert not restored['hidden'] and not restored['pinned_at'] and not restored['unread']
    assert store.list_conversation_messages(cid) == before and store.recall('a')[0]['value'] == 'keep'
    assert store.list_conversations()[0]['folder'] == '研究'
    store.close()


class FakeRuntime:
    def __init__(self, config, store):
        self.config, self.store, self.bus = config, store, EventBus()
        self.registry = build_registry()
        self.submissions = 0
        # /browser/probe 与 /sandbox/probe 在本机模式下会调用 Runtime 的同名接口，
        # 测试替身需要提供与生产 Runtime 一致的最小接口。
        self.browsers = SimpleNamespace(probe=self.probe_browser)
        self.notifier = SimpleNamespace(describe=lambda: {"enabled": False, "channels": []})
        self.sandboxes = SimpleNamespace(probe=self.probe_sandbox, probe_node=self.probe_node)

    async def probe_browser(self, node=None):
        return {"ok": False, "status": "unavailable", "error": "测试环境不启动浏览器", "node": None}

    async def probe_sandbox(self, mode="local"):
        return {"ok": False, "mode": mode, "error": "测试环境不启动沙箱"}
        return {"ok": False, "mode": mode, "error": "测试环境不启动沙箱"}

    async def _emit(self, type_, payload, task_id="", agent_id=""):
        event = self.store.add_event(type_, payload, task_id=task_id, agent_id=agent_id)
        await self.bus.publish(event)

    async def submit_message(self, cid, content, request_id, agent_id=None):
        agent_id = agent_id or self.store.get_conversation(cid)["agent_ids"][0]
        result = self.store.create_conversation_turn(
            cid, agent_id, content, request_id,
            meta={"node": {"node_id": "node-a", "name": "MacBook", "identity_file": "/secret/key"}},
        )
        self.submissions += int(result["created"])
        return result

    async def probe_node(self, node_id=None):
        try:
            self.config.sandbox.resolve_node(node_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        raise AssertionError("No configured SSH node may be probed by this test")


async def test_api(root: Path) -> None:
    directory = root / "config"
    directory.mkdir()
    config_module.CONFIG_DIR = directory
    config_module._cache = None
    os.environ["CARME_TEST_SECRET"] = "synthetic-test-value-not-a-real-key"
    (directory / "agents.yaml").write_text(yaml.safe_dump({
        "defaults": {"max_steps": 8},
        "agents": {"a": {"name": "Alpha", "tier": "balanced", "prompt": "${PRESERVE_RAW_REFERENCE}"},
                   "b": {"name": "Beta", "tier": "balanced"}},
    }))
    (directory / "models.yaml").write_text(yaml.safe_dump({
        "providers": {"test": {"base_url": "https://example.invalid", "api_key_env": "TEST_UNUSED_KEY"}},
        "tiers": {"balanced": {"candidates": ["test/model"]}},
    }))
    config = config_module.load(reload=True)
    store = Store(root / "api.db")
    runtime = FakeRuntime(config, store)
    app = FastAPI()
    router = build_router(config, store, runtime)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/conversations", json={"agent_ids": ["a", "b"]})
        assert response.status_code == 201, response.text
        cid = response.json()["conversation"]["id"]
        path = f"/api/conversations/{cid}/messages"
        first = await client.post(path, json={"content": "hello", "request_id": "unique"})
        retry = await client.post(path, json={"content": "hello", "request_id": "unique"})
        first_result, retry_result = first.json(), retry.json()
        assert first_result["task_id"] == retry_result["task_id"]
        assert first_result["message_id"] == retry_result["message_id"]
        assert first_result["created"] is True and first_result["steering"] is False
        assert retry_result["created"] is False
        assert runtime.submissions == 1
        conflict = await client.post(path, json={"content": "different", "request_id": "unique"})
        assert conflict.status_code == 409
        detail = (await client.get(f"/api/conversations/{cid}")).json()
        assert detail["conversation"]["agent_ids"] == ["a", "b"]
        assert len(detail["messages"]) == 1
        assert detail["tasks"][0]["node_id"] == "node-a"
        assert "/secret/key" not in json.dumps(detail)
        created = await client.post("/api/agents", json={"id": "c", "name": "Gamma", "model": "test/model", "prompt": "${CARME_TEST_SECRET}"})
        assert created.status_code == 201, created.text
        assert created.json()["agent"]["prompt"] == "${CARME_TEST_SECRET}"
        updated = await client.patch("/api/agents/c", json={"title": "Editor", "entry": True})
        assert updated.json()["agent"]["title"] == "Editor"
        store.remember('c', 'private', 'original only')
        duplicated = await client.post('/api/agents/c/duplicate')
        assert duplicated.status_code == 201, duplicated.text
        clone = duplicated.json()['agent']; clone_cid = duplicated.json()['conversation']['id']
        assert clone['id'] != 'c' and clone['prompt'] == '${CARME_TEST_SECRET}' and not clone['entry']
        assert store.recall(clone['id']) == [] and store.list_conversation_messages(clone_cid) == []
        changed = await client.patch(f'/api/conversations/{clone_cid}', json={'pinned': True, 'folder': '工作', 'unread': True})
        assert changed.status_code == 200 and changed.json()['conversation']['folder'] == '工作'
        assert (await client.patch(f'/api/conversations/{clone_cid}', json={'deleted': True})).status_code == 200
        assert len((await client.get('/api/conversations?view=deleted')).json()['conversations']) == 1
        assert (await client.patch(f'/api/conversations/{clone_cid}', json={'deleted': False})).status_code == 200
        assert (await client.get('/api/conversations?view=invalid')).status_code == 422
        assert (await client.patch(f'/api/conversations/{clone_cid}', json={'unknown': 'x'})).status_code == 422
        assert config.agents.entry_agent.id == "c"
        raw = yaml.safe_load((directory / "agents.yaml").read_text())
        assert raw["agents"]["a"]["prompt"] == "${PRESERVE_RAW_REFERENCE}"
        assert raw["defaults"]["max_steps"] == 8
        assert (await client.post("/api/agents", json={"id": "c", "name": "again"})).status_code == 409
        assert (await client.patch("/api/agents/c", json={"api_key": "do not save"})).status_code == 422
        assert (await client.patch("/api/agents/c", json={"model": "missing/model"})).status_code == 422
        assert (await client.patch("/api/agents/c", json={"tools": ["nonexistent_tool"]})).status_code == 422
        assert (await client.get("/api/nodes")).json()["nodes"] == []
        assert not (await client.get("/api/browser/probe")).json()["ok"]
        assert not (await client.post("/api/sandbox/probe", json={})).json()["ok"]
        created_node = await client.post("/api/nodes", json={"node_id": "draft", "name": "New Mac"})
        assert created_node.status_code == 201, created_node.text
        probe = await client.post("/api/nodes/draft/probe")
        assert not probe.json()["ok"]
        assert (await client.post("/api/nodes/draft/default")).status_code == 422
        patch = await client.patch("/api/nodes/draft", json={"name": "Renamed"})
        assert patch.status_code == 200, patch.text
        assert patch.json()["nodes"][0]["name"] == "Renamed"

    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/events")
    os.environ["CARME_DEV_NO_AUTH"] = "1"
    class Request(HTTPRequest):
        fixture_headers = {"last-event-id": "0"}
        def __init__(self):
            super().__init__({"type": "http", "method": "GET", "scheme": "http", "path": "/api/events",
                "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 5000), "query_string": b"",
                "headers": [(k.encode(), v.encode()) for k, v in self.fixture_headers.items()], "app": app})
        async def is_disconnected(self):
            return False
    previous = store.max_event_id()
    await runtime._emit("first", {}, "task-1")
    response = await endpoint(Request(), after_id=previous)
    stream = response.body_iterator
    assert await anext(stream) == ": connected\n\n"
    item = await anext(stream)
    assert f"id: {previous + 1}\n" in item
    # 超过内存队列容量的事件全部应从数据库补发。
    for index in range(700):
        await runtime._emit("test", {"index": index}, "task-1")
    ids = []
    for _ in range(700):
        item = await anext(stream)
        ids.append(int(item.splitlines()[0].split(": ")[1]))
    assert ids == list(range(previous + 2, previous + 702))
    await stream.aclose()
    assert runtime.bus.subscriber_count == 0
    # Last-Event-ID 比原 URL 中 after_id 更新时，以前者为准。
    Request.fixture_headers = {"last-event-id": str(ids[-2])}
    response = await endpoint(Request(), after_id=previous)
    stream = response.body_iterator
    await anext(stream)
    assert f"id: {ids[-1]}\n" in await anext(stream)
    await stream.aclose()
    store.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="carme-conversations-") as directory:
        root = Path(directory)
        test_store(root)
        asyncio.run(test_api(root))
    print("PASS: additive migration, conversation persistence, idempotency, steering, approvals, API, safe config edits, SSE replay/overflow")
