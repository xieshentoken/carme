"""One public origin, password login, and server-selected isolated account backends.

This process owns authentication only. It never opens an account's application DB,
mounts a browser profile, evaluates user code, or accepts a client-selected backend.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .security import password_hash, password_matches, visitor_proof, visitor_route

ACCOUNT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
# 入口登录会话时长：7 天。2026-09-21 由 12 小时延长到 3 天，随后按要求延长到 7 天。
# 实际有效期还会被 Cloudflare Access 会话（请求 JWT 的 exp）截断，见 cloudflare()：
# 必须把 Zero Trust → Access → Applications 里该应用的 Session Duration 设为 7 天或更长。
SESSION_SECONDS = 604800
COOKIE = "carme_login"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@contextlib.contextmanager
def database(home: Path):
    base = home / "gateway"
    if home.resolve() != home or base.resolve() != base:
        raise ValueError("gateway_symlink_denied")
    base.mkdir(mode=0o700, exist_ok=True)
    base.chmod(0o700)
    path = base / "auth.db"
    if path.is_symlink():
        raise ValueError("gateway_symlink_denied")
    db = sqlite3.connect(path, timeout=10)
    path.chmod(0o600)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(home: Path):
    with database(home) as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
            account TEXT PRIMARY KEY, instance TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
            must_change INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL UNIQUE, account TEXT NOT NULL,
            version INTEGER NOT NULL, token_hash TEXT NOT NULL, subject TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account);
        CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
        """)


def account_backend(home: Path, name: str, instance: str | None = None) -> tuple[dict, str]:
    if not ACCOUNT.fullmatch(name):
        raise ValueError("invalid_account")
    base = home / "accounts" / name
    paths = [home / "installation.json", base / "account.json", base / "runtime/secrets/control-token"]
    if any(p.resolve() != p or not p.is_file() for p in paths):
        raise ValueError("account_path_denied")
    installation = json.loads(paths[0].read_text())
    acc = json.loads(paths[1].read_text())
    origin = urlsplit(acc.get("origin", ""))
    if (acc.get("id") != name or acc.get("home") != str(base)
            or acc.get("installation") != installation.get("id")
            or installation.get("home") != str(home)
            or (instance is not None and acc.get("instance_id") != instance)
            or not re.fullmatch(r"carme-[a-f0-9]{20}", acc.get("instance_id", ""))
            or type(acc.get("port")) is not int or not 1024 <= acc["port"] <= 65535
            or origin.scheme != "http" or origin.port != acc["port"]
            or not re.fullmatch(r"c[a-f0-9]{16}\.localhost", origin.hostname or "")
            or origin.path or origin.query or origin.fragment or origin.username or origin.password):
        raise ValueError("account_owner_mismatch")
    token = paths[2].read_text().strip()
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("invalid_account_token")
    return acc, token


def set_password(home: Path, name: str, password: str, *, initial: bool = False):
    acc, _ = account_backend(home, name)
    hashed = password_hash(password)
    initialize(home)
    with database(home) as db:
        if initial and db.execute("SELECT 1 FROM users WHERE account=?", (name,)).fetchone():
            raise ValueError("账号已有登录密码；请使用 login-password 重置")
        changed = db.execute("""INSERT INTO users(account,instance,password,must_change) VALUES(?,?,?,?)
            ON CONFLICT(account) DO UPDATE SET password=excluded.password, enabled=1,
            version=users.version+1,must_change=excluded.must_change
            WHERE users.instance=excluded.instance""", (name, acc["instance_id"], hashed, int(initial))).rowcount
        if not changed:
            raise ValueError("account_owner_mismatch")
        db.execute("DELETE FROM sessions WHERE account=?", (name,))
        db.execute("DELETE FROM attempts WHERE key=?", ("user:"+digest(name),))
    if not initial:
        (home / "accounts" / name / "login-initial.txt").unlink(missing_ok=True)


def session_row(home: Path, secret: str, subject: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
        return None
    with database(home) as db:
        row = db.execute("""SELECT s.*,u.instance,u.must_change FROM sessions s JOIN users u
            ON u.account=s.account AND u.version=s.version
            WHERE s.secret_hash=? AND s.subject=? AND s.expires_at>? AND u.enabled=1""",
            (digest(secret), subject, time.time())).fetchone()
    if not row:
        return None
    try:
        _, token = account_backend(home, row["account"], row["instance"])
    except (ValueError, OSError, KeyError):
        return None
    return dict(row) if hmac.compare_digest(row["token_hash"], digest(token)) else None


def csrf(secret: str) -> str:
    return hmac.new(secret.encode(), b"carme-gateway-csrf-v1", hashlib.sha256).hexdigest()


def throttle(home: Path, name: str, subject: str):
    now = time.time()
    with database(home) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM attempts WHERE expires<=?", (now,))
        for key, limit, seconds in [("all", 40, 60), ("user:"+digest(name), 8, 900),
                                    ("subject:"+subject, 20, 900)]:
            row = db.execute("SELECT count FROM attempts WHERE key=?", (key,)).fetchone()
            if row and row["count"] >= limit:
                raise HTTPException(429, "尝试次数过多，请稍后再试", headers={"Retry-After": str(seconds)})
        for key, _, seconds in [("all", 40, 60), ("user:"+digest(name), 8, 900), ("subject:"+subject, 20, 900)]:
            db.execute("INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1", (key, now+seconds))


async def json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "需要 JSON 请求")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 4096:
            raise HTTPException(413, "请求过大")
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(400, "请求格式不正确") from None


def create_gateway(config: dict, *, transport=None) -> FastAPI:
    home = Path(config["home"])
    origin = config["origin"].rstrip("/")
    public = urlsplit(origin)
    local = config.get("local_only") is True
    if (home.resolve() != home or public.path or public.query or public.fragment or public.username
            or public.password or not public.hostname or (not local and public.scheme != "https")
            or (local and (public.scheme != "http" or public.hostname != "127.0.0.1"))):
        raise ValueError("invalid_gateway_config")
    team, audience = config.get("access_team", ""), config.get("access_audience", "")
    if not local and (not re.fullmatch(r"[a-z0-9-]+", team) or not re.fullmatch(r"[a-f0-9]{64}", audience)):
        raise ValueError("cloudflare_access_configuration_required")
    web = Path(config["web_dir"]).resolve()
    initialize(home)
    dummy = password_hash(secrets.token_urlsafe(32))
    hashing = asyncio.Semaphore(2)
    key_lock = asyncio.Lock()
    keys, keys_at = {}, 0.0
    issuer = f"https://{team}.cloudflareaccess.com"

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport,
                                     timeout=httpx.Timeout(30, connect=5),
                                     limits=httpx.Limits(max_connections=128, max_keepalive_connections=32)) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def cloudflare(request: Request) -> tuple[str, float]:
        nonlocal keys, keys_at
        if local:
            if (not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}
                    or any(k in request.headers for k in ("forwarded", "x-forwarded-for", "cf-connecting-ip", "cf-access-jwt-assertion"))):
                raise HTTPException(403, "local_preview_only")
            return "local-preview", time.time()+SESSION_SECONDS
        import jwt
        encoded = request.headers.get("cf-access-jwt-assertion", "")
        if not encoded or len(encoded) > 16384:
            raise HTTPException(401, "cloudflare_access_required")
        try:
            header = jwt.get_unverified_header(encoded)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError()
            async with key_lock:
                if time.time()-keys_at > 300 or (header["kid"] not in keys and time.time()-keys_at > 30):
                    r = await app.state.client.get(issuer+"/cdn-cgi/access/certs")
                    r.raise_for_status()
                    keys = {k["kid"]: jwt.PyJWK.from_dict(k).key for k in r.json()["keys"]
                            if k.get("kty") == "RSA" and k.get("use", "sig") == "sig" and k.get("alg", "RS256") == "RS256"}
                    keys_at = time.time()
            claims = jwt.decode(encoded, keys[header["kid"]], algorithms=["RS256"], issuer=issuer,
                                audience=audience, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError()
            return digest(issuer+":"+claims["sub"]), float(claims["exp"])
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, httpx.HTTPError):
            raise HTTPException(401, "cloudflare_access_required") from None

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            if request.headers.get("host", "").lower() != public.netloc.lower():
                raise HTTPException(421, "gateway_origin_required")
            if request.query_params.get("token") or request.headers.get("authorization"):
                raise HTTPException(401, "password_login_required")
            if request.headers.get("origin") not in {None, origin}:
                raise HTTPException(403, "origin_denied")
            # Access returns via a top-level navigation from cloudflareaccess.com.
            if (request.headers.get("sec-fetch-site") == "cross-site"
                    and not (request.method == "GET" and request.headers.get("sec-fetch-mode") == "navigate")):
                raise HTTPException(403, "origin_denied")
            if request.method not in SAFE_METHODS and request.headers.get("origin") != origin:
                raise HTTPException(403, "origin_required")
            subject, expires = await cloudflare(request)
            request.state.subject, request.state.access_expires = subject, expires
            if any(h.startswith('x-carme-visitor') for h in request.headers):
                raise HTTPException(401, 'visitor_header_denied')
            visitor_route = request.url.path.startswith('/api/visitor')
            secret = request.cookies.get('carme_visitor' if visitor_route else COOKIE, "")
            request.state.session = None if visitor_route else session_row(home, secret, subject)
            if visitor_route and secret and request.method not in SAFE_METHODS:
                if not hmac.compare_digest(request.headers.get('x-carme-csrf', '').encode(), csrf(secret).encode()):
                    raise HTTPException(403, 'csrf_required')
            if request.state.session and not visitor_route:
                expected_account = request.headers.get("x-carme-account") or request.query_params.get("account")
                if expected_account and expected_account != request.state.session["account"]:
                    raise HTTPException(409, "account_changed")
            if request.state.session and not visitor_route and request.method not in SAFE_METHODS:
                if not hmac.compare_digest(request.headers.get("x-carme-csrf", "").encode(), csrf(secret).encode()):
                    raise HTTPException(403, "csrf_required")
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        if request.url.path.startswith("/api/visitor/") and response.status_code == 401:
            response.delete_cookie("carme_visitor", path="/api/visitor", httponly=True, secure=not local, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def require(request: Request, *, changed: bool = True):
        row = request.state.session
        if not row:
            raise HTTPException(401, "password_login_required")
        if changed and row["must_change"]:
            raise HTTPException(403, "password_change_required")
        return row

    @app.api_route('/api/visitor/{account}/{operation:path}', methods=['GET', 'POST', 'DELETE', 'PUT', 'PATCH', 'HEAD', 'OPTIONS'])
    async def visitor_auth(account: str, operation: str, request: Request):
        route = visitor_route(request.method, request.url.path)
        if not route:
            raise HTTPException(403, 'visitor_route_denied')
        def backend():
            with database(home) as db:
                user = db.execute('SELECT instance,enabled,version FROM users WHERE account=?', (account,)).fetchone()
            if not user or not user['enabled']:
                raise HTTPException(401, 'visitor_account_unavailable')
            try:
                acc, token = account_backend(home, account, user['instance'])
            except (ValueError, OSError, KeyError):
                raise HTTPException(503, 'visitor_account_unavailable') from None
            return acc, token, user['version']
        acc, token, version = backend()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (65536 if route[1] == 'write' else 4096):
                raise HTTPException(413, 'request_too_large')
        secret = request.cookies.get('carme_visitor', '')
        if len(secret) > 128:
            raise HTTPException(401, 'visitor_session_invalid')
        target = request.scope['raw_path'].decode('ascii')
        if request.scope['query_string']:
            target += '?' + request.scope['query_string'].decode('ascii')
        # Resolve the registered backend again immediately before forwarding.
        if backend() != (acc, token, version) or time.time() >= request.state.access_expires:
            raise HTTPException(401, 'visitor_account_unavailable')
        proof = visitor_proof(token, account=account, instance=acc['instance_id'],
            method=request.method, target=target, body=bytes(body), secret=secret,
            subject=digest(request.state.subject), access_expires=request.state.access_expires, account_version=version)
        if route[1] in {'read', 'write'}:
            headers = {k: request.headers[k] for k in ('range', 'last-event-id', 'content-type') if k in request.headers}
            headers.update({'Host': urlsplit(acc['origin']).netloc, 'X-Carme-Visitor-Proof': proof,
                            'X-Carme-Visitor-Session': secret})
            outgoing = app.state.client.build_request(request.method,
                f'http://127.0.0.1:{acc["port"]}' + target, content=bytes(body), headers=headers,
                timeout=httpx.Timeout(30, connect=5, read=None))
            outgoing.headers.pop('cookie', None)
            try:
                upstream = await app.state.client.send(outgoing, stream=True)
            except httpx.HTTPError:
                raise HTTPException(502, 'visitor_backend_unavailable') from None
            if upstream.status_code not in {200, 206}:
                code = upstream.status_code if upstream.status_code in {400, 401, 403, 404, 409, 413, 415, 416, 422, 429} else 502
                await upstream.aclose()
                return JSONResponse({'detail': 'visitor_read_failed'}, status_code=code)
            content_type = upstream.headers.get('content-type', '').split(';')[0]
            if content_type not in {'application/json', 'application/octet-stream', 'image/webp', 'text/event-stream'}:
                await upstream.aclose()
                raise HTTPException(502, 'visitor_backend_unavailable')
            out_headers = {k: upstream.headers[k] for k in
                ('content-type', 'content-disposition', 'content-range', 'accept-ranges', 'content-length') if k in upstream.headers}
            out_headers['Cache-Control'] = 'no-store'
            out_headers['X-Content-Type-Options'] = 'nosniff'
            if content_type == 'text/event-stream':
                out_headers['X-Accel-Buffering'] = 'no'
            # Transport proofs are intentionally short-lived. Reconnect with a
            # fresh signed request; never extend an old proof inside a long stream.
            deadline = min(time.time() + 29, request.state.access_expires)
            async def visitor_chunks():
                pending = None
                iterator = upstream.aiter_bytes().__aiter__()
                try:
                    while time.time() < deadline:
                        if backend() != (acc, token, version) or await request.is_disconnected():
                            break
                        if pending is None:
                            pending = asyncio.create_task(anext(iterator))
                        ready, _ = await asyncio.wait({pending}, timeout=0.5)
                        if not ready:
                            continue
                        try:
                            chunk = pending.result()
                        except StopAsyncIteration:
                            break
                        pending = None
                        if backend() != (acc, token, version) or time.time() >= deadline:
                            break
                        if chunk:
                            yield chunk
                except (HTTPException, httpx.HTTPError):
                    return
                finally:
                    if pending:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    await upstream.aclose()
            from starlette.background import BackgroundTask
            return StreamingResponse(visitor_chunks(), status_code=upstream.status_code, headers=out_headers,
                                     background=BackgroundTask(upstream.aclose))
        try:
            reply = await app.state.client.request(request.method,
                f'http://127.0.0.1:{acc["port"]}' + target, content=bytes(body),
                headers={'Host': urlsplit(acc['origin']).netloc,
                    'Content-Type': request.headers.get('content-type', ''),
                    'X-Carme-Visitor-Proof': proof, 'X-Carme-Visitor-Session': secret})
            data = reply.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, 'visitor_backend_unavailable') from None
        if backend() != (acc, token, version) or time.time() >= request.state.access_expires:
            raise HTTPException(401, 'visitor_account_unavailable')
        if reply.status_code != 200:
            # Never relay arbitrary backend error bodies or headers to a visitor.
            code = reply.status_code if reply.status_code in {400, 401, 403, 413, 415, 429} else 502
            return JSONResponse({'detail': 'visitor_auth_failed'}, status_code=code,
                headers={'Retry-After': '900'} if code == 429 else None)
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise HTTPException(502, 'visitor_backend_unavailable')
        result = {'ok': True, 'auth_mode': 'visitor'}
        if request.method != 'DELETE':
            session = data.get('session')
            fields = ('id', 'visitor_id', 'conversation_id', 'username', 'display_name',
                      'membership_version', 'created_at', 'expires_at')
            if not isinstance(session, dict) or any(k not in session for k in fields):
                raise HTTPException(502, 'visitor_backend_unavailable')
            result['session'] = {k: session[k] for k in fields}
            if operation == 'login':
                secret = data.get('secret', '')
                if not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', secret):
                    raise HTTPException(502, 'visitor_backend_unavailable')
            result['csrf'] = csrf(secret)
        response = JSONResponse(result)
        if operation == 'login':
            ttl = max(0, int(min(result['session']['expires_at'], request.state.access_expires) - time.time()))
            response.set_cookie('carme_visitor', secret, max_age=ttl, httponly=True,
                                secure=not local, samesite='strict', path='/api/visitor')
        elif request.method == 'DELETE':
            response.delete_cookie('carme_visitor', path='/api/visitor', httponly=True,
                                   secure=not local, samesite='strict')
        return response

    @app.post("/api/login")
    async def login(request: Request):
        data = await json_body(request)
        name, password = data.get("username", ""), data.get("password", "")
        if not isinstance(name, str) or not isinstance(password, str):
            raise HTTPException(400, "请求格式不正确")
        name = name.strip().lower()
        throttle(home, name[:128], request.state.subject)
        with database(home) as db:
            user = db.execute("SELECT * FROM users WHERE account=?", (name,)).fetchone()
        async with hashing:
            valid = await asyncio.to_thread(password_matches, password, user["password"] if user else dummy)
        if not valid or not user or not user["enabled"]:
            raise HTTPException(401, "账号或密码不正确")
        try:
            _, token = account_backend(home, name, user["instance"])
        except (ValueError, OSError, KeyError):
            raise HTTPException(503, "账号暂不可用，请联系管理员") from None
        secret, sid, now = secrets.token_urlsafe(32), secrets.token_hex(16), time.time()
        with database(home) as db:
            # A concurrent reset/disable must not resurrect an old password.
            current = db.execute("SELECT version,enabled FROM users WHERE account=?", (name,)).fetchone()
            if not current or not current["enabled"] or current["version"] != user["version"]:
                raise HTTPException(401, "账号或密码不正确")
            db.execute("DELETE FROM sessions WHERE expires_at<=? OR secret_hash=?", (now, digest(request.cookies.get(COOKIE, ""))))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                       (sid, digest(secret), name, user["version"], digest(token), request.state.subject, now, now+SESSION_SECONDS))
            db.execute("DELETE FROM attempts WHERE key=?", ("user:"+digest(name),))
        response = JSONResponse({"ok": True, "username": name, "csrf": csrf(secret), "must_change": bool(user["must_change"])})
        response.set_cookie(COOKIE, secret, max_age=SESSION_SECONDS, httponly=True,
                            secure=not local, samesite="strict", path="/")
        return response

    @app.get("/api/session")
    async def session(request: Request):
        row = require(request, changed=False)
        return {"ok": True, "username": row["account"], "csrf": csrf(request.cookies[COOKIE]),
                "must_change": bool(row["must_change"]), "auth_mode": "password",
                "session": {k: row[k] for k in ("id", "created_at", "expires_at")}}

    @app.delete("/api/session")
    async def logout(request: Request):
        row = require(request, changed=False)
        with database(home) as db:
            db.execute("DELETE FROM sessions WHERE id=? AND account=?", (row["id"], row["account"]))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", httponly=True, secure=not local, samesite="strict")
        return response

    @app.get("/api/sessions")
    async def sessions(request: Request):
        row = require(request)
        with database(home) as db:
            rows = db.execute("SELECT id,created_at,expires_at FROM sessions WHERE account=? AND expires_at>? AND version=?",
                              (row["account"], time.time(), row["version"])).fetchall()
        return {"sessions": [dict(s) for s in rows]}

    @app.delete("/api/sessions/{sid}")
    async def revoke(sid: str, request: Request):
        row = require(request)
        with database(home) as db:
            found = db.execute("DELETE FROM sessions WHERE id=? AND account=?", (sid, row["account"])).rowcount
        if not found:
            raise HTTPException(404, "会话不存在")
        return {"ok": True}

    @app.post("/api/password")
    async def change_password(request: Request):
        row = require(request, changed=False)
        data = await json_body(request)
        throttle(home, row["account"], request.state.subject)
        with database(home) as db:
            user = db.execute("SELECT * FROM users WHERE account=?", (row["account"],)).fetchone()
        async with hashing:
            if not await asyncio.to_thread(password_matches, data.get("current_password"), user["password"]):
                raise HTTPException(400, "当前密码不正确")
            if data.get("current_password") == data.get("new_password"):
                raise HTTPException(400, "新密码需要与当前密码不同")
            try:
                hashed = await asyncio.to_thread(password_hash, data.get("new_password"))
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        with database(home) as db:
            changed = db.execute("UPDATE users SET password=?,version=version+1,must_change=0 WHERE account=? AND version=? AND enabled=1",
                                 (hashed, row["account"], row["version"])).rowcount
            if not changed:
                raise HTTPException(401, "请重新登录")
            db.execute("DELETE FROM sessions WHERE account=?", (row["account"],))
        (home / "accounts" / row["account"] / "login-initial.txt").unlink(missing_ok=True)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=not local, httponly=True, samesite="strict")
        return response

    @app.get("/")
    async def index(request: Request):
        row = request.state.session
        if not row:
            return HTMLResponse(auth_page(False))
        if row["must_change"]:
            return RedirectResponse("/account", 303)
        if not (web / "index.html").is_file():
            raise HTTPException(503, "网页尚未构建")
        html = (web / "index.html").read_text().replace("<html ", '<html data-carme-account="'+row["account"]+'" ', 1)
        return HTMLResponse(html)

    @app.get('/visit/{account}/{conversation_id}')
    async def visitor_page(account: str, conversation_id: str):
        # Access middleware still applies. The shell contains no identity or credential.
        if not ACCOUNT.fullmatch(account) or not re.fullmatch(r'c_[a-f0-9]+', conversation_id):
            raise HTTPException(404, 'visitor_entry_missing')
        if not (web / 'index.html').is_file():
            raise HTTPException(503, '网页尚未构建')
        return HTMLResponse((web / 'index.html').read_text())

    @app.get("/account")
    async def account_page(request: Request):
        if not request.state.session:
            return RedirectResponse("/", 303)
        return HTMLResponse(auth_page(True))

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        if path.startswith('visitor'):
            raise HTTPException(403, 'visitor_route_denied')
        row = require(request)
        if path in {"session", "login", "password", "sessions"} or path.startswith("sessions/"):
            raise HTTPException(405, "method_not_allowed")
        if "\\" in path or any(p in {".", "..", ""} for p in path.split("/")):
            raise HTTPException(400, "path_denied")
        try:
            acc, token = account_backend(home, row["account"], row["instance"])
        except (ValueError, OSError, KeyError):
            raise HTTPException(503, "账号暂不可用，请联系管理员") from None
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 128*1024*1024:
                raise HTTPException(413, "请求过大")
        if not session_row(home, request.cookies[COOKIE], request.state.subject) or time.time() >= request.state.access_expires:
            raise HTTPException(401, "password_login_required")
        headers = {k: request.headers[k] for k in ("content-type", "accept", "range", "last-event-id", "x-carme-desktop-control") if k in request.headers}
        headers.update({"Authorization": "Bearer "+token, "Host": urlsplit(acc["origin"]).netloc})
        url = httpx.URL(scheme="http", host="127.0.0.1", port=acc["port"],
                        raw_path=request.scope["raw_path"] + (b"?"+request.scope["query_string"] if request.scope["query_string"] else b""))
        client = app.state.client
        upstream_request = client.build_request(request.method, url, content=bytes(body), headers=headers,
                                               timeout=httpx.Timeout(30, connect=5, read=None))
        upstream_request.headers.pop("cookie", None)
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            raise HTTPException(503, "当前账号服务未启动，请联系管理员") from None
        # Never forward account backend cookies, redirects, or CORS/security headers.
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            raise HTTPException(502, "backend_redirect_denied")
        out_headers = {k: upstream.headers[k] for k in ("content-type", "content-disposition", "content-range", "accept-ranges", "x-carme-bot", "x-screenshot-mtime", "x-carme-desktop-target", "x-carme-desktop-state") if k in upstream.headers}
        events = upstream.headers.get("content-type", "").split(";", 1)[0] == "text/event-stream"
        if events:
            out_headers["X-Accel-Buffering"] = "no"
        secret, subject = request.cookies[COOKIE], request.state.subject

        async def stream():
            pending = None
            deadline = min(row["expires_at"], request.state.access_expires)
            if not events:
                deadline = min(deadline, time.time()+120)
            iterator = upstream.aiter_bytes().__aiter__()
            try:
                while time.time() < deadline:
                    if await request.is_disconnected() or not session_row(home, secret, subject):
                        break
                    if pending is None:
                        pending = asyncio.create_task(anext(iterator))
                    ready, _ = await asyncio.wait({pending}, timeout=1)
                    if not ready:
                        continue
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        break
                    pending = None
                    if chunk:
                        yield chunk
            finally:
                if pending:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await upstream.aclose()
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=out_headers)

    @app.get("/{path:path}")
    async def asset(path: str):
        allowed = {"sw.js", "manifest.webmanifest", "favicon.png", "apple-touch-icon.png",
                   "icon-192.png", "icon-512.png", "icon-maskable.png"}
        target = web / path
        if ((path not in allowed and not path.startswith("assets/")) or target.resolve() != target
                or not target.is_relative_to(web) or not target.is_file()):
            raise HTTPException(404, "资源不存在")
        return FileResponse(target)

    return app


def auth_page(account: bool) -> str:
    # Static form, no passwords/tokens/user-provided HTML embedded in the page.
    return AUTH_HTML.replace("__MODE__", "account" if account else "login")


AUTH_HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Carme · 账号</title>
<link rel="icon" href="/favicon.png"><style>
*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:#20242b;font:15px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;min-height:100dvh;display:grid;place-items:center;padding:24px}
main{width:100%;max-width:410px;background:white;border:1px solid #e4e7eb;border-radius:24px;padding:36px;box-shadow:0 14px 50px #23334a08}
img{width:48px;height:48px;border-radius:12px}h1{font-size:25px;letter-spacing:-.6px;margin:24px 0 10px}p{color:#6d747f;line-height:1.6}label{display:block;margin:21px 0 8px;font-weight:550}input{display:block;width:100%;padding:13px;border:1px solid #d6dbe2;border-radius:10px;font:inherit;background:#fff}input:focus{outline:2px solid #3b7bde;outline-offset:2px}button{width:100%;padding:14px;margin-top:24px;background:#1f66c2;color:white;border:0;border-radius:11px;font:inherit;font-weight:600;cursor:pointer}button:disabled{opacity:.55;cursor:wait}a{color:#1f66c2}small{display:block;color:#8b929c;line-height:1.6;margin-top:24px}#error{color:#b43d35;font-size:14px;margin-top:16px}#identity{font-weight:600;color:#29313c}.secondary{background:#edf1f6;color:#33445b;margin-top:12px}[hidden]{display:none!important}@media(max-width:480px){body{padding:16px}main{padding:28px}}
</style></head><body><main><img src="/icon-192.png" alt="Carme"><h1 id="title">登录 Carme</h1>
<p id="intro">使用你的账号，进入自己的工作空间。</p><p id="identity" hidden></p>
<form id="form"><div id="username-field"><label for="username">账号</label><input id="username" name="username" autocomplete="username" required maxlength="32" autocapitalize="none" spellcheck="false"></div>
<label id="password-label" for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="128">
<div id="new-fields" hidden><label for="new-password">新密码</label><input id="new-password" name="new-password" type="password" autocomplete="new-password" minlength="15" maxlength="128"><label for="confirm">确认新密码</label><input id="confirm" type="password" autocomplete="new-password" minlength="15" maxlength="128"></div>
<div id="error" role="alert" aria-live="polite"></div><button id="submit" type="submit">登录</button></form>
<button class="secondary" id="logout" hidden>退出账号</button><p id="back" hidden><a href="/">返回工作空间</a></p>
<small id="note">账号由管理员创建。聊天、文件与浏览器登录状态按账号独立保存。</small></main><script>
const mode="__MODE__", $=id=>document.getElementById(id);let csrf="";
async function request(path,body,method="POST") {const r=await fetch(path,{method,credentials:"same-origin",redirect:"error",headers:{"Content-Type":"application/json",...(csrf?{"X-Carme-CSRF":csrf}:{})},...(body?{body:JSON.stringify(body)}:{})});let data;try{data=await r.json()}catch{throw Error("入口登录已过期，请刷新页面完成 Cloudflare 登录。")};if(!r.ok)throw Error(data.detail==="cloudflare_access_required"?"请刷新页面完成 Cloudflare 登录。":data.detail||"操作未完成");return data}
function changed(){try{localStorage.setItem("carme_auth_changed",String(Date.now()))}catch{} }
if(mode==="account") {$("title").textContent="账号与密码";$("intro").textContent="修改密码后，所有设备需要重新登录。";$("username-field").hidden=true;$("username").required=false;$("new-fields").hidden=false;$("new-password").required=true;$("confirm").required=true;$("password-label").textContent="当前密码";$("submit").textContent="保存新密码";$("logout").hidden=false;
request("/api/session",null,"GET").then(s=>{csrf=s.csrf;$("identity").textContent="当前账号："+s.username;$("identity").hidden=false;$("back").hidden=s.must_change;$("intro").textContent=s.must_change?"首次登录，请先设置自己的密码（15–128 个字符）。":"修改密码后，所有设备需要重新登录。"}).catch(()=>location.replace("/"));}
else request("/api/session",null,"GET").then(s=>location.replace(s.must_change?"/account":"/")).catch(()=>{});
$("form").onsubmit=async e=>{e.preventDefault();$("error").textContent="";if(mode==="account"&&$("new-password").value!==$("confirm").value){$("error").textContent="两次新密码不一致";return}$("submit").disabled=true;try{const r=await request(mode==="account"?"/api/password":"/api/login",mode==="account"?{current_password:$("password").value,new_password:$("new-password").value}:{username:$("username").value,password:$("password").value});$("password").value="";$("new-password").value="";$("confirm").value="";changed();location.replace(r.must_change?"/account":"/")}catch(e){$("error").textContent=e.message}finally{$("submit").disabled=false}};
$("logout").onclick=async()=>{try{await request("/api/session",null,"DELETE");changed();location.replace("/")}catch(e){$("error").textContent=e.message}};
window.addEventListener("pageshow",e=>{if(e.persisted)location.reload()});
if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").then(r=>r.update()).catch(()=>{});
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "login-init", "login-password", "login-disable"], nargs="?", default="serve")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--account")
    args = parser.parse_args()
    if args.action != "serve":
        if not args.home or not args.account:
            parser.error("需要 --home 和 --account")
        home = args.home.absolute()
        account_backend(home, args.account)
        initialize(home)
        if args.action == "login-disable":
            with database(home) as db:
                db.execute("UPDATE users SET enabled=0,version=version+1 WHERE account=?", (args.account,))
                db.execute("DELETE FROM sessions WHERE account=?", (args.account,))
            print("已禁用账号登录并撤销全部会话；数据已保留。")
        elif args.action == "login-init":
            path = home / "accounts" / args.account / "login-initial.txt"
            if path.exists():
                raise ValueError("初始密码文件已存在；不会覆盖，请使用 login-password 重置")
            password = secrets.token_urlsafe(24)
            set_password(home, args.account, password, initial=True)
            with path.open("x") as f:
                path.chmod(0o600)
                f.write("账号："+args.account+"\n初始密码："+password+"\n首次登录必须修改；修改后本文件自动删除。\n")
            print("初始密码仅保存在本机私有文件："+str(path))
        else:
            import getpass
            password = getpass.getpass("新密码（15–128 个字符，不会回显）：")
            if not hmac.compare_digest(password.encode(), getpass.getpass("再次输入：").encode()):
                raise ValueError("两次密码不一致")
            set_password(home, args.account, password)
            print("已更新密码并撤销全部设备会话。")
        return
    if not args.config:
        parser.error("需要 --config")
    config = json.loads(args.config.read_text())
    import uvicorn
    # Do not log paths/query strings or trust spoofable forwarding headers.
    # keep-alive 必须长于 cloudflared 的源站池（默认 90 秒），否则复用到已被本进程关闭的连接会得到
    # connection reset，Cloudflare 直接渲染 502（隧道日志里的 Unable to reach the origin）。
    uvicorn.run(create_gateway(config), host="127.0.0.1", port=config["port"],
                access_log=False, proxy_headers=False, log_level="warning", timeout_keep_alive=120,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
