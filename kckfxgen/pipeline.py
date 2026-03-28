#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""从 EPUB 或漫画压缩包（ZIP/CBZ、RAR/CBR）生成 KPF，再经本包 ``kckfxgen.kfxlib`` 打包为单文件 KFX。

生成的 book.kdf 中 book_metadata 含 kindle_capability_metadata（yj_fixed_layout、
yj_publisher_panels），便于 Kindle 侧识别固定版式漫画；``cde_content_type`` 为 **PDOC**（个人文档），非商店图书 (EBOK)。

对外主流程为 ``convert_to_kfx``（``convert_epub_to_kfx`` 为兼容别名）：
临时 .kpf → ``kpf_to_kfx.kpf_path_to_kfx_file``（``cde_pdoc=True`` / ``KfxContainer.serialize``）→ 目标目录中的 ``书名-作者.kfx``（重名时 ``_2``、``_3`` 后缀）。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from .archive_comic import (
    COMIC_ARCHIVE_SUFFIXES,
    collect_sorted_comic_images,
    extract_comic_archive,
    is_comic_archive_path,
)
from .epub_collect import (
    EPUBMetadata,
    apply_metadata_overrides,
    collect_ordered_images,
    extract_epub,
    get_epub_metadata,
    metadata_from_comic_archive_stem,
)
from .kdf_writer import TOOL_NAME, TOOL_VERSION, ImageKdfWriter
from .kpf_to_kfx import kpf_path_to_kfx_file

logger = logging.getLogger(__name__)


def _cli_verbose() -> bool:
    return logging.getLogger().getEffectiveLevel() <= logging.DEBUG


def _safe_kpf_name_stem(stem: str, *, max_len: int = 100) -> str:
    """Windows / 预览器安全的主文件名（不含扩展名），避免非法字符导致异常输出名。"""
    forbidden = '<>:"/\\|?*'
    s = "".join("_" if c in forbidden or ord(c) < 32 else c for c in stem)
    s = s.strip(" .")
    if not s:
        s = "book"
    return s[:max_len]


def _unique_filename_in_dir(directory: Path, filename: str) -> Path:
    """若 ``directory/filename`` 已存在，则依次尝试 ``stem_2.ext``、``stem_3``…"""
    directory = directory.resolve()
    p = directory / filename
    if not p.exists():
        return p
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    for n in range(2, 10_000):
        cand = directory / f"{stem}_{n}{suffix}"
        if not cand.exists():
            return cand
    raise OSError(f"无法为 {filename!r} 在目录内分配唯一文件名: {directory}")


def _kfx_basename_title_author(
    input_path: Path,
    *,
    book_title: str | None,
    book_author: str | None,
    book_publisher: str | None,
) -> str:
    """与 ``epub_to_kpf`` / ``comic_archive_to_kpf`` 一致的有效书名、作者，生成 ``书名-作者.kfx`` 主文件名。"""
    suf = input_path.suffix.lower()
    if suf == ".epub":
        with tempfile.TemporaryDirectory() as tmp:
            epub_root = Path(tmp) / "epub"
            extract_epub(input_path, epub_root)
            meta = get_epub_metadata(epub_root)
            apply_metadata_overrides(
                meta,
                title=book_title,
                author=book_author,
                publisher=book_publisher,
            )
    else:
        meta = metadata_from_comic_archive_stem(input_path.stem)
        apply_metadata_overrides(
            meta,
            title=book_title,
            author=book_author,
            publisher=book_publisher,
        )

    title_part = (meta.title or "").strip() or input_path.stem
    author_part = (meta.author or "").strip() or "Unknown"
    stem = (
        f"{_safe_kpf_name_stem(title_part, max_len=100)}-"
        f"{_safe_kpf_name_stem(author_part, max_len=100)}"
    )
    return f"{stem}.kfx"


# 与 Kindle Create 漫画工程一致（参见 book_state，固定版式 + 漫画输入/目标类型）
_COMIC_BOOK_STATE = {
    "book_fl_type": 1,
    "book_input_type": 4,
    "book_reading_direction": 1,
    "book_reading_option": 1,
    "book_target_type": 3,
    "book_virtual_panelmovement": 0,
}


def _write_kcb(kpf_dir: Path) -> None:
    kcb_path = kpf_dir / "book.kcb"
    logger.debug("写入 book.kcb: %s", kcb_path)
    with kcb_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "book_state": _COMIC_BOOK_STATE,
                "content_hash": None,
                "metadata": {
                    "book_path": "resources",
                    "edited_tool_versions": [TOOL_VERSION],
                    "format": "yj",
                    "global_styling": True,
                    "tool_name": TOOL_NAME,
                    "tool_version": TOOL_VERSION,
                },
            },
            f,
            indent=2,
            ensure_ascii=False,
        )


def _write_manifest(resources_dir: Path) -> None:
    manifest_path = resources_dir / "ManifestFile"
    logger.debug("写入 ManifestFile: %s", manifest_path)
    manifest_path.write_text(
        """AmazonYJManifest
digital_content_manifest::{
  version:1,
  storage_type:"localSqlLiteDB",
  digital_content_name:"book.kdf"
}
""",
        encoding="utf-8",
    )


def _images_to_kpf_zip(
    tmp_path: Path,
    images: list[Path],
    meta: EPUBMetadata,
    dest: Path,
    *,
    split_spreads: bool,
    split_page_order: Literal["right-left", "left-right"],
    portrait_cover: bool = False,
    rotate_landscape_90: bool = False,
) -> None:
    """在已存在的 ``tmp_path`` 工作目录内生成 KPF 目录树并打包为 ``dest``（.kpf ZIP）。

    ``portrait_cover=True``（漫画压缩包）时，``kindle_title_metadata.cover_image`` 指向首张竖屏图对应资源，阅读顺序不变。
    ``rotate_landscape_90=True`` 时，宽>高 的页在写入 KDF 前逆时针旋转 90°，以竖屏展示。
    """
    if split_spreads:
        from .spread_split import expand_spread_pages

        logger.debug("环节: 双页裁切（空白中缝检测 + 正中切，顺序=%s）", split_page_order)
        split_dir = tmp_path / "_kckfxgen_split"
        images = expand_spread_pages(
            images,
            split_dir,
            page_order=split_page_order,
        )
    if not images:
        raise ValueError("没有可用光栅图片，无法生成 KDF。")
    logger.debug("环节: 最终参与生成 KDF 的图片共 %d 张", len(images))

    kpf_dir = tmp_path / "kpf"
    kpf_dir.mkdir()
    resources = kpf_dir / "resources"
    resources.mkdir()
    logger.debug(
        "环节: 创建 KPF 布局 kpf_dir=%s resources=%s",
        kpf_dir,
        resources,
    )
    db_path = resources / "book.kdf"
    logger.debug("环节: 生成 book.kdf -> %s", db_path)
    writer = ImageKdfWriter(meta)
    writer.create_kdf(
        tmp_path,
        db_path,
        images,
        cover_from_first_portrait=portrait_cover,
        rotate_landscape_90=rotate_landscape_90,
    )
    _write_kcb(kpf_dir)
    _write_manifest(resources)

    archive_stem = tmp_path / "out"
    logger.debug("环节: 将 kpf 目录打包为 ZIP -> %s.zip", archive_stem)
    shutil.make_archive(str(archive_stem), "zip", kpf_dir)
    zip_path = tmp_path / "out.zip"
    if zip_path.exists():
        logger.debug("环节: 移动 ZIP 到最终输出 -> %s", dest)
        zip_path.replace(dest)
    else:
        raise RuntimeError(f"打包失败，未找到 {zip_path}")


def epub_to_kpf(
    epub_path: Path,
    output_kpf: Path | None = None,
    *,
    split_spreads: bool = False,
    split_page_order: Literal["right-left", "left-right"] = "right-left",
    rotate_landscape_90: bool = False,
    book_title: str | None = None,
    book_author: str | None = None,
    book_publisher: str | None = None,
) -> Path:
    """将 EPUB 打成 .kpf（ZIP）。``output_kpf`` 为 ``None`` 时在系统临时目录创建临时文件，调用方须自行删除。"""
    epub_path = epub_path.expanduser().resolve()
    logger.debug("环节: 校验输入 EPUB 路径 -> %s", epub_path)
    if not epub_path.is_file():
        raise FileNotFoundError(epub_path)
    if output_kpf is None:
        fd, name = tempfile.mkstemp(suffix=".kpf", prefix="kckfxgen_")
        os.close(fd)
        dest = Path(name)
    else:
        dest = output_kpf.expanduser().resolve()
    logger.debug("环节: 输出 KPF 路径 -> %s", dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        logger.debug("环节: 创建临时目录 -> %s", tmp_path)
        epub_root = tmp_path / "epub"
        logger.debug("环节: 解压 EPUB 到 -> %s", epub_root)
        extract_epub(epub_path, epub_root)
        logger.debug("环节: 读取 OPF 与 EPUB 元数据")
        meta = get_epub_metadata(epub_root)
        apply_metadata_overrides(
            meta,
            title=book_title,
            author=book_author,
            publisher=book_publisher,
        )
        logger.debug("环节: 按 spine/manifest 收集光栅图列表")
        images = collect_ordered_images(epub_root, meta)
        if meta.cover_path and meta.cover_path.is_file():
            cpr = meta.cover_path.resolve()
            if all(p.resolve() != cpr for p in images):
                logger.debug("环节: 将封面插到列表首位 -> %s", meta.cover_path)
                images.insert(0, meta.cover_path)
            else:
                logger.debug("环节: 封面已在图列表中，跳过前置插入")
        else:
            logger.debug("环节: 无有效封面路径，跳过前置插入")
        if not images:
            raise ValueError(
                "未发现可用光栅图片。请确认 EPUB 内含 jpg/png/webp 等，且出现在 spine 或 manifest 中。"
            )
        _images_to_kpf_zip(
            tmp_path,
            images,
            meta,
            dest,
            split_spreads=split_spreads,
            split_page_order=split_page_order,
            rotate_landscape_90=rotate_landscape_90,
        )

    logger.debug("环节: 临时目录已清理，流程结束")
    return dest


def comic_archive_to_kpf(
    archive_path: Path,
    output_kpf: Path | None = None,
    *,
    split_spreads: bool = False,
    split_page_order: Literal["right-left", "left-right"] = "right-left",
    rotate_landscape_90: bool = False,
    book_title: str | None = None,
    book_author: str | None = None,
    book_publisher: str | None = None,
) -> Path:
    """将 ZIP/CBZ 或 RAR/CBR 漫画包打成 .kpf。图片顺序为包内路径自然序。"""
    archive_path = archive_path.expanduser().resolve()
    logger.debug("环节: 校验输入漫画包 -> %s", archive_path)
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    if not is_comic_archive_path(archive_path):
        raise ValueError(
            f"非支持的漫画压缩格式: {archive_path.suffix}（支持 {', '.join(sorted(COMIC_ARCHIVE_SUFFIXES))}）"
        )
    if output_kpf is None:
        fd, name = tempfile.mkstemp(suffix=".kpf", prefix="kckfxgen_")
        os.close(fd)
        dest = Path(name)
    else:
        dest = output_kpf.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        comic_root = tmp_path / "comic"
        logger.debug("环节: 解压漫画包到 -> %s", comic_root)
        extract_comic_archive(archive_path, comic_root)
        meta = metadata_from_comic_archive_stem(archive_path.stem)
        apply_metadata_overrides(
            meta,
            title=book_title,
            author=book_author,
            publisher=book_publisher,
        )
        logger.debug(
            "环节: 图书元数据 title=%r author=%r publisher=%r",
            meta.title,
            meta.author,
            meta.publisher,
        )
        logger.debug("环节: 按路径自然序收集光栅图")
        images = collect_sorted_comic_images(comic_root)
        if not images:
            raise ValueError(
                "压缩包内未发现可用光栅图（jpg/png/webp 等）。请确认包内为图片文件而非仅嵌套压缩包。"
            )
        _images_to_kpf_zip(
            tmp_path,
            images,
            meta,
            dest,
            split_spreads=split_spreads,
            split_page_order=split_page_order,
            portrait_cover=True,
            rotate_landscape_90=rotate_landscape_90,
        )

    logger.debug("环节: 漫画包临时目录已清理")
    return dest


def convert_to_kfx(
    input_path: Path,
    kfx_output_dir: Path | None = None,
    *,
    split_spreads: bool = False,
    split_page_order: Literal["right-left", "left-right"] = "right-left",
    rotate_landscape_90: bool = False,
    book_title: str | None = None,
    book_author: str | None = None,
    book_publisher: str | None = None,
) -> Path:
    """根据扩展名选择 EPUB 或漫画压缩包流程，生成单文件 KFX。返回写出目录。"""
    input_path = input_path.expanduser().resolve()
    out_dir = (kfx_output_dir or input_path.parent).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp_kpf_parent = Path(tempfile.mkdtemp(prefix="kckfxgen_kpf_"))
    stem_safe = _safe_kpf_name_stem(input_path.stem)
    tmp_kpf = tmp_kpf_parent / f"{stem_safe}_{secrets.token_hex(4)}.kpf"
    kfx_name = _kfx_basename_title_author(
        input_path,
        book_title=book_title,
        book_author=book_author,
        book_publisher=book_publisher,
    )
    out_kfx = _unique_filename_in_dir(out_dir, kfx_name)
    try:
        if not _cli_verbose():
            logger.info("处理「%s」…", input_path.stem)
        suf = input_path.suffix.lower()
        if suf == ".epub":
            epub_to_kpf(
                input_path,
                tmp_kpf,
                split_spreads=split_spreads,
                split_page_order=split_page_order,
                rotate_landscape_90=rotate_landscape_90,
                book_title=book_title,
                book_author=book_author,
                book_publisher=book_publisher,
            )
        elif suf in COMIC_ARCHIVE_SUFFIXES:
            comic_archive_to_kpf(
                input_path,
                tmp_kpf,
                split_spreads=split_spreads,
                split_page_order=split_page_order,
                rotate_landscape_90=rotate_landscape_90,
                book_title=book_title,
                book_author=book_author,
                book_publisher=book_publisher,
            )
        else:
            raise ValueError(
                f"不支持的扩展名: {input_path.suffix}（支持 .epub、"
                f"{', '.join(sorted(COMIC_ARCHIVE_SUFFIXES))}）"
            )
        logger.debug("中间 KPF: %s", tmp_kpf)
        if not _cli_verbose():
            logger.info("KPF → KFX（kfxlib / KfxContainer）…")
        kpf_path_to_kfx_file(tmp_kpf, out_kfx, cde_pdoc=True)
    finally:
        try:
            tmp_kpf.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(tmp_kpf_parent, ignore_errors=True)

    logger.info(
        "「%s」完成 → %s",
        input_path.stem,
        out_kfx,
        extra={"cli_style": "success"},
    )
    return out_dir


def convert_epub_to_kfx(
    epub_path: Path,
    kfx_output_dir: Path | None = None,
    *,
    split_spreads: bool = False,
    split_page_order: Literal["right-left", "left-right"] = "right-left",
    rotate_landscape_90: bool = False,
    book_title: str | None = None,
    book_author: str | None = None,
    book_publisher: str | None = None,
) -> Path:
    """兼容旧名：与 ``convert_to_kfx`` 相同，输入可为 ``.epub`` 或漫画 ``.zip/.cbz/.rar/.cbr``。"""
    return convert_to_kfx(
        epub_path,
        kfx_output_dir,
        split_spreads=split_spreads,
        split_page_order=split_page_order,
        rotate_landscape_90=rotate_landscape_90,
        book_title=book_title,
        book_author=book_author,
        book_publisher=book_publisher,
    )
