"""Skill（技能包）的安装、管理与读取。

一个 Skill 就是一个目录，里面有 SKILL.md：

    SKILL.md            —— YAML frontmatter（name / description）+ 正文说明
    scripts/…           —— 可选：技能自带的脚本、模板、参考资料

SKILL.md 的正文不会自动进入上下文。Agent 的系统提示里只放「名字 + 一句话用途」，
真正要用的时候由模型调用 use_skill 把完整说明读进来（渐进式披露），
这样装几十个技能也不会把每一步的 prompt 撑爆。

安装来源四选一：
    path    本机已有目录或 .md 文件（复制进来）
    url     http(s) 上的 markdown（单文件技能）
    github  owner/repo（可选 subpath 指定仓库里的子目录）
    text    直接在界面上粘贴 markdown

技能内容落在 data/skills/<id>/，安装元数据与启停状态落在 config/skills.yaml。
目录才是内容的事实来源：手工删掉一个目录，列表里就少一个技能。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import time
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urljoin

import httpx
import yaml

SKILL_FILENAME = "SKILL.md"
SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# 单个 SKILL.md 的正文上限：技能是说明，不是数据集。
MAX_BODY_BYTES = 512 * 1024
# 一个技能目录的总量与文件数上限，防止把仓库整个拖进来当技能。
MAX_SKILL_BYTES = 32 * 1024 * 1024
MAX_SKILL_FILES = 400
# 下载上限：GitHub tarball 与单文件 markdown 各自封顶。
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
# GitHub 压缩包的成员数上限：压缩比可以很高，所以按成员数再拦一道。
MAX_ARCHIVE_MEMBERS = 5000
# 列给模型看的文件清单条数；目录再大也不铺满上下文。
MAX_LISTED_FILES = 200
# 技能清单缓存秒数：Agent 每一步都会问一次，但目录不可能每秒都变。
PROMPT_CACHE_SECONDS = 5.0
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".idea", ".vscode"}
_SKIP_FILES = {".DS_Store"}
_USER_AGENT = "Carme/0.2 (+skill-installer)"


class SkillError(RuntimeError):
    """技能安装或读取失败；消息可以直接显示给人工用户。"""


@dataclass
class Skill:
    """一个已安装技能的元数据（正文按需再读）。"""

    id: str
    name: str
    description: str
    version: str = ""
    path: Path = Path()
    source: str = ""
    installed_at: float = 0.0
    enabled: bool = True
    files: list[str] = field(default_factory=list)
    size: int = 0
    error: str = ""
    shared: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "installed_at": self.installed_at,
            "enabled": self.enabled,
            "files": self.files,
            "file_count": len(self.files),
            "size": self.size,
            "error": self.error,
            "shared": self.shared,
        }

    def prompt_line(self) -> str:
        """系统提示里的一行：只给名字和用途。"""
        return f"- {self.id}（{self.name}）：{self.description}" if self.description else f"- {self.id}（{self.name}）"


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return text[:64]


def _skill_id(name: str, fallback: str = "") -> str:
    """中文名没有 ASCII 可留，就用哈希兜底，保证 id 永远是安全的目录名。"""
    candidate = _slug(name)
    if candidate and SKILL_ID_PATTERN.match(candidate):
        return candidate
    digest = hashlib.sha256((name or fallback or "skill").encode("utf-8")).hexdigest()[:10]
    return f"skill-{digest}"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 YAML frontmatter；没有就返回空字典和原文。"""
    cleaned = text.lstrip("\ufeff")
    if not cleaned.lstrip().startswith("---"):
        return {}, text
    lines = cleaned.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "---"), None)
    if start is None:
        return {}, text
    end = next((index for index in range(start + 1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return {}, text
    try:
        meta = yaml.safe_load("\n".join(lines[start + 1:end])) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


def _describe(body: str) -> str:
    """没有 description 时，从正文里摘第一段有信息量的文字。"""
    for raw in body.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line and not line.startswith("```"):
            return line[:160]
    return ""


def _check_document_size(document: str) -> None:
    """安装前就检查正文体积：装进来的技能必须马上可用。"""
    if len(document.encode("utf-8")) > MAX_BODY_BYTES:
        raise SkillError(f"技能正文超过 {MAX_BODY_BYTES // 1024} KB，请拆分后再安装")


def _safe_time(value: Any) -> float:
    """时间戳容错：手写的 skills.yaml 里写错也只当没有。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and number < 10 ** 12 else 0.0


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{path.name} 不是 UTF-8 文本，无法作为技能说明") from exc
    except OSError as exc:
        raise SkillError(f"无法读取 {path.name}：{exc}") from exc


def _safe_relative(value: str) -> str:
    """校验相对路径：不允许绝对路径、.. 或控制字符。"""
    candidate = (value or "").strip().replace("\\", "/").strip("/")
    if not candidate:
        return ""
    if any(ord(char) < 32 for char in candidate):
        raise SkillError("路径不能包含控制字符")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise SkillError("路径不能包含 ..")
    return "/".join(parts)


def _copy_tree(source: Path, target: Path) -> tuple[list[str], int]:
    """把技能目录复制到安装位置；只复制普通文件，跳过缓存与版本库。"""
    files: list[str] = []
    total = 0
    for current, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(name for name in dirnames
                             if name not in _SKIP_DIRS and not (Path(current) / name).is_symlink())
        for filename in sorted(filenames):
            origin = Path(current) / filename
            if filename in _SKIP_FILES or origin.is_symlink() or not origin.is_file():
                continue
            relative = origin.relative_to(source).as_posix()
            if not _safe_relative(relative):
                continue
            size = origin.stat().st_size
            if len(files) >= MAX_SKILL_FILES:
                raise SkillError(f"技能文件数超过 {MAX_SKILL_FILES} 个，请只保留必要文件")
            if total + size > MAX_SKILL_BYTES:
                raise SkillError("技能体积超过 32 MB，请精简后重试")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, destination)
            files.append(relative)
            total += size
    return files, total


def _list_files(root: Path, *, complete=False) -> tuple[list[str], int]:
    """列出目录里的文件；清单条数封顶，体积照实统计。"""
    files: list[str] = []
    total = 0
    scanned = 0
    if complete and (not root.is_dir() or root.resolve()!=root):raise SkillError('skill_snapshot_path_denied')
    for current, dirnames, filenames in os.walk(root):
        if complete and any((Path(current)/name).is_symlink() for name in [*dirnames,*filenames]):raise SkillError('skill_snapshot_symlink_denied')
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        for filename in sorted(filenames):
            if filename in _SKIP_FILES:
                continue
            path = Path(current) / filename
            if not path.is_file() or path.is_symlink():
                continue
            scanned += 1
            total += path.stat().st_size
            if complete and (scanned>MAX_SKILL_FILES or total>MAX_SKILL_BYTES):raise SkillError('skill_snapshot_limit')
            if complete or len(files) < MAX_LISTED_FILES:
                files.append(path.relative_to(root).as_posix())
            if scanned > MAX_SKILL_FILES * 4:
                return files, total
    return files, total


def _bundle_entries(path: Path) -> list[dict]:
    files, _ = _list_files(path, complete=True)
    entries = []
    for name in sorted(files):
        raw = (path / name).read_bytes()
        entries.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)})
    return entries


class SkillManager:
    """扫描、安装、启停技能。所有方法都可从事件循环里直接调用。"""

    def __init__(self, root: Path, settings_path: Path, *, download_check=None, download_safety=None) -> None:
        self._lock = threading.RLock()
        self.download_check = download_check or (lambda: None)
        self.download_safety = download_safety or {}
        self.root = Path(root).resolve()
        self.settings_path = Path(settings_path)
        self._prompt_cache: tuple[tuple, float, str] | None = None
    # ---------------- 配置 ----------------

    def settings(self) -> dict[str, Any]:
        if not self.settings_path.is_file():
            return {"version": 1, "roots": [], "disabled": [], "installed": {}}
        try:
            raw = yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            return {"version": 1, "roots": [], "disabled": [], "installed": {}}
        if not isinstance(raw, dict):
            return {"version": 1, "roots": [], "disabled": [], "installed": {}}
        raw.setdefault("version", 1)
        raw.setdefault("roots", [])
        raw.setdefault("disabled", [])
        raw.setdefault("installed", {})
        if not isinstance(raw["roots"], list):
            raw["roots"] = []
        if not isinstance(raw["disabled"], list):
            raw["disabled"] = []
        if not isinstance(raw["installed"], dict):
            raw["installed"] = {}
        return raw

    def _save(self, settings: dict[str, Any]) -> None:
        from .config import atomic_write

        payload = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False)
        try:
            atomic_write(self.settings_path, payload.encode("utf-8"))
            self.settings_path.chmod(0o600)
        except (OSError, ValueError) as exc:
            # 符号链接或只读目录：变成界面能读的报错，而不是 500。
            raise SkillError(f"无法写入 {self.settings_path.name}：{exc}") from exc

    def _roots(self) -> list[Path]:
        roots = [self.root]
        for extra in self.settings().get("roots") or []:
            candidate = Path(str(extra)).expanduser()
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
        return roots

    # ---------------- 读取 ----------------

    def list_skills(self) -> list[Skill]:
        """扫描所有技能根目录。坏技能也列出来，但要带上 error 说明。"""
        with self._lock:
            settings = self.settings()
            disabled = {str(item) for item in settings.get("disabled") or []}
            metadata = settings.get("installed") or {}
            found: dict[str, Skill] = {}
            for root in self._roots():
                if not root.is_dir():
                    continue
                for entry in sorted(root.iterdir()):
                    if not entry.is_dir() or entry.is_symlink():
                        continue
                    skill_id = entry.name
                    if not SKILL_ID_PATTERN.match(skill_id) or skill_id in found or skill_id in settings.get("aliases", {}):
                        continue
                    try:
                        skill = self._load(skill_id, entry)
                    except SkillError as exc:
                        skill = Skill(id=skill_id, name=skill_id, description="", path=entry, error=str(exc))
                    except OSError as exc:  # 扫描途中目录被删或权限变化：当成坏技能列出来
                        skill = Skill(id=skill_id, name=skill_id, description="", path=entry,
                                      error=f"无法读取技能目录：{exc}")
                    record = metadata.get(skill_id) if isinstance(metadata.get(skill_id), dict) else {}
                    skill.source = str(record.get("source") or skill.source or "本地目录")
                    skill.installed_at = _safe_time(record.get("installed_at"))
                    skill.enabled = skill_id not in disabled
                    skill.shared = skill_id in settings.get("grants", {}).get("*", {})
                    found[skill_id] = skill
            return sorted(found.values(), key=lambda item: item.id)

    def _load(self, skill_id: str, directory: Path) -> Skill:
        document = directory / SKILL_FILENAME
        if not document.is_file():
            raise SkillError(f"缺少 {SKILL_FILENAME}")
        if document.stat().st_size > MAX_BODY_BYTES:
            raise SkillError("SKILL.md 超过 512 KB")
        text = _read_text(document)
        meta, body = _split_frontmatter(text)
        name = str(meta.get("name") or "").strip()
        if not name:
            heading = next((line.lstrip("#").strip() for line in body.splitlines() if line.strip().startswith("#")), "")
            name = heading or skill_id
        description = str(meta.get("description") or "").strip() or _describe(body)
        files, size = _list_files(directory)
        return Skill(
            id=skill_id,
            name=name[:120],
            description=description[:400],
            version=str(meta.get("version") or "").strip()[:40],
            path=directory,
            files=files,
            size=size,
        )

    def get(self, key: str) -> Skill:
        """按 id 或名字取技能；名字不唯一时要求用 id。"""
        wanted = (key or "").strip().lower()
        if not wanted:
            raise SkillError("请提供技能 id 或名称")
        wanted = self.settings().get("aliases", {}).get(wanted, wanted)
        skills = self.list_skills()
        for skill in skills:
            if skill.id == wanted:
                return skill
        matches = [skill for skill in skills if skill.name.lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SkillError(f"有多个技能都叫「{key}」，请改用 id：{', '.join(item.id for item in matches)}")
        raise SkillError(f"没有已安装的技能「{key}」。可用：{', '.join(item.id for item in skills) or '（无）'}")

    def body(self, skill: Skill) -> str:
        """返回 SKILL.md 正文（去掉 frontmatter），供 use_skill 注入上下文。"""
        document = skill.path / SKILL_FILENAME
        if not document.is_file():
            raise SkillError(f"技能 {skill.id} 的 {SKILL_FILENAME} 已丢失")
        text = _read_text(document)
        _, body = _split_frontmatter(text)
        return (body or text).strip()

    def enabled_skills(self) -> list[Skill]:
        return [skill for skill in self.list_skills() if skill.enabled and not skill.error]

    def _roots_signature(self) -> tuple:
        """技能目录的轻量指纹：目录 mtime + 配置 mtime，够判断「有没有变化」。"""
        signature: list = []
        for root in self._roots():
            try:
                signature.append((str(root), root.stat().st_mtime_ns))
            except OSError:
                signature.append((str(root), 0))
        try:
            signature.append(self.settings_path.stat().st_mtime_ns)
        except OSError:
            signature.append(0)
        return tuple(signature)

    def prompt_block(self, bot_id: str = '') -> str:
        """系统提示里的技能清单；没有技能时返回空串。

        每个 Agent 每一步都会问一次这里，而扫描目录要走一遍文件系统，
        所以按「目录 + 配置的 mtime」缓存几秒 —— 装、删、启停都会改 mtime，
        人工改动也能在几秒内反映出来。
        """
        if bot_id:
            grants=self.effective_grants(bot_id)
            lines=[f'- {self.manifest(sid,revision)["name"]}（{sid}），固定版本 {revision}；用 use_skill 分页读取 manifest 和完整说明。'
                   for sid,revision in grants.items() if sid not in self.settings()['disabled']]
            return '## 你可用的技能（Skill）\n'+'\n'.join(lines) if lines else ''
        signature = self._roots_signature()
        cached = self._prompt_cache
        now = time.monotonic()
        if cached is not None and cached[0] == signature and now - cached[1] < PROMPT_CACHE_SECONDS:
            return cached[2]
        block = self._build_prompt_block()
        self._prompt_cache = (signature, now, block)
        return block

    def _build_prompt_block(self) -> str:
        skills = self.enabled_skills()
        if not skills:
            return ""
        lines = [
            "## 你可用的技能（Skill）",
            "下面是已安装技能的名字和用途。需要用到某个技能时，先调用 use_skill 读它的完整说明，再按说明执行；不要凭技能名猜测步骤。",
            *[skill.prompt_line() for skill in skills],
        ]
        return "\n".join(lines)


    # ---------------- 写入 ----------------

    def snapshot(self, skill_id: str) -> dict:
        with self._lock:
            skill=self.get(skill_id)
            if skill.error:raise SkillError(skill.error)
            entries = _bundle_entries(skill.path)
            from .security import digest
            revision=digest(entries);destination=self.root/'.versions'/skill.id/revision
            if not destination.exists():
                destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                temporary=destination.with_name(revision+'.pending')
                if temporary.exists():shutil.rmtree(temporary)
                _copy_tree(skill.path,temporary)
                temporary.rename(destination)
            return self.manifest(skill.id,revision)

    def manifest(self, skill_id: str, revision: str) -> dict:
        from .security import digest
        if not SKILL_ID_PATTERN.fullmatch(skill_id) or not re.fullmatch(r'[a-f0-9]{64}',revision):raise SkillError('invalid_skill_revision')
        root=self.root/'.versions'/skill_id/revision
        files,_=_list_files(root,complete=True)
        entries=[{'path':p,'sha256':hashlib.sha256((root/p).read_bytes()).hexdigest(),'size':(root/p).stat().st_size} for p in sorted(files)]
        if not entries or digest(entries)!=revision:raise SkillError('skill_revision_hash_conflict')
        meta,body=_split_frontmatter(_read_text(root/SKILL_FILENAME))
        constraints=meta.get('constraints',[])
        if not isinstance(constraints,list) or any(not isinstance(c,str) for c in constraints) or len(json.dumps(constraints,ensure_ascii=False))>3000:
            raise SkillError('skill_constraints_limit')
        return {'id':skill_id,'name':str(meta.get('name',skill_id)),'revision':revision,'files':entries,'constraints':constraints,
                'chapters':[{'title':m.group(0)[:120],'offset':m.start()} for m in re.finditer(r'^#{1,6} .+$',body,re.M)],
                'body_chars':len(body),'directory':'/inputs/skills/'+skill_id+'/'+revision}

    def grant(self, bot_id: str, skill_id: str, revision: str, *, revoke=False):
        with self._lock:
            self.manifest(skill_id,revision)
            settings=self.settings();approved=settings.setdefault('approved_versions',{}).setdefault(skill_id,[])
            # Explicit admin grant approves an installed version; learned candidates use publish_candidate.
            candidates=settings.get('candidates',{})
            if any(c['skill_id']==skill_id and c['revision']==revision and c['status']!='published' for c in candidates.values()):
                raise SkillError('candidate_requires_test_and_publication')
            if revision not in approved:approved.append(revision)
            grants=settings.setdefault('grants',{}).setdefault(bot_id,{})
            previous=grants.get(skill_id)
            if revoke:grants.pop(skill_id,None)
            else:grants[skill_id]=revision
            if bot_id != '*':
                disabled = set(settings.setdefault('disabled_by_bot', {}).get(bot_id, []))
                if revoke: disabled.add(skill_id)
                else: disabled.discard(skill_id)
                settings['disabled_by_bot'][bot_id] = sorted(disabled)
            settings.setdefault('grant_history',[]).append({'bot_id':bot_id,'skill_id':skill_id,'previous':previous,
                'revision':None if revoke else revision,'time':time.time()})
            self._save(settings)

    def effective_grants(self, bot_id: str, settings=None) -> dict[str, str]:
        settings = self.settings() if settings is None else settings
        grants = {**settings.get('grants', {}).get('*', {}),
                  **settings.get('grants', {}).get(bot_id, {})}
        disabled = set(settings['disabled']) | set(settings.get('disabled_by_bot', {}).get(bot_id, []))
        return {sid: revision for sid, revision in grants.items() if sid not in disabled}

    def share(self, skill_id: str, revision: str) -> None:
        """A user installation is available to existing and future bots of this account."""
        with self._lock:
            skill = self.get(skill_id)
            if not skill.enabled or skill.error:
                raise SkillError('skill_disabled_or_invalid')
            self.grant('*', skill.id, revision)

    def migrate_shared(self) -> dict[str, str]:
        """Run while the account is stopped; merge only identical, already approved bundles."""
        with self._lock:
            settings = self.settings()
            if settings.get('account_sharing_version') == 1:
                return {}
            candidate_ids = {c['skill_id'] for c in settings.get('candidates', {}).values()}
            seen, aliases = {}, {}
            for skill in sorted(self.list_skills(), key=lambda s: (s.installed_at, s.id)):
                if skill.error or not skill.enabled or skill.id in candidate_ids:
                    continue
                authorized = {g[skill.id] for g in settings.get('grants', {}).values() if skill.id in g}
                if not authorized:
                    continue
                revision = self.snapshot(skill.id)['revision']
                if revision not in authorized:
                    continue  # Never approve an edited, unreviewed working copy during migration.
                canonical = seen.setdefault(revision, skill.id)
                settings.setdefault('grants', {}).setdefault('*', {})[canonical] = revision
                approved = settings.setdefault('approved_versions', {}).setdefault(canonical, [])
                if revision not in approved: approved.append(revision)
                for bot, grants in settings['grants'].items():
                    if bot != '*' and grants.get(skill.id) == revision:
                        grants.pop(skill.id)
                if canonical != skill.id and authorized == {revision} and skill.path.parent == self.root:
                    aliases[skill.id] = canonical
            settings.setdefault('aliases', {}).update(aliases)
            for alias in aliases:
                settings['installed'].pop(alias, None)
                settings['grants']['*'].pop(alias, None)
            settings['account_sharing_version'] = 1
            self._save(settings)
            # Old immutable .versions and task receipts stay intact. Aliases keep old chat IDs usable.
            for alias in aliases:
                shutil.rmtree(self.root / alias)
            return aliases

    def authorized_revision(self, bot_id, skill_id, *, task=None):
        settings=self.settings()
        if skill_id in settings['disabled']:raise SkillError('技能已停用：skill_disabled')
        revision=self.effective_grants(bot_id, settings).get(skill_id)
        test=(json.loads(task['meta']).get('skill_test',{}) if task else {})
        if test.get('skill_id')==skill_id and task['agent_id']==bot_id:revision=test['revision']
        if not revision:raise SkillError('skill_version_not_granted')
        self.manifest(skill_id,revision)
        return revision

    def page(self, bot_id, skill_id, *, cursor='', file='SKILL.md', task=None):
        revision=self.authorized_revision(bot_id,skill_id,task=task)
        manifest=self.manifest(skill_id,revision);offset=0
        if cursor:
            try:bound,offset=cursor.split(':');offset=int(offset)
            except (ValueError,AttributeError):raise SkillError('invalid_skill_cursor') from None
            if bound!=revision or offset<0:raise SkillError('stale_skill_cursor')
        if file=='@manifest':
            content=json.dumps(manifest,ensure_ascii=False,indent=2)
        else:
            if file not in {e['path'] for e in manifest['files']}:raise SkillError('skill_file_denied')
            raw=_read_text(self.root/'.versions'/skill_id/revision/file)
            content=_split_frontmatter(raw)[1] if file=='SKILL.md' else raw
        if offset>len(content):raise SkillError('invalid_skill_offset')
        page=content[offset:offset+4500];end=offset+len(page)
        return {'skill_id':skill_id,'revision':revision,'file':file,'directory':manifest['directory'],
                'constraints':manifest['constraints'],'total_chars':len(content),'offset':offset,'content':page,
                'next_cursor':f'{revision}:{end}' if end<len(content) else None,'complete':end==len(content),
                'manifest_cursor':'','manifest_file':'@manifest','file_count':len(manifest['files'])}

    def candidate(self, store, source_task_id, *, name, document, private_literals, files=None):
        with self._lock:
            from .security import digest
            source=store.get_task(source_task_id);outcome=store.outcome(source_task_id)
            if not source or source['status']!='done' or outcome['status']!='verified' or not outcome['user_accepted']:
                raise SkillError('accepted_verified_trace_required')
            if not store.memory_refs_valid(json.loads(source['meta']).get('memory_refs',[])):
                raise SkillError('source_memory_changed')
            if not isinstance(private_literals,list) or any(not isinstance(v,str) or not v for v in private_literals):raise SkillError('privacy_literals_required')
            if not isinstance(name,str) or not isinstance(document,str):raise SkillError('candidate_text_required')
            files=files or {}
            if not isinstance(files,dict) or len(files)>50 or len(json.dumps(files).encode())>512000:raise SkillError('candidate_bundle_limit')
            for rel,text in files.items():
                if not isinstance(rel,str) or not isinstance(text,str) or _safe_relative(rel)=='SKILL.md':raise SkillError('candidate_file_denied')
                if any(value in rel for value in private_literals):raise SkillError('candidate_private_filename_denied')
            for value in sorted(private_literals,key=len,reverse=True):name=name.replace(value,'redacted')
            cleaned=document
            for value in sorted(private_literals,key=len,reverse=True):cleaned=cleaned.replace(value,'[已脱敏]')
            cleaned=re.sub(r'(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._-]{8,}|(?:api[_-]?key|password|token)\s*[:=]\s*[^\s]+)', '[凭据已移除]',cleaned)
            cleaned=re.sub(r'/Users/[^\s"\']+', '[个人路径已移除]',cleaned)
            skill=self.install_from_text(cleaned,name=name,source='verified-task:'+source_task_id)
            files=files or {}
            if not isinstance(files,dict) or len(files)>50 or len(json.dumps(files).encode())>512000:raise SkillError('candidate_bundle_limit')
            for rel,text in files.items():
                rel=_safe_relative(rel)
                if rel=='SKILL.md' or not isinstance(text,str):raise SkillError('candidate_file_denied')
                for value in sorted(private_literals,key=len,reverse=True):text=text.replace(value,'[已脱敏]')
                text=re.sub(r'(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._-]{8,}|(?:api[_-]?key|password|token)\s*[:=]\s*[^\s]+)', '[凭据已移除]',text)
                text=re.sub(r'/Users/[^\s"\']+', '[个人路径已移除]',text)
                path=skill.path/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
            manifest=self.snapshot(skill.id);cid='candidate_'+digest({'source':source_task_id,'revision':manifest['revision']})[:24]
            candidate={'id':cid,'skill_id':skill.id,'revision':manifest['revision'],'source_task_id':source_task_id,
                'source_report_hash':digest(outcome['report']),'source_inputs':json.loads(source['meta'])['envelope']['input_artifacts'],
                'status':'candidate','test_task_id':None,'privacy_review_required':True,'created_at':time.time()}
            settings=self.settings();settings.setdefault('candidates',{})[cid]=candidate;self._save(settings)
            return candidate

    def publish_candidate(self, store, candidate_id, *, test_task_id, bot_ids, revision, privacy_reviewed):
        with self._lock:
            from .security import digest
            settings=self.settings();candidate=settings.get('candidates',{}).get(candidate_id)
            if not candidate or candidate['revision']!=revision or privacy_reviewed is not True or not bot_ids:
                raise SkillError('explicit_publication_review_required')
            source=store.outcome(candidate['source_task_id']);task=store.get_task(test_task_id);outcome=store.outcome(test_task_id)
            if source['status']!='verified' or not source['user_accepted'] or digest(source['report'])!=candidate['source_report_hash']:
                raise SkillError('source_acceptance_changed')
            meta=json.loads(task['meta']) if task else {};test=meta.get('skill_test',{})
            if (not task or task['status']!='done' or outcome['status']!='verified' or not outcome['user_accepted']
                    or test.get('candidate_id')!=candidate_id or test.get('revision')!=revision
                    or revision not in meta.get('skills_used',{}).values()):raise SkillError('actual_verified_candidate_test_required')
            old={i['sha256'] for i in candidate['source_inputs']};new={i['sha256'] for i in meta['envelope']['input_artifacts']}
            if not new or not new-old:raise SkillError('new_input_required')
            refs=json.loads(store.get_task(candidate['source_task_id'])['meta']).get('memory_refs',[])
            if not store.memory_refs_valid(refs):raise SkillError('source_memory_revoked')
            from .attachments import file_path
            for report in (source['report'],outcome['report']):
                for artifact in report.get('artifacts',[]):
                    if hashlib.sha256(file_path(store,artifact['id']).read_bytes()).hexdigest()!=artifact['sha256']:
                        raise SkillError('accepted_artifact_version_changed')
            candidate.update(status='published',test_task_id=test_task_id,privacy_review_required=False)
            settings.setdefault('approved_versions',{}).setdefault(candidate['skill_id'],[]).append(revision)
            self._save(settings)
            for bot in bot_ids:self.grant(bot,candidate['skill_id'],revision)
            return candidate

    def install_from_text(self, body: str, *, name: str = "", description: str = "", source: str = "界面粘贴") -> Skill:
        if not (body or "").strip():
            raise SkillError("技能内容不能为空")
        meta, parsed_body = _split_frontmatter(body)
        resolved_name = (name or str(meta.get("name") or "")).strip() or "未命名技能"
        resolved_description = (description or str(meta.get("description") or "")).strip() or _describe(parsed_body)
        document = self._compose(resolved_name, resolved_description, parsed_body, meta)
        return self._write_skill(document, resolved_name, source=source)

    def install_from_path(self, value: str, *, name: str = "", source: str = "本机目录") -> Skill:
        """从本机目录或单个 .md 文件安装。"""
        with self._lock:
            origin = Path((value or "").strip()).expanduser()
            if not origin.exists():
                raise SkillError(f"路径不存在：{origin}")
            if origin.is_symlink():
                raise SkillError("不接受符号链接，请给出真实路径")
            if origin.is_dir():
                document = origin / SKILL_FILENAME
                if not document.is_file():
                    raise SkillError(f"目录里没有 {SKILL_FILENAME}，这不是一个技能目录")
                for root in self._roots():
                    try:
                        if root.is_dir() and origin.parent.resolve() == root.resolve():
                            raise SkillError("这个目录已经在技能目录里，请直接用现有技能")
                    except OSError:  # pragma: no cover - 解析失败时让后面的复制去报错
                        continue
                text = _read_text(document)
                meta, _ = _split_frontmatter(text)
                resolved_name = (name or str(meta.get("name") or "")).strip() or origin.name
                skill = self._create(resolved_name, source=f"{source}：{origin}")
                shutil.rmtree(skill.path, ignore_errors=True)
                try:
                    files, size = _copy_tree(origin, skill.path)
                except BaseException:
                    shutil.rmtree(skill.path, ignore_errors=True)
                    raise
                if not (skill.path / SKILL_FILENAME).is_file():
                    shutil.rmtree(skill.path, ignore_errors=True)
                    raise SkillError(f"复制后缺少 {SKILL_FILENAME}")
                return self._record(skill.id, resolved_name, files, size, source=f"{source}：{origin}")
            if origin.suffix.lower() not in {".md", ".markdown", ".txt"}:
                raise SkillError("只支持技能目录或 .md 文件")
            if origin.stat().st_size > MAX_BODY_BYTES:
                raise SkillError("技能文件超过 512 KB")
            text = _read_text(origin)
            meta, body = _split_frontmatter(text)
            resolved_name = (name or str(meta.get("name") or "")).strip() or origin.stem
            description = str(meta.get("description") or "").strip() or _describe(body)
            document = self._compose(resolved_name, description, body, meta)
            return self._write_skill(document, resolved_name, source=f"{source}：{origin}")

    async def install_from_url(self, value: str, *, name: str = "", source: str = "URL") -> Skill:
        url = _http_url(value)
        text = await self._download_text(url)
        meta, body = _split_frontmatter(text)
        resolved_name = (name or str(meta.get("name") or "")).strip() or Path(urlsplit(url).path).stem or "未命名技能"
        description = str(meta.get("description") or "").strip() or _describe(body)
        document = self._compose(resolved_name, description, body, meta)
        return self._write_skill(document, resolved_name, source=f"{source}：{url}")

    async def install_from_github(self, value: str, *, subpath: str = "", name: str = "", ref: str = "") -> Skill:
        """从 GitHub 仓库安装：owner/repo 或仓库链接，可指定子目录。"""
        owner, repo = _github_parts(value)
        wanted = _safe_relative(subpath)
        branch = (ref or "").strip() or await self._default_branch(owner, repo)
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch) or ".." in branch:
            raise SkillError("分支名不合法")
        archive_url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
        archive = await self._download_bytes(archive_url, MAX_ARCHIVE_BYTES)
        # 解包是纯 CPU + 内存：放到线程里做，别让整个后端（所有 Bot、所有接口）跟着卡住。
        extracted = await asyncio.to_thread(_extract_skill, archive, wanted)
        if extracted is None:
            hint = f"子目录 {wanted}" if wanted else "仓库根目录"
            raise SkillError(f"在 {hint} 里没有找到 {SKILL_FILENAME}，请确认路径")
        document, files = extracted
        source_text = f"GitHub：{owner}/{repo}" + (f"/{wanted}" if wanted else "") + f"@{branch}"
        return self._install_bundle(document, files, name=name, fallback_name=wanted.split('/')[-1] or repo, source=source_text)

    def install_from_zip(self, raw: bytes, *, subpath: str = "", name: str = "", source: str = "ZIP 技能包") -> Skill:
        """Read bounded regular files only; never extract paths or run package scripts."""
        if not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
            raise SkillError('skill_archive_size_denied')
        wanted = _safe_relative(subpath)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ARCHIVE_MEMBERS:
                    raise SkillError('skill_archive_member_limit')
                files = {}; total = 0; seen = set()
                for entry in entries:
                    path = entry.filename
                    if (not path or path.startswith('/') or '\\' in path or ':' in path
                            or any(ord(c) < 32 for c in path) or '..' in path.split('/')
                            or _safe_relative(path.rstrip('/')) != path.rstrip('/')
                            or path.casefold() in seen or entry.flag_bits & 1
                            or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                        raise SkillError('unsafe_skill_archive_member')
                    seen.add(path.casefold())
                    total += entry.file_size
                    if (entry.file_size > MAX_SKILL_BYTES or total > MAX_SKILL_BYTES
                            or entry.file_size > 200 * max(1, entry.compress_size)):
                        raise SkillError('skill_archive_expansion_limit')
                    if not entry.is_dir():
                        files[path] = entry
                documents = [p for p in files if p == (wanted + '/' if wanted else '') + SKILL_FILENAME]
                if not wanted and not documents:
                    documents = [p for p in files if p.endswith('/' + SKILL_FILENAME)]
                if len(documents) != 1:
                    raise SkillError('请用 subpath 指定唯一包含 SKILL.md 的目录')
                document = documents[0]; base = document[:-len(SKILL_FILENAME)]
                selected = [(p[len(base):], entry) for p, entry in files.items() if p.startswith(base)
                            and not any(part in _SKIP_DIRS for part in p[len(base):].split('/'))]
                if len(selected) > MAX_SKILL_FILES or files[document].file_size > MAX_BODY_BYTES:
                    raise SkillError('skill_bundle_limit')
                contents = [(p, archive.read(entry)) for p, entry in selected if p != SKILL_FILENAME]
                return self._install_bundle(archive.read(files[document]).decode('utf-8-sig'), contents, name=name, source=source)
        except (zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
            if isinstance(exc, SkillError):
                raise
            raise SkillError('无法读取 ZIP 技能包') from exc

    def _install_bundle(self, document, files, *, name='', fallback_name='未命名技能', source='') -> Skill:
        with self._lock:
            meta, body = _split_frontmatter(document)
            resolved_name = (name or str(meta.get("name") or "")).strip() or fallback_name
            description = str(meta.get("description") or "").strip() or _describe(body)
            composed = self._compose(resolved_name, description, body, meta)
            _check_document_size(composed)
            skill = self._create(resolved_name, source=source)
            shutil.rmtree(skill.path, ignore_errors=True)
            skill.path.mkdir(parents=True, exist_ok=True)
            (skill.path / SKILL_FILENAME).write_text(composed, encoding="utf-8")
            kept = {SKILL_FILENAME}
            total = len(composed.encode("utf-8"))
            for relative, content in files:
                if relative in kept:
                    continue
                if len(kept) >= MAX_SKILL_FILES or total + len(content) > MAX_SKILL_BYTES:
                    shutil.rmtree(skill.path)
                    raise SkillError('skill_bundle_limit')
                destination = skill.path / _safe_relative(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                kept.add(relative)
                total += len(content)
            return self._record(skill.id, resolved_name, sorted(kept), total, source=source)

    async def _default_branch(self, owner: str, repo: str) -> str:
        try:
            raw = await self._download_bytes(f"https://api.github.com/repos/{owner}/{repo}", MAX_MARKDOWN_BYTES)
            branch = str((json.loads(raw) or {}).get("default_branch") or "").strip()
            if branch:
                return branch
        except (SkillError, ValueError):
            pass
        return "main"

    async def _download_text(self, url: str) -> str:
        raw = await self._download_bytes(url, MAX_MARKDOWN_BYTES)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError("下载到的技能不是 UTF-8 文本") from exc

    async def _download_bytes(self, url: str, limit: int) -> bytes:
        from .docker_browser import fetch_public
        try:
            async with asyncio.timeout(60):
                for _ in range(6):
                    output = bytearray()
                    response = await fetch_public({'url': _http_url(url), 'method': 'GET',
                        'headers': {'User-Agent': _USER_AGENT}, 'body': ''},
                        self.download_safety, self.download_check, sink=output.extend, max_bytes=limit)
                    if response['status'] in {301, 302, 303, 307, 308}:
                        location = next((v for k, v in response['headers'] if k.lower() == 'location'), '')
                        if not location:
                            raise SkillError('下载重定向缺少地址')
                        url = urljoin(url, location)
                        continue
                    if response['status'] != 200:
                        raise SkillError(f"下载失败：HTTP {response['status']}")
                    return bytes(output)
                raise SkillError('下载重定向次数过多')
        except SkillError:
            raise
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            raise SkillError(f"下载失败：{type(exc).__name__}") from exc

    def remove(self, skill_id: str) -> None:
        with self._lock:
            skill = self.get(skill_id)
            # 只允许删「本机安装目录」里的技能：额外技能根目录是给人自己管的，
            # Carme 只读不写，免得误删一份别人维护的共享技能。
            try:
                owned = skill.path.parent.resolve() == self.root.resolve()
            except OSError:  # pragma: no cover - 解析失败按只读处理
                owned = False
            if not owned:
                raise SkillError("这个技能来自只读技能目录，请在对应目录里删除")
            shutil.rmtree(skill.path, ignore_errors=True)
            if skill.path.exists():
                raise SkillError(f"无法删除 {skill.path}，请检查文件权限或占用后重试")
            settings = self.settings()
            settings["disabled"] = [item for item in settings["disabled"] if item != skill.id]
            installed = settings.get("installed") or {}
            installed.pop(skill.id, None)
            for grants in settings.get('grants',{}).values():grants.pop(skill.id,None)
            for bot, disabled in settings.get('disabled_by_bot', {}).items():
                settings['disabled_by_bot'][bot] = [sid for sid in disabled if sid != skill.id]
            settings['aliases'] = {k: v for k, v in settings.get('aliases', {}).items() if v != skill.id}
            self._save(settings)

    def set_enabled(self, skill_id: str, enabled: bool) -> Skill:
        with self._lock:
            skill = self.get(skill_id)
            settings = self.settings()
            disabled = {str(item) for item in settings.get("disabled") or []}
            if enabled:
                disabled.discard(skill.id)
            else:
                disabled.add(skill.id)
            settings["disabled"] = sorted(disabled)
            self._save(settings)
            skill.enabled = enabled
            return skill

        # ---------------- 安装落盘 ----------------

    def _compose(self, name: str, description: str, body: str, meta: dict[str, Any]) -> str:
        front: dict[str, Any] = {"name": name}
        if description:
            front["description"] = description
        if 'constraints' in meta:
            if not isinstance(meta['constraints'],list) or any(not isinstance(v,str) for v in meta['constraints']):raise SkillError('constraints_must_be_strings')
            front['constraints']=meta['constraints']
        for key in ("version", "license", "author"):
            value = str(meta.get(key) or "").strip()
            if value:
                front[key] = value
        header = yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip()
        text = (body or "").strip()
        return f"---\n{header}\n---\n\n{text}\n"

    def _create(self, name: str, *, source: str) -> Skill:
        base = _skill_id(name)
        self.root.mkdir(parents=True, exist_ok=True)
        candidate = base
        index = 2
        while (self.root / candidate).exists():
            candidate = f"{base}-{index}"
            index += 1
            if index > 50:
                raise SkillError("同名技能过多，请换一个名字")
        path = self.root / candidate
        path.mkdir(parents=True)
        return Skill(id=candidate, name=name[:120], description="", path=path, source=source)

    def _write_skill(self, document: str, name: str, *, source: str) -> Skill:
        with self._lock:
            _check_document_size(document)
            skill = self._create(name, source=source)
            target = skill.path / SKILL_FILENAME
            target.write_text(document, encoding="utf-8")
            return self._record(skill.id, name, [SKILL_FILENAME], len(document.encode("utf-8")), source=source)

    def _record(self, skill_id: str, name: str, files: Iterable[str], size: int, *, source: str) -> Skill:
        settings = self.settings()
        installed = settings.setdefault("installed", {})
        if not source.startswith('verified-task:'):
            entries = _bundle_entries(self.root / skill_id)
            candidates = {c['skill_id'] for c in settings.get('candidates', {}).values()}
            for existing in installed:
                path = self.root / existing
                if existing == skill_id or existing in candidates or not path.is_dir():
                    continue
                try: existing_entries = _bundle_entries(path)
                except (SkillError, OSError): continue
                if existing_entries == entries:
                    shutil.rmtree(self.root / skill_id)
                    return self.get(existing)
        installed[skill_id] = {"source": source, "installed_at": time.time(), "name": name[:120]}
        self._save(settings)
        skill = self.get(skill_id)
        skill.source = source
        skill.files = [item for item in files]
        skill.size = size
        return skill


# --------------------------------------------------------------------------- #
#  下载与解包
# --------------------------------------------------------------------------- #


def _http_url(value: str) -> str:
    url = (value or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SkillError("请填写以 http:// 或 https:// 开头的地址")
    if parts.username or parts.password:
        raise SkillError("地址里不能带用户名或密码")
    return url


def _github_parts(value: str) -> tuple[str, str]:
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise SkillError("请填写 owner/repo 或 GitHub 仓库地址")
    if raw.startswith("http://") or raw.startswith("https://"):
        parts = urlsplit(raw)
        if (parts.hostname or "").lower() not in {"github.com", "www.github.com"}:
            raise SkillError("只支持 github.com 上的仓库")
        raw = parts.path.strip("/")
    raw = raw.removesuffix(".git")
    segments = [item for item in raw.split("/") if item]
    if len(segments) < 2:
        raise SkillError("请按 owner/repo 的格式填写")
    owner, repo = segments[0], segments[1]
    for item in (owner, repo):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", item) or item in {".", ".."}:
            raise SkillError("仓库名不合法")
    return owner, repo


def _extract_skill(archive: bytes, subpath: str) -> tuple[str, list[tuple[str, bytes]]] | None:
    """在 tarball 里找 SKILL.md；返回 (SKILL.md 正文, 同目录其他文件)。

    压缩包是外部输入：成员数、单个成员大小和解压总量都要封顶，
    否则一个刻意构造的压缩包就能把内存吃光。整个函数是同步阻塞的，
    调用方用 asyncio.to_thread 跑它。
    """
    try:
        handle = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
    except tarfile.TarError as exc:
        raise SkillError("下载到的压缩包无法解析，请确认仓库地址") from exc
    with handle:
        candidates: list[tuple[str, tarfile.TarInfo]] = []
        members: list[tarfile.TarInfo] = []
        seen_members = 0
        expanded_bytes = 0
        for member in handle:
            seen_members += 1
            expanded_bytes += member.size
            if seen_members > MAX_ARCHIVE_MEMBERS:
                raise SkillError(f"仓库里的文件超过 {MAX_ARCHIVE_MEMBERS} 个，请用子目录指定技能位置")
            if member.size > MAX_ARCHIVE_BYTES or expanded_bytes > MAX_ARCHIVE_BYTES * 4:
                raise SkillError('skill_archive_expansion_limit')
            name = member.name.replace("\\", "/").lstrip("./")
            if not name or name.startswith("/") or ".." in name.split("/"):
                continue
            if member.issym() or member.islnk() or member.isdev():
                continue
            if not (member.isfile() or member.isdir()):
                continue
            parts = name.split("/")
            if len(parts) < 2:
                continue
            relative = "/".join(parts[1:])
            wanted_document = f"{subpath}/{SKILL_FILENAME}" if subpath else SKILL_FILENAME
            if relative == wanted_document:
                candidates.append((relative, member))
            if subpath and not relative.startswith(subpath + "/"):
                continue
            members.append(member)
        if not candidates:
            return None
        if len(candidates) > 1 and subpath:
            exact = [item for item in candidates if item[0] == f"{subpath}/{SKILL_FILENAME}"]
            candidates = exact or candidates
        _, document_member = sorted(candidates, key=lambda item: item[0])[0]
        raw = handle.extractfile(document_member)
        if raw is None:
            return None
        document = raw.read(MAX_BODY_BYTES + 1)
        if len(document) > MAX_BODY_BYTES:
            raise SkillError("仓库里的 SKILL.md 超过 512 KB")
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError("仓库里的 SKILL.md 不是 UTF-8 文本") from exc
        base = document_member.name.replace("\\", "/").lstrip("./").rsplit("/", 1)[0]
        others: list[tuple[str, bytes]] = []
        total = len(document)
        for member in members:
            name = member.name.replace("\\", "/").lstrip("./")
            if not member.isfile() or not name.startswith(base + "/"):
                continue
            relative = name[len(base) + 1:]
            relative = _safe_relative(relative)
            if not relative or relative == SKILL_FILENAME:
                continue
            if relative.split("/")[0] in _SKIP_DIRS:
                continue
            if member.size > MAX_SKILL_BYTES or total + member.size > MAX_SKILL_BYTES:
                raise SkillError('skill_bundle_limit')
            stream = handle.extractfile(member)
            if stream is None:
                continue
            content = stream.read(MAX_SKILL_BYTES + 1)
            if len(content) > MAX_SKILL_BYTES:
                raise SkillError('skill_bundle_limit')
            others.append((relative, content))
            total += len(content)
            if len(others) >= MAX_SKILL_FILES:
                raise SkillError('skill_bundle_limit')
        return text, others
