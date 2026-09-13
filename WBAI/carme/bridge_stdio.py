"""MCP stdio relay for one Carme task.

The parent process owns the loopback server and the ToolContext.  This small
process only translates MCP JSON-RPC to that server; it has no credentials,
tool implementation, or permission policy of its own.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

PREFIX = "mcp__carme__"


def _config() -> tuple[str, str]:
    url = os.environ.get("CARME_BRIDGE_URL", "").strip().rstrip("/")
    token = os.environ.get("CARME_BRIDGE_TOKEN", "")
    if not url or not token:
        raise RuntimeError("Carme 工具桥接未配置")
    return url, token


def _http(method: str, path: str, payload: dict | None = None) -> dict:
    url, token = _config()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url + path, data=body, method=method,
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            # Attachment tools may return a validated, resized JPEG as MCP
            # image content.  Bound the response, but do not truncate a
            # legitimate image into invalid JSON at the old 512 KiB limit.
            raw = response.read(12 * 1024 * 1024)
    except (OSError, URLError) as exc:
        raise RuntimeError("Carme 工具桥接连接失败") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Carme 工具桥接返回无效响应") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Carme 工具桥接返回格式错误")
    return data


def _response(request_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle(message: dict) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method == "notifications/initialized" or (request_id is None and str(method).startswith("notifications/")):
        return
    try:
        if method == "initialize":
            _response(request_id, {
                "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "carme", "version": "0.2"},
            })
        elif method == "ping":
            _response(request_id, {})
        elif method == "tools/list":
            data = _http("GET", "/tools")
            _response(request_id, {"tools": data.get("tools") or []})
        elif method == "tools/call":
            name = str(params.get("name") or "")
            # MCP exposes raw names; clients such as Claude/Codex add the
            # server prefix only to the model-facing name.
            if name.startswith(PREFIX):
                name = name[len(PREFIX):]
            if not name or any(char in name for char in "\x00\r\n"):
                raise ValueError("工具不属于 Carme 任务桥接")
            data = _http("POST", "/call", {"name": name, "arguments": params.get("arguments") or {}})
            if data.get("ok"):
                content = data.get("content")
                if not isinstance(content, list):
                    content = [{"type": "text", "text": str(data.get("text") or "")}]
                _response(request_id, {"content": content, "isError": False})
            else:
                _response(request_id, {"content": [{"type": "text", "text": str(data.get("error") or "工具调用失败")}],
                                       "isError": True})
        else:
            _response(request_id, error={"code": -32601, "message": "方法不支持"})
    except Exception as exc:  # Keep protocol errors bounded and never echo bridge credentials.
        _response(request_id, error={"code": -32000, "message": str(exc)[:300]})


def main() -> int:
    for line in sys.stdin:
        if len(line) > 4 * 1024 * 1024:
            _response(None, error={"code": -32600, "message": "请求过大"})
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _response(None, error={"code": -32700, "message": "无效 JSON"})
            continue
        if isinstance(message, dict):
            _handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
