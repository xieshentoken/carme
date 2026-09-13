"""HTTP API —— 界面和手机端都通过它跟 Runtime 说话。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import hashlib
import hmac
import io
import secrets
import warnings
from urllib.parse import urlsplit, urlunsplit
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
import yaml

from ..config import Config
from ..runtime import BudgetExceeded, Runtime
from ..store import Store
from ..tools.base import TOOL_GROUPS
from .. import config as config_module
from ..llm import LLMError
from ..engines import CLI_ENGINE_DEFS, detect_cli_engines, resolve_workspace

log = logging.getLogger("carme.api")

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
CLOUDFLARE_ORIGIN = "http://127.0.0.1:8899"
CLOUDFLARE_CONFIG_DEFAULT = WEB_DIR.parent / "deploy" / "cloudflared" / "carme-tunnel.yml"
CLOUDFLARE_PID_DEFAULT = WEB_DIR.parent / ".local" / "active" / "cloudflared.pid"


# --------------------------------------------------------------------------- #
#  请求体
# --------------------------------------------------------------------------- #


class NewTask(BaseModel):
    goal: str = Field(..., min_length=1, max_length=20000, description="要做什么")
    agent_id: str = Field("", description="指定成员；留空则交给主控")
    title: str = Field("", max_length=120)


class ProbeRequest(BaseModel):
    mode: str = Field("", description="local / docker / remote；留空用配置默认")


class ApprovalDecision(BaseModel):
    approved: bool = Field(..., description="批准执行 / 拒绝")
    note: str = Field("", max_length=500, description="给你的批注，会回传给模型")


class NewConversation(BaseModel):
    agent_ids: list[str] = Field(..., min_length=1, max_length=6)
    title: str = Field("", max_length=120)


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(None, min_length=1, max_length=120)
    pinned: bool | None = None
    folder: str | None = Field(None, max_length=60)
    hidden: bool | None = None
    deleted: bool | None = None
    unread: bool | None = None


class ModelRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tiers: dict[str, list[str]]
    allow_mock: bool = False


class ConversationMessage(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)
    request_id: str = Field(..., min_length=1, max_length=128)
    agent_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)


class MemoryEntry(BaseModel):
    key: str = Field(..., min_length=1, max_length=160)
    value: str = Field(..., min_length=1, max_length=20000)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    name: str | None = Field(None, min_length=1, max_length=80)
    title: str | None = Field(None, max_length=160)
    emoji: str | None = Field(None, max_length=32)
    prompt: str | None = Field(None, max_length=20000)
    tier: str | None = Field(None, max_length=80)
    model: str | None = Field(None, max_length=200)
    effort: str | None = Field(None, max_length=16)
    engine: str | None = Field(None, pattern=r"^(api|codex|pi|claude)$")
    engine_model: str | None = Field(None, max_length=200)
    engine_effort: str | None = Field(None, max_length=16)
    engine_workspace: str | None = Field(None, max_length=500)
    avatar: dict | None = None
    tools: list[str] | None = Field(None, max_length=64)
    entry: bool | None = None
    can_delegate: bool | None = None
    sandbox: str | None = Field(None, pattern=r"^(none|remote|local|docker)$")


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    label: str = Field(..., min_length=1, max_length=80)
    type: str = Field(..., max_length=32)
    base_url: str = Field(..., min_length=1, max_length=2048)
    api_key: SecretStr | None = None


class ModelSelection(BaseModel):
    id: str = Field(..., min_length=1, max_length=200)
    effort: str = Field("", max_length=16)


class ModelConnectionSave(ModelConnection):
    probe_id: str
    models: list[ModelSelection] = Field(..., min_length=1, max_length=16)


class ModelRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refs: list[str] = Field(..., min_length=1, max_length=32)


class EffortProbe(ModelConnection):
    probe_id: str
    model_id: str = Field(..., min_length=1, max_length=200)


class EngineTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field("", max_length=200)
    effort: str = Field("", max_length=16)


class CloudflareConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tunnel: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    credentials_file: str = Field(..., min_length=1, max_length=2048)
    hostname: str = Field(..., min_length=3, max_length=253)
    protocol: str = Field(..., pattern=r"^(auto|http2|quic)$")
    team_name: str = Field(..., min_length=1, max_length=128)
    audience_tag: str = Field(..., min_length=1, max_length=256)


def normalise_model_url(value: str, api_type: str) -> str:
    parts = urlsplit(value.strip())
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("请输入 HTTP(S) API 基础网址，不要包含密码、查询参数或片段")
    try:
        parts.port
    except ValueError:
        raise ValueError("API 网址端口无效") from None
    path = parts.path.rstrip("/")
    suffix = "/messages" if api_type == "anthropic" else "/responses" if api_type == "openai_responses" else "/chat/completions"
    if path.endswith(suffix):
        path = path[:-len(suffix)]
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def public_task(task: dict) -> dict:
    """API 不公开任务固定的 SSH 连接快照。"""
    row = dict(task)
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    node = meta.pop("node", None) or {}
    row["node_id"] = meta.get("node_id", node.get("node_id", ""))
    row["node_name"] = meta.get("node_name", node.get("name", ""))
    row["engine"] = meta.get("agent_engine", meta.get("engine", "api"))
    row["engine_model"] = meta.get("agent_engine_model", meta.get("engine_model", ""))
    row["execution_host"] = meta.get("execution_host", "node" if node else "unassigned")
    row["engine_workspace"] = meta.get("engine_workspace", meta.get("agent_engine_workspace", ""))
    for key in ("cost_known", "tokens_known"):
        value = row.get(key, True)
        if isinstance(value, str):
            row[key] = value.strip().lower() not in {"", "0", "false", "no", "off"}
        else:
            row[key] = True if value is None else bool(value)
    row["meta"] = json.dumps(meta, ensure_ascii=False)
    return row


def _find_cloudflared() -> tuple[str, str]:
    """Find cloudflared without assuming Homebrew or changing the host."""
    candidates = [
        (shutil.which("cloudflared"), "PATH"),
        ("/opt/homebrew/bin/cloudflared", "Homebrew"),
        ("/usr/local/bin/cloudflared", "Homebrew"),
        ("/usr/bin/cloudflared", "system"),
    ]
    for candidate, source in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate, source
    return "", ""


def _cloudflared_version(binary: str) -> str:
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    match = re.search(r"\bversion\s+v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", result.stdout, re.IGNORECASE)
    return match.group(1) if match else ""


def _cloudflare_hostname(value: object) -> str:
    hostname = str(value or "").strip().lower().rstrip(".")
    labels = hostname.split(".") if hostname else []
    if (not hostname or "." not in hostname or len(hostname) > 253
            or any(not 1 <= len(label) <= 63
                   or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                   for label in labels)):
        return ""
    return hostname


def _cloudflare_origin_matches(value: object, target: str = CLOUDFLARE_ORIGIN) -> bool:
    try:
        actual = urlsplit(str(value or "").strip())
        expected = urlsplit(target)
        return (actual.scheme == expected.scheme == "http"
                and actual.hostname == expected.hostname == "127.0.0.1"
                and actual.port == expected.port
                and actual.path.rstrip("/") in {"", expected.path.rstrip("/")}
                and not actual.username and not actual.password
                and not actual.query and not actual.fragment)
    except ValueError:
        return False


def _cloudflare_pid_status(path: Path, config_path: Path) -> str:
    if not path.is_file():
        return "not_running"
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        if pid <= 1:
            return "unknown"
        os.kill(pid, 0)
    except ProcessLookupError:
        return "not_running"
    except (OSError, ValueError, UnicodeError):
        return "unknown"
    try:
        process = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    command = process.stdout.strip()
    if process.returncode != 0 or "cloudflared" not in command or str(config_path.resolve()) not in command:
        return "unknown"
    return "running"


def _read_cloudflare_config(path: Path) -> dict:
    result = {
        "exists": path.is_file(),
        "valid": False,
        "structural_valid": False,
        "tunnel_named": False,
        "credentials_present": False,
        "origin_target_ok": False,
        "hostname_configured": False,
        "hostname_mismatch": False,
        "fallback_present": False,
        "access_configured": False,
        "hostname": "",
        "protocol": "http2",
        "protocol_valid": False,
        "tunnel": "",
        "credentials_file": "",
        "access_team": "",
        "access_audience": "",
    }
    if not result["exists"]:
        return result
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return result
    if not isinstance(raw, dict):
        return result
    result["tunnel"] = str(raw.get("tunnel", "")).strip()
    result["tunnel_named"] = bool(result["tunnel"])
    credentials = str(raw.get("credentials-file", "")).strip()
    result["credentials_file"] = credentials
    credentials_path = Path(credentials).expanduser() if credentials else Path()
    if credentials and not credentials_path.is_absolute():
        credentials_path = path.parent / credentials_path
    result["credentials_present"] = bool(credentials and credentials_path.is_file())
    protocol = str(raw.get("protocol", "http2")).strip().lower()
    result["protocol_valid"] = protocol in {"auto", "http2", "quic"}
    result["protocol"] = protocol if result["protocol_valid"] else "invalid"
    ingress = raw.get("ingress")
    if not isinstance(ingress, list):
        return result
    fallback = ingress[-1] if ingress else None
    result["fallback_present"] = bool(
        isinstance(fallback, dict)
        and set(fallback) == {"service"}
        and str(fallback.get("service", "")).strip().lower().replace(" ", "") == "http_status:404"
    )
    routes = []
    for item in ingress:
        if not isinstance(item, dict):
            continue
        hostname = _cloudflare_hostname(item.get("hostname"))
        service = str(item.get("service", "")).strip()
        if service:
            access = item.get("originRequest") if isinstance(item.get("originRequest"), dict) else {}
            access = access.get("access") if isinstance(access.get("access"), dict) else {}
            audience = access.get("audTag")
            access_team = str(access.get("teamName", "")).strip()
            access_audience = next((str(tag).strip() for tag in audience if str(tag).strip()), "") if isinstance(audience, list) else ""
            routes.append({
                "hostname": hostname,
                "service": service,
                "origin_ok": _cloudflare_origin_matches(service),
                "root_path": item.get("path") in (None, "", "/"),
                "access_required": access.get("required") is True,
                "access_team": bool(access_team),
                "access_team_value": access_team,
                "access_audience": bool(access_audience),
                "access_audience_value": access_audience,
            })
    target_routes = [route for route in routes if route["origin_ok"]]
    if (len(target_routes) == 1 and target_routes[0]["root_path"]
            and target_routes[0]["hostname"]):
        result["origin_target_ok"] = True
        result["hostname"] = target_routes[0]["hostname"]
        configured_hostname = _cloudflare_hostname(os.getenv("CARME_CLOUDFLARE_HOSTNAME", ""))
        if configured_hostname:
            selected = next((route for route in target_routes if route["hostname"] == configured_hostname), None)
            if selected is None:
                result["hostname_mismatch"] = True
                result["hostname"] = ""
            else:
                result["hostname"] = selected["hostname"]
        selected = next((route for route in target_routes if route["hostname"] == result["hostname"]), None)
        if selected:
            result["access_configured"] = bool(selected["access_required"] and selected["access_team"]
                                                and selected["access_audience"])
            result["access_team"] = selected["access_team_value"]
            result["access_audience"] = selected["access_audience_value"]
    result["hostname_configured"] = bool(result["hostname"])
    result["structural_valid"] = bool(result["tunnel_named"] and result["credentials_present"]
                                       and result["origin_target_ok"] and result["fallback_present"]
                                       and result["protocol_valid"])
    result["valid"] = bool(result["structural_valid"] and result["hostname_configured"]
                            and result["access_configured"])
    return result


def _cloudflare_snapshot() -> dict:
    binary, source = _find_cloudflared()
    config_path = Path(os.getenv("CARME_CLOUDFLARE_CONFIG", str(CLOUDFLARE_CONFIG_DEFAULT))).expanduser().resolve()
    config = _read_cloudflare_config(config_path)
    pid_path = Path(os.getenv("CARME_CLOUDFLARE_PID_FILE", str(CLOUDFLARE_PID_DEFAULT))).expanduser().resolve()
    running = _cloudflare_pid_status(pid_path, config_path) if config["exists"] else "not_running"
    token_configured = bool(os.getenv("CARME_TOKEN", "").strip())
    hostname = config["hostname"]
    url = f"https://{hostname}/" if hostname else ""
    base = {
        "state": "unknown",
        "message": "Cloudflare Tunnel 状态未知。",
        "next_steps": [],
        "cloudflared": {"installed": bool(binary), "version": _cloudflared_version(binary) if binary else "", "source": source},
        "config": {
            "exists": config["exists"],
            "valid": config["valid"],
            "structural_valid": config["structural_valid"],
            "origin_target_ok": config["origin_target_ok"],
            "hostname_configured": config["hostname_configured"],
            "hostname_mismatch": config["hostname_mismatch"],
            "credentials_present": config["credentials_present"],
            "tunnel_named": config["tunnel_named"],
            "fallback_present": config["fallback_present"],
            "access_configured": config["access_configured"],
            "protocol": config["protocol"],
            "protocol_valid": config["protocol_valid"],
        },
        "form": {
            "tunnel": config["tunnel"],
            "credentials_file": config["credentials_file"],
            "hostname": config["hostname"],
            "protocol": config["protocol"] if config["protocol_valid"] else "http2",
            "team_name": config["access_team"],
            "audience_tag": config["access_audience"],
        },
        "tunnel_running": running == "running",
        "tunnel_process": running,
        "hostname": hostname,
        "url": url,
        "origin": CLOUDFLARE_ORIGIN,
        "carme_token_configured": token_configured,
        "access": {
            "status": "configured_unverified" if config["access_configured"] else "missing_config",
            "message": "cloudflared 将在转发前校验 Access JWT；允许哪些账号仍需由你在 Cloudflare Dashboard 配置并用受限邮箱实际登录验证。"
            if config["access_configured"] else "配置必须包含 originRequest.access.required、teamName 和 audTag；缺少时不会视为受保护入口。",
        },
        "script": "./deploy/cloudflared/carme-tunnel.sh",
        "protocol_options": ["http2", "quic", "auto"],
    }
    if not binary:
        base.update(state="not_installed", message="未找到 cloudflared；尚未安装，不能开始命名隧道。",
                    next_steps=["在常驻后端 Mac 安装 cloudflared，然后点击重新检查。"])
    elif not config["exists"] or not config["structural_valid"]:
        base.update(state="not_configured", message="cloudflared 已安装，但命名隧道配置尚未完成。",
                    next_steps=["创建命名隧道、填写固定域名和 credentials-file，再执行配置检查。"])
    elif not hostname:
        base.update(state="domain_missing", message="命名隧道配置存在，但还没有与当前 origin 匹配的固定域名。",
                    next_steps=["准备你控制的域名，在 Cloudflare DNS 中接入后填入配置，再重新检查。"])
    elif not config["access_configured"]:
        base.update(state="access_config_required", message="固定域名已配置，但 Cloudflare Access JWT 校验尚未写入该 ingress。",
                    next_steps=["在该 hostname 的 originRequest.access 中填写 required=true、teamName 和 audTag，再用受限邮箱实际登录测试。"])
    elif not token_configured:
        base.update(state="carme_auth_required",
                    message="固定入口尚未可上线：Carme 的 CARME_TOKEN 未配置。先配置并重启现有 8899 服务，再启动隧道。",
                    next_steps=["在 WBAI/.local/active/.env 设置 CARME_TOKEN（不要粘贴到聊天或 URL），重启现有 Carme 服务。",
                                "在 Cloudflare Access 仅允许你的账号/邮箱，然后用受限邮箱实际登录测试。"])
    elif running != "running":
        base.update(state="tunnel_not_running", message="本地配置和 Carme 鉴权已具备，但 cloudflared 隧道当前没有运行。",
                    next_steps=["先运行配置检查，再按选定协议启动命名隧道；连接日志出现成功后再做手机测试。"])
    else:
        base.update(state="access_pending", message="隧道进程正在运行，但 Cloudflare Access 策略与公网入口仍需真实登录验证。",
                    next_steps=["在 Cloudflare Dashboard 确认 Access 只允许你的账号，并在 iPhone Safari 实际登录后验证 API、SSE 和附件。"])
    return base


# --------------------------------------------------------------------------- #
#  鉴权
# --------------------------------------------------------------------------- #


def _session_cookie_value(expected: str) -> str:
    return hmac.new(expected.encode("utf-8"), b"carme-session-v1", hashlib.sha256).hexdigest()


async def require_token(request: Request) -> None:
    """如果设了 CARME_TOKEN，就要求带 token。

    局域网内自用可以不设；一旦要从外网访问手机端，务必设上。
    """
    expected = os.getenv("CARME_TOKEN", "").strip()
    if not expected:
        return
    provided = (
        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or request.query_params.get("token", "")
    )
    if not provided:
        cookie = request.cookies.get("carme_session", "")
        if cookie and hmac.compare_digest(cookie, _session_cookie_value(expected)):
            return
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="token 不正确")


# --------------------------------------------------------------------------- #
#  路由
# --------------------------------------------------------------------------- #


def build_router(config: Config, store: Store, runtime: Runtime) -> APIRouter:
    router = APIRouter(prefix="/api")
    model_probes: dict[str, dict] = {}
    model_save_gate = asyncio.Lock()
    avatar_dir = store.path.parent / "avatars"

    def memory_owner(scope: str) -> str:
        from ..tools.memory import SHARED_AGENT
        if scope == "__shared__":
            return SHARED_AGENT
        try:
            return config.agents.get(scope).id
        except KeyError:
            raise HTTPException(404, "Bot 不存在") from None

    @router.get("/memory/{scope}")
    async def read_memory(scope: str) -> dict:
        return {"memory": store.recall(memory_owner(scope))}

    @router.put("/memory/{scope}")
    async def write_memory(scope: str, body: MemoryEntry) -> dict:
        owner = memory_owner(scope)
        try:
            store.remember(owner, body.key, body.value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("memory.updated", {"scope": scope})
        return {"memory": store.recall(owner)}

    @router.delete("/memory/{scope}")
    async def delete_memory(scope: str, key: str) -> dict:
        owner = memory_owner(scope)
        store.forget(owner, key)
        await runtime._emit("memory.updated", {"scope": scope})
        return {"memory": store.recall(owner)}

    @router.post("/conversations/{conversation_id}/attachments", status_code=201)
    async def upload_attachment(conversation_id: str, request: Request, name: str) -> dict:
        from ..attachments import save_file, MAX_BYTES
        conversation = store.get_conversation(conversation_id)
        if not conversation or conversation["deleted_at"]:
            raise HTTPException(404, "会话不存在或已删除")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HTTPException(413, "每个附件不超过 10 MB")
        try:
            file = await asyncio.to_thread(save_file, store, conversation_id, name, bytes(raw))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ValueError, UnicodeError)) else "无法解析文件，请检查文件内容与格式"
            raise HTTPException(422, detail[:300]) from None
        return {"file": file}

    def require_file(conversation_id: str, file_id: str) -> dict:
        file = store.get_attachment(file_id)
        if not file or file["conversation_id"] != conversation_id:
            raise HTTPException(404, "文件不存在")
        return file

    @router.get("/conversations/{conversation_id}/attachments/{file_id}")
    async def attachment_detail(conversation_id: str, file_id: str) -> dict:
        return {"file": require_file(conversation_id, file_id)}

    @router.get("/conversations/{conversation_id}/attachments/{file_id}/download")
    async def download_attachment(conversation_id: str, file_id: str):
        from ..attachments import file_path
        file = require_file(conversation_id, file_id)
        path = file_path(store, file_id)
        if not path.is_file():
            raise HTTPException(404, "归档文件丢失，请从备份恢复")
        return FileResponse(path, filename=file["name"], media_type="application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @router.delete("/conversations/{conversation_id}/attachments/{file_id}")
    async def remove_draft_attachment(conversation_id: str, file_id: str) -> dict:
        from ..attachments import file_path
        # 与消息提交共用数据库锁，已发送文件不能被草稿清理误删。
        with store._lock:
            file = require_file(conversation_id, file_id)
            if file["message_id"] or file["kind"] != "upload":
                raise HTTPException(409, "只能移除尚未发送的附件")
            store._write("DELETE FROM attachments WHERE id=?", (file_id,))
            file_path(store, file_id).unlink(missing_ok=True)
        return {"removed": True}

    @router.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "version": "0.2.0",
            "time": time.time(),
            "running_tasks": runtime.running,
            "sandboxes_live": runtime.sandboxes.live_count,
            "sse_subscribers": runtime.bus.subscriber_count,
        }

    @router.post("/session")
    async def create_session(request: Request):
        """Issue a short-scoped browser session cookie so SSE need not put CARME_TOKEN in its URL."""
        expected = os.getenv("CARME_TOKEN", "").strip()
        response = JSONResponse({"ok": True})
        if expected:
            forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
            response.set_cookie(
                "carme_session",
                _session_cookie_value(expected),
                max_age=12 * 60 * 60,
                httponly=True,
                secure=request.url.scheme == "https" or forwarded_proto == "https",
                samesite="lax",
                path="/",
            )
        return response

    @router.get("/cloudflare")
    async def cloudflare_status() -> dict:
        # 只读取本机安装、配置和脚本 pid；不登录 Cloudflare 或修改隧道。
        return await asyncio.to_thread(_cloudflare_snapshot)

    @router.put("/cloudflare/config")
    async def save_cloudflare_config(body: CloudflareConfigUpdate) -> dict:
        hostname = _cloudflare_hostname(body.hostname)
        credentials = body.credentials_file.strip()
        team_name = body.team_name.strip()
        audience_tag = body.audience_tag.strip()
        if not hostname:
            raise HTTPException(422, "请输入不含协议、路径或通配符的固定域名")
        if not team_name or not audience_tag:
            raise HTTPException(422, "Access Team Name 和 Audience Tag 不能为空")
        if any(ord(char) < 32 for char in credentials) or "BEGIN " in credentials.upper():
            raise HTTPException(422, "credentials-file 只能填写后端已有凭据文件的路径，不要粘贴凭据内容")
        path = Path(os.getenv("CARME_CLOUDFLARE_CONFIG", str(CLOUDFLARE_CONFIG_DEFAULT))).expanduser()
        if path.is_file():
            try:
                existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeError, yaml.YAMLError):
                raise HTTPException(409, "现有 Cloudflare 配置无法安全读取，请先人工备份并修复") from None
            ingress = existing.get("ingress") if isinstance(existing, dict) else None
            carme_owned = (
                isinstance(existing, dict)
                and not set(existing) - {"tunnel", "credentials-file", "protocol", "ingress"}
                and isinstance(ingress, list)
                and len(ingress) == 2
                and isinstance(ingress[0], dict)
                and set(ingress[0]) == {"hostname", "service", "originRequest"}
                and bool(_cloudflare_hostname(ingress[0].get("hostname")))
                and _cloudflare_origin_matches(ingress[0].get("service"))
                and ingress[0].get("path") in (None, "", "/")
                and isinstance(ingress[0].get("originRequest"), dict)
                and set(ingress[0]["originRequest"]) == {"access"}
                and isinstance(ingress[0]["originRequest"].get("access"), dict)
                and set(ingress[0]["originRequest"]["access"]) == {"required", "teamName", "audTag"}
                and ingress[0]["originRequest"]["access"].get("required") is True
                and bool(str(ingress[0]["originRequest"]["access"].get("teamName", "")).strip())
                and isinstance(ingress[0]["originRequest"]["access"].get("audTag"), list)
                and len(ingress[0]["originRequest"]["access"]["audTag"]) == 1
                and bool(str(ingress[0]["originRequest"]["access"]["audTag"][0]).strip())
                and isinstance(ingress[1], dict)
                and set(ingress[1]) == {"service"}
                and str(ingress[1].get("service", "")).strip().lower().replace(" ", "") == "http_status:404"
            )
            if not carme_owned:
                raise HTTPException(409, "现有配置不是 Carme 独占的单路由结构；为避免丢失其他 ingress 或 originRequest，未覆盖原文件")
        raw = {
            "tunnel": body.tunnel,
            "credentials-file": credentials,
            "protocol": body.protocol,
            "ingress": [{
                "hostname": hostname,
                "service": CLOUDFLARE_ORIGIN,
                "originRequest": {
                    "access": {
                        "required": True,
                        "teamName": team_name,
                        "audTag": [audience_tag],
                    },
                },
            }, {"service": "http_status:404"}],
        }
        try:
            config_module.atomic_write(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode())
        except (OSError, ValueError) as exc:
            raise HTTPException(500, f"无法写入 Cloudflare 配置：{str(exc)[:180]}") from None
        await runtime._emit("cloudflare.updated", {})
        return await asyncio.to_thread(_cloudflare_snapshot)

    # ---------------- 成员 ----------------

    @router.get("/agents")
    async def list_agents() -> dict:
        entry = config.agents.entry_agent.id
        agents = []
        for spec in config.agents.agents.values():
            agents.append(
                {
                    "id": spec.id,
                    "name": spec.name,
                    "title": spec.title,
                    "emoji": spec.emoji,
                    "entry": spec.entry,
                    "tier": spec.tier,
                    "model": spec.model,
                    "effort": spec.effort,
                    "engine": spec.engine,
                    "engine_model": spec.engine_model,
                    "engine_effort": spec.engine_effort,
                    "engine_workspace": spec.engine_workspace,
                    "avatar": spec.avatar,
                    "sandbox": spec.sandbox,
                    "tools": spec.tools,
                    "can_delegate": spec.can_delegate,
                    "summary": (spec.prompt.strip().splitlines() or [""])[0][:160],
                }
            )
        return {"entry": entry, "agents": agents}

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str) -> dict:
        try:
            spec = config.agents.get(agent_id)
        except KeyError:
            raise HTTPException(404, f"没有成员 {agent_id}") from None
        return {**asdict(spec), "memory": store.recall(spec.id)}

    @router.get("/engines")
    async def list_engines() -> dict:
        """探测本机固定 CLI；发现二进制不等于已经登录或具体模型可用。"""
        api_ready = any(provider.available and provider.type != "mock"
                        for provider in config.models.providers.values())
        built_in = [{"id": "api", "label": "Carme API 网关", "binary": "", "installed": True,
                     "ready": api_ready, "status": "ready" if api_ready else "not_configured",
                     "version": "内置", "auth_status": "configured" if api_ready else "unknown",
                     "capability": "Carme 工具与现有模型配置"}]
        return {"engines": built_in + await asyncio.to_thread(detect_cli_engines)}

    @router.post("/engines/{engine_id}/test")
    async def test_engine(engine_id: str, body: EngineTest) -> dict:
        if engine_id not in CLI_ENGINE_DEFS:
            raise HTTPException(422, "请选择已发现的 CLI 引擎")
        try:
            response = await runtime.gateway.chat(
                [{"role": "user", "content": "这是 Carme 的连接测试。不要读取文件、调用工具或修改任何内容，只回复 OK。"}],
                engine=engine_id, engine_model=body.model.strip(), effort=body.effort.strip(), tools=None,
                retries_per_model=1,
            )
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "engine": engine_id, "model": response.model,
                "reply": response.text[:200], "message": "CLI 文本调用成功；这不代表 Carme 工具桥接已接通。"}

    def save_agent(body: AgentUpdate, agent_id: str, *, creating: bool) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise HTTPException(422, "成员配置字段不能为 null")
        if changes.get("id", agent_id) != agent_id:
            raise HTTPException(400, "不能修改成员 id")
        exists = agent_id in config.agents.agents
        if creating and exists:
            raise HTTPException(409, "成员 id 已存在")
        if not creating and not exists:
            raise HTTPException(404, "成员不存在")
        if not agent_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id):
            raise HTTPException(422, "请提供有效的成员 id")
        changes.pop("id", None)
        if "avatar" in changes:
            avatar = changes["avatar"]
            if avatar:
                if avatar.get("kind") == "bot":
                    if (set(avatar) != {"kind", "shape", "color"} or avatar.get("shape") not in
                            ("circle", "oval", "square", "pill", "triangle", "hexagon", "cloud", "drop")
                            or not re.fullmatch(r"#[0-9a-fA-F]{6}", str(avatar.get("color", "")))):
                        raise HTTPException(422, "Bot 头像的形状或颜色无效")
                elif avatar.get("kind") == "image":
                    filename = str(avatar.get("file", ""))
                    if (set(avatar) != {"kind", "file"} or not re.fullmatch(r"[a-f0-9]{32}\.webp", filename)
                            or not (avatar_dir / filename).is_file()):
                        raise HTTPException(422, "请先上传有效的头像图片")
                else:
                    raise HTTPException(422, "不支持的头像类型")
        path = config_module.CONFIG_DIR / "agents.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
        roster = raw.setdefault("agents", {})
        current = roster.get(agent_id, {})
        if creating:
            current = {"name": agent_id, "sandbox": "remote", "tools": ["memory", "browser"]}
        merged = {**current, **changes}
        engine = str(merged.get("engine", "api") or "api")
        if engine not in config_module.ENGINE_IDS:
            raise HTTPException(422, "不支持的引擎")
        engine_model = str(merged.get("engine_model", "") or "").strip()
        if engine_model and (any(ord(char) < 33 or ord(char) > 126 for char in engine_model)
                             or engine_model.startswith("-") or len(engine_model) > 200):
            raise HTTPException(422, "CLI 模型名只能使用可见 ASCII 字符，且不能以短横线开头")
        merged["engine"] = engine
        merged["engine_model"] = engine_model
        engine_effort = str(merged.get("engine_effort", "") or "").strip()
        if any(ord(char) < 33 or ord(char) > 126 for char in engine_effort) or len(engine_effort) > 16:
            raise HTTPException(422, "CLI effort 只能使用可见 ASCII 字符，且长度不能超过 16")
        merged["engine_effort"] = engine_effort
        engine_workspace = str(merged.get("engine_workspace", "") or "").strip()
        if engine_workspace and (any(ord(char) < 32 for char in engine_workspace)
                                 or len(engine_workspace) > 500):
            raise HTTPException(422, "CLI 工作目录不能包含控制字符，且长度不能超过 500")
        merged["engine_workspace"] = engine_workspace
        if not str(merged.get("name", "")).strip():
            raise HTTPException(422, "成员名称不能为空")
        valid_tools = set(TOOL_GROUPS) | set(runtime.registry.names())
        unknown_tools = set(merged.get("tools", [])) - valid_tools
        if unknown_tools:
            raise HTTPException(422, f"未知工具：{', '.join(sorted(unknown_tools))}")
        # CLI bots retain their API model/tier/effort for a later switch back,
        # but those fields are not required or resolvable while CLI is active.
        if engine == "api":
            if merged.get("model"):
                try:
                    config.models.resolve(merged["model"])
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
                if (merged["model"] in config.models.invalid_refs
                        and merged["model"] != current.get("model", "")):
                    raise HTTPException(422, "该模型在最近一次列表检测中不存在或无权访问，请重新测试连接并选择实际返回的型号")
            elif merged.get("tier", "balanced") not in config.models.tiers:
                raise HTTPException(422, "模型档位未配置")
            effort = merged.get("effort", "")
            if effort and (not merged.get("model") or effort not in config.models.price(merged["model"]).effort_options):
                raise HTTPException(422, "该模型尚未确认支持此 effort，请在模型设置中检测或选择默认值")
        if merged.get("entry"):
            for settings in roster.values():
                settings["entry"] = False
        roster[agent_id] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".yaml", delete=False, encoding="utf-8") as stream:
                temporary = Path(stream.name)
                yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        config.agents = config_module.load(reload=True).agents
        runtime.config.agents = config.agents
        return asdict(config.agents.get(agent_id))

    @router.post("/agents", status_code=201)
    async def create_agent(body: AgentUpdate) -> dict:
        agent = save_agent(body, body.id or "", creating=True)
        await runtime._emit("agent.updated", {"agent_id": agent["id"]}, "", agent["id"])
        return {"agent": agent}

    @router.patch("/agents/{agent_id}")
    async def update_agent(agent_id: str, body: AgentUpdate) -> dict:
        agent = save_agent(body, agent_id, creating=False)
        await runtime._emit("agent.updated", {"agent_id": agent_id}, "", agent_id)
        return {"agent": agent}

    @router.post("/agents/{agent_id}/duplicate", status_code=201)
    async def duplicate_agent(agent_id: str) -> dict:
        try:
            original = asdict(config.agents.get(agent_id))
        except KeyError:
            raise HTTPException(404, "Bot 不存在") from None
        original.update(id="bot_" + secrets.token_hex(6), name=original["name"][:75] + " 副本", entry=False)
        agent = save_agent(AgentUpdate(**original), original["id"], creating=True)
        conversation = store.create_conversation([agent["id"]], title=agent["name"])
        await runtime._emit("agent.updated", {"agent_id": agent["id"], "conversation_id": conversation["id"]})
        return {"agent": agent, "conversation": conversation}

    @router.post("/avatars", status_code=201)
    async def upload_avatar(request: Request) -> dict:
        # 限制实际读取量，不能只信 Content-Length 或浏览器传来的 MIME。
        from PIL import Image, ImageOps, UnidentifiedImageError
        if request.headers.get("content-type", "").split(";")[0] not in ("image/png", "image/jpeg", "image/webp"):
            raise HTTPException(415, "支持 PNG、JPEG、WebP 静态图片；不支持 GIF、SVG 或 HEIC")
        buffer = bytearray()
        async for chunk in request.stream():
            if len(buffer) + len(chunk) > 5 * 1024 * 1024:
                raise HTTPException(413, "头像文件不能超过 5 MB")
            buffer.extend(chunk)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(buffer)) as original:
                    if original.format not in ("PNG", "JPEG", "WEBP") or getattr(original, "n_frames", 1) != 1:
                        raise ValueError("请上传 PNG、JPEG 或 WebP 静态图片")
                    if min(original.size) < 32 or max(original.size) > 4096:
                        raise ValueError("图片宽高均需在 32–4096 像素之间，建议正方形 512×512")
                    original.load()
                    # 确认真实格式并重新编码；只保留像素，去掉 EXIF、位置等元数据。
                    picture = ImageOps.fit(ImageOps.exif_transpose(original).convert("RGBA"), (512, 512))
                    clean = Image.new("RGBA", picture.size)
                    clean.paste(picture)
                    output = io.BytesIO()
                    clean.save(output, "WEBP", quality=90)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise HTTPException(422, "图片损坏、内容与格式不符或尺寸过大") from None
        filename = secrets.token_hex(16) + ".webp"
        config_module.atomic_write(avatar_dir / filename, output.getvalue())
        return {"avatar": {"kind": "image", "file": filename}, "width": 512, "height": 512}

    @router.get("/avatars/{filename}")
    async def avatar_image(filename: str):
        if not re.fullmatch(r"[a-f0-9]{32}\.webp", filename) or not (avatar_dir / filename).is_file():
            raise HTTPException(404, "头像不存在")
        return FileResponse(avatar_dir / filename, media_type="image/webp",
                            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    # ---------------- 持续会话 ----------------

    @router.get("/conversations")
    async def list_conversations(view: str = "active") -> dict:
        try:
            return {"conversations": store.list_conversations(view)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.post("/conversations", status_code=201)
    async def create_conversation(body: NewConversation) -> dict:
        try:
            members = [config.agents.get(member) for member in dict.fromkeys(body.agent_ids)]
            title = body.title.strip() or "、".join(member.name for member in members)
            conversation = store.create_conversation([member.id for member in members], title=title)
        except (KeyError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("conversation.created", {"conversation_id": conversation["id"]}, "", "")
        return {"conversation": conversation}

    @router.patch("/conversations/{conversation_id}")
    async def update_conversation(conversation_id: str, body: ConversationUpdate) -> dict:
        if store.get_conversation(conversation_id) is None:
            raise HTTPException(404, "会话不存在")
        changes = body.model_dump(exclude_none=True)
        for name in ("title", "folder"):
            if name in changes:
                changes[name] = changes[name].strip()
        if changes.get("title") == "":
            raise HTTPException(422, "名称不能为空")
        try:
            conversation = store.update_conversation(conversation_id, changes)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        await runtime._emit("conversation.updated", {"conversation_id": conversation_id})
        return {"conversation": conversation}

    @router.get("/conversations/{conversation_id}")
    async def conversation_detail(conversation_id: str) -> dict:
        conversation = store.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(404, "会话不存在")
        return {
            "conversation": conversation,
            "messages": store.list_conversation_messages(conversation_id),
            "tasks": [public_task(task) for task in store.list_conversation_tasks(conversation_id)],
            "approvals": store.list_conversation_approvals(conversation_id),
            "summary": store.get_summary(conversation_id),
            "files": store.list_attachments(conversation_id),
        }

    @router.post("/conversations/{conversation_id}/messages")
    async def send_message(conversation_id: str, body: ConversationMessage) -> dict:
        if store.get_conversation(conversation_id) is None:
            raise HTTPException(404, "会话不存在")
        if not body.content.strip() or not body.request_id.strip():
            raise HTTPException(422, "消息内容和 request_id 不能为空")
        try:
            if len(set(body.attachment_ids)) != len(body.attachment_ids):
                raise ValueError("附件不能重复")
            result = await runtime.submit_message(conversation_id, body.content, body.request_id, body.agent_id,
                                                 **({"attachment_ids": body.attachment_ids} if body.attachment_ids else {}))
        except BudgetExceeded as exc:
            raise HTTPException(429, str(exc)) from None
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            conflict = "request_id" in str(exc) or "请求标识" in str(exc)
            raise HTTPException(409 if conflict else 422, str(exc)) from None
        return result

    # ---------------- 可替换的执行电脑 ----------------

    @router.get("/nodes")
    async def list_nodes() -> dict:
        return {"nodes": config.sandbox.list_nodes(), "default_node_id": config.sandbox.default_node_id}

    def apply_nodes(fresh) -> None:
        config.sandbox = fresh
        runtime.config.sandbox = fresh

    @router.post("/nodes", status_code=201)
    async def create_node(body: dict) -> dict:
        node_id = body.get("node_id", body.get("id"))
        if any(node["node_id"] == node_id for node in config.sandbox.list_nodes()):
            raise HTTPException(409, "执行电脑 id 已存在")
        try:
            apply_nodes(config_module.save_node(body))
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("node.updated", {"node_id": node_id})
        return await list_nodes()

    @router.patch("/nodes/{node_id}")
    async def update_node(node_id: str, body: dict) -> dict:
        old = next((node for node in config.sandbox.list_nodes() if node["node_id"] == node_id), None)
        if old is None:
            raise HTTPException(404, "执行电脑不存在")
        if body.get("node_id", body.get("id", node_id)) != node_id:
            raise HTTPException(422, "不能修改执行电脑 id")
        old = {key: value for key, value in old.items() if key not in {"is_default", "configured", "status"}}
        for key in ("browser", "desktop"):
            if isinstance(body.get(key), dict):
                body[key] = {**old.get(key, {}), **body[key]}
        try:
            apply_nodes(config_module.save_node({**old, **body, "node_id": node_id}))
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("node.updated", {"node_id": node_id})
        return await list_nodes()

    @router.post("/nodes/{node_id}/default")
    async def default_node(node_id: str) -> dict:
        try:
            apply_nodes(config_module.set_default_node(node_id))
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("node.updated", {"node_id": node_id, "default": True})
        return await list_nodes()

    @router.post("/nodes/{node_id}/probe")
    async def probe_node(node_id: str) -> dict:
        return await runtime.sandboxes.probe_node(node_id)

    # ---------------- 任务 ----------------

    @router.post("/tasks")
    async def create_task(body: NewTask) -> dict:
        agent_id = body.agent_id.strip() or config.agents.entry_agent.id
        try:
            config.agents.get(agent_id)
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from None
        try:
            task_id = await runtime.submit(
                agent_id, body.goal, title=body.title, source="web"
            )
        except BudgetExceeded as exc:
            raise HTTPException(429, str(exc)) from None
        return {"task_id": task_id, "agent_id": agent_id}

    @router.get("/tasks")
    async def list_tasks(limit: int = 50, agent_id: str = "", status: str = "") -> dict:
        tasks = store.list_tasks(
            agent_id=agent_id or None, status=status or None, limit=max(1, min(limit, 200))
        )
        return {"tasks": [public_task(task) for task in tasks]}

    @router.get("/tasks/{task_id}")
    async def task_detail(task_id: str) -> dict:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return {
            "task": public_task(task),
            "messages": store.list_messages(task_id),
            "children": [public_task(child) for child in store.children_of(task_id)],
        }

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        if store.get_task(task_id) is None:
            raise HTTPException(404, "任务不存在")
        await runtime.cancel(task_id)
        return {"ok": True}

    # ---------------- 实时事件流 ----------------

    @router.get("/events")
    async def events(request: Request, task_id: str = "", after_id: int = 0):
        """SQLite 是事件来源；队列只唤醒读取，慢连接也不会因队列溢出漏事件。"""
        try:
            cursor = max(after_id, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Last-Event-ID 必须是事件序号") from None
        if cursor < 0 or after_id < 0:
            raise HTTPException(400, "事件序号不能为负数")
        # 先订阅再查持久化历史，覆盖查询与开始监听之间的事件。
        queue = await runtime.bus.subscribe(replay=False)

        async def generator():
            nonlocal cursor
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    batch = store.list_events(after_id=cursor, limit=500)
                    if batch:
                        for event in batch:
                            cursor = event["id"]
                            if task_id and event.get("task_id") not in ("", task_id):
                                continue
                            yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                        continue
                    try:
                        await asyncio.wait_for(queue.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # 防代理掐断
            finally:
                await runtime.bus.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ---------------- 人工确认闸门 ----------------

    @router.get("/approvals")
    async def list_approvals(status: str = "pending", limit: int = 50) -> dict:
        """列出待你点头的动作。默认只看 pending 的。"""
        rows = store.list_approvals(status=status or None, limit=max(1, min(limit, 200)))
        return {"approvals": rows, "pending": store.pending_approval_count()}

    @router.post("/approvals/{approval_id}/decide")
    async def decide_approval(approval_id: str, body: ApprovalDecision) -> dict:
        """批准或拒绝一个挂起的动作。

        批准后，那个正阻塞等待的任务会在 1 秒内继续往下走。
        拒绝的话，模型会收到拒绝理由，自己换个做法。
        """
        row = store.get_approval(approval_id)
        if row is None:
            raise HTTPException(404, "没有这条审批记录")
        if row["status"] != "pending":
            raise HTTPException(409, f"这条已经处理过了（当前状态：{row['status']}）")

        ok = store.decide_approval(
            approval_id, approved=body.approved, note=body.note.strip()
        )
        if not ok:
            raise HTTPException(409, "状态刚刚变化了，请刷新后重试")

        await runtime._emit(
            "approval.decided",
            {
                "approval_id": approval_id,
                "approved": body.approved,
                "note": body.note.strip()[:200],
            },
            row.get("task_id", ""),
            row.get("agent_id", ""),
        )
        return {"ok": True, "approved": body.approved}

    # ---------------- 代操作自检 ----------------

    @router.get("/browser/probe")
    async def probe_browser() -> dict:
        """检查所选执行电脑的浏览器，不隐式启动后端本机浏览器。"""
        try:
            node = config.sandbox.resolve_node()
        except ValueError as exc:
            return {"ok": False, "status": "unconfigured", "error": str(exc)}
        result = await runtime.browsers.probe(node=node)
        result["notify"] = runtime.notifier.describe()
        result["require_confirmation"] = config.browser.require_confirmation
        result["approval_timeout_seconds"] = config.browser.approval_timeout
        result["dangerous_patterns"] = config.browser.dangerous_patterns
        result["pending_approvals"] = store.pending_approval_count()
        return result

    # ---------------- 运维 ----------------

    @router.get("/stats")
    async def stats() -> dict:
        return {
            **store.stats(),
            "budget": runtime.budget_status(),
            "runtime": {
                "running_tasks": runtime.running,
                "sandboxes_live": runtime.sandboxes.live_count,
                "browsers_live": runtime.browsers.live_count,
                "pending_approvals": store.pending_approval_count(),
                "max_concurrent_tasks": config.sandbox.max_concurrent_tasks,
                "max_concurrent_sandbox": config.sandbox.max_concurrent_sandbox,
                "max_concurrent_browser": config.browser.max_concurrent,
            },
            "models": {
                "providers_ready": [
                    name
                    for name, p in config.models.providers.items()
                    if p.available and not p.is_local
                ],
                "local_providers": [
                    name for name, p in config.models.providers.items() if p.is_local
                ],
                "mock_enabled": config.models.allow_mock,
                "tiers": {
                    tier: config.models.candidates(tier)
                    for tier in config.models.tiers
                },
            },
        }

    @router.get("/usage")
    async def usage(days: int = 7) -> dict:
        return {
            "summary": store.usage_summary(days=max(1, min(days, 90))),
            "spend_today_usd": store.spend_today(),
        }

    @router.post("/sandbox/probe")
    async def probe_sandbox(body: ProbeRequest) -> dict:
        mode = body.mode.strip() or "remote"
        if mode == "remote":
            return await runtime.sandboxes.probe_node()
        result = await runtime.sandboxes.probe(mode)
        return result

    # ---------------- 模型连接配置 ----------------

    def connection_from(body: ModelConnection):
        if body.type not in config_module.API_TYPES:
            raise HTTPException(422, "请选择已支持的 API 协议；其他服务可使用 OpenAI 兼容协议")
        try:
            base_url = normalise_model_url(body.base_url, body.type)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        current = config.models.providers.get(body.id)
        key = body.api_key.get_secret_value().strip() if body.api_key is not None else ""
        if not key and current:
            # 用户改网址后必须显式重新输入 key，避免把保存的凭据转发到新站点。
            if (normalise_model_url(current.base_url, current.type) != base_url or current.type != body.type):
                raise HTTPException(422, "更换 API 类型或网址后，请重新输入该连接的 API key")
            key = current.api_key
        if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise HTTPException(422, "请输入有效的 API key（需为 ASCII 字符，不能包含空格或换行）")
        return config_module.Provider(body.id, body.type, base_url, "", key, label=body.label.strip())

    def connection_fingerprint(provider):
        return hashlib.sha256(json.dumps([provider.name, provider.type, provider.base_url,
                                         provider.api_key]).encode()).hexdigest()

    def checked_probe(body, provider):
        proof = model_probes.get(body.probe_id)
        if (not proof or time.monotonic() - proof["time"] > 900
                or proof["fingerprint"] != connection_fingerprint(provider)):
            raise HTTPException(409, "连接信息已更改或测试已过期，请重新测试连接")
        return proof

    @router.get("/models")
    async def configured_models() -> dict:
        connections = [{"id": name, "label": provider.label or name, "type": provider.type,
                        "base_url": provider.base_url, "has_key": provider.available}
                       for name, provider in config.models.providers.items() if provider.type in config_module.API_TYPES]
        models = []
        refs = set(config.models.models)
        refs.update(ref for values in config.models.tiers.values() for ref in values)
        for ref in sorted(refs):
            try:
                provider_id, provider, model_id = config.models.resolve(ref)
            except ValueError:
                continue
            spec = config.models.price(ref)
            invalid = ref in config.models.invalid_refs
            models.append({"ref": ref, "id": model_id, "provider_id": provider_id,
                           "api_type": provider.type,
                           "provider_label": provider.label or provider_id, "effort": spec.effort,
                           "effort_options": spec.effort_options, "effort_source": spec.effort_source,
                           "verified": spec.verified,
                           "available": (not invalid) and provider.available and
                           (provider.type != "mock" or config.models.allow_mock),
                           "status": "invalid_model" if invalid else "configured"})
        return {"connections": connections, "models": models, "api_types": config_module.API_TYPES,
                "tiers": config.models.tiers, "allow_mock": config.models.allow_mock}

    @router.patch("/models/routing")
    async def save_model_routing(body: ModelRouting) -> dict:
        if set(body.tiers) != set(config.models.tiers) or not body.tiers:
            raise HTTPException(422, "请为现有的每个模型档位设置候选")
        for refs in body.tiers.values():
            if not refs or len(refs) > 8 or len(set(refs)) != len(refs):
                raise HTTPException(422, "每个档位需要 1 至 8 个不同候选模型")
            for ref in refs:
                try:
                    _, provider, _ = config.models.resolve(ref)
                except ValueError:
                    raise HTTPException(422, "请选择已配置的模型") from None
                known = ref in config.models.models or any(ref in values for values in config.models.tiers.values())
                if not known or not provider.available or (provider.type == "mock" and not body.allow_mock):
                    raise HTTPException(422, "所选模型不可用，或演示模型已关闭")
        async with model_save_gate:
            invalid_refs = set(config.models.invalid_refs)
            config.models = config_module.save_model_routing(body.tiers, body.allow_mock)
            config.models.invalid_refs = invalid_refs
            runtime.config.models = config.models
            runtime.gateway.config.models = config.models
        await runtime._emit("models.updated", {})
        return {"ok": True}

    @router.post("/models/connections/test")
    async def test_model_connection(body: ModelConnection) -> dict:
        provider = connection_from(body)
        try:
            models = await runtime.gateway.discover_models(provider)
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}
        if not models:
            return {"ok": False, "error": "连接已响应，但此 key 没有可见模型，请检查账号权限或 API 类型"}
        visible = {item["id"] for item in models}
        stale = set()
        for ref in set(config.models.models) | {item for refs in config.models.tiers.values() for item in refs}:
            try:
                provider_id, _, _ = config.models.resolve(ref)
            except ValueError:
                continue
            if provider_id == provider.name and ref.split("/", 1)[1] not in visible:
                stale.add(ref)
        config.models.invalid_refs = {
            ref for ref in config.models.invalid_refs
            if not ref.startswith(provider.name + "/")
        } | stale
        runtime.config.models.invalid_refs = set(config.models.invalid_refs)
        runtime.gateway.config.models.invalid_refs = set(config.models.invalid_refs)
        current = config.models.providers.get(provider.name)
        if (current and current.type == provider.type and current.api_key == provider.api_key
                and normalise_model_url(current.base_url, current.type) == provider.base_url):
            for item in models:
                previous = config.models.models.get(f"{provider.name}/{item['id']}")
                if item["effort_source"] == "unknown" and previous and previous.verified and previous.effort_options:
                    # 保留同一连接此前验证的选择；保存时仍会实际调用所选 effort。
                    item.update(effort_options=list(previous.effort_options), effort_source="saved")
        for key in list(model_probes):
            if time.monotonic() - model_probes[key]["time"] > 900:
                model_probes.pop(key)
        while len(model_probes) >= 32:
            model_probes.pop(next(iter(model_probes)))
        probe_id = secrets.token_urlsafe(24)
        model_probes[probe_id] = {"time": time.monotonic(), "fingerprint": connection_fingerprint(provider),
                                  "models": {item["id"]: item for item in models}}
        message = f"连接成功，已读取 {len(models)} 个模型。保存时会验证所选模型调用。"
        if stale:
            message += f" 最近配置中有 {len(stale)} 个型号未出现在列表里，已标记为待重新选择；不会自动换型号。"
        return {"ok": True, "probe_id": probe_id, "models": models,
                "stale_refs": sorted(stale), "message": message}

    @router.post("/models/connections/efforts")
    async def detect_model_efforts(body: EffortProbe) -> dict:
        provider = connection_from(body)
        proof = checked_probe(body, provider)
        model = proof["models"].get(body.model_id)
        if model is None:
            raise HTTPException(422, "请选择本次测试返回的模型")
        try:
            result = await runtime.gateway.detect_efforts(provider, body.model_id)
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}
        model.update({key: result[key] for key in ("effort_options", "effort_source")})
        return {"ok": True, **result}

    @router.post("/models/connections")
    async def save_connection(body: ModelConnectionSave) -> dict:
        provider = connection_from(body)
        proof = checked_probe(body, provider)
        if len({item.id for item in body.models}) != len(body.models):
            raise HTTPException(422, "不能重复选择模型")
        selected = []
        for item in body.models:
            known = proof["models"].get(item.id)
            if known is None or (item.effort and item.effort not in known["effort_options"]):
                raise HTTPException(422, "所选模型或 effort 尚未确认可用，请重新测试")
            selected.append({"id": item.id, "effort": item.effort,
                             "effort_options": known["effort_options"], "effort_source": known["effort_source"]})
        # 同配置的重复保存串行；网络调用不持有文件锁，不修改 Bot、会话或记忆。
        async with model_save_gate:
            invalid_refs = set(config.models.invalid_refs)
            for item in selected:
                result = await runtime.gateway.test_model(provider, item["id"], item["effort"])
                if not result["ok"]:
                    return {"ok": False, "error": f"{item['id']}：{result['error']}；配置尚未保存"}
            try:
                config.models = config_module.save_model_connection(provider, selected)
                config.models.invalid_refs = invalid_refs - {f"{provider.name}/{item['id']}" for item in selected}
            except (OSError, ValueError):
                raise HTTPException(500, "无法写入模型配置，请检查后端配置目录与 .env 的写入权限") from None
            runtime.config.models = config.models
            runtime.gateway.config.models = config.models
        await runtime._emit("models.updated", {"provider_id": provider.name})
        return {"ok": True, "message": "连接与所选模型已保存，Bot 现在可以选择这些模型"}

    def model_refs_in_use(provider_name: str) -> tuple[list[str], list[str]]:
        """返回仍引用该供应商模型的（团队档位候选, 成员固定模型），用于删除前拦截。"""
        prefix = f"{provider_name}/"
        tier_refs = sorted({ref for refs in config.models.tiers.values() for ref in refs if ref.startswith(prefix)})
        agent_refs = sorted({agent.model for agent in config.agents.agents.values()
                             if agent.model and agent.model.startswith(prefix)})
        return tier_refs, agent_refs

    @router.post("/models/connections/{provider_id}/models/delete")
    async def remove_configured_models(provider_id: str, body: ModelRemoval) -> dict:
        provider = config.models.providers.get(provider_id)
        if provider is None or provider.type not in config_module.API_TYPES:
            raise HTTPException(404, "连接不存在")
        refs = []
        for model_id in body.refs:
            model_id = model_id.strip()
            if not model_id or len(model_id) > 200 or any(ord(c) < 33 for c in model_id):
                raise HTTPException(422, "模型名无效")
            refs.append(f"{provider_id}/{model_id}")
        tier_refs, agent_refs = model_refs_in_use(provider_id)
        conflicts = []
        tier_hits = sorted(set(tier_refs) & set(refs))
        agent_hits = sorted(set(agent_refs) & set(refs))
        if tier_hits:
            conflicts.append(f"团队默认档位仍在使用：{', '.join(tier_hits)}，请先在「团队默认模型」中调整")
        if agent_hits:
            conflicts.append(f"以下成员固定使用该模型：{', '.join(agent_hits)}，请先修改成员设置")
        if conflicts:
            raise HTTPException(409, "；".join(conflicts))
        async with model_save_gate:
            config.models = config_module.delete_models(provider_id, [model_id.strip() for model_id in body.refs])
            config.models.invalid_refs = {ref for ref in config.models.invalid_refs if ref not in set(refs)}
            runtime.config.models = config.models
            runtime.gateway.config.models = config.models
        await runtime._emit("models.updated", {"provider_id": provider_id})
        return {"ok": True, "message": f"已移除 {len(body.refs)} 个模型"}

    @router.delete("/models/connections/{provider_id}")
    async def delete_connection(provider_id: str) -> dict:
        provider = config.models.providers.get(provider_id)
        if provider is None or provider.type not in config_module.API_TYPES:
            raise HTTPException(404, "连接不存在")
        tier_refs, agent_refs = model_refs_in_use(provider_id)
        conflicts = []
        if tier_refs:
            conflicts.append(f"团队默认档位仍在使用：{', '.join(tier_refs)}，请先在「团队默认模型」中调整")
        if agent_refs:
            conflicts.append(f"以下成员固定使用该连接的模型：{', '.join(agent_refs)}，请先修改成员设置")
        if conflicts:
            raise HTTPException(409, "；".join(conflicts))
        async with model_save_gate:
            config.models = config_module.delete_model_connection(provider_id)
            config.models.invalid_refs = {ref for ref in config.models.invalid_refs
                                          if not ref.startswith(provider_id + "/")}
            runtime.config.models = config.models
            runtime.gateway.config.models = config.models
        await runtime._emit("models.updated", {"provider_id": provider_id})
        return {"ok": True, "message": "连接与其模型已删除，对应密钥已从后端清除"}

    @router.get("/models/probe")
    async def probe_models() -> dict:
        return await runtime.gateway.probe()

    @router.post("/config/reload")
    async def reload_config() -> dict:
        """改完 yaml 不用重启进程。

        浏览器这块特意做成了热更新：重启会关掉所有浏览器、
        丢掉登录态，而登录态恰恰是最难重建的东西。
        """
        from .. import config as config_module

        fresh = config_module.load(reload=True)
        config.agents = fresh.agents
        config.models = fresh.models
        config.sandbox = fresh.sandbox
        config.browser = fresh.browser
        runtime.config = config
        runtime.browsers.reconfigure(fresh.browser.as_manager_settings())
        return {
            "ok": True,
            "agents": list(config.agents.agents),
            "browser_enabled": fresh.browser.enabled,
            "browser_patterns": len(fresh.browser.dangerous_patterns),
        }

    return router
