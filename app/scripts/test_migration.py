"""迁移验收：导出包结构、导入合并策略、跨设备降级与边界。

只使用隔离的临时配置目录与数据库，不访问网络，也不写本机真实配置。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"
os.environ.pop("CARME_TOKEN", None)
os.environ["FIXTURE_MODEL_KEY"] = "fixture-key"

import httpx  # noqa: E402
import yaml  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from PIL import Image  # noqa: E402
from carme import config as config_module  # noqa: E402
from carme.api.routes import build_router  # noqa: E402
from carme.bus import EventBus  # noqa: E402
from carme.store import Store  # noqa: E402
from carme.tools.base import TOOL_GROUPS  # noqa: E402


def registry_names() -> set[str]:
    from carme.tools import build_registry
    return set(build_registry().names())


class FakeRuntime:
    """只提供迁移路径真正用到的最小接口。"""

    def __init__(self, config, store):
        from carme.tools import build_registry
        self.config, self.store, self.bus = config, store, EventBus()
        self.registry = build_registry()
        self.events: list[tuple[str, dict]] = []

    async def _emit(self, type_, payload, task_id="", agent_id=""):
        self.events.append((type_, payload))


def webp_data_uri(size: int = 64) -> str:
    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (200, 120, 80, 255)).save(buffer, "WEBP")
    return "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


async def main(root: Path) -> None:
    directory = root / "config"
    directory.mkdir()
    config_module.CONFIG_DIR = directory
    config_module._cache = None
    (directory / "agents.yaml").write_text(yaml.safe_dump({
        "defaults": {"max_steps": 8},
        "agents": {
            "alpha": {"name": "Alpha", "title": "研究员", "prompt": "你是 Alpha", "tier": "balanced",
                      "model": "test/model", "tools": ["memory", "browser"], "can_delegate": True,
                      "avatar": {"kind": "bot", "shape": "circle", "color": "#3aa76d"}},
            "beta": {"name": "Beta", "title": "工程师", "prompt": "你是 Beta", "tier": "balanced"},
        },
    }, allow_unicode=True))
    (directory / "models.yaml").write_text(yaml.safe_dump({
        "providers": {"test": {"label": "Test", "type": "openai_compatible",
                               "base_url": "https://api.test.invalid/v1", "api_key_env": "FIXTURE_MODEL_KEY"}},
        "tiers": {"balanced": {"candidates": ["test/model"]}},
    }, allow_unicode=True))
    config = config_module.load(reload=True)
    store = Store(root / "data" / "carme.db")
    runtime = FakeRuntime(config, store)
    app = FastAPI()
    app.include_router(build_router(config, store, runtime))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    # ---------- 造数据：记忆 + 单聊 + 群聊 + 摘要 ----------
    store.remember("alpha", "偏好语言", "中文回答")
    store.remember("alpha", "项目代号", "carme")
    store.remember("beta", "只在 Beta 里", "不应出现在 alpha 的导出中")
    direct = store.create_conversation(["alpha"], title="Alpha 单聊")
    group = store.create_conversation(["alpha", "beta"], title="研究小组")
    for cid, rows in ((direct["id"], [("user", "你好", "alpha"), ("assistant", "你好，我是 Alpha", "alpha")]),
                      (group["id"], [("user", "一起看看", "alpha"), ("assistant", "收到", "beta")])):
        for role, content, agent_id in rows:
            store._write("INSERT INTO conversation_messages (id, conversation_id, agent_id, role, content, task_id, created_at) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (f"m_{cid}_{role}_{content[:4]}", cid, agent_id, role, content, "", 1_700_000_000.0))
    store.save_summary(direct["id"], "这里是摘要内容", 2, "test/model")

    # ---------- 导出单个 Bot ----------
    exported = (await client.get("/api/agents/alpha/export")).json()
    assert exported["carme_export"] == 1 and exported["kind"] == "agents", exported
    assert [entry["id"] for entry in exported["agents"]] == ["alpha"], exported["agents"]
    profile = exported["agents"][0]["profile"]
    assert profile["name"] == "Alpha" and profile["model"] == "test/model" and profile["can_delegate"] is True
    assert profile["avatar"] == {"kind": "bot", "shape": "circle", "color": "#3aa76d"}
    memory_keys = {entry["key"] for entry in exported["agents"][0]["memory"]}
    assert memory_keys == {"偏好语言", "项目代号"}, memory_keys
    # 只导出该 Bot 参与的会话：单聊 + 群聊
    titles = sorted(item["title"] for item in exported["conversations"])
    assert titles == ["Alpha 单聊", "研究小组"], titles
    summary = next(item for item in exported["conversations"] if item["title"] == "Alpha 单聊")["summary"]
    assert summary and summary["content"] == "这里是摘要内容" and summary["through_seq"] == 2
    assert all("不应出现" not in entry["value"] for entry in exported["agents"][0]["memory"])
    # 导出必须可 JSON 序列化（前端要直接下载成文件）
    text = json.dumps(exported, ensure_ascii=False)
    assert "carme_export" in text and len(text) > 100
    print("PASS: 单 Bot 导出包含角色、记忆、所参与会话与摘要，且不串其他 Bot 的记忆")

    # ---------- 导出全部 ----------
    everything = (await client.get("/api/export")).json()
    assert sorted(entry["id"] for entry in everything["agents"]) == ["alpha", "beta"]
    assert len(everything["conversations"]) == 2
    assert "avatar_image" in everything["agents"][0] and everything["agents"][0]["avatar_image"] == ""
    print("PASS: 全部导出覆盖两个 Bot 与两个会话")

    # ---------- 导入：round trip（同机合并，会话必须新建） ----------
    before_ids = {row["id"] for row in store.list_conversations()}
    imported = (await client.post("/api/import", json={"bundle": everything})).json()
    report = imported["report"]
    assert imported["ok"] and report["memory_count"] == 3, report
    assert [item["profile"] for item in report["agents"]] == ["updated", "updated"], report["agents"]
    assert len(report["conversations"]) == 2, report["conversations"]
    after_ids = {row["id"] for row in store.list_conversations()}
    assert len(after_ids) == 4 and not (before_ids - after_ids)
    imported_ids = {item["id"] for item in report["conversations"]}
    assert not (imported_ids & before_ids), "导入必须生成新会话 id，不得覆盖本机会话"
    # 消息与摘要跟着一起进来
    restored = store.get_conversation(report["conversations"][0]["id"])
    assert restored["agent_ids"] == ["alpha"]
    assert len(store.list_conversation_messages(restored["id"])) == 2
    assert store.get_summary(restored["id"])["content"] == "这里是摘要内容"
    assert len(store.recall("alpha")) == 2
    print("PASS: 导入生成新会话 id、恢复消息与摘要，且不覆盖本机会话")

    # ---------- profile_mode = skip ----------
    skipped = (await client.post("/api/import", json={
        "bundle": everything, "profile_mode": "skip", "import_conversations": False})).json()["report"]
    assert [item["profile"] for item in skipped["agents"]] == ["skipped", "skipped"], skipped["agents"]
    assert skipped["memory_count"] == 3 and not skipped["conversations"]
    assert (await client.post("/api/import", json={
        "bundle": everything, "profile_mode": "skip", "import_memory": False})).json()["report"]["memory_count"] == 0
    print("PASS: skip 策略不覆盖本机角色，且可选择不导入记忆/会话")

    # ---------- 跨设备降级：模型、档位、工具、引擎都不存在 ----------
    foreign = {"carme_export": 1, "kind": "agents", "agents": [{
        "id": "gamma", "profile": {
            "id": "gamma", "name": "Gamma", "title": "", "emoji": "🔧", "prompt": "来自另一台设备",
            "tier": "turbo", "model": "ghost/nowhere", "effort": "ultra", "engine": "warp",
            "sandbox": "quantum", "tools": ["memory", "telepathy"], "can_delegate": True, "entry": False,
        }, "memory": [{"key": "旧记忆", "value": "跨设备迁移", "created_at": 1_690_000_000.0,
                       "updated_at": 1_690_000_100.0}],
    }], "conversations": []}
    report = (await client.post("/api/import", json={"bundle": foreign})).json()["report"]
    item = report["agents"][0]
    assert item["profile"] == "created", item
    notes = " ".join(item["notes"])
    created = config_module.load(reload=True).agents.get("gamma")
    assert created.engine == "api" and created.sandbox == "local", created
    assert created.model in ("", None) and created.tier == "balanced", (created.model, created.tier)
    assert list(created.tools) == ["memory"], created.tools
    for expected in ("模型「ghost/nowhere」", "档位「turbo」", "telepathy", "引擎「warp」", "执行环境「quantum」"):
        assert expected in notes, (expected, notes)
    assert store.recall("gamma")[0]["key"] == "旧记忆"
    assert store.recall("gamma")[0]["updated_at"] == 1_690_000_100.0
    print("PASS: 跨设备导入就地降级并在报告里逐条说明，不因缺失依赖而整包失败")

    # ---------- 头像图片内嵌迁移 ----------
    with_avatar = {"carme_export": 1, "kind": "agents", "agents": [{
        "id": "delta", "profile": {"name": "Delta", "tier": "balanced", "tools": []},
        "memory": [], "avatar_image": webp_data_uri(),
    }], "conversations": [{"title": "Delta 会话", "agent_ids": ["delta", "alpha"],
                           "messages": [{"role": "user", "content": "hi"},
                                        {"role": "tool", "content": "应被丢弃"},
                                        {"role": "assistant", "content": "ok", "agent_id": "delta"}]}]}
    report = (await client.post("/api/import", json={"bundle": with_avatar})).json()["report"]
    delta = config_module.load(reload=True).agents.get("delta")
    assert delta.avatar.get("kind") == "image", delta.avatar
    avatar_path = store.path.parent / "avatars" / delta.avatar["file"]
    assert avatar_path.is_file() and avatar_path.stat().st_size > 0
    assert report["conversations"][0]["messages"] == 2, "非对话角色（tool）必须被丢弃"
    assert report["conversations"][0]["agent_ids"] == ["delta", "alpha"]
    print("PASS: 内嵌头像图片被正确校验写回，未知角色的消息被丢弃")

    # ---------- 边界：坏包、缺 Bot、超量 ----------
    bad = await client.post("/api/import", json={"bundle": {"agents": []}})
    assert bad.status_code == 422, bad.text
    assert (await client.post("/api/import", json={"bundle": {}})).status_code == 422
    assert (await client.post("/api/import", json={"bundle": {"agents": "nope"}})).status_code == 422
    assert (await client.post("/api/import", json={"bundle": {"agents": [{"id": "!!bad!!"}]}})).json()["report"]["warnings"]
    orphan = (await client.post("/api/import", json={"bundle": {
        "agents": [{"id": "alpha", "profile": {"name": "Alpha"}, "memory": []}],
        "conversations": [{"title": "孤儿", "agent_ids": ["ghost"], "messages": []}]}})).json()["report"]
    assert orphan["conversations"] == [] and orphan["warnings"], orphan
    big = (await client.post("/api/import", json={"bundle": {
        "agents": [{"id": f"bulk{i}", "profile": {"name": f"Bulk {i}"}, "memory": []} for i in range(80)]}})).json()["report"]
    assert len(big["agents"]) == 64, len(big["agents"])
    assert (await client.get("/api/agents/ghost/export")).status_code == 404
    print("PASS: 坏包 422、孤儿会话与非法 id 只记警告、单次导入上限 64 个 Bot")

    assert TOOL_GROUPS and registry_names(), "工具集合必须可用，否则降级判断无意义"
    await client.aclose()
    store.close()
    print("ALL PASS: 迁移导出/导入")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="carme-migration-") as tmp:
        asyncio.run(main(Path(tmp)))
