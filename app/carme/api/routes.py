"""HTTP API —— 界面和手机端都通过它跟 Runtime 说话。"""

from __future__ import annotations

import base64
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
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, Response
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr
import httpx
import yaml

from .. import __version__ as carme_version
from ..config import Config
from ..desktop import DesktopControlDisabled, DesktopError, DesktopInputError, DesktopUnavailable
from ..runtime import BudgetExceeded, Runtime
from ..store import Store
from ..tools.base import TOOL_GROUPS
from .. import config as config_module
from .. import fonts as fonts_module
from ..llm import LLMError, provider_session_headers
from ..engines import CLI_ENGINE_DEFS, ENGINE_MODEL_PATTERN, PI_PACKAGE, PI_VERSION, detect_cli_engines

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
    project_snapshot: dict | None = None
    envelope: dict | None = None


class ProbeRequest(BaseModel):
    mode: str = Field("", description="local / docker / remote；留空用配置默认")


class DesktopControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DesktopKeyboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: str = Field(..., min_length=1, max_length=24)


class DesktopMouse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["move", "move_rel", "click", "scroll", "press", "release"]
    x: float | None = None
    y: float | None = None
    dx: float | None = None
    dy: float | None = None
    button: Literal["left", "right", "middle"] = "left"
    clicks: int = Field(1, ge=1, le=3)
    delta_y: float | None = None


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
    project_snapshot: dict | None = None
    envelope: dict | None = None
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
    runtime_profile: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{0,64}$")
    execution_target: str | None = Field(None, pattern=r"^(none|container|ssh|macos)$")
    execution_target_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{0,64}$")
    avatar: dict | None = None
    tools: list[str] | None = Field(None, max_length=64)
    entry: bool | None = None
    can_delegate: bool | None = None
    sandbox: str | None = Field(None, pattern=r"^(none|remote|local|docker)$")
class BundleImport(BaseModel):
    """迁移导入：bundle 是导出的 JSON 原文，其余是本机侧的合并策略。"""
    model_config = ConfigDict(extra="forbid")
    bundle: dict
    profile_mode: Literal["merge", "overwrite", "skip"] = "merge"
    import_memory: bool = True
    import_conversations: bool = True


class NewBotDefaultsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=80)
    title: str | None = Field(None, max_length=160)
    prompt: str | None = Field(None, max_length=20000)
    engine: str | None = Field(None, pattern=r"^(api|codex|pi|claude)$")
    engine_model: str | None = Field(None, max_length=200)
    engine_effort: str | None = Field(None, max_length=16)
    model: str | None = Field(None, max_length=200)
    tier: str | None = Field(None, max_length=80)
    effort: str | None = Field(None, max_length=16)
    sandbox: str | None = Field(None, pattern=r"^(none|remote|local|docker)$")
    can_delegate: bool | None = None
    tools: list[str] | None = Field(None, max_length=64)
    execution_target: str | None = Field(None, pattern=r"^(none|container|ssh|macos)$")
    execution_target_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{0,64}$")


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


class VoiceSettings(BaseModel):
    """语音输入设置：浏览器识别，或指定连接 / 直接填写语音识别接口走服务端转写。"""

    model_config = ConfigDict(extra="forbid")
    mode: str = Field("", max_length=16)
    source: str = Field("connection", max_length=16)  # connection（已有模型连接）| custom（直接填写接口）
    provider: str = Field("", max_length=64)
    model: str = Field("", max_length=1000)  # 型号上限由 save_voice 校验为 200，这里只挡超大请求体
    base_url: str = Field("", max_length=300)
    api_key: str = Field("", max_length=500)  # 只写 voice.yaml（0600），留空表示不修改，绝不回显


class EffortProbe(ModelConnection):
    probe_id: str
    model_id: str = Field(..., min_length=1, max_length=200)


class EngineTest(BaseModel):
    runtime_profile: str = Field("", max_length=96)
    model_config = ConfigDict(extra="forbid")
    model: str = Field("", max_length=200)
    effort: str = Field("", max_length=16)


class PiCredential(BaseModel):
    """Pi 专用身份：账号自己的 OpenAI 兼容凭据。密钥只落账号凭据文件，不回显。"""

    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr
    base_url: str = Field(..., min_length=8, max_length=300)
    model: str = Field(..., min_length=3, max_length=200)


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


def _csrf_value(token: str) -> str:
    return hmac.new(token.encode(), b"carme-csrf-v2", hashlib.sha256).hexdigest()


def _request_origin(request: Request) -> str:
    # Reverse proxy HTTPS must be configured by the administrator, never inferred from an arbitrary header.
    return os.getenv("CARME_PUBLIC_ORIGIN", "").rstrip("/") or str(request.base_url).rstrip("/")


def _local_development(request: Request) -> bool:
    return (os.getenv("CARME_DEV_NO_AUTH") == "1" and request.client is not None
            and request.client.host in {"127.0.0.1", "::1", "localhost"}
            and request.url.hostname in {"127.0.0.1", "::1", "localhost"}
            and not any(name in request.headers for name in
                        ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "cf-connecting-ip")))


async def require_token(request: Request) -> None:
    expected = os.getenv("CARME_TOKEN", "").strip()
    origin = request.headers.get("origin")
    if origin and origin != _request_origin(request):
        raise HTTPException(403, "origin_denied")
    if request.query_params.get("token"):
        raise HTTPException(401, "query_token_disabled")
    if not expected:
        if _local_development(request):
            return
        raise HTTPException(503, "authentication_not_initialized")
    provided = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if provided:
        if hmac.compare_digest(provided, expected):
            request.state.auth_kind = "admin"
            return
        raise HTTPException(401, "token 不正确")
    cookie = request.cookies.get("carme_session", "")
    store = getattr(request.app.state, "store", None)
    session = store.session(cookie, expected) if store and cookie else None
    if not session:
        raise HTTPException(401, "session_expired_or_revoked")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not hmac.compare_digest(request.headers.get("x-carme-csrf", ""), _csrf_value(cookie)):
            raise HTTPException(403, "csrf_required")
    request.state.auth_kind = "session"
    request.state.session = session


# --------------------------------------------------------------------------- #
#  语音转写
# --------------------------------------------------------------------------- #

VOICE_MAX_BYTES = 25 * 1024 * 1024
AUDIO_CONTENT_TYPES = {".webm": "audio/webm", ".mp4": "audio/mp4", ".m4a": "audio/mp4",
                       ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}


def audio_upload_name(value: str) -> str:
    """multipart 文件名只保留 basename 与可见字符，不把路径或控制字符带给上游。"""
    name = os.path.basename(str(value or "").replace("\\", "/")).strip()
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)
    return name if name and len(name) <= 120 else "voice.webm"


def audio_content_type(name: str) -> str:
    return AUDIO_CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def redact_secret(message: object, secret: str) -> str:
    """错误详情脱敏 API key 并截断，避免密钥进响应或日志。"""
    text = str(message)
    if secret:
        text = text.replace(secret, "[redacted]")
    return text[:600]


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
            validation=None
            if os.getenv('CARME_CONTAINER_CONTROL')=='1':
                validation=await runtime.validate_bytes(name,bytes(raw),[{'kind':'format'}])
            file = await asyncio.to_thread(save_file, store, conversation_id, name, bytes(raw),validation=validation)
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
        if file['sha256'] and hashlib.sha256(path.read_bytes()).hexdigest()!=file['sha256']:
            raise HTTPException(409, 'artifact_version_conflict')
        return FileResponse(path, filename=file["name"], media_type="application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @router.delete("/conversations/{conversation_id}/attachments/{file_id}")
    async def remove_draft_attachment(conversation_id: str, file_id: str) -> dict:
        from ..attachments import file_path
        # 与消息提交共用数据库锁，已发送文件不能被草稿清理误删。
        with store._lock:
            file = require_file(conversation_id, file_id)
            if file["message_id"] or file["kind"] != "upload" or store._query_one('SELECT task_id FROM task_artifacts WHERE artifact_id=?',(file_id,)):
                raise HTTPException(409, "只能移除尚未发送且未纳入任务合同的附件")
            path=file_path(store,file_id)
            store._write("DELETE FROM attachments WHERE id=?", (file_id,))
            if not file['sha256'] or not store._query_one('SELECT id FROM attachments WHERE sha256=?',(file['sha256'],)):
                path.unlink(missing_ok=True)
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
            "execution": runtime.execution.health(),
        }

    @router.post("/session")
    async def create_session(request: Request):
        expected = os.getenv("CARME_TOKEN", "").strip()
        if not expected and _local_development(request):
            return {"ok": True, "csrf": "", "development": True}
        if getattr(request.state, "auth_kind", "") != "admin":
            raise HTTPException(401, "pairing_token_required")
        cookie, session = store.create_session(expected)
        await runtime._emit("session.created", {"session_id": session["id"], "expires_at": session["expires_at"]})
        response = JSONResponse({"ok": True, "csrf": _csrf_value(cookie), **session})
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie("carme_session", cookie, max_age=604800, httponly=True,
                            secure=_request_origin(request).startswith("https://"), samesite="strict", path="/")
        return response

    @router.post('/maintenance')
    async def maintenance(body: DesktopControl):
        from ..projects import save
        path=store.path.parent/'admission-paused.json'
        if body.enabled:save(path,{'paused':True,'at':time.time()})
        else:path.unlink(missing_ok=True)
        await runtime._emit('maintenance.changed',{'paused':body.enabled})
        return {'paused':body.enabled,'running_tasks':runtime.running,
                'active':store._query("SELECT id,status FROM tasks WHERE status IN ('queued','running','waiting_approval')"),
                'migration_ready':not store._query("SELECT id FROM tasks WHERE status IN ('queued','running','waiting_approval')")}

    @router.get("/session")
    async def current_session(request: Request):
        cookie = request.cookies.get("carme_session", "")
        return JSONResponse({"ok": True, "csrf": _csrf_value(cookie) if cookie else "",
                             "session": getattr(request.state, "session", None)},
                            headers={"Cache-Control": "no-store"})

    @router.get("/sessions")
    async def sessions():
        return {"sessions": store.list_sessions(os.getenv("CARME_TOKEN", "").strip())}

    @router.delete("/sessions/{session_id}")
    async def revoke_session(session_id: str):
        store.revoke_session(session_id)
        await runtime._emit("session.revoked", {"session_id": session_id})
        return {"ok": True}

    @router.delete("/session")
    async def logout(request: Request):
        session = getattr(request.state, "session", None)
        if session:
            store.revoke_session(session["id"])
            await runtime._emit("session.revoked", {"session_id": session["id"]})
        response = JSONResponse({"ok": True})
        response.delete_cookie("carme_session", path="/")
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
    NEW_BOT_DEFAULTS = {
        "name": "新 Bot",
        "title": "待补充描述",
        "prompt": "你是 Carme 团队中的一名成员。根据用户的请求完成对应的工作；\n需要协作时通过 delegate 把任务拆给合适的成员，并汇总它们的成果。\n描述可以后续在 Bot 设置中补充。",
        "engine": "api",
        "engine_model": "",
        "engine_effort": "",
        "model": "",
        "tier": "balanced",
        "effort": "",
        "sandbox": "none",
        "can_delegate": True,
        "tools": ["delegate", "memory", "browser", "computer"],
        "execution_target": "container",
        "execution_target_id": "action",
    }


    def _new_bot_defaults() -> dict:
        stored = config.agents.defaults.get("new_bot") or {}
        merged = dict(NEW_BOT_DEFAULTS)
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in merged and value is not None:
                    merged[key] = value
        merged["tools"] = [str(item) for item in (merged.get("tools") or [])][:64]
        return merged

    def _validate_new_bot_defaults(merged: dict) -> None:
        engine = str(merged.get("engine") or "api")
        if engine not in config_module.ENGINE_IDS:
            raise HTTPException(422, "不支持的引擎")
        engine_model = str(merged.get("engine_model") or "").strip()
        if engine_model and (any(ord(char) < 33 or ord(char) > 126 for char in engine_model)
                             or engine_model.startswith("-") or len(engine_model) > 200):
            raise HTTPException(422, "CLI 模型名只能使用可见 ASCII 字符，且不能以短横线开头")
        valid_tools = set(TOOL_GROUPS) | set(runtime.registry.names())
        unknown_tools = set(merged.get("tools") or []) - valid_tools
        if unknown_tools:
            raise HTTPException(422, f"未知工具：{', '.join(sorted(unknown_tools))}")
        if str(merged.get("execution_target", "none") or "none") not in {"none", "container", "ssh", "macos"}:
            raise HTTPException(422, "不支持的执行目标")
        execution_target_id = str(merged.get("execution_target_id", "") or "").strip()
        if execution_target_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", execution_target_id):
            raise HTTPException(422, "执行目标 ID 无效")
        if engine == "api":
            if merged.get("model"):
                try:
                    config.models.resolve(merged["model"])
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
            elif merged.get("tier", "balanced") not in config.models.tiers:
                raise HTTPException(422, "模型档位未配置；请先在「模型」中配置连接，或改用本机 CLI 引擎")

    @router.get("/agents/defaults")
    async def get_new_bot_defaults() -> dict:
        return {"defaults": _new_bot_defaults()}

    @router.put("/agents/defaults")
    async def update_new_bot_defaults(body: NewBotDefaultsUpdate) -> dict:
        changes = body.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise HTTPException(422, "默认值字段不能为 null")
        merged = {**_new_bot_defaults(), **changes}
        _validate_new_bot_defaults(merged)
        path = config_module.CONFIG_DIR / "agents.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}
        defaults_section = raw.setdefault("defaults", {})
        stored = defaults_section.setdefault("new_bot", {})
        stored.clear()
        stored.update({key: value for key, value in merged.items() if key in NEW_BOT_DEFAULTS})
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
        return {"defaults": _new_bot_defaults()}

    @router.post("/agents/quick", status_code=201)
    async def quick_create_agent() -> dict:
        """零输入创建 Bot：全部使用设置中的默认值，描述可后续编辑。"""
        defaults = _new_bot_defaults()
        _validate_new_bot_defaults(defaults)
        agent_id = ""
        for _ in range(8):
            candidate = "bot_" + secrets.token_hex(4)
            if candidate not in config.agents.agents:
                agent_id = candidate
                break
        if not agent_id:
            raise HTTPException(500, "无法生成唯一的 Bot id，请重试")
        body = AgentUpdate(
            id=agent_id,
            name=str(defaults.get("name") or agent_id)[:80],
            title=str(defaults.get("title") or "")[:160],
            prompt=str(defaults.get("prompt") or ""),
            engine=str(defaults.get("engine") or "api"),
            engine_model=str(defaults.get("engine_model") or ""),
            engine_effort=str(defaults.get("engine_effort") or ""),
            model=str(defaults.get("model") or ""),
            tier=str(defaults.get("tier") or "balanced"),
            effort=str(defaults.get("effort") or ""),
            sandbox=str(defaults.get("sandbox") or "local"),
            can_delegate=bool(defaults.get("can_delegate")),
            tools=[str(item) for item in (defaults.get("tools") or [])],
        )
        agent = save_agent(body, agent_id, creating=True)
        await runtime._emit("agent.updated", {"agent_id": agent["id"]}, "", agent["id"])
        return {"agent": agent}


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
                    "runtime_profile": spec.runtime_profile,
                    "execution_target": spec.execution_target,
                    "execution_target_id": spec.execution_target_id,
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
        try:
            _, boundary = runtime._task_agent_snapshot(spec.id)
            boundary.pop("deadline", None)
            if spec.engine != "api":
                boundary["runtime"] = next(item for item in detect_cli_engines(
                    {spec.runtime_profile: config.isolation.get("profiles", {}).get(spec.runtime_profile, {})}, runtime.execution)
                    if item["id"] == spec.engine)
        except ValueError as exc:
            boundary = {"error": str(exc)}
        return {**asdict(spec), "memory": store.recall(spec.id), "boundary": boundary}

    @router.get("/engines")
    async def list_engines() -> dict:
        """探测本机固定 CLI；发现二进制不等于已经登录或具体模型可用。"""
        api_ready = any(provider.available and provider.type != "mock"
                        for provider in config.models.providers.values())
        built_in = [{"id": "api", "label": "Carme API 网关", "binary": "", "installed": True,
                     "ready": api_ready, "status": "ready" if api_ready else "not_configured",
                     "version": "内置", "auth_status": "configured" if api_ready else "unknown",
                     "capability": "Carme 工具与现有模型配置"}]
        return {"engines": built_in + detect_cli_engines(config.isolation.get("profiles", {}), runtime.execution),
                "execution": runtime.execution.health()}

    @router.post("/engines/{engine_id}/test")
    async def test_engine(engine_id: str, body: EngineTest) -> dict:
        if engine_id not in CLI_ENGINE_DEFS:
            raise HTTPException(422, "请选择已发现的 CLI 引擎")
        probe_id = ""
        runner = None
        borrowed = None
        note = ""
        try:
            if engine_id == "pi":
                # 身份解析：显式 profile → 已登记的第一个 profile → 从「添加模型连接」自动派生。
                profiles_map = dict(config.isolation.get("profiles", {}))
                profile_id = body.runtime_profile if body.runtime_profile in profiles_map else (
                    next(iter(profiles_map)) if profiles_map else "")
                if not profile_id:
                    profile_id, note = await _derive_pi_profile(body.model.strip())
                else:
                    profile = profiles_map[profile_id]
                    ref = str(profile.get("credential_ref", ""))
                    entry = (config.isolation.get("credentials", {}) or {}).get(ref)
                    known_keys = {item.api_key for item in config.models.providers.values()
                                  if item.type in {"openai", "openai_compatible"} and item.available}
                    key_current = ""
                    try:
                        key_current = _pi_credential_path().read_text().strip()
                    except OSError:
                        pass
                    if (not entry or not key_current
                            or (known_keys and key_current not in known_keys)
                            or (body.model.strip() and body.model.strip() != profile.get("model")
                                and body.model.strip() in config.models.models)):
                        # profile 还在但身份缺失、密钥是占位/旧值、或用户在「测试连接」旁显式
                        # 选择了别的已配置模型：按选择重新派生（绑定模型到该 harness）。
                        _, note = await _derive_pi_profile(body.model.strip() or str(profile.get("model", "")))
                spec = next((agent for agent in config.agents.agents.values()
                             if agent.runtime_profile == profile_id and agent.execution_target == "container"), None)
                if spec is None:
                    # 「测试连接」只探测：临时把入口 Bot 接到该 profile（仅内存，不改配置文件）。
                    entry_agent = config.agents.entry_agent
                    if entry_agent.execution_target != "container":
                        raise LLMError("runtime_profile_unassigned: 请先把一个 Bot 的执行目标设为「容器」，再测试 Pi")
                    borrowed = (entry_agent, entry_agent.engine, entry_agent.runtime_profile)
                    entry_agent.engine, entry_agent.runtime_profile = "pi", profile_id
                    spec = entry_agent
                _, snapshot = runtime._task_agent_snapshot(spec.id)
                snapshot["policy"]["tools"] = []  # Connection probes cannot invoke any tool.
                probe_id = store.create_task(spec.id, "Pi 专用身份连接测试", source="probe", meta=snapshot,
                    max_daily_tasks=config.sandbox.limit("max_daily_tasks", 200))
                store.set_task_status(probe_id, "running")
                async def runner(prompt, profile, **kwargs):
                    return await runtime.execution.pi(probe_id, prompt, profile, timeout=60, **kwargs)
                response = await runtime.gateway.chat(
                    [{"role": "user", "content": "这是 Carme 的连接测试。不要读取文件、调用工具或修改任何内容，只回复 OK。"}],
                    engine=engine_id, engine_model="", effort=body.effort.strip(), tools=None,
                    runtime_profile=profile_id,
                    retries_per_model=1,
                    **({"cli_runner": runner} if runner else {}),
                )
            else:
                response = await runtime.gateway.chat(
                    [{"role": "user", "content": "这是 Carme 的连接测试。不要读取文件、调用工具或修改任何内容，只回复 OK。"}],
                    engine=engine_id, engine_model=body.model.strip(), effort=body.effort.strip(), tools=None,
                    runtime_profile=body.runtime_profile,
                    retries_per_model=1,
                )
        except (LLMError, RuntimeError) as exc:
            if probe_id:
                store.finish_task(probe_id, "", status="failed", error=str(exc))
            return {"ok": False, "error": str(exc)}
        except BaseException:
            if probe_id:
                store.finish_task(probe_id, "", status="failed", error="probe_interrupted")
            raise
        finally:
            if borrowed:
                borrowed[0].engine, borrowed[0].runtime_profile = borrowed[1], borrowed[2]
        if probe_id:
            store.add_task_usage(probe_id, 0, 0, cost_known=False, tokens_known=False)
            store.finish_task(probe_id, response.text, status="done")
        return {"ok": True, "engine": engine_id, "model": response.model,
                "reply": response.text[:200], "message": ("专用引擎文本调用成功；工具与成果仍需各自验收。" + note).strip()}
    async def _derive_pi_profile(model_hint: str) -> tuple[str, str]:
        """从「添加模型连接」保存的 OpenAI 兼容连接派生 Pi 专用身份（幂等；密钥只落账号凭据文件）。"""
        candidates = [provider for provider in config.models.providers.values()
                      if provider.type in {"openai", "openai_compatible"} and provider.available]
        if not candidates:
            raise LLMError("runtime_profile_required: 请先用「添加模型连接」保存一个 OpenAI 兼容连接（含 API key）")
        hint = (model_hint or "").strip()
        provider = None
        if hint and "/" in hint:
            provider = next((item for item in candidates if item.name == hint.split("/", 1)[0]), None)
            if provider is None and len(candidates) > 1:
                raise LLMError(f"模型 {hint} 不属于已保存的模型连接；请检查连接名，或先保存该连接")
        if provider is None:
            if len(candidates) > 1:
                raise LLMError("存在多个模型连接；请在测试时用「连接名/模型名」指明要用的模型")
            provider = candidates[0]
        model = hint if hint.startswith(provider.name + "/") else ""
        if not model:
            verified = sorted(ref for ref, spec in config.models.models.items()
                              if ref.startswith(provider.name + "/") and spec.verified)
            if not verified:
                raise LLMError("该模型连接还没有已验证的模型：请先在「添加模型连接」里测试并保存模型")
            model = verified[0]
        url = urlsplit(provider.base_url)
        if url.scheme != "https" or url.port not in (None, 443) or url.username or url.password:
            raise LLMError("Pi 需要 443 端口的 https OpenAI 兼容接口；请调整该连接的网址")
        digest = str((runtime.execution.broker_health or {}).get("images", {}).get("pi", ""))
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise HTTPException(409, "Pi 镜像尚未就绪：请先在后台启动本账号")
        if not provider.api_key or any(char.isspace() for char in provider.api_key):
            raise LLMError("该连接的 API key 无效；请在「添加模型连接」里重新输入")
        path = _pi_credential_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o777)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(provider.api_key + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            raise HTTPException(500, "无法写入账号凭据目录；请确认账号已按新版本启动") from None

        def mutate(data: dict) -> None:
            data.setdefault("credentials", {})[PI_CREDENTIAL_REF] = {
                "protocol": "openai-completions", "base_url": provider.base_url, "models": [model],
                "key_file": "/run/credentials/" + PI_CREDENTIAL_REF}
            data.setdefault("profiles", {})[PI_PROFILE_ID] = {
                "engine": "pi", "package_name": PI_PACKAGE, "package_version": PI_VERSION,
                "image_digest": digest, "execution_mode": "managed_bridge",
                "inherit_user_config": False, "native_tools": [], "session_authority": "carme",
                "resume_personal_sessions": False, "credential_kind": "api_key",
                "credential_ref": PI_CREDENTIAL_REF, "model": model}

        _write_isolation(mutate)
        _reload_config_state()
        return PI_PROFILE_ID, f"（已从模型连接「{provider.name}」派生 Pi 身份：{model}）"
    PI_CREDENTIAL_REF = "pi-carme-api"
    PI_PROFILE_ID = "pi-carme-v1"

    def _reload_config_state():
        """改完 yaml 不重启进程地重载（浏览器会话与登录态因此得以保留）。"""
        fresh = config_module.load(reload=True)
        config.agents = fresh.agents
        config.models = fresh.models
        config.sandbox = fresh.sandbox
        config.browser = fresh.browser
        config.isolation = fresh.isolation
        runtime.config = config
        runtime.browsers.reconfigure(fresh.browser.as_manager_settings())
        return fresh

    def _pi_credential_path() -> Path:
        root = Path(os.getenv("CARME_CREDENTIALS_DIR", "/run/credentials"))
        if not root.is_absolute():
            raise HTTPException(422, "账号凭据目录不可用")
        return root / PI_CREDENTIAL_REF

    def _write_isolation(mutate) -> None:
        """只改账号自己的 config/isolation.yaml；失败即报错，不静默降级。"""
        path = config_module.CONFIG_DIR / "isolation.yaml"
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            raise HTTPException(500, "账号隔离配置不可读") from None
        mutate(data)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False))
            os.chmod(tmp, 0o666)
            os.replace(tmp, path)
        except OSError:
            raise HTTPException(500, "账号隔离配置不可写；此功能只在 Docker 账号内可用") from None

    @router.post("/engines/pi/credential")
    async def save_pi_credential(body: PiCredential) -> dict:
        """为 Pi 登记专用身份：密钥只写进本账号的凭据文件，绝不读取宿主机个人配置。"""
        key = body.api_key.get_secret_value().strip()
        if not key or len(key) > 16384 or any(char.isspace() for char in key) or key.startswith(("{", "[")):
            raise HTTPException(422, "API key 不能为空，也不能含空白字符")
        base_url = body.base_url.strip()
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.port not in (None, 443)):
            raise HTTPException(422, "接口地址必须是 443 端口的 https OpenAI 兼容地址，例如 https://api.openai.com/v1")
        model = body.model.strip()
        if not ENGINE_MODEL_PATTERN.fullmatch(model) or not all(model.split("/", 1)) or "/" not in model:
            raise HTTPException(422, "模型要写成 供应商/模型名，例如 openai/gpt-4o-mini")
        digest = str((runtime.execution.broker_health or {}).get("images", {}).get("pi", ""))
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise HTTPException(409, "Pi 镜像尚未就绪：请先在后台启动本账号")
        path = _pi_credential_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o777)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(key + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            raise HTTPException(500, "无法写入账号凭据目录；请确认账号已按新版本启动") from None

        def mutate(data: dict) -> None:
            data.setdefault("credentials", {})[PI_CREDENTIAL_REF] = {
                "protocol": "openai-completions", "base_url": base_url, "models": [model],
                "key_file": "/run/credentials/" + PI_CREDENTIAL_REF}
            data.setdefault("profiles", {})[PI_PROFILE_ID] = {
                "engine": "pi", "package_name": PI_PACKAGE, "package_version": PI_VERSION,
                "image_digest": digest, "execution_mode": "managed_bridge",
                "inherit_user_config": False, "native_tools": [], "session_authority": "carme",
                "resume_personal_sessions": False, "credential_kind": "api_key",
                "credential_ref": PI_CREDENTIAL_REF, "model": model}

        _write_isolation(mutate)
        _reload_config_state()
        engine = next((item for item in detect_cli_engines(config.isolation.get("profiles", {}), runtime.execution)
                       if item["id"] == "pi"), {})
        return {"ok": True, "credential_ref": PI_CREDENTIAL_REF, "profile": PI_PROFILE_ID, "engine": engine}

    @router.delete("/engines/pi/credential")
    async def remove_pi_credential() -> dict:
        removed = []

        def mutate(data: dict) -> None:
            for section, name in (("credentials", PI_CREDENTIAL_REF), ("profiles", PI_PROFILE_ID)):
                if isinstance(data.get(section), dict) and data[section].pop(name, None) is not None:
                    removed.append(section)

        _write_isolation(mutate)
        try:
            _pi_credential_path().unlink()
        except OSError:
            pass
        _reload_config_state()
        return {"ok": True, "removed": removed}

    @router.get("/fonts")
    async def list_fonts() -> dict:
        """运行 Carme 的这台机器的可用字体；Web UI 的字体选择器直接读它。"""
        return await asyncio.to_thread(fonts_module.system_fonts)

    @router.post("/fonts/reload")
    async def reload_fonts() -> dict:
        """重新扫描字体目录：新装字体后不必重启后端，也不受一小时缓存影响。"""
        fonts_module.clear_cache()
        return await asyncio.to_thread(lambda: fonts_module.system_fonts(use_cache=False))
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
        if engine == "pi":
            profiles = config.isolation.get("profiles", {}) or {}
            registered = [key for key, value in profiles.items() if isinstance(value, dict) and value.get("engine") == "pi"]
            wanted = str(merged.get("runtime_profile", "") or "").strip()
            if not wanted and len(registered) == 1:
                wanted = registered[0]  # 唯一已登记档案自动绑定，Bot 无需逐个手选
            if wanted and wanted not in registered:
                raise HTTPException(422, "该 Bot 绑定的 Pi 运行档案不存在；请先在「设置 → 模型」测试 Pi 自动登记，再在 Bot 设置里重新选择")
            if not wanted and not registered:
                raise HTTPException(422, "Pi 运行档案尚未登记：请先在「设置 → 模型」页测试 Pi 连接（测试时自动登记），然后再保存使用 Pi 的 Bot")
            if not wanted:
                raise HTTPException(422, "存在多个 Pi 运行档案：请在 Bot 设置的「Carme Runtime Profile」下拉中选择一个")
            merged["runtime_profile"] = wanted
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

    # ---------------- 迁移：导出 / 导入 ----------------

    EXPORT_FORMAT = 1
    MAX_EXPORT_CONVERSATIONS = 500
    MAX_EXPORT_MESSAGES_PER_CONVERSATION = 5000
    MAX_IMPORT_AGENTS = 64
    MAX_IMPORT_CONVERSATIONS = 200
    MAX_IMPORT_MESSAGES_PER_CONVERSATION = 2000
    IMPORTABLE_ROLES = ("user", "assistant", "system")
    AVATAR_SHAPES = ("circle", "oval", "square", "pill", "triangle", "hexagon", "cloud", "drop")

    def avatar_data_uri(avatar: dict) -> str:
        """把本机头像图片读成 data URI，便于整包迁移；形状头像无需内嵌。"""
        if (avatar or {}).get("kind") != "image":
            return ""
        filename = str(avatar.get("file") or "")
        if not re.fullmatch(r"[a-f0-9]{32}\.webp", filename):
            return ""
        path = avatar_dir / filename
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                return ""
            return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return ""

    def store_avatar_data_uri(uri: str) -> str:
        """校验并写回导入的头像图片，返回本机文件名；无效则返回空字符串。"""
        from PIL import Image, ImageOps
        try:
            _, encoded = uri.split(",", 1)
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return ""
        if not raw or len(raw) > 5 * 1024 * 1024:
            return ""
        try:
            with Image.open(io.BytesIO(raw)) as original:
                if original.format != "WEBP" or getattr(original, "n_frames", 1) != 1:
                    return ""
                if min(original.size) < 32 or max(original.size) > 4096:
                    return ""
                original.load()
                picture = ImageOps.fit(original.convert("RGBA"), (512, 512))
                output = io.BytesIO()
                picture.save(output, "WEBP", quality=90)
        except Exception:
            return ""
        filename = secrets.token_hex(16) + ".webp"
        config_module.atomic_write(avatar_dir / filename, output.getvalue())
        return filename

    def build_export_bundle(agent_ids: list[str]) -> dict:
        """导出角色、记忆与对话上下文。任务、审批、事件与执行电脑凭据都不导出。"""
        known = [value for value in dict.fromkeys(agent_ids) if value in config.agents.agents]
        entries = []
        for agent_id in known:
            profile = asdict(config.agents.get(agent_id))
            entries.append({"id": agent_id, "profile": profile, "memory": store.recall(agent_id),
                            "avatar_image": avatar_data_uri(profile.get("avatar") or {})})
        conversations = []
        for row in store.conversations_for_agents(known)[:MAX_EXPORT_CONVERSATIONS]:
            messages = [m for m in store.list_conversation_messages(row["id"])
                        if m.get("role") in IMPORTABLE_ROLES][:MAX_EXPORT_MESSAGES_PER_CONVERSATION]
            summary = store.get_summary(row["id"])
            conversations.append({
                "source_id": row["id"],
                "title": row.get("title") or "",
                "kind": row.get("kind") or ("group" if len(row["agent_ids"]) > 1 else "direct"),
                "agent_ids": row["agent_ids"],
                "created_at": row.get("created_at") or 0,
                "updated_at": row.get("updated_at") or 0,
                "summary": ({"content": summary.get("content") or "",
                             "through_seq": summary.get("through_seq") or 0,
                             "model": summary.get("model") or "",
                             "updated_at": summary.get("updated_at") or 0} if summary else None),
                "messages": [{"role": m.get("role"), "agent_id": m.get("agent_id") or "",
                              "content": m.get("content") or "", "created_at": m.get("created_at") or 0,
                              "model": m.get("model") or "", "provider": m.get("provider") or ""}
                             for m in messages],
            })
        return {"carme_export": EXPORT_FORMAT, "kind": "agents", "app_version": carme_version,
                "exported_at": time.time(), "agents": entries, "conversations": conversations}

    def sanitise_import_profile(profile: dict, agent_id: str, notes: list[str]) -> dict:
        """把导入资料收敛为本机可用配置。

        迁移到另一台设备时，模型、档位、工具很可能不存在。这里就地降级并在
        报告里说明，而不是让整包导入因为一个字段失败。
        """
        def text(value, limit: int) -> str:
            return str(value if value is not None else "")[:limit]

        clean: dict = {
            "name": text(profile.get("name"), 80).strip() or agent_id,
            "title": text(profile.get("title"), 160),
            "emoji": text(profile.get("emoji"), 32),
            "prompt": text(profile.get("prompt"), 20000),
            "can_delegate": bool(profile.get("can_delegate")),
            "entry": bool(profile.get("entry")),
            "execution_target": "none", "execution_target_id": "", "runtime_profile": "",
        }
        if profile.get("execution_target", "none") != "none" or profile.get("runtime_profile"):
            notes.append("执行目标和 runtime profile 需在本机重新授权，已设为 none")
        sandbox = str(profile.get("sandbox") or "")
        clean["sandbox"] = sandbox if sandbox in {"none", "remote", "local", "docker"} else "local"
        if sandbox and clean["sandbox"] != sandbox:
            notes.append(f"执行环境「{sandbox}」本机不支持，已改为 local")
        engine = str(profile.get("engine") or "api")
        clean["engine"] = engine if engine in config_module.ENGINE_IDS else "api"
        if clean["engine"] != engine:
            notes.append(f"引擎「{engine}」本机不支持，已改为 API 网关")
        clean["engine_model"] = text(profile.get("engine_model"), 200).strip()
        clean["engine_effort"] = text(profile.get("engine_effort"), 16).strip()
        clean["engine_workspace"] = text(profile.get("engine_workspace"), 500).strip()
        valid_tools = set(TOOL_GROUPS) | set(runtime.registry.names())
        wanted_tools = [str(item) for item in (profile.get("tools") or [])]
        clean["tools"] = [item for item in dict.fromkeys(wanted_tools) if item in valid_tools][:64]
        dropped = sorted(set(wanted_tools) - set(clean["tools"]))
        if dropped:
            notes.append("本机没有这些工具，已跳过：" + ", ".join(dropped))
        model = text(profile.get("model"), 200).strip()
        tier = text(profile.get("tier"), 80).strip() or "balanced"
        if clean["engine"] == "api" and model:
            try:
                config.models.resolve(model)
            except ValueError:
                notes.append(f"模型「{model}」本机未配置，已改为按档位自动选择")
                model = ""
        if model:
            clean["model"] = model
            clean["tier"] = tier
            effort = text(profile.get("effort"), 16).strip()
            try:
                options = config.models.price(model).effort_options
            except ValueError:
                options = []
            if effort and effort not in options:
                notes.append(f"该模型在本机未确认支持 effort「{effort}」，已改用模型默认值")
                effort = ""
            clean["effort"] = effort
        else:
            clean["tier"] = tier if tier in config.models.tiers else "balanced"
            if clean["tier"] != tier:
                notes.append(f"档位「{tier}」本机未配置，已改为 balanced")
        avatar = profile.get("avatar")
        if isinstance(avatar, dict) and avatar.get("kind") == "bot":
            shape, color = str(avatar.get("shape") or ""), str(avatar.get("color") or "")
            if shape in AVATAR_SHAPES and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                clean["avatar"] = {"kind": "bot", "shape": shape, "color": color}
        return clean

    @router.get("/agents/{agent_id}/export")
    async def export_agent(agent_id: str) -> dict:
        """导出单个 Bot：角色信息、它的记忆，以及它参与的全部对话上下文。"""
        if agent_id not in config.agents.agents:
            raise HTTPException(404, "Bot 不存在")
        return build_export_bundle([agent_id])

    @router.get("/export")
    async def export_all_agents() -> dict:
        """导出本机全部 Bot，用于整机迁移。"""
        return build_export_bundle(list(config.agents.agents))

    @router.post("/import")
    async def import_bundle(body: BundleImport) -> dict:
        """把导出包并入本机：Bot 永远生成新会话 id，不会覆盖本机已有对话。"""
        bundle = body.bundle if isinstance(body.bundle, dict) else {}
        raw_agents = bundle.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents:
            raise HTTPException(422, "这不是有效的 Carme 导出文件：缺少 agents 列表")
        entries = [item for item in raw_agents if isinstance(item, dict)][:MAX_IMPORT_AGENTS]
        report: dict = {"agents": [], "memory_count": 0, "conversations": [], "warnings": []}
        warned: set[str] = set()

        def warn(message: str) -> None:
            if message not in warned:
                warned.add(message)
                report["warnings"].append(message)

        for entry in entries:
            profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
            agent_id = str(entry.get("id") or profile.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_id):
                warn(f"跳过 id 无效的条目：{agent_id or '(空)'}")
                continue
            exists = agent_id in config.agents.agents
            item: dict = {"id": agent_id, "existed": exists, "memory": 0}
            if exists and body.profile_mode == "skip":
                item["profile"] = "skipped"
            else:
                notes: list[str] = []
                clean = sanitise_import_profile(profile, agent_id, notes)
                avatar_uri = str(entry.get("avatar_image") or "")
                if avatar_uri.startswith("data:image/"):
                    filename = await asyncio.to_thread(store_avatar_data_uri, avatar_uri)
                    if filename:
                        clean["avatar"] = {"kind": "image", "file": filename}
                    else:
                        notes.append("头像图片无法识别，已改用形状头像")
                try:
                    save_agent(AgentUpdate(id=agent_id, **clean), agent_id, creating=not exists)
                except HTTPException as exc:
                    item["profile"] = "failed"
                    item["notes"] = notes + [str(exc.detail)]
                    report["agents"].append(item)
                    continue
                item["profile"] = "updated" if exists else "created"
                item["notes"] = notes
                await runtime._emit("agent.updated", {"agent_id": agent_id}, "", agent_id)
            if agent_id in config.agents.agents and body.import_memory:
                item["memory"] = store.import_memory(agent_id, entry.get("memory") or [])
                report["memory_count"] += item["memory"]
            report["agents"].append(item)

        if body.import_conversations:
            raw_conversations = bundle.get("conversations")
            if isinstance(raw_conversations, list):
                for conversation in [c for c in raw_conversations if isinstance(c, dict)][:MAX_IMPORT_CONVERSATIONS]:
                    members = list(dict.fromkeys(
                        str(value) for value in (conversation.get("agent_ids") or [])
                        if str(value) in config.agents.agents))
                    if not members:
                        warn("有会话引用的 Bot 不在本机，已跳过这些会话（可先导入对应 Bot）")
                        continue
                    # 只统计真正会被写入的消息：报告必须反映实际结果，
                    # 而不是导出文件里的条数（tool 等非对话角色不会入库）。
                    messages = [m for m in (conversation.get("messages") or [])
                                if isinstance(m, dict) and str(m.get("role") or "") in IMPORTABLE_ROLES
                                and str(m.get("content") or "").strip()][:MAX_IMPORT_MESSAGES_PER_CONVERSATION]
                    created = store.import_conversation({**conversation, "agent_ids": members, "messages": messages})
                    if created:
                        report["conversations"].append({
                            "id": created["id"], "title": created["title"],
                            "agent_ids": members, "messages": len(messages),
                            "source_id": str(conversation.get("source_id") or ""),
                        })
        return {"ok": True, "report": report}

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
                                                 **({'envelope':body.envelope} if body.envelope is not None else {}),
                                                 **({"attachment_ids": body.attachment_ids} if body.attachment_ids else {}),
                                                 **({'project_snapshot':body.project_snapshot} if body.project_snapshot is not None else {}))
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
                agent_id, body.goal, title=body.title, source="web",
                **({'envelope':body.envelope} if body.envelope is not None else {}),
                **({'project_snapshot': body.project_snapshot} if body.project_snapshot is not None else {})
            )
        except BudgetExceeded as exc:
            raise HTTPException(429, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
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
            "outcome":{**store.outcome(task_id),"report_hash":__import__("carme.security",fromlist=["digest"]).digest(store.outcome(task_id)["report"])},
            "runs":store._query('SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at',(task_id,)),
            "operations":store._query('SELECT * FROM task_operations WHERE task_id=? ORDER BY created_at',(task_id,)),
            "checkpoint":store._query_one('SELECT seq,sha256,created_at FROM task_checkpoints WHERE task_id=? ORDER BY seq DESC LIMIT 1',(task_id,)),
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
                    try:
                        await require_token(request)
                    except HTTPException:
                        break
                    batch = store.list_events(after_id=cursor, limit=500)
                    if batch:
                        for event in batch:
                            try:
                                await require_token(request)
                            except HTTPException:
                                return
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
        """检查本机浏览器，或检查明确选择的远端执行电脑。"""
        if config.isolation.get('browser'):
            return await runtime.browsers.probe()
        try:
            node = config.sandbox.resolve_node() if config.sandbox.default_node_id else None
        except ValueError as exc:
            return {"ok": False, "status": "unconfigured", "error": str(exc)}
        result = await runtime.browsers.probe(node=node)
        result["execution"] = "remote" if node else "local"
        result["notify"] = runtime.notifier.describe()
        result["require_confirmation"] = config.browser.require_confirmation
        result["approval_timeout_seconds"] = config.browser.approval_timeout
        result["dangerous_patterns"] = config.browser.dangerous_patterns
        result["pending_approvals"] = store.pending_approval_count()
        return result

    # ---------------- 本机桌面画面与人工鼠标控制 ----------------
    def mac_runner_desktop() -> bool:
        """Runner 已连接且本地授权有效（前端据此显示真实 Mac 屏幕）。"""
        return time.time() - runtime.execution.mac_seen < 5 and runtime.execution.mac_authorized

    @router.get("/desktop/status")
    async def desktop_status() -> dict:
        if mac_runner_desktop():
            execution = runtime.execution
            width, height = execution.mac_frame_size
            return {"enabled": True, "control_enabled": execution.mac_control_enabled,
                    "available": bool(execution.mac_frame), "screen_width": width, "screen_height": height,
                    "cursor_x": 0.0, "cursor_y": 0.0, "error": "", "mode": "runner"}
        status = await asyncio.to_thread(runtime.desktop.status)
        # 浏览器隔离账号没有本机桌面：前端据此把画面源切换到 /browser/screenshot。
        status["mode"] = "browser" if config.isolation.get('browser') else "local"
        return status

    async def runner_frame(since: str = "") -> Response:
        execution = runtime.execution
        execution.mac_screen_until = time.time() + 3  # 让 Runner 在 claim 时持续抓帧
        try:
            since_at = float(since) if since else 0.0
        except ValueError:
            since_at = since_at = 0.0
        frame = execution.mac_frame
        if frame and execution.mac_frame_at > since_at:
            stamp = execution.mac_frame_at
        else:
            waiter = asyncio.get_running_loop().create_future()
            execution.mac_screen_waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, timeout=2.0)
            except asyncio.TimeoutError:
                raise HTTPException(503, "Mac 画面尚未就绪；请确认 Runner 已连接且已授权。") from None
            frame = execution.mac_frame
            stamp = execution.mac_frame_at
        if not frame or stamp <= since_at:
            raise HTTPException(503, "Mac 画面尚未就绪；请确认 Runner 已连接且已授权。")
        return Response(
            content=frame,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Screenshot-Mtime": f"{stamp:.6f}",
            },
        )

    async def runner_control(kind: str, body: dict) -> dict:
        execution = runtime.execution
        if not execution.mac_control_enabled:
            raise HTTPException(409, "远程控制未开启：请先在画面面板勾选启用鼠标键盘。")
        execution.mac_control_seq += 1
        action_id = f"ctl-{execution.mac_control_seq}-{secrets.token_hex(4)}"
        waiter = asyncio.get_running_loop().create_future()
        execution.mac_control_waiters[action_id] = waiter
        execution.mac_control_queue.append({"id": action_id, "kind": kind, "body": body})
        try:
            return await asyncio.wait_for(waiter, timeout=3.0)
        except asyncio.TimeoutError:
            raise HTTPException(503, "Runner 没有回应该控制动作；请确认 Runner 仍在运行。") from None
    @router.get("/desktop/screenshot")
    async def desktop_screenshot(since: str = "") -> Response:
        if mac_runner_desktop():
            return await runner_frame(since)
        if config.isolation.get('browser'):
            raise HTTPException(409, 'native_mac_requires_paired_mac_runner')
        try:
            image = await asyncio.to_thread(runtime.desktop.screenshot)
        except DesktopError as exc:
            raise HTTPException(503, str(exc)) from None
        return Response(
            content=image,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/browser/screenshot")
    async def browser_screenshot(since: str = "") -> Response:
        """浏览器隔离账号的降级画面：返回 Bot 最近一次浏览器操作的截图（截图流）。

        本机桌面不可用（isolation.browser 启用）时，前端改为轮询本端点。
        `since` 传上次响应的 X-Screenshot-Mtime：画面未更新时返回 304，省去重复传输。
        """
        if not config.isolation.get('browser'):
            raise HTTPException(404, 'browser_view_requires_browser_isolation')
        latest = store.latest_png_artifact()
        if latest is None:
            raise HTTPException(
                503,
                '还没有浏览器画面。Bot 在浏览器里执行任务并截图后，这里会显示最近一幕。',
            )
        try:
            if since and round(float(since), 6) >= round(latest['created_at'], 6):
                return Response(status_code=304, headers={"Cache-Control": "no-store"})
        except ValueError:
            pass
        from ..attachments import file_path
        path = file_path(store, latest['id'])
        try:
            image = path.read_bytes()
        except OSError:
            raise HTTPException(503, '浏览器截图文件丢失。') from None
        return Response(
            content=image,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Screenshot-Mtime": f"{latest['created_at']:.6f}",
            },
        )

    @router.post("/desktop/control")
    async def desktop_control(body: DesktopControl) -> dict:
        if mac_runner_desktop():
            runtime.execution.mac_control_enabled = bool(body.enabled)
            runtime.execution.mac_control_queue.clear()
            for waiter in runtime.execution.mac_control_waiters.values():
                if not waiter.done():waiter.set_result({"error": "控制开关已变更"})
            runtime.execution.mac_control_waiters.clear()
            runtime.execution.mac_screen_until = time.time() + 3
            return {"enabled": True, "control_enabled": runtime.execution.mac_control_enabled,
                    "available": bool(runtime.execution.mac_frame), "mode": "runner"}
        if config.isolation.get('browser'):
            raise HTTPException(409, 'native_mac_requires_paired_mac_runner')
        try:
            return await asyncio.to_thread(runtime.desktop.set_control, body.enabled)
        except DesktopControlDisabled as exc:
            raise HTTPException(409, str(exc)) from None
        except DesktopError as exc:
            raise HTTPException(503, str(exc)) from None

    @router.post("/desktop/mouse")
    async def desktop_mouse(body: DesktopMouse) -> dict:
        if mac_runner_desktop():
            return await runner_control("mouse", {"action": body.action, "x": body.x, "y": body.y,
                "dx": body.dx, "dy": body.dy, "button": body.button,
                "clicks": body.clicks, "delta_y": body.delta_y})
        if config.isolation.get('browser'):
            raise HTTPException(409, 'native_mac_requires_paired_mac_runner')
        try:
            return await asyncio.to_thread(
                runtime.desktop.mouse,
                action=body.action,
                x=body.x,
                y=body.y,
                dx=body.dx,
                dy=body.dy,
                button=body.button,
                clicks=body.clicks,
                delta_y=body.delta_y,
            )
        except DesktopControlDisabled as exc:
            raise HTTPException(409, str(exc)) from None
        except DesktopInputError as exc:
            raise HTTPException(422, str(exc)) from None
        except DesktopUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except DesktopError as exc:
            raise HTTPException(503, str(exc)) from None

    @router.post("/desktop/keyboard")
    async def desktop_keyboard(body: DesktopKeyboard) -> dict:
        if mac_runner_desktop():
            return await runner_control("keyboard", {"keys": body.keys})
        if config.isolation.get('browser'):
            raise HTTPException(409, 'native_mac_requires_paired_mac_runner')
        try:
            return await asyncio.to_thread(runtime.desktop.keyboard, keys=body.keys)
        except DesktopControlDisabled as exc:
            raise HTTPException(409, str(exc)) from None
        except DesktopInputError as exc:
            raise HTTPException(422, str(exc)) from None
        except DesktopUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except DesktopError as exc:
            raise HTTPException(503, str(exc)) from None

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
        mode = body.mode.strip() or config.sandbox.default_mode or "local"
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
                "tiers": config.models.tiers, "allow_mock": config.models.allow_mock,
                "usage": store.usage_by_provider()}

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

    @router.get("/models/voice")
    async def voice_settings() -> dict:
        # 每次都从磁盘重读，避免返回陈旧设置；has_key 只报告有无密钥。
        voice = config_module.load_voice()
        return {"voice": voice, "has_key": config_module.voice_has_key(voice)}

    @router.put("/models/voice")
    async def save_voice_settings(body: VoiceSettings) -> dict:
        try:
            voice = config_module.save_voice(body.mode, body.source, body.provider, body.model,
                                             body.base_url, body.api_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(500, "无法写入模型配置，请检查后端配置目录的写入权限") from None
        config.voice = voice
        runtime.config.voice = voice
        await runtime._emit("models.updated", {})
        # 不回显 api_key，只报告是否已保存密钥。
        return {"voice": voice, "has_key": config_module.voice_has_key(voice)}

    @router.post("/audio/transcriptions")
    async def transcribe_audio(request: Request, name: str = "voice.webm", language: str = "zh") -> dict:
        """服务端语音转写：音频只在内存里转发给所选连接，不落盘、不写库、不进日志。"""
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > VOICE_MAX_BYTES:
                raise HTTPException(413, "语音文件不超过 25 MB")
        if not raw:
            raise HTTPException(422, "没有收到音频数据")
        voice = config_module.load_voice()
        source = str(voice.get("source") or "connection")
        model = str(voice.get("model") or "")
        if source == "custom":
            # 用户直接填写的语音识别接口：密钥来自 voice.yaml（或旧的 .env），请求头不依赖 provider.available。
            provider_name = "voice"
            api_key = config_module.voice_key(voice)
            provider = config_module.Provider(name="voice", label="语音识别", type="openai",
                                              base_url=str(voice.get("base_url") or ""),
                                              api_key_env=str(voice.get("api_key_env") or "CARME_VOICE_API_KEY"))
            if not provider.base_url or not model:
                raise HTTPException(409, "未配置语音转写连接；请先在「设置 → 访问 → 语音输入」中选择模型连接与转写型号")
            if not api_key:
                raise HTTPException(409, "语音识别接口缺少 API 密钥，请在「设置 → 访问 → 语音输入」中填写，或直接编辑 /config/voice.yaml")
        else:
            provider_name = str(voice.get("provider") or "")
            if not provider_name or not model:
                raise HTTPException(409, "未配置语音转写连接；请先在「设置 → 访问 → 语音输入」中选择模型连接与转写型号")
            provider = config.models.providers.get(provider_name)
            if provider is None or provider.type not in config_module.API_TYPES:
                raise HTTPException(409, "语音转写连接已失效，请重新选择")
            if not provider.available:
                raise HTTPException(409, "语音转写连接缺少 API 密钥，请在「模型」中重新保存该连接")
            api_key = provider.api_key
        # 识别语言：合法的查询参数优先，其次 voice.yaml 的 language（要过同一个正则），最后回落中文。
        if not re.fullmatch(r"[A-Za-z][A-Za-z-]{1,9}", language or ""):
            from_file = str(voice.get("language") or "")
            language = from_file if re.fullmatch(r"[A-Za-z][A-Za-z-]{1,9}", from_file) else "zh"
        filename = audio_upload_name(name)
        headers = {"Authorization": f"Bearer {api_key}", **provider_session_headers(provider)}
        try:
            # 复用网关客户端：继承其超时与连接池，转写慢于普通对话也够用。
            client = await runtime.gateway._http()
            response = await client.post(f"{provider.base_url}/audio/transcriptions",
                                         files={"file": (filename, bytes(raw), audio_content_type(filename))},
                                         data={"model": model, "language": language}, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"无法连接语音转写服务：{redact_secret(exc, api_key)}") from None
        if not response.is_success:
            detail = redact_secret(response.text, api_key)
            # 有些连接不提供转写接口，会返回整页 HTML；原样抛给界面只会是噪声。
            if detail.lstrip().startswith("<") or "<html" in detail[:200].lower():
                detail = "该连接没有提供转写接口（返回了网页内容），请确认它支持 /audio/transcriptions 或换一个转写型号"
            raise HTTPException(422, f"语音转写失败（{response.status_code}）：{detail}")
        try:
            payload = response.json()
            text = str(payload.get("text") or "")
        except (ValueError, AttributeError):
            raise HTTPException(502, "语音转写返回了无法解析的内容") from None
        return {"text": text, "model": model, "provider": provider_name}

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
        # 部分网关（如 OpenCode Zen）的模型列表是公开的：无效 key 也能列出模型，
        # 「测试连接」必须再做一次真实调用，否则用户到保存时才发现 key 无效。
        # 依次尝试前几个模型：401 才代表 key 无效；403/404 只说明该型号不可用，
        # 不能据此判定整个 key 有问题。
        public_list = await runtime.gateway.check_models_public(provider)
        verify_note = ""
        if public_list and models:
            auth_error = ""
            verified_model = ""
            for candidate in [item["id"] for item in models[:3]]:
                check = await runtime.gateway.test_model(provider, candidate)
                if check["ok"]:
                    verified_model = candidate
                    break
                if check.get("status") == 401 and not auth_error:
                    auth_error = f"{candidate}：{check['error']}"
            if verified_model:
                verify_note = f" 该服务模型列表公开（列模型不校验 key），已用模型 {verified_model} 实际调用验证 API key 可用。"
            elif auth_error:
                return {"ok": False,
                        "error": f"该服务的模型列表是公开的，列模型不能验证 API key；"
                                 f"实际调用返回：{auth_error} "
                                 f"请确认 key 有效、账户有余额后重新测试。"}
            else:
                verify_note = " 注意：该服务模型列表公开，且试用的前几个模型都未通过验证调用；" \
                              "这不代表 key 无效，但保存时会实际验证你勾选的模型。"
        for key in list(model_probes):
            if time.monotonic() - model_probes[key]["time"] > 900:
                model_probes.pop(key)
        while len(model_probes) >= 32:
            model_probes.pop(next(iter(model_probes)))
        probe_id = secrets.token_urlsafe(24)
        model_probes[probe_id] = {"time": time.monotonic(), "fingerprint": connection_fingerprint(provider),
                                  "models": {item["id"]: item for item in models}}
        message = f"连接成功，已读取 {len(models)} 个模型。保存时会验证所选模型调用。{verify_note}"
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
        failed: list[dict] = []
        for item in body.models:
            known = proof["models"].get(item.id)
            if known is None or (item.effort and item.effort not in known["effort_options"]):
                raise HTTPException(422, "所选模型或 effort 尚未确认可用，请重新测试")
            # 每个型号单独验证：一个型号不可用不应该阻止其余可用型号保存。
            result = await runtime.gateway.test_model(provider, item.id, item.effort)
            if not result["ok"] and item.effort:
                # 先判断失败是否由 effort 引起：不带 effort 重试一次，
                # 这样能明确告诉用户「这个型号不接受该档位」，而不是笼统报错。
                plain = await runtime.gateway.test_model(provider, item.id)
                if plain["ok"]:
                    failed.append({"id": item.id, "effort": item.effort, "effort_rejected": True,
                                   "error": f"该型号不接受 effort「{item.effort}」：{result['error']} "
                                            f"（去掉 effort 后调用正常，请改选默认档位或更换档位）"})
                    continue
            if not result["ok"]:
                entry = {"id": item.id, "effort": item.effort,
                         "error": str(result.get("error") or "调用失败")}
                if result.get("status") in (403, 404):
                    # 型号出现在列表里却被拒，最常见的两个原因与「列表可见」
                    # 无关，必须说清楚，否则用户会以为是 key 或网址填错。
                    entry["hint"] = ("该型号在模型列表里但调用被拒：常见原因是它需要不同的接口协议"
                                     "（例如 Responses 或 Messages，而不是 Chat Completions），"
                                     "或不在当前账户额度内。可改用对应的 API 类型，或只勾选确认可用的型号。")
                failed.append(entry)
                continue
            selected.append({"id": item.id, "effort": item.effort,
                             "effort_options": known["effort_options"], "effort_source": known["effort_source"]})
        if not selected:
            return {"ok": False, "saved": [], "failed": failed,
                    "error": "所选模型都未通过调用验证，没有任何配置被写入。"
                             + (f"首个原因：{failed[0]['error']}" if failed else "")}
        # 同配置的重复保存串行；不修改 Bot、会话或记忆。
        async with model_save_gate:
            invalid_refs = set(config.models.invalid_refs)
            try:
                config.models = config_module.save_model_connection(provider, selected)
                config.models.invalid_refs = invalid_refs - {f"{provider.name}/{item['id']}" for item in selected}
            except (OSError, ValueError):
                raise HTTPException(500, "无法写入模型配置，请检查后端配置目录与 .env 的写入权限") from None
            runtime.config.models = config.models
            runtime.gateway.config.models = config.models
        await runtime._emit("models.updated", {"provider_id": provider.name})
        saved_ids = [item["id"] for item in selected]
        message = f"已保存 {len(saved_ids)} 个模型，Bot 现在可以选择它们"
        if failed:
            message += f"；另有 {len(failed)} 个型号未通过调用验证，没有写入"
        return {"ok": True, "message": message, "saved": saved_ids, "failed": failed}

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
        fresh = _reload_config_state()
        return {
            "ok": True,
            "agents": list(config.agents.agents),
            "browser_enabled": fresh.browser.enabled,
            "browser_patterns": len(fresh.browser.dangerous_patterns),
        }

    return router
