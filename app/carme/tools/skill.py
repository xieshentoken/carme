"""技能类工具 —— 让模型自己决定要不要读某个技能。

    list_skills  看清单（只花几十个 token）
    use_skill    载入某个技能的完整说明（渐进式披露的第二跳）

系统提示里已经列了「名字 + 一句话用途」，所以多数情况下模型会直接 use_skill；
list_skills 主要是给「描述不够、想再看看全貌」的场景兜底。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..skills import SkillError, SkillManager
from .base import Tool, ToolContext

MAX_LISTED = 40


class ListSkillsTool(Tool):
    name = "list_skills"
    description = (
        "列出当前 Bot 已获授权并启用的技能（Skill）。技能是一份写给模型看的操作说明，"
        "先用本工具确认有哪些可用，再用 use_skill 载入完整步骤。"
    )
    parameters: dict = {"type": "object", "properties": {"offset":{"type":"integer","minimum":0}}, "additionalProperties": False}

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def run(self, ctx: ToolContext, **kwargs: Any) -> str:
        import json
        grants=self.manager.effective_grants(ctx.agent.id)
        offset=max(0,int(kwargs.get('offset',0)))
        items=[{'id':sid,'name':self.manager.manifest(sid,revision)['name'],'revision':revision} for sid,revision in sorted(grants.items())
               if sid not in self.manager.settings()['disabled']]
        return json.dumps({'skills':items[offset:offset+20],
            'next_offset':offset+20 if offset+20<len(items) else None},ensure_ascii=False)


class UseSkillTool(Tool):
    name = "use_skill"
    description = (
        "分页读取已授权固定版本的说明。检查 next_cursor，直到 complete=true。"
        "@manifest 列出全部文件和章节；directory 用于任务 shell/read_file，computer_directory 用于 bot_computer 的 Linux 桌面。"
        "输入目录只读，需要修改时复制到工作目录。始终遵守返回的 constraints。"
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
            runtime = getattr(execution, 'runtime', None)
            if runtime and ctx.extras.get('policy', {}).get('skill_grants', {}).get(name) != page['revision']:
                runtime.commit_skill_change(ctx, lambda: None)
                task = ctx.store.get_task(ctx.task_id)
            meta=json.loads(task['meta'])
            if meta.get('execution_target')=='container':
                if execution is None:raise SkillError('skill_container_not_available')
                if ctx.extras.get('staged_skills',{}).get(name)!=page['revision']:
                    manifest=self.manager.manifest(name,page['revision'])
                    for entry in manifest['files']:
                        raw=(self.manager.root/'.versions'/name/page['revision']/entry['path']).read_bytes()
                        await execution.stage_input(ctx.task_id,'skills/'+name+'/'+page['revision']+'/'+entry['path'],raw)
                    ctx.extras.setdefault('staged_skills',{})[name]=page['revision']
                if ctx.extras.get('policy', {}).get('computer_target') == 'linux':
                    page['computer_directory'] = '/task-files/' + ctx.task_id + page['directory']
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


def _skill_runtime(ctx):
    runtime = getattr(getattr(ctx.browser_manager, 'execution', None), 'runtime', None)
    if runtime is None:
        raise SkillError('skill_management_runtime_required')
    runtime.check_task_policy(ctx.task_id)
    return runtime


class InstallSkillTool(Tool):
    name = 'install_skill'
    description = ('用户要求安装 Skill 时使用：source 为 github（owner/repo 或仓库链接，可指定 subpath/ref）、'
        'url（公网 SKILL.md 或 ZIP 链接）、attachment（本会话已发送 MD/ZIP 的 file_id）、'
        'installed（本账号已安装 Skill 的 ID/名称）。安装一次即供本账号全部现有和新建 Bot 使用；相同完整内容复用同一份。自己的 Linux 直接安装，其他执行目标保留聊天审批。'
        '不会执行安装脚本、安装软件或授予新工具权限；成功后可立即 use_skill。不能传宿主路径。')
    parameters = {'type': 'object', 'properties': {
        'source': {'type': 'string', 'enum': ['github', 'url', 'attachment', 'installed']},
        'value': {'type': 'string', 'maxLength': 8000},
        'subpath': {'type': 'string', 'maxLength': 300},
        'ref': {'type': 'string', 'maxLength': 120}},
        'required': ['source', 'value'], 'additionalProperties': False}

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def run(self, ctx, source, value, subpath='', ref=''):
        runtime = _skill_runtime(ctx)
        if (source not in {'github', 'url', 'attachment', 'installed'} or not isinstance(value, str)
                or not 0 < len(value) <= 8000 or not isinstance(subpath, str) or len(subpath) > 300
                or not isinstance(ref, str) or len(ref) > 120):
            raise SkillError('invalid_skill_source')
        with tempfile.TemporaryDirectory(prefix='carme-skill-') as temporary:
            staging = SkillManager(Path(temporary) / 'skills', Path(temporary) / 'settings.yaml',
                download_check=lambda: runtime.check_task_policy(ctx.task_id),
                download_safety=runtime.config.browser.safety)
            owner = staging
            if source == 'installed':
                owner = self.manager
                skill = owner.get(value)
                if not skill.enabled or skill.error:
                    raise SkillError('skill_disabled_or_invalid')
            elif source == 'attachment':
                from ..attachments import file_path
                file = ctx.store.artifact_access(ctx.task_id, value)
                if not file['message_id']:
                    raise SkillError('artifact_not_sent')
                raw = file_path(ctx.store, value).read_bytes()
                if file.get('sha256') and hashlib.sha256(raw).hexdigest() != file['sha256']:
                    raise SkillError('artifact_version_conflict')
                suffix = Path(file['name']).suffix.lower()
                if suffix == '.zip':
                    skill = await asyncio.to_thread(staging.install_from_zip, raw, subpath=subpath, source='聊天附件：' + file['name'])
                elif suffix in {'.md', '.markdown', '.txt'}:
                    skill = staging.install_from_text(raw.decode('utf-8-sig'), source='聊天附件：' + file['name'])
                else:
                    raise SkillError('Skill 附件需为 MD 或包含 SKILL.md 的 ZIP')
            elif source == 'github' or (source == 'url' and urlsplit(value).hostname in {'github.com', 'www.github.com'}):
                parts = urlsplit(value).path.strip('/').split('/')
                if len(parts) > 3 and parts[2] in {'tree', 'blob'}:
                    ref = ref or parts[3]
                    subpath = subpath or '/'.join(parts[4:]).removesuffix('/SKILL.md')
                    if subpath == 'SKILL.md': subpath = ''
                skill = await staging.install_from_github(value, subpath=subpath, ref=ref)
            elif urlsplit(value).path.lower().endswith('.zip'):
                from ..skills import MAX_ARCHIVE_BYTES
                raw = await staging._download_bytes(value, MAX_ARCHIVE_BYTES)
                skill = await asyncio.to_thread(staging.install_from_zip, raw, subpath=subpath, source='网址：' + value)
            else:
                skill = await staging.install_from_url(value)
            manifest = owner.snapshot(skill.id)
            revision = manifest['revision']
            if (source == 'installed' and self.manager.settings().get('grants', {}).get('*', {}).get(skill.id) == revision
                    and self.manager.effective_grants(ctx.agent.id).get(skill.id) == revision):
                return json.dumps({'status': 'already_installed', 'skill_id': skill.id, 'revision': revision}, ensure_ascii=False)
            if not ctx.local_autonomy:
                decision = await ctx.request_approval(kind='skill_install', summary=f'为本账号共享安装 Skill「{skill.name}」', detail={
                    'name': skill.name, 'description': skill.description, 'source': skill.source,
                    'revision': revision, 'file_count': len(manifest['files']), 'size': skill.size,
                    'files': [f['path'] for f in manifest['files']][:30],
                    'scope': '当前账号全部 Bot（含新建 Bot）；不执行脚本，不增加工具或系统权限',
                    'preview': (skill.path / 'SKILL.md').read_text(encoding='utf-8')[:2000]})
                if not decision.approved:
                    return '未安装 Skill：' + decision.note
            def commit():
                # Bind approval to the prepared bytes, not to another download of the URL.
                if owner.snapshot(skill.id)['revision'] != revision:
                    raise SkillError('skill_changed_after_approval')
                existing_ids = set(self.manager.settings().get('installed', {}))
                ctx.extras['skill_change_started'] = True
                installed = skill if source == 'installed' else self.manager._install_bundle(
                    (skill.path / 'SKILL.md').read_text(encoding='utf-8'),
                    [(entry['path'], (skill.path / entry['path']).read_bytes())
                     for entry in manifest['files'] if entry['path'] != 'SKILL.md'], source=skill.source)
                try:
                    if self.manager.snapshot(installed.id)['revision'] != revision:
                        raise SkillError('skill_revision_mismatch')
                    self.manager.share(installed.id, revision)
                    if installed.id in self.manager.settings().get('disabled_by_bot', {}).get(ctx.agent.id, []):
                        self.manager.grant(ctx.agent.id, installed.id, revision)
                except BaseException:
                    if source != 'installed' and installed.id not in existing_ids: self.manager.remove(installed.id)
                    raise
                return installed
            installed = runtime.commit_skill_change(ctx, commit)
            await ctx.notify('skills.updated', {'action': 'install', 'skill_id': installed.id})
            return json.dumps({'status': 'installed', 'skill_id': installed.id, 'revision': revision,
                'scope': 'account', 'next': '本账号全部 Bot 可立即用 use_skill 读取，无需再次安装；包内脚本尚未执行。'}, ensure_ascii=False)


class RemoveSkillTool(Tool):
    name = 'remove_skill'
    description = ('默认 scope=bot，只停用当前 Bot 的使用，共享安装包和其他 Bot 不受影响。'
        '用户明确要求全账号卸载时用 scope=account，必须通过工具审批；历史版本与任务记录保留。')
    parameters = {'type': 'object', 'properties': {
        'name': {'type': 'string', 'maxLength': 120},
        'scope': {'type': 'string', 'enum': ['bot', 'account'], 'default': 'bot'}},
        'required': ['name'], 'additionalProperties': False}

    def __init__(self, manager: SkillManager) -> None:
        self.manager = manager

    async def run(self, ctx, name, scope='bot'):
        runtime = _skill_runtime(ctx)
        if scope not in {'bot', 'account'}:
            raise SkillError('invalid_skill_scope')
        skill = self.manager.get(name)
        revision = self.manager.effective_grants(ctx.agent.id).get(skill.id)
        if not revision:
            raise SkillError('skill_version_not_granted')
        if scope == 'account' or not ctx.local_autonomy:
            decision = await ctx.request_approval(kind='skill_remove',
                summary=f'{"全账号卸载" if scope == "account" else "停用当前 Bot 的"} Skill「{skill.name}」', detail={
                    'skill_id': skill.id, 'revision': revision, 'scope': scope,
                    'impact': '全部 Bot 将无法使用该 Skill' if scope == 'account' else '只停用当前 Bot；共享包保留',
                    'historical_versions_retained': True})
            if not decision.approved:
                return '未删除 Skill：' + decision.note
        def commit():
            if self.manager.effective_grants(ctx.agent.id).get(skill.id) != revision:
                raise SkillError('skill_changed_after_approval')
            ctx.extras['skill_change_started'] = True
            if scope == 'account':
                self.manager.remove(skill.id)
            else:
                self.manager.grant(ctx.agent.id, skill.id, revision, revoke=True)
        runtime.commit_skill_change(ctx, commit)
        ctx.extras.get('staged_skills', {}).pop(skill.id, None)
        await ctx.notify('skills.updated', {'action': 'remove', 'skill_id': skill.id})
        return json.dumps({'status': 'removed', 'skill_id': skill.id, 'scope': scope,
            'package_uninstalled': scope == 'account', 'historical_versions_retained': True}, ensure_ascii=False)
