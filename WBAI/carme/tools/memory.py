"""记忆工具 —— 让 Bot 跨任务记住事情。

这是 Grok Bot 那种「用久了它越来越懂你」的基础。
每个 Bot 有自己的私有命名空间；团队共享内容使用独立命名空间，
只有明确标为 shared 的条目才向其他 Bot 提供。
"""

from __future__ import annotations

from .base import Tool, ToolContext

SHARED_AGENT = "__shared__"


class RememberTool(Tool):
    name = "remember"
    description = (
        "把一条值得长期保留的信息存进你的记忆。\n"
        "适合存：用户的偏好和习惯、项目约定、踩过的坑、可复用的结论。\n"
        "不适合存：一次性的中间结果、能现查到的公开事实。\n"
        "同名的 key 会覆盖旧值，所以 key 要起得有意义，如 'user_timezone'。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "简短的英文/拼音键名，如 user_prefers_concise"},
            "value": {"type": "string", "description": "要记住的内容，写清楚一点"},
            "shared": {
                "type": "boolean",
                "description": "true 表示存到团队共享记忆，所有成员都能看到（默认 false，只自己可见）",
            },
        },
        "required": ["key", "value"],
    }

    async def run(self, ctx: ToolContext, key: str, value: str, shared: bool = False) -> str:
        owner = SHARED_AGENT if shared else ctx.agent.id
        ctx.store.remember(owner, key, value)
        scope = "团队共享" if shared else "仅自己"
        return f"已记住（{scope}）：{key} = {value[:200]}"


class RecallTool(Tool):
    name = "recall"
    description = "读回你之前记住的内容。不给 key 就列出全部。"
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "要读的键名，省略则列出全部"},
            "include_shared": {
                "type": "boolean",
                "description": "是否同时读团队共享记忆，默认 true",
            },
        },
    }

    async def run(self, ctx: ToolContext, key: str | None = None, include_shared: bool = True) -> str:
        rows = ctx.store.recall(ctx.agent.id, key)
        if include_shared and ctx.agent.id != SHARED_AGENT:
            shared = ctx.store.recall(SHARED_AGENT, key)
            seen = {(r["key"], r["value"]) for r in rows}
            rows += [r for r in shared if (r["key"], r["value"]) not in seen]

        if not rows:
            return "记忆里没有相关内容。" if key else "你的记忆还是空的。"

        lines = []
        for row in rows:
            scope = "共享" if row["agent_id"] == SHARED_AGENT else "私有"
            lines.append(f"[{scope}] {row['key']}: {row['value']}")
        return "\n".join(lines)


class ForgetTool(Tool):
    name = "forget"
    description = "删掉一条已经过时或错误的记忆。"
    parameters = {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "要删除的键名"}},
        "required": ["key"],
    }

    async def run(self, ctx: ToolContext, key: str) -> str:
        ctx.store.forget(ctx.agent.id, key)
        return f"已忘记：{key}"
