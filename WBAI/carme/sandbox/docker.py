"""Docker 沙箱 —— 真正的宿主机隔离。

走 docker CLI 而不是 Python SDK：少一个依赖，且 colima / Docker Desktop
两种后端都能用。Mac 上装法见 deploy/README。

一次任务 = 一个容器，任务结束容器销毁（--rm）。
"""

from __future__ import annotations

import asyncio
import shlex
import uuid

from .base import ExecResult, Sandbox, SandboxError


class DockerSandbox(Sandbox):
    def __init__(self, spec) -> None:
        super().__init__(spec)
        s = spec.settings
        self.image = s.get("image", "carme/sandbox:latest")
        self.memory = s.get("memory", "2g")
        self.cpus = str(s.get("cpus", 2.0))
        self.pids_limit = int(s.get("pids_limit", 256))
        self.network = bool(s.get("network", True))
        self.container_workdir = s.get("workdir", "/workspace")
        self.setup_command = s.get("setup_command", "")
        self._name = f"carme-{spec.agent_id}-{uuid.uuid4().hex[:8]}"
        self._host_dir = None

    @property
    def workdir(self) -> str:
        return self.container_workdir

    async def _docker(self, *args: str, timeout: int = 120) -> ExecResult:
        cmd = " ".join(shlex.quote(a) for a in ("docker", *args))
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            ok=(proc.returncode == 0),
        )

    async def _ensure_docker(self) -> None:
        probe = await self._docker("version", "--format", "{{.Server.Version}}", timeout=20)
        if not probe.ok:
            raise SandboxError(
                "Docker 不可用。Mac 上请先装 colima 与 docker CLI：\n"
                "  brew install colima docker\n"
                "  colima start --cpu 2 --memory 4\n"
                f"原始错误：{probe.stderr.strip()[:300]}"
            )

    async def setup(self) -> None:
        if self._ready:
            return
        await self._ensure_docker()

        from pathlib import Path

        host_root = Path("./data/workspaces").expanduser().resolve()
        self._host_dir = host_root / self.spec.agent_id / self.spec.task_id
        self._host_dir.mkdir(parents=True, exist_ok=True)

        args = [
            "run",
            "-d",
            "--rm",
            "--name",
            self._name,
            "-m",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "-v",
            f"{self._host_dir}:{self.container_workdir}",
            "-w",
            self.container_workdir,
        ]
        if not self.network:
            args += ["--network", "none"]
        args += [self.image, "sleep", "infinity"]

        result = await self._docker(*args, timeout=180)
        if not result.ok:
            raise SandboxError(
                f"容器启动失败（镜像 {self.image} 是否已构建？见 deploy/Dockerfile.sandbox）："
                f"{result.stderr.strip()[:400]}"
            )

        if self.setup_command:
            await self._run(self.setup_command, None, 300)
        self._ready = True

    async def _run(self, command: str, cwd: str | None, timeout: int) -> ExecResult:
        inner = command
        if cwd:
            inner = f"cd {shlex.quote(cwd)} && {command}"
        return await self._docker(
            "exec", "-w", self.container_workdir, self._name, "sh", "-lc", inner, timeout=timeout
        )

    async def teardown(self) -> None:
        if self._ready:
            await self._docker("rm", "-f", self._name, timeout=60)
        self._ready = False

    async def health(self) -> dict:
        version = await self._docker("version", "--format", "{{.Server.Version}}", timeout=20)
        return {
            "ok": version.ok,
            "mode": "docker",
            "container": self._name,
            "image": self.image,
            "memory": self.memory,
            "detail": version.stdout.strip() or version.stderr.strip(),
        }
