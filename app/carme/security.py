"""Shared subprocess boundaries. Never derive a child identity from the parent environment."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

NETWORK_ENV = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                         "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"})
RESERVED_ENV = frozenset({"HOME", "PATH", "TMPDIR", "TMP", "TEMP", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH",
                         "PYTHONHOME", "BASH_ENV", "ENV", "ZDOTDIR", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
                         "GPG_AGENT_INFO", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "LD_PRELOAD",
                         "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "PI_CODING_AGENT_DIR"})


def explicit_child_values(values: dict) -> dict[str, str]:
    """MCP credentials may be explicitly granted; process identity/injection settings may not."""
    for key, value in values.items():
        if (key in RESERVED_ENV or key.startswith(("CARME_", "XDG_", "DYLD_", "LD_", "GIT_", "NPM_CONFIG_", "PIP_"))
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str) or "\x00" in value):
            raise ValueError("reserved_child_environment_variable:" + key)
    return dict(values)


def child_env(home: str | Path, *, path: str = "/usr/local/bin:/usr/bin:/bin",
              network: dict | None = None) -> dict[str, str]:
    home = str(Path(home).absolute())
    env = {"HOME": home, "PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "TMPDIR": home + "/tmp", "TMP": home + "/tmp", "TEMP": home + "/tmp",
           "XDG_CONFIG_HOME": home + "/config", "XDG_CACHE_HOME": home + "/cache",
           "XDG_DATA_HOME": home + "/data", "PI_CODING_AGENT_DIR": home + "/pi"}
    for key, value in (network or {}).items():
        if key not in NETWORK_ENV or not isinstance(value, str) or "\x00" in value:
            raise ValueError("invalid_child_network_env")
        env[key] = value
    return env


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


async def bounded_output(process, *, timeout: float, limit: int) -> tuple[bytes, bytes]:
    """Bound retained bytes while reading; terminate our entire process group on any failure."""
    from .engines import _stop_process_group
    total = 0

    async def read(stream):
        nonlocal total
        chunks = []
        while chunk := await stream.read(min(65536, limit + 1)):
            total += len(chunk)
            if total > limit:
                raise RuntimeError("output_limit_exceeded")
            chunks.append(chunk)
        return b"".join(chunks)

    readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
    try:
        async with asyncio.timeout(timeout):
            out, err = await asyncio.gather(*readers)
            await process.wait()
            return out, err
    except BaseException:
        for reader in readers:
            reader.cancel()
        await _stop_process_group(process)
        await asyncio.gather(*readers, return_exceptions=True)
        raise
