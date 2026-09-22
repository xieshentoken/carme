"""Trusted host executable: pull signed jobs, validate, run fixed container images.

No inbound listener, model, shell evaluation or arbitrary Docker arguments.
Invoke with a private, administrator-owned JSON config: python -m carme.broker FILE.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .engines import _redact
from .execution import (LEASE_CALL_TIMEOUT, LEASE_RENEW_SECONDS, LEASE_RETRY_SECONDS,
                        MAX_MESSAGE, encoded, signature)
from .security import bounded_output, child_env

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}\Z")
SPEC_KEYS = {"job_id", "instance_id", "bot_id", "run_id", "role", "target_id", "image_digest",
             "deadline", "max_output_bytes", "permission_version", "token_id", "audience", "tools", "payload"}

# Worker stderr is the only explanation of a non-zero CLI exit. Keep a bounded tail per job and
# rotate the diagnostics file so a long-lived instance cannot fill the disk with old failures.
WORKER_STDERR_KEEP = 8 * 1024
WORKER_DIAG_LIMIT = 512 * 1024
WORKER_DIAG_NAME = "worker-diag.jsonl"
WORKER_DIAG_ARCHIVE = "worker-diag.1.jsonl"


def validate_spec(spec, config):
    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS:
        raise ValueError("runspec_fields_denied")
    for field in ("job_id", "instance_id", "bot_id", "run_id", "target_id", "token_id"):
        if not isinstance(spec[field], str) or not ID.fullmatch(spec[field]):
            raise ValueError("invalid_execution_id")
    if spec["instance_id"] != config["instance_id"] or spec["target_id"] not in config["targets"]:
        raise ValueError("runspec_target_denied")
    if (spec["role"] not in {"pi", "action", "browser"} or not IMAGE.fullmatch(spec["image_digest"])
            or spec["image_digest"] != config["images"].get(spec["role"])):
        raise ValueError("runspec_image_denied")
    deadline = spec["deadline"]
    if (isinstance(deadline, bool) or not isinstance(deadline, (float, int)) or not math.isfinite(deadline)
            or deadline <= time.time() or deadline > time.time() + config.get("max_seconds", 600)):
        raise ValueError("runspec_deadline_denied")
    limit = spec["max_output_bytes"]
    if type(limit) is not int or not 1 <= limit <= config.get("max_output_bytes", 65536):
        raise ValueError("runspec_output_limit_denied")
    if spec["audience"] != "worker-relay" or not re.fullmatch(r"[a-f0-9]{64}", spec["permission_version"]):
        raise ValueError("runspec_identity_denied")
    payload = spec["payload"]
    large = isinstance(payload, dict) and payload.get('op') in {'stage_input', 'validate_artifact'}
    if not isinstance(payload, dict) or len(encoded(payload)) > (MAX_MESSAGE - 4096 if large else 1024 * 1024):
        raise ValueError("runspec_payload_denied")
    if spec["role"] == "action":
        if payload.get("op") == "exec":
            if set(payload) != {"op", "command", "cwd", "timeout"} or not isinstance(payload["command"], str) or len(payload["command"]) > 32768:
                raise ValueError("action_fields_denied")
        elif payload.get("op") == "export":
            if set(payload) != {"op", "path"} or not isinstance(payload["path"], str):
                raise ValueError("action_fields_denied")
        elif payload.get("op") == "snapshot":
            from .projects import validate_bundle
            if set(payload) != {"op", "bundle"}:
                raise ValueError("action_fields_denied")
            validate_bundle(payload["bundle"])
        elif payload.get('op') in {'stage_input', 'validate_artifact'}:
            expected = {'op', 'name', 'bytes', 'sha256'} | ({'checks'} if payload['op']=='validate_artifact' else set())
            if set(payload)!=expected or not isinstance(payload['name'],str):raise ValueError('artifact_fields_denied')
            raw=base64.b64decode(payload['bytes'],validate=True)
            minimum=0 if payload['op']=='stage_input' else 1
            if not minimum<=len(raw)<=10*1024*1024 or hashlib.sha256(raw).hexdigest()!=payload['sha256']:
                raise ValueError('artifact_hash_or_size_denied')
            if payload['op']=='stage_input':
                parts=payload['name'].split('/')
                if parts[0] not in {'artifacts','skills'} or len(parts)<3 or any(not 1<=len(p)<=160 or p in {'.','..'} or '\\' in p or ':' in p or any(ord(c)<32 for c in p) for p in parts):
                    raise ValueError('input_path_denied')
            elif '/' in payload['name'] or '\\' in payload['name'] or not isinstance(payload['checks'],list):
                raise ValueError('artifact_fields_denied')
        elif payload.get('op')=='mcp':
            if set(payload)!={'op','server','server_id','call'} or not isinstance(payload['server'],dict):raise ValueError('mcp_fields_denied')
            server=payload['server']
            if server.get('transport')!='stdio' or server.get('executor')!='action' or server.get('trusted_host') or server.get('cwd'):
                raise ValueError('mcp_isolated_stdio_required')
            from .security import explicit_child_values
            explicit_child_values(server.get('env',{}))
        else:
            raise ValueError("action_operation_denied")
    elif spec['role'] == 'browser':
        from .docker_browser import PROFILE, WEB_TOOLS
        if (set(payload) != {'profile', 'bot_id', 'run_id', 'safety', 'max_output_bytes', 'browser_channel'}
                or not isinstance(payload['profile'], str) or not PROFILE.fullmatch(payload['profile'])
                or payload['bot_id'] != spec['bot_id'] or payload['run_id'] != spec['run_id']
                or payload['max_output_bytes'] != spec['max_output_bytes']
                or not isinstance(payload['browser_channel'], str)
                or not re.fullmatch(r'(?:[a-z][a-z0-9-]{0,19})?', payload['browser_channel'])
                or not isinstance(payload['safety'], dict) or not WEB_TOOLS & set(spec['tools'])):
            raise ValueError('browser_fields_denied')
    elif set(payload) != {"prompt", "model", "effort", "tools", "max_tool_calls"}:
        raise ValueError("pi_fields_denied")
    return spec


class Broker:
    def __init__(self, config):
        import pwd
        self.config = config
        if not ID.fullmatch(config["instance_id"]):
            raise ValueError("invalid_instance_id")
        self.home = Path(config["home"])
        personal_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if (not self.home.is_absolute() or self.home.resolve() != self.home
                or self.home in {Path("/"), personal_home} or any(p in {".pi", ".ssh"} for p in self.home.parts)):
            raise ValueError("broker_home_requires_dedicated_canonical_directory")
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.key = Path(config["key_file"]).read_bytes().strip()
        if len(self.key) < 32:
            raise ValueError("broker_key_too_short")
        url = urlsplit(config["control_url"])
        if (url.scheme not in {"http", "https"} or url.username or url.password or url.query or url.fragment
                or url.path not in {"", "/"} or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "::1", "localhost"})):
            raise ValueError("broker_requires_tls_or_loopback")
        if not Path(config["docker_binary"]).is_absolute() or not config["docker_context"]:
            raise ValueError("explicit_docker_context_required")
        self.env = child_env(self.directory("runtime", "broker", "home"))
        Path(self.env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        self.env["DOCKER_CONFIG"] = config["docker_config"]
        self.active: dict[str, tuple[str, asyncio.Task]] = {}
        self.browser_profiles: set[tuple[str, str]] = set()
        self.client = httpx.AsyncClient(base_url=config["control_url"], trust_env=False,
            verify=config.get("control_ca_file", True), timeout=10, follow_redirects=False)
        self.health = {"action": "unverified", "pi": "unverified"}
        self.health["images"] = dict(config["images"])
        self.health["max_pi"] = int(config.get("max_pi", 2))
        self.health['max_browser'] = int(config.get('max_browser', 2))
        self.stopping = False

    def directory(self, *parts):
        current = self.home
        for part in parts:
            if not ID.fullmatch(part):
                raise ValueError("invalid_execution_path")
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink_mount_denied")
            current.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(current.lstat().st_mode) or current.resolve() != current:
                raise ValueError("mount_escape_denied")
        return current

    def record_worker_stderr(self, spec, raw):
        """Append a bounded, redacted Worker stderr tail to the instance diagnostics file.

        The Worker reports engine and provider failures generically, so a non-zero CLI exit is
        otherwise undiagnosable. This file is operator-only and best-effort only: it must never
        fail or slow down a real job.
        """
        if not raw:
            return
        try:
            directory = self.directory("runtime", "logs")
            path = directory / WORKER_DIAG_NAME
            if path.is_file() and path.stat().st_size > WORKER_DIAG_LIMIT:
                path.replace(directory / WORKER_DIAG_ARCHIVE)
            line = json.dumps({"t": round(time.time(), 3), "role": spec["role"], "run_id": spec["run_id"],
                               "job_id": spec["job_id"], "bytes": len(raw),
                               "stderr": _redact(raw.decode("utf-8", errors="replace"))},
                              ensure_ascii=False) + "\n"
            with os.fdopen(os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600),
                           "a", encoding="utf-8") as stream:
                stream.write(line)
        except Exception:
            pass
    async def docker(self, *args, timeout=30):
        proc = await asyncio.create_subprocess_exec(self.config["docker_binary"], "--context",
            self.config["docker_context"], *args, env=self.env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        out, err = await bounded_output(proc, timeout=timeout, limit=1024 * 1024)
        if proc.returncode:
            if args[0] == "inspect" and (b"no such object" in err.lower() or b"no such container" in err.lower()):
                return ""
            raise RuntimeError("docker_operation_failed:" + args[0])
        return out.decode().strip()

    async def call(self, data, timeout=None):
        # Control refuses a signed request whose nonce is older than its replay window, and a
        # stalled Control can hold a request in the socket for longer than that. Such a rejection
        # never reaches the handler, so lease traffic re-signs once instead of failing a job whose
        # tools already ran; Control's window is not widened for a stall. Claims stay out of it:
        # their fast loop already retries and their short timeout cannot go stale.
        try:
            return await self.request(data, timeout)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403 or data.get("op") == "claim":
                raise
        return await self.request(data, timeout)

    async def request(self, data, timeout=None):
        body = encoded(data)
        nonce = f"{time.time():.3f}:{secrets.token_hex(16)}"
        # Lease traffic outlives the default client timeout, so a stalled Control can still answer
        # a renewal that was already in flight instead of failing a job that is working fine.
        extra = {} if timeout is None else {"timeout": timeout}
        async with self.client.stream("POST", "/internal/broker", content=body,
                headers={"Content-Type": "application/json", "X-Carme-Nonce": nonce,
                         "X-Carme-Signature": signature(self.key, nonce, body)}, **extra) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_MESSAGE:
                    raise RuntimeError("control_message_too_large")
            if not hmac.compare_digest(signature(self.key, nonce, raw), response.headers.get("X-Carme-Signature", "")):
                raise RuntimeError("control_signature_invalid")
            return json.loads(raw)

    def create_args(self, spec):
        validate_spec(spec, self.config)
        name = f"carme-{self.config['instance_id']}-{spec['job_id']}"
        args = ["create", "--name", name, "--pull=never", "--label", "carme.instance=" + spec["instance_id"],
                "--label", "carme.run=" + spec["run_id"], "--label", "carme.job=" + spec["job_id"],
                "--label", "carme.role=" + spec["role"], "--network=none", "--read-only", "--user=1000:1000",
                "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--pids-limit=64",
                "--memory=" + str(self.config.get("memory", "768m")), "--memory-swap=" + str(self.config.get("memory", "768m")),
                "--cpus=" + str(self.config.get("cpus", 1)), "--init", "--log-driver=none",
                "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
                "--tmpfs=/runtime:rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=700", "--interactive"]
        # The Worker never sees host path spellings, a whole CARME_HOME, or another run.
        if spec["role"] == "action":
            run = self.directory("runtime", "runs", spec["run_id"])
            owner = run / "owner.json"
            expected = {"instance_id": spec["instance_id"], "bot_id": spec["bot_id"], "run_id": spec["run_id"]}
            if owner.exists():
                if owner.is_symlink() or json.loads(owner.read_text()) != expected:
                    raise ValueError("run_owner_conflict")
            else:
                with owner.open("x") as f:
                    json.dump(expected, f)
            for sub in ("workspace", "inputs", "out"):
                path = self.directory("runtime", "runs", spec["run_id"], sub)
                # The parent is Broker-owned; the mount roots alone are writable by UID 1000.
                path.chmod(0o777 if sub != "inputs" else 0o755)
                if "," in str(path):
                    raise ValueError("mount_path_comma_unsupported")
                args += ["--mount", f"type=bind,src={path},dst=/{sub}" + (",readonly" if sub == "inputs" else "")]
            if spec['payload'].get('op')=='stage_input':
                # Only Broker populates the input mount. Workers always receive it read-only.
                item=spec['payload'];path=run/'inputs'
                for part in item['name'].split('/')[:-1]:
                    path=path/part
                    if path.is_symlink():raise ValueError('input_symlink_denied')
                    path.mkdir(mode=0o755,exist_ok=True)
                path=path/item['name'].split('/')[-1]
                if path.is_symlink():raise ValueError('input_symlink_denied')
                raw=base64.b64decode(item['bytes'],validate=True)
                if path.exists():
                    if path.read_bytes()!=raw:raise ValueError('immutable_input_conflict')
                else:
                    with path.open('xb') as stream:stream.write(raw)
                    path.chmod(0o444)
            args += ["--workdir=/workspace"]
        elif spec['role'] == 'browser':
            profile = self.directory('runtime', 'browser', spec['bot_id'], spec['payload']['profile'])
            profile.chmod(0o777)
            if ',' in str(profile):
                raise ValueError('mount_path_comma_unsupported')
            seccomp = Path(__file__).resolve().parents[1] / 'deploy/docker/browser-seccomp.json'
            if not seccomp.is_file():
                raise ValueError('browser_seccomp_missing')
            args += ['--mount', f'type=bind,src={profile},dst=/profile', '--workdir=/runtime',
                     '--shm-size=256m', '--pids-limit=256', '--memory=1g', '--memory-swap=1g',
                     '--security-opt=seccomp=' + str(seccomp)]
        else:
            args += ["--workdir=/runtime"]
        args += [spec["image_digest"]]
        return name, args

    async def remove(self, name, spec):
        raw = await self.docker("inspect", "--format", "{{json .Config.Labels}}", name)
        if not raw:
            return
        labels = json.loads(raw)
        if labels.get("carme.instance") != self.config["instance_id"] or labels.get("carme.job") != spec["job_id"]:
            raise RuntimeError("cleanup_owner_mismatch")
        await self.docker("rm", "-f", name)

    async def run_job(self, claim):
        spec, lease = claim["spec"], claim["lease"]
        name = ""
        proc = None
        result = {"error": "execution_interrupted: reconcile before retry"}
        tasks = []
        relays = set()
        browser_identity = None
        cleanup_ok = False
        relay_failure = asyncio.get_running_loop().create_future()
        stderr_task = None
        stderr_tail = bytearray()
        def relay_done(task):
            if not task.cancelled() and task.exception() and not relay_failure.done():
                relay_failure.set_exception(task.exception())
        try:
            if spec['role'] == 'browser':
                validate_spec(spec, self.config)
                identity = (spec['bot_id'], spec['payload']['profile'])
                if identity in self.browser_profiles:
                    raise RuntimeError('browser_identity_busy: another run owns this Bot/profile')
                self.browser_profiles.add(identity)
                browser_identity = identity
            name, args = self.create_args(spec)
            await self.docker(*args)
            proc = await asyncio.create_subprocess_exec(self.config["docker_binary"], "--context",
                self.config["docker_context"], "start", "--attach", "--interactive", name,
                env=self.env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE + 1, start_new_session=True)
            proc.stdin.write(encoded({"role": spec["role"], "payload": spec["payload"],
                "max_output_bytes": spec["max_output_bytes"]}) + b"\n")
            await proc.stdin.drain()

            async def renew():
                confirmed = time.monotonic()
                while True:
                    await asyncio.sleep(LEASE_RENEW_SECONDS)
                    try:
                        response = await self.call({"op": "renew", "job_id": spec["job_id"], "lease": lease},
                                                   timeout=LEASE_CALL_TIMEOUT)
                    except Exception:
                        # Silence is not a revoked lease: keep asking, so a stalled host does not
                        # fail a job whose tools already ran. Only an authoritative refusal below,
                        # this retry budget, the task deadline and cleanup end it.
                        if time.monotonic() - confirmed > LEASE_RETRY_SECONDS:
                            raise
                        continue
                    if not response.get("active"):
                        raise RuntimeError("execution_lease_revoked")
                    confirmed = time.monotonic()
                    proc.stdin.write(b'{"type":"lease"}\n')
                    await proc.stdin.drain()

            async def stderr():
                total = 0
                while chunk := await proc.stderr.read(4096):
                    total += len(chunk)
                    stderr_tail.extend(chunk)
                    if len(stderr_tail) > WORKER_STDERR_KEEP:
                        del stderr_tail[:-WORKER_STDERR_KEEP]
                    if total > spec["max_output_bytes"]:
                        raise RuntimeError("worker_stderr_limit")

            async def relay(message):
                async with self.client.stream("POST", f"/internal/runs/{spec['run_id']}/messages",
                        json=message, headers={"Authorization": "Bearer " + claim["task_token"]}, timeout=600) as response:
                    # Control validates the task token; responses are HMAC-bound too.
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_MESSAGE:
                            raise RuntimeError("relay_response_limit")
                    expected = signature(claim["task_token"].encode(), message["event_id"], raw)
                    if not hmac.compare_digest(expected, response.headers.get("X-Carme-Signature", "")):
                        raise RuntimeError("relay_signature_invalid")
                    reply = json.loads(raw)
                proc.stdin.write(encoded({"id": message["event_id"], **reply}) + b"\n")
                await proc.stdin.drain()

            async def read():
                sequence = 0
                total = 0
                final = None
                while line := await proc.stdout.readline():
                    total += len(line)
                    if len(line) > MAX_MESSAGE or total > 32 * 1024 * 1024:
                        raise RuntimeError("worker_output_limit")
                    data = json.loads(line)
                    if data.get("type") == "result" and set(data) == {"type", "result"}:
                        if final is not None:
                            raise RuntimeError("duplicate_worker_result")
                        final = data["result"]
                    elif data.get("type") == "rpc" and spec["role"] in {"pi", "browser"} and final is None:
                        if set(data) != {"type", "kind", "payload", "id"}:
                            raise RuntimeError("worker_rpc_fields_denied")
                        sequence += 1
                        message = {"sequence": sequence, "event_id": data["id"], "kind": data["kind"], "payload": data["payload"]}
                        if spec['role'] == 'browser':
                            for finished in list(relays):
                                if finished.done():
                                    finished.result()
                                    relays.remove(finished)
                            if len(relays) >= 32:
                                raise RuntimeError('browser_relay_concurrency_limit')
                            relay_task = asyncio.create_task(relay(message))
                            relay_task.add_done_callback(relay_done)
                            relays.add(relay_task)
                        else:
                            await relay(message)
                    else:
                        raise RuntimeError("worker_protocol_denied")
                await proc.wait()
                if proc.returncode != 0 or final is None:
                    raise RuntimeError("worker_failed")
                return final

            stderr_task = asyncio.create_task(stderr())
            tasks = [asyncio.create_task(read()), asyncio.create_task(renew()), stderr_task, relay_failure]
            async with asyncio.timeout(max(0, spec["deadline"] - time.time())):
                # A lease failure interrupts even when the Worker is awaiting an approval/model.
                pending = set(tasks)
                while tasks[0] in pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                result = tasks[0].result()
        except (Exception, asyncio.CancelledError) as exc:
            result = {"error": "container_execution_failed:" + type(exc).__name__ + ":" +
                      (str(exc)[:140] if isinstance(exc, (ValueError, RuntimeError)) else "no host fallback")}
        finally:
            # Drain the stderr reader before cancelling it: that tail is the only explanation of a
            # non-zero CLI exit, so it is kept and persisted while the job is still in hand.
            if stderr_task is not None and not stderr_task.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(stderr_task, timeout=1)
            if stderr_tail:
                self.record_worker_stderr(spec, bytes(stderr_tail))
            for task in [*tasks, *relays]:
                task.cancel()
            await asyncio.gather(*tasks, *relays, return_exceptions=True)
            if name:
                receipt = self.directory("runtime", "broker") / (spec["job_id"] + ".json")
                try:
                    await self.remove(name, spec)
                    cleanup_ok = True
                    receipt.unlink(missing_ok=True)
                    result["_execution"] = {"container": name, "cleanup": "removed"}
                except Exception:
                    receipt.write_text(json.dumps({"container": name, "job_id": spec["job_id"],
                        "run_id": spec["run_id"], "instance_id": self.config["instance_id"], "cleanup": "pending"}))
                    result = {"error": "container_cleanup_pending: broker reconciliation required",
                              "_execution": {"container": name, "cleanup": "pending"}}
            if proc and proc.returncode is None:
                from .engines import _stop_process_group
                await _stop_process_group(proc)
            if browser_identity:
                if not name or cleanup_ok:
                    self.browser_profiles.discard(browser_identity)
                else:
                    self.health['browser'] = 'cleanup_pending'
            with contextlib.suppress(Exception):
                await self.call({"op": "finish", "job_id": spec["job_id"], "lease": lease,
                                 "result": result}, timeout=LEASE_CALL_TIMEOUT)

    async def startup(self):
        await self.docker("version", "--format", "{{.Server.Version}}")
        # No replay after restart. Reconcile the instance's prior containers by terminating them.
        names = await self.docker("ps", "-aq", "--filter", "label=carme.instance=" + self.config["instance_id"])
        for name in names.splitlines():
            raw = await self.docker("inspect", "--format", "{{json .Config.Labels}}", name)
            if not raw:
                continue
            labels = json.loads(raw)
            if labels.get("carme.instance") == self.config["instance_id"] and ID.fullmatch(labels.get("carme.job", "")):
                await self.remove(name, {"job_id": labels["carme.job"]})
        for receipt in self.directory("runtime", "broker").glob("*.json"):
            record = json.loads(receipt.read_text())
            if (record.get("instance_id") != self.config["instance_id"]
                    or not ID.fullmatch(record.get("job_id", ""))
                    or record.get("container") != f"carme-{self.config['instance_id']}-{record['job_id']}"):
                raise RuntimeError("cleanup_receipt_owner_mismatch")
            await self.remove(record["container"], record)
            receipt.unlink()
        for role in ('pi', 'action', *(['browser'] if 'browser' in self.config['images'] else [])):
            image = self.config["images"][role]
            if not IMAGE.fullmatch(image):
                raise ValueError("image_digest_required")
            try:
                actual = await self.docker("image", "inspect", "--format", "{{.Id}}", image)
                self.health[role] = "ready" if actual == image else "image_mismatch"
            except RuntimeError:
                self.health[role] = "image_missing"

    async def serve(self):
        # An exclusive instance lock prevents a second Broker from killing live leases.
        import fcntl
        lock = (self.directory("runtime", "broker") / "broker.lock").open("a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            await self.startup()
            while not self.stopping:
                self.active = {j: value for j, value in self.active.items() if not value[1].done()}
                counts = {r: sum(v[0] == r for v in self.active.values()) for r in self.config['images'] if r in {'pi', 'action', 'browser'}}
                capacity = [r for r in counts if counts[r] < (self.config.get('max_' + r, 2) if r in {'pi', 'browser'} else 1)]
                try:
                    claim = await self.call({"op": "claim", "health": self.health,
                        "instance_id": self.config["instance_id"], "capacity": capacity})
                    if claim:
                        spec = claim["spec"]
                        task = asyncio.create_task(self.run_job(claim))
                        self.active[spec["job_id"]] = (spec["role"], task)
                except Exception:
                    # No daemon/context fallback and no replay. Existing jobs lose their lease fast.
                    pass
                await asyncio.sleep(0.25)
        finally:
            for _, task in self.active.values():
                task.cancel()
            await asyncio.gather(*(v[1] for v in self.active.values()), return_exceptions=True)
            await self.client.aclose()
            lock.close()


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    broker = Broker(config)
    async def run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, setattr, broker, "stopping", True)
        await broker.serve()
    asyncio.run(run())


if __name__ == "__main__":
    main()
