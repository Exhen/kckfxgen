"""一次性冒烟：生成最小 KPF 并 KPF→KFX，观察 position map 告警是否消失。"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from PIL import Image

logging.basicConfig(level=logging.WARNING)

from kckfxgen.epub_collect import EPUBMetadata
from kckfxgen.kdf_writer import ImageKdfWriter
from kckfxgen.kpf_to_kfx import kpf_path_to_kfx_bytes
from kckfxgen.pipeline import _write_kcb, _write_manifest

tmp = Path(tempfile.mkdtemp())
try:
    img = tmp / "p.jpg"
    Image.new("RGB", (400, 600), (10, 20, 30)).save(img, "JPEG", quality=90)
    kpf_dir = tmp / "kpf"
    res = kpf_dir / "resources"
    res.mkdir(parents=True)
    meta = EPUBMetadata(title="T", author="A", language="en")
    w = ImageKdfWriter(meta)
    w.create_kdf(tmp, res / "book.kdf", [img])
    _write_kcb(kpf_dir)
    _write_manifest(res)
    shutil.make_archive(str(tmp / "pack"), "zip", kpf_dir)
    (tmp / "pack.zip").rename(tmp / "t.kpf")
    data = kpf_path_to_kfx_bytes(tmp / "t.kpf")
    assert data[:4] == b"CONT"
    print("ok", len(data))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
