/* ══════════════════════════════════════════════════════════
   Carme 前端逻辑
   原生 ES 模块，无构建步骤。改完刷新即可，不需要 npm。
   ══════════════════════════════════════════════════════════ */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* ─────────── 状态 ─────────── */

const state = {
  agents: [],
  entry: "chief",
  tasks: [],
  approvals: [],
  filter: "",
  view: "work",
  currentTask: null,
  sse: null,
  feed: [],
  maxFeed: 120,
};

/* token：优先 URL 参数（手机扫码用），其次 localStorage */
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) {
  localStorage.setItem("carme_token", urlToken);
  history.replaceState({}, "", location.pathname);
}
const TOKEN = localStorage.getItem("carme_token") || "";

/* ─────────── 工具函数 ─────────── */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function relTime(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

const STATUS_TEXT = {
  queued: "排队中", running: "干活中", done: "已完成",
  failed: "失败", cancelled: "已中止", truncated: "已达步数上限",
};

function agentOf(id) {
  return state.agents.find((a) => a.id === id) || { id, name: id, emoji: "🤖", title: "" };
}

let toastTimer;
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("is-err", isErr);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (opts.body) headers["Content-Type"] = "application/json";
  if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;

  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (res.status === 401) {
    toast("需要访问令牌。在网址后加 ?token=你的令牌", true);
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* 保持原样 */ }
    throw new Error(detail);
  }
  return res.json();
}

/* ─────────── 渲染：侧栏花名册 ─────────── */

function renderRoster() {
  $("#roster").innerHTML = state.agents
    .map((a) => `
      <button class="roster-item" data-agent="${esc(a.id)}">
        <span class="roster-emoji">${esc(a.emoji)}</span>
        <span class="roster-body">
          <span class="roster-name">${esc(a.name)}</span>
          <span class="roster-title">${esc(a.title || a.id)}</span>
        </span>
      </button>`)
    .join("");

  $$(".roster-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.agent;
      $("#agentSelect").value = id;
      switchView("work");
      $("#goalInput").focus();
      toast(`已选中 ${agentOf(id).name}`);
    });
  });

  const options = state.agents
    .map((a) => `<option value="${esc(a.id)}">${esc(a.emoji)} ${esc(a.name)}</option>`)
    .join("");
  const sel = $("#agentSelect");
  sel.innerHTML = options;
  sel.value = state.entry;
}

/* ─────────── 渲染：任务列表 ─────────── */

function taskCard(t) {
  const a = agentOf(t.agent_id);
  const status = t.status === "done" && t.result?.includes("已达") ? "truncated" : t.status;
  const preview = t.status === "done" && t.result
    ? `<div class="task-preview">${esc(t.result.slice(0, 260))}</div>` : "";
  const err = t.status === "failed" && t.error
    ? `<div class="task-preview">${esc(t.error.slice(0, 200))}</div>` : "";

  return `
    <article class="task-card" data-task="${esc(t.id)}">
      <div class="task-top">
        <span class="task-agent">${esc(a.emoji)} ${esc(a.name)}</span>
        <span class="status-chip s-${esc(status)}">${STATUS_TEXT[status] || status}</span>
        <span class="task-time">${relTime(t.created_at)}</span>
      </div>
      <div class="task-title">${esc(t.title || t.goal)}</div>
      ${preview}${err}
      <div class="task-foot">
        <span>${t.tokens ? `${t.tokens} tok` : ""}</span>
        <span>${t.cost_usd ? `$${t.cost_usd.toFixed(4)}` : ""}</span>
      </div>
    </article>`;
}

function renderTasks() {
  const list = state.filter
    ? state.tasks.filter((t) => t.status === state.filter)
    : state.tasks;

  const html = list.length
    ? list.map(taskCard).join("")
    : `<div class="empty">这里还是空的。<br>去「派活」丢个任务试试。</div>`;

  $("#taskList").innerHTML = html;

  const recent = state.tasks.slice(0, 4);
  $("#recentList").innerHTML = recent.length
    ? recent.map(taskCard).join("")
    : `<div class="empty">还没有任务记录。</div>`;

  $$("[data-task]").forEach((el) => {
    el.addEventListener("click", () => openDrawer(el.dataset.task));
  });

  const active = state.tasks.filter((t) => t.status === "running" || t.status === "queued").length;
  $$("[data-badge]").forEach((el) => { el.textContent = active || ""; });

  const pill = $("#runningPill");
  if (active) { pill.hidden = false; pill.textContent = `${active} 个任务在跑`; }
  else pill.hidden = true;
}

/* ─────────── 渲染：待审批的动作 ─────────── */

function waitText(createdAt) {
  const t = relTime(createdAt);
  return t === "刚刚" ? "刚提交" : `等了 ${t.replace("前", "")}`;
}

function approvalCard(a) {
  const agent = agentOf(a.agent_id);
  const d = a.detail || {};
  const kind = APPROVAL_KIND[a.kind] || a.kind || "操作";
  const hit = d.hit ? `<span class="ap-hit">命中「${esc(d.hit)}」</span>` : "";
  const where = d.url
    ? `<div class="ap-where">${esc(d.title ? d.title + " · " : "")}${esc(String(d.url).slice(0, 90))}</div>`
    : "";
  const el = d.element?.text
    ? `<div class="ap-el">目标元素：${esc(String(d.element.text).slice(0, 120))}</div>`
    : "";

  return `
    <article class="ap-card" data-approval="${esc(a.id)}">
      <div class="ap-top">
        <span class="ap-agent">${esc(agent.emoji)} ${esc(agent.name)}</span>
        <span class="ap-kind">${esc(kind)}</span>
        ${hit}
        <span class="ap-time">等了 ${relTime(a.created_at).replace("前", "") || "一会儿"}</span>
      </div>
      <div class="ap-summary">${esc(a.summary || "")}</div>
      ${where}${el}
      <div class="ap-actions">
        <input class="ap-note" type="text" placeholder="批注（可选，会转告它原因）" maxlength="200">
        <button class="btn-approve" data-approve="${esc(a.id)}">批准执行</button>
        <button class="btn-reject" data-reject="${esc(a.id)}">拒绝</button>
        <button class="link-btn" data-task-link="${esc(a.task_id)}">看任务 →</button>
      </div>
    </article>`;
}

function renderApprovals() {
  const bar = $("#approvalBar");
  const list = state.approvals;
  if (!list.length) { bar.hidden = true; return; }

  bar.hidden = false;
  $("#approvalCount").textContent = list.length;
  $("#approvalList").innerHTML = list.map(approvalCard).join("");

  $$("#approvalList [data-approve]").forEach((btn) =>
    btn.addEventListener("click", () => decideApproval(btn.dataset.approve, true, btn)));
  $$("#approvalList [data-reject]").forEach((btn) =>
    btn.addEventListener("click", () => decideApproval(btn.dataset.reject, false, btn)));
  $$("#approvalList [data-task-link]").forEach((btn) =>
    btn.addEventListener("click", () => openDrawer(btn.dataset.taskLink)));
}

async function loadApprovals() {
  console.log("loadApprovals start");
  try {
    const data = await api("/approvals?status=pending");
    console.log("loadApprovals data:", data);
    const before = state.approvals.length;
    state.approvals = data.approvals || [];
    console.log("loadApprovals approvals set:", state.approvals.length);
    renderApprovals();
    if (state.approvals.length > before) {
      const newest = state.approvals[0];
      toast(`有个操作等你点头：${String(newest.summary || "").slice(0, 40)}`);
    }
  } catch (err) { console.error("loadApprovals failed:", err); }
}

async function decideApproval(id, approved, btn) {
  const card = btn?.closest(".ap-card");
  const note = card?.querySelector(".ap-note")?.value.trim() || "";
  $$(`[data-approval="${id}"] button`).forEach((b) => { b.disabled = true; });
  try {
    await api(`/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify({ approved, note }),
    });
    toast(approved ? "已批准，它会继续往下走" : "已拒绝，它会换个做法");
    await loadApprovals();
    await loadTasks();
  } catch (err) {
    toast(`提交失败：${err.message}`, true);
    await loadApprovals();
  }
}

/* ─────────── 渲染：实时活动 ─────────── */

const FEED_ICON = {
  "task.created": "📥", "task.started": "▶️", "task.finished": "✅",
  "task.failed": "❌", "task.cancelled": "⏹",
  "tool.start": "⚙️", "tool.end": "✓",
  "delegate.start": "↳", "delegate.end": "↲", "delegate.failed": "⚠️",
  "sandbox.fallback": "🔄",
  "approval.requested": "🔐", "approval.decided": "🔓", "approval.timeout": "⌛",
};

const APPROVAL_KIND = {
  click: "点击按钮",
  "type+submit": "填表并提交",
  "browser.click": "点击按钮",
  "browser.type": "填写内容",
};

function feedText(e) {
  const p = e.payload || {};
  switch (e.type) {
    case "task.created":    return p.goal ? String(p.goal).slice(0, 90) : "";
    case "task.started":    return p.title || "";
    case "task.finished":   return `${p.status === "done" ? "完成" : "结束"}　${p.steps || "?"} 步　${p.duration || "?"}s　$${(p.cost_usd || 0).toFixed(4)}`;
    case "task.failed":     return p.error || "";
    case "task.cancelled":  return "任务被中止";
    case "tool.start":      return `${p.tool}: ${String(p.command || p.query || p.url || p.path || "").slice(0, 80)}`;
    case "tool.end":        return `${p.tool} 结束${p.exit_code !== undefined ? ` (exit ${p.exit_code})` : ""}`;
    case "delegate.start":  return `派给 ${p.to}：${String(p.title || "").slice(0, 60)}`;
    case "delegate.end":    return `${p.to} 回报完成`;
    case "delegate.failed": return `${p.to} 失败：${String(p.error || "").slice(0, 60)}`;
    case "sandbox.fallback": return `${p.from} 不可用，已在本机 ${p.to} 上继续跑`;
    case "approval.requested": return `等你确认：${String(p.summary || "").slice(0, 90)}`;
    case "approval.decided":   return p.approved ? "你批准了，继续执行" : "你拒绝了，已停下";
    case "approval.timeout":   return `等了 ${p.seconds || "?"} 秒没人回应，已按拒绝处理`;
    default: return "";
  }
}

function pushFeed(event) {
  const text = feedText(event);
  if (!text) return;
  state.feed.push({ ...event, text });
  if (state.feed.length > state.maxFeed) state.feed = state.feed.slice(-state.maxFeed);
  renderFeed();
}

function renderFeed() {
  const box = $("#activityFeed");
  if (!state.feed.length) {
    box.innerHTML = `<div class="feed-empty">成员开始干活时，这里会实时滚动。</div>`;
    return;
  }
  box.innerHTML = state.feed
    .slice(-60)
    .reverse()
    .map((e) => {
      const a = agentOf(e.agent_id);
      const cls = e.type.startsWith("delegate") ? " is-delegate" : "";
      return `
        <div class="feed-item${cls}">
          <span class="feed-ico">${FEED_ICON[e.type] || "·"}</span>
          <span class="feed-body">
            <span class="feed-agent">${esc(a.name)}</span>
            <span class="feed-time"> ${relTime(e.created_at)}</span>
            <div class="feed-text">${esc(e.text)}</div>
          </span>
        </div>`;
    })
    .join("");
}

/* ─────────── 渲染：系统视图 ─────────── */

function renderSystem(stats) {
  const t = stats.tasks || {};
  const r = stats.runtime || {};
  const b = stats.budget || {};

  $("#sysCards").innerHTML = `
    <div class="card"><div class="stat-num">${t.total || 0}</div><div class="stat-lab">累计任务</div></div>
    <div class="card"><div class="stat-num">${t.running || 0}</div><div class="stat-lab">正在执行</div></div>
    <div class="card"><div class="stat-num">${t.done || 0}</div><div class="stat-lab">已完成</div></div>
    <div class="card"><div class="stat-num">$${(stats.spend_today_usd || 0).toFixed(3)}</div><div class="stat-lab">今日花费</div></div>
  `;

  const tiers = stats.models?.tiers || {};
  $("#tierCard").innerHTML = Object.entries(tiers).map(([name, list]) => `
    <div class="tier-row">
      <div class="tier-name">${esc(name)}</div>
      <div class="tier-list">${
        list.length
          ? list.map((m, i) => i === 0 ? `<div>→ ${esc(m)}</div>` : `<div class="dim">　${esc(m)}</div>`).join("")
          : '<div class="dim">没有可用模型（检查 .env 里的 API key）</div>'
      }</div>
    </div>`).join("") || "<div class='tier-list'>未配置档位</div>";

  const providers = (stats.models?.providers_ready || []).join(", ") || "（无）";
  const locals = (stats.models?.local_providers || []).join(", ");
  $("#tierCard").insertAdjacentHTML("beforeend", `
    <div class="kv" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
      <div class="kv-row"><span class="kv-key">云端供应商</span><span class="kv-val">${esc(providers)}</span></div>
      ${locals ? `<div class="kv-row"><span class="kv-key">本地服务</span><span class="kv-val" style="color:var(--muted)">${esc(locals)}</span></div>` : ""}
      <div class="kv-row"><span class="kv-key">任务并发</span><span class="kv-val">${r.running_tasks || 0} / ${r.max_concurrent_tasks || "?"}</span></div>
      <div class="kv-row"><span class="kv-key">沙箱并发</span><span class="kv-val">${r.sandboxes_live || 0} / ${r.max_concurrent_sandbox || "?"}</span></div>
      <div class="kv-row"><span class="kv-key">浏览器</span><span class="kv-val">${r.browsers_live || 0} / ${r.max_concurrent_browser || "?"}</span></div>
      <div class="kv-row"><span class="kv-key">待你确认</span><span class="kv-val">${r.pending_approvals || 0} 个</span></div>
      <div class="kv-row"><span class="kv-key">今日预算</span><span class="kv-val">${
        b.limit_usd ? `$${(b.spent_today_usd || 0).toFixed(4)} / $${b.limit_usd}` : "不限"
      }</span></div>
    </div>`);

  loadUsage();
}

async function loadUsage() {
  try {
    const data = await api("/usage?days=7");
    const rows = data.summary || [];
    $("#usageCard").innerHTML = rows.length
      ? `<div class="kv">${rows.map((u) => `
          <div class="kv-row">
            <span class="kv-key">${esc(agentOf(u.agent_id).name)}</span>
            <span class="kv-val mono-sm">${esc(u.model)}　${u.calls} 次　$${(u.cost_usd || 0).toFixed(4)}</span>
          </div>`).join("")}</div>`
      : `<div class="kv-val" style="color:var(--muted)">还没有用量记录</div>`;
  } catch {
    $("#usageCard").innerHTML = `<div class="kv-val" style="color:var(--muted)">读取失败</div>`;
  }
}

/* ─────────── 任务详情抽屉 ─────────── */

async function openDrawer(taskId) {
  const data = await api(`/tasks/${taskId}`);
  const t = data.task;
  const a = agentOf(t.agent_id);
  state.currentTask = t.id;

  $("#drawerTitle").textContent = t.title || t.goal;
  $("#drawerMeta").textContent =
    `${t.id}　${a.name}　${STATUS_TEXT[t.status] || t.status}` +
    (t.finished_at && t.started_at ? `　${(t.finished_at - t.started_at).toFixed(1)}s` : "") +
    (t.cost_usd ? `　$${t.cost_usd.toFixed(4)}` : "") +
    (t.tokens ? `　${t.tokens} tok` : "");
  $("#cancelBtn").hidden = !["running", "queued"].includes(t.status);

  const steps = (data.messages || []).map((m) => {
    if (m.role === "user") {
      return `<div class="step step-user">
        <div class="step-head"><span class="step-role">📋 派单</span><span>step ${m.step}</span></div>
        <div class="step-content">${esc(m.content)}</div>
      </div>`;
    }
    if (m.role === "tool") {
      const long = m.content.length > 500;
      const body = long
        ? `<details class="step-fold"><summary>展开查看完整输出（${m.content.length} 字符）</summary>
             <div class="step-content mono">${esc(m.content)}</div></details>`
        : `<div class="step-content mono">${esc(m.content)}</div>`;
      return `<div class="step step-tool">
        <div class="step-head"><span class="step-role">⚙ ${esc(m.tool_name || "tool")}</span><span>step ${m.step}</span></div>
        ${body}
      </div>`;
    }
    if (!m.content) return "";
    return `<div class="step">
      <div class="step-head"><span class="step-role">${esc(a.emoji)} ${esc(a.name)}</span><span>step ${m.step}</span></div>
      <div class="step-content">${esc(m.content)}</div>
    </div>`;
  }).join("");

  const children = (data.children || []);
  const childHtml = children.length
    ? `<div class="section-head"><span>子任务 ${children.length} 条</span></div>` +
      children.map((c) => `
        <div class="child-box" data-task="${esc(c.id)}" style="cursor:pointer">
          <div class="child-head">${esc(agentOf(c.agent_id).emoji)} ${esc(agentOf(c.agent_id).name)}　${STATUS_TEXT[c.status] || c.status}　$${c.cost_usd.toFixed(4)}</div>
          <div class="step-content">${esc((c.result || c.error || c.title).slice(0, 400))}</div>
        </div>`).join("")
    : "";

  $("#drawerBody").innerHTML = `<div class="steps">${steps || '<div class="empty">还没有步骤记录</div>'}</div>${childHtml}`;

  $$("#drawerBody [data-task]").forEach((el) => {
    el.addEventListener("click", (ev) => { ev.stopPropagation(); openDrawer(el.dataset.task); });
  });

  $("#drawer").hidden = false;
}

function closeDrawer() {
  $("#drawer").hidden = true;
  state.currentTask = null;
}

/* ─────────── 视图切换 ─────────── */

function switchView(view) {
  state.view = view;
  $$(".view").forEach((el) => { el.hidden = el.id !== `view-${view}`; });
  $$(".view-btn").forEach((el) => el.classList.toggle("is-active", el.dataset.view === view));
  $$(".mnav-btn[data-view]").forEach((el) => el.classList.toggle("is-active", el.dataset.view === view));
  $("#topTitle").textContent = { work: "派活", tasks: "任务流", system: "系统" }[view] || view;
  $("#sidebar").classList.remove("is-open");
  if (view === "system") refreshStats();
}

/* ─────────── 提交任务 ─────────── */

async function submitTask() {
  const goal = $("#goalInput").value.trim();
  if (!goal) { toast("先写点要办的事", true); return; }

  const btn = $("#sendBtn");
  btn.disabled = true;
  btn.textContent = "派发中…";

  try {
    const agentId = $("#agentSelect").value;
    const { task_id } = await api("/tasks", {
      method: "POST",
      body: JSON.stringify({ goal, agent_id: agentId }),
    });
    $("#goalInput").value = "";
    toast(`已派给 ${agentOf(agentId).name}`);
    await loadTasks();
    switchView("tasks");
    setTimeout(() => openDrawer(task_id), 260);
  } catch (err) {
    toast(`派发失败：${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "派活";
  }
}

/* ─────────── 数据加载 ─────────── */

async function loadAgents() {
  const data = await api("/agents");
  state.agents = data.agents;
  state.entry = data.entry;
  renderRoster();
  const sub = $("#workSub");
  const entry = agentOf(state.entry);
  sub.innerHTML = `默认交给 <strong>${esc(entry.name)}</strong>，它会自己拆解、派人、汇总。`;
}

async function loadTasks() {
  const data = await api("/tasks?limit=60");
  state.tasks = data.tasks;
  renderTasks();
}

async function refreshStats() {
  try {
    const stats = await api("/stats");
    renderSystem(stats);

    // 没有任何真实云端供应商时，把假模型告警亮出来
    const noRealModel = !(stats.models?.providers_ready || []).length;
    $("#mockBanner").hidden = !noRealModel;

    const n = (stats.runtime?.running_tasks || 0) + (stats.runtime?.sandboxes_live || 0);
    const node = $("#nodeLine");
    node.innerHTML = n
      ? `<span class="dot dot-run"></span><span>${n} 个沙箱/任务在跑</span>`
      : `<span class="dot dot-live"></span><span>待命中</span>`;
    $("#spendLine").textContent = `今日 $${(stats.spend_today_usd || 0).toFixed(4)}`;

    // 数量对不上说明漏了推送（息屏、断线重连），补一次
    if ((stats.runtime?.pending_approvals || 0) !== state.approvals.length) {
      loadApprovals();
    }
  } catch { /* 静默 */ }
}

/* ─────────── SSE ─────────── */

function connectSSE() {
  if (state.sse) state.sse.close();
  const url = `/api/events${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""}`;
  const es = new EventSource(url);
  state.sse = es;

  es.onmessage = (msg) => {
    try {
      const event = JSON.parse(msg.data);
      pushFeed(event);
      if (["task.created", "task.finished", "task.failed", "task.cancelled"].includes(event.type)) {
        loadTasks();
        refreshStats();
        if (state.currentTask === event.task_id && event.type === "task.finished") {
          setTimeout(() => openDrawer(state.currentTask), 400);
        }
      }
    } catch { /* 忽略坏消息 */ }
  };

  es.onerror = () => {
    const node = $("#nodeLine");
    node.innerHTML = `<span class="dot dot-err"></span><span>连接断开，重连中…</span>`;
    // EventSource 自己会重连，这里不手动 close
  };
}

/* ─────────── 事件绑定 ─────────── */

function bind() {
  $("#sendBtn").addEventListener("click", submitTask);

  $("#goalInput").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submitTask(); }
  });

  $$(".view-btn").forEach((el) =>
    el.addEventListener("click", () => switchView(el.dataset.view)));
  $$(".mnav-btn[data-view]").forEach((el) =>
    el.addEventListener("click", () => switchView(el.dataset.view)));
  $$(".mnav-btn[data-toggle]").forEach((el) =>
    el.addEventListener("click", () => $(".activity").classList.toggle("is-open")));

  $$("[data-goto]").forEach((el) =>
    el.addEventListener("click", () => switchView(el.dataset.goto)));

  $$(".chip").forEach((el) =>
    el.addEventListener("click", () => {
      $$(".chip").forEach((c) => c.classList.remove("is-active"));
      el.classList.add("is-active");
      state.filter = el.dataset.status;
      renderTasks();
    }));

  $("#menuBtn").addEventListener("click", () => $("#sidebar").classList.toggle("is-open"));
  $("#sidebarClose").addEventListener("click", () => $("#sidebar").classList.remove("is-open"));
  $("#activityClose").addEventListener("click", () => $(".activity").classList.remove("is-open"));
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });
  $("#refreshBtn").addEventListener("click", async () => {
    await Promise.all([loadTasks(), refreshStats(), loadApprovals()]);
    toast("已刷新");
  });

  $("#cancelBtn").addEventListener("click", async () => {
    if (!state.currentTask) return;
    try {
      await api(`/tasks/${state.currentTask}/cancel`, { method: "POST" });
      toast("已发送中止信号");
      setTimeout(() => openDrawer(state.currentTask), 500);
    } catch (err) { toast(err.message, true); }
  });

  $$("[data-probe]").forEach((el) =>
    el.addEventListener("click", async () => {
      const mode = el.dataset.probe;
      const out = $("#probeOut");
      out.textContent = `正在检测 ${mode} …`;
      try {
        const res = await api("/sandbox/probe", {
          method: "POST",
          body: JSON.stringify({ mode }),
        });
        out.textContent = JSON.stringify(res, null, 2);
      } catch (err) {
        out.textContent = `检测失败：${err.message}`;
      }
    }));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeDrawer(); $(".activity").classList.remove("is-open"); }
  });

  // 回到前台时补一次数据，弥补息屏期间错过的推送
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { loadTasks(); refreshStats(); }
  });
}

/* ─────────── 启动 ─────────── */

async function boot() {
  bind();
  try {
    await loadAgents();
  } catch (err) {
    toast(`读取成员失败：${err.message}`, true);
  }
  await Promise.all([loadTasks(), refreshStats(), loadApprovals()]);
  connectSSE();
}

boot();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* 离线能力失败不影响使用 */ });
  });
}
