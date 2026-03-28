# SPDX-License-Identifier: GPL-3.0-or-later
"""kckfxgen：EPUB 漫画 → KFX（Kindle 固定版式）。"""

from __future__ import annotations

__version__ = "0.1.0"

from .kpf_to_kfx import kpf_path_to_kfx_bytes, kpf_path_to_kfx_file
from .pipeline import (
    comic_archive_to_kpf,
    convert_epub_to_kfx,
    convert_to_kfx,
    epub_to_kpf,
)

__all__ = [
    "__version__",
    "comic_archive_to_kpf",
    "convert_epub_to_kfx",
    "convert_to_kfx",
    "epub_to_kpf",
    "kpf_path_to_kfx_bytes",
    "kpf_path_to_kfx_file",
]
