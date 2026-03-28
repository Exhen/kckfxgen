# SPDX-License-Identifier: GPL-3.0-or-later
"""将 Kindle Create 风格 .kpf（内含 book.kdf）打包为单文件 .kfx（CONT 容器）。

实现位于本包 ``kckfxgen.kfxlib``（源自 John Howell 的 Calibre KFX Output / [dstaley fork](https://github.com/dstaley/calibre-kfx-output)，GPL v3），
通过 ``KpfContainer`` 读 KDF、``KpfBook.fix_kpf_prepub_book`` 修正、``KfxContainer.serialize`` 写出，
与上游 `kfx_container.py <https://github.com/dstaley/calibre-kfx-output/blob/master/kfxlib/kfx_container.py>`__ 中流程一致。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

logger = logging.getLogger(__name__)

_calibre_stub_installed = False


def _ensure_calibre_config_stub() -> None:
    """kfxlib 的 ``YJ_Book.load_symbol_catalog`` 会读 ``calibre.constants.config_dir``；无 Calibre 时用临时目录占位。"""
    global _calibre_stub_installed
    if _calibre_stub_installed:
        return
    import os
    import tempfile

    if "calibre.constants" in sys.modules:
        _calibre_stub_installed = True
        return

    root = tempfile.mkdtemp(prefix="kckfxgen_calibre_cfg_")
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)

    calibre_mod = types.ModuleType("calibre")
    constants_mod = types.ModuleType("calibre.constants")
    constants_mod.config_dir = root
    calibre_mod.constants = constants_mod

    sys.modules["calibre"] = calibre_mod
    sys.modules["calibre.constants"] = constants_mod
    _calibre_stub_installed = True


class _KfxLibLogAdapter:
    """kfxlib 的 ``log.warn`` 等接口对齐到标准 logging。"""

    def __init__(self, base: logging.Logger) -> None:
        self._l = base

    def debug(self, msg: object) -> None:
        self._l.debug("%s", msg)

    def info(self, msg: object) -> None:
        self._l.info("%s", msg)

    def warn(self, msg: object) -> None:
        self._l.warning("%s", msg)

    def warning(self, msg: object) -> None:
        self._l.warning("%s", msg)

    def error(self, msg: object) -> None:
        self._l.error("%s", msg)

    def exception(self, msg: object) -> None:
        self._l.exception("%s", msg)


def kpf_path_to_kfx_bytes(kpf_path: Path, *, cde_pdoc: bool = True) -> bytes:
    """
    读取 ``.kpf`` 路径，返回单文件 KFX 二进制（CONT）。

    默认 ``cde_pdoc=True``：个人文档 (PDOC)。若 ``cde_pdoc=False`` 则按商店电子书 (EBOK) 并生成 ASIN 占位。
    """
    _ensure_calibre_config_stub()
    from kckfxgen.kfxlib import YJ_Book, YJ_Metadata, set_logger

    kpf_path = kpf_path.expanduser().resolve()
    if not kpf_path.is_file():
        raise FileNotFoundError(kpf_path)

    lib_log = _KfxLibLogAdapter(logging.getLogger("kfxlib"))
    set_logger(lib_log)
    try:
        md = YJ_Metadata(replace_existing_authors_with_sort=True)
        md.asin = True
        md.cde_content_type = "PDOC" if cde_pdoc else "EBOK"

        book = YJ_Book(str(kpf_path))
        book.decode_book(set_metadata=md, set_approximate_pages=-1)
        return book.convert_to_single_kfx()
    finally:
        set_logger(None)


def kpf_path_to_kfx_file(
    kpf_path: Path,
    output_kfx: Path,
    *,
    cde_pdoc: bool = True,
) -> Path:
    """写出 ``output_kfx``（父目录须已存在或可创建）。"""
    output_kfx = output_kfx.expanduser().resolve()
    output_kfx.parent.mkdir(parents=True, exist_ok=True)
    data = kpf_path_to_kfx_bytes(kpf_path, cde_pdoc=cde_pdoc)
    output_kfx.write_bytes(data)
    logger.info("已写入 KFX: %s (%d 字节)", output_kfx, len(data))
    return output_kfx
