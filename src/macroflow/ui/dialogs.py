from __future__ import annotations

import ctypes
import json
import tkinter as tk
import copy
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageEnhance, ImageTk
from pypinyin import lazy_pinyin
from ttkbootstrap import DateEntry

from macroflow.core.image_match import capture_bgr
from macroflow.input.input_guard import KeyCapturer, RESERVED_HOTKEY_VKS
from macroflow.core.models import (
    ACTION_ID_KEY, END_CURRENT_SCRIPT_LABEL, NEXT_WORKFLOW_STEP_TARGET_ID,
    SCRIPT_START_TARGET_ID, ensure_action_ids, special_action_label,
)
from macroflow.execution.player import running_process_names
from macroflow.core.storage import (
    BASE_DIR, DIRECTION_SCRIPTS_DIR, IMAGES_DIR, SCRIPTS_DIR, display_path,
    load_app_settings, load_module_images_dir, load_module_objects,
    load_script, load_template_regions,
    module_image_inventory, module_objects_by_category,
    registered_module_object, resolve_path, save_module_images_dir, save_module_objects,
    save_template_regions, save_script, update_module_object,
)
from macroflow.input.wininput import (
    WindowInfo, enum_windows, get_cursor_pos, get_virtual_screen_rect,
    is_current_process_window, make_window_no_activate, set_dark_titlebar,
    show_window_no_activate, window_from_point,
)


COLOR_BG = "#0E1419"
COLOR_SURFACE = "#182129"
COLOR_TEXT = "#E8EDF2"
COLOR_MUTED = "#94A1AD"
COLOR_BLUE_SELECTION = "#244D78"

GLOBAL_SCRIPT_END_LABEL = "脚本结束（工作流中执行下一项）"
SCRIPT_START_LABEL = "脚本开头（从第 1 行开始）"
SCRIPT_END_LABEL = "脚本结尾（立即结束当前脚本）"
SCRIPT_CATEGORY_LABELS = {
    "all": "全部", "level": "关卡", "level_pack": "关卡封装",
    "switch": "切换",
}

TIME_UNITS = ("ms", "s", "min")

_UNIT_TO_MS = {"ms": 1, "s": 1000, "min": 60000}


class DurationVar(tk.StringVar):
    """Display ms/seconds/minutes, while Python callers always receive milliseconds."""

    def __init__(self, value=0, master=None):
        super().__init__(master=master, value=str(value))
        self.unit = tk.StringVar(master=master, value="ms")
        self._last_unit = "ms"
        self.unit.trace_add("write", self._unit_changed)

    def _raw(self) -> str:
        return str(self._tk.globalgetvar(self._name))

    def _unit_changed(self, *_args):
        new_unit = str(self.unit.get())
        if new_unit == self._last_unit:
            return
        try:
            value = float(self._raw())
            converted = value * _UNIT_TO_MS[self._last_unit] / _UNIT_TO_MS[new_unit]
            super().set(str(int(converted)) if converted.is_integer() else f"{converted:g}")
        except ValueError:
            pass
        self._last_unit = new_unit

    def get(self) -> str:
        value = float(self._raw())
        milliseconds = round(value * _UNIT_TO_MS[self.unit.get()])
        return str(milliseconds)


def duration_var(value=0) -> DurationVar:
    return DurationVar(value)


def pinyin_sort_key(value: str) -> tuple[str, str]:
    """Stable Chinese-name sort key using pypinyin, then original text."""
    text = str(value).strip()
    return ("".join(lazy_pinyin(text)).casefold(), text.casefold())


def module_reference_binding(key: str, obj: dict | None = None) -> dict:
    """Return the stable image/module binding carried by inserted actions."""
    obj = obj if obj is not None else (registered_module_object(key) or {})
    raw_region = obj.get("region", [])
    region = []
    if isinstance(raw_region, (list, tuple)) and len(raw_region) == 4:
        try:
            parts = [int(part) for part in raw_region]
        except (TypeError, ValueError):
            parts = []
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            region = parts
    return {
        "template": str(obj.get("template") or key),
        "module_key": key,
        "module_ref": True,
        "module_category": str(obj.get("category") or "switch"),
        "region_mode": "template",
        "region": region,
    }


def action_with_live_module_binding(action: dict | None) -> dict:
    """Refresh editable action fields from its current module object."""
    updated = dict(action or {})
    if not updated.get("module_ref"):
        return updated
    key = str(updated.get("module_key", "")).strip()
    obj = registered_module_object(key) if key else None
    if obj is None:
        return updated
    updated.update(module_reference_binding(key, obj))
    return updated


def choose_module_binding(parent, categories: tuple[str, ...]) -> dict | None:
    """Open the shared module picker and return only its image/region binding."""
    result = ModulePickerDialog(
        parent, categories=categories, selection_only=True, allow_number=False,
    ).show()
    return result if isinstance(result, dict) and result.get("module_ref") else None


def module_action_for_key(key: str, category: str, obj: dict | None = None) -> dict:
    """Build the live-reference action stored when a module is inserted."""
    if category == "special":
        return {
            "type": "end_current_script"
            if key == END_CURRENT_SCRIPT_LABEL
            else "restart_workflow"
        }
    obj = obj if obj is not None else (registered_module_object(key) or {})
    binding = module_reference_binding(key, obj)
    if category in ("workflow_global", "script_global", "global"):
        return {
            "type": "global_detect", **binding,
            "module_category": (
                "workflow_global" if category == "global" else category
            ), "delay_ms": 0,
        }
    action = {
        "type": "image_match", **binding,
        "module_category": "switch", "delay_ms": 0,
        "on_found": "continue", "on_timeout": "continue",
    }
    if obj.get("recognize") == "number":
        # 数字模块插入脚本时由行编辑框补比较值；相等默认跳转，失败默认继续下一行。
        action["on_found"] = "jump"
    return action


def _valid_scripts_in(root: Path) -> list[Path]:
    """Return valid JSON scripts directly under a directory (recursively)."""
    if not root.is_dir():
        return []
    paths: dict[str, Path] = {}
    for path in root.rglob("*.json"):
        resolved = path.resolve()
        try:
            load_script(resolved)
        except Exception:
            continue
        paths[str(resolved).casefold()] = resolved
    return sorted(paths.values(), key=lambda path: pinyin_sort_key(path.stem))


def direction_script_files() -> list[Path]:
    """Return valid JSON scripts from the hotkey direction folder (scripts/方向)."""
    return _valid_scripts_in(resolve_path(DIRECTION_SCRIPTS_DIR))


def configured_script_files(settings: dict | None = None) -> list[Path]:
    """Return valid JSON scripts from every configured script directory."""
    settings = settings or load_app_settings()
    roots = []
    for setting_key, default in (
        ("level_scripts_dir", "scripts/关卡"),
        ("level_pack_scripts_dir", "scripts/关卡封装"),
        ("switch_scripts_dir", "scripts/切换"),
        ("direction_scripts_dir", DIRECTION_SCRIPTS_DIR),
    ):
        roots.append(resolve_path(str(settings.get(setting_key, default))))
    paths: dict[str, Path] = {}
    for root in roots:
        for path in _valid_scripts_in(root):
            paths[str(path.resolve()).casefold()] = path
    return sorted(paths.values(), key=lambda path: pinyin_sort_key(path.stem))


def script_category_for_path(path: str | Path, settings: dict | None = None) -> str:
    """Resolve a script's saved category, with configured-directory fallback."""
    path = Path(path).resolve()
    settings = settings or load_app_settings()
    try:
        script = load_script(path)
    except Exception:
        return "level"
    saved = str(script.settings.get("category", "")).strip()
    if saved in ("level", "level_pack", "switch", "direction"):
        return saved
    for category, setting_key, default in (
        ("level_pack", "level_pack_scripts_dir", "scripts/关卡封装"),
        ("switch", "switch_scripts_dir", "scripts/切换"),
        ("direction", "direction_scripts_dir", DIRECTION_SCRIPTS_DIR),
        ("level", "level_scripts_dir", "scripts/关卡"),
    ):
        root = resolve_path(str(settings.get(setting_key, default))).resolve()
        if path == root or root in path.parents:
            return category
    return "level"


def prepend_module_to_scripts(key: str, category: str,
                              script_paths: list[str | Path]) -> tuple[int, list[Path], list[tuple[Path, str]]]:
    """Insert one module at row 1 of selected scripts and report added/skipped/errors."""
    added = 0
    skipped: list[Path] = []
    errors: list[tuple[Path, str]] = []
    module_obj = registered_module_object(key)
    if module_obj is not None and module_obj.get("recognize") == "number":
        return 0, [], [
            (Path(raw_path), "读取数字需要为每个脚本行设置比较值和两路跳转，不能批量加入")
            for raw_path in script_paths
        ]
    if module_obj is not None and not module_obj.get("enabled", True):
        return 0, [], [
            (Path(raw_path), "模块已禁用，不能插入到脚本")
            for raw_path in script_paths
        ]
    for raw_path in script_paths:
        path = Path(raw_path)
        try:
            script = load_script(path)
            if category in ("workflow_global", "script_global", "global") and (
                    script.is_global or str(script.settings.get("category", "")) in (
                        "global", "workflow_global", "script_global",
                    )
                    or bool(script.settings.get("trigger"))):
                skipped.append(path)
                continue
            ensure_action_ids(script.actions)
            action = module_action_for_key(key, category)
            if category in ("script_global", "global"):
                action["jump_row"] = 2
                if script.actions:
                    action["jump_action_id"] = str(
                        script.actions[0].get(ACTION_ID_KEY, "")
                    ).strip()
            script.actions.insert(0, action)
            ensure_action_ids(script.actions)
            save_script(script, path)
            added += 1
        except Exception as exc:
            errors.append((path, str(exc)))
    return added, skipped, errors


def remove_module_from_scripts(key: str,
                               script_paths: list[str | Path]) -> tuple[int, list[Path], list[tuple[Path, str]]]:
    """Remove every action row referencing one module from selected scripts.

    Return (scripts_changed, scripts_untouched, errors). Untouched scripts
    either never used the module or (for nested references) only reference it
    inside another module's code segment; only top-level rows are removed.
    """
    removed = 0
    untouched: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for raw_path in script_paths:
        path = Path(raw_path)
        try:
            script = load_script(path)
            before = len(script.actions)
            script.actions = [
                action for action in script.actions
                if str(action.get("module_key", "")).strip() != key
            ]
            if len(script.actions) == before:
                untouched.append(path)
                continue
            ensure_action_ids(script.actions)
            save_script(script, path)
            removed += 1
        except Exception as exc:
            errors.append((path, str(exc)))
    return removed, untouched, errors


def find_module_references(key: str,
                           script_paths: list[str | Path]) -> list[dict]:
    """Return every top-level script action that references ``key`` in order."""
    references: list[dict] = []
    for raw_path in script_paths:
        path = Path(raw_path)
        try:
            script = load_script(path)
        except Exception:
            continue
        for index, action in enumerate(script.actions):
            if (action.get("module_ref")
                    and str(action.get("module_key", "")).strip() == key):
                references.append({"path": path, "index": index, "action": action})
    return references


def remove_module_references(references: list[dict]) -> tuple[int, list[Path], list[tuple[Path, str]]]:
    """Remove only the supplied reference locations, grouped by script."""
    grouped: dict[Path, list[int]] = {}
    for reference in references:
        grouped.setdefault(Path(reference["path"]), []).append(int(reference["index"]))
    changed = 0
    untouched: list[Path] = []
    errors: list[tuple[Path, str]] = []
    for path, indices in grouped.items():
        try:
            script = load_script(path)
            valid = {index for index in indices if 0 <= index < len(script.actions)}
            if not valid:
                untouched.append(path)
                continue
            script.actions = [
                action for index, action in enumerate(script.actions)
                if index not in valid
            ]
            ensure_action_ids(script.actions)
            save_script(script, path)
            changed += len(valid)
        except Exception as exc:
            errors.append((path, str(exc)))
    return changed, untouched, errors


VK_NAMES = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "SHIFT": 0x10,
    "CTRL": 0x11, "ALT": 0x12, "PAUSE": 0x13, "CAPSLOCK": 0x14,
    "ESC": 0x1B, "SPACE": 0x20, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "END": 0x23, "HOME": 0x24, "LEFT": 0x25, "UP": 0x26,
    "RIGHT": 0x27, "DOWN": 0x28, "INSERT": 0x2D, "DELETE": 0x2E,
    "LWIN": 0x5B, "RWIN": 0x5C, "NUMLOCK": 0x90, "SCROLLLOCK": 0x91,
}


def dark_checkbutton(parent, text: str, variable, command=None):
    """Native dark checkbox that does not depend on ttkbootstrap icon fonts."""
    return tk.Checkbutton(
        parent, text=text, variable=variable, command=command,
        background=COLOR_BG, foreground=COLOR_TEXT,
        activebackground=COLOR_BG, activeforeground=COLOR_TEXT,
        selectcolor=COLOR_SURFACE, highlightthickness=0, borderwidth=0,
        font=("Microsoft YaHei UI", 10), cursor="hand2",
    )
for _i in range(1, 25):
    VK_NAMES[f"F{_i}"] = 0x6F + _i


def selectable_target_windows(windows: list[WindowInfo]) -> list[WindowInfo]:
    """Remove MacroFlow's own windows and duplicate handles from the picker."""
    result: list[WindowInfo] = []
    seen: set[int] = set()
    for item in windows:
        if item.hwnd in seen or is_current_process_window(item.hwnd):
            continue
        seen.add(item.hwnd)
        result.append(item)
    return result


def drag_selection_region(start_x: int, start_y: int, end_x: int, end_y: int) -> list[int] | None:
    """Return x,y,w,h only for a visible upper-left to lower-right drag."""
    width, height = int(end_x) - int(start_x), int(end_y) - int(start_y)
    if width < 2 or height < 2:
        return None
    return [int(start_x), int(start_y), width, height]


def restore_modal_after_overlay(dialog, main, previous_main_state: str) -> bool:
    """Restore a hidden modal safely after a full-screen selection overlay."""
    try:
        main.deiconify()
    except tk.TclError:
        pass
    if previous_main_state == "zoomed":
        try:
            main.state("zoomed")
        except tk.TclError:
            pass
    try:
        main.update_idletasks()
    except tk.TclError:
        pass
    try:
        show_window_no_activate(int(main.winfo_id()))
    except (TypeError, ValueError, tk.TclError):
        pass
    try:
        dialog.transient(main)
    except tk.TclError:
        pass
    try:
        dialog.deiconify()
        dialog.update_idletasks()
    except tk.TclError:
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        return False
    for operation in (dialog.lift, dialog.focus_force):
        try:
            operation()
        except tk.TclError:
            pass
    try:
        dialog.grab_set()
    except tk.TclError:
        return False
    return True


def activate_main_after_modal(main) -> bool:
    """Return focus to the main editor without changing its geometry."""
    try:
        main.deiconify()
        main.update_idletasks()
        main.lift()
        main.focus_force()
    except tk.TclError:
        return False
    return True


def ancestor_windows(window) -> list:
    """Return every parent window above ``window``, nearest first."""
    result = []
    seen = {id(window)}
    current = window
    while current is not None and len(result) < 20:
        try:
            current = current.master
        except (AttributeError, tk.TclError):
            break
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        result.append(current)
    return result


class ScreenPointPicker:
    """Full-screen screenshot curtain that records one or two screen points.

    Hides the modal dialog and the main window while picking, then reports the
    picked screen coordinate(s) through ``on_result``. With ``two_points`` the
    first click records the start point and the second click the end point.
    """

    def __init__(self, owner, main, on_result, two_points: bool = False,
                 tip_text: str = "",
                 hidden_windows: list | None = None):
        # owner may be None when the picker is started from the main window
        # itself (no modal dialog to hide and restore).
        self.owner = owner
        self.main = main
        self.on_result = on_result
        self.two_points = two_points
        self.tip_text = tip_text
        self.overlay = None
        self.screenshot = None
        self.canvas = None
        self.tip_id = None
        self.main_previous_state = "normal"
        self.hidden_windows = list(hidden_windows or [])
        self.hidden_alphas: list[float] = []
        self.first_point = None
        self.origin = (0, 0)

    def start(self):
        if self.overlay is not None:
            return
        try:
            self.main_previous_state = str(self.main.state())
            self.hidden_alphas = []
            for window in self.hidden_windows:
                try:
                    self.hidden_alphas.append(float(window.attributes("-alpha")))
                except (TypeError, ValueError, tk.TclError):
                    self.hidden_alphas.append(1.0)
                window.attributes("-alpha", 0.0)
            if self.owner is not None:
                self.owner.grab_release()
                self.owner.withdraw()
            self.main.withdraw()
            self.main.update_idletasks()
            self.main.after(100, self._show_curtain)
        except Exception as exc:
            self.close()
            if self.owner is not None:
                show_floating_notice(self.owner, "无法选取位置", str(exc))

    def _show_curtain(self):
        try:
            screen, origin = capture_bgr()
            image = Image.fromarray(screen[:, :, ::-1])
            image = ImageEnhance.Brightness(image).enhance(0.62)
            overlay = tk.Toplevel(self.main)
            self.overlay = overlay
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.configure(background="#000000", cursor="crosshair")
            width, height = image.size
            left, top = int(origin[0]), int(origin[1])
            self.origin = (left, top)
            overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
            self.screenshot = ImageTk.PhotoImage(image, master=overlay)
            canvas = tk.Canvas(
                overlay, width=width, height=height,
                highlightthickness=0, cursor="crosshair",
            )
            canvas.pack(fill="both", expand=True)
            canvas.create_image(0, 0, image=self.screenshot, anchor="nw")
            self.canvas = canvas
            self.tip_id = canvas.create_text(
                width // 2, 34,
                text=self.tip_text or (
                    "点击要记录的位置；只记录坐标，不会点击下方窗口；Esc 取消"
                    if not self.two_points else
                    "第一次点击记录起点，移动光标到终点后再次点击；Esc 取消"
                ),
                fill="#FFFFFF", font=("Microsoft YaHei UI", 13, "bold"),
            )
            canvas.bind("<Button-1>", self._on_click)
            overlay.bind("<Escape>", lambda _event: self.close())
            overlay.update_idletasks()
            overlay.lift()
            overlay.focus_force()
            overlay.grab_set()
        except Exception as exc:
            self.close()
            if self.owner is not None:
                show_floating_notice(self.owner, "无法截取幕布", str(exc))

    def _on_click(self, event):
        x, y = int(event.x_root), int(event.y_root)
        if not self.two_points:
            self.close()
            self.on_result(x, y)
            return
        if self.first_point is None:
            self.first_point = (x, y)
            if self.canvas is not None and self.tip_id is not None:
                self.canvas.itemconfigure(
                    self.tip_id,
                    text=f"起点 ({x}, {y}) 已记录，移动光标到终点后再次点击；Esc 取消",
                )
            return
        start_x, start_y = self.first_point
        self.close()
        self.on_result(start_x, start_y, x, y)

    def close(self):
        overlay = self.overlay
        self.overlay = None
        self.screenshot = None
        self.canvas = None
        self.tip_id = None
        self.first_point = None
        if overlay is not None:
            try:
                overlay.grab_release()
                overlay.destroy()
            except tk.TclError:
                pass
        for window, previous_state in zip(
            reversed(getattr(self, "hidden_windows", [])),
            reversed(getattr(self, "hidden_alphas", [])),
        ):
            try:
                window.attributes("-alpha", previous_state)
                window.update_idletasks()
            except (TypeError, ValueError, tk.TclError):
                pass
        if self.owner is not None:
            restore_modal_after_overlay(self.owner, self.main, self.main_previous_state)
            try:
                self.owner.after(30, lambda: restore_modal_after_overlay(
                    self.owner, self.main, self.main_previous_state,
                ))
            except tk.TclError:
                pass
        else:
            try:
                self.main.deiconify()
                if self.main_previous_state == "zoomed":
                    self.main.state("zoomed")
                self.main.update_idletasks()
                show_window_no_activate(int(self.main.winfo_id()))
            except (TypeError, ValueError, tk.TclError):
                pass


class ScreenRegionPicker:
    """Full-screen curtain that records a drag-selected rectangle.

    Mirrors the image-action region picker: press the left button at the
    top-left corner, drag to the bottom-right corner and release. Reports the
    region as ``[x, y, w, h]`` through ``on_result``; Esc cancels.

    While selecting, the whole window chain is hidden — the owner dialog, the
    main window and every ancestor dialog up to the app main window
    (``hidden_windows``) — so the screen is unobstructed for drag selection
    and ``on_result`` can capture a clean screenshot before the windows come
    back.
    """

    def __init__(self, owner, main, on_result, tip_text: str = "",
                 hidden_windows: list | None = None):
        self.owner = owner
        self.main = main
        self.on_result = on_result
        self.tip_text = tip_text
        self.overlay = None
        self.canvas = None
        self.tip_id = None
        self.rectangle_id = None
        self.drag_start = None
        self.main_previous_state = "normal"
        # 框选期间需要一并隐藏的上层窗口（从 main 的父窗口一直数到应用主窗口）。
        self.hidden_windows = list(hidden_windows or [])
        self.hidden_states: list[str] = []

    def start(self):
        if self.overlay is not None:
            return
        try:
            self.main_previous_state = str(self.main.state())
            # 整条窗口链全部隐藏：否则主窗口 / 上级对话框遮挡屏幕，无法框选和截图。
            self.hidden_states = []
            for window in self.hidden_windows:
                try:
                    self.hidden_states.append(str(window.state()))
                except tk.TclError:
                    self.hidden_states.append("normal")
                window.withdraw()
            if self.owner is not None:
                self.owner.grab_release()
                self.owner.withdraw()
            self.main.withdraw()
            self.main.update_idletasks()
            self.main.after(100, self._show_overlay)
        except Exception as exc:
            self.close()
            if self.owner is not None:
                show_floating_notice(self.owner, "无法框选", str(exc))

    def _show_overlay(self):
        try:
            screen = get_virtual_screen_rect()
            left, top = int(screen["left"]), int(screen["top"])
            width, height = int(screen["width"]), int(screen["height"])
            overlay = tk.Toplevel(self.main)
            self.overlay = overlay
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.attributes("-alpha", 0.32)
            overlay.configure(background="#000000", cursor="crosshair")
            overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
            canvas = tk.Canvas(
                overlay, background="#000000", highlightthickness=0,
                cursor="crosshair",
            )
            canvas.pack(fill="both", expand=True)
            self.canvas = canvas
            self.tip_id = canvas.create_text(
                width // 2, 34,
                text=self.tip_text or (
                    "按住鼠标左键，从左上角向右下角拖动；松开完成，Esc 取消"
                ),
                fill="#FFFFFF", font=("Microsoft YaHei UI", 13, "bold"),
            )
            canvas.bind("<ButtonPress-1>", self._drag_begin)
            canvas.bind("<B1-Motion>", self._drag_move)
            canvas.bind("<ButtonRelease-1>", self._drag_finish)
            overlay.bind("<Escape>", lambda _event: self.close())
            overlay.update_idletasks()
            overlay.lift()
            overlay.focus_force()
            overlay.grab_set()
        except Exception as exc:
            self.close()
            if self.owner is not None:
                show_floating_notice(self.owner, "无法框选", str(exc))

    def _drag_begin(self, event):
        self.drag_start = (
            int(event.x_root), int(event.y_root), int(event.x), int(event.y),
        )
        if self.rectangle_id is not None:
            self.canvas.delete(self.rectangle_id)
        self.rectangle_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#FFB020", width=4,
        )

    def _drag_move(self, event):
        if self.drag_start is None or self.rectangle_id is None:
            return
        _, _, local_x, local_y = self.drag_start
        self.canvas.coords(self.rectangle_id, local_x, local_y, event.x, event.y)
        width, height = event.x - local_x, event.y - local_y
        if self.tip_id is not None:
            self.canvas.itemconfigure(
                self.tip_id,
                text=f"区域：{max(0, width)} × {max(0, height)}　松开完成，Esc 取消",
            )

    def _drag_finish(self, event):
        if self.drag_start is None:
            return
        start_x, start_y, _, _ = self.drag_start
        region = drag_selection_region(
            start_x, start_y, int(event.x_root), int(event.y_root),
        )
        if region is None:
            self.close()
            if self.owner is not None:
                show_floating_notice(
                    self.owner, "框选无效",
                    "请按住左键，从左上角向右下角拖出一个矩形区域。",
                )
            return
        # 先移除幕布、保持应用窗口隐藏，再回调（"截图新建…"时能截到干净屏幕），
        # 最后恢复被隐藏的窗口。
        self._destroy_overlay()
        try:
            self.on_result(region)
        finally:
            self._restore_windows()

    def _destroy_overlay(self):
        overlay = self.overlay
        self.overlay = None
        self.canvas = None
        self.tip_id = None
        self.rectangle_id = None
        self.drag_start = None
        if overlay is not None:
            try:
                overlay.grab_release()
                overlay.destroy()
            except tk.TclError:
                pass

    def _restore_windows(self):
        """Restore the windows hidden while selecting (top-most one last)."""
        for window, previous_state in zip(
            reversed(self.hidden_windows), reversed(self.hidden_states),
        ):
            try:
                window.deiconify()
                if previous_state == "zoomed":
                    window.state("zoomed")
                window.update_idletasks()
                show_window_no_activate(int(window.winfo_id()))
            except (TypeError, ValueError, tk.TclError):
                pass
        if self.owner is not None:
            restore_modal_after_overlay(self.owner, self.main, self.main_previous_state)
            try:
                self.owner.after(30, lambda: restore_modal_after_overlay(
                    self.owner, self.main, self.main_previous_state,
                ))
            except tk.TclError:
                pass
        else:
            try:
                self.main.deiconify()
                if self.main_previous_state == "zoomed":
                    self.main.state("zoomed")
                self.main.update_idletasks()
                show_window_no_activate(int(self.main.winfo_id()))
            except (TypeError, ValueError, tk.TclError):
                pass

    def close(self):
        """Cancel the selection: remove the overlay and restore the hidden windows."""
        self._destroy_overlay()
        self._restore_windows()


class ScreenOffsetPicker(ScreenRegionPicker):
    """Full-screen curtain that records a press-drag-release offset vector."""

    def _drag_begin(self, event):
        self.drag_start = (
            int(event.x_root), int(event.y_root), int(event.x), int(event.y),
        )
        if self.rectangle_id is not None:
            self.canvas.delete(self.rectangle_id)
        self.rectangle_id = self.canvas.create_line(
            event.x, event.y, event.x, event.y,
            fill="#FFB020", width=4, arrow="last",
        )

    def _drag_move(self, event):
        if self.drag_start is None or self.rectangle_id is None:
            return
        start_x, start_y, local_x, local_y = self.drag_start
        self.canvas.coords(self.rectangle_id, local_x, local_y, event.x, event.y)
        dx = int(event.x_root) - start_x
        dy = int(event.y_root) - start_y
        if self.tip_id is not None:
            self.canvas.itemconfigure(
                self.tip_id,
                text=f"偏移：dx={dx:+d}，dy={dy:+d}　松开完成，Esc 取消",
            )

    def _drag_finish(self, event):
        if self.drag_start is None:
            return
        start_x, start_y, _, _ = self.drag_start
        end_x, end_y = int(event.x_root), int(event.y_root)
        if start_x == end_x and start_y == end_y:
            self.close()
            if self.owner is not None:
                show_floating_notice(
                    self.owner, "偏移无效",
                    "请按住左键从起点拖到终点，移动后再松开。",
                )
            return
        self._destroy_overlay()
        try:
            self.on_result(start_x, start_y, end_x, end_y)
        finally:
            self._restore_windows()


def image_action_option_defaults(action: dict) -> tuple[str, bool]:
    """Return new-action defaults while preserving explicitly saved values."""
    return (
        str(action.get("on_found", "click")),
        bool(action.get("show_result_notice", True)),
    )


IMAGE_TIMEOUT_OPTIONS = (
    ("继续执行", "continue"),
    ("跳转到目标动作", "jump"),
    ("结束当前脚本", "end_current_script"),
    ("停止全部执行", "stop"),
)

MODULE_RESULT_OPTIONS = (
    ("继续下一行", "continue"),
    ("跳转到行对象", "jump"),
    ("结束当前最里层脚本", "end_current_script"),
)


def _option_value(value: str, options, default: str) -> str:
    """把界面显示值或存储值换算成存储值；未知值回退 default。"""
    return next(
        (stored for label, stored in options if value in {label, stored}),
        default,
    )


def _option_label(value: str, options, default: str) -> str:
    """把存储值换算成界面显示值；未知值回退 default。"""
    return next((label for label, stored in options if stored == value), default)


def module_result_option_label(value: str) -> str:
    return _option_label(value, MODULE_RESULT_OPTIONS, "继续下一行")


def module_result_option_value(value: str) -> str:
    return _option_value(value, MODULE_RESULT_OPTIONS, "continue")


def image_timeout_option_label(value: str) -> str:
    return _option_label(value, IMAGE_TIMEOUT_OPTIONS, "继续执行")


def image_timeout_option_value(value: str) -> str:
    return _option_value(value, IMAGE_TIMEOUT_OPTIONS, "continue")


def image_timeout_option_defaults(action: dict) -> tuple[str, int, int, int, int]:
    """Return timeout behavior, timeout, pre-delay, jump row and post-timeout delay."""
    behavior = str(action.get("on_timeout", "continue"))
    if behavior not in {stored for _label, stored in IMAGE_TIMEOUT_OPTIONS}:
        behavior = "continue"
    return (
        behavior,
        max(0, int(action.get("timeout_ms", 3000))),
        max(0, int(action.get("delay_ms", 1000))),
        max(1, int(action.get("timeout_jump_row", 1))),
        max(0, int(action.get("timeout_delay_ms", 0))),
    )


def image_jump_target_options(actions: list[dict]) -> list[tuple[str, str]]:
    """Return current row labels paired with stable action identities.

    Labels include a concrete, type-specific detail (coordinates, key name,
    template or script filename, delay, …) so the user can tell which row
    a jump target refers to without opening the script.
    """
    kind_labels = {
        "delay": "延时", "key": "键盘", "key_press": "键盘", "text": "文本",
        "mouse_move": "鼠标移动", "mouse_button": "鼠标按键", "click": "点击",
        "repeat_click": "连续点击",
        "scroll": "滚轮", "image_match": "识图", "text_ocr": "识别文字",
        "ocr_compare": "数字比较", "multi_condition_click": "多条件识图",
        "notice": "浮动提醒", "comment": "注释",
        "script_ref": "引用脚本", "open_app": "打开软件",
        "close_app": "关闭软件", "jump": "跳转",
        "global_detect": "全局检测", "restart_workflow": "重启工作流",
        "end_current_script": "结束脚本", "jump_current_script_last": "跳转脚本尾",
        "activate_window": "前置窗口", "module_ref": "模块引用",
    }
    button_names = {"left": "左键", "right": "右键", "middle": "中键"}
    clip = lambda text, limit=20: (  # noqa: E731
        s if len(s := str(text).replace("\n", " ").strip()) <= limit
        else s[:limit] + "…"
    )

    def point(action: dict) -> str:
        raw = action.get("click_point")
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                return f"({int(raw[0])},{int(raw[1])})"
            except (TypeError, ValueError):
                pass
        try:
            return f"({int(action.get('x', 0))},{int(action.get('y', 0))})"
        except (TypeError, ValueError):
            return ""

    options: list[tuple[str, str]] = []
    for index, action in enumerate(actions):
        action_id = str(action.get("action_id", "")).strip()
        if not action_id:
            continue
        kind = str(action.get("type", "动作"))
        row_kind_label = kind_labels.get(kind, kind)
        button = button_names.get(str(action.get("button", "left")), str(action.get("button", "left")))
        detail = ""
        if kind == "delay":
            detail = f"等 {int(action.get('ms', 0))} ms"
        elif kind in {"key", "key_press"}:
            key_name = str(action.get("name") or action.get("vk") or "未知")
            detail = ("按下 " if action.get("down") else "松开 ") + key_name if kind == "key" else f"敲击 {key_name}"
        elif kind == "text":
            detail = "输入 " + clip(action.get("text", ""), 16)
        elif kind == "mouse_move":
            if action.get("mode") == "relative":
                detail = f"ΔX {int(action.get('dx', 0))},ΔY {int(action.get('dy', 0))}"
            else:
                detail = f"移动到 {point(action)}"
        elif kind == "mouse_button":
            state = "按下" if action.get("down") else "松开"
            detail = f"{state} {button} {point(action)}"
        elif kind in {"click", "repeat_click"}:
            count = int(action.get("count", 2)) if kind == "repeat_click" else 1
            times = f" ×{count}" if count != 1 else ""
            if kind == "click" and action.get("pos_mode") == "current":
                detail = f"{button} 当前位置"
            else:
                detail = f"{button}{times} {point(action)}"
        elif kind == "scroll":
            detail = f"横{int(action.get('dx', 0))} 纵{int(action.get('dy', 0))} {point(action)}"
        elif kind == "image_match":
            if action.get("module_ref"):
                key = str(action.get("module_key") or action.get("template", ""))
                obj = registered_module_object(key)
                name = str(
                    obj.get("name") or Path(key.replace("\\", "/")).stem
                ) if obj else Path(key.replace("\\", "/")).stem
                if obj and obj.get("recognize") == "number":
                    row_kind_label = "读取数字"
                    expected = action.get("expected_number")
                    detail = f"模块 {clip(name, 14)} · 比较 {expected if expected is not None else '未设置'}"
                else:
                    detail = "模块 " + clip(name, 16)
            else:
                detail = clip(Path(str(action.get("template", ""))).name, 16)
        elif kind == "text_ocr":
            detail = clip(action.get("expected_text", ""), 16) or "任意文字"
        elif kind == "ocr_compare":
            separator = str(action.get("separator", "/"))
            detail = f"数字{separator}数字 · 相等:{action.get('equal_action', 'continue')} · 不相等:{action.get('not_equal_action', 'continue')}"
        elif kind == "multi_condition_click":
            enabled = [
                str(condition.get("type", ""))
                for condition in action.get("conditions", [])
                if isinstance(condition, dict) and condition.get("enabled")
            ]
            detail = f"启用 {len(enabled)}/3 个条件 · 点击 {int(action.get('click_count', 1))} 次"
        elif kind == "notice":
            detail = clip(action.get("text", ""), 16)
        elif kind == "comment":
            detail = clip(action.get("text", ""), 16)
        elif kind == "script_ref":
            detail = "执行 " + clip(Path(str(action.get("script", ""))).name or "未设置", 16)
        elif kind == "open_app":
            name = clip(Path(str(action.get("path", ""))).name or "未设置", 16)
            args = str(action.get("args", "")).strip()
            detail = f"启动 {name}" + (f"（{args}）" if args else "")
        elif kind == "close_app":
            detail = "结束 " + clip(action.get("name", "") or "未设置", 16)
        elif kind == "jump":
            jump_id = str(action.get("jump_action_id", "")).strip()
            if jump_id == SCRIPT_START_TARGET_ID:
                detail = "跳到脚本开头"
            elif jump_id == NEXT_WORKFLOW_STEP_TARGET_ID:
                detail = "跳到脚本结尾"
            else:
                detail = f"跳到第 {max(1, int(action.get('jump_row', 1)))} 行"
        elif kind == "activate_window":
            detail = clip(action.get("title") or action.get("name", ""), 16)
        label = f"第 {index + 1} 行 · {row_kind_label}"
        if detail:
            label += f" · {detail}"
        options.append((label, action_id))
    return options


def image_found_jump_target_options(actions: list[dict]) -> list[tuple[str, str]]:
    """Successful recognition can jump to an action or finish this workflow step."""
    return [
        ("直接结束当前脚本，执行工作流下一项", NEXT_WORKFLOW_STEP_TARGET_ID),
        *image_jump_target_options(actions),
    ]


def select_jump_target_label(saved_action_id: str, saved_row: int,
                             jump_options: list[tuple[str, str]]) -> str:
    """Pick the row-object label for a saved jump target.

    Stable action id wins (rows may have moved since the target was saved),
    then the saved row number, then the first row as a safe default.
    """
    target = next(
        (label for label, action_id in jump_options if action_id == saved_action_id), "",
    )
    if target:
        return target
    if 1 <= saved_row <= len(jump_options):
        return jump_options[saved_row - 1][0]
    return jump_options[0][0] if jump_options else ""


def image_click_target_defaults(action: dict) -> tuple[str, list[int]]:
    mode = str(action.get("click_target", "match"))
    label = "自定义坐标" if mode == "custom" else "识图区域中心"
    raw = action.get("click_point", [0, 0])
    try:
        point = [int(raw[0]), int(raw[1])] if len(raw) >= 2 else [0, 0]
    except (TypeError, ValueError):
        point = [0, 0]
    return label, point


def show_floating_notice(parent, title: str, text: str, duration_ms: int = 4500) -> None:
    """Show a reusable, non-blocking notice instead of a modal message box."""
    try:
        root = parent._root()
    except (AttributeError, tk.TclError):
        root = parent
    callback = getattr(root, "_macroflow_notice_callback", None)
    content = f"{title}：{text}" if title else str(text)
    if callable(callback):
        callback(content, duration_ms)
        return

    existing = getattr(root, "_macroflow_fallback_notice", None)
    try:
        if existing is not None and existing.winfo_exists():
            root._macroflow_fallback_notice_label.configure(text=content)
            timer = getattr(root, "_macroflow_fallback_notice_timer", None)
            if timer is not None:
                existing.after_cancel(timer)
            root._macroflow_fallback_notice_timer = existing.after(
                duration_ms, lambda: _close_fallback_notice(root, existing),
            )
            existing.deiconify()
            existing.lift()
            return
    except tk.TclError:
        pass

    notice = tk.Toplevel(root)
    root._macroflow_fallback_notice = notice
    notice.withdraw()
    notice.overrideredirect(True)
    notice.attributes("-topmost", True)
    notice.configure(background="#263541", takefocus=False)
    width, height = 360, 68
    x = max(10, (notice.winfo_screenwidth() - width) // 2)
    notice.geometry(f"{width}x{height}+{x}+36")
    frame = ttk.Frame(notice, padding=(12, 10))
    frame.pack(fill="both", expand=True)
    label = ttk.Label(frame, text=content, wraplength=330, justify="left")
    label.pack(anchor="w", fill="both", expand=True)
    root._macroflow_fallback_notice_label = label
    notice.update_idletasks()
    try:
        make_window_no_activate(notice.winfo_id())
    except Exception:
        pass
    notice.deiconify()
    notice.lift()
    root._macroflow_fallback_notice_timer = notice.after(
        duration_ms, lambda: _close_fallback_notice(root, notice),
    )


def _close_fallback_notice(root, notice) -> None:
    if notice is not getattr(root, "_macroflow_fallback_notice", None):
        return
    root._macroflow_fallback_notice = None
    root._macroflow_fallback_notice_label = None
    root._macroflow_fallback_notice_timer = None
    try:
        notice.destroy()
    except tk.TclError:
        pass


def key_to_vk(text: str) -> tuple[int, str]:
    value = text.strip().upper()
    if not value:
        raise ValueError("请输入按键")
    if value in VK_NAMES:
        return VK_NAMES[value], value
    if value.startswith("VK_"):
        return int(value[3:], 0), value
    # 单个字符优先于纯数字：按键捕获得到的数字键（如 "0" = VK 0x30）
    # 必须按字符解析，而不是当作十进制虚键码 0-9（VK 0 是空键）。
    if len(value) == 1:
        vk = ctypes.windll.user32.VkKeyScanW(value) & 0xFF
        if vk == 0xFF:
            raise ValueError(f"无法识别按键：{text}")
        return vk, value
    if value.startswith("0X") or value.isdigit():
        return int(value, 0), f"VK_{int(value, 0):#x}"
    raise ValueError("请使用单个字符、F1、ENTER、SPACE、CTRL、方向键或 VK_0xNN")


def vk_to_key_name(vk: int) -> str:
    """Inverse of key_to_vk: turn a virtual key code back into a name."""
    vk = int(vk) & 0xFF
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    for name, code in VK_NAMES.items():
        if code == vk:
            return name
    return f"VK_0x{vk:x}"


class ModalDialog(tk.Toplevel):
    def __init__(self, parent, title: str, width: int = 520, height: int = 360,
                 align_top: bool = False, defer_show: bool = False):
        super().__init__(parent)
        self._deferred_show = bool(defer_show)
        if self._deferred_show:
            # 模块表单内容较多：先在隐藏状态完成全部控件、尺寸和顶部定位，
            # show() 时再一次性显示，彻底消除中间位置或空白窗口闪现。
            self.withdraw()
        self.result = None
        self.title(title)
        self.configure(background=COLOR_BG)
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.geometry(f"{width}x{height}")
        self.update_idletasks()
        set_dark_titlebar(self.winfo_id())
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        # 高表单可从创建第一帧就贴顶，避免先在屏幕中间显示、完成内容布局后
        # 再跳到顶部。普通对话框仍沿用父窗口内居中。
        y = 0 if align_top else parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        screen_w = parent.winfo_screenwidth()
        screen_h = parent.winfo_screenheight()
        x = max(0, min(x, max(0, screen_w - width)))
        y = max(0, min(y, max(0, screen_h - height)))
        self.geometry(f"+{x}+{y}")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def show(self):
        self._install_duration_units()
        # 置前并抢占 OS 焦点：模态框从后台窗口打开时若不激活，真实点击
        # 会被其他窗口截走，输入框永远得不到焦点（v1.82.6）。
        if getattr(self, "_deferred_show", False):
            self.deiconify()
            self.update_idletasks()
        self.lift()
        self.focus_force()
        self.wait_window()
        return self.result

    def _install_duration_units(self):
        """Add one ms/s selector beside every entry backed by DurationVar."""
        if getattr(self, "_duration_units_installed", False):
            return
        self._duration_units_installed = True
        if getattr(self, "_skip_auto_duration_units", False):
            # 对话框已手动放置单位框（如 DurationDialog），不能再自动插一个。
            return
        if not hasattr(self, "tk"):
            return
        for widget in self.winfo_children():
            self._install_duration_units_in(widget)

    def _install_duration_units_in(self, parent):
        for widget in parent.winfo_children():
            variable_name = ""
            if isinstance(widget, (ttk.Entry, ttk.Spinbox)):
                variable_name = str(widget.cget("textvariable"))
            variable = None
            for candidate in self.__dict__.values():
                if isinstance(candidate, DurationVar) and str(candidate) == variable_name:
                    variable = candidate
                    break
            if variable is not None:
                manager = widget.winfo_manager()
                combo = ttk.Combobox(
                    parent, textvariable=variable.unit, values=TIME_UNITS,
                    state="readonly", width=4,
                )
                if manager == "grid":
                    info = widget.grid_info()
                    combo.grid(
                        row=int(info["row"]), column=int(info["column"]) + 1,
                        sticky="w", padx=(6, 0), pady=info.get("pady", 0),
                    )
                elif manager == "pack":
                    combo.pack(side="left", padx=(6, 0))
            self._install_duration_units_in(widget)


class ScheduleDialog(ModalDialog):
    def __init__(self, parent, value: str = ""):
        super().__init__(parent, "选择工作流开始时间", 470, 245)
        try:
            initial = datetime.strptime(value, "%Y-%m-%d %H:%M:%S") if value else datetime.now() + timedelta(minutes=1)
        except ValueError:
            initial = datetime.now() + timedelta(minutes=1)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="日期").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.date_entry = DateEntry(
            body, dateformat="%Y-%m-%d", startdate=initial.date(),
            popup_title="选择日期", width=16,
        )
        self.date_entry.grid(row=1, column=0, sticky="ew", padx=(0, 14))

        time_frame = ttk.Frame(body)
        time_frame.grid(row=1, column=1, sticky="w")
        self.hour_var = tk.StringVar(value=f"{initial.hour:02d}")
        self.minute_var = tk.StringVar(value=f"{initial.minute:02d}")
        self.second_var = tk.StringVar(value=f"{initial.second:02d}")
        ttk.Label(body, text="时间").grid(row=0, column=1, sticky="w", pady=(0, 8))
        for index, (variable, values) in enumerate((
            (self.hour_var, [f"{n:02d}" for n in range(24)]),
            (self.minute_var, [f"{n:02d}" for n in range(60)]),
            (self.second_var, [f"{n:02d}" for n in range(60)]),
        )):
            ttk.Combobox(time_frame, textvariable=variable, values=values, state="readonly", width=3).pack(side="left")
            if index < 2:
                ttk.Label(time_frame, text=":").pack(side="left", padx=3)

        ttk.Label(body, text="点击日期框右侧的日历按钮选择日期；时间使用下拉框选择。",
                  foreground=COLOR_MUTED).grid(row=2, column=0, columnspan=2, sticky="w", pady=(18, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", command=self.save).pack(side="right", padx=8)
        ttk.Button(buttons, text="立即执行（清除时间）", command=self.clear).pack(side="left")
        body.columnconfigure(0, weight=1)

    def save(self):
        text = f"{self.date_entry.entry.get()} {self.hour_var.get()}:{self.minute_var.get()}:{self.second_var.get()}"
        try:
            datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            show_floating_notice(self, "时间无效", "请选择有效的日期和时间。")
            return
        self.result = text
        self.destroy()

    def clear(self):
        self.result = ""
        self.destroy()


class DurationDialog(ModalDialog):
    """Small reusable duration editor for standalone delay prompts."""

    def __init__(self, parent, title: str, prompt: str, initial_ms: int = 0):
        super().__init__(parent, title, 430, 205)
        # 单位框已在下方手动放置；show() 的自动安装器会再插一个，必须跳过。
        self._skip_auto_duration_units = True
        self.value = duration_var(max(0, int(initial_ms)))
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=prompt).pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(12, 0))
        ttk.Entry(row, textvariable=self.value).pack(side="left", fill="x", expand=True)
        ttk.Combobox(
            row, textvariable=self.value.unit, values=TIME_UNITS,
            state="readonly", width=4,
        ).pack(side="left", padx=(8, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(20, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def save(self):
        try:
            value = int(self.value.get())
            if value < 0 or value > 86400000:
                raise ValueError
        except ValueError:
            show_floating_notice(self, "时间无效", "请输入 0–86400000 ms 以内的时间。")
            return
        self.result = value
        self.destroy()


class WorkflowBatchSettingsDialog(ModalDialog):
    def __init__(self, parent, repeats: int = 1, before_ms: int = 0,
                 repeat_interval_ms: int = 1000, unlimited: bool = False):
        super().__init__(parent, "统一设置工作流参数", 430, 345)
        self.repeats_var = tk.IntVar(value=max(0, int(repeats)))
        self.before_var = duration_var(max(0, int(before_ms)))
        self.interval_var = duration_var(max(0, int(repeat_interval_ms)))
        self.unlimited_var = tk.BooleanVar(value=bool(unlimited))
        self.enabled_vars = {
            "repeats": tk.BooleanVar(value=False),
            "before_ms": tk.BooleanVar(value=False),
            "repeat_interval_ms": tk.BooleanVar(value=False),
        }
        self.value_widgets = {}

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="只勾选需要统一的参数，未勾选项保持每行原值。",
                  foreground=COLOR_MUTED).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 18))
        for row, (key, label, variable, minimum, maximum, unit) in enumerate((
            ("repeats", "执行次数", self.repeats_var, 0, 999999, "次"),
            ("before_ms", "开始前等待", self.before_var, 0, 86400000, ""),
            ("repeat_interval_ms", "重复间隔", self.interval_var, 0, 86400000, ""),
        ), start=1):
            dark_checkbutton(
                body, text=label, variable=self.enabled_vars[key],
                command=lambda selected=key: self._toggle_field(selected),
            ).grid(row=row, column=0, sticky="w", pady=7)
            widget = ttk.Spinbox(body, textvariable=variable, from_=minimum, to=maximum,
                                 width=16, state="disabled")
            widget.grid(row=row, column=1, sticky="ew", padx=(18, 8), pady=7)
            self.value_widgets[key] = widget
            ttk.Label(body, text=unit, foreground=COLOR_MUTED).grid(row=row, column=2, sticky="w")

        dark_checkbutton(
            body,
            text="不计次数（每次到达这一行都执行一次，不扣减）",
            variable=self.unlimited_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=7)
        ttk.Label(
            body, text="勾选后所有行都设为不计次数。",
            foreground=COLOR_MUTED,
        ).grid(row=5, column=0, columnspan=3, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(22, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="应用到全部任务", command=self.save).pack(side="right", padx=8)
        body.columnconfigure(1, weight=1)

    def _toggle_field(self, key: str):
        state = "normal" if self.enabled_vars[key].get() else "disabled"
        self.value_widgets[key].configure(state=state)

    def save(self):
        selected = {key for key, variable in self.enabled_vars.items() if variable.get()}
        if self.unlimited_var.get():
            selected.add("unlimited")
        if not selected:
            show_floating_notice(self, "尚未选择", "请至少勾选一个需要统一的参数。")
            return
        try:
            values = {
                "repeats": int(self.repeats_var.get()),
                "before_ms": int(self.before_var.get()),
                "repeat_interval_ms": int(self.interval_var.get()),
            }
        except (tk.TclError, ValueError):
            show_floating_notice(self, "数值无效", "请输入有效的整数。")
            return
        ranges = {
            "repeats": (0, 999999),
            "before_ms": (0, 86400000),
            "repeat_interval_ms": (0, 86400000),
        }
        # “不计次数”不是数值字段，只做开关，不参与范围校验与取值。
        field_keys = [key for key in selected if key in ranges]
        if any(not ranges[key][0] <= values[key] <= ranges[key][1] for key in field_keys):
            show_floating_notice(self, "数值超出范围", "请检查执行次数和等待时间。")
            return
        result = {key: values[key] for key in field_keys}
        if "unlimited" in selected:
            result["unlimited"] = True
        self.result = result
        self.destroy()


class WorkflowRepeatDialog(ModalDialog):
    """Single-row workflow count editor with an always-execute option and an
    optional "repeat 2+ starts from a chosen row" target."""

    def __init__(self, parent, repeats: int = 1, unlimited: bool = False,
                 actions: list[dict] | None = None,
                 repeat_start_action_id: str = "", script_name: str = ""):
        title = "设置执行次数"
        if script_name:
            name = str(script_name).strip()
            if len(name) > 24:
                name = name[:24] + "…"
            title += f" — {name}"
        super().__init__(parent, title, 520, 345)
        self.repeats_var = tk.IntVar(value=max(0, int(repeats)))
        self.unlimited_var = tk.BooleanVar(value=bool(unlimited))
        self.actions = list(actions) if actions else []
        self.jump_options = image_jump_target_options(self.actions)
        self.repeat_start_var = tk.BooleanVar(
            value=bool(repeat_start_action_id) and any(
                action_id == str(repeat_start_action_id).strip()
                for _label, action_id in self.jump_options
            ),
        )
        self._preserved_repeat_start_id = str(repeat_start_action_id).strip()

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="不计次数：只要轮到这一行就执行一次，不扣减次数。",
            foreground=COLOR_MUTED,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        dark_checkbutton(
            body,
            text="不计次数（始终执行）",
            variable=self.unlimited_var,
            command=self._update_count_state,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)

        count_row = ttk.Frame(body)
        count_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(count_row, text="剩余次数").pack(side="left")
        self.repeats_spin = ttk.Spinbox(
            count_row, textvariable=self.repeats_var, from_=0, to=999999, width=16,
        )
        self.repeats_spin.pack(side="left", padx=(18, 8))
        ttk.Label(count_row, text="次", foreground=COLOR_MUTED).pack(side="left")
        self._update_count_state()

        dark_checkbutton(
            body,
            text="第 2 次及以后从指定行开始",
            variable=self.repeat_start_var,
            command=self._update_repeat_start_state,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 6))

        start_row = ttk.Frame(body)
        start_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(start_row, text="起始行").pack(side="left")
        if self.jump_options:
            labels = [label for label, _action_id in self.jump_options]
            saved_label = select_jump_target_label(
                str(repeat_start_action_id).strip(), 0, self.jump_options,
            )
            self.repeat_start_combo = ttk.Combobox(
                start_row, values=labels, state="readonly", width=52,
            )
            if saved_label in labels:
                self.repeat_start_combo.set(saved_label)
            elif labels:
                self.repeat_start_combo.set(labels[0])
        else:
            self.repeat_start_combo = ttk.Combobox(
                start_row, values=[], state="disabled", width=52,
            )
        self.repeat_start_combo.pack(side="left", padx=(18, 8))

        if not self.jump_options:
            hint = "脚本文件不存在，无法选择起始行。"
        else:
            hint = "第 1 次始终从脚本第 1 行开始；不计次数时此项不生效。"
        ttk.Label(
            body, text=hint, foreground=COLOR_MUTED,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self._update_repeat_start_state()

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)
        body.columnconfigure(1, weight=1)

    def _update_count_state(self, _event=None):
        state = "disabled" if self.unlimited_var.get() else "normal"
        self.repeats_spin.configure(state=state)

    def _update_repeat_start_state(self, _event=None):
        enabled = bool(self.repeat_start_var.get()) and bool(self.jump_options)
        self.repeat_start_combo.configure(
            state="readonly" if enabled else "disabled",
        )

    def save(self):
        try:
            repeats = max(0, int(self.repeats_var.get()))
        except (tk.TclError, ValueError):
            show_floating_notice(self, "数值无效", "请输入有效的整数。")
            return
        repeat_start_action_id = ""
        if self.repeat_start_var.get() and self.jump_options:
            selected = str(self.repeat_start_combo.get())
            repeat_start_action_id = next(
                (
                    action_id for label, action_id in self.jump_options
                    if label == selected
                ),
                "",
            )
        elif not self.jump_options:
            # 脚本文件缺失：原样保留已有配置，避免误抹掉。
            repeat_start_action_id = str(
                getattr(self, "_preserved_repeat_start_id", ""),
            )
        self.result = {
            "repeats": repeats,
            "unlimited": bool(self.unlimited_var.get()),
            "repeat_start_action_id": repeat_start_action_id,
        }
        self.destroy()


class JumpActionDialog(ModalDialog):
    """Configure a jump target and its optional workflow-repeat condition."""

    def __init__(self, parent, action: dict | None = None,
                 actions: list[dict] | None = None):
        super().__init__(parent, "添加跳转动作" if not action else "编辑跳转动作", 620, 370)
        action = action or {}
        self._source = dict(action or {})
        action_list = actions or []
        current_id = str(action.get(ACTION_ID_KEY, "")).strip()
        normal_options = [
            (label, action_id)
            for label, action_id in image_jump_target_options(action_list)
            if action_id != current_id
        ]
        self.target_ids = {
            SCRIPT_START_LABEL: SCRIPT_START_TARGET_ID,
            **{label: action_id for label, action_id in normal_options},
            SCRIPT_END_LABEL: NEXT_WORKFLOW_STEP_TARGET_ID,
        }
        row_by_id = {
            str(item.get(ACTION_ID_KEY, "")).strip(): index + 1
            for index, item in enumerate(action_list)
        }
        self.target_rows = {
            SCRIPT_START_LABEL: 1,
            **{label: row_by_id.get(action_id, 1) for label, action_id in normal_options},
            SCRIPT_END_LABEL: len(action_list) + 1,
        }
        saved_id = str(action.get("jump_action_id", "")).strip()
        if saved_id == SCRIPT_START_TARGET_ID:
            selected = SCRIPT_START_LABEL
        elif saved_id == NEXT_WORKFLOW_STEP_TARGET_ID:
            selected = SCRIPT_END_LABEL
        else:
            selected = next(
                (label for label, action_id in normal_options if action_id == saved_id),
                SCRIPT_START_LABEL,
            )
        self.target = tk.StringVar(value=selected)
        self.workflow_repeat_at_least_2 = tk.BooleanVar(
            value=bool(action.get("workflow_repeat_at_least_2", True)),
        )

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="跳转到").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=8)
        ttk.Combobox(
            body, textvariable=self.target, values=list(self.target_ids),
            state="readonly", width=50,
        ).grid(row=0, column=1, sticky="ew", pady=8)
        ttk.Label(
            body,
            text=("脚本开头会从第 1 行重新执行；指定行会跟随该动作移动；"
                  "脚本结尾会立即结束当前脚本，工作流继续下一项。"),
            foreground=COLOR_MUTED, wraplength=530,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        condition_frame = ttk.LabelFrame(body, text="跳转生效条件", padding=(12, 8))
        condition_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Radiobutton(
            condition_frame,
            text="每次执行到该动作都跳转",
            variable=self.workflow_repeat_at_least_2,
            value=False,
        ).pack(anchor="w")
        ttk.Radiobutton(
            condition_frame,
            text="仅当工作流第 2 次或脚本多次执行的第 2 次及以后时跳转",
            variable=self.workflow_repeat_at_least_2,
            value=True,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            body,
            text=("选择第二项后：工作流第 1 次、脚本重复执行的第 1 次和单次运行脚本时"
                  "都会继续下一行；从第 2 次开始才跳到上方选择的行对象。"),
            foreground=COLOR_MUTED, wraplength=530,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=(0, 8))

    def save(self):
        label = self.target.get()
        target_id = self.target_ids.get(label)
        if not target_id:
            show_floating_notice(self, "请选择目标", "请选择脚本开头、指定动作或脚本结尾。")
            return
        condition_var = getattr(self, "workflow_repeat_at_least_2", None)
        updated = dict(getattr(self, "_source", None) or {})
        updated.update({
            "type": "jump",
            "jump_action_id": target_id,
            "jump_row": int(self.target_rows.get(label, 1)),
            "workflow_repeat_at_least_2": (
                bool(condition_var.get()) if condition_var is not None else True
            ),
        })
        updated.setdefault("delay_ms", 0)
        self.result = updated
        self.destroy()


class GlobalDetectDialog(ModalDialog):
    """Configure a global-detection trigger.

    require_click=False 为脚本"触发条件"模式：只配置识别设置，点击等操作写在
    脚本语句体里，触发后依次执行语句体。

    jump=True 为普通脚本内嵌"全局模块"行模式：播放到该行时启用检测，
    触发后跳转到脚本第 N 行继续执行，无点击。
    """

    def __init__(self, parent, action: dict | None = None, require_click: bool = True,
                 jump: bool = False, actions: list[dict] | None = None):
        self.jump = bool(jump)
        self.require_click = bool(require_click) and not self.jump
        if self.jump:
            title, height = "添加全局模块", 450
        elif not self.require_click:
            title, height = "设置触发条件", 410
        else:
            title, height = "添加全局检测", 470
        super().__init__(parent, title, 580, height)
        action = action_with_live_module_binding(action)
        region = action.get("region", [])
        try:
            region_text = ",".join(str(int(part)) for part in region) if len(region) == 4 else ""
        except (TypeError, ValueError):
            region_text = ""
        click_point = action.get("click_point", [])
        try:
            click_text = ",".join(str(int(part)) for part in click_point) if len(click_point) == 2 else ""
        except (TypeError, ValueError):
            click_text = ""
        # 旧配置没有 region_mode：有区域按自定义区域，否则按全屏。
        default_mode = "custom" if len(region) == 4 else "screen"
        self.region_mode = tk.StringVar(value=str(action.get("region_mode", default_mode)))
        saved_module_key = str(action.get("module_key", "")).strip()
        saved_module = registered_module_object(saved_module_key) if saved_module_key else None
        self.module_key = tk.StringVar(value=saved_module_key)
        self.module_name = tk.StringVar(value=(
            str((saved_module or {}).get("name") or "").strip()
            or (Path(saved_module_key.replace("\\", "/")).stem if saved_module_key else "未选择模块")
        ))
        self.template = tk.StringVar(value=str(action.get("template", "")))
        self.threshold = tk.StringVar(value=str(action.get("threshold", 0.85)))
        self.interval = duration_var(action.get("interval_ms", 500))
        self.region = tk.StringVar(value=region_text)
        self.hold = duration_var(action.get("hold_ms", 1000))
        self.click_point = tk.StringVar(value=click_text)
        # 旧配置没有 restart_delay_ms：默认 1000 ms（与 app.DEFAULT_GLOBAL_CLICK_DELAY_MS 一致）。
        self.restart_delay = duration_var(action.get("restart_delay_ms", 1000))
        try:
            jump_row = max(1, int(action.get("jump_row", 1)))
        except (TypeError, ValueError):
            jump_row = 1
        self.jump_row = tk.StringVar(value=str(jump_row))
        self.jump_enabled_var = tk.BooleanVar(value=bool(action.get("jump_enabled", False)))
        self.jump_target_combo = None
        self.picker = None

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="模板").grid(row=0, column=0, sticky="w", pady=8)
        template_row = ttk.Frame(body)
        template_row.grid(row=0, column=1, sticky="ew")
        self.template_combo = ttk.Combobox(
            template_row, textvariable=self.template,
            values=registered_template_options(str(action.get("template", ""))),
            state="readonly",
        )
        self.template_combo.pack(side="left", fill="x", expand=True)
        self.template_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._clear_image_module_binding(),
        )
        ttk.Button(
            template_row, text="选择模块…", command=self.select_image_module,
        ).pack(side="left", padx=(6, 0))
        ttk.Button(template_row, text="模板区域…", command=self.open_template_region_manager).pack(
            side="left", padx=(6, 0),
        )

        rows = [
            ("相似度", self.threshold, 0.1, 1.0, 0.05),
            ("检测间隔", self.interval, 100, 10000, 100),
            ("持续超过", self.hold, 0, 60000, 100),
        ]
        for offset, (label, variable, low, high, increment) in enumerate(rows, start=1):
            ttk.Label(body, text=label).grid(row=offset, column=0, sticky="w", pady=8)
            ttk.Spinbox(
                body, from_=low, to=high, increment=increment,
                textvariable=variable, width=10,
            ).grid(row=offset, column=1, sticky="ew")
        if self.require_click:
            ttk.Label(body, text="点击后延时").grid(row=4, column=0, sticky="w", pady=8)
            ttk.Spinbox(
                body, from_=0, to=60000, increment=100,
                textvariable=self.restart_delay, width=10,
            ).grid(row=4, column=1, sticky="ew")

        jump_row_index = None
        if self.jump:
            # 全局模块行：触发后可跳转到脚本的某一行对象（按动作唯一标识引用），
            # 从该行继续播放到脚本末尾后结束；也可取消勾选只触发不跳转。
            jump_row_index = 5 if self.require_click else 4
            ttk.Checkbutton(
                body, text="启用触发后跳转", variable=self.jump_enabled_var,
                command=self._sync_jump_target_state,
            ).grid(row=jump_row_index, column=0, sticky="w", pady=8)
            jump_row_frame = ttk.Frame(body)
            jump_row_frame.grid(row=jump_row_index, column=1, sticky="ew")
            self.jump_target_ids: dict[str, str] = {}
            self.jump_row_numbers: dict[str, int] = {}
            action_list = actions or []
            normal_options = image_jump_target_options(action_list)
            jump_options = normal_options + [
                (GLOBAL_SCRIPT_END_LABEL, NEXT_WORKFLOW_STEP_TARGET_ID),
            ]
            if jump_options:
                for row_number, (label, action_id) in enumerate(normal_options, start=1):
                    self.jump_target_ids[label] = action_id
                    self.jump_row_numbers[label] = row_number
                self.jump_target_ids[GLOBAL_SCRIPT_END_LABEL] = NEXT_WORKFLOW_STEP_TARGET_ID
                self.jump_row_numbers[GLOBAL_SCRIPT_END_LABEL] = len(action_list) + 1
                saved_target_id = str(action.get("jump_action_id", "")).strip()
                if saved_target_id == NEXT_WORKFLOW_STEP_TARGET_ID or (
                        not saved_target_id and jump_row > len(action_list)):
                    selected_target = GLOBAL_SCRIPT_END_LABEL
                elif normal_options:
                    selected_target = select_jump_target_label(
                        saved_target_id, jump_row, normal_options,
                    )
                else:
                    selected_target = GLOBAL_SCRIPT_END_LABEL
                self.jump_row = tk.StringVar(value=selected_target)
                self.jump_target_combo = ttk.Combobox(
                    jump_row_frame, textvariable=self.jump_row,
                    values=[label for label, _ in jump_options], state="readonly",
                    width=48,
                )
                self.jump_target_combo.pack(side="left")
                ttk.Label(
                    jump_row_frame, text="（也可直接结束当前脚本）", foreground=COLOR_MUTED,
                ).pack(side="left", padx=(6, 0))
            else:
                # 脚本里没有可跳转的行（防御）：退回数字行号输入。
                self.jump_target_combo = ttk.Spinbox(
                    jump_row_frame, from_=1, to=99999, textvariable=self.jump_row, width=8,
                )
                self.jump_target_combo.pack(side="left")
                ttk.Label(
                    jump_row_frame, text="行", foreground=COLOR_MUTED,
                ).pack(side="left", padx=(6, 0))
                ttk.Label(
                    jump_row_frame, text="（跳转后继续播放到脚本末尾）", foreground=COLOR_MUTED,
                ).pack(side="left", padx=(6, 0))
            self._sync_jump_target_state()

        hint_row_index = 6 if self.require_click else (5 if self.jump else 4)
        if self.require_click:
            ttk.Label(body, text="点击位置 (x,y) 留空=点识别处").grid(row=5, column=0, sticky="w", pady=8)
            click_row = ttk.Frame(body)
            # 与标签同行（row 5）；按钮行在 hint_row_index + 1 = row 7，
            # 若放在 row 7 会与按钮行重叠，输入框和“点击屏幕选取…”被遮住。
            click_row.grid(row=5, column=1, sticky="ew")
            ttk.Entry(click_row, textvariable=self.click_point, state="readonly").pack(
                side="left", fill="x", expand=True,
            )
            ttk.Button(click_row, text="点击屏幕选取…", command=self.pick_click_point).pack(
                side="left", padx=(6, 0),
            )
            hint_text = (
                "该模块会启用全局检测：所选模板在检测区域内持续出现超过设定时长后，点击指定位置并延时；"
                "检测区域来自模板（模板图片 + 框选区域），可在“模板区域…”中管理；"
                "作为全局模块时，触发后先执行模块步骤，再继续原工作流。"
            )
        elif self.jump:
            hint_text = (
                "该行是全局模块：脚本播放到本行时启用全局检测，所选模板在检测区域内持续出现超过"
                "设定时长后，跳转到所选的行继续执行脚本；播放到末尾后脚本结束。"
            )
        else:
            hint_text = (
                "该脚本是全局脚本：所选模板在检测区域内持续出现超过设定时长后触发，"
                "依次执行脚本内的所有动作（语句体）；执行完继续检测，触发条件仍满足则再次触发。"
            )
        ttk.Label(
            body,
            text=hint_text,
            foreground=COLOR_MUTED, wraplength=480,
        ).grid(row=hint_row_index, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=hint_row_index + 1, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def open_template_region_manager(self):
        TemplateRegionManagerDialog(self).show()
        self._refresh_template_options()

    def _clear_image_module_binding(self):
        module_key = getattr(self, "module_key", None)
        if module_key is not None:
            module_key.set("")
        module_name = getattr(self, "module_name", None)
        if module_name is not None:
            module_name.set("未选择模块")

    def select_image_module(self):
        binding = choose_module_binding(
            self, categories=("switch", "workflow_global", "script_global"),
        )
        if not binding:
            return
        module_key = str(binding["module_key"])
        template = str(binding["template"])
        region = list(binding.get("region") or [])
        self.module_key.set(module_key)
        self.template.set(template)
        self.region_mode.set("template")
        self.region.set(",".join(map(str, region)))
        obj = registered_module_object(module_key) or {}
        self.module_name.set(
            str(obj.get("name") or "").strip()
            or Path(module_key.replace("\\", "/")).stem
        )
        self.template_combo.configure(values=registered_template_options(template))

    def _refresh_template_options(self):
        current = self.template.get()
        if current and current not in load_template_regions():
            # 管理器里已删除/改名的模板不再可用：清空，由保存校验提示重选。
            self.template.set("")
            current = ""
        self.template_combo.configure(values=registered_template_options(current))

    def pick_click_point(self):
        self.picker = ScreenPointPicker(
            self, self.master, self._apply_click_point,
            tip_text="点击全局检测触发时要点击的位置；只记录坐标；Esc 取消",
        )
        self.picker.start()

    def _apply_click_point(self, x, y):
        self.click_point.set(f"{int(x)},{int(y)}")

    def _sync_jump_target_state(self):
        """取消勾选“启用触发后跳转”时禁用目标选择控件。"""
        combo = self.jump_target_combo
        if combo is None:
            return
        combo.configure(
            state="readonly" if self.jump_enabled_var.get() else "disabled",
        )

    def save(self):
        try:
            module_key_var = getattr(self, "module_key", None)
            module_key = module_key_var.get().strip() if module_key_var is not None else ""
            module_binding = None
            if module_key:
                module_obj = registered_module_object(module_key)
                if module_obj is None:
                    raise ValueError("所选图片模块已不存在，请重新选择")
                module_binding = module_reference_binding(module_key, module_obj)
            template = (
                str(module_binding["template"])
                if module_binding is not None else self.template.get().strip()
            )
            if not template:
                raise ValueError("请从列表中选择模板")
            threshold = max(0.1, min(1.0, float(self.threshold.get())))
            interval = max(100, min(10000, int(self.interval.get())))
            hold = max(0, min(60000, int(self.hold.get())))
            if module_binding is not None:
                region_mode = "template"
                region = list(module_binding["region"])
            elif template in load_template_regions():
                # 引用已登记模板：区域运行时从模板登记表实时读取。
                region_mode, region = "template", []
            else:
                # 编辑旧动作且未改动模板：保留原有区域配置。
                region_mode = self.region_mode.get()
                region = (
                    [int(part.strip()) for part in self.region.get().split(",")]
                    if self.region.get().strip() else []
                )
            if region and (len(region) != 4 or region[2] <= 0 or region[3] <= 0):
                raise ValueError("检测区域需要 x,y,w,h 四个正整数")
            if self.require_click:
                restart_delay = max(0, min(60000, int(self.restart_delay.get())))
                click_point = (
                    [int(part.strip()) for part in self.click_point.get().split(",")]
                    if self.click_point.get().strip() else []
                )
                if click_point and len(click_point) != 2:
                    raise ValueError("点击位置需要 x,y 两个整数")
            else:
                # 触发条件 / 全局模块行模式：没有点击，点击等操作写在语句体里
                # （模块行模式为触发后跳转行，见下方 jump_row）。
                restart_delay = 0
                click_point = None
            jump_action_id = ""
            if getattr(self, "jump", False):
                jump_target_ids = getattr(self, "jump_target_ids", None) or {}
                if jump_target_ids:
                    # 行对象模式：所选行映射回行号，并保存稳定的动作标识。
                    label = self.jump_row.get()
                    jump_row = self.jump_row_numbers.get(label, 1)
                    jump_action_id = jump_target_ids.get(label, "")
                else:
                    jump_row = max(1, int(self.jump_row.get()))
        except ValueError as exc:
            show_floating_notice(self, "参数错误", str(exc))
            return
        result = {
            "type": "global_detect",
            "template": template,
            "threshold": threshold,
            "interval_ms": interval,
            "region_mode": region_mode,
            "region": region,
            "hold_ms": hold,
            "click_point": click_point,
            "restart_delay_ms": restart_delay,
            "delay_ms": 0,
        }
        if module_binding is not None:
            result.update({
                "module_ref": True,
                "module_key": module_key,
                "module_category": str(module_binding.get("module_category") or "switch"),
            })
        if getattr(self, "jump", False):
            jump_enabled_var = getattr(self, "jump_enabled_var", None)
            result["jump_enabled"] = (
                bool(jump_enabled_var.get()) if jump_enabled_var is not None else False
            )
            result["jump_row"] = jump_row
            if jump_action_id:
                result["jump_action_id"] = jump_action_id
        self.result = result
        self.destroy()


def registered_template_options(current: str = "") -> list[str]:
    """注册表里的模板列表（图片 display path）；编辑旧动作时把旧值临时加进来保证显示。"""
    options = sorted(load_template_regions().keys())
    if current and current not in options:
        options = [current] + options
    return options


def fallback_template_options(current: str = "") -> list[str]:
    """备用模板下拉选项：第一项"（不启用）"，其余为注册表模板。"""
    options = ["（不启用）"]
    options.extend(registered_template_options(current))
    return options


def fit_window_to_content(window, parent, minimum_width=640, minimum_height=360,
                          content_width: int | None = None,
                          content_height: int | None = None,
                          align_top: bool = False):
    """按内容实际需求重设窗口尺寸并居中（防高 DPI 下内容被裁掉）。

    打包后的 EXE（PyInstaller onefile）按显示器真实 DPI 渲染（125% 缩放时
    内容需求高度约为开发环境的 1.3 倍），固定高度窗口会把底部内容挤出窗口。
    geometry 的宽高即内容区大小，因此按 winfo_reqwidth/reqheight 重设后
    再居中即可装下全部内容；屏幕放不下时压缩到屏幕内并用可拉伸兜底。

    content_width / content_height：可滚动表单里 Canvas 包裹内容后，窗口自身
    的 reqsize 不再反映内容尺寸，需显式传入内容实际需求尺寸（如 body 的
    winfo_reqwidth / winfo_reqheight）。
    align_top=True：保持水平居中，但窗口顶部贴到屏幕顶部；适合较高的编辑表单。
    """
    window.update_idletasks()
    width = max(
        minimum_width,
        content_width if content_width is not None else window.winfo_reqwidth(),
    )
    height = max(
        minimum_height,
        content_height if content_height is not None else window.winfo_reqheight(),
    )
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    if height > screen_h - 80:
        height = max(minimum_height, screen_h - 80)
    window.geometry(f"{width}x{height}")
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
    y = 0 if align_top else parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    x = max(0, min(x, max(0, screen_w - width)))
    y = max(0, min(y, max(0, screen_h - height)))
    window.geometry(f"+{x}+{y}")
    # 兜底：若内容仍超出屏幕可手动拉伸，按钮行始终可达。
    window.resizable(True, True)


class Tooltip:
    """给控件挂悬停说明：光标停在上面约 0.4 秒后显示，移开自动消失。

    说明框定位在 anchor 控件（默认是被悬停的控件本身）正下方，绝不遮挡
    同行右侧的输入框——悬停字段名时，说明弹出在整行下方，输入框始终
    可见可点，不会被说明文字盖住（否则用户会以为"没有可以输入的地方"）。
    """

    def __init__(self, widget, text: str, anchor=None):
        self.widget = widget
        self.text = text
        self.anchor = anchor if anchor is not None else widget
        self._after_id = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        if self._after_id is not None:
            return
        self._after_id = self.widget.after(400, self._show)

    def _show(self):
        self._after_id = None
        if self._tip is not None:
            return
        anchor = self.anchor
        x = anchor.winfo_rootx() + 8
        y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(
            tip, text=self.text, background="#ffffe0", foreground="#333333",
            justify="left", padx=10, pady=6, font=("Microsoft YaHei UI", 9),
        ).pack()
        tip.update_idletasks()
        sw, sh = tip.winfo_screenwidth(), tip.winfo_screenheight()
        w, h = tip.winfo_width(), tip.winfo_height()
        if x + w > sw:
            x = max(0, sw - w - 4)
        if y + h > sh:
            # 下方放不下就翻到 anchor 上方。
            y = max(0, anchor.winfo_rooty() - h - 6)
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


CATEGORY_LABELS = {
    "switch": "切换模块",
    "workflow_global": "工作流全局模块",
    "script_global": "脚本全局模块",
    "special": "特殊模块",
}
AFTER_ACTION_LABELS = {
    "click_match": "点击识别区域",
    "click_custom": "点击自定义位置",
    "continue": "成功后继续",
    "second_match": "二次识别后点击",
}
AFTER_ACTION_VALUES = {label: value for value, label in AFTER_ACTION_LABELS.items()}
FALLBACK_ON_MATCH_LABELS = {
    "continue": "继续识别主模块（不点击）",
    "click_continue": "点击备用命中位置，继续识别主模块",
    "exit": "直接退出主模块识别（不点击）",
    "click_exit": "点击备用命中位置后退出主模块识别",
}
FALLBACK_ON_MATCH_VALUES = {
    label: value for value, label in FALLBACK_ON_MATCH_LABELS.items()
}
SECOND_MATCH_CLICK_TARGET_LABELS = {
    "first": "第一次识别位置",
    "second": "第二次识别位置",
    "custom_region": "自定义框选区域",
}
SECOND_MATCH_CLICK_TARGET_VALUES = {
    label: value for value, label in SECOND_MATCH_CLICK_TARGET_LABELS.items()
}
SEGMENT_DEPTH_LIMIT = 8


def segment_action_is_blocking(action: dict) -> bool:
    """Return whether a segment row can wait indefinitely for recognition."""
    if action.get("type") not in ("image_match", "global_detect"):
        return False
    if action.get("module_ref"):
        key = str(action.get("module_key") or action.get("template", "")).strip()
        obj = registered_module_object(key) if key else None
        return bool(obj and (
            obj.get("blocking")
            or obj.get("wait_text_absent")
        ))
    return bool(action.get("blocking"))


def workflow_step_label(step: dict) -> str:
    """工作流树里一行对象的显示名（与 app._workflow_step_name 保持一致）。"""
    if step.get("kind") != "module":
        raw = str(step.get("script", ""))
        return Path(raw.replace("\\", "/")).stem or raw or "未设置脚本"
    action = step.get("action") if isinstance(step.get("action"), dict) else {}
    special_type = str(action.get("type", ""))
    if special_type == "restart_workflow":
        return "重新执行工作流"
    if special_type == "end_current_script":
        return END_CURRENT_SCRIPT_LABEL
    if special_type == "jump_current_script_last":
        return "跳转到当前脚本最后一行"
    module_key = str(action.get("module_key") or action.get("template") or "").strip()
    module_obj = registered_module_object(module_key)
    name = (
        str(action.get("module_name", "")).strip()
        or (str(module_obj.get("name", "")).strip() if module_obj else "")
        or Path(module_key.replace("\\", "/")).stem
    )
    return f"模块 {name or '未设置'}"


RESTART_USE_DEFAULT_ROW_LABEL = "（使用默认跳转行）"


def restart_workflow_row_options(workflow_steps: list[dict], default_row: int = 0,
                                 default_label: str = RESTART_USE_DEFAULT_ROW_LABEL,
                                 ) -> tuple[list[str], dict[str, int]]:
    """构建「重新执行工作流」跳转行下拉选项（第 N 行 · 名称，行号 1 基）。

    返回 (labels, label→row 映射)；row 0 表示使用默认跳转行。default_row 非 0
    时在默认项里标注当前默认行号，方便用户知道不选时跳到哪里。
    """
    default_row = max(0, int(default_row or 0))
    first_label = f"（使用默认跳转行：第 {default_row} 行）" if default_row else default_label
    mapping = {first_label: 0}
    for index, step in enumerate(workflow_steps):
        label = f"第 {index + 1} 行 · {workflow_step_label(step)}"
        mapping[label] = index + 1
    return list(mapping), mapping


def _app_workflow_steps(parent) -> list[dict]:
    """沿父窗口链找主应用，取当前工作流的行对象列表（不含全局模块行）。"""
    window = parent
    seen = set()
    while window is not None and len(seen) < 20:
        identity = id(window)
        if identity in seen:
            break
        seen.add(identity)
        app = getattr(window, "_macroflow_app", None)
        workflow = getattr(app, "workflow", None)
        if workflow is not None:
            steps = getattr(workflow, "steps", None)
            if isinstance(steps, list):
                return [step for step in steps if step.get("kind") != "global_module"]
        try:
            window = window.master
        except (AttributeError, tk.TclError):
            break
    return []


def _app_workflow_default_row(parent) -> int:
    """取主应用当前工作流统一设置的「重新执行工作流」默认跳转行（0 = 未设置）。"""
    window = parent
    seen = set()
    while window is not None and len(seen) < 20:
        identity = id(window)
        if identity in seen:
            break
        seen.add(identity)
        app = getattr(window, "_macroflow_app", None)
        workflow = getattr(app, "workflow", None)
        if workflow is not None:
            try:
                return max(0, int(getattr(workflow, "restart_default_row", 0) or 0))
            except (TypeError, ValueError):
                return 0
        try:
            window = window.master
        except (AttributeError, tk.TclError):
            break
    return 0


class RestartWorkflowTargetDialog(ModalDialog):
    """配置「重新执行工作流」动作的跳转目标：行对象或使用默认跳转行。

    row=0 表示使用默认：按当前工作流页面统一设置的默认跳转行
    （default_row，工作流文件里的 restart_default_row），未设置则从第 1 行开始。
    """

    def __init__(self, parent, action: dict | None = None,
                 workflow_steps: list[dict] | None = None,
                 default_row: int = 0):
        super().__init__(parent, "重新执行工作流跳转目标", 600, 300)
        self.workflow_steps = list(workflow_steps if workflow_steps is not None
                                   else _app_workflow_steps(parent))
        self.default_row = max(0, int(default_row or 0))
        try:
            saved_row = max(0, int((action or {}).get("restart_workflow_target_row", 0) or 0))
        except (TypeError, ValueError):
            saved_row = 0
        self.row_var = tk.StringVar()
        self.row_ids: dict[str, int] = {}
        self.row_spin_var = tk.StringVar(value=str(saved_row if saved_row > 0 else 1))
        self._reload_options(selected_row=saved_row)

        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="跳转到").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 8))
        self.row_combo = ttk.Combobox(
            body, textvariable=self.row_var, values=self.row_labels,
            state="readonly", width=46,
        )
        self.row_combo.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        self.row_combo.bind("<<ComboboxSelected>>", self._on_row_selected)
        ttk.Label(
            body,
            text=("选择工作流里的行对象，触发后从该行重新执行工作流；"
                  "选“使用默认跳转行”时按工作流页面统一设置的默认决定。"
                  "未打开工作流时可勾选“自定义行号…”直接输入。"),
            foreground=COLOR_MUTED, wraplength=540,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row_frame = ttk.Frame(body)
        row_frame.grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Label(row_frame, text="行号", foreground=COLOR_MUTED).pack(side="left")
        self.row_spin = ttk.Spinbox(
            row_frame, from_=1, to=99999, textvariable=self.row_spin_var,
            width=8,
        )
        self.row_spin.pack(side="left", padx=(8, 0))
        self._on_row_selected()
        hint_frame = ttk.Frame(body)
        hint_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.default_label = ttk.Label(
            hint_frame,
            text=self._default_hint(), foreground=COLOR_MUTED,
        )
        self.default_label.pack(side="left")
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(20, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def _default_hint(self) -> str:
        if not self.default_row:
            return "工作流默认：未设置（按第 1 行处理）"
        return f"工作流默认：第 {self.default_row} 行（在工作流页面统一设置）"

    def _reload_options(self, selected_row: int = 0):
        labels, self.row_ids = restart_workflow_row_options(
            self.workflow_steps, self.default_row,
        )
        self.row_labels = labels + ["自定义行号…"]
        selected = next(
            (label for label, row in self.row_ids.items() if row == selected_row),
            "自定义行号…" if not self.workflow_steps else self.row_labels[0],
        )
        self.row_var.set(selected)

    def _on_row_selected(self, _event=None):
        custom = self.row_var.get() == "自定义行号…"
        if not custom and not self.workflow_steps:
            self.row_var.set("自定义行号…")
            custom = True
        self.row_spin.configure(state="normal" if custom else "readonly")

    def save(self):
        label = self.row_var.get()
        row = self.row_ids.get(label, 0)
        if not row and label == "自定义行号…":
            try:
                row = max(1, int(self.row_spin_var.get()))
            except (TypeError, ValueError):
                show_floating_notice(self, "行号格式错误", "请输入 1–99999 之间的行号。")
                return
        self.result = {"type": "restart_workflow", "restart_workflow_target_row": row}
        self.destroy()


def segment_row_label(action: dict) -> str:
    """代码段列表里一条动作的摘要文本（仅展示用）。"""
    kind = action.get("type", "")
    if kind == "delay":
        return f"延时 {action.get('ms', 0)} ms"
    if kind in ("key", "key_press"):
        return f"按键 {action.get('key', action.get('name', '?'))}"
    if kind == "text":
        return "输入文本"
    if kind in ("click", "mouse_button"):
        return f"点击（{action.get('button', 'left')}）"
    if kind == "turn":
        return (
            f"转向 ΔX={action.get('dx', 0)}，ΔY={action.get('dy', 0)}"
        )
    if kind == "repeat_click":
        return f"重复点击 {action.get('count', 1)} 次"
    if kind == "mouse_move":
        return "移动鼠标"
    if kind == "image_match":
        label = f"识图 {Path(str(action.get('template', ''))).stem}"
        return f"【阻塞等待】{label}" if segment_action_is_blocking(action) else label
    if kind == "global_detect":
        label = f"全局检测 {Path(str(action.get('template', ''))).stem}"
        return f"【阻塞等待】{label}" if segment_action_is_blocking(action) else label
    if kind == "script_ref":
        return f"引用脚本 {Path(str(action.get('script', ''))).stem}"
    if kind == "open_app":
        return f"打开软件 {Path(str(action.get('path', ''))).stem or '?'}"
    if kind == "close_app":
        return f"关闭软件 {action.get('name', '?')}"
    if kind == "notice":
        return f"提醒 {action.get('text', '')}"
    if kind == "restart_workflow":
        try:
            row = max(0, int(action.get("restart_workflow_target_row", 0) or 0))
        except (TypeError, ValueError):
            row = 0
        return "重新执行工作流" + (f"（跳转第 {row} 行）" if row else "（默认跳转行）")
    if kind == "end_current_script":
        return END_CURRENT_SCRIPT_LABEL
    if kind == "jump_current_script_last":
        return "跳转到当前脚本最后一行"
    if kind == "jump":
        return "跳转"
    if kind == "activate_window":
        signature = action.get("window") or {}
        return f"前置窗口 {signature.get('title', '未设置')}"
    if kind == "comment":
        return f"注释 {action.get('text', '')}"
    return f"动作 {kind}"


class TemplateRegionFormDialog(ModalDialog):
    """新增 / 编辑模块对象表单：模板图片 + 框选区域 + 行为属性。

    保存时校验图片、区域、识别成功后动作相关字段都有效，通过后把
    ``(old_key, key, object_dict)`` 写入 ``self.result``，``show()`` 返回该
    结果；取消返回 ``None``。old_key 非空表示编辑旧条目（更换图片时移除旧
    条目）；object_dict 提供时按它初始化所有字段（对象实时引用编辑）。
    """

    def __init__(self, parent, old_key: str = "", region: list[int] | None = None,
                 category: str = "switch", object_dict: dict | None = None,
                 segment_depth: int = 0, initial_image: str = "",
                 images_dir: str | Path | None = None):
        super().__init__(
            parent, "编辑模块对象" if old_key else "新增模块对象", 620, 760,
            align_top=True, defer_show=True,
        )
        obj = dict(object_dict or {})
        if obj.get("category") not in CATEGORY_LABELS:
            obj["category"] = category if category in CATEGORY_LABELS else "switch"
        self.old_key = old_key
        self.module_enabled = bool(obj.get("enabled", True))
        self.images_dir = Path(images_dir) if images_dir else load_module_images_dir()
        self.segment_depth = segment_depth
        self.picker = None
        self.point_overlay = None
        self.point_screenshot = None
        self.main_previous_state = "normal"
        pure_edit = bool(obj.get("pure_action"))
        self.category_choices = (
            [CATEGORY_LABELS["special"]]
            if pure_edit else [
                CATEGORY_LABELS["switch"], CATEGORY_LABELS["workflow_global"],
                CATEGORY_LABELS["script_global"],
            ]
        )
        legacy_template = (
            old_key if old_key and not old_key.startswith("module:") else ""
        )
        image_source = str(obj.get("template") or initial_image or legacy_template)
        if pure_edit:
            # 纯动作特殊模块（无图片，如「重新执行工作流」）：用名称做 key，
            # 图片留空才能走纯动作保存分支。
            obj["category"] = "special"
            self.image_var = tk.StringVar(value="")
        else:
            self.image_var = tk.StringVar(value=image_source)
        raw_name = str(obj.get("name") or "").strip()
        default_name = self._default_name_for_image(image_source)
        # 旧版本曾把完整图片路径当作名称；打开编辑时自动迁回干净文件名。
        legacy_names = {str(image_source).strip(), Path(str(image_source).replace("\\", "/")).name}
        if not raw_name or raw_name in legacy_names:
            raw_name = default_name
            self._auto_name_value = default_name
        else:
            self._auto_name_value = raw_name if raw_name == default_name else ""
        self.name_var = tk.StringVar(value=raw_name)
        stored_region = region if region is not None else obj.get("region", [0, 0, 0, 0])
        self.region_var = tk.StringVar(
            value=",".join(map(str, stored_region))
            if len(stored_region) == 4 and (stored_region[2] > 0 or stored_region[3] > 0)
            else "",
        )
        self.category_var = tk.StringVar(value=CATEGORY_LABELS[obj["category"]])
        self.recognize_var = tk.StringVar(value={
            "text": "识别文字",
            "number": "读取数字",
            "none": "无需识图",
        }.get(obj.get("recognize"), "模板图片"))
        self.expected_text_var = tk.StringVar(value=str(obj.get("expected_text", "")))
        self.match_mode_var = tk.StringVar(
            value="等于" if obj.get("match_mode") == "equals" else "包含",
        )
        self.wait_text_absent_var = tk.BooleanVar(
            value=bool(obj.get("wait_text_absent", False)),
        )
        self.ocr_offset_up_var = tk.StringVar(value=str(obj.get("ocr_offset_up", 0)))
        self.ocr_offset_down_var = tk.StringVar(value=str(obj.get("ocr_offset_down", 0)))
        self.ocr_offset_left_var = tk.StringVar(value=str(obj.get("ocr_offset_left", 0)))
        self.ocr_offset_right_var = tk.StringVar(value=str(obj.get("ocr_offset_right", 0)))
        self.threshold_var = tk.StringVar(value=str(obj.get("threshold", 0.85)))
        self.interval_var = duration_var(obj.get("interval_ms", 250))
        self.start_delay_var = duration_var(obj.get("start_delay_ms", 0))
        fallback_objects = load_module_objects()
        fallback_key = str(obj.get("fallback_module_key", "")).strip()
        self.fallback_module_keys = {"（不启用）": ""}
        for key, value in fallback_objects.items():
            if key == old_key or value.get("recognize") in ("number", "none") or value.get("pure_action"):
                continue
            name = str(value.get("name", "")).strip() or Path(key.replace("\\", "/")).stem
            label = name if name not in self.fallback_module_keys else f"{name} · {key}"
            self.fallback_module_keys[label] = key
        fallback_label = next(
            (label for label, key in self.fallback_module_keys.items() if key == fallback_key),
            "（不启用）",
        )
        self.fallback_module_key_var = tk.StringVar(value=fallback_label)
        fallback_on_match = str(obj.get("fallback_on_match", "")).strip()
        if fallback_on_match not in FALLBACK_ON_MATCH_LABELS:
            fallback_on_match = "click_continue" if bool(obj.get("fallback_click", False)) else "continue"
        self.fallback_on_match_var = tk.StringVar(
            value=FALLBACK_ON_MATCH_LABELS[fallback_on_match],
        )
        self.fallback_click_var = tk.BooleanVar(value=fallback_on_match.startswith("click_"))
        self.fallback_click_count_var = tk.StringVar(value=str(obj.get("fallback_click_count", 1)))
        self.fallback_click_interval_var = duration_var(obj.get("fallback_click_interval_ms", 100))
        self.ignore_background_var = tk.BooleanVar(
            value=bool(obj.get("ignore_background", False)),
        )
        self.blocking_var = tk.BooleanVar(value=bool(obj.get("blocking", False)))
        self.hold_enabled_var = tk.BooleanVar(value=bool(obj.get("hold_enabled", False)))
        self.hold_var = duration_var(obj.get("hold_ms", 1000))
        self.delay_var = duration_var(obj.get("delay_ms", 0))
        after_action = str(obj.get("after_action", "click_match"))
        if obj.get("recognize") == "text" and after_action == "second_match":
            # 识别文字方式没有二次图片匹配：编辑旧对象时回落为点击识别区域。
            after_action = "click_match"
        self.after_action_var = tk.StringVar(
            value=AFTER_ACTION_LABELS.get(after_action, "点击识别区域"),
        )
        self.run_code_after_action_var = tk.BooleanVar(
            value=bool(obj.get("run_code_after_action", after_action == "run_actions")),
        )
        self.button_var = tk.StringVar(value=obj.get("button", "left"))
        self.click_count_var = tk.StringVar(value=str(obj.get("click_count", 1)))
        click_point = obj.get("click_point") or []
        self.click_point_var = tk.StringVar(
            value=",".join(map(str, click_point)) if len(click_point) == 2 else "",
        )
        self.second_template_var = tk.StringVar(value=str(obj.get("second_match_template", "")))
        # 二次识别直接使用所选模板对象登记的区域，不再单独维护另一份区域。
        self.second_timeout_var = duration_var(obj.get("second_match_timeout_ms", 3000))
        second_click_target = str(obj.get("second_match_click_target", "second"))
        self.second_click_target_var = tk.StringVar(
            value=SECOND_MATCH_CLICK_TARGET_LABELS.get(
                second_click_target, "第二次识别位置",
            ),
        )
        second_click_region = obj.get("second_match_click_region") or []
        self.second_click_region_var = tk.StringVar(
            value=",".join(map(str, second_click_region))
            if len(second_click_region) == 4 and second_click_region[2] > 0 else "",
        )
        self.segment = [dict(item) for item in obj.get("on_success_actions") or []]
        self.run_code_on_timeout_var = tk.BooleanVar(
            value=bool(obj.get("run_code_on_timeout", False)),
        )
        self.not_found_timeout_var = duration_var(obj.get("not_found_timeout_ms", 3000))
        self.timeout_segment = [dict(item) for item in obj.get("on_timeout_actions") or []]
        # 表单行数多，小屏 / 高 DPI（打包版按真实 DPI 渲染）下固定高度窗口会把
        # 底部的延时、识别成功后动作、点击按钮等行挤出窗口且没有滚动条（用户
        # 报告“相似度、检测间隔、延时、动作、点击按钮等都没有输入的地方”）。
        # 用 Canvas + 滚动条包住表单，所有行始终可滚动到达。
        canvas = tk.Canvas(self, background=COLOR_BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = ttk.Frame(canvas, padding=18)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        self.body = body
        self._canvas = canvas
        self._scrollbar = scrollbar

        def update_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def stretch_body(event):
            canvas.itemconfigure(body_window, width=event.width)

        body.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", stretch_body)
        # 窗口初次映射时再校正一次滚动区域（避免首帧布局未完成导致的
        # 视口与内容不匹配，行被裁掉）。
        canvas.bind("<Map>", update_scrollregion)
        self.bind("<MouseWheel>", self._scroll_form)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(
            body, text="模块对象设置", foreground=COLOR_TEXT,
            font=("Microsoft YaHei UI", 16, "bold"),
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(
            body, text="只填写当前模块需要的内容；将鼠标停在 ? 上可查看说明。",
            foreground=COLOR_MUTED,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 12))
        row += 1
        self.basic_section_heading = self._section_heading(body, row, "基本信息")
        row += 1
        self.row_category = self._labeled_row(
            body, row, "模块类别",
            lambda m: self._row_combo(
                m, self.category_var, self.category_choices,
                width=14, set_attr="category_combo",
            ),
            "切换模块用于普通识图；工作流全局用于工作流常驻检测；脚本全局用于脚本内检测；特殊模块为固定动作。",
        )
        self.category_combo.bind("<<ComboboxSelected>>", self._toggle_sections)
        row += 1
        self.row_name = self._labeled_row(
            body, row, "名称",
            lambda m: ttk.Entry(m, textvariable=self.name_var, width=24),
            "模块名称，脚本里插入后以此显示。特殊模块纯动作只需填名称，"
            "行为固定为「重新执行工作流」。",
        )
        row += 1
        self.row_image = self._labeled_row(
            body, row, "模板图片", self._image_picker_row,
            "“选择图片…”登记已有图片文件；“截图新建…”框选屏幕区域生成新模板图片，"
            "截图区域同时作为默认搜索区域。特殊模块纯动作不需要图片。",
        )
        row += 1
        self.row_region = self._labeled_row(
            body, row, "框选区域 (x,y,w,h)",
            lambda m: self._entry_button_row(
                m, readonly=True, textvariable=self.region_var,
                button_text="框选区域…", command=self._pick_region, expand=True,
            ),
            "识别时在屏幕上搜索的区域，留空 = 全屏搜索。",
        )
        row += 1
        self.detect_section_heading = self._section_heading(body, row, "识别设置")
        row += 1
        self.row_recognize = self._labeled_row(
            body, row, "识别方式",
            lambda m: self._row_combo(
                m, self.recognize_var, ("模板图片", "识别文字", "读取数字", "无需识图"),
                width=14, set_attr="recognize_combo",
            ),
            "模板图片：按图像匹配；识别文字：截取区域做 OCR；读取数字：把指定区域"
            "内的数字从左到右拼成整数，并由脚本行判断；无需识图：模块执行到时直接运行。",
        )
        self.recognize_combo.bind("<<ComboboxSelected>>", self._toggle_sections)
        row += 1
        self.row_expected_text = self._labeled_row(
            body, row, "期望文字",
            lambda m: ttk.Entry(m, textvariable=self.expected_text_var, width=24),
            "识别区域里应出现的文字，支持“包含 / 等于”匹配；留空 = 识别到任意文字即命中。",
        )
        row += 1
        self.row_match_mode = self._labeled_row(
            body, row, "匹配方式",
            lambda m: self._row_combo(m, self.match_mode_var, ("包含", "等于"), width=10),
            "包含：识别结果里出现期望文字即命中；等于：去掉首尾空白后整体相同（不区分大小写）。",
        )
        row += 1
        self.row_wait_text_absent = self._labeled_row(
            body, row, "等待目标消失",
            lambda m: dark_checkbutton(
                m, "直到区域内检测不到期望文字才完成", self.wait_text_absent_var,
            ),
            "模板图片和识别文字均可使用。勾选后，每次找到目标都会执行成功动作并重新识别；"
            "直到区域内检测不到目标才完成当前模块。F12 仍可紧急停止。",
        )
        row += 1
        self.row_threshold = self._labeled_row(
            body, row, "相似度 (0.1–1.0)",
            lambda m: ttk.Entry(m, textvariable=self.threshold_var, width=14),
            "图像匹配相似度阈值，越高越严格，越低越容易误识别。",
        )
        row += 1
        self.row_ignore_background = self._labeled_row(
            body, row, "忽略背景",
            lambda m: dark_checkbutton(m, "只识别字，忽略背景颜色", self.ignore_background_var),
            "开启后只按模板上的文字笔画匹配，背景颜色、纹理、高亮变化都不影响识别。"
            "适合按钮背景会变色/高亮/变灰的场景；背景无法自动识别时自动回退普通匹配。",
        )
        row += 1
        self.row_interval = self._labeled_row(
            body, row, "检测间隔",
            lambda m: ttk.Entry(m, textvariable=self.interval_var, width=14),
            "每两次识别之间的等待毫秒数，越小响应越快、越耗资源。",
        )
        row += 1
        self.row_start_delay = self._labeled_row(
            body, row, "开始识别前延时",
            lambda m: ttk.Entry(m, textvariable=self.start_delay_var, width=14),
            "仅脚本全局模块使用。脚本开始执行后先等待这段时间，再启动图片、文字或其他识别；支持 ms / s / min。",
        )
        row += 1
        self.row_fallback_module = self._labeled_row(
            body, row, "备用识别模块",
            lambda m: self._row_combo(
                m, self.fallback_module_key_var, tuple(self.fallback_module_keys), width=32,
                set_attr="fallback_module_combo",
            ),
            "主模块等待且尚未超时期间，每轮先识别主模块，再同时识别这里选择的备用图片或文字模块。主命中立即结束；备用命中后继续等待主模块。",
        )
        self.fallback_module_combo.bind("<<ComboboxSelected>>", self._toggle_sections)
        row += 1
        self.row_fallback_click = self._labeled_row(
            body, row, "备用命中后",
            lambda m: self._row_combo(
                m, self.fallback_on_match_var,
                tuple(FALLBACK_ON_MATCH_LABELS.values()), width=32,
                set_attr="fallback_on_match_combo",
            ),
            "可选择不点击或点击备用命中位置，并决定继续识别主模块还是直接退出主模块识别。备用持续存在时只处理一次，消失后再次出现才会再次处理。",
        )
        self.fallback_on_match_combo.bind("<<ComboboxSelected>>", self._toggle_sections)
        row += 1
        self.row_fallback_click_settings = self._labeled_row(
            body, row, "备用点击参数",
            lambda m: self._entry_pair_row(
                m, self.fallback_click_count_var, self.fallback_click_interval_var,
                "次数", "间隔 ms",
            ),
            "备用模块命中后连续点击的次数，以及两次点击之间的等待毫秒数。",
        )
        row += 1
        self.row_blocking = self._labeled_row(
            body, row, "阻塞识别",
            lambda m: dark_checkbutton(m, "启用", self.blocking_var),
            "开启：识别不到就一直等，直到识别成功才继续；"
            "关闭：识别不到直接跳过。",
        )
        row += 1
        self.row_hold = self._labeled_row(
            body, row, "持续超过",
            self._build_hold_control,
            "工作流全局和脚本全局模块使用。勾选后，命中状态持续达到设定时长才触发；"
            "不勾选则第一次识别命中就立即执行。",
        )
        row += 1
        self.row_delay = self._labeled_row(
            body, row, "延时",
            lambda m: ttk.Entry(m, textvariable=self.delay_var, width=14),
            "识别成功后、执行动作前的等待毫秒数。",
        )
        row += 1
        self.action_section_heading = self._section_heading(body, row, "成功后动作")
        row += 1
        self.row_after = self._labeled_row(
            body, row, "识别成功后执行",
            lambda m: self._row_combo(
                m, self.after_action_var, list(AFTER_ACTION_LABELS.values()),
                width=18, set_attr="after_action_combo",
            ),
            "识别成功后的行为：点击识别区域 / 点击自定义位置 / 成功后继续 / "
            "二次识别后点击。需要追加代码时，在下方启用附加代码段。",
        )
        self.after_action_combo.bind("<<ComboboxSelected>>", self._toggle_sections)
        row += 1
        self.row_button = self._labeled_row(
            body, row, "点击按钮",
            lambda m: self._row_combo(
                m, self.button_var, ("left", "right", "middle"), width=10,
            ),
            "点击使用的鼠标键：左键 / 右键 / 中键。",
        )
        row += 1
        self.row_click_count = self._labeled_row(
            body, row, "点击次数",
            lambda m: ttk.Entry(m, textvariable=self.click_count_var, width=10),
            "识别成功后在所选位置连续点击多少下，默认 1；"
            "点击识别区域、自定义位置和二次识别点击均使用该次数。",
        )
        row += 1
        self.row_ocr_offset = self._labeled_row(
            body, row, "文字点击偏移 (px)",
            self._build_ocr_offset_control,
            "以识别到的文字框中心为基准。分别填写向上、向下、向左、向右的像素；"
            "也可点“拖拽选取…”：在起点按住左键，拖到终点后松开，自动计算偏移。",
        )
        row += 1
        self.row_click_point = self._labeled_row(
            body, row, "点击位置 (x,y)",
            lambda m: self._entry_button_row(
                m, textvariable=self.click_point_var, width=14,
                button_text="幕布选取…", command=self.start_click_point_selection,
            ),
            "点击的屏幕坐标，可点“幕布选取…”在屏幕上选取。",
        )
        row += 1
        self.row_second_template = self._labeled_row(
            body, row, "二次识别模板",
            lambda m: self._row_combo(
                m, self.second_template_var,
                registered_template_options(str(obj.get("second_match_template", ""))),
            ),
            "识别成功后，再识别另一个已登记模板，两个都识别到才执行。",
        )
        row += 1
        self.row_second_region = None
        self.row_second_timeout = self._labeled_row(
            body, row, "二次识别超时",
            lambda m: ttk.Entry(m, textvariable=self.second_timeout_var, width=14),
            "等待二次识别的最大毫秒数；开启阻塞识别时无限等待。",
        )
        row += 1
        self.row_second_click_target = self._labeled_row(
            body, row, "二次识别后点击位置",
            lambda m: self._row_combo(
                m, self.second_click_target_var,
                list(SECOND_MATCH_CLICK_TARGET_LABELS.values()), width=18,
                set_attr="second_click_target_combo",
            ),
            "二次识别成功后，可点击第一次识别中心、第二次识别中心或自定义框选区域中心。",
        )
        self.second_click_target_combo.bind("<<ComboboxSelected>>", self._toggle_sections)
        row += 1
        self.row_second_click_region = self._labeled_row(
            body, row, "自定义点击区域 (x,y,w,h)",
            lambda m: self._entry_button_row(
                m, textvariable=self.second_click_region_var, width=14,
                button_text="框选…", command=self._pick_second_click_region,
            ),
            "仅选择“自定义框选区域”时使用；识别成功后点击该区域中心。",
        )
        row += 1
        self.segment_section_heading = self._section_heading(body, row, "附加代码段")
        row += 1
        self.row_run_code_after_action = self._labeled_row(
            body, row, "动作完成后再执行代码段",
            lambda m: dark_checkbutton(
                m, "启用", self.run_code_after_action_var,
                command=self._toggle_sections,
            ),
            "可选。先完成上面的点击、继续或二次识别动作，再依次执行代码段；"
            "代码段完成后才继续原脚本或工作流。",
        )
        row += 1
        self.segment_frame = self._build_segment_panel(body, row)
        row += 1
        self.timeout_section_heading = self._section_heading(body, row, "未识别超时")
        row += 1
        self.row_run_code_on_timeout = self._labeled_row(
            body, row, "超过时限未识别执行代码段",
            lambda m: dark_checkbutton(
                m, "启用", self.run_code_on_timeout_var,
                command=self._toggle_sections,
            ),
            "与成功后代码段完全独立。连续未识别达到下方时限后，执行超时代码段。",
        )
        row += 1
        self.row_not_found_timeout = self._labeled_row(
            body, row, "未识别时限",
            lambda m: ttk.Entry(m, textvariable=self.not_found_timeout_var, width=14),
            "切换模块达到该时限后向当前脚本行返回失败；若启用下方代码段，会先执行代码段。",
        )
        row += 1
        self.timeout_segment_frame = self._build_segment_panel(
            body, row, segment_attr="timeout_segment",
            listbox_attr="timeout_segment_listbox", title="未识别超时后执行的代码段",
        )
        row += 1
        ttk.Separator(body).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        row += 1
        ttk.Label(
            body,
            text="保存后所有引用该模块的脚本自动生效。",
            foreground=COLOR_MUTED, wraplength=560,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))
        row += 1
        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="保存模块", command=self.save).pack(side="right", padx=8)
        self._toggle_sections()
        fit_window_to_content(
            self, parent, minimum_width=680, minimum_height=600,
            content_width=body.winfo_reqwidth() + self._scrollbar.winfo_reqwidth(),
            content_height=body.winfo_reqheight() + 4,
            align_top=True,
        )

    def _build_hold_control(self, master):
        frame = ttk.Frame(master)
        dark_checkbutton(
            frame, "启用持续延时", self.hold_enabled_var,
            command=self._toggle_hold_control,
        ).pack(side="left")
        self.hold_entry = ttk.Entry(frame, textvariable=self.hold_var, width=14)
        self.hold_entry.pack(side="left", padx=(10, 0))
        return frame

    def _build_ocr_offset_control(self, master):
        frame = ttk.Frame(master)
        for label, variable in (
            ("上", self.ocr_offset_up_var), ("下", self.ocr_offset_down_var),
            ("左", self.ocr_offset_left_var), ("右", self.ocr_offset_right_var),
        ):
            ttk.Label(frame, text=label).pack(side="left", padx=(8 if frame.winfo_children() else 0, 3))
            ttk.Entry(frame, textvariable=variable, width=6).pack(side="left")
        ttk.Button(
            frame, text="拖拽选取…", command=self._pick_ocr_offset,
        ).pack(side="left", padx=(10, 0))
        return frame

    def _pick_ocr_offset(self):
        self.picker = ScreenOffsetPicker(
            self, self.master, self._apply_ocr_offset,
            hidden_windows=self._ancestors_to_hide(),
            tip_text="在起点按住鼠标左键，不要松开；拖到终点后松开，自动记录两点偏移；Esc 取消",
        )
        self.picker.start()

    def _apply_ocr_offset(self, start_x, start_y, end_x, end_y):
        dx, dy = int(end_x) - int(start_x), int(end_y) - int(start_y)
        self.ocr_offset_left_var.set(str(max(0, -dx)))
        self.ocr_offset_right_var.set(str(max(0, dx)))
        self.ocr_offset_up_var.set(str(max(0, -dy)))
        self.ocr_offset_down_var.set(str(max(0, dy)))

    def _toggle_hold_control(self):
        entry = getattr(self, "hold_entry", None)
        if entry is not None:
            entry.configure(state="normal" if self.hold_enabled_var.get() else "disabled")

    def _labeled_row(self, body, row, label, control_factory, tip: str = ""):
        """字段行：label 在左、控件在右。

        control_factory(frame) 以本行 frame 为 master 创建控件 —— 控件是行的
        真实子控件。不能用 grid(in_=frame) 把 body 的子控件放进行的网格：
        那种布局下控件的窗口没有落在声明的位置，真实点击进不了输入框、焦点
        也进不去（v1.82.6，用户反馈「只有分类和模板图片能改」）。v1.82.1 曾
        因直接 grid 到 body 的 (0,1) 与分类下拉重叠，故必须由工厂创建。
        """
        frame = ttk.Frame(body)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        frame.columnconfigure(0, minsize=180)
        frame.columnconfigure(1, weight=1)
        name_box = ttk.Frame(frame)
        name_box.grid(row=0, column=0, sticky="w", padx=(0, 14))
        ttk.Label(name_box, text=label).pack(side="left")
        if tip:
            help_badge = tk.Label(
                name_box, text="?", width=2, cursor="hand2",
                background=COLOR_BLUE_SELECTION, foreground="#EAF4FF",
                font=("Microsoft YaHei UI", 9, "bold"), relief="flat",
            )
            help_badge.pack(side="left", padx=(6, 0))
            Tooltip(help_badge, tip, anchor=frame)
        control = control_factory(frame)
        control.grid(row=0, column=1, sticky="ew")
        return frame

    @staticmethod
    def _section_heading(body, row: int, text: str):
        frame = ttk.Frame(body)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(
            frame, text=text, foreground="#79BFFF",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left")
        ttk.Separator(frame).pack(side="left", fill="x", expand=True, padx=(10, 0))
        return frame

    def _image_picker_row(self, frame):
        row = ttk.Frame(frame)
        row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=self.image_var, state="readonly").grid(
            row=0, column=0, sticky="ew",
        )
        ttk.Button(row, text="选择图片…", command=self._choose_image).grid(
            row=0, column=1, padx=(8, 0),
        )
        ttk.Button(row, text="截图新建…", command=self._capture).grid(
            row=0, column=2, padx=(8, 0),
        )
        return row

    @staticmethod
    def _default_name_for_image(value: str | Path) -> str:
        """Use the final path component without its extension as module name."""
        return Path(str(value).replace("\\", "/")).stem

    def _set_image_and_default_name(self, value: str | Path) -> None:
        display_value = display_path(value)
        current_name = self.name_var.get().strip()
        previous_auto_name = getattr(self, "_auto_name_value", "")
        self.image_var.set(display_value)
        if not current_name or current_name == previous_auto_name:
            default_name = self._default_name_for_image(display_value)
            self.name_var.set(default_name)
            self._auto_name_value = default_name

    def _row_combo(self, frame, var, values, width: int | None = None,
                   set_attr: str | None = None):
        """行内只读下拉框（真实子控件）。"""
        combo = ttk.Combobox(
            frame, textvariable=var, values=list(values),
            state="readonly", **({"width": width} if width else {}),
        )
        combo.grid(row=0, column=1, sticky="w")
        if set_attr:
            setattr(self, set_attr, combo)
        return combo

    def _entry_button_row(self, frame, *, readonly=False, textvariable,
                          width=14, button_text="", command=None,
                          expand=False):
        """行内输入框 + 按钮（真实子控件）；expand 时输入框撑满本行。"""
        entry = ttk.Entry(
            frame, textvariable=textvariable,
            state="readonly" if readonly else "normal", width=width,
        )
        entry.grid(row=0, column=1, sticky="we" if expand else "w")
        if button_text:
            ttk.Button(frame, text=button_text, command=command).grid(
                row=0, column=2, padx=(8, 0), sticky="w",
            )
        return entry

    def _entry_pair_row(self, frame, first_var, second_var, first_label, second_label):
        """Two compact numeric entries used for count plus interval settings."""
        row = ttk.Frame(frame)
        ttk.Entry(row, textvariable=first_var, width=8).grid(row=0, column=0, sticky="w")
        ttk.Label(row, text=first_label).grid(row=0, column=1, padx=(6, 12), sticky="w")
        ttk.Entry(row, textvariable=second_var, width=10).grid(row=0, column=2, sticky="w")
        ttk.Label(row, text=second_label).grid(row=0, column=3, padx=(6, 0), sticky="w")
        return row

    def _fallback_on_match_value(self) -> str:
        variable = getattr(self, "fallback_on_match_var", None)
        if variable is not None:
            value = FALLBACK_ON_MATCH_VALUES.get(variable.get())
            if value:
                return value
        return "click_continue" if bool(self.fallback_click_var.get()) else "continue"

    def _toggle_sections(self, _event=None):
        after = self.after_action_var.get()
        category = self.category_var.get()
        # 特殊模块 = 纯动作（无图片）：只保留 分类 + 名称，隐藏全部检测/行为行。
        pure = category == "特殊模块"
        text_mode = not pure and self.recognize_var.get() == "识别文字"
        number_mode = not pure and self.recognize_var.get() == "读取数字"
        direct_mode = not pure and self.recognize_var.get() == "无需识图"
        direct_global = direct_mode and category in (
            "工作流全局模块", "脚本全局模块",
        )
        if hasattr(self, "after_action_combo"):
            self.after_action_combo.configure(values=(
                ("点击自定义位置", "成功后继续")
                if direct_mode else ("成功后继续",)
                if number_mode else tuple(AFTER_ACTION_LABELS.values())
            ))
        if number_mode and after != "成功后继续":
            self.after_action_var.set("成功后继续")
            after = "成功后继续"
        elif direct_mode and after not in ("点击自定义位置", "成功后继续"):
            self.after_action_var.set("成功后继续")
            after = "成功后继续"
        if text_mode and after == "二次识别后点击":
            # 识别文字方式没有二次图片匹配，强制回落到点击识别区域。
            self.after_action_var.set("点击识别区域")
            after = "点击识别区域"
        # 名称行对普通模块和特殊模块（纯动作，只保留 分类+名称）都可见；
        # 传 pure 会把普通模块的名称行也 grid_remove 掉（v1.82.1 回归）。
        self._set_row(self.row_name, True)
        self._set_row(self.row_image, not pure and not text_mode and not number_mode and not direct_mode)
        self._set_row(self.row_region, not pure and not direct_mode)
        self._set_row(self.detect_section_heading, not pure)
        self._set_row(self.row_recognize, not pure)
        self._set_row(self.row_expected_text, text_mode)
        self._set_row(self.row_match_mode, text_mode)
        self._set_row(self.row_wait_text_absent, not pure and not number_mode and not direct_mode)
        self._set_row(self.row_threshold, not pure and not text_mode and not number_mode and not direct_mode)
        self._set_row(self.row_ignore_background, not pure and not text_mode and not number_mode and not direct_mode)
        self._set_row(self.row_interval, not pure and not direct_mode)
        self._set_row(self.row_start_delay, category == "脚本全局模块")
        fallback_supported = not pure and not number_mode and not direct_mode
        self._set_row(self.row_fallback_module, fallback_supported)
        self._set_row(
            self.row_fallback_click,
            fallback_supported and bool(
                getattr(self, "fallback_module_keys", {}).get(
                    self.fallback_module_key_var.get(),
                    self.fallback_module_key_var.get().strip(),
                )
            ),
        )
        self._set_row(
            getattr(self, "row_fallback_click_settings", None),
            fallback_supported and self._fallback_on_match_value().startswith("click_") and bool(
                getattr(self, "fallback_module_keys", {}).get(
                    self.fallback_module_key_var.get(), self.fallback_module_key_var.get().strip(),
                )
            ),
        )
        self._set_row(self.row_blocking, not pure and not direct_mode)
        self._set_row(self.row_delay, not pure and not number_mode and not direct_global)
        self._set_row(self.action_section_heading, not pure and not number_mode and not direct_global)
        self._set_row(self.row_after, not pure and not number_mode and not direct_global)
        self._set_row(
            self.row_hold,
            not direct_mode and category in ("工作流全局模块", "脚本全局模块"),
        )
        self._toggle_hold_control()
        self._set_row(
            self.row_button,
            not pure and after in ("点击识别区域", "点击自定义位置", "二次识别后点击"),
        )
        self._set_row(
            self.row_click_count,
            not pure and after in ("点击识别区域", "点击自定义位置", "二次识别后点击"),
        )
        self._set_row(
            self.row_ocr_offset,
            text_mode and after == "点击识别区域",
        )
        self._set_row(self.row_click_point, not pure and after == "点击自定义位置")
        show_second = not pure and not text_mode and after == "二次识别后点击"
        self._set_row(self.row_second_template, show_second)
        self._set_row(self.row_second_timeout, show_second)
        self._set_row(self.row_second_click_target, show_second)
        self._set_row(
            self.row_second_click_region,
            show_second and self.second_click_target_var.get() == "自定义框选区域",
        )
        self._set_row(self.segment_section_heading, not pure and not number_mode and not direct_global)
        self._set_row(self.row_run_code_after_action, not pure and not number_mode and not direct_global)
        self._set_row(
            self.segment_frame,
            not pure and not number_mode and not direct_global
            and bool(self.run_code_after_action_var.get()),
        )
        self._set_row(self.timeout_section_heading, not pure and (not direct_mode or direct_global))
        self._set_row(
            self.row_run_code_on_timeout,
            not pure and not number_mode and (not direct_mode or direct_global),
        )
        timeout_enabled = (
            not pure and not number_mode and (not direct_mode or direct_global)
            and bool(self.run_code_on_timeout_var.get())
        )
        switch_failure_timeout = (
            category == "切换模块" and not direct_mode
            and not bool(self.blocking_var.get())
        )
        self._set_row(
            self.row_not_found_timeout, timeout_enabled or switch_failure_timeout,
        )
        self._set_row(self.timeout_segment_frame, timeout_enabled)
        self._resize_for_content()

    def _set_row(self, row, visible: bool):
        if row is None:
            return
        if visible:
            row.grid()
        else:
            row.grid_remove()

    def _resize_for_content(self):
        # 内容在 Canvas 里滚动，窗口自身 reqsize 不再反映内容尺寸：从 body 的
        # 实际需求尺寸计算窗口大小；超出屏幕时保持窗口不变（内容可滚动到达）。
        try:
            self.update_idletasks()
            width = max(
                self.winfo_width(),
                self.body.winfo_reqwidth() + self._scrollbar.winfo_reqwidth(),
            )
            height = max(
                self.winfo_height(),
                min(self.body.winfo_reqheight() + 4, self.winfo_screenheight() - 80),
            )
            self.geometry(f"{width}x{height}")
        except tk.TclError:
            pass

    def _scroll_form(self, event):
        """滚轮滚动整个表单；列表自身已有滚轮绑定，跳过避免双重滚动。"""
        if not event.delta or isinstance(event.widget, tk.Listbox):
            return
        self._canvas.yview_scroll(-int(event.delta / 120), "units")

    def _build_segment_panel(self, body, row, *, segment_attr="segment",
                             listbox_attr="segment_listbox",
                             title="主动作完成后执行的代码段"):
        frame = ttk.LabelFrame(body, text=title)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        listbox = tk.Listbox(
            list_frame, background=COLOR_SURFACE, foreground=COLOR_TEXT,
            selectbackground=COLOR_BLUE_SELECTION, height=5,
            font=("Microsoft YaHei UI", 10), relief="flat", borderwidth=0,
            selectmode="extended", exportselection=False,
        )
        setattr(self, listbox_attr, listbox)
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        listbox.bind(
            "<Double-1>",
            lambda _event: self._edit_segment_item(segment_attr, listbox_attr),
        )
        listbox.bind(
            "<Control-a>",
            lambda _event: self._select_all_segment_items(listbox_attr),
        )
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=(6, 10))
        ttk.Button(
            buttons, text="添加…",
            command=lambda: self._add_segment_item(segment_attr, listbox_attr),
        ).pack(side="left")
        ttk.Button(
            buttons, text="编辑",
            command=lambda: self._edit_segment_item(segment_attr, listbox_attr),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="移除",
            command=lambda: self._remove_segment_item(segment_attr, listbox_attr),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="上移",
            command=lambda: self._move_segment_item(-1, segment_attr, listbox_attr),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="下移",
            command=lambda: self._move_segment_item(1, segment_attr, listbox_attr),
        ).pack(side="left", padx=(8, 0))
        self._reload_segment_list(segment_attr, listbox_attr)
        return frame

    def _reload_segment_list(self, segment_attr="segment", listbox_attr="segment_listbox"):
        segment = getattr(self, segment_attr)
        listbox = getattr(self, listbox_attr)
        listbox.delete(0, "end")
        for index, item in enumerate(segment):
            listbox.insert("end", f"{index + 1}. {segment_row_label(item)}")
            if segment_action_is_blocking(item):
                listbox.itemconfigure(index, foreground="#F2B84B")

    def _segment_selection(self, listbox_attr="segment_listbox"):
        selection = getattr(self, listbox_attr).curselection()
        return selection[0] if selection else None

    def _select_all_segment_items(self, listbox_attr="segment_listbox"):
        """Select every row in one of the module's internal action segments."""
        listbox = getattr(self, listbox_attr)
        listbox.selection_set(0, "end")
        return "break"

    def _add_segment_item(self, segment_attr="segment", listbox_attr="segment_listbox"):
        menu = tk.Menu(self, tearoff=0)
        target = (segment_attr, listbox_attr)
        menu.add_command(label="延时", command=lambda: self._add_segment_delay(*target))
        menu.add_command(label="键盘", command=lambda: self._add_segment_dialog(KeyActionDialog, *target))
        menu.add_command(label="文本", command=lambda: self._add_segment_dialog(TextActionDialog, *target))
        menu.add_command(label="点击", command=lambda: self._add_segment_dialog(ClickDialog, *target))
        menu.add_command(label="连续点击", command=lambda: self._add_segment_dialog(RepeatClickDialog, *target))
        menu.add_command(label="移动", command=lambda: self._add_segment_dialog(MouseMoveDialog, *target))
        menu.add_command(
            label="识别模块…", command=lambda: self._add_segment_module_ref(*target),
            state="normal" if self.segment_depth < SEGMENT_DEPTH_LIMIT else "disabled",
        )
        menu.add_command(label="引用脚本", command=lambda: self._add_segment_dialog(ScriptRefDialog, *target))
        menu.add_command(label="打开软件", command=lambda: self._add_segment_dialog(OpenAppDialog, *target))
        menu.add_command(label="关闭软件", command=lambda: self._add_segment_dialog(CloseAppDialog, *target))
        menu.add_command(label="提醒", command=lambda: self._add_segment_notice(*target))
        menu.add_command(label="前置指定窗口…", command=lambda: self._add_segment_activate_window(*target))
        menu.add_separator()
        menu.add_command(
            label="跳转到当前脚本最后一行",
            command=lambda: self._add_segment_jump_current_script_last(*target),
        )
        menu.add_command(
            label=END_CURRENT_SCRIPT_LABEL,
            command=lambda: self._add_segment_end_current_script(*target),
        )
        if self.segment_depth >= SEGMENT_DEPTH_LIMIT:
            menu.add_command(label="（代码段嵌套最多 8 层）", state="disabled")
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _append_segment(self, action: dict, segment_attr="segment",
                        listbox_attr="segment_listbox"):
        ensure_action_ids([action])
        getattr(self, segment_attr).append(action)
        self._reload_segment_list(segment_attr, listbox_attr)

    def _add_segment_delay(self, segment_attr="segment", listbox_attr="segment_listbox"):
        value = DurationDialog(self, "添加延时", "延时时间：", 100).show()
        if value is not None:
            self._append_segment({"type": "delay", "ms": value}, segment_attr, listbox_attr)

    def _add_segment_notice(self, segment_attr="segment", listbox_attr="segment_listbox"):
        text = simpledialog.askstring("添加提醒", "提示文字：", parent=self, initialvalue="")
        if text is not None and text.strip():
            self._append_segment({"type": "notice", "text": text.strip()}, segment_attr, listbox_attr)

    def _add_segment_end_current_script(self, segment_attr="segment", listbox_attr="segment_listbox"):
        self._append_segment({"type": "end_current_script"}, segment_attr, listbox_attr)

    def _add_segment_jump_current_script_last(self, segment_attr="segment",
                                               listbox_attr="segment_listbox"):
        self._append_segment({"type": "jump_current_script_last"}, segment_attr, listbox_attr)

    def _add_segment_activate_window(self, segment_attr="segment", listbox_attr="segment_listbox"):
        selected = WindowPicker(self).show()
        if selected:
            self._append_segment({
                "type": "activate_window",
                "window": {
                    "title": selected.title,
                    "class_name": selected.class_name,
                    "process_path": selected.process_path,
                },
            }, segment_attr, listbox_attr)

    def _add_segment_dialog(self, dialog_class, segment_attr="segment", listbox_attr="segment_listbox"):
        result = dialog_class(self, None).show()
        if result is not None:
            self._append_segment(result, segment_attr, listbox_attr)

    def _add_segment_module_ref(self, segment_attr="segment", listbox_attr="segment_listbox"):
        result = ModulePickerDialog(
            self, nested=True, segment_depth=self.segment_depth + 1,
        ).show()
        if result is not None:
            self._append_segment(result, segment_attr, listbox_attr)

    def _edit_segment_item(self, segment_attr="segment", listbox_attr="segment_listbox"):
        index = self._segment_selection(listbox_attr)
        if index is None:
            return
        updated = edit_action(
            self, getattr(self, segment_attr)[index], all_actions=getattr(self, segment_attr),
            segment_depth=self.segment_depth + 1,
        )
        if updated is not None:
            getattr(self, segment_attr)[index] = updated
            self._reload_segment_list(segment_attr, listbox_attr)

    def _remove_segment_item(self, segment_attr="segment", listbox_attr="segment_listbox"):
        selection = getattr(self, listbox_attr).curselection()
        if not selection:
            return
        segment = getattr(self, segment_attr)
        for index in sorted((int(item) for item in selection), reverse=True):
            del segment[index]
        self._reload_segment_list(segment_attr, listbox_attr)

    def _move_segment_item(self, delta: int, segment_attr="segment", listbox_attr="segment_listbox"):
        index = self._segment_selection(listbox_attr)
        if index is None:
            return
        target = index + delta
        segment = getattr(self, segment_attr)
        if not 0 <= target < len(segment):
            return
        item = segment.pop(index)
        segment.insert(target, item)
        self._reload_segment_list(segment_attr, listbox_attr)
        getattr(self, listbox_attr).selection_set(target)

    def _choose_image(self):
        self.images_dir.mkdir(parents=True, exist_ok=True)
        path = filedialog.askopenfilename(
            parent=self, title="选择模板图片",
            initialdir=str(self.images_dir),
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")],
        )
        if path:
            self._set_image_and_default_name(path)
            self._toggle_sections()

    def _ancestors_to_hide(self):
        """从管理器对话框向上到应用主窗口的所有窗口。

        框选区域 / 截图新建时整条窗口链都要隐藏，否则主窗口被幕布之外的其他
        软件窗口遮挡无法看清要框选的区域，截图也会截进本程序自己的窗口。
        """
        windows = []
        seen = set()
        window = self.master
        while window is not None and len(windows) < 20:
            try:
                identity = id(window)
            except Exception:
                break
            if identity in seen:
                # 防御：master 链出现环（或 Mock）时立即停止，避免无限循环吃内存。
                break
            seen.add(identity)
            try:
                parent = window.master
            except (AttributeError, tk.TclError):
                break
            if parent is None:
                break
            window = parent
            windows.append(window)
            try:
                if window.winfo_class() == "Tk":
                    break
            except tk.TclError:
                break
        return windows

    def _capture(self):
        """框选屏幕区域截图存为新模板图片，并同时填入图片与区域两项。"""

        def on_result(region):
            try:
                self.images_dir.mkdir(parents=True, exist_ok=True)
                screen, _origin = capture_bgr(tuple(int(part) for part in region))
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
                path = self.images_dir / f"template_{stamp}.png"
                Image.fromarray(screen[:, :, ::-1]).save(path)
            except Exception as exc:
                show_floating_notice(self, "截图失败", str(exc))
                return
            self._set_image_and_default_name(path)
            self.region_var.set(",".join(map(str, region)))
            self._toggle_sections()

        self.picker = ScreenRegionPicker(
            self, self.master, on_result,
            hidden_windows=self._ancestors_to_hide(),
            tip_text="按住鼠标左键，从左上角向右下角拖动框选要截图成模板的区域；松开完成，Esc 取消",
        )
        self.picker.start()

    def _pick_region(self):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: self.region_var.set(",".join(map(str, region))),
            hidden_windows=self._ancestors_to_hide(),
            tip_text="按住鼠标左键，从左上角向右下角拖动框选该模板的默认搜索区域；松开完成，Esc 取消",
        )
        self.picker.start()

    def _pick_second_click_region(self):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: self.second_click_region_var.set(",".join(map(str, region))),
            hidden_windows=self._ancestors_to_hide(),
            tip_text="框选二次识别成功后要点击的区域；松开后将点击该区域中心，Esc 取消",
        )
        self.picker.start()

    def start_click_point_selection(self):
        if self.point_overlay is not None:
            return
        main = self.master
        try:
            self.main_previous_state = str(main.state())
            self.grab_release()
            self.withdraw()
            main.withdraw()
            main.update_idletasks()
            main.after(100, self._show_click_point_curtain)
        except Exception as exc:
            self._close_click_point_selection()
            show_floating_notice(self, "无法选取点击位置", str(exc))

    def _show_click_point_curtain(self):
        try:
            screen, origin = capture_bgr()
            image = Image.fromarray(screen[:, :, ::-1])
            image = ImageEnhance.Brightness(image).enhance(0.62)
            overlay = tk.Toplevel(self.master)
            self.point_overlay = overlay
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.configure(background="#000000", cursor="crosshair")
            width, height = image.size
            left, top = int(origin[0]), int(origin[1])
            overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
            self.point_screenshot = ImageTk.PhotoImage(image, master=overlay)
            canvas = tk.Canvas(overlay, width=width, height=height, highlightthickness=0, cursor="crosshair")
            canvas.pack(fill="both", expand=True)
            canvas.create_image(0, 0, image=self.point_screenshot, anchor="nw")
            canvas.create_text(
                width // 2, 34,
                text="点击要执行操作的位置；只记录坐标，不会点击下方窗口；Esc 取消",
                fill="#FFFFFF", font=("Microsoft YaHei UI", 13, "bold"),
            )
            canvas.bind("<Button-1>", self._select_click_point)
            overlay.bind("<Escape>", lambda _event: self._close_click_point_selection())
            overlay.update_idletasks()
            overlay.lift()
            overlay.focus_force()
            overlay.grab_set()
        except Exception as exc:
            self._close_click_point_selection()
            show_floating_notice(self, "无法截取幕布", str(exc))

    def _select_click_point(self, event):
        self.click_point_var.set(f"{int(event.x_root)},{int(event.y_root)}")
        self._close_click_point_selection()

    def _close_click_point_selection(self):
        overlay = self.point_overlay
        self.point_overlay = None
        self.point_screenshot = None
        if overlay is not None:
            try:
                overlay.grab_release()
                overlay.destroy()
            except tk.TclError:
                pass
        self._restore_after_overlay()
        try:
            self.after(30, self._restore_after_overlay)
        except tk.TclError:
            pass

    def _restore_after_overlay(self):
        restore_modal_after_overlay(self, self.master, self.main_previous_state)

    def save(self):
        if self.category_var.get() == "特殊模块":
            # 特殊模块 = 纯动作（无图片）：名称做 key，行为固定为「重新执行工作流」。
            name = self.name_var.get().strip()
            if not name:
                show_floating_notice(self, "缺少名称", "特殊模块纯动作需要填写名称。")
                return
            self.result = (
                self.old_key, name,
                {"category": "special", "name": name, "pure_action": True},
            )
            self.destroy()
            return
        text_mode = self.recognize_var.get() == "识别文字"
        number_mode = self.recognize_var.get() == "读取数字"
        direct_mode = self.recognize_var.get() == "无需识图"
        direct_global = direct_mode and self.category_var.get() in (
            "工作流全局模块", "脚本全局模块",
        )
        template_key = self.image_var.get().strip()
        if number_mode and self.category_var.get() != "切换模块":
            show_floating_notice(self, "类别不适用", "读取数字模块只能保存为切换模块。")
            return
        if not text_mode and not number_mode and not direct_mode and not template_key:
            show_floating_notice(self, "缺少模板图片", "请先“选择图片…”或“截图新建…”。")
            return
        region: list[int] = []
        if not text_mode and not direct_mode and not self.region_var.get().strip():
            # 模板图片和数字读取必须指定区域；识别文字方式留空表示全屏。
            show_floating_notice(self, "缺少框选区域", "请先“框选区域…”。")
            return
        if self.region_var.get().strip():
            parts = self.region_var.get().split(",")
            if len(parts) != 4:
                show_floating_notice(self, "缺少框选区域", "请先“框选区域…”。")
                return
            try:
                region = [int(part) for part in parts]
            except ValueError:
                show_floating_notice(
                    self, "区域格式错误", f"“{self.region_var.get()}”不是有效的 x,y,w,h 数字。",
                )
                return
            if region[2] <= 0 or region[3] <= 0:
                show_floating_notice(self, "区域无效", "框选区域的宽高必须大于 0。")
                return
        expected_text = self.expected_text_var.get().strip()
        match_mode = "equals" if self.match_mode_var.get() == "等于" else "contains"
        threshold = 0.85
        if not text_mode and not number_mode and not direct_mode:
            try:
                threshold = float(self.threshold_var.get())
            except ValueError:
                show_floating_notice(self, "相似度格式错误", "相似度必须是数字，例如 0.85。")
                return
            if not 0.1 <= threshold <= 1:
                show_floating_notice(self, "相似度无效", "相似度必须在 0.1 到 1.0 之间。")
                return
        try:
            interval = max(50, int(self.interval_var.get()))
        except ValueError:
            show_floating_notice(self, "检测间隔格式错误", "检测间隔必须是不小于 50 的整数（毫秒）。")
            return
        try:
            start_delay = max(0, min(86400000, int(self.start_delay_var.get())))
        except ValueError:
            show_floating_notice(self, "开始识别前延时格式错误", "延时必须是大于等于 0 的时间。")
            return
        try:
            delay = max(0, int(self.delay_var.get()))
            hold = max(0, int(self.hold_var.get()))
            click_count = max(1, min(9999, int(self.click_count_var.get())))
            fallback_count_var = getattr(self, "fallback_click_count_var", None)
            fallback_interval_var = getattr(self, "fallback_click_interval_var", None)
            fallback_click_count = max(1, min(9999, int(fallback_count_var.get()) if fallback_count_var else 1))
            fallback_click_interval = max(0, min(60000, int(fallback_interval_var.get()) if fallback_interval_var else 100))
        except ValueError:
            show_floating_notice(
                self, "数字格式错误",
                "延时和持续时间必须是大于等于 0 的整数；点击次数必须是大于等于 1 的整数。",
            )
            return
        after_value = AFTER_ACTION_VALUES.get(self.after_action_var.get(), "click_match")
        if direct_mode and after_value not in ("click_custom", "continue"):
            after_value = "continue"
        try:
            ocr_offsets = {
                "ocr_offset_up": max(0, int(self.ocr_offset_up_var.get())),
                "ocr_offset_down": max(0, int(self.ocr_offset_down_var.get())),
                "ocr_offset_left": max(0, int(self.ocr_offset_left_var.get())),
                "ocr_offset_right": max(0, int(self.ocr_offset_right_var.get())),
            }
        except ValueError:
            show_floating_notice(
                self, "文字点击偏移格式错误",
                "向上、向下、向左、向右偏移必须是大于等于 0 的整数像素。",
            )
            return
        click_point: list[int] = []
        if after_value == "click_custom":
            click_point = self._parse_point(
                self.click_point_var.get(), "点击位置",
                "请输入自定义点击坐标 x,y（逗号分隔），或点“幕布选取…”。",
            )
            if click_point is None:
                return
        second_template = ""
        second_region: list[int] = []
        second_timeout = 3000
        second_click_target = "second"
        second_click_region: list[int] = []
        if after_value == "second_match":
            second_template = self.second_template_var.get().strip()
            if not second_template:
                show_floating_notice(self, "缺少二次识别模板", "请选择二次识别要识别的模板。")
                return
            try:
                second_timeout = max(0, int(self.second_timeout_var.get()))
            except ValueError:
                show_floating_notice(
                    self, "二次识别超时格式错误",
                    "二次识别超时必须是大于等于 0 的整数（毫秒）。",
                )
                return
            second_click_target = SECOND_MATCH_CLICK_TARGET_VALUES.get(
                self.second_click_target_var.get(), "second",
            )
            if second_click_target == "custom_region":
                second_click_region = self._parse_region_or_empty(
                    self.second_click_region_var.get(),
                    label="自定义点击区域", empty_hint="必须框选一个区域",
                )
                if second_click_region is None:
                    return
                if not second_click_region:
                    show_floating_notice(
                        self, "缺少自定义点击区域",
                        "请选择“框选…”设置二次识别成功后要点击的区域。",
                    )
                    return
        run_code_after_action = (
            not number_mode and not direct_global and bool(self.run_code_after_action_var.get())
        )
        if run_code_after_action:
            if not self.segment:
                show_floating_notice(
                    self, "代码段为空",
                    "已启用“动作完成后再执行代码段”，请至少添加一个动作。",
                )
                return
            ensure_action_ids(self.segment)
        run_code_on_timeout = (
            not number_mode and (not direct_mode or direct_global)
            and bool(self.run_code_on_timeout_var.get())
        )
        try:
            not_found_timeout = max(0, int(self.not_found_timeout_var.get()))
        except ValueError:
            show_floating_notice(
                self, "未识别时限格式错误",
                "未识别时限必须是大于等于 0 的整数（毫秒）。",
            )
            return
        if run_code_on_timeout:
            if not self.timeout_segment:
                show_floating_notice(
                    self, "超时代码段为空",
                    "已启用“超过时限未识别执行代码段”，请至少添加一个动作。",
                )
                return
            ensure_action_ids(self.timeout_segment)
        name = self.name_var.get().strip() or (
            "识别文字" if text_mode else
            "读取数字" if number_mode else
            "无需识图" if direct_mode else self._default_name_for_image(template_key)
        )
        module_key = self.old_key or f"module:{uuid.uuid4().hex}"
        module_dict = {
            "category": {
                "切换模块": "switch",
                "工作流全局模块": "workflow_global",
                "脚本全局模块": "script_global",
            }.get(self.category_var.get(), "special"),
            "enabled": getattr(self, "module_enabled", True),
            "name": name,
            "template": "" if number_mode else template_key,
            "region": region,
            "threshold": threshold,
            "interval_ms": interval,
            "start_delay_ms": start_delay if self.category_var.get() == "脚本全局模块" else 0,
            "fallback_module_key": (
                getattr(self, "fallback_module_keys", {}).get(
                    self.fallback_module_key_var.get(), self.fallback_module_key_var.get().strip(),
                )
                if not number_mode and not direct_mode else ""
            ),
            "fallback_on_match": self._fallback_on_match_value(),
            "fallback_click": self._fallback_on_match_value().startswith("click_"),
            "fallback_click_count": fallback_click_count,
            "fallback_click_interval_ms": fallback_click_interval,
            "ignore_background": bool(self.ignore_background_var.get()),
            "blocking": False if direct_mode else bool(self.blocking_var.get()),
            "hold_enabled": bool(self.hold_enabled_var.get()),
            "hold_ms": hold,
            "delay_ms": 0 if number_mode else delay,
            "after_action": "continue" if number_mode else after_value,
            "run_code_after_action": run_code_after_action,
            "click_point": click_point,
            **ocr_offsets,
            "button": self.button_var.get()
            if self.button_var.get() in ("left", "right", "middle") else "left",
            "click_count": click_count,
            "second_match_template": second_template,
            "second_match_region": second_region,
            "second_match_timeout_ms": second_timeout,
            "second_match_click_target": second_click_target,
            "second_match_click_region": second_click_region,
            "on_success_actions": [] if number_mode else self.segment,
            "run_code_on_timeout": run_code_on_timeout,
            "not_found_timeout_ms": not_found_timeout,
            "on_timeout_actions": [] if number_mode else self.timeout_segment,
            "wait_text_absent": False if direct_mode or number_mode else bool(self.wait_text_absent_var.get()),
        }
        if text_mode:
            module_dict["recognize"] = "text"
            module_dict["expected_text"] = expected_text
            module_dict["match_mode"] = match_mode
        elif number_mode:
            module_dict["recognize"] = "number"
        elif direct_mode:
            module_dict["recognize"] = "none"
        self.result = (self.old_key, module_key, module_dict)
        self.destroy()

    def _parse_point(self, text: str, label: str, hint: str) -> list[int] | None:
        text = text.strip()
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 2:
            show_floating_notice(self, f"缺少{label}", hint)
            return None
        try:
            return [int(part) for part in parts]
        except ValueError:
            show_floating_notice(self, f"{label}格式错误", f"“{text}”不是有效的 x,y 数字。")
            return None

    def _parse_region_or_empty(self, text: str, label: str = "二次识别区域",
                               empty_hint: str = "留空表示全屏") -> list[int] | None:
        text = text.strip()
        if not text:
            return []
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 4:
            show_floating_notice(
                self, f"{label}格式错误",
                f"“{label}”需要 x,y,w,h 四个数字（{empty_hint}）。",
            )
            return None
        try:
            region = [int(part) for part in parts]
        except ValueError:
            show_floating_notice(self, f"{label}格式错误", f"“{text}”不是有效的 x,y,w,h 数字。")
            return None
        if region[2] <= 0 or region[3] <= 0:
            show_floating_notice(
                self, f"{label}无效", f"{label}的宽高必须大于 0（{empty_hint}）。",
            )
            return None
        return region


def module_manager_label(key: str, obj: dict) -> str:
    """Return the module-manager name with explicit risk/state markers."""
    name = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
    if not obj.get("enabled", True):
        name = f"【已禁用】{name}"
    if (
        obj.get("blocking")
        or obj.get("wait_text_absent")
    ) and not obj.get("pure_action"):
        name = f"【阻塞识别】{name}"
    if module_manager_special_action_summary(obj):
        name = f"【特殊代码段】{name}"
    return name


def module_manager_special_action_summary(obj: dict) -> str:
    """List fixed special actions in enabled success/timeout code segments.

    The source is included because the same special action has very different
    operational meaning when it runs after a match versus after a timeout.
    """
    parts: list[str] = []
    for source, field, enabled in (
        (
            "附加", "on_success_actions",
            bool(obj.get("run_code_after_action", False))
            or obj.get("after_action") == "run_actions",
        ),
        ("超时", "on_timeout_actions", bool(obj.get("run_code_on_timeout", False))),
    ):
        if not enabled:
            continue
        names: list[str] = []
        for action in obj.get(field) or []:
            if not isinstance(action, dict):
                continue
            name = special_action_label(str(action.get("type", "")))
            if name and name not in names:
                names.append(name)
        if names:
            parts.append(f"{source}：{'、'.join(names)}")
    return "；".join(parts)


def module_manager_tag(obj: dict) -> str:
    """Disabled > special code segment > blocking visual priority."""
    if not obj.get("enabled", True):
        return "disabled"
    if module_manager_special_action_summary(obj):
        return "special_action"
    blocking = obj.get("blocking") or obj.get("wait_text_absent")
    return "blocking" if blocking and not obj.get("pure_action") else ""


def module_manager_selection_colors(obj: dict | None) -> tuple[str, str]:
    """Return foreground/background for the selected module's enabled state."""
    if obj is None:
        return "#FFFFFF", COLOR_BLUE_SELECTION
    if not obj.get("enabled", True):
        return "#FFFFFF", "#7A3434"
    if module_manager_special_action_summary(obj):
        return "#FFFFFF", "#713C78"
    return "#FFFFFF", "#1F6B45"


def configure_module_tree_styles(style) -> None:
    """Clone the base Treeview layout and keep all manager variants readable."""
    base_tree_layout = style.layout("Treeview")
    for style_name, obj in (
        ("ModuleManagerNeutral.Treeview", None),
        ("ModuleManagerEnabled.Treeview", {"enabled": True}),
        ("ModuleManagerSpecial.Treeview", {
            "enabled": True,
            "run_code_after_action": True,
            "on_success_actions": [{"type": "restart_workflow"}],
        }),
        ("ModuleManagerDisabled.Treeview", {"enabled": False}),
    ):
        if base_tree_layout:
            style.layout(style_name, copy.deepcopy(base_tree_layout))
        style.configure(
            style_name,
            background=COLOR_SURFACE,
            fieldbackground=COLOR_SURFACE,
            foreground=COLOR_TEXT,
            rowheight=42,
            font=("Microsoft YaHei UI", 11),
        )
        selected_fg, selected_bg = module_manager_selection_colors(obj)
        style.map(
            style_name,
            foreground=[("selected", selected_fg)],
            background=[("selected", selected_bg)],
        )


class TemplateRegionManagerDialog(ModalDialog):
    """Manage the module-object registry (template image + region + behavior).

    五个可点击切换的页签：全部 / 切换 / 工作流全局 / 脚本全局 / 特殊。
    每个条目是结构化模块对象（类别 + 行为属性），模块选择窗口和 module_ref
    脚本动作运行时实时引用这些对象。新增 / 编辑走
    :class:`TemplateRegionFormDialog`；双击普通模块可直接编辑。
    """

    TAB_KEYS = (
        "all", "images", "switch", "workflow_global", "script_global", "special",
    )

    def __init__(self, parent, app=None):
        super().__init__(parent, "模块对象管理", 1040, 520)
        self.app = app or getattr(parent, "_macroflow_app", None)
        self.objects: dict[str, dict] = load_module_objects()
        self.current = "all"
        self.trees: dict[str, ttk.Treeview] = {}
        self.images_dir = load_module_images_dir()
        self.images_dir_var = tk.StringVar(value=str(self.images_dir))
        self.inventory_items: dict[str, dict[str, str]] = {}
        self.sort_direction = "asc"
        self.module_tree_style = ttk.Style(self)
        configure_module_tree_styles(self.module_tree_style)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="双击普通模块直接编辑；特殊模块为固定动作，不提供编辑设置。",
            foreground=COLOR_MUTED, wraplength=670,
        ).pack(anchor="w")
        directory_row = ttk.Frame(body)
        directory_row.pack(fill="x", pady=(10, 0))
        ttk.Label(directory_row, text="识图文件夹").pack(side="left")
        ttk.Entry(
            directory_row, textvariable=self.images_dir_var, state="readonly",
        ).pack(side="left", fill="x", expand=True, padx=(10, 8))
        ttk.Button(directory_row, text="选择目录…", command=self._choose_images_dir).pack(side="left")
        ttk.Button(directory_row, text="刷新", command=self._refresh_inventory).pack(side="left", padx=(8, 0))
        self.inventory_summary_var = tk.StringVar(value="")
        ttk.Label(
            body, textvariable=self.inventory_summary_var, foreground=COLOR_MUTED,
        ).pack(anchor="w", pady=(5, 0))
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True, pady=(10, 0))
        for tab_key in self.TAB_KEYS:
            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=self._tab_label(tab_key))
            self._build_tab(tab_key, tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # 初始化选中的是第一个页签（全部），但该选择不触发 TabChanged 事件，
        # 显式同步一次，保证 self.current 与当前可见页签一致（编辑 / 移除都
        # 操作当前页签的树，不同步会导致"全部"页签下按钮静默失效）。
        self._on_tab_changed()
        self._reload_trees()
        # 移除撤销栈：(key, object_dict)；"移除所选模块"后可用按钮或 Ctrl+Z 恢复。
        self._undo_stack: list[tuple[str, dict]] = []
        buttons = ttk.Frame(body)
        self.buttons_frame = buttons
        buttons.pack(fill="x", pady=(12, 0))
        self.add_button = ttk.Button(buttons, text="新增模块", command=self._open_add)
        self.add_button.pack(side="left")
        self.edit_button = ttk.Button(buttons, text="编辑选中", command=self._open_edit)
        self.edit_button.pack(side="left", padx=(8, 0))
        self.enabled_button = ttk.Button(
            buttons, text="禁用选中", command=self._toggle_selected_enabled,
        )
        self.enabled_button.pack(side="left", padx=(8, 0))
        self.remove_button = ttk.Button(buttons, text="移除所选模块", command=self._remove_selected)
        self.remove_button.pack(side="left", padx=(8, 0))
        self.undo_button = ttk.Button(
            buttons, text="撤销移除", command=self._undo_remove, state="disabled",
        )
        self.undo_button.pack(side="left", padx=(8, 0))
        self.batch_button = ttk.Button(
            buttons, text="批量加入脚本…", command=self._batch_add_selected,
        )
        self.batch_button.pack(side="left", padx=(8, 0))
        self.batch_remove_button = ttk.Button(
            buttons, text="批量从脚本删除…", command=self._batch_remove_from_scripts,
        )
        self.batch_remove_button.pack(side="left", padx=(8, 0))
        self.reference_button = ttk.Button(
            buttons, text="查看引用位置", command=self._show_references,
        )
        self.reference_button.pack(side="left", padx=(8, 0))
        self.remove_all_references_button = ttk.Button(
            buttons, text="删除全部引用", command=self._remove_all_references,
        )
        self.remove_all_references_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        self.bind("<Control-z>", self._undo_remove)
        self._update_action_buttons()

        # 固定 470 高度在打包后的 EXE（按真实 DPI 渲染）里会装不下内容：
        # 高 DPI 下列表和按钮行的实际需求高度超过窗口，按钮行被挤出窗口底部
        # （按钮完全看不见，用户报告"什么按钮都没有"）。这里按内容实际需求
        # 重设窗口尺寸（geometry 的宽高即内容区大小）并重新居中。
        self._fit_window_to_content(parent)

    def _fit_window_to_content(self, parent):
        """按内容实际需求重设窗口尺寸并居中（防高 DPI 下按钮行被裁掉）。

        最小尺寸按 96 DPI 的需求量兜底：打包版若请求尺寸未按真实 DPI 缩放
        （部分 DPI 时序问题），窗口也不会缩到按钮行被挤出的大小。
        """
        fit_window_to_content(self, parent, minimum_width=1000, minimum_height=500)

    @staticmethod
    def _tab_label(tab_key: str) -> str:
        return {
            "all": "全部模块", "images": "图片采用情况", "switch": "切换",
            "workflow_global": "工作流全局", "script_global": "脚本全局",
            "special": "特殊",
        }[tab_key]

    def _build_tab(self, tab_key: str, tab: ttk.Frame):
        if tab_key == "images":
            self.inventory_filter = "all"
            self.inventory_filter_buttons: dict[str, tk.Button] = {}
            filter_row = ttk.Frame(tab)
            filter_row.pack(fill="x", padx=4, pady=(6, 2))
            ttk.Label(filter_row, text="查看：", foreground=COLOR_MUTED).pack(side="left")
            for value, label in (("all", "全部图片"), ("adopted", "已采用"), ("unused", "未采用")):
                button = tk.Button(
                    filter_row, text=label, command=lambda item=value: self._set_inventory_filter(item),
                    background=COLOR_BLUE_SELECTION if value == "all" else COLOR_SURFACE,
                    foreground="#FFFFFF" if value == "all" else COLOR_TEXT,
                    activebackground=COLOR_BLUE_SELECTION, activeforeground="#FFFFFF",
                    relief="flat", borderwidth=0, padx=12, pady=4, cursor="hand2",
                    font=("Microsoft YaHei UI", 10),
                )
                button.pack(side="left", padx=(0, 6))
                self.inventory_filter_buttons[value] = button
        list_frame = ttk.Frame(tab)
        list_frame.pack(fill="both", expand=True, padx=4, pady=(4, 0))
        if tab_key == "images":
            tree = ttk.Treeview(
                list_frame, columns=("status", "kind"), show="tree headings", height=10,
                style="ModuleManagerNeutral.Treeview",
            )
            tree.heading("#0", text="图片文件")
            tree.heading("status", text="采用情况")
            tree.heading("kind", text="模块类别")
            tree.column("#0", width=410)
            tree.column("status", width=125, anchor="center")
            tree.column("kind", width=105, anchor="center")
            tree.tag_configure("adopted", foreground="#7BC96F")
            tree.tag_configure("unused", foreground="#F2B84B")
        elif tab_key == "special":
            tree = ttk.Treeview(
                list_frame, columns=("kind",), show="tree headings", height=10,
                style="ModuleManagerNeutral.Treeview",
            )
            tree.heading("#0", text="名称")
            tree.heading("kind", text="类型")
            tree.column("#0", width=380)
            tree.column("kind", width=90, anchor="center")
            tree.tag_configure("disabled", foreground="#707B85")
        else:
            tree = ttk.Treeview(
                list_frame, columns=("region", "special_actions"), show="tree headings", height=10,
                style="ModuleManagerNeutral.Treeview",
            )
            tree.heading("#0", text="模块名称")
            tree.heading("region", text="框选区域 (x,y,w,h)")
            tree.heading("special_actions", text="代码段特殊模块")
            tree.column("#0", width=310)
            tree.column("region", width=190, anchor="center")
            tree.column("special_actions", width=420)
            tree.tag_configure("blocking", foreground="#F2B84B")
            tree.tag_configure("special_action", foreground="#FF8DE1")
            tree.tag_configure("disabled", foreground="#707B85")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        tree.bind(
            "<Double-1>",
            lambda _event: self._open_inventory_item() if tab_key == "images" else self._open_edit(),
        )
        tree.bind("<Button-3>", self._show_module_context_menu, add="+")
        tree.bind("<<TreeviewSelect>>", self._update_action_buttons)
        self.trees[tab_key] = tree
        self._apply_sort_heading(tab_key, tree)

    @staticmethod
    def _sort_heading_label(tab_key: str) -> str:
        return {
            "all": "模块名称", "images": "图片文件",
            "switch": "模块名称", "workflow_global": "模块名称",
            "script_global": "模块名称", "special": "名称",
        }[tab_key]

    def _apply_sort_heading(self, tab_key: str, tree):
        arrow = "↑" if getattr(self, "sort_direction", "asc") == "asc" else "↓"
        tree.heading(
            "#0", text=f"{self._sort_heading_label(tab_key)} {arrow}",
            command=self._toggle_sort_direction,
        )

    def _on_tab_changed(self, _event=None):
        self.current = self.TAB_KEYS[self.notebook.index(self.notebook.select())]
        self._update_action_buttons()

    def _reload_trees(self):
        for tab_key, tree in self.trees.items():
            self._reload_tree(tab_key, tree)

    def _reload_tree(self, tab_key: str, tree: ttk.Treeview):
        tree.delete(*tree.get_children())
        if tab_key == "images":
            inventory = module_image_inventory(self.images_dir, self.objects)
            self.inventory_items = {item["path"]: item for item in inventory}
            adopted_count = sum(bool(item["module_key"]) for item in inventory)
            self.inventory_summary_var.set(
                f"共 {len(inventory)} 张图片：已采用 {adopted_count}，未采用 {len(inventory) - adopted_count}"
            )
            current_filter = getattr(self, "inventory_filter", "all")
            visible = [
                item for item in inventory
                if current_filter == "all"
                or (current_filter == "adopted" and bool(item["module_key"]))
                or (current_filter == "unused" and not item["module_key"])
            ]
            if current_filter != "all":
                self.inventory_summary_var.set(
                    self.inventory_summary_var.get() + f"；当前显示 {len(visible)} 张"
                )
            visible.sort(
                key=lambda item: pinyin_sort_key(Path(item["path"].replace("\\", "/")).stem),
                reverse=getattr(self, "sort_direction", "asc") == "desc",
            )
            for item in visible:
                keys = item.get("module_keys") or ([item["module_key"]] if item["module_key"] else [])
                categories = {
                    {
                        "switch": "切换", "workflow_global": "工作流全局",
                        "script_global": "脚本全局",
                    }.get(
                        self.objects.get(key, {}).get("category"), "—",
                    )
                    for key in keys
                }
                category = "/".join(sorted(categories)) if categories else "—"
                tree.insert(
                    "", "end", iid=item["path"],
                    text=str(Path(item["path"].replace("\\", "/")).name),
                    values=(item["status"], category),
                    tags=("adopted" if item["module_key"] else "unused",),
                )
            return
        items = sorted(
            self.objects.items(),
            key=lambda item: pinyin_sort_key(
                str(item[1].get("name") or Path(item[0].replace("\\", "/")).stem)
            ),
            reverse=getattr(self, "sort_direction", "asc") == "desc",
        )
        for key, obj in items:
            pure = bool(obj.get("pure_action"))
            if tab_key == "all":
                if pure:
                    tag = module_manager_tag(obj)
                    tree.insert(
                        "", "end", iid=key, text=module_manager_label(key, obj),
                        values=("—", "固定特殊模块"), tags=((tag,) if tag else ()),
                    )
                else:
                    region = obj.get("region", [0, 0, 0, 0])
                    text = ",".join(map(str, region)) if region[2] > 0 else "未设置区域（全屏）"
                    tree.insert(
                        "", "end", iid=key, text=module_manager_label(key, obj),
                        values=(text, module_manager_special_action_summary(obj) or "—"),
                        tags=((module_manager_tag(obj),) if module_manager_tag(obj) else ()),
                    )
            elif tab_key in ("switch", "workflow_global", "script_global"):
                if obj.get("category") != tab_key or pure:
                    continue
                region = obj.get("region", [0, 0, 0, 0])
                text = ",".join(map(str, region)) if region[2] > 0 else "未设置区域（全屏）"
                tree.insert(
                    "", "end", iid=key, text=module_manager_label(key, obj),
                    values=(text, module_manager_special_action_summary(obj) or "—"),
                    tags=((module_manager_tag(obj),) if module_manager_tag(obj) else ()),
                )
            else:  # special
                if obj.get("category") != "special":
                    continue
                tag = module_manager_tag(obj)
                tree.insert(
                    "", "end", iid=key, text=module_manager_label(key, obj),
                    values=("特殊",), tags=((tag,) if tag else ()),
                )

    def _set_inventory_filter(self, value: str):
        if value not in ("all", "adopted", "unused"):
            return
        self.inventory_filter = value
        for key, button in getattr(self, "inventory_filter_buttons", {}).items():
            selected = key == value
            button.configure(
                background=COLOR_BLUE_SELECTION if selected else COLOR_SURFACE,
                foreground="#FFFFFF" if selected else COLOR_TEXT,
            )
        tree = self.trees.get("images")
        if tree is not None:
            self._reload_tree("images", tree)

    def _set_sort_direction(self, value: str):
        if value not in ("asc", "desc"):
            return
        self.sort_direction = value
        for tab_key, tree in self.trees.items():
            self._apply_sort_heading(tab_key, tree)
        self._reload_trees()

    def _toggle_sort_direction(self):
        self._set_sort_direction("desc" if self.sort_direction == "asc" else "asc")

    def _open_add(self):
        if self.current == "images":
            self._open_inventory_item(require_unused=True)
            return
        if self.current == "special":
            show_floating_notice(self, "固定特殊模块", "特殊模块由软件提供，不能新增或编辑。")
            return
        category = "switch" if self.current == "all" else self.current
        self._open_form("", category=category)

    def _open_edit(self):
        if self.current == "images":
            self._open_inventory_item()
            return
        tree = self.trees[self.current]
        selection = tree.selection()
        if not selection:
            show_floating_notice(self, "请先选择模块", "先在列表里选中一个模块，再编辑。")
            return
        key = selection[0]
        obj = self.objects.get(key)
        if not obj or obj.get("category") == "special" or obj.get("pure_action"):
            show_floating_notice(self, "固定特殊模块", "该模块行为固定，无需也不能编辑。")
            return
        self._open_form(key, obj)

    def _show_module_context_menu(self, event):
        """Show move/copy actions for workflow/script global modules."""
        tree = event.widget
        key = tree.identify_row(event.y)
        if not key:
            return
        obj = self.objects.get(key)
        category = str(obj.get("category", "")) if obj else ""
        if category not in ("workflow_global", "script_global"):
            return
        tree.selection_set(key)
        tree.focus(key)
        target = "script_global" if category == "workflow_global" else "workflow_global"
        target_label = "脚本全局" if target == "script_global" else "工作流全局"
        menu = tk.Menu(
            self, tearoff=0, background=COLOR_SURFACE, foreground=COLOR_TEXT,
            activebackground=COLOR_BLUE_SELECTION, activeforeground="#FFFFFF",
        )
        menu.add_command(
            label=f"改成{target_label}",
            command=lambda: self._change_global_module_category(key, target, copy_object=False),
        )
        menu.add_command(
            label=f"复制成{target_label}",
            command=lambda: self._change_global_module_category(key, target, copy_object=True),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _change_global_module_category(self, key: str, target: str,
                                       copy_object: bool = False):
        """Move or independently clone one global module into the other category."""
        obj = self.objects.get(key)
        if not obj or target not in ("workflow_global", "script_global"):
            return
        source = str(obj.get("category", ""))
        if source not in ("workflow_global", "script_global") or source == target:
            return
        changed = copy.deepcopy(obj)
        changed["category"] = target
        changed_key = f"module:{uuid.uuid4().hex}" if copy_object else key
        if copy_object and not str(changed.get("name") or "").strip():
            # 旧对象可能没有 name（过去以图片路径为键，靠文件名兜底显示）；
            # 复制后换成 module:<uuid> 键，兜底会退化成 uuid，复制时按模板文件名补名。
            changed["name"] = Path(
                str(changed.get("template") or key).replace("\\", "/")
            ).stem
        self.objects[changed_key] = changed
        save_module_objects(self.objects)
        self._reload_trees()
        target_label = "脚本全局" if target == "script_global" else "工作流全局"
        action_label = "复制" if copy_object else "移动"
        name = str(changed.get("name") or Path(key.replace("\\", "/")).stem)
        show_floating_notice(
            self, f"已{action_label}为{target_label}",
            f"“{name}”已{action_label}为{target_label}模块。",
        )

    def _open_form(self, key: str = "", object_dict: dict | None = None,
                   category: str = "switch", initial_image: str = ""):
        """打开新增 / 编辑表单；保存后更新对象仓库并持久化。

        表单结果 (old_key, new_key, object_dict)：old_key 非空且更换了图片时
        移除旧条目；图片不变则只更新对象属性。
        """
        form_kwargs = {"object_dict": object_dict, "category": category}
        if initial_image:
            form_kwargs["initial_image"] = initial_image
        if getattr(self, "images_dir", None):
            form_kwargs["images_dir"] = self.images_dir
        form = TemplateRegionFormDialog(self, key, **form_kwargs)
        result = form.show()
        if result is None:
            return
        old_key, new_key, obj = result
        self.objects = update_module_object(new_key, obj, old_key=old_key)
        self._reload_trees()
        tree = self.trees[self.current]
        try:
            tree.selection_set(new_key)
            tree.see(new_key)
        except tk.TclError:
            # 编辑时改了类别，新条目不在当前页签树里（如 特殊→切换）。
            pass

    def _choose_images_dir(self):
        selected = filedialog.askdirectory(
            parent=self, title="选择识图文件夹", initialdir=str(self.images_dir),
        )
        if not selected:
            return
        self.images_dir = save_module_images_dir(selected)
        self.images_dir_var.set(str(self.images_dir))
        self._reload_trees()

    def _refresh_inventory(self):
        self.objects = load_module_objects()
        self._reload_trees()

    def _selected_inventory_item(self) -> dict | None:
        tree = self.trees.get("images")
        selection = tree.selection() if tree is not None else ()
        return self.inventory_items.get(selection[0]) if selection else None

    def _open_inventory_item(self, require_unused: bool = False):
        item = self._selected_inventory_item()
        if not item:
            show_floating_notice(self, "请先选择图片", "先在图片采用情况中选择一张图片。")
            return
        module_keys = list(item.get("module_keys") or [])
        module_key = item.get("module_key", "")
        if require_unused:
            # “新增模块”允许复用已采用图片；图片只是一项属性，不再是唯一身份。
            self._open_form(category="switch", initial_image=item["path"])
            return
        if len(module_keys) > 1:
            show_floating_notice(
                self, "图片被多个模块使用",
                f"这张图片已被 {len(module_keys)} 个独立模块使用。请到“全部 / 切换 / 工作流全局”页按模块名称编辑。",
            )
            return
        if module_key:
            self._open_form(module_key, self.objects.get(module_key))
            return
        self._open_form(category="switch", initial_image=item["path"])

    def _remove_selected(self):
        if self.current == "images":
            return
        tree = self.trees[self.current]
        selection = tree.selection()
        if not selection:
            return
        key = selection[0]
        obj = self.objects.get(key)
        if obj is None:
            return
        self._undo_stack.append((key, dict(obj)))
        self.objects.pop(key, None)
        save_module_objects(self.objects)
        self._reload_trees()
        self._update_undo_button()

    def _selected_module(self) -> tuple[str, dict] | None:
        if self.current == "images":
            item = self._selected_inventory_item()
            key = str(item.get("module_key", "")) if item else ""
        else:
            tree = self.trees.get(self.current)
            selection = tree.selection() if tree is not None else ()
            key = selection[0] if selection else ""
        obj = self.objects.get(key)
        return (key, obj) if key and obj else None

    def _toggle_selected_enabled(self):
        tree = self.trees.get(self.current)
        selection = tree.selection() if tree is not None else ()
        row_id = selection[0] if selection else ""
        selected = self._selected_module()
        if not selected:
            show_floating_notice(self, "请先选择模块", "先选择一个模块，再启用或禁用。")
            return
        key, obj = selected
        enabled = not bool(obj.get("enabled", True))
        obj["enabled"] = enabled
        save_module_objects(self.objects)
        self._reload_trees()
        if tree is not None:
            try:
                target = row_id or key
                tree.selection_set(target)
                tree.see(target)
            except tk.TclError:
                pass
        self._update_action_buttons()

    def _reference_paths(self):
        return configured_script_files()

    def _show_references(self):
        selected = self._selected_module()
        if not selected:
            show_floating_notice(self, "请先选择模块", "先选中一个模块，再查看它的引用位置。")
            return
        key, obj = selected
        references = find_module_references(key, self._reference_paths())
        if not references:
            show_floating_notice(self, "没有引用", "当前配置的脚本中没有找到这个模块的引用。")
            return
        name = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
        ModuleReferenceDialog(
            self, name, references,
            on_jump=getattr(self.app, "jump_to_module_reference", None),
            on_delete_all=self._remove_all_references,
            on_delete_selected=self._remove_selected_references,
        ).show()

    def _remove_selected_references(self, references, dialog=None):
        if not references:
            show_floating_notice(self, "未选择引用", "请先在引用位置列表中选择要删除的行。")
            return
        if not messagebox.askyesno(
                "删除选中引用", f"确定删除选中的 {len(references)} 个引用位置吗？",
                parent=self,
        ):
            return
        removed, untouched, errors = remove_module_references(references)
        if dialog is not None:
            dialog.destroy()
        self._reload_trees()
        self._update_action_buttons()
        detail = f"已删除 {removed} 个引用位置。"
        if untouched:
            detail += f"\n{len(untouched)} 个脚本未发生变化。"
        if errors:
            detail += "\n失败：" + "；".join(
                f"{path.name}：{message}" for path, message in errors[:3]
            )
        show_floating_notice(self, "删除选中引用完成", detail, 7000)

    def _remove_all_references(self):
        selected = self._selected_module()
        if not selected:
            show_floating_notice(self, "请先选择模块", "先选中一个模块，再删除它的全部引用。")
            return
        key, obj = selected
        paths = self._reference_paths()
        references = find_module_references(key, paths)
        if not references:
            show_floating_notice(self, "没有引用", "当前配置的脚本中没有找到这个模块的引用。")
            return
        name = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
        if not messagebox.askyesno(
                "删除全部引用", f"确定从 {len(references)} 个引用位置删除“{name}”吗？",
                parent=self,
        ):
            return
        removed, untouched, errors = remove_module_from_scripts(key, paths)
        self._reload_trees()
        self._update_action_buttons()
        detail = f"已从 {removed} 个脚本删除“{name}”的全部引用。"
        if untouched:
            detail += f"\n{len(untouched)} 个脚本未发生变化。"
        if errors:
            detail += "\n失败：" + "；".join(
                f"{path.name}：{message}" for path, message in errors[:3]
            )
        show_floating_notice(self, "删除引用完成", detail, 7000)

    def _batch_add_selected(self):
        selected = self._selected_module()
        if not selected:
            show_floating_notice(self, "请先选择模块", "先选中一个已采用的模块，再批量加入脚本。")
            return
        key, obj = selected
        if not obj.get("enabled", True):
            show_floating_notice(
                self, "模块已禁用", "请先启用该模块，再把它加入脚本。",
            )
            return
        if obj.get("recognize") == "number":
            show_floating_notice(
                self, "不能批量加入",
                "读取数字需要为每个脚本行分别设置比较数字和两路跳转，请在脚本编辑器中逐行插入。",
            )
            return
        category = str(obj.get("category", "switch"))
        if category == "workflow_global":
            show_floating_notice(
                self, "不能加入脚本",
                "工作流全局模块只用于工作流；请使用脚本全局模块加入脚本。",
            )
            return
        name = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
        paths = configured_script_files()
        if not paths:
            show_floating_notice(self, "没有脚本", "配置的脚本目录中没有可用脚本。")
            return
        chosen = BatchModuleScriptDialog(self, name, paths).show()
        if not chosen:
            return
        added, skipped, errors = prepend_module_to_scripts(key, category, chosen)
        detail = f"已将“{name}”加入 {added} 个脚本的开头。"
        if skipped:
            detail += f"\n跳过 {len(skipped)} 个全局脚本（不能嵌套全局模块）。"
        if errors:
            detail += f"\n失败 {len(errors)} 个：" + "；".join(
                f"{path.name}：{message}" for path, message in errors[:3]
            )
        show_floating_notice(self, "批量加入完成", detail, 7000)

    def _batch_remove_from_scripts(self):
        selected = self._selected_module()
        if not selected:
            show_floating_notice(self, "请先选择模块", "先选中一个已采用的模块，再批量从脚本删除。")
            return
        key, obj = selected
        if obj.get("category") == "special" or obj.get("pure_action"):
            show_floating_notice(
                self, "不能从脚本删除",
                "特殊模块是固定动作，脚本里不保存模块引用，无法按模块批量删除。"
                "请直接在脚本编辑器删除对应动作行。",
            )
            return
        name = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
        paths = configured_script_files()
        if not paths:
            show_floating_notice(self, "没有脚本", "配置的脚本目录中没有可用脚本。")
            return
        chosen = BatchModuleScriptDialog(self, name, paths, mode="remove", module_key=key).show()
        if not chosen:
            return
        removed, untouched, errors = remove_module_from_scripts(key, chosen)
        detail = f"已从 {removed} 个脚本移除“{name}”的动作行。"
        if untouched:
            detail += f"\n{len(untouched)} 个脚本没有该模块，未改动。"
        if errors:
            detail += f"\n失败 {len(errors)} 个：" + "；".join(
                f"{path.name}：{message}" for path, message in errors[:3]
            )
        show_floating_notice(self, "批量删除完成", detail, 7000)

    def _undo_remove(self, _event=None):
        """撤销最近一次"移除所选模块"：恢复条目、保存并选中它。"""
        if not self._undo_stack:
            return
        key, obj = self._undo_stack.pop()
        self.objects[key] = obj
        save_module_objects(self.objects)
        self._reload_trees()
        tree = self.trees[self.current]
        try:
            tree.selection_set(key)
            tree.see(key)
        except tk.TclError:
            # 恢复的条目不在当前页签（理论上不会发生：类别未变）。
            pass
        self._update_undo_button()

    def _update_undo_button(self):
        self.undo_button.configure(
            state="normal" if self._undo_stack else "disabled",
        )

    def _update_action_buttons(self, _event=None):
        """Keep edit/add affordances aligned with the active module category."""
        tree = self.trees.get(self.current)
        selection = tree.selection() if tree is not None else ()
        obj = self.objects.get(selection[0]) if selection else None
        editable = bool(obj) and obj.get("category") != "special" and not obj.get("pure_action")
        if self.current == "images":
            item = self._selected_inventory_item()
            adopted = bool(item and item.get("module_key"))
            selected = self._selected_module()
            selected_obj = selected[1] if selected else None
            self._update_selection_highlight(selected_obj)
            editable = bool(item)
            if getattr(self, "add_button", None) is not None:
                self.add_button.configure(
                    text="采用为模块", state="normal" if item and not adopted else "disabled",
                )
            if getattr(self, "edit_button", None) is not None:
                self.edit_button.configure(
                    text="编辑模块" if adopted else "采用并设置",
                    state="normal" if item else "disabled",
                )
            if getattr(self, "remove_button", None) is not None:
                self.remove_button.configure(state="disabled")
            if getattr(self, "batch_button", None) is not None:
                self.batch_button.configure(
                    state="normal"
                    if adopted and selected_obj and selected_obj.get("enabled", True)
                    else "disabled",
                )
            if getattr(self, "batch_remove_button", None) is not None:
                self.batch_remove_button.configure(state="normal" if adopted else "disabled")
            enabled_button = getattr(self, "enabled_button", None)
            if enabled_button is not None:
                enabled_button.configure(
                    text="禁用选中" if selected_obj and selected_obj.get("enabled", True) else "启用选中",
                    state="normal" if selected_obj else "disabled",
                )
            return
        self._update_selection_highlight(obj)
        edit_button = getattr(self, "edit_button", None)
        if edit_button is not None:
            edit_button.configure(text="编辑选中")
            edit_button.configure(state="normal" if editable else "disabled")
        add_button = getattr(self, "add_button", None)
        if add_button is not None:
            add_button.configure(text="新增模块")
            add_button.configure(state="disabled" if self.current == "special" else "normal")
        remove_button = getattr(self, "remove_button", None)
        if remove_button is not None:
            remove_button.configure(state="normal" if editable else "disabled")
        batch_button = getattr(self, "batch_button", None)
        if batch_button is not None:
            batch_button.configure(
                state="normal" if obj and obj.get("enabled", True) else "disabled",
            )
        enabled_button = getattr(self, "enabled_button", None)
        if enabled_button is not None:
            enabled_button.configure(
                text="禁用选中" if obj and obj.get("enabled", True) else "启用选中",
                state="normal" if obj else "disabled",
            )
        batch_remove_button = getattr(self, "batch_remove_button", None)
        if batch_remove_button is not None:
            batch_remove_button.configure(state="normal" if editable else "disabled")

    def _update_selection_highlight(self, obj: dict | None):
        tree = getattr(self, "trees", {}).get(getattr(self, "current", ""))
        if tree is None:
            return
        if obj is None:
            style_name = "ModuleManagerNeutral.Treeview"
        elif module_manager_special_action_summary(obj) and obj.get("enabled", True):
            style_name = "ModuleManagerSpecial.Treeview"
        elif obj.get("enabled", True):
            style_name = "ModuleManagerEnabled.Treeview"
        else:
            style_name = "ModuleManagerDisabled.Treeview"
        tree.configure(style=style_name)


class ModuleReferenceDialog(ModalDialog):
    """Navigate through every script location that references one module."""

    def __init__(self, parent, module_name: str, references: list[dict],
                 on_jump=None, on_delete_all=None, on_delete_selected=None):
        super().__init__(parent, "模块引用位置", 760, 430)
        self.references = list(references)
        self.on_jump = on_jump
        self.on_delete_all = on_delete_all
        self.on_delete_selected = on_delete_selected
        self.position = 0
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body, text=f"模块“{module_name}”共有 {len(self.references)} 个引用位置。",
            foreground=COLOR_TEXT,
        ).pack(anchor="w")
        self.location_var = tk.StringVar()
        ttk.Label(body, textvariable=self.location_var, foreground=COLOR_MUTED).pack(
            anchor="w", pady=(6, 8),
        )
        self.tree = ttk.Treeview(
            body, columns=("path", "row"), show="headings", height=12,
            selectmode="extended",
        )
        self.tree.heading("path", text="脚本")
        self.tree.heading("row", text="引用行")
        self.tree.column("path", width=590)
        self.tree.column("row", width=90, anchor="center")
        self.tree.pack(fill="both", expand=True)
        for index, reference in enumerate(self.references):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(display_path(reference["path"]), reference["index"] + 1),
            )
        self.tree.bind("<Double-1>", lambda _event: self._jump_to(self.position))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="上一个", command=self._previous).pack(side="left")
        ttk.Button(buttons, text="下一个", command=self._next).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="删除选中引用", command=self._delete_selected,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="删除全部引用", command=self._delete_all,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side="right")
        self._select_current()
        fit_window_to_content(self, parent, minimum_width=760, minimum_height=430)

    def _select_current(self):
        if not self.references:
            return
        self.position = max(0, min(self.position, len(self.references) - 1))
        self.tree.selection_set(str(self.position))
        self.tree.focus(str(self.position))
        self.tree.see(str(self.position))
        reference = self.references[self.position]
        self.location_var.set(
            f"第 {self.position + 1}/{len(self.references)} 个："
            f"{display_path(reference['path'])} · 第 {reference['index'] + 1} 行"
        )

    def _jump_to(self, position: int):
        if not self.references:
            return
        self.position = max(0, min(position, len(self.references) - 1))
        self._select_current()
        reference = self.references[self.position]
        if callable(self.on_jump):
            self.on_jump(reference["path"], reference["index"])

    def _next(self):
        self._jump_to((self.position + 1) % len(self.references))

    def _previous(self):
        self._jump_to((self.position - 1) % len(self.references))

    def _delete_all(self):
        if callable(self.on_delete_all):
            self.on_delete_all()

    def _delete_selected(self):
        selected = [int(item) for item in self.tree.selection()]
        references = [self.references[index] for index in selected if 0 <= index < len(self.references)]
        if callable(self.on_delete_selected):
            self.on_delete_selected(references, self)


class BatchModuleScriptDialog(ModalDialog):
    """Checkbox-style multi-selection of scripts for batch module insertion.

    ``mode="add"`` 勾选后把模块插入脚本第 1 行；``mode="remove"`` 勾选后从
    脚本删除该模块的所有引用行——此时列出每个脚本的引用行数（未使用的标
    "未使用"）并预勾选含该模块的脚本。
    """

    def __init__(self, parent, module_name: str, script_paths: list[Path],
                 mode: str = "add", module_key: str = ""):
        self.mode = mode
        super().__init__(parent, "批量从脚本删除" if mode == "remove" else "批量加入脚本", 700, 560)
        self.script_paths = list(script_paths)
        settings = load_app_settings()
        self.script_categories = [
            script_category_for_path(path, settings) for path in self.script_paths
        ]
        self.current_filter = "all"
        self.checked: set[int] = set()
        if mode == "remove":
            self.usage_counts = self._count_module_usage(module_key)
            self.checked = {
                index for index, count in self.usage_counts.items() if count
            }
        else:
            self.usage_counts = {}
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=(f"选择要移除“{module_name}”的脚本："
                  if mode == "remove"
                  else f"选择要在第 1 行加入“{module_name}”的脚本："),
            foreground=COLOR_TEXT,
        ).pack(anchor="w")
        if mode == "remove":
            ttk.Label(
                body,
                text="只移除脚本中引用该模块的动作行，脚本其余内容不变。",
                foreground=COLOR_MUTED,
            ).pack(anchor="w", pady=(4, 0))
        self.filter_buttons: dict[str, tk.Button] = {}
        filter_row = ttk.Frame(body)
        filter_row.pack(fill="x", pady=(10, 0))
        ttk.Label(filter_row, text="分类：", foreground=COLOR_MUTED).pack(side="left")
        for category in SCRIPT_CATEGORY_LABELS:
            count = (
                len(self.script_paths) if category == "all"
                else self.script_categories.count(category)
            )
            button = tk.Button(
                filter_row,
                text=f"{SCRIPT_CATEGORY_LABELS[category]} {count}",
                command=lambda value=category: self._set_filter(value),
                background=COLOR_BLUE_SELECTION if category == "all" else COLOR_SURFACE,
                foreground="#FFFFFF" if category == "all" else COLOR_TEXT,
                activebackground=COLOR_BLUE_SELECTION, activeforeground="#FFFFFF",
                relief="flat", borderwidth=0, padx=10, pady=4, cursor="hand2",
                font=("Microsoft YaHei UI", 10),
            )
            button.pack(side="left", padx=(0, 6))
            self.filter_buttons[category] = button
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(
            frame, columns=("checked", "category", "path"), show="headings", height=16,
            selectmode="extended",
        )
        self.tree.heading("checked", text="勾选")
        self.tree.heading("category", text="分类")
        self.tree.heading("path", text="脚本")
        self.tree.column("checked", width=60, anchor="center", stretch=False)
        self.tree.column("category", width=85, anchor="center", stretch=False)
        self.tree.column("path", width=470)
        self._reload_visible_scripts()
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._toggle_selected)
        self.tree.bind("<space>", self._toggle_selected)
        self.tree.bind("<Control-a>", self._select_all)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="全选", command=self._select_all).pack(side="left")
        ttk.Button(buttons, text="全不选", command=self._clear_all).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self._save).pack(side="right", padx=(0, 8))
        fit_window_to_content(self, parent, minimum_width=680, minimum_height=500)

    def _count_module_usage(self, module_key: str) -> dict[int, int]:
        """Per-script count of top-level actions referencing the module."""
        counts: dict[int, int] = {}
        for index, path in enumerate(self.script_paths):
            count = 0
            try:
                script = load_script(path)
                count = sum(
                    1 for action in script.actions
                    if str(action.get("module_key", "")).strip() == module_key
                )
            except Exception:
                pass
            counts[index] = count
        return counts

    def _path_display(self, index: int) -> str:
        text = display_path(self.script_paths[index])
        if self.mode != "remove":
            return text
        count = self.usage_counts.get(index, 0)
        return f"{text}（{count} 行）" if count else f"{text}（未使用）"

    def _set_checked(self, index: int, checked: bool):
        if checked:
            self.checked.add(index)
        else:
            self.checked.discard(index)
        if self.tree.exists(str(index)):
            category = SCRIPT_CATEGORY_LABELS.get(self.script_categories[index], "关卡")
            self.tree.item(
                str(index),
                values=("☑" if checked else "☐", category, self._path_display(index)),
            )

    def _visible_indices(self) -> list[int]:
        return [
            index for index, category in enumerate(self.script_categories)
            if self.current_filter == "all" or category == self.current_filter
        ]

    def _reload_visible_scripts(self):
        self.tree.delete(*self.tree.get_children())
        for index in self._visible_indices():
            category = SCRIPT_CATEGORY_LABELS.get(self.script_categories[index], "关卡")
            self.tree.insert(
                "", "end", iid=str(index),
                values=("☑" if index in self.checked else "☐", category, self._path_display(index)),
            )

    def _set_filter(self, category: str):
        if category not in SCRIPT_CATEGORY_LABELS:
            return
        self.current_filter = category
        for key, button in self.filter_buttons.items():
            selected = key == category
            button.configure(
                background=COLOR_BLUE_SELECTION if selected else COLOR_SURFACE,
                foreground="#FFFFFF" if selected else COLOR_TEXT,
            )
        self._reload_visible_scripts()

    def _toggle_indices(self, indices: list[int]):
        for index in indices:
            self._set_checked(index, index not in self.checked)

    def _on_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        item = self.tree.identify_row(event.y)
        if item:
            self._toggle_indices([int(item)])
            return "break"

    def _toggle_selected(self, _event=None):
        indices = [int(item) for item in self.tree.selection()]
        if indices:
            self._toggle_indices(indices)
        return "break"

    def _select_all(self, _event=None):
        for index in self._visible_indices():
            self._set_checked(index, True)
        return "break"

    def _clear_all(self):
        for index in self._visible_indices():
            self._set_checked(index, False)

    def _save(self):
        if not self.checked:
            show_floating_notice(self, "尚未勾选", "请至少勾选一个脚本。")
            return
        self.result = [self.script_paths[index] for index in sorted(self.checked)]
        self.destroy()


class ModulePickerDialog(ModalDialog):
    """Pick a module object or special action to insert into a script.

    脚本编辑器显示切换 / 脚本全局 / 特殊；工作流入口只显示工作流全局。
    新建同步进仓库；特殊模块是无需图片的纯动作；nested 时隐藏特殊页签）。
    ``show()`` 返回要插入的脚本动作 dict；取消返回 ``None``。
    """

    def __init__(self, parent, actions: list[dict] | None = None,
                 nested: bool = False, segment_depth: int = 0,
                 categories: tuple[str, ...] | None = None,
                 multi_select: bool = False, allow_number: bool | None = None,
                 selection_only: bool = False):
        # 附加代码段允许插入固定特殊模块（例如“重新执行工作流”）；nested
        # 仍用于限制模块代码段递归深度，不再隐藏特殊模块页签。
        allowed = categories or ("switch", "script_global", "special")
        self.allowed_categories = tuple(
            category for category in allowed
            if category in ("switch", "workflow_global", "script_global", "special")
        ) or ("switch",)
        title = (
            "选择工作流全局模块"
            if self.allowed_categories == ("workflow_global",) else "插入模块"
        )
        super().__init__(parent, title, 560, 460)
        self.actions = actions or []
        self.nested = nested
        self.allow_number = (not nested) if allow_number is None else bool(allow_number)
        self.segment_depth = segment_depth
        self.multi_select = bool(multi_select)
        self.selection_only = bool(selection_only)
        self.objects: dict[str, dict] = load_module_objects()
        self.category_keys: dict[str, list[str]] = {
            "switch": [], "workflow_global": [], "script_global": [], "special": [],
        }
        self.listboxes: dict[str, tk.Listbox] = {}
        self.empty_labels: dict[str, ttk.Label] = {}
        self.tab_special: ttk.Frame | None = None
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=("可用 Ctrl 多选、Shift 连选、Ctrl+A 全选；点击“选择”批量添加。"
                  if self.multi_select
                  else "双击选择模块对象；这里只显示模块仓库中的工作流全局模块。"
                  if self.allowed_categories == ("workflow_global",)
                  else "双击选择模块；特殊模块为固定动作，插入后无需配置。"),
            foreground=COLOR_MUTED, wraplength=520,
        ).pack(anchor="w")
        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True, pady=(10, 0))
        if "switch" in self.allowed_categories:
            tab_switch = ttk.Frame(notebook)
            notebook.add(tab_switch, text="切换模块")
            self._build_category_tab("switch", tab_switch)
        if "workflow_global" in self.allowed_categories:
            tab_global = ttk.Frame(notebook)
            notebook.add(tab_global, text="工作流全局模块")
            self._build_category_tab("workflow_global", tab_global)
        if "script_global" in self.allowed_categories:
            tab_script_global = ttk.Frame(notebook)
            notebook.add(tab_script_global, text="脚本全局模块")
            self._build_category_tab("script_global", tab_script_global)
        if "special" in self.allowed_categories:
            self.tab_special = ttk.Frame(notebook)
            notebook.add(self.tab_special, text="特殊模块")
            self._build_category_tab("special", self.tab_special)
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        fit_window_to_content(self, parent, minimum_width=560, minimum_height=420)

    def _build_category_tab(self, category: str, tab: ttk.Frame):
        list_frame = ttk.Frame(tab)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        listbox = tk.Listbox(
            list_frame, background=COLOR_SURFACE, foreground=COLOR_TEXT,
            selectbackground=COLOR_BLUE_SELECTION,
            font=("Microsoft YaHei UI", 11), relief="flat", borderwidth=0,
            selectmode="extended" if self.multi_select else "browse",
            exportselection=False,
        )
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        listbox.bind("<Double-1>", lambda _event: self._choose_category(category))
        if self.multi_select:
            listbox.bind(
                "<Control-a>",
                lambda _event, key=category: self._select_all_category(key),
            )
        self.listboxes[category] = listbox
        empty_label = ttk.Label(
            tab,
            text=("暂无可用的固定特殊模块" if category == "special"
                  else "该分类还没有模块，点“新建模块…”创建"),
            foreground=COLOR_MUTED,
        )
        empty_label.pack(anchor="w", padx=10, pady=(6, 0))
        self.empty_labels[category] = empty_label
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", padx=10, pady=(10, 10))
        ttk.Button(
            buttons, text="选择", command=lambda: self._choose_category(category),
        ).pack(side="left")
        if category != "special":
            ttk.Button(
                buttons, text="新建模块…", command=lambda: self._new_object(category),
            ).pack(side="left", padx=(8, 0))
        self._refresh_category(category)

    def _refresh_category(self, category: str):
        keys = [
            key for key, obj in self.objects.items()
            if obj.get("category") == category and obj.get("enabled", True)
            and (getattr(self, "allow_number", True) or obj.get("recognize") != "number")
        ]
        self.category_keys[category] = keys
        listbox = self.listboxes[category]
        listbox.delete(0, "end")
        for key in keys:
            obj = self.objects[key]
            label = str(obj.get("name") or Path(key.replace("\\", "/")).stem)
            if obj.get("recognize") == "text":
                label += " · 识别文字"
                if obj.get("wait_text_absent"):
                    label += " · 等待文字消失"
            elif obj.get("recognize") == "number":
                label += " · 读取数字"
            elif obj.get("wait_text_absent"):
                label += " · 等待模板消失"
            listbox.insert("end", label)
        empty_label = self.empty_labels[category]
        if keys:
            empty_label.pack_forget()
        else:
            empty_label.pack(anchor="w", padx=10, pady=(6, 0))

    def _refresh_lists(self):
        self.objects = load_module_objects()
        for category in getattr(
                self, "allowed_categories", ("switch", "script_global", "special")):
            self._refresh_category(category)

    def _choose_category(self, category: str):
        selection = self.listboxes[category].curselection()
        if not selection:
            show_floating_notice(self, "请先选择模块", "先选中一个模块再点“选择”。")
            return
        keys = self.category_keys[category]
        selected_keys = [keys[index] for index in selection if index < len(keys)]
        if not selected_keys:
            return
        if getattr(self, "multi_select", False):
            self.result = [self._action_for_key(key, category) for key in selected_keys]
            self.destroy()
            return
        self._choose_key(selected_keys[0], category)

    def _select_all_category(self, category: str):
        listbox = self.listboxes[category]
        listbox.selection_set(0, "end")
        return "break"

    def _choose_key(self, key: str, category: str):
        obj = getattr(self, "objects", {}).get(key)
        if (
            obj and obj.get("recognize") == "number"
            and not getattr(self, "allow_number", True)
        ):
            show_floating_notice(
                self, "此处不能插入",
                "读取数字需要脚本行提供比较数字和两路跳转，只能在脚本编辑器中插入。",
            )
            return
        action = self._action_for_key(key, category)
        self.result = [action] if getattr(self, "multi_select", False) else action
        self.destroy()

    def _action_for_key(self, key: str, category: str) -> dict:
        objects = getattr(self, "objects", {})
        if getattr(self, "selection_only", False):
            return module_reference_binding(key, objects.get(key))
        return module_action_for_key(key, category, objects.get(key))

    def _new_object(self, category: str):
        if category == "special":
            return
        form = TemplateRegionFormDialog(
            self, category=category, segment_depth=self.segment_depth + 1,
        )
        result = form.show()
        if result is None:
            return
        old_key, key, obj = result
        update_module_object(key, obj, old_key=old_key)
        self._refresh_lists()
        self._choose_key(key, category)


class ModuleReferenceDelayDialog(ModalDialog):
    """Replace a module reference or edit its per-reference result branches."""

    def __init__(self, parent, action: dict, actions: list[dict] | None = None):
        result_routes = action.get("type") == "image_match"
        key = str(action.get("module_key") or action.get("template", ""))
        obj = registered_module_object(key)
        number_routes = bool(result_routes and obj and obj.get("recognize") == "number")
        super().__init__(
            parent, "编辑数字读取" if number_routes else "编辑模块引用",
            680, 640 if number_routes else 570 if result_routes else 330,
        )
        self.action = dict(action)
        self.result_routes_enabled = result_routes
        self.number_routes_enabled = number_routes
        self.delay = duration_var(action.get("delay_ms", 0))
        self.after_delay = duration_var(action.get("after_delay_ms", 0))
        self.jump_options = image_jump_target_options(actions or [])
        self.jump_target_ids = dict(self.jump_options)
        self.on_success = tk.StringVar(
            value=module_result_option_label(str(action.get("on_found", "continue"))),
        )
        self.on_failure = tk.StringVar(
            value=module_result_option_label(str(action.get("on_timeout", "continue"))),
        )
        self.success_target = tk.StringVar(value=select_jump_target_label(
            str(action.get("found_jump_action_id", "")).strip(),
            max(1, int(action.get("found_jump_row", 1))), self.jump_options,
        ))
        self.failure_target = tk.StringVar(value=select_jump_target_label(
            str(action.get("timeout_jump_action_id", "")).strip(),
            max(1, int(action.get("timeout_jump_row", 1))), self.jump_options,
        ))
        self.expected_number = tk.StringVar(
            value="" if action.get("expected_number") is None
            else str(action.get("expected_number")),
        )
        name = str(obj.get("name") or Path(key.replace("\\", "/")).stem) if obj else Path(key.replace("\\", "/")).stem
        self.module_name = tk.StringVar(value=name or "未设置")

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="引用模块").grid(row=0, column=0, sticky="w", pady=8)
        module_row = ttk.Frame(body)
        module_row.grid(row=0, column=1, sticky="ew", pady=8)
        module_row.columnconfigure(0, weight=1)
        ttk.Label(
            module_row, textvariable=self.module_name, foreground=COLOR_MUTED,
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            module_row, text="替换模块…", command=self.replace_reference,
        ).grid(row=0, column=1, padx=(8, 0))
        for row, (label, variable) in enumerate((
            ("进入模块前延时", self.delay),
            ("模块完成后延时", self.after_delay),
        ), start=1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            ttk.Spinbox(
                body, from_=0, to=86400000, increment=100,
                textvariable=variable, width=12,
            ).grid(row=row, column=1, sticky="ew", pady=8)
        next_row = 3
        if number_routes:
            ttk.Label(body, text="比较数字").grid(
                row=next_row, column=0, sticky="w", pady=8,
            )
            ttk.Entry(body, textvariable=self.expected_number).grid(
                row=next_row, column=1, sticky="ew", pady=8,
            )
            next_row += 1
        if result_routes:
            option_labels = tuple(label for label, _value in MODULE_RESULT_OPTIONS)
            target_labels = tuple(label for label, _action_id in self.jump_options)
            ttk.Label(body, text="数字等于时" if number_routes else "模块成功后").grid(
                row=next_row, column=0, sticky="w", pady=8,
            )
            ttk.Combobox(
                body, textvariable=self.on_success, values=option_labels,
                state="readonly",
            ).grid(row=next_row, column=1, sticky="ew", pady=8)
            next_row += 1
            ttk.Label(body, text="等于后跳转到" if number_routes else "成功跳转到").grid(
                row=next_row, column=0, sticky="w", pady=8,
            )
            self.success_target_combo = ttk.Combobox(
                body, textvariable=self.success_target, values=target_labels,
                state="disabled",
            )
            self.success_target_combo.grid(row=next_row, column=1, sticky="ew", pady=8)
            next_row += 1
            ttk.Label(
                body, text="数字不等于或未读取到时" if number_routes else "模块失败后",
            ).grid(
                row=next_row, column=0, sticky="w", pady=8,
            )
            ttk.Combobox(
                body, textvariable=self.on_failure, values=option_labels,
                state="readonly",
            ).grid(row=next_row, column=1, sticky="ew", pady=8)
            next_row += 1
            ttk.Label(body, text="不等于后跳转到" if number_routes else "失败跳转到").grid(
                row=next_row, column=0, sticky="w", pady=8,
            )
            self.failure_target_combo = ttk.Combobox(
                body, textvariable=self.failure_target, values=target_labels,
                state="disabled",
            )
            self.failure_target_combo.grid(row=next_row, column=1, sticky="ew", pady=8)
            next_row += 1
            self.on_success.trace_add("write", self._update_result_target_states)
            self.on_failure.trace_add("write", self._update_result_target_states)
            self._update_result_target_states()
        ttk.Label(
            body,
            text=(
                "读取到数字后立即比较；等于走成功分支，不等于走失败分支。未读取到数字会按模块的阻塞和未识别时限重试，超时后走失败分支。"
                if number_routes else
                "结果分支只属于当前脚本行；识别方式、区域、相似度、阻塞、未识别时限、点击和代码段仍在“模块管理…”统一设置。"
                if result_routes else
                "此处只设置当前引用的进入/完成延时；检测和触发行为统一到“模块管理…”修改。"
            ),
            foreground=COLOR_MUTED, wraplength=610,
        ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(10, 0))
        next_row += 1
        buttons = ttk.Frame(body)
        buttons.grid(row=next_row, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=(0, 8))

    def _update_result_target_states(self, *_args):
        if not self.result_routes_enabled:
            return
        self.success_target_combo.configure(
            state="readonly"
            if module_result_option_value(self.on_success.get()) == "jump" else "disabled",
        )
        self.failure_target_combo.configure(
            state="readonly"
            if module_result_option_value(self.on_failure.get()) == "jump" else "disabled",
        )

    def replace_reference(self):
        category = str(self.action.get("module_category", "switch"))
        if category == "global":
            category = "script_global"
        if category not in ("switch", "script_global"):
            category = "switch"
        replacement = ModulePickerDialog(
            self, categories=(category,),
        ).show()
        if not isinstance(replacement, dict):
            return
        replacement_key = str(
            replacement.get("module_key") or replacement.get("template", "")
        )
        replacement_obj = registered_module_object(replacement_key)
        replacement_is_number = bool(
            replacement_obj and replacement_obj.get("recognize") == "number"
        )
        if replacement_is_number != bool(getattr(self, "number_routes_enabled", False)):
            show_floating_notice(
                self, "模块类型不同",
                "读取数字模块和普通模块的行级设置不同，请删除当前行后重新插入。",
            )
            return
        preserved = {
            key: self.action[key]
            for key in (
                ACTION_ID_KEY, "delay_ms", "after_delay_ms",
                "jump_row", "jump_action_id",
                "on_found", "found_jump_action_id",
                "on_timeout", "timeout_jump_action_id",
                "expected_number",
            )
            if key in self.action
        }
        self.action = dict(replacement)
        self.action.update(preserved)
        key = str(self.action.get("module_key") or self.action.get("template", ""))
        obj = registered_module_object(key)
        name = (
            str(obj.get("name") or Path(key.replace("\\", "/")).stem)
            if obj else Path(key.replace("\\", "/")).stem
        )
        self.module_name.set(name or "未设置")

    def save(self):
        try:
            delay = max(0, min(86400000, int(self.delay.get())))
            after_delay = max(0, min(86400000, int(self.after_delay.get())))
        except ValueError:
            show_floating_notice(self, "参数错误", "执行前延时和执行后延时必须是整数。")
            return
        result = dict(self.action)
        result["delay_ms"] = delay
        result["after_delay_ms"] = after_delay
        if bool(getattr(self, "number_routes_enabled", False)):
            try:
                expected_number = int(self.expected_number.get())
            except (TypeError, ValueError):
                show_floating_notice(self, "比较数字无效", "比较数字必须是大于等于 0 的整数。")
                return
            if expected_number < 0:
                show_floating_notice(self, "比较数字无效", "比较数字必须是大于等于 0 的整数。")
                return
            result["expected_number"] = expected_number
        else:
            result.pop("expected_number", None)
        if self.result_routes_enabled:
            success = module_result_option_value(self.on_success.get())
            failure = module_result_option_value(self.on_failure.get())
            success_target_id = self.jump_target_ids.get(self.success_target.get(), "")
            failure_target_id = self.jump_target_ids.get(self.failure_target.get(), "")
            if success == "jump" and not success_target_id:
                show_floating_notice(self, "缺少目标", "请选择模块成功后要跳转的行对象。")
                return
            if failure == "jump" and not failure_target_id:
                show_floating_notice(self, "缺少目标", "请选择模块失败后要跳转的行对象。")
                return
            result["on_found"] = success
            result["found_jump_action_id"] = success_target_id
            result["on_timeout"] = failure
            result["timeout_jump_action_id"] = failure_target_id
        self.result = result
        self.destroy()


class ScriptRefDialog(ModalDialog):
    """Choose another script to reference; its latest content is read at runtime."""

    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "引用脚本", 560, 320)
        action = action or {}
        self.script = tk.StringVar(value=str(action.get("script", "")))
        self.delay = duration_var(action.get("delay_ms", 0))
        self.after_delay = duration_var(action.get("after_delay_ms", 0))

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="脚本文件").grid(row=0, column=0, sticky="w", pady=8)
        script_row = ttk.Frame(body)
        script_row.grid(row=0, column=1, sticky="ew")
        ttk.Entry(script_row, textvariable=self.script, state="readonly").pack(
            side="left", fill="x", expand=True,
        )
        ttk.Button(script_row, text="替换脚本…", command=self.choose).pack(
            side="left", padx=(6, 0),
        )

        ttk.Label(body, text="执行前延时").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.delay, width=10,
        ).grid(row=1, column=1, sticky="ew")
        ttk.Label(body, text="执行后延时").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.after_delay, width=10,
        ).grid(row=2, column=1, sticky="ew")

        ttk.Label(
            body,
            text="运行时实时读取所选脚本的最新内容；修改原脚本后，这里的引用会自动跟着更新。",
            foreground=COLOR_MUTED, wraplength=480,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def choose(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择要引用的脚本",
            filetypes=[("MacroFlow 脚本", "*.json"), ("所有文件", "*.*")],
        )
        if path:
            self.script.set(display_path(Path(path)))

    def save(self):
        script = self.script.get().strip()
        if not script:
            show_floating_notice(self, "脚本无效", "请选择要引用的脚本。")
            return
        try:
            delay = max(0, int(self.delay.get()))
            after_delay = max(0, int(self.after_delay.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "延时必须是整数毫秒。")
            return
        self.result = {
            "type": "script_ref",
            "script": script,
            "delay_ms": delay,
            "after_delay_ms": after_delay,
        }
        self.destroy()


class OpenAppDialog(ModalDialog):
    """Choose an application to launch; its path is saved in the action."""

    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "打开软件", 560, 350)
        action = action or {}
        self.path = tk.StringVar(value=str(action.get("path", "")))
        self.args = tk.StringVar(value=str(action.get("args", "")))
        self.delay = duration_var(action.get("delay_ms", 0))
        self.after_delay = duration_var(action.get("after_delay_ms", 0))

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="软件路径").grid(row=0, column=0, sticky="w", pady=8)
        path_row = ttk.Frame(body)
        path_row.grid(row=0, column=1, sticky="ew")
        ttk.Entry(path_row, textvariable=self.path, state="readonly").pack(
            side="left", fill="x", expand=True,
        )
        ttk.Button(path_row, text="选择…", command=self.choose).pack(
            side="left", padx=(6, 0),
        )

        ttk.Label(body, text="启动参数").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Entry(body, textvariable=self.args).grid(row=1, column=1, sticky="ew")
        ttk.Label(
            body,
            text="例：-windowed -u=xxx；留空则不带参数启动。",
            foreground=COLOR_MUTED,
        ).grid(row=2, column=1, sticky="w")

        ttk.Label(body, text="执行前延时").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.delay, width=10,
        ).grid(row=3, column=1, sticky="ew")
        ttk.Label(body, text="执行后延时").grid(row=4, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.after_delay, width=10,
        ).grid(row=4, column=1, sticky="ew")

        ttk.Label(
            body,
            text="执行到这一行时会启动所选软件，然后再继续后面的动作。",
            foreground=COLOR_MUTED, wraplength=480,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def choose(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择要打开的软件",
            filetypes=[("程序", "*.exe *.bat *.cmd *.lnk"), ("所有文件", "*.*")],
        )
        if path:
            self.path.set(display_path(Path(path)))

    def save(self):
        path = self.path.get().strip()
        if not path:
            show_floating_notice(self, "路径无效", "请选择要打开的软件。")
            return
        try:
            delay = max(0, int(self.delay.get()))
            after_delay = max(0, int(self.after_delay.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "延时必须是整数毫秒。")
            return
        self.result = {
            "type": "open_app",
            "path": path,
            "args": self.args.get().strip(),
            "delay_ms": delay,
            "after_delay_ms": after_delay,
        }
        self.destroy()


class CloseAppDialog(ModalDialog):
    """Terminate a running program by image name (e.g. clash-verge.exe)."""

    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "关闭软件", 560, 400)
        action = action or {}
        self.name = tk.StringVar(value=str(action.get("name", "")))
        self.graceful = tk.BooleanVar(value=bool(action.get("graceful", True)))
        self.graceful_wait_ms = duration_var(action.get("graceful_wait_ms", 2000))
        self.tree = tk.BooleanVar(value=bool(action.get("tree", False)))
        self.elevated_retry = tk.BooleanVar(value=bool(action.get("elevated_retry", True)))
        self.delay = duration_var(action.get("delay_ms", 0))
        self.after_delay = duration_var(action.get("after_delay_ms", 0))

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="进程名").grid(row=0, column=0, sticky="w", pady=8)
        name_row = ttk.Frame(body)
        name_row.grid(row=0, column=1, sticky="ew")
        ttk.Entry(name_row, textvariable=self.name).pack(side="left", fill="x", expand=True)
        ttk.Button(name_row, text="选择…", command=self.choose).pack(
            side="left", padx=(6, 0),
        )
        ttk.Label(
            body,
            text="填任务管理器里的映像名称，如 clash-verge.exe；同名的所有进程都会被结束。",
            foreground=COLOR_MUTED, wraplength=480,
        ).grid(row=1, column=1, sticky="w")

        dark_checkbutton(
            body, "先发送关闭请求（优雅退出），超时后强制结束", self.graceful,
        ).grid(row=2, column=1, sticky="w", pady=(10, 0))
        ttk.Label(body, text="优雅退出等待").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=60000, increment=100,
            textvariable=self.graceful_wait_ms, width=10,
        ).grid(row=3, column=1, sticky="ew")

        dark_checkbutton(
            body, "连同其子进程一起结束（进程树，慎用）", self.tree,
        ).grid(row=4, column=1, sticky="w", pady=(8, 0))
        dark_checkbutton(
            body, "普通权限结束失败时以管理员权限重试（会弹出 UAC 授权窗口）", self.elevated_retry,
        ).grid(row=5, column=1, sticky="w", pady=(4, 0))

        ttk.Label(body, text="执行前延时").grid(row=6, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.delay, width=10,
        ).grid(row=6, column=1, sticky="ew")
        ttk.Label(body, text="执行后延时").grid(row=7, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.after_delay, width=10,
        ).grid(row=7, column=1, sticky="ew")

        ttk.Label(
            body,
            text="执行到这一行时会结束指定软件，再继续后面的动作；进程不存在时自动跳过。"
            "普通权限反复强制结束仍失败（通常是目标软件以管理员身份运行）时，会尝试以管理员权限结束并弹出 UAC 授权窗口。",
            foreground=COLOR_MUTED, wraplength=480,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

        # 固定 400 高度在打包后的 EXE（按真实 DPI 渲染）里会装不下内容：
        # 行数多、两条长说明文字在高 DPI 下换行更多，底部按钮行被挤出窗口，
        # 确定/取消按钮完全看不见。按内容实际需求重设窗口尺寸并重新居中。
        fit_window_to_content(self, parent, minimum_width=560, minimum_height=400)

    def choose(self):
        names = running_process_names()
        if not names:
            show_floating_notice(self, "无法枚举进程", "读取进程列表失败。")
            return
        picker = tk.Toplevel(self)
        picker.title("选择正在运行的进程")
        picker.configure(background=COLOR_BG)
        picker.geometry("380x420")
        picker.transient(self)
        frame = ttk.Frame(picker, padding=12)
        frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(
            frame, font=("Consolas", 10),
            background=COLOR_SURFACE, foreground=COLOR_TEXT,
            selectbackground=COLOR_BLUE_SELECTION,
            highlightthickness=1, relief="solid",
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for name in names:
            listbox.insert("end", name)

        def confirm(_event=None):
            selection = listbox.curselection()
            if selection:
                self.name.set(listbox.get(selection[0]))
            picker.destroy()

        listbox.bind("<Double-Button-1>", confirm)
        buttons_row = ttk.Frame(frame)
        buttons_row.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons_row, text="取消", command=picker.destroy).pack(side="right")
        ttk.Button(buttons_row, text="确定", command=confirm).pack(side="right", padx=8)

    def save(self):
        name = self.name.get().strip()
        if not name:
            show_floating_notice(self, "进程名无效", "请输入要结束的进程名。")
            return
        try:
            graceful_wait_ms = max(0, int(self.graceful_wait_ms.get()))
            delay = max(0, int(self.delay.get()))
            after_delay = max(0, int(self.after_delay.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "延时必须是整数毫秒。")
            return
        self.result = {
            "type": "close_app",
            "name": name,
            "graceful": self.graceful.get(),
            "graceful_wait_ms": graceful_wait_ms,
            "tree": self.tree.get(),
            "elevated_retry": self.elevated_retry.get(),
            "delay_ms": delay,
            "after_delay_ms": after_delay,
        }
        self.destroy()


class ScriptDirectoriesDialog(ModalDialog):
    """Configure the save folders for the three script categories."""

    def __init__(self, parent, level_dir: str = "scripts/关卡",
                 level_pack_dir: str = "scripts/关卡封装",
                 switch_dir: str = "scripts/切换",
                 direction_dir: str = DIRECTION_SCRIPTS_DIR):
        super().__init__(parent, "脚本保存目录", 560, 420)
        self.level_dir = tk.StringVar(value=level_dir or "scripts/关卡")
        self.level_pack_dir = tk.StringVar(value=level_pack_dir or "scripts/关卡封装")
        self.switch_dir = tk.StringVar(value=switch_dir or "scripts/切换")
        self.direction_dir = tk.StringVar(value=direction_dir or DIRECTION_SCRIPTS_DIR)
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate((
            ("关卡脚本目录", self.level_dir),
            ("关卡封装脚本目录", self.level_pack_dir),
            ("切换脚本目录", self.switch_dir),
            ("方向脚本目录", self.direction_dir),
        )):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            row_frame = ttk.Frame(body)
            row_frame.grid(row=row, column=1, sticky="ew")
            ttk.Entry(row_frame, textvariable=variable).pack(side="left", fill="x", expand=True)
            ttk.Button(
                row_frame, text="浏览…", width=7,
                command=lambda var=variable: self._browse(var),
            ).pack(side="left", padx=(6, 0))
        ttk.Label(
            body,
            text="脚本类别包含关卡、关卡封装、切换和方向；方向目录的脚本供快捷键绑定执行。工作流全局与脚本全局属于模块类别。可填绝对路径或相对路径。",
            foreground=COLOR_MUTED, wraplength=480,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def _browse(self, variable):
        path = filedialog.askdirectory(
            parent=self, title="选择脚本保存目录",
            initialdir=str(Path(variable.get()).resolve()) if variable.get().strip() else str(BASE_DIR),
        )
        if path:
            variable.set(path)

    def save(self):
        self.result = {
            "level_dir": self.level_dir.get().strip() or "scripts/关卡",
            "level_pack_dir": self.level_pack_dir.get().strip() or "scripts/关卡封装",
            "switch_dir": self.switch_dir.get().strip() or "scripts/切换",
            "direction_dir": self.direction_dir.get().strip() or DIRECTION_SCRIPTS_DIR,
        }
        self.destroy()


# 不能单独作为快捷键的键：纯修饰键（按住才有意义）与系统状态切换键。
HOTKEY_DISALLOWED_NAMES = {
    "SHIFT", "CTRL", "ALT", "LWIN", "RWIN",
    "CAPSLOCK", "NUMLOCK", "SCROLLLOCK", "PAUSE",
}


class HotkeyBindingDialog(ModalDialog):
    """Capture one hotkey key and pick the script it runs."""

    def __init__(self, parent, current: dict | None = None):
        super().__init__(parent, "设置快捷键绑定", 580, 300)
        self.current = dict(current) if current else None
        self.key_vk = int((current or {}).get("vk") or 0)
        self.key_name_var = tk.StringVar(
            value=str((current or {}).get("key", "")) if current else ""
        )
        self.script_var = tk.StringVar(
            value=str((current or {}).get("script", "")) if current else ""
        )
        self._capturer = None
        self._script_labels: dict[str, Path] = {}
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="快捷键").grid(row=0, column=0, sticky="w", pady=8)
        key_row = ttk.Frame(body)
        key_row.grid(row=0, column=1, sticky="ew")
        self.key_label = ttk.Label(
            key_row, text=self.key_name_var.get() or "未设置",
            foreground=COLOR_MUTED, width=16,
        )
        self.key_label.pack(side="left")
        ttk.Button(
            key_row, text="按下新键…", width=10,
            command=self._capture_key,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(body, text="执行脚本").grid(row=1, column=0, sticky="w", pady=8)
        script_row = ttk.Frame(body)
        script_row.grid(row=1, column=1, sticky="ew")
        self.script_box = ttk.Combobox(
            script_row, textvariable=self.script_var, width=34,
        )
        self.script_box.pack(side="left", fill="x", expand=True)
        self.script_box.bind("<MouseWheel>", lambda _event: "break")
        self._refresh_script_options()
        ttk.Label(
            body,
            text="执行脚本只能从「scripts/方向」目录选择（下拉只显示脚本名）；按快捷键立即执行该脚本（快捷键本身不会录进当前脚本，脚本回放的按键与鼠标会被录进）。",
            foreground=COLOR_MUTED, wraplength=500,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

        # 固定 300 高度在打包后的 EXE（按真实 DPI 渲染）里会装不下内容：
        # 高 DPI 下各行与说明文字的实际需求高度超过窗口，按钮行被挤出窗口。
        # 按内容实际需求重设窗口尺寸并重新居中（与仓库其他对话框一致）。
        fit_window_to_content(self, parent, minimum_width=580, minimum_height=300)

    def _refresh_script_options(self):
        root = resolve_path(DIRECTION_SCRIPTS_DIR)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        labels = {}
        try:
            files = direction_script_files()
        except Exception:
            files = []
        for path in files:
            # 下拉只显示脚本名，不显示父辈目录。
            labels[path.stem] = path
        saved = self.script_var.get().strip()
        if saved:
            candidate = resolve_path(saved)
            try:
                resolved = candidate.resolve()
                inside = resolved == root.resolve() or root.resolve() in resolved.parents
            except OSError:
                inside = False
            if candidate.is_file() and inside:
                # 已保存的是相对/绝对路径：回填为脚本名。
                self.script_var.set(candidate.stem)
        self._script_labels = labels
        self.script_box.configure(values=list(labels))

    def _capture_key(self):
        self.key_label.configure(text="请按键…")

        def on_key(vk):
            def apply():
                name = vk_to_key_name(vk)
                if name in HOTKEY_DISALLOWED_NAMES:
                    self.key_label.configure(text=self.key_name_var.get() or "未设置")
                    show_floating_notice(
                        self, "快捷键不可用",
                        f"{name} 是系统功能键，不能单独作为快捷键。",
                    )
                    return
                self.key_vk = int(vk)
                self.key_name_var.set(name)
                self.key_label.configure(text=name)
            try:
                self.after(0, apply)
            except tk.TclError:
                pass

        def on_cancel():
            def apply():
                self.key_label.configure(text=self.key_name_var.get() or "未设置")
            try:
                self.after(0, apply)
            except tk.TclError:
                pass

        capturer = KeyCapturer(on_key, on_cancel)
        self._capturer = capturer
        if not capturer.start():
            self._capturer = None
            self.key_label.configure(text="无法捕获按键")
            return

    def destroy(self):
        capturer = self._capturer
        if capturer is not None:
            try:
                capturer.stop()
            except Exception:
                pass
        self._capturer = None
        super().destroy()

    def save(self):
        if not self.key_vk:
            show_floating_notice(self, "缺少快捷键", "请先点击“按下新键…”设置快捷键。")
            return
        name = vk_to_key_name(self.key_vk)
        if name in HOTKEY_DISALLOWED_NAMES:
            show_floating_notice(self, "快捷键不可用", f"{name} 是系统功能键，不能单独作为快捷键。")
            return
        if self.key_vk in RESERVED_HOTKEY_VKS:
            show_floating_notice(self, "快捷键不可用", "F8/F9/F12 已被录制、执行与紧急停止占用。")
            return
        raw = self.script_var.get().strip()
        path = self._script_labels.get(raw)
        if path is None:
            # 只允许 scripts/方向 目录里的脚本：按名字或路径解析后校验目录。
            candidate = resolve_path(raw)
            root = resolve_path(DIRECTION_SCRIPTS_DIR)
            try:
                resolved = candidate.resolve()
                inside = resolved == root.resolve() or root.resolve() in resolved.parents
            except OSError:
                inside = False
            if candidate.is_file() and inside:
                path = candidate
        if path is None:
            show_floating_notice(
                self, "缺少脚本",
                f"只能选择「{DIRECTION_SCRIPTS_DIR}」目录中的脚本（下拉只显示脚本名）。",
            )
            return
        self.result = {"key": name, "vk": self.key_vk, "script": display_path(path)}
        self.destroy()


class HotkeyScriptsDialog(ModalDialog):
    """Manage the list of hotkey → script bindings."""

    def __init__(self, parent, bindings: list[dict] | None = None):
        super().__init__(parent, "快捷键脚本", 720, 470)
        self.bindings = [dict(item) for item in (bindings or [])]
        top = ttk.Frame(self, padding=(18, 14, 18, 6))
        top.pack(fill="x")
        ttk.Label(
            top, text="在录制或执行脚本的过程中，按下快捷键立即执行绑定的脚本。",
            foreground=COLOR_MUTED,
        ).pack(anchor="w")
        ttk.Label(
            top, text="例如把 J 绑定到“转向左 90°”脚本：录制时按 J，转向操作会被录进当前脚本。",
            foreground=COLOR_MUTED,
        ).pack(anchor="w", pady=(4, 0))
        frame = ttk.Frame(self, padding=(18, 6, 18, 8))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            frame, columns=("key", "script"), show="headings", selectmode="browse",
        )
        self.tree.heading("key", text="快捷键")
        self.tree.heading("script", text="脚本")
        self.tree.column("key", width=110, anchor="center")
        self.tree.column("script", width=460, stretch=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _: self._edit_selected())
        side = ttk.Frame(frame)
        side.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(side, text="添加", command=self._add).pack(fill="x")
        ttk.Button(side, text="编辑", command=self._edit_selected).pack(fill="x", pady=(6, 0))
        ttk.Button(side, text="删除", command=self._remove_selected).pack(fill="x", pady=(6, 0))
        ttk.Button(side, text="清空", command=self._clear_all).pack(fill="x", pady=(6, 0))
        bottom = ttk.Frame(self, padding=(18, 8, 18, 14))
        bottom.pack(fill="x")
        ttk.Label(
            bottom, text="F8/F9/F12 为系统功能键，不可绑定；快捷键脚本按纯动作执行。",
            foreground=COLOR_MUTED,
        ).pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="保存", command=self.save).pack(side="right", padx=8)
        self._render()

        # 固定 720×470 在打包后的 EXE（按真实 DPI 渲染）里会装不下内容：
        # 高 DPI 下列表行高、按钮和说明文字的需求尺寸都变大，底部按钮行被
        # 挤出窗口；脚本列固定 530px 也常被右侧按钮区挤出。按内容实际需求
        # 重设窗口尺寸并重新居中，脚本列随窗口拉伸（与仓库其他对话框一致）。
        fit_window_to_content(self, parent, minimum_width=760, minimum_height=470)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.bindings):
            script = str(item.get("script", ""))
            name = Path(script).stem or "未设置"
            # 只显示脚本名，不显示父辈目录。
            self.tree.insert(
                "", "end", iid=str(index),
                values=(str(item.get("key", "")), name),
            )

    def _add(self):
        self._edit_index(None)

    def _edit_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        self._edit_index(int(selected[0]))

    def _edit_index(self, index: int | None):
        current = self.bindings[index] if index is not None else None
        dialog = HotkeyBindingDialog(self, current)
        result = dialog.show()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        if result is None:
            return
        key = str(result.get("key", ""))
        if any(
            str(item.get("key", "")).upper() == key.upper()
            for pos, item in enumerate(self.bindings)
            if pos != index
        ):
            show_floating_notice(self, "快捷键重复", f"快捷键 {key} 已被其他绑定使用。")
            return
        if index is None:
            self.bindings.append(result)
        else:
            self.bindings[index] = result
        self._render()

    def _remove_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        self.bindings.pop(int(selected[0]))
        self._render()

    def _clear_all(self):
        self.bindings = []
        self._render()

    def save(self):
        self.result = list(self.bindings)
        self.destroy()


class WindowPicker(ModalDialog):
    def __init__(self, parent):
        super().__init__(parent, "选择要绑定的窗口", 820, 520)
        self.windows: list[WindowInfo] = []
        self.search_var = tk.StringVar()
        self.dragging = False
        top = ttk.Frame(self, padding=14)
        top.pack(fill="x")
        ttk.Label(top, text="搜索窗口").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.search_var, width=42)
        entry.pack(side="left", padx=10)
        entry.bind("<KeyRelease>", lambda _: self._render())
        self.drag_handle = tk.Label(
            top, text="✚", width=3, cursor="crosshair",
            background=COLOR_BLUE_SELECTION, foreground=COLOR_TEXT,
            font=("Segoe UI Symbol", 13), relief="raised", bd=1,
        )
        self.drag_handle.pack(side="right", padx=(8, 0))
        self.drag_handle.bind("<ButtonPress-1>", self._drag_start)
        self.drag_handle.bind("<B1-Motion>", self._drag_motion)
        self.drag_handle.bind("<ButtonRelease-1>", self._drag_release)
        ttk.Label(top, text="按住十字拖到目标窗口后松开", foreground=COLOR_MUTED).pack(side="right", padx=(8, 0))
        ttk.Button(top, text="刷新", command=self.refresh).pack(side="right")
        frame = ttk.Frame(self, padding=(14, 0, 14, 8))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("title", "class"), show="headings", selectmode="browse")
        self.tree.heading("title", text="窗口标题")
        self.tree.heading("class", text="窗口类")
        self.tree.column("title", width=540)
        self.tree.column("class", width=220)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _: self.choose())
        bottom = ttk.Frame(self, padding=14)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="仅显示当前可见且有标题的顶层窗口。", foreground=COLOR_MUTED).pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="绑定所选窗口", command=self.choose).pack(side="right", padx=8)
        self.refresh()

    def refresh(self):
        self.windows = selectable_target_windows(enum_windows())
        self._render()

    def _render(self):
        query = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.windows):
            if query and query not in item.title.lower() and query not in item.class_name.lower():
                continue
            self.tree.insert("", "end", iid=str(index), values=(item.title, item.class_name))

    def choose(self):
        selected = self.tree.selection()
        if not selected:
            show_floating_notice(self, "请选择", "请先选择一个窗口。")
            return
        self.result = self.windows[int(selected[0])]
        self.destroy()


    def _drag_start(self, _event):
        self.dragging = True
        self.configure(cursor="crosshair")
        self.drag_handle.configure(text="●")

    def _drag_motion(self, _event):
        if not self.dragging:
            return
        x, y = get_cursor_pos()
        info = window_from_point(x, y)
        if info and not is_current_process_window(info.hwnd):
            self.title(f"拖放选择：{info.title or info.class_name}")
        else:
            self.title("选择要绑定的窗口")

    def _drag_release(self, _event):
        if not self.dragging:
            return
        self.dragging = False
        self.configure(cursor="")
        self.drag_handle.configure(text="✚")
        x, y = get_cursor_pos()
        info = window_from_point(x, y)
        if info and not is_current_process_window(info.hwnd):
            self.result = info
            self.destroy()
        else:
            self.title("选择要绑定的窗口")


KEY_HINT_DEFAULT = "例：A、F5、ENTER、SPACE、CTRL、LEFT、VK_0x41"
KEY_HINT_CAPTURING = "请按下要插入的按键（F8 / F9 / F12 为软件快捷键）…按 Esc 取消"


class KeyActionDialog(ModalDialog):
    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "添加键盘动作", 560, 330)
        self._source = dict(action or {})
        action = action or {}
        self.mode = tk.StringVar(value="press" if action.get("type") == "key_press" else ("down" if action.get("down", True) else "up"))
        self.key = tk.StringVar(value=str(action.get("name", "A")))
        self.hold = duration_var(action.get("hold_ms", 30))
        self.delay = duration_var(action.get("delay_ms", 0))
        self.capturer: KeyCapturer | None = None
        self.capture_hint = tk.StringVar(value=KEY_HINT_DEFAULT)
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="按键", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=8)
        key_row = ttk.Frame(body)
        key_row.grid(row=0, column=1, sticky="ew", pady=8)
        ttk.Entry(key_row, textvariable=self.key).pack(side="left", fill="x", expand=True)
        self.capture_button = ttk.Button(key_row, text="检测按键…", command=self.start_capture)
        self.capture_button.pack(side="left", padx=(8, 0))
        ttk.Label(body, textvariable=self.capture_hint, foreground=COLOR_MUTED).grid(row=1, column=1, sticky="w")
        ttk.Label(body, text="动作").grid(row=2, column=0, sticky="w", pady=14)
        box = ttk.Combobox(body, textvariable=self.mode, values=("press", "down", "up"), state="readonly")
        box.grid(row=2, column=1, sticky="ew")
        ttk.Label(body, text="按住时长").grid(row=3, column=0, sticky="w", pady=8)
        ttk.Entry(body, textvariable=self.hold).grid(row=3, column=1, sticky="ew")
        ttk.Label(body, text="执行前延时").grid(row=4, column=0, sticky="w", pady=8)
        ttk.Entry(body, textvariable=self.delay).grid(row=4, column=1, sticky="ew")
        body.columnconfigure(1, weight=1)
        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def start_capture(self):
        if self.capturer is not None:
            return
        self.capture_button.configure(state="disabled")
        self.capture_hint.set(KEY_HINT_CAPTURING)
        self.capturer = KeyCapturer(
            on_key=lambda vk: self.after(0, self._apply_captured_key, vk),
            on_cancel=lambda: self.after(0, self._cancel_capture),
        )
        if not self.capturer.start():
            self._end_capture()
            show_floating_notice(self, "按键检测失败", "无法启动按键检测，请重试")

    def _apply_captured_key(self, vk: int):
        self.key.set(vk_to_key_name(vk))
        self._end_capture()

    def _cancel_capture(self):
        self._end_capture()

    def _end_capture(self):
        self.capture_hint.set(KEY_HINT_DEFAULT)
        if self.capturer is not None:
            self.capturer.stop()
            self.capturer = None
        try:
            self.capture_button.configure(state="normal")
        except tk.TclError:
            pass

    def destroy(self):
        if self.capturer is not None:
            self.capturer.stop()
            self.capturer = None
        super().destroy()

    def save(self):
        try:
            vk, name = key_to_vk(self.key.get())
            hold = max(1, int(self.hold.get()))
            delay = max(0, int(self.delay.get()))
        except ValueError as exc:
            show_floating_notice(self, "参数错误", str(exc))
            return
        if self.mode.get() == "press":
            updated = dict(getattr(self, "_source", None) or {})
            updated.update({"type": "key_press", "vk": vk, "name": name, "hold_ms": hold, "delay_ms": delay})
            updated.pop("down", None)
            self.result = updated
        else:
            updated = dict(getattr(self, "_source", None) or {})
            updated.update({"type": "key", "vk": vk, "name": name, "down": self.mode.get() == "down", "delay_ms": delay})
            updated.pop("hold_ms", None)
            self.result = updated
        self.destroy()


class MouseMoveDialog(ModalDialog):
    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "添加鼠标移动", 480, 300)
        self._source = dict(action or {})
        action = action or {}
        self.mode = tk.StringVar(value=action.get("mode", "absolute"))
        self.x = tk.StringVar(value=str(action.get("x", action.get("dx", 0))))
        self.y = tk.StringVar(value=str(action.get("y", action.get("dy", 0))))
        self.delay = duration_var(action.get("delay_ms", 0))
        self.picker = None
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        labels = (("坐标模式", self.mode), ("X / ΔX", self.x), ("Y / ΔY", self.y), ("执行前延时", self.delay))
        for row, (label, variable) in enumerate(labels):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=9)
            if row == 0:
                combo = ttk.Combobox(body, textvariable=variable, values=("absolute", "relative"), state="readonly")
                combo.grid(row=row, column=1, sticky="ew")
                combo.bind("<<ComboboxSelected>>", self._mode_changed)
            elif variable is self.x:
                frame = ttk.Frame(body)
                frame.grid(row=row, column=1, sticky="ew")
                ttk.Entry(frame, textvariable=variable).pack(side="left", fill="x", expand=True)
                self.pick_button = ttk.Button(frame, text="点击屏幕选取…", command=self.start_pick_position)
                self.pick_button.pack(side="left", padx=(8, 0))
            else:
                entry = ttk.Entry(body, textvariable=variable)
                entry.grid(row=row, column=1, sticky="ew")
        self._update_pick_label()
        body.columnconfigure(1, weight=1)
        buttons = ttk.Frame(self, padding=(22, 0, 22, 18))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def _mode_changed(self, _event=None):
        self._update_pick_label()

    def _update_pick_label(self):
        if self.mode.get() == "relative":
            self.pick_button.configure(text="两点测量…")
        else:
            self.pick_button.configure(text="点击屏幕选取…")

    def start_pick_position(self):
        two_points = self.mode.get() == "relative"
        self.picker = ScreenPointPicker(
            self, self.master, self._apply_picked_point, two_points=two_points,
            tip_text=(
                "第一次点击记录起点，移动光标到终点后再次点击，得到 ΔX/ΔY；Esc 取消"
                if two_points else
                "点击要移动到的位置；只记录坐标，不会点击下方窗口；Esc 取消"
            ),
        )
        self.picker.start()

    def _apply_picked_point(self, *coords):
        if self.mode.get() == "relative":
            start_x, start_y, end_x, end_y = coords
            self.x.set(str(int(end_x) - int(start_x)))
            self.y.set(str(int(end_y) - int(start_y)))
        else:
            self.x.set(str(int(coords[0])))
            self.y.set(str(int(coords[1])))

    def save(self):
        try:
            x, y, delay = int(self.x.get()), int(self.y.get()), max(0, int(self.delay.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "坐标和延时必须是整数。")
            return
        updated = dict(getattr(self, "_source", None) or {})
        updated["type"] = "mouse_move"
        updated["mode"] = self.mode.get()
        updated["delay_ms"] = delay
        if self.mode.get() == "relative":
            updated["dx"] = x
            updated["dy"] = y
            updated.pop("x", None)
            updated.pop("y", None)
        else:
            updated["x"] = x
            updated["y"] = y
            updated.pop("dx", None)
            updated.pop("dy", None)
        self.result = updated
        self.destroy()


class ClickDialog(ModalDialog):
    """Edit a mouse click; also handles recorded 'mouse_button' actions (按下/松开)."""

    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "编辑鼠标点击" if action else "添加鼠标点击", 480, 385)
        self._source = dict(action or {})
        action = action or {}
        self.kind = str(action.get("type", "click"))
        cursor = get_cursor_pos()
        self.button = tk.StringVar(value=action.get("button", "left"))
        self.x = tk.StringVar(value=str(action.get("x", cursor[0])))
        self.y = tk.StringVar(value=str(action.get("y", cursor[1])))
        self.hold = duration_var(action.get("hold_ms", 30))
        # 录制的点击原本不带延时，编辑时默认 0，避免无意间改变播放节奏。
        self.delay = duration_var(action.get("delay_ms", 1000 if self.kind == "click" else 0))
        self.down = tk.StringVar(value="按下" if action.get("down", True) else "松开")
        self.pos_mode = tk.BooleanVar(value=action.get("pos_mode") == "current")
        self.picker = None
        self._x_entry = None
        self._y_entry = None
        self._pick_button = None
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        if self.kind == "mouse_button":
            values: list[tuple[str, tk.StringVar | None]] = [
                ("鼠标键", self.button), ("状态", None), ("屏幕 X", self.x), ("屏幕 Y", self.y),
                ("执行前延时", self.delay),
            ]
            row_offset = 0
        else:
            values = [
                ("鼠标键", self.button), ("屏幕 X", self.x), ("屏幕 Y", self.y),
                ("按住时长", self.hold), ("执行前延时", self.delay),
            ]
            row_offset = 1
            ttk.Checkbutton(
                body,
                text="点击鼠标当前位置（执行时不移动鼠标）",
                variable=self.pos_mode,
                command=self._update_pos_mode,
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        for row, (label, variable) in enumerate(values):
            grid_row = row + row_offset
            ttk.Label(body, text=label).grid(row=grid_row, column=0, sticky="w", pady=8)
            if row == 0:
                ttk.Combobox(body, textvariable=variable, values=("left", "right", "middle"), state="readonly").grid(row=grid_row, column=1, sticky="ew")
            elif self.kind == "mouse_button" and variable is None:
                ttk.Combobox(body, textvariable=self.down, values=("按下", "松开"), state="readonly").grid(row=grid_row, column=1, sticky="ew")
            elif variable is self.x:
                frame = ttk.Frame(body)
                frame.grid(row=grid_row, column=1, sticky="ew")
                self._x_entry = ttk.Entry(frame, textvariable=variable)
                self._x_entry.pack(side="left", fill="x", expand=True)
                if self.kind != "mouse_button":
                    self._pick_button = ttk.Button(
                        frame, text="点击屏幕选取…", command=self.start_pick_position,
                    )
                    self._pick_button.pack(side="left", padx=(8, 0))
            else:
                entry = ttk.Entry(body, textvariable=variable)
                entry.grid(row=grid_row, column=1, sticky="ew")
                if variable is self.y:
                    self._y_entry = entry
        body.columnconfigure(1, weight=1)
        buttons = ttk.Frame(self, padding=(22, 0, 22, 18))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)
        self._update_pos_mode()

    def _update_pos_mode(self, _event=None):
        current = self.pos_mode.get()
        state = "disabled" if current else "normal"
        for entry in (self._x_entry, self._y_entry):
            if entry is not None:
                entry.configure(state=state)
        if self._pick_button is not None:
            self._pick_button.configure(state="disabled" if current else "normal")

    def start_pick_position(self):
        self.picker = ScreenPointPicker(
            self, self.master, self._apply_picked_point,
            tip_text="点击要执行操作的位置；只记录坐标，不会点击下方窗口；Esc 取消",
            hidden_windows=ancestor_windows(self.master),
        )
        self.picker.start()

    def _apply_picked_point(self, x, y):
        self.x.set(str(int(x)))
        self.y.set(str(int(y)))

    def save(self):
        try:
            x, y = int(self.x.get()), int(self.y.get())
            delay = max(0, int(self.delay.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "坐标和时间必须是整数。")
            return
        if self.kind == "mouse_button":
            updated = dict(getattr(self, "_source", None) or {})
            updated.update({
                "type": "mouse_button", "button": self.button.get(),
                "down": self.down.get() == "按下", "x": x, "y": y,
                "delay_ms": delay,
            })
            updated.pop("hold_ms", None)
            self.result = updated
            self.destroy()
            return
        try:
            hold = max(1, int(self.hold.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "时间必须是整数。")
            return
        updated = dict(getattr(self, "_source", None) or {})
        updated.update({
            "type": "click", "button": self.button.get(),
            "x": x, "y": y, "hold_ms": hold, "delay_ms": delay,
        })
        # 固定坐标时不写 pos_mode 字段（旧文件零迁移）；勾选当前位置才标记。
        if (getattr(self, "pos_mode", None) is not None and self.pos_mode.get()):
            updated["pos_mode"] = "current"
        else:
            updated.pop("pos_mode", None)
        self.result = updated
        self.destroy()


DEFAULT_GAME_SETUP_NOTE = """使用本软件前，建议在游戏中完成以下设置（可自行修改补充）：

1. 分辨率：使用与录制时一致的分辨率（默认 1920×1080），脚本坐标会按分辨率缩放。
2. 鼠标灵敏度：录制与执行时保持一致，灵敏度变化会导致转向/瞄准幅度不同。
3. 鼠标加速：关闭游戏内鼠标加速和系统「提高指针精确度」，否则转向偏移不准。
4. 窗口模式：使用全屏或窗口化运行，不要最小化；执行时游戏窗口需保持在前台。
5. 输入法：系统需安装「英语（美国）」键盘，执行时会自动切换到英语输入法。
6. 管理员权限：以管理员身份运行本软件，否则系统级输入锁定（专注模式）可能失败。
7. 绑定窗口：进入游戏后，在软件侧栏点击「绑定窗口」选择游戏窗口。
8. 快捷键：F8 开始/停止录制，F9 执行当前脚本，F12 紧急停止。"""


class GameSetupNoteDialog(ModalDialog):
    """查看/编辑使用本软件前游戏需要设置的参数说明（文字可自行修改）。"""

    def __init__(self, parent, initial_text: str | None = None):
        super().__init__(parent, "游戏设置说明", 660, 480)
        body = ttk.Frame(self, padding=(16, 14))
        body.pack(fill="both", expand=True)
        editor = ttk.Frame(body)
        editor.pack(fill="both", expand=True)
        text = tk.Text(
            editor, wrap="word", undo=True,
            background=COLOR_SURFACE, foreground=COLOR_TEXT,
            insertbackground=COLOR_TEXT, selectbackground=COLOR_BLUE_SELECTION,
            relief="flat", borderwidth=0, padx=10, pady=8,
            font=("Microsoft YaHei UI", 11),
        )
        scroll = ttk.Scrollbar(editor, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        text.insert("1.0", initial_text if initial_text is not None else DEFAULT_GAME_SETUP_NOTE)
        self.text = text
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="恢复默认", command=self._restore_default).pack(side="left")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def _restore_default(self):
        if self.text.get("1.0", "end-1c").strip() != DEFAULT_GAME_SETUP_NOTE.strip() \
                and not messagebox.askyesno(
                    "恢复默认", "将清空当前修改并恢复默认说明，确定吗？", parent=self,
                ):
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", DEFAULT_GAME_SETUP_NOTE)

    def save(self):
        self.result = self.text.get("1.0", "end-1c")
        self.destroy()


class TurnActionDialog(ModalDialog):
    """添加/编辑鼠标转向动作：鼠标相对移动 ΔX/ΔY，不按键（用于游戏内转向）。"""

    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "编辑鼠标转向" if action else "添加鼠标转向", 420, 250)
        self._source = dict(action or {})
        self.dx = tk.StringVar(value=str(self._source.get("dx", 0)))
        self.dy = tk.StringVar(value=str(self._source.get("dy", 0)))
        self.delay = duration_var(self._source.get("delay_ms", 0))
        body = ttk.Frame(self, padding=(16, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="ΔX").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.dx, width=8).grid(row=0, column=1, sticky="ew")
        ttk.Label(body, text="ΔY").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.dy, width=8).grid(row=1, column=1, sticky="ew")
        ttk.Label(body, text="执行前延时").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.delay, width=8).grid(row=2, column=1, sticky="ew")
        ttk.Label(
            body,
            text="鼠标相对移动量，不按键（游戏内转向）；ΔX 正值向右，ΔY 正值向下。",
            foreground=COLOR_MUTED,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)
        # 窗口尺寸按内容实际需求收敛（防高 DPI 下内容被裁掉），下限只保证
        # 不会缩得过分，不再把窗口撑出大片空白。
        fit_window_to_content(self, parent, minimum_width=300, minimum_height=210)

    def save(self):
        try:
            dx = int(self.dx.get())
            dy = int(self.dy.get())
        except ValueError:
            show_floating_notice(self, "参数错误", "ΔX 和 ΔY 必须是整数。")
            return
        # 基于原动作更新，保留未在对话框中展示的字段（执行后延时、步数等）。
        updated = dict(getattr(self, "_source", None) or {})
        updated["type"] = "turn"
        updated["dx"] = dx
        updated["dy"] = dy
        updated["delay_ms"] = max(0, int(self.delay.get()))
        self.result = updated
        self.destroy()


class RepeatClickDialog(ModalDialog):
    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "添加连续点击", 480, 400)
        self._source = dict(action or {})
        action = action or {}
        cursor = get_cursor_pos()
        self.button = tk.StringVar(value=action.get("button", "left"))
        self.x = tk.StringVar(value=str(action.get("x", cursor[0])))
        self.y = tk.StringVar(value=str(action.get("y", cursor[1])))
        self.count = tk.StringVar(value=str(action.get("count", 2)))
        self.interval = duration_var(action.get("interval_ms", 100))
        self.hold = duration_var(action.get("hold_ms", 30))
        self.delay = duration_var(action.get("delay_ms", 1000))
        self.picker = None
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        values = (
            ("鼠标键", self.button),
            ("屏幕 X", self.x),
            ("屏幕 Y", self.y),
            ("点击次数", self.count),
            ("点击间隔", self.interval),
            ("按住时长", self.hold),
            ("执行前延时", self.delay),
        )
        for row, (label, variable) in enumerate(values):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            if variable is self.button:
                ttk.Combobox(body, textvariable=variable, values=("left", "right", "middle"),
                             state="readonly").grid(row=row, column=1, sticky="ew")
            elif variable is self.x:
                frame = ttk.Frame(body)
                frame.grid(row=row, column=1, sticky="ew")
                ttk.Entry(frame, textvariable=variable).pack(side="left", fill="x", expand=True)
                ttk.Button(frame, text="点击屏幕选取…", command=self.start_pick_position).pack(side="left", padx=(8, 0))
            elif variable is self.y:
                entry = ttk.Entry(body, textvariable=variable)
                entry.grid(row=row, column=1, sticky="ew")
            else:
                ttk.Entry(body, textvariable=variable).grid(row=row, column=1, sticky="ew")
        buttons = ttk.Frame(body)
        buttons.grid(row=len(values), column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def start_pick_position(self):
        self.picker = ScreenPointPicker(
            self, self.master, self._apply_picked_point,
            tip_text="点击要连续点击的位置；只记录坐标，不会点击下方窗口；Esc 取消",
            hidden_windows=ancestor_windows(self.master),
        )
        self.picker.start()

    def _apply_picked_point(self, x, y):
        self.x.set(str(int(x)))
        self.y.set(str(int(y)))

    def save(self):
        try:
            x, y = int(self.x.get()), int(self.y.get())
            count = max(1, int(self.count.get()))
            interval = max(0, int(self.interval.get()))
            hold, delay = max(1, int(self.hold.get())), max(0, int(self.delay.get()))
        except ValueError:
            show_floating_notice(self, "参数错误", "坐标、次数和时间必须是整数。")
            return
        updated = dict(getattr(self, "_source", None) or {})
        updated.update({
            "type": "repeat_click",
            "button": self.button.get(),
            "x": x, "y": y,
            "count": count, "interval_ms": interval,
            "hold_ms": hold, "delay_ms": delay,
        })
        self.result = updated
        self.destroy()


class TextActionDialog(ModalDialog):
    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "编辑文本动作", 520, 280)
        self._source = dict(action or {})
        action = action or {}
        self.text_var = tk.StringVar(value=str(action.get("text", "")))
        self.char_delay = duration_var(action.get("char_delay_ms", 15))
        self.delay = duration_var(action.get("delay_ms", 0))
        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="文本内容").grid(row=0, column=0, sticky="w", pady=8)
        ttk.Entry(body, textvariable=self.text_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(body, text="字符间隔").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=10000, increment=1,
            textvariable=self.char_delay, width=10,
        ).grid(row=1, column=1, sticky="ew")
        ttk.Label(body, text="执行前延时").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Spinbox(
            body, from_=0, to=86400000, increment=100,
            textvariable=self.delay, width=10,
        ).grid(row=2, column=1, sticky="ew")
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def save(self):
        try:
            char_delay = max(0, min(10000, int(self.char_delay.get())))
            delay = max(0, min(86400000, int(self.delay.get())))
        except (tk.TclError, ValueError):
            show_floating_notice(self, "参数错误", "时间必须是整数。")
            return
        updated = dict(getattr(self, "_source", None) or {})
        updated.update({
            "type": "text",
            "text": self.text_var.get(),
            "char_delay_ms": char_delay,
            "delay_ms": delay,
        })
        self.result = updated
        self.destroy()


class ImageActionDialog(ModalDialog):
    def __init__(self, parent, action: dict | None = None, actions: list[dict] | None = None):
        super().__init__(parent, "添加识图动作", 650, 950)
        action = action_with_live_module_binding(action)
        default_on_found, default_result_notice = image_action_option_defaults(action)
        default_click_target, default_click_point = image_click_target_defaults(action)
        (default_on_timeout, default_timeout, default_delay, default_jump_row,
         default_timeout_delay) = image_timeout_option_defaults(action)
        jump_options = image_jump_target_options(actions or [])
        found_jump_options = image_found_jump_target_options(actions or [])
        self.jump_target_ids = {
            label: action_id for label, action_id in found_jump_options
        }
        saved_target_id = str(action.get("timeout_jump_action_id", "")).strip()
        selected_target = next(
            (label for label, action_id in jump_options if action_id == saved_target_id), "",
        )
        if not selected_target and not saved_target_id:
            if "timeout_jump_row" in action and 1 <= default_jump_row <= len(jump_options):
                selected_target = jump_options[default_jump_row - 1][0]
            elif jump_options:
                selected_target = jump_options[0][0]
        saved_found_target_id = str(action.get("found_jump_action_id", "")).strip()
        selected_found_target = next(
            (label for label, action_id in found_jump_options if action_id == saved_found_target_id), "",
        )
        if not selected_found_target and not saved_found_target_id:
            try:
                found_legacy_row = int(action.get("found_jump_row", 0))
            except (TypeError, ValueError):
                found_legacy_row = 0
            if 1 <= found_legacy_row <= len(jump_options):
                selected_found_target = jump_options[found_legacy_row - 1][0]
            elif jump_options:
                selected_found_target = jump_options[0][0]
            else:
                selected_found_target = found_jump_options[0][0]
        region = action.get("region", [0, 0, 0, 0])
        saved_module_key = str(action.get("module_key", "")).strip()
        saved_module = registered_module_object(saved_module_key) if saved_module_key else None
        self.module_key = tk.StringVar(value=saved_module_key)
        self.module_name = tk.StringVar(value=(
            str((saved_module or {}).get("name") or "").strip()
            or (Path(saved_module_key.replace("\\", "/")).stem if saved_module_key else "未选择模块")
        ))
        self.template = tk.StringVar(value=str(action.get("template", "")))
        self.threshold = tk.StringVar(value=str(action.get("threshold", 0.85)))
        self.timeout = duration_var(default_timeout)
        self.interval = duration_var(action.get("interval_ms", 250))
        self.region_mode = tk.StringVar(value=str(action.get("region_mode", "screen")))
        self.region = tk.StringVar(value=",".join(map(str, region)))
        self.on_found = tk.StringVar(value=default_on_found)
        self.found_jump_target = tk.StringVar(value=selected_found_target)
        self.found_delay = duration_var(action.get("found_delay_ms", 0))
        self.click_target_mode = tk.StringVar(value=default_click_target)
        self.click_point = tk.StringVar(value=",".join(map(str, default_click_point)))
        self.on_timeout = tk.StringVar(value=image_timeout_option_label(default_on_timeout))
        self.timeout_jump_target = tk.StringVar(value=selected_target)
        self.timeout_delay = duration_var(default_timeout_delay)
        self.wait_forever = tk.BooleanVar(value=bool(action.get("wait_forever", False)))
        self.fallback_template = tk.StringVar(value=str(action.get("fallback_template", "")))
        self.fallback_switch_ms = duration_var(action.get("fallback_switch_ms", 3000))
        fallback_region = action.get("fallback_region", [0, 0, 0, 0])
        self.fallback_region_mode = tk.StringVar(value=str(action.get("fallback_region_mode", "screen")))
        self.fallback_region = tk.StringVar(value=",".join(map(str, fallback_region)))
        self.fallback_click = tk.BooleanVar(value=bool(action.get("fallback_click", True)))
        fallback_on_match = str(action.get("fallback_on_match", "回到主模板的检测"))
        if fallback_on_match not in ("回到主模板的检测", "直接退出识别"):
            fallback_on_match = "回到主模板的检测"
        self.fallback_on_match = tk.StringVar(value=fallback_on_match)
        self.button = tk.StringVar(value=str(action.get("button", "left")))
        self.delay = duration_var(default_delay)
        self.after_delay = duration_var(action.get("after_delay_ms", 0))
        self.show_result_notice = tk.BooleanVar(value=default_result_notice)
        self.point_overlay = None
        self.point_screenshot = None
        self.main_previous_state = "normal"
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="模板").grid(row=0, column=0, sticky="w", pady=8)
        template_row = ttk.Frame(body)
        template_row.grid(row=0, column=1, sticky="ew")
        self.template_combo = ttk.Combobox(
            template_row, textvariable=self.template,
            values=registered_template_options(str(action.get("template", ""))),
            state="readonly",
        )
        self.template_combo.pack(side="left", fill="x", expand=True)
        self.template_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._clear_image_module_binding(),
        )
        ttk.Button(
            template_row, text="选择模块…", command=self.select_image_module,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            template_row, text="框选新建…", command=self.capture_custom_template,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(template_row, text="模板区域…", command=self.open_template_region_manager).pack(
            side="left", padx=(8, 0),
        )
        rows = [
            ("相似度 (0.1–1.0)", self.threshold, None),
            ("等待超时", self.timeout, None),
            ("检测间隔", self.interval, None),
            ("找到后", self.on_found, ("continue", "click", "jump")),
            ("找到后跳转目标动作", self.found_jump_target,
             tuple(label for label, _ in found_jump_options)),
            ("识别成功后等待", self.found_delay, None),
            ("点击位置", self.click_target_mode, ("识图区域中心", "自定义坐标")),
            ("自定义点击坐标 x,y", self.click_point, None),
            ("超时后", self.on_timeout, tuple(label for label, _value in IMAGE_TIMEOUT_OPTIONS)),
            ("超时后等待", self.timeout_delay, None),
            ("超时跳转目标动作", self.timeout_jump_target, tuple(label for label, _ in jump_options)),
            ("点击按钮", self.button, ("left", "right", "middle")),
            ("执行前延时", self.delay, None),
            ("执行后延时", self.after_delay, None),
        ]
        for offset, (label, variable, options) in enumerate(rows, start=1):
            ttk.Label(body, text=label).grid(row=offset, column=0, sticky="w", pady=8)
            if variable is self.click_point:
                point_row = ttk.Frame(body)
                point_row.grid(row=offset, column=1, sticky="ew")
                self.click_point_entry = ttk.Entry(point_row, textvariable=variable)
                self.click_point_entry.pack(side="left", fill="x", expand=True)
                self.click_point_button = ttk.Button(
                    point_row, text="幕布选取…", command=self.start_click_point_selection,
                )
                self.click_point_button.pack(side="left", padx=(8, 0))
            elif options or variable in (self.timeout_jump_target, self.found_jump_target):
                combo = ttk.Combobox(body, textvariable=variable, values=options, state="readonly")
                combo.grid(row=offset, column=1, sticky="ew")
                if variable is self.click_target_mode:
                    combo.bind("<<ComboboxSelected>>", self._click_target_changed)
                elif variable is self.on_found:
                    combo.bind("<<ComboboxSelected>>", self._found_action_changed)
                elif variable is self.on_timeout:
                    combo.bind("<<ComboboxSelected>>", self._timeout_action_changed)
                    self.timeout_combo = combo
                elif variable is self.found_jump_target:
                    self.found_jump_entry = combo
                elif variable is self.timeout_jump_target:
                    self.timeout_jump_entry = combo
            else:
                entry = ttk.Entry(body, textvariable=variable)
                entry.grid(row=offset, column=1, sticky="ew")
                if variable is self.timeout:
                    self.timeout_entry = entry
                elif variable is self.timeout_delay:
                    self.timeout_delay_entry = entry
        self._update_click_point_controls()
        self._update_found_jump_control()
        self._update_timeout_jump_control()
        dark_checkbutton(
            body,
            text="一直等待直到出现（不超时）",
            variable=self.wait_forever,
            command=self._update_wait_forever_controls,
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(body, text="备用模板").grid(row=16, column=0, sticky="w", pady=6)
        fallback_row = ttk.Frame(body)
        fallback_row.grid(row=16, column=1, sticky="ew")
        self.fallback_combo = ttk.Combobox(
            fallback_row, textvariable=self.fallback_template,
            values=fallback_template_options(str(action.get("fallback_template", ""))),
            state="readonly",
        )
        self.fallback_combo.pack(side="left", fill="x", expand=True)
        ttk.Label(body, text="备用切换超时").grid(row=17, column=0, sticky="w", pady=6)
        self.fallback_switch_entry = ttk.Entry(body, textvariable=self.fallback_switch_ms)
        self.fallback_switch_entry.grid(row=17, column=1, sticky="ew")
        self.fallback_click_button = dark_checkbutton(
            body,
            text="备用模板出现后点击它（不勾选则只检测不点击）",
            variable=self.fallback_click,
        )
        self.fallback_click_button.grid(row=18, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(body, text="备用出现后").grid(row=19, column=0, sticky="w", pady=6)
        self.fallback_action_combo = ttk.Combobox(
            body, textvariable=self.fallback_on_match,
            values=("回到主模板的检测", "直接退出识别"), state="readonly", width=18,
        )
        self.fallback_action_combo.grid(row=19, column=1, sticky="w")
        self._update_wait_forever_controls()
        dark_checkbutton(
            body,
            text="显示识别结果浮动提醒",
            variable=self.show_result_notice,
        ).grid(row=20, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(body, text="一直等待时：主模板超过切换超时未出现则改用备用模板，在模板的检测区域里识别；备用模板出现时可选是否点击，出现后回到主模板检测或直接退出识别。幕布选取只记录坐标；最小检测间隔为 50 ms。", foreground=COLOR_MUTED, wraplength=560).grid(row=21, column=0, columnspan=2, sticky="w", pady=(10, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=22, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def open_template_region_manager(self):
        TemplateRegionManagerDialog(self).show()
        self._refresh_template_options()

    def _clear_image_module_binding(self):
        module_key = getattr(self, "module_key", None)
        if module_key is not None:
            module_key.set("")
        module_name = getattr(self, "module_name", None)
        if module_name is not None:
            module_name.set("未选择模块")

    def select_image_module(self):
        binding = choose_module_binding(self, categories=("switch",))
        if not binding:
            return
        module_key = str(binding["module_key"])
        template = str(binding["template"])
        region = list(binding.get("region") or [])
        self.module_key.set(module_key)
        self.template.set(template)
        self.region_mode.set("template")
        self.region.set(",".join(map(str, region)))
        obj = registered_module_object(module_key) or {}
        self.module_name.set(
            str(obj.get("name") or "").strip()
            or Path(module_key.replace("\\", "/")).stem
        )
        self.template_combo.configure(values=registered_template_options(template))

    def _ancestors_to_hide(self):
        """Return windows above the image dialog so the capture is unobstructed."""
        windows = []
        seen = set()
        window = self.master
        while window is not None and len(windows) < 20:
            identity = id(window)
            if identity in seen:
                break
            seen.add(identity)
            try:
                parent = window.master
            except (AttributeError, tk.TclError):
                break
            if parent is None:
                break
            window = parent
            windows.append(window)
            try:
                if window.winfo_class() == "Tk":
                    break
            except tk.TclError:
                break
        return windows

    def capture_custom_template(self):
        """Capture an ad-hoc template and region for this image action only."""

        def on_result(region):
            try:
                images_dir = load_module_images_dir()
                images_dir.mkdir(parents=True, exist_ok=True)
                screen, _origin = capture_bgr(tuple(int(part) for part in region))
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
                path = images_dir / f"recognition_{stamp}.png"
                Image.fromarray(screen[:, :, ::-1]).save(path)
                template = display_path(path)
            except Exception as exc:
                show_floating_notice(self, "截图失败", str(exc))
                return
            self.template.set(template)
            self._clear_image_module_binding()
            self.region_mode.set("custom")
            self.region.set(",".join(map(str, region)))
            self.template_combo.configure(values=registered_template_options(template))

        self.picker = ScreenRegionPicker(
            self, self.master, on_result,
            hidden_windows=self._ancestors_to_hide(),
            tip_text=(
                "按住鼠标左键框选要识别的画面；松开后自动保存图片，"
                "并将该框作为此动作的检测区域，Esc 取消"
            ),
        )
        self.picker.start()

    def _refresh_template_options(self):
        current = self.template.get()
        if current and current not in load_template_regions() \
                and not resolve_path(current).is_file():
            # 管理器里已删除/改名的模板不再可用：清空，由保存校验提示重选。
            self.template.set("")
            current = ""
        self.template_combo.configure(values=registered_template_options(current))
        fallback = self.fallback_template.get()
        if fallback and fallback != "（不启用）" and fallback not in load_template_regions():
            self.fallback_template.set("（不启用）")
            fallback = "（不启用）"
        self.fallback_combo.configure(values=fallback_template_options(fallback))

    def _click_target_changed(self, _event=None):
        self._update_click_point_controls()
        if self.click_target_mode.get() == "自定义坐标":
            self.start_click_point_selection()

    def _update_click_point_controls(self):
        state = "normal" if self.click_target_mode.get() == "自定义坐标" else "disabled"
        self.click_point_entry.configure(state=state)
        self.click_point_button.configure(state=state)

    def _timeout_action_changed(self, _event=None):
        self._update_timeout_jump_control()

    def _found_action_changed(self, _event=None):
        self._update_found_jump_control()

    def _update_found_jump_control(self):
        self.found_jump_entry.configure(
            state="normal" if self.on_found.get() == "jump" else "disabled",
        )

    def _update_timeout_jump_control(self):
        if self.wait_forever.get():
            self.timeout_jump_entry.configure(state="disabled")
            return
        self.timeout_jump_entry.configure(
            state="normal" if image_timeout_option_value(self.on_timeout.get()) == "jump" else "disabled",
        )

    def _update_wait_forever_controls(self, _event=None):
        state = "disabled" if self.wait_forever.get() else "normal"
        self.timeout_entry.configure(state=state)
        self.timeout_delay_entry.configure(state=state)
        self.timeout_combo.configure(state=state)
        fallback_state = "normal" if self.wait_forever.get() else "disabled"
        self.fallback_combo.configure(state=fallback_state)
        self.fallback_switch_entry.configure(state=fallback_state)
        self.fallback_click_button.configure(state=fallback_state)
        self.fallback_action_combo.configure(state=fallback_state)
        self._update_timeout_jump_control()

    def start_click_point_selection(self):
        if self.point_overlay is not None:
            return
        main = self.master
        try:
            self.main_previous_state = str(main.state())
            self.grab_release()
            self.withdraw()
            main.withdraw()
            main.update_idletasks()
            main.after(100, self._show_click_point_curtain)
        except Exception as exc:
            self._close_click_point_selection()
            show_floating_notice(self, "无法选取点击位置", str(exc))

    def _show_click_point_curtain(self):
        try:
            screen, origin = capture_bgr()
            image = Image.fromarray(screen[:, :, ::-1])
            image = ImageEnhance.Brightness(image).enhance(0.62)
            overlay = tk.Toplevel(self.master)
            self.point_overlay = overlay
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.configure(background="#000000", cursor="crosshair")
            width, height = image.size
            left, top = int(origin[0]), int(origin[1])
            overlay.geometry(f"{width}x{height}{left:+d}{top:+d}")
            self.point_screenshot = ImageTk.PhotoImage(image, master=overlay)
            canvas = tk.Canvas(overlay, width=width, height=height, highlightthickness=0, cursor="crosshair")
            canvas.pack(fill="both", expand=True)
            canvas.create_image(0, 0, image=self.point_screenshot, anchor="nw")
            canvas.create_text(
                width // 2, 34,
                text="点击要执行操作的位置；只记录坐标，不会点击下方窗口；Esc 取消",
                fill="#FFFFFF", font=("Microsoft YaHei UI", 13, "bold"),
            )
            canvas.bind("<Button-1>", self._select_click_point)
            overlay.bind("<Escape>", lambda _event: self._close_click_point_selection())
            overlay.update_idletasks()
            overlay.lift()
            overlay.focus_force()
            overlay.grab_set()
        except Exception as exc:
            self._close_click_point_selection()
            show_floating_notice(self, "无法截取幕布", str(exc))

    def _select_click_point(self, event):
        self.click_point.set(f"{int(event.x_root)},{int(event.y_root)}")
        self.click_target_mode.set("自定义坐标")
        self._update_click_point_controls()
        self._close_click_point_selection()

    def _close_click_point_selection(self):
        overlay = self.point_overlay
        self.point_overlay = None
        self.point_screenshot = None
        if overlay is not None:
            try:
                overlay.grab_release()
                overlay.destroy()
            except tk.TclError:
                pass
        self._restore_after_overlay()
        try:
            self.after(30, self._restore_after_overlay)
        except tk.TclError:
            pass

    def _restore_after_overlay(self):
        restore_modal_after_overlay(self, self.master, self.main_previous_state)

    def save(self):
        try:
            module_key_var = getattr(self, "module_key", None)
            module_key = module_key_var.get().strip() if module_key_var is not None else ""
            module_binding = None
            if module_key:
                module_obj = registered_module_object(module_key)
                if module_obj is None:
                    raise ValueError("所选图片模块已不存在，请重新选择")
                module_binding = module_reference_binding(module_key, module_obj)
            template = (
                str(module_binding["template"])
                if module_binding is not None else self.template.get().strip()
            )
            if not template:
                raise ValueError("请从列表中选择模板，或使用“框选新建…”")
            threshold = float(self.threshold.get())
            if not 0.1 <= threshold <= 1:
                raise ValueError("相似度必须在 0.1 到 1.0 之间")
            timeout = max(0, int(self.timeout.get()))
            interval = max(50, int(self.interval.get()))
            found_delay = max(0, int(self.found_delay.get()))
            timeout_delay = max(0, int(self.timeout_delay.get()))
            registry = load_template_regions()
            if module_binding is not None:
                region_mode = "template"
                region = list(module_binding["region"])
            elif template in registry:
                # 引用已登记模板：区域运行时从模板登记表实时读取。
                region_mode, region = "template", []
            else:
                # 编辑旧动作且未改动模板：保留原有区域配置。
                region_mode = self.region_mode.get()
                region = [int(part.strip()) for part in self.region.get().split(",")]
                if len(region) != 4:
                    raise ValueError("自定义区域需要四个整数：x,y,w,h")
            click_target = "custom" if self.click_target_mode.get() == "自定义坐标" else "match"
            if click_target == "custom":
                click_point = [int(part.strip()) for part in self.click_point.get().split(",")]
                if len(click_point) != 2:
                    raise ValueError("自定义点击坐标需要两个整数：x,y")
            else:
                click_point = [0, 0]
            delay = max(0, int(self.delay.get()))
            after_delay = max(0, int(self.after_delay.get()))
            fallback_template = self.fallback_template.get().strip()
            if fallback_template == "（不启用）":
                fallback_template = ""
            fallback_switch_ms = max(0, int(self.fallback_switch_ms.get()))
            if fallback_template and fallback_template in registry:
                # 备用模板引用已登记模板：区域运行时读取。
                fallback_region_mode, fallback_region = "template", []
            else:
                # 未启用备用，或编辑旧动作未改动备用模板：保留原有区域配置。
                fallback_region_mode = self.fallback_region_mode.get()
                fallback_region = [int(part.strip()) for part in self.fallback_region.get().split(",")]
                if len(fallback_region) != 4:
                    raise ValueError("备用自定义区域需要四个整数：x,y,w,h")
            timeout_action = image_timeout_option_value(self.on_timeout.get())
            timeout_jump_action_id = self.jump_target_ids.get(self.timeout_jump_target.get(), "")
            if timeout_action == "jump" and not timeout_jump_action_id:
                raise ValueError("请选择超时后要跳转的目标动作")
            found_jump_action_id = self.jump_target_ids.get(self.found_jump_target.get(), "")
            if self.on_found.get() == "jump" and not found_jump_action_id:
                raise ValueError("请选择找到后要跳转的目标动作")
        except ValueError as exc:
            show_floating_notice(self, "参数错误", str(exc))
            return
        self.result = {
            "type": "image_match", "template": template, "threshold": threshold,
            "timeout_ms": timeout, "interval_ms": interval,
            "region_mode": region_mode, "region": region,
            "on_found": self.on_found.get(), "on_timeout": timeout_action,
            "found_jump_action_id": found_jump_action_id,
            "timeout_jump_action_id": timeout_jump_action_id,
            "found_delay_ms": found_delay,
            "timeout_delay_ms": timeout_delay,
            "wait_forever": bool(self.wait_forever.get()),
            "fallback_template": fallback_template,
            "fallback_switch_ms": fallback_switch_ms,
            "fallback_region_mode": fallback_region_mode,
            "fallback_region": fallback_region,
            "fallback_click": bool(self.fallback_click.get()),
            "fallback_on_match": self.fallback_on_match.get(),
            "click_target": click_target, "click_point": click_point,
            "button": self.button.get(), "delay_ms": delay,
            "after_delay_ms": after_delay,
            "show_result_notice": bool(self.show_result_notice.get()),
        }
        if module_binding is not None:
            self.result.update({
                "module_ref": True,
                "module_key": module_key,
                "module_category": str(module_binding.get("module_category") or "switch"),
            })
        main = self.master
        try:
            main.after_idle(lambda root=main: activate_main_after_modal(root))
        except tk.TclError:
            pass
        self.destroy()


class OcrActionDialog(ModalDialog):
    """识别文字动作表单：识别区域 + 期望文字 + 找到/超时行为。

    OCR 每次识别约几百毫秒，适合一次性判断或慢速轮询；期望文字留空时
    只要识别到任意文字就算命中。跳转目标复用识图动作的同一套机制。
    """

    REGION_MODE_OPTIONS = (("全屏", "screen"), ("自定义区域", "custom"), ("绑定窗口", "window"))
    MATCH_MODE_OPTIONS = (("包含", "contains"), ("等于", "equals"))
    ON_FOUND_OPTIONS = (("继续执行", "continue"), ("跳转到目标动作", "jump"))
    ON_TIMEOUT_OPTIONS = (("继续执行", "continue"), ("跳转到目标动作", "jump"), ("停止脚本", "stop"))

    def __init__(self, parent, action: dict | None = None, actions: list[dict] | None = None):
        super().__init__(parent, "识别文字动作", 650, 950)
        action = action or {}
        jump_options = image_jump_target_options(actions or [])
        found_jump_options = image_found_jump_target_options(actions or [])
        self.jump_target_ids = {
            label: action_id for label, action_id in found_jump_options
        }
        saved_target_id = str(action.get("timeout_jump_action_id", "")).strip()
        selected_target = next(
            (label for label, action_id in jump_options if action_id == saved_target_id), "",
        )
        if not selected_target and not saved_target_id and jump_options:
            selected_target = jump_options[0][0]
        saved_found_target_id = str(action.get("found_jump_action_id", "")).strip()
        selected_found_target = next(
            (label for label, action_id in found_jump_options
             if action_id == saved_found_target_id), "",
        )
        if not selected_found_target and not saved_found_target_id:
            selected_found_target = (
                found_jump_options[0][0] if found_jump_options else "继续执行"
            )
        region = action.get("region", [0, 0, 0, 0])
        saved_mode = str(action.get("region_mode", "screen"))
        saved_mode_label = next(
            (label for label, value in self.REGION_MODE_OPTIONS if value == saved_mode),
            "全屏",
        )
        saved_match = str(action.get("match_mode", "contains"))
        saved_match_label = next(
            (label for label, value in self.MATCH_MODE_OPTIONS if value == saved_match),
            "包含",
        )
        saved_on_found = str(action.get("on_found", "continue"))
        saved_on_found_label = next(
            (label for label, value in self.ON_FOUND_OPTIONS if value == saved_on_found),
            "继续执行",
        )
        saved_on_timeout = str(action.get("on_timeout", "continue"))
        saved_on_timeout_label = next(
            (label for label, value in self.ON_TIMEOUT_OPTIONS if value == saved_on_timeout),
            "继续执行",
        )
        self.region_mode = tk.StringVar(value=saved_mode_label)
        self.region = tk.StringVar(value=",".join(map(str, region)))
        self.expected_text = tk.StringVar(value=str(action.get("expected_text", "")))
        self.match_mode = tk.StringVar(value=saved_match_label)
        self.timeout = duration_var(action.get("timeout_ms", 3000))
        self.interval = duration_var(action.get("interval_ms", 500))
        self.on_found = tk.StringVar(value=saved_on_found_label)
        self.found_jump_target = tk.StringVar(value=selected_found_target)
        self.found_delay = duration_var(action.get("found_delay_ms", 0))
        self.on_timeout = tk.StringVar(value=saved_on_timeout_label)
        self.timeout_delay = duration_var(action.get("timeout_delay_ms", 0))
        self.timeout_jump_target = tk.StringVar(value=selected_target)
        self.show_result_notice = tk.BooleanVar(
            value=bool(action.get("show_result_notice", True))
        )
        self.picker = None
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        def combo_row(row, label, variable, options, bind=None):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            combo = ttk.Combobox(body, textvariable=variable, values=options,
                                 state="readonly")
            combo.grid(row=row, column=1, sticky="ew")
            if bind:
                combo.bind("<<ComboboxSelected>>", bind)
            return combo

        def entry_row(row, label, variable):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            entry = ttk.Entry(body, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew")
            return entry

        combo_row(0, "识别区域模式", self.region_mode,
                  tuple(label for label, _ in self.REGION_MODE_OPTIONS))
        ttk.Label(body, text="识别区域 x,y,w,h").grid(row=1, column=0, sticky="w", pady=8)
        region_row = ttk.Frame(body)
        region_row.grid(row=1, column=1, sticky="ew")
        self.region_entry = ttk.Entry(region_row, textvariable=self.region)
        self.region_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(
            region_row, text="框选区域…", command=self.start_region_selection,
        ).pack(side="left", padx=(8, 0))
        entry_row(2, "期望文字（留空 = 识别到任意文字）", self.expected_text)
        combo_row(3, "匹配方式", self.match_mode,
                  tuple(label for label, _ in self.MATCH_MODE_OPTIONS))
        self.timeout_entry = entry_row(4, "等待超时（0 = 只识别一次）", self.timeout)
        entry_row(5, "检测间隔（未命中时多久再试一次）", self.interval)
        combo_row(6, "找到后", self.on_found,
                  tuple(label for label, _ in self.ON_FOUND_OPTIONS),
                  bind=lambda _event: self._update_jump_controls())
        self.found_jump_combo = combo_row(
            7, "找到后跳转目标动作", self.found_jump_target,
            tuple(label for label, _ in found_jump_options),
        )
        entry_row(8, "找到后等待", self.found_delay)
        combo_row(9, "超时后", self.on_timeout,
                  tuple(label for label, _ in self.ON_TIMEOUT_OPTIONS),
                  bind=lambda _event: self._update_jump_controls())
        self.timeout_delay_entry = entry_row(10, "超时后等待", self.timeout_delay)
        self.timeout_jump_combo = combo_row(
            11, "超时跳转目标动作", self.timeout_jump_target,
            tuple(label for label, _ in jump_options),
        )
        dark_checkbutton(
            body, text="显示识别结果浮动提醒", variable=self.show_result_notice,
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(
            body,
            text="识别区域留空表示全屏；“绑定窗口”在播放时对绑定目标窗口的区域做识别，"
            "没有绑定窗口时回退全屏。每次识别约需几百毫秒，检测间隔不建议小于 200 ms。"
            "期望文字支持“包含 / 等于”，等于时忽略大小写与首尾空白。",
            foreground=COLOR_MUTED, wraplength=560,
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._update_jump_controls()
        buttons = ttk.Frame(body)
        buttons.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def _ancestors_to_hide(self):
        windows = []
        seen = set()
        window = self.master
        while window is not None and len(windows) < 20:
            identity = id(window)
            if identity in seen:
                break
            seen.add(identity)
            try:
                parent = window.master
            except (AttributeError, tk.TclError):
                break
            if parent is None:
                break
            window = parent
            windows.append(window)
            try:
                if window.winfo_class() == "Tk":
                    break
            except tk.TclError:
                break
        return windows

    def start_region_selection(self):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: (
                self.region.set(",".join(map(str, region))),
                self.region_mode.set("自定义区域"),
            ),
            hidden_windows=self._ancestors_to_hide(),
            tip_text="按住鼠标左键，从左上角向右下角拖动框选要识别文字的区域；"
            "松开完成，Esc 取消（留空表示全屏）",
        )
        self.picker.start()

    def _update_jump_controls(self, _event=None):
        self.found_jump_combo.configure(
            state="normal" if self.on_found.get() == "跳转到目标动作" else "disabled",
        )
        self.timeout_jump_combo.configure(
            state="normal" if self.on_timeout.get() == "跳转到目标动作" else "disabled",
        )

    def save(self):
        def value_of(label, options, fallback):
            return next((v for l, v in options if l == label), fallback)

        try:
            region = [int(part.strip()) for part in self.region.get().split(",")
                      if part.strip()]
            if len(region) != 4 or any(part < 0 for part in region):
                raise ValueError("识别区域需要四个非负整数：x,y,w,h（留空表示全屏）")
            expected_text = self.expected_text.get().strip()
            timeout = max(0, int(self.timeout.get()))
            interval = max(200, int(self.interval.get()))
            found_delay = max(0, int(self.found_delay.get()))
            timeout_delay = max(0, int(self.timeout_delay.get()))
            found_jump_action_id = self.jump_target_ids.get(
                self.found_jump_target.get(), ""
            )
            if self.on_found.get() == "跳转到目标动作" and not found_jump_action_id:
                raise ValueError("请选择找到后要跳转的目标动作")
            timeout_jump_action_id = self.jump_target_ids.get(
                self.timeout_jump_target.get(), ""
            )
            if self.on_timeout.get() == "跳转到目标动作" and not timeout_jump_action_id:
                raise ValueError("请选择超时后要跳转的目标动作")
        except ValueError as exc:
            show_floating_notice(self, "参数错误", str(exc))
            return
        self.result = {
            "type": "text_ocr",
            "region_mode": value_of(self.region_mode.get(), self.REGION_MODE_OPTIONS, "screen"),
            "region": region,
            "expected_text": expected_text,
            "match_mode": value_of(self.match_mode.get(), self.MATCH_MODE_OPTIONS, "contains"),
            "timeout_ms": timeout,
            "interval_ms": interval,
            "on_found": value_of(self.on_found.get(), self.ON_FOUND_OPTIONS, "continue"),
            "found_jump_action_id": found_jump_action_id,
            "found_delay_ms": found_delay,
            "on_timeout": value_of(self.on_timeout.get(), self.ON_TIMEOUT_OPTIONS, "continue"),
            "timeout_jump_action_id": timeout_jump_action_id,
            "timeout_delay_ms": timeout_delay,
            "show_result_notice": bool(self.show_result_notice.get()),
        }
        main = self.master
        try:
            main.after_idle(lambda root=main: activate_main_after_modal(root))
        except tk.TclError:
            pass
        self.destroy()


class OcrCompareActionDialog(ModalDialog):
    """Compare two OCR integers around a configurable separator."""

    BRANCH_OPTIONS = (("继续执行", "continue"), ("连续点击", "click"), ("跳转到目标动作", "jump"))
    TIMEOUT_OPTIONS = (("继续执行", "continue"), ("跳转到目标动作", "jump"), ("停止脚本", "stop"))

    def __init__(self, parent, action: dict | None = None, actions: list[dict] | None = None):
        super().__init__(parent, "识别数字比较动作", 700, 820)
        action = action or {}
        jump_options = image_jump_target_options(actions or [])
        self.jump_target_ids = dict(jump_options)

        def target_label(target_id: str) -> str:
            return next((label for label, value in jump_options if value == target_id), "")

        self.region = tk.StringVar(
            value=",".join(map(str, action.get("region", [])))
            if len(action.get("region", [])) == 4 else "",
        )
        self.separator = tk.StringVar(value=str(action.get("separator", "/")))
        self.click_region = tk.StringVar(
            value=",".join(map(str, action.get("click_region", [])))
            if len(action.get("click_region", [])) == 4 else "",
        )
        self.button = tk.StringVar(value=str(action.get("button", "left")))
        self.equal_action = tk.StringVar(
            value=_option_label(str(action.get("equal_action", "continue")), self.BRANCH_OPTIONS, "继续执行"),
        )
        self.equal_click_count = tk.StringVar(value=str(action.get("equal_click_count", 1)))
        self.equal_jump_target = tk.StringVar(
            value=target_label(str(action.get("equal_jump_action_id", "")).strip()),
        )
        self.not_equal_action = tk.StringVar(
            value=_option_label(str(action.get("not_equal_action", "continue")), self.BRANCH_OPTIONS, "继续执行"),
        )
        self.not_equal_click_count = tk.StringVar(value=str(action.get("not_equal_click_count", 1)))
        self.not_equal_jump_target = tk.StringVar(
            value=target_label(str(action.get("not_equal_jump_action_id", "")).strip()),
        )
        self.timeout = duration_var(action.get("timeout_ms", 3000))
        self.interval = duration_var(action.get("interval_ms", 500))
        self.on_timeout = tk.StringVar(
            value=_option_label(str(action.get("on_timeout", "continue")), self.TIMEOUT_OPTIONS, "继续执行"),
        )
        self.timeout_jump_target = tk.StringVar(
            value=target_label(str(action.get("timeout_jump_action_id", "")).strip()),
        )
        self.show_result_notice = tk.BooleanVar(value=bool(action.get("show_result_notice", True)))
        self.picker = None

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        def entry_row(row, label, variable, button_text=None, command=None):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            holder = ttk.Frame(body)
            holder.grid(row=row, column=1, sticky="ew", pady=8)
            holder.columnconfigure(0, weight=1)
            ttk.Entry(holder, textvariable=variable).grid(row=0, column=0, sticky="ew")
            if button_text:
                ttk.Button(holder, text=button_text, command=command).grid(row=0, column=1, padx=(8, 0))

        def combo_row(row, label, variable, options, bind=None):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=8)
            combo = ttk.Combobox(body, textvariable=variable, values=options, state="readonly")
            combo.grid(row=row, column=1, sticky="ew", pady=8)
            if bind:
                combo.bind("<<ComboboxSelected>>", bind)
            return combo

        entry_row(0, "识别区域 (x,y,w,h)", self.region, "框选区域…", self.start_region_selection)
        entry_row(1, "分隔符", self.separator)
        entry_row(2, "点击区域 (x,y,w,h)", self.click_region, "框选区域…", self.start_click_region_selection)
        combo_row(3, "点击按钮", self.button, ("left", "right", "middle"))

        self.equal_action_combo = combo_row(
            4, "相等时", self.equal_action,
            tuple(label for label, _value in self.BRANCH_OPTIONS),
            lambda _event: self._update_branch_controls(),
        )
        entry_row(5, "相等点击次数", self.equal_click_count)
        self.equal_jump_combo = combo_row(
            6, "相等跳转目标", self.equal_jump_target,
            tuple(label for label, _value in jump_options),
        )

        self.not_equal_action_combo = combo_row(
            7, "不相等时", self.not_equal_action,
            tuple(label for label, _value in self.BRANCH_OPTIONS),
            lambda _event: self._update_branch_controls(),
        )
        entry_row(8, "不相等点击次数", self.not_equal_click_count)
        self.not_equal_jump_combo = combo_row(
            9, "不相等跳转目标", self.not_equal_jump_target,
            tuple(label for label, _value in jump_options),
        )
        combo_row(
            10, "识别超时后", self.on_timeout,
            tuple(label for label, _value in self.TIMEOUT_OPTIONS),
            lambda _event: self._update_timeout_controls(),
        )
        entry_row(11, "等待超时", self.timeout)
        entry_row(12, "检测间隔", self.interval)
        self.timeout_jump_combo = combo_row(
            13, "超时跳转目标", self.timeout_jump_target,
            tuple(label for label, _value in jump_options),
        )
        dark_checkbutton(
            body, text="显示识别结果浮动提醒", variable=self.show_result_notice,
        ).grid(row=14, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(
            body,
            text="识别区域和点击区域分别框选；例如识别到 12/34 时比较两侧数字。"
            "相等与不相等分支可分别连续点击或跳转到行对象。",
            foreground=COLOR_MUTED, wraplength=620,
        ).grid(row=15, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self._update_branch_controls()
        self._update_timeout_controls()
        buttons = ttk.Frame(body)
        buttons.grid(row=16, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    def _ancestors_to_hide(self):
        windows = []
        seen = set()
        window = self.master
        while window is not None and len(windows) < 20:
            identity = id(window)
            if identity in seen:
                break
            seen.add(identity)
            try:
                window = window.master
            except (AttributeError, tk.TclError):
                break
            if window is None:
                break
            windows.append(window)
            try:
                if window.winfo_class() == "Tk":
                    break
            except tk.TclError:
                break
        return windows

    def start_region_selection(self):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: (
                self.region.set(",".join(map(str, region))),
            ),
            hidden_windows=self._ancestors_to_hide(),
            tip_text="框选要识别数字比较的区域，松开完成，Esc 取消",
        )
        self.picker.start()

    def start_click_region_selection(self):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: (
                self.click_region.set(",".join(map(str, region))),
            ),
            hidden_windows=self._ancestors_to_hide(),
            tip_text="框选相等/不相等分支要点击的区域，松开完成，Esc 取消",
        )
        self.picker.start()

    def _update_branch_controls(self, _event=None):
        self.equal_jump_combo.configure(
            state="readonly" if self.equal_action.get() == "跳转到目标动作" else "disabled",
        )
        self.not_equal_jump_combo.configure(
            state="readonly" if self.not_equal_action.get() == "跳转到目标动作" else "disabled",
        )

    def _update_timeout_controls(self, _event=None):
        self.timeout_jump_combo.configure(
            state="readonly" if self.on_timeout.get() == "跳转到目标动作" else "disabled",
        )

    @staticmethod
    def _parse_region(value: str, label: str) -> list[int]:
        try:
            region = [int(part.strip()) for part in value.split(",") if part.strip()]
        except ValueError as exc:
            raise ValueError(f"{label}需要四个整数：x,y,w,h") from exc
        if len(region) != 4 or any(part < 0 for part in region) or region[2] <= 0 or region[3] <= 0:
            raise ValueError(f"{label}需要有效的 x,y,w,h 框选区域")
        return region

    def save(self):
        def branch(prefix, behavior_var, count_var, target_var):
            behavior = _option_value(behavior_var.get(), self.BRANCH_OPTIONS, "continue")
            count = max(1, min(9999, int(count_var.get())))
            target_id = self.jump_target_ids.get(target_var.get(), "")
            if behavior == "jump" and not target_id:
                raise ValueError(f"请选择{prefix}时要跳转的行对象")
            return behavior, count, target_id

        try:
            region = self._parse_region(self.region.get(), "识别区域")
            click_region = self._parse_region(self.click_region.get(), "点击区域")
            separator = self.separator.get().strip()
            if not separator:
                raise ValueError("分隔符不能为空")
            timeout = max(0, int(self.timeout.get()))
            interval = max(200, int(self.interval.get()))
            equal_behavior, equal_count, equal_target = branch(
                "相等", self.equal_action, self.equal_click_count, self.equal_jump_target,
            )
            not_equal_behavior, not_equal_count, not_equal_target = branch(
                "不相等", self.not_equal_action, self.not_equal_click_count, self.not_equal_jump_target,
            )
            timeout_behavior = _option_value(self.on_timeout.get(), self.TIMEOUT_OPTIONS, "continue")
            timeout_target = self.jump_target_ids.get(self.timeout_jump_target.get(), "")
            if timeout_behavior == "jump" and not timeout_target:
                raise ValueError("请选择超时后要跳转的行对象")
        except (TypeError, ValueError) as exc:
            show_floating_notice(self, "参数错误", str(exc))
            return
        self.result = {
            "type": "ocr_compare",
            "region_mode": "custom",
            "region": region,
            "separator": separator,
            "click_region": click_region,
            "button": self.button.get(),
            "equal_action": equal_behavior,
            "equal_click_count": equal_count,
            "equal_jump_action_id": equal_target,
            "not_equal_action": not_equal_behavior,
            "not_equal_click_count": not_equal_count,
            "not_equal_jump_action_id": not_equal_target,
            "timeout_ms": timeout,
            "interval_ms": interval,
            "on_timeout": timeout_behavior,
            "timeout_jump_action_id": timeout_target,
            "show_result_notice": bool(self.show_result_notice.get()),
        }
        main = self.master
        try:
            main.after_idle(lambda root=main: activate_main_after_modal(root))
        except tk.TclError:
            pass
        self.destroy()


class MultiConditionClickDialog(ModalDialog):
    """Fixed three-slot image/OCR/number condition click action."""

    CONDITION_TYPES = (("图片识别", "image"), ("OCR识别", "ocr"), ("数字比较", "number_compare"))
    MATCH_MODES = (("包含", "contains"), ("完全相等", "equals"))
    RELATIONS = (("相等", "equal"), ("不相等", "not_equal"))
    TIMEOUT_OPTIONS = (("继续执行", "continue"), ("停止脚本", "stop"))

    def __init__(self, parent, action: dict | None = None):
        super().__init__(parent, "多条件识图点击", 820, 760)
        action = action or {}
        saved = action.get("conditions", [])
        saved = saved if isinstance(saved, list) else []
        self.picker = None
        self.condition_enabled = []
        self.condition_type = []
        self.condition_region = []
        self.condition_module_key = []
        self.condition_template = []
        self.condition_threshold = []
        self.condition_expected = []
        self.condition_match_mode = []
        self.condition_separator = []
        self.condition_relation = []

        canvas = tk.Canvas(self, background=COLOR_BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = ttk.Frame(canvas, padding=18)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        self._form_canvas = canvas

        def update_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def stretch_body(event):
            canvas.itemconfigure(body_window, width=event.width)

        body.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", stretch_body)
        canvas.bind("<Map>", update_scrollregion)
        self.bind("<MouseWheel>", self._scroll_form)
        canvas.bind("<MouseWheel>", self._scroll_form, add="+")
        canvas.after_idle(update_scrollregion)
        body.columnconfigure(0, weight=1)
        type_labels = tuple(label for label, _value in self.CONDITION_TYPES)
        match_labels = tuple(label for label, _value in self.MATCH_MODES)
        relation_labels = tuple(label for label, _value in self.RELATIONS)

        ttk.Label(
            body,
            text="固定三个条件槽位：勾选后才参与判断；启用的条件必须全部满足，才会执行下方连续点击。",
            foreground=COLOR_MUTED, wraplength=760,
        ).pack(anchor="w", pady=(0, 10))
        for index in range(3):
            condition = saved[index] if index < len(saved) and isinstance(saved[index], dict) else {}
            frame = ttk.LabelFrame(body, text=f"条件 {index + 1}", padding=10)
            frame.pack(fill="x", pady=5)
            frame.columnconfigure(1, weight=1)
            enabled = tk.BooleanVar(value=bool(condition.get("enabled", index == 0)))
            kind = str(condition.get("type", "image"))
            module_key = str(condition.get("module_key", "")).strip()
            module_obj = registered_module_object(module_key) if module_key else None
            if module_obj is not None:
                condition = dict(condition)
                condition["template"] = str(module_obj.get("template", ""))
                condition["region"] = list(module_obj.get("region") or [])
            self.condition_enabled.append(enabled)
            self.condition_type.append(tk.StringVar(value=_option_label(kind, self.CONDITION_TYPES, "图片识别")))
            region = condition.get("region", [])
            self.condition_region.append(tk.StringVar(
                value=",".join(map(str, region)) if len(region) == 4 else "",
            ))
            self.condition_module_key.append(tk.StringVar(value=module_key))
            self.condition_template.append(tk.StringVar(value=str(condition.get("template", ""))))
            self.condition_threshold.append(tk.StringVar(value=str(condition.get("threshold", 0.85))))
            self.condition_expected.append(tk.StringVar(value=str(condition.get("expected_text", ""))))
            self.condition_match_mode.append(tk.StringVar(
                value=_option_label(str(condition.get("match_mode", "contains")), self.MATCH_MODES, "包含"),
            ))
            self.condition_separator.append(tk.StringVar(value=str(condition.get("separator", "/"))))
            self.condition_relation.append(tk.StringVar(
                value=_option_label(str(condition.get("relation", "equal")), self.RELATIONS, "相等"),
            ))
            dark_checkbutton(frame, text="启用", variable=enabled).grid(row=0, column=0, sticky="w", padx=(0, 10))
            ttk.Label(frame, text="类型").grid(row=0, column=1, sticky="w")
            ttk.Combobox(
                frame, textvariable=self.condition_type[-1], values=type_labels,
                state="readonly", width=12,
            ).grid(row=0, column=2, sticky="w", padx=(8, 0))
            ttk.Label(frame, text="识别区域 (x,y,w,h)").grid(row=1, column=0, sticky="w", pady=(8, 0))
            region_row = ttk.Frame(frame)
            region_row.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
            region_row.columnconfigure(0, weight=1)
            ttk.Entry(region_row, textvariable=self.condition_region[-1]).grid(row=0, column=0, sticky="ew")
            ttk.Button(
                region_row, text="框选区域…",
                command=lambda slot=index: self.start_condition_region_selection(slot),
            ).grid(row=0, column=1, padx=(8, 0))
            ttk.Label(frame, text="图片模板").grid(row=2, column=0, sticky="w", pady=(8, 0))
            template_combo = ttk.Combobox(
                frame, textvariable=self.condition_template[-1],
                values=registered_template_options(str(condition.get("template", ""))),
                state="readonly", width=48,
            )
            template_combo.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
            template_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event, slot=index: self.condition_module_key[slot].set(""),
            )
            ttk.Button(
                frame, text="选择模块…",
                command=lambda slot=index: self.select_condition_module(slot),
            ).grid(row=2, column=3, sticky="e", padx=(8, 0), pady=(8, 0))
            ttk.Label(frame, text="相似度").grid(row=3, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(frame, textvariable=self.condition_threshold[-1], width=12).grid(
                row=3, column=1, sticky="w", pady=(8, 0),
            )
            ttk.Label(frame, text="OCR文字").grid(row=4, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(frame, textvariable=self.condition_expected[-1]).grid(
                row=4, column=1, columnspan=2, sticky="ew", pady=(8, 0),
            )
            ttk.Label(frame, text="OCR匹配").grid(row=5, column=0, sticky="w", pady=(8, 0))
            ttk.Combobox(
                frame, textvariable=self.condition_match_mode[-1], values=match_labels,
                state="readonly", width=12,
            ).grid(row=5, column=1, sticky="w", pady=(8, 0))
            ttk.Label(frame, text="数字分隔符").grid(row=6, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(frame, textvariable=self.condition_separator[-1], width=12).grid(
                row=6, column=1, sticky="w", pady=(8, 0),
            )
            ttk.Label(frame, text="数字关系").grid(row=6, column=2, sticky="w", padx=(20, 0), pady=(8, 0))
            ttk.Combobox(
                frame, textvariable=self.condition_relation[-1], values=relation_labels,
                state="readonly", width=12,
            ).grid(row=6, column=3, sticky="e", pady=(8, 0))

        click_frame = ttk.LabelFrame(body, text="满足条件后的操作", padding=10)
        click_frame.pack(fill="x", pady=(10, 5))
        click_frame.columnconfigure(1, weight=1)
        self.click_region = tk.StringVar(
            value=",".join(map(str, action.get("click_region", [])))
            if len(action.get("click_region", [])) == 4 else "",
        )
        self.button = tk.StringVar(value=str(action.get("button", "left")))
        self.click_count = tk.StringVar(value=str(action.get("click_count", 1)))
        self.timeout = duration_var(action.get("timeout_ms", 3000))
        self.interval = duration_var(action.get("interval_ms", 500))
        self.on_timeout = tk.StringVar(
            value=_option_label(str(action.get("on_timeout", "continue")), self.TIMEOUT_OPTIONS, "继续执行"),
        )
        self.show_result_notice = tk.BooleanVar(value=bool(action.get("show_result_notice", True)))
        self._entry_row(click_frame, 0, "点击区域 (x,y,w,h)", self.click_region, self.start_click_region_selection)
        self._entry_row(click_frame, 1, "连续点击次数", self.click_count)
        self._combo_row(click_frame, 2, "点击按钮", self.button, ("left", "right", "middle"))
        self._entry_row(click_frame, 3, "等待超时", self.timeout)
        self._entry_row(click_frame, 4, "检测间隔", self.interval)
        self._combo_row(
            click_frame, 5, "超时后", self.on_timeout,
            tuple(label for label, _value in self.TIMEOUT_OPTIONS),
        )
        dark_checkbutton(
            click_frame, text="显示识别结果浮动提醒", variable=self.show_result_notice,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=self.save).pack(side="right", padx=8)

    @staticmethod
    def _parse_region(value: str, label: str) -> list[int]:
        try:
            region = [int(part.strip()) for part in value.split(",") if part.strip()]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}需要四个整数：x,y,w,h") from exc
        if len(region) != 4 or any(part < 0 for part in region) or region[2] <= 0 or region[3] <= 0:
            raise ValueError(f"{label}需要有效的 x,y,w,h 框选区域")
        return region

    def _entry_row(self, parent, row, label, variable, picker=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=6)
        holder.columnconfigure(0, weight=1)
        ttk.Entry(holder, textvariable=variable).grid(row=0, column=0, sticky="ew")
        if picker:
            ttk.Button(holder, text="框选区域…", command=picker).grid(row=0, column=1, padx=(8, 0))

    @staticmethod
    def _combo_row(parent, row, label, variable, values):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=6)
        ttk.Combobox(parent, textvariable=variable, values=values, state="readonly").grid(
            row=row, column=1, sticky="w", pady=6,
        )

    def _scroll_form(self, event):
        if not event.delta:
            return "break"
        self._form_canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _ancestors_to_hide(self):
        windows = []
        seen = set()
        window = self.master
        while window is not None and len(windows) < 20:
            identity = id(window)
            if identity in seen:
                break
            seen.add(identity)
            try:
                window = window.master
            except (AttributeError, tk.TclError):
                break
            if window is None:
                break
            windows.append(window)
            try:
                if window.winfo_class() == "Tk":
                    break
            except tk.TclError:
                break
        return windows

    def start_condition_region_selection(self, slot):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: self.condition_region[slot].set(",".join(map(str, region))),
            hidden_windows=self._ancestors_to_hide(),
            tip_text=f"框选条件 {slot + 1} 的识别区域，松开完成，Esc 取消",
        )
        self.picker.start()

    def select_condition_module(self, slot: int):
        binding = choose_module_binding(self, categories=("switch",))
        if not binding:
            return
        self.condition_module_key[slot].set(str(binding["module_key"]))
        self.condition_template[slot].set(str(binding["template"]))
        self.condition_region[slot].set(",".join(map(str, binding.get("region") or [])))
        self.condition_type[slot].set("图片识别")

    def start_click_region_selection(self):
        self.picker = ScreenRegionPicker(
            self, self.master,
            lambda region: self.click_region.set(",".join(map(str, region))),
            hidden_windows=self._ancestors_to_hide(),
            tip_text="框选满足条件后要连续点击的区域，松开完成，Esc 取消",
        )
        self.picker.start()

    def save(self):
        try:
            conditions = []
            for index in range(3):
                enabled = bool(self.condition_enabled[index].get())
                kind = _option_value(self.condition_type[index].get(), self.CONDITION_TYPES, "image")
                module_key = (
                    self.condition_module_key[index].get().strip()
                    if index < len(getattr(self, "condition_module_key", [])) else ""
                )
                module_binding = None
                if kind == "image" and module_key:
                    module_obj = registered_module_object(module_key)
                    if module_obj is None:
                        raise ValueError(f"条件 {index + 1} 所选图片模块已不存在")
                    module_binding = module_reference_binding(module_key, module_obj)
                    region = list(module_binding["region"])
                else:
                    raw_region = self.condition_region[index].get().strip()
                    region = self._parse_region(
                        raw_region, f"条件 {index + 1} 识别区域",
                    ) if raw_region else [0, 0, 0, 0]
                condition = {"enabled": enabled, "type": kind, "region": region}
                if enabled and region[2] <= 0:
                    raise ValueError(f"请设置条件 {index + 1} 的识别区域")
                if kind == "image":
                    template = (
                        str(module_binding["template"])
                        if module_binding is not None
                        else self.condition_template[index].get().strip()
                    )
                    if enabled and not template:
                        raise ValueError(f"请设置条件 {index + 1} 的图片模板")
                    threshold = float(self.condition_threshold[index].get())
                    if not 0.1 <= threshold <= 1:
                        raise ValueError("图片相似度必须在 0.1 到 1.0 之间")
                    condition.update(template=template, threshold=threshold)
                    if module_binding is not None:
                        condition.update({
                            "module_ref": True,
                            "module_key": module_key,
                            "region_mode": "template",
                        })
                elif kind == "ocr":
                    condition.update(
                        expected_text=self.condition_expected[index].get(),
                        match_mode=_option_value(self.condition_match_mode[index].get(), self.MATCH_MODES, "contains"),
                    )
                elif kind == "number_compare":
                    separator = self.condition_separator[index].get().strip()
                    if enabled and not separator:
                        raise ValueError(f"请设置条件 {index + 1} 的数字分隔符")
                    condition.update(
                        separator=separator or "/",
                        relation=_option_value(self.condition_relation[index].get(), self.RELATIONS, "equal"),
                    )
                conditions.append(condition)
            if not any(condition["enabled"] for condition in conditions):
                raise ValueError("至少需要启用一个条件")
            click_region = self._parse_region(self.click_region.get(), "点击区域")
            click_count = max(1, min(9999, int(self.click_count.get())))
            timeout = max(0, int(self.timeout.get()))
            interval = max(200, int(self.interval.get()))
            on_timeout = _option_value(self.on_timeout.get(), self.TIMEOUT_OPTIONS, "continue")
        except (TypeError, ValueError) as exc:
            show_floating_notice(self, "参数错误", str(exc))
            return
        self.result = {
            "type": "multi_condition_click",
            "conditions": conditions,
            "click_region": click_region,
            "button": self.button.get(),
            "click_count": click_count,
            "timeout_ms": timeout,
            "interval_ms": interval,
            "on_timeout": on_timeout,
            "show_result_notice": bool(self.show_result_notice.get()),
        }
        try:
            self.master.after_idle(lambda root=self.master: activate_main_after_modal(root))
        except tk.TclError:
            pass
        self.destroy()


class JsonActionDialog(ModalDialog):
    def __init__(self, parent, action: dict):
        super().__init__(parent, "高级动作编辑", 640, 500)
        ttk.Label(self, text="编辑当前动作参数（JSON）", padding=(18, 14, 18, 6)).pack(anchor="w")
        self.text = tk.Text(
            self, font=("Consolas", 11), wrap="none", undo=True,
            background=COLOR_SURFACE, foreground=COLOR_TEXT,
            insertbackground=COLOR_TEXT, selectbackground=COLOR_BLUE_SELECTION,
            relief="flat", borderwidth=0, padx=12, pady=10,
        )
        self.text.pack(fill="both", expand=True, padx=18)
        self.text.insert("1.0", json.dumps(action, ensure_ascii=False, indent=2))
        buttons = ttk.Frame(self, padding=18)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", command=self.save).pack(side="right", padx=8)

    def save(self):
        try:
            value = json.loads(self.text.get("1.0", "end"))
            if not isinstance(value, dict) or not value.get("type"):
                raise ValueError("动作必须是包含 type 的对象")
        except (json.JSONDecodeError, ValueError) as exc:
            show_floating_notice(self, "格式错误", str(exc))
            return
        self.result = value
        self.destroy()


def edit_action(parent, action: dict, all_actions: list[dict] | None = None,
                segment_depth: int = 0) -> dict | None:
    def preserve_identity(updated: dict | None) -> dict | None:
        if updated is not None and action.get("action_id"):
            updated["action_id"] = action["action_id"]
        return updated

    kind = action.get("type")
    if kind == "restart_workflow":
        return preserve_identity(
            RestartWorkflowTargetDialog(
                parent, action, default_row=_app_workflow_default_row(parent),
            ).show(),
        )
    if kind in ("end_current_script", "jump_current_script_last"):
        message = (
            f"{END_CURRENT_SCRIPT_LABEL}，无需配置。"
            if kind == "end_current_script" else
            "执行到这里时会离开模块代码段，并从当前脚本最后一行继续，无需配置。"
        )
        show_floating_notice(parent, "特殊模块", message)
        return None
    if kind == "jump":
        return preserve_identity(JumpActionDialog(parent, action, actions=all_actions).show())
    if kind == "activate_window":
        selected = WindowPicker(parent).show()
        if not selected:
            return None
        updated = dict(action)
        updated["window"] = {
            "title": selected.title,
            "class_name": selected.class_name,
            "process_path": selected.process_path,
        }
        return preserve_identity(updated)
    if kind in ("image_match", "global_detect") and action.get("module_ref"):
        return preserve_identity(
            ModuleReferenceDelayDialog(parent, action, actions=all_actions).show(),
        )
    if kind in {"key", "key_press"}:
        return preserve_identity(KeyActionDialog(parent, action).show())
    if kind == "mouse_move":
        return preserve_identity(MouseMoveDialog(parent, action).show())
    if kind in {"click", "mouse_button"}:
        return preserve_identity(ClickDialog(parent, action).show())
    if kind == "turn":
        return preserve_identity(TurnActionDialog(parent, action).show())
    if kind == "repeat_click":
        return preserve_identity(RepeatClickDialog(parent, action).show())
    if kind == "image_match":
        return preserve_identity(ImageActionDialog(parent, action, actions=all_actions).show())
    if kind == "text_ocr":
        return preserve_identity(
            OcrActionDialog(parent, action, actions=all_actions).show(),
        )
    if kind == "ocr_compare":
        return preserve_identity(
            OcrCompareActionDialog(parent, action, actions=all_actions).show(),
        )
    if kind == "multi_condition_click":
        return preserve_identity(
            MultiConditionClickDialog(parent, action).show(),
        )
    if kind == "global_detect":
        return preserve_identity(
            GlobalDetectDialog(
                parent, action, jump=bool(action.get("jump_row")),
                actions=all_actions,
            ).show(),
        )
    if kind == "text":
        return preserve_identity(TextActionDialog(parent, action).show())
    if kind == "delay":
        value = DurationDialog(
            parent, "编辑延时", "延时时间：", int(action.get("ms", 100)),
        ).show()
        if value is not None:
            updated = dict(action)
            updated["ms"] = value
            return updated
        return None
    if kind == "notice":
        title = "编辑浮动提醒"
        text = simpledialog.askstring(
            title, "提示文字：", parent=parent,
            initialvalue=str(action.get("text", "")),
        )
        if text is not None and text.strip():
            updated = dict(action)
            updated["text"] = text.strip()
            return updated
        return None
    if kind == "script_ref":
        return preserve_identity(ScriptRefDialog(parent, action).show())
    if kind == "open_app":
        return preserve_identity(OpenAppDialog(parent, action).show())
    if kind == "close_app":
        return preserve_identity(CloseAppDialog(parent, action).show())
    return preserve_identity(JsonActionDialog(parent, action).show())
