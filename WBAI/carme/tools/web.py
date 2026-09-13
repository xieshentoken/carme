"""代操作工具集 —— 让 Bot 像真人一样操作网页。

这是「账号代操作」的最后一块拼图。它把四样东西接起来：

    BrowserManager   身份隔离 + 并发闸门 + 闲置回收
    BrowserSession   持久登录态 + 按 ref 操作
    snapshot         编号化的页面快照
    request_approval 危险动作的人工闸门

给模型的接口刻意做得极窄：**只用编号，不写选择器**。

    web_open       打开网址，拿到第一份快照
    web_snapshot   重新看一眼当前页面（页面变了就得重看）
    web_click      点 [7]
    web_type       在 [3] 里输入
    web_press      按键（Enter / Tab / Escape）
    web_scroll     上下滚动
    web_back       后退一页
    web_screenshot 截图留证
    web_login      打开可见窗口，请用户手动登录一次
    web_close      关掉，把内存还给系统

关于并发：**每次工具调用各自申请一个 handle**，调用结束就归还。
不用「整任务占一个浏览器」的做法，因为那会引入死锁 ——
主控占着唯一浏览器 → 派活给成员 → 成员也要浏览器 → 互等。
按调用申请则天然没这个问题：delegate 是另一次工具调用，
那时浏览器早就还回去了。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..browser import BrowserError, BrowserManager, compact_snapshot, format_snapshot
from .base import Tool, ToolContext

log = logging.getLogger("carme.tools.web")

# 输入内容不回显给模型：字段里可能是密码、验证码、身份证号
REDACT_TYPED = True


# --------------------------------------------------------------------------- #
#  取句柄 / 安全检查
# --------------------------------------------------------------------------- #


def _manager(ctx: ToolContext) -> BrowserManager:
    mgr = ctx.browser_manager
    if mgr is None:
        raise RuntimeError(
            "运行时没有装配浏览器管理器。这是程序错误，不是配置问题。"
        )
    if not mgr.enabled:
        raise RuntimeError(
            "浏览器代操作被关掉了（config/browser.yaml 的 enabled: false）。"
            "要启用请把它改成 true 并重启服务。"
        )
    return mgr


def _profile(mgr: BrowserManager, profile: str) -> str:
    return (profile or "").strip() or mgr.default_profile


def _guard_domain(mgr: BrowserManager, url: str) -> str | None:
    """返回非空字符串表示拒绝。"""
    reason = mgr.check_domain(url)
    if reason:
        return f"[已拦截] {reason}"
    return None


def _guard_readonly(mgr: BrowserManager, url: str) -> str | None:
    if mgr.is_read_only(url):
        return (
            f"[已拦截] 当前页面 {url} 在只读名单里"
            "（config/browser.yaml 的 safety.read_only_domains），不允许做写操作。"
        )
    return None


@dataclass
class Guard:
    """人工确认的结果。blocked 非空 = 这一步没执行。"""

    blocked: str = ""
    note: str = ""
    hit: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked

    def line(self) -> str:
        """给模型的附加说明，附在成功回执后面。"""
        if not self.hit:
            return ""
        extra = f"：{self.note}" if self.note else ""
        return f"（这一步命中了危险关键词「{self.hit}」，已获用户批准{extra}）\n"


async def _guard_danger(
    ctx: ToolContext,
    mgr: BrowserManager,
    *,
    kind: str,
    summary: str,
    detail: dict[str, Any],
    texts: list[str],
) -> Guard:
    """命中危险关键词就挂起等用户点头；超时按拒绝。

    ⚠️ 调用前必须已经归还浏览器槽位。
    审批可能等十分钟，而浏览器并发默认只有 1 —— 占着它干等，
    会把所有其他要用网页的成员一起冻住。
    """
    hit = mgr.danger_hit(*texts)
    if not hit:
        return Guard()

    outcome = await ctx.request_approval(
        kind=kind,
        summary=f"{summary}（命中危险词：{hit}）",
        detail={
            **detail,
            "hit": hit,
            "patterns_source": "config/browser.yaml → safety.dangerous_patterns",
        },
    )

    if outcome.approved:
        return Guard(note=outcome.note or "用户已批准", hit=hit)

    why = outcome.note if outcome.timed_out else f"用户拒绝：{outcome.note or '未说明理由'}"
    return Guard(
        blocked=(
            f"[已拦截] 这一步命中危险操作关键词「{hit}」，需要人工确认，但{why}\n"
            "不要重试同一步。可选做法：\n"
            "  1. 换一条不涉及该动作的路径完成任务；\n"
            "  2. 把这一步具体要做什么整理清楚，交给用户自己点；\n"
            "  3. 如果这是误判，请用户调整 config/browser.yaml 的 dangerous_patterns。"
        ),
        hit=hit,
    )


# --------------------------------------------------------------------------- #
#  打开 / 观察
# --------------------------------------------------------------------------- #


class WebOpenTool(Tool):
    name = "web_open"
    description = (
        "打开一个网址并返回页面快照（可交互元素编号 + 正文）。"
        "这是所有网页操作的起点。之后的 web_click / web_type 都用这里返回的编号。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "完整网址，例如 https://example.com。不带协议会自动补 https://",
            },
            "profile": {
                "type": "string",
                "description": "用哪个身份（登录态隔离）。留空用配置里的默认身份。",
            },
        },
        "required": ["url"],
    }

    async def run(self, ctx: ToolContext, url: str, profile: str = "") -> str:
        mgr = _manager(ctx)
        blocked = _guard_domain(mgr, url)
        if blocked:
            return blocked

        name = _profile(mgr, profile)
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                snap = await session.goto(url)
        except BrowserError as exc:
            return f"[失败] 打开 {url} 失败：{exc}"

        return format_snapshot(snap)


class WebSnapshotTool(Tool):
    name = "web_snapshot"
    description = (
        "重新读取当前页面，拿到最新的元素编号和正文。"
        "页面只要变过（跳转、弹窗、加载出新内容），旧编号就失效了 —— 操作前先看一眼。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "include_text": {
                "type": "boolean",
                "description": "是否附带页面正文。只关心按钮时设 false 可省 token。",
            },
            "max_text": {
                "type": "integer",
                "description": "正文最多返回多少字，默认 6000。",
            },
            "profile": {"type": "string", "description": "身份名，留空用默认。"},
        },
    }

    async def run(
        self,
        ctx: ToolContext,
        include_text: bool = True,
        max_text: int = 6000,
        profile: str = "",
    ) -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        if not mgr.has_session(name, node=ctx.node):
            return "当前没有打开任何页面。先用 web_open 打开一个网址。"

        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                snap = await session.snapshot()
        except BrowserError as exc:
            return f"[失败] 读取页面失败：{exc}"

        return format_snapshot(
            snap, include_text=bool(include_text), max_text=max(200, int(max_text))
        )


# --------------------------------------------------------------------------- #
#  写操作（都带人工确认闸门）
# --------------------------------------------------------------------------- #


class WebClickTool(Tool):
    name = "web_click"
    description = (
        "点击快照里编号为 ref 的元素。ref 来自最近一次快照。"
        "如果元素文案命中危险关键词（删除、支付、转账……），这一步会暂停等你确认。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "integer", "description": "快照里的元素编号，例如 7"},
            "expect_navigation": {
                "type": "boolean",
                "description": "如果这一下会跳转页面，设为 true 会更稳（等页面加载完）。",
            },
            "profile": {"type": "string", "description": "身份名，留空用默认。"},
        },
        "required": ["ref"],
    }

    async def run(
        self,
        ctx: ToolContext,
        ref: int,
        expect_navigation: bool = False,
        profile: str = "",
    ) -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        pending: dict[str, Any] | None = None

        # ── 第一阶段：占用浏览器读现场，判断这一步要不要人点头 ──
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                page = await session.page_context()

                blocked = _guard_readonly(mgr, page.get("url", ""))
                if blocked:
                    return blocked

                info = await session.element_summary(ref)
                if info is None:
                    return (
                        f"[失败] 编号 [{ref}] 在当前页面上找不到。"
                        "页面可能已经变化，请重新 web_snapshot 拿最新编号再试。"
                    )

                hit = mgr.danger_hit(
                    info.get("text", ""), info.get("href", ""), page.get("title", "")
                )
                if not hit:
                    snap = await session.click(ref, expect_navigation=bool(expect_navigation))
                    return "已点击。\n" + compact_snapshot(snap)

                pending = {
                    "kind": "click",
                    "summary": f"点击 [{ref}]「{info.get('text', '')[:80] or '(无文案)'}」",
                    "detail": {
                        "ref": ref,
                        "url": page.get("url", ""),
                        "title": page.get("title", ""),
                        "element": info,
                    },
                    "texts": [info.get("text", ""), info.get("href", ""), page.get("title", "")],
                }
            # ← 已出 with：浏览器槽位归还，接下来等人不会占着它
        except BrowserError as exc:
            return f"[失败] {exc}"

        # ── 第二阶段：等人点头（此刻不占浏览器）──
        guard = await _guard_danger(ctx, mgr, **pending)
        if not guard.ok:
            return guard.blocked

        # ── 第三阶段：重新占用，复核元素还在不在，然后才真的点 ──
        # 等审批可能过了十分钟，页面早变了。不复核就点，等于闭着眼睛按。
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                current_page = await session.page_context()
                current_element = await session.element_summary(ref)
                if (current_page.get("url") != pending["detail"]["url"]
                        or current_element != pending["detail"]["element"]):
                    return (
                        "用户已批准这一步，但等待期间页面已经变化，"
                        f"编号 [{ref}] 对应的页面或元素与批准时不一致，所以没有执行。\n"
                        + guard.line()
                        + "请重新 web_snapshot 看当前页面，再决定下一步。"
                    )
                snap = await session.click(ref, expect_navigation=bool(expect_navigation))
        except BrowserError as exc:
            return f"[失败] 用户已批准，但执行时出错：{exc}"

        return "已点击。\n" + guard.line() + compact_snapshot(snap)


class WebTypeTool(Tool):
    name = "web_type"
    description = (
        "在编号为 ref 的输入框里输入文字。submit=true 表示输完按回车提交（适合搜索框）。"
        "输入内容不会回显在结果里，所以不要靠返回值确认输对了 —— 看页面反馈。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "integer", "description": "输入框的编号"},
            "text": {"type": "string", "description": "要输入的内容"},
            "clear": {
                "type": "boolean",
                "description": "输入前先清空原有内容，默认 true。",
            },
            "submit": {
                "type": "boolean",
                "description": "输完是否按回车提交，默认 false。",
            },
            "profile": {"type": "string", "description": "身份名，留空用默认。"},
        },
        "required": ["ref", "text"],
    }

    async def run(
        self,
        ctx: ToolContext,
        ref: int,
        text: str,
        clear: bool = True,
        submit: bool = False,
        profile: str = "",
    ) -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        guard = Guard()
        pending: dict[str, Any] | None = None

        # ── 第一阶段：读现场 ──
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                page = await session.page_context()

                info = await session.element_summary(ref)
                if info is None:
                    return (
                        f"[失败] 编号 [{ref}] 在当前页面上找不到。"
                        "请重新 web_snapshot 拿最新编号。"
                    )

                # 只有「回车提交」才算不可逆动作。
                # 检查的是「这个框是干什么的」+ 页面标题，而不是输入内容本身 ——
                # 在搜索框里输入「删除」两个字并不危险，点「确认删除」才危险。
                hit = (
                    mgr.danger_hit(info.get("text", ""), page.get("title", ""))
                    if submit
                    else None
                )
                if not hit:
                    if submit:
                        blocked = _guard_readonly(mgr, page.get("url", ""))
                        if blocked:
                            return blocked
                    snap = await session.type_text(
                        ref, text, clear=bool(clear), submit=bool(submit)
                    )
                    shown = "（内容已隐藏）" if REDACT_TYPED else f"「{text[:60]}」"
                    tail = "并按了回车。" if submit else "。"
                    return (
                        f"已在 [{ref}] 输入 {len(text)} 个字符{shown}{tail}\n"
                        + compact_snapshot(snap)
                    )

                pending = {
                    "kind": "type+submit",
                    "summary": f"在 [{ref}]「{info.get('text', '')[:60]}」输入内容并回车提交",
                    "detail": {
                        "ref": ref,
                        "url": page.get("url", ""),
                        "title": page.get("title", ""),
                        "element": info,
                        "chars": len(text),
                    },
                    "texts": [info.get("text", ""), page.get("title", "")],
                }
        except BrowserError as exc:
            return f"[失败] {exc}"

        # ── 第二阶段：等人点头（不占浏览器）──
        guard = await _guard_danger(ctx, mgr, **pending)
        if not guard.ok:
            return guard.blocked

        # ── 第三阶段：重新占用，复核后执行 ──
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                current_page = await session.page_context()
                current_element = await session.element_summary(ref)
                if (current_page.get("url") != pending["detail"]["url"]
                        or current_element != pending["detail"]["element"]):
                    return (
                        "用户已批准这一步，但等待期间页面已经变化，"
                        f"编号 [{ref}] 对应的页面或元素与批准时不一致，所以没有执行。请重新 web_snapshot 看当前页面。\n"
                        + guard.line()
                    )
                snap = await session.type_text(
                    ref, text, clear=bool(clear), submit=bool(submit)
                )
        except BrowserError as exc:
            return f"[失败] 用户已批准，但执行时出错：{exc}"

        shown = "（内容已隐藏）" if REDACT_TYPED else f"「{text[:60]}」"
        return (
            f"已在 [{ref}] 输入 {len(text)} 个字符{shown}并按了回车。\n"
            + guard.line()
            + compact_snapshot(snap)
        )


class WebPressTool(Tool):
    name = "web_press"
    description = "在当前页面按一个键。常用：Enter、Tab、Escape、PageDown、ArrowDown。"
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "键名，例如 Enter"},
            "profile": {"type": "string", "description": "身份名，留空用默认。"},
        },
        "required": ["key"],
    }

    async def run(self, ctx: ToolContext, key: str, profile: str = "") -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                snap = await session.press(key)
        except BrowserError as exc:
            return f"[失败] {exc}"
        return f"已按下 {key}。\n" + compact_snapshot(snap)


# --------------------------------------------------------------------------- #
#  浏览辅助
# --------------------------------------------------------------------------- #


class WebScrollTool(Tool):
    name = "web_scroll"
    description = (
        "上下滚动页面。长页面里目标元素看不到时用它；滚完编号不变，但内容会更新。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["down", "up"],
                "description": "滚动方向，默认 down。",
            },
            "amount": {
                "type": "integer",
                "description": "滚动像素数，默认 700（约一屏）。",
            },
            "profile": {"type": "string", "description": "身份名，留空用默认。"},
        },
    }

    async def run(
        self,
        ctx: ToolContext,
        direction: str = "down",
        amount: int = 700,
        profile: str = "",
    ) -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        step = max(100, min(int(amount), 5000))
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                snap = await session.scroll(direction=direction, amount=step)
        except BrowserError as exc:
            return f"[失败] {exc}"
        pos = f"{snap.get('scroll_y', 0)} / {snap.get('scroll_height', 0)}"
        return f"已向{direction}滚动 {step}px，当前位置 {pos}。\n" + compact_snapshot(snap)


class WebBackTool(Tool):
    name = "web_back"
    description = "浏览器后退一页。"
    parameters = {
        "type": "object",
        "properties": {"profile": {"type": "string", "description": "身份名，留空用默认。"}},
    }

    async def run(self, ctx: ToolContext, profile: str = "") -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        try:
            async with mgr.handle(profile=name, node=ctx.node) as handle:
                session = await handle.get()
                snap = await session.go_back()
        except BrowserError as exc:
            return f"[失败] 后退失败：{exc}"
        return "已后退。\n" + compact_snapshot(snap)


class WebScreenshotTool(Tool):
    name = "web_screenshot"
    description = (
        "给当前页面截图存到本地，返回文件路径。"
        "关键步骤留证、或者要把页面样子给用户看时用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "文件名备注，例如 login-done"},
            "full_page": {
                "type": "boolean",
                "description": "是否整页截图（含滚动区），默认按配置。",
            },
            "profile": {"type": "string", "description": "身份名，留空用默认。"},
        },
    }

    async def run(
        self,
        ctx: ToolContext,
        name: str = "",
        full_page: bool | None = None,
        profile: str = "",
    ) -> str:
        mgr = _manager(ctx)
        if not mgr.shots.get("enabled", True):
            return "[已跳过] 截图功能被关掉了（config/browser.yaml 的 screenshots.enabled）。"

        profile_name = _profile(mgr, profile)
        slug = "".join(c for c in (name or "shot") if c.isalnum() or c in "-_")[:40] or "shot"
        path = mgr.screenshots_dir / ctx.task_id[:16] / f"{slug}_{int(time.time())}.png"
        use_full = mgr.screenshot_full_page if full_page is None else bool(full_page)

        try:
            async with mgr.handle(profile=profile_name, node=ctx.node) as handle:
                session = await handle.get()
                await session.screenshot(path, full_page=use_full)
        except BrowserError as exc:
            return f"[失败] 截图失败：{exc}"

        mgr.prune_screenshots()
        kb = path.stat().st_size / 1024 if path.exists() else 0
        return f"截图已保存：{path}（{kb:.0f} KB，{'整页' if use_full else '可视区'}）"


# --------------------------------------------------------------------------- #
#  登录 / 关闭
# --------------------------------------------------------------------------- #


class WebLoginTool(Tool):
    name = "web_login"
    description = (
        "打开一个**可见**的浏览器窗口并跳到登录页，请用户手动完成登录"
        "（验证码、扫码、短信验证码都行）。登录态会持久化，之后的任务自动复用。"
        "只在首次登录或登录态失效时用；平时直接 web_open 就行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "登录页网址"},
            "profile": {
                "type": "string",
                "description": "身份名。不同账号用不同身份名，登录态互不干扰。",
            },
        },
        "required": ["url"],
    }

    async def run(self, ctx: ToolContext, url: str, profile: str = "") -> str:
        mgr = _manager(ctx)
        blocked = _guard_domain(mgr, url)
        if blocked:
            return blocked

        name = _profile(mgr, profile)
        # 已开的会话可能是无头模式，必须先关掉才能换成可见窗口
        await mgr.close_profile(name, node=ctx.node)

        try:
            handle = mgr.handle(profile=name, headless=False, node=ctx.node)
            async with handle:
                session = await handle.get()
                snap = await session.goto(url)
        except BrowserError as exc:
            return (
                f"[失败] 打开可见浏览器失败：{exc}\n"
                "如果服务跑在没有图形界面的环境里，改不了可见模式；"
                "这种情况下请在能显示窗口的机器上先登录一次，"
                "再把 data/browser/<身份名> 目录整体拷过去。"
            )

        return (
            f"已打开可见浏览器窗口并跳转到：{snap.get('url', url)}\n\n"
            "现在请让用户在这个窗口里手动完成登录。登录完成后用户会告诉你，"
            "你再继续下一步。\n"
            f"登录态会写入 {name} 这个身份的持久化目录，之后不用重复登录。\n\n"
            + compact_snapshot(snap)
        )


class WebCloseTool(Tool):
    name = "web_close"
    description = (
        "断开当前执行电脑的浏览器自动化连接。远端 Chrome 和登录现场继续保留。"
        "登录态不会丢，下次打开仍是登录状态。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "description": "要关闭的身份名，留空关闭默认身份。",
            }
        },
    }

    async def run(self, ctx: ToolContext, profile: str = "") -> str:
        mgr = _manager(ctx)
        name = _profile(mgr, profile)
        if not mgr.has_session(name, node=ctx.node):
            return f"身份 {name} 的浏览器本来就没开着。"
        await mgr.close_profile(name, node=ctx.node)
        return f"已断开身份 {name} 的浏览器连接。远端 Chrome 与登录现场保持运行。" if ctx.node is not None else f"已关闭身份 {name} 的浏览器。"


# 供 base.build_registry 一次性装配
WEB_TOOLS: list[Tool] = [
    WebOpenTool(),
    WebSnapshotTool(),
    WebClickTool(),
    WebTypeTool(),
    WebPressTool(),
    WebScrollTool(),
    WebBackTool(),
    WebScreenshotTool(),
    WebLoginTool(),
    WebCloseTool(),
]

__all__ = [
    "WEB_TOOLS",
    "WebOpenTool",
    "WebSnapshotTool",
    "WebClickTool",
    "WebTypeTool",
    "WebPressTool",
    "WebScrollTool",
    "WebBackTool",
    "WebScreenshotTool",
    "WebLoginTool",
    "WebCloseTool",
]
