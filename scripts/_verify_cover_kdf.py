"""Verify cover_image kfx_id survives KDF round-trip and get_cover_image_data works."""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

logging.disable(logging.CRITICAL)

# calibre stub (kpf_to_kfx)
if "calibre.constants" not in sys.modules:
    import os

    root = tempfile.mkdtemp(prefix="kckfxgen_calibre_cfg_")
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)
    calibre_mod = types.ModuleType("calibre")
    constants_mod = types.ModuleType("calibre.constants")
    constants_mod.config_dir = root
    calibre_mod.constants = constants_mod
    sys.modules["calibre"] = calibre_mod
    sys.modules["calibre.constants"] = constants_mod

from PIL import Image

from kckfxgen.epub_collect import EPUBMetadata
from kckfxgen.kdf_writer import ImageKdfWriter
from kckfxgen.kfxlib import YJ_Book

tmp = Path(tempfile.mkdtemp())
try:
    img = tmp / "p.jpg"
    Image.new("RGB", (400, 600)).save(img, "JPEG", quality=90)
    kpf = tmp / "b.kpf"
    # minimal kpf zip with book.kdf
    import zipfile

    res = tmp / "resources"
    res.mkdir()
    w = ImageKdfWriter(EPUBMetadata(title="T", author="A", language="en"))
    w.create_kdf(tmp, res / "book.kdf", [img], cover_from_first_portrait=True)
    with zipfile.ZipFile(kpf, "w") as zf:
        for p in res.rglob("*"):
            if p.is_file():
                zf.write(p, f"resources/{p.relative_to(tmp).as_posix().split('/', 1)[-1]}")
        zf.writestr(
            "book.kcb",
            '{"book_state":{},"metadata":{"book_path":"resources","tool_name":"x","tool_version":"1"}}',
        )
    book = YJ_Book(str(kpf))
    book.load()
    cv = book.get_metadata_value("cover_image")
    assert cv is not None, "cover_image metadata missing"
    data = book.get_cover_image_data()
    assert data is not None and len(data[1]) > 500, data
    print("ok cover resource", cv, "jpeg bytes", len(data[1]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
