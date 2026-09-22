"""页面快照器 —— 代操作能力的地基。

问题：怎么让模型可靠地操作一个它「看不见」的网页？

错误做法：让模型写 CSS 选择器。它会猜，猜错就报错，然后重试，然后烧 token。
正确做法：我们把页面上所有可交互元素扫出来、编号、连同名称一起给它，
          它只说「点 [7]」，我们用编号反查真实 DOM 节点。

这个区别是「能用」和「玩具」的分界。同一套思路在 Playwright MCP、
browser-use 这些成熟项目里也是这么做的。

快照里包含三部分：
  1. 页面元信息（标题、URL、滚动位置）
  2. 可交互元素清单（编号 + 角色 + 名称 + 状态）
  3. 正文纯文本（让模型能读到内容，而不只是看到按钮）
"""

from __future__ import annotations

import json
from typing import Any

# 在浏览器上下文里跑的扫描脚本。
# 要点：
#   · 只挑真正可交互的元素，不要把整个 DOM 倒出来
#   · 过滤不可见元素（隐藏的菜单项最容易被误点）
#   · 给每个元素打上 data-carme-ref，之后靠它精确回点
#   · 用「无障碍名称」而不是 innerText 作为主要标识，更接近人看到的东西
SNAPSHOT_JS = r"""
() => {
  const MAX_ELEMENTS = 220;
  const MAX_TEXT = 12000;

  const isVisible = (el) => {
    if (!el.isConnected) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    if (parseFloat(style.opacity || '1') < 0.05) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    return true;
  };

  const visibleText = (el, limit) => {
    // 标签/对话框中嵌套的 textarea 也可能包含凭据，不把它当标签文案返回。
    let source = el;
    if (el.querySelector('input,textarea')) {
      source = el.cloneNode(true);
      source.querySelectorAll('input,textarea').forEach(n => n.remove());
    }
    const raw = (source.innerText || source.textContent || '');
    return raw.replace(/\s+/g, ' ').trim().slice(0, limit);
  };

  const labelFor = (el) => {
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return visibleText(lab, 80);
    }
    const wrap = el.closest('label');
    if (wrap) {
      const t = visibleText(wrap, 80);
      if (t) return t;
    }
    return '';
  };

  const accName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim().slice(0, 100);

    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const t = labelledby.split(/\s+/)
        .map(id => { const n = document.getElementById(id); return n ? visibleText(n, 60) : ''; })
        .filter(Boolean).join(' ').trim();
      if (t) return t.slice(0, 100);
    }

    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      const lab = labelFor(el);
      if (lab) return lab;
      const ph = el.getAttribute('placeholder');
      if (ph) return ph.slice(0, 100);
      const nm = el.getAttribute('name');
      if (nm) return nm;
      const ty = el.getAttribute('type');
      if (ty && ty !== 'text') return ty;
      return '';
    }

    const txt = visibleText(el, 100);
    if (txt) return txt;

    const title = el.getAttribute('title');
    if (title) return title.slice(0, 100);

    const img = el.querySelector('img[alt]');
    if (img && img.alt) return img.alt.slice(0, 100);

    return '';
  };

  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'summary') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'search') return 'searchbox';
      return 'textbox';
    }
    return 'generic';
  };

  const SELECTOR = [
    'a[href]', 'button', 'input:not([type="hidden"])', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="checkbox"]',
    '[role="radio"]', '[role="menuitem"]', '[role="option"]', '[role="combobox"]',
    '[role="searchbox"]', '[role="switch"]', '[onclick]',
    '[contenteditable="true"]', 'summary',
    '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  // 清掉上一轮编号，避免残留干扰
  document.querySelectorAll('[data-carme-ref]').forEach(
    n => n.removeAttribute('data-carme-ref')
  );

  const elements = [];
  const seen = new Set();

  for (const el of document.querySelectorAll(SELECTOR)) {
    if (elements.length >= MAX_ELEMENTS) break;
    if (seen.has(el)) continue;
    seen.add(el);

    const tag = el.tagName.toLowerCase();
    const rect = el.getBoundingClientRect();

    // 被包裹在另一个可交互祖先里的元素跳过，否则一个按钮会重复出现三次
    const parentInteractive = el.parentElement
      ? el.parentElement.closest(SELECTOR)
      : null;
    const nestedInsideLink = tag !== 'a' && !!el.closest('a[href]');
    if (parentInteractive && !nestedInsideLink) continue;

    if (!isVisible(el)) continue;
    if (el.disabled && tag !== 'input') { /* 保留但标记 */ }

    const ref = elements.length + 1;
    el.setAttribute('data-carme-ref', String(ref));

    const name = accName(el);
    const item = {
      ref,
      tag,
      role: el.getAttribute('role') || implicitRole(el),
      name: name || '(无名称)',
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      y: Math.round(rect.top + window.scrollY),
      in_view: rect.top < window.innerHeight && rect.bottom > 0
    };

    const type = el.getAttribute('type');
    if (type) item.type = type;

    if ('value' in el && tag !== 'button' && tag !== 'a' && tag !== 'select') {
      const v = String(el.value || '');
      // 密码、验证码及 API key 也可能是普通 text 输入框；只报告是否填过。
      if (v) item.filled = true;
    }
    if (tag === 'select') {
      const opt = el.options && el.options[el.selectedIndex];
      if (opt) item.value = String(opt.text || '').slice(0, 60);
    }
    if ('checked' in el && (type === 'checkbox' || type === 'radio')) {
      item.checked = !!el.checked;
    }
    if (el.getAttribute('aria-expanded') !== null) {
      item.expanded = el.getAttribute('aria-expanded') === 'true';
    }
    if (el.href && typeof el.href === 'string') {
      item.href = el.href.slice(0, 160);
    }
    if (el.getAttribute('placeholder')) {
      item.placeholder = el.getAttribute('placeholder').slice(0, 80);
    }

    elements.push(item);
  }

  // 正文：去掉脚本样式，压掉多余空行
  let bodyText = '';
  try {
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll('script,style,noscript,svg,iframe,input,textarea').forEach(n => n.remove());
    bodyText = (clone.innerText || clone.textContent || '')
      .replace(/[ \t\u00a0]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  } catch (e) { bodyText = ''; }

  return {
    url: location.href,
    title: document.title || '',
    elements,
    truncated: elements.length >= MAX_ELEMENTS,
    text: bodyText.slice(0, MAX_TEXT),
    text_truncated: bodyText.length > MAX_TEXT,
    scroll_y: Math.round(window.scrollY),
    scroll_height: Math.max(
      document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0
    ),
    viewport_height: window.innerHeight,
    dialogs: Array.from(document.querySelectorAll('[role="dialog"], dialog[open]'))
      .slice(0, 3).map(d => visibleText(d, 300)).filter(Boolean),
    alerts: Array.from(document.querySelectorAll('[role="alert"], .error, .alert'))
      .slice(0, 3).map(a => visibleText(a, 200)).filter(Boolean)
  };
}
"""


def format_snapshot(snap: dict[str, Any], *, max_text: int = 6000,
                    include_text: bool = True) -> str:
    """把快照渲染成模型能读的紧凑文本。

    格式刻意为「一眼能扫」设计：元素一行一个，编号在最前面，
    这样模型选目标时不会看串行。
    """
    lines: list[str] = []
    lines.append(f"标题：{snap.get('title') or '(无)'}")
    lines.append(f"网址：{snap.get('url', '')}")

    if snap.get("warning"):
        lines.append(f"⚠ {snap['warning']}")
    total = snap.get("scroll_height") or 0
    viewport = snap.get("viewport_height") or 0
    y = snap.get("scroll_y") or 0
    if total > viewport:
        lines.append(f"滚动：{y} / {max(total - viewport, 0)}")

    dialogs = snap.get("dialogs") or []
    if dialogs:
        lines.append("")
        lines.append("⚠ 页面弹窗：")
        for d in dialogs:
            lines.append(f"  {d}")

    alerts = snap.get("alerts") or []
    if alerts:
        lines.append("")
        lines.append("⚠ 页面提示：")
        for a in alerts:
            lines.append(f"  {a}")

    elements = snap.get("elements") or []
    lines.append("")
    lines.append(f"── 可交互元素（{len(elements)} 个）──")
    if not elements:
        lines.append("  （没有找到可交互元素，页面可能还在加载，或者内容是图片）")
    for el in elements:
        parts = [f"[{el['ref']}]", el.get("role") or el.get("tag", "")]
        parts.append(f'"{el.get("name", "")}"')

        extras = []
        if el.get("type"):
            extras.append(el["type"])
        if el.get("value"):
            extras.append(f'值="{el["value"]}"')
        elif el.get("filled"):
            extras.append("已填写，内容隐藏")
        if el.get("checked") is not None:
            extras.append("已勾选" if el["checked"] else "未勾选")
        if el.get("expanded") is not None:
            extras.append("展开" if el["expanded"] else "收起")
        if el.get("disabled"):
            extras.append("已禁用")
        if el.get("href"):
            extras.append(el["href"])
        if extras:
            parts.append("· " + " ".join(extras))

        lines.append("  " + " ".join(parts))

    if snap.get("truncated"):
        lines.append("  …（元素过多已截断，可用 web_scroll 或缩小范围）")

    if include_text and snap.get("text"):
        lines.append("")
        lines.append("── 正文 ──")
        text = snap["text"]
        if len(text) > max_text:
            text = text[:max_text] + f"\n…（正文已截断，共 {len(snap['text'])} 字）"
        lines.append(text)

    return "\n".join(lines)


def compact_snapshot(snap: dict[str, Any], limit: int = 40) -> str:
    """动作执行后的简短回执：只报关键变化，不重复整页正文。

    每次动作后都回一整页快照会让上下文爆炸。
    这里只给「页面变了什么」+ 最相关的少量元素。
    """
    lines = [f"标题：{snap.get('title') or '(无)'}", f"网址：{snap.get('url', '')}"]

    dialogs = snap.get("dialogs") or []
    if dialogs:
        lines.append("⚠ 弹窗：" + " | ".join(d[:200] for d in dialogs))
    alerts = snap.get("alerts") or []
    if alerts:
        lines.append("⚠ 提示：" + " | ".join(a[:200] for a in alerts))

    elements = (snap.get("elements") or [])[:limit]
    lines.append("")
    lines.append(f"── 可交互元素（前 {len(elements)} 个）──")
    for el in elements:
        extra = ""
        if el.get("value"):
            extra = f' 值="{el["value"]}"'
        elif el.get("filled"):
            extra = " 已填写，内容隐藏"
        elif el.get("disabled"):
            extra = " (已禁用)"
        lines.append(f'  [{el["ref"]}] {el.get("role", "")} "{el.get("name", "")}"{extra}')

    if snap.get("text"):
        preview = snap["text"][:1200]
        lines.append("")
        lines.append("── 正文摘要 ──")
        lines.append(preview + ("…" if len(snap["text"]) > 1200 else ""))

    return "\n".join(lines)


def snapshot_json(snap: dict[str, Any]) -> str:
    return json.dumps(snap, ensure_ascii=False, indent=None)[:4000]
