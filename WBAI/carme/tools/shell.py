"""shell 工具 —— 在沙箱里跑命令。

这是 Agent 与真实世界交互的主力工具，也是最危险的工具。
三道闸：沙箱模式隔离 + 危险命令模式拦截 + 超时与输出截断。
"""

from __future__ import annotations

from .base import Tool, ToolContext


class ShellTool(Tool):
    name = "shell"
    description = (
        "在隔离的执行环境里运行 shell 命令。用于：安装依赖、运行程序、跑测试、"
        "查看系统信息、处理文件。工作目录已经是本任务的专属目录，直接用相对路径即可。\n"
        "相对路径落在本任务目录；要读写别的任务留下的产物，用从 workspace 根开始的完整路径。\n"
        "注意：每次调用是独立的 shell，环境变量和 cd 不会保留；"
        "需要多步请用 && 串起来。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "cwd": {
                "type": "string",
                "description": "工作目录（相对路径即可，省略则用任务根目录）",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认取配置值，长任务可调大",
            },
        },
        "required": ["command"],
    }

    async def run(self, ctx: ToolContext, command: str, cwd: str | None = None, timeout: int | None = None) -> str:
        sandbox = await ctx.sandbox()
        await ctx.notify("tool.start", {"tool": "shell", "command": command[:300]})

        result = await sandbox.exec(command, cwd=cwd, timeout=timeout)

        await ctx.notify(
            "tool.end",
            {
                "tool": "shell",
                "command": command[:300],
                "exit_code": result.exit_code,
                "duration": round(result.duration, 2),
            },
        )
        return result.as_text()
