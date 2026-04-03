# SPDX-License-Identifier: GPL-3.0-or-later
"""多进程并行时的子进程入口。

Windows 上多个 **线程** 同时跑 NumPy/FFT/BLAS 仍可能原生闪退；用进程隔离每本书的转换可避免共享运行时冲突。
本模块须可被 pickle 按 **模块路径** 导入（勿把任务函数定义在 ``__main__`` 内）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import kckfxgen.blas_thread_env  # noqa: F401


def build_convert_payload(
    src: Path,
    kfx_dir: Path,
    *,
    split_spreads: bool,
    split_page_order: Literal["right-left", "left-right"],
    rotate_landscape_90: bool,
    erase_colorsoft_rainbow: bool,
    page_progression: Literal["ltr", "rtl"],
    layout_view: Literal["fixed", "virtual"],
    virtual_panel_axis: Literal["vertical", "horizontal"],
    keep_kpf: bool,
    book_title: str | None,
    book_author: str | None,
    book_publisher: str | None,
) -> dict[str, Any]:
    return {
        "src": str(src.expanduser().resolve()),
        "kfx_dir": str(kfx_dir.expanduser().resolve()),
        "split_spreads": split_spreads,
        "split_page_order": split_page_order,
        "rotate_landscape_90": rotate_landscape_90,
        "erase_colorsoft_rainbow": erase_colorsoft_rainbow,
        "page_progression": page_progression,
        "layout_view": layout_view,
        "virtual_panel_axis": virtual_panel_axis,
        "keep_kpf": keep_kpf,
        "book_title": book_title,
        "book_author": book_author,
        "book_publisher": book_publisher,
    }


def convert_job_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    """子进程执行单文件转换。返回 ``(源路径字符串, 错误文本或 None)``。"""
    from kckfxgen.pipeline import convert_to_kfx

    src = Path(payload["src"])
    kfx_dir = Path(payload["kfx_dir"])
    try:
        convert_to_kfx(
            src,
            kfx_dir,
            split_spreads=payload["split_spreads"],
            split_page_order=payload["split_page_order"],
            rotate_landscape_90=payload["rotate_landscape_90"],
            erase_colorsoft_rainbow=payload["erase_colorsoft_rainbow"],
            page_progression=payload["page_progression"],
            layout_view=payload["layout_view"],
            virtual_panel_axis=payload["virtual_panel_axis"],
            keep_kpf=payload["keep_kpf"],
            book_title=payload.get("book_title"),
            book_author=payload.get("book_author"),
            book_publisher=payload.get("book_publisher"),
        )
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return (str(src), f"{type(e).__name__}: {e}")
    return (str(src), None)
