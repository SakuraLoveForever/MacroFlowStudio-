from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import mss

from detect_overlay import show_overlay
from image_match import find_template
from ocr import (
    extract_ocr_integer, find_expected_match, format_ocr_observation, matches_expected, recognize_region,
    recognize_region_with_boxes,
)
from models import (
    ACTION_ID_KEY, END_CURRENT_SCRIPT_LABEL, NEXT_WORKFLOW_STEP_TARGET_ID,
    SCRIPT_START_TARGET_ID,
)
from storage import load_script, registered_module_object, registered_template_region, resolve_path
from wininput import (
    activate_window, enum_windows, get_cursor_pos, get_virtual_screen_rect, get_window_rect, is_window,
    is_window_process_foreground, send_button, send_key, send_move_absolute, send_move_relative, send_scroll,
    send_text,
)


class PlaybackStopped(Exception):
    """播放被停止（F12/全局模块中断等）。

    referenced_actions / referenced_source_screen 只在停止瞬间正处于
    被引用脚本（script_ref 嵌套）内部时设置，供"结束当前脚本"跳到
    最内层脚本最后一行使用。
    """

    def __init__(self):
        super().__init__()
        self.referenced_actions: list[dict] | None = None
        self.referenced_source_screen: dict | None = None


class JumpToCurrentScriptLastAction(Exception):
    """Leave a nested module segment and continue at the outer script's last action."""

    def __init__(self):
        super().__init__()
        self.current_index: int | None = None


JUMP_CURRENT_SCRIPT_LAST_RESULT = "jump_current_script_last"


class AdvanceToNextWorkflowStep(Exception):
    """Finish the current top-level script and let its workflow advance."""

    pass


class EndCurrentScriptRequest(Exception):
    """Leave nested code segments and stop at the nearest script boundary."""

    pass


MAX_SCRIPT_REF_DEPTH = 16


def scale_screen_point(x: int, y: int, source: dict | None,
                       target: dict | None) -> tuple[int, int]:
    """Scale an absolute desktop point from its recorded screen to this PC."""
    if not source or not target:
        return int(x), int(y)
    try:
        source_width, source_height = int(source.get("width", 0)), int(source.get("height", 0))
        target_width, target_height = int(target.get("width", 0)), int(target.get("height", 0))
    except (TypeError, ValueError):
        return int(x), int(y)
    if min(source_width, source_height, target_width, target_height) <= 0:
        return int(x), int(y)
    source_left, source_top = int(source.get("left", 0)), int(source.get("top", 0))
    target_left, target_top = int(target.get("left", 0)), int(target.get("top", 0))
    scaled_x = target_left + round((int(x) - source_left) * target_width / source_width)
    scaled_y = target_top + round((int(y) - source_top) * target_height / source_height)
    return scaled_x, scaled_y


TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
    ]


def running_process_names() -> list[str]:
    """Return every running image name (lowercase, unique, sorted)."""
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        names: set[str] = set()
        while True:
            if entry.szExeFile:
                names.add(entry.szExeFile.lower())
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return sorted(names)
    finally:
        kernel32.CloseHandle(snapshot)


def is_process_running(image_name: str) -> bool:
    image_name = image_name.strip().lower()
    if not image_name:
        return False
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return False
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile and entry.szExeFile.lower() == image_name:
                return True
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return False
    finally:
        kernel32.CloseHandle(snapshot)


def taskkill_process(image_name: str, force: bool = False, tree: bool = False) -> tuple[int, str]:
    """End a process via taskkill; returns (returncode, stderr).

    returncode 0 means the request was accepted; /T ends the whole process tree.
    """
    cmd = ["taskkill", "/IM", image_name]
    if force:
        cmd.append("/F")
    if tree:
        cmd.append("/T")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=15,
        )
        return proc.returncode, (proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def elevated_taskkill(image_name: str, tree: bool = False) -> bool:
    """Kill via an elevated taskkill (UAC prompt).

    True when the elevated taskkill process was started; the user may still
    decline the prompt or the kill may fail afterwards.
    """
    cmd = f'taskkill /IM "{image_name}" /F'
    if tree:
        cmd += " /T"
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "taskkill.exe", cmd, None, 0,
    )
    return result > 32


class MacroPlayer:
    def __init__(self, on_status: Callable[[str], None] | None = None,
                 on_notice: Callable[[str, int], None] | None = None,
                 on_global_detect_request: Callable[[dict], None] | None = None,
                 on_restart_workflow_request: Callable[[dict], bool] | None = None,
                 on_log: Callable[[str], None] | None = None,
                 on_script_scope_enter: Callable[[list[dict]], object] | None = None,
                 on_script_scope_exit: Callable[[object], None] | None = None,
                 on_target_window_request: Callable[[], int | None] | None = None):
        self.on_status = on_status
        self.on_notice = on_notice
        self.on_global_detect_request = on_global_detect_request
        # 特殊模块“重新执行工作流”只由应用在当前工作流中接管；独立脚本
        # 没有可重启的工作流，因此回调返回 False 时跳过该固定动作。
        self.on_restart_workflow_request = on_restart_workflow_request
        # 运行日志通道：模块触发/超时等关键操作同步写入（on_status 只进状态栏）。
        self.on_log = on_log
        self.on_script_scope_enter = on_script_scope_enter
        self.on_script_scope_exit = on_script_scope_exit
        self.on_target_window_request = on_target_window_request
        self.stop_event = threading.Event()
        self.running = False
        self._held_keys: set[int] = set()
        self._held_buttons: set[str] = set()
        self._relative_target_hwnd: int | None = None
        self._legacy_relative_started = False
        self._source_screen: dict | None = None
        self._target_screen: dict | None = None
        self._jump_reason: str | None = None
        self._activate_target = True
        self._activation_hwnd: int | None = None
        self._workflow_context = False
        self._workflow_repeat_number = 0
        self._script_scope_managed = False

    def stop(self) -> None:
        self.stop_event.set()

    def reset(self) -> None:
        self.stop_event.clear()

    def _status(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)

    def _log_event(self, text: str) -> None:
        """状态栏 + 运行日志双写（模块触发/超时等关键操作）。"""
        self._status(text)
        if self.on_log:
            self.on_log(text)

    def _wait(self, milliseconds: int) -> None:
        if milliseconds <= 0:
            if self.stop_event.is_set():
                raise PlaybackStopped()
            return
        if self.stop_event.wait(milliseconds / 1000):
            raise PlaybackStopped()

    def play(self, actions: list[dict], repeats: int = 1, hwnd: int | None = None,
             repeat_interval_ms: int = 0,
             source_screen: dict | None = None,
             activate_target: bool = True,
             activation_hwnd: int | None = None,
             on_repeat: Callable[[int, int], None] | None = None,
             on_repeat_complete: Callable[[int, int], None] | None = None,
             start_index: int = 0, start_repeat: int = 0,
             resume_action_index: int | None = None,
             repeat_start_action_id: str | None = None,
             on_action: Callable[[int, int], None] | None = None,
             propagate_current_script_jump: bool = False,
             workflow_context: bool = False) -> bool | str:
        if self.running:
            raise RuntimeError("已有脚本正在执行")
        self.running = True
        # 每次播放前清空上次中断时的被引用脚本信息。
        self._last_stop_referenced_actions = None
        self._last_stop_referenced_source_screen = None
        self.reset()
        self._relative_target_hwnd = None
        self._legacy_relative_started = False
        self._source_screen = dict(source_screen) if source_screen else None
        self._target_screen = get_virtual_screen_rect() if self._source_screen else None
        self._activate_target = bool(activate_target)
        self._activation_hwnd = int(activation_hwnd) if activation_hwnd else None
        self._workflow_context = bool(workflow_context)
        self._workflow_repeat_number = 0
        self._advance_reason = ""
        advanced_to_next_workflow_step = False
        jump_current_script_last = False
        script_scope = None
        try:
            if hwnd and not is_window(hwnd):
                self._log_event(
                    "绑定窗口已失效；普通动作继续执行，只有相对转向或窗口区域动作需要重新绑定。",
                )
                hwnd = None
            if self._activation_hwnd and not is_window(self._activation_hwnd):
                raise RuntimeError("执行前置窗口已关闭，请重新选择")
            # “执行前置窗口”只在本次播放开始前激活一次，用于完成准备动作；
            # 它不是输入目标，不能在之后的相对鼠标动作中反复抢回前台。
            if self._activation_hwnd and not activate_window(self._activation_hwnd):
                self._status("未能执行一次前置窗口激活，将继续尝试发送输入")
            focus_hwnd = hwnd
            if focus_hwnd:
                if self._activate_target:
                    if not activate_window(focus_hwnd):
                        self._status("未能强制前置目标窗口，将继续尝试发送输入")
                    else:
                        self._relative_target_hwnd = int(focus_hwnd)
                elif is_window_process_foreground(focus_hwnd):
                    self._relative_target_hwnd = int(focus_hwnd)
                else:
                    self._status("已关闭自动前置；目标窗口当前不在前台")
            repeat_total = max(1, int(repeats))
            repeat_interval = max(0, int(repeat_interval_ms))
            first_action_index = max(0, min(int(start_index), max(0, len(actions) - 1)))
            start_repeat = max(0, min(int(start_repeat), max(0, repeat_total - 1)))
            resume_action = resume_action_index
            if resume_action is not None and int(resume_action) >= len(actions):
                # 被打断重复的脚本动作已全部完成：跳过该次重复，从下一次开头继续。
                # 断点置 None 而非 0，让下一次"全新重复"走 first_action_index 或
                # repeat_start_action_id 指定的起始行。start_repeat 超过
                # repeat_total - 1 时循环为空，表示整个步骤已完成。
                resume_action = None
                start_repeat = start_repeat + 1
            repeat_start_idx = None
            if repeat_start_action_id:
                repeat_start_idx = next(
                    (
                        i for i, action in enumerate(actions)
                        if str(action.get(ACTION_ID_KEY, "")).strip()
                        == str(repeat_start_action_id).strip()
                    ),
                    None,
                )
                if repeat_start_idx is None:
                    self._status("第 2 次起的起始行已失效（原行不存在），回退从第 1 行开始")
            for repeat_index in range(start_repeat, repeat_total):
                self._workflow_repeat_number = repeat_index + 1
                if on_repeat:
                    on_repeat(repeat_index + 1, repeat_total)
                # 每次重复重新进入脚本全局作用域：先退出上一重复注册的全局
                # 模块，再注册本次的。这样"执行 x 次"的每次重复都有独立的
                # 全局检测监控，超时等计时从本次重复开始重新计算。
                if self.on_script_scope_exit and script_scope is not None:
                    self.on_script_scope_exit(script_scope)
                    script_scope = None
                if self.on_script_scope_enter:
                    script_scope = self.on_script_scope_enter(actions)
                    self._script_scope_managed = True
                if repeat_start_idx is not None and repeat_index >= 1:
                    self._status(
                        f"执行第 {repeat_index + 1}/{repeat_total} 次"
                        f"（从第 {repeat_start_idx + 1} 行）"
                    )
                else:
                    self._status(f"执行第 {repeat_index + 1}/{repeat_total} 次")
                action_start = first_action_index
                if resume_action is not None and repeat_index == start_repeat:
                    # 断点恢复优先：被打断的那一次从断点继续。
                    action_start = max(0, min(int(resume_action), max(0, len(actions) - 1)))
                elif repeat_index >= 1 and repeat_start_idx is not None:
                    # 第 2 次及以后从指定行开始。
                    action_start = repeat_start_idx
                try:
                    self._run_action_sequence(
                        actions, hwnd, start_index=action_start, on_action=on_action,
                    )
                except JumpToCurrentScriptLastAction as request:
                    if propagate_current_script_jump:
                        jump_current_script_last = True
                        self._status("模块代码段要求跳转到当前脚本最后一行")
                        break
                    last_index = len(actions) - 1
                    if last_index >= 0 and last_index != request.current_index \
                            and str(actions[last_index].get("type")) \
                            != "jump_current_script_last":
                        self._status(f"模块代码段跳转到当前脚本第 {last_index + 1} 行")
                        self._run_action_sequence(
                            actions, hwnd, start_index=last_index,
                            on_action=on_action,
                        )
                except EndCurrentScriptRequest:
                    if on_repeat_complete:
                        on_repeat_complete(repeat_index + 1, repeat_total)
                    advanced_to_next_workflow_step = True
                    self._status(f"已{END_CURRENT_SCRIPT_LABEL}")
                    break
                except AdvanceToNextWorkflowStep:
                    # The current repeat counts as completed, but remaining
                    # repeats of this workflow step are skipped immediately.
                    if on_repeat_complete:
                        on_repeat_complete(repeat_index + 1, repeat_total)
                    advanced_to_next_workflow_step = True
                    self._status(
                        self._advance_reason
                        or "已结束当前脚本，执行工作流下一项"
                    )
                    break
                if on_repeat_complete:
                    on_repeat_complete(repeat_index + 1, repeat_total)
                if repeat_index + 1 < repeat_total and repeat_interval:
                    self._status(f"重复间隔 {repeat_interval} ms")
                    self._wait(repeat_interval)
        except PlaybackStopped as stopped:
            self._status("执行已停止")
            # 全局模块中断时若正处于被引用脚本内部，把该脚本的动作序列
            # 暴露给应用层，用于"结束当前脚本"跳到最内层脚本最后一行。
            self._last_stop_referenced_actions = stopped.referenced_actions
            self._last_stop_referenced_source_screen = stopped.referenced_source_screen
        finally:
            if self.on_script_scope_exit and script_scope is not None:
                self.on_script_scope_exit(script_scope)
            self._release_all(hwnd)
            self._relative_target_hwnd = None
            self._source_screen = None
            self._target_screen = None
            self._activate_target = True
            self._activation_hwnd = None
            self._workflow_context = False
            self._workflow_repeat_number = 0
            self._script_scope_managed = False
            self.running = False
        if jump_current_script_last:
            return JUMP_CURRENT_SCRIPT_LAST_RESULT
        return advanced_to_next_workflow_step

    def _run_action_sequence(self, actions: list[dict], hwnd: int | None,
                             start_index: int = 0,
                             script_stack: set[str] | None = None,
                             depth: int = 0,
                             on_action: Callable[[int, int], None] | None = None) -> None:
        """Execute an action sequence (a script or a referenced script) in order."""
        action_indices_by_id = {
            str(action.get("action_id")): index
            for index, action in enumerate(actions)
            if action.get("action_id")
        }
        index = max(0, min(int(start_index), max(0, len(actions) - 1)))
        while index < len(actions):
            action = actions[index]
            default_delay = 1000 if action.get("type") == "image_match" else 0
            self._wait(int(action.get("delay_ms", default_delay)))
            try:
                jump_target = self._execute_action(action, hwnd, script_stack, depth)
            except JumpToCurrentScriptLastAction as request:
                if depth == 0:
                    request.current_index = index
                raise
            self._status(f"动作 {index + 1}/{len(actions)}")
            if on_action and depth == 0:
                # 只在最外层记录，报告下一个要执行的动作下标（可能等于总数，表示脚本已完成）。
                on_action(index + 1, len(actions))
            after_delay = max(0, int(action.get("after_delay_ms", 0)))
            if after_delay:
                self._wait(after_delay)
            if jump_target is None:
                index += 1
                continue
            target_kind, target_value = jump_target
            if target_kind == "end_current_script":
                raise EndCurrentScriptRequest()
            if target_kind == "next_workflow_step":
                self._advance_reason = "已结束当前脚本，执行工作流下一项"
                raise AdvanceToNextWorkflowStep()
            if target_kind == "action_id":
                target_index = action_indices_by_id.get(str(target_value))
                if target_index is None:
                    raise RuntimeError("识图跳转目标动作已被删除，请重新选择")
            else:
                jump_row = int(target_value)
                if not 1 <= jump_row <= len(actions):
                    raise RuntimeError(f"识图跳转行无效：第 {jump_row} 行，脚本共 {len(actions)} 行")
                target_index = jump_row - 1
            self._status(f"{self._jump_reason or '识图'}，跳到第 {target_index + 1} 行目标动作")
            index = target_index

    def _scale_point(self, x: int, y: int) -> tuple[int, int]:
        return scale_screen_point(x, y, self._source_screen, self._target_screen)

    def _scale_region(self, region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, width, height = region
        left, top = self._scale_point(x, y)
        right, bottom = self._scale_point(x + width, y + height)
        return left, top, max(1, right - left), max(1, bottom - top)

    def _template_region(self, template_path: Path) -> tuple[int, int, int, int] | None:
        """已登记模板的检测区域（模板登记表实时读取）；未登记/未设置区域则全屏并告警。"""
        region = registered_template_region(template_path)
        if region and region[2] > 0 and region[3] > 0:
            return self._scale_region(tuple(map(int, region)))
        self._status(f"模板 {Path(template_path).name} 未设置区域，按全屏识别")
        return None

    def _execute_action(self, action: dict, hwnd: int | None,
                        script_stack: set[str] | None = None,
                        depth: int = 0) -> tuple[str, str | int] | None:
        kind = action.get("type")
        if kind == "delay":
            self._wait(int(action.get("ms", 100)))
        elif kind == "key":
            vk = int(action.get("vk", 0))
            down = bool(action.get("down", True))
            if not vk:
                return
            send_key(vk, down)
            (self._held_keys.add if down else self._held_keys.discard)(vk)
        elif kind == "key_press":
            vk = int(action.get("vk", 0))
            hold = int(action.get("hold_ms", 30))
            send_key(vk, True)
            self._wait(hold)
            send_key(vk, False)
        elif kind == "text":
            send_text(str(action.get("text", "")), int(action.get("char_delay_ms", 10)))
        elif kind == "mouse_move":
            move_mode = action.get("mode", "absolute")
            if move_mode == "relative":
                dx = int(action.get("dx", 0))
                dy = int(action.get("dy", 0))
                # 通用相对转向：MOUSEEVENTF_MOVE 是系统级事件，Windows 直接
                # 投递给当前前台窗口，无需任何窗口句柄，也不区分游戏/桌面
                # 窗口。有可用目标窗口时仅“尽力”激活到前台保证送达（激活
                # 失败不影响发送）；没有窗口也直接发送——任何前台状态下都
                # 能执行，不再有任何“需要有效目标窗口”的报错。
                focus_hwnd = self._relative_target_hwnd
                if not focus_hwnd or not is_window(focus_hwnd):
                    focus_hwnd = hwnd if hwnd and is_window(hwnd) else None
                    if self.on_target_window_request:
                        focus_hwnd = self.on_target_window_request() or focus_hwnd
                # 只在目标窗口变化时激活到前台（通常播放开始一次）；此后每个
                # 转向动作直接发送相对移动，避免每次 SetForegroundWindow 的
                # 系统开销拖慢转向序列。
                if focus_hwnd and self._relative_target_hwnd != int(focus_hwnd):
                    if self._activate_target:
                        if not activate_window(focus_hwnd):
                            self._status("未能强制前置目标窗口，将继续尝试发送输入")
                        else:
                            self._relative_target_hwnd = int(focus_hwnd)
                    elif is_window_process_foreground(focus_hwnd):
                        self._relative_target_hwnd = int(focus_hwnd)
                    else:
                        self._status("已关闭自动前置；目标窗口当前不在前台")
                if not self._legacy_relative_started:
                    self._status("游戏相对轨迹使用 1.2.1 兼容方式")
                    self._legacy_relative_started = True
                send_move_relative(dx, dy)
            else:
                x, y = int(action.get("x", 0)), int(action.get("y", 0))
                x, y = self._scale_point(x, y)
                send_move_absolute(x, y)
        elif kind == "mouse_button":
            button = str(action.get("button", "left"))
            down = bool(action.get("down", True))
            send_button(button, down)
            (self._held_buttons.add if down else self._held_buttons.discard)(button)
        elif kind == "click":
            button = str(action.get("button", "left"))
            hold_ms = int(action.get("hold_ms", 30))
            if action.get("pos_mode") == "current":
                # 点击鼠标当前位置：不移动光标、不做分辨率缩放。
                send_button(button, True)
                self._wait(hold_ms)
                send_button(button, False)
            else:
                x, y = int(action.get("x", get_cursor_pos()[0])), int(action.get("y", get_cursor_pos()[1]))
                x, y = self._scale_point(x, y)
                send_move_absolute(x, y)
                send_button(button, True)
                self._wait(hold_ms)
                send_button(button, False)
        elif kind == "repeat_click":
            button = str(action.get("button", "left"))
            x, y = int(action.get("x", get_cursor_pos()[0])), int(action.get("y", get_cursor_pos()[1]))
            x, y = self._scale_point(x, y)
            count = max(1, int(action.get("count", 2)))
            interval_ms = max(0, int(action.get("interval_ms", 100)))
            hold_ms = max(1, int(action.get("hold_ms", 30)))
            self._status(f"连续点击 {count} 次，间隔 {interval_ms} ms @ ({x}, {y})")
            for index in range(count):
                if self.stop_event.is_set():
                    raise PlaybackStopped()
                send_move_absolute(x, y)
                send_button(button, True)
                self._wait(hold_ms)
                send_button(button, False)
                if index < count - 1 and interval_ms > 0:
                    self._wait(interval_ms)
        elif kind == "scroll":
            dx, dy = int(action.get("dx", 0)), int(action.get("dy", 0))
            send_scroll(dy)
        elif kind == "image_match":
            return self._execute_image(action, hwnd, script_stack, depth)
        elif kind == "text_ocr":
            return self._execute_text_ocr(action, hwnd)
        elif kind == "global_detect":
            if self.on_global_detect_request and not self._script_scope_managed:
                self.on_global_detect_request(action)
        elif kind == "restart_workflow":
            if self.on_restart_workflow_request and self.on_restart_workflow_request(action):
                # 应用已接管：停止当前工作流并从第一步重新执行。
                raise PlaybackStopped()
            # 独立脚本运行时没有“当前工作流”，该固定动作不执行。
            return None
        elif kind == "end_current_script":
            raise EndCurrentScriptRequest()
        elif kind == "jump_current_script_last":
            raise JumpToCurrentScriptLastAction()
        elif kind == "activate_window":
            self._execute_activate_window(action)
        elif kind == "jump":
            if bool(action.get("workflow_repeat_at_least_2", False)) and (
                not self._workflow_context or self._workflow_repeat_number < 2
            ):
                self._jump_reason = None
                self._status("跳转动作条件未满足：仅在工作流第 2 次及以后跳转")
                return None
            self._jump_reason = "跳转动作"
            target_id = str(action.get("jump_action_id", "")).strip()
            if target_id == SCRIPT_START_TARGET_ID:
                return "row", 1
            if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                return "next_workflow_step", ""
            if target_id:
                return "action_id", target_id
            return "row", max(1, int(action.get("jump_row", 1)))
        elif kind == "script_ref":
            script_value = str(action.get("script", "")).strip()
            if not script_value:
                raise RuntimeError("引用脚本动作缺少脚本文件")
            script_path = resolve_path(script_value)
            if not script_path.is_file():
                raise RuntimeError(f"引用的脚本不存在：{script_value}")
            resolved = str(script_path.resolve())
            if script_stack is None:
                script_stack = set()
            if resolved in script_stack:
                raise RuntimeError(f"检测到脚本循环引用：{Path(script_path).stem} 引用了自身或其上级脚本")
            if depth >= MAX_SCRIPT_REF_DEPTH:
                raise RuntimeError("脚本引用嵌套过深，已停止执行")
            script_stack.add(resolved)
            referenced_scope = None
            try:
                referenced = load_script(script_path)
                if self.on_script_scope_enter:
                    referenced_scope = self.on_script_scope_enter(referenced.actions)
                self._status(
                    f"执行引用脚本 {referenced.name}（{len(referenced.actions)} 个动作）",
                )
                self._run_action_sequence(
                    referenced.actions, hwnd,
                    script_stack=script_stack, depth=depth + 1,
                )
            except EndCurrentScriptRequest:
                # 只在最近的脚本引用边界接住；模块代码段本身不是脚本边界，
                # 因而结束信号会先穿过代码段，再结束当前最里层引用脚本。
                self._status(f"已{END_CURRENT_SCRIPT_LABEL}（返回外层脚本）")
            except PlaybackStopped as stopped:
                # 异常从最内层先冒泡：只保留最内层被引用脚本的信息，
                # 嵌套更深的外层引用不覆盖。
                if stopped.referenced_actions is None:
                    stopped.referenced_actions = referenced.actions
                    stopped.referenced_source_screen = (
                        dict(referenced.settings.get("recorded_screen", {})) or None
                    )
                raise
            finally:
                if self.on_script_scope_exit and referenced_scope is not None:
                    self.on_script_scope_exit(referenced_scope)
                script_stack.discard(resolved)
        elif kind == "open_app":
            app_value = str(action.get("path", "")).strip()
            if not app_value:
                raise RuntimeError("打开软件动作缺少路径")
            app_path = resolve_path(app_value)
            if not app_path.is_file():
                raise RuntimeError(f"要打开的软件不存在：{app_value}")
            app_args = str(action.get("args", "")).strip()
            try:
                os.startfile(str(app_path), arguments=app_args)
            except OSError as exc:
                raise RuntimeError(f"无法打开软件：{app_value}（{exc}）") from exc
            self._status(f"已启动软件 {app_path.name}" + (f"（{app_args}）" if app_args else ""))
        elif kind == "close_app":
            self._execute_close_app(action)
        elif kind == "notice":
            text = str(action.get("text", "提醒"))
            duration = max(500, min(60000, int(action.get("duration_ms", 3000))))
            if self.on_notice:
                self.on_notice(text, duration)
        elif kind in {"comment", None}:
            return
        else:
            raise RuntimeError(f"未知动作类型：{kind}")

    def _close_process(self, image_name: str, graceful: bool = True,
                       graceful_wait_ms: int = 2000, tree: bool = False,
                       elevated_retry: bool = False) -> None:
        """End a process by image name; graceful close first, force with retries
        as fallback, then optionally an elevated kill. Raises RuntimeError when
        the process survives every attempt."""
        if not is_process_running(image_name):
            return
        if graceful:
            code, _err = taskkill_process(image_name, force=False, tree=tree)
            if code != 0:
                self._status(f"{image_name} 关闭请求失败（权限不足或进程异常），改为强制结束")
            else:
                deadline = time.perf_counter() + max(0, int(graceful_wait_ms)) / 1000
                while is_process_running(image_name):
                    if self.stop_event.is_set():
                        raise PlaybackStopped()
                    if time.perf_counter() >= deadline:
                        break
                    self._wait(50)
                if not is_process_running(image_name):
                    self._status(f"已结束 {image_name}")
                    return
                self._status(f"{image_name} 未响应关闭请求，强制结束")
        else:
            self._status(f"强制结束 {image_name}")
        for _ in range(3):
            code, _err = taskkill_process(image_name, force=True, tree=tree)
            if code != 0:
                # taskkill 本身失败（如权限不足），轮询等待没有意义
                self._wait(200)
                continue
            deadline = time.perf_counter() + 1000 / 1000
            while True:
                if not is_process_running(image_name):
                    self._status(f"已强制结束 {image_name}")
                    return
                if self.stop_event.is_set():
                    raise PlaybackStopped()
                if time.perf_counter() >= deadline:
                    break
                self._wait(50)
            self._wait(200)
        if elevated_retry:
            self._status(f"{image_name} 普通权限无法结束，尝试以管理员权限结束（可能弹出 UAC 授权窗口）")
            if elevated_taskkill(image_name, tree=tree):
                deadline = time.perf_counter() + 8000 / 1000
                while True:
                    if not is_process_running(image_name):
                        self._status(f"已以管理员权限结束 {image_name}")
                        return
                    if self.stop_event.is_set():
                        raise PlaybackStopped()
                    if time.perf_counter() >= deadline:
                        break
                    self._wait(50)
            self._status("管理员权限结束失败或授权被取消")
        raise RuntimeError(f"无法结束进程：{image_name}")

    def _execute_close_app(self, action: dict) -> None:
        image_name = str(action.get("name", "")).strip()
        if not image_name:
            raise RuntimeError("关闭软件动作缺少进程名")
        if not is_process_running(image_name):
            self._status(f"{image_name} 未在运行，跳过")
            return
        self._close_process(
            image_name,
            graceful=bool(action.get("graceful", True)),
            graceful_wait_ms=int(action.get("graceful_wait_ms", 2000)),
            tree=bool(action.get("tree", False)),
            elevated_retry=bool(action.get("elevated_retry", False)),
        )

    def _execute_activate_window(self, action: dict) -> None:
        """Resolve a saved stable window signature and bring that live window forward."""
        signature = action.get("window") or {}
        title = str(signature.get("title", "")).strip()
        class_name = str(signature.get("class_name", "")).strip()
        process_path = os.path.normcase(str(signature.get("process_path", "")).strip()).casefold()
        if not title and not class_name and not process_path:
            raise RuntimeError("前置窗口动作未设置窗口")

        candidates = enum_windows()

        def score(item) -> int:
            item_path = os.path.normcase(str(item.process_path or "")).casefold()
            value = 0
            if title and item.title == title:
                value += 4
            if class_name and item.class_name == class_name:
                value += 3
            if process_path and item_path == process_path:
                value += 5
            return value

        required = (4 if title else 0) + (3 if class_name else 0) + (5 if process_path else 0)
        exact = [item for item in candidates if score(item) == required]
        if exact:
            selected = exact[0]
        else:
            # 窗口标题可能包含会话状态；类名 + 程序路径仍能稳定找回重启后的窗口。
            stable = [
                item for item in candidates
                if (class_name or process_path)
                and (not class_name or item.class_name == class_name)
                and (not process_path or os.path.normcase(str(item.process_path or "")).casefold() == process_path)
            ]
            selected = stable[0] if stable else None
        if selected is None or not activate_window(selected.hwnd):
            raise RuntimeError(f"要前置的窗口当前未打开：{title or class_name or process_path}")
        self._relative_target_hwnd = int(selected.hwnd)
        self._status(f"已前置窗口：{selected.title}")

    def _execute_image(self, action: dict, hwnd: int | None,
                       script_stack: set[str] | None = None,
                       depth: int = 0) -> tuple[str, str | int] | None:
        template = resolve_path(str(action.get("template", "")))
        module_obj = None
        if action.get("module_ref"):
            # 实时引用：阻塞/相似度/间隔/延时/动作B 全部从模块区域对象读取。
            module_obj = registered_module_object(
                str(action.get("module_key") or action.get("template", ""))
            )
            if module_obj is None:
                self._status(f"模块 {Path(str(action.get('template', ''))).name} 不存在，按内嵌参数执行")
            elif str(module_obj.get("template", "")).strip():
                template = resolve_path(str(module_obj["template"]))
        if module_obj is not None and module_obj.get("recognize") == "none":
            module_label = str(module_obj.get("name") or "无需识图模块")
            self._status(f"无需识图，直接执行模块：{module_label}")
            self._log_event(f"模块 {module_label} 无需识图，直接执行")
            self._wait(max(0, int(module_obj.get("delay_ms", 0))))
            result = self._after_module_success(
                module_obj, {}, hwnd, script_stack, depth,
            )
            return result if result is not None else self._module_result_route(
                action, module_obj, succeeded=True,
            )
        timeout_ms = max(0, int(action.get("timeout_ms", 3000)))
        wait_forever = bool(action.get("wait_forever", False))
        interval_ms = max(50, int(action.get("interval_ms", 250)))
        threshold = min(1.0, max(0.1, float(action.get("threshold", 0.85))))
        if module_obj is not None:
            wait_forever = bool(module_obj.get("blocking", False))
            interval_ms = max(50, int(module_obj.get("interval_ms", 250)))
            threshold = min(1.0, max(0.1, float(module_obj.get("threshold", 0.85))))
            timeout_ms = max(0, int(module_obj.get("not_found_timeout_ms", timeout_ms)))
        # OCR 单次约几百毫秒，文字和数字模式的轮询间隔不能太短。
        text_module = bool(module_obj is not None and module_obj.get("recognize") == "text")
        number_module = bool(module_obj is not None and module_obj.get("recognize") == "number")
        wait_target_absent = bool(
            module_obj is not None and module_obj.get("wait_text_absent", False)
        )
        if text_module or number_module:
            interval_ms = max(interval_ms, 200)
        expected_number = None
        if number_module:
            if "expected_number" not in action:
                raise RuntimeError("数字读取模块的当前脚本行未设置比较数字")
            try:
                expected_number = int(action["expected_number"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("数字读取模块的比较数字不是有效整数") from exc
        if wait_target_absent:
            # “直到目标消失”本身就是无限等待条件，不受普通识别超时影响。
            wait_forever = True
        ignore_background = bool(
            (module_obj or action).get("ignore_background", False)
        )
        module_timeout_enabled = bool(
            module_obj is not None and module_obj.get("run_code_on_timeout", False)
        )
        if wait_target_absent:
            module_timeout_enabled = False
        if number_module:
            module_timeout_enabled = False
        module_timeout_ms = max(
            0, int(module_obj.get("not_found_timeout_ms", 3000))
        ) if module_obj is not None else 0
        fallback_template = None
        if module_obj is None:
            fallback_template = (
                resolve_path(str(action.get("fallback_template", "")))
                if str(action.get("fallback_template", "")).strip() else None
            )
        fallback_switch_ms = max(0, int(action.get("fallback_switch_ms", 3000)))
        fallback_region = None
        module_fallback = None
        if module_obj is not None:
            fallback_key = str(module_obj.get("fallback_module_key", "")).strip()
            candidate = registered_module_object(fallback_key) if fallback_key else None
            if candidate is not None and candidate.get("recognize") not in ("number", "none"):
                module_fallback = candidate
        if fallback_template is not None:
            fallback_region_mode = action.get("fallback_region_mode", "screen")
            if fallback_region_mode == "template":
                # 备用模板引用已登记模板：区域运行时从模板登记表读取。
                fallback_region = self._template_region(fallback_template)
            elif fallback_region_mode == "custom":
                raw = action.get("fallback_region", [0, 0, 0, 0])
                if len(raw) == 4 and int(raw[2]) > 0 and int(raw[3]) > 0:
                    fallback_region = self._scale_region(tuple(map(int, raw)))
            elif fallback_region_mode == "window":
                if not hwnd:
                    raise RuntimeError("窗口区域识别需要有效的目标窗口，请重新绑定")
                fallback_region = get_window_rect(hwnd)
        region = None
        region_mode = action.get("region_mode", "screen")
        if region_mode == "template":
            # 模块引用使用自己的独立区域；同一图片可被多个模块以不同区域复用。
            if module_obj is not None:
                raw = module_obj.get("region", [])
                if len(raw) == 4 and int(raw[2]) > 0 and int(raw[3]) > 0:
                    region = self._scale_region(tuple(map(int, raw)))
            else:
                region = self._template_region(template)
        elif region_mode == "custom":
            raw = action.get("region", [0, 0, 0, 0])
            if len(raw) == 4 and int(raw[2]) > 0 and int(raw[3]) > 0:
                region = tuple(map(int, raw))
                region = self._scale_region(region)
        elif region_mode == "window":
            if not hwnd:
                raise RuntimeError("窗口区域识别需要有效的目标窗口，请重新绑定")
            region = get_window_rect(hwnd)
        if number_module and region is None:
            raise RuntimeError("数字读取模块未设置有效的指定识别区域")
        start = time.perf_counter()
        match = None
        fallback_active = False
        module_fallback_present = False
        recognized = ""
        waiting_absent_logged = False
        last_ocr_observation = None
        while True:
            if self.stop_event.is_set():
                raise PlaybackStopped()
            if number_module:
                recognized, ocr_matches = recognize_region_with_boxes(region)
                number_value, raw_digits = extract_ocr_integer(recognized, ocr_matches)
                module_label = str(module_obj.get("name") or "读取数字")
                if number_value is not None:
                    equal = number_value == expected_number
                    observation = (
                        f"模块 {module_label} 读取「{raw_digits}」→ 数字 {number_value}；"
                        f"比较 {expected_number} · {'相等' if equal else '不相等'}"
                    )
                    self._log_event(observation)
                    self._status(observation)
                    return self._module_result_route(
                        action, module_obj, succeeded=equal,
                        result_label=f"比较结果：{'相等' if equal else '不相等'}",
                    )
                observation = f"模块 {module_label}：指定区域内未读取到数字"
                if observation != last_ocr_observation:
                    last_ocr_observation = observation
                    self._log_event(observation)
                match = None
            elif text_module:
                recognized, ocr_matches = recognize_region_with_boxes(region)
                expected_text = str(module_obj.get("expected_text", ""))
                match_mode = str(module_obj.get("match_mode", "contains"))
                match = find_expected_match(ocr_matches, expected_text, match_mode)
                text_present = match is not None
                if not text_present and matches_expected(recognized, expected_text, match_mode):
                    # 极少数期望内容可能横跨多个 OCR 行；仍保留旧的整体匹配能力。
                    text_present = True
                    match = self._ocr_match_data(region)
                observation = format_ocr_observation(
                    recognized, expected_text, text_present,
                    str(module_obj.get("name") or "识别文字模块"),
                )
                if observation != last_ocr_observation:
                    last_ocr_observation = observation
                    self._log_event(observation)
                if wait_target_absent and text_present:
                    if not waiting_absent_logged:
                        waiting_absent_logged = True
                        self._status(
                            f"识别文字命中，循环点击直到消失：{recognized[:40] or '（无文字）'}"
                        )
                    self._wait(max(0, int(module_obj.get("delay_ms", 0))))
                    result = self._after_module_success(
                        module_obj, match, hwnd, script_stack, depth,
                    )
                    if result is not None:
                        return result
                    self._wait(interval_ms)
                    continue
                elif wait_target_absent:
                    self._status("框选区域内已检测不到期望文字，结束循环")
                    if action.get("show_result_notice") and self.on_notice:
                        self.on_notice("期望文字已消失，循环点击完成", 3500)
                    return self._module_result_route(action, module_obj, succeeded=True)
                elif text_present:
                    break
                else:
                    match = None
            else:
                # 主模板始终在自己的区域检测；备用激活后两者同时检测（各自区域）。
                match = find_template(template, threshold, region,
                                      ignore_background=ignore_background)
                if wait_target_absent and match:
                    if not waiting_absent_logged:
                        waiting_absent_logged = True
                        self._status(
                            f"模板图片命中，循环执行直到消失：{Path(template).name}"
                        )
                    show_overlay(
                        match["x"], match["y"], match["width"], match["height"],
                    )
                    self._wait(max(0, int(module_obj.get("delay_ms", 0))))
                    result = self._after_module_success(
                        module_obj, match, hwnd, script_stack, depth,
                    )
                    if result is not None:
                        return result
                    self._wait(interval_ms)
                    continue
                if wait_target_absent:
                    self._status("框选区域内已检测不到目标模板，结束循环")
                    if action.get("show_result_notice") and self.on_notice:
                        self.on_notice("目标模板已消失，循环执行完成", 3500)
                    return self._module_result_route(action, module_obj, succeeded=True)
                if match:
                    break
            fallback_match = None
            if module_fallback is not None:
                fallback_match = self._match_fallback_module(module_fallback, hwnd)
                if fallback_match and not module_fallback_present:
                    module_fallback_present = True
                    fallback_name = str(module_fallback.get("name") or "备用识别模块")
                    show_overlay(
                        fallback_match["x"], fallback_match["y"],
                        fallback_match["width"], fallback_match["height"],
                    )
                    if module_obj.get("fallback_click", False):
                        self._click_module_point(
                            int(fallback_match["center_x"]), int(fallback_match["center_y"]),
                            str(module_fallback.get("button", "left")),
                            max(1, int(module_fallback.get("click_count", 1))),
                        )
                        self._status(f"备用模块 {fallback_name} 已识别并点击，继续识别主模块")
                    else:
                        self._status(f"备用模块 {fallback_name} 已识别，继续识别主模块")
                elif not fallback_match:
                    module_fallback_present = False
            if wait_forever and fallback_template is not None and (
                fallback_active
                or (time.perf_counter() - start) * 1000 >= fallback_switch_ms
            ):
                if not fallback_active:
                    fallback_active = True
                    self._status(
                        f"等待 {Path(template).name} 超过 {fallback_switch_ms} ms，"
                        f"备用模板 {Path(fallback_template).name} 加入同时检测",
                    )
                fallback_match = find_template(fallback_template, threshold, fallback_region,
                                               ignore_background=ignore_background)
            if module_obj is None and fallback_match:
                # 备用模板命中：圈出匹配区域提醒；可选点击；出现后回到主模板检测或直接退出识图。
                show_overlay(
                    fallback_match["x"], fallback_match["y"],
                    fallback_match["width"], fallback_match["height"],
                )
                fallback_x, fallback_y = fallback_match["center_x"], fallback_match["center_y"]
                if action.get("fallback_click", True):
                    if action.get("click_target", "match") == "custom":
                        raw_point = action.get("click_point", [fallback_x, fallback_y])
                        if isinstance(raw_point, (list, tuple)) and len(raw_point) >= 2:
                            fallback_x, fallback_y = self._scale_point(
                                int(raw_point[0]), int(raw_point[1]),
                            )
                    send_move_absolute(fallback_x, fallback_y)
                    send_button(str(action.get("button", "left")), True)
                    self._wait(30)
                    send_button(str(action.get("button", "left")), False)
                if action.get("show_result_notice") and self.on_notice:
                    self.on_notice(
                        f"备用模板已出现：{Path(fallback_template).name} · "
                        f"({fallback_x}, {fallback_y})",
                        3500,
                    )
                if action.get("fallback_on_match", "回到主模板的检测") == "直接退出识别":
                    self._status(
                        f"备用模板 {Path(fallback_template).name} 已出现，退出识图",
                    )
                    return
                click_text = "点击后" if action.get("fallback_click", True) else ""
                self._status(
                    f"备用模板 {Path(fallback_template).name} 已出现，{click_text}回到主模板检测",
                )
                self._wait(interval_ms)
                continue
            if module_timeout_enabled and (time.perf_counter() - start) * 1000 >= module_timeout_ms:
                segment = list(module_obj.get("on_timeout_actions") or [])
                timeout_subject = (
                    str(module_obj.get("name") or module_obj.get("expected_text") or "识别文字")
                    if text_module else str(module_obj.get("name") or "").strip()
                    or Path(template).name
                )
                self._log_event(
                    f"模块 {timeout_subject} 连续 {module_timeout_ms} ms 未识别到，"
                    f"执行超时代码段（{len(segment)} 个动作）",
                )
                if depth >= MAX_SCRIPT_REF_DEPTH:
                    raise RuntimeError("模块超时代码段嵌套过深，已停止执行")
                if segment:
                    self._run_action_sequence(
                        segment, hwnd,
                        script_stack=script_stack, depth=depth + 1,
                    )
                return self._module_result_route(
                    action, module_obj, succeeded=False,
                    result_label="读取结果：未读取到数字" if number_module else None,
                )
            if not wait_forever and (time.perf_counter() - start) * 1000 >= timeout_ms:
                subject = (
                    str(module_obj.get("name") or "读取数字")
                    if number_module else "识别文字" if text_module else Path(template).name
                )
                if action.get("show_result_notice") and self.on_notice:
                    self.on_notice(
                        f"未读取到数字：{subject} · {timeout_ms} ms 内未读取到数字"
                        if number_module else f"识别文字未找到：{subject} · {timeout_ms} ms 超时"
                        if text_module else
                        f"识图未找到：{subject} · {timeout_ms} ms 超时",
                        3500,
                    )
                self._wait(max(0, int(action.get("timeout_delay_ms", 0))))
                if module_obj is not None:
                    if number_module:
                        self._log_event(
                            f"模块 {subject} 连续 {timeout_ms} ms 未读取到数字，"
                            "按“不等于或未读取到”分支处理"
                        )
                    return self._module_result_route(
                        action, module_obj, succeeded=False,
                        result_label="读取结果：未读取到数字" if number_module else None,
                    )
                timeout_action = action.get("on_timeout", "continue")
                if timeout_action == "continue":
                    self._status("识别文字超时，按设置继续" if text_module
                                 else "识图超时，按设置继续")
                    return
                if timeout_action == "end_current_script":
                    self._status("识图超时，结束当前脚本")
                    return "end_current_script", 0
                if timeout_action == "jump":
                    target_id = str(action.get("timeout_jump_action_id", "")).strip()
                    if target_id:
                        self._jump_reason = "识别文字超时" if text_module else "识图超时"
                        return "action_id", target_id
                    self._jump_reason = "识别文字超时" if text_module else "识图超时"
                    return "row", max(1, int(action.get("timeout_jump_row", 1)))
                raise RuntimeError(f"识别文字超时：{subject}" if text_module
                                   else f"识图超时：{subject}")
            self._wait(interval_ms)

        if not text_module:
            show_overlay(match["x"], match["y"], match["width"], match["height"])
        if text_module:
            self._status(f"识别文字命中：{recognized[:40]}")
        else:
            self._status(f"识图成功，相似度 {match['score']:.1%}")
        if action.get("show_result_notice") and self.on_notice:
            self.on_notice(
                f"识别文字命中：{recognized[:40] or '（空）'}"
                if text_module else
                f"识图成功：{Path(template).name} · {match['score']:.1%} · "
                f"({match['center_x']}, {match['center_y']})",
                3500,
            )
        found_delay = (module_obj.get("delay_ms", 0) if module_obj is not None
                       else action.get("found_delay_ms", 0))
        self._wait(max(0, int(found_delay)))
        if module_obj is not None:
            # 实时引用：动作 B 由模块对象决定（点击识别区域/自定义/继续/二次识别/代码段）。
            result = self._after_module_success(module_obj, match, hwnd,
                                                script_stack, depth)
            return result if result is not None else self._module_result_route(
                action, module_obj, succeeded=True,
            )
        if action.get("on_found", "click") == "click":
            x, y = match["center_x"], match["center_y"]
            if action.get("click_target", "match") == "custom":
                raw_point = action.get("click_point", [x, y])
                if isinstance(raw_point, (list, tuple)) and len(raw_point) >= 2:
                    x, y = self._scale_point(int(raw_point[0]), int(raw_point[1]))
            send_move_absolute(x, y)
            send_button(str(action.get("button", "left")), True)
            self._wait(30)
            send_button(str(action.get("button", "left")), False)
        elif action.get("on_found") == "jump":
            target_id = str(action.get("found_jump_action_id", "")).strip()
            if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                self._jump_reason = "识图成功"
                return "next_workflow_step", 0
            if target_id:
                self._jump_reason = "识图成功"
                return "action_id", target_id
            self._jump_reason = "识图成功"
            return "row", max(1, int(action.get("found_jump_row", 1)))

    def _match_fallback_module(self, obj: dict, hwnd: int | None) -> dict | None:
        raw_region = obj.get("region") or []
        region = None
        if len(raw_region) == 4 and int(raw_region[2]) > 0 and int(raw_region[3]) > 0:
            region = self._scale_region(tuple(map(int, raw_region)))
        if obj.get("recognize") == "text":
            recognized, boxes = recognize_region_with_boxes(region)
            expected = str(obj.get("expected_text", ""))
            mode = str(obj.get("match_mode", "contains"))
            match = find_expected_match(boxes, expected, mode)
            if match is None and matches_expected(recognized, expected, mode):
                match = self._ocr_match_data(region)
            return match
        template = resolve_path(str(obj.get("template", "")))
        if not template.is_file():
            return None
        return find_template(
            template,
            min(1.0, max(0.1, float(obj.get("threshold", 0.85)))),
            region,
            ignore_background=bool(obj.get("ignore_background", False)),
        )

    def _module_result_route(self, action: dict, module_obj: dict, succeeded: bool,
                             result_label: str | None = None) -> tuple[str, str | int] | None:
        """Publish one module result and apply this reference row's branch."""
        result_text = "成功" if succeeded else "失败"
        module_label = str(module_obj.get("name") or "").strip() \
            or Path(str(module_obj.get("template", ""))).name or "模块"
        self._log_event(f"模块 {module_label} {result_label or f'执行结果：{result_text}'}")
        status_result = result_label or f"执行{result_text}"
        behavior_key = "on_found" if succeeded else "on_timeout"
        target_key = "found_jump_action_id" if succeeded else "timeout_jump_action_id"
        legacy_row_key = "found_jump_row" if succeeded else "timeout_jump_row"
        behavior = str(action.get(behavior_key, "continue"))
        if behavior in {"continue", "click"}:
            self._status(f"模块{status_result}，按设置继续下一行")
            return None
        if behavior == "end_current_script":
            self._status(f"模块{status_result}，按设置结束当前最里层脚本")
            return "end_current_script", 0
        if behavior == "jump":
            self._jump_reason = f"模块{status_result}"
            target_id = str(action.get(target_key, "")).strip()
            if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                return "next_workflow_step", 0
            if target_id:
                return "action_id", target_id
            return "row", max(1, int(action.get(legacy_row_key, 1)))
        raise RuntimeError(f"模块执行{result_text}，按设置停止全部执行")

    def _execute_text_ocr(self, action: dict, hwnd: int | None,
                          ) -> tuple[str, str | int] | None:
        """识别文字动作：截取区域 OCR，命中则继续/跳转，未命中轮询到超时。

        期望文字为空时识别到任意文字即命中；timeout_ms=0 只识别一次。
        """
        region = None
        region_mode = action.get("region_mode", "screen")
        if region_mode == "custom":
            raw = action.get("region", [0, 0, 0, 0])
            if len(raw) == 4 and int(raw[2]) > 0 and int(raw[3]) > 0:
                region = self._scale_region(tuple(map(int, raw)))
        elif region_mode == "window":
            if not hwnd:
                raise RuntimeError("窗口区域识别需要有效的目标窗口，请重新绑定")
            region = get_window_rect(hwnd)
        expected = str(action.get("expected_text", "")).strip()
        match_mode = str(action.get("match_mode", "contains"))
        timeout_ms = max(0, int(action.get("timeout_ms", 3000)))
        interval_ms = max(200, int(action.get("interval_ms", 500)))
        start = time.perf_counter()
        last_ocr_observation = None
        while True:
            if self.stop_event.is_set():
                raise PlaybackStopped()
            recognized = recognize_region(region)
            matched = matches_expected(recognized, expected, match_mode)
            observation = format_ocr_observation(
                recognized, expected, matched, "识别文字动作",
            )
            if observation != last_ocr_observation:
                last_ocr_observation = observation
                self._log_event(observation)
            if matched:
                if expected:
                    self._status(f"识别文字命中：{recognized[:40]}")
                else:
                    self._status(f"识别到文字：{recognized[:40]}")
                if action.get("show_result_notice") and self.on_notice:
                    self.on_notice(
                        f"识别文字命中：{recognized[:40] or '（空）'}", 3500,
                    )
                self._wait(max(0, int(action.get("found_delay_ms", 0))))
                if action.get("on_found", "continue") == "jump":
                    self._jump_reason = "识别文字命中"
                    target_id = str(action.get("found_jump_action_id", "")).strip()
                    if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                        return "next_workflow_step", 0
                    if target_id:
                        return "action_id", target_id
                    return "row", max(1, int(action.get("found_jump_row", 1)))
                return None
            if timeout_ms <= 0 or (time.perf_counter() - start) * 1000 >= timeout_ms:
                break
            self._wait(interval_ms)
        self._status(f"识别文字未命中：{recognized[:40] or '（无文字）'}")
        if action.get("show_result_notice") and self.on_notice:
            self.on_notice(
                f"识别文字未命中：{expected or '任意文字'} · "
                f"{timeout_ms or 0} ms 超时",
                3500,
            )
        self._wait(max(0, int(action.get("timeout_delay_ms", 0))))
        timeout_action = action.get("on_timeout", "continue")
        if timeout_action == "continue":
            self._status("识别文字超时，按设置继续")
            return None
        if timeout_action == "jump":
            self._jump_reason = "识别文字超时"
            target_id = str(action.get("timeout_jump_action_id", "")).strip()
            if target_id:
                return "action_id", target_id
            return "row", max(1, int(action.get("timeout_jump_row", 1)))
        raise RuntimeError("识别文字超时未命中，按设置停止")

    def _ocr_match_data(self, region) -> dict:
        """识别文字命中时构造的伪匹配：点击识别区域用区域中心，全屏用主屏中心。

        文字识别没有模板那样的精确位置，只有识别区域；区域为空（全屏）时
        退化为主屏中心，保证"点击识别区域"仍有坐标可用。
        """
        if region:
            x, y, w, h = (int(part) for part in region)
        else:
            with mss.mss() as grabber:
                monitor = grabber.monitors[1]
            x, y, w, h = (int(monitor[k]) for k in ("left", "top", "width", "height"))
        return {
            "x": x, "y": y, "width": w, "height": h,
            "center_x": x + w // 2, "center_y": y + h // 2,
            "score": 1.0,
        }

    def _click_module_point(self, x: int, y: int, button: str, count: int) -> None:
        """Click one module target repeatedly with cooperative F12 cancellation."""
        send_move_absolute(int(x), int(y))
        total = max(1, min(9999, int(count)))
        for index in range(total):
            send_button(button, True)
            self._wait(30)
            send_button(button, False)
            if index + 1 < total:
                self._wait(50)

    def _after_module_success(self, obj: dict, match: dict, hwnd: int | None,
                              script_stack: set[str] | None,
                              depth: int) -> tuple[str, str | int] | None:
        """先执行模块主动作，再执行可选代码段，最后才返回原执行流。"""
        after_action = obj.get("after_action", "click_match")
        button = str(obj.get("button", "left"))
        click_count = max(1, min(9999, int(obj.get("click_count", 1))))
        module_label = str(obj.get("name") or "").strip() \
            or Path(str(obj.get("template", ""))).name or "模块"
        result = None
        if after_action in ("click_match", "click_custom"):
            if after_action == "click_custom":
                raw_point = obj.get("click_point", [])
                if not isinstance(raw_point, (list, tuple)) or len(raw_point) < 2:
                    raise RuntimeError(f"模块 {module_label} 未设置自定义点击位置")
                x, y = self._scale_point(int(raw_point[0]), int(raw_point[1]))
            else:
                x, y = match["center_x"], match["center_y"]
            if after_action == "click_match" and obj.get("recognize") == "text":
                x += int(obj.get("ocr_offset_right", 0)) - int(obj.get("ocr_offset_left", 0))
                y += int(obj.get("ocr_offset_down", 0)) - int(obj.get("ocr_offset_up", 0))
            self._click_module_point(x, y, button, click_count)
            self._log_event(
                f"模块 {module_label} 已点击 ({x}, {y})"
                + (f" × {click_count}" if click_count > 1 else "")
            )
        elif after_action == "second_match":
            result = self._execute_second_match(obj, hwnd, match)
        # 旧对象的 run_actions 与新对象的开关都按附加代码段处理。
        run_segment = bool(obj.get("run_code_after_action", False)) \
            or after_action == "run_actions"
        if run_segment:
            segment = list(obj.get("on_success_actions") or [])
            if depth >= MAX_SCRIPT_REF_DEPTH:
                raise RuntimeError("模块代码段嵌套过深，已停止执行")
            if segment:
                self._log_event(
                    f"模块 {module_label} 主动作完成，执行附加代码段"
                    f"（{len(segment)} 个动作）",
                )
                self._run_action_sequence(segment, hwnd,
                                          script_stack=script_stack, depth=depth + 1)
        return result

    def _execute_second_match(self, obj: dict, hwnd: int | None,
                              first_match: dict | None = None) -> None:
        """二次识别成功后，按配置点击首次、二次或自定义区域中心。"""
        second = str(obj.get("second_match_template", "")).strip()
        if not second:
            self._status("模块未设置二次识别模板，直接继续")
            return None
        second_path = resolve_path(second)
        threshold = min(1.0, max(0.1, float(obj.get("threshold", 0.85))))
        interval_ms = max(50, int(obj.get("interval_ms", 250)))
        ignore_background = bool(obj.get("ignore_background", False))
        region = None
        # 二次模板沿用它在模块对象仓库中登记的搜索区域；未登记有效区域则全屏。
        raw = registered_template_region(second)
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            if int(raw[2]) > 0 and int(raw[3]) > 0:
                region = self._scale_region(tuple(map(int, raw)))
        blocking = bool(obj.get("blocking", False))
        timeout_ms = max(0, int(obj.get("second_match_timeout_ms", 3000)))
        start = time.perf_counter()
        while True:
            if self.stop_event.is_set():
                raise PlaybackStopped()
            second_match = find_template(second_path, threshold, region,
                                         ignore_background=ignore_background)
            if second_match:
                break
            if not blocking and (time.perf_counter() - start) * 1000 >= timeout_ms:
                self._status(f"二次识别超时：{Path(second_path).name} 未出现，继续")
                return None
            self._wait(interval_ms)
        show_overlay(second_match["x"], second_match["y"],
                     second_match["width"], second_match["height"])
        click_target = str(obj.get("second_match_click_target", "second"))
        if click_target == "first" and first_match:
            x, y = first_match["center_x"], first_match["center_y"]
            target_label = "第一次识别位置"
        elif click_target == "custom_region":
            raw_click_region = obj.get("second_match_click_region", [])
            if isinstance(raw_click_region, (list, tuple)) and len(raw_click_region) == 4:
                click_region = self._scale_region(tuple(map(int, raw_click_region)))
                x = click_region[0] + click_region[2] // 2
                y = click_region[1] + click_region[3] // 2
                target_label = "自定义框选区域"
            else:
                x, y = second_match["center_x"], second_match["center_y"]
                target_label = "第二次识别位置"
        else:
            x, y = second_match["center_x"], second_match["center_y"]
            target_label = "第二次识别位置"
        click_count = max(1, min(9999, int(obj.get("click_count", 1))))
        self._click_module_point(
            x, y, str(obj.get("button", "left")), click_count,
        )
        self._status(
            f"二次识别成功，已点击{target_label} ({x}, {y})"
            + (f" × {click_count}" if click_count > 1 else "")
            + f" · {Path(second_path).name}",
        )
        return None

    def _release_all(self, hwnd: int | None) -> None:
        for vk in list(self._held_keys):
            try:
                send_key(vk, False)
            except Exception:
                pass
        for button in list(self._held_buttons):
            try:
                send_button(button, False)
            except Exception:
                pass
        self._held_keys.clear()
        self._held_buttons.clear()
