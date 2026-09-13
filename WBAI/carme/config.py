"""配置加载：agents.yaml / models.yaml / sandbox.yaml -> 类型化对象。

设计原则：配置即真相。角色、模型路由、执行环境全部外置成文本，
改配置不需要动代码，也不需要重启进程（见 reload()）。
"""

from __future__ import annotations

import os
import re
import tempfile
import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("CARME_CONFIG_DIR", str(PROJECT_ROOT / "config"))).expanduser()
DATA_DIR = Path(os.getenv("CARME_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()
ENV_FILE = Path(os.getenv("CARME_ENV_FILE", str(PROJECT_ROOT / ".env"))).expanduser()
API_TYPES = ("openai", "openai_responses", "anthropic", "openai_compatible")
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ENGINE_IDS = ("api", "codex", "pi", "claude")


def _expand(value: Any, *, expand_home: bool = True) -> Any:
    """展开字符串里的 ~ 和 $ENV_VAR。"""
    if isinstance(value, str):
        return os.path.expandvars(os.path.expanduser(value) if expand_home else value)
    if isinstance(value, dict):
        return {k: _expand(v, expand_home=expand_home) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, expand_home=expand_home) for v in value]
    return value


def _load_yaml(path: Path, *, expand_home: bool = True, expand_values: bool = True) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
        return _expand(raw, expand_home=expand_home) if expand_values else raw


# --------------------------------------------------------------------------- #
#  模型配置
# --------------------------------------------------------------------------- #


@dataclass
class Provider:
    name: str
    type: str  # openai | anthropic
    base_url: str
    api_key_env: str
    api_key_default: str = field(default="", repr=False)
    is_local: bool = False  # 本地服务（如 ollama），不计入云端供应商统计
    label: str = ""

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "") or self.api_key_default

    @property
    def available(self) -> bool:
        return bool(self.api_key)


@dataclass
class ModelSpec:
    id: str
    price_in: float = 0.0
    price_out: float = 0.0
    effort: str = ""
    effort_options: list[str] = field(default_factory=list)
    effort_source: str = "unknown"
    verified: bool = False


@dataclass
class ModelsConfig:
    providers: dict[str, Provider] = field(default_factory=dict)
    models: dict[str, ModelSpec] = field(default_factory=dict)
    tiers: dict[str, list[str]] = field(default_factory=dict)
    budget_daily_usd: float = 0.0
    budget_warn_ratio: float = 0.8
    allow_mock: bool = False
    # 只在当前进程记录最近一次模型列表检测确认失效的引用；不删除用户配置。
    invalid_refs: set[str] = field(default_factory=set, repr=False, compare=False)

    def resolve(self, ref: str) -> tuple[str, Provider, str]:
        """把 'anthropic/claude-sonnet-4-6' 拆成 (provider_name, Provider, model_id)。

        模型 id 里可能带斜杠（如 openrouter/anthropic/claude-...），
        所以按第一个斜杠切分供应商，剩下的全部算模型名。
        """
        if "/" not in ref:
            raise ValueError(f"模型引用缺少供应商前缀：{ref!r}，应形如 provider/model")
        provider_name, model_id = ref.split("/", 1)
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"未登记的供应商：{provider_name!r}（见 config/models.yaml）")
        return provider_name, provider, model_id

    def price(self, ref: str) -> ModelSpec:
        return self.models.get(ref, ModelSpec(id=ref))

    def candidates(self, tier: str) -> list[str]:
        """返回该档位下「当前有 key、可以真正调用」的候选模型，按优先级排序。"""
        raw = self.tiers.get(tier) or []
        usable: list[str] = []
        for ref in raw:
            if ref in self.invalid_refs or ref in usable:
                continue
            try:
                _, provider, _ = self.resolve(ref)
            except ValueError:
                continue
            if provider.type == "mock" and not self.allow_mock:
                continue
            if provider.available:
                usable.append(ref)
        return usable


# --------------------------------------------------------------------------- #
#  角色配置
# --------------------------------------------------------------------------- #


@dataclass
class AgentSpec:
    id: str
    name: str
    title: str = ""
    emoji: str = "🤖"
    entry: bool = False
    tier: str = "balanced"
    model: str = ""  # 直接指定模型时覆盖 tier
    effort: str = ""  # 留空继承模型连接设置
    engine: str = "api"  # api | codex | pi | claude
    engine_model: str = ""  # CLI 的可选模型名，不影响内置 API 的 model 引用
    engine_effort: str = ""  # CLI 推理强度；与 API 的 effort 分开保存
    engine_workspace: str = ""  # CLI 后端 Mac 工作目录；空值按 Bot 在 data 下隔离
    avatar: dict = field(default_factory=dict)
    can_delegate: bool = False
    sandbox: str = "local"
    tools: list[str] = field(default_factory=list)
    prompt: str = ""

    @property
    def display(self) -> str:
        return f"{self.emoji} {self.name}" + (f" · {self.title}" if self.title else "")


@dataclass
class AgentsConfig:
    defaults: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, AgentSpec] = field(default_factory=dict)

    @property
    def entry_agent(self) -> AgentSpec:
        for spec in self.agents.values():
            if spec.entry:
                return spec
        return next(iter(self.agents.values()))

    def get(self, agent_id: str) -> AgentSpec:
        spec = self.agents.get(agent_id)
        if spec is None:
            raise KeyError(f"没有这个成员：{agent_id}（可选：{', '.join(self.agents)}）")
        return spec


# --------------------------------------------------------------------------- #
#  沙箱配置
# --------------------------------------------------------------------------- #


@dataclass
class SandboxConfig:
    default_mode: str = "local"
    modes: dict[str, dict] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    nodes: list[dict] = field(default_factory=list)
    default_node_id: str = ""

    def list_nodes(self) -> list[dict]:
        """只报告配置状态；未探测过的电脑不能显示为在线。"""
        result = []
        for raw in self.nodes:
            node = _normalise_node(raw)
            configured = bool(node["host"] and node["user"])
            result.append({**node, "is_default": node["node_id"] == self.default_node_id,
                           "configured": configured, "status": "unknown" if configured else "unconfigured"})
        return result

    def resolve_node(self, node_id: str | None = None) -> dict:
        """返回任务拥有的连接快照；切换默认电脑不会改变已创建的任务。"""
        selected = node_id if node_id is not None else self.default_node_id
        if not selected:
            raise ValueError("尚未选择 Bot 的执行电脑，请在设置中登记并选择默认电脑")
        for raw in self.nodes:
            if raw.get("node_id", raw.get("id")) == selected:
                node = _normalise_node(raw)
                if not node["host"] or not node["user"]:
                    raise ValueError(f"执行电脑 {node['name']} 尚未配置 SSH 地址和用户")
                # 补齐公共资源限制后复制，绝不继承 modes.remote 中的地址或 fallback。
                settings = {k: deepcopy(v) for k, v in self.mode("remote").items()
                            if k in {"timeout_seconds", "max_output_bytes", "connect_timeout"}}
                return {**settings, **deepcopy(node), "fallback": "none", "use_docker": False}
        raise ValueError(f"未登记的执行电脑：{selected}")

    def mode(self, name: str) -> dict:
        return self.modes.get(name, {})

    def fallback_for(self, mode: str) -> str | None:
        """仅兼容明确配置的本地开发模式；远程执行从不落回后端。"""
        if mode == "remote":
            return None
        target = str(self.mode(mode).get("fallback", "") or "").strip()
        if not target or target == "none" or target == mode:
            return None
        return target if target in self.modes else None

    @property
    def max_concurrent_sandbox(self) -> int:
        return int(self.limits.get("max_concurrent_sandbox", 1))

    @property
    def max_concurrent_tasks(self) -> int:
        return int(self.limits.get("max_concurrent_tasks", 3))


# --------------------------------------------------------------------------- #
#  浏览器代操作配置
# --------------------------------------------------------------------------- #


@dataclass
class BrowserConfig:
    """config/browser.yaml 的类型化视图。

    为什么单独立一层：代操作是唯一能让 Bot 动到你真实账号的能力，
    它的安全参数（危险词、超时、并发、白名单）必须能被代码直接读到，
    而不是散在工具里各读各的 yaml。
    """

    enabled: bool = True
    headless: bool = True
    default_profile: str = "default"
    profiles: dict[str, dict] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    screenshots: dict[str, Any] = field(default_factory=dict)

    def as_manager_settings(self) -> dict:
        """还原成 BrowserManager 认的原始形状。"""
        return {
            "enabled": self.enabled,
            "headless": self.headless,
            "default_profile": self.default_profile,
            "profiles": self.profiles,
            "safety": self.safety,
            "screenshots": self.screenshots,
        }

    # ---- 安全 ----

    @property
    def require_confirmation(self) -> bool:
        return bool(self.safety.get("require_confirmation", True))

    @property
    def approval_timeout(self) -> float:
        return float(self.safety.get("approval_timeout_seconds", 600) or 600)

    @property
    def dangerous_patterns(self) -> list[str]:
        return [str(p) for p in (self.safety.get("dangerous_patterns") or [])]

    @property
    def allowed_domains(self) -> list[str]:
        return [str(d) for d in (self.safety.get("allowed_domains") or [])]

    # ---- 资源 ----

    @property
    def max_concurrent(self) -> int:
        return int(self.safety.get("max_concurrent_browser", 1))

    @property
    def idle_close_seconds(self) -> float:
        return float(self.safety.get("idle_close_seconds", 300))

    # ---- 截图 ----

    @property
    def screenshots_enabled(self) -> bool:
        return bool(self.screenshots.get("enabled", True))

    @property
    def keep_screenshots(self) -> int:
        return int(self.screenshots.get("keep", 100))

    def screenshots_dir(self, root: Path) -> Path:
        raw = Path(str(self.screenshots.get("dir", "./data/screenshots")))
        return raw if raw.is_absolute() else root / raw


# --------------------------------------------------------------------------- #
#  汇总
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    agents: AgentsConfig
    models: ModelsConfig
    sandbox: SandboxConfig
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    root: Path = PROJECT_ROOT
    # Tests and alternate profiles can supply their own root without touching
    # the live DATA_DIR.  load() fills the deployed data directory explicitly.
    data_dir: Path | None = None


_cache: Config | None = None


def load(reload: bool = False) -> Config:
    """加载全部配置。reload=True 时忽略缓存重新读盘。"""
    global _cache
    if _cache is not None and not reload:
        return _cache

    if os.getenv("CARME_LOAD_ENV", "1").lower() not in {"0", "false", "no"}:
        load_dotenv(ENV_FILE, override=False, interpolate=False)

    raw_models = _load_yaml(CONFIG_DIR / "models.yaml", expand_values=False)
    providers = {
        name: Provider(
            name=name,
            type=spec.get("type", "openai"),
            base_url=str(spec.get("base_url", "")).rstrip("/"),
            api_key_env=spec.get("api_key_env", f"{name.upper()}_API_KEY"),
            api_key_default=spec.get("api_key_default", ""),
            is_local=bool(spec.get("local", False)),
            label=str(spec.get("label", name)),
        )
        for name, spec in (raw_models.get("providers") or {}).items()
    }
    models = {
        m["id"]: ModelSpec(
            id=m["id"],
            price_in=float(m.get("in", 0.0)),
            price_out=float(m.get("out", 0.0)),
            effort=str(m.get("effort", "")),
            effort_options=list(m.get("effort_options") or []),
            effort_source=str(m.get("effort_source", "unknown")),
            verified=bool(m.get("verified", False)),
        )
        for m in (raw_models.get("models") or [])
        if "id" in m
    }
    tiers = {
        name: list(spec.get("candidates") or [])
        for name, spec in (raw_models.get("tiers") or {}).items()
    }
    budget = raw_models.get("budget") or {}
    models_cfg = ModelsConfig(
        providers=providers,
        models=models,
        tiers=tiers,
        budget_daily_usd=float(budget.get("daily_usd", 0.0) or 0.0),
        budget_warn_ratio=float(budget.get("warn_at_ratio", 0.8)),
        allow_mock=bool(raw_models.get("allow_mock", False)),
    )

    # 名称和角色指令是用户文本；展开其中的 ${...} 会把后端环境中的密钥送进模型。
    raw_agents = _load_yaml(CONFIG_DIR / "agents.yaml", expand_values=False)
    agent_specs: dict[str, AgentSpec] = {}
    for agent_id, spec in (raw_agents.get("agents") or {}).items():
        agent_specs[agent_id] = AgentSpec(
            id=agent_id,
            name=spec.get("name", agent_id),
            title=spec.get("title", ""),
            emoji=spec.get("emoji", "🤖"),
            entry=bool(spec.get("entry", False)),
            tier=spec.get("tier", "balanced"),
            model=spec.get("model", ""),
            effort=spec.get("effort", ""),
            engine=str(spec.get("engine", "api") or "api"),
            engine_model=str(spec.get("engine_model", "") or ""),
            engine_effort=str(spec.get("engine_effort", spec.get("effort", "") if str(spec.get("engine", "api") or "api") != "api" else "") or ""),
            engine_workspace=str(spec.get("engine_workspace", "") or ""),
            avatar=spec.get("avatar") or {},
            can_delegate=bool(spec.get("can_delegate", False)),
            sandbox=spec.get("sandbox", "local"),
            tools=list(spec.get("tools") or []),
            prompt=str(spec.get("prompt", "")).strip(),
        )
    agents_cfg = AgentsConfig(
        defaults=raw_agents.get("defaults") or {},
        agents=agent_specs,
    )

    # 远端 ~/... 是执行电脑的用户目录，不能展开成后端 Mac 的 HOME。
    raw_sandbox = _load_yaml(CONFIG_DIR / "sandbox.yaml", expand_values=False)
    sandbox_cfg = SandboxConfig(
        default_mode=raw_sandbox.get("default_mode", "local"),
        modes=_expand(raw_sandbox.get("modes") or {}, expand_home=False),
        limits=raw_sandbox.get("limits") or {},
        nodes=raw_sandbox.get("nodes") or [],
        default_node_id=str(raw_sandbox.get("default_node_id") or ""),
    )

    raw_browser = _load_yaml(CONFIG_DIR / "browser.yaml")
    browser_cfg = BrowserConfig(
        enabled=bool(raw_browser.get("enabled", True)),
        headless=bool(raw_browser.get("headless", True)),
        default_profile=raw_browser.get("default_profile", "default"),
        profiles=raw_browser.get("profiles") or {},
        safety=raw_browser.get("safety") or {},
        screenshots=raw_browser.get("screenshots") or {},
    )

    _cache = Config(
        agents=agents_cfg,
        models=models_cfg,
        sandbox=sandbox_cfg,
        browser=browser_cfg,
        data_dir=DATA_DIR,
    )
    return _cache


def _normalise_node(raw: dict) -> dict:
    """节点配置只存连接参数及密钥文件引用，不接受密码或密钥内容。"""
    node_id = str(raw.get("node_id", raw.get("id", ""))).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", node_id):
        raise ValueError("node_id 只能使用字母、数字、下划线和短横线，长度 1–64")
    allowed = {"node_id", "id", "name", "host", "port", "user", "identity_file", "root",
               "connect_timeout", "browser", "desktop"}
    if set(raw) - allowed:
        raise ValueError(f"不支持的执行电脑配置字段：{', '.join(sorted(set(raw) - allowed))}")
    node = {"node_id": node_id, "name": str(raw.get("name") or node_id).strip(),
            "host": str(raw.get("host") or "").strip(), "user": str(raw.get("user") or "").strip(),
            "port": int(raw.get("port", 22)), "identity_file": str(raw.get("identity_file") or ""),
            "root": str(raw.get("root") or "~/carme-node/workspaces"),
            "connect_timeout": int(raw.get("connect_timeout", 10)),
            "browser": deepcopy(raw.get("browser") or {}), "desktop": deepcopy(raw.get("desktop") or {})}
    for key, pattern in (("host", r"[A-Za-z0-9_.:%\[\]-]+"), ("user", r"[A-Za-z_][A-Za-z0-9_.-]*")):
        if node[key] and (node[key].startswith("-") or not re.fullmatch(pattern, node[key])):
            raise ValueError(f"{key} 包含无效字符")
    if not 1 <= node["port"] <= 65535 or not 1 <= node["connect_timeout"] <= 60:
        raise ValueError("SSH 端口应在 1–65535，连接超时应在 1–60 秒")
    if any("\x00" in node[k] or "\n" in node[k] for k in ("name", "identity_file", "root")):
        raise ValueError("执行电脑配置不能包含换行或空字符")
    if "PRIVATE KEY" in node["identity_file"]:
        raise ValueError("identity_file 只能保存密钥文件路径，不能保存密钥内容")
    if not node["root"].startswith(("/", "~/")):
        raise ValueError("远端工作目录必须是绝对路径或以 ~/ 开头")
    for key in ("browser", "desktop"):
        if not isinstance(node[key], dict):
            raise ValueError(f"{key} 必须是对象")
    browser_allowed = {"cdp_host", "cdp_port", "profiles"}
    if set(node["browser"]) - browser_allowed:
        raise ValueError("browser 只支持 cdp_host、cdp_port 和 profiles")
    profiles = node["browser"].get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("浏览器 profiles 必须是身份名称到 CDP 配置的对象")
    targets = [node["browser"], *profiles.values()]
    for index, target in enumerate(targets):
        allowed_fields = browser_allowed if index == 0 else {"cdp_host", "cdp_port"}
        if not isinstance(target, dict) or set(target) - allowed_fields:
            raise ValueError("浏览器身份只保存 CDP 连接参数")
        if target.get("cdp_host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("CDP 必须连接执行电脑的本地回环地址，经 SSH 隧道访问")
        if not 1 <= int(target.get("cdp_port", 9222)) <= 65535:
            raise ValueError("CDP 端口应在 1–65535")
    if set(node["desktop"]) - {"enabled", "vnc_port"}:
        raise ValueError("desktop 只支持 enabled 和 vnc_port；认证信息不得存入节点配置")
    if not 1 <= int(node["desktop"].get("vnc_port", 5900)) <= 65535:
        raise ValueError("VNC 端口应在 1–65535")
    return node


def _save_sandbox_change(change) -> SandboxConfig:
    path = CONFIG_DIR / "sandbox.yaml"
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    change(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换，保留非节点配置以及尚未展开的环境变量引用。
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".yaml", delete=False, encoding="utf-8") as stream:
            temp_path = Path(stream.name)
            yaml.safe_dump(raw, stream, allow_unicode=True, sort_keys=False)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return load(reload=True).sandbox


def save_node(data: dict) -> SandboxConfig:
    node = _normalise_node(data)
    def change(raw: dict) -> None:
        nodes = list(raw.get("nodes") or [])
        for index, old in enumerate(nodes):
            if old.get("node_id", old.get("id")) == node["node_id"]:
                nodes[index] = node
                break
        else:
            nodes.append(node)
        raw["nodes"] = nodes
    return _save_sandbox_change(change)


def set_default_node(node_id: str) -> SandboxConfig:
    def change(raw: dict) -> None:
        nodes = raw.get("nodes") or []
        config = SandboxConfig(nodes=nodes, default_node_id=node_id)
        config.resolve_node()  # 不把缺少地址的草稿设为默认电脑。
        raw["default_node_id"] = node_id
    return _save_sandbox_change(change)


def atomic_write(path: Path, content: bytes) -> None:
    """替换完整文件；临时文件和结果文件只允许当前用户读写。"""
    if path.is_symlink():
        raise ValueError("配置或上传目标不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_model_connection(provider: Provider, selected: list[dict]) -> ModelsConfig:
    """保存经过接口验证的模型；key 只写 .env，YAML 只记录变量名。"""
    path = CONFIG_DIR / "models.yaml"
    raw = _load_yaml(path, expand_values=False)
    env_name = "CARME_MODEL_KEY_" + hashlib.sha256(provider.name.encode()).hexdigest()[:16].upper()
    old_env = ENV_FILE.read_bytes() if ENV_FILE.exists() else None
    env_text = (old_env or b"").decode("utf-8")
    # 本应用生成的独立变量名，不修改已有供应商共享的环境变量。
    line = f"{env_name}='" + provider.api_key.replace("\\", "\\\\").replace("'", "\\'") + "'\n"
    lines = [item for item in env_text.splitlines(keepends=True)
             if not re.match(rf"^\s*(?:export\s+)?{env_name}\s*=", item)]
    env_text = "".join(lines)
    if env_text and not env_text.endswith("\n"):
        env_text += "\n"
    providers = raw.setdefault("providers", {})
    old_provider = providers.get(provider.name) or {}
    providers[provider.name] = {
        **old_provider, "label": provider.label or provider.name, "type": provider.type,
        "base_url": provider.base_url, "api_key_env": env_name,
    }
    providers[provider.name].pop("api_key_default", None)
    models = {model["id"]: model for model in raw.get("models", [])}
    for model in selected:
        ref = f"{provider.name}/{model['id']}"
        models[ref] = {**models.get(ref, {}), **model, "id": ref, "verified": True}
    raw["models"] = list(models.values())
    encoded = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode()
    atomic_write(ENV_FILE, (env_text + line).encode())
    try:
        atomic_write(path, encoded)
    except Exception:
        if old_env is None:
            ENV_FILE.unlink(missing_ok=True)
        else:
            atomic_write(ENV_FILE, old_env)
        raise
    os.environ[env_name] = provider.api_key
    return load(reload=True).models


def save_model_routing(tiers: dict[str, list[str]], allow_mock: bool) -> ModelsConfig:
    path = CONFIG_DIR / "models.yaml"
    raw = _load_yaml(path, expand_values=False)
    for name, refs in tiers.items():
        raw.setdefault("tiers", {}).setdefault(name, {})["candidates"] = refs
    raw["allow_mock"] = allow_mock
    atomic_write(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode())
    return load(reload=True).models


def delete_models(provider_name: str, model_ids: list[str]) -> ModelsConfig:
    """从 models.yaml 移除指定模型条目；档位与成员引用由路由层先行校验。"""
    path = CONFIG_DIR / "models.yaml"
    raw = _load_yaml(path, expand_values=False)
    if provider_name not in (raw.get("providers") or {}):
        raise ValueError(f"连接不存在：{provider_name}")
    drop = {f"{provider_name}/{model_id}" for model_id in model_ids}
    kept = [model for model in raw.get("models", []) if str(model.get("id", "")) not in drop]
    if len(kept) == len(raw.get("models", [])):
        raise ValueError("所选模型不在已配置列表中")
    raw["models"] = kept
    atomic_write(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode())
    return load(reload=True).models


def delete_model_connection(provider_name: str) -> ModelsConfig:
    """删除连接及其全部模型条目，并清理 .env 中对应的密钥行；写入失败时回滚密钥文件。"""
    path = CONFIG_DIR / "models.yaml"
    raw = _load_yaml(path, expand_values=False)
    providers = raw.get("providers") or {}
    if provider_name not in providers:
        raise ValueError(f"连接不存在：{provider_name}")
    env_name = str(providers[provider_name].get("api_key_env") or "")
    providers.pop(provider_name)
    raw["providers"] = providers
    raw["models"] = [model for model in raw.get("models", [])
                     if not str(model.get("id", "")).startswith(f"{provider_name}/")]
    encoded = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode()
    old_env = ENV_FILE.read_bytes() if ENV_FILE.exists() else None
    if env_name and old_env is not None:
        env_text = old_env.decode("utf-8")
        kept_lines = [item for item in env_text.splitlines(keepends=True)
                      if not re.match(rf"^\s*(?:export\s+)?{re.escape(env_name)}\s*=", item)]
        atomic_write(ENV_FILE, "".join(kept_lines).encode())
    try:
        atomic_write(path, encoded)
    except Exception:
        if old_env is not None:
            atomic_write(ENV_FILE, old_env)
        raise
    if env_name:
        os.environ.pop(env_name, None)
    return load(reload=True).models
