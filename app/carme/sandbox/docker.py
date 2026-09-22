"""Container tools use the Control queue; Docker access exists only in the Broker."""
from __future__ import annotations

import base64

from .base import ExecResult, Sandbox, SandboxError


class DockerSandbox(Sandbox):
    def __init__(self, spec, execution=None):
        super().__init__(spec)
        if execution is None:
            raise SandboxError("container_runner_unavailable: no host fallback")
        self.execution = execution

    @property
    def workdir(self):
        return "/workspace"

    async def setup(self):
        self.execution.key()
        self.execution.runtime.check_task_policy(self.spec.task_id)
        if self.execution.health()["broker"] != "ready":
            raise SandboxError("container_runner_unavailable: broker offline")
        self._ready = True

    async def _run(self, command, cwd, timeout):
        result = await self.execution.submit(self.spec.task_id, "action",
            {"op": "exec", "command": command, "cwd": cwd or "/workspace", "timeout": timeout}, timeout=timeout + 10)
        return ExecResult(**result)

    async def read_bytes(self, path):
        result = await self.execution.submit(self.spec.task_id, "action", {"op": "export", "path": path})
        return base64.b64decode(result["bytes"], validate=True)

    async def health(self):
        return {"ok": self._ready, "mode": "docker", **self.execution.health()}
