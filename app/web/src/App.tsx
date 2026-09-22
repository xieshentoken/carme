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
import { clampToScreen, dragDelta, takeRelativeStep } from "./desktopInput";
import {
  ArrowLeft,
  ArrowUp,
  ArrowDown,
  Bell,
  Download,
  Upload,
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
  Mic,
  Monitor,
  Maximize,
  MoreHorizontal,
  Package,
  Paperclip,
  Plug,
  Plus,
  Pencil,
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
  ZoomIn,
  ZoomOut,
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
  runtime_profile?: string;
  execution_target?: string;
  execution_target_id?: string;
  boundary?: { error?: string; runtime_profile?: string; execution_target?: string; runtime?: EngineInfo; policy?: { tools: string[]; network: string; visible_directories: string[]; permission_version: string; isolation_mode: string } };
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
 type ExportBundle = { carme_export?: number; kind?: string; app_version?: string; exported_at?: number; agents?: { id: string; profile?: Record<string, unknown>; memory?: unknown[]; avatar_image?: string }[]; conversations?: { title?: string; agent_ids?: string[]; messages?: unknown[] }[] };
 type ImportAgentReport = { id: string; existed: boolean; profile: string; memory: number; notes?: string[] };
 type ImportReport = { agents: ImportAgentReport[]; memory_count: number; conversations: { id: string; title: string; agent_ids: string[]; messages: number }[]; warnings: string[] };

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function fileStamp() {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}`;
}
 type ModelFailure = { id: string; effort?: string; effort_rejected?: boolean; error: string; hint?: string };
type ProviderUsage = { input: number; output: number; requests: number; known: number; last: number };
type ModelSettings = { connections: Connection[]; models: SavedModel[]; tiers?: Record<string, string[]>; allow_mock?: boolean; usage?: Record<string, ProviderUsage> };
type EngineInfo = { id: "api" | "codex" | "pi" | "claude"; label: string; binary?: string; installed: boolean; ready: boolean; status: string; version: string; auth_status: string; capability: string; provider?: string; model?: string; auth_method?: string; profile?: string; credential_ref?: string; base_url?: string };
type EngineSettings = { engines: EngineInfo[]; execution?: { control: string; broker: string; pi: string; action: string; browser?: string; mac_runner: string; worker_network: string; active_jobs: number } };
function cliEffortOptions(engine: Agent["engine"] | EngineInfo["id"]) {
  if (engine === "claude") return ["low", "medium", "high", "xhigh", "max"];
  if (engine === "pi") return ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
  if (engine === "codex") return ["minimal", "low", "medium", "high", "xhigh", "max"];
  return ["minimal", "low", "medium", "high", "xhigh", "max"];
}

/** 只列出「当前已连接」的 Agent：API 网关要有可用 key，CLI 要装好且已登录。 */
function connectedAgents(engines: EngineInfo[], current?: string): EngineInfo[] {
  const ready = engines.filter((engine) => engine.ready);
  if (!current || ready.some((engine) => engine.id === current)) return ready;
  // 已保存但暂时不可用的 Agent 仍留在列表里，避免保存时静默改配置。
  const saved = engines.find((engine) => engine.id === current);
  return saved ? [...ready, saved] : ready;
}
/** 只列出「已经配置好 API key」的模型；已保存但暂时不可用的同样保留并标注。 */
function availableModels(models: SavedModel[], current?: string): SavedModel[] {
  const usable = models.filter((model) => model.available);
  if (!current || usable.some((model) => model.ref === current)) return usable;
  const saved = models.find((model) => model.ref === current);
  return saved ? [...usable, saved] : usable;
}
/** 模型下拉按连接分组，名字和「设置 → 模型」里的连接名保持一致。 */
function modelGroups(models: SavedModel[]) {
  const groups: { label: string; items: SavedModel[] }[] = [];
  for (const model of models) {
    const group = groups.find((item) => item.label === model.provider_label);
    if (group) group.items.push(model);
    else groups.push({ label: model.provider_label, items: [model] });
  }
  return groups;
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
  known_hosts_file?: string;
  root?: string;
  cdp_port?: number;
  browser?: { cdp_port?: number; cdp_host?: string };
  desktop?: { enabled?: boolean; vnc_port?: number };
  enabled?: boolean;
  configured?: boolean;
  status?: string;
  is_default?: boolean;
};
type DesktopStatus = {
  enabled: boolean;
  control_enabled: boolean;
  available: boolean;
  screen_width: number;
  screen_height: number;
  cursor_x?: number;
  cursor_y?: number;
  pressed?: string;
  error?: string;
  platform?: string;
  // 本机桌面=local；浏览器隔离账号=browser（画面源切换到 /api/browser/screenshot）；Runner 已授权=runner（真实 Mac 屏，画面走 /api/desktop/screenshot）
  mode?: "local" | "browser" | "runner";
};

// 长按判定：按住不动这么久就当作「按住左键拖动」；手指先移动超过这个距离则不算长按。
const LONG_PRESS_MS = 450;
const LONG_PRESS_SLOP = 10;

function LocalDesktopView({ variant = "panel" }: { variant?: "panel" | "fullscreen" }) {
  const fullscreen = variant === "fullscreen";
  // 会话式查看：仅面板打开且 60 秒内有交互时轮询画面；超时自动断开，需手动重连。
  const IDLE_DISCONNECT_MS = 60_000;
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [frameUrl, setFrameUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [disconnected, setDisconnected] = useState(false);
  const frame = useRef<HTMLImageElement>(null);
  const disposed = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const inFlight = useRef(false);
  const lastMove = useRef(0);
  const lastTouchAt = useRef(0);
  const touchDown = useRef(false);
  const lastMouseSyncAt = useRef(0);
  const lastActivity = useRef(Date.now());
  const statusRef = useRef<DesktopStatus | null>(null);
  // 浏览器降级：/desktop/status 的 mode=browser 时，画面源切换到 /api/browser/screenshot。
  const [browserMode, setBrowserMode] = useState(false);
  const browserModeRef = useRef(false);
  const lastShotAt = useRef(0);
  const [immersive, setImmersive] = useState(false);
  // cursorRef：我们对「电脑真实光标位置」的认知，只由后端回读更新
  // displayRef：画面上画出来的光标点，单指拖动时由手指意图驱动
  const cursorRef = useRef({ x: 0, y: 0 });
  const overlayImg = useRef<HTMLImageElement>(null);
  const frameDot = useRef<HTMLSpanElement>(null);
  const overlayDot = useRef<HTMLSpanElement>(null);
  const dragRef = useRef<{ pointerId: number; lastX: number; lastY: number; startX: number; startY: number } | null>(null);
  const pendingRel = useRef({ dx: 0, dy: 0 });
  const displayRef = useRef({ x: 0, y: 0 });
  const moveInFlight = useRef(false);
  const touchState = useRef({ count: 0, startX: 0, startY: 0, moved: false, multi: false });
  const activePointers = useRef<Set<number>>(new Set());
  // 长按拖动：按住左键的状态，以及与位移队列串行化的按下/松开请求
  const pressTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pressRequested = useRef("");
  const releaseRequested = useRef(false);
  const pressedRef = useRef(false);
  const [holdActive, setHoldActive] = useState(false);
  const kbInput = useRef<HTMLInputElement>(null);
  const overlayKbInput = useRef<HTMLInputElement>(null);
  statusRef.current = status;

  const readStatus = useCallback(async () => {
    return api<DesktopStatus>("/desktop/status");
  }, []);

  const loadFrame = useCallback(async () => {
    const browser = browserModeRef.current;
    const since = lastShotAt.current;
    let response: Response;
    try {
      response = await fetch(`/api/${browser ? "browser" : "desktop"}/screenshot${since ? `?since=${since}` : ""}`, {
        cache: "no-store",
        redirect: "manual",
        credentials: "same-origin",
        headers: authHeaders(),
      });
    } catch {
      throw new Error(browser ? "无法读取浏览器画面；请确认 Carme 服务仍在运行。" : "无法读取本机屏幕；请确认 Carme 服务仍在运行。");
    }
    if (response.status === 304) return; // 画面未更新：保留当前帧
    const contentType = response.headers.get("content-type") || "";
    if (response.redirected || response.type === "opaqueredirect" || response.type === "opaque"
        || response.status === 0 || (response.status >= 300 && response.status < 400)
        || contentType.includes("text/html")) {
      throw new Error(ACCESS_LOGIN_MESSAGE);
    }
    if (response.status === 401) throw new Error("需要访问令牌，请在设置中连接后端。");
    if (!response.ok) {
      let message = browser ? `读取浏览器画面失败（${response.status}）` : `读取本机屏幕失败（${response.status}）`;
      try {
        const data = await response.json();
        if (typeof data.detail === "string") message = data.detail;
      } catch { /* Keep the HTTP status. */ }
      throw new Error(message);
    }
    const stamp = Number(response.headers.get("x-screenshot-mtime") || "");
    if (Number.isFinite(stamp) && stamp > 0) lastShotAt.current = stamp;
    const nextUrl = URL.createObjectURL(await response.blob());
    if (disposed.current) URL.revokeObjectURL(nextUrl);
    else setFrameUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return nextUrl;
    });
  }, []);

  const disconnect = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = undefined; }
    releaseButtonNow(); // 断开前先松开可能按住的鼠标键，别留在电脑上
    lastShotAt.current = 0; // 重连时不带旧 since，否则 304 会卡在空画面
    setFrameUrl((previous) => {
      if (previous) URL.revokeObjectURL(previous);
      return "";
    });
    setDisconnected(true);
  }, []);

  const tick = useCallback(async () => {
    if (disposed.current || inFlight.current) return;
    if (Date.now() - lastActivity.current > IDLE_DISCONNECT_MS) { disconnect(); return; }
    inFlight.current = true;
    try {
      const next = await readStatus();
      if (!disposed.current) {
        const isBrowser = next.mode === "browser";
        if (isBrowser !== browserModeRef.current) {
          // 数据源切换：清掉旧帧，避免本机桌面最后一帧冒充浏览器画面
          browserModeRef.current = isBrowser;
          lastShotAt.current = 0;
          setFrameUrl((previous) => {
            if (previous) URL.revokeObjectURL(previous);
            return "";
          });
        }
        setBrowserMode(isBrowser);
        setStatus(next);
        if (!dragRef.current && !moveInFlight.current && Date.now() - lastMouseSyncAt.current > 2500) {
          // 完全空闲时才用 status 里的真实光标跟随"电脑端自己的移动"，或纠正累积偏差；
          // 拖动中、拖动刚结束（位移还在途）都不采用，否则会把画面光标拽回去，表现为乱跳。
          const realX = Number(next.cursor_x);
          const realY = Number(next.cursor_y);
          if (Number.isFinite(realX) && Number.isFinite(realY)) {
            cursorRef.current = { x: realX, y: realY };
            if (Math.hypot(displayRef.current.x - realX, displayRef.current.y - realY) > 2) {
              displayRef.current = { ...cursorRef.current };
            }
          }
        }
        requestAnimationFrame(() => syncCursorDom());
      }
      await loadFrame();
      if (!disposed.current) setError("");
    } catch (e) {
      if (!disposed.current) setError((e as Error).message);
    } finally {
      inFlight.current = false;
      if (disposed.current) return;
      if (Date.now() - lastActivity.current > IDLE_DISCONNECT_MS) { disconnect(); return; }
      timer.current = setTimeout(() => { void tick(); }, 800);
    }
  }, [loadFrame, readStatus, disconnect]);

  const reconnect = useCallback(() => {
    setDisconnected(false);
    setError("");
    lastActivity.current = Date.now();
    void tick();
  }, [tick]);

  useEffect(() => {
    disposed.current = false;
    lastActivity.current = Date.now();
    void tick();
    return () => {
      disposed.current = true;
      if (timer.current) clearTimeout(timer.current);
      releaseButtonNow(); // 关闭面板 / 退出弹窗时也不要留下按住的键
      setFrameUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return ""; });
    };
  }, [tick]);

  // 手机锁屏、切后台或关闭页面时尽力松开：这种时候不会再有 pointerup。
  useEffect(() => {
    const onLeave = () => releaseButtonNow();
    const onVisibility = () => { if (document.visibilityState === "hidden") onLeave(); };
    window.addEventListener("pagehide", onLeave);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", onLeave);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  // React 的 onWheel 注册的是 passive 监听，preventDefault 无效；滚轮走原生非 passive 监听。
  useEffect(() => {
    const targets = [frame.current, immersive ? overlayImg.current : null].filter(
      (element): element is HTMLImageElement => Boolean(element),
    );
    if (!targets.length || !frameUrl) return;
    const handler = (event: WheelEvent) => {
      if (browserModeRef.current) return; // 浏览器画面只读：不劫持滚轮
      event.preventDefault();
      lastActivity.current = Date.now();
      const location = pointFrom(event.clientX, event.clientY, event.currentTarget as HTMLImageElement);
      if (location) void sendMouse({
        action: "scroll",
        ...location,
        delta_y: Math.max(-1000, Math.min(1000, -event.deltaY)),
      });
    };
    for (const element of targets) element.addEventListener("wheel", handler, { passive: false });
    return () => {
      for (const element of targets) element.removeEventListener("wheel", handler);
    };
  }, [frameUrl, immersive]);
  // 手机触屏：画面就是触控板。单指拖动 = 相对移动电脑鼠标（见 move()），
  // 单指点按 = 在电脑当前光标位置左键单击，双指点按 = 右键。
  // 这里只负责阻止浏览器自身的滚动 / 缩放 / 长按菜单，并判定「点按」。
  useEffect(() => {
    const targets = [frame.current, immersive ? overlayImg.current : null].filter(
      (element): element is HTMLImageElement => Boolean(element),
    );
    if (!targets.length || !frameUrl) return;
    const handleStart = (event: TouchEvent) => {
      if (!statusRef.current?.control_enabled) return; // 未打开控制时保留页面正常滚动
      event.preventDefault();
      lastActivity.current = Date.now();
      lastTouchAt.current = Date.now();
      touchDown.current = true;
      const first = event.touches[0];
      touchState.current = {
        count: event.touches.length,
        startX: first?.clientX ?? 0,
        startY: first?.clientY ?? 0,
        moved: false,
        multi: event.touches.length >= 2,
      };
    };
    const handleMove = (event: TouchEvent) => {
      if (!statusRef.current?.control_enabled) return;
      event.preventDefault();
      lastActivity.current = Date.now();
      const first = event.touches[0];
      if (!first || touchState.current.moved) return;
      if (Math.hypot(first.clientX - touchState.current.startX, first.clientY - touchState.current.startY) > 12) {
        touchState.current.moved = true;
      }
    };
    const handleEnd = (event: TouchEvent) => {
      if (!statusRef.current?.control_enabled) return;
      event.preventDefault();
      lastActivity.current = Date.now();
      lastTouchAt.current = Date.now();
      const wasCount = touchState.current.count;
      const wasMulti = touchState.current.multi;
      const first = event.changedTouches[0];
      const moved = touchState.current.moved
        || (first ? Math.hypot(first.clientX - touchState.current.startX, first.clientY - touchState.current.startY) > 12 : false);
      touchState.current.count = event.touches.length;
      if (event.touches.length === 0) touchDown.current = false;
      if (event.touches.length > 0 || moved) return;
      // 点按不带坐标：由后端在电脑真实光标位置点击，避免把光标瞬移到手指按下的位置
      void sendMouse({ action: "click", button: wasCount >= 2 || wasMulti ? "right" : "left", clicks: 1 });
    };
    const handleCancel = () => {
      // 系统抢走手势（来电、切后台等）时按「已移动」处理：不补点击，并丢掉拖动基准
      touchDown.current = false;
      touchState.current.moved = true;
      touchState.current.count = 0;
      activePointers.current.clear();
      dragRef.current = null;
      pendingRel.current = { dx: 0, dy: 0 };
      requestRelease();
    };
    for (const element of targets) {
      element.addEventListener("touchstart", handleStart, { passive: false });
      element.addEventListener("touchmove", handleMove, { passive: false });
      element.addEventListener("touchend", handleEnd, { passive: false });
      element.addEventListener("touchcancel", handleCancel, { passive: false });
    }
    return () => {
      for (const element of targets) {
        element.removeEventListener("touchstart", handleStart);
        element.removeEventListener("touchmove", handleMove);
        element.removeEventListener("touchend", handleEnd);
        element.removeEventListener("touchcancel", handleCancel);
      }
    };
  }, [immersive, frameUrl]);

  // 沉浸模式下软键盘常驻：进入即聚焦，意外失焦自动拉回。
  useEffect(() => {
    if (!immersive) return;
    const input = overlayKbInput.current;
    const refocus = () => { if (!disposed.current && input && !input.matches(":focus")) input.focus({ preventScroll: true }); };
    const timer = setTimeout(refocus, 150);
    input?.addEventListener("blur", refocus);
    return () => { clearTimeout(timer); input?.removeEventListener("blur", refocus); };
  }, [immersive]);

  useEffect(() => {
    const onResize = () => requestAnimationFrame(() => syncCursorDom());
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, []);

  useEffect(() => {
    if (!fullscreen) return;
    const handler = (event: KeyboardEvent) => {
      if (document.activeElement instanceof HTMLInputElement) return; // 键盘输入条自己处理
      if (!statusRef.current?.control_enabled) return;
      if (!statusRef.current?.control_enabled) return;
      if (["Meta", "Control", "Shift", "Alt", "CapsLock"].includes(event.key)) return;
      const special: Record<string, string> = {
        Enter: "return", Tab: "tab", Backspace: "delete", Delete: "delete",
        ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
        PageUp: "pageup", PageDown: "pagedown", Home: "home", End: "end", " ": "space",
      };
      let key: string | undefined;
      if (event.key in special) key = special[event.key];
      else if (/^F([1-9]|1[0-2])$/.test(event.key)) key = event.key.toLowerCase();
      else if (event.key.length === 1) key = event.key;
      if (!key) return;
      event.preventDefault();
      event.stopPropagation();
      lastActivity.current = Date.now();
      const mods: string[] = [];
      if (event.metaKey) mods.push("cmd");
      if (event.ctrlKey) mods.push("control");
      if (event.altKey) mods.push("option");
      if (event.shiftKey) mods.push("shift");
      void sendKeyboard({ keys: [...mods, key].join("+") });
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [fullscreen]);


  async function control(enabled: boolean) {
    setBusy(true);
    setError("");
    try {
      const next = await api<DesktopStatus>("/desktop/control", { enabled });
      setStatus(next);
      if (!next.control_enabled) {
        // 后端关闭控制时会自动松开按键，这里同步清掉本地状态
        cancelLongPress();
        pressRequested.current = "";
        releaseRequested.current = false;
        pressedRef.current = false;
        setHoldActive(false);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function pointFrom(clientX: number, clientY: number, image: HTMLImageElement | null = frame.current) {
    const current = statusRef.current;
    if (!image || !current?.screen_width || !current.screen_height) return null;
    const rect = image.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    // object-fit: contain 后的实际内容区（letterbox 不参与映射）
    const scale = Math.min(rect.width / current.screen_width, rect.height / current.screen_height);
    const innerWidth = current.screen_width * scale;
    const innerHeight = current.screen_height * scale;
    const left = rect.left + (rect.width - innerWidth) / 2;
    const top = rect.top + (rect.height - innerHeight) / 2;
    return {
      x: Math.max(0, Math.min(current.screen_width - 1, (clientX - left) / innerWidth * current.screen_width)),
      y: Math.max(0, Math.min(current.screen_height - 1, (clientY - top) / innerHeight * current.screen_height)),
    };
  }

  function syncCursorDom() {
    const s = statusRef.current;
    if (!s?.screen_width || !s.screen_height) return;
    for (const [dot, img] of [[frameDot.current, frame.current], [overlayDot.current, overlayImg.current]] as const) {
      if (!dot || !img) continue;
      const rect = img.getBoundingClientRect();
      if (!rect.width || !rect.height) continue;
      const scale = Math.min(rect.width / s.screen_width, rect.height / s.screen_height);
      const innerWidth = s.screen_width * scale;
      const innerHeight = s.screen_height * scale;
      dot.style.left = `${(rect.width - innerWidth) / 2 + displayRef.current.x / s.screen_width * innerWidth}px`;
      dot.style.top = `${(rect.height - innerHeight) / 2 + displayRef.current.y / s.screen_height * innerHeight}px`;
    }
  }

  async function sendKeyboard(body: Record<string, unknown>) {
    if (!statusRef.current?.control_enabled) return;
    try {
      await api("/desktop/keyboard", body);
    } catch (e) {
      if (!disposed.current) setError((e as Error).message);
    }
  }

  async function sendMouse(body: Record<string, unknown>) {
    if (!statusRef.current?.control_enabled) return;
    try {
      const result = await api<{ ok?: boolean; x?: number; y?: number }>("/desktop/mouse", body);
      if (typeof result?.x === "number" && typeof result?.y === "number") {
        // 桌面绝对路径(移动/点击/滚轮):显示与电脑真实位置同步
        cursorRef.current = { x: result.x, y: result.y };
        displayRef.current = cursorRef.current;
        lastMouseSyncAt.current = Date.now();
        requestAnimationFrame(() => syncCursorDom());
      }
    } catch (e) {
      if (!disposed.current) setError((e as Error).message);
    }
  }

  function touchGestureActive() {
    // 触屏流程已经处理过这次点按：忽略浏览器随后合成的 click / contextmenu
    return touchDown.current || Date.now() - lastTouchAt.current < 1200;
  }

  function click(event: React.MouseEvent<HTMLImageElement>) {
    if (touchGestureActive()) return;
    lastActivity.current = Date.now();
    const location = pointFrom(event.clientX, event.clientY, event.currentTarget);
    if (location) void sendMouse({ action: "click", ...location, button: "left", clicks: 1 });
  }

  function contextMenu(event: React.MouseEvent<HTMLImageElement>) {
    if (touchGestureActive()) return;
    if (!statusRef.current?.control_enabled) return;
    event.preventDefault();
    lastActivity.current = Date.now();
    const location = pointFrom(event.clientX, event.clientY, event.currentTarget);
    if (location) void sendMouse({ action: "click", ...location, button: "right", clicks: 1 });
  }

  // 单指拖动 = 移动电脑鼠标：手指位移只在手机本地按画面显示比例换算成屏幕像素，
  // 再作为相对位移下发；电脑原本的光标位置只当「从哪儿继续走」的基准，
  // 绝不把光标拽到手指按下的坐标上，因此不会与它自己的鼠标位置互相打架。
  function move(event: React.PointerEvent<HTMLImageElement>) {
    lastActivity.current = Date.now();
    if (event.pointerType === "touch") {
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      const s = statusRef.current;
      if (!s?.screen_width || !s.screen_height) return;
      // 手指先移动了就不再算长按（长按拖动只认「按住不动」）
      if (pressTimer.current !== undefined
          && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > LONG_PRESS_SLOP) {
        cancelLongPress();
      }
      const fingers = Math.max(touchState.current.count, activePointers.current.size);
      if (fingers >= 2) {
        // 双指阶段只用于点按：吞掉这段行程，抬起时才不会一次性补发一大段位移
        drag.lastX = event.clientX;
        drag.lastY = event.clientY;
        return;
      }
      const now = Date.now();
      if (now - lastMove.current < 16) return; // 高频事件先攒着，下一次一起发，位移不丢
      lastMove.current = now;
      const dx = event.clientX - drag.lastX;
      const dy = event.clientY - drag.lastY;
      // 死区：抑制触屏静止噪声，避免光标自主抖动
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      const step = dragDelta(dx, dy, event.currentTarget.getBoundingClientRect(), s);
      if (step) queueMoveRel(step.dx, step.dy);
      return;
    }
    if (Date.now() - lastMove.current < 100) return;
    lastMove.current = Date.now();
    const location = pointFrom(event.clientX, event.clientY, event.currentTarget);
    if (location) void sendMouse({ action: "move", ...location });
  }

  function queueMoveRel(dxScreen: number, dyScreen: number) {
    pendingRel.current = {
      dx: pendingRel.current.dx + dxScreen,
      dy: pendingRel.current.dy + dyScreen,
    };
    // 画面光标只由手指位移累加驱动（纯意图），绝不从后端回读值反推：
    // 回读值永远滞后于在途请求，用它重算会让光标回跳、并在松手时"补走"一段。
    displayRef.current = clampToScreen({
      x: displayRef.current.x + dxScreen,
      y: displayRef.current.y + dyScreen,
    }, statusRef.current);
    requestAnimationFrame(() => syncCursorDom());
    if (!moveInFlight.current) void pumpMove();
  }

  async function pumpMove() {
    moveInFlight.current = true;
    try {
      while (!disposed.current) {
        const press = pressRequested.current;
        if (press) {
          // 长按拖动：先按下，再发位移，顺序不能反
          pressRequested.current = "";
          try {
            const result = await api<{ pressed?: string }>("/desktop/mouse", { action: "press", button: press });
            if (!disposed.current) {
              // 请求成功就记为"按住"（除非后端明确回话说没按住）：否则一旦响应里没有
              // pressed 字段，客户端就不会再发 release，左键会被留在电脑上按下。
              pressedRef.current = result?.pressed === undefined || result?.pressed === press;
              if (pressedRef.current) setHoldActive(true);
            }
          } catch (e) {
            if (!disposed.current) setError((e as Error).message);
          }
          continue;
        }
        if (Math.abs(pendingRel.current.dx) <= 0.01 && Math.abs(pendingRel.current.dy) <= 0.01) break;
        if (!statusRef.current?.control_enabled) break;
        // 后端单次相对位移上限 1200 像素：长滑动拆成多段依次下发，避免整段被拒而丢动作
        const { step, rest } = takeRelativeStep(pendingRel.current);
        pendingRel.current = rest;
        try {
          const result = await api<{ x?: number; y?: number }>("/desktop/mouse", {
            action: "move_rel", dx: step.dx, dy: step.dy,
          });
          if (typeof result?.x === "number" && typeof result?.y === "number") {
            // 回读只更新「电脑真实位置」这一基准；拖动中不做任何回拉，显示由手指驱动
            cursorRef.current = { x: result.x, y: result.y };
            lastMouseSyncAt.current = Date.now();
          }
        } catch (e) {
          pendingRel.current = { dx: 0, dy: 0 };
          if (!disposed.current) setError((e as Error).message);
          break;
        }
      }
      if (releaseRequested.current) {
        // 松开必须排在最后一段位移之后，否则拖拽会提前结束
        releaseRequested.current = false;
        pressedRef.current = false;
        if (!disposed.current) setHoldActive(false);
        if (statusRef.current?.control_enabled) {
          try {
            await api("/desktop/mouse", { action: "release", button: "left" });
          } catch (e) {
            if (!disposed.current) setError((e as Error).message);
          }
        }
      }
    } finally {
      moveInFlight.current = false;
    }
    // 这里刻意不做任何"以真实位置为准"的吸附：手指停下后光标必须立刻停住，
    // 与电脑真实位置的收敛只在完全空闲时由状态轮询完成（见 tick）。
  }

  function pointerDown(event: React.PointerEvent<HTMLImageElement>) {
    lastActivity.current = Date.now();
    if (event.pointerType !== "touch") return;
    // 自己数活跃手指：只派发 pointer 事件、不给 touch 事件的环境里也要能判定双指点按
    activePointers.current.add(event.pointerId);
    touchDown.current = true;
    if (!statusRef.current?.control_enabled) return;
    if (activePointers.current.size > 1 || dragRef.current) {
      // 第二根手指：取消还没生效的长按；已经在按住拖动时吞掉这次手势，避免再补一次右键
      cancelLongPress();
      if (pressedRef.current) touchState.current.moved = true;
      return;
    }
    // 阻止焦点转移保持软键盘常驻；按下即锁定相对拖动基准，点按本身由触屏流程处理
    event.preventDefault();
    pendingRel.current = { dx: 0, dy: 0 };
    dragRef.current = {
      pointerId: event.pointerId,
      lastX: event.clientX, lastY: event.clientY,
      startX: event.clientX, startY: event.clientY,
    };
    lastMove.current = 0;
    startLongPress(event.pointerId);
    requestAnimationFrame(() => syncCursorDom());
  }

  function pointerUp(event: React.PointerEvent<HTMLImageElement>) {
    if (event.pointerType !== "touch") return;
    activePointers.current.delete(event.pointerId);
    if (activePointers.current.size === 0) touchDown.current = false;
    // 节流窗口内攒下的最后一段位移要补齐再收尾，否则指针会停在手指后面
    flushTouchDrag(event);
    requestRelease();
    endTouchDrag(event.pointerId);
  }

  function flushTouchDrag(event: React.PointerEvent<HTMLImageElement>) {
    const drag = dragRef.current;
    const s = statusRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (!s?.screen_width || !s.screen_height) return;
    const dx = event.clientX - drag.lastX;
    const dy = event.clientY - drag.lastY;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
    const step = dragDelta(dx, dy, event.currentTarget.getBoundingClientRect(), s);
    if (step) queueMoveRel(step.dx, step.dy);
  }

  function pointerCancel(event: React.PointerEvent<HTMLImageElement>) {
    if (event.pointerType !== "touch") return;
    activePointers.current.delete(event.pointerId);
    if (activePointers.current.size === 0) touchDown.current = false;
    if (dragRef.current?.pointerId !== event.pointerId) return;
    // pointercancel 时后续坐标已不可信：丢掉基准与未发位移，避免接着用错误的起点
    dragRef.current = null;
    pendingRel.current = { dx: 0, dy: 0 };
    requestRelease();
  }

  function endTouchDrag(pointerId: number) {
    if (dragRef.current?.pointerId !== pointerId) return;
    // 只清基准，不动光标位置：手指停下时画面光标就停在那里（不吸附、不回拉）
    dragRef.current = null;
  }

  function cancelLongPress() {
    if (pressTimer.current !== undefined) {
      clearTimeout(pressTimer.current);
      pressTimer.current = undefined;
    }
  }

  // 长按不动 = 按住电脑鼠标左键；此后的手指拖动就是「按住左键拖动」（选文字、拖窗口/文件）。
  function startLongPress(pointerId: number) {
    cancelLongPress();
    pressTimer.current = setTimeout(() => {
      pressTimer.current = undefined;
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== pointerId) return;
      if (Math.hypot(drag.lastX - drag.startX, drag.lastY - drag.startY) > LONG_PRESS_SLOP) return;
      if (!statusRef.current?.control_enabled) return;
      if (typeof navigator !== "undefined" && "vibrate" in navigator) navigator.vibrate?.(12);
      pressRequested.current = "left";
      if (!moveInFlight.current) void pumpMove();
    }, LONG_PRESS_MS);
  }

  // 松手或手势被系统抢走：松开请求排在最后一段位移之后，避免拖拽提前结束。
  function requestRelease() {
    cancelLongPress();
    pressRequested.current = "";
    if (!pressedRef.current && !releaseRequested.current) return;
    releaseRequested.current = true;
    pressedRef.current = false;
    setHoldActive(false);
    if (!moveInFlight.current) void pumpMove();
  }

  // 断开连接 / 卸载 / 切后台时的尽力松开：不走队列，直接发一次。
  function releaseButtonNow() {
    cancelLongPress();
    pressRequested.current = "";
    const held = pressedRef.current || releaseRequested.current;
    pressedRef.current = false;
    releaseRequested.current = false;
    if (!held || !statusRef.current?.control_enabled) return;
    void api("/desktop/mouse", { action: "release", button: "left" }).catch(() => {});
  }

  const [kbValue, setKbValue] = useState("");
  function kbSend(text: string) {
    for (const character of text) void sendKeyboard({ keys: character });
  }
  function kbChange(event: React.ChangeEvent<HTMLInputElement>) {
    const value = event.target.value;
    if (!value || (event.nativeEvent as InputEvent).isComposing) return;
    kbSend(value);
    setKbValue("");
  }
  function kbCompositionEnd(event: React.CompositionEvent<HTMLInputElement>) {
    if (event.data) kbSend(event.data);
    setKbValue("");
  }
  function kbKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if ((event.nativeEvent as KeyboardEvent).isComposing) return;
    if (event.key === "Enter") { event.preventDefault(); void sendKeyboard({ keys: "return" }); }
    else if (event.key === "Backspace") { event.preventDefault(); void sendKeyboard({ keys: "delete" }); }
  }

  const available = Boolean(frameUrl && !disconnected && (browserMode || status?.available));
  return <section className={`local-desktop ${fullscreen ? "local-desktop-fullscreen" : ""}`}>
    <div className="local-desktop-heading">
      <div><h3>{browserMode ? "浏览器画面" : fullscreen ? "屏幕画面" : "本机屏幕"}</h3><p>{browserMode
        ? "显示 Bot 最近一次浏览器操作的画面（自动截图流）；60 秒无操作自动断开。"
        : fullscreen ? "显示这台电脑的全屏画面；60 秒无操作自动断开。" : "显示运行 Carme 后端的这台 Mac 的主屏幕；60 秒无操作自动断开。"}</p></div>
      <button type="button" className="secondary-button" onClick={() => void tick()} disabled={inFlight.current || disconnected}><RefreshCw size={14} />刷新</button>
    </div>
    <div
      className={`desktop-frame ${fullscreen ? "desktop-frame-full" : ""} ${status?.control_enabled && available ? "desktop-frame-control" : ""}`}
      style={fullscreen && status?.screen_width && status.screen_height
        ? ({ "--dr": String(status.screen_width / status.screen_height) } as React.CSSProperties)
        : undefined}
    >
      {available ? <img ref={frame} src={frameUrl} alt={browserMode ? "Bot 浏览器画面" : "本机 Mac 屏幕"} draggable={false} onClick={click} onContextMenu={contextMenu} onPointerMove={move} onPointerDown={pointerDown} onPointerUp={pointerUp} onPointerCancel={pointerCancel} /> : (
        <div className="desktop-empty">
          <Monitor size={30} strokeWidth={1.3} />
          <strong>{disconnected ? "已断开" : browserMode ? "浏览器画面暂不可用" : status?.enabled === false ? "本机桌面功能已关闭" : "屏幕暂不可用"}</strong>
          <span>{disconnected ? "超过 60 秒没有操作，已停止查看。" : error || (browserMode ? "等待 Bot 在浏览器里操作并产生截图…" : status?.error || "正在读取屏幕…")}</span>
          {disconnected && <button type="button" className="primary-button" onClick={reconnect}><RefreshCw size={14} />重新连接</button>}
        </div>
      )}
      {fullscreen && <button type="button" className="desktop-fs-btn" aria-label="全屏显示电脑屏幕" disabled={!available} onClick={(event) => { event.stopPropagation(); setImmersive(true); }}><Maximize size={16} /></button>}
      {fullscreen && !browserMode && <span ref={frameDot} className={`desktop-cursor ${holdActive ? "pressed" : ""}`} aria-hidden="true" />}
    </div>
    <div className="local-desktop-toolbar">
      <span className={`desktop-status-dot ${available ? "online" : ""}`} />
      <span>{available ? (browserMode ? "浏览器画面 · 自动截图" : `${status?.screen_width} × ${status?.screen_height}`) : disconnected ? "已断开" : "未连接"}</span>
      {holdActive && <span className="desktop-hold-badge">按住左键中</span>}
      {!browserMode && <label className="desktop-control-toggle"><input type="checkbox" checked={Boolean(status?.control_enabled)} disabled={busy || !status?.enabled || disconnected} onChange={(event) => void control(event.target.checked)} /><span>鼠标与键盘</span></label>}
    </div>
    {fullscreen && !browserMode && <div className="desktop-keyboard-bar">
      <input
        ref={kbInput}
        type="text"
        placeholder={status?.control_enabled ? "用手机键盘直接输入到电脑；回车发送 Enter" : "打开「鼠标与键盘」后可输入"}
        disabled={!status?.control_enabled}
        value={kbValue}
        onChange={kbChange}
        onCompositionEnd={kbCompositionEnd}
        onKeyDown={kbKeyDown}
        autoCapitalize="none"
        autoCorrect="off"
        autoComplete="off"
        spellCheck={false}
        enterKeyHint="send"
      />
    </div>}
    {(error || (!browserMode && status?.error)) && <p className="form-error" role="alert">{error || (!browserMode && status?.error)}</p>}
    {!fullscreen && browserMode && <div className="info-box desktop-permission-help"><Info size={16} /><p>本账号未授权连接本机电脑，画面已降级为 Bot 浏览器的<strong>自动截图流</strong>：Bot 在浏览器里执行任务并截图后，这里自动更新为最近一幕。画面只读、不可点击，也不会向电脑发送鼠标键盘事件。</p></div>}
    {!fullscreen && !browserMode && <div className="info-box desktop-permission-help"><Info size={16} /><p>macOS 首次使用请在「系统设置 → 隐私与安全性 → 屏幕录制」中允许运行 Carme 的 Python（路径见 README）使用<strong>屏幕录制</strong>；远程鼠标控制还需要在<strong>辅助功能</strong>中允许它。权限变更后必须重启 Carme 服务。</p></div>}
    {browserMode
      ? <p className="form-help">浏览器隔离账号的降级画面：展示 Bot 最近一次浏览器截图，随任务自动刷新（约每 2 秒）；60 秒无操作自动断开。画面只读，不提供鼠标与键盘控制；如需实时画面或人工接管，请按流程授权连接执行电脑。</p>
      : fullscreen
      ? <p className="form-help">手机触控板：在画面上<strong>单指拖动即可移动电脑鼠标</strong>——手指位移按画面比例换算成相对位移，从电脑当前光标位置继续走，不与它原本的位置冲突；<strong>按住不动约半秒再拖动 = 按住左键拖动</strong>（选文字、拖窗口/文件，画面上光标会变成实心点提示），单指点按=在电脑当前光标位置单击，双指点按=右键。底部输入条实时向电脑打字，回车发送 Enter。60 秒无操作自动断开并停止抓屏；屏幕录制 / 辅助功能权限见侧边栏「执行电脑」。</p>
      : <p className="form-help">画面按需连接：打开本面板或点击「重新连接」才开始查看，鼠标在画面上移动、点击或滚动会保持连接，手机端在画面上单指拖动同样移动电脑鼠标（相对位移；按住不动约半秒再拖动 = 按住左键拖动；点按=在电脑当前光标位置单击）；60 秒无操作自动断开并停止抓屏，关闭面板同样立即停止。鼠标控制只接受已认证的 Carme 前端请求，随服务重启关闭。不要在不可信网络公开 Carme 端口。</p>}
    {fullscreen && immersive && (
      <div className="desktop-immersive" role="dialog" aria-label="电脑屏幕全屏显示">
        {available
          ? <img
              ref={overlayImg}
              src={frameUrl}
              alt={browserMode ? "Bot 浏览器画面（全屏）" : "电脑屏幕全屏画面"}
              draggable={false}
              onClick={click}
              onPointerMove={move}
              onPointerDown={pointerDown}
              onPointerUp={pointerUp}
              onPointerCancel={pointerCancel}
            />
          : <div className="desktop-empty"><Monitor size={34} strokeWidth={1.3} /><strong>{browserMode ? "浏览器画面暂不可用" : "屏幕暂不可用"}</strong><span>{disconnected ? "超过 60 秒没有操作，已停止查看。" : error || (browserMode ? "等待 Bot 在浏览器里操作并产生截图…" : status?.error || "正在读取屏幕…")}</span></div>}
        {disconnected && available && <div className="computer-screen-overlay"><span>已休眠</span><button type="button" className="primary-button" onClick={reconnect}><RefreshCw size={14} />唤醒连接</button></div>}
        <div className="desktop-immersive-bar">
          <span className={`desktop-status-dot ${available && !disconnected ? "online" : ""}`} />
          <span>{available ? (browserMode ? "浏览器画面 · 自动截图" : `${status?.screen_width} × ${status?.screen_height}`) : "未连接"}</span>
          {holdActive && <span className="desktop-hold-badge">按住左键中</span>}
          {!browserMode && <label className="desktop-control-toggle"><input type="checkbox" checked={Boolean(status?.control_enabled)} disabled={busy || !status?.enabled} onChange={(event) => void control(event.target.checked)} /><span>鼠标与键盘</span></label>}
          <button type="button" className="secondary-button" onClick={() => setImmersive(false)}><X size={14} />退出全屏</button>
        {available && !browserMode && <span ref={overlayDot} className={`desktop-cursor ${holdActive ? "pressed" : ""}`} aria-hidden="true" />}
        {!browserMode && <div className="desktop-immersive-kb">
          <input
            ref={overlayKbInput}
            type="text"
            placeholder={status?.control_enabled ? "用手机键盘直接输入到电脑；回车发送 Enter" : "打开「鼠标与键盘」后可输入"}
            disabled={!status?.control_enabled}
            value={kbValue}
            onChange={kbChange}
            onCompositionEnd={kbCompositionEnd}
            onKeyDown={kbKeyDown}
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="off"
            spellCheck={false}
            enterKeyHint="send"
          />
        </div>}
        </div>
      </div>
    )}
  </section>;
}

function ComputerScreenDialog({ onClose }: { onClose: () => void }) {
  return <Modal title="Bot 的电脑 · 屏幕画面" onClose={onClose} wide className="screen-modal">
    <div className="modal-body">
      <LocalDesktopView variant="fullscreen" />
      <p className="form-help">需要管理执行电脑（SSH 节点、浏览器、桌面参数）时，使用侧边栏「执行电脑」。</p>
    </div>
  </Modal>;
}

function ComputerPreviewFrame({ title, note, footerIdle }: { title: string; note: string; footerIdle: string }) {
  // 详情页缩略画面：2 秒一帧；60 秒无操作休眠——保留最后一帧并压暗，点击唤醒恢复。
  const IDLE_DIM_MS = 60_000;
  const [status, setStatus] = useState<DesktopStatus | null>(null);
  const [frameUrl, setFrameUrl] = useState("");
  // 浏览器降级：mode=browser 时画面源切换到 /api/browser/screenshot。
  const [browserMode, setBrowserMode] = useState(false);
  const browserModeRef = useRef(false);
  const lastShotAt = useRef(0);
  const [error, setError] = useState("");
  const [disconnected, setDisconnected] = useState(false);
  const disposed = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const inFlight = useRef(false);
  const lastActivity = useRef(Date.now());

  const disconnect = useCallback(() => {
    if (timer.current) { clearTimeout(timer.current); timer.current = undefined; }
    lastShotAt.current = 0; // 唤醒重连时不带旧 since，避免 304 压制新画面
    setDisconnected(true);  // 保留最后一帧画面，只压暗
  }, []);

  const tick = useCallback(async () => {
    if (disposed.current || inFlight.current) return;
    if (Date.now() - lastActivity.current > IDLE_DIM_MS) { disconnect(); return; }
    inFlight.current = true;
    try {
      const next = await api<DesktopStatus>("/desktop/status");
      if (!disposed.current) {
        const isBrowser = next.mode === "browser";
        if (isBrowser !== browserModeRef.current) {
          // 数据源切换：清掉旧帧，避免本机桌面最后一帧冒充浏览器画面
          browserModeRef.current = isBrowser;
          lastShotAt.current = 0;
          setFrameUrl((previous) => {
            if (previous) URL.revokeObjectURL(previous);
            return "";
          });
        }
        setBrowserMode(isBrowser);
        setStatus(next);
      }
      const browser = browserModeRef.current;
      const since = lastShotAt.current;
      let response: Response;
      try {
        response = await fetch(`/api/${browser ? "browser" : "desktop"}/screenshot${since ? `?since=${since}` : ""}`, {
          cache: "no-store", redirect: "manual", credentials: "same-origin",
          headers: authHeaders(),
        });
      } catch { throw new Error(browser ? "无法读取浏览器画面；请确认 Carme 服务仍在运行。" : "无法读取本机屏幕；请确认 Carme 服务仍在运行。"); }
      if (response.status === 304) return; // 画面未更新：保留当前帧
      const contentType = response.headers.get("content-type") || "";
      if (response.redirected || response.type === "opaqueredirect" || response.type === "opaque"
          || response.status === 0 || (response.status >= 300 && response.status < 400)
          || contentType.includes("text/html")) throw new Error(ACCESS_LOGIN_MESSAGE);
      if (response.status === 401) throw new Error("需要访问令牌，请在设置中连接后端。");
      if (!response.ok) {
        let message = browser ? `读取浏览器画面失败（${response.status}）` : `读取本机屏幕失败（${response.status}）`;
        try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch { /* keep status */ }
        throw new Error(message);
      }
      const stamp = Number(response.headers.get("x-screenshot-mtime") || "");
      if (Number.isFinite(stamp) && stamp > 0) lastShotAt.current = stamp;
      const nextUrl = URL.createObjectURL(await response.blob());
      if (disposed.current) URL.revokeObjectURL(nextUrl);
      else setFrameUrl((previous) => {
        if (previous) URL.revokeObjectURL(previous);
        return nextUrl;
      });
      if (!disposed.current) setError("");
    } catch (e) {
      if (!disposed.current) setError((e as Error).message);
    } finally {
      inFlight.current = false;
      if (disposed.current) return;
      if (Date.now() - lastActivity.current > IDLE_DIM_MS) { disconnect(); return; }
      timer.current = setTimeout(() => { void tick(); }, 2000);
    }
  }, [disconnect]);

  const reconnect = useCallback(() => {
    setDisconnected(false);
    setError("");
    lastActivity.current = Date.now();
    void tick();
  }, [tick]);

  useEffect(() => {
    disposed.current = false;
    lastActivity.current = Date.now();
    void tick();
    return () => {
      disposed.current = true;
      if (timer.current) clearTimeout(timer.current);
      setFrameUrl((previous) => { if (previous) URL.revokeObjectURL(previous); return ""; });
    };
  }, [tick]);

  function touch() {
    lastActivity.current = Date.now();
  }

  const connected = Boolean(frameUrl && (browserMode || status?.available));
  return <>
    {connected
      ? <div className={`computer-screen ${disconnected ? "computer-screen-dim" : ""}`}>
          <img src={frameUrl} alt={browserMode ? "Bot 浏览器画面" : "电脑屏幕画面"} onPointerMove={touch} onPointerDown={touch} />
          {disconnected && <div className="computer-screen-overlay">
            <span>已休眠</span>
            <button type="button" className="primary-button" onClick={(event) => { event.stopPropagation(); reconnect(); }}><RefreshCw size={14} />唤醒连接</button>
          </div>}
        </div>
      : <div className="computer-placeholder">
          <Monitor size={33} strokeWidth={1.2} />
          <strong>{title}</strong>
          <span>{error || note}</span>
        </div>}
    <div className="computer-card-footer">
      <span className={`connection-dot ${connected && !disconnected ? "live" : ""}`} />
      <span>{disconnected ? "已休眠 · 点击唤醒" : connected ? (browserMode ? "浏览器画面" : "屏幕实时画面") : footerIdle}</span>
      <ChevronRight size={14} />
    </div>
  </>;
}

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
type Panel = "new" | "nodes" | "screen" | "bot" | "market" | "settings" | "routine" | "history" | "memory" | "files" | "summary" | null;
type ChatAction = "pin" | "folder" | "unread" | "rename" | "edit" | "duplicate" | "copy" | "hide" | "delete";
type ChatDialogAction = { kind: "rename" | "folder" | "delete"; conversation: Conversation };

const ACCOUNT_NAME = document.documentElement.dataset.carmeAccount || "";
function storageKey(key: string) { return ACCOUNT_NAME ? `carme:${ACCOUNT_NAME}:${key}` : key; }
function readStorage(key: string) {
  try {
    return localStorage.getItem(storageKey(key)) || "";
  } catch {
    return "";
  }
}
function saveStorage(key: string, value: string) {
  try {
    value ? localStorage.setItem(storageKey(key), value) : localStorage.removeItem(storageKey(key));
  } catch {
    /* Private browsing may restrict storage. */
  }
}
// Retire the legacy secret storage and token URL immediately.
try { localStorage.removeItem("carme_token"); } catch { /* Storage can be disabled. */ }
saveStorage("carme_token", "");
if (new URLSearchParams(location.search).has("token")) {
  const url = new URL(location.href);
  url.searchParams.delete("token");
  history.replaceState({}, "", url);
}
let token = "";
let csrf = "";
let sessionReady: Promise<void> | undefined;
function connectSession() {
  if (!sessionReady) {
    sessionReady = api<{ csrf: string }>("/session", token ? {} : undefined)
      .then((session) => { csrf = session.csrf; token = ""; })
      .catch((error) => { sessionReady = undefined; token = ""; throw error; });
  }
  return sessionReady;
}
function authHeaders(): Record<string, string> {
  return { ...(csrf ? { "X-Carme-CSRF": csrf } : {}), ...(ACCOUNT_NAME ? { "X-Carme-Account": ACCOUNT_NAME } : {}) };
}

function accountChanged() {
  try { localStorage.setItem("carme_auth_changed", String(Date.now())); } catch { /* Storage can be disabled. */ }
  location.replace("/");
}
if (ACCOUNT_NAME) {
  window.addEventListener("storage", (event) => { if (event.key === "carme_auth_changed") location.replace("/"); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
}

const ACCESS_LOGIN_MESSAGE = "Cloudflare Access 登录已过期或尚未完成，请重新打开受保护地址登录后再试。";
function reopenAccessEntry() {
  const url = new URL(location.href);
  url.searchParams.delete("token");
  window.location.assign(url.pathname + url.search + url.hash);
}

// —— 语音输入：浏览器原生识别（Web Speech API）与服务端转写 ——
type SpeechAlternative = { transcript: string };
type SpeechResult = { isFinal: boolean; length: number; [index: number]: SpeechAlternative };
type SpeechResultEvent = { resultIndex: number; results: ArrayLike<SpeechResult> };
type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start(): void;
  stop(): void;
  abort(): void;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onresult: ((event: SpeechResultEvent) => void) | null;
};
function speechRecognitionCtor(): (new () => SpeechRecognitionLike) | null {
  const scope = window as unknown as {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  };
  return scope.SpeechRecognition || scope.webkitSpeechRecognition || null;
}
const VOICE_LANGS = [
  { id: "zh-CN", label: "中文（普通话）" },
  { id: "en-US", label: "English (US)" },
  { id: "ja-JP", label: "日本語" },
  { id: "yue-Hant-HK", label: "粤语（香港）" },
];
function readVoiceLang() {
  const stored = readStorage("carme_voice_lang");
  return VOICE_LANGS.some((item) => item.id === stored) ? stored : "zh-CN";
}
// 浏览器只给错误码，这里换成可执行的中文提示。
const VOICE_ERRORS: Record<string, string> = {
  "not-allowed": "麦克风权限被拒绝：请在浏览器地址栏的站点设置里允许麦克风后重试。",
  "service-not-allowed": "浏览器拒绝了语音识别服务；可改用「服务端转写」（设置 → 访问 → 语音输入）。",
  "audio-capture": "没有检测到可用的麦克风，请检查系统的输入设备。",
  network: "语音识别服务不可达（Chrome 需要访问 Google、Safari 需要访问 Apple）；可改用「服务端转写」。",
  "no-speech": "没有听到声音，请靠近麦克风再试一次。",
  "bad-grammar": "识别服务无法处理这段语音。",
  language: "识别语言不受支持，请在设置里换一种语言。",
};
type VoiceConfig = { mode: "browser" | "server"; source: "connection" | "custom"; provider: string;
  model: string; base_url: string; api_key_env: string };
type VoiceSettingsResponse = { voice: VoiceConfig; has_key: boolean };
const EMPTY_VOICE: VoiceConfig = { mode: "browser", source: "connection", provider: "", model: "", base_url: "", api_key_env: "" };
const VOICE_MODEL_SUGGESTIONS = ["whisper-1", "gpt-4o-mini-transcribe", "whisper-large-v3",
  "FunAudioLLM/SenseVoiceSmall", "paraformer-v2", "qwen-audio-asr"];
// 语音识别接口预设：都走 OpenAI 兼容的 /audio/transcriptions，降低配置成本。
const ASR_PRESETS = [
  { id: "siliconflow", label: "硅基流动 SiliconFlow", base_url: "https://api.siliconflow.cn/v1",
    models: ["FunAudioLLM/SenseVoiceSmall", "TeleAI/TeleSpeechASR"] },
  { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1",
    models: ["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"] },
  { id: "groq", label: "Groq", base_url: "https://api.groq.com/openai/v1",
    models: ["whisper-large-v3-turbo", "whisper-large-v3"] },
  { id: "dashscope", label: "阿里云百炼（兼容模式）", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: ["qwen-audio-asr", "paraformer-v2"] },
  { id: "custom", label: "自定义（OpenAI 兼容）", base_url: "", models: [] },
];
function voiceModelOptions(config: VoiceConfig, models: SavedModel[]) {
  const preset = ASR_PRESETS.find((item) => item.base_url && item.base_url === config.base_url);
  return Array.from(new Set([
    ...models.filter((model) => model.provider_id === config.provider).map((model) => model.id),
    ...(preset ? preset.models : []),
    ...VOICE_MODEL_SUGGESTIONS,
  ]));
}
function voiceAudioType() {
  if (typeof MediaRecorder === "undefined") return "";
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}
function voiceExtension(type: string) {
  if (type.includes("mp4")) return "m4a";
  if (type.includes("ogg")) return "ogg";
  return "webm";
}
function voiceJoin(base: string, spoken: string) {
  const tail = spoken.replace(/^\s+/, "");
  if (!tail) return base;
  return base + tail;
}
function isAccessLoginError(message: string) {
  return message.includes("Cloudflare Access") || message.includes("非 JSON") || message.includes("实时连接中断");
}

/** 内置字体：后端字体列表还没到、或读取失败时的兜底选项。 */
const BUILTIN_FONTS: Record<string, {label: string; css: string}> = {
  system: {label: "系统默认", css: '-apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif'},
  pingfang: {label: "苹方（内置）", css: '"PingFang SC", "Hiragino Sans GB", sans-serif'},
  songti: {label: "宋体（内置）", css: '"Songti SC", "STSong", "SimSun", serif'},
  kaiti: {label: "楷体（内置）", css: '"Kaiti SC", "STKaiti", "KaiTi", serif'},
  mono: {label: "等宽（内置）", css: '"SFMono-Regular", Menlo, "PingFang SC", monospace'},
};
/** 运行 Carme 的机器上的字体；/api/fonts 直接返回，界面按家族名成对展示。 */
type SystemFont = { family: string; label: string; stack: string; monospace: boolean; faces?: number };
type FontCatalog = { fonts: SystemFont[]; count: number; source: string; platform: string };
// 系统字体用前缀区分内置选项：家族名有可能和内置 id 撞名。
const SYSTEM_FONT_PREFIX = "font:";
const systemFontName = (family: string) => family.slice(SYSTEM_FONT_PREFIX.length);
function fontStack(family: string, fonts: SystemFont[]) {
  if (Object.hasOwn(BUILTIN_FONTS, family)) return BUILTIN_FONTS[family].css;
  if (family.startsWith(SYSTEM_FONT_PREFIX)) {
    // 列表还没到时先按名字写，浏览器会自动用后面的兜底字体。
    const name = systemFontName(family);
    return fonts.find((font) => font.family === name)?.stack
      ?? `"${name.replace(/"/g, "")}", ${BUILTIN_FONTS.system.css}`;
  }
  return BUILTIN_FONTS.system.css;
}
function readAppearance() {
  try {
    const value = JSON.parse(readStorage("carme_appearance"));
    const family = typeof value.family === "string" && value.family.length <= 160 ? value.family as string : "system";
    return {family, size: Number.isInteger(value.size) && value.size >= 14 && value.size <= 22 ? value.size as number : 16};
  } catch { return {family: "system", size: 16}; }
}
function applyAppearance(value: {family: string; size: number}, fonts: SystemFont[] = []) {
  document.documentElement.style.setProperty("--ui-font-family", fontStack(value.family, fonts));
  document.documentElement.style.setProperty("--ui-font-size", `${value.size}px`);
}
applyAppearance(readAppearance());

async function api<T>(
  path: string,
  body?: unknown,
  method = body === undefined ? "GET" : "POST",
): Promise<T> {
  if (path !== "/session") await connectSession();
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      redirect: "manual",
      credentials: "same-origin",
      headers: {
        ...(body !== undefined ? { "Content-Type": body instanceof Blob ? body.type : "application/json" } : {}),
        ...authHeaders(),
        ...(path === "/session" && token ? { Authorization: `Bearer ${token}` } : {}),
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
    if (ACCOUNT_NAME && (response.status === 401 || message === "account_changed" || message === "password_change_required")) {
      location.replace(message === "password_change_required" ? "/account" : "/");
      message = "登录状态已变更，正在重新打开登录入口。";
    } else if (response.status === 401) message = "需要访问令牌，请在设置中连接后端。";
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
  agent?: { name?: string; emoji?: string; avatar?: BotAvatar };
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
      headers: authHeaders(), signal: abort.signal,
    }).then(async (response) => {
      if (!response.ok) return;
      objectUrl = URL.createObjectURL(await response.blob());
      if (disposed) URL.revokeObjectURL(objectUrl); else setSource(objectUrl);
    }).catch(() => {});
    return () => { disposed = true; abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [filename, group, token]);
  const glyphAvatar = !group && !source;
  return (
    <span className={`avatar ${size} ${group ? "group" : ""} ${glyphAvatar ? "glyph-avatar" : ""} ${working ? "avatar-working" : ""}`}
      role={working ? "img" : undefined} aria-label={working ? `${group ? "群聊成员" : agent?.name || "Bot"}正在执行任务` : undefined}>
      <span className="avatar-face">{group ? <Users size={20} /> : source ? <img src={source} alt={`${agent?.name || "Bot"}的头像`} /> : agent?.avatar?.kind === "bot" ? working ? <BotSolid shape={agent.avatar.shape} color={agent.avatar.color} /> : <BotGlyph shape={agent.avatar.shape} color={agent.avatar.color} /> : <BotGlyph shape="circle" color="#000000" />}</span>
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
  disabled = false,
  pressed,
  className = "",
}: {
  label: string;
  children: ReactNode;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
  pressed?: boolean;
  className?: string;
}) {
  return (
    <button
      className={`icon-button ${active ? "active" : ""} ${className}`}
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      {...(pressed === undefined ? {} : { "aria-pressed": pressed })}
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
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  className?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className={`modal ${wide ? "wide" : ""} ${className}`.trimEnd()}
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
  const [listening, setListening] = useState(false);
  const [voiceSeconds, setVoiceSeconds] = useState(0);
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [voiceLang, setVoiceLang] = useState(readVoiceLang);
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfig | null>(null);
  const voiceRef = useRef<{ rec: SpeechRecognitionLike | null; base: string; final: string; interim: string;
    cid: string; heard: boolean; failed: boolean }>({ rec: null, base: "", final: "", interim: "", cid: "", heard: false, failed: false });
  // 服务端听写：边录边按分片上传，每次返回的是整段文字，所以是替换而不是追加。
  const recorderRef = useRef<{ recorder: MediaRecorder | null; chunks: Blob[]; stream: MediaStream | null;
    cid: string; base: string; mime: string; bytes: number; dirty: boolean; uploading: boolean; text: string; failed: boolean }>(
    { recorder: null, chunks: [], stream: null, cid: "", base: "", mime: "", bytes: 0, dirty: false, uploading: false, text: "", failed: false });
  const uploadInput = useRef<HTMLInputElement>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [marketTab, setMarketTab] = useState<"bots" | "skills" | "mcp">("bots");
  const [showDetails, setShowDetails] = useState(true);
  const [tasksOpen, setTasksOpen] = useState(false);
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
  const atBottom = useRef(true);
  const messageScroll = useRef<HTMLDivElement>(null);
  const [scrollHints, setScrollHints] = useState({ top: true, bottom: true, scrollable: false });
  function measureScrollHints() {
    const el = messageScroll.current;
    if (!el) return;
    const next = {
      top: el.scrollTop < 28,
      bottom: el.scrollHeight - el.scrollTop - el.clientHeight < 100,
      scrollable: el.scrollHeight - el.clientHeight > 320,
    };
    setScrollHints((previous) =>
      previous.top === next.top && previous.bottom === next.bottom && previous.scrollable === next.scrollable
        ? previous
        : next,
    );
  }
  function scrollMessages(where: "top" | "bottom") {
    const el = messageScroll.current;
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top: where === "bottom" ? el.scrollHeight : 0, behavior: reduce ? "auto" : "smooth" });
    atBottom.current = where === "bottom";
  }
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
      try { await connectSession(); } catch (e) { if (!disposed) setError((e as Error).message); return; }
      if (disposed) return;
      const events = new EventSource("/api/events" + (ACCOUNT_NAME ? `?account=${encodeURIComponent(ACCOUNT_NAME)}` : ""), { withCredentials: true });
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
    if (!current) return;
    // 打开会话时始终落到最新一条：详情渲染、图片/任务卡撑高都可能在首帧之后发生，
    // 因此分几次补贴底，否则会停在旧位置，看起来像"没有显示最新对话"。
    const pin = () => {
      const el = messageScroll.current;
      if (el && atBottom.current) el.scrollTop = el.scrollHeight;
      measureScrollHints();
    };
    const timers = [0, 120, 400].map((delay) => window.setTimeout(pin, delay));
    void refreshDetail(current).then(pin).catch((e) => setError(e.message));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [current, refreshDetail]);
  useEffect(() => {
    const el = messageScroll.current;
    if (!el) return;
    if (atBottom.current) el.scrollTop = el.scrollHeight;
    measureScrollHints();
    const frame = requestAnimationFrame(() => {
      if (atBottom.current) el.scrollTop = el.scrollHeight;
      measureScrollHints();
    });
    return () => cancelAnimationFrame(frame);
  }, [selected?.messages.length, selected?.messages.at(-1)?.content, current, selected?.tasks.length, showDetails]);
  useEffect(() => {
    const el = messageScroll.current;
    if (!el) return;
    measureScrollHints();
    if (typeof ResizeObserver === "undefined") return;
    // 内容变高（长回复、图片、任务卡）时，若视图本来贴着底部就继续保持贴底。
    const pin = () => {
      if (atBottom.current) el.scrollTop = el.scrollHeight;
      measureScrollHints();
    };
    const observer = new ResizeObserver(pin);
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    return () => observer.disconnect();
  }, [mobileView, panel, current, showDetails, selected?.messages.length]);
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
    cancelVoice();
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
  const micMode = voiceConfig?.mode === "server" ? "server" : "browser";
  const micAvailable = micMode === "server" ? typeof MediaRecorder !== "undefined" : !!speechRecognitionCtor();
  function finishVoice(run: typeof voiceRef.current) {
    if (voiceRef.current !== run) return; // 已经换成新的识别，忽略这次收尾
    run.rec = null;
    setListening(false);
    if (!run.failed && !run.heard && run.cid) setNotice("没有听到内容。");
    run.cid = "";
    run.base = ""; run.final = ""; run.interim = "";
    if (currentRef.current) textarea.current?.focus();
  }
  function stopVoice() {
    const run = voiceRef.current;
    if (run.rec) { try { run.rec.stop(); } catch { /* 已经停止 */ } }
    const recorder = recorderRef.current;
    if (recorder.recorder && recorder.recorder.state !== "inactive") {
      try { recorder.recorder.stop(); } catch { /* 已经停止 */ }
    }
  }
  // 中止并丢弃本次识别结果（发送消息、切换会话时用，避免文字落到别的会话里）。
  function cancelVoice() {
    const run = voiceRef.current;
    if (run.rec) {
      run.cid = ""; run.heard = false; run.failed = true;
      try { run.rec.abort(); } catch { /* 已经停止 */ }
      run.rec = null;
    }
    const recorder = recorderRef.current;
    if (recorder.recorder && recorder.recorder.state !== "inactive") {
      recorder.cid = "";
      try { recorder.recorder.stop(); } catch { /* 已经停止 */ }
    }
    setListening(false);
  }
  function startBrowserVoice(cid: string) {
    const Ctor = speechRecognitionCtor();
    if (!Ctor) { setError("当前浏览器不支持语音输入，请用 Chrome / Edge / Safari，或改用「服务端转写」。"); return; }
    cancelVoice();
    const recognition = new Ctor();
    recognition.lang = voiceLang;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    const run = { rec: recognition, base: draft, final: "", interim: "", cid, heard: false, failed: false };
    voiceRef.current = run;
    recognition.onstart = () => { if (voiceRef.current !== run) return; setListening(true); setVoiceSeconds(0); };
    recognition.onresult = (event) => {
      if (voiceRef.current !== run || !run.cid) return;
      let interim = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const text = result ? result[0]?.transcript || "" : "";
        if (!result || !text) continue;
        if (result.isFinal) run.final += text; else interim += text;
      }
      run.interim = interim;
      if ((run.final + interim).trim()) run.heard = true;
      const value = voiceJoin(run.base, run.final + run.interim);
      setDrafts((prev) => ({ ...prev, [run.cid]: value }));
    };
    recognition.onerror = (event) => {
      if (voiceRef.current !== run) return;
      run.failed = true;
      if (event.error !== "aborted") setError(VOICE_ERRORS[event.error] || `语音识别出错（${event.error}）。`);
    };
    recognition.onend = () => finishVoice(run);
    try { recognition.start(); } catch { setError("无法启动语音识别，请稍后重试。"); }
  }
  // 服务端听写：每 2.5 秒把「从开始到现在的整段音频」发给识别接口；上游返回整段文字，
  // 因此直接替换草稿中的听写区间，不会重复叠加。
  async function flushVoice() {
    const run = recorderRef.current;
    if (!run.dirty || run.uploading) return;
    const cid = run.cid;
    if (!cid || !run.chunks.length) { run.dirty = false; return; }
    run.dirty = false; run.uploading = true;
    setVoiceBusy(true);
    const base = run.base;
    const mime = run.mime || "audio/webm";
    const blob = new Blob(run.chunks, { type: mime });
    try {
      const result = await api<{ text: string }>(
        `/audio/transcriptions?name=voice.${voiceExtension(mime)}&language=${encodeURIComponent(voiceLang)}`, blob);
      const text = (result.text || "").trim();
      run.text = text;
      if (recorderRef.current === run && run.cid) setDrafts((prev) => ({ ...prev, [run.cid]: voiceJoin(base, text) }));
    } catch (e) {
      // 每 2.5 秒一次请求，同一次听写只报一次错，避免刷屏。
      if (!run.failed) { run.failed = true; setError((e as Error).message); }
    } finally {
      run.uploading = false;
      setVoiceBusy(false);
      if (run.dirty && run.recorder) void flushVoice(); // 上传期间又攒了新分片
    }
  }
  async function startServerVoice(cid: string) {
    const ready = voiceConfig?.mode === "server" && !!voiceConfig.model
      && (voiceConfig.source === "custom" ? !!voiceConfig.base_url : !!voiceConfig.provider);
    if (!ready) {
      setError("尚未配置语音识别接口：请在「设置 → 访问 → 语音输入」中选择接口来源，并填写地址与转写型号。");
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("当前浏览器不支持录音，请改用「浏览器识别」或 Chrome / Safari。");
      return;
    }
    cancelVoice();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const type = voiceAudioType();
      const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      const run: typeof recorderRef.current = { recorder, chunks: [], stream, cid, base: draft,
        mime: recorder.mimeType || type || "audio/webm", bytes: 0, dirty: false, uploading: false, text: "", failed: false };
      recorderRef.current = run;
      recorder.ondataavailable = (event) => {
        if (!event.data.size) return;
        run.chunks.push(event.data);
        run.bytes += event.data.size;
        run.dirty = true;
        if (run.bytes > 20 * 1024 * 1024) { // 到上限主动收尾，避免上游拒收
          setNotice("听写已到长度上限，已结束本次输入。");
          if (run.recorder && run.recorder.state !== "inactive") run.recorder.stop();
          return;
        }
        void flushVoice();
      };
      recorder.onstop = () => {
        if (recorderRef.current !== run) { stream.getTracks().forEach((track) => track.stop()); return; }
        run.stream?.getTracks().forEach((track) => track.stop());
        run.stream = null; run.recorder = null;
        setListening(false);
        if (!run.cid) { run.chunks = []; return; }
        run.dirty = true;
        void flushVoice();
      };
      recorder.start(2500);
      setListening(true); setVoiceSeconds(0);
    } catch (e) {
      setError(`无法访问麦克风：${(e as Error).message}`);
    }
  }
  function toggleVoice() {
    if (listening) { stopVoice(); return; }
    if (!current || sending || uploading || voiceBusy) return;
    if (micMode === "server") void startServerVoice(current);
    else startBrowserVoice(current);
  }
  useEffect(() => {
    let alive = true;
    const load = () => {
      setVoiceLang(readVoiceLang());
      void api<VoiceSettingsResponse>("/models/voice")
        .then((result) => { if (alive) setVoiceConfig(result.voice); })
        .catch(() => { if (alive) setVoiceConfig(EMPTY_VOICE); });
    };
    load();
    window.addEventListener("carme:voice-updated", load);
    return () => { alive = false; window.removeEventListener("carme:voice-updated", load); };
  }, [connected]);
  useEffect(() => { cancelVoice(); /* 切换会话时停止识别，避免文字写进另一个会话 */ }, [current]);
  useEffect(() => {
    if (!listening) return;
    const timer = setInterval(() => setVoiceSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [listening]);
  useEffect(() => () => {
    voiceRef.current.rec?.abort();
    const recorder = recorderRef.current;
    if (recorder.recorder && recorder.recorder.state !== "inactive") recorder.recorder.stop();
    recorder.stream?.getTracks().forEach((track) => track.stop());
  }, []);
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
  const quickCreateBot = async () => {
    const result = await api<{ agent: Agent }>("/agents/quick", {}, "POST");
    await refreshAgents();
    setPanel(null);
    await createConversation([result.agent.id]);
    setNotice(`已创建 ${result.agent.name}（引擎 ${result.agent.engine === "api" ? "API 网关" : result.agent.engine}）。描述与模型可在 Bot 设置中随时修改。`);
  };
  const openComputerPanel = () => {
    setPanel("screen");
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
              {ACCOUNT_NAME ? `${ACCOUNT_NAME} 的工作空间` : "我的工作空间"}
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
            <span className="avatar-anchor">
              <Avatar agent={primary} group={group} working={primaryWorking} />
              {!connected && !loading && (
                <span className="avatar-net-badge" role="status" title="连接中断，正在自动重连" aria-label="连接中断，正在自动重连">!</span>
              )}
            </span>
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
            <IconButton label="设置" onClick={() => setPanel("settings")}>
              <SlidersHorizontal size={19} />
            </IconButton>
            <IconButton
              label="聊天详情"
              active={showDetails}
              onClick={() => {
                setShowDetails(!showDetails);
                if (document.documentElement.clientWidth <= 700) setMobileView("details");
              }}
            >
              <MoreHorizontal size={21} />
            </IconButton>
          </div>
        </header>
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
        <div className="message-area">
        <div
          className="message-scroll"
          ref={messageScroll}
          onScroll={(e) => {
            const el = e.currentTarget;
            atBottom.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 100;
            measureScrollHints();
          }}
        >
          {!current ? (
            <div className="welcome">
              <img
                className="welcome-mark"
                src="/icon-192.png"
                alt=""
                width={84}
                height={84}
              />
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
                      {message.status === "streaming" ? (
                        <p className="thinking-note" role="status">正在思考... ...</p>
                      ) : (
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
                            img: ({ src, alt }) => {
                              const match = typeof src === "string" && src.match(/\/api\/conversations\/([^/]+)\/attachments\/([^/?#]+)\/download/);
                              const file = match && decodeURIComponent(match[1]) === selected.conversation.id
                                ? (selected.files || []).find((item) => item.id === decodeURIComponent(match[2]) && item.mime.startsWith("image/"))
                                : undefined;
                              return file
                                ? <InlineImage file={file} onPreview={setPreviewFile} />
                                : <span className="inline-image-note">[图片：{alt || "附件"}]</span>;
                            },
                          }}
                        >
                          {message.content}
                        </Markdown>
                      )}
                      {message.attachments?.map((file) => (file.mime.startsWith("image/")
                        ? <InlineImage key={file.id} file={file} onPreview={setPreviewFile} />
                        : <FileCard key={file.id} file={file} onPreview={setPreviewFile} />))}
                    </div>
                    <div className="message-meta">
                      <time>{formatTime(message.created_at)}</time>
                      {message.model && <span title={message.provider && ["codex", "pi", "claude"].includes(message.provider) ? "本次任务使用的 CLI 引擎与模型设置" : "由实际 API 响应记录；供应商可能返回模型别名"}>{message.provider} / {message.model}</span>}
                      {message.status === "interrupted" && <span className="stream-state">已中断 · 部分回复</span>}
                      {message.role === "user" && <CheckCheck size={13} />}
                    </div>
                  </div>
                </article>
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
            </div>
          )}
        </div>
        {scrollHints.scrollable && (!scrollHints.top || !scrollHints.bottom) && (
          <div className="scroll-hints">
            {!scrollHints.top && (
              <button type="button" className="scroll-hint" aria-label="回到顶部" title="回到顶部" onClick={() => scrollMessages("top")}>
                <ArrowUp size={15} />
              </button>
            )}
            {!scrollHints.bottom && (
              <button type="button" className="scroll-hint" aria-label="回到底部" title="回到底部" onClick={() => scrollMessages("bottom")}>
                <ArrowDown size={15} />
              </button>
            )}
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
              onChange={(e) => {
                const value = e.target.value;
                // 手动修改时以新内容为基线，避免下一次识别结果覆盖用户的编辑。
                if (listening) {
                  voiceRef.current.base = value;
                  voiceRef.current.final = "";
                  voiceRef.current.interim = "";
                }
                setDrafts((prev) => ({ ...prev, [current]: value }));
              }}
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
              <input ref={uploadInput} type="file" multiple hidden accept=".pdf,.docx,.xlsx,.zip,.txt,.md,.csv,.json,.html,.png,.jpg,.jpeg,.webp" onChange={(event) => void uploadFiles(event.target.files)} />
              <IconButton
                label="添加附件"
                onClick={() => { if (!uploading && !sending && current) uploadInput.current?.click(); }}
              >
                <Paperclip size={19} />
              </IconButton>
              {micAvailable && (
                <IconButton
                  label={listening ? "停止语音输入" : voiceBusy ? "正在转写语音" : "开始语音输入"}
                  className={`mic-button${listening ? " listening" : ""}`}
                  pressed={listening}
                  disabled={!current || sending || uploading || (!listening && voiceBusy)}
                  onClick={toggleVoice}
                >
                  <Mic size={19} />
                </IconButton>
              )}
              <span className={`composer-hint${listening ? " listening" : ""}`}>
                {listening ? `${micMode === "server" ? "正在听写" : "正在听"}…（${voiceSeconds}s）${voiceBusy ? " · 转写中" : ""}说完点麦克风结束` : voiceBusy ? "正在转写…" : uploading ? "正在上传并解析…" : running.length ? "可以继续补充要求" : "文字、文档与图片"}
              </span>
              <button
                className={`send-button${!sending && running.length ? " stopping" : ""}`}
                type={!sending && running.length ? "button" : "submit"}
                disabled={!current || sending || (!running.length && uploading) || (!running.length && !draft.trim() && !draftFiles.length)}
                aria-label={!sending && running.length ? "停止任务" : "发送消息"}
                title={!sending && running.length ? "停止当前任务" : undefined}
                onClick={
                  !sending && running.length
                    ? () => {
                        for (const task of running)
                          if (!task.parent_id)
                            void perform(() => api(`/tasks/${encodeURIComponent(task.id)}/cancel`, {}), "已请求停止任务");
                      }
                    : undefined
                }
              >
                {sending ? (
                  <LoaderCircle className="spin" size={19} />
                ) : running.length ? (
                  <Square size={17} />
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
              PDF（未加密，≤100 页）、DOCX、UTF-8 TXT / MD / CSV / JSON / HTML、PNG / JPEG / WebP；每个 ≤10 MB，每条消息 ≤4 个。文本最多 50 万字符。图片边长 ≤8192 px、总像素 ≤2400 万，处理为最长边 2048 px 的 JPEG，动图仅首帧。扫描 PDF 未做 OCR；图片理解需要视觉模型。发送后附件内容会随请求交给所选模型。语音输入需 Chrome / Edge / Safari（Firefox 可改用「服务端转写」）；识别由浏览器或你在「设置 → 访问 → 语音输入」中选择的转写连接完成，首次使用需授权麦克风，本机 http 直连需为 localhost 或 https。
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
              <ComputerPreviewFrame
                title={activeCliTask ? "受管推理引擎" : currentNode?.name || "尚未连接执行电脑"}
                note={activeCliTask ? activeCliTask.engine_workspace || "Bot 独立工作目录" : currentNode ? "远端桌面暂未接入" : "正在连接本机屏幕…"}
                footerIdle={activeCliTask ? "任务执行边界" : currentNode ? "桌面未连接" : "等待屏幕画面"}
              />
            </button>
            <p className="detail-note">
              {activeCliTask
                ? `当前任务使用专用 runtime；历史工作目录记录：${activeCliTask.engine_workspace || "Bot 独立目录"}。`
                : activeTask
                ? `当前任务${currentNode ? `固定使用 ${currentNode.name}` : "未绑定执行电脑"}。`
                : "执行电脑暂定为 2018 MacBook，可随时更换。"}{" "}
              {activeCliTask ? " 执行目标与权限修改只对新授权生效。" : " 更换默认电脑只影响新任务。"}
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
          {!!tasks.length && (
            <section className="detail-section">
              <button type="button" className="section-toggle" aria-expanded={tasksOpen} onClick={() => setTasksOpen((open) => !open)}>
                <span className="section-title">任务与验收</span>
                <span className="section-count">{tasks.length}</span>
                {tasksOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              </button>
              <div className="section-body" hidden={!tasksOpen}>
              {tasks.map((task) => (
                <div key={task.id} className={`task-activity ${task.status === "failed" ? "failed" : ""}`}>
                  {isRunning(task) ? (
                    <Avatar agent={agentOf(task.agent_id)} working={task.status === "running"} />
                  ) : <span className="activity-icon"><Info size={16} /></span>}
                  <div>
                    <strong>
                      {agentOf(task.agent_id)?.name || task.agent_id}{" "}
                      <span>{statusText[task.status] || task.status}</span>
                    </strong>
                    {task.parent_id && <small>成员协作 · {task.title || "子任务"}</small>}
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
                          () => api(`/tasks/${encodeURIComponent(task.id)}/cancel`, {}),
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
              {tasks.filter((t) => !isRunning(t)).map((task) => <TaskEvidence key={task.id} taskId={task.id} title={task.title || task.id} />)}
              </div>
            </section>
          )}
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
          onQuickBot={quickCreateBot}
        />
      )}
      {panel === "screen" && (
        <ComputerScreenDialog onClose={() => setPanel(null)} />
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
      {previewFile && (previewFile.mime.startsWith("image/")
        ? <ImageLightbox file={previewFile} onClose={() => setPreviewFile(null)} />
        : <FilePreview file={previewFile} onClose={() => setPreviewFile(null)} />)}
      {panel === "market" && (
        <Modal title="探索 Bot" onClose={() => setPanel(null)} wide>
          <div className="modal-body">
            <div className="history-tabs" role="tablist" aria-label="探索 Bot">
              {([["bots", "Bot 团队"], ["skills", "已安装的 Skill"], ["mcp", "已安装的 MCP"]] as const).map(([id, label]) => (
                <button key={id} role="tab" aria-selected={marketTab === id} onClick={() => setMarketTab(id)}>{label}</button>
              ))}
            </div>
            {marketTab === "bots" && <>
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
                  技能与 MCP 管理已放到上方标签页。你也可以创建自己的
                  Bot，指定角色、模型和可用工具。
                </p>
              </div>
              <button
                onClick={() => void perform(quickCreateBot)}
              >
                <Plus size={16} />
                一键创建 Bot（使用默认引擎与模型）
              </button>
              <button
                className="text-button"
                onClick={() => {
                  setEditingAgent(null);
                  setPanel("bot");
                }}
              >
                <SlidersHorizontal size={14} />
                自定义创建
              </button>
            </>}
            {marketTab === "skills" && <SkillPanel onAgentsChanged={() => void refreshAgents()} />}
            {marketTab === "mcp" && <McpPanel onAgentsChanged={() => void refreshAgents()} />}
          </div>
        </Modal>
      )}
      {panel === "settings" && (
        <SettingsDialog
          stats={stats}
          connected={connected}
          onModelsSaved={() => { void api<Stats>("/stats").then(setStats); }}
          onMigrated={() => { void refreshAgents(); void refreshList(); void api<Stats>("/stats").then(setStats); }}
          onClose={() => setPanel(null)}
          onConnect={(value) => {
            token = value;
            sessionReady = undefined;
            void connectSession().then(() => {
              setError(""); setAuthVersion((v) => v + 1); setPanel(null);
            }).catch((e) => setError(e.message));
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
  onQuickBot,
}: {
  agents: Agent[];
  onCreate: (ids: string[], title?: string) => Promise<void>;
  onClose: () => void;
  onNewBot: () => void;
  onQuickBot: () => Promise<void>;
}) {
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [quickBusy, setQuickBusy] = useState(false);
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
  async function quickBot() {
    setQuickBusy(true);
    setError("");
    try {
      await onQuickBot();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setQuickBusy(false);
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
          <button disabled={quickBusy || busy} onClick={() => void quickBot()}>
            <span className="action-disc">
              {quickBusy ? <LoaderCircle size={19} className="spin" /> : <Plus size={19} />}
            </span>
            <strong>{quickBusy ? "正在创建…" : "创建 Bot"}</strong>
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
        <button type="button" className="text-button new-bot-advanced" onClick={onNewBot}>
          <SlidersHorizontal size={14} />
          自定义创建（手动填写引擎、模型与角色）
        </button>
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
            <LocalDesktopView />
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
    known_hosts_file: node?.known_hosts_file || "",
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
          显式授权后端使用此密钥文件。留空不能连接，不会读取默认密钥或 SSH agent。不要粘贴私钥内容。
        </small>
      </label>
      <label className="form-label">SSH known_hosts 文件路径
        <input value={data.known_hosts_file} autoCapitalize="off" onChange={(e) => field("known_hosts_file", e.target.value)} />
        <small>填写已核实主机指纹的文件路径。未登记的主机拒绝连接。</small>
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
          连接；本机屏幕查看与鼠标控制见「执行电脑」顶部的「本机屏幕」。
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
      headers: authHeaders(),
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

function InlineImage({ file, onPreview }: { file: ChatFile; onPreview: (file: ChatFile) => void }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    let objectUrl = "";
    void (async () => {
      try {
        objectUrl = URL.createObjectURL(await fileBlob(file));
        if (alive) setUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => { alive = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [file.id, file.conversation_id]);
  if (failed) return <FileCard file={file} onPreview={onPreview} />;
  return <button type="button" className="message-image-button" aria-label={`放大查看 ${file.name}`}
      title={`${file.name} · 点击放大查看`} onClick={() => onPreview(file)}>
    {url ? <img className="message-image" src={url} alt={file.name} loading="lazy" />
      : <span className="message-image loading">正在载入图片…</span>}
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

function ImageLightbox({ file, onClose }: { file: ChatFile; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const closeRef = useRef(onClose);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actualSize, setActualSize] = useState(false);
  const [zoomable, setZoomable] = useState(false);
  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => {
    let alive = true, objectUrl = "";
    setLoading(true); setError(""); setUrl(""); setActualSize(false); setZoomable(false);
    void (async () => {
      try {
        objectUrl = URL.createObjectURL(await fileBlob(file));
        if (alive) setUrl(objectUrl); else URL.revokeObjectURL(objectUrl);
      } catch (e) { if (alive) setError((e as Error).message); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [file.id, file.conversation_id, attempt]);
  useEffect(() => {
    const opened = dialog.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    opened?.showModal();
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeRef.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      opened?.close();
      const active = document.activeElement;
      if (previous?.isConnected && (!active || active === document.body)) previous.focus();
    };
  }, []);
  async function download() {
    setBusy(true);
    try {
      const objectUrl = URL.createObjectURL(await fileBlob(file));
      const link = document.createElement("a"); link.href = objectUrl; link.download = file.name;
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function measure(image: HTMLImageElement) {
    const box = image.getBoundingClientRect();
    if (!box.width || !box.height || !image.naturalWidth || !image.naturalHeight) return;
    const scale = Math.min(box.width / image.naturalWidth, box.height / image.naturalHeight);
    setZoomable(scale < 0.92 || scale > 1.08);
  }
  return <dialog
    // 兜底：任何原生关闭路径（浏览器自己处理的 Esc 等）都要把 React 状态一起收掉，
    // 否则 dialog 关了而 previewFile 还在，界面会卡成一个看不见的弹窗。
    onClose={onClose}
    ref={dialog}
    className="lightbox"
    role="dialog"
    aria-modal="true"
    aria-label={`放大查看 ${file.name}`}
    onCancel={onClose}
    onClick={(event) => {
      const target = event.target;
      if (target instanceof HTMLButtonElement) return;
      if (target instanceof HTMLImageElement) { if (zoomable) setActualSize((value) => !value); return; }
      if (target instanceof HTMLElement && target.closest(".lightbox-error")) return;
      onClose();
    }}
  >
    <div className="lightbox-actions">
      {zoomable && !loading && !error && <button type="button" className="lightbox-action" aria-pressed={actualSize}
        aria-label={actualSize ? "适应窗口" : "查看原始大小"} title={actualSize ? "适应窗口" : "查看原始大小"}
        onClick={() => setActualSize((value) => !value)}>{actualSize ? <ZoomOut size={19} /> : <ZoomIn size={19} />}</button>}
      <button type="button" className="lightbox-action lightbox-download" aria-label="下载原图" title="下载原图"
        aria-busy={busy} disabled={busy} onClick={() => void download()}>{busy ? <LoaderCircle size={19} className="spin" /> : <Download size={19} />}</button>
      <button type="button" ref={closeButton} className="lightbox-action" aria-label="关闭放大查看" title="关闭放大查看" onClick={onClose}><X size={20} /></button>
    </div>
    <div className={`lightbox-stage${actualSize ? " actual" : ""}`}>
      {loading ? <p className="lightbox-status"><LoaderCircle size={20} className="spin" />正在载入图片…</p>
        : error ? <div className="lightbox-error">
          <p role="alert">{error}</p>
          {isAccessLoginError(error)
            ? <button type="button" className="text-button" onClick={reopenAccessEntry}>重新打开保护入口</button>
            : <button type="button" className="text-button" onClick={() => setAttempt((value) => value + 1)}>重新载入</button>}
        </div>
        : <img className="lightbox-image" src={url} alt={file.name} decoding="async" onLoad={(event) => measure(event.currentTarget)} />}
    </div>
    <p className="lightbox-caption">
      <span className="lightbox-name">{file.name}</span>
      {zoomable && !loading && !error && <span className="lightbox-hint">{actualSize ? "原始大小 · 点击图片恢复适应窗口" : "适应窗口 · 点击图片查看原始大小"}</span>}
    </p>
  </dialog>;
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
    emoji: agent?.emoji || "",
    title: agent?.title || "",
    prompt: agent?.prompt || "",
    tier: agent?.tier || "balanced",
    model: agent?.model || "",
    effort: agent?.effort || "",
    engine: agent?.engine || "api",
    engine_model: agent?.engine_model || "",
    engine_effort: agent?.engine_effort || "",
    engine_workspace: agent?.engine_workspace || "",
    runtime_profile: agent?.runtime_profile || "",
    execution_target: agent?.execution_target || "none",
    execution_target_id: agent?.execution_target_id || "",
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
  const [notice, setNotice] = useState("");

  async function exportThisBot(agentId: string) {
    setError(""); setNotice("");
    try {
      const bundle = await api<ExportBundle>(`/agents/${encodeURIComponent(agentId)}/export`);
      downloadJson(`carme-bot-${agentId}-${fileStamp()}.json`, bundle);
      setNotice("已导出该 Bot 的角色设定、记忆与它参与的对话上下文。");
    } catch (e) { setError((e as Error).message); }
  }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    void Promise.all([api<ModelSettings>("/models"), api<EngineSettings>("/engines")])
      .then(([modelResult, engineResult]) => { setModels(modelResult.models); setEngines(engineResult.engines); })
      .catch((e) => setError(e.message));
  }, []);
  // 下拉列表只列「当前可用」的选项：Agent 要已连接，模型要已配置 API key。
  const selectedModel = models.find((model) => model.ref === data.model);
  const savedCliEngine = data.engine && data.engine !== "api" ? data.engine : null;
  const piEngine = engines.find((engine) => engine.id === "pi");
  const engineOptions: EngineInfo[] = engines.length ? connectedAgents(engines, data.engine) : [
    { id: "api", label: "Carme API 网关", installed: true, ready: true, status: "ready", version: "内置", auth_status: "configured", capability: "Carme 工具与现有模型配置" },
    ...(savedCliEngine ? [{ id: savedCliEngine, label: `${savedCliEngine} CLI（检测中）`, installed: true, ready: false, status: "detected", version: "", auth_status: "unknown", capability: "" }] : []),
  ];
  const modelOptions = availableModels(models, data.model);
  const modelOptionGroups = modelGroups(modelOptions);
  const modelOption = (model: SavedModel) => <option key={model.ref} value={model.ref}>{model.id}{model.effort ? ` · 默认 ${model.effort}` : ""}{model.available ? "" : "（当前不可用）"}</option>;
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
        <div className="bot-form-heading">
        {agent && <div className="bot-export-row">
          <button type="button" className="secondary-button" onClick={() => setMemoryOpen(true)}>管理此 Bot 的记忆</button>
          <button type="button" className="secondary-button" onClick={() => void exportThisBot(agent.id)}>
            <Download size={15} />导出此 Bot
          </button>
        </div>}
        {notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
          <button
            type="button"
            className="avatar-edit-button"
            aria-label="更换头像"
            aria-expanded={avatarOpen}
            onClick={() => setAvatarOpen((open) => !open)}
          >
            <Avatar agent={{ name: data.name, emoji: data.emoji, avatar: data.avatar }} size="hero" />
            <span className="avatar-pencil" aria-hidden="true"><Pencil size={14} /></span>
          </button>
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
            显示 Emoji（只影响 CLI 名称，不再用于头像）
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
          使用的 Agent（引擎）
          <select aria-label="使用的 Agent" value={data.engine} onChange={(e) => setData({ ...data, engine: e.target.value as Agent["engine"] })}>
            {!engineOptions.length && <option value={data.engine}>{data.engine === "api" ? "Carme API 网关（暂无可用模型）" : `${data.engine} CLI（当前未连接）`}</option>}
            {engineOptions.map((engine) => <option key={engine.id} value={engine.id}>{engine.label}{[engine.provider, engine.model].filter(Boolean).length ? ` · ${[engine.provider, engine.model].filter(Boolean).join(" / ")}` : ""}{engine.ready ? "" : "（当前未连接）"}</option>)}
          </select>
          <small>{data.engine === "api" ? "使用 Carme API 网关、已配置的模型和执行电脑。" : "使用 Carme 专用受管引擎；执行目标和权限会在任务创建时快照。"}列表只显示当前已连接的 Agent，可在「设置 → 模型」重新检测。</small>
        </label>
        {data.engine !== "api" && <div className="engine-config-card">
          {data.engine === "pi" ? <>
            <label className="form-label">Carme Runtime Profile
              <select value={data.runtime_profile} onChange={(e) => setData({ ...data, runtime_profile: e.target.value })}>
                <option value="">未指定（先在「模型」页测试 Pi 自动登记）</option>
                {piEngine?.profile && <option value={piEngine.profile}>{piEngine.profile}（{piEngine.model || "profile 模型"}）</option>}
                {data.runtime_profile && data.runtime_profile !== piEngine?.profile && <option value={data.runtime_profile}>{data.runtime_profile}（当前保存值）</option>}
              </select>
              <small>登记自动完成：测试 Pi 连接时自动写入，无需手填。</small>
            </label>
            <label className="form-label">CLI 模型
              <select value={data.engine_model} onChange={(e) => setData({ ...data, engine_model: e.target.value })}>
                <option value="">跟随 profile 显式模型（推荐）</option>
                {piEngine?.model && <option value={piEngine.model}>{piEngine.model}</option>}
                {data.engine_model && data.engine_model !== piEngine?.model && <option value={data.engine_model}>{data.engine_model}（当前保存值）</option>}
              </select>
              <small>Pi 的模型由 profile 固定；留空即跟随 profile。</small>
            </label>
          </> : <>
            <label className="form-label">
              CLI 模型（可选）
              <input value={data.engine_model} maxLength={200} autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder="由 Carme profile 显式指定模型" onChange={(e) => setData({ ...data, engine_model: e.target.value })} />
              <small>这里填写 CLI 自己支持的模型名或别名，不会改变 Carme API 模型列表。</small>
            </label>
            <label className="form-label">Carme Runtime Profile
              <input value={data.runtime_profile} placeholder="pi-managed-v1" onChange={(e) => setData({ ...data, runtime_profile: e.target.value })} />
              <small>使用管理员登记的固定版本与专用身份。个人 Pi 配置不会导入。</small>
            </label>
          </>}
        </div>}
        <label className="form-label">执行目标
          <select aria-label="执行目标" value={data.execution_target} onChange={(e) => setData({ ...data, execution_target: e.target.value })}>
            <option value="none">未分配（仅推理）</option><option value="container">Docker（文件 / 命令 / Web）</option>
            <option value="ssh">已登记的 SSH 节点</option><option value="macos">Mac Runner（真实 Mac，需配对授权）</option>
          </select>
          <input aria-label="执行目标 ID" value={data.execution_target_id} placeholder="已登记的目标 ID" onChange={(e) => setData({ ...data, execution_target_id: e.target.value })} />
        </label>
        {agent?.boundary && <div className="form-help" aria-label="实际权限边界">
          <p>已保存的引擎：{agent.engine} · Profile：{agent.runtime_profile || "未登记"} · 版本：{agent.boundary.runtime?.version || (agent.engine === "api" ? "内置" : "未验证")} · 身份引用：{agent.boundary.runtime?.credential_ref || (agent.engine === "api" ? agent.model || agent.tier : "未登记")}</p>
          {agent.boundary.runtime && <p>执行器状态：{agent.boundary.runtime.status} · 认证状态：{agent.boundary.runtime.auth_status}</p>}
          <p>执行目标：{agent.boundary.execution_target || "none"} · 隔离模式：{agent.boundary.policy?.isolation_mode || "未验证"}</p>
          {agent.boundary.error ? <p>{agent.boundary.error}</p> : <>
            <p>可见目录：{agent.boundary.policy?.visible_directories.join(", ") || "无"} · 网络：{agent.boundary.policy?.network || "none"}</p>
            <p>实际工具：{agent.boundary.policy?.tools.join(", ") || "无"}</p>
          </>}
        </div>}
        <label className="form-label">
          使用的大模型
          <select aria-label="使用的大模型" disabled={data.engine !== "api"} value={data.model} onChange={(e) => setData({ ...data, model: e.target.value, effort: "" })}>
            <option value="">按模型档位自动选择</option>
            {data.model && !modelOptions.some((model) => model.ref === data.model) && <option value={data.model}>{data.model}（原配置，不在已配置列表中）</option>}
            {modelOptionGroups.length > 1
              ? modelOptionGroups.map((group) => <optgroup key={group.label} label={group.label}>{group.items.map(modelOption)}</optgroup>)
              : modelOptionGroups.flatMap((group) => group.items.map(modelOption))}
          </select>
          <small>{data.engine !== "api" ? "当前 Agent 不使用 Carme API 模型；此前选好的模型会保留，切回 API 网关后继续可用。" : modelOptions.some((model) => model.available) ? `只列出已经配置好 API key 的模型（${modelOptions.filter((model) => model.available).length} 个），effort 跟随所选模型。` : "还没有可用模型：请先在「设置 → 模型」添加连接并验证 key。"}</small>
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
              {data.effort && !selectedModel?.effort_options.includes(data.effort) && <option value={data.effort}>{data.effort}（原配置）</option>}
              {selectedModel?.effort_options.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
            </select>
            <small>{data.engine !== "api" ? "API effort 只对 Carme API 模型生效。" : !data.model ? "先选模型，再选 effort。" : selectedModel?.effort_options.length ? "档位来自该模型的连接设置。" : "这个模型没有可选档位，按连接默认执行。"}</small>
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
          模型身份由 Carme 专用配置提供。所有引擎共用任务权限和显式执行目标，未配置目标或目标不可用时执行失败关闭。
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
const SETTINGS_TABS = [
  { id: "general", label: "通用", icon: Settings2 },
  { id: "models", label: "模型", icon: Cpu },
  { id: "agents", label: "Agent", icon: Users },
  { id: "migration", label: "迁移", icon: Download },
  { id: "notify", label: "通知", icon: Bell },
  { id: "access", label: "访问", icon: Globe },
  { id: "about", label: "关于", icon: CircleHelp },
] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number]["id"];

function AccountPasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (next.length < 15 || next.length > 128) return setError("新密码需要 15–128 个字符");
    if (next !== confirm) return setError("两次新密码不一致");
    if (next === current) return setError("新密码需要与当前密码不同");
    setBusy(true);
    try {
      // Gateway-owned route (never proxied to the account backend); revokes every device session.
      await api("/password", { current_password: current, new_password: next });
      setCurrent(""); setNext(""); setConfirm(""); setDone(true);
      window.setTimeout(() => accountChanged(), 1600);
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改密码失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="settings-section">
      <h3>
        <ShieldCheck size={17} />
        账号密码
      </h3>
      {done ? (
        <p className="form-success" role="status">
          <Check size={16} />
          密码已修改；该账号在所有设备上的登录会话已撤销，正在重新打开登录入口…
        </p>
      ) : (
        <form className="settings-form" onSubmit={submit}>
          <label className="form-label">
            当前密码
            <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
          </label>
          <label className="form-label">
            新密码（15–128 位）
            <input type="password" autoComplete="new-password" minLength={15} maxLength={128} value={next} onChange={(e) => setNext(e.target.value)} required />
          </label>
          <label className="form-label">
            确认新密码
            <input type="password" autoComplete="new-password" minLength={15} maxLength={128} value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
          </label>
          <button type="submit" className="secondary-button" disabled={busy}>
            {busy ? "保存中…" : "修改密码"}
          </button>
          <p className="form-help">修改后会撤销该账号在所有设备上的登录会话（包括这台），需要用新密码重新登录。忘记当前密码请联系管理员重置。</p>
          {error && <p role="alert" className="form-error">{error}</p>}
        </form>
      )}
    </div>
  );
}

function VoiceInputSettings() {
  const [config, setConfig] = useState<VoiceConfig>(EMPTY_VOICE);
  const [hasKey, setHasKey] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [preset, setPreset] = useState("custom");
  const [connections, setConnections] = useState<Connection[]>([]);
  const [models, setModels] = useState<SavedModel[]>([]);
  const [lang, setLang] = useState(readVoiceLang);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const testRef = useRef<{ recorder: MediaRecorder | null; stream: MediaStream | null }>({ recorder: null, stream: null });
  useEffect(() => {
    void api<VoiceSettingsResponse>("/models/voice").then((data) => {
      setConfig(data.voice);
      setHasKey(data.has_key);
      setPreset(ASR_PRESETS.find((item) => item.base_url && item.base_url === data.voice.base_url)?.id || "custom");
    }).catch(() => {});
    void api<ModelSettings>("/models").then((data) => { setConnections(data.connections); setModels(data.models); }).catch(() => {});
  }, []);
  useEffect(() => {
    if (!recording) return;
    const timer = setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [recording]);
  useEffect(() => () => {
    const run = testRef.current;
    if (run.recorder && run.recorder.state !== "inactive") run.recorder.stop();
    run.stream?.getTracks().forEach((track) => track.stop());
  }, []);
  const supported = !!speechRecognitionCtor();
  const suggested = voiceModelOptions(config, models);
  async function save() {
    setBusy(true); setError(""); setNotice("");
    try {
      const data = await api<VoiceSettingsResponse>("/models/voice", {
        mode: config.mode, source: config.source, provider: config.provider, model: config.model,
        base_url: config.base_url, api_key: apiKey,
      }, "PUT");
      setConfig(data.voice); setHasKey(data.has_key); setApiKey("");
      window.dispatchEvent(new Event("carme:voice-updated"));
      setNotice(data.voice.mode === "server" && !data.has_key ? "设置已保存，但还没有可用的密钥。" : "语音输入设置已保存。");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function updateLang(value: string) {
    setLang(value);
    saveStorage("carme_voice_lang", value === "zh-CN" ? "" : value);
    window.dispatchEvent(new Event("carme:voice-updated"));
  }
  function applyPreset(id: string) {
    setPreset(id);
    const item = ASR_PRESETS.find((entry) => entry.id === id);
    if (!item) return;
    setConfig({ ...config, source: "custom", base_url: item.base_url, model: item.models[0] || config.model });
  }
  async function toggleTest() {
    if (recording) {
      const run = testRef.current;
      if (run.recorder && run.recorder.state !== "inactive") run.recorder.stop();
      return;
    }
    const ready = !!config.model && (config.source === "custom" ? !!config.base_url : !!config.provider);
    if (!ready) { setError("请先选择接口来源，并填写接口地址与转写型号，再测试。"); return; }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") { setError("当前浏览器不支持录音，无法测试。"); return; }
    setError(""); setNotice(""); setResult("");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const type = voiceAudioType();
      const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
      const chunks: Blob[] = [];
      testRef.current = { recorder, stream };
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        testRef.current = { recorder: null, stream: null };
        setRecording(false);
        const mime = recorder.mimeType || type || "audio/webm";
        const blob = new Blob(chunks, { type: mime });
        if (blob.size < 1200) { setResult("录音太短，没有听到内容。"); return; }
        setBusy(true);
        void api<{ text: string }>(`/audio/transcriptions?name=voice.${voiceExtension(mime)}&language=${encodeURIComponent(lang)}`, blob)
          .then((data) => setResult((data.text || "").trim() || "（没有识别出文字）"))
          .catch((e) => setError((e as Error).message))
          .finally(() => setBusy(false));
      };
      recorder.start();
      setSeconds(0); setRecording(true);
    } catch (e) { setError(`无法访问麦克风：${(e as Error).message}`); }
  }
  return <div className="settings-section">
    <h3><Mic size={17} />语音输入</h3>
    <p className="form-help">聊天输入框的麦克风按钮：说话转成文字后仍由你确认再发送。服务端转写会边说边更新文字。</p>
    <div className="form-grid">
      <label className="form-label">识别方式<select aria-label="语音识别方式" value={config.mode} disabled={busy}
        onChange={(e) => {
          const mode = e.target.value === "server" ? "server" : "browser";
          // 还没配过任何接口时，默认走「直接填写语音识别接口」，少一步切换。
          const source = mode === "server" && !config.provider && !config.base_url ? "custom" : config.source;
          setConfig({ ...config, mode, source });
        }}>
        <option value="browser">浏览器识别（Chrome / Edge / Safari）</option>
        <option value="server">服务端转写（可配置任意语音识别接口）</option>
      </select><small>{supported ? "浏览器识别由 Chrome（Google）或 Safari（Apple）完成，音频不经过 Carme 后端。" : "当前浏览器不支持浏览器识别，建议改用服务端转写。"}</small></label>
      <label className="form-label">识别语言<select aria-label="语音识别语言" value={lang} onChange={(e) => updateLang(e.target.value)}>
        {VOICE_LANGS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select><small>保存在此设备。</small></label>
    </div>
    {config.mode === "server" && <>
      <label className="form-label">接口来源<select aria-label="语音识别接口来源" value={config.source} disabled={busy}
        onChange={(e) => setConfig({ ...config, source: e.target.value === "custom" ? "custom" : "connection" })}>
        <option value="custom">直接填写语音识别接口</option>
        <option value="connection">已配置的模型连接</option>
      </select><small>两者都调用 OpenAI 兼容的 /audio/transcriptions。</small></label>
      {config.source === "custom" ? <>
        <label className="form-label">服务商预设<select aria-label="语音识别服务商预设" value={preset} disabled={busy}
          onChange={(e) => applyPreset(e.target.value)}>
          {ASR_PRESETS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select><small>选择后自动填好接口地址与常见型号，可再手动修改。</small></label>
        <label className="form-label">接口地址<input aria-label="语音识别接口地址" value={config.base_url} disabled={busy}
          placeholder="https://api.siliconflow.cn/v1" onChange={(e) => setConfig({ ...config, base_url: e.target.value })} /></label>
        <label className="form-label">API Key<input type="password" aria-label="语音识别 API Key" value={apiKey} disabled={busy}
          autoComplete="off" placeholder={hasKey ? "已保存密钥（留空表示不修改）" : "粘贴语音识别接口的密钥"}
          onChange={(e) => setApiKey(e.target.value)} />
          <small>{hasKey ? "密钥已保存在后端，不会回显。" : "密钥只写入后端 .env，不写进配置文件，也不会回显。"}</small></label>
      </> : <label className="form-label">模型连接<select aria-label="语音转写模型连接" value={config.provider} disabled={busy}
        onChange={(e) => setConfig({ ...config, provider: e.target.value })}>
        <option value="">请选择已配置的连接…</option>
        {connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.label}{connection.has_key ? "" : "（缺少密钥）"}</option>)}
      </select><small>该连接必须支持 /audio/transcriptions。</small></label>}
      <label className="form-label">转写型号<input list="carme-voice-models" aria-label="语音转写型号" value={config.model} disabled={busy}
        placeholder="例如 FunAudioLLM/SenseVoiceSmall" onChange={(e) => setConfig({ ...config, model: e.target.value })} />
        <datalist id="carme-voice-models">{suggested.map((id) => <option key={id} value={id} />)}</datalist>
        <small>型号按接口文档填写，不必出现在「模型」列表里。</small></label>
      <div className="voice-actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={() => void toggleTest()}>
          {recording ? `停止并转写（${seconds}s）` : "录制测试"}
        </button>
        <span className="form-help">录一段话，确认接口、密钥与型号可用。</span>
      </div>
      {result && <p className="form-success" role="status"><Check size={16} />识别结果：{result}</p>}
    </>}
    <div className="voice-actions">
      <button type="button" className="secondary-button" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "保存语音设置"}</button>
    </div>
    {notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
function SettingsDialog({
  stats,
  connected,
  onClose,
  onConnect,
  onModelsSaved,
  onMigrated,
}: {
  stats: Stats;
  connected: boolean;
  onClose: () => void;
  onConnect: (token: string) => void;
  onModelsSaved: () => void;
  onMigrated: () => void;
}) {
  const [tab, setTab] = useState<SettingsTab>("general");
  const [value, setValue] = useState(token);
  const [sessions, setSessions] = useState<{ id: string; created_at: number; expires_at: number }[]>([]);
  const [sessionError, setSessionError] = useState("");
  useEffect(() => {
    if (tab === "general" && connected) void api<{ sessions: typeof sessions }>("/sessions")
      .then((result) => setSessions(result.sessions)).catch((error) => setSessionError(error.message));
  }, [tab, connected]);
  return (
    <Modal title="设置" onClose={onClose} wide>
      <div className="modal-body settings-form settings-layout">
        <nav className="settings-menubar" role="tablist" aria-label="设置分类">
          {SETTINGS_TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={`settings-menu-item${tab === id ? " active" : ""}`}
              onClick={() => setTab(id)}
            >
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="settings-content" role="tabpanel">
          {tab === "general" && <AppearanceSettings />}
          {tab === "models" && (
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
          )}
          {tab === "agents" && <AgentDefaults />}
          {tab === "migration" && <MigrationSettings onMigrated={onMigrated} />}
          {tab === "notify" && (
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
          )}
          {tab === "access" && (
            <>
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
                {ACCOUNT_NAME ? <p className="form-help">当前账号：{ACCOUNT_NAME}<br />此账号的聊天、文件和浏览器环境独立保存。</p> : <><form
                  onSubmit={(e) => {
                    e.preventDefault();
                    onConnect(value.trim());
                    setValue("");
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
                    配对并连接
                  </button>
                </form>
                <p className="form-help">访问令牌只用于本次配对，不保存在浏览器存储中。会话最长 7 天。</p></>}
                <button type="button" className="text-button" onClick={() => {
                  void api("/session", undefined, "DELETE").then(() => ACCOUNT_NAME ? accountChanged() : location.reload());
                }}>退出此设备</button>
                {sessions.length > 0 && <div aria-label="已配对设备会话"><p>有效设备会话（{sessions.length}）</p>
                  {sessions.map((session) => <p key={session.id}>
                    {session.id.slice(-8)} · {new Date(session.created_at * 1000).toLocaleString()} 配对 · {new Date(session.expires_at * 1000).toLocaleString()} 到期
                    <button type="button" className="text-button" onClick={() => {
                      void api(`/sessions/${session.id}`, undefined, "DELETE")
                        .then(() => setSessions((items) => items.filter((item) => item.id !== session.id)))
                        .catch((error) => setSessionError(error.message));
                    }}>撤销会话</button>
                  </p>)}
                </div>}
                {sessionError && <p role="alert" className="form-error">{sessionError}</p>}
              </div>
              {ACCOUNT_NAME && <AccountPasswordSection />}
              <VoiceInputSettings />
              <CloudflareSettings />
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
            </>
          )}
          {tab === "about" && (
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
          )}
        </div>
      </div>
    </Modal>
  );
}

type NewBotDefaults = {
  name: string;
  title: string;
  prompt: string;
  engine: Agent["engine"];
  engine_model: string;
  engine_effort: string;
  model: string;
  tier: string;
  effort: string;
  sandbox: string;
  can_delegate: boolean;
  tools: string[];
};

function MigrationSettings({ onMigrated }: { onMigrated: () => void }) {
  const [fileName, setFileName] = useState("");
  const [bundle, setBundle] = useState<ExportBundle | null>(null);
  const [profileMode, setProfileMode] = useState<"merge" | "overwrite" | "skip">("merge");
  const [withMemory, setWithMemory] = useState(true);
  const [withConversations, setWithConversations] = useState(true);
  const [agentCount, setAgentCount] = useState(0);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [report, setReport] = useState<ImportReport | null>(null);
  useEffect(() => {
    void api<{ agents: unknown[] }>("/agents")
      .then((result) => setAgentCount(result.agents.length))
      .catch(() => setAgentCount(0));
  }, []);
  async function exportAll() {
    setBusy("export"); setError(""); setNotice(""); setReport(null);
    try {
      const data = await api<ExportBundle>("/export");
      downloadJson(`carme-bots-${fileStamp()}.json`, data);
      setNotice(`已导出 ${data.agents?.length || 0} 个 Bot 的角色、记忆与对话上下文。`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function pickFile(file: File | null) {
    setError(""); setNotice(""); setReport(null); setBundle(null); setFileName("");
    if (!file) return;
    if (file.size > 64 * 1024 * 1024) { setError("文件超过 64 MB，请确认选的是 Carme 导出文件。"); return; }
    try {
      const parsed = JSON.parse(await file.text());
      if (!parsed || typeof parsed !== "object" || !Array.isArray(parsed.agents) || !parsed.agents.length) {
        throw new Error("这不是有效的 Carme 导出文件：缺少 agents 列表。");
      }
      setBundle(parsed as ExportBundle);
      setFileName(file.name);
    } catch (e) { setError((e as Error).message); }
  }

  async function runImport() {
    if (!bundle) return;
    setBusy("import"); setError(""); setNotice(""); setReport(null);
    try {
      const result = await api<{ ok: boolean; report: ImportReport }>("/import", {
        bundle, profile_mode: profileMode, import_memory: withMemory, import_conversations: withConversations,
      });
      setReport(result.report);
      onMigrated();
      setNotice("导入完成；本机已有对话不会被覆盖，导入的会话一律新建。");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  const preview = bundle ? {
    agents: bundle.agents?.length || 0,
    memories: (bundle.agents || []).reduce((sum, item) => sum + (item.memory?.length || 0), 0),
    conversations: bundle.conversations?.length || 0,
    messages: (bundle.conversations || []).reduce((sum, item) => sum + (item.messages?.length || 0), 0),
    avatars: (bundle.agents || []).filter((item) => item.avatar_image).length,
  } : null;

  return <>
    <div className="settings-section">
      <h3><Download size={17} />导出</h3>
      <p className="form-help">
        导出内容包含：每个 Bot 的角色设定（名称、描述、角色指令、引擎、模型、可用工具、头像）、它的长期记忆，以及它参与的全部对话上下文（含长对话摘要）。导出文件里的密钥、任务记录和执行电脑凭据不会被写入。
      </p>
      <div className="form-actions">
        <button type="button" className="primary-button" disabled={!!busy} onClick={() => void exportAll()}>
          {busy === "export" ? <LoaderCircle size={16} className="spin" /> : <Download size={16} />}
          导出全部 Bot（{agentCount}）
        </button>
      </div>
      <p className="form-help">单个 Bot 的导出在「Bot 设置」里，只包含它自己的记忆与它参与的对话。</p>
    </div>
    <div className="settings-section">
      <h3><Upload size={17} />导入到本机</h3>
      <p className="form-help">
        在本机并入导出文件。本机已有的对话不会被覆盖：导入的会话一律新建，Bot 的 id 相同则按下面的策略处理。
      </p>
      <label className="form-label">
        导出文件
        <input type="file" accept="application/json,.json" disabled={!!busy}
          onChange={(e) => void pickFile(e.target.files?.[0] || null)} />
        <small>{fileName ? `已选择：${fileName}` : "选择之前导出的 .json 文件；不会上传到任何第三方。"}</small>
      </label>
      {preview && <div className="migration-preview">
        <span>Bot {preview.agents}</span>
        <span>记忆 {preview.memories}</span>
        <span>对话 {preview.conversations}</span>
        <span>消息 {preview.messages}</span>
        {preview.avatars > 0 && <span>内嵌头像 {preview.avatars}</span>}
      </div>}
      <label className="form-label">
        同 id 的 Bot
        <select value={profileMode} disabled={!!busy} onChange={(e) => setProfileMode(e.target.value as typeof profileMode)}>
          <option value="merge">合并：保留本机设置，只补齐缺失字段</option>
          <option value="overwrite">覆盖：用导出文件替换本机的角色设定</option>
          <option value="skip">跳过：完全不动本机已有的 Bot</option>
        </select>
      </label>
      <label className="checkbox-label">
        <input type="checkbox" checked={withMemory} disabled={!!busy} onChange={(e) => setWithMemory(e.target.checked)} />
        导入长期记忆（同名记忆会被导出内容覆盖）
      </label>
      <label className="checkbox-label">
        <input type="checkbox" checked={withConversations} disabled={!!busy} onChange={(e) => setWithConversations(e.target.checked)} />
        导入对话上下文（一律新建会话，不覆盖本机对话）
      </label>
      <div className="form-actions">
        <button type="button" className="primary-button" disabled={!bundle || !!busy} onClick={() => void runImport()}>
          {busy === "import" && <LoaderCircle size={16} className="spin" />}
          {busy === "import" ? "正在导入…" : "开始导入"}
        </button>
      </div>
      {notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
    {report && <div className="settings-section">
      <h3><Check size={17} />导入结果</h3>
      <div className="migration-preview">
        <span>新建 {report.agents.filter((item) => item.profile === "created").length}</span>
        <span>更新 {report.agents.filter((item) => item.profile === "updated").length}</span>
        <span>跳过 {report.agents.filter((item) => item.profile === "skipped").length}</span>
        <span>失败 {report.agents.filter((item) => item.profile === "failed").length}</span>
        <span>记忆 {report.memory_count}</span>
        <span>会话 {report.conversations.length}</span>
      </div>
      <ul className="migration-list">
        {report.agents.map((item) => <li key={item.id}>
          <strong>{item.id}</strong>
          <small>{item.profile === "created" ? "新建" : item.profile === "updated" ? "更新" : item.profile === "skipped" ? "跳过（本机已有）" : "失败"}
            {item.memory ? ` · 记忆 ${item.memory}` : ""}</small>
          {(item.notes || []).map((note) => <em key={note}>{note}</em>)}
        </li>)}
      </ul>
      {report.conversations.length > 0 && <details className="migration-conversations">
        <summary>已导入 {report.conversations.length} 个会话</summary>
        <ul className="migration-list">{report.conversations.map((item) => <li key={item.id}>
          <strong>{item.title || "(无标题)"}</strong>
          <small>{item.agent_ids.join("、")} · {item.messages} 条消息</small>
        </li>)}</ul>
      </details>}
      {report.warnings.length > 0 && <ul className="migration-warnings">
        {report.warnings.map((warning) => <li key={warning}>{warning}</li>)}
      </ul>}
    </div>}
  </>;
}

function AgentDefaults() {
  const [data, setData] = useState<NewBotDefaults | null>(null);
  const [tools, setTools] = useState("");
  const [engines, setEngines] = useState<EngineInfo[]>([]);
  const [models, setModels] = useState<SavedModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    void Promise.all([
      api<{ defaults: NewBotDefaults }>("/agents/defaults"),
      api<EngineSettings>("/engines"),
      api<ModelSettings>("/models"),
    ])
      .then(([defaultsResult, engineResult, modelResult]) => {
        setData(defaultsResult.defaults);
        setTools((defaultsResult.defaults.tools || []).join(", "));
        setEngines(engineResult.engines);
        setModels(modelResult.models);
      })
      .catch((e) => setError(e.message));
  }, []);
  if (!data) return <div className="settings-section"><p className="muted">{error || "正在读取默认值…"}</p>{error && <p className="form-error" role="alert">{error}</p>}</div>;
  const selectedModel = models.find((model) => model.ref === data.model);
  const engineOptions = connectedAgents(engines, data.engine);
  const modelOptions = availableModels(models, data.model);
  const modelOptionGroups = modelGroups(modelOptions);
  const modelOption = (model: SavedModel) => <option key={model.ref} value={model.ref}>{model.id}{model.effort ? ` · 默认 ${model.effort}` : ""}{model.available ? "" : "（当前不可用）"}</option>;
  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ defaults: NewBotDefaults }>("/agents/defaults", {
        ...data,
        tools: tools.split(/[,，\n]/).map((v) => v.trim()).filter(Boolean),
      }, "PUT");
      setData(result.defaults);
      setTools((result.defaults.tools || []).join(", "));
      setNotice("默认 Agent 与默认模型已保存。新建 Bot 会直接使用这些默认值。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="settings-section">
      <h3>
        <Users size={17} />
        默认 Agent 与模型
      </h3>
      <p className="form-help">
        一键新建 Bot 时不需要填写任何内容：名称、引擎、模型和工具都取自这里，描述可以创建后再编辑。
      </p>
      <div className="form-grid">
        <label className="form-label">
          默认名称
          <input value={data.name} maxLength={80} onChange={(e) => setData({ ...data, name: e.target.value })} />
        </label>
        <label className="form-label">
          默认描述
          <input value={data.title} maxLength={160} placeholder="例如：通用助理" onChange={(e) => setData({ ...data, title: e.target.value })} />
        </label>
      </div>
      <label className="form-label">
        默认角色指令
        <textarea rows={4} value={data.prompt} onChange={(e) => setData({ ...data, prompt: e.target.value })} />
      </label>
      <label className="form-label">
        默认 Agent（引擎）
        <select value={data.engine} onChange={(e) => setData({ ...data, engine: e.target.value as Agent["engine"] })}>
          {!engineOptions.length && <option value={data.engine}>{data.engine === "api" ? "Carme API 网关（暂无可用模型）" : `${data.engine} CLI（当前未连接）`}</option>}
          {engineOptions.map((engine) => (
            <option key={engine.id} value={engine.id}>
              {engine.label}{engine.ready ? "" : "（当前未连接）"}
            </option>
          ))}
        </select>
        <small>{data.engine === "api" ? "使用 Carme API 网关和已配置的模型连接。" : "使用 Carme 专用受管引擎。"}列表只显示当前已连接的 Agent。</small>
      </label>
      {data.engine !== "api" ? (
        <div className="form-grid">
          <label className="form-label">
            默认 CLI 模型
            <input value={data.engine_model} maxLength={200} autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder="由 Carme profile 显式指定模型" onChange={(e) => setData({ ...data, engine_model: e.target.value })} />
          </label>
          <label className="form-label">
            默认 CLI 推理强度
            <select value={data.engine_effort} onChange={(e) => setData({ ...data, engine_effort: e.target.value })}>
              <option value="">CLI 默认</option>
              {cliEffortOptions(data.engine).map((level) => <option key={level} value={level}>{level}</option>)}
            </select>
          </label>
        </div>
      ) : (
        <>
          <label className="form-label">
            默认模型
            <select value={data.model} onChange={(e) => setData({ ...data, model: e.target.value, effort: "" })}>
              <option value="">按模型档位自动选择</option>
              {data.model && !modelOptions.some((model) => model.ref === data.model) && <option value={data.model}>{data.model}（原配置，不在已配置列表中）</option>}
              {modelOptionGroups.length > 1
                ? modelOptionGroups.map((group) => <optgroup key={group.label} label={group.label}>{group.items.map(modelOption)}</optgroup>)
                : modelOptionGroups.flatMap((group) => group.items.map(modelOption))}
            </select>
            <small>{modelOptions.some((model) => model.available) ? `只列出已经配置好 API key 的模型（${modelOptions.filter((model) => model.available).length} 个）。` : "还没有可用模型：请先在「设置 → 模型」添加连接并验证 key。"}</small>
          </label>
          <div className="form-grid">
            <label className="form-label">
              默认模型档位
              <select value={data.tier} disabled={!!data.model} onChange={(e) => setData({ ...data, tier: e.target.value })}>
                {Array.from(new Set(["reason", "balanced", "fast", data.tier])).map((tier) => (
                  <option key={tier} value={tier}>{tier}</option>
                ))}
              </select>
            </label>
            <label className="form-label">
              默认推理强度
              <select value={data.effort} disabled={!data.model} onChange={(e) => setData({ ...data, effort: e.target.value })}>
                <option value="">连接默认{selectedModel?.effort ? `（${selectedModel.effort}）` : "（由模型决定）"}</option>
                {data.effort && !selectedModel?.effort_options.includes(data.effort) && <option value={data.effort}>{data.effort}（原配置）</option>}
                {selectedModel?.effort_options.map((effort) => <option key={effort} value={effort}>{effort}</option>)}
              </select>
            </label>
          </div>
        </>
      )}
      <label className="form-label">
        默认可用工具
        <input value={tools} placeholder="delegate, memory, browser" onChange={(e) => setTools(e.target.value)} />
        <small>逗号分隔。会话附件读取和成果生成始终可用，各 Bot 之间可传递文本与文件附件。</small>
      </label>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={data.can_delegate}
          onChange={(e) => setData({ ...data, can_delegate: e.target.checked })}
        />
        默认允许 Bot 之间互相委派（传递文本与文件附件）
      </label>
      <div className="form-actions">
        <button type="button" className="primary-button" disabled={busy} onClick={() => void save()}>
          {busy && <LoaderCircle size={16} className="spin" />}
          保存默认值
        </button>
      </div>
      {notice && <p className="form-success" role="status">{notice}</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
    </div>
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
  openai_compatible: "Custom / OpenAI 兼容",
};
const PROVIDER_PRESETS: { id: string; label: string; type: string; url: string; group: "direct" | "relay" }[] = [
  { id: "openai", label: "OpenAI", type: "openai", url: "https://api.openai.com/v1", group: "direct" },
  { id: "anthropic", label: "Anthropic", type: "anthropic", url: "https://api.anthropic.com/v1", group: "direct" },
  { id: "google", label: "Google Gemini", type: "openai", url: "https://generativelanguage.googleapis.com/v1beta/openai", group: "direct" },
  { id: "deepseek", label: "DeepSeek", type: "openai", url: "https://api.deepseek.com/v1", group: "direct" },
  { id: "xai", label: "xAI · Grok", type: "openai", url: "https://api.x.ai/v1", group: "direct" },
  { id: "dashscope", label: "阿里云百炼 / Qwen", type: "openai", url: "https://dashscope.aliyuncs.com/compatible-mode/v1", group: "direct" },
  { id: "doubao", label: "火山引擎 · Doubao", type: "openai", url: "https://ark.cn-beijing.volces.com/api/v3", group: "direct" },
  { id: "moonshot", label: "Moonshot · Kimi", type: "openai", url: "https://api.moonshot.cn/v1", group: "direct" },
  { id: "minimax", label: "MiniMax", type: "openai", url: "https://api.minimaxi.com/v1", group: "direct" },
  { id: "zhipu", label: "智谱 · GLM", type: "openai", url: "https://open.bigmodel.cn/api/paas/v4", group: "direct" },
  { id: "mistral", label: "Mistral", type: "openai", url: "https://api.mistral.ai/v1", group: "direct" },
  { id: "cohere", label: "Cohere", type: "openai", url: "https://api.cohere.com/v1", group: "direct" },
  { id: "groq", label: "Groq", type: "openai", url: "https://api.groq.com/openai/v1", group: "direct" },
  { id: "together", label: "Together AI", type: "openai", url: "https://api.together.xyz/v1", group: "direct" },
  { id: "opencode-go", label: "OpenCode Go", type: "openai", url: "https://opencode.ai/zen/go/v1", group: "relay" },
  { id: "flatkey", label: "Flatkey", type: "openai", url: "https://router.flatkey.ai/v1", group: "relay" },
  { id: "openrouter", label: "OpenRouter", type: "openai", url: "https://openrouter.ai/api/v1", group: "relay" },
  { id: "302ai", label: "302.AI", type: "openai", url: "https://api.302.ai/v1", group: "relay" },
  { id: "siliconflow", label: "SiliconFlow 硅基流动", type: "openai", url: "https://api.siliconflow.cn/v1", group: "relay" },
  { id: "requesty", label: "Requesty", type: "openai", url: "https://router.requesty.ai/v1", group: "relay" },
];
const sameUrl = (a: string, b: string) => a.replace(/\/+$/, "") === b.replace(/\/+$/, "");

function LocalEngines() {
  const [engines, setEngines] = useState<EngineInfo[]>([]);
  const [execution, setExecution] = useState<EngineSettings["execution"]>();
  const [models, setModels] = useState<Record<string, string>>({});
  const [efforts, setEfforts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const refresh = useCallback(async () => {
    const result = await api<EngineSettings>("/engines");
    setEngines(result.engines); setExecution(result.execution);
  }, []);
  useEffect(() => { void refresh().catch((e) => setError(e.message)); }, [refresh]);
  const [savedModels, setSavedModels] = useState<SavedModel[]>([]);
  useEffect(() => {
    void api<ModelSettings>("/models").then((result) =>
      setSavedModels(result.models.filter((model) => model.verified && model.available))).catch(() => {});
  }, []);
  async function test(engine: EngineInfo) {
    setBusy(engine.id); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; error?: string; reply?: string; model?: string }>(`/engines/${engine.id}/test`, {
        model: models[engine.id] || "", effort: efforts[engine.id] || "", runtime_profile: engine.profile || "",
      });
      if (!result.ok) throw new Error(result.error || "CLI 测试失败");
      setNotice(`连接成功 · ${result.model || models[engine.id] || "profile 中的显式模型"}（${result.reply || "OK"}）。实际任务会继续验证工具权限。`);
      await refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  }
  const connectedEngines = engines.filter((engine) => engine.id !== "api" && engine.ready);
  const engineDetail = (engine: EngineInfo) => {
    if (engine.id === "api") return engine.capability;
    const detail = [engine.provider, engine.model].filter(Boolean);
    if (engine.auth_method === "oauth_token") detail.push("OAuth 登录");
    return `Profile：${engine.profile || "未登记"} · 身份引用：${engine.credential_ref || "未登记"} · 认证：${engine.auth_status === "verified" ? "最近调用已验证" : engine.auth_status === "rejected" ? "供应商拒绝身份，请检查专用凭据" : "请用连接测试验证"}${detail.length ? " · " + detail.join(" / ") : ""} · 模式：${engine.capability}。执行失败会停止任务，不回退宿主机。`;
  };
  return <div className="local-engines">
    <div className="status-line"><span>使用已登记的 Carme runtime profile。切换引擎保留聊天、角色和记忆，并继续执行相同权限检查。</span><button type="button" className="text-button" onClick={() => void refresh().catch((e) => setError(e.message))}><RefreshCw size={13} />重新检测</button></div>
    {execution && <div className="engine-connected" aria-label="执行组件状态">
      <strong>执行组件</strong>
      <div className="engine-connected-chips">{([['Control', execution.control], ['Broker', execution.broker], ['Pi', execution.pi], ['Action', execution.action], ['Docker Browser', execution.browser || 'not_configured'], ['Mac Runner', execution.mac_runner]] as const).map(([name, state]) =>
        <span className="engine-chip" key={name}>{name} · {({ready: '已就绪', offline: '未连接', unavailable: '不可用', unverified: '未验收', not_paired: '未配对', image_missing: '镜像缺失'} as Record<string, string>)[state] || state}</span>)}</div>
      <small>活动执行：{execution.active_jobs} · Worker 外部网络：{execution.worker_network === 'none' ? '禁用' : execution.worker_network}。Web 操作 → Docker Browser；真实 Mac 操作 → Mac Runner（需单独配对和授权）。</small>
    </div>}
    {connectedEngines.length > 0 && <div className="engine-connected" role="status">
      <strong>已配置的受管引擎（{connectedEngines.length}）</strong>
      <div className="engine-connected-chips">{connectedEngines.map((engine) => {
        const detail = [engine.provider, engine.model].filter(Boolean).join(" / ");
        return <span className="engine-chip" key={engine.id}><Check size={13} />{engine.label}{detail ? ` · ${detail}` : ""}</span>;
      })}</div>
      <small>在各个 Bot 设置的「引擎」中即可选择以上 Agent 接管该 Bot。</small>
    </div>}
    <div className="engine-list">{engines.map((engine) => <article className={`engine-card${engine.ready ? " connected" : ""}`} key={engine.id}>
      <div className="engine-card-head"><div><strong>{engine.label}</strong><span className={`engine-status ${engine.status}`}>{engine.id === "api" ? "已配置" : engine.status === "ready" ? "已连接" : engine.status === "configured_auth_unverified" ? "已配置，身份待验证" : engine.status === "detected" ? "已发现，待验证" : engine.status === "not_configured" ? "未配置" : engine.status === "unsupported" ? "不支持安全适配" : engine.status === "runtime_profile_required" ? "未登记（测试时自动登记）" : engine.status}</span></div><small>{engine.version ? `${engine.version}${engine.id === "api" ? "" : "（profile 固定版本）"}` : ""}</small></div>
      <p>{engineDetail(engine)}</p>
      {engine.id !== "api" && engine.installed && <div className="engine-test-fields">
        <select aria-label={`${engine.label} 模型`} value={models[engine.id] || ""} onChange={(e) => setModels({ ...models, [engine.id]: e.target.value })}>
          <option value="">{engine.id === "pi" ? "跟随已登记的 profile 模型" : "profile 中的显式模型"}</option>
          {savedModels.map((model) => <option key={model.ref} value={model.ref}>{model.provider_label} / {model.id}</option>)}
          {models[engine.id] && !savedModels.some((model) => model.ref === models[engine.id]) && <option value={models[engine.id]}>{models[engine.id]}</option>}
        </select>
        <select aria-label={`${engine.label} effort`} value={efforts[engine.id] || ""} onChange={(e) => setEfforts({ ...efforts, [engine.id]: e.target.value })}><option value="">默认 effort</option>{cliEffortOptions(engine.id).map((level) => <option value={level} key={level}>{level}</option>)}</select>
      </div>}
      {engine.id === "pi" && engine.installed && <small>换选模型并点「测试连接」会把这个模型绑定到 Pi harness（重新登记专用身份）。</small>}
      {engine.id !== "api" && <button type="button" className="secondary-button" disabled={busy === engine.id || (engine.id !== "pi" && !engine.installed)} onClick={() => void test(engine)}>{busy === engine.id && <LoaderCircle size={15} className="spin" />}{busy === engine.id ? "正在测试…" : "测试连接"}</button>}
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
  if (editing) return <ModelConnectionForm key={editing.id} connection={editing} savedModels={settings.models.filter((model) => model.provider_id === editing.id)} onCancel={() => setEditing(null)} onSaved={async (options?: { keepOpen?: boolean }) => { await refresh(); onSaved(); if (options?.keepOpen) return; setEditing(null); setNotice("连接与模型已保存，可在每个 Bot 的设置中选择。"); }} />;
  return <div className="model-connections">
    {settings.tiers && <ModelRoutingSettings key={JSON.stringify([settings.tiers,settings.allow_mock])} settings={settings} onSaved={async () => {await refresh(); onSaved(); setNotice("团队默认模型已保存，单独指定模型的 Bot 保持原设置。");}} />}
    <p className="form-help">添加 API 连接，测试后选择模型和推理强度。各 Bot 可独立选择已配置的模型。</p>
    {settings.connections.map((connection) => {
      const connectionModels = settings.models.filter((model) => model.provider_id === connection.id);
      const modelKey = (model: SavedModel) => `model:${connection.id}/${model.ref}`;
      const connKey = `conn:${connection.id}`;
      // 该连接（API key）的累计 token 消耗，来自后端 usage_log 聚合。
      const usage = settings.usage?.[connection.id];
      const usageText = !usage ? "暂无用量"
        : usage.known === 0 ? `token 用量未知（${usage.requests} 次调用）`
        : `输入 ${formatTokens(usage.input)} / 输出 ${formatTokens(usage.output)} tokens`;
      const usageTitle = usage
        ? `累计 ${usage.requests} 次调用，其中 ${usage.known} 次记录了 token${usage.last ? ` · 最近 ${new Date(usage.last * 1000).toLocaleString()}` : ""}`
        : "这个连接还没有调用记录";
      return <div className="model-connection-card" key={connection.id}>
        <div>
          <strong>{connection.label}</strong><span>{API_TYPE_LABELS[connection.type] || connection.type}</span><small>{connection.base_url}</small>
          <p>{connection.has_key ? "密钥已保存" : "尚未配置密钥"} · {connectionModels.length} 个模型{connectionModels.length === 0 && <> · {usageText}</>}</p>
          {connectionModels.length > 0 && <ul className="connection-model-list">
            {connectionModels.map((model) => <li key={model.ref}>
              <span>{model.id}<small>{model.effort ? ` · effort ${model.effort}` : " · 模型默认 effort"}{model.verified ? " · 已验证" : ""}{!model.available ? " · 不可用" : ""}</small></span>
              <span className="connection-row-tail">
                <button type="button" className="text-button" disabled={!!removing}
                  onClick={() => removing === modelKey(model) ? void removeModels(connection.id, model.id) : setRemoving(modelKey(model))}>
                  {removing === modelKey(model) ? "确认移除" : "移除"}</button>
                <small className="connection-usage" title={usageTitle}>{usageText}</small>
              </span>
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

type GroupBot = { id: string; name: string };
type SkillRecord = {
  id: string; name: string; description: string; version: string; source: string;
  installed_at: number; enabled: boolean; files: string[]; file_count: number;
  size: number; error: string;
};
type SkillCatalog = { skills: SkillRecord[]; root: string; enabled: number; bots: GroupBot[] };
type McpTool = { name: string; description: string; registered: string };
type McpServer = {
  id: string; name: string; label: string; transport: "stdio" | "http";
  isolation: string;
  target: string; command: string; args: string[]; cwd: string; url: string;
  env_keys: string[]; header_keys: string[];
  enabled: boolean; approval: "auto" | "confirm"; timeout: number;
  source: string; installed_at: number;
  status: "connected" | "connecting" | "error" | "disabled" | "stopped" | "missing";
  tools: McpTool[]; tool_count: number; error: string; server_info: string; protocol: string;
};
type McpCatalog = { servers: McpServer[]; bots: GroupBot[]; config: string };
type McpMutation = { ok: boolean; server: McpServer; message: string };
type SkillSource = "text" | "path" | "url" | "github";
type McpForm = {
  id: string; name: string; transport: "stdio" | "http"; command: string; args_text: string;
  env_text: string; clear_env: boolean; cwd: string; url: string; headers_text: string;
  enabled: boolean; approval: "auto" | "confirm"; timeout: string;
};

const SKILL_SOURCES: [SkillSource, string][] = [
  ["text", "粘贴 Markdown"], ["path", "本机路径"], ["url", "网址"], ["github", "GitHub 仓库"],
];
const MCP_STATUS_TEXT: Record<string, string> = {
  connected: "已连接", connecting: "连接中", error: "连接失败",
  disabled: "已停用", stopped: "未连接", missing: "已移除",
};
const MCP_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$/;

function formatTokens(value: number) {
  if (!value) return "0";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(2)}M`;
}
function formatSize(bytes: number) {
  if (!bytes) return "0 KB";
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function emptyMcpForm(): McpForm {
  return { id: "", name: "", transport: "stdio", command: "", args_text: "", env_text: "",
    clear_env: false, cwd: "", url: "", headers_text: "", enabled: true, approval: "auto", timeout: "30" };
}
function mcpFormFrom(server: McpServer): McpForm {
  // 命令 / 参数 / 工作目录 / 地址都按结构化字段填回，参数里的空格不会被拆坏。
  return {
    id: server.id, name: server.name || server.label, transport: server.transport,
    command: server.command, args_text: (server.args || []).join("\n"),
    env_text: "", clear_env: false, cwd: server.cwd,
    url: server.transport === "http" ? server.url : "", headers_text: "",
    enabled: server.enabled, approval: server.approval, timeout: String(server.timeout || 30),
  };
}

function ToolGroupHint({ bots, group, onAgentsChanged }: { bots: GroupBot[]; group: "skill" | "mcp"; onAgentsChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function grant() {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api<{ ok: boolean; updated: string[]; message: string }>("/tool-groups/grant", { group });
      setMessage(result.message || "已为全部 Bot 打开该工具分组。");
      onAgentsChanged();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  if (bots.length) return <p className="muted">{bots.length} 个 Bot 可以使用：{bots.map((bot) => bot.name).join("、")}。</p>;
  return <>
    <div className="info-box">
      <Info size={16} />
      <p>只有工具列表里包含 <code>{group}</code> 分组的 Bot 才能调用{group === "skill" ? "已安装的 Skill" : "已安装的 MCP Server"}。现在还没有这样的 Bot，点下面的按钮把它加进每个 Bot 的工具列表。</p>
    </div>
    <button type="button" className="secondary-button" disabled={busy} onClick={() => void grant()}>
      {busy ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}{busy ? "正在打开…" : "为全部 Bot 打开"}
    </button>
    {!!message && <p className="form-success" role="status"><Check size={16} />{message}</p>}
    {!!error && <p className="form-error" role="alert">{error}</p>}
  </>;
}

function SkillPanel({ onAgentsChanged }: { onAgentsChanged: () => void }) {
  const [skills, setSkills] = useState<SkillRecord[]>([]);
  const [root, setRoot] = useState("");
  const [bots, setBots] = useState<GroupBot[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [removing, setRemoving] = useState("");
  const [open, setOpen] = useState("");
  const [docs, setDocs] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [form, setForm] = useState({ source: "text" as SkillSource, value: "", text: "", name: "", description: "", subpath: "" });
  const load = useCallback(async () => {
    const result = await api<SkillCatalog>("/skills");
    setSkills(result.skills || []); setRoot(result.root || ""); setBots(result.bots || []); setLoaded(true);
  }, []);
  useEffect(() => { void load().catch((e) => setError((e as Error).message)); }, [load]);
  async function rescan() {
    setBusy("reload"); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; skills: SkillRecord[]; message: string }>("/skills/reload", undefined, "POST");
      setSkills(result.skills || []); setNotice(result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function toggle(skill: SkillRecord) {
    setBusy(`toggle:${skill.id}`); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; skill: SkillRecord; message: string }>(`/skills/${encodeURIComponent(skill.id)}`, { enabled: !skill.enabled }, "PATCH");
      setSkills((previous) => previous.map((item) => item.id === skill.id ? result.skill : item));
      setNotice(result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function remove(id: string) {
    setBusy(`remove:${id}`); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; message: string }>(`/skills/${encodeURIComponent(id)}`, undefined, "DELETE");
      await load(); setNotice(result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); setRemoving(""); }
  }
  async function showDoc(skill: SkillRecord) {
    const next = open === skill.id ? "" : skill.id;
    setOpen(next); setError("");
    if (!next || docs[skill.id] !== undefined) return;
    setBusy(`doc:${skill.id}`);
    try {
      const result = await api<{ body: string }>(`/skills/${encodeURIComponent(skill.id)}`);
      setDocs((previous) => ({ ...previous, [skill.id]: result.body || "（SKILL.md 是空文件）" }));
    } catch (e) { setOpen(""); setError((e as Error).message); } finally { setBusy(""); }
  }
  async function install(event: FormEvent) {
    event.preventDefault();
    const payload: { source: SkillSource; value?: string; text?: string; name?: string; description?: string; subpath?: string } = { source: form.source };
    if (form.source === "text") {
      if (!form.text.trim()) { setError("请粘贴技能的 Markdown 内容（SKILL.md）。"); return; }
      payload.text = form.text;
    } else if (!form.value.trim()) {
      setError(form.source === "path" ? "请填写后端 Mac 上的技能目录或 .md 文件路径。"
        : form.source === "url" ? "请填写 SKILL.md 的 http(s) 地址。" : "请填写 owner/repo 或 GitHub 仓库地址。");
      return;
    } else payload.value = form.value.trim();
    if (form.source === "github" && form.subpath.trim()) payload.subpath = form.subpath.trim();
    if (form.name.trim()) payload.name = form.name.trim();
    if (form.description.trim()) payload.description = form.description.trim();
    setBusy("install"); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; skill: SkillRecord; message: string }>("/skills/install", payload);
      setForm({ source: "text", value: "", text: "", name: "", description: "", subpath: "" });
      await load(); setNotice(result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  const valuePlaceholder = form.source === "path" ? "/Users/you/skills/my-skill 或 SKILL.md 文件路径"
    : form.source === "url" ? "https://example.com/SKILL.md" : "owner/repo 或 https://github.com/owner/repo";
  const valueLabel = form.source === "path" ? "本机路径" : form.source === "url" ? "SKILL.md 地址" : "GitHub 仓库";
  return <div className="model-connections settings-form">
    <div className="ext-panel-head">
      <p className="muted">已安装 {skills.length} 个技能 · 目录 <span className="ext-mono">{root || "读取中…"}</span></p>
      <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void rescan()}>
        {busy === "reload" ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}重新扫描
      </button>
    </div>
    {skills.map((skill) => <div className="model-connection-card" key={skill.id}>
      <div className="ext-body">
        <div className="ext-card-head">
          <strong>{skill.name || skill.id}</strong>
          {!!skill.version && <span className="tag ext-tag">{skill.version}</span>}
          {!!skill.error && <span className="ext-status error">不可用</span>}
          {!skill.enabled && !skill.error && <span className="ext-status disabled">已停用</span>}
        </div>
        {!!skill.description && <p className="ext-note">{skill.description}</p>}
        <small className="ext-meta">{skill.id} · {skill.file_count} 个文件 · {formatSize(skill.size)} · {skill.source || "来源未知"} · 安装于 {formatTime(skill.installed_at, true)}</small>
        {!!skill.error && <div className="ext-error" role="alert">{skill.error}</div>}
        <label className="check-label">
          <input type="checkbox" aria-label={`启用技能 ${skill.name || skill.id}`} checked={skill.enabled} disabled={!!busy || !!skill.error} onChange={() => void toggle(skill)} />启用
        </label>
        <button type="button" className="text-button" disabled={busy === `doc:${skill.id}`} onClick={() => void showDoc(skill)}>
          {busy === `doc:${skill.id}` ? <LoaderCircle className="spin" size={14} /> : <FileText size={14} />}{open === skill.id ? "收起说明" : "查看说明"}
        </button>
        {open === skill.id && (docs[skill.id] !== undefined
          ? <pre className="ext-doc">{docs[skill.id]}</pre>
          : <small className="ext-meta">正在读取 SKILL.md…</small>)}
      </div>
      <div className="model-connection-actions">
        <button type="button" className="text-button danger-text" disabled={!!busy} onClick={() => removing === skill.id ? void remove(skill.id) : setRemoving(skill.id)}>
          <Trash2 size={14} />{removing === skill.id ? "确认删除" : "删除"}
        </button>
      </div>
    </div>)}
    {loaded && !skills.length && <p className="muted">技能（Skill）就是一个带 SKILL.md 的目录：用法写在 SKILL.md 里，脚本和模板放在同一个目录下，Bot 需要时会先读说明再使用。现在还没有安装任何技能，用下面的表单装一个。</p>}
    <form className="settings-form ext-form" onSubmit={install}>
      <strong>安装技能</strong>
      <p className="form-help">技能会保存到后端目录 <span className="ext-mono">{root || "skills"}</span>。可以从 Markdown、本机路径、网址或 GitHub 仓库安装；同名技能会生成新的 id（例如 pdf-2），不会覆盖已有技能。</p>
      <label className="form-label">来源
        <select value={form.source} disabled={!!busy} onChange={(e) => setForm({ ...form, source: e.target.value as SkillSource })}>
          {SKILL_SOURCES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </label>
      {form.source === "text"
        ? <label className="form-label">SKILL.md 内容<textarea className="ext-doc-input" rows={8} disabled={!!busy} value={form.text} placeholder={"---\nname: PDF 报告\nversion: 1.0.0\ndescription: 把数据整理成 PDF\n---\n\n# 用法\n1. …"} onChange={(e) => setForm({ ...form, text: e.target.value })} /><small>直接粘贴带 frontmatter 的 Markdown；名称、说明和版本会从 frontmatter 读取。</small></label>
        : <label className="form-label">{valueLabel}<input disabled={!!busy} autoCapitalize="off" autoCorrect="off" spellCheck={false} value={form.value} placeholder={valuePlaceholder} onChange={(e) => setForm({ ...form, value: e.target.value })} /></label>}
      {form.source === "github" && <label className="form-label">子目录<input disabled={!!busy} value={form.subpath} placeholder="仓库内的子目录，例如 skills/pdf" onChange={(e) => setForm({ ...form, subpath: e.target.value })} /><small>留空表示在仓库根目录查找 SKILL.md。</small></label>}
      <div className="form-grid">
        <label className="form-label">名称（可选）<input disabled={!!busy} maxLength={120} value={form.name} placeholder="留空读取 frontmatter" onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label className="form-label">说明（可选）<input disabled={!!busy} maxLength={400} value={form.description} placeholder="留空读取 frontmatter" onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
      </div>
      <button type="submit" className="secondary-button" disabled={!!busy}>
        {busy === "install" ? <LoaderCircle className="spin" size={16} /> : <Package size={16} />}{busy === "install" ? "正在安装…" : "安装技能"}
      </button>
    </form>
    <SkillLearning bots={bots} installed={skills} />
    <ToolGroupHint bots={bots} group="skill" onAgentsChanged={onAgentsChanged} />
    <p className="form-help">工具分组只开放读取入口。每个 Bot 还需单独授权固定 Skill 版本。</p>
    {!!notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
    {!!error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}

function McpPanel({ onAgentsChanged }: { onAgentsChanged: () => void }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [bots, setBots] = useState<GroupBot[]>([]);
  const [config, setConfig] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [removing, setRemoving] = useState("");
  const [expanded, setExpanded] = useState("");
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [form, setForm] = useState<McpForm>(() => emptyMcpForm());
  const load = useCallback(async () => {
    const result = await api<McpCatalog>("/mcp");
    setServers(result.servers || []); setBots(result.bots || []); setConfig(result.config || ""); setLoaded(true);
  }, []);
  useEffect(() => { void load().catch((e) => setError((e as Error).message)); }, [load]);
  function apply(server: McpServer, message: string) {
    setServers((previous) => previous.map((item) => item.id === server.id ? server : item));
    setNotice(message || "");
    setError(server.error || "");
  }
  async function refresh() {
    setBusy("load"); setError(""); setNotice("");
    try { await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function connect(server: McpServer) {
    setBusy(`connect:${server.id}`); setError(""); setNotice("");
    try {
      const result = await api<McpMutation>(`/mcp/servers/${encodeURIComponent(server.id)}/connect`, undefined, "POST");
      apply(result.server, result.message);
    } catch (e) {
      setError((e as Error).message);
      await load().catch(() => {}); // 连接失败：重新读一次，行内状态会变成「连接失败」。
    } finally { setBusy(""); }
  }
  async function disconnect(server: McpServer) {
    setBusy(`disconnect:${server.id}`); setError(""); setNotice("");
    try {
      const result = await api<McpMutation>(`/mcp/servers/${encodeURIComponent(server.id)}/disconnect`, undefined, "POST");
      apply(result.server, result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function toggle(server: McpServer) {
    setBusy(`toggle:${server.id}`); setError(""); setNotice("");
    try {
      const result = await api<McpMutation>(`/mcp/servers/${encodeURIComponent(server.id)}`, { enabled: !server.enabled }, "PATCH");
      apply(result.server, result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function remove(id: string) {
    setBusy(`remove:${id}`); setError(""); setNotice("");
    try {
      const result = await api<{ ok: boolean; message: string }>(`/mcp/servers/${encodeURIComponent(id)}`, undefined, "DELETE");
      await load(); setNotice(result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); setRemoving(""); }
  }
  function startCreate() { setForm(emptyMcpForm()); setCreating(true); setEditing(null); setError(""); setNotice(""); }
  function startEdit(server: McpServer) { setForm(mcpFormFrom(server)); setEditing(server); setCreating(false); setError(""); setNotice(""); }
  function stopEdit() { setCreating(false); setEditing(null); setError(""); }
  async function save(event: FormEvent) {
    event.preventDefault();
    const id = form.id.trim();
    if (!MCP_ID_PATTERN.test(id)) { setError("ID 只能使用字母、数字、下划线和短横线，必须以字母或数字开头，最长 48 位。"); return; }
    const stdio = form.transport === "stdio";
    if (stdio && !form.command.trim()) { setError("请填写要运行的命令，例如 npx。"); return; }
    if (!stdio && !/^https?:\/\/\S+$/i.test(form.url.trim())) { setError("请填写以 http:// 或 https:// 开头的地址。"); return; }
    const payload = {
      id, name: form.name.trim(), transport: form.transport,
      command: stdio ? form.command.trim() : "",
      args: null,
      args_text: stdio ? form.args_text : "",
      env_text: stdio ? form.env_text : "",
      clear_env: form.clear_env,
      cwd: stdio ? form.cwd.trim() : "",
      url: stdio ? "" : form.url.trim(),
      headers_text: stdio ? "" : form.headers_text,
      enabled: form.enabled, approval: form.approval,
      timeout: Math.min(300, Math.max(1, Number(form.timeout) || 30)),
    };
    setBusy("save"); setError(""); setNotice("");
    try {
      const result = await api<McpMutation>(editing ? `/mcp/servers/${encodeURIComponent(editing.id)}` : "/mcp/servers", payload, editing ? "PUT" : "POST");
      await load();
      setEditing(null); setCreating(false);
      apply(result.server, result.message);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  if (editing || creating) {
    const keys = editing ? (form.transport === "stdio" ? editing.env_keys : editing.header_keys) : [];
    const secretHint = keys.length
      ? `已保存的变量：${keys.join("、")}（留空则保持不变，填写则整组替换）`
      : "密钥只保存在后端的 mcp.yaml 里，界面不会回显已保存的值。";
    return <form className="model-connections settings-form" onSubmit={save}>
      <div className="ext-panel-head">
        <strong>{editing ? `编辑「${editing.label}」` : "添加 MCP Server"}</strong>
        <button type="button" className="text-button" disabled={!!busy} onClick={stopEdit}>返回列表</button>
      </div>
      <fieldset disabled={!!busy} className="connection-fields ext-form">
        <div className="form-grid">
          <label className="form-label">显示名称<input maxLength={80} value={form.name} placeholder="例如：文件系统" onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
          <label className="form-label">ID<input required disabled={!!editing} maxLength={48} autoCapitalize="off" autoCorrect="off" spellCheck={false} value={form.id} placeholder="例如：filesystem" onChange={(e) => setForm({ ...form, id: e.target.value })} /><small>字母、数字、下划线、短横线，需以字母或数字开头，最长 48 位；创建后不可修改。</small></label>
        </div>
        <label className="form-label">传输方式<select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value === "http" ? "http" : "stdio" })}><option value="stdio">stdio · 在后端 Mac 上运行命令</option><option value="http">http · 连接远程地址</option></select></label>
        {form.transport === "stdio" ? <>
          <label className="form-label">可执行文件<input maxLength={400} value={form.command} placeholder="npx" onChange={(e) => setForm({ ...form, command: e.target.value })} /><small>在后端 Mac 上直接执行的命令，例如 npx、uvx 或绝对路径。</small></label>
          <label className="form-label">参数（每行一个）<textarea className="ext-doc-input" rows={5} maxLength={4000} value={form.args_text} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/tmp"} onChange={(e) => setForm({ ...form, args_text: e.target.value })} /><small>每行是一个参数，会按顺序传给可执行文件。</small></label>
          <label className="form-label">工作目录<input maxLength={500} value={form.cwd} placeholder="留空使用后端默认目录" onChange={(e) => setForm({ ...form, cwd: e.target.value })} /></label>
          <label className="form-label">环境变量（每行一条 KEY=value）<textarea className="ext-doc-input" rows={4} maxLength={8000} value={form.env_text} onChange={(e) => setForm({ ...form, env_text: e.target.value })} /><small>{secretHint}</small></label>
        </> : <>
          <label className="form-label">地址<input maxLength={2048} autoCapitalize="off" autoCorrect="off" spellCheck={false} value={form.url} placeholder="https://example.com/mcp" onChange={(e) => setForm({ ...form, url: e.target.value })} /><small>支持 http(s) 的 MCP 端点地址。</small></label>
          <label className="form-label">请求头（每行一条 Header=value）<textarea className="ext-doc-input" rows={4} maxLength={8000} value={form.headers_text} onChange={(e) => setForm({ ...form, headers_text: e.target.value })} /><small>{secretHint}</small></label>
        </>}
        <label className="check-label"><input type="checkbox" checked={form.clear_env} onChange={(e) => setForm({ ...form, clear_env: e.target.checked })} />清空已保存的{form.transport === "stdio" ? "环境变量" : "请求头"}（勾选后以文本框内容为准，全部留空＝全部删除）</label>
        <label className="check-label"><input type="checkbox" checked={form.approval === "confirm"} onChange={(e) => setForm({ ...form, approval: e.target.checked ? "confirm" : "auto" })} />每次调用需人工确认</label>
        <label className="form-label">超时（秒）<input type="number" min={1} max={300} value={form.timeout} onChange={(e) => setForm({ ...form, timeout: e.target.value })} /><small>单次调用的最长等待时间，1–300 秒。</small></label>
        <label className="check-label"><input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />启用（{editing ? "启用状态下保存会立即尝试连接" : "保存后立即尝试连接"}）</label>
      </fieldset>
      <div className="form-actions">
        <button type="button" className="secondary-button" disabled={!!busy} onClick={stopEdit}>取消</button>
        <button type="submit" className="primary-button" disabled={!!busy}>
          {busy === "save" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}{busy === "save" ? "正在保存…" : editing ? "保存修改" : "添加 Server"}
        </button>
      </div>
      {!!notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
      {!!error && <p className="form-error" role="alert">{error}</p>}
    </form>;
  }
  const connected = servers.filter((server) => server.status === "connected").length;
  return <div className="model-connections settings-form">
    <div className="ext-panel-head">
      <p className="muted">已连接 {connected}/{servers.length} 个 MCP Server · 配置文件 <span className="ext-mono">{config || "读取中…"}</span></p>
      <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void refresh()}>
        {busy === "load" ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}刷新
      </button>
    </div>
    {servers.map((server) => <div className="model-connection-card" key={server.id}>
      <div className="ext-body">
        <div className="ext-card-head">
          <strong>{server.label}</strong>
          <span className="tag ext-tag">{server.transport === "http" ? "http" : "stdio"}</span>
          <span className={`ext-status ${server.status}`}>{server.status === "connecting" && <LoaderCircle className="spin" size={11} />}{MCP_STATUS_TEXT[server.status] || server.status}</span>
        </div>
        <small className="ext-meta ext-mono">{server.target || "（未填写命令）"}</small>
        <small className="ext-meta">执行边界：{server.isolation === "trusted-host-not-isolated" ? "显式宿主调试，无系统隔离；Bot 不可调用" : server.isolation === "external-http" ? "外部 HTTP 服务" : "等待隔离执行器"}</small>
        <small className="ext-meta">{server.approval === "confirm" ? "每次确认" : "自动执行"} · 超时 {server.timeout}s · {server.enabled ? "已启用" : "已停用"} · {server.source || "手动添加"} · 安装于 {formatTime(server.installed_at, true)}</small>
        {!!server.error && <div className="ext-error" role="alert">{server.error}</div>}
        {server.status === "connected" ? <>
          <button type="button" className="text-button" onClick={() => setExpanded(expanded === server.id ? "" : server.id)}>
            <ChevronRight className={expanded === server.id ? "ext-open" : ""} size={14} />{server.tool_count} 个工具
          </button>
          {expanded === server.id && <ul className="connection-model-list">
            {server.tools.map((tool) => <li key={tool.registered || tool.name}>
              <span><code>{tool.registered || tool.name}</code>{tool.registered && tool.name && tool.registered !== tool.name ? ` · ${tool.name}` : ""}<small>{tool.description}</small></span>
            </li>)}
            {!server.tools.length && <li><span>这个 Server 没有提供任何工具</span></li>}
          </ul>}
          {!!(server.server_info || server.protocol) && <small className="ext-meta">{server.server_info}{server.server_info && server.protocol ? " · " : ""}{server.protocol}</small>}
        </> : server.tool_count > 0 ? <small className="ext-meta">上次连接时注册了 {server.tool_count} 个工具</small> : null}
        <label className="check-label">
          <input type="checkbox" aria-label={`启用 MCP Server ${server.label}`} checked={server.enabled} disabled={!!busy} onChange={() => void toggle(server)} />启用（启用时会立即尝试连接）
        </label>
      </div>
      <div className="model-connection-actions">
        <button type="button" className="secondary-button" disabled={!!busy} onClick={() => startEdit(server)}><Pencil size={14} />编辑</button>
        <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void connect(server)}>
          {busy === `connect:${server.id}` ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}重连
        </button>
        {server.status === "connected" && <button type="button" className="secondary-button" disabled={!!busy} onClick={() => void disconnect(server)}>
          {busy === `disconnect:${server.id}` ? <LoaderCircle className="spin" size={16} /> : <WifiOff size={16} />}断开
        </button>}
        <button type="button" className="text-button danger-text" disabled={!!busy} onClick={() => removing === server.id ? void remove(server.id) : setRemoving(server.id)}>
          <Trash2 size={14} />{removing === server.id ? "确认删除" : "删除连接"}
        </button>
      </div>
    </div>)}
    {loaded && !servers.length && <p className="muted">MCP Server 让 Bot 用上外部工具：stdio 类型会在后端 Mac 上启动一条真实命令，http 类型会连接一个远程地址。现在还没有配置任何 Server，用下面的按钮添加一个。</p>}
    <button type="button" className="secondary-button" disabled={!!busy} onClick={startCreate}><Plug size={16} />添加 MCP Server</button>
    <div className="info-box">
      <Info size={16} />
      <p>stdio 类型的 Server 是运行在后端 Mac 上的真实进程，会继承后端的运行环境与权限；http 类型会把你填写的地址当作可信服务直接调用。只添加你信任的来源。</p>
    </div>
    <McpGrants bots={bots} servers={servers} />
    <ToolGroupHint bots={bots} group="mcp" onAgentsChanged={onAgentsChanged} />
    <p className="form-help">stdio 在 Docker Action 中执行。分组授权不会开放未授权的工具或资源。</p>
    {!!notice && <p className="form-success" role="status"><Check size={16} />{notice}</p>}
    {!!error && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
 function ModelConnectionForm({ connection, savedModels, onCancel, onSaved }: { connection: Connection; savedModels: SavedModel[]; onCancel: () => void; onSaved: (options?: { keepOpen?: boolean }) => Promise<void> }) {
  const [data, setData] = useState({ ...connection, api_key: "" });
  const [probeId, setProbeId] = useState("");
  const [models, setModels] = useState<DiscoveredModel[]>([]);
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const payload = () => ({ id: data.id, label: data.label.trim(), type: data.type, base_url: data.base_url.trim(), ...(data.api_key ? { api_key: data.api_key } : {}) });
  const [failures, setFailures] = useState<ModelFailure[]>([]);
  const change = (field: keyof Connection, value: string) => { setData((previous) => ({ ...previous, [field]: value })); setProbeId(""); setModels([]); setError(""); setNotice(""); setFailures([]); };
  const activePreset = PROVIDER_PRESETS.find((item) => item.type === data.type && sameUrl(item.url, data.base_url));
  const presetValue = activePreset ? activePreset.id : "type:" + (data.type in API_TYPE_LABELS ? data.type : "openai_compatible");
  function applyPreset(id: string) {
    if (id.startsWith("type:")) { change("type", id.slice(5)); return; }
    const preset = PROVIDER_PRESETS.find((item) => item.id === id);
    if (!preset) return;
    change("type", preset.type);
    change("base_url", preset.url);
  }
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
    event.preventDefault(); setBusy("save"); setError(""); setFailures([]); setNotice("");
    try {
      const result = await api<{
        ok: boolean; error?: string; message?: string; failed?: ModelFailure[];
      }>("/models/connections", { ...payload(), probe_id: probeId, models: Object.entries(selected).map(([id, effort]) => ({ id, effort })) });
      setData((previous) => ({ ...previous, api_key: "" }));
      if (result.failed?.length) {
        // 部分成功：把可用型号留在列表里，失败的保持在勾选状态便于改档位重试。
        setFailures(result.failed);
        setSelected(Object.fromEntries(result.failed.map((item) => [item.id, item.effort_rejected ? "" : (item.effort || "")])));
        setNotice(result.message || "");
        await onSaved({ keepOpen: true });
        return;
      }
      if (!result.ok) throw new Error(result.error || "保存失败");
      await onSaved();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  return <form className="settings-form connection-form" onSubmit={save}>
    <div className="connection-form-heading"><strong>{connection.has_key ? "编辑模型连接" : "添加模型连接"}</strong><button type="button" className="text-button" disabled={!!busy} onClick={onCancel}>返回列表</button></div>
    <fieldset disabled={!!busy} className="connection-fields">
      <div className="form-grid">
        <label className="form-label">连接名称<input required placeholder="例如：我的 OpenAI" maxLength={80} value={data.label} onChange={(e) => change("label", e.target.value)} /></label>
        <label className="form-label">模型提供商<select value={presetValue} onChange={(e) => applyPreset(e.target.value)}>
          <optgroup label="直连提供商">{PROVIDER_PRESETS.filter((item) => item.group === "direct").map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</optgroup>
          <optgroup label="中转 / 聚合">{PROVIDER_PRESETS.filter((item) => item.group === "relay").map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</optgroup>
          <optgroup label="协议与自定义">{Object.entries(API_TYPE_LABELS).map(([id, label]) => <option key={id} value={"type:" + id}>{label}</option>)}</optgroup>
        </select><small>选择提供商会自动填入官方接口地址；「Custom / OpenAI 兼容」用于本地模型（如 Ollama）或其他服务。更换提供商后需重新输入 API key。</small></label>
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
          <div><span className="form-help">{model.effort_source === "metadata" ? "支持范围来自接口信息" : model.effort_source === "verified" ? "支持范围已通过接口检测" : model.effort_source === "partial" ? "部分选项已验证，可重试完成检测" : model.effort_source === "saved" ? "保留此前验证的选项，保存时复核" : model.effort_source === "unverified" ? "该服务不校验 effort，全部档位可选；保存时会实际调用复核" : "接口未提供 effort 信息，可点「检测 effort」获取可选档位"}</span><button className="text-button" type="button" disabled={!!busy} onClick={() => void detect(model)}>{busy === model.id && <LoaderCircle size={14} className="spin" />}{busy === model.id ? "正在检测…" : "检测 effort"}</button></div>
        </div>}
      </div>)}</div>
      <p className="form-help">勾选要添加或更新的模型，最多 16 个；已有模型保留。检测 effort 和保存验证会发送少量固定测试消息，可能产生 API 费用，不发送你的聊天内容。</p>
      <button className="primary-button full-width" type="submit" disabled={!!busy || !Object.keys(selected).length || Object.keys(selected).length > 16}>{busy === "save" && <LoaderCircle size={16} className="spin" />}{busy === "save" ? "正在验证并保存…" : `验证并保存所选模型（${Object.keys(selected).length}）`}</button>
    </div>}
    {failures.length > 0 && <div className="model-failures" role="alert">
      <strong><X size={15} />以下型号未通过调用验证，没有写入配置（其余已保存）</strong>
      <ul>{failures.map((item) => <li key={item.id}><code>{item.id}</code><span>{item.error}</span>
        {item.effort_rejected && <small>已把该型号改回「模型默认」，可重新选择档位后再保存。</small>}
        {item.hint && <small>{item.hint}</small>}
      </li>)}</ul>
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
  const [fonts,setFonts]=useState<SystemFont[]>([]);
  const [source,setSource]=useState("");
  const [search,setSearch]=useState("");
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const load=useCallback(async(rescan=false)=>{
    const result=await api<FontCatalog>(rescan?"/fonts/reload":"/fonts",rescan?{}:undefined,rescan?"POST":"GET");
    setFonts(result.fonts||[]);setSource(result.source||"");
    applyAppearance(readAppearance(),result.fonts||[]);
  },[]);
  useEffect(()=>{void load().catch((e)=>setError((e as Error).message));},[load]);
  const update=(next:typeof value)=>{setValue(next);applyAppearance(next,fonts);saveStorage('carme_appearance',JSON.stringify(next));};
  const keyword=search.trim().toLowerCase();
  const matched=keyword?fonts.filter((font)=>`${font.label} ${font.family}`.toLowerCase().includes(keyword)):fonts;
  const options=(list:SystemFont[])=>list.map((font)=><option key={font.family} value={SYSTEM_FONT_PREFIX+font.family}>{font.label}{font.label===font.family?"":` · ${font.family}`}</option>);
  const proportional=matched.filter((font)=>!font.monospace),monospaceFonts=matched.filter((font)=>font.monospace);
  const chosen=value.family.startsWith(SYSTEM_FONT_PREFIX)?systemFontName(value.family):"";
  const chosenFont=fonts.find((font)=>font.family===chosen);
  const chosenFiltered=!!chosen&&!matched.some((font)=>font.family===chosen);
  return <div className="settings-section"><h3><Type size={17}/>外观</h3>
    <div className="form-grid"><label className="form-label">界面字体<select aria-label="界面字体" value={value.family} onChange={(e)=>update({...value,family:e.target.value})}>
      {Object.entries(BUILTIN_FONTS).map(([id,font])=><option key={id} value={id}>{font.label}</option>)}
      {chosenFiltered&&<option value={value.family}>{chosenFont?`${chosenFont.label} · ${chosenFont.family}`:chosen}（当前选择）</option>}
      {proportional.length>0&&<optgroup label={`系统字体（${proportional.length}）`}>{options(proportional)}</optgroup>}
      {monospaceFonts.length>0&&<optgroup label={`等宽字体（${monospaceFonts.length}）`}>{options(monospaceFonts)}</optgroup>}
      {!fonts.length&&<option value="" disabled>{error?"读不到后端字体列表，先使用内置字体":"正在读取运行 Carme 的系统的字体…"}</option>}
    </select><small>列表来自运行 Carme 的后端；本机（或手机）没装的字体会自动落到替代字体。</small></label>
    <label className="form-label">基准字号 <output>{value.size} px</output><input type="range" aria-label="界面字号" min="14" max="22" step="1" value={value.size} onChange={(e)=>update({...value,size:Number(e.target.value)})}/></label></div>
    {fonts.length>11&&<label className="search-box appearance-font-search"><Search size={16}/><input aria-label="搜索系统字体" value={search} onChange={(e)=>setSearch(e.target.value)} placeholder={`在 ${fonts.length} 种系统字体中搜索`}/></label>}
    <p className="appearance-preview">Carme · 让 Bot 帮你处理日常工作。<br/>文字大小、菜单和聊天将同步调整。</p>
    <div className="appearance-help"><small>{fonts.length?`已读取 ${fonts.length} 种系统字体${source==="fontconfig"?"（读自 fontconfig）":source==="builtin"?"（系统字体不可读，使用内置清单）":""}。立即生效，保存在此设备。`:error?`无法读取后端字体列表：${error}。当前使用内置字体。`:"正在读取运行 Carme 的系统的字体。"}</small>
      <span className="appearance-help-actions">{(fonts.length>0||!!error)&&<button className="text-button" type="button" disabled={busy} onClick={async()=>{setBusy(true);setError("");try{await load(true);setSearch("");}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}><RefreshCw size={13}/>{busy?"重新检测中…":"重新检测"}</button>}<button className="text-button" type="button" onClick={()=>update({family:'system',size:16})}>恢复默认</button></span></div>
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


type OutcomeEvidence = { status: string; user_accepted: boolean; report: Record<string, unknown>; report_hash?: string };
function TaskEvidence({ taskId, title }: { taskId: string; title: string }) {
  const [evidence, setEvidence] = useState<{ outcome: OutcomeEvidence; operations: { id: string; tool: string; status: string }[]; checkpoint: unknown } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [receipt, setReceipt] = useState("");
  async function action(path?: string, body: object = {}) {
    setBusy(true); setError("");
    try { if (path) await api(`/tasks/${encodeURIComponent(taskId)}/${path}`, body);
      setEvidence(await api(`/tasks/${encodeURIComponent(taskId)}`));
    } catch(e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <details className="approval-card"><summary onClick={() => { if (!evidence) void action(); }}>{title} · 验收与恢复</summary>
    <small>{taskId}</small>
    {evidence && <>
      <p>成果：{evidence.outcome.status === "verified" ? "验收通过" : evidence.outcome.status === "failed" ? "验收失败" : "尚未验收"} · {evidence.outcome.user_accepted ? "用户已确认" : "用户未确认"}</p>
      <pre className="ext-doc">{JSON.stringify(evidence.outcome.report, null, 2)}</pre>
      <button type="button" className="secondary-button" disabled={busy} onClick={() => void action("verify")}>按任务要求验收</button>
      <button type="button" className="secondary-button" disabled={busy || evidence.outcome.status !== "verified" || evidence.outcome.user_accepted} onClick={() => void action("accept", { report_hash: evidence.outcome.report_hash })}>确认这一版成果</button>
      {!!evidence.checkpoint && <button type="button" className="text-button" disabled={busy} onClick={() => void action("resume")}>从检查点继续</button>}
      {evidence.operations.filter(o => o.status === "pending").map(o => <div key={o.id}>
        <p>{o.tool} 的结果尚未确认；请先核对外部服务。</p>
        <label className="form-label">外部回执或核对依据<input value={receipt} onChange={e => setReceipt(e.target.value)} /></label>
        <button type="button" disabled={busy || !receipt.trim()} onClick={() => void action("reconcile", { operation_id: o.id, effect: "confirmed", receipt: { note: receipt } })}>确认已执行，禁止重放</button>
        <button type="button" disabled={busy || !receipt.trim()} onClick={() => void action("reconcile", { operation_id: o.id, effect: "not_performed", receipt: { note: receipt } })}>确认未执行，允许重试</button>
      </div>)}
    </>}
    {error && <p role="alert" className="form-error">{error}</p>}
  </details>;
}

type LearningCandidate = { id: string; skill_id: string; revision: string; source_task_id: string; status: string; test_task_id: string | null };
type LearningState = { candidates: LearningCandidate[]; skill_grants: Record<string, Record<string, string>>; approved_versions: Record<string, string[]> };
function SkillLearning({ bots, installed }: { bots: GroupBot[]; installed: SkillRecord[] }) {
  const [state, setState] = useState<LearningState | null>(null); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [source, setSource] = useState(""); const [name, setName] = useState(""); const [document, setDocument] = useState(""); const [privateNames, setPrivateNames] = useState("");
  const [bot, setBot] = useState(""); const [skill, setSkill] = useState(""); const [revision, setRevision] = useState("");
  const [candidateReview, setCandidateReview] = useState<{ id: string; files: Record<string, string> } | null>(null);
  const [candidateId, setCandidateId] = useState(""); const [testTask, setTestTask] = useState(""); const [reviewed, setReviewed] = useState(false);
  const [conversation, setConversation] = useState(""); const [inputIds, setInputIds] = useState(""); const [goal, setGoal] = useState(""); const [filename, setFilename] = useState("report.pdf"); const [requiredText, setRequiredText] = useState("");
  const load = useCallback(async () => setState(await api<LearningState>("/learning")), []);
  async function action(path: string, body: object) { setBusy(true); setError(""); try { const result = await api<Record<string, unknown>>(path, body); await load(); return result; } catch(e) { setError((e as Error).message); } finally { setBusy(false); } }
  const candidate = state?.candidates.find(c => c.id === candidateId);
  return <details className="settings-form ext-form"><summary onClick={() => { if (!state) void load().catch(e => setError((e as Error).message)); }}>版本授权与 Skill 学习</summary>
    <p className="form-help">先验收并确认源任务，再生成脱敏候选。新输入实测并获确认后，才可发布给选定 Bot。</p>
    <label className="form-label">授权 Bot<select value={bot} onChange={e => setBot(e.target.value)}><option value="">请选择</option>{bots.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select></label>
    <label className="form-label">已安装技能<select value={skill} onChange={e => { setSkill(e.target.value); setRevision(""); }}><option value="">请选择</option>{installed.map(s => <option value={s.id} key={s.id}>{s.name}</option>)}</select></label>
    <button type="button" disabled={busy || !skill} onClick={() => void action(`/skills/${encodeURIComponent(skill)}/snapshot`, {}).then(r => { if (r) setRevision(String(r.revision)); })}>生成待授权版本</button>
    <label className="form-label">版本（可选旧版本回滚）<input value={revision} onChange={e => setRevision(e.target.value)} list="skill-approved-versions" /><datalist id="skill-approved-versions">{(state?.approved_versions[skill] || []).map(v => <option key={v} value={v} />)}</datalist></label>
    <p className="form-help">当前授权：{state?.skill_grants[bot]?.[skill] || "无"}</p>
    <button type="button" disabled={busy || !bot || !skill || !revision} onClick={() => void action("/skill-grants", { bot_id: bot, skill_id: skill, revision })}>授权所选版本 / 回滚</button>
    <button type="button" disabled={busy || !state?.skill_grants[bot]?.[skill]} onClick={() => void action("/skill-grants", { bot_id: bot, skill_id: skill, revision: state?.skill_grants[bot]?.[skill], revoke: true })}>撤销此授权</button>
    <label className="form-label">已认可的源任务 ID<input value={source} onChange={e => setSource(e.target.value)} /></label>
    <label className="form-label">候选名称<input value={name} onChange={e => setName(e.target.value)} /></label>
    <label className="form-label">候选说明<textarea rows={6} value={document} onChange={e => setDocument(e.target.value)} /></label>
    <label className="form-label">需脱敏的私人信息（每行一项）<textarea rows={2} value={privateNames} onChange={e => setPrivateNames(e.target.value)} /></label>
    <button type="button" disabled={busy || !source || !name || !document} onClick={() => void action("/skill-candidates", { source_task_id: source, name, document, private_literals: privateNames.split("\n").map(s => s.trim()).filter(Boolean) })}>生成待审候选</button>
    <label className="form-label">候选<select aria-label="候选" value={candidateId} onChange={e => { setCandidateId(e.target.value); setReviewed(false); }}><option value="">请选择</option>{state?.candidates.map(c => <option key={c.id} value={c.id}>{c.skill_id} · {c.status}</option>)}</select></label>
    {candidate && <>
      <p className="ext-mono">{candidate.revision}</p><p>来源：{candidate.source_task_id}</p>
      <button type="button" disabled={busy} onClick={() => { setError(""); void api<{ files: Record<string, string> }>(`/skill-candidates/${candidate.id}/review`).then(r => setCandidateReview({ id: candidate.id, files: r.files })).catch(e => setError((e as Error).message)); }}>审阅完整候选与脚本</button>
      {candidateReview?.id === candidate.id && Object.entries(candidateReview.files).map(([path, text]) => <details key={path}><summary>{path}</summary><pre className="ext-doc">{text}</pre></details>)}
      <label className="form-label">测试会话 ID<input value={conversation} onChange={e => setConversation(e.target.value)} /></label>
      <label className="form-label">新输入附件 ID（逗号分隔）<input value={inputIds} onChange={e => setInputIds(e.target.value)} /></label>
      <label className="form-label">测试任务<textarea rows={2} value={goal} onChange={e => setGoal(e.target.value)} placeholder={`使用 ${candidate.skill_id} 处理新输入`} /></label>
      <label className="form-label">测试成果文件名<input value={filename} onChange={e => setFilename(e.target.value)} /></label>
      <label className="form-label">成果必须包含的文字<input value={requiredText} onChange={e => setRequiredText(e.target.value)} /></label>
      <button type="button" disabled={busy || !bot || !conversation || !inputIds || !goal || !filename || !requiredText} onClick={() => void action(`/skill-candidates/${candidate.id}/test`, { conversation_id: conversation, agent_id: bot, goal, envelope: { input_artifact_ids: inputIds.split(",").map(s => s.trim()).filter(Boolean), expected_outputs: [filename], acceptance_checks: [{ output: filename, check: { kind: "text_contains", text: requiredText } }] } }).then(r => { if (r) setTestTask(String(r.task_id)); })}>在隔离容器用新输入测试</button>
      <label className="form-label">测试任务 ID<input value={testTask} onChange={e => setTestTask(e.target.value)} /></label>
      {testTask && <TaskEvidence taskId={testTask} title="候选实测" />}
      <label className="check-label"><input type="checkbox" checked={reviewed} disabled={candidateReview?.id !== candidate.id} onChange={e => setReviewed(e.target.checked)} />已查看候选全文、来源及实测结果，确认无私人信息</label>
      <button type="button" disabled={busy || !bot || !testTask || !reviewed} onClick={() => void action(`/skill-candidates/${candidate.id}/publish`, { test_task_id: testTask, bot_ids: [bot], revision: candidate.revision, privacy_reviewed: reviewed })}>发布给所选 Bot</button>
    </>}
    {error && <p role="alert" className="form-error">{error}</p>}
  </details>;
}

function McpGrants({ bots, servers }: { bots: GroupBot[]; servers: McpServer[] }) {
  const [bot, setBot] = useState(""); const [server, setServer] = useState(""); const [remote, setRemote] = useState(""); const [scope, setScope] = useState("{}"); const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  async function grant(revoke: boolean) { setBusy(true); try { await api("/mcp-grants", { bot_id: bot, server_id: server, remote, argument_allowlist: JSON.parse(scope), revoke }); setMessage(revoke ? "已撤销" : "已绑定当前身份、工具 Schema 和资源范围"); } catch(e) { setMessage((e as Error).message); } finally { setBusy(false); } }
  return <details className="settings-form ext-form"><summary>按 Bot 授权工具与资源</summary>
    <label className="form-label">Bot<select value={bot} onChange={e => setBot(e.target.value)}><option value="">请选择</option>{bots.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}</select></label>
    <label className="form-label">MCP<select value={server} onChange={e => { setServer(e.target.value); setRemote(""); }}><option value="">请选择</option>{servers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
    <label className="form-label">具体工具<input value={remote} onChange={e => setRemote(e.target.value)} placeholder="已发现的远端工具名" /></label>
    <label className="form-label">资源参数允许值<textarea value={scope} onChange={e => setScope(e.target.value)} placeholder={'{"path":["/inputs/artifacts/授权文件"]}'} /></label>
    <p className="form-help">填写参数名与确切允许值。身份或 Schema 改变后须重新授权；新增工具默认不可用。</p>
    <button type="button" disabled={busy || !bot || !server || !remote} onClick={() => void grant(false)}>授权此工具与资源</button>
    <button type="button" disabled={busy || !bot || !server || !remote} onClick={() => void grant(true)}>撤销</button>
    {message && <p role="status">{message}</p>}
  </details>;
}
