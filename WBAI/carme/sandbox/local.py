"""本机受限目录沙箱 —— 零依赖，立刻能跑。

用途：先把整条链路跑通，以及在远端节点没就绪时兜底。
代价：它只是「把 cwd 锁在一个目录里」，绝对路径仍能碰到宿主文件，
      所以它不是安全边界。要真隔离请用 docker 或 remote 模式。

两级目录：共享根（workspace）+ 每任务子目录。
  相对路径一律落在本任务目录（默认 cwd 与 HOME），
  跨任务读写必须写从共享根开始的显式路径 —— 这样接力任务能捡起
  上一个任务的产物，又不会因为随手写相对路径踩到别人的文件。
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from .base import ExecResult, Sandbox, SandboxError


class LocalSandbox(Sandbox):
    def __init__(self, spec) -> None:
        super().__init__(spec)
        root = Path(spec.settings.get("root", "./data/workspaces")).expanduser().resolve()
        # 共享根：同一 workspace 下所有任务目录互相可见
        self._workspace = root
        if spec.settings.get("per_task_dir", True):
            self._root = root / spec.agent_id / spec.task_id
        else:
            self._root = root / spec.agent_id
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def workdir(self) -> str:
        return str(self._root)

    @property
    def workspace(self) -> str:
        return str(self._workspace)

    async def setup(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._ready = True

    async def _run(self, command: str, cwd: str | None, timeout: int) -> ExecResult:
        workdir = self._resolve_cwd(cwd)
        env = dict(os.environ)
        # 不给子进程看到我们的密钥，减少被 prompt injection 顺走的风险
        for key in list(env):
            if key.endswith("_API_KEY") or key in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}:
                env.pop(key, None)
        env["CARME_SANDBOX"] = "local"
        env["HOME"] = str(self._root)

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workdir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh",
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
        )

    def _resolve_cwd(self, cwd: str | None) -> str:
        if not cwd:
            return str(self._root)
        candidate = Path(cwd)
        if not candidate.is_absolute():
            # 相对路径仍然落在本任务目录
            candidate = self._root / candidate
        candidate = candidate.resolve()
        # 共享根内自由活动（可以进别的任务目录读产物），越出共享根就拉回本任务目录
        try:
            candidate.relative_to(self._workspace)
        except ValueError:
            return str(self._root)
        return str(candidate) if candidate.exists() else str(self._root)

    async def write(self, path: str, content: str) -> None:
        target = Path(self._resolve_write_path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def read(self, path: str) -> str:
        target = Path(self._resolve_write_path(path))
        if not target.exists():
            raise SandboxError(f"文件不存在：{target}")
        return target.read_text(encoding="utf-8", errors="replace")

    def _resolve_write_path(self, path: str) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self._workspace)
        except ValueError as exc:
            raise SandboxError(f"拒绝写到 workspace 之外：{path}") from exc
        return str(candidate)

    async def ls(self, path: str = ".") -> list[str]:
        target = Path(self._resolve_cwd(path))
        if not target.exists():
            return []
        entries = []
        for item in sorted(target.iterdir()):
            size = item.stat().st_size if item.is_file() else 0
            kind = "d" if item.is_dir() else "f"
            entries.append(f"{kind} {size:>9} {item.name}")
        return entries

    async def health(self) -> dict:
        result = await self.exec("uname -a; echo '---'; df -h . | tail -1")
        return {
            "ok": result.ok,
            "mode": "local",
            "workdir": self.workdir,
            "detail": result.stdout.strip() or result.stderr.strip(),
        }
