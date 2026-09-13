import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import Markdown from "react-markdown";
import BotSolid from "./BotSolid";
import {
  ArrowLeft,
  ArrowUp,
  Bell,
  Bot,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  Cpu,
  FileText,
  Globe,
  Info,
  LoaderCircle,
  MessageCircle,
  Monitor,
  MoreHorizontal,
  Paperclip,
  Plus,
  Pencil,
  Upload,
  Shuffle,
  RotateCcw,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Users,
  WifiOff,
  X,
  Pin,
  PinOff,
  FolderPlus,
  Copy,
  EyeOff,
  Trash2,
  Type,
  ArchiveRestore,
} from "lucide-react";

type Agent = {
  id: string;
  name: string;
  title?: string;
  emoji?: string;
  prompt?: string;
  summary?: string;
  tier?: string;
  model?: string;
  effort?: string;
  engine?: "api" | "codex" | "pi" | "claude";
  engine_model?: string;
  engine_effort?: string;
  engine_workspace?: string;
  avatar?: BotAvatar;
  tools?: string[];
  can_delegate?: boolean;
  entry?: boolean;
  sandbox?: string;
};
type BotAvatar = { kind?: "bot" | "image"; shape?: string; color?: string; file?: string };
type SavedModel = { ref: string; id: string; provider_id: string; provider_label: string; api_type: string; effort: string; effort_options: string[]; effort_source: string; available: boolean; verified: boolean };
type Connection = { id: string; label: string; type: string; base_url: string; has_key?: boolean; api_key?: string };
type DiscoveredModel = { id: string; name: string; effort_options: string[]; effort_source: string };
type ModelSettings = { connections: Connection[]; models: SavedModel[]; tiers?: Record<string, string[]>; allow_mock?: boolean };
type EngineInfo = { id: "api" | "codex" | "pi" | "claude"; label: string; binary?: string; installed: boolean; ready: boolean; status: string; version: string; auth_status: string; capability: string; provider?: string; model?: string };
type EngineSettings = { engines: EngineInfo[] };
function cliEffortOptions(engine: Agent["engine"] | EngineInfo["id"]) {
  if (engine === "claude") return ["low", "medium", "high", "xhigh", "max"];
  if (engine === "pi") return ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
  if (engine === "codex") return ["minimal", "low", "medium", "high", "xhigh", "max"];
  return ["minimal", "low", "medium", "high", "xhigh", "max"];
}
type Conversation = {
  id: string;
  title: string;
  agent_ids: string[];
  kind?: string;
  updated_at?: number;
  created_at?: number;
  last_message?: string;
  pinned_at?: number;
  folder?: string;
  hidden?: boolean;
  deleted_at?: number;
  unread?: boolean;
  active_agent_ids?: string[];
};
type Message = {
  id: string;
  role: string;
  content: string;
  agent_id?: string;
  created_at?: number;
  task_id?: string;
  attachments?: ChatFile[];
  model?: string;
  provider?: string;
  status?: string;
};
type ChatFile = { id: string; conversation_id: string; message_id: string; name: string; mime: string; size: number; kind: string; note: string; text?: string };
type MemoryItem = { key: string; value: string; updated_at: number };
type Node = {
  id?: string;
  node_id?: string;
  name: string;
  host?: string;
  user?: string;
  port?: number;
  identity_file?: string;
  root?: string;
  cdp_port?: number;
  browser?: { cdp_port?: number; cdp_host?: string };
  desktop?: { enabled?: boolean; vnc_port?: number };
  enabled?: boolean;
  configured?: boolean;
  status?: string;
  is_default?: boolean;
};
type Task = {
  id: string;
  agent_id: string;
  title?: string;
  status: string;
  error?: string;
  result?: string;
  node_id?: string;
  node_name?: string;
  meta?: { node_id?: string; node?: Node };
  parent_id?: string;
  engine?: string;
  engine_model?: string;
  execution_host?: string;
  engine_workspace?: string;
  cost_known?: boolean;
  tokens_known?: boolean;
};
type Approval = {
  id: string;
  status: string;
  reason?: string;
  summary?: string;
  tool_name?: string;
  action?: string;
  args?: unknown;
  arguments?: unknown;
  detail?: unknown;
};
type Detail = {
  conversation: Conversation;
  messages: Message[];
  tasks: Task[];
  approvals: Approval[];
  summary?: { content: string; model: string; updated_at: number; through_seq: number };
  files?: ChatFile[];
};
type Stats = {
  models?: {
    providers_ready?: string[];
    mock_enabled?: boolean;
    tiers?: Record<string, string[]>;
  };
  budget?: { spend_today_usd?: number; limit_usd?: number };
  runtime?: { running_tasks?: number };
};
type CloudflareStatus = {
  state: "not_installed" | "not_configured" | "domain_missing" | "access_config_required" | "carme_auth_required" | "tunnel_not_running" | "access_pending" | "unknown";
  message: string;
  next_steps: string[];
  cloudflared: { installed: boolean; version: string; source: string };
  config: {
    exists: boolean;
    valid: boolean;
    structural_valid: boolean;
    origin_target_ok: boolean;
    hostname_configured: boolean;
    hostname_mismatch: boolean;
    credentials_present: boolean;
    tunnel_named: boolean;
    fallback_present: boolean;
    access_configured: boolean;
    protocol: string;
    protocol_valid: boolean;
  };
  form: { tunnel: string; credentials_file: string; hostname: string; protocol: string; team_name: string; audience_tag: string };
  tunnel_running: boolean;
  tunnel_process: string;
  hostname: string;
  url: string;
  origin: string;
  carme_token_configured: boolean;
  access: { status: string; message: string };
  script: string;
  protocol_options: string[];
};
type Panel = "new" | "nodes" | "bot" | "market" | "settings" | "routine" | "history" | "memory" | "files" | "summary" | null;
type ChatAction = "pin" | "folder" | "unread" | "rename" | "edit" | "duplicate" | "copy" | "hide" | "delete";
type ChatDialogAction = { kind: "rename" | "folder" | "delete"; conversation: Conversation };

function readStorage(key: string) {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}
function saveStorage(key: string, value: string) {
  try {
    value ? localStorage.setItem(key, value) : localStorage.removeItem(key);
  } catch {
    /* Private browsing may restrict storage. */
  }
}
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) {
  saveStorage("carme_token", urlToken);
  const url = new URL(location.href);
  url.searchParams.delete("token");
  history.replaceState({}, "", url);
}
let token = readStorage("carme_token");

const ACCESS_LOGIN_MESSAGE = "Cloudflare Access 登录已过期或尚未完成，请重新打开受保护地址登录后再试。";
function reopenAccessEntry() {
  const url = new URL(location.href);
  url.searchParams.delete("token");
  window.location.assign(url.pathname + url.search + url.hash);
}
function isAccessLoginError(message: string) {
  return message.includes("Cloudflare Access") || message.includes("非 JSON") || message.includes("实时连接中断");
}

const FONT_CHOICES: Record<string, {label: string; css: string}> = {
  system: {label: "系统默认", css: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif'},
  pingfang: {label: "苹方", css: '"PingFang SC", "Hiragino Sans GB", sans-serif'},
  songti: {label: "宋体", css: '"Songti SC", "STSong", "SimSun", serif'},
  kaiti: {label: "楷体", css: '"Kaiti SC", "STKaiti", "KaiTi", serif'},
  mono: {label: "等宽", css: '"SFMono-Regular", Menlo, "PingFang SC", monospace'},
};
function readAppearance() {
  try {
    const value = JSON.parse(readStorage("carme_appearance"));
    return {family: Object.hasOwn(FONT_CHOICES, value.family) ? value.family as string : "system", size: Number.isInteger(value.size) && value.size >= 14 && value.size <= 22 ? value.size as number : 16};
  } catch { return {family: "system", size: 16}; }
}
function applyAppearance(value: {family: string; size: number}) {
  document.documentElement.style.setProperty("--ui-font-family", FONT_CHOICES[value.family].css);
  document.documentElement.style.setProperty("--ui-font-size", `${value.size}px`);
}
applyAppearance(readAppearance());

async function api<T>(
  path: string,
  body?: unknown,
  method = body === undefined ? "GET" : "POST",
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      redirect: "manual",
      credentials: "same-origin",
      headers: {
        ...(body !== undefined ? { "Content-Type": body instanceof Blob ? body.type : "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      ...(body !== undefined ? { body: body instanceof Blob ? body : JSON.stringify(body) } : {}),
    });
  } catch {
    throw new Error("无法读取后端响应；如果 Cloudflare Access 登录已过期，请重新打开受保护入口后再试。");
  }
  const contentType = response.headers.get("content-type") || "";
  const accessHtml = contentType.includes("text/html") || response.redirected || response.type === "opaqueredirect" || response.type === "opaque" || response.status === 0 || (response.status >= 300 && response.status < 400);
  if (accessHtml) {
    throw new Error(ACCESS_LOGIN_MESSAGE);
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch {
      /* Keep the HTTP status. */
    }
    if (response.status === 401) message = "需要访问令牌，请在设置中连接后端。";
    throw new Error(message);
  }
  if (!contentType.includes("application/json")) {
    throw new Error("后端返回了非 JSON 响应，可能是 Cloudflare Access 登录页；请重新登录入口后再试。");
  }
  return response.json();
}
function requestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
}
function nodeId(node: Node) {
  return node.node_id || node.id || "";
}
function formatTime(value?: number, full = false) {
  if (!value) return "";
  const date = new Date(value < 1e12 ? value * 1000 : value);
  return date.toLocaleString(
    "zh-CN",
    full
      ? { month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" },
  );
}
function brief(value: string) {
  return value.replace(/[#*`\n]/g, " ").trim();
}
const statusText: Record<string, string> = {
  queued: "排队中",
  running: "正在处理",
  done: "已完成",
  failed: "执行失败",
  cancelled: "已停止",
  waiting_approval: "等待确认",
  paused: "已暂停",
  truncated: "达到步数上限",
};
const isRunning = (task: Task) =>
  ["queued", "running", "waiting_approval"].includes(task.status);
function Avatar({
  agent,
  size = "",
  group = false,
  working = false,
}: {
  agent?: Agent;
  size?: string;
  group?: boolean;
  working?: boolean;
}) {
  const [source, setSource] = useState("");
  const filename = agent?.avatar?.kind === "image" ? agent.avatar.file : "";
  useEffect(() => {
    setSource("");
    if (!filename || group) return;
    let disposed = false, objectUrl = "";
    const abort = new AbortController();
    void fetch(`/api/avatars/${encodeURIComponent(filename)}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {}, signal: abort.signal,
    }).then(async (response) => {
      if (!response.ok) return;
      objectUrl = URL.createObjectURL(await response.blob());
      if (disposed) URL.revokeObjectURL(objectUrl); else setSource(objectUrl);
    }).catch(() => {});
    return () => { disposed = true; abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [filename, group, token]);
  return (
    <span className={`avatar ${size} ${group ? "group" : ""} ${!group && agent?.avatar?.kind === "bot" ? "glyph-avatar" : ""} ${working ? "avatar-working" : ""}`}
      role={working ? "img" : undefined} aria-label={working ? `${group ? "群聊成员" : agent?.name || "Bot"}正在执行任务` : undefined}>
      <span className="avatar-face">{group ? <Users size={20} /> : source ? <img src={source} alt={`${agent?.name || "Bot"}的头像`} /> : agent?.avatar?.kind === "bot" ? working ? <BotSolid shape={agent.avatar.shape} color={agent.avatar.color} /> : <BotGlyph shape={agent.avatar.shape} color={agent.avatar.color} /> : agent?.emoji || <Bot size={22} />}</span>
      {working && <span className="avatar-orbit" aria-hidden="true" />}
    </span>
  );
}

const BOT_SHAPES = ["circle", "oval", "square", "pill", "triangle", "hexagon", "cloud", "drop"];
const SHAPE_LABELS = ["圆形", "椭圆", "方形", "胶囊", "三角", "六边形", "云朵", "水滴"];
const BOT_COLORS = ["#000000", "#a77c53", "#ff385a", "#ff7c17", "#ffb333", "#00ca70", "#18bfae", "#2d8dff", "#aa79ff", "#ff55b7", "#999999"];
function BotGlyph({ shape = "circle", color = "#18bfae" }: { shape?: string; color?: string }) {
  const paths: Record<string, string> = {
    circle: "M50 8a42 42 0 1 1 0 84a42 42 0 1 1 0-84",
    oval: "M49 10C77 10 94 27 94 55C94 84 75 94 49 87C21 80 8 68 8 45C8 20 22 10 49 10",
    square: "M31 12H69Q88 12 88 31V69Q88 88 69 88H31Q12 88 12 69V31Q12 12 31 12",
    pill: "M33 21H67a30 30 0 0 1 0 60H33a30 30 0 0 1 0-60",
    triangle: "M40 13Q50-4 60 13L93 73Q103 91 81 91H19Q-3 91 7 73Z",
    hexagon: "M43 6Q50 2 57 6L84 22Q92 26 92 36V65Q92 75 84 80L58 95Q50 100 42 95L16 80Q8 75 8 65V36Q8 26 16 22Z",
    cloud: "M21 40C15 11 50 7 59 23C77 11 93 36 87 48C113 74 81 94 64 81C47 101 30 84 25 83C0 88-4 53 21 40Z",
    drop: "M50 3C44 18 12 44 12 62a38 38 0 0 0 76 0C88 43 61 17 50 3Z",
  };
  return <svg viewBox="0 0 100 100" aria-hidden="true"><path d={paths[shape] || paths.circle} fill={color} /><g fill="white" transform="rotate(-15 57 48)"><rect x="43" y="36" width="8" height="17" rx="4" /><rect x="66" y="36" width="8" height="17" rx="4" /></g></svg>;
}

function AvatarEditor({ value, onChange, onError, onBusy }: { value: BotAvatar; onChange: (value: BotAvatar) => void; onError: (message: string) => void; onBusy: (busy: boolean) => void }) {
  const [tab, setTab] = useState("bot");
  const [uploading, setUploading] = useState(false);
  const randomSet = () => Array.from({ length: 8 }, () => ({ kind: "bot" as const, shape: BOT_SHAPES[Math.floor(Math.random() * 8)], color: BOT_COLORS[Math.floor(Math.random() * BOT_COLORS.length)] }));
  const [generated, setGenerated] = useState(randomSet);
  async function upload(file?: File) {
    if (!file) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) { onError("请选择 PNG、JPEG 或 WebP 静态图片，不支持 GIF、SVG 或 HEIC。"); return; }
    if (file.size > 5 * 1024 * 1024) { onError("头像文件不能超过 5 MB。"); return; }
    setUploading(true); onBusy(true); onError("");
    try { const result = await api<{ avatar: BotAvatar }>("/avatars", file); onChange(result.avatar); }
    catch (e) { onError((e as Error).message); }
    finally { setUploading(false); onBusy(false); }
  }
  return <div className="avatar-editor">
    <div className="avatar-tabs" role="tablist" aria-label="头像来源">
      {[["bot", "Bot"], ["generate", "生成"], ["upload", "上传"]].map(([id, label]) => <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{label}</button>)}
      <button type="button" className="avatar-reset" onClick={() => { onChange({}); onError(""); }}><RotateCcw size={14} />重置</button>
    </div>
    <div className="avatar-editor-body" role="tabpanel">
      {tab === "bot" && <>
        <div className="shape-grid">{BOT_SHAPES.map((shape, index) => <button type="button" key={shape} aria-label={`${SHAPE_LABELS[index]}头像`} aria-pressed={value.kind === "bot" && value.shape === shape} onClick={() => onChange({ kind: "bot", shape, color: value.color || "#18bfae" })}><BotGlyph shape={shape} color={value.color || "#18bfae"} /></button>)}</div>
        <div className="color-grid">{BOT_COLORS.map((color) => <button type="button" key={color} aria-label={`头像颜色 ${color}`} aria-pressed={value.kind === "bot" && value.color === color} style={{ backgroundColor: color }} onClick={() => onChange({ kind: "bot", shape: value.shape || "circle", color })} />)}</div>
      </>}
      {tab === "generate" && <>
        <p className="form-help">随机组合 Bot 的形状与颜色，在本地生成。</p>
        <div className="shape-grid">{generated.map((avatar, index) => <button type="button" key={index} aria-label={`选择生成头像 ${index + 1}`} onClick={() => onChange(avatar)}><BotGlyph {...avatar} /></button>)}</div>
        <button type="button" className="secondary-button" onClick={() => setGenerated(randomSet())}><Shuffle size={16} />换一组</button>
      </>}
      {tab === "upload" && <div className="avatar-upload">
        <Upload size={28} strokeWidth={1.5} /><strong>上传你自己的头像</strong>
        <p>PNG、JPG / JPEG、WebP 静态图片，最大 5 MB。<br />宽高 32–4096 像素，建议 512×512 正方形。</p>
        <label className="secondary-button upload-label">{uploading ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}{uploading ? "正在上传…" : "选择图片"}<input type="file" aria-label="上传头像图片" accept="image/png,image/jpeg,image/webp" disabled={uploading} onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = ""; }} /></label>
        <small>居中裁切并保存为 512×512 WebP。保存 Bot 后生效。</small>
      </div>}
    </div>
  </div>;
}
function IconButton({
  label,
  children,
  onClick,
  active = false,
  className = "",
}: {
  label: string;
  children: ReactNode;
  onClick: () => void;
  active?: boolean;
  className?: string;
}) {
  return (
    <button
      className={`icon-button ${active ? "active" : ""} ${className}`}
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      {children}
    </button>
  );
}
function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className={`modal ${wide ? "wide" : ""}`}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      aria-label={title}
    >
      <div className="modal-inner">
        <header className="modal-header">
          <h2>{title}</h2>
          <IconButton label="关闭" onClick={onClose}>
            <X size={20} />
          </IconButton>
        </header>
        {children}
      </div>
    </dialog>
  );
}

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [entry, setEntry] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [current, setCurrent] = useState(readStorage("carme_conversation"));
  const [detail, setDetail] = useState<Detail | null>(null);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [defaultNode, setDefaultNode] = useState("");
  const [stats, setStats] = useState<Stats>({});
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [search, setSearch] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [previewFile, setPreviewFile] = useState<ChatFile | null>(null);
  const uploadInput = useRef<HTMLInputElement>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [showDetails, setShowDetails] = useState(true);
  const [mobileView, setMobileView] = useState<"list" | "chat" | "details">(
    "list",
  );
  const [editingAgent, setEditingAgent] = useState<Agent | null>(null);
  const [contextMenu, setContextMenu] = useState<{ conversation: Conversation; x: number; y: number } | null>(null);
  const [chatAction, setChatAction] = useState<ChatDialogAction | null>(null);
  const [authVersion, setAuthVersion] = useState(0);
  const currentRef = useRef(current);
  const pendingSend = useRef<{
    conversation: string;
    content: string;
    request_id: string;
    attachment_ids: string[];
  } | null>(null);
  const messageEnd = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  currentRef.current = current;
  const agentOf = (id?: string) => agents.find((agent) => agent.id === id);
  const conversationName = (c: Conversation) => c.agent_ids.length === 1 ? agentOf(c.agent_ids[0])?.name || c.title : c.title;
  const filteredConversations = conversations.filter((c) => `${conversationName(c)} ${c.last_message || ""}`.toLowerCase().includes(search.toLowerCase()));
  const folders = Array.from(new Set(conversations.map((c) => c.folder || "").filter(Boolean))).sort();
  const sections = [
    { id: "pinned", title: "置顶", rows: filteredConversations.filter((c) => c.pinned_at) },
    { id: "default", title: folders.length ? "未分组" : "聊天", rows: filteredConversations.filter((c) => !c.pinned_at && !c.folder) },
    ...folders.map((folder) => ({ id: `folder:${folder}`, title: folder, rows: filteredConversations.filter((c) => !c.pinned_at && c.folder === folder) })),
  ];
  const selected = detail?.conversation.id === current ? detail : null;
  const draftFiles = (selected?.files || []).filter((file) => !file.message_id && file.kind === "upload");
  const primary =
    agentOf(selected?.conversation.agent_ids[0]) || agentOf(entry);
  const group = (selected?.conversation.agent_ids.length || 0) > 1;
  const tasks = selected?.tasks || [];
  const running = tasks.filter(isRunning);
  const workingAgents = new Set(tasks.filter((task) => task.status === "running").map((task) => task.agent_id));
  const primaryWorking = group ? workingAgents.size > 0 : workingAgents.has(primary?.id || "");
  const activeTask =
    running.find((t) => !t.parent_id) || tasks.find((t) => !t.parent_id);
  const activeCliTask = activeTask?.execution_host === "backend" ? activeTask : null;
  const taskNodeId = activeTask?.node_id || activeTask?.meta?.node_id;
  const currentNode = activeTask
    ? activeTask.meta?.node ||
      (taskNodeId
        ? {
            id: taskNodeId,
            name:
              activeTask.node_name ||
              nodes.find((node) => nodeId(node) === taskNodeId)?.name ||
              taskNodeId,
          }
        : undefined)
    : nodes.find((node) => nodeId(node) === defaultNode);
  const draft = drafts[current] || "";

  const refreshList = useCallback(async () => {
    const result = await api<{ conversations: Conversation[] }>(
      "/conversations",
    );
    setConversations(result.conversations || []);
    if (currentRef.current && !result.conversations.some((c) => c.id === currentRef.current)) {
      setCurrent(""); setDetail(null);
    }
    return result.conversations || [];
  }, []);
  const refreshDetail = useCallback(async (id: string) => {
    if (!id) return;
    const result = await api<Detail>(
      `/conversations/${encodeURIComponent(id)}`,
    );
    if (currentRef.current === id) setDetail(result);
  }, []);
  const refreshNodes = useCallback(async () => {
    const result = await api<{ nodes: Node[]; default_node_id: string }>(
      "/nodes",
    );
    setNodes(result.nodes || []);
    setDefaultNode(result.default_node_id || "");
  }, []);
  const refreshAgents = useCallback(async () => {
    const result = await api<{ entry: string; agents: Agent[] }>("/agents");
    setAgents(result.agents);
    setEntry(result.entry);
  }, []);
  const sync = useCallback(async () => {
    await Promise.all([
      refreshList(),
      currentRef.current
        ? refreshDetail(currentRef.current)
        : Promise.resolve(),
    ]);
  }, [refreshList, refreshDetail]);

  useEffect(() => {
    let disposed = false;
    setLoading(true);
    const load = async () => {
      const results = await Promise.allSettled([
        refreshAgents(),
        refreshList(),
        refreshNodes(),
        api<Stats>("/stats").then(setStats),
      ]);
      if (disposed) return;
      const failed = results.find((r) => r.status === "rejected");
      if (failed?.status === "rejected") setError(failed.reason.message);
      if (results[1].status === "fulfilled") {
        const list = results[1].value;
        if (!list.some((c) => c.id === currentRef.current))
          setCurrent(list[0]?.id || "");
      }
      setLoading(false);
    };
    void load();
    let sse: EventSource | null = null;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const reconcile = () => {
      void sync().catch((e) => {
        if (!disposed) setError(e.message);
      });
    };
    const refreshConfiguration = () => {
      void Promise.all([refreshAgents(), refreshNodes(), api<Stats>("/stats").then(setStats)]).catch(() => {});
    };
    const openEvents = async () => {
      if (token) {
        try { await api("/session", {}, "POST"); } catch { /* The initial API load reports the useful auth error. */ }
      }
      if (disposed) return;
      const events = new EventSource("/api/events", { withCredentials: true });
      sse = events;
      events.onopen = () => {
        if (!disposed) {
          setConnected(true);
          reconcile();
        }
      };
      events.onerror = () => {
        if (!disposed) {
          setConnected(false);
      setError("实时连接中断；如果 Cloudflare Access 登录已过期，请重新打开受保护地址完成登录。");
        }
      };
      let configurationDirty = false;
      events.onmessage = (event) => {
        try { configurationDirty ||= ["agent.updated", "models.updated", "node.updated", "cloudflare.updated"].includes(JSON.parse(event.data).type); } catch { /* Ignore malformed notifications. */ }
        if (!refreshTimer)
          refreshTimer = setTimeout(() => {
            refreshTimer = undefined;
            reconcile();
            if (configurationDirty) { configurationDirty = false; refreshConfiguration(); }
          }, 180);
      };
    };
    void openEvents();
    const onVisible = () => {
      if (document.visibilityState === "visible") reconcile();
    };
    const poll = setInterval(onVisible, 15000);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", reconcile);
    return () => {
      disposed = true;
      sse?.close();
      clearInterval(poll);
      clearTimeout(refreshTimer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", reconcile);
    };
  }, [authVersion, refreshAgents, refreshList, refreshNodes, sync]);
  useEffect(() => {
    saveStorage("carme_conversation", current);
    setDetail(null);
    atBottom.current = true;
    if (current) void refreshDetail(current).catch((e) => setError(e.message));
  }, [current, refreshDetail]);
  useEffect(() => {
    if (atBottom.current)
      messageEnd.current?.scrollIntoView({ behavior: "instant" });
  }, [selected?.messages.length, selected?.messages.at(-1)?.content, current, selected?.tasks.length]);
  useEffect(() => {
    if (current && selected?.conversation.unread && !panel && document.visibilityState === "visible"
        && (mobileView === "chat" || window.matchMedia("(min-width: 901px)").matches)) {
      void api(`/conversations/${encodeURIComponent(current)}`, { unread: false }, "PATCH").then(refreshList).catch(() => {});
    }
  }, [current, selected?.messages.at(-1)?.id, panel, mobileView, refreshList]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 4500);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    const el = textarea.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    }
  }, [draft]);

  useEffect(() => {
    const find = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setMobileView("list");
        searchInput.current?.focus();
      }
    };
    window.addEventListener("keydown", find);
    return () => window.removeEventListener("keydown", find);
  }, []);

  function selectConversation(id: string) {
    setContextMenu(null);
    setCurrent(id);
    setMobileView("chat");
    setError("");
    if (conversations.find((c) => c.id === id)?.unread) {
      void api(`/conversations/${encodeURIComponent(id)}`, { unread: false }, "PATCH").then(refreshList).catch((e) => setError(e.message));
    }
  }
  async function createConversation(ids: string[], title?: string) {
    const result = await api<{ conversation: Conversation }>("/conversations", {
      agent_ids: ids,
      ...(title ? { title } : {}),
    });
    await refreshList();
    selectConversation(result.conversation.id);
    setPanel(null);
  }
  async function sendMessage(event?: FormEvent) {
    event?.preventDefault();
    const content = draft.trim() || (draftFiles.length ? "请查看附件。" : "");
    if (!content || !current || sending || uploading) return;
    const conversation = current;
    if (draftFiles.length > 4) { setError("每条消息最多 4 个附件，请先移除多余附件。"); return; }
    const attachment_ids = draftFiles.slice(0, 4).map((file) => file.id).sort();
    if (
      pendingSend.current?.conversation !== conversation ||
      pendingSend.current.content !== content ||
      JSON.stringify(pendingSend.current.attachment_ids) !== JSON.stringify(attachment_ids)
    )
      pendingSend.current = { conversation, content, request_id: requestId(), attachment_ids };
    const body = pendingSend.current;
    setSending(true);
    setError("");
    atBottom.current = true;
    let accepted = false;
    try {
      await api(`/conversations/${encodeURIComponent(conversation)}/messages`, {
        content,
        request_id: body.request_id,
        attachment_ids,
      });
      accepted = true;
      setDrafts((prev) => ({
        ...prev,
        [conversation]:
          prev[conversation]?.trim() === content ? "" : prev[conversation],
      }));
      pendingSend.current = null;
      await Promise.all([refreshList(), refreshDetail(conversation)]);
    } catch (e) {
      setError(
        accepted
          ? "消息已发送，暂时无法刷新进度。正在等待重新同步。"
          : (e as Error).message + " 文字已保留，可重新发送。",
      );
    } finally {
      setSending(false);
      textarea.current?.focus();
    }
  }
  async function uploadFiles(files: FileList | null) {
    if (!files || !current || uploading || sending) return;
    const cid = current;
    if (files.length + draftFiles.length > 4) { setError("每条消息最多 4 个附件，请先发送或移除已有附件。"); return; }
    setUploading(true); setError("");
    try {
      for (const file of Array.from(files)) {
        if (!file.size || file.size > 10 * 1024 * 1024) throw new Error(`${file.name}：文件需大于 0 字节且不超过 10 MB`);
        await api(`/conversations/${encodeURIComponent(cid)}/attachments?name=${encodeURIComponent(file.name)}`, file);
      }
    } catch (e) { setError((e as Error).message); }
    finally {
      setUploading(false);
      if (uploadInput.current) uploadInput.current.value = "";
      if (currentRef.current === cid) await refreshDetail(cid);
    }
  }
  async function perform(action: () => Promise<unknown>, success?: string) {
    try {
      await action();
      await sync();
      if (success) setNotice(success);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const editBot = async (agent: Agent) => {
    try {
      const result = await api<Agent | { agent: Agent }>(
        `/agents/${encodeURIComponent(agent.id)}`,
      );
      setEditingAgent({
        ...agent,
        ...("agent" in result ? result.agent : result),
      });
      setPanel("bot");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const openComputerPanel = () => {
    const cliAgent = activeCliTask ? agentOf(activeCliTask.agent_id) : undefined;
    if (cliAgent) void editBot(cliAgent);
    else setPanel("nodes");
  };

  async function changeChat(c: Conversation, changes: Record<string, unknown>) {
    await api(`/conversations/${encodeURIComponent(c.id)}`, changes, "PATCH");
    if ((changes.hidden || changes.deleted || changes.unread) && c.id === current) {
      setCurrent(""); setDetail(null); setMobileView("list");
    }
    await refreshList();
  }
  async function runChatAction(c: Conversation, action: ChatAction) {
    setContextMenu(null);
    if (action === "rename" || action === "folder" || action === "delete") {
      setChatAction({kind: action, conversation: c}); return;
    }
    if (action === "pin") await changeChat(c, { pinned: !c.pinned_at });
    if (action === "unread") await changeChat(c, { unread: !c.unread });
    if (action === "hide") { await changeChat(c, { hidden: true }); setNotice("已隐藏，可在「隐藏与最近删除」中恢复。"); }
    if (action === "copy") {
      try { await navigator.clipboard.writeText(c.id); setNotice("已复制对话 ID"); }
      catch { setNotice(`对话 ID：${c.id}`); }
    }
    const agent = agentOf(c.agent_ids[0]);
    if (action === "edit" && agent) await editBot(agent);
    if (action === "duplicate" && agent) {
      const result = await api<{agent: Agent; conversation: Conversation}>(`/agents/${encodeURIComponent(agent.id)}/duplicate`, {});
      await refreshAgents(); await refreshList(); selectConversation(result.conversation.id);
      setNotice("已创建 Bot 副本和新会话；原对话与记忆保留在原 Bot。");
    }
  }

  return (
    <div
      className={`app-shell mobile-${mobileView} ${showDetails ? "with-details" : ""}`}
    >
      <aside className="sidebar">
        <header className="brand-row">
          <button
            className="brand"
            onClick={() => {
              setMobileView("list");
            }}
            aria-label="Carme 会话列表"
          >
            <img className="brand-logo" src="/icon-192.png" alt="" />
            <strong>Carme</strong>
          </button>
          <IconButton label="新建聊天" onClick={() => setPanel("new")}>
            <Plus size={20} />
          </IconButton>
        </header>
        <label className="search-box">
          <Search size={16} />
          <input
            ref={searchInput}
            placeholder="搜索聊天"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="搜索聊天"
          />
          <kbd>⌘ K</kbd>
        </label>
        <div className="side-label">
          聊天 <span>{conversations.length || ""}</span>
        </div>
        <nav className="conversation-list" aria-label="聊天列表">
          {sections.filter((section) => section.rows.length).map((section) => <section key={section.id} aria-label={section.title}>
            {(section.id !== "default" || folders.length > 0 || conversations.some((c) => c.pinned_at)) && <h3 className="conversation-section-label">{section.id === "pinned" && <Pin size={12} />}{section.title}</h3>}
            {section.rows.map((c) => <ConversationRow key={c.id} conversation={c} name={conversationName(c)} agent={agentOf(c.agent_ids[0])} selected={current === c.id} onSelect={() => selectConversation(c.id)} onMenu={(x, y) => setContextMenu({conversation:c, x, y})} />)}
          </section>)}
          {!loading && !conversations.length && (
            <div className="side-empty">
              <MessageCircle size={26} />
              <p>你的聊天会出现在这里</p>
              <button className="text-button" onClick={() => setPanel("new")}>
                开始新聊天 <Plus size={14} />
              </button>
            </div>
          )}
          {loading && (
            <div className="loading-row">
              <LoaderCircle className="spin" size={16} />
              正在连接后端
            </div>
          )}
          {conversations.length > 0 && !filteredConversations.length && <p className="empty-search">没有找到相关聊天</p>}
        </nav>
        <footer className="sidebar-footer">
          <button className="side-action" onClick={() => setPanel("history")}><ArchiveRestore size={18} /><span>隐藏与最近删除</span></button>
          <button className="side-action" onClick={() => setPanel("market")}>
            <Globe size={18} />
            <span>探索 Bot</span>
            <ChevronRight size={15} />
          </button>
          <button className="side-action" onClick={() => setPanel("nodes")}>
            <Monitor size={18} />
            <span>执行电脑</span>
            <span className="small-count">{nodes.length}</span>
          </button>
          <div className="account-row">
            <span className="user-avatar">我</span>
            <span className="account-name">
              我的工作空间
              <small>
                <i className={`connection-dot ${connected ? "live" : ""}`} />
                {connected ? "后端已连接" : "正在重连后端"}
              </small>
            </span>
            <IconButton label="设置" onClick={() => setPanel("settings")}>
              <Settings2 size={18} />
            </IconButton>
          </div>
        </footer>
      </aside>

      <main className="chat-pane">
        <header className="chat-header">
          <IconButton
            label="返回聊天列表"
            className="mobile-only"
            onClick={() => setMobileView("list")}
          >
            <ArrowLeft size={21} />
          </IconButton>
          <button
            className="chat-heading"
            onClick={() => {
              setShowDetails(true);
              setMobileView("details");
            }}
          >
            <Avatar agent={primary} group={group} working={primaryWorking} />
            <span>
              <strong>{selected ? conversationName(selected.conversation) : "我的 Bot 团队"}</strong>
              <small>
                {group
                  ? `${selected?.conversation.agent_ids.length} 位成员`
                  : primary?.title || "把想做的事，交给你的 Bot"}
              </small>
            </span>
            <ChevronDown size={15} />
          </button>
          <div className="header-actions">
            <IconButton
              label="聊天详情"
              active={showDetails}
              onClick={() => {
                setShowDetails(!showDetails);
                setMobileView("details");
              }}
            >
              <SlidersHorizontal size={19} />
            </IconButton>
            <IconButton label="更多设置" onClick={() => setPanel("settings")}>
              <MoreHorizontal size={21} />
            </IconButton>
          </div>
        </header>
        {!connected && !loading && (
          <div className="connection-banner" role="status">
            <WifiOff size={15} />
            <span>连接中断，正在自动重连。已发送的任务仍由后端处理。</span>
            <button onClick={() => void perform(sync)}>重试</button>
          </div>
        )}
        {stats.models?.mock_enabled && (
          <div className="connection-banner warning">
            <Info size={15} />
            <span>
              已启用演示模型回退。标有 mock 的回复仅用于测试，不代表真实执行。
            </span>
            <button onClick={() => setPanel("settings")}>查看</button>
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            <Info size={16} />
            <span>{error}</span>
            {isAccessLoginError(error) && <button className="secondary-button" type="button" onClick={reopenAccessEntry}>重新打开保护入口</button>}
            <IconButton label="关闭错误提示" onClick={() => setError("")}>
              <X size={15} />
            </IconButton>
          </div>
        )}
        <div
          className="message-scroll"
          onScroll={(e) => {
            const el = e.currentTarget;
            atBottom.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 100;
          }}
        >
          {!current ? (
            <div className="welcome">
              <span className="welcome-mark">
                C<span>·</span>
              </span>
              <h1>让想法，开始行动。</h1>
              <p>和你的 Bot 聊聊，让它在你的电脑上完成工作。</p>
              <button
                className="primary-button"
                onClick={() => setPanel("new")}
              >
                <Plus size={17} />
                开始新聊天
              </button>
              <div className="welcome-members">
                {agents.slice(0, 5).map((agent) => (
                  <button
                    key={agent.id}
                    onClick={() =>
                      void perform(() => createConversation([agent.id]))
                    }
                  >
                    <Avatar agent={agent} />
                    <span>{agent.name}</span>
                    <small>{agent.title || "自定义 Bot"}</small>
                  </button>
                ))}
              </div>
            </div>
          ) : !selected ? (
            <div className="loading-row center">
              <LoaderCircle className="spin" size={20} />
              正在读取聊天
            </div>
          ) : (
            <div className="messages">
              <div className="conversation-start">
                <Avatar agent={primary} size="large" group={group} />
                <h2>{conversationName(selected.conversation)}</h2>
                <p>
                  {group
                    ? "成员在这里协作，你可以随时继续补充。"
                    : primary?.summary ||
                      primary?.title ||
                      "向 Bot 发送第一条消息，开始一起工作。"}
                </p>
                <time>
                  {formatTime(selected.conversation.created_at, true)}
                </time>
              </div>
              {selected.messages.map((message) => (
                <article
                  key={message.id}
                  className={`message ${message.role === "user" ? "outgoing" : "incoming"}`}
                >
                  {message.role !== "user" && (
                    <Avatar
                      agent={agentOf(message.agent_id) || primary}
                      size="small"
                    />
                  )}
                  <div className="message-column">
                    {message.role !== "user" && (
                      <div className="message-author">
                        {agentOf(message.agent_id)?.name ||
                          primary?.name ||
                          "Bot"}
                      </div>
                    )}
                    <div className="bubble">
                      <Markdown
                        components={{
                          a: ({ href, children }) => (
                            <a
                              href={href}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              {children}
                            </a>
                          ),
                          img: ({ alt }) => (
                            <span className="inline-image-note">
                              [图片：{alt || "附件"}]
                            </span>
                          ),
                        }}
                      >
                        {message.content}
                      </Markdown>
                      {message.attachments?.map((file) => <FileCard key={file.id} file={file} onPreview={setPreviewFile} />)}
                    </div>
                    <div className="message-meta">
                      <time>{formatTime(message.created_at)}</time>
                      {message.model && <span title={message.provider && ["codex", "pi", "claude"].includes(message.provider) ? "本次任务使用的 CLI 引擎与模型设置" : "由实际 API 响应记录；供应商可能返回模型别名"}>{message.provider} / {message.model}</span>}
                      {message.status === "streaming" && <span className="stream-state">正在回复…</span>}
                      {message.status === "interrupted" && <span className="stream-state">已中断 · 部分回复</span>}
                      {message.role === "user" && <CheckCheck size={13} />}
                    </div>
                  </div>
                </article>
              ))}
              {tasks
                .filter(
                  (t) =>
                    isRunning(t) ||
                    t.status === "failed" ||
                    t.status === "cancelled",
                )
                .map((task) => (
                  <div
                    key={task.id}
                    className={`task-activity ${task.status === "failed" ? "failed" : ""}`}
                  >
                    {isRunning(task) ? (
                      <Avatar agent={agentOf(task.agent_id)} working={task.status === "running"} />
                    ) : <span className="activity-icon"><Info size={16} /></span>}
                    <div>
                      <strong>
                        {agentOf(task.agent_id)?.name || task.agent_id}{" "}
                        <span>{statusText[task.status] || task.status}</span>
                      </strong>
                      {task.parent_id && (
                        <small>成员协作 · {task.title || "子任务"}</small>
                      )}
                      {task.execution_host === "backend" && <small>本机 CLI · 后端 Mac · {task.engine_workspace || "Bot 独立工作目录"}</small>}
                      {task.execution_host === "node" && task.node_name && <small>API 引擎 · 执行电脑：{task.node_name}</small>}
                      {task.cost_known === false && <small>费用未知（CLI 供应商未提供可核对的计费数据）</small>}
                      {task.error && <p>{task.error}</p>}
                    </div>
                    {isRunning(task) && (
                      <button
                        className="text-button"
                        onClick={() =>
                          void perform(
                            () =>
                              api(
                                `/tasks/${encodeURIComponent(task.id)}/cancel`,
                                {},
                              ),
                            "任务已请求停止",
                          )
                        }
                      >
                        <Square size={12} />
                        停止
                      </button>
                    )}
                  </div>
                ))}
              {(selected.approvals || [])
                .filter((approval) => approval.status === "pending")
                .map((approval) => (
                  <div className="approval-card" key={approval.id}>
                    <div className="approval-title">
                      <ShieldCheck size={18} />
                      <strong>这个操作需要你确认</strong>
                    </div>
                    <p>
                      {approval.reason ||
                        approval.summary ||
                        approval.action ||
                        approval.tool_name ||
                        "Bot 正在等待你的决定。"}
                    </p>
                    {approval.detail || approval.args || approval.arguments ? (
                      <pre>
                        {JSON.stringify(
                          approval.detail ||
                            approval.args ||
                            approval.arguments,
                          null,
                          2,
                        )}
                      </pre>
                    ) : null}
                    <div className="approval-actions">
                      <button
                        className="secondary-button"
                        onClick={() =>
                          void perform(() =>
                            api(
                              `/approvals/${encodeURIComponent(approval.id)}/decide`,
                              { approved: false, note: "用户在聊天中拒绝" },
                            ),
                          )
                        }
                      >
                        拒绝
                      </button>
                      <button
                        className="primary-button"
                        onClick={() =>
                          void perform(() =>
                            api(
                              `/approvals/${encodeURIComponent(approval.id)}/decide`,
                              { approved: true },
                            ),
                          )
                        }
                      >
                        <Check size={16} />
                        允许执行
                      </button>
                    </div>
                  </div>
                ))}
              <div ref={messageEnd} />
            </div>
          )}
        </div>
        <div className="composer-area">
          <form
            className={`composer ${!current ? "disabled" : ""}`}
            onSubmit={sendMessage}
          >
            <textarea
              ref={textarea}
              rows={1}
              value={draft}
              onChange={(e) =>
                setDrafts((prev) => ({ ...prev, [current]: e.target.value }))
              }
              placeholder={
                current
                  ? `发送消息给 ${group ? "群聊" : primary?.name || "Bot"}…`
                  : "新建聊天后，开始发送消息…"
              }
              disabled={!current || sending}
              aria-label="消息"
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing &&
                  !window.matchMedia("(max-width: 700px)").matches
                ) {
                  e.preventDefault();
                  void sendMessage();
                }
              }}
            />
            <div className="composer-toolbar">
              <input ref={uploadInput} type="file" multiple hidden accept=".pdf,.docx,.txt,.md,.csv,.json,.html,.png,.jpg,.jpeg,.webp" onChange={(event) => void uploadFiles(event.target.files)} />
              <IconButton
                label="添加附件"
                onClick={() => { if (!uploading && !sending && current) uploadInput.current?.click(); }}
              >
                <Paperclip size={19} />
              </IconButton>
              <span className="composer-hint">
                {uploading ? "正在上传并解析…" : running.length ? "可以继续补充要求" : "文字、文档与图片"}
              </span>
              <button
                className="send-button"
                type="submit"
                disabled={!current || (!draft.trim() && !draftFiles.length) || sending || uploading}
                aria-label="发送消息"
              >
                {sending ? (
                  <LoaderCircle className="spin" size={19} />
                ) : (
                  <ArrowUp size={21} />
                )}
              </button>
            </div>
            {draftFiles.length > 0 && <div className="draft-files">{draftFiles.map((file) => <div key={file.id}>
              <button type="button" onClick={() => setPreviewFile(file)}>{file.name}</button>
              <button type="button" disabled={sending || uploading} aria-label={`移除 ${file.name}`} onClick={() => void perform(() => api(`/conversations/${encodeURIComponent(current)}/attachments/${file.id}`, undefined, "DELETE"))}><X size={14} /></button>
            </div>)}</div>}
            <details className="upload-help"><summary>支持的附件格式与限制</summary>
              PDF（未加密，≤100 页）、DOCX、UTF-8 TXT / MD / CSV / JSON / HTML、PNG / JPEG / WebP；每个 ≤10 MB，每条消息 ≤4 个。文本最多 50 万字符。图片边长 ≤8192 px、总像素 ≤2400 万，处理为最长边 2048 px 的 JPEG，动图仅首帧。扫描 PDF 未做 OCR；图片理解需要视觉模型。发送后附件内容会随请求交给所选模型。
            </details>
          </form>
          <div className="composer-footnote">
            Carme 可能会出错，请核实重要信息。
          </div>
        </div>
      </main>

      <aside className="details-pane">
        <header className="details-header">
          <IconButton
            label="返回聊天"
            className="mobile-only"
            onClick={() => setMobileView("chat")}
          >
            <ArrowLeft size={21} />
          </IconButton>
          <h2>聊天详情</h2>
          <IconButton
            label="收起详情"
            onClick={() => {
              setShowDetails(false);
              setMobileView("chat");
            }}
          >
            <X size={18} />
          </IconButton>
        </header>
        <div className="details-scroll">
          <div className="profile-card">
            <Avatar agent={primary} size="hero" group={group} working={primaryWorking} />
            <h2>{selected ? conversationName(selected.conversation) : "我的 Bot 团队"}</h2>
            <span className="profile-label">
              {group ? "群聊" : primary?.title || "专属工作伙伴"}
            </span>
          </div>
          <section className="detail-section">
            <div className="section-heading">
              <h3>Bot 的电脑</h3>
              <button className="text-button" onClick={openComputerPanel}>
                管理
                <ChevronRight size={13} />
              </button>
            </div>
            <button className="computer-card" onClick={openComputerPanel}>
              <div className="computer-placeholder">
                <Monitor size={33} strokeWidth={1.2} />
                <strong>{activeCliTask ? "后端 Mac（CLI）" : currentNode?.name || "尚未连接执行电脑"}</strong>
                <span>
                  {activeCliTask ? activeCliTask.engine_workspace || "Bot 独立工作目录" : currentNode ? "桌面画面尚未接入" : "连接 Bot 背后的 MacBook"}
                </span>
              </div>
              <div className="computer-card-footer">
                <span className="connection-dot" />
                <span>{activeCliTask ? "本机 CLI 工作目录" : currentNode ? "桌面未连接" : "等待配置"}</span>
                <ChevronRight size={14} />
              </div>
            </button>
            <p className="detail-note">
              {activeCliTask
                ? `当前任务固定使用后端 Mac 的 CLI 工作目录：${activeCliTask.engine_workspace || "Bot 独立目录"}。`
                : activeTask
                ? `当前任务${currentNode ? `固定使用 ${currentNode.name}` : "未绑定执行电脑"}。`
                : "执行电脑暂定为 2018 MacBook，可随时更换。"}{" "}
              {activeCliTask ? " CLI 工作目录修改从下一次任务开始生效。" : " 更换默认电脑只影响新任务。"}
            </p>
          </section>
          <section className="detail-section">
            <h3>成员</h3>
            {(selected?.conversation.agent_ids || (entry ? [entry] : [])).map(
              (id) => (
                <button
                  key={id}
                  className="member-row"
                  onClick={() => {
                    const agent = agentOf(id);
                    if (agent) void editBot(agent);
                  }}
                >
                  <Avatar agent={agentOf(id)} size="small" working={workingAgents.has(id)} />
                  <span>
                    <strong>{agentOf(id)?.name || id}</strong>
                    <small>{agentOf(id)?.title || "Bot"}</small>
                  </span>
                  <ChevronRight size={15} />
                </button>
              ),
            )}
          </section>
          <section className="detail-section settings-list">
            <button onClick={() => setPanel("memory")}><FileText size={17} /><span>记忆管理</span><ChevronRight size={15} /></button>
            <button disabled={!current} onClick={() => setPanel("summary")}><MessageCircle size={17} /><span>长对话摘要</span><ChevronRight size={15} /></button>
            <button disabled={!current} onClick={() => setPanel("files")}><Paperclip size={17} /><span>附件与成果</span><ChevronRight size={15} /></button>
            <button
              onClick={() => {
                if (primary) void editBot(primary);
                else {
                  setEditingAgent(null);
                  setPanel("bot");
                }
              }}
            >
              <Settings2 size={17} />
              <span>Bot 设置</span>
              <ChevronRight size={15} />
            </button>
            <button onClick={() => setPanel("routine")}>
              <Clock3 size={17} />
              <span>例行任务</span>
              <span className="subtle-tag">待开放</span>
            </button>
            <button onClick={() => setPanel("settings")}>
              <Bell size={17} />
              <span>通知</span>
              <ChevronRight size={15} />
            </button>
          </section>
          <div className="details-bottom">
            <ShieldCheck size={16} />
            <p>Bot 配置、聊天与归档文件保存在常驻后端。换电脑，聊天还在。</p>
          </div>
        </div>
      </aside>

      {panel === "new" && (
        <NewChat
          agents={agents}
          onCreate={createConversation}
          onClose={() => setPanel(null)}
          onNewBot={() => {
            setEditingAgent(null);
            setPanel("bot");
          }}
        />
      )}
      {panel === "nodes" && (
        <NodesDialog
          nodes={nodes}
          defaultNode={defaultNode}
          refresh={refreshNodes}
          onClose={() => setPanel(null)}
        />
      )}
      {panel === "bot" && (
        <BotDialog
          agent={editingAgent}
          stats={stats}
          onClose={() => setPanel(null)}
          onSaved={async () => {
            await refreshAgents();
            setPanel(null);
            setNotice("Bot 设置已保存");
          }}
        />
      )}
      {panel === "memory" && <MemoryDialog agents={agents} initialId={primary?.id || entry} onClose={() => setPanel(null)} />}
      {panel === "summary" && <Modal title="长对话摘要" onClose={() => setPanel(null)}><div className="modal-body">
        <p className="detail-note">对话超过 40 条或约 2.4 万字符时自动压缩较早内容，保留近期消息。原始聊天不会删除，切换模型会沿用摘要。摘要可能遗漏细节，请核对重要事实。</p>
        {selected?.summary ? <><small>{selected.summary.model} · {new Date(selected.summary.updated_at * 1000).toLocaleString()}</small><div className="document-preview"><Markdown>{selected.summary.content}</Markdown></div></> : <p>当前会话尚未生成摘要。</p>}
      </div></Modal>}
      {panel === "files" && <Modal title="附件与成果" onClose={() => setPanel(null)}><div className="modal-body archive-list">
        <p className="detail-note">文件保存在后端，换执行电脑不会丢失。可请 Bot 将结果保存为 TXT、Markdown、CSV、JSON 或 HTML 文件。每次生成保留独立版本。</p>
        {(selected?.files || []).map((file) => <FileCard key={file.id} file={file} onPreview={setPreviewFile} />)}
        {!selected?.files?.length && <p>尚无文件。可从聊天输入框添加附件。</p>}
      </div></Modal>}
      {previewFile && <FilePreview file={previewFile} onClose={() => setPreviewFile(null)} />}
      {panel === "market" && (
        <Modal title="探索 Bot" onClose={() => setPanel(null)} wide>
          <div className="modal-body">
            <div className="modal-intro">
              <Globe size={26} />
              <h3>你的工作伙伴</h3>
              <p>当前工作空间中真实可用的 Bot。选择一位，开启聊天。</p>
            </div>
            <div className="bot-grid">
              {agents.map((agent) => (
                <button
                  className="bot-tile"
                  key={agent.id}
                  onClick={() =>
                    void perform(() => createConversation([agent.id]))
                  }
                >
                  <Avatar agent={agent} />
                  <strong>{agent.name}</strong>
                  <span>{agent.title}</span>
                  <p>{agent.summary || "已配置的自定义 Bot"}</p>
                  <span className="text-button">
                    开始聊天 <ChevronRight size={14} />
                  </span>
                </button>
              ))}
            </div>
            <div className="info-box">
              <Info size={16} />
              <p>
                第三方技能与模板市场尚未接入。你可以创建自己的
                Bot，指定角色、模型和可用工具。
              </p>
            </div>
            <button
              className="secondary-button"
              onClick={() => {
                setEditingAgent(null);
                setPanel("bot");
              }}
            >
              <Plus size={16} />
              创建 Bot
            </button>
          </div>
        </Modal>
      )}
      {panel === "settings" && (
        <SettingsDialog
          stats={stats}
          connected={connected}
          onModelsSaved={() => { void api<Stats>("/stats").then(setStats); }}
          onClose={() => setPanel(null)}
          onConnect={(value) => {
            token = value;
            saveStorage("carme_token", value);
            void api("/session", {}, "POST").catch(() => {});
            setError("");
            setAuthVersion((v) => v + 1);
            setPanel(null);
          }}
        />
      )}
      {contextMenu && <ChatContextMenu menu={contextMenu} onClose={() => setContextMenu(null)} onAction={(action) => void perform(() => runChatAction(contextMenu.conversation, action))} />}
      {chatAction && <ChatActionDialog key={`${chatAction.kind}:${chatAction.conversation.id}`} action={chatAction} name={conversationName(chatAction.conversation)} folders={folders} onClose={() => setChatAction(null)} onApply={async (value) => {
        const c = chatAction.conversation;
        if (chatAction.kind === "rename") {
          if (c.agent_ids.length === 1) { await api(`/agents/${encodeURIComponent(c.agent_ids[0])}`, {name:value}, "PATCH"); await refreshAgents(); }
          else await changeChat(c, {title:value});
        } else await changeChat(c, chatAction.kind === "folder" ? {folder:value} : {deleted:true});
        setChatAction(null);
      }} />}
      {panel === "history" && <HiddenChatsDialog agents={agents} onClose={() => setPanel(null)} onRestored={refreshList} />}
      {panel === "routine" && (
        <Modal title="例行任务" onClose={() => setPanel(null)}>
          <div className="modal-body">
            <div className="feature-empty">
              <Clock3 size={36} strokeWidth={1.2} />
              <h3>让 Bot 按时开始工作</h3>
              <p>
                例行任务尚未开放。后续可设置重复指令、运行时间，并在这里查看每次运行结果。
              </p>
              <span className="subtle-tag">尚未接入调度器</span>
            </div>
          </div>
        </Modal>
      )}
      {notice && (
        <div className="toast" role="status">
          <Info size={17} />
          {notice}
          <button aria-label="关闭提示" onClick={() => setNotice("")}>
            <X size={16} />
          </button>
        </div>
      )}
    </div>
  );
}

function NewChat({
  agents,
  onCreate,
  onClose,
  onNewBot,
}: {
  agents: Agent[];
  onCreate: (ids: string[], title?: string) => Promise<void>;
  onClose: () => void;
  onNewBot: () => void;
}) {
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function create(ids: string[]) {
    setBusy(true);
    try {
      await onCreate(ids, title.trim() || undefined);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={group ? "创建群聊" : "新建聊天"} onClose={onClose}>
      <div className="modal-body">
        <label className="search-box modal-search">
          <Search size={17} />
          <input
            autoFocus
            placeholder="搜索 Bot"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <div className="new-chat-actions">
          <button onClick={onNewBot}>
            <span className="action-disc">
              <Plus size={19} />
            </span>
            <strong>创建 Bot</strong>
            <ChevronRight size={17} />
          </button>
          <button
            onClick={() => {
              setGroup(!group);
              setSelected([]);
            }}
          >
            <span className="action-disc">
              <Users size={19} />
            </span>
            <strong>{group ? "切换为单聊" : "创建群聊"}</strong>
            <ChevronRight size={17} />
          </button>
        </div>
        {group && (
          <label className="form-label">
            群聊名称
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="例如：我的研究小组"
              maxLength={120}
            />
          </label>
        )}
        <p className="small-section-label">
          {group ? "选择成员（至少 2 位）" : "成员"}
        </p>
        <div className="picker-list">
          {agents
            .filter((agent) => `${agent.name} ${agent.title}`.includes(search))
            .map((agent) => (
              <button
                disabled={busy}
                key={agent.id}
                onClick={() =>
                  group
                    ? setSelected((previous) =>
                        previous.includes(agent.id)
                          ? previous.filter((id) => id !== agent.id)
                          : [...previous, agent.id],
                      )
                    : void create([agent.id])
                }
              >
                <Avatar agent={agent} />
                <span>
                  <strong>{agent.name}</strong>
                  <small>{agent.title || agent.id}</small>
                </span>
                {group ? (
                  <span
                    className={`selection-check ${selected.includes(agent.id) ? "checked" : ""}`}
                  >
                    {selected.includes(agent.id) && <Check size={14} />}
                  </span>
                ) : (
                  <ChevronRight size={16} />
                )}
              </button>
            ))}
        </div>
        {!agents.length && (
          <p className="muted">尚未加载到 Bot，请检查后端连接。</p>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {group && (
          <button
            className="primary-button full-width"
            disabled={selected.length < 2 || busy}
            onClick={() => void create(selected)}
          >
            {busy ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Users size={16} />
            )}
            创建群聊{selected.length > 0 ? `（${selected.length}）` : ""}
          </button>
        )}
      </div>
    </Modal>
  );
}

function NodesDialog({
  nodes,
  defaultNode,
  refresh,
  onClose,
}: {
  nodes: Node[];
  defaultNode: string;
  refresh: () => Promise<void>;
  onClose: () => void;
}) {
  const [editing, setEditing] = useState<Node | null>(null);
  const [newNode, setNewNode] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [probe, setProbe] = useState<Record<string, unknown>>({});
  useEffect(() => {
    void refresh().catch((e) => setError(e.message));
  }, [refresh]);
  async function mutate(id: string, action: () => Promise<unknown>) {
    setBusy(id);
    setError("");
    try {
      await action();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  return (
    <Modal
      title={editing ? "编辑执行电脑" : newNode ? "添加执行电脑" : "执行电脑"}
      onClose={onClose}
      wide
    >
      <div className="modal-body">
        {editing || newNode ? (
          <NodeForm
            node={editing}
            onCancel={() => {
              setEditing(null);
              setNewNode(false);
            }}
            onSave={async (data) => {
              const { id, cdp_port, enabled: _, ...config } = data;
              await api(
                `/nodes${editing ? `/${encodeURIComponent(nodeId(editing))}` : ""}`,
                {
                  ...config,
                  node_id: id,
                  browser: { cdp_port, cdp_host: "127.0.0.1" },
                },
                editing ? "PATCH" : "POST",
              );
              await refresh();
              setEditing(null);
              setNewNode(false);
            }}
          />
        ) : (
          <>
            <div className="info-box">
              <Monitor size={20} />
              <p>
                这里是 <strong>Bot 背后的执行电脑</strong>，目前暂定 2018
                MacBook。常驻后端和你手中的 iPhone / Mac
                客户端分别承担其他职责。
              </p>
            </div>
            <div className="node-list">
              {nodes.map((node) => (
                <div className="node-card" key={nodeId(node)}>
                  <div className="node-head">
                    <span className="node-icon">
                      <Monitor size={23} />
                    </span>
                    <div>
                      <strong>{node.name}</strong>
                      <small>
                        {node.user && node.host
                          ? `${node.user}@${node.host}`
                          : "SSH 尚未配置"}
                      </small>
                    </div>
                    {nodeId(node) === defaultNode && (
                      <span className="tag">默认</span>
                    )}
                  </div>
                  <div className="node-status">
                    <span className="connection-dot" />
                    {node.configured === false ? "待配置" : "连接状态待检查"}
                    <span>桌面尚未接入</span>
                  </div>
                  <div className="node-actions">
                    <button
                      className="secondary-button"
                      disabled={!!busy}
                      onClick={() => setEditing(node)}
                    >
                      编辑
                    </button>
                    <button
                      className="secondary-button"
                      disabled={!!busy}
                      onClick={() =>
                        void mutate(nodeId(node), async () => {
                          const result = await api(
                            `/nodes/${encodeURIComponent(nodeId(node))}/probe`,
                            {},
                          );
                          setProbe((prev) => ({
                            ...prev,
                            [nodeId(node)]: result,
                          }));
                        })
                      }
                    >
                      {busy === nodeId(node) ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        <RefreshCw size={14} />
                      )}
                      检查连接
                    </button>
                    {nodeId(node) !== defaultNode && (
                      <button
                        className="secondary-button"
                        disabled={!!busy}
                        onClick={() =>
                          void mutate(nodeId(node), () =>
                            api(
                              `/nodes/${encodeURIComponent(nodeId(node))}/default`,
                              {},
                            ),
                          )
                        }
                      >
                        设为默认
                      </button>
                    )}
                  </div>
                  {probe[nodeId(node)] !== undefined && (
                    <pre className="probe-result">
                      {JSON.stringify(probe[nodeId(node)], null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
            {!nodes.length && (
              <div className="feature-empty compact">
                <Monitor size={33} strokeWidth={1.2} />
                <h3>连接第一台执行电脑</h3>
                <p>
                  添加 SSH 连接信息。Bot
                  执行命令与浏览器操作时，将使用这台电脑。
                </p>
              </div>
            )}
            <button className="primary-button" onClick={() => setNewNode(true)}>
              <Plus size={16} />
              添加执行电脑
            </button>
            <p className="form-help">
              新任务使用默认电脑；正在运行的任务继续使用原电脑。更换电脑不迁移网站登录态或正在运行的程序。
            </p>
            {error && (
              <p className="form-error" role="alert">
                {error}
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
function NodeForm({
  node,
  onSave,
  onCancel,
}: {
  node: Node | null;
  onSave: (data: Node) => Promise<void>;
  onCancel: () => void;
}) {
  const [data, setData] = useState<Node>({
    id: node ? nodeId(node) : "",
    name: node?.name || "MacBook 2018",
    host: node?.host || "",
    user: node?.user || "",
    port: node?.port || 22,
    identity_file: node?.identity_file || "",
    root: node?.root || "~/carme-workspace",
    cdp_port: node?.browser?.cdp_port || node?.cdp_port || 9222,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const field = (key: keyof Node, value: string | number | boolean) =>
    setData((prev) => ({ ...prev, [key]: value }));
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSave(data);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="settings-form" onSubmit={submit}>
      <div className="form-grid">
        <label className="form-label">
          电脑名称
          <input
            required
            value={data.name}
            onChange={(e) => field("name", e.target.value)}
          />
        </label>
        <label className="form-label">
          固定标识
          <input
            required
            disabled={!!node}
            pattern="[a-zA-Z0-9_-]+"
            placeholder="macbook-2018"
            value={data.id}
            onChange={(e) => field("id", e.target.value)}
          />
        </label>
        <label className="form-label">
          SSH 地址
          <input
            required
            placeholder="192.168.1.20"
            autoCapitalize="off"
            autoCorrect="off"
            value={data.host}
            onChange={(e) => field("host", e.target.value)}
          />
        </label>
        <label className="form-label">
          SSH 用户
          <input
            required
            placeholder="macbook"
            autoCapitalize="off"
            autoCorrect="off"
            value={data.user}
            onChange={(e) => field("user", e.target.value)}
          />
        </label>
        <label className="form-label">
          SSH 端口
          <input
            required
            type="number"
            min={1}
            max={65535}
            value={data.port}
            onChange={(e) => field("port", Number(e.target.value))}
          />
        </label>
        <label className="form-label">
          Chrome CDP 端口
          <input
            required
            type="number"
            min={1}
            max={65535}
            value={data.cdp_port}
            onChange={(e) => field("cdp_port", Number(e.target.value))}
          />
        </label>
      </div>
      <label className="form-label">
        SSH 密钥文件路径
        <input
          placeholder="~/.ssh/id_ed25519"
          autoCapitalize="off"
          value={data.identity_file}
          onChange={(e) => field("identity_file", e.target.value)}
        />
        <small>
          引用后端 Mac 上已有的文件路径，留空使用 SSH
          默认密钥。不要粘贴私钥内容。
        </small>
      </label>
      <label className="form-label">
        执行电脑工作目录
        <input
          required
          value={data.root}
          onChange={(e) => field("root", e.target.value)}
        />
      </label>
      <div className="info-box">
        <Info size={16} />
        <p>
          电脑需要先开启远程登录并完成密钥配置。保存后可检查 SSH 与 Chrome
          连接；桌面查看和接管尚未开放。
        </p>
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="form-actions">
        <button className="secondary-button" type="button" onClick={onCancel}>
          返回
        </button>
        <button className="primary-button" type="submit" disabled={busy}>
          {busy && <LoaderCircle size={16} className="spin" />}保存电脑
        </button>
      </div>
    </form>
  );
}
async function fileBlob(file: ChatFile) {
  let response: Response;
  try {
    response = await fetch(`/api/conversations/${encodeURIComponent(file.conversation_id)}/attachments/${file.id}/download`, {
      redirect: "manual",
      credentials: "same-origin",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
  } catch {
    throw new Error("无法读取附件；如果 Cloudflare Access 登录已过期，请重新打开受保护入口后再试。");
  }
  const contentType = response.headers.get("content-type") || "";
  if (response.redirected || response.type === "opaqueredirect" || response.type === "opaque" || response.status === 0 || (response.status >= 300 && response.status < 400) || contentType.includes("text/html")) {
    throw new Error(ACCESS_LOGIN_MESSAGE);
  }
  if (!response.ok) throw new Error(`读取文件失败（${response.status}）`);
  return new Blob([await response.arrayBuffer()], { type: file.mime });
}

function FileCard({ file, onPreview }: { file: ChatFile; onPreview: (file: ChatFile) => void }) {
  return <button type="button" className="file-card" onClick={() => onPreview(file)}>
    <FileText size={22} /><span><strong>{file.name}</strong><small>{file.kind === "artifact" ? "已归档成果" : file.message_id ? "附件" : "待发送"} · {file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`} · 预览 / 下载</small></span><ChevronRight size={16} />
  </button>;
}

function FilePreview({ file, onClose }: { file: ChatFile; onClose: () => void }) {
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true, objectUrl = "";
    setLoading(true); setError(""); setText(""); setUrl("");
    void (async () => {
      try {
        if (file.mime.startsWith("image/")) {
          objectUrl = URL.createObjectURL(await fileBlob(file));
          if (alive) setUrl(objectUrl); else URL.revokeObjectURL(objectUrl);
        } else {
          const result = await api<{ file: ChatFile }>(`/conversations/${encodeURIComponent(file.conversation_id)}/attachments/${file.id}`);
          if (alive) setText(result.file.text || "未提取到文字，请下载原文件查看。");
        }
      } catch (e) { if (alive) setError((e as Error).message); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [file.id, file.conversation_id, file.mime]);
  async function download() {
    setBusy(true); setError("");
    try {
      const objectUrl = URL.createObjectURL(await fileBlob(file));
      const link = document.createElement("a"); link.href = objectUrl; link.download = file.name;
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <Modal title={file.name} onClose={onClose} wide><div className="modal-body">
    <p className="detail-note">{file.note || "已保存到后端。"}</p>
    <button type="button" className="primary-button" disabled={busy} onClick={() => void download()}>{busy ? "准备下载…" : "下载文件"}</button>
    {error && <div className="form-error" role="alert"><span>{error}</span>{isAccessLoginError(error) && <button className="text-button" type="button" onClick={reopenAccessEntry}>重新打开保护入口</button>}</div>}
    {loading ? <p>正在读取…</p> : url ? <img className="attachment-image" src={url} alt={file.name} /> :
      file.mime === "text/html" ? <><p className="detail-note">安全预览：脚本、外部资源及表单已禁用。</p><iframe title="HTML 成果预览" className="html-preview" sandbox="" srcDoc={'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; form-action \'none\'; base-uri \'none\'">' + text} /></> :
      <pre className="document-preview">{text}</pre>}
  </div></Modal>;
}

function MemoryDialog({ agents, initialId, onClose }: { agents: Agent[]; initialId: string; onClose: () => void }) {
  const [scope, setScope] = useState(initialId);
  const [rows, setRows] = useState<MemoryItem[]>([]);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState("");
  useEffect(() => {
    let alive = true;
    setRows([]); setError(""); setNotice(""); setKey(""); setValue(""); setEditing(false); setDeleting(""); setBusy(true);
    void api<{ memory: MemoryItem[] }>(`/memory/${encodeURIComponent(scope)}`).then((result) => { if (alive) setRows(result.memory); })
      .catch((e) => { if (alive) setError(e.message); }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [scope]);
  async function change(method: string, item?: MemoryItem, shared = false) {
    setBusy(true); setError(""); setNotice("");
    try {
      const target = shared ? "__shared__" : scope;
      const result = await api<{ memory: MemoryItem[] }>(`/memory/${encodeURIComponent(target)}${method === "DELETE" ? `?key=${encodeURIComponent(item!.key)}` : ""}`,
        method === "DELETE" ? undefined : { key: item?.key || key.trim(), value: item?.value || value.trim() }, method);
      if (!shared) { setRows(result.memory); setKey(""); setValue(""); setEditing(false); setDeleting(""); }
      setNotice(shared ? "已复制到团队共享记忆；同名条目以当前内容为准。" : "记忆已更新，将在后续任务中使用。");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <Modal title="记忆管理" onClose={onClose}><div className="modal-body settings-form">
    <label className="form-label">记忆范围<select aria-label="记忆范围" value={scope} disabled={busy} onChange={(e) => setScope(e.target.value)}>
      {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name} · 私有</option>)}<option value="__shared__">团队共享 · 所有 Bot 可读</option>
    </select></label>
    <p className="detail-note">原有记忆保留在所属 Bot 的私有区。主 Bot 的私有记忆不会自动传给其他成员。共享内容需明确保存到团队共享区；保存或复制会覆盖该范围内的同名条目。切换模型不清空记忆、角色或聊天。</p>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <div className="memory-list">{rows.map((item) => <article key={item.key}>
      <strong>{item.key}</strong><p>{item.value}</p><div className="memory-actions">
        <button type="button" disabled={busy} onClick={() => { setKey(item.key); setValue(item.value); setEditing(true); }}>编辑</button>
        {scope !== "__shared__" && <button type="button" disabled={busy} onClick={() => void change("PUT", item, true)}>复制到团队共享</button>}
        <button type="button" disabled={busy} onClick={() => deleting === item.key ? void change("DELETE", item) : setDeleting(item.key)}>{deleting === item.key ? "确认删除此记忆" : "删除"}</button>
        {deleting === item.key && <button type="button" onClick={() => setDeleting("")}>取消删除</button>}
      </div>
    </article>)}</div>
    {!rows.length && <p>{busy ? "正在读取…" : "这个范围还没有记忆。"}</p>}
    <h3>{editing ? "编辑记忆" : "新增记忆"}</h3>
    <label className="form-label">名称<input aria-label="记忆名称" value={key} disabled={editing || busy} maxLength={160} onChange={(e) => setKey(e.target.value)} /></label>
    <label className="form-label">内容<textarea aria-label="记忆内容" value={value} disabled={busy} maxLength={20000} rows={4} onChange={(e) => setValue(e.target.value)} /></label>
    <button type="button" className="primary-button" disabled={busy || !key.trim() || !value.trim()} onClick={() => void change("PUT")}>保存记忆</button>
    {editing && <button type="button" disabled={busy} onClick={() => { setEditing(false); setKey(""); setValue(""); }}>取消编辑</button>}
  </div></Modal>;
}

function BotDialog({
  agent,
  stats,
  onClose,
  onSaved,
}: {
  agent: Agent | null;
  stats: Stats;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [data, setData] = useState<Agent>({
    id: agent?.id || "",
    name: agent?.name || "",
    emoji: agent?.emoji || "🤖",
    title: agent?.title || "",
    prompt: agent?.prompt || "",
    tier: agent?.tier || "balanced",
    model: agent?.model || "",
    effort: agent?.effort || "",
    engine: agent?.engine || "api",
    engine_model: agent?.engine_model || "",
    engine_effort: agent?.engine_effort || "",
    engine_workspace: agent?.engine_workspace || "",
    avatar: agent?.avatar || {},
    can_delegate: agent?.can_delegate || false,
    entry: agent?.entry || false,
    sandbox: agent?.sandbox || "remote",
  });
  const [tools, setTools] = useState((agent?.tools || []).join(", "));
  const [models, setModels] = useState<SavedModel[]>([]);
  const [engines, setEngines] = useState<EngineInfo[]>([]);
  const [avatarOpen, setAvatarOpen] = useState(false);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    void Promise.all([api<ModelSettings>("/models"), api<EngineSettings>("/engines")])
      .then(([modelResult, engineResult]) => { setModels(modelResult.models); setEngines(engineResult.engines); })
      .catch((e) => setError(e.message));
  }, []);
  const selectedModel = models.find((model) => model.ref === data.model);
  const savedCliEngine = data.engine && data.engine !== "api" ? data.engine : null;
  const engineOptions: EngineInfo[] = engines.length ? engines : [
    { id: "api", label: "Carme API 网关", installed: true, ready: true, status: "ready", version: "内置", auth_status: "configured", capability: "Carme 工具与现有模型配置" },
    ...(savedCliEngine ? [{ id: savedCliEngine, label: `${savedCliEngine} CLI（检测中）`, installed: true, ready: false, status: "detected", version: "", auth_status: "unknown", capability: "" }] : []),
  ];
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(
        `/agents${agent ? `/${encodeURIComponent(agent.id)}` : ""}`,
        {
          ...data,
          tools: tools
            .split(/[,，\n]/)
            .map((v) => v.trim())
            .filter(Boolean),
        },
        agent ? "PATCH" : "POST",
      );
      await onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (memoryOpen && agent) return <MemoryDialog agents={[agent]} initialId={agent.id} onClose={() => setMemoryOpen(false)} />;
  return (
    <Modal title={agent ? "Bot 设置" : "创建 Bot"} onClose={onClose}>
      <form className="modal-body settings-form" onSubmit={submit}>
        {agent && <button type="button" className="secondary-button" onClick={() => setMemoryOpen(true)}>管理此 Bot 的记忆</button>}
        <div className="bot-form-heading">
          <button type="button" className="avatar-edit-button" aria-label="编辑 Bot 头像" aria-expanded={avatarOpen} onClick={() => setAvatarOpen(!avatarOpen)}><Avatar agent={data} size="hero" /><span className="avatar-pencil"><Pencil size={22} /></span></button>
          {!avatarOpen && <button type="button" className="text-button" onClick={() => setAvatarOpen(true)}>更换头像</button>}
        </div>
        {avatarOpen && <AvatarEditor value={data.avatar || {}} onChange={(avatar) => setData((previous) => ({ ...previous, avatar }))} onError={setError} onBusy={setAvatarBusy} />}
        <div className="form-grid">
          <label className="form-label">
            名称
            <input
              required
              value={data.name}
              onChange={(e) => setData({ ...data, name: e.target.value })}
            />
          </label>
          <label className="form-label">
            默认 Emoji（重置后使用）
            <input
              value={data.emoji}
              maxLength={12}
              onChange={(e) => setData({ ...data, emoji: e.target.value })}
            />
          </label>
        </div>
        <label className="form-label">
          固定标识
          <input
            required
            disabled={!!agent}
            pattern="[a-zA-Z0-9_-]+"
            placeholder="researcher"
            value={data.id}
            onChange={(e) => setData({ ...data, id: e.target.value })}
          />
        </label>
        <label className="form-label">
          角色描述
          <input
            value={data.title}
            placeholder="例如：研究员，帮我找到可靠的答案"
            onChange={(e) => setData({ ...data, title: e.target.value })}
          />
        </label>
        <label className="form-label">
          角色指令
          <textarea
            rows={5}
            required
            placeholder="描述它擅长什么，以及应该如何完成工作…"
            value={data.prompt}
            onChange={(e) => setData({ ...data, prompt: e.target.value })}
          />
        </label>
        <label className="form-label">
          使用引擎
          <select disabled={!engines.length} value={data.engine} onChange={(e) => setData({ ...data, engine: e.target.value as Agent["engine"] })}>
            {engineOptions.map((engine) => <option key={engine.id} value={engine.id} disabled={engine.id !== "api" && !engine.installed}>{engine.label}{engine.id !== "api" && !engine.installed ? "（未发现）" : ""}</option>)}
          </select>
          <small>{data.engine === "api" ? "使用 Carme API 网关、原有模型设置和执行电脑。" : "使用后端 Mac 的本机 CLI；任务工作目录和当前引擎会在任务创建时快照。"}</small>
        </label>
        {data.engine !== "api" && <div className="engine-config-card">
          <label className="form-label">
            CLI 模型（可选）
            <input value={data.engine_model} maxLength={200} autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder="留空使用 CLI 默认模型" onChange={(e) => setData({ ...data, engine_model: e.target.value })} />
            <small>这里填写 CLI 自己支持的模型名或别名，不会改变 Carme API 模型列表。</small>
          </label>
          <label className="form-label">
            后端 Mac 工作目录（可选）
            <input value={data.engine_workspace} maxLength={500} autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder={`留空使用此 Bot 的独立工作目录（${data.id || "bot"}）`} onChange={(e) => setData({ ...data, engine_workspace: e.target.value })} />
            <small>空值按 Bot 隔离目录创建。CLI 原生终端和文件工具只在后端 Mac 上运行；不会使用 2018 MacBook 的远程工作目录。</small>
          </label>
        </div>}
        <label className="form-label">
          使用的模型
          <select disabled={data.engine !== "api"} value={data.model} onChange={(e) => setData({ ...data, model: e.target.value, effort: "" })}>
            <option value="">按模型档位自动选择</option>
            {data.model && !models.some((model) => model.ref === data.model) && <option value={data.model}>{data.model}（原配置）</option>}
            {models.map((model) => <option key={model.ref} value={model.ref} disabled={!model.available}>{model.provider_label} · {model.id}{!model.available ? "（未连接）" : ""}</option>)}
          </select>
          <small>{data.engine === "api" ? "可在「设置 → 模型连接」中添加连接并验证模型。切换引擎不会删除此前 API 模型设置。" : "当前引擎不使用 Carme API 模型；此前 API 模型设置会保留，切回 API 后继续可用。"}</small>
        </label>
        <div className="form-grid">
          <label className="form-label">
            模型档位
            <select
              disabled={!!data.model || data.engine !== "api"}
              value={data.tier}
              onChange={(e) => setData({ ...data, tier: e.target.value })}
            >
              {Array.from(
                new Set([
                  ...Object.keys(stats.models?.tiers || {}),
                  data.tier || "balanced",
                ]),
              ).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="form-label">
            推理强度 effort
            <select value={data.effort} disabled={data.engine !== "api" || !data.model} onChange={(e) => setData({ ...data, effort: e.target.value })}>
              <option value="">连接默认{selectedModel?.effort ? `（${selectedModel.effort}）` : "（由模型决定）"}</option>
              {selectedModel?.effort_options.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            </select>
          </label>
        </div>
        {data.engine !== "api" && <label className="form-label">
          CLI 推理强度
          <select value={data.engine_effort} onChange={(e) => setData({ ...data, engine_effort: e.target.value })}>
            <option value="">CLI 默认</option>
            {cliEffortOptions(data.engine).map((level) => <option key={level} value={level}>{level}</option>)}
          </select>
          <small>具体可用级别由所选 CLI 和模型决定；调用失败会明确返回原始能力错误，不会静默切换 API 模型。</small>
        </label>}
        <p className="form-help">切换模型从下一次任务开始生效。当前 Bot 的聊天记录、长期记忆和角色指令会保留；不同模型的上下文容量可能不同。</p>
        <label className="form-label">
          可用工具
          <input
            value={tools}
            placeholder="web_search, shell, read_file"
            onChange={(e) => setTools(e.target.value)}
          />
          <small>
            填写额外工具名称，以逗号分隔。会话附件读取和成果生成始终可用；无效工具名称会由后端拒绝。
          </small>
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={data.can_delegate}
            onChange={(e) =>
              setData({ ...data, can_delegate: e.target.checked })
            }
          />
          允许委派给其他成员
        </label>
        <p className="form-help">
          API 密钥由模型连接设置保存到后端。API 引擎的执行命令、文件与浏览器操作使用任务固定的执行电脑；CLI 引擎使用后端 Mac 的独立工作目录，Carme 记忆、附件、成果与委派通过受控任务桥接。
        </p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="primary-button" disabled={busy || avatarBusy}>
            {busy && <LoaderCircle size={16} className="spin" />}
            {agent ? "保存修改" : "创建 Bot"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
function SettingsDialog({
  stats,
  connected,
  onClose,
  onConnect,
  onModelsSaved,
}: {
  stats: Stats;
  connected: boolean;
  onClose: () => void;
  onConnect: (token: string) => void;
  onModelsSaved: () => void;
}) {
  const [value, setValue] = useState(token);
  return (
    <Modal title="设置" onClose={onClose} wide>
      <div className="modal-body settings-form">
        <CloudflareSettings />
        <AppearanceSettings />
        <div className="settings-section">
          <h3>
            <Globe size={17} />
            后端连接
          </h3>
          <div className="status-line">
            <i className={`connection-dot ${connected ? "live" : ""}`} />
            {connected ? "已连接" : "等待连接"}
            <span>{location.host}</span>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              onConnect(value.trim());
            }}
          >
            <label className="form-label">
              访问令牌
              <input
                type="password"
                value={value}
                autoComplete="off"
                placeholder="后端设置的 CARME_TOKEN"
                onChange={(e) => setValue(e.target.value)}
              />
            </label>
            <button type="submit" className="secondary-button">
              保存并连接
            </button>
          </form>
        </div>
        <div className="settings-section">
          <h3>
            <Cpu size={17} />
            模型
          </h3>
          <LocalEngines />
          <ModelConnections onSaved={onModelsSaved} />
          {stats.models?.mock_enabled && (
            <p className="form-help">
              演示模型已启用，不代表真实模型链路通过。
            </p>
          )}
          <p className="form-help">
            对话和必要的工具输出会发送给你配置的云端模型。
          </p>
        </div>
        <div className="settings-section">
          <h3>
            <Bell size={17} />
            通知
          </h3>
          <p className="muted">
            Web Push
            尚未接入。当前在应用打开时实时同步进度，关闭页面后仍可再次进入查看任务结果。
          </p>
        </div>
        <div className="settings-section">
          <h3>
            <Monitor size={17} />在 iPhone 与 Mac 上使用
          </h3>
          <p className="muted">
            iPhone：Safari 中打开，选择「分享 →
            添加到主屏幕」。Mac：使用浏览器访问同一个后端地址。
          </p>
          <p className="form-help">
            主屏幕离线外壳与推送能力需要安全的 HTTPS 连接。
            {!window.isSecureContext
              ? "当前连接不是安全上下文，暂不能注册离线应用。"
              : ""}
          </p>
        </div>
        <div className="settings-section">
          <h3>
            <CircleHelp size={17} />
            关于 Carme
          </h3>
          <p className="muted">
            Bot 的电脑暂定为 2018 MacBook，可以替换。后端保存
            Bot、会话和任务；客户端负责聊天与查看。
          </p>
        </div>
      </div>
    </Modal>
  );
}

const CLOUDFLARE_STATE_LABELS: Record<CloudflareStatus["state"], string> = {
  not_installed: "未安装 cloudflared",
  not_configured: "命名隧道未配置",
  domain_missing: "固定域名未完成",
  access_config_required: "需要配置 Access JWT 校验",
  carme_auth_required: "需要启用 Carme 鉴权",
  tunnel_not_running: "隧道未运行",
  access_pending: "等待 Access 与手机验收",
  unknown: "状态未知",
};

function DeploymentCheck({ value }: { value: boolean | null | undefined }) {
  if (value === true) return <Check size={15} className="deployment-check ok" aria-label="已完成" />;
  if (value === false) return <X size={15} className="deployment-check no" aria-label="未完成" />;
  return <CircleHelp size={15} className="deployment-check unknown" aria-label="未知" />;
}

function CloudflareSettings() {
  const [status, setStatus] = useState<CloudflareStatus | null>(null);
  const [form, setForm] = useState<CloudflareStatus["form"]>({
    tunnel: "",
    credentials_file: "",
    hostname: "",
    protocol: "http2",
    team_name: "",
    audience_tag: "",
  });
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const result = await api<CloudflareStatus>("/cloudflare");
      setStatus(result);
      setForm((previous) => ({ ...previous, ...result.form }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const pageHttps = location.protocol === "https:" && window.isSecureContext;
  const update = (key: keyof CloudflareStatus["form"], value: string) => setForm((previous) => ({ ...previous, [key]: value }));
  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setNotice(`${label}已复制。`);
    } catch {
      setNotice(`复制失败，请手动选择${label}。`);
    }
  };
  async function save(e: FormEvent) {
    e.preventDefault();
    setSaving(true); setError(""); setNotice("");
    try {
      const result = await api<CloudflareStatus>("/cloudflare/config", form, "PUT");
      setStatus(result); setForm((previous) => ({ ...previous, ...result.form }));
      setNotice("Cloudflare 本地配置已保存；凭据文件、Carme 重启、Access 账号和隧道连接仍需实际完成。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  const command = (value: string, label: string) => (
    <div className="deployment-command" key={value}>
      <code>{value}</code>
      <button className="text-button" type="button" onClick={() => void copy(value, label)}><Copy size={13} />复制</button>
    </div>
  );

  return (
    <div className="settings-section cloudflare-settings">
      <h3><Globe size={17} />Cloudflare Tunnel · iPhone / Mac 访问</h3>
      <div className="cloudflare-summary">
        <div className="status-line">
          <i className={`connection-dot ${status?.tunnel_running ? "live" : ""}`} />
          <strong>{status ? CLOUDFLARE_STATE_LABELS[status.state] : "正在检查"}</strong>
          <button className="text-button" type="button" onClick={() => void refresh()} disabled={busy}><RefreshCw size={13} className={busy ? "spin" : ""} />重新检查</button>
        </div>
        <p>{status?.message || "正在读取本机 cloudflared、命名隧道和鉴权配置。"}</p>
      </div>
      <div className="deployment-checks">
        <div><DeploymentCheck value={status?.cloudflared.installed} /><span>cloudflared</span><small>{status?.cloudflared.installed ? status.cloudflared.version || status.cloudflared.source : "未安装"}</small></div>
        <div><DeploymentCheck value={status?.config.origin_target_ok} /><span>Origin → 127.0.0.1:8899</span><small>{status?.config.origin_target_ok ? "目标正确" : "未确认"}</small></div>
        <div><DeploymentCheck value={status?.config.access_configured} /><span>Cloudflare Access JWT</span><small>{status?.config.access_configured ? "已写入，待登录验证" : "未配置"}</small></div>
        <div><DeploymentCheck value={status?.carme_token_configured} /><span>Carme CARME_TOKEN</span><small>{status?.carme_token_configured ? "已配置" : "未配置"}</small></div>
        <div><DeploymentCheck value={status?.tunnel_running} /><span>隧道连接进程</span><small>{status?.tunnel_process || "未知"}</small></div>
        <div><DeploymentCheck value={pageHttps} /><span>当前页面 HTTPS</span><small>{pageHttps ? "安全上下文" : "当前不是 HTTPS"}</small></div>
      </div>
      {status?.url && <div className="deployment-share">
        <label className="form-label">计划访问地址（未代表已上线）</label>
        <div className="copy-field">
          <input readOnly value={status.url} aria-label="Cloudflare Carme 访问地址" />
          <button className="secondary-button" type="button" onClick={() => void copy(status.url, "访问地址")}><Copy size={15} />复制</button>
        </div>
        <small>地址不包含 CARME_TOKEN；Access 登录和 Carme 令牌都通过独立鉴权完成。</small>
      </div>}
      {error && <div className="form-error" role="alert"><span>{error}</span>{isAccessLoginError(error) && <button className="text-button" type="button" onClick={reopenAccessEntry}>重新打开保护入口</button>}</div>}
      <form className="cloudflare-config-form" onSubmit={(e) => void save(e)}>
        <div className="form-grid">
          <label className="form-label">命名隧道名称或 UUID<input required value={form.tunnel} onChange={(e) => update("tunnel", e.target.value)} placeholder="例如 carme" autoCapitalize="off" /></label>
          <label className="form-label">固定域名<input required value={form.hostname} onChange={(e) => update("hostname", e.target.value)} placeholder="carme.example.com" autoCapitalize="off" /></label>
        </div>
        <label className="form-label">credentials-file 路径<input required value={form.credentials_file} onChange={(e) => update("credentials_file", e.target.value)} placeholder="~/.cloudflared/<tunnel-uuid>.json" autoCapitalize="off" /><small>只填写后端已有凭据文件的路径，不粘贴 JSON 或令牌。</small></label>
        <div className="form-grid">
          <label className="form-label">连接协议<select value={form.protocol} onChange={(e) => update("protocol", e.target.value)}><option value="http2">HTTP/2（优先验证 TCP 7844）</option><option value="quic">QUIC（UDP 7844）</option><option value="auto">自动（实际日志决定）</option></select></label>
          <label className="form-label">Access Team Name<input required value={form.team_name} onChange={(e) => update("team_name", e.target.value)} placeholder="你的 Cloudflare Access team" autoCapitalize="off" /></label>
        </div>
        <label className="form-label">Access Application Audience Tag<input required value={form.audience_tag} onChange={(e) => update("audience_tag", e.target.value)} placeholder="Access 应用的 AUD tag" autoCapitalize="off" /><small>这是 Access 应用标识，不是登录密码；允许哪些邮箱/账号仍在 Cloudflare Dashboard 配置。</small></label>
        <div className="form-actions">
          <button className="secondary-button" type="button" onClick={() => void refresh()} disabled={busy}>重新读取</button>
          <button className="primary-button" type="submit" disabled={saving || busy}>{saving && <LoaderCircle size={16} className="spin" />}保存本地配置</button>
        </div>
      </form>
      <details className="deployment-guide" open={status?.state !== "access_pending"}>
        <summary>上线步骤、协议诊断与 iPhone 安装</summary>
        <div className="deployment-guide-body">
          <ol>
            <li>在 Cloudflare 账户中准备你控制的域名，创建命名 Tunnel；本项目不购买域名、不登录账户、不创建 Tunnel。</li>
            <li>创建 Access Application，设置 Allow 规则只包含你的账号/邮箱，复制 AUD tag；这里的 `originRequest.access` 会由 cloudflared 校验 JWT，但允许用户仍需云端策略。</li>
            <li>在 Carme 运行目录设置 CARME_TOKEN 并重启现有 8899 服务；本页保存的不是密钥，也不会替你改 `.env`。</li>
            <li>运行检查和隧道，确认实际日志出现连接成功；HTTP/2 与 QUIC 分开诊断，不能把安装成功当成连接成功。</li>
            <li>用 iPhone Safari 打开计划地址，完成 Access 登录，再检查聊天、SSE 重连、附件上传下载，最后选择「分享 → 添加到主屏幕」。</li>
          </ol>
          <div className="deployment-commands">
            {command("brew install cloudflared", "安装命令")}
            {command("cloudflared tunnel login", "登录命令")}
            {command("cloudflared tunnel create carme", "创建命名隧道命令")}
            {command("cloudflared tunnel route dns carme YOUR_DOMAIN", "DNS 路由命令")}
            {command("./deploy/cloudflared/carme-tunnel.sh check", "检查命令")}
            {command("./deploy/cloudflared/carme-tunnel.sh start", "启动命令")}
            {command("./deploy/cloudflared/carme-tunnel.sh diagnose http2", "HTTP/2 诊断命令")}
            {command("./deploy/cloudflared/carme-tunnel.sh diagnose quic", "QUIC 诊断命令")}
          </div>
          <p className="form-help">命令在 WBAI 目录执行。停止时脚本只识别自己记录、且命令行同时包含 cloudflared 与当前配置路径的进程，不会用全局 kill。日志默认写入活动目录；前端 SSE 不再把 CARME_TOKEN 放进 URL，避免令牌进入隧道请求路径。</p>
          <p className="form-help">Cloudflare Access JWT 由 cloudflared 按本 ingress 的 `required/teamName/audTag` 校验；Carme 只接受自己的 Bearer/session 鉴权，不信任任意转发头或回环来源。Cloudflare 账号登录、域名、Access Allow 规则和 iPhone 安装仍必须由你本人完成。</p>
          <p className="form-help"><a href="https://one.dash.cloudflare.com/" target="_blank" rel="noreferrer">打开 Cloudflare One Dashboard</a></p>
        </div>
      </details>
      {notice && <p className="form-success">{notice}</p>}
    </div>
  );
}

const API_TYPE_LABELS: Record<string, string> = {
  openai: "OpenAI · Chat Completions",
  openai_responses: "OpenAI · Responses",
  anthropic: "Anthropic · Messages",
  openai_compatible: "其他服务 · OpenAI 兼容",
};
function LocalEngines() {
  const [engines, setEngines] = useState<EngineInfo[]>([]);
  const [models, setModels] = useState<Record<string, string>>({});
  const [efforts, setEfforts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => setEngines((await api<EngineSettings>("/engines")).engines), []);
  useEffect(() => { void refresh().catch((e) => setError(e.message)); }, [refresh]);
  async function test(engine: EngineInfo) {
    setBusy(engine.id); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; error?: string; reply?: string; model?: string }>(`/engines/${engine.id}/test`, {
        model: models[engine.id] || "", effort: efforts[engine.id] || "",
      });
      if (!result.ok) throw new Error(result.error || "CLI 测试失败");
      setNotice(`连接成功 · ${result.model || models[engine.id] || "CLI 默认模型"}（${result.reply || "OK"}）。实际任务会继续验证工具权限。`);
      await refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  }
  return <div className="local-engines">
    <div className="status-line"><span>使用这台 Mac 上已安装并登录的 CLI。每个 Bot 可独立选择，切换保留聊天、角色和记忆。</span><button type="button" className="text-button" onClick={() => void refresh().catch((e) => setError(e.message))}><RefreshCw size={13} />重新检测</button></div>
    <div className="engine-list">{engines.map((engine) => <article className="engine-card" key={engine.id}>
      <div className="engine-card-head"><div><strong>{engine.label}</strong><span className={`engine-status ${engine.status}`}>{engine.id === "api" ? "已配置" : engine.status === "ready" ? "已发现且已登录" : engine.status === "detected" ? "已发现，待验证" : engine.status === "not_configured" ? "未配置" : "未发现"}</span></div><small>{engine.version || engine.binary || ""}</small></div>
      <p>{engine.id === "api" ? engine.capability : `认证：${engine.auth_status === "logged_in" ? "已登录" : engine.auth_status === "unknown" ? "未能确认" : "需要登录"} · 实际任务会按 Bot 权限验证本机文件能力和 Carme 工具。`}</p>
      {engine.id !== "api" && engine.installed && <div className="engine-test-fields"><input aria-label={`${engine.label} 模型`} placeholder="CLI 默认模型" value={models[engine.id] || ""} onChange={(e) => setModels({ ...models, [engine.id]: e.target.value })} /><select aria-label={`${engine.label} effort`} value={efforts[engine.id] || ""} onChange={(e) => setEfforts({ ...efforts, [engine.id]: e.target.value })}><option value="">默认 effort</option>{cliEffortOptions(engine.id).map((level) => <option value={level} key={level}>{level}</option>)}</select></div>}
      {engine.id !== "api" && <button type="button" className="secondary-button" disabled={!engine.installed || busy === engine.id} onClick={() => void test(engine)}>{busy === engine.id && <LoaderCircle size={15} className="spin" />}{busy === engine.id ? "正在测试…" : "测试连接"}</button>}
    </article>)}</div>
    {notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
function ModelConnections({ onSaved }: { onSaved: () => void }) {
  const [settings, setSettings] = useState<ModelSettings>({ connections: [], models: [] });
  const [editing, setEditing] = useState<Connection | null>(null);
  const [removing, setRemoving] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => setSettings(await api<ModelSettings>("/models")), []);
  useEffect(() => { void refresh().catch((e) => setError(e.message)); }, [refresh]);
  async function removeModels(providerId: string, modelId: string) {
    setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; message?: string; error?: string }>(
        `/models/connections/${encodeURIComponent(providerId)}/models/delete`, { refs: [modelId] });
      if (!result.ok) throw new Error(result.error || "移除失败");
      await refresh(); onSaved();
      setNotice(result.message || "已移除模型。");
    } catch (e) { setError((e as Error).message); } finally { setRemoving(""); }
  }
  async function removeConnection(providerId: string) {
    setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; message?: string; error?: string }>(
        `/models/connections/${encodeURIComponent(providerId)}`, undefined, "DELETE");
      if (!result.ok) throw new Error(result.error || "删除失败");
      await refresh(); onSaved();
      setNotice(result.message || "连接已删除。");
    } catch (e) { setError((e as Error).message); } finally { setRemoving(""); }
  }
  if (editing) return <ModelConnectionForm key={editing.id} connection={editing} savedModels={settings.models.filter((model) => model.provider_id === editing.id)} onCancel={() => setEditing(null)} onSaved={async () => { await refresh(); onSaved(); setEditing(null); setNotice("连接与模型已保存，可在每个 Bot 的设置中选择。"); }} />;
  return <div className="model-connections">
    {settings.tiers && <ModelRoutingSettings key={JSON.stringify([settings.tiers,settings.allow_mock])} settings={settings} onSaved={async () => {await refresh(); onSaved(); setNotice("团队默认模型已保存，单独指定模型的 Bot 保持原设置。");}} />}
    <p className="form-help">添加 API 连接，测试后选择模型和推理强度。各 Bot 可独立选择已配置的模型。</p>
    {settings.connections.map((connection) => {
      const connectionModels = settings.models.filter((model) => model.provider_id === connection.id);
      const modelKey = (model: SavedModel) => `model:${connection.id}/${model.ref}`;
      const connKey = `conn:${connection.id}`;
      return <div className="model-connection-card" key={connection.id}>
        <div>
          <strong>{connection.label}</strong><span>{API_TYPE_LABELS[connection.type] || connection.type}</span><small>{connection.base_url}</small>
          <p>{connection.has_key ? "密钥已保存" : "尚未配置密钥"} · {connectionModels.length} 个模型</p>
          {connectionModels.length > 0 && <ul className="connection-model-list">
            {connectionModels.map((model) => <li key={model.ref}>
              <span>{model.id}<small>{model.effort ? ` · effort ${model.effort}` : " · 模型默认 effort"}{model.verified ? " · 已验证" : ""}{!model.available ? " · 不可用" : ""}</small></span>
              <button type="button" className="text-button" disabled={!!removing}
                onClick={() => removing === modelKey(model) ? void removeModels(connection.id, model.id) : setRemoving(modelKey(model))}>
                {removing === modelKey(model) ? "确认移除" : "移除"}</button>
            </li>)}
          </ul>}
        </div>
        <div className="model-connection-actions">
          <button type="button" className="secondary-button" onClick={() => { setEditing(connection); setNotice(""); }}>编辑</button>
          <button type="button" className="text-button danger-text" disabled={!!removing}
            onClick={() => removing === connKey ? void removeConnection(connection.id) : setRemoving(connKey)}>
            {removing === connKey ? `确认删除（含 ${connectionModels.length} 个模型）` : "删除连接"}</button>
        </div>
      </div>;
    })}
    <button className="secondary-button" type="button" onClick={() => { setEditing({ id: `api_${requestId().slice(0, 12)}`, label: "", type: "openai", base_url: "https://api.openai.com/v1" }); setNotice(""); }}><Plus size={16} />添加模型连接</button>
    {notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
function ModelConnectionForm({ connection, savedModels, onCancel, onSaved }: { connection: Connection; savedModels: SavedModel[]; onCancel: () => void; onSaved: () => Promise<void> }) {
  const [data, setData] = useState({ ...connection, api_key: "" });
  const [probeId, setProbeId] = useState("");
  const [models, setModels] = useState<DiscoveredModel[]>([]);
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const payload = () => ({ id: data.id, label: data.label.trim(), type: data.type, base_url: data.base_url.trim(), ...(data.api_key ? { api_key: data.api_key } : {}) });
  const change = (field: keyof Connection, value: string) => { setData((previous) => ({ ...previous, [field]: value })); setProbeId(""); setModels([]); setError(""); setNotice(""); };
  async function test() {
    if (!data.label.trim()) { setError("请填写连接名称。"); return; }
    setBusy("test"); setError(""); setNotice(""); setProbeId(""); setModels([]);
    try {
      const result = await api<{ ok: boolean; error?: string; message: string; probe_id: string; models: DiscoveredModel[] }>("/models/connections/test", payload());
      if (!result.ok) throw new Error(result.error || "连接失败");
      setProbeId(result.probe_id); setModels(result.models); setNotice(result.message);
      setSelected(Object.fromEntries(savedModels.filter((saved) => result.models.some((model) => model.id === saved.id)).map((saved) => [saved.id, result.models.find((model) => model.id === saved.id)?.effort_options.includes(saved.effort) ? saved.effort : ""])));
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function detect(model: DiscoveredModel) {
    setBusy(model.id); setError("");
    try {
      const result = await api<{ ok: boolean; error?: string; effort_options: string[]; effort_source: string; note: string }>("/models/connections/efforts", { ...payload(), probe_id: probeId, model_id: model.id });
      if (!result.ok) throw new Error(result.error || "无法检测 effort");
      setModels((previous) => previous.map((item) => item.id === model.id ? { ...item, effort_options: result.effort_options, effort_source: result.effort_source } : item));
      setSelected((previous) => model.id in previous && previous[model.id] && !result.effort_options.includes(previous[model.id]) ? { ...previous, [model.id]: "" } : previous);
      setNotice(result.note);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy("save"); setError("");
    try {
      const result = await api<{ ok: boolean; error?: string }>("/models/connections", { ...payload(), probe_id: probeId, models: Object.entries(selected).map(([id, effort]) => ({ id, effort })) });
      if (!result.ok) throw new Error(result.error || "保存失败");
      setData((previous) => ({ ...previous, api_key: "" }));
      await onSaved();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  return <form className="settings-form connection-form" onSubmit={save}>
    <div className="connection-form-heading"><strong>{connection.has_key ? "编辑模型连接" : "添加模型连接"}</strong><button type="button" className="text-button" disabled={!!busy} onClick={onCancel}>返回列表</button></div>
    <fieldset disabled={!!busy} className="connection-fields">
      <div className="form-grid">
        <label className="form-label">连接名称<input required placeholder="例如：我的 OpenAI" maxLength={80} value={data.label} onChange={(e) => change("label", e.target.value)} /></label>
        <label className="form-label">API 类型<select value={data.type} onChange={(e) => change("type", e.target.value)}>{Object.entries(API_TYPE_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
      </div>
      <label className="form-label">API 基础网址<input required type="url" autoCapitalize="off" autoCorrect="off" spellCheck={false} value={data.base_url} onChange={(e) => change("base_url", e.target.value)} placeholder={data.type === "anthropic" ? "https://api.anthropic.com" : "https://api.openai.com/v1"} /><small>{data.type === "anthropic" ? "例如 https://api.anthropic.com；也支持带 /v1 的地址。" : "填写供应商的 API 基础地址，例如 https://api.openai.com/v1。"}{data.type === "openai_compatible" ? "此选项适用于提供 OpenAI Chat Completions 兼容接口的其他服务。" : ""}</small></label>
      <label className="form-label">API key<input type="password" autoComplete="new-password" autoCapitalize="off" spellCheck={false} value={data.api_key} onChange={(e) => change("api_key", e.target.value)} placeholder={connection.has_key ? "已保存；留空保留原密钥" : "输入此 API 服务的密钥"} /><small>密钥仅保存到后端 .env，不回显，也不存入浏览器。更换网址或 API 类型后需重新输入。</small></label>
    </fieldset>
    <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void test()}>{busy === "test" ? <LoaderCircle size={16} className="spin" /> : <RefreshCw size={16} />}测试连接</button>
    {probeId && <div className="model-selection">
      <div className="form-success"><Check size={16} />连接成功 · {models.length} 个可见模型</div>
      <label className="search-box"><Search size={16} /><input aria-label="搜索可用模型" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索模型名称" /></label>
      <div className="model-options">{models.filter((model) => `${model.id} ${model.name}`.toLowerCase().includes(search.toLowerCase())).map((model) => <div className={`model-option ${model.id in selected ? "selected" : ""}`} key={model.id}>
        <label className="model-choice"><input type="checkbox" aria-label={`选择模型 ${model.id}`} checked={model.id in selected} disabled={!!busy} onChange={(e) => setSelected((previous) => { const next = { ...previous }; if (e.target.checked) next[model.id] = ""; else delete next[model.id]; return next; })} /><span><strong>{model.name}</strong>{model.name !== model.id && <small>{model.id}</small>}</span></label>
        {model.id in selected && <div className="model-effort">
          <label className="form-label">推理强度 effort<select aria-label={`${model.id} effort`} value={selected[model.id]} disabled={!!busy} onChange={(e) => setSelected({ ...selected, [model.id]: e.target.value })}><option value="">模型默认（不传 effort）</option>{model.effort_options.map((level) => <option value={level} key={level}>{level}</option>)}</select></label>
          <div><span className="form-help">{model.effort_source === "metadata" ? "支持范围来自接口信息" : model.effort_source === "verified" ? "支持范围已通过接口检测" : model.effort_source === "partial" ? "部分选项已验证，可重试完成检测" : model.effort_source === "saved" ? "保留此前验证的选项，保存时复核" : "接口未提供 effort 信息"}</span><button className="text-button" type="button" disabled={!!busy} onClick={() => void detect(model)}>{busy === model.id && <LoaderCircle size={14} className="spin" />}{busy === model.id ? "正在检测…" : "检测 effort"}</button></div>
        </div>}
      </div>)}</div>
      <p className="form-help">勾选要添加或更新的模型，最多 16 个；已有模型保留。检测 effort 和保存验证会发送少量固定测试消息，可能产生 API 费用，不发送你的聊天内容。</p>
      <button className="primary-button full-width" type="submit" disabled={!!busy || !Object.keys(selected).length || Object.keys(selected).length > 16}>{busy === "save" && <LoaderCircle size={16} className="spin" />}{busy === "save" ? "正在验证并保存…" : `验证并保存所选模型（${Object.keys(selected).length}）`}</button>
    </div>}
    {notice && <p className="form-help" role="status">{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </form>;
}

function ConversationRow({conversation: c, name, agent, selected, onSelect, onMenu}: {
  conversation: Conversation; name: string; agent?: Agent; selected: boolean;
  onSelect: () => void; onMenu: (x: number, y: number) => void;
}) {
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const origin = useRef({x:0,y:0});
  const held = useRef(false);
  const clear = () => clearTimeout(timer.current);
  useEffect(() => clear, []);
  return <div className="conversation-row" onContextMenu={(e) => {e.preventDefault(); held.current=true; clear(); onMenu(e.clientX,e.clientY);}}>
    <button className={`conversation-item ${selected ? "selected" : ""} ${c.unread ? "unread" : ""}`}
      onClick={() => {if (!held.current) onSelect(); held.current=false;}}
      onKeyDown={(e) => {if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {e.preventDefault(); const rect=e.currentTarget.getBoundingClientRect(); onMenu(rect.left+30,rect.top+30);}}}
      onPointerDown={(e) => {held.current=false; if(e.pointerType !== "touch") return; origin.current={x:e.clientX,y:e.clientY}; clear(); timer.current=setTimeout(() => {held.current=true; onMenu(origin.current.x,origin.current.y);},550);}}
      onPointerMove={(e) => {if(Math.hypot(e.clientX-origin.current.x,e.clientY-origin.current.y)>10) clear();}}
      onPointerUp={clear} onPointerCancel={clear}>
      <span className="conversation-avatar"><Avatar agent={agent} group={c.agent_ids.length>1} working={!!c.active_agent_ids?.length}/>{!!c.unread && <i className="unread-dot" aria-label="未读" />}</span>
      <span className="conversation-copy"><span className="conversation-title"><span>{name}</span><time>{formatTime(c.updated_at)}</time></span>
      <span className="conversation-preview">{brief(c.last_message || "开始聊点什么吧")}</span></span>
    </button>
    <button className="conversation-more icon-button" aria-label={`${name} 更多操作`} aria-haspopup="menu" onClick={(e) => {const r=e.currentTarget.getBoundingClientRect(); onMenu(r.right,r.bottom);}}><MoreHorizontal size={18}/></button>
  </div>;
}

function ChatContextMenu({menu, onClose, onAction}: {menu: {conversation: Conversation; x: number; y: number}; onClose: () => void; onAction: (action: ChatAction) => void}) {
  const element = useRef<HTMLDivElement>(null);
  const [position,setPosition] = useState({left:menu.x,top:menu.y});
  useLayoutEffect(() => {
    const rect=element.current!.getBoundingClientRect();
    setPosition({left:Math.max(12,Math.min(menu.x,window.innerWidth-rect.width-12)),top:Math.max(12,Math.min(menu.y,window.innerHeight-rect.height-12))});
    element.current?.querySelector('button')?.focus();
  },[menu.x,menu.y]);
  useEffect(() => {
    const dismiss=(e:PointerEvent) => {if (!element.current?.contains(e.target as globalThis.Node)) onClose();};
    const resize=() => onClose();
    document.addEventListener("pointerdown",dismiss); window.addEventListener("resize",resize);
    return () => {document.removeEventListener("pointerdown",dismiss);window.removeEventListener("resize",resize);};
  },[onClose]);
  const c=menu.conversation, single=c.agent_ids.length===1;
  const row=(action:ChatAction, label:string, icon:ReactNode, danger=false) => <button role="menuitem" className={danger?"danger-text":""} onClick={() => onAction(action)}>{icon}<span>{label}</span></button>;
  return <div ref={element} role="menu" aria-label="Bot 操作" className="chat-context-menu" style={position}
    onKeyDown={(e) => {const buttons=Array.from(element.current!.querySelectorAll('button')); const index=buttons.indexOf(document.activeElement as HTMLButtonElement);
      if(e.key==='Escape'||e.key==='Tab') {onClose(); return;}
      if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)) {e.preventDefault(); buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(index+(e.key==='ArrowDown'?1:buttons.length-1))%buttons.length]?.focus();}}}>
    {row("pin",c.pinned_at?"取消置顶":"置顶",c.pinned_at?<PinOff size={21}/>:<Pin size={21}/>)}
    {row("folder","移至新分组",<FolderPlus size={21}/>)}
    {row("unread",c.unread?"标为已读":"标为未读",<Bell size={21}/>)}
    <hr/>{row("rename",single?"重命名 Bot":"重命名群聊",<Pencil size={21}/>)}
    {single && row("edit","编辑资料",<Settings2 size={21}/>)}
    {single && row("duplicate","创建副本",<Copy size={21}/>)}
    <hr/>{row("copy","复制对话 ID",<Copy size={21}/>)}
    <hr/>{row("hide","从侧边栏隐藏",<EyeOff size={21}/>)}
    {row("delete","删除",<Trash2 size={21}/>,true)}
  </div>;
}

function ChatActionDialog({action, name, folders, onClose, onApply}: {action:ChatDialogAction; name:string; folders:string[]; onClose:()=>void; onApply:(value:string)=>Promise<void>}) {
  const [value,setValue]=useState(action.kind==='rename'?name:'');
  const [folder,setFolder]=useState('__new');
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  const title=action.kind==='rename'?(action.conversation.agent_ids.length===1?'重命名 Bot':'重命名群聊'):action.kind==='folder'?'移至分组':'删除对话';
  async function submit(e:FormEvent) {e.preventDefault();setBusy(true);setError('');try {await onApply(action.kind==='folder'&&folder!=='__new'?folder:value.trim());}catch(e){setError((e as Error).message);}finally{setBusy(false);}}
  return <Modal title={title} onClose={onClose}><form className="modal-body settings-form" onSubmit={submit}>
    {action.kind==='delete'?<p className="form-help">将「{name}」的这段对话移到最近删除。Bot 的角色、记忆和其他对话会保留，可在「隐藏与最近删除」中恢复。</p>:<>
      {action.kind==='folder'&&<label className="form-label">分组<select value={folder} onChange={(e)=>setFolder(e.target.value)}><option value="__new">创建新分组</option><option value="">移出分组</option>{folders.map((f)=><option key={f} value={f}>{f}</option>)}</select></label>}
      {(action.kind==='rename'||folder==='__new')&&<label className="form-label">{action.kind==='rename'?'名称':'新分组名称'}<input autoFocus required value={value} maxLength={action.kind==='rename'?80:60} onChange={(e)=>setValue(e.target.value)}/></label>}
      {action.kind==='rename'&&<p className="form-help">修改名称会保留模型、角色指令、头像、聊天和记忆。</p>}
    </>}
    {error&&<p className="form-error" role="alert">{error}</p>}
    <div className="form-actions"><button type="button" className="secondary-button" onClick={onClose} disabled={busy}>取消</button><button type="submit" className={`primary-button ${action.kind==='delete'?'danger-button':''}`} disabled={busy || (action.kind!=='delete'&&(action.kind==='rename'||folder==='__new')&&!value.trim())}>{busy?<LoaderCircle size={16} className="spin"/>:action.kind==='delete'?'移到最近删除':'保存'}</button></div>
  </form></Modal>;
}

function HiddenChatsDialog({agents,onClose,onRestored}: {agents:Agent[];onClose:()=>void;onRestored:()=>Promise<unknown>}) {
  const [view,setView]=useState<'hidden'|'deleted'>('hidden'),[rows,setRows]=useState<Conversation[]>([]),[busy,setBusy]=useState(''),[error,setError]=useState('');
  const refresh=useCallback(async()=>{const result=await api<{conversations:Conversation[]}>(`/conversations?view=${view}`);setRows(result.conversations);},[view]);
  useEffect(()=>{setRows([]);void refresh().catch((e)=>setError(e.message));},[refresh]);
  return <Modal title="隐藏与最近删除" onClose={onClose}><div className="modal-body settings-form">
    <div className="history-tabs" role="tablist" aria-label="恢复聊天">{(['hidden','deleted'] as const).map((v)=><button key={v} role="tab" aria-selected={view===v} onClick={()=>setView(v)}>{v==='hidden'?'已隐藏':'最近删除'}</button>)}</div>
    {rows.map((c)=><div className="history-chat" key={c.id}><Avatar agent={agents.find((a)=>a.id===c.agent_ids[0])} group={c.agent_ids.length>1}/><div><strong>{c.agent_ids.length===1?agents.find((a)=>a.id===c.agent_ids[0])?.name||c.title:c.title}</strong><small>{brief(c.last_message||'暂无消息')}</small></div><button className="secondary-button" disabled={!!busy} onClick={async()=>{setBusy(c.id);setError('');try{await api(`/conversations/${c.id}`,view==='hidden'?{hidden:false}:{deleted:false},'PATCH');await refresh();await onRestored();}catch(e){setError((e as Error).message);}finally{setBusy('');}}}>{busy===c.id?'恢复中…':'恢复'}</button></div>)}
    {!rows.length&&<p className="muted">{view==='hidden'?'没有隐藏的聊天':'最近删除为空'}</p>}
    {error&&<p className="form-error" role="alert">{error}</p>}
    <p className="form-help">恢复后重新出现在侧边栏，聊天记录、任务和 Bot 记忆保持不变。</p>
  </div></Modal>;
}

function AppearanceSettings() {
  const [value,setValue]=useState(readAppearance);
  const update=(next:typeof value)=>{setValue(next);applyAppearance(next);saveStorage('carme_appearance',JSON.stringify(next));};
  return <div className="settings-section"><h3><Type size={17}/>外观</h3>
    <div className="form-grid"><label className="form-label">界面字体<select value={value.family} onChange={(e)=>update({...value,family:e.target.value})}>{Object.entries(FONT_CHOICES).map(([id,font])=><option key={id} value={id}>{font.label}</option>)}</select></label>
    <label className="form-label">基准字号 <output>{value.size} px</output><input type="range" aria-label="界面字号" min="14" max="22" step="1" value={value.size} onChange={(e)=>update({...value,size:Number(e.target.value)})}/></label></div>
    <p className="appearance-preview">Carme · 让 Bot 帮你处理日常工作。<br/>文字大小、菜单和聊天将同步调整。</p>
    <div className="appearance-help"><small>立即生效，保存在此设备。未安装的字体会使用系统替代字体。</small><button className="text-button" type="button" onClick={()=>update({family:'system',size:16})}>恢复默认</button></div>
  </div>;
}

function ModelRoutingSettings({settings,onSaved}:{settings:ModelSettings;onSaved:()=>Promise<void>}) {
  const [tiers,setTiers]=useState(settings.tiers||{}),[mock,setMock]=useState(!!settings.allow_mock),[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[error,setError]=useState('');
  return <form className="model-routing settings-form" onSubmit={async(e)=>{e.preventDefault();setBusy(true);setError('');setNotice('');try{await api('/models/routing',{tiers,allow_mock:mock},'PATCH');setNotice('团队默认模型已保存。');await onSaved();}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>
    <strong>团队默认模型</strong><p className="form-help">未单独指定模型的 Bot 使用对应档位。已经单独选择模型的 Bot 保持原设置。</p>
    {Object.entries(tiers).map(([tier,refs])=><label className="form-label" key={tier}>{({balanced:'标准',cheap:'轻量',reason:'推理'} as Record<string,string>)[tier]||tier}档位<select aria-label={`${tier} 默认模型`} value={refs[0]||''} onChange={(e)=>setTiers({...tiers,[tier]:[e.target.value]})}><option value="" disabled>请选择已配置模型</option>{settings.models.filter((m)=>m.available||refs.includes(m.ref)).map((m)=><option key={m.ref} value={m.ref} disabled={m.api_type==='mock'&&!mock}>{m.provider_label} · {m.id}{m.api_type==='mock'?'（演示）':''}</option>)}</select>{refs.length>1&&<small>当前有 {refs.length} 个顺序候选；更改此项会将该档位设为单个模型。</small>}</label>)}
    <label className="check-label"><input type="checkbox" checked={mock} onChange={(e)=>setMock(e.target.checked)}/>允许演示模型（仅用于测试）</label>
    <button className="secondary-button" type="submit" disabled={busy}>{busy?'保存中…':'保存默认模型'}</button>
    {error&&<p className="form-error" role="alert">{error}</p>}{notice&&<p className="form-success" role="status">{notice}</p>}
  </form>;
}
