"""执行环境的统一抽象。

不管命令最终落在本机目录、Docker 容器还是远端 2018 那台机器上，
对上层 Agent 来说都是同一套 exec / read / write 接口。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import shlex
import time
from dataclasses import dataclass, field

log = logging.getLogger("carme.sandbox")

# 无论哪种模式都拦掉的命令模式。不是安全边界，是防手滑和防幻觉的第一道闸。
DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "rm -rf $HOME",
    "mkfs",
    "dd if=/dev/zero of=/dev/",
    ":(){ :|:& };:",
    "chmod -R 777 /",
    "> /dev/sda",
    "shutdown",
    "reboot",
    "halt",
]


@dataclass
class ExecResult:
    ok: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    truncated: bool = False
    command: str = ""

    def as_text(self, limit: int = 6000) -> str:
        """给模型看的紧凑表示。"""
        parts = [f"$ {self.command}", f"exit={self.exit_code} ({self.duration:.2f}s)"]
        if self.stdout:
            parts.append(self.stdout[:limit])
        if self.stderr:
            parts.append("[stderr]\n" + self.stderr[: limit // 2])
        if self.truncated:
            parts.append("[输出已截断]")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 3),
            "truncated": self.truncated,
            "command": self.command,
        }


class SandboxError(RuntimeError):
    pass


@dataclass
class SandboxSpec:
    """描述一个沙箱实例该长什么样。"""

    agent_id: str
    task_id: str
    mode: str = "local"
    settings: dict = field(default_factory=dict)


class Sandbox(abc.ABC):
    """一次任务一个实例。setup() 建环境，teardown() 收拾干净。"""

    def __init__(self, spec: SandboxSpec) -> None:
        self.spec = spec
        self.max_output_bytes = int(spec.settings.get("max_output_bytes", 200_000))
        self.default_timeout = int(spec.settings.get("timeout_seconds", 120))
        self._ready = False

    @property
    @abc.abstractmethod
    def workdir(self) -> str:
        """这个沙箱的工作目录（容器内路径或宿主路径）。"""

    @property
    def label(self) -> str:
        return f"{self.spec.mode}:{self.spec.agent_id}/{self.spec.task_id}"

    async def setup(self) -> None:
        self._ready = True

    async def teardown(self) -> None:
        self._ready = False

    @abc.abstractmethod
    async def _run(self, command: str, cwd: str | None, timeout: int) -> ExecResult:
        """真正执行命令，由子类实现。"""

    async def exec(self, command: str, *, cwd: str | None = None, timeout: int | None = None) -> ExecResult:
        if not self._ready:
            await self.setup()

        blocked = self._check_dangerous(command)
        if blocked:
            return ExecResult(
                ok=False,
                exit_code=126,
                stderr=f"命令被安全闸拦截（匹配到危险模式 {blocked!r}）。如确实需要，请说明理由并让用户手动执行。",
                command=command,
            )

        started = time.time()
        try:
            result = await self._run(command, cwd, timeout or self.default_timeout)
        except asyncio.TimeoutError:
            result = ExecResult(
                ok=False,
                exit_code=124,
                stderr=f"命令超时（{timeout or self.default_timeout}s）",
                command=command,
            )
        result.duration = time.time() - started
        result.command = command
        result.ok = result.exit_code == 0

        if len(result.stdout) > self.max_output_bytes:
            result.stdout = result.stdout[: self.max_output_bytes]
            result.truncated = True
        if len(result.stderr) > self.max_output_bytes:
            result.stderr = result.stderr[: self.max_output_bytes]
            result.truncated = True
        return result

    @staticmethod
    def _check_dangerous(command: str) -> str | None:
        flat = " ".join(command.split())
        for pattern in DANGEROUS_PATTERNS:
            if pattern in flat:
                return pattern
        return None

    # ---------------- 文件操作（默认走 shell，子类可覆盖）----------------

    async def read(self, path: str) -> str:
        result = await self.exec(f"cat {shlex.quote(path)}")
        if not result.ok:
            raise SandboxError(f"读取失败：{result.stderr or result.stdout}")
        return result.stdout

    async def write(self, path: str, content: str) -> None:
        quoted_path = shlex.quote(path)
        parent = str(path).rsplit("/", 1)[0] if "/" in str(path) else "."
        payload = shlex.quote(content)
        result = await self.exec(
            f"mkdir -p {shlex.quote(parent)} && printf '%s' {payload} > {quoted_path}"
        )
        if not result.ok:
            raise SandboxError(f"写入失败：{result.stderr or result.stdout}")

    async def ls(self, path: str = ".") -> list[str]:
        result = await self.exec(f"ls -la {shlex.quote(path)}")
        if not result.ok:
            return []
        return [line for line in result.stdout.splitlines() if line.strip()]

    async def health(self) -> dict:
        result = await self.exec("uname -a; echo '---'; df -h . | tail -1")
        return {"ok": result.ok, "detail": result.stdout.strip() or result.stderr.strip()}


def make_sandbox(spec: SandboxSpec) -> Sandbox:
    """按模式造沙箱。延迟导入，避免没装 docker SDK 时直接崩。"""
    mode = spec.mode or "local"
    if mode == "local":
        from .local import LocalSandbox

        return LocalSandbox(spec)
    if mode == "docker":
        from .docker import DockerSandbox

        return DockerSandbox(spec)
    if mode == "remote":
        from .remote import RemoteSandbox

        return RemoteSandbox(spec)
    raise SandboxError(f"未知的沙箱模式：{mode!r}（可选 local / docker / remote）")
