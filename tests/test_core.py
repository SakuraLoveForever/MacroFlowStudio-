from __future__ import annotations

import sys
from pathlib import Path

# 源码包位于项目根/src：从 tests/ 运行（python tests/test_core.py）时
# 把项目根与 src 加入导入路径，才能解析 macroflow.* 包。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import ctypes
import inspect
import json
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, call, patch

import cv2
import numpy as np
import macroflow.ui.dialogs as dialog_module

from macroflow.core.alerts import play_alert
from macroflow.ui.app import (
    BACKUP_INTERVAL_CHOICES, BACKUP_INTERVAL_MS, MacroFlowApp,
    action_summary, coordinate_scale_summary,
    disable_combobox_wheel_selection,
    key_action_matches,
    set_matching_key_action_delays,
    recorded_action_description, floating_notice_xy,
    windows_startup_command, workflow_execution_progress, workflow_script_name,
    spawn_new_instance,
)
from macroflow.ui.dialogs import (
    KEY_HINT_CAPTURING, BatchModuleScriptDialog, ClickDialog, CloseAppDialog, GlobalDetectDialog,
    DurationVar, ImageActionDialog, JumpActionDialog, KeyActionDialog, ModalDialog,
    ModulePickerDialog, ModuleReferenceDelayDialog, MultiConditionClickDialog,
    MouseMoveDialog, OcrActionDialog, OcrCompareActionDialog, OpenAppDialog, RepeatClickDialog, RestartWorkflowTargetDialog,
    ScreenPointPicker,
    ScreenOffsetPicker, ScreenRegionPicker, ScriptDirectoriesDialog, TemplateRegionFormDialog,
    TemplateRegionManagerDialog, TextActionDialog, activate_main_after_modal, ancestor_windows,
    drag_selection_region, edit_action,
    configure_module_tree_styles,
    fallback_template_options, fit_window_to_content,
    image_action_option_defaults, image_click_target_defaults,
    image_found_jump_target_options, image_jump_target_options,
    image_timeout_option_label, image_timeout_option_value,
    image_timeout_option_defaults, key_to_vk,
    module_action_for_key, module_manager_label, module_manager_selection_colors,
    module_manager_special_action_summary, module_manager_tag,
    pinyin_sort_key, prepend_module_to_scripts,
    remove_module_from_scripts, script_category_for_path,
    registered_template_options, restart_workflow_row_options, select_jump_target_label, vk_to_key_name,
    restore_modal_after_overlay, show_floating_notice,
    segment_action_is_blocking, segment_row_label,
    selectable_target_windows,
)
from macroflow.core.image_match import find_template, find_template_in_image
from macroflow.core.ocr import (
    extract_ocr_integer, find_expected_match, format_ocr_observation, matches_expected,
    parse_ocr_number_pair, recognize_image_with_boxes,
)
from macroflow.input.input_guard import (
    FocusInputGuard, KBDLLHOOKSTRUCT, KeyCapturer, LLKHF_INJECTED,
    LLMHF_INJECTED, RESERVED_HOTKEY_VKS, VK_ESCAPE, VK_F12, VK_F9,
    WM_KEYDOWN, WM_SYSKEYDOWN, should_block_keyboard, should_block_mouse,
)
from macroflow.core.models import (
    ACTION_ID_KEY, DEFAULT_MOUSE_MOVE_INTERVAL_MS, DEFAULT_RECORDED_SCREEN,
    DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
    NEXT_WORKFLOW_STEP_TARGET_ID, SCRIPT_START_TARGET_ID, MacroScript, Workflow,
    clone_actions_with_new_ids, ensure_action_ids,
    ensure_workflow_step_ids, is_global_script,
)
from macroflow.execution.player import (
    AdvanceToNextWorkflowStep, EndCurrentScriptRequest, GuardJumpRequest,
    JUMP_CURRENT_SCRIPT_LAST_RESULT, MacroPlayer, PlaybackStopped,
    scale_screen_point, screen_template_scale,
)
from macroflow.input.rawinput import RawMouseListener
from macroflow.input.recorder import MacroRecorder
from macroflow.core.storage import (
    BASE_DIR, available_script_path, backup_script, display_path, load_app_settings,
    load_module_images_dir, load_module_objects, load_script,
    load_template_regions, load_workflow,
    migrate_workflow_templates,
    module_image_inventory,
    registered_module_object, registered_template_region, remap_hotkey_script_bindings,
    resolve_path, save_app_settings, save_script,
    save_module_images_dir, save_module_objects,
    save_template_regions, save_workflow,
)
from macroflow.input.wininput import (
    MACROFLOW_INPUT_TAG, WindowInfo, activate_window, force_english_input, is_cursor_near_window_center,
    resolve_window_signature, send_move_relative, set_input_dispatcher, show_window, show_window_no_activate,
)


class ComboboxWheelTests(unittest.TestCase):
    def test_duration_var_switches_units_without_changing_milliseconds(self):
        master = tk.Tcl()
        value = DurationVar(1500, master=master)
        self.assertEqual(value.get(), "1500")
        value.unit.set("s")
        self.assertEqual(value._raw(), "1.5")
        self.assertEqual(value.get(), "1500")
        value.set("2.25")
        self.assertEqual(value.get(), "2250")
        value.unit.set("ms")
        self.assertEqual(value._raw(), "2250")

    def test_duration_var_switches_minutes_without_changing_milliseconds(self):
        master = tk.Tcl()
        value = DurationVar(90000, master=master)
        value.unit.set("min")
        self.assertEqual(value._raw(), "1.5")
        self.assertEqual(value.get(), "90000")
        value.set("2")
        self.assertEqual(value.get(), "120000")
        value.unit.set("s")
        self.assertEqual(value._raw(), "120")
        value.unit.set("ms")
        self.assertEqual(value._raw(), "120000")

    def test_all_combobox_wheel_sequences_are_blocked(self):
        root = Mock()
        disable_combobox_wheel_selection(root)
        self.assertEqual(
            [call.args[:2] for call in root.bind_class.call_args_list],
            [
                ("TCombobox", "<MouseWheel>"),
                ("TCombobox", "<Button-4>"),
                ("TCombobox", "<Button-5>"),
            ],
        )
        for call in root.bind_class.call_args_list:
            self.assertEqual(call.args[2](Mock()), "break")


class GameSetupNoteTests(unittest.TestCase):
    def test_open_game_setup_note_saves_custom_content(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app._game_setup_note = None
        app._persist_sidebar_settings = Mock(return_value=True)
        app._set_status = Mock()
        app._log = Mock()
        dialog = Mock()
        dialog.show.return_value = "custom game setup note"

        with patch("macroflow.ui.app.GameSetupNoteDialog", return_value=dialog):
            app.open_game_setup_note()

        self.assertEqual(app._game_setup_note, "custom game setup note")
        app._persist_sidebar_settings.assert_called_once_with(show_feedback=True)
        app._set_status.assert_called_once_with("游戏设置说明已保存", "success")

    def test_open_game_setup_note_reports_save_failure(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app._game_setup_note = None
        app._persist_sidebar_settings = Mock(return_value=False)
        app._set_status = Mock()
        app._log = Mock()
        dialog = Mock()
        dialog.show.return_value = "custom game setup note"

        with patch("macroflow.ui.app.GameSetupNoteDialog", return_value=dialog):
            app.open_game_setup_note()

        self.assertEqual(app._game_setup_note, "custom game setup note")
        app._persist_sidebar_settings.assert_called_once_with(show_feedback=True)
        app._set_status.assert_called_once_with("游戏设置说明保存失败", "danger")


class StorageTests(unittest.TestCase):
    def test_relative_display_path_is_based_on_app_dir_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as app_folder, \
             tempfile.TemporaryDirectory() as launch_folder:
            base = Path(app_folder)
            registry = base / "template_regions.json"
            key = str(Path("images") / "部分" / "专注.png")
            registry.write_text(json.dumps({
                key: {
                    "category": "switch", "name": "专注",
                    "region": [1, 2, 30, 40],
                },
            }, ensure_ascii=False), encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(launch_folder)
                with patch("macroflow.core.storage.BASE_DIR", base), \
                     patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", registry):
                    self.assertEqual(display_path(key), key)
                    obj = registered_module_object(key)
            finally:
                os.chdir(previous_cwd)

            self.assertIsNotNone(obj)
            self.assertEqual(obj["name"], "专注")

    def test_module_image_directory_round_trip_and_inventory_status(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            images = base / "images" / "部分"
            images.mkdir(parents=True)
            adopted = images / "已采用.png"
            unused = images / "未采用.jpg"
            ignored = images / "说明.txt"
            adopted.write_bytes(b"png")
            unused.write_bytes(b"jpg")
            ignored.write_text("x", encoding="utf-8")
            settings_path = base / "module_settings.json"
            with patch("macroflow.core.storage.BASE_DIR", base), \
                 patch("macroflow.core.storage.MODULE_SETTINGS_PATH", settings_path):
                saved = save_module_images_dir(images)
                self.assertEqual(load_module_images_dir(), saved)
                rows = module_image_inventory(
                    saved,
                    {"images/部分/已采用.png": {"category": "switch"}},
                )
            self.assertEqual([row["status"] for row in rows], ["已采用（1 个）", "未采用"])
            self.assertEqual(rows[0]["module_key"], "images/部分/已采用.png")

    def test_same_image_can_back_independent_switch_and_global_modules(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder) / "template_regions.json"
            objects = {
                "module:switch-id": {
                    "category": "switch", "name": "切换入口",
                    "template": "images/shared.png", "region": [1, 2, 30, 40],
                    "after_action": "click_match",
                },
                "module:global-id": {
                    "category": "global", "name": "全局保护",
                    "template": "images/shared.png", "region": [5, 6, 70, 80],
                    "after_action": "continue", "hold_ms": 2500,
                },
            }
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", registry):
                save_module_objects(objects)
                loaded = load_module_objects()

            self.assertEqual(loaded["module:switch-id"]["template"], "images/shared.png")
            self.assertEqual(loaded["module:global-id"]["template"], "images/shared.png")
            self.assertEqual(loaded["module:switch-id"]["region"], [1, 2, 30, 40])
            self.assertEqual(loaded["module:global-id"]["region"], [5, 6, 70, 80])
            self.assertEqual(loaded["module:switch-id"]["after_action"], "click_match")
            self.assertEqual(loaded["module:global-id"]["after_action"], "continue")

    def test_global_module_can_disable_hold_delay_without_losing_value(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder) / "template_regions.json"
            objects = {
                "module:instant": {
                    "category": "workflow_global", "name": "立即执行",
                    "template": "images/shared.png", "hold_enabled": False,
                    "hold_ms": 2500,
                },
            }
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", registry):
                save_module_objects(objects)
                loaded = load_module_objects()["module:instant"]
        self.assertFalse(loaded["hold_enabled"])
        self.assertEqual(loaded["hold_ms"], 2500)

    def test_module_objects_backfill_missing_name_from_template(self):
        # 旧对象可能没有 name（过去以图片路径为键靠文件名兜底）；复制成
        # module:<uuid> 键后兜底会退化成 uuid，加载时按模板文件名补名。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                save_module_objects({
                    "module:legacy": {
                        "category": "workflow_global",
                        "template": "images/legacy.png", "region": [1, 2, 30, 40],
                    },
                })
                loaded = load_module_objects()["module:legacy"]
        self.assertEqual(loaded["name"], "legacy")
        self.assertEqual(loaded["template"], "images/legacy.png")

    def test_old_list_module_entries_get_name_from_key(self):
        # 旧格式 [x,y,w,h] 列表条目没有名字字段：用图片路径文件名兜底。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            path.write_text(json.dumps({"images/old.png": [1, 2, 30, 40]}),
                            encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                loaded = load_module_objects()["images/old.png"]
        self.assertEqual(loaded["name"], "old")

    def test_action_ids_survive_reorder_and_legacy_jump_is_migrated(self):
        actions = [
            {"type": "comment", "text": "A"},
            {"type": "image_match", "on_timeout": "jump", "timeout_jump_row": 1},
        ]
        self.assertTrue(ensure_action_ids(actions))
        target_id = actions[0][ACTION_ID_KEY]
        self.assertEqual(actions[1]["timeout_jump_action_id"], target_id)

        actions.insert(0, {"type": "comment", "text": "新插入"})
        ensure_action_ids(actions)

        self.assertEqual(actions[2]["timeout_jump_action_id"], target_id)
        self.assertEqual(actions[1]["text"], "A")

    def test_cloned_actions_get_new_ids_and_internal_jump_is_remapped(self):
        actions = [
            {"type": "comment", "text": "目标", ACTION_ID_KEY: "target"},
            {
                "type": "image_match", ACTION_ID_KEY: "jump",
                "on_timeout": "jump", "timeout_jump_action_id": "target",
            },
        ]

        clones = clone_actions_with_new_ids(actions)

        self.assertNotEqual(clones[0][ACTION_ID_KEY], "target")
        self.assertNotEqual(clones[1][ACTION_ID_KEY], "jump")
        self.assertEqual(clones[1]["timeout_jump_action_id"], clones[0][ACTION_ID_KEY])

    def test_cloned_unconditional_jump_is_remapped(self):
        actions = [
            {"type": "comment", "text": "目标", ACTION_ID_KEY: "target"},
            {"type": "jump", ACTION_ID_KEY: "jump", "jump_action_id": "target"},
        ]
        clones = clone_actions_with_new_ids(actions)
        self.assertEqual(clones[1]["jump_action_id"], clones[0][ACTION_ID_KEY])

    def test_default_move_interval_is_20_ms(self):
        self.assertEqual(DEFAULT_MOUSE_MOVE_INTERVAL_MS, 20)
        self.assertEqual(
            MacroScript().settings["move_interval_ms"], DEFAULT_MOUSE_MOVE_INTERVAL_MS,
        )
        self.assertEqual(MacroScript().settings["recorded_screen"], DEFAULT_RECORDED_SCREEN)

    def test_script_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.json"
            value = MacroScript(name="测试", actions=[{"type": "delay", "ms": 50}])
            save_script(value, path)
            loaded = load_script(path)
            self.assertEqual(loaded.name, "测试")
            self.assertEqual(loaded.actions[0]["ms"], 50)

    def test_available_script_path_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder, patch("macroflow.core.storage.SCRIPTS_DIR", Path(folder)):
            (Path(folder) / "已有脚本.json").write_text("old", encoding="utf-8")
            self.assertEqual(available_script_path("已有脚本").name, "已有脚本 (2).json")

    def test_available_script_path_uses_custom_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "目标.json").write_text("x", encoding="utf-8")
            path = available_script_path("目标", base)
            self.assertEqual(path, base / "目标 (2).json")

    def test_renaming_direction_script_remaps_only_matching_hotkey_bindings(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old_path = base / "scripts" / "方向" / "原方向.json"
            new_path = base / "scripts" / "方向" / "新方向.json"
            old_path.parent.mkdir(parents=True)
            bindings = [
                {"key": "J", "script": "scripts/方向/原方向.json"},
                {"key": "K", "script": str(old_path)},
                {"key": "L", "script": "scripts/方向/其他方向.json"},
                {"key": "M", "script": "scripts/关卡/原方向.json"},
            ]

            with patch("macroflow.core.storage.BASE_DIR", base):
                updated = remap_hotkey_script_bindings(bindings, old_path, new_path)
                expected_path = display_path(new_path)

            self.assertEqual(updated, 2)
            self.assertEqual(bindings[0]["script"], expected_path)
            self.assertEqual(bindings[1]["script"], expected_path)
            self.assertEqual(bindings[2]["script"], "scripts/方向/其他方向.json")
            self.assertEqual(bindings[3]["script"], "scripts/关卡/原方向.json")

    def test_script_backup_overwrites_one_stable_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            scripts = base / "scripts"
            backups = base / "backups"
            scripts.mkdir()
            source = scripts / "领取.json"
            source.write_text('{"version": 1}', encoding="utf-8")
            with patch("macroflow.core.storage.SCRIPTS_DIR", scripts), patch("macroflow.core.storage.SCRIPT_BACKUPS_DIR", backups):
                first = backup_script(source)
                source.write_text('{"version": 2}', encoding="utf-8")
                second = backup_script(source)
            self.assertEqual(first, second)
            self.assertEqual(second.read_text(encoding="utf-8"), '{"version": 2}')
            self.assertEqual(list(backups.rglob("*.json")), [second])

    def test_source_startup_command_quotes_python_and_app(self):
        with patch("macroflow.ui.app.sys.frozen", False, create=True):
            command = windows_startup_command()
        self.assertIn(Path(os.sys.executable).name, command)
        self.assertIn("app.py", command)

    def test_workflow_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.json"
            value = Workflow(name="流程", steps=[{"script": "x.json", "repeats": 3}])
            save_workflow(value, path)
            loaded = load_workflow(path)
            self.assertEqual(loaded.steps[0]["repeats"], 3)
            self.assertEqual(
                loaded.steps[0]["repeat_interval_ms"],
                DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
            )
            self.assertTrue(loaded.steps[0]["enabled"])
            self.assertFalse(loaded.steps[0]["unlimited"])

    def test_migrate_workflow_templates_writes_workflow_files_once(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old = base / "workflow_templates.json"
            old.write_text(json.dumps({"templates": {
                "日常": Workflow(name="日常", steps=[{
                    "script": "scripts/a.json", "repeats": 3, "before_ms": 200,
                }]).to_dict(),
                "活动": Workflow(name="活动", steps=[{
                    "kind": "global_module", "config": {"module_key": "module:event"},
                }, {
                    "script": "scripts/b.json", "unlimited": True,
                    "repeat_interval_ms": 1500,
                }]).to_dict(),
            }}, ensure_ascii=False), encoding="utf-8")
            with patch("macroflow.core.storage.BASE_DIR", base), \
                 patch("macroflow.core.storage.WORKFLOWS_DIR", base / "workflows"), \
                 patch("macroflow.core.storage.SCRIPTS_DIR", base / "scripts"), \
                 patch("macroflow.core.storage.IMAGES_DIR", base / "images"), \
                 patch("macroflow.core.storage.SCRIPT_BACKUPS_DIR", base / "backups" / "scripts"):
                migrated = migrate_workflow_templates()
                # 幂等：第二次调用不再迁移。
                second = migrate_workflow_templates()
            self.assertEqual(migrated, 2)
            self.assertEqual(second, 0)
            daily = json.loads((base / "workflows" / "日常.json").read_text(encoding="utf-8"))
            self.assertEqual(daily["steps"][0]["repeats"], 3)
            event = json.loads((base / "workflows" / "活动.json").read_text(encoding="utf-8"))
            self.assertEqual(event["steps"][0]["config"]["module_key"], "module:event")
            self.assertTrue(event["steps"][1]["unlimited"])
            self.assertTrue((base / "workflow_templates.migrated.json").is_file())
            self.assertFalse(old.is_file())

    def test_migrate_workflow_templates_skips_existing_files_and_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "workflows").mkdir(parents=True)
            (base / "workflows" / "日常.json").write_text(
                json.dumps({"name": "日常", "steps": []}), encoding="utf-8",
            )
            old = base / "workflow_templates.json"
            old.write_text(json.dumps({"templates": {
                "日常": Workflow(name="日常", steps=[]).to_dict(),
                "新模板": Workflow(name="新模板", steps=[]).to_dict(),
            }}, ensure_ascii=False), encoding="utf-8")
            with patch("macroflow.core.storage.BASE_DIR", base), \
                 patch("macroflow.core.storage.WORKFLOWS_DIR", base / "workflows"), \
                 patch("macroflow.core.storage.SCRIPTS_DIR", base / "scripts"), \
                 patch("macroflow.core.storage.IMAGES_DIR", base / "images"), \
                 patch("macroflow.core.storage.SCRIPT_BACKUPS_DIR", base / "backups" / "scripts"):
                migrated = migrate_workflow_templates()
            self.assertEqual(migrated, 1)
            self.assertTrue((base / "workflows" / "新模板.json").is_file())
            # 已有文件未被覆盖。
            self.assertEqual(
                json.loads((base / "workflows" / "日常.json").read_text(encoding="utf-8")),
                {"name": "日常", "steps": []},
            )
            with patch("macroflow.core.storage.BASE_DIR", base), \
                 patch("macroflow.core.storage.WORKFLOWS_DIR", base / "workflows"), \
                 patch("macroflow.core.storage.SCRIPTS_DIR", base / "scripts"), \
                 patch("macroflow.core.storage.IMAGES_DIR", base / "images"), \
                 patch("macroflow.core.storage.SCRIPT_BACKUPS_DIR", base / "backups" / "scripts"):
                self.assertEqual(migrate_workflow_templates(), 0)  # 无旧文件
        with tempfile.TemporaryDirectory() as empty:
            with patch("macroflow.core.storage.BASE_DIR", Path(empty)):
                self.assertEqual(migrate_workflow_templates(), 0)

    def test_sidebar_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app_settings.json"
            value = {
                "sound_enabled": False,
                "mini_window_enabled": True,
                "close_action": "tray",
                "focus_mode_enabled": False,
                "activate_target_enabled": False,
                "floating_notice_position": "右下",
                "activation_window": {
                    "title": "执行窗口", "class_name": "RunWindow",
                    "process_path": "C:/Game/run.exe",
                },
                "activation_window_enabled": True,
                "activation_window_draft": {
                    "title": "最近前置窗口", "class_name": "DraftWindow",
                    "process_path": "C:/Game/draft.exe",
                },
                "activation_window_draft_enabled": True,
                "record_mode": "relative",
                "move_interval_ms": 125,
                "repeat": 7,
                "bound_window": {"title": "测试窗口", "class_name": "TestWindow"},
                "workflow_draft": Workflow(
                    name="上次流程", steps=[{"script": "scripts/a.json", "repeats": 2}]
                ).to_dict(),
                "backup_interval": "1周",
                "backup_interval_minutes": 30,
            }
            with patch("macroflow.core.storage.SETTINGS_PATH", path):
                save_app_settings(value)
                loaded = load_app_settings()
            self.assertEqual(loaded["move_interval_ms"], 125)
            self.assertEqual(loaded["repeat"], 7)
            self.assertEqual(loaded["bound_window"]["title"], "测试窗口")
            self.assertEqual(loaded["workflow_draft"]["name"], "上次流程")
            self.assertEqual(loaded["workflow_draft"]["steps"][0]["repeats"], 2)
            self.assertFalse(loaded["focus_mode_enabled"])
            self.assertFalse(loaded["activate_target_enabled"])
            self.assertEqual(loaded["floating_notice_position"], "右下")
            self.assertNotIn("activation_window", loaded)
            self.assertNotIn("activation_window_enabled", loaded)
            self.assertTrue(loaded["activation_window_draft_enabled"])
            self.assertEqual(loaded["activation_window_draft"]["title"], "最近前置窗口")
            self.assertNotIn("execution_mode", loaded)
            self.assertEqual(loaded["backup_interval"], "1周")
            self.assertNotIn("backup_interval_minutes", loaded)

    def test_last_script_path_defaults_to_empty(self):
        # 旧版设置文件没有 last_script_path 字段：默认空字符串（不恢复脚本）。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app_settings.json"
            path.write_text(json.dumps({"sound_enabled": True}), encoding="utf-8")
            with patch("macroflow.core.storage.SETTINGS_PATH", path):
                loaded = load_app_settings()
        self.assertEqual(loaded["last_script_path"], "")

    def test_backup_interval_is_limited_to_three_fixed_choices(self):
        self.assertEqual(BACKUP_INTERVAL_CHOICES, ("1h", "1天", "1周"))
        self.assertEqual(BACKUP_INTERVAL_MS["1h"], 3_600_000)
        self.assertEqual(BACKUP_INTERVAL_MS["1天"], 86_400_000)
        self.assertEqual(BACKUP_INTERVAL_MS["1周"], 604_800_000)

    def test_from_dict_migrates_legacy_global_detect_action_to_trigger(self):
        # 旧脚本：全局检测是一条动作。迁移后进入 settings["trigger"]，动作被移除。
        raw = {
            "name": "旧全局",
            "actions": [
                {"type": "global_detect", "template": "images/g.png",
                 "hold_ms": 1500, "region": [10, 20, 30, 40]},
                {"type": "delay", "delay_ms": 100},
            ],
        }
        script = MacroScript.from_dict(raw)
        trigger = script.settings["trigger"]
        self.assertEqual(trigger["template"], "images/g.png")
        self.assertEqual(trigger["hold_ms"], 1500)
        self.assertEqual(trigger["region"], [10, 20, 30, 40])
        self.assertNotIn("type", trigger)
        self.assertEqual(script.actions, [{"type": "delay", "delay_ms": 100}])
        self.assertTrue(is_global_script(script.to_dict()))

    def test_from_dict_skips_migration_when_trigger_already_present(self):
        raw = {
            "name": "新全局",
            "settings": {"trigger": {"template": "images/g.png"}},
            "actions": [{"type": "delay", "delay_ms": 100}],
        }
        script = MacroScript.from_dict(raw)
        self.assertEqual(script.settings["trigger"]["template"], "images/g.png")
        self.assertEqual(len(script.actions), 1)
        self.assertTrue(is_global_script(script.to_dict()))

    def test_script_trigger_config_prefers_settings_and_falls_back_to_action(self):
        # 新格式：settings["trigger"] 优先。
        new_script = MacroScript(actions=[], settings={"trigger": {"template": "new.png"}})
        self.assertEqual(
            MacroFlowApp._script_trigger_config(new_script)["template"], "new.png",
        )
        # 回退：只有全局检测动作（未迁移的旧 JSON）。
        old_script = MacroScript(actions=[
            {"type": "global_detect", "template": "old.png"},
            {"type": "delay", "delay_ms": 1},
        ])
        self.assertEqual(
            MacroFlowApp._script_trigger_config(old_script)["template"], "old.png",
        )
        # 普通脚本：没有触发配置。
        plain = MacroScript(actions=[{"type": "delay", "delay_ms": 1}])
        self.assertEqual(MacroFlowApp._script_trigger_config(plain), {})

    def test_from_dict_keeps_embedded_module_row_in_normal_script(self):
        # v1.68：普通脚本内嵌全局模块行（global_detect + jump_row）不能迁移为触发条件。
        raw = {
            "name": "普通",
            "actions": [
                {"type": "global_detect", "template": "images/g.png", "jump_row": 3},
                {"type": "delay", "delay_ms": 100},
            ],
        }
        script = MacroScript.from_dict(raw)
        self.assertEqual(script.settings["trigger"], {})
        self.assertEqual(len(script.actions), 2)
        self.assertEqual(script.actions[0]["jump_row"], 3)
        self.assertFalse(is_global_script(script.to_dict()))

    def test_from_dict_never_migrates_module_row_even_when_marked_global(self):
        # 即使脚本被标记为全局，带 jump_row 的模块行也保留在语句体中。
        raw = {
            "name": "标记全局",
            "is_global": True,
            "actions": [
                {"type": "global_detect", "template": "x.png", "jump_row": 2},
            ],
        }
        script = MacroScript.from_dict(raw)
        self.assertEqual(script.settings["trigger"], {})
        self.assertEqual(len(script.actions), 1)
        self.assertTrue(is_global_script(script.to_dict()))

    def test_script_trigger_config_skips_embedded_module_rows(self):
        # v1.68：普通脚本内嵌全局模块行（带 jump_row）不是触发条件，回退扫描跳过。
        script = MacroScript(actions=[
            {"type": "global_detect", "template": "module.png", "jump_row": 2},
        ])
        self.assertEqual(MacroFlowApp._script_trigger_config(script), {})


class BindingTests(unittest.TestCase):
    def test_unbind_window_persists_cleared_binding(self):
        # 解除绑定必须持久化，否则重启后旧绑定被恢复，录制/执行又对准
        # 用户已明确清除的窗口。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.bound_window = Mock()
        app.saved_window_signature = {"title": "游戏"}
        app.bind_label_var = Mock()
        app._persist_sidebar_settings = Mock()
        app._log = Mock()
        app.unbind_window()
        app._persist_sidebar_settings.assert_called_once()
        self.assertIsNone(app.bound_window)
        self.assertIsNone(app.saved_window_signature)

    def test_region_overlay_restores_main_without_activating_or_moving_it(self):
        main = Mock()
        main.winfo_id.return_value = 123
        dialog = Mock()
        with patch("macroflow.ui.dialogs.show_window_no_activate", return_value=True) as show:
            restored = restore_modal_after_overlay(dialog, main, "zoomed")
        self.assertTrue(restored)
        show.assert_called_once_with(123)
        main.deiconify.assert_called_once()
        main.state.assert_called_once_with("zoomed")
        dialog.deiconify.assert_called_once()
        dialog.grab_set.assert_called_once()

    def test_confirming_image_action_returns_focus_to_main_window(self):
        main = Mock()
        self.assertTrue(activate_main_after_modal(main))
        main.deiconify.assert_called_once()
        main.lift.assert_called_once()
        main.focus_force.assert_called_once()

    def test_drag_selection_region_reads_upper_left_to_lower_right_rectangle(self):
        self.assertEqual(drag_selection_region(120, 80, 620, 380), [120, 80, 500, 300])
        self.assertIsNone(drag_selection_region(620, 380, 120, 80))
        self.assertIsNone(drag_selection_region(120, 80, 121, 81))

    def test_window_picker_excludes_self_and_duplicate_handles(self):
        windows = [
            WindowInfo(10, "MacroFlow", "TkTopLevel"),
            WindowInfo(20, "Game", "GameWindow"),
            WindowInfo(20, "Game duplicate", "GameWindow"),
        ]
        with patch("macroflow.ui.dialogs.is_current_process_window", side_effect=lambda hwnd: hwnd == 10):
            result = selectable_target_windows(windows)
        self.assertEqual([(item.hwnd, item.title) for item in result], [(20, "Game")])

    def test_live_cursor_reader_updates_at_any_screen_position(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.cursor_tracking = True
        app.cursor_tracking_after_id = None
        app.cursor_position_var = Mock()
        app.cursor_tracking_mini_var = Mock()
        app.root = Mock()
        app.root.after.return_value = "poll-id"
        with patch("macroflow.ui.app.get_cursor_pos", side_effect=[(30, 40), (960, 540)]), \
             patch("macroflow.ui.app.get_virtual_screen_rect", return_value=DEFAULT_RECORDED_SCREEN):
            app._poll_cursor_position()
            app._poll_cursor_position()
        self.assertEqual(
            [call.args[0] for call in app.cursor_position_var.set.call_args_list],
            ["(30, 40) · 1920×1080", "(960, 540) · 1920×1080"],
        )
        self.assertEqual(
            [call.args[0] for call in app.cursor_tracking_mini_var.set.call_args_list],
            ["X: 30    Y: 40", "X: 960    Y: 540"],
        )

    def test_every_main_log_line_contains_current_cursor_position(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.log_text = Mock()

        with patch("macroflow.ui.app.get_cursor_pos", return_value=(958, 415)):
            app._log("全局检测已点击")

        inserted = app.log_text.insert.call_args.args[1]
        self.assertRegex(
            inserted,
            r"^\[\d{2}:\d{2}:\d{2}\] \[鼠标 958,415\] 全局检测已点击\n$",
        )

    def test_every_floating_log_line_contains_current_cursor_position(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.mini_steps_text = Mock()
        app.mini_steps_text.winfo_exists.return_value = True
        app.mini_steps_text.index.return_value = "2.0"

        with patch("macroflow.ui.app.get_cursor_pos", return_value=(640, 360)):
            app._append_mini_step("工作流继续")

        inserted = app.mini_steps_text.insert.call_args.args[1]
        self.assertRegex(
            inserted,
            r"^\d{2}:\d{2}:\d{2}  \[鼠标 640,360\] 工作流继续\n$",
        )

    def test_saved_binding_rebinds_restarted_window_by_foreground_class(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.saved_window_signature = {"title": "Game - old session", "class_name": "GameWindow"}
        app.record_mode_var = Mock()
        app.bind_label_var = Mock()
        app.bound_window = None
        current = WindowInfo(222, "Game - new session", "GameWindow")
        with patch("macroflow.ui.app.enum_windows", return_value=[current]), \
             patch("macroflow.ui.app.get_foreground_window_info", return_value=current):
            self.assertTrue(app._restore_saved_window_binding())
        self.assertEqual(app.bound_window.hwnd, 222)
        app.bind_label_var.set.assert_called_once_with("Game - new session")

    def test_disabling_target_activation_preserves_saved_binding(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        signature = {"title": "Game", "class_name": "GameWindow"}
        target = WindowInfo(222, "Game", "GameWindow")
        app.saved_window_signature = signature
        app.bound_window = target
        app.activate_target_enabled_var = Mock()
        app.activate_target_enabled_var.get.return_value = False
        app._persist_sidebar_settings = Mock(return_value=True)
        app._log = Mock()

        app._toggle_target_activation()

        self.assertIs(app.saved_window_signature, signature)
        self.assertIs(app.bound_window, target)
        app._persist_sidebar_settings.assert_called_once_with()
        self.assertIn("目标窗口绑定仍然保留", app._log.call_args.args[0])

    def test_foreground_target_status_matches_current_hwnd(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.bound_window = WindowInfo(222, "Game", "GameWindow")
        app.saved_window_signature = {
            "title": "Game", "class_name": "GameWindow", "process_path": "C:/Game/game.exe",
            "window_rect": (0, 0, 1920, 1080), "client_size": (1920, 1080),
        }
        self.assertTrue(app._foreground_matches_target(WindowInfo(
            333, "Game", "GameWindow", "C:/Game/game.exe",
            (0, 0, 1920, 1080), (1920, 1080),
        )))
        self.assertTrue(app._foreground_matches_target(WindowInfo(
            333, "Game", "GameWindow", "C:/Game/game.exe",
            (0, 0, 1280, 720), (1280, 720),
        )))
        self.assertFalse(app._foreground_matches_target(WindowInfo(
            333, "Game", "OtherWindow", "C:/Game/game.exe",
        )))

    def test_foreground_target_allows_dynamic_title_with_stable_identity(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.bound_window = WindowInfo(222, "旧关卡", "GameWindow", "C:/Game/game.exe")
        app.saved_window_signature = {
            "title": "旧关卡", "class_name": "GameWindow",
            "process_path": "C:/Game/game.exe",
        }

        self.assertTrue(app._foreground_matches_target(WindowInfo(
            333, "新关卡", "GameWindow", "C:/Game/game.exe",
        )))

    def test_bound_hwnd_prefers_matching_foreground_over_still_valid_old_hwnd(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.bound_window = WindowInfo(111, "旧窗口", "GameWindow", "C:/Game/game.exe")
        app.saved_window_signature = {
            "title": "旧窗口", "class_name": "GameWindow",
            "process_path": "C:/Game/game.exe",
        }
        app.bind_label_var = Mock()
        foreground = WindowInfo(222, "当前游戏", "GameWindow", "C:/Game/game.exe")

        with patch("macroflow.ui.app.get_foreground_window_info", return_value=foreground), \
             patch("macroflow.ui.app.is_current_process_window", return_value=False):
            hwnd = app._bound_hwnd()

        self.assertEqual(hwnd, 222)
        self.assertEqual(app.bound_window.hwnd, 222)
        app.bind_label_var.set.assert_called_once_with("当前游戏")

    def test_execution_clock_resets_only_for_new_run(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.execution_started_at = 123.0
        app.mini_elapsed_var = Mock()

        with patch("macroflow.ui.app.time.perf_counter", return_value=456.0):
            app._reset_execution_clock_for_new_run(None)
        self.assertEqual(app.execution_started_at, 456.0)
        app.mini_elapsed_var.set.assert_called_once_with("00:00")

        app.mini_elapsed_var.reset_mock()
        with patch("macroflow.ui.app.time.perf_counter", return_value=999.0):
            app._reset_execution_clock_for_new_run(4)
        self.assertEqual(app.execution_started_at, 456.0)
        app.mini_elapsed_var.set.assert_not_called()

    def test_reused_execution_mini_restarts_stopped_refresh_loop(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.mini_window = Mock()
        app.mini_window.winfo_exists.return_value = True
        app.mini_window.winfo_ismapped.return_value = True
        app.mini_mode = "execution"
        app.mini_update_after_id = None
        app._update_operation_mini = Mock()

        app._show_operation_mini("execution")

        app._update_operation_mini.assert_called_once_with()

    def test_restore_scan_foreground_restores_binding_when_not_foreground(self):
        # 截图后的前台恢复：主绑定窗口不在前台时激活它。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._bound_hwnd = Mock(return_value=100)
        with patch("macroflow.ui.app.is_window_process_foreground", return_value=False) as is_fore, \
             patch("macroflow.ui.app.activate_window") as activate:
            app._restore_workflow_scan_foreground()
        is_fore.assert_called_once_with(100)
        activate.assert_called_once_with(100)

    def test_restore_scan_foreground_skips_when_binding_foreground(self):
        # 主绑定窗口已在前台：零开销跳过。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._bound_hwnd = Mock(return_value=100)
        with patch("macroflow.ui.app.is_window_process_foreground", return_value=True) as is_fore, \
             patch("macroflow.ui.app.activate_window") as activate:
            app._restore_workflow_scan_foreground()
        is_fore.assert_called_once_with(100)
        activate.assert_not_called()


class ScriptRecordingSafetyTests(unittest.TestCase):
    def test_start_recording_blocks_when_script_is_dirty(self):
        # 录制会清空编辑器动作并分离当前脚本：未保存的修改必须拦截，
        # 否则被静默丢弃（与新建脚本的 dirty 拦截一致）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.worker = None
        app.dirty = True
        app._notify = Mock()
        app.start_recording()
        app._notify.assert_called_once()

    def test_recording_detaches_open_script_before_save(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(name="战斗脚本")
        app.script_path = Path("scripts/战斗脚本.json")
        app.script_requires_new_file = False
        app.script_name_var = Mock()
        app.script_name_var.get.return_value = "战斗脚本"
        app._log = Mock()

        app._detach_open_script_for_recording()

        self.assertIsNone(app.script_path)
        self.assertTrue(app.script_requires_new_file)
        app.script_name_var.set.assert_called_once_with("战斗脚本_新录制")


class StartupVisibilityTests(unittest.TestCase):
    def test_execution_mini_position_is_clamped_to_screen(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.root.winfo_screenwidth.return_value = 1000
        app.root.winfo_screenheight.return_value = 800
        app.execution_mini_position = [900, 700]
        self.assertEqual(app._execution_mini_position(420, 316), (580, 484))

    def test_spawn_new_instance_resets_pyinstaller_extraction_environment(self):
        inherited = {
            "_MEIPASS": "C:/old-mei",
            "_MEIPASS2": "C:/old-mei2",
            "PATH": "C:/Windows",
        }
        with patch.dict("macroflow.ui.app.os.environ", inherited, clear=True), \
             patch("macroflow.ui.app.subprocess.Popen") as popen, \
             patch("macroflow.ui.app.sys.frozen", True, create=True):
            spawn_new_instance(["MacroFlowStudio.exe", "--open-script", "x.json"])
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("_MEIPASS", env)
        self.assertNotIn("_MEIPASS2", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(env["PATH"], "C:/Windows")

    def test_startup_explicitly_shows_and_activates_main_window(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.root.winfo_id.return_value = 123
        app.exiting = False
        app.main_hidden_to_tray = False
        app.main_hidden_for_recording = False
        app.main_hidden_for_execution = False
        app.main_hidden_for_cursor_tracking = False
        with patch("macroflow.ui.app.show_window", return_value=True) as show, \
             patch("macroflow.ui.app.activate_window", return_value=True) as activate:
            app._ensure_startup_visible()
        app.root.deiconify.assert_called_once()
        app.root.state.assert_called_once_with("normal")
        show.assert_called_once_with(123)
        activate.assert_called_once_with(123)


class ScriptEditingTests(unittest.TestCase):
    def test_key_action_search_matches_key_and_state(self):
        self.assertTrue(key_action_matches(
            {"type": "key", "name": "A", "vk": 65, "down": True}, "a", "down",
        ))
        self.assertFalse(key_action_matches(
            {"type": "key", "name": "A", "vk": 65, "down": True}, "a", "up",
        ))
        self.assertTrue(key_action_matches(
            {"type": "key", "name": "A", "vk": 65, "down": False}, "65", "up",
        ))
        self.assertTrue(key_action_matches(
            {"type": "key_press", "name": "ENTER", "vk": 13}, "enter", "press",
        ))
        self.assertFalse(key_action_matches(
            {"type": "key_press", "name": "ENTER", "vk": 13}, "enter", "down",
        ))

    def test_set_matching_key_action_delays_changes_only_search_matches(self):
        actions = [
            {"type": "key", "name": "A", "vk": 65, "down": True, "delay_ms": 10},
            {"type": "key", "name": "A", "vk": 65, "down": False, "delay_ms": 20},
            {"type": "key_press", "name": "A", "vk": 65, "delay_ms": 30, "hold_ms": 300},
        ]

        changed = set_matching_key_action_delays(actions, "A", "down", 120)

        self.assertEqual(changed, [0])
        self.assertEqual([action["delay_ms"] for action in actions], [120, 20, 30])
        self.assertEqual(actions[2]["hold_ms"], 300)

    def test_module_reference_keeps_script_edit_button_enabled_for_delays(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{
            "type": "image_match", "module_ref": True,
            "template": "images/shared.png",
        }])
        app.edit_action_button = Mock()
        app._selected_action_index = Mock(return_value=0)

        app._update_action_edit_button()

        app.edit_action_button.configure.assert_called_once_with(state="normal")

    def test_add_jump_action_inserts_dialog_result(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.script = MacroScript(actions=[{"type": "delay", "ms": 1}])
        app._insert_action = Mock()
        result = {
            "type": "jump", "jump_action_id": SCRIPT_START_TARGET_ID,
            "jump_row": 1, "delay_ms": 0,
        }
        with patch("macroflow.ui.app.JumpActionDialog") as dialog_class:
            dialog_class.return_value.show.return_value = result
            app.add_jump()
        dialog_class.assert_called_once_with(app.root, actions=app.script.actions)
        app._insert_action.assert_called_once_with(result)

    def test_editing_action_preserves_stable_identity(self):
        original = {"type": "key", "action_id": "stable-target", "name": "A"}
        with patch("macroflow.ui.dialogs.KeyActionDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "key_press", "name": "B", "vk": 66,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-target")

    def test_editing_text_action_uses_text_dialog(self):
        original = {"type": "text", "action_id": "stable-text", "text": "旧文本", "delay_ms": 0}
        with patch("macroflow.ui.dialogs.TextActionDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "text", "text": "新文本", "char_delay_ms": 20, "delay_ms": 1000,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-text")
        self.assertEqual(updated["delay_ms"], 1000)
        self.assertEqual(updated["text"], "新文本")
        dialog_class.assert_called_once()

    def test_editing_repeat_click_action_uses_repeat_click_dialog(self):
        original = {"type": "repeat_click", "action_id": "stable-repeat", "x": 1, "y": 2}
        with patch("macroflow.ui.dialogs.RepeatClickDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "repeat_click", "x": 9, "y": 9,
                "count": 3, "interval_ms": 50,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-repeat")
        self.assertEqual(updated["count"], 3)
        dialog_class.assert_called_once()

    def test_editing_open_app_action_uses_open_app_dialog(self):
        original = {"type": "open_app", "action_id": "stable-app", "path": "C:/old/app.exe"}
        with patch("macroflow.ui.dialogs.OpenAppDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "open_app", "path": "C:/new/app.exe",
                "delay_ms": 300, "after_delay_ms": 800,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-app")
        self.assertEqual(updated["path"], "C:/new/app.exe")
        dialog_class.assert_called_once()

    def test_editing_close_app_action_uses_close_app_dialog(self):
        original = {"type": "close_app", "action_id": "stable-close", "name": "old.exe"}
        with patch("macroflow.ui.dialogs.CloseAppDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "close_app", "name": "new.exe",
                "graceful": False, "graceful_wait_ms": 1000,
                "delay_ms": 0, "after_delay_ms": 0,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-close")
        self.assertEqual(updated["name"], "new.exe")
        dialog_class.assert_called_once()

    def test_editing_jump_action_preserves_identity(self):
        original = {
            "type": "jump", "action_id": "stable-jump",
            "jump_action_id": SCRIPT_START_TARGET_ID,
        }
        with patch("macroflow.ui.dialogs.JumpActionDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "jump", "jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
                "jump_row": 3,
            }
            updated = edit_action(None, original, [original])
        self.assertEqual(updated["action_id"], "stable-jump")
        self.assertEqual(updated["jump_action_id"], NEXT_WORKFLOW_STEP_TARGET_ID)

    def test_run_script_from_selected_action_uses_first_selected_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("4", "2")
        app.run_current_script = Mock()
        app._notify = Mock()

        app.run_script_from_selected_action()

        app.run_current_script.assert_called_once_with(start_index=2)
        app._notify.assert_not_called()

    def test_ctrl_a_selects_all_script_actions(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.action_tree = Mock()
        app.action_tree.get_children.return_value = ("0", "1", "2")

        result = app._select_all_actions()

        self.assertEqual(result, "break")
        app.action_tree.selection_set.assert_called_once_with("0", "1", "2")
        app.action_tree.focus.assert_called_once_with("0")
        app.action_tree.see.assert_called_once_with("0")

    def test_run_script_worker_keeps_detecting_for_global_script(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._ui = Mock()
        app._finish_execution_visibility = Mock()
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.exiting = False
        app._evaluating_guards = False
        app.ocr_engine_ready = True
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._activate_global_detect_from_config = Mock()
        thread = threading.Thread(
            target=app._run_script_worker,
            args=([{"type": "delay", "delay_ms": 10}], 1, None, None, False, None, False, 0),
            kwargs={"trigger": {"template": "images/g.png", "hold_ms": 500}},
            daemon=True,
        )
        thread.start()
        time.sleep(0.2)
        # 播放已结束但保持"检测中"，未显示"执行完成"。
        app.player.play.assert_called_once()
        activation_call = app._activate_global_detect_from_config.call_args
        self.assertEqual(activation_call.args[0], {"template": "images/g.png", "hold_ms": 500})
        self.assertEqual(
            activation_call.kwargs["standalone_replay"]["actions"],
            [{"type": "delay", "delay_ms": 10}],
        )
        # 语句体参数已存档，供触发后回放。
        self.assertEqual(app.standalone_global_replay["actions"], [{"type": "delay", "delay_ms": 10}])
        texts = [
            str(call.args[1]) if len(call.args) > 1 else ""
            for call in app._ui.call_args_list
        ]
        self.assertTrue(any("全局检测已启用，持续检测中" in text for text in texts))
        self.assertFalse(any("脚本执行完成" in text for text in texts))
        self.assertTrue(thread.is_alive())
        app.player.stop_event.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(app.standalone_global_replay)

    def test_run_script_worker_finishes_normal_script(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._ui = Mock()
        app._sound = Mock()
        app._finish_execution_visibility = Mock()
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.ocr_engine_ready = True
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._run_script_worker(
            [{"type": "delay", "delay_ms": 10}], 1, None, None, False, None, False, 0,
        )
        app.player.play.assert_called_once()
        self.assertTrue(app._ui.called)
        texts = [
            str(call.args[1]) if len(call.args) > 1 else ""
            for call in app._ui.call_args_list
        ]
        self.assertTrue(any("脚本执行完成" in text for text in texts))

    def test_edit_global_trigger_stores_config_without_type(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript()
        app.root = Mock()
        app._mark_dirty = Mock()
        app._sync_global_script_marker = Mock()
        app._set_status = Mock()
        with patch("macroflow.ui.app.GlobalDetectDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "global_detect", "template": "images/g.png",
                "threshold": 0.85, "interval_ms": 500, "hold_ms": 1000,
                "click_point": None, "restart_delay_ms": 0,
            }
            app._edit_global_trigger()
        trigger = app.script.settings["trigger"]
        self.assertNotIn("type", trigger)
        self.assertEqual(trigger["template"], "images/g.png")
        dialog_class.assert_called_once_with(app.root, {}, require_click=False)
        app._mark_dirty.assert_called_once()
        app._sync_global_script_marker.assert_called_once()
        app._set_status.assert_called_once()

    def test_edit_global_trigger_cancel_keeps_previous_config(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript()
        app.script.settings["trigger"] = {"template": "images/old.png"}
        app.root = Mock()
        app._mark_dirty = Mock()
        with patch("macroflow.ui.app.GlobalDetectDialog") as dialog_class:
            dialog_class.return_value.show.return_value = None
            app._edit_global_trigger()
        self.assertEqual(app.script.settings["trigger"]["template"], "images/old.png")
        app._mark_dirty.assert_not_called()

    def test_clear_global_trigger_removes_config_and_marks_dirty(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript()
        app.script.settings["trigger"] = {"template": "images/g.png"}
        app._mark_dirty = Mock()
        app._sync_global_script_marker = Mock()
        app._clear_global_trigger()
        self.assertNotIn("trigger", app.script.settings)
        app._mark_dirty.assert_called_once()
        app._sync_global_script_marker.assert_called_once()

    def test_delete_selected_actions_updates_script_and_can_be_undone(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "comment", "text": "A"},
            {"type": "comment", "text": "B"},
            {"type": "comment", "text": "C"},
        ])
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("1",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._set_status = Mock()
        app._notify = Mock()
        app.delete_actions()
        self.assertEqual([action["text"] for action in app.script.actions], ["A", "C"])
        app._checkpoint_action_edit.assert_called_once()
        app._mark_dirty.assert_called_once()
        app.action_tree.selection_set.assert_called_once_with("1")
        app.action_tree.focus.assert_called_once_with("1")
        app.action_tree.see.assert_called_once_with("1")
        app._set_status.assert_called_once()

    def test_delete_last_action_selects_new_last_action(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "comment", "text": "A"},
            {"type": "comment", "text": "B"},
        ])
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("1",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._set_status = Mock()
        app._notify = Mock()

        app.delete_actions()

        self.assertEqual([action["text"] for action in app.script.actions], ["A"])
        app.action_tree.selection_set.assert_called_once_with("0")
        app.action_tree.focus.assert_called_once_with("0")
        app.action_tree.see.assert_called_once_with("0")

    def test_insert_script_below_selected_row_stores_reference(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "comment", "text": "A"},
            {"type": "comment", "text": "B"},
        ])
        app.root = Mock()
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._notify = Mock()
        inserted = MacroScript(actions=[
            {"type": "comment", "text": "C1"},
            {"type": "comment", "text": "C2"},
        ])
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("macroflow.ui.app.load_script", return_value=inserted):
            app._insert_script(False)
        self.assertEqual(len(app.script.actions), 3)
        ref = app.script.actions[1]
        self.assertEqual(ref["type"], "script_ref")
        self.assertEqual(ref["script"], str(Path("C:/scripts/C.json").resolve()))
        self.assertTrue(ref.get("action_id"))
        app._checkpoint_action_edit.assert_called_once()
        app._mark_dirty.assert_called_once()
        app.action_tree.selection_set.assert_called_once_with("1")

    def test_insert_script_into_empty_script_allowed(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[])
        app.root = Mock()
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ()
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._notify = Mock()
        inserted = MacroScript(actions=[{"type": "comment", "text": "C"}])
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("macroflow.ui.app.load_script", return_value=inserted):
            app._insert_script(False)
        self.assertEqual(len(app.script.actions), 1)
        self.assertEqual(app.script.actions[0]["type"], "script_ref")
        self.assertEqual(app.script.actions[0]["script"], str(Path("C:/scripts/C.json").resolve()))
        app._checkpoint_action_edit.assert_called_once()
        app.action_tree.selection_set.assert_called_once_with("0")
        app._notify.assert_called_once()

    def test_insert_script_requires_selection_when_actions_exist(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "comment", "text": "A"}])
        app.root = Mock()
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ()
        app._notify = Mock()
        with patch("macroflow.ui.app.filedialog.askopenfilename") as picker:
            app._insert_script(False)
        picker.assert_not_called()
        app._notify.assert_called_once()

    def test_insert_action_above_selected_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "comment", "text": "A"},
            {"type": "comment", "text": "B"},
        ])
        app.insert_position_var = Mock()
        app.insert_position_var.get.return_value = "above"
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("1",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._insert_action({"type": "comment", "text": "C"})
        self.assertEqual([action["text"] for action in app.script.actions], ["A", "C", "B"])
        app.action_tree.selection_set.assert_called_once_with("1")

    def test_insert_action_below_selected_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "comment", "text": "A"}])
        app.insert_position_var = Mock()
        app.insert_position_var.get.return_value = "below"
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._insert_action({"type": "comment", "text": "C"})
        self.assertEqual([action["text"] for action in app.script.actions], ["A", "C"])

    def test_insert_above_with_no_selection_goes_to_top(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "comment", "text": "A"}])
        app.insert_position_var = Mock()
        app.insert_position_var.get.return_value = "above"
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ()
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._insert_action({"type": "comment", "text": "C"})
        self.assertEqual([action["text"] for action in app.script.actions], ["C", "A"])

    def test_insert_script_above_selected_row_stores_reference(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "comment", "text": "A"},
            {"type": "comment", "text": "B"},
        ])
        app.root = Mock()
        app.insert_position_var = Mock()
        app.insert_position_var.get.return_value = "above"
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("1",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._notify = Mock()
        inserted = MacroScript(actions=[{"type": "comment", "text": "C"}])
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("macroflow.ui.app.load_script", return_value=inserted):
            app._insert_script(False)
        self.assertEqual(len(app.script.actions), 3)
        self.assertEqual(app.script.actions[1]["type"], "script_ref")
        self.assertEqual(app.script.actions[1]["script"], str(Path("C:/scripts/C.json").resolve()))

    def test_insert_script_expanded_copies_rows_and_remaps_jump_ids(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "comment", "text": "A"},
            {"type": "comment", "text": "B"},
        ])
        app.root = Mock()
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._notify = Mock()
        inserted = MacroScript(actions=[
            {"type": "comment", "text": "C1", "action_id": "src1"},
            {"type": "comment", "text": "C2", "action_id": "src2", "jump_action_id": "src1"},
            {"type": "image_match", "text": "img", "action_id": "src3",
             "timeout_jump_action_id": "src2", "found_jump_action_id": "src3"},
        ])
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("macroflow.ui.app.load_script", return_value=inserted):
            app._insert_script(True)
        self.assertEqual(len(app.script.actions), 5)
        inserted_actions = app.script.actions[1:4]
        self.assertEqual([action["text"] for action in inserted_actions], ["C1", "C2", "img"])
        # 行 ID 全部重建，不残留源脚本 ID
        new_ids = {action["action_id"] for action in inserted_actions}
        self.assertEqual(len(new_ids), 3)
        self.assertNotIn("src1", new_ids)
        self.assertNotIn("src2", new_ids)
        self.assertNotIn("src3", new_ids)
        # 跳转引用映射到新 ID：C2 跳 C1、img 超时跳 C2、img 找到后跳自己
        c1_id = inserted_actions[0]["action_id"]
        c2_id = inserted_actions[1]["action_id"]
        img = inserted_actions[2]
        self.assertEqual(img["jump_action_id"] if "jump_action_id" in img else None, None)
        self.assertEqual(inserted_actions[1]["jump_action_id"], c1_id)
        self.assertEqual(img["timeout_jump_action_id"], c2_id)
        self.assertEqual(img["found_jump_action_id"], img["action_id"])
        # 源脚本对象未被修改
        self.assertEqual(inserted.actions[0]["action_id"], "src1")
        # 插入的是逐行动作而非 script_ref
        self.assertNotEqual(app.script.actions[1]["type"], "script_ref")
        app._checkpoint_action_edit.assert_called_once()
        app._mark_dirty.assert_called_once()
        app._notify.assert_called_once()

    def test_insert_script_expanded_migrates_legacy_jump_row(self):
        # 旧版脚本的 jump_row（无 action_id）插入后必须迁移为指向插入块内
        # 对应行的 jump_action_id，否则跳转会带着源脚本相对行号错位。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "comment", "text": "主"}])
        app.root = Mock()
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0",)
        app._checkpoint_action_edit = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._notify = Mock()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({
                "name": "Ref",
                "actions": [
                    {"type": "comment", "text": "R1"},
                    {"type": "comment", "text": "R2"},
                    {"type": "jump", "jump_row": 2},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            with patch("macroflow.ui.app.filedialog.askopenfilename", return_value=str(ref)):
                app._insert_script(True)
        inserted = app.script.actions[1:4]
        self.assertEqual([action.get("text") for action in inserted], ["R1", "R2", None])
        jump = inserted[2]
        self.assertEqual(jump["jump_action_id"], inserted[1][ACTION_ID_KEY])

    def test_insert_script_above_requires_selection_when_actions_exist(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "comment", "text": "A"}])
        app.root = Mock()
        app.insert_position_var = Mock()
        app.insert_position_var.get.return_value = "above"
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ()
        app._notify = Mock()
        with patch("macroflow.ui.app.filedialog.askopenfilename") as picker:
            app._insert_script(True)
        picker.assert_not_called()
        app._notify.assert_called_once()

    def test_open_new_window_launches_second_instance(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app._set_status = Mock()
        app._notify = Mock()
        with patch("macroflow.ui.app.subprocess.Popen") as popen, \
             patch("macroflow.ui.app.sys.executable", "C:/Python313/python.exe"), \
             patch("macroflow.ui.app.sys.frozen", False, create=True), \
             patch("macroflow.ui.app.__file__", "E:/proj/app.py"):
            app.open_new_window()
        popen.assert_called_once()
        args = popen.call_args.args[0]
        self.assertEqual(
            args,
            ["C:/Python313/python.exe", str(Path("E:/proj/app.py")), "--new-script"],
        )
        self.assertEqual(popen.call_args.kwargs["cwd"], str(BASE_DIR))
        app._log.assert_called_once()

    def test_script_category_can_change_back_from_global(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 1}])
        app.script.settings["category"] = "global"
        app.script_category_var = Mock()
        app.script_category_var.get.return_value = "关卡"
        app.global_script_marker = Mock()
        app._mark_dirty = Mock()
        app._set_status = Mock()
        app._script_category_changed()
        self.assertEqual(app.script.settings["category"], "level")
        self.assertFalse(app.script.is_global)
        app.global_script_marker.configure.assert_called_with(text="")
        app.script_category_var.set.assert_not_called()

    def test_script_category_not_locked_by_module_row(self):
        # v1.68：普通脚本内嵌全局模块行（global_detect + jump_row）不再强制类别为全局。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "global_detect", "template": "x.png", "jump_row": 3},
        ])
        app.script.settings["category"] = "level"
        app.script_category_var = Mock()
        app.script_category_var.get.return_value = "关卡"
        app.global_script_marker = Mock()
        app._mark_dirty = Mock()
        app._set_status = Mock()
        app._script_category_changed()
        self.assertEqual(app.script.settings["category"], "level")
        self.assertFalse(app.script.is_global)
        app.script_category_var.set.assert_not_called()
        app.global_script_marker.configure.assert_called_with(text="")

    def test_add_global_detect_inserts_module_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 1}])
        app.root = Mock()
        app._insert_action = Mock()
        app._notify = Mock()
        with patch("macroflow.ui.app.GlobalDetectDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "global_detect", "template": "images/g.png",
                "jump_row": 3, "jump_action_id": "target-a",
                "click_point": None, "restart_delay_ms": 0,
            }
            app.add_global_detect()
        # v1.70：跳转目标从脚本行列表中选择，对话框拿到全部动作。
        dialog_class.assert_called_once_with(
            app.root, jump=True, actions=app.script.actions,
        )
        app._insert_action.assert_called_once()
        action = app._insert_action.call_args.args[0]
        self.assertEqual(action["type"], "global_detect")
        self.assertEqual(action["jump_row"], 3)
        self.assertEqual(action["jump_action_id"], "target-a")
        app._notify.assert_not_called()

    def test_add_global_detect_refuses_for_global_script(self):
        # 全局脚本的触发条件在"触发条件"区块配置，不能添加模块行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[], settings={"trigger": {"template": "g.png"}})
        app._insert_action = Mock()
        app._notify = Mock()
        app.add_global_detect()
        app._notify.assert_called_once()
        app._insert_action.assert_not_called()

    def test_add_module_inserts_switch_module_ref(self):
        # 切换模块引用：直接插入，无需补跳转行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 1}])
        app.root = Mock()
        app._insert_action = Mock(return_value=0)
        app._default_global_jump = Mock()
        app._notify = Mock()
        action = {
            "type": "image_match", "template": "images/s.png",
            "module_ref": True, "module_category": "switch",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = action
            app.add_module()
        picker_class.assert_called_once_with(app.root, actions=app.script.actions)
        app._insert_action.assert_called_once_with(action)
        app._default_global_jump.assert_not_called()
        app._notify.assert_not_called()

    def test_add_number_module_configures_comparison_before_inserting(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 1, "action_id": "target"}])
        app.root = Mock()
        app._insert_action = Mock(return_value=1)
        app._default_global_jump = Mock()
        app._notify = Mock()
        raw_action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:number", "template": "",
            "module_category": "switch", "region_mode": "template",
        }
        configured = dict(
            raw_action, expected_number=7, on_found="jump",
            found_jump_action_id="target", on_timeout="jump",
            timeout_jump_action_id="target",
        )
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class, \
             patch("macroflow.ui.app.registered_module_object", return_value={"recognize": "number"}), \
             patch("macroflow.ui.app.edit_action", return_value=configured) as edit:
            picker_class.return_value.show.return_value = raw_action
            app.add_module()
        edit.assert_called_once_with(app.root, raw_action, all_actions=app.script.actions)
        app._insert_action.assert_called_once_with(configured)

    def test_add_module_global_middle_insert_sets_default_jump(self):
        # 全局模块引用中间插入：默认跳到下一行（jump_row=insert_at+2，1 基行号）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.script = MacroScript(actions=[
            {"type": "delay", "ms": 1, "action_id": "a1"},
            {"type": "delay", "ms": 2, "action_id": "a2"},
            {"type": "delay", "ms": 3, "action_id": "a3"},
        ])
        app._selected_action_index = Mock(return_value=0)
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app.action_tree = Mock()
        action = {
            "type": "global_detect", "template": "images/g.png",
            "module_ref": True, "module_category": "special",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = action
            app.add_module()
        inserted = app.script.actions[1]
        self.assertEqual(inserted["jump_row"], 3)
        self.assertEqual(inserted["jump_action_id"], "a2")
        self.assertEqual(inserted["template"], "images/g.png")

    def test_add_module_global_end_insert_uses_out_of_range_jump(self):
        # 全局模块引用末尾插入：跳转行号用越界值 len+1，触发后段/动作播完脚本结束。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.script = MacroScript(actions=[
            {"type": "delay", "ms": 1, "action_id": "a1"},
            {"type": "delay", "ms": 2, "action_id": "a2"},
        ])
        app._selected_action_index = Mock(return_value=1)
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app.action_tree = Mock()
        action = {
            "type": "global_detect", "template": "images/g.png",
            "module_ref": True, "module_category": "special",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = action
            app.add_module()
        inserted = app.script.actions[2]
        self.assertEqual(inserted["jump_row"], 4)
        self.assertNotIn("jump_action_id", inserted)

    def test_add_module_refuses_global_module_in_global_script(self):
        # 全局脚本不能插入全局模块引用（触发条件在区块配置）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.script = MacroScript(actions=[], settings={"trigger": {"template": "g.png"}})
        app._insert_action = Mock()
        app._notify = Mock()
        action = {
            "type": "global_detect", "template": "images/g.png",
            "module_ref": True, "module_category": "global",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = action
            app.add_module()
        app._notify.assert_called_once()
        app._insert_action.assert_not_called()

    def test_undo_restores_actions_before_last_edit(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 10}])
        app.action_undo_stack = []
        app.action_redo_stack = []
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("1",)
        app.undo_button = Mock()
        app.redo_button = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._set_status = Mock()

        app._checkpoint_action_edit()
        app.script.actions.append({"type": "delay", "ms": 20})
        app._undo_redo_action_edit(False)

        self.assertEqual(app.script.actions, [{"type": "delay", "ms": 10}])
        self.assertEqual(app.action_undo_stack, [])
        # 撤销后当前状态（含刚追加的动作）进重做栈，可恢复。
        self.assertEqual(
            app.action_redo_stack,
            [[{"type": "delay", "ms": 10}, {"type": "delay", "ms": 20}]],
        )
        app.undo_button.configure.assert_called_with(state="disabled")
        app.redo_button.configure.assert_called_with(state="normal")

    def test_redo_restores_actions_after_undo(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 10}])
        app.action_undo_stack = []
        app.action_redo_stack = [[{"type": "delay", "ms": 20}]]
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0",)
        app.undo_button = Mock()
        app.redo_button = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._set_status = Mock()

        app._undo_redo_action_edit(True)

        self.assertEqual(app.script.actions, [{"type": "delay", "ms": 20}])
        self.assertEqual(app.action_redo_stack, [])
        # 重做把当前状态压回撤销栈，可再撤销。
        self.assertEqual(app.action_undo_stack, [[{"type": "delay", "ms": 10}]])
        app.undo_button.configure.assert_called_with(state="normal")
        app.redo_button.configure.assert_called_with(state="disabled")

    def test_redo_empty_stack_does_nothing(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 10}])
        app.action_redo_stack = []
        app._mark_dirty = Mock()
        app.redo_button = Mock()

        app._undo_redo_action_edit(True)

        self.assertEqual(app.script.actions, [{"type": "delay", "ms": 10}])
        app._mark_dirty.assert_not_called()
        app.redo_button.configure.assert_called_with(state="disabled")

    def test_new_edit_clears_redo_stack(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 10}])
        app.action_undo_stack = [[{"type": "delay", "ms": 5}]]
        app.action_redo_stack = [[{"type": "delay", "ms": 10}]]
        app.undo_button = Mock()
        app.redo_button = Mock()

        app.script.actions.append({"type": "delay", "ms": 30})
        app._checkpoint_action_edit()

        # 撤销后改动作：重做栈作废。
        self.assertEqual(app.action_redo_stack, [])
        self.assertEqual(len(app.action_undo_stack), 2)
        app.redo_button.configure.assert_called_with(state="disabled")

    def test_successful_save_clears_and_disables_undo(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(name="测试", actions=[{"type": "delay", "ms": 10}])
        app.script_name_var = Mock()
        app.script_name_var.get.return_value = "测试"
        app.script_path = Path("测试.json")
        app.script_requires_new_file = False
        app.action_undo_stack = [[{"type": "delay", "ms": 5}]]
        app.undo_button = Mock()
        app._current_script_settings = Mock(return_value={})
        app._refresh_coordinate_scale_status = Mock()
        app.refresh_script_files = Mock()
        app._set_status = Mock()
        app._log = Mock()
        app.script_category_var = Mock()
        app.script_category_var.get.return_value = "关卡"
        app.workflow_tree = None
        for name in ("_level_scripts_dir", "_level_pack_scripts_dir",
                     "_switch_scripts_dir", "_global_scripts_dir"):
            setattr(app, name, lambda: Path("."))

        with patch("macroflow.ui.app.save_script", return_value=Path("测试.json")):
            app.save_current_script()

        self.assertEqual(app.action_undo_stack, [])
        app.undo_button.configure.assert_called_with(state="disabled")

    def _save_app(self, folder: Path, category: str) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._notify = Mock()
        app._log = Mock()
        app._set_status = Mock()
        app._clear_action_undo = Mock()
        app.refresh_script_files = Mock()
        app.script = MacroScript(name="A", actions=[])
        app.script_name_var = Mock()
        app.script_name_var.get.return_value = "A"
        app.script_category_var = Mock()
        app.script_category_var.get.return_value = category
        app.script_requires_new_file = False
        app.dirty = False
        app.action_undo_stack = []
        app.workflow_tree = None
        app._current_script_settings = Mock(return_value={})
        app._refresh_coordinate_scale_status = Mock()
        level_dir = folder / "level"
        level_pack_dir = folder / "level_pack"
        level_dir.mkdir(exist_ok=True)
        level_pack_dir.mkdir(exist_ok=True)
        app._level_scripts_dir = lambda: level_dir
        app._level_pack_scripts_dir = lambda: level_pack_dir
        app._switch_scripts_dir = lambda: level_dir
        return app

    def test_save_after_category_change_moves_file_to_new_category_dir(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            app = self._save_app(Path(folder), "关卡封装")
            level_dir = Path(folder) / "level"
            level_pack_dir = Path(folder) / "level_pack"
            original = level_dir / "A.json"
            original.write_text("{}", encoding="utf-8")
            app.script_path = original
            with patch("macroflow.ui.app.save_script", return_value=level_pack_dir / "A.json") as save:
                result = app.save_current_script()
        self.assertEqual(result, level_pack_dir / "A.json")
        self.assertFalse(original.exists())
        app._set_status.assert_called_once_with(
            "已保存并移动到 level_pack/A.json", "success")

    def test_save_same_category_keeps_file_in_place(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            app = self._save_app(Path(folder), "关卡")
            level_dir = Path(folder) / "level"
            original = level_dir / "A.json"
            original.write_text("{}", encoding="utf-8")
            app.script_path = original
            with patch("macroflow.ui.app.save_script", return_value=original):
                app.save_current_script()
            self.assertTrue(original.exists())
            app._set_status.assert_called_once_with("已保存 A.json", "success")

    def test_save_after_category_change_with_name_collision_uses_new_name(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            app = self._save_app(Path(folder), "关卡封装")
            level_dir = Path(folder) / "level"
            level_pack_dir = Path(folder) / "level_pack"
            original = level_dir / "A.json"
            original.write_text("{}", encoding="utf-8")
            conflict = level_pack_dir / "A.json"
            conflict.write_text("另一个脚本", encoding="utf-8")
            app.script_path = original
            with patch("macroflow.ui.app.save_script", return_value=level_pack_dir / "A (2).json"):
                result = app.save_current_script()
            self.assertEqual(result, level_pack_dir / "A (2).json")
            self.assertFalse(original.exists())
            self.assertTrue(conflict.exists())

    def test_save_move_failure_keeps_original_file(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            app = self._save_app(Path(folder), "关卡封装")
            level_dir = Path(folder) / "level"
            original = level_dir / "A.json"
            original.write_text("{}", encoding="utf-8")
            app.script_path = original
            with patch("macroflow.ui.app.save_script", side_effect=RuntimeError("磁盘已满")):
                result = app.save_current_script()
            self.assertIsNone(result)
            self.assertTrue(original.exists())
            app._notify.assert_called_once_with("保存失败", "磁盘已满")

    def test_save_after_rename_removes_old_file(self):
        # 改名保存会留下孤儿旧文件（工作流/引用仍指向陈旧内容）——修复：
        # 与类别变化分支一致，保存成功后删除旧文件。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            app = self._save_app(Path(folder), "关卡")
            level_dir = Path(folder) / "level"
            old = level_dir / "A.json"
            old.write_text("{}", encoding="utf-8")
            app.script_path = old
            app.script_name_var.get.return_value = "B"
            app.dirty = False
            with patch("macroflow.ui.app.save_script",
                       side_effect=lambda _script, path: path) as save:
                result = app.save_current_script()
            self.assertEqual(result, level_dir / "B.json")
            self.assertFalse(old.exists())
            self.assertTrue(save.called)

    def test_save_after_direction_rename_updates_hotkey_bindings(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            app = self._save_app(Path(folder), "方向")
            direction_dir = Path(folder) / "direction"
            direction_dir.mkdir()
            app._direction_scripts_dir = lambda: direction_dir
            old = direction_dir / "A.json"
            old.write_text("{}", encoding="utf-8")
            app.script_path = old
            app.script_name_var.get.return_value = "B"
            app.hotkey_scripts = [{
                "key": "J", "vk": 74, "script": display_path(old),
            }]
            app._apply_hotkey_bindings = Mock()
            app._refresh_hotkey_summary = Mock()
            app._persist_sidebar_settings = Mock(return_value=True)

            with patch("macroflow.ui.app.save_script", side_effect=lambda _script, path: path):
                result = app.save_current_script()

            new = direction_dir / "B.json"
            self.assertEqual(result, new)
            self.assertEqual(app.hotkey_scripts[0]["script"], display_path(new))
            app._apply_hotkey_bindings.assert_called_once_with()
            app._refresh_hotkey_summary.assert_called_once_with()
            app._persist_sidebar_settings.assert_called_once_with()

    def test_save_current_script_blocked_during_recording(self):
        # 录制中的动作在 recorder.actions 里，编辑器列表为空：保存会写出
        # 永远为空内容的“已保存”文件——必须拦截。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.recorder = Mock()
        app.recorder.running = True
        app._notify = Mock()
        self.assertIsNone(app.save_current_script())
        app._notify.assert_called_once()

    def test_copy_contiguous_actions_inserts_after_selection(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "key", "vk": 65, "meta": {"name": "A"}},
            {"type": "delay", "ms": 100},
            {"type": "key", "vk": 66},
        ])
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0", "1")
        app.root = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()
        app._set_status = Mock()

        app.copy_selected_actions_down()

        self.assertEqual([action["type"] for action in app.script.actions], [
            "key", "delay", "key", "delay", "key",
        ])
        self.assertEqual(app.script.actions[2]["vk"], 65)
        self.assertIsNot(app.script.actions[2], app.script.actions[0])
        self.assertIsNot(app.script.actions[2]["meta"], app.script.actions[0]["meta"])
        app.action_tree.selection_set.assert_called_once_with("2", "3")

    def test_copy_rejects_non_contiguous_selection(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "delay", "ms": 1},
            {"type": "delay", "ms": 2},
            {"type": "delay", "ms": 3},
        ])
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ("0", "2")
        app.root = Mock()
        app._notify = Mock()
        app._mark_dirty = Mock()
        app.rebuild_action_tree = Mock()

        app.copy_selected_actions_down()

        self.assertEqual(len(app.script.actions), 3)
        app._mark_dirty.assert_not_called()
        app._notify.assert_called_once()


class WorkflowInsertTests(unittest.TestCase):
    def _app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._notify = Mock()
        app._script_category_dir = Mock()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._update_workflow_selection_color = Mock()
        app.workflow_tree = Mock()
        app.root = Mock()
        return app

    def test_insert_above_places_step_before_selected(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"kind": "global_module", "script": "g.json", "step_id": "g"},
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "above"
        app.workflow_tree.selection.return_value = ("0",)  # 选中 a.json（全局模块在单独列表）
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/scripts/新脚本.json"):
            app.insert_workflow_step()
        # 全局模块保持首位，新步骤插在 a.json 之前
        self.assertEqual(
            [Path(s["script"]).name for s in app.workflow.steps],
            ["g.json", "新脚本.json", "a.json", "b.json"],
        )
        app.workflow_tree.selection_set.assert_called_with("0")
        app._persist_workflow_draft.assert_called_once()

    def test_insert_below_places_step_after_selected(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("0",)
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/x/new.json"):
            app.insert_workflow_step()
        self.assertEqual(
            [Path(s["script"]).name for s in app.workflow.steps],
            ["a.json", "new.json", "b.json"],
        )
        app.workflow_tree.selection_set.assert_called_with("1")

    def test_insert_below_last_row_appends_to_end(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("1",)
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/x/new.json"):
            app.insert_workflow_step()
        self.assertEqual(
            [Path(s["script"]).name for s in app.workflow.steps],
            ["a.json", "b.json", "new.json"],
        )
        app.workflow_tree.selection_set.assert_called_with("2")

    def test_insert_requires_selected_row(self):
        app = self._app()
        app.workflow = Workflow(steps=[{"script": "a.json", "step_id": "a"}])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "above"
        app.workflow_tree.selection.return_value = ()
        app.insert_workflow_step()
        app._notify.assert_called_once_with("插入脚本", "请先选择插入位置所在的工作流行。")
        self.assertEqual(len(app.workflow.steps), 1)

    def test_insert_cancel_keeps_steps_unchanged(self):
        app = self._app()
        app.workflow = Workflow(steps=[{"script": "a.json", "step_id": "a"}])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("0",)
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value=""):
            app.insert_workflow_step()
        self.assertEqual(len(app.workflow.steps), 1)
        app._persist_workflow_draft.assert_not_called()

    def test_insert_module_below_selected_workflow_row(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("0",)
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "images/switch.png", "template": "images/switch.png",
        }
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = action
            app.insert_workflow_module_step()

        self.assertEqual([step.get("kind", "script") for step in app.workflow.steps], [
            "script", "module", "script",
        ])
        self.assertEqual(app.workflow.steps[1]["action"]["module_key"], "images/switch.png")
        self.assertEqual(app.workflow.steps[1]["action"]["module_name"], "switch")
        app.workflow_tree.selection_set.assert_called_with("1")
        picker_class.assert_called_once_with(
            app.root, categories=("switch", "special"), allow_number=False,
        )

    def test_add_multiple_modules_as_workflow_rows(self):
        app = self._app()
        app.workflow = Workflow()
        app._set_status = Mock()
        app.workflow_tree.selection.return_value = ()  # 未选中行：追加到末尾
        actions = [
            {"type": "image_match", "module_ref": True, "module_key": "module:a"},
            {"type": "image_match", "module_ref": True, "module_key": "module:b"},
        ]
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = actions
            app.add_workflow_module_step()

        self.assertEqual([step["kind"] for step in app.workflow.steps], ["module", "module"])
        self.assertEqual([step["action"]["module_key"] for step in app.workflow.steps], [
            "module:a", "module:b",
        ])
        picker_class.assert_called_once_with(
            app.root, categories=("switch", "special"), multi_select=True,
            allow_number=False,
        )
        app._persist_workflow_draft.assert_called_once()

    def test_add_script_step_inserts_below_selected(self):
        # “选择已有脚本”也跟随插入位置选项：选中行下方插入，不再总是追加到末尾。
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("0",)
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/x/new.json"):
            app.add_script_step()
        self.assertEqual(
            [Path(s["script"]).name for s in app.workflow.steps],
            ["a.json", "new.json", "b.json"],
        )
        app.workflow_tree.selection_set.assert_called_with("1")

    def test_add_script_step_inserts_above_selected(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "above"
        app.workflow_tree.selection.return_value = ("1",)
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/x/new.json"):
            app.add_script_step()
        self.assertEqual(
            [Path(s["script"]).name for s in app.workflow.steps],
            ["a.json", "new.json", "b.json"],
        )
        app.workflow_tree.selection_set.assert_called_with("1")

    def test_add_script_step_appends_without_selection(self):
        # 未选中行：保持“添加”语义，追加到末尾。
        app = self._app()
        app.workflow = Workflow(steps=[{"script": "a.json", "step_id": "a"}])
        app.workflow_insert_position_var = Mock()
        app.workflow_tree.selection.return_value = ()
        with patch("macroflow.ui.app.filedialog.askopenfilename", return_value="C:/x/new.json"):
            app.add_script_step()
        self.assertEqual(
            [Path(s["script"]).name for s in app.workflow.steps],
            ["a.json", "new.json"],
        )

    def test_add_workflow_module_step_inserts_below_selected(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app._set_status = Mock()
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("0",)
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "images/switch.png", "template": "images/switch.png",
        }
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = action
            app.add_workflow_module_step()

        self.assertEqual([step.get("kind", "script") for step in app.workflow.steps], [
            "script", "module", "script",
        ])
        self.assertEqual(app.workflow.steps[1]["action"]["module_key"], "images/switch.png")
        app.workflow_tree.selection_set.assert_called_with("1")

    def test_add_workflow_module_multiple_inserts_in_order(self):
        # 多选模块按顺序插入到选中行下方。
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app._set_status = Mock()
        app.workflow_insert_position_var = Mock()
        app.workflow_insert_position_var.get.return_value = "below"
        app.workflow_tree.selection.return_value = ("0",)
        actions = [
            {"type": "image_match", "module_ref": True, "module_key": "module:a"},
            {"type": "image_match", "module_ref": True, "module_key": "module:b"},
        ]
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = actions
            app.add_workflow_module_step()
        self.assertEqual(
            [step.get("action", {}).get("module_key") for step in app.workflow.steps],
            [None, "module:a", "module:b", None],
        )
        app.workflow_tree.selection_set.assert_called_with("2")

    def test_set_workflow_insert_position_toggles_buttons(self):
        app = self._app()

        class FakeVar:
            def __init__(self):
                self.value = "below"

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        app.workflow_insert_position_var = FakeVar()
        app.workflow_insert_above_button = Mock()
        app.workflow_insert_below_button = Mock()
        app._set_workflow_insert_position(True)
        self.assertEqual(app.workflow_insert_position_var.get(), "above")
        app.workflow_insert_above_button.configure.assert_called_with(bootstyle="primary")
        app.workflow_insert_below_button.configure.assert_called_with(bootstyle="secondary")

    def test_add_workflow_global_module_selects_module_object_directly(self):
        app = self._app()
        app.workflow = Workflow()
        app.global_tree = Mock()
        app._set_status = Mock()
        actions = [{
            "type": "global_detect", "template": "images/global.png",
            "module_ref": True, "module_category": "workflow_global",
        }, {
            "type": "global_detect", "template": "images/global2.png",
            "module_ref": True, "module_category": "workflow_global",
        }]
        app._append_global_module = Mock()
        with patch("macroflow.ui.app.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = actions
            app.add_workflow_global_module()
        picker_class.assert_called_once_with(
            app.root, categories=("workflow_global",), multi_select=True,
        )
        self.assertEqual(app._append_global_module.call_count, 2)
        app._append_global_module.assert_any_call(config=actions[0], refresh=False)
        app._append_global_module.assert_any_call(config=actions[1], refresh=False)
        app.rebuild_workflow_tree.assert_called_once()
        app._persist_workflow_draft.assert_called_once()


class WorkflowDisplayTests(unittest.TestCase):
    def test_workflow_tab_uses_resizable_split_and_grouped_toolbars(self):
        source = inspect.getsource(MacroFlowApp._build_workflow_tab)
        self.assertIn("ttk.Panedwindow", source)
        self.assertIn('text="添加 / 插入"', source)
        self.assertIn('text="编辑 / 排序"', source)
        self.assertIn("height=6", source)
        self.assertIn("height=10", source)

    def test_restart_resolved_row_prefers_action_then_workflow_default(self):
        # 「重新执行工作流」跳转行解析：动作级 → 工作流统一默认 → 第 1 行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(restart_default_row=4)
        self.assertEqual(
            app._restart_workflow_resolved_row({"restart_workflow_target_row": 3}),
            3,
        )
        self.assertEqual(
            app._restart_workflow_resolved_row({"type": "restart_workflow"}),
            4,
        )
        app.workflow.restart_default_row = 0
        self.assertEqual(
            app._restart_workflow_resolved_row({"type": "restart_workflow"}),
            1,
        )
        # 非法值一律视为未设置；未挂工作流对象时按第 1 行处理。
        app.workflow.restart_default_row = 4
        self.assertEqual(
            app._restart_workflow_resolved_row(
                {"restart_workflow_target_row": "oops"},
            ),
            4,
        )
        del app.workflow
        self.assertEqual(
            app._restart_workflow_resolved_row({"type": "restart_workflow"}),
            1,
        )

    def test_workflow_model_has_no_unified_restart_target(self):
        # 旧工作流文件里的 restart_target_step_id 不再进入模型（统一跳转已删除）。
        workflow = Workflow.from_dict({
            "name": "测试",
            "steps": [{"script": "a.json", "step_id": "row-a"}],
            "restart_target_step_id": "row-a",
        })
        self.assertFalse(hasattr(workflow, "restart_target_step_id"))
        self.assertNotIn("restart_target_step_id", workflow.to_dict())

    def test_workflow_restart_default_row_round_trips(self):
        # 默认跳转行是工作流文件字段（工作流页面统一设置），随文件保存。
        workflow = Workflow.from_dict({
            "name": "测试", "restart_default_row": "3",
            "steps": [{"script": "a.json", "step_id": "row-a"}],
        })
        self.assertEqual(workflow.restart_default_row, 3)
        self.assertEqual(workflow.to_dict()["restart_default_row"], 3)
        self.assertEqual(Workflow.from_dict(workflow.to_dict()).restart_default_row, 3)
        # 非法值按未设置处理。
        self.assertEqual(
            Workflow.from_dict({"restart_default_row": "oops"}).restart_default_row, 0,
        )

    def test_workflow_module_name_reads_existing_nested_action_reference(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {
            "kind": "module",
            "action": {
                "module_key": "images/部分/资讯叉叉.png",
                "template": "images/部分/资讯叉叉.png",
            },
        }
        with patch("macroflow.ui.app.registered_module_object", return_value={"name": "资讯叉叉"}):
            self.assertEqual(app._workflow_step_name(step), "模块 资讯叉叉")

    def test_workflow_module_name_reads_persisted_action_name(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {
            "kind": "module", "action": {
                "module_key": "module:claim", "module_name": "可领取",
            },
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=None):
            self.assertEqual(app._workflow_module_key(step), "module:claim")
            self.assertEqual(app._workflow_step_name(step), "模块 可领取")

    def test_successful_workflow_repeat_decrements_to_zero_without_disabling(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "a.json", "repeats": 1, "enabled": True,
        }])
        app.workflow_path = Path("flow.json")
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()

        with patch("macroflow.ui.app.save_workflow", return_value=Path("flow.json")) as save:
            app._consume_workflow_repeat(0)

        self.assertEqual(app.workflow.steps[0]["repeats"], 0)
        self.assertTrue(app.workflow.steps[0]["enabled"])
        save.assert_called_once_with(app.workflow, Path("flow.json"))
        app._persist_workflow_draft.assert_called_once()

    def test_batch_settings_apply_all_three_values_to_every_step(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"script": "a", "repeats": 1, "before_ms": 0, "repeat_interval_ms": 1000},
            {"script": "b", "repeats": 9, "before_ms": 300, "repeat_interval_ms": 500},
        ])
        app.root = Mock()
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ("1",)
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._set_status = Mock()
        dialog = Mock()
        dialog.show.return_value = {
            "repeats": 4, "before_ms": 1200, "repeat_interval_ms": 2300,
        }

        with patch("macroflow.ui.app.WorkflowBatchSettingsDialog", return_value=dialog):
            app.set_all_workflow_step_options()

        for step in app.workflow.steps:
            self.assertEqual(step["repeats"], 4)
            self.assertEqual(step["before_ms"], 1200)
            self.assertEqual(step["repeat_interval_ms"], 2300)
        app.workflow_tree.selection_set.assert_called_once_with("1")
        app._persist_workflow_draft.assert_called_once()

    def test_batch_settings_only_change_selected_parameter(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"script": "a", "repeats": 2, "before_ms": 100, "repeat_interval_ms": 500},
            {"script": "b", "repeats": 7, "before_ms": 900, "repeat_interval_ms": 800},
        ])
        app.root = Mock()
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._set_status = Mock()
        dialog = Mock()
        dialog.show.return_value = {"repeat_interval_ms": 3000}

        with patch("macroflow.ui.app.WorkflowBatchSettingsDialog", return_value=dialog):
            app.set_all_workflow_step_options()

        self.assertEqual(app.workflow.steps[0]["repeats"], 2)
        self.assertEqual(app.workflow.steps[1]["repeats"], 7)
        self.assertEqual(app.workflow.steps[0]["before_ms"], 100)
        self.assertEqual(app.workflow.steps[1]["before_ms"], 900)
        self.assertEqual([step["repeat_interval_ms"] for step in app.workflow.steps], [3000, 3000])

    def test_single_click_release_does_not_edit_workflow_cell(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_drag_index = 0
        app.workflow_was_dragged = False
        app._persist_workflow_draft = Mock()
        app._edit_workflow_cell = Mock()

        app._workflow_drag_end(Mock())

        app._edit_workflow_cell.assert_not_called()
        app._persist_workflow_draft.assert_called_once()

    def test_editing_interval_cell_only_changes_interval(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "scripts/a.json", "repeats": 3,
            "before_ms": 250, "repeat_interval_ms": 1000,
        }])
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = "0"
        app.workflow_tree.identify_column.return_value = "#5"
        app.root = Mock()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()

        with patch("macroflow.ui.app.DurationDialog") as prompt:
            prompt.return_value.show.return_value = 2400
            app._edit_workflow_cell(Mock(x=500, y=10))

        self.assertEqual(app.workflow.steps[0]["repeat_interval_ms"], 2400)
        self.assertEqual(app.workflow.steps[0]["repeats"], 3)
        self.assertEqual(app.workflow.steps[0]["before_ms"], 250)
        prompt.assert_called_once()

    def test_new_workflow_step_uses_defaults_without_prompts(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        with patch("macroflow.ui.app.display_path", return_value="scripts/a.json"), \
             patch("macroflow.ui.app.simpledialog.askinteger") as prompt:
            app._append_workflow_step(Path("a.json"))
        step = app.workflow.steps[0]
        self.assertEqual(step["script"], "scripts/a.json")
        self.assertEqual(step["repeats"], 1)
        self.assertEqual(step["before_ms"], 0)
        self.assertEqual(step["repeat_interval_ms"], DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS)
        self.assertFalse(step["unlimited"])
        self.assertTrue(step["enabled"])
        self.assertTrue(step.get("step_id"))
        prompt.assert_not_called()

    def test_workflow_only_shows_script_name(self):
        self.assertEqual(workflow_script_name("scripts/副本循环.json"), "副本循环")
        self.assertEqual(workflow_script_name(r"scripts\每日任务.json"), "每日任务")

    def test_workflow_progress_shows_script_and_repeat_positions(self):
        self.assertEqual(
            workflow_execution_progress(2, 29, "每日任务", 4, 3),
            "工作流 2/29 · 每日任务\n共执行 4 次 · 当前第 3/4 次 · F12 停止",
        )

    def test_drag_reorders_actual_workflow_steps(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a"}, {"script": "b"}, {"script": "c"}])
        app.workflow_drag_index = 0
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = "2"
        app.rebuild_workflow_tree = Mock()

        app._workflow_drag_motion(Mock(y=100))

        self.assertEqual([step["script"] for step in app.workflow.steps], ["b", "c", "a"])
        self.assertEqual(app.workflow_drag_index, 2)

    def test_missing_script_row_is_marked(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "scripts/definitely-missing.json"}])
        app.workflow_tree = Mock()
        app.workflow_tree.get_children.return_value = ()
        app.empty_workflow_hint = Mock()

        app.rebuild_workflow_tree()

        values = app.workflow_tree.insert.call_args.kwargs["values"]
        tags = app.workflow_tree.insert.call_args.kwargs["tags"]
        self.assertIn("文件不存在", values[1])
        self.assertEqual(tags, ("missing",))

    def test_disabled_workflow_row_is_dimmed(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "scripts/disabled.json", "enabled": False}])
        app.workflow_tree = Mock()
        app.workflow_tree.get_children.return_value = ()
        app.empty_workflow_hint = Mock()

        app.rebuild_workflow_tree()

        values = app.workflow_tree.insert.call_args.kwargs["values"]
        tags = app.workflow_tree.insert.call_args.kwargs["tags"]
        self.assertEqual(values[-1], "● 已禁用")
        self.assertEqual(tags, ("disabled",))

    def test_zero_repeat_workflow_row_is_marked_exhausted(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "scripts/exhausted.json", "repeats": 0, "enabled": True,
        }])
        app.workflow_tree = Mock()
        app.workflow_tree.get_children.return_value = ()
        app.empty_workflow_hint = Mock()

        app.rebuild_workflow_tree()

        values = app.workflow_tree.insert.call_args.kwargs["values"]
        tags = app.workflow_tree.insert.call_args.kwargs["tags"]
        self.assertEqual(values[-1], "○ 次数用完")
        self.assertEqual(tags, ("exhausted",))

    def test_unlimited_workflow_row_shows_infinite_and_not_exhausted(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "scripts/unlimited.json", "repeats": 0,
            "unlimited": True, "enabled": True,
        }])
        app.workflow_tree = Mock()
        app.workflow_tree.get_children.return_value = ()
        app.empty_workflow_hint = Mock()

        app.rebuild_workflow_tree()

        values = app.workflow_tree.insert.call_args.kwargs["values"]
        tags = app.workflow_tree.insert.call_args.kwargs["tags"]
        self.assertEqual(values[2], "∞")
        self.assertEqual(values[-1], "✓ 不计次数")
        self.assertEqual(tags, ("unlimited",))

    def test_unlimited_workflow_repeat_is_not_consumed(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "a.json", "repeats": 3, "unlimited": True, "enabled": True,
        }])
        app.workflow_path = Path("flow.json")
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()

        with patch("macroflow.ui.app.save_workflow") as save:
            app._consume_workflow_repeat(0)

        self.assertEqual(app.workflow.steps[0]["repeats"], 3)
        save.assert_not_called()
        app._persist_workflow_draft.assert_not_called()
        self.assertTrue(any("不计次数" in call.args[0] for call in app._log.call_args_list))

    def test_workflow_progress_unlimited_mode(self):
        self.assertEqual(
            workflow_execution_progress(2, 29, "每日任务", 1, unlimited=True),
            "工作流 2/29 · 每日任务\n不计次数 · 每次到达执行 1 次 · F12 停止",
        )

    def test_editing_repeat_cell_opens_workflow_repeat_dialog(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "scripts/a.json", "repeats": 2, "unlimited": False,
            "before_ms": 0, "repeat_interval_ms": 1000,
        }])
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = "0"
        app.workflow_tree.identify_column.return_value = "#3"
        app.root = Mock()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        dialog = Mock()
        dialog.show.return_value = {"repeats": 5, "unlimited": True}

        with patch("macroflow.ui.app.WorkflowRepeatDialog", return_value=dialog):
            app._edit_workflow_cell(Mock(x=100, y=10))

        self.assertEqual(app.workflow.steps[0]["repeats"], 5)
        self.assertTrue(app.workflow.steps[0]["unlimited"])
        app._persist_workflow_draft.assert_called_once()

    def test_batch_settings_apply_unlimited_to_every_step(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"script": "a", "repeats": 1, "before_ms": 0, "repeat_interval_ms": 1000},
            {"script": "b", "repeats": 9, "before_ms": 300, "repeat_interval_ms": 500},
        ])
        app.root = Mock()
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._set_status = Mock()
        dialog = Mock()
        dialog.show.return_value = {"unlimited": True}

        with patch("macroflow.ui.app.WorkflowBatchSettingsDialog", return_value=dialog):
            app.set_all_workflow_step_options()

        self.assertTrue(all(step["unlimited"] for step in app.workflow.steps))

    def test_toggle_selected_workflow_step_preserves_selection(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a", "enabled": True}])
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ("0",)
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._set_status = Mock()

        app.toggle_selected_workflow_step()

        self.assertFalse(app.workflow.steps[0]["enabled"])
        app.workflow_tree.selection_set.assert_called_once_with("0")


class WorkflowDeleteUndoTests(unittest.TestCase):
    def _app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_tree = Mock()
        app.global_tree = Mock()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._update_workflow_selection_color = Mock()
        app._set_status = Mock()
        app.workflow_delete_undo_stack = []
        app.global_delete_undo_stack = []
        return app

    def test_workflow_delete_selects_neighbor_and_undo_restores_row(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
            {"script": "c.json", "step_id": "c"},
        ])
        app.workflow_tree.selection.return_value = ("1",)

        app.delete_workflow_step()

        self.assertEqual([step["step_id"] for step in app.workflow.steps], ["a", "c"])
        app.workflow_tree.selection_set.assert_called_with("1")
        app.workflow_tree.see.assert_called_with("1")

        app.undo_delete_workflow_step()

        self.assertEqual([step["step_id"] for step in app.workflow.steps], ["a", "b", "c"])
        app.workflow_tree.selection_set.assert_called_with("1")
        self.assertEqual(app.workflow_delete_undo_stack, [])

    def test_workflow_delete_last_row_selects_previous_row(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
        ])
        app.workflow_tree.selection.return_value = ("1",)

        app.delete_workflow_step()

        app.workflow_tree.selection_set.assert_called_once_with("0")

    def test_ctrl_a_selects_all_workflow_and_global_rows(self):
        app = self._app()
        app.workflow_tree.get_children.return_value = ("0", "1", "2")
        app.global_tree.get_children.return_value = ("0", "1")

        self.assertEqual(app._select_all_workflow_steps(), "break")
        self.assertEqual(app._select_all_global_modules(), "break")

        app.workflow_tree.selection_set.assert_called_once_with("0", "1", "2")
        app.global_tree.selection_set.assert_called_once_with("0", "1")

    def test_workflow_multi_delete_removes_all_selected_and_undo_restores(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"script": "a.json", "step_id": "a"},
            {"script": "b.json", "step_id": "b"},
            {"script": "c.json", "step_id": "c"},
        ])
        app.workflow_tree.selection.return_value = ("0", "2")

        app.delete_workflow_step()

        self.assertEqual([step["step_id"] for step in app.workflow.steps], ["b"])
        app.undo_delete_workflow_step()
        app.undo_delete_workflow_step()
        self.assertEqual([step["step_id"] for step in app.workflow.steps], ["a", "b", "c"])

    def test_global_multi_delete_removes_all_selected_and_undo_restores(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"kind": "global_module", "step_id": "g1"},
            {"kind": "global_module", "step_id": "g2"},
            {"kind": "global_module", "step_id": "g3"},
            {"script": "task.json", "step_id": "task"},
        ])
        app.global_tree.selection.return_value = ("0", "2")

        app.delete_global_module()

        self.assertEqual([step["step_id"] for step in app._global_module_steps()], ["g2"])
        app.undo_delete_global_module()
        app.undo_delete_global_module()
        self.assertEqual(
            [step["step_id"] for step in app._global_module_steps()], ["g1", "g2", "g3"],
        )

    def test_main_log_is_appended_to_its_session_file(self):
        with tempfile.TemporaryDirectory() as folder:
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.log_text = Mock()
            app.session_log_path = Path(folder) / "2026-08-11" / "session.log"
            app.session_log_path.parent.mkdir(parents=True)
            with patch("macroflow.ui.app.get_cursor_pos", return_value=(12, 34)):
                app._log("备份完成")
            self.assertIn("[鼠标 12,34] 备份完成", app.session_log_path.read_text(encoding="utf-8"))

    def test_worker_log_is_written_before_ui_callback_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.root = Mock()
            app.log_text = Mock()
            app.log_file_lock = threading.Lock()
            app.session_log_path = Path(folder) / "2026-08-11" / "session.log"
            with patch("macroflow.ui.app.get_cursor_pos", return_value=(56, 78)):
                app._ui(app._log, "后台识别完成")
            self.assertIn(
                "[鼠标 56,78] 后台识别完成",
                app.session_log_path.read_text(encoding="utf-8"),
            )
            app.log_text.insert.assert_not_called()
            queued_callback, queued_line = app.root.after.call_args.args[1:]
            queued_callback(queued_line)
            app.log_text.insert.assert_called_once()

    def test_multi_toggle_applies_one_state_to_all_selected_rows(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"kind": "global_module", "step_id": "g1", "enabled": False},
            {"kind": "global_module", "step_id": "g2", "enabled": True},
            {"script": "a.json", "step_id": "a", "enabled": True},
            {"script": "b.json", "step_id": "b", "enabled": True},
        ])
        app.workflow_tree.selection.return_value = ("0", "1")
        app.global_tree.selection.return_value = ("0", "1")

        app.toggle_selected_workflow_step()
        app.toggle_selected_global_module()

        self.assertTrue(all(not step["enabled"] for step in app._workflow_only_steps()))
        self.assertTrue(all(step["enabled"] for step in app._global_module_steps()))
        app.workflow_tree.selection_set.assert_called_once_with("0", "1")
        app.global_tree.selection_set.assert_called_once_with("0", "1")

    def test_global_tree_shows_live_module_registry_disabled_state(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "kind": "global_module", "enabled": True,
            "config": {
                "module_ref": True, "module_key": "module:global-disabled",
                "template": "images/global.png",
            },
        }])
        app.global_tree = Mock()
        app.global_tree.get_children.return_value = ()
        app.empty_global_hint = Mock()
        app._global_module_label = Mock(return_value="◆ 模块对象 · 已禁用全局模块")
        app._autosize_tree_column = Mock()

        with patch("macroflow.ui.app.registered_module_object", return_value={"enabled": False}):
            app.rebuild_global_tree()

        insert = app.global_tree.insert.call_args
        self.assertEqual(insert.kwargs["values"][2], "● 模块已禁用")
        self.assertEqual(insert.kwargs["tags"], ("disabled",))

    def test_global_modules_can_be_deleted_continuously_and_undone(self):
        app = self._app()
        app.workflow = Workflow(steps=[
            {"kind": "global_module", "step_id": "g1"},
            {"kind": "global_module", "step_id": "g2"},
            {"kind": "global_module", "step_id": "g3"},
            {"script": "task.json", "step_id": "task"},
        ])
        app.global_tree.selection.return_value = ("1",)

        app.delete_global_module()
        self.assertEqual(
            [step["step_id"] for step in app._global_module_steps()], ["g1", "g3"],
        )
        app.global_tree.selection_set.assert_called_with("1")

        app.global_tree.selection.return_value = ("1",)
        app.delete_global_module()
        self.assertEqual([step["step_id"] for step in app._global_module_steps()], ["g1"])
        app.global_tree.selection_set.assert_called_with("0")

        app.undo_delete_global_module()
        app.undo_delete_global_module()

        self.assertEqual(
            [step["step_id"] for step in app._global_module_steps()], ["g1", "g2", "g3"],
        )
        self.assertEqual(app.global_delete_undo_stack, [])

    def test_workflow_global_module_key_only_accepts_module_reference(self):
        self.assertEqual(
            MacroFlowApp._workflow_global_module_key({
                "config": {"module_ref": True, "template": "images/global.png"},
            }),
            "images/global.png",
        )
        self.assertEqual(
            MacroFlowApp._workflow_global_module_key({"config": {"template": "legacy.png"}}),
            "",
        )

    def test_open_global_module_editor_keeps_identity_and_updates_shared_image(self):
        app = self._app()
        app.root = Mock()
        step = {
            "kind": "global_module",
            "config": {"module_ref": True, "template": "images/old.png"},
        }
        obj = {"category": "global", "name": "旧模块", "template": "images/old.png"}
        updated = {"category": "global", "name": "新模块", "template": "images/new.png"}
        form = Mock()
        form.show.return_value = ("images/old.png", "images/old.png", updated)
        with patch("macroflow.ui.app.registered_module_object", return_value=obj), \
             patch("macroflow.ui.app.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("macroflow.ui.app.update_module_object") as update:
            app._open_module_object_editor("images/old.png", workflow_step=step)

        form_class.assert_called_once_with(
            app.root, "images/old.png", object_dict=obj, category="global",
        )
        update.assert_called_once_with("images/old.png", updated, old_key="images/old.png")
        self.assertEqual(step["config"]["module_key"], "images/old.png")
        self.assertEqual(step["config"]["template"], "images/new.png")
        app.rebuild_workflow_tree.assert_called_once()
        app._persist_workflow_draft.assert_called_once()

    def test_open_global_module_in_new_window_passes_module_key(self):
        app = self._app()
        app._log = Mock()
        step = {
            "kind": "global_module",
            "config": {"module_ref": True, "template": "images/global.png"},
        }
        with patch("macroflow.ui.app.registered_module_object", return_value={"category": "global"}), \
             patch("macroflow.ui.app.spawn_new_instance") as spawn:
            app._open_workflow_global_module_in_new_window(step)

        args = spawn.call_args.args[0]
        self.assertEqual(args[-2:], ["--edit-module", "images/global.png"])

    def test_disabled_selection_uses_distinct_highlight_color(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a", "enabled": False}])
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ("0",)
        app.root = Mock()

        app._update_workflow_selection_color()

        call = app.root.style.map.call_args
        self.assertEqual(call.args[0], "Workflow.Treeview")
        self.assertEqual(call.kwargs["background"], [("selected", "#6B4615")])
        self.assertEqual(call.kwargs["foreground"], [("selected", "#FFE1A3")])

    def test_unlimited_selection_uses_distinct_highlight_color(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{
            "script": "a", "enabled": True, "unlimited": True,
        }])
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ("0",)
        app.root = Mock()

        app._update_workflow_selection_color()

        call = app.root.style.map.call_args
        self.assertEqual(call.args[0], "Workflow.Treeview")
        self.assertEqual(call.kwargs["background"], [("selected", "#1F4D30")])
        self.assertEqual(call.kwargs["foreground"], [("selected", "#7BC96F")])

    def test_execution_skips_missing_script_and_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            valid_path = Path(folder) / "valid.json"
            save_script(MacroScript(name="有效脚本", actions=[{"type": "delay", "ms": 0}]), valid_path)
            missing_path = Path(folder) / "missing.json"

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(missing_path), "repeats": 1, "before_ms": 0},
                {"script": str(valid_path), "repeats": 1, "before_ms": 0},
            ]

            app._run_workflow_worker(steps, None, None, False)

            app.player.play.assert_called_once()
            self.assertEqual(
                app.player.play.call_args.kwargs["repeat_interval_ms"],
                DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
            )
            self.assertTrue(any("跳过工作流第 1/2 行" in call.args[0] for call in app._log.call_args_list))

    def test_execution_skips_disabled_script_and_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            disabled_path = Path(folder) / "disabled.json"
            enabled_path = Path(folder) / "enabled.json"
            save_script(MacroScript(name="禁用项", actions=[{"type": "delay", "ms": 1}]), disabled_path)
            save_script(MacroScript(name="启用项", actions=[{"type": "delay", "ms": 2}]), enabled_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(disabled_path), "enabled": False},
                {"script": str(enabled_path), "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False)

            app.player.play.assert_called_once()
            self.assertEqual(app.player.play.call_args.args[0], [{"type": "delay", "ms": 2}])
            self.assertTrue(any("该任务已禁用" in call.args[0] for call in app._log.call_args_list))

    def test_execution_skips_zero_repeat_script_and_continues(self):
        with tempfile.TemporaryDirectory() as folder:
            exhausted_path = Path(folder) / "exhausted.json"
            enabled_path = Path(folder) / "enabled.json"
            save_script(MacroScript(name="次数用完", actions=[{"type": "delay", "ms": 1}]), exhausted_path)
            save_script(MacroScript(name="仍可执行", actions=[{"type": "delay", "ms": 2}]), enabled_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(exhausted_path), "repeats": 0, "enabled": True},
                {"script": str(enabled_path), "repeats": 1, "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False)

            app.player.play.assert_called_once()
            self.assertEqual(app.player.play.call_args.args[0], [{"type": "delay", "ms": 2}])
            self.assertTrue(any("执行次数已用完" in call.args[0] for call in app._log.call_args_list))

    def test_execution_runs_unlimited_row_even_with_zero_repeats(self):
        with tempfile.TemporaryDirectory() as folder:
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 1}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(unlimited_path), "repeats": 0, "unlimited": True, "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False)

            app.player.play.assert_called_once()
            self.assertEqual(app.player.play.call_args.args[1], 1)
            self.assertFalse(any("执行次数已用完" in call.args[0] for call in app._log.call_args_list))
            self.assertTrue(any("不计次数" in call.args[0] for call in app._log.call_args_list))

    def test_workflow_test_mode_caps_counted_rows_and_keeps_unlimited_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            counted_path = Path(folder) / "counted.json"
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="计次", actions=[{"type": "delay", "ms": 1}]), counted_path)
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 2}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(counted_path), "repeats": 8, "enabled": True},
                {"script": str(unlimited_path), "repeats": 0, "unlimited": True, "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False, test_mode=True)

            self.assertEqual(app.player.play.call_count, 2)
            self.assertEqual([call.args[1] for call in app.player.play.call_args_list], [1, 1])
            self.assertTrue(any("测试执行 1 次" in call.args[0] for call in app._log.call_args_list))
            self.assertTrue(any("不计次数" in call.args[0] for call in app._log.call_args_list))

    def test_workflow_test_mode_runs_unlimited_when_all_counted_rows_are_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            exhausted_path = Path(folder) / "exhausted.json"
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="用完", actions=[{"type": "delay", "ms": 1}]), exhausted_path)
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 2}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)

            app._run_workflow_worker([
                {"script": str(exhausted_path), "repeats": 0, "enabled": True},
                {"script": str(unlimited_path), "repeats": 0, "unlimited": True, "enabled": True},
            ], None, None, False, test_mode=True)

            app.player.play.assert_called_once()
            self.assertEqual(app.player.play.call_args.args[0], [{"type": "delay", "ms": 2}])

    def test_workflow_test_mode_does_not_consume_remaining_count(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {"repeats": 8, "unlimited": False}
        app.workflow_test_mode_active = True
        app._workflow_only_steps = Mock(return_value=[step])
        app._log = Mock()

        app._consume_workflow_repeat(0)

        self.assertEqual(step["repeats"], 8)
        self.assertIn("测试模式，不扣减次数", app._log.call_args.args[0])

    def test_repeat_count_is_consumed_before_next_workflow_step_starts(self):
        with tempfile.TemporaryDirectory() as folder:
            first_path = Path(folder) / "first.json"
            second_path = Path(folder) / "second.json"
            save_script(MacroScript(name="第一步", actions=[{"type": "delay", "ms": 1}]), first_path)
            save_script(MacroScript(name="第二步", actions=[{"type": "delay", "ms": 1}]), second_path)

            steps = [
                {"script": str(first_path), "repeats": 2, "enabled": True},
                {"script": str(second_path), "repeats": 1, "enabled": True},
            ]
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow = Workflow(steps=steps)
            app.workflow_path = None
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._workflow_only_steps = lambda: steps
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            queued_ui_calls = []
            app._ui = lambda callback, *args: queued_ui_calls.append((callback, args))
            observed_remaining = []

            def play(*args, **kwargs):
                observed_remaining.append(steps[0]["repeats"])
                total = int(args[1])
                for current in range(1, total + 1):
                    kwargs["on_repeat_complete"](current, total)

            app.player.play.side_effect = play

            app._run_workflow_worker(steps, None, None, False)

            self.assertEqual(observed_remaining, [2, 0])

    def test_workflow_executes_module_row_without_script_file(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app._ui = lambda callback, *args: callback(*args)
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:claim", "template": "images/claim.png",
        }
        step = {
            "kind": "module", "action": action, "repeats": 1,
            "enabled": True, "before_ms": 0,
        }

        with patch("macroflow.ui.app.registered_module_object", return_value={
            "name": "领取", "category": "switch", "template": "images/claim.png",
        }):
            app._run_workflow_worker([step], None, None, False)

        app.player.play.assert_called_once()
        self.assertEqual(app.player.play.call_args.args[0], [action])
        self.assertTrue(any("模块 领取" in call.args[0] for call in app._log.call_args_list))

    def test_workflow_ends_when_all_counted_steps_exhausted(self):
        with tempfile.TemporaryDirectory() as folder:
            exhausted_path = Path(folder) / "exhausted.json"
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="次数用完", actions=[{"type": "delay", "ms": 1}]), exhausted_path)
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 2}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.global_guards = {"m1": {"key": "m1"}}
            app.guards_lock = threading.Lock()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(exhausted_path), "repeats": 0, "enabled": True},
                {"script": str(unlimited_path), "repeats": 0, "unlimited": True, "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False)

            app.player.play.assert_not_called()
            self.assertEqual(app.global_guards, {})
            self.assertTrue(any("所有计次脚本已执行完毕" in call.args[0] for call in app._log.call_args_list))
            self.assertTrue(any("工作流结束" in call.args[0] for call in app._append_mini_step.call_args_list))
            app._sound.assert_any_call("run_done")

    def test_workflow_does_not_end_while_counted_steps_remain(self):
        with tempfile.TemporaryDirectory() as folder:
            exhausted_path = Path(folder) / "exhausted.json"
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="次数用完", actions=[{"type": "delay", "ms": 1}]), exhausted_path)
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 2}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.global_guards = {"m1": {"key": "m1"}}
            app.guards_lock = threading.Lock()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(exhausted_path), "repeats": 0, "enabled": True},
                {"script": str(unlimited_path), "repeats": 0, "unlimited": True, "enabled": True},
                {"script": str(exhausted_path), "repeats": 2, "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False)

            self.assertEqual(app.player.play.call_count, 2)
            # 守卫生命周期 = 一次执行：工作流结束后清空，不能残留到下一次执行。
            self.assertEqual(app.global_guards, {})
            self.assertFalse(any("所有计次脚本已执行完毕" in call.args[0] for call in app._log.call_args_list))

    def test_unlimited_only_workflow_does_not_end_prematurely(self):
        with tempfile.TemporaryDirectory() as folder:
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 1}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.global_guards = {"m1": {"key": "m1"}}
            app.guards_lock = threading.Lock()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(unlimited_path), "repeats": 0, "unlimited": True, "enabled": True},
            ]

            app._run_workflow_worker(steps, None, None, False)

            app.player.play.assert_called_once()
            # 守卫生命周期 = 一次执行：工作流结束后清空，不能残留到下一次执行。
            self.assertEqual(app.global_guards, {})
            self.assertFalse(any("所有计次脚本已执行完毕" in call.args[0] for call in app._log.call_args_list))

    def test_selected_row_unlimited_step_runs_after_prior_steps_are_exhausted(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "selected.json"
            save_script(MacroScript(name="选中行", actions=[{"type": "delay", "ms": 1}]), script_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)

            app._run_workflow_worker(
                [
                    {"script": str(script_path), "repeats": 0, "enabled": True},
                    {
                        "script": str(script_path),
                        "repeats": 0,
                        "unlimited": True,
                        "enabled": True,
                    },
                ],
                None,
                None,
                False,
                start_index=1,
            )

            app.player.play.assert_called_once()

    def test_workflow_can_start_from_selected_row(self):
        with tempfile.TemporaryDirectory() as folder:
            first_path = Path(folder) / "first.json"
            second_path = Path(folder) / "second.json"
            save_script(MacroScript(name="第一项", actions=[{"type": "delay", "ms": 11}]), first_path)
            save_script(MacroScript(name="第二项", actions=[{"type": "delay", "ms": 22}]), second_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app._ui = lambda callback, *args: callback(*args)
            steps = [
                {"script": str(first_path), "repeats": 1, "before_ms": 0},
                {"script": str(second_path), "repeats": 1, "before_ms": 0},
            ]

            app._run_workflow_worker(steps, None, None, False, start_index=1)

            app.player.play.assert_called_once()
            self.assertEqual(app.player.play.call_args.args[0], [{"type": "delay", "ms": 22}])
            self.assertTrue(any("从第 2/2 行开始" in call.args[0] for call in app._log.call_args_list))

    def test_run_workflow_from_selected_uses_shared_test_mode_option(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._selected_workflow_index = Mock(return_value=3)
        app.run_workflow = Mock()

        app.run_workflow_from_selected()

        app.run_workflow.assert_called_once_with(start_index=3)

    def test_new_workflow_run_reads_checked_test_mode_option(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.worker = None
        app.workflow_test_mode_var = Mock()
        app.workflow_test_mode_var.get.return_value = True
        app.workflow_test_mode_active = False
        app._workflow_only_steps = Mock(return_value=[{"script": "unused.json"}])
        app._global_module_steps = Mock(return_value=[])
        app._workflow_snapshot = Mock(side_effect=RuntimeError("stop after mode selection"))

        with self.assertRaisesRegex(RuntimeError, "stop after mode selection"):
            app.run_workflow()

        self.assertTrue(app.workflow_test_mode_active)

    def test_run_workflow_suppress_start_sound_skips_sound(self):
        # 「重新执行工作流」用 suppress_start_sound=True 重启，不能重复播放
        # 开始提示音；该参数曾缺失导致 TypeError（重启直接失败）。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            script_path = Path(folder) / "plain.json"
            save_script(MacroScript(name="普通脚本", actions=[{"type": "delay", "ms": 1}]),
                        script_path)

            def make_app() -> MacroFlowApp:
                app = MacroFlowApp.__new__(MacroFlowApp)
                app.worker = None
                app.workflow_test_mode_var = Mock()
                app.workflow_test_mode_var.get.return_value = False
                app._workflow_only_steps = Mock(
                    return_value=[{"script": str(script_path), "repeats": 1}],
                )
                app._global_module_steps = Mock(return_value=[])
                app._workflow_snapshot = Mock()
                app._persist_workflow_draft = Mock()
                app.rebuild_workflow_tree = Mock()
                app.workflow_start_var = Mock()
                app.workflow_start_var.get.return_value = ""
                app._bound_hwnd = Mock(return_value=123)
                app._activation_settings_from_script = Mock(return_value=(False, None))
                app._log = Mock()
                app._notify = Mock()
                app.focus_mode_enabled_var = Mock()
                app.focus_mode_enabled_var.get.return_value = False
                app.activate_target_enabled_var = Mock()
                app.activate_target_enabled_var.get.return_value = True
                app.activation_enabled_var = Mock()
                app.activation_enabled_var.get.return_value = False
                app._clear_global_guards = Mock()
                app._clear_global_detect_rearm_locks = Mock()
                app.workflow_stop = threading.Event()
                app._sound = Mock()
                app._hide_main_for_execution = Mock()
                app._reset_execution_clock_for_new_run = Mock()
                app._set_execution_progress = Mock()
                app._show_execution_mini = Mock()
                app._append_mini_step = Mock()
                return app

            app = make_app()
            with patch("macroflow.ui.app.threading.Thread"):
                app.run_workflow(suppress_start_sound=True)
            app._sound.assert_not_called()

            app = make_app()
            with patch("macroflow.ui.app.threading.Thread"):
                app.run_workflow()
            app._sound.assert_called_once_with("run_start")

    def test_workflow_start_delay_settings_are_persisted(self):
        workflow = Workflow.from_dict({
            "name": "延时工作流", "steps": [],
            "start_delay_enabled": True, "start_delay_seconds": 12,
        })
        self.assertTrue(workflow.start_delay_enabled)
        self.assertEqual(workflow.start_delay_seconds, 12)
        self.assertEqual(workflow.to_dict()["start_delay_seconds"], 12)

    def test_workflow_start_delay_subsecond_rounds_up(self):
        # 存储精度是整秒：500ms → 1s、2500ms → 3s，亚秒延时不能被截断成 0。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(name="w", steps=[])
        app.workflow_start_delay_enabled_var = Mock()
        app.workflow_start_delay_enabled_var.get.return_value = True
        app.workflow_start_delay_seconds_var = Mock()
        app.workflow_start_delay_seconds_var.get.return_value = "500"
        self.assertEqual(app._read_workflow_start_delay(validate=False), 1)
        app.workflow_start_delay_seconds_var.get.return_value = "2500"
        self.assertEqual(app._read_workflow_start_delay(validate=False), 3)

    def test_guard_wait_guard_request_skips_wait_and_continues(self):
        # 步骤间隙等待中，守卫处理段要求结束/推进/跳转：无脚本上下文可作用，
        # 必须跳过剩余等待继续工作流，而不是把 False 当终止、静默杀掉工作流。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.player.handle_guard_hit = Mock(side_effect=AdvanceToNextWorkflowStep())
        app._evaluate_global_guards = Mock(return_value={"kind": "success"})
        app._ui = lambda callback, *args: callback(*args)
        app._log = Mock()

        self.assertTrue(app._guard_wait(5.0))
        self.assertTrue(any(
            "继续工作流" in call.args[0] for call in app._log.call_args_list
        ))

    def test_save_workflow_rename_never_overwrites_existing_file(self):
        # 改名保存曾直接落默认目录：与已有同名工作流文件冲突时静默覆盖
        # （数据丢失），旧文件也遗留成孤儿。修复：改名走“绝不覆盖”去重路径。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder, \
             patch("macroflow.ui.app.WORKFLOWS_DIR", Path(folder)):
            existing = Path(folder) / "B.json"
            existing.write_text("existing", encoding="utf-8")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow = Workflow(name="B", steps=[])
            app.workflow_name_var = Mock()
            app.workflow_name_var.get.return_value = "B"
            app.workflow_start_var = Mock()
            app.workflow_start_var.get.return_value = ""
            app.workflow_path = None
            app._read_workflow_start_delay = Mock(return_value=0)
            app._persist_workflow_draft = Mock()
            app._set_status = Mock()
            app._log = Mock()
            app._notify = Mock()
            app.save_current_workflow()
            self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
            self.assertTrue((Path(folder) / "B (2).json").is_file())

    def test_save_workflow_rename_removes_old_file(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder, \
             patch("macroflow.ui.app.WORKFLOWS_DIR", Path(folder)):
            old = Path(folder) / "A.json"
            old.write_text("old", encoding="utf-8")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow = Workflow(name="B", steps=[])
            app.workflow_name_var = Mock()
            app.workflow_name_var.get.return_value = "B"
            app.workflow_start_var = Mock()
            app.workflow_start_var.get.return_value = ""
            app.workflow_path = old
            app._read_workflow_start_delay = Mock(return_value=0)
            app._persist_workflow_draft = Mock()
            app._set_status = Mock()
            app._log = Mock()
            app._notify = Mock()
            app.save_current_workflow()
            self.assertFalse(old.exists())
            self.assertTrue((Path(folder) / "B.json").is_file())

    def _rename_app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.workflow = Workflow(name="A", steps=[])
        app.workflow_name_var = Mock()
        app.workflow_name_var.get.return_value = "A"
        app.save_current_workflow = Mock()
        app._notify = Mock()
        return app

    def test_rename_workflow_applies_name_and_saves(self):
        app = self._rename_app()
        with patch("macroflow.ui.app.simpledialog.askstring", return_value="B"):
            app.rename_workflow()
        app.workflow_name_var.set.assert_called_once_with("B")
        app.save_current_workflow.assert_called_once()

    def test_rename_workflow_cancel_keeps_name(self):
        app = self._rename_app()
        with patch("macroflow.ui.app.simpledialog.askstring", return_value=None):
            app.rename_workflow()
        app.workflow_name_var.set.assert_not_called()
        app.save_current_workflow.assert_not_called()

    def test_rename_workflow_rejects_empty_name(self):
        app = self._rename_app()
        with patch("macroflow.ui.app.simpledialog.askstring", return_value="   "):
            app.rename_workflow()
        app.workflow_name_var.set.assert_not_called()
        app.save_current_workflow.assert_not_called()
        app._notify.assert_called_once()

    def _duplicate_app(self, workflow: Workflow) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        app.workflow = workflow
        app.workflow_path = None
        app.workflow_name_var = Mock()
        app.workflow_name_var.get.return_value = workflow.name
        app.workflow_start_var = Mock()
        app.workflow_start_var.get.return_value = workflow.start_at
        app.workflow_start_delay_enabled_var = Mock()
        app.workflow_start_delay_enabled_var.get.return_value = workflow.start_delay_enabled
        app.workflow_start_delay_seconds_var = Mock()
        app.workflow_start_delay_seconds_var.get.return_value = "5000"
        app.workflow_start_delay_seconds_var.unit = Mock()
        app._read_workflow_start_delay = Mock(return_value=0)
        app._clear_workflow_delete_history = Mock()
        app._toggle_workflow_start_delay_control = Mock()
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._set_status = Mock()
        app._log = Mock()
        app._notify = Mock()
        return app

    def test_duplicate_workflow_creates_new_file_and_opens_copy(self):
        # 复制后生成独立新文件：新名称、步骤 ID 重新分配、不继承定时开始时间，
        # 原工作流文件不受影响，界面立即切换到副本。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder, \
             patch("macroflow.ui.app.WORKFLOWS_DIR", Path(folder)):
            original = Workflow(
                name="原流程", start_at="2026-01-01 12:00:00",
                steps=[{"script": "a.json", "step_id": "s1"},
                       {"kind": "global_module", "step_id": "s2"}],
            )
            original_path = Path(folder) / "原流程.json"
            save_workflow(original, original_path)
            original_snapshot = original_path.read_text(encoding="utf-8")
            app = self._duplicate_app(original)
            app.workflow_path = original_path
            with patch("macroflow.ui.app.simpledialog.askstring", return_value="副本"):
                app.duplicate_workflow()
            target = Path(folder) / "副本.json"
            self.assertTrue(target.is_file())
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["name"], "副本")
            self.assertEqual(saved["start_at"], "")
            self.assertEqual(len(saved["steps"]), 2)
            self.assertNotIn("s1", [step["step_id"] for step in saved["steps"]])
            self.assertNotIn("s2", [step["step_id"] for step in saved["steps"]])
            # 原工作流文件保持原样，界面切换到副本。
            self.assertEqual(original_path.read_text(encoding="utf-8"), original_snapshot)
            self.assertEqual(app.workflow.name, "副本")
            self.assertEqual(app.workflow_path, target)
            self.assertEqual(app.workflow.start_at, "")
            self.assertEqual(len(app.workflow.steps), 2)
            app.rebuild_workflow_tree.assert_called_once()
            app._persist_workflow_draft.assert_called_once()

    def test_duplicate_workflow_avoids_name_collision(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder, \
             patch("macroflow.ui.app.WORKFLOWS_DIR", Path(folder)):
            (Path(folder) / "副本.json").write_text("existing", encoding="utf-8")
            app = self._duplicate_app(Workflow(name="原流程", steps=[]))
            with patch("macroflow.ui.app.simpledialog.askstring", return_value="副本"):
                app.duplicate_workflow()
            self.assertEqual((Path(folder) / "副本.json").read_text(encoding="utf-8"), "existing")
            self.assertTrue((Path(folder) / "副本 (2).json").is_file())

    def test_duplicate_workflow_cancel_keeps_current(self):
        app = self._duplicate_app(Workflow(name="原流程", steps=[]))
        with patch("macroflow.ui.app.simpledialog.askstring", return_value=None):
            app.duplicate_workflow()
        self.assertIsNone(app.workflow_path)
        self.assertEqual(app.workflow.name, "原流程")
        app.rebuild_workflow_tree.assert_not_called()

    def test_duplicate_workflow_rejects_empty_name(self):
        app = self._duplicate_app(Workflow(name="原流程", steps=[]))
        with patch("macroflow.ui.app.simpledialog.askstring", return_value="  "):
            app.duplicate_workflow()
        app._notify.assert_called_once()
        self.assertIsNone(app.workflow_path)

    def test_workflow_worker_waits_for_configured_start_delay(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = Mock()
        app.workflow_stop.wait.return_value = True
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._leave_focus_mode = Mock()
        app._finish_execution_visibility = Mock()
        app._ui = lambda callback, *args: callback(*args)

        app._run_workflow_worker([], None, None, False, start_delay_seconds=7)

        app.workflow_stop.wait.assert_called_once_with(7)
        self.assertTrue(any("启动延时 7 秒" in call.args[0] for call in app._log.call_args_list))


class GuardTestHelpers:
    """守卫引擎测试共用夹具：guard 字典构造与 app 骨架。"""

    def _make_guard(self, template, **overrides):
        """Build a guard dict as used by the guard engine (no thread/stop fields)."""
        guard = {
            "key": "<test>",
            "module": None,
            "template": Path(template),
            "threshold": 0.85,
            "interval_ms": 100,
            "start_delay_ms": 0,
            "start_delay_since": 0.0,
            "start_delay_done": False,
            "fallback_module_key": "",
            "fallback_click": False,
            "fallback_click_count": 1,
            "fallback_click_interval_ms": 100,
            "fallback_present": False,
            "fallback_click_since": 0.0,
            "ignore_background": False,
            "recognize": "",
            "expected_text": "",
            "match_mode": "contains",
            "wait_text_absent": False,
            "target_absent_armed": False,
            "click_count": 1,
            "ocr_offset_up": 0, "ocr_offset_down": 0,
            "ocr_offset_left": 0, "ocr_offset_right": 0,
            "hold_ms": 0,
            "hold_enabled": True,
            "delay_ms": 0,
            "region_mode": "screen",
            "region": None,
            "click": None,
            "jump_row": 0,
            "jump_action_id": "",
            "module_ref": False,
            "module_key": "",
            "module_display_name": "测试模块",
            "after_action": "click_match",
            "button": "left",
            "second": None,
            "segment": [],
            "success_segment": [],
            "segment_ready": False,
            "timeout_enabled": False,
            "not_found_timeout_ms": 3000,
            "timeout_segment": [],
            "timeout_triggered": False,
            "not_found_since": time.perf_counter(),
            "trigger_kind": "success",
            "was_detected": False,
            "triggered": False,
            "awaiting_clear": False,
            "awaiting_clear_logged": False,
            "match_since": None,
            "match_data": None,
            "last_check_time": 0.0,
            "warned_missing_template": False,
            "warned_find_error": False,
            "warned_missing_module": False,
            "standalone_replay": None,
        }
        guard.update(overrides)
        return guard

    def _make_guard_app(self):
        """App skeleton with the guard-engine state the evaluator touches."""
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app.global_detect_trigger_count = 0
        app._evaluating_guards = False
        app.exiting = False
        app._bound_hwnd = Mock(return_value=None)
        app._restore_workflow_scan_foreground = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._log = Mock()
        app._append_mini_step = Mock()
        app.player = MacroPlayer()
        return app


class GlobalDetectTests(GuardTestHelpers, unittest.TestCase):
    def test_switch_module_fallback_clicks_once_then_main_match_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            main_path = Path(folder) / "main.png"
            fallback_path = Path(folder) / "fallback.png"
            main_path.write_bytes(b"main")
            fallback_path.write_bytes(b"fallback")
            main_obj = {
                "name": "主模块", "template": str(main_path), "region": [1, 2, 30, 40],
                "threshold": 0.85, "interval_ms": 50, "blocking": True,
                "fallback_module_key": "module:fallback", "fallback_click": True,
                "after_action": "continue",
            }
            fallback_obj = {
                "name": "备用模块", "template": str(fallback_path), "region": [5, 6, 20, 20],
                "threshold": 0.8, "button": "left", "click_count": 1,
            }
            main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                          "center_x": 25, "center_y": 40, "score": 0.9}
            fallback_match = {"x": 50, "y": 60, "width": 20, "height": 20,
                              "center_x": 60, "center_y": 70, "score": 0.9}
            player = MacroPlayer()
            player._wait = Mock()
            with patch("macroflow.execution.player.registered_module_object", side_effect=lambda key: {
                "module:main": main_obj, "module:fallback": fallback_obj,
            }.get(key)), patch("macroflow.execution.player.find_template", side_effect=[
                None, fallback_match, main_match,
            ]), patch("macroflow.execution.player.send_move_absolute") as move, patch("macroflow.execution.player.send_button") as button, \
                 patch("macroflow.execution.player.show_overlay"):
                player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:main", "template": str(main_path),
                    "region_mode": "template",
                }, None)
            move.assert_called_once_with(60, 70)
            self.assertEqual(button.call_count, 2)

    def test_switch_module_continuous_fallback_is_clicked_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            main_path = Path(folder) / "main.png"
            fallback_path = Path(folder) / "fallback.png"
            main_path.write_bytes(b"main")
            fallback_path.write_bytes(b"fallback")
            main_obj = {
                "name": "main", "template": str(main_path), "region": [1, 2, 30, 40],
                "threshold": 0.85, "interval_ms": 50, "blocking": True,
                "fallback_module_key": "module:fallback", "fallback_click": True,
                "after_action": "continue",
            }
            fallback_obj = {
                "name": "fallback", "template": str(fallback_path),
                "region": [5, 6, 20, 20], "threshold": 0.8,
                "button": "left", "click_count": 1,
            }
            main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                          "center_x": 25, "center_y": 40, "score": 0.9}
            fallback_match = {"x": 50, "y": 60, "width": 20, "height": 20,
                              "center_x": 60, "center_y": 70, "score": 0.9}
            player = MacroPlayer()
            player._wait = Mock()
            with patch("macroflow.execution.player.registered_module_object", side_effect=lambda key: {
                "module:main": main_obj, "module:fallback": fallback_obj,
            }.get(key)), patch("macroflow.execution.player.find_template", side_effect=[
                None, fallback_match, None, fallback_match, main_match,
            ]), patch("macroflow.execution.player.send_move_absolute") as move, patch("macroflow.execution.player.send_button") as button, \
                 patch("macroflow.execution.player.show_overlay"):
                player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:main", "template": str(main_path),
                    "region_mode": "template",
                }, None)
            move.assert_called_once_with(60, 70)
            self.assertEqual(button.call_count, 2)

    def test_switch_module_fallback_can_click_and_exit_main_recognition(self):
        with tempfile.TemporaryDirectory() as folder:
            main_path = Path(folder) / "main.png"
            fallback_path = Path(folder) / "fallback.png"
            main_path.write_bytes(b"main")
            fallback_path.write_bytes(b"fallback")
            main_obj = {
                "name": "main", "template": str(main_path), "region": [1, 2, 30, 40],
                "threshold": 0.85, "interval_ms": 50, "blocking": True,
                "fallback_module_key": "module:fallback",
                "fallback_on_match": "click_exit",
                "after_action": "continue",
            }
            fallback_obj = {
                "name": "fallback", "template": str(fallback_path),
                "region": [5, 6, 20, 20], "threshold": 0.8,
                "button": "left", "click_count": 1,
            }
            fallback_match = {"x": 50, "y": 60, "width": 20, "height": 20,
                              "center_x": 60, "center_y": 70, "score": 0.9}
            player = MacroPlayer()
            player._wait = Mock()
            with patch("macroflow.execution.player.registered_module_object", side_effect=lambda key: {
                "module:main": main_obj, "module:fallback": fallback_obj,
            }.get(key)), patch("macroflow.execution.player.find_template", side_effect=[
                None, fallback_match,
            ]) as find, patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button") as button, \
                 patch("macroflow.execution.player.show_overlay"):
                result = player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:main", "template": str(main_path),
                    "region_mode": "template",
                }, None)
        self.assertIsNone(result)
        self.assertEqual(find.call_count, 2)
        move.assert_called_once_with(60, 70)
        self.assertEqual(button.call_count, 2)

    def test_timeout_guard_returns_timeout_hit(self):
        # recognize=none 守卫不截图；超过 not_found_timeout_ms 后返回 kind=timeout 的
        # 处理段描述（超时段动作随 hit 返回，由播放器内联执行）。
        app = self._make_guard_app()
        app._log = Mock()
        app._append_mini_step = Mock()
        segment = [{"type": "delay", "ms": 1}]
        guard = self._make_guard(
            "", recognize="none", timeout_enabled=True, not_found_timeout_ms=0,
            timeout_segment=segment, not_found_since=time.perf_counter() - 1.0,
        )
        app.global_guards[guard["key"]] = guard
        with patch("macroflow.ui.app.find_template_in_image") as find, \
             patch("macroflow.ui.app.registered_module_object", return_value=None):
            hit = app._evaluate_global_guards()
        find.assert_not_called()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], "timeout")
        self.assertEqual(hit["actions"], segment)
        self.assertTrue(guard["timeout_triggered"])

    def test_success_guard_carries_success_segment(self):
        # 引用模块勾选“再执行代码段”：命中后成功代码段必须随 hit 返回由
        # 播放器内联执行（曾因 segment_ready 死分支而永不执行）。
        app = self._make_guard_app()
        segment = [{"type": "delay", "ms": 1}]
        guard = self._make_guard(
            "images/g.png", recognize="image", success_segment=segment,
        )
        app.global_guards[guard["key"]] = guard
        hit = app._build_guard_hit(guard)
        self.assertEqual(hit["kind"], "success")
        self.assertEqual(hit["actions"], segment)

    def test_guard_template_scale_uses_player_screens(self):
        # 守卫图片匹配必须带上播放器当前脚本的录制屏 → 当前屏缩放系数，
        # 否则截图尺寸不同时全局检测的匹配度同样下降。
        app = self._make_guard_app()
        app.player._source_screen = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        app.player._target_screen = {"left": 0, "top": 0, "width": 3840, "height": 2160}
        self.assertEqual(app._guard_template_scale(), 2.0)
        app.player._source_screen = None
        self.assertEqual(app._guard_template_scale(), 1.0)
        app.player = None
        self.assertEqual(app._guard_template_scale(), 1.0)

    def test_ensure_ocr_ready_loads_engine_once(self):
        # OCR 引擎首次导入不可中断且可能耗时数十秒：播放开始前确保就绪，
        # 避免第一次文字识别把“正在播放”卡在导入里。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app.ocr_engine_ready = False
        with patch("macroflow.ui.app._get_engine", return_value=object()) as load:
            self.assertTrue(app._ensure_ocr_ready())
        load.assert_called_once()
        self.assertTrue(app.ocr_engine_ready)
        with patch("macroflow.ui.app._get_engine") as load_again:
            self.assertTrue(app._ensure_ocr_ready())
        load_again.assert_not_called()

    def test_ocr_progress_updates_execution_text_and_progressbar(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.execution_progress_text = ""
        app.mini_mode = "execution"
        app.mini_count_var = Mock()
        app.mini_ocr_progress_var = Mock()
        app.mini_ocr_progressbar = Mock()

        app._on_ocr_progress("正在导入 PaddleOCR", 25)

        self.assertEqual(app.execution_progress_text, "OCR：正在导入 PaddleOCR · 25% · F12 停止")
        app.mini_ocr_progress_var.set.assert_called_once_with(25)
        app.mini_count_var.set.assert_called_once_with(
            "OCR：正在导入 PaddleOCR · 25% · F12 停止",
        )

    def test_ensure_ocr_ready_aborts_when_stop_requested(self):
        # 等待引擎加载期间按 F12：加载完成后必须中止执行，不能继续播放。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.player.stop_event.set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app.ocr_engine_ready = False
        with patch("macroflow.ui.app._get_engine", return_value=object()):
            self.assertFalse(app._ensure_ocr_ready())

    def test_wait_ocr_ready_interruptible_by_stop(self):
        # 预加载线程仍在导入（引擎未就绪）时按 F12：轮询等待必须立即
        # 返回 False，不能像抢初始化锁那样卡住不可中断。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.player.stop_event.set()
        app.ocr_engine_ready = False
        app.ocr_warmup_thread = Mock()
        app.ocr_warmup_thread.is_alive.return_value = True
        self.assertFalse(app._wait_ocr_ready())

    def test_wait_ocr_ready_returns_when_prewarm_finished(self):
        # 预加载线程已结束（失败）或不存在：不再轮询，交由调用方同步重试。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.ocr_engine_ready = False
        app.ocr_warmup_thread = Mock()
        app.ocr_warmup_thread.is_alive.return_value = False
        self.assertTrue(app._wait_ocr_ready())


class ScriptOcrNeedTests(GuardTestHelpers, unittest.TestCase):
    """_script_needs_ocr / _workflow_needs_ocr：按需等待 OCR 引擎的判断。"""

    def test_pure_input_script_does_not_need_ocr(self):
        # 纯键鼠 + 模板匹配脚本：不等待 OCR 引擎，立即开始执行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        actions = [
            {"type": "key", "vk": 65, "down": True},
            {"type": "click", "x": 100, "y": 200},
            {"type": "image_match", "template": "images/a.png", "region_mode": "template"},
            {"type": "global_detect", "template": "images/b.png", "region_mode": "template"},
            {"type": "script_ref", "script": "scripts/other.json"},
        ]
        with patch("macroflow.ui.app.resolve_path", return_value=Path("scripts/other.json")) as resolve, \
                patch("macroflow.ui.app.load_script") as load, \
                patch("macroflow.ui.app.registered_module_object", return_value=None) as lookup:
            self.assertFalse(app._script_needs_ocr(actions))
        load.assert_not_called()

    def test_text_ocr_action_needs_ocr(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        self.assertTrue(app._script_needs_ocr(
            [{"type": "text_ocr", "region": [0, 0, 10, 10]}],
        ))

    def test_ocr_compare_action_needs_ocr(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        self.assertTrue(app._script_needs_ocr(
            [{"type": "ocr_compare", "region": [0, 0, 10, 10]}],
        ))

    def test_multi_condition_click_needs_ocr_only_for_enabled_ocr_conditions(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        image_only = [{
            "type": "multi_condition_click",
            "conditions": [
                {"enabled": True, "type": "image"},
                {"enabled": False, "type": "ocr"},
                {"enabled": False, "type": "number_compare"},
            ],
        }]
        self.assertFalse(app._script_needs_ocr(image_only))
        mixed = [{
            "type": "multi_condition_click",
            "conditions": [
                {"enabled": True, "type": "image"},
                {"enabled": True, "type": "ocr"},
                {"enabled": False, "type": "number_compare"},
            ],
        }]
        self.assertTrue(app._script_needs_ocr(mixed))

    def test_text_guard_needs_ocr(self):
        # 文字识别全局守卫（recognize == "text"）必须等 OCR 引擎。
        app = MacroFlowApp.__new__(MacroFlowApp)
        self.assertTrue(app._script_needs_ocr(
            [{"type": "global_detect", "recognize": "text", "expected_text": "确认"}],
        ))

    def test_module_ref_text_object_needs_ocr(self):
        # 引用模块本身是文字识别模块：命中判断走 OCR。
        app = MacroFlowApp.__new__(MacroFlowApp)
        with patch("macroflow.ui.app.registered_module_object",
                   return_value={"recognize": "text", "expected_text": "体力不足"}) as lookup:
            self.assertTrue(app._script_needs_ocr(
                [{"type": "global_detect", "module_key": "module:123", "module_ref": True}],
            ))
        lookup.assert_called_once_with("module:123")

    def test_module_ref_code_segment_needs_ocr(self):
        # 模块本体是模板，但成功代码段里有文字识别动作。
        app = MacroFlowApp.__new__(MacroFlowApp)
        module = {
            "recognize": "template", "template": "images/a.png",
            "on_success_actions": [{"type": "text_ocr", "region": [0, 0, 10, 10]}],
            "on_timeout_actions": [],
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module):
            self.assertTrue(app._script_needs_ocr(
                [{"type": "global_detect", "module_key": "module:123"}],
            ))

    def test_fallback_text_module_needs_ocr(self):
        # 主模块是模板，备用识别模块是文字模块。
        app = MacroFlowApp.__new__(MacroFlowApp)
        with patch("macroflow.ui.app.registered_module_object",
                   side_effect=[None, {"recognize": "text"}]):
            self.assertTrue(app._script_needs_ocr(
                [{"type": "global_detect", "module_key": "module:main",
                  "fallback_module_key": "module:fb"}],
            ))

    def test_script_ref_follows_referenced_script(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        referenced = Mock()
        referenced.actions = [{"type": "text_ocr", "region": [0, 0, 10, 10]}]
        path = Mock()
        path.is_file.return_value = True
        path.resolve.return_value = "C:/scripts/ref.json"
        with patch("macroflow.ui.app.resolve_path", return_value=path) as resolve, \
                patch("macroflow.ui.app.load_script", return_value=referenced) as load:
            self.assertTrue(app._script_needs_ocr(
                [{"type": "script_ref", "script": "scripts/ref.json"}],
            ))
        load.assert_called_once()

    def test_script_ref_cycle_is_safe(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        a = Mock()
        a.actions = [{"type": "script_ref", "script": "b.json"}]
        b = Mock()
        b.actions = [{"type": "script_ref", "script": "a.json"}]

        def fake_resolve(value):
            path = Mock()
            path.is_file.return_value = True
            path.resolve.return_value = f"C:/{value}"
            return path

        def fake_load(path):
            return a if path.resolve() == "C:/a.json" else b

        with patch("macroflow.ui.app.resolve_path", side_effect=fake_resolve), \
                patch("macroflow.ui.app.load_script", side_effect=fake_load):
            self.assertFalse(app._script_needs_ocr(
                [{"type": "script_ref", "script": "a.json"}],
            ))

    def test_unresolvable_ref_is_conservative(self):
        # 引用脚本解析失败：宁可按需要 OCR 等待，不在播放中途撞上导入。
        app = MacroFlowApp.__new__(MacroFlowApp)
        path = Mock()
        path.is_file.return_value = True
        path.resolve.return_value = "C:/broken.json"
        with patch("macroflow.ui.app.resolve_path", return_value=path), \
                patch("macroflow.ui.app.load_script", side_effect=RuntimeError("解析失败")):
            self.assertTrue(app._script_needs_ocr(
                [{"type": "script_ref", "script": "scripts/broken.json"}],
            ))

    def test_workflow_script_steps(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        pure = Mock()
        pure.actions = [{"type": "click", "x": 1, "y": 2}]
        ocr_script = Mock()
        ocr_script.actions = [{"type": "text_ocr"}]

        def fake_resolve(value):
            path = Mock()
            path.is_file.return_value = True
            path.resolve.return_value = f"C:/{value}"
            return path

        def fake_load(path):
            return pure if path.resolve() == "C:/pure.json" else ocr_script

        with patch("macroflow.ui.app.resolve_path", side_effect=fake_resolve), \
                patch("macroflow.ui.app.load_script", side_effect=fake_load):
            self.assertFalse(app._workflow_needs_ocr(
                [{"kind": "script", "script": "pure.json"}], [],
            ))
            self.assertTrue(app._workflow_needs_ocr(
                [{"kind": "script", "script": "pure.json"},
                 {"kind": "script", "script": "ocr.json"}], [],
            ))

    def test_workflow_module_step(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        steps = [{"kind": "module", "action": {
            "module_key": "module:123", "type": "global_detect",
        }}]
        with patch("macroflow.ui.app.registered_module_object", return_value={"recognize": "text"}):
            self.assertTrue(app._workflow_needs_ocr(steps, []))

    def test_workflow_global_module_config(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        module = {"config": {
            "module_ref": True, "module_key": "module:g", "template": "images/g.png",
        }}
        with patch("macroflow.ui.app.registered_module_object", return_value={"recognize": "text"}):
            self.assertTrue(app._workflow_needs_ocr([], [module]))

    def test_activate_global_detect_from_config_configures_guard(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module = {"kind": "global_module", "script": "m.json", "step_id": "m1"}
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "template": "images/g.png",
                "threshold": "1.7",
                "interval_ms": "50",
                "hold_ms": "700000",
                "restart_delay_ms": "200",
                "region": [100, 50, 300, 200],
                "click_point": [640, 360],
            }, module)
        guard = app.global_guards["workflow:m1"]
        self.assertEqual(guard["threshold"], 1.0)
        self.assertEqual(guard["interval_ms"], 100)
        self.assertEqual(guard["hold_ms"], 600000)
        self.assertEqual(guard["delay_ms"], 200)
        self.assertEqual(guard["template"], Path("images/g.png"))
        self.assertEqual(guard["click"], (640, 360))
        self.assertEqual(guard["region"], (100, 50, 300, 200))
        self.assertEqual(guard["module"], module)
        # 注册只写数据，不启动线程。
        self.assertNotIn("thread", guard)
        self.assertNotIn("stop", guard)

    def test_script_global_module_start_delay_is_loaded_into_guard(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "template": "images/g.png",
            "start_delay_ms": 125000,
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj), \
             patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
            })
        self.assertEqual(app.global_guards["script:row-g"]["start_delay_ms"], 125000)

    def test_guard_check_interval_throttles(self):
        # 节流窗口内重复评估只截一次图：所有到点守卫共享同一帧，未到点的跳过。
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "g.png"
            template.write_bytes(b"x")
            app = self._make_guard_app()
            guard = self._make_guard(template, interval_ms=100)
            app.global_guards[guard["key"]] = guard
            screen = np.zeros((60, 80, 3), dtype=np.uint8)
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (-20, 0))) as capture, \
                 patch("macroflow.ui.app.find_template_in_image", return_value=None), \
                 patch("macroflow.ui.app.show_overlay"):
                self.assertIsNone(app._evaluate_global_guards())
                self.assertEqual(capture.call_count, 1)
                # 拉长间隔：下一次评估未到节流点，直接跳过，不截图。
                guard["interval_ms"] = 10000
                self.assertIsNone(app._evaluate_global_guards())
            self.assertEqual(capture.call_count, 1)

    def test_guard_text_module_trigger_uses_ocr_text_box(self):
        # 识别文字全局守卫：命中后把 OCR 返回的命中文字中心写入 match_data，
        # 处理段点击它而不是整个区域中心。
        app = self._make_guard_app()
        found = {
            "text": "体力不足", "x": 180, "y": 80, "width": 80, "height": 30,
            "center_x": 220, "center_y": 95, "score": 0.99,
        }
        guard = self._make_guard(
            "module-x.png", recognize="text", expected_text="体力不足",
            match_mode="contains", region=None, hold_ms=0,
        )
        app.global_guards[guard["key"]] = guard
        screen = np.zeros((400, 400, 3), dtype=np.uint8)
        with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
             patch("macroflow.ui.app.recognize_image_with_boxes", return_value=("体力不足", [found])) as recognize, \
             patch("macroflow.ui.app.show_overlay"):
            hit = app._evaluate_global_guards()
        recognize.assert_called_once()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], "success")
        self.assertEqual(guard["match_data"]["center_x"], 220)
        self.assertEqual(guard["match_data"]["center_y"], 95)

    def test_guard_text_absent_waits_for_present_then_disappeared_edge(self):
        app = self._make_guard_app()
        guard = self._make_guard(
            "module-x.png", recognize="text", expected_text="加载中",
            match_mode="contains", region=None, wait_text_absent=True,
            target_absent_armed=False, hold_ms=0,
        )
        app.global_guards[guard["key"]] = guard
        screen = np.zeros((400, 400, 3), dtype=np.uint8)
        # 未出现：不触发也不武装。
        with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
             patch("macroflow.ui.app.recognize_image_with_boxes",
                   return_value=("其他文字", [{"text": "其他文字"}])), \
             patch("macroflow.ui.app.show_overlay"):
            self.assertIsNone(app._evaluate_global_guards())
        self.assertFalse(guard["target_absent_armed"])
        # 出现：武装等待消失。
        guard["last_check_time"] = 0.0
        with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
             patch("macroflow.ui.app.recognize_image_with_boxes", return_value=(
                 "加载中",
                 [{"text": "加载中", "x": 180, "y": 80, "width": 80, "height": 30,
                   "center_x": 220, "center_y": 95}],
             )), \
             patch("macroflow.ui.app.show_overlay"):
            self.assertIsNone(app._evaluate_global_guards())
        self.assertTrue(guard["target_absent_armed"])
        # 已消失：满足"先出现后消失"条件，触发。
        guard["last_check_time"] = 0.0
        with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
             patch("macroflow.ui.app.recognize_image_with_boxes",
                   return_value=("已完成", [{"text": "已完成"}])), \
             patch("macroflow.ui.app.show_overlay"):
            hit = app._evaluate_global_guards()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], "success")

    def test_guard_template_absent_waits_for_present_then_disappeared_edge(self):
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "target.png"
            template.write_bytes(b"x")
            app = self._make_guard_app()
            guard = self._make_guard(
                template, region=None, wait_text_absent=True,
                target_absent_armed=False, hold_ms=0,
            )
            app.global_guards[guard["key"]] = guard
            found = {
                "x": 180, "y": 80, "width": 80, "height": 30,
                "center_x": 220, "center_y": 95, "score": 0.99,
            }
            screen = np.zeros((400, 400, 3), dtype=np.uint8)
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=found) as find, \
                 patch("macroflow.ui.app.show_overlay"):
                self.assertIsNone(app._evaluate_global_guards())
            self.assertTrue(guard["target_absent_armed"])
            self.assertEqual(find.call_count, 1)
            guard["last_check_time"] = 0.0
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=None), \
                 patch("macroflow.ui.app.show_overlay"):
                hit = app._evaluate_global_guards()
            self.assertIsNotNone(hit)
            self.assertEqual(guard["match_data"]["center_x"], 220)

    def test_guard_text_module_not_found_triggers_timeout_branch(self):
        app = self._make_guard_app()
        guard = self._make_guard(
            "module-x.png", recognize="text", expected_text="体力不足",
            match_mode="contains", region=None, timeout_enabled=True,
            not_found_timeout_ms=0, not_found_since=time.perf_counter() - 1.0,
        )
        app.global_guards[guard["key"]] = guard
        screen = np.zeros((400, 400, 3), dtype=np.uint8)
        with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (0, 0))), \
             patch("macroflow.ui.app.recognize_image_with_boxes",
                   return_value=("其他文字", [{"text": "其他文字"}])), \
             patch("macroflow.ui.app.show_overlay"):
            hit = app._evaluate_global_guards()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], "timeout")
        self.assertTrue(guard["timeout_triggered"])
        observation = "体力不足 OCR：识别到「其他文字」；期望「体力不足」· 未命中"
        app._log.assert_any_call(observation)
        app._append_mini_step.assert_any_call(observation)

    def test_module_ref_activation_uses_resolved_object_and_preserves_rearm_lock(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = {"workflow:m1"}
        logs = []
        app._log = Mock(side_effect=logs.append)
        app._ui = lambda callback, *args: callback(*args)
        module = {"kind": "global_module", "step_id": "m1"}
        resolved = Path("C:/Macro/images/点击游戏画面.png")
        obj = {
            "threshold": 0.85, "interval_ms": 250, "hold_ms": 100,
            "hold_enabled": True,
            "delay_ms": 0, "after_action": "click_match", "button": "left",
        }

        with patch("macroflow.ui.app.resolve_path", return_value=resolved), \
             patch("macroflow.ui.app.registered_module_object", return_value=obj) as lookup:
            app._activate_global_detect_from_config({
                "template": "images/点击游戏画面.png", "module_ref": True,
                "hold_ms": 1000,
            }, module)

        guard = app.global_guards["workflow:m1"]
        self.assertEqual(guard["hold_ms"], 100)
        # 重新武装锁跨执行保留：同 key 守卫注册后仍处于 awaiting_clear。
        self.assertTrue(guard["awaiting_clear"])
        self.assertTrue(any("持续超过 100 ms" in text for text in logs))
        self.assertEqual(lookup.call_count, 2)
        self.assertTrue(all(
            item.args == ("images/点击游戏画面.png",) for item in lookup.call_args_list
        ))

    def test_disabled_module_reference_cannot_register_guard(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)

        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/disabled.png")), \
             patch("macroflow.ui.app.registered_module_object", return_value={
                 "name": "已禁用模块", "enabled": False,
             }):
            app._activate_global_detect_from_config({
                "module_ref": True, "module_key": "module:disabled",
                "template": "images/disabled.png",
            }, {"kind": "global_module", "step_id": "disabled-row"})

        self.assertEqual(app.global_guards, {})
        self.assertTrue(any("已禁用" in call.args[0] for call in app._log.call_args_list))

    def test_activate_global_detect_from_config_carries_jump_row(self):
        # v1.68：普通脚本内嵌全局模块行的配置携带跳转行，启用日志随之变化。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "jump_row": 4,
                "jump_action_id": "target-a",
                "jump_enabled": True,
            })
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["jump_row"], 4)
        self.assertEqual(guard["jump_action_id"], "target-a")
        self.assertTrue(any("跳转到目标行执行，播放到末尾后结束" in text for text in logs))

    def test_activate_global_detect_jump_disabled_does_not_jump(self):
        # 未勾选“启用触发后跳转”：守卫保留目标配置（避免落入旧版“无跳转则
        # 点击识别处”的兼容分支），但命中打包不带跳转，触发后继续执行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "jump_row": 4,
                "jump_action_id": "target-a",
                "jump_enabled": False,
            })
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["jump_row"], 4)
        self.assertEqual(guard["jump_action_id"], "target-a")
        self.assertTrue(guard["jump_disabled"])
        self.assertTrue(any("不跳转，继续执行脚本" in text for text in logs))
        # 命中打包：不写跳转字段，也不触发旧版“点击识别处”兜底。
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 16, "center_y": 22}))
        self.assertNotIn("jump_row", hit)
        self.assertNotIn("jump_action_id", hit)
        self.assertNotIn("click", hit)

    def test_module_ref_click_match_clicks_detected_position(self):
        # 回归：引用模块「点击识别区域」（默认动作）命中后必须点击识别位置，
        # 与旧全局检测引擎一致——否则脚本内嵌全局模块行“只触发不点击”。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "name": "测试模块",
            "template": "images/g.png", "after_action": "click_match",
            "click_count": 2, "button": "right",
            "ocr_offset_up": 5, "ocr_offset_down": 0,
            "ocr_offset_left": 0, "ocr_offset_right": 0,
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj), \
             patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
            })
        guard = app.global_guards["script:row-g"]
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertEqual(hit["click"], (100, 195))  # y 减去 ocr_offset_up 5
        self.assertEqual(hit["click_count"], 2)
        self.assertEqual(hit["button"], "right")

    def test_module_ref_click_custom_uses_module_point(self):
        # 引用模块「点击自定义位置」：命中后点击模块保存的自定义坐标。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "name": "测试模块",
            "template": "images/g.png", "after_action": "click_custom",
            "click_point": [640, 360], "click_count": 1,
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj), \
             patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
            })
        guard = app.global_guards["script:row-g"]
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertEqual(hit["click"], (640, 360))

    def test_module_ref_continue_does_not_click(self):
        # 引用模块「成功后继续」：命中只触发不点击。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "name": "测试模块",
            "template": "images/g.png", "after_action": "continue",
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj), \
             patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
            })
        guard = app.global_guards["script:row-g"]
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertNotIn("click", hit)

    def test_module_ref_legacy_jump_target_stays_active(self):
        # 回归：引用模块行没有“启用触发后跳转”开关，旧脚本里配置的
        # jump_row/jump_action_id（缺失 jump_enabled 字段）必须继续生效，
        # 命中后跳转——不能因新复选框默认不勾选而静默失效。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "name": "测试模块",
            "template": "images/g.png", "after_action": "click_match",
            "click_count": 2,
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj), \
             patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
                "jump_row": 2, "jump_action_id": "target-a",
            })
        guard = app.global_guards["script:row-g"]
        self.assertFalse(guard["jump_disabled"])
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertEqual(hit["jump_row"], 2)
        self.assertEqual(hit["jump_action_id"], "target-a")
        # 跳转不替代模块的点击：两者都要。
        self.assertEqual(hit["click"], (100, 200))

    def test_module_ref_explicit_jump_disabled_keeps_click(self):
        # 显式写入 jump_enabled=False 的引用模块行：不跳转，但模块动作的
        # 点击仍然执行（点击由模块对象配置，与行级跳转开关无关）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "name": "测试模块",
            "template": "images/g.png", "after_action": "click_match",
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj), \
             patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
                "jump_row": 2, "jump_action_id": "target-a",
                "jump_enabled": False,
            })
        guard = app.global_guards["script:row-g"]
        self.assertTrue(guard["jump_disabled"])
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertNotIn("jump_row", hit)
        self.assertNotIn("jump_action_id", hit)
        self.assertEqual(hit["click"], (100, 200))

    def test_direct_row_with_jump_enabled_clicks_then_jumps(self):
        # 回归：普通行（非引用）配置了跳转目标且“启用触发后跳转”勾选时，
        # 命中后必须先点击识别位置再跳转（旧引擎语义）——否则结算确定这类
        # 按钮永远不会被点击，结算界面不关闭，脚本重复执行时反复触发死循环。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "row-g",
                "template": "images/g.png",
                "jump_row": 14, "jump_action_id": "target-a",
                "jump_enabled": True,
            })
        guard = app.global_guards["script:row-g"]
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertEqual(hit["click"], (100, 200))
        self.assertEqual(hit["jump_row"], 14)
        self.assertEqual(hit["jump_action_id"], "target-a")

    def test_direct_row_without_jump_clicks_match(self):
        # 普通行没有跳转目标：命中后点击识别位置（“点击位置留空=点识别处”）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "row-g",
                "template": "images/g.png",
            })
        guard = app.global_guards["script:row-g"]
        app._bound_hwnd = Mock(return_value=None)
        hit = app._build_guard_hit(dict(guard, match_data={"center_x": 100, "center_y": 200}))
        self.assertEqual(hit["click"], (100, 200))

    def test_clear_global_guards_empties_registry(self):
        # _clear_global_guards 替代 _stop_all_global_detect_monitors：只清空守卫
        # 注册表（守卫无线程可停）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {"a": {"key": "a"}, "b": {"key": "b"}}
        app.guards_lock = threading.Lock()
        app._clear_global_guards()
        self.assertEqual(app.global_guards, {})

    def test_activate_global_detect_defaults_click_delay_to_1000ms(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "global-a",
                "template": "images/g.png",
            })
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["delay_ms"], 1000)

    def test_activate_global_detect_multiple_modules_each_get_own_guard(self):
        # 核心回归：每个全局模块启用后都有自己的守卫，互不覆盖。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_a = {"kind": "global_module", "script": "a.json", "step_id": "a"}
        module_b = {"kind": "global_module", "script": "b.json", "step_id": "b"}
        with patch("macroflow.ui.app.resolve_path", side_effect=[Path("images/a.png"), Path("images/b.png")]):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/a.png"}, module_a,
            )
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/b.png"}, module_b,
            )
        self.assertEqual(set(app.global_guards), {"workflow:a", "workflow:b"})
        self.assertEqual(app.global_guards["workflow:a"]["template"], Path("images/a.png"))
        self.assertEqual(app.global_guards["workflow:b"]["template"], Path("images/b.png"))
        # 同一个模块重新启用（工作流恢复）会替换旧守卫，而不是再开一个。
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/a.png")):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/a.png"}, module_a,
            )
        self.assertEqual(len(app.global_guards), 2)

    def test_script_global_actions_each_keep_an_independent_guard(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)

        with patch("macroflow.ui.app.resolve_path", side_effect=[Path("images/mainline.png"), Path("images/init.png")]):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "mainline",
                "template": "images/mainline.png",
            })
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "initialize",
                "template": "images/init.png",
            })

        self.assertEqual(
            set(app.global_guards),
            {"script:mainline", "script:initialize"},
        )

    def test_script_global_actions_have_independent_rearm_locks(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = {"script:mainline"}
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)

        with patch("macroflow.ui.app.resolve_path", side_effect=[Path("images/mainline.png"), Path("images/init.png")]):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "mainline",
                "template": "images/mainline.png",
            })
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "initialize",
                "template": "images/init.png",
            })

        self.assertTrue(app.global_guards["script:mainline"]["awaiting_clear"])
        self.assertFalse(app.global_guards["script:initialize"]["awaiting_clear"])

    def test_script_scope_enables_all_globals_before_playback_start_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._activate_global_detect_from_config = Mock()
        actions = [
            {"type": "global_detect", "action_id": "before", "template": "images/a.png"},
            {"type": "delay", "ms": 0, "action_id": "start"},
            {"type": "global_detect", "action_id": "after", "template": "images/b.png"},
        ]

        keys = app._enter_script_global_scope(actions)

        self.assertEqual(keys, ("script:before", "script:after"))
        self.assertEqual(
            [call.args[0]["action_id"] for call in app._activate_global_detect_from_config.call_args_list],
            ["before", "after"],
        )

    def test_scope_exit_removes_only_script_guards(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {
            "script:one": {"key": "script:one"},
            "workflow:one": {"key": "workflow:one"},
        }
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = {"script:one", "workflow:one"}

        app._exit_script_global_scope(("script:one",))

        self.assertNotIn("script:one", app.global_guards)
        self.assertIn("workflow:one", app.global_guards)
        # 离开作用域同时丢弃该脚本守卫的重新武装锁。
        self.assertEqual(app.global_detect_rearm_locks, {"workflow:one"})

    def test_activate_global_detect_region_mode_parsing(self):
        # 旧配置没有 region_mode：无区域 → 全屏；有区域 → 自定义区域。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "action_id": "global-a", "template": "images/g.png"},
            )
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["region_mode"], "screen")
        self.assertIsNone(guard["region"])
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "region": [100, 50, 300, 200],
            })
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["region_mode"], "custom")
        self.assertEqual(guard["region"], (100, 50, 300, 200))
        # 显式 window 模式：记录模式，region 无意义。
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "region_mode": "window",
            })
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["region_mode"], "window")

    def test_activate_global_detect_template_mode_reads_registered_region(self):
        # v1.78：region_mode="template" 时区域运行时从模板登记表读取。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")), \
             patch("macroflow.ui.app.registered_template_region", return_value=[100, 50, 300, 200]):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "global-a", "template": "images/g.png",
                "region_mode": "template",
            })
        guard = app.global_guards["script:global-a"]
        self.assertEqual(guard["region_mode"], "template")
        self.assertEqual(guard["region"], (100, 50, 300, 200))
        self.assertTrue(any("区域 模板区域" in text for text in logs))

    def test_activate_global_detect_template_without_region_uses_fullscreen(self):
        # 模板未登记 / 未设置区域：按全屏检测并在日志中告警。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")), \
             patch("macroflow.ui.app.registered_template_region", return_value=None):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "global-a", "template": "images/g.png",
                "region_mode": "template",
            })
        guard = app.global_guards["script:global-a"]
        self.assertIsNone(guard["region"])
        self.assertTrue(any("模板未设置区域，按全屏检测" in text for text in logs))

    def test_trigger_summary_template_mode_shows_template_region(self):
        # v1.78：引用模板的触发条件摘要显示"区域：模板"，不展开坐标。
        app = MacroFlowApp.__new__(MacroFlowApp)
        summary = app._trigger_summary({
            "template": "images/g.png", "region_mode": "template",
            "region": [], "hold_ms": 1500, "hold_enabled": True,
        })
        self.assertIn("g.png", summary)
        self.assertIn("区域：模板", summary)
        self.assertNotIn("0,0,0,0", summary)
        self.assertIn("持续超过 1500 ms", summary)

    def test_guard_window_mode_uses_bound_window_rect(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = self._make_guard_app()
            guard = self._make_guard(template_path, region_mode="window")
            match = {"x": 1, "y": 2, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 16, "center_y": 22}
            with patch("macroflow.ui.app.find_template", return_value=match) as find, \
                 patch("macroflow.ui.app.get_window_rect", return_value=(1, 2, 300, 200)), \
                 patch("macroflow.ui.app.show_overlay"):
                hit = app._evaluate_one_guard(guard, None, None, time.perf_counter())
            self.assertIsNotNone(hit)
            app._bound_hwnd.assert_called()
            # 每轮用目标窗口当前区域作为识别区域。
            find.assert_called_once()
            self.assertEqual(find.call_args.args[2], (1, 2, 300, 200))

    def test_activate_registers_guard_without_thread(self):
        # 注册只写守卫数据，不启动任何后台线程；守卫没有 thread/stop 字段。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_guards = {}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module = {"kind": "global_module", "script": "m.json", "step_id": "m1"}
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "template": "images/g.png",
            }, module)
        self.assertIn("workflow:m1", app.global_guards)
        workflow_guard = app.global_guards["workflow:m1"]
        self.assertNotIn("thread", workflow_guard)
        self.assertNotIn("stop", workflow_guard)
        with patch("macroflow.ui.app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "row-g", "template": "images/g.png",
            })
        self.assertIn("script:row-g", app.global_guards)
        script_guard = app.global_guards["script:row-g"]
        self.assertNotIn("thread", script_guard)
        self.assertNotIn("stop", script_guard)

    def test_evaluate_global_guards_hold_then_trigger(self):
        # 上升沿语义：首次评估只置 was_detected/match_since，达到 hold 时长后
        # 第二次评估返回 hit，且守卫进入 awaiting_clear。
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "g.png"
            template.write_bytes(b"x")
            app = self._make_guard_app()
            guard = self._make_guard(template, hold_ms=1000)
            app.global_guards[guard["key"]] = guard
            match = {"x": 10, "y": 20, "width": 30, "height": 40,
                     "center_x": 25, "center_y": 40, "score": 0.9}
            screen = np.zeros((60, 80, 3), dtype=np.uint8)
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (-20, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=match), \
                 patch("macroflow.ui.app.show_overlay"):
                # 第一次评估：识别到但未到持续时长 → 无 hit。
                self.assertIsNone(app._evaluate_global_guards())
                self.assertTrue(guard["was_detected"])
                self.assertIsNotNone(guard["match_since"])
                self.assertFalse(guard["awaiting_clear"])
                # 拨快 match_since 到超过 hold，第二次评估触发。
                guard["match_since"] = time.perf_counter() - 2.0
                guard["last_check_time"] = 0.0
                hit = app._evaluate_global_guards()
            self.assertIsNotNone(hit)
            self.assertEqual(hit["kind"], "success")
            self.assertTrue(guard["triggered"])
            self.assertTrue(guard["awaiting_clear"])
            self.assertIn(guard["key"], app.global_detect_rearm_locks)

    def test_evaluate_global_guards_awaiting_clear_blocks_retrigger(self):
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "g.png"
            template.write_bytes(b"x")
            app = self._make_guard_app()
            guard = self._make_guard(
                template, triggered=True, awaiting_clear=True, awaiting_clear_logged=False,
            )
            app.global_guards[guard["key"]] = guard
            app.global_detect_rearm_locks = {guard["key"]}
            match = {"x": 10, "y": 20, "width": 30, "height": 40,
                     "center_x": 25, "center_y": 40, "score": 0.9}
            screen = np.zeros((60, 80, 3), dtype=np.uint8)
            # 目标仍在：awaiting_clear 阻止再次触发。
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (-20, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=match), \
                 patch("macroflow.ui.app.show_overlay"):
                self.assertIsNone(app._evaluate_global_guards())
            self.assertTrue(guard["awaiting_clear"])
            self.assertIn(guard["key"], app.global_detect_rearm_locks)
            # 目标消失：解除 awaiting_clear 并重新武装，允许下次触发。
            guard["last_check_time"] = 0.0
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (-20, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=None), \
                 patch("macroflow.ui.app.show_overlay"):
                self.assertIsNone(app._evaluate_global_guards())
            self.assertFalse(guard["awaiting_clear"])
            self.assertNotIn(guard["key"], app.global_detect_rearm_locks)

    def test_evaluate_skips_when_player_stopped(self):
        app = self._make_guard_app()
        app.player.stop_event.set()
        guard = self._make_guard("images/g.png")
        app.global_guards[guard["key"]] = guard
        with patch("macroflow.ui.app.capture_bgr") as capture, \
             patch("macroflow.ui.app.find_template_in_image"), \
             patch("macroflow.ui.app.show_overlay"):
            self.assertIsNone(app._evaluate_global_guards())
        capture.assert_not_called()

    def test_guard_disabled_hold_delay_triggers_on_first_match(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = self._make_guard_app()
            logs = []
            app._log = Mock(side_effect=logs.append)
            guard = self._make_guard(
                template_path, hold_ms=60000, hold_enabled=False,
            )
            app.global_guards[guard["key"]] = guard
            match = {
                "x": 10, "y": 20, "width": 30, "height": 40, "score": 0.9,
                "center_x": 25, "center_y": 40,
            }
            screen = np.zeros((60, 80, 3), dtype=np.uint8)
            with patch("macroflow.ui.app.capture_bgr", return_value=(screen, (-20, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=match), \
                 patch("macroflow.ui.app.show_overlay"):
                hit = app._evaluate_global_guards()
            self.assertIsNotNone(hit)
            self.assertTrue(guard["awaiting_clear"])
            self.assertTrue(any("立即触发" in text for text in logs))

    def test_guard_module_ref_reads_object_each_round(self):
        # 引用模块守卫：每轮评估实时重读对象（阈值/区域/持续时长），
        # 修改对象即时生效。
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = self._make_guard_app()
            guard = self._make_guard(
                template_path, module_ref=True, module_key="module:g",
                interval_ms=500, threshold=0.85, hold_ms=1000,
            )
            app.global_guards[guard["key"]] = guard
            obj = {
                "category": "switch", "region": [1, 2, 30, 40],
                "threshold": 0.9, "interval_ms": 300, "blocking": False,
                "hold_ms": 2000, "hold_enabled": True,
                "delay_ms": 0, "after_action": "click_match",
            }
            match = {"x": 1, "y": 2, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 16, "center_y": 22}
            screen = np.zeros((60, 80, 3), dtype=np.uint8)
            with patch("macroflow.ui.app.registered_module_object", return_value=obj) as obj_lookup, \
                 patch("macroflow.ui.app.capture_bgr", return_value=(screen, (-20, 0))), \
                 patch("macroflow.ui.app.find_template_in_image", return_value=match) as find, \
                 patch("macroflow.ui.app.show_overlay"):
                self.assertIsNone(app._evaluate_global_guards())
            obj_lookup.assert_called_once_with("module:g")
            # 阈值 / 区域来自对象，且 hold_ms 被对象值覆盖。
            self.assertEqual(find.call_args.args[2], 0.9)
            self.assertEqual(find.call_args.args[4], (1, 2, 30, 40))
            self.assertEqual(guard["hold_ms"], 2000)

    def test_guard_logs_missing_template_once(self):
        # 模板缺失是"加了没反应"的常见原因：日志提示一次，不每轮刷屏。
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.png"
            app = self._make_guard_app()
            logs = []
            app._log = Mock(side_effect=logs.append)
            guard = self._make_guard(missing)
            with patch("macroflow.ui.app.find_template_in_image") as find, \
                 patch("macroflow.ui.app.show_overlay"):
                detected, match = app._guard_image_detect(guard, None, None)
            self.assertFalse(detected)
            self.assertIsNone(match)
            find.assert_not_called()
            self.assertEqual(len(logs), 1)
            self.assertIn("模板图片不存在", logs[0])
            # 第二轮不再刷屏。
            with patch("macroflow.ui.app.find_template_in_image") as find2, \
                 patch("macroflow.ui.app.show_overlay"):
                detected, match = app._guard_image_detect(guard, None, None)
            self.assertEqual(len(logs), 1)
            find2.assert_not_called()

    def test_on_restart_workflow_request_standalone_returns_false(self):
        # 独立脚本（非工作流）：没有当前工作流，固定特殊动作被跳过。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = None
        app.workflow_restart_requested = False
        app.player = Mock()
        app.workflow_stop = threading.Event()
        app._ui = Mock()
        app._log = Mock()
        result = app._on_restart_workflow_request({"type": "restart_workflow"})
        self.assertFalse(result)
        self.assertFalse(app.workflow_restart_requested)
        app.player.stop.assert_not_called()
        app._ui.assert_called_once()

    def test_on_restart_workflow_request_workflow_restarts(self):
        # 工作流中：置标志、解析动作级跳转行、停播放并清空守卫，再轮询等 worker 死后重启。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = 0
        app.workflow_restart_requested = False
        app.workflow = Workflow(steps=[{"script": "a.json", "step_id": "row-a"}])
        app.player = Mock()
        app.workflow_stop = threading.Event()
        app.worker = None
        scheduled = []
        app._ui = lambda callback, *args: scheduled.append(callback)
        app._clear_global_guards = Mock()
        app._launch_workflow_restart = Mock()
        result = app._on_restart_workflow_request({
            "type": "restart_workflow",
            "restart_workflow_target_row": 3,
        })
        self.assertTrue(result)
        self.assertTrue(app.workflow_restart_requested)
        self.assertEqual(app.workflow_restart_target_row, 3)
        self.assertTrue(app.workflow_stop.is_set())
        app.player.stop.assert_called_once()
        app._clear_global_guards.assert_called_once()
        # 主线程轮询：worker 已死 → 立即重启工作流。
        app._poll_workflow_stop_for_restart_workflow()
        app._launch_workflow_restart.assert_called_once()

    def test_poll_workflow_stop_for_restart_waits_for_worker(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app.workflow_restart_requested = True
        app._launch_workflow_restart = Mock()
        app.root = Mock()
        app._poll_workflow_stop_for_restart_workflow()
        app._launch_workflow_restart.assert_not_called()
        app.root.after.assert_called_once()

    def test_poll_workflow_stop_for_restart_cancelled_by_f12(self):
        # F12 紧急停止已把 workflow_restart_requested 清 False：残留的轮询
        # 不得再拉起工作流（否则会带着已清理的重启状态执行）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.worker = None
        app.workflow_restart_requested = False
        app._launch_workflow_restart = Mock()
        app.root = Mock()
        app._poll_workflow_stop_for_restart_workflow()
        app._launch_workflow_restart.assert_not_called()
        app.root.after.assert_not_called()

    def test_launch_workflow_restart_preserves_current_repeats(self):
        # 重启工作流只能沿用当前剩余次数，不能恢复到本轮初始快照。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"kind": "script", "script": "a.json", "repeats": 0, "unlimited": False},
            {"kind": "script", "script": "b.json", "repeats": 5, "unlimited": False},
        ])
        app.workflow_restart_requested = True
        app.workflow_repeats_snapshot = {0: (2, False), 1: (3, True)}
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app.run_workflow = Mock()
        app._launch_workflow_restart()
        steps = app._workflow_only_steps()
        self.assertEqual(steps[0]["repeats"], 0)
        self.assertEqual(steps[1]["repeats"], 5)
        self.assertFalse(steps[1]["unlimited"])
        self.assertFalse(app.workflow_restart_requested)
        app.run_workflow.assert_called_once_with(
            start_index=0, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True, suppress_start_sound=True,
        )

    def test_launch_workflow_restart_uses_configured_row_object(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"kind": "script", "script": "a.json", "step_id": "row-a"},
            {"kind": "script", "script": "b.json", "step_id": "row-b"},
        ])
        app.workflow_restart_requested = True
        app.workflow_restart_target_row = 2
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app.run_workflow = Mock()

        app._launch_workflow_restart()

        app.run_workflow.assert_called_once_with(
            start_index=1, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True, suppress_start_sound=True,
        )

    def test_launch_workflow_restart_clamps_row_beyond_workflow_length(self):
        # 跳转行越界时收敛到工作流最后一行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"kind": "script", "script": "a.json"},
            {"kind": "script", "script": "b.json"},
        ])
        app.workflow_restart_requested = True
        app.workflow_restart_target_row = 99
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app.run_workflow = Mock()

        app._launch_workflow_restart()

        app.run_workflow.assert_called_once_with(
            start_index=1, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True, suppress_start_sound=True,
        )
        # 重启完成后目标行复位，避免影响下一次触发。
        self.assertEqual(app.workflow_restart_target_row, 1)

    def test_workflow_module_enabled_follows_registry_state(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {"kind": "module", "action": {"module_key": "module:test"}}
        with patch("macroflow.ui.app.registered_module_object", return_value={"enabled": False}):
            self.assertFalse(app._workflow_module_enabled(step))
        with patch("macroflow.ui.app.registered_module_object", return_value={"enabled": True}):
            self.assertTrue(app._workflow_module_enabled(step))

    def test_record_workflow_repeat_stores_index(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_repeat_index = 0
        app._set_execution_progress = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("macroflow.ui.app.workflow_execution_progress", return_value="p"):
            app._record_workflow_repeat(3, 5, 1, 2, "脚本")
        self.assertEqual(app.current_workflow_repeat_index, 2)
        app._set_execution_progress.assert_called_once_with("p")

    def test_ensure_workflow_step_ids_assigns_ids(self):
        steps = [
            {"script": "a.json"},
            {"kind": "global_module", "config": {"template": "x.png"}},
            {"script": "b.json"},
        ]
        self.assertTrue(ensure_workflow_step_ids(steps))
        self.assertTrue(steps[0].get("step_id"))
        self.assertTrue(steps[1].get("step_id"))
        first_ids = [step["step_id"] for step in steps]
        self.assertFalse(ensure_workflow_step_ids(steps))
        self.assertEqual([step["step_id"] for step in steps], first_ids)

    def test_append_global_module_always_goes_to_row_one(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a"}, {"script": "b"}])
        app.workflow_tree = Mock()
        app.workflow_tree.selection.return_value = ("1",)
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._append_global_module(config={"template": "x.png"})
        modules = app._global_module_steps()
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["config"]["template"], "x.png")
        self.assertEqual([step["script"] for step in app._workflow_only_steps()], ["a", "b"])

    def test_workflow_worker_activates_global_module(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app.current_workflow_step_index = None
        app._activate_global_detect_from_config = Mock()

        def fake_ui(callback, *args):
            callback(*args)

        app._ui = fake_ui
        module = {
            "kind": "global_module", "script": "", "enabled": True,
            "config": {"template": "x.png"},
        }
        app._run_workflow_worker(
            [], None, None, False, global_modules=[module],
        )
        app._activate_global_detect_from_config.assert_called_once_with(
            {"template": "x.png"}, module,
        )
        # 新设计：全局模块在开始时只开启检测，不执行脚本。
        app.player.play.assert_not_called()

    def test_pure_global_module_workflow_dwells_until_stopped(self):
        # 纯全局模块工作流（无脚本步骤）：守卫必须持续评估直到 F12，
        # 否则注册后立即“执行完成”、检测永不生效（v1.0 常驻监控行为）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.player.handle_guard_hit = Mock()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app.current_workflow_step_index = None
        app._ui = lambda callback, *args: callback(*args)
        app.global_guards = {"m1": {"key": "m1"}}
        app.guards_lock = threading.Lock()
        app.global_detect_rearm_locks = set()
        app.global_detect_trigger_count = 0
        app._evaluating_guards = False
        app.exiting = False
        app._restore_workflow_scan_foreground = Mock()
        calls = {"n": 0}

        def evaluate():
            calls["n"] += 1
            if calls["n"] >= 3:
                app.workflow_stop.set()
            return None

        app._evaluate_global_guards = Mock(side_effect=evaluate)

        app._run_workflow_worker([], None, None, False)

        self.assertGreaterEqual(calls["n"], 3)
        self.assertTrue(any(
            "持续运行全局检测" in call.args[0] for call in app._log.call_args_list
        ))
        self.assertFalse(any(
            "工作流执行完成" in call.args[0] for call in app._log.call_args_list
        ))

    def test_workflow_interrupt_keeps_focus_dispatcher_alive_for_restart(self):
        # 特殊模块「重新执行工作流」：worker 收尾时保留输入锁给重启流程。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app.workflow_restart_requested = True

        app._run_workflow_worker([], None, None, False)

        app._leave_focus_mode.assert_not_called()
        app._finish_execution_visibility.assert_not_called()

    def test_workflow_resume_does_not_reenter_focus_mode(self):
        # 全局模块中断后的断点恢复（resume_action_index 非空）不再重复设置
        # 专注模式：输入法切换和系统输入锁只在工作流首次开始时执行一次。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app.current_workflow_step_index = None
        app._activate_global_detect_from_config = Mock()
        app._ui = lambda callback, *args: callback(*args)

        app._run_workflow_worker([], None, None, False, resume_action_index=2)

        app._enter_focus_mode.assert_not_called()
        self.assertTrue(any("沿用已开启的强制专注模式" in call.args[0]
                            for call in app._log.call_args_list))

    def test_workflow_fresh_start_enters_focus_mode_once(self):
        # 工作流首次开始（resume_action_index 为空）：执行一次专注模式设置。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app.current_workflow_step_index = None
        app._activate_global_detect_from_config = Mock()
        app._ui = lambda callback, *args: callback(*args)

        app._run_workflow_worker([], None, None, focus_enabled=True)

        app._enter_focus_mode.assert_called_once_with(None, True)

    def test_workflow_worker_skips_disabled_global_module(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app.current_workflow_step_index = None
        app._activate_global_detect_from_config = Mock()

        def fake_ui(callback, *args):
            callback(*args)

        app._ui = fake_ui
        app._run_workflow_worker(
            [], None, None, False,
            global_modules=[{
                "kind": "global_module", "script": "", "enabled": False,
                "config": {"template": "x.png"},
            }],
        )
        app._activate_global_detect_from_config.assert_not_called()
        app.player.play.assert_not_called()

    def test_workflow_worker_skips_registry_disabled_global_module_without_waiting(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_stop = Mock()
        app.workflow_stop.is_set.return_value = False
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._set_status = Mock()
        app._set_execution_progress = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._handle_worker_error = Mock()
        app._finish_execution_visibility = Mock()
        app.current_workflow_step_index = None
        app._activate_global_detect_from_config = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module = {
            "kind": "global_module", "script": "", "enabled": True,
            "before_ms": 99999,
            "config": {
                "module_ref": True, "module_key": "module:disabled",
                "template": "images/disabled.png",
            },
        }

        with patch("macroflow.ui.app.registered_module_object", return_value={
            "name": "禁用检测", "enabled": False,
        }):
            app._run_workflow_worker(
                [], None, None, False, global_modules=[module],
            )

        app.workflow_stop.wait.assert_not_called()
        app._activate_global_detect_from_config.assert_not_called()
        self.assertTrue(any(
            "模块管理中已禁用" in call.args[0] for call in app._log.call_args_list
        ))

    def test_workflow_worker_extracts_config_from_module_script(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "m.json"
            actions = [
                {"type": "global_detect", "template": "images/检测图.png",
                 "hold_ms": 1500, "region": [10, 20, 30, 40]},
                {"type": "delay", "delay_ms": 100},
            ]
            save_script(MacroScript(name="m", actions=actions), script_path)
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app.current_workflow_step_index = None
            app._activate_global_detect_from_config = Mock()

            def fake_ui(callback, *args):
                callback(*args)

            app._ui = fake_ui
            module = {
                "kind": "global_module", "script": str(script_path),
                "enabled": True, "config": None,
            }
            with patch("macroflow.ui.app.resolve_path", return_value=script_path):
                app._run_workflow_worker(
                    [], None, None, False, global_modules=[module],
                )
            # 配置来自脚本 settings["trigger"]（迁移后不含 "type"）。
            app._activate_global_detect_from_config.assert_called_once_with(
                {"template": "images/检测图.png", "hold_ms": 1500, "region": [10, 20, 30, 40]},
                module,
            )
            app.player.play.assert_not_called()

    def test_tab_changed_refreshes_workflow_tree(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow_tree = Mock()
        app.notebook = Mock()
        app.notebook.index.return_value = 1
        app.rebuild_workflow_tree = Mock()
        app._on_tab_changed()
        app.rebuild_workflow_tree.assert_called_once()

        app.rebuild_workflow_tree.reset_mock()
        app.notebook.index.return_value = 0
        app._on_tab_changed()
        app.rebuild_workflow_tree.assert_not_called()

    def test_global_module_label_reads_script_config(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "g.json"
            actions = [
                {
                    "type": "global_detect", "template": "images/检测图.png",
                    "hold_ms": 1500, "hold_enabled": True, "region": [10, 20, 30, 40],
                },
            ]
            save_script(MacroScript(name="g", actions=actions), script_path)
            app = MacroFlowApp.__new__(MacroFlowApp)
            with patch("macroflow.ui.app.resolve_path", return_value=script_path), \
                 patch("macroflow.ui.app.load_script", return_value=MacroScript(name="g", actions=actions)):
                label = app._global_module_label({
                    "kind": "global_module", "script": "scripts/g.json",
                })
            self.assertIn("⇄ 引用脚本 · g · 检测图.png", label)
            self.assertIn("检测图.png", label)
            self.assertIn("10,20,30,40", label)
            self.assertIn("1500", label)
            self.assertIn("触发后执行模块步骤，再继续工作流", label)

    def test_screen_point_picker_restores_main_without_owner(self):
        picker = ScreenPointPicker.__new__(ScreenPointPicker)
        picker.owner = None
        picker.main = Mock()
        picker.main_previous_state = "normal"
        picker.overlay = None
        picker.screenshot = None
        picker.canvas = None
        picker.tip_id = None
        picker.first_point = None
        picker.close()
        picker.main.deiconify.assert_called_once()

    def test_screen_point_picker_keeps_ancestor_windows_mapped_while_hidden(self):
        owner = Mock()
        manager = Mock()
        root = Mock()
        root.attributes.return_value = 0.85
        picker = ScreenPointPicker(owner, manager, Mock(), hidden_windows=[root])
        picker.start()
        root.attributes.assert_any_call("-alpha", 0.0)
        root.withdraw.assert_not_called()
        picker.close()
        root.attributes.assert_any_call("-alpha", 0.85)
        root.deiconify.assert_not_called()

    def test_screen_region_picker_drag_reports_region(self):
        picker = ScreenRegionPicker.__new__(ScreenRegionPicker)
        picker.canvas = Mock()
        picker.rectangle_id = None
        picker.tip_id = 1
        picker.overlay = None
        picker.owner = None
        picker.main = Mock()
        picker.main_previous_state = "normal"
        picker.hidden_windows = []
        picker.hidden_states = []
        picker.on_result = Mock()
        picker._drag_begin(Mock(x=10, y=20, x_root=100, y_root=200))
        self.assertEqual(picker.drag_start, (100, 200, 10, 20))
        picker.canvas.create_rectangle.assert_called_once()
        picker._drag_finish(Mock(x=50, y=80, x_root=300, y_root=400))
        picker.on_result.assert_called_once_with([100, 200, 200, 200])
        picker.main.deiconify.assert_called()

    def test_screen_region_picker_runs_result_before_restoring_windows(self):
        # v1.79："截图新建…"的回调（截屏）必须在窗口恢复之前执行，
        # 否则截图会把本程序自己的窗口截进去。
        picker = ScreenRegionPicker.__new__(ScreenRegionPicker)
        picker.overlay = None
        picker.canvas = None
        picker.rectangle_id = None
        picker.tip_id = None
        picker.drag_start = (100, 200, 0, 0)
        picker.owner = None
        picker.main = Mock()
        picker.main_previous_state = "normal"
        picker.hidden_windows = []
        picker.hidden_states = []
        calls = []
        picker.on_result = Mock(side_effect=lambda region: calls.append("result"))
        picker._restore_windows = Mock(side_effect=lambda: calls.append("restore"))
        picker._drag_finish(Mock(x=150, y=280, x_root=300, y_root=400))
        self.assertEqual(calls, ["result", "restore"])

    def test_screen_offset_picker_drag_reports_start_and_end_points(self):
        picker = ScreenOffsetPicker.__new__(ScreenOffsetPicker)
        picker.canvas = Mock()
        picker.rectangle_id = None
        picker.tip_id = 1
        picker.overlay = None
        picker.owner = None
        picker.main = Mock()
        picker.main_previous_state = "normal"
        picker.hidden_windows = []
        picker.hidden_states = []
        picker.on_result = Mock()
        picker._drag_begin(Mock(x=10, y=20, x_root=400, y_root=300))
        picker.canvas.create_line.assert_called_once()
        picker._drag_move(Mock(x=55, y=5, x_root=445, y_root=285))
        picker._drag_finish(Mock(x=55, y=5, x_root=445, y_root=285))
        picker.on_result.assert_called_once_with(400, 300, 445, 285)
        picker.main.deiconify.assert_called()

    def test_screen_region_picker_hides_whole_window_chain(self):
        # v1.79：框选 / 截图时隐藏从表单到主窗口的整条窗口链，避免遮挡屏幕。
        owner = Mock()
        manager = Mock()
        manager.state.return_value = "normal"
        root = Mock()
        root.state.return_value = "normal"
        picker = ScreenRegionPicker(owner, manager, Mock(), hidden_windows=[root])
        picker.start()
        root.withdraw.assert_called_once()
        manager.withdraw.assert_called_once()
        owner.withdraw.assert_called_once()
        manager.after.assert_called_once()

    def test_screen_region_picker_restores_hidden_chain_on_close(self):
        # 取消（Esc / 无效框选）时窗口链一起恢复，且不丢失最大化状态。
        owner = Mock()
        manager = Mock()
        manager.state.return_value = "zoomed"
        root = Mock()
        root.state.return_value = "normal"
        picker = ScreenRegionPicker(owner, manager, Mock(), hidden_windows=[root])
        picker.start()
        picker.close()
        root.deiconify.assert_called_once()
        manager.deiconify.assert_called()
        owner.deiconify.assert_called()
        manager.state.assert_called_with("zoomed")
        # root 恢复前是 normal：不重置其最大化状态（只记录过无参的读取调用）。
        self.assertEqual([call.args for call in root.state.call_args_list], [()])

    def test_global_detect_dialog_saves_action(self):
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.restart_delay = Mock()
        dialog.restart_delay.get.return_value = "200"
        dialog.region = Mock()
        dialog.region.get.return_value = "100,50,300,200"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "custom"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "640,360"
        dialog.require_click = True
        dialog.destroy = Mock()
        dialog.save()
        result = dialog.result
        self.assertEqual(result["type"], "global_detect")
        self.assertEqual(result["template"], "images/g.png")
        self.assertAlmostEqual(result["threshold"], 0.9)
        self.assertEqual(result["region"], [100, 50, 300, 200])
        self.assertEqual(result["region_mode"], "custom")
        self.assertEqual(result["click_point"], [640, 360])
        self.assertEqual(result["restart_delay_ms"], 200)
        self.assertNotIn("jump_row", result)
        self.assertNotIn("jump_enabled", result)
        self.assertNotIn("jump_step_id", result)
        dialog.destroy.assert_called_once()

    def test_global_detect_module_selection_copies_bound_region(self):
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.module_key = Mock()
        dialog.template = Mock()
        dialog.region_mode = Mock()
        dialog.region = Mock()
        dialog.template_combo = Mock()
        dialog.module_name = Mock()
        with patch("macroflow.ui.dialogs.choose_module_binding", return_value={
            "module_ref": True, "module_key": "module:first",
            "template": "images/shared.png", "region_mode": "template",
            "region": [11, 22, 333, 444],
        }) as choose:
            dialog.select_image_module()

        choose.assert_called_once_with(
            dialog, categories=("switch", "workflow_global", "script_global"),
        )
        dialog.module_key.set.assert_called_once_with("module:first")
        dialog.template.set.assert_called_once_with("images/shared.png")
        dialog.region_mode.set.assert_called_once_with("template")
        dialog.region.set.assert_called_once_with("11,22,333,444")

    def test_global_detect_saves_selected_module_identity_and_region(self):
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.module_key = Mock(**{"get.return_value": "module:first"})
        dialog.template = Mock(**{"get.return_value": "images/stale.png"})
        dialog.threshold = Mock(**{"get.return_value": "0.9"})
        dialog.interval = Mock(**{"get.return_value": "500"})
        dialog.hold = Mock(**{"get.return_value": "1000"})
        dialog.region = Mock(**{"get.return_value": "1,2,3,4"})
        dialog.region_mode = Mock(**{"get.return_value": "custom"})
        dialog.click_point = Mock(**{"get.return_value": "640,360"})
        dialog.restart_delay = Mock(**{"get.return_value": "200"})
        dialog.require_click = True
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={
            "category": "workflow_global", "template": "images/shared.png",
            "region": [11, 22, 333, 444],
        }):
            dialog.save()

        self.assertEqual(dialog.result["module_key"], "module:first")
        self.assertTrue(dialog.result["module_ref"])
        self.assertEqual(dialog.result["template"], "images/shared.png")
        self.assertEqual(dialog.result["region"], [11, 22, 333, 444])
        self.assertEqual(dialog.result["region_mode"], "template")

    def test_global_detect_dialog_saves_template_region_mode(self):
        # v1.78：模板已登记 → 动作只引用模板，区域运行时从登记表读取。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region_mode = Mock()
        dialog.require_click = True
        dialog.restart_delay = Mock()
        dialog.restart_delay.get.return_value = "200"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "640,360"
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.load_template_regions", return_value={
            "images/g.png": [100, 50, 300, 200],
        }):
            dialog.save()
        result = dialog.result
        self.assertEqual(result["template"], "images/g.png")
        self.assertEqual(result["region_mode"], "template")
        self.assertEqual(result["region"], [])
        dialog.destroy.assert_called_once()

    def test_global_detect_dialog_keeps_legacy_region_when_template_not_registered(self):
        # 编辑旧动作且模板不在登记表（旧模板被删除）：保留原有区域配置。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = "100,50,300,200"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "custom"
        dialog.require_click = True
        dialog.restart_delay = Mock()
        dialog.restart_delay.get.return_value = "200"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "640,360"
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.load_template_regions", return_value={}):
            dialog.save()
        result = dialog.result
        self.assertEqual(result["region_mode"], "custom")
        self.assertEqual(result["region"], [100, 50, 300, 200])
        dialog.destroy.assert_called_once()

    def test_global_detect_dialog_trigger_mode_saves_without_click(self):
        # 触发条件模式（require_click=False）：不写点击位置与点击后延时。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.require_click = False
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = "100,50,300,200"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "custom"
        dialog.destroy = Mock()
        dialog.save()
        result = dialog.result
        self.assertEqual(result["template"], "images/g.png")
        self.assertEqual(result["region"], [100, 50, 300, 200])
        self.assertIsNone(result["click_point"])
        self.assertEqual(result["restart_delay_ms"], 0)
        dialog.destroy.assert_called_once()

    def test_global_detect_dialog_jump_mode_saves_row_object(self):
        # 普通脚本内嵌全局模块行（jump=True）：保存跳转行号和动作标识，不写点击位置。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.require_click = False
        dialog.jump = True
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = ""
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.jump_target_ids = {
            "第 3 行 · 键盘 · A": "target-a",
            "第 5 行 · 延时": "target-b",
        }
        dialog.jump_row_numbers = {"第 3 行 · 键盘 · A": 3, "第 5 行 · 延时": 5}
        dialog.jump_row = Mock()
        dialog.jump_row.get.return_value = "第 5 行 · 延时"
        dialog.destroy = Mock()
        dialog.save()
        result = dialog.result
        self.assertEqual(result["template"], "images/g.png")
        self.assertEqual(result["jump_row"], 5)
        self.assertEqual(result["jump_action_id"], "target-b")
        self.assertIsNone(result["click_point"])
        self.assertEqual(result["restart_delay_ms"], 0)

    def test_global_detect_dialog_saves_script_end_target(self):
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.require_click = False
        dialog.jump = True
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = ""
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.jump_target_ids = {
            "脚本结束（工作流中执行下一项）": NEXT_WORKFLOW_STEP_TARGET_ID,
        }
        dialog.jump_row_numbers = {"脚本结束（工作流中执行下一项）": 4}
        dialog.jump_row = Mock()
        dialog.jump_row.get.return_value = "脚本结束（工作流中执行下一项）"
        dialog.destroy = Mock()

        dialog.save()

        self.assertEqual(dialog.result["jump_row"], 4)
        self.assertEqual(
            dialog.result["jump_action_id"], NEXT_WORKFLOW_STEP_TARGET_ID,
        )

    def test_global_detect_dialog_jump_disabled_keeps_target_but_flags_off(self):
        # 取消勾选“启用触发后跳转”：保留目标配置（便于重新启用），
        # 但写入 jump_enabled=False 供运行时与显示判断。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.require_click = False
        dialog.jump = True
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = ""
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.jump_target_ids = {
            "第 3 行 · 键盘 · A": "target-a",
            "第 5 行 · 延时": "target-b",
        }
        dialog.jump_row_numbers = {"第 3 行 · 键盘 · A": 3, "第 5 行 · 延时": 5}
        dialog.jump_row = Mock()
        dialog.jump_row.get.return_value = "第 5 行 · 延时"
        dialog.jump_enabled_var = _FakeBooleanVar(False)
        dialog.destroy = Mock()
        dialog.save()
        result = dialog.result
        self.assertFalse(result["jump_enabled"])
        self.assertEqual(result["jump_row"], 5)
        self.assertEqual(result["jump_action_id"], "target-b")

    def test_global_detect_dialog_jump_defaults_to_disabled(self):
        # 默认不勾选“启用触发后跳转”：未配置 jump_enabled 时按停用处理。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.require_click = False
        dialog.jump = True
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = ""
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.jump_target_ids = {"第 5 行 · 延时": "target-b"}
        dialog.jump_row_numbers = {"第 5 行 · 延时": 5}
        dialog.jump_row = Mock()
        dialog.jump_row.get.return_value = "第 5 行 · 延时"
        dialog.destroy = Mock()
        dialog.save()
        self.assertFalse(dialog.result["jump_enabled"])

    def test_global_detect_dialog_jump_mode_falls_back_to_spinbox(self):
        # 脚本没有可跳转的行时退回数字行号输入：只保存 jump_row。
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.require_click = False
        dialog.jump = True
        dialog.template = Mock()
        dialog.template.get.return_value = "images/g.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.9"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "500"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "1000"
        dialog.region = Mock()
        dialog.region.get.return_value = ""
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.jump_target_ids = {}
        dialog.jump_row = Mock()
        dialog.jump_row.get.return_value = "5"
        dialog.destroy = Mock()
        dialog.save()
        result = dialog.result
        self.assertEqual(result["jump_row"], 5)
        self.assertNotIn("jump_action_id", result)
        self.assertIsNone(result["click_point"])

    def test_select_jump_target_label_prefers_stable_id(self):
        options = [("第 1 行 · 延时", "a"), ("第 2 行 · 键盘 · B", "b")]
        # 行移动后旧行号失效，但动作标识仍能解析到当前行。
        self.assertEqual(
            select_jump_target_label("b", 1, options), "第 2 行 · 键盘 · B",
        )
        # 无标识时按保存的行号。
        self.assertEqual(select_jump_target_label("", 1, options), "第 1 行 · 延时")
        # 行号越界时选第一行兜底。
        self.assertEqual(select_jump_target_label("", 99, options), "第 1 行 · 延时")
        self.assertEqual(select_jump_target_label("", 0, []), "")

    def test_global_detect_dialog_requires_template(self):
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = ""
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            dialog.save()
        notice.assert_called_once()

    def test_script_directories_dialog_saves_paths(self):
        dialog = ScriptDirectoriesDialog.__new__(ScriptDirectoriesDialog)
        dialog.level_dir = Mock()
        dialog.level_dir.get.return_value = "scripts/关卡"
        dialog.level_pack_dir = Mock()
        dialog.level_pack_dir.get.return_value = "scripts/关卡封装"
        dialog.switch_dir = Mock()
        dialog.switch_dir.get.return_value = "D:/switch"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "level_dir": "scripts/关卡",
            "level_pack_dir": "scripts/关卡封装",
            "switch_dir": "D:/switch",
        })
        dialog.destroy.assert_called_once()

    def test_script_dir_helpers_use_configurable_paths(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.level_scripts_dir_var = Mock()
        app.level_scripts_dir_var.get.return_value = "my_level"
        app.level_pack_scripts_dir_var = Mock()
        app.level_pack_scripts_dir_var.get.return_value = "my_level_pack"
        app.switch_scripts_dir_var = Mock()
        app.switch_scripts_dir_var.get.return_value = "my_switch"
        self.assertEqual(app._level_scripts_dir(), BASE_DIR / "my_level")
        self.assertEqual(app._level_pack_scripts_dir(), BASE_DIR / "my_level_pack")
        self.assertEqual(app._switch_scripts_dir(), BASE_DIR / "my_switch")

    def test_script_category_key_and_dir_routing(self):
        from macroflow.ui.app import SCRIPT_CATEGORY_VALUES, script_category_key
        self.assertEqual(SCRIPT_CATEGORY_VALUES, ("关卡", "关卡封装", "切换"))
        self.assertEqual(script_category_key("关卡"), "level")
        self.assertEqual(script_category_key("关卡封装"), "level_pack")
        self.assertEqual(script_category_key("切换"), "switch")
        self.assertEqual(script_category_key("工作流全局"), "level")
        self.assertEqual(script_category_key("脚本全局"), "level")

        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script_category_var = Mock()
        app._level_pack_scripts_dir = Mock(return_value=Path("lp"))
        app._switch_scripts_dir = Mock(return_value=Path("s"))
        app._level_scripts_dir = Mock(return_value=Path("l"))
        app.script_category_var.get.return_value = "切换"
        self.assertEqual(app._script_category_dir(), Path("s"))
        app.script_category_var.get.return_value = "关卡封装"
        self.assertEqual(app._script_category_dir(), Path("lp"))
        app.script_category_var.get.return_value = "关卡"
        self.assertEqual(app._script_category_dir(), Path("l"))

    def test_toggle_locked_spinbox_edit_and_save(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        spin = Mock()
        button = Mock()
        on_save = Mock()
        button.cget.return_value = "修改"
        app._toggle_locked_spinbox(spin, button, on_save)
        button.configure.assert_called_with(text="保存")
        spin.configure.assert_called_with(state="normal")
        spin.focus_set.assert_called_once()
        on_save.assert_not_called()

        button.cget.return_value = "保存"
        app._toggle_locked_spinbox(spin, button, on_save)
        button.configure.assert_called_with(text="修改")
        spin.configure.assert_called_with(state="disabled")
        on_save.assert_called_once()


class RecordingDisplayTests(unittest.TestCase):
    def test_floating_notice_positions_cover_all_six_choices(self):
        self.assertEqual(floating_notice_xy("左上", 1920, 1080), (18, 18))
        self.assertEqual(floating_notice_xy("顶部居中", 1920, 1080), (780, 18))
        self.assertEqual(floating_notice_xy("右上", 1920, 1080), (1542, 18))
        self.assertEqual(floating_notice_xy("左下", 1920, 1080), (18, 994))
        self.assertEqual(floating_notice_xy("底部居中", 1920, 1080), (780, 994))
        self.assertEqual(floating_notice_xy("右下", 1920, 1080), (1542, 994))

    def test_successful_execution_always_restores_main_from_tray(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.execution_progress_text = "running"
        app.main_hidden_for_execution = True
        app.root = Mock()
        app._hide_execution_mini = Mock()
        app._restore_main_window = Mock()
        app._finish_execution_visibility()
        app._restore_main_window.assert_called_once()
        self.assertFalse(app.main_hidden_for_execution)

    def test_worker_error_logs_mini_cleanup_before_hiding_it(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        events = []
        app._set_status = Mock()
        app._append_mini_step = Mock()
        app._sound = Mock()
        app._notify = Mock()
        app._clear_global_guards = Mock()
        app._log = Mock(side_effect=lambda text: events.append(("log", text)))
        app._finish_execution_visibility = Mock(
            side_effect=lambda: events.append(("finish", "")),
        )

        app._handle_worker_error("工作流执行失败", RuntimeError("绑定窗口已关闭"))

        self.assertEqual(events, [
            ("log", "工作流执行失败：绑定窗口已关闭"),
            ("log", "执行异常收尾：即将关闭执行小窗并恢复主界面。"),
            ("finish", ""),
        ])
        app._clear_global_guards.assert_called_once()

    def test_repeated_notice_reuses_single_window_and_restarts_timer(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        window = Mock()
        window.winfo_exists.return_value = True
        window.winfo_screenwidth.return_value = 1920
        window.winfo_screenheight.return_value = 1080
        window.after.return_value = "new-timer"
        app.execution_notice_window = window
        app.execution_notice_label = Mock()
        app.execution_notice_after_id = "old-timer"

        app._show_execution_notice("新的提醒内容", 4500)

        app.execution_notice_label.configure.assert_called_once_with(text="新的提醒内容")
        window.after_cancel.assert_called_once_with("old-timer")
        window.after.assert_called_once()
        self.assertEqual(app.execution_notice_after_id, "new-timer")

    def test_notice_action_has_clear_summary(self):
        notice = action_summary({
            "type": "notice", "text": "体力即将用完", "duration_ms": 3000, "delay_ms": 0,
        })
        self.assertIn("浮动提醒", notice[0])
        self.assertIn("3000 ms", notice[1])

    def test_coordinate_scale_status_is_useful(self):
        self.assertEqual(
            coordinate_scale_summary(
                {"width": 1920, "height": 1080},
                {"width": 1920, "height": 1080},
            ),
            "坐标缩放  1920×1080 → 1920×1080 （1:1）",
        )
        self.assertIn("自动缩放", coordinate_scale_summary(
            {"width": 1920, "height": 1080},
            {"width": 2560, "height": 1440},
        ))

    def test_action_column_uses_compact_icons(self):
        cases = (
            ({"type": "delay", "ms": 100}, "◷  延时"),
            ({"type": "key", "name": "A", "down": True}, "⌨  键盘"),
            ({"type": "text", "text": "hello"}, "T  文本"),
            ({"type": "mouse_move", "mode": "absolute", "x": 1, "y": 2}, "↖  移动"),
            ({"type": "mouse_button", "button": "left", "down": True}, "◉  点击"),
            ({"type": "repeat_click", "x": 1, "y": 2}, "↻  连续点击"),
            ({"type": "scroll", "dx": 0, "dy": 1}, "↕  滚轮"),
            ({"type": "image_match", "template": "a.png"}, "▣  识图"),
        )
        for action, expected in cases:
            with self.subTest(action=action):
                self.assertEqual(action_summary(action)[0], expected)

    def test_absolute_move_shows_destination_and_mode(self):
        text = recorded_action_description({
            "type": "mouse_move", "mode": "absolute", "x": 640, "y": 360,
        })
        self.assertEqual(text, "鼠标移动到：(640, 360)（桌面坐标）")

    def test_relative_move_shows_delta_and_mode(self):
        text = recorded_action_description({
            "type": "mouse_move", "mode": "relative", "dx": 18, "dy": -7,
        })
        self.assertEqual(text, "游戏转向：ΔX=18，ΔY=-7（相对轨迹）")

    def test_click_and_scroll_show_coordinates(self):
        self.assertEqual(
            recorded_action_description({
                "type": "mouse_button", "button": "left", "down": True,
                "x": 321, "y": 456,
            }),
            "左键按下：(321, 456)",
        )
        self.assertIn("位置=(222, 333)", recorded_action_description({
            "type": "scroll", "dx": 0, "dy": -1, "x": 222, "y": 333,
        }))


class WinInputTests(unittest.TestCase):
    def test_resolve_window_signature_matches_exact_signature(self):
        # 标题 + 类名 + 进程路径全匹配时命中对应窗口。
        windows = [
            WindowInfo(10, "大厅", "Launcher", "C:/Game/launcher.exe"),
            WindowInfo(20, "游戏", "GameWindow", "C:/Game/game.exe"),
        ]
        with patch("macroflow.input.wininput.enum_windows", return_value=windows):
            info = resolve_window_signature({
                "title": "游戏", "class_name": "GameWindow",
                "process_path": "C:/Game/game.exe",
            })
        self.assertIsNotNone(info)
        self.assertEqual(info.hwnd, 20)

    def test_resolve_window_signature_falls_back_to_class_and_process(self):
        # 标题带会话状态（"游戏 - 副本 1"）时退化为 类名+进程路径 匹配。
        windows = [
            WindowInfo(10, "游戏 - 副本 1", "GameWindow", "C:/Game/game.exe"),
        ]
        with patch("macroflow.input.wininput.enum_windows", return_value=windows):
            info = resolve_window_signature({
                "title": "游戏", "class_name": "GameWindow",
                "process_path": "C:/Game/game.exe",
            })
        self.assertIsNotNone(info)
        self.assertEqual(info.hwnd, 10)

    def test_resolve_window_signature_missing_window_returns_none(self):
        windows = [WindowInfo(10, "大厅", "Launcher", "C:/Game/launcher.exe")]
        with patch("macroflow.input.wininput.enum_windows", return_value=windows):
            self.assertIsNone(resolve_window_signature({"title": "游戏"}))
            self.assertIsNone(resolve_window_signature({}))
            self.assertIsNone(resolve_window_signature(None))

    def test_show_window_uses_show_without_resizing_normal_window(self):
        with patch("macroflow.input.wininput.is_window", return_value=True), \
             patch("macroflow.input.wininput.user32.IsIconic", return_value=False), \
             patch("macroflow.input.wininput.user32.ShowWindow") as show, \
             patch("macroflow.input.wininput.user32.IsWindowVisible", return_value=True):
            self.assertTrue(show_window(123))
        self.assertEqual(show.call_args.args[1], 5)

    def test_show_window_no_activate_preserves_geometry_and_focus(self):
        with patch("macroflow.input.wininput.is_window", return_value=True), \
             patch("macroflow.input.wininput.user32.ShowWindow") as show, \
             patch("macroflow.input.wininput.user32.SetWindowPos", return_value=True) as position:
            self.assertTrue(show_window_no_activate(123))
        self.assertEqual(show.call_args.args[1], 4)
        flags = position.call_args.args[-1]
        self.assertTrue(flags & 0x0001)  # SWP_NOSIZE
        self.assertTrue(flags & 0x0002)  # SWP_NOMOVE
        self.assertTrue(flags & 0x0004)  # SWP_NOZORDER
        self.assertTrue(flags & 0x0010)  # SWP_NOACTIVATE

    def test_mouse_input_falls_back_when_sendinput_returns_zero_without_error(self):
        with patch("macroflow.input.wininput.user32.SendInput", return_value=0), \
             patch("macroflow.input.wininput.user32.mouse_event") as fallback, \
             patch("macroflow.input.wininput.time.sleep"):
            send_move_relative(12, -7)
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[-1], MACROFLOW_INPUT_TAG)

    def test_sendinput_uses_focus_guard_dispatcher_when_installed(self):
        dispatcher = Mock()
        set_input_dispatcher(dispatcher)
        try:
            with patch("macroflow.input.wininput.user32.SendInput") as send_input:
                send_move_relative(12, -7)
        finally:
            set_input_dispatcher(None)
        dispatcher.assert_called_once()
        packet = dispatcher.call_args.args[0]
        self.assertEqual((packet.mi.dx, packet.mi.dy), (12, -7))
        self.assertEqual(packet.mi.dwExtraInfo, MACROFLOW_INPUT_TAG)
        send_input.assert_not_called()

    def test_activate_window_does_not_restore_non_minimized_window(self):
        with patch("macroflow.input.wininput.is_window", return_value=True), \
             patch("macroflow.input.wininput.user32.IsIconic", return_value=False), \
             patch("macroflow.input.wininput.user32.ShowWindow") as show, \
             patch("macroflow.input.wininput.kernel32.GetCurrentThreadId", return_value=1), \
             patch("macroflow.input.wininput.user32.GetWindowThreadProcessId", return_value=1), \
             patch("macroflow.input.wininput.user32.BringWindowToTop"), \
             patch("macroflow.input.wininput.user32.SetForegroundWindow"), \
             patch("macroflow.input.wininput.user32.SetFocus"), \
             patch("macroflow.input.wininput.user32.GetForegroundWindow", return_value=123), \
             patch("macroflow.input.wininput.time.sleep"):
            self.assertTrue(activate_window(123))
        show.assert_not_called()

    def test_activate_window_restores_only_minimized_window(self):
        with patch("macroflow.input.wininput.is_window", return_value=True), \
             patch("macroflow.input.wininput.user32.IsIconic", return_value=True), \
             patch("macroflow.input.wininput.user32.ShowWindow") as show, \
             patch("macroflow.input.wininput.kernel32.GetCurrentThreadId", return_value=1), \
             patch("macroflow.input.wininput.user32.GetWindowThreadProcessId", return_value=1), \
             patch("macroflow.input.wininput.user32.BringWindowToTop"), \
             patch("macroflow.input.wininput.user32.SetForegroundWindow"), \
             patch("macroflow.input.wininput.user32.SetFocus"), \
             patch("macroflow.input.wininput.user32.GetForegroundWindow", return_value=123), \
             patch("macroflow.input.wininput.time.sleep"):
            self.assertTrue(activate_window(123))
        show.assert_called_once()

    def test_activate_window_skips_set_focus_when_already_foreground(self):
        # 窗口已在前台：不再 SetFocus，避免从窗口内的子渲染表面
        # （Flash/CEF 画布）夺走键盘焦点，游戏不会弹出“点击游戏画面继续操作”。
        with patch("macroflow.input.wininput.is_window", return_value=True), \
             patch("macroflow.input.wininput.user32.IsIconic", return_value=False), \
             patch("macroflow.input.wininput.kernel32.GetCurrentThreadId", return_value=1), \
             patch("macroflow.input.wininput.user32.GetWindowThreadProcessId", return_value=1), \
             patch("macroflow.input.wininput.user32.BringWindowToTop"), \
             patch("macroflow.input.wininput.user32.SetForegroundWindow"), \
             patch("macroflow.input.wininput.user32.SetFocus") as set_focus, \
             patch("macroflow.input.wininput.user32.GetForegroundWindow", return_value=123), \
             patch("macroflow.input.wininput.time.sleep"):
            self.assertTrue(activate_window(123))
        set_focus.assert_not_called()

    def test_activate_window_set_focus_only_when_activation_failed(self):
        # SetForegroundWindow 未把窗口带到前台（前台是别的窗口）：才补 SetFocus。
        with patch("macroflow.input.wininput.is_window", return_value=True), \
             patch("macroflow.input.wininput.user32.IsIconic", return_value=False), \
             patch("macroflow.input.wininput.kernel32.GetCurrentThreadId", return_value=1), \
             patch("macroflow.input.wininput.user32.GetWindowThreadProcessId", return_value=1), \
             patch("macroflow.input.wininput.user32.BringWindowToTop"), \
             patch("macroflow.input.wininput.user32.SetForegroundWindow"), \
             patch("macroflow.input.wininput.user32.SetFocus") as set_focus, \
             patch("macroflow.input.wininput.user32.GetForegroundWindow", return_value=999), \
             patch("macroflow.input.wininput.time.sleep"):
            self.assertFalse(activate_window(123))
        set_focus.assert_called_once()
        self.assertEqual(set_focus.call_args.args[0].value, 123)

    def test_force_english_input_changes_layout_and_closes_ime(self):
        layout = 0x04090409
        with patch("macroflow.input.wininput.user32.LoadKeyboardLayoutW", return_value=layout) as load, \
             patch("macroflow.input.wininput.user32.ActivateKeyboardLayout") as activate, \
             patch("macroflow.input.wininput.user32.PostMessageW", return_value=True) as post, \
             patch("macroflow.input.wininput.user32.GetWindowThreadProcessId", return_value=77), \
             patch("macroflow.input.wininput.user32.GetKeyboardLayout", return_value=layout), \
             patch("macroflow.input.wininput.imm32.ImmGetContext", return_value=88), \
             patch("macroflow.input.wininput.imm32.ImmSetOpenStatus") as close_ime, \
             patch("macroflow.input.wininput.imm32.ImmReleaseContext"), \
             patch("macroflow.input.wininput.time.sleep"):
            self.assertTrue(force_english_input(123))
        load.assert_called_once_with("00000409", 1)
        activate.assert_called_once_with(layout, 0)
        post.assert_called_once()
        close_ime.assert_called_once()

    def test_center_lock_uses_window_position_plus_size(self):
        with patch("macroflow.input.wininput.user32.GetForegroundWindow", return_value=0), \
             patch("macroflow.input.wininput.get_window_rect", return_value=(100, 50, 800, 600)), \
             patch("macroflow.input.wininput.get_cursor_pos", return_value=(500, 350)):
            self.assertTrue(is_cursor_near_window_center(123))


class ImageTests(unittest.TestCase):
    def test_image_dialog_capture_creates_private_template_and_region(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.master = Mock()
        dialog.template = Mock()
        dialog.region_mode = Mock()
        dialog.region = Mock()
        dialog.template_combo = Mock()
        dialog._ancestors_to_hide = Mock(return_value=[])
        with patch("macroflow.ui.dialogs.ScreenRegionPicker") as picker_class:
            dialog.capture_custom_template()
        picker_class.return_value.start.assert_called_once()
        on_result = picker_class.call_args.args[2]
        with tempfile.TemporaryDirectory() as folder:
            images_dir = Path(folder) / "images"
            screen = np.zeros((40, 50, 3), dtype=np.uint8)
            with patch("macroflow.ui.dialogs.load_module_images_dir", return_value=images_dir), \
                 patch("macroflow.ui.dialogs.capture_bgr", return_value=(screen, (10, 20))), \
                 patch("macroflow.ui.dialogs.registered_template_options", return_value=["captured"]) as options:
                on_result([100, 200, 50, 40])
            saved = list(images_dir.glob("recognition_*.png"))
            self.assertEqual(len(saved), 1)
            selected = dialog.template.set.call_args.args[0]
            self.assertEqual(resolve_path(selected), saved[0])
            options.assert_called_once_with(selected)
        dialog.region_mode.set.assert_called_once_with("custom")
        dialog.region.set.assert_called_once_with("100,200,50,40")
        dialog.template_combo.configure.assert_called_once_with(values=["captured"])

    def test_image_dialog_capture_failure_keeps_current_template(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.master = Mock()
        dialog.template = Mock()
        dialog.region_mode = Mock()
        dialog.region = Mock()
        dialog.template_combo = Mock()
        dialog._ancestors_to_hide = Mock(return_value=[])
        with patch("macroflow.ui.dialogs.ScreenRegionPicker") as picker_class:
            dialog.capture_custom_template()
        on_result = picker_class.call_args.args[2]
        with patch("macroflow.ui.dialogs.capture_bgr", side_effect=RuntimeError("boom")), \
             patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            on_result([100, 200, 50, 40])
        self.assertIn("截图失败", notice.call_args.args[1])
        dialog.template.set.assert_not_called()
        dialog.region.set.assert_not_called()

    def test_custom_click_controls_are_disabled_for_match_center(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.click_target_mode = Mock()
        dialog.click_point_entry = Mock()
        dialog.click_point_button = Mock()
        dialog.click_target_mode.get.return_value = "识图区域中心"
        dialog._update_click_point_controls()
        dialog.click_point_entry.configure.assert_called_with(state="disabled")
        dialog.click_point_button.configure.assert_called_with(state="disabled")

        dialog.click_target_mode.get.return_value = "自定义坐标"
        dialog._update_click_point_controls()
        dialog.click_point_entry.configure.assert_called_with(state="normal")
        dialog.click_point_button.configure.assert_called_with(state="normal")

    def test_image_click_target_defaults_to_match_center(self):
        self.assertEqual(image_click_target_defaults({}), ("识图区域中心", [0, 0]))
        self.assertEqual(
            image_click_target_defaults({"click_target": "custom", "click_point": [640, 360]}),
            ("自定义坐标", [640, 360]),
        )

    def test_image_action_module_selection_copies_bound_region(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.module_key = Mock()
        dialog.template = Mock()
        dialog.region_mode = Mock()
        dialog.region = Mock()
        dialog.template_combo = Mock()
        dialog.module_name = Mock()
        with patch("macroflow.ui.dialogs.choose_module_binding", return_value={
            "module_ref": True, "module_key": "module:first",
            "template": "images/shared.png", "region_mode": "template",
            "region": [11, 22, 333, 444],
        }) as choose:
            dialog.select_image_module()

        choose.assert_called_once_with(dialog, categories=("switch",))
        dialog.module_key.set.assert_called_once_with("module:first")
        dialog.template.set.assert_called_once_with("images/shared.png")
        dialog.region_mode.set.assert_called_once_with("template")
        dialog.region.set.assert_called_once_with("11,22,333,444")

    def test_multi_condition_module_selection_copies_bound_region(self):
        dialog = MultiConditionClickDialog.__new__(MultiConditionClickDialog)
        dialog.condition_module_key = [Mock()]
        dialog.condition_template = [Mock()]
        dialog.condition_region = [Mock()]
        dialog.condition_type = [Mock()]
        with patch("macroflow.ui.dialogs.choose_module_binding", return_value={
            "module_ref": True, "module_key": "module:first",
            "template": "images/shared.png", "region_mode": "template",
            "region": [11, 22, 333, 444],
        }) as choose:
            dialog.select_condition_module(0)

        choose.assert_called_once_with(dialog, categories=("switch",))
        dialog.condition_module_key[0].set.assert_called_once_with("module:first")
        dialog.condition_template[0].set.assert_called_once_with("images/shared.png")
        dialog.condition_region[0].set.assert_called_once_with("11,22,333,444")
        dialog.condition_type[0].set.assert_called_once_with("图片识别")

    def test_image_dialog_saves_fallback_fields(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/x.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.85"
        dialog.timeout = Mock()
        dialog.timeout.get.return_value = "3000"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "250"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.region = Mock()
        dialog.region.get.return_value = "0,0,0,0"
        dialog.on_found = Mock()
        dialog.on_found.get.return_value = "click"
        dialog.found_jump_target = Mock()
        dialog.found_jump_target.get.return_value = ""
        dialog.found_delay = Mock()
        dialog.found_delay.get.return_value = "0"
        dialog.click_target_mode = Mock()
        dialog.click_target_mode.get.return_value = "识图区域中心"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "0,0"
        dialog.on_timeout = Mock()
        dialog.on_timeout.get.return_value = "continue"
        dialog.timeout_jump_target = Mock()
        dialog.timeout_jump_target.get.return_value = ""
        dialog.timeout_delay = Mock()
        dialog.timeout_delay.get.return_value = "0"
        dialog.wait_forever = Mock()
        dialog.wait_forever.get.return_value = True
        dialog.fallback_template = Mock()
        dialog.fallback_template.get.return_value = "images/y.png"
        dialog.fallback_switch_ms = Mock()
        dialog.fallback_switch_ms.get.return_value = "5000"
        dialog.fallback_region_mode = Mock()
        dialog.fallback_region_mode.get.return_value = "custom"
        dialog.fallback_region = Mock()
        dialog.fallback_region.get.return_value = "10,20,30,40"
        dialog.fallback_click = Mock()
        dialog.fallback_click.get.return_value = True
        dialog.fallback_on_match = Mock()
        dialog.fallback_on_match.get.return_value = "回到主模板的检测"
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "0"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "0"
        dialog.show_result_notice = Mock()
        dialog.show_result_notice.get.return_value = True
        dialog.jump_target_ids = {}
        dialog.master = Mock()
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.activate_main_after_modal"):
            dialog.save()
        self.assertEqual(dialog.result["fallback_template"], "images/y.png")
        self.assertEqual(dialog.result["fallback_switch_ms"], 5000)
        self.assertEqual(dialog.result["fallback_region_mode"], "custom")
        self.assertEqual(dialog.result["fallback_region"], [10, 20, 30, 40])
        self.assertTrue(dialog.result["wait_forever"])
        self.assertTrue(dialog.result["fallback_click"])
        self.assertEqual(dialog.result["fallback_on_match"], "回到主模板的检测")
        dialog.destroy.assert_called_once()

    def test_image_dialog_saves_fallback_no_click_and_exit(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/x.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.85"
        dialog.timeout = Mock()
        dialog.timeout.get.return_value = "3000"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "250"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.region = Mock()
        dialog.region.get.return_value = "0,0,0,0"
        dialog.on_found = Mock()
        dialog.on_found.get.return_value = "click"
        dialog.found_jump_target = Mock()
        dialog.found_jump_target.get.return_value = ""
        dialog.found_delay = Mock()
        dialog.found_delay.get.return_value = "0"
        dialog.click_target_mode = Mock()
        dialog.click_target_mode.get.return_value = "识图区域中心"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "0,0"
        dialog.on_timeout = Mock()
        dialog.on_timeout.get.return_value = "continue"
        dialog.timeout_jump_target = Mock()
        dialog.timeout_jump_target.get.return_value = ""
        dialog.timeout_delay = Mock()
        dialog.timeout_delay.get.return_value = "0"
        dialog.wait_forever = Mock()
        dialog.wait_forever.get.return_value = True
        dialog.fallback_template = Mock()
        dialog.fallback_template.get.return_value = "images/y.png"
        dialog.fallback_switch_ms = Mock()
        dialog.fallback_switch_ms.get.return_value = "3000"
        dialog.fallback_region_mode = Mock()
        dialog.fallback_region_mode.get.return_value = "screen"
        dialog.fallback_region = Mock()
        dialog.fallback_region.get.return_value = "0,0,0,0"
        dialog.fallback_click = Mock()
        dialog.fallback_click.get.return_value = False
        dialog.fallback_on_match = Mock()
        dialog.fallback_on_match.get.return_value = "直接退出识别"
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "0"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "0"
        dialog.show_result_notice = Mock()
        dialog.show_result_notice.get.return_value = False
        dialog.jump_target_ids = {}
        dialog.master = Mock()
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.activate_main_after_modal"):
            dialog.save()
        self.assertFalse(dialog.result["fallback_click"])
        self.assertEqual(dialog.result["fallback_on_match"], "直接退出识别")
        self.assertNotIn("fallback_jump_action_id", dialog.result)
        dialog.destroy.assert_called_once()

    def test_wait_forever_controls_toggle_fallback_state(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.wait_forever = Mock()
        dialog.timeout_entry = Mock()
        dialog.timeout_delay_entry = Mock()
        dialog.timeout_combo = Mock()
        dialog.fallback_combo = Mock()
        dialog.fallback_switch_entry = Mock()
        dialog.fallback_click_button = Mock()
        dialog.fallback_action_combo = Mock()
        dialog._update_timeout_jump_control = Mock()

        dialog.wait_forever.get.return_value = True
        dialog._update_wait_forever_controls()
        dialog.timeout_entry.configure.assert_called_with(state="disabled")
        dialog.timeout_delay_entry.configure.assert_called_with(state="disabled")
        dialog.timeout_combo.configure.assert_called_with(state="disabled")
        dialog.fallback_combo.configure.assert_called_with(state="normal")
        dialog.fallback_switch_entry.configure.assert_called_with(state="normal")
        dialog.fallback_click_button.configure.assert_called_with(state="normal")
        dialog.fallback_action_combo.configure.assert_called_with(state="normal")

        dialog.wait_forever.get.return_value = False
        dialog._update_wait_forever_controls()
        dialog.timeout_entry.configure.assert_called_with(state="normal")
        dialog.timeout_delay_entry.configure.assert_called_with(state="normal")
        dialog.timeout_combo.configure.assert_called_with(state="normal")
        dialog.fallback_combo.configure.assert_called_with(state="disabled")
        dialog.fallback_switch_entry.configure.assert_called_with(state="disabled")
        dialog.fallback_click_button.configure.assert_called_with(state="disabled")
        dialog.fallback_action_combo.configure.assert_called_with(state="disabled")

    def test_image_dialog_saves_template_region_mode_and_disabled_fallback(self):
        # v1.78：主模板已登记 → 引用模板；备用选"（不启用）"→ fallback_template 为空。
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.module_key = Mock()
        dialog.module_key.get.return_value = "module:first"
        dialog.template = Mock()
        dialog.template.get.return_value = "images/x.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.85"
        dialog.timeout = Mock()
        dialog.timeout.get.return_value = "3000"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "250"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.region = Mock()
        dialog.region.get.return_value = "0,0,0,0"
        dialog.on_found = Mock()
        dialog.on_found.get.return_value = "click"
        dialog.found_jump_target = Mock()
        dialog.found_jump_target.get.return_value = ""
        dialog.found_delay = Mock()
        dialog.found_delay.get.return_value = "0"
        dialog.click_target_mode = Mock()
        dialog.click_target_mode.get.return_value = "识图区域中心"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "0,0"
        dialog.on_timeout = Mock()
        dialog.on_timeout.get.return_value = "continue"
        dialog.timeout_jump_target = Mock()
        dialog.timeout_jump_target.get.return_value = ""
        dialog.timeout_delay = Mock()
        dialog.timeout_delay.get.return_value = "0"
        dialog.wait_forever = Mock()
        dialog.wait_forever.get.return_value = False
        dialog.fallback_template = Mock()
        dialog.fallback_template.get.return_value = "（不启用）"
        dialog.fallback_switch_ms = Mock()
        dialog.fallback_switch_ms.get.return_value = "0"
        dialog.fallback_region_mode = Mock()
        dialog.fallback_region_mode.get.return_value = "screen"
        dialog.fallback_region = Mock()
        dialog.fallback_region.get.return_value = "0,0,0,0"
        dialog.fallback_click = Mock()
        dialog.fallback_click.get.return_value = False
        dialog.fallback_on_match = Mock()
        dialog.fallback_on_match.get.return_value = "回到主模板的检测"
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "0"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "0"
        dialog.show_result_notice = Mock()
        dialog.show_result_notice.get.return_value = False
        dialog.jump_target_ids = {}
        dialog.master = Mock()
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={
            "category": "switch", "template": "images/x.png",
            "region": [10, 20, 30, 40],
        }), patch("macroflow.ui.dialogs.load_template_regions", return_value={
            "images/x.png": [10, 20, 30, 40],
        }), patch("macroflow.ui.dialogs.activate_main_after_modal"):
            dialog.save()
        result = dialog.result
        self.assertEqual(result["region_mode"], "template")
        self.assertEqual(result["region"], [10, 20, 30, 40])
        self.assertEqual(result["module_key"], "module:first")
        self.assertTrue(result["module_ref"])
        self.assertEqual(result["fallback_template"], "")
        self.assertEqual(result["fallback_region_mode"], "screen")
        dialog.destroy.assert_called_once()

    def test_image_dialog_saves_fallback_template_region_mode(self):
        # v1.78：备用模板已登记 → 备用区域同样引用模板登记表。
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/x.png"
        dialog.threshold = Mock()
        dialog.threshold.get.return_value = "0.85"
        dialog.timeout = Mock()
        dialog.timeout.get.return_value = "3000"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "250"
        dialog.region_mode = Mock()
        dialog.region_mode.get.return_value = "screen"
        dialog.region = Mock()
        dialog.region.get.return_value = "0,0,0,0"
        dialog.on_found = Mock()
        dialog.on_found.get.return_value = "click"
        dialog.found_jump_target = Mock()
        dialog.found_jump_target.get.return_value = ""
        dialog.found_delay = Mock()
        dialog.found_delay.get.return_value = "0"
        dialog.click_target_mode = Mock()
        dialog.click_target_mode.get.return_value = "识图区域中心"
        dialog.click_point = Mock()
        dialog.click_point.get.return_value = "0,0"
        dialog.on_timeout = Mock()
        dialog.on_timeout.get.return_value = "continue"
        dialog.timeout_jump_target = Mock()
        dialog.timeout_jump_target.get.return_value = ""
        dialog.timeout_delay = Mock()
        dialog.timeout_delay.get.return_value = "0"
        dialog.wait_forever = Mock()
        dialog.wait_forever.get.return_value = False
        dialog.fallback_template = Mock()
        dialog.fallback_template.get.return_value = "images/y.png"
        dialog.fallback_switch_ms = Mock()
        dialog.fallback_switch_ms.get.return_value = "5000"
        dialog.fallback_region_mode = Mock()
        dialog.fallback_region_mode.get.return_value = "custom"
        dialog.fallback_region = Mock()
        dialog.fallback_region.get.return_value = "10,20,30,40"
        dialog.fallback_click = Mock()
        dialog.fallback_click.get.return_value = False
        dialog.fallback_on_match = Mock()
        dialog.fallback_on_match.get.return_value = "回到主模板的检测"
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "0"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "0"
        dialog.show_result_notice = Mock()
        dialog.show_result_notice.get.return_value = False
        dialog.jump_target_ids = {}
        dialog.master = Mock()
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.load_template_regions", return_value={
            "images/y.png": [100, 200, 300, 400],
        }), patch("macroflow.ui.dialogs.activate_main_after_modal"):
            dialog.save()
        result = dialog.result
        self.assertEqual(result["region_mode"], "screen")
        self.assertEqual(result["fallback_template"], "images/y.png")
        self.assertEqual(result["fallback_region_mode"], "template")
        self.assertEqual(result["fallback_region"], [])
        dialog.destroy.assert_called_once()

    def test_curtain_click_records_screen_coordinate_without_clicking_through(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.click_point = Mock()
        dialog.click_target_mode = Mock()
        dialog.click_target_mode.get.return_value = "自定义坐标"
        dialog.click_point_entry = Mock()
        dialog.click_point_button = Mock()
        dialog._close_click_point_selection = Mock()
        event = Mock(x_root=777, y_root=444)
        dialog._select_click_point(event)
        dialog.click_point.set.assert_called_once_with("777,444")
        dialog.click_target_mode.set.assert_called_once_with("自定义坐标")
        dialog._close_click_point_selection.assert_called_once()

    def test_dialog_messages_use_root_floating_notice_callback(self):
        root = Mock()
        root._macroflow_notice_callback = Mock()
        parent = Mock()
        parent._root.return_value = root
        show_floating_notice(parent, "参数错误", "请输入有效整数", 3200)
        root._macroflow_notice_callback.assert_called_once_with(
            "参数错误：请输入有效整数", 3200,
        )

    def test_new_image_action_defaults_to_click_and_result_notice(self):
        self.assertEqual(image_action_option_defaults({}), ("click", True))
        self.assertEqual(
            image_action_option_defaults({"on_found": "continue", "show_result_notice": False}),
            ("continue", False),
        )

    def test_new_image_timeout_defaults(self):
        self.assertEqual(
            image_timeout_option_defaults({}),
            ("continue", 3000, 1000, 1, 0),
        )
        self.assertEqual(
            image_timeout_option_defaults({
                "on_timeout": "jump", "timeout_ms": 8000,
                "delay_ms": 250, "timeout_jump_row": 7,
                "timeout_delay_ms": 600,
            }),
            ("jump", 8000, 250, 7, 600),
        )
        self.assertEqual(
            image_timeout_option_defaults({"on_timeout": "end_current_script"})[0],
            "end_current_script",
        )
        self.assertEqual(image_timeout_option_label("end_current_script"), "结束当前脚本")
        self.assertEqual(image_timeout_option_value("结束当前脚本"), "end_current_script")

    def test_image_jump_options_follow_action_ids_not_old_rows(self):
        actions = [
            {"type": "comment", "text": "新插入", ACTION_ID_KEY: "new"},
            {"type": "click", ACTION_ID_KEY: "target"},
        ]
        options = image_jump_target_options(actions)
        self.assertEqual(options[1][1], "target")
        self.assertIn("第 2 行", options[1][0])

    def test_jump_dialog_saves_start_and_end_targets(self):
        dialog = JumpActionDialog.__new__(JumpActionDialog)
        dialog.target = Mock()
        dialog.target_ids = {
            "开头": SCRIPT_START_TARGET_ID,
            "结尾": NEXT_WORKFLOW_STEP_TARGET_ID,
        }
        dialog.target_rows = {"开头": 1, "结尾": 4}
        dialog.target.get.return_value = "结尾"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "jump", "jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
            "jump_row": 4, "workflow_repeat_at_least_2": True, "delay_ms": 0,
        })
        dialog.destroy.assert_called_once()

    def test_jump_dialog_saves_workflow_second_repeat_condition(self):
        dialog = JumpActionDialog.__new__(JumpActionDialog)
        dialog.target = Mock()
        dialog.target.get.return_value = "目标"
        dialog.target_ids = {"目标": "target"}
        dialog.target_rows = {"目标": 3}
        dialog.workflow_repeat_at_least_2 = Mock()
        dialog.workflow_repeat_at_least_2.get.return_value = True
        dialog.destroy = Mock()

        dialog.save()

        self.assertTrue(dialog.result["workflow_repeat_at_least_2"])
        self.assertEqual(dialog.result["jump_action_id"], "target")

    def test_template_match_supports_chinese_file_path(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "测试服务器.png"
            template = np.zeros((16, 18, 3), dtype=np.uint8)
            template[2:14, 3:15] = (20, 180, 240)
            template[6:10, :] = (230, 30, 40)
            encoded_ok, encoded = cv2.imencode(".png", template)
            self.assertTrue(encoded_ok)
            template_path.write_bytes(encoded.tobytes())
            screen = np.zeros((80, 100, 3), dtype=np.uint8)
            screen[25:41, 44:62] = template
            with patch("macroflow.core.image_match.capture_bgr", return_value=(screen, (10, 20))):
                match = find_template(template_path, 0.95)
            self.assertIsNotNone(match)
            self.assertEqual((match["x"], match["y"]), (54, 45))

    def test_template_match(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "template.png"
            template = np.zeros((16, 18, 3), dtype=np.uint8)
            template[2:14, 3:15] = (20, 180, 240)
            template[6:10, :] = (230, 30, 40)
            cv2.imwrite(str(template_path), template)
            screen = np.zeros((80, 100, 3), dtype=np.uint8)
            screen[25:41, 44:62] = template
            with patch("macroflow.core.image_match.capture_bgr", return_value=(screen, (10, 20))):
                match = find_template(template_path, 0.95)
            self.assertIsNotNone(match)
            self.assertEqual((match["x"], match["y"]), (54, 45))

    def test_template_scale_matches_resized_target(self):
        # 执行机截图尺寸与录制机不同（多屏/分辨率/DPI 差异）时，目标在截图
        # 里的像素大小与模板不一致，固定尺寸匹配会失败；按屏幕宽度比缩放
        # 模板后再匹配即可命中，匹配结果坐标仍是截图坐标系。
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "resized.png"
            template = np.zeros((16, 18, 3), dtype=np.uint8)
            template[2:14, 3:15] = (20, 180, 240)
            template[6:10, :] = (230, 30, 40)
            cv2.imwrite(str(template_path), template)
            screen = np.zeros((80, 100, 3), dtype=np.uint8)
            screen[25:41, 44:62] = template
            scaled = cv2.resize(screen, (200, 160), interpolation=cv2.INTER_AREA)
            # 不缩放：40% 面积差导致匹配度跌到阈值以下（用户遇到的现象）。
            self.assertIsNone(find_template_in_image(template_path, scaled, 0.95))
            # 按 2x 缩放模板：命中且位置（44,25）×2 = (88,50) 正确。
            match = find_template_in_image(template_path, scaled, 0.95, scale=2.0)
            self.assertIsNotNone(match)
            self.assertEqual((match["x"], match["y"]), (88, 50))

    def test_screen_template_scale(self):
        self.assertEqual(screen_template_scale(
            {"width": 1920, "height": 1080}, {"width": 3840, "height": 2160}), 2.0)
        self.assertEqual(screen_template_scale(
            {"width": 3840, "height": 2160}, {"width": 1920, "height": 1080}), 0.5)
        self.assertEqual(screen_template_scale(
            {"width": 1920, "height": 1080}, {"width": 1920, "height": 1080}), 1.0)
        self.assertEqual(screen_template_scale(None, {"width": 3840}), 1.0)
        self.assertEqual(screen_template_scale({"width": 0}, {"width": 3840}), 1.0)

    def test_template_match_reuses_existing_full_screenshot_with_region(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "shared.png"
            template = np.zeros((12, 14, 3), dtype=np.uint8)
            template[2:10, 3:11] = (10, 170, 230)
            template[5:8, :] = (220, 20, 30)
            cv2.imwrite(str(template_path), template)
            screen = np.zeros((100, 140, 3), dtype=np.uint8)
            screen[45:57, 70:84] = template
            match = find_template_in_image(
                template_path, screen, 0.95, origin=(10, 20),
                region=(60, 50, 60, 50),
            )
        self.assertIsNotNone(match)
        self.assertEqual((match["x"], match["y"]), (80, 65))

    def test_ignore_background_matches_when_background_changed(self):
        # 模板：深灰底 + 白笔画。截图：金色纹理底 + 相同白笔画（按钮高亮）。
        # 背景颜色变了，普通匹配分数掉到阈值下；忽略背景只按笔画匹配仍高分命中。
        rng = np.random.default_rng(7)
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "button.png"
            template = np.zeros((40, 120, 3), dtype=np.uint8)
            template[:] = (35, 35, 35)
            for y, x0, w in [(8, 10, 100), (18, 10, 100), (28, 10, 60)]:
                template[y:y + 6, x0:x0 + w] = (245, 245, 245)
            template = cv2.GaussianBlur(template, (3, 3), 0)
            cv2.imwrite(str(template_path), template)
            screen = np.zeros((300, 400, 3), dtype=np.uint8)
            for x in range(400):
                for y in range(300):
                    pick = (x // 13 + y // 13) % 3
                    screen[y, x] = (
                        (220, 170, 60) if pick == 0 else
                        (180, 120, 30) if pick == 1 else (120, 70, 15)
                    )
            screen += rng.integers(0, 10, screen.shape).astype(np.uint8)
            # 笔画绘制后做一次模糊模拟抗锯齿（与模板截图一致），纹理部分保持原样。
            stroke_mask = np.zeros((300, 400), dtype=np.uint8)
            for y, x0, w in [(8, 10, 100), (18, 10, 100), (28, 10, 60)]:
                screen[100 + y:106 + y, 50 + x0:50 + x0 + w] = (245, 245, 245)
                stroke_mask[100 + y:106 + y, 50 + x0:50 + x0 + w] = 255
            blurred = cv2.GaussianBlur(screen, (3, 3), 0)
            screen = np.where(stroke_mask[..., None] > 0, blurred, screen)
            self.assertIsNone(find_template_in_image(template_path, screen, 0.92))
            match = find_template_in_image(
                template_path, screen, 0.92, ignore_background=True,
            )
            self.assertIsNotNone(match)
            self.assertEqual((match["x"], match["y"]), (50, 100))
            self.assertGreater(match["score"], 0.95)

    def test_ignore_background_rejects_when_text_absent(self):
        # 没有文字的目标图：忽略背景匹配不应在纯背景上产生假阳性。
        rng = np.random.default_rng(7)
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "button.png"
            template = np.zeros((40, 120, 3), dtype=np.uint8)
            template[:] = (35, 35, 35)
            for y, x0, w in [(8, 10, 100), (18, 10, 100), (28, 10, 60)]:
                template[y:y + 6, x0:x0 + w] = (245, 245, 245)
            template = cv2.GaussianBlur(template, (3, 3), 0)
            cv2.imwrite(str(template_path), template)
            screen = np.zeros((300, 400, 3), dtype=np.uint8)
            for x in range(400):
                for y in range(300):
                    pick = (x // 13 + y // 13) % 3
                    screen[y, x] = (
                        (60, 30, 160) if pick == 0 else
                        (40, 20, 110) if pick == 1 else (25, 12, 70)
                    )
            screen += rng.integers(0, 10, screen.shape).astype(np.uint8)
            self.assertIsNone(
                find_template_in_image(
                    template_path, screen, 0.85, ignore_background=True,
                )
            )

    def test_ignore_background_falls_back_when_background_unidentifiable(self):
        # 模板本身是渐变背景，无法用单一颜色描述 → 忽略背景自动回退普通匹配。
        # 渐变背景在目标截图里原样保留，普通匹配仍能精确命中。
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "button.png"
            template = np.zeros((40, 120, 3), dtype=np.uint8)
            for x in range(120):
                shade = 40 + x * 180 // 120
                template[:, x] = (shade, shade, shade)
            for y, x0, w in [(8, 10, 100), (18, 10, 100), (28, 10, 60)]:
                template[y:y + 6, x0:x0 + w] = (245, 245, 245)
            cv2.imwrite(str(template_path), template)
            screen = np.zeros((300, 400, 3), dtype=np.uint8)
            for x in range(400):
                shade = 40 + x * 180 // 400
                screen[:, x] = (shade, shade, shade)
            screen[100:140, 50:170] = template
            match = find_template_in_image(
                template_path, screen, 0.85, ignore_background=True,
            )
            self.assertIsNotNone(match)
            self.assertEqual((match["x"], match["y"]), (50, 100))

    def test_ensure_action_ids_migrates_found_jump_row(self):
        actions = [
            {"type": "comment", "text": "目标"},
            {"type": "image_match", "on_found": "jump", "found_jump_row": 1},
        ]
        ensure_action_ids(actions)
        self.assertEqual(actions[1]["found_jump_action_id"], actions[0][ACTION_ID_KEY])

    def test_clone_actions_remap_found_jump_target(self):
        actions = [
            {"type": "image_match", "on_found": "jump", "found_jump_action_id": "target"},
            {"type": "comment", "text": "目标", ACTION_ID_KEY: "target"},
        ]
        clones = clone_actions_with_new_ids(actions)
        self.assertNotEqual(clones[0]["found_jump_action_id"], "target")
        self.assertEqual(clones[0]["found_jump_action_id"], clones[1][ACTION_ID_KEY])

    def test_found_jump_options_include_finish_current_script(self):
        actions = [{"type": "comment", "text": "目标", ACTION_ID_KEY: "target"}]
        options = image_found_jump_target_options(actions)
        self.assertEqual(options[0], (
            "直接结束当前脚本，执行工作流下一项",
            NEXT_WORKFLOW_STEP_TARGET_ID,
        ))
        self.assertEqual(options[1][1], "target")

    def test_clone_actions_preserves_next_workflow_target(self):
        actions = [{
            "type": "image_match", "on_found": "jump",
            "found_jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
        }]
        clones = clone_actions_with_new_ids(actions)
        self.assertEqual(
            clones[0]["found_jump_action_id"], NEXT_WORKFLOW_STEP_TARGET_ID,
        )

    def test_found_jump_control_state_follows_on_found(self):
        dialog = ImageActionDialog.__new__(ImageActionDialog)
        dialog.on_found = Mock()
        dialog.found_jump_entry = Mock()
        dialog.on_found.get.return_value = "jump"
        dialog._update_found_jump_control()
        dialog.found_jump_entry.configure.assert_called_with(state="normal")

        dialog.on_found.get.return_value = "continue"
        dialog._update_found_jump_control()
        dialog.found_jump_entry.configure.assert_called_with(state="disabled")

    def test_click_dialog_applies_picked_position(self):
        dialog = ClickDialog.__new__(ClickDialog)
        dialog.x = Mock()
        dialog.y = Mock()
        dialog._apply_picked_point(777, 444)
        dialog.x.set.assert_called_once_with("777")
        dialog.y.set.assert_called_once_with("444")

    def test_click_dialog_saves_values(self):
        dialog = ClickDialog.__new__(ClickDialog)
        dialog.kind = "click"
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.x = Mock()
        dialog.x.get.return_value = "300"
        dialog.y = Mock()
        dialog.y.get.return_value = "400"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "30"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "500"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "click", "button": "left",
            "x": 300, "y": 400, "hold_ms": 30, "delay_ms": 500,
        })
        dialog.destroy.assert_called_once()

    def test_click_dialog_saves_current_pos_mode(self):
        dialog = ClickDialog.__new__(ClickDialog)
        dialog.kind = "click"
        dialog.button = Mock()
        dialog.button.get.return_value = "right"
        dialog.x = Mock()
        dialog.x.get.return_value = "300"
        dialog.y = Mock()
        dialog.y.get.return_value = "400"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "30"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "500"
        dialog.pos_mode = Mock()
        dialog.pos_mode.get.return_value = True
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "click", "button": "right",
            "x": 300, "y": 400, "hold_ms": 30, "delay_ms": 500,
            "pos_mode": "current",
        })

    def test_click_dialog_delay_and_coords_are_editable(self):
        # 回归：X/Y 可手动输入（保留幕布选取按钮）；执行前延时绝不能被
        # _update_pos_mode 误设只读（曾因 _y_entry 槽位被延时框覆盖而只读）。
        root = tk.Tk()
        root.withdraw()
        try:
            dialog = ClickDialog(root, {"type": "click"})
            entries: dict[str, tk.Widget] = {}

            def collect(widget):
                if widget.winfo_class() == "TEntry":
                    name = widget.cget("textvariable")
                    if name:
                        entries[name] = widget
                for child in widget.winfo_children():
                    collect(child)

            collect(dialog)
            for variable, label in ((dialog.x, "X"), (dialog.y, "Y"), (dialog.delay, "延时")):
                entry = entries.get(str(variable))
                self.assertIsNotNone(entry, f"缺少 {label} 输入框")
                self.assertEqual(str(entry.cget("state")), "normal", f"{label} 应可输入")
            dialog.pos_mode.set(True)
            dialog._update_pos_mode()
            self.assertEqual(str(entries[str(dialog.x)].cget("state")), "disabled")
            self.assertEqual(str(entries[str(dialog.y)].cget("state")), "disabled")
            self.assertEqual(str(entries[str(dialog.delay)].cget("state")), "normal")
            dialog.destroy()
        finally:
            root.destroy()

    def test_click_summary_shows_current_position(self):
        self.assertEqual(
            action_summary({"type": "click", "button": "left", "pos_mode": "current"})[1],
            "left @ 鼠标当前位置",
        )
        self.assertEqual(
            action_summary({"type": "click", "button": "left", "x": 10, "y": 20})[1],
            "left @ (10, 20)",
        )

    def test_mouse_button_dialog_saves_press_and_release(self):
        for down_label, down_value in (("按下", True), ("松开", False)):
            dialog = ClickDialog.__new__(ClickDialog)
            dialog.kind = "mouse_button"
            dialog.button = Mock()
            dialog.button.get.return_value = "middle"
            dialog.x = Mock()
            dialog.x.get.return_value = "100"
            dialog.y = Mock()
            dialog.y.get.return_value = "200"
            dialog.down = Mock()
            dialog.down.get.return_value = down_label
            dialog.delay = Mock()
            dialog.delay.get.return_value = "0"
            dialog.destroy = Mock()
            dialog.save()
            self.assertEqual(dialog.result, {
                "type": "mouse_button", "button": "middle",
                "down": down_value, "x": 100, "y": 200, "delay_ms": 0,
            })
            dialog.destroy.assert_called_once()

    def test_mouse_button_dialog_saves_delay(self):
        dialog = ClickDialog.__new__(ClickDialog)
        dialog.kind = "mouse_button"
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.x = Mock()
        dialog.x.get.return_value = "10"
        dialog.y = Mock()
        dialog.y.get.return_value = "20"
        dialog.down = Mock()
        dialog.down.get.return_value = "按下"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "800"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result["delay_ms"], 800)

    def test_editing_mouse_button_action_uses_click_dialog(self):
        original = {"type": "mouse_button", "action_id": "stable-mb", "down": True}
        with patch("macroflow.ui.dialogs.ClickDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "mouse_button", "button": "left", "down": False,
                "x": 1, "y": 2,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-mb")
        self.assertFalse(updated["down"])
        dialog_class.assert_called_once()

    def test_editing_module_row_uses_jump_mode_dialog(self):
        # v1.68：编辑带 jump_row 的全局模块行时用跳转模式对话框。
        # v1.70：跳转目标是行对象，对话框拿到全部动作用于行选择列表。
        original = {"type": "global_detect", "action_id": "stable-g",
                    "template": "images/g.png", "jump_row": 4}
        others = [{"type": "delay", "ms": 1, "action_id": "a"},
                  {"type": "key_press", "name": "B", "action_id": "b"}]
        with patch("macroflow.ui.dialogs.GlobalDetectDialog") as dialog_class:
            dialog_class.return_value.show.return_value = dict(original)
            updated = edit_action(None, original, others)
        self.assertEqual(updated["action_id"], "stable-g")
        dialog_class.assert_called_once_with(None, original, jump=True, actions=others)

    def test_editing_plain_global_detect_keeps_default_dialog_mode(self):
        original = {"type": "global_detect", "action_id": "stable-g",
                    "template": "images/g.png"}
        with patch("macroflow.ui.dialogs.GlobalDetectDialog") as dialog_class:
            dialog_class.return_value.show.return_value = dict(original)
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-g")
        dialog_class.assert_called_once_with(None, original, jump=False, actions=None)

    def test_repeat_click_dialog_saves_values(self):
        dialog = RepeatClickDialog.__new__(RepeatClickDialog)
        dialog.button = Mock()
        dialog.button.get.return_value = "left"
        dialog.x = Mock()
        dialog.x.get.return_value = "300"
        dialog.y = Mock()
        dialog.y.get.return_value = "400"
        dialog.count = Mock()
        dialog.count.get.return_value = "5"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "80"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "20"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "1000"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "repeat_click", "button": "left",
            "x": 300, "y": 400,
            "count": 5, "interval_ms": 80,
            "hold_ms": 20, "delay_ms": 1000,
        })
        dialog.destroy.assert_called_once()

    def test_repeat_click_dialog_clamps_count_and_interval(self):
        dialog = RepeatClickDialog.__new__(RepeatClickDialog)
        dialog.button = Mock()
        dialog.button.get.return_value = "right"
        dialog.x = Mock()
        dialog.x.get.return_value = "10"
        dialog.y = Mock()
        dialog.y.get.return_value = "20"
        dialog.count = Mock()
        dialog.count.get.return_value = "0"
        dialog.interval = Mock()
        dialog.interval.get.return_value = "-5"
        dialog.hold = Mock()
        dialog.hold.get.return_value = "0"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "-1"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result["count"], 1)
        self.assertEqual(dialog.result["interval_ms"], 0)
        self.assertEqual(dialog.result["hold_ms"], 1)
        self.assertEqual(dialog.result["delay_ms"], 0)

    def test_repeat_click_dialog_applies_picked_position(self):
        dialog = RepeatClickDialog.__new__(RepeatClickDialog)
        dialog.x = Mock()
        dialog.y = Mock()
        dialog._apply_picked_point(555, 666)
        dialog.x.set.assert_called_once_with("555")
        dialog.y.set.assert_called_once_with("666")

    def test_text_action_dialog_saves_values(self):
        dialog = TextActionDialog.__new__(TextActionDialog)
        dialog.text_var = Mock()
        dialog.text_var.get.return_value = "你好"
        dialog.char_delay = Mock()
        dialog.char_delay.get.return_value = "20"
        dialog.delay = Mock()
        dialog.delay.get.return_value = "1000"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "text", "text": "你好", "char_delay_ms": 20, "delay_ms": 1000,
        })
        dialog.destroy.assert_called_once()

    def test_open_app_dialog_saves_values(self):
        dialog = OpenAppDialog.__new__(OpenAppDialog)
        dialog.path = Mock()
        dialog.path.get.return_value = "C:/Tools/游戏.exe"
        dialog.args = Mock()
        dialog.args.get.return_value = " -windowed "
        dialog.delay = Mock()
        dialog.delay.get.return_value = "200"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "500"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "open_app", "path": "C:/Tools/游戏.exe",
            "args": "-windowed",
            "delay_ms": 200, "after_delay_ms": 500,
        })
        dialog.destroy.assert_called_once()

    def test_open_app_dialog_saves_without_args(self):
        dialog = OpenAppDialog.__new__(OpenAppDialog)
        dialog.path = Mock()
        dialog.path.get.return_value = "C:/Tools/游戏.exe"
        dialog.args = Mock()
        dialog.args.get.return_value = ""
        dialog.delay = Mock()
        dialog.delay.get.return_value = "0"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "0"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result["args"], "")
        dialog.destroy.assert_called_once()

    def test_close_app_dialog_saves_values(self):
        dialog = CloseAppDialog.__new__(CloseAppDialog)
        dialog.name = Mock()
        dialog.name.get.return_value = " clash-verge.exe "
        dialog.graceful = Mock()
        dialog.graceful.get.return_value = True
        dialog.graceful_wait_ms = Mock()
        dialog.graceful_wait_ms.get.return_value = "3000"
        dialog.tree = Mock()
        dialog.tree.get.return_value = False
        dialog.elevated_retry = Mock()
        dialog.elevated_retry.get.return_value = True
        dialog.delay = Mock()
        dialog.delay.get.return_value = "100"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "0"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result, {
            "type": "close_app", "name": "clash-verge.exe",
            "graceful": True, "graceful_wait_ms": 3000,
            "tree": False, "elevated_retry": True,
            "delay_ms": 100, "after_delay_ms": 0,
        })
        dialog.destroy.assert_called_once()

    def test_move_dialog_absolute_pick_records_position(self):
        dialog = MouseMoveDialog.__new__(MouseMoveDialog)
        dialog.mode = Mock()
        dialog.mode.get.return_value = "absolute"
        dialog.x = Mock()
        dialog.y = Mock()
        dialog._apply_picked_point(640, 360)
        dialog.x.set.assert_called_once_with("640")
        dialog.y.set.assert_called_once_with("360")

    def test_move_dialog_relative_measurement_computes_delta(self):
        dialog = MouseMoveDialog.__new__(MouseMoveDialog)
        dialog.mode = Mock()
        dialog.mode.get.return_value = "relative"
        dialog.x = Mock()
        dialog.y = Mock()
        dialog._apply_picked_point(100, 50, 260, 110)
        dialog.x.set.assert_called_once_with("160")
        dialog.y.set.assert_called_once_with("60")

    def test_screen_point_picker_single_click_reports_point(self):
        picker = ScreenPointPicker.__new__(ScreenPointPicker)
        picker.two_points = False
        picker.close = Mock()
        picker.on_result = Mock()
        picker._on_click(Mock(x_root=333, y_root=222))
        picker.close.assert_called_once()
        picker.on_result.assert_called_once_with(333, 222)

    def test_screen_point_picker_two_points_reports_start_and_end(self):
        picker = ScreenPointPicker.__new__(ScreenPointPicker)
        picker.two_points = True
        picker.first_point = None
        picker.canvas = Mock()
        picker.tip_id = 1
        picker.close = Mock()
        picker.on_result = Mock()
        picker._on_click(Mock(x_root=100, y_root=50))
        self.assertEqual(picker.first_point, (100, 50))
        picker.on_result.assert_not_called()
        picker._on_click(Mock(x_root=260, y_root=110))
        picker.on_result.assert_called_once_with(100, 50, 260, 110)


class OcrTests(unittest.TestCase):

    def test_parse_ocr_number_pair_accepts_full_width_separator_and_spaces(self):
        self.assertEqual(parse_ocr_number_pair("当前 １２ ／ １２", "/"), (12, 12))
        self.assertEqual(parse_ocr_number_pair("挑战 12 / 34", "/"), (12, 34))

    def test_parse_ocr_number_pair_rejects_missing_or_malformed_side(self):
        self.assertIsNone(parse_ocr_number_pair("12-34", "/"))
        self.assertIsNone(parse_ocr_number_pair("12/", "/"))
        self.assertIsNone(parse_ocr_number_pair("/34", "/"))

    def test_ocr_compare_dialog_saves_custom_regions_and_two_branches(self):
        form = OcrCompareActionDialog.__new__(OcrCompareActionDialog)
        form.master = Mock()
        form.jump_target_ids = {"第 1 行 · 延时": "aid1", "第 2 行 · 点击": "aid2"}
        form.region = Mock(); form.region.get.return_value = "10,20,300,400"
        form.separator = Mock(); form.separator.get.return_value = "/"
        form.click_region = Mock(); form.click_region.get.return_value = "100,200,50,40"
        form.button = Mock(); form.button.get.return_value = "left"
        form.equal_action = Mock(); form.equal_action.get.return_value = "连续点击"
        form.equal_click_count = Mock(); form.equal_click_count.get.return_value = "3"
        form.equal_jump_target = Mock(); form.equal_jump_target.get.return_value = ""
        form.not_equal_action = Mock(); form.not_equal_action.get.return_value = "跳转到目标动作"
        form.not_equal_click_count = Mock(); form.not_equal_click_count.get.return_value = "1"
        form.not_equal_jump_target = Mock(); form.not_equal_jump_target.get.return_value = "第 2 行 · 点击"
        form.timeout = Mock(); form.timeout.get.return_value = "3000"
        form.interval = Mock(); form.interval.get.return_value = "500"
        form.on_timeout = Mock(); form.on_timeout.get.return_value = "继续执行"
        form.timeout_jump_target = Mock(); form.timeout_jump_target.get.return_value = ""
        form.show_result_notice = Mock(); form.show_result_notice.get.return_value = True
        form.destroy = Mock()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_not_called()
        form.destroy.assert_called_once()
        self.assertEqual(form.result["type"], "ocr_compare")
        self.assertEqual(form.result["region"], [10, 20, 300, 400])
        self.assertEqual(form.result["click_region"], [100, 200, 50, 40])
        self.assertEqual(form.result["equal_action"], "click")
        self.assertEqual(form.result["equal_click_count"], 3)
        self.assertEqual(form.result["not_equal_action"], "jump")
        self.assertEqual(form.result["not_equal_jump_action_id"], "aid2")

    def test_multi_condition_click_dialog_saves_three_selectable_conditions(self):
        form_class = getattr(dialog_module, "MultiConditionClickDialog", None)
        self.assertIsNotNone(form_class, "缺少固定三条件多条件识图点击配置窗口")
        form = form_class.__new__(form_class)
        form.master = Mock()
        form.condition_enabled = [Mock(), Mock(), Mock()]
        form.condition_enabled[0].get.return_value = True
        form.condition_enabled[1].get.return_value = True
        form.condition_enabled[2].get.return_value = False
        form.condition_type = [Mock(), Mock(), Mock()]
        form.condition_type[0].get.return_value = "image"
        form.condition_type[1].get.return_value = "ocr"
        form.condition_type[2].get.return_value = "number_compare"
        form.condition_region = [Mock(), Mock(), Mock()]
        form.condition_region[0].get.return_value = "10,20,100,80"
        form.condition_region[1].get.return_value = "200,20,120,40"
        form.condition_region[2].get.return_value = "400,20,120,40"
        form.condition_module_key = [Mock(), Mock(), Mock()]
        form.condition_module_key[0].get.return_value = "module:first"
        form.condition_module_key[1].get.return_value = ""
        form.condition_module_key[2].get.return_value = ""
        form.condition_template = [Mock(), Mock(), Mock()]
        form.condition_template[0].get.return_value = "button.png"
        form.condition_template[1].get.return_value = ""
        form.condition_template[2].get.return_value = ""
        form.condition_threshold = [Mock(), Mock(), Mock()]
        form.condition_threshold[0].get.return_value = "0.9"
        form.condition_threshold[1].get.return_value = "0.85"
        form.condition_threshold[2].get.return_value = "0.85"
        form.condition_expected = [Mock(), Mock(), Mock()]
        form.condition_expected[0].get.return_value = ""
        form.condition_expected[1].get.return_value = "完成"
        form.condition_expected[2].get.return_value = ""
        form.condition_match_mode = [Mock(), Mock(), Mock()]
        form.condition_match_mode[0].get.return_value = "contains"
        form.condition_match_mode[1].get.return_value = "contains"
        form.condition_match_mode[2].get.return_value = "contains"
        form.condition_separator = [Mock(), Mock(), Mock()]
        form.condition_separator[0].get.return_value = "/"
        form.condition_separator[1].get.return_value = "/"
        form.condition_separator[2].get.return_value = "/"
        form.condition_relation = [Mock(), Mock(), Mock()]
        form.condition_relation[0].get.return_value = "equal"
        form.condition_relation[1].get.return_value = "equal"
        form.condition_relation[2].get.return_value = "not_equal"
        form.click_region = Mock(); form.click_region.get.return_value = "600,200,50,40"
        form.button = Mock(); form.button.get.return_value = "left"
        form.click_count = Mock(); form.click_count.get.return_value = "4"
        form.timeout = Mock(); form.timeout.get.return_value = "3000"
        form.interval = Mock(); form.interval.get.return_value = "500"
        form.on_timeout = Mock(); form.on_timeout.get.return_value = "continue"
        form.show_result_notice = Mock(); form.show_result_notice.get.return_value = True
        form.destroy = Mock()
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={
            "category": "switch", "template": "images/shared.png",
            "region": [11, 22, 333, 444],
        }), patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_not_called()
        form.destroy.assert_called_once()
        self.assertEqual(form.result["type"], "multi_condition_click")
        self.assertEqual([item["enabled"] for item in form.result["conditions"]], [True, True, False])
        self.assertEqual([item["type"] for item in form.result["conditions"]], ["image", "ocr", "number_compare"])
        self.assertEqual(form.result["conditions"][0]["template"], "images/shared.png")
        self.assertEqual(form.result["conditions"][0]["module_key"], "module:first")
        self.assertEqual(form.result["conditions"][0]["region"], [11, 22, 333, 444])
        self.assertTrue(form.result["conditions"][0]["module_ref"])
        self.assertEqual(form.result["conditions"][1]["expected_text"], "完成")
        self.assertEqual(form.result["click_region"], [600, 200, 50, 40])

    def test_multi_condition_click_dialog_has_scrollable_form(self):
        form_class = getattr(dialog_module, "MultiConditionClickDialog", None)
        self.assertIsNotNone(form_class, "缺少固定三条件多条件识图点击配置窗口")
        init_source = inspect.getsource(form_class.__init__)
        scroll_source = inspect.getsource(form_class._scroll_form)
        self.assertIn("tk.Canvas", init_source)
        self.assertIn("ttk.Scrollbar", init_source)
        self.assertIn("scrollregion", init_source)
        self.assertIn("create_window", init_source)
        self.assertIn("yview_scroll", scroll_source)

    def test_extract_ocr_integer_sorts_boxes_left_to_right(self):
        value, digits = extract_ocr_integer("721", [
            {"text": "7", "x": 130},
            {"text": "1", "x": 10},
            {"text": "2", "x": 70},
        ])
        self.assertEqual((value, digits), (127, "127"))

    def test_extract_ocr_integer_normalizes_full_width_and_leading_zeroes(self):
        value, digits = extract_ocr_integer("unused", [
            {"text": "００", "x": 10}, {"text": "７", "x": 40},
        ])
        self.assertEqual((value, digits), (7, "007"))

    def test_extract_ocr_integer_returns_none_without_digits(self):
        self.assertEqual(extract_ocr_integer("体力", [{"text": "ABC", "x": 1}]), (None, ""))

    def test_ocr_observation_shows_non_target_text_and_match_state(self):
        self.assertEqual(
            format_ocr_observation("可锁取", "可领取", False, "奖励可领取"),
            "奖励可领取 OCR：识别到「可锁取」；期望「可领取」· 未命中",
        )

    def test_recognize_image_with_boxes_returns_absolute_text_coordinates(self):
        engine = Mock()
        engine.predict.return_value = [{
            "rec_texts": ["可领取"],
            "rec_scores": [0.98],
            "rec_polys": [np.array([[5, 4], [45, 4], [45, 24], [5, 24]])],
        }]
        with patch("macroflow.core.ocr._get_engine", return_value=engine):
            text, matches = recognize_image_with_boxes(
                np.zeros((30, 50, 3), dtype=np.uint8), (100, 200),
            )
        self.assertEqual(text, "可领取")
        self.assertEqual(
            (matches[0]["x"], matches[0]["y"],
             matches[0]["center_x"], matches[0]["center_y"]),
            (105, 204, 125, 214),
        )
        self.assertIs(find_expected_match(matches, "可领取", "contains"), matches[0])
    """识别文字：匹配逻辑 + 对话框保存。"""

    def test_matches_expected_contains(self):
        self.assertTrue(matches_expected("体力不足，请补充", "体力不足", "contains"))
        self.assertTrue(matches_expected("背包已满", "背包", "contains"))
        self.assertTrue(matches_expected("ABC", "abc", "contains"))  # 忽略大小写
        self.assertFalse(matches_expected("体力不足", "背包", "contains"))

    def test_matches_expected_equals_strips_and_folds_case(self):
        self.assertTrue(matches_expected("  背包已满  ", "背包已满", "equals"))
        self.assertTrue(matches_expected("MacroFlow", "macroflow", "equals"))
        self.assertFalse(matches_expected("背包已满", "背包已满！", "equals"))
        self.assertFalse(matches_expected("背包已满！", "背包已满", "equals"))

    def test_matches_expected_empty_expected_requires_any_text(self):
        self.assertTrue(matches_expected("任何文字", "", "contains"))
        self.assertTrue(matches_expected("任何文字", "  ", "equals"))
        self.assertFalse(matches_expected("", "", "contains"))
        self.assertFalse(matches_expected("  ", ""))

    def test_matches_expected_default_mode_is_contains(self):
        self.assertTrue(matches_expected("确认购买？", "确认"))
        self.assertFalse(matches_expected("取消", "确认"))

    def test_ocr_dialog_save_builds_action_dict(self):
        form = OcrActionDialog.__new__(OcrActionDialog)
        form.master = Mock()
        form.jump_target_ids = {"第 1 行 · 延时": "aid1", "第 2 行 · 点击": "aid2"}
        form.region_mode = Mock()
        form.region_mode.get.return_value = "自定义区域"
        form.region = Mock()
        form.region.get.return_value = "10,20,300,400"
        form.expected_text = Mock()
        form.expected_text.get.return_value = "体力不足"
        form.match_mode = Mock()
        form.match_mode.get.return_value = "包含"
        form.timeout = Mock()
        form.timeout.get.return_value = "3000"
        form.interval = Mock()
        form.interval.get.return_value = "500"
        form.on_found = Mock()
        form.on_found.get.return_value = "跳转到目标动作"
        form.found_jump_target = Mock()
        form.found_jump_target.get.return_value = "第 1 行 · 延时"
        form.found_delay = Mock()
        form.found_delay.get.return_value = "0"
        form.on_timeout = Mock()
        form.on_timeout.get.return_value = "继续执行"
        form.timeout_delay = Mock()
        form.timeout_delay.get.return_value = "0"
        form.timeout_jump_target = Mock()
        form.timeout_jump_target.get.return_value = "第 2 行 · 点击"
        form.show_result_notice = Mock()
        form.show_result_notice.get.return_value = True
        form.destroy = Mock()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_not_called()
        form.destroy.assert_called_once()
        self.assertEqual(form.result["type"], "text_ocr")
        self.assertEqual(form.result["region_mode"], "custom")
        self.assertEqual(form.result["region"], [10, 20, 300, 400])
        self.assertEqual(form.result["expected_text"], "体力不足")
        self.assertEqual(form.result["match_mode"], "contains")
        self.assertEqual(form.result["timeout_ms"], 3000)
        self.assertEqual(form.result["interval_ms"], 500)
        self.assertEqual(form.result["on_found"], "jump")
        self.assertEqual(form.result["found_jump_action_id"], "aid1")
        self.assertEqual(form.result["on_timeout"], "continue")
        self.assertEqual(form.result["timeout_jump_action_id"], "aid2")
        self.assertTrue(form.result["show_result_notice"])

    def test_ocr_dialog_save_rejects_jump_without_target(self):
        form = OcrActionDialog.__new__(OcrActionDialog)
        form.master = Mock()
        form.jump_target_ids = {}
        form.region_mode = Mock()
        form.region_mode.get.return_value = "全屏"
        form.region = Mock()
        form.region.get.return_value = ""
        form.expected_text = Mock()
        form.expected_text.get.return_value = ""
        form.match_mode = Mock()
        form.match_mode.get.return_value = "包含"
        form.timeout = Mock()
        form.timeout.get.return_value = "3000"
        form.interval = Mock()
        form.interval.get.return_value = "500"
        form.on_found = Mock()
        form.on_found.get.return_value = "跳转到目标动作"
        form.found_jump_target = Mock()
        form.found_jump_target.get.return_value = "不存在"
        form.found_delay = Mock()
        form.found_delay.get.return_value = "0"
        form.on_timeout = Mock()
        form.on_timeout.get.return_value = "继续执行"
        form.timeout_delay = Mock()
        form.timeout_delay.get.return_value = "0"
        form.timeout_jump_target = Mock()
        form.timeout_jump_target.get.return_value = "不存在"
        form.show_result_notice = Mock()
        form.show_result_notice.get.return_value = False
        form.result = None
        form.destroy = Mock()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        form.destroy.assert_not_called()
        self.assertIsNone(form.result)

    def test_ocr_dialog_save_rejects_bad_region(self):
        form = OcrActionDialog.__new__(OcrActionDialog)
        form.master = Mock()
        form.jump_target_ids = {}
        form.region_mode = Mock()
        form.region_mode.get.return_value = "全屏"
        form.region = Mock()
        form.region.get.return_value = "1,2,3"
        form.expected_text = Mock()
        form.expected_text.get.return_value = ""
        form.match_mode = Mock()
        form.match_mode.get.return_value = "包含"
        form.timeout = Mock()
        form.timeout.get.return_value = "3000"
        form.interval = Mock()
        form.interval.get.return_value = "500"
        form.on_found = Mock()
        form.on_found.get.return_value = "继续执行"
        form.found_jump_target = Mock()
        form.found_jump_target.get.return_value = ""
        form.found_delay = Mock()
        form.found_delay.get.return_value = "0"
        form.on_timeout = Mock()
        form.on_timeout.get.return_value = "继续执行"
        form.timeout_delay = Mock()
        form.timeout_delay.get.return_value = "0"
        form.timeout_jump_target = Mock()
        form.timeout_jump_target.get.return_value = ""
        form.show_result_notice = Mock()
        form.show_result_notice.get.return_value = False
        form.destroy = Mock()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        form.destroy.assert_not_called()


class DetectOverlayTests(unittest.TestCase):
    def test_show_and_hide_overlay_creates_window(self):
        from macroflow.ui.detect_overlay import hide_overlay, show_overlay
        show_overlay(50, 60, 100, 80)
        show_overlay(60, 70, 120, 90, duration_ms=80)  # 重复调用刷新位置
        hide_overlay()
        show_overlay(70, 80, 10, 10)
        hide_overlay()

    def test_show_overlay_ignores_empty_region(self):
        from macroflow.ui.detect_overlay import hide_overlay, show_overlay
        show_overlay(0, 0, 0, 0)
        show_overlay(10, 10, -5, 5)
        hide_overlay()


class AlertTests(unittest.TestCase):
    def test_alert_uses_windows_audio_device(self):
        called = threading.Event()

        def capture_audio(data, _flags):
            self.assertTrue(data.startswith(b"RIFF"))
            called.set()

        with patch("macroflow.core.alerts.winsound.PlaySound", side_effect=capture_audio):
            play_alert("record_start")
            self.assertTrue(called.wait(1.0))


class PlayerTests(unittest.TestCase):
    def test_missing_module_reference_does_not_run_stale_template(self):
        player = MacroPlayer()
        with patch(
            "macroflow.execution.player.registered_module_object", return_value=None,
        ), patch("macroflow.execution.player.find_template", return_value=None) as find:
            with self.assertRaisesRegex(RuntimeError, "引用的模块不存在"):
                player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:deleted", "template": "images/stale.png",
                    "region_mode": "template", "region": [1, 2, 3, 4],
                    "timeout_ms": 0,
                }, None)

        find.assert_not_called()

    def test_recorded_input_actions_keep_each_recorded_delay(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._execute_action = Mock(return_value=None)

        player._run_action_sequence([
            {"type": "key", "delay_ms": 0},
            {"type": "key", "delay_ms": 100},
            {"type": "mouse_button", "delay_ms": 200},
        ], None)

        self.assertEqual(
            [call.args[0] for call in player._wait.call_args_list],
            [0, 100, 200],
        )

    def test_playback_speed_scales_waits_without_changing_hold_time(self):
        player = MacroPlayer()
        player.set_playback_speed(1.2)
        player._wait = Mock()
        player._execute_action = Mock(return_value=None)

        player._run_action_sequence([
            {"type": "key_press", "delay_ms": 120, "hold_ms": 300},
            {"type": "delay", "delay_ms": 240},
        ], None)

        self.assertEqual(
            [call.args[0] for call in player._wait.call_args_list],
            [100, 200],
        )

    def test_execute_action_does_not_recheck_foreground_for_each_action(self):
        player = MacroPlayer()
        player._ensure_foreground_for_input = Mock()

        player._execute_action({"type": "comment", "text": "no-op"}, None)

        player._ensure_foreground_for_input.assert_not_called()

    def test_ensure_foreground_activates_target_when_not_foreground(self):
        # 目标窗口不在前台时激活它。
        player = MacroPlayer()
        player._activate_target = True
        player._relative_target_hwnd = 50
        player._status = Mock()
        with patch.object(player, "_input_target_hwnd", return_value=50), \
             patch("macroflow.execution.player.is_window_process_foreground", return_value=False), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate:
            player._ensure_foreground_for_input(None)
        activate.assert_called_once_with(50)

    def test_ensure_foreground_skips_when_target_is_foreground(self):
        player = MacroPlayer()
        player._activate_target = True
        player._relative_target_hwnd = 50
        player._status = Mock()
        with patch.object(player, "_input_target_hwnd", return_value=50), \
             patch("macroflow.execution.player.is_window_process_foreground", return_value=True), \
             patch("macroflow.execution.player.activate_window") as activate:
            player._ensure_foreground_for_input(None)
        activate.assert_not_called()

    def test_restore_target_foreground_activates_target(self):
        player = MacroPlayer()
        player._activate_target = True
        player._status = Mock()
        with patch.object(player, "_input_target_hwnd", return_value=50), \
             patch("macroflow.execution.player.is_window_process_foreground", return_value=False), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate:
            player._restore_target_foreground(None)
        activate.assert_called_once_with(50)

    def test_long_wait_does_not_recheck_foreground(self):
        # 长等待期间只轮询守卫，不再反复检测或激活目标窗口。
        player = MacroPlayer()
        player.on_guard_poll = Mock(return_value=None)
        player.stop_event = Mock()
        player.stop_event.wait.return_value = False
        player._ensure_foreground_for_input = Mock()
        player._wait(600)
        player._ensure_foreground_for_input.assert_not_called()

    def test_short_wait_skips_foreground_guard(self):
        player = MacroPlayer()
        player.on_guard_poll = Mock(return_value=None)
        player.stop_event = Mock()
        player.stop_event.wait.return_value = False
        player._ensure_foreground_for_input = Mock()
        player._wait(100)
        player._ensure_foreground_for_input.assert_not_called()

    def test_no_recognition_module_executes_directly_without_image_matching(self):
        player = MacroPlayer()
        waits = []
        logs = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        player._log_event = lambda text: logs.append(text)
        module = {
            "name": "直接动作", "recognize": "none", "delay_ms": 120,
            "after_action": "continue", "run_code_after_action": True,
            "on_success_actions": [{"type": "delay", "ms": 25}],
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.find_template") as find, \
             patch.object(player, "_run_action_sequence") as run_segment:
            result = player._execute_image({
                "type": "image_match", "module_ref": True,
                "module_key": "module:direct", "template": "",
            }, None)
        self.assertIsNone(result)
        self.assertEqual(waits, [120])
        find.assert_not_called()
        run_segment.assert_called_once()
        self.assertIn("无需识图", logs[0])

    def test_number_module_reads_region_and_routes_equal_to_success_target(self):
        player = MacroPlayer()
        player._wait = lambda _milliseconds: None
        module = {
            "name": "剩余次数", "recognize": "number", "region": [10, 20, 80, 30],
            "blocking": False, "interval_ms": 50, "not_found_timeout_ms": 1000,
        }
        action = {
            "type": "image_match", "module_ref": True, "module_key": "module:number",
            "region_mode": "template", "expected_number": 127,
            "on_found": "jump", "found_jump_action_id": "equal-target",
            "on_timeout": "jump", "timeout_jump_action_id": "other-target",
        }
        boxes = [
            {"text": "7", "x": 130}, {"text": "1", "x": 10},
            {"text": "2", "x": 70},
        ]
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.recognize_region_with_boxes", return_value=("721", boxes)) as read, \
             patch("macroflow.execution.player.find_template") as find:
            result = player._execute_image(action, None)
        self.assertEqual(result, ("action_id", "equal-target"))
        read.assert_called_once_with((10, 20, 80, 30))
        find.assert_not_called()

    def test_number_module_routes_not_equal_immediately_to_failure_target(self):
        logs = []
        player = MacroPlayer(on_log=logs.append)
        module = {
            "name": "剩余次数", "recognize": "number", "region": [1, 2, 3, 4],
            "blocking": True, "interval_ms": 50, "not_found_timeout_ms": 9999,
        }
        action = {
            "type": "image_match", "module_ref": True, "module_key": "module:number",
            "region_mode": "template", "expected_number": 5,
            "on_found": "jump", "found_jump_action_id": "equal-target",
            "on_timeout": "jump", "timeout_jump_action_id": "other-target",
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.recognize_region_with_boxes", return_value=("4", [{"text": "4", "x": 1}])) as read:
            result = player._execute_image(action, None)
        self.assertEqual(result, ("action_id", "other-target"))
        read.assert_called_once()
        self.assertIn("模块 剩余次数 比较结果：不相等", logs)
        self.assertFalse(any("执行结果：失败" in text for text in logs))

    def test_number_module_retries_no_digits_then_compares(self):
        player = MacroPlayer()
        player._wait = lambda _milliseconds: None
        module = {
            "name": "层数", "recognize": "number", "region": [1, 2, 30, 40],
            "blocking": True, "interval_ms": 50, "not_found_timeout_ms": 0,
        }
        action = {
            "type": "image_match", "module_ref": True, "module_key": "module:number",
            "region_mode": "template", "expected_number": 7,
            "on_found": "jump", "found_jump_action_id": "equal-target",
            "on_timeout": "jump", "timeout_jump_action_id": "other-target",
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.recognize_region_with_boxes", side_effect=[
                 ("加载", []), ("００７", [{"text": "００７", "x": 1}]),
             ]) as read:
            result = player._execute_image(action, None)
        self.assertEqual(result, ("action_id", "equal-target"))
        self.assertEqual(read.call_count, 2)

    def test_number_module_no_digits_timeout_uses_failure_target(self):
        logs = []
        player = MacroPlayer(on_log=logs.append)
        module = {
            "name": "层数", "recognize": "number", "region": [1, 2, 30, 40],
            "blocking": False, "interval_ms": 50, "not_found_timeout_ms": 0,
        }
        action = {
            "type": "image_match", "module_ref": True, "module_key": "module:number",
            "region_mode": "template", "expected_number": 7,
            "on_found": "jump", "found_jump_action_id": "equal-target",
            "on_timeout": "jump", "timeout_jump_action_id": "other-target",
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.recognize_region_with_boxes", return_value=("加载", [])):
            result = player._execute_image(action, None)
        self.assertEqual(result, ("action_id", "other-target"))
        self.assertIn("模块 层数 读取结果：未读取到数字", logs)
        self.assertFalse(any("执行结果：失败" in text for text in logs))

    def test_number_module_requires_row_comparison_value(self):
        player = MacroPlayer()
        module = {
            "name": "层数", "recognize": "number", "region": [1, 2, 30, 40],
            "blocking": False, "interval_ms": 50, "not_found_timeout_ms": 0,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "未设置比较数字"):
                player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:number", "region_mode": "template",
                }, None)

    def setUp(self):
        # 识别成功时 player 会调用检测框提醒；测试中拦截，避免创建真实窗口。
        patcher = patch("macroflow.execution.player.show_overlay")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_script_scope_reentered_on_each_repeat(self):
        # 关卡封装"执行 x 次"时，每次重复都要重新进入脚本全局作用域，
        # 让全局模块（如超时计时）从本次重复开始重新注册、重新计时。
        entered = []
        exited = []
        player = MacroPlayer(
            on_script_scope_enter=lambda actions: entered.append(1) or (),
            on_script_scope_exit=lambda keys: exited.append(1),
        )
        player._status = lambda text: None
        player.play([{"type": "delay", "ms": 0, "delay_ms": 0}], repeats=3)
        self.assertEqual(len(entered), 3)
        self.assertEqual(len(exited), 3)
        # 每次进入时上一次作用域必须已退出，避免监控叠加。
        self.assertEqual(
            entered, exited,
            "每次重复进入前应退出上一次的全局作用域",
        )

    def test_script_scope_single_repeat_enters_and_exits_once(self):
        entered = []
        exited = []
        player = MacroPlayer(
            on_script_scope_enter=lambda actions: entered.append(1) or (),
            on_script_scope_exit=lambda keys: exited.append(1),
        )
        player._status = lambda text: None
        player.play([{"type": "delay", "ms": 0, "delay_ms": 0}], repeats=1)
        self.assertEqual(len(entered), 1)
        self.assertEqual(len(exited), 1)

    def test_image_timeout_jump_follows_target_action_after_row_insert(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        actions = [
            {
                "type": "image_match", ACTION_ID_KEY: "jump",
                "template": "images/目标.png", "timeout_ms": 0, "delay_ms": 0,
                "on_timeout": "jump", "timeout_jump_action_id": "target",
            },
            {"type": "comment", "text": "后来插入的行", ACTION_ID_KEY: "inserted"},
            {"type": "unknown_must_be_skipped", ACTION_ID_KEY: "skip"},
            {
                "type": "notice", "text": "到达目标动作", "duration_ms": 1000,
                ACTION_ID_KEY: "target",
            },
        ]
        with patch("macroflow.execution.player.find_template", return_value=None):
            player.play(actions)
        self.assertEqual(notices, [("到达目标动作", 1000)])

    def test_play_can_start_from_selected_action_index(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))

        player.play([
            {"type": "unknown_must_be_skipped"},
            {"type": "notice", "text": "从第二行开始", "duration_ms": 1000},
        ], start_index=1)

        self.assertEqual(notices, [("从第二行开始", 1000)])

    def test_image_timeout_can_jump_to_one_based_action_row(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        with patch("macroflow.execution.player.find_template", return_value=None):
            player.play([
                {
                    "type": "image_match", "template": "images/目标.png",
                    "timeout_ms": 0, "delay_ms": 0,
                    "on_timeout": "jump", "timeout_jump_row": 3,
                },
                {"type": "unknown_must_be_skipped"},
                {"type": "notice", "text": "已跳转", "duration_ms": 1000},
            ])
        self.assertEqual(notices, [("已跳转", 1000)])

    def test_image_found_jump_follows_target_action_after_row_insert(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        actions = [
            {
                "type": "image_match", ACTION_ID_KEY: "jump",
                "template": "images/目标.png", "timeout_ms": 0, "delay_ms": 0,
                "on_found": "jump", "found_jump_action_id": "target",
            },
            {"type": "comment", "text": "后来插入的行", ACTION_ID_KEY: "inserted"},
            {"type": "notice", "text": "找到后跳转成功", "duration_ms": 1000, ACTION_ID_KEY: "target"},
        ]
        with patch("macroflow.execution.player.find_template", return_value=match):
            player.play(actions)
        self.assertEqual(notices, [("找到后跳转成功", 1000)])

    def test_image_found_can_jump_to_one_based_action_row(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", return_value=match):
            player.play([
                {
                    "type": "image_match", "template": "images/目标.png",
                    "timeout_ms": 0, "delay_ms": 0,
                    "on_found": "jump", "found_jump_row": 3,
                },
                {"type": "unknown_must_be_skipped"},
                {"type": "notice", "text": "已跳转", "duration_ms": 1000},
            ])
        self.assertEqual(notices, [("已跳转", 1000)])

    def test_image_timeout_waits_before_jump(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.find_template", return_value=None):
            result = player._execute_image({
                "template": "images/目标.png",
                "timeout_ms": 0,
                "on_timeout": "jump",
                "timeout_jump_row": 3,
                "timeout_delay_ms": 500,
                "show_result_notice": False,
            }, None)
        self.assertEqual(result, ("row", 3))
        self.assertIn(500, waits)

    def test_image_timeout_waits_before_continue(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.find_template", return_value=None):
            result = player._execute_image({
                "template": "images/目标.png",
                "timeout_ms": 0,
                "on_timeout": "continue",
                "timeout_delay_ms": 800,
                "show_result_notice": False,
            }, None)
        self.assertIsNone(result)
        self.assertIn(800, waits)

    def test_image_wait_forever_blocks_until_found(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", side_effect=[None, None, match]):
            result = player._execute_image({
                "template": "images/目标.png",
                "timeout_ms": 0,
                "wait_forever": True,
                "on_found": "continue",
                "show_result_notice": False,
                "interval_ms": 100,
            }, None)
        self.assertIsNone(result)
        self.assertEqual(waits, [100, 100, 0])

    def test_image_wait_forever_switches_to_fallback_after_timeout(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 3 else dict(main_match)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button") as button:
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        self.assertEqual(
            [Path(call).name for call in calls],
            ["main.png", "fallback.png", "main.png"],
        )
        move.assert_called_once_with(200, 300)
        button.assert_called()
        self.assertEqual(button.call_args_list[0].args, ("left", True))

    def test_image_template_region_mode_reads_registered_region(self):
        # v1.78：region_mode="template" → 区域从模板登记表读取并传给 find_template。
        player = MacroPlayer()
        match = {"x": 10, "y": 20, "width": 30, "height": 40,
                 "center_x": 25, "center_y": 40, "score": 0.95}
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            template_png = Path(folder) / "main.png"
            template_png.write_bytes(b"x")
            with patch("macroflow.execution.player.find_template", return_value=match) as find, \
                 patch("macroflow.execution.player.registered_template_region", return_value=[100, 50, 300, 200]), \
                 patch("macroflow.execution.player.send_move_absolute"), patch("macroflow.execution.player.send_button"):
                player._execute_image({
                    "template": str(template_png),
                    "region_mode": "template",
                    "timeout_ms": 0,
                    "on_found": "continue",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertEqual(find.call_args.args[0], template_png)
        # 未配置源/目标屏幕时缩放为恒等：区域原样传给识图。
        self.assertEqual(find.call_args.args[2], (100, 50, 300, 200))

    def test_image_template_region_mode_without_region_uses_fullscreen(self):
        # 模板未登记 / 未设置区域：全屏识别并一次性告警。
        player = MacroPlayer()
        statuses = []
        player.on_status = lambda text: statuses.append(text)
        match = {"x": 10, "y": 20, "width": 30, "height": 40,
                 "center_x": 25, "center_y": 40, "score": 0.95}
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            template_png = Path(folder) / "main.png"
            template_png.write_bytes(b"x")
            with patch("macroflow.execution.player.find_template", return_value=match) as find, \
                 patch("macroflow.execution.player.registered_template_region", return_value=None), \
                 patch("macroflow.execution.player.send_move_absolute"), patch("macroflow.execution.player.send_button"):
                player._execute_image({
                    "template": str(template_png),
                    "region_mode": "template",
                    "timeout_ms": 0,
                    "on_found": "continue",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(find.call_args.args[2])
        self.assertTrue(any("未设置区域，按全屏识别" in text for text in statuses))

    def test_ocr_hit_continues(self):
        # 识别文字命中：按 found_delay 等待后继续执行。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.recognize_region", return_value="体力不足，请补充"):
            result = player._execute_text_ocr({
                "expected_text": "体力不足",
                "timeout_ms": 0,
                "interval_ms": 500,
                "on_found": "continue",
                "found_delay_ms": 200,
                "on_timeout": "continue",
                "show_result_notice": False,
            }, None)
        self.assertIsNone(result)
        self.assertEqual(waits, [200])

    def test_ocr_compare_equal_clicks_custom_click_region(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._click_module_point = Mock()
        with patch(
            "macroflow.execution.player.recognize_region_with_boxes",
            return_value=("12/12", []),
        ) as recognize:
            try:
                result = player._execute_action({
                    "type": "ocr_compare",
                    "region_mode": "custom",
                    "region": [10, 20, 300, 400],
                    "separator": "/",
                    "click_region": [100, 200, 50, 40],
                    "button": "left",
                    "equal_action": "click",
                    "equal_click_count": 3,
                    "not_equal_action": "continue",
                    "timeout_ms": 0,
                }, None)
            except RuntimeError as exc:
                self.fail(f"识别数字比较动作未实现：{exc}")
        self.assertIsNone(result)
        recognize.assert_called_once_with((10, 20, 300, 400))
        player._click_module_point.assert_called_once_with(125, 220, "left", 3, None)

    def test_ocr_compare_not_equal_jumps_to_selected_action(self):
        player = MacroPlayer()
        player._wait = Mock()
        with patch(
            "macroflow.execution.player.recognize_region_with_boxes",
            return_value=("12/34", []),
        ):
            try:
                result = player._execute_action({
                    "type": "ocr_compare",
                    "region_mode": "custom",
                    "region": [10, 20, 300, 400],
                    "separator": "/",
                    "click_region": [100, 200, 50, 40],
                    "not_equal_action": "jump",
                    "not_equal_jump_action_id": "row-b",
                    "equal_action": "continue",
                    "timeout_ms": 0,
                }, None)
            except RuntimeError as exc:
                self.fail(f"识别数字比较动作未实现：{exc}")
        self.assertEqual(result, ("action_id", "row-b"))

    def test_ocr_compare_invalid_text_uses_timeout_branch(self):
        player = MacroPlayer()
        player._wait = Mock()
        with patch(
            "macroflow.execution.player.recognize_region_with_boxes",
            return_value=("没有有效格式", []),
        ), patch(
            "macroflow.execution.player.time.perf_counter",
            side_effect=[100.0, 101.0],
        ):
            result = player._execute_action({
                "type": "ocr_compare",
                "region_mode": "custom",
                "region": [10, 20, 300, 400],
                "separator": "/",
                "click_region": [100, 200, 50, 40],
                "on_timeout": "jump",
                "timeout_jump_action_id": "timeout-row",
                "timeout_ms": 50,
            }, None)
        self.assertEqual(result, ("action_id", "timeout-row"))

    def test_multi_condition_click_requires_image_ocr_and_number_all_match(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._click_module_point = Mock()
        image_match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        action = {
            "type": "multi_condition_click",
            "conditions": [
                {
                    "enabled": True, "type": "image", "template": "button.png",
                    "threshold": 0.9, "region": [10, 20, 100, 80],
                },
                {
                    "enabled": True, "type": "ocr", "expected_text": "完成",
                    "match_mode": "contains", "region": [200, 20, 120, 40],
                },
                {
                    "enabled": True, "type": "number_compare", "separator": "/",
                    "relation": "equal", "region": [400, 20, 120, 40],
                },
            ],
            "click_region": [600, 200, 50, 40],
            "button": "left", "click_count": 4,
            "timeout_ms": 0, "interval_ms": 200,
        }
        with patch(
            "macroflow.execution.player.find_template", return_value=image_match,
        ) as find, patch(
            "macroflow.execution.player.recognize_region_with_boxes",
            side_effect=[("完成", [{"text": "完成"}]), ("8/8", [])],
        ) as recognize:
            try:
                result = player._execute_action(action, None)
            except RuntimeError as exc:
                self.fail(f"多条件识图点击动作未实现：{exc}")
        self.assertIsNone(result)
        find.assert_called_once()
        self.assertEqual(find.call_args.args[2], (10, 20, 100, 80))
        recognize.assert_has_calls([call((200, 20, 120, 40)), call((400, 20, 120, 40))])
        player._click_module_point.assert_called_once_with(625, 220, "left", 4, None)

    def test_multi_condition_image_module_uses_its_live_bound_region(self):
        player = MacroPlayer()
        condition = {
            "enabled": True, "type": "image", "module_ref": True,
            "module_key": "module:first", "template": "images/stale.png",
            "region": [1, 2, 3, 4], "threshold": 0.5,
        }
        module_obj = {
            "template": "images/shared.png", "region": [11, 22, 333, 444],
            "threshold": 0.91, "ignore_background": True,
        }
        with patch(
            "macroflow.execution.player.registered_module_object",
            return_value=module_obj,
        ), patch("macroflow.execution.player.find_template", return_value={
            "center_x": 20, "center_y": 30,
        }) as find:
            matched = player._multi_condition_matches(condition, None)

        self.assertTrue(matched)
        find.assert_called_once_with(
            resolve_path("images/shared.png"), 0.91, (11, 22, 333, 444),
            ignore_background=True, scale=1.0,
        )

    def test_multi_condition_click_does_not_click_when_one_condition_is_missing(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._click_module_point = Mock()
        action = {
            "type": "multi_condition_click",
            "conditions": [
                {
                    "enabled": True, "type": "image", "template": "button.png",
                    "threshold": 0.9, "region": [10, 20, 100, 80],
                },
                {
                    "enabled": True, "type": "ocr", "expected_text": "完成",
                    "match_mode": "contains", "region": [200, 20, 120, 40],
                },
                {"enabled": False, "type": "number_compare"},
            ],
            "click_region": [600, 200, 50, 40],
            "button": "left", "click_count": 4,
            "timeout_ms": 0, "interval_ms": 200,
        }
        with patch(
            "macroflow.execution.player.find_template", return_value=None,
        ), patch(
            "macroflow.execution.player.recognize_region_with_boxes",
        ) as recognize:
            try:
                result = player._execute_action(action, None)
            except RuntimeError as exc:
                self.fail(f"多条件识图点击动作未实现：{exc}")
        self.assertIsNone(result)
        recognize.assert_not_called()
        player._click_module_point.assert_not_called()

    def test_multi_condition_click_number_not_equal_condition(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._click_module_point = Mock()
        action = {
            "type": "multi_condition_click",
            "conditions": [
                {
                    "enabled": True, "type": "number_compare", "separator": "/",
                    "relation": "not_equal", "region": [400, 20, 120, 40],
                },
                {"enabled": False, "type": "image"},
                {"enabled": False, "type": "ocr"},
            ],
            "click_region": [600, 200, 50, 40],
            "button": "right", "click_count": 2,
            "timeout_ms": 0, "interval_ms": 200,
        }
        with patch(
            "macroflow.execution.player.recognize_region_with_boxes",
            return_value=("5/6", []),
        ):
            try:
                result = player._execute_action(action, None)
            except RuntimeError as exc:
                self.fail(f"多条件数字比较条件未实现：{exc}")
        self.assertIsNone(result)
        player._click_module_point.assert_called_once_with(625, 220, "right", 2, None)

    def test_ocr_hit_any_text_when_expected_empty(self):
        # 期望文字留空：识别到任意文字即命中。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.recognize_region", return_value="随便什么文字"):
            result = player._execute_text_ocr({
                "expected_text": "",
                "timeout_ms": 0,
                "on_found": "continue",
                "on_timeout": "continue",
                "show_result_notice": False,
            }, None)
        self.assertIsNone(result)
        self.assertEqual(waits, [0])

    def test_ocr_hit_jumps_to_target_action(self):
        # 命中后跳转到目标动作（按稳定动作 ID）。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.recognize_region", return_value="确认购买？"):
            result = player._execute_text_ocr({
                "expected_text": "确认",
                "timeout_ms": 0,
                "on_found": "jump",
                "found_jump_action_id": "target123",
                "found_delay_ms": 0,
                "on_timeout": "continue",
                "show_result_notice": False,
            }, None)
        self.assertEqual(result, ("action_id", "target123"))
        self.assertEqual(waits, [0])

    def test_ocr_miss_timeout_continues(self):
        # 未命中直到超时：按设置继续。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.recognize_region", return_value=""), \
             patch("macroflow.execution.player.time.perf_counter", side_effect=[100.0, 100.05, 101.0]):
            result = player._execute_text_ocr({
                "expected_text": "体力不足",
                "timeout_ms": 100,
                "interval_ms": 300,
                "on_found": "continue",
                "on_timeout": "continue",
                "timeout_delay_ms": 50,
                "show_result_notice": False,
            }, None)
        self.assertIsNone(result)
        # 第一次识别后未命中 → 等 interval 重试 → 重试后已超时 → 等 timeout_delay。
        self.assertEqual(waits, [300, 50])

    def test_ocr_miss_timeout_jumps(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.recognize_region", return_value="没有字"), \
             patch("macroflow.execution.player.time.perf_counter", side_effect=[100.0, 101.0]):
            result = player._execute_text_ocr({
                "expected_text": "体力不足",
                "timeout_ms": 50,
                "interval_ms": 300,
                "on_found": "continue",
                "on_timeout": "jump",
                "timeout_jump_action_id": "target456",
                "show_result_notice": False,
            }, None)
        self.assertEqual(result, ("action_id", "target456"))

    def test_ocr_miss_timeout_stops(self):
        # 超时后选择停止：抛出异常终止执行。
        player = MacroPlayer()
        player._wait = lambda milliseconds: None
        with patch("macroflow.execution.player.recognize_region", return_value="没有字"), \
             patch("macroflow.execution.player.time.perf_counter", side_effect=[100.0, 101.0]):
            with self.assertRaisesRegex(RuntimeError, "识别文字超时"):
                player._execute_text_ocr({
                    "expected_text": "体力不足",
                    "timeout_ms": 10,
                    "interval_ms": 300,
                    "on_found": "continue",
                    "on_timeout": "stop",
                    "show_result_notice": False,
                }, None)

    def test_ocr_timeout_zero_recognizes_once(self):
        # timeout_ms=0：只识别一次，不轮询。
        player = MacroPlayer()
        player._wait = lambda milliseconds: None
        with patch("macroflow.execution.player.recognize_region", return_value="") as recognize:
            result = player._execute_text_ocr({
                "expected_text": "体力不足",
                "timeout_ms": 0,
                "interval_ms": 300,
                "on_found": "continue",
                "on_timeout": "continue",
                "show_result_notice": False,
            }, None)
        self.assertIsNone(result)
        recognize.assert_called_once()

    def test_ocr_retries_until_hit(self):
        # 未命中时按检测间隔轮询，直到命中为止。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        results = ["", "", "体力不足，请补充"]
        with patch("macroflow.execution.player.recognize_region", side_effect=lambda _region: results.pop(0)), \
             patch("macroflow.execution.player.time.perf_counter", side_effect=[100.0, 100.1, 100.2, 100.3]):
            result = player._execute_text_ocr({
                "expected_text": "体力不足",
                "timeout_ms": 3000,
                "interval_ms": 300,
                "on_found": "continue",
                "on_timeout": "continue",
                "show_result_notice": False,
            }, None)
        self.assertIsNone(result)
        self.assertEqual(waits, [300, 300, 0])

    def test_ocr_custom_region_scaled_and_passed(self):
        # 自定义区域经 DPI 缩放后传给截屏识别。
        player = MacroPlayer()
        player._wait = lambda milliseconds: None
        with patch("macroflow.execution.player.recognize_region") as recognize:
            recognize.return_value = "命中文字"
            player._execute_text_ocr({
                "region_mode": "custom",
                "region": [10, 20, 300, 400],
                "expected_text": "命中",
                "timeout_ms": 0,
                "on_found": "continue",
                "on_timeout": "continue",
                "show_result_notice": False,
            }, None)
        # 未配置源/目标屏幕时缩放为恒等：区域原样传给识别。
        recognize.assert_called_once_with((10, 20, 300, 400))

    def test_ocr_window_region_uses_bound_window_rect(self):
        # 绑定窗口模式：使用目标窗口矩形做识别区域。
        player = MacroPlayer()
        player._wait = lambda milliseconds: None
        with patch("macroflow.execution.player.recognize_region") as recognize, \
             patch("macroflow.execution.player.get_window_rect", return_value=(1, 2, 800, 600)):
            recognize.return_value = "命中文字"
            player._execute_text_ocr({
                "region_mode": "window",
                "expected_text": "命中",
                "timeout_ms": 0,
                "on_found": "continue",
                "on_timeout": "continue",
                "show_result_notice": False,
            }, 12345)
        recognize.assert_called_once_with((1, 2, 800, 600))

    def test_image_fallback_without_click_only_detects(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 3 else dict(main_match)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button") as button:
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "fallback_click": False,
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        self.assertEqual(
            [Path(call).name for call in calls],
            ["main.png", "fallback.png", "main.png"],
        )
        move.assert_not_called()
        button.assert_not_called()

    def test_image_fallback_exit_ends_detection(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        calls = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button") as button:
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "fallback_on_match": "直接退出识别",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        # 退出后不再继续检测主模板，也不再检测备用模板。
        self.assertEqual(
            [Path(call).name for call in calls],
            ["main.png", "fallback.png"],
        )
        move.assert_called_once_with(200, 300)
        button.assert_called()

    def test_image_fallback_exit_without_click(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button") as button:
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "fallback_click": False,
                    "fallback_on_match": "直接退出识别",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        move.assert_not_called()
        button.assert_not_called()

    def test_image_fallback_returns_to_main_detection(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 3 else dict(main_match)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button"):
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "fallback_on_match": "回到主模板的检测",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        self.assertEqual(
            [Path(call).name for call in calls],
            ["main.png", "fallback.png", "main.png"],
        )
        move.assert_called_once_with(200, 300)

    def test_repeat_click_clicks_count_times_with_interval(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button") as button:
            player._execute_action({
                "type": "repeat_click", "button": "left",
                "x": 100, "y": 200,
                "count": 3, "interval_ms": 50, "hold_ms": 30,
            }, None, False)
        self.assertEqual(move.call_args_list, [((100, 200),)] * 3)
        self.assertEqual(
            [call.args for call in button.call_args_list],
            [("left", True), ("left", False)] * 3,
        )
        # 每次点击 hold 30 ms，点击之间间隔 50 ms。
        self.assertEqual(waits, [30, 50, 30, 50, 30])

    def test_repeat_click_min_count_and_zero_interval(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.send_move_absolute"), \
             patch("macroflow.execution.player.send_button") as button:
            player._execute_action({
                "type": "repeat_click", "button": "right",
                "x": 5, "y": 6,
                "count": 0, "interval_ms": 0, "hold_ms": 10,
            }, None, False)
        # count 至少 1 次；间隔 0 时只在 hold 之间等待。
        self.assertEqual(len(button.call_args_list), 2)
        self.assertEqual(waits, [10])

    def test_repeat_click_aborts_when_stop_requested(self):
        player = MacroPlayer()
        player.stop_event.set()
        with patch("macroflow.execution.player.send_move_absolute"), \
             patch("macroflow.execution.player.send_button") as button:
            with self.assertRaises(PlaybackStopped):
                player._execute_action({
                    "type": "repeat_click", "x": 1, "y": 2,
                    "count": 5, "interval_ms": 10, "hold_ms": 5,
                }, None, False)
        button.assert_not_called()

    def test_image_wait_forever_fallback_loops_back_to_main(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")
            sequence = iter([
                None, dict(fallback_match), None, dict(fallback_match), dict(main_match),
            ])

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                return next(sequence)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button"):
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        self.assertEqual(
            [Path(call).name for call in calls],
            ["main.png", "fallback.png", "main.png", "fallback.png", "main.png"],
        )
        self.assertEqual(move.call_args_list, [((200, 300),), ((200, 300),)])

    def test_image_wait_forever_fallback_uses_its_own_region(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        main_attempts = {"count": 0}
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append((Path(template).name, region))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                main_attempts["count"] += 1
                return None if main_attempts["count"] == 1 else dict(main_match)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute"), \
                 patch("macroflow.execution.player.send_button"):
                result = player._execute_image({
                    "template": str(main_png),
                    "region_mode": "custom", "region": [0, 0, 100, 100],
                    "fallback_template": str(fallback_png),
                    "fallback_region_mode": "custom",
                    "fallback_region": [10, 20, 30, 40],
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "continue",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        self.assertEqual(calls[0], ("main.png", (0, 0, 100, 100)))
        self.assertEqual(calls[1], ("fallback.png", (10, 20, 30, 40)))
        self.assertEqual(calls[2], ("main.png", (0, 0, 100, 100)))

    def test_image_fallback_active_main_still_detected(self):
        # 备用激活后主模板必须继续一起检测：备用一直不出现，主模板在超时后出现应命中。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        main_attempts = {"count": 0}
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return None
                main_attempts["count"] += 1
                return None if main_attempts["count"] == 1 else dict(main_match)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute"), \
                 patch("macroflow.execution.player.send_button"):
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "click",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        self.assertEqual(
            [Path(call).name for call in calls],
            ["main.png", "fallback.png", "main.png"],
        )

    def test_image_both_detected_main_template_handled_first(self):
        # 主备同时出现时主模板优先：命中后按主模板的 on_found 处理。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        fallback_match = {"x": 190, "y": 290, "width": 20, "height": 20,
                          "center_x": 200, "center_y": 300, "score": 0.95}
        main_match = {"x": 10, "y": 20, "width": 30, "height": 40,
                      "center_x": 25, "center_y": 40, "score": 0.9}
        calls = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            main_png = Path(folder) / "main.png"
            fallback_png = Path(folder) / "fallback.png"
            main_png.write_bytes(b"x")
            fallback_png.write_bytes(b"x")

            def fake_find(template, threshold, region, ignore_background=False, scale=1.0):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 2 else dict(main_match)

            with patch("macroflow.execution.player.find_template", side_effect=fake_find), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button") as button:
                result = player._execute_image({
                    "template": str(main_png),
                    "fallback_template": str(fallback_png),
                    "fallback_switch_ms": 0,
                    "timeout_ms": 0,
                    "wait_forever": True,
                    "on_found": "click",
                    "show_result_notice": False,
                    "interval_ms": 100,
                }, None)
        self.assertIsNone(result)
        # 第一次迭代备用命中点击 (200,300)；第二次主命中点击主模板中心 (25,40)。
        self.assertEqual(move.call_args_list, [((200, 300),), ((25, 40),)])

    def test_image_summary_shows_wait_forever_fallback(self):
        _kind, detail, _delay = action_summary({
            "type": "image_match", "template": "images/x.png",
            "wait_forever": True, "on_found": "continue",
            "fallback_template": "images/y.png", "fallback_switch_ms": 5000,
        })
        self.assertIn("一直等待", detail)
        self.assertIn("备用", detail)
        self.assertIn("y.png", detail)
        self.assertIn("5000", detail)

    def test_image_summary_ignores_fallback_without_wait_forever(self):
        _kind, detail, _delay = action_summary({
            "type": "image_match", "template": "images/x.png",
            "on_found": "continue", "fallback_template": "images/y.png",
        })
        self.assertNotIn("备用", detail)

    def test_global_module_row_summary_shows_jump_row(self):
        # v1.68：普通脚本内嵌全局模块行显示跳转行（启用跳转时）。
        kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 3, "jump_enabled": True, "hold_ms": 1500, "threshold": 0.9,
        })
        self.assertIn("全局模块", kind)
        self.assertIn("触发后跳转到第 3 行", detail)
        self.assertIn("g.png", detail)
        self.assertNotIn("点击", detail)

    def test_global_module_row_summary_shows_jump_disabled(self):
        # 未勾选“启用触发后跳转”：摘要显示触发后不跳转。
        _kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 3, "jump_action_id": "target-a",
            "jump_enabled": False, "hold_ms": 1500, "threshold": 0.9,
        })
        self.assertIn("触发后不跳转，继续执行", detail)
        self.assertNotIn("跳转到第 3 行", detail)
        self.assertNotIn("点击", detail)
        # 缺失字段同样按默认不启用处理。
        _kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 3, "hold_ms": 1500,
        })
        self.assertIn("触发后不跳转，继续执行", detail)

    def test_module_ref_summary_shows_legacy_jump(self):
        # 引用模块行沿用旧引擎跳转语义：缺失 jump_enabled 也按启用显示
        # （该行没有“启用触发后跳转”开关）。
        module_obj = {
            "enabled": True, "category": "script_global", "name": "结算确定",
            "template": "images/g.png", "after_action": "click_match",
            "click_count": 2,
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj):
            kind, detail, _delay = action_summary({
                "type": "global_detect", "template": "images/g.png",
                "module_ref": True, "module_key": "images/g.png",
                "jump_row": 3, "jump_action_id": "target-a",
                "hold_ms": 1000, "threshold": 0.9,
            }, {"target-a": 6})
        self.assertIn("脚本全局模块", kind)
        self.assertIn("触发后跳转到第 6 行", detail)
        self.assertIn("点击识别区域", detail)

    def test_module_ref_summary_shows_deleted_jump_target(self):
        module_obj = {
            "enabled": True, "category": "script_global", "name": "结算确定",
            "template": "images/g.png", "after_action": "click_match",
        }
        with patch("macroflow.ui.app.registered_module_object", return_value=module_obj):
            _kind, detail, _delay = action_summary({
                "type": "global_detect", "template": "images/g.png",
                "module_ref": True, "module_key": "images/g.png",
                "jump_row": 3, "jump_action_id": "deleted-target",
                "hold_ms": 1000, "threshold": 0.9,
            }, {"target-a": 6})
        self.assertIn("触发后跳转目标已删除", detail)

    def test_global_module_row_summary_resolves_row_object(self):
        # v1.70：跳转目标是行的对象，摘要按动作标识解析到当前行号。
        action_rows = {"target-a": 6, "target-b": 2}
        _kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 1, "jump_action_id": "target-a", "jump_enabled": True,
        }, action_rows)
        self.assertIn("触发后跳转到第 6 行", detail)
        # 目标行被删除：明确提示而不是显示旧行号。
        _kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 3, "jump_action_id": "deleted-target", "jump_enabled": True,
        }, action_rows)
        self.assertIn("触发后跳转目标已删除", detail)

    def test_image_summary_shows_fallback_click_and_continue(self):
        _kind, detail, _delay = action_summary({
            "type": "image_match", "template": "images/x.png",
            "wait_forever": True, "on_found": "continue",
            "fallback_template": "images/y.png", "fallback_switch_ms": 5000,
            "fallback_click": False,
        })
        self.assertIn("不点击", detail)
        self.assertIn("出现后回到主模板检测", detail)

    def test_image_summary_shows_fallback_exit(self):
        _kind, detail, _delay = action_summary({
            "type": "image_match", "template": "images/x.png",
            "wait_forever": True, "on_found": "continue",
            "fallback_template": "images/y.png", "fallback_switch_ms": 5000,
            "fallback_click": True, "fallback_on_match": "直接退出识别",
        })
        self.assertIn("点击", detail)
        self.assertIn("出现后退出识别", detail)

    def test_image_summary_shows_wait_forever(self):
        kind, detail, _delay = action_summary({
            "type": "image_match", "template": "images/x.png",
            "wait_forever": True, "on_found": "continue",
        })
        self.assertIn("识图", kind)
        self.assertIn("一直等待", detail)
        self.assertIn("不超时", detail)

    def test_image_found_jump_waits_found_delay(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", return_value=match):
            result = player._execute_image({
                "template": "images/目标.png",
                "on_found": "jump",
                "found_jump_row": 3,
                "found_delay_ms": 900,
                "show_result_notice": False,
            }, None)
        self.assertEqual(result, ("row", 3))
        self.assertIn(900, waits)

    def test_image_found_can_finish_current_script_for_next_workflow_step(self):
        player = MacroPlayer()
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", return_value=match), \
             patch("macroflow.execution.player.show_overlay"):
            result = player._execute_image({
                "template": "images/目标.png",
                "on_found": "jump",
                "found_jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
                "found_delay_ms": 0,
                "show_result_notice": False,
            }, None)
        self.assertEqual(result, ("next_workflow_step", 0))

    def test_finish_current_script_skips_remaining_actions_and_repeats(self):
        player = MacroPlayer()
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        repeats_started = []
        repeats_completed = []
        actions_seen = []
        statuses = []
        player.on_status = statuses.append
        with patch("macroflow.execution.player.find_template", return_value=match) as find, \
             patch("macroflow.execution.player.show_overlay"):
            player.play([
                {
                    "type": "image_match", "template": "images/目标.png",
                    "delay_ms": 0, "on_found": "jump",
                    "found_jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
                    "found_delay_ms": 0, "show_result_notice": False,
                },
                {"type": "comment", "text": "不应执行"},
            ], repeats=3,
                on_repeat=lambda current, total: repeats_started.append((current, total)),
                on_repeat_complete=lambda current, total: repeats_completed.append((current, total)),
                on_action=lambda next_index, total: actions_seen.append((next_index, total)))
        self.assertEqual(find.call_count, 1)
        self.assertEqual(repeats_started, [(1, 3)])
        self.assertEqual(repeats_completed, [(1, 3)])
        self.assertEqual(actions_seen, [(1, 2)])
        self.assertTrue(any("执行工作流下一项" in text for text in statuses))

    def test_image_summary_shows_finish_current_script(self):
        _kind, detail, _delay = action_summary({
            "type": "image_match", "template": "images/x.png",
            "on_found": "jump",
            "found_jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
        })
        self.assertIn("结束当前脚本", detail)
        self.assertIn("工作流下一项", detail)

    def test_image_action_waits_after_execution_before_next_action(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", return_value=match):
            player.play([
                {
                    "type": "image_match", "template": "images/目标.png",
                    "delay_ms": 0, "timeout_ms": 0,
                    "on_found": "continue", "after_delay_ms": 700,
                },
                {"type": "notice", "text": "之后执行", "duration_ms": 1},
            ])
        self.assertEqual(notices, [("之后执行", 500)])
        self.assertIn(700, waits)

    def test_module_continue_runs_post_code_before_returning_to_script(self):
        player = MacroPlayer()
        order = []
        player._run_action_sequence = Mock(side_effect=lambda *_args, **_kwargs: order.append("segment"))
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        obj = {
            "after_action": "continue", "run_code_after_action": True,
            "on_success_actions": [{"type": "delay", "ms": 1}],
        }
        result = player._after_module_success(obj, match, None, None, 0)
        self.assertIsNone(result)
        self.assertEqual(order, ["segment"])
        player._run_action_sequence.assert_called_once_with(
            obj["on_success_actions"], None, script_stack=None, depth=1,
        )

    def test_module_post_code_special_restart_takes_effect(self):
        requests = []
        player = MacroPlayer(
            on_restart_workflow_request=lambda action: requests.append(action) or True,
        )
        obj = {
            "after_action": "continue", "run_code_after_action": True,
            "on_success_actions": [{"type": "restart_workflow"}],
        }
        with self.assertRaises(PlaybackStopped):
            player._after_module_success(
                obj, {"center_x": 1, "center_y": 2}, None, None, 0,
            )
        self.assertEqual(requests[0]["type"], "restart_workflow")

    def test_module_timeout_writes_log_with_module_name(self):
        logs = []
        player = MacroPlayer(on_log=lambda text: logs.append(text))
        module_obj = {
            "name": "专注", "template": "images/专注.png",
            "region": [0, 0, 0, 0], "blocking": False, "interval_ms": 50,
            "threshold": 0.85, "run_code_on_timeout": True,
            "not_found_timeout_ms": 0,
            "on_timeout_actions": [{"type": "delay", "ms": 1}],
        }
        actions = [{
            "type": "image_match", "template": "images/专注.png",
            "module_key": "module:专注", "module_ref": True,
            "region_mode": "template", "delay_ms": 0,
        }]
        with patch("macroflow.execution.player.registered_module_object", return_value=module_obj), \
             patch("macroflow.execution.player.find_template", return_value=None):
            player.play(actions)
        self.assertTrue(
            any("模块 专注 连续" in text and "未识别到" in text
                and "执行超时代码段" in text for text in logs),
            f"日志应包含模块超时原因，实际：{logs}",
        )

    def test_module_success_signal_jumps_to_stable_row_object(self):
        logs = []
        notices = []
        player = MacroPlayer(
            on_log=logs.append,
            on_notice=lambda text, _duration: notices.append(text),
        )
        module = {
            "name": "主线关卡", "template": "images/主线关卡.png",
            "region": [], "blocking": False, "interval_ms": 50,
            "threshold": 0.85, "delay_ms": 0,
            "after_action": "continue", "run_code_after_action": False,
        }
        match = {
            "x": 1, "y": 2, "width": 3, "height": 4,
            "center_x": 2, "center_y": 4, "score": 0.95,
        }
        actions = [
            {
                "type": "image_match", "module_ref": True,
                "module_key": "module:main", "template": "images/主线关卡.png",
                "delay_ms": 0, "on_found": "jump",
                "found_jump_action_id": "target",
            },
            {"type": "notice", "text": "不应执行", "action_id": "middle"},
            {"type": "notice", "text": "目标已执行", "action_id": "target"},
        ]
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.find_template", return_value=match), \
             patch("macroflow.execution.player.show_overlay"):
            player.play(actions)

        self.assertEqual(notices, ["目标已执行"])
        self.assertIn("模块 主线关卡 执行结果：成功", logs)

    def test_module_failure_signal_uses_object_timeout_then_jumps(self):
        logs = []
        notices = []
        player = MacroPlayer(
            on_log=logs.append,
            on_notice=lambda text, _duration: notices.append(text),
        )
        module = {
            "name": "主线关卡", "template": "images/主线关卡.png",
            "region": [], "blocking": False, "interval_ms": 50,
            "threshold": 0.85, "after_action": "continue",
            "run_code_after_action": False, "run_code_on_timeout": False,
            "not_found_timeout_ms": 0, "on_timeout_actions": [],
        }
        actions = [
            {
                "type": "image_match", "module_ref": True,
                "module_key": "module:main", "template": "images/主线关卡.png",
                "delay_ms": 0, "on_timeout": "jump",
                "timeout_jump_action_id": "target",
            },
            {"type": "notice", "text": "不应执行", "action_id": "middle"},
            {"type": "notice", "text": "失败目标已执行", "action_id": "target"},
        ]
        with patch("macroflow.execution.player.registered_module_object", return_value=module), \
             patch("macroflow.execution.player.find_template", return_value=None):
            player.play(actions)

        self.assertEqual(notices, ["失败目标已执行"])
        self.assertIn("模块 主线关卡 执行结果：失败", logs)

    def test_module_result_can_end_current_script_on_success_or_failure(self):
        match = {
            "x": 1, "y": 2, "width": 3, "height": 4,
            "center_x": 2, "center_y": 4, "score": 0.95,
        }
        success_module = {
            "name": "成功模块", "template": "images/s.png", "region": [],
            "blocking": False, "interval_ms": 50, "threshold": 0.85,
            "delay_ms": 0, "after_action": "continue", "run_code_after_action": False,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=success_module), \
             patch("macroflow.execution.player.find_template", return_value=match), \
             patch("macroflow.execution.player.show_overlay"):
            ended_on_success = MacroPlayer().play([
                {
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:s", "template": "images/s.png",
                    "delay_ms": 0, "on_found": "end_current_script",
                },
                {"type": "unknown_must_be_skipped"},
            ])
        failure_module = dict(
            success_module, name="失败模块", run_code_on_timeout=True,
            not_found_timeout_ms=0, on_timeout_actions=[],
        )
        with patch("macroflow.execution.player.registered_module_object", return_value=failure_module), \
             patch("macroflow.execution.player.find_template", return_value=None):
            ended_on_failure = MacroPlayer().play([
                {
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:f", "template": "images/f.png",
                    "delay_ms": 0, "on_timeout": "end_current_script",
                },
                {"type": "unknown_must_be_skipped"},
            ])

        self.assertTrue(ended_on_success)
        self.assertTrue(ended_on_failure)

    def test_module_success_logs_click_and_segment_with_name(self):
        logs = []
        player = MacroPlayer(on_log=lambda text: logs.append(text))
        player._run_action_sequence = Mock()
        obj = {
            "name": "结算确定", "after_action": "click_match",
            "run_code_after_action": True,
            "on_success_actions": [{"type": "delay", "ms": 1}],
        }
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.send_move_absolute"), patch("macroflow.execution.player.send_button"):
            player._after_module_success(obj, match, None, None, 0)
        self.assertTrue(
            any("模块 结算确定 已点击 (25, 40)" in text for text in logs),
            f"日志应包含模块点击，实际：{logs}",
        )
        self.assertTrue(
            any("模块 结算确定 主动作完成，执行附加代码段" in text for text in logs),
            f"日志应包含附加代码段执行，实际：{logs}",
        )

    def test_module_success_respects_click_count(self):
        logs = []
        player = MacroPlayer(on_log=logs.append)
        player._wait = Mock()
        obj = {
            "name": "连续领取", "after_action": "click_match",
            "button": "left", "click_count": 3,
        }
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button") as button:
            player._after_module_success(obj, match, None, None, 0)
        move.assert_called_once_with(25, 40)
        self.assertEqual(button.call_count, 6)
        self.assertIn("模块 连续领取 已点击 (25, 40) × 3", logs)

    def test_module_post_code_can_jump_to_outer_script_last_action(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, _duration: notices.append(text))
        module_obj = {
            "template": "images/module.png", "region": [0, 0, 0, 0],
            "blocking": False, "interval_ms": 50, "threshold": 0.85,
            "after_action": "continue", "run_code_after_action": True,
            "on_success_actions": [
                {"type": "jump_current_script_last"},
                {"type": "notice", "text": "代码段剩余动作"},
            ],
        }
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        actions = [
            {
                "type": "image_match", "template": "images/module.png",
                "module_key": "module:test", "module_ref": True,
                "region_mode": "template", "delay_ms": 0,
            },
            {"type": "notice", "text": "脚本中间动作"},
            {"type": "notice", "text": "脚本最后一行"},
        ]
        with patch("macroflow.execution.player.registered_module_object", return_value=module_obj), \
             patch("macroflow.execution.player.find_template", return_value=match), \
             patch("macroflow.execution.player.show_overlay"):
            player.play(actions)

        self.assertEqual(notices, ["脚本最后一行"])

    def test_jump_to_last_does_not_repeat_module_when_module_is_already_last(self):
        player = MacroPlayer()
        module_obj = {
            "template": "images/module.png", "region": [0, 0, 0, 0],
            "blocking": False, "interval_ms": 50, "threshold": 0.85,
            "after_action": "continue", "run_code_after_action": True,
            "on_success_actions": [{"type": "jump_current_script_last"}],
        }
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module_obj), \
             patch("macroflow.execution.player.find_template", return_value=match) as find, \
             patch("macroflow.execution.player.show_overlay"):
            player.play([{
                "type": "image_match", "template": "images/module.png",
                "module_key": "module:test", "module_ref": True,
                "region_mode": "template", "delay_ms": 0,
            }])
        self.assertEqual(find.call_count, 1)

    def test_module_not_found_timeout_runs_its_own_segment(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._run_action_sequence = Mock()
        timeout_segment = [{"type": "delay", "ms": 25}]
        obj = {
            "blocking": True, "interval_ms": 250, "threshold": 0.85,
            "run_code_on_timeout": True, "not_found_timeout_ms": 0,
            "on_timeout_actions": timeout_segment,
            "run_code_after_action": True,
            "on_success_actions": [{"type": "notice", "text": "不应执行"}],
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=obj), \
             patch("macroflow.execution.player.find_template", return_value=None):
            result = player._execute_image({
                "type": "image_match", "template": "images/missing.png",
                "module_ref": True,
            }, None)
        self.assertIsNone(result)
        player._run_action_sequence.assert_called_once_with(
            timeout_segment, None, script_stack=None, depth=1,
        )

    def test_module_without_region_does_not_borrow_shared_image_region(self):
        player = MacroPlayer()
        player._template_region = Mock(side_effect=AssertionError("不应按共用图片反查区域"))
        obj = {
            "template": "images/shared.png", "region": [0, 0, 0, 0],
            "blocking": False, "interval_ms": 250, "threshold": 0.85,
            "after_action": "continue", "run_code_after_action": False,
        }
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=obj) as lookup, \
             patch("macroflow.execution.player.find_template", return_value=match) as find:
            player._execute_image({
                "type": "image_match", "template": "images/shared.png",
                "module_key": "module:independent", "module_ref": True,
                "region_mode": "template",
            }, None)

        lookup.assert_called_once_with("module:independent")
        self.assertIsNone(find.call_args.args[2])
        player._template_region.assert_not_called()

    def test_activate_window_action_resolves_saved_signature(self):
        player = MacroPlayer()
        target = WindowInfo(456, "游戏窗口", "GameWnd", r"C:\\Game\\game.exe")
        with patch("macroflow.execution.player.resolve_window_signature", return_value=target), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate:
            player._execute_action({
                "type": "activate_window",
                "window": {
                    "title": target.title,
                    "class_name": target.class_name,
                    "process_path": target.process_path,
                },
            }, None, False)
        activate.assert_not_called()
        self.assertEqual(player._relative_target_hwnd, 456)

    def test_end_current_script_action_skips_remaining_actions(self):
        statuses = []
        player = MacroPlayer(on_status=statuses.append)

        advanced = player.play([
            {"type": "end_current_script"},
            {"type": "unknown_must_be_skipped"},
        ])

        self.assertTrue(advanced)
        self.assertIn("已结束当前最里层脚本，继续执行", statuses)

    def test_end_current_script_in_module_segment_reaches_script_boundary(self):
        player = MacroPlayer()
        module = {
            "name": "结束内层",
            "after_action": "continue",
            "run_code_after_action": True,
            "on_success_actions": [{"type": "end_current_script"}],
        }
        match = {
            "x": 1, "y": 2, "width": 3, "height": 4,
            "center_x": 2, "center_y": 4,
        }

        with self.assertRaises(EndCurrentScriptRequest):
            player._after_module_success(
                module, match, None, script_stack=None, depth=0,
            )

    def test_jump_action_uses_stable_target_action_id(self):
        player = MacroPlayer()
        waits = []
        player._wait = waits.append
        advanced = player.play([
            {"type": "jump", "jump_action_id": "target", "delay_ms": 0},
            {"type": "delay", "ms": 25, "action_id": "target", "delay_ms": 0},
        ])
        self.assertFalse(advanced)
        self.assertIn(25, waits)

    def test_jump_action_supports_script_start_and_end(self):
        player = MacroPlayer()
        self.assertEqual(
            player._execute_action(
                {"type": "jump", "jump_action_id": SCRIPT_START_TARGET_ID,
                 "workflow_repeat_at_least_2": False},
                None, False,
            ),
            ("row", 1),
        )
        self.assertEqual(
            player._execute_action(
                {"type": "jump", "jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
                 "workflow_repeat_at_least_2": False},
                None, False,
            ),
            ("next_workflow_step", ""),
        )

    def test_conditional_jump_only_applies_from_second_workflow_repeat(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {
                "type": "jump", "action_id": "jump", "jump_action_id": "target",
                "workflow_repeat_at_least_2": True,
            },
            {"type": "comment", "action_id": "middle"},
            {"type": "comment", "action_id": "target"},
        ]

        player.play(
            actions, repeats=2, workflow_context=True,
            on_action=lambda next_index, _total: executed.append(next_index),
        )

        self.assertEqual(executed, [1, 2, 3, 1, 3])

    def test_conditional_jump_is_skipped_when_script_runs_standalone(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {
                "type": "jump", "action_id": "jump", "jump_action_id": "target",
                "workflow_repeat_at_least_2": True,
            },
            {"type": "comment", "action_id": "middle"},
            {"type": "comment", "action_id": "target"},
        ]

        player.play(
            actions,
            on_action=lambda next_index, _total: executed.append(next_index),
        )

        self.assertEqual(executed, [1, 2, 3])

    def test_conditional_jump_applies_from_second_repeat_when_standalone(self):
        # 脚本多次执行（重复次数 >1）的第 2 次起生效：单独运行脚本同样适用。
        player = MacroPlayer()
        executed = []
        actions = [
            {
                "type": "jump", "action_id": "jump", "jump_action_id": "target",
                "workflow_repeat_at_least_2": True,
            },
            {"type": "comment", "action_id": "middle"},
            {"type": "comment", "action_id": "target"},
        ]

        player.play(
            actions, repeats=2,
            on_action=lambda next_index, _total: executed.append(next_index),
        )

        self.assertEqual(executed, [1, 2, 3, 1, 3])

    def test_jump_condition_defaults_to_second_repeat_on(self):
        # 缺失 workflow_repeat_at_least_2 字段时按“第 2 次及以后生效”处理。
        player = MacroPlayer()
        executed = []
        actions = [
            {"type": "jump", "action_id": "jump", "jump_action_id": "target"},
            {"type": "comment", "action_id": "middle"},
            {"type": "comment", "action_id": "target"},
        ]

        player.play(
            actions, repeats=2,
            on_action=lambda next_index, _total: executed.append(next_index),
        )

        self.assertEqual(executed, [1, 2, 3, 1, 3])

    def test_script_scope_callbacks_wrap_playback_even_when_starting_later(self):
        events = []
        actions = [
            {"type": "global_detect", "action_id": "global"},
            {"type": "comment", "action_id": "start"},
        ]
        player = MacroPlayer(
            on_script_scope_enter=lambda value: events.append(("enter", value)) or "scope",
            on_script_scope_exit=lambda token: events.append(("exit", token)),
        )

        player.play(actions, start_index=1)

        self.assertEqual(events, [("enter", actions), ("exit", "scope")])

    def test_image_timeout_jump_waits_after_execution(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("macroflow.execution.player.find_template", return_value=None):
            player.play([
                {
                    "type": "image_match", "template": "images/目标.png",
                    "delay_ms": 0, "timeout_ms": 0,
                    "on_timeout": "jump", "timeout_jump_row": 3,
                    "after_delay_ms": 500,
                },
                {"type": "unknown_must_be_skipped"},
                {"type": "notice", "text": "跳转目标", "duration_ms": 1},
            ])
        self.assertEqual(notices, [("跳转目标", 500)])
        self.assertIn(500, waits)

    def test_image_timeout_can_end_top_level_script(self):
        statuses = []
        player = MacroPlayer(on_status=statuses.append)
        with patch("macroflow.execution.player.find_template", return_value=None):
            advanced = player.play([
                {
                    "type": "image_match", "template": "images/目标.png",
                    "delay_ms": 0, "timeout_ms": 0,
                    "on_timeout": "end_current_script",
                },
                {"type": "unknown_must_be_skipped"},
            ])
        self.assertTrue(advanced)
        self.assertIn("识图超时，结束当前脚本", statuses)

    def test_image_timeout_ends_only_current_referenced_script(self):
        notices = []
        with tempfile.TemporaryDirectory() as folder:
            referenced_path = Path(folder) / "referenced.json"
            save_script(MacroScript(name="引用", actions=[
                {
                    "type": "image_match", "template": "images/目标.png",
                    "delay_ms": 0, "timeout_ms": 0,
                    "on_timeout": "end_current_script",
                },
                {"type": "unknown_must_be_skipped"},
            ]), referenced_path)
            player = MacroPlayer(on_notice=lambda text, duration: notices.append(text))
            with patch("macroflow.execution.player.find_template", return_value=None):
                advanced = player.play([
                    {"type": "script_ref", "script": str(referenced_path), "delay_ms": 0},
                    {"type": "notice", "text": "外层继续", "duration_ms": 1},
                ])
        self.assertFalse(advanced)
        self.assertEqual(notices, ["外层继续"])

    def test_global_detect_action_requests_monitor(self):
        requests = []
        player = MacroPlayer(on_global_detect_request=requests.append)
        player.play([
            {
                "type": "global_detect", "template": "images/g.png",
                "delay_ms": 0,
            },
        ])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["type"], "global_detect")

    def test_image_without_saved_delay_uses_new_1000_ms_default(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", return_value=match):
            player.play([{
                "type": "image_match", "template": "images/目标.png",
                "on_found": "continue",
            }])
        self.assertEqual(waits[0], 1000)

    def test_image_success_waits_then_clicks_custom_scaled_point(self):
        player = MacroPlayer()
        player._wait = Mock()
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("macroflow.execution.player.find_template", return_value=match), \
             patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button"):
            player._execute_image({
                "template": "images/目标.png",
                "on_found": "click",
                "found_delay_ms": 1000,
                "click_target": "custom",
                "click_point": [700, 500],
            }, None)
        self.assertEqual(player._wait.call_args_list[0].args, (1000,))
        move.assert_called_once_with(700, 500)

    def test_image_result_notice_reports_success_details(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.936,
        }
        with patch("macroflow.execution.player.find_template", return_value=match):
            player._execute_image({
                "template": "images/目标.png",
                "show_result_notice": True,
                "on_found": "continue",
            }, None)
        self.assertEqual(len(notices), 1)
        self.assertIn("识图成功", notices[0][0])
        self.assertIn("93.6%", notices[0][0])
        self.assertIn("(25, 40)", notices[0][0])

    def test_image_result_notice_reports_timeout_when_continuing(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        with patch("macroflow.execution.player.find_template", return_value=None):
            player._execute_image({
                "template": "images/目标.png",
                "timeout_ms": 0,
                "on_timeout": "continue",
                "show_result_notice": True,
            }, None)
        self.assertEqual(len(notices), 1)
        self.assertIn("识图未找到", notices[0][0])
        self.assertIn("目标.png", notices[0][0])

    def test_text_module_hit_clicks_ocr_box_center_with_offsets(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._status = Mock()
        module_obj = {
            "recognize": "text", "expected_text": "体力不足", "match_mode": "contains",
            "template": "", "region": [10, 20, 300, 400],
            "blocking": False, "interval_ms": 250, "threshold": 0.85,
            "after_action": "click_match", "run_code_after_action": False,
            "delay_ms": 0, "ocr_offset_up": 5, "ocr_offset_down": 15,
            "ocr_offset_left": 20, "ocr_offset_right": 5,
        }
        found = {
            "text": "当前体力不足", "x": 80, "y": 60, "width": 80, "height": 40,
            "center_x": 120, "center_y": 80, "score": 0.99,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module_obj), \
             patch(
                 "macroflow.execution.player.recognize_region_with_boxes",
                 return_value=("当前体力不足", [found]),
             ) as recognize, \
             patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button"):
            player._execute_image({
                "type": "image_match", "template": "", "module_ref": True,
                "module_key": "module:text", "region_mode": "template", "delay_ms": 0,
            }, None)
        recognize.assert_called_once()
        # OCR 中心 (120,80)，水平偏移 5-20=-15，垂直偏移 15-5=+10。
        move.assert_called_once_with(105, 90)
        self.assertTrue(any(
            "识别文字命中" in item.args[0]
            for item in player._status.call_args_list
        ))

    def test_text_module_miss_times_out_and_continues(self):
        player = MacroPlayer()
        player._wait = Mock()
        with patch("macroflow.execution.player.registered_module_object", return_value={
            "recognize": "text", "expected_text": "体力不足", "match_mode": "contains",
            "template": "", "region": [], "blocking": False, "interval_ms": 250,
            "threshold": 0.85, "after_action": "continue", "run_code_after_action": False,
        }), \
             patch(
                 "macroflow.execution.player.recognize_region_with_boxes",
                 return_value=("其他文字", [{"text": "其他文字"}]),
             ):
            result = player._execute_image({
                "type": "image_match", "template": "", "module_ref": True,
                "module_key": "module:text", "region_mode": "template",
                "timeout_ms": 0, "on_timeout": "continue",
            }, None)
        self.assertIsNone(result)

    def test_text_module_miss_writes_actual_text_to_log_and_status(self):
        logs = []
        statuses = []
        player = MacroPlayer(on_status=statuses.append, on_log=logs.append)
        player._wait = Mock()
        with patch("macroflow.execution.player.registered_module_object", return_value={
            "name": "奖励可领取", "recognize": "text",
            "expected_text": "可领取", "match_mode": "contains",
            "template": "", "region": [], "blocking": False,
            "interval_ms": 250, "threshold": 0.85,
            "after_action": "continue", "run_code_after_action": False,
        }), patch(
            "macroflow.execution.player.recognize_region_with_boxes",
            return_value=("可锁取", [{"text": "可锁取", "center_x": 10, "center_y": 20}]),
        ):
            player._execute_image({
                "type": "image_match", "template": "", "module_ref": True,
                "module_key": "module:text", "region_mode": "template",
                "timeout_ms": 0, "on_timeout": "continue",
            }, None)
        expected = "奖励可领取 OCR：识别到「可锁取」；期望「可领取」· 未命中"
        self.assertIn(expected, logs)
        self.assertIn(expected, statuses)

    def test_text_module_waits_until_expected_text_disappears(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._status = Mock()
        player._run_action_sequence = Mock()
        module_obj = {
            "recognize": "text", "expected_text": "加载中", "match_mode": "contains",
            "wait_text_absent": True,
            "template": "", "region": [10, 20, 300, 400],
            "blocking": False, "interval_ms": 250, "threshold": 0.85,
            "after_action": "click_match", "run_code_after_action": False,
            "run_code_on_timeout": True, "not_found_timeout_ms": 0,
            "on_timeout_actions": [{"type": "delay", "ms": 1}], "delay_ms": 0,
            "ocr_offset_up": 2, "ocr_offset_down": 7,
            "ocr_offset_left": 3, "ocr_offset_right": 13,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module_obj), \
             patch(
                 "macroflow.execution.player.recognize_region_with_boxes",
                 side_effect=[
                     ("加载中", [{"text": "加载中", "center_x": 50, "center_y": 60}]),
                     ("仍在加载中", [{"text": "仍在加载中", "center_x": 80, "center_y": 100}]),
                     ("完成", [{"text": "完成", "center_x": 20, "center_y": 30}]),
                 ],
             ) as recognize, \
             patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button"):
            result = player._execute_image({
                "type": "image_match", "template": "", "module_ref": True,
                "module_key": "module:text", "region_mode": "template",
                "timeout_ms": 0, "on_timeout": "continue",
            }, None)
        self.assertIsNone(result)
        self.assertEqual(recognize.call_count, 3)
        self.assertEqual(
            [call.args for call in move.call_args_list],
            [(60, 65), (90, 105)],
        )
        player._run_action_sequence.assert_not_called()
        self.assertTrue(any(
            "结束循环" in item.args[0]
            for item in player._status.call_args_list
        ))

    def test_template_module_repeats_until_target_image_disappears(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._status = Mock()
        module_obj = {
            "template": "images/claim.png", "region": [10, 20, 300, 400],
            "wait_text_absent": True, "blocking": False,
            "interval_ms": 250, "threshold": 0.85, "delay_ms": 0,
            "after_action": "click_match", "run_code_after_action": False,
            "run_code_on_timeout": True, "not_found_timeout_ms": 0,
            "on_timeout_actions": [{"type": "delay", "ms": 1}],
            "button": "left",
        }
        found = {
            "x": 100, "y": 200, "width": 40, "height": 20,
            "center_x": 120, "center_y": 210, "score": 0.96,
        }
        with patch("macroflow.execution.player.registered_module_object", return_value=module_obj), \
             patch("macroflow.execution.player.find_template", side_effect=[found, found, None]) as find, \
             patch("macroflow.execution.player.show_overlay"), \
             patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button"):
            result = player._execute_image({
                "type": "image_match", "template": "images/claim.png",
                "module_ref": True, "module_key": "module:claim",
                "region_mode": "template", "timeout_ms": 0,
                "on_timeout": "continue",
            }, None)

        self.assertIsNone(result)
        self.assertEqual(find.call_count, 3)
        self.assertEqual([call.args for call in move.call_args_list], [(120, 210), (120, 210)])
        self.assertTrue(any(
            "目标模板" in item.args[0] and "结束循环" in item.args[0]
            for item in player._status.call_args_list
        ))

    def test_text_module_blocking_waits_then_runs_timeout_segment(self):
        player = MacroPlayer()
        player._wait = Mock()
        player._run_action_sequence = Mock()
        segment = [{"type": "delay", "ms": 25}]
        with patch("macroflow.execution.player.registered_module_object", return_value={
            "recognize": "text", "expected_text": "体力不足", "match_mode": "contains",
            "template": "", "region": [], "blocking": True, "interval_ms": 250,
            "threshold": 0.85, "after_action": "continue", "run_code_after_action": False,
            "run_code_on_timeout": True, "not_found_timeout_ms": 0,
            "on_timeout_actions": segment,
        }), \
             patch(
                 "macroflow.execution.player.recognize_region_with_boxes",
                 return_value=("其他文字", [{"text": "其他文字"}]),
             ):
            result = player._execute_image({
                "type": "image_match", "template": "", "module_ref": True,
                "module_key": "module:text", "region_mode": "template",
            }, None)
        self.assertIsNone(result)
        player._run_action_sequence.assert_called_once_with(
            segment, None, script_stack=None, depth=1,
        )

    def test_notice_callback_does_not_block(self):
        events = []
        player = MacroPlayer(
            on_notice=lambda text, duration: events.append(("notice", text, duration)),
        )
        player.play([
            {"type": "notice", "text": "只是提醒", "duration_ms": 2500},
        ])
        self.assertEqual(events, [
            ("notice", "只是提醒", 2500),
        ])

    def test_scroll_playback_forwards_dx_and_dy(self):
        # 滚轮动作曾只传 dy：send_scroll(dx, dy) 双参数签名下直接 TypeError，
        # 且纵/横滚轮永远发不出。回归：dx、dy 必须原样传给 send_scroll。
        player = MacroPlayer()
        with patch("macroflow.execution.player.send_scroll") as scroll:
            player.play([
                {"type": "scroll", "dx": 3, "dy": -2, "delay_ms": 0},
            ])
        scroll.assert_called_once_with(3, -2)

    def test_player_template_scale_from_screens(self):
        player = MacroPlayer()
        player._source_screen = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        player._target_screen = {"left": 0, "top": 0, "width": 3840, "height": 2160}
        self.assertEqual(player._template_scale(), 2.0)
        player._source_screen = None
        self.assertEqual(player._template_scale(), 1.0)

    def test_image_action_passes_template_scale_to_matcher(self):
        # 识图动作的模板匹配必须带上录制屏 → 当前屏的缩放系数，
        # 否则截图尺寸不同时匹配度下降（坐标缩放 ≠ 模板缩放）。
        player = MacroPlayer()
        player._source_screen = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        player._target_screen = {"left": 0, "top": 0, "width": 3840, "height": 2160}
        player._wait = Mock()
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "t.png"
            template = np.zeros((8, 8, 3), dtype=np.uint8)
            cv2.imwrite(str(template_path), template)
            with patch("macroflow.execution.player.find_template", return_value=None) as find:
                player._execute_image({
                    "type": "image_match", "template": str(template_path),
                    "timeout_ms": 0, "interval_ms": 50, "threshold": 0.85,
                }, None)
            self.assertGreaterEqual(len(find.call_args_list), 1)
            self.assertEqual(find.call_args.kwargs["scale"], 2.0)

    def test_key_press_skips_zero_vk(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.send_key") as send:
            player.play([{"type": "key_press", "vk": 0, "hold_ms": 10}])
        send.assert_not_called()

    def test_key_press_stop_during_hold_releases_key(self):
        # F12 在按下-抬起窗口内停止：抬起必须补发，不能物理卡键。
        player = MacroPlayer()
        # 第一次 _wait 是动作的延时等待（0ms），第二次是按住等待：在其中停止。
        player._wait = Mock(side_effect=[None, PlaybackStopped()])
        with patch("macroflow.execution.player.send_key") as send:
            player.play([{"type": "key_press", "vk": 65, "hold_ms": 300}])
        self.assertEqual(
            [call.args for call in send.call_args_list], [(65, True), (65, False)],
        )

    def test_click_stop_during_hold_releases_button(self):
        player = MacroPlayer()
        player._wait = Mock(side_effect=[None, PlaybackStopped()])
        with patch("macroflow.execution.player.send_button") as send, \
             patch("macroflow.execution.player.send_move_absolute"):
            player.play([{"type": "click", "x": 10, "y": 20, "hold_ms": 300}])
        self.assertEqual(
            [call.args for call in send.call_args_list],
            [("left", True), ("left", False)],
        )

    def test_guard_jump_inside_nested_sequence_propagates_to_outer_frame(self):
        # 守卫跳转按设计只由最外层动作序列（depth==0）解析：嵌套帧必须
        # 原样抛出，否则会按内层动作列表错误解析目标行。
        hit = {
            "kind": "success", "log_subject": "模块[m] · 图",
            "jump_action_id": "outer-a", "jump_row": 1, "delay_ms": 0,
        }

        player = MacroPlayer(on_guard_poll=lambda: hit)
        actions = [{"type": "delay", "ms": 1, "action_id": "outer-a"}]
        with self.assertRaises(GuardJumpRequest):
            player._run_action_sequence(actions, None, depth=1)

    def test_activation_window_runs_once_then_target_is_raised(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=True), \
             patch("macroflow.execution.player.is_window_process_foreground", return_value=True), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate:
            player.play(
                [{"type": "comment"}], hwnd=123,
                activation_hwnd=456, activate_target=True,
            )
        # 目标窗口已在前台：播放启动不再激活它（程序化激活会让游戏客户端
        # 重弹“点击游戏画面继续操作”），只激活执行前置窗口一次。
        self.assertEqual(activate.call_args_list, [call(456)])

    def test_play_start_raises_target_only_when_not_foreground(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=True), \
             patch("macroflow.execution.player.is_window_process_foreground", side_effect=[False, True]), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate:
            player.play(
                [{"type": "comment"}], hwnd=123,
                activation_hwnd=456, activate_target=True,
            )
        # 播放启动时目标不在前台：激活一次；随后首个动作的前台守卫看到
        # 目标已在前台，不再重复激活（v1.1.0 每个输入动作前的前台校验）。
        self.assertEqual(activate.call_args_list, [call(456), call(123)])

    def test_explicit_activation_window_is_raised_when_target_activation_is_off(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=True), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate:
            player.play(
                [{"type": "comment"}], hwnd=123,
                activation_hwnd=456, activate_target=False,
            )
        activate.assert_called_once_with(456)

    def test_disabled_auto_activation_does_not_raise_target_window(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=True), \
             patch("macroflow.execution.player.is_window_process_foreground", return_value=False), \
             patch("macroflow.execution.player.activate_window") as activate:
            player.play([{"type": "comment"}], hwnd=123, activate_target=False)
        activate.assert_not_called()

    def test_stale_bound_window_does_not_stop_ordinary_actions(self):
        logs = []
        player = MacroPlayer(on_log=logs.append)
        with patch("macroflow.execution.player.is_window", return_value=False), \
             patch("macroflow.execution.player.activate_window") as activate, \
             patch("macroflow.execution.player.send_move_absolute") as move:
            player.play([
                {"type": "mouse_move", "mode": "absolute", "x": 120, "y": 240},
            ], hwnd=123)
        move.assert_called_once_with(120, 240)
        activate.assert_not_called()
        self.assertEqual(logs, [
            "绑定窗口已失效；普通动作继续执行，只有相对转向或窗口区域动作需要重新绑定。",
        ])

    def test_stale_bound_window_relative_action_sends_directly(self):
        # 相对移动是系统级事件（MOUSEEVENTF_MOVE），窗口失效时不再报错，
        # 直接发送到当前前台窗口（通用转向，不区分游戏/桌面窗口）。
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=False), \
             patch("macroflow.execution.player.send_move_relative") as move:
            player.play([
                {"type": "mouse_move", "mode": "relative", "dx": 2, "dy": 3},
            ], hwnd=123)
        move.assert_called_once_with(2, 3)

    def test_relative_action_resolves_game_window_created_after_workflow_start(self):
        player = MacroPlayer(on_target_window_request=Mock(return_value=456))
        with patch("macroflow.execution.player.is_window", side_effect=lambda hwnd: hwnd == 456), \
             patch("macroflow.execution.player.activate_window", return_value=True) as activate, \
             patch("macroflow.execution.player.send_move_relative") as move:
            player.play([
                {"type": "mouse_move", "mode": "relative", "dx": 2, "dy": 3},
            ], hwnd=None)
        player.on_target_window_request.assert_called_once_with()
        activate.assert_not_called()
        move.assert_called_once_with(2, 3)

    def test_relative_move_sends_when_auto_activation_off_and_not_foreground(self):
        # 关闭自动前置且目标窗口不在前台：仅提示，仍直接发送相对移动。
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=True), \
             patch("macroflow.execution.player.is_window_process_foreground", return_value=False), \
             patch("macroflow.execution.player.activate_window") as activate, \
             patch("macroflow.execution.player.send_move_relative") as move:
            player.play(
                [{"type": "mouse_move", "mode": "relative", "dx": 2, "dy": 3}],
                hwnd=123, activate_target=False,
            )
        activate.assert_not_called()
        move.assert_called_once_with(2, 3)

    def test_absolute_coordinates_scale_to_current_resolution(self):
        source = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        target = {"left": 0, "top": 0, "width": 1280, "height": 720}
        self.assertEqual(scale_screen_point(960, 540, source, target), (640, 360))
        player = MacroPlayer()
        with patch("macroflow.execution.player.get_virtual_screen_rect", return_value=target), \
             patch("macroflow.execution.player.send_move_absolute") as move:
            player.play(
                [{"type": "mouse_move", "mode": "absolute", "x": 960, "y": 540}],
                source_screen=source,
            )
        move.assert_called_once_with(640, 360)

    def test_second_match_can_click_first_match_position(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            second_path = Path(folder) / "second.png"
            second_path.write_bytes(b"x")
            player = MacroPlayer()
            player._wait = Mock()
            first = {"center_x": 111, "center_y": 222}
            second = {
                "x": 10, "y": 20, "width": 30, "height": 40,
                "center_x": 25, "center_y": 40, "score": 0.9,
            }
            obj = {
                "second_match_template": str(second_path),
                "second_match_click_target": "first",
            }
            with patch("macroflow.execution.player.registered_template_region", return_value=[5, 6, 70, 80]), \
                 patch("macroflow.execution.player.find_template", return_value=second) as find, \
                 patch("macroflow.execution.player.show_overlay"), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button"):
                player._execute_second_match(obj, None, first)
            find.assert_called_once_with(second_path, 0.85, (5, 6, 70, 80),
                                         ignore_background=False, scale=1.0)
            move.assert_called_once_with(111, 222)

    def test_second_match_can_click_custom_region_center(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            second_path = Path(folder) / "second.png"
            second_path.write_bytes(b"x")
            player = MacroPlayer()
            player._wait = Mock()
            player._source_screen = None
            player._target_screen = None
            second = {
                "x": 10, "y": 20, "width": 30, "height": 40,
                "center_x": 25, "center_y": 40, "score": 0.9,
            }
            obj = {
                "second_match_template": str(second_path),
                "second_match_click_target": "custom_region",
                "second_match_click_region": [100, 200, 80, 40],
            }
            with patch("macroflow.execution.player.find_template", return_value=second), \
                 patch("macroflow.execution.player.show_overlay"), \
                 patch("macroflow.execution.player.send_move_absolute") as move, \
                 patch("macroflow.execution.player.send_button"):
                player._execute_second_match(obj, None)
            move.assert_called_once_with(140, 220)

    def test_invalid_recorded_resolution_does_not_change_coordinates(self):
        self.assertEqual(
            scale_screen_point(320, 240, {"width": 0, "height": 0}, {"width": 1280, "height": 720}),
            (320, 240),
        )

    def test_delay_and_comment(self):
        player = MacroPlayer()
        start = time.perf_counter()
        player.play([{"type": "delay", "ms": 15}, {"type": "comment", "text": "ok"}])
        self.assertGreaterEqual(time.perf_counter() - start, 0.01)

    def test_repeat_callback_reports_current_and_total(self):
        player = MacroPlayer()
        progress = []
        player.play([{"type": "delay", "ms": 0}], repeats=3,
                    on_repeat=lambda current, total: progress.append((current, total)))
        self.assertEqual(progress, [(1, 3), (2, 3), (3, 3)])

    def test_play_start_repeat_skips_earlier_repeats(self):
        player = MacroPlayer()
        progress = []
        completed = []
        player.play(
            [{"type": "delay", "ms": 0}], repeats=5, start_repeat=2,
            on_repeat=lambda current, total: progress.append((current, total)),
            on_repeat_complete=lambda current, total: completed.append((current, total)),
        )
        # 从第 3 次开始执行：只运行 3、4、5 次，但回调仍报告正确的总数。
        self.assertEqual(progress, [(3, 5), (4, 5), (5, 5)])
        self.assertEqual(completed, [(3, 5), (4, 5), (5, 5)])

    def test_play_start_repeat_clamps_out_of_range(self):
        player = MacroPlayer()
        progress = []
        player.play([{"type": "delay", "ms": 0}], repeats=2, start_repeat=99,
                    on_repeat=lambda current, total: progress.append((current, total)))
        self.assertEqual(progress, [(2, 2)])

    def test_play_click_current_position_uses_cursor(self):
        # 点击鼠标当前位置：不移动光标、不缩放，鼠标在哪就在哪点击。
        player = MacroPlayer()
        with patch("macroflow.execution.player.get_cursor_pos", return_value=(777, 888)) as cursor, \
             patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button") as button:
            player.play(
                [{"type": "click", "button": "left", "pos_mode": "current",
                  "hold_ms": 0, "delay_ms": 0}],
            )
        cursor.assert_not_called()
        move.assert_not_called()
        self.assertEqual(
            [call.args for call in button.call_args_list],
            [("left", True), ("left", False)],
        )

    def test_play_click_fixed_position_moves_and_scales(self):
        player = MacroPlayer()
        player._scale_point = Mock(side_effect=lambda x, y: (x * 2, y * 2))
        with patch("macroflow.execution.player.send_move_absolute") as move, \
             patch("macroflow.execution.player.send_button") as button:
            player.play(
                [{"type": "click", "button": "left", "x": 50, "y": 60,
                  "hold_ms": 0, "delay_ms": 0}],
            )
        move.assert_called_once_with(100, 120)
        self.assertEqual(
            [call.args for call in button.call_args_list],
            [("left", True), ("left", False)],
        )

    def test_play_start_repeat_zero_runs_all(self):
        player = MacroPlayer()
        progress = []
        player.play([{"type": "delay", "ms": 0}], repeats=2, start_repeat=0,
                    on_repeat=lambda current, total: progress.append((current, total)))
        self.assertEqual(progress, [(1, 2), (2, 2)])

    def test_play_resume_action_continues_from_next_action(self):
        player = MacroPlayer()
        executed = []
        player.play(
            [{"type": "delay", "ms": 0}] * 3, repeats=2, resume_action_index=1,
            on_action=lambda next_index, total: executed.append(next_index),
        )
        # 第一次重复从动作 2 的下一个动作继续（动作 2、3），第二次重复从头执行。
        self.assertEqual(executed, [2, 3, 1, 2, 3])

    def test_play_resume_action_combines_with_start_repeat(self):
        player = MacroPlayer()
        executed = []
        player.play(
            [{"type": "delay", "ms": 0}] * 4, repeats=4, start_repeat=2,
            resume_action_index=1,
            on_action=lambda next_index, total: executed.append(next_index),
        )
        # 第 3 次重复从动作 2 继续，第 4 次从头。
        self.assertEqual(executed, [2, 3, 4, 1, 2, 3, 4])

    def test_play_resume_action_at_script_end_skips_finished_repeat(self):
        player = MacroPlayer()
        executed = []
        player.play(
            [{"type": "delay", "ms": 0}] * 3, repeats=2, resume_action_index=3,
            on_action=lambda next_index, total: executed.append(next_index),
        )
        # 被打断重复的动作已全部完成：跳过该次，从下一次重复的第一个动作开始。
        self.assertEqual(executed, [1, 2, 3])

    def test_play_resume_action_last_repeat_done_completes_immediately(self):
        player = MacroPlayer()
        executed = []
        player.play(
            [{"type": "delay", "ms": 0}] * 2, repeats=2, start_repeat=1,
            resume_action_index=2,
            on_action=lambda next_index, total: executed.append(next_index),
        )
        self.assertEqual(executed, [])

    def test_repeat_start_action_id_starts_later_repeats_from_row(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {"type": "delay", "ms": 0, "action_id": f"id{i}"}
            for i in range(6)
        ]
        player.play(
            actions, repeats=5, repeat_start_action_id="id4",
            on_action=lambda next_index, total: executed.append(next_index),
        )
        # 第 1 次从头（1..6）；第 2-5 次从第 5 行（index 4）开始。
        self.assertEqual(
            executed,
            [1, 2, 3, 4, 5, 6, 5, 6, 5, 6, 5, 6, 5, 6],
        )

    def test_repeat_start_action_id_no_effect_on_single_repeat(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {"type": "delay", "ms": 0, "action_id": f"id{i}"}
            for i in range(6)
        ]
        player.play(
            actions, repeats=1, repeat_start_action_id="id4",
            on_action=lambda next_index, total: executed.append(next_index),
        )
        self.assertEqual(executed, [1, 2, 3, 4, 5, 6])

    def test_repeat_start_action_id_missing_falls_back_to_first_row(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {"type": "delay", "ms": 0, "action_id": f"id{i}"}
            for i in range(6)
        ]
        player.play(
            actions, repeats=5, repeat_start_action_id="missing",
            on_action=lambda next_index, total: executed.append(next_index),
        )
        self.assertEqual(executed, [1, 2, 3, 4, 5, 6] * 5)

    def test_repeat_start_action_id_combines_with_resume(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {"type": "delay", "ms": 0, "action_id": f"id{i}"}
            for i in range(6)
        ]
        player.play(
            actions, repeats=5, start_repeat=2, resume_action_index=1,
            repeat_start_action_id="id4",
            on_action=lambda next_index, total: executed.append(next_index),
        )
        # 第 3 次从断点（动作 2）继续；第 4-5 次从指定行（第 5 行）开始。
        self.assertEqual(executed, [2, 3, 4, 5, 6, 5, 6, 5, 6])

    def test_repeat_start_action_id_with_overflow_resume_skips_finished_repeat(self):
        player = MacroPlayer()
        executed = []
        actions = [
            {"type": "delay", "ms": 0, "action_id": f"id{i}"}
            for i in range(6)
        ]
        player.play(
            actions, repeats=5, start_repeat=2, resume_action_index=6,
            repeat_start_action_id="id4",
            on_action=lambda next_index, total: executed.append(next_index),
        )
        # 被打断的重复已全部完成：跳过；余下重复都从指定行（第 5 行）开始。
        self.assertEqual(executed, [5, 6, 5, 6])

    def test_on_action_reports_next_action_index_and_total(self):
        player = MacroPlayer()
        reports = []
        player.play(
            [{"type": "delay", "ms": 0}, {"type": "comment"}],
            on_action=lambda next_index, total: reports.append((next_index, total)),
        )
        self.assertEqual(reports, [(1, 2), (2, 2)])

    def test_repeat_complete_callback_only_runs_after_successful_repeat(self):
        player = MacroPlayer()
        completed = []
        player.play(
            [{"type": "delay", "ms": 0}], repeats=3,
            on_repeat_complete=lambda current, total: completed.append((current, total)),
        )
        self.assertEqual(completed, [(1, 3), (2, 3), (3, 3)])

        failed = []
        with self.assertRaises(RuntimeError):
            player.play(
                [{"type": "unknown-action"}],
                on_repeat_complete=lambda current, total: failed.append((current, total)),
            )
        self.assertEqual(failed, [])

    def test_repeat_interval_only_runs_between_repetitions(self):
        player = MacroPlayer()
        player._wait = Mock()
        player.play([{"type": "comment"}], repeats=3, repeat_interval_ms=750)
        self.assertEqual(
            [call.args[0] for call in player._wait.call_args_list if call.args[0]],
            [750, 750],
        )

    def test_stop_interrupts_wait(self):
        player = MacroPlayer()
        thread = threading.Thread(target=lambda: player.play([{"type": "delay", "ms": 5000}]))
        thread.start()
        time.sleep(0.03)
        player.stop()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())

    def test_relative_move_uses_121_compatibility_by_default(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_window", return_value=True), \
             patch("macroflow.execution.player.activate_window", return_value=True), \
             patch("macroflow.execution.player.send_move_relative") as send_relative:
            player.play([
                {"type": "mouse_move", "mode": "relative", "dx": 12, "dy": -4},
            ], hwnd=123)
        send_relative.assert_called_once_with(12, -4)

    def test_script_ref_executes_referenced_script_actions(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref_path = Path(folder) / "referenced.json"
            ref_path.write_text(json.dumps({
                "name": "被引用脚本",
                "actions": [{"type": "notice", "text": "来自引用脚本", "duration_ms": 500}],
            }, ensure_ascii=False), encoding="utf-8")
            player.play([{"type": "script_ref", "script": str(ref_path), "delay_ms": 0}])
        self.assertEqual(notices, [("来自引用脚本", 500)])

    def test_script_ref_reads_latest_file_content_each_run(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref_path = Path(folder) / "referenced.json"

            def write_referenced(text):
                ref_path.write_text(json.dumps({
                    "name": "ref",
                    "actions": [{"type": "notice", "text": text, "duration_ms": 500}],
                }, ensure_ascii=False), encoding="utf-8")

            write_referenced("第一版")
            player.play([{"type": "script_ref", "script": str(ref_path)}])
            write_referenced("第二版")
            player.play([{"type": "script_ref", "script": str(ref_path)}])
        self.assertEqual(notices, [("第一版", 500), ("第二版", 500)])

    def test_script_ref_respects_pre_and_after_delay(self):
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref_path = Path(folder) / "referenced.json"
            ref_path.write_text(json.dumps({
                "name": "ref",
                "actions": [{"type": "delay", "ms": 0}],
            }, ensure_ascii=False), encoding="utf-8")
            player.play([
                {"type": "script_ref", "script": str(ref_path), "delay_ms": 200, "after_delay_ms": 300},
            ])
        self.assertIn(200, waits)
        self.assertIn(300, waits)

    def test_script_ref_missing_file_raises(self):
        player = MacroPlayer()
        with self.assertRaisesRegex(RuntimeError, "不存在"):
            player.play([{"type": "script_ref", "script": "scripts/不存在的脚本.json"}])

    def test_script_ref_cycle_detected(self):
        player = MacroPlayer()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            a_path = Path(folder) / "a.json"
            b_path = Path(folder) / "b.json"
            a_path.write_text(json.dumps({
                "name": "A",
                "actions": [{"type": "script_ref", "script": str(b_path)}],
            }, ensure_ascii=False), encoding="utf-8")
            b_path.write_text(json.dumps({
                "name": "B",
                "actions": [{"type": "script_ref", "script": str(a_path)}],
            }, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "循环引用"):
                player.play([{"type": "script_ref", "script": str(a_path)}])

    def test_script_ref_summary(self):
        kind, detail, _delay = action_summary({
            "type": "script_ref", "script": "scripts/关卡/某脚本.json",
        })
        self.assertIn("引用脚本", kind)
        self.assertIn("某脚本", detail)

    def test_open_app_action_launches_selected_executable(self):
        player = MacroPlayer()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            exe = Path(folder) / "app.exe"
            exe.write_bytes(b"MZ")
            with patch("macroflow.execution.player.os.startfile") as startfile:
                player.play([{"type": "open_app", "path": str(exe)}])
            startfile.assert_called_once_with(str(exe), arguments="")

    def test_open_app_action_launches_with_arguments(self):
        player = MacroPlayer()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            exe = Path(folder) / "app.exe"
            exe.write_bytes(b"MZ")
            with patch("macroflow.execution.player.os.startfile") as startfile:
                player.play([{"type": "open_app", "path": str(exe), "args": "-windowed -u=dev"}])
            startfile.assert_called_once_with(str(exe), arguments="-windowed -u=dev")

    def test_open_app_missing_file_raises(self):
        player = MacroPlayer()
        with self.assertRaisesRegex(RuntimeError, "不存在"):
            player.play([{"type": "open_app", "path": "C:/no_such_dir/app.exe"}])

    def test_open_app_summary(self):
        kind, detail, _delay = action_summary({
            "type": "open_app", "path": "C:/Tools/某软件.exe",
        })
        self.assertIn("打开软件", kind)
        self.assertIn("某软件.exe", detail)
        self.assertNotIn("（", detail)

    def test_open_app_summary_with_arguments(self):
        _kind, detail, _delay = action_summary({
            "type": "open_app", "path": "C:/Tools/某软件.exe", "args": "-windowed",
        })
        self.assertIn("某软件.exe", detail)
        self.assertIn("-windowed", detail)

    def test_close_app_action_missing_name_raises(self):
        player = MacroPlayer()
        with self.assertRaisesRegex(RuntimeError, "缺少进程名"):
            player._execute_close_app({"type": "close_app", "name": "  "})

    def test_close_app_not_running_skips(self):
        player = MacroPlayer()
        statuses = []
        player._status = lambda text: statuses.append(text)
        with patch("macroflow.execution.player.is_process_running", return_value=False), \
             patch("macroflow.execution.player.taskkill_process") as taskkill:
            player._execute_close_app({
                "type": "close_app", "name": "clash-verge.exe",
                "graceful": True, "graceful_wait_ms": 2000,
            })
        taskkill.assert_not_called()
        self.assertTrue(any("未在运行" in text for text in statuses))

    def test_close_app_graceful_then_force_fallback(self):
        player = MacroPlayer()
        statuses = []
        player._status = lambda text: statuses.append(text)
        # running → graceful → still running (wait expired) → force → gone
        with patch("macroflow.execution.player.is_process_running", side_effect=[True, True, True, True, False]), \
             patch("macroflow.execution.player.taskkill_process", side_effect=[(0, ""), (0, "")]) as taskkill:
            player._execute_close_app({
                "type": "close_app", "name": "clash-verge.exe",
                "graceful": True, "graceful_wait_ms": 0,
            })
        self.assertEqual(
            [(call.args[0], call.kwargs.get("force")) for call in taskkill.call_args_list],
            [("clash-verge.exe", False), ("clash-verge.exe", True)],
        )
        self.assertTrue(any("强制结束" in text for text in statuses))

    def test_close_app_force_direct(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_process_running", side_effect=[True, True, False]), \
             patch("macroflow.execution.player.taskkill_process", side_effect=[(0, "")]) as taskkill:
            player._execute_close_app({
                "type": "close_app", "name": "demo.exe",
                "graceful": False, "graceful_wait_ms": 2000,
            })
        taskkill.assert_called_once_with("demo.exe", force=True, tree=False)

    def test_close_app_graceful_success(self):
        player = MacroPlayer()
        with patch("macroflow.execution.player.is_process_running", side_effect=[True, True, False, False]), \
             patch("macroflow.execution.player.taskkill_process", side_effect=[(0, "")]) as taskkill:
            player._execute_close_app({
                "type": "close_app", "name": "demo.exe",
                "graceful": True, "graceful_wait_ms": 2000,
            })
        taskkill.assert_called_once_with("demo.exe", force=False, tree=False)

    def test_close_app_graceful_access_denied_forces(self):
        player = MacroPlayer()
        statuses = []
        player._status = lambda text: statuses.append(text)
        # 优雅关闭请求被拒绝（如权限不足）→ 不再干等，直接强制结束
        with patch("macroflow.execution.player.is_process_running", side_effect=[True, True, False]), \
             patch("macroflow.execution.player.taskkill_process", side_effect=[(1, "拒绝访问"), (0, "")]) as taskkill:
            player._execute_close_app({
                "type": "close_app", "name": "demo.exe",
                "graceful": True, "graceful_wait_ms": 60000,
            })
        self.assertEqual(
            [(call.args[0], call.kwargs.get("force")) for call in taskkill.call_args_list],
            [("demo.exe", False), ("demo.exe", True)],
        )
        self.assertTrue(any("关闭请求失败" in text for text in statuses))

    def test_close_app_elevated_fallback(self):
        player = MacroPlayer()
        statuses = []
        player._status = lambda text: statuses.append(text)
        # 普通权限反复结束失败，最终由管理员权限结束
        with patch("macroflow.execution.player.is_process_running", side_effect=[True, True, False]), \
             patch("macroflow.execution.player.taskkill_process", return_value=(1, "拒绝访问")), \
             patch("macroflow.execution.player.elevated_taskkill", return_value=True) as elev:
            player._execute_close_app({
                "type": "close_app", "name": "app_launcher.exe",
                "graceful": True, "graceful_wait_ms": 2000,
                "tree": False, "elevated_retry": True,
            })
        elev.assert_called_once_with("app_launcher.exe", tree=False)
        self.assertTrue(any("管理员权限" in text for text in statuses))

    def test_close_app_elevated_declined_raises(self):
        player = MacroPlayer()
        # UAC 授权被取消 → 最终报错
        with patch("macroflow.execution.player.is_process_running", return_value=True), \
             patch("macroflow.execution.player.taskkill_process", return_value=(1, "拒绝访问")), \
             patch("macroflow.execution.player.elevated_taskkill", return_value=False) as elev:
            with self.assertRaisesRegex(RuntimeError, "无法结束进程"):
                player._execute_close_app({
                    "type": "close_app", "name": "demo.exe",
                    "graceful": False, "elevated_retry": True,
                })
        elev.assert_called_once()

    def test_close_app_summary(self):
        kind, detail, _delay = action_summary({
            "type": "close_app", "name": "clash-verge.exe",
        })
        self.assertIn("关闭软件", kind)
        self.assertIn("clash-verge.exe", detail)
        self.assertIn("优雅", detail)

    def test_close_app_summary_force(self):
        _kind, detail, _delay = action_summary({
            "type": "close_app", "name": "demo.exe", "graceful": False,
        })
        self.assertIn("强制", detail)


class CloseScriptTests(unittest.TestCase):
    def _app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._notify = Mock()
        app._log = Mock()
        app._set_status = Mock()
        app._clear_action_undo = Mock()
        app.rebuild_action_tree = Mock()
        app._refresh_coordinate_scale_status = Mock()
        app._sync_activation_ui_from_script = Mock()
        app.script_name_var = Mock()
        app.interval_var = Mock()
        app.script_category_var = Mock()
        app.record_mode_var = Mock()
        return app

    def test_close_script_snapshots_and_clears_editor(self):
        app = self._app()
        app.script = MacroScript(name="A", actions=[{"type": "delay", "delay_ms": 100}])
        app.script_path = Path("C:/x/A.json")
        app.script_requires_new_file = False
        app.dirty = True
        app.action_undo_stack = [{"type": "delay"}]
        app.undo_open_stack = []
        app.undo_open_button = Mock()
        app.close_script()
        self.assertEqual(len(app.undo_open_stack), 1)
        snap = app.undo_open_stack[0]
        self.assertEqual(snap["script"].name, "A")
        self.assertEqual(len(snap["script"].actions), 1)
        self.assertEqual(snap["script_path"], Path("C:/x/A.json"))
        self.assertTrue(snap["dirty"])
        self.assertIsInstance(app.script, MacroScript)
        self.assertEqual(app.script.name, "未命名脚本")
        self.assertIsNone(app.script_path)
        self.assertFalse(app.dirty)
        app.undo_open_button.configure.assert_called_with(state="normal")

    def test_undo_open_restores_closed_script(self):
        app = self._app()
        app.script = MacroScript(name="A", actions=[{"type": "delay", "delay_ms": 100}])
        app.script_path = Path("C:/x/A.json")
        app.script_requires_new_file = False
        app.dirty = True
        app.action_undo_stack = [{"type": "delay"}]
        app.undo_open_stack = []
        app.undo_open_button = Mock()
        app.close_script()
        app.undo_open_script()
        self.assertEqual(app.script.name, "A")
        self.assertEqual(len(app.script.actions), 1)
        self.assertEqual(app.script_path, Path("C:/x/A.json"))
        self.assertTrue(app.dirty)
        self.assertEqual(app.action_undo_stack, [{"type": "delay"}])
        self.assertEqual(app.undo_open_stack, [])
        app.undo_open_button.configure.assert_called_with(state="disabled")

    def test_close_snapshot_isolated_from_later_edits(self):
        app = self._app()
        app.script = MacroScript(name="A", actions=[{"type": "delay", "delay_ms": 100}])
        app.script_path = None
        app.script_requires_new_file = False
        app.dirty = False
        app.action_undo_stack = []
        app.undo_open_stack = []
        app.close_script()
        app.script.actions.append({"type": "click", "x": 1, "y": 2})
        self.assertEqual(len(app.undo_open_stack[0]["script"].actions), 1)

    def test_new_script_clears_undo_open_stack(self):
        app = self._app()
        app.script = MacroScript()
        app.dirty = False
        app.undo_open_stack = [{"x": 1}]
        app.new_script()
        self.assertEqual(app.undo_open_stack, [])

    def test_opening_script_clears_undo_open_stack(self):
        app = self._app()
        app.script = MacroScript()
        app.dirty = False
        app.undo_open_stack = [{"x": 1}]
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({"name": "Ref", "actions": []}, ensure_ascii=False),
                           encoding="utf-8")
            app.load_script_into_editor(ref)
        self.assertEqual(app.undo_open_stack, [])
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            path = Path(folder) / "b.json"
            path.write_text(json.dumps({"name": "B", "actions": []}, ensure_ascii=False),
                            encoding="utf-8")
            with patch("macroflow.ui.app.load_script", return_value=MacroScript(name="B")):
                app.load_script_into_editor(path)
        self.assertEqual(app.undo_open_stack, [])
        self.assertEqual(app.script.name, "b")

    def test_load_script_into_editor_blocks_when_dirty(self):
        # 打开脚本会替换编辑器内容并清空“撤销打开”栈：未保存的修改
        # 必须拦截（与新建脚本/工作流内打开一致），否则静默丢失。
        app = self._app()
        app.dirty = True
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({"name": "Ref", "actions": []}, ensure_ascii=False),
                           encoding="utf-8")
            app.load_script_into_editor(ref)
        app._notify.assert_called_once()

    def test_opening_script_uses_current_filename_when_json_name_is_stale(self):
        app = self._app()
        app.script = MacroScript(name="旧名称")
        app.dirty = False
        app.undo_open_stack = []
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            path = Path(folder) / "修改后的名称.json"
            path.write_text(json.dumps({"name": "旧名称", "actions": []}, ensure_ascii=False),
                            encoding="utf-8")
            app.load_script_into_editor(path)
        self.assertEqual(app.script.name, "修改后的名称")
        app.script_name_var.set.assert_called_with("修改后的名称")


class ScriptRefWindowTests(unittest.TestCase):
    def _app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._notify = Mock()
        app._log = Mock()
        app._set_status = Mock()
        app.load_script_into_editor = Mock()
        return app

    def test_open_referenced_script_launches_new_window_with_flag(self):
        app = self._app()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({"name": "Ref", "actions": []}, ensure_ascii=False),
                           encoding="utf-8")
            with patch("macroflow.ui.app.subprocess.Popen") as popen:
                app.open_referenced_script_in_new_window(
                    {"type": "script_ref", "script": str(ref)})
            popen.assert_called_once()
            args = popen.call_args.args[0]
            self.assertIn("--open-script", args)
            self.assertEqual(args[args.index("--open-script") + 1], str(ref))
            self.assertEqual(popen.call_args.kwargs["cwd"], str(BASE_DIR))
        app._set_status.assert_called_once()

    def test_open_referenced_script_missing_file_notifies_without_launch(self):
        app = self._app()
        with patch("macroflow.ui.app.subprocess.Popen") as popen:
            app.open_referenced_script_in_new_window(
                {"type": "script_ref", "script": "C:/no_such_dir/ref.json"})
        popen.assert_not_called()
        app._notify.assert_called_once_with("引用脚本不存在", "找不到文件：C:/no_such_dir/ref.json")

    def test_open_referenced_script_empty_path_notifies(self):
        app = self._app()
        with patch("macroflow.ui.app.subprocess.Popen") as popen:
            app.open_referenced_script_in_new_window({"type": "script_ref", "script": "  "})
        popen.assert_not_called()
        app._notify.assert_called_once_with("引用脚本无效", "该引用动作没有脚本路径。")

    def test_context_menu_only_offered_on_script_ref_rows(self):
        app = self._app()
        app.root = Mock()
        app.script = MacroScript(actions=[
            {"type": "script_ref", "script": "scripts/ref.json"},
            {"type": "comment", "text": "备注"},
        ])
        app.action_tree = Mock()
        app.action_tree.identify_row.return_value = "0"
        event = Mock()
        event.y, event.x_root, event.y_root = 20, 100, 120
        with patch("macroflow.ui.app.tk.Menu") as menu_class:
            app._show_action_context_menu(event)
        menu_class.assert_called_once()
        menu = menu_class.return_value
        menu.add_command.assert_called_once()
        menu.tk_popup.assert_called_once_with(100, 120)
        menu.grab_release.assert_called_once()

    def test_context_menu_skipped_for_non_ref_rows(self):
        app = self._app()
        app.root = Mock()
        app.script = MacroScript(actions=[{"type": "comment", "text": "备注"}])
        app.action_tree = Mock()
        app.action_tree.identify_row.return_value = "0"
        with patch("macroflow.ui.app.tk.Menu") as menu_class:
            app._show_action_context_menu(Mock())
        menu_class.assert_not_called()

    def test_workflow_context_menu_offers_open_script_items(self):
        app = self._app()
        app.root = Mock()
        app.workflow = Workflow(steps=[{"script": "scripts/关卡/a.json"}])
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = "0"
        event = Mock()
        event.y, event.x_root, event.y_root = 20, 100, 120
        with patch("macroflow.ui.app.tk.Menu") as menu_class:
            app._show_workflow_context_menu(event)
        app.workflow_tree.selection_set.assert_called_once_with("0")
        menu_class.assert_called_once()
        menu = menu_class.return_value
        self.assertEqual(menu.add_command.call_count, 3)
        labels = [call.kwargs["label"] for call in menu.add_command.call_args_list]
        self.assertIn("▶ 单独执行一次测试", labels)
        self.assertIn("⇪ 在新窗口打开脚本", labels)
        self.assertIn("✎ 在当前编辑器打开", labels)
        menu.tk_popup.assert_called_once_with(100, 120)
        menu.grab_release.assert_called_once()

    def test_workflow_context_menu_skipped_for_row_without_script(self):
        app = self._app()
        app.root = Mock()
        app.workflow = Workflow(steps=[{"kind": "global_module", "module": "m"}])
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = "0"
        with patch("macroflow.ui.app.tk.Menu") as menu_class:
            app._show_workflow_context_menu(Mock())
        menu_class.assert_not_called()

    def test_workflow_context_menu_skipped_outside_row(self):
        app = self._app()
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = ""
        with patch("macroflow.ui.app.tk.Menu") as menu_class:
            app._show_workflow_context_menu(Mock())
        menu_class.assert_not_called()

    def test_open_workflow_script_in_editor_loads_file(self):
        app = self._app()
        app.dirty = False
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({"name": "Ref", "actions": []}, ensure_ascii=False),
                           encoding="utf-8")
            app._open_workflow_script_in_editor({"script": str(ref)})
        app.load_script_into_editor.assert_called_once_with(ref)

    def test_open_workflow_script_in_editor_blocked_when_dirty(self):
        app = self._app()
        app.dirty = True
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({"name": "Ref", "actions": []}, ensure_ascii=False),
                           encoding="utf-8")
            app._open_workflow_script_in_editor({"script": str(ref)})
        app.load_script_into_editor.assert_not_called()
        app._notify.assert_called_once()

    def test_open_workflow_script_in_editor_missing_file_notifies(self):
        app = self._app()
        app.dirty = False
        app._open_workflow_script_in_editor({"script": "C:/no_such_dir/ref.json"})
        app.load_script_into_editor.assert_not_called()
        app._notify.assert_called_once_with("脚本不存在", "找不到文件：C:/no_such_dir/ref.json")

    def _test_app_for_workflow_script_alone(self) -> MacroFlowApp:
        app = self._app()
        app.recorder = Mock()
        app.recorder.running = False
        app.worker = Mock()
        app.worker.is_alive.return_value = False
        app._bound_hwnd = Mock(return_value=123)
        for name in ("focus_mode_enabled_var", "activate_target_enabled_var"):
            variable = Mock()
            variable.get.return_value = False
            setattr(app, name, variable)
        app.workflow_stop = Mock()
        for name in ("_sound", "_hide_main_for_execution", "_show_execution_mini",
                     "_append_mini_step", "_set_execution_progress",
                     "_run_script_worker"):
            setattr(app, name, Mock())
        return app

    def _write_test_script(self, actions) -> Path:
        folder = tempfile.TemporaryDirectory(dir=BASE_DIR)
        self.addCleanup(folder.cleanup)
        ref = Path(folder.name) / "ref.json"
        ref.write_text(json.dumps({"name": "Ref", "actions": actions}, ensure_ascii=False),
                       encoding="utf-8")
        return ref

    def test_run_workflow_script_alone_starts_worker_once(self):
        app = self._test_app_for_workflow_script_alone()
        ref = self._write_test_script([{"type": "click", "x": 1, "y": 2, "delay_ms": 0}])
        with patch("macroflow.ui.app.threading.Thread") as thread_class:
            app.run_workflow_script_alone({"script": str(ref)})
        thread_class.assert_called_once()
        self.assertEqual(thread_class.call_args.kwargs["target"], app._run_script_worker)
        worker_args = thread_class.call_args.kwargs["args"]
        self.assertEqual(worker_args[1], 1)
        self.assertEqual(worker_args[0], list(load_script(ref).actions))
        self.assertEqual(thread_class.call_args.kwargs["kwargs"],
                         {"trigger": {}})
        thread_class.return_value.start.assert_called_once()
        app._notify.assert_not_called()

    def test_run_workflow_script_alone_missing_file_notifies(self):
        app = self._test_app_for_workflow_script_alone()
        app.run_workflow_script_alone({"script": "C:/no_such_dir/ref.json"})
        app._notify.assert_called_once_with("脚本不存在", "找不到文件：C:/no_such_dir/ref.json")
        app.worker.start.assert_not_called()

    def test_run_workflow_script_alone_invalid_script_notifies(self):
        app = self._test_app_for_workflow_script_alone()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "bad.json"
            ref.write_text("not json", encoding="utf-8")
            app.run_workflow_script_alone({"script": str(ref)})
        self.assertEqual(app._notify.call_args.args[0], "无法加载脚本")

    def test_run_workflow_script_alone_without_actions_notifies(self):
        app = self._test_app_for_workflow_script_alone()
        ref = self._write_test_script([])
        app.run_workflow_script_alone({"script": str(ref)})
        app._notify.assert_called_once()
        app.worker.start.assert_not_called()

    def test_run_workflow_script_alone_blocked_while_worker_running(self):
        app = self._test_app_for_workflow_script_alone()
        app.worker.is_alive.return_value = True
        ref = self._write_test_script([{"type": "click", "x": 1, "y": 2, "delay_ms": 0}])
        app.run_workflow_script_alone({"script": str(ref)})
        app._notify.assert_called_once_with("正在运行", "已有脚本或工作流正在执行。")
        app.worker.start.assert_not_called()

    def test_run_workflow_script_alone_skips_missing_activation_window_but_runs(self):
        app = self._test_app_for_workflow_script_alone()
        app._execution_activation_hwnd = Mock(
            side_effect=RuntimeError("脚本的前置窗口当前未打开。"))
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({
                "name": "Ref",
                "settings": {
                    "activation_window_enabled": True,
                    "activation_window": {"title": "游戏窗口"},
                },
                "actions": [{"type": "click", "x": 1, "y": 2, "delay_ms": 0}],
            }, ensure_ascii=False), encoding="utf-8")
            with patch("macroflow.ui.app.threading.Thread") as thread_class:
                app.run_workflow_script_alone({"script": str(ref)})
        app._execution_activation_hwnd.assert_called_once_with(
            123, True, {"title": "游戏窗口", "class_name": "", "process_path": ""})
        app._notify.assert_not_called()
        app._log.assert_called_once_with("前置窗口未打开，已跳过前置窗口，继续执行脚本。")
        thread_class.assert_called_once()
        thread_class.return_value.start.assert_called_once()

    def test_load_startup_script_loads_existing_file(self):
        app = self._app()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            ref = Path(folder) / "ref.json"
            ref.write_text(json.dumps({"name": "Ref", "actions": []}, ensure_ascii=False),
                           encoding="utf-8")
            app._load_startup_script(ref)
        app.load_script_into_editor.assert_called_once_with(ref)
        app._notify.assert_not_called()

    def test_load_startup_script_missing_file_notifies(self):
        app = self._app()
        missing = Path(BASE_DIR) / "no_such_ref.json"
        app._load_startup_script(missing)
        app.load_script_into_editor.assert_not_called()
        app._notify.assert_called_once()


class _FakeBooleanVar:
    """Links set()/get() like a real Tk BooleanVar (set only updates get)."""

    def __init__(self, value: bool = False):
        self._value = bool(value)

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = bool(value)


class _FakeSettingVar:
    """set()/get() 桩，模拟 Tk StringVar 的读写（不依赖 Tk）。"""

    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class LastScriptRestoreTests(unittest.TestCase):
    """启动时恢复上次关闭时脚本编辑页正在编辑的脚本。"""

    def _app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.app_settings = {}
        app._log = Mock()
        app.load_script_into_editor = Mock()
        return app

    def test_restores_recorded_script_at_startup(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            path = Path(folder) / "a.json"
            path.write_text("{}", encoding="utf-8")
            app = self._app()
            app.app_settings["last_script_path"] = str(path)
            app._load_last_script()
            app.load_script_into_editor.assert_called_once_with(path)

    def test_restores_relative_recorded_script(self):
        # 设置里存的是相对程序目录的路径（display_path 产出），启动时按
        # BASE_DIR 解析回绝对路径再打开。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            path = Path(folder) / "b.json"
            path.write_text("{}", encoding="utf-8")
            app = self._app()
            app.app_settings["last_script_path"] = display_path(path)
            app._load_last_script()
            app.load_script_into_editor.assert_called_once_with(path)

    def test_skips_missing_recorded_script(self):
        # 脚本已被删除/移动时不能弹“打开失败”，静默跳过并记日志。
        app = self._app()
        app.app_settings["last_script_path"] = "scripts/关卡/不存在的脚本.json"
        app._load_last_script()
        app.load_script_into_editor.assert_not_called()
        self.assertTrue(any("已不存在" in call.args[0] for call in app._log.call_args_list))

    def test_skips_empty_record(self):
        # 编辑器没有脚本（新建/关闭/录制分离）时记录为空，启动不恢复。
        app = self._app()
        app._load_last_script()
        app.load_script_into_editor.assert_not_called()

    def test_sidebar_settings_record_editor_script_path(self):
        # 每次持久化侧栏设置（含关闭应用）都要带上脚本编辑页当前打开的脚本。
        app = self._app()
        app.interval_var = _FakeSettingVar("100")
        app.repeat_var = _FakeSettingVar("1")
        app.backup_interval_var = _FakeSettingVar("1h")
        app.sound_enabled_var = _FakeSettingVar(True)
        app.mini_window_enabled_var = _FakeSettingVar(True)
        app.execution_mini_enabled_var = _FakeSettingVar(True)
        app.execution_mini_position = []
        app.close_action_var = _FakeSettingVar("exit")
        app.focus_mode_enabled_var = _FakeSettingVar(False)
        app.activate_target_enabled_var = _FakeSettingVar(True)
        app.floating_notice_position_var = _FakeSettingVar("顶部居中")
        app.saved_window_signature = None
        app.activation_draft_enabled = False
        app.activation_enabled_var = _FakeSettingVar(False)
        app.activation_draft_signature = None
        app._workflow_snapshot = Mock(return_value={})
        app.workflow_path = None
        app.timed_backup_enabled_var = _FakeSettingVar(False)
        app.windows_startup_enabled_var = _FakeSettingVar(False)
        app.start_minimized_to_tray_var = _FakeSettingVar(False)
        app.startup_run_workflow_var = _FakeSettingVar(False)
        app.startup_workflow_path_var = _FakeSettingVar("")
        app.level_scripts_dir_var = _FakeSettingVar("scripts/关卡")
        app.level_pack_scripts_dir_var = _FakeSettingVar("scripts/关卡封装")
        app.switch_scripts_dir_var = _FakeSettingVar("scripts/切换")

        app.script_path = Path("C:/x/A.json")
        self.assertEqual(
            app._collect_sidebar_settings()["last_script_path"],
            display_path(app.script_path),
        )
        app.script_path = None
        self.assertEqual(app._collect_sidebar_settings()["last_script_path"], "")


class ActivationWindowToggleTests(unittest.TestCase):
    SIGNATURE = {"title": "前置窗口", "class_name": "Front", "process_path": "C:/Game/front.exe"}

    def _app(self) -> MacroFlowApp:
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.activation_window = None
        app.saved_activation_signature = dict(self.SIGNATURE)
        app.activation_draft_signature = dict(self.SIGNATURE)
        app.activation_draft_enabled = False
        app.activation_enabled_var = _FakeBooleanVar(False)
        app.activation_label_var = Mock()
        app.script = MacroScript(actions=[])
        app._mark_dirty = Mock()
        app._log = Mock()
        app._persist_sidebar_settings = Mock(return_value=True)
        return app

    def test_workflow_start_reads_selected_script_prewindow(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            script_path = Path(folder) / "selected.json"
            save_script(MacroScript(
                actions=[],
                settings={
                    "activation_window_enabled": True,
                    "activation_window": dict(self.SIGNATURE),
                },
            ), script_path)
            app = self._app()

            self.assertEqual(
                app._activation_settings_from_workflow_step({"script": str(script_path)}),
                (True, self.SIGNATURE),
            )

    def test_disabled_prewindow_has_no_explicit_activation(self):
        app = self._app()
        app._restore_saved_activation_window = Mock()
        self.assertIsNone(app._execution_activation_hwnd(123, False, self.SIGNATURE))
        app._restore_saved_activation_window.assert_not_called()

    def test_enabled_prewindow_uses_saved_window(self):
        app = self._app()
        app.activation_window = Mock()
        app.activation_window.hwnd = 456
        app._restore_saved_activation_window = Mock(return_value=True)
        self.assertEqual(
            app._execution_activation_hwnd(123, True, self.SIGNATURE), 456,
        )

    def test_enabled_prewindow_without_signature_falls_back(self):
        app = self._app()
        self.assertIsNone(app._execution_activation_hwnd(123, True, None))

    def test_enabled_prewindow_revalidates_cached_window_for_each_script(self):
        app = self._app()
        app.activation_window = Mock()
        app.activation_window.hwnd = 111
        app._restore_saved_activation_window = Mock(return_value=True)
        app._restore_saved_activation_window.side_effect = lambda _signature: setattr(
            app.activation_window, "hwnd", 456,
        ) or True
        self.assertEqual(
            app._execution_activation_hwnd(123, True, self.SIGNATURE), 456,
        )
        app._restore_saved_activation_window.assert_called_once_with(self.SIGNATURE)

    def test_enabled_prewindow_missing_window_raises(self):
        app = self._app()
        app._restore_saved_activation_window = Mock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "前置窗口"):
            app._execution_activation_hwnd(123, True, self.SIGNATURE)

    def test_workflow_entry_skips_missing_prewindow_and_continues_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "plain.json"
            save_script(MacroScript(name="普通脚本", actions=[{"type": "delay", "ms": 1}]), script_path)
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.worker = None
            app.workflow_test_mode_active = False
            app.workflow_test_mode_var = _FakeBooleanVar(False)
            app._workflow_only_steps = Mock(return_value=[{"script": str(script_path), "repeats": 1}])
            app._global_module_steps = Mock(return_value=[])
            app._workflow_snapshot = Mock()
            app._persist_workflow_draft = Mock()
            app.rebuild_workflow_tree = Mock()
            app.workflow_start_var = Mock()
            app.workflow_start_var.get.return_value = ""
            app._bound_hwnd = Mock(return_value=123)
            app._activation_settings_from_script = Mock(return_value=(True, dict(self.SIGNATURE)))
            app._execution_activation_hwnd = Mock(side_effect=RuntimeError("前置窗口当前未打开"))
            app._log = Mock()
            app._notify = Mock()
            app.focus_mode_enabled_var = _FakeBooleanVar(False)
            app.activate_target_enabled_var = _FakeBooleanVar(True)
            app._clear_global_guards = Mock(side_effect=RuntimeError("startup continued"))

            with self.assertRaisesRegex(RuntimeError, "startup continued"):
                app.run_workflow()

            self.assertTrue(any("已跳过前置窗口" in call.args[0] for call in app._log.call_args_list))
            app._notify.assert_not_called()

    def test_choose_activation_window_writes_script_settings(self):
        app = self._app()
        selected = Mock()
        selected.title = "新前置窗口"
        selected.class_name = "NewFront"
        selected.process_path = "C:/Game/new.exe"
        selected.label = "新前置窗口（NewFront）"
        app.root = Mock()
        with patch("macroflow.ui.app.WindowPicker") as picker, patch("macroflow.ui.app.is_window", return_value=True):
            picker.return_value.show.return_value = selected
            app.choose_activation_window()
        self.assertEqual(app.saved_activation_signature["title"], "新前置窗口")
        self.assertTrue(app.activation_enabled_var.get())
        self.assertTrue(app.script.settings["activation_window_enabled"])
        self.assertEqual(app.script.settings["activation_window"]["title"], "新前置窗口")
        app._mark_dirty.assert_called_once()
        app._persist_sidebar_settings.assert_called_once()

    def test_blank_script_restores_last_activation_window(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.app_settings = {
            "activation_window_draft_enabled": True,
            "activation_window_draft": dict(self.SIGNATURE),
        }
        script = app._blank_script_with_activation_draft()
        self.assertTrue(script.settings["activation_window_enabled"])
        self.assertEqual(script.settings["activation_window"], self.SIGNATURE)

    def test_unbind_activation_window_clears_script_settings(self):
        app = self._app()
        app.activation_window = Mock()
        app.unbind_activation_window()
        self.assertIsNone(app.saved_activation_signature)
        self.assertFalse(app.activation_enabled_var.get())
        app.activation_label_var.set.assert_called_once_with("跟随目标窗口")
        self.assertFalse(app.script.settings["activation_window_enabled"])
        self.assertIsNone(app.script.settings["activation_window"])
        app._mark_dirty.assert_called_once()

    def test_toggle_disabled_label_shows_deactivated(self):
        app = self._app()
        app._refresh_activation_label()
        app.activation_label_var.set.assert_called_once_with("前置窗口（已停用）")

    def test_toggle_enabled_label_shows_saved_title(self):
        app = self._app()
        app.activation_enabled_var.set(True)
        app.activation_window = None
        app._refresh_activation_label()
        app.activation_label_var.set.assert_called_once_with("已保存，等待窗口：前置窗口")

    def test_sync_activation_ui_reads_script_settings(self):
        app = self._app()
        app.script.settings["activation_window_enabled"] = True
        app.script.settings["activation_window"] = dict(self.SIGNATURE)
        app._restore_saved_activation_window = Mock(return_value=True)
        app._sync_activation_ui_from_script()
        self.assertTrue(app.activation_enabled_var.get())
        self.assertEqual(app.saved_activation_signature, self.SIGNATURE)
        app._restore_saved_activation_window.assert_called_once_with(self.SIGNATURE)

    def test_sync_activation_ui_clears_when_script_has_none(self):
        app = self._app()
        app.script.settings["activation_window"] = None
        app.script.settings["activation_window_enabled"] = False
        app._restore_saved_activation_window = Mock()
        app._sync_activation_ui_from_script()
        self.assertFalse(app.activation_enabled_var.get())
        self.assertIsNone(app.saved_activation_signature)
        app._restore_saved_activation_window.assert_not_called()

    def test_sync_unconfigured_script_inherits_saved_draft_without_erasing_it(self):
        app = self._app()
        app.activation_draft_enabled = True
        app.activation_draft_signature = dict(self.SIGNATURE)
        app.saved_activation_signature = None
        app._restore_saved_activation_window = Mock(return_value=True)

        app._sync_activation_ui_from_script()

        self.assertTrue(app.activation_enabled_var.get())
        self.assertEqual(app.saved_activation_signature, self.SIGNATURE)
        self.assertEqual(app.activation_draft_signature, self.SIGNATURE)
        app._restore_saved_activation_window.assert_called_once_with(self.SIGNATURE)

    def test_toggle_updates_persistent_activation_draft(self):
        app = self._app()
        app.activation_enabled_var.set(True)

        app._toggle_activation_enabled()

        self.assertTrue(app.activation_draft_enabled)
        self.assertEqual(app.activation_draft_signature, self.SIGNATURE)

    def test_current_script_settings_include_activation_config(self):
        app = self._app()
        app.activation_enabled_var.set(True)
        app.interval_var = Mock()
        app.interval_var.get.return_value = "100"
        settings = app._current_script_settings()
        self.assertTrue(settings["activation_window_enabled"])
        self.assertEqual(settings["activation_window"]["title"], "前置窗口")

    def test_workflow_step_uses_its_own_script_prewindow(self):
        with tempfile.TemporaryDirectory() as folder:
            with_front = Path(folder) / "with_front.json"
            no_front = Path(folder) / "no_front.json"
            save_script(MacroScript(
                name="带前置", actions=[{"type": "delay", "ms": 1}],
                settings={"activation_window_enabled": True, "activation_window": dict(self.SIGNATURE)},
            ), with_front)
            save_script(MacroScript(name="无前置", actions=[{"type": "delay", "ms": 2}]), no_front)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app.current_workflow_step_index = None
            app._ui = lambda callback, *args: callback(*args)
            app.activation_window = Mock()
            app.activation_window.hwnd = 456
            app._restore_saved_activation_window = Mock(return_value=True)
            app._run_workflow_worker(
                [
                    {"script": str(with_front), "repeats": 1},
                    {"script": str(no_front), "repeats": 1},
                ],
                None, None, False,
            )
            calls = app.player.play.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].kwargs["activation_hwnd"], 456)
            self.assertIsNone(calls[1].kwargs["activation_hwnd"])

    def test_workflow_step_skips_missing_prewindow_but_still_runs_script(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "missing_front.json"
            save_script(MacroScript(
                name="前置窗口未打开", actions=[{"type": "delay", "ms": 1}],
                settings={"activation_window_enabled": True, "activation_window": dict(self.SIGNATURE)},
            ), script_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app.current_workflow_step_index = None
            app._ui = lambda callback, *args: callback(*args)
            app.activation_window = None
            app._restore_saved_activation_window = Mock(return_value=False)

            app._run_workflow_worker(
                [{"script": str(script_path), "repeats": 1}],
                None, None, False,
            )

            app.player.play.assert_called_once()
            self.assertIsNone(app.player.play.call_args.kwargs["activation_hwnd"])
            self.assertTrue(any("已跳过前置窗口条件" in call.args[0] for call in app._log.call_args_list))

    def test_workflow_uses_editor_prewindow_as_default_for_steps(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "plain.json"
            save_script(MacroScript(
                name="普通脚本", actions=[{"type": "delay", "ms": 1}],
            ), script_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app.current_workflow_step_index = None
            app._ui = lambda callback, *args: callback(*args)
            app._run_workflow_worker(
                [{"script": str(script_path), "repeats": 1}],
                None, None, False, workflow_activation_hwnd=789,
            )
            self.assertEqual(
                app.player.play.call_args.kwargs["activation_hwnd"], 789,
            )

    def test_workflow_step_own_prewindow_suppressed_when_sidebar_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            with_front = Path(folder) / "with_front.json"
            save_script(MacroScript(
                name="带前置", actions=[{"type": "delay", "ms": 1}],
                settings={"activation_window_enabled": True, "activation_window": dict(self.SIGNATURE)},
            ), with_front)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app._enter_focus_mode = Mock()
            app._leave_focus_mode = Mock()
            app._set_status = Mock()
            app._set_execution_progress = Mock()
            app._append_mini_step = Mock()
            app._log = Mock()
            app._sound = Mock()
            app._handle_worker_error = Mock()
            app._finish_execution_visibility = Mock()
            app.current_workflow_step_index = None
            app._ui = lambda callback, *args: callback(*args)
            app.activation_window = None
            app._restore_saved_activation_window = Mock(return_value=True)

            # 侧栏“启用执行前置窗口”未勾选（activation_allowed=False）：
            # 步骤脚本自己保存的前置窗口一律不激活，也不会报“已跳过”。
            app._run_workflow_worker(
                [{"script": str(with_front), "repeats": 1}],
                None, None, False, activation_allowed=False,
            )

            app.player.play.assert_called_once()
            self.assertIsNone(app.player.play.call_args.kwargs["activation_hwnd"])
            app._restore_saved_activation_window.assert_not_called()
            self.assertFalse(any(
                "已跳过前置窗口条件" in call.args[0] for call in app._log.call_args_list
            ))

    def test_run_workflow_forwards_sidebar_activation_toggle(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "plain.json"
            save_script(MacroScript(
                name="普通脚本", actions=[{"type": "delay", "ms": 1}],
            ), script_path)

            def make_app() -> MacroFlowApp:
                app = MacroFlowApp.__new__(MacroFlowApp)
                app.worker = None
                app.workflow_test_mode_var = _FakeBooleanVar(False)
                app._workflow_only_steps = Mock(
                    return_value=[{"script": str(script_path), "repeats": 1}],
                )
                app._global_module_steps = Mock(return_value=[])
                app._workflow_snapshot = Mock()
                app._persist_workflow_draft = Mock()
                app.rebuild_workflow_tree = Mock()
                app.workflow_start_var = Mock()
                app.workflow_start_var.get.return_value = ""
                app._bound_hwnd = Mock(return_value=123)
                app._activation_settings_from_script = Mock(return_value=(False, None))
                app._log = Mock()
                app._notify = Mock()
                app.focus_mode_enabled_var = _FakeBooleanVar(False)
                app.activate_target_enabled_var = _FakeBooleanVar(True)
                app._clear_global_guards = Mock()
                app._clear_global_detect_rearm_locks = Mock()
                app.workflow_stop = threading.Event()
                app._sound = Mock()
                app._hide_main_for_execution = Mock()
                app._reset_execution_clock_for_new_run = Mock()
                app._set_execution_progress = Mock()
                app._show_execution_mini = Mock()
                app._append_mini_step = Mock()
                app.activation_enabled_var = _FakeBooleanVar(False)
                return app

            # 侧栏未勾选：工作流总开关关闭，步骤脚本自带前置窗口也不会执行。
            app = make_app()
            with patch("macroflow.ui.app.threading.Thread") as thread_class:
                app.run_workflow()
            worker_args = thread_class.call_args.kwargs["args"]
            self.assertFalse(worker_args[-1])
            self.assertIsNone(worker_args[9])

            # 侧栏勾选且编辑器脚本自带前置窗口：作为工作流默认前置窗口传下去。
            app = make_app()
            app.activation_enabled_var.set(True)
            app._activation_settings_from_script.return_value = (True, dict(self.SIGNATURE))
            app._restore_saved_activation_window = Mock(return_value=True)
            app.activation_window = Mock()
            app.activation_window.hwnd = 456
            with patch("macroflow.ui.app.threading.Thread") as thread_class:
                app.run_workflow()
            worker_args = thread_class.call_args.kwargs["args"]
            self.assertTrue(worker_args[-1])
            self.assertEqual(worker_args[9], 456)


class FocusModeTests(unittest.TestCase):
    def test_disabled_focus_only_switches_english(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.input_guard = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._log = Mock()
        with patch("macroflow.ui.app.force_english_input", return_value=True):
            self.assertFalse(app._enter_focus_mode(123, enabled=False))
        app.input_guard.start.assert_not_called()
        app._log.assert_called_once()

    def test_focus_guard_blocks_game_input_and_releases_on_owner_thread(self):
        release = threading.Event()

        def fake_get_message(*_args):
            release.wait(2)
            return 0

        def fake_post(_thread_id, message, *_args):
            if message == 0x0012:  # WM_QUIT
                release.set()
            return True

        guard = FocusInputGuard()
        with patch("macroflow.input.input_guard.user32.SetWindowsHookExW", return_value=1), \
             patch("macroflow.input.input_guard.user32.GetMessageW", side_effect=fake_get_message), \
             patch("macroflow.input.input_guard.user32.PostThreadMessageW", side_effect=fake_post), \
             patch("macroflow.input.input_guard.user32.UnhookWindowsHookEx"), \
             patch("macroflow.input.input_guard.user32.BlockInput", return_value=True) as block_input:
            self.assertTrue(guard.start(timeout=1.0))
            self.assertTrue(guard.block())
            guard.stop()
        self.assertEqual(
            [call.args[0] for call in block_input.call_args_list], [True, False],
        )

    def test_only_f12_and_injected_keyboard_are_allowed(self):
        self.assertTrue(should_block_keyboard(0x41, 0))
        self.assertFalse(should_block_keyboard(VK_F12, 0))
        self.assertTrue(should_block_keyboard(0x41, LLKHF_INJECTED))
        self.assertFalse(should_block_keyboard(
            0x41, LLKHF_INJECTED, MACROFLOW_INPUT_TAG,
        ))

    def test_only_macroflow_injected_mouse_is_allowed(self):
        self.assertTrue(should_block_mouse(0))
        self.assertTrue(should_block_mouse(LLMHF_INJECTED))
        self.assertFalse(should_block_mouse(
            LLMHF_INJECTED, MACROFLOW_INPUT_TAG,
        ))

    def test_focus_mode_switches_english_before_locking_input(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        order = []
        app.input_guard = Mock()
        app.input_guard.start.side_effect = lambda: order.append("guard") or True
        app.input_guard.block.side_effect = lambda: order.append("block") or True
        app._ui = Mock()
        app._log = Mock()
        with patch("macroflow.ui.app.force_english_input", side_effect=lambda _hwnd: order.append("english") or True):
            app._enter_focus_mode(123)
        self.assertEqual(order, ["english", "guard", "block"])

    def test_focus_mode_failure_stops_hook(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.input_guard = Mock()
        app.input_guard.start.return_value = True
        app.input_guard.block.return_value = False
        with patch("macroflow.ui.app.force_english_input", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "管理员身份"):
                app._enter_focus_mode(123)
        app.input_guard.stop.assert_called_once()


class KeyCaptureTests(unittest.TestCase):
    """KeyCapturer hook + KeyActionDialog capture flow."""

    def test_key_dialog_can_open_for_new_action_without_existing_action(self):
        widgets = ("Frame", "Label", "Entry", "Button", "Combobox")
        with patch("macroflow.ui.dialogs.ModalDialog.__init__", return_value=None), \
             patch("macroflow.ui.dialogs.tk.StringVar", side_effect=lambda **_: Mock()), \
             patch("macroflow.ui.dialogs.duration_var", return_value=Mock()), \
             patch.multiple("macroflow.ui.dialogs.ttk", **{
                 name: Mock(return_value=Mock()) for name in widgets
             }):
            dialog = KeyActionDialog(object())

        self.assertIsNone(dialog.capturer)

    def _install_capturer(self, events, release):
        captured = {}

        def fake_set_hook(_kind, proc, _inst, _tid):
            captured["proc"] = proc
            return ctypes.c_void_p(1)

        def fake_get_message(*_):
            release.wait(3)
            return 0

        patch_set = patch("macroflow.input.input_guard.user32.SetWindowsHookExW", side_effect=fake_set_hook)
        patch_get = patch("macroflow.input.input_guard.user32.GetMessageW", side_effect=fake_get_message)
        patch_post = patch("macroflow.input.input_guard.user32.PostThreadMessageW")
        patch_next = patch("macroflow.input.input_guard.user32.CallNextHookEx", return_value=0)
        patch_unhook = patch("macroflow.input.input_guard.user32.UnhookWindowsHookEx")
        patch_set.start()
        patch_get.start()
        post = patch_post.start()
        patch_next.start()
        patch_unhook.start()
        capturer = KeyCapturer(on_key=events.append, on_cancel=lambda: events.append("cancel"))
        self.assertTrue(capturer.start(timeout=1.0))
        return capturer, captured, post, (patch_set, patch_get, patch_post, patch_next, patch_unhook)

    def _press(self, proc, vk, flags=0):
        data = KBDLLHOOKSTRUCT()
        data.vkCode = vk
        data.flags = flags
        return proc(0, WM_KEYDOWN, ctypes.addressof(data))

    def _finish(self, capturer, patches, release):
        release.set()
        capturer.stop()
        for patch_ in patches:
            patch_.stop()

    def test_key_capturer_captures_next_keydown(self):
        events, release = [], threading.Event()
        capturer, captured, post, patches = self._install_capturer(events, release)
        try:
            self.assertEqual(self._press(captured["proc"], 0x41), 1)
            post.assert_called_once()
        finally:
            self._finish(capturer, patches, release)
        self.assertEqual(events, [0x41])

    def test_key_capturer_esc_cancels(self):
        events, release = [], threading.Event()
        capturer, captured, post, patches = self._install_capturer(events, release)
        try:
            self.assertEqual(self._press(captured["proc"], VK_ESCAPE), 1)
            post.assert_called_once()
        finally:
            self._finish(capturer, patches, release)
        self.assertEqual(events, ["cancel"])

    def test_key_capturer_lets_reserved_hotkeys_pass(self):
        events, release = [], threading.Event()
        capturer, captured, post, patches = self._install_capturer(events, release)
        try:
            self.assertEqual(self._press(captured["proc"], VK_F9), 0)
            self.assertEqual(self._press(captured["proc"], 0x7B), 0)  # F12
            post.assert_not_called()
        finally:
            self._finish(capturer, patches, release)
        self.assertEqual(events, [])

    def test_key_capturer_ignores_injected_keys(self):
        events, release = [], threading.Event()
        capturer, captured, post, patches = self._install_capturer(events, release)
        try:
            self.assertEqual(self._press(captured["proc"], 0x41, flags=LLKHF_INJECTED), 0)
            post.assert_not_called()
        finally:
            self._finish(capturer, patches, release)
        self.assertEqual(events, [])

    def test_key_capturer_start_failure_reports_false(self):
        with patch("macroflow.input.input_guard.user32.SetWindowsHookExW", return_value=0) as set_hook, \
             patch("macroflow.input.input_guard.user32.GetMessageW") as get_msg, \
             patch("macroflow.input.input_guard.user32.UnhookWindowsHookEx"):
            capturer = KeyCapturer(on_key=Mock())
            self.assertFalse(capturer.start(timeout=1.0))
        set_hook.assert_called_once()
        get_msg.assert_not_called()

    def test_vk_to_key_name_round_trips_through_key_to_vk(self):
        for name in ("A", "0", "ENTER", "SPACE", "CTRL", "F5", "LEFT", "DELETE", "NUMLOCK"):
            vk, parsed = key_to_vk(name)
            self.assertEqual(parsed, name)
            self.assertEqual(vk_to_key_name(vk), name)
            self.assertEqual(key_to_vk(vk_to_key_name(vk))[0], vk)

    def test_vk_to_key_name_falls_back_to_vk_prefix(self):
        self.assertEqual(vk_to_key_name(0x5D), "VK_0x5d")  # 菜单键不在 VK_NAMES
        self.assertEqual(key_to_vk("VK_0x5d")[0], 0x5D)
        self.assertIn(VK_F9, RESERVED_HOTKEY_VKS)

    def test_key_dialog_start_capture_starts_capturer(self):
        dialog = KeyActionDialog.__new__(KeyActionDialog)
        dialog.after = lambda delay, callback, *args: None
        dialog.capture_button = Mock()
        dialog.capture_hint = Mock()
        dialog.capturer = None
        with patch("macroflow.ui.dialogs.KeyCapturer") as capturer_class:
            capturer_class.return_value.start.return_value = True
            dialog.start_capture()
        capturer_class.assert_called_once()
        dialog.capture_button.configure.assert_called_with(state="disabled")
        dialog.capture_hint.set.assert_called_with(KEY_HINT_CAPTURING)
        dialog.capturer.start.assert_called_once()

    def test_key_dialog_capture_failure_shows_notice(self):
        dialog = KeyActionDialog.__new__(KeyActionDialog)
        dialog.after = lambda delay, callback, *args: None
        dialog.capture_button = Mock()
        dialog.capture_hint = Mock()
        dialog.capturer = None
        with patch("macroflow.ui.dialogs.KeyCapturer") as capturer_class, \
             patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            capturer_class.return_value.start.return_value = False
            dialog.start_capture()
        notice.assert_called_once()
        self.assertIsNone(dialog.capturer)
        dialog.capture_button.configure.assert_called_with(state="normal")

    def test_key_dialog_apply_captured_key_sets_key_and_ends_capture(self):
        dialog = KeyActionDialog.__new__(KeyActionDialog)
        dialog.key = Mock()
        dialog.capture_button = Mock()
        dialog.capture_hint = Mock()
        capturer = Mock()
        dialog.capturer = capturer
        dialog._apply_captured_key(0x41)
        dialog.key.set.assert_called_with("A")
        capturer.stop.assert_called_once()
        self.assertIsNone(dialog.capturer)
        dialog.capture_button.configure.assert_called_with(state="normal")

    def test_key_dialog_cancel_capture_ends_capture(self):
        dialog = KeyActionDialog.__new__(KeyActionDialog)
        dialog.capture_button = Mock()
        dialog.capture_hint = Mock()
        capturer = Mock()
        dialog.capturer = capturer
        dialog._cancel_capture()
        capturer.stop.assert_called_once()
        self.assertIsNone(dialog.capturer)

    def test_key_dialog_destroy_stops_capturer(self):
        dialog = KeyActionDialog.__new__(KeyActionDialog)
        capturer = Mock()
        dialog.capturer = capturer
        with patch("macroflow.ui.dialogs.ModalDialog.destroy", create=True) as base_destroy:
            dialog.destroy()
        capturer.stop.assert_called_once()
        self.assertIsNone(dialog.capturer)
        base_destroy.assert_called_once()


class TemplateRegionTests(unittest.TestCase):
    def test_live_module_binding_refreshes_saved_action_for_edit(self):
        stale = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:first", "template": "images/stale.png",
            "region_mode": "template", "region": [1, 2, 3, 4],
        }
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={
            "category": "switch", "template": "images/current.png",
            "region": [11, 22, 333, 444],
        }):
            refreshed = dialog_module.action_with_live_module_binding(stale)

        self.assertEqual(refreshed["template"], "images/current.png")
        self.assertEqual(refreshed["region"], [11, 22, 333, 444])
        self.assertEqual(refreshed["module_key"], "module:first")
        self.assertEqual(stale["template"], "images/stale.png")

    def test_module_manager_label_marks_blocking_module(self):
        self.assertEqual(
            module_manager_label(
                "module:blocking", {"name": "退出队伍", "blocking": True},
            ),
            "【阻塞识别】退出队伍",
        )
        self.assertEqual(
            module_manager_label(
                "module:normal", {"name": "结算确定", "blocking": False},
            ),
            "结算确定",
        )

    def test_module_manager_label_and_tag_mark_disabled_module(self):
        obj = {"name": "退出队伍", "blocking": True, "enabled": False}
        self.assertEqual(
            module_manager_label("module:disabled", obj),
            "【阻塞识别】【已禁用】退出队伍",
        )
        self.assertEqual(module_manager_tag(obj), "disabled")

    def test_module_manager_selection_colors_distinguish_enabled_and_disabled(self):
        self.assertEqual(
            module_manager_selection_colors({"enabled": True}),
            ("#FFFFFF", "#1F6B45"),
        )
        self.assertEqual(
            module_manager_selection_colors({"enabled": False}),
            ("#FFFFFF", "#7A3434"),
        )
        self.assertEqual(
            module_manager_selection_colors({
                "enabled": True,
                "run_code_on_timeout": True,
                "on_timeout_actions": [{"type": "end_current_script"}],
            }),
            ("#FFFFFF", "#713C78"),
        )

    def test_module_manager_marks_named_special_actions_in_both_segments(self):
        obj = {
            "name": "结算检测", "enabled": True,
            "run_code_after_action": True,
            "run_code_on_timeout": True,
            "on_success_actions": [
                {"type": "restart_workflow"}, {"type": "delay", "ms": 10},
            ],
            "on_timeout_actions": [
                {"type": "end_current_script"},
                {"type": "jump_current_script_last"},
            ],
        }
        self.assertEqual(
            module_manager_special_action_summary(obj),
            "附加：重新执行工作流；超时：结束当前最里层脚本，继续执行、跳转到当前脚本最后一行",
        )
        self.assertEqual(
            module_manager_label("module:special-code", obj),
            "【特殊代码段】结算检测",
        )
        self.assertEqual(module_manager_tag(obj), "special_action")

    def test_module_manager_hides_special_actions_when_segments_are_not_enabled(self):
        obj = {
            "name": "未启用代码段",
            "run_code_after_action": False,
            "run_code_on_timeout": False,
            "on_success_actions": [{"type": "restart_workflow"}],
            "on_timeout_actions": [{"type": "end_current_script"}],
        }
        self.assertEqual(module_manager_special_action_summary(obj), "")
        self.assertEqual(module_manager_label("module:off", obj), "未启用代码段")
        self.assertEqual(module_manager_tag(obj), "")

    def test_manager_selection_highlight_updates_tree_style(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        tree = Mock()
        dialog.current = "all"
        dialog.trees = {"all": tree}

        dialog._update_selection_highlight({"enabled": False})
        tree.configure.assert_called_once_with(style="ModuleManagerDisabled.Treeview")

        tree.reset_mock()
        dialog._update_selection_highlight({"enabled": True})
        tree.configure.assert_called_once_with(style="ModuleManagerEnabled.Treeview")

        tree.reset_mock()
        dialog._update_selection_highlight({
            "enabled": True,
            "run_code_after_action": True,
            "on_success_actions": [{"type": "restart_workflow"}],
        })
        tree.configure.assert_called_once_with(style="ModuleManagerSpecial.Treeview")

    def test_module_manager_styles_copy_layout_and_keep_dark_readable_colors(self):
        style = Mock()
        style.layout.return_value = [("Treeview.treearea", {"sticky": "nswe"})]

        configure_module_tree_styles(style)

        self.assertEqual(style.layout.call_count, 5)
        configured = {item.args[0]: item.kwargs for item in style.configure.call_args_list}
        self.assertEqual(
            set(configured),
            {
                "ModuleManagerNeutral.Treeview",
                "ModuleManagerEnabled.Treeview",
                "ModuleManagerSpecial.Treeview",
                "ModuleManagerDisabled.Treeview",
            },
        )
        for options in configured.values():
            self.assertEqual(options["background"], "#182129")
            self.assertEqual(options["fieldbackground"], "#182129")
            self.assertEqual(options["foreground"], "#E8EDF2")

    def test_manager_tree_tags_blocking_modules_in_all_and_category_tabs(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {
            "module:blocking": {
                "name": "退出队伍", "category": "workflow_global",
                "blocking": True, "region": [1, 2, 3, 4],
            },
            "module:normal": {
                "name": "结算确定", "category": "workflow_global",
                "blocking": False, "region": [0, 0, 0, 0],
            },
            "module:special-code": {
                "name": "结算退出", "category": "workflow_global",
                "blocking": False, "region": [0, 0, 0, 0],
                "run_code_after_action": True,
                "run_code_on_timeout": True,
                "on_success_actions": [{"type": "restart_workflow"}],
                "on_timeout_actions": [{"type": "end_current_script"}],
            },
        }
        dialog.sort_direction = "asc"
        for tab_key in ("all", "workflow_global"):
            tree = Mock()
            tree.get_children.return_value = ()
            dialog._reload_tree(tab_key, tree)
            inserted = {item.kwargs["iid"]: item.kwargs for item in tree.insert.call_args_list}
            self.assertEqual(inserted["module:blocking"]["text"], "【阻塞识别】退出队伍")
            self.assertEqual(inserted["module:blocking"]["tags"], ("blocking",))
            self.assertEqual(inserted["module:normal"]["text"], "结算确定")
            self.assertEqual(inserted["module:normal"]["tags"], ())
            self.assertEqual(
                inserted["module:special-code"]["text"],
                "【特殊代码段】结算退出",
            )
            self.assertEqual(
                inserted["module:special-code"]["values"],
                (
                    "未设置区域（全屏）",
                    "附加：重新执行工作流；超时：结束当前最里层脚本，继续执行",
                ),
            )
            self.assertEqual(
                inserted["module:special-code"]["tags"], ("special_action",),
            )

    def test_manager_toggle_selected_enabled_persists_and_reselects(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        obj = {"name": "退出队伍", "category": "workflow_global", "enabled": True}
        dialog.objects = {"module:item": obj}
        dialog.current = "workflow_global"
        tree = Mock()
        tree.selection.return_value = ("module:item",)
        dialog.trees = {"workflow_global": tree}
        dialog._update_action_buttons = Mock()
        with patch("macroflow.ui.dialogs.save_module_objects") as save, \
             patch.object(TemplateRegionManagerDialog, "_reload_trees") as reload_trees:
            dialog._toggle_selected_enabled()
        self.assertFalse(obj["enabled"])
        save.assert_called_once_with(dialog.objects)
        reload_trees.assert_called_once_with()
        tree.selection_set.assert_called_once_with("module:item")
        tree.see.assert_called_once_with("module:item")
        dialog._update_action_buttons.assert_called_once_with()

    def test_pinyin_sort_key_orders_chinese_names(self):
        names = ["张三", "阿明", "李四", "白云"]
        self.assertEqual(
            sorted(names, key=pinyin_sort_key),
            ["阿明", "白云", "李四", "张三"],
        )

    def test_manager_sort_direction_reloads_all_trees(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.sort_direction = "asc"
        tree = Mock()
        dialog.trees = {"all": tree}
        dialog._reload_trees = Mock()
        dialog._set_sort_direction("desc")
        self.assertEqual(dialog.sort_direction, "desc")
        dialog._reload_trees.assert_called_once_with()
        heading = tree.heading.call_args
        self.assertEqual(heading.args[0], "#0")
        self.assertEqual(heading.kwargs["text"], "模块名称 ↓")

    def test_clicking_sort_heading_toggles_direction(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.sort_direction = "asc"
        dialog._set_sort_direction = Mock()
        dialog._toggle_sort_direction()
        dialog._set_sort_direction.assert_called_once_with("desc")

    def test_inventory_filter_switches_visible_group_and_reloads(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.inventory_filter = "all"
        dialog.inventory_filter_buttons = {
            "all": Mock(), "adopted": Mock(), "unused": Mock(),
        }
        tree = Mock()
        dialog.trees = {"images": tree}
        dialog._reload_tree = Mock()
        dialog._set_inventory_filter("unused")
        self.assertEqual(dialog.inventory_filter, "unused")
        dialog._reload_tree.assert_called_once_with("images", tree)
        self.assertEqual(
            dialog.inventory_filter_buttons["unused"].configure.call_args.kwargs["background"],
            "#244D78",
        )

    def test_template_regions_storage_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                save_template_regions({"images/a.png": [10, 20, 300, 400]})
                loaded = load_template_regions()
            self.assertEqual(loaded, {"images/a.png": [10, 20, 300, 400]})

    def test_module_enabled_state_roundtrips_and_defaults_to_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                save_module_objects({
                    "module:disabled": {
                        "name": "停用模块", "category": "switch", "enabled": False,
                    },
                    "module:default": {
                        "name": "默认模块", "category": "switch",
                    },
                })
                loaded = load_module_objects()
            self.assertFalse(loaded["module:disabled"]["enabled"])
            self.assertTrue(loaded["module:default"]["enabled"])

    def test_text_absent_wait_state_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                save_module_objects({
                    "module:text": {
                        "name": "等待加载结束", "category": "switch",
                        "recognize": "text", "expected_text": "加载中",
                        "wait_text_absent": True,
                        "ocr_offset_up": 6, "ocr_offset_down": 7,
                        "ocr_offset_left": 8, "ocr_offset_right": 9,
                    },
                })
                loaded = load_module_objects()
            self.assertTrue(loaded["module:text"]["wait_text_absent"])
            self.assertEqual(loaded["module:text"]["ocr_offset_up"], 6)
            self.assertEqual(loaded["module:text"]["ocr_offset_down"], 7)
            self.assertEqual(loaded["module:text"]["ocr_offset_left"], 8)
            self.assertEqual(loaded["module:text"]["ocr_offset_right"], 9)

    def test_number_module_roundtrips_as_read_only_switch_module(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                save_module_objects({
                    "module:number": {
                        "name": "剩余次数", "category": "workflow_global",
                        "recognize": "number", "template": "images/old.png",
                        "region": [10, 20, 80, 30], "after_action": "click_match",
                        "run_code_after_action": True,
                        "on_success_actions": [{"type": "click"}],
                    },
                })
                loaded = load_module_objects()["module:number"]
                template_regions = load_template_regions()
        self.assertEqual(loaded["category"], "switch")
        self.assertEqual(loaded["recognize"], "number")
        self.assertEqual(loaded["template"], "")
        self.assertEqual(loaded["region"], [10, 20, 80, 30])
        self.assertEqual(loaded["after_action"], "continue")
        self.assertFalse(loaded["run_code_after_action"])
        self.assertEqual(loaded["on_success_actions"], [])
        self.assertNotIn("", template_regions)

    def test_template_regions_filters_invalid_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            path.write_text(json.dumps({
                "images/good.png": [1, 2, 3, 4],
                "images/unset.png": [0, 0, 0, 0],
                "images/short.png": [1, 2, 3],
                "images/typed.png": ["x", "y", "w", "h"],
                "images/negative.png": [1, 2, -3, 4],
                "": [1, 2, 3, 4],
            }), encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                loaded = load_template_regions()
            # 未设置区域（全 0）的占位条目合法保留，格式错误的丢弃。
            self.assertEqual(loaded, {
                "images/good.png": [1, 2, 3, 4],
                "images/unset.png": [0, 0, 0, 0],
            })

    def test_unset_template_region_placeholder_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                save_template_regions({"images/new.png": [0, 0, 0, 0]})
                loaded = load_template_regions()
            self.assertEqual(loaded, {"images/new.png": [0, 0, 0, 0]})

    def test_module_objects_migrate_special_detection_back_to_global(self):
        # 1.81 曾把旧全局模块并入特殊；1.82 起特殊分类只放纯动作，检测型条目
        # （有图 / 未标纯动作）加载时惰性迁回「全局模块」，字段全保留；纯动作不动。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            path.write_text(json.dumps({
                "images/g.png": {"category": "global", "region": [10, 20, 300, 400]},
                "images/s2.png": {"category": "special", "region": [1, 2, 300, 400],
                                  "hold_ms": 2000, "blocking": True},
                "重新执行工作流": {"category": "special", "name": "重新执行工作流",
                              "pure_action": True},
            }), encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                objects = load_module_objects()
            # 旧 global 类别迁移为工作流全局。
            self.assertEqual(objects["images/g.png"]["category"], "workflow_global")
            self.assertEqual(objects["images/g.png"]["region"], [10, 20, 300, 400])
            # 检测型特殊（有图）→ 全局，字段全保留。
            obj = objects["images/s2.png"]
            self.assertEqual(obj["category"], "workflow_global")
            self.assertEqual(obj["region"], [1, 2, 300, 400])
            self.assertEqual(obj["hold_ms"], 2000)
            self.assertTrue(obj["blocking"])
            self.assertFalse(obj.get("pure_action"))
            # 纯动作特殊保持特殊。
            self.assertEqual(objects["重新执行工作流"]["category"], "special")
            self.assertTrue(objects["重新执行工作流"].get("pure_action"))

    def test_module_objects_migrate_legacy_run_actions_to_optional_post_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            path.write_text(json.dumps({
                "images/legacy.png": {
                    "category": "switch", "region": [1, 2, 3, 4],
                    "after_action": "run_actions",
                    "on_success_actions": [{"type": "restart_workflow"}],
                },
            }), encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                obj = load_module_objects()["images/legacy.png"]
        self.assertEqual(obj["after_action"], "continue")
        self.assertTrue(obj["run_code_after_action"])
        self.assertEqual(obj["on_success_actions"][0]["type"], "restart_workflow")

    def test_module_objects_normalize_independent_timeout_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            path.write_text(json.dumps({
                "images/timeout.png": {
                    "category": "switch", "region": [1, 2, 3, 4],
                    "run_code_on_timeout": True,
                    "not_found_timeout_ms": "4500",
                    "on_timeout_actions": [{"type": "delay", "ms": 10}],
                },
            }), encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                obj = load_module_objects()["images/timeout.png"]
        self.assertTrue(obj["run_code_on_timeout"])
        self.assertEqual(obj["not_found_timeout_ms"], 4500)
        self.assertEqual(obj["on_timeout_actions"][0]["type"], "delay")
        self.assertEqual(obj["on_success_actions"], [])

    def test_load_module_objects_seeds_default_pure_action_special(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                objects = load_module_objects()
            # 空仓库补种两个固定纯动作，且不落盘。
            self.assertIn("重新执行工作流", objects)
            self.assertIn("结束当前最里层脚本，继续执行", objects)
            self.assertTrue(objects["重新执行工作流"].get("pure_action"))
            self.assertFalse(path.exists())
            # 仓库已有用户纯动作时仍独立补齐固定条目。
            path.write_text(json.dumps({
                "自定特殊": {"category": "special", "name": "自定特殊", "pure_action": True},
            }), encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                loaded = load_module_objects()
            self.assertIn("重新执行工作流", loaded)
            self.assertIn("结束当前最里层脚本，继续执行", loaded)
            self.assertIn("自定特殊", loaded)

    def test_load_template_regions_skips_pure_action_specials(self):
        # 纯动作特殊模块（无图片）不进入模板下拉。
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            path.write_text(json.dumps({
                "images/s.png": {"category": "switch", "region": [1, 2, 3, 4]},
                "重新执行工作流": {"category": "special", "pure_action": True},
            }), encoding="utf-8")
            with patch("macroflow.core.storage.TEMPLATE_REGIONS_PATH", path):
                regions = load_template_regions()
            self.assertEqual(regions, {"images/s.png": [1, 2, 3, 4]})

    def test_load_template_regions_skips_no_recognition_modules(self):
        with patch("macroflow.core.storage.load_module_objects", return_value={
            "module:direct": {
                "recognize": "none", "template": "", "region": [0, 0, 0, 0],
            },
            "module:image": {
                "template": "images/a.png", "region": [1, 2, 3, 4],
            },
        }):
            self.assertEqual(load_template_regions(), {"images/a.png": [1, 2, 3, 4]})

    def test_registered_template_region_lookup(self):
        key = display_path("images/a.png")
        with patch("macroflow.core.storage.load_template_regions", return_value={key: [10, 20, 300, 400]}):
            self.assertEqual(registered_template_region("images/a.png"), [10, 20, 300, 400])
        with patch("macroflow.core.storage.load_template_regions", return_value={}):
            self.assertIsNone(registered_template_region("images/missing.png"))

    def test_registered_template_options_include_legacy_value(self):
        with patch("macroflow.ui.dialogs.load_template_regions", return_value={"images/b.png": [1, 2, 3, 4]}):
            self.assertEqual(registered_template_options(), ["images/b.png"])
            # 编辑旧动作：模板不在注册表时临时加回，保证下拉显示原值。
            self.assertEqual(
                registered_template_options("images/legacy.png"),
                ["images/legacy.png", "images/b.png"],
            )

    def test_fallback_template_options_has_disabled_first(self):
        with patch("macroflow.ui.dialogs.load_template_regions", return_value={"images/b.png": [1, 2, 3, 4]}):
            self.assertEqual(
                fallback_template_options("images/legacy.png"),
                ["（不启用）", "images/legacy.png", "images/b.png"],
            )
            self.assertEqual(fallback_template_options(""), ["（不启用）", "images/b.png"])

    def test_open_template_region_manager_shows_and_refreshes(self):
        # 打开管理器（show）后刷新模板下拉；管理器里已删除的模板被清空。
        with patch("macroflow.ui.dialogs.TemplateRegionManagerDialog") as manager_class, \
             patch("macroflow.ui.dialogs.load_template_regions", return_value={"images/g.png": [1, 2, 3, 4]}):
            dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
            dialog.template = Mock()
            dialog.template.get.return_value = "images/g.png"
            dialog.template_combo = Mock()
            dialog.open_template_region_manager()
        manager_class.assert_called_once_with(dialog)
        manager_class.return_value.show.assert_called_once()
        dialog.template_combo.configure.assert_called_once_with(values=["images/g.png"])

    def test_refresh_template_options_clears_removed_template(self):
        dialog = GlobalDetectDialog.__new__(GlobalDetectDialog)
        dialog.template = Mock()
        dialog.template.get.return_value = "images/removed.png"
        dialog.template_combo = Mock()
        with patch("macroflow.ui.dialogs.load_template_regions", return_value={"images/g.png": [1, 2, 3, 4]}):
            dialog._refresh_template_options()
        dialog.template.set.assert_called_once_with("")
        dialog.template_combo.configure.assert_called_once_with(values=["images/g.png"])

    def test_editor_page_opens_unified_region_manager(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        with patch("macroflow.ui.app.TemplateRegionManagerDialog") as manager_class:
            app.open_template_region_manager()
        manager_class.assert_called_once_with(app.root)
        manager_class.return_value.show.assert_called_once()

    def _object(self, region=(10, 20, 300, 400), category="switch", **overrides):
        obj = {
            "category": category, "region": list(region), "threshold": 0.85,
            "interval_ms": 250, "blocking": False, "hold_ms": 1000,
            "delay_ms": 0, "after_action": "click_match", "click_point": [],
            "button": "left", "second_match_template": "",
            "second_match_region": [], "second_match_timeout_ms": 3000,
            "on_success_actions": [], "run_code_on_timeout": False,
            "not_found_timeout_ms": 3000, "on_timeout_actions": [],
        }
        obj.update(overrides)
        return obj

    def test_segment_blocking_module_has_visible_warning_marker(self):
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:blocking", "template": "images/wait.png",
        }
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={"blocking": True}):
            self.assertTrue(segment_action_is_blocking(action))
            self.assertEqual(segment_row_label(action), "【阻塞等待】识图 wait")

    def test_segment_text_absent_module_is_also_marked_blocking(self):
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:text", "template": "",
        }
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={
            "blocking": False, "recognize": "text", "wait_text_absent": True,
        }):
            self.assertTrue(segment_action_is_blocking(action))

    def test_segment_nonblocking_module_has_no_warning_marker(self):
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:normal", "template": "images/next.png",
        }
        with patch("macroflow.ui.dialogs.registered_module_object", return_value={"blocking": False}):
            self.assertFalse(segment_action_is_blocking(action))
            self.assertEqual(segment_row_label(action), "识图 next")

    def test_reload_segment_list_colors_every_blocking_module(self):
        form = TemplateRegionFormDialog.__new__(TemplateRegionFormDialog)
        form.segment = [
            {"type": "image_match", "module_ref": True,
             "module_key": "module:block", "template": "images/block.png"},
            {"type": "image_match", "module_ref": True,
             "module_key": "module:normal", "template": "images/normal.png"},
        ]
        form.segment_listbox = Mock()

        def lookup(key):
            return {"blocking": key == "module:block"}

        with patch("macroflow.ui.dialogs.registered_module_object", side_effect=lookup):
            form._reload_segment_list()

        form.segment_listbox.itemconfigure.assert_called_once_with(
            0, foreground="#F2B84B",
        )

    def test_manager_reload_tree_shows_category_and_region(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        trees = {
            key: Mock() for key in (
                "all", "switch", "workflow_global", "script_global", "special",
            )
        }
        for tree in trees.values():
            tree.get_children.return_value = ["old"]
        dialog.trees = trees
        dialog.objects = {
            "images/a.png": self._object(),
            "images/b.png": self._object(region=(0, 0, 0, 0)),
            "images/g.png": self._object(category="workflow_global"),
            "images/sg.png": self._object(category="script_global"),
            "重新执行工作流": {
                "category": "special", "name": "重新执行工作流", "pure_action": True,
            },
        }
        dialog._reload_trees()
        for tree in trees.values():
            tree.delete.assert_called_once_with("old")
        # 全部页签：switch / global 显示区域，纯动作显示名称与区域 "—"。
        calls = trees["all"].insert.call_args_list
        by_iid = {call.kwargs["iid"]: call.kwargs for call in calls}
        self.assertEqual(by_iid["images/a.png"]["values"], ("10,20,300,400", "—"))
        self.assertEqual(by_iid["images/b.png"]["values"], ("未设置区域（全屏）", "—"))
        self.assertEqual(by_iid["images/g.png"]["values"], ("10,20,300,400", "—"))
        self.assertEqual(by_iid["重新执行工作流"]["text"], "重新执行工作流")
        self.assertEqual(by_iid["重新执行工作流"]["values"], ("—", "固定特殊模块"))
        # 切换 / 全局页签各列所属类别；特殊页签只列纯动作（名称 + 类型）。
        self.assertEqual(
            [call.kwargs["iid"] for call in trees["switch"].insert.call_args_list],
            ["images/a.png", "images/b.png"],
        )
        self.assertEqual(
            [call.kwargs["iid"] for call in trees["workflow_global"].insert.call_args_list],
            ["images/g.png"],
        )
        self.assertEqual(
            [call.kwargs["iid"] for call in trees["script_global"].insert.call_args_list],
            ["images/sg.png"],
        )
        special_calls = trees["special"].insert.call_args_list
        self.assertEqual(
            [call.kwargs["iid"] for call in special_calls], ["重新执行工作流"],
        )
        self.assertEqual(special_calls[0].kwargs["values"], ("特殊",))

    def _form(self, image="", region="", after_action="点击识别区域", recognize="模板图片"):
        """构造表单桩：__new__ 跳过 __init__，用 Mock 变量代替控件。"""
        form = TemplateRegionFormDialog.__new__(TemplateRegionFormDialog)
        form.old_key = ""
        form.segment_depth = 0
        form.images_dir = Path(tempfile.gettempdir())
        form.image_var = Mock()
        form.image_var.get.return_value = image
        form.region_var = Mock()
        form.region_var.get.return_value = region
        form.recognize_var = Mock()
        form.recognize_var.get.return_value = recognize
        form.expected_text_var = Mock()
        form.expected_text_var.get.return_value = ""
        form.match_mode_var = Mock()
        form.match_mode_var.get.return_value = "包含"
        form.wait_text_absent_var = Mock()
        form.wait_text_absent_var.get.return_value = False
        form.ocr_offset_up_var = Mock()
        form.ocr_offset_up_var.get.return_value = "0"
        form.ocr_offset_down_var = Mock()
        form.ocr_offset_down_var.get.return_value = "0"
        form.ocr_offset_left_var = Mock()
        form.ocr_offset_left_var.get.return_value = "0"
        form.ocr_offset_right_var = Mock()
        form.ocr_offset_right_var.get.return_value = "0"
        form.category_var = Mock()
        form.category_var.get.return_value = "切换模块"
        form.threshold_var = Mock()
        form.threshold_var.get.return_value = "0.85"
        form.ignore_background_var = Mock()
        form.ignore_background_var.get.return_value = False
        form.interval_var = Mock()
        form.interval_var.get.return_value = "250"
        form.start_delay_var = Mock()
        form.start_delay_var.get.return_value = "0"
        form.fallback_module_key_var = Mock()
        form.fallback_module_key_var.get.return_value = ""
        form.fallback_click_var = Mock()
        form.fallback_click_var.get.return_value = False
        form.fallback_on_match_var = Mock()
        form.fallback_on_match_var.get.return_value = "继续识别主模块（不点击）"
        form.blocking_var = Mock()
        form.blocking_var.get.return_value = False
        form.hold_enabled_var = Mock()
        form.hold_enabled_var.get.return_value = False  # 持续延时默认不启用
        form.hold_var = Mock()
        form.hold_var.get.return_value = "1000"
        form.delay_var = Mock()
        form.delay_var.get.return_value = "0"
        form.after_action_var = Mock()
        form.after_action_var.get.return_value = after_action
        form.run_code_after_action_var = Mock()
        form.run_code_after_action_var.get.return_value = False
        form.run_code_on_timeout_var = Mock()
        form.run_code_on_timeout_var.get.return_value = False
        form.not_found_timeout_var = Mock()
        form.not_found_timeout_var.get.return_value = "3000"
        form.button_var = Mock()
        form.button_var.get.return_value = "left"
        form.click_count_var = Mock()
        form.click_count_var.get.return_value = "1"
        form.click_point_var = Mock()
        form.click_point_var.get.return_value = ""
        form.second_template_var = Mock()
        form.second_template_var.get.return_value = ""
        form.second_region_var = Mock()
        form.second_region_var.get.return_value = ""
        form.second_timeout_var = Mock()
        form.second_timeout_var.get.return_value = "3000"
        form.second_click_target_var = Mock()
        form.second_click_target_var.get.return_value = "第二次识别位置"
        form.second_click_region_var = Mock()
        form.second_click_region_var.get.return_value = ""
        form.segment = []
        form.timeout_segment = []
        form.name_var = Mock()
        form.name_var.get.return_value = ""
        form._toggle_sections = Mock()
        form.destroy = Mock()
        return form

    def test_form_capture_saves_image_and_fills_vars(self):
        # "截图新建…"：框选区域截图存为新模板图片，图片与区域两项一起填入。
        form = self._form()
        form.master = Mock()
        with patch("macroflow.ui.dialogs.ScreenRegionPicker") as picker_class:
            form._capture()
        on_result = picker_class.call_args[0][2]
        self.assertEqual(picker_class.return_value.start.call_count, 1)
        with tempfile.TemporaryDirectory() as folder:
            images_dir = Path(folder) / "images"
            form.images_dir = images_dir
            screen = np.zeros((40, 50, 3), dtype=np.uint8)
            with patch("macroflow.ui.dialogs.capture_bgr", return_value=(screen, (0, 0))):
                on_result([10, 20, 30, 40])
            saved = list(images_dir.glob("template_*.png"))
            self.assertEqual(len(saved), 1)
            key = str(saved[0])
        form.image_var.set.assert_called_once_with(key)
        form.region_var.set.assert_called_once_with("10,20,30,40")

    def test_form_capture_failure_shows_notice(self):
        form = self._form()
        form.master = Mock()
        with patch("macroflow.ui.dialogs.ScreenRegionPicker") as picker_class:
            form._capture()
        on_result = picker_class.call_args[0][2]
        with patch("macroflow.ui.dialogs.capture_bgr", side_effect=RuntimeError("boom")), \
             patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            on_result([10, 20, 30, 40])
        notice.assert_called_once()
        self.assertIn("截图失败", notice.call_args.args[1])
        form.image_var.set.assert_not_called()
        form.region_var.set.assert_not_called()

    def test_form_choose_image_sets_image_var(self):
        form = self._form()
        chosen = r"C:\images\部分\勇士挑战确定.png"
        with patch("macroflow.ui.dialogs.filedialog.askopenfilename", return_value=chosen):
            form._choose_image()
        form.image_var.set.assert_called_once_with(chosen)
        form.name_var.set.assert_called_once_with("勇士挑战确定")

    def test_form_choose_image_preserves_custom_name(self):
        form = self._form()
        form.name_var.get.return_value = "手动名称"
        with patch("macroflow.ui.dialogs.filedialog.askopenfilename", return_value=r"C:\images\new.png"):
            form._choose_image()
        form.name_var.set.assert_not_called()

    def test_module_default_name_uses_final_path_stem(self):
        self.assertEqual(
            TemplateRegionFormDialog._default_name_for_image(
                r"images\部分\勇士挑战确定.png"
            ),
            "勇士挑战确定",
        )

    def test_form_pick_region_fills_region_var(self):
        form = self._form()
        form.master = Mock()
        with patch("macroflow.ui.dialogs.ScreenRegionPicker") as picker_class:
            form._pick_region()
        on_result = picker_class.call_args[0][2]
        on_result([1, 2, 3, 4])
        form.region_var.set.assert_called_once_with("1,2,3,4")

    def test_form_save_requires_image(self):
        form = self._form(image="", region="10,20,300,400")
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少模板图片", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_requires_region(self):
        form = self._form(image="images/g.png", region="")
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少框选区域", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_rejects_malformed_region(self):
        for region in ("1,2,3", "x,y,w,h", "1,2,-3,4"):
            form = self._form(image="images/g.png", region=region)
            with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
                form.save()
            notice.assert_called_once()
            form.destroy.assert_not_called()

    def test_form_save_sets_result_and_closes(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[0], "")
        self.assertTrue(form.result[1].startswith("module:"))
        obj = form.result[2]
        self.assertEqual(obj["template"], "images/g.png")
        self.assertEqual(obj["category"], "switch")
        self.assertEqual(obj["region"], [10, 20, 300, 400])
        self.assertEqual(obj["after_action"], "click_match")
        self.assertFalse(obj["run_code_after_action"])
        self.assertEqual(obj["threshold"], 0.85)
        self.assertEqual(obj["interval_ms"], 250)
        self.assertEqual(obj["start_delay_ms"], 0)
        self.assertEqual(obj["fallback_module_key"], "")
        self.assertFalse(obj["fallback_click"])
        self.assertFalse(obj["blocking"])
        self.assertFalse(obj["hold_enabled"])  # 持续延时默认不启用
        self.assertEqual(obj["hold_ms"], 1000)
        self.assertEqual(obj["delay_ms"], 0)
        self.assertEqual(obj["button"], "left")
        self.assertEqual(obj["click_point"], [])
        self.assertEqual(obj["second_match_template"], "")
        self.assertEqual(obj["second_match_region"], [])
        self.assertEqual(obj["second_match_timeout_ms"], 3000)
        self.assertEqual(obj["second_match_click_target"], "second")
        self.assertEqual(obj["second_match_click_region"], [])
        self.assertEqual(obj["on_success_actions"], [])
        self.assertFalse(obj["run_code_on_timeout"])
        self.assertEqual(obj["not_found_timeout_ms"], 3000)
        self.assertEqual(obj["on_timeout_actions"], [])
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_can_disable_global_hold_delay(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "工作流全局模块"
        form.hold_enabled_var.get.return_value = False

        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()

        self.assertFalse(form.result[2]["hold_enabled"])
        self.assertEqual(form.result[2]["hold_ms"], 1000)
        notice.assert_not_called()

    def test_form_saves_start_delay_only_for_script_global_module(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "脚本全局模块"
        form.start_delay_var.get.return_value = "125000"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["start_delay_ms"], 125000)
        notice.assert_not_called()

    def test_restart_target_dialog_save_picks_row_object(self):
        dialog = RestartWorkflowTargetDialog.__new__(RestartWorkflowTargetDialog)
        dialog.row_ids = {"（使用默认跳转行）": 0, "第 2 行 · 脚本a": 2}
        dialog.row_var = Mock()
        dialog.row_var.get.return_value = "第 2 行 · 脚本a"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result["restart_workflow_target_row"], 2)
        dialog.destroy.assert_called_once()

    def test_restart_target_dialog_save_use_default(self):
        dialog = RestartWorkflowTargetDialog.__new__(RestartWorkflowTargetDialog)
        dialog.row_ids = {"（使用默认跳转行：第 4 行）": 0}
        dialog.row_var = Mock()
        dialog.row_var.get.return_value = "（使用默认跳转行：第 4 行）"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result["restart_workflow_target_row"], 0)
        dialog.destroy.assert_called_once()

    def test_restart_target_dialog_save_custom_row(self):
        dialog = RestartWorkflowTargetDialog.__new__(RestartWorkflowTargetDialog)
        dialog.row_ids = {}
        dialog.row_var = Mock()
        dialog.row_var.get.return_value = "自定义行号…"
        dialog.row_spin_var = Mock()
        dialog.row_spin_var.get.return_value = "7"
        dialog.destroy = Mock()
        dialog.save()
        self.assertEqual(dialog.result["restart_workflow_target_row"], 7)

    def test_restart_workflow_row_options_label_rows(self):
        labels, mapping = restart_workflow_row_options(
            [{"kind": "script", "script": "scripts/a.json"},
             {"kind": "module", "action": {"module_name": "可领取"}}],
            default_row=2,
        )
        self.assertEqual(mapping[labels[0]], 0)
        self.assertIn("（使用默认跳转行：第 2 行）", labels[0])
        self.assertEqual(mapping["第 1 行 · a"], 1)
        self.assertEqual(mapping["第 2 行 · 模块 可领取"], 2)

    def test_form_saves_fallback_module_and_click_behavior(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.fallback_module_key_var.get.return_value = "module:fallback"
        form.fallback_click_var.get.return_value = True
        form.fallback_on_match_var.get.return_value = "点击备用命中位置，继续识别主模块"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["fallback_module_key"], "module:fallback")
        self.assertTrue(form.result[2]["fallback_click"])
        self.assertEqual(
            form.result[2]["fallback_on_match"], "click_continue",
        )
        notice.assert_not_called()

    def test_form_saves_fallback_exit_option(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.fallback_module_key_var.get.return_value = "module:fallback"
        form.fallback_on_match_var.get.return_value = "点击备用命中位置后退出主模块识别"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["fallback_on_match"], "click_exit")
        self.assertTrue(form.result[2]["fallback_click"])
        notice.assert_not_called()

    def test_form_save_click_custom_requires_point(self):
        form = self._form(
            image="images/g.png", region="10,20,300,400",
            after_action="点击自定义位置",
        )
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        form.destroy.assert_not_called()
        form.click_point_var.get.return_value = "120,340"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["after_action"], "click_custom")
        self.assertEqual(form.result[2]["click_point"], [120, 340])
        form.destroy.assert_called_once()

    def test_form_save_second_match_requires_template(self):
        form = self._form(
            image="images/g.png", region="10,20,300,400",
            after_action="二次识别后点击",
        )
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少二次识别模板", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_second_match_custom_click_region(self):
        form = self._form(
            image="images/g.png", region="10,20,300,400",
            after_action="二次识别后点击",
        )
        form.second_template_var.get.return_value = "images/second.png"
        form.second_click_target_var.get.return_value = "自定义框选区域"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertIn("缺少自定义点击区域", notice.call_args.args[1])
        form.destroy.assert_not_called()

        form.second_click_region_var.get.return_value = "100,200,80,40"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        obj = form.result[2]
        self.assertEqual(obj["second_match_click_target"], "custom_region")
        self.assertEqual(obj["second_match_click_region"], [100, 200, 80, 40])
        form.destroy.assert_called_once()

    def test_form_save_enabled_post_action_code_requires_segment(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.run_code_after_action_var.get.return_value = True
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("代码段为空", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_continue_with_post_action_code(self):
        form = self._form(
            image="images/g.png", region="10,20,300,400",
            after_action="成功后继续",
        )
        form.run_code_after_action_var.get.return_value = True
        form.segment = [{"type": "restart_workflow"}]
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        obj = form.result[2]
        self.assertEqual(obj["after_action"], "continue")
        self.assertTrue(obj["run_code_after_action"])
        self.assertEqual(obj["on_success_actions"][0]["type"], "restart_workflow")
        self.assertTrue(obj["on_success_actions"][0]["action_id"])
        notice.assert_not_called()

    def test_form_save_independent_not_found_timeout_code(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.run_code_on_timeout_var.get.return_value = True
        form.not_found_timeout_var.get.return_value = "4200"
        form.timeout_segment = [{"type": "delay", "ms": 25}]
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        obj = form.result[2]
        self.assertTrue(obj["run_code_on_timeout"])
        self.assertEqual(obj["not_found_timeout_ms"], 4200)
        self.assertEqual(obj["on_timeout_actions"][0]["type"], "delay")
        self.assertEqual(obj["on_success_actions"], [])
        self.assertTrue(obj["on_timeout_actions"][0]["action_id"])
        notice.assert_not_called()

    def test_add_segment_activate_window_stores_stable_signature(self):
        form = self._form()
        form.segment_listbox = Mock()
        selected = WindowInfo(321, "目标窗口", "GameWnd", r"C:\\Game\\game.exe")
        with patch("macroflow.ui.dialogs.WindowPicker") as picker:
            picker.return_value.show.return_value = selected
            form._add_segment_activate_window()
        action = form.segment[0]
        self.assertEqual(action["type"], "activate_window")
        self.assertEqual(action["window"]["title"], "目标窗口")
        self.assertEqual(action["window"]["class_name"], "GameWnd")
        self.assertTrue(action["action_id"])

    def test_segment_add_menu_includes_repeat_click_for_timeout_code_block(self):
        form = self._form()
        form.winfo_pointerx = Mock(return_value=10)
        form.winfo_pointery = Mock(return_value=20)
        menu = Mock()
        with patch("macroflow.ui.dialogs.tk.Menu", return_value=menu):
            form._add_segment_item("timeout_segment", "timeout_segment_listbox")
        commands = {
            item.kwargs.get("label"): item.kwargs.get("command")
            for item in menu.add_command.call_args_list
        }
        self.assertIn("连续点击", commands)
        form._add_segment_dialog = Mock()
        commands["连续点击"]()
        form._add_segment_dialog.assert_called_once_with(
            RepeatClickDialog, "timeout_segment", "timeout_segment_listbox",
        )

    def test_nested_click_picker_hides_every_ancestor_window(self):
        root = Mock()
        root.master = None
        manager = Mock()
        manager.master = root
        module_form = Mock()
        module_form.master = manager
        dialog = ClickDialog.__new__(ClickDialog)
        dialog.master = module_form
        dialog._apply_picked_point = Mock()
        with patch("macroflow.ui.dialogs.ScreenPointPicker") as picker:
            dialog.start_pick_position()
        picker.assert_called_once_with(
            dialog, module_form, dialog._apply_picked_point,
            tip_text="点击要执行操作的位置；只记录坐标，不会点击下方窗口；Esc 取消",
            hidden_windows=[manager, root],
        )
        picker.return_value.start.assert_called_once()

    def test_ctrl_a_selects_all_module_segment_actions(self):
        form = self._form()
        form.segment_listbox = Mock()

        result = form._select_all_segment_items()

        self.assertEqual(result, "break")
        form.segment_listbox.selection_set.assert_called_once_with(0, "end")

    def test_remove_segment_item_deletes_every_selected_action(self):
        form = self._form()
        form.segment = [
            {"type": "delay", "ms": 1},
            {"type": "delay", "ms": 2},
            {"type": "delay", "ms": 3},
        ]
        form.segment_listbox = Mock()
        form.segment_listbox.curselection.return_value = (0, 2)

        form._remove_segment_item()

        self.assertEqual(form.segment, [{"type": "delay", "ms": 2}])

    def test_form_save_pure_action_requires_name(self):
        # 特殊模块纯动作（未选图片）：名称必填。
        form = self._form(image="")
        form.category_var.get.return_value = "特殊模块"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少名称", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_pure_action_sets_result(self):
        # 特殊模块纯动作保存：名称做 key，对象只有 category/name/pure_action。
        form = self._form(image="")
        form.category_var.get.return_value = "特殊模块"
        form.name_var.get.return_value = "重新执行工作流"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[0], "")
        self.assertEqual(form.result[1], "重新执行工作流")
        self.assertEqual(form.result[2], {
            "category": "special", "name": "重新执行工作流", "pure_action": True,
        })
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_save_global_with_image_sets_global_category(self):
        # 全局模块（检测型，选了图片）：对象类别为 global，其余字段照常。
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "工作流全局模块"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["category"], "workflow_global")
        self.assertEqual(form.result[2]["region"], [10, 20, 300, 400])
        self.assertNotIn("pure_action", form.result[2])
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_save_text_mode_allows_no_image_or_region(self):
        # 识别文字方式：不需要模板图片，区域可留空（空=全屏），名称缺省"识别文字"。
        form = self._form(recognize="识别文字")
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        module = form.result[2]
        self.assertEqual(module["recognize"], "text")
        self.assertEqual(module["template"], "")
        self.assertEqual(module["region"], [])
        self.assertEqual(module["expected_text"], "")
        self.assertEqual(module["match_mode"], "contains")
        self.assertEqual(module["name"], "识别文字")
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_save_number_mode_requires_region_and_has_no_click_side_effects(self):
        form = self._form(region="10,20,80,30", recognize="读取数字")
        form.run_code_after_action_var.get.return_value = True
        form.run_code_on_timeout_var.get.return_value = True
        form.segment = [{"type": "click"}]
        form.timeout_segment = [{"type": "click"}]
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        module = form.result[2]
        self.assertEqual(module["recognize"], "number")
        self.assertEqual(module["name"], "读取数字")
        self.assertEqual(module["template"], "")
        self.assertEqual(module["region"], [10, 20, 80, 30])
        self.assertEqual(module["after_action"], "continue")
        self.assertFalse(module["run_code_after_action"])
        self.assertFalse(module["run_code_on_timeout"])
        self.assertEqual(module["on_success_actions"], [])
        self.assertEqual(module["on_timeout_actions"], [])
        notice.assert_not_called()

    def test_form_save_number_mode_rejects_missing_region(self):
        form = self._form(recognize="读取数字")
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertIn("缺少框选区域", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_number_mode_rejects_global_category(self):
        form = self._form(region="10,20,80,30", recognize="读取数字")
        form.category_var.get.return_value = "工作流全局模块"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertIn("类别不适用", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_no_recognition_mode_runs_directly_without_image_or_region(self):
        form = self._form(recognize="无需识图")
        form.run_code_after_action_var.get.return_value = True
        form.segment = [{"type": "delay", "ms": 25}]
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        module = form.result[2]
        self.assertEqual(module["recognize"], "none")
        self.assertEqual(module["template"], "")
        self.assertEqual(module["region"], [])
        self.assertEqual(module["after_action"], "continue")
        self.assertTrue(module["run_code_after_action"])
        self.assertFalse(module["run_code_on_timeout"])
        self.assertEqual(module["name"], "无需识图")
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_allows_no_recognition_for_global_module_timeout(self):
        form = self._form(recognize="无需识图")
        form.category_var.get.return_value = "工作流全局模块"
        form.run_code_on_timeout_var.get.return_value = True
        form.timeout_segment = [{"type": "delay", "ms": 25}]
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        module = form.result[2]
        self.assertEqual(module["recognize"], "none")
        self.assertTrue(module["run_code_on_timeout"])
        self.assertEqual(module["on_timeout_actions"][0]["type"], "delay")
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_save_text_mode_keeps_expected_text_and_equals(self):
        form = self._form(region="10,20,300,400", recognize="识别文字")
        form.expected_text_var.get.return_value = "体力不足"
        form.match_mode_var.get.return_value = "等于"
        form.wait_text_absent_var.get.return_value = True
        form.ocr_offset_up_var.get.return_value = "8"
        form.ocr_offset_down_var.get.return_value = "2"
        form.ocr_offset_left_var.get.return_value = "4"
        form.ocr_offset_right_var.get.return_value = "12"
        form.name_var.get.return_value = "体力检测"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        module = form.result[2]
        self.assertEqual(module["recognize"], "text")
        self.assertEqual(module["expected_text"], "体力不足")
        self.assertEqual(module["match_mode"], "equals")
        self.assertTrue(module["wait_text_absent"])
        self.assertEqual(module["ocr_offset_up"], 8)
        self.assertEqual(module["ocr_offset_down"], 2)
        self.assertEqual(module["ocr_offset_left"], 4)
        self.assertEqual(module["ocr_offset_right"], 12)
        self.assertEqual(module["region"], [10, 20, 300, 400])
        self.assertEqual(module["name"], "体力检测")
        notice.assert_not_called()

    def test_form_save_template_mode_keeps_wait_until_absent_option(self):
        form = self._form(
            image="images/claim.png", region="10,20,300,400", recognize="模板图片",
        )
        form.wait_text_absent_var.get.return_value = True
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        module = form.result[2]
        self.assertTrue(module["wait_text_absent"])
        self.assertNotEqual(module.get("recognize"), "text")
        notice.assert_not_called()

    def test_form_save_module_click_count_defaults_to_one_and_accepts_custom_value(self):
        form = self._form(
            image="images/claim.png", region="10,20,300,400", recognize="模板图片",
        )
        form.click_count_var.get.return_value = "4"
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["click_count"], 4)
        notice.assert_not_called()

    def test_form_drag_offset_converts_two_points_to_four_directions(self):
        form = self._form(recognize="识别文字")
        form._apply_ocr_offset(500, 400, 465, 428)
        form.ocr_offset_left_var.set.assert_called_once_with("35")
        form.ocr_offset_right_var.set.assert_called_once_with("0")
        form.ocr_offset_up_var.set.assert_called_once_with("0")
        form.ocr_offset_down_var.set.assert_called_once_with("28")

    def test_toggle_sections_text_mode_shows_ocr_rows_and_forces_click_match(self):
        form = self._form(recognize="识别文字")
        form.pure = False
        for attr in (
            "row_name", "row_image", "row_region", "detect_section_heading",
            "row_recognize", "row_expected_text", "row_match_mode", "row_threshold",
            "row_wait_text_absent",
            "row_ignore_background", "row_interval", "row_start_delay", "row_fallback_module",
            "row_fallback_click", "row_blocking", "row_delay",
            "action_section_heading", "row_after", "row_hold", "row_button",
            "row_click_count",
            "row_ocr_offset",
            "row_click_point", "row_second_template", "row_second_timeout",
            "row_second_click_target", "row_second_click_region",
            "segment_section_heading", "row_run_code_after_action", "segment_frame",
            "timeout_section_heading", "row_run_code_on_timeout",
            "row_not_found_timeout", "timeout_segment_frame",
        ):
            setattr(form, attr, Mock())
        form.after_action_var.get.return_value = "二次识别后点击"
        form.after_action_var.set = Mock()
        form.second_click_target_var = Mock()
        form.second_click_target_var.get.return_value = "第二次识别位置"
        form.run_code_after_action_var.get.return_value = False
        form.run_code_on_timeout_var.get.return_value = False
        form._set_row = Mock()
        form._resize_for_content = Mock()
        form._toggle_sections = TemplateRegionFormDialog._toggle_sections.__get__(
            form, TemplateRegionFormDialog,
        )

        form._toggle_sections()

        # 识别文字方式：二次识别被强制回落到"点击识别区域"。
        form.after_action_var.set.assert_called_once_with("点击识别区域")
        visible = {
            item.args[0] for item in form._set_row.call_args_list if item.args[1]
        }
        hidden = {
            item.args[0] for item in form._set_row.call_args_list if not item.args[1]
        }
        for row in ("row_image", "row_threshold", "row_ignore_background",
                    "row_second_template", "row_second_timeout"):
            self.assertIn(getattr(form, row), hidden)
        for row in (
            "row_expected_text", "row_match_mode", "row_wait_text_absent",
            "row_region", "row_recognize", "row_ocr_offset",
        ):
            self.assertIn(getattr(form, row), visible)

    def test_toggle_sections_template_mode_shows_wait_until_absent(self):
        form = self._form(recognize="模板图片")
        form.pure = False
        for attr in (
            "row_name", "row_image", "row_region", "detect_section_heading",
            "row_recognize", "row_expected_text", "row_match_mode", "row_threshold",
            "row_wait_text_absent", "row_ignore_background", "row_interval", "row_start_delay",
            "row_fallback_module", "row_fallback_click",
            "row_blocking", "row_delay", "action_section_heading", "row_after",
            "row_hold", "row_button", "row_click_count", "row_ocr_offset", "row_click_point",
            "row_second_template", "row_second_timeout", "row_second_click_target",
            "row_second_click_region", "segment_section_heading",
            "row_run_code_after_action", "segment_frame", "timeout_section_heading",
            "row_run_code_on_timeout", "row_not_found_timeout", "timeout_segment_frame",
        ):
            setattr(form, attr, Mock())
        form.after_action_var.get.return_value = "点击识别区域"
        form.second_click_target_var = Mock()
        form.second_click_target_var.get.return_value = "第二次识别位置"
        form.run_code_after_action_var.get.return_value = False
        form.run_code_on_timeout_var.get.return_value = False
        form._set_row = Mock()
        form._resize_for_content = Mock()
        form._toggle_sections = TemplateRegionFormDialog._toggle_sections.__get__(
            form, TemplateRegionFormDialog,
        )

        form._toggle_sections()

        visible = {
            item.args[0] for item in form._set_row.call_args_list if item.args[1]
        }
        self.assertIn(form.row_wait_text_absent, visible)

    def test_manager_open_add_persists_new_entry(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {}
        dialog.current = "switch"
        dialog.trees = {"switch": Mock()}
        form = Mock()
        obj = self._object()
        form.show.return_value = ("", "images/g.png", obj)
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("macroflow.ui.dialogs.update_module_object", return_value={"images/g.png": obj}) as save, \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            dialog._open_add()
        form_class.assert_called_once_with(dialog, "", object_dict=None, category="switch")
        save.assert_called_once_with("images/g.png", obj, old_key="")
        self.assertEqual(dialog.objects, {"images/g.png": obj})
        dialog.trees["switch"].selection_set.assert_called_once_with("images/g.png")
        dialog.trees["switch"].see.assert_called_once_with("images/g.png")

    def test_manager_open_edit_switches_file_keeps_region(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {"images/a.png": self._object()}
        dialog.current = "switch"
        tree = Mock()
        tree.selection.return_value = ("images/a.png",)
        dialog.trees = {"switch": tree}
        original = dialog.objects["images/a.png"]
        form = Mock()
        obj = self._object()
        form.show.return_value = ("images/a.png", "images/b.png", obj)
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("macroflow.ui.dialogs.update_module_object", return_value={"images/b.png": obj}) as save, \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            dialog._open_edit()
        form_class.assert_called_once_with(
            dialog, "images/a.png", object_dict=original, category="switch",
        )
        save.assert_called_once_with("images/b.png", obj, old_key="images/a.png")
        self.assertEqual(dialog.objects, {"images/b.png": obj})
        tree.selection_set.assert_called_once_with("images/b.png")

    def test_manager_open_edit_without_selection_does_nothing(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {"images/a.png": self._object()}
        dialog.current = "switch"
        tree = Mock()
        tree.selection.return_value = ()
        dialog.trees = {"switch": tree}
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog") as form_class, \
             patch("macroflow.ui.dialogs.update_module_object") as save, \
             patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            dialog._open_edit()
        form_class.assert_not_called()
        self.assertEqual(dialog.objects, {"images/a.png": dialog.objects["images/a.png"]})
        save.assert_not_called()
        notice.assert_called_once()

    def test_manager_form_cancelled_keeps_objects(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        obj = self._object()
        dialog.objects = {"images/a.png": obj}
        dialog.current = "switch"
        dialog.trees = {"switch": Mock()}
        form = Mock()
        form.show.return_value = None
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog", return_value=form), \
             patch("macroflow.ui.dialogs.update_module_object") as save:
            dialog._open_form("images/a.png", obj)
        self.assertEqual(dialog.objects, {"images/a.png": obj})
        save.assert_not_called()

    def test_manager_form_edit_same_image_updates_object(self):
        # 图片不变只改对象属性，条目不换 key。
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {"images/a.png": self._object(region=(0, 0, 0, 0))}
        dialog.current = "switch"
        tree = Mock()
        tree.selection_set.side_effect = tk.TclError
        dialog.trees = {"switch": tree}
        form = Mock()
        obj = self._object(region=(10, 20, 300, 400), after_action="continue")
        form.show.return_value = ("images/a.png", "images/a.png", obj)
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog", return_value=form), \
             patch("macroflow.ui.dialogs.update_module_object", return_value={"images/a.png": obj}) as save, \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            dialog._open_form("images/a.png", dialog.objects["images/a.png"])
        self.assertEqual(dialog.objects, {"images/a.png": obj})
        save.assert_called_once_with("images/a.png", obj, old_key="images/a.png")
        tree.selection_set.assert_called_once_with("images/a.png")

    def test_manager_special_module_cannot_be_edited(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.current = "all"
        dialog.objects = {
            "重新执行工作流": {
                "category": "special", "name": "重新执行工作流", "pure_action": True,
            },
        }
        tree = Mock()
        tree.selection.return_value = ("重新执行工作流",)
        dialog.trees = {"all": tree}
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog") as form_class, \
             patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            dialog._open_edit()
        form_class.assert_not_called()
        notice.assert_called_once()

    def test_manager_remove_selected_persists(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {"images/g.png": self._object()}
        dialog.current = "switch"
        tree = Mock()
        tree.selection.return_value = ("images/g.png",)
        dialog.trees = {"switch": tree}
        dialog._undo_stack = []
        dialog.undo_button = Mock()
        with patch("macroflow.ui.dialogs.save_module_objects") as save, \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            dialog._remove_selected()
        self.assertEqual(dialog.objects, {})
        save.assert_called_once_with({})
        # 移除的条目连同完整对象快照进撤销栈，供"撤销移除"恢复。
        self.assertEqual(dialog._undo_stack, [("images/g.png", self._object())])
        dialog.undo_button.configure.assert_called_with(state="normal")

    def test_manager_undo_remove_restores_entry_and_persists(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        obj = self._object()
        dialog.objects = {}
        dialog.current = "switch"
        tree = Mock()
        dialog.trees = {"switch": tree}
        dialog._undo_stack = [("images/g.png", obj)]
        dialog.undo_button = Mock()
        with patch("macroflow.ui.dialogs.save_module_objects") as save, \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            dialog._undo_remove()
        self.assertEqual(dialog.objects, {"images/g.png": obj})
        save.assert_called_once_with({"images/g.png": obj})
        self.assertEqual(dialog._undo_stack, [])
        tree.selection_set.assert_called_once_with("images/g.png")
        tree.see.assert_called_once_with("images/g.png")
        dialog.undo_button.configure.assert_called_with(state="disabled")

    def test_manager_undo_remove_empty_stack_does_nothing(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {}
        dialog.current = "switch"
        dialog.trees = {"switch": Mock()}
        dialog._undo_stack = []
        with patch("macroflow.ui.dialogs.save_module_objects") as save:
            dialog._undo_remove()
        save.assert_not_called()

    def test_manager_remove_without_selection_does_nothing(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        obj = self._object()
        dialog.objects = {"images/g.png": obj}
        dialog.current = "switch"
        tree = Mock()
        tree.selection.return_value = ()
        dialog.trees = {"switch": tree}
        with patch("macroflow.ui.dialogs.save_module_objects") as save:
            dialog._remove_selected()
        self.assertEqual(dialog.objects, {"images/g.png": obj})
        save.assert_not_called()

    def test_manager_copies_script_global_to_independent_workflow_global(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        original = self._object(
            category="script_global", name="同名全局", template="images/shared.png",
            on_success_actions=[{"type": "delay", "ms": 10}],
        )
        dialog.objects = {"module:source": original}
        with patch("macroflow.ui.dialogs.uuid.uuid4") as uuid4, \
             patch("macroflow.ui.dialogs.save_module_objects") as save, \
             patch("macroflow.ui.dialogs.show_floating_notice"), \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            uuid4.return_value.hex = "copied"
            dialog._change_global_module_category(
                "module:source", "workflow_global", copy_object=True,
            )
        self.assertEqual(dialog.objects["module:source"]["category"], "script_global")
        copied = dialog.objects["module:copied"]
        self.assertEqual(copied["category"], "workflow_global")
        self.assertEqual(copied["name"], "同名全局")
        self.assertEqual(copied["template"], "images/shared.png")
        self.assertIsNot(copied["on_success_actions"], original["on_success_actions"])
        save.assert_called_once_with(dialog.objects)

    def test_manager_copy_backfills_name_for_nameless_source(self):
        # 旧对象没有 name 时（图片路径键年代），复制成 module:<uuid> 键后
        # 显示兜底会退化成 uuid——复制时按模板文件名补名。
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        original = self._object(category="workflow_global", template="images/shared.png")
        dialog.objects = {"images/shared.png": original}
        with patch("macroflow.ui.dialogs.uuid.uuid4") as uuid4, \
             patch("macroflow.ui.dialogs.save_module_objects") as save, \
             patch("macroflow.ui.dialogs.show_floating_notice"), \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            uuid4.return_value.hex = "copied"
            dialog._change_global_module_category(
                "images/shared.png", "script_global", copy_object=True,
            )
        copied = dialog.objects["module:copied"]
        self.assertEqual(copied["category"], "script_global")
        self.assertEqual(copied["name"], "shared")
        save.assert_called_once_with(dialog.objects)

    def test_manager_moves_workflow_global_to_script_global_with_same_id(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {
            "module:source": self._object(category="workflow_global", name="全局"),
        }
        with patch("macroflow.ui.dialogs.save_module_objects") as save, \
             patch("macroflow.ui.dialogs.show_floating_notice"), \
             patch.object(TemplateRegionManagerDialog, "_reload_trees"):
            dialog._change_global_module_category(
                "module:source", "script_global", copy_object=False,
            )
        self.assertEqual(list(dialog.objects), ["module:source"])
        self.assertEqual(dialog.objects["module:source"]["category"], "script_global")
        save.assert_called_once_with(dialog.objects)

    def test_manager_global_context_menu_offers_reverse_category_actions(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {
            "module:source": self._object(category="script_global", name="全局"),
        }
        tree = Mock()
        tree.identify_row.return_value = "module:source"
        event = Mock(widget=tree, y=30, x_root=100, y_root=120)
        with patch("macroflow.ui.dialogs.tk.Menu") as menu_class:
            dialog._show_module_context_menu(event)
        labels = [call.kwargs["label"] for call in menu_class.return_value.add_command.call_args_list]
        self.assertEqual(labels, ["改成工作流全局", "复制成工作流全局"])
        tree.selection_set.assert_called_once_with("module:source")
        menu_class.return_value.tk_popup.assert_called_once_with(100, 120)

    def test_prepend_global_module_to_selected_scripts(self):
        with tempfile.TemporaryDirectory() as folder:
            normal_path = Path(folder) / "normal.json"
            global_path = Path(folder) / "global.json"
            original = {"type": "delay", "ms": 10}
            save_script(MacroScript(name="normal", actions=[original]), normal_path)
            save_script(MacroScript(name="global", actions=[], is_global=True), global_path)

            added, skipped, errors = prepend_module_to_scripts(
                "images/g.png", "global", [normal_path, global_path],
            )

            self.assertEqual(added, 1)
            self.assertEqual(skipped, [global_path])
            self.assertEqual(errors, [])
            script = load_script(normal_path)
            self.assertEqual(script.actions[0]["type"], "global_detect")
            self.assertEqual(script.actions[0]["template"], "images/g.png")
            self.assertEqual(script.actions[0]["jump_row"], 2)
            self.assertEqual(
                script.actions[0]["jump_action_id"],
                script.actions[1][ACTION_ID_KEY],
            )

    def test_prepend_disabled_module_is_rejected_without_changing_script(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "normal.json"
            save_script(MacroScript(name="normal", actions=[]), path)
            with patch(
                "macroflow.ui.dialogs.registered_module_object",
                return_value={"enabled": False, "category": "switch"},
            ):
                added, skipped, errors = prepend_module_to_scripts(
                    "module:disabled", "switch", [path],
                )
            self.assertEqual((added, skipped), (0, []))
            self.assertEqual(errors, [(path, "模块已禁用，不能插入到脚本")])
            self.assertEqual(load_script(path).actions, [])

    def test_prepend_switch_module_only_changes_checked_script(self):
        with tempfile.TemporaryDirectory() as folder:
            selected_path = Path(folder) / "selected.json"
            other_path = Path(folder) / "other.json"
            save_script(MacroScript(name="selected", actions=[]), selected_path)
            save_script(MacroScript(name="other", actions=[]), other_path)

            added, skipped, errors = prepend_module_to_scripts(
                "images/s.png", "switch", [selected_path],
            )

            self.assertEqual((added, skipped, errors), (1, [], []))
            self.assertEqual(load_script(selected_path).actions[0], {
                "type": "image_match", "template": "images/s.png",
                "module_key": "images/s.png",
                "module_ref": True, "module_category": "switch",
                "region_mode": "template", "region": [], "delay_ms": 0,
                "on_found": "continue", "on_timeout": "continue",
                ACTION_ID_KEY: load_script(selected_path).actions[0][ACTION_ID_KEY],
            })
            self.assertEqual(load_script(other_path).actions, [])

    def test_remove_module_from_scripts_removes_all_reference_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            used_path = Path(folder) / "used.json"
            other_path = Path(folder) / "other.json"
            unrelated_path = Path(folder) / "unrelated.json"
            kept = {"type": "delay", "ms": 10, ACTION_ID_KEY: "keep-id"}
            reference = module_action_for_key("images/g.png", "switch")
            another_reference = module_action_for_key("images/g.png", "switch")
            save_script(MacroScript(
                name="used", actions=[kept, reference, another_reference],
            ), used_path)
            save_script(MacroScript(name="other", actions=[kept]), other_path)
            save_script(MacroScript(
                name="unrelated",
                actions=[module_action_for_key("images/s.png", "switch")],
            ), unrelated_path)

            removed, untouched, errors = remove_module_from_scripts(
                "images/g.png", [used_path, other_path, unrelated_path],
            )

            self.assertEqual(removed, 1)
            self.assertEqual(set(untouched), {other_path, unrelated_path})
            self.assertEqual(errors, [])
            script = load_script(used_path)
            self.assertEqual(script.actions, [kept])

    def test_remove_module_rebuilds_action_ids_of_remaining_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "script.json"
            kept = {"type": "delay", "ms": 10, ACTION_ID_KEY: "keep"}
            save_script(MacroScript(
                name="script",
                actions=[kept, module_action_for_key("images/g.png", "switch")],
            ), path)

            removed, untouched, errors = remove_module_from_scripts("images/g.png", [path])

            self.assertEqual((removed, untouched, errors), (1, [], []))
            remaining = load_script(path).actions
            self.assertEqual(len(remaining), 1)
            self.assertTrue(str(remaining[0].get(ACTION_ID_KEY, "")).strip())

    def test_remove_module_from_scripts_skips_untouched_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            clean_path = Path(folder) / "clean.json"
            broken_path = Path(folder) / "broken.json"
            save_script(MacroScript(name="clean", actions=[]), clean_path)
            broken_path.write_text("{not json", encoding="utf-8")

            removed, untouched, errors = remove_module_from_scripts(
                "images/g.png", [clean_path, broken_path],
            )

            self.assertEqual(removed, 0)
            self.assertEqual(untouched, [clean_path])
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0][0], broken_path)

    def test_batch_remove_dialog_counts_usage_and_prechecks_used(self):
        with tempfile.TemporaryDirectory() as folder:
            used_path = Path(folder) / "used.json"
            clean_path = Path(folder) / "clean.json"
            save_script(MacroScript(
                name="used", actions=[module_action_for_key("images/g.png", "switch")],
            ), used_path)
            save_script(MacroScript(name="clean", actions=[]), clean_path)

            dialog = BatchModuleScriptDialog.__new__(BatchModuleScriptDialog)
            dialog.script_paths = [used_path, clean_path]
            counts = dialog._count_module_usage("images/g.png")

            self.assertEqual(counts, {0: 1, 1: 0})

    def test_batch_remove_dialog_path_display_suffixes_usage(self):
        dialog = BatchModuleScriptDialog.__new__(BatchModuleScriptDialog)
        dialog.script_paths = [Path("scripts/关卡/目标.json"), Path("scripts/关卡/其他.json")]
        dialog.mode = "remove"
        dialog.usage_counts = {0: 2, 1: 0}
        used_text = f"scripts{os.sep}关卡{os.sep}目标.json（2 行）"
        clean_text = f"scripts{os.sep}关卡{os.sep}其他.json（未使用）"
        self.assertEqual(dialog._path_display(0), used_text)
        self.assertEqual(dialog._path_display(1), clean_text)
        dialog.mode = "add"
        self.assertEqual(dialog._path_display(0), used_text.split("（")[0])

    def test_batch_script_category_uses_saved_category_and_global_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            switch_path = Path(folder) / "switch.json"
            global_path = Path(folder) / "global.json"
            switch_script = MacroScript(name="switch")
            switch_script.settings["category"] = "switch"
            save_script(switch_script, switch_path)
            save_script(MacroScript(name="global", is_global=True), global_path)
            self.assertEqual(script_category_for_path(switch_path, {}), "switch")
            self.assertEqual(script_category_for_path(global_path, {}), "level")

    def test_batch_category_filter_preserves_cross_category_checks(self):
        dialog = BatchModuleScriptDialog.__new__(BatchModuleScriptDialog)
        dialog.script_paths = [Path("a.json"), Path("b.json"), Path("c.json")]
        dialog.script_categories = ["switch", "level_pack", "switch"]
        dialog.current_filter = "switch"
        dialog.checked = {1}
        dialog.tree = Mock()
        dialog.tree.exists.return_value = False

        dialog._select_all()

        self.assertEqual(dialog.checked, {0, 1, 2})
        dialog.current_filter = "level_pack"
        dialog._clear_all()
        self.assertEqual(dialog.checked, {0, 2})

    def test_batch_set_filter_reloads_without_clearing_checks(self):
        dialog = BatchModuleScriptDialog.__new__(BatchModuleScriptDialog)
        dialog.current_filter = "all"
        dialog.checked = {2}
        dialog.filter_buttons = {
            "all": Mock(), "switch": Mock(), "level_pack": Mock(),
        }
        with patch.object(BatchModuleScriptDialog, "_reload_visible_scripts") as reload:
            dialog._set_filter("level_pack")
        self.assertEqual(dialog.current_filter, "level_pack")
        self.assertEqual(dialog.checked, {2})
        reload.assert_called_once()

    def test_module_picker_switch_choose_returns_module_ref_action(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.category_keys = {"switch": ["images/s.png"], "special": []}
        picker.listboxes = {"switch": Mock(), "special": Mock()}
        picker.listboxes["switch"].curselection.return_value = (0,)
        picker.objects = {"images/s.png": self._object()}
        picker.destroy = Mock()
        picker._choose_category("switch")
        self.assertEqual(picker.result, {
            "type": "image_match", "template": "images/s.png", "module_ref": True,
            "module_key": "images/s.png",
            "module_category": "switch", "region_mode": "template",
            "region": [10, 20, 300, 400], "delay_ms": 0,
            "on_found": "continue", "on_timeout": "continue",
        })
        picker.destroy.assert_called_once()

    def test_module_picker_binds_selected_image_to_its_region(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.category_keys = {"switch": ["module:first"], "special": []}
        picker.listboxes = {"switch": Mock(), "special": Mock()}
        picker.listboxes["switch"].curselection.return_value = (0,)
        picker.objects = {
            "module:first": self._object(
                region=(11, 22, 333, 444),
                template="images/shared.png",
            ),
        }
        picker.destroy = Mock()

        picker._choose_category("switch")

        self.assertEqual(picker.result["module_key"], "module:first")
        self.assertEqual(picker.result["template"], "images/shared.png")
        self.assertEqual(picker.result["region"], [11, 22, 333, 444])
        self.assertEqual(picker.result["region_mode"], "template")
        picker.destroy.assert_called_once()

    def test_module_picker_selection_only_returns_bound_module_payload(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.selection_only = True
        picker.objects = {
            "module:first": self._object(
                region=(11, 22, 333, 444),
                template="images/shared.png",
            ),
        }

        result = picker._action_for_key("module:first", "switch")

        self.assertEqual(result["module_key"], "module:first")
        self.assertEqual(result["template"], "images/shared.png")
        self.assertEqual(result["region"], [11, 22, 333, 444])
        self.assertTrue(result["module_ref"])

    def test_same_image_modules_keep_independent_bound_regions_when_selected(self):
        first = module_action_for_key(
            "module:first", "switch", {
                "template": "images/shared.png", "region": [1, 2, 30, 40],
            },
        )
        second = module_action_for_key(
            "module:second", "switch", {
                "template": "images/shared.png", "region": [5, 6, 70, 80],
            },
        )

        self.assertEqual(first["module_key"], "module:first")
        self.assertEqual(first["region"], [1, 2, 30, 40])
        self.assertEqual(second["module_key"], "module:second")
        self.assertEqual(second["region"], [5, 6, 70, 80])

    def test_module_picker_number_action_defaults_failure_to_continue(self):
        action = module_action_for_key("module:number", "switch", {
            "recognize": "number", "template": "", "name": "剩余次数",
        })
        self.assertEqual(action["module_key"], "module:number")
        self.assertEqual(action["on_found"], "jump")
        self.assertEqual(action["on_timeout"], "continue")
        self.assertNotIn("expected_number", action)

    def test_module_picker_rejects_number_where_row_comparison_is_unavailable(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.allow_number = False
        picker.objects = {"module:number": {"recognize": "number", "category": "switch"}}
        picker.destroy = Mock()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            picker._choose_key("module:number", "switch")
        notice.assert_called_once()
        picker.destroy.assert_not_called()
        self.assertIsNone(getattr(picker, "result", None))

    def test_module_picker_hides_disabled_modules(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.objects = {
            "module:enabled": self._object(name="可用", enabled=True),
            "module:disabled": self._object(name="停用", enabled=False),
        }
        listbox = Mock()
        empty_label = Mock()
        picker.category_keys = {"switch": []}
        picker.listboxes = {"switch": listbox}
        picker.empty_labels = {"switch": empty_label}

        picker._refresh_category("switch")

        self.assertEqual(picker.category_keys["switch"], ["module:enabled"])
        listbox.insert.assert_called_once_with("end", "可用")
        empty_label.pack_forget.assert_called_once_with()

    def test_module_picker_script_global_choose_returns_global_detect_action(self):
        # 脚本全局模块插入 global_detect 动作，并保留明确类别。
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.category_keys = {"switch": [], "script_global": ["images/g.png"]}
        picker.listboxes = {"switch": Mock(), "script_global": Mock()}
        picker.listboxes["script_global"].curselection.return_value = (0,)
        picker.objects = {"images/g.png": self._object(category="script_global")}
        picker.destroy = Mock()
        picker._choose_category("script_global")
        self.assertEqual(picker.result, {
            "type": "global_detect", "template": "images/g.png", "module_ref": True,
            "module_key": "images/g.png",
            "module_category": "script_global", "region_mode": "template",
            "region": [10, 20, 300, 400], "delay_ms": 0,
        })
        picker.destroy.assert_called_once()

    def test_module_picker_global_multi_select_returns_all_selected_actions(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.multi_select = True
        picker.category_keys = {
            "workflow_global": ["images/g1.png", "images/g2.png", "images/g3.png"],
        }
        picker.listboxes = {"workflow_global": Mock()}
        picker.listboxes["workflow_global"].curselection.return_value = (0, 2)
        picker.destroy = Mock()

        picker._choose_category("workflow_global")

        self.assertEqual(
            [action["template"] for action in picker.result],
            ["images/g1.png", "images/g3.png"],
        )
        self.assertTrue(all(action["module_ref"] for action in picker.result))
        picker.destroy.assert_called_once()

    def test_module_picker_ctrl_a_selects_every_row(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        listbox = Mock()
        picker.listboxes = {"workflow_global": listbox}

        result = picker._select_all_category("workflow_global")

        listbox.selection_set.assert_called_once_with(0, "end")
        self.assertEqual(result, "break")

    def test_module_picker_special_pure_action_returns_restart_workflow(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.objects = {
            "重新执行工作流": {
                "category": "special", "name": "重新执行工作流", "pure_action": True,
            },
        }
        picker.destroy = Mock()
        picker._choose_key("重新执行工作流", "special")
        self.assertEqual(picker.result, {"type": "restart_workflow"})
        picker.destroy.assert_called_once()

    def test_module_picker_special_can_end_current_script(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.destroy = Mock()
        picker._choose_key("结束当前最里层脚本，继续执行", "special")
        self.assertEqual(picker.result, {"type": "end_current_script"})
        picker.destroy.assert_called_once()

    def test_module_picker_choose_without_selection_shows_notice(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.category_keys = {"switch": [], "special": []}
        picker.listboxes = {"switch": Mock(), "special": Mock()}
        picker.listboxes["switch"].curselection.return_value = ()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            picker._choose_category("switch")
        notice.assert_called_once()
        self.assertIsNone(getattr(picker, "result", None))

    def test_module_picker_new_object_stores_repo_and_returns_action(self):
        picker = ModulePickerDialog.__new__(ModulePickerDialog)
        picker.segment_depth = 0
        picker.destroy = Mock()
        form = Mock()
        obj = self._object()
        form.show.return_value = ("", "images/n.png", obj)
        picker.objects = {"images/n.png": obj}
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("macroflow.ui.dialogs.update_module_object") as save, \
             patch.object(ModulePickerDialog, "_refresh_lists") as refresh:
            picker._new_object("switch")
        form_class.assert_called_once_with(picker, category="switch", segment_depth=1)
        save.assert_called_once_with("images/n.png", obj, old_key="")
        refresh.assert_called_once()
        self.assertEqual(picker.result["template"], "images/n.png")
        self.assertEqual(picker.result["module_category"], "switch")
        picker.destroy.assert_called_once()

    def test_segment_add_module_ref_uses_nested_picker(self):
        form = self._form()
        form.segment_listbox = Mock()
        with patch("macroflow.ui.dialogs.ModulePickerDialog") as picker_class:
            picker_class.return_value.show.return_value = {
                "type": "image_match", "template": "images/m.png", "module_ref": True,
            }
            form._add_segment_module_ref()
        picker_class.assert_called_once_with(form, nested=True, segment_depth=1)
        self.assertEqual(len(form.segment), 1)
        self.assertEqual(form.segment[0]["module_ref"], True)
        self.assertTrue(form.segment[0]["action_id"])

    def test_segment_can_add_end_current_script_directly(self):
        form = self._form()
        form.segment_listbox = Mock()

        form._add_segment_end_current_script()

        self.assertEqual(form.segment[0]["type"], "end_current_script")
        self.assertTrue(form.segment[0]["action_id"])

    def test_segment_can_add_jump_to_current_script_last_action(self):
        form = self._form()
        form.segment_listbox = Mock()

        form._add_segment_jump_current_script_last()

        self.assertEqual(form.segment[0]["type"], "jump_current_script_last")
        self.assertTrue(form.segment[0]["action_id"])

    def test_edit_action_module_ref_opens_reference_result_dialog(self):
        action = {"type": "image_match", "template": "images/m.png", "module_ref": True,
                  "module_category": "switch", "action_id": "abc"}
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog") as form_class, \
             patch("macroflow.ui.dialogs.update_module_object") as save, \
             patch("macroflow.ui.dialogs.ModuleReferenceDelayDialog") as delay_dialog:
            delay_dialog.return_value.show.return_value = dict(
                action, delay_ms=500, after_delay_ms=800,
            )
            updated = edit_action(None, action)
        self.assertEqual(updated["delay_ms"], 500)
        self.assertEqual(updated["after_delay_ms"], 800)
        self.assertEqual(updated["action_id"], "abc")
        form_class.assert_not_called()
        save.assert_not_called()
        delay_dialog.assert_called_once_with(None, action, actions=None)

    def test_edit_action_global_module_ref_uses_same_delay_dialog(self):
        action = {"type": "global_detect", "template": "images/m.png", "module_ref": True,
                  "module_category": "global", "action_id": "abc"}
        with patch("macroflow.ui.dialogs.TemplateRegionFormDialog") as form_class, \
             patch("macroflow.ui.dialogs.ModuleReferenceDelayDialog") as delay_dialog:
            delay_dialog.return_value.show.return_value = None
            updated = edit_action(None, action)
        self.assertIsNone(updated)
        form_class.assert_not_called()
        delay_dialog.assert_called_once_with(None, action, actions=None)

    def test_module_reference_dialog_saves_timing_and_result_branches(self):
        dialog = ModuleReferenceDelayDialog.__new__(ModuleReferenceDelayDialog)
        dialog.action = {
            "type": "image_match", "template": "images/m.png",
            "module_ref": True, "threshold": 0.91, "action_id": "stable",
        }
        dialog.delay = Mock()
        dialog.delay.get.return_value = "600"
        dialog.after_delay = Mock()
        dialog.after_delay.get.return_value = "900"
        dialog.result_routes_enabled = True
        dialog.on_success = Mock()
        dialog.on_success.get.return_value = "jump"
        dialog.on_failure = Mock()
        dialog.on_failure.get.return_value = "end_current_script"
        dialog.success_target = Mock()
        dialog.success_target.get.return_value = "第 3 行"
        dialog.failure_target = Mock()
        dialog.failure_target.get.return_value = "第 2 行"
        dialog.jump_target_ids = {"第 3 行": "success-target", "第 2 行": "failure-target"}
        dialog.destroy = Mock()

        dialog.save()

        self.assertEqual(dialog.result["delay_ms"], 600)
        self.assertEqual(dialog.result["after_delay_ms"], 900)
        self.assertEqual(dialog.result["on_found"], "jump")
        self.assertEqual(dialog.result["found_jump_action_id"], "success-target")
        self.assertEqual(dialog.result["on_timeout"], "end_current_script")
        self.assertEqual(dialog.result["timeout_jump_action_id"], "failure-target")
        self.assertEqual(dialog.result["threshold"], 0.91)
        self.assertEqual(dialog.result["action_id"], "stable")
        dialog.destroy.assert_called_once()

    def test_number_module_reference_dialog_saves_comparison_and_existing_routes(self):
        dialog = ModuleReferenceDelayDialog.__new__(ModuleReferenceDelayDialog)
        dialog.action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:number", "action_id": "stable",
        }
        dialog.delay = Mock(**{"get.return_value": "0"})
        dialog.after_delay = Mock(**{"get.return_value": "0"})
        dialog.result_routes_enabled = True
        dialog.number_routes_enabled = True
        dialog.expected_number = Mock(**{"get.return_value": "007"})
        dialog.on_success = Mock(**{"get.return_value": "jump"})
        dialog.on_failure = Mock(**{"get.return_value": "jump"})
        dialog.success_target = Mock(**{"get.return_value": "等于"})
        dialog.failure_target = Mock(**{"get.return_value": "不等于"})
        dialog.jump_target_ids = {"等于": "equal-target", "不等于": "other-target"}
        dialog.destroy = Mock()

        dialog.save()

        self.assertEqual(dialog.result["expected_number"], 7)
        self.assertEqual(dialog.result["found_jump_action_id"], "equal-target")
        self.assertEqual(dialog.result["timeout_jump_action_id"], "other-target")
        dialog.destroy.assert_called_once()

    def test_number_module_reference_dialog_rejects_invalid_comparison(self):
        dialog = ModuleReferenceDelayDialog.__new__(ModuleReferenceDelayDialog)
        dialog.action = {"type": "image_match", "module_ref": True}
        dialog.delay = Mock(**{"get.return_value": "0"})
        dialog.after_delay = Mock(**{"get.return_value": "0"})
        dialog.result_routes_enabled = True
        dialog.number_routes_enabled = True
        dialog.expected_number = Mock(**{"get.return_value": "abc"})
        dialog.destroy = Mock()
        with patch("macroflow.ui.dialogs.show_floating_notice") as notice:
            dialog.save()
        self.assertIn("比较数字无效", notice.call_args.args[1])
        dialog.destroy.assert_not_called()

    def test_module_reference_dialog_can_replace_module_and_keep_row_settings(self):
        dialog = ModuleReferenceDelayDialog.__new__(ModuleReferenceDelayDialog)
        dialog.action = {
            "type": "global_detect", "template": "images/old.png",
            "module_ref": True, "module_category": "global",
            "delay_ms": 600, "after_delay_ms": 900,
            "jump_row": 4, "jump_action_id": "target-row",
            "action_id": "stable-action",
        }
        dialog.module_name = Mock()
        replacement = {
            "type": "global_detect", "template": "images/new.png",
            "module_ref": True, "module_category": "script_global",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        with patch("macroflow.ui.dialogs.ModulePickerDialog") as picker_class, \
             patch("macroflow.ui.dialogs.registered_module_object", return_value={"name": "新模块"}):
            picker_class.return_value.show.return_value = replacement
            dialog.replace_reference()

        picker_class.assert_called_once_with(dialog, categories=("script_global",))
        self.assertEqual(dialog.action["template"], "images/new.png")
        self.assertEqual(dialog.action["delay_ms"], 600)
        self.assertEqual(dialog.action["after_delay_ms"], 900)
        self.assertEqual(dialog.action["jump_row"], 4)
        self.assertEqual(dialog.action["jump_action_id"], "target-row")
        self.assertEqual(dialog.action["action_id"], "stable-action")
        dialog.module_name.set.assert_called_once_with("新模块")

    def test_edit_action_restart_workflow_opens_target_dialog(self):
        dialog = Mock()
        dialog.show.return_value = {
            "type": "restart_workflow", "restart_workflow_target_row": 5,
        }
        with patch("macroflow.ui.dialogs.RestartWorkflowTargetDialog", return_value=dialog) as dialog_class:
            updated = edit_action(None, {"type": "restart_workflow"})
        dialog_class.assert_called_once()
        self.assertEqual(updated["restart_workflow_target_row"], 5)
        self.assertEqual(updated["type"], "restart_workflow")

    def test_edit_action_restart_workflow_cancel_keeps_original(self):
        dialog = Mock()
        dialog.show.return_value = None
        with patch("macroflow.ui.dialogs.RestartWorkflowTargetDialog", return_value=dialog):
            updated = edit_action(None, {"type": "restart_workflow"})
        self.assertIsNone(updated)

    def test_segment_row_label_shows_restart_target_row(self):
        self.assertEqual(
            segment_row_label({"type": "restart_workflow"}),
            "重新执行工作流（默认跳转行）",
        )
        self.assertEqual(
            segment_row_label({"type": "restart_workflow", "restart_workflow_target_row": 4}),
            "重新执行工作流（跳转第 4 行）",
        )

    def _fit_dialog(self, reqw, reqh, screen_w, screen_h, parent=None):
        """构造一个用于 _fit_window_to_content 的 Mock 对话框。"""
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.update_idletasks = Mock()
        dialog.winfo_reqwidth = Mock(return_value=reqw)
        dialog.winfo_reqheight = Mock(return_value=reqh)
        dialog.winfo_screenwidth = Mock(return_value=screen_w)
        dialog.winfo_screenheight = Mock(return_value=screen_h)
        dialog.geometry = Mock()
        dialog.resizable = Mock()
        if parent is None:
            parent = Mock()
            parent.winfo_rootx.return_value = 100
            parent.winfo_rooty.return_value = 100
            parent.winfo_width.return_value = 1700
            parent.winfo_height.return_value = 920
        return dialog, parent

    def test_manager_fit_sizes_window_to_content_request(self):
        # 高 DPI（打包版）下内容需求高度超过固定 470，窗口必须按需求尺寸
        # 自适应，否则底部按钮行被挤出窗口（按钮完全看不见）。
        dialog, parent = self._fit_dialog(700, 620, 1920, 1080)
        dialog._fit_window_to_content(parent)
        size_call, pos_call = dialog.geometry.call_args_list
        self.assertEqual(size_call.args[0], "1000x620")
        # 居中：x = 100 + (1700 - 1000)//2 = 450；y = 100 + (920 - 620)//2 = 250
        self.assertEqual(pos_call.args[0], "+450+250")
        dialog.resizable.assert_called_once_with(True, True)

    def test_manager_fit_enforces_minimum_size(self):
        dialog, parent = self._fit_dialog(620, 120, 1920, 1080)
        dialog._fit_window_to_content(parent)
        size_call = dialog.geometry.call_args_list[0]
        self.assertEqual(size_call.args[0], "1000x500")

    def test_manager_fit_clamps_height_to_screen(self):
        # 屏幕放不下时不越过屏幕底部；可拉伸兜底保证按钮行可达。
        dialog, parent = self._fit_dialog(640, 900, 640, 600)
        dialog._fit_window_to_content(parent)
        size_call = dialog.geometry.call_args_list[0]
        self.assertEqual(size_call.args[0], "1000x520")  # 600 - 80（宽度取最小 1000）
        dialog.resizable.assert_called_once_with(True, True)

    def test_manager_fit_clamps_position_inside_screen(self):
        # 父窗口位于屏幕右下角时，居中位置被限制在屏幕内。
        parent = Mock()
        parent.winfo_rootx.return_value = 1800
        parent.winfo_rooty.return_value = 1000
        parent.winfo_width.return_value = 800
        parent.winfo_height.return_value = 600
        dialog, _ = self._fit_dialog(700, 620, 1920, 1080)
        dialog._fit_window_to_content(parent)
        pos_call = dialog.geometry.call_args_list[1]
        # x = 1800 + (800-1000)//2 = 1700 → 限制到 1920-1000 = 920
        # y = 1000 + (600-620)//2 = 990 → 限制到 1080-620 = 460
        self.assertEqual(pos_call.args[0], "+920+460")

    def test_fit_window_to_content_uses_custom_minimums(self):
        # 表单类（新增模板）用较小的最小尺寸；模块级函数按调用方参数执行。
        dialog, parent = self._fit_dialog(500, 180, 1920, 1080)
        fit_window_to_content(dialog, parent, minimum_width=560, minimum_height=220)
        size_call = dialog.geometry.call_args_list[0]
        self.assertEqual(size_call.args[0], "560x220")
        dialog.resizable.assert_called_once_with(True, True)

    def test_fit_window_to_content_can_align_module_form_to_screen_top(self):
        dialog, parent = self._fit_dialog(680, 900, 1920, 1080)
        fit_window_to_content(
            dialog, parent, minimum_width=680, minimum_height=600,
            align_top=True,
        )
        size_call, pos_call = dialog.geometry.call_args_list
        self.assertEqual(size_call.args[0], "680x900")
        self.assertEqual(pos_call.args[0], "+610+0")

    def test_deferred_module_form_is_shown_once_after_layout(self):
        dialog = TemplateRegionFormDialog.__new__(TemplateRegionFormDialog)
        dialog._deferred_show = True
        dialog.deiconify = Mock()
        dialog.update_idletasks = Mock()
        dialog.lift = Mock()
        dialog.focus_force = Mock()
        dialog.wait_window = Mock()
        dialog.result = None
        dialog.show()
        dialog.deiconify.assert_called_once()
        dialog.update_idletasks.assert_called_once()
        dialog.lift.assert_called_once()


class RawInputTests(unittest.TestCase):
    def test_listener_lifecycle(self):
        listener = RawMouseListener(lambda _x, _y: None)
        listener.start()
        self.assertTrue(listener.hwnd)
        listener.stop()
        self.assertFalse(listener.thread.is_alive())


class RecorderTests(unittest.TestCase):
    def test_discard_recent_ui_events(self):
        recorder = MacroRecorder()
        recorder.running = True
        recorder._last_action_time = time.perf_counter()
        recorder._append({"type": "mouse_button", "button": "left", "down": True})
        recorder._append({"type": "mouse_button", "button": "left", "down": False})
        removed = recorder.discard_recent(600)
        self.assertEqual(removed, 2)
        self.assertEqual(recorder.actions, [])

    def test_auto_recording_switches_mouse_capture_by_foreground_window(self):
        recorder = MacroRecorder()
        recorder.running = True
        recorder.mode = "auto"
        recorder.target_hwnd = 123
        recorder.interval_ms = 10
        recorder._last_action_time = time.perf_counter()
        with patch("macroflow.input.recorder.is_window_process_foreground", return_value=False):
            recorder._on_move(10, 20)
            self.assertEqual(recorder.actions[-1]["mode"], "absolute")
            recorder._on_raw_move(4, 5)
        with patch("macroflow.input.recorder.is_window_process_foreground", return_value=True):
            recorder._on_move(30, 40)
            recorder._on_raw_move(7, -3)
            recorder._flush_raw(force=True)
        self.assertEqual(recorder.actions[-1]["mode"], "relative")
        self.assertEqual((recorder.actions[-1]["dx"], recorder.actions[-1]["dy"]), (7, -3))

    def test_unbound_game_requires_stable_center_lock(self):
        recorder = MacroRecorder()
        recorder.running = True
        recorder.mode = "auto"
        recorder.target_hwnd = 123
        recorder.relative_requires_center_lock = True
        recorder.interval_ms = 100
        recorder._last_action_time = time.perf_counter()
        recorder._raw_last_flush = time.perf_counter() - 1
        with patch("macroflow.input.recorder.is_window_process_foreground", return_value=True), \
             patch("macroflow.input.recorder.is_cursor_near_window_center", return_value=True):
            recorder._on_raw_move(2, 1)
            recorder._on_raw_move(3, -1)
            self.assertEqual(recorder.current_mode(), "absolute")
            recorder._on_raw_move(4, 2)
            self.assertEqual(recorder.current_mode(), "relative")
        self.assertEqual(recorder.actions[-1]["mode"], "relative")

    def test_relative_capture_uses_game_frequency_cap(self):
        recorder = MacroRecorder()
        recorder.running = True
        recorder.mode = "relative"
        recorder.interval_ms = 100
        recorder._last_action_time = time.perf_counter()
        recorder._raw_last_flush = time.perf_counter() - 0.02
        recorder._on_raw_move(9, -2)
        self.assertEqual(recorder.actions[-1]["mode"], "relative")
        self.assertEqual((recorder.actions[-1]["dx"], recorder.actions[-1]["dy"]), (9, -2))


if __name__ == "__main__":
    unittest.main()
