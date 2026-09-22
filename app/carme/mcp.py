"""MCP（Model Context Protocol）客户端 —— 把外部 MCP Server 的工具接进 Carme。

方向和 bridge_stdio.py 相反：那边是 Carme 把自己的工具「喂」给 CLI 引擎，
这边是 Carme 去连别人写的 MCP Server，把对方的工具变成 Bot 能调用的工具。

支持两种传输：
    stdio   在本机拉起一个常驻子进程，用换行分隔的 JSON-RPC 说话（最常见）
    http    用 POST 发 JSON-RPC，兼容 streamable HTTP 的 JSON / SSE 两种响应

连接是懒加载的：Bot 真的有一个 mcp 工具要调用时才需要进程活着；
管理端可以手动连接/断开，也可以把 enabled 打开让后端启动时自动连。

工具命名沿用社区习惯：mcp__<server>__<tool>。
外部工具名会做安全化（只留 [A-Za-z0-9_-]，超长加哈希后缀），
所以模型看到的工具名一定符合各家 API 的函数名约束。

安全边界：stdio server 是真实的本机进程，能跑什么完全由它自己决定，
因此「装哪个 server」本身就是一次授权动作 —— 和装 npm 包一样要人来点。
config/mcp.yaml 里每个 server 还可以选 approval: confirm，让每次调用都过人工闸门。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import yaml

from .tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from .tools.base import ToolRegistry

log = logging.getLogger("carme.mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_TOOL_PREFIX = "mcp__"
MCP_SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
MAX_SERVERS = 32
MAX_JSON_BYTES = 4 * 1024 * 1024
# 子进程 stdout 的单行上限：超过它 readline 会抛异常，用来拦住失控的 Server。
STDIO_LINE_LIMIT = MAX_JSON_BYTES
MAX_TOOL_NAME = 64
MAX_STDERR_LINES = 40
DEFAULT_TIMEOUT = 30.0
CONNECT_TIMEOUT = 90.0
MAX_TIMEOUT = 300.0
MAX_HEADER_VALUE = 2000
MAX_ARG_COUNT = 32
# 单个图片结果最大 4 MB：再大既塞不进上下文，也没必要替模型转存。
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class MCPError(RuntimeError):
    """MCP 连接、协议或工具调用失败；消息可以直接显示给人工用户。"""


def validate_schema(schema, value, *, definition=False, depth=0):
    """Conservative JSON Schema subset; unsupported constraints never become grants."""
    allowed={'type','properties','required','additionalProperties','items','enum','description','title',
             'default','minimum','maximum','minLength','maxLength','minItems','maxItems','format','$schema'}
    if not isinstance(schema,dict) or set(schema)-allowed or depth>12:raise MCPError('schema_constraint_unsupported')
    kind=schema.get('type')
    if kind not in {'object','array','string','number','integer','boolean','null'}:raise MCPError('schema_type_unsupported')
    if kind=='object':
        props=schema.get('properties',{});required=schema.get('required',[])
        if not isinstance(props,dict) or not isinstance(required,list) or not set(required)<=set(props) or schema.get('additionalProperties',False) is not False:raise MCPError('schema_object_unsupported')
        for sub in props.values():validate_schema(sub,None,definition=True,depth=depth+1)
    if kind=='array':validate_schema(schema.get('items'),None,definition=True,depth=depth+1)
    if definition:return
    import math
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
           'number':type(value) in (float,int) and math.isfinite(value),'integer':type(value) is int,
           'boolean':type(value) is bool,'null':value is None}[kind]
    if not valid or ('enum' in schema and value not in schema['enum']):raise MCPError('mcp_argument_schema_denied')
    if kind=='object':
        if not set(required)<=set(value) or (set(value)-set(props) and not schema.get('additionalProperties',False)):raise MCPError('mcp_argument_fields_denied')
        for key,item in value.items():
            if key in props:validate_schema(props[key],item,depth=depth+1)
    if kind=='array':
        if not schema.get('minItems',0)<=len(value)<=min(schema.get('maxItems',1000),1000):raise MCPError('mcp_array_limit')
        for item in value:validate_schema(schema['items'],item,depth=depth+1)
    if kind=='string' and not schema.get('minLength',0)<=len(value)<=min(schema.get('maxLength',32768),32768):raise MCPError('mcp_string_limit')
    if kind in {'integer','number'} and not schema.get('minimum',-float('inf'))<=value<=schema.get('maximum',float('inf')):raise MCPError('mcp_number_limit')


def check_grant(server, specs, remote, arguments, grant):
    from .security import digest
    spec=next((s for s in specs if s['name']==remote),None)
    if not server or not server.enabled or not spec or digest(server.to_yaml())!=grant['server_hash'] or digest(spec['inputSchema'])!=grant['schema_hash']:
        raise MCPError('mcp_schema_or_identity_changed')
    validate_schema(spec['inputSchema'],arguments)
    for key,values in grant['argument_allowlist'].items():
        if key in arguments and arguments[key] not in values:raise MCPError('mcp_resource_denied')


# --------------------------------------------------------------------------- #
#  配置
# --------------------------------------------------------------------------- #


@dataclass
class MCPServer:
    id: str
    name: str = ""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    approval: str = "auto"
    timeout: float = DEFAULT_TIMEOUT
    source: str = ""
    installed_at: float = 0.0
    executor: str = ''
    trusted_host: bool = False  # Explicit local debugging only, never implied by enabled.

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def target(self) -> str:
        """界面上显示「连到哪里」，不回显任何密钥。"""
        if self.transport == "http":
            return self.url
        return " ".join([self.command, *self.args]).strip()

    def to_yaml(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"name": self.name, "transport": self.transport}
        if self.transport == "stdio":
            raw["command"] = self.command
            if self.args:
                raw["args"] = list(self.args)
            if self.env:
                raw["env"] = dict(self.env)
            if self.cwd:
                raw["cwd"] = self.cwd
        else:
            raw["url"] = self.url
            if self.headers:
                raw["headers"] = dict(self.headers)
        raw.update({
            "enabled": self.enabled,
            "approval": self.approval,
            "timeout": self.timeout,
            "source": self.source,
            "installed_at": self.installed_at,
            "trusted_host": self.trusted_host,
            "executor": self.executor,
        })
        return raw

    @classmethod
    def from_yaml(cls, server_id: str, raw: dict[str, Any]) -> MCPServer:
        """从手写的 YAML 里读一个 Server 定义。

        这一段必须足够宽容：mcp.yaml 是人工可编辑的文件，写错一个字段
        不该让整个后端起不来 —— 坏值退化成默认值，最差也只是这个 Server 连不上。
        """
        args = raw.get("args")
        env = raw.get("env")
        headers = raw.get("headers")
        return cls(
            id=server_id,
            name=str(raw.get("name") or server_id)[:80],
            transport="http" if str(raw.get("transport") or "stdio") == "http" else "stdio",
            command=str(raw.get("command") or "")[:400],
            args=[str(item)[:400] for item in (args if isinstance(args, list) else [])
                  if isinstance(item, (str, int, float))][:MAX_ARG_COUNT],
            env={str(key): str(value) for key, value in (env if isinstance(env, dict) else {}).items()
                 if isinstance(value, (str, int, float))},
            cwd=str(raw.get("cwd") or "")[:500],
            url=str(raw.get("url") or "")[:2048],
            headers={str(key): str(value)[:MAX_HEADER_VALUE] for key, value in (headers if isinstance(headers, dict) else {}).items()
                     if isinstance(value, (str, int, float))},
            enabled=bool(raw.get("enabled", True)),
            approval="confirm" if str(raw.get("approval") or "auto") == "confirm" else "auto",
            timeout=_clamp_timeout(raw.get("timeout")),
            source=str(raw.get("source") or "config/mcp.yaml")[:200],
            installed_at=_safe_time(raw.get("installed_at")),
            executor='action' if raw.get('executor')=='action' else '',
            trusted_host=raw.get("trusted_host") is True,
        )

    def public(self, *, status: str, tools: list[dict], error: str = "",
               server_info: str = "", protocol: str = "") -> dict[str, Any]:
        """给界面看的形态。

        命令、参数、工作目录、地址都要原样回显：编辑表单靠它们填回原值，
        否则「带空格的参数」在重新保存时会被拆坏。
        env / headers 只回显键名，值永不外泄。
        """
        return {
            "id": self.id,
            "isolation": "docker-action" if self.executor == "action" else "trusted-host-not-isolated" if self.trusted_host else "external-http" if self.transport == "http" else "executor-required",
            "executor": self.executor,
            "name": self.name,
            "label": self.label,
            "transport": self.transport,
            "target": self.target,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "url": self.url,
            "env_keys": sorted(self.env),
            "header_keys": sorted(self.headers),
            "enabled": self.enabled,
            "approval": self.approval,
            "timeout": self.timeout,
            "source": self.source,
            "installed_at": self.installed_at,
            "status": status,
            "tools": tools,
            "tool_count": len(tools),
            "error": error,
            "server_info": server_info,
            "protocol": protocol,
        }


def _safe_time(value: Any) -> float:
    """时间戳容错：手写 YAML 里写错也只当没有。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number < 10 ** 12 else 0.0


def _clamp_timeout(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if number != number:  # NaN
        return DEFAULT_TIMEOUT
    return max(1.0, min(number, MAX_TIMEOUT))


def _clean_text(value: str, limit: int, *, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise MCPError("这一项不能为空")
    if len(text) > limit:
        raise MCPError(f"内容超过 {limit} 个字符")
    if any(ord(char) < 32 for char in text):
        raise MCPError("内容不能包含控制字符")
    return text


def _clean_env(values: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (values or {}).items():
        name = str(key).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            raise MCPError(f"环境变量名不合法：{name[:40]}")
        text = str(value)
        if len(text) > 2000:
            raise MCPError(f"环境变量 {name} 的值过长")
        if any(char == "\x00" for char in text):
            raise MCPError(f"环境变量 {name} 的值不能包含空字符")
        result[name] = text
        if len(result) > MAX_ARG_COUNT:
            raise MCPError(f"环境变量最多 {MAX_ARG_COUNT} 个")
    return result


def _clean_headers(values: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (values or {}).items():
        name = str(key).strip()
        if not name or len(name) > 64 or any(ord(char) < 33 or ord(char) > 126 for char in name):
            raise MCPError(f"请求头名称不合法：{name[:40]}")
        text = str(value)
        if len(text) > MAX_HEADER_VALUE or any(ord(char) < 32 for char in text):
            raise MCPError(f"请求头 {name} 的值不合法")
        result[name] = text
        if len(result) > MAX_ARG_COUNT:
            raise MCPError(f"请求头最多 {MAX_ARG_COUNT} 个")
    return result


def _rpc_error(error: dict) -> str:
    """把 JSON-RPC 错误变成一行可读消息，保留错误码供上层判断能力缺失。"""
    code = error.get("code")
    message = str(error.get("message") or "MCP Server 返回错误")
    return (f"[{code}] {message}" if isinstance(code, int) else message)[:300]


def tool_name(server_id: str, remote: str, taken: set[str]) -> str:
    """拼出模型可见的工具名：安全字符、不超长、全局唯一。"""
    safe_server = re.sub(r"[^A-Za-z0-9_-]", "_", server_id)[:20]
    safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", remote)[:40]
    name = f"{MCP_TOOL_PREFIX}{safe_server}__{safe_tool}"
    digest = hashlib.sha256(f"{server_id}/{remote}".encode("utf-8")).hexdigest()
    # 超长或撞名都用同一段短哈希收尾；撞名时换一段继续试，尽量拿到唯一名字。
    if len(name) > MAX_TOOL_NAME:
        name = name[:MAX_TOOL_NAME - 9] + "_" + digest[:8]
    attempt = 0
    while name in taken and attempt < 8:
        attempt += 1
        name = name[:MAX_TOOL_NAME - 9] + "_" + digest[attempt * 2:attempt * 2 + 8]
    return name


# --------------------------------------------------------------------------- #
#  传输层
# --------------------------------------------------------------------------- #


class _StdioTransport:
    """把子进程的 stdin/stdout 当 JSON-RPC 管道用。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._counter = 0
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._stderr_lines: list[str] = []
        self._closed = False
        self._temporary = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and not self._closed

    @property
    def stderr_tail(self) -> str:
        return " ".join(self._stderr_lines[-3:])[:400]

    async def start(self) -> None:
        argv = [self.server.command, *self.server.args]
        if os.getenv("CARME_CONTAINER_CONTROL") == "1":
            raise MCPError("stdio_denied_in_control: use an isolated executor")
        if not self.server.trusted_host:
            raise MCPError("stdio_requires_isolated_executor: 未隔离的 MCP 不在 Control 自动启动")
        from .security import explicit_child_values
        from .engines import _cli_base_env
        import tempfile
        try:
            explicit = explicit_child_values(self.server.env)
        except ValueError as exc:
            raise MCPError(str(exc)) from exc
        self._temporary = tempfile.TemporaryDirectory(prefix="carme-mcp-")
        env = {**_cli_base_env(self._temporary.name), **explicit}
        Path(env["TMPDIR"]).mkdir()
        cwd = self.server.cwd or self._temporary.name
        if cwd and not Path(cwd).expanduser().is_dir():
            self._temporary.cleanup()
            raise MCPError(f"工作目录不存在：{cwd}")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(cwd).expanduser()) if cwd else None,
                env=env,
                limit=STDIO_LINE_LIMIT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self._temporary.cleanup()
            raise MCPError(f"找不到可执行文件：{self.server.command}（请确认已安装并在 PATH 里）") from exc
        except OSError as exc:
            self._temporary.cleanup()
            raise MCPError(f"无法启动 MCP Server：{type(exc).__name__}") from exc
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:  # pragma: no cover - start() 已保证有管道
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                if len(line) > MAX_JSON_BYTES:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError) as exc:
            # 单行超过 limit 时 readline 抛 ValueError：这条连接已经不可信，
            # 必须标记退出并结束子进程，否则后续每次调用都会白等到超时。
            self._stderr_lines.append(f"输出行超过 {STDIO_LINE_LIMIT // (1024 * 1024)} MB：{type(exc).__name__}")
            del self._stderr_lines[:-MAX_STDERR_LINES]
            self._closed = True
            self._terminate()
        finally:
            self._fail_pending("MCP Server 已退出或断开连接")

    async def _read_stderr(self) -> None:
        stream = self.process.stderr if self.process else None
        if stream is None:  # pragma: no cover
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_lines.append(text[:200])
                    del self._stderr_lines[:-MAX_STDERR_LINES]
        except (asyncio.CancelledError, OSError):
            pass

    def _dispatch(self, message: dict) -> None:
        if "id" not in message:
            return  # 通知：Carme 目前不需要订阅 Server 侧事件
        future = self._pending.pop(message.get("id"), None)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(MCPError(_rpc_error(error)))
        else:
            future.set_result(message.get("result") or {})

    def _fail_pending(self, reason: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(MCPError(reason))
        self._pending.clear()

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise MCPError("MCP Server 进程未运行")
        self._counter += 1
        request_id = self._counter
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self.process.stdin.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._pending.pop(request_id, None)
            raise MCPError("MCP Server 的输入管道已关闭") from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise MCPError(f"MCP Server 在 {int(timeout)} 秒内没有响应 {method}") from None

    async def notify(self, method: str, params: dict) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self.process.stdin.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _terminate(self) -> None:
        """同步杀掉子进程组（不给它留后代）。reader 任务发现异常时也走这里。"""
        process = self.process
        self._closed = True
        if process is None or process.returncode is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, AttributeError):
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.terminate()
            except (ProcessLookupError, OSError):  # pragma: no cover - 进程刚好自己退出
                pass

    async def close(self) -> None:
        self._fail_pending("MCP Server 已断开")
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()
        process = self.process
        self._terminate()
        if process is not None:
            from .engines import _stop_process_group
            await _stop_process_group(process)
            self.process = None
        for task in (self._reader, self._stderr):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


class _HttpTransport:
    """POST JSON-RPC；兼容直接返回 JSON 和 text/event-stream 两种应答。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self._client: httpx.AsyncClient | None = None
        self._counter = 0
        self._session_id = ""
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed

    @property
    def stderr_tail(self) -> str:
        return ""

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(CONNECT_TIMEOUT, connect=20.0),
                                         follow_redirects=False, headers={"User-Agent": "Carme/0.2"})

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   **self.server.headers}
        if self._session_id:
            headers.setdefault("Mcp-Session-Id", self._session_id)
        return headers

    async def request(self, method: str, params: dict, timeout: float) -> dict:
        if self._client is None:
            raise MCPError("MCP HTTP 传输尚未初始化")
        self._counter += 1
        request_id = self._counter
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            response = await self._client.post(self.server.url, json=payload, headers=self._headers(),
                                               timeout=httpx.Timeout(timeout, connect=20.0))
        except httpx.HTTPError as exc:
            raise MCPError(f"连接 MCP Server 失败：{type(exc).__name__}") from exc
        session_id = response.headers.get("mcp-session-id", "").strip()
        if session_id and not self._session_id:
            self._session_id = session_id
        if response.status_code >= 300:
            # 3xx 也当成错误：这里刻意不跟随重定向，免得把请求（含自定义请求头）转发到别处。
            hint = "（重定向未跟随，请直接填写最终地址）" if response.status_code < 400 else ""
            raise MCPError(f"MCP Server 返回 HTTP {response.status_code}{hint}")
        if response.status_code == 202 or not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        raw = response.content
        if len(raw) > MAX_JSON_BYTES:
            raise MCPError("MCP Server 的响应过大")
        if "text/event-stream" in content_type:
            return _parse_sse(raw.decode("utf-8", "replace"), request_id)
        try:
            message = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise MCPError("MCP Server 返回的不是有效 JSON") from exc
        if not isinstance(message, dict):
            raise MCPError("MCP Server 返回格式错误")
        error = message.get("error")
        if isinstance(error, dict):
            raise MCPError(_rpc_error(error))
        return message.get("result") or {}

    async def notify(self, method: str, params: dict) -> None:
        if self._client is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            await self._client.post(self.server.url, json=payload, headers=self._headers(),
                                    timeout=httpx.Timeout(15.0, connect=10.0))
        except httpx.HTTPError:
            pass

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse_sse(text: str, request_id: int) -> dict:
    """从 streamable HTTP 的事件流里挑出属于本次请求的那一条。"""
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
            continue
        if line.strip() == "":
            if not data_lines:
                continue
            chunk = "\n".join(data_lines)
            data_lines = []
            try:
                message = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                error = message.get("error")
                if isinstance(error, dict):
                    raise MCPError(_rpc_error(error))
                return message.get("result") or {}
    if data_lines:
        try:
            message = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            message = None
        if isinstance(message, dict) and message.get("id") == request_id:
            return message.get("result") or {}
    raise MCPError("MCP Server 的事件流里没有本次请求的结果")


# --------------------------------------------------------------------------- #
#  会话与工具代理
# --------------------------------------------------------------------------- #


class MCPSession:
    """一个已建立（或正在建立）的 MCP 连接。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server
        self.transport: _StdioTransport | _HttpTransport = (
            _HttpTransport(server) if server.transport == "http" else _StdioTransport(server)
        )
        self.tools: list[dict] = []
        self.server_info: str = ""
        self.protocol: str = ""
        self.connected = False

    async def start(self) -> list[dict]:
        await self.transport.start()
        result = await self.transport.request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "carme", "version": "0.2"},
        }, CONNECT_TIMEOUT)
        self.protocol = str(result.get("protocolVersion") or "")
        info = result.get("serverInfo") or {}
        if isinstance(info, dict):
            self.server_info = str(info.get("name") or "")[:80] + (
                f" {info.get('version')}" if info.get("version") else "")
        await self.transport.notify("notifications/initialized", {})
        self.tools = await self.list_tools()
        self.connected = True
        return self.tools

    async def list_tools(self) -> list[dict]:
        tools=[];cursor=None;seen=set();names=set()
        for _ in range(100):
            try:
                result=await self.transport.request('tools/list',{'cursor':cursor} if cursor else {},self.server.timeout)
            except MCPError as exc:
                if not tools and ('-32601' in str(exc) or 'Method not found' in str(exc)):return []
                raise
            raw=result.get('tools')
            if not isinstance(raw,list):raise MCPError('invalid_tools_page')
            for item in raw:
                if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not item['name'] or len(item['name'])>200 or item['name'] in names:
                    raise MCPError('invalid_or_duplicate_tool')
                names.add(item['name']);schema=item.get('inputSchema',{'type':'object','properties':{}})
                if not isinstance(schema,dict):raise MCPError('invalid_tool_schema')
                tools.append({'name':item['name'],'description':str(item.get('description',''))[:800],'inputSchema':schema})
            if len(tools)>2000:raise MCPError('tool_catalog_limit')
            cursor=result.get('nextCursor')
            if not cursor:return tools
            if not isinstance(cursor,str) or cursor in seen or len(cursor)>2000:raise MCPError('tools_cursor_loop')
            seen.add(cursor)
        raise MCPError('tools_page_limit')

    async def call(self, remote: str, arguments: dict, timeout: float) -> dict:
        if not self.connected:
            raise MCPError(f"MCP Server「{self.server.label}」尚未连接")
        return await self.transport.request("tools/call", {"name": remote, "arguments": arguments}, timeout)

    async def close(self) -> None:
        self.connected = False
        await self.transport.close()


class MCPTool(Tool):
    """外部 MCP 工具在 Carme 工具表里的替身。"""

    def __init__(self, manager: "MCPManager", server: MCPServer, spec: dict, name: str) -> None:
        self.manager = manager
        self.server_id = server.id
        self.remote = spec["name"]
        self.name = name
        self.description = (f"[MCP·{server.label}] " + (spec.get("description") or ""))[:1024]
        schema = spec.get("inputSchema") or {}
        self.parameters = schema if isinstance(schema, dict) and schema.get("type") == "object" else {
            "type": "object", "properties": {}}

    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        from .approval import ApprovalOutcome

        server = self.manager.server(self.server_id)
        if server is None:
            raise MCPError(f"MCP Server「{self.server_id}」已被移除")
        grant=self.manager.authorize(ctx.agent.id,self.server_id,self.remote,kwargs)
        if server.approval == "confirm":
            outcome: ApprovalOutcome = await ctx.request_approval(
                kind="mcp",
                summary=f"调用外部 MCP 工具 {server.label} · {self.remote}",
                detail={"server": server.label, "tool": self.remote, "target": server.target,
                        "arguments": json.dumps(kwargs, ensure_ascii=False)[:2000]},
            )
            if not outcome.approved:
                return f"[已拒绝] 人工没有批准本次 MCP 调用（{outcome.note or '未说明原因'}）。"
        await ctx.notify("tool.start", {"tool": self.name, "source": "mcp", "server": server.label})
        if server.executor=='action':
            if self.manager.execution is None:raise MCPError('isolated_mcp_executor_required')
            content=await self.manager.execution.submit(ctx.task_id,'action',{'op':'mcp','server':server.to_yaml(),'server_id':server.id,
                'call':{'remote':self.remote,'arguments':dict(kwargs),'grant':grant}})
        else:
            content = await self.manager.call_tool(self.server_id, self.remote, dict(kwargs), timeout=server.timeout,grant=grant)
        text = _content_text(content, server.label, self.remote)
        images = _content_images(content)
        if images:
            ctx.extras.setdefault("images", []).extend(images)
        await ctx.notify("tool.end", {"tool": self.name, "source": "mcp", "chars": len(text)})
        return text


def _content_text(content: Any, server: str, remote: str) -> str:
    blocks = content.get("content") if isinstance(content, dict) else None
    if not isinstance(blocks, list):
        blocks = []
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource, dict):
                parts.append(f"[资源 {resource.get('uri', '')}]\n{str(resource.get('text') or '')[:4000]}")
        elif kind == "image":
            parts.append("[图片结果已作为图像内容附加]")
        elif kind == "audio":
            parts.append("[音频结果暂不支持显示]")
    if isinstance(content, dict) and content.get("isError"):
        prefix = f"[MCP 工具报错] {server} · {remote}："
    else:
        prefix = ""
    body = "\n".join(part for part in parts if part).strip()
    return (prefix + body) if body else (prefix + "(MCP 工具没有返回文本内容)" if prefix else "(MCP 工具没有返回文本内容)")


def _content_images(content: Any) -> list[dict]:
    blocks = content.get("content") if isinstance(content, dict) else None
    images: list[dict] = []
    for block in blocks or []:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        data = str(block.get("data") or "")
        mime = str(block.get("mimeType") or "image/png")
        if not data or len(data) > MAX_IMAGE_BYTES or not mime.startswith("image/") or "/" in mime[6:]:
            continue
        images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return images


# --------------------------------------------------------------------------- #
#  管理器
# --------------------------------------------------------------------------- #


class MCPManager:
    """MCP Server 的注册表、连接池与工具注册。

    进程归 Runtime 所有：一个 Server 一个常驻连接，多个 Bot、多次任务共用。
    """

    def __init__(self, path: Path, registry: "ToolRegistry") -> None:
        self.path = Path(path)
        self.registry = registry
        self._servers: dict[str, MCPServer] = {}
        self._sessions: dict[str, MCPSession] = {}
        self._states: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._registered: dict[str, list[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.grants = {}
        self.catalogs = {}
        self.execution = None
        self._load()

    # ---------------- 持久化 ----------------

    def _load(self) -> None:
        self._servers = {}
        if not self.path.is_file():
            return
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            log.warning("无法读取 %s：%s", self.path, exc)
            return
        if isinstance(raw,dict):
            self.grants=raw.get('grants',{})
            self.catalogs=raw.get('catalogs',{})
        servers = raw.get("servers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            return
        for server_id, entry in list(servers.items())[:MAX_SERVERS]:
            key = str(server_id)
            if not MCP_SERVER_ID_PATTERN.match(key) or not isinstance(entry, dict):
                log.warning("跳过不合法的 MCP Server 配置：%s", key[:40])
                continue
            self._servers[key] = MCPServer.from_yaml(key, entry)

    def _save(self) -> None:
        from .config import atomic_write

        payload = {"version": 1, "grants":self.grants, "catalogs":self.catalogs,
                   "servers": {server_id: server.to_yaml() for server_id, server in sorted(self._servers.items())}}
        try:
            atomic_write(self.path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"))
            self.path.chmod(0o600)
        except (OSError, ValueError) as exc:
            # 符号链接或只读目录：变成界面能读的报错，而不是 500。
            raise MCPError(f"无法写入 {self.path.name}：{exc}") from exc

    # ---------------- 查询 ----------------

    def server(self, server_id: str) -> MCPServer | None:
        return self._servers.get(server_id)

    def servers(self) -> list[MCPServer]:
        return [self._servers[key] for key in sorted(self._servers)]

    def status(self, server_id: str) -> str:
        server = self._servers.get(server_id)
        if server is None:
            return "missing"
        if not server.enabled:
            return "disabled"
        if server_id in self._states:
            return self._states[server_id]
        return "stopped"

    def tool_specs(self, server_id: str) -> list[dict]:
        session = self._sessions.get(server_id)
        specs=session.tools if session else self.catalogs.get(server_id,[])
        return [{"name": spec["name"], "description": spec["description"], "schema_hash":__import__("carme.security",fromlist=["digest"]).digest(spec["inputSchema"]),
                 "registered": self._registered_name(server_id, spec["name"])}
                for spec in specs]

    def _registered_name(self, server_id: str, remote: str) -> str:
        for name in self._registered.get(server_id, []):
            tool = self.registry.get(name)
            if isinstance(tool, MCPTool) and tool.remote == remote:
                return name
        return ""

    def tool_names(self) -> list[str]:
        """注册表里当前所有 MCP 工具名 —— 供工具分组 mcp 展开。"""
        names: list[str] = []
        for server_id in self._servers:
            names.extend(self._registered.get(server_id, []))
        return sorted(names)

    def public_servers(self) -> list[dict]:
        result = []
        for server in self.servers():
            session = self._sessions.get(server.id)
            result.append(server.public(status=self.status(server.id), tools=self.tool_specs(server.id),
                                        error=self._errors.get(server.id, ""),
                                        server_info=session.server_info if session else "",
                                        protocol=session.protocol if session else ""))
        return result

    def public_server(self, server_id: str) -> dict:
        server = self._servers.get(server_id)
        if server is None:
            raise MCPError("MCP Server 不存在")
        session = self._sessions.get(server_id)
        return server.public(status=self.status(server_id), tools=self.tool_specs(server_id),
                             error=self._errors.get(server_id, ""),
                             server_info=session.server_info if session else "",
                             protocol=session.protocol if session else "")

    # ---------------- 增删改 ----------------

    async def upsert(self, payload: dict, *, server_id: str = "") -> dict:
        """新增或更新一个 Server 定义；enabled 的 Server 会立即尝试连接。"""
        key = str(payload.get("id") or server_id or "").strip()
        if not MCP_SERVER_ID_PATTERN.match(key):
            raise MCPError("Server id 只能使用字母数字、下划线和短横线，且以字母数字开头（1-48 位）")
        existing = self._servers.get(key)
        if existing is None and len(self._servers) >= MAX_SERVERS:
            raise MCPError(f"最多只能配置 {MAX_SERVERS} 个 MCP Server")
        transport = "http" if str(payload.get("transport") or "stdio") == "http" else "stdio"
        name = _clean_text(str(payload.get("name") or key), 80) or key
        approval = "confirm" if str(payload.get("approval") or "auto") == "confirm" else "auto"
        timeout = _clamp_timeout(payload.get("timeout", existing.timeout if existing else DEFAULT_TIMEOUT))
        enabled = bool(payload.get("enabled", True if existing is None else existing.enabled))
        server = MCPServer(id=key, name=name, transport=transport, enabled=enabled, approval=approval,
                          timeout=timeout,
                          source=str(payload.get("source") or (existing.source if existing else "手动添加"))[:200],
                          installed_at=existing.installed_at if existing else time.time(),
                          trusted_host=payload.get("trusted_host", existing.trusted_host if existing else False) is True)
        if transport == "stdio":
            server.command = _clean_text(str(payload.get("command") or ""), 400, allow_empty=False)
            raw_args = payload.get("args")
            if isinstance(raw_args, str):
                raw_args = [line for line in raw_args.splitlines() if line.strip()]
            if raw_args is None:
                raw_args = existing.args if existing else []
            if not isinstance(raw_args, list) or len(raw_args) > MAX_ARG_COUNT:
                raise MCPError(f"参数最多 {MAX_ARG_COUNT} 个")
            server.args = [_clean_text(str(item), 400) for item in raw_args]
            env = payload.get("env")
            if isinstance(env, str):
                env = parse_env_text(env)
            if env is None and existing is not None:
                env = existing.env
            keep_env = existing.env if (existing is not None and payload.get("merge_env", True)) else {}
            server.env = _clean_env({**keep_env, **dict(env or {})})
            server.cwd = _clean_text(str(payload.get("cwd") or ""), 500)
        else:
            server.url = _clean_text(str(payload.get("url") or ""), 2048, allow_empty=False)
            parts = urlsplit(server.url)
            if parts.scheme not in {"http", "https"} or not parts.netloc:
                raise MCPError("HTTP 传输需要以 http:// 或 https:// 开头的地址")
            if parts.username or parts.password:
                raise MCPError("地址里不能带用户名或密码，请改用自定义请求头")
            headers = payload.get("headers")
            if isinstance(headers, str):
                headers = parse_env_text(headers)
            if headers is None and existing is not None:
                headers = existing.headers
            keep_headers = existing.headers if (existing is not None and payload.get("merge_env", True)) else {}
            server.headers = _clean_headers({**keep_headers, **dict(headers or {})})
        server.executor='action' if payload.get('executor',existing.executor if existing else '')=='action' else ''
        if server.executor=='action' and server.transport!='stdio':raise MCPError('action_mcp_requires_stdio')
        self._servers[key] = server
        self._save()
        if server.enabled:
            try:
                await self.connect(key)
            except MCPError:
                # 定义已经存下来了：连接失败只是当下连不上（没装、网络不通、需要登录），
                # 界面会带着 error 显示，人工改完再点重连即可。
                pass
        else:
            await self.disconnect(key)
        return self.public_server(key)

    async def remove(self, server_id: str) -> None:
        await self.disconnect(server_id)
        self._servers.pop(server_id, None)
        self._states.pop(server_id, None)
        self._errors.pop(server_id, None)
        self._save()

    async def set_enabled(self, server_id: str, enabled: bool) -> dict:
        server = self._servers.get(server_id)
        if server is None:
            raise MCPError("MCP Server 不存在")
        server.enabled = bool(enabled)
        self._save()
        if server.enabled:
            try:
                await self.connect(server_id)
            except MCPError:
                pass
        else:
            await self.disconnect(server_id)
        return self.public_server(server_id)

    # ---------------- 连接 ----------------

    def _lock(self, server_id: str) -> asyncio.Lock:
        if server_id not in self._locks:
            self._locks[server_id] = asyncio.Lock()
        return self._locks[server_id]

    async def connect(self, server_id: str) -> dict:
        server = self._servers.get(server_id)
        if server is None:
            raise MCPError("MCP Server 不存在")
        if not server.enabled:
            raise MCPError("该 MCP Server 已停用")
        if server.executor=='action':
            self._states[server_id]='error'
            self._errors[server_id]='stdio_requires_isolated_executor: Action target and paired Broker required'
            if self.execution is None:raise MCPError(self._errors[server_id])
            runtime=self.execution.runtime
            spec=next((s for s in runtime.config.agents.agents.values() if s.execution_target=='container'),None)
            if spec is None:raise MCPError(self._errors[server_id])
            node,snapshot=runtime._task_agent_snapshot(spec.id)
            tid=runtime.store.create_task(spec.id,'MCP 工具目录检查',source='mcp-discovery',meta={'node':node,**snapshot})
            try:
                result=await self.execution.submit(tid,'action',{'op':'mcp','server':server.to_yaml(),'server_id':server.id,'call':None})
                self.catalogs[server.id]=result['tools'];self._register(server,None,result['tools'])
                self._states[server.id]='connected';self._errors.pop(server.id,None);self._save();runtime.store.finish_task(tid,'隔离发现完成')
            except Exception as exc:
                runtime.store.finish_task(tid,'隔离发现失败',status='failed')
                self._errors[server.id]='isolated_mcp_unavailable: '+str(exc)[:200]
                raise MCPError(self._errors[server.id]) from None
            return self.public_server(server_id)
        async with self._lock(server_id):
            await self._close_session(server_id)
            self._states[server_id] = "connecting"
            self._errors.pop(server_id, None)
            session = MCPSession(server)
            try:
                tools = await session.start()
            except BaseException as exc:
                # 包括 CancelledError（关闭时后台连接被掐）：一定要收掉已经拉起来的子进程，
                # 否则它会以 start_new_session 自成一组活过后端退出。
                detail = str(exc) if isinstance(exc, MCPError) else f"{type(exc).__name__}: {exc}"
                tail = session.transport.stderr_tail if str(detail) else ""
                await session.close()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                self._states[server_id] = "error"
                self._errors[server_id] = (detail + (f"（{tail}）" if tail else ""))[:300]
                raise MCPError(self._errors[server_id]) from exc
            self._sessions[server_id] = session
            self._states[server_id] = "connected"
            self._register(server, session, tools)
            log.info("MCP Server 已连接：%s（%d 个工具）", server.label, len(tools))
            return self.public_server(server_id)

    async def disconnect(self, server_id: str) -> None:
        async with self._lock(server_id):
            await self._close_session(server_id)
        if server_id in self._states and self._states[server_id] != "error":
            self._states[server_id] = "stopped"

    async def _close_session(self, server_id: str) -> None:
        self._unregister(server_id)
        session = self._sessions.pop(server_id, None)
        if session is not None:
            await session.close()

    def _register(self, server: MCPServer, session: MCPSession, tools: list[dict]) -> None:
        self._unregister(server.id)
        # 本服务器的旧名字已经在上一步注销；这里用全量名字表，防止两个服务器算出同一个名字。
        taken = set(self.registry.names())
        names: list[str] = []
        for spec in tools:
            name = tool_name(server.id, spec["name"], taken | set(names))
            taken.add(name)
            self.registry.register(MCPTool(self, server, spec, name))
            names.append(name)
        self._registered[server.id] = names

    def _unregister(self, server_id: str) -> None:
        for name in self._registered.pop(server_id, []):
            self.registry.unregister(name)

    async def call_tool(self, server_id: str, remote: str, arguments: dict, *, timeout: float = DEFAULT_TIMEOUT, grant=None) -> dict:
        if not isinstance(arguments, dict):
            raise MCPError("MCP 工具参数必须是对象")
        async with self._lock(server_id):
            server = self._servers.get(server_id)
            if server is None:
                raise MCPError("MCP Server 不存在")
            session = self._sessions.get(server_id)
            if session is not None and not session.transport.alive:
                # 子进程已经死了（崩溃、被系统回收、输出失控）：先把死连接和它的工具摘掉。
                await self._close_session(server_id)
                session = None
            if session is None:
                if not server.enabled:
                    raise MCPError(f"MCP Server「{server.label}」已停用")
                # 按需重连一次；失败必须收掉已经拉起来的进程。
                session = MCPSession(server)
                try:
                    tools = await session.start()
                except BaseException:
                    await session.close()
                    self._states[server_id] = "error"
                    self._errors[server_id] = "MCP Server 重连失败"
                    raise
                self._sessions[server_id] = session
                self._states[server_id] = "connected"
                self._errors.pop(server_id, None)
                self._register(server, session, tools)
            try:
                fresh=await session.list_tools()
                if grant:check_grant(server,fresh,remote,arguments,grant)
                return await session.call(remote, arguments, timeout)
            except MCPError as exc:
                self._states[server_id] = "error"
                self._errors[server_id] = str(exc)[:300]
                raise

    # ---------------- 生命周期 ----------------

    def grant(self, bot_id, server_id, remote, *, argument_allowlist, revoke=False):
        from .security import digest
        if revoke:
            self.grants.get(bot_id,{}).get(server_id,{}).pop(remote,None)
            self._save();return {}
        server=self.server(server_id)
        specs=self._sessions[server_id].tools if server_id in self._sessions else self.catalogs.get(server_id,[])
        spec=next((s for s in specs if s['name']==remote),None)
        if not server or not spec:raise MCPError('tool_discovery_required')
        validate_schema(spec['inputSchema'],None,definition=True)
        properties=spec['inputSchema'].get('properties',{})
        # Bind compound arguments as complete values: nested paths must not escape a top-level grant.
        resource_keys={k for k,v in properties.items() if v.get('type') in {'object','array'} or v.get('format') in {'uri','uri-reference'} or re.search(r'(path|file|url|uri|resource|directory)',k,re.I)}
        if not isinstance(argument_allowlist,dict) or not resource_keys<=set(argument_allowlist) or set(argument_allowlist)-set(properties):
            raise MCPError('explicit_resource_argument_grant_required')
        if any(not isinstance(v,list) or not v or len(v)>100 for v in argument_allowlist.values()):raise MCPError('invalid_resource_allowlist')
        for key,values in argument_allowlist.items():
            for value in values:validate_schema(properties[key],value)
        grant={'server_hash':digest(server.to_yaml()),'schema_hash':digest(spec['inputSchema']),
               'argument_allowlist':argument_allowlist}
        grants=self.grants.setdefault(bot_id,{}).setdefault(server_id,{})
        if revoke:grants.pop(remote,None)
        else:grants[remote]=grant
        self._save();return grant

    def authorize(self, bot_id, server_id, remote, arguments):
        grant=self.grants.get(bot_id,{}).get(server_id,{}).get(remote)
        if not grant:raise MCPError('mcp_tool_not_granted')
        server=self.server(server_id)
        specs=self._sessions[server_id].tools if server_id in self._sessions else self.catalogs.get(server_id,[])
        check_grant(server,specs,remote,arguments,grant)
        return grant

    async def start_all(self) -> None:
        """后端启动时把 enabled 的 Server 连上；单个失败不影响其它。"""
        for server in self.servers():
            if not server.enabled:
                self._states[server.id] = "disabled"
                continue
            try:
                await self.connect(server.id)
            except MCPError as exc:
                log.warning("MCP Server 连接失败：%s（%s）", server.label, exc)

    async def close_all(self) -> None:
        for server_id in list(self._sessions):
            await self._close_session(server_id)
            self._states[server_id] = "stopped"

    def shutdown_sync_state(self) -> None:
        self._states = {server_id: "stopped" for server_id in self._servers}


def parse_env_text(text: str) -> dict[str, str]:
    """把界面上的「KEY=value 每行一条」解析成字典。"""
    result: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise MCPError(f"这一行缺少等号：{line[:40]}")
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result
