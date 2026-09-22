"""记忆工具 —— 让 Bot 跨任务记住事情。

这是 Grok Bot 那种「用久了它越来越懂你」的基础。
每个 Bot 有自己的私有命名空间；团队共享内容使用独立命名空间，
只有明确标为 shared 的条目才向其他 Bot 提供。
"""

from __future__ import annotations

import json

from .base import Tool, ToolContext

SHARED_AGENT = "__shared__"


class RememberTool(Tool):
    name = "remember"
    description = (
        "把一条值得长期保留的信息存进你的记忆。\n"
        "适合存：用户的偏好和习惯、项目约定、踩过的坑、可复用的结论。\n"
        "不适合存：一次性的中间结果、能现查到的公开事实。\n"
        "更新已有 key 必须提供 recall 返回的 expected_version，避免覆盖新事实。共享写入需要管理员单独授权。"
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
            "scope": {"type":"string","enum":["bot","user","project","task"]},
            "scope_id": {"type":"string"},
            "expected_version": {"type":"integer","minimum":0},
            "constraint": {"type":"boolean"},
            "expires_at": {"type":"number"},
        },
        "required": ["key", "value"],
    }

    async def run(self, ctx: ToolContext, key: str, value: str, shared: bool = False,
                  scope='bot',scope_id='',expected_version=None,constraint=False,expires_at=None) -> str:
        if shared:scope,scope_id='user','shared'
        scope_id=scope_id or (ctx.agent.id if scope=='bot' else ctx.task_id if scope=='task' else '')
        result=ctx.store.memory_write(ctx.agent.id,scope,scope_id,key,value,task_id=ctx.task_id,
            expected_version=expected_version,constraint=constraint,expires_at=expires_at)
        return '已记住：'+json.dumps(result,ensure_ascii=False)


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
            "query":{"type":"string"},"offset":{"type":"integer","minimum":0},
            "limit":{"type":"integer","minimum":1,"maximum":100},
            "value_offset":{"type":"integer","minimum":0},
        },
    }

    async def run(self, ctx: ToolContext, key: str | None = None, include_shared: bool = True,
                  query='',offset=0,limit=20,value_offset=0) -> str:
        rows=ctx.store.memory_search(ctx.agent.id,task_id=ctx.task_id,key=key,query=query,
            scope=None if include_shared else 'bot',offset=offset,limit=min(100,max(1,limit)))

        if not rows:
            return "记忆里没有相关内容。" if key else "你的记忆还是空的。"

        entries=[];size=0
        for row in rows:
            value=row['value'];end=min(len(value),value_offset+2500)
            entry={**row,'value':value[value_offset:end],'value_chars':len(value),
                   'value_offset':value_offset,'next_value_offset':end if end<len(value) else None}
            item_size=len(json.dumps(entry,ensure_ascii=False))
            if entries and size+item_size>9500:break
            entries.append(entry);size+=item_size
        refs=ctx.extras.setdefault('memory_refs',[])
        for entry in entries:
            ref={k:entry[k] for k in ('scope','scope_id','key','version')}
            if ref not in refs:refs.append(ref)
        ctx.store.update_task_meta(ctx.task_id,{'memory_refs':refs})
        return json.dumps({'entries':entries,'offset':offset,'next_offset':offset+len(entries) if len(entries)<len(rows) or len(rows)==limit else None},ensure_ascii=False)


class ForgetTool(Tool):
    name = "forget"
    description = "删掉一条已经过时或错误的记忆。"
    parameters = {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "要撤销的键名"},
                       "expected_version":{"type":"integer","minimum":1}},
        "required": ["key"],
    }

    async def run(self, ctx: ToolContext, key: str, expected_version=None) -> str:
        ctx.store.memory_write(ctx.agent.id,'bot',ctx.agent.id,key,'',task_id=ctx.task_id,
                              expected_version=expected_version,revoke=True)
        return f"已忘记：{key}"
