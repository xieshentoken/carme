"""DesktopController 鼠标语义隔离测试：只用假 Quartz，不碰真实屏幕。

本测试通过 ``sys.modules["Quartz"]`` 注入一个自写的假 Quartz 模块，只验证
``carme/desktop.py`` 中 ``DesktopController`` 的鼠标动作语义：

- 不调用真实抓屏（直接写死 ``_screen_width`` / ``_screen_height``）；
- 不移动真实光标、不投递真实 CGEvent（全部由假 Quartz 记录）；
- 不访问网络，也不读取或修改 ``.env``、配置文件或任何数据文件。

运行方式::

    cd app && ./.venv/bin/python scripts/test_desktop_control.py
"""
from __future__ import annotations

import math
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from carme.desktop import DesktopControlDisabled, DesktopController, DesktopInputError


class FakeQuartz:
    """假 Quartz 模块：用可变内部状态模拟 macOS 光标与事件投递。

    ``CGEventPost`` 只把事件记进 ``posted`` / ``mouse_events``；对鼠标事件还会
    把假光标位置更新为该事件的 point，模拟 macOS 真的把光标移了过去。
    ``defer_mouse_update`` 打开后，投递只进 ``pending_mouse``，假光标保持原位，
    由测试调用 ``apply_pending_mouse()`` 决定事件何时生效（模拟滞后回读）。
    """

    kCGEventMouseMoved = 5
    kCGEventLeftMouseDown = 1
    kCGEventLeftMouseUp = 2
    kCGEventRightMouseDown = 3
    kCGEventRightMouseUp = 4
    kCGEventOtherMouseDown = 25
    kCGEventOtherMouseUp = 26
    kCGMouseButtonLeft = 0
    kCGMouseButtonRight = 1
    kCGMouseButtonCenter = 2
    kCGHIDEventTap = 0
    kCGScrollEventUnitPixel = 0

    _MOUSE_EVENT_TYPES = frozenset({
        kCGEventMouseMoved,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseUp,
    })

    def __init__(self, cursor_x: float = 0.0, cursor_y: float = 0.0) -> None:
        self.cursor_x = float(cursor_x)
        self.cursor_y = float(cursor_y)
        self.posted: list[object] = []
        self.mouse_events: list[tuple[int, tuple[float, float], int]] = []
        # True 时投递鼠标事件不立刻移动假光标，模拟 macOS 尚未处理该事件；
        # 测试用 apply_pending_mouse() 决定事件何时生效。
        self.defer_mouse_update = False
        self.pending_mouse: list[tuple[int, tuple[float, float], int]] = []

    # ---- 光标 ----
    def CGEventCreate(self, *_args: object) -> object:
        return object()

    def CGEventGetLocation(self, _event: object):
        return types.SimpleNamespace(x=self.cursor_x, y=self.cursor_y)

    # ---- 鼠标事件 ----
    def CGEventCreateMouseEvent(self, _source: object, event_type: int, point, button: int):
        x, y = float(point[0]), float(point[1])
        return types.SimpleNamespace(event_type=event_type, point=(x, y), button=button)

    def CGEventPost(self, _tap: int, event: object) -> None:
        self.posted.append(event)
        event_type = getattr(event, "event_type", None)
        if event_type in self._MOUSE_EVENT_TYPES:
            self.mouse_events.append((event_type, event.point, event.button))
            if self.defer_mouse_update:
                # 事件已投递但系统还没处理：假光标停在原位，等测试放行。
                self.pending_mouse.append((event_type, event.point, event.button))
            else:
                self.cursor_x, self.cursor_y = event.point

    def apply_pending_mouse(self) -> None:
        """让所有已投递但尚未生效的鼠标事件依次落到假光标上。"""
        for _event_type, point, _button in self.pending_mouse:
            self.cursor_x, self.cursor_y = point
        self.pending_mouse.clear()

    # ---- 显示器 ----
    def CGGetActiveDisplayList(self, _max_displays: int, _array: object, _count: object):
        # 主屏 id=1，第二块屏 id=2（位于主屏左侧）。
        return (0, [1, 2], 2)

    def CGDisplayBounds(self, display_id: int):
        if int(display_id) == 2:
            return types.SimpleNamespace(
                origin=types.SimpleNamespace(x=-1920.0, y=0.0),
                size=types.SimpleNamespace(width=1920.0, height=1080.0),
            )
        return types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0.0, y=0.0),
            size=types.SimpleNamespace(width=1440.0, height=900.0),
        )


class MouseSemanticsTests(unittest.TestCase):
    """只覆盖 DesktopController 的鼠标语义（move/move_rel/click）与光标认知校正。"""

    def setUp(self) -> None:
        self.quartz = FakeQuartz()
        self._previous_quartz = sys.modules.get("Quartz")
        sys.modules["Quartz"] = self.quartz
        self.controller = DesktopController({"enabled": True, "control_enabled": True})
        # 直接写死屏幕尺寸，_screen_dimensions() 不会去截图。
        self.controller._screen_width = 1440
        self.controller._screen_height = 900
        self.controller.set_control(True)

    def tearDown(self) -> None:
        self.controller.close()
        if self._previous_quartz is None:
            sys.modules.pop("Quartz", None)
        else:
            sys.modules["Quartz"] = self._previous_quartz

    # ---- 小工具 ----
    def event_types(self) -> list[int]:
        return [event_type for event_type, _point, _button in self.quartz.mouse_events]

    def event_points(self) -> list[tuple[float, float]]:
        return [point for _event_type, point, _button in self.quartz.mouse_events]

    # ---- 用例 ----
    def test_move_rel_accumulates_from_real_cursor(self) -> None:
        """move_rel 以电脑真实光标为基准累加 dx/dy。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        result = self.controller.mouse(action="move_rel", dx=30, dy=-40)
        self.assertEqual(self.event_types(), [FakeQuartz.kCGEventMouseMoved])
        self.assertEqual(self.event_points(), [(130.0, 160.0)])
        self.assertEqual(self.quartz.mouse_events[0][2], FakeQuartz.kCGMouseButtonLeft)
        self.assertIs(result["ok"], True)
        self.assertEqual(result["action"], "move_rel")
        self.assertEqual((result["x"], result["y"]), (130.0, 160.0))

    def test_move_rel_keeps_cursor_on_secondary_screen(self) -> None:
        """光标在第二块屏幕（x<0）时相对移动不被拉回主屏 0。"""
        self.quartz.cursor_x, self.quartz.cursor_y = -100.0, 300.0
        result = self.controller.mouse(action="move_rel", dx=-50, dy=0)
        self.assertEqual(self.event_points(), [(-150.0, 300.0)])
        self.assertNotEqual(self.event_points()[0], (0.0, 300.0))
        self.assertEqual((result["x"], result["y"]), (-150.0, 300.0))

    def test_move_rel_rejects_oversized_or_non_finite_offsets(self) -> None:
        """单次 |dx|/|dy| > 1200、None 或非有限数都抛 DesktopInputError。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        for dx, dy in (
            (1201, 0),
            (0, -1201),
            (float("nan"), 0),
            (0, float("inf")),
            (None, 0),
            (0, None),
        ):
            with self.subTest(dx=dx, dy=dy):
                with self.assertRaises(DesktopInputError):
                    self.controller.mouse(action="move_rel", dx=dx, dy=dy)
        self.assertEqual(self.quartz.mouse_events, [])
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (100.0, 200.0))

    def test_click_without_coordinates_uses_real_cursor(self) -> None:
        """不带 x/y 的 click 在电脑当前真实光标位置按下并抬起左键。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        result = self.controller.mouse(action="click")
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        self.assertEqual(self.event_points(), [(640.0, 400.0), (640.0, 400.0)])
        self.assertEqual(
            [button for _type, _point, button in self.quartz.mouse_events],
            [FakeQuartz.kCGMouseButtonLeft, FakeQuartz.kCGMouseButtonLeft],
        )
        self.assertEqual((result["x"], result["y"]), (640.0, 400.0))
        self.assertEqual(result["button"], "left")
        self.assertEqual(result["clicks"], 1)

    def test_click_rejects_unknown_button(self) -> None:
        """非法 button（如 side）抛 DesktopInputError，且不投递任何事件。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        with self.assertRaises(DesktopInputError):
            self.controller.mouse(action="click", button="side")
        self.assertEqual(self.quartz.mouse_events, [])
        self.assertEqual(self.quartz.posted, [])

    def test_absolute_move_stays_inside_main_screen(self) -> None:
        """绝对 move 落在主屏 0..1439 / 0..899；越界抛 DesktopInputError。"""
        result = self.controller.mouse(action="move", x=10, y=20)
        self.assertEqual(self.event_points(), [(10.0, 20.0)])
        self.assertEqual((result["x"], result["y"]), (10.0, 20.0))
        with self.assertRaises(DesktopInputError):
            self.controller.mouse(action="move", x=2000, y=10)
        with self.assertRaises(DesktopInputError):
            self.controller.mouse(action="move", x=10, y=900)
        self.assertEqual(self.event_points(), [(10.0, 20.0)])

    def test_mouse_requires_runtime_control_switch(self) -> None:
        """control_enabled 只表示允许；未 set_control(True) 时 mouse 抛 DesktopControlDisabled。"""
        controller = DesktopController({"enabled": True, "control_enabled": True})
        controller._screen_width = 1440
        controller._screen_height = 900
        with self.assertRaises(DesktopControlDisabled):
            controller.mouse(action="move_rel", dx=1, dy=1)
        self.assertEqual(self.quartz.mouse_events, [])
        self.assertEqual(self.quartz.posted, [])


    def test_move_rel_ignores_stale_readback_after_post(self) -> None:
        """投递后立刻回读会滞后一拍：move_rel 以上一步的认知为基准，不丢位移。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        # 让假光标停在原位，模拟 CGEventPost 之后系统还没来得及处理该事件。
        self.quartz.defer_mouse_update = True
        first = self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.assertEqual(self.event_points(), [(130.0, 200.0)])
        self.assertEqual(first["x"], 130.0)
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (100.0, 200.0))

        second = self.controller.mouse(action="move_rel", dx=30, dy=0)
        # 滞后回读 (100,200) 正好等于「认知 130 - 上一步位移 30」，必须识别成滞后，
        # 以认知 130 为基准累加，投递点只能是 160。
        self.assertEqual(self.event_points()[-1], (160.0, 200.0))
        self.assertEqual(second["x"], 160.0)
        # 对照：把滞后回读当基准会得到 100 + 30 = 130 这个错误值。
        stale_based_x = self.quartz.cursor_x + 30
        self.assertEqual(stale_based_x, 130.0)
        self.assertNotEqual(self.event_points()[-1][0], stale_based_x)

        # 事件最终被系统处理时光标应停在我们认知的 160，而不是 130。
        self.quartz.apply_pending_mouse()
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (160.0, 200.0))

    def test_move_rel_survives_several_in_flight_steps(self) -> None:
        """一次快滑会连续下发好几次位移：回读落后多步也必须按认知累加，不能退回陈旧基准。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        # 事件在约 250ms 的稳定窗口内都还没被系统处理，回读一直停在 100。
        self.quartz.defer_mouse_update = True
        points: list[tuple[float, float]] = []
        for _ in range(4):
            result = self.controller.mouse(action="move_rel", dx=30, dy=0)
            points.append((result["x"], result["y"]))
        # 四步各 +30 必须是 130/160/190/220。只比对最后一步时，第 3 步起会退回 100 的陈旧
        # 基准，得到 130/160/130/160——指针在 130↔160 之间来回跳，正是要修的毛病。
        self.assertEqual(
            self.event_points(),
            [(130.0, 200.0), (160.0, 200.0), (190.0, 200.0), (220.0, 200.0)],
        )
        self.assertEqual(
            points,
            [(130.0, 200.0), (160.0, 200.0), (190.0, 200.0), (220.0, 200.0)],
        )
        # 系统最终处理完所有事件时光标停在手指意图的位置，而不是其中某一步。
        self.quartz.apply_pending_mouse()
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (220.0, 200.0))

    def test_move_rel_after_absolute_move_ignores_stale_readback(self) -> None:
        """绝对移动（桌面端路径）之后立刻单指拖动时，滞后回读不能把整段绝对移动抹掉。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        self.controller.mouse(action="move_rel", dx=10, dy=0)
        self.quartz.defer_mouse_update = True
        self.controller.mouse(action="move", x=900.0, y=500.0)
        self.assertEqual((self.controller._cursor_x, self.controller._cursor_y), (900.0, 500.0))
        # 回读仍是绝对移动之前的位置：必须识别成滞后，以认知 (900,500) 为基准累加。
        result = self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.assertEqual(self.event_points()[-1], (930.0, 500.0))
        self.assertEqual((result["x"], result["y"]), (930.0, 500.0))

    def test_move_rel_follows_cursor_moved_by_user(self) -> None:
        """用户自己搬走光标（与上一步位移不同）时，move_rel 尊重真实回读位置。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.quartz.cursor_x, self.quartz.cursor_y = 500.0, 500.0
        result = self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.assertEqual(self.event_points()[-1], (530.0, 500.0))
        self.assertEqual((result["x"], result["y"]), (530.0, 500.0))

    def test_status_resyncs_cursor_cognition(self) -> None:
        """status() 越过稳定时间门限后用稳定回读校正认知，后续 move_rel 从新位置累加。"""
        # status() 会调用 _capture()；换成桩，避免测试真的去抓屏。
        self.controller._capture = lambda: b"stub-jpeg"
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.assertEqual((self.controller._cursor_x, self.controller._cursor_y), (130.0, 200.0))
        self.quartz.cursor_x, self.quartz.cursor_y = 777.0, 333.0
        # 刚动作完的回读仍可能滞后，先清掉时间戳绕过 status() 的稳定时间门限。
        self.controller._cursor_at = 0.0
        status = self.controller.status()
        self.assertEqual((status["cursor_x"], status["cursor_y"]), (777.0, 333.0))
        self.assertEqual((self.controller._cursor_x, self.controller._cursor_y), (777.0, 333.0))
        self.assertIsNone(self.controller._last_step)
        result = self.controller.mouse(action="move_rel", dx=10, dy=0)
        self.assertEqual(self.event_points()[-1], (787.0, 333.0))
        self.assertEqual((result["x"], result["y"]), (787.0, 333.0))

    def test_set_control_off_then_on_reprobes_real_cursor(self) -> None:
        """关闭再打开控制会丢掉光标认知，重新以真实回读为基准。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.assertEqual((self.controller._cursor_x, self.controller._cursor_y), (130.0, 200.0))
        self.controller.set_control(False)
        self.assertIsNone(self.controller._cursor_x)
        self.quartz.cursor_x, self.quartz.cursor_y = 10.0, 20.0
        self.controller.set_control(True)
        result = self.controller.mouse(action="move_rel", dx=5, dy=0)
        self.assertEqual(self.event_points()[-1], (15.0, 20.0))
        self.assertEqual((result["x"], result["y"]), (15.0, 20.0))

    def test_move_rel_records_clamped_step_at_screen_edge(self) -> None:
        """被屏幕并集边界钳位时，_last_step 记实际生效位移，滞后回读也不会漂移。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 1435.0, 900.0
        # 让回读停在 1435，逼出「认知 - 实际生效位移」这条判断分支。
        self.quartz.defer_mouse_update = True
        clamped = self.controller.mouse(action="move_rel", dx=200, dy=0)
        # 主屏 1440x900：右边界 1439，x 被钳位，实际只生效了 4 像素。
        self.assertEqual(self.event_points()[-1], (1439.0, 900.0))
        self.assertEqual(clamped["x"], 1439.0)
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (1435.0, 900.0))
        result = self.controller.mouse(action="move_rel", dx=-200, dy=0)
        self.assertEqual(self.event_points()[-1], (1239.0, 900.0))
        self.assertEqual(result["x"], 1239.0)
        # 对照：若 applied 记成请求的 200，滞后回读会被当真，投递点会漂到 1235。
        self.assertNotEqual(self.event_points()[-1][0], 1235.0)
        self.quartz.apply_pending_mouse()
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (1239.0, 900.0))

    # ---- press / release（长按拖动） ----

    def test_press_without_coordinates_uses_real_cursor(self) -> None:
        """不带 x/y 的 press 在电脑当前真实光标位置按下左键并记住按住状态。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        result = self.controller.mouse(action="press")
        self.assertEqual(self.event_types(), [FakeQuartz.kCGEventLeftMouseDown])
        self.assertEqual(self.event_points(), [(640.0, 400.0)])
        self.assertEqual(self.quartz.mouse_events[0][2], FakeQuartz.kCGMouseButtonLeft)
        self.assertEqual(self.controller._pressed_button, "left")
        self.assertEqual(result["pressed"], "left")
        self.assertEqual((result["x"], result["y"]), (640.0, 400.0))

    def test_press_with_explicit_coordinates_and_bounds(self) -> None:
        """显式 x/y 的 press/release 走 _validate_point；越界或缺坐标抛 DesktopInputError。"""
        result = self.controller.mouse(action="press", x=100, y=200)
        self.assertEqual(self.event_types(), [FakeQuartz.kCGEventLeftMouseDown])
        self.assertEqual(self.event_points(), [(100.0, 200.0)])
        self.assertEqual(result["pressed"], "left")
        self.assertEqual((result["x"], result["y"]), (100.0, 200.0))
        self.controller.mouse(action="release", x=100, y=200)
        for x, y in ((2000, 10), (10, 900), (10, None)):
            with self.subTest(x=x, y=y):
                with self.assertRaises(DesktopInputError):
                    self.controller.mouse(action="press", x=x, y=y)
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        self.assertIsNone(self.controller._pressed_button)

    def test_press_without_coordinates_uses_settled_cursor_under_lag(self) -> None:
        """move_rel 后立刻 press：滞后回读不能把按下点拽回旧位置，要用认知位置。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        self.quartz.defer_mouse_update = True
        self.controller.mouse(action="move_rel", dx=30, dy=0)
        self.assertEqual((self.quartz.cursor_x, self.quartz.cursor_y), (100.0, 200.0))
        result = self.controller.mouse(action="press")
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventMouseMoved, FakeQuartz.kCGEventLeftMouseDown],
        )
        self.assertEqual(self.event_points(), [(130.0, 200.0), (130.0, 200.0)])
        self.assertEqual((result["x"], result["y"]), (130.0, 200.0))
        self.assertEqual(result["pressed"], "left")

    def test_press_is_idempotent_for_same_button(self) -> None:
        """重复 press 同一个键是幂等的：不再投递事件，按住状态与返回不变。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        first = self.controller.mouse(action="press")
        second = self.controller.mouse(action="press")
        self.assertEqual(self.event_types(), [FakeQuartz.kCGEventLeftMouseDown])
        self.assertEqual(self.event_points(), [(640.0, 400.0)])
        self.assertEqual(first["pressed"], "left")
        self.assertEqual(second["pressed"], "left")
        self.assertEqual(self.controller._pressed_button, "left")

    def test_press_other_button_while_held_raises(self) -> None:
        """按住 left 时再 press right 抛 DesktopInputError（消息含「已经按住了」），零新事件。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        with self.assertRaises(DesktopInputError) as ctx:
            self.controller.mouse(action="press", button="right")
        self.assertIn("已经按住了", str(ctx.exception))
        self.assertEqual(self.event_types(), [FakeQuartz.kCGEventLeftMouseDown])
        self.assertEqual(self.controller._pressed_button, "left")

    def test_press_and_release_map_right_and_middle_buttons(self) -> None:
        """button=right/middle 映射到 Right/Other 的 Down/Up 事件与 Right/Center 键码。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press", button="right")
        self.controller.mouse(action="release", button="right")
        self.controller.mouse(action="press", button="middle")
        result = self.controller.mouse(action="release", button="middle")
        self.assertEqual(
            self.event_types(),
            [
                FakeQuartz.kCGEventRightMouseDown,
                FakeQuartz.kCGEventRightMouseUp,
                FakeQuartz.kCGEventOtherMouseDown,
                FakeQuartz.kCGEventOtherMouseUp,
            ],
        )
        self.assertEqual(self.event_points(), [(640.0, 400.0)] * 4)
        self.assertEqual(
            [button for _type, _point, button in self.quartz.mouse_events],
            [
                FakeQuartz.kCGMouseButtonRight,
                FakeQuartz.kCGMouseButtonRight,
                FakeQuartz.kCGMouseButtonCenter,
                FakeQuartz.kCGMouseButtonCenter,
            ],
        )
        self.assertIsNone(self.controller._pressed_button)
        self.assertEqual(result["pressed"], "")

    def test_press_release_reject_unknown_button_without_posting(self) -> None:
        """非法 button（side）对 press/release 都抛 DesktopInputError，且零投递。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        with self.assertRaises(DesktopInputError):
            self.controller.mouse(action="press", button="side")
        with self.assertRaises(DesktopInputError):
            self.controller.mouse(action="release", button="side")
        self.assertEqual(self.quartz.mouse_events, [])
        self.assertEqual(self.quartz.posted, [])
        self.assertIsNone(self.controller._pressed_button)

    def test_release_without_press_is_noop(self) -> None:
        """没有按住时 release 不投递任何事件、不报错，返回 pressed 为空字符串。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        result = self.controller.mouse(action="release")
        self.assertEqual(self.quartz.mouse_events, [])
        self.assertEqual(self.quartz.posted, [])
        self.assertIs(result["ok"], True)
        self.assertEqual(result["action"], "release")
        self.assertEqual(result["pressed"], "")

    def test_release_posts_up_and_clears_pressed_state(self) -> None:
        """release 投递对应的 Up 事件并清空按住状态，返回 pressed 为空字符串。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        result = self.controller.mouse(action="release")
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        self.assertEqual(self.event_points(), [(640.0, 400.0), (640.0, 400.0)])
        self.assertIsNone(self.controller._pressed_button)
        self.assertEqual(result["pressed"], "")

    def test_move_rel_while_pressed_keeps_button_state(self) -> None:
        """按住状态下 move_rel 只投递移动事件，按键状态不变，返回 pressed 仍是该键。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 100.0, 200.0
        self.controller.mouse(action="press")
        result = self.controller.mouse(action="move_rel", dx=30, dy=-10)
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventMouseMoved],
        )
        self.assertEqual(self.event_points(), [(100.0, 200.0), (130.0, 190.0)])
        self.assertEqual(self.controller._pressed_button, "left")
        self.assertEqual(result["pressed"], "left")
        self.assertEqual((result["x"], result["y"]), (130.0, 190.0))

    def test_click_while_pressed_releases_before_clicking(self) -> None:
        """按住状态下 click 先松开（Up 在按住点），再在点击坐标投递 Down/Up 一对。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        result = self.controller.mouse(action="click", x=100, y=200)
        self.assertEqual(
            self.event_types(),
            [
                FakeQuartz.kCGEventLeftMouseDown,
                FakeQuartz.kCGEventLeftMouseUp,
                FakeQuartz.kCGEventLeftMouseDown,
                FakeQuartz.kCGEventLeftMouseUp,
            ],
        )
        self.assertEqual(
            self.event_points(),
            [(640.0, 400.0), (640.0, 400.0), (100.0, 200.0), (100.0, 200.0)],
        )
        self.assertIsNone(self.controller._pressed_button)
        # 松开发生在结果组装之前，click 的返回里没有 pressed 键（按空处理）。
        self.assertEqual(result.get("pressed", ""), "")
        self.assertEqual(result["button"], "left")
        self.assertEqual(result["clicks"], 1)

    def test_stale_press_released_by_safety_valve_via_mouse(self) -> None:
        """按住超过安全阀时限后，下一次 mouse() 调用自动投递 Up 并清空状态。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        # 模拟客户端此后不再发动作：把按住时间戳改到 100 秒前。
        self.controller._pressed_at = time.monotonic() - 100.0
        result = self.controller.mouse(action="release")
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        self.assertEqual(self.event_points(), [(640.0, 400.0), (640.0, 400.0)])
        self.assertIsNone(self.controller._pressed_button)
        self.assertEqual(result["pressed"], "")

    def test_stale_press_safety_valve_keeps_fresh_press(self) -> None:
        """刚按住的 press 不被安全阀松开；直接 _release_stale_press(limit=0) 才松开。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        self.controller._release_stale_press(self.controller._load_quartz())
        self.assertEqual(self.event_types(), [FakeQuartz.kCGEventLeftMouseDown])
        self.assertEqual(self.controller._pressed_button, "left")
        self.controller._release_stale_press(self.controller._load_quartz(), limit=0)
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        self.assertEqual(self.event_points()[-1], (640.0, 400.0))
        self.assertIsNone(self.controller._pressed_button)

    def test_set_control_off_releases_pressed_button(self) -> None:
        """按住状态下 set_control(False) 必须投递 Up 并清空状态。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        self.controller.set_control(False)
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        # 松开点必须是按住的真实位置：拿不到位置就退回 (0,0) 会把光标拽到屏幕左上角。
        self.assertEqual(self.event_points()[-1], (640.0, 400.0))
        self.assertIsNone(self.controller._pressed_button)

    def test_close_releases_pressed_button(self) -> None:
        """按住状态下 close() 必须投递 Up（在按住点松开）并清空状态。"""
        self.quartz.cursor_x, self.quartz.cursor_y = 640.0, 400.0
        self.controller.mouse(action="press")
        self.controller.close()
        self.assertEqual(
            self.event_types(),
            [FakeQuartz.kCGEventLeftMouseDown, FakeQuartz.kCGEventLeftMouseUp],
        )
        self.assertEqual(self.event_points()[-1], (640.0, 400.0))
        self.assertIsNone(self.controller._pressed_button)

    def test_mouse_action_whitelist_includes_press_and_release(self) -> None:
        """action 白名单包含 press/release；非法 action（fly）抛 DesktopInputError 且零投递。"""
        with self.assertRaises(DesktopInputError) as ctx:
            self.controller.mouse(action="fly")
        self.assertIn("press", str(ctx.exception))
        self.assertIn("release", str(ctx.exception))
        self.assertEqual(self.quartz.mouse_events, [])
        self.assertEqual(self.quartz.posted, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
