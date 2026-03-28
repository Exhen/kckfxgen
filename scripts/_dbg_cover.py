import logging
import shutil
import sys
import tempfile
import types
from pathlib import Path

logging.disable(logging.CRITICAL)
if "calibre.constants" not in sys.modules:
    import os

    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)
    m1, m2 = types.ModuleType("calibre"), types.ModuleType("calibre.constants")
    m2.config_dir = root
    m1.constants = m2
    sys.modules["calibre"] = m1
    sys.modules["calibre.constants"] = m2

import zipfile

from PIL import Image

from kckfxgen.epub_collect import EPUBMetadata
from kckfxgen.kdf_writer import ImageKdfWriter
from kckfxgen.kfxlib import YJ_Book, YJ_Metadata
from kckfxgen.kfxlib.ion import IS

tmp = Path(tempfile.mkdtemp())
try:
    img = tmp / "p.jpg"
    Image.new("RGB", (400, 600)).save(img, "JPEG")
    meta = EPUBMetadata(title="T", author="A", language="en")
    db = tmp / "book.kdf"
    ImageKdfWriter(meta).create_kdf(tmp, db, [img], cover_from_first_portrait=True)
    kpf = tmp / "x.kpf"
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
    print("meta cover", repr(book.get_metadata_value("cover_image")))
    print("title", book.get_metadata_value("title"))
    m = book.fragments.get("$258")
    if m:
        print("258 keys", [repr(k) for k in m.value.keys()])
        for k in m.value.keys():
            if "424" in str(k) or "cover" in str(k).lower():
                print(" key", repr(k), "val", repr(m.value[k]))
    cv = book.get_metadata_value("cover_image")
    cr = book.fragments.get(ftype="$164", fid=cv)
    print("cover164", cr is not None, cr)
    if cr:
        loc = cr.value.get("$165")
        print("165 loc", repr(loc), type(loc))
        r = book.fragments.get(ftype="$417", fid=loc)
        print("417 blob", r is not None)
    print("cover data", book.get_cover_image_data())
    for f in book.fragments.get_all("$164"):
        print("164 fid", repr(f.fid), type(f.fid))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
