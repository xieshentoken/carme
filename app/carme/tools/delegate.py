"""delegate 工具 —— 主控把任务派给成员 Bot，并等它干完。

这是整个「AI 员工团队」的关键接口：主控不是自己干活，
而是通过它把活分出去、收回来、再汇总。

实现上是同步等待（子任务完成才返回），但对模型来说就是一次普通工具调用，
所以主控的推理循环不需要为异步派发做任何特殊处理。
"""

from __future__ import annotations

from .base import Tool, ToolContext


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "把一个子任务派给团队成员去做，并等它完成，返回它的成果。\n"
        "多个子任务互不依赖时，在一次回复里同时发起多个 delegate 调用，它们会并行执行。\n"
        "\n"
        "派活时务必写清楚三件事：\n"
        "  1. 要它做什么（具体到可验收）\n"
        "  2. 需要什么背景（它看不到你和用户的对话原文）\n"
        "  3. 要什么形式的产出\n"
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "成员 ID，通过 list_agents 查询当前实际可委派成员。",
            },
            "goal": {
                "type": "string",
                "description": "任务描述。要自包含 —— 把背景、约束、期望产出都写进去。",
            },
            "title": {
                "type": "string",
                "description": "给这个子任务起个短标题，界面上会显示",
            },
            "envelope": {"type":"object","description":"输入 input_artifact_ids、expected_outputs 文件名、acceptance_checks、deadline、budget；权限仍由父子交集决定。"},
        },
        "required": ["agent", "goal"],
    }

    async def run(self, ctx: ToolContext, agent: str, goal: str, title: str = "", envelope=None) -> str:
        if not ctx.agent.can_delegate:
            return (
                f"[权限拒绝] {ctx.agent.name} 没有被授予派活权限。"
                "请在 config/agents.yaml 里给它设 can_delegate: true。"
            )
        if ctx.delegate is None:
            return "[错误] 运行时没有注入派发能力。"

        target = (agent or "").strip().lstrip("@")
        if target == ctx.agent.id:
            return "[错误] 不能派活给自己，那会死循环。请直接自己完成，或派给其他成员。"

        await ctx.notify("delegate.start", {"to": target, "title": title or goal[:50]})
        try:
            result = await ctx.delegate(target, goal, title, envelope=envelope) if envelope is not None else await ctx.delegate(target, goal, title)
        except Exception as exc:  # noqa: BLE001
            await ctx.notify("delegate.failed", {"to": target, "error": str(exc)[:300]})
            return f"[派发失败] 给 {target} 的任务没能完成：{exc}"

        await ctx.notify("delegate.end", {"to": target, "title": title or goal[:50]})
        return f"—— {target} 的回报 ——\n{result}"


class ListAgentsTool(Tool):
    name='list_agents'
    description='列出当前配置中、满足父任务执行目标和会话范围的成员及可委派权限。'
    parameters={'type':'object','properties':{}}

    async def run(self, ctx, **kwargs):
        import json
        execution=getattr(ctx.browser_manager,'execution',None)
        if execution is None:raise ValueError('runtime_required')
        runtime=execution.runtime;parent=ctx.store.get_task(ctx.task_id);items=[]
        conversation=ctx.store.get_conversation(parent['conversation_id']) if parent.get('conversation_id') else None
        for spec in runtime.config.agents.agents.values():
            if spec.id==ctx.agent.id:continue
            if conversation and conversation['kind']=='group' and spec.id not in conversation['agent_ids']:continue
            try:_,snapshot=runtime._task_agent_snapshot(spec.id,parent)
            except ValueError:continue
            if ctx.store.task_context(ctx.task_id)['context_mode'] == 'visitor_group':
                from .base import COMMON_CONTEXT_TOOLS
                snapshot['policy']['tools'] = sorted(set(snapshot['policy']['tools']) & COMMON_CONTEXT_TOOLS)
            items.append({'id':spec.id,'name':spec.name,'title':spec.title,'tools':snapshot['policy']['tools'],
                          'execution_target':snapshot['execution_target']})
        return json.dumps(items,ensure_ascii=False)
