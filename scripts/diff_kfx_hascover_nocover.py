#!/usr/bin/env python3
"""详细对比两个 KFX（默认可选 hascover.kfx / nocover4.kfx）：元数据、片段统计、差异键。"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import types
from collections import Counter
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
from kckfxgen.kfxlib.ion import ion_type


def _str(x: object) -> str:
    if x is None:
        return ""
    return str(x)


def load(path: Path) -> YJ_Book:
    set_logger(logging.getLogger("silent"))
    logging.getLogger("silent").setLevel(logging.CRITICAL + 1)
    b = YJ_Book(str(path))
    b.decode_book()
    return b


def ftype_counts(book: YJ_Book) -> Counter[str]:
    c: Counter[str] = Counter()
    for fr in book.fragments:
        c[_str(fr.ftype)] += 1
    return c


def kindle_title_rows(book: YJ_Book) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    f490 = book.fragments.get("$490")
    if not f490:
        return rows
    for cm in f490.value.get("$491", []):
        if _str(cm.get("$495", "")) != "kindle_title_metadata":
            continue
        for kv in cm.get("$258", []):
            k = _str(kv.get("$492", ""))
            v = kv.get("$307", "")
            rows.append((k, _str(v)))
        rows.sort(key=lambda t: t[0])
        break
    return rows


def metadata_258_dump(book: YJ_Book) -> dict[str, str]:
    out: dict[str, str] = {}
    f258 = book.fragments.get("$258")
    if not f258:
        return out
    for k, v in f258.value.items():
        sk = _str(k)
        if ion_type(v).__name__ in ("IonStruct", "IonList") or (
            isinstance(v, (list, dict)) and len(_str(v)) > 200
        ):
            out[sk] = f"<{ion_type(v).__name__} len={len(v) if hasattr(v,'__len__') else '?'}>"
        else:
            out[sk] = _str(v)[:500]
    return dict(sorted(out.items()))


def container_270(book: YJ_Book) -> dict[str, str]:
    fr = book.fragments.get("$270")
    if not fr:
        return {}
    v = fr.value
    out: dict[str, str] = {}
    for k in sorted(v.keys(), key=_str):
        val = v[k]
        sk = _str(k)
        if sk == "$181":
            out[sk] = f"<list len={len(val)}>"
            continue
        out[sk] = _str(val)[:200]
    return out


def cover_blob_head(book: YJ_Book, n: int = 32) -> str:
    cid = book.get_metadata_value("cover_image")
    if not cid:
        return ""
    data = book.get_cover_image_data()
    if not data:
        return "no binary"
    b = data[1][:n]
    return b.hex()


def main() -> None:
    base = Path(__file__).resolve().parents[1]
    a = base / (sys.argv[1] if len(sys.argv) > 1 else "hascover.kfx")
    bpath = base / (sys.argv[2] if len(sys.argv) > 2 else "nocover4.kfx")
    if not a.is_file() or not bpath.is_file():
        print("用法: python diff_kfx_hascover_nocover.py [A.kfx] [B.kfx]")
        print("缺失文件:", a, bpath)
        sys.exit(1)

    ba, bb = load(a), load(bpath)
    names = (a.name, bpath.name)

    print("=" * 72)
    print("文件大小（字节）")
    print(f"  {names[0]}: {a.stat().st_size}")
    print(f"  {names[1]}: {bpath.stat().st_size}")

    print("\n片段类型计数（仅列出数量不同的 ftype）")
    ca, cb = ftype_counts(ba), ftype_counts(bb)
    all_t = sorted(set(ca) | set(cb))
    diff_rows = [(t, ca[t], cb[t]) for t in all_t if ca[t] != cb[t]]
    if not diff_rows:
        print("  （各 ftype 数量一致）")
    for t, na, nb in diff_rows:
        print(f"  {t}: {names[0]}={na}  {names[1]}={nb}")

    print("\nkindle_title_metadata（$492 -> $307，按 key 排序）")
    ra, rb = kindle_title_rows(ba), kindle_title_rows(bb)
    print(f"  [{names[0]}]")
    for k, v in ra:
        print(f"    {k}: {v[:120]}{'…' if len(v) > 120 else ''}")
    print(f"  [{names[1]}]")
    for k, v in rb:
        print(f"    {k}: {v[:120]}{'…' if len(v) > 120 else ''}")
    sa, sb = {x[0] for x in ra}, {x[0] for x in rb}
    print("  仅 A 有 key:", sorted(sa - sb) or "无")
    print("  仅 B 有 key:", sorted(sb - sa) or "无")
    common = sa & sb
    for k in sorted(common):
        va = next(x[1] for x in ra if x[0] == k)
        vb = next(x[1] for x in rb if x[0] == k)
        if va != vb:
            print(f"  同 key 值不同 [{k}]:\n    A: {va[:200]}\n    B: {vb[:200]}")

    print("\n顶层 metadata 片段 $258（键 -> 摘要）")
    ma, mb = metadata_258_dump(ba), metadata_258_dump(bb)
    print(f"  [{names[0]}] keys: {list(ma.keys())}")
    for k, v in ma.items():
        print(f"    {k}: {v}")
    print(f"  [{names[1]}] keys: {list(mb.keys())}")
    for k, v in mb.items():
        print(f"    {k}: {v}")
    print("  仅 A 有 $258 键:", sorted(set(ma) - set(mb)) or "无")
    print("  仅 B 有 $258 键:", sorted(set(mb) - set(ma)) or "无")

    print("\n封面（元数据 cover_image + $164 + JPEG 头）")
    for book, nm in ((ba, names[0]), (bb, names[1])):
        cid = book.get_metadata_value("cover_image")
        cov = book.get_cover_image_data()
        print(f"  [{nm}]")
        print(f"    cover_image: {cid!r} ({type(cid).__name__})")
        print(f"    get_cover_image_data: {'OK' if cov else 'NONE'} bytes={len(cov[1]) if cov else 0}")
        if cid and cov:
            fr = book.fragments.get(ftype="$164", fid=cid)
            if fr:
                keys = sorted(_str(k) for k in fr.value.keys())
                print(f"    $164 keys: {keys}")
                for kk in sorted(fr.value.keys(), key=_str):
                    print(f"      {_str(kk)}: {fr.value[kk]}")
        print(f"    JPEG 前 32 字节 hex: {cover_blob_head(book, 32)}")

    print("\n主容器 $270（节选，不含长 $181 列表）")
    ta, tb = container_270(ba), container_270(bb)
    keys = sorted(set(ta) | set(tb))
    for k in keys:
        va, vb = ta.get(k, "<无>"), tb.get(k, "<无>")
        if va != vb:
            print(f"  {k}:\n    A: {va}\n    B: {vb}")

    print("\nlocate_cover_image_resource_from_content()")
    print(f"  A: {ba.locate_cover_image_resource_from_content()}")
    print(f"  B: {bb.locate_cover_image_resource_from_content()}")

    print("\n$593 format_capabilities（条数与前几项）")
    for book, nm in ((ba, names[0]), (bb, names[1])):
        f593 = book.fragments.get("$593")
        if not f593:
            print(f"  [{nm}] 无 $593")
            continue
        val = f593.value
        lst = val if isinstance(val, list) else list(val)
        print(f"  [{nm}] count={len(lst)}")
        for item in lst[:8]:
            print(f"    {item}")

    print("\n$419 entity map 块数与首块 $181 元素个数")
    for book, nm in ((ba, names[0]), (bb, names[1])):
        f419 = book.fragments.get("$419", first=True)
        if not f419:
            print(f"  [{nm}] 无 $419")
            continue
        blocks = f419.value.get("$252", [])
        lens = [len(b.get("$181", [])) for b in blocks]
        print(f"  [{nm}] blocks={len(blocks)} $181 sizes={lens}")

    print("=" * 72)


if __name__ == "__main__":
    main()
