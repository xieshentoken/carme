"""Agent 运行时 —— 一个角色跑一个「思考 → 调工具 → 看结果」的循环。

没有用任何 agent 框架：核心循环就一百多行，
自己写的好处是每一步都可见、可打断、可记成本，
出了问题不用去猜框架里发生了什么。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import AgentSpec, Config
from ..engines import BRIDGE_TOOL_NAMES, resolve_workspace
from ..llm import LLMGateway, LLMResponse, Usage
from ..store import Store
from ..tools.base import ToolContext, ToolRegistry

log = logging.getLogger("carme.agent")

MAX_TOOL_RESULT_CHARS = 12_000


@dataclass
class AgentRun:
    """一次 Agent 执行的完整记录。"""

    task_id: str
    agent_id: str
    output: str = ""
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    status: str = "done"
    error: str = ""
    transcript: list[dict] = field(default_factory=list)
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done"


class Agent:
    """一个角色实例。无状态 —— 每次任务新建一个，避免上下文串味。"""

    def __init__(
        self,
        spec: AgentSpec,
        config: Config,
        gateway: LLMGateway,
        registry: ToolRegistry,
        store: Store,
    ) -> None:
        self.spec = spec
        self.config = config
        self.gateway = gateway
        self.registry = registry
        self.store = store

    # ---------------- 人设组装 ----------------

    def system_prompt(self) -> str:
        """拼出这个角色的完整系统提示。

        顺序有讲究：先身份，再纪律，再环境事实。
        环境事实放在最后，因为它是模型每次都要用到的「当下情况」。
        """
        defaults = self.config.agents.defaults
        max_steps = defaults.get("max_steps", 24)

        parts = [
            f"你是「{self.spec.name}」，职位是{self.spec.title or '团队成员'}。",
            "",
            self.spec.prompt,
            "",
            "## 通用工作纪律",
            f"- 你有最多 {max_steps} 步工具调用预算，用完必须给出结论，所以别浪费在无关探索上。",
            "- 需要事实就去查，不要凭印象编造。查不到就明说查不到。",
            "- 工具报错时先读懂错误信息再重试，同样的错不要犯第三次。",
            "- 最终回复直接给成果，不要复述任务、不要写「好的我来帮你」这类过渡话。",
            "- 如果任务本身有问题（信息不足、前提错误、超出你的能力），直接指出来，不要硬做。",
        ]

        # 把这个角色能用的工具列出来，减少它瞎猜工具名
        tool_names = self.registry.names()
        available = [n for n in self.registry.expand(self.spec.tools)
                     if n in tool_names and (self.spec.engine == "api" or n in BRIDGE_TOOL_NAMES)]
        if available:
            parts += ["", f"## 你可用的工具\n{', '.join(available)}"]
        if self.spec.engine != "api":
            parts += ["", "## 当前引擎边界\n当前使用本机 CLI 引擎，进程默认从后端 Mac 的任务工作目录启动；原生终端和文件的实际访问范围由 CLI 权限模式与 Mac 账号权限决定，不要假定它等同于 API 的 OS / SSH 沙箱。列表中的 Carme 工具通过任务级桥接执行并受当前 Bot 权限、审批和预算约束。不要把后端 Mac 描述成远程执行电脑，也不要声称完成未实际执行的操作。"]

        return "\n".join(p for p in parts if p is not None)

    # ---------------- 主循环 ----------------

    async def run(
        self,
        goal: str,
        *,
        task_id: str,
        sandbox_handle=None,
        browser_manager=None,
        node: dict | None = None,
        delegate: Callable[[str, str, str], Awaitable[str]] | None = None,
        approve: Callable[..., Awaitable[Any]] | None = None,
        emit: Callable[[str, dict], Awaitable[None]] | None = None,
        extra_context: str = "",
        max_steps: int | None = None,
        history: list[dict] | None = None,
        read_input: Callable[[bool], list[str]] | None = None,
        check_budget: Callable[[], None] | None = None,
    ) -> AgentRun:
        started = time.time()
        run = AgentRun(task_id=task_id, agent_id=self.spec.id)

        defaults = self.config.agents.defaults
        step_budget = max_steps or int(defaults.get("max_steps", 24))

        ctx = ToolContext(
            agent=self.spec,
            task_id=task_id,
            store=self.store,
            sandbox_handle=sandbox_handle,
            browser_manager=browser_manager,
            node=node,
            delegate=delegate,
            approve=approve,
            emit=emit,
        )

        conversation = self.store.get_task(task_id) or {}
        allowed_tools = self.spec.tools + (["read_attachment", "create_artifact"] if conversation.get("conversation_id") else [])
        ctx.extras["allowed_tools"] = allowed_tools
        bridge_allowed = [name for name in self.registry.expand(allowed_tools) if name in BRIDGE_TOOL_NAMES]
        tool_specs = self.registry.specs_for(allowed_tools if self.spec.engine == "api" else bridge_allowed)
        cli_workspace = (str(resolve_workspace(self.spec.id, self.spec.engine_workspace,
                                               getattr(self.config, "data_dir", None) or self.config.root / "data"))
                         if self.spec.engine != "api" else "")

        # 长期记忆直接灌进第一条用户消息，比让模型自己想起来更可靠
        from ..tools.memory import SHARED_AGENT
        memories = self.store.recall(self.spec.id) + self.store.recall(SHARED_AGENT)
        memory_block = ""
        if memories:
            lines = [f"- [{'共享' if m['agent_id'] == SHARED_AGENT else '私有'}] {m['key']}: {m['value'][:1000]}" for m in memories[:40]]
            memory_block = "\n\n[你的长期记忆]\n" + "\n".join(lines)
            memory_block += "\n（这是记忆预览；需要完整值或更多条目时调用 recall。）"

        user_content = goal
        if extra_context:
            user_content = f"{goal}\n\n[补充背景]\n{extra_context}"
        if memory_block:
            user_content += memory_block

        from ..attachments import message_content
        first_message = next((m for m in self.store.list_conversation_messages(conversation.get("conversation_id", ""))
                              if m["task_id"] == task_id and m["role"] == "user"), None)
        files = first_message["attachments"] if first_message else []
        messages: list[dict] = list(history or []) + [{"role": "user", "content": message_content(self.store, user_content, files)}]
        self.store.add_message(task_id, self.spec.id, "user", user_content, step=0)

        async def publish(text: str, response=None) -> None:
            if emit is not None and text and not (response and response.streamed and response.text):
                await emit("assistant.message", {"content": text,
                    "model": response.model if response else "", "provider": response.provider if response else ""})

        async def stream(payload):
            if emit:
                await emit("assistant.stream", payload)

        bridge_step = {"value": 0}
        cli_tool_calls = {"value": 0}

        def claim_cli_tool() -> bool:
            if cli_tool_calls["value"] >= step_budget:
                return False
            if check_budget:
                check_budget()
            cli_tool_calls["value"] += 1
            return True

        async def cli_tool_execute(name: str, arguments: dict) -> str:
            if name not in bridge_allowed:
                return f"[权限拒绝] 当前 Bot 没有 Carme 工具 {name} 的权限。"
            if any(key in arguments for key in ("task_id", "node", "agent_id")):
                return "[权限拒绝] 工具参数不能覆盖当前任务、Bot 或执行边界。"
            if not claim_cli_tool():
                return "[工具预算已用尽] 当前 CLI 任务不能再执行工具，请直接给出已有资料的结论。"
            await ctx.notify("tool.start", {"tool": name, "source": "cli"})
            result = await self.registry.execute(ctx, name, arguments)
            if len(result) > MAX_TOOL_RESULT_CHARS:
                result = result[:MAX_TOOL_RESULT_CHARS] + "\n[...结果过长已截断]"
            self.store.add_message(task_id, self.spec.id, "tool", result,
                                   tool_name=name, step=bridge_step["value"])
            await ctx.notify("tool.end", {"tool": name, "source": "cli", "chars": len(result)})
            images = ctx.extras.pop("images", [])
            if images:
                content = [{"type": "text", "text": result}]
                for image in images:
                    url = ((image.get("image_url") or {}).get("url")
                           if isinstance(image, dict) else "")
                    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                        header, data = url.split(";base64,", 1)
                        content.append({"type": "image", "mimeType": header[5:], "data": data})
                return {"text": result, "content": content}
            return result

        async def cli_tool_event(type_: str, payload: dict) -> None:
            if type_ == "tool.start" and not claim_cli_tool():
                raise RuntimeError("CLI 工具调用已达到当前任务预算")
            if emit is not None:
                await emit(type_, payload)

        def append_input(items: list[str], step: int) -> None:
            for content in items:
                messages.append({"role": "user", "content": content})
                logged = content if isinstance(content, str) else "\n".join(b.get("text", "[图片附件]") for b in content)
                self.store.add_message(task_id, self.spec.id, "user", logged, step=step)

        for step in range(1, step_budget + 1):
            run.steps = step
            bridge_step["value"] = step
            try:
                if read_input:
                    append_input(read_input(False), step)
                if check_budget:
                    check_budget()
                response = await self._think(messages, tool_specs, stream if emit else None,
                                             cli_tool_execute if self.spec.engine != "api" else None,
                                             cli_workspace, step_budget,
                                             cli_tool_event if self.spec.engine != "api" else None)
            except Exception as exc:  # noqa: BLE001
                run.status = "failed"
                run.error = str(exc)
                run.output = f"执行失败：{exc}"
                self.store.add_message(task_id, self.spec.id, "assistant", run.output, step=step)
                await publish(run.output)
                break

            run.usage = run.usage + response.usage
            self._log_usage(task_id, response)

            # 没有工具调用 = 这就是最终答案
            if not response.wants_tools:
                run.output = response.text or "(没有产出内容)"
                self.store.add_message(task_id, self.spec.id, "assistant", run.output, step=step)
                # 模型等待期间到达的用户补充，必须在结束前处理。
                pending = read_input(True) if read_input else []
                await publish(run.output, response)
                if pending:
                    messages.append({"role": "assistant", "content": run.output})
                    append_input(pending, step)
                    if step < step_budget:
                        continue
                    run.status = "truncated"
                    run.output = "已达步数上限，最新补充要求尚未完成。请继续发消息处理。"
                    self.store.add_message(task_id, self.spec.id, "assistant", run.output, step=step)
                    await publish(run.output)
                break

            # 有工具调用：先把模型的意图记下来
            if response.text:
                self.store.add_message(
                    task_id, self.spec.id, "assistant", response.text, step=step
                )
                await publish(response.text, response)
            messages.append(self._assistant_message(response))

            # 并行执行这一轮的所有工具调用
            results = await self._execute_tools(ctx, response, step)
            for call, result_text in results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result_text,
                    }
                )
                self.store.add_message(
                    task_id,
                    self.spec.id,
                    "tool",
                    result_text,
                    tool_name=call.name,
                    step=step,
                )

            images = ctx.extras.pop("images", [])
            if images:
                messages.append({"role": "user", "content": [{"type": "text", "text": "工具读取的图片资料："}, *images]})

            if step == step_budget:
                run.output = (
                    f"（已达 {step_budget} 步工具预算上限，强制收尾）\n\n"
                    + (response.text or "未能得出最终结论。")
                )
                run.status = "truncated"
                self.store.add_message(
                    task_id, self.spec.id, "assistant", run.output, step=step
                )
                await publish(run.output)

        run.duration = time.time() - started
        run.transcript = messages
        return run

    # ---------------- 内部 ----------------

    async def _think(self, messages: list[dict], tool_specs: list[dict], on_stream=None,
                     cli_tool_execute=None, cli_workspace: str = "", cli_max_tool_calls: int = 32,
                     cli_tool_event=None) -> LLMResponse:
        return await self.gateway.chat(
            messages,
            engine=self.spec.engine,
            engine_model=self.spec.engine_model,
            tier=self.spec.tier,
            model=self.spec.model or None,
            tools=tool_specs or None,
            system_extra=self.system_prompt(),
            effort=(self.spec.engine_effort if self.spec.engine != "api" else self.spec.effort) or None,
            **({"on_stream": on_stream} if on_stream else {}),
            cli_tool_execute=cli_tool_execute,
            cli_workspace=cli_workspace,
            cli_max_tool_calls=cli_max_tool_calls,
            cli_tool_event=cli_tool_event,
        )

    async def _execute_tools(self, ctx: ToolContext, response: LLMResponse, step: int):
        """同一轮里的多个工具调用并行跑。主控派活给多个成员时靠的就是这个。"""

        async def one(call):
            if call.name not in self.registry.expand(ctx.extras.get("allowed_tools", self.spec.tools)):
                return call, f"[权限拒绝] 当前 Bot 没有工具 {call.name} 的权限。"
            text = await self.registry.execute(ctx, call.name, call.arguments)
            if len(text) > MAX_TOOL_RESULT_CHARS:
                text = text[:MAX_TOOL_RESULT_CHARS] + "\n[...结果过长已截断]"
            return call, text

        # 浏览器、文件及命令可能依赖前一个动作的结果，按模型给定顺序执行。
        # 只有独立成员委派允许并行，子任务仍各自遵守执行资源限制。
        if all(call.name == "delegate" for call in response.tool_calls):
            return await asyncio.gather(*(one(call) for call in response.tool_calls))
        return [await one(call) for call in response.tool_calls]

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict:
        return {
            "role": "assistant",
            "content": response.text,
            **({"_response_items": response.response_items} if response.response_items else {}),
            **({"_anthropic_content": response.anthropic_content} if response.anthropic_content else {}),
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                }
                for call in response.tool_calls
            ],
        }

    def _log_usage(self, task_id: str, response: LLMResponse) -> None:
        usage = response.usage
        self.store.log_usage(
            task_id=task_id,
            agent_id=self.spec.id,
            provider=response.provider,
            model=response.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            cost_known=usage.cost_known,
            tokens_known=usage.tokens_known,
        )
        self.store.add_task_usage(
            task_id, usage.cost_usd, usage.prompt_tokens + usage.completion_tokens,
            cost_known=usage.cost_known, tokens_known=usage.tokens_known,
        )
