"""工具基类与注册表。

工具 = Agent 的手。每个角色的手伸多长，由 config/agents.yaml 里
的 tools 列表决定 —— 情报员能上网不能改文件，审校官能读不能写。
这就是「给 Bot 分职责」在代码层面的落点。

配置里可以写工具分组名（如 memory / files / browser / computer），
比让用户逐个记 read_file / write_file 友好得多。

两个浏览器相关的分组要分清：
    browser   检索：搜网页、抓正文（只读，无登录态，无副作用）
    computer  代操作：打开/点击/输入/登录（有副作用，带人工确认闸门）
把有副作用的工具单独成组，是为了让「谁能代我操作账号」这件事在配置里一眼可见。
"""
from __future__ import annotations

import abc
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..agents.base import AgentSpec
    from ..browser import BrowserManager
    from ..bus import EventBus
    from ..mcp import MCPManager
    from ..sandbox import Sandbox, SandboxHandle
    from ..skills import SkillManager
    from ..store import Store

log = logging.getLogger("carme.tools")

# 配置里可用的分组别名 -> 实际工具名
TOOL_GROUPS: dict[str, list[str]] = {
    "memory": ["remember", "recall", "forget"],
    "files": ["read_file", "write_file", "list_files", "share_attachment"],
    # 只读检索：不碰登录态，无副作用
    "browser": ["web_search", "fetch_page"],
    # 代操作：能点你账号里的按钮，所以自带人工确认闸门
    "computer": [
        "web_open",
        "web_snapshot",
        "web_click",
        "web_type",
        "web_press",
        "web_scroll",
        "web_back",
        "web_screenshot",
        "web_login",
        "web_login_wait",
        "web_close",
    ],
    "exec": ["shell"],
    "team": ["delegate", "list_agents"],
    # 技能：读「探索 Bot → 已安装的 Skill」里装好的 SKILL.md
    "skill": ["list_skills", "use_skill"],
    # MCP：展开成「当前已连上的外部 MCP Server 的全部工具」，
    # 具体内容随连接状态变化，由 ToolRegistry 的动态分组在运行时算。
    "mcp": [],
}


@dataclass
class ToolContext:
    """工具执行时能看到的一切。"""

    agent: "AgentSpec"
    task_id: str
    store: "Store"
    bus: "EventBus | None" = None
    # 沙箱按需占用：只有真的要跑命令时才申请槽位。
    # 这是为了避免「主控占着沙箱等下属、下属又要抢沙箱」的死锁。
    sandbox_handle: "SandboxHandle | None" = None
    # 浏览器管理器。代操作工具每次调用各自申请句柄、用完即还，
    # 所以这里给的是管理器而不是一个长期占用的句柄。
    browser_manager: "BrowserManager | None" = None
    # 本任务固定的执行电脑；{} 表示尚未配置，不能自动落到后端。
    node: dict[str, Any] | None = None
    # 由运行时注入：派发子任务并等待结果
    delegate: Callable[[str, str, str], Awaitable[str]] | None = None
    # 由运行时注入：不可逆动作的人工确认闸门
    approve: Callable[..., Awaitable["ApprovalOutcome"]] | None = None
    # 由运行时注入：向界面推一条即时状态
    emit: Callable[[str, dict], Awaitable[None]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    async def notify(self, type_: str, payload: dict | None = None) -> None:
        if self.emit is not None:
            await self.emit(type_, payload or {})

    async def sandbox(self) -> "Sandbox":
        """取执行环境。没配或拿不到就直接报错，让模型看懂并调整策略。"""
        if self.sandbox_handle is None:
            raise RuntimeError("target_unassigned: 本任务没有获准使用的执行目标")
        return await self.sandbox_handle.get()

    async def request_approval(
        self, *, kind: str, summary: str, detail: dict | None = None
    ) -> "ApprovalOutcome":
        """请人确认一个不可逆动作。

        没有闸门时一律拒绝 —— 这是刻意的「失败安全」：
        宁可这一步不做，也不能在没人把关的情况下把钱付出去、把东西删掉。
        """
        from ..approval import ApprovalOutcome

        if self.approve is None:
            return ApprovalOutcome(
                False,
                "当前运行时没有接入人工确认闸门，出于安全考虑这一步没有执行。",
            )
        from ..security import digest
        check = self.extras.get("check_policy")
        if check:
            check()
        def current_binding():
            return digest({"detail": detail or {}, "call": self.extras.get("call", {}), "node": self.node,
                           "policy": self.extras.get("policy", {})})
        binding = current_binding()
        decision = await self.approve(kind=kind, summary=summary,
                                      detail={**deepcopy(detail or {}), "action_digest": binding,
                                              "execution_target": deepcopy(self.extras.get("policy", {}).get("target"))})
        if check:
            check()
        if current_binding() != binding:
            return ApprovalOutcome(False, "approval_binding_changed: 参数或目标变化，需要重新审批")
        return decision


class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}}

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        """执行工具，返回值会作为工具结果喂回模型。"""

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, dynamic_groups: dict[str, Callable[[], list[str]]] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._dynamic: dict[str, Callable[[], list[str]]] = dict(dynamic_groups or {})

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """注销工具。MCP 断开连接时要把它的工具一起摘掉，避免模型看到死工具。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def set_dynamic_group(self, name: str, provider: Callable[[], list[str]]) -> None:
        self._dynamic[name] = provider

    def groups(self) -> list[str]:
        return sorted(set(TOOL_GROUPS) | set(self._dynamic))

    def expand(self, names: list[str]) -> list[str]:
        """把分组别名展开成真实工具名，保持顺序且去重。

        分组可以是静态的（TOOL_GROUPS）或动态的（如 mcp：跟着已连接的服务走）。
        """
        out: list[str] = []
        for name in names:
            provider = self._dynamic.get(name)
            if provider is not None:
                resolved_names = list(provider())
            else:
                resolved_names = TOOL_GROUPS.get(name, [name])
            for resolved in resolved_names:
                if resolved not in out:
                    out.append(resolved)
        return out

    def specs_for(self, names: list[str]) -> list[dict]:
        """给指定工具名生成模型能看懂的 schema。未知名字直接跳过（配置写错不至于崩）。"""
        specs = []
        for name in self.expand(names):
            tool = self._tools.get(name)
            if tool is None:
                log.warning("配置里引用了不存在的工具：%s", name)
                continue
            specs.append(tool.schema())
        return specs

    async def execute(self, ctx: ToolContext, name: str, arguments: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"[工具错误] 不存在名为 {name!r} 的工具。可用：{', '.join(self.names())}"
        try:
            policy = ctx.extras["policy"] if "policy" in ctx.extras else {"tools": self.expand(ctx.agent.tools), "max_tool_calls": 32}
            if name in {"shell", "read_file", "write_file", "list_files"} and ctx.sandbox_handle is None:
                return "[权限拒绝] target_unassigned"
            if name not in policy.get("tools", []):
                return "[权限拒绝] capability_denied"
            if set(arguments) & {"task_id", "agent_id", "bot_id", "run_id", "node", "execution_target", "policy"}:
                return "[权限拒绝] identity_override_denied"
            check = ctx.extras.get("check_policy")
            if check:
                check()
            calls = ctx.extras.get("tool_calls", 0)
            if calls >= policy.get("max_tool_calls", 32):
                return "[权限拒绝] tool_call_limit_exceeded"
            ctx.extras["tool_calls"] = calls + 1
            if ctx.store:ctx.store.claim_tool_budget(ctx.task_id)
            arguments = deepcopy(arguments)
            ctx.extras["call"] = {"name": name, "arguments": arguments}
            operation=None
            if ctx.store and (name.startswith('mcp__') or name in {'delegate','mac_action','web_click','web_type','web_press','web_login'}
                              or (policy.get('target')=='ssh' and name in {'shell','write_file'})):
                from ..security import digest
                # Stable call ordinal is checkpointed, so a resumed call finds its receipt.
                ordinal=ctx.extras.get('operation_ordinal',0)+1
                ctx.extras['operation_ordinal']=ordinal
                operation=ctx.store.operation_begin(ctx.task_id,name,digest({'name':name,'args':arguments,
                    'permission':policy.get('permission_version'),'ordinal':ordinal}))
                if operation['status']=='finished':return operation['result']
                if operation['status']!='new':raise ValueError('external_effect_reconciliation_required')
            from ..docker_browser import WEB_TOOLS
            if name in WEB_TOOLS and policy.get('target') == 'container':
                execution = getattr(ctx.browser_manager, 'execution', None)
                if execution is None:
                    raise RuntimeError('docker_browser_not_configured: no host fallback')
                result = await execution.browser_tool(ctx, name, arguments)
            else:
                result = await tool.run(ctx, **arguments)
            if operation:
                ctx.store.operation_finish(operation['id'],result,receipt={'tool':name,'ordinal':ordinal,
                    'permission_version':policy.get('permission_version')})
            limit = policy.get("max_output_bytes", 65536)
            encoded = result.encode("utf-8")
            if len(encoded) > limit:
                await ctx.notify("tool.output_limited", {"tool": name, "limit_bytes": limit})
                return encoded[:limit].decode("utf-8", errors="ignore") + "\n[output_limit_exceeded]"
            return result
        except TypeError as exc:
            return f"[工具参数错误] {name} 的参数不匹配：{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具异常要变成模型能读的文本，而不是崩掉整个任务
            log.exception("工具 %s 执行失败", name)
            return f"[工具异常] {name} 执行失败：{type(exc).__name__}: {exc}"


def build_registry(skills: "SkillManager | None" = None, mcp: "MCPManager | None" = None) -> ToolRegistry:
    """装配内置工具表。

    技能与 MCP 的管理器由 Runtime 持有并注入：技能是静态工具（读 SKILL.md），
    MCP 是动态注册（连上才有工具），所以这里只收一个引用。
    """
    from .browser import FetchPageTool, WebSearchTool
    from .delegate import DelegateTool, ListAgentsTool
    from .files import (ListFilesTool, ReadFileTool, WriteFileTool, ReadAttachmentTool, CreateArtifactTool,
                        VerifyArtifactTool, ShareAttachmentTool)
    from .memory import ForgetTool, RecallTool, RememberTool
    from .shell import ShellTool
    from .skill import ListSkillsTool, UseSkillTool, ProposeSkillTool
    from .web import WEB_TOOLS

    registry = ToolRegistry({"mcp": (mcp.tool_names if mcp is not None else (lambda: []))})
    for tool in (
        ShellTool(),
        ReadFileTool(),
        WriteFileTool(),
        ListFilesTool(),
        ReadAttachmentTool(),
        CreateArtifactTool(),
        VerifyArtifactTool(),
        ShareAttachmentTool(),
        WebSearchTool(),
        FetchPageTool(),
        RememberTool(),
        RecallTool(),
        ForgetTool(),
        DelegateTool(),
        ListAgentsTool(),
        *WEB_TOOLS,
    ):
        registry.register(tool)
    if skills is not None:
        registry.register(ListSkillsTool(skills))
        registry.register(UseSkillTool(skills))
        registry.register(ProposeSkillTool(skills))
    return registry
