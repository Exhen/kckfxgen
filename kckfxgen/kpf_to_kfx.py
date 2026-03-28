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


def _normalize_kindle_title_cover_image_str(book: object) -> None:
    """把 kindle_title 里 cover_image 的 $307 写成内置 str（kfx_id 解析后常为 IonSymbol，设备表现不一致）。"""
    from kckfxgen.kfxlib.ion import IS, IonStruct

    f490 = book.fragments.get("$490")
    if f490 is None:
        return
    for cm in f490.value.get("$491", []):
        if str(cm.get("$495", "")) != "kindle_title_metadata":
            continue
        new_rows = []
        for kv in cm.get("$258", []):
            key = kv.get("$492", "")
            val = kv.get("$307", "")
            if key == "cover_image":
                new_rows.append(
                    IonStruct(IS("$492"), "cover_image", IS("$307"), str(val))
                )
            else:
                new_rows.append(kv)
        cm[IS("$258")] = new_rows
        break


def _pdoc_strip_fragment_258_cover_asin(book: object) -> None:
    """PDOC：删除顶层 $258 的 $424/$224；勿清空整段 $258（会破坏与 document_data 的阅读顺序一致性）。"""
    f258 = book.fragments.get("$258")
    if f258 is None:
        return
    v = f258.value
    for k in list(v.keys()):
        if str(k) in ("$424", "$224"):
            del v[k]


def kpf_path_to_kfx_bytes(kpf_path: Path, *, cde_pdoc: bool = True) -> bytes:
    """
    读取 ``.kpf`` 路径，返回单文件 KFX 二进制（CONT）。

    默认 ``cde_pdoc=True``：个人文档 (PDOC)。若 ``cde_pdoc=False`` 则按商店电子书 (EBOK) 并生成 ASIN 占位。
    """
    _ensure_calibre_config_stub()
    from kckfxgen.kfxlib import YJ_Book, YJ_Metadata, set_logger
    from kckfxgen.kfxlib.utilities import (
        begin_parallel_kfx_convert,
        end_parallel_kfx_convert,
    )

    kpf_path = kpf_path.expanduser().resolve()
    if not kpf_path.is_file():
        raise FileNotFoundError(kpf_path)

    begin_parallel_kfx_convert()
    try:
        lib_log = _KfxLibLogAdapter(logging.getLogger("kfxlib"))
        set_logger(lib_log)
        try:
            md = YJ_Metadata(replace_existing_authors_with_sort=True)
            md.asin = True
            md.cde_content_type = "PDOC" if cde_pdoc else "EBOK"

            book = YJ_Book(str(kpf_path))
            book.decode_book(set_metadata=md, set_approximate_pages=-1)
            # 封面保持 kindle_title 指向的正文 $164，勿改绑 kfx_cover_image；始终 set_cover_image_data 以补 $162（image/jpg 或 image/png）。
            cov = book.get_cover_image_data()
            if cov is not None:
                fixed = book.fix_cover_image_data(cov)
                book.set_cover_image_data(fixed)
                book.set_yj_metadata_to_book(book.get_yj_metadata_from_book())
            if cde_pdoc:
                _pdoc_strip_fragment_258_cover_asin(book)
            _normalize_kindle_title_cover_image_str(book)
            return book.convert_to_single_kfx()
        finally:
            set_logger(None)
    finally:
        end_parallel_kfx_convert()


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
