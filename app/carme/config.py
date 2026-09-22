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
ENV_FILE = Path(os.getenv("CARME_ENV_FILE", str(CONFIG_DIR / ".env"))).expanduser()
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
    emoji: str = ""
    entry: bool = False
    tier: str = "balanced"
    model: str = ""  # 直接指定模型时覆盖 tier
    effort: str = ""  # 留空继承模型连接设置
    engine: str = "api"  # api | codex | pi | claude
    engine_model: str = ""  # CLI 的可选模型名，不影响内置 API 的 model 引用
    engine_effort: str = ""  # CLI 推理强度；与 API 的 effort 分开保存
    engine_workspace: str = ""  # Legacy metadata only; never grants access to a host directory.
    runtime_profile: str = ""
    execution_target: str = "none"  # none | container | ssh | macos
    execution_target_id: str = ""
    avatar: dict = field(default_factory=dict)
    can_delegate: bool = False
    sandbox: str = "none"
    tools: list[str] = field(default_factory=list)
    prompt: str = ""

    @property
    def display(self) -> str:
        return f"{self.emoji} {self.name}".strip() + (f" · {self.title}" if self.title else "")


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
    default_mode: str = "none"
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
        """An unavailable target is never a grant to execute somewhere else."""
        return None

    def limit(self, name: str, default: int) -> int:
        value = self.limits.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"invalid_limit:{name}")
        return value

    @property
    def max_concurrent_sandbox(self) -> int:
        return self.limit("max_concurrent_sandbox", 1)

    @property
    def max_concurrent_tasks(self) -> int:
        return self.limit("max_concurrent_tasks", 3)


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
    # Playwright 渠道：留空用自带 Chromium；设 "chrome" 用系统/镜像内的 Google Chrome 正式版。
    channel: str = ""
    safety: dict[str, Any] = field(default_factory=dict)
    screenshots: dict[str, Any] = field(default_factory=dict)
    desktop: dict[str, Any] = field(default_factory=dict)

    def as_manager_settings(self) -> dict:
        """还原成 BrowserManager 认的原始形状。"""
        return {
            "enabled": self.enabled,
            "headless": self.headless,
            "default_profile": self.default_profile,
            "profiles": self.profiles,
            "channel": self.channel,
            "safety": self.safety,
            "screenshots": self.screenshots,
            "desktop": self.desktop,
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
    isolation: dict = field(default_factory=dict)
    # 语音输入：浏览器识别或服务端转写（见 load_voice / save_voice）。
    voice: dict = field(default_factory=dict)


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
            emoji=spec.get("emoji", ""),
            entry=bool(spec.get("entry", False)),
            tier=spec.get("tier", "balanced"),
            model=spec.get("model", ""),
            effort=spec.get("effort", ""),
            engine=str(spec.get("engine", "api") or "api"),
            engine_model=str(spec.get("engine_model", "") or ""),
            engine_effort=str(spec.get("engine_effort", spec.get("effort", "") if str(spec.get("engine", "api") or "api") != "api" else "") or ""),
            engine_workspace=str(spec.get("engine_workspace", "") or ""),
            runtime_profile=str(spec.get("runtime_profile", "") or ""),
            execution_target=str(spec.get("execution_target", "none") or "none"),
            execution_target_id=str(spec.get("execution_target_id", "") or ""),
            avatar=spec.get("avatar") or {},
            can_delegate=bool(spec.get("can_delegate", False)),
            sandbox=spec.get("sandbox", "none"),
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
        default_mode=raw_sandbox.get("default_mode", "none"),
        modes=_expand(raw_sandbox.get("modes") or {}, expand_home=False),
        limits=raw_sandbox.get("limits") or {},
        nodes=raw_sandbox.get("nodes") or [],
        default_node_id=str(raw_sandbox.get("default_node_id") or ""),
    )
    # Migrate only a previously explicit SSH selection. Legacy local/backend grants stay inactive.
    for agent_id, spec in agent_specs.items():
        if "execution_target" not in raw_agents["agents"][agent_id] and sandbox_cfg.default_node_id:
            spec.execution_target = "ssh"
    for name, default in {"max_task_seconds": 600, "max_daily_tasks": 200, "max_tool_calls": 32,
                          "max_output_bytes": 65536, "max_concurrent_tasks": 3,
                          "max_concurrent_sandbox": 1}.items():
        sandbox_cfg.limit(name, default)
    if any(str(mode.get("fallback", "none")) not in {"", "none"} for mode in sandbox_cfg.modes.values()):
        raise ValueError("execution_fallback_disabled")

    raw_browser = _load_yaml(CONFIG_DIR / "browser.yaml")
    browser_cfg = BrowserConfig(
        enabled=bool(raw_browser.get("enabled", True)),
        headless=bool(raw_browser.get("headless", True)),
        default_profile=raw_browser.get("default_profile", "default"),
        channel=_browser_channel(raw_browser.get("channel")),
        profiles=raw_browser.get("profiles") or {},
        safety=raw_browser.get("safety") or {},
        screenshots=raw_browser.get("screenshots") or {},
        desktop=raw_browser.get("desktop") or {},
    )

    _cache = Config(
        agents=agents_cfg,
        models=models_cfg,
        sandbox=sandbox_cfg,
        browser=browser_cfg,
        data_dir=DATA_DIR,
        isolation=_load_yaml(CONFIG_DIR / "isolation.yaml", expand_values=False),
        voice=load_voice(),
    )
    return _cache


def _browser_channel(raw: object) -> str:
    """browser.channel 只接受 Playwright 渠道名；白名单校验，拒绝任意字符串进 launch。"""
    channel = str(raw or "").strip().lower()
    if channel and not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", channel):
        raise ValueError("browser.channel 需为 Playwright 渠道名（例如 chrome），小写字母/数字/短横线")
    return channel


def _normalise_node(raw: dict) -> dict:
    """节点配置只存连接参数及密钥文件引用，不接受密码或密钥内容。"""
    node_id = str(raw.get("node_id", raw.get("id", ""))).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", node_id):
        raise ValueError("node_id 只能使用字母、数字、下划线和短横线，长度 1–64")
    allowed = {"node_id", "id", "name", "host", "port", "user", "identity_file", "known_hosts_file", "root",
               "connect_timeout", "browser", "desktop"}
    if set(raw) - allowed:
        raise ValueError(f"不支持的执行电脑配置字段：{', '.join(sorted(set(raw) - allowed))}")
    node = {"node_id": node_id, "name": str(raw.get("name") or node_id).strip(),
            "host": str(raw.get("host") or "").strip(), "user": str(raw.get("user") or "").strip(),
            "port": int(raw.get("port", 22)), "identity_file": str(raw.get("identity_file") or ""),
            "known_hosts_file": str(raw.get("known_hosts_file") or ""),
            "root": str(raw.get("root") or "~/carme-node/workspaces"),
            "connect_timeout": int(raw.get("connect_timeout", 10)),
            "browser": deepcopy(raw.get("browser") or {}), "desktop": deepcopy(raw.get("desktop") or {})}
    for key, pattern in (("host", r"[A-Za-z0-9_.:%\[\]-]+"), ("user", r"[A-Za-z_][A-Za-z0-9_.-]*")):
        if node[key] and (node[key].startswith("-") or not re.fullmatch(pattern, node[key])):
            raise ValueError(f"{key} 包含无效字符")
    if not 1 <= node["port"] <= 65535 or not 1 <= node["connect_timeout"] <= 60:
        raise ValueError("SSH 端口应在 1–65535，连接超时应在 1–60 秒")
    if any("\x00" in node[k] or "\n" in node[k] for k in ("name", "identity_file", "known_hosts_file", "root")):
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
    # 首次保存真实模型时播种默认档位，让「团队默认模型」立刻可配置：
    # 只在没有任何候选时播种，不覆盖管理员已有的档位安排。
    tiers = raw.setdefault("tiers", {})
    if not any((tiers.get(name) or {}).get("candidates") for name in tiers):
        first_ref = f"{provider.name}/{selected[0]['id']}"
        for tier_name in ("reason", "balanced", "cheap"):
            tiers.setdefault(tier_name, {})["candidates"] = [first_ref]
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


VOICE_MODES = ("browser", "server")
VOICE_SOURCES = ("connection", "custom")
VOICE_KEY_ENV = "CARME_VOICE_API_KEY"

VOICE_FILE = CONFIG_DIR / "voice.yaml"
# 写 voice.yaml 时始终保留的注释头：告诉用户可以手工编辑这个文件。
VOICE_FILE_HEADER = ("# 语音转文字（ASR）配置。密钥只保存在这个文件里：不写 .env、不进日志、不回显给前端。\n"
                     "# 可以手工编辑：非空的 base_url / model 会覆盖界面里的设置（此时按「服务端转写」使用）。\n")


def _normalise_voice(raw: Any) -> dict:
    """voice 段只保留六个字段：非字符串当空串，未知识别方式回落浏览器识别，未知来源回落已有连接。"""
    spec = raw if isinstance(raw, dict) else {}

    def text(key: str) -> str:
        value = spec.get(key)
        return value.strip() if isinstance(value, str) else ""

    mode = text("mode").lower()
    source = text("source").lower()
    # 老配置只有 mode/provider/model，读出来即 source=connection。
    return {"mode": mode if mode in VOICE_MODES else "browser",
            "source": source if source in VOICE_SOURCES else "connection",
            "provider": text("provider"), "model": text("model"),
            "base_url": text("base_url"), "api_key_env": text("api_key_env")}


def load_voice_secret() -> dict:
    """读 voice.yaml（每次都读盘，不缓存）：文件缺失、YAML 写坏或字段非字符串一律当空串，绝不抛异常。"""
    secret = {"api_key": "", "base_url": "", "model": "", "language": ""}
    try:
        raw = _load_yaml(VOICE_FILE, expand_values=False)
    except Exception:
        return secret  # 手工编辑出错不能把服务打挂。
    if not isinstance(raw, dict):
        return secret
    for key in secret:
        value = raw.get(key)
        secret[key] = value.strip() if isinstance(value, str) else ""
    return secret


def save_voice_secret(api_key: str | None = None, base_url: str = "", model: str = "",
                      language: str = "") -> dict:
    """写 voice.yaml：api_key 为 None 或空串表示不修改；base_url/model/language 传入即覆盖（空串清空）。"""
    secret = load_voice_secret()
    key = str(api_key or "").strip()
    if key:
        secret["api_key"] = key
    secret["base_url"] = str(base_url or "").strip()
    secret["model"] = str(model or "").strip()
    secret["language"] = str(language or "").strip()
    payload = yaml.safe_dump(secret, allow_unicode=True, sort_keys=False)
    atomic_write(VOICE_FILE, (VOICE_FILE_HEADER + payload).encode())
    os.chmod(VOICE_FILE, 0o600)  # atomic_write 的临时文件本就是 0600，这里再显式保证一次。
    return secret


def load_voice() -> dict:
    """语音转写设置：models.yaml 的 voice 段叠加 voice.yaml 的手工覆盖；缺省为浏览器识别。"""
    voice = _normalise_voice(_load_yaml(CONFIG_DIR / "models.yaml", expand_values=False).get("voice"))
    secret = load_voice_secret()
    if secret["base_url"]:
        voice["base_url"] = secret["base_url"]
    if secret["model"]:
        voice["model"] = secret["model"]
    # 叠加后地址与型号都非空即视为手工直连：按服务端转写处理，不再需要界面里的连接选择。
    if voice["base_url"] and voice["model"]:
        voice["mode"] = "server"
        voice["source"] = "custom"
        voice["provider"] = ""
    voice["language"] = secret["language"]
    return voice


def voice_key(voice: dict | None = None) -> str:
    """取语音转写密钥：voice.yaml 优先，其次回落 .env 里的旧变量；只用于请求头，绝不回显给前端。"""
    spec = voice if isinstance(voice, dict) else {}
    secret = load_voice_secret()["api_key"]
    if secret:
        return secret
    env_name = str(spec.get("api_key_env") or VOICE_KEY_ENV).strip() or VOICE_KEY_ENV
    return _env_value(env_name)


def _checked_voice_model(model: str) -> str:
    """转写型号是自由文本（whisper-1、FunAudioLLM/SenseVoiceSmall…），不要求在已保存模型列表里。"""
    if not model:
        raise ValueError("请填写转写型号")
    if len(model) > 200 or model.startswith("-") or any(not 33 <= ord(c) <= 126 for c in model):
        raise ValueError("转写型号只能使用可见 ASCII 字符，且不能以短横线开头")
    return model


def _unquote_env_value(value: str) -> str:
    """去掉 .env 值外层成对引号并还原反斜杠转义（本应用写 .env 时用单引号包裹密钥）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def _env_value(env_name: str) -> str:
    """读环境变量：进程环境优先，未同步时回退读 .env；只在本进程内用于请求头，不落日志。"""
    if not env_name:
        return ""
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    if not ENV_FILE.exists():
        return ""
    try:
        env_text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    for item in env_text.splitlines():
        match = re.match(rf"^\s*(?:export\s+)?{re.escape(env_name)}\s*=\s*(.*)$", item)
        if match:
            return _unquote_env_value(match.group(1).strip())
    return ""


def voice_has_key(voice: dict) -> bool:
    """语音转写是否已有可用密钥；只回传布尔值，绝不把密钥本身交给前端。"""
    spec = voice if isinstance(voice, dict) else {}
    secret = load_voice_secret()
    # custom 来源，或 voice.yaml 的手工覆盖已生效时，密钥都来自 voice.yaml / 旧的环境变量。
    if str(spec.get("source") or "connection") == "custom" or (secret["base_url"] and secret["model"]):
        return bool(voice_key(spec))
    providers = _load_yaml(CONFIG_DIR / "models.yaml", expand_values=False).get("providers") or {}
    raw = providers.get(str(spec.get("provider") or "")) if isinstance(providers, dict) else None
    if not isinstance(raw, dict):
        return False
    # 与 load() 一样构造 Provider，复用它的 available 判定（环境变量或 api_key_default）。
    return Provider(name="voice", type=str(raw.get("type", "openai")), base_url=str(raw.get("base_url", "")),
                    api_key_env=str(raw.get("api_key_env", "")),
                    api_key_default=str(raw.get("api_key_default", ""))).available


def save_voice(mode: str = "", source: str = "connection", provider: str = "", model: str = "",
               base_url: str = "", api_key: str = "") -> dict:
    """校验并写入 models.yaml 的 voice 段；密钥只写 voice.yaml，YAML 只记录变量名。失败抛 ValueError（中文消息）。"""
    mode = str(mode or "").strip().lower()
    source = str(source or "").strip().lower()
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    base_url = str(base_url or "").strip()
    api_key = str(api_key or "").strip()
    if mode not in VOICE_MODES:
        raise ValueError("识别方式无效")
    if source not in VOICE_SOURCES:
        source = "connection"
    path = CONFIG_DIR / "models.yaml"
    raw = _load_yaml(path, expand_values=False)
    api_key_env = _normalise_voice(raw.get("voice"))["api_key_env"]
    if mode == "server":
        if source == "connection":
            providers = raw.get("providers") or {}
            spec = providers.get(provider) if isinstance(providers, dict) else None
            if not isinstance(spec, dict) or str(spec.get("type", "")) not in API_TYPES:
                raise ValueError("请选择已配置的模型连接")
            model = _checked_voice_model(model)
            base_url = ""
        else:
            if (len(base_url) > 300 or any(not 33 <= ord(c) <= 126 for c in base_url)
                    or not re.fullmatch(r"https?://[^\s]+", base_url)):
                raise ValueError("请填写语音识别接口地址（http/https）")
            model = _checked_voice_model(model)
            provider = ""
    if api_key:
        api_key_env = VOICE_KEY_ENV
    elif source == "custom" and not api_key_env:
        api_key_env = VOICE_KEY_ENV
    if api_key:
        # 密钥只进 voice.yaml（0600，不再写 .env）；界面已把地址与型号写进 models.yaml，
        # 这里清空 voice.yaml 里的手工值，避免旧值反向覆盖界面设置；language 保留。
        save_voice_secret(api_key=api_key, base_url="", model="", language=load_voice_secret()["language"])
    # 只替换 voice 段，providers/models/tiers 等其它内容原样保留。
    raw["voice"] = {"mode": mode, "source": source, "provider": provider, "model": model,
                    "base_url": base_url, "api_key_env": api_key_env}
    atomic_write(path, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode())
    return load(reload=True).voice


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
