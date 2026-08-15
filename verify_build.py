"""Post-build verification for MacroFlowStudio.exe via CArchiveReader.

Never executes extracted code — only inspects co_names / co_consts.
"""
import marshal
import os
import struct
import sys
import zlib

sys.path.insert(0, ".deps")
import pefile  # noqa: E402
from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

EXE = sys.argv[1] if len(sys.argv) > 1 else "dist/MacroFlowStudio.exe"
EXPECT_VERSION = "1.0.0"
EXPECT_SYMBOLS = {
    "app": ["open_template_region_manager", "add_module", "add_jump", "_default_global_jump",
            "_on_restart_workflow_request", "_poll_workflow_stop_for_restart_workflow",
            "_launch_workflow_restart", "_poll_second_match_click",
            "_ensure_global_click_foreground",
            "_resume_workflow_after_global_module",
            "_write_log_line", "_read_workflow_start_delay",
            "_workflow_global_module_registry_state",
            "redo_action_edit", "_update_redo_button", "_update_action_edit_button",
            "_select_all_actions",
            "_toggle_target_activation", "undo_delete_workflow_step",
            "undo_delete_global_module", "_update_workflow_delete_undo_buttons",
            "_select_all_workflow_steps", "_select_all_global_modules",
            "_wait_workflow_global_scan_turn", "_finish_workflow_global_scan_turn",
            "_workflow_global_match",
            "add_workflow_global_module",
            "_global_ocr_match_data", "recognize_region_with_boxes",
            "_run_timed_backup", "_run_configured_startup_workflow",
            "_sync_windows_startup"],
    "dialogs": ["TemplateRegionManagerDialog", "TemplateRegionFormDialog",
                "ScreenOffsetPicker",
                "ModulePickerDialog", "BatchModuleScriptDialog", "JumpActionDialog",
                "ModuleReferenceDelayDialog",
                "fit_window_to_content",
                "segment_action_is_blocking", "segment_row_label", "module_manager_label",
                "module_manager_tag", "_toggle_selected_enabled",
                "module_manager_selection_colors", "_update_selection_highlight",
                "configure_module_tree_styles",
                "registered_template_options", "fallback_template_options",
                "open_template_region_manager", "_open_add", "_open_edit",
                "_open_form", "_remove_selected", "_update_action_buttons",
                 "_undo_remove", "_update_undo_button", "_fit_window_to_content",
                 "_labeled_row", "_section_heading", "_entry_button_row", "_row_combo",
                 "_choose_images_dir", "_refresh_inventory", "_open_inventory_item",
                 "_set_inventory_filter", "_set_sort_direction", "_toggle_sort_direction",
                 "_apply_sort_heading", "pinyin_sort_key",
                 "SECOND_MATCH_CLICK_TARGET_LABELS", "_pick_second_click_region",
                 "image_found_jump_target_options",
                 "capture_custom_template", "configured_script_files",
                 "prepend_module_to_scripts", "script_category_for_path",
                 "_batch_add_selected", "_show_module_context_menu",
                 "_change_global_module_category", "_set_filter", "_visible_indices",
                 "_select_all_segment_items",
                 "_select_all_category", "_action_for_key",
                 "_toggle_sections", "recognize_var", "expected_text_var",
                 "match_mode_var", "recognize_combo"],
    "storage": ["TEMPLATE_REGIONS_PATH", "load_module_objects",
                "save_module_objects", "registered_module_object",
                "module_objects_by_category", "update_module_object",
                 "load_template_regions", "save_template_regions",
                 "registered_template_region", "load_module_images_dir",
                 "save_module_images_dir", "module_image_inventory"],
    "player": ["registered_module_object", "on_restart_workflow_request",
               "_execute_second_match", "AdvanceToNextWorkflowStep",
               "_ocr_match_data", "recognize_region_with_boxes", "matches_expected"],
    "models": ["NEXT_WORKFLOW_STEP_TARGET_ID", "SCRIPT_START_TARGET_ID"],
    "image_match": ["find_template", "find_template_in_image",
                    "_estimate_background_color", "_build_ignore_background_mask",
                    "_match_with_mask"],
    "ocr": ["recognize_region", "recognize_image", "recognize_image_with_boxes",
            "recognize_region_with_boxes", "find_expected_match", "matches_expected",
            "format_ocr_observation", "extract_ocr_integer",
            "_get_engine", "_models_root", "MODEL_DIRS"],
    "input_guard": ["BlockInput", "WM_MACROFLOW_INPUT", "_dispatch_input",
                    "_drain_input_requests"],
    "wininput": ["set_input_dispatcher", "_send_input_direct",
                 "MACROFLOW_INPUT_TAG"],
}
PYZ_ITEM_MODULE = 0
PYZ_ITEM_PKG = 1
ERRORS = []


def names_of(code):
    yield from code.co_names
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            yield from names_of(const)


def literals_of(code):
    """Recursively yield literal constants (strings / numbers) embedded in code."""
    for const in code.co_consts:
        if isinstance(const, (str, int, float)):
            yield const
        elif isinstance(const, (tuple, list, frozenset)):
            for item in const:
                if isinstance(item, (str, int, float)):
                    yield item
        elif hasattr(const, "co_names"):
            yield from literals_of(const)


def unmarshal(obj):
    return marshal.loads(obj) if isinstance(obj, bytes) else obj


def read_pyz(data: bytes) -> dict:
    """Parse an in-memory PYZ archive (mirror of ZlibArchiveReader)."""
    if data[:4] != b"PYZ\0":
        raise ValueError("PYZ magic mismatch")
    toc_offset = struct.unpack("!i", data[8:12])[0]
    return dict(marshal.loads(data[toc_offset:]))


def extract_pyz_entry(data: bytes, entry) -> bytes:
    _typecode, offset, length = entry
    return zlib.decompress(data[offset:offset + length])


archive = CArchiveReader(EXE)
image = pefile.PE(EXE, fast_load=True)
if image.OPTIONAL_HEADER.Subsystem != pefile.SUBSYSTEM_TYPE["IMAGE_SUBSYSTEM_WINDOWS_GUI"]:
    ERRORS.append("EXE 不是 Windows GUI 子系统")
image.parse_data_directories([
    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
])
for entry in getattr(image, "DIRECTORY_ENTRY_IMPORT", []):
    if entry.dll.lower() == b"comctl32.dll" and any(
        item.ordinal == 380 for item in entry.imports
    ):
        ERRORS.append("EXE 仍导入 COMCTL32 序数 380")
image.close()
# 版本号：主模块在 CArchive 根目录。
app_code = unmarshal(archive.extract("app"))
if EXPECT_VERSION not in list(literals_of(app_code)):
    ERRORS.append(f"app 缺少 APP_VERSION 常量 {EXPECT_VERSION}")

# 其余模块在 PYZ.pyz 里。
pyz_data = archive.extract("PYZ.pyz")
if isinstance(pyz_data, tuple):
    pyz_data = pyz_data[0]
pyz_toc = read_pyz(pyz_data)
if "pypinyin" not in pyz_toc:
    ERRORS.append("缺少拼音排序依赖 pypinyin")
# v1.87.0 起 OCR 引擎外置：exe 不应再包含 paddle，改为检查 exe 同目录的
# paddle_ocr/（由 build.ps1 复制），首次使用 OCR 时按需加载。
for paddle_module in ("paddle", "paddleocr", "paddlex"):
    if paddle_module in pyz_toc:
        ERRORS.append(f"OCR 引擎未外置：exe 仍包含 {paddle_module}")
exe_dir = os.path.dirname(os.path.abspath(EXE))
ocr_root = os.path.join(exe_dir, "paddle_ocr")
if not os.path.isdir(ocr_root):
    ERRORS.append(f"缺少外置 OCR 组件目录 {ocr_root}")
else:
    for model_dir in ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec", "PP-LCNet_x1_0_textline_ori"):
        marker = os.path.join(ocr_root, "paddle_models", model_dir, "inference.pdiparams")
        if not os.path.isfile(marker):
            ERRORS.append(f"缺少 OCR 模型 {model_dir}/inference.pdiparams")
    if not os.path.isfile(os.path.join(ocr_root, "paddle", "__init__.py")):
        ERRORS.append(f"缺少 OCR 引擎 paddle 包（{ocr_root}）")
    # v1.87.1 起：paddleocr 运行时依赖（colorlog 等）与 paddlex 的
    # importlib.metadata 检查项必须随外置目录一并复制，否则 OCR 报
    # "缺少 PaddleOCR 依赖 (No module named 'colorlog')"。
    for rel, desc in {
        "colorlog/__init__.py": "colorlog",
        "setuptools/__init__.py": "setuptools",
        "_distutils_hack/__init__.py": "_distutils_hack",
        "google/protobuf/__init__.py": "google/protobuf",
        "paddle/_typing/__init__.py": "paddle/_typing（运行时依赖，不能删）",
        "pandas.libs": "pandas.libs",
        "shapely.libs": "shapely.libs",
        "paddlepaddle-3.3.1.dist-info": "paddlepaddle 元数据",
        "paddlex-3.7.2.dist-info": "paddlex 元数据",
        "paddleocr-3.7.0.dist-info": "paddleocr 元数据",
        "pyclipper-1.4.0.dist-info": "pyclipper 元数据(ocr-core)",
        "python_bidi-0.6.11.dist-info": "python-bidi 元数据(ocr-core)",
        "shapely-2.1.2.dist-info": "shapely 元数据(ocr-core)",
        "pypdfium2-5.12.1.dist-info": "pypdfium2 元数据(ocr-core)",
    }.items():
        if not os.path.exists(os.path.join(ocr_root, rel)):
            ERRORS.append(f"缺少 OCR 依赖 {desc}（{rel}）")


def module_code(module):
    raw = extract_pyz_entry(pyz_data, pyz_toc[module])
    return marshal.loads(raw)


for module, symbols in EXPECT_SYMBOLS.items():
    if module == "app":
        code = unmarshal(archive.extract("app"))
    else:
        code = module_code(module)
    names = list(names_of(code))
    for symbol in symbols:
        if symbol not in names:
            ERRORS.append(f"{module} 缺少符号 {symbol}")

for module, expected_literals in {
    "app": ["关卡", "关卡封装", "切换", "workflow_global", "script_global",
            "wait_text_absent", "ocr_offset_up", "ocr_offset_down",
            "ocr_offset_left", "ocr_offset_right"],
    "dialogs": ["工作流全局模块", "脚本全局模块", "读取数字", "expected_number",
                "wait_text_absent",
                "ocr_offset_up", "ocr_offset_down", "ocr_offset_left", "ocr_offset_right"],
    "storage": ["workflow_global", "script_global", "number", "workflow_templates.migrated.json",
                "wait_text_absent",
                "ocr_offset_up", "ocr_offset_down", "ocr_offset_left", "ocr_offset_right"],
    "player": ["expected_number", "number", "wait_text_absent", "ocr_offset_up", "ocr_offset_down",
               "ocr_offset_left", "ocr_offset_right"],
}.items():
    code = unmarshal(archive.extract("app")) if module == "app" else module_code(module)
    literals = set(literals_of(code))
    for expected in expected_literals:
        if expected not in literals:
            ERRORS.append(f"{module} 缺少分类常量 {expected}")
if ERRORS:
    print("验证失败：")
    for error in ERRORS:
        print(" -", error)
    sys.exit(1)
print(f"OK：版本 {EXPECT_VERSION}，GUI 子系统、序数 380、所有符号与游戏级输入锁检查通过")
