"""OCR 文字识别：PaddleOCR 引擎的惰性单例封装。

使用 PaddleOCR 3.x（paddlepaddle 3.3.1），det/rec/文字方向 3 个模型内置在
paddle_models/ 目录，完全离线；引擎首次调用时才初始化（加载模型需要一两秒），
避免拖慢软件启动。打包版中 paddle 全家（paddle/paddleocr/paddlex + 模型）位于
exe 同目录的 paddle_ocr/，由 build.ps1 复制，首次使用 OCR 时才加入 sys.path
按需加载（主 exe 因此从约 253MB 缩到约 70MB）。识别结果只取文本，各识别行
直接拼接（中文没有空格，拉丁字母连写符合截图实际排版）。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import unicodedata
from pathlib import Path

import numpy as np

# paddle 3.x 在 Windows 上对 PP-OCRv5 模型启用 oneDNN(MKLDNN) 会触发
# ConvertPirAttribute2RuntimeAttribute 崩溃，必须关闭；同时固定模型源为百度
# BOS 并跳过连通性探测，保证离线可用。这些环境变量须在 import paddle 之前
# 生效，故放在模块最顶部。
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

from macroflow.core.image_match import capture_bgr  # noqa: E402

_engine = None
_progress_callback = None
# 引擎初始化可能被启动预加载线程与播放线程并发触发：首次导入 paddle 全家
# 可能耗时数十秒，必须串行化，避免两边同时初始化。
_engine_lock = threading.Lock()

# (模型目录名, model_name 参数名, model_dir 参数名)
MODEL_DIRS = (
    ("PP-OCRv5_mobile_det", "text_detection_model_name", "text_detection_model_dir"),
    ("PP-OCRv5_mobile_rec", "text_recognition_model_name", "text_recognition_model_dir"),
    ("PP-LCNet_x1_0_textline_ori", "textline_orientation_model_name", "textline_orientation_model_dir"),
)


def set_progress_callback(callback) -> None:
    """Set a best-effort callback for coarse OCR initialization progress."""
    global _progress_callback
    _progress_callback = callback


def _report_progress(stage: str, percent: int) -> None:
    callback = _progress_callback
    if callback is not None:
        try:
            callback(stage, max(0, min(100, int(percent))))
        except Exception:
            pass


def _ocr_component_root() -> Path | None:
    """打包版 OCR 组件目录：exe 同目录的 paddle_ocr/；源码运行返回 None。"""
    if not getattr(sys, "frozen", False):
        return None
    root = Path(sys.executable).resolve().parent / "paddle_ocr"
    return root if root.is_dir() else None


def _models_root() -> Path:
    """模型目录：打包后位于 paddle_ocr/paddle_models，开发时位于项目目录。"""
    ocr_root = _ocr_component_root()
    if ocr_root is not None:
        return ocr_root / "paddle_models"
    return Path(__file__).resolve().parent / "paddle_models"


def _get_engine():
    """初始化并返回全局 OCR 引擎（线程安全，初始化串行化）。"""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        _report_progress("准备 OCR 组件", 5)
        ocr_root = _ocr_component_root()
        try:
            if ocr_root is not None:
                sys.path.insert(0, str(ocr_root))
            _report_progress("正在导入 PaddleOCR", 20)
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                f"OCR 引擎不可用：缺少 PaddleOCR 依赖（{exc}）。"
                "打包版请保持 paddle_ocr 目录与程序同目录，或重新安装软件；"
                "源码运行请使用 run.bat 启动"
            ) from exc
        model_params = {}
        for index, (dir_name, name_param, dir_param) in enumerate(MODEL_DIRS):
            _report_progress(f"正在检查模型 {index + 1}/{len(MODEL_DIRS)}", 25 + index * 15)
            model_dir = _models_root() / dir_name
            if not (model_dir / "inference.pdiparams").is_file():
                raise RuntimeError(f"OCR 引擎不可用：缺少模型目录 {model_dir}")
            model_params[name_param] = dir_name
            model_params[dir_param] = str(model_dir)
        _report_progress("正在创建 OCR 引擎", 75)
        # 关掉文档方向分类/展平（扫描件功能，游戏截图用不到，省两个模型）；
        # det 长边限制 960 避免全屏大图识别过慢（1080p 约 2.5 秒）。
        _engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            text_det_limit_side_len=960,
            text_det_limit_type="max",
            **model_params,
        )
        _report_progress("OCR 引擎已加载", 100)
    return _engine


def recognize_image_with_boxes(
    screen: np.ndarray, origin: tuple[int, int] = (0, 0),
) -> tuple[str, list[dict]]:
    """识别图片并返回拼接文字及每一行文字的绝对屏幕坐标。"""
    try:
        result = _get_engine().predict(screen)
    except Exception as exc:
        raise RuntimeError(f"OCR 识别失败：{exc}") from exc
    texts = []
    matches = []
    origin_x, origin_y = map(int, origin)
    for page in result:
        if isinstance(page, dict):
            page_texts = list(page.get("rec_texts") or [])
            scores = list(page.get("rec_scores") or [])
            polygons = list(page.get("rec_polys") or page.get("dt_polys") or [])
            texts.extend(page_texts)
            for index, text in enumerate(page_texts):
                if index >= len(polygons):
                    continue
                points = np.asarray(polygons[index]).reshape(-1, 2)
                if not points.size:
                    continue
                left = int(np.floor(points[:, 0].min())) + origin_x
                top = int(np.floor(points[:, 1].min())) + origin_y
                right = int(np.ceil(points[:, 0].max())) + origin_x
                bottom = int(np.ceil(points[:, 1].max())) + origin_y
                width = max(1, right - left)
                height = max(1, bottom - top)
                matches.append({
                    "text": str(text),
                    "x": left, "y": top, "width": width, "height": height,
                    "center_x": left + width // 2,
                    "center_y": top + height // 2,
                    "score": float(scores[index]) if index < len(scores) else 1.0,
                })
    return "".join(str(text) for text in texts), matches


def recognize_image(screen: np.ndarray) -> str:
    """识别一张已截取的 BGR 图片，返回全部识别文本拼接。"""
    text, _matches = recognize_image_with_boxes(screen)
    return text


def recognize_region(region: tuple[int, int, int, int] | None = None) -> str:
    """截取指定区域并识别文字，返回全部识别文本拼接（空串 = 没识别到）。"""
    screen, _origin = capture_bgr(region)
    return recognize_image(screen)


def recognize_region_with_boxes(
    region: tuple[int, int, int, int] | None = None,
) -> tuple[str, list[dict]]:
    """截取区域并返回文字及命中文字的绝对屏幕坐标。"""
    screen, origin = capture_bgr(region)
    return recognize_image_with_boxes(screen, origin)


def extract_ocr_integer(recognized: str, matches: list[dict]) -> tuple[int | None, str]:
    """拼接 OCR 数字段并返回整数及原始数字串。

    有坐标的文字框按屏幕 x 坐标从左到右排列；每个框内部保留 OCR 原顺序。
    全角数字先经 NFKC 转为半角。没有任何 0-9 时返回 ``(None, "")``。
    """
    positioned: list[tuple[int, str]] = []
    for match in matches or []:
        text = unicodedata.normalize("NFKC", str(match.get("text", "")))
        digits = "".join(char for char in text if char in "0123456789")
        if not digits:
            continue
        try:
            x = int(match.get("x", match.get("center_x", 0)))
        except (TypeError, ValueError):
            x = 0
        positioned.append((x, digits))
    if positioned:
        raw_digits = "".join(digits for _x, digits in sorted(positioned))
    else:
        text = unicodedata.normalize("NFKC", str(recognized or ""))
        raw_digits = "".join(char for char in text if char in "0123456789")
    return (int(raw_digits), raw_digits) if raw_digits else (None, "")


def parse_ocr_number_pair(recognized: str, separator: str = "/") -> tuple[int, int] | None:
    """Parse the first integer pair separated by a configured OCR symbol.

    OCR may return full-width punctuation or spaces around the separator, so
    both the recognized text and separator are normalized with NFKC first.
    Only the first integer on each side is used; malformed text returns None.
    """
    text = unicodedata.normalize("NFKC", str(recognized or ""))
    token = unicodedata.normalize("NFKC", str(separator or "")).strip()
    if not token:
        return None
    parts = text.split(token, 1)
    if len(parts) != 2:
        return None
    left = re.search(r"\d+", parts[0])
    right = re.search(r"\d+", parts[1])
    if left is None or right is None:
        return None
    return int(left.group(0)), int(right.group(0))


def matches_expected(recognized: str, expected: str, mode: str = "contains") -> bool:
    """判断识别文字是否命中期望文字。

    期望文字为空时只要识别到任意文字即命中；"equals" 忽略大小写、去掉
    两端空白后整体相等；"contains" 为子串包含（同样忽略大小写）。
    """
    expected = (expected or "").strip()
    if not expected:
        return bool((recognized or "").strip())
    if mode == "equals":
        return (recognized or "").strip().casefold() == expected.casefold()
    return expected.casefold() in (recognized or "").casefold()


def find_expected_match(
    matches: list[dict], expected: str, mode: str = "contains",
) -> dict | None:
    """返回第一条命中期望内容的 OCR 文字框。"""
    for match in matches:
        if matches_expected(str(match.get("text", "")), expected, mode):
            return match
    return None


def format_ocr_observation(
    recognized: str, expected: str, matched: bool, subject: str = "识别文字",
    limit: int = 80,
) -> str:
    """Format one compact OCR observation for the log and execution mini window."""
    text = " ".join(str(recognized or "").split())
    if len(text) > limit:
        text = text[:limit] + "…"
    target = " ".join(str(expected or "").split()) or "任意文字"
    if len(target) > 40:
        target = target[:40] + "…"
    actual = f"识别到「{text}」" if text else "未识别到文字"
    return f"{subject} OCR：{actual}；期望「{target}」· {'命中' if matched else '未命中'}"


def ocr_match_center(region: tuple[int, int, int, int] | None = None) -> dict:
    """识别文字命中时构造的伪匹配：点击识别区域用区域中心，全屏用主屏中心。

    文字识别没有模板那样的精确位置，只有识别区域；区域为空（全屏）时
    退化为主屏中心，保证"点击识别区域"仍有坐标可用。
    """
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
