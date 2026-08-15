from __future__ import annotations

import threading
import time
from typing import Callable

from pynput import keyboard, mouse

from rawinput import RawMouseListener
from wininput import is_cursor_near_window_center, is_window_process_foreground


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
        self._center_lock_samples = 0
        self._center_lock_active = False
        self.max_actions = 200_000
        self.limit_reached = False
        self._event_times: list[float] = []

    def start(self, mode: str = "absolute", interval_ms: int = 100,
              target_hwnd: int | None = None, target_relative_enabled: bool = True,
              relative_requires_center_lock: bool = False) -> None:
        if self.running:
            return
        self.mode = mode
        self.target_hwnd = target_hwnd
        self.target_relative_enabled = bool(target_relative_enabled)
        self.relative_requires_center_lock = bool(relative_requires_center_lock)
        self.interval_ms = max(10, min(500, int(interval_ms)))
        self.actions = []
        self._event_times = []
        self.limit_reached = False
        now = time.perf_counter()
        self._last_action_time = now
        self._last_move_time = 0.0
        self._raw_last_flush = now
        self._center_lock_samples = 0
        self._center_lock_active = False
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
            if mode in {"relative", "auto"}:
                self._raw_listener = RawMouseListener(self._on_raw_move)
                self._raw_listener.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> list[dict]:
        was_running = self.running
        if was_running and self.mode in {"relative", "auto"}:
            self._flush_raw(force=True)
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

    def _append(self, action: dict) -> None:
        if not self.running:
            return
        with self._lock:
            if len(self.actions) >= self.max_actions:
                self.limit_reached = True
                return
            now = time.perf_counter()
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

    def _on_press(self, key) -> None:
        vk, name = _key_data(key)
        if name.lower() in {"f8", "f9", "f12"}:
            return
        self._append({"type": "key", "vk": vk, "name": name, "down": True})

    def _on_release(self, key) -> None:
        vk, name = _key_data(key)
        if name.lower() in {"f8", "f9", "f12"}:
            return
        self._append({"type": "key", "vk": vk, "name": name, "down": False})

    def _on_move(self, x: int, y: int) -> None:
        if self.current_mode() != "absolute":
            return
        now = time.perf_counter()
        if (now - self._last_move_time) * 1000 < self.interval_ms:
            return
        self._last_move_time = now
        self._append({"type": "mouse_move", "mode": "absolute", "x": int(x), "y": int(y)})

    def _on_raw_move(self, dx: int, dy: int) -> None:
        if not self.running:
            return
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
        self._append({
            "type": "mouse_button", "button": _button_name(button),
            "down": bool(pressed), "x": int(x), "y": int(y),
        })

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._append({"type": "scroll", "dx": int(dx), "dy": int(dy), "x": int(x), "y": int(y)})
