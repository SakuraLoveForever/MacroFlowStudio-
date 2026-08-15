from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


SCRIPT_VERSION = 1
WORKFLOW_VERSION = 1
DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS = 1000
DEFAULT_MOUSE_MOVE_INTERVAL_MS = 20
DEFAULT_RECORDED_SCREEN = {"left": 0, "top": 0, "width": 1920, "height": 1080}
ACTION_ID_KEY = "action_id"
NEXT_WORKFLOW_STEP_TARGET_ID = "__next_workflow_step__"
SCRIPT_START_TARGET_ID = "__script_start__"
END_CURRENT_SCRIPT_LABEL = "结束当前最里层脚本，继续执行"
SPECIAL_ACTION_LABELS = {
    "restart_workflow": "重新执行工作流",
    "end_current_script": END_CURRENT_SCRIPT_LABEL,
    "jump_current_script_last": "跳转到当前脚本最后一行",
}


def special_action_label(action_type: str) -> str:
    """Return the visible name of one fixed special action, or an empty string."""
    return SPECIAL_ACTION_LABELS.get(str(action_type), "")


def new_action_id() -> str:
    return uuid.uuid4().hex


def ensure_action_ids(actions: list[dict[str, Any]]) -> bool:
    """Give every action a unique stable identity and migrate legacy row jumps."""
    changed = False
    used: set[str] = set()
    for action in actions:
        action_id = str(action.get(ACTION_ID_KEY, "")).strip()
        if not action_id or action_id in used:
            action_id = new_action_id()
            action[ACTION_ID_KEY] = action_id
            changed = True
        used.add(action_id)

    for action in actions:
        if action.get("type") == "jump" and not str(action.get("jump_action_id", "")).strip():
            try:
                legacy_row = int(action.get("jump_row", 0))
            except (TypeError, ValueError):
                legacy_row = 0
            if 1 <= legacy_row <= len(actions):
                action["jump_action_id"] = actions[legacy_row - 1][ACTION_ID_KEY]
                changed = True
        if action.get("type") != "image_match":
            continue
        for behavior, target_key, legacy_key in (
            ("on_timeout", "timeout_jump_action_id", "timeout_jump_row"),
            ("on_found", "found_jump_action_id", "found_jump_row"),
        ):
            if action.get(behavior) != "jump":
                continue
            target_id = str(action.get(target_key, "")).strip()
            if target_id:
                continue
            try:
                legacy_row = int(action.get(legacy_key, 0))
            except (TypeError, ValueError):
                legacy_row = 0
            if 1 <= legacy_row <= len(actions):
                action[target_key] = actions[legacy_row - 1][ACTION_ID_KEY]
                changed = True
    return changed


def clone_actions_with_new_ids(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clone actions as new objects and keep jumps within the clone connected."""
    prepared = copy.deepcopy(actions)
    ensure_action_ids(prepared)
    clones = copy.deepcopy(prepared)
    id_map: dict[str, str] = {}
    for source, clone in zip(prepared, clones):
        source_id = str(source.get(ACTION_ID_KEY, "")).strip()
        replacement = new_action_id()
        clone[ACTION_ID_KEY] = replacement
        if source_id:
            id_map[source_id] = replacement
    for clone in clones:
        for target_key in ("timeout_jump_action_id", "found_jump_action_id", "jump_action_id"):
            target_id = str(clone.get(target_key, "")).strip()
            if target_id in id_map:
                clone[target_key] = id_map[target_id]
    return clones


def ensure_workflow_step_ids(steps: list[dict[str, Any]]) -> bool:
    """Give every workflow step a stable identity."""
    changed = False
    used: set[str] = set()
    for step in steps:
        step_id = str(step.get("step_id", "")).strip()
        if not step_id or step_id in used:
            step["step_id"] = new_action_id()
            changed = True
        used.add(step_id)
    return changed


@dataclass
class MacroScript:
    name: str = "未命名脚本"
    actions: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    settings: dict[str, Any] = field(default_factory=lambda: {
        "record_mode": "auto",
        "move_interval_ms": DEFAULT_MOUSE_MOVE_INTERVAL_MS,
        "recorded_screen": dict(DEFAULT_RECORDED_SCREEN),
    })
    is_global: bool = False
    version: int = SCRIPT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MacroScript":
        settings = dict(data.get("settings", {}))
        settings.setdefault("recorded_screen", dict(DEFAULT_RECORDED_SCREEN))
        actions = list(data.get("actions", []))
        settings.setdefault("trigger", {})
        # 旧脚本迁移：全局检测曾经是一条动作。现在触发条件移到 settings["trigger"]，
        # 语句体只保留其余动作；迁移幂等（已有 trigger 则跳过）。
        # v1.68 起普通脚本可内嵌全局模块行（global_detect + jump_row），必须跳过。
        if not settings["trigger"]:
            for index, action in enumerate(actions):
                if str(action.get("type")) == "global_detect":
                    if "jump_row" in action:
                        break  # 内嵌全局模块行：保留在语句体中。
                    trigger = dict(action)
                    trigger.pop("type", None)
                    settings["trigger"] = trigger
                    del actions[index]
                    break
        return cls(
            name=str(data.get("name", "未命名脚本")),
            actions=actions,
            description=str(data.get("description", "")),
            settings=settings,
            is_global=bool(data.get("is_global", False)),
            version=int(data.get("version", SCRIPT_VERSION)),
        )


def is_global_script(data: dict[str, Any]) -> bool:
    """A script is global when marked or carries a trigger.

    v1.68 起普通脚本可内嵌全局模块行（global_detect + jump_row），
    它们不代表全局脚本，故不再扫描动作。旧脚本经 from_dict 迁移后
    触发条件已进入 settings["trigger"]。
    """
    if bool(data.get("is_global")):
        return True
    settings = data.get("settings", {})
    return bool(settings.get("trigger"))


@dataclass
class Workflow:
    name: str = "未命名工作流"
    steps: list[dict[str, Any]] = field(default_factory=list)
    start_at: str = ""
    start_delay_enabled: bool = False
    start_delay_seconds: int = 5
    restart_target_step_id: str = ""
    version: int = WORKFLOW_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        steps = []
        for raw_step in data.get("steps", []):
            step = dict(raw_step)
            step.setdefault("repeat_interval_ms", DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS)
            step.setdefault("enabled", True)
            step.setdefault("unlimited", False)
            steps.append(step)
        ensure_workflow_step_ids(steps)
        return cls(
            name=str(data.get("name", "未命名工作流")),
            steps=steps,
            start_at=str(data.get("start_at", "")),
            start_delay_enabled=bool(data.get("start_delay_enabled", False)),
            start_delay_seconds=max(0, min(86400, int(data.get("start_delay_seconds", 5)))),
            restart_target_step_id=str(data.get("restart_target_step_id", "")),
            version=int(data.get("version", WORKFLOW_VERSION)),
        )
