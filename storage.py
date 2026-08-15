from __future__ import annotations

import json
import os
import re
import shutil
import sys
from hashlib import sha1
from pathlib import Path
from typing import TypeVar

from models import END_CURRENT_SCRIPT_LABEL, MacroScript, Workflow


T = TypeVar("T", MacroScript, Workflow)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = app_dir()
SCRIPTS_DIR = BASE_DIR / "scripts"
WORKFLOWS_DIR = BASE_DIR / "workflows"
IMAGES_DIR = BASE_DIR / "images"
SETTINGS_PATH = BASE_DIR / "app_settings.json"
SCRIPT_BACKUPS_DIR = BASE_DIR / "backups" / "scripts"
TEMPLATE_REGIONS_PATH = BASE_DIR / "template_regions.json"
MODULE_SETTINGS_PATH = BASE_DIR / "module_settings.json"
MODULE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}


def ensure_dirs() -> None:
    for folder in (SCRIPTS_DIR, WORKFLOWS_DIR, IMAGES_DIR, SCRIPT_BACKUPS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def safe_name(name: str, fallback: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return text or fallback


def save_script(script: MacroScript, path: Path | None = None) -> Path:
    ensure_dirs()
    path = path or SCRIPTS_DIR / f"{safe_name(script.name, 'script')}.json"
    path.write_text(json.dumps(script.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def script_backup_path(source: str | Path) -> Path:
    """Return the single, stable backup destination for one script file."""
    source_path = Path(source).resolve()
    try:
        relative = source_path.relative_to(SCRIPTS_DIR.resolve())
        return SCRIPT_BACKUPS_DIR / relative
    except ValueError:
        digest = sha1(str(source_path).casefold().encode("utf-8")).hexdigest()[:12]
        return SCRIPT_BACKUPS_DIR / "external" / f"{digest}_{source_path.name}"


def backup_script(source: str | Path, snapshot: dict | None = None) -> Path:
    """Overwrite a script's sole backup, optionally using an in-memory snapshot."""
    source_path = Path(source).resolve()
    target = script_backup_path(source_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if snapshot is None:
        shutil.copy2(source_path, target)
    else:
        target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def available_script_path(name: str, folder: Path | None = None) -> Path:
    """Return a new script path without ever replacing an existing file."""
    folder = folder or SCRIPTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stem = safe_name(name, "script")
    candidate = folder / f"{stem}.json"
    number = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({number}).json"
        number += 1
    return candidate


def load_script(path: str | Path) -> MacroScript:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return MacroScript.from_dict(data)


def save_workflow(workflow: Workflow, path: Path | None = None) -> Path:
    ensure_dirs()
    path = path or WORKFLOWS_DIR / f"{safe_name(workflow.name, 'workflow')}.json"
    path.write_text(json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_workflow(path: str | Path) -> Workflow:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Workflow.from_dict(data)


def migrate_workflow_templates() -> int:
    """一次性迁移：把旧的工作流恢复模板（workflow_templates.json）转成普通工作流文件。

    每个模板写成 workflows/<名称>.json（不覆盖已存在的工作流文件）；迁移成功后旧文件
    改名为 workflow_templates.migrated.json 保留备份，避免重复迁移。返回迁移数量
    （0 表示无需迁移或全部跳过）。
    """
    path = BASE_DIR / "workflow_templates.json"
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    templates = data.get("templates", {}) if isinstance(data, dict) else {}
    if not isinstance(templates, dict):
        return 0
    ensure_dirs()
    migrated = 0
    for name, snapshot in templates.items():
        if not str(name).strip() or not isinstance(snapshot, dict):
            continue
        try:
            workflow = Workflow.from_dict(snapshot)
        except Exception:
            continue
        target = WORKFLOWS_DIR / f"{safe_name(str(workflow.name or name), 'workflow')}.json"
        if target.is_file():
            continue
        target.write_text(
            json.dumps(workflow.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        migrated += 1
    if migrated:
        try:
            path.rename(BASE_DIR / "workflow_templates.migrated.json")
        except OSError:
            pass
    return migrated


def display_path(path: str | Path) -> str:
    path = Path(path)
    # 配置中的相对路径始终相对于程序/EXE 目录，而不是进程启动目录。
    # 否则从快捷方式、终端或其他目录启动时，同一个模块会被展开成不同
    # 的绝对路径，导致仓库明明有对象却误报“对象不存在”。
    if not path.is_absolute():
        path = BASE_DIR / path
    path = path.resolve()
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def resolve_path(path: str | Path) -> Path:
    value = Path(path)
    resolved = value if value.is_absolute() else BASE_DIR / value
    if resolved.exists():
        return resolved
    # 1.84.28 将 scripts/全局 更名为 scripts/工作流全局。旧工作流和脚本引用
    # 仍可能保存旧路径，运行时透明转向新目录，避免迁移后出现“脚本不存在”。
    parts = list(resolved.parts)
    for index in range(len(parts) - 1):
        if parts[index].casefold() == "scripts" and parts[index + 1] == "全局":
            parts[index + 1] = "工作流全局"
            migrated = Path(*parts)
            if migrated.exists():
                return migrated
            break
    return resolved


def load_app_settings() -> dict:
    defaults = {
        "sound_enabled": True,
        "mini_window_enabled": True,
        "close_action": "exit",
        "record_mode": "auto",
        "move_interval_ms": 20,
        "floating_notice_position": "顶部居中",
        "repeat": 1,
        "bound_window": None,
        "activation_window_draft_enabled": False,
        "activation_window_draft": None,
        "workflow_draft": None,
        "workflow_path": "",
        "timed_backup_enabled": False,
        "backup_interval": "1h",
        "windows_startup_enabled": False,
        "start_minimized_to_tray": False,
        "startup_run_workflow": False,
        "startup_workflow_path": "",
    }
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            defaults.update(value)
            # activation_window(_enabled) 自 1.48 起改为脚本属性，不再全局保存。
            for obsolete_key in (
                "execution_mode", "dxdy_port", "target_relative_enabled",
                "activation_window", "activation_window_enabled",
                "backup_interval_minutes",
            ):
                defaults.pop(obsolete_key, None)
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def save_app_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_module_images_dir() -> Path:
    """Return the configured recognition-image folder.

    A separate settings file keeps this manager preference independent from
    sidebar setting snapshots. Packaged builds therefore default to
    ``dist/images/部分`` while source runs default to the matching source path.
    """
    default = IMAGES_DIR / "部分"
    if not MODULE_SETTINGS_PATH.exists():
        return default
    try:
        value = json.loads(MODULE_SETTINGS_PATH.read_text(encoding="utf-8"))
        raw = str(value.get("images_dir", "")).strip() if isinstance(value, dict) else ""
    except (OSError, json.JSONDecodeError):
        raw = ""
    return resolve_path(raw) if raw else default


def save_module_images_dir(path: str | Path) -> Path:
    directory = Path(path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    MODULE_SETTINGS_PATH.write_text(
        json.dumps({"images_dir": display_path(directory)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return directory


def module_image_inventory(directory: str | Path, objects: dict[str, dict]) -> list[dict]:
    """List supported image files recursively and mark registered module keys."""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    adopted: dict[str, list[str]] = {}
    for key, obj in objects.items():
        if obj.get("pure_action"):
            continue
        template = str(obj.get("template") or key).strip()
        try:
            normalized = str(resolve_path(template).resolve()).casefold()
        except (OSError, ValueError):
            continue
        adopted.setdefault(normalized, []).append(key)
    result = []
    for path in sorted(
        (item for item in folder.rglob("*") if item.is_file() and item.suffix.lower() in MODULE_IMAGE_EXTENSIONS),
        key=lambda item: str(item).casefold(),
    ):
        resolved = str(path.resolve()).casefold()
        keys = adopted.get(resolved, [])
        key = keys[0] if keys else ""
        result.append({
            "path": display_path(path),
            "module_key": key,
            "module_keys": keys,
            "status": f"已采用（{len(keys)} 个）" if keys else "未采用",
        })
    return result


# 模块对象：模块区域仓库中的结构化识图对象。
# 旧格式（v1.79 及更早）是 {display path: [x, y, w, h]} 列表；
# 新对象以 module:<uuid> 为独立身份，图片路径保存在 template 字段。同一图片可被
# 多个不同名称、类别和行为的模块共用；旧图片路径 key 继续兼容。
# 四类：切换模块 / 工作流全局模块 / 脚本全局模块 / 特殊模块。
MODULE_CATEGORIES = ("switch", "workflow_global", "script_global", "special")
MODULE_AFTER_ACTIONS = (
    "click_match",       # 点击成功识别的区域
    "click_custom",      # 点击自定义框选位置
    "continue",          # 直接继续下一个动作
    "second_match",      # 二次识别后点击：识别另一个模板（可限定区域）后点击其位置
    "run_actions",       # 旧格式：仅执行代码段；加载时迁移为 continue + 附加代码段
)
DEFAULT_MODULE_OBJECT: dict = {
    "category": "switch",
    "enabled": True,                    # 仓库可用性：禁用后不能再插入新引用
    "region": [0, 0, 0, 0],            # [0,0,0,0] = 尚未设置区域 → 全屏识别
    "threshold": 0.85,
    "interval_ms": 250,
    "start_delay_ms": 0,                # 脚本全局模块进入脚本后多久开始识别
    "fallback_module_key": "",         # 主模块等待期间同时识别的备用图片/文字模块
    "fallback_click": False,            # 备用首次出现时点击备用命中位置
    "blocking": False,                 # 阻塞识别：识别不到就一直等
    "wait_text_absent": False,         # OCR：目标文字存在时循环，消失后才完成
    "hold_enabled": True,              # 全局模块是否要求命中状态持续达到 hold_ms
    "hold_ms": 1000,                   # 全局模块（检测型）「持续超过」语义（切换模块忽略）
    "delay_ms": 0,                     # 识别成功后的延时 A
    "after_action": "click_match",
    "run_code_after_action": False,  # 主动作完成后，再执行 on_success_actions
    "click_point": [],                 # click_custom 用
    "ocr_offset_up": 0,               # OCR 命中文字中心的四向点击偏移（像素）
    "ocr_offset_down": 0,
    "ocr_offset_left": 0,
    "ocr_offset_right": 0,
    "button": "left",
    "second_match_template": "",       # second_match 用
    "second_match_region": [],         # 旧字段；二次模板现直接使用自身登记区域
    "second_match_timeout_ms": 3000,   # 非阻塞时二次识别超时
    "second_match_click_target": "second",  # first / second / custom_region
    "second_match_click_region": [],   # custom_region 时点击该框选区域中心
    "on_success_actions": [],          # 可选附加代码段（动作 dict 列表，可含特殊模块）
    "run_code_on_timeout": False,      # 连续未识别达到时限后执行独立代码段
    "not_found_timeout_ms": 3000,
    "on_timeout_actions": [],
}


def _normalize_object(value, key: str = ""):
    """Normalize a template_regions.json entry into a structured module object.

    旧格式 list [x,y,w,h] → 带该区域的默认对象；无效 list（长度/类型/负宽高）
    按旧规则丢弃（返回 None）；dict → 合并默认值并校验/钳制。
    """
    if isinstance(value, (list, tuple)):
        try:
            parts = [int(part) for part in value]
        except (TypeError, ValueError):
            return None
        if len(parts) == 4 and parts[2] >= 0 and parts[3] >= 0:
            return dict(
                DEFAULT_MODULE_OBJECT, region=parts,
                template=key if key and not key.startswith("module:") else "",
            )
        return None
    if not isinstance(value, dict):
        return None
    obj = dict(DEFAULT_MODULE_OBJECT)
    obj.update(value)
    if obj.get("category") == "global":
        # 原全局模块属于工作流的常驻检测，升级为明确的工作流全局类别。
        obj["category"] = "workflow_global"
    if obj.get("category") == "special" and not obj.get("pure_action"):
        # 特殊分类自 1.82 起只放纯动作（无图片）：1.81 曾把旧全局模块并入特殊，
        # 检测型条目（有图或未标纯动作）加载时惰性迁回「全局模块」。
        obj["category"] = "workflow_global"
    if obj.get("category") not in MODULE_CATEGORIES:
        obj["category"] = "switch"
    if not obj.get("pure_action"):
        # 旧仓库用图片路径作为主键；新仓库用独立 module:<id> 主键，图片只是属性。
        # 加载旧条目时补出 template，旧脚本引用仍可按原路径命中。
        obj["template"] = str(
            obj.get("template") or (key if key and not key.startswith("module:") else "")
        ).strip()
    raw_region = obj.get("region", [0, 0, 0, 0])
    if isinstance(raw_region, (list, tuple)) and len(raw_region) == 4:
        try:
            parts = [int(part) for part in raw_region]
            obj["region"] = parts if parts[2] >= 0 and parts[3] >= 0 else [0, 0, 0, 0]
        except (TypeError, ValueError):
            obj["region"] = [0, 0, 0, 0]
    else:
        obj["region"] = [0, 0, 0, 0]
    try:
        obj["threshold"] = min(1.0, max(0.1, float(obj.get("threshold", 0.85))))
    except (TypeError, ValueError):
        obj["threshold"] = 0.85
    try:
        obj["interval_ms"] = max(50, min(10000, int(obj.get("interval_ms", 250))))
    except (TypeError, ValueError):
        obj["interval_ms"] = 250
    try:
        obj["start_delay_ms"] = max(0, min(86400000, int(obj.get("start_delay_ms", 0))))
    except (TypeError, ValueError):
        obj["start_delay_ms"] = 0
    try:
        obj["hold_ms"] = max(0, min(60000, int(obj.get("hold_ms", 1000))))
    except (TypeError, ValueError):
        obj["hold_ms"] = 1000
    obj["blocking"] = bool(obj.get("blocking", False))
    obj["fallback_module_key"] = str(obj.get("fallback_module_key", "")).strip()
    obj["fallback_click"] = bool(obj.get("fallback_click", False))
    obj["wait_text_absent"] = bool(obj.get("wait_text_absent", False))
    obj["enabled"] = bool(obj.get("enabled", True))
    obj["hold_enabled"] = bool(obj.get("hold_enabled", True))
    try:
        obj["delay_ms"] = max(0, min(60000, int(obj.get("delay_ms", 0))))
    except (TypeError, ValueError):
        obj["delay_ms"] = 0
    if obj.get("after_action") not in MODULE_AFTER_ACTIONS:
        obj["after_action"] = "click_match"
    # 兼容旧对象：过去“执行代码段”是成功后动作中的互斥选项。新格式将它
    # 迁移为“成功后继续 + 再执行代码段”，执行效果不变，同时允许点击、
    # 二次识别等主动作完成后继续追加代码段。
    if obj.get("after_action") == "run_actions":
        obj["after_action"] = "continue"
        obj["run_code_after_action"] = True
    else:
        obj["run_code_after_action"] = bool(obj.get("run_code_after_action", False))
    if obj.get("recognize") == "none" and obj.get("after_action") not in (
        "click_custom", "continue",
    ):
        obj["after_action"] = "continue"
    raw_point = obj.get("click_point", [])
    if isinstance(raw_point, (list, tuple)) and len(raw_point) == 2:
        try:
            obj["click_point"] = [int(part) for part in raw_point]
        except (TypeError, ValueError):
            obj["click_point"] = []
    else:
        obj["click_point"] = []
    for field in (
        "ocr_offset_up", "ocr_offset_down", "ocr_offset_left", "ocr_offset_right",
    ):
        try:
            obj[field] = max(0, min(10000, int(obj.get(field, 0))))
        except (TypeError, ValueError):
            obj[field] = 0
    if obj.get("button") not in ("left", "right", "middle"):
        obj["button"] = "left"
    obj["second_match_template"] = str(obj.get("second_match_template", "")).strip()
    # 旧版允许给二次识别再框选一份区域，容易与模板对象自己的区域冲突。
    # 新版统一跟随二次模板登记区域，加载时清空旧字段。
    obj["second_match_region"] = []
    try:
        obj["second_match_timeout_ms"] = max(0, int(obj.get("second_match_timeout_ms", 3000)))
    except (TypeError, ValueError):
        obj["second_match_timeout_ms"] = 3000
    if obj.get("second_match_click_target") not in ("first", "second", "custom_region"):
        obj["second_match_click_target"] = "second"
    raw_click_region = obj.get("second_match_click_region", [])
    if isinstance(raw_click_region, (list, tuple)) and len(raw_click_region) == 4:
        try:
            parts = [int(part) for part in raw_click_region]
            obj["second_match_click_region"] = parts if parts[2] > 0 and parts[3] > 0 else []
        except (TypeError, ValueError):
            obj["second_match_click_region"] = []
    else:
        obj["second_match_click_region"] = []
    if not isinstance(obj.get("on_success_actions"), list):
        obj["on_success_actions"] = []
    obj["run_code_on_timeout"] = bool(obj.get("run_code_on_timeout", False))
    try:
        obj["not_found_timeout_ms"] = max(
            0, min(86400000, int(obj.get("not_found_timeout_ms", 3000)))
        )
    except (TypeError, ValueError):
        obj["not_found_timeout_ms"] = 3000
    if not isinstance(obj.get("on_timeout_actions"), list):
        obj["on_timeout_actions"] = []
    if obj.get("recognize") == "number":
        # 数字读取只负责在脚本行里产出比较结果，不执行点击或模块代码段。
        obj.update({
            "category": "switch", "template": "", "delay_ms": 0,
            "after_action": "continue", "run_code_after_action": False,
            "on_success_actions": [], "run_code_on_timeout": False,
            "on_timeout_actions": [], "wait_text_absent": False,
        })
    return obj


def load_module_objects() -> dict[str, dict]:
    """Return {module id -> module object}; 旧图片路径 key 惰性兼容。"""
    result: dict[str, dict] = {}
    if TEMPLATE_REGIONS_PATH.exists():
        try:
            value = json.loads(TEMPLATE_REGIONS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        for key, raw in value.items():
            if not isinstance(key, str) or not key.strip():
                continue
            normalized = _normalize_object(raw, key)
            if normalized is not None:
                result[key] = normalized
    # 固定特殊动作常驻仓库（仅内存态补种，不落盘），每一项独立补齐。
    # 固定动作的旧误导名称不再展示或持久化。动作本身按 type 保存，改名
    # 不影响脚本和模块代码段中的 end_current_script。
    result.pop("结束当前脚本，执行工作流下一项", None)
    for name in ("重新执行工作流", END_CURRENT_SCRIPT_LABEL):
        result.setdefault(name, {
            "category": "special", "name": name, "pure_action": True,
        })
    return result


def save_module_objects(objects: dict[str, dict | list]) -> None:
    payload: dict[str, dict] = {}
    for key, raw in objects.items():
        normalized = _normalize_object(raw, key)
        if normalized is not None:
            payload[key] = normalized
    TEMPLATE_REGIONS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def registered_module_object(module_key: str | Path) -> dict | None:
    """Look up by independent module id, with legacy image-path fallback."""
    objects = load_module_objects()
    raw = str(module_key).strip()
    if raw in objects:
        return objects[raw]
    try:
        legacy = display_path(raw)
    except (OSError, ValueError):
        legacy = raw
    if legacy in objects:
        return objects[legacy]
    return None


def module_objects_by_category(category: str) -> dict[str, dict]:
    """Return module objects belonging to one of the four module categories."""
    return {
        key: obj for key, obj in load_module_objects().items()
        if obj.get("category") == category
    }


def update_module_object(key: str, obj: dict, old_key: str = "") -> dict:
    """Persist one module object; 改名（更换图片）时移除旧条目。返回最新对象仓库。"""
    objects = load_module_objects()
    if old_key and old_key != key:
        objects.pop(old_key, None)
    objects[key] = obj
    save_module_objects(objects)
    return objects


def load_template_regions() -> dict[str, list[int]]:
    """Return {display path -> [x, y, w, h]} derived from registered module objects.

    纯动作特殊模块（无图片，pure_action=True）不参与模板下拉。
    """
    return {
        str(obj.get("template") or key): obj["region"]
        for key, obj in load_module_objects().items()
        if not obj.get("pure_action")
        and obj.get("recognize") not in ("none", "number")
        and str(obj.get("template", "")).strip()
    }


def save_template_regions(mapping: dict[str, list[int] | dict]) -> None:
    """Legacy save API：把区域（或对象）合并进对象仓库，不覆盖对象的行为字段。"""
    objects = load_module_objects()
    for key, raw in mapping.items():
        if isinstance(raw, dict):
            objects[key] = raw
        else:
            objects[key] = dict(objects.get(key, dict(DEFAULT_MODULE_OBJECT)), region=raw)
    save_module_objects(objects)


def registered_template_region(file_path: str | Path) -> list[int] | None:
    """Look up the registered search region [x, y, w, h] for a template image."""
    return load_template_regions().get(display_path(file_path))


ensure_dirs()
