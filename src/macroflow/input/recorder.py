from __future__ import annotations

import threading
import time
from typing import Callable

from pynput import keyboard, mouse

from macroflow.input.rawinput import RawMouseListener
from macroflow.input.wininput import is_cursor_near_window_center, is_window_process_foreground


def _button_name(button: mouse.Button) -> str:
    text = str(button).split(".")[-1]
    return text if text in {"left", "right", "middle"} else "left"


def _key_data(key) -> tuple[int, str]:
    vk = getattr(key, "vk", None)
    if vk is None and hasattr(key, "value"):
        vk = getattr(key.value, "vk", None)
    name = getattr(key, "char", None) or str(key).replace("Key.", "")
    return int(vk or 0), str(name)


class MacroRecorder:
    def __init__(self, on_action: Callable[[dict], None] | None = None):
        self.on_action = on_action
        self.actions: list[dict] = []
        self.running = False
        self.mode = "absolute"
        self.target_hwnd: int | None = None
        self.target_relative_enabled = True
        self.relative_requires_center_lock = False
        self.interval_ms = 100
        self._keyboard_listener = None
        self._mouse_listener = None
        self._raw_listener: RawMouseListener | None = None
        self._lock = threading.RLock()
        self._last_action_time = 0.0
        self._last_move_time = 0.0
        self._raw_dx = 0
        self._raw_dy = 0
        self._raw_last_flush = 0.0
        # 快捷键脚本回放（注入）的相对位移：按 50 ms 脉冲窗口合并成一条
        # 「转向」动作，与快捷键脚本内容一致（而不是录成普通鼠标移动）。
        # _injected_started 记录当前注入脉冲的起始时刻，落成动作时用它计算
        # delay_ms（而不是用刷出时刻），保证按快捷键的时刻与真实按下时间一致。
        self._injected_dx = 0
        self._injected_dy = 0
        self._injected_last = 0.0
        self._injected_started = 0.0
        self._injected_flush_window = 0.05
        self._center_lock_samples = 0
        self._center_lock_active = False
        self._filter_vks: set[int] = set()
        self.max_actions = 200_000
        self.limit_reached = False
        self._event_times: list[float] = []

    def start(self, mode: str = "absolute", interval_ms: int = 100,
              target_hwnd: int | None = None, target_relative_enabled: bool = True,
              relative_requires_center_lock: bool = False,
              filter_vks: set[int] | None = None) -> None:
        if self.running:
            return
        self.mode = mode
        self.target_hwnd = target_hwnd
        self.target_relative_enabled = bool(target_relative_enabled)
        self.relative_requires_center_lock = bool(relative_requires_center_lock)
        self.interval_ms = max(10, min(500, int(interval_ms)))
        # 已绑定快捷键的按键（虚键码）不录进脚本：快捷键只是触发动作的
        # 开关，脚本回放的注入输入才是要记录的内容。
        self._filter_vks = set(int(vk) for vk in (filter_vks or ()) if int(vk) > 0)
        self.actions = []
        self._event_times = []
        self.limit_reached = False
        now = time.perf_counter()
        self._last_action_time = now
        self._last_move_time = 0.0
        self._raw_last_flush = now
        self._center_lock_samples = 0
        self._center_lock_active = False
        self._injected_dx = self._injected_dy = 0
        self._injected_last = 0.0
        self._injected_started = 0.0
        self.running = True
        try:
            self._keyboard_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self._keyboard_listener.start()
            self._mouse_listener = mouse.Listener(
                on_move=self._on_move if mode in {"absolute", "auto"} else None,
                on_click=self._on_click,
                on_scroll=self._on_scroll,
            )
            self._mouse_listener.start()
            # 所有模式都监听原始位移：物理移动只在相对/自动模式消费；注入的
            # 快捷键脚本回放位移在任何模式下都录成「转向」动作（与快捷键
            # 脚本一致，游戏中可生效）。
            self._raw_listener = RawMouseListener(self._on_raw_move)
            self._raw_listener.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> list[dict]:
        was_running = self.running
        if was_running and self.mode in {"relative", "auto"}:
            self._flush_raw(force=True)
        self._flush_injected(force=True)
        self.running = False
        for listener in (self._keyboard_listener, self._mouse_listener):
            if listener:
                listener.stop()
        if self._raw_listener:
            self._raw_listener.stop()
        self._keyboard_listener = self._mouse_listener = self._raw_listener = None
        self.target_hwnd = None
        self.target_relative_enabled = True
        self.relative_requires_center_lock = False
        self._filter_vks = set()
        self._center_lock_samples = 0
        self._center_lock_active = False
        return list(self.actions)

    def current_mode(self) -> str:
        """Resolve the active mouse capture mode for this recording."""
        if self.mode != "auto":
            return self.mode
        target_active = self.target_relative_enabled and is_window_process_foreground(self.target_hwnd)
        if target_active and (not self.relative_requires_center_lock or self._center_lock_active):
            return "relative"
        return "absolute"

    def _append(self, action: dict, when: float | None = None) -> None:
        if not self.running:
            return
        with self._lock:
            if len(self.actions) >= self.max_actions:
                self.limit_reached = True
                return
            # when 用于注入的「转向」动作：delay_ms 从快捷键实际按下时刻
            # （注入脉冲起始）起算，而不是从刷出时刻起算，否则转向的延时
            # 会被记到下一次快捷键/停止的时间点，丢失真实按下时机。
            now = time.perf_counter() if when is None else when
            action["delay_ms"] = max(0, round((now - self._last_action_time) * 1000))
            self._last_action_time = now
            self.actions.append(action)
            self._event_times.append(now)
        if self.on_action:
            self.on_action(action)

    def discard_recent(self, milliseconds: int = 500) -> int:
        """Remove UI clicks made immediately before a mini-window stop command."""
        cutoff = time.perf_counter() - max(0, milliseconds) / 1000
        removed = 0
        with self._lock:
            while self._event_times and self._event_times[-1] >= cutoff:
                self._event_times.pop()
                self.actions.pop()
                removed += 1
            if self._event_times:
                self._last_action_time = self._event_times[-1]
        return removed

    def _on_press(self, key, injected=False) -> None:
        # 先把尚未刷出的注入转向落成动作，再记录本次按键：保证「转向」排在
        # 它之后发生的实体输入之前，且延时从快捷键按下时刻起算。
        self._flush_injected()
        vk, name = _key_data(key)
        if vk in self._filter_vks:
            return
        if name.lower() in {"f8", "f9", "f12"}:
            return
        self._append({"type": "key", "vk": vk, "name": name, "down": True})

    def _on_release(self, key, injected=False) -> None:
        self._flush_injected()
        vk, name = _key_data(key)
        if vk in self._filter_vks:
            return
        if name.lower() in {"f8", "f9", "f12"}:
            return
        self._append({"type": "key", "vk": vk, "name": name, "down": False})

    def _on_move(self, x: int, y: int, injected: bool = False) -> None:
        if injected:
            # 快捷键脚本回放产生的绝对移动不录制：录进去只是一条在游戏中
            # 无效的坐标移动（点击等动作自带坐标，不需要依赖移动动作）。
            return
        self._flush_injected()
        if self.current_mode() != "absolute":
            return
        now = time.perf_counter()
        if (now - self._last_move_time) * 1000 < self.interval_ms:
            return
        self._last_move_time = now
        self._append({"type": "mouse_move", "mode": "absolute", "x": int(x), "y": int(y)})

    def _on_raw_move(self, dx: int, dy: int, injected: bool = False) -> None:
        if not self.running:
            return
        if injected:
            # 快捷键脚本回放注入的相对位移：录成与快捷键脚本一致的「转向」
            # 动作（ΔX/ΔY），回放时走转向的前置/居中逻辑，游戏中即可生效。
            self._accumulate_injected(dx, dy)
            return
        # 实体鼠标位移到来说明注入脉冲已结束：先刷出未落的转向，避免转向
        # 被排到后续实体移动/点击之后，丢失真实按下时机。
        self._flush_injected()
        if self.mode == "auto" and self.relative_requires_center_lock:
            target_active = self.target_relative_enabled and is_window_process_foreground(self.target_hwnd)
            centered = target_active and is_cursor_near_window_center(self.target_hwnd)
            self._center_lock_samples = self._center_lock_samples + 1 if centered else 0
            self._center_lock_active = self._center_lock_samples >= 3
        if self.current_mode() != "relative":
            with self._lock:
                self._raw_dx = self._raw_dy = 0
            return
        with self._lock:
            self._raw_dx += dx
            self._raw_dy += dy
        self._flush_raw()

    def _accumulate_injected(self, dx: int, dy: int) -> None:
        """合并同一注入脉冲窗口内的相对位移，落成一条「转向」动作。

        记录脉冲起始时刻（_injected_started）：刷出时用它计算 delay_ms，
        保证转向的延时从快捷键实际按下时刻起算，而不是从刷出时刻起算。
        """
        with self._lock:
            now = time.perf_counter()
            if self._injected_last and now - self._injected_last > self._injected_flush_window:
                flush_dx, flush_dy = self._injected_dx, self._injected_dy
                flush_started = self._injected_started
                self._injected_dx = self._injected_dy = 0
                self._injected_started = 0.0
            else:
                flush_dx = flush_dy = 0
                flush_started = 0.0
            if not self._injected_started:
                self._injected_started = now
            self._injected_dx += dx
            self._injected_dy += dy
            self._injected_last = now
        if flush_dx or flush_dy:
            self._append({"type": "turn", "dx": flush_dx, "dy": flush_dy}, when=flush_started)

    def _flush_injected(self, force: bool = False) -> None:
        with self._lock:
            dx, dy = self._injected_dx, self._injected_dy
            started = self._injected_started
            self._injected_dx = self._injected_dy = 0
            self._injected_started = 0.0
            self._injected_last = 0.0
        if dx or dy:
            self._append({"type": "turn", "dx": dx, "dy": dy}, when=started)

    def _flush_raw(self, force: bool = False) -> None:
        now = time.perf_counter()
        # Camera movement is consumed frame-by-frame by many games. Keeping raw
        # samples near 60 Hz avoids one large 100 ms packet being clamped or
        # discarded while preserving the user-selected desktop sampling rate.
        raw_interval_ms = min(self.interval_ms, 16)
        if not force and (now - self._raw_last_flush) * 1000 < raw_interval_ms:
            return
        with self._lock:
            dx, dy = self._raw_dx, self._raw_dy
            self._raw_dx = self._raw_dy = 0
            self._raw_last_flush = now
        if dx or dy:
            self._append({"type": "mouse_move", "mode": "relative", "dx": dx, "dy": dy})

    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        # 注入的点击（快捷键脚本回放）也先刷出未落的转向，保持动作顺序。
        self._flush_injected()
        self._append({
            "type": "mouse_button", "button": _button_name(button),
            "down": bool(pressed), "x": int(x), "y": int(y),
        })

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._flush_injected()
        self._append({"type": "scroll", "dx": int(dx), "dy": int(dy), "x": int(x), "y": int(y)})
