from __future__ import annotations

from pathlib import Path

import cv2
import mss
import numpy as np


def load_image(path: str | Path) -> np.ndarray | None:
    """Load an image without relying on OpenCV's Windows path handling."""
    try:
        encoded = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if encoded.size == 0:
        return None
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def capture_bgr(region: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, tuple[int, int]]:
    with mss.mss() as grabber:
        if region:
            left, top, width, height = region
            monitor = {"left": left, "top": top, "width": max(1, width), "height": max(1, height)}
            origin = (left, top)
        else:
            monitor = grabber.monitors[0]
            origin = (int(monitor["left"]), int(monitor["top"]))
        shot = np.asarray(grabber.grab(monitor))
        return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR), origin


def find_template(template_path: str | Path, threshold: float = 0.85,
                  region: tuple[int, int, int, int] | None = None,
                  ignore_background: bool = False) -> dict | None:
    screen, origin = capture_bgr(region)
    return find_template_in_image(template_path, screen, threshold, origin,
                                  region, ignore_background)


def _estimate_background_color(template: np.ndarray) -> np.ndarray | None:
    """用模板最外圈像素估计主背景色。

    模板图通常带纯色/近纯色背景，文字笔画不会顶到边缘。边缘颜色量化后
    以最高频颜色为种子，把量化域内相近（≤1 级）的颜色并入同一簇；簇占
    比达到 30% 才认为背景可用单一颜色描述（纯色背景、少量颜色组成的
    纹理都成立；渐变背景每像素颜色不同，占比必然不足，返回 None 让
    调用方回退普通匹配）。背景色取簇内原始像素的均值，更贴近实际底色。
    """
    border = np.vstack([
        template[:2].reshape(-1, 3),
        template[-2:].reshape(-1, 3),
        template[:, :2].reshape(-1, 3),
        template[:, -2:].reshape(-1, 3),
    ])
    quantized = border // 16  # 每通道量化到 16 级，聚合相近颜色
    colors, counts = np.unique(quantized, axis=0, return_counts=True)
    seed = colors[np.argmax(counts)]
    members = np.abs(colors.astype(np.int16) - seed.astype(np.int16)).max(axis=1) <= 1
    share = float(counts[members].sum()) / float(counts.sum())
    if share < 0.30:
        return None
    indices = np.argwhere(np.abs(quantized.astype(np.int16) - seed.astype(np.int16))
                          .max(axis=1) <= 1).ravel()
    return border[indices].mean(axis=0).astype(np.uint8)


def _build_ignore_background_mask(template: np.ndarray,
                                  background: np.ndarray) -> np.ndarray | None:
    """生成掩码：与背景色距离超过容差的像素视为前景（文字笔画）。

    抗锯齿会把笔画边缘像素混成“背景与文字之间”的颜色，背景一变这些
    边缘像素就跟着变，参与匹配会大幅拉低分数。因此对掩码做一次腐蚀，
    只保留笔画核心的纯色像素——它们与背景颜色无关，是“字”最稳定的部分。

    前景占比必须落在 [5%, 95%] 之间，否则掩码无意义（纯背景图或纯前景图），
    返回 None 回退普通匹配。
    """
    diff = np.linalg.norm(
        template.astype(np.float32) - background.astype(np.float32), axis=2,
    )
    mask = np.where(diff > 48.0, 255, 0).astype(np.uint8)
    # 抗锯齿/模糊还会把笔画外 1px 的背景污染成前景；腐蚀 2 次才能净删掉
    # 这层污染，同时把笔画边缘的混合像素一并去掉，只留纯色核心。
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=2)
    ratio = float(mask[mask > 0].size) / float(mask.size)
    if not 0.05 <= ratio <= 0.95:
        return None
    return mask


def _match_with_mask(search: np.ndarray, template: np.ndarray,
                     mask: np.ndarray) -> tuple[float, tuple[int, int]]:
    """掩码匹配：只按模板上的前景像素（文字笔画）计算，忽略背景颜色。

    OpenCV 的 matchTemplate 只有 TM_SQDIFF(_NORMED) 与 TM_CCORR_NORMED
    支持掩码，但 SQDIFF 对整体亮度敏感——亮背景 + 亮文字的相对差异小，
    归一化分数虚高，会在错误位置产生假阳性。这里用 filter2D 手工实现
    “掩码区域内的零均值相关系数”（等价于支持掩码的 CCOEFF）：只统计
    前景像素，且各自减去窗口均值再归一化，背景颜色与整体亮度变化都不
    影响分数，语义与普通匹配一致（越大越像，1.0 = 完全一致）。
    """
    gray_s = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gray_t = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY).astype(np.float32)
    m = (mask > 0).astype(np.float32)
    area = float(m.sum())
    sum_t = float((gray_t * m).sum())
    sum_t2 = float((gray_t * gray_t * m).sum())
    var_t = sum_t2 - sum_t * sum_t / area
    # 模板与搜索窗口对齐时：cov = Σ(T·I·M) - Σ(T·M)·Σ(I·M)/ΣM
    # var_I = Σ(I²·M) - (Σ(I·M))²/ΣM。三项都是滑动窗口求和，
    # 用 filter2D 按窗口左上角做相关（anchor=0），边缘补零即截断。
    sum_i = cv2.filter2D(gray_s, -1, m, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT)
    sum_i2 = cv2.filter2D(gray_s * gray_s, -1, m, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT)
    sum_ti = cv2.filter2D(gray_s, -1, m * gray_t, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT)
    sh, sw = search.shape[:2]
    th, tw = template.shape[:2]
    rows = slice(0, sh - th + 1)
    cols = slice(0, sw - tw + 1)
    sum_i = sum_i[rows, cols]
    sum_i2 = sum_i2[rows, cols]
    sum_ti = sum_ti[rows, cols]
    cov = sum_ti - sum_t * sum_i / area
    var_i = sum_i2 - sum_i * sum_i / area
    var_i = np.maximum(var_i, 0.0)
    if var_t > 25.0:
        # 模板笔画有明暗变化（渐变字、描边字）：用零均值相关系数，
        # 亮度与对比度变化都被归一化吸收，窗口内纯色时判 0 防噪声。
        # （var_t ≤ 0 时除法无意义，但本分支只在 var_t > 25 时进入。）
        corr = cov / np.sqrt(var_t * var_i)
        corr = np.where(var_i < 25.0, 0.0, np.clip(corr, -1.0, 1.0))
        score_map = corr
    else:
        # 模板笔画是纯色（如纯白字，var_t≈0）：零均值化失去意义，
        # 改用“亮度差 + 均匀度”：窗口内掩码像素应与模板笔画同亮度
        # （|mean-c|，容忍整体亮度偏移），且内部均匀（std 小，防止在
        # 纯色背景上误报）。两种扣分都除以 255 归一化到 [0,1]。
        mean_i = sum_i / area
        std_i = np.sqrt(var_i)
        score_map = 1.0 - (np.abs(mean_i - sum_t / area) + std_i) / 255.0
    _, _, _, point = cv2.minMaxLoc(score_map)
    return float(score_map[point[1], point[0]]), point


def find_template_in_image(template_path: str | Path, screen: np.ndarray,
                           threshold: float = 0.85,
                           origin: tuple[int, int] = (0, 0),
                           region: tuple[int, int, int, int] | None = None,
                           ignore_background: bool = False) -> dict | None:
    """Match one template against an existing screenshot.

    Global detectors call this repeatedly with the same full-desktop screenshot,
    cropping their own regions in memory instead of capturing the desktop again.

    ignore_background=True 时只匹配模板上的前景像素（自动识别为文字笔画等
    与背景色不同的内容），背景颜色变化不影响匹配；背景无法自动识别时
    自动回退普通匹配。
    """
    template = load_image(template_path)
    if template is None:
        raise FileNotFoundError(f"无法读取模板图片：{template_path}")
    search = screen
    search_origin = (int(origin[0]), int(origin[1]))
    if region:
        left, top, width, height = map(int, region)
        image_height, image_width = screen.shape[:2]
        raw_x1 = left - int(origin[0])
        raw_y1 = top - int(origin[1])
        x1 = max(0, raw_x1)
        y1 = max(0, raw_y1)
        x2 = min(image_width, raw_x1 + max(1, width))
        y2 = min(image_height, raw_y1 + max(1, height))
        if x2 <= x1 or y2 <= y1:
            return None
        search = screen[y1:y2, x1:x2]
        search_origin = (int(origin[0]) + x1, int(origin[1]) + y1)
    th, tw = template.shape[:2]
    sh, sw = search.shape[:2]
    if th > sh or tw > sw:
        return None
    if ignore_background:
        background = _estimate_background_color(template)
        if background is not None:
            mask = _build_ignore_background_mask(template, background)
            if mask is not None:
                score, point = _match_with_mask(search, template, mask)
                if score >= float(threshold):
                    x = search_origin[0] + point[0]
                    y = search_origin[1] + point[1]
                    return {
                        "x": x, "y": y, "width": tw, "height": th,
                        "center_x": x + tw // 2, "center_y": y + th // 2,
                        "score": float(score),
                    }
                return None
    result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, point = cv2.minMaxLoc(result)
    if score < float(threshold):
        return None
    x = search_origin[0] + point[0]
    y = search_origin[1] + point[1]
    return {
        "x": x, "y": y, "width": tw, "height": th,
        "center_x": x + tw // 2, "center_y": y + th // 2,
        "score": float(score),
    }
