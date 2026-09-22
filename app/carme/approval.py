"""人工确认闸门 —— 「只在关键节点找你确认」的落地。

为什么必须有这个：
    代操作意味着 Bot 能点你账号里的按钮。没有闸门，一次幻觉就可能
    删掉东西、付掉钱、发出去一封不该发的邮件。

怎么工作：
    1. 危险动作（按钮文案命中关键词）不直接执行
    2. 落一条待审批记录，往界面推事件、往你手机推一条通知
    3. 工具调用阻塞，轮询等待你的决定
    4. 你批准 → 继续执行；拒绝 → 返回拒绝信息给模型，让它换个做法

超时怎么办：
    默认 600 秒。超时按「拒绝」处理 —— 宁可任务失败，不可擅自执行。
    这个取向是有意的：代操作的失败成本不对称。少做一件事，你顶多重派一次；
    多做一件错事，可能没法撤回。

为什么用轮询而不是内存里的事件：
    审批可能来自另一个进程、另一个设备（手机浏览器打的是同一个 API）。
    状态放在 SQLite 里，谁都能读能写，进程重启也不丢。一秒一次的
    SELECT 在个人团队这个量级上完全不构成负担。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .store import Store

if TYPE_CHECKING:
    from .notify import Notifier

log = logging.getLogger("carme.approval")

POLL_INTERVAL = 1.0


@dataclass
class ApprovalOutcome:
    approved: bool
    note: str = ""
    timed_out: bool = False


async def request_approval(
    store: Store,
    *,
    task_id: str,
    agent_id: str,
    kind: str,
    summary: str,
    detail: dict[str, Any] | None = None,
    timeout: float = 600.0,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
    notifier: "Notifier | None" = None,
) -> ApprovalOutcome:
    """发起一次人工确认并等结果。

    on_event 负责推给界面（SSE），notifier 负责推到手机。两者都可以没有。
    """
    approval_id = store.create_approval(
        task_id=task_id,
        agent_id=agent_id,
        kind=kind,
        summary=summary,
        detail=detail or {},
    )

    if on_event is not None:
        await on_event(
            "approval.requested",
            {"approval_id": approval_id, "kind": kind, "summary": summary[:300]},
        )
    if notifier is not None:
        # 通知是尽力而为，不能因为它慢了就拖住任务
        asyncio.create_task(
            notifier.approval_requested(summary, kind=kind, approval_id=approval_id)
        )

    log.info("等待人工确认 %s：%s", approval_id, summary[:120])

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        row = store.get_approval(approval_id)
        if row is None:
            return ApprovalOutcome(False, "审批记录丢失")

        status = row["status"]
        if status == "approved":
            if on_event is not None:
                await on_event("approval.decided", {"approval_id": approval_id, "approved": True})
            return ApprovalOutcome(True, row.get("note") or "")
        if status == "rejected":
            if on_event is not None:
                await on_event("approval.decided", {"approval_id": approval_id, "approved": False})
            return ApprovalOutcome(False, row.get("note") or "用户拒绝了这一步")

        if loop.time() >= deadline:
            store.decide_approval(approval_id, approved=False, note="等待超时，按拒绝处理")
            if on_event is not None:
                await on_event(
                    "approval.timeout", {"approval_id": approval_id, "seconds": int(timeout)}
                )
            if notifier is not None:
                asyncio.create_task(
                    notifier.approval_timeout(summary, seconds=int(timeout))
                )
            return ApprovalOutcome(
                False,
                f"等待人工确认超过 {int(timeout)} 秒没有回应，已按拒绝处理。"
                "如果这是误判，请让用户调整 config/browser.yaml 的 dangerous_patterns。",
                timed_out=True,
            )

        await asyncio.sleep(POLL_INTERVAL)


__all__ = ["ApprovalOutcome", "request_approval"]
