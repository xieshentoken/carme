"""扩展能力的管理接口：已安装的 Skill 与已连接的 MCP Server。

单独成模块的原因：routes.py 已经两千行，而这两块是「管理外部装进来的能力」，
和对话、任务、模型这些主流程耦合很低，只是需要同一个 config / store / runtime。

界面上的位置：侧边栏「探索 Bot → 已安装的 Skill / 已安装的 MCP」。
鉴权和其它 /api 路由一致，由 app.py 统一挂 require_token。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import config as config_module
from ..config import Config
from ..mcp import MCPError, parse_env_text
from ..runtime import Runtime
from ..skills import Skill, SkillError, SkillManager
from ..store import Store

log = logging.getLogger("carme.extensions")

# 授予某个 Bot 分组时，允许的取值就是工具表里的分组名（skill / mcp）。
GRANTABLE_GROUPS = ("skill", "mcp")


# --------------------------------------------------------------------------- #
#  请求体
# --------------------------------------------------------------------------- #


class SkillInstall(BaseModel):
    """安装一个技能。四种来源，只有对应的字段会被读取。"""

    model_config = ConfigDict(extra="forbid")
    source: Literal["text", "path", "url", "github"] = "text"
    value: str = Field("", max_length=8000, description="path / url / owner/repo")
    text: str = Field("", max_length=200_000, description="直接粘贴的 markdown")
    name: str = Field("", max_length=120)
    description: str = Field("", max_length=400)
    subpath: str = Field("", max_length=300, description="GitHub 仓库内的子目录")


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class McpServerBody(BaseModel):
    """新增或更新一个 MCP Server。

    env / headers 用「每行一条 KEY=value」的文本传：界面上不回显历史密钥，
    留空表示沿用后端已有值，填了就以填的为准。
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field("", max_length=48)
    name: str = Field("", max_length=80)
    transport: Literal["stdio", "http"] = "stdio"
    executor: Literal['', 'action'] = 'action'
    command: str = Field("", max_length=400)
    args: list[str] | None = Field(None, max_length=32)
    args_text: str = Field("", max_length=4000)
    env_text: str = Field("", max_length=8000)
    clear_env: bool = False
    cwd: str = Field("", max_length=500)
    url: str = Field("", max_length=2048)
    headers_text: str = Field("", max_length=8000)
    enabled: bool = True
    approval: Literal["auto", "confirm"] = "auto"
    timeout: float = Field(30, ge=1, le=300)


class McpToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class GroupGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: Literal["skill", "mcp"]
    agent_ids: list[str] | None = Field(None, max_length=200)


# --------------------------------------------------------------------------- #
#  工具函数
# --------------------------------------------------------------------------- #


def _agents_file():
    return config_module.CONFIG_DIR / "agents.yaml"


def _read_agents() -> dict:
    path = _agents_file()
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HTTPException(409, f"agents.yaml 无法安全读取：{exc}") from None
    return raw if isinstance(raw, dict) else {}


def _bots_with_group(config: Config, group: str) -> list[dict]:
    """哪些 Bot 的 tools 里含这个分组（界面用来提示「装了但没人能用」）。"""
    result = []
    for spec in config.agents.agents.values():
        expanded = [str(item) for item in (spec.tools or [])]
        explicit = {"list_skills", "use_skill", "install_skill", "remove_skill"} if group == "skill" else set()
        named = any(item.startswith("mcp__") for item in expanded) if group == "mcp" else False
        if group in expanded or explicit & set(expanded) or named:
            result.append({"id": spec.id, "name": spec.name})
    return result


def _write_agents(raw: dict) -> None:
    payload = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    config_module.atomic_write(_agents_file(), payload.encode("utf-8"))


# --------------------------------------------------------------------------- #
#  路由
# --------------------------------------------------------------------------- #


def build_extensions_router(config: Config, store: Store, runtime: Runtime) -> APIRouter:
    router = APIRouter(prefix="/api")
    skills: SkillManager = runtime.skills
    mcp = runtime.mcp
    root = str(skills.root)

    @router.get('/learning')
    async def learning():
        return {'candidates':list(skills.settings().get('candidates',{}).values()),
                'skill_grants':skills.settings().get('grants',{}),'disabled_by_bot':skills.settings().get('disabled_by_bot',{}),'approved_versions':skills.settings().get('approved_versions',{}),
                'mcp_grants':mcp.grants,'memory_acl':store._query('SELECT * FROM memory_acl')}

    @router.post('/tasks/{task_id}/verify')
    async def verify(task_id: str):
        try:return await runtime.verify_outcome(task_id)
        except (ValueError,RuntimeError) as exc:raise HTTPException(409,str(exc)) from None

    @router.post('/tasks/{task_id}/accept')
    async def accept(task_id: str, body: dict):
        try:
            if set(body)!={'report_hash'}:raise ValueError('report_hash_required')
            store.accept_outcome(task_id,body['report_hash']);return store.outcome(task_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None

    @router.post('/tasks/{task_id}/resume')
    async def resume(task_id: str):
        try:return await runtime.resume(task_id)
        except (ValueError,RuntimeError):raise HTTPException(409,'任务暂无法继续，请查看任务状态。') from None

    @router.post('/tasks/{task_id}/reconcile')
    async def reconcile(task_id: str, body: dict):
        try:
            if set(body)!={'operation_id','effect','receipt'}:raise ValueError('reconciliation_fields_required')
            store.operation_reconcile(task_id,**body);return {'ok':True}
        except ValueError:raise HTTPException(409,'操作核对未完成，请检查提交信息。') from None

    @router.post('/memory-grants')
    async def memory_grant(body: dict):
        try:
            if set(body)!={'bot_id','scope','scope_id','read','write'}:raise ValueError('memory_grant_fields_required')
            config.agents.get(body['bot_id']);store.memory_grant(**body);return {'ok':True}
        except (KeyError,ValueError) as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/skills/{skill_id}/snapshot')
    async def snapshot_skill(skill_id: str):
        try:return skills.snapshot(skill_id)
        except SkillError as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/skill-grants')
    async def skill_grant(body: dict):
        try:
            if set(body)-{'bot_id','skill_id','revision','revoke'} or not {'bot_id','skill_id','revision'}<=set(body):raise ValueError('skill_grant_fields_required')
            if body['bot_id'] != '*': config.agents.get(body['bot_id'])
            skills.grant(**body);return {'ok':True}
        except (KeyError,ValueError,SkillError) as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/skill-candidates')
    async def candidate(body: dict):
        try:
            if set(body)-{'source_task_id','name','document','private_literals','files'} or not {'source_task_id','name','document','private_literals'}<=set(body):raise ValueError('candidate_fields_required')
            return skills.candidate(store,**body)
        except (ValueError,SkillError) as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/skill-candidates/{candidate_id}/test')
    async def test_candidate(candidate_id: str, body: dict):
        try:
            if set(body)!={'conversation_id','agent_id','goal','envelope'}:raise ValueError('test_contract_required')
            candidate=skills.settings().get('candidates',{}).get(candidate_id)
            if not candidate:raise ValueError('candidate_missing')
            import uuid
            result=await runtime.submit_message(body['conversation_id'],body['goal'],'skill-test-'+uuid.uuid4().hex,
                body['agent_id'],envelope=body['envelope'],skill_test={'candidate_id':candidate_id,
                    'skill_id':candidate['skill_id'],'revision':candidate['revision']})
            if result.get('steering'):raise ValueError('candidate_test_requires_idle_conversation')
            return result
        except (ValueError,SkillError,KeyError) as exc:raise HTTPException(422,str(exc)) from None

    @router.get('/skill-candidates/{candidate_id}/review')
    async def review_candidate(candidate_id: str):
        candidate=skills.settings().get('candidates',{}).get(candidate_id)
        if not candidate:raise HTTPException(404,'candidate_missing')
        try:
            manifest=skills.manifest(candidate['skill_id'],candidate['revision'])
            files={item['path']:(skills.root/'.versions'/candidate['skill_id']/candidate['revision']/item['path']).read_text()
                   for item in manifest['files']}
            return {'candidate':candidate,'manifest':manifest,'files':files}
        except (SkillError,UnicodeError) as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/skill-candidates/{candidate_id}/publish')
    async def publish(candidate_id: str, body: dict):
        try:
            if set(body)!={'test_task_id','bot_ids','revision','privacy_reviewed'}:raise ValueError('publication_fields_required')
            for bot in body['bot_ids']:
                if bot != '*': config.agents.get(bot)
            return skills.publish_candidate(store,candidate_id,**body)
        except (ValueError,SkillError,KeyError) as exc:raise HTTPException(422,str(exc)) from None

    @router.post('/mcp-grants')
    async def mcp_grant(body: dict):
        try:
            if set(body)-{'bot_id','server_id','remote','argument_allowlist','revoke'} or not {'bot_id','server_id','remote','argument_allowlist'}<=set(body):raise ValueError('mcp_grant_fields_required')
            config.agents.get(body['bot_id']);return mcp.grant(**body)
        except (ValueError,MCPError,KeyError) as exc:raise HTTPException(422,str(exc)) from None

    # ---------------- 技能 ----------------

    @router.get("/skills")
    async def list_skills() -> dict:
        catalog = await asyncio.to_thread(skills.list_skills)
        return {
            "skills": [skill.public() for skill in catalog],
            "root": root,
            "enabled": len([skill for skill in catalog if skill.enabled and not skill.error]),
            "bots": _bots_with_group(config, "skill"),
        }

    @router.post("/skills/install", status_code=201)
    async def install_skill(body: SkillInstall) -> dict:
        source = body.source
        try:
            if source == "text":
                content = (body.text or body.value).strip()
                if not content:
                    raise HTTPException(422, "请粘贴技能内容或填写 SKILL.md 地址")
                skill = await asyncio.to_thread(skills.install_from_text, content,
                                                name=body.name, description=body.description)
            elif source == "path":
                if not body.value.strip():
                    raise HTTPException(422, "请填写本机技能目录或 .md 文件路径")
                skill = await asyncio.to_thread(skills.install_from_path, body.value, name=body.name)
            elif source == "url":
                if not body.value.strip():
                    raise HTTPException(422, "请填写 SKILL.md 的 http(s) 地址")
                skill = await skills.install_from_url(body.value, name=body.name)
            else:
                if not body.value.strip():
                    raise HTTPException(422, "请填写 owner/repo 或 GitHub 仓库地址")
                skill = await skills.install_from_github(body.value, subpath=body.subpath, name=body.name)
            skills.share(skill.id, skills.snapshot(skill.id)['revision'])
            skill = skills.get(skill.id)
        except SkillError as exc:
            # 安装失败的原因（路径不存在、下载失败、没有 SKILL.md）都要原样告诉人。
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("skills.updated", {"skill": skill.id}, "", "")
        return {"ok": True, "skill": skill.public(), "message": f"已安装技能「{skill.name}」"}

    @router.post("/skills/reload")
    async def reload_skills() -> dict:
        catalog = await asyncio.to_thread(skills.list_skills)
        return {"ok": True, "skills": [skill.public() for skill in catalog],
                "message": f"已重新扫描 {len(catalog)} 个技能"}

    @router.get("/skills/{skill_id}")
    async def skill_detail(skill_id: str) -> dict:
        try:
            skill = await asyncio.to_thread(skills.get, skill_id)
            body = await asyncio.to_thread(_preview, skills, skill)
        except SkillError as exc:
            raise HTTPException(404, str(exc)) from None
        return {"skill": skill.public(), "body": body}

    @router.patch("/skills/{skill_id}")
    async def update_skill(skill_id: str, body: SkillUpdate) -> dict:
        try:
            if body.enabled:
                # Validate candidate publication before changing the global enabled state.
                skills.grant('*', skill_id, skills.snapshot(skill_id)['revision'])
            skill = skills.set_enabled(skill_id, body.enabled)
        except SkillError as exc:
            raise HTTPException(404, str(exc)) from None
        await runtime._emit("skills.updated", {"skill": skill.id}, "", "")
        return {"ok": True, "skill": skill.public(),
                "message": f"已{'启用' if body.enabled else '停用'}技能「{skill.name}」"}

    @router.delete("/skills/{skill_id}")
    async def remove_skill(skill_id: str) -> dict:
        try:
            skill = await asyncio.to_thread(skills.get, skill_id)
            await asyncio.to_thread(skills.remove, skill_id)
        except SkillError as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("skills.updated", {"skill": skill_id}, "", "")
        return {"ok": True, "removed": skill_id, "message": f"已删除技能「{skill.name}」"}

    # ---------------- MCP ----------------

    @router.get("/mcp")
    async def list_mcp() -> dict:
        return {"servers": mcp.public_servers(), "bots": _bots_with_group(config, "mcp"),
                "config": str(mcp.path)}

    @router.post("/mcp/servers", status_code=201)
    async def add_mcp_server(body: McpServerBody) -> dict:
        try:
            payload = _mcp_payload(body, mcp.server(body.id))
            server = await mcp.upsert(payload)
        except MCPError as exc:
            # 校验类错误（id 不合法、命令为空、env 行缺等号）都在这里变成 422；
            # 连接失败不算错误：定义已经存下，界面上按 error 状态显示并允许重连。
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("mcp.updated", {"server": server["id"]}, "", "")
        return {"ok": True, "server": server, "message": _mcp_message(server)}

    @router.put("/mcp/servers/{server_id}")
    async def update_mcp_server(server_id: str, body: McpServerBody) -> dict:
        if mcp.server(server_id) is None:
            raise HTTPException(404, "MCP Server 不存在")
        try:
            payload = _mcp_payload(body, mcp.server(server_id))
            payload["id"] = server_id
            server = await mcp.upsert(payload, server_id=server_id)
        except MCPError as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("mcp.updated", {"server": server_id}, "", "")
        return {"ok": True, "server": server, "message": _mcp_message(server)}

    @router.delete("/mcp/servers/{server_id}")
    async def remove_mcp_server(server_id: str) -> dict:
        if mcp.server(server_id) is None:
            raise HTTPException(404, "MCP Server 不存在")
        await mcp.remove(server_id)
        await runtime._emit("mcp.updated", {"server": server_id}, "", "")
        return {"ok": True, "removed": server_id, "message": "已删除 MCP Server 并断开连接"}

    @router.post("/mcp/servers/{server_id}/connect")
    async def connect_mcp_server(server_id: str) -> dict:
        try:
            server = await mcp.connect(server_id)
        except MCPError as exc:
            raise HTTPException(502, str(exc)) from None
        await runtime._emit("mcp.updated", {"server": server_id}, "", "")
        return {"ok": True, "server": server, "message": _mcp_message(server, action="连接")}

    @router.post("/mcp/servers/{server_id}/disconnect")
    async def disconnect_mcp_server(server_id: str) -> dict:
        if mcp.server(server_id) is None:
            raise HTTPException(404, "MCP Server 不存在")
        await mcp.disconnect(server_id)
        await runtime._emit("mcp.updated", {"server": server_id}, "", "")
        return {"ok": True, "server": mcp.public_server(server_id), "message": "已断开连接"}

    @router.patch("/mcp/servers/{server_id}")
    async def toggle_mcp_server(server_id: str, body: McpToggle) -> dict:
        try:
            server = await mcp.set_enabled(server_id, body.enabled)
        except MCPError as exc:
            raise HTTPException(422, str(exc)) from None
        await runtime._emit("mcp.updated", {"server": server_id}, "", "")
        return {"ok": True, "server": server,
                "message": f"已{'启用并连接' if body.enabled else '停用'}「{server['label']}」"}

    # ---------------- 工具分组授权 ----------------

    @router.post("/tool-groups/grant")
    async def grant_group(body: GroupGrant) -> dict:
        """把 skill / mcp 分组加进 Bot 的 tools 列表。

        装好能力和「谁可以用」是两件事：这里由人工显式点一下，
        比自己动手改 agents.yaml 友好，也仍然是一次明确授权。
        """
        raw = _read_agents()
        roster = raw.get("agents")
        if not isinstance(roster, dict):
            raise HTTPException(409, "agents.yaml 里没有 agents 段，无法授权")
        # agent_ids 省略 = 全部；显式给空列表 = 一个都不改（而不是意外全开）。
        wanted = set(roster) if body.agent_ids is None else set(body.agent_ids)
        updated: list[str] = []
        for agent_id, settings in roster.items():
            if agent_id not in wanted or not isinstance(settings, dict):
                continue
            tools = [str(item) for item in (settings.get("tools") or [])]
            if body.group in tools:
                continue
            tools.append(body.group)
            settings["tools"] = tools
            updated.append(agent_id)
        unknown = sorted(wanted - set(roster))
        if updated:
            try:
                await asyncio.to_thread(_write_agents, raw)
            except (OSError, ValueError) as exc:
                raise HTTPException(409, f"无法写入 agents.yaml：{exc}") from None
            fresh = await asyncio.to_thread(config_module.load, True)
            config.agents = fresh.agents
            runtime.config.agents = config.agents
            await runtime._emit("agent.updated", {"group": body.group, "agents": updated}, "", "")
        message = (f"已为 {len(updated)} 个 Bot 打开 {body.group} 分组" if updated
                   else f"所有 Bot 都已经能使用 {body.group} 分组")
        if unknown:
            message += f"；忽略了不存在的 Bot：{', '.join(unknown)}"
        return {"ok": True, "updated": updated, "unknown": unknown, "message": message}

    return router


def _preview(skills: SkillManager, skill: Skill) -> str:
    """Administrator review must include the complete bounded document."""
    try:
        return skills.body(skill)
    except SkillError as exc:
        return f"（无法读取：{exc}）"


def _mcp_payload(body: McpServerBody, existing) -> dict:
    """把界面请求体翻译成 MCPManager 能消化的字典。"""
    payload: dict = {
        "id": body.id,
        "name": body.name,
        "transport": body.transport, "executor":body.executor if body.transport=="stdio" else "",
        "enabled": body.enabled,
        "approval": body.approval,
        "timeout": body.timeout,
    }
    if body.transport == "stdio":
        payload["command"] = body.command
        if body.args is not None:
            payload["args"] = body.args
        elif body.args_text.strip():
            payload["args"] = [line for line in body.args_text.splitlines() if line.strip()]
        elif existing is not None:
            payload["args"] = existing.args
        else:
            payload["args"] = []
        payload["cwd"] = body.cwd
        payload["env"] = _pairs(body.env_text, existing.env if existing else {}, body.clear_env)
    else:
        payload["url"] = body.url
        payload["headers"] = _pairs(body.headers_text, existing.headers if existing else {}, body.clear_env)
    # 界面传来的就是最终值（留空＝沿用后端已有），所以不让管理器再做合并。
    payload["merge_env"] = False
    return payload


def _pairs(text: str, existing: dict, clear: bool) -> dict:
    """env / headers 的取值规则：清空 → {}；填了 → 以填的为准；留空 → 沿用。"""
    if clear:
        return parse_env_text(text)
    if text.strip():
        return parse_env_text(text)
    return dict(existing)


def _mcp_message(server: dict, *, action: str = "保存") -> str:
    if server.get("error"):
        return f"{action}成功，但连接失败：{server['error']}"
    if server.get("status") == "connected":
        return f"{action}成功，已连接 {server.get('tool_count', 0)} 个工具"
    return f"{action}成功（已停用，不会连接）"
