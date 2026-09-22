"""浏览器/联网工具 —— 让 Bot 能查资料。

两层能力：
  1. web_search —— 检索。默认走 DuckDuckGo 的 HTML 端点，不需要任何 API key。
     如果你配了 SearxNG 或 Tavily，会自动优先用它们。
  2. fetch_page —— 抓正文。默认纯 HTTP + 正则清洗，轻量；
     装了 playwright 之后可以对 JS 渲染页面做真实浏览器抓取。

为什么不用 Playwright 做所有事：8GB 的机器上开一个 Chromium 就是几百 MB，
检索类任务绝大多数页面是静态的，纯 HTTP 又快又省。
"""

from __future__ import annotations

import html
import logging
import os
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from .base import Tool, ToolContext

log = logging.getLogger("carme.tools.browser")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    """把 HTML 洗成可读文本。够用就好，不追求完美解析。"""
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL.sub("\n\n", text).strip()


def _clean_ddg_url(href: str) -> str:
    """DuckDuckGo 的结果链接是跳转链接，把真实 URL 抠出来。"""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        query = urlparse(href).query or href.split("?", 1)[-1]
        params = parse_qs(query)
        if "uddg" in params:
            return unquote(params["uddg"][0])
    return href


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "搜索互联网。返回标题、链接和摘要。\n"
        "拿到链接后，用 fetch_page 抓正文再下结论 —— 摘要经常不全或有误导。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，越具体越好"},
            "max_results": {
                "type": "integer",
                "description": "返回条数，默认 8，最多 20",
            },
        },
        "required": ["query"],
    }

    def __init__(self) -> None:
        self.searx_url = os.getenv("CARME_SEARX_URL", "").rstrip("/")
        self.tavily_key = os.getenv("TAVILY_API_KEY", "")

    async def run(self, ctx: ToolContext, query: str, max_results: int = 8) -> str:
        limit = max(1, min(int(max_results or 8), 20))
        await ctx.notify("tool.start", {"tool": "web_search", "query": query})

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers={"User-Agent": UA}
        ) as client:
            try:
                if ctx.extras.get('http_request'):
                    results = await self._duckduckgo(client, query, limit, request=ctx.extras['http_request'])
                    engine = 'duckduckgo (Docker Browser)'
                elif self.tavily_key:
                    results = await self._tavily(client, query, limit)
                    engine = "tavily"
                elif self.searx_url:
                    results = await self._searx(client, query, limit)
                    engine = "searxng"
                else:
                    results = await self._duckduckgo(client, query, limit)
                    engine = "duckduckgo"
            except Exception as exc:  # noqa: BLE001
                return f"[搜索失败] {type(exc).__name__}: {exc}\n可以改用 fetch_page 直接抓已知网址。"

        await ctx.notify("tool.end", {"tool": "web_search", "engine": engine, "count": len(results)})

        if not results:
            return f"没有搜到「{query}」的结果。换个说法再试，或直接用 fetch_page。"

        lines = [f"引擎：{engine}　共 {len(results)} 条", ""]
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. {item['title']}")
            lines.append(f"   {item['url']}")
            if item.get("snippet"):
                lines.append(f"   {item['snippet'][:400]}")
            lines.append("")
        return "\n".join(lines)

    # ---------------- 各引擎实现 ----------------

    @staticmethod
    async def _duckduckgo(client: httpx.AsyncClient, query: str, limit: int, request=None) -> list[dict]:
        if request:
            from urllib.parse import urlencode
            resp = await request('https://html.duckduckgo.com/html/?' + urlencode({'q': query}))
        else:
            resp = await client.post('https://html.duckduckgo.com/html/', data={'q': query})
        resp.raise_for_status()
        body = resp.text

        results: list[dict] = []
        # 结果块：标题链接 + 紧随其后的摘要
        pattern = re.compile(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'(?:.*?class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>)?',
            re.S | re.I,
        )
        for match in pattern.finditer(body):
            url = _clean_ddg_url(html.unescape(match.group(1)))
            title = html_to_text(match.group(2))
            snippet = html_to_text(match.group(3) or "")
            if not url.startswith("http") or not title:
                continue
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= limit:
                break
        return results

    async def _searx(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        resp = await client.get(
            f"{self.searx_url}/search",
            params={"q": query, "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in (data.get("results") or [])[:limit]
        ]

    async def _tavily(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.tavily_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in (data.get("results") or [])
        ]


class FetchPageTool(Tool):
    name = "fetch_page"
    description = (
        "抓取指定网址的正文并转成纯文本。\n"
        "render=true 时用真实浏览器渲染（慢，但能拿到 JS 动态加载的内容）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整网址，含 https://"},
            "max_chars": {
                "type": "integer",
                "description": "最多返回多少字符，默认 12000",
            },
            "render": {
                "type": "boolean",
                "description": "是否用浏览器渲染 JS，默认 false",
            },
        },
        "required": ["url"],
    }

    async def run(
        self, ctx: ToolContext, url: str, max_chars: int = 12000, render: bool = False
    ) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        limit = max(500, min(int(max_chars or 12000), 60000))
        await ctx.notify("tool.start", {"tool": "fetch_page", "url": url, "render": render})

        try:
            if render:
                raw = await self._render(ctx, url)
            elif ctx.extras.get('http_request'):
                raw = (await ctx.extras['http_request'](url)).text
            else:
                async with httpx.AsyncClient(
                    timeout=30.0,
                    follow_redirects=True,
                    headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                ) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    raw = resp.text
        except Exception as exc:  # noqa: BLE001
            return f"[抓取失败] {url}：{type(exc).__name__}: {exc}"

        text = html_to_text(raw)
        if not text:
            return f"[抓取成功但正文为空] {url}（可能是纯 JS 页面，试试 render=true）"

        truncated = len(text) > limit
        await ctx.notify(
            "tool.end", {"tool": "fetch_page", "url": url, "chars": len(text), "truncated": truncated}
        )
        head = text[:limit]
        if truncated:
            head += f"\n\n[...已截断，全文 {len(text)} 字符]"
        return f"来源：{url}\n\n{head}"

    @staticmethod
    async def _render(ctx: ToolContext, url: str) -> str:
        manager = ctx.browser_manager
        if manager is None or not manager.enabled:
            raise RuntimeError("浏览器尚未启用或配置")
        blocked = manager.check_domain(url)
        if blocked:
            raise RuntimeError(blocked)
        async with manager.handle(node=ctx.node) as handle:
            session = await handle.get()
            await session.goto(url)
            return await session.page.content()
