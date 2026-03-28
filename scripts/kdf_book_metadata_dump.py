#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""从 book.kdf 解码 book_metadata 为可读树状结构。

KDF 中 book_metadata 的 Ion 二进制依赖同文件内前导的 $ion_symbol_table；
且第二个顶层值前的 IVM 会在 amazon.ion 的 managed reader 里重置符号表，
因此需去掉 book_metadata 片段开头的 IVM 再与 $ion_symbol_table 拼接后解码。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from amazon.ion import simpleion
from amazon.ion.core import IonType
from amazon.ion.simple_types import IonPyDecimal, IonPyNull, IonPySymbol, IonPyText
from amazon.ion.symbols import SymbolTableCatalog, shared_symbol_table

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from kckfxgen.yj_symbols import YJ_SYMBOLS

_IVM = b"\xe0\x01\x00\xea"

_FPSIG = b"\xfa\x50\x0a\x5f"
_FP_OFF = 1024
_FP_LEN = 1024
_SKIP = 1024 * 1024


def unwrap_kdf_bytes(raw: bytes) -> bytes:
    if len(raw) < _FP_OFF + 4 or raw[_FP_OFF : _FP_OFF + 4] != _FPSIG:
        return raw
    data = bytearray(raw)
    off = _FP_OFF
    while len(data) >= off + _FP_LEN:
        if data[off : off + 4] != _FPSIG:
            break
        del data[off : off + _FP_LEN]
        off += _SKIP
    return bytes(data)


def _open_unwrapped_sqlite(kdf_path: Path) -> tuple[sqlite3.Connection, str]:
    raw = unwrap_kdf_bytes(kdf_path.read_bytes())
    fd, tmp = tempfile.mkstemp(suffix=".kdf")
    os.write(fd, raw)
    os.close(fd)
    return sqlite3.connect(tmp), tmp


def _blob(conn: sqlite3.Connection, fid: str) -> bytes | None:
    row = conn.execute(
        "SELECT payload_value FROM fragments WHERE id=? AND payload_type='blob'",
        (fid,),
    ).fetchone()
    if not row:
        return None
    v = row[0]
    return bytes(v) if isinstance(v, (bytes, memoryview)) else v.encode("utf-8")


def _catalog_yj_import_only() -> SymbolTableCatalog:
    """与 kdf_writer._insert_ion_symbol_table 中 import 一致：仅 YJ_symbols，不含 conversion 串在共享表。"""
    cat = SymbolTableCatalog()
    cat.register(shared_symbol_table("YJ_symbols", 10, YJ_SYMBOLS))
    return cat


def _ion_key_str(k: Any) -> str:
    if isinstance(k, IonPySymbol):
        t = k.ion_text
        return t if t is not None else str(k)
    if isinstance(k, str):
        return k
    if hasattr(k, "text") and getattr(k, "text", None):
        return str(k.text)
    return str(k)


def ion_to_tree(obj: Any) -> Any:
    """Ion 值 -> JSON 友好树（dict/list/标量）。"""
    if obj is None or isinstance(obj, IonPyNull):
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float, str)):
        return obj
    if isinstance(obj, IonPyDecimal):
        return str(obj)
    if isinstance(obj, IonPyText):
        return str(obj)
    if isinstance(obj, IonPySymbol):
        return obj.ion_text if obj.ion_text is not None else f"symbol::{obj.ion_nickname}"

    t = getattr(obj, "ion_type", None)
    if t is IonType.STRUCT or t == IonType.STRUCT:
        out: dict[str, Any] = {}
        for fk, fv in obj.items():
            out[_ion_key_str(fk)] = ion_to_tree(fv)
        return out
    if t is IonType.LIST or t == IonType.LIST:
        return [ion_to_tree(x) for x in obj]
    if t is IonType.SEXP or t == IonType.SEXP:
        return {"sexp": [ion_to_tree(x) for x in obj]}

    if hasattr(obj, "annotations") and hasattr(obj, "value"):
        ann = getattr(obj, "annotations", None)
        inner = getattr(obj, "value", obj)
        if ann:
            return {
                "_annotations": [str(a) for a in ann],
                "_value": ion_to_tree(inner),
            }
        return ion_to_tree(inner)

    return str(obj)


def decode_book_metadata(conn: sqlite3.Connection) -> Any:
    sym = _blob(conn, "$ion_symbol_table")
    meta = _blob(conn, "book_metadata")
    if not meta:
        raise FileNotFoundError("fragment book_metadata (blob) not found")
    cat = _catalog_yj_import_only()

    if sym:
        meta_body = meta[len(_IVM) :] if meta.startswith(_IVM) else meta
        stream = sym + meta_body
        vals = simpleion.load(io.BytesIO(stream), catalog=cat, single_value=False)
        if not vals:
            raise ValueError("Ion 流无顶层值")
        return ion_to_tree(vals[-1])

    # 无符号表片段时退回单段解码（少见）
    vals = simpleion.load(io.BytesIO(meta), catalog=cat, single_value=False)
    return ion_to_tree(vals[-1])


def print_tree(data: Any, prefix: str = "", is_last: bool = True, name: str = "") -> None:
    branch = "└── " if is_last else "├── "
    ext = "    " if is_last else "│   "
    base = prefix + (ext if name else "")

    if name:
        print(f"{prefix}{branch}{name}: ", end="")

    if isinstance(data, dict):
        print(f"{{ {len(data)} keys }}")
        keys = list(data.keys())
        for i, k in enumerate(keys):
            print_tree(data[k], base, i == len(keys) - 1, str(k))
    elif isinstance(data, list):
        print(f"[ {len(data)} items ]")
        for i, item in enumerate(data):
            print_tree(item, base, i == len(data) - 1, f"[{i}]")
    else:
        print(repr(data))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="从 .kdf 解码 book_metadata，输出树状结构（或 JSON）"
    )
    parser.add_argument(
        "kdf",
        type=Path,
        nargs="?",
        default=Path("book.kdf"),
        help="book.kdf 路径（默认当前目录 book.kdf）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON（UTF-8），而非缩进树",
    )
    parser.add_argument(
        "--ion-text",
        action="store_true",
        help="输出 Ion 文本（带缩进），而非树",
    )
    args = parser.parse_args()

    if not args.kdf.is_file():
        raise SystemExit(f"文件不存在: {args.kdf}")

    conn, tmp = _open_unwrapped_sqlite(args.kdf)
    try:
        tree = decode_book_metadata(conn)
    finally:
        conn.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if args.ion_text:
        conn2, tmp2 = _open_unwrapped_sqlite(args.kdf)
        try:
            sym = _blob(conn2, "$ion_symbol_table")
            meta = _blob(conn2, "book_metadata")
            if not meta:
                raise SystemExit("无 book_metadata")
            cat = _catalog_yj_import_only()
            if sym:
                meta_body = meta[len(_IVM) :] if meta.startswith(_IVM) else meta
                stream = sym + meta_body
                vals = simpleion.load(io.BytesIO(stream), catalog=cat, single_value=False)
            else:
                vals = simpleion.load(io.BytesIO(meta), catalog=cat, single_value=False)
            print(simpleion.dumps(vals[-1], binary=False, indent=" "))
        finally:
            conn2.close()
            try:
                os.unlink(tmp2)
            except OSError:
                pass
    elif args.json:
        print(json.dumps(tree, ensure_ascii=False, indent=2))
    else:
        print_tree(tree, name="book_metadata")


if __name__ == "__main__":
    main()
