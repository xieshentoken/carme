"""技能类工具 —— 让模型自己决定要不要读某个技能。

    list_skills  看清单（只花几十个 token）
    use_skill    载入某个技能的完整说明（渐进式披露的第二跳）

系统提示里已经列了「名字 + 一句话用途」，所以多数情况下模型会直接 use_skill；
list_skills 主要是给「描述不够、想再看看全貌」的场景兜底。
"""

from __future__ import annotations

from typing import Any

from ..skills import SkillError, SkillManager
from .base import Tool, ToolContext

MAX_LISTED = 40


class ListSkillsTool(Tool):
    name = "list_skills"
    description = (
        "列出本机已安装并启用的技能（Skill）。技能是一份写给模型看的操作说明，"
        "先用本工具确认有哪些可用，再用 use_skill 载入完整步骤。"
    )
    parameters: dict = {"type": "object", "properties": {"offset":{"type":"integer","minimum":0}}, "additionalProperties": False}

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        import json
        grants=self.manager.settings().get('grants',{}).get(ctx.agent.id,{})
        offset=max(0,int(kwargs.get('offset',0)))
        items=[{'id':sid,'name':self.manager.manifest(sid,revision)['name'],'revision':revision} for sid,revision in sorted(grants.items())
               if sid not in self.manager.settings()['disabled']]
        return json.dumps({'skills':items[offset:offset+20],
            'next_offset':offset+20 if offset+20<len(items) else None},ensure_ascii=False)


class UseSkillTool(Tool):
    name = "use_skill"
    description = (
        "分页读取已授权固定版本的说明。检查 next_cursor，直到 complete=true。"
        "@manifest 列出全部文件和章节；容器输入目录为只读，始终遵守返回的 constraints。"
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "来自 list_skills 的技能 ID"},
            "cursor":{"type":"string"},"file":{"type":"string","description":"SKILL.md 或 @manifest 或 manifest 内文件路径"},
        },
        "required": ["name"],
    }

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def run(self, ctx: ToolContext, name: str = "", cursor='', file='SKILL.md', **kwargs: Any) -> str:
        import json
        task=ctx.store.get_task(ctx.task_id) if ctx.store else None
        try:
            name=self.manager.get(name).id
            page=self.manager.page(ctx.agent.id,name,cursor=cursor,file=file,task=task)
        except SkillError as exc:return '[技能错误] '+str(exc)
        if task:
            execution=getattr(ctx.browser_manager,'execution',None)
            meta=json.loads(task['meta'])
            if meta.get('execution_target')=='container':
                if execution is None:raise SkillError('skill_container_not_available')
                if ctx.extras.get('staged_skills',{}).get(name)!=page['revision']:
                    manifest=self.manager.manifest(name,page['revision'])
                    for entry in manifest['files']:
                        raw=(self.manager.root/'.versions'/name/page['revision']/entry['path']).read_bytes()
                        await execution.stage_input(ctx.task_id,'skills/'+name+'/'+page['revision']+'/'+entry['path'],raw)
                    ctx.extras.setdefault('staged_skills',{})[name]=page['revision']
            elif page['file_count']>1:
                raise SkillError('skill_bundle_requires_container')
            used=meta.get('skills_used',{});used[name]=page['revision']
            ctx.store.update_task_meta(ctx.task_id,{'skills_used':used})
        return json.dumps(page,ensure_ascii=False)


class ProposeSkillTool(Tool):
    name = 'propose_skill'
    description = '从你已完成、实际验收并获用户认可的任务提出脱敏 Skill 候选。只提交待审候选；不能发布或授予权限。需要单独授权本工具。'
    parameters = {'type':'object','properties':{'source_task_id':{'type':'string'},'name':{'type':'string'},
        'document':{'type':'string'},'private_literals':{'type':'array','items':{'type':'string'}},
        'files':{'type':'object','description':'可选的相对路径到文本脚本或模板内容映射'}},
        'required':['source_task_id','name','document','private_literals'],'additionalProperties':False}

    def __init__(self, manager: SkillManager) -> None:
        self.manager=manager

    async def run(self, ctx: ToolContext, source_task_id, **kwargs):
        import json
        source=ctx.store.get_task(source_task_id)
        if not source or source['agent_id']!=ctx.agent.id:raise SkillError('candidate_source_bot_denied')
        return json.dumps(self.manager.candidate(ctx.store,source_task_id,**kwargs),ensure_ascii=False)
