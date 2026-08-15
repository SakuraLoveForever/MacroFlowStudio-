from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog

import ttkbootstrap as ttk
import pystray
from PIL import Image, ImageDraw
from pynput import keyboard

from alerts import play_alert, prewarm_alert
from detect_overlay import show_overlay
from dialogs import (
    ClickDialog, GlobalDetectDialog,
    JsonActionDialog, JumpActionDialog, KeyActionDialog,
    RepeatClickDialog, CloseAppDialog,
    ModulePickerDialog,
    MouseMoveDialog, ScheduleDialog,
    OpenAppDialog, ScriptDirectoriesDialog, TemplateRegionFormDialog,
    TemplateRegionManagerDialog, WindowPicker,
    WorkflowBatchSettingsDialog, WorkflowRepeatDialog,
    DurationDialog, DurationVar, TIME_UNITS, Tooltip, edit_action, show_floating_notice,
)
from image_match import capture_bgr, find_template, find_template_in_image
from ocr import (
    find_expected_match, format_ocr_observation, matches_expected, recognize_image_with_boxes,
    recognize_region_with_boxes,
)
from models import (
    ACTION_ID_KEY, DEFAULT_MOUSE_MOVE_INTERVAL_MS, DEFAULT_RECORDED_SCREEN,
    DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
    END_CURRENT_SCRIPT_LABEL, NEXT_WORKFLOW_STEP_TARGET_ID, SCRIPT_START_TARGET_ID,
    MacroScript, Workflow, clone_actions_with_new_ids,
    ensure_action_ids, is_global_script,
    new_action_id,
)
from input_guard import FocusInputGuard
from player import JUMP_CURRENT_SCRIPT_LAST_RESULT, MacroPlayer
from recorder import MacroRecorder
from storage import (
    BASE_DIR, IMAGES_DIR, SCRIPTS_DIR, WORKFLOWS_DIR, available_script_path, backup_script,
    display_path, ensure_dirs, migrate_workflow_templates,
    load_app_settings, load_module_restart_default_row, load_script, load_workflow,
    registered_module_object, registered_template_region, resolve_path, save_app_settings,
    save_script, save_workflow,
    update_module_object,
)
from wininput import (
    WindowInfo, activate_window, enum_windows, get_cursor_pos, get_virtual_screen_rect,
    force_english_input, get_foreground_window_info, get_window_rect,
    is_current_process_window, is_window, is_window_process_foreground,
    make_window_no_activate, send_button, send_move_absolute, set_dark_titlebar, show_window,
    show_window_no_activate,
)


APP_NAME = "MacroFlow Studio"
APP_VERSION = "1.0.0"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "MacroFlowStudio"
# 全局检测触发点击后、继续执行前的默认等待时间 (ms)，保证点击来得及生效。
DEFAULT_GLOBAL_CLICK_DELAY_MS = 1000
MAX_TREE_ROWS = 20_000
BACKUP_INTERVAL_CHOICES = ("1h", "1天", "1周")
BACKUP_INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "1天": 24 * 60 * 60 * 1000,
    "1周": 7 * 24 * 60 * 60 * 1000,
}


def windows_startup_command() -> str:
    """Build the quoted command stored in the current-user Windows Run key."""
    parts = [str(Path(sys.executable).resolve())]
    if not getattr(sys, "frozen", False):
        parts.append(str(Path(__file__).resolve()))
    return subprocess.list2cmdline(parts)


def set_windows_startup(enabled: bool) -> None:
    """Enable or disable per-user startup without requiring administrator rights."""
    import winreg

    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, windows_startup_command())
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except FileNotFoundError:
        pass


def spawn_new_instance(args: list[str]):
    """Launch another MacroFlow instance.

    Reset every PyInstaller onefile extraction hint.  The Windows bootloader
    uses ``_MEIPASS2`` (not only ``_MEIPASS``) for inherited child processes;
    reusing that directory can leave Tcl/Tk looking for a deleted ``tcl_data``
    folder.  The reset flag makes the child extract a fresh private directory.
    """
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in {"_MEIPASS", "_MEIPASS2"}
    }
    if getattr(sys, "frozen", False):
        clean_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(args, cwd=str(BASE_DIR), env=clean_env)

FLOATING_NOTICE_POSITIONS = ("左上", "顶部居中", "右上", "左下", "底部居中", "右下")
SCRIPT_CATEGORY_VALUES = ("关卡", "关卡封装", "切换")
FLOATING_NOTICE_WIDTH = 360
FLOATING_NOTICE_HEIGHT = 68

COLOR_BG = "#0E1419"
COLOR_SIDEBAR = "#131B22"
COLOR_SURFACE = "#182129"
COLOR_SURFACE_ALT = "#1D2831"
COLOR_BORDER = "#2D3943"
COLOR_TEXT = "#E8EDF2"
COLOR_MUTED = "#94A1AD"
COLOR_BLUE = "#2F80ED"
COLOR_RED = "#E04444"
COLOR_GREEN = "#18A66F"

ACTION_ICONS = {
    "delay": "◷",
    "key": "⌨",
    "key_press": "⌨",
    "text": "T",
    "mouse_move": "↖",
    "mouse_button": "◉",
    "click": "◉",
    "repeat_click": "↻",
    "scroll": "↕",
    "image_match": "▣",
    "notice": "i",
    "comment": "≡",
    "script_ref": "⇄",
    "open_app": "▶",
    "close_app": "✕",
    "jump": "⇢",
    "jump_current_script_last": "⇥",
}


def floating_notice_xy(position: str, screen_width: int, screen_height: int,
                       width: int = FLOATING_NOTICE_WIDTH,
                       height: int = FLOATING_NOTICE_HEIGHT) -> tuple[int, int]:
    margin = 18
    left = margin
    center = max(margin, (int(screen_width) - width) // 2)
    right = max(margin, int(screen_width) - width - margin)
    top = margin
    bottom = max(margin, int(screen_height) - height - margin)
    positions = {
        "左上": (left, top), "顶部居中": (center, top), "右上": (right, top),
        "左下": (left, bottom), "底部居中": (center, bottom), "右下": (right, bottom),
    }
    return positions.get(position, positions["顶部居中"])


def action_kind_label(kind: str, label: str) -> str:
    return f"{ACTION_ICONS.get(kind, '•')}  {label}"


def disable_combobox_wheel_selection(root) -> None:
    """Prevent every ttk combobox from changing value via the mouse wheel.

    The popup list is a separate Listbox, so users can still scroll the opened
    option list; only an unfurled combobox under the pointer is protected.
    """
    stop = lambda _event: "break"
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_class("TCombobox", sequence, stop)


def workflow_script_name(value: str) -> str:
    """Show a workflow script as a clean name, never as scripts/name.json."""
    return Path(str(value).replace("\\", "/")).stem or str(value)


def script_category_key(label: str) -> str:
    return {
        "关卡": "level", "关卡封装": "level_pack",
        "切换": "switch",
    }.get(label, "level")


def parse_click_point(value: str) -> tuple[int, int] | None:
    """Parse a saved 'x,y' click position, returning None when invalid."""
    try:
        parts = [int(part.strip()) for part in str(value).split(",")]
        if len(parts) == 2:
            return parts[0], parts[1]
    except (TypeError, ValueError):
        pass
    return None


def parse_region(value: str) -> tuple[int, int, int, int] | None:
    """Parse a saved 'x,y,w,h' detection region; None means full screen."""
    try:
        parts = [int(part.strip()) for part in str(value).split(",")]
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return parts[0], parts[1], parts[2], parts[3]
    except (TypeError, ValueError):
        pass
    return None


def workflow_execution_progress(script_number: int, script_total: int, script_name: str,
                                repeat_total: int, repeat_current: int | None = None,
                                unlimited: bool = False) -> str:
    short_name = script_name if len(script_name) <= 18 else f"{script_name[:18]}…"
    if unlimited:
        return (f"工作流 {script_number}/{script_total} · {short_name}\n"
                f"不计次数 · 每次到达执行 1 次 · F12 停止")
    repeat_text = "正在准备" if repeat_current is None else f"当前第 {repeat_current}/{repeat_total} 次"
    return (f"工作流 {script_number}/{script_total} · {short_name}\n"
            f"共执行 {repeat_total} 次 · {repeat_text} · F12 停止")


def coordinate_scale_summary(source: dict | None, current: dict | None) -> str:
    source = source or DEFAULT_RECORDED_SCREEN
    current = current or DEFAULT_RECORDED_SCREEN
    source_size = f"{int(source.get('width', 1920))}×{int(source.get('height', 1080))}"
    current_size = f"{int(current.get('width', 1920))}×{int(current.get('height', 1080))}"
    suffix = "（1:1）" if source_size == current_size else "（自动缩放）"
    return f"坐标缩放  {source_size} → {current_size} {suffix}"


MODULE_AFTER_ACTION_LABELS = {
    "click_match": "点击识别区域",
    "click_custom": "点击自定义位置",
    "continue": "成功后继续",
    "second_match": "二次识别后点击",
    "run_actions": "成功后执行代码段",
}


def _module_row_result_summary(action: dict, action_rows: dict[str, int] | None,
                               number_mode: bool = False) -> str:
    def describe(behavior_key: str, target_key: str) -> str:
        behavior = str(action.get(behavior_key, "continue"))
        if behavior == "end_current_script":
            return "结束当前最里层脚本"
        if behavior == "jump":
            target_id = str(action.get(target_key, "")).strip()
            target_row = action_rows.get(target_id) if action_rows and target_id else None
            if target_row is not None:
                return f"跳到第 {target_row} 行"
            return "跳转目标已删除" if target_id else "跳转目标未设置"
        return "继续下一行"

    if number_mode:
        expected = action.get("expected_number")
        expected_text = "未设置" if expected is None else str(expected)
        return (
            f"比较 {expected_text} · 等于时{describe('on_found', 'found_jump_action_id')} / "
            f"不等于或未读取到时{describe('on_timeout', 'timeout_jump_action_id')}"
        )
    return (f"结果 成功后{describe('on_found', 'found_jump_action_id')} / "
            f"失败后{describe('on_timeout', 'timeout_jump_action_id')}")


def _module_ref_summary(action: dict, label: str,
                        action_rows: dict[str, int] | None = None) -> tuple[str, str, str]:
    """引用模块动作的实时摘要：运行时从对象仓库读属性渲染。"""
    kind = str(action.get("type", "unknown"))
    key = str(action.get("module_key") or action.get("template", ""))
    obj = registered_module_object(key)
    if obj is None:
        name = Path(key).name or "未设置"
        result_text = (
            f" · {_module_row_result_summary(action, action_rows, 'expected_number' in action)}"
            if kind == "image_match" else ""
        )
        return (
            action_kind_label(kind, label),
            f"引用模块 {name}（对象不存在，按内嵌参数执行）{result_text}",
            f"{int(action.get('delay_ms', 0))} ms",
        )
    name = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
    category = {
        "switch": "切换", "workflow_global": "工作流全局",
        "script_global": "脚本全局",
    }.get(obj.get("category"), "特殊")
    label = {
        "workflow_global": "工作流全局模块",
        "script_global": "脚本全局模块",
    }.get(obj.get("category"), label)
    after = str(obj.get("after_action", "click_match"))
    after_label = MODULE_AFTER_ACTION_LABELS.get(after, after)
    direct_mode = obj.get("recognize") == "none"
    number_mode = obj.get("recognize") == "number"
    blocking = (
        ("等待期望文字消失" if obj.get("recognize") == "text" else "等待模板图片消失")
        if obj.get("wait_text_absent") else
        "阻塞直到出现" if obj.get("blocking") else "等待超时后继续"
    )
    delay_a = int(obj.get("delay_ms", 0))
    region = obj.get("region", [0, 0, 0, 0])
    region_text = (
        ",".join(map(str, region))
        if len(region) == 4 and region[2] > 0 else "全屏"
    )
    if number_mode:
        detail = f"引用{category}模块 {name} · 读取数字 · 区域 {region_text} · {blocking}"
    else:
        detail = (
            f"引用{category}模块 {name} · "
            + ("无需识图 · " if direct_mode else f"区域 {region_text} · {blocking} · ")
            + f"延时 {delay_a} ms · 动作 {after_label}"
        )
    if not number_mode and after in ("click_match", "click_custom", "second_match"):
        detail += f" × {max(1, int(obj.get('click_count', 1)))} 下"
    if obj.get("category") in ("workflow_global", "script_global"):
        detail += (
            f" · 持续超过 {int(obj.get('hold_ms', 1000))} ms"
            if obj.get("hold_enabled", True) else " · 识别到立即执行"
        )
    if obj.get("category") == "script_global" and int(obj.get("start_delay_ms", 0)) > 0:
        detail += f" · {int(obj.get('start_delay_ms', 0))} ms 后开始识别"
    if not number_mode and (bool(obj.get("run_code_after_action", False)) or after == "run_actions"):
        detail += f" · 再执行代码段 {len(obj.get('on_success_actions') or [])} 项"
    if bool(obj.get("run_code_on_timeout", False)):
        detail += (
            f" · 未识别 {int(obj.get('not_found_timeout_ms', 3000))} ms 后"
            f"执行代码段 {len(obj.get('on_timeout_actions') or [])} 项"
        )
    if kind == "global_detect" and str(action.get("jump_action_id", "")).strip() == \
            NEXT_WORKFLOW_STEP_TARGET_ID:
        detail += " · 触发后结束当前脚本，执行工作流下一项"
    if kind == "image_match":
        detail += f" · {_module_row_result_summary(action, action_rows, number_mode)}"
    return action_kind_label(kind, label), detail, f"{int(action.get('delay_ms', 0))} ms"


def action_summary(action: dict, action_rows: dict[str, int] | None = None) -> tuple[str, str, str]:
    kind = action.get("type", "unknown")
    delay = f"{int(action.get('delay_ms', 1000 if kind == 'image_match' else 0))} ms"
    if kind == "delay":
        return action_kind_label(kind, "延时"), f"等待 {action.get('ms', 0)} ms", delay
    if kind == "key":
        state = "按下" if action.get("down") else "松开"
        return action_kind_label(kind, "键盘"), f"{state} {action.get('name', action.get('vk'))}", delay
    if kind == "key_press":
        return action_kind_label(kind, "键盘"), f"敲击 {action.get('name', action.get('vk'))}，按住 {action.get('hold_ms', 30)} ms", delay
    if kind == "text":
        text = str(action.get("text", "")).replace("\n", "↵")
        return action_kind_label(kind, "文本"), f"输入 “{text[:60]}”", delay
    if kind == "mouse_move":
        if action.get("mode") == "relative":
            return action_kind_label(kind, "转向"), f"ΔX {action.get('dx', 0)}，ΔY {action.get('dy', 0)}", delay
        return action_kind_label(kind, "移动"), f"X {action.get('x', 0)}，Y {action.get('y', 0)}", delay
    if kind == "mouse_button":
        state = "按下" if action.get("down") else "松开"
        return action_kind_label(kind, "点击"), f"{state} {action.get('button', 'left')} @ ({action.get('x', 0)}, {action.get('y', 0)})", delay
    if kind == "click":
        if action.get("pos_mode") == "current":
            return action_kind_label(kind, "点击"), f"{action.get('button', 'left')} @ 鼠标当前位置", delay
        return action_kind_label(kind, "点击"), f"{action.get('button', 'left')} @ ({action.get('x', 0)}, {action.get('y', 0)})", delay
    if kind == "repeat_click":
        return (
            action_kind_label(kind, "连续点击"),
            f"{action.get('button', 'left')} ×{action.get('count', 2)} 次 · "
            f"{action.get('interval_ms', 100)} ms 间隔 @ ({action.get('x', 0)}, {action.get('y', 0)})",
            delay,
        )
    if kind == "scroll":
        return action_kind_label(kind, "滚轮"), f"横向 {action.get('dx', 0)}，纵向 {action.get('dy', 0)} @ ({action.get('x', 0)}, {action.get('y', 0)})", delay
    if kind == "image_match":
        if action.get("module_ref"):
            return _module_ref_summary(action, "识图模块", action_rows)
        if action.get("on_found") == "jump":
            found_target_id = str(action.get("found_jump_action_id", "")).strip()
            found_target_row = action_rows.get(found_target_id) if action_rows and found_target_id else None
            if found_target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                operation = "找到后结束当前脚本，执行工作流下一项"
            elif found_target_row is not None:
                operation = f"找到后跳到第 {found_target_row} 行"
            elif found_target_id:
                operation = "找到后跳转目标已删除"
            else:
                operation = f"找到后跳到第 {max(1, int(action.get('found_jump_row', 1)))} 行（旧格式）"
        elif action.get("on_found", "click") == "click":
            operation = "找到后点击"
        else:
            operation = "等待出现"
        if action.get("click_target", "match") == "custom":
            point = action.get("click_point", [0, 0])
            click_target = f"自定义坐标 ({point[0]}, {point[1]})"
        else:
            click_target = "识图区域中心"
        found_delay = int(action.get("found_delay_ms", 0))
        timeout_ms = int(action.get("timeout_ms", 3000))
        wait_forever = bool(action.get("wait_forever", False))
        timeout_delay = int(action.get("timeout_delay_ms", 0))
        after_delay = int(action.get("after_delay_ms", 0))
        if wait_forever:
            timeout_text = "一直等待直到出现（不超时）"
        else:
            timeout_action = action.get("on_timeout", "continue")
            if timeout_action == "jump":
                target_id = str(action.get("timeout_jump_action_id", "")).strip()
                target_row = action_rows.get(target_id) if action_rows and target_id else None
                if target_row is not None:
                    timeout_text = f"超时跳到第 {target_row} 行目标动作"
                elif target_id:
                    timeout_text = "超时跳转目标已删除"
                else:
                    timeout_text = f"超时跳到第 {max(1, int(action.get('timeout_jump_row', 1)))} 行（旧格式）"
            elif timeout_action == "end_current_script":
                timeout_text = "超时结束当前脚本"
            elif timeout_action == "stop":
                timeout_text = "超时停止"
            else:
                timeout_text = "超时继续"
        timeout_delay_text = f" · 超时后等待 {timeout_delay} ms" if timeout_delay else ""
        after_delay_text = f" · 执行后等待 {after_delay} ms" if after_delay else ""
        notice = " · 浮动提醒" if action.get("show_result_notice") else ""
        fallback_name = str(action.get("fallback_template", "")).strip()
        fallback_text = ""
        if wait_forever and fallback_name:
            fallback_parts = [
                f"超 {int(action.get('fallback_switch_ms', 3000))} ms 换备用 {Path(fallback_name).name}",
                "点击" if action.get("fallback_click", True) else "不点击",
                "出现后退出识别"
                if action.get("fallback_on_match", "回到主模板的检测") == "直接退出识别"
                else "出现后回到主模板检测",
            ]
            fallback_text = " · " + "，".join(fallback_parts)
        timeout_label = "一直等待" if wait_forever else f"等待超时 {timeout_ms} ms"
        return action_kind_label(kind, "识图"), f"{operation} · {click_target} · {timeout_label} · {timeout_text}{timeout_delay_text}{fallback_text} · 成功后等待 {found_delay} ms · {Path(str(action.get('template', ''))).name} · 阈值 {float(action.get('threshold', .85)):.0%}{after_delay_text}{notice}", delay
    if kind == "text_ocr":
        expected = str(action.get("expected_text", "")).strip() or "任意文字"
        match_text = "等于" if action.get("match_mode", "contains") == "equals" else "包含"
        region_text = (
            ",".join(str(int(part)) for part in action.get("region", []))
            if len(action.get("region", [])) == 4
            else {"screen": "全屏", "window": "绑定窗口"}.get(
                str(action.get("region_mode", "screen")), "全屏"
            )
        )
        timeout_ms = int(action.get("timeout_ms", 3000))
        timeout_label = "只识别一次" if timeout_ms <= 0 else f"等待超时 {timeout_ms} ms"
        if action.get("on_found", "continue") == "jump":
            target_id = str(action.get("found_jump_action_id", "")).strip()
            target_row = action_rows.get(target_id) if action_rows and target_id else None
            if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                found_text = "找到后结束当前脚本，执行工作流下一项"
            elif target_row is not None:
                found_text = f"找到后跳到第 {target_row} 行"
            elif target_id:
                found_text = "找到后跳转目标已删除"
            else:
                found_text = f"找到后跳到第 {max(1, int(action.get('found_jump_row', 1)))} 行（旧格式）"
        else:
            found_text = "找到后继续"
        timeout_action = action.get("on_timeout", "continue")
        if timeout_action == "jump":
            target_id = str(action.get("timeout_jump_action_id", "")).strip()
            target_row = action_rows.get(target_id) if action_rows and target_id else None
            if target_row is not None:
                timeout_text = f"超时跳到第 {target_row} 行目标动作"
            elif target_id:
                timeout_text = "超时跳转目标已删除"
            else:
                timeout_text = f"超时跳到第 {max(1, int(action.get('timeout_jump_row', 1)))} 行（旧格式）"
        elif timeout_action == "stop":
            timeout_text = "超时停止"
        else:
            timeout_text = "超时继续"
        timeout_delay_text = f" · 超时后等待 {int(action.get('timeout_delay_ms', 0))} ms" \
            if int(action.get("timeout_delay_ms", 0)) else ""
        interval_text = f" · 间隔 {int(action.get('interval_ms', 500))} ms"
        return (
            action_kind_label(kind, "识别文字"),
            f"期望 {expected}（{match_text}） · 区域 {region_text} · "
            f"{found_text} · {timeout_label} · {timeout_text}{timeout_delay_text}"
            f"{interval_text} · 找到后等待 {int(action.get('found_delay_ms', 0))} ms"
            f"{' · 浮动提醒' if action.get('show_result_notice') else ''}",
            delay,
        )
    if kind == "global_detect":
        if action.get("module_ref"):
            return _module_ref_summary(action, "脚本全局模块", action_rows)
        template_name = Path(str(action.get("template", ""))).name or "未设置"
        region_text = (
            ",".join(str(int(part)) for part in action.get("region", []))
            if len(action.get("region", [])) == 4 else "全屏"
        )
        jump_row = action.get("jump_row")
        if jump_row or action.get("jump_action_id"):
            # 普通脚本内嵌全局模块行：跳转目标是脚本里的一行对象（按动作唯一标识引用）。
            target_id = str(action.get("jump_action_id", "")).strip()
            target_row = action_rows.get(target_id) if action_rows and target_id else None
            if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                jump_text = "触发后结束当前脚本，执行工作流下一项"
            elif target_row is not None:
                jump_text = f"触发后跳转到第 {target_row} 行"
            elif target_id:
                jump_text = "触发后跳转目标已删除"
            else:
                jump_text = f"触发后跳转到第 {max(1, int(jump_row or 1))} 行"
            detail = (
                f"脚本全局模块 {template_name} · 区域 {region_text} · 持续超过 "
                f"{int(action.get('hold_ms', 1000))} ms · {jump_text} · "
                f"阈值 {float(action.get('threshold', .85)):.0%}"
            )
            return action_kind_label(kind, "脚本全局模块"), detail, delay
        click_text = (
            ",".join(str(int(part)) for part in action.get("click_point", []))
            if len(action.get("click_point", [])) == 2 else "未设置"
        )
        detail = (
            f"全局检测 {template_name} · 区域 {region_text} · 持续超过 "
            f"{int(action.get('hold_ms', 1000))} ms · 点击 ({click_text}) · "
            f"点击后 {int(action.get('restart_delay_ms', DEFAULT_GLOBAL_CLICK_DELAY_MS))} ms 继续原工作流 · "
            f"阈值 {float(action.get('threshold', .85)):.0%}"
        )
        return action_kind_label(kind, "全局"), detail, delay
    if kind == "notice":
        duration = int(action.get("duration_ms", 3000))
        return action_kind_label(kind, "浮动提醒"), f"{str(action.get('text', ''))[:60]} · 显示 {duration} ms", delay
    if kind == "comment":
        return action_kind_label(kind, "注释"), str(action.get("text", "")), delay
    if kind == "script_ref":
        name = workflow_script_name(str(action.get("script", ""))) or "未设置"
        return action_kind_label(kind, "引用脚本"), f"执行 {name}（实时读取原脚本最新内容）", delay
    if kind == "open_app":
        name = Path(str(action.get("path", ""))).name or "未设置"
        args = str(action.get("args", "")).strip()
        detail = f"启动 {name}" + (f"（{args}）" if args else "")
        return action_kind_label(kind, "打开软件"), detail, delay
    if kind == "close_app":
        name = str(action.get("name", "")).strip() or "未设置"
        mode = "优雅优先" if action.get("graceful", True) else "强制"
        extras = [flag for flag, on in (
            ("进程树", action.get("tree")), ("管理员重试", action.get("elevated_retry")),
        ) if on]
        detail = f"结束 {name}（{mode}" + ("、" + "、".join(extras) if extras else "") + "）"
        return action_kind_label(kind, "关闭软件"), detail, delay
    if kind == "restart_workflow":
        try:
            row = max(0, int(action.get("restart_workflow_target_row", 0) or 0))
        except (TypeError, ValueError):
            row = 0
        detail = (
            f"重新执行工作流（跳转到第 {row} 行；独立运行时跳过）" if row
            else "重新执行工作流（默认跳转行；独立运行时跳过）"
        )
        return action_kind_label(kind, "特殊模块"), detail, delay
    if kind == "end_current_script":
        return (
            action_kind_label(kind, "特殊模块"),
            f"{END_CURRENT_SCRIPT_LABEL}（顶层脚本结束后由调用方继续）",
            delay,
        )
    if kind == "jump_current_script_last":
        return (
            action_kind_label(kind, "跳转"),
            "离开模块代码段，从当前脚本实际最后一行动作继续执行",
            delay,
        )
    if kind == "jump":
        target_id = str(action.get("jump_action_id", "")).strip()
        if target_id == SCRIPT_START_TARGET_ID:
            target_text = "脚本开头（第 1 行）"
        elif target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
            target_text = "脚本结尾（立即结束）"
        else:
            target_row = action_rows.get(target_id) if action_rows and target_id else None
            if target_row is not None:
                target_text = f"第 {target_row} 行目标动作"
            elif target_id:
                target_text = "目标动作已删除"
            else:
                target_text = f"第 {max(1, int(action.get('jump_row', 1)))} 行（旧格式）"
        condition = " · 仅工作流第 2 次及以后" if action.get("workflow_repeat_at_least_2") else ""
        return action_kind_label(kind, "跳转"), f"跳转到{target_text}{condition}", delay
    return action_kind_label(kind, kind), json.dumps(action, ensure_ascii=False)[:100], delay


def recorded_action_description(action: dict) -> str:
    """Short, concrete wording for the live recording panel."""
    kind = action.get("type", "unknown")
    if kind == "mouse_move":
        if action.get("mode") == "relative":
            return f"游戏转向：ΔX={action.get('dx', 0)}，ΔY={action.get('dy', 0)}（相对轨迹）"
        return f"鼠标移动到：({action.get('x', 0)}, {action.get('y', 0)})（桌面坐标）"
    if kind == "mouse_button":
        button_names = {"left": "左键", "right": "右键", "middle": "中键"}
        button = button_names.get(str(action.get("button", "left")), str(action.get("button", "left")))
        state = "按下" if action.get("down") else "松开"
        return f"{button}{state}：({action.get('x', 0)}, {action.get('y', 0)})"
    if kind == "scroll":
        return (f"滚轮：横向={action.get('dx', 0)}，纵向={action.get('dy', 0)}，"
                f"位置=({action.get('x', 0)}, {action.get('y', 0)})")
    if kind == "key":
        state = "按下" if action.get("down") else "松开"
        return f"键盘{state}：{action.get('name', action.get('vk', '未知'))}"
    if kind == "key_press":
        return f"敲击按键：{action.get('name', action.get('vk', '未知'))}"
    _, detail, _ = action_summary(action)
    return detail


ACTION_SHORT_LABELS = {
    "delay": "延时", "key": "键盘", "key_press": "键盘", "text": "文本",
    "mouse_move": "鼠标移动", "mouse_button": "鼠标按键", "click": "点击",
    "repeat_click": "连续点击", "scroll": "滚轮", "image_match": "识图",
    "text_ocr": "识别文字", "notice": "浮动提醒", "comment": "注释", "script_ref": "引用脚本",
    "open_app": "打开软件", "close_app": "关闭软件", "global_detect": "脚本全局模块",
    "restart_workflow": "特殊模块", "end_current_script": "特殊模块",
    "jump_current_script_last": "跳转", "jump": "跳转",
    "activate_window": "前置窗口",
}


def action_short_text(action: dict) -> str:
    """一行紧凑、具体的动作描述，如 键盘：A、识图：g.png、延时：500 ms。"""
    kind = str(action.get("type", "unknown"))
    label = ACTION_SHORT_LABELS.get(kind, kind)
    if kind in {"key", "key_press"}:
        return f"{label}：{action.get('name', action.get('vk', '未知'))}"
    if kind == "image_match":
        if action.get("module_ref"):
            key = str(action.get("module_key") or action.get("template", ""))
            obj = registered_module_object(key)
            name = str(obj.get("name") or Path(key.replace("\\", "/")).stem) if obj else Path(key.replace("\\", "/")).stem
            if obj and obj.get("recognize") == "number":
                return f"读取数字：模块 {name or '未设置'}"
            return f"{label}：模块 {name or '未设置'}"
        return f"{label}：{Path(str(action.get('template', ''))).name or '未设置'}"
    if kind == "text_ocr":
        return f"{label}：{str(action.get('expected_text', '')).strip()[:18] or '任意文字'}"
    if kind == "global_detect" and action.get("module_ref"):
        key = str(action.get("module_key") or action.get("template", ""))
        obj = registered_module_object(key)
        name = str(obj.get("name") or Path(key.replace("\\", "/")).stem) if obj else Path(key.replace("\\", "/")).stem
        return f"{label}：模块 {name or '未设置'}"
    if kind == "restart_workflow":
        return "特殊模块：重新执行工作流"
    if kind == "end_current_script":
        return f"特殊模块：{END_CURRENT_SCRIPT_LABEL}"
    if kind == "jump_current_script_last":
        return "跳转：当前脚本最后一行"
    if kind == "jump":
        target_id = str(action.get("jump_action_id", "")).strip()
        if target_id == SCRIPT_START_TARGET_ID:
            return "跳转：脚本开头"
        if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
            return "跳转：脚本结尾"
        return "跳转：指定动作"
    if kind == "activate_window":
        signature = action.get("window") or {}
        return f"前置窗口：{signature.get('title', '未设置')}"
    if kind in {"text", "notice", "comment"}:
        content = str(action.get("text", "")).replace("\n", " ")[:18]
        return f"{label}：{content}" if content else label
    if kind == "delay":
        return f"{label}：{int(action.get('ms', 0))} ms"
    if kind == "open_app":
        return f"{label}：{Path(str(action.get('path', ''))).name or '未设置'}"
    if kind == "close_app":
        return f"{label}：{action.get('name', '未设置')}"
    if kind == "script_ref":
        return f"{label}：{Path(str(action.get('script', ''))).stem or '未设置'}"
    if kind == "repeat_click":
        return f"{label}：{int(action.get('count', 0))} 次"
    if kind == "mouse_move" and action.get("mode") == "relative":
        return f"{label}（相对）"
    return label


class MacroFlowApp:
    def __init__(self):
        ensure_dirs()
        migrate_workflow_templates()
        started_at = datetime.now()
        self.logs_dir = BASE_DIR / "logs"
        session_logs_dir = self.logs_dir / started_at.strftime("%Y-%m-%d")
        session_logs_dir.mkdir(parents=True, exist_ok=True)
        self.session_log_path = session_logs_dir / (
            f"MacroFlow_{started_at.strftime('%H-%M-%S-%f')[:-3]}_{os.getpid()}.log"
        )
        self.log_file_lock = threading.Lock()
        self.root = ttk.Window(themename="darkly")
        self.root._macroflow_app = self
        self.root.title(f"{APP_NAME}  {APP_VERSION}")
        self.root.geometry("1700x940")
        self.root.minsize(1480, 780)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 11))
        disable_combobox_wheel_selection(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._configure_dark_theme()
        self.root.update_idletasks()
        set_dark_titlebar(self.root.winfo_id())

        self.app_settings = load_app_settings()
        saved_mini_position = self.app_settings.get("execution_mini_position")
        try:
            self.execution_mini_position = (
                [int(saved_mini_position[0]), int(saved_mini_position[1])]
                if isinstance(saved_mini_position, (list, tuple)) and len(saved_mini_position) == 2
                else []
            )
        except (TypeError, ValueError):
            self.execution_mini_position = []
        self.execution_mini_position_editor = None
        draft_signature = self.app_settings.get("activation_window_draft")
        self.activation_draft_signature: dict[str, str] | None = (
            {
                "title": str(draft_signature.get("title", "")),
                "class_name": str(draft_signature.get("class_name", "")),
                "process_path": str(draft_signature.get("process_path", "")),
            }
            if isinstance(draft_signature, dict) and draft_signature.get("title")
            else None
        )
        self.activation_draft_enabled = bool(
            self.app_settings.get("activation_window_draft_enabled", False)
        )
        self.script = self._blank_script_with_activation_draft()
        self.action_undo_stack: list[list[dict]] = []
        self.action_redo_stack: list[list[dict]] = []
        self.undo_open_stack: list[dict] = []
        self.script_path: Path | None = None
        self.script_requires_new_file = False
        saved_workflow = self.app_settings.get("workflow_draft")
        self.workflow = Workflow.from_dict(saved_workflow) if isinstance(saved_workflow, dict) else Workflow()
        saved_workflow_path = str(self.app_settings.get("workflow_path", "")).strip()
        self.workflow_path: Path | None = resolve_path(saved_workflow_path) if saved_workflow_path else None
        self.workflow_drag_index: int | None = None
        self.workflow_was_dragged = False
        self.workflow_delete_undo_stack: list[tuple[int, dict]] = []
        self.global_delete_undo_stack: list[tuple[int, dict]] = []
        self.workflow_draft_after_id = None
        self.workflow_insert_position_var = tk.StringVar(value="below")
        self.startup_new_script = "--new-script" in sys.argv
        self.startup_open_script = None
        if "--open-script" in sys.argv:
            try:
                self.startup_open_script = sys.argv[sys.argv.index("--open-script") + 1]
            except IndexError:
                pass
        self.startup_edit_module = None
        if "--edit-module" in sys.argv:
            try:
                self.startup_edit_module = sys.argv[sys.argv.index("--edit-module") + 1]
            except IndexError:
                pass
        self.bound_window: WindowInfo | None = None
        saved_binding = self.app_settings.get("bound_window")
        self.saved_window_signature: dict[str, str] | None = (
            {
                "title": str(saved_binding.get("title", "")),
                "class_name": str(saved_binding.get("class_name", "")),
                "process_path": str(saved_binding.get("process_path", "")),
                "window_rect": tuple(saved_binding.get("window_rect", (0, 0, 0, 0))),
                "client_size": tuple(saved_binding.get("client_size", (0, 0))),
            }
            if isinstance(saved_binding, dict) and saved_binding.get("title")
            else None
        )
        # 执行时仍以脚本自己的设置为准；draft 独立保存最近选择，不能在
        # 被动打开其他脚本时被当前脚本的空设置覆盖。
        self.activation_window: WindowInfo | None = None
        self.saved_activation_signature: dict[str, str] | None = None
        self.recorder = MacroRecorder(self._record_action_callback)
        self.player = MacroPlayer(
            self._player_status_callback,
            on_notice=self._player_notice_callback,
            on_global_detect_request=self._activate_global_detect_from_config,
            on_restart_workflow_request=self._on_restart_workflow_request,
            on_log=lambda text: self._ui(self._log, text),
            on_script_scope_enter=self._enter_script_global_scope,
            on_script_scope_exit=self._exit_script_global_scope,
            on_target_window_request=lambda: self._bound_hwnd(update_display=False),
        )
        self.input_guard = FocusInputGuard(lambda: self._ui(self.stop_all))
        self.workflow_stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.current_workflow_step_index: int | None = None
        self.current_workflow_repeat_index: int = 0
        self.current_workflow_action_index: int = 0
        # 全局检测监控：工作流全局模块按 step_id、脚本全局动作按 action_id
        # 分别拥有独立监控与重触发锁，互不替换、同时运行。
        self.global_detect_monitors: dict[str, dict] = {}
        # 所有全局模块共用一个全屏截图生产线程。各监控只在内存中按自己的
        # 区域和阈值匹配，避免同一轮为每个模块分别调用屏幕捕获。
        self.global_capture_condition = threading.Condition()
        self.global_capture_thread: threading.Thread | None = None
        self.global_capture_generation = 0
        self.global_capture_screen = None
        self.global_capture_origin = (0, 0)
        # 触发后跨工作流断点恢复保留锁定；新监控确认图片消失后才重新启用。
        self.global_detect_rearm_locks: set[str] = set()
        self.global_detect_module_running = False
        self.global_module_workflow_context = False
        self.global_detect_trigger_count = 0
        self.global_detect_pending_restart = False
        self.global_detect_restart_polls = 0
        self.global_detect_resume_index: int | None = None
        self.global_detect_resume_repeat: int = 0
        self.global_detect_resume_action: int = 0
        self.global_detect_end_current_script = False
        self.global_detect_advance_workflow_step = False
        # 模块代码段"跳转到当前脚本最后一行"：中断时正处于被引用脚本内部，
        # 已把该脚本最后一行动作播放完，恢复时从外层脚本的引用动作之后继续。
        self.global_detect_jumped_referenced_last = False
        # 单独执行（F9）全局脚本时的语句体回放参数：触发条件满足后重新播放语句体。
        self.standalone_global_replay: dict | None = None
        # 普通脚本内嵌全局模块行（触发后跳转行）：跳转播放期间 worker 等待其结束。
        self.standalone_jump_pending = False
        self.standalone_jump_done = threading.Event()
        # 特殊模块「重新执行工作流」：标志 + 目标行（1 基，运行时按
        # 动作 → 模块 → 全局默认解析）+ 工作流各步次数快照（重启时恢复，
        # 防止已消费的行在重跑中被跳过）。
        self.workflow_restart_requested = False
        self.workflow_restart_target_row = 1
        self.workflow_repeats_snapshot: dict[int, tuple[int, bool]] | None = None
        # 正在播放的全局模块代码段所属模块对象（解析模块级重启跳转行用）。
        self.global_detect_active_module: dict | None = None
        self.workflow_test_mode_active = False
        self.dirty = False
        self.mini_window: tk.Toplevel | None = None
        self.mini_elapsed_var = tk.StringVar(value="00:00")
        self.mini_count_var = tk.StringVar(value="0 个动作")
        self.mini_context_var = tk.StringVar(value="")
        self.mini_window_var = tk.StringVar(value="当前窗口：未知")
        self.mini_mode = ""
        self.mini_steps_text: tk.Text | None = None
        self.mini_update_after_id = None
        self.mini_binding_label = None
        self.bind_label_widget = None
        self.record_started_at = 0.0
        self.execution_started_at = 0.0
        self.execution_progress_text = ""
        self.execution_focus_requested = False
        self.execution_notice_window: tk.Toplevel | None = None
        self.execution_notice_label = None
        self.execution_notice_after_id = None
        self.root._macroflow_notice_callback = self._show_execution_notice
        self.recording_capture_mode = ""
        self.recording_screen: dict[str, int] | None = None
        self.cursor_tracking = False
        self.cursor_tracking_after_id = None
        self.cursor_tracking_mini: tk.Toplevel | None = None
        self.main_hidden_for_cursor_tracking = False
        self.tray_icon: pystray.Icon | None = None
        self.main_hidden_to_tray = False
        self.main_hidden_for_recording = False
        self.main_hidden_for_execution = False
        self.execution_should_remain_in_tray = False
        self.backup_after_id = None
        self.backup_running = False
        self.exiting = False

        self._create_variables()
        self._build_ui()
        self._restore_saved_window_binding()
        self._sync_activation_ui_from_script()
        self.rebuild_action_tree()
        self.rebuild_workflow_tree()
        self._start_hotkeys()
        self.refresh_script_files()
        self.refresh_workflow_files()
        self._log("应用已就绪。F8 录制/停止，F9 执行当前脚本，F12 紧急停止。")
        self._set_status("就绪", "success")
        self._sync_windows_startup(log_errors=True)
        self._schedule_timed_backup()
        explicit_editor_start = bool(
            self.startup_new_script or self.startup_open_script or self.startup_edit_module
        )
        if self.start_minimized_to_tray_var.get() and not explicit_editor_start:
            self.root.after(120, self._hide_main_to_tray)
        else:
            # Some Windows launchers propagate SW_HIDE to the first created window.
            self.root.after(120, self._ensure_startup_visible)
        self.root.after_idle(self._start_execution_prewarm)
        if self.startup_open_script:
            self.root.after(300, lambda: self._load_startup_script(resolve_path(self.startup_open_script)))
        if self.startup_edit_module:
            self.root.after(300, lambda: self._open_module_object_editor(self.startup_edit_module))
        if self.startup_run_workflow_var.get() and not explicit_editor_start:
            self.root.after(700, self._run_configured_startup_workflow)

    def _configure_dark_theme(self):
        style = self.root.style
        self.root.configure(background=COLOR_BG)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Workspace.TFrame", background=COLOR_BG)
        style.configure("Sidebar.TFrame", background=COLOR_SIDEBAR)
        style.configure("Surface.TFrame", background=COLOR_SURFACE)
        style.configure("Toolbar.TFrame", background=COLOR_BG)
        style.configure("Status.TFrame", background=COLOR_BG)

        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("Sidebar.TLabel", background=COLOR_SIDEBAR, foreground=COLOR_TEXT)
        style.configure("Brand.TLabel", background=COLOR_SIDEBAR, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Muted.TLabel", background=COLOR_BG, foreground=COLOR_MUTED)
        style.configure("SidebarMuted.TLabel", background=COLOR_SIDEBAR, foreground=COLOR_MUTED)
        style.configure("Section.TLabel", background=COLOR_SIDEBAR, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("SidebarSection.TLabel", background=COLOR_SIDEBAR, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("PageTitle.TLabel", background=COLOR_BG, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Empty.TLabel", background=COLOR_SURFACE, foreground=COLOR_MUTED,
                        font=("Microsoft YaHei UI", 11))
        style.configure("StatusText.TLabel", background=COLOR_BG, foreground=COLOR_GREEN,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("MiniTitle.TLabel", background=COLOR_SURFACE, foreground=COLOR_RED,
                        font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("MiniText.TLabel", background=COLOR_SURFACE, foreground=COLOR_MUTED)
        style.configure("MiniTime.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT,
                        font=("Consolas", 13, "bold"))
        style.configure("MiniWarning.TLabel", background=COLOR_SURFACE, foreground=COLOR_RED,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("GlobalMarker.TLabel", background=COLOR_BG, foreground="#7BC96F",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("GlobalTrigger.TFrame", background=COLOR_SURFACE)
        style.configure("GlobalTriggerTitle.TLabel", background=COLOR_SURFACE,
                        foreground=COLOR_TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("GlobalTriggerSummary.TLabel", background=COLOR_SURFACE,
                        foreground=COLOR_TEXT)

        style.configure("TEntry", fieldbackground=COLOR_SURFACE_ALT, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        insertcolor=COLOR_TEXT, padding=8)
        style.configure("TSpinbox", fieldbackground=COLOR_SURFACE_ALT, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, arrowcolor=COLOR_MUTED, padding=6)
        style.configure("TCombobox", fieldbackground=COLOR_SURFACE_ALT, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, arrowcolor=COLOR_MUTED, padding=6)
        style.configure("TSeparator", background=COLOR_BORDER)

        style.configure("TNotebook", background=COLOR_BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=COLOR_BG, foreground=COLOR_MUTED,
                        borderwidth=0, padding=(20, 13), font=("Microsoft YaHei UI", 11))
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_BG), ("active", COLOR_SURFACE)],
                  foreground=[("selected", COLOR_TEXT), ("active", COLOR_TEXT)],
                  lightcolor=[("selected", COLOR_BLUE)], bordercolor=[("selected", COLOR_BLUE)])

        style.configure("Treeview", background=COLOR_SURFACE, fieldbackground=COLOR_SURFACE,
                        foreground=COLOR_TEXT, bordercolor=COLOR_BORDER, rowheight=42,
                        font=("Microsoft YaHei UI", 11))
        style.configure("Treeview.Heading", background=COLOR_SURFACE_ALT, foreground=COLOR_MUTED,
                        bordercolor=COLOR_BORDER, relief="flat", padding=(9, 10),
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#244D78")],
                  foreground=[("selected", "#FFFFFF")])
        style.configure("Workflow.Treeview")
        style.map("Workflow.Treeview", background=[("selected", "#244D78")],
                  foreground=[("selected", "#FFFFFF")])
        style.map("Treeview.Heading", background=[("active", "#24313B")])
        style.configure("Ghost.TButton", background=COLOR_BG, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        relief="solid", borderwidth=1, padding=(13, 8),
                        font=("Microsoft YaHei UI", 10))
        style.map("Ghost.TButton",
                  background=[("active", COLOR_SURFACE_ALT), ("pressed", "#263541")],
                  foreground=[("disabled", "#58646F"), ("active", "#FFFFFF")],
                  bordercolor=[("active", "#516170")])
        style.configure("CompactGhost.TButton", background=COLOR_BG, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        relief="solid", borderwidth=1, padding=(10, 7),
                        font=("Microsoft YaHei UI", 10))
        style.map("CompactGhost.TButton",
                  background=[("active", COLOR_SURFACE_ALT), ("pressed", "#263541")],
                  foreground=[("disabled", "#58646F"), ("active", "#FFFFFF")],
                  bordercolor=[("active", "#516170")])
        style.configure("ToolGroupTitle.TLabel", background=COLOR_BG, foreground=COLOR_MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("SectionCard.TLabelframe", background=COLOR_SURFACE,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER,
                        darkcolor=COLOR_BORDER, relief="solid", borderwidth=1)
        style.configure("SectionCard.TLabelframe.Label", background=COLOR_SURFACE,
                        foreground=COLOR_TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("ScriptTool.TButton", background=COLOR_BG, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        relief="solid", borderwidth=1, padding=(5, 7),
                        font=("Microsoft YaHei UI", 9))
        style.map("ScriptTool.TButton",
                  background=[("active", COLOR_SURFACE_ALT), ("pressed", "#263541")],
                  foreground=[("disabled", "#58646F"), ("active", "#FFFFFF")],
                  bordercolor=[("active", "#516170")])
        style.configure("AccentScriptTool.TButton", background="#122D48", foreground="#8FC4FF",
                        bordercolor=COLOR_BLUE, lightcolor=COLOR_BLUE, darkcolor=COLOR_BLUE,
                        relief="solid", borderwidth=1, padding=(5, 7),
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.map("AccentScriptTool.TButton",
                  background=[("active", "#18426A"), ("pressed", "#205582")],
                  foreground=[("disabled", "#58646F"), ("active", "#FFFFFF")])
        style.configure("DangerScriptTool.TButton", background=COLOR_BG, foreground="#FF6B6B",
                        bordercolor="#A93636", lightcolor="#A93636", darkcolor="#A93636",
                        relief="solid", borderwidth=1, padding=(5, 7),
                        font=("Microsoft YaHei UI", 9))
        style.map("DangerScriptTool.TButton",
                  background=[("active", "#3B1E22"), ("pressed", "#522329")],
                  foreground=[("active", "#FFFFFF")], bordercolor=[("active", COLOR_RED)])
        style.configure("SidebarGhost.TButton", background=COLOR_SIDEBAR, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        relief="solid", borderwidth=1, padding=(12, 7),
                        font=("Microsoft YaHei UI", 10))
        style.map("SidebarGhost.TButton",
                  background=[("active", COLOR_SURFACE_ALT), ("pressed", "#263541")],
                  foreground=[("disabled", "#58646F"), ("active", "#FFFFFF")],
                  bordercolor=[("active", "#516170")])
        self.root.option_add("*TCombobox*Listbox*Background", COLOR_SURFACE_ALT)
        self.root.option_add("*TCombobox*Listbox*Foreground", COLOR_TEXT)

    def _create_variables(self):
        # 空白脚本的鼠标轨迹间隔固定从 20 ms 开始。该值属于脚本，不能从
        # 旧版 app_settings 或上一个脚本继承成 100 ms。
        interval = DEFAULT_MOUSE_MOVE_INTERVAL_MS
        try:
            repeat = max(1, min(999999, int(self.app_settings.get("repeat", 1))))
        except (TypeError, ValueError):
            repeat = 1
        self.script_name_var = tk.StringVar(value=self.script.name)
        # Recording is intentionally a single smart mode. Keep the variable so
        # older script/settings files remain compatible without exposing three
        # overlapping choices in the UI.
        self.record_mode_var = tk.StringVar(value="auto")
        self.interval_var = DurationVar(value=interval)
        self.repeat_var = tk.IntVar(value=repeat)
        self.bind_label_var = tk.StringVar(value="未绑定窗口")
        self.activation_enabled_var = tk.BooleanVar(value=False)
        self.activation_label_var = tk.StringVar(value="跟随目标窗口")
        self.cursor_position_var = tk.StringVar(value="光标坐标：尚未读取")
        self.cursor_tracking_mini_var = tk.StringVar(value="X: 0    Y: 0")
        self.status_var = tk.StringVar(value="就绪")
        self.coordinate_scale_var = tk.StringVar(value=coordinate_scale_summary(
            self.script.settings.get("recorded_screen"), get_virtual_screen_rect(),
        ))
        self.record_count_var = tk.StringVar(value="0 个动作")
        self.workflow_name_var = tk.StringVar(value=self.workflow.name)
        self.workflow_start_var = tk.StringVar(value=self.workflow.start_at)
        self.workflow_start_delay_enabled_var = tk.BooleanVar(
            value=bool(self.workflow.start_delay_enabled),
        )
        self.workflow_start_delay_seconds_var = DurationVar(
            value=int(self.workflow.start_delay_seconds) * 1000,
        )
        self.workflow_test_mode_var = tk.BooleanVar(value=False)
        self.sound_enabled_var = tk.BooleanVar(value=bool(self.app_settings.get("sound_enabled", True)))
        self.mini_window_enabled_var = tk.BooleanVar(value=bool(self.app_settings.get("mini_window_enabled", True)))
        self.execution_mini_enabled_var = tk.BooleanVar(
            value=bool(self.app_settings.get("execution_mini_enabled", True)),
        )
        self.focus_mode_enabled_var = tk.BooleanVar(value=bool(self.app_settings.get("focus_mode_enabled", False)))
        self.activate_target_enabled_var = tk.BooleanVar(value=bool(self.app_settings.get("activate_target_enabled", True)))
        notice_position = str(self.app_settings.get("floating_notice_position", "顶部居中"))
        if notice_position not in FLOATING_NOTICE_POSITIONS:
            notice_position = "顶部居中"
        self.floating_notice_position_var = tk.StringVar(value=notice_position)
        close_action = self.app_settings.get("close_action", "exit")
        self.close_action_var = tk.StringVar(value=close_action if close_action in {"exit", "tray"} else "exit")
        self.timed_backup_enabled_var = tk.BooleanVar(
            value=bool(self.app_settings.get("timed_backup_enabled", False)),
        )
        backup_interval = str(self.app_settings.get("backup_interval", "1h"))
        self.backup_interval_var = tk.StringVar(
            value=backup_interval if backup_interval in BACKUP_INTERVAL_CHOICES else "1h",
        )
        self.windows_startup_enabled_var = tk.BooleanVar(
            value=bool(self.app_settings.get("windows_startup_enabled", False)),
        )
        self.start_minimized_to_tray_var = tk.BooleanVar(
            value=bool(self.app_settings.get("start_minimized_to_tray", False)),
        )
        self.startup_run_workflow_var = tk.BooleanVar(
            value=bool(self.app_settings.get("startup_run_workflow", False)),
        )
        self.startup_workflow_path_var = tk.StringVar(
            value=str(self.app_settings.get("startup_workflow_path", "")),
        )
        self.level_scripts_dir_var = tk.StringVar(
            value=str(self.app_settings.get("level_scripts_dir", "scripts/关卡")),
        )
        self.level_pack_scripts_dir_var = tk.StringVar(
            value=str(self.app_settings.get("level_pack_scripts_dir", "scripts/关卡封装")),
        )
        self.switch_scripts_dir_var = tk.StringVar(
            value=str(self.app_settings.get("switch_scripts_dir", "scripts/切换")),
        )
        self.script_category_var = tk.StringVar(value="关卡")
        self.insert_position_var = tk.StringVar(value="below")

    def _build_ui(self):
        root_frame = ttk.Frame(self.root, style="Workspace.TFrame")
        root_frame.pack(fill="both", expand=True)
        status = ttk.Frame(root_frame, padding=(16, 8, 16, 8), style="Status.TFrame")
        status.pack(side="bottom", fill="x")
        self.status_dot = ttk.Label(status, textvariable=self.status_var, style="StatusText.TLabel")
        self.status_dot.pack(side="left")
        ttk.Label(status, textvariable=self.coordinate_scale_var, style="Muted.TLabel").pack(side="right")

        content = ttk.Frame(root_frame, style="Workspace.TFrame")
        content.pack(fill="both", expand=True)
        self._build_sidebar(content)
        main = ttk.Frame(content, padding=(22, 18, 22, 10), style="Workspace.TFrame")
        main.pack(side="left", fill="both", expand=True)
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill="both", expand=True)
        self.script_tab = ttk.Frame(self.notebook)
        self.workflow_tab = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.script_tab, text="脚本编辑")
        self.notebook.add(self.workflow_tab, text="工作流")
        self.notebook.add(self.log_tab, text="运行日志")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_script_tab()
        self._build_workflow_tab()
        self._build_log_tab()

    def _on_tab_changed(self, _event=None):
        """Refresh workflow displays when the workflow tab is shown."""
        if getattr(self, "workflow_tree", None) is None:
            return
        try:
            if self.notebook.index(self.notebook.select()) == 1:
                self.rebuild_workflow_tree()
        except (tk.TclError, ValueError):
            pass

    def _build_sidebar(self, parent):
        # Keep the configuration panel usable on shorter screens. The inner
        # frame keeps its existing layout while the canvas provides vertical
        # scrolling for all controls.
        sidebar_shell = ttk.Frame(parent, width=380, style="Sidebar.TFrame")
        sidebar_shell.pack(side="left", fill="y")
        sidebar_shell.pack_propagate(False)
        sidebar_canvas = tk.Canvas(
            sidebar_shell, background=COLOR_SIDEBAR, highlightthickness=0,
            borderwidth=0, width=358,
        )
        sidebar_scrollbar = ttk.Scrollbar(sidebar_shell, orient="vertical", command=sidebar_canvas.yview)
        sidebar_canvas.configure(yscrollcommand=sidebar_scrollbar.set)
        sidebar_canvas.pack(side="left", fill="both", expand=True)
        sidebar_scrollbar.pack(side="right", fill="y")
        sidebar = ttk.Frame(sidebar_canvas, width=358, padding=(20, 22), style="Sidebar.TFrame")
        sidebar_window = sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")

        def update_sidebar_scrollregion(_event=None):
            sidebar_canvas.configure(scrollregion=sidebar_canvas.bbox("all"))

        def resize_sidebar_content(event):
            sidebar_canvas.itemconfigure(sidebar_window, width=event.width)

        sidebar.bind("<Configure>", update_sidebar_scrollregion)
        sidebar_canvas.bind("<Configure>", resize_sidebar_content)

        def scroll_sidebar(event):
            if event.delta:
                sidebar_canvas.yview_scroll(-int(event.delta / 120), "units")

        def bind_sidebar_wheel(widget):
            widget.bind("<MouseWheel>", scroll_sidebar, add="+")
            for child in widget.winfo_children():
                bind_sidebar_wheel(child)
        ttk.Label(sidebar, text="MacroFlow", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text="录制、识图与自动工作流", style="SidebarMuted.TLabel").pack(anchor="w", pady=(2, 22))

        self.record_button = ttk.Button(sidebar, text="开始录制    F8", command=lambda: self.toggle_record(from_ui=True), bootstyle="danger")
        self.record_button.pack(fill="x", ipady=7)
        self.run_button = ttk.Button(sidebar, text="执行当前脚本    F9", command=self.run_current_script, bootstyle="success")
        self.run_button.pack(fill="x", ipady=7, pady=(10, 0))
        ttk.Button(sidebar, text="紧急停止    F12", command=self.stop_all, style="SidebarGhost.TButton").pack(fill="x", pady=(10, 20))

        settings_title = ttk.Frame(sidebar, style="Sidebar.TFrame")
        settings_title.pack(fill="x", pady=(0, 8))
        ttk.Label(settings_title, text="录制设置", style="SidebarSection.TLabel").pack(side="left")
        ttk.Button(settings_title, text="测试声音", command=self.test_sound,
                   style="SidebarGhost.TButton", width=7).pack(side="right")
        ttk.Button(settings_title, text="保存配置", command=self.save_sidebar_config,
                   style="SidebarGhost.TButton", width=7).pack(side="right", padx=(0, 4))
        option_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        option_row.pack(fill="x", pady=(0, 15))
        check_style = {
            "anchor": "w", "background": COLOR_SIDEBAR, "activebackground": COLOR_SIDEBAR,
            "foreground": COLOR_TEXT, "activeforeground": COLOR_TEXT, "selectcolor": COLOR_SURFACE_ALT,
            "highlightthickness": 0, "borderwidth": 0, "font": ("Microsoft YaHei UI", 11),
        }
        tk.Checkbutton(option_row, text="快捷键提示音", variable=self.sound_enabled_var,
                       command=self._settings_changed, **check_style).pack(anchor="w", pady=2)
        tk.Checkbutton(option_row, text="录制时显示悬浮小窗", variable=self.mini_window_enabled_var,
                       command=self._settings_changed, **check_style).pack(anchor="w", pady=2)
        tk.Checkbutton(option_row, text="执行时显示悬浮小窗", variable=self.execution_mini_enabled_var,
                       command=self._settings_changed, **check_style).pack(anchor="w", pady=2)
        ttk.Button(
            option_row, text="调节录制/执行小窗位置（显示边界）",
            command=self._adjust_execution_mini_position,
            style="SidebarGhost.TButton",
        ).pack(anchor="w", fill="x", pady=(2, 6))
        ttk.Label(option_row, text="点击关闭按钮时", style="Section.TLabel").pack(anchor="w", pady=(9, 3))
        close_row = ttk.Frame(option_row, style="Sidebar.TFrame")
        close_row.pack(fill="x")
        ttk.Radiobutton(close_row, text="直接退出", value="exit", variable=self.close_action_var,
                        command=self._settings_changed).pack(side="left")
        ttk.Radiobutton(close_row, text="隐藏到托盘", value="tray", variable=self.close_action_var,
                        command=self._settings_changed).pack(side="left", padx=(12, 0))

        record_mode_title = ttk.Frame(sidebar, style="Sidebar.TFrame")
        record_mode_title.pack(fill="x", pady=(0, 7))
        ttk.Label(record_mode_title, text="智能录制", style="SidebarSection.TLabel").pack(side="left")
        self._help_badge(
            record_mode_title,
            "桌面自动记录坐标；绑定游戏窗口，或在游戏中按 F8，可自动记录锁中心的视角转向。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(7, 0))
        # 标题与控件分两行：单行会把标题、帮助徽章、数字框、单位框和按钮
        # 全部挤在 318px 内，高 DPI 下整行溢出侧栏，按钮文字被裁切。
        interval_title = ttk.Frame(sidebar, style="Sidebar.TFrame")
        interval_title.pack(fill="x", pady=(0, 7))
        ttk.Label(interval_title, text="桌面轨迹间隔", style="Sidebar.TLabel").pack(side="left")
        self._help_badge(
            interval_title, "录制桌面鼠标移动时，相邻轨迹点的最小间隔；数值越小记录越细。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(6, 0))
        interval_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        interval_row.pack(fill="x", pady=(0, 15))
        self.interval_spin = ttk.Spinbox(
            interval_row, from_=10, to=500, increment=5,
            textvariable=self.interval_var, width=7,
        )
        self.interval_spin.pack(side="left")
        ttk.Combobox(
            interval_row, textvariable=self.interval_var.unit, values=TIME_UNITS,
            state="readonly", width=4,
        ).pack(side="left", padx=(5, 0))
        self.interval_edit_button = ttk.Button(
            interval_row, text="修改", width=5, style="SidebarGhost.TButton",
            command=lambda: self._toggle_locked_spinbox(
                self.interval_spin, self.interval_edit_button, self._settings_changed,
            ),
        )
        self.interval_edit_button.pack(side="left", padx=(8, 0))
        self.interval_spin.configure(state="disabled")

        ttk.Separator(sidebar).pack(fill="x", pady=4)
        target_title = ttk.Frame(sidebar, style="Sidebar.TFrame")
        target_title.pack(fill="x", pady=(16, 7))
        ttk.Label(target_title, text="目标窗口", style="Section.TLabel").pack(side="left")
        self._help_badge(
            target_title,
            "绑定后，坐标会按目标窗口记录和回放；游戏相对转向保持原始视角幅度，不参与分辨率缩放。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(7, 0))
        self.bind_label_widget = ttk.Label(sidebar, textvariable=self.bind_label_var,
                                           wraplength=250, style="SidebarMuted.TLabel")
        self.bind_label_widget.pack(anchor="w", fill="x", pady=(0, 8))
        row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="选择窗口", command=self.choose_window, bootstyle="primary").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="清除绑定", command=self.unbind_window, width=7, style="SidebarGhost.TButton").pack(side="left", padx=(8, 0))
        coordinate_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        coordinate_row.pack(fill="x", pady=(8, 0))
        self.cursor_position_button = ttk.Button(
            coordinate_row, text="开始实时读取", command=self.toggle_cursor_tracking,
            style="SidebarGhost.TButton",
        )
        self.cursor_position_button.pack(side="left")
        ttk.Label(
            coordinate_row, textvariable=self.cursor_position_var,
            style="SidebarMuted.TLabel",
        ).pack(side="left", padx=(8, 0))

        ttk.Label(sidebar, text="执行设置", style="SidebarSection.TLabel").pack(anchor="w", pady=(18, 7))
        focus_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        focus_row.pack(fill="x", pady=(0, 6))
        tk.Checkbutton(
            focus_row, text="强制专注模式",
            variable=self.focus_mode_enabled_var,
            command=self._settings_changed,
            **check_style,
        ).pack(side="left")
        self._help_badge(
            focus_row, "执行时锁定实体键鼠，适合需要持续前台操作的目标。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(6, 0))
        activate_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        activate_row.pack(fill="x", pady=(0, 6))
        tk.Checkbutton(
            activate_row, text="执行时前置目标",
            variable=self.activate_target_enabled_var,
            command=self._toggle_target_activation,
            **check_style,
        ).pack(side="left")
        self._help_badge(
            activate_row, "勾选后每次执行前激活目标窗口；取消勾选只停止前置，不会清除已保存的目标窗口。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(6, 0))
        activation_toggle_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        activation_toggle_row.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            activation_toggle_row, text="启用执行前置窗口",
            variable=self.activation_enabled_var,
            command=self._toggle_activation_enabled,
            **check_style,
        ).pack(side="left")
        self._help_badge(
            activation_toggle_row, "可指定另一个窗口先被激活，再执行当前脚本。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(sidebar, text="执行前置窗口", style="SidebarMuted.TLabel").pack(anchor="w")
        ttk.Label(
            sidebar, textvariable=self.activation_label_var,
            wraplength=250, style="SidebarMuted.TLabel",
        ).pack(anchor="w", fill="x", pady=(2, 6))
        activation_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        activation_row.pack(fill="x", pady=(0, 8))
        ttk.Button(
            activation_row, text="选择前置窗口", command=self.choose_activation_window,
            style="SidebarGhost.TButton",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            activation_row, text="跟随目标", command=self.unbind_activation_window,
            width=8, style="SidebarGhost.TButton",
        ).pack(side="left", padx=(8, 0))
        # 同“桌面轨迹间隔”：标题与控件分两行，避免高 DPI 下控件被挤出侧栏。
        repeat_title = ttk.Frame(sidebar, style="Sidebar.TFrame")
        repeat_title.pack(fill="x", pady=(0, 7))
        ttk.Label(repeat_title, text="脚本重复次数", style="Sidebar.TLabel").pack(side="left")
        self._help_badge(
            repeat_title, "执行当前脚本时完整重复的次数。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(6, 0))
        repeat_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        repeat_row.pack(fill="x")
        self.repeat_spin = ttk.Spinbox(
            repeat_row, from_=1, to=999999, textvariable=self.repeat_var, width=7,
        )
        self.repeat_spin.pack(side="left")
        self.repeat_edit_button = ttk.Button(
            repeat_row, text="修改", width=5, style="SidebarGhost.TButton",
            command=lambda: self._toggle_locked_spinbox(
                self.repeat_spin, self.repeat_edit_button, self._settings_changed,
            ),
        )
        self.repeat_edit_button.pack(side="left", padx=(8, 0))
        self.repeat_spin.configure(state="disabled")

        notice_position_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        notice_position_row.pack(fill="x", pady=(9, 0))
        notice_title = ttk.Frame(notice_position_row, style="Sidebar.TFrame")
        notice_title.pack(side="left")
        ttk.Label(notice_title, text="浮动提醒位置", style="Sidebar.TLabel").pack(side="left")
        self._help_badge(
            notice_title, "选择脚本“提醒”动作在屏幕上的显示位置。",
            background=COLOR_SIDEBAR,
        ).pack(side="left", padx=(6, 0))
        notice_position_box = ttk.Combobox(
            notice_position_row,
            textvariable=self.floating_notice_position_var,
            values=FLOATING_NOTICE_POSITIONS,
            state="readonly",
            width=10,
        )
        notice_position_box.pack(side="right")
        notice_position_box.bind("<<ComboboxSelected>>", self._settings_changed)
        notice_position_box.bind("<MouseWheel>", lambda _event: "break")

        ttk.Separator(sidebar).pack(fill="x", pady=(16, 4))
        ttk.Label(sidebar, text="启动与备份", style="SidebarSection.TLabel").pack(
            anchor="w", pady=(12, 7),
        )
        tk.Checkbutton(
            sidebar, text="启用定时备份", variable=self.timed_backup_enabled_var,
            command=self._startup_backup_settings_changed, **check_style,
        ).pack(anchor="w", pady=2)
        backup_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        backup_row.pack(fill="x", pady=(5, 8))
        ttk.Label(backup_row, text="备份间隔", style="Sidebar.TLabel").pack(side="left")
        self.backup_interval_box = ttk.Combobox(
            backup_row, textvariable=self.backup_interval_var,
            values=BACKUP_INTERVAL_CHOICES, state="readonly", width=7,
        )
        self.backup_interval_box.pack(side="right")
        self.backup_interval_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._startup_backup_settings_changed(),
        )
        self.backup_interval_box.bind("<MouseWheel>", lambda _event: "break")
        tk.Checkbutton(
            sidebar, text="开机自启动", variable=self.windows_startup_enabled_var,
            command=self._startup_backup_settings_changed, **check_style,
        ).pack(anchor="w", pady=2)
        tk.Checkbutton(
            sidebar, text="启动时最小化到托盘", variable=self.start_minimized_to_tray_var,
            command=self._startup_backup_settings_changed, **check_style,
        ).pack(anchor="w", pady=2)
        tk.Checkbutton(
            sidebar, text="启动时执行工作流", variable=self.startup_run_workflow_var,
            command=self._startup_backup_settings_changed, **check_style,
        ).pack(anchor="w", pady=2)
        startup_workflow_row = ttk.Frame(sidebar, style="Sidebar.TFrame")
        startup_workflow_row.pack(fill="x", pady=(5, 0))
        ttk.Entry(
            startup_workflow_row, textvariable=self.startup_workflow_path_var,
            state="readonly", width=22,
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            startup_workflow_row, text="选择…", width=6, style="SidebarGhost.TButton",
            command=self._choose_startup_workflow,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            sidebar, text="每个脚本固定覆盖同一份备份，不累计历史副本。",
            wraplength=250, style="SidebarMuted.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        ttk.Label(sidebar, text="专注执行：F12 停止；无响应时按 Ctrl + Alt + Del。",
                  wraplength=250, style="SidebarMuted.TLabel").pack(anchor="w", pady=(10, 0))

        bind_sidebar_wheel(sidebar)
        update_sidebar_scrollregion()

    @staticmethod
    def _help_badge(parent, text: str, *, background: str = COLOR_BG):
        """Create a visible question-mark badge with a hover description."""
        badge = tk.Label(
            parent, text="?", width=2, cursor="hand2",
            background=COLOR_BLUE, foreground="#EAF4FF",
            activebackground=COLOR_BLUE, activeforeground="#FFFFFF",
            font=("Microsoft YaHei UI", 9, "bold"), relief="flat",
        )
        Tooltip(badge, text, anchor=parent)
        return badge

    def _build_script_tab(self):
        header = ttk.Frame(self.script_tab, padding=(16, 14, 16, 8), style="Workspace.TFrame")
        header.pack(fill="x")
        meta_row = ttk.Frame(header, style="Workspace.TFrame")
        meta_row.pack(fill="x")
        ttk.Label(meta_row, text="脚本名称", style="PageTitle.TLabel").pack(side="left")
        name_entry = ttk.Entry(meta_row, textvariable=self.script_name_var, width=30)
        name_entry.pack(side="left", padx=(12, 14), ipady=2)
        name_entry.bind("<KeyRelease>", lambda _: self._mark_dirty())
        self.global_script_marker = ttk.Label(meta_row, text="", style="GlobalMarker.TLabel")
        self.global_script_marker.pack(side="left", padx=(0, 10))
        ttk.Label(meta_row, text="类别").pack(side="left")
        category_box = ttk.Combobox(
            meta_row, textvariable=self.script_category_var,
            values=SCRIPT_CATEGORY_VALUES, state="readonly", width=10,
        )
        category_box.pack(side="left", padx=(6, 14))
        category_box.bind("<<ComboboxSelected>>", self._script_category_changed)
        ttk.Label(meta_row, textvariable=self.record_count_var, style="Muted.TLabel").pack(side="right")

        file_row = ttk.Frame(header, style="Workspace.TFrame")
        file_row.pack(fill="x", pady=(10, 0))
        ttk.Button(file_row, text="新建", command=self.new_script, style="Ghost.TButton").pack(side="left")
        ttk.Button(file_row, text="打开", command=self.open_script, style="Ghost.TButton").pack(side="left", padx=(6, 0))
        ttk.Button(file_row, text="关闭", command=self.close_script, style="DangerScriptTool.TButton").pack(side="left", padx=(6, 0))
        self.undo_open_button = ttk.Button(
            file_row, text="↩ 撤销打开", command=self.undo_open_script,
            style="Ghost.TButton", state="disabled",
        )
        self.undo_open_button.pack(side="left", padx=(6, 0))
        ttk.Button(file_row, text="保存", command=self.save_current_script, bootstyle="primary").pack(side="left", padx=(6, 0))
        ttk.Button(file_row, text="新开窗口", command=self.open_new_window, style="Ghost.TButton").pack(side="left", padx=(6, 0))
        ttk.Button(file_row, text="模块管理…", command=self.open_template_region_manager,
                   style="Ghost.TButton").pack(side="right")
        ttk.Button(file_row, text="目录设置…", command=self._configure_script_directories,
                   style="Ghost.TButton").pack(side="right", padx=(0, 6))
        ttk.Button(file_row, text="打开脚本目录", command=lambda: self.open_folder(self._script_category_dir()),
                   style="Ghost.TButton").pack(side="right", padx=(0, 6))

        toolbar = ttk.Frame(self.script_tab, padding=(16, 4, 16, 10), style="Toolbar.TFrame")
        toolbar.pack(fill="x")
        toolbar.columnconfigure(0, weight=1)

        add_group = ttk.Frame(toolbar, style="Toolbar.TFrame")
        add_group.grid(row=0, column=0, sticky="ew")
        lower_toolbar = ttk.Frame(toolbar, style="Toolbar.TFrame")
        lower_toolbar.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        lower_toolbar.columnconfigure(2, weight=1)
        pos_group = ttk.Frame(lower_toolbar, style="Toolbar.TFrame")
        pos_group.grid(row=0, column=0, sticky="w", padx=(0, 14))
        ttk.Label(pos_group, text="插入位置", style="ToolGroupTitle.TLabel").pack(anchor="w", pady=(0, 5))
        pos_buttons = ttk.Frame(pos_group, style="Toolbar.TFrame")
        pos_buttons.pack(fill="x")
        self.insert_above_button = ttk.Button(
            pos_buttons, text="▲ 向上插入",
            command=lambda: self._set_insert_position(True),
        )
        self.insert_above_button.pack(side="left")
        self.insert_below_button = ttk.Button(
            pos_buttons, text="▼ 向下插入",
            command=lambda: self._set_insert_position(False),
        )
        self.insert_below_button.pack(side="left", padx=(6, 0))
        ttk.Separator(lower_toolbar, orient="vertical").grid(row=0, column=1, sticky="nsw", padx=(0, 14))
        edit_group = ttk.Frame(lower_toolbar, style="Toolbar.TFrame")
        edit_group.grid(row=0, column=2, sticky="ew")
        self._set_insert_position(False)

        ttk.Label(add_group, text="添加动作", style="ToolGroupTitle.TLabel").pack(anchor="w", pady=(0, 5))
        add_buttons = ttk.Frame(add_group, style="Toolbar.TFrame")
        add_buttons.pack(fill="x")
        add_button_specs = (
            ("◷ 延时", self.add_delay, "ScriptTool.TButton"),
            ("⌨ 键盘", self.add_key, "ScriptTool.TButton"),
            ("T 文本", self.add_text, "ScriptTool.TButton"),
            ("i 提醒", self.add_notice, "ScriptTool.TButton"),
            ("↖ 移动", self.add_mouse_move, "ScriptTool.TButton"),
            ("◉ 点击", self.add_click, "ScriptTool.TButton"),
            ("↻ 连点", self.add_repeat_click, "ScriptTool.TButton"),
            ("▶ 软件", self.add_open_app, "ScriptTool.TButton"),
            ("✕ 关闭", self.add_close_app, "ScriptTool.TButton"),
            ("◈ 脚本全局", self.add_global_detect, "AccentScriptTool.TButton"),
            ("▤ 识别模块", self.add_module, "AccentScriptTool.TButton"),
            ("⇢ 跳转", self.add_jump, "AccentScriptTool.TButton"),
        )
        for index, (text, command, style_name) in enumerate(add_button_specs):
            ttk.Button(add_buttons, text=text, command=command, style=style_name).pack(
                side="left", padx=(0 if index == 0 else 4, 0),
            )
        ttk.Label(edit_group, text="编辑选中动作", style="ToolGroupTitle.TLabel").pack(anchor="w", pady=(0, 5))
        edit_buttons = ttk.Frame(edit_group, style="Toolbar.TFrame")
        edit_buttons.pack(fill="x")
        self.undo_button = ttk.Button(edit_buttons, text="↶ 撤销", command=self.undo_action_edit,
                                      style="ScriptTool.TButton", state="disabled")
        self.undo_button.pack(side="left")
        self.redo_button = ttk.Button(edit_buttons, text="↷ 重做", command=self.redo_action_edit,
                                      style="ScriptTool.TButton", state="disabled")
        self.redo_button.pack(side="left", padx=(4, 0))
        edit_button_specs = (
            ("✎ 编辑", self.edit_selected_action, "ScriptTool.TButton"),
            ("⧉ 复制", self.copy_selected_actions_down, "ScriptTool.TButton"),
            ("⇥ 引用脚本", self.insert_script_reference, "ScriptTool.TButton"),
            ("⇥ 逐行插入", self.insert_script_expanded, "ScriptTool.TButton"),
            ("▶ 从此", self.run_script_from_selected_action, "AccentScriptTool.TButton"),
            ("↑ 上移", lambda: self.move_action(-1), "ScriptTool.TButton"),
            ("↓ 下移", lambda: self.move_action(1), "ScriptTool.TButton"),
            ("× 删除", self.delete_actions, "DangerScriptTool.TButton"),
        )
        for index, (text, command, style_name) in enumerate(edit_button_specs):
            button = ttk.Button(edit_buttons, text=text, command=command, style=style_name)
            button.pack(
                side="left", padx=(4, 0),
            )
            if index == 0:
                self.edit_action_button = button

        # 全局脚本：触发条件区块 + 语句体标题（类别为"全局"时显示）。
        self.trigger_holder = ttk.Frame(self.script_tab, padding=(16, 0, 16, 0), style="Workspace.TFrame")
        self.trigger_holder.pack(fill="x")
        self.trigger_section = ttk.Frame(self.trigger_holder, style="GlobalTrigger.TFrame")
        trigger_row = ttk.Frame(self.trigger_section, style="GlobalTrigger.TFrame")
        trigger_row.pack(fill="x", pady=(10, 6))
        ttk.Label(
            trigger_row, text="◈ 触发条件：", style="GlobalTriggerTitle.TLabel",
        ).pack(side="left")
        self.trigger_summary_var = tk.StringVar(value="")
        self.trigger_summary_label = ttk.Label(
            trigger_row, textvariable=self.trigger_summary_var,
            style="GlobalTriggerSummary.TLabel",
        )
        self.trigger_summary_label.pack(side="left", fill="x", expand=True)
        ttk.Button(
            trigger_row, text="编辑触发条件", command=self._edit_global_trigger,
            style="Ghost.TButton",
        ).pack(side="right")
        self.clear_trigger_button = ttk.Button(
            trigger_row, text="清除", command=self._clear_global_trigger,
            style="DangerScriptTool.TButton",
        )
        ttk.Label(
            self.trigger_section, text="要执行的动作（触发后按顺序执行）：",
            style="GlobalTriggerTitle.TLabel",
        ).pack(fill="x", pady=(0, 8))

        frame = ttk.Frame(self.script_tab, padding=(16, 0, 16, 16), style="Surface.TFrame")
        frame.pack(fill="both", expand=True)
        self.action_tree = ttk.Treeview(frame, columns=("index", "kind", "detail", "delay"), show="headings", selectmode="extended")
        for column, text, width, anchor in (
            ("index", "#", 50, "center"), ("kind", "动作", 104, "w"),
            ("detail", "参数", 590, "w"), ("delay", "执行前延时", 106, "center"),
        ):
            self.action_tree.heading(column, text=text)
            self.action_tree.column(column, width=width, anchor=anchor, stretch=column == "detail")
        self.action_tree.column("kind", minwidth=96)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.action_tree.yview)
        self.action_tree.configure(yscrollcommand=scroll.set)
        self.action_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.empty_action_hint = ttk.Label(
            frame, text="还没有动作\n按 F8 开始录制，或使用上方工具栏添加动作",
            style="Empty.TLabel", anchor="center", justify="center"
        )
        self.action_tree.bind("<Double-1>", lambda _: self.edit_selected_action())
        self.action_tree.bind("<<TreeviewSelect>>", self._update_action_edit_button, add="+")
        self.action_tree.bind("<Delete>", lambda _: self.delete_actions())
        self.action_tree.bind("<Control-z>", lambda _: self.undo_action_edit())
        self.action_tree.bind("<Control-y>", lambda _: self.redo_action_edit())
        self.action_tree.bind("<Control-Shift-z>", lambda _: self.redo_action_edit())
        self.action_tree.bind("<Control-a>", self._select_all_actions)
        self.action_tree.bind("<Button-3>", self._show_action_context_menu)

    def _build_workflow_tab(self):
        header = ttk.Frame(self.workflow_tab, padding=(16, 18, 16, 12), style="Workspace.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="工作流名称", style="PageTitle.TLabel").pack(side="left")
        workflow_name_entry = ttk.Entry(header, textvariable=self.workflow_name_var, width=22)
        workflow_name_entry.pack(side="left", padx=(8, 15))
        workflow_name_entry.bind("<KeyRelease>", self._schedule_workflow_draft_save)
        start_label = ttk.Frame(header, style="Workspace.TFrame")
        start_label.pack(side="left")
        ttk.Label(start_label, text="开始时间").pack(side="left")
        self._help_badge(
            start_label, "留空表示手动运行；设置后到达指定时间自动开始当前工作流。",
        ).pack(side="left", padx=(6, 0))
        ttk.Entry(header, textvariable=self.workflow_start_var, width=20, state="readonly").pack(side="left", padx=(8, 4))
        ttk.Button(header, text="📅 选择", command=self.choose_workflow_start,
                   style="CompactGhost.TButton").pack(side="left")
        ttk.Button(
            header, text="运行工作流", command=self.run_workflow,
            bootstyle="success",
        ).pack(side="right")
        ttk.Checkbutton(
            header, text="测试模式", variable=self.workflow_test_mode_var,
            bootstyle="round-toggle",
        ).pack(side="right", padx=(0, 8))
        ttk.Button(header, text="从选中行运行", command=self.run_workflow_from_selected,
                   style="CompactGhost.TButton").pack(side="right", padx=(0, 8))

        start_delay_bar = ttk.Frame(self.workflow_tab, padding=(16, 0, 16, 8), style="Workspace.TFrame")
        start_delay_bar.pack(fill="x")
        ttk.Checkbutton(
            start_delay_bar, text="启动延时", variable=self.workflow_start_delay_enabled_var,
            command=self._toggle_workflow_start_delay_control, bootstyle="round-toggle",
        ).pack(side="left")
        self.workflow_start_delay_entry = ttk.Entry(
            start_delay_bar, textvariable=self.workflow_start_delay_seconds_var, width=8,
        )
        self.workflow_start_delay_entry.pack(side="left", padx=(8, 5))
        self.workflow_start_delay_entry.bind("<KeyRelease>", self._schedule_workflow_draft_save)
        ttk.Combobox(
            start_delay_bar, textvariable=self.workflow_start_delay_seconds_var.unit,
            values=TIME_UNITS, state="readonly", width=4,
        ).pack(side="left", padx=(0, 5))
        ttk.Label(start_delay_bar, text="后开始（从头运行和从选中行运行均生效）",
                  style="Muted.TLabel").pack(side="left")
        self._toggle_workflow_start_delay_control(persist=False)

        # Global modules and workflow steps share a draggable vertical split.
        self.workflow_content_pane = ttk.Panedwindow(self.workflow_tab, orient="vertical")
        self.workflow_content_pane.pack(fill="both", expand=True)

        # Global module box (top, independent numbering from 1)
        global_box = ttk.Frame(self.workflow_content_pane, padding=(16, 0, 16, 6), style="Surface.TFrame")
        self.workflow_content_pane.add(global_box, weight=2)
        global_header = ttk.Frame(global_box, style="Surface.TFrame")
        global_header.pack(fill="x", pady=(10, 6))
        ttk.Label(global_header, text="工作流全局模块", style="PageTitle.TLabel").pack(side="left")
        ttk.Label(global_header, text="独立编号 · 执行时启用全局检测", style="Muted.TLabel").pack(
            side="left", padx=(8, 0),
        )
        global_toolbar = ttk.Frame(global_box, style="Surface.TFrame")
        global_toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(
            global_toolbar, text="添加工作流全局模块", command=self.add_workflow_global_module,
            bootstyle="primary-outline",
        ).pack(side="left")
        ttk.Button(
            global_toolbar, text="编辑选中", command=self.edit_selected_global_module,
            style="CompactGhost.TButton",
        ).pack(side="left", padx=5)
        ttk.Button(
            global_toolbar, text="启用/禁用", command=self.toggle_selected_global_module,
            style="CompactGhost.TButton",
        ).pack(side="left")
        ttk.Button(
            global_toolbar, text="删除", command=self.delete_global_module,
            bootstyle="danger-outline",
        ).pack(side="left", padx=5)
        self.global_delete_undo_button = ttk.Button(
            global_toolbar, text="↶ 撤销删除", command=self.undo_delete_global_module,
            style="CompactGhost.TButton", state="disabled",
        )
        self.global_delete_undo_button.pack(side="left")
        ttk.Label(
            global_toolbar, text="双击行更换模块", style="Muted.TLabel",
        ).pack(side="left", padx=(10, 0))
        global_tree_frame = ttk.Frame(global_box, style="Surface.TFrame")
        global_tree_frame.pack(fill="x", pady=(0, 10))
        self.global_tree = ttk.Treeview(
            global_tree_frame, columns=("index", "module", "status"),
            show="headings", selectmode="extended", style="Workflow.Treeview", height=6,
        )
        for column, text, width, anchor in (
            ("index", "步骤", 65, "center"),
            ("module", "全局检测模块", 700, "w"),
            ("status", "状态", 100, "center"),
        ):
            self.global_tree.heading(column, text=text)
            self.global_tree.column(column, width=width, anchor=anchor, stretch=column == "module")
        global_scroll = ttk.Scrollbar(global_tree_frame, orient="vertical", command=self.global_tree.yview)
        self.global_tree.configure(yscrollcommand=global_scroll.set)
        self.global_tree.tag_configure("disabled", foreground="#F2B84B", background="#2B2418")
        self.global_tree.tag_configure("global", foreground="#7BC96F", background="#14261B")
        self.global_tree.pack(side="left", fill="both", expand=True)
        global_scroll.pack(side="right", fill="y")
        self.empty_global_hint = ttk.Label(
            global_tree_frame, text="还没有工作流全局模块\n点击“添加工作流全局模块”从模块仓库中选择",
            style="Empty.TLabel", anchor="center", justify="center",
        )
        self.global_tree.bind("<Double-1>", lambda _event: self.edit_selected_global_module())
        self.global_tree.bind("<Control-a>", self._select_all_global_modules)
        self.global_tree.bind("<Button-3>", self._show_global_context_menu, add="+")

        # Workflow box (bottom, independent numbering from 1)
        workflow_box = ttk.Frame(self.workflow_content_pane, padding=(16, 0, 16, 12), style="Surface.TFrame")
        self.workflow_content_pane.add(workflow_box, weight=3)
        workflow_header = ttk.Frame(workflow_box, style="Surface.TFrame")
        workflow_header.pack(fill="x", pady=(8, 0))
        ttk.Label(workflow_header, text="工作流", style="PageTitle.TLabel").pack(side="left")
        ttk.Label(workflow_header, text="独立编号 · 从 1 开始", style="Muted.TLabel").pack(
            side="left", padx=(8, 0),
        )
        toolbar = ttk.Frame(workflow_box, padding=(0, 4, 0, 8), style="Surface.TFrame")
        toolbar.pack(fill="x")
        file_toolbar = ttk.Frame(toolbar, style="Surface.TFrame")
        file_toolbar.pack(fill="x")
        ttk.Button(file_toolbar, text="新建", command=self.new_workflow, style="CompactGhost.TButton").pack(side="left")
        ttk.Button(file_toolbar, text="打开", command=self.open_workflow, style="CompactGhost.TButton").pack(side="left", padx=(5, 0))
        ttk.Button(file_toolbar, text="保存", command=self.save_current_workflow, bootstyle="primary").pack(side="left", padx=(5, 0))
        ttk.Button(file_toolbar, text="打开工作流目录", command=lambda: self.open_folder(WORKFLOWS_DIR), style="CompactGhost.TButton").pack(side="right")

        add_toolbar = ttk.Frame(toolbar, style="Surface.TFrame")
        add_toolbar.pack(fill="x", pady=(7, 0))
        ttk.Label(add_toolbar, text="添加 / 插入", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ttk.Button(add_toolbar, text="添加当前脚本", command=self.add_current_script_step, style="CompactGhost.TButton").pack(side="left")
        ttk.Button(add_toolbar, text="选择已有脚本", command=self.add_script_step, style="CompactGhost.TButton").pack(side="left", padx=(5, 0))
        ttk.Button(add_toolbar, text="添加模块", command=self.add_workflow_module_step,
                   style="CompactGhost.TButton").pack(side="left", padx=(5, 0))
        ttk.Separator(add_toolbar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(add_toolbar, text="插入位置", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.workflow_insert_above_button = ttk.Button(
            add_toolbar, text="▲ 上方", width=6,
            command=lambda: self._set_workflow_insert_position(True),
            style="CompactGhost.TButton",
        )
        self.workflow_insert_above_button.pack(side="left")
        self.workflow_insert_below_button = ttk.Button(
            add_toolbar, text="▼ 下方", width=6,
            command=lambda: self._set_workflow_insert_position(False),
            style="CompactGhost.TButton",
        )
        self.workflow_insert_below_button.pack(side="left", padx=(5, 0))
        ttk.Button(add_toolbar, text="插入脚本", command=self.insert_workflow_step,
                   bootstyle="primary-outline").pack(side="left", padx=(6, 0))
        ttk.Button(add_toolbar, text="插入模块", command=self.insert_workflow_module_step,
                   bootstyle="primary-outline").pack(side="left", padx=(4, 0))
        self._set_workflow_insert_position(False)

        edit_toolbar = ttk.Frame(toolbar, style="Surface.TFrame")
        edit_toolbar.pack(fill="x", pady=(5, 0))
        ttk.Label(edit_toolbar, text="编辑 / 排序", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        ttk.Button(edit_toolbar, text="统一设置参数", command=self.set_all_workflow_step_options,
                   style="CompactGhost.TButton").pack(side="left")
        ttk.Button(edit_toolbar, text="启用/禁用", command=self.toggle_selected_workflow_step,
                   style="CompactGhost.TButton").pack(side="left", padx=(5, 0))
        ttk.Button(edit_toolbar, text="上移", command=lambda: self.move_workflow_step(-1), style="CompactGhost.TButton").pack(side="left", padx=(5, 2))
        ttk.Button(edit_toolbar, text="下移", command=lambda: self.move_workflow_step(1), style="CompactGhost.TButton").pack(side="left")
        ttk.Button(edit_toolbar, text="删除", command=self.delete_workflow_step, bootstyle="danger-outline").pack(side="left", padx=5)
        self.workflow_delete_undo_button = ttk.Button(
            edit_toolbar, text="↶ 撤销删除", command=self.undo_delete_workflow_step,
            style="CompactGhost.TButton", state="disabled",
        )
        self.workflow_delete_undo_button.pack(side="left")
        ttk.Label(edit_toolbar, text="提示：双击单元格可直接修改", style="Muted.TLabel").pack(side="right")

        frame = ttk.Frame(workflow_box, style="Surface.TFrame")
        frame.pack(fill="both", expand=True)
        self.workflow_tree = ttk.Treeview(
            frame, columns=("index", "script", "repeat", "before", "interval", "enabled"),
            show="headings", selectmode="extended", style="Workflow.Treeview", height=10,
        )
        for column, text, width, anchor in (
            ("index", "步骤", 65, "center"), ("script", "脚本 / 模块", 500, "w"),
            ("repeat", "执行次数", 90, "center"), ("before", "开始前等待", 115, "center"),
            ("interval", "重复间隔", 115, "center"),
            ("enabled", "状态", 80, "center"),
        ):
            self.workflow_tree.heading(column, text=text)
            self.workflow_tree.column(column, width=width, anchor=anchor, stretch=column == "script")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.workflow_tree.yview)
        self.workflow_tree.configure(yscrollcommand=scroll.set)
        self.workflow_tree.tag_configure("missing", foreground="#FF6B6B", background="#321F24")
        self.workflow_tree.tag_configure("disabled", foreground="#F2B84B", background="#2B2418")
        self.workflow_tree.tag_configure("module_disabled", foreground="#FF8A8A", background="#3A2028")
        self.workflow_tree.tag_configure("exhausted", foreground="#87939E", background="#161D23")
        self.workflow_tree.tag_configure("unlimited", foreground="#7BC96F", background="#14261B")
        self.workflow_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.empty_workflow_hint = ttk.Label(
            frame, text="工作流还没有步骤\n添加脚本或模块后，可按住步骤上下拖动调整顺序",
            style="Empty.TLabel", anchor="center", justify="center"
        )
        self.workflow_tree.bind("<ButtonPress-1>", self._workflow_drag_start, add="+")
        self.workflow_tree.bind("<B1-Motion>", self._workflow_drag_motion, add="+")
        self.workflow_tree.bind("<ButtonRelease-1>", self._workflow_drag_end, add="+")
        self.workflow_tree.bind("<Double-1>", self._edit_workflow_cell, add="+")
        self.workflow_tree.bind("<<TreeviewSelect>>", self._update_workflow_selection_color, add="+")
        self.workflow_tree.bind("<Control-a>", self._select_all_workflow_steps)
        self.workflow_tree.bind("<Button-3>", self._show_workflow_context_menu, add="+")

    def _build_log_tab(self):
        frame = ttk.Frame(self.log_tab, padding=16, style="Workspace.TFrame")
        frame.pack(fill="both", expand=True)
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="运行与错误记录", style="PageTitle.TLabel").pack(side="left")
        ttk.Button(top, text="清空", command=lambda: self.log_text.delete("1.0", "end"), bootstyle="secondary-outline").pack(side="right")
        ttk.Button(
            top, text="打开日志目录", command=lambda: self.open_folder(self.logs_dir),
            bootstyle="secondary-outline",
        ).pack(side="right", padx=(0, 6))
        self.log_text = tk.Text(frame, wrap="word", state="disabled", background=COLOR_SURFACE,
                                foreground=COLOR_TEXT, insertbackground=COLOR_TEXT,
                                selectbackground="#244D78", relief="flat", bd=0,
                                font=("Consolas", 10), padx=16, pady=14)
        self.log_text.pack(fill="both", expand=True)

    # General helpers
    def _ui(self, callback, *args):
        # 后台线程产生的运行日志必须先同步落盘，再排队更新 Tk 界面。
        # 这样即使 UI 正忙或程序随后异常退出，文件中也保留已经产生的日志。
        if getattr(callback, "__self__", None) is self \
                and getattr(callback, "__func__", None) is MacroFlowApp._log \
                and args:
            line = self._format_log_line(str(args[0]))
            self._write_log_line(line)
            callback = self._append_log_line_to_ui
            args = (line,)
        try:
            self.root.after(0, callback, *args)
        except RuntimeError:
            pass

    def _set_status(self, text: str, style: str = "normal"):
        self.status_var.set(text)
        colors = {"success": "#12B76A", "warning": "#F79009", "error": "#F04438", "normal": "#667085"}
        self.status_dot.configure(foreground=colors.get(style, colors["normal"]))

    def _refresh_coordinate_scale_status(self):
        self.coordinate_scale_var.set(coordinate_scale_summary(
            self.script.settings.get("recorded_screen"), get_virtual_screen_rect(),
        ))

    def _format_log_line(self, text: str) -> str:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            x, y = get_cursor_pos()
            cursor = f"[鼠标 {x},{y}]"
        except Exception:
            cursor = "[鼠标 ?,?]"
        return f"[{stamp}] {cursor} {text}\n"

    def _append_log_line_to_ui(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _write_log_line(self, line: str) -> None:
        log_path = getattr(self, "session_log_path", None)
        if log_path is not None:
            try:
                lock = getattr(self, "log_file_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    self.log_file_lock = lock
                with lock:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as log_file:
                        log_file.write(line)
                        log_file.flush()
            except OSError:
                pass

    def _log(self, text: str):
        line = self._format_log_line(text)
        self._write_log_line(line)
        self._append_log_line_to_ui(line)

    def _mark_dirty(self):
        self.dirty = True

    def _blank_script_with_activation_draft(self) -> MacroScript:
        """Create a blank script and restore the most recently selected pre-window."""
        script = MacroScript()
        app_settings = getattr(self, "app_settings", {})
        signature = app_settings.get("activation_window_draft")
        if isinstance(signature, dict) and signature.get("title"):
            script.settings["activation_window_enabled"] = bool(
                app_settings.get("activation_window_draft_enabled", True)
            )
            script.settings["activation_window"] = {
                "title": str(signature.get("title", "")),
                "class_name": str(signature.get("class_name", "")),
                "process_path": str(signature.get("process_path", "")),
            }
        return script

    def _remember_activation_draft(self) -> None:
        """Remember an explicit sidebar change independently from script loading."""
        self.activation_draft_enabled = bool(self.activation_enabled_var.get())
        self.activation_draft_signature = (
            dict(self.saved_activation_signature) if self.saved_activation_signature else None
        )

    def _collect_sidebar_settings(self) -> dict:
        try:
            interval = max(10, min(500, int(self.interval_var.get())))
        except (tk.TclError, TypeError, ValueError):
            interval = DEFAULT_MOUSE_MOVE_INTERVAL_MS
        try:
            repeat = max(1, min(999999, int(self.repeat_var.get())))
        except (tk.TclError, TypeError, ValueError):
            repeat = 1
        backup_interval = self.backup_interval_var.get()
        if backup_interval not in BACKUP_INTERVAL_CHOICES:
            backup_interval = "1h"
        self.interval_var.set(interval)
        self.repeat_var.set(repeat)
        self.backup_interval_var.set(backup_interval)
        return {
            "sound_enabled": bool(self.sound_enabled_var.get()),
            "mini_window_enabled": bool(self.mini_window_enabled_var.get()),
            "execution_mini_enabled": bool(self.execution_mini_enabled_var.get()),
            "execution_mini_position": list(getattr(self, "execution_mini_position", [])),
            "close_action": self.close_action_var.get(),
            "record_mode": "auto",
            "focus_mode_enabled": bool(self.focus_mode_enabled_var.get()),
            "activate_target_enabled": bool(self.activate_target_enabled_var.get()),
            "floating_notice_position": self.floating_notice_position_var.get(),
            "repeat": repeat,
            "bound_window": self.saved_window_signature,
            "activation_window_draft_enabled": bool(getattr(
                self, "activation_draft_enabled", self.activation_enabled_var.get(),
            )),
            "activation_window_draft": (
                dict(self.activation_draft_signature)
                if getattr(self, "activation_draft_signature", None) else None
            ),
            "workflow_draft": self._workflow_snapshot(),
            "workflow_path": display_path(self.workflow_path) if self.workflow_path else "",
            "timed_backup_enabled": bool(self.timed_backup_enabled_var.get()),
            "backup_interval": backup_interval,
            "windows_startup_enabled": bool(self.windows_startup_enabled_var.get()),
            "start_minimized_to_tray": bool(self.start_minimized_to_tray_var.get()),
            "startup_run_workflow": bool(self.startup_run_workflow_var.get()),
            "startup_workflow_path": self.startup_workflow_path_var.get().strip(),
            "level_scripts_dir": self.level_scripts_dir_var.get().strip() or "scripts/关卡",
            "level_pack_scripts_dir": self.level_pack_scripts_dir_var.get().strip() or "scripts/关卡封装",
            "switch_scripts_dir": self.switch_scripts_dir_var.get().strip() or "scripts/切换",
        }

    def _workflow_snapshot(self) -> dict:
        self.workflow.name = self.workflow_name_var.get().strip() or "未命名工作流"
        self.workflow.start_at = self.workflow_start_var.get().strip()
        self._read_workflow_start_delay(validate=False)
        return self.workflow.to_dict()

    def _persist_workflow_draft(self) -> None:
        self.workflow_draft_after_id = None
        self._persist_sidebar_settings()

    def _schedule_workflow_draft_save(self, _event=None) -> None:
        if self.workflow_draft_after_id is not None:
            self.root.after_cancel(self.workflow_draft_after_id)
        self.workflow_draft_after_id = self.root.after(350, self._persist_workflow_draft)

    def _persist_sidebar_settings(self, show_feedback: bool = False) -> bool:
        self.app_settings = self._collect_sidebar_settings()
        try:
            save_app_settings(self.app_settings)
        except OSError as exc:
            self._log(f"保存应用设置失败：{exc}")
            if show_feedback:
                self._notify("保存失败", str(exc))
            return False
        if show_feedback:
            self._set_status("左侧配置已保存", "success")
            self._log("已保存左侧配置，下次启动会自动恢复。")
        return True

    def save_sidebar_config(self):
        if self._persist_sidebar_settings(show_feedback=True):
            self._sync_windows_startup(log_errors=True)
            self._schedule_timed_backup()

    def _settings_changed(self, _event=None):
        self._persist_sidebar_settings()
        if self.recorder.running:
            if self.mini_window_enabled_var.get():
                self._show_recording_mini()
            else:
                self._hide_recording_mini()
                if self.main_hidden_for_recording:
                    self._restore_main_window()

    def _startup_backup_settings_changed(self):
        self._persist_sidebar_settings()
        self._sync_windows_startup(log_errors=True)
        self._schedule_timed_backup()

    def _sync_windows_startup(self, *, log_errors: bool = False) -> bool:
        try:
            set_windows_startup(bool(self.windows_startup_enabled_var.get()))
            return True
        except OSError as exc:
            if log_errors:
                self._log(f"同步开机自启动设置失败：{exc}")
            return False

    def _choose_startup_workflow(self):
        current = self.startup_workflow_path_var.get().strip()
        current_path = resolve_path(current) if current else WORKFLOWS_DIR
        initial_dir = current_path.parent if current and current_path.parent.is_dir() else WORKFLOWS_DIR
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=initial_dir, title="选择启动时执行的工作流",
            filetypes=[("MacroFlow 工作流", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.startup_workflow_path_var.set(display_path(path))
        self.startup_run_workflow_var.set(True)
        self._startup_backup_settings_changed()

    def _schedule_timed_backup(self):
        if self.backup_after_id is not None:
            try:
                self.root.after_cancel(self.backup_after_id)
            except tk.TclError:
                pass
            self.backup_after_id = None
        if self.exiting or not self.timed_backup_enabled_var.get():
            return
        interval = self.backup_interval_var.get()
        delay_ms = BACKUP_INTERVAL_MS.get(interval, BACKUP_INTERVAL_MS["1h"])
        self.backup_after_id = self.root.after(delay_ms, self._run_timed_backup)

    def _run_timed_backup(self):
        self.backup_after_id = None
        self._schedule_timed_backup()
        if self.exiting or self.backup_running:
            return
        roots = {
            SCRIPTS_DIR.resolve(), self._level_scripts_dir().resolve(),
            self._level_pack_scripts_dir().resolve(), self._switch_scripts_dir().resolve(),
        }
        current_path = self.script_path.resolve() if self.script_path else None
        current_snapshot = self.script.to_dict() if current_path else None
        self.backup_running = True

        def backup_worker():
            backed_up = 0
            errors: list[str] = []
            files: dict[str, Path] = {}
            for root in roots:
                if not root.is_dir():
                    continue
                try:
                    for path in root.rglob("*.json"):
                        if path.is_file():
                            files[str(path.resolve()).casefold()] = path.resolve()
                except OSError as exc:
                    errors.append(f"{root}: {exc}")
            if current_path:
                files[str(current_path).casefold()] = current_path
            for key, path in files.items():
                try:
                    snapshot = current_snapshot if current_path and key == str(current_path).casefold() else None
                    backup_script(path, snapshot)
                    backed_up += 1
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(f"{path}: {exc}")
            self.backup_running = False
            if self.exiting:
                return
            self._ui(self._log, f"定时备份完成：已覆盖 {backed_up} 个脚本的单份备份。")
            if errors:
                self._ui(self._log, f"定时备份失败 {len(errors)} 项：{errors[0]}")

        threading.Thread(target=backup_worker, name="MacroFlowScriptBackup", daemon=True).start()

    def _run_configured_startup_workflow(self):
        raw_path = self.startup_workflow_path_var.get().strip()
        if not raw_path:
            self._log("启动工作流未执行：尚未选择工作流文件。")
            return
        path = resolve_path(raw_path)
        try:
            self.workflow = load_workflow(path)
            self.workflow_path = path
            self._clear_workflow_delete_history()
            self.workflow_name_var.set(self.workflow.name)
            self.workflow_start_var.set(self.workflow.start_at)
            self.workflow_start_delay_enabled_var.set(self.workflow.start_delay_enabled)
            self.workflow_start_delay_seconds_var.unit.set("ms")
            self.workflow_start_delay_seconds_var.set(
                str(int(self.workflow.start_delay_seconds) * 1000)
            )
            self._toggle_workflow_start_delay_control(persist=False)
            self.rebuild_workflow_tree()
            self._persist_workflow_draft()
            self._log(f"启动时自动执行工作流：{path}")
            self.run_workflow()
        except Exception as exc:
            self._log(f"启动工作流执行失败：{path}：{exc}")

    def _toggle_target_activation(self):
        """Toggle execution focus without changing the persisted target binding."""
        self._persist_sidebar_settings()
        if self.activate_target_enabled_var.get():
            self._log("已启用执行时前置目标窗口；保留当前目标窗口绑定。")
        else:
            self._log("已停用执行时前置目标窗口；目标窗口绑定仍然保留，可随时重新启用。")

    def _toggle_locked_spinbox(self, spin, button, on_save):
        """Toggle a millisecond setting between locked and editable states."""
        if button.cget("text") == "修改":
            button.configure(text="保存")
            spin.configure(state="normal")
            spin.focus_set()
        else:
            button.configure(text="修改")
            spin.configure(state="disabled")
            on_save()

    # Global detection (script action or workflow global module)
    def _enter_script_global_scope(self, actions: list[dict]) -> tuple[str, ...]:
        """Enable every script-global action for the lifetime of this script.

        Registration is independent of the playback start row, so workflow
        repeats and breakpoint resumes cannot skip global actions above it.
        """
        keys: list[str] = []
        for action in actions:
            if str(action.get("type", "")) != "global_detect":
                continue
            action_id = str(action.get(ACTION_ID_KEY, "")).strip()
            if not action_id:
                action_id = new_action_id()
                action[ACTION_ID_KEY] = action_id
            key = f"script:{action_id}"
            keys.append(key)
            self._activate_global_detect_from_config(action)
        return tuple(keys)

    def _exit_script_global_scope(self, keys: object) -> None:
        """Stop only the script-global monitors owned by the leaving script."""
        locks = getattr(self, "global_detect_rearm_locks", None)
        preserve_rearm = bool(getattr(self, "global_detect_pending_restart", False))
        for key in tuple(keys or ()):
            monitor = self.global_detect_monitors.pop(str(key), None)
            if monitor is not None:
                monitor["stop"].set()
            if locks is not None and not preserve_rearm:
                locks.discard(str(key))
        condition = getattr(self, "global_capture_condition", None)
        if condition is not None:
            with condition:
                condition.notify_all()

    def _activate_global_detect_from_config(self, config: dict, module: dict | None = None):
        """Enable one global-detect monitor.

        Workflow-global modules are keyed by step_id. Script-global actions are
        keyed by action_id. While a module's steps are executing, re-arming is
        skipped because the monitor is already active.
        """
        if self.global_detect_module_running or getattr(self, "global_detect_pending_restart", False):
            return
        template_path = resolve_path(str(config.get("template", "")))
        # 引用模块：识别参数与动作 B 从对象仓库实时读取（改对象即生效）。
        # 统一用解析后的绝对路径查询，避免启用阶段和检测线程读到不同配置。
        module_ref = bool(config.get("module_ref"))
        module_ref_key = str(config.get("module_key") or config.get("template", ""))
        if module_ref:
            obj = registered_module_object(module_ref_key)
            if obj is None:
                self._log(f"全局检测未启用：引用的模块对象不存在：{module_ref_key or '未设置'}。")
                return
            if not bool(obj.get("enabled", True)):
                module_name = str(obj.get("name", "")).strip() or Path(
                    module_ref_key.replace("\\", "/"),
                ).stem
                self._log(f"全局检测未启用：模块管理中的 {module_name or '未命名模块'} 已禁用。")
                return
            if obj is not None:
                config = dict(config)
                if str(obj.get("template", "")).strip():
                    template_path = resolve_path(str(obj["template"]))
                config["threshold"] = obj.get("threshold", 0.85)
                config["interval_ms"] = obj.get("interval_ms", 250)
                config["start_delay_ms"] = obj.get("start_delay_ms", 0)
                config["fallback_module_key"] = obj.get("fallback_module_key", "")
                config["fallback_click"] = obj.get("fallback_click", False)
                config["hold_enabled"] = obj.get("hold_enabled", True)
                config["hold_ms"] = obj.get("hold_ms", 1000)
                config["restart_delay_ms"] = obj.get("delay_ms", 0)
                config["ignore_background"] = obj.get("ignore_background", False)
                config["recognize"] = obj.get("recognize", "")
                config["expected_text"] = obj.get("expected_text", "")
                config["match_mode"] = obj.get("match_mode", "contains")
                config["wait_text_absent"] = obj.get("wait_text_absent", False)
                config["click_count"] = obj.get("click_count", 1)
                for field in (
                    "ocr_offset_up", "ocr_offset_down", "ocr_offset_left", "ocr_offset_right",
                ):
                    config[field] = obj.get(field, 0)
        try:
            threshold = max(0.1, min(1.0, float(config.get("threshold", 0.85))))
            interval = max(100, min(10000, int(config.get("interval_ms", 500))))
            start_delay = max(0, min(86400000, int(config.get("start_delay_ms", 0))))
            # hold 上限放宽到 10 分钟：长时间“持续可见”判定（如回合间主线界面
            # 卡死 5 分钟才触发的兜底检测）不能被 60 秒截断。
            hold = max(0, min(600000, int(config.get("hold_ms", 1000))))
            restart_delay = max(0, min(60000, int(config.get("restart_delay_ms", DEFAULT_GLOBAL_CLICK_DELAY_MS))))
            jump_row = max(0, int(config.get("jump_row", 0)))
        except (TypeError, ValueError):
            threshold, interval, start_delay, hold, restart_delay, jump_row = 0.85, 500, 0, 1000, 0, 0
        region = config.get("region", [])
        # 旧配置没有 region_mode：有区域按自定义区域，否则按全屏。
        region_mode = str(config.get(
            "region_mode", "custom" if isinstance(region, (list, tuple)) and len(region) == 4 else "screen",
        ))
        if region_mode == "template":
            # 模块引用使用该模块自己的区域。同一图片可由多个模块共用，不能按图片路径
            # 反查一个不确定的区域；普通识图动作仍兼容旧的图片区域登记表。
            registered = (
                (obj or {}).get("region")
                if module_ref else registered_template_region(str(config.get("template", "")))
            )
            if registered and registered[2] > 0 and registered[3] > 0:
                region = tuple(int(part) for part in registered)
            else:
                region = None
        click = config.get("click_point", [])
        if module is not None:
            step_id = str(module.get("step_id", "")).strip()
            if not step_id:
                step_id = new_action_id()
                module["step_id"] = step_id
            key = f"workflow:{step_id}"
        else:
            action_id = str(config.get(ACTION_ID_KEY, "")).strip()
            if not action_id:
                action_id = new_action_id()
                config[ACTION_ID_KEY] = action_id
            key = f"script:{action_id}"
        rearm_locks = getattr(self, "global_detect_rearm_locks", None)
        if rearm_locks is None:
            rearm_locks = self.global_detect_rearm_locks = set()
        old = self.global_detect_monitors.get(key)
        if old is not None:
            old["stop"].set()
        # 引用模块的动作 B 分发参数（实时引用对象，worker 每轮重读对象属性）。
        after_action = "click_match"
        button = "left"
        second = None
        segment: list[dict] = []
        timeout_enabled = False
        not_found_timeout_ms = 3000
        timeout_segment: list[dict] = []
        if module_ref:
            obj = registered_module_object(module_ref_key)
            if obj is not None:
                after_action = str(obj.get("after_action", "click_match"))
                button = str(obj.get("button", "left"))
                if after_action == "click_custom" and len(obj.get("click_point") or []) == 2:
                    click = obj.get("click_point")
                if after_action == "second_match":
                    second = {
                        "template": str(obj.get("second_match_template", "")).strip(),
                        "timeout_ms": max(0, int(obj.get("second_match_timeout_ms", 3000))),
                        "blocking": bool(obj.get("blocking", False)),
                        "click_target": str(obj.get("second_match_click_target", "second")),
                        "click_region": obj.get("second_match_click_region") or [],
                    }
                if bool(obj.get("run_code_after_action", False)) or after_action == "run_actions":
                    segment = list(obj.get("on_success_actions") or [])
                timeout_enabled = bool(obj.get("run_code_on_timeout", False)) and not bool(
                    obj.get("wait_text_absent", False)
                )
                not_found_timeout_ms = max(0, int(obj.get("not_found_timeout_ms", 3000)))
                timeout_segment = list(obj.get("on_timeout_actions") or [])
        if module is not None:
            module_display_name = (
                str(module.get("name", "")).strip()
                or Path(str(module.get("script", ""))).stem
                or "工作流全局模块"
            )
        elif module_ref:
            module_display_name = (
                str((obj or {}).get("name", "")).strip()
                or Path(module_ref_key.replace("\\", "/")).stem
                or "引用全局模块"
            )
        else:
            module_display_name = str(config.get("name", "")).strip() or "脚本全局模块"
        monitor = {
            "key": key,
            "module": dict(module) if module is not None else None,
            "thread": None,
            "stop": threading.Event(),
            "template": template_path,
            "threshold": threshold,
            "interval_ms": interval,
            "start_delay_ms": start_delay if module is None else 0,
            "fallback_module_key": str(config.get("fallback_module_key", "")).strip(),
            "fallback_click": bool(config.get("fallback_click", False)),
            "fallback_click_count": max(1, min(9999, int(config.get("fallback_click_count", 1)))),
            "fallback_click_interval_ms": max(0, min(60000, int(config.get("fallback_click_interval_ms", 100)))),
            "fallback_present": False,
            "ignore_background": bool(config.get("ignore_background", False)),
            "recognize": str(config.get("recognize", "")),
            "expected_text": str(config.get("expected_text", "")),
            "match_mode": str(config.get("match_mode", "contains")),
            "wait_text_absent": bool(config.get("wait_text_absent", False)),
            "target_absent_armed": False,
            "click_count": max(1, min(9999, int(config.get("click_count", 1)))),
            "ocr_offset_up": int(config.get("ocr_offset_up", 0)),
            "ocr_offset_down": int(config.get("ocr_offset_down", 0)),
            "ocr_offset_left": int(config.get("ocr_offset_left", 0)),
            "ocr_offset_right": int(config.get("ocr_offset_right", 0)),
            "hold_ms": hold,
            "hold_enabled": bool(config.get("hold_enabled", True)),
            "delay_ms": restart_delay,
            "region_mode": region_mode,
            "region": (
                tuple(int(part) for part in region)
                if isinstance(region, (list, tuple)) and len(region) == 4 else None
            ),
            "click": (
                (int(click[0]), int(click[1]))
                if isinstance(click, (list, tuple)) and len(click) == 2 else None
            ),
            "jump_row": jump_row,
            "jump_action_id": str(config.get("jump_action_id", "")).strip(),
            "module_ref": module_ref,
            "module_key": module_ref_key,
            "module_display_name": module_display_name,
            "after_action": after_action,
            "button": button,
            "second": second,
            "segment": segment,
            "success_segment": segment,
            "segment_ready": False,
            "timeout_enabled": timeout_enabled,
            "not_found_timeout_ms": not_found_timeout_ms,
            "timeout_segment": timeout_segment,
            "timeout_triggered": False,
            "not_found_since": time.perf_counter(),
            "trigger_kind": "success",
            "shared_capture": (
                str(config.get("recognize", "")) != "text" and module is None
            ),
            "capture_generation": 0,
            "was_detected": False,
            "triggered": False,
            "awaiting_clear": key in rearm_locks,
            "awaiting_clear_logged": False,
            "match_since": None,
            "match_data": None,
            "last_ocr_observation": None,
        }
        self.global_detect_monitors[key] = monitor
        self._start_global_detect_monitor(monitor)
        name = monitor["template"].name or "未设置"
        if region_mode == "window":
            region_text = "目标窗口"
        elif region_mode == "template":
            region_text = "模板区域" if monitor["region"] else "模板未设置区域，按全屏检测"
        elif region_mode == "custom" and monitor["region"]:
            region_text = ",".join(str(part) for part in monitor["region"])
        else:
            region_text = "全屏"
        if monitor["module"]:
            tail = "先执行全局模块步骤，再继续原工作流。"
        elif monitor["jump_row"] or monitor.get("jump_action_id"):
            if monitor.get("jump_action_id") == NEXT_WORKFLOW_STEP_TARGET_ID:
                tail = "结束当前脚本，执行工作流下一项。"
            else:
                jump_row = self._resolve_module_jump_row(monitor)
                monitor["jump_row"] = jump_row
                tail = f"跳转到{self._module_jump_target_text(jump_row)}执行，播放到末尾后结束。"
        else:
            tail = "执行脚本动作，再继续检测。"
        hold_text = f"持续超过 {hold} ms" if monitor.get("hold_enabled", True) else "识别到立即执行"
        start_delay_text = (
            f" · {start_delay} ms 后开始识别" if monitor.get("start_delay_ms", 0) else ""
        )
        self._ui(
            self._log,
            f"全局检测已启用：模块[{module_display_name}] · {name} · 区域 {region_text} · {hold_text}"
            f"{start_delay_text} · "
            f"触发后{tail}",
        )

    @staticmethod
    def _global_monitor_subject(monitor: dict, subject: str) -> str:
        """给全局检测日志补上实际触发的模块名，避免同图多模块时无法追溯。"""
        module_name = str(monitor.get("module_display_name", "")).strip() or "脚本全局模块"
        return f"模块[{module_name}] · {subject}"

    def _start_global_detect_monitor(self, monitor: dict):
        monitor["stop"] = threading.Event()
        monitor["was_detected"] = False
        monitor["triggered"] = False
        monitor["match_since"] = None
        monitor["not_found_since"] = time.perf_counter()
        monitor["timeout_triggered"] = False
        monitor["thread"] = threading.Thread(
            target=self._global_detect_worker, args=(monitor,), daemon=True,
        )
        monitor["thread"].start()

    def _wait_workflow_global_scan_turn(self, monitor: dict) -> bool:
        """Gate workflow-global monitors in list order and pin one screenshot per round."""
        condition = getattr(self, "workflow_global_scan_condition", None)
        if condition is None:
            condition = self.workflow_global_scan_condition = threading.Condition()
            self.workflow_global_scan_turn = 0
            self.workflow_global_scan_screen = None
            self.workflow_global_scan_origin = (0, 0)
            self.workflow_global_scan_paused = False
        while not monitor["stop"].is_set() and not getattr(self, "exiting", False):
            with condition:
                active = [
                    item for item in self.global_detect_monitors.values()
                    if item.get("module") is not None and not item["stop"].is_set()
                ]
                if monitor not in active:
                    return False
                if self.workflow_global_scan_paused \
                        or self.global_detect_module_running \
                        or getattr(self, "global_detect_pending_restart", False):
                    condition.wait(0.1)
                    continue
                turn = min(int(self.workflow_global_scan_turn), len(active) - 1)
                if active[turn] is not monitor:
                    condition.wait(0.1)
                    continue
                need_capture = turn == 0 and self.workflow_global_scan_screen is None
            if need_capture:
                try:
                    screen, origin = capture_bgr()
                except Exception as exc:
                    self._ui(self._log, f"全局检测：工作流共享屏幕截图失败：{exc}")
                    if monitor["stop"].wait(max(100, int(monitor.get("interval_ms", 500))) / 1000):
                        return False
                    continue
                with condition:
                    if self.workflow_global_scan_screen is None:
                        self.workflow_global_scan_screen = screen
                        self.workflow_global_scan_origin = origin
            with condition:
                monitor["workflow_scan_screen"] = self.workflow_global_scan_screen
                monitor["workflow_scan_origin"] = self.workflow_global_scan_origin
            return True
        return False

    def _finish_workflow_global_scan_turn(self, monitor: dict, pause: bool = False) -> None:
        condition = getattr(self, "workflow_global_scan_condition", None)
        if condition is None:
            return
        with condition:
            active = [
                item for item in self.global_detect_monitors.values()
                if item.get("module") is not None and not item["stop"].is_set()
            ]
            if pause:
                self.workflow_global_scan_paused = True
            elif active:
                self.workflow_global_scan_turn += 1
                if self.workflow_global_scan_turn >= len(active):
                    self.workflow_global_scan_turn = 0
                    self.workflow_global_scan_screen = None
                    self.workflow_global_scan_origin = (0, 0)
            condition.notify_all()

    def _workflow_global_match(self, monitor: dict, template: Path, threshold: float,
                               region: tuple[int, int, int, int] | None,
                               ignore_background: bool = False):
        screen = monitor.get("workflow_scan_screen")
        if screen is None:
            return None
        return find_template_in_image(
            template, screen, threshold,
            monitor.get("workflow_scan_origin", (0, 0)), region, ignore_background,
        )

    def _ensure_global_capture_worker(self) -> None:
        condition = getattr(self, "global_capture_condition", None)
        if condition is None:
            condition = self.global_capture_condition = threading.Condition()
            self.global_capture_thread = None
            self.global_capture_generation = 0
            self.global_capture_screen = None
            self.global_capture_origin = (0, 0)
        thread = getattr(self, "global_capture_thread", None)
        if thread is not None and thread.is_alive():
            with condition:
                condition.notify_all()
            return
        self.global_capture_thread = threading.Thread(
            target=self._global_capture_worker, daemon=True,
        )
        self.global_capture_thread.start()

    def _global_capture_worker(self) -> None:
        """Capture once per global scan round and publish it to every monitor."""
        condition = self.global_capture_condition
        while not getattr(self, "exiting", False):
            monitors = [
                item for item in list(getattr(self, "global_detect_monitors", {}).values())
                if item.get("shared_capture") and not item.get("stop", threading.Event()).is_set()
            ]
            if not monitors:
                with condition:
                    condition.wait(0.2)
                continue
            interval_ms = max(100, min(int(item.get("interval_ms", 500)) for item in monitors))
            try:
                screen, origin = capture_bgr()
            except Exception as exc:
                self._ui(self._log, f"全局检测：共享屏幕截图失败：{exc}")
                with condition:
                    condition.wait(interval_ms / 1000)
                continue
            with condition:
                self.global_capture_screen = screen
                self.global_capture_origin = origin
                self.global_capture_generation += 1
                condition.notify_all()
                condition.wait(interval_ms / 1000)

    def _shared_global_match(self, monitor: dict, template: Path, threshold: float,
                             region: tuple[int, int, int, int] | None,
                             ignore_background: bool = False):
        """Wait for the next shared screenshot, then match only in memory."""
        condition = self.global_capture_condition
        last_generation = int(monitor.get("capture_generation", 0))
        with condition:
            while (
                self.global_capture_generation <= last_generation
                and not monitor["stop"].is_set()
                and not getattr(self, "exiting", False)
            ):
                condition.wait(0.2)
            if monitor["stop"].is_set() or getattr(self, "exiting", False):
                return None
            screen = self.global_capture_screen
            origin = self.global_capture_origin
            monitor["capture_generation"] = self.global_capture_generation
        if screen is None:
            return None
        return find_template_in_image(template, screen, threshold, origin, region,
                                      ignore_background)

    def _stop_all_global_detect_monitors(self, clear: bool = False):
        for monitor in list(self.global_detect_monitors.values()):
            monitor["stop"].set()
        if clear:
            self.global_detect_monitors.clear()
            scan_condition = getattr(self, "workflow_global_scan_condition", None)
            if scan_condition is not None:
                with scan_condition:
                    self.workflow_global_scan_turn = 0
                    self.workflow_global_scan_screen = None
                    self.workflow_global_scan_origin = (0, 0)
                    self.workflow_global_scan_paused = False
                    scan_condition.notify_all()
        condition = getattr(self, "global_capture_condition", None)
        if condition is not None:
            with condition:
                condition.notify_all()

    def _clear_global_detect_rearm_locks(self):
        locks = getattr(self, "global_detect_rearm_locks", None)
        if locks is not None:
            locks.clear()

    def _global_ocr_match_data(self, region) -> dict | None:
        """识别文字命中时的伪匹配：点击"识别区域"用区域中心；全屏用主屏中心。"""
        if region:
            x, y, w, h = (int(part) for part in region)
        else:
            import mss

            with mss.mss() as grabber:
                monitor = grabber.monitors[1]
            x, y, w, h = (int(monitor[k]) for k in ("left", "top", "width", "height"))
        return {
            "x": x, "y": y, "width": w, "height": h,
            "center_x": x + w // 2, "center_y": y + h // 2,
            "score": 1.0,
        }

    def _global_detect_worker(self, monitor: dict):
        stop = monitor["stop"]
        start_delay_ms = max(0, int(monitor.get("start_delay_ms", 0)))
        if start_delay_ms:
            if stop.wait(start_delay_ms / 1000):
                return
            monitor["not_found_since"] = time.perf_counter()
        if monitor.get("shared_capture"):
            self._ensure_global_capture_worker()
        warned_missing_template = False
        warned_find_error = False
        warned_missing_module = False
        while not stop.is_set():
            workflow_global = monitor.get("module") is not None
            scan_turn = workflow_global and monitor.get("recognize") != "none"
            if scan_turn and not self._wait_workflow_global_scan_turn(monitor):
                break
            template = monitor["template"]
            threshold = monitor["threshold"]
            interval = monitor["interval_ms"]
            region = monitor["region"]
            if monitor.get("module_ref"):
                # 引用模块：每轮实时重读对象（阈值 / 间隔 / 区域 / 持续时长），
                # 修改对象即时生效；缺对象告警一次后沿用当前值。
                obj = registered_module_object(str(monitor.get("module_key") or template))
                if obj is not None:
                    warned_missing_module = False
                    try:
                        obj_template = str(obj.get("template", "")).strip()
                        if obj_template:
                            template = resolve_path(obj_template)
                            monitor["template"] = template
                        threshold = max(0.1, min(1.0, float(obj.get("threshold", threshold))))
                        interval = max(100, min(10000, int(obj.get("interval_ms", interval))))
                        monitor["ignore_background"] = bool(obj.get("ignore_background", False))
                        monitor["hold_enabled"] = bool(obj.get("hold_enabled", True))
                        monitor["hold_ms"] = max(0, int(obj.get("hold_ms", monitor["hold_ms"])))
                        monitor["timeout_enabled"] = bool(
                            obj.get("run_code_on_timeout", False)
                        ) and not bool(obj.get("wait_text_absent", False))
                        monitor["not_found_timeout_ms"] = max(
                            0, int(obj.get(
                                "not_found_timeout_ms",
                                monitor.get("not_found_timeout_ms", 3000),
                            ))
                        )
                        monitor["timeout_segment"] = list(obj.get("on_timeout_actions") or [])
                        monitor["success_segment"] = list(obj.get("on_success_actions") or []) \
                            if bool(obj.get("run_code_after_action", False)) else []
                        monitor["recognize"] = str(obj.get("recognize", ""))
                        monitor["expected_text"] = str(obj.get("expected_text", ""))
                        monitor["match_mode"] = str(obj.get("match_mode", "contains"))
                        monitor["wait_text_absent"] = bool(
                            obj.get("wait_text_absent", False)
                        )
                        monitor["click_count"] = max(
                            1, min(9999, int(obj.get("click_count", 1)))
                        )
                        monitor["fallback_module_key"] = str(
                            obj.get("fallback_module_key", "")
                        ).strip()
                        monitor["fallback_click"] = bool(obj.get("fallback_click", False))
                        monitor["fallback_click_count"] = max(
                            1, min(9999, int(obj.get("fallback_click_count", 1)))
                        )
                        monitor["fallback_click_interval_ms"] = max(
                            0, min(60000, int(obj.get("fallback_click_interval_ms", 100)))
                        )
                        for field in (
                            "ocr_offset_up", "ocr_offset_down",
                            "ocr_offset_left", "ocr_offset_right",
                        ):
                            monitor[field] = max(0, int(obj.get(field, 0)))
                        if not monitor["wait_text_absent"]:
                            monitor["target_absent_armed"] = False
                    except (TypeError, ValueError):
                        pass
                    raw_region = obj.get("region") or []
                    if len(raw_region) == 4 and raw_region[2] > 0 and raw_region[3] > 0:
                        region = tuple(int(part) for part in raw_region)
                    else:
                        region = None
                elif not warned_missing_module:
                    warned_missing_module = True
                    missing_name = str(monitor.get("module_key") or template).replace("\\", "/").rsplit("/", 1)[-1]
                    self._ui(
                        self._log,
                        f"全局检测：引用模块 {missing_name} 不存在，沿用当前配置。",
                    )
            if monitor["region_mode"] == "window":
                # 目标窗口模式：每轮取当前绑定的窗口区域（无窗口时退化为全屏）。
                region = get_window_rect(self._bound_hwnd())
            detected = False
            match = None
            if monitor.get("recognize") == "none":
                # 无需识图的全局模块只走“未识别时限”计时，到时执行超时代码段。
                detected = False
                warned_missing_template = False
            elif monitor.get("recognize") == "text":
                try:
                    if workflow_global:
                        screen = monitor.get("workflow_scan_screen")
                        origin_x, origin_y = monitor.get("workflow_scan_origin", (0, 0))
                        if region and screen is not None:
                            x, y, width, height = region
                            left = max(0, int(x) - int(origin_x))
                            top = max(0, int(y) - int(origin_y))
                            right = min(screen.shape[1], left + max(1, int(width)))
                            bottom = min(screen.shape[0], top + max(1, int(height)))
                            ocr_screen = screen[top:bottom, left:right]
                            ocr_origin = (int(origin_x) + left, int(origin_y) + top)
                        else:
                            ocr_screen = screen
                            ocr_origin = (int(origin_x), int(origin_y))
                        if ocr_screen is not None:
                            recognized, ocr_matches = recognize_image_with_boxes(
                                ocr_screen, ocr_origin,
                            )
                        else:
                            recognized, ocr_matches = "", []
                    else:
                        recognized, ocr_matches = recognize_region_with_boxes(region)
                    expected_text = str(monitor.get("expected_text", ""))
                    match_mode = str(monitor.get("match_mode", "contains"))
                    ocr_match = find_expected_match(ocr_matches, expected_text, match_mode)
                    text_present = ocr_match is not None
                    if not text_present and matches_expected(recognized, expected_text, match_mode):
                        text_present = True
                        ocr_match = self._global_ocr_match_data(region)
                    observation = format_ocr_observation(
                        recognized, expected_text, text_present,
                        str(monitor.get("expected_text", "")).strip() or "全局文字模块",
                    )
                    if observation != monitor.get("last_ocr_observation"):
                        monitor["last_ocr_observation"] = observation
                        self._ui(self._log, observation)
                        self._ui(self._append_mini_step, observation)
                    if monitor.get("wait_text_absent"):
                        if text_present:
                            if not monitor.get("target_absent_armed"):
                                monitor["target_absent_armed"] = True
                                self._ui(
                                    self._log,
                                    f"全局检测：{self._global_monitor_subject(monitor, '已识别到期望文字')}，开始等待文字消失。",
                                )
                            detected = False
                        else:
                            detected = bool(monitor.get("target_absent_armed"))
                    else:
                        detected = text_present
                    warned_find_error = False
                    match = (
                        self._global_ocr_match_data(region)
                        if detected and monitor.get("wait_text_absent") else
                        ocr_match if detected else None
                    )
                    monitor["match_data"] = match
                except Exception as exc:
                    # 识别出错也要可见：成功一次前只记录一次，避免无限刷屏。
                    if not warned_find_error:
                        warned_find_error = True
                        self._ui(self._log, f"全局检测：OCR 识别失败：{exc}")
                    detected = False
            elif template.is_file():
                warned_missing_template = False
                try:
                    if workflow_global:
                        match = self._workflow_global_match(
                            monitor, template, threshold, region,
                            ignore_background=bool(monitor.get("ignore_background", False)),
                        )
                    elif monitor.get("shared_capture"):
                        match = self._shared_global_match(
                            monitor, template, threshold, region,
                            ignore_background=bool(monitor.get("ignore_background", False)),
                        )
                    else:
                        match = find_template(
                            template, threshold, region,
                            ignore_background=bool(monitor.get("ignore_background", False)),
                        )
                    warned_find_error = False
                    template_present = match is not None
                    if monitor.get("wait_text_absent"):
                        if template_present:
                            if not monitor.get("target_absent_armed"):
                                monitor["target_absent_armed"] = True
                                self._ui(
                                    self._log,
                                    f"全局检测：{self._global_monitor_subject(monitor, '已识别到目标模板')}，开始等待模板消失。",
                                )
                            monitor["last_present_match"] = dict(match)
                            detected = False
                        else:
                            detected = bool(monitor.get("target_absent_armed"))
                            match = monitor.get("last_present_match") if detected else None
                    else:
                        detected = template_present
                except Exception as exc:
                    # 识别出错也要可见：成功一次前只记录一次，避免无限刷屏。
                    if not warned_find_error:
                        warned_find_error = True
                        self._ui(self._log, f"全局检测：识别失败：{exc}")
                    detected = False
            elif not warned_missing_template:
                # 模板文件不存在是"加了没反应"的常见原因，必须让用户看得到。
                warned_missing_template = True
                self._ui(self._log, f"全局检测：模板图片不存在，跳过检测：{template}")
            fallback_key = str(monitor.get("fallback_module_key", "")).strip()
            if not detected and fallback_key:
                fallback_obj = registered_module_object(fallback_key)
                fallback_match = self._match_global_fallback_module(fallback_obj)
                if fallback_match and not monitor.get("fallback_present"):
                    monitor["fallback_present"] = True
                    fallback_name = str((fallback_obj or {}).get("name") or "备用识别模块")
                    show_overlay(
                        fallback_match["x"], fallback_match["y"],
                        fallback_match["width"], fallback_match["height"],
                    )
                    if monitor.get("fallback_click"):
                        count = max(1, min(9999, int(monitor.get("fallback_click_count", 1))))
                        interval_ms = max(0, min(60000, int(monitor.get("fallback_click_interval_ms", 100))))
                        button = str((fallback_obj or {}).get("button", "left"))
                        for _ in range(count):
                            send_move_absolute(
                                int(fallback_match["center_x"]), int(fallback_match["center_y"]),
                            )
                            send_button(button, True)
                            time.sleep(0.03)
                            send_button(button, False)
                            if _ + 1 < count:
                                time.sleep(interval_ms / 1000)
                        self._ui(self._log, f"全局检测：备用模块 {fallback_name} 已识别并点击，继续识别主模块。")
                    else:
                        self._ui(self._log, f"全局检测：备用模块 {fallback_name} 已识别，继续识别主模块。")
                elif not fallback_match:
                    monitor["fallback_present"] = False
            subject = (
                str(monitor.get("expected_text", "")).strip() or "识别文字"
                if monitor.get("recognize") == "text" else
                "无需识图" if monitor.get("recognize") == "none" else template.name
            )
            condition_subject = (
                f"{subject} 已消失" if monitor.get("wait_text_absent") else subject
            )
            condition_subject = self._global_monitor_subject(monitor, condition_subject)
            absent_target_name = (
                "期望文字" if monitor.get("recognize") == "text" else "目标模板"
            )
            now = time.perf_counter()
            # 断点恢复会重建监控。新一代监控必须亲自确认图片消失，旧监控
            # 退出前看到的短暂消失不能解除锁定，否则同一画面会立即再次触发。
            if monitor.get("awaiting_clear"):
                if detected:
                    if not monitor.get("awaiting_clear_logged"):
                        monitor["awaiting_clear_logged"] = True
                        self._ui(
                            self._log,
                            (
                                f"全局检测：{condition_subject} 刚刚已触发，"
                                f"等待{absent_target_name}重新出现后再允许下次触发。"
                                if monitor.get("wait_text_absent") else
                        f"全局检测：{condition_subject} 刚刚已触发，图片仍存在，"
                                "等待消失后再允许下次触发。"
                            ),
                        )
                else:
                    locks = getattr(self, "global_detect_rearm_locks", None)
                    if locks is not None:
                        locks.discard(str(monitor.get("key", "")))
                    monitor["awaiting_clear"] = False
                    monitor["awaiting_clear_logged"] = False
                    monitor["was_detected"] = False
                    monitor["triggered"] = False
                    monitor["match_since"] = None
                    monitor["not_found_since"] = time.perf_counter()
                    self._ui(
                        self._log,
                        (
                            f"全局检测：{absent_target_name}已重新出现，允许下次消失时再次触发。"
                            if monitor.get("wait_text_absent") else
                            f"全局检测：{condition_subject} 已确认消失，允许下次出现时再次触发。"
                        ),
                    )
                if scan_turn:
                    self._finish_workflow_global_scan_turn(monitor)
                if stop.wait(max(100, interval) / 1000):
                    break
                continue
            if detected:
                monitor["not_found_since"] = None
                monitor["timeout_triggered"] = False
                if not monitor["was_detected"]:
                    monitor["was_detected"] = True
                    monitor["match_since"] = now
                    if match:
                        # 记下最新匹配位置：未配置固定点击位置时点这里。
                        monitor["match_data"] = dict(match)
                        self._ui(
                            self._log,
                            f"全局检测：识别到 {condition_subject} @ "
                            f"({match['center_x']}, {match['center_y']})，"
                            + (f"等待持续超过 {monitor['hold_ms']} ms 后触发。"
                               if monitor.get("hold_enabled", True) else "立即触发。"),
                        )
                    else:
                        self._ui(
                            self._log,
                            f"全局检测：识别到 {condition_subject}，"
                            + (f"等待持续超过 {monitor['hold_ms']} ms 后触发。"
                               if monitor.get("hold_enabled", True) else "立即触发。"),
                        )
                    if match:
                        # 识别到：在匹配区域周围画一圈细边框提醒。
                        show_overlay(
                            match["x"], match["y"],
                            match["width"], match["height"],
                        )
                hold_ms = monitor["hold_ms"] if monitor.get("hold_enabled", True) else 0
                elapsed_ms = (now - (monitor["match_since"] or now)) * 1000
                if not monitor["triggered"] and elapsed_ms >= hold_ms:
                    monitor["triggered"] = True
                    locks = getattr(self, "global_detect_rearm_locks", None)
                    if locks is None:
                        locks = self.global_detect_rearm_locks = set()
                    locks.add(str(monitor.get("key", "")))
                    if match:
                        show_overlay(
                            match["x"], match["y"],
                            match["width"], match["height"],
                        )
                    self._ui(self._on_global_detect_match, monitor)
            else:
                old_generation_after_trigger = bool(
                    monitor.get("workflow_resume_snapshot")
                    and str(monitor.get("key", ""))
                    in getattr(self, "global_detect_rearm_locks", set())
                )
                if monitor["was_detected"] and not old_generation_after_trigger:
                    self._ui(
                        self._log,
                        f"全局检测：{self._global_monitor_subject(monitor, absent_target_name + '已重新出现')}，消失计时重置。"
                        if monitor.get("wait_text_absent") else
                        f"全局检测：{self._global_monitor_subject(monitor, '图片已消失')}，计时重置。",
                    )
                monitor["was_detected"] = False
                monitor["triggered"] = False
                monitor["match_since"] = None
                if monitor.get("not_found_since") is None:
                    monitor["not_found_since"] = now
                timeout_elapsed = (now - monitor["not_found_since"]) * 1000
                if (
                    monitor.get("timeout_enabled")
                    and not monitor.get("timeout_triggered")
                    and timeout_elapsed >= int(monitor.get("not_found_timeout_ms", 3000))
                ):
                    monitor["timeout_triggered"] = True
                    self._ui(self._on_global_detect_timeout, monitor)
            if scan_turn:
                self._finish_workflow_global_scan_turn(
                    monitor,
                    pause=bool(monitor.get("triggered") or monitor.get("timeout_triggered")),
                )
            if stop.wait(max(100, interval) / 1000):
                break

    @staticmethod
    def _match_global_fallback_module(obj: dict | None) -> dict | None:
        if not obj or obj.get("recognize") in ("number", "none"):
            return None
        raw_region = obj.get("region") or []
        region = (
            tuple(map(int, raw_region))
            if len(raw_region) == 4 and int(raw_region[2]) > 0 and int(raw_region[3]) > 0
            else None
        )
        if obj.get("recognize") == "text":
            recognized, boxes = recognize_region_with_boxes(region)
            expected = str(obj.get("expected_text", ""))
            mode = str(obj.get("match_mode", "contains"))
            match = find_expected_match(boxes, expected, mode)
            if match is None and matches_expected(recognized, expected, mode):
                if region:
                    x, y, width, height = region
                    return {
                        "x": x, "y": y, "width": width, "height": height,
                        "center_x": x + width // 2, "center_y": y + height // 2,
                    }
            return match
        template = resolve_path(str(obj.get("template", "")))
        if not template.is_file():
            return None
        return find_template(
            template, min(1.0, max(0.1, float(obj.get("threshold", 0.85)))), region,
            ignore_background=bool(obj.get("ignore_background", False)),
        )

    def _prepare_global_detect_interrupt(self, monitor: dict) -> bool:
        if self.global_detect_module_running or getattr(self, "global_detect_pending_restart", False):
            return False
        if monitor["stop"].is_set():
            return False
        self.global_detect_trigger_count += 1
        self.global_detect_jump_current_script_last = False
        self.global_detect_jumped_referenced_last = False
        if self.current_workflow_step_index is not None:
            # 从触发开始到模块执行完并恢复为止只允许一个全局模块进入。
            # 该标志也阻止恢复初始化阶段排队中的旧监控重新注册/再次触发。
            self.global_detect_pending_restart = True
            snapshot = (
                self.current_workflow_step_index,
                self.current_workflow_repeat_index,
                self.current_workflow_action_index,
            )
            monitor["workflow_resume_snapshot"] = snapshot
            self.global_detect_resume_index = snapshot[0]
            self.global_detect_resume_repeat = snapshot[1]
            self.global_detect_resume_action = snapshot[2]
            self.global_detect_end_current_script = False
            self.global_detect_advance_workflow_step = (
                str(monitor.get("jump_action_id", "")).strip()
                == NEXT_WORKFLOW_STEP_TARGET_ID
            )
            workflow_stop = getattr(self, "workflow_stop", None)
            if workflow_stop is not None:
                workflow_stop.set()
            player = getattr(self, "player", None)
            if player is not None:
                player.stop()
        return True

    def _on_global_detect_match(self, monitor: dict):
        if not self._prepare_global_detect_interrupt(monitor):
            return
        monitor["trigger_kind"] = "success"
        name = (
            str(monitor.get("expected_text", "")).strip() or "识别文字"
            if monitor.get("recognize") == "text" else monitor["template"].name
        )
        module = monitor["module"]
        subject = self._global_monitor_subject(monitor, name)
        if module and str(module.get("script", "")).strip():
            self._log(f"全局检测触发：检测到 {subject}，先执行模块脚本动作，再继续原工作流。")
        else:
            self._log(f"全局检测触发：检测到 {subject}，开始执行脚本动作。")
        threading.Thread(
            target=self._run_global_detect_action, args=(monitor,), daemon=True,
        ).start()

    def _on_global_detect_timeout(self, monitor: dict):
        if not self._prepare_global_detect_interrupt(monitor):
            monitor["timeout_triggered"] = False
            return
        timeout_ms = int(monitor.get("not_found_timeout_ms", 3000))
        monitor["trigger_kind"] = "timeout"
        self.global_detect_end_current_script = False
        self.global_detect_advance_workflow_step = False
        monitor["segment"] = list(monitor.get("timeout_segment") or [])
        monitor["segment_ready"] = bool(monitor["segment"])
        subject = (
            str(monitor.get("expected_text", "")).strip() or "识别文字"
            if monitor.get("recognize") == "text" else monitor["template"].name
        )
        subject = self._global_monitor_subject(monitor, subject)
        self._log(
            f"全局检测：连续 {timeout_ms} ms 未识别到 {subject}，"
            f"执行超时代码段（{len(monitor['segment'])} 个动作）。"
        )
        self._after_global_detect_action(monitor)

    def _ensure_global_click_foreground(self, x: int, y: int) -> None:
        """全局检测点击前把目标窗口带到前台。

        全局检测的点击是桌面级 SendInput：当绑定窗口（游戏）不在前台时，点击
        会落在该坐标最上层的其他窗口上，激活的窗口随之改变——被激活的窗口可能
        遮挡游戏画面或引发画面短暂变化，全局检测据此解除“等待图片消失”锁，
        图片重现后同一个模块会再次触发，产生重复点击。这里在点击点落在绑定
        窗口矩形范围内时先把它前置，保证点击真正到达目标窗口。
        """
        hwnd = self._bound_hwnd()
        if not hwnd or is_window_process_foreground(hwnd):
            return
        rect = get_window_rect(hwnd)
        if not rect:
            return
        left, top, width, height = rect
        if left <= int(x) < left + width and top <= int(y) < top + height:
            if activate_window(hwnd):
                self._ui(self._log, "全局检测：点击前已把目标窗口置于前台。")
            else:
                self._ui(
                    self._log,
                    "全局检测：点击前未能把目标窗口置于前台，点击可能落在其他窗口上。",
                )

    def _run_global_detect_action(self, monitor: dict):
        # 触发条件只负责识别；点击仅保留给旧配置（模块配置里显式设置了点击位置，
        # 或旧独立脚本没有语句体、没有跳转行时点击识别到的位置）。
        if monitor.get("module_ref"):
            self._run_global_detect_module_ref(monitor)
            return
        click = monitor["click"]
        if not click and not self.standalone_global_replay and not monitor.get("jump_row"):
            # 旧独立脚本（无语句体）兼容：点击识别到的位置（与识图动作默认行为一致）。
            match = monitor.get("match_data")
            if match:
                click = (match["center_x"], match["center_y"])
        if click:
            try:
                before = get_cursor_pos()
                self._ensure_global_click_foreground(click[0], click[1])
                send_move_absolute(click[0], click[1])
                after_move = get_cursor_pos()
                send_button("left", True)
                time.sleep(0.03)
                send_button("left", False)
                cursor = get_cursor_pos()
                self._ui(self._log, f"全局检测已点击 ({click[0]}, {click[1]})")
                self._ui(self._append_mini_step, f"全局检测已点击 ({click[0]}, {click[1]})")
                if abs(cursor[0] - click[0]) > 5 or abs(cursor[1] - click[1]) > 5:
                    # SendInput 可能返回成功但事件被系统丢弃（如游戏以管理员
                    # 身份运行而 MacroFlow 不是），光标位置对不上就是证据。
                    self._ui(
                        self._log,
                        f"[调试] 点击前光标 {before} / 移动后立即 {after_move} / "
                        f"点击后 {cursor}，目标 {click}",
                    )
                    self._ui(
                        self._log,
                        f"全局检测：点击后光标在 ({cursor[0]}, {cursor[1]})，"
                        f"与目标 ({click[0]}, {click[1]}) 不一致，点击可能未生效。",
                    )
            except Exception as exc:
                self._ui(self._log, f"全局检测点击失败：{exc}")
            delay = monitor["delay_ms"]
            if delay and monitor["stop"].wait(delay / 1000):
                return
        self._ui(self._after_global_detect_action, monitor)

    def _run_global_detect_module_ref(self, monitor: dict):
        """引用模块触发后的动作 B 分发（触发线程）。

        先延时 A，再执行对象主动作；若启用了附加代码段，只置 segment_ready
        标志。代码段由主线程在 player 停止后执行，完成后才恢复原工作流。
        """
        stop = monitor["stop"]
        delay = monitor["delay_ms"]
        if delay and stop.wait(delay / 1000):
            return
        after_action = monitor.get("after_action", "click_match")
        monitor["segment"] = list(
            monitor.get("success_segment", monitor.get("segment") or []) or []
        )
        if monitor.get("segment"):
            monitor["segment_ready"] = True
        if after_action == "continue":
            self._ui(self._after_global_detect_action, monitor)
            return
        click = None
        if after_action == "click_match":
            match = monitor.get("match_data")
            if match:
                click = (
                    match["center_x"]
                    + int(monitor.get("ocr_offset_right", 0))
                    - int(monitor.get("ocr_offset_left", 0)),
                    match["center_y"]
                    + int(monitor.get("ocr_offset_down", 0))
                    - int(monitor.get("ocr_offset_up", 0)),
                )
        elif after_action == "click_custom":
            click = monitor.get("click")
        if click:
            button = monitor.get("button", "left")
            click_count = max(1, min(9999, int(monitor.get("click_count", 1))))
            try:
                before = get_cursor_pos()
                self._ensure_global_click_foreground(click[0], click[1])
                send_move_absolute(click[0], click[1])
                after_move = get_cursor_pos()
                completed = 0
                for index in range(click_count):
                    if stop.is_set():
                        break
                    send_button(button, True)
                    time.sleep(0.03)
                    send_button(button, False)
                    completed += 1
                    if index + 1 < click_count and stop.wait(0.05):
                        break
                cursor = get_cursor_pos()
                count_text = f" × {completed}" if completed > 1 else ""
                self._ui(self._log, f"全局检测已点击 ({click[0]}, {click[1]}){count_text}")
                self._ui(
                    self._append_mini_step,
                    f"全局检测已点击 ({click[0]}, {click[1]}){count_text}",
                )
                if abs(cursor[0] - click[0]) > 5 or abs(cursor[1] - click[1]) > 5:
                    self._ui(
                        self._log,
                        f"全局检测：点击后光标在 ({cursor[0]}, {cursor[1]})，"
                        f"与目标 ({click[0]}, {click[1]}) 不一致，点击可能未生效。",
                    )
            except Exception as exc:
                self._ui(self._log, f"全局检测点击失败：{exc}")
        if after_action == "second_match":
            self._poll_second_match_click(monitor)
            return
        self._ui(self._after_global_detect_action, monitor)

    def _poll_second_match_click(self, monitor: dict):
        """二次识别后点击（触发线程）：轮询另一模板（可限定区域），找到点中心。

        阻塞识别无限等；否则至 second_match_timeout_ms 超时后按 continue 继续。
        """
        second = monitor.get("second")
        if not second or not second.get("template"):
            self._ui(self._log, "全局检测：二次识别模板未配置，跳过。")
            self._ui(self._after_global_detect_action, monitor)
            return
        template = resolve_path(second["template"])
        if not template.is_file():
            self._ui(self._log, f"全局检测：二次识别模板不存在：{template}")
            self._ui(self._after_global_detect_action, monitor)
            return
        region = None
        # 二次模板直接沿用模块对象仓库里登记的框选区域；没有有效区域则全屏。
        raw_region = registered_template_region(str(second.get("template", ""))) or []
        if len(raw_region) == 4 and raw_region[2] > 0 and raw_region[3] > 0:
            region = tuple(int(part) for part in raw_region)
        threshold = monitor["threshold"]
        interval = monitor["interval_ms"]
        timeout_ms = max(0, int(second.get("timeout_ms", 3000)))
        blocking = bool(second.get("blocking", False))
        button = monitor.get("button", "left")
        stop = monitor["stop"]
        deadline = time.perf_counter() + timeout_ms / 1000
        while not stop.is_set():
            try:
                match = find_template(
                    template, threshold, region,
                    ignore_background=bool(monitor.get("ignore_background", False)),
                )
            except Exception as exc:
                self._ui(self._log, f"全局检测：二次识别失败：{exc}")
                match = None
            if match is not None:
                show_overlay(match["x"], match["y"], match["width"], match["height"])
                click_target = str(second.get("click_target", "second"))
                first_match = monitor.get("match_data") or {}
                click_region = second.get("click_region") or []
                if click_target == "first" and first_match:
                    center = (first_match["center_x"], first_match["center_y"])
                    target_label = "第一次识别位置"
                elif click_target == "custom_region" and len(click_region) == 4:
                    center = (
                        int(click_region[0]) + int(click_region[2]) // 2,
                        int(click_region[1]) + int(click_region[3]) // 2,
                    )
                    target_label = "自定义框选区域"
                else:
                    center = (match["center_x"], match["center_y"])
                    target_label = "第二次识别位置"
                completed = 0
                try:
                    self._ensure_global_click_foreground(center[0], center[1])
                    send_move_absolute(center[0], center[1])
                    click_count = max(
                        1, min(9999, int(monitor.get("click_count", 1)))
                    )
                    for index in range(click_count):
                        if stop.is_set():
                            break
                        send_button(button, True)
                        time.sleep(0.03)
                        send_button(button, False)
                        completed += 1
                        if index + 1 < click_count and stop.wait(0.05):
                            break
                except Exception as exc:
                    self._ui(self._log, f"全局检测：二次识别点击失败：{exc}")
                self._ui(
                    self._log,
                    f"全局检测：二次识别到 {template.name}，点击{target_label} "
                    f"({center[0]}, {center[1]})"
                    + (f" × {completed}" if completed > 1 else ""),
                )
                self._ui(self._append_mini_step, "全局检测：二次识别已点击。")
                break
            if not blocking and time.perf_counter() >= deadline:
                self._ui(
                    self._log,
                    f"全局检测：二次识别超时（{timeout_ms} ms），继续。",
                )
                break
            if stop.wait(max(100, interval) / 1000):
                return
        self._ui(self._after_global_detect_action, monitor)

    def _resolve_module_jump_row(self, monitor: dict) -> int:
        """模块行的跳转目标按动作唯一标识解析为当前行号，找不到时回退到配置行号。"""
        target_id = str(monitor.get("jump_action_id", "")).strip()
        if target_id == NEXT_WORKFLOW_STEP_TARGET_ID:
            return len(self.script.actions) + 1
        if target_id:
            for index, action in enumerate(self.script.actions):
                if str(action.get(ACTION_ID_KEY, "")).strip() == target_id:
                    return index + 1
        try:
            return max(1, int(monitor.get("jump_row") or 1))
        except (TypeError, ValueError):
            return 1

    def _module_jump_target_text(self, jump_row: int) -> str:
        """跳转目标的紧凑描述：第 3 行（键盘：A）；目标行不存在时只有行号。"""
        index = jump_row - 1
        if 0 <= index < len(self.script.actions):
            return f"第 {jump_row} 行（{action_short_text(self.script.actions[index])}）"
        return f"第 {jump_row} 行"

    def _after_global_detect_action(self, monitor: dict):
        if monitor["stop"].is_set():
            # 触发完成后该监控已被替换/停止（如 F12 或工作流恢复重新启用），不再处理。
            return
        triggered_in_workflow = monitor.get("workflow_resume_snapshot") is not None
        if self.current_workflow_step_index is None and not triggered_in_workflow:
            # 单独执行脚本（不在工作流中）。
            if monitor.get("trigger_kind") == "timeout" and monitor.get("segment_ready"):
                self._log("全局检测未识别超时：执行独立超时代码段。")
                threading.Thread(
                    target=self._play_standalone_global_body, args=(monitor,), daemon=True,
                ).start()
                return
            if monitor.get("jump_row"):
                # 普通脚本内嵌全局模块行：停止当前播放，跳转到脚本目标行
                # 继续播放到末尾，然后脚本结束（不再继续检测）。
                jump_row = self._resolve_module_jump_row(monitor)
                monitor["jump_row"] = jump_row
                target_text = self._module_jump_target_text(jump_row)
                self.standalone_jump_pending = True
                self.standalone_jump_done.clear()
                if self.player.running:
                    self.player.stop()
                self._log(
                    f"全局检测触发：检测到 {self._global_monitor_subject(monitor, monitor['template'].name)}，"
                    f"跳转到{target_text}执行，播放完脚本结束。",
                )
                self._append_mini_step(
                    f"全局检测触发：跳转到{target_text}执行。",
                )
                threading.Thread(
                    target=self._play_standalone_jump_body, args=(monitor,), daemon=True,
                ).start()
                return
            # 有语句体则重新执行语句体，检测保持运行。
            if self.standalone_global_replay or monitor.get("segment_ready"):
                self._log("全局检测触发：执行脚本动作。")
                self._append_mini_step("全局检测触发：执行脚本动作。")
                threading.Thread(
                    target=self._play_standalone_global_body, args=(monitor,), daemon=True,
                ).start()
                return
            # 旧独立脚本（无语句体）：触发后只点击，检测保持运行，条件仍满足则再次触发。
            self._log("全局检测触发：已点击，继续检测。")
            self._append_mini_step("全局检测触发：已点击，继续检测。")
            monitor["triggered"] = False
            return
        if self.worker and self.worker.is_alive():
            self.workflow_stop.set()
            self.player.stop()
            self.global_detect_pending_restart = True
            self.global_detect_restart_polls = 0
            self.root.after(100, lambda: self._poll_workflow_stop_for_restart(monitor))
            self._log("全局检测：正在停止当前执行，准备执行模块步骤并继续工作流。")
            return
        self._launch_global_detect_restart(monitor)

    # 特殊模块「重新执行工作流」：player 线程 hook 只置标志/事件 + _ui 调度，
    # 停止与重启全部由主线程轮询执行（player 单实例守卫）。
    def _restart_workflow_resolved_row(self, action: dict) -> int:
        """解析「重新执行工作流」的跳转行：动作 → 模块 → 全局默认 → 第 1 行。

        行号是当前工作流里的 1 基行号（对应工作流树里的行对象）；越界时由
        _launch_workflow_restart 收敛到首尾。
        """
        try:
            row = max(0, int(action.get("restart_workflow_target_row", 0) or 0))
        except (TypeError, ValueError):
            row = 0
        if not row:
            module_obj = getattr(self, "global_detect_active_module", None) or {}
            try:
                row = max(0, int(module_obj.get("restart_workflow_target_row", 0) or 0))
            except (TypeError, ValueError):
                row = 0
        if not row:
            row = load_module_restart_default_row()
        return max(1, row)

    def _on_restart_workflow_request(self, action: dict) -> bool:
        in_global_module = bool(
            getattr(self, "global_module_workflow_context", False)
            and getattr(self, "global_detect_pending_restart", False)
        )
        if self.current_workflow_step_index is None and not in_global_module:
            # 固定特殊动作只对当前工作流生效；独立脚本运行时直接跳过。
            self._ui(
                self._log,
                "特殊模块：当前未在工作流中执行，已跳过“重新执行工作流”。",
            )
            return False
        self.workflow_restart_requested = True
        self.workflow_restart_target_row = self._restart_workflow_resolved_row(action)
        self.workflow_stop.set()
        self.player.stop()
        self._stop_all_global_detect_monitors()
        self._ui(self._poll_workflow_stop_for_restart_workflow)
        return True

    def _poll_workflow_stop_for_restart_workflow(self):
        if (self.worker and self.worker.is_alive()) \
                or getattr(self, "global_detect_module_running", False) \
                or getattr(getattr(self, "player", None), "running", False) is True:
            self.root.after(100, lambda: self._poll_workflow_stop_for_restart_workflow())
            return
        self._launch_workflow_restart()

    def _launch_workflow_restart(self):
        steps = self._workflow_only_steps()
        target_row = max(1, int(getattr(self, "workflow_restart_target_row", 1) or 1))
        target_index = min(target_row, len(steps)) - 1 if steps else 0
        snapshot = self.workflow_repeats_snapshot or {}
        for index, step in enumerate(steps):
            saved = snapshot.get(index)
            if saved is not None:
                step["repeats"], step["unlimited"] = saved
        self.workflow_restart_requested = False
        self.workflow_restart_target_row = 1
        self.global_detect_pending_restart = False
        self.global_detect_end_current_script = False
        self.global_detect_advance_workflow_step = False
        self.workflow_repeats_snapshot = None
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()
        target_text = f"第 {target_index + 1} 行" if steps else "工作流开头"
        self._log(f"特殊模块：重新执行工作流，跳转到{target_text}。")
        self._append_mini_step(f"特殊模块：重新执行工作流，跳转到{target_text}。")
        self.run_workflow(
            start_index=target_index, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )

    def _poll_workflow_stop_for_restart(self, monitor: dict):
        if self.worker and self.worker.is_alive():
            self.global_detect_restart_polls += 1
            if self.global_detect_restart_polls < 300:
                self.root.after(100, lambda: self._poll_workflow_stop_for_restart(monitor))
                return
            self._log("全局检测：等待当前执行停止超时，仍尝试执行模块步骤并继续工作流。")
        self._launch_global_detect_restart(monitor)

    def _launch_global_detect_restart(self, monitor: dict):
        # pending 保持到模块执行完并真正开始断点恢复；否则 finally 中刚把
        # triggered 复位，旧监控可能抢在主线程恢复回调前再次触发。
        self.global_detect_pending_restart = True
        self.global_detect_restart_polls = 0
        self.workflow_stop.clear()
        # player.stop() 只是为了暂停被打断脚本；模块执行阶段重新开放停止事件。
        # 若模块对象本身没有代码段/脚本，也必须清除此标志才能恢复原断点。
        player = getattr(self, "player", None)
        if player is not None:
            player.reset()
        threading.Thread(
            target=self._run_global_module_steps,
            args=(monitor, self._bound_hwnd()), daemon=True,
        ).start()

    def _run_global_module_steps(self, monitor: dict, bound_hwnd: int | None):
        """Execute the triggered global module's script steps, then resume the workflow."""
        self.global_detect_module_running = True
        self.global_module_workflow_context = monitor.get("workflow_resume_snapshot") is not None
        self.global_detect_active_module = monitor.get("module")
        try:
            segment = list(monitor.get("segment") or [])
            if monitor.get("segment_ready") and segment:
                # 主动作后的附加代码段必须完整播放，再恢复原工作流。
                segment_result = self.player.play(
                    segment, 1, bound_hwnd,
                    propagate_current_script_jump=True,
                )
                if segment_result == JUMP_CURRENT_SCRIPT_LAST_RESULT:
                    referenced_actions = getattr(
                        self.player, "_last_stop_referenced_actions", None
                    )
                    referenced_screen = getattr(
                        self.player, "_last_stop_referenced_source_screen", None
                    )
                    if referenced_actions:
                        # 触发时正处于被引用脚本（关卡脚本）内部："结束当前脚本"
                        # 应结束最内层的它——跳到其最后一行执行，外层脚本恢复时
                        # 从引用动作之后继续，而不是跳到外层脚本最后一行。
                        self.global_detect_jumped_referenced_last = True
                        self._ui(
                            self._log,
                            "模块代码段要求跳转到当前脚本最后一行，"
                            "当前在被引用脚本内，已跳转该脚本最后一行执行。",
                        )
                        self.player.play(
                            referenced_actions, 1, bound_hwnd,
                            source_screen=referenced_screen,
                            start_index=max(0, len(referenced_actions) - 1),
                        )
                    else:
                        self.global_detect_jump_current_script_last = True
                        self._ui(
                            self._log,
                            "模块代码段要求跳转到当前脚本最后一行，停止执行代码段剩余动作。",
                        )
                    return
                if segment_result is True:
                    self.global_detect_end_current_script = True
                    self._ui(self._log, f"模块代码段要求{END_CURRENT_SCRIPT_LABEL}。")
                    return
                if not getattr(self, "workflow_restart_requested", False):
                    self._ui(self._log, "模块附加代码段执行完成。")
            module = monitor.get("module")
            if monitor.get("trigger_kind") == "timeout":
                return
            script_value = str(module.get("script", "")).strip() if module else ""
            if not script_value:
                return
            script_path = resolve_path(script_value)
            if not script_path.is_file():
                self._ui(self._log, f"全局模块脚本不存在：{script_value}，跳过模块步骤。")
                return
            try:
                script = load_script(script_path)
                module_activation = self._execution_activation_hwnd(
                    bound_hwnd,
                    bool(script.settings.get("activation_window_enabled", False)),
                    script.settings.get("activation_window"),
                )
                end_requested = self.player.play(
                    script.actions, 1, bound_hwnd,
                    source_screen=dict(script.settings.get("recorded_screen", {})) or None,
                    activate_target=bool(self.activate_target_enabled_var.get()),
                    activation_hwnd=module_activation,
                )
                if end_requested is True:
                    self.global_detect_end_current_script = True
                self._ui(self._log, f"全局模块步骤已执行：{script.name}。")
            except Exception as exc:
                self._ui(self._log, f"全局模块步骤执行失败：{exc}")
        finally:
            self.global_detect_module_running = False
            self.global_module_workflow_context = False
            self.global_detect_active_module = None
            # 模块步骤已执行完并恢复工作流：检测保持运行，条件仍满足则再次触发。
            monitor["segment_ready"] = False
            monitor["triggered"] = False
            # 代码段里的“重新执行工作流”已经接管执行流时，不能再恢复旧断点。
            if not getattr(self, "workflow_restart_requested", False):
                self._ui(self._resume_workflow_after_global_module)

    def _play_standalone_global_body(self, monitor: dict):
        """单独执行全局脚本：触发后重新播放语句体，播放完继续检测。"""
        self.global_detect_module_running = True
        self.global_detect_active_module = monitor.get("module")
        try:
            segment = list(monitor.get("segment") or [])
            if monitor.get("segment_ready") and segment:
                segment_result = self.player.play(
                    segment, 1, (self.standalone_global_replay or {}).get("hwnd"),
                    propagate_current_script_jump=True,
                )
                if segment_result == JUMP_CURRENT_SCRIPT_LAST_RESULT:
                    replay = self.standalone_global_replay or {}
                    actions = list(replay.get("actions") or [])
                    if actions:
                        self.player.play(
                            actions, 1, replay.get("hwnd"),
                            source_screen=replay.get("source_screen"),
                            activate_target=bool(replay.get("activate_target")),
                            activation_hwnd=replay.get("activation_hwnd"),
                            start_index=len(actions) - 1,
                        )
                    monitor["stop"].set()
                    self._ui(self._log, "模块代码段已跳转执行当前脚本最后一行，脚本结束。")
                    return
                if segment_result is True:
                    monitor["stop"].set()
                    self._ui(self._log, "模块代码段已结束当前脚本。")
                    return
                self._ui(self._log, "模块附加代码段执行完成。")
            if monitor.get("trigger_kind") == "timeout":
                self._ui(self._log, "模块超时代码段执行完成，继续全局检测。")
                return
            replay = self.standalone_global_replay or {}
            actions = list(replay.get("actions") or [])
            if actions:
                end_requested = self.player.play(
                    actions, 1, replay.get("hwnd"),
                    source_screen=replay.get("source_screen"),
                    activate_target=bool(replay.get("activate_target")),
                    activation_hwnd=replay.get("activation_hwnd"),
                )
                if end_requested is True:
                    monitor["stop"].set()
                    self._ui(self._log, "全局脚本已按代码段要求结束。")
                    return
            self._ui(self._log, "全局脚本动作执行完成，继续检测。")
            self._ui(self._append_mini_step, "全局脚本动作执行完成，继续检测。")
        except Exception as exc:
            self._ui(self._log, f"全局脚本动作执行失败：{exc}")
        finally:
            self.global_detect_module_running = False
            self.global_detect_active_module = None
            monitor["segment_ready"] = False
            # 语句体已执行完：检测保持运行，条件仍满足则再次触发。
            monitor["triggered"] = False
            if monitor.get("trigger_kind") == "timeout":
                monitor["timeout_triggered"] = False
                monitor["not_found_since"] = time.perf_counter()

    def _play_standalone_jump_body(self, monitor: dict):
        """普通脚本内嵌全局模块行：触发后从跳转行播放到末尾，然后脚本结束。

        触发时若当前播放仍在运行，worker 会等本方法结束（standalone_jump_done）
        再执行"脚本执行完成"收尾；若播放已自然结束（worker 已收尾），本方法
        负责恢复执行界面。
        """
        self.global_detect_module_running = True
        # 主播放是否已自然结束（worker 已收尾）：是则本方法接管执行界面。
        manages_ui = not self.main_hidden_for_execution
        try:
            # 当前播放已被停止（或已自然结束）：等它完全退出后再开始跳转播放。
            while self.player.running:
                self.player.stop_event.wait(0.1)
            jump_row = max(1, int(monitor.get("jump_row") or 1))
            actions = list(self.script.actions)
            start_index = max(0, jump_row - 1)
            hwnd = self._bound_hwnd()
            activation_enabled, activation_signature = self._activation_settings_from_script()
            try:
                activation_hwnd = self._execution_activation_hwnd(
                    hwnd, activation_enabled, activation_signature,
                )
            except RuntimeError:
                activation_hwnd = hwnd
            activate_target = bool(self.activate_target_enabled_var.get())
            source_screen = dict(self.script.settings.get("recorded_screen", {})) or None
            if manages_ui:
                self._ui(self._hide_main_for_execution)
                self._ui(self._show_execution_mini)
            # 引用模块对象动作 B = 代码段：先播放代码段，再执行跳转（末尾插入时
            # jump_row 越界，跳转被跳过，段播完脚本自然结束）。
            if monitor.get("segment_ready") and monitor.get("segment"):
                segment_result = self.player.play(
                    list(monitor["segment"]), 1, hwnd,
                    source_screen=source_screen, activate_target=activate_target,
                    activation_hwnd=activation_hwnd,
                    propagate_current_script_jump=True,
                )
                self._ui(self._log, "模块代码段执行完成。")
                if segment_result == JUMP_CURRENT_SCRIPT_LAST_RESULT:
                    jump_row = max(1, len(actions))
                    start_index = max(0, jump_row - 1)
                    self._ui(self._log, "模块代码段要求跳转到当前脚本最后一行。")
                elif segment_result is True:
                    self._ui(self._log, "模块代码段已结束当前脚本。")
                    return
            target_text = self._module_jump_target_text(jump_row)
            self._ui(
                self._log,
                f"全局检测触发：从{target_text}继续执行脚本，播放到末尾后结束。",
            )
            self._ui(self._append_mini_step, f"全局检测触发：从{target_text}继续执行。")
            if jump_row <= len(actions) and actions and not self.player.stop_event.is_set():
                self.player.play(
                    actions, 1, hwnd, source_screen=source_screen,
                    activate_target=activate_target, activation_hwnd=activation_hwnd,
                    start_index=start_index,
                    on_repeat=lambda current, total: self._ui(
                        self._set_execution_progress,
                        f"全局模块 · 从{target_text}跳转执行 · "
                        f"第 {current}/{total} 次 · F12 停止",
                    ),
                )
            self._ui(self._log, "全局模块动作执行完成，脚本结束。")
        except Exception as exc:
            self._ui(self._log, f"全局模块动作执行失败：{exc}")
        finally:
            self.global_detect_module_running = False
            # 跳转播放结束：停止检测并通知等待中的 worker 收尾；脚本到此结束。
            monitor["stop"].set()
            self.standalone_jump_pending = False
            self.standalone_jump_done.set()
            if manages_ui:
                self._ui(self._finish_execution_visibility)

    def _resume_workflow_after_global_module(self):
        if self.workflow_stop.is_set() or self.player.stop_event.is_set():
            self.global_detect_pending_restart = False
            self._leave_focus_mode()
            self._log("全局检测：已收到停止指令，不继续工作流。")
            return
        index = self.global_detect_resume_index
        repeat = self.global_detect_resume_repeat
        action_index = self.global_detect_resume_action
        jump_current_script_last = bool(
            getattr(self, "global_detect_jump_current_script_last", False)
        )
        self.global_detect_jump_current_script_last = False
        jumped_referenced_last = bool(
            getattr(self, "global_detect_jumped_referenced_last", False)
        )
        self.global_detect_jumped_referenced_last = False
        end_current_script = bool(getattr(self, "global_detect_end_current_script", False))
        advance_workflow_step = bool(
            getattr(self, "global_detect_advance_workflow_step", False)
        )
        self.global_detect_resume_index = None
        self.global_detect_resume_repeat = 0
        self.global_detect_resume_action = 0
        self.global_detect_end_current_script = False
        self.global_detect_advance_workflow_step = False
        if index is None:
            index = 0
            repeat = 0
            action_index = 0
        if jump_current_script_last or jumped_referenced_last:
            steps = self._workflow_only_steps()
            if 0 <= index < len(steps):
                if jumped_referenced_last:
                    # 已把被引用关卡脚本最后一行播放完：外层脚本（封装关卡）
                    # 从引用动作之后继续，不再跳到外层脚本最后一行。
                    action_index = action_index + 1
                else:
                    script_path = resolve_path(steps[index].get("script", ""))
                    if script_path.is_file():
                        try:
                            current_script = load_script(script_path)
                            action_index = max(0, len(current_script.actions) - 1)
                        except Exception as exc:
                            self._log(f"无法读取当前脚本最后一行，沿用原断点：{exc}")
        if not self._workflow_only_steps():
            self.global_detect_pending_restart = False
            self._leave_focus_mode()
            self._log("全局检测：工作流为空，无法继续。")
            return
        if end_current_script:
            remaining = self._consume_workflow_repeat(index)
            step = self._workflow_only_steps()[index]
            has_next_repeat = (
                not bool(getattr(self, "workflow_test_mode_active", False))
                and not bool(step.get("unlimited", False))
                and remaining > 0
            )
            if has_next_repeat:
                self.global_detect_pending_restart = False
                self.run_workflow(
                    start_index=index, start_repeat=0, resume_action_index=0,
                    preserve_global_rearm_locks=True,
                )
                self._log(
                    f"全局检测：已触发 {self.global_detect_trigger_count} 次，"
                    f"模块步骤执行完毕，已结束工作流第 {index + 1} 行当前第 {repeat + 1} 次脚本，"
                    f"继续下一次（剩余 {int(step.get('repeats', 0))} 次）。",
                )
                return
            advance_workflow_step = True
        if advance_workflow_step:
            if not end_current_script:
                self._consume_workflow_repeat(index)
            next_index = index + 1
            self.global_detect_pending_restart = False
            if next_index < len(self._workflow_only_steps()):
                self.run_workflow(
                    start_index=next_index, start_repeat=0, resume_action_index=0,
                    preserve_global_rearm_locks=True,
                )
                self._log(
                    f"全局检测：已触发 {self.global_detect_trigger_count} 次，"
                    f"模块步骤执行完毕，已结束工作流第 {index + 1} 行脚本，"
                    f"从第 {next_index + 1} 行继续。",
                )
            else:
                self.workflow_repeats_snapshot = None
                self.workflow_test_mode_active = False
                self._stop_all_global_detect_monitors(clear=True)
                self._leave_focus_mode()
                self._set_status("工作流执行完成", "success")
                self._append_mini_step("全局模块已结束最后一个脚本，工作流执行完成。")
                self._log("全局模块已结束最后一个脚本，工作流执行完成。")
                self._sound("run_done")
                self._finish_execution_visibility()
            return
        # 当前在 Tk 主线程；先解除互斥标志，再由 run_workflow 同步清空旧监控，
        # 中间不会处理其他监控回调。
        self.global_detect_pending_restart = False
        self.run_workflow(
            start_index=index, start_repeat=repeat,
            resume_action_index=action_index,
            preserve_global_rearm_locks=True,
        )
        self._log(
            f"全局检测：已触发 {self.global_detect_trigger_count} 次，"
            f"模块步骤执行完毕，从工作流第 {index + 1} 行、"
            f"第 {repeat + 1} 次、脚本动作断点 {action_index + 1} 继续。",
        )

    def _record_workflow_action(self, next_index):
        """Record the next action index of the current script (player thread)."""
        self.current_workflow_action_index = max(0, int(next_index))

    def _record_workflow_repeat(self, current, total, number, count, name):
        """Record the repeat index about to run, then update the progress label."""
        self.current_workflow_repeat_index = max(0, current - 1)
        self._ui(
            self._set_execution_progress,
            workflow_execution_progress(number, count, name, total, current),
        )

    def _sound(self, name: str):
        play_alert(name, bool(self.sound_enabled_var.get()))

    def test_sound(self):
        if not self.sound_enabled_var.get():
            self._set_status("提示音已关闭", "warning")
            self._log("测试提示音未播放：请先勾选“快捷键提示音”。")
            return
        self._sound("record_start")
        self._set_status("正在播放测试提示音", "success")

    def _show_recording_mini(self):
        if not self.mini_window_enabled_var.get():
            return
        self._hide_main_to_tray(for_recording=True)
        self._show_operation_mini("recording")

    def _show_execution_mini(self):
        if not self.execution_mini_enabled_var.get():
            return
        self._show_operation_mini("execution")

    def _show_operation_mini(self, mode: str):
        if self.mini_window and self.mini_window.winfo_exists() and self.mini_mode == mode:
            # 窗口创建后一直保持可见并置顶，无需重新显示。Tk 的 deiconify 会
            # Restack 激活（SetWindowPos 不带 SWP_NOACTIVATE），抢走游戏前台
            # 导致游戏退全屏，所以这里不做任何激活性操作。
            if not self.mini_window.winfo_ismapped():
                show_window_no_activate(self.mini_window.winfo_id())
            # 全局模块中断期间旧 worker 会结束，原刷新循环随之停下；断点恢复
            # 复用同一个小窗时必须重新启动刷新，否则时间会永远停在 00:00。
            if getattr(self, "mini_update_after_id", None) is None:
                self._update_operation_mini()
            return
        self._hide_operation_mini()
        self.mini_mode = mode
        mini = tk.Toplevel(self.root)
        self.mini_window = mini
        mini.title("MacroFlow 录制中" if mode == "recording" else "MacroFlow 执行中")
        mini.configure(background=COLOR_BG)
        mini.resizable(False, False)
        mini.attributes("-topmost", True)
        try:
            mini.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        # Keep the panel compact so it does not cover the game. The denser log
        # below carries the useful detail instead of spending space on chrome.
        width, height = (420, 272) if mode == "recording" else (420, 316)
        # 录制小窗和执行小窗共用同一个用户调节的位置。
        x, y = self._execution_mini_position(width, height)
        mini.geometry(f"{width}x{height}+{x}+{y}")
        mini.protocol("WM_DELETE_WINDOW", self._hide_operation_mini)
        # 在窗口首次映射（显示）之前就设置 WS_EX_NOACTIVATE。winfo_id 只创建
        # HWND 不会显示窗口；若等到 update_idletasks 映射之后再设置，Tk 映射
        # 新顶层窗口时会先激活它，小窗弹出的瞬间就会抢走激活窗口。
        make_window_no_activate(mini.winfo_id())

        body = ttk.Frame(mini, padding=10, style="Surface.TFrame")
        body.pack(fill="both", expand=True)
        top = ttk.Frame(body, style="Surface.TFrame")
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.mini_context_var, style="MiniTitle.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.mini_elapsed_var, style="MiniTime.TLabel").pack(side="right")
        ttk.Label(body, textvariable=self.mini_count_var, style="MiniText.TLabel",
                  wraplength=390, justify="left").pack(anchor="w", pady=(7, 2))
        self.mini_binding_label = ttk.Label(body, textvariable=self.mini_window_var,
                                            style="MiniText.TLabel", wraplength=390)
        self.mini_binding_label.pack(anchor="w", pady=(0, 5))
        steps_frame = ttk.Frame(body, style="Surface.TFrame")
        steps_frame.pack(fill="both", expand=True, pady=(0, 7))
        self.mini_steps_text = tk.Text(
            steps_frame, height=6, state="disabled", wrap="word",
            background=COLOR_SURFACE_ALT, foreground=COLOR_TEXT,
            insertbackground=COLOR_TEXT, selectbackground="#244D78",
            relief="flat", bd=0, font=("Microsoft YaHei UI", 9),
            padx=7, pady=5, takefocus=False,
        )
        self.mini_steps_text.pack(side="left", fill="both", expand=True)
        mini_scroll = ttk.Scrollbar(steps_frame, orient="vertical",
                                    command=self.mini_steps_text.yview, takefocus=False)
        mini_scroll.pack(side="right", fill="y")
        self.mini_steps_text.configure(yscrollcommand=mini_scroll.set)
        # A normal top-level window may make an exclusive/fullscreen game leave
        # fullscreen. WS_EX_NOACTIVATE has already been applied above, before the
        # window was first mapped, so it never takes activation.
        mini.update_idletasks()
        set_dark_titlebar(mini.winfo_id())
        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.pack(fill="x")
        if mode == "recording":
            buttons.columnconfigure(0, weight=3)
            buttons.columnconfigure(1, weight=3)
            buttons.columnconfigure(2, weight=2)
            ttk.Button(buttons, text="停止录制  F8", command=lambda: self.toggle_record(from_ui=True),
                       bootstyle="danger", takefocus=False).grid(row=0, column=0, sticky="ew")
            ttk.Button(buttons, text="紧急停止  F12", command=lambda: self.stop_all(from_ui=True),
                       bootstyle="danger-outline", takefocus=False).grid(row=0, column=1, sticky="ew", padx=6)
            ttk.Button(buttons, text="隐藏", command=self._hide_operation_mini,
                       bootstyle="secondary-outline", takefocus=False).grid(row=0, column=2, sticky="ew")
            self._append_mini_step("实时记录已打开，不会切换或恢复游戏窗口。")
        else:
            buttons.columnconfigure(0, weight=1)
            if self.execution_focus_requested:
                ttk.Label(
                    body,
                    text="紧急恢复：先按 F12；若无响应，按 Ctrl + Alt + Del",
                    style="MiniWarning.TLabel", wraplength=390, justify="center",
                ).pack(fill="x", pady=(0, 7), before=buttons)
                ttk.Button(buttons, text="强制专注中 · 按 F12 停止并解除",
                           command=lambda: None, bootstyle="danger",
                           takefocus=False).grid(row=0, column=0, sticky="ew")
                self._append_mini_step("强制专注已开启：实体键鼠已锁定，按 F12 停止并解除。")
            else:
                ttk.Button(buttons, text="普通执行模式 · 按 F12 停止",
                           command=lambda: None, bootstyle="secondary",
                           takefocus=False).grid(row=0, column=0, sticky="ew")
                self._append_mini_step("普通执行模式：未锁定实体键鼠，点击正常发送。")
        self._update_operation_mini()

    def _execution_mini_position(self, width: int = 420, height: int = 316) -> tuple[int, int]:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        saved = getattr(self, "execution_mini_position", None)
        if isinstance(saved, (list, tuple)) and len(saved) == 2:
            try:
                x, y = int(saved[0]), int(saved[1])
            except (TypeError, ValueError):
                x, y = screen_w - width - 24, screen_h - height - 72
        else:
            x, y = screen_w - width - 24, screen_h - height - 72
        return (
            max(0, min(x, max(0, screen_w - width))),
            max(0, min(y, max(0, screen_h - height))),
        )

    def _adjust_execution_mini_position(self):
        """Show a draggable, bordered preview and persist its top-left position."""
        if getattr(self, "execution_mini_position_editor", None):
            return
        width, height = 420, 316
        preview = tk.Toplevel(self.root)
        self.execution_mini_position_editor = preview
        preview.title("调节执行小窗位置")
        preview.geometry(
            f"{width}x{height}+{self._execution_mini_position(width, height)[0]}+"
            f"{self._execution_mini_position(width, height)[1]}"
        )
        preview.resizable(False, False)
        preview.attributes("-topmost", True)
        preview.configure(background="#E04444", highlightthickness=3, highlightbackground="#FF6B6B")
        body = ttk.Frame(preview, padding=12, style="Surface.TFrame")
        body.pack(fill="both", expand=True, padx=3, pady=3)
        ttk.Label(
            body, text="执行小窗边界（拖动标题区域调整位置）",
            style="MiniWarning.TLabel", wraplength=380, justify="center",
        ).pack(fill="x", pady=(4, 12))
        ttk.Label(
            body, text="红色边框就是执行小窗的完整占用范围\n确认后执行小窗会固定在此位置。",
            style="MiniText.TLabel", justify="center",
        ).pack(expand=True)
        buttons = ttk.Frame(body, style="Surface.TFrame")
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="确认并保存", command=lambda: self._confirm_execution_mini_position(preview),
                   bootstyle="success").pack(side="left", fill="x", expand=True)
        ttk.Button(buttons, text="取消", command=lambda: self._close_execution_mini_position_editor(preview),
                   bootstyle="secondary").pack(side="left", fill="x", expand=True, padx=(8, 0))
        drag = {"x": 0, "y": 0}
        def begin(event):
            drag["x"], drag["y"] = event.x_root, event.y_root
        def move(event):
            current_x, current_y = preview.winfo_x(), preview.winfo_y()
            preview.geometry(f"+{current_x + event.x_root - drag['x']}+{current_y + event.y_root - drag['y']}")
            drag["x"], drag["y"] = event.x_root, event.y_root
        for widget in (preview, body):
            widget.bind("<ButtonPress-1>", begin)
            widget.bind("<B1-Motion>", move)
        preview.protocol("WM_DELETE_WINDOW", lambda: self._close_execution_mini_position_editor(preview))
        preview.focus_force()

    def _confirm_execution_mini_position(self, preview):
        self.execution_mini_position = [preview.winfo_x(), preview.winfo_y()]
        self._persist_sidebar_settings()
        self._close_execution_mini_position_editor(preview)

    def _close_execution_mini_position_editor(self, preview):
        if getattr(self, "execution_mini_position_editor", None) is preview:
            self.execution_mini_position_editor = None
        try:
            preview.destroy()
        except tk.TclError:
            pass

    def _hide_recording_mini(self):
        self._hide_operation_mini()

    def _hide_execution_mini(self):
        if self.mini_mode == "execution":
            self._hide_operation_mini()

    def _hide_operation_mini(self):
        after_id = getattr(self, "mini_update_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
            self.mini_update_after_id = None
        if self.mini_window and self.mini_window.winfo_exists():
            self.mini_window.destroy()
        self.mini_window = None
        self.mini_steps_text = None
        self.mini_mode = ""

    def _update_recording_mini(self):
        self._update_operation_mini()

    def _update_operation_mini(self):
        self.mini_update_after_id = None
        active = self.recorder.running or (self.worker and self.worker.is_alive())
        if not active or not self.mini_window or not self.mini_window.winfo_exists():
            return
        # 兜底防失焦：Tk 窗口/控件若意外抢到前台（小窗映射/刷新/滚动时
        # 焦点管理，WS_EX_NOACTIVATE 挡不住 SetFocus 给子控件），把焦点
        # 还给绑定窗口。仅执行中且激活目标开启时生效；用户切到外屏工作
        # 窗口时前台不是本进程窗口，不会被打断。
        if self.mini_mode == "execution" and self.activate_target_enabled_var.get():
            foreground = get_foreground_window_info()
            if foreground and is_current_process_window(foreground.hwnd):
                target_hwnd = self._bound_hwnd(update_display=False)
                if target_hwnd and is_window(target_hwnd) \
                        and not is_current_process_window(target_hwnd):
                    activate_window(target_hwnd)
        started_at = self.record_started_at if self.mini_mode == "recording" else self.execution_started_at
        elapsed = max(0, int(time.perf_counter() - started_at))
        self.mini_elapsed_var.set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        if self.mini_mode == "recording":
            self.mini_context_var.set("正在录制")
            capture_mode = "原始相对坐标" if self.recorder.current_mode() == "relative" else "普通桌面坐标"
            self.mini_count_var.set(f"已记录 {len(self.recorder.actions):,} 个动作 · {capture_mode} · F8 停止")
        else:
            self.mini_context_var.set("强制专注执行中" if self.execution_focus_requested else "普通执行中")
            self.mini_count_var.set(self.execution_progress_text or "正在准备 · F12 停止")
        info = get_foreground_window_info()
        self._refresh_binding_for_display()
        target = self.bound_window
        target_title = target.title if target else (
            self.saved_window_signature.get("title", "未设置") if self.saved_window_signature else "未设置"
        )
        current_title = (info.title or info.class_name or "无标题窗口") if info else "未知"
        bound = bool(info and self._foreground_matches_target(info))
        state = "已绑定" if bound else "未绑定"
        if self.mini_mode == "execution":
            activation_title = (
                self.activation_window.title if self.activation_window else target_title
            )
            self.mini_window_var.set(
                f"前台：{current_title} · 目标：{target_title} · 前置：{activation_title}"
            )
        else:
            self.mini_window_var.set(f"前台：{current_title} · 目标：{target_title} · {state}")
        color = COLOR_GREEN if bound else COLOR_RED
        if self.mini_binding_label and self.mini_binding_label.winfo_exists():
            self.mini_binding_label.configure(foreground=color)
        if self.bind_label_widget and self.bind_label_widget.winfo_exists():
            self.bind_label_widget.configure(foreground=color if target else COLOR_RED)
        self.mini_update_after_id = self.root.after(250, self._update_operation_mini)

    def _refresh_binding_for_display(self):
        """Re-resolve a restarted target so the status never relies on a stale HWND."""
        if self.saved_window_signature and (not self.bound_window or not is_window(self.bound_window.hwnd)):
            self._restore_saved_window_binding()

    def _foreground_matches_target(self, info: WindowInfo) -> bool:
        bound_window = getattr(self, "bound_window", None)
        if bound_window and int(info.hwnd) == int(bound_window.hwnd):
            return True
        signature = getattr(self, "saved_window_signature", None)
        if not signature:
            return False
        expected_class = str(signature.get("class_name", ""))
        expected_title = str(signature.get("title", ""))
        expected_path = os.path.normcase(str(signature.get("process_path", ""))).casefold()
        if expected_class and info.class_name != expected_class:
            return False
        if expected_path and info.process_path and os.path.normcase(info.process_path).casefold() != expected_path:
            return False
        # 游戏标题可能随服务器/关卡改变。已有窗口类或进程路径时，它们才是
        # 稳定身份；只有两者都缺失时才退回标题匹配。
        if not expected_class and not expected_path and expected_title and info.title != expected_title:
            return False
        # Position and client size are diagnostic metadata only. Fullscreen,
        # DPI, and border changes must not disable raw-relative recording.
        return True

    def _append_mini_step(self, text: str):
        mini_steps_text = getattr(self, "mini_steps_text", None)
        if not mini_steps_text or not mini_steps_text.winfo_exists():
            return
        try:
            x, y = get_cursor_pos()
            cursor = f"[鼠标 {x},{y}]"
        except Exception:
            cursor = "[鼠标 ?,?]"
        self.mini_steps_text.configure(state="normal")
        self.mini_steps_text.insert(
            "end", f"{datetime.now():%H:%M:%S}  {cursor} {text}\n",
        )
        lines = int(self.mini_steps_text.index("end-1c").split(".")[0])
        if lines > 120:
            self.mini_steps_text.delete("1.0", f"{lines - 120}.0")
        self.mini_steps_text.see("end")
        self.mini_steps_text.configure(state="disabled")

    # System tray
    @staticmethod
    def _create_tray_image() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (30, 41, 59, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 59, 59), radius=13, fill=(37, 99, 235, 255))
        draw.ellipse((17, 17, 47, 47), fill=(255, 255, 255, 255))
        draw.ellipse((24, 24, 40, 40), fill=(217, 45, 32, 255))
        return image

    def _ensure_tray(self, visible: bool = True) -> bool:
        if self.tray_icon is not None:
            if visible:
                self._set_tray_visible_async(True)
            return True
        try:
            ready = threading.Event()
            setup_errors: list[Exception] = []

            def setup(icon: pystray.Icon):
                try:
                    if visible:
                        icon.visible = True
                except Exception as exc:
                    setup_errors.append(exc)
                finally:
                    ready.set()

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._tray_restore, default=True),
                pystray.MenuItem("退出", self._tray_exit),
            )
            self.tray_icon = pystray.Icon(
                "MacroFlowStudio", self._create_tray_image(), APP_NAME, menu
            )
            self.tray_icon.run_detached(setup=setup)
            if not ready.wait(timeout=3):
                raise RuntimeError("系统托盘启动超时")
            if setup_errors:
                raise setup_errors[0]
            return True
        except Exception as exc:
            if self.tray_icon is not None:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass
            self.tray_icon = None
            self._ui(self._log, f"创建系统托盘图标失败：{exc}")
            return False

    def _set_tray_visible_async(self, visible: bool):
        icon = self.tray_icon
        if icon is None:
            return

        def update_visibility():
            try:
                if self.tray_icon is icon:
                    icon.visible = visible
            except Exception as exc:
                self._ui(self._log, f"更新系统托盘状态失败：{exc}")

        threading.Thread(
            target=update_visibility, name="MacroFlowTrayVisibility", daemon=True,
        ).start()

    def _start_execution_prewarm(self):
        """Prepare first-run-only resources without blocking the Tk thread."""
        if self.exiting:
            return
        prewarm_alert("run_start")

        def prepare_tray():
            if not self.exiting:
                self._ensure_tray(visible=False)

        threading.Thread(target=prepare_tray, name="MacroFlowTrayWarmup", daemon=True).start()

    def _stop_tray(self):
        icon = self.tray_icon
        self.tray_icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def _tray_restore(self, _icon=None, _item=None):
        self._ui(self._restore_main_window)

    def _tray_exit(self, _icon=None, _item=None):
        self._ui(self._quit_app)

    def _hide_main_to_tray(self, for_recording: bool = False) -> bool:
        if not self._ensure_tray():
            return False
        was_visible = self.root.state() != "withdrawn"
        if for_recording and was_visible:
            self.main_hidden_for_recording = True
        self.root.withdraw()
        self.main_hidden_to_tray = True
        return True

    def _restore_main_window(self):
        if self.recorder.running and self.mini_window_enabled_var.get():
            self._show_recording_mini()
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.main_hidden_to_tray = False
        self.main_hidden_for_recording = False
        if self.tray_icon is not None:
            self._set_tray_visible_async(False)

    def _hide_main_for_execution(self):
        if self.root.state() == "withdrawn":
            self.execution_should_remain_in_tray = self.main_hidden_to_tray
            self.main_hidden_for_execution = True
            return
        self.execution_should_remain_in_tray = False
        if not self._hide_main_to_tray():
            # A tray icon is helpful, but execution must still be unobstructed
            # when the shell refuses to create one.
            self.root.withdraw()
        self.main_hidden_for_execution = True

    def _finish_execution_visibility(self):
        self.execution_progress_text = ""
        if not self.main_hidden_for_execution:
            return
        self.main_hidden_for_execution = False
        self._hide_execution_mini()
        if getattr(self, "execution_should_remain_in_tray", False):
            self.execution_should_remain_in_tray = False
            return
        self._restore_main_window()

    def open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    # Window binding
    def choose_window(self):
        selected = WindowPicker(self.root).show()
        if selected:
            self.bound_window = selected
            self.saved_window_signature = {
                "title": selected.title,
                "class_name": selected.class_name,
                "process_path": selected.process_path,
                "window_rect": list(selected.window_rect),
                "client_size": list(selected.client_size),
            }
            self.bind_label_var.set(selected.title)
            self._persist_sidebar_settings()
            self._log(f"已绑定并保存目标窗口：{selected.label}；下次启动会自动恢复。")
            self._log("智能录制会在目标窗口激活时记录原始相对轨迹，离开目标窗口后记录普通坐标。")

    def unbind_window(self):
        self.bound_window = None
        self.saved_window_signature = None
        self.bind_label_var.set("未绑定窗口")
        self._log("已解除窗口绑定。")

    def _activation_settings_from_script(self) -> tuple[bool, dict[str, str] | None]:
        """Read the pre-window config stored in the current script's settings."""
        enabled = bool(self.script.settings.get("activation_window_enabled", False))
        signature = self.script.settings.get("activation_window")
        if isinstance(signature, dict) and signature.get("title"):
            signature = {
                "title": str(signature.get("title", "")),
                "class_name": str(signature.get("class_name", "")),
                "process_path": str(signature.get("process_path", "")),
            }
        else:
            signature = None
        return enabled, signature

    def _persist_activation_to_script(self):
        """Write the pre-window config into the current script's settings."""
        self.script.settings["activation_window_enabled"] = bool(self.activation_enabled_var.get())
        self.script.settings["activation_window"] = (
            dict(self.saved_activation_signature) if self.saved_activation_signature else None
        )
        self._mark_dirty()

    def _sync_activation_ui_from_script(self):
        """Refresh the sidebar pre-window controls from the current script."""
        has_script_config = (
            "activation_window_enabled" in self.script.settings
            or "activation_window" in self.script.settings
        )
        if has_script_config:
            enabled, signature = self._activation_settings_from_script()
        else:
            # 老脚本/未配置脚本继承最近保存值。被动打开脚本不能清除全局记忆。
            enabled = bool(getattr(self, "activation_draft_enabled", False))
            draft = getattr(self, "activation_draft_signature", None)
            signature = dict(draft) if draft else None
        self.activation_enabled_var.set(enabled)
        self.saved_activation_signature = signature
        if enabled and signature:
            self._restore_saved_activation_window(signature)
        else:
            self.activation_window = None
        self._refresh_activation_label()

    def _toggle_activation_enabled(self):
        self._persist_activation_to_script()
        self._remember_activation_draft()
        self._refresh_activation_label()
        self._persist_sidebar_settings()

    def _refresh_activation_label(self):
        if not self.saved_activation_signature:
            self.activation_label_var.set("跟随目标窗口")
            return
        title = self.saved_activation_signature["title"]
        if not self.activation_enabled_var.get():
            self.activation_label_var.set(f"{title}（已停用）")
        elif self.activation_window and is_window(self.activation_window.hwnd):
            self.activation_label_var.set(title)
        else:
            self.activation_label_var.set(f"已保存，等待窗口：{title}")

    def choose_activation_window(self):
        selected = WindowPicker(self.root).show()
        if not selected:
            return
        self.activation_window = selected
        self.saved_activation_signature = {
            "title": selected.title,
            "class_name": selected.class_name,
            "process_path": selected.process_path,
        }
        self.activation_enabled_var.set(True)
        self._refresh_activation_label()
        self._persist_activation_to_script()
        self._remember_activation_draft()
        self._persist_sidebar_settings()
        self._log(f"已为本脚本设置执行前置窗口：{selected.label}；下次启动会自动恢复。")

    def unbind_activation_window(self):
        self.activation_window = None
        self.saved_activation_signature = None
        self.activation_enabled_var.set(False)
        self._refresh_activation_label()
        self._persist_activation_to_script()
        self._remember_activation_draft()
        self._persist_sidebar_settings()
        self._log("已清除本脚本的执行前置窗口，执行时跟随目标窗口。")

    def _restore_saved_activation_window(self, signature: dict[str, str] | None) -> bool:
        """Find the pre-window matching signature; fills self.activation_window only."""
        if not signature:
            return False
        title = str(signature.get("title", ""))
        class_name = str(signature.get("class_name", ""))
        process_path = os.path.normcase(str(signature.get("process_path", ""))).casefold()
        foreground = get_foreground_window_info()

        def matches(item: WindowInfo, require_title: bool = True) -> bool:
            item_path = os.path.normcase(item.process_path).casefold() if item.process_path else ""
            return (
                (not require_title or not title or item.title == title)
                and (not class_name or item.class_name == class_name)
                and (not process_path or not item_path or item_path == process_path)
            )

        windows = enum_windows()
        exact = [item for item in windows if matches(item)]
        if exact:
            selected = next((item for item in exact if foreground and item.hwnd == foreground.hwnd), exact[0])
        else:
            compatible = [item for item in windows if matches(item, require_title=False)]
            selected = foreground if foreground and matches(foreground, require_title=False) else (
                compatible[0] if len(compatible) == 1 else None
            )
        if selected:
            self.activation_window = selected
            return True
        self.activation_window = None
        return False

    def _execution_activation_hwnd(self, target_hwnd: int | None,
                                   enabled: bool, signature: dict[str, str] | None) -> int | None:
        """Resolve the pre-window hwnd from a script's own settings."""
        if not enabled or not signature:
            return None
        # 工作流会连续执行不同脚本，不能仅因缓存 HWND 仍有效就复用上一个
        # 脚本的窗口；每次都按当前签名重新核对并解析。
        if not self._restore_saved_activation_window(signature):
            raise RuntimeError("脚本的前置窗口当前未打开，请重新选择或取消勾选。")
        return self.activation_window.hwnd

    def toggle_cursor_tracking(self):
        if self.cursor_tracking:
            self._stop_cursor_tracking()
            return
        self.cursor_tracking = True
        self.cursor_position_button.configure(text="停止实时读取")
        self.cursor_position_var.set("移动鼠标以读取外部坐标…")
        self._show_cursor_tracking_mini()
        self.root.withdraw()
        self.main_hidden_for_cursor_tracking = True
        self._set_status("正在实时读取光标坐标，再次点击按钮停止", "warning")
        self._poll_cursor_position()

    def _show_cursor_tracking_mini(self):
        if self.cursor_tracking_mini and self.cursor_tracking_mini.winfo_exists():
            return
        mini = tk.Toplevel(self.root)
        self.cursor_tracking_mini = mini
        mini.overrideredirect(True)
        mini.attributes("-topmost", True)
        mini.configure(background=COLOR_SURFACE)
        width, height = 280, 62
        x = max(8, mini.winfo_screenwidth() - width - 18)
        mini.geometry(f"{width}x{height}+{x}+18")
        body = ttk.Frame(mini, padding=(12, 9), style="Surface.TFrame")
        body.pack(fill="both", expand=True)
        ttk.Label(
            body, textvariable=self.cursor_tracking_mini_var,
            style="MiniTime.TLabel",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            body, text="停止", command=self.toggle_cursor_tracking,
            bootstyle="danger", width=7,
        ).pack(side="right")
        mini.update_idletasks()
        make_window_no_activate(mini.winfo_id())

    def _hide_cursor_tracking_mini(self):
        if self.cursor_tracking_mini and self.cursor_tracking_mini.winfo_exists():
            self.cursor_tracking_mini.destroy()
        self.cursor_tracking_mini = None

    def _poll_cursor_position(self):
        if not self.cursor_tracking:
            return
        x, y = get_cursor_pos()
        screen = get_virtual_screen_rect()
        self.cursor_position_var.set(f"({x}, {y}) · {screen['width']}×{screen['height']}")
        self.cursor_tracking_mini_var.set(f"X: {x}    Y: {y}")
        self.cursor_tracking_after_id = self.root.after(50, self._poll_cursor_position)

    def _stop_cursor_tracking(self):
        self.cursor_tracking = False
        if self.cursor_tracking_after_id is not None:
            self.root.after_cancel(self.cursor_tracking_after_id)
            self.cursor_tracking_after_id = None
        self.cursor_position_button.configure(text="开始实时读取")
        self._hide_cursor_tracking_mini()
        if self.main_hidden_for_cursor_tracking:
            self.main_hidden_for_cursor_tracking = False
            if not self.exiting:
                self._restore_main_window()
        self._set_status(f"已停止读取，最后坐标：{self.cursor_position_var.get()}", "success")
        self._log(f"停止实时读取光标坐标；最后结果：{self.cursor_position_var.get()}。")

    def _restore_saved_window_binding(self, update_display: bool = True) -> bool:
        signature = self.saved_window_signature
        if not signature:
            return False
        # A persisted target is the signal for the desktop-to-game workflow:
        # keep the recorder in automatic mode even before the game is opened.
        title = signature.get("title", "")
        class_name = signature.get("class_name", "")
        process_path = str(signature.get("process_path", ""))
        expected_path = os.path.normcase(process_path).casefold() if process_path else ""
        expected_rect = tuple(signature.get("window_rect", (0, 0, 0, 0)))
        expected_client = tuple(signature.get("client_size", (0, 0)))
        windows = enum_windows()
        foreground = get_foreground_window_info()

        def path_matches(item):
            return not expected_path or not item.process_path or os.path.normcase(item.process_path).casefold() == expected_path

        def identity_matches(item):
            return (
                (not class_name or item.class_name == class_name)
                and (not title or item.title == title)
                and path_matches(item)
                and (not any(expected_rect) or tuple(item.window_rect) == expected_rect)
                and (not any(expected_client) or tuple(item.client_size) == expected_client)
            )

        def shape_matches(item):
            return (
                (not class_name or item.class_name == class_name)
                and path_matches(item)
            )

        # Runtime window handles change whenever a game restarts. Resolve the
        # persisted target by its stable window
        # identity instead, preferring the current foreground window when all
        # saved properties match. Volatile runtime identifiers are deliberately
        # not part of the saved identity.
        exact = [item for item in windows if identity_matches(item)]
        if exact:
            selected = next((item for item in exact if foreground and item.hwnd == foreground.hwnd), exact[0])
        else:
            same_class = [item for item in windows if shape_matches(item)]
            if foreground and shape_matches(foreground):
                selected = foreground
            elif len(same_class) == 1:
                selected = same_class[0]
            else:
                same_title = [item for item in windows if title and item.title == title]
                selected = same_title[0] if len(same_title) == 1 else None
        if selected:
            self.bound_window = selected
            if update_display:
                self.bind_label_var.set(selected.title)
            return True
        self.bound_window = None
        if update_display:
            self.bind_label_var.set(f"已保存，等待窗口：{title}")
        return False

    def _bound_hwnd(self, update_display: bool = True) -> int | None:
        foreground = get_foreground_window_info()
        if foreground and not is_current_process_window(foreground.hwnd) \
                and self._foreground_matches_target(foreground):
            # 已保存的旧 HWND 可能仍有效但已不是当前游戏窗口。当前前台明确
            # 符合目标身份时优先重新绑定，窗口区域识别才能截取正确画面。
            previous_hwnd = int(self.bound_window.hwnd) if self.bound_window else 0
            self.bound_window = foreground
            bind_label_var = getattr(self, "bind_label_var", None)
            if update_display and bind_label_var is not None:
                bind_label_var.set(foreground.title)
            if update_display and previous_hwnd and previous_hwnd != int(foreground.hwnd):
                ui = getattr(self, "_ui", None)
                if ui is not None and getattr(self, "root", None) is not None:
                    ui(
                        self._log,
                        f"检测到当前前台目标窗口已变化，已从 HWND={previous_hwnd} "
                        f"重新绑定到 HWND={foreground.hwnd}。",
                    )
            return foreground.hwnd
        if not self.bound_window:
            if not self._restore_saved_window_binding(update_display=update_display):
                return None
        if not is_window(self.bound_window.hwnd):
            self.bound_window = None
            if not self._restore_saved_window_binding(update_display=update_display):
                if update_display:
                    self._log("已保存的目标窗口当前未打开。")
                return None
        return self.bound_window.hwnd

    # Recording
    def toggle_record(self, from_ui: bool = False):
        if self.recorder.running:
            self.stop_recording(discard_recent=from_ui)
        else:
            self.start_recording(from_ui=from_ui)

    def start_recording(self, from_ui: bool = False):
        if self.worker and self.worker.is_alive():
            self._notify("正在运行", "请先停止当前脚本或工作流。")
            return
        hwnd = self._bound_hwnd()
        # A bound window may be either a game or an ordinary desktop program.
        # Center-lock detection is what makes the single recording mode choose
        # relative camera deltas without a separate game-mode switch.
        relative_requires_center_lock = bool(hwnd)
        if not hwnd and not from_ui:
            foreground = get_foreground_window_info()
            if foreground and not is_current_process_window(foreground.hwnd):
                # F8 inside an unbound game should work without a setup step.
                # Requiring a stable center cursor keeps ordinary desktop apps
                # on absolute coordinates even when recording starts by F8.
                hwnd = foreground.hwnd
                relative_requires_center_lock = True
        try:
            interval = max(10, min(500, int(self.interval_var.get())))
            self.interval_var.set(interval)
            if not force_english_input(hwnd):
                raise RuntimeError("无法切换到英语输入法，请确认系统已安装英语（美国）键盘。")
            self.recorder.start(
                "auto", interval,
                target_hwnd=hwnd,
                target_relative_enabled=True,
                relative_requires_center_lock=relative_requires_center_lock,
            )
        except Exception as exc:
            self._notify("无法录制", str(exc))
            self._log(f"启动录制失败：{exc}")
            return
        self.recording_screen = get_virtual_screen_rect()
        self._detach_open_script_for_recording()
        self.script.actions = []
        self._clear_action_undo()
        self.record_started_at = time.perf_counter()
        self.recording_capture_mode = self.recorder.current_mode()
        self.rebuild_action_tree()
        self.record_button.configure(text="停止录制    F8", bootstyle="danger")
        self._set_status("正在录制输入…", "error")
        target_note = "已绑定目标" if self.saved_window_signature else (
            "正在识别锁中心游戏" if relative_requires_center_lock else "桌面坐标"
        )
        self._log(f"开始智能录制：{target_note}；桌面间隔 {interval} ms，游戏转向间隔不高于 16 ms。")
        self._log("录制前已强制切换为英语（美国）输入法，并关闭中文输入状态。")
        self._sound("record_start")
        self._show_recording_mini()
        initial_mode = "游戏转向模式（相对轨迹）" if self.recording_capture_mode == "relative" else "桌面模式（绝对坐标）"
        self._append_mini_step(f"当前模式：{initial_mode}")
        self._poll_recording_mode()

    def _detach_open_script_for_recording(self):
        """Turn F8 recording into a new document instead of overwriting an open file."""
        if self.script_path is None:
            return
        original_name = self.script_name_var.get().strip() or self.script.name
        self.script_path = None
        self.script_requires_new_file = True
        self.script_name_var.set(f"{original_name}_新录制")
        self._log("新录制已与原脚本分离，保存时会创建新文件。")

    def _poll_recording_mode(self):
        if not self.recorder.running:
            return
        # The game may be launched after recording starts and its window handle
        # changes on every launch. Re-resolve the saved title/class signature
        # while recording so auto mode can switch to raw relative capture as
        # soon as the current target becomes foreground.
        if self.saved_window_signature:
            previous_hwnd = self.recorder.target_hwnd
            foreground = get_foreground_window_info()
            resolved_hwnd = foreground.hwnd if foreground and self._foreground_matches_target(foreground) else self._bound_hwnd()
            if resolved_hwnd != previous_hwnd:
                self.recorder.target_hwnd = resolved_hwnd
                if resolved_hwnd:
                    self._log(f"已重新绑定当前目标窗口，HWND={resolved_hwnd}；后续按前台状态自动选择坐标模式。")
        current = self.recorder.current_mode()
        if current != self.recording_capture_mode:
            self.recording_capture_mode = current
            label = "游戏转向模式（相对轨迹）" if current == "relative" else "桌面模式（绝对坐标）"
            self._set_status(f"正在录制：{label}", "warning")
            self._log(f"录制模式已切换：{label}。")
            self._append_mini_step(f"模式切换到：{label}")
        self.root.after(180, self._poll_recording_mode)

    def stop_recording(self, discard_recent: bool = False, sound: bool = True):
        if discard_recent:
            removed = self.recorder.discard_recent(600)
            if removed:
                self._log(f"已清理悬浮窗操作产生的 {removed} 条末尾事件。")
        actions = self.recorder.stop()
        ensure_action_ids(actions)
        self.script.actions = actions
        self.script.settings = self._current_script_settings(self.recording_screen)
        self.recording_screen = None
        self._refresh_coordinate_scale_status()
        self.recording_capture_mode = ""
        self.record_button.configure(text="开始录制    F8", bootstyle="danger")
        self.rebuild_action_tree()
        self._mark_dirty()
        self._set_status(f"录制完成：{len(actions)} 个动作", "success")
        self._log(f"录制完成，共 {len(actions)} 个动作。")
        self._hide_recording_mini()
        if self.main_hidden_for_recording:
            self._restore_main_window()
        if sound:
            self._sound("record_stop")
        if self.recorder.limit_reached:
            self._notify("已达到安全上限", "录制达到 200,000 个动作。请保存并拆分脚本，避免界面和执行卡顿。")

    def _record_action_callback(self, action: dict):
        count = len(self.recorder.actions)
        self._ui(self._show_recorded_action, dict(action), count)

    def _show_recorded_action(self, action: dict, count: int):
        if not self.recorder.running:
            return
        self.record_count_var.set(f"{count} 个动作（录制中）")
        delay = int(action.get("delay_ms", 0))
        self._append_mini_step(f"#{count}  {recorded_action_description(action)} · 间隔 {delay} ms")

    # Script persistence and tree
    def rebuild_action_tree(self):
        self.action_tree.delete(*self.action_tree.get_children())
        action_rows = {
            str(action.get(ACTION_ID_KEY, "")): index + 1
            for index, action in enumerate(self.script.actions)
            if action.get(ACTION_ID_KEY)
        }
        detail_texts = []
        for index, action in enumerate(self.script.actions[:MAX_TREE_ROWS]):
            kind, detail, delay = action_summary(action, action_rows)
            detail_texts.append(detail)
            self.action_tree.insert("", "end", iid=str(index), values=(index + 1, kind, detail, delay))
        self._autosize_tree_column(self.action_tree, "detail", 590, detail_texts)
        total = len(self.script.actions)
        suffix = "（仅显示前 20,000 条）" if total > MAX_TREE_ROWS else ""
        self.record_count_var.set(f"{total} 个动作{suffix}")
        if total == 0:
            self.empty_action_hint.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self.empty_action_hint.place_forget()
        self._sync_global_script_marker()
        self._update_action_edit_button()

    def _sync_global_script_marker(self):
        # v1.68 起普通脚本可内嵌全局模块行（global_detect + jump_row），
        # 它们不代表全局脚本；只有类别为全局或带触发条件才标记为全局脚本。
        category = self.script.settings.get("category", "level")
        is_global = bool(self.script.settings.get("trigger"))
        self.script.is_global = is_global
        marker = "◈ 旧全局触发脚本" if is_global else ""
        self.global_script_marker.configure(text=marker)
        self._refresh_global_trigger_section()

    def _refresh_global_trigger_section(self):
        """全局脚本显示"触发条件 + 语句体"区块；普通脚本隐藏。"""
        if not getattr(self, "trigger_section", None):
            return
        if self.script.is_global:
            self.trigger_section.pack(fill="x")
            trigger = self.script.settings.get("trigger") or {}
            summary = self._trigger_summary(trigger)
            self.trigger_summary_var.set(summary)
            self.trigger_summary_label.configure(
                foreground=COLOR_RED if not trigger.get("template") else COLOR_TEXT,
            )
            if trigger.get("template"):
                self.clear_trigger_button.pack(side="right", padx=(6, 0))
            else:
                self.clear_trigger_button.pack_forget()
        else:
            self.trigger_section.pack_forget()

    def _trigger_summary(self, trigger: dict) -> str:
        """触发条件的摘要文本（用于脚本编辑器的触发条件区块）。"""
        template = str(trigger.get("template", ""))
        if not template:
            return "未配置触发条件（编辑后保存，识别成功即触发执行语句体）"
        parts = [Path(template).name]
        region = trigger.get("region") or []
        mode = str(trigger.get("region_mode", ""))
        if mode == "template":
            parts.append("区域：模板")
        elif mode == "window":
            parts.append("区域：目标窗口")
        elif mode == "custom" or len(region) == 4:
            try:
                parts.append("区域：" + ",".join(str(int(part)) for part in region))
            except (TypeError, ValueError):
                pass
        else:
            parts.append("区域：全屏")
        try:
            parts.append(f"持续超过 {int(trigger.get('hold_ms', 1000))} ms")
        except (TypeError, ValueError):
            pass
        return " · ".join(parts)

    def _edit_global_trigger(self):
        trigger = dict(self.script.settings.get("trigger") or {})
        config = GlobalDetectDialog(self.root, trigger, require_click=False).show()
        if config is None:
            return
        config.pop("type", None)
        self.script.settings["trigger"] = config
        self._mark_dirty()
        self._sync_global_script_marker()
        self._set_status("触发条件已更新：识别成功后依次执行脚本内的所有动作", "success")

    def _clear_global_trigger(self):
        self.script.settings.pop("trigger", None)
        self._mark_dirty()
        self._sync_global_script_marker()

    def _reset_script_editor(self):
        """把脚本编辑器重置为空白新脚本（不清除撤销打开栈）。"""
        self.script = self._blank_script_with_activation_draft()
        self.script_path = None
        self.script_requires_new_file = False
        self.script_name_var.set(self.script.name)
        self.record_mode_var.set("auto")
        self.interval_var.set(DEFAULT_MOUSE_MOVE_INTERVAL_MS)
        self.script_category_var.set("关卡")
        self.dirty = False
        self._clear_action_undo()
        self.rebuild_action_tree()
        self._refresh_coordinate_scale_status()
        self._sync_activation_ui_from_script()

    def new_script(self):
        if self.dirty:
            self._notify("当前修改尚未保存", "请先保存脚本或撤销修改后再新建。")
            return
        self._reset_script_editor()
        self.undo_open_stack = []
        self._update_undo_open_button()
        self._set_status("已新建脚本", "success")

    def close_script(self):
        """关闭当前脚本：保留一份快照供撤销打开恢复，然后清空编辑器。"""
        snapshot = {
            "script": copy.deepcopy(self.script),
            "script_path": self.script_path,
            "script_requires_new_file": self.script_requires_new_file,
            "name": self.script_name_var.get(),
            "interval": self.interval_var.get(),
            "category": self.script_category_var.get(),
            "dirty": self.dirty,
            "action_undo_stack": copy.deepcopy(getattr(self, "action_undo_stack", [])),
            "action_redo_stack": copy.deepcopy(getattr(self, "action_redo_stack", [])),
        }
        history = getattr(self, "undo_open_stack", None)
        if history is None:
            history = self.undo_open_stack = []
        history.append(snapshot)
        if len(history) > 10:
            del history[:-10]
        self._reset_script_editor()
        self._update_undo_open_button()
        self._set_status("已关闭脚本，可撤销打开", "success")
        self._log("关闭脚本，可撤销打开")

    def undo_open_script(self):
        """撤销上次的关闭/打开操作，恢复关闭前的脚本及其编辑状态。"""
        history = getattr(self, "undo_open_stack", [])
        if not history:
            self._update_undo_open_button()
            return
        snapshot = history.pop()
        self.script = snapshot["script"]
        self.script_path = snapshot["script_path"]
        self.script_requires_new_file = snapshot["script_requires_new_file"]
        self.script_name_var.set(snapshot["name"])
        self.interval_var.set(snapshot["interval"])
        self.script_category_var.set(snapshot["category"])
        self.dirty = snapshot["dirty"]
        self.action_undo_stack = snapshot["action_undo_stack"]
        self.action_redo_stack = snapshot.get("action_redo_stack", [])
        self.rebuild_action_tree()
        self._refresh_coordinate_scale_status()
        self._sync_activation_ui_from_script()
        self._update_undo_button()
        self._update_redo_button()
        self._update_undo_open_button()
        self._set_status("已撤销打开，恢复关闭前的脚本", "success")

    def _update_undo_open_button(self):
        button = getattr(self, "undo_open_button", None)
        if button is not None:
            button.configure(
                state="normal" if getattr(self, "undo_open_stack", []) else "disabled",
            )

    def open_new_window(self):
        """Launch a second MacroFlow instance starting with a new script."""
        try:
            args = [sys.executable]
            if not getattr(sys, "frozen", False):
                args.append(str(Path(__file__).resolve()))
            args.append("--new-script")
            spawn_new_instance(args)
        except Exception as exc:
            self._notify("无法新开窗口", str(exc))
            return
        self._log("已新开一个 MacroFlow 窗口（新建脚本）。")
        self._set_status("已新开窗口", "success")

    def _show_action_context_menu(self, event):
        """Right-click menu on the action list; offers to open referenced scripts in a new window."""
        row_id = self.action_tree.identify_row(event.y)
        if not row_id:
            return
        self.action_tree.selection_set(row_id)
        index = int(row_id)
        if index >= len(self.script.actions):
            return
        action = self.script.actions[index]
        if str(action.get("type")) != "script_ref":
            return
        menu = tk.Menu(
            self.root, tearoff=False,
            background=COLOR_SURFACE, foreground=COLOR_TEXT,
            activebackground="#1D4358", activeforeground="#FFFFFF",
            borderwidth=1, relief="solid",
        )
        menu.add_command(
            label="⇪ 在新窗口打开引用的脚本",
            command=lambda: self.open_referenced_script_in_new_window(action),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_referenced_script_in_new_window(self, action: dict):
        """Launch a second MacroFlow window loading the referenced script."""
        ref_value = str(action.get("script", "")).strip()
        if not ref_value:
            self._notify("引用脚本无效", "该引用动作没有脚本路径。")
            return
        ref_path = resolve_path(ref_value)
        if not ref_path.is_file():
            self._notify("引用脚本不存在", f"找不到文件：{ref_value}")
            return
        try:
            args = [sys.executable]
            if not getattr(sys, "frozen", False):
                args.append(str(Path(__file__).resolve()))
            args += ["--open-script", str(ref_path)]
            spawn_new_instance(args)
        except Exception as exc:
            self._notify("无法新开窗口", str(exc))
            return
        self._log(f"已在新窗口打开引用的脚本：{ref_path}")
        self._set_status(f"已在新窗口打开 {ref_path.name}", "success")

    def _show_workflow_context_menu(self, event):
        """Right-click menu on the workflow table; offers to open the step's script."""
        row_id = self.workflow_tree.identify_row(event.y)
        if not row_id:
            return
        self.workflow_tree.selection_set(row_id)
        index = int(row_id)
        steps = self._workflow_only_steps()
        if index >= len(steps):
            return
        step = steps[index]
        if not str(step.get("script", "")).strip():
            return
        menu = tk.Menu(
            self.root, tearoff=False,
            background=COLOR_SURFACE, foreground=COLOR_TEXT,
            activebackground="#1D4358", activeforeground="#FFFFFF",
            borderwidth=1, relief="solid",
        )
        menu.add_command(
            label="▶ 单独执行一次测试",
            command=lambda: self.run_workflow_script_alone(step),
        )
        menu.add_command(
            label="⇪ 在新窗口打开脚本",
            command=lambda: self.open_referenced_script_in_new_window(step),
        )
        menu.add_command(
            label="✎ 在当前编辑器打开",
            command=lambda: self._open_workflow_script_in_editor(step),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _open_workflow_script_in_editor(self, step: dict):
        """Load a workflow step's script into the current script editor."""
        ref_value = str(step.get("script", "")).strip()
        ref_path = resolve_path(ref_value)
        if not ref_path.is_file():
            self._notify("脚本不存在", f"找不到文件：{ref_value}")
            return
        if self.dirty:
            self._notify("当前修改尚未保存", "请先保存脚本或撤销修改后再打开。")
            return
        self.load_script_into_editor(ref_path)

    def run_workflow_script_alone(self, step: dict):
        """工作流右键“单独执行一次测试”：加载该行脚本执行 1 次，不扣减工作流次数。

        使用该行脚本自己的前置窗口与录屏设置（同打开脚本按 F9），
        目标窗口沿用当前编辑器绑定，与工作流执行保持一致。
        """
        script_value = str(step.get("script", "")).strip()
        script_path = resolve_path(script_value)
        if not script_path.is_file():
            self._notify("脚本不存在", f"找不到文件：{script_value}")
            return
        try:
            script = load_script(script_path)
        except Exception as exc:
            self._notify("无法加载脚本", f"脚本解析失败：{exc}")
            return
        if self.recorder.running:
            self.stop_recording()
        if self.worker and self.worker.is_alive():
            self._notify("正在运行", "已有脚本或工作流正在执行。")
            return
        trigger = dict(script.settings.get("trigger") or {})
        if not script.actions and not trigger.get("template"):
            self._notify("没有动作", f"脚本 {script.name} 没有动作，无法执行。")
            return
        hwnd = self._bound_hwnd()
        activation_enabled = bool(script.settings.get("activation_window_enabled", False))
        activation_signature = script.settings.get("activation_window")
        if isinstance(activation_signature, dict) and activation_signature.get("title"):
            activation_signature = {
                "title": str(activation_signature.get("title", "")),
                "class_name": str(activation_signature.get("class_name", "")),
                "process_path": str(activation_signature.get("process_path", "")),
            }
        else:
            activation_signature = None
        try:
            activation_hwnd = self._execution_activation_hwnd(
                hwnd, activation_enabled, activation_signature,
            )
        except RuntimeError as exc:
            self._notify("无法执行", str(exc))
            return
        focus_enabled = bool(self.focus_mode_enabled_var.get())
        activate_target = bool(self.activate_target_enabled_var.get())
        self.execution_focus_requested = focus_enabled
        source_screen = dict(script.settings.get("recorded_screen", {})) or None
        self.workflow_stop.clear()
        self._sound("run_start")
        self._hide_main_for_execution()
        self.execution_started_at = time.perf_counter()
        self._set_execution_progress(f"单独测试 · {script.name} · 共执行 1 次 · 正在准备 · F12 停止")
        self.worker = threading.Thread(
            target=self._run_script_worker,
            args=(list(script.actions), 1, hwnd, activation_hwnd,
                  source_screen, focus_enabled, activate_target, 0),
            kwargs={"trigger": trigger},
            daemon=True,
        )
        self.worker.start()
        self._show_execution_mini()
        self._append_mini_step(f"单独执行一次测试：脚本 {script.name}，共 1 次。")

    def _load_startup_script(self, path: Path):
        if not path.is_file():
            self._notify("无法打开引用脚本", f"文件不存在：{path}")
            return
        self.load_script_into_editor(path)

    def _current_script_settings(self, recorded_screen: dict | None = None) -> dict:
        settings = dict(self.script.settings)
        settings.update({
            "record_mode": "auto",
            "move_interval_ms": int(self.interval_var.get()),
            "activation_window_enabled": bool(self.activation_enabled_var.get()),
            "activation_window": (
                dict(self.saved_activation_signature) if self.saved_activation_signature else None
            ),
        })
        if recorded_screen:
            settings["recorded_screen"] = dict(recorded_screen)
        else:
            settings.setdefault("recorded_screen", dict(DEFAULT_RECORDED_SCREEN))
        return settings

    def _resolve_scripts_dir(self, var_name: str, default: str) -> Path:
        var = getattr(self, var_name, None)
        value = var.get().strip() if var is not None else default
        path = Path(value)
        return path if path.is_absolute() else BASE_DIR / path

    def _level_scripts_dir(self) -> Path:
        return self._resolve_scripts_dir("level_scripts_dir_var", "scripts/关卡")

    def _level_pack_scripts_dir(self) -> Path:
        return self._resolve_scripts_dir("level_pack_scripts_dir_var", "scripts/关卡封装")

    def _switch_scripts_dir(self) -> Path:
        return self._resolve_scripts_dir("switch_scripts_dir_var", "scripts/切换")

    def _script_category_dir(self) -> Path:
        var = getattr(self, "script_category_var", None)
        label = var.get() if var is not None else "关卡"
        if label == "关卡封装":
            return self._level_pack_scripts_dir()
        if label == "切换":
            return self._switch_scripts_dir()
        return self._level_scripts_dir()

    def _script_category_changed(self, _event=None):
        label = self.script_category_var.get()
        self.script.settings["category"] = script_category_key(label)
        self._mark_dirty()
        self._sync_global_script_marker()
        self._set_status(f"脚本类别已改为：{label}", "success")

    def _set_insert_position(self, above: bool):
        self.insert_position_var.set("above" if above else "below")
        self.insert_above_button.configure(
            bootstyle="primary" if above else "secondary",
        )
        self.insert_below_button.configure(
            bootstyle="primary" if not above else "secondary",
        )

    def open_template_region_manager(self):
        """打开统一的"模板区域"管理模块：查看/修改每个模板图片登记的搜索区域。

        识图与全局识图共用同一份登记（template_regions.json），选中模板时自动导入。
        """
        TemplateRegionManagerDialog(self.root).show()
        if getattr(self, "workflow_tree", None) is not None:
            self.rebuild_workflow_tree()

    def jump_to_module_reference(self, path: Path, action_index: int):
        """Open a script and select the referenced module row in the editor."""
        path = Path(path)
        if not path.exists():
            self._notify("引用脚本不存在", f"找不到文件：{path}")
            return
        self.load_script_into_editor(path)
        index = int(action_index)

        def select_row():
            tree = getattr(self, "action_tree", None)
            if tree is None or not 0 <= index < len(self.script.actions):
                return
            row = str(index)
            tree.selection_set(row)
            tree.focus(row)
            tree.see(row)

        self.root.after_idle(select_row)

    def _configure_script_directories(self):
        values = ScriptDirectoriesDialog(
            self.root,
            level_dir=self.level_scripts_dir_var.get(),
            level_pack_dir=self.level_pack_scripts_dir_var.get(),
            switch_dir=self.switch_scripts_dir_var.get(),
        ).show()
        if not values:
            return
        self.level_scripts_dir_var.set(values["level_dir"])
        self.level_pack_scripts_dir_var.set(values["level_pack_dir"])
        self.switch_scripts_dir_var.set(values["switch_dir"])
        self._level_scripts_dir().mkdir(parents=True, exist_ok=True)
        self._level_pack_scripts_dir().mkdir(parents=True, exist_ok=True)
        self._switch_scripts_dir().mkdir(parents=True, exist_ok=True)
        self._persist_sidebar_settings()
        self._set_status("脚本保存目录已更新", "success")
        self._log(
            f"关卡目录：{self._level_scripts_dir()}；关卡封装目录：{self._level_pack_scripts_dir()}；"
            f"切换目录：{self._switch_scripts_dir()}",
        )

    def open_script(self):
        initial_dir = self._script_category_dir()
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=initial_dir, title="打开脚本",
            filetypes=[("MacroFlow 脚本", "*.json"), ("所有文件", "*.*")],
        )
        if path:
            self.load_script_into_editor(Path(path))

    def load_script_into_editor(self, path: Path):
        try:
            self.script = load_script(path)
            ensure_action_ids(self.script.actions)
            # 文件名是脚本引用和工作流定位脚本的唯一外部身份。
            # 脚本在目录中被改名后，JSON 内旧的 name 不能继续覆盖编辑器名称，
            # 否则保存会再次按旧名称写回/移动文件。
            path = Path(path)
            self.script.name = path.stem
            self.script_path = path
            self.script_requires_new_file = False
            self.script_name_var.set(self.script.name)
            self.record_mode_var.set("auto")
            self.interval_var.set(int(self.script.settings.get(
                "move_interval_ms", DEFAULT_MOUSE_MOVE_INTERVAL_MS,
            )))
            category_key = str(self.script.settings.get("category", "level"))
            self.script_category_var.set({
                "level": "关卡", "level_pack": "关卡封装",
                "switch": "切换",
            }.get(category_key, "关卡"))
            self.dirty = False
            self._clear_action_undo()
            self.undo_open_stack = []
            self._update_undo_open_button()
            self.rebuild_action_tree()
            self._refresh_coordinate_scale_status()
            self._sync_activation_ui_from_script()
            self._set_status(f"已打开 {path.name}", "success")
            self._log(f"打开脚本：{path}")
        except Exception as exc:
            self._notify("打开失败", str(exc))

    def save_current_script(self):
        name = self.script_name_var.get().strip() or "未命名脚本"
        self.script.name = name
        self.script.settings = self._current_script_settings()
        category_var = getattr(self, "script_category_var", None)
        category_label = category_var.get() if category_var is not None else "关卡"
        self.script.settings["category"] = script_category_key(category_label)
        ensure_action_ids(self.script.actions)
        self.script.is_global = is_global_script(self.script.to_dict())
        self._refresh_coordinate_scale_status()
        if category_label == "关卡封装":
            target_dir = self._level_pack_scripts_dir()
        elif category_label == "切换":
            target_dir = self._switch_scripts_dir()
        else:
            target_dir = self._level_scripts_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = self.script_path
        moved_from = None
        if self.script_requires_new_file:
            target = available_script_path(name, target_dir)
        elif target is None or target.stem != name:
            target = available_script_path(name, target_dir)
        elif target.parent != target_dir:
            # 类别变化（或脚本应归入其他目录）：保存到新目录并移走旧位置的文件。
            moved_from = target
            target = available_script_path(name, target_dir)
        try:
            self.script_path = save_script(self.script, target)
            self.script_requires_new_file = False
            self.dirty = False
            self._clear_action_undo()
            self.refresh_script_files()
            if getattr(self, "workflow_tree", None) is not None:
                self.rebuild_workflow_tree()
            if moved_from is not None and moved_from != self.script_path:
                try:
                    moved_from.unlink()
                except OSError:
                    pass
            if moved_from is not None:
                self._set_status(f"已保存并移动到 {self.script_path.parent.name}/{self.script_path.name}", "success")
                self._log(f"保存脚本并移动：{moved_from} → {self.script_path}")
            else:
                self._set_status(f"已保存 {self.script_path.name}", "success")
                self._log(f"保存脚本：{self.script_path}")
            return self.script_path
        except Exception as exc:
            self._notify("保存失败", str(exc))
            return None

    def refresh_script_files(self):
        pass

    def _selected_action_index(self) -> int | None:
        selected = self.action_tree.selection()
        return int(selected[0]) if selected else None

    def _select_all_actions(self, _event=None):
        """Select every action row when Ctrl+A is pressed in the script list."""
        rows = self.action_tree.get_children()
        if rows:
            self.action_tree.selection_set(*rows)
            self.action_tree.focus(rows[0])
            self.action_tree.see(rows[0])
        return "break"

    def _update_undo_button(self):
        button = getattr(self, "undo_button", None)
        if button is not None:
            button.configure(state="normal" if getattr(self, "action_undo_stack", []) else "disabled")

    def _update_redo_button(self):
        button = getattr(self, "redo_button", None)
        if button is not None:
            button.configure(state="normal" if getattr(self, "action_redo_stack", []) else "disabled")

    def _clear_action_undo(self):
        self.action_undo_stack = []
        self.action_redo_stack = []
        self._update_undo_button()
        self._update_redo_button()

    def _checkpoint_action_edit(self):
        history = getattr(self, "action_undo_stack", None)
        if history is None:
            history = self.action_undo_stack = []
        snapshot = copy.deepcopy(self.script.actions)
        if not history or history[-1] != snapshot:
            history.append(snapshot)
            if len(history) > 100:
                del history[:-100]
            # 新的编辑使"重做"历史失效：撤销之后改动作，重做栈作废。
            if getattr(self, "action_redo_stack", None):
                self.action_redo_stack = []
                self._update_redo_button()
        self._update_undo_button()

    def undo_action_edit(self):
        history = getattr(self, "action_undo_stack", [])
        if not history:
            self._update_undo_button()
            return
        selected = self._selected_action_index()
        redo_history = getattr(self, "action_redo_stack", None)
        if redo_history is None:
            redo_history = self.action_redo_stack = []
        redo_history.append(copy.deepcopy(self.script.actions))
        self.script.actions = history.pop()
        self._mark_dirty()
        self.rebuild_action_tree()
        if self.script.actions and selected is not None:
            restored_index = min(selected, len(self.script.actions) - 1)
            if restored_index < MAX_TREE_ROWS:
                self.action_tree.selection_set(str(restored_index))
                self.action_tree.see(str(restored_index))
        self._update_undo_button()
        self._update_redo_button()
        self._set_status("已撤销上一次脚本编辑", "success")

    def redo_action_edit(self):
        redo_history = getattr(self, "action_redo_stack", [])
        if not redo_history:
            self._update_redo_button()
            return
        selected = self._selected_action_index()
        undo_history = getattr(self, "action_undo_stack", None)
        if undo_history is None:
            undo_history = self.action_undo_stack = []
        undo_history.append(copy.deepcopy(self.script.actions))
        self.script.actions = redo_history.pop()
        self._mark_dirty()
        self.rebuild_action_tree()
        if self.script.actions and selected is not None:
            restored_index = min(selected, len(self.script.actions) - 1)
            if restored_index < MAX_TREE_ROWS:
                self.action_tree.selection_set(str(restored_index))
                self.action_tree.see(str(restored_index))
        self._update_undo_button()
        self._update_redo_button()
        self._set_status("已重做上一次脚本编辑", "success")

    def _insert_action(self, action: dict):
        index = self._selected_action_index()
        position_var = getattr(self, "insert_position_var", None)
        position = position_var.get() if position_var is not None else "below"
        if position == "above":
            insert_at = index if index is not None else 0
        else:
            insert_at = len(self.script.actions) if index is None else index + 1
        action = dict(action)
        action[ACTION_ID_KEY] = new_action_id()
        self._checkpoint_action_edit()
        self.script.actions.insert(insert_at, action)
        self._mark_dirty()
        self.rebuild_action_tree()
        if insert_at < MAX_TREE_ROWS:
            self.action_tree.selection_set(str(insert_at))
            self.action_tree.see(str(insert_at))
        return insert_at

    def add_delay(self):
        value = DurationDialog(self.root, "插入延时", "延时时间：", 500).show()
        if value is not None:
            self._insert_action({"type": "delay", "ms": value, "delay_ms": 0})

    def add_jump(self):
        ensure_action_ids(self.script.actions)
        action = JumpActionDialog(self.root, actions=self.script.actions).show()
        if action:
            self._insert_action(action)

    def add_key(self):
        action = KeyActionDialog(self.root).show()
        if action:
            self._insert_action(action)

    def add_text(self):
        text = simpledialog.askstring("插入文本", "要输入的文本：", parent=self.root)
        if text is not None:
            self._insert_action({"type": "text", "text": text, "char_delay_ms": 15, "delay_ms": 0})

    def add_notice(self):
        text = simpledialog.askstring("添加浮动提醒", "提醒会显示 3 秒，脚本不会暂停。\n提醒文字：", parent=self.root)
        if text is not None and text.strip():
            self._insert_action({
                "type": "notice", "text": text.strip(),
                "duration_ms": 3000, "delay_ms": 0,
            })

    def add_mouse_move(self):
        action = MouseMoveDialog(self.root).show()
        if action:
            self._insert_action(action)

    def add_click(self):
        action = ClickDialog(self.root).show()
        if action:
            self._insert_action(action)

    def add_repeat_click(self):
        action = RepeatClickDialog(self.root).show()
        if action:
            self._insert_action(action)

    def add_open_app(self):
        action = OpenAppDialog(self.root).show()
        if action:
            self._insert_action(action)

    def add_close_app(self):
        action = CloseAppDialog(self.root).show()
        if action:
            self._insert_action(action)

    def add_global_detect(self):
        # 普通脚本内嵌全局模块行：播放到该行时启用全局检测，触发后跳转到脚本第 N 行。
        # 全局脚本的触发条件在"触发条件"区块配置，不能添加模块行。
        if self.script.settings.get("trigger"):
            self._notify(
                "不能添加",
                "全局脚本在“触发条件”区块配置识别设置，不需要也不能添加全局模块行。",
            )
            return
        ensure_action_ids(self.script.actions)
        action = GlobalDetectDialog(self.root, jump=True, actions=self.script.actions).show()
        if action:
            self._insert_action(action)

    def add_module(self):
        """打开模块选择窗口，把选中的模块对象 / 特殊动作插入脚本。

        全局模块引用（检测型，含 1.81 旧格式 module_category="special"）插入后
        补上默认跳转行（否则 models.from_dict 会把无 jump_row 的全局检测行迁成
        settings["trigger"]）。
        """
        ensure_action_ids(self.script.actions)
        action = ModulePickerDialog(self.root, actions=self.script.actions).show()
        if not action:
            return
        if action.get("module_ref") and action.get("module_category") in (
                "script_global", "global", "special") and self.script.settings.get("trigger"):
            self._notify(
                "不能添加",
                "全局脚本在“触发条件”区块配置识别设置，不能添加全局模块行。",
            )
            return
        module_key = str(action.get("module_key") or action.get("template", ""))
        module_obj = registered_module_object(module_key)
        if module_obj and module_obj.get("recognize") == "number":
            configured = edit_action(
                self.root, action, all_actions=self.script.actions,
            )
            if configured is None:
                return
            action = configured
        insert_at = self._insert_action(action)
        if action.get("module_ref") and action.get("module_category") in (
                "script_global", "global", "special"):
            self._default_global_jump(insert_at)

    def _default_global_jump(self, insert_at: int):
        """给刚插入的全局模块引用行补默认跳转。

        中间插入：跳到下一行；末尾插入：跳转行号用越界值 len+1，触发后
        代码段 / 动作播完脚本自然结束（不跳转）。
        """
        actions = self.script.actions
        jump_row = insert_at + 2
        jump_action_id = ""
        if insert_at + 1 < len(actions):
            jump_action_id = str(actions[insert_at + 1].get(ACTION_ID_KEY, "")).strip()
        else:
            jump_row = len(actions) + 1
        row = actions[insert_at]
        row["jump_row"] = jump_row
        if jump_action_id:
            row["jump_action_id"] = jump_action_id
        self._mark_dirty()
        self.rebuild_action_tree()
        if insert_at < MAX_TREE_ROWS:
            self.action_tree.selection_set(str(insert_at))
            self.action_tree.see(str(insert_at))

    def edit_selected_action(self):
        index = self._selected_action_index()
        if index is None:
            self._notify("编辑动作", "请先选择一条动作。")
            return
        if self.script.actions[index].get("type") in ("restart_workflow", "end_current_script"):
            self._set_status("特殊模块为固定动作，无需编辑", "success")
            return
        ensure_action_ids(self.script.actions)
        updated = edit_action(self.root, self.script.actions[index], self.script.actions)
        if updated:
            self._checkpoint_action_edit()
            self.script.actions[index] = updated
            self._mark_dirty()
            self.rebuild_action_tree()
            self.action_tree.selection_set(str(index))

    def _update_action_edit_button(self, _event=None):
        button = getattr(self, "edit_action_button", None)
        if button is None:
            return
        index = self._selected_action_index()
        editable = (
            index is not None
            and index < len(self.script.actions)
            and self.script.actions[index].get("type") not in ("restart_workflow", "end_current_script")
        )
        button.configure(state="normal" if editable else "disabled")

    def delete_actions(self):
        selected = sorted((int(item) for item in self.action_tree.selection()), reverse=True)
        if not selected:
            self._notify("删除动作", "请先选择一行或多行动作。")
            return
        next_selection = min(selected)
        self._checkpoint_action_edit()
        for index in selected:
            if index < len(self.script.actions):
                self.script.actions.pop(index)
        self._mark_dirty()
        self.rebuild_action_tree()
        if self.script.actions:
            # The original next row shifts into the deleted row's position.
            # When deleting the final row, keep the new final row selected.
            next_selection = min(next_selection, len(self.script.actions) - 1)
            item = str(next_selection)
            self.action_tree.selection_set(item)
            self.action_tree.focus(item)
            self.action_tree.see(item)
        self._set_status(f"已删除 {len(selected)} 行动作，可使用撤销恢复", "success")

    def copy_selected_actions_down(self):
        selected = sorted({int(item) for item in self.action_tree.selection()})
        if not selected:
            self._notify("向下复制", "请先选择一行或连续多行动作。")
            return
        if selected != list(range(selected[0], selected[-1] + 1)):
            self._notify("无法复制", "请选择连续的多行动作后再复制。")
            return
        if selected[-1] >= len(self.script.actions):
            return
        copies = clone_actions_with_new_ids(self.script.actions[selected[0]:selected[-1] + 1])
        insert_at = selected[-1] + 1
        self._checkpoint_action_edit()
        self.script.actions[insert_at:insert_at] = copies
        self._mark_dirty()
        self.rebuild_action_tree()
        copied_rows = [str(index) for index in range(insert_at, insert_at + len(copies)) if index < MAX_TREE_ROWS]
        if copied_rows:
            self.action_tree.selection_set(*copied_rows)
            self.action_tree.see(copied_rows[-1])
        self._set_status(f"已向下复制 {len(copies)} 行动作", "success")

    def _insert_script_position(self) -> int | None:
        """计算插入位置；未选中行且脚本已有动作时提示并返回 None。"""
        selected = sorted({int(item) for item in self.action_tree.selection()})
        if not selected and self.script.actions:
            self._notify("插入脚本", "请先选择插入位置所在的动作行。")
            return None
        position_var = getattr(self, "insert_position_var", None)
        position = position_var.get() if position_var is not None else "below"
        if position == "above":
            return selected[0] if selected else 0
        return min(len(self.script.actions), selected[-1] + 1) if selected else 0

    def _pick_script_file(self) -> Path | None:
        """文件选择 + 校验可解析为 MacroFlow 脚本，返回路径或 None。"""
        path = filedialog.askopenfilename(
            parent=self.root,
            initialdir=self._script_category_dir(),
            title="选择要插入的脚本",
            filetypes=[("MacroFlow 脚本", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return None
        try:
            # Validate the file parses as a MacroFlow script before inserting it.
            load_script(Path(path))
        except Exception as exc:
            self._notify("无法插入脚本", str(exc))
            return None
        return Path(path)

    def insert_script_reference(self):
        """插入一行引用动作，执行时实时读取原脚本最新内容。"""
        insert_at = self._insert_script_position()
        if insert_at is None:
            return
        path = self._pick_script_file()
        if path is None:
            return
        self._insert_script_reference(insert_at, path)

    def insert_script_expanded(self):
        """逐行插入：把脚本每一行复制到当前位置，插入后可单独修改。"""
        insert_at = self._insert_script_position()
        if insert_at is None:
            return
        path = self._pick_script_file()
        if path is None:
            return
        self._insert_script_expanded(insert_at, path)

    def _insert_script_reference(self, insert_at: int, path: Path):
        ref_action = {
            "type": "script_ref",
            "script": display_path(path),
            "delay_ms": 0,
            "after_delay_ms": 0,
        }
        ref_action[ACTION_ID_KEY] = new_action_id()
        self._checkpoint_action_edit()
        self.script.actions[insert_at:insert_at] = [ref_action]
        self._mark_dirty()
        self.rebuild_action_tree()
        if insert_at < MAX_TREE_ROWS:
            self.action_tree.selection_set(str(insert_at))
            self.action_tree.see(str(insert_at))
        self._notify(
            "已插入脚本引用",
            f"{path.stem} · 执行时实时读取该脚本 · 插入到第 {insert_at + 1} 行",
        )

    def _insert_script_expanded(self, insert_at: int, path: Path):
        """逐行插入：把脚本每一行复制进来，行 ID 全部重建、跳转引用同步映射。"""
        script = load_script(path)
        actions = [dict(action) for action in (script.actions or [])]
        id_map = {}
        for action in actions:
            old_id = str(action.get(ACTION_ID_KEY, "")).strip()
            if old_id:
                id_map[old_id] = new_action_id()
        for action in actions:
            old_id = str(action.get(ACTION_ID_KEY, "")).strip()
            if old_id:
                action[ACTION_ID_KEY] = id_map[old_id]
            for field in ("jump_action_id", "timeout_jump_action_id", "found_jump_action_id"):
                target = str(action.get(field, "")).strip()
                if target in id_map:
                    action[field] = id_map[target]
        self._checkpoint_action_edit()
        self.script.actions[insert_at:insert_at] = actions
        self._mark_dirty()
        self.rebuild_action_tree()
        if actions and insert_at < MAX_TREE_ROWS:
            self.action_tree.selection_set(str(insert_at))
            self.action_tree.see(str(insert_at))
        self._notify(
            "已逐行插入脚本",
            f"{path.stem} · 共 {len(actions)} 行插入到第 {insert_at + 1} 行，插入后可单独修改",
        )

    def move_action(self, offset: int):
        index = self._selected_action_index()
        if index is None:
            return
        target = index + offset
        if not 0 <= target < len(self.script.actions):
            return
        self._checkpoint_action_edit()
        self.script.actions[index], self.script.actions[target] = self.script.actions[target], self.script.actions[index]
        self._mark_dirty()
        self.rebuild_action_tree()
        self.action_tree.selection_set(str(target))
        self.action_tree.see(str(target))

    # Execution
    def _enter_focus_mode(self, hwnd: int | None, enabled: bool = True) -> bool:
        if not force_english_input(hwnd):
            raise RuntimeError("无法切换到英语输入法，已取消执行。")
        if not enabled:
            self._ui(self._log, "已切换英语输入法；强制专注模式未开启，实体键鼠不会被锁定。")
            return False
        if not self.input_guard.start():
            raise RuntimeError("无法启动专注模式，已取消执行以避免误触。")
        if not self.input_guard.block():
            self.input_guard.stop()
            raise RuntimeError("系统级输入锁定失败，请尝试以管理员身份运行软件。")
        self._ui(self._log, "已切换英语输入法并进入强制专注模式；桌面及游戏原始键鼠输入均已锁定，仅 F12 可紧急停止。")
        return True

    def _leave_focus_mode(self) -> None:
        guard = getattr(self, "input_guard", None)
        if guard is not None:
            guard.release()

    def _set_execution_progress(self, text: str) -> None:
        self.execution_progress_text = text
        if self.mini_mode == "execution":
            self.mini_count_var.set(text)

    def _reset_execution_clock_for_new_run(self, resume_action_index: int | None) -> None:
        """Start at zero only for a newly requested run, never for internal resume."""
        if resume_action_index is not None:
            return
        self.execution_started_at = time.perf_counter()
        self.mini_elapsed_var.set("00:00")

    def run_script_from_selected_action(self):
        selected = sorted(int(item) for item in self.action_tree.selection())
        if not selected:
            self._notify("从选中行运行", "请先选择一行动作。")
            return
        self.run_current_script(start_index=selected[0])

    def run_current_script(self, start_index: int = 0):
        if self.recorder.running:
            self.stop_recording()
        if self.worker and self.worker.is_alive():
            self._notify("正在运行", "已有脚本或工作流正在执行。")
            return
        trigger = dict(self.script.settings.get("trigger") or {})
        if not self.script.actions and not trigger.get("template"):
            self._notify("没有动作", "请先录制或添加动作。")
            return
        start_index = max(0, min(int(start_index), len(self.script.actions) - 1))
        repeats = max(1, int(self.repeat_var.get()))
        hwnd = self._bound_hwnd()
        activation_enabled, activation_signature = self._activation_settings_from_script()
        try:
            activation_hwnd = self._execution_activation_hwnd(
                hwnd, activation_enabled, activation_signature,
            )
        except RuntimeError as exc:
            self._notify("无法执行", str(exc))
            return
        focus_enabled = bool(self.focus_mode_enabled_var.get())
        activate_target = bool(self.activate_target_enabled_var.get())
        self.execution_focus_requested = focus_enabled
        source_screen = dict(self.script.settings.get("recorded_screen", {})) or None
        self.workflow_stop.clear()
        self._sound("run_start")
        self._hide_main_for_execution()
        self.execution_started_at = time.perf_counter()
        start_note = f"从第 {start_index + 1}/{len(self.script.actions)} 行开始 · " if start_index else ""
        self._set_execution_progress(f"当前脚本 · {start_note}共执行 {repeats} 次 · 正在准备 · F12 停止")
        self.worker = threading.Thread(
            target=self._run_script_worker,
            args=(list(self.script.actions), repeats, hwnd, activation_hwnd,
                  source_screen, focus_enabled, activate_target, start_index),
            kwargs={"trigger": trigger},
            daemon=True,
        )
        self.worker.start()
        self._show_execution_mini()
        if start_index:
            self._append_mini_step(
                f"从第 {start_index + 1}/{len(self.script.actions)} 行开始执行，重复 {repeats} 次。"
            )
        else:
            self._append_mini_step(f"开始执行当前脚本，重复 {repeats} 次。")

    def _run_script_worker(self, actions, repeats, hwnd, activation_hwnd, source_screen,
                           focus_enabled, activate_target, start_index=0, trigger=None):
        self._ui(self._set_status, "正在执行脚本…", "warning")
        if start_index:
            self._ui(self._log, f"从第 {start_index + 1}/{len(actions)} 行开始执行脚本，重复 {repeats} 次。")
        else:
            self._ui(self._log, f"开始执行脚本，重复 {repeats} 次。")
        try:
            self._enter_focus_mode(activation_hwnd or hwnd, focus_enabled)
            self.player.play(
                actions, repeats, hwnd, source_screen=source_screen,
                activate_target=activate_target, activation_hwnd=activation_hwnd,
                start_index=start_index,
                on_repeat=lambda current, total: self._ui(
                    self._set_execution_progress,
                    f"当前脚本 · "
                    f"{'从第 ' + str(start_index + 1) + '/' + str(len(actions)) + ' 行 · ' if start_index else ''}"
                    f"共执行 {total} 次 · 当前第 {current}/{total} 次 · F12 停止",
                ),
            )
            if not self.player.stop_event.is_set():
                if trigger and str(trigger.get("template", "")).strip():
                    # 全局脚本：播放完成后不结束，保持检测运行直到停止；
                    # 触发条件满足时重新执行语句体（脚本内的所有动作）。
                    self.standalone_global_replay = {
                        "actions": list(actions),
                        "hwnd": hwnd,
                        "activation_hwnd": activation_hwnd,
                        "source_screen": source_screen,
                        "activate_target": activate_target,
                    }
                    self._activate_global_detect_from_config(dict(trigger))
                    self._ui(
                        self._set_status,
                        "全局检测运行中 · 触发后执行脚本动作 · F12 停止",
                        "warning",
                    )
                    self._ui(
                        self._append_mini_step,
                        "全局检测已启用，持续检测中：触发后执行脚本动作。",
                    )
                    self._ui(
                        self._log,
                        "全局检测已启用，持续检测中：触发后执行脚本动作，按 F12 停止。",
                    )
                    while not self.player.stop_event.is_set():
                        self.player.stop_event.wait(0.5)
                else:
                    self._ui(self._append_mini_step, "脚本执行完成。")
                    self._ui(self._set_status, "脚本执行完成", "success")
                    self._ui(self._log, "脚本执行完成。")
                    self._ui(self._sound, "run_done")
            elif self.standalone_jump_pending:
                # 普通脚本内嵌全局模块行：触发后跳转播放中，等待其结束再收尾。
                while not self.standalone_jump_done.wait(0.2):
                    pass
                self._ui(self._append_mini_step, "脚本执行完成。")
                self._ui(self._set_status, "脚本执行完成", "success")
                self._ui(self._log, "脚本执行完成。")
                self._ui(self._sound, "run_done")
        except Exception as exc:
            self._ui(self._handle_worker_error, "脚本执行失败", exc)
        finally:
            self.standalone_global_replay = None
            self._leave_focus_mode()
            self._ui(self._finish_execution_visibility)

    def _player_status_callback(self, text: str):
        self._ui(self._set_status, text, "warning")
        self._ui(self._append_mini_step, text)

    def _notify(self, title: str, text: str, duration_ms: int = 4500):
        self._show_execution_notice(f"{title}：{text}", duration_ms)

    def _player_notice_callback(self, text: str, duration_ms: int):
        self._ui(self._show_execution_notice, text, duration_ms)

    def _show_execution_notice(self, text: str, duration_ms: int):
        try:
            keep_main_hidden = self.root.state() == "withdrawn" or self.main_hidden_for_execution
        except (AttributeError, tk.TclError):
            keep_main_hidden = False
        try:
            position = self.floating_notice_position_var.get()
        except (AttributeError, tk.TclError):
            position = "顶部居中"
        existing = self.execution_notice_window
        if existing is not None and existing.winfo_exists():
            self.execution_notice_label.configure(text=text)
            x, y = floating_notice_xy(
                position, existing.winfo_screenwidth(), existing.winfo_screenheight(),
            )
            existing.geometry(f"{FLOATING_NOTICE_WIDTH}x{FLOATING_NOTICE_HEIGHT}+{x}+{y}")
            if self.execution_notice_after_id is not None:
                existing.after_cancel(self.execution_notice_after_id)
            existing.deiconify()
            existing.lift()
            if keep_main_hidden:
                self.root.withdraw()
            self.execution_notice_after_id = existing.after(
                duration_ms, lambda window=existing: self._close_execution_notice(window),
            )
            return
        notice = tk.Toplevel(self.root)
        self.execution_notice_window = notice
        notice.withdraw()
        notice.overrideredirect(True)
        notice.attributes("-topmost", True)
        notice.configure(background="#263541")
        width, height = FLOATING_NOTICE_WIDTH, FLOATING_NOTICE_HEIGHT
        x, y = floating_notice_xy(
            position, notice.winfo_screenwidth(), notice.winfo_screenheight(), width, height,
        )
        notice.geometry(f"{width}x{height}+{x}+{y}")
        frame = ttk.Frame(notice, padding=(12, 10), style="Surface.TFrame")
        frame.pack(fill="both", expand=True)
        self.execution_notice_label = ttk.Label(
            frame, text=text, style="MiniText.TLabel", wraplength=330, justify="left",
        )
        self.execution_notice_label.pack(anchor="w", fill="both", expand=True)
        notice.update_idletasks()
        make_window_no_activate(notice.winfo_id())
        notice.deiconify()
        notice.lift()
        if keep_main_hidden:
            self.root.withdraw()
        self.execution_notice_after_id = notice.after(
            duration_ms, lambda window=notice: self._close_execution_notice(window),
        )

    def _close_execution_notice(self, notice):
        if notice is not self.execution_notice_window:
            return
        self.execution_notice_window = None
        self.execution_notice_label = None
        self.execution_notice_after_id = None
        try:
            notice.destroy()
        except tk.TclError:
            pass

    def _handle_worker_error(self, title: str, exc: Exception):
        self._set_status(str(exc), "error")
        self._log(f"{title}：{exc}")
        self._append_mini_step(f"{title}：{exc}")
        self._log("执行异常收尾：即将关闭执行小窗并恢复主界面。")
        self._stop_all_global_detect_monitors(clear=True)
        self._finish_execution_visibility()
        self._sound("error")
        self._notify(title, str(exc), 6000)

    def stop_all(self, from_ui: bool = False):
        if self.recorder.running:
            self.stop_recording(discard_recent=from_ui, sound=False)
        self.workflow_stop.set()
        self.player.stop()
        self.workflow_restart_requested = False
        self.global_detect_pending_restart = False
        self.global_detect_end_current_script = False
        self.global_detect_advance_workflow_step = False
        self.workflow_repeats_snapshot = None
        self._clear_global_detect_rearm_locks()
        self._stop_all_global_detect_monitors()
        # F12 必须立即解除 BlockInput，不能等待工作流/全局模块线程自然退出。
        self._leave_focus_mode()
        self._finish_execution_visibility()
        self._set_status("已发送停止指令", "warning")
        self._log("用户触发紧急停止。")
        self._sound("emergency_stop")

    # Workflow
    def _global_module_steps(self) -> list[dict]:
        return [step for step in self.workflow.steps if step.get("kind") == "global_module"]

    def _workflow_only_steps(self) -> list[dict]:
        return [step for step in self.workflow.steps if step.get("kind") != "global_module"]

    @staticmethod
    def _workflow_module_key(step: dict) -> str:
        action = step.get("action") if isinstance(step.get("action"), dict) else {}
        return str(action.get("module_key") or action.get("template") or "").strip()

    def _workflow_module_enabled(self, step: dict) -> bool:
        """Whether the module registry currently allows this workflow row."""
        if step.get("kind") != "module":
            return True
        action = step.get("action") if isinstance(step.get("action"), dict) else {}
        if str(action.get("type", "")) in {
            "restart_workflow", "end_current_script", "jump_current_script_last",
        }:
            return True
        module_obj = registered_module_object(self._workflow_module_key(step))
        return bool(module_obj and module_obj.get("enabled", True))

    def _workflow_step_name(self, step: dict) -> str:
        if step.get("kind") != "module":
            return workflow_script_name(step.get("script", "")) or "未设置脚本"
        module_key = self._workflow_module_key(step)
        module_obj = registered_module_object(module_key)
        action = step.get("action") if isinstance(step.get("action"), dict) else {}
        special_type = str(action.get("type", ""))
        if special_type == "restart_workflow":
            return "重新执行工作流"
        if special_type == "end_current_script":
            return END_CURRENT_SCRIPT_LABEL
        if special_type == "jump_current_script_last":
            return "跳转到当前脚本最后一行"
        name = (
            str(action.get("module_name", "")).strip()
            or (str(module_obj.get("name", "")).strip() if module_obj else "")
            or Path(module_key.replace("\\", "/")).stem
        )
        return f"模块 {name or '未设置'}"

    def rebuild_workflow_tree(self):
        self.workflow_tree.delete(*self.workflow_tree.get_children())
        workflow_steps = self._workflow_only_steps()
        script_labels = []
        for index, step in enumerate(workflow_steps):
            is_module = step.get("kind") == "module"
            script_value = step.get("script", "")
            module_enabled = self._workflow_module_enabled(step)
            enabled = bool(step.get("enabled", True)) and module_enabled
            unlimited = bool(step.get("unlimited", False))
            exhausted = not unlimited and int(step.get("repeats", 1)) <= 0
            if is_module:
                module_key = self._workflow_module_key(step)
                module_obj = registered_module_object(module_key)
                missing = module_obj is None
                script_label = f"◆ {self._workflow_step_name(step)}"
            else:
                missing = not resolve_path(script_value).is_file()
                script_label = workflow_script_name(script_value)
            if missing:
                missing_text = "模块不存在" if is_module else "文件不存在"
                script_label = f"⚠ {script_label}  ·  {missing_text}"
            script_labels.append(script_label)
            if unlimited:
                repeat_label = "∞"
            else:
                repeat_label = step.get("repeats", 1)
            if str(step.get("repeat_start_action_id", "")).strip():
                repeat_label = f"{repeat_label} ↻"
            if is_module and not module_enabled:
                status_label = "● 模块已禁用"
            elif not bool(step.get("enabled", True)):
                status_label = "● 已禁用"
            elif unlimited:
                status_label = "✓ 不计次数"
            elif exhausted:
                status_label = "○ 次数用完"
            else:
                status_label = "✓ 启用"
            self.workflow_tree.insert(
                "", "end", iid=str(index),
                values=(
                    index + 1, script_label, repeat_label,
                    f"{step.get('before_ms', 0)} ms",
                    f"{step.get('repeat_interval_ms', DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS)} ms",
                    status_label,
                ),
                tags=("module_disabled",) if is_module and not module_enabled else (
                    ("disabled",) if not bool(step.get("enabled", True)) else (
                        ("unlimited",) if unlimited else (
                            ("exhausted",) if exhausted else (("missing",) if missing else ())
                        )
                    )
                ),
            )
        if workflow_steps:
            self.empty_workflow_hint.place_forget()
        else:
            self.empty_workflow_hint.place(relx=0.5, rely=0.45, anchor="center")
        self._autosize_tree_column(self.workflow_tree, "script", 500, script_labels)
        self.rebuild_global_tree()

    def rebuild_global_tree(self):
        tree = getattr(self, "global_tree", None)
        if tree is None:
            return
        tree.delete(*tree.get_children())
        module_labels = []
        for index, step in enumerate(self._global_module_steps()):
            row_enabled = bool(step.get("enabled", True))
            registry_state = self._workflow_global_module_registry_state(step)
            enabled = row_enabled and registry_state in (None, "enabled")
            module_label = self._global_module_label(step)
            module_labels.append(module_label)
            if registry_state == "missing":
                status_label = "⚠ 模块不存在"
            elif registry_state == "disabled":
                status_label = "● 模块已禁用"
            elif not row_enabled:
                status_label = "● 已禁用"
            else:
                status_label = "✓ 全局"
            tree.insert(
                "", "end", iid=str(index),
                values=(index + 1, module_label, status_label),
                tags=("disabled",) if not enabled else ("global",),
            )
        if self._global_module_steps():
            self.empty_global_hint.place_forget()
        else:
            self.empty_global_hint.place(relx=0.5, rely=0.45, anchor="center")
        self._autosize_tree_column(tree, "module", 700, module_labels)

    @staticmethod
    def _script_trigger_config(script) -> dict:
        """全局脚本的触发条件：优先 settings["trigger"]，旧脚本回退扫描全局检测动作。

        v1.68 起普通脚本可内嵌全局模块行（带 jump_row），它们不是触发条件，
        回退扫描时跳过。
        """
        config = dict(script.settings.get("trigger") or {})
        if not config:
            for action in script.actions:
                if str(action.get("type")) == "global_detect" and "jump_row" not in action:
                    return dict(action)
        return config

    def _global_module_label(self, step: dict) -> str:
        """Full summary for a global module, reading the referenced script when needed."""
        config = dict(step.get("config") or {})
        script_value = step.get("script", "")
        if not config and script_value.strip():
            script_path = resolve_path(script_value)
            if script_path.is_file():
                try:
                    script = load_script(script_path)
                    config = self._script_trigger_config(script)
                except Exception:
                    pass
        if config.get("module_ref") and str(config.get("template", "")).strip():
            module_key = str(config.get("module_key") or config.get("template", "")).strip()
            module_obj = registered_module_object(module_key) or {}
            module_name = str(module_obj.get("name", "")).strip() or Path(
                module_key.replace("\\", "/"),
            ).stem
            script_text = f"◆ 模块对象 · {module_name}"
        elif str(script_value).strip():
            script_name = workflow_script_name(script_value)
            script_text = f"⇄ 引用脚本 · {script_name}" if script_name else "⇄ 引用脚本 · 未配置"
        else:
            script_text = "◈ 全局检测 · 未配置"
        template_name = Path(str(config.get("template", ""))).name
        if not template_name:
            return script_text
        region = config.get("region") or []
        region_mode = str(config.get("region_mode", ""))
        if region_mode == "template":
            region_text = "模板区域"
        elif region_mode == "window":
            region_text = "目标窗口"
        elif region_mode == "custom" or len(region) == 4:
            try:
                region_text = ",".join(str(int(part)) for part in region) if len(region) == 4 else "全屏"
            except (TypeError, ValueError):
                region_text = "全屏"
        else:
            region_text = "全屏"
        try:
            hold = int(config.get("hold_ms", 1000))
        except (TypeError, ValueError):
            hold = 1000
        hold_text = f"持续 {hold} ms" if config.get("hold_enabled", True) else "识别到立即执行"
        return (f"◈ 全局检测 · {script_text} · {template_name} · 区域 {region_text} · "
                f"{hold_text} · 触发后执行模块步骤，再继续工作流")

    def _measure_text_width(self, text: str) -> int:
        try:
            return tkfont.Font(family="Microsoft YaHei UI", size=11).measure(str(text))
        except RuntimeError:
            # No Tk root available (unit tests): estimate CJK vs ASCII widths.
            return sum(14 if ord(ch) > 0x2E7F else 7 for ch in str(text))

    def _autosize_tree_column(self, tree, column: str, min_width: int, texts: list[str]) -> None:
        """Widen a tree column so its longest text stays fully visible."""
        needed = max((self._measure_text_width(text) for text in texts), default=0)
        tree.column(column, width=max(min_width, min(1600, needed + 40)), minwidth=min_width)

    def new_workflow(self):
        self.workflow = Workflow()
        self.workflow_path = None
        self._clear_workflow_delete_history()
        self.workflow_name_var.set(self.workflow.name)
        self.workflow_start_var.set("")
        self.workflow_start_delay_enabled_var.set(False)
        self.workflow_start_delay_seconds_var.unit.set("ms")
        self.workflow_start_delay_seconds_var.set("5000")
        self._toggle_workflow_start_delay_control(persist=False)
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()

    def open_workflow(self):
        path = filedialog.askopenfilename(parent=self.root, initialdir=WORKFLOWS_DIR, title="打开工作流", filetypes=[("MacroFlow 工作流", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            self.workflow = load_workflow(path)
            self.workflow_path = Path(path)
            self._clear_workflow_delete_history()
            self.workflow_name_var.set(self.workflow.name)
            self.workflow_start_var.set(self.workflow.start_at)
            self.workflow_start_delay_enabled_var.set(self.workflow.start_delay_enabled)
            self.workflow_start_delay_seconds_var.unit.set("ms")
            self.workflow_start_delay_seconds_var.set(
                str(int(self.workflow.start_delay_seconds) * 1000)
            )
            self._toggle_workflow_start_delay_control(persist=False)
            self.rebuild_workflow_tree()
            self._persist_workflow_draft()
            self._log(f"打开工作流：{path}")
        except Exception as exc:
            self._notify("打开失败", str(exc))

    def save_current_workflow(self):
        self.workflow.name = self.workflow_name_var.get().strip() or "未命名工作流"
        self.workflow.start_at = self.workflow_start_var.get().strip()
        if self._read_workflow_start_delay(validate=True) is None:
            return None
        target = self.workflow_path
        if target is None or target.stem != self.workflow.name:
            target = None
        try:
            self.workflow_path = save_workflow(self.workflow, target)
            self._persist_workflow_draft()
            self._set_status(f"已保存 {self.workflow_path.name}", "success")
            self._log(f"保存工作流：{self.workflow_path}")
            return self.workflow_path
        except Exception as exc:
            self._notify("保存失败", str(exc))
            return None

    def refresh_workflow_files(self):
        pass

    def add_current_script_step(self):
        path = self.save_current_script()
        if path:
            self._append_workflow_step(path)

    def add_script_step(self):
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=self._script_category_dir(), title="选择脚本",
            filetypes=[("MacroFlow 脚本", "*.json"), ("所有文件", "*.*")],
        )
        if path:
            self._append_workflow_step(Path(path))

    def add_workflow_module_step(self):
        actions = ModulePickerDialog(
            self.root, categories=("switch", "special"), multi_select=True,
            allow_number=False,
        ).show()
        if not actions:
            return
        selected_actions = actions if isinstance(actions, list) else [actions]
        for action in selected_actions:
            self.workflow.steps.append(self._new_workflow_module_step(action))
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()
        index = len(self._workflow_only_steps()) - 1
        if index >= 0:
            self.workflow_tree.selection_set(str(index))
            self.workflow_tree.see(str(index))
        self._set_status(f"已添加 {len(selected_actions)} 个模块到工作流", "success")

    def add_workflow_global_module(self):
        actions = ModulePickerDialog(
            self.root, categories=("workflow_global",), multi_select=True,
        ).show()
        if not actions:
            return
        selected_actions = actions if isinstance(actions, list) else [actions]
        for action in selected_actions:
            self._append_global_module(config=dict(action), refresh=False)
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()
        count = len(selected_actions)
        if count:
            self.global_tree.selection_set(str(len(self._global_module_steps()) - 1))
            self.global_tree.see(str(len(self._global_module_steps()) - 1))
        self._set_status(f"已添加 {count} 个工作流全局模块", "success")

    def _append_global_module(self, config: dict | None = None, script: str = "",
                              refresh: bool = True):
        step = {
            "kind": "global_module",
            "script": script,
            "repeats": 1,
            "before_ms": 0,
            "repeat_interval_ms": DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
            "unlimited": True,
            "enabled": True,
            "config": config,
            "step_id": new_action_id(),
        }
        self.workflow.steps.append(step)
        if refresh:
            self.rebuild_workflow_tree()
            self._persist_workflow_draft()

    def _append_workflow_step(self, path: Path):
        self.workflow.steps.append({
            "script": display_path(path),
            "repeats": 1,
            "before_ms": 0,
            "repeat_interval_ms": DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
            "unlimited": False,
            "enabled": True,
            "step_id": new_action_id(),
        })
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()

    @classmethod
    def _new_workflow_module_step(cls, action: dict) -> dict:
        action = dict(action)
        action.setdefault(ACTION_ID_KEY, new_action_id())
        module_key = str(action.get("module_key") or action.get("template", "")).strip()
        module_obj = registered_module_object(module_key)
        module_name = (
            str(module_obj.get("name", "")).strip()
            if module_obj else Path(module_key.replace("\\", "/")).stem
        )
        action["module_name"] = module_name
        return {
            "kind": "module",
            "action": action,
            "repeats": 1,
            "before_ms": 0,
            "repeat_interval_ms": DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
            "unlimited": False,
            "enabled": True,
            "step_id": new_action_id(),
        }

    def _set_workflow_insert_position(self, above: bool):
        self.workflow_insert_position_var.set("above" if above else "below")
        self.workflow_insert_above_button.configure(
            bootstyle="primary" if above else "secondary",
        )
        self.workflow_insert_below_button.configure(
            bootstyle="primary" if not above else "secondary",
        )

    def insert_workflow_step(self):
        """在选中步骤的上方或下方插入一个脚本步骤（按插入位置设置）。"""
        workflow_steps = self._workflow_only_steps()
        selected = self._selected_workflow_index()
        if selected is None or not workflow_steps:
            self._notify("插入脚本", "请先选择插入位置所在的工作流行。")
            return
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=self._script_category_dir(), title="选择要插入的脚本",
            filetypes=[("MacroFlow 脚本", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        position_var = getattr(self, "workflow_insert_position_var", None)
        position = position_var.get() if position_var is not None else "below"
        index = selected if position == "above" else selected + 1
        self._insert_workflow_step_at(Path(path), index)

    def insert_workflow_module_step(self):
        workflow_steps = self._workflow_only_steps()
        selected = self._selected_workflow_index()
        if selected is None or not workflow_steps:
            self._notify("插入模块", "请先选择插入位置所在的工作流行。")
            return
        action = ModulePickerDialog(
            self.root, categories=("switch", "special"), allow_number=False,
        ).show()
        if not isinstance(action, dict):
            return
        position_var = getattr(self, "workflow_insert_position_var", None)
        position = position_var.get() if position_var is not None else "below"
        index = selected if position == "above" else selected + 1
        step = self._new_workflow_module_step(action)
        self._insert_workflow_task_at(step, index)

    def _insert_workflow_step_at(self, path: Path, index: int):
        step = {
            "script": display_path(path),
            "repeats": 1,
            "before_ms": 0,
            "repeat_interval_ms": DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
            "unlimited": False,
            "enabled": True,
            "step_id": new_action_id(),
        }
        self._insert_workflow_task_at(step, index)

    def _insert_workflow_task_at(self, step: dict, index: int):
        script_steps = self._workflow_only_steps()
        index = min(max(0, index), len(script_steps))
        if index >= len(script_steps):
            self.workflow.steps.append(step)
        else:
            anchor = script_steps[index]
            position = self.workflow.steps.index(anchor)
            self.workflow.steps.insert(position, step)
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()
        self.workflow_tree.selection_set(str(index))
        self.workflow_tree.see(str(index))
        self._update_workflow_selection_color()

    def _selected_workflow_index(self) -> int | None:
        selected = self.workflow_tree.selection()
        return int(selected[0]) if selected else None

    def _selected_workflow_indices(self) -> list[int]:
        return sorted({int(item) for item in self.workflow_tree.selection()})

    def _select_all_workflow_steps(self, _event=None):
        rows = self.workflow_tree.get_children()
        if rows:
            self.workflow_tree.selection_set(*rows)
            self.workflow_tree.focus(rows[0])
            self.workflow_tree.see(rows[0])
        return "break"

    def _update_workflow_selection_color(self, _event=None):
        index = self._selected_workflow_index()
        workflow_steps = self._workflow_only_steps()
        if index is not None and 0 <= index < len(workflow_steps):
            step = workflow_steps[index]
            if not self._workflow_module_enabled(step):
                background, foreground = "#552B35", "#FFB3B3"
            elif not bool(step.get("enabled", True)):
                background, foreground = "#6B4615", "#FFE1A3"
            elif bool(step.get("unlimited", False)):
                background, foreground = "#1F4D30", "#7BC96F"
            else:
                background, foreground = "#244D78", "#FFFFFF"
        else:
            background, foreground = "#244D78", "#FFFFFF"
        self.root.style.map(
            "Workflow.Treeview",
            background=[("selected", background)],
            foreground=[("selected", foreground)],
        )

    def _edit_workflow_cell(self, event):
        row = self.workflow_tree.identify_row(event.y)
        column = self.workflow_tree.identify_column(event.x)
        if not row or column == "#1":
            return
        index = int(row)
        workflow_steps = self._workflow_only_steps()
        if not 0 <= index < len(workflow_steps):
            return
        step = workflow_steps[index]
        if column == "#2":
            if step.get("kind") == "module":
                action = ModulePickerDialog(
                    self.root, categories=("switch", "special"), allow_number=False,
                ).show()
                if not isinstance(action, dict):
                    return
                action = dict(action)
                action.setdefault(ACTION_ID_KEY, new_action_id())
                module_key = str(action.get("module_key") or action.get("template", "")).strip()
                module_obj = registered_module_object(module_key)
                action["module_name"] = (
                    str(module_obj.get("name", "")).strip()
                    if module_obj else Path(module_key.replace("\\", "/")).stem
                )
                step["action"] = action
            else:
                current = resolve_path(step.get("script", ""))
                initial_dir = current.parent if current.parent.is_dir() else self._level_scripts_dir()
                path = filedialog.askopenfilename(
                    parent=self.root, initialdir=initial_dir, title="替换这一行的脚本",
                    filetypes=[("MacroFlow 脚本", "*.json"), ("所有文件", "*.*")],
                )
                if not path:
                    return
                step["script"] = display_path(Path(path))
        elif column == "#3":
            if step.get("kind") == "module":
                repeat_script = MacroScript(
                    name=self._workflow_step_name(step),
                    actions=[dict(step.get("action") or {})],
                )
            else:
                try:
                    repeat_script = load_script(resolve_path(step.get("script", "")))
                except Exception:
                    repeat_script = None
            values = WorkflowRepeatDialog(
                self.root,
                repeats=int(step.get("repeats", 1)),
                unlimited=bool(step.get("unlimited", False)),
                actions=repeat_script.actions if repeat_script else [],
                repeat_start_action_id=str(step.get("repeat_start_action_id", "")),
                script_name=self._workflow_step_name(step),
            ).show()
            if values is None:
                return
            step["repeats"] = values["repeats"]
            step["unlimited"] = values["unlimited"]
            if values.get("repeat_start_action_id"):
                step["repeat_start_action_id"] = values["repeat_start_action_id"]
            else:
                step.pop("repeat_start_action_id", None)
        elif column == "#4":
            value = DurationDialog(
                self.root, "开始前等待", "执行这一行前等待：",
                int(step.get("before_ms", 0)),
            ).show()
            if value is None:
                return
            step["before_ms"] = value
        elif column == "#5":
            value = DurationDialog(
                self.root, "重复间隔", "同一脚本相邻两次执行之间等待：",
                int(step.get("repeat_interval_ms", DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS)),
            ).show()
            if value is None:
                return
            step["repeat_interval_ms"] = value
        elif column == "#6":
            step["enabled"] = not bool(step.get("enabled", True))
        else:
            return
        self.rebuild_workflow_tree()
        self.workflow_tree.selection_set(str(index))
        self._persist_workflow_draft()

    def toggle_selected_workflow_step(self):
        indices = self._selected_workflow_indices()
        if not indices:
            self._notify("未选择任务", "请先单击选择一个工作流任务。")
            return
        workflow_steps = self._workflow_only_steps()
        indices = [index for index in indices if 0 <= index < len(workflow_steps)]
        if not indices:
            return
        enabled = not all(bool(workflow_steps[index].get("enabled", True)) for index in indices)
        for index in indices:
            workflow_steps[index]["enabled"] = enabled
        self.rebuild_workflow_tree()
        rows = tuple(str(index) for index in indices)
        self.workflow_tree.selection_set(*rows)
        self.workflow_tree.see(rows[0])
        self._persist_workflow_draft()
        state = "启用" if enabled else "禁用"
        self._set_status(f"已{state} {len(indices)} 个工作流任务", "success")

    def _consume_workflow_repeat(self, index: int, test_mode: bool | None = None) -> int:
        """扣减一行工作流的重复次数并返回剩余次数（供调用方判定是否继续）。

        返回语义：测试模式 / 不计次数 / 越界返回 0；正常扣减返回剩余次数。
        """
        workflow_steps = self._workflow_only_steps()
        if not 0 <= index < len(workflow_steps):
            return 0
        step = workflow_steps[index]
        if test_mode is None:
            test_mode = bool(getattr(self, "workflow_test_mode_active", False))
        if test_mode:
            self._log(f"工作流第 {index + 1} 行测试完成一次（测试模式，不扣减次数）。")
            return 0
        if bool(step.get("unlimited", False)):
            self._log(f"工作流第 {index + 1} 行完成一次（不计次数，不扣减）。")
            return 0
        remaining = max(0, int(step.get("repeats", 0)) - 1)
        step["repeats"] = remaining
        self.rebuild_workflow_tree()
        self._persist_workflow_draft()
        if self.workflow_path is not None:
            try:
                save_workflow(self.workflow, self.workflow_path)
            except Exception as exc:
                self._log(f"自动保存工作流剩余次数失败：{exc}")
        state_note = "，次数已用完，下次将自动跳过" if remaining == 0 else ""
        self._log(f"工作流第 {index + 1} 行成功完成一次，剩余 {remaining} 次{state_note}。")
        return remaining

    def set_all_workflow_step_options(self):
        workflow_steps = self._workflow_only_steps()
        if not workflow_steps:
            self._notify("没有任务", "请先向工作流添加脚本或模块。")
            return
        first = workflow_steps[0]
        values = WorkflowBatchSettingsDialog(
            self.root,
            repeats=int(first.get("repeats", 1)),
            before_ms=int(first.get("before_ms", 0)),
            repeat_interval_ms=int(first.get("repeat_interval_ms", DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS)),
            unlimited=bool(first.get("unlimited", False)),
        ).show()
        if values is None:
            return
        for step in workflow_steps:
            step.update(values)
        selected = self._selected_workflow_index()
        self.rebuild_workflow_tree()
        if selected is not None:
            self.workflow_tree.selection_set(str(selected))
        self._persist_workflow_draft()
        self._set_status(f"已统一设置 {len(workflow_steps)} 个工作流任务", "success")

    def delete_workflow_step(self):
        indices = self._selected_workflow_indices()
        if not indices:
            return
        workflow_steps = self._workflow_only_steps()
        indices = [index for index in indices if 0 <= index < len(workflow_steps)]
        if not indices:
            return
        history = getattr(self, "workflow_delete_undo_stack", None)
        if history is None:
            history = self.workflow_delete_undo_stack = []
        for index in reversed(indices):
            removed = workflow_steps.pop(index)
            history.append((index, copy.deepcopy(removed)))
        self.workflow.steps = self._global_module_steps() + workflow_steps
        self.rebuild_workflow_tree()
        if workflow_steps:
            target = min(indices[0], len(workflow_steps) - 1)
            self.workflow_tree.selection_set(str(target))
            self.workflow_tree.see(str(target))
            self._update_workflow_selection_color()
        self._update_workflow_delete_undo_buttons()
        self._persist_workflow_draft()
        self._set_status(f"已删除 {len(indices)} 个工作流任务，可逐项撤销", "warning")

    def undo_delete_workflow_step(self):
        history = getattr(self, "workflow_delete_undo_stack", [])
        if not history:
            self._update_workflow_delete_undo_buttons()
            return
        index, removed = history.pop()
        workflow_steps = self._workflow_only_steps()
        target = min(max(0, int(index)), len(workflow_steps))
        workflow_steps.insert(target, copy.deepcopy(removed))
        self.workflow.steps = self._global_module_steps() + workflow_steps
        self.rebuild_workflow_tree()
        self.workflow_tree.selection_set(str(target))
        self.workflow_tree.see(str(target))
        self._update_workflow_selection_color()
        self._update_workflow_delete_undo_buttons()
        self._persist_workflow_draft()
        self._set_status(f"已撤销删除，恢复工作流第 {target + 1} 行", "success")

    def move_workflow_step(self, offset: int):
        index = self._selected_workflow_index()
        if index is None:
            return
        workflow_steps = self._workflow_only_steps()
        target = index + offset
        if not 0 <= target < len(workflow_steps):
            return
        workflow_steps[index], workflow_steps[target] = workflow_steps[target], workflow_steps[index]
        self.workflow.steps = self._global_module_steps() + workflow_steps
        self.rebuild_workflow_tree()
        self.workflow_tree.selection_set(str(target))
        self._persist_workflow_draft()

    def _workflow_drag_start(self, event):
        row = self.workflow_tree.identify_row(event.y)
        self.workflow_drag_index = int(row) if row else None
        self.workflow_was_dragged = False
        if row:
            self.workflow_tree.selection_set(row)

    def _workflow_drag_motion(self, event):
        if self.workflow_drag_index is None:
            return
        row = self.workflow_tree.identify_row(event.y)
        if not row:
            return
        target = int(row)
        source = self.workflow_drag_index
        workflow_steps = self._workflow_only_steps()
        if target == source or not (0 <= target < len(workflow_steps)):
            return
        step = workflow_steps.pop(source)
        workflow_steps.insert(target, step)
        self.workflow.steps = self._global_module_steps() + workflow_steps
        self.workflow_drag_index = target
        self.workflow_was_dragged = True
        self.rebuild_workflow_tree()
        self.workflow_tree.selection_set(str(target))
        self.workflow_tree.see(str(target))

    def _workflow_drag_end(self, event):
        self.workflow_drag_index = None
        self.workflow_was_dragged = False
        self._persist_workflow_draft()

    def _selected_global_index(self) -> int | None:
        selected = self.global_tree.selection()
        return int(selected[0]) if selected else None

    def _selected_global_indices(self) -> list[int]:
        return sorted({int(item) for item in self.global_tree.selection()})

    def _select_all_global_modules(self, _event=None):
        rows = self.global_tree.get_children()
        if rows:
            self.global_tree.selection_set(*rows)
            self.global_tree.focus(rows[0])
            self.global_tree.see(rows[0])
        return "break"

    def _show_global_context_menu(self, event):
        """Right-click a workflow global module to edit its module object."""
        row_id = self.global_tree.identify_row(event.y)
        if not row_id:
            return
        self.global_tree.selection_set(row_id)
        index = int(row_id)
        modules = self._global_module_steps()
        if index >= len(modules):
            return
        step = modules[index]
        menu = tk.Menu(
            self.root, tearoff=False,
            background=COLOR_SURFACE, foreground=COLOR_TEXT,
            activebackground="#1D4358", activeforeground="#FFFFFF",
            borderwidth=1, relief="solid",
        )
        module_key = self._workflow_global_module_key(step)
        if module_key:
            menu.add_command(
                label="✎ 在当前编辑器打开模块",
                command=lambda: self._open_workflow_global_module_in_editor(step),
            )
            menu.add_command(
                label="⇪ 在新窗口编辑模块",
                command=lambda: self._open_workflow_global_module_in_new_window(step),
            )
        else:
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    @staticmethod
    def _workflow_global_module_key(step: dict) -> str:
        config = step.get("config")
        if not isinstance(config, dict) or not config.get("module_ref"):
            return ""
        return str(config.get("module_key") or config.get("template", "")).strip()

    def _workflow_global_module_registry_state(self, step: dict) -> str | None:
        """Return the live registry state of a referenced workflow-global module."""
        module_key = self._workflow_global_module_key(step)
        if not module_key:
            return None
        module_obj = registered_module_object(module_key)
        if module_obj is None:
            return "missing"
        return "enabled" if bool(module_obj.get("enabled", True)) else "disabled"

    def _open_module_object_editor(self, module_key: str, workflow_step: dict | None = None):
        """Open one referenced module object directly in this MacroFlow window."""
        key = str(module_key).strip()
        obj = registered_module_object(key) if key else None
        if not obj:
            self._notify("模块对象不存在", f"模块对象已被移除或路径已改变：{key or '未指定'}")
            return
        if obj.get("category") == "special" or obj.get("pure_action"):
            self._notify("固定特殊模块", "该模块行为固定，无需也不能编辑。")
            return
        result = TemplateRegionFormDialog(
            self.root, key, object_dict=obj,
            category=str(obj.get("category", "workflow_global")),
        ).show()
        if result is None:
            return
        old_key, new_key, updated_obj = result
        update_module_object(new_key, updated_obj, old_key=old_key)
        if workflow_step is not None:
            config = dict(workflow_step.get("config") or {})
            current_key = str(config.get("module_key") or config.get("template", "")).strip()
            if config.get("module_ref") and current_key == key:
                config["module_key"] = new_key
                config["template"] = str(updated_obj.get("template", config.get("template", "")))
                workflow_step["config"] = config
                self.rebuild_workflow_tree()
                self._persist_workflow_draft()
        self._set_status(
            f"已保存全局模块：{updated_obj.get('name') or Path(new_key).stem}", "success",
        )

    def _open_workflow_global_module_in_editor(self, step: dict):
        self._open_module_object_editor(self._workflow_global_module_key(step), workflow_step=step)

    def _open_workflow_global_module_in_new_window(self, step: dict):
        key = self._workflow_global_module_key(step)
        if not key or not registered_module_object(key):
            self._notify("模块对象不存在", f"模块对象已被移除或路径已改变：{key or '未指定'}")
            return
        try:
            args = [sys.executable]
            if not getattr(sys, "frozen", False):
                args.append(str(Path(__file__).resolve()))
            args += ["--edit-module", key]
            spawn_new_instance(args)
        except Exception as exc:
            self._notify("无法新开窗口", str(exc))
            return
        self._log(f"已在新窗口打开全局模块：{key}")
        self._set_status("已在新窗口打开全局模块", "success")

    def edit_selected_global_module(self):
        index = self._selected_global_index()
        modules = self._global_module_steps()
        if index is None or not 0 <= index < len(modules):
            self._notify("未选择全局模块", "请先单击选择一个全局模块。")
            return
        step = modules[index]
        replacement = ModulePickerDialog(
            self.root, categories=("workflow_global",),
        ).show()
        if not replacement:
            return
        step["config"] = dict(replacement)
        step["script"] = ""
        self.rebuild_workflow_tree()
        self.global_tree.selection_set(str(index))
        self._persist_workflow_draft()
        self._set_status("已更换工作流全局模块对象", "success")

    def toggle_selected_global_module(self):
        indices = self._selected_global_indices()
        modules = self._global_module_steps()
        indices = [index for index in indices if 0 <= index < len(modules)]
        if not indices:
            self._notify("未选择全局模块", "请先单击选择一个全局模块。")
            return
        enabled = not all(bool(modules[index].get("enabled", True)) for index in indices)
        for index in indices:
            modules[index]["enabled"] = enabled
        self.rebuild_workflow_tree()
        rows = tuple(str(index) for index in indices)
        self.global_tree.selection_set(*rows)
        self.global_tree.see(rows[0])
        self._persist_workflow_draft()
        state = "启用" if enabled else "禁用"
        self._set_status(f"已{state} {len(indices)} 个全局模块", "success")

    def delete_global_module(self):
        indices = self._selected_global_indices()
        modules = self._global_module_steps()
        indices = [index for index in indices if 0 <= index < len(modules)]
        if not indices:
            self._notify("未选择全局模块", "请先单击选择一个全局模块。")
            return
        history = getattr(self, "global_delete_undo_stack", None)
        if history is None:
            history = self.global_delete_undo_stack = []
        for index in reversed(indices):
            removed = modules.pop(index)
            history.append((index, copy.deepcopy(removed)))
        self.workflow.steps = modules + self._workflow_only_steps()
        self.rebuild_workflow_tree()
        if modules:
            target = min(indices[0], len(modules) - 1)
            self.global_tree.selection_set(str(target))
            self.global_tree.see(str(target))
        self._update_workflow_delete_undo_buttons()
        self._persist_workflow_draft()
        self._set_status(f"已删除 {len(indices)} 个全局模块，可逐项撤销", "warning")

    def undo_delete_global_module(self):
        history = getattr(self, "global_delete_undo_stack", [])
        if not history:
            self._update_workflow_delete_undo_buttons()
            return
        index, removed = history.pop()
        modules = self._global_module_steps()
        target = min(max(0, int(index)), len(modules))
        modules.insert(target, copy.deepcopy(removed))
        self.workflow.steps = modules + self._workflow_only_steps()
        self.rebuild_workflow_tree()
        self.global_tree.selection_set(str(target))
        self.global_tree.see(str(target))
        self._update_workflow_delete_undo_buttons()
        self._persist_workflow_draft()
        self._set_status(f"已撤销删除，恢复全局模块第 {target + 1} 行", "success")

    def _update_workflow_delete_undo_buttons(self):
        workflow_button = getattr(self, "workflow_delete_undo_button", None)
        if workflow_button is not None:
            workflow_button.configure(
                state="normal" if getattr(self, "workflow_delete_undo_stack", []) else "disabled",
            )
        global_button = getattr(self, "global_delete_undo_button", None)
        if global_button is not None:
            global_button.configure(
                state="normal" if getattr(self, "global_delete_undo_stack", []) else "disabled",
            )

    def _clear_workflow_delete_history(self):
        self.workflow_delete_undo_stack = []
        self.global_delete_undo_stack = []
        self._update_workflow_delete_undo_buttons()

    def choose_workflow_start(self):
        selected = ScheduleDialog(self.root, self.workflow_start_var.get()).show()
        if selected is not None:
            self.workflow_start_var.set(selected)
            self._persist_workflow_draft()

    def _toggle_workflow_start_delay_control(self, persist: bool = True):
        entry = getattr(self, "workflow_start_delay_entry", None)
        enabled_var = getattr(self, "workflow_start_delay_enabled_var", None)
        enabled = bool(enabled_var.get()) if enabled_var is not None else False
        if entry is not None:
            entry.configure(state="normal" if enabled else "disabled")
        if persist:
            self._persist_workflow_draft()

    def _read_workflow_start_delay(self, *, validate: bool) -> int | None:
        enabled_var = getattr(self, "workflow_start_delay_enabled_var", None)
        seconds_var = getattr(self, "workflow_start_delay_seconds_var", None)
        enabled = bool(enabled_var.get()) if enabled_var is not None else False
        raw = seconds_var.get().strip() if seconds_var is not None else "5000"
        try:
            milliseconds = int(raw)
            if milliseconds < 0 or milliseconds > 86400000:
                raise ValueError
            seconds = round(milliseconds / 1000)
        except (TypeError, ValueError):
            if validate:
                self._notify("启动延时无效", "启动延时请输入 0–86400000 ms（1440 分钟）以内的时间。")
                return None
            workflow = getattr(self, "workflow", None)
            seconds = int(getattr(workflow, "start_delay_seconds", 5))
        workflow = getattr(self, "workflow", None)
        if workflow is not None:
            workflow.start_delay_enabled = enabled
            workflow.start_delay_seconds = seconds
        return seconds if enabled else 0

    def run_workflow_from_selected(self):
        index = self._selected_workflow_index()
        if index is None:
            self._notify("未选择任务", "请先单击选择要开始执行的工作流任务。")
            return
        self.run_workflow(start_index=index)

    def run_workflow(self, start_index: int = 0, start_repeat: int = 0,
                     resume_action_index: int | None = None,
                     preserve_global_rearm_locks: bool = False,
                     test_mode: bool | None = None):
        if self.worker and self.worker.is_alive():
            self._notify("正在运行", "已有脚本或工作流正在执行。")
            return
        workflow_steps = self._workflow_only_steps()
        global_modules = [dict(step) for step in self._global_module_steps()]
        if not workflow_steps and not global_modules:
            self._notify("没有步骤", "请先向工作流添加脚本或模块。")
            return
        start_index = max(0, min(int(start_index), max(0, len(workflow_steps) - 1)))
        if resume_action_index is None:
            if test_mode is None:
                test_mode_var = getattr(self, "workflow_test_mode_var", None)
                test_mode = bool(test_mode_var.get()) if test_mode_var is not None else False
            self.workflow_test_mode_active = bool(test_mode)
            start_delay_seconds = self._read_workflow_start_delay(validate=True)
            if start_delay_seconds is None:
                return
        else:
            # 全局模块断点恢复与“重新执行工作流”属于同一次运行，不重复等待。
            start_delay_seconds = 0
        self._workflow_snapshot()
        self._persist_workflow_draft()
        self.rebuild_workflow_tree()
        missing_rows = [
            index + 1 for index, step in enumerate(workflow_steps)
            if index >= start_index
            if bool(step.get("enabled", True))
            if step.get("kind") != "module"
            if not resolve_path(step.get("script", "")).is_file()
        ]
        if missing_rows:
            row_text = "、".join(str(row) for row in missing_rows)
            self._set_status(f"第 {row_text} 行脚本不存在，执行时将跳过", "error")
            self._log(f"工作流缺失脚本：第 {row_text} 行；这些行将在执行时跳过。")
        start_text = self.workflow_start_var.get().strip()
        start_at = None
        if start_text:
            try:
                start_at = datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                self._notify("时间格式错误", "请使用格式：2026-08-03 23:30:00")
                return
        hwnd = self._bound_hwnd()
        # 当前侧栏选择作为整个工作流的默认前置窗口；步骤脚本若保存了自己的
        # 前置窗口，则在 _run_workflow_worker 中覆盖这个默认值。
        workflow_activation_enabled, workflow_activation_signature = self._activation_settings_from_script()
        try:
            workflow_activation_hwnd = self._execution_activation_hwnd(
                hwnd, workflow_activation_enabled, workflow_activation_signature,
            )
        except RuntimeError as exc:
            workflow_activation_hwnd = None
            self._log(f"工作流前置窗口已跳过：{exc}；继续执行工作流。")
        # 全局模块中断后的断点恢复仍属于同一次工作流，不能再次执行前置窗口。
        if resume_action_index is not None:
            workflow_activation_hwnd = None
        focus_enabled = bool(self.focus_mode_enabled_var.get())
        activate_target = bool(self.activate_target_enabled_var.get())
        # 特殊模块「重新执行工作流」：入口快照各步剩余次数，防止重启时跳过已消费的行。
        if self.workflow_repeats_snapshot is None:
            self.workflow_repeats_snapshot = {
                index: (int(step.get("repeats", 1)), bool(step.get("unlimited", False)))
                for index, step in enumerate(workflow_steps)
            }
        self.execution_focus_requested = focus_enabled
        # 每次开始/断点恢复前先原子地淘汰上一轮监控。否则旧监控可能在新监控
        # 逐个注册期间再次触发，随后被替换并因 stop 标志跳过恢复，工作流就会停住。
        self._stop_all_global_detect_monitors(clear=True)
        if not preserve_global_rearm_locks:
            self._clear_global_detect_rearm_locks()
        self.workflow_stop.clear()
        self._sound("run_start")
        self._hide_main_for_execution()
        # 只在用户/启动项真正开始一次工作流时归零。全局模块触发后的
        # 断点恢复属于同一次运行，必须沿用原始开始时间。
        self._reset_execution_clock_for_new_run(resume_action_index)
        steps = [dict(step) for step in workflow_steps]
        if start_index:
            initial_progress = f"工作流从第 {start_index + 1}/{len(steps)} 行开始 · 等待开始 · F12 停止"
        else:
            initial_progress = f"工作流 0/{len(steps)} · 等待开始 · F12 停止"
        self._set_execution_progress(initial_progress)
        self.worker = threading.Thread(
            target=self._run_workflow_worker,
            args=(steps, start_at, hwnd, focus_enabled,
                  activate_target, start_index, global_modules,
                  start_repeat, resume_action_index, workflow_activation_hwnd,
                  self.workflow_test_mode_active, start_delay_seconds),
            daemon=True,
        )
        self.worker.start()
        self._show_execution_mini()
        if self.workflow_test_mode_active:
            self._append_mini_step("工作流测试模式：普通计次行最多执行 1 次，且不扣减剩余次数。")
            self._log("工作流测试模式已启用：普通计次行最多执行 1 次；不计次数行保持原样。")
        if start_index:
            self._append_mini_step(f"从第 {start_index + 1}/{len(steps)} 行开始执行工作流。")
        else:
            self._append_mini_step(f"开始执行工作流，共 {len(steps)} 个步骤。")

    def _run_workflow_worker(self, steps, start_at, hwnd,
                             focus_enabled=False, activate_target=True,
                             start_index=0, global_modules=None, start_repeat=0,
                             resume_action_index=None, workflow_activation_hwnd=None,
                             test_mode=False, start_delay_seconds=0):
        try:
            if start_at and start_at > datetime.now():
                seconds = (start_at - datetime.now()).total_seconds()
                self._ui(self._set_status, f"工作流已排期：{start_at:%m-%d %H:%M:%S}", "warning")
                self._ui(self._log, f"工作流等待至 {start_at:%Y-%m-%d %H:%M:%S} 开始。")
                if self.workflow_stop.wait(seconds):
                    return
            if start_delay_seconds > 0:
                delay_text = f"工作流启动延时 {start_delay_seconds} 秒，等待结束后执行。"
                self._ui(self._set_status, delay_text, "warning")
                self._ui(self._set_execution_progress, f"{delay_text} · F12 停止")
                self._ui(self._append_mini_step, delay_text)
                self._ui(self._log, delay_text)
                if self.workflow_stop.wait(start_delay_seconds):
                    return
                self._ui(self._log, "工作流启动延时结束，开始执行。")
            if resume_action_index is None:
                # 专注模式（切换英语输入法 + 系统输入锁）只在工作流首次开始时
                # 执行一次。全局模块中断后的断点恢复、特殊模块“重新执行工作流”
                # 都沿用第一次建立的输入锁，不再重复切换输入法或重新锁定，
                # 避免中途重置输入状态。
                self._enter_focus_mode(hwnd, focus_enabled)
            else:
                self._ui(
                    self._log,
                    "继续执行：沿用已开启的强制专注模式，不再重复设置输入法/输入锁。",
                )
            start_index = max(0, min(int(start_index), max(0, len(steps) - 1)))
            self.current_workflow_step_index = start_index if steps else None
            self.current_workflow_repeat_index = 0
            self.current_workflow_action_index = 0
            if start_index:
                self._ui(self._log, f"从第 {start_index + 1}/{len(steps)} 行开始执行工作流。")
            else:
                self._ui(self._log, f"开始执行工作流，共 {len(steps)} 个步骤。")
            counted_steps = [
                step for step in steps
                if bool(step.get("enabled", True)) and not bool(step.get("unlimited", False))
                and self._workflow_module_enabled(step)
            ]
            if not test_mode and counted_steps \
                    and all(int(step.get("repeats", 1)) <= 0 for step in counted_steps):
                # 所有计次脚本都已执行完毕：整个工作流结束，不再循环执行不计次数脚本。
                self._stop_all_global_detect_monitors()
                self._ui(self._set_status, "工作流执行完成", "success")
                self._ui(self._append_mini_step, "所有计次脚本已执行完毕，工作流结束。")
                self._ui(self._log, "所有计次脚本已执行完毕，工作流结束。")
                self._ui(self._sound, "run_done")
                return
            for module in (global_modules or []):
                if self.workflow_stop.is_set():
                    return
                if not bool(module.get("enabled", True)):
                    continue
                registry_state = self._workflow_global_module_registry_state(module)
                if registry_state in {"disabled", "missing"}:
                    module_label = self._global_module_label(module)
                    reason = "模块管理中已禁用" if registry_state == "disabled" else "模块对象不存在"
                    message = f"— 跳过工作流全局模块：{module_label}，{reason}"
                    self._ui(self._append_mini_step, message)
                    self._ui(self._log, message)
                    continue
                before = max(0, int(module.get("before_ms", 0)))
                if before and self.workflow_stop.wait(before / 1000):
                    return
                config = dict(module.get("config") or {})
                script_value = str(module.get("script", "")).strip()
                if not config and script_value:
                    script_path = resolve_path(script_value)
                    if script_path.is_file():
                        try:
                            script = load_script(script_path)
                            config = self._script_trigger_config(script)
                        except Exception:
                            pass
                if config:
                    self._ui(self._activate_global_detect_from_config, config, module)
                    self._ui(self._append_mini_step, "全局检测模块已启用。")
                    self._ui(self._log, "全局检测模块已启用，触发后执行模块步骤并继续工作流。")
                else:
                    self._ui(
                        self._log,
                        f"全局模块 {workflow_script_name(script_value) or '未配置'}："
                        "未找到全局检测配置，未启用检测。",
                    )
            # “执行前置窗口”是整个工作流的一次性准备动作，只交给第一个实际
            # 执行的脚本。后续脚本以及全局模块断点恢复都直接以目标窗口为准。
            pending_activation_hwnd = workflow_activation_hwnd
            activation_consumed = False
            for index in range(start_index, len(steps)):
                step = steps[index]
                if self.workflow_stop.is_set():
                    return
                script_number = index + 1
                self.current_workflow_step_index = index
                self.current_workflow_repeat_index = 0
                self.current_workflow_action_index = 0
                is_module = step.get("kind") == "module"
                if not bool(step.get("enabled", True)):
                    disabled_name = self._workflow_step_name(step)
                    message = f"— 跳过工作流第 {script_number}/{len(steps)} 行：{disabled_name}，该任务已禁用"
                    self._ui(self._set_execution_progress, message)
                    self._ui(self._append_mini_step, message)
                    self._ui(self._log, message)
                    continue
                if is_module and not self._workflow_module_enabled(step):
                    disabled_name = self._workflow_step_name(step)
                    message = (
                        f"— 跳过工作流第 {script_number}/{len(steps)} 行：{disabled_name}，"
                        "模块管理中已禁用"
                    )
                    self._ui(self._set_execution_progress, message)
                    self._ui(self._append_mini_step, message)
                    self._ui(self._log, message)
                    continue
                unlimited = bool(step.get("unlimited", False))
                planned_repeats = int(step.get("repeats", 1))
                if not unlimited and planned_repeats <= 0:
                    exhausted_name = self._workflow_step_name(step)
                    message = f"— 跳过工作流第 {script_number}/{len(steps)} 行：{exhausted_name}，执行次数已用完"
                    self._ui(self._set_execution_progress, message)
                    self._ui(self._append_mini_step, message)
                    self._ui(self._log, message)
                    continue
                if is_module:
                    action = dict(step.get("action") or {})
                    module_key = self._workflow_module_key(step)
                    if module_key and not action.get("module_key"):
                        action["module_key"] = module_key
                    if module_key and not action.get("template"):
                        action["template"] = module_key
                    if registered_module_object(module_key) is None:
                        message = (
                            f"⚠ 跳过工作流第 {script_number}/{len(steps)} 行："
                            f"{self._workflow_step_name(step)}，模块不存在"
                        )
                        self._ui(self._set_execution_progress, message)
                        self._ui(self._append_mini_step, message)
                        self._ui(self._log, message)
                        continue
                    script = MacroScript(
                        name=self._workflow_step_name(step), actions=[action],
                    )
                else:
                    script_path = resolve_path(step.get("script", ""))
                    if not script_path.is_file():
                        missing_name = workflow_script_name(step.get("script", ""))
                        message = f"⚠ 跳过工作流第 {script_number}/{len(steps)} 行：{missing_name}，文件不存在"
                        self._ui(self._set_execution_progress, message)
                        self._ui(self._append_mini_step, message)
                        self._ui(self._log, message)
                        continue
                    script = load_script(script_path)
                before = max(0, int(step.get("before_ms", 0)))
                self._ui(self._set_status, f"工作流步骤 {index + 1}/{len(steps)}", "warning")
                if before and self.workflow_stop.wait(before / 1000):
                    return
                repeats = 1 if unlimited else (min(planned_repeats, 1) if test_mode else planned_repeats)
                repeat_desc = (
                    "不计次数，每次到达执行 1 次"
                    if unlimited else
                    f"测试执行 {repeats} 次（不扣减）" if test_mode else
                    f"执行 {repeats} 次"
                )
                repeat_interval = max(0, int(step.get(
                    "repeat_interval_ms", DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
                )))
                repeat_start_action_id = str(
                    step.get("repeat_start_action_id", ""),
                ).strip() or None
                if repeat_start_action_id:
                    repeat_start_row = next(
                        (
                            i + 1 for i, action in enumerate(script.actions)
                            if str(action.get("action_id", "")).strip()
                            == repeat_start_action_id
                        ),
                        None,
                    )
                    if repeat_start_row:
                        repeat_desc += f"，第 2 次起从第 {repeat_start_row} 行开始"
                self._ui(
                    self._set_execution_progress,
                    workflow_execution_progress(
                        script_number, len(steps), script.name, repeats, unlimited=unlimited,
                    ),
                )
                self._ui(
                    self._log,
                    f"步骤 {index + 1}：{script.name}，{repeat_desc}，重复间隔 {repeat_interval} ms。",
                )
                step_activation = pending_activation_hwnd
                if not is_module and not activation_consumed and step_activation is None \
                        and workflow_activation_hwnd is None \
                        and bool(script.settings.get("activation_window_enabled", False)):
                    # 当前编辑器没有提供工作流前置配置时，首个实际执行脚本可
                    # 提供一次性前置窗口；执行后同样不再重复。
                    try:
                        step_activation = self._execution_activation_hwnd(
                            hwnd, True, script.settings.get("activation_window"),
                        )
                    except RuntimeError as exc:
                        message = (
                            f"⚠ 工作流第 {script_number}/{len(steps)} 行：{exc}"
                            "已跳过前置窗口条件，继续执行脚本。"
                        )
                        self._ui(self._set_execution_progress, message)
                        self._ui(self._append_mini_step, message)
                        self._ui(self._log, message)
                        step_activation = None
                self.player.play(
                    script.actions, repeats, hwnd,
                    repeat_interval_ms=repeat_interval,
                    source_screen=dict(script.settings.get("recorded_screen", {})) or None,
                    activate_target=activate_target, activation_hwnd=step_activation,
                    start_repeat=start_repeat if index == start_index else 0,
                    resume_action_index=(
                        resume_action_index if index == start_index else None
                    ),
                    repeat_start_action_id=repeat_start_action_id,
                    workflow_context=True,
                    on_action=lambda next_index, _total: self._record_workflow_action(
                        next_index,
                    ),
                    on_repeat=lambda current, total, number=script_number, name=script.name: self._record_workflow_repeat(
                        current, total, number, len(steps), name,
                    ),
                    on_repeat_complete=lambda _current, _total, row=index, testing=test_mode: self._ui(
                        self._consume_workflow_repeat, row, testing,
                    ),
                )
                if step_activation is not None:
                    activation_consumed = True
                pending_activation_hwnd = None
                if self.workflow_stop.is_set() or self.player.stop_event.is_set():
                    return
            self._ui(self._set_status, "工作流执行完成", "success")
            self._ui(self._append_mini_step, "工作流执行完成。")
            self._ui(self._log, "工作流执行完成。")
            self._ui(self._sound, "run_done")
        except Exception as exc:
            self._ui(self._handle_worker_error, "工作流执行失败", exc)
        finally:
            self.current_workflow_step_index = None
            self.current_workflow_repeat_index = 0
            self.current_workflow_action_index = 0
            # 全局检测只是暂时中断当前脚本。输入锁及其 SendInput 转发线程必须
            # 跨越“中断 -> 模块动作 -> 断点恢复”保持存活，否则模块点击与旧
            # worker 的收尾并发时，请求会被关闭并报“强制专注模式已停止”。
            if not getattr(self, "global_detect_pending_restart", False) \
                    and not getattr(self, "workflow_restart_requested", False):
                self._leave_focus_mode()
            # 全局模块触发只是临时暂停：保持执行界面隐藏，模块完成后会从断点
            # 启动新 worker。普通完成、报错或 F12 停止才恢复主界面。
            if not getattr(self, "global_detect_pending_restart", False):
                self._ui(self._finish_execution_visibility)
            # 工作流重启中：保留快照给 _launch_workflow_restart 恢复；否则释放。
            if not getattr(self, "workflow_restart_requested", False):
                self.workflow_repeats_snapshot = None
            if not getattr(self, "global_detect_pending_restart", False) \
                    and not getattr(self, "workflow_restart_requested", False):
                self.workflow_test_mode_active = False

    # Hotkeys and lifecycle
    def _ensure_startup_visible(self):
        if self.exiting or self.main_hidden_to_tray or self.main_hidden_for_recording \
                or self.main_hidden_for_execution or self.main_hidden_for_cursor_tracking:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.update_idletasks()
        hwnd = int(self.root.winfo_id())
        show_window(hwnd)
        activate_window(hwnd)

    def _start_hotkeys(self):
        def on_press(key):
            if self.input_guard.active:
                if key == keyboard.Key.f12:
                    self._ui(self.stop_all)
                return
            if key == keyboard.Key.f8:
                self._ui(self.toggle_record, False)
            elif key == keyboard.Key.f9:
                self._ui(self.run_current_script)
            elif key == keyboard.Key.f12:
                self._ui(self.stop_all)
        self.hotkey_listener = keyboard.Listener(on_press=on_press)
        self.hotkey_listener.start()

    def on_close(self):
        self._persist_workflow_draft()
        if self.close_action_var.get() == "tray":
            self._hide_main_to_tray()
            return
        self._quit_app()

    def _quit_app(self):
        if self.exiting:
            return
        self.exiting = True
        self._persist_workflow_draft()
        if self.backup_after_id is not None:
            try:
                self.root.after_cancel(self.backup_after_id)
            except tk.TclError:
                pass
            self.backup_after_id = None
        self.workflow_stop.set()
        self.player.stop()
        self._stop_all_global_detect_monitors()
        if self.cursor_tracking:
            self._stop_cursor_tracking()
        self.input_guard.stop()
        if self.recorder.running:
            self.recorder.stop()
        self._hide_recording_mini()
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        self._stop_tray()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    try:
        MacroFlowApp().run()
    except Exception:
        error = traceback.format_exc()
        try:
            Path(BASE_DIR / "crash.log").write_text(error, encoding="utf-8")
            root = tk.Tk()
            root.withdraw()
            show_floating_notice(
                root,
                "MacroFlow 启动失败",
                f"错误信息已保存到 crash.log\n{error[-900:]}",
                6000,
            )
            root.after(6200, root.destroy)
            root.mainloop()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
