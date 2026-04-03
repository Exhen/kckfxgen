# SPDX-License-Identifier: GPL-3.0-or-later
"""在导入 NumPy 之前限制 BLAS/OpenMP 线程数。

多线程并发（如 ``ThreadPoolExecutor`` 内同时跑多路转换）时，每个线程内的 FFT/线性代数
会再 spawn 一批 OpenBLAS/MKL/Accelerate 线程，极易在 Windows 等环境下触发原生崩溃。
将每个 BLAS 实例限制为单线程，由应用层线程池提供并行，可显著降低闪退概率。

使用 ``setdefault``：若环境已有配置则不覆盖（便于高级用户调参）。
"""

from __future__ import annotations

import os

_VARS: tuple[tuple[str, str], ...] = (
    ("OPENBLAS_NUM_THREADS", "1"),
    ("OMP_NUM_THREADS", "1"),
    ("MKL_NUM_THREADS", "1"),
    ("NUMEXPR_NUM_THREADS", "1"),
    # macOS Accelerate / vecLib
    ("VECLIB_MAXIMUM_THREADS", "1"),
)


def apply_limits() -> None:
    for key, value in _VARS:
        os.environ.setdefault(key, value)


apply_limits()
