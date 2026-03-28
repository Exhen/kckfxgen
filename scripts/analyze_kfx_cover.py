#!/usr/bin/env python3
"""分析 KFX 封面：元数据 cover_image、get_cover_image_data、$164/$417、$270.$181 与 $419。"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if "calibre.constants" not in sys.modules:
    root = tempfile.mkdtemp(prefix="kcfg_")
    os.makedirs(os.path.join(root, "plugins"), exist_ok=True)
    cm = types.ModuleType("calibre.constants")
    cm.config_dir = root
    cal = types.ModuleType("calibre")
    cal.constants = cm
    sys.modules["calibre"] = cal
    sys.modules["calibre.constants"] = cm

from kckfxgen.kfxlib import YJ_Book, set_logger


def _str(x: object) -> str:
    if x is None:
        return ""
    return str(x)


def analyze(path: Path) -> None:
    set_logger(logging.getLogger("silent"))
    logging.getLogger("silent").setLevel(logging.CRITICAL + 1)

    b = YJ_Book(str(path))
    b.decode_book()

    cov_id = b.get_metadata_value("cover_image")
    cov_data = b.get_cover_image_data()

    f490 = b.fragments.get("$490")
    kt_rows: list[tuple[str, str]] = []
    if f490:
        for cm in f490.value.get("$491", []):
            if _str(cm.get("$495", "")) == "kindle_title_metadata":
                for kv in cm.get("$258", []):
                    k = _str(kv.get("$492", ""))
                    if k in ("cover_image", "title", "cde_content_type"):
                        v = kv.get("$307", "")
                        kt_rows.append((k, _str(v)[:120]))

    f258 = b.fragments.get("$258")
    top424 = None
    if f258:
        for k in f258.value.keys():
            if _str(k) == "$424":
                top424 = _str(f258.value[k])
                break

    # $164 for cover_image id
    cover164 = None
    cover417 = None
    if cov_id:
        fr = b.fragments.get(ftype="$164", fid=cov_id)
        if fr is not None:
            cover164 = {str(k): _str(v)[:80] for k, v in list(fr.value.items())[:15]}
            loc = fr.value.get("$165")
            if loc is not None:
                blob = b.fragments.get(ftype="$417", fid=loc)
                if blob is not None and hasattr(blob.value, "__len__"):
                    cover417 = len(blob.value)

    # $270 $181 pairs (type_id, sym)
    f270 = b.fragments.get("$270")
    pairs = []
    if f270 and "$181" in f270.value:
        sym = b.symtab.get_symbol
        for row in f270.value["$181"][:30]:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                pairs.append((row[0], _str(sym(row[1]) if isinstance(row[1], int) else row[1])))

    f419 = b.fragments.get("$419", first=True)
    cem181_sets: list[set] = []
    if f419:
        for block in f419.value.get("$252", []):
            ids = block.get("$181", [])
            s = set(_str(x) for x in ids)
            cem181_sets.append(s)

    print("===", path.name, "===")
    print("cover_image (meta):", cov_id, type(cov_id).__name__)
    print("get_cover_image_data:", "OK" if cov_data else "NONE", "bytes", len(cov_data[1]) if cov_data else 0)
    print("kindle_title:", kt_rows)
    print("top $258 $424:", top424)
    print("$164 for cover id:", "found" if cover164 else "MISSING", cover164)
    print("$417 blob len:", cover417)
    print("$270 $181 sample (first 8):", pairs[:8], "total", len(pairs) if f270 and "$181" in f270.value else 0)
    print("$419 $181 set sizes:", [len(s) for s in cem181_sets])

    try:
        loc = b.locate_cover_image_resource_from_content()
    except Exception as e:
        loc = f"<err {e}>"
    print("locate_cover_image_resource_from_content:", loc, "vs meta", cov_id)

    if fr := (b.fragments.get(ftype="$164", fid=cov_id) if cov_id else None):
        print("cover $164 all keys:", sorted(str(k) for k in fr.value.keys()))

    if cov_id and cover164 is None:
        # list any $164 whose $175 matches or short id
        hits = []
        for fr in b.fragments.get_all("$164"):
            name = _str(fr.value.get("$175", ""))
            if name == _str(cov_id) or fr.fid == cov_id:
                hits.append((_str(fr.fid), name))
        print("fragments $164 matching name/fid:", hits[:5])


def main() -> None:
    base = Path(__file__).resolve().parents[1]
    for name in sys.argv[1:] or ["hascover.kfx"]:
        p = base / name
        if not p.is_file():
            print("skip missing:", p)
            continue
        analyze(p)
        print()


if __name__ == "__main__":
    main()
