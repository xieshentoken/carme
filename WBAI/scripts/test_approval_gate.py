"""审批闸门的端到端回归测试。

为什么值得单独写一个测试：
    审批闸门是「代操作敢不敢用」的唯一保障。它坏掉的方式很隐蔽 ——
    不是报错，而是**悄悄放行**。那种失败你在日志里看不出来，
    只有真出事的时候才知道。所以必须有一条自动化的路径专门验证：
    危险动作确实被拦下、批准后确实执行、拒绝后确实没执行。

它测的是真东西，不是 mock：
    真的起一个本地 HTTP 服务、真的开 Chromium、真的点按钮、
    真的走一遍 SQLite 里的审批记录。唯一被替代的是「人」——
    审批回调是脚本模拟的。

跑法：
    ./.venv/bin/python scripts/test_approval_gate.py
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from carme.approval import ApprovalOutcome, request_approval  # noqa: E402
from carme.browser import BrowserManager  # noqa: E402
from carme.config import AgentSpec  # noqa: E402
from carme.store import Store  # noqa: E402
from carme.tools.base import ToolContext  # noqa: E402
from carme.tools.web import (  # noqa: E402
    WebClickTool,
    WebOpenTool,
    WebSnapshotTool,
)

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>审批闸门测试页</title></head>
<body>
  <h1>账号设置</h1>
  <button id="save" onclick="mark('SAVED')">保存设置</button>
  <button id="del"  onclick="mark('DELETED')">删除账号</button>
  <button id="buy"  onclick="mark('PURCHASED')">确认购买</button>
  <div id="out">初始</div>
  <script>
    function mark(v) { document.getElementById('out').textContent = v; }
  </script>
</body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 别把访问日志喷到测试输出里
        pass


def start_server() -> tuple[http.server.HTTPServer, str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/"


class FakeHuman:
    """模拟坐在界面前面的人。

    关键：它**通过改数据库来表态**，而不是直接返回一个结果。
    这一点很重要 —— 真实的审批就是这么走的（浏览器点「批准」→ API 改库 →
    正在阻塞的任务轮询到状态变化 → 继续执行）。如果这里图省事直接返回，
    就绕过了 request_approval 本身，等于没测最核心的那段。
    """

    def __init__(
        self,
        store: Store,
        decisions: list[bool],
        *,
        timeout: float = 30.0,
        decide_delay: float = 1.2,
    ) -> None:
        self.store = store
        self.decisions = list(decisions)
        self.timeout = timeout
        self.decide_delay = decide_delay
        self.requests: list[dict] = []
        self.pending_seen: list[int] = []

    async def __call__(self, *, kind: str, summary: str, detail: dict) -> ApprovalOutcome:
        self.requests.append({"kind": kind, "summary": summary, "detail": detail})

        async def decide_later() -> None:
            await asyncio.sleep(self.decide_delay)
            pending = self.store.list_approvals(status="pending")
            self.pending_seen.append(len(pending))
            if not pending or not self.decisions:
                return  # 谁都不理它 → 走超时
            approved = self.decisions.pop(0)
            self.store.decide_approval(
                pending[0]["id"],
                approved=approved,
                note="测试决定的批注" if approved else "测试拒绝了",
            )

        asyncio.create_task(decide_later())

        return await request_approval(
            self.store,
            task_id="t_test",
            agent_id="operator",
            kind=kind,
            summary=summary,
            detail=detail,
            timeout=self.timeout,
        )


def ref_of(snapshot_text: str, label: str) -> int:
    """从快照文本里按元素名称找编号。"""
    for line in snapshot_text.splitlines():
        if label in line and line.strip().startswith("["):
            return int(line.strip().split("]")[0].lstrip("["))
    raise AssertionError(f"快照里找不到「{label}」：\n{snapshot_text[:2000]}")


def page_state(snapshot_text: str) -> str:
    for token in ("SAVED", "DELETED", "PURCHASED", "初始"):
        if token in snapshot_text:
            return token
    return "?"


async def main() -> int:
    server, url = start_server()
    tmp = Path(tempfile.mkdtemp(prefix="carme-test-"))
    store = Store(tmp / "test.db")

    settings = {
        "enabled": True,
        "headless": True,
        "default_profile": "test",
        "profiles": {"test": {"user_data_dir": str(tmp / "profile")}},
        "safety": {
            "require_confirmation": True,
            "approval_timeout_seconds": 30,
            "max_concurrent_browser": 1,
            "dangerous_patterns": ["删除", "确认购买", "delete", "purchase"],
        },
        "screenshots": {"enabled": False},
    }
    manager = BrowserManager(settings, ROOT)
    human = FakeHuman(store, [True, False])  # 第一个批准，第二个拒绝

    spec = AgentSpec(id="operator", name="代操作", title="测试", tools=["computer"])
    ctx = ToolContext(
        agent=spec,
        task_id="t_test",
        store=store,
        browser_manager=manager,
        approve=human,
    )

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        checks.append((name, ok, note))
        print(f"  {'✓' if ok else '✗'} {name}" + (f"　{note}" if note else ""))

    try:
        print(f"\n测试页：{url}\n")

        print("[1] 打开页面")
        snap = await WebOpenTool().run(ctx, url)
        check("页面打开成功", "账号设置" in snap)
        check("快照给出可交互元素", "可交互元素" in snap)

        print("\n[2] 安全动作：点「保存设置」——不应触发审批")
        before = len(human.requests)
        out = await WebClickTool().run(ctx, ref_of(snap, "保存设置"))
        check("直接执行，没有拦", "已点击" in out)
        check("没有产生审批请求", len(human.requests) == before)
        snap = await WebSnapshotTool().run(ctx)
        check("页面状态已变 SAVED", page_state(snap) == "SAVED", page_state(snap))

        print("\n[3] 危险动作：点「删除账号」——应拦下，然后被批准")
        before = len(human.requests)
        out = await WebClickTool().run(ctx, ref_of(snap, "删除账号"))
        check("触发了审批请求", len(human.requests) == before + 1)
        check("拦截摘要提到危险词", "删除" in human.requests[-1]["summary"], human.requests[-1]["summary"])
        check("批准后确实执行了", "已点击" in out, out.splitlines()[0] if out else "")
        snap = await WebSnapshotTool().run(ctx)
        check("页面状态变成 DELETED", page_state(snap) == "DELETED", page_state(snap))

        print("\n[4] 危险动作：点「确认购买」——应拦下，然后被拒绝")
        before = len(human.requests)
        out = await WebClickTool().run(ctx, ref_of(snap, "确认购买"))
        check("触发了审批请求", len(human.requests) == before + 1)
        check("拒绝后返回拦截说明", "已拦截" in out)
        check("拦截信息里有替代做法", "交给用户自己点" in out)
        snap = await WebSnapshotTool().run(ctx)
        check(
            "拒绝后页面没有变化",
            page_state(snap) == "DELETED",
            f"仍是 {page_state(snap)}（未被点成 PURCHASED）",
        )

        print("\n[5] 没人理它 —— 应超时并按拒绝处理")
        impatient = FakeHuman(store, [], timeout=3.0)
        ctx.approve = impatient
        out = await WebClickTool().run(ctx, ref_of(snap, "删除账号"))
        check("超时后返回拦截说明", "已拦截" in out)
        check("说明了是超时", "没有回应" in out, out.splitlines()[0] if out else "")
        snap = await WebSnapshotTool().run(ctx)
        check("超时后页面没有变化", page_state(snap) == "DELETED", page_state(snap))

        print("\n[6] 审批记录落库情况")
        rows = store.list_approvals()
        check("三条审批记录都在库里", len(rows) == 3, f"{len(rows)} 条")
        statuses = sorted(r["status"] for r in rows)
        check(
            "状态是 approved / rejected / rejected",
            statuses == ["approved", "rejected", "rejected"],
            str(statuses),
        )
        check("没有遗留 pending", store.pending_approval_count() == 0)
        check(
            "等待期间确实能看到待审批记录",
            human.pending_seen == [1, 1],
            str(human.pending_seen),
        )
        detail = rows[0]["detail"]
        check("审批详情里记了命中的危险词", bool(detail.get("hit")), str(detail.get("hit")))

    finally:
        await manager.close_all()
        store.close()
        server.shutdown()

    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print(f"\n{'─' * 46}\n通过 {passed}/{total}")
    if passed < total:
        print("失败项：")
        for name, ok, note in checks:
            if not ok:
                print(f"  · {name}　{note}")
        return 1
    print("审批闸门工作正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
