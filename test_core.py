from __future__ import annotations

import ctypes
import inspect
import json
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import cv2
import numpy as np

from alerts import play_alert
from app import (
    BACKUP_INTERVAL_CHOICES, BACKUP_INTERVAL_MS, MacroFlowApp,
    action_short_text, action_summary, coordinate_scale_summary,
    disable_combobox_wheel_selection,
    recorded_action_description, floating_notice_xy, parse_click_point, parse_region,
    windows_startup_command, workflow_execution_progress, workflow_script_name,
    spawn_new_instance,
)
from dialogs import (
    KEY_HINT_CAPTURING, BatchModuleScriptDialog, ClickDialog, CloseAppDialog, GlobalDetectDialog,
    DurationVar, ImageActionDialog, JumpActionDialog, KeyActionDialog, ModalDialog,
    ModulePickerDialog, ModuleReferenceDelayDialog,
    MouseMoveDialog, OcrActionDialog, OpenAppDialog, RepeatClickDialog, RestartWorkflowTargetDialog,
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
from image_match import find_template, find_template_in_image
from ocr import (
    extract_ocr_integer, find_expected_match, format_ocr_observation, matches_expected,
    recognize_image_with_boxes,
)
from input_guard import (
    FocusInputGuard, KBDLLHOOKSTRUCT, KeyCapturer, LLKHF_INJECTED,
    LLMHF_INJECTED, RESERVED_HOTKEY_VKS, VK_ESCAPE, VK_F12, VK_F9,
    WM_KEYDOWN, WM_SYSKEYDOWN, should_block_keyboard, should_block_mouse,
)
from models import (
    ACTION_ID_KEY, DEFAULT_MOUSE_MOVE_INTERVAL_MS, DEFAULT_RECORDED_SCREEN,
    DEFAULT_WORKFLOW_REPEAT_INTERVAL_MS,
    NEXT_WORKFLOW_STEP_TARGET_ID, SCRIPT_START_TARGET_ID, MacroScript, Workflow,
    clone_actions_with_new_ids, ensure_action_ids,
    ensure_workflow_step_ids, is_global_script,
)
from player import (
    EndCurrentScriptRequest, JUMP_CURRENT_SCRIPT_LAST_RESULT, MacroPlayer,
    PlaybackStopped, scale_screen_point,
)
from rawinput import RawMouseListener
from recorder import MacroRecorder
from storage import (
    BASE_DIR, available_script_path, backup_script, display_path, load_app_settings,
    load_module_images_dir, load_module_objects, load_module_restart_default_row, load_script,
    load_template_regions, load_workflow,
    migrate_workflow_templates,
    module_image_inventory,
    registered_module_object, registered_template_region, resolve_path, save_app_settings, save_script,
    save_module_images_dir, save_module_objects, save_module_restart_default_row,
    save_template_regions, save_workflow,
)
from wininput import (
    MACROFLOW_INPUT_TAG, WindowInfo, activate_window, force_english_input, is_cursor_near_window_center,
    send_move_relative, set_input_dispatcher, show_window, show_window_no_activate,
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
                with patch("storage.BASE_DIR", base), \
                     patch("storage.TEMPLATE_REGIONS_PATH", registry):
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
            with patch("storage.BASE_DIR", base), \
                 patch("storage.MODULE_SETTINGS_PATH", settings_path):
                saved = save_module_images_dir(images)
                self.assertEqual(load_module_images_dir(), saved)
                rows = module_image_inventory(
                    saved,
                    {"images/部分/已采用.png": {"category": "switch"}},
                )
            self.assertEqual([row["status"] for row in rows], ["已采用（1 个）", "未采用"])
            self.assertEqual(rows[0]["module_key"], "images/部分/已采用.png")

    def test_module_restart_default_row_round_trips_and_survives_images_dir(self):
        # 默认跳转行存模块设置文件；保存识图目录不能覆盖该值。
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            settings_path = base / "module_settings.json"
            images = base / "images"
            with patch("storage.MODULE_SETTINGS_PATH", settings_path):
                self.assertEqual(load_module_restart_default_row(), 0)
                self.assertEqual(save_module_restart_default_row(3), 3)
                self.assertEqual(load_module_restart_default_row(), 3)
                save_module_images_dir(images)
                self.assertEqual(load_module_restart_default_row(), 3)
                self.assertEqual(load_module_images_dir(), images.resolve())

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
            with patch("storage.TEMPLATE_REGIONS_PATH", registry):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", registry):
                save_module_objects(objects)
                loaded = load_module_objects()["module:instant"]
        self.assertFalse(loaded["hold_enabled"])
        self.assertEqual(loaded["hold_ms"], 2500)

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
        with tempfile.TemporaryDirectory() as folder, patch("storage.SCRIPTS_DIR", Path(folder)):
            (Path(folder) / "已有脚本.json").write_text("old", encoding="utf-8")
            self.assertEqual(available_script_path("已有脚本").name, "已有脚本 (2).json")

    def test_available_script_path_uses_custom_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "目标.json").write_text("x", encoding="utf-8")
            path = available_script_path("目标", base)
            self.assertEqual(path, base / "目标 (2).json")

    def test_script_backup_overwrites_one_stable_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            scripts = base / "scripts"
            backups = base / "backups"
            scripts.mkdir()
            source = scripts / "领取.json"
            source.write_text('{"version": 1}', encoding="utf-8")
            with patch("storage.SCRIPTS_DIR", scripts), patch("storage.SCRIPT_BACKUPS_DIR", backups):
                first = backup_script(source)
                source.write_text('{"version": 2}', encoding="utf-8")
                second = backup_script(source)
            self.assertEqual(first, second)
            self.assertEqual(second.read_text(encoding="utf-8"), '{"version": 2}')
            self.assertEqual(list(backups.rglob("*.json")), [second])

    def test_source_startup_command_quotes_python_and_app(self):
        with patch("app.sys.frozen", False, create=True):
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
            with patch("storage.BASE_DIR", base), \
                 patch("storage.WORKFLOWS_DIR", base / "workflows"), \
                 patch("storage.SCRIPTS_DIR", base / "scripts"), \
                 patch("storage.IMAGES_DIR", base / "images"), \
                 patch("storage.SCRIPT_BACKUPS_DIR", base / "backups" / "scripts"):
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
            with patch("storage.BASE_DIR", base), \
                 patch("storage.WORKFLOWS_DIR", base / "workflows"), \
                 patch("storage.SCRIPTS_DIR", base / "scripts"), \
                 patch("storage.IMAGES_DIR", base / "images"), \
                 patch("storage.SCRIPT_BACKUPS_DIR", base / "backups" / "scripts"):
                migrated = migrate_workflow_templates()
            self.assertEqual(migrated, 1)
            self.assertTrue((base / "workflows" / "新模板.json").is_file())
            # 已有文件未被覆盖。
            self.assertEqual(
                json.loads((base / "workflows" / "日常.json").read_text(encoding="utf-8")),
                {"name": "日常", "steps": []},
            )
            with patch("storage.BASE_DIR", base), \
                 patch("storage.WORKFLOWS_DIR", base / "workflows"), \
                 patch("storage.SCRIPTS_DIR", base / "scripts"), \
                 patch("storage.IMAGES_DIR", base / "images"), \
                 patch("storage.SCRIPT_BACKUPS_DIR", base / "backups" / "scripts"):
                self.assertEqual(migrate_workflow_templates(), 0)  # 无旧文件
        with tempfile.TemporaryDirectory() as empty:
            with patch("storage.BASE_DIR", Path(empty)):
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
            with patch("storage.SETTINGS_PATH", path):
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
    def test_region_overlay_restores_main_without_activating_or_moving_it(self):
        main = Mock()
        main.winfo_id.return_value = 123
        dialog = Mock()
        with patch("dialogs.show_window_no_activate", return_value=True) as show:
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
        with patch("dialogs.is_current_process_window", side_effect=lambda hwnd: hwnd == 10):
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
        with patch("app.get_cursor_pos", side_effect=[(30, 40), (960, 540)]), \
             patch("app.get_virtual_screen_rect", return_value=DEFAULT_RECORDED_SCREEN):
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

        with patch("app.get_cursor_pos", return_value=(958, 415)):
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

        with patch("app.get_cursor_pos", return_value=(640, 360)):
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
        with patch("app.enum_windows", return_value=[current]), \
             patch("app.get_foreground_window_info", return_value=current):
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

        with patch("app.get_foreground_window_info", return_value=foreground), \
             patch("app.is_current_process_window", return_value=False):
            hwnd = app._bound_hwnd()

        self.assertEqual(hwnd, 222)
        self.assertEqual(app.bound_window.hwnd, 222)
        app.bind_label_var.set.assert_called_once_with("当前游戏")

    def test_execution_clock_resets_only_for_new_run(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.execution_started_at = 123.0
        app.mini_elapsed_var = Mock()

        with patch("app.time.perf_counter", return_value=456.0):
            app._reset_execution_clock_for_new_run(None)
        self.assertEqual(app.execution_started_at, 456.0)
        app.mini_elapsed_var.set.assert_called_once_with("00:00")

        app.mini_elapsed_var.reset_mock()
        with patch("app.time.perf_counter", return_value=999.0):
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


class ScriptRecordingSafetyTests(unittest.TestCase):
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
        with patch.dict("app.os.environ", inherited, clear=True), \
             patch("app.subprocess.Popen") as popen, \
             patch("app.sys.frozen", True, create=True):
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
        with patch("app.show_window", return_value=True) as show, \
             patch("app.activate_window", return_value=True) as activate:
            app._ensure_startup_visible()
        app.root.deiconify.assert_called_once()
        app.root.state.assert_called_once_with("normal")
        show.assert_called_once_with(123)
        activate.assert_called_once_with(123)


class ScriptEditingTests(unittest.TestCase):
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
        with patch("app.JumpActionDialog") as dialog_class:
            dialog_class.return_value.show.return_value = result
            app.add_jump()
        dialog_class.assert_called_once_with(app.root, actions=app.script.actions)
        app._insert_action.assert_called_once_with(result)

    def test_editing_action_preserves_stable_identity(self):
        original = {"type": "key", "action_id": "stable-target", "name": "A"}
        with patch("dialogs.KeyActionDialog") as dialog_class:
            dialog_class.return_value.show.return_value = {
                "type": "key_press", "name": "B", "vk": 66,
            }
            updated = edit_action(None, original)
        self.assertEqual(updated["action_id"], "stable-target")

    def test_editing_text_action_uses_text_dialog(self):
        original = {"type": "text", "action_id": "stable-text", "text": "旧文本", "delay_ms": 0}
        with patch("dialogs.TextActionDialog") as dialog_class:
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
        with patch("dialogs.RepeatClickDialog") as dialog_class:
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
        with patch("dialogs.OpenAppDialog") as dialog_class:
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
        with patch("dialogs.CloseAppDialog") as dialog_class:
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
        with patch("dialogs.JumpActionDialog") as dialog_class:
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
        app._activate_global_detect_from_config.assert_called_once_with(
            {"template": "images/g.png", "hold_ms": 500},
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
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.standalone_jump_pending = False
        app.standalone_jump_done = threading.Event()
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
        with patch("app.GlobalDetectDialog") as dialog_class:
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
        with patch("app.GlobalDetectDialog") as dialog_class:
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
        with patch("app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("app.load_script", return_value=inserted):
            app.insert_script_reference()
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
        with patch("app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("app.load_script", return_value=inserted):
            app.insert_script_reference()
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
        with patch("app.filedialog.askopenfilename") as picker:
            app.insert_script_reference()
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
        with patch("app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("app.load_script", return_value=inserted):
            app.insert_script_reference()
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
        with patch("app.filedialog.askopenfilename", return_value="C:/scripts/C.json"), \
             patch("app.load_script", return_value=inserted):
            app.insert_script_expanded()
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

    def test_insert_script_above_requires_selection_when_actions_exist(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "comment", "text": "A"}])
        app.root = Mock()
        app.insert_position_var = Mock()
        app.insert_position_var.get.return_value = "above"
        app.action_tree = Mock()
        app.action_tree.selection.return_value = ()
        app._notify = Mock()
        with patch("app.filedialog.askopenfilename") as picker:
            app.insert_script_expanded()
        picker.assert_not_called()
        app._notify.assert_called_once()

    def test_open_new_window_launches_second_instance(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app._set_status = Mock()
        app._notify = Mock()
        with patch("app.subprocess.Popen") as popen, \
             patch("app.sys.executable", "C:/Python313/python.exe"), \
             patch("app.sys.frozen", False, create=True), \
             patch("app.__file__", "E:/proj/app.py"):
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
        with patch("app.GlobalDetectDialog") as dialog_class:
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
        with patch("app.ModulePickerDialog") as picker_class:
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
        with patch("app.ModulePickerDialog") as picker_class, \
             patch("app.registered_module_object", return_value={"recognize": "number"}), \
             patch("app.edit_action", return_value=configured) as edit:
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
        with patch("app.ModulePickerDialog") as picker_class:
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
        with patch("app.ModulePickerDialog") as picker_class:
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
        with patch("app.ModulePickerDialog") as picker_class:
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
        app.undo_action_edit()

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

        app.redo_action_edit()

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

        app.redo_action_edit()

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

        with patch("app.save_script", return_value=Path("测试.json")):
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
            with patch("app.save_script", return_value=level_pack_dir / "A.json") as save:
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
            with patch("app.save_script", return_value=original):
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
            with patch("app.save_script", return_value=level_pack_dir / "A (2).json"):
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
            with patch("app.save_script", side_effect=RuntimeError("磁盘已满")):
                result = app.save_current_script()
            self.assertIsNone(result)
            self.assertTrue(original.exists())
            app._notify.assert_called_once_with("保存失败", "磁盘已满")

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
        with patch("app.filedialog.askopenfilename", return_value="C:/scripts/新脚本.json"):
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
        with patch("app.filedialog.askopenfilename", return_value="C:/x/new.json"):
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
        with patch("app.filedialog.askopenfilename", return_value="C:/x/new.json"):
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
        with patch("app.filedialog.askopenfilename", return_value=""):
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
        with patch("app.ModulePickerDialog") as picker_class:
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
        actions = [
            {"type": "image_match", "module_ref": True, "module_key": "module:a"},
            {"type": "image_match", "module_ref": True, "module_key": "module:b"},
        ]
        with patch("app.ModulePickerDialog") as picker_class:
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
        with patch("app.ModulePickerDialog") as picker_class:
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

    def test_restart_resolved_row_prefers_action_then_module_then_default(self):
        # 「重新执行工作流」跳转行解析：动作级 → 模块级 → 全局默认 → 第 1 行。
        app = MacroFlowApp.__new__(MacroFlowApp)
        with patch("app.load_module_restart_default_row", return_value=4):
            self.assertEqual(
                app._restart_workflow_resolved_row({"restart_workflow_target_row": 3}),
                3,
            )
            app.global_detect_active_module = {"restart_workflow_target_row": 2}
            self.assertEqual(
                app._restart_workflow_resolved_row({"type": "restart_workflow"}),
                2,
            )
            app.global_detect_active_module = {}
            self.assertEqual(
                app._restart_workflow_resolved_row({"type": "restart_workflow"}),
                4,
            )
            with patch("app.load_module_restart_default_row", return_value=0):
                self.assertEqual(
                    app._restart_workflow_resolved_row({"type": "restart_workflow"}),
                    1,
                )
            # 非法值一律视为未设置。
            app.global_detect_active_module = {"restart_workflow_target_row": "bad"}
            self.assertEqual(
                app._restart_workflow_resolved_row(
                    {"restart_workflow_target_row": "oops"},
                ),
                4,
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

    def test_workflow_module_name_reads_existing_nested_action_reference(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {
            "kind": "module",
            "action": {
                "module_key": "images/部分/资讯叉叉.png",
                "template": "images/部分/资讯叉叉.png",
            },
        }
        with patch("app.registered_module_object", return_value={"name": "资讯叉叉"}):
            self.assertEqual(app._workflow_step_name(step), "模块 资讯叉叉")

    def test_workflow_module_name_reads_persisted_action_name(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {
            "kind": "module", "action": {
                "module_key": "module:claim", "module_name": "可领取",
            },
        }
        with patch("app.registered_module_object", return_value=None):
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

        with patch("app.save_workflow", return_value=Path("flow.json")) as save:
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

        with patch("app.WorkflowBatchSettingsDialog", return_value=dialog):
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

        with patch("app.WorkflowBatchSettingsDialog", return_value=dialog):
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

        with patch("app.DurationDialog") as prompt:
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
        with patch("app.display_path", return_value="scripts/a.json"), \
             patch("app.simpledialog.askinteger") as prompt:
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

        with patch("app.save_workflow") as save:
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

        with patch("app.WorkflowRepeatDialog", return_value=dialog):
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

        with patch("app.WorkflowBatchSettingsDialog", return_value=dialog):
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
            with patch("app.get_cursor_pos", return_value=(12, 34)):
                app._log("备份完成")
            self.assertIn("[鼠标 12,34] 备份完成", app.session_log_path.read_text(encoding="utf-8"))

    def test_worker_log_is_written_before_ui_callback_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.root = Mock()
            app.log_text = Mock()
            app.log_file_lock = threading.Lock()
            app.session_log_path = Path(folder) / "2026-08-11" / "session.log"
            with patch("app.get_cursor_pos", return_value=(56, 78)):
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

        with patch("app.registered_module_object", return_value={"enabled": False}):
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
        with patch("app.registered_module_object", return_value=obj), \
             patch("app.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("app.update_module_object") as update:
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
        with patch("app.registered_module_object", return_value={"category": "global"}), \
             patch("app.spawn_new_instance") as spawn:
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

        with patch("app.registered_module_object", return_value={
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
            monitor_stop = threading.Event()
            app.global_detect_monitors = {
                "m1": {"stop": monitor_stop},
            }
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
            self.assertTrue(monitor_stop.is_set())
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
            monitor_stop = threading.Event()
            app.global_detect_monitors = {
                "m1": {"stop": monitor_stop},
            }
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
            self.assertFalse(monitor_stop.is_set())
            self.assertFalse(any("所有计次脚本已执行完毕" in call.args[0] for call in app._log.call_args_list))

    def test_unlimited_only_workflow_does_not_end_prematurely(self):
        with tempfile.TemporaryDirectory() as folder:
            unlimited_path = Path(folder) / "unlimited.json"
            save_script(MacroScript(name="不计次数", actions=[{"type": "delay", "ms": 1}]), unlimited_path)

            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow_stop = threading.Event()
            monitor_stop = threading.Event()
            app.global_detect_monitors = {
                "m1": {"stop": monitor_stop},
            }
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
            self.assertFalse(monitor_stop.is_set())
            self.assertFalse(any("所有计次脚本已执行完毕" in call.args[0] for call in app._log.call_args_list))

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

    def test_workflow_start_delay_settings_are_persisted(self):
        workflow = Workflow.from_dict({
            "name": "延时工作流", "steps": [],
            "start_delay_enabled": True, "start_delay_seconds": 12,
        })
        self.assertTrue(workflow.start_delay_enabled)
        self.assertEqual(workflow.start_delay_seconds, 12)
        self.assertEqual(workflow.to_dict()["start_delay_seconds"], 12)

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


class GlobalDetectTests(unittest.TestCase):
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
            with patch("player.registered_module_object", side_effect=lambda key: {
                "module:main": main_obj, "module:fallback": fallback_obj,
            }.get(key)), patch("player.find_template", side_effect=[
                None, fallback_match, main_match,
            ]), patch("player.send_move_absolute") as move, patch("player.send_button") as button, \
                 patch("player.show_overlay"):
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
            with patch("player.registered_module_object", side_effect=lambda key: {
                "module:main": main_obj, "module:fallback": fallback_obj,
            }.get(key)), patch("player.find_template", side_effect=[
                None, fallback_match, None, fallback_match, main_match,
            ]), patch("player.send_move_absolute") as move, patch("player.send_button") as button, \
                 patch("player.show_overlay"):
                player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:main", "template": str(main_path),
                    "region_mode": "template",
                }, None)
            move.assert_called_once_with(60, 70)
            self.assertEqual(button.call_count, 2)

    def test_no_recognition_global_module_triggers_timeout_without_matching(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_rearm_locks = set()
        app._ui = lambda fn, *args: fn(*args)
        app._log = Mock()
        app._append_mini_step = Mock()
        monitor = self._make_global_monitor(
            "", recognize="none", timeout_enabled=True, not_found_timeout_ms=0,
            timeout_triggered=False, not_found_since=time.perf_counter(),
            module_ref=False, wait_text_absent=False, awaiting_clear=False,
        )
        app._wait_workflow_global_scan_turn = Mock(return_value=True)
        app._finish_workflow_global_scan_turn = Mock()
        app._on_global_detect_timeout = Mock(
            side_effect=lambda _monitor: monitor["stop"].set(),
        )
        with patch("app.find_template") as find, patch("app.registered_module_object", return_value=None):
            app._global_detect_worker(monitor)
        find.assert_not_called()
        app._on_global_detect_timeout.assert_called_once_with(monitor)

    def test_parse_click_point(self):
        self.assertEqual(parse_click_point("640,360"), (640, 360))
        self.assertIsNone(parse_click_point(""))
        self.assertIsNone(parse_click_point("abc"))
        self.assertIsNone(parse_click_point("1,2,3"))

    def test_parse_region(self):
        self.assertEqual(parse_region("100,50,640,360"), (100, 50, 640, 360))
        self.assertIsNone(parse_region(""))
        self.assertIsNone(parse_region("100,50,0,360"))
        self.assertIsNone(parse_region("1,2,3"))

    def _make_global_monitor(self, template, hold_ms=0, interval_ms=100,
                             region=None, region_mode="screen", **overrides):
        """Build a monitor dict as used by the per-module global-detect monitors."""
        monitor = {
            "key": "<test>",
            "module": None,
            "thread": None,
            "stop": threading.Event(),
            "template": Path(template),
            "threshold": 0.85,
            "interval_ms": interval_ms,
            "hold_ms": hold_ms,
            "delay_ms": 0,
            "region_mode": region_mode,
            "region": region,
            "click": None,
            "jump_row": 0,
            "jump_action_id": "",
            "was_detected": False,
            "triggered": False,
            "match_since": None,
            "match_data": None,
        }
        monitor.update(overrides)
        return monitor

    def test_activate_global_detect_from_config_configures_monitor(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module = {"kind": "global_module", "script": "m.json", "step_id": "m1"}
        with patch("app.resolve_path", return_value=Path("images/g.png")):
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
        monitor = app.global_detect_monitors["workflow:m1"]
        self.assertEqual(monitor["threshold"], 1.0)
        self.assertEqual(monitor["interval_ms"], 100)
        self.assertEqual(monitor["hold_ms"], 600000)
        self.assertEqual(monitor["delay_ms"], 200)
        self.assertEqual(monitor["template"], Path("images/g.png"))
        self.assertEqual(monitor["click"], (640, 360))
        self.assertEqual(monitor["region"], (100, 50, 300, 200))
        self.assertEqual(monitor["module"], module)
        app._start_global_detect_monitor.assert_called_once()

    def test_script_global_module_start_delay_is_loaded_into_monitor(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_obj = {
            "enabled": True, "category": "script_global", "template": "images/g.png",
            "start_delay_ms": 125000,
        }
        with patch("app.registered_module_object", return_value=module_obj), \
             patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "module_ref": True,
                "module_key": "module:g", "action_id": "row-g",
            })
        self.assertEqual(app.global_detect_monitors["script:row-g"]["start_delay_ms"], 125000)

    def test_script_global_worker_waits_before_starting_shared_capture(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ensure_global_capture_worker = Mock()
        monitor = self._make_global_monitor(
            "g.png", start_delay_ms=2500, shared_capture=True,
        )
        monitor["stop"] = Mock()
        monitor["stop"].wait.return_value = True
        app._global_detect_worker(monitor)
        monitor["stop"].wait.assert_called_once_with(2.5)
        app._ensure_global_capture_worker.assert_not_called()

    def test_workflow_global_scan_uses_one_screenshot_in_module_order(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_pending_restart = False
        app.exiting = False
        app._ui = lambda callback, *args: callback(*args)
        app._log = Mock()
        first = self._make_global_monitor("a.png", key="a", module={"step_id": "a"})
        second = self._make_global_monitor("b.png", key="b", module={"step_id": "b"})
        app.global_detect_monitors = {"a": first, "b": second}
        screen = np.zeros((60, 80, 3), dtype=np.uint8)

        with patch("app.capture_bgr", return_value=(screen, (-20, 0))) as capture, \
             patch("app.find_template_in_image", return_value=None) as match:
            self.assertTrue(app._wait_workflow_global_scan_turn(first))
            app._workflow_global_match(first, Path("a.png"), 0.8, None)
            app._finish_workflow_global_scan_turn(first)
            self.assertTrue(app._wait_workflow_global_scan_turn(second))
            app._workflow_global_match(second, Path("b.png"), 0.9, None)
            app._finish_workflow_global_scan_turn(second)

        capture.assert_called_once_with()
        self.assertEqual([call.args[0].name for call in match.call_args_list], ["a.png", "b.png"])
        self.assertIs(match.call_args_list[0].args[1], screen)
        self.assertIs(match.call_args_list[1].args[1], screen)
        self.assertIsNone(app.workflow_global_scan_screen)

    def test_triggered_workflow_global_pauses_following_modules(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_pending_restart = False
        app.exiting = False
        first = self._make_global_monitor("a.png", key="a", module={"step_id": "a"})
        second = self._make_global_monitor("b.png", key="b", module={"step_id": "b"})
        app.global_detect_monitors = {"a": first, "b": second}
        app.workflow_global_scan_condition = threading.Condition()
        app.workflow_global_scan_turn = 0
        app.workflow_global_scan_screen = np.zeros((2, 2, 3), dtype=np.uint8)
        app.workflow_global_scan_origin = (0, 0)
        app.workflow_global_scan_paused = False

        app._finish_workflow_global_scan_turn(first, pause=True)

        self.assertTrue(app.workflow_global_scan_paused)
        self.assertEqual(app.workflow_global_scan_turn, 0)

    def test_global_module_not_found_timeout_triggers_timeout_branch(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        monitor = self._make_global_monitor(
            "missing-timeout.png", module_ref=False,
            timeout_enabled=True, not_found_timeout_ms=0,
            timeout_segment=[{"type": "delay", "ms": 1}],
            timeout_triggered=False, not_found_since=time.perf_counter(),
        )
        app._on_global_detect_timeout = Mock(side_effect=lambda _monitor: monitor["stop"].set())

        app._global_detect_worker(monitor)

        app._on_global_detect_timeout.assert_called_once_with(monitor)
        self.assertTrue(monitor["timeout_triggered"])

    def test_global_detect_text_module_trigger_uses_ocr_text_box(self):
        # 识别文字全局模块：点击 OCR 返回的命中文字中心，不再点整个区域中心。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app.global_detect_rearm_locks = set()
        monitor = self._make_global_monitor(
            "module-x.png", hold_ms=0, region=(100, 50, 300, 200),
            recognize="text", expected_text="体力不足", match_mode="contains",
            timeout_enabled=False,
        )
        app._on_global_detect_match = Mock(
            side_effect=lambda _monitor: monitor["stop"].set(),
        )

        def fake_ui(callback, *args):
            callback(*args)

        app._ui = fake_ui
        found = {
            "text": "体力不足", "x": 180, "y": 80, "width": 80, "height": 30,
            "center_x": 220, "center_y": 95, "score": 0.99,
        }
        with patch(
            "app.recognize_region_with_boxes", return_value=("体力不足", [found]),
        ) as recognize, \
             patch("app.show_overlay"):
            app._global_detect_worker(monitor)
        recognize.assert_called_once()
        self.assertEqual(monitor["match_data"]["center_x"], 220)
        self.assertEqual(monitor["match_data"]["center_y"], 95)
        app._on_global_detect_match.assert_called_once_with(monitor)

    def test_global_text_absent_waits_for_present_then_disappeared_edge(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app.global_detect_rearm_locks = set()
        monitor = self._make_global_monitor(
            "module-x.png", hold_ms=0, region=(100, 50, 300, 200),
            recognize="text", expected_text="加载中", match_mode="contains",
            wait_text_absent=True, target_absent_armed=False, timeout_enabled=False,
        )
        app._on_global_detect_match = Mock(
            side_effect=lambda _monitor: monitor["stop"].set(),
        )
        app._ui = lambda callback, *args: callback(*args)
        with patch(
            "app.recognize_region_with_boxes",
            side_effect=[
                ("其他文字", [{"text": "其他文字"}]),
                ("加载中", [{"text": "加载中", "center_x": 10, "center_y": 20}]),
                ("已完成", [{"text": "已完成"}]),
            ],
        ) as recognize, patch("app.show_overlay"):
            app._global_detect_worker(monitor)
        self.assertEqual(recognize.call_count, 3)
        self.assertTrue(monitor["target_absent_armed"])
        app._on_global_detect_match.assert_called_once_with(monitor)

    def test_global_template_absent_waits_for_present_then_disappeared_edge(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app.global_detect_rearm_locks = set()
        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "target.png"
            template.touch()
            monitor = self._make_global_monitor(
                template, hold_ms=0, region=(100, 50, 300, 200),
                wait_text_absent=True, target_absent_armed=False,
                timeout_enabled=False,
            )
            app._on_global_detect_match = Mock(
                side_effect=lambda _monitor: monitor["stop"].set(),
            )
            app._ui = lambda callback, *args: callback(*args)
            found = {
                "x": 180, "y": 80, "width": 80, "height": 30,
                "center_x": 220, "center_y": 95, "score": 0.99,
            }
            with patch("app.find_template", side_effect=[found, None]) as find, \
                 patch("app.show_overlay"):
                app._global_detect_worker(monitor)

        self.assertEqual(find.call_count, 2)
        self.assertTrue(monitor["target_absent_armed"])
        self.assertEqual(monitor["match_data"]["center_x"], 220)
        app._on_global_detect_match.assert_called_once_with(monitor)

    def test_global_detect_text_module_not_found_triggers_timeout_branch(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app._append_mini_step = Mock()
        app._ui = lambda callback, *args: callback(*args)
        monitor = self._make_global_monitor(
            "module-x.png", region=None, recognize="text", expected_text="体力不足",
            match_mode="contains", timeout_enabled=True, not_found_timeout_ms=0,
        )
        app._on_global_detect_timeout = Mock(
            side_effect=lambda _monitor: monitor["stop"].set(),
        )

        with patch(
            "app.recognize_region_with_boxes",
            return_value=("其他文字", [{"text": "其他文字"}]),
        ):
            app._global_detect_worker(monitor)
        app._on_global_detect_timeout.assert_called_once_with(monitor)
        self.assertTrue(monitor["timeout_triggered"])
        observation = "体力不足 OCR：识别到「其他文字」；期望「体力不足」· 未命中"
        app._log.assert_any_call(observation)
        app._append_mini_step.assert_any_call(observation)

    def test_global_modules_match_against_same_published_screenshot(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_capture_condition = threading.Condition()
        app.global_capture_generation = 7
        app.global_capture_screen = np.zeros((60, 80, 3), dtype=np.uint8)
        app.global_capture_origin = (-20, 0)
        app.exiting = False
        first = {"stop": threading.Event(), "capture_generation": 0}
        second = {"stop": threading.Event(), "capture_generation": 0}
        with patch("app.find_template_in_image", return_value=None) as match:
            app._shared_global_match(first, Path("a.png"), 0.8, None)
            app._shared_global_match(second, Path("b.png"), 0.9, (1, 2, 3, 4))
        self.assertEqual(match.call_count, 2)
        self.assertIs(match.call_args_list[0].args[1], app.global_capture_screen)
        self.assertIs(match.call_args_list[1].args[1], app.global_capture_screen)
        self.assertEqual(first["capture_generation"], 7)
        self.assertEqual(second["capture_generation"], 7)

    def test_module_ref_activation_uses_resolved_object_and_preserves_rearm_lock(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_rearm_locks = {"workflow:m1"}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        logs = []
        app._log = Mock(side_effect=logs.append)
        app._ui = lambda callback, *args: callback(*args)
        module = {"kind": "global_module", "step_id": "m1"}
        resolved = Path("C:/Macro/images/点击游戏画面.png")
        obj = {
            "threshold": 0.85, "interval_ms": 250, "hold_ms": 100,
            "delay_ms": 0, "after_action": "click_match", "button": "left",
        }

        with patch("app.resolve_path", return_value=resolved), \
             patch("app.registered_module_object", return_value=obj) as lookup:
            app._activate_global_detect_from_config({
                "template": "images/点击游戏画面.png", "module_ref": True,
                "hold_ms": 1000,
            }, module)

        monitor = app.global_detect_monitors["workflow:m1"]
        self.assertEqual(monitor["hold_ms"], 100)
        self.assertTrue(monitor["awaiting_clear"])
        self.assertTrue(any("持续超过 100 ms" in text for text in logs))
        self.assertEqual(lookup.call_count, 2)
        self.assertTrue(all(
            item.args == ("images/点击游戏画面.png",) for item in lookup.call_args_list
        ))

    def test_disabled_module_reference_cannot_register_global_monitor(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()

        with patch("app.resolve_path", return_value=Path("images/disabled.png")), \
             patch("app.registered_module_object", return_value={
                 "name": "已禁用模块", "enabled": False,
             }):
            app._activate_global_detect_from_config({
                "module_ref": True, "module_key": "module:disabled",
                "template": "images/disabled.png",
            }, {"kind": "global_module", "step_id": "disabled-row"})

        self.assertEqual(app.global_detect_monitors, {})
        app._start_global_detect_monitor.assert_not_called()
        self.assertTrue(any("已禁用" in call.args[0] for call in app._log.call_args_list))

    def test_activate_global_detect_from_config_carries_jump_row(self):
        # v1.68：普通脚本内嵌全局模块行的配置携带跳转行，启用日志随之变化。
        # v1.70：启用日志按动作标识解析跳转目标并显示具体内容。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "key_press", "name": "A", "action_id": "target-a"},
            {"type": "delay", "ms": 500, "action_id": "other"},
        ])
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "jump_row": 4,
                "jump_action_id": "target-a",
            })
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertEqual(monitor["jump_row"], 1)
        self.assertEqual(monitor["jump_action_id"], "target-a")
        self.assertTrue(any("触发后跳转到第 1 行（键盘：A）执行" in text for text in logs))

    def test_pending_global_restart_rejects_queued_monitor_registration(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_pending_restart = True
        app.global_detect_monitors = {}
        app._start_global_detect_monitor = Mock()

        app._activate_global_detect_from_config({"template": "images/late.png"})

        self.assertEqual(app.global_detect_monitors, {})
        app._start_global_detect_monitor.assert_not_called()

    def test_stop_global_monitors_can_clear_previous_generation(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        first = {"stop": threading.Event()}
        second = {"stop": threading.Event()}
        app.global_detect_monitors = {"a": first, "b": second}

        app._stop_all_global_detect_monitors(clear=True)

        self.assertTrue(first["stop"].is_set())
        self.assertTrue(second["stop"].is_set())
        self.assertEqual(app.global_detect_monitors, {})

    def test_activate_global_detect_defaults_click_delay_to_1000ms(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "global-a",
                "template": "images/g.png",
            })
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertEqual(monitor["delay_ms"], 1000)

    def test_activate_global_detect_multiple_modules_each_get_own_monitor(self):
        # 核心回归：每个全局模块启用后都有自己的监控，互不覆盖。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        module_a = {"kind": "global_module", "script": "a.json", "step_id": "a"}
        module_b = {"kind": "global_module", "script": "b.json", "step_id": "b"}
        with patch("app.resolve_path", side_effect=[Path("images/a.png"), Path("images/b.png")]):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/a.png"}, module_a,
            )
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/b.png"}, module_b,
            )
        self.assertEqual(set(app.global_detect_monitors), {"workflow:a", "workflow:b"})
        self.assertEqual(app.global_detect_monitors["workflow:a"]["template"], Path("images/a.png"))
        self.assertEqual(app.global_detect_monitors["workflow:b"]["template"], Path("images/b.png"))
        self.assertEqual(app._start_global_detect_monitor.call_count, 2)
        # 同一个模块重新启用（工作流恢复）会替换旧监控，而不是再开一个。
        with patch("app.resolve_path", return_value=Path("images/a.png")):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/a.png"}, module_a,
            )
        self.assertEqual(len(app.global_detect_monitors), 2)

    def test_script_global_actions_each_keep_an_independent_monitor(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_rearm_locks = set()
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)

        with patch("app.resolve_path", side_effect=[Path("images/mainline.png"), Path("images/init.png")]):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "mainline",
                "template": "images/mainline.png",
            })
            first_stop = app.global_detect_monitors["script:mainline"]["stop"]
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "initialize",
                "template": "images/init.png",
            })

        self.assertEqual(
            set(app.global_detect_monitors),
            {"script:mainline", "script:initialize"},
        )
        self.assertFalse(first_stop.is_set())
        self.assertEqual(app._start_global_detect_monitor.call_count, 2)

    def test_script_global_actions_have_independent_rearm_locks(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_rearm_locks = {"script:mainline"}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)

        with patch("app.resolve_path", side_effect=[Path("images/mainline.png"), Path("images/init.png")]):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "mainline",
                "template": "images/mainline.png",
            })
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "initialize",
                "template": "images/init.png",
            })

        self.assertTrue(app.global_detect_monitors["script:mainline"]["awaiting_clear"])
        self.assertFalse(app.global_detect_monitors["script:initialize"]["awaiting_clear"])

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

    def test_leaving_script_scope_stops_only_its_script_globals(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        script_monitor = {"stop": threading.Event()}
        workflow_monitor = {"stop": threading.Event()}
        app.global_detect_monitors = {
            "script:one": script_monitor,
            "workflow:one": workflow_monitor,
        }
        app.global_detect_rearm_locks = {"script:one", "workflow:one"}

        app._exit_script_global_scope(("script:one",))

        self.assertTrue(script_monitor["stop"].is_set())
        self.assertNotIn("script:one", app.global_detect_monitors)
        self.assertFalse(workflow_monitor["stop"].is_set())
        self.assertIn("workflow:one", app.global_detect_monitors)
        self.assertEqual(app.global_detect_rearm_locks, {"workflow:one"})

    def test_script_scope_preserves_rearm_lock_during_interrupt_resume(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_pending_restart = True
        app.global_detect_monitors = {"script:one": {"stop": threading.Event()}}
        app.global_detect_rearm_locks = {"script:one"}

        app._exit_script_global_scope(("script:one",))

        self.assertEqual(app.global_detect_rearm_locks, {"script:one"})

    def test_activate_global_detect_skipped_while_module_steps_run(self):
        # 模块步骤执行期间，脚本内的全局检测动作回读时不再重复启用监控。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = True
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "template": "images/g.png"},
            )
        self.assertEqual(app.global_detect_monitors, {})

    def test_activate_global_detect_region_mode_parsing(self):
        # 旧配置没有 region_mode：无区域 → 全屏；有区域 → 自定义区域。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config(
                {"type": "global_detect", "action_id": "global-a", "template": "images/g.png"},
            )
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertEqual(monitor["region_mode"], "screen")
        self.assertIsNone(monitor["region"])
        with patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "region": [100, 50, 300, 200],
            })
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertEqual(monitor["region_mode"], "custom")
        self.assertEqual(monitor["region"], (100, 50, 300, 200))
        # 显式 window 模式：记录模式，region 无意义。
        with patch("app.resolve_path", return_value=Path("images/g.png")):
            app._activate_global_detect_from_config({
                "type": "global_detect",
                "action_id": "global-a",
                "template": "images/g.png",
                "region_mode": "window",
            })
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertEqual(monitor["region_mode"], "window")

    def test_activate_global_detect_template_mode_reads_registered_region(self):
        # v1.78：region_mode="template" 时区域运行时从模板登记表读取。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.resolve_path", return_value=Path("images/g.png")), \
             patch("app.registered_template_region", return_value=[100, 50, 300, 200]):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "global-a", "template": "images/g.png",
                "region_mode": "template",
            })
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertEqual(monitor["region_mode"], "template")
        self.assertEqual(monitor["region"], (100, 50, 300, 200))
        self.assertTrue(any("区域 模板区域" in text for text in logs))

    def test_activate_global_detect_template_without_region_uses_fullscreen(self):
        # 模板未登记 / 未设置区域：按全屏检测并在日志中告警。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_monitors = {}
        app.global_detect_module_running = False
        app._start_global_detect_monitor = Mock()
        logs = []
        app._log = Mock(side_effect=lambda text: logs.append(text))
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.resolve_path", return_value=Path("images/g.png")), \
             patch("app.registered_template_region", return_value=None):
            app._activate_global_detect_from_config({
                "type": "global_detect", "action_id": "global-a", "template": "images/g.png",
                "region_mode": "template",
            })
        monitor = app.global_detect_monitors["script:global-a"]
        self.assertIsNone(monitor["region"])
        self.assertTrue(any("模板未设置区域，按全屏检测" in text for text in logs))

    def test_trigger_summary_template_mode_shows_template_region(self):
        # v1.78：引用模板的触发条件摘要显示"区域：模板"，不展开坐标。
        app = MacroFlowApp.__new__(MacroFlowApp)
        summary = app._trigger_summary({
            "template": "images/g.png", "region_mode": "template",
            "region": [], "hold_ms": 1500,
        })
        self.assertIn("g.png", summary)
        self.assertIn("区域：模板", summary)
        self.assertNotIn("0,0,0,0", summary)
        self.assertIn("持续超过 1500 ms", summary)

    def test_worker_window_mode_uses_bound_window_rect(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._bound_hwnd = Mock(return_value=12345)
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(
                template_path, region_mode="window",
            )

            def fake_ui(callback, *args):
                callback(*args)
                monitor["stop"].set()

            app._ui = fake_ui
            match = {"x": 1, "y": 2, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 16, "center_y": 22}
            with patch("app.find_template", return_value=match) as find, \
                 patch("app.get_window_rect", return_value=(1, 2, 300, 200)), \
                 patch("app.show_overlay"):
                app._global_detect_worker(monitor)
            app._bound_hwnd.assert_called_once()
            # 每轮用目标窗口当前区域作为识别区域。
            find.assert_called_once()
            self.assertEqual(find.call_args.args[2], (1, 2, 300, 200))

    def test_start_global_detect_monitor_starts_thread(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._global_detect_worker = Mock()
        monitor = self._make_global_monitor("images/g.png")
        with patch("app.threading.Thread") as thread_cls:
            app._start_global_detect_monitor(monitor)
        thread_cls.assert_called_once_with(
            target=app._global_detect_worker, args=(monitor,), daemon=True,
        )
        thread_cls.return_value.start.assert_called_once()
        self.assertIsNotNone(monitor["thread"])
        # 启动即重置该监控的运行状态。
        self.assertFalse(monitor["was_detected"])
        self.assertFalse(monitor["triggered"])

    def test_worker_triggers_once_on_rising_edge(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(template_path)
            calls = []

            def fake_ui(callback, *args):
                calls.append((callback, args))
                callback(*args)
                monitor["stop"].set()

            app._ui = fake_ui
            match = {"x": 10, "y": 20, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 25, "center_y": 40}
            with patch("app.find_template", return_value=match), \
                 patch("app.show_overlay") as show:
                app._global_detect_worker(monitor)
            # 先记录"识别到"诊断日志，再触发。
            self.assertEqual(len(calls), 2)
            self.assertIs(calls[0][0], app._log)
            self.assertIn("识别到", calls[0][1][0])
            self.assertIs(calls[1][0], app._on_global_detect_match)
            app._on_global_detect_match.assert_called_once_with(monitor)
            self.assertIn("<test>", app.global_detect_rearm_locks)
            # 识别到（上升沿）和触发时都在匹配区域周围画框提醒。
            show.assert_called()
            show.assert_any_call(10, 20, 30, 40)

    def test_disabled_hold_delay_triggers_on_first_match(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._on_global_detect_match = Mock()
            logs = []
            app._log = Mock(side_effect=logs.append)
            monitor = self._make_global_monitor(
                template_path, hold_ms=60000, hold_enabled=False,
            )

            def fake_ui(callback, *args):
                callback(*args)
                if callback is app._on_global_detect_match:
                    monitor["stop"].set()

            app._ui = fake_ui
            match = {
                "x": 10, "y": 20, "width": 30, "height": 40, "score": 0.9,
                "center_x": 25, "center_y": 40,
            }
            with patch("app.find_template", return_value=match), patch("app.show_overlay"):
                app._global_detect_worker(monitor)

            app._on_global_detect_match.assert_called_once_with(monitor)
            self.assertTrue(any("立即触发" in text for text in logs))

    def test_rebuilt_monitor_waits_while_triggered_image_still_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.global_detect_rearm_locks = {"<test>"}
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(
                template_path, awaiting_clear=True, awaiting_clear_logged=False,
            )

            def fake_ui(callback, *args):
                callback(*args)
                monitor["stop"].set()

            app._ui = fake_ui
            match = {
                "x": 10, "y": 20, "width": 30, "height": 40,
                "score": 0.9, "center_x": 25, "center_y": 40,
            }
            with patch("app.find_template", return_value=match):
                app._global_detect_worker(monitor)

            app._on_global_detect_match.assert_not_called()
            self.assertIn("<test>", app.global_detect_rearm_locks)
            self.assertTrue(monitor["awaiting_clear"])
            self.assertIn("图片仍存在", app._log.call_args.args[0])

    def test_rebuilt_monitor_rearms_only_after_it_confirms_image_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.global_detect_rearm_locks = {"<test>"}
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(
                template_path, awaiting_clear=True, awaiting_clear_logged=False,
            )

            def fake_ui(callback, *args):
                callback(*args)
                monitor["stop"].set()

            app._ui = fake_ui
            with patch("app.find_template", return_value=None):
                app._global_detect_worker(monitor)

            self.assertNotIn("<test>", app.global_detect_rearm_locks)
            self.assertFalse(monitor["awaiting_clear"])
            self.assertIn("已确认消失", app._log.call_args.args[0])

    def test_worker_requires_hold_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(template_path, hold_ms=1000)
            calls = []

            def fake_ui(callback, *args):
                calls.append((callback, args))
                callback(*args)
                # 只在真正触发时停止，识别到的诊断日志不影响继续计时。
                if callback is app._on_global_detect_match:
                    monitor["stop"].set()

            app._ui = fake_ui
            match = {"x": 10, "y": 20, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 25, "center_y": 40}
            with patch("app.find_template", return_value=match), \
                 patch("app.time.perf_counter", side_effect=[100.0, 101.5]), \
                 patch("app.show_overlay") as show:
                app._global_detect_worker(monitor)
            # 第一轮 0 ms 未达到持续时长，第二轮 1500 ms 才触发。
            self.assertEqual(len(calls), 2)
            self.assertIs(calls[0][0], app._log)
            self.assertIn("识别到", calls[0][1][0])
            self.assertIs(calls[1][0], app._on_global_detect_match)
            app._on_global_detect_match.assert_called_once_with(monitor)
            show.assert_called()

    def test_worker_module_ref_reads_object_each_round(self):
        # 引用模块：worker 每轮实时重读对象（阈值/间隔/区域/持续时长），
        # 修改对象即时生效。
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(
                template_path, module_ref=True, interval_ms=500,
                threshold=0.85, hold_ms=1000,
            )

            def fake_ui(callback, *args):
                callback(*args)
                monitor["stop"].set()

            app._ui = fake_ui
            obj = {
                "category": "switch", "region": [1, 2, 30, 40],
                "threshold": 0.9, "interval_ms": 300, "blocking": False,
                "hold_ms": 2000, "delay_ms": 0, "after_action": "click_match",
            }
            match = {"x": 1, "y": 2, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 16, "center_y": 22}
            with patch("app.registered_module_object", return_value=obj) as obj_lookup, \
                 patch("app.find_template", return_value=match) as find, \
                 patch("app.show_overlay"):
                app._global_detect_worker(monitor)
            obj_lookup.assert_called_once_with(str(template_path))
            # 阈值 / 区域来自对象，且 hold_ms 被对象值覆盖。
            find.assert_called_once_with(template_path, 0.9, (1, 2, 30, 40), ignore_background=False)
            self.assertEqual(monitor["hold_ms"], 2000)

    def test_run_global_detect_action_clicks_then_continues(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", click=(640, 360), delay_ms=0,
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button, \
             patch("app.get_cursor_pos", return_value=(640, 360)), \
             patch.object(app, "_ensure_global_click_foreground") as ensure, \
             patch("app.time.sleep"):
            app._run_global_detect_action(monitor)
        move.assert_called_once_with(640, 360)
        button.assert_any_call("left", True)
        button.assert_any_call("left", False)
        ensure.assert_called_once_with(640, 360)
        # 点击信息也会写入滚动小窗。
        app._ui.assert_any_call(app._append_mini_step, "全局检测已点击 (640, 360)")
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_action_waits_delay(self):
        # 旧配置带点击位置：点击后等待点击后延时，再继续检测。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        app.standalone_global_replay = None
        stop = Mock()
        stop.wait.return_value = False
        monitor = self._make_global_monitor(
            "images/g.png", click=(100, 200), delay_ms=800, stop=stop,
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button"), \
             patch("app.get_cursor_pos", return_value=(100, 200)), \
             patch.object(app, "_ensure_global_click_foreground"), \
             patch("app.time.sleep"):
            app._run_global_detect_action(monitor)
        stop.wait.assert_called_once_with(0.8)
        move.assert_called_once_with(100, 200)
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_action_clicks_match_position_when_no_click_point(self):
        # 未配置固定点击位置时，点击识别到的位置（与识图动作默认行为一致）。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        app.standalone_global_replay = None
        monitor = self._make_global_monitor("images/g.png")
        monitor["match_data"] = {"x": 10, "y": 20, "width": 30, "height": 40,
                                 "center_x": 25, "center_y": 40, "score": 0.9}
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button, \
             patch("app.get_cursor_pos", return_value=(25, 40)), \
             patch.object(app, "_ensure_global_click_foreground"), \
             patch("app.time.sleep"):
            app._run_global_detect_action(monitor)
        move.assert_called_once_with(25, 40)
        button.assert_any_call("left", True)
        button.assert_any_call("left", False)
        app._ui.assert_any_call(app._append_mini_step, "全局检测已点击 (25, 40)")

    def test_run_global_detect_action_trigger_format_skips_click_and_delay(self):
        # 触发条件新格式（无 click_point）：不点击、不延时，直接进入触发后流程。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        app.standalone_global_replay = {"actions": []}
        stop = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", click=None, delay_ms=500, stop=stop,
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button:
            app._run_global_detect_action(monitor)
        move.assert_not_called()
        button.assert_not_called()
        stop.wait.assert_not_called()
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_module_ref_click_match_center(self):
        # 引用文字模块动作 B = 点击 OCR 匹配中心并应用四向偏移。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="click_match", button="right",
            match_data={"center_x": 25, "center_y": 40},
            ocr_offset_up=2, ocr_offset_down=7,
            ocr_offset_left=3, ocr_offset_right=13,
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button, \
             patch("app.get_cursor_pos", return_value=(35, 45)), \
             patch.object(app, "_ensure_global_click_foreground"), \
             patch("app.time.sleep"):
            app._run_global_detect_action(monitor)
        move.assert_called_once_with(35, 45)
        button.assert_any_call("right", True)
        button.assert_any_call("right", False)
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_module_ref_respects_click_count(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="click_match",
            button="left", click_count=3,
            match_data={"center_x": 25, "center_y": 40},
        )
        with patch("app.send_move_absolute"), \
             patch("app.send_button") as button, \
             patch("app.get_cursor_pos", return_value=(25, 40)), \
             patch.object(app, "_ensure_global_click_foreground"), \
             patch("app.time.sleep"):
            app._run_global_detect_action(monitor)
        self.assertEqual(button.call_count, 6)
        app._ui.assert_any_call(
            app._append_mini_step, "全局检测已点击 (25, 40) × 3",
        )

    def test_run_global_detect_module_ref_click_custom_point(self):
        # 动作 B = 点击自定义位置：点击对象配置的点击点。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="click_custom",
            click=(640, 360),
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button, \
             patch("app.get_cursor_pos", return_value=(640, 360)), \
             patch.object(app, "_ensure_global_click_foreground"), \
             patch("app.time.sleep"):
            app._run_global_detect_action(monitor)
        move.assert_called_once_with(640, 360)
        button.assert_any_call("left", True)
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_module_ref_continue_skips_click(self):
        # 动作 B = continue：不点击，直接继续。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="continue",
            match_data={"center_x": 25, "center_y": 40},
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button:
            app._run_global_detect_action(monitor)
        move.assert_not_called()
        button.assert_not_called()
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_module_ref_run_actions_sets_segment_ready(self):
        # 动作 B = 执行代码段：只置 segment_ready 标志，绝不在监控线程播放。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        segment = [{"type": "delay", "ms": 5, "action_id": "s1"}]
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="run_actions",
            segment=segment, segment_ready=False,
        )
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button:
            app._run_global_detect_action(monitor)
        move.assert_not_called()
        button.assert_not_called()
        self.assertTrue(monitor["segment_ready"])
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_run_global_detect_module_ref_continue_sets_post_segment_ready(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        segment = [{"type": "restart_workflow", "action_id": "s1"}]
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="continue",
            segment=segment, segment_ready=False,
        )
        app._run_global_detect_module_ref(monitor)
        self.assertTrue(monitor["segment_ready"])
        app._ui.assert_called_once_with(app._after_global_detect_action, monitor)

    def test_ensure_global_click_foreground_activates_window_inside_bounds(self):
        # 点击点落在绑定窗口矩形内且窗口不在前台：点击前先前置目标窗口，
        # 避免点击落在该位置最上层的其他窗口上、激活窗口改变后重复触发。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._log = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._bound_hwnd = Mock(return_value=123)
        with patch("app.is_window_process_foreground", return_value=False) as foreground, \
             patch("app.get_window_rect", return_value=(0, 0, 1920, 1080)), \
             patch("app.activate_window", return_value=True) as activate:
            app._ensure_global_click_foreground(1243, 134)
        foreground.assert_called_once_with(123)
        activate.assert_called_once_with(123)
        self.assertTrue(any("已把目标窗口置于前台" in call.args[0]
                            for call in app._log.call_args_list))

    def test_ensure_global_click_foreground_skips_when_already_foreground(self):
        # 目标窗口已在前台：点击前不再重复前置。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._bound_hwnd = Mock(return_value=123)
        with patch("app.is_window_process_foreground", return_value=True), \
             patch("app.activate_window") as activate:
            app._ensure_global_click_foreground(640, 360)
        activate.assert_not_called()

    def test_ensure_global_click_foreground_skips_click_outside_bounds(self):
        # 点击点不在绑定窗口矩形内（模块想点击其他窗口）：不前置目标窗口。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._bound_hwnd = Mock(return_value=123)
        with patch("app.is_window_process_foreground", return_value=False), \
             patch("app.get_window_rect", return_value=(0, 0, 1920, 1080)), \
             patch("app.activate_window") as activate:
            app._ensure_global_click_foreground(2000, 2000)
        activate.assert_not_called()

    def test_ensure_global_click_foreground_skips_without_bound_window(self):
        # 未绑定窗口（独立脚本）：点击前不前置任何窗口。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._bound_hwnd = Mock(return_value=None)
        with patch("app.is_window_process_foreground") as foreground, \
             patch("app.activate_window") as activate:
            app._ensure_global_click_foreground(640, 360)
        foreground.assert_not_called()
        activate.assert_not_called()

    def test_run_global_detect_module_ref_second_match_polls(self):
        # 动作 B = 二次识别后点击：转发给 _poll_second_match_click。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        app._poll_second_match_click = Mock()
        second = {"template": "images/s2.png", "region": [], "timeout_ms": 3000,
                  "blocking": False}
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="second_match",
            second=second,
        )
        with patch("app.send_move_absolute"), patch("app.send_button"):
            app._run_global_detect_action(monitor)
        app._poll_second_match_click.assert_called_once_with(monitor)
        app._ui.assert_not_called()

    def test_poll_second_match_click_finds_and_clicks(self):
        # 二次识别到目标模板：点击其中心后继续。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            second_path = Path(folder) / "s2.png"
            second_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._ui = Mock()
            monitor = self._make_global_monitor(
                "images/g.png", module_ref=True, after_action="second_match",
                threshold=0.9, interval_ms=100,
                second={"template": str(second_path), "region": [0, 0, 10, 10],
                        "timeout_ms": 3000, "blocking": False},
            )
            match = {"x": 1, "y": 2, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 16, "center_y": 22}
            with patch("app.find_template", return_value=match) as find, \
                 patch("app.registered_template_region", return_value=[0, 0, 10, 10]), \
                 patch("app.show_overlay") as show, \
                 patch("app.send_move_absolute") as move, \
                 patch("app.send_button") as button, \
                 patch.object(app, "_ensure_global_click_foreground") as ensure, \
                 patch("app.time.sleep"):
                app._poll_second_match_click(monitor)
            find.assert_called_once_with(second_path, 0.9, (0, 0, 10, 10), ignore_background=False)
            show.assert_called_once_with(1, 2, 30, 40)
            move.assert_called_once_with(16, 22)
            button.assert_any_call("left", True)
            ensure.assert_called_once_with(16, 22)
            app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_global_second_match_can_click_first_or_custom_region(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            second_path = Path(folder) / "s2.png"
            second_path.write_bytes(b"x")
            found = {"x": 1, "y": 2, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 16, "center_y": 22}
            for target, click_region, expected in (
                ("first", [], (70, 80)),
                ("custom_region", [100, 200, 80, 40], (140, 220)),
            ):
                with self.subTest(target=target):
                    app = MacroFlowApp.__new__(MacroFlowApp)
                    app._ui = Mock()
                    monitor = self._make_global_monitor(
                        "images/g.png", module_ref=True, after_action="second_match",
                        threshold=0.9, interval_ms=100,
                        match_data={"center_x": 70, "center_y": 80},
                        second={
                            "template": str(second_path), "region": [],
                            "timeout_ms": 3000, "blocking": False,
                            "click_target": target, "click_region": click_region,
                        },
                    )
                    with patch("app.find_template", return_value=found), \
                         patch("app.show_overlay"), \
                         patch("app.send_move_absolute") as move, \
                         patch("app.send_button"), \
                         patch.object(app, "_ensure_global_click_foreground"), \
                         patch("app.time.sleep"):
                        app._poll_second_match_click(monitor)
                    move.assert_called_once_with(*expected)

    def test_poll_second_match_click_timeout_continues(self):
        # 非阻塞二次识别超时：按 continue 继续，不点击。
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            second_path = Path(folder) / "s2.png"
            second_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._ui = Mock()
            stop = threading.Event()
            monitor = self._make_global_monitor(
                "images/g.png", module_ref=True, after_action="second_match",
                interval_ms=50, stop=stop,
                second={"template": str(second_path), "region": [],
                        "timeout_ms": 60, "blocking": False},
            )
            with patch("app.find_template", return_value=None), \
                 patch("app.send_move_absolute") as move, \
                 patch("app.send_button") as button, \
                 patch("app.time.perf_counter", side_effect=[100.0, 100.1]):
                app._poll_second_match_click(monitor)
            move.assert_not_called()
            button.assert_not_called()
            texts = [str(call.args[1]) for call in app._ui.call_args_list
                     if call.args and len(call.args) > 1]
            self.assertTrue(any("超时" in text for text in texts))
            app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_poll_second_match_click_missing_template_skips(self):
        # 二次识别模板不存在：提示后按 continue 继续。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "images/g.png", module_ref=True, after_action="second_match",
            second={"template": "images/missing.png", "region": [],
                    "timeout_ms": 3000, "blocking": False},
        )
        with patch("app.find_template") as find, \
             patch("app.send_move_absolute") as move:
            app._poll_second_match_click(monitor)
        find.assert_not_called()
        move.assert_not_called()
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_after_global_detect_standalone_with_body_replays_script_actions(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = None
        app.standalone_global_replay = {
            "actions": [{"type": "delay", "delay_ms": 5}],
            "hwnd": 123, "activation_hwnd": None,
            "source_screen": None, "activate_target": False,
        }
        app._log = Mock()
        app._append_mini_step = Mock()
        app._play_standalone_global_body = Mock()
        monitor = self._make_global_monitor("images/g.png", triggered=True)
        with patch("app.threading.Thread") as thread_class:
            app._after_global_detect_action(monitor)
        app._log.assert_called_once_with("全局检测触发：执行脚本动作。")
        app._append_mini_step.assert_called_once_with("全局检测触发：执行脚本动作。")
        # 语句体回放在后台线程执行，不阻塞检测。
        thread_class.assert_called_once_with(
            target=app._play_standalone_global_body, args=(monitor,), daemon=True,
        )

    def test_play_standalone_global_body_plays_actions_and_resets_trigger(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.player = Mock()
        app._ui = Mock()
        app.standalone_global_replay = {
            "actions": [{"type": "delay", "delay_ms": 5}],
            "hwnd": 123, "activation_hwnd": 456,
            "source_screen": (0, 0, 1920, 1080), "activate_target": True,
        }
        app.global_detect_module_running = False
        monitor = self._make_global_monitor("images/g.png", triggered=True)
        app._play_standalone_global_body(monitor)
        app.player.play.assert_called_once_with(
            [{"type": "delay", "delay_ms": 5}], 1, 123,
            source_screen=(0, 0, 1920, 1080),
            activate_target=True, activation_hwnd=456,
        )
        app._ui.assert_any_call(app._log, "全局脚本动作执行完成，继续检测。")
        self.assertFalse(monitor["triggered"])
        self.assertFalse(app.global_detect_module_running)

    def test_play_standalone_global_body_skips_empty_body(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.player = Mock()
        app._ui = Mock()
        app.standalone_global_replay = {"actions": []}
        app.global_detect_module_running = False
        monitor = self._make_global_monitor("images/g.png", triggered=True)
        app._play_standalone_global_body(monitor)
        app.player.play.assert_not_called()
        self.assertFalse(monitor["triggered"])
        self.assertFalse(app.global_detect_module_running)

    def test_after_global_detect_standalone_script_only_clicks_and_keeps_detecting(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = None
        app.standalone_global_replay = None
        app._log = Mock()
        app._append_mini_step = Mock()
        monitor = self._make_global_monitor("images/g.png", triggered=True)
        app._after_global_detect_action(monitor)
        # 单独执行脚本（非工作流）：触发后只点击，检测保持运行，条件仍满足则再次触发。
        self.assertFalse(monitor["triggered"])
        app._log.assert_called_once_with("全局检测触发：已点击，继续检测。")
        # 触发信息也写入滚动小窗。
        app._append_mini_step.assert_called_once_with("全局检测触发：已点击，继续检测。")

    def test_run_global_detect_action_skips_click_for_jump_row(self):
        # 模块行（jump_row）触发后不点击，直接进入触发后流程。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._ui = Mock()
        app.standalone_global_replay = None
        monitor = self._make_global_monitor("images/g.png", click=None, jump_row=3)
        monitor["match_data"] = {"x": 10, "y": 20, "width": 30, "height": 40,
                                 "center_x": 25, "center_y": 40, "score": 0.9}
        with patch("app.send_move_absolute") as move, \
             patch("app.send_button") as button:
            app._run_global_detect_action(monitor)
        move.assert_not_called()
        button.assert_not_called()
        app._ui.assert_any_call(app._after_global_detect_action, monitor)

    def test_after_global_detect_jump_row_starts_jump_thread(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = None
        app.script = MacroScript(actions=[])
        app._log = Mock()
        app._append_mini_step = Mock()
        app.standalone_jump_pending = False
        app.standalone_jump_done = threading.Event()
        app.player = Mock()
        app.player.running = True
        app.player.stop_event = threading.Event()
        monitor = self._make_global_monitor("images/g.png", triggered=True,
                                            jump_row=3)
        with patch("app.threading.Thread") as thread_class:
            app._after_global_detect_action(monitor)
        self.assertTrue(app.standalone_jump_pending)
        app.player.stop.assert_called_once()
        app._log.assert_called_once_with(
            "全局检测触发：检测到 模块[脚本全局模块] · g.png，跳转到第 3 行执行，播放完脚本结束。",
        )
        thread_class.assert_called_once_with(
            target=app._play_standalone_jump_body, args=(monitor,), daemon=True,
        )

    def test_after_global_detect_resolves_jump_target_by_action_id(self):
        # v1.70：跳转目标是行的对象（action_id 引用）——行移动后仍解析到当前行号。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = None
        app.script = MacroScript(actions=[
            {"type": "delay", "ms": 1, "action_id": "other"},
            {"type": "key_press", "name": "A", "action_id": "target-a"},
            {"type": "delay", "ms": 2, "action_id": "later"},
        ])
        app._log = Mock()
        app._append_mini_step = Mock()
        app.standalone_jump_pending = False
        app.standalone_jump_done = threading.Event()
        app.player = Mock()
        app.player.running = False
        app.player.stop_event = threading.Event()
        monitor = self._make_global_monitor("images/g.png", triggered=True,
                                            jump_row=1, jump_action_id="target-a")
        with patch("app.threading.Thread") as thread_class:
            app._after_global_detect_action(monitor)
        self.assertEqual(monitor["jump_row"], 2)
        app._log.assert_called_once_with(
            "全局检测触发：检测到 模块[脚本全局模块] · g.png，跳转到第 2 行（键盘：A）执行，播放完脚本结束。",
        )
        app._append_mini_step.assert_called_once_with(
            "全局检测触发：跳转到第 2 行（键盘：A）执行。",
        )
        thread_class.assert_called_once_with(
            target=app._play_standalone_jump_body, args=(monitor,), daemon=True,
        )

    def test_resolve_module_jump_row_falls_back_to_config_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "ms": 1, "action_id": "x"}])
        monitor = self._make_global_monitor("images/g.png", jump_row=7)
        self.assertEqual(app._resolve_module_jump_row(monitor), 7)
        monitor = self._make_global_monitor("images/g.png", jump_row=0, jump_action_id="missing")
        self.assertEqual(app._resolve_module_jump_row(monitor), 1)
        monitor = self._make_global_monitor("images/g.png", jump_row=3, jump_action_id="x")
        self.assertEqual(app._resolve_module_jump_row(monitor), 1)

    def test_action_short_text_shows_concrete_content(self):
        # v1.70：跳转日志里的目标行要带具体信息，不能只有行号。
        self.assertEqual(action_short_text({"type": "key_press", "name": "A"}), "键盘：A")
        self.assertEqual(action_short_text({"type": "delay", "ms": 500}), "延时：500 ms")
        self.assertEqual(action_short_text({"type": "image_match", "template": "images/g.png"}), "识图：g.png")
        self.assertEqual(action_short_text({"type": "text", "text": "你好"}), "文本：你好")
        self.assertEqual(action_short_text({"type": "text"}), "文本")
        self.assertEqual(action_short_text({"type": "repeat_click", "count": 3}), "连续点击：3 次")
        self.assertEqual(action_short_text({"type": "click"}), "点击")
        self.assertEqual(action_short_text({"type": "mouse_move", "mode": "relative"}), "鼠标移动（相对）")
        self.assertEqual(action_short_text({"type": "text_ocr", "expected_text": "体力不足"}), "识别文字：体力不足")
        self.assertEqual(action_short_text({"type": "text_ocr"}), "识别文字：任意文字")

    def test_module_jump_target_text_includes_target_content(self):
        # v1.70：跳转目标描述 = 行号 + 目标行内容；目标行不存在时只有行号。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "key_press", "name": "A"},
            {"type": "delay", "ms": 500},
        ])
        self.assertEqual(app._module_jump_target_text(2), "第 2 行（延时：500 ms）")
        self.assertEqual(app._module_jump_target_text(99), "第 99 行")
        self.assertEqual(app._module_jump_target_text(0), "第 0 行")

    def test_action_summary_module_ref_renders_live_object(self):
        # 引用模块摘要实时渲染仓库对象：分类/区域/阻塞/延时/动作。
        action = {
            "type": "image_match", "template": "images/s.png",
            "module_ref": True, "module_category": "switch",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        obj = {
            "category": "switch", "region": [10, 20, 300, 400],
            "threshold": 0.85, "interval_ms": 250, "blocking": True,
            "hold_ms": 1000, "delay_ms": 1000, "after_action": "click_match",
        }
        with patch("app.registered_module_object", return_value=obj):
            kind, detail, _delay = action_summary(action)
        self.assertIn("识图模块", kind)
        self.assertIn("引用切换模块 s", detail)
        self.assertIn("区域 10,20,300,400", detail)
        self.assertIn("阻塞直到出现", detail)
        self.assertIn("延时 1000 ms", detail)
        self.assertIn("动作 点击识别区域", detail)
        self.assertIn("结果 成功后继续下一行 / 失败后继续下一行", detail)

    def test_action_summary_module_ref_shows_row_result_branches(self):
        action = {
            "type": "image_match", "template": "images/s.png",
            "module_ref": True, "on_found": "jump",
            "found_jump_action_id": "success-target",
            "on_timeout": "end_current_script",
        }
        with patch("app.registered_module_object", return_value={
            "name": "入口", "category": "switch", "region": [],
            "blocking": False, "delay_ms": 0, "after_action": "continue",
        }):
            _kind, detail, _delay = action_summary(
                action, {"success-target": 7},
            )
        self.assertIn("成功后跳到第 7 行", detail)
        self.assertIn("失败后结束当前最里层脚本", detail)

    def test_action_summary_number_module_shows_comparison_branches(self):
        action = {
            "type": "image_match", "module_ref": True, "module_key": "module:number",
            "expected_number": 12, "on_found": "jump",
            "found_jump_action_id": "equal", "on_timeout": "jump",
            "timeout_jump_action_id": "other",
        }
        obj = {
            "name": "剩余次数", "category": "switch", "recognize": "number",
            "region": [10, 20, 80, 30], "blocking": False,
            "not_found_timeout_ms": 1000, "after_action": "continue", "delay_ms": 0,
        }
        with patch("app.registered_module_object", return_value=obj):
            _kind, detail, _delay = action_summary(action, {"equal": 3, "other": 5})
            short = action_short_text(action)
        self.assertIn("读取数字", detail)
        self.assertIn("比较 12", detail)
        self.assertIn("等于时跳到第 3 行", detail)
        self.assertIn("不等于或未读取到时跳到第 5 行", detail)
        self.assertEqual(short, "读取数字：模块 剩余次数")

    def test_action_summary_module_ref_missing_object_falls_back(self):
        # 对象被删除：按内嵌参数回退渲染并提示。
        action = {
            "type": "global_detect", "template": "images/g.png",
            "module_ref": True, "module_category": "global",
            "region_mode": "template", "region": [], "delay_ms": 0,
        }
        with patch("app.registered_module_object", return_value=None):
            kind, detail, _delay = action_summary(action)
        self.assertIn("全局模块", kind)
        self.assertIn("对象不存在", detail)

    def test_action_summary_text_ocr(self):
        kind, detail, _delay = action_summary({
            "type": "text_ocr",
            "expected_text": "体力不足",
            "match_mode": "contains",
            "region_mode": "custom",
            "region": [10, 20, 300, 400],
            "timeout_ms": 3000,
            "interval_ms": 500,
            "on_found": "continue",
            "on_timeout": "jump",
            "timeout_jump_action_id": "target456",
            "found_delay_ms": 0,
            "timeout_delay_ms": 100,
            "show_result_notice": False,
        }, action_rows={"target456": 4})
        self.assertIn("识别文字", kind)
        self.assertIn("期望 体力不足（包含）", detail)
        self.assertIn("区域 10,20,300,400", detail)
        self.assertIn("找到后继续", detail)
        self.assertIn("等待超时 3000 ms", detail)
        self.assertIn("超时跳到第 4 行目标动作", detail)
        self.assertIn("超时后等待 100 ms", detail)
        self.assertIn("间隔 500 ms", detail)

    def test_action_summary_text_ocr_any_text_and_stop(self):
        _kind, detail, _delay = action_summary({
            "type": "text_ocr",
            "expected_text": "",
            "region_mode": "screen",
            "timeout_ms": 0,
            "on_found": "jump",
            "found_jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
            "on_timeout": "stop",
            "show_result_notice": False,
        })
        self.assertIn("期望 任意文字（包含）", detail)
        self.assertIn("区域 全屏", detail)
        self.assertIn("找到后结束当前脚本，执行工作流下一项", detail)
        self.assertIn("只识别一次", detail)
        self.assertIn("超时停止", detail)

    def test_action_summary_restart_workflow_special(self):
        kind, detail, _delay = action_summary({"type": "restart_workflow"})
        self.assertIn("特殊模块", kind)
        self.assertIn("重新执行工作流", detail)
        self.assertEqual(
            action_short_text({"type": "restart_workflow"}),
            "特殊模块：重新执行工作流",
        )

    def test_action_summary_end_current_script_special(self):
        kind, detail, _delay = action_summary({"type": "end_current_script"})
        self.assertIn("特殊模块", kind)
        self.assertIn("结束当前最里层脚本", detail)
        self.assertEqual(
            action_short_text({"type": "end_current_script"}),
            "特殊模块：结束当前最里层脚本，继续执行",
        )

    def test_action_summary_unconditional_jump_targets(self):
        _kind, detail, _delay = action_summary({
            "type": "jump", "jump_action_id": SCRIPT_START_TARGET_ID,
        })
        self.assertIn("脚本开头", detail)
        _kind, detail, _delay = action_summary({
            "type": "jump", "jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID,
        })
        self.assertIn("脚本结尾", detail)
        _kind, detail, _delay = action_summary({
            "type": "jump", "jump_action_id": "target",
            "workflow_repeat_at_least_2": True,
        }, {"target": 7})
        self.assertIn("第 7 行", detail)
        self.assertIn("仅工作流第 2 次及以后", detail)

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
        # 工作流中：置标志、解析动作级跳转行、停播放与全局监控，并轮询等 worker 死后重启。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = 0
        app.workflow_restart_requested = False
        app.workflow = Workflow(steps=[{"script": "a.json", "step_id": "row-a"}])
        app.player = Mock()
        app.workflow_stop = threading.Event()
        app.worker = None
        scheduled = []
        app._ui = lambda callback, *args: scheduled.append(callback)
        app._stop_all_global_detect_monitors = Mock()
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
        app._stop_all_global_detect_monitors.assert_called_once()
        # 主线程轮询：worker 已死 → 立即重启工作流。
        app._poll_workflow_stop_for_restart_workflow()
        app._launch_workflow_restart.assert_called_once()

    def test_on_restart_workflow_request_inside_global_module_is_effective(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_step_index = None
        app.global_module_workflow_context = True
        app.global_detect_pending_restart = True
        app.workflow_restart_requested = False
        app.workflow = Workflow(steps=[{"script": "a.json", "step_id": "row-a"}])
        app.player = Mock()
        app.workflow_stop = threading.Event()
        app._stop_all_global_detect_monitors = Mock()
        app._ui = Mock()
        with patch("app.load_module_restart_default_row", return_value=2):
            result = app._on_restart_workflow_request({"type": "restart_workflow"})
        self.assertTrue(result)
        self.assertTrue(app.workflow_restart_requested)
        self.assertEqual(app.workflow_restart_target_row, 2)
        app._ui.assert_called_once_with(app._poll_workflow_stop_for_restart_workflow)

    def test_poll_workflow_stop_for_restart_waits_for_worker(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.worker = Mock()
        app.worker.is_alive.return_value = True
        app._launch_workflow_restart = Mock()
        app.root = Mock()
        app._poll_workflow_stop_for_restart_workflow()
        app._launch_workflow_restart.assert_not_called()
        app.root.after.assert_called_once()

    def test_launch_workflow_restart_restores_repeats_snapshot(self):
        # 快照恢复被消费的行（repeats/unlimited），再完整重启工作流。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"kind": "script", "script": "a.json", "repeats": 0, "unlimited": False},
            {"kind": "global_module", "script": "m.json", "repeats": 1},
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
        self.assertEqual(steps[0]["repeats"], 2)
        self.assertEqual(steps[1]["repeats"], 3)
        self.assertTrue(steps[1]["unlimited"])
        self.assertFalse(app.workflow_restart_requested)
        self.assertIsNone(app.workflow_repeats_snapshot)
        app.run_workflow.assert_called_once_with(
            start_index=0, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )

    def test_launch_workflow_restart_uses_configured_row_object(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"kind": "script", "script": "a.json", "step_id": "row-a"},
            {"kind": "script", "script": "b.json", "step_id": "row-b"},
        ])
        app.workflow_restart_requested = True
        app.workflow_restart_target_row = 2
        app.workflow_repeats_snapshot = None
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app.run_workflow = Mock()

        app._launch_workflow_restart()

        app.run_workflow.assert_called_once_with(
            start_index=1, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
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
        app.workflow_repeats_snapshot = None
        app.rebuild_workflow_tree = Mock()
        app._persist_workflow_draft = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app.run_workflow = Mock()

        app._launch_workflow_restart()

        app.run_workflow.assert_called_once_with(
            start_index=1, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )
        # 重启完成后目标行复位，避免影响下一次触发。
        self.assertEqual(app.workflow_restart_target_row, 1)

    def test_workflow_module_enabled_follows_registry_state(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        step = {"kind": "module", "action": {"module_key": "module:test"}}
        with patch("app.registered_module_object", return_value={"enabled": False}):
            self.assertFalse(app._workflow_module_enabled(step))
        with patch("app.registered_module_object", return_value={"enabled": True}):
            self.assertTrue(app._workflow_module_enabled(step))

    def test_play_standalone_jump_body_plays_from_jump_row(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "delay", "delay_ms": 10},
            {"type": "delay", "delay_ms": 20},
            {"type": "delay", "delay_ms": 30},
            {"type": "delay", "delay_ms": 40},
        ])
        app.player = Mock()
        app.player.running = False
        app.player.stop_event = threading.Event()
        app._ui = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app._bound_hwnd = Mock(return_value=123)
        app._activation_settings_from_script = Mock(return_value=(False, None))
        app._execution_activation_hwnd = Mock(return_value=456)
        app.activate_target_enabled_var = Mock()
        app.activate_target_enabled_var.get.return_value = True
        app.main_hidden_for_execution = True
        app.global_detect_module_running = False
        app.standalone_jump_pending = True
        app.standalone_jump_done = threading.Event()
        monitor = self._make_global_monitor("images/g.png", triggered=True,
                                            jump_row=3)
        app._play_standalone_jump_body(monitor)
        # 从第 3 行（下标 2）播放到末尾，重复 1 次。
        app.player.play.assert_called_once()
        call = app.player.play.call_args
        self.assertEqual(call.args[0], app.script.actions)
        self.assertEqual(call.args[1], 1)
        self.assertEqual(call.args[2], 123)
        self.assertEqual(call.kwargs["start_index"], 2)
        # 播放结束后：停止检测、复位 pending、通知 worker 收尾。
        self.assertTrue(monitor["stop"].is_set())
        self.assertFalse(app.standalone_jump_pending)
        self.assertTrue(app.standalone_jump_done.is_set())
        self.assertFalse(app.global_detect_module_running)

    def test_play_standalone_jump_body_manages_ui_when_worker_finished(self):
        # 主播放已自然结束（worker 已收尾）：跳转播放自己接管执行界面。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "delay_ms": 5}])
        app.player = Mock()
        app.player.running = False
        app.player.stop_event = threading.Event()
        app._ui = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app._bound_hwnd = Mock(return_value=None)
        app._activation_settings_from_script = Mock(return_value=(False, None))
        app._execution_activation_hwnd = Mock(return_value=None)
        app.activate_target_enabled_var = Mock()
        app.activate_target_enabled_var.get.return_value = False
        app.main_hidden_for_execution = False
        app.global_detect_module_running = False
        app.standalone_jump_pending = True
        app.standalone_jump_done = threading.Event()
        monitor = self._make_global_monitor("images/g.png", triggered=True,
                                            jump_row=1)
        app._play_standalone_jump_body(monitor)
        app._ui.assert_any_call(app._hide_main_for_execution)
        app._ui.assert_any_call(app._show_execution_mini)
        app._ui.assert_any_call(app._finish_execution_visibility)

    def test_play_standalone_jump_body_skips_when_stop_requested(self):
        # F12 已按下：不再启动跳转播放，但仍停止检测并通知 worker 收尾。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[{"type": "delay", "delay_ms": 5}])
        app.player = Mock()
        app.player.running = False
        app.player.stop_event = threading.Event()
        app.player.stop_event.set()
        app._ui = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app._bound_hwnd = Mock(return_value=None)
        app._activation_settings_from_script = Mock(return_value=(False, None))
        app._execution_activation_hwnd = Mock(return_value=None)
        app.activate_target_enabled_var = Mock()
        app.activate_target_enabled_var.get.return_value = False
        app.main_hidden_for_execution = True
        app.global_detect_module_running = False
        app.standalone_jump_pending = True
        app.standalone_jump_done = threading.Event()
        monitor = self._make_global_monitor("images/g.png", triggered=True,
                                            jump_row=1)
        app._play_standalone_jump_body(monitor)
        app.player.play.assert_not_called()
        self.assertTrue(monitor["stop"].is_set())
        self.assertTrue(app.standalone_jump_done.is_set())

    def test_play_standalone_jump_body_plays_segment_before_jump(self):
        # 引用模块动作 B = 代码段：先播放代码段，再播放跳转段。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "delay", "delay_ms": 10},
            {"type": "delay", "delay_ms": 20},
            {"type": "delay", "delay_ms": 30},
        ])
        app.player = Mock()
        app.player.running = False
        app.player.stop_event = threading.Event()
        app._ui = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app._bound_hwnd = Mock(return_value=123)
        app._activation_settings_from_script = Mock(return_value=(False, None))
        app._execution_activation_hwnd = Mock(return_value=456)
        app.activate_target_enabled_var = Mock()
        app.activate_target_enabled_var.get.return_value = True
        app.main_hidden_for_execution = True
        app.global_detect_module_running = False
        app.standalone_jump_pending = True
        app.standalone_jump_done = threading.Event()
        segment = [{"type": "delay", "ms": 5, "action_id": "s1"}]
        monitor = self._make_global_monitor(
            "images/g.png", triggered=True, jump_row=2,
            segment=segment, segment_ready=True,
        )
        app._play_standalone_jump_body(monitor)
        self.assertEqual(app.player.play.call_count, 2)
        first = app.player.play.call_args_list[0]
        self.assertEqual(first.args[0], segment)
        self.assertEqual(first.args[1], 1)
        self.assertEqual(first.args[2], 123)
        second = app.player.play.call_args_list[1]
        self.assertEqual(second.args[0], app.script.actions)
        self.assertEqual(second.kwargs["start_index"], 1)
        self.assertTrue(monitor["stop"].is_set())
        self.assertTrue(app.standalone_jump_done.is_set())

    def test_play_standalone_jump_body_skips_out_of_range_jump(self):
        # 末尾插入的全局模块引用：jump_row 越界（len+1），段播完后不再跳转，
        # 脚本自然结束。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.script = MacroScript(actions=[
            {"type": "delay", "delay_ms": 10},
        ])
        app.player = Mock()
        app.player.running = False
        app.player.stop_event = threading.Event()
        app._ui = Mock()
        app._log = Mock()
        app._append_mini_step = Mock()
        app._bound_hwnd = Mock(return_value=123)
        app._activation_settings_from_script = Mock(return_value=(False, None))
        app._execution_activation_hwnd = Mock(return_value=456)
        app.activate_target_enabled_var = Mock()
        app.activate_target_enabled_var.get.return_value = True
        app.main_hidden_for_execution = True
        app.global_detect_module_running = False
        app.standalone_jump_pending = True
        app.standalone_jump_done = threading.Event()
        segment = [{"type": "delay", "ms": 5, "action_id": "s1"}]
        monitor = self._make_global_monitor(
            "images/g.png", triggered=True, jump_row=2,
            segment=segment, segment_ready=True,
        )
        app._play_standalone_jump_body(monitor)
        # 只播放代码段，跳转行越界被跳过，脚本到此结束。
        app.player.play.assert_called_once_with(
            segment, 1, 123,
            source_screen={"left": 0, "top": 0, "width": 1920, "height": 1080},
            activate_target=True, activation_hwnd=456,
            propagate_current_script_jump=True,
        )
        self.assertTrue(monitor["stop"].is_set())
        self.assertTrue(app.standalone_jump_done.is_set())

    def test_run_script_worker_waits_for_jump_play_to_finish(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app._enter_focus_mode = Mock()
        app._leave_focus_mode = Mock()
        app._ui = Mock()
        app._sound = Mock()
        app._finish_execution_visibility = Mock()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.player.stop_event.set()  # 模块行触发后播放被停止
        app.standalone_jump_pending = True
        app.standalone_jump_done = threading.Event()
        thread = threading.Thread(
            target=app._run_script_worker,
            args=([{"type": "delay", "delay_ms": 10}], 1, None, None, False, None, False, 0),
            daemon=True,
        )
        thread.start()
        time.sleep(0.15)
        # 跳转播放未结束时 worker 等待，不显示"执行完成"。
        self.assertTrue(thread.is_alive())
        app.standalone_jump_done.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        texts = [
            str(call.args[1]) if len(call.args) > 1 else ""
            for call in app._ui.call_args_list
        ]
        self.assertTrue(any("脚本执行完成" in text for text in texts))

    def test_worker_retriggers_while_condition_still_met(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "g.png"
            template_path.write_bytes(b"x")
            app = MacroFlowApp.__new__(MacroFlowApp)
            app._on_global_detect_match = Mock()
            app._log = Mock()
            monitor = self._make_global_monitor(template_path)
            calls = []

            def fake_ui(callback, *args):
                calls.append((callback, args))
                callback(*args)
                if callback is app._on_global_detect_match:
                    if len([c for c, _ in calls if c is app._on_global_detect_match]) == 1:
                        # 模拟触发流程完成时的复位：图片仍在，worker 下一轮再次触发。
                        monitor["triggered"] = False
                    else:
                        monitor["stop"].set()

            app._ui = fake_ui
            match = {"x": 10, "y": 20, "width": 30, "height": 40, "score": 0.9,
                     "center_x": 25, "center_y": 40}
            with patch("app.find_template", return_value=match), \
                 patch("app.show_overlay"):
                app._global_detect_worker(monitor)
            self.assertGreaterEqual(len(calls), 3)
            self.assertEqual(
                app._on_global_detect_match.call_count, 2,
            )

    def test_worker_logs_missing_template_once(self):
        with tempfile.TemporaryDirectory() as folder:
            template_path = Path(folder) / "missing.png"
            app = MacroFlowApp.__new__(MacroFlowApp)
            stop = Mock()
            stop.is_set.return_value = False
            stop.wait.side_effect = [False, True]
            monitor = self._make_global_monitor(
                template_path, stop=stop,
            )
            app._on_global_detect_match = Mock()
            app._log = Mock()
            app._ui = lambda callback, *args: callback(*args)
            with patch("app.find_template") as find, patch("app.show_overlay"):
                app._global_detect_worker(monitor)
            find.assert_not_called()
            # 模板缺失是"加了没反应"的常见原因：日志提示一次，不每轮刷屏。
            app._log.assert_called_once()
            self.assertIn("模板图片不存在", app._log.call_args.args[0])

    def test_after_global_detect_launches_when_worker_idle(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a.json"}])
        app.current_workflow_step_index = 0
        app.worker = None
        app.workflow_stop = threading.Event()
        app.global_detect_trigger_count = 1
        app._launch_global_detect_restart = Mock()
        app._log = Mock()
        monitor = self._make_global_monitor("images/g.png")
        app._after_global_detect_action(monitor)
        app._launch_global_detect_restart.assert_called_once_with(monitor)

    def test_after_global_detect_uses_saved_workflow_context_after_worker_clears_current(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a.json"}])
        app.current_workflow_step_index = None
        app.worker = None
        app.workflow_stop = threading.Event()
        app.global_detect_trigger_count = 1
        app._launch_global_detect_restart = Mock()
        app._log = Mock()
        monitor = self._make_global_monitor("images/g.png")
        monitor["workflow_resume_snapshot"] = (0, 2, 4)

        app._after_global_detect_action(monitor)

        app._launch_global_detect_restart.assert_called_once_with(monitor)

    def test_after_global_detect_stops_running_worker_before_steps(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a.json"}])
        app.current_workflow_step_index = 0
        worker = Mock()
        worker.is_alive.return_value = True
        app.worker = worker
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.root = Mock()
        app.global_detect_pending_restart = False
        app.global_detect_restart_polls = 0
        app._launch_global_detect_restart = Mock()
        app._log = Mock()
        monitor = self._make_global_monitor("images/g.png")
        app._after_global_detect_action(monitor)
        self.assertTrue(app.workflow_stop.is_set())
        app.player.stop.assert_called_once()
        # 通过轮询回调等待执行线程真正退出，再启动模块步骤。
        self.assertEqual(app.root.after.call_args[0][0], 100)
        app.root.after.call_args[0][1]()
        self.assertEqual(app.global_detect_restart_polls, 1)
        app._launch_global_detect_restart.assert_not_called()

    def test_launch_global_detect_restart_runs_module_steps_thread(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_pending_restart = False
        app.global_detect_restart_polls = 0
        app.workflow_stop = threading.Event()
        app._bound_hwnd = Mock(return_value=12345)
        monitor = self._make_global_monitor("images/g.png")
        with patch("app.threading.Thread") as thread_cls:
            app._launch_global_detect_restart(monitor)
        self.assertFalse(app.workflow_stop.is_set())
        thread_cls.assert_called_once_with(
            target=app._run_global_module_steps,
            args=(monitor, 12345), daemon=True,
        )
        thread_cls.return_value.start.assert_called_once()

    def test_on_global_detect_match_snapshots_resume_position(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_trigger_count = 0
        app.global_detect_resume_action = 0
        app.current_workflow_step_index = 2
        app.current_workflow_repeat_index = 1
        app.current_workflow_action_index = 3
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app._log = Mock()
        app._run_global_detect_action = Mock()
        module = {"kind": "global_module", "script": "m.json", "step_id": "m1"}
        monitor = self._make_global_monitor("g.png", module=module)
        with patch("app.threading.Thread") as thread_cls:
            app._on_global_detect_match(monitor)
        self.assertEqual(app.global_detect_resume_index, 2)
        self.assertEqual(app.global_detect_resume_repeat, 1)
        self.assertEqual(app.global_detect_resume_action, 3)
        self.assertEqual(monitor["workflow_resume_snapshot"], (2, 1, 3))
        self.assertTrue(app.global_detect_pending_restart)
        self.assertTrue(app.workflow_stop.is_set())
        app.player.stop.assert_called_once()
        self.assertEqual(app.global_detect_trigger_count, 1)
        thread_cls.assert_called_once_with(
            target=app._run_global_detect_action, args=(monitor,), daemon=True,
        )

    def test_on_global_detect_match_marks_script_end_target(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_pending_restart = False
        app.global_detect_trigger_count = 0
        app.current_workflow_step_index = 0
        app.current_workflow_repeat_index = 0
        app.current_workflow_action_index = 2
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app._log = Mock()
        monitor = self._make_global_monitor("g.png")
        monitor["jump_action_id"] = NEXT_WORKFLOW_STEP_TARGET_ID
        with patch("app.threading.Thread"):
            app._on_global_detect_match(monitor)
        self.assertFalse(app.global_detect_end_current_script)
        self.assertTrue(app.global_detect_advance_workflow_step)

    def test_additional_trigger_is_ignored_while_global_resume_is_pending(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_pending_restart = True
        app.global_detect_trigger_count = 7
        monitor = self._make_global_monitor("g.png")

        with patch("app.threading.Thread") as thread_cls:
            app._on_global_detect_match(monitor)

        self.assertEqual(app.global_detect_trigger_count, 7)
        thread_cls.assert_not_called()

    def test_on_global_detect_match_ignored_while_module_steps_run(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = True
        app.global_detect_trigger_count = 0
        app._log = Mock()
        app._run_global_detect_action = Mock()
        monitor = self._make_global_monitor("g.png")
        with patch("app.threading.Thread") as thread_cls:
            app._on_global_detect_match(monitor)
        self.assertEqual(app.global_detect_trigger_count, 0)
        thread_cls.assert_not_called()

    def test_run_global_module_steps_plays_script_then_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "m.json"
            actions = [{"type": "delay", "delay_ms": 100}]
            save_script(MacroScript(name="m", actions=actions), script_path)
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.global_detect_module_running = False
            app.global_detect_resume_index = 0
            app.global_detect_resume_repeat = 0
            app.global_detect_resume_action = 0
            app.global_detect_trigger_count = 1
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app.workflow_stop = threading.Event()
            app.workflow = Workflow(steps=[{"script": "a.json"}])
            app.run_workflow = Mock()
            app._log = Mock()
            app.activate_target_enabled_var = Mock()
            app.activate_target_enabled_var.get.return_value = True
            app._execution_activation_hwnd = Mock(return_value=None)
            module = {
                "kind": "global_module", "script": str(script_path),
                "step_id": "m1",
            }
            monitor = self._make_global_monitor(
                "g.png", module=module, triggered=True,
            )
            calls = []

            def fake_ui(callback, *args):
                calls.append(callback)
                callback(*args)

            app._ui = fake_ui
            with patch("app.resolve_path", return_value=script_path):
                app._run_global_module_steps(monitor, 12345)
            app.player.play.assert_called_once()
            self.assertEqual(app.player.play.call_args[0][0], actions)
            self.assertFalse(app.global_detect_module_running)
            self.assertFalse(monitor["triggered"])
            self.assertIn(app._resume_workflow_after_global_module, calls)

    def test_run_global_module_steps_missing_script_resumes(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.player = Mock()
        app._ui = Mock()
        module = {"kind": "global_module", "script": "scripts/gone.json"}
        monitor = self._make_global_monitor(
            "g.png", module=module, triggered=True,
        )
        with patch("app.resolve_path", return_value=Path("scripts/gone.json")):
            app._run_global_module_steps(monitor, None)
        app.player.play.assert_not_called()
        self.assertFalse(app.global_detect_module_running)
        self.assertFalse(monitor["triggered"])
        app._ui.assert_any_call(app._resume_workflow_after_global_module)

    def test_run_global_module_steps_plays_segment_when_ready(self):
        # 引用模块动作 B = 代码段：播放代码段后恢复工作流，不再播放模块脚本。
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_resume_index = 0
        app.global_detect_resume_repeat = 0
        app.global_detect_resume_action = 0
        app.global_detect_trigger_count = 1
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.workflow_stop = threading.Event()
        app.workflow = Workflow(steps=[{"script": "a.json"}])
        app.run_workflow = Mock()
        app._log = Mock()
        segment = [{"type": "delay", "ms": 5, "action_id": "s1"}]
        monitor = self._make_global_monitor(
            "g.png", module_ref=True, triggered=True,
            segment=segment, segment_ready=True,
        )
        calls = []

        def fake_ui(callback, *args):
            calls.append(callback)
            callback(*args)

        app._ui = fake_ui
        app._run_global_module_steps(monitor, 12345)
        app.player.play.assert_called_once_with(
            segment, 1, 12345,
            propagate_current_script_jump=True,
        )
        app._log.assert_any_call("模块附加代码段执行完成。")
        self.assertFalse(monitor["segment_ready"])
        self.assertFalse(monitor["triggered"])
        self.assertFalse(app.global_detect_module_running)
        # 段播完恢复工作流，并保留刚触发模块的重启锁。
        app.run_workflow.assert_called_once_with(
            start_index=0, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )
        self.assertIn(app._resume_workflow_after_global_module, calls)
        self.assertTrue(
            app.run_workflow.call_args.kwargs["preserve_global_rearm_locks"]
        )

    def test_global_module_segment_can_end_interrupted_script(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.global_detect_end_current_script = False
        app.workflow_restart_requested = False
        app.player = Mock()
        app.player.play.return_value = True
        app._log = Mock()
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "g.png", module_ref=True, triggered=True,
            segment=[{"type": "end_current_script"}], segment_ready=True,
        )

        app._run_global_module_steps(monitor, 12345)

        self.assertTrue(app.global_detect_end_current_script)
        app._ui.assert_any_call(
            app._log, "模块代码段要求结束当前最里层脚本，继续执行。",
        )
        app._ui.assert_any_call(app._resume_workflow_after_global_module)

    def test_global_module_post_code_restart_suppresses_old_breakpoint_resume(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.workflow_restart_requested = False
        app.player = Mock()
        app.player.play.side_effect = lambda *_args, **_kwargs: setattr(
            app, "workflow_restart_requested", True,
        )
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "g.png", module_ref=True, triggered=True,
            segment=[{"type": "restart_workflow"}], segment_ready=True,
            workflow_resume_snapshot={"step_index": 0},
        )
        app._run_global_module_steps(monitor, 12345)
        callbacks = [call.args[0] for call in app._ui.call_args_list if call.args]
        self.assertNotIn(app._resume_workflow_after_global_module, callbacks)
        self.assertFalse(app.global_module_workflow_context)

    def test_run_global_module_steps_config_module_resumes_without_play(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.player = Mock()
        app._ui = Mock()
        module = {"kind": "global_module", "script": ""}
        monitor = self._make_global_monitor(
            "g.png", module=module, triggered=True,
        )
        app._run_global_module_steps(monitor, None)
        app.player.play.assert_not_called()
        self.assertFalse(app.global_detect_module_running)
        self.assertFalse(monitor["triggered"])
        app._ui.assert_any_call(app._resume_workflow_after_global_module)

    def test_global_module_segment_requests_current_script_last_action(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.global_detect_module_running = False
        app.workflow_restart_requested = False
        app.global_detect_jump_current_script_last = False
        app.global_detect_jumped_referenced_last = False
        app.player = Mock()
        app.player.play.return_value = JUMP_CURRENT_SCRIPT_LAST_RESULT
        # 触发时不在被引用脚本内部：不提供最内层脚本，走"跳当前脚本末行"分支。
        app.player._last_stop_referenced_actions = None
        app.player._last_stop_referenced_source_screen = None
        app._ui = Mock()
        monitor = self._make_global_monitor(
            "g.png", module={"kind": "global_module", "script": ""},
            segment=[{"type": "jump_current_script_last"}], segment_ready=True,
        )

        app._run_global_module_steps(monitor, 123)

        self.assertTrue(app.global_detect_jump_current_script_last)
        self.assertTrue(app.player.play.call_args.kwargs["propagate_current_script_jump"])
        app._ui.assert_any_call(app._resume_workflow_after_global_module)

    def test_resume_workflow_after_global_module_uses_snapshot(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a"}, {"script": "b"}])
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.global_detect_resume_index = 1
        app.global_detect_resume_repeat = 2
        app.global_detect_resume_action = 3
        app.global_detect_trigger_count = 1
        app.run_workflow = Mock()
        app._log = Mock()
        app._resume_workflow_after_global_module()
        app.run_workflow.assert_called_once_with(
            start_index=1, start_repeat=2, resume_action_index=3,
            preserve_global_rearm_locks=True,
        )
        self.assertIsNone(app.global_detect_resume_index)
        self.assertEqual(app.global_detect_resume_repeat, 0)
        self.assertEqual(app.global_detect_resume_action, 0)

    def test_resume_workflow_after_global_module_defaults_to_start(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a"}])
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.global_detect_resume_index = None
        app.global_detect_resume_repeat = 0
        app.global_detect_resume_action = 0
        app.global_detect_trigger_count = 1
        app.run_workflow = Mock()
        app._log = Mock()
        app._resume_workflow_after_global_module()
        app.run_workflow.assert_called_once_with(
            start_index=0, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )

    def test_resume_global_module_jump_uses_current_script_actual_last_row(self):
        with tempfile.TemporaryDirectory() as folder:
            script_path = Path(folder) / "current.json"
            save_script(MacroScript(name="当前脚本", actions=[
                {"type": "delay", "ms": 1},
                {"type": "delay", "ms": 2},
                {"type": "notice", "text": "最后一行"},
            ]), script_path)
            app = MacroFlowApp.__new__(MacroFlowApp)
            app.workflow = Workflow(steps=[{"script": str(script_path)}])
            app.workflow_stop = threading.Event()
            app.player = Mock()
            app.player.stop_event = threading.Event()
            app.global_detect_resume_index = 0
            app.global_detect_resume_repeat = 1
            app.global_detect_resume_action = 1
            app.global_detect_jump_current_script_last = True
            app.global_detect_trigger_count = 1
            app.run_workflow = Mock()
            app._log = Mock()

            app._resume_workflow_after_global_module()

        app.run_workflow.assert_called_once_with(
            start_index=0, start_repeat=1, resume_action_index=2,
            preserve_global_rearm_locks=True,
        )

    def test_resume_global_module_can_end_current_repeat_and_continue_next_repeat(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"script": "a", "repeats": 3}, {"script": "b", "repeats": 1},
        ])
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.global_detect_resume_index = 0
        app.global_detect_resume_repeat = 2
        app.global_detect_resume_action = 4
        app.global_detect_end_current_script = True
        app.global_detect_advance_workflow_step = False
        app.global_detect_pending_restart = True
        app.global_detect_trigger_count = 3
        app._consume_workflow_repeat = Mock(
            side_effect=lambda index: app.workflow.steps[index].__setitem__("repeats", 2) or 2,
        )
        app.run_workflow = Mock()
        app._log = Mock()

        app._resume_workflow_after_global_module()

        app._consume_workflow_repeat.assert_called_once_with(0)
        app.run_workflow.assert_called_once_with(
            start_index=0, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )
        self.assertFalse(app.global_detect_end_current_script)
        self.assertFalse(app.global_detect_pending_restart)

    def test_resume_global_module_end_on_last_step_finishes_workflow(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a"}])
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.global_detect_resume_index = 0
        app.global_detect_resume_repeat = 0
        app.global_detect_resume_action = 1
        app.global_detect_end_current_script = True
        app.global_detect_advance_workflow_step = False
        app.global_detect_pending_restart = True
        app.global_detect_trigger_count = 1
        app.workflow_repeats_snapshot = {0: (1, False)}
        app._consume_workflow_repeat = Mock(return_value=0)
        app._stop_all_global_detect_monitors = Mock()
        app._set_status = Mock()
        app._append_mini_step = Mock()
        app._log = Mock()
        app._sound = Mock()
        app._finish_execution_visibility = Mock()
        app.run_workflow = Mock()

        app._resume_workflow_after_global_module()

        app.run_workflow.assert_not_called()
        app._consume_workflow_repeat.assert_called_once_with(0)
        app._stop_all_global_detect_monitors.assert_called_once_with(clear=True)
        app._set_status.assert_called_once_with("工作流执行完成", "success")
        self.assertIsNone(app.workflow_repeats_snapshot)

    def test_resume_global_module_explicit_next_target_advances_workflow(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[
            {"script": "a", "repeats": 3}, {"script": "b", "repeats": 1},
        ])
        app.workflow_stop = threading.Event()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.global_detect_resume_index = 0
        app.global_detect_resume_repeat = 2
        app.global_detect_resume_action = 4
        app.global_detect_end_current_script = False
        app.global_detect_advance_workflow_step = True
        app.global_detect_pending_restart = True
        app.global_detect_trigger_count = 3
        app._consume_workflow_repeat = Mock()
        app.run_workflow = Mock()
        app._log = Mock()

        app._resume_workflow_after_global_module()

        app._consume_workflow_repeat.assert_called_once_with(0)
        app.run_workflow.assert_called_once_with(
            start_index=1, start_repeat=0, resume_action_index=0,
            preserve_global_rearm_locks=True,
        )

    def test_resume_workflow_after_global_module_skips_when_stopped(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.workflow = Workflow(steps=[{"script": "a"}])
        app.workflow_stop = threading.Event()
        app.workflow_stop.set()
        app.player = Mock()
        app.player.stop_event = threading.Event()
        app.run_workflow = Mock()
        app._log = Mock()
        app._resume_workflow_after_global_module()
        app.run_workflow.assert_not_called()

    def test_record_workflow_repeat_stores_index(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.current_workflow_repeat_index = 0
        app._set_execution_progress = Mock()
        app._ui = lambda callback, *args: callback(*args)
        with patch("app.workflow_execution_progress", return_value="p"):
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

    def test_workflow_interrupt_keeps_focus_dispatcher_alive_for_global_click(self):
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
        app.global_detect_pending_restart = True

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

        with patch("app.registered_module_object", return_value={
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
            with patch("app.resolve_path", return_value=script_path):
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
                    "hold_ms": 1500, "region": [10, 20, 30, 40],
                },
            ]
            save_script(MacroScript(name="g", actions=actions), script_path)
            app = MacroFlowApp.__new__(MacroFlowApp)
            with patch("app.resolve_path", return_value=script_path), \
                 patch("app.load_script", return_value=MacroScript(name="g", actions=actions)):
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
        with patch("dialogs.load_template_regions", return_value={
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
        with patch("dialogs.load_template_regions", return_value={}):
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
        with patch("dialogs.show_floating_notice") as notice:
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
        from app import SCRIPT_CATEGORY_VALUES, script_category_key
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
        app._stop_all_global_detect_monitors = Mock()
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
        app._stop_all_global_detect_monitors.assert_called_once_with(clear=True)

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
    def test_show_window_uses_show_without_resizing_normal_window(self):
        with patch("wininput.is_window", return_value=True), \
             patch("wininput.user32.IsIconic", return_value=False), \
             patch("wininput.user32.ShowWindow") as show, \
             patch("wininput.user32.IsWindowVisible", return_value=True):
            self.assertTrue(show_window(123))
        self.assertEqual(show.call_args.args[1], 5)

    def test_show_window_no_activate_preserves_geometry_and_focus(self):
        with patch("wininput.is_window", return_value=True), \
             patch("wininput.user32.ShowWindow") as show, \
             patch("wininput.user32.SetWindowPos", return_value=True) as position:
            self.assertTrue(show_window_no_activate(123))
        self.assertEqual(show.call_args.args[1], 4)
        flags = position.call_args.args[-1]
        self.assertTrue(flags & 0x0001)  # SWP_NOSIZE
        self.assertTrue(flags & 0x0002)  # SWP_NOMOVE
        self.assertTrue(flags & 0x0004)  # SWP_NOZORDER
        self.assertTrue(flags & 0x0010)  # SWP_NOACTIVATE

    def test_mouse_input_falls_back_when_sendinput_returns_zero_without_error(self):
        with patch("wininput.user32.SendInput", return_value=0), \
             patch("wininput.user32.mouse_event") as fallback, \
             patch("wininput.time.sleep"):
            send_move_relative(12, -7)
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[-1], MACROFLOW_INPUT_TAG)

    def test_sendinput_uses_focus_guard_dispatcher_when_installed(self):
        dispatcher = Mock()
        set_input_dispatcher(dispatcher)
        try:
            with patch("wininput.user32.SendInput") as send_input:
                send_move_relative(12, -7)
        finally:
            set_input_dispatcher(None)
        dispatcher.assert_called_once()
        packet = dispatcher.call_args.args[0]
        self.assertEqual((packet.mi.dx, packet.mi.dy), (12, -7))
        self.assertEqual(packet.mi.dwExtraInfo, MACROFLOW_INPUT_TAG)
        send_input.assert_not_called()

    def test_activate_window_does_not_restore_non_minimized_window(self):
        with patch("wininput.is_window", return_value=True), \
             patch("wininput.user32.IsIconic", return_value=False), \
             patch("wininput.user32.ShowWindow") as show, \
             patch("wininput.kernel32.GetCurrentThreadId", return_value=1), \
             patch("wininput.user32.GetWindowThreadProcessId", return_value=1), \
             patch("wininput.user32.BringWindowToTop"), \
             patch("wininput.user32.SetForegroundWindow"), \
             patch("wininput.user32.SetFocus"), \
             patch("wininput.user32.GetForegroundWindow", return_value=123), \
             patch("wininput.time.sleep"):
            self.assertTrue(activate_window(123))
        show.assert_not_called()

    def test_activate_window_restores_only_minimized_window(self):
        with patch("wininput.is_window", return_value=True), \
             patch("wininput.user32.IsIconic", return_value=True), \
             patch("wininput.user32.ShowWindow") as show, \
             patch("wininput.kernel32.GetCurrentThreadId", return_value=1), \
             patch("wininput.user32.GetWindowThreadProcessId", return_value=1), \
             patch("wininput.user32.BringWindowToTop"), \
             patch("wininput.user32.SetForegroundWindow"), \
             patch("wininput.user32.SetFocus"), \
             patch("wininput.user32.GetForegroundWindow", return_value=123), \
             patch("wininput.time.sleep"):
            self.assertTrue(activate_window(123))
        show.assert_called_once()

    def test_force_english_input_changes_layout_and_closes_ime(self):
        layout = 0x04090409
        with patch("wininput.user32.LoadKeyboardLayoutW", return_value=layout) as load, \
             patch("wininput.user32.ActivateKeyboardLayout") as activate, \
             patch("wininput.user32.PostMessageW", return_value=True) as post, \
             patch("wininput.user32.GetWindowThreadProcessId", return_value=77), \
             patch("wininput.user32.GetKeyboardLayout", return_value=layout), \
             patch("wininput.imm32.ImmGetContext", return_value=88), \
             patch("wininput.imm32.ImmSetOpenStatus") as close_ime, \
             patch("wininput.imm32.ImmReleaseContext"), \
             patch("wininput.time.sleep"):
            self.assertTrue(force_english_input(123))
        load.assert_called_once_with("00000409", 1)
        activate.assert_called_once_with(layout, 0)
        post.assert_called_once()
        close_ime.assert_called_once()

    def test_center_lock_uses_window_position_plus_size(self):
        with patch("wininput.user32.GetForegroundWindow", return_value=0), \
             patch("wininput.get_window_rect", return_value=(100, 50, 800, 600)), \
             patch("wininput.get_cursor_pos", return_value=(500, 350)):
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
        with patch("dialogs.ScreenRegionPicker") as picker_class:
            dialog.capture_custom_template()
        picker_class.return_value.start.assert_called_once()
        on_result = picker_class.call_args.args[2]
        with tempfile.TemporaryDirectory() as folder:
            images_dir = Path(folder) / "images"
            screen = np.zeros((40, 50, 3), dtype=np.uint8)
            with patch("dialogs.load_module_images_dir", return_value=images_dir), \
                 patch("dialogs.capture_bgr", return_value=(screen, (10, 20))), \
                 patch("dialogs.registered_template_options", return_value=["captured"]) as options:
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
        with patch("dialogs.ScreenRegionPicker") as picker_class:
            dialog.capture_custom_template()
        on_result = picker_class.call_args.args[2]
        with patch("dialogs.capture_bgr", side_effect=RuntimeError("boom")), \
             patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.activate_main_after_modal"):
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
        with patch("dialogs.activate_main_after_modal"):
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
        with patch("dialogs.load_template_regions", return_value={
            "images/x.png": [10, 20, 30, 40],
        }), patch("dialogs.activate_main_after_modal"):
            dialog.save()
        result = dialog.result
        self.assertEqual(result["region_mode"], "template")
        self.assertEqual(result["region"], [])
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
        with patch("dialogs.load_template_regions", return_value={
            "images/y.png": [100, 200, 300, 400],
        }), patch("dialogs.activate_main_after_modal"):
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
            "jump_row": 4, "workflow_repeat_at_least_2": False, "delay_ms": 0,
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
            with patch("image_match.capture_bgr", return_value=(screen, (10, 20))):
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
            with patch("image_match.capture_bgr", return_value=(screen, (10, 20))):
                match = find_template(template_path, 0.95)
            self.assertIsNotNone(match)
            self.assertEqual((match["x"], match["y"]), (54, 45))

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
        with patch("dialogs.ClickDialog") as dialog_class:
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
        with patch("dialogs.GlobalDetectDialog") as dialog_class:
            dialog_class.return_value.show.return_value = dict(original)
            updated = edit_action(None, original, others)
        self.assertEqual(updated["action_id"], "stable-g")
        dialog_class.assert_called_once_with(None, original, jump=True, actions=others)

    def test_editing_plain_global_detect_keeps_default_dialog_mode(self):
        original = {"type": "global_detect", "action_id": "stable-g",
                    "template": "images/g.png"}
        with patch("dialogs.GlobalDetectDialog") as dialog_class:
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
        with patch("ocr._get_engine", return_value=engine):
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        form.destroy.assert_not_called()


class DetectOverlayTests(unittest.TestCase):
    def test_show_and_hide_overlay_creates_window(self):
        from detect_overlay import hide_overlay, show_overlay
        show_overlay(50, 60, 100, 80)
        show_overlay(60, 70, 120, 90, duration_ms=80)  # 重复调用刷新位置
        hide_overlay()
        show_overlay(70, 80, 10, 10)
        hide_overlay()

    def test_show_overlay_ignores_empty_region(self):
        from detect_overlay import hide_overlay, show_overlay
        show_overlay(0, 0, 0, 0)
        show_overlay(10, 10, -5, 5)
        hide_overlay()


class AlertTests(unittest.TestCase):
    def test_alert_uses_windows_audio_device(self):
        called = threading.Event()

        def capture_audio(data, _flags):
            self.assertTrue(data.startswith(b"RIFF"))
            called.set()

        with patch("alerts.winsound.PlaySound", side_effect=capture_audio):
            play_alert("record_start")
            self.assertTrue(called.wait(1.0))


class PlayerTests(unittest.TestCase):
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.find_template") as find, \
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.recognize_region_with_boxes", return_value=("721", boxes)) as read, \
             patch("player.find_template") as find:
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.recognize_region_with_boxes", return_value=("4", [{"text": "4", "x": 1}])) as read:
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.recognize_region_with_boxes", side_effect=[
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.recognize_region_with_boxes", return_value=("加载", [])):
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
        with patch("player.registered_module_object", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "未设置比较数字"):
                player._execute_image({
                    "type": "image_match", "module_ref": True,
                    "module_key": "module:number", "region_mode": "template",
                }, None)

    def setUp(self):
        # 识别成功时 player 会调用检测框提醒；测试中拦截，避免创建真实窗口。
        patcher = patch("player.show_overlay")
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
        with patch("player.find_template", return_value=None):
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
        with patch("player.find_template", return_value=None):
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
        with patch("player.find_template", return_value=match):
            player.play(actions)
        self.assertEqual(notices, [("找到后跳转成功", 1000)])

    def test_image_found_can_jump_to_one_based_action_row(self):
        notices = []
        player = MacroPlayer(on_notice=lambda text, duration: notices.append((text, duration)))
        match = {
            "x": 10, "y": 20, "width": 30, "height": 40,
            "center_x": 25, "center_y": 40, "score": 0.95,
        }
        with patch("player.find_template", return_value=match):
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
        with patch("player.find_template", return_value=None):
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
        with patch("player.find_template", return_value=None):
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
        with patch("player.find_template", side_effect=[None, None, match]):
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 3 else dict(main_match)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button") as button:
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
            with patch("player.find_template", return_value=match) as find, \
                 patch("player.registered_template_region", return_value=[100, 50, 300, 200]), \
                 patch("player.send_move_absolute"), patch("player.send_button"):
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
            with patch("player.find_template", return_value=match) as find, \
                 patch("player.registered_template_region", return_value=None), \
                 patch("player.send_move_absolute"), patch("player.send_button"):
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
        with patch("player.recognize_region", return_value="体力不足，请补充"):
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

    def test_ocr_hit_any_text_when_expected_empty(self):
        # 期望文字留空：识别到任意文字即命中。
        player = MacroPlayer()
        waits = []
        player._wait = lambda milliseconds: waits.append(milliseconds)
        with patch("player.recognize_region", return_value="随便什么文字"):
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
        with patch("player.recognize_region", return_value="确认购买？"):
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
        with patch("player.recognize_region", return_value=""), \
             patch("player.time.perf_counter", side_effect=[100.0, 100.05, 101.0]):
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
        with patch("player.recognize_region", return_value="没有字"), \
             patch("player.time.perf_counter", side_effect=[100.0, 101.0]):
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
        with patch("player.recognize_region", return_value="没有字"), \
             patch("player.time.perf_counter", side_effect=[100.0, 101.0]):
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
        with patch("player.recognize_region", return_value="") as recognize:
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
        with patch("player.recognize_region", side_effect=lambda _region: results.pop(0)), \
             patch("player.time.perf_counter", side_effect=[100.0, 100.1, 100.2, 100.3]):
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
        with patch("player.recognize_region") as recognize:
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
        with patch("player.recognize_region") as recognize, \
             patch("player.get_window_rect", return_value=(1, 2, 800, 600)):
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 3 else dict(main_match)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button") as button:
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button") as button:
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

            def fake_find(template, threshold, region, ignore_background=False):
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button") as button:
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 3 else dict(main_match)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button"):
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
        with patch("player.send_move_absolute") as move, \
             patch("player.send_button") as button:
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
        with patch("player.send_move_absolute"), \
             patch("player.send_button") as button:
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
        with patch("player.send_move_absolute"), \
             patch("player.send_button") as button:
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                return next(sequence)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button"):
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append((Path(template).name, region))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                main_attempts["count"] += 1
                return None if main_attempts["count"] == 1 else dict(main_match)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute"), \
                 patch("player.send_button"):
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return None
                main_attempts["count"] += 1
                return None if main_attempts["count"] == 1 else dict(main_match)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute"), \
                 patch("player.send_button"):
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

            def fake_find(template, threshold, region, ignore_background=False):
                calls.append(str(template))
                if str(template).endswith("fallback.png"):
                    return dict(fallback_match)
                return None if len(calls) < 2 else dict(main_match)

            with patch("player.find_template", side_effect=fake_find), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button") as button:
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
        # v1.68：普通脚本内嵌全局模块行显示跳转行。
        kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 3, "hold_ms": 1500, "threshold": 0.9,
        })
        self.assertIn("全局模块", kind)
        self.assertIn("触发后跳转到第 3 行", detail)
        self.assertIn("g.png", detail)
        self.assertNotIn("点击", detail)

    def test_global_module_row_summary_resolves_row_object(self):
        # v1.70：跳转目标是行的对象，摘要按动作标识解析到当前行号。
        action_rows = {"target-a": 6, "target-b": 2}
        _kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 1, "jump_action_id": "target-a",
        }, action_rows)
        self.assertIn("触发后跳转到第 6 行", detail)
        # 目标行被删除：明确提示而不是显示旧行号。
        _kind, detail, _delay = action_summary({
            "type": "global_detect", "template": "images/g.png",
            "jump_row": 3, "jump_action_id": "deleted-target",
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
        with patch("player.find_template", return_value=match):
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
        with patch("player.find_template", return_value=match), \
             patch("player.show_overlay"):
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
        with patch("player.find_template", return_value=match) as find, \
             patch("player.show_overlay"):
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
        with patch("player.find_template", return_value=match):
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
        with patch("player.registered_module_object", return_value=module_obj), \
             patch("player.find_template", return_value=None):
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.find_template", return_value=match), \
             patch("player.show_overlay"):
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
        with patch("player.registered_module_object", return_value=module), \
             patch("player.find_template", return_value=None):
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
        with patch("player.registered_module_object", return_value=success_module), \
             patch("player.find_template", return_value=match), \
             patch("player.show_overlay"):
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
        with patch("player.registered_module_object", return_value=failure_module), \
             patch("player.find_template", return_value=None):
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
        with patch("player.send_move_absolute"), patch("player.send_button"):
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
        with patch("player.send_move_absolute") as move, \
             patch("player.send_button") as button:
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
        with patch("player.registered_module_object", return_value=module_obj), \
             patch("player.find_template", return_value=match), \
             patch("player.show_overlay"):
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
        with patch("player.registered_module_object", return_value=module_obj), \
             patch("player.find_template", return_value=match) as find, \
             patch("player.show_overlay"):
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
        with patch("player.registered_module_object", return_value=obj), \
             patch("player.find_template", return_value=None):
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
        with patch("player.registered_module_object", return_value=obj) as lookup, \
             patch("player.find_template", return_value=match) as find:
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
        with patch("player.enum_windows", return_value=[target]), \
             patch("player.activate_window", return_value=True) as activate:
            player._execute_action({
                "type": "activate_window",
                "window": {
                    "title": target.title,
                    "class_name": target.class_name,
                    "process_path": target.process_path,
                },
            }, None, False)
        activate.assert_called_once_with(456)
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
                {"type": "jump", "jump_action_id": SCRIPT_START_TARGET_ID},
                None, False,
            ),
            ("row", 1),
        )
        self.assertEqual(
            player._execute_action(
                {"type": "jump", "jump_action_id": NEXT_WORKFLOW_STEP_TARGET_ID},
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
        with patch("player.find_template", return_value=None):
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
        with patch("player.find_template", return_value=None):
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
            with patch("player.find_template", return_value=None):
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
        with patch("player.find_template", return_value=match):
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
        with patch("player.find_template", return_value=match), \
             patch("player.send_move_absolute") as move, \
             patch("player.send_button"):
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
        with patch("player.find_template", return_value=match):
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
        with patch("player.find_template", return_value=None):
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
        with patch("player.registered_module_object", return_value=module_obj), \
             patch(
                 "player.recognize_region_with_boxes",
                 return_value=("当前体力不足", [found]),
             ) as recognize, \
             patch("player.send_move_absolute") as move, \
             patch("player.send_button"):
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
        with patch("player.registered_module_object", return_value={
            "recognize": "text", "expected_text": "体力不足", "match_mode": "contains",
            "template": "", "region": [], "blocking": False, "interval_ms": 250,
            "threshold": 0.85, "after_action": "continue", "run_code_after_action": False,
        }), \
             patch(
                 "player.recognize_region_with_boxes",
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
        with patch("player.registered_module_object", return_value={
            "name": "奖励可领取", "recognize": "text",
            "expected_text": "可领取", "match_mode": "contains",
            "template": "", "region": [], "blocking": False,
            "interval_ms": 250, "threshold": 0.85,
            "after_action": "continue", "run_code_after_action": False,
        }), patch(
            "player.recognize_region_with_boxes",
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
        with patch("player.registered_module_object", return_value=module_obj), \
             patch(
                 "player.recognize_region_with_boxes",
                 side_effect=[
                     ("加载中", [{"text": "加载中", "center_x": 50, "center_y": 60}]),
                     ("仍在加载中", [{"text": "仍在加载中", "center_x": 80, "center_y": 100}]),
                     ("完成", [{"text": "完成", "center_x": 20, "center_y": 30}]),
                 ],
             ) as recognize, \
             patch("player.send_move_absolute") as move, \
             patch("player.send_button"):
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
        with patch("player.registered_module_object", return_value=module_obj), \
             patch("player.find_template", side_effect=[found, found, None]) as find, \
             patch("player.show_overlay"), \
             patch("player.send_move_absolute") as move, \
             patch("player.send_button"):
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
        with patch("player.registered_module_object", return_value={
            "recognize": "text", "expected_text": "体力不足", "match_mode": "contains",
            "template": "", "region": [], "blocking": True, "interval_ms": 250,
            "threshold": 0.85, "after_action": "continue", "run_code_after_action": False,
            "run_code_on_timeout": True, "not_found_timeout_ms": 0,
            "on_timeout_actions": segment,
        }), \
             patch(
                 "player.recognize_region_with_boxes",
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

    def test_activation_window_runs_once_then_target_is_raised(self):
        player = MacroPlayer()
        with patch("player.is_window", return_value=True), \
             patch("player.activate_window", return_value=True) as activate:
            player.play(
                [{"type": "comment"}], hwnd=123,
                activation_hwnd=456, activate_target=True,
            )
        self.assertEqual(activate.call_args_list, [call(456), call(123)])

    def test_explicit_activation_window_is_raised_when_target_activation_is_off(self):
        player = MacroPlayer()
        with patch("player.is_window", return_value=True), \
             patch("player.activate_window", return_value=True) as activate:
            player.play(
                [{"type": "comment"}], hwnd=123,
                activation_hwnd=456, activate_target=False,
            )
        activate.assert_called_once_with(456)

    def test_disabled_auto_activation_does_not_raise_target_window(self):
        player = MacroPlayer()
        with patch("player.is_window", return_value=True), \
             patch("player.is_window_process_foreground", return_value=False), \
             patch("player.activate_window") as activate:
            player.play([{"type": "comment"}], hwnd=123, activate_target=False)
        activate.assert_not_called()

    def test_stale_bound_window_does_not_stop_ordinary_actions(self):
        logs = []
        player = MacroPlayer(on_log=logs.append)
        with patch("player.is_window", return_value=False), \
             patch("player.activate_window") as activate, \
             patch("player.send_move_absolute") as move:
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
        with patch("player.is_window", return_value=False), \
             patch("player.send_move_relative") as move:
            player.play([
                {"type": "mouse_move", "mode": "relative", "dx": 2, "dy": 3},
            ], hwnd=123)
        move.assert_called_once_with(2, 3)

    def test_relative_action_resolves_game_window_created_after_workflow_start(self):
        player = MacroPlayer(on_target_window_request=Mock(return_value=456))
        with patch("player.is_window", side_effect=lambda hwnd: hwnd == 456), \
             patch("player.activate_window", return_value=True) as activate, \
             patch("player.send_move_relative") as move:
            player.play([
                {"type": "mouse_move", "mode": "relative", "dx": 2, "dy": 3},
            ], hwnd=None)
        player.on_target_window_request.assert_called_once_with()
        activate.assert_called_once_with(456)
        move.assert_called_once_with(2, 3)

    def test_relative_move_sends_when_auto_activation_off_and_not_foreground(self):
        # 关闭自动前置且目标窗口不在前台：仅提示，仍直接发送相对移动。
        player = MacroPlayer()
        with patch("player.is_window", return_value=True), \
             patch("player.is_window_process_foreground", return_value=False), \
             patch("player.activate_window") as activate, \
             patch("player.send_move_relative") as move:
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
        with patch("player.get_virtual_screen_rect", return_value=target), \
             patch("player.send_move_absolute") as move:
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
            with patch("player.registered_template_region", return_value=[5, 6, 70, 80]), \
                 patch("player.find_template", return_value=second) as find, \
                 patch("player.show_overlay"), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button"):
                player._execute_second_match(obj, None, first)
            find.assert_called_once_with(second_path, 0.85, (5, 6, 70, 80), ignore_background=False)
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
            with patch("player.find_template", return_value=second), \
                 patch("player.show_overlay"), \
                 patch("player.send_move_absolute") as move, \
                 patch("player.send_button"):
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
        with patch("player.get_cursor_pos", return_value=(777, 888)) as cursor, \
             patch("player.send_move_absolute") as move, \
             patch("player.send_button") as button:
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
        with patch("player.send_move_absolute") as move, \
             patch("player.send_button") as button:
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
        with patch("player.is_window", return_value=True), \
             patch("player.activate_window", return_value=True), \
             patch("player.send_move_relative") as send_relative:
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
            with patch("player.os.startfile") as startfile:
                player.play([{"type": "open_app", "path": str(exe)}])
            startfile.assert_called_once_with(str(exe), arguments="")

    def test_open_app_action_launches_with_arguments(self):
        player = MacroPlayer()
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as folder:
            exe = Path(folder) / "app.exe"
            exe.write_bytes(b"MZ")
            with patch("player.os.startfile") as startfile:
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
        with patch("player.is_process_running", return_value=False), \
             patch("player.taskkill_process") as taskkill:
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
        with patch("player.is_process_running", side_effect=[True, True, True, True, False]), \
             patch("player.taskkill_process", side_effect=[(0, ""), (0, "")]) as taskkill:
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
        with patch("player.is_process_running", side_effect=[True, True, False]), \
             patch("player.taskkill_process", side_effect=[(0, "")]) as taskkill:
            player._execute_close_app({
                "type": "close_app", "name": "demo.exe",
                "graceful": False, "graceful_wait_ms": 2000,
            })
        taskkill.assert_called_once_with("demo.exe", force=True, tree=False)

    def test_close_app_graceful_success(self):
        player = MacroPlayer()
        with patch("player.is_process_running", side_effect=[True, True, False, False]), \
             patch("player.taskkill_process", side_effect=[(0, "")]) as taskkill:
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
        with patch("player.is_process_running", side_effect=[True, True, False]), \
             patch("player.taskkill_process", side_effect=[(1, "拒绝访问"), (0, "")]) as taskkill:
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
        with patch("player.is_process_running", side_effect=[True, True, False]), \
             patch("player.taskkill_process", return_value=(1, "拒绝访问")), \
             patch("player.elevated_taskkill", return_value=True) as elev:
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
        with patch("player.is_process_running", return_value=True), \
             patch("player.taskkill_process", return_value=(1, "拒绝访问")), \
             patch("player.elevated_taskkill", return_value=False) as elev:
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
            path = Path(folder) / "b.json"
            path.write_text(json.dumps({"name": "B", "actions": []}, ensure_ascii=False),
                            encoding="utf-8")
            with patch("app.load_script", return_value=MacroScript(name="B")):
                app.load_script_into_editor(path)
        self.assertEqual(app.undo_open_stack, [])
        self.assertEqual(app.script.name, "b")

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
            with patch("app.subprocess.Popen") as popen:
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
        with patch("app.subprocess.Popen") as popen:
            app.open_referenced_script_in_new_window(
                {"type": "script_ref", "script": "C:/no_such_dir/ref.json"})
        popen.assert_not_called()
        app._notify.assert_called_once_with("引用脚本不存在", "找不到文件：C:/no_such_dir/ref.json")

    def test_open_referenced_script_empty_path_notifies(self):
        app = self._app()
        with patch("app.subprocess.Popen") as popen:
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
        with patch("app.tk.Menu") as menu_class:
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
        with patch("app.tk.Menu") as menu_class:
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
        with patch("app.tk.Menu") as menu_class:
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
        with patch("app.tk.Menu") as menu_class:
            app._show_workflow_context_menu(Mock())
        menu_class.assert_not_called()

    def test_workflow_context_menu_skipped_outside_row(self):
        app = self._app()
        app.workflow_tree = Mock()
        app.workflow_tree.identify_row.return_value = ""
        with patch("app.tk.Menu") as menu_class:
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
        with patch("app.threading.Thread") as thread_class:
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

    def test_run_workflow_script_alone_missing_activation_window_notifies(self):
        app = self._test_app_for_workflow_script_alone()
        app._execution_activation_hwnd = Mock(
            side_effect=RuntimeError("脚本的前置窗口当前未打开，请重新选择或取消勾选。"))
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
            app.run_workflow_script_alone({"script": str(ref)})
        app._execution_activation_hwnd.assert_called_once_with(
            123, True, {"title": "游戏窗口", "class_name": "", "process_path": ""})
        app._notify.assert_called_once_with(
            "无法执行", "脚本的前置窗口当前未打开，请重新选择或取消勾选。")
        app.worker.start.assert_not_called()

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
            app.workflow_repeats_snapshot = None
            app._stop_all_global_detect_monitors = Mock(side_effect=RuntimeError("startup continued"))

            with self.assertRaisesRegex(RuntimeError, "startup continued"):
                app.run_workflow()

            self.assertTrue(any("前置窗口已跳过" in call.args[0] for call in app._log.call_args_list))
            app._notify.assert_not_called()

    def test_choose_activation_window_writes_script_settings(self):
        app = self._app()
        selected = Mock()
        selected.title = "新前置窗口"
        selected.class_name = "NewFront"
        selected.process_path = "C:/Game/new.exe"
        selected.label = "新前置窗口（NewFront）"
        app.root = Mock()
        with patch("app.WindowPicker") as picker, patch("app.is_window", return_value=True):
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


class FocusModeTests(unittest.TestCase):
    def test_disabled_focus_only_switches_english(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.input_guard = Mock()
        app._ui = lambda callback, *args: callback(*args)
        app._log = Mock()
        with patch("app.force_english_input", return_value=True):
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
        with patch("input_guard.user32.SetWindowsHookExW", return_value=1), \
             patch("input_guard.user32.GetMessageW", side_effect=fake_get_message), \
             patch("input_guard.user32.PostThreadMessageW", side_effect=fake_post), \
             patch("input_guard.user32.UnhookWindowsHookEx"), \
             patch("input_guard.user32.BlockInput", return_value=True) as block_input:
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
        with patch("app.force_english_input", side_effect=lambda _hwnd: order.append("english") or True):
            app._enter_focus_mode(123)
        self.assertEqual(order, ["english", "guard", "block"])

    def test_focus_mode_failure_stops_hook(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.input_guard = Mock()
        app.input_guard.start.return_value = True
        app.input_guard.block.return_value = False
        with patch("app.force_english_input", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "管理员身份"):
                app._enter_focus_mode(123)
        app.input_guard.stop.assert_called_once()


class KeyCaptureTests(unittest.TestCase):
    """KeyCapturer hook + KeyActionDialog capture flow."""

    def _install_capturer(self, events, release):
        captured = {}

        def fake_set_hook(_kind, proc, _inst, _tid):
            captured["proc"] = proc
            return ctypes.c_void_p(1)

        def fake_get_message(*_):
            release.wait(3)
            return 0

        patch_set = patch("input_guard.user32.SetWindowsHookExW", side_effect=fake_set_hook)
        patch_get = patch("input_guard.user32.GetMessageW", side_effect=fake_get_message)
        patch_post = patch("input_guard.user32.PostThreadMessageW")
        patch_next = patch("input_guard.user32.CallNextHookEx", return_value=0)
        patch_unhook = patch("input_guard.user32.UnhookWindowsHookEx")
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
        with patch("input_guard.user32.SetWindowsHookExW", return_value=0) as set_hook, \
             patch("input_guard.user32.GetMessageW") as get_msg, \
             patch("input_guard.user32.UnhookWindowsHookEx"):
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
        with patch("dialogs.KeyCapturer") as capturer_class:
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
        with patch("dialogs.KeyCapturer") as capturer_class, \
             patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.ModalDialog.destroy", create=True) as base_destroy:
            dialog.destroy()
        capturer.stop.assert_called_once()
        self.assertIsNone(dialog.capturer)
        base_destroy.assert_called_once()


class TemplateRegionTests(unittest.TestCase):
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
        with patch("dialogs.save_module_objects") as save, \
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
                save_template_regions({"images/a.png": [10, 20, 300, 400]})
                loaded = load_template_regions()
            self.assertEqual(loaded, {"images/a.png": [10, 20, 300, 400]})

    def test_module_enabled_state_roundtrips_and_defaults_to_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
                loaded = load_template_regions()
            # 未设置区域（全 0）的占位条目合法保留，格式错误的丢弃。
            self.assertEqual(loaded, {
                "images/good.png": [1, 2, 3, 4],
                "images/unset.png": [0, 0, 0, 0],
            })

    def test_unset_template_region_placeholder_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
                obj = load_module_objects()["images/timeout.png"]
        self.assertTrue(obj["run_code_on_timeout"])
        self.assertEqual(obj["not_found_timeout_ms"], 4500)
        self.assertEqual(obj["on_timeout_actions"][0]["type"], "delay")
        self.assertEqual(obj["on_success_actions"], [])

    def test_load_module_objects_seeds_default_pure_action_special(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "template_regions.json"
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
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
            with patch("storage.TEMPLATE_REGIONS_PATH", path):
                regions = load_template_regions()
            self.assertEqual(regions, {"images/s.png": [1, 2, 3, 4]})

    def test_load_template_regions_skips_no_recognition_modules(self):
        with patch("storage.load_module_objects", return_value={
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
        with patch("storage.load_template_regions", return_value={key: [10, 20, 300, 400]}):
            self.assertEqual(registered_template_region("images/a.png"), [10, 20, 300, 400])
        with patch("storage.load_template_regions", return_value={}):
            self.assertIsNone(registered_template_region("images/missing.png"))

    def test_registered_template_options_include_legacy_value(self):
        with patch("dialogs.load_template_regions", return_value={"images/b.png": [1, 2, 3, 4]}):
            self.assertEqual(registered_template_options(), ["images/b.png"])
            # 编辑旧动作：模板不在注册表时临时加回，保证下拉显示原值。
            self.assertEqual(
                registered_template_options("images/legacy.png"),
                ["images/legacy.png", "images/b.png"],
            )

    def test_fallback_template_options_has_disabled_first(self):
        with patch("dialogs.load_template_regions", return_value={"images/b.png": [1, 2, 3, 4]}):
            self.assertEqual(
                fallback_template_options("images/legacy.png"),
                ["（不启用）", "images/legacy.png", "images/b.png"],
            )
            self.assertEqual(fallback_template_options(""), ["（不启用）", "images/b.png"])

    def test_open_template_region_manager_shows_and_refreshes(self):
        # 打开管理器（show）后刷新模板下拉；管理器里已删除的模板被清空。
        with patch("dialogs.TemplateRegionManagerDialog") as manager_class, \
             patch("dialogs.load_template_regions", return_value={"images/g.png": [1, 2, 3, 4]}):
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
        with patch("dialogs.load_template_regions", return_value={"images/g.png": [1, 2, 3, 4]}):
            dialog._refresh_template_options()
        dialog.template.set.assert_called_once_with("")
        dialog.template_combo.configure.assert_called_once_with(values=["images/g.png"])

    def test_editor_page_opens_unified_region_manager(self):
        app = MacroFlowApp.__new__(MacroFlowApp)
        app.root = Mock()
        with patch("app.TemplateRegionManagerDialog") as manager_class:
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
        with patch("dialogs.registered_module_object", return_value={"blocking": True}):
            self.assertTrue(segment_action_is_blocking(action))
            self.assertEqual(segment_row_label(action), "【阻塞等待】识图 wait")

    def test_segment_text_absent_module_is_also_marked_blocking(self):
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:text", "template": "",
        }
        with patch("dialogs.registered_module_object", return_value={
            "blocking": False, "recognize": "text", "wait_text_absent": True,
        }):
            self.assertTrue(segment_action_is_blocking(action))

    def test_segment_nonblocking_module_has_no_warning_marker(self):
        action = {
            "type": "image_match", "module_ref": True,
            "module_key": "module:normal", "template": "images/next.png",
        }
        with patch("dialogs.registered_module_object", return_value={"blocking": False}):
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

        with patch("dialogs.registered_module_object", side_effect=lookup):
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
        form.blocking_var = Mock()
        form.blocking_var.get.return_value = False
        form.hold_enabled_var = Mock()
        form.hold_enabled_var.get.return_value = True
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
        with patch("dialogs.ScreenRegionPicker") as picker_class:
            form._capture()
        on_result = picker_class.call_args[0][2]
        self.assertEqual(picker_class.return_value.start.call_count, 1)
        with tempfile.TemporaryDirectory() as folder:
            images_dir = Path(folder) / "images"
            form.images_dir = images_dir
            screen = np.zeros((40, 50, 3), dtype=np.uint8)
            with patch("dialogs.capture_bgr", return_value=(screen, (0, 0))):
                on_result([10, 20, 30, 40])
            saved = list(images_dir.glob("template_*.png"))
            self.assertEqual(len(saved), 1)
            key = str(saved[0])
        form.image_var.set.assert_called_once_with(key)
        form.region_var.set.assert_called_once_with("10,20,30,40")

    def test_form_capture_failure_shows_notice(self):
        form = self._form()
        form.master = Mock()
        with patch("dialogs.ScreenRegionPicker") as picker_class:
            form._capture()
        on_result = picker_class.call_args[0][2]
        with patch("dialogs.capture_bgr", side_effect=RuntimeError("boom")), \
             patch("dialogs.show_floating_notice") as notice:
            on_result([10, 20, 30, 40])
        notice.assert_called_once()
        self.assertIn("截图失败", notice.call_args.args[1])
        form.image_var.set.assert_not_called()
        form.region_var.set.assert_not_called()

    def test_form_choose_image_sets_image_var(self):
        form = self._form()
        chosen = r"C:\images\部分\勇士挑战确定.png"
        with patch("dialogs.filedialog.askopenfilename", return_value=chosen):
            form._choose_image()
        form.image_var.set.assert_called_once_with(chosen)
        form.name_var.set.assert_called_once_with("勇士挑战确定")

    def test_form_choose_image_preserves_custom_name(self):
        form = self._form()
        form.name_var.get.return_value = "手动名称"
        with patch("dialogs.filedialog.askopenfilename", return_value=r"C:\images\new.png"):
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
        with patch("dialogs.ScreenRegionPicker") as picker_class:
            form._pick_region()
        on_result = picker_class.call_args[0][2]
        on_result([1, 2, 3, 4])
        form.region_var.set.assert_called_once_with("1,2,3,4")

    def test_form_save_requires_image(self):
        form = self._form(image="", region="10,20,300,400")
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少模板图片", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_requires_region(self):
        form = self._form(image="images/g.png", region="")
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少框选区域", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_rejects_malformed_region(self):
        for region in ("1,2,3", "x,y,w,h", "1,2,-3,4"):
            form = self._form(image="images/g.png", region=region)
            with patch("dialogs.show_floating_notice") as notice:
                form.save()
            notice.assert_called_once()
            form.destroy.assert_not_called()

    def test_form_save_sets_result_and_closes(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        with patch("dialogs.show_floating_notice") as notice:
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
        self.assertTrue(obj["hold_enabled"])
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

        with patch("dialogs.show_floating_notice") as notice:
            form.save()

        self.assertFalse(form.result[2]["hold_enabled"])
        self.assertEqual(form.result[2]["hold_ms"], 1000)
        notice.assert_not_called()

    def test_form_saves_start_delay_only_for_script_global_module(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "脚本全局模块"
        form.start_delay_var.get.return_value = "125000"
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["start_delay_ms"], 125000)
        notice.assert_not_called()

    def test_form_saves_restart_target_row_for_global_module(self):
        # 工作流全局模块：选中的行对象写入模块对象的 restart_workflow_target_row。
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "工作流全局模块"
        form.restart_target_var = Mock()
        form.restart_target_var.get.return_value = "第 3 行 · 脚本b"
        form.restart_target_ids = {"（使用全局默认：第 1 行）": 0, "第 3 行 · 脚本b": 3}
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["restart_workflow_target_row"], 3)
        notice.assert_not_called()

    def test_form_saves_restart_default_row_for_script_global_module(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "脚本全局模块"
        form.restart_target_var = Mock()
        form.restart_target_var.get.return_value = "（使用全局默认：第 2 行）"
        form.restart_target_ids = {"（使用全局默认：第 2 行）": 0}
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["restart_workflow_target_row"], 0)
        notice.assert_not_called()

    def test_form_save_restart_custom_row_requires_number(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.category_var.get.return_value = "脚本全局模块"
        form.restart_target_var = Mock()
        form.restart_target_var.get.return_value = "自定义行号…"
        form.restart_target_spin_var = Mock()
        form.restart_target_spin_var.get.return_value = "abc"
        form.restart_target_ids = {}
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("行号格式错误", notice.call_args.args[1])
        form.destroy.assert_not_called()

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
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["fallback_module_key"], "module:fallback")
        self.assertTrue(form.result[2]["fallback_click"])
        notice.assert_not_called()

    def test_form_save_click_custom_requires_point(self):
        form = self._form(
            image="images/g.png", region="10,20,300,400",
            after_action="点击自定义位置",
        )
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        form.destroy.assert_not_called()
        form.click_point_var.get.return_value = "120,340"
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["after_action"], "click_custom")
        self.assertEqual(form.result[2]["click_point"], [120, 340])
        form.destroy.assert_called_once()

    def test_form_save_second_match_requires_template(self):
        form = self._form(
            image="images/g.png", region="10,20,300,400",
            after_action="二次识别后点击",
        )
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertIn("缺少自定义点击区域", notice.call_args.args[1])
        form.destroy.assert_not_called()

        form.second_click_region_var.get.return_value = "100,200,80,40"
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        obj = form.result[2]
        self.assertEqual(obj["second_match_click_target"], "custom_region")
        self.assertEqual(obj["second_match_click_region"], [100, 200, 80, 40])
        form.destroy.assert_called_once()

    def test_form_save_enabled_post_action_code_requires_segment(self):
        form = self._form(image="images/g.png", region="10,20,300,400")
        form.run_code_after_action_var.get.return_value = True
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.WindowPicker") as picker:
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
        with patch("dialogs.tk.Menu", return_value=menu):
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
        with patch("dialogs.ScreenPointPicker") as picker:
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
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        notice.assert_called_once()
        self.assertIn("缺少名称", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_pure_action_sets_result(self):
        # 特殊模块纯动作保存：名称做 key，对象只有 category/name/pure_action。
        form = self._form(image="")
        form.category_var.get.return_value = "特殊模块"
        form.name_var.get.return_value = "重新执行工作流"
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertEqual(form.result[2]["category"], "workflow_global")
        self.assertEqual(form.result[2]["region"], [10, 20, 300, 400])
        self.assertNotIn("pure_action", form.result[2])
        form.destroy.assert_called_once()
        notice.assert_not_called()

    def test_form_save_text_mode_allows_no_image_or_region(self):
        # 识别文字方式：不需要模板图片，区域可留空（空=全屏），名称缺省"识别文字"。
        form = self._form(recognize="识别文字")
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertIn("缺少框选区域", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_number_mode_rejects_global_category(self):
        form = self._form(region="10,20,80,30", recognize="读取数字")
        form.category_var.get.return_value = "工作流全局模块"
        with patch("dialogs.show_floating_notice") as notice:
            form.save()
        self.assertIn("类别不适用", notice.call_args.args[1])
        form.destroy.assert_not_called()

    def test_form_save_no_recognition_mode_runs_directly_without_image_or_region(self):
        form = self._form(recognize="无需识图")
        form.run_code_after_action_var.get.return_value = True
        form.segment = [{"type": "delay", "ms": 25}]
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("dialogs.update_module_object", return_value={"images/g.png": obj}) as save, \
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
        with patch("dialogs.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("dialogs.update_module_object", return_value={"images/b.png": obj}) as save, \
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
        with patch("dialogs.TemplateRegionFormDialog") as form_class, \
             patch("dialogs.update_module_object") as save, \
             patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.TemplateRegionFormDialog", return_value=form), \
             patch("dialogs.update_module_object") as save:
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
        with patch("dialogs.TemplateRegionFormDialog", return_value=form), \
             patch("dialogs.update_module_object", return_value={"images/a.png": obj}) as save, \
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
        with patch("dialogs.TemplateRegionFormDialog") as form_class, \
             patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.save_module_objects") as save, \
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
        with patch("dialogs.save_module_objects") as save, \
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
        with patch("dialogs.save_module_objects") as save:
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
        with patch("dialogs.save_module_objects") as save:
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
        with patch("dialogs.uuid.uuid4") as uuid4, \
             patch("dialogs.save_module_objects") as save, \
             patch("dialogs.show_floating_notice"), \
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

    def test_manager_moves_workflow_global_to_script_global_with_same_id(self):
        dialog = TemplateRegionManagerDialog.__new__(TemplateRegionManagerDialog)
        dialog.objects = {
            "module:source": self._object(category="workflow_global", name="全局"),
        }
        with patch("dialogs.save_module_objects") as save, \
             patch("dialogs.show_floating_notice"), \
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
        with patch("dialogs.tk.Menu") as menu_class:
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
                "dialogs.registered_module_object",
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
            "region": [], "delay_ms": 0,
            "on_found": "continue", "on_timeout": "continue",
        })
        picker.destroy.assert_called_once()

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
        with patch("dialogs.show_floating_notice") as notice:
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
            "region": [], "delay_ms": 0,
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.TemplateRegionFormDialog", return_value=form) as form_class, \
             patch("dialogs.update_module_object") as save, \
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
        with patch("dialogs.ModulePickerDialog") as picker_class:
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
        with patch("dialogs.TemplateRegionFormDialog") as form_class, \
             patch("dialogs.update_module_object") as save, \
             patch("dialogs.ModuleReferenceDelayDialog") as delay_dialog:
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
        with patch("dialogs.TemplateRegionFormDialog") as form_class, \
             patch("dialogs.ModuleReferenceDelayDialog") as delay_dialog:
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
        with patch("dialogs.show_floating_notice") as notice:
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
        with patch("dialogs.ModulePickerDialog") as picker_class, \
             patch("dialogs.registered_module_object", return_value={"name": "新模块"}):
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
        with patch("dialogs.RestartWorkflowTargetDialog", return_value=dialog) as dialog_class:
            updated = edit_action(None, {"type": "restart_workflow"})
        dialog_class.assert_called_once()
        self.assertEqual(updated["restart_workflow_target_row"], 5)
        self.assertEqual(updated["type"], "restart_workflow")

    def test_edit_action_restart_workflow_cancel_keeps_original(self):
        dialog = Mock()
        dialog.show.return_value = None
        with patch("dialogs.RestartWorkflowTargetDialog", return_value=dialog):
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
        with patch("recorder.is_window_process_foreground", return_value=False):
            recorder._on_move(10, 20)
            self.assertEqual(recorder.actions[-1]["mode"], "absolute")
            recorder._on_raw_move(4, 5)
        with patch("recorder.is_window_process_foreground", return_value=True):
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
        with patch("recorder.is_window_process_foreground", return_value=True), \
             patch("recorder.is_cursor_near_window_center", return_value=True):
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
