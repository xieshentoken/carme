#!/usr/bin/env python3
"""8899 -> 8898 本机回环转发（2026-09-20 域名切换过渡）。

背景：carme.example.com 的 Tunnel ingress 在 Cloudflare 云端托管，
当前仍指向 http://localhost:8899，而统一入口 Gateway 在 127.0.0.1:8898。
本脚本把 8899 上的原始 TCP 流量原样转发到 8898，使公网域名在不改动
云端配置的情况下打到 Gateway（Access JWT + 账号密码双重登录）。

等云端 ingress 改为 8898 后，停掉本服务并还原旧的 com.carme.serve 即可。
同时监听 127.0.0.1 和 [::1]，避免 macOS 上 localhost 先解析 ::1 的首连失败。
"""

from __future__ import annotations

import asyncio
import sys

LISTEN_PORT = 8899
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8898
IDLE_TIMEOUT = 300.0


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, activity: list[float]) -> None:
    try:
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                # SSE clients are silent while the server sends heartbeats.
                # Only expire a connection when both directions are idle.
                if asyncio.get_running_loop().time() - activity[0] >= IDLE_TIMEOUT:
                    break
                continue
            if not data:
                break
            activity[0] = asyncio.get_running_loop().time()
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(TARGET_HOST, TARGET_PORT), timeout=10.0
        )
    except (asyncio.TimeoutError, OSError):
        try:
            client_writer.close()
        except Exception:
            pass
        return
    activity = [asyncio.get_running_loop().time()]
    tasks = [asyncio.create_task(pipe(client_reader, upstream_writer, activity)),
             asyncio.create_task(pipe(upstream_reader, client_writer, activity))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    servers = []
    for host in ("127.0.0.1", "::1"):
        server = await asyncio.start_server(handle, host=host, port=LISTEN_PORT, backlog=128)
        servers.append(server)
        print(f"forward {host}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except OSError as exc:
        print(f"无法监听 {LISTEN_PORT}: {exc}", file=sys.stderr)
        sys.exit(1)
