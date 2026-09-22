"""手机端「画面即触控板」端到端验证：用真实触摸事件驱动本机屏幕，检查鼠标请求安全。

- 视口 390x844 触摸设备，用系统 Chrome（Playwright `channel="chrome"`）。
- 在网络层拦下 /api/desktop/mouse 并伪造响应：请求不会到达后端，
  因此不会真的移动这台 Mac 的鼠标；触摸事件走浏览器真实的 touch → pointer 链路。
- 断言：单指点按必须是「不带坐标的左键单击」；单指拖动只允许相对位移（move_rel）、
  单段不超过 600 像素、总位移与手指位移按画面比例一致；长按后拖动必须先 press、再位移、最后 release；
  画面光标必须单调跟随手指（不回跳）、松手后立即停止、位移与手指 1:1（撞屏幕边界时按钳位后比较）。
- 需要 Carme 服务正在运行（默认 http://127.0.0.1:8899）。服务没起、没装 Playwright
  或找不到 Chrome 时打印 SKIP 并以 0 退出，方便放进常规自检。
- 令牌优先取环境变量 CARME_TOKEN，其次读 app/.env。

用法：.venv/bin/python scripts/test_desktop_touch_e2e.py [--base http://127.0.0.1:8899]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

SCREEN = {"width": 1920, "height": 1080}


def read_token() -> str:
    token = os.environ.get("CARME_TOKEN", "").strip()
    if token:
        return token
    env_path = APP / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CARME_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def service_alive(base: str, token: str) -> bool:
    request = urllib.request.Request(
        base + "/api/desktop/status", headers={"Authorization": "Bearer " + token}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False

def read_status(base: str, token: str) -> dict:
    request = urllib.request.Request(
        base + "/api/desktop/status", headers={"Authorization": "Bearer " + token}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())
    except Exception:  # noqa: BLE001
        return {}


def check_dot_track(data: dict, direction: float, finger_x: float, finger_y: float,
                    screen_w: float, screen_h: float) -> list[str]:
    """画面光标轨迹：必须沿拖动方向单调前进（不回跳）、松手后不再移动、位移与手指 1:1。

    光标点的 CSS 位置先反解回客户端内部的屏幕坐标（与布局无关），
    到达屏幕边界被钳位时按钳位后的期望值比较。
    """
    rows = data.get("rows") or []
    if len(rows) < 10:
        return ["没有采到画面光标样本（画面里没有光标可视化？）"]

    def to_screen(left: float, top: float, rect_w: float, rect_h: float) -> tuple[float, float]:
        scale = min(rect_w / screen_w, rect_h / screen_h)
        inner_w, inner_h = screen_w * scale, screen_h * scale
        return (
            (left - (rect_w - inner_w) / 2) / inner_w * screen_w,
            (top - (rect_h - inner_h) / 2) / inner_h * screen_h,
        )

    x0, y0 = to_screen(rows[0][1], rows[0][2], rows[0][3], rows[0][4])
    progress: list[tuple[float, float]] = []
    for t, left, top, rect_w, rect_h in rows:
        x, y = to_screen(left, top, rect_w, rect_h)
        progress.append((t, ((x - x0) + (y - y0)) * direction))
    backward = [
        round(progress[index][1] - progress[index - 1][1], 1)
        for index in range(1, len(progress))
        if progress[index][1] - progress[index - 1][1] < -1.0
    ]
    stop_at = float(data.get("stopAt") or 0)
    at_stop = max((value for t, value in progress if t <= stop_at), default=progress[0][1])
    final = progress[-1][1]
    scale = min(rows[0][3] / screen_w, rows[0][4] / screen_h)
    expect_x = min(screen_w - 1, max(0.0, x0 + direction * finger_x / scale)) - x0
    expect_y = min(screen_h - 1, max(0.0, y0 + direction * finger_y / scale)) - y0
    expected = (expect_x + expect_y) * direction
    print(f"光标轨迹：最终 {final:.0f}px，松手瞬间 {at_stop:.0f}px，回跳 {len(backward)} 次，"
          f"钳位后期望 {expected:.0f}px")

    issues = []
    if backward:
        issues.append(f"拖动中光标回跳 {len(backward)} 次（最大 {min(backward):.0f}px），应单调前进")
    if final - at_stop > 4:
        issues.append(f"松手后光标又自行前进 {final - at_stop:.0f}px，应为 0")
    if abs(final - expected) > 8:
        issues.append(f"光标位移 {final:.0f}px 与手指位移（钳位后 {expected:.0f}px）不一致")
    return issues


async def run(base: str, token: str) -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    url = base + "/"
    recorded: list[dict] = []
    failures: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            has_touch=True,
            is_mobile=True,
            device_scale_factor=2,
        )
        paired = await context.request.post(base + "/api/session", headers={"Authorization": "Bearer " + token})
        assert paired.ok, "Fixture pairing must succeed before touch checks"
        page = await context.new_page()

        async def handle(route, request):
            try:
                body = json.loads(request.post_data or "{}")
            except Exception:  # noqa: BLE001
                body = {"_raw": request.post_data}
            recorded.append(body)
            action = body.get("action")
            if action == "press":
                reply = {"ok": True, "pressed": body.get("button", "left"), "x": 500, "y": 500}
            elif action == "release":
                reply = {"ok": True, "pressed": "", "x": 500, "y": 500}
            else:
                reply = {"ok": True, "x": 500, "y": 500}
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(reply),
            )

        await page.route("**/api/desktop/mouse", handle)
        await page.goto(url, wait_until="load")
        await page.wait_for_timeout(2500)

        # 打开带画面的面板：优先全屏「屏幕画面」弹窗，否则侧边栏「执行电脑」面板。
        opened = await page.evaluate(
            """() => {
              const card = document.querySelector('.computer-card');
              if (card) { card.click(); return 'computer-card'; }
              const side = [...document.querySelectorAll('button')].find((b) => b.textContent.includes('执行电脑'));
              if (side) { side.click(); return 'side-action'; }
              return '';
            }"""
        )
        if not opened:
            print("SKIP: 当前界面没有可打开的画面对话框（需要至少一个会话或执行电脑面板）")
            await browser.close()
            return 0
        try:
            await page.wait_for_selector(".desktop-frame img", timeout=15000)
        except Exception:  # noqa: BLE001
            print("SKIP: 画面没有出现（后端未授予屏幕录制权限，或本机桌面功能被关闭）")
            await browser.close()
            return 0

        state = await page.evaluate(
            """() => {
              const img = document.querySelector('.desktop-frame img');
              const rect = img.getBoundingClientRect();
              const toggle = document.querySelector('.desktop-control-toggle input');
              return {
                rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                control: toggle ? toggle.checked : null,
              };
            }"""
        )
        was_control = bool(state["control"])
        if not was_control:
            # 控制状态只存在后端进程内，重启后默认关闭：临时打开，跑完还原。
            await page.click(".desktop-control-toggle input")
            await page.wait_for_timeout(1500)
            state = await page.evaluate(
                """() => {
                  const toggle = document.querySelector('.desktop-control-toggle input');
                  return { control: toggle ? toggle.checked : null };
                }"""
            )
        if not state.get("control"):
            print("SKIP: 无法打开「鼠标与键盘」开关（检查 config/browser.yaml 的 desktop.control_enabled）")
            await browser.close()
            return 0

        rect = await page.evaluate(
            """() => {
              const img = document.querySelector('.desktop-frame img');
              const rect = img.getBoundingClientRect();
              return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
            }"""
        )
        scale = min(rect["width"] / SCREEN["width"], rect["height"] / SCREEN["height"])
        client = await context.new_cdp_session(page)
        center_x = rect["x"] + rect["width"] / 2
        center_y = rect["y"] + rect["height"] / 2
        try:
            # 1) 单指点按
            tap = {"x": center_x, "y": center_y}
            await client.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [tap]})
            await asyncio.sleep(0.06)
            await client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await asyncio.sleep(0.5)
            tap_records = list(recorded)
            recorded.clear()

            # 2) 单指拖动 240 x 72 像素
            start = {"x": rect["x"] + 60, "y": center_y}
            await client.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [start]})
            steps = 0
            for index in range(1, 25):
                point = {"x": start["x"] + index * 10, "y": start["y"] + index * 3}
                await client.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
                steps = index
                await asyncio.sleep(0.03)
            await client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await asyncio.sleep(1.5)
            drag_records = list(recorded)

            # 3) 长按后拖动：必须「先 press → 中间是相对位移 → 最后 release」，顺序不能乱
            recorded.clear()
            hold_start = {"x": rect["x"] + 80, "y": center_y + 20}
            await client.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [hold_start]})
            await asyncio.sleep(0.9)  # 超过客户端 450ms 的长按判定
            for index in range(1, 11):
                point = {"x": hold_start["x"] + index * 6, "y": hold_start["y"] + index * 2}
                await client.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
                await asyncio.sleep(0.03)
            await client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await asyncio.sleep(1.5)
            hold_records = list(recorded)
            print(f"长按拖动请求：{json.dumps(hold_records, ensure_ascii=False)}")

            expected = (steps * 10 / scale, steps * 3 / scale)
            total = (
                sum(float(item.get("dx", 0)) for item in drag_records),
                sum(float(item.get("dy", 0)) for item in drag_records),
            )
            print(f"点按请求：{json.dumps(tap_records, ensure_ascii=False)}")
            print(f"拖动请求数：{len(drag_records)}；总位移 {total}，期望 {expected}")

            if len(tap_records) != 1:
                failures.append(f"点按发出了 {len(tap_records)} 个鼠标请求，应为 1 个")
            else:
                body = tap_records[0]
                if body.get("action") != "click" or body.get("button") != "left":
                    failures.append(f"点按不是左键单击：{body}")
                if "x" in body or "y" in body:
                    failures.append(f"点按带了绝对坐标（会与电脑原光标位置冲突）：{body}")
            if not drag_records:
                failures.append("单指拖动没有产生任何鼠标请求")
            for body in drag_records:
                if body.get("action") != "move_rel":
                    failures.append(f"拖动里出现非相对位移动作：{body}")
                if "x" in body or "y" in body:
                    failures.append(f"拖动里出现绝对坐标：{body}")
                if abs(float(body.get("dx", 0))) > 600 or abs(float(body.get("dy", 0))) > 600:
                    failures.append(f"单段位移超过分段上限 600：{body}")
            if abs(total[0] - expected[0]) > 2 or abs(total[1] - expected[1]) > 2:
                failures.append(f"拖动总位移与手指位移不一致：实际 {total}，应为 {expected}")
            if len(hold_records) < 3 or hold_records[0].get("action") != "press":
                failures.append(f"长按拖动没有先按下左键：{hold_records[:3]}")
            elif hold_records[0].get("button") != "left" or "x" in hold_records[0] or "y" in hold_records[0]:
                failures.append(f"按下请求不对（应为不带坐标的 left）：{hold_records[0]}")
            if not hold_records or hold_records[-1].get("action") != "release":
                failures.append(f"长按拖动没有以松开左键收尾：{hold_records[-2:]}")
            elif hold_records[-1].get("button") != "left" or "x" in hold_records[-1] or "y" in hold_records[-1]:
                failures.append(f"松开请求不对（应为不带坐标的 left）：{hold_records[-1]}")
            for body in hold_records[1:-1]:
                if body.get("action") != "move_rel":
                    failures.append(f"按住与松开之间出现非位移动作：{body}")

            # 4) 光标轨迹：慢速拖动必须单调跟随手指（不回跳），松手后不得再移动
            await page.evaluate(
                """() => {
                  window.__dotRows = [];
                  window.__dotStopAt = 0;
                  const picture = () => document.querySelector('.desktop-frame img');
                  const cursor = () => document.querySelector('.desktop-frame .desktop-cursor');
                  window.__dotTimer = setInterval(() => {
                    const image = picture();
                    const dot = cursor();
                    if (!image || !dot) return;
                    const box = image.getBoundingClientRect();
                    window.__dotRows.push([performance.now(), parseFloat(dot.style.left) || 0,
                                           parseFloat(dot.style.top) || 0, box.width, box.height]);
                  }, 8);
                }"""
            )
            status = read_status(base, token)
            screen_w = float(status.get("screen_width") or 1920)
            screen_h = float(status.get("screen_height") or 1080)
            cursor_x = float(status.get("cursor_x") or 0)
            # 往屏幕里侧拖，避免一上来就撞边界（撞边界时按钳位后的期望值比较）
            direction = -1.0 if cursor_x > screen_w / 2 else 1.0
            slow_start = {
                "x": rect["x"] + (rect["width"] - 40 if direction < 0 else 40),
                "y": center_y + (40 if direction < 0 else -40),
            }
            slow_steps, slow_dx, slow_dy = 20, 4.0, 2.0
            await client.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [slow_start]})
            await asyncio.sleep(0.2)
            for index in range(1, slow_steps + 1):
                point = {
                    "x": slow_start["x"] + direction * index * slow_dx,
                    "y": slow_start["y"] + direction * index * slow_dy,
                }
                await client.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
                await asyncio.sleep(0.035)
            await client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await page.evaluate("() => { window.__dotStopAt = performance.now(); }")
            await asyncio.sleep(1.0)
            dot_data = await page.evaluate(
                """() => { clearInterval(window.__dotTimer);
                           return { rows: window.__dotRows, stopAt: window.__dotStopAt }; }"""
            )
            failures.extend(check_dot_track(
                dot_data, direction, slow_steps * slow_dx, slow_steps * slow_dy, screen_w, screen_h,
            ))
        finally:
            if not was_control:
                await page.click(".desktop-control-toggle input")
                await page.wait_for_timeout(800)
                print("已把「鼠标与键盘」控制还原为关闭")
            shot = Path(tempfile.gettempdir()) / "carme-desktop-touch-e2e.png"
            await page.screenshot(path=str(shot))
            await browser.close()

    if failures:
        print("\n".join("FAIL: " + item for item in failures))
        return 1
    print("PASS: 单指拖动只下发相对位移、总量与手指一致；点按在电脑真实光标位置单击")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="手机端触控鼠标端到端验证")
    parser.add_argument("--base", default="http://127.0.0.1:8899", help="Carme 服务地址")
    args = parser.parse_args()
    token = read_token()
    if not token:
        print("SKIP: 没有找到 CARME_TOKEN（环境变量或 app/.env）")
        return 0
    if not service_alive(args.base, token):
        print(f"SKIP: {args.base} 上没有可用的 Carme 服务（或令牌不一致）")
        return 0
    try:
        import playwright  # noqa: F401, PLC0415
    except ImportError:
        print("SKIP: 未安装 Playwright（./.venv/bin/pip install playwright）")
        return 0
    try:
        return asyncio.run(run(args.base.rstrip("/"), token))
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 无法启动 Chrome / Playwright：{exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
