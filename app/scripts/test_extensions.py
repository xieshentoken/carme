"""隔离验收 Skill 与 MCP：装、连、调、管，全部用本地假服务，不碰真配置。

覆盖四层：
    1. carme/skills.py   —— 解析 / 安装（粘贴、本机路径、网址、GitHub 压缩包）/ 启停 / 删除
    2. carme/mcp.py      —— 校验、stdio 与 HTTP(JSON/SSE) 传输、工具注册、断开与重连
    3. /api/skills|mcp|tool-groups —— 令牌校验、请求体边界、密钥不外泄
    4. 与 Agent 的接线   —— 系统提示里的技能清单、工具分组展开、CLI 桥接白名单
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"

import httpx  # noqa: E402
import yaml  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402

from carme import config as config_module  # noqa: E402
from carme import mcp as mcp_module  # noqa: E402
from carme import skills as skills_module  # noqa: E402
from carme.agents.base import Agent  # noqa: E402
from carme.api.extensions import build_extensions_router  # noqa: E402
from carme.api.routes import require_token  # noqa: E402
from carme.bus import EventBus  # noqa: E402
from carme.engines import BRIDGE_TOOL_NAMES, _bridge_tools  # noqa: E402
from carme.mcp import MCPError, MCPManager, parse_env_text, tool_name  # noqa: E402
from carme.runtime import Runtime  # noqa: E402
from carme.skills import (MAX_BODY_BYTES, MAX_SKILL_FILES, SKILL_FILENAME, SkillError,  # noqa: E402
                          SkillManager)
from carme.store import Store  # noqa: E402
from carme.tools.base import TOOL_GROUPS, ToolContext  # noqa: E402

TOKEN = "extensions-fixture-token"

# ---------------------------------------------------------------- 假 MCP Server

FAKE_STDIO_SERVER = '''"""假 MCP Server：stdio + JSON-RPC，供 test_extensions.py 使用。"""
import json
import sys

PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wD/"
       "wJ1kAAAAAElFTkSuQmCC")

TOOLS = [
    {"name": "echo", "description": "回显传入的文本",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "boom", "description": "总是报错", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "pic", "description": "返回一张 1x1 PNG", "inputSchema": {"type": "object", "properties": {}}},
]


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "carme-fake", "version": "9.9"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = (message.get("params") or {}).get("name")
        arguments = (message.get("params") or {}).get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "echo: " + str(arguments.get("text", ""))}],
                "isError": False}})
        elif name == "boom":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "内部炸了"}], "isError": True}})
        elif name == "pic":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "image", "mimeType": "image/png", "data": PNG}], "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "未知工具"}})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
'''

SKILL_TEXT = """---
name: PDF 报告
description: 用脚本把数据渲染成 PDF 报告
version: "1"
---

# 步骤
1. 读 data.csv
2. 运行 scripts/render.py
"""


class FakeHttpServer:
    """极小的 JSON-RPC over HTTP：验证 http 传输与 SSE 解析，顺带记录收到的请求头。"""

    def __init__(self, *, sse: bool = False, fail: bool = False) -> None:
        self.sse = sse
        self.fail = fail
        self.headers: list[dict] = []
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/mcp"

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers: dict[str, str] = {}
        await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("utf-8", "replace").partition(":")
            headers[key.strip().lower()] = value.strip()
        self.headers.append(headers)
        raw = await reader.readexactly(int(headers.get("content-length", 0)))
        message = json.loads(raw.decode("utf-8"))
        if self.fail:
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        result = {"jsonrpc": "2.0", "id": message.get("id"), "result": self._result(message)}
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if self.sse:
            body = f"event: message\ndata: {json.dumps(result, ensure_ascii=False)}\n\n".encode("utf-8")
            content_type = "text/event-stream"
        else:
            content_type = "application/json"
        writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body)
        await writer.drain()
        writer.close()

    @staticmethod
    def _result(message: dict) -> dict:
        method = message.get("method")
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "carme-http-fake", "version": "1.0"}}
        if method == "tools/list":
            return {"tools": [{"name": "http_echo", "description": "HTTP 回显",
                               "inputSchema": {"type": "object",
                                               "properties": {"text": {"type": "string"}}}}]}
        if method == "tools/call":
            text = ((message.get("params") or {}).get("arguments") or {}).get("text", "")
            return {"content": [{"type": "text", "text": f"http: {text}"}], "isError": False}
        return {}


# ---------------------------------------------------------------- 1. 技能单元


def skill_unit_checks(root: Path) -> None:
    skills_root = root / "skills"
    settings = root / "skills.yaml"
    manager = SkillManager(skills_root, settings)

    assert manager.list_skills() == [], "空目录应当是空列表"
    # 粘贴安装：frontmatter 决定展示名与描述；id 由名字安全化得到（纯中文名走哈希兜底）。
    skill = manager.install_from_text(SKILL_TEXT)
    assert skill.id == "pdf", skill.id
    assert skill.name == "PDF 报告" and skill.description == "用脚本把数据渲染成 PDF 报告", skill
    assert skill.version == "1" and skill.enabled and skill.files == [SKILL_FILENAME], skill
    assert (skills_root / skill.id / SKILL_FILENAME).is_file()
    assert "读 data.csv" in manager.body(skill) and not manager.body(skill).startswith("---")
    chinese = manager.install_from_text("---\nname: 中文技能\n---\n\n只有中文名字。\n")
    assert chinese.id.startswith("skill-") and chinese.name == "中文技能", chinese
    manager.remove(chinese.id)

    # 按 id、按名字都能取到；取不到要有可读的报错。
    assert manager.get(skill.id).id == skill.id and manager.get("PDF 报告").id == skill.id
    try:
        manager.get("不存在")
        raise AssertionError("取不存在的技能应当报错")
    except SkillError as exc:
        assert "没有已安装的技能" in str(exc), exc

    # 系统提示只给名字和用途。
    block = manager.prompt_block()
    assert "## 你可用的技能（Skill）" in block and "PDF 报告" in block and "render.py" not in block, block

    # 停用后不再出现在提示里，use_skill 也应当被拒绝。
    assert manager.set_enabled(skill.id, False).enabled is False
    assert manager.prompt_block() == "" and manager.enabled_skills() == []
    assert manager.set_enabled(skill.id, True).enabled is True
    assert json.loads(json.dumps(manager.settings()))["disabled"] == []

    # 本机目录安装：scripts/ 一起去，符号链接不跟。
    source = root / "incoming" / "pdf-tool"
    (source / "scripts").mkdir(parents=True)
    (source / SKILL_FILENAME).write_text("---\nname: pdf-tool\ndescription: 本机目录技能\n---\n\n跑 scripts/render.py\n",
                                         encoding="utf-8")
    (source / "scripts" / "render.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"junk")
    (source / "linked.py").symlink_to(source / "scripts" / "render.py")
    copied = manager.install_from_path(str(source))
    assert copied.name == "pdf-tool" and copied.description == "本机目录技能", copied
    assert copied.files == [SKILL_FILENAME, "scripts/render.py"], copied.files
    assert not (skills_root / copied.id / ".DS_Store").exists()
    assert not (skills_root / copied.id / "linked.py").exists()

    # 单个 .md 文件安装。
    single = root / "incoming" / "quick.md"
    single.write_text("# 快速上手\n\n直接干活。\n", encoding="utf-8")
    from_file = manager.install_from_path(str(single))
    assert from_file.name == "quick" and from_file.description == "快速上手", from_file

    # 只读技能目录：多根目录能扫到，但不允许从这里删除。
    extra = root / "shared-skills"
    (extra / "shared-one").mkdir(parents=True)
    (extra / "shared-one" / SKILL_FILENAME).write_text("---\nname: shared-one\n---\n\n共享技能\n", encoding="utf-8")
    settings_payload = manager.settings()
    settings_payload["roots"] = [str(extra)]
    manager._save(settings_payload)
    shared = manager.get("shared-one")
    assert shared.source == "本地目录", shared
    try:
        manager.remove("shared-one")
        raise AssertionError("只读根目录里的技能不该被删除")
    except SkillError as exc:
        assert "只读技能目录" in str(exc), exc

    # 坏技能照样列出来，但要带上原因；超大文件直接拒绝。
    (skills_root / "broken-one").mkdir()
    broken = next(item for item in manager.list_skills() if item.id == "broken-one")
    assert "SKILL.md" in broken.error and broken.enabled
    assert "broken-one" not in manager.prompt_block(), "坏技能不该进系统提示"
    big = root / "incoming" / "big.md"
    big.write_text("x" * (skills_module.MAX_BODY_BYTES + 10), encoding="utf-8")
    try:
        manager.install_from_path(str(big))
        raise AssertionError("超大技能应当被拒绝")
    except SkillError as exc:
        assert "512 KB" in str(exc), exc

    # 地址与 GitHub 压缩包：打桩掉网络，只验证解析与路径安全。
    url_skill = asyncio.run(_install_from_url(manager))
    assert url_skill.name == "remote-skill", url_skill
    github_skill = asyncio.run(_install_from_github(manager))
    assert github_skill.name == "PDF Skill" and "scripts/run.py" in github_skill.files, github_skill.files
    assert not (skills_root / github_skill.id / "evil.md").exists(), "压缩包里的 ../ 必须被丢掉"

    # 删除之后目录与元数据都要清干净。
    manager.remove(github_skill.id)
    assert not (skills_root / github_skill.id).exists()
    assert github_skill.id not in manager.settings()["installed"]
    print("PASS: 技能解析 / 四种安装来源 / 启停 / 删除 / 只读目录与坏技能容忍")


async def _install_from_url(manager: SkillManager) -> object:
    async def fake(_self: object, _url: str, _limit: int) -> bytes:
        return "---\nname: remote-skill\ndescription: 来自网址\n---\n\n按这个做。\n".encode("utf-8")

    original = SkillManager._download_bytes
    SkillManager._download_bytes = fake  # type: ignore[assignment]
    try:
        return await manager.install_from_url("https://example.com/skill.md")
    finally:
        SkillManager._download_bytes = original  # type: ignore[assignment]


async def _install_from_github(manager: SkillManager) -> object:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        def add(name: str, content: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

        add("repo-main/skills/pdf-skill/SKILL.md",
            b"---\nname: PDF Skill\ndescription: \xe4\xbb\x8e\xe4\xbb\x93\xe5\xba\x93\xe5\xae\x89\xe8\xa3\x85\n---\n\n"
            b"1. \xe8\xb7\x91 scripts/run.py\n")
        add("repo-main/skills/pdf-skill/scripts/run.py", b"print('run')\n")
        add("repo-main/skills/pdf-skill/.git/config", b"[core]\n")
        add("repo-main/evil.md", b"# \xe4\xb8\x8d\xe8\xaf\xa5\xe5\x87\xba\xe7\x8e\xb0\n")
        add("repo-main/skills/pdf-skill/../evil.md", b"# \xe8\xb7\xaf\xe5\xbe\x84\xe7\xa9\xbf\xe9\x80\x8f\n")
    payload = buffer.getvalue()

    async def fake(_self: object, _url: str, _limit: int) -> bytes:
        return payload

    original = SkillManager._download_bytes
    SkillManager._download_bytes = fake  # type: ignore[assignment]
    try:
        return await manager.install_from_github("acme/repo", subpath="skills/pdf-skill", ref="main")
    finally:
        SkillManager._download_bytes = original  # type: ignore[assignment]


# ---------------------------------------------------------------- 2. MCP 单元


async def mcp_unit_checks(root: Path, server_path: Path) -> None:
    from carme.tools.base import ToolRegistry

    registry = ToolRegistry()
    manager = MCPManager(root / "mcp.yaml", registry)
    # 和 Runtime 一样把 mcp 分组接到管理器上：配置里写 mcp 就等于「当前已连上的全部 MCP 工具」。
    registry.set_dynamic_group("mcp", manager.tool_names)
    assert manager.public_servers() == []

    # 参数校验：id 形状、stdio 必须给命令、http 必须给地址、环境变量名、参数条数、超时钳制。
    for payload, expected in (
        ({"id": "bad id!"}, "Server id"),
        ({"id": "a" * 49}, "Server id"),
        ({"id": "ok", "transport": "stdio", "command": ""}, "这一项不能为空"),
        ({"id": "ok", "transport": "http", "url": "ftp://x"}, "http"),
        ({"id": "ok", "transport": "http", "url": "https://user:pw@example.com/mcp"}, "用户名或密码"),
        ({"id": "ok", "transport": "stdio", "command": "x", "env": {"BAD NAME": "1"}}, "环境变量名"),
        ({"id": "ok", "transport": "stdio", "command": "x", "args": ["a"] * 40}, "参数最多"),
    ):
        try:
            await manager.upsert(dict(payload))
            raise AssertionError(f"应当被拒绝：{payload}")
        except MCPError as exc:
            assert expected in str(exc), (expected, str(exc))
    assert manager.public_servers() == [], "校验失败的定义不能落盘"
    clamped = await manager.upsert({"id": "clamp", "transport": "stdio", "trusted_host": True, "command": "x", "enabled": False, "timeout": 9999})
    assert clamped["timeout"] == 300, clamped
    assert (await manager.upsert({"id": "clamp", "transport": "stdio", "trusted_host": True, "command": "x", "enabled": False,
                                  "timeout": 0}))["timeout"] == 1
    await manager.remove("clamp")
    assert not (root / "mcp.yaml").read_text(encoding="utf-8").count("clamp"), "删除后配置文件里不该留痕"

    # stdio：连上假 Server，工具按 mcp__<server>__<tool> 注册。
    connected = await manager.upsert({"id": "echo", "name": "回显服务", "transport": "stdio", "trusted_host": True,
                                      "command": sys.executable, "args": [str(server_path)],
                                      "enabled": True, "timeout": 20})
    assert connected["status"] == "connected" and connected["tool_count"] == 3, connected
    assert connected["server_info"] == "carme-fake 9.9" and connected["protocol"] == "2024-11-05", connected
    assert registry.expand(["mcp"]) == ["mcp__echo__boom", "mcp__echo__echo", "mcp__echo__pic"]
    assert [item["registered"] for item in connected["tools"]] == ["mcp__echo__echo", "mcp__echo__boom", "mcp__echo__pic"]

    ctx = ToolContext(agent=config_module.AgentSpec("fixture", "Fixture", tools=["mcp"]), task_id="t", store=None)  # type: ignore[arg-type]
    for remote in ("echo","boom","pic"):
        manager.grant("fixture","echo",remote,argument_allowlist={})
    assert await registry.execute(ctx, "mcp__echo__echo", {"text": "你好"}) == "echo: 你好"
    labeled = await registry.execute(ctx, "mcp__echo__boom", {})
    assert "内部炸了" in labeled and "MCP 工具报错" in labeled and "回显服务 · boom" in labeled, labeled
    await registry.execute(ctx, "mcp__echo__pic", {})
    images = ctx.extras.pop("images", [])
    assert images and images[0]["image_url"]["url"].startswith("data:image/png;base64,"), images

    # 重复连接不会把工具注册两遍；断开要把工具一起摘掉。
    await manager.connect("echo")
    assert registry.expand(["mcp"]) == ["mcp__echo__boom", "mcp__echo__echo", "mcp__echo__pic"]
    await manager.disconnect("echo")
    assert manager.status("echo") == "stopped" and registry.expand(["mcp"]) == []
    assert registry.get("mcp__echo__echo") is None

    # 断线后调用会按需重连：模型不该看到「工具明明在表里却调不动」。
    assert await manager.call_tool("echo", "echo", {"text": "again"}) is not None
    assert manager.status("echo") == "connected" and "mcp__echo__echo" in registry.names()
    await manager.disconnect("echo")

    # 起不来的 Server：状态是 error，带着原因，但定义保留（人工改完可重连）。
    broken = await manager.upsert({"id": "broken", "transport": "stdio", "trusted_host": True, "command": "/definitely/not/here",
                                   "enabled": True})
    assert broken["status"] == "error" and "找不到可执行文件" in broken["error"], broken
    try:
        await manager.connect("broken")
        raise AssertionError("连不上就该报错")
    except MCPError as exc:
        assert "找不到可执行文件" in str(exc), exc
    crashing = await manager.upsert({"id": "crash", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                                     "args": ["-c", "import sys; sys.exit(1)"], "enabled": True})
    assert crashing["status"] == "error" and crashing["error"], crashing
    await manager.remove("crash")
    await manager.remove("broken")

    # HTTP：JSON 与 SSE 两种应答都要能解析，自定义请求头要真的发出去。
    for sse in (False, True):
        http = FakeHttpServer(sse=sse)
        url = await http.start()
        try:
            server = await manager.upsert({"id": f"http{'sse' if sse else 'json'}", "name": "HTTP 服务",
                                           "transport": "http", "url": url, "enabled": True,
                                           "headers": {"X-Carme-Test": "yes"}})
            assert server["status"] == "connected" and server["tool_count"] == 1, server
            name = server["tools"][0]["registered"]
            assert name.startswith("mcp__http"), name
            result = await manager.call_tool(server["id"], "http_echo", {"text": "hi"})
            assert result["content"][0]["text"] == "http: hi", result
            assert http.headers[-1].get("x-carme-test") == "yes", http.headers[-1]
            await manager.remove(server["id"])
        finally:
            await http.close()
    failing = FakeHttpServer(fail=True)
    url = await failing.start()
    try:
        server = await manager.upsert({"id": "httpfail", "transport": "http", "url": url, "enabled": True})
        assert server["status"] == "error" and "HTTP 500" in server["error"], server
    finally:
        await failing.close()
        await manager.remove("httpfail")

    # 工具名安全化：非法字符替换、超长截断且唯一。
    taken = {"mcp__x__y"}
    first = tool_name("my server", "read:file/name", taken)
    second = tool_name("my server", "read:file/name", taken | {first})
    assert first != second and len(first) <= mcp_module.MAX_TOOL_NAME
    assert all(char.isalnum() or char in "_-" for char in first), first
    assert parse_env_text("A=1\n# 注释\nB=two=three") == {"A": "1", "B": "two=three"}
    try:
        parse_env_text("没有等号")
        raise AssertionError("缺等号的行应当报错")
    except MCPError as exc:
        assert "等号" in str(exc), exc

    await manager.close_all()
    assert manager.public_servers(), "close_all 只断连接，不该删定义"
    print("PASS: MCP 校验 / stdio / HTTP+SSE / 工具注册与注销 / 断线重连 / 工具名安全化")


# ---------------------------------------------------------------- 3. HTTP 接口


async def api_checks(root: Path, server_path: Path) -> None:
    config_dir, data_dir = root / "config", root / "data"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "models.yaml").write_text(
        yaml.safe_dump({"providers": {}, "models": [], "tiers": {}, "budget": {}}), encoding="utf-8")
    (config_dir / "agents.yaml").write_text(yaml.safe_dump(
        {"agents": {"chief": {"name": "Chief", "prompt": "fixture", "tier": "balanced", "sandbox": "none",
                              "entry": True, "tools": ["memory"]},
                    "worker": {"name": "Worker", "prompt": "fixture", "tier": "balanced", "sandbox": "none",
                               "tools": ["memory"]}}}), encoding="utf-8")
    # 直接改模块常量：CONFIG_DIR / DATA_DIR 在 import 时就固定了，测试不能再动真配置。
    config_module.CONFIG_DIR = config_dir
    config_module.DATA_DIR = data_dir
    config_module.ENV_FILE = root / ".env"
    os.environ["CARME_SKILLS_DIR"] = str(data_dir / "skills")
    os.environ["CARME_SKILLS_CONFIG"] = str(config_dir / "skills.yaml")
    os.environ["CARME_MCP_CONFIG"] = str(config_dir / "mcp.yaml")
    os.environ["CARME_TOKEN"] = TOKEN

    config = config_module.load(reload=True)
    store = Store(data_dir / "carme.db")
    runtime = Runtime(config, store, EventBus())
    app = FastAPI()
    app.include_router(build_extensions_router(config, store, runtime), dependencies=[Depends(require_token)])
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://carme.test",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        # 令牌校验
        anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://carme.test")
        assert (await anonymous.get("/api/skills")).status_code == 401
        assert (await anonymous.get("/api/mcp")).status_code == 401
        await anonymous.aclose()

        # 技能：空 → 安装 → 列表 → 详情 → 停用 → 删除
        empty = (await client.get("/api/skills")).json()
        assert empty["skills"] == [] and empty["enabled"] == 0 and empty["root"].endswith("skills"), empty
        assert empty["bots"] == [], empty

        installed = await client.post("/api/skills/install", json={"source": "text", "text": SKILL_TEXT})
        assert installed.status_code == 201, installed.text
        skill = installed.json()["skill"]
        assert skill["enabled"] and skill["file_count"] == 1, skill

        listed = (await client.get("/api/skills")).json()
        assert listed["enabled"] == 1 and listed["skills"][0]["id"] == skill["id"], listed
        detail = await client.get(f"/api/skills/{skill['id']}")
        assert detail.status_code == 200 and "render.py" in detail.json()["body"], detail.text
        assert (await client.get("/api/skills/nope")).status_code == 404
        assert (await client.patch("/api/skills/nope", json={"enabled": False})).status_code == 404
        assert (await client.delete("/api/skills/nope")).status_code == 422

        assert (await client.patch(f"/api/skills/{skill['id']}", json={"enabled": False})).json()["skill"]["enabled"] is False
        assert (await client.get("/api/skills")).json()["enabled"] == 0
        assert (await client.patch(f"/api/skills/{skill['id']}", json={"enabled": True})).json()["ok"]

        # 本机目录安装 + 重新扫描
        incoming = root / "incoming-skill"
        incoming.mkdir()
        (incoming / SKILL_FILENAME).write_text("---\nname: local-dir\ndescription: 目录技能\n---\n\n正文\n", encoding="utf-8")
        from_path = await client.post("/api/skills/install", json={"source": "path", "value": str(incoming)})
        assert from_path.status_code == 201 and from_path.json()["skill"]["name"] == "local-dir", from_path.text
        rescanned = await client.post("/api/skills/reload")
        assert rescanned.status_code == 200 and len(rescanned.json()["skills"]) == 2, rescanned.text

        # 请求体边界：未知字段、空内容、坏仓库名都要 422
        assert (await client.post("/api/skills/install", json={"source": "text", "text": "", "value": ""})).status_code == 422
        assert (await client.post("/api/skills/install", json={"source": "text", "text": "x", "nope": 1})).status_code == 422
        assert (await client.post("/api/skills/install", json={"source": "github", "value": "not-a-repo"})).status_code == 422
        assert (await client.post("/api/skills/install", json={"source": "path", "value": "/nope/nope"})).status_code == 422

        # MCP：新增（自动连接）→ 列表 → 断开 → 重连 → 停用 → 密钥不外泄 → 删除
        # 命令用本机解释器的绝对路径：PUT 是全量语义，必须原样带上（路径里可能有空格）。
        body = {"id": "echo", "name": "回显服务", "transport": "stdio", "command": sys.executable,
                "args": [str(server_path)], "args_text": "", "env_text": "SECRET=topsecret",
                "clear_env": False, "cwd": "", "url": "", "headers_text": "",
                "enabled": True, "approval": "auto", "timeout": 20}
        blocked = await client.post("/api/mcp/servers", json=body)
        assert "stdio_requires_isolated_executor" in blocked.text, blocked.text
        # Explicitly trust only this temporary Python protocol fixture, not arbitrary Bot execution.
        await runtime.mcp.upsert({"id": "echo", "command": sys.executable,
                                 "args": [str(server_path)], "trusted_host": True, "enabled": False})
        body["executor"] = ""  # Explicit host protocol fixture only; production stdio uses Action.
        added = await client.post("/api/mcp/servers", json=body)
        assert added.status_code == 201, added.text
        server = added.json()["server"]
        assert server["status"] == "connected" and server["tool_count"] == 3, server
        assert server["env_keys"] == ["SECRET"], server
        assert "topsecret" not in added.text, "环境变量的值绝不能回显"
        # 命令与参数要结构化回显：界面靠它填回原值，带空格的参数不能被拆坏。
        assert server["command"] == sys.executable and server["args"] == [str(server_path)], server
        assert server["cwd"] == "" and server["url"] == ""
        spaced = await client.put("/api/mcp/servers/echo",
                                  json={**body, "args": [str(server_path), "/tmp/dir with space"]})
        assert spaced.json()["server"]["args"] == [str(server_path), "/tmp/dir with space"], spaced.text
        assert runtime.mcp.server("echo").args[-1] == "/tmp/dir with space"
        await client.put("/api/mcp/servers/echo", json=body)
        listed_mcp = (await client.get("/api/mcp")).json()
        assert listed_mcp["servers"][0]["status"] == "connected" and listed_mcp["config"].endswith("mcp.yaml")
        assert "topsecret" not in json.dumps(listed_mcp, ensure_ascii=False)

        # 留空 env_text = 沿用后端已存的变量；填了 = 以填的为准；清空 = 全删。
        kept = await client.put("/api/mcp/servers/echo", json={**body, "env_text": ""})
        assert kept.status_code == 200 and kept.json()["server"]["env_keys"] == ["SECRET"], kept.text
        replaced = await client.put("/api/mcp/servers/echo", json={**body, "env_text": "OTHER=1"})
        assert replaced.json()["server"]["env_keys"] == ["OTHER"], replaced.text
        cleared = await client.put("/api/mcp/servers/echo", json={**body, "env_text": "", "clear_env": True})
        assert cleared.json()["server"]["env_keys"] == [], cleared.text
        assert (await client.put("/api/mcp/servers/echo", json={**body, "env_text": "坏的"})).status_code == 422

        assert (await client.post("/api/mcp/servers/echo/disconnect")).json()["server"]["status"] == "stopped"
        assert runtime.registry.expand(["mcp"]) == []
        assert (await client.post("/api/mcp/servers/echo/connect")).json()["server"]["status"] == "connected"
        assert (await client.patch("/api/mcp/servers/echo", json={"enabled": False})).json()["server"]["status"] == "disabled"
        assert runtime.registry.expand(["mcp"]) == []
        assert (await client.patch("/api/mcp/servers/echo", json={"enabled": True})).json()["server"]["status"] == "connected"
        assert (await client.get("/api/mcp")).json()["servers"][0]["tools"][0]["registered"].startswith("mcp__echo__")

        # 校验失败不落盘；连不上仍然保存，但状态是 error 且带原因。
        assert (await client.post("/api/mcp/servers", json={"id": "bad id", "command": "x"})).status_code == 422
        assert (await client.post("/api/mcp/servers", json={"id": "nocmd", "transport": "stdio", "command": ""})).status_code == 422
        assert (await client.post("/api/mcp/servers", json={"id": "x1", "transport": "stdio", "command": "x",
                                                            "unknown_field": 1})).status_code == 422
        assert (await client.put("/api/mcp/servers/nope", json={"id": "nope", "command": "x"})).status_code == 404
        assert (await client.post("/api/mcp/servers/nope/connect")).status_code == 502
        assert [item["id"] for item in (await client.get("/api/mcp")).json()["servers"]] == ["echo"], "坏定义不该落盘"

        unreachable = await client.post("/api/mcp/servers", json={
            "id": "missing", "name": "连不上", "transport": "stdio", "command": "/definitely/not/here", "enabled": True})
        assert unreachable.status_code == 201, unreachable.text
        assert unreachable.json()["server"]["status"] == "error", unreachable.text
        assert "stdio_requires_isolated_executor" in unreachable.json()["server"]["error"]
        assert "连接失败" in unreachable.json()["message"], unreachable.text
        assert (await client.delete("/api/mcp/servers/missing")).json()["ok"]

        # 工具分组授权：一次生效、幂等、可指定成员、坏分组 422
        granted = await client.post("/api/tool-groups/grant", json={"group": "mcp"})
        assert granted.status_code == 200, granted.text
        assert sorted(granted.json()["updated"]) == ["chief", "worker"], granted.text
        assert (await client.post("/api/tool-groups/grant", json={"group": "mcp"})).json()["updated"] == []
        one = await client.post("/api/tool-groups/grant", json={"group": "skill", "agent_ids": ["chief"]})
        assert one.json()["updated"] == ["chief"], one.text
        assert "skill" in config.agents.get("chief").tools and "skill" not in config.agents.get("worker").tools
        assert (await client.post("/api/tool-groups/grant", json={"group": "bogus"})).status_code == 422
        on_disk = yaml.safe_load((config_dir / "agents.yaml").read_text(encoding="utf-8"))
        assert on_disk["agents"]["chief"]["tools"] == ["memory", "mcp", "skill"], on_disk["agents"]["chief"]
        assert (await client.get("/api/mcp")).json()["bots"] == [{"id": "chief", "name": "Chief"},
                                                                  {"id": "worker", "name": "Worker"}]

        # 删除
        assert (await client.delete("/api/mcp/servers/echo")).json()["ok"]
        assert (await client.get("/api/mcp")).json()["servers"] == []
        for item in (await client.get("/api/skills")).json()["skills"]:
            assert (await client.delete(f"/api/skills/{item['id']}")).json()["ok"]
        assert (await client.get("/api/skills")).json()["skills"] == []
        print("PASS: /api/skills 与 /api/mcp 的令牌校验、增删改查、密钥保护、分组授权")
    finally:
        await client.aclose()
        await runtime.shutdown()
        store.close()




# ---------------------------------------------------------------- 4. Agent 接线


async def _cli_bridge_check(config, runtime, store, spec, config_dir: Path, root: Path) -> None:
    """CLI 引擎这条路径最容易断：模型看到的工具要先过「桥接白名单」。

    子进程绑在事件循环上，所以整段必须在同一个循环里跑完（upsert + 断言 + 收尾）。
    """
    from carme.mcp import MCPManager
    from carme.tools.base import build_registry

    # 和 Runtime 一样装配：内置技能工具 + 动态 mcp 分组。
    cli_registry = build_registry(skills=runtime.skills)
    cli_mcp = MCPManager(config_dir / "cli-mcp.yaml", cli_registry)
    cli_registry.set_dynamic_group("mcp", cli_mcp.tool_names)
    fake_server = root / "wiring-mcp.py"
    fake_server.write_text(FAKE_STDIO_SERVER, encoding="utf-8")
    try:
        connected = await cli_mcp.upsert({"id": "cli", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                                          "args": [str(fake_server)], "enabled": True})
        assert connected["status"] == "connected", connected
        spec.engine = "claude"          # 换成 CLI 引擎，走 bridged 分支
        spec.tools = ["memory", "skill", "mcp"]
        bridged = [name for name in cli_registry.expand(spec.tools)
                   if name in BRIDGE_TOOL_NAMES or name.startswith("mcp__")]
        bridged_names = [item["function"]["name"] for item in cli_registry.specs_for(bridged)]
        assert "mcp__cli__echo" in bridged_names and "use_skill" in bridged_names, bridged_names
        assert "shell" not in bridged_names and "web_search" not in bridged_names, bridged_names
        cli_prompt = Agent(spec, config, runtime.gateway, cli_registry, store, runtime.skills).system_prompt()
        assert "mcp__cli__echo" in cli_prompt and "## 当前引擎边界" in cli_prompt, cli_prompt
    finally:
        await cli_mcp.close_all()


def wiring_checks(root: Path) -> None:
    assert {"skill", "mcp"} <= set(TOOL_GROUPS), TOOL_GROUPS
    assert TOOL_GROUPS["skill"] == ["list_skills", "use_skill"]
    assert {"list_skills", "use_skill"} <= BRIDGE_TOOL_NAMES, BRIDGE_TOOL_NAMES

    bridged = _bridge_tools([
        {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}
        for name in ("remember", "use_skill", "list_skills", "mcp__echo__echo", "shell", "web_search")
    ])
    assert [item.name for item in bridged] == ["remember", "use_skill", "list_skills", "mcp__echo__echo", "shell", "web_search"], bridged

    config_dir, data_dir = root / "wiring", root / "wiring-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "models.yaml").write_text(
        yaml.safe_dump({"providers": {}, "models": [], "tiers": {}, "budget": {}}), encoding="utf-8")
    (config_dir / "agents.yaml").write_text(yaml.safe_dump({"agents": {"chief": {
        "name": "Chief", "prompt": "fixture", "tier": "balanced", "sandbox": "none",
        "entry": True, "tools": ["memory"]}}}), encoding="utf-8")
    config_module.CONFIG_DIR = config_dir
    config_module.DATA_DIR = data_dir
    config_module.ENV_FILE = root / "wiring.env"
    os.environ["CARME_SKILLS_DIR"] = str(data_dir / "skills")
    os.environ["CARME_SKILLS_CONFIG"] = str(config_dir / "skills.yaml")
    os.environ["CARME_MCP_CONFIG"] = str(config_dir / "mcp.yaml")

    config = config_module.load(reload=True)
    store = Store(data_dir / "carme.db")
    runtime = Runtime(config, store, EventBus())
    try:
        spec = config.agents.get("chief")
        assert runtime.registry.get("use_skill") is not None, "注册表里应当有技能工具"
        assert runtime.registry.groups() == sorted(set(TOOL_GROUPS)), runtime.registry.groups()

        # 没给技能工具时，系统提示里不该出现技能段（避免诱导幻觉）。
        quiet = Agent(spec, config, runtime.gateway, runtime.registry, store, runtime.skills).system_prompt()
        assert "## 你可用的技能（Skill）" not in quiet, quiet

        installed=runtime.skills.install_from_text(SKILL_TEXT)
        revision=runtime.skills.snapshot(installed.id)["revision"]
        runtime.skills.grant(spec.id,installed.id,revision)
        spec.tools = ["memory", "skill"]
        prompt = Agent(spec, config, runtime.gateway, runtime.registry, store, runtime.skills).system_prompt()
        assert "## 你可用的技能（Skill）" in prompt and "PDF 报告" in prompt, prompt
        assert "use_skill" in prompt and "render.py" not in prompt, prompt
        names = [item["function"]["name"] for item in runtime.registry.specs_for(spec.tools)]
        assert names == ["remember", "recall", "forget", "list_skills", "use_skill"], names
        assert runtime.registry.specs_for(["mcp"]) == [], "没有连接时 mcp 分组展开为空"

        # 技能工具直接跑一遍：list_skills 有内容，use_skill 给正文，停用后给出明确拒绝。
        ctx = ToolContext(agent=spec, task_id="t", store=store)
        listed = asyncio.run(runtime.registry.execute(ctx, "list_skills", {}))
        assert "PDF 报告" in listed, listed
        loaded = asyncio.run(runtime.registry.execute(ctx, "use_skill", {"name": "PDF 报告"}))
        assert "render.py" in loaded and "/inputs/skills/" in loaded and str(runtime.skills.root) not in loaded, loaded  # M4: Worker sees target path, never Control path
        assert "技能错误" in asyncio.run(runtime.registry.execute(ctx, "use_skill", {"name": "没有这个"}))
        runtime.skills.set_enabled(runtime.skills.list_skills()[0].id, False)
        assert "已停用" in asyncio.run(runtime.registry.execute(ctx, "use_skill", {"name": "PDF 报告"}))

        asyncio.run(_cli_bridge_check(config, runtime, store, spec, config_dir, root))
        print("PASS: 技能与 MCP 已接进工具表、系统提示和 CLI 桥接")
    finally:
        asyncio.run(runtime.shutdown())
        store.close()


# ---------------------------------------------------------------- 5. 边界回归
#
# 反复出问题的地方：子进程继承了什么环境、取消 / 崩溃后有没有收干净、
# 手写的配置文件写坏了会不会连累整个后端。

ENV_PROBE_SERVER = '''"""把进程环境写出来，验证 Carme 没有把令牌和密钥漏给 MCP 子进程。"""
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(dict(os.environ), stream)

for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    if message.get("id") is None:
        continue
    method = message.get("method")
    if method == "initialize":
        out = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "probe", "version": "1"}}
    elif method == "tools/list":
        out = {"tools": [{"name": "noop", "description": "", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        out = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": out}) + "\\n")
    sys.stdout.flush()
'''

SLOW_SERVER = '''"""启动了就不回应 initialize：验证取消连接时子进程会被收掉。"""
import os
import sys
import time

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
time.sleep(120)
'''

BIG_LINE_SERVER = '''"""tools/list 返回超过单行上限的响应：验证连接被判定失效而不是一直等。"""
import json
import sys

for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    if message.get("id") is None:
        continue
    method = message.get("method")
    if method == "initialize":
        out = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "big", "version": "1"}}
    elif method == "tools/list":
        out = {"tools": [{"name": "huge", "description": "x" * (5 * 1024 * 1024),
                          "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        out = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": out}) + "\\n")
    sys.stdout.flush()
'''


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:  # pragma: no cover - 别的用户的进程
        return True
    return True


async def hardening_checks(root: Path) -> None:
    from carme.tools.base import ToolRegistry

    root.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    manager = MCPManager(root / "mcp.yaml", registry)
    registry.set_dynamic_group("mcp", manager.tool_names)

    # 1) 子进程环境：CARME_TOKEN 与模型密钥不能漏给它，显式配的 env 要到。
    probe = root / "env_probe.py"
    probe.write_text(ENV_PROBE_SERVER, encoding="utf-8")
    dump = root / "child_env.json"
    os.environ["CARME_TOKEN"] = "should-not-leak"
    os.environ["OPENAI_API_KEY"] = "sk-should-not-leak"
    try:
        connected = await manager.upsert({"id": "probe", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                                          "args": [str(probe), str(dump)], "enabled": True,
                                          "env": {"FIXTURE_MCP_ALLOWED": "yes"}})
        assert connected["status"] == "connected", connected
        child = json.loads(dump.read_text(encoding="utf-8"))
        assert "CARME_TOKEN" not in child, "MCP 子进程不该拿到 Carme 的访问令牌"
        assert "OPENAI_API_KEY" not in child, "MCP 子进程不该拿到模型密钥"
        assert child.get("FIXTURE_MCP_ALLOWED") == "yes", "显式配置的 env 必须传下去"
        assert child.get("PATH"), "PATH 要保留，否则子进程找不到自己的运行时"
        await manager.remove("probe")
    finally:
        os.environ.pop("CARME_TOKEN", None)
        os.environ.pop("OPENAI_API_KEY", None)

    # 2) 取消连接：已经拉起来的子进程必须被收掉，否则会活过后端。
    slow = root / "slow_server.py"
    slow.write_text(SLOW_SERVER, encoding="utf-8")
    pid_file = root / "slow.pid"
    await manager.upsert({"id": "slow", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                          "args": [str(slow), str(pid_file)], "enabled": False})
    # 用「启用」触发连接：enabled 的 Server 才会被 connect() 接进来。
    task = asyncio.create_task(manager.set_enabled("slow", True))
    for _ in range(200):
        if pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert pid_file.exists(), "慢 Server 应当已经被拉起来"
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for _ in range(100):
        if not _alive(child_pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(child_pid), f"取消连接后子进程 {child_pid} 仍然活着"
    await manager.remove("slow")
    assert manager.status("slow") == "missing"

    # 3) 单行超过上限：连接判定失效并收掉子进程，而不是每次都白等到超时。
    big = root / "big_line.py"
    big.write_text(BIG_LINE_SERVER, encoding="utf-8")
    oversized = await manager.upsert({"id": "big", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                                      "args": [str(big)], "enabled": True, "timeout": 10})
    assert oversized["status"] == "error" and oversized["error"], oversized
    assert registry.expand(["mcp"]) == [], "连不上的 Server 不该留下工具"
    await manager.remove("big")

    # 4) 连上之后子进程崩溃：下一次调用按需重连，而不是永远报「进程未运行」。
    fake = root / "fake_mcp_server.py"
    fake.write_text(FAKE_STDIO_SERVER, encoding="utf-8")
    revived = await manager.upsert({"id": "revive", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                                    "args": [str(fake)], "enabled": True})
    assert revived["status"] == "connected", revived
    manager._sessions["revive"].transport._terminate()
    await asyncio.sleep(0.1)
    result = await manager.call_tool("revive", "echo", {"text": "back"})
    assert result["content"][0]["text"] == "echo: back", result
    assert manager.status("revive") == "connected" and "mcp__revive__echo" in registry.names()
    await manager.remove("revive")

    print("PASS: 子进程环境隔离 / 取消与崩溃后的清理 / 单行上限 / 坏 mcp.yaml 容错")


async def collision_and_skill_edge_checks(root: Path) -> None:
    """工具名撞车、坏 skills.yaml、超限安装、配置文件权限。"""
    from carme.tools.base import ToolRegistry

    root.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    manager = MCPManager(root / "mcp.yaml", registry)
    registry.set_dynamic_group("mcp", manager.tool_names)
    fake = root / "fake_mcp_server.py"
    fake.write_text(FAKE_STDIO_SERVER, encoding="utf-8")

    # 5) 两个 id 前 20 位相同的 Server 不能互相顶掉对方的工具。
    first, second = "server-abcdefghij-one", "server-abcdefghij-two"
    for server_id in (first, second):
        await manager.upsert({"id": server_id, "transport": "stdio", "trusted_host": True, "command": sys.executable,
                              "args": [str(fake)], "enabled": True})
    names = registry.expand(["mcp"])
    assert len(names) == 6 and len(set(names)) == 6, names
    await manager.remove(first)
    remaining = registry.expand(["mcp"])
    assert len(remaining) == 3, "断开一个 Server 不能带走另一个的工具"
    assert all(len(name) <= mcp_module.MAX_TOOL_NAME for name in remaining), remaining

    # 6) 手写的 mcp.yaml 写坏了：坏值退化成默认值，好条目照常加载。
    broken = root / "broken.yaml"
    broken.write_text(yaml.safe_dump({"version": 1, "servers": {
        "ok": {"name": "好配置", "transport": "stdio", "command": "npx", "args": ["-y", "x"],
               "env": {"A": "1"}, "enabled": False, "installed_at": "昨天"},
        "weird": {"transport": "标准输入", "args": "不是列表", "env": "不是字典", "headers": ["x"],
                  "timeout": "很久", "installed_at": None},
        "bad id!": {"command": "x"},
    }}), encoding="utf-8")
    tolerant = MCPManager(broken, ToolRegistry())
    loaded = {item["id"]: item for item in tolerant.public_servers()}
    assert set(loaded) == {"ok", "weird"}, loaded
    assert loaded["ok"]["installed_at"] == 0.0 and loaded["ok"]["env_keys"] == ["A"], loaded["ok"]
    assert loaded["weird"]["transport"] == "stdio" and loaded["weird"]["args"] == [], loaded["weird"]
    assert loaded["weird"]["timeout"] == 30.0 and loaded["weird"]["header_keys"] == [], loaded["weird"]

    # 7) skills.yaml 里的坏时间戳不能让列表接口 500。
    skills = SkillManager(root / "skills", root / "skills.yaml")
    skills.install_from_text(SKILL_TEXT)
    payload = skills.settings()
    only = skills.list_skills()[0]
    payload["installed"][only.id] = {"installed_at": "昨天", "source": "手动"}
    skills._save(payload)
    listed = skills.list_skills()
    assert listed[0].installed_at == 0.0 and listed[0].source == "手动", listed[0].public()

    # 8) 正文超限：安装时就拒绝，而且不留下半个目录。
    before = {item.id for item in skills.list_skills()}
    try:
        skills.install_from_text("---\nname: too-big\n---\n\n" + "x" * (MAX_BODY_BYTES + 100))
        raise AssertionError("超过 512 KB 的技能应当在安装时被拒绝")
    except SkillError as exc:
        assert "512 KB" in str(exc), exc
    assert {item.id for item in skills.list_skills()} == before, "失败的安装不该留下技能"

    # 9) 文件数超限：复制中途失败也要把目录清掉。
    bulky = root / "bulky"
    bulky.mkdir()
    (bulky / SKILL_FILENAME).write_text("---\nname: bulky\n---\n\n正文\n", encoding="utf-8")
    for index in range(MAX_SKILL_FILES + 5):
        (bulky / f"file-{index}.txt").write_text("x", encoding="utf-8")
    try:
        skills.install_from_path(str(bulky))
        raise AssertionError("文件数超限应当被拒绝")
    except SkillError as exc:
        assert "400" in str(exc), exc
    assert not (skills.root / "bulky").exists(), "失败的本机安装不该留下目录"

    # 10) 配置文件权限：env / headers 的值只躺在 0600 的文件里。
    assert (root / "mcp.yaml").stat().st_mode & 0o777 == 0o600
    assert (root / "skills.yaml").stat().st_mode & 0o777 == 0o600

    # 11) approval=confirm：有人工闸门才执行，没有闸门一律拒绝。
    from carme.approval import ApprovalOutcome
    from carme.tools.base import ToolContext

    await manager.upsert({"id": "gated", "transport": "stdio", "trusted_host": True, "command": sys.executable,
                          "args": [str(fake)], "enabled": True, "approval": "confirm"})
    manager.grant('fixture','gated','echo',argument_allowlist={})  # M4: grant and per-call approval are independent gates.
    try:
        bare = ToolContext(agent=config_module.AgentSpec("fixture", "Fixture", tools=["mcp"]), task_id="t", store=None)  # type: ignore[arg-type]
        denied = await registry.execute(bare, "mcp__gated__echo", {"text": "hi"})
        assert "已拒绝" in denied and "hi" not in denied, denied

        seen = {"n": 0}

        async def approve(*, kind: str, summary: str, detail: dict) -> ApprovalOutcome:
            seen["n"] += 1
            assert kind == "mcp" and "gated" in summary and detail["tool"] == "echo", (kind, summary, detail)
            return ApprovalOutcome(True, "同意")

        allowed = ToolContext(agent=config_module.AgentSpec("fixture", "Fixture", tools=["mcp"]), task_id="t", store=None, approve=approve)  # type: ignore[arg-type]
        manager.grant("fixture","gated","echo",argument_allowlist={})
        assert await registry.execute(allowed, "mcp__gated__echo", {"text": "hi"}) == "echo: hi"
        assert seen["n"] == 1, seen
    finally:
        await manager.remove("gated")

    print("PASS: 工具名撞车 / 坏配置容错 / 超限安装清理 / 文件权限 / MCP 人工确认闸门")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="carme-extensions-") as directory:
        base = Path(directory)
        fake = base / "fixtures" / "fake_mcp_server.py"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(FAKE_STDIO_SERVER, encoding="utf-8")
        skill_unit_checks(base / "unit")
        asyncio.run(mcp_unit_checks(base / "unit", fake))
        asyncio.run(api_checks(base / "api", fake))
        asyncio.run(hardening_checks(base / "hardening"))
        asyncio.run(collision_and_skill_edge_checks(base / "edges"))
        wiring_checks(base)
        for key in ("CARME_SKILLS_DIR", "CARME_SKILLS_CONFIG", "CARME_MCP_CONFIG", "CARME_TOKEN"):
            os.environ.pop(key, None)
    print("ALL PASS: Skill 与 MCP 的安装、调用与管理")
