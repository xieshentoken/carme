"""Shared authentication and subprocess boundaries; child identities stay explicit."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import json
import os
import re
import stat
import time
from pathlib import Path

NETWORK_ENV = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                         "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"})
RESERVED_ENV = frozenset({"HOME", "PATH", "TMPDIR", "TMP", "TEMP", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH",
                         "PYTHONHOME", "BASH_ENV", "ENV", "ZDOTDIR", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
                         "GPG_AGENT_INFO", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "LD_PRELOAD",
                         "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "PI_CODING_AGENT_DIR"})


TASK_INTERRUPTED = "任务暂未完成。可以稍后从检查点继续；如需调整要求，请发送新消息。"


def execution_diagnostic(root: Path, stage: str, *, task_id="", job_id="", error=None, **fields):
    """Private, bounded operational metadata. Never accept prompts, URLs or exception text."""
    try:
        row = {"time": time.time(), "stage": stage, "task_id": task_id, "job_id": job_id}
        if error is not None:
            row["error_class"] = type(error).__name__
        allowed = {"status", "attempt", "elapsed_ms", "first_byte_ms", "bytes", "chunks",
                   "retry_after", "success", "delay_ms", "deadline_remaining", "code"}
        for key, value in fields.items():
            if key in allowed and (type(value) in (int, float, bool) or
                    isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value)):
                row[key] = value
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{0,128}", str(row[k]))
               for k in ("stage", "task_id", "job_id", *(["error_class"] if error is not None else []))):
            return
        root = Path(root) / "diagnostics"
        if root.is_symlink():
            return
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / "execution.jsonl"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return
        # This process is the sole writer in its Control/Broker directory.
        if path.exists() and path.stat().st_size >= 1024 * 1024:
            path.replace(root / "execution.previous.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                os.fchmod(fd, 0o600)
                os.write(fd, (json.dumps(row, ensure_ascii=True, allow_nan=False) + "\n").encode())
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        pass  # Diagnostics must never fail or delay the task itself.


def explicit_child_values(values: dict) -> dict[str, str]:
    """MCP credentials may be explicitly granted; process identity/injection settings may not."""
    for key, value in values.items():
        if (key in RESERVED_ENV or key.startswith(("CARME_", "XDG_", "DYLD_", "LD_", "GIT_", "NPM_CONFIG_", "PIP_"))
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(value, str) or "\x00" in value):
            raise ValueError("reserved_child_environment_variable:" + key)
    return dict(values)


def child_env(home: str | Path, *, path: str = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
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


def password_hash(password: str, salt: str | None = None) -> str:
    if not isinstance(password, str) or not 15 <= len(password) <= 128:
        raise ValueError("密码需要 15–128 个字符")
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**17,
                            r=8, p=1, maxmem=256*1024*1024, dklen=32)
    return "scrypt$131072$8$1$" + salt + "$" + result.hex()


def password_matches(password: str, stored: str) -> bool:
    try:
        if not stored.startswith("scrypt$131072$8$1$"):
            return False
        # Invalid lengths still incur the same expensive verification.
        valid = isinstance(password, str) and 15 <= len(password) <= 128
        actual = password_hash(password if valid else "invalid-password-placeholder", stored.split("$")[4])
        return hmac.compare_digest(actual, stored) and valid
    except (ValueError, TypeError):
        return False


def new_visitor_password() -> tuple[str, str]:
    """Trusted owner provisioning only: return once; persist only the verifier."""
    password = secrets.token_urlsafe(24)
    return password, password_hash(password)


def visitor_key(token: str) -> bytes:
    return hmac.new(token.encode(), b"carme-visitor-transport-v1", hashlib.sha256).digest()


def visitor_proof(token: str, *, account: str, instance: str, method: str,
                  target: str, body: bytes, secret: str, subject: str, access_expires: float, account_version: int) -> str:
    import jwt
    now = time.time()
    return jwt.encode({"iss": "carme-gateway", "aud": "carme-visitor-control",
        "account": account, "instance": instance, "account_version": account_version, "method": method, "target": target,
        "body": hashlib.sha256(body).hexdigest(), "session": hashlib.sha256(secret.encode()).hexdigest(),
        "sub": subject, "access_exp": access_expires, "iat": now, "exp": min(now + 30, access_expires)},
        visitor_key(token), algorithm="HS256")


def verify_visitor_proof(encoded: str, token: str, *, account: str, instance: str,
                         method: str, target: str, body: bytes, secret: str) -> dict:
    import jwt
    try:
        if not token or not account or not instance or len(encoded) > 4096:
            raise ValueError()
        if jwt.get_unverified_header(encoded) != {"alg": "HS256", "typ": "JWT"}:
            raise ValueError()
        claims = jwt.decode(encoded, visitor_key(token), algorithms=["HS256"],
            issuer="carme-gateway", audience="carme-visitor-control",
            options={"require": ["exp", "iat", "sub", "access_exp", "account_version"]})
        expected = {"iss": "carme-gateway", "aud": "carme-visitor-control",
            "account": account, "instance": instance, "method": method, "target": target,
            "body": hashlib.sha256(body).hexdigest(), "session": hashlib.sha256(secret.encode()).hexdigest()}
        if any(claims.get(k) != v for k, v in expected.items()):
            raise ValueError()
        if (type(claims["account_version"]) is not int or claims["account_version"] < 1
                or not isinstance(claims["sub"], str) or not re.fullmatch(r"[0-9a-f]{64}", claims["sub"])
                or any(type(claims[k]) not in (int, float) for k in ("iat", "exp", "access_exp"))
                or not time.time() < claims["exp"] <= claims["iat"] + 30
                or not claims["exp"] <= claims["access_exp"]):
            raise ValueError()
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("visitor_transport_denied") from None


def visitor_route(method: str, path: str) -> tuple[str, str] | None:
    """One exact allowlist shared by both authentication boundaries."""
    match = re.fullmatch(r"/api/visitor/([a-z][a-z0-9_-]{0,31})/(.+)", path)
    if not match:
        return None
    account, operation = match.groups()
    if (method, operation) in {('POST', 'login'), ('GET', 'session'), ('DELETE', 'session')}:
        return account, operation
    if method == 'POST' and re.fullmatch(r'conversations/c_[a-f0-9]{12}/messages', operation):
        return account, 'write'
    if method != 'GET':
        return None
    if operation == 'conversations':
        return account, 'read'
    if re.fullmatch(r"conversations/c_[a-f0-9]{12}(?:/(?:messages(?:/m_[a-f0-9]{12})?|tasks/t_[a-f0-9]{12}|attachments/f_[a-f0-9]{32}(?:/download)?|avatars/[a-zA-Z0-9_-]{1,96}|events))?", operation):
        return account, 'read'
    return None
