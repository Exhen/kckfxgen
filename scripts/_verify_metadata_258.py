"""decode 后 get_metadata_value 能从 $258 读到 title/author/publisher。"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

logging.disable(logging.CRITICAL)
if "calibre.constants" not in sys.modules:
    import os

    root = tempfile.mkdtemp(prefix="kckfxgen_calibre_cfg_")
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)
    m1, m2 = types.ModuleType("calibre"), types.ModuleType("calibre.constants")
    m2.config_dir = root
    m1.constants = m2
    sys.modules["calibre"] = m1
    sys.modules["calibre.constants"] = m2

from PIL import Image

from kckfxgen.epub_collect import EPUBMetadata
from kckfxgen.kdf_writer import ImageKdfWriter
from kckfxgen.kfxlib import YJ_Book, YJ_Metadata

tmp = Path(tempfile.mkdtemp())
try:
    img = tmp / "p.jpg"
    Image.new("RGB", (400, 600)).save(img, "JPEG")
    meta = EPUBMetadata(
        title="我的书",
        author="张三",
        publisher="出版社",
        language="zh",
        description="简介一行",
    )
    db = tmp / "book.kdf"
    ImageKdfWriter(meta).create_kdf(
        tmp, db, [img], cover_from_first_portrait=True
    )
    import zipfile

    kpf = tmp / "x.kpf"
    res = db.parent
    with zipfile.ZipFile(kpf, "w") as zf:
        zf.write(db, "resources/book.kdf")
        zf.writestr(
            "book.kcb",
            '{"book_state":{},"metadata":{"book_path":"resources","tool_name":"t","tool_version":"1"}}',
        )
    md = YJ_Metadata(replace_existing_authors_with_sort=True)
    md.asin = True
    md.cde_content_type = "EBOK"
    book = YJ_Book(str(kpf))
    book.decode_book(set_metadata=md, set_approximate_pages=-1)
    assert book.get_metadata_value("title") == "我的书", book.get_metadata_value("title")
    assert book.get_metadata_value("author") == "张三", book.get_metadata_value("author")
    assert book.get_metadata_value("publisher") == "出版社"
    assert book.get_metadata_value("language") == "zh"
    assert book.get_cover_image_data() is not None
    print("ok")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
