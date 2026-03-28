# SPDX-License-Identifier: GPL-3.0-or-later
"""ZIP/CBZ、RAR/CBR 等漫画压缩包：安全解压与按路径自然序收集光栅图。"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path

from .epub_collect import _is_raster_path

logger = logging.getLogger(__name__)

COMIC_ARCHIVE_SUFFIXES = frozenset({".zip", ".cbz", ".rar", ".cbr"})


def is_comic_archive_path(path: Path) -> bool:
    return path.suffix.lower() in COMIC_ARCHIVE_SUFFIXES


def _natural_key(s: str) -> list[str | int]:
    out: list[str | int] = []
    for part in re.split(r"(\d+)", s):
        if part.isdigit():
            out.append(int(part))
        elif part:
            out.append(part.lower())
    return out


def _skip_path_parts(rel: Path) -> bool:
    parts_lower = {p.lower() for p in rel.parts}
    if "__macosx" in parts_lower:
        return True
    if ".ds_store" in parts_lower:
        return True
    for p in rel.parts:
        if p.startswith("._"):
            return True
    return False


def collect_sorted_comic_images(root: Path) -> list[Path]:
    root = root.resolve()
    found: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or not _is_raster_path(p):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if _skip_path_parts(rel):
            continue
        found.append(p)

    found.sort(
        key=lambda p: [_natural_key(x) for x in p.relative_to(root).parts]
    )
    logger.debug(
        "[archive_comic] 自压缩包收集光栅图: 共 %d 张（路径自然序）",
        len(found),
    )
    for i, p in enumerate(found, 1):
        logger.debug("[archive_comic]   图 [%d/%d] %s", i, len(found), p)
    return found


def _safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        if info.filename.startswith("/") or ".." in Path(info.filename).parts:
            logger.debug("[archive_comic] 跳过可疑 ZIP 项: %s", info.filename)
            continue
        target = (dest / info.filename).resolve()
        try:
            target.relative_to(dest)
        except ValueError:
            logger.debug("[archive_comic] 跳过越界 ZIP 项: %s", info.filename)
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)


def extract_comic_archive(archive: Path, dest: Path) -> None:
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    suf = archive.suffix.lower()

    if suf in (".zip", ".cbz"):
        logger.debug("[archive_comic] ZIP 解压: %s -> %s", archive, dest)
        with zipfile.ZipFile(archive) as zf:
            _safe_extract_zip(zf, dest)
        return

    if suf in (".rar", ".cbr"):
        try:
            import rarfile
        except ImportError as e:
            raise ValueError(
                "处理 .rar/.cbr 请先安装: pip install rarfile；"
                "并配置 UnRAR（见 https://www.rarlab.com/rar_add.htm ）"
            ) from e
        logger.debug("[archive_comic] RAR 解压: %s -> %s", archive, dest)
        try:
            with rarfile.RarFile(archive) as rf:
                rf.extractall(dest)
        except rarfile.Error as e:
            raise ValueError(
                "RAR/CBR 解压失败（需可用的 UnRAR；请安装 WinRAR/UnRAR 并配置 rarfile）"
            ) from e
        return

    raise ValueError(f"非支持的漫画压缩格式: {archive.suffix}")
