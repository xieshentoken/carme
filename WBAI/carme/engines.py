"""本机已安装的 CLI 引擎：只做固定命令探测和无工具文本调用。

这里不读取或修改任何 CLI 的凭据文件，也不接受网页传入的可执行文件路径。
每个引擎只通过代码内登记的二进制名称启动，参数用 argv 传递，不经过 shell。
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


CLI_ENGINE_DEFS = {
    "codex": {"label": "Codex CLI", "binary": "codex"},
    "pi": {"label": "Pi", "binary": "pi"},
    "claude": {"label": "Claude Code", "binary": "claude"},
}
ENGINE_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,199}$")
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_PROMPT_BYTES = 120_000
MAX_BRIDGE_BODY_BYTES = 512 * 1024
MAX_BRIDGE_TOOL_CALLS = 32
CARME_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_TOOL_PREFIX = "mcp__carme__"
BRIDGE_TOOL_NAMES = {"remember", "recall", "forget", "read_attachment", "create_artifact", "delegate"}


class CliEngineError(RuntimeError):
    """CLI 缺失、未登录、超时、非零退出或空输出。"""


@dataclass
class CliRunResult:
    engine: str
    text: str
    exit_code: int
    stderr: str = ""


@dataclass(frozen=True)
class BridgeTool:
    name: str
    description: str
    input_schema: dict[str, Any]


def _definition(engine: str) -> dict:
    definition = CLI_ENGINE_DEFS.get(engine)
    if definition is None:
        raise CliEngineError(f"不支持的 CLI 引擎：{engine}")
    return definition


def resolve_binary(engine: str) -> str:
    """只按固定引擎 id 查找固定二进制，不接受用户指定路径。"""
    binary = _definition(engine)["binary"]
    candidates = [shutil.which(binary)]
    candidates.extend(str(Path.home() / relative) for relative in (
        ".npm-global/bin/" + binary,
        ".local/bin/" + binary,
        ".bun/bin/" + binary,
    ))
    candidates.extend(f"{root}/{binary}" for root in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"))
    path = next((item for item in candidates if item and Path(item).is_file() and os.access(item, os.X_OK)), "")
    if not path:
        raise CliEngineError(f"未发现 {_definition(engine)['label']} 可执行文件")
    return str(Path(path).resolve())


def resolve_workspace(agent_id: str, configured: str = "", root: str | Path | None = None) -> Path:
    """Return the backend-Mac workspace snapshotted for a CLI task.

    A CLI task is deliberately not assigned the remote execution node.  Empty
    configuration gets one directory per Bot under Carme's local data tree;
    an explicit path is expanded locally and never interpreted on the node.
    """
    safe_agent = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", str(agent_id or ""))
    if not safe_agent:
        raise CliEngineError("CLI 工作目录缺少有效的 Bot id")
    configured = str(configured or "").strip()
    if any(char in configured for char in "\x00\r\n"):
        raise CliEngineError("CLI 工作目录不能包含控制字符")
    # ``root`` is the active profile's data directory, not necessarily the
    # source tree's ``data`` directory (deployments may set CARME_DATA_DIR).
    base = Path(root).expanduser().resolve() if root is not None else CARME_ROOT / "data"
    path = Path(configured).expanduser() if configured else base / "cli-workspaces" / agent_id
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _bridge_tools(tool_specs: list[dict]) -> list[BridgeTool]:
    """Convert existing OpenAI-style schemas into the small Carme bridge set."""
    result: list[BridgeTool] = []
    seen: set[str] = set()
    for spec in tool_specs:
        function = spec.get("function") or {}
        name = str(function.get("name") or "")
        if name not in BRIDGE_TOOL_NAMES or name in seen:
            continue
        seen.add(name)
        result.append(BridgeTool(name=name, description=str(function.get("description") or ""),
                                 input_schema=function.get("parameters") or {"type": "object", "properties": {}}))
    return result


def _mcp_name(name: str) -> str:
    return BRIDGE_TOOL_PREFIX + name


class TaskToolBridge:
    """One-task loopback bridge to the existing ToolRegistry.

    The bridge owns no tools and no execution policy.  The callback is built by
    Agent with the current ToolContext, so node selection, approval, budget,
    delegation, and event publication remain the existing Carme paths.
    """

    def __init__(self, tool_specs: list[dict], execute: Callable[[str, dict], Awaitable[Any]],
                 *, max_calls: int = MAX_BRIDGE_TOOL_CALLS) -> None:
        self.tools = _bridge_tools(tool_specs)
        self._by_name = {tool.name: tool for tool in self.tools}
        self._execute = execute
        self._max_calls = max(1, min(int(max_calls), MAX_BRIDGE_TOOL_CALLS))
        self._calls = 0
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.tools)

    @property
    def call_count(self) -> int:
        return self._calls

    @property
    def token(self) -> str:
        return self._token

    async def start(self) -> str:
        if not self.enabled:
            raise CliEngineError("本次 CLI 任务没有可桥接的 Carme 工具")
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=1_048_576)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        current = asyncio.current_task()
        pending = [task for task in self._clients if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass
        finally:
            if task is not None:
                self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes] | None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None
        if len(header) > 16 * 1024:
            return None
        lines = header[:-4].decode("latin-1", errors="replace").split("\r\n")
        if not lines or len(lines[0].split()) != 3:
            return None
        method, path, _ = lines[0].split()
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower().strip()] = value.strip()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_BRIDGE_BODY_BYTES:
            return None
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            return None
        return method.upper(), path, headers, body

    @staticmethod
    async def _write_response(writer: asyncio.StreamWriter, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                  405: "Method Not Allowed", 413: "Payload Too Large", 429: "Too Many Requests"}.get(status, "Error")
        writer.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body)
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await self._read_request(reader)
        if request is None:
            await self._write_response(writer, 400, {"ok": False, "error": "无效桥接请求"})
            return
        method, path, headers, body = request
        supplied = headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {self._token}"):
            await self._write_response(writer, 401, {"ok": False, "error": "桥接认证失败"})
            return
        if method == "GET" and path == "/tools":
            await self._write_response(writer, 200, {"tools": [
                # MCP clients add their server prefix to this raw tool name.
                {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
                for tool in self.tools
            ]})
            return
        if method != "POST" or path != "/call":
            await self._write_response(writer, 404 if path not in {"/tools", "/call"} else 405,
                                        {"ok": False, "error": "桥接路径或方法不支持"})
            return
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._write_response(writer, 400, {"ok": False, "error": "桥接参数不是有效 JSON"})
            return
        if not isinstance(data, dict):
            await self._write_response(writer, 400, {"ok": False, "error": "桥接参数必须是对象"})
            return
        raw_name = str(data.get("name") or "")
        name = raw_name[len(BRIDGE_TOOL_PREFIX):] if raw_name.startswith(BRIDGE_TOOL_PREFIX) else raw_name
        tool = self._by_name.get(name)
        arguments = data.get("arguments") or {}
        if tool is None or not isinstance(arguments, dict):
            await self._write_response(writer, 400, {"ok": False, "error": "工具未被本任务授权或参数无效"})
            return
        if self._calls >= self._max_calls:
            await self._write_response(writer, 429, {"ok": False, "error": "本次 CLI 工具调用已达到任务预算"})
            return
        self._calls += 1
        try:
            text = await self._execute(name, arguments)
            if isinstance(text, dict):
                payload = {"ok": True, **text}
                payload.setdefault("text", "")
            else:
                payload = {"ok": True, "text": str(text or "")}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - callback should already normalize tool errors
            payload = {"ok": False, "error": _safe_line(_redact(str(exc)))}
        await self._write_response(writer, 200, payload)

    def claude_config(self, python_path: str, pythonpath: str) -> dict:
        return {"mcpServers": {"carme": {
            "type": "stdio", "command": python_path,
            "args": ["-m", "carme.bridge_stdio"],
            "env": {"PYTHONPATH": pythonpath},
        }}}

    def mcp_names(self) -> list[str]:
        return [_mcp_name(tool.name) for tool in self.tools]

    def raw_names(self) -> list[str]:
        return [tool.name for tool in self.tools]


def _safe_line(value: str) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value or "").split())[:160]


def _redact(value: str) -> str:
    result = value or ""
    for key, secret in os.environ.items():
        if secret and len(secret) >= 4 and ("KEY" in key or "TOKEN" in key or "SECRET" in key):
            result = result.replace(secret, "[redacted]")
    # Do not rely on seeing the exact environment value: providers often
    # return a shortened or already-masked key in their error text.
    result = re.sub(
        r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+",
        r"\1[redacted]", result,
    )
    result = re.sub(
        r"(?i)(\b(?:x-api-key|api[-_ ]?key|apiKey|access[-_ ]?token|refresh[-_ ]?token)\b(?:\s+(?:provided|supplied|used))?\s*[:=]\s*['\"]?)[^'\"\s,;}]+",
        r"\1[redacted]", result,
    )
    result = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[redacted]", result)
    result = re.sub(
        r"(?i)\b(?:oapi|sk(?:-[a-z]+)?|pk|rk|xai|AIza)[A-Za-z0-9._*-]*(?:\.\.\.|…|\*{2,}|x{2,})[A-Za-z0-9._*-]+\b",
        "[redacted]", result,
    )
    return result


def _cli_base_env() -> dict[str, str]:
    """Environment shared by CLI discovery probes and real task launches."""
    env = os.environ.copy()
    # CLI processes use their own login/configuration.  Do not expose
    # Carme's model keys, bearer secrets, or webhook credentials to them.
    for key in list(env):
        if (key.startswith("CARME_") or key.endswith("_API_KEY")
                or key in {"TAVILY_API_KEY", "CARME_NOTIFY_SECRET"}):
            env.pop(key, None)
    return env


def _process_group_exists(pgid: int) -> bool:
    """Return whether a process remains in the dedicated subprocess group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The group exists, but the current process cannot signal it.  Keep
        # the conservative answer so cancellation does not claim cleanup.
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


async def _wait_process_group_gone(pgid: int, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_group_exists(pgid):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return True


async def _stop_process_group(process) -> None:
    """Terminate the complete CLI process group, including a dead leader's children.

    ``Process.returncode`` only describes the leader.  A leader may have
    already exited while a child that inherited its process group is still
    running, so never use the leader's return code as a cleanup shortcut.
    """
    pgid = int(process.pid)
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            # The normal path is a dedicated, same-user session.  Retain the
            # leader fallback for unusual platforms where killpg is absent.
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        pass

    if await _wait_process_group_gone(pgid, timeout=0.5):
        return

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        pass
    # SIGKILL cannot be ignored, but allow the kernel/reaper a short interval
    # to remove the group before returning to the cancelled task.
    await _wait_process_group_gone(pgid, timeout=1)


def _run_probe(argv: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=timeout,
                                check=False, env=_cli_base_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", _safe_line(str(exc))
    return result.returncode, result.stdout, result.stderr


def _pi_defaults() -> tuple[str, str]:
    raw_dir = os.getenv("PI_CODING_AGENT_DIR", "~/.pi/agent")
    path = Path(raw_dir).expanduser() / "settings.json"
    try:
        if path.stat().st_size > 256 * 1024:
            return "", ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    provider = str(data.get("defaultProvider") or "").strip()
    model = str(data.get("defaultModel") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", provider):
        provider = ""
    if model and not ENGINE_MODEL_PATTERN.fullmatch(model):
        model = ""
    if not provider:
        provider = _pi_auth_provider()
    return provider, model


def _pi_auth_provider() -> str:
    """settings.json 未设默认 provider 时，从 auth.json 读取已登录的 provider 名。

    只使用顶层键名判断登录了哪些 provider，不读取任何凭据内容。
    """
    raw_dir = os.getenv("PI_CODING_AGENT_DIR", "~/.pi/agent")
    path = Path(raw_dir).expanduser() / "auth.json"
    try:
        if path.stat().st_size > 256 * 1024:
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in data:
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", key):
            return key
    return ""


def _auth_status(engine: str, binary: str) -> dict:
    """只返回登录状态摘要；不把 CLI 输出原文送进网页。"""
    if engine == "codex":
        code, _, _ = _run_probe([binary, "login", "status"])
        return {"status": "logged_in" if code == 0 else "not_logged_in", "provider": ""}
    if engine == "claude":
        code, stdout, _ = _run_probe([binary, "auth", "status", "--json"])
        try:
            data = json.loads(stdout)
        except (ValueError, TypeError):
            data = {}
        logged_in = bool(data.get("loggedIn")) if isinstance(data, dict) else False
        method = str(data.get("authMethod") or "") if isinstance(data, dict) else ""
        return {"status": "logged_in" if code == 0 and logged_in else "not_logged_in",
                "auth_method": method if method in {"oauth_token", "api_key", "console_api_key"} else ""}
    provider, model = _pi_defaults()
    if not provider and not model:
        return {"status": "unknown", "provider": "", "model": ""}
    flag, value = ("--provider", provider) if provider else ("--model", model)
    code, stdout, _ = _run_probe([binary, "auth", "check", flag, value, "--json", "--no-refresh"])
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        data = {}
    state = str(data.get("status") or "") if isinstance(data, dict) else ""
    return {"status": "logged_in" if code == 0 and state in {"ready", "logged_in", "authenticated"} else
            "not_logged_in" if state in {"not_ready", "logged_out", "unauthenticated"} or code else "unknown",
            "provider": provider, "model": model}


def detect_cli_engines() -> list[dict]:
    result = []
    for engine, definition in CLI_ENGINE_DEFS.items():
        try:
            binary = resolve_binary(engine)
        except CliEngineError:
            result.append({"id": engine, "label": definition["label"], "binary": definition["binary"],
                           "installed": False, "ready": False, "status": "not_installed",
                           "version": "", "auth_status": "unknown", "capability": "text_readonly"})
            continue
        code, stdout, stderr = _run_probe([binary, "--version"])
        version = _safe_line(stdout or stderr)
        auth = _auth_status(engine, binary)
        result.append({"id": engine, "label": definition["label"], "binary": definition["binary"],
                       "path": binary, "installed": code == 0, "ready": code == 0 and auth["status"] == "logged_in",
                       "status": "ready" if code == 0 and auth["status"] == "logged_in" else "detected",
                       "version": version, "auth_status": auth["status"],
                       **{key: value for key, value in auth.items() if key not in {"status"}},
                       "capability": "text_readonly"})
    return result


def _checked_model(value: str) -> str:
    value = str(value or "").strip()
    if value and not ENGINE_MODEL_PATTERN.fullmatch(value):
        raise CliEngineError("CLI 模型名包含不支持的字符")
    return value


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ",".join(_toml_string(value) for value in values) + "]"


def _typebox_schema(schema: dict, indent: str = "  ") -> str:
    """Render the small JSON-schema subset used by Carme tools as JS objects."""
    if not isinstance(schema, dict):
        return "{ type: \"object\", properties: {} }"
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        fields = []
        for key, value in properties.items():
            rendered = _typebox_schema(value, indent + "  ")
            fields.append(f"{json.dumps(str(key))}: {rendered}")
        required_js = "[" + ",".join(json.dumps(str(item)) for item in schema.get("required") or []) + "]"
        return "{ type: \"object\", properties: { " + ", ".join(fields) + " }, required: " + required_js + " }"
    if kind == "array":
        return "{ type: \"array\", items: " + _typebox_schema(schema.get("items") or {}, indent + "  ") + " }"
    if kind in {"string", "integer", "number", "boolean"}:
        return "{ type: " + json.dumps(kind) + " }"
    return "{}"


def _write_pi_extension(path: Path, tools: list[BridgeTool]) -> None:
    lines = [
        "// Generated for one Carme task. The capability token is read only from the child process environment.",
        "const bridgeUrl = String(process.env.CARME_BRIDGE_URL || '').replace(/\\/$/, '');",
        "const bridgeToken = String(process.env.CARME_BRIDGE_TOKEN || '');",
        "async function callCarme(name, arguments_) {",
        "  if (!bridgeUrl || !bridgeToken) throw new Error('Carme 工具桥接未配置');",
        "  const response = await fetch(bridgeUrl + '/call', { method: 'POST', headers: { 'Authorization': 'Bearer ' + bridgeToken, 'Content-Type': 'application/json' }, body: JSON.stringify({ name, arguments: arguments_ || {} }) });",
        "  const data = await response.json();",
        "  if (!data.ok) throw new Error(String(data.error || 'Carme 工具调用失败'));",
        "  return data;",
        "}",
        "export default function(pi) {",
    ]
    for tool in tools:
        safe_name = "carme_" + re.sub(r"[^A-Za-z0-9_]", "_", tool.name)
        parameters = _typebox_schema(tool.input_schema)
        lines.extend([
            "  pi.registerTool({",
            f"    name: {json.dumps(safe_name)},",
            f"    label: {json.dumps('Carme ' + tool.name)},",
            f"    description: {json.dumps(tool.description)},",
            f"    parameters: {parameters},",
            "    async execute(_toolCallId, params) {",
            f"      const data = await callCarme({json.dumps(_mcp_name(tool.name))}, params);",
            "      return { content: Array.isArray(data.content) ? data.content : [{ type: 'text', text: String(data.text || '') }], details: {} };",
            "    },",
            "  });",
        ])
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_argv(engine: str, binary: str, model: str = "", effort: str = "", *,
               workspace_dir: str = "", bridge: TaskToolBridge | None = None,
               bridge_config_path: str = "", pi_extension_path: str = "",
               claude_settings_path: str = "", max_turns: int = 24) -> list[str]:
    model, effort = _checked_model(model), _checked_model(effort)
    if engine == "codex":
        argv = [binary, "exec", "--ephemeral", "--json", "--skip-git-repo-check",
                "--ignore-user-config", "--sandbox", "workspace-write", "--color", "never"]
        for feature in (
            "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
            "computer_use", "hooks", "image_generation", "in_app_browser",
            "in_app_local_automation", "memories", "multi_agent", "plugins", "plugin_sharing",
            "remote_plugin", "skill_mcp_dependency_install", "sleep_tool",
        ):
            argv.extend(["--disable", feature])
        if workspace_dir:
            argv.extend(["--cd", workspace_dir])
        if bridge is not None:
            argv.extend(["-c", "mcp_servers.carme.command=" + _toml_string(sys.executable),
                         "-c", "mcp_servers.carme.args=" + _toml_array(["-m", "carme.bridge_stdio"]),
                         "-c", "mcp_servers.carme.enabled_tools=" + _toml_array(bridge.raw_names()),
                         "-c", "mcp_servers.carme.env_vars=" + _toml_array(["CARME_BRIDGE_URL", "CARME_BRIDGE_TOKEN", "PYTHONPATH"])])
            for name in bridge.raw_names():
                argv.extend(["-c", f"mcp_servers.carme.tools.{name}.approval_mode=\"approve\""])
        if effort:
            argv.extend(["-c", "model_reasoning_effort=" + _toml_string(effort)])
        if model:
            argv.extend(["--model", model])
        return argv
    if engine == "pi":
        native = ["read", "bash", "edit", "write", "grep", "find", "ls"]
        custom = ["carme_" + tool.name for tool in (bridge.tools if bridge is not None else [])]
        argv = [binary, "--mode", "json", "--print", "--no-session", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                "--tools", ",".join(native + custom)]
        if pi_extension_path:
            argv.extend(["--extension", pi_extension_path])
        if model:
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--thinking", "off" if effort == "none" else effort])
        return argv
    native = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
    mcp_names = bridge.mcp_names() if bridge is not None else []
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--no-session-persistence", "--tools",
            ",".join(native if bridge is None else native + mcp_names),
            "--strict-mcp-config", "--permission-mode", "acceptEdits",
            "--permission-prompts", "none", "--setting-sources", "user",
            "--disable-slash-commands", "--no-chrome", "--max-turns", str(max(1, min(int(max_turns), 32)))]
    if bridge is None:
        argv[argv.index("--tools") + 1] = ""
    elif bridge_config_path:
        argv.extend(["--mcp-config", bridge_config_path, "--allowed-tools", ",".join(native + mcp_names)])
    if claude_settings_path:
        argv.extend(["--settings", claude_settings_path])
    if workspace_dir:
        argv.extend(["--add-dir", workspace_dir])
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    return argv


def _text_from_content(content, *, has_tool_bridge: bool = False) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict) and block.get("type") in {"image_url", "input_image", "image"}:
                parts.append("[图片附件未直接嵌入 CLI 输入；请使用 read_attachment 获取图片资料。]"
                             if has_tool_bridge else "[图片资料已省略：当前 CLI 调用未启用图片输入]")
        return "\n".join(parts)
    return str(content or "")


def format_prompt(messages: list[dict], system_extra: str = "", *,
                  has_tool_bridge: bool = False, workspace_dir: str = "") -> str:
    parts = [
        ("你正在 Carme 中通过本机 CLI 引擎回答问题。你可以使用当前后端 Mac 工作目录中的本机终端/文件工具；"
         "不要把它描述成远程执行电脑。" if workspace_dir else
         "你正在 Carme 中通过本机 CLI 文本引擎回答问题。不要声称做过未实际完成的操作。"),
    ]
    if workspace_dir:
        parts.append(f"后端 Mac 工作目录：{workspace_dir}。CLI 默认从此目录启动；原生文件访问范围由当前 CLI 权限模式和 Mac 账号权限决定，不要假定 Carme 对原生工具提供了完整 OS 目录隔离。")
    if has_tool_bridge:
        parts.append("本次调用还提供了提示中列出的 Carme 记忆、附件、成果或委派工具；这些工具受当前任务权限与预算约束。")
    else:
        parts.append("本次调用没有 Carme 领域工具；不要声称已读取附件、保存成果、调用记忆或委派任务。")
    if system_extra:
        parts.extend(["\n[角色与运行规则]", system_extra])
    for message in messages:
        role = str(message.get("role") or "user")
        text = _text_from_content(message.get("content"), has_tool_bridge=has_tool_bridge)
        if text:
            parts.append(f"\n[{role}]\n{text}")
    prompt = "\n".join(parts)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise CliEngineError("Carme 上下文超过 CLI 引擎安全上限；请先让 Carme 生成摘要后再继续")
    return prompt


def _event_text(engine: str, event: dict) -> tuple[str, str]:
    """返回 (增量或最终正文, 错误)。"""
    if isinstance(event.get("event"), dict):
        nested_event = event["event"]
        if event.get("type") == "stream_event":
            event = nested_event
    kind = str(event.get("type") or "")
    if kind in {"error", "turn.failed", "response.failed", "result_error"}:
        value = event.get("error") or event.get("message") or event.get("result") or "CLI 返回错误"
        return "", _safe_line(_redact(str(value)))
    if engine == "pi":
        nested = event.get("assistantMessageEvent") or {}
        if isinstance(nested, dict) and nested.get("type") == "text_delta":
            return str(nested.get("delta") or ""), ""
        if kind in {"message_end", "message_stop"}:
            message = event.get("message") or {}
            if str(message.get("stopReason") or "") == "error":
                return "", _safe_line(_redact(str(message.get("errorMessage") or "Pi 返回错误")))
            return _text_from_content(message.get("content")), ""
    if engine == "claude":
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            return str(delta.get("text") or ""), ""
        if kind == "result":
            if event.get("is_error"):
                return "", _safe_line(_redact(str(event.get("result") or "Claude Code 返回错误")))
            return str(event.get("result") or ""), ""
        if kind == "assistant":
            message = event.get("message") or {}
            return _text_from_content(message.get("content")), ""
    if engine == "codex":
        if kind.endswith("agentMessage/delta") or kind.endswith("agent_message/delta"):
            return str(event.get("delta") or event.get("text") or ""), ""
        item = event.get("item") or {}
        if isinstance(item, dict) and item.get("type") in {"agent_message", "assistant_message", "message"}:
            return str(item.get("text") or _text_from_content(item.get("content"))), ""
    return "", ""


def _native_tool_events(
    engine: str,
    event: dict,
    pending: dict[str, tuple[str, bool]],
    bridge_names: set[str],
) -> list[tuple[str, str, bool]]:
    """Extract safe start/end summaries from a CLI's native tool events.

    The CLI output can contain commands, paths, URLs, and tool arguments.  The
    activity feed only needs a display name and status, so deliberately do not
    copy any of those fields into the event payload.
    """
    inner = event
    if event.get("type") == "stream_event" and isinstance(event.get("event"), dict):
        inner = event["event"]
    kind = str(inner.get("type") or "")

    def suppressed(name: str) -> bool:
        raw = str(name or "")
        if raw in bridge_names:
            return True
        if raw.startswith(BRIDGE_TOOL_PREFIX) and raw[len(BRIDGE_TOOL_PREFIX):] in bridge_names:
            return True
        return raw.startswith("carme_") and raw[6:] in bridge_names

    def start(name: str, identifier: str) -> list[tuple[str, str, bool]]:
        name = str(name or "native_tool")[:80]
        identifier = str(identifier or name)[:160]
        if identifier in pending:
            return []
        hidden = suppressed(name)
        pending[identifier] = (name, hidden)
        return [] if hidden else [("tool.start", name, False)]

    def end(name: str, identifier: str, failed: bool = False) -> list[tuple[str, str, bool]]:
        identifier = str(identifier or name)[:160]
        previous = pending.pop(identifier, None)
        if previous is None and name:
            # Some Pi/Claude result events only carry the tool name.
            for key, candidate in list(pending.items()):
                if candidate[0] == name:
                    previous = pending.pop(key)
                    break
        display_name, hidden = previous or (str(name or "native_tool")[:80], suppressed(str(name or "")))
        return [] if hidden else [("tool.end", display_name, failed)]

    if engine == "codex":
        item = inner.get("item")
        if not isinstance(item, dict):
            return []
        item_type = str(item.get("type") or "")
        if item_type not in {"command_execution", "file_change", "mcp_tool_call",
                             "web_search_call", "computer_call", "shell_command"}:
            return []
        name = str(item.get("tool_name") or item.get("name") or item_type)
        identifier = str(item.get("id") or item.get("call_id") or name)
        if kind in {"item.started", "item.created", "item.in_progress", "item.updated"}:
            return start(name, identifier)
        if kind in {"item.completed", "item.failed", "item.error"}:
            status = str(item.get("status") or "")
            return end(name, identifier, kind in {"item.failed", "item.error"}
                       or status in {"failed", "error", "incomplete"})
        return []

    if engine == "claude":
        blocks: list[dict] = []
        if kind == "content_block_start":
            block = inner.get("content_block") or inner.get("block")
            if isinstance(block, dict):
                blocks = [block]
        elif kind in {"assistant", "user"}:
            message = inner.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else []
        elif kind in {"tool_use", "tool_result"}:
            blocks = [inner]
        output: list[tuple[str, str, bool]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "tool_use" or kind == "tool_use":
                output.extend(start(str(block.get("name") or "tool_use"),
                                    str(block.get("id") or block.get("tool_use_id") or block.get("name") or "tool_use")))
            elif block_type == "tool_result" or kind == "tool_result":
                output.extend(end(str(block.get("name") or ""),
                                  str(block.get("tool_use_id") or block.get("id") or ""),
                                  bool(block.get("is_error") or block.get("isError"))))
        return output

    if engine == "pi":
        if kind == "tool_execution_start":
            return start(str(inner.get("toolName") or inner.get("tool_name") or inner.get("name") or "tool"),
                         str(inner.get("toolCallId") or inner.get("tool_call_id") or inner.get("id") or ""))
        if kind == "tool_execution_end":
            return end(str(inner.get("toolName") or inner.get("tool_name") or inner.get("name") or ""),
                       str(inner.get("toolCallId") or inner.get("tool_call_id") or inner.get("id") or ""),
                       bool(inner.get("isError") or inner.get("is_error") or inner.get("error")))
    return []


def _event_is_snapshot(engine: str, event: dict) -> bool:
    if isinstance(event.get("event"), dict) and event.get("type") == "stream_event":
        event = event["event"]
    kind = str(event.get("type") or "")
    return ((engine == "claude" and kind in {"result", "assistant"})
            or (engine == "pi" and kind in {"message_end", "message_stop"})
            or (engine == "codex" and isinstance(event.get("item"), dict)
                and event["item"].get("type") in {"agent_message", "assistant_message", "message"}))


async def run_cli_engine(
    engine: str,
    prompt: str,
    *,
    model: str = "",
    effort: str = "",
    timeout: float = 180.0,
    workspace_dir: str = "",
    tool_specs: list[dict] | None = None,
    tool_execute: Callable[[str, dict], Awaitable[Any]] | None = None,
    max_tool_calls: int = MAX_BRIDGE_TOOL_CALLS,
    on_stream: Callable[[dict], Awaitable[None]] | None = None,
    on_tool_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> CliRunResult:
    binary = resolve_binary(engine)
    if tool_specs and tool_execute is None:
        raise CliEngineError("CLI 工具桥接缺少 Carme 执行回调")
    bridge = TaskToolBridge(tool_specs or [], tool_execute, max_calls=max_tool_calls) if tool_specs and tool_execute else None
    temporary = tempfile.TemporaryDirectory(prefix="carme-cli-")
    temp_dir = Path(temporary.name)
    process = None
    try:
        bridge_url = await bridge.start() if bridge is not None else ""
        bridge_config_path = ""
        pi_extension_path = ""
        claude_settings_path = ""
        env = _cli_base_env()
        if engine == "claude":
            claude_settings_path = str(temp_dir / "claude-settings.json")
            (temp_dir / "claude-settings.json").write_text(
                json.dumps({"disableAllHooks": True}, ensure_ascii=False), encoding="utf-8"
            )
        if bridge is not None:
            env["CARME_BRIDGE_URL"] = bridge_url
            env["CARME_BRIDGE_TOKEN"] = bridge.token
            pythonpath = str(CARME_ROOT)
            inherited = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = pythonpath + (os.pathsep + inherited if inherited else "")
            if engine == "claude":
                bridge_config_path = str(temp_dir / "mcp.json")
                (temp_dir / "mcp.json").write_text(
                    json.dumps(bridge.claude_config(sys.executable, env["PYTHONPATH"]), ensure_ascii=False),
                    encoding="utf-8",
                )
            elif engine == "pi":
                pi_extension_path = str(temp_dir / "carme-tools.ts")
                _write_pi_extension(Path(pi_extension_path), bridge.tools)
        argv = build_argv(engine, binary, model, effort, workspace_dir=workspace_dir,
                          bridge=bridge, bridge_config_path=bridge_config_path,
                          pi_extension_path=pi_extension_path,
                          claude_settings_path=claude_settings_path,
                          max_turns=max_tool_calls)
        # No configured workspace means an isolated temporary directory, never the Carme repo.
        cwd = Path(workspace_dir).expanduser().resolve() if workspace_dir else temp_dir
        cwd.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=env, limit=1_048_576,
            start_new_session=True,
        )
    except OSError as exc:
        if bridge is not None:
            await bridge.close()
        temporary.cleanup()
        raise CliEngineError(f"无法启动 {_definition(engine)['label']}：{_safe_line(str(exc))}") from None
    except Exception:
        if bridge is not None:
            await bridge.close()
        temporary.cleanup()
        raise

    state = {"text": "", "bytes": 0}
    native_pending: dict[str, tuple[str, bool]] = {}
    native_started = 0
    bridge_names = set(bridge.raw_names()) if bridge is not None else set()
    message_id = f"cli_{engine}_{id(process)}"

    async def read_stdout() -> None:
        nonlocal native_started
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            state["bytes"] += len(line)
            if state["bytes"] > MAX_STDOUT_BYTES:
                raise CliEngineError("CLI 输出超过 2 MB 上限")
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                event = json.loads(decoded)
            except (ValueError, TypeError):
                # JSON modes are required.  A stray diagnostic line must not
                # become an assistant answer or reflect a secret to the UI.
                continue
            else:
                native_events = _native_tool_events(engine, event if isinstance(event, dict) else {},
                                                    native_pending, bridge_names)
                for event_type, tool_name, failed in native_events:
                    if event_type == "tool.start":
                        native_started += 1
                        if native_started > max(1, min(int(max_tool_calls), MAX_BRIDGE_TOOL_CALLS)):
                            raise CliEngineError("CLI 原生工具调用已达到任务预算")
                    if on_tool_event:
                        payload = {"tool": tool_name, "source": "cli-native", "engine": engine}
                        if event_type == "tool.end":
                            payload["status"] = "error" if failed else "done"
                        await on_tool_event(event_type, payload)
                delta, error = _event_text(engine, event if isinstance(event, dict) else {})
            if error:
                raise CliEngineError(_safe_line(_redact(error)))
            if not delta:
                continue
            if isinstance(event, dict) and _event_is_snapshot(engine, event):
                state["text"] = delta
            elif delta.startswith(state["text"]):
                state["text"] = delta
            else:
                state["text"] += delta
            if on_stream:
                await on_stream({"message_id": message_id, "content": state["text"],
                                 "model": model or f"{engine}-cli", "provider": engine, "status": "streaming"})

    async def read_stderr() -> str:
        tail = bytearray()
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            tail.extend(chunk)
            if len(tail) > MAX_STDERR_BYTES:
                del tail[:-MAX_STDERR_BYTES]
        return bytes(tail).decode("utf-8", errors="replace")

    stdout_task = asyncio.create_task(read_stdout())
    stderr_task = asyncio.create_task(read_stderr())
    try:
        if process.stdin:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
        await asyncio.wait_for(asyncio.gather(stdout_task, process.wait()), timeout=timeout)
        stderr = await stderr_task
    except asyncio.TimeoutError:
        await _stop_process_group(process)
        for task in (stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise CliEngineError(f"{_definition(engine)['label']} 调用超时（{int(timeout)} 秒）") from None
    except asyncio.CancelledError:
        await _stop_process_group(process)
        for task in (stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    except Exception:
        await _stop_process_group(process)
        for task in (stdout_task, stderr_task):
            task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    finally:
        if bridge is not None:
            await bridge.close()
        temporary.cleanup()

    text = state["text"].strip()
    if process.returncode != 0:
        detail = _safe_line(_redact(stderr)) or "未提供错误详情"
        raise CliEngineError(f"{_definition(engine)['label']} 退出码 {process.returncode}：{detail}")
    if not text:
        raise CliEngineError(f"{_definition(engine)['label']} 返回空输出；请检查登录状态和模型配置")
    return CliRunResult(engine=engine, text=text, exit_code=process.returncode, stderr=_safe_line(_redact(stderr)))
