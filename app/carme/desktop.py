"""本机 macOS 桌面截图与人工鼠标控制。

该模块只负责当前 Carme 后端所在 Mac 的主屏幕。截图保存在进程内存中，
不会写入文件；鼠标注入依赖 macOS 的 Quartz/CoreGraphics，且只能由
已经通过 Carme API 鉴权的人工前端调用。
"""

from __future__ import annotations

import io
import os
import math
import platform as platform_module
import sys
import threading
import time
from typing import Any


class DesktopError(RuntimeError):
    """桌面能力错误，消息可直接显示给人工用户。"""


class DesktopUnavailable(DesktopError):
    """屏幕截图或桌面环境当前不可用。"""


class DesktopControlDisabled(DesktopError):
    """控制尚未由人工在内存中打开。"""


class DesktopInputError(DesktopError):
    """鼠标输入参数不符合当前屏幕或动作约束。"""


class DesktopController:
    """当前 macOS 主屏幕的截图和鼠标控制器。

    ``control_enabled`` 是配置允许打开控制的总开关；实际控制状态只存在于
    当前进程内，初始关闭，服务重启不会把鼠标控制自动恢复为开启状态。
    """

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        if sys.platform == "darwin":
            # 守护进程环境可能精简了 PATH（如 daemonize_serve.py），
            # 而 Pillow 的 ImageGrab 依赖 /usr/sbin/screencapture —— 启动时补全。
            path_env = os.environ.get("PATH", "")
            if "/usr/sbin" not in path_env.split(":"):
                os.environ["PATH"] = (path_env.rstrip(":") + ":/usr/sbin").lstrip(":")
        self._lock = threading.RLock()
        self._last_jpeg = b""
        self._last_capture_at = 0.0
        self._screen_width = 0
        self._screen_height = 0
        self._last_error = ""
        self._quartz: Any = None
        # CGEventPost 之后立刻回读光标会滞后（读到的是若干步之前的位置，实测约 250ms 才稳定）。
        # 这里记住「我们让光标去的目标位置」和最近几步实际生效的位移，用来识别这种滞后；
        # 否则每次 move_rel 都会拿滞后值当基准，把前面的位移覆盖掉——表现为乱跳。
        self._cursor_x: float | None = None
        self._cursor_y: float | None = None
        self._last_step: tuple[float, float] | None = None
        self._steps: list[tuple[float, float]] = []
        # 长按拖动：当前按住的鼠标键（None = 没有按住）与最近一次动作时间（安全阀用）。
        self._pressed_button: str | None = None
        self._pressed_at = 0.0
        self._cursor_at = 0.0
        self._control_enabled = False
        self.enabled = True
        self.control_allowed = False
        self.jpeg_quality = 82
        self.max_fps = 5.0
        self.reconfigure(settings or {}, initial=True)

    def reconfigure(self, settings: dict[str, Any] | None = None, *, initial: bool = False) -> None:
        """热更新 desktop 配置，不持久化当前人工控制状态。"""
        raw = settings or {}
        try:
            quality = int(raw.get("jpeg_quality", 82))
        except (TypeError, ValueError):
            quality = 82
        try:
            max_fps = float(raw.get("max_fps", 5))
        except (TypeError, ValueError):
            max_fps = 5.0
        with self._lock:
            self.enabled = bool(raw.get("enabled", True))
            self.control_allowed = bool(raw.get("control_enabled", False))
            self.jpeg_quality = max(1, min(95, quality))
            self.max_fps = max(0.1, min(60.0, max_fps))
            if initial or not self.enabled or not self.control_allowed:
                self._control_enabled = False
            self._release_press()
            self._forget_cursor()
            self._last_jpeg = b""
            self._last_capture_at = 0.0
            self._last_error = ""

    def close(self) -> None:
        """释放进程内缓存和控制状态；不操作或关闭用户桌面窗口。"""
        with self._lock:
            self._last_jpeg = b""
            self._last_capture_at = 0.0
            self._screen_width = 0
            self._screen_height = 0
            self._last_error = ""
            self._control_enabled = False
            self._release_press()
            self._forget_cursor()
            self._quartz = None

    def status(self) -> dict[str, Any]:
        """返回当前能力状态；探测失败只写入可读错误，不向 API 抛异常。"""
        with self._lock:
            enabled = self.enabled
        error = ""
        available = False
        if not enabled:
            error = "本机桌面功能在 config/browser.yaml 的 desktop.enabled 中被关闭。"
        else:
            try:
                self._capture()
                available = True
            except DesktopError as exc:
                error = str(exc)
        logical_width = logical_height = 0
        if error:
            try:
                import Quartz
                _bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
                logical_width = int(_bounds.size.width)
                logical_height = int(_bounds.size.height)
            except Exception:
                pass
        cursor_x = cursor_y = 0.0
        cursor_probed = False
        try:
            import Quartz
            _point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            cursor_x = float(_point.x)
            cursor_y = float(_point.y)
            cursor_probed = True
        except Exception:
            pass
        with self._lock:
            if error:
                self._last_error = error
            elif available:
                self._last_error = ""
            if logical_width:
                self._screen_width = logical_width
                self._screen_height = logical_height
            if cursor_probed and time.monotonic() - self._cursor_at > 0.6:
                # 距最近一次鼠标动作已超过事件稳定时间（实测约 250ms）：用稳定回读校正认知
                # （用户自己移动过鼠标、或事件被屏幕边界钳位）。若回读仍只是我们自己事件的
                # 滞后，就不要覆盖认知。
                if self._lag_aware(cursor_x, cursor_y) is None:
                    self._remember(cursor_x, cursor_y, None)
            return {
                "enabled": self.enabled,
                "control_enabled": self._control_enabled,
                "available": available or bool(self._last_jpeg),
                "screen_width": self._screen_width,
                "screen_height": self._screen_height,
                "cursor_x": cursor_x,
                "cursor_y": cursor_y,
                "error": self._last_error,
                "platform": sys.platform,
                "pressed": self._pressed_button or "",
            }

    def screenshot(self) -> bytes:
        """抓取主屏并编码为 JPEG bytes，绝不落盘。"""
        with self._lock:
            if not self.enabled:
                raise DesktopUnavailable(
                    "本机桌面功能在 config/browser.yaml 的 desktop.enabled 中被关闭。"
                )
        try:
            return self._capture()
        except DesktopError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DesktopUnavailable(f"读取本机屏幕失败：{exc}") from exc

    def set_control(self, enabled: bool) -> dict[str, Any]:
        """在当前进程内打开或关闭人工鼠标控制。"""
        with self._lock:
            if enabled and not self.enabled:
                raise DesktopUnavailable(
                    "本机桌面功能已关闭，不能打开鼠标控制；请启用 desktop.enabled。"
                )
            if enabled and not self.control_allowed:
                raise DesktopControlDisabled(
                    "鼠标控制未获配置允许。请在 config/browser.yaml 的 desktop.control_enabled "
                    "中设为 true 后重新加载配置；控制状态只保存在当前进程内。"
                )
            self._control_enabled = bool(enabled)
            if not self._control_enabled:
                # 先松开可能按住的键（用当前认知的位置），再丢掉认知：顺序反了会拿不到位置，
                # 把松开事件投递到 (0,0)，光标会被直接拽到屏幕左上角。
                self._release_press()
                self._forget_cursor()
            return self._status_without_probe()

    def mouse(
        self,
        *,
        action: str,
        x: float | None = None,
        y: float | None = None,
        dx: float | None = None,
        dy: float | None = None,
        button: str = "left",
        clicks: int = 1,
        delta_y: float | None = None,
    ) -> dict[str, Any]:
        """执行一次鼠标动作，不提供键盘注入。

        - ``move`` / ``click`` / ``scroll`` 使用绝对坐标（桌面浏览器上的鼠标与滚轮）。
        - ``move_rel`` 只累加**相对位移**：位移基准取自电脑当前真实光标位置，并允许
          停留在这台 Mac 的任意一块屏幕上，因此不会把用户自己移动过的光标拉回原地，
          也不会与被控电脑原本的鼠标位置互相打架。单次相对位移上限 1200 像素，
          手机端应把更长的滑动拆成多次下发。
        - 返回值里的 ``x`` / ``y`` 是**我们让光标去的目标位置**，不是投递后立刻回读的
          值：立刻回读会滞后一拍，客户端拿它当基准会把画面光标往回拽。
        - ``press`` / ``release`` 是长按拖动用的按键原语：只按下或松开 ``button``，
          位置取电脑当前光标位置（也可显式给 x/y），不产生点击。按住状态由后端跟踪；
          关闭控制、重载配置、服务退出或长时间没有后续动作都会自动松开，
          不会把鼠标键留在按下状态。
        """
        with self._lock:
            if not self._control_enabled:
                raise DesktopControlDisabled(
                    "鼠标控制尚未打开。请先在「本机屏幕」面板打开鼠标控制开关。"
                )
            if not self.enabled:
                raise DesktopUnavailable("本机桌面功能已关闭。")
            width, height = self._screen_dimensions()
            if action not in {"move", "move_rel", "click", "scroll", "press", "release"}:
                raise DesktopInputError(
                    "鼠标 action 只能是 move、move_rel、click、scroll、press 或 release。"
                )
            quartz = self._load_quartz()
            self._release_stale_press(quartz)

            def real_cursor() -> tuple[float, float]:
                _point = quartz.CGEventGetLocation(quartz.CGEventCreate(None))
                return float(_point.x), float(_point.y)

            def settled_cursor() -> tuple[float, float]:
                """回读光标位置，并识别「刚投递的事件还没被系统处理」造成的滞后。

                回读与我们的认知一致时采信回读；若回读正好等于「认知 − 最近 k 步位移之和」
                （k = 1..N，一屏快滑时可能一次落后好几步），判定为滞后并沿用认知；
                其余情况（用户自己动了鼠标、事件被屏幕边界钳位）以真实位置为准。
                """
                real_x, real_y = real_cursor()
                if self._cursor_x is None or self._cursor_y is None:
                    return real_x, real_y
                if abs(real_x - self._cursor_x) <= 1.5 and abs(real_y - self._cursor_y) <= 1.5:
                    return real_x, real_y
                lagging = self._lag_aware(real_x, real_y)
                return lagging if lagging is not None else (real_x, real_y)

            applied: tuple[float, float] | None = None
            if action == "move_rel":
                if dx is None or dy is None or not math.isfinite(float(dx)) or not math.isfinite(float(dy)):
                    raise DesktopInputError("move_rel 必须提供有限的 dx、dy。")
                if abs(float(dx)) > 1200 or abs(float(dy)) > 1200:
                    raise DesktopInputError("单次相对位移不能超过 1200 像素。")
                cur_x, cur_y = settled_cursor()
                # 相对拖动只把手机上的滑动量叠加到电脑真实光标位置上；
                # 位置不限制在主屏内，否则光标正处于第二块屏幕时会被硬拉回主屏，
                # 在手机端就表现为一拖就乱跳。
                left, top, right, bottom = self._cursor_bounds(quartz, width, height)
                x = max(left, min(right - 1, cur_x + float(dx)))
                y = max(top, min(bottom - 1, cur_y + float(dy)))
                applied = (x - cur_x, y - cur_y)
            elif action in {"move", "click", "press", "release"}:
                if action in {"press", "release"} and x is None and y is None:
                    # 长按拖动：不带坐标的按下/松开都作用在电脑当前光标位置
                    x, y = settled_cursor()
                elif action == "click" and x is None and y is None:
                    # 触控板模式:不带坐标的点击 = 在电脑当前真实光标位置点击,
                    # 避免手机端预测位置与电脑真实光标竞态。
                    x, y = settled_cursor()
                else:
                    self._validate_point(x, y, width, height)
            elif (x is None) != (y is None):
                raise DesktopInputError("scroll 如提供坐标，必须同时提供 x 和 y。")
            elif x is not None and y is not None:
                self._validate_point(x, y, width, height)

            if action in {"move", "move_rel"}:
                self._post_mouse(quartz, x, y, "move")
            elif action == "click":
                if button not in {"left", "right", "middle"}:
                    raise DesktopInputError("button 只能是 left、right 或 middle。")
                if not isinstance(clicks, int) or not 1 <= clicks <= 3:
                    raise DesktopInputError("clicks 必须是 1、2 或 3。")
                # 不能带着按下的键点击：先松开，避免把按键状态留在电脑上
                self._release_press()
                self._post_clicks(quartz, x, y, button, clicks)
            elif action in {"press", "release"}:
                if button not in {"left", "right", "middle"}:
                    raise DesktopInputError("button 只能是 left、right 或 middle。")
                if action == "press":
                    if self._pressed_button is None:
                        self._post_button(quartz, x, y, button, down=True)
                        self._pressed_button = button
                    elif self._pressed_button != button:
                        raise DesktopInputError(
                            f"已经按住了 {self._pressed_button} 键；一次只能按住一个键，请先松开。"
                        )
                elif self._pressed_button is not None:
                    self._post_button(quartz, x, y, self._pressed_button, down=False)
                    self._pressed_button = None
            else:
                if delta_y is None or not math.isfinite(float(delta_y)):
                    raise DesktopInputError("scroll 必须提供有限的 delta_y。")
                delta = float(delta_y)
                if abs(delta) > 10000:
                    raise DesktopInputError("delta_y 绝对值不能超过 10000。")
                if x is not None and y is not None:
                    self._post_mouse(quartz, x, y, "move")
                amount = int(round(delta))
                unit = getattr(quartz, "kCGScrollEventUnitPixel", 0)
                event = quartz.CGEventCreateScrollWheelEvent(None, unit, 1, amount)
                if event is None:
                    raise DesktopError("macOS 没有创建滚轮事件；请检查辅助功能权限。")
                quartz.CGEventPost(quartz.kCGHIDEventTap, event)

            # 返回"我们让光标去的那个位置"，而不是投递后立刻回读：
            # 立刻回读会滞后一拍，客户端拿它当基准就会把画面光标往回拽。
            prev_x, prev_y = self._cursor_x, self._cursor_y
            if x is not None and y is not None:
                if applied is not None:
                    step = applied
                elif prev_x is not None and prev_y is not None:
                    # 绝对动作也记下认知的位移：紧随其后的相对拖动会遇到同样的滞后回读
                    step = (float(x) - prev_x, float(y) - prev_y)
                else:
                    step = None
                self._remember(float(x), float(y), step)
            elif self._cursor_x is None or self._cursor_y is None:
                self._remember(*real_cursor(), None)
            result: dict[str, Any] = {
                "ok": True,
                "action": action,
                "x": float(self._cursor_x),  # type: ignore[arg-type]
                "y": float(self._cursor_y),  # type: ignore[arg-type]
            }
            if action == "click":
                result["button"] = button
                result["clicks"] = clicks
            if action == "scroll":
                result["delta_y"] = float(delta_y or 0)
            if self._pressed_button is not None:
                # 有动作就刷新时间戳：安全阀只针对"客户端不管了"的情况
                self._pressed_at = time.monotonic()
            result["pressed"] = self._pressed_button or ""
            return result

    # macOS ANSI 虚拟键码（kVK_*）
    _VK: dict[str, int] = {
        "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "forwarddelete": 117,
        "escape": 53, "esc": 53, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
        "up": 126, "down": 125, "left": 123, "right": 124,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
        "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
        "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
        "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    }
    _MODS: dict[str, int] = {
        "cmd": 1 << 20, "command": 1 << 20, "control": 1 << 18, "ctrl": 1 << 18,
        "option": 1 << 19, "alt": 1 << 19, "shift": 1 << 17,
    }

    def keyboard(self, *, keys: str) -> dict[str, Any]:
        """注入一次按键：可打印字符直接转发；特殊键/组合键用虚拟键码 + 修饰掩码。"""
        with self._lock:
            if not self._control_enabled:
                raise DesktopControlDisabled("鼠标与键盘控制尚未打开。请先在画面面板打开控制开关。")
            if not self.enabled:
                raise DesktopUnavailable("本机桌面功能已关闭。")
            quartz = self._load_quartz()
            text = str(keys or "").strip()
            if not text or len(text) > 24 or any(ord(ch) < 32 for ch in text):
                raise DesktopInputError("按键描述为 1–24 个可见字符，形如 a、b 或 cmd+c。")
            parts = [part for part in text.split("+") if part.strip()]
            flags = 0
            while parts and parts[0].lower() in self._MODS:
                flags |= self._MODS[parts.pop(0).lower()]
            if not parts or len(parts) > 1:
                raise DesktopInputError("一次发送一个主键，修饰键放在前面，如 cmd+c、shift+return。")
            key = parts[0]
            if flags == 0 and len(key) == 1 and ord(key) > 32 and key not in self._VK:
                for down in (True, False):
                    event = quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    if event is None:
                        raise DesktopError("macOS 没有创建键盘事件；请检查辅助功能权限。")
                    try:
                        quartz.CGEventKeyboardSetUnicodeString(event, len(key), key)
                    except TypeError:
                        quartz.CGEventKeyboardSetUnicodeString(event, key)
                    quartz.CGEventPost(quartz.kCGHIDEventTap, event)
                return {"ok": True, "keys": key}
            vkey = self._VK.get(key.lower())
            if vkey is None:
                raise DesktopInputError(f"不支持的按键：{key}（支持字母/数字、cmd/ctrl/option/shift 组合与常用特殊键）")
            for down in (True, False):
                event = quartz.CGEventCreateKeyboardEvent(None, vkey, down)
                if event is None:
                    raise DesktopError("macOS 没有创建键盘事件；请检查辅助功能权限。")
                if flags:
                    quartz.CGEventSetFlags(event, flags)
                quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            return {"ok": True, "keys": text}

    def _forget_cursor(self) -> None:
        """丢掉对光标位置的认知；下次动作重新以真实回读为准。"""
        self._cursor_x = None
        self._cursor_y = None
        self._last_step = None
        self._steps = []
        self._cursor_at = 0.0

    def _remember(self, x: float, y: float, step: tuple[float, float] | None) -> None:
        """记住我们让光标去的位置；``step`` 是这一步实际生效的位移（None 表示认知重置）。"""
        self._cursor_x = float(x)
        self._cursor_y = float(y)
        self._last_step = step
        if step is None:
            self._steps = []
        else:
            self._steps.append((float(step[0]), float(step[1])))
            if len(self._steps) > 8:  # 只留最近几步，足够覆盖一次快滑的事件稳定窗口
                del self._steps[0]
        self._cursor_at = time.monotonic()

    def _lag_aware(self, real_x: float, real_y: float) -> tuple[float, float] | None:
        """回读是否只是「我们刚投递的事件还没被系统处理」？

        是（回读正好等于认知减去最近 k 步位移之和）则返回我们认知的位置，否则返回 None。
        一次快滑会在约 250ms 的稳定窗口里下发好几次相对位移，所以必须按「落后任意 k 步」
        判断；只比对最后一步时，第 3 次之后的位移会退回陈旧基准，指针反而往回跳。
        """
        belief_x, belief_y = self._cursor_x, self._cursor_y
        if belief_x is None or belief_y is None or not self._steps:
            return None
        offset_x = belief_x - real_x
        offset_y = belief_y - real_y
        total_x = total_y = 0.0
        for step in reversed(self._steps):
            total_x += step[0]
            total_y += step[1]
            if abs(offset_x - total_x) <= 1.5 and abs(offset_y - total_y) <= 1.5:
                return belief_x, belief_y
        return None

    def _release_press(self) -> None:
        """尽力松开当前按住的鼠标键；没有按住、或环境不可用时什么都不做。"""
        button, self._pressed_button = self._pressed_button, None
        if button is None:
            return
        try:
            quartz = self._load_quartz()
        except Exception:  # noqa: BLE001
            return
        position = self._press_position(quartz)
        if position is None:
            return
        try:
            self._post_button(quartz, position[0], position[1], button, down=False)
        except Exception:  # noqa: BLE001
            pass

    def _press_position(self, quartz) -> tuple[float, float] | None:
        """松开事件使用的位置：优先用我们认知的光标位置，不知道就回读真实位置。

        绝不能用 (0,0) 兜底：鼠标事件带坐标，投递到 (0,0) 会把光标拽到屏幕左上角。
        """
        if self._cursor_x is not None and self._cursor_y is not None:
            return self._cursor_x, self._cursor_y
        try:
            point = quartz.CGEventGetLocation(quartz.CGEventCreate(None))
            return float(point.x), float(point.y)
        except Exception:  # noqa: BLE001
            return None

    def _release_stale_press(self, quartz, *, limit: float = 90.0) -> None:
        """按住超过 ``limit`` 秒且没有任何后续动作时自动松开，避免把鼠标键留在按下状态。"""
        if self._pressed_button is None or time.monotonic() - self._pressed_at <= limit:
            return
        button, self._pressed_button = self._pressed_button, None
        position = self._press_position(quartz)
        if position is None:
            return
        self._post_button(quartz, position[0], position[1], button, down=False)

    def _status_without_probe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "control_enabled": self._control_enabled,
            "available": bool(self._last_jpeg),
            "screen_width": self._screen_width,
            "screen_height": self._screen_height,
            "error": self._last_error,
            "platform": sys.platform,
            "pressed": self._pressed_button or "",
        }

    def _capture(self) -> bytes:
        with self._lock:
            if self._last_jpeg and time.monotonic() - self._last_capture_at < 1 / self.max_fps:
                return self._last_jpeg
        if sys.platform != "darwin":
            raise DesktopUnavailable(
                f"本机桌面截图控制器当前只支持 macOS（检测到 {platform_module.system() or sys.platform}）。"
            )
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise DesktopUnavailable(
                "本机屏幕截图需要 Pillow。请安装：./.venv/bin/pip install pillow"
            ) from exc
        image = None
        rgb = None
        try:
            # ImageGrab 在 macOS 上读取当前用户的主屏幕；截图只在内存中编码。
            image = ImageGrab.grab()
            if not image or not getattr(image, "size", None):
                raise DesktopUnavailable("ImageGrab 没有返回本机屏幕图像。")
            width, height = (int(image.size[0]), int(image.size[1]))
            rgb = image.convert("RGB")
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=self.jpeg_quality, optimize=True)
            data = output.getvalue()
        except DesktopError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DesktopUnavailable(
                "无法读取本机屏幕。请在 macOS「系统设置 → 隐私与安全性 → 屏幕录制」中允许 "
                "运行 Carme 的终端或 Python，并重新启动服务；原始错误："
                f"{str(exc)[:180]}"
            ) from exc
        finally:
            if rgb is not None:
                try:
                    rgb.close()
                except Exception:
                    pass
            if image is not None:
                try:
                    image.close()
                except Exception:
                    pass
        logical_width = logical_height = 0
        try:
            import Quartz
            _bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            logical_width = int(_bounds.size.width)
            logical_height = int(_bounds.size.height)
        except Exception:
            pass
        with self._lock:
            self._screen_width = logical_width or width
            self._screen_height = logical_height or height
            self._last_jpeg = data
            self._last_capture_at = time.monotonic()
            self._last_error = ""
            return data

    def _screen_dimensions(self) -> tuple[int, int]:
        if self._screen_width <= 0 or self._screen_height <= 0:
            self._capture()
        if self._screen_width <= 0 or self._screen_height <= 0:
            raise DesktopUnavailable("尚未取得本机主屏幕尺寸；请先确认屏幕录制权限。")
        return self._screen_width, self._screen_height

    @staticmethod
    def _cursor_bounds(quartz, width: int, height: int) -> tuple[float, float, float, float]:
        """相对移动允许停留的坐标范围：所有活动显示器的并集，取不到时退回主屏。"""
        try:
            _error, displays, count = quartz.CGGetActiveDisplayList(16, None, None)
            left = top = float("inf")
            right = bottom = float("-inf")
            for index in range(int(count)):
                bounds = quartz.CGDisplayBounds(displays[index])
                left = min(left, float(bounds.origin.x))
                top = min(top, float(bounds.origin.y))
                right = max(right, float(bounds.origin.x + bounds.size.width))
                bottom = max(bottom, float(bounds.origin.y + bounds.size.height))
            if right > left and bottom > top:
                return left, top, right, bottom
        except Exception:  # noqa: BLE001
            pass
        return 0.0, 0.0, float(width), float(height)

    @staticmethod
    def _validate_point(x: float | None, y: float | None, width: int, height: int) -> None:
        if x is None or y is None:
            raise DesktopInputError("move 和 click 必须提供 x、y 坐标。")
        try:
            x_value, y_value = float(x), float(y)
        except (TypeError, ValueError) as exc:
            raise DesktopInputError("x、y 必须是数字。") from exc
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise DesktopInputError("x、y 必须是有限数字。")
        if not (0 <= x_value < width and 0 <= y_value < height):
            raise DesktopInputError(
                f"坐标超出当前主屏幕范围：x 需在 0–{width - 1}，y 需在 0–{height - 1}。"
            )

    @staticmethod
    def _load_quartz():
        try:
            import Quartz  # type: ignore
        except ImportError as exc:
            raise DesktopError(
                "鼠标控制需要 macOS Quartz/CoreGraphics。请安装可选依赖："
                "./.venv/bin/pip install -e '.[desktop]'；然后在 macOS「系统设置 → "
                "隐私与安全性 → 辅助功能」允许运行 Carme 的终端或 Python。"
            ) from exc
        return Quartz

    @staticmethod
    def _post_mouse(quartz, x: float | None, y: float | None, kind: str) -> None:
        point = (float(x), float(y))
        event_type = getattr(quartz, "kCGEventMouseMoved", None)
        button = getattr(quartz, "kCGMouseButtonLeft", 0)
        if kind == "move":
            event_type = getattr(quartz, "kCGEventMouseMoved", event_type)
        event = quartz.CGEventCreateMouseEvent(None, event_type, point, button)
        if event is None:
            raise DesktopError("macOS 没有创建鼠标事件；请检查辅助功能权限。")
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    @staticmethod
    def _post_button(quartz, x: float, y: float, button: str, *, down: bool) -> None:
        """按下或松开一个鼠标键（长按拖动用）；位置取当前光标位置。"""
        names = {
            "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
            "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
            "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
        }
        if button not in names:
            raise DesktopInputError("button 只能是 left、right 或 middle。")
        down_name, up_name, button_name = names[button]
        event_type = getattr(quartz, down_name if down else up_name)
        mouse_button = getattr(quartz, button_name)
        event = quartz.CGEventCreateMouseEvent(None, event_type, (float(x), float(y)), mouse_button)
        if event is None:
            raise DesktopError("macOS 没有创建鼠标事件；请检查辅助功能权限。")
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    @staticmethod
    def _post_clicks(quartz, x: float | None, y: float | None, button: str, clicks: int) -> None:
        point = (float(x), float(y))
        names = {
            "left": ("kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGMouseButtonLeft"),
            "right": ("kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGMouseButtonRight"),
            "middle": ("kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGMouseButtonCenter"),
        }
        down_name, up_name, button_name = names[button]
        down = getattr(quartz, down_name)
        up = getattr(quartz, up_name)
        mouse_button = getattr(quartz, button_name)
        for _ in range(clicks):
            for event_type in (down, up):
                event = quartz.CGEventCreateMouseEvent(None, event_type, point, mouse_button)
                if event is None:
                    raise DesktopError("macOS 没有创建鼠标事件；请检查辅助功能权限。")
                quartz.CGEventPost(quartz.kCGHIDEventTap, event)


__all__ = [
    "DesktopController",
    "DesktopError",
    "DesktopUnavailable",
    "DesktopControlDisabled",
    "DesktopInputError",
]
