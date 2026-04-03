# SPDX-License-Identifier: GPL-3.0-or-later
"""双页漫：空白正中胶缝、或中心窗口内检出「半页带」强分界且缝近几何中心、两侧相关性低时竖切；否则整图保留。

数码拼页常见中缝略偏轴、正中心相邻列灰度相同，仅靠单列差会漏切；跨页单图可能在窗内有强竖线但离中心远（用 ``|x-cx|/w`` 排除）或缝两侧仍相关（用相关阈值排除）。"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

SplitPageOrder = Literal["right-left", "left-right"]


def find_split_x(width: int) -> int:
    """竖向裁切线：几何正中 ``width // 2``（空白中缝 / 旧版中缝判据仍用正中切）。"""
    return width // 2


# 扫描装订缝：中心窗口比例、与几何中心最大偏离（相对宽度）
_SEAM_WINDOW_LO = 0.35
_SEAM_WINDOW_HI = 0.65
_SEAM_MAX_OFFSET_FRAC = 0.06
# 窗口内 median(|左带均值−右带均值|) 须超过此值；略高于 nocut3 的误检顶 (~28)
_SEAM_SCORE_MIN = 32.0
# 缝两侧同宽条像素相关性 ≥ 此值则视为跨页连续原画（如 nocut3）；真双页左右页相关性通常更低
_SEAM_CORR_MAX = 0.20


def _gray_f32(im_rgb: Image.Image) -> np.ndarray:
    rgb = np.asarray(im_rgb.convert("RGB"), dtype=np.float32)
    return 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]


def blank_center_seam_likely(gray: np.ndarray) -> bool:
    """
    判断宽幅图正中附近是否为「空白装订缝」：中心竖带列内灰度沿竖向变化很小，
    且明显比左右内容区更「平」。连续跨页无中缝图会在正中仍有较大列方差，返回 False。
    """
    h, w = gray.shape
    if w < 32 or h < 16:
        return False

    lw = max(8, w // 5)
    left_m = float(np.median(np.std(gray[:, :lw], axis=0)))
    right_m = float(np.median(np.std(gray[:, w - lw :], axis=0)))
    side_m = max(left_m, right_m, 1.0)

    # 整页几乎无纹理（全白/单色），不按双页裁
    if side_m < 4.0:
        return False

    cx = w // 2
    bh = max(6, min(w // 18, 96))
    x0, x1 = cx - bh // 2, cx + bh // 2 + (bh % 2)
    x0, x1 = max(0, x0), min(w, x1)
    if x1 - x0 < 4:
        return False

    strip = gray[:, x0:x1]
    col_std = strip.std(axis=0)
    med_c = float(np.median(col_std))
    flat_frac = float(np.mean(col_std < 7.0))
    min_c = float(np.min(col_std))

    # 中缝至少有几列非常「平」
    if min_c > 6.5:
        return False
    # 中心带整体比左右画区平静得多
    if med_c > 0.30 * side_m:
        return False
    if med_c > 12.0:
        return False
    # 足够多的列为低方差（空白条带）
    if flat_frac < 0.40:
        return False
    return True


def center_vertical_discontinuity_likely(gray: np.ndarray) -> bool:
    """
    无白胶拼页：连续跨页在正中附近单列差 (k1) 与左右各若干列均值差 (k5) 量级接近；
    真双页拼接则「跨缝」累积对比明显大于「仅相邻两列」对比 (k5 >> k1)。

    单靠 k1+页内参考列在实拍漫图上易与跨页大图混淆（见 test/cut1 vs nocut*.jpg 统计）。
    """
    h, w = gray.shape
    if w < 48 or h < 16:
        return False

    cx = w // 2
    if cx < 6 or cx > w - 6:
        return False

    k1 = np.abs(gray[:, cx - 1] - gray[:, cx])
    k1_med = float(np.median(k1))

    left5 = gray[:, cx - 5 : cx].mean(axis=1)
    right5 = gray[:, cx : cx + 5].mean(axis=1)
    k5 = np.abs(left5 - right5)
    k5_med = float(np.median(k5))

    # 低对比平滑宽图（含渐变跨页）：k5 整体很小
    if k5_med < 14.0:
        return False

    # 主判据：左右半页在缝两侧的局部均值差明显大于单列差（test/cut1 典型）
    if k5_med > max(17.0, 2.08 * k1_med):
        return True

    # 两页平涂硬接：k1≈k5 且都很大，比值接近 1 但幅值足够
    if k1_med > 28.0 and k5_med > 22.0:
        return True

    return False


def _best_seam_score_and_x(gray: np.ndarray) -> tuple[float, int]:
    """
    在宽度 ``[_SEAM_WINDOW_LO·w, _SEAM_WINDOW_HI·w]`` 内扫描竖线 x，
    最大化每行「左半页带 / 右半页带」灰度均值之差的绝对值的中位数。
    数码拼页的中缝常不在几何中心，且正中心相邻列可能完全相等（k1=0），须靠带宽统计。
    """
    h, w = gray.shape
    bw = max(10, min(22, w // 50))
    lo = max(bw + 1, int(w * _SEAM_WINDOW_LO))
    hi = min(w - bw - 1, int(w * _SEAM_WINDOW_HI))
    if hi <= lo + 4:
        return 0.0, w // 2
    best_s, best_x = -1.0, (lo + hi) // 2
    for x in range(lo, hi):
        left = gray[:, x - bw : x].mean(axis=1)
        right = gray[:, x : x + bw].mean(axis=1)
        s = float(np.median(np.abs(left - right)))
        if s > best_s:
            best_s, best_x = s, x
    return best_s, best_x


def _seam_lr_correlation(gray: np.ndarray, x: int) -> float:
    """缝两侧等宽竖条的展平灰度 Pearson 相关；跨页连续原画往往较高。"""
    h, w = gray.shape
    tw = min(24, max(8, w // 45), x, w - x)
    if tw < 6:
        return 0.0
    a = gray[:, x - tw : x].reshape(-1).astype(np.float64)
    b = gray[:, x : x + tw].reshape(-1).astype(np.float64)
    if a.size != b.size:
        m = min(a.size, b.size)
        a, b = a[:m], b[:m]
    sa, sb = float(np.std(a)), float(np.std(b))
    if sa < 1e-6 or sb < 1e-6:
        return 0.0
    c = float(np.corrcoef(a, b)[0, 1])
    if np.isnan(c):
        return 0.0
    return float(np.clip(c, -1.0, 1.0))


def _wide_spread_seam_likely(gray: np.ndarray) -> tuple[bool, int]:
    """
    宽幅「真双页」：中心窗口内有明显半页分界，且缝位置接近几何中心、
    两侧条带相关性不高（排除跨页单图与栏内竖线，如 nocut2）。
    返回 ``(是否裁切, 建议竖线 x)``；不裁切时 x 仍为中心，调用方勿用。
    """
    h, w = gray.shape
    cx = w // 2
    score, x = _best_seam_score_and_x(gray)
    if abs(x - cx) / float(w) > _SEAM_MAX_OFFSET_FRAC:
        return False, cx
    if score < _SEAM_SCORE_MIN:
        return False, cx
    if _seam_lr_correlation(gray, x) >= _SEAM_CORR_MAX:
        return False, cx
    return True, x


def split_decision(gray: np.ndarray) -> tuple[bool, int]:
    """
    是否裁切与竖线 x（像素，合法范围由调用方再 clamp）。

    顺序：空白正中胶缝（正中切）→ 中心窗口扫描缝→
    原「单列 vs 半页带」不连续判据（正中切）。
    """
    w = gray.shape[1]
    cx = w // 2
    if blank_center_seam_likely(gray):
        return True, cx
    ws_ok, xw = _wide_spread_seam_likely(gray)
    if ws_ok:
        return True, xw
    if center_vertical_discontinuity_likely(gray):
        return True, cx
    return False, cx


def spread_should_split(gray: np.ndarray) -> bool:
    """宽幅双页是否应竖切（逻辑见 ``split_decision``）。"""
    return split_decision(gray)[0]


def _save_split_page(im: Image.Image, path: Path) -> None:
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        im.save(path, "JPEG", quality=95, optimize=False)
    elif ext == ".png":
        im.save(path, "PNG", compress_level=3)
    elif ext == ".webp":
        im.save(path, "WEBP", quality=95, method=3)
    else:
        im.save(path)


def _process_one_spread(
    idx: int,
    src: Path,
    out_dir: Path,
    pad: int,
    page_order: SplitPageOrder,
) -> list[Path]:
    stem = src.stem
    ext = src.suffix.lower() or ".jpg"

    with Image.open(src) as im:
        w, h = im.size
        wide = w >= h * 1.25
        if not wide:
            dest = out_dir / f"{idx:0{pad}d}_{stem}{ext}"
            shutil.copy2(src, dest)
            return [dest]

        im_rgb = im.convert("RGB")
        gray = _gray_f32(im_rgb)
        do_split, x = split_decision(gray)
        if not do_split:
            dest = out_dir / f"{idx:0{pad}d}_{stem}{ext}"
            shutil.copy2(src, dest)
            logger.debug(
                "[spread_split] 宽幅但未检出空白中缝/中缝不连续，保留整图: %s",
                src.name,
            )
            return [dest]

        x = max(1, min(w - 1, x))
        left = im_rgb.crop((0, 0, x, h))
        right = im_rgb.crop((x, 0, w, h))

        if page_order == "right-left":
            first, second = right, left
            tags = ("_R", "_L")
        else:
            first, second = left, right
            tags = ("_L", "_R")

        p0 = out_dir / f"{idx:0{pad}d}{tags[0]}_{stem}{ext}"
        p1 = out_dir / f"{idx:0{pad}d}{tags[1]}_{stem}{ext}"
        _save_split_page(first, p0)
        _save_split_page(second, p1)
        logger.debug(
            "[spread_split] 双页（空白中缝或中缝不连续，正中切） %s -> %s + %s (x=%d/%d)",
            src.name,
            p0.name,
            p1.name,
            x,
            w,
        )
        return [p0, p1]


def expand_spread_pages(
    images: list[Path],
    out_dir: Path,
    *,
    page_order: SplitPageOrder = "right-left",
) -> list[Path]:
    """
    对 **宽 ≥ 高×1.25** 的图：当 **空白正中胶缝**、或 **宽度 35%–65% 窗口内** 检出明显半页分界
    （且缝距几何中心 ≤6% 宽、缝两侧条带相关足够低）、或 **原单列/半页带不连续** 判据成立时，
    沿检出的竖线（多为正中，数码拼页可为略偏轴）切成两页；否则整图复制。非宽幅图始终整图复制。

    多图时 **顺序** 处理（曾用线程池并行，Windows 上易与 NumPy/BLAS 冲突闪退）。需 **numpy**、**Pillow**。
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pad = max(4, len(str(len(images) * 2 + 10)))
    n = len(images)
    if n == 0:
        return []

    args = [(i, src, out_dir, pad, page_order) for i, src in enumerate(images)]
    nested = [_process_one_spread(*a) for a in args]

    return [p for group in nested for p in group]
