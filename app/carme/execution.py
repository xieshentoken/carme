"""Control-owned jobs. Only the separately authenticated Broker can lease them.

Workers have no network. Their bounded JSON messages travel over Docker stdio
and the Broker relay; task credentials never authorize container management.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import socket
import ssl
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .engines import CliEngineError, CliRunResult, validate_runtime_profile
from .llm import provider_session_headers
MAX_MESSAGE = 15 * 1024 * 1024
# Broker liveness for health reporting and admitting new work stays snappy and unchanged.
HEARTBEAT_SECONDS = 12
# A job lease is renewed by the Broker over its own HTTP path. A host-level stall (memory
# pressure, CPU starvation) delays that renewal and this polling loop alike, so the window has
# to absorb a stalled host instead of failing a job whose tools already succeeded.
POLL_SECONDS = 0.5
STALL_SECONDS = 2.0
LEASE_SECONDS = 30
LEASE_RENEW_SECONDS = 1.0
LEASE_CALL_TIMEOUT = LEASE_SECONDS + 5
# How long the Broker keeps asking before it accepts that Control is gone. Renewal traffic is
# re-signed when Control's replay window rejects a request that waited in a stalled socket, so
# this budget is the only thing that ends a job during a Control outage. The task deadline and
# Control's own lease stay the hard bounds.
LEASE_RETRY_SECONDS = 60
# Control refuses a signed Broker request whose nonce is older than this window. Kept as a
# named constant because the Broker's re-signing threshold has to stay inside it.
NONCE_WINDOW_SECONDS = 30


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def signature(key: bytes, nonce: str, body: bytes) -> str:
    return hmac.new(key, nonce.encode() + b"\n" + body, hashlib.sha256).hexdigest()


async def bounded_body(request: Request, limit=MAX_MESSAGE) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, "message_too_large")
    return bytes(body)


class Execution:
    """One Control process owns policy, callbacks and ephemeral execution leases."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.jobs: dict[str, dict] = {}
        self.nonces: dict[str, float] = {}
        self.broker_seen = 0.0
        self.broker_health: dict = {}
        self.auth_status: dict[str, str] = {}
        self.mac_jobs: dict[str, dict] = {}
        self.mac_seen = 0.0
        self.mac_authorized = False
        # 人工桌面查看/控制通道：Runner 授权期间由 /desktop/* 路由使用。
        self.mac_frame = b""
        self.mac_frame_at = 0.0
        self.mac_frame_size = (0, 0)
        self.mac_screen_until = 0.0
        self.mac_screen_waiters: list = []
        self.mac_control_enabled = False
        self.mac_control_queue: list = []
        self.mac_control_waiters: dict = {}
        self.mac_control_seq = 0
        self.browser_sessions: dict[tuple[str, str], dict] = {}

    @property
    def settings(self):
        return self.runtime.config.isolation.get("broker", {})

    def key(self) -> bytes:
        try:
            key = Path(self.settings["key_file"]).read_bytes().strip()
            if len(key) < 32:
                raise ValueError()
            return key
        except (KeyError, OSError, ValueError):
            raise RuntimeError("broker_not_configured") from None

    def health(self):
        fresh = time.time() - self.broker_seen < HEARTBEAT_SECONDS
        return {"control": "ready", "broker": "ready" if fresh else "offline",
                "action": self.broker_health.get("action", "unverified") if fresh else "unavailable",
                "pi": self.broker_health.get("pi", "unverified") if fresh else "unavailable",
                "browser": self.broker_health.get("browser", "not_configured") if fresh else "unavailable",
                "web_route": "Docker Browser", "native_route": "Mac Runner (explicit local grant)",
                "mac_runner": ("authorized" if self.mac_authorized else "connected_needs_local_grant") if time.time()-self.mac_seen<5 else
                    "offline" if self.runtime.config.isolation.get('mac_runner') else "not_paired", "worker_network": "none",
                "active_jobs": sum(not j["future"].done() for j in self.jobs.values())}

    def mac_key(self):
        settings=self.runtime.config.isolation.get('mac_runner',{})
        try:
            key=Path(settings['key_file']).read_bytes().strip()
            if len(key)<32 or hmac.compare_digest(key,self.key()):raise ValueError()
            return key
        except (KeyError,OSError,ValueError):raise RuntimeError('mac_runner_not_paired') from None

    async def native(self,run_id,action):
        from .macos_runner import validate_action
        from .projects import digest
        validate_action(action);self.mac_key();self.runtime.check_task_policy(run_id)
        task=self.runtime.store.get_task(run_id);meta=self.runtime._meta(task)
        settings=self.runtime.config.isolation.get('mac_runner',{})
        if (meta['execution_target']!='macos' or meta['execution_target_id']!=settings.get('runner_id')
                or 'mac_action' not in meta['policy']['tools']):raise RuntimeError('native_target_or_capability_denied')
        if time.time()-self.mac_seen>=5 or not self.mac_authorized:raise RuntimeError('mac_runner_unavailable_or_local_grant_required')
        job_id=secrets.token_hex(16);future=asyncio.get_running_loop().create_future()
        job={'job_id':job_id,'run_id':run_id,'bot_id':task['agent_id'],'action':action,'action_sha256':digest(action),
             'deadline':min(meta['deadline'],time.time()+30),'permission_version':meta['policy']['permission_version']}
        self.mac_jobs[job_id]={'spec':job,'future':future,'lease':''}
        await self.runtime._emit('mac.action_queued',{'job_id':job_id,'action_sha256':job['action_sha256']},run_id,task['agent_id'])
        try:
            while not future.done():
                self.runtime.check_task_policy(run_id)
                if time.time()>=job['deadline'] or time.time()-self.mac_seen>=5 or not self.mac_authorized:raise RuntimeError('native_lease_revoked_no_replay')
                await asyncio.wait({future},timeout=.1)
            result=future.result()
            await self.runtime._emit('mac.action_finished',{'job_id':job_id,'result':result},run_id,task['agent_id'])
            return result
        finally:future.cancel();self.mac_jobs.pop(job_id,None)

    def check(self, job):
        if job["future"].done() or time.time() >= job["spec"]["deadline"]:
            raise RuntimeError("task_token_revoked")
        self.runtime.check_task_policy(job["spec"]["run_id"])

    async def submit(self, run_id: str, role: str, payload: dict, *, timeout=120,
                     profile=None, callback=None, on_stream=None):
        self.key()  # Missing pairing never becomes a local executor.
        self.runtime.check_task_policy(run_id)
        task = self.runtime.store.get_task(run_id)
        meta = self.runtime._meta(task)
        if meta["execution_target"] != "container":
            raise RuntimeError("target_unassigned")
        target = self.runtime.config.isolation["targets"][meta["execution_target_id"]]
        image = (profile["image_digest"] if role == "pi" else
                 self.runtime.config.isolation.get('browser', {}).get('image_digest', '') if role == 'browser'
                 else target.get("image_digest", ""))
        if time.time() - self.broker_seen >= HEARTBEAT_SECONDS:
            raise RuntimeError("container_runner_unavailable: broker offline; no host fallback")
        pi_limit = min(int(self.settings.get("max_pi", 2)), int(self.broker_health.get("max_pi", 1)))
        if role == "pi" and sum(j["spec"]["role"] == "pi" for j in self.jobs.values()) >= pi_limit:
            raise RuntimeError("pi_concurrency_combination_unsupported: no waiting parent may hold the last slot")
        token = "tk_" + secrets.token_urlsafe(32)
        job_id = secrets.token_hex(16)
        spec = {"job_id": job_id, "instance_id": self.settings["instance_id"],
                "bot_id": task["agent_id"], "run_id": run_id, "role": role,
                "target_id": meta["execution_target_id"], "image_digest": image,
                "deadline": min(meta["deadline"], time.time() + timeout),
                "max_output_bytes": meta["policy"]["max_output_bytes"],
                "permission_version": meta["policy"]["permission_version"],
                "token_id": secrets.token_hex(16), "audience": "worker-relay",
                "tools": list(meta["policy"]["tools"]), "payload": payload}
        future = asyncio.get_running_loop().create_future()
        job = {"spec": spec, "future": future, "token": token, "callback": callback,
               "stream": on_stream, "profile": profile, "lease": "", "lease_until": 0,
               "sequence": 0, "messages": {}, "lock": asyncio.Lock(), "model_calls": 0}
        self.jobs[job_id] = job
        await self.runtime._emit("execution.queued", {"job_id": job_id, "role": role,
            "image_digest": image, "target_id": spec["target_id"],"attempt_id":meta.get('attempt_id')}, run_id, task["agent_id"])
        try:
            poll_started = time.time()
            while not future.done():
                # Wall-clock overshoot means this process was starved, not that the Broker gave up:
                # credit the measured stall back before judging the lease, or a host-level stall
                # fails a healthy job whose tools already succeeded. The deadline check stays the
                # absolute bound, so credit can never keep a job alive past its own deadline.
                overshoot = time.time() - poll_started - POLL_SECONDS
                if job["lease"] and overshoot >= STALL_SECONDS:
                    job["lease_until"] += overshoot
                self.check(job)
                if job["lease"] and time.time() > job["lease_until"]:
                    raise RuntimeError("broker_lease_expired: reconcile before retry")
                poll_started = time.time()
                await asyncio.wait({future}, timeout=POLL_SECONDS)
            result = future.result()
            self.runtime.check_task_policy(run_id)
            receipt = result.pop("_execution", {})
            await self.runtime._emit("execution.finished", {"job_id": job_id, "role": role,
                "image_digest": image, "status": "failed" if "error" in result else "finished", **receipt},
                run_id, task["agent_id"])
            if job.get('yield_tool'):
                if receipt.get('cleanup')!='removed':raise RuntimeError('pi_yield_cleanup_unconfirmed')
                return {'text':'','yield_tool':job['yield_tool']}
            if "error" in result:
                raise RuntimeError(result["error"])
            return result
        finally:
            future.cancel()
            job["token"] = ""  # Revoke immediately; a stale relay cannot issue another action.
            job["callback"] = job["stream"] = None
            # Broker lease polling sees absence and kills only this job's container.
            self.jobs.pop(job_id, None)

    async def browser_tool(self, ctx, name, arguments):
        from .docker_browser import WEB_TOOLS, PROFILE, DEFAULT_DANGER
        self.runtime.check_task_policy(ctx.task_id)
        meta = self.runtime._meta(self.runtime.store.get_task(ctx.task_id))
        if (meta['execution_target'] != 'container' or name not in WEB_TOOLS
                or name not in meta['policy']['tools'] or not self.runtime.config.browser.enabled):
            raise RuntimeError('docker_browser_capability_denied')
        profile = arguments.get('profile') or 'default'
        if not isinstance(profile, str) or not PROFILE.fullmatch(profile):
            raise ValueError('browser_profile_denied')
        if not self.runtime.config.isolation.get('browser', {}).get('image_digest'):
            raise RuntimeError('docker_browser_not_configured: no host fallback')
        key = (ctx.task_id, profile)
        entry = self.browser_sessions.get(key)
        if entry is None:
            if self.health()['browser'] != 'ready':
                raise RuntimeError('docker_browser_unavailable: no host fallback')
            if len(self.browser_sessions) >= int(self.settings.get('max_browser', 2)):
                raise RuntimeError('browser_capacity_busy: close an idle browser before delegating')
            safety = dict(self.runtime.config.browser.safety)
            safety.setdefault('dangerous_patterns', DEFAULT_DANGER)
            entry = {'queue': asyncio.Queue(maxsize=1), 'lock': asyncio.Lock(), 'pending': None,
                     'ctx': None, 'safety': safety, 'requests': 0, 'bytes': 0,
                     'network_slots': asyncio.Semaphore(24)}
            self.browser_sessions[key] = entry
            payload = {'profile': profile, 'bot_id': ctx.agent.id, 'run_id': ctx.task_id,
                       'safety': safety, 'max_output_bytes': meta['policy']['max_output_bytes'],
                       'browser_channel': self.runtime.config.browser.channel}
            entry['task'] = asyncio.create_task(self.submit(ctx.task_id, 'browser', payload,
                timeout=max(1, meta['deadline'] - time.time()), callback=entry))
        async with entry['lock']:
            if entry['task'].done():
                entry['task'].result()
                raise RuntimeError('browser_session_ended: explicit new run required')
            self.runtime.check_task_policy(ctx.task_id)
            future = asyncio.get_running_loop().create_future()
            identity = secrets.token_hex(16)
            entry['ctx'] = ctx
            entry['pending'] = (identity, future)
            await entry['queue'].put({'id': identity, 'name': name, 'arguments': arguments})
            try:
                await asyncio.wait({future, entry['task']}, return_when=asyncio.FIRST_COMPLETED)
                self.runtime.check_task_policy(ctx.task_id)
                if not future.done():
                    entry['task'].result()
                    raise RuntimeError('browser_interrupted: no automatic action replay')
                result = future.result()
                text = result['text']
                if result['files']:
                    from .attachments import archive_binary
                    task = self.runtime.store.get_task(ctx.task_id)
                    cid = task.get('conversation_id')
                    if not cid:
                        return text + '\n截图未归档：本任务没有会话。'
                    for item in result['files']:
                        raw = base64.b64decode(item['body'], validate=True)
                        if len(raw) > 4 * 1024 * 1024 or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                            raise ValueError('browser_screenshot_denied')
                        file = archive_binary(self.runtime.store, cid, item['name'], raw, task_id=ctx.task_id, kind='browser_shot')
                        mid = self.runtime.store.add_conversation_message(cid, ctx.agent.id, 'assistant',
                            'Docker Browser 截图', task_id=ctx.task_id)
                        self.runtime.store._write('UPDATE attachments SET message_id=? WHERE id=?', (mid, file['id']))
                        await ctx.notify('conversation.message', {'message_id': mid})
                        text += '\n已归档截图：' + json.dumps({'id': file['id'], 'name': file['name'], 'sha256': file['sha256']}, ensure_ascii=False)
                if name == 'web_close':
                    await self.close_browsers(ctx.task_id, profile=profile)
                return text
            finally:
                entry['pending'] = None
                future.cancel()
        # A failed/ambiguous action is never replayed in another browser or on the host.

    async def stage_input(self, task_id, name, raw):
        return await self.submit(task_id,'action',{'op':'stage_input','name':name,
            'bytes':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()})

    async def validate_artifact(self, task_id, name, raw, checks):
        result=await self.submit(task_id,'action',{'op':'validate_artifact','name':name,
            'bytes':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest(),'checks':checks})
        if result.get('sha256')!=hashlib.sha256(raw).hexdigest():raise ValueError('validator_hash_conflict')
        return result

    async def close_browsers(self, run_id, *, profile=None):
        for key, entry in list(self.browser_sessions.items()):
            if key[0] != run_id or (profile is not None and key[1] != profile):
                continue
            try:
                if not entry['task'].done() and not entry['queue'].full():
                    entry['queue'].put_nowait({'close': True})
                    await asyncio.wait({entry['task']}, timeout=4)
            finally:
                entry['task'].cancel()
                await asyncio.gather(entry['task'], return_exceptions=True)
                self.browser_sessions.pop(key, None)

    def _browser_diag(self, kind: str, payload: dict) -> None:
        """临时诊断：把浏览器 RPC 的关键节点写入容器内 /runtime，便于事后取证。"""
        if kind == 'browser_next':
            return
        try:
            line = json.dumps({'t': round(time.time(), 3), 'kind': kind,
                               'payload': {k: (str(v)[:120] if not isinstance(v, (int, float)) else v)
                                           for k, v in (payload or {}).items() if k != 'body'}}, ensure_ascii=False)
            with open('/runtime/browser-diag.log', 'a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass

    async def browser_message(self, job, data):
        """Browser RPCs can overlap network loads. Accept each message once, never replay."""
        from .docker_browser import fetch_public
        if set(data) != {'sequence', 'event_id', 'kind', 'payload'}:
            raise ValueError('invalid_browser_message')
        if (type(data['sequence']) is not int or not 1 <= data['sequence'] <= 20000
                or not isinstance(data['event_id'], str) or len(data['event_id']) != 32):
            raise ValueError('browser_event_identity_denied')
        seen = job.setdefault('browser_seen_events', set())
        seqs = job.setdefault('browser_seen_sequences', set())
        if data['event_id'] in seen or data['sequence'] in seqs:
            raise ValueError('browser_replay_denied')
        seen.add(data['event_id']); seqs.add(data['sequence'])
        entry = job['callback']
        kind, payload = data['kind'], data['payload']
        self._browser_diag(kind, payload)
        if kind == 'browser_next' and payload == {}:
            try:
                async with asyncio.timeout(.5):
                    result = await entry['queue'].get()
            except TimeoutError:
                result = {}
        elif kind == 'browser_result' and set(payload) == {'id', 'text', 'files'}:
            pending = entry['pending']
            if (not pending or pending[0] != payload['id'] or pending[1].done()
                    or not isinstance(payload['text'], str) or len(payload['text'].encode()) > job['spec']['max_output_bytes'] + 100
                    or not isinstance(payload['files'], list) or len(payload['files']) > 2):
                raise ValueError('browser_result_scope_denied')
            self.check(job)
            pending[1].set_result(payload)
            result = {}
        elif kind == 'browser_approve' and set(payload) == {'kind', 'summary', 'detail'} and entry['pending']:
            from dataclasses import asdict
            result = asdict(await entry['ctx'].request_approval(**payload))
        elif kind == 'browser_fetch':
            entry['requests'] += 1
            if entry['requests'] > 1000 or entry['bytes'] > 64 * 1024 * 1024:
                return {'denied': 'browser_network_budget_exceeded'}
            imported_at = time.time()
            async with entry['network_slots']:
                try:
                    result = await fetch_public(payload, entry['safety'], lambda: self.check(job))
                    entry['bytes'] += len(result['body']) * 3 // 4
                    self._browser_diag('browser_fetch:end', {'url': str(payload.get('url', ''))[:120],
                        'status': result['status'], 'bytes': len(result['body']), 'seconds': round(time.time() - imported_at, 2)})
                except (ValueError, OSError, httpx.HTTPError) as exc:
                    self._browser_diag('browser_fetch:deny', {'url': str(payload.get('url', ''))[:120],
                        'error': f'{type(exc).__name__}: {exc}'[:160], 'seconds': round(time.time() - imported_at, 2)})
                    result = {'denied': 'browser_destination_or_response_denied'}
        else:
            raise ValueError('browser_audience_denied')
        self.check(job)
        return result

    async def pi(self, run_id: str, prompt: str, profile: dict, **kwargs):
        profile = validate_runtime_profile(profile)
        self.credential(profile)  # Identity must exist before creating a Worker.
        payload = {"prompt": prompt, "model": profile["model"],
                   "effort": kwargs.get("effort", ""), "tools": kwargs.get("tool_specs") or [],
                   "max_tool_calls": kwargs.get("max_tool_calls", 32)}
        try:
            result = await self.submit(run_id, "pi", payload, timeout=kwargs.get("timeout", 600),
                profile=profile, callback=kwargs.get("tool_execute"), on_stream=kwargs.get("on_stream"))
        except RuntimeError as exc:
            if str(exc) in {"model_provider_http_401", "model_provider_http_403"}:
                self.auth_status[profile["credential_ref"]] = "rejected"
            raise
        self.auth_status[profile["credential_ref"]] = "verified"
        return CliRunResult(text=result["text"], engine="pi", exit_code=0,yielded_tool=result.get('yield_tool'))

    def credential(self, profile):
        entry = self.runtime.config.isolation.get("credentials", {}).get(profile["credential_ref"])
        if not entry or entry.get("protocol") != "openai-completions":
            raise CliEngineError("carme_auth_required: dedicated openai-completions identity required")
        if set(entry) - {"protocol", "base_url", "models", "key_file", "ca_file", "allowed_ips"}:
            raise CliEngineError("credential_configuration_unsupported: proxy or extra fields are not silently ignored")
        if profile["model"] not in entry.get("models", []):
            raise CliEngineError("credential_model_denied")
        try:
            path = Path(entry["key_file"])
            roots = {Path(os.getenv("CARME_CREDENTIALS_DIR", "/run/secrets")).resolve(),
                     Path("/run/secrets").resolve()}
            if (not path.is_absolute() or path.parent not in roots or path.is_symlink()
                    or any(p in {".pi", ".ssh"} for p in path.parts)
                    or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 16384):
                raise ValueError()
            key = path.read_text().strip()
            if not key or any(c.isspace() for c in key) or key.startswith(("{", "[")):
                raise ValueError()
        except (OSError, KeyError, ValueError):
            raise CliEngineError("carme_auth_required") from None
        return entry, key

    async def model(self, job, data):
        """Fixed provider protocol + pinned destination, no Worker-selected URL/headers."""
        if not isinstance(data, dict) or set(data) != {"body"} or not isinstance(data["body"], dict):
            raise ValueError("invalid_model_request")
        entry, key = self.credential(job["profile"])
        body = data["body"]
        if body.get("model") != job["profile"]["model"].split("/", 1)[1]:
            raise ValueError("model_denied")
        job["model_calls"] += 1
        if job["model_calls"] > len(job["spec"]["tools"]) + 64:
            raise ValueError("model_call_limit")
        url = urlsplit(entry["base_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.port not in (None, 443):
            raise ValueError("model_endpoint_requires_https_443")
        addresses = {r[4][0] for r in await asyncio.to_thread(socket.getaddrinfo, url.hostname, 443, type=socket.SOCK_STREAM)}
        pinned = entry.get("allowed_ips", [])  # Optional administrator-owned private model endpoint.
        if not addresses or any((not ipaddress.ip_address(a).is_global and a not in pinned)
                                or ipaddress.ip_address(a).is_link_local for a in addresses):
            raise ValueError("model_endpoint_denied")
        address = sorted(addresses)[0]
        destination = f"https://{'[' + address + ']' if ':' in address else address}{url.path.rstrip('/')}/chat/completions"
        verify = ssl.create_default_context(cafile=entry.get("ca_file"))
        async with httpx.AsyncClient(trust_env=False, verify=verify, follow_redirects=False, timeout=90) as client:
            req = client.build_request("POST", destination, json=body,
                headers={"Host": url.hostname, "Authorization": "Bearer " + key,
                         **provider_session_headers(SimpleNamespace(base_url=entry["base_url"]),
                                                    job["spec"]["run_id"])},
                extensions={"sni_hostname": url.hostname})
            response = await client.send(req, stream=True)
            try:
                if response.status_code != 200:
                    # Never forward arbitrary provider errors containing request credentials.
                    raise RuntimeError(f"model_provider_http_{response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    self.check(job)
                    raw.extend(chunk)
                    if len(raw) > 4 * 1024 * 1024:
                        raise RuntimeError("model_output_limit")
                return {"body": base64.b64encode(raw).decode(),
                        "content_type": "text/event-stream" if body.get("stream") else "application/json"}
            finally:
                await response.aclose()

    async def message(self, run_id, token, data):
        job = next((j for j in self.jobs.values() if j["token"] and
                    secrets.compare_digest(j["token"], token)), None)
        if not job or job["spec"]["run_id"] != run_id or job["spec"]["role"] not in {"pi", "browser"}:
            raise HTTPException(403, "task_token_denied")
        self.check(job)
        if job.get('yield_tool'):raise HTTPException(403,'pi_yielded_waiting_cleanup')
        if job['spec']['role'] == 'browser':
            return await self.browser_message(job, data)
        if set(data) != {"sequence", "event_id", "kind", "payload"}:
            raise ValueError("invalid_worker_message")
        if type(data["sequence"]) is not int or not isinstance(data["event_id"], str) or not 1 <= len(data["event_id"]) <= 128:
            raise ValueError("invalid_worker_event_identity")
        async with job["lock"]:
            self.check(job)
            fingerprint = hashlib.sha256(encoded(data)).hexdigest()
            prior = job["messages"].get(data["event_id"])
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError("event_id_conflict")
                return prior[1]
            if data["sequence"] != job["sequence"] + 1 or job["sequence"] >= 4096:
                raise ValueError("event_sequence_denied")
            job["sequence"] += 1
            kind, payload = data["kind"], data["payload"]
            if kind == "tool":
                if (set(payload) != {"name", "arguments"} or payload["name"] not in job["spec"]["tools"]
                        or not isinstance(payload["arguments"], dict) or job["callback"] is None):
                    raise ValueError("tool_scope_denied")
                if payload['name']=='delegate':
                    job['yield_tool']={**payload,'id':data['event_id']}
                    result='Carme 正在释放父 Pi 容器，再执行委派。'
                else:
                    result = await job["callback"](payload["name"], payload["arguments"])
            elif kind == "model":
                result = await self.model(job, payload)
            elif kind == "stream":
                if set(payload) - {"message_id", "content", "model", "provider", "status"}:
                    raise ValueError("event_identity_override")
                if len(str(payload.get("content", "")).encode()) > job["spec"]["max_output_bytes"]:
                    raise ValueError("worker_output_limit")
                if job["stream"]:
                    await job["stream"](payload)
                result = None
            else:
                raise ValueError("worker_audience_denied")
            self.check(job)
            job["messages"][data["event_id"]] = (fingerprint, result)
            # Deduplicate recent receipts; old sequence numbers are rejected without replay.
            while len(job["messages"]) > 8:
                job["messages"].pop(next(iter(job["messages"])))
            return result


def build_execution_router(execution: Execution):
    router = APIRouter()

    @router.post('/internal/mac')
    async def mac(request: Request):
        raw=await bounded_body(request,3_000_000);nonce=request.headers.get('X-Carme-Nonce','')
        try:
            key=execution.mac_key();timestamp,random=nonce.split(':',1)
            if not math.isfinite(float(timestamp)) or abs(time.time()-float(timestamp))>5 or len(random)!=32:raise ValueError()
            if not hmac.compare_digest(signature(key,nonce,raw),request.headers.get('X-Carme-Signature','')):raise ValueError()
            execution.nonces={n:t for n,t in execution.nonces.items() if t>time.time()-60}
            if 'mac:'+nonce in execution.nonces:raise ValueError()
            execution.nonces['mac:'+nonce]=time.time()
            data=json.loads(raw)
            if data['runner_id']!=execution.runtime.config.isolation['mac_runner']['runner_id']:raise ValueError()
        except (RuntimeError,ValueError,KeyError,TypeError):raise HTTPException(403,'mac_runner_auth_denied') from None
        result={};execution.mac_seen=time.time()
        if data.get('op')=='claim' and set(data)=={'op','runner_id','authorized'} and type(data['authorized']) is bool:
            execution.mac_authorized=data['authorized']
            if not data['authorized']:
                for job in execution.mac_jobs.values():
                    if not job['future'].done():job['future'].set_result({'error':'human_takeover_or_local_grant_revoked'})
                # 授权撤销：丢掉人工画面/控制通道，前端会退回浏览器画面。
                execution.mac_control_enabled=False;execution.mac_control_queue.clear()
                for waiter in execution.mac_control_waiters.values():
                    if not waiter.done():waiter.set_result({'error':'local_grant_revoked'})
                execution.mac_control_waiters.clear()
                execution.mac_screen_until=0.0
            else:
                for job in execution.mac_jobs.values():
                    if job['future'].done() or job['lease'] or time.time()>=job['spec']['deadline']:continue
                    try:execution.runtime.check_task_policy(job['spec']['run_id'])
                    except RuntimeError:continue
                    job['lease']=secrets.token_hex(16);result={'job':job['spec'],'lease':job['lease']};break
                # 人工桌面：有人在看画面就让 Runner 抓帧；控制开着就带上排队的人工动作。
                if time.time()<execution.mac_screen_until and (not execution.mac_frame or time.time()-execution.mac_frame_at>0.35):
                    result['screen']=True
                if execution.mac_control_enabled and execution.mac_control_queue:
                    batch=execution.mac_control_queue[:6];del execution.mac_control_queue[:len(batch)]
                    result['control']=batch
        elif data.get('op')=='screen' and set(data)=={'op','runner_id','frame','mtime','width','height'}:
            import base64
            try:
                frame=base64.b64decode(data['frame'],validate=True)
                mtime=float(data['mtime']);width,height=int(data['width']),int(data['height'])
                if not frame.startswith(b'\xff\xd8') or not math.isfinite(mtime) or not (0<width<20000 and 0<height<20000):raise ValueError()
            except (ValueError,TypeError):raise HTTPException(400,'invalid_frame') from None
            execution.mac_frame=frame;execution.mac_frame_at=mtime if mtime<=time.time()+5 else time.time()
            execution.mac_frame_size=(width,height)
            waiters,execution.mac_screen_waiters=execution.mac_screen_waiters,[]
            for waiter in waiters:
                if not waiter.done():waiter.set_result(True)
            result={'accepted':True}
        elif data.get('op')=='control_results' and set(data)=={'op','runner_id','results'} and isinstance(data['results'],list):
            for item in data['results'][:12]:
                waiter=execution.mac_control_waiters.pop(item.get('id'),None) if isinstance(item,dict) else None
                if waiter is not None and not waiter.done():waiter.set_result(item.get('result') if item.get('ok') else {'error':str(item.get('error') or 'control_failed')})
            result={'accepted':True}
        elif data.get('op') in {'finish','authorize'} and set(data)==({'op','runner_id','job_id','lease','result'} if data['op']=='finish' else {'op','runner_id','job_id','lease'}):
            job=execution.mac_jobs.get(data['job_id'])
            if job and job['lease'] and hmac.compare_digest(job['lease'],data['lease']) and not job['future'].done() and time.time()<job['spec']['deadline']:
                execution.runtime.check_task_policy(job['spec']['run_id'])
                if data['op']=='finish':job['future'].set_result(data['result'])
                result={'accepted':True}
        else:raise HTTPException(400,'native_operation_denied')
        body=encoded(result);return Response(body,media_type='application/json',headers={'X-Carme-Signature':signature(key,nonce,body)})

    @router.post("/internal/broker")
    async def broker(request: Request):
        raw = await bounded_body(request)
        nonce = request.headers.get("X-Carme-Nonce", "")
        supplied = request.headers.get("X-Carme-Signature", "")
        try:
            key = execution.key()
            timestamp, random = nonce.split(":", 1)
            if (not math.isfinite(float(timestamp)) or len(random) != 32
                    or abs(time.time() - float(timestamp)) > NONCE_WINDOW_SECONDS):
                raise ValueError()
            if not hmac.compare_digest(signature(key, nonce, raw), supplied):
                raise ValueError()
            execution.nonces = {n: t for n, t in execution.nonces.items() if t > time.time() - 60}
            if nonce in execution.nonces:
                raise ValueError()
            execution.nonces[nonce] = time.time()
        except (ValueError, RuntimeError):
            raise HTTPException(403, "broker_auth_denied") from None
        data = json.loads(raw)
        result = {}
        if data.get("op") == "claim" and set(data) == {"op", "health", "instance_id", "capacity"}:
            if data["instance_id"] != execution.settings["instance_id"]:
                raise HTTPException(403, "instance_denied")
            execution.broker_seen = time.time()
            execution.broker_health = data["health"]
            for job in execution.jobs.values():
                if not job["lease"] and job["spec"]["role"] in {'pi','action','browser'} and job["spec"]["role"] in data["capacity"]:
                    try:
                        execution.check(job)
                    except RuntimeError:
                        continue
                    job["lease"] = secrets.token_hex(16)
                    job["lease_until"] = time.time() + LEASE_SECONDS
                    result = {"spec": job["spec"], "lease": job["lease"],
                              "task_token": job["token"], "lease_seconds": LEASE_SECONDS}
                    break
        elif data.get("op") in {"renew", "finish"} and set(data) <= {"op", "job_id", "lease", "result"}:
            job = execution.jobs.get(data.get("job_id"))
            if not job or not secrets.compare_digest(job["lease"], data.get("lease", "")) or not job["lease"]:
                result = {"active": False}
            else:
                try:
                    execution.check(job)
                    if time.time() > job["lease_until"]:
                        raise RuntimeError("lease_expired")
                except RuntimeError:
                    result = {"active": False}
                else:
                    execution.broker_seen = time.time()
                    job["lease_until"] = time.time() + LEASE_SECONDS
                    result = {"active": True}
                    if data['op']=='renew' and job.get('yield_tool'):result={'active':False}
                    if data["op"] == "finish":
                        job["future"].set_result(data["result"])
        else:
            raise HTTPException(400, "invalid_broker_operation")
        body = encoded(result)
        return Response(body, media_type="application/json", headers={"X-Carme-Signature": signature(key, nonce, body)})

    @router.post("/internal/runs/{run_id}/messages")
    async def worker(run_id: str, request: Request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer tk_"):
            raise HTTPException(403, "worker_auth_required")
        try:
            data = json.loads(await bounded_body(request, 5 * 1024 * 1024))
            result = await execution.message(run_id, auth[7:], data)
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("model_provider_http_"):
                body = encoded({"error": str(exc)})
                return Response(body, media_type="application/json", headers={
                    "X-Carme-Signature": signature(auth[7:].encode(), data["event_id"], body)})
            raise HTTPException(403, str(exc)[:160]) from None
        body = encoded({"result": result})
        return Response(body, media_type="application/json", headers={
            "X-Carme-Signature": signature(auth[7:].encode(), data["event_id"], body)})

    return router
