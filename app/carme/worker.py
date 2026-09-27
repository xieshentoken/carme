"""Fixed image entrypoint. All arbitrary commands execute only in Action containers."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

from .engines import CliEngineError, _redact, _run_cli_process, _safe_line
from .security import bounded_output, child_env

# The Broker pings {"type":"lease"} after every successful renewal, so this backstop only
# catches a Broker that is alive but silent — a stalled Control. A dead Broker is noticed
# immediately by stdin EOF in messages(). One renewal may legitimately wait up to
# LEASE_CALL_TIMEOUT in carme.execution, so this has to outlast that stall, otherwise the
# container kills a job whose tools are still working. The task deadline stays the hard bound.
LEASE_SILENCE_SECONDS = 90


def output(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def action(payload, limit):
    if payload['op']=='mcp':
        from .mcp import MCPServer, MCPSession, check_grant
        server=MCPServer.from_yaml(payload['server_id'],payload['server'])
        session=MCPSession(server)
        # This process is the fixed Action image entrypoint, with no host mounts or network.
        session.server.trusted_host=True
        try:
            tools=await session.start();session.server.trusted_host=False
            if payload['call'] is None:return {'tools':tools}
            call=payload['call']
            check_grant(server,tools,call['remote'],call['arguments'],call['grant'])
            return await session.call(call['remote'],call['arguments'],server.timeout)
        finally:await session.close()
    if payload['op']=='stage_input':
        import hashlib
        raw=(Path('/inputs')/payload['name']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=payload['sha256']:raise ValueError('input_hash_conflict')
        return {'path':'/inputs/'+payload['name'],'sha256':payload['sha256'],'read_only':True}
    if payload['op']=='validate_artifact':
        import hashlib
        from .attachments import validate_binary
        raw=base64.b64decode(payload['bytes'],validate=True)
        if hashlib.sha256(raw).hexdigest()!=payload['sha256']:raise ValueError('artifact_hash_conflict')
        return validate_binary(payload['name'],raw,payload['checks'])
    if payload["op"] == "snapshot":
        from .projects import materialize
        return materialize(payload["bundle"], Path("/workspace"))
    if payload["op"] == "export":
        # openat + O_NOFOLLOW for every component; reject FIFOs/devices and symlink escapes.
        path = Path(payload["path"])
        if not path.is_absolute():
            path = Path("/workspace") / path
        if path.parts[1] not in {"workspace", "out"} or any(p in {".", ".."} for p in path.parts):
            raise ValueError("artifact_path_denied")
        descriptors = []
        try:
            fd = os.open("/" + path.parts[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            for component in path.parts[2:-1]:
                fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 10 * 1024 * 1024:
                raise ValueError("artifact_size_or_type_denied")
            raw = bytearray()
            while chunk := os.read(fd, 65536):
                raw.extend(chunk)
                if len(raw) > 10 * 1024 * 1024:
                    raise ValueError("artifact_size_denied")
            return {"bytes": base64.b64encode(raw).decode()}
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
    if payload["op"] != "exec":
        raise ValueError("action_operation_denied")
    cwd = Path(payload["cwd"] or "/workspace")
    if not cwd.is_absolute():
        cwd = Path("/workspace") / cwd
    cwd = cwd.resolve()
    if not any(cwd == p or cwd.is_relative_to(p) for p in map(Path, ("/workspace", "/out", "/inputs"))):
        raise ValueError("action_cwd_denied")
    env = child_env("/runtime/home")
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec("/bin/sh", "-c", payload["command"], cwd=cwd,
        env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    raw, err = await bounded_output(proc, timeout=min(float(payload["timeout"]), 600), limit=limit)
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": raw.decode(errors="replace"), "stderr": err.decode(errors="replace")}


async def pi(payload, rpc, *, timeout=600):
    """Real pinned Pi with a local HTTP facade; long-lived credentials stay in Control."""
    proxy_token = secrets.token_urlsafe(32)
    clients = set()
    async def client(reader, writer):
        clients.add(asyncio.current_task())
        sent_headers = False
        ended = False
        async def frame(data):
            nonlocal sent_headers, ended
            kind = data.get('type')
            if kind == 'accepted': return
            if ended: raise RuntimeError('model_frame_after_end')
            if kind == 'headers':
                if sent_headers or data['content_type'] not in {'text/event-stream','application/json'}:
                    raise RuntimeError('model_headers_invalid')
                status = int(data['status'])
                if not 200 <= status <= 599: raise RuntimeError('model_status_invalid')
                writer.write((f"HTTP/1.1 {status} Model response\r\nContent-Type: " + data['content_type'] +
                    "\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode())
                sent_headers = True
            elif kind == 'chunk':
                if not sent_headers: raise RuntimeError('model_headers_required')
                raw = base64.b64decode(data['body'], validate=True)
                if not 0 < len(raw) <= 32768: raise RuntimeError('model_chunk_invalid')
                writer.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
            elif kind == 'end':
                if not sent_headers: raise RuntimeError('model_headers_required')
                writer.write(b"0\r\n\r\n"); ended = True
            elif kind == 'error':
                # A truncated stream must remain truncated: never turn it into a successful answer.
                if not sent_headers:
                    status = 503 if data.get('retryable') else 400
                    raw = json.dumps({'error':{'message':'Service unavailable' if status == 503 else 'Request denied'}}).encode()
                    writer.write(f"HTTP/1.1 {status} Model error\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode()+raw)
                    sent_headers = True
                ended = True
                writer.close()
            else:
                raise RuntimeError('model_frame_denied')
            if not writer.is_closing():
                async with asyncio.timeout(30): await writer.drain()
        try:
            async with asyncio.timeout(timeout):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("latin1").split("\r\n")
                headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
                headers = {k.lower(): v.strip() for k, v in headers.items()}
                size = int(headers.get("content-length", "0"))
                if (lines[0] != "POST /v1/chat/completions HTTP/1.1" or not 0 < size < 1024 * 1024
                        or headers.get("authorization") != "Bearer " + proxy_token or "transfer-encoding" in headers):
                    raise ValueError("model_proxy_request_denied")
                body = json.loads(await reader.readexactly(size))
                await rpc("model", {"body": body}, on_frame=frame)
                if not ended: raise RuntimeError('model_stream_incomplete')
        except Exception:
            if not sent_headers:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                with contextlib.suppress(Exception): await writer.drain()

        finally:
            clients.discard(asyncio.current_task())
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(client, "127.0.0.1", 0, limit=65536)
    model_id = payload["model"].split("/", 1)[1]
    limits = payload.get('model_limits', {'context_window': 32768, 'max_output_tokens': 8192})
    models = {"providers": {"carme": {"baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        "api": "openai-completions", "apiKey": proxy_token,
        "models": [{"id": model_id, "name": model_id, "reasoning": False, "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": limits['context_window'], "maxTokens": limits['max_output_tokens']}]}}}
    async def execute(name, arguments, tool_call_id=''):
        return await rpc('tool', {'name': name, 'arguments': arguments,
                                 **({'tool_call_id': tool_call_id} if tool_call_id else {})})
    previous = ''
    async def stream(event):
        nonlocal previous
        content = event['content']
        replace = not content.startswith(previous)
        delta = content if replace else content[len(previous):]
        previous = content
        return await rpc('stream', {**event, 'content': delta, 'replace': replace})
    try:
        result = await _run_cli_process("pi", payload["prompt"],
            binary="/opt/pi/node_modules/.bin/pi", model="carme/" + model_id,
            effort=payload["effort"], timeout=timeout, tool_specs=payload["tools"],
            tool_execute=execute,
            max_tool_calls=payload["max_tool_calls"], on_stream=stream,
            on_diagnostic=lambda event: rpc('diagnostic', event),
            runtime_models=models,
            native_context={'task_id': payload['session']['task_id'], 'messages': payload['messages'],
                            'system_prompt': payload['system_prompt'], 'session_dir': '/session',
                            'model_limits': limits} if payload.get('session') else None)
        return {"text": result.text, 'context_summary': result.context_summary}
    finally:
        server.close()
        pending = list(clients)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    job = json.loads(await reader.readline())
    pending = {}
    rpc_lock = asyncio.Semaphore(24 if job['role'] == 'browser' else 1)
    model_lock = asyncio.Semaphore(1)
    last_lease = time.monotonic()

    async def rpc(kind, payload, on_frame=None):
        async with (model_lock if kind == 'model' else rpc_lock):
            call_id = secrets.token_hex(16)
            future = loop.create_future()
            pending[call_id] = (future, on_frame)
            try:
                output({"type": "rpc", "id": call_id, "kind": kind, "payload": payload})
                return await future
            finally:
                pending.pop(call_id, None)

    async def messages():
        nonlocal last_lease
        while line := await reader.readline():
            response = json.loads(line)
            if response == {"type": "lease"}:
                last_lease = time.monotonic()
            elif response.get("id") in pending:
                future, on_frame = pending[response['id']]
                if future.done(): raise RuntimeError('duplicate_relay_result')
                if 'frame' in response and on_frame:
                    await on_frame(response['frame'])
                    if response['frame'].get('type') in {'end','error'}: future.set_result(None)
                elif "error" in response:
                    future.set_exception(RuntimeError(response["error"]))
                else:
                    future.set_result(response["result"])
            else:
                raise RuntimeError("relay_protocol_denied")
        raise RuntimeError("broker_disconnected")

    async def watchdog():
        while True:
            await asyncio.sleep(1)
            if time.monotonic() - last_lease > LEASE_SILENCE_SECONDS:
                raise RuntimeError("worker_lease_expired")

    if job['role'] == 'browser':
        from .docker_browser import serve
        work = serve(job['payload'], rpc)
    elif job['role'] == 'pi':
        work = pi(job['payload'], rpc, timeout=max(.1, job.get('deadline', time.time()+600)-time.time()))
    elif job['role'] == 'action':
        work = action(job['payload'], job['max_output_bytes'])
    else:
        raise ValueError('worker_role_denied')
    operation = asyncio.create_task(work)
    tasks = [operation, asyncio.create_task(messages()), asyncio.create_task(watchdog())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        output({"type": "result", "result": operation.result()})
    except Exception as exc:
        # Action errors carry no secret environment; Pi/provider errors are deliberately generic.
        safe_codes={'mcp_schema_or_identity_changed','mcp_resource_denied','mcp_argument_schema_denied',
                    'mcp_argument_fields_denied','schema_constraint_unsupported','invalid_pdf','unsafe_archive_member',
                    'archive_expansion_limit','nested_archive_unsupported','artifact_hash_conflict','input_hash_conflict',
                    'document_external_reference_denied','document_active_content_denied'}
        message=str(exc)
        if message in safe_codes:
            detail=':'+message
        elif isinstance(exc, CliEngineError) and message.strip():
            # engines.py already redacts secrets and strips control characters. This is the only
            # place a CLI failure explains itself (non-zero CLI exit or provider error event);
            # without it every engine fault reaches the UI as an opaque "worker_failed".
            detail=': '+_safe_line(_redact(message))
        else:
            detail=''
        output({"type": "result", "result": {"error": "worker_failed:" + type(exc).__name__+detail}})
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()


if __name__ == "__main__":
    os.environ["TMPDIR"] = "/runtime"
    asyncio.run(main())
