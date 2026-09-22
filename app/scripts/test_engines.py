"""隔离验收 CLI argv、任务桥接、解析器及未知费用标记；不调用真实模型。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["CARME_LOAD_ENV"] = "0"

from carme.engines import (  # noqa: E402
    BRIDGE_TOOL_PREFIX,
    CliEngineError,
    TaskToolBridge,
    _stop_process_group,
    _native_tool_events,
    _redact,
    _run_probe,
    _event_text,
    build_argv,
    format_prompt,
    run_cli_engine,
    _run_cli_process,
)
from carme import engines as engines_module  # noqa: E402
from carme.store import Store  # noqa: E402


def schema(name: str) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": "fixture",
             "parameters": {"type": "object", "properties": {"key": {"type": "string"}}}}}]


async def bridge_check() -> None:
    calls: list[tuple[str, dict]] = []
    large_image = "Z" * (600 * 1024)

    async def execute(name: str, arguments: dict):
        calls.append((name, arguments))
        return {"text": "fixture image", "content": [
            {"type": "text", "text": "fixture image"},
            {"type": "image", "mimeType": "image/jpeg", "data": large_image},
        ]}

    bridge = TaskToolBridge(schema("read_attachment"), execute)
    url = await bridge.start()
    port = bridge._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /tools HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert b"401 Unauthorized" in raw

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"GET /tools HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {bridge.token}\r\n\r\n").encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert b'"name":"read_attachment"' in raw

    request = json.dumps({"name": BRIDGE_TOOL_PREFIX + "read_attachment", "arguments": {"key": "ok"}}, ensure_ascii=False).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write((f"POST /call HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {bridge.token}\r\n"
                  f"Content-Type: application/json\r\nContent-Length: {len(request)}\r\n\r\n").encode() + request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert b'"ok":true' in raw and b'"mimeType":"image/jpeg"' in raw
    assert calls == [("read_attachment", {"key": "ok"})]

    env = os.environ.copy()
    env.update({"CARME_BRIDGE_URL": url, "CARME_BRIDGE_TOKEN": bridge.token,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "carme.bridge_stdio", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        limit=2_000_000,
    )

    async def rpc(request: dict) -> dict:
        child.stdin.write((json.dumps(request) + "\n").encode())
        await child.stdin.drain()
        line = await asyncio.wait_for(child.stdout.readline(), timeout=3)
        return json.loads(line)

    assert (await rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))["result"]["serverInfo"]["name"] == "carme"
    child.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    await child.stdin.drain()
    listed = await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed["result"]["tools"][0]["name"] == "read_attachment"
    called = await rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "read_attachment", "arguments": {"key": "mcp"}}})
    assert called["result"]["content"][1]["type"] == "image"
    assert len(called["result"]["content"][1]["data"]) > 512 * 1024
    child.terminate()
    await child.wait()
    await bridge.close()


async def process_group_check() -> None:
    """A reaped CLI leader must not leave an inherited child behind."""
    with tempfile.TemporaryDirectory(prefix="carme-engine-process-") as directory:
        pidfile = Path(directory) / "child.pid"
        child_code = (
            "import os,signal,sys,time;"
            "from pathlib import Path;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "Path(sys.argv[1]).write_text(str(os.getpid()));"
            "time.sleep(60)"
        )
        parent_code = (
            "import subprocess,sys;"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],"
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", parent_code, child_code, str(pidfile),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        child_pid = None
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
            for _ in range(100):
                if pidfile.exists():
                    child_pid = int(pidfile.read_text())
                    break
                await asyncio.sleep(0.01)
            assert child_pid, "process-group fixture failed to start child"
            assert process.returncode is not None
            await _stop_process_group(process)
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("reaped CLI leader left a child process alive")
        finally:
            if child_pid:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    try:
                        os.kill(child_pid, 9)
                    except ProcessLookupError:
                        pass


async def native_activity_check() -> None:
    """Exercise the complete JSONL reader, not only its event parser."""
    fixtures = {
        "codex": [
            {"type": "item.started", "item": {"id": "c-1", "type": "command_execution", "command": "private"}},
            {"type": "item.completed", "item": {"id": "c-1", "type": "command_execution", "status": "completed"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "codex final"}},
        ],
        "claude": [
            {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {
                "type": "tool_use", "id": "a-1", "name": "Write", "input": {"private": "omit"}}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a-1", "content": "private"}]}},
            {"type": "result", "result": "claude final"},
        ],
        "pi": [
            {"type": "tool_execution_start", "toolCallId": "p-1", "toolName": "write", "args": {"private": "omit"}},
            {"type": "tool_execution_end", "toolCallId": "p-1", "toolName": "write", "isError": False},
            {"type": "message_end", "message": {"stopReason": "stop", "content": [{"type": "text", "text": "pi final"}]}},
        ],
    }
    original_resolve = engines_module.resolve_binary
    original_build = engines_module.build_argv
    secret_env = {key: os.environ.get(key) for key in ("OPENAI_API_KEY", "CARME_MODEL_KEY_SYNTHETIC", "CARME_TOKEN")}
    os.environ["OPENAI_API_KEY"] = "synthetic-not-for-cli"
    os.environ["CARME_MODEL_KEY_SYNTHETIC"] = "synthetic-carme-key"
    os.environ["CARME_TOKEN"] = "synthetic-carme-token"
    try:
        code, _, stderr = _run_probe([sys.executable, "-c", "import os; assert not any(os.environ.get(k) for k in ('OPENAI_API_KEY','CARME_MODEL_KEY_SYNTHETIC','CARME_TOKEN'))"])
        assert code == 0, stderr
        for engine, events in fixtures.items():
            with tempfile.TemporaryDirectory(prefix=f"carme-engine-{engine}-") as directory:
                script = Path(directory) / "fixture.py"
                script.write_text(
                    "import json,sys\n"
                    "import os\n"
                    "assert not any(os.environ.get(key) for key in ('OPENAI_API_KEY','CARME_MODEL_KEY_SYNTHETIC','CARME_TOKEN')), 'Carme secret leaked to CLI'\n"
                    f"events={events!r}\n"
                    "for event in events: print(json.dumps(event), flush=True)\n",
                    encoding="utf-8",
                )
                engines_module.resolve_binary = lambda _engine: sys.executable
                engines_module.build_argv = lambda *args, _script=str(script), **kwargs: [sys.executable, _script]
                seen: list[tuple[str, dict]] = []

                async def on_tool_event(type_: str, payload: dict) -> None:
                    seen.append((type_, payload))

                # Parser/lifecycle fixture only. Public harness admission is tested separately.
                result = await _run_cli_process(engine, "fixture", binary=sys.executable, timeout=5, on_tool_event=on_tool_event)
                assert result.text == f"{engine} final"
                assert [item[0] for item in seen] == ["tool.start", "tool.end"]
                assert all("private" not in json.dumps(item) for item in seen)
    finally:
        engines_module.resolve_binary = original_resolve
        engines_module.build_argv = original_build
        for key, value in secret_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def pure_checks() -> None:
    bridge = TaskToolBridge(schema("recall"), lambda *_: None)
    for engine in ("codex", "claude"):
        try:
            build_argv(engine, "/fixed/" + engine, bridge=bridge)
        except CliEngineError as exc:
            assert "harness_unsupported" in str(exc)
        else:
            raise AssertionError("An unverified harness must not grant native tools")
    pi = build_argv("pi", "/fixed/pi", "fixture/model", bridge=bridge, pi_extension_path="/tmp/trusted-fixture.ts")
    assert "--no-builtin-tools" in pi and "--no-context-files" in pi
    assert "--no-session" in pi and "--no-skills" in pi
    assert pi[pi.index("--tools") + 1] == "carme_recall"
    assert "CARME_BRIDGE_TOKEN" not in json.dumps(pi)

    assert _event_text("claude", {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"text": "hi"}}}) == ("hi", "")
    assert _event_text("claude", {"type": "result", "is_error": True, "result": "bad"}) == ("", "bad")
    assert _event_text("pi", {"type": "message_end", "message": {"stopReason": "error", "errorMessage": "bad"}})[1] == "bad"
    original_key = os.environ.get("CARME_SYNTHETIC_API_KEY")
    os.environ["CARME_SYNTHETIC_API_KEY"] = "oapi-full-secret-value"
    try:
        redacted = _redact(
            "Authorization: Bearer oapi-full-secret-value; "
            "Incorrect API key provided: oapi-synthetic********tail; "
            "masked sk-proj-...tail"
        )
        assert "oapi-full-secret-value" not in redacted
        assert "oapi-synthetic" not in redacted
        assert "sk-proj" not in redacted
        assert "Authorization: Bearer [redacted]" in redacted
    finally:
        if original_key is None:
            os.environ.pop("CARME_SYNTHETIC_API_KEY", None)
        else:
            os.environ["CARME_SYNTHETIC_API_KEY"] = original_key
    assert _event_text("codex", {"type": "item.completed", "item": {"type": "error", "message": "nonfatal"}}) == ("", "")
    assert _event_text("codex", {"type": "turn.failed", "error": "fatal"})[1] == "fatal"
    pending: dict[str, tuple[str, bool]] = {}
    assert _native_tool_events("codex", {"type": "item.started", "item": {
        "id": "cmd-1", "type": "command_execution", "command": "secret --token value"}}, pending, set()) == [
        ("tool.start", "command_execution", False)]
    assert _native_tool_events("codex", {"type": "item.completed", "item": {
        "id": "cmd-1", "type": "command_execution", "status": "completed",
        "command": "secret --token value"}}, pending, set()) == [("tool.end", "command_execution", False)]
    assert _native_tool_events("claude", {"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {
            "type": "tool_use", "id": "m-1", "name": "mcp__carme__recall", "input": {"secret": "omit"}}}},
        pending, {"recall"}) == []
    assert _native_tool_events("claude", {"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": "m-1", "content": "private"}]}}, pending, {"recall"}) == []
    pending = {}
    assert _native_tool_events("pi", {"type": "tool_execution_start", "toolCallId": "p-1", "toolName": "bash",
                                       "args": {"command": "private"}}, pending, set()) == [
        ("tool.start", "bash", False)]
    assert _native_tool_events("pi", {"type": "tool_execution_end", "toolCallId": "p-1", "toolName": "bash",
                                       "isError": True, "result": "private"}, pending, set()) == [
        ("tool.end", "bash", True)]
    try:
        format_prompt([{"role": "user", "content": "x" * 120_000}])
    except CliEngineError:
        pass
    else:
        raise AssertionError("oversized CLI context must fail explicitly")

    with tempfile.TemporaryDirectory(prefix="carme-engine-store-") as directory:
        store = Store(Path(directory) / "carme.db")
        task = store.create_task("fixture", "goal")
        store.add_task_usage(task, 0, 0, cost_known=False, tokens_known=False)
        row = store.get_task(task)
        assert row["cost_known"] == 0 and row["tokens_known"] == 0
        store.finish_task(task, "failed", status="failed", error="bridge failed")
        assert store.get_task(task)["error"] == "bridge failed"
        store.close()


async def main() -> None:
    pure_checks()
    await bridge_check()
    await process_group_check()
    await native_activity_check()
    print("PASS: fixed CLI boundaries, native activity JSONL integration, nested stream errors, task bridge MCP, image content, unknown cost state, and process-group cleanup")


if __name__ == "__main__":
    asyncio.run(main())
