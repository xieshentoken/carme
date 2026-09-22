"""审批通知 —— 让「人工确认」不至于变成「守着屏幕等」。

为什么需要这一层：
    审批闸门的前提是「你能及时看到」。如果确认请求只出现在网页上，
    而你正在开会、在地铁上，那闸门就只有两个结局：超时拒绝（白干），
    或者你干脆把闸门关掉（等于没有）。
    所以确认请求必须能推到手机上。

支持四种通道，按 URL 自动识别，不用改代码：

    Bark          https://api.day.app/你的key
    Server 酱     https://sctapi.ftqq.com/你的key.send
    Telegram      https://api.telegram.org/bot<token>/sendMessage   （另需 chat_id）
    通用 webhook   其余任何 URL，直接 POST 一段 JSON

配置（写在 .env 里）：
    CARME_WEBHOOK_URL=...
    CARME_TELEGRAM_CHAT_ID=...     # 仅 Telegram 需要
    CARME_NOTIFY_SECRET=...        # Server 酱 Turbo 版等需要

设计取向：**通知失败绝不能影响主流程**。所有异常吞掉只记日志，
因为「通知没发出去」远没有「任务因此崩了」严重。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger("carme.notify")

TIMEOUT = 8.0


def _detect_kind(url: str) -> str:
    low = url.lower()
    if "api.day.app" in low or "day.app" in low:
        return "bark"
    if "sctapi.ftqq.com" in low or "sc.ftqq.com" in low:
        return "serverchan"
    if "api.telegram.org" in low:
        return "telegram"
    return "generic"


class Notifier:
    """把审批请求推到你的手机。没配 URL 就是个静默的空实现。"""

    def __init__(
        self,
        url: str | None = None,
        *,
        telegram_chat_id: str | None = None,
        secret: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.url = (url if url is not None else os.getenv("CARME_WEBHOOK_URL", "")).strip()
        self.chat_id = (
            telegram_chat_id
            if telegram_chat_id is not None
            else os.getenv("CARME_TELEGRAM_CHAT_ID", "")
        ).strip()
        self.secret = (
            secret if secret is not None else os.getenv("CARME_NOTIFY_SECRET", "")
        ).strip()
        # 通知里带一个回跳地址，点一下就能回到界面处理
        self.base_url = (
            base_url if base_url is not None else os.getenv("CARME_PUBLIC_URL", "")
        ).strip().rstrip("/")
        self.kind = _detect_kind(self.url) if self.url else ""

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def describe(self) -> dict[str, Any]:
        """给 doctor / 界面看的自检信息（不泄露 key）。"""
        if not self.enabled:
            return {"enabled": False, "hint": "在 .env 里设 CARME_WEBHOOK_URL 即可推送到手机"}
        return {
            "enabled": True,
            "kind": self.kind,
            "host": self.url.split("//", 1)[-1].split("/", 1)[0],
        }

    # ---------------- 发送 ----------------

    async def send(self, title: str, body: str, *, url: str = "", payload: dict | None = None) -> bool:
        """发一条通知。永远不抛异常 —— 发不出去只记日志。"""
        if not self.enabled:
            return False
        try:
            return await self._dispatch(title, body, url, payload or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("通知发送失败（不影响任务）：%s: %s", type(exc).__name__, exc)
            return False

    async def _dispatch(self, title: str, body: str, link: str, payload: dict) -> bool:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            if self.kind == "bark":
                return await self._bark(client, title, body, link)
            if self.kind == "serverchan":
                return await self._serverchan(client, title, body, link)
            if self.kind == "telegram":
                return await self._telegram(client, title, body, link)
            return await self._generic(client, title, body, link, payload)

    async def _bark(self, client: httpx.AsyncClient, title: str, body: str, link: str) -> bool:
        # Bark 的路径式接口：/key/标题/正文，比 JSON 体更稳
        from urllib.parse import quote

        base = self.url.rstrip("/")
        resp = await client.get(
            f"{base}/{quote(title)}/{quote(body)}",
            params={"url": link} if link else None,
        )
        return resp.status_code == 200

    async def _serverchan(
        self, client: httpx.AsyncClient, title: str, body: str, link: str
    ) -> bool:
        data = {"title": title, "desp": body + (f"\n\n[打开确认]({link})" if link else "")}
        if self.secret:
            data["channel"] = self.secret
        resp = await client.post(self.url, data=data)
        return resp.status_code == 200

    async def _telegram(
        self, client: httpx.AsyncClient, title: str, body: str, link: str
    ) -> bool:
        if not self.chat_id:
            log.warning("Telegram 通知缺少 CARME_TELEGRAM_CHAT_ID，跳过")
            return False
        text = f"*{title}*\n{body}" + (f"\n{link}" if link else "")
        resp = await client.post(
            self.url,
            json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
        )
        return resp.status_code == 200

    async def _generic(
        self,
        client: httpx.AsyncClient,
        title: str,
        body: str,
        link: str,
        payload: dict,
    ) -> bool:
        resp = await client.post(
            self.url,
            json={
                "title": title,
                "text": body,
                "url": link,
                "source": "carme",
                **payload,
            },
        )
        return resp.status_code < 400

    # ---------------- 语义封装 ----------------

    async def approval_requested(self, summary: str, *, kind: str, approval_id: str) -> bool:
        link = f"{self.base_url}/?approval={approval_id}" if self.base_url else ""
        return await self.send(
            "🔐 Carme 有个操作等你点头",
            f"{summary}\n\n动作类型：{kind}\n编号：{approval_id}",
            url=link,
            payload={"event": "approval.requested", "approval_id": approval_id},
        )

    async def approval_timeout(self, summary: str, *, seconds: int) -> bool:
        return await self.send(
            "⌛ Carme 的确认请求已超时",
            f"{summary}\n\n等了 {seconds} 秒没人回应，已按「拒绝」处理。",
            payload={"event": "approval.timeout"},
        )


__all__ = ["Notifier"]
