# SPDX-License-Identifier: GPL-3.0-or-later
"""削弱固定版式漫画在 Kindle Colorsoft 等设备上可能出现的彩虹摩尔纹（频域定向衰减）。

算法与实现改编自 **Kindle Comic Converter (KCC)** 的 ``rainbow_artifacts_eraser.py``（同 GPL-3.0）：
https://github.com/ciromattia/kcc/blob/master/kindlecomicconverter/rainbow_artifacts_eraser.py

在 kckfxgen 中仅做必要整理：修正 ``is_color is None`` 分支、类型注解与模块说明。
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True


def fourier_transform_image(img: Image.Image | np.ndarray) -> np.ndarray:
    """对实值图像（PIL 或 ``float32``/``uint8`` 数组）做 ``rfft2``。"""
    img_array = np.asarray(img, dtype=np.float32)
    return np.fft.rfft2(img_array)


def attenuate_diagonal_frequencies(
    fft_spectrum: np.ndarray,
    freq_threshold: float = 0.30,
    target_angle: float = 135,
    angle_tolerance: float = 10,
    attenuation_factor: float = 0.10,
) -> np.ndarray:
    """在频域削弱沿特定角度的高频能量（彩虹纹常见走向）。"""
    if fft_spectrum.ndim == 2:
        height, width_rfft = fft_spectrum.shape
    else:
        height, width_rfft = fft_spectrum.shape[:2]

    width_original = (width_rfft - 1) * 2

    freq_y = np.fft.fftfreq(height, d=1.0)
    freq_x = np.fft.rfftfreq(width_original, d=1.0)

    freq_y_grid = freq_y.reshape(-1, 1)
    freq_x_grid = freq_x.reshape(1, -1)

    freq_radial_sq = freq_x_grid**2 + freq_y_grid**2
    freq_threshold_sq = freq_threshold**2

    freq_condition = freq_radial_sq >= freq_threshold_sq

    if not np.any(freq_condition):
        return fft_spectrum

    angles_rad = np.arctan2(freq_y_grid, freq_x_grid)
    angles_deg = np.rad2deg(angles_rad) % 360

    target_angle_2 = (target_angle + 180) % 360
    target_angle_3 = (target_angle + 90) % 360
    target_angle_4 = (target_angle_3 + 180) % 360

    angle_condition = np.zeros_like(angles_deg, dtype=bool)

    for angle in (target_angle, target_angle_2, target_angle_3, target_angle_4):
        min_angle = (angle - angle_tolerance) % 360
        max_angle = (angle + angle_tolerance) % 360

        if min_angle > max_angle:
            angle_condition |= (angles_deg >= min_angle) | (angles_deg <= max_angle)
        else:
            angle_condition |= (angles_deg >= min_angle) & (angles_deg <= max_angle)

    combined_condition = freq_condition & angle_condition

    if attenuation_factor == 0:
        if fft_spectrum.ndim == 2:
            fft_spectrum[combined_condition] = 0
        else:
            fft_spectrum[combined_condition, :] = 0
        return fft_spectrum
    if attenuation_factor == 1:
        return fft_spectrum

    if fft_spectrum.ndim == 2:
        fft_spectrum[combined_condition] *= attenuation_factor
    else:
        fft_spectrum[combined_condition, :] *= attenuation_factor
    return fft_spectrum


def inverse_fourier_transform_image(
    fft_spectrum: np.ndarray,
    is_color: bool,
    original_shape: tuple[int, int] | None = None,
) -> Image.Image:
    if original_shape is not None:
        img_reconstructed = np.fft.irfft2(fft_spectrum, s=original_shape)
    else:
        img_reconstructed = np.fft.irfft2(fft_spectrum)

    img_reconstructed = np.clip(img_reconstructed, 0, 255)
    img_reconstructed = img_reconstructed.astype(np.uint8)

    if is_color and img_reconstructed.ndim == 3:
        return Image.fromarray(img_reconstructed, mode="RGB")
    return Image.fromarray(img_reconstructed, mode="L")


def rgb_to_yuv(rgb_array: np.ndarray) -> np.ndarray:
    rgb_to_yuv_matrix = np.array(
        [
            [0.299, 0.587, 0.114],
            [-0.14713, -0.28886, 0.436],
            [0.615, -0.51499, -0.10001],
        ]
    )
    original_shape = rgb_array.shape
    rgb_flat = rgb_array.reshape(-1, 3)
    yuv_flat = rgb_flat @ rgb_to_yuv_matrix.T
    return yuv_flat.reshape(original_shape)


def yuv_to_rgb(yuv_array: np.ndarray) -> np.ndarray:
    yuv_to_rgb_matrix = np.array(
        [
            [1.0, 0.0, 1.13983],
            [1.0, -0.39465, -0.58060],
            [1.0, 2.03211, 0.0],
        ]
    )
    original_shape = yuv_array.shape
    yuv_flat = yuv_array.reshape(-1, 3)
    rgb_flat = yuv_flat @ yuv_to_rgb_matrix.T
    return rgb_flat.reshape(original_shape)


def erase_rainbow_artifacts(
    img: Image.Image,
    is_color: bool | None,
) -> Image.Image:
    """去除或削弱彩虹摩尔纹；彩色图只处理亮度 (Y) 通道，色度保留。"""
    if is_color is None:
        is_color = img.mode in ("RGB", "RGBA")

    if is_color and img.mode in ("RGB", "RGBA"):
        if img.mode == "RGBA":
            img = img.convert("RGB")

        img_array = np.array(img, dtype=np.float32)
        yuv_array = rgb_to_yuv(img_array)
        luminance = yuv_array[:, :, 0]

        fft_spectrum = fourier_transform_image(luminance)
        clean_spectrum = attenuate_diagonal_frequencies(fft_spectrum)
        clean_luminance = np.fft.irfft2(clean_spectrum, s=luminance.shape)

        clean_luminance = np.clip(clean_luminance, 0, 255)
        yuv_array[:, :, 0] = clean_luminance

        rgb_array = yuv_to_rgb(yuv_array)
        rgb_array = np.clip(rgb_array, 0, 255).astype(np.uint8)
        return Image.fromarray(rgb_array, mode="RGB")

    if img.mode != "L":
        img = img.convert("L")

    original_shape = (img.height, img.width)

    fft_spectrum = fourier_transform_image(img)
    clean_spectrum = attenuate_diagonal_frequencies(fft_spectrum)
    return inverse_fourier_transform_image(clean_spectrum, False, original_shape)
