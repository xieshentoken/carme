"""远端节点沙箱 —— 把命令丢到另一台机器上跑（你那台 2018 款 MBP）。

这一层就是 Grok Bot「每个 Bot 有一台专属云计算机」的等价物，
只不过那台「云计算机」是你自己闲置的笔记本。

用 SSH 而不是自研 agent：零部署、走系统已有的密钥认证、
断线自动重连，而且你能随时手动 ssh 上去看现场。

连接复用（ControlMaster）是关键：否则每条命令都要重新握手，
在局域网里也要几百毫秒，跑几十步的 Agent 会很难受。
"""

from __future__ import annotations

import asyncio
import re
import shlex
import tempfile
from pathlib import Path

from .base import ExecResult, Sandbox, SandboxError
from ..security import child_env, bounded_output


def remote_quote(path: str) -> str:
    """~ 属于远端用户，shell 引号不能把它变成字面目录名。"""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def ssh_args(settings: dict) -> list[str]:
    host, user = str(settings.get("host") or ""), str(settings.get("user") or "")
    if not host or not user:
        raise SandboxError("Bot 的执行电脑尚未配置 SSH 地址和用户；不会改在后端电脑执行")
    if (host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:%\[\]-]+", host)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user)):
        raise SandboxError("执行电脑的 SSH 地址或用户包含无效字符")
    args = ["/usr/bin/ssh", "-F", "/dev/null", "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes",
            "-o", "ForwardAgent=no", "-o", "GSSAPIAuthentication=no", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UserKnownHostsFile=" + str(Path(settings["known_hosts_file"]).expanduser() if settings.get("known_hosts_file") else "/dev/null"),
            "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(settings.get('connect_timeout', 10))}",
            "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
            "-p", str(int(settings.get("port", 22)))]
    if settings.get("identity_file"):
        args += ["-i", str(Path(settings["identity_file"]).expanduser())]
    else:
        args += ["-o", "IdentityFile=none"]
    return [*args, f"{user}@{host}"]


class RemoteSandbox(Sandbox):
    def __init__(self, spec) -> None:
        super().__init__(spec)
        s = spec.settings
        self.host = str(s.get("host", "")).strip()
        self.port = int(s.get("port", 22))
        self.user = str(s.get("user", "")).strip()
        self.identity = str(s.get("identity_file", ""))
        self.remote_root = str(s.get("root", "~/carme-node/workspaces"))
        self.use_docker = bool(s.get("use_docker", False))
        self.docker_memory = str(s.get("docker_memory", "2g"))
        self.connect_timeout = int(s.get("connect_timeout", 10))
        self.health_command = str(s.get("health_check_command", "uname -a"))

        if not self.host or not self.user:
            raise SandboxError(
                "Bot 的执行电脑尚未配置 SSH 地址和用户；不会改在后端电脑执行"
            )

        self._remote_dir = f"{self.remote_root}/{spec.agent_id}/{spec.task_id}"
        self._temporary = tempfile.TemporaryDirectory(prefix="carme-ssh-", dir="/tmp")
        self._child_home = Path(self._temporary.name)
        (self._child_home / "tmp").mkdir()
        self._control_path = str(self._child_home / "control")
        self._connected = False

    @property
    def workdir(self) -> str:
        return self._remote_dir

    @property
    def workspace(self) -> str:
        """共享根：同一 remote_root 下所有任务目录互相可读。"""
        return self.remote_root

    # ---------------- SSH 通道 ----------------

    def _ssh_args(self, *, tty: bool = False) -> list[str]:
        args = ssh_args(self.spec.settings)
        target = args.pop()
        args += [
            # 连接复用：第一条命令建连，后续几十条都走同一条 TCP
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=120s",
        ]
        if tty:
            args.append("-t")
        args.append(target)
        return args

    async def _ssh(self, command: str, timeout: int) -> ExecResult:
        args = self._ssh_args()
        proc = await asyncio.create_subprocess_exec(
            *args,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(self._child_home), start_new_session=True,
        )
        self._connected = True
        out, err = await bounded_output(proc, timeout=timeout, limit=self.max_output_bytes)
        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            ok=(proc.returncode == 0),
        )

    # ---------------- 生命周期 ----------------

    async def setup(self) -> None:
        if self._ready:
            return

        probe = await self._ssh("echo carme-ok", timeout=self.connect_timeout + 5)
        if not probe.ok or "carme-ok" not in probe.stdout:
            raise SandboxError(
                f"连不上远端节点 {self.user}@{self.host}:{self.port}。\n"
                f"排查顺序：\n"
                f"  1. 目标机是否开机、是否在同一个网络（ping {self.host}）\n"
                f"  2. 目标机是否开了「远程登录」（系统设置 → 通用 → 共享 → 远程登录）\n"
                f"  3. 公钥是否已装到目标机：ssh-copy-id -i {self.identity}.pub {self.user}@{self.host}\n"
                f"原始错误：{(probe.stderr or probe.stdout).strip()[:300]}"
            )

        created = await self._ssh(f"mkdir -p {remote_quote(self._remote_dir)}", timeout=20)
        if not created.ok:
            raise SandboxError(f"无法创建远端工作目录：{created.stderr.strip()[:300]}")

        if self.use_docker:
            ready = await self._ssh("command -v docker >/dev/null && echo yes", timeout=20)
            if "yes" not in ready.stdout:
                raise SandboxError(
                    "远端配置了 use_docker: true，但那台机器上没有 docker。"
                    "要么装上 colima+docker，要么把 use_docker 改回 false。"
                )

        self._ready = True

    async def _run(self, command: str, cwd: str | None, timeout: int) -> ExecResult:
        workdir = self._resolve_cwd(cwd)
        inner = f"cd {remote_quote(workdir)} && {command}"

        if self.use_docker:
            inner = (
                f"docker run --rm -i -m {self.docker_memory} --cpus 2 "
                f"-v {remote_quote(workdir)}:/workspace -w /workspace "
                f"carme/sandbox:latest sh -lc {shlex.quote(command)}"
            )

        return await self._ssh(inner, timeout=timeout)

    async def teardown(self) -> None:
        from ..engines import _stop_process_group
        try:
            if self._connected:
                args = self._ssh_args()
                target = args.pop()
                proc = await asyncio.create_subprocess_exec(*args, "-O", "exit", target,
                                                            stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL,
                                                            env=child_env(self._child_home),
                                                            start_new_session=True)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                finally:
                    await _stop_process_group(proc)
        finally:
            self._connected = False
            self._ready = False
            self._temporary.cleanup()

    # ---------------- 文件操作（直接走远端路径）----------------

    async def read(self, path: str) -> str:
        result = await self._ssh(f"cat {remote_quote(self._abs(path))}", timeout=60)
        if not result.ok:
            raise SandboxError(f"远端读取失败：{result.stderr.strip()}")
        return result.stdout

    async def write(self, path: str, content: str) -> None:
        target = self._abs(path)
        parent = target.rsplit("/", 1)[0]
        payload = shlex.quote(content)
        result = await self._ssh(
            f"mkdir -p {remote_quote(parent)} && printf '%s' {payload} > {remote_quote(target)}",
            timeout=60,
        )
        if not result.ok:
            raise SandboxError(f"远端写入失败：{result.stderr.strip()}")

    async def ls(self, path: str = ".") -> list[str]:
        result = await self._ssh(f"ls -la {remote_quote(self._abs(path))}", timeout=30)
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _abs(self, path: str) -> str:
        """相对路径 = 本任务目录；显式绝对路径（含 ~）原样使用。

        约定：跨任务读写要写从共享根开始的完整路径，别用 `../` 爬到别人目录；
        相对路径里的 `..` 不会越过本任务目录。
        """
        if path.startswith("/") or path.startswith("~"):
            return path
        return self._join_task(path)

    def _resolve_cwd(self, cwd: str | None) -> str:
        """相对 cwd 落在本任务目录；绝对路径（含 ~）原样使用。

        远端是账号级权限、不是安全边界，这里只保证「相对 = 本任务目录」，
        免得 cwd 被远端 shell 解析成 home 下的随机位置。
        """
        if not cwd:
            return self._remote_dir
        if cwd.startswith("/") or cwd.startswith("~"):
            return cwd
        return self._join_task(cwd)

    def _join_task(self, path: str) -> str:
        parts: list[str] = []
        for chunk in path.split("/"):
            if chunk in ("", "."):
                continue
            if chunk == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(chunk)
        return "/".join([self._remote_dir, *parts]) if parts else self._remote_dir

    async def health(self) -> dict:
        result = await self._ssh(self.health_command, timeout=30)
        return {
            "ok": result.ok,
            "mode": "remote",
            "node": f"{self.user}@{self.host}:{self.port}",
            "workdir": self._remote_dir,
            "docker": self.use_docker,
            "detail": result.stdout.strip() or result.stderr.strip(),
        }
