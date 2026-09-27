"""Control-owned jobs. Only the separately authenticated Broker can lease them.

Workers have no network. Their bounded JSON messages travel over Docker stdio
and the Broker relay; task credentials never authorize container management.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
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
from fastapi.responses import Response, StreamingResponse

from .engines import CliEngineError, CliRunResult, validate_runtime_profile
from .llm import provider_session_headers
from .security import execution_diagnostic
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
MODEL_IDLE_SECONDS = 300
MODEL_CONNECT_SECONDS = 15


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
        self.desktop_requests: dict[str, dict] = {}
        self.desktop_ready = asyncio.Event()
        self.desktop_http: dict[str, dict] = {}
        self.desktop_controls: dict[str, dict] = {}
        self.desktop_sessions: dict[str, str] = {}
        self.host_grant: dict = {}
        self.host_screen_size = (0, 0)

    def desktop_bot(self, bot_id):
        if not self.runtime.config.isolation.get('desktop'):
            raise RuntimeError('bot_desktop_not_configured')
        spec = self.runtime.config.agents.agents.get(bot_id)
        if spec is None or spec.execution_target != 'container':
            raise ValueError('bot_desktop_target_denied')
        return spec

    def desktop_target(self, bot_id):
        grant = self.host_grant
        if (os.getenv('CARME_ACCOUNT_ID') == 'main' and self.runtime.config.isolation.get('mac_runner', {}).get('computer_use')
                and time.time() - self.mac_seen < 5 and grant.get('expires', 0) > time.time()
                and grant.get('bot_id') in {'*', bot_id} and grant.get('generation')):
            return 'host:' + grant['generation']
        return 'linux'

    def desktop_control(self, bot_id):
        self.desktop_bot(bot_id)
        target = self.desktop_target(bot_id); key = '@host' if target.startswith('host:') else bot_id
        value = self.desktop_controls.setdefault(key, {'enabled': False, 'id': '', 'expires': 0, 'epoch': 0, 'target': target})
        if value['target'] != target:
            value.update(enabled=False, id='', expires=0, epoch=value['epoch'] + 1, target=target)
        if value['enabled'] and time.time() >= value['expires']:
            value.update(enabled=False, id='', epoch=value['epoch'] + 1)
        return value

    def check_desktop_request(self, request, *, finished=False):
        self.desktop_bot(request['bot_id'])
        if request.get('target', 'linux') != self.desktop_target(request['bot_id']): raise RuntimeError('desktop_target_changed_no_replay')
        if (request['future'].done() and not finished) or time.time() >= request['deadline']:
            raise RuntimeError('desktop_request_expired')
        control = self.desktop_control(request['bot_id'])
        if request['operation'] not in {'status', 'screenshot'} and request['epoch'] != control['epoch']:
            raise RuntimeError('desktop_control_changed')
        if request['ctx']:
            self.runtime.check_task_policy(request['ctx'].task_id)
            if control['enabled']: raise RuntimeError('desktop_under_external_control')
        elif request['operation'] in {'mouse', 'keyboard'}:
            if not control['enabled'] or not secrets.compare_digest(request['control_id'], control['id']):
                raise RuntimeError('desktop_external_control_required')

    async def desktop_request(self, bot_id, operation, arguments=None, *, ctx=None, control_id=''):
        from .docker_desktop import validate
        if ctx and self.runtime.store.task_context(ctx.task_id)['context_mode'] == 'visitor_group':
            raise ValueError('common_desktop_denied')
        self.desktop_bot(bot_id); arguments = validate(operation, arguments or {})
        target = self.desktop_target(bot_id)
        if target == 'linux' and self.health()['broker'] != 'ready': raise RuntimeError('desktop_broker_unavailable')
        if ctx: self.runtime.check_task_policy(ctx.task_id)
        if target.startswith('host:') and operation not in {'status', 'screenshot', 'mouse', 'keyboard', 'release', 'help', 'find_roots', 'observe_ui', 'search_ui', 'expand_ui', 'inspect_ui', 'act_ui', 'read_text', 'wait_for', 'navigate_browser'}:
            raise RuntimeError('宿主电脑通过 bot_computer 的界面工具操作；软件安装请先退出宿主授权，使用独立 Linux 电脑。')
        if len(self.desktop_requests) >= 24: raise RuntimeError('desktop_queue_busy')
        control = self.desktop_control(bot_id)
        if ctx and ctx.agent.id != bot_id: raise ValueError('desktop_bot_scope_denied')
        if ctx and control['enabled']: raise RuntimeError('desktop_under_external_control')
        if not ctx and operation not in {'status', 'screenshot', 'mouse', 'keyboard', 'release'}:
            raise ValueError('desktop_human_operation_denied')
        if operation in {'mouse', 'keyboard'} and (not control['enabled'] or not secrets.compare_digest(control_id, control['id'])):
            raise RuntimeError('desktop_external_control_required')
        if operation in {'mouse', 'keyboard', 'status', 'screenshot'} and control_id and secrets.compare_digest(control_id, control['id']):
            control['expires'] = time.time() + 90
        identity = secrets.token_hex(16); future = asyncio.get_running_loop().create_future()
        request = {'id': identity, 'bot_id': bot_id, 'operation': operation, 'arguments': arguments,
            'deadline': time.time() + 85, 'epoch': control['epoch'], 'control_id': control_id,
            'ctx': ctx, 'future': future, 'claimed': False, 'target': target}
        if not ctx and operation in {'status', 'screenshot'}:
            shared = next((r for r in self.desktop_requests.values() if not r['ctx']
                and r['bot_id'] == bot_id and r['operation'] == operation and r['target'] == target
                and r['control_id'] == control_id and r['epoch'] == control['epoch']
                and not r['future'].done() and time.time() < r['deadline']), None)
            if shared:
                request = shared; identity = shared['id']; future = shared['future']
        request['waiters'] = request.get('waiters', 0) + 1
        self.desktop_requests[identity] = request
        self.desktop_ready.set()
        try:
            while True:
                pending = {t for t in request.get('network_tasks', ()) if not t.done()}
                if not future.done(): pending.add(future)
                if not pending: break
                self.check_desktop_request(request, finished=future.done())
                await asyncio.wait(pending, timeout=.25)
            self.check_desktop_request(request, finished=True)
            result = dict(future.result())
            if isinstance(result, dict) and result.get('error'):
                if operation == 'fetch' and target == 'linux' and result.get('effect') == 'not_performed':
                    from .docker_desktop import DesktopNoEffectError
                    status = result.get('http_status')
                    raise DesktopNoEffectError(result['error'], status_code=status if type(status) is int and 100 <= status <= 599 else None)
                raise RuntimeError(result['error'])
            state = result if operation == 'status' else result.get('state') if operation == 'screenshot' else None
            if isinstance(state, dict):
                state.update(control_enabled=control['enabled'] and bool(control_id) and secrets.compare_digest(control_id, control['id']),
                    externally_controlled=control['enabled'], bot_id=bot_id, desktop_target=target)
            if operation == 'status' and target.startswith('host:'):
                self.host_screen_size = (result.get('screen_width', 0), result.get('screen_height', 0))
            result['desktop_target'] = target
            if request.get('network_denied'):
                result['text'] = result.get('text', '') + '\n[对外请求未执行] ' + request['network_denied'] + '。不要自动重试或换工具绕过。'
            if request.get('network_sent'):
                result['text'] = result.get('text', '') + '\n[对外请求] 本次已批准并传输 ' + str(request['network_sent']) + ' 个请求；请观察结果，勿重复提交。'
            return result
        finally:
            request['waiters'] -= 1
            if not request['waiters']:
                future.cancel(); self.desktop_requests.pop(identity, None)
                for task in request.get('network_tasks', ()): task.cancel()

    async def desktop_fetch(self, bot_id, session_id, payload):
        """All desktop browser traffic, including shell/native UI clicks, crosses this gate."""
        from urllib.parse import urlsplit
        from .docker_browser import fetch_public, is_submission
        from .security import digest
        def check_session():
            if self.desktop_sessions.get(bot_id) != session_id or self.desktop_target(bot_id) != 'linux':
                raise ValueError('desktop_session_revoked')
        check_session()
        current = None
        if is_submission(payload) and not self.desktop_control(bot_id)['enabled']:
            current = next((r for r in self.desktop_requests.values() if r['bot_id'] == bot_id
                            and r['claimed'] and r['ctx'] and r.get('target', 'linux') == 'linux'), None)
            if current is None:
                raise ValueError('external_submission_requires_active_task')
            self.check_desktop_request(current, finished=True)
            if current.get('network_tasks'):
                raise ValueError('external_submission_pending: do not duplicate or retry')
            current.setdefault('network_tasks', set()).add(asyncio.current_task())
        manual_epoch = self.desktop_control(bot_id)['epoch']
        binding = digest(payload)
        def check():
            check_session()
            if current:
                self.check_desktop_request(current, finished=True)
            elif is_submission(payload):
                control = self.desktop_control(bot_id)
                if not control['enabled'] or control['epoch'] != manual_epoch:
                    raise ValueError('desktop_external_control_changed')
        async def authorize():
            if current is None: return  # The user is operating the desktop with the control switch on.
            ctx = current['ctx']; url = urlsplit(payload['url'])
            # Keep the task and transport alive while the existing chat approval waits.
            current['deadline'] = min(self.runtime._meta(self.runtime.store.get_task(ctx.task_id))['deadline'], time.time() + 650)
            decision = await ctx.request_approval(kind='external_submission', summary=f'向 {url.hostname} 提交数据', detail={
                'destination': url.scheme + '://' + url.netloc + url.path, 'method': payload['method'],
                'body_bytes': len(base64.b64decode(payload['body'], validate=True)), 'request_digest': binding,
                'scope': '仅本次请求；可能发送消息、上传文件或修改远端数据。凭据和正文不写入审批记录。'})
            check()
            if digest(payload) != binding: raise ValueError('external_submission_changed')
            if not decision.approved: raise ValueError('用户拒绝了对外提交')
        try:
            result = await fetch_public(payload, dict(self.runtime.config.browser.safety), check,
                pool=self.desktop_http.setdefault(bot_id, {}), authorize=authorize)
            if current: current['network_sent'] = current.get('network_sent', 0) + 1
            return result
        except BaseException as exc:
            if current: current['network_denied'] = '对外请求被拒绝或中断（' + type(exc).__name__ + '）'
            raise
        finally:
            if current: current['network_tasks'].discard(asyncio.current_task())

    async def set_desktop_control(self, bot_id, enabled, control_id=''):
        target = self.desktop_target(bot_id)
        value = self.desktop_control(bot_id)
        if enabled:
            if any((r['bot_id'] == bot_id or (self.desktop_target(bot_id).startswith('host:') and r.get('target', '').startswith('host:'))) and r['ctx'] and (not r['future'].done() or r.get('network_tasks')) for r in self.desktop_requests.values()):
                raise RuntimeError('Bot 正在操作桌面，请等待当前动作完成后开启外部控制。')
            if value['enabled'] and not secrets.compare_digest(control_id, value['id']):
                raise RuntimeError('另一个页面正在控制这个 Bot；请先在原页面关闭外部控制。')
            value.update(enabled=True, id=secrets.token_urlsafe(24), expires=time.time() + 90, epoch=value['epoch'] + 1)
        else:
            if value['enabled'] and not secrets.compare_digest(control_id, value['id']): raise RuntimeError('desktop_control_owner_mismatch')
            value.update(enabled=False, id='', expires=0, epoch=value['epoch'] + 1)
        with contextlib.suppress(Exception): await self.desktop_request(bot_id, 'release')
        if target != self.desktop_target(bot_id):
            value.update(enabled=False, id='', expires=0, epoch=value['epoch'] + 1)
            raise RuntimeError('desktop_target_changed_no_replay')
        width, height = self.host_screen_size if target.startswith('host:') else (1280, 800)
        return {'enabled': True, 'available': True, 'mode': 'host' if target.startswith('host:') else 'docker', 'bot_id': bot_id,
                'desktop_target': target, 'control_enabled': value['enabled'], 'control_id': value['id'],
                'screen_width': width, 'screen_height': height}

    async def desktop_result(self, ctx, result):
        from .attachments import archive_binary
        task = self.runtime.store.get_task(ctx.task_id); cid = task.get('conversation_id')
        text = result.get('text', '')
        files = list(result.get('files', []))
        for item in result.get('content', []):
            if item.get('type') == 'text': text += '\n' + item.get('text', '')
            elif item.get('type') == 'image' and item.get('mimeType') == 'image/png':
                files.append({'name': 'bot-desktop.png', 'body': item['data']})
        for item in files:
            raw = base64.b64decode(item['body'], validate=True)
            if len(raw) > 4 * 1024 * 1024 or not raw.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('desktop_image_denied')
            if cid:
                file = archive_binary(self.runtime.store, cid, item['name'], raw, task_id=ctx.task_id, kind='browser_shot')
                text += '\n截图：' + json.dumps({'id': file['id'], 'name': file['name']}, ensure_ascii=False)
        return text.strip() or json.dumps(result, ensure_ascii=False)

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
                "browser": self.broker_health.get("desktop" if self.runtime.config.isolation.get('desktop') else "browser", "not_configured") if fresh else "unavailable",
                "desktop": self.broker_health.get("desktop", "not_configured") if fresh else "unavailable",
                "web_route": "Bot Linux Desktop" if self.runtime.config.isolation.get('desktop') else "Docker Browser",
                "native_route": "Mac Runner (explicit local grant)",
                "mac_runner": ("authorized" if self.mac_authorized or self.host_grant.get('expires', 0) > time.time() else "connected_needs_local_grant") if time.time()-self.mac_seen<5 else
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

    def diagnostic(self, job, stage, **fields):
        execution_diagnostic(self.runtime.store.path.parent, stage,
            task_id=job['spec']['run_id'], job_id=job['spec']['job_id'], **fields)

    async def submit(self, run_id: str, role: str, payload: dict, *, timeout=120,
                     profile=None, callback=None, on_stream=None):
        self.key()  # Missing pairing never becomes a local executor.
        self.runtime.check_task_policy(run_id)
        task = self.runtime.store.get_task(run_id)
        meta = self.runtime._meta(task)
        if meta["execution_target"] != "container":
            raise RuntimeError("target_unassigned")
        if self.runtime.store.task_context(run_id)['context_mode'] == 'visitor_group':
            policy = meta['policy']
            if policy.get('context_mode') != 'visitor_group' or policy.get('context_epoch') != self.runtime.store.context_epoch(run_id):
                raise ValueError('common_policy_required')
            if role not in {'action', 'pi'}:
                raise ValueError('common_worker_denied')
            if role == 'action':
                op = payload.get('op')
                if op not in {'exec', 'export', 'stage_input', 'validate_artifact'}:
                    raise ValueError('common_action_denied')
                if op == 'stage_input':
                    parts = payload.get('name', '').split('/')
                    if len(parts) != 3 or parts[0] != 'artifacts':
                        raise ValueError('common_input_denied')
                    file = self.runtime.store.artifact_access(run_id, parts[1])
                    # Uploads may have no stored hash; the bound task input always has one.
                    expected = next((item['sha256'] for item in meta.get('envelope', {}).get('input_artifacts', [])
                                     if item['id'] == file['id']), '')
                    if (parts[2] != file['name'] or not re.fullmatch(r'[a-f0-9]{64}', expected)
                            or payload.get('sha256') != expected):
                        raise ValueError('common_input_denied')
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
                detail = str(result['error'])
                code = next((code for marker, code in (
                    ('pi_turn_incomplete', 'pi_turn_incomplete'), ('pi_empty_response', 'pi_empty_response'),
                    ('返回空输出', 'pi_empty_response'), ('TimeoutError', 'worker_timeout'),
                    ('lease', 'lease_interrupted'), ('docker_operation_failed', 'docker_operation_failed'))
                    if marker in detail), 'worker_reported_failure')
                self.diagnostic(job, 'worker.result', code=code)
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            self.diagnostic(job, 'execution.wait', error=exc)
            raise
        finally:
            future.cancel()
            job["token"] = ""  # Revoke immediately; a stale relay cannot issue another action.
            job["callback"] = job["stream"] = None
            # Broker lease polling sees absence and kills only this job's container.
            self.jobs.pop(job_id, None)

    async def common_query(self, ctx, name, arguments):
        """No ambient browser identity; every non-exact public query needs owner consent."""
        from .docker_browser import fetch_public, is_submission, web_url, MAX_UPLOAD
        from .tools.browser import WebSearchTool, html_to_text
        from .attachments import file_path
        from .security import digest
        from urllib.parse import quote_plus
        import copy
        task = self.runtime.store.get_task(ctx.task_id)
        store = self.runtime.store
        args = copy.deepcopy(arguments)
        if store.task_context(ctx.task_id)['context_mode'] != 'visitor_group':
            raise ValueError('common_context_required')
        if name not in {'web_search', 'fetch_page'} or args.get('render'):
            raise ValueError('common_query_capability_denied')
        fields = {'query','max_results'} if name == 'web_search' else {'url','max_chars','render','method','body','headers','attachment_id'}
        if set(args) - fields:
            raise ValueError('common_query_arguments_denied')
        links = ctx.extras.setdefault('common_query_links', set())
        attachment = None
        if name == 'web_search':
            query = args.get('query')
            if not isinstance(query,str) or not query or len(query)>2000:
                raise ValueError('public_query_invalid')
            url = 'https://html.duckduckgo.com/html/?q=' + quote_plus(query)
            public = not task.get('parent_id') and task['goal'] == '公开查询：' + query
            method, headers, raw = 'GET', {}, b''
        else:
            url = args.get('url'); method = args.get('method','GET')
            headers = args.get('headers',{});body = args.get('body','')
            if (method not in {'GET','HEAD','POST','PUT','PATCH','DELETE'} or not isinstance(headers,dict)
                    or len(headers)>20 or any(not isinstance(k,str) or not isinstance(v,str) for k,v in headers.items())
                    or not isinstance(body,str) or any(k.lower() in {'cookie','authorization','proxy-authorization','host'} for k in headers)):
                raise ValueError('common_request_invalid')
            raw = body.encode('utf-8')
            if args.get('attachment_id'):
                if body:raise ValueError('common_body_conflict')
                attachment = store.artifact_access(ctx.task_id,args['attachment_id'])
                raw = file_path(store,attachment['id']).read_bytes()
            public = url in links and method == 'GET' and not raw and not headers
        url = web_url(url)
        if len(raw)>MAX_UPLOAD:raise ValueError('common_upload_limit')
        data = {'url':url,'method':method,'headers':headers,'body':base64.b64encode(raw).decode()}
        public = public and not is_submission(data)
        origin = copy.deepcopy(self.runtime._meta(task)['origin'])
        effect_hash = digest({'request':data,'origin':origin})
        def check():
            store.task_context(ctx.task_id)
            self.runtime.check_task_policy(ctx.task_id)
            if attachment:
                current = store.artifact_access(ctx.task_id,attachment['id'])
                if hashlib.sha256(file_path(store,current['id']).read_bytes()).hexdigest() != hashlib.sha256(raw).hexdigest():
                    raise ValueError('artifact_version_conflict')
        check()
        async def request(destination):
            if destination != url:raise ValueError('public_query_destination_changed')
            check()
            count = ctx.extras.get('common_query_requests',0)
            if count >= 20:raise ValueError('common_query_budget_exceeded')
            ctx.extras['common_query_requests'] = count+1
            operation = None
            if not public:
                expires = min(time.time()+300, self.runtime._meta(store.get_task(ctx.task_id)).get('deadline',time.time()+300))
                detail = {'request':copy.deepcopy(data),'body_text':raw.decode('utf-8',errors='replace') if not attachment else '',
                          'body_sha256':hashlib.sha256(raw).hexdigest(),'origin':origin,'expires_at':expires,
                          'attachment':{k:attachment[k] for k in ('id','name','sha256')} if attachment else None}
                outcome = await ctx.request_approval(kind='visitor_http',summary='群任务请求向外发送或查询数据',detail=detail)
                check()
                if not outcome.approved:raise ValueError('owner_approval_required')
                if time.time()>=expires:raise ValueError('approval_expired')
                with store.transaction():
                    check()
                    operation = store.operation_begin(ctx.task_id,'visitor_http',effect_hash)
                if operation['status']=='finished':
                    result=json.loads(operation['result'])
                    return httpx.Response(result['status'],headers=result['headers'],content=base64.b64decode(result['body']),request=httpx.Request(method,url))
                if operation['status']!='new':raise ValueError('external_effect_reconciliation_required')
            # fetch_public resolves and pins public IPs, never follows redirects or ambient cookies.
            result = await fetch_public(data,self.runtime.config.browser.safety,check)
            if operation:
                store.operation_finish(operation['id'],json.dumps(result),receipt={'request_sha256':effect_hash,'status':result['status']})
            check()
            response = httpx.Response(result['status'],headers=result['headers'],content=base64.b64decode(result['body']),request=httpx.Request(method,url))
            if response.is_redirect:raise ValueError('public_query_redirect_requires_new_approval')
            return response
        if name == 'web_search':
            rows = await WebSearchTool()._duckduckgo(None,query,max(1,min(int(args.get('max_results',8)),20)),request=request)
            for row in rows:
                if len(links)<100:links.add(row['url'])
            result = json.dumps(rows,ensure_ascii=False)
        else:
            response = await request(url)
            result = 'HTTP '+str(response.status_code)+'\n'+html_to_text(response.text)[:max(500,min(int(args.get('max_chars',12000)),60000))]
        check()
        return result

    async def browser_tool(self, ctx, name, arguments):
        from .docker_browser import WEB_TOOLS, PROFILE, DEFAULT_DANGER
        self.runtime.check_task_policy(ctx.task_id)
        meta = self.runtime._meta(self.runtime.store.get_task(ctx.task_id))
        if (meta['execution_target'] != 'container' or name not in WEB_TOOLS
                or name not in meta['policy']['tools'] or not self.runtime.config.browser.enabled):
            raise RuntimeError('docker_browser_capability_denied')
        if self.runtime.store.task_context(ctx.task_id)['context_mode'] == 'visitor_group':
            return await self.common_query(ctx, name, arguments)
        if self.runtime.config.isolation.get('desktop'):
            result = await self.desktop_request(ctx.agent.id, 'web', {'name': name, 'arguments': arguments}, ctx=ctx)
            return await self.desktop_result(ctx, result)
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
        if 'messages' in kwargs:
            task = self.runtime.store.get_task(run_id)
            session = self.runtime._meta(task).get('pi_session')
            if not session or session['scope'] != task.get('conversation_id'):
                raise ValueError('pi_session_binding_required')
            payload.update(session={**session, 'task_id': run_id}, messages=kwargs['messages'],
                           system_prompt=kwargs.get('system_prompt', ''))
        payload['model_limits'] = {'context_window': profile.get('context_window', 32768),
                                  'max_output_tokens': profile.get('max_output_tokens', 8192)}
        try:
            result = await self.submit(run_id, "pi", payload, timeout=kwargs.get("timeout", 600),
                profile=profile, callback=kwargs.get("tool_execute"), on_stream=kwargs.get("on_stream"))
        except RuntimeError as exc:
            if str(exc) in {"model_provider_http_401", "model_provider_http_403"}:
                self.auth_status[profile["credential_ref"]] = "rejected"
            raise
        self.auth_status[profile["credential_ref"]] = "verified"
        if payload.get('session') and 'context_summary' in result:
            summary = result['context_summary']
            if summary is not None:
                if (not isinstance(summary, dict) or not isinstance(summary.get('content'), str)
                        or len(summary['content']) > 262144 or not isinstance(summary.get('updated_at'), (int, float))):
                    raise ValueError('pi_summary_invalid')
                summary = {**summary, 'model': 'Pi · ' + profile['model']}
            self.runtime.store.update_task_meta(run_id, {'pi_summary': summary})
            await self.runtime._emit('conversation.summary', {'source': 'pi'}, run_id, task['agent_id'])
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
        """Authenticated bounded frames; provider credentials and error bodies stay in Control."""
        started = time.monotonic()
        owner = job.get('model_active')
        def release():
            if job.get('model_active') == owner:
                job['model_active'] = False
        total = chunks = 0
        stage = 'model.validate'
        try:
            if not isinstance(data, dict) or set(data) != {"body"} or not isinstance(data["body"], dict):
                raise ValueError("invalid_model_request")
            entry, key = self.credential(job["profile"])
            body = data["body"]
            if body.get("model") != job["profile"]["model"].split("/", 1)[1]:
                raise ValueError("model_denied")
            job["model_calls"] += 1
            if job["model_calls"] > len(job["spec"]["tools"]) + 64:
                raise ValueError("model_call_limit")
            self.diagnostic(job, stage, attempt=job['model_calls'])
            # Acknowledgement lets the Broker read progress RPCs while inference is in flight.
            yield {'type': 'accepted'}
            url = urlsplit(entry["base_url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.port not in (None, 443):
                raise ValueError("model_endpoint_requires_https_443")
            stage = 'model.dns'
            async with asyncio.timeout(MODEL_CONNECT_SECONDS):
                addresses = {r[4][0] for r in await asyncio.to_thread(socket.getaddrinfo, url.hostname, 443, type=socket.SOCK_STREAM)}
            pinned = entry.get("allowed_ips", [])
            if not addresses or any((not ipaddress.ip_address(a).is_global and a not in pinned)
                                    or ipaddress.ip_address(a).is_link_local for a in addresses):
                raise ValueError("model_endpoint_denied")
            address = sorted(addresses)[0]
            destination = f"https://{'[' + address + ']' if ':' in address else address}{url.path.rstrip('/')}/chat/completions"
            verify = ssl.create_default_context(cafile=entry.get("ca_file"))
            timeout = httpx.Timeout(connect=MODEL_CONNECT_SECONDS, read=MODEL_IDLE_SECONDS, write=30, pool=15)
            stage = 'model.connect_headers'
            async with httpx.AsyncClient(trust_env=False, verify=verify, follow_redirects=False, timeout=timeout) as client:
                req = client.build_request("POST", destination, json=body,
                    headers={"Host": url.hostname, "Authorization": "Bearer " + key,
                             **provider_session_headers(SimpleNamespace(base_url=entry["base_url"]), job["spec"]["run_id"])},
                    extensions={"sni_hostname": url.hostname})
                response = await client.send(req, stream=True)
                try:
                    self.check(job)
                    self.diagnostic(job, stage, status=response.status_code,
                                    elapsed_ms=round((time.monotonic()-started)*1000))
                    if response.status_code != 200:
                        # Only classify known provider errors; never relay their text/headers.
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk[:max(0, 16384-len(raw))])
                            if len(raw) >= 16384: break
                        text = raw.decode(errors='replace').lower()
                        code = ('context_length_exceeded' if response.status_code in {400,413} and
                                any(x in text for x in ('context_length', 'context length', 'maximum context', 'too many tokens'))
                                else 'insufficient_quota' if response.status_code in {402,429} and
                                any(x in text for x in ('insufficient_quota', 'quota exceeded', 'insufficient balance', 'credit balance'))
                                else 'model_provider_http_' + str(response.status_code))
                        self.diagnostic(job, 'model.rejected', status=response.status_code, code=code)
                        if response.status_code in {401,403}:
                            self.auth_status[job['profile']['credential_ref']] = 'rejected'
                        status = 402 if code == 'insufficient_quota' else response.status_code
                        yield {'type':'headers', 'status':status, 'content_type':'application/json'}
                        yield {'type':'chunk', 'body':base64.b64encode(encoded({'error':{'message':code,'type':code,'code':code}})).decode()}
                    else:
                        yield {'type':'headers', 'status':200,
                               'content_type':'text/event-stream' if body.get('stream') else 'application/json'}
                        stage = 'model.read'
                        async for chunk in response.aiter_bytes():
                            self.check(job)
                            if not chunk: continue
                            if not chunks:
                                self.diagnostic(job, 'model.first_byte', first_byte_ms=round((time.monotonic()-started)*1000))
                            total += len(chunk)
                            if total > 4 * 1024 * 1024: raise RuntimeError('model_output_limit')
                            for offset in range(0, len(chunk), 32768):
                                chunks += 1
                                yield {'type':'chunk', 'body':base64.b64encode(chunk[offset:offset+32768]).decode()}
                    self.diagnostic(job, 'model.complete', bytes=total, chunks=chunks,
                                    elapsed_ms=round((time.monotonic()-started)*1000))
                    release()
                    yield {'type':'end'}
                finally:
                    await response.aclose()
        except (httpx.HTTPError, TimeoutError) as exc:
            self.diagnostic(job, stage, error=exc, elapsed_ms=round((time.monotonic()-started)*1000))
            release()
            yield {'type':'error', 'error':'model_transport_interrupted', 'retryable':True}
        except (RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
            self.diagnostic(job, stage, error=exc, elapsed_ms=round((time.monotonic()-started)*1000))
            release()
            yield {'type':'error', 'error':'model_request_denied', 'retryable':False}
        finally:
            release()

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
                native_id = payload.get('tool_call_id', '')
                if (set(payload) not in ({"name", "arguments"}, {"name", "arguments", "tool_call_id"})
                        or (native_id and (not job['spec']['payload'].get('session') or not isinstance(native_id, str) or len(native_id) > 200))
                        or payload["name"] not in job["spec"]["tools"]
                        or not isinstance(payload["arguments"], dict) or job["callback"] is None):
                    raise ValueError("tool_scope_denied")
                if payload['name']=='delegate':
                    job['yield_tool']={'name':payload['name'], 'arguments':payload['arguments'], 'id':native_id or data['event_id']}
                    result={'yielded': True, 'text': 'Carme 正在释放父 Pi 容器，再执行委派。'} if native_id else 'Carme 正在释放父 Pi 容器，再执行委派。'
                else:
                    result = await job["callback"](payload["name"], payload["arguments"], native_id) if native_id else await job["callback"](payload["name"], payload["arguments"])
            elif kind == "model":
                if job.get('model_active'):
                    raise ValueError('model_already_active')
                job['model_active'] = data['event_id']
                # Streaming calls cannot be replayed. Their sequence remains consumed.
                return self.model(job, payload)
            elif kind == 'diagnostic':
                if not isinstance(payload, dict) or set(payload) - {'event', 'attempt', 'delay_ms', 'elapsed_ms', 'success'}:
                    raise ValueError('diagnostic_fields_denied')
                if payload.get('event') not in {'auto_retry_start','auto_retry_end','compaction_start',
                        'compaction_end','summarization_retry_scheduled','summarization_retry_finished',
                        'turn_recovery_start','turn_recovery_end'}:
                    raise ValueError('diagnostic_event_denied')
                self.diagnostic(job, 'pi.' + payload['event'],
                    **{k:v for k,v in payload.items() if k != 'event'})
                result = None
            elif kind == "stream":
                if set(payload) - {"message_id", "content", "model", "provider", "status", "replace"}:
                    raise ValueError("event_identity_override")
                if 'replace' in payload:
                    if type(payload['replace']) is not bool or not isinstance(payload.get('content'), str):
                        raise ValueError('invalid_stream_delta')
                    content = payload['content'] if payload['replace'] else job.get('stream_text', '') + payload['content']
                    payload = {k:v for k,v in payload.items() if k != 'replace'}
                    payload['content'] = content
                    job['stream_text'] = content
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
        raw=await bounded_body(request,16_000_000);nonce=request.headers.get('X-Carme-Nonce','')
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
        if data.get('op') == 'computer_claim' and set(data) == {'op', 'runner_id', 'grant'}:
            if os.getenv('CARME_ACCOUNT_ID') != 'main' or not execution.runtime.config.isolation['mac_runner'].get('computer_use'):
                raise HTTPException(403, 'main_host_computer_only')
            grant = data['grant']
            if grant and (not isinstance(grant, dict) or set(grant) != {'bot_id', 'generation', 'expires'}
                    or not isinstance(grant['bot_id'], str) or not re.fullmatch(r'(?:\*|[A-Za-z0-9_-]{1,96})', grant['bot_id'])
                    or not re.fullmatch(r'[a-f0-9]{32}', str(grant['generation']))
                    or type(grant['expires']) not in {int, float} or not time.time() < grant['expires'] <= time.time() + 601):
                raise HTTPException(400, 'host_grant_denied')
            execution.host_grant = grant or {}
            if not grant:
                value = execution.desktop_controls.get('@host')
                if value: value.update(enabled=False, id='', expires=0, epoch=value['epoch'] + 1)
            for item in execution.desktop_requests.values():
                if item['claimed'] or not item.get('target', '').startswith('host:'): continue
                try: execution.check_desktop_request(item)
                except (ValueError, RuntimeError): continue
                item['claimed'] = True
                result = {k: item[k] for k in ('id', 'bot_id', 'operation', 'arguments', 'deadline', 'target')}
                break
        elif data.get('op') in {'computer_check', 'computer_finish'} and set(data) == ({'op', 'runner_id', 'id'} | ({'result'} if data['op'] == 'computer_finish' else set())):
            item = execution.desktop_requests.get(data['id'])
            try:
                if not item or not item['claimed'] or not item.get('target', '').startswith('host:'): raise ValueError('host_request_missing')
                execution.check_desktop_request(item)
                if data['op'] == 'computer_finish': item['future'].set_result(data['result'])
                result = {'active': True}
            except (ValueError, RuntimeError): result = {'active': False}
        elif data.get('op')=='claim' and set(data)=={'op','runner_id','authorized'} and type(data['authorized']) is bool:
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
        if data.get('op') == 'desktop_claim' and set(data) in ({'op'}, {'op', 'wait'}):
            wait = data.get('wait', 0)
            if type(wait) is not int or not 0 <= wait <= 2:
                raise HTTPException(400, 'desktop_wait_denied')
            for attempt in range(2):
                execution.desktop_ready.clear()
                # Human input precedes disposable frame reads; stable sorting preserves input order.
                pending = sorted(execution.desktop_requests.values(), key=lambda item:
                    0 if not item['ctx'] and item['operation'] in {'mouse', 'keyboard', 'release'} else
                    2 if item['operation'] in {'status', 'screenshot'} else 1)
                for item in pending:
                    if item['claimed'] or item.get('target', 'linux') != 'linux': continue
                    try: execution.check_desktop_request(item)
                    except (ValueError, RuntimeError): continue
                    item['claimed'] = True
                    result = {k: item[k] for k in ('id', 'bot_id', 'operation', 'arguments', 'deadline')}
                    break
                if result or not wait or attempt: break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(execution.desktop_ready.wait(), wait)
        elif data.get('op') == 'desktop_check' and set(data) == {'op', 'id', 'session_id'}:
            item = execution.desktop_requests.get(data['id'])
            try:
                if not item or not item['claimed'] or item.get('target', 'linux') != 'linux' or not re.fullmatch(r'[a-f0-9]{32}', str(data['session_id'])): raise ValueError('desktop_request_missing')
                execution.check_desktop_request(item, finished=bool(item.get('network_tasks')))
                execution.desktop_sessions[item['bot_id']] = data['session_id']
                result = {'active': True, 'deadline': item['deadline'], 'safety': dict(execution.runtime.config.browser.safety)}
            except (ValueError, RuntimeError): result = {'active': False}
        elif data.get('op') == 'desktop_finish' and set(data) == {'op', 'id', 'result'}:
            item = execution.desktop_requests.get(data['id'])
            if item and item['claimed'] and not item['future'].done(): item['future'].set_result(data['result'])
            result = {'ok': True}
        elif data.get('op') == 'desktop_rpc' and set(data) == {'op', 'bot_id', 'session_id', 'kind', 'payload'}:
            execution.desktop_bot(data['bot_id'])
            if execution.desktop_sessions.get(data['bot_id']) != data['session_id']:
                raise HTTPException(403, 'desktop_session_denied')
            from .docker_browser import fetch_public
            safety = dict(execution.runtime.config.browser.safety)
            def check_session():
                if execution.desktop_sessions.get(data['bot_id']) != data['session_id']:
                    raise ValueError('desktop_session_revoked')
            if data['kind'] == 'browser_fetch':
                result = await execution.desktop_fetch(data['bot_id'], data['session_id'], data['payload'])
            elif data['kind'] == 'download':
                payload = data['payload']
                if not isinstance(payload, dict) or set(payload) != {'url', 'name'}: raise HTTPException(400, 'download_fields_denied')
                result = await fetch_public({'url': payload['url'], 'method': 'GET', 'headers': {}, 'body': ''}, safety, check_session)
                if result.get('status') != 200: raise HTTPException(400, 'download_failed_or_redirected')
            elif data['kind'] == 'approve':
                current = next((r for r in execution.desktop_requests.values() if r['bot_id'] == data['bot_id'] and r['claimed'] and r['ctx']), None)
                if not current: raise HTTPException(403, 'desktop_approval_task_missing')
                execution.check_desktop_request(current)
                approved = await current['ctx'].request_approval(**data['payload'])
                result = {'approved': approved.approved, 'note': approved.note}
            else: raise HTTPException(400, 'desktop_rpc_denied')
        elif data.get("op") == "claim" and set(data) == {"op", "health", "instance_id", "capacity"}:
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
        if hasattr(result, '__aiter__'):
            async def frames():
                sequence = 0
                try:
                    async for frame in result:
                        sequence += 1
                        raw = encoded(frame)
                        yield encoded({'sequence':sequence, 'data':frame,
                            'signature':signature(auth[7:].encode(), data['event_id'] + ':' + str(data['sequence']) + ':' + str(sequence), raw)}) + b"\n"
                finally:
                    await result.aclose()
            return StreamingResponse(frames(), media_type='application/x-ndjson',
                headers={'X-Carme-Stream':'1', 'Cache-Control':'no-store', 'X-Accel-Buffering':'no'})
        body = encoded({"result": result})
        return Response(body, media_type="application/json", headers={
            "X-Carme-Signature": signature(auth[7:].encode(), data["event_id"], body)})

    return router
