# SPDX-License-Identifier: GPL-3.0-or-later
# EPUB helpers derived from kpfgen (https://github.com/xxyzz/kpfgen) epub.py

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin

from lxml import etree

logger = logging.getLogger(__name__)

NAMESPACES = {
    "n": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "xml": "http://www.w3.org/1999/xhtml",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _local(tag: str | None) -> str:
    if not tag:
        return ""
    return tag.split("}", 1)[-1]


def extract_epub(epub_path: Path, dest: Path) -> None:
    logger.debug("[epub_collect] 解压: %s -> %s", epub_path, dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path) as zf:
        n = len(zf.namelist())
        logger.debug("[epub_collect] ZIP 内条目数: %d", n)
        zf.extractall(dest)
    logger.debug("[epub_collect] 解压完成")


@dataclass
class EPUBMetadata:
    language: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    publisher: str = ""
    cover_path: Path | None = None
    spine_paths: list[Path] = field(default_factory=list)
    toc: Path | None = None
    # 供 collect_ordered_images 复用，避免再次查找/解析 OPF
    opf_path: Path | None = None


# 连续 [a][b]… 文件名中可跳过的首部类型/分区标签（小写比对）
_COMIC_STEM_LEADING_TAGS = frozenset(
    {
        "comic",
        "manga",
        "manhua",
        "漫画",
        "漫畫",
        "コミック",
    }
)


def _strip_trailing_corner_notes(s: str) -> str:
    """去掉文件名末尾「【…】」类标注（如汉化说明、单页版）。"""
    t = s.strip()
    while True:
        u = re.sub(r"【[^】]*】\s*$", "", t).strip()
        if u == t:
            break
        t = u
    return t


def _split_leading_bracket_tags(s: str) -> tuple[list[str], str]:
    """
    从串首解析连续的 ``[…][…]…``，返回 ``(各括号内文本, 剩余尾部)``。
    若首字符不是 ``[``，则 ``([], 原串)``。
    """
    s = s.lstrip()
    if not s.startswith("["):
        return [], s
    tags: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "[":
            break
        j = s.find("]", i + 1)
        if j < 0:
            break
        tags.append(s[i + 1 : j].strip())
        i = j + 1
        while i < n and s[i].isspace():
            i += 1
    return tags, s[i:].strip()


def _normalize_volume_tail(tail: str) -> str:
    """将 ``Vol_01``、``vol 3`` 等卷标整理为可读形式（保留卷号前导零）。"""
    x = tail.strip()
    if not x:
        return ""
    m = re.match(r"(?i)^vol[_\s]*(\d+)$", x)
    if m:
        return f"Vol.{m.group(1)}"
    return x


def _is_volume_like_part(text: str) -> bool:
    """判断 ``text`` 是否 mainly 卷号/话数（应用作书名延续而非作者名）。"""
    t = text.strip()
    if not t:
        return False
    if re.match(r"^卷\s*[\d０-９一二三四五六七八九十百千]+$", t):
        return True
    if re.match(r"^第\s*[\d０-９一二三四五六七八九十百千]+\s*[卷册冊話话回]$", t):
        return True
    if re.match(r"^[\d０-９]+\s*[卷册冊](?:[._\s\-上中下篇]*)?$", t):
        return True
    if re.match(r"(?i)^v(?:ol)?[._\s]*\d+$", t):
        return True
    if re.search(r"(?i)vol\.?\s*\d", t) and len(t) <= 32:
        return True
    return False


def _parse_multi_bracket_stem(s: str) -> tuple[str, str, str] | None:
    """处理串首连续 ``[…][…]…``（至少两个括号块），如 ``[Comic][书名][作者]…`` 及尾部卷标。"""
    tags, rest = _split_leading_bracket_tags(s)
    if len(tags) < 2:
        return None
    while tags and tags[0].strip().lower() in _COMIC_STEM_LEADING_TAGS:
        tags = tags[1:]
    if not tags:
        return None
    if len(tags) >= 3:
        title, author = tags[0], tags[1]
        publisher = " · ".join(tags[2:])
    elif len(tags) == 2:
        title, author = tags[0], tags[1]
        publisher = ""
    else:
        title, author, publisher = tags[0], "", ""
    if rest:
        title = f"{title} {_normalize_volume_tail(rest)}".strip()
    return title, author, publisher


def _merge_single_bracket_and_rest(tags: list[str], rest: str) -> tuple[str, str, str] | None:
    """``[书名]`` 后接 ``- 卷N`` 等：并入书名，不设作者。"""
    if len(tags) != 1 or not rest:
        return None
    base = tags[0].strip()
    r = rest.strip()
    m = re.match(r"^[—–\-]\s*(.+)$", r)
    if m:
        right = m.group(1).strip()
        if _is_volume_like_part(right):
            return f"{base} - {right}", "", ""
    if _is_volume_like_part(r):
        return f"{base} {_normalize_volume_tail(r)}", "", ""
    return None


def parse_comic_archive_stem(stem: str) -> tuple[str, str, str]:
    """
    从漫画压缩包主文件名（无扩展名）解析 ``(title, author, publisher)``。

    规则（按顺序）：

    1. 去掉尾部 ``【…】``（如画质/版式说明）。
    2. 若串首为连续 ``[…][…]…``（至少两段有效内容）：跳过常见类型标签
       （如 Comic、漫画）；依次为书名、作者、(余下标签以 `` · `` 连接作为出版社)；
       若最后一个 ``]`` 后还有 ``Vol_01`` 等卷标则并入书名。
    3. 若仅一段 ``[书名]`` 且其后为 ``- 卷N`` 等卷信息：整段作为书名，作者为空。
    4. 否则：末尾 ``[…]`` 或 ``(…)`` → 出版社；剩余中 `` — / – / - ``（两侧空格）
       拆书名与作者；若右侧仅为卷号样式则不与作者拆分，整段作为书名。
    """
    s = _strip_trailing_corner_notes(stem.strip())
    if not s:
        return "", "", ""

    got = _parse_multi_bracket_stem(s)
    if got is not None:
        return got

    tags, rest = _split_leading_bracket_tags(s)
    merged = _merge_single_bracket_and_rest(tags, rest)
    if merged is not None:
        return merged

    publisher = ""
    work = s
    m = re.search(r"\[([^\]]+)\]\s*$", work)
    if m:
        publisher = m.group(1).strip()
        work = work[: m.start()].rstrip()
    else:
        m2 = re.search(r"\(([^)]+)\)\s*$", work)
        if m2:
            publisher = m2.group(1).strip()
            work = work[: m2.start()].rstrip()

    title, author = work, ""
    for sep in (" — ", " – ", " - "):
        if sep in work:
            left, right = work.split(sep, 1)
            left, right = left.strip(), right.strip()
            if _is_volume_like_part(right):
                title = f"{left}{sep}{right}".strip()
                author = ""
            else:
                title, author = left, right
            break

    return title, author, publisher


def metadata_from_comic_archive_stem(stem: str, *, language: str = "zh") -> EPUBMetadata:
    t, a, p = parse_comic_archive_stem(stem)
    return EPUBMetadata(title=t, author=a, publisher=p, language=language)


def metadata_from_stem(stem: str, *, language: str = "zh") -> EPUBMetadata:
    return metadata_from_comic_archive_stem(stem, language=language)


def apply_metadata_overrides(
    meta: EPUBMetadata,
    *,
    title: str | None = None,
    author: str | None = None,
    publisher: str | None = None,
) -> None:
    if title is not None:
        meta.title = title
    if author is not None:
        meta.author = author
    if publisher is not None:
        meta.publisher = publisher


def _find_opf_path(epub_dir: Path) -> Path:
    container_root = etree.parse(epub_dir / "META-INF" / "container.xml")
    el = container_root.find(".//n:rootfile", NAMESPACES)
    if el is None:
        raise ValueError("container.xml has no rootfile")
    opf_path_str = unquote(el.get("full-path", ""))
    opf_path = epub_dir / opf_path_str
    if not opf_path.exists():
        found = next(epub_dir.rglob(Path(opf_path_str).name), None)
        if found is None:
            raise FileNotFoundError(opf_path)
        opf_path = found
    return opf_path


def get_epub_metadata(epub_dir: Path) -> EPUBMetadata:
    opf_path = _find_opf_path(epub_dir)
    opf_root = etree.parse(opf_path)
    metadata = EPUBMetadata()
    for element_type in ("language", "title", "description", "publisher"):
        for element in opf_root.iterfind(f"opf:metadata/dc:{element_type}", NAMESPACES):
            if element.text:
                setattr(metadata, element_type, element.text)
    author_element = opf_root.find("opf:metadata/dc:creator", NAMESPACES)
    if author_element is not None and author_element.text:
        metadata.author = author_element.text
    cover_element = opf_root.find('opf:metadata/opf:meta[@name="cover"]', NAMESPACES)
    if cover_element is not None:
        cover_id = cover_element.get("content")
        if cover_id:
            item = opf_root.find(f'opf:manifest/opf:item[@id="{cover_id}"]', NAMESPACES)
            if item is not None and item.get("href"):
                metadata.cover_path = (opf_path.parent / unquote(item.get("href", ""))).resolve()
    for spine_item in opf_root.iterfind("opf:spine/opf:itemref", NAMESPACES):
        manifest_item_id = spine_item.get("idref", "")
        manifest_item = opf_root.find(
            f'opf:manifest/opf:item[@id="{manifest_item_id}"]', NAMESPACES
        )
        if manifest_item is not None and manifest_item.get("href"):
            metadata.spine_paths.append(
                (opf_path.parent / unquote(manifest_item.get("href", ""))).resolve()
            )
    toc_element = opf_root.find('opf:manifest/opf:item[@properties="nav"]', NAMESPACES)
    if toc_element is not None and toc_element.get("href"):
        metadata.toc = (opf_path.parent / unquote(toc_element.get("href", ""))).resolve()
    metadata.opf_path = opf_path
    logger.debug(
        "[epub_collect] OPF=%s title=%r author=%r language=%r spine_items=%d toc=%s cover=%s",
        opf_path,
        metadata.title,
        metadata.author,
        metadata.language,
        len(metadata.spine_paths),
        metadata.toc,
        metadata.cover_path,
    )
    return metadata


def _manifest_image_hrefs_in_order(
    epub_dir: Path, opf_path: Path | None = None
) -> list[Path]:
    opf_path = opf_path or _find_opf_path(epub_dir)
    opf_root = etree.parse(opf_path)
    base = opf_path.parent
    out: list[Path] = []
    for item in opf_root.iterfind("opf:manifest/opf:item", NAMESPACES):
        mt = (item.get("media-type") or "").lower()
        href = item.get("href")
        if not href:
            continue
        if mt.startswith("image/") or mt == "image/svg+xml":
            p = (base / unquote(href)).resolve()
            out.append(p)
    return out


_SRCSET_FIRST = re.compile(r"^\s*([^\s,]+)")


def _imgs_from_html_file(html_path: Path) -> list[Path]:
    if not html_path.is_file():
        return []
    try:
        tree = etree.parse(html_path, etree.XMLParser(recover=True))
    except etree.XMLSyntaxError:
        return []
    base_url = html_path.parent.resolve().as_uri() + "/"
    found: list[Path] = []

    for el in tree.iter():
        tag = _local(el.tag)
        if tag == "img":
            src = el.get("src")
            if src:
                absu = urljoin(base_url, unquote(src.split("#")[0]))
                if absu.startswith("file:"):
                    raw = absu[7:]
                    if raw.startswith("//"):
                        raw = raw[1:]
                    p = Path(unquote(raw)).resolve()
                    if p.is_file():
                        found.append(p)
        elif tag == "source":
            src = el.get("src")
            if src:
                absu = urljoin(base_url, unquote(src.split("#")[0]))
                if absu.startswith("file:"):
                    raw = absu[7:]
                    if raw.startswith("//"):
                        raw = raw[1:]
                    p = Path(unquote(raw)).resolve()
                    if p.is_file():
                        found.append(p)
            srcset = el.get("srcset")
            if srcset:
                m = _SRCSET_FIRST.match(srcset)
                if m:
                    absu = urljoin(base_url, unquote(m.group(1).split("#")[0]))
                    if absu.startswith("file:"):
                        raw = absu[7:]
                        if raw.startswith("//"):
                            raw = raw[1:]
                        p = Path(unquote(raw)).resolve()
                        if p.is_file():
                            found.append(p)
    return found


_RASTER_SUFFIX = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".jxr"}


def _is_raster_path(p: Path) -> bool:
    return p.suffix.lower() in _RASTER_SUFFIX


def collect_ordered_images(epub_dir: Path, meta: EPUBMetadata) -> list[Path]:
    """
    Order: images referenced from spine HTML/XHTML in document order, then
    any manifest image items not yet included (manifest order).
    Skips SVG (not handled by PIL pipeline).
    """
    seen: set[str] = set()
    ordered: list[Path] = []

    def add(p: Path) -> None:
        if not p.is_file() or not _is_raster_path(p):
            return
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            ordered.append(p)

    for spine_path in meta.spine_paths:
        if not spine_path.is_file():
            continue
        suf = spine_path.suffix.lower()
        if suf in (".xhtml", ".html", ".htm", ".xml"):
            for p in _imgs_from_html_file(spine_path):
                add(p)
        elif _is_raster_path(spine_path):
            add(spine_path)

    for p in _manifest_image_hrefs_in_order(epub_dir, meta.opf_path):
        add(p)

    logger.debug(
        "[epub_collect] 光栅图收集完成: 共 %d 张（顺序为 spine 内 HTML 引用优先，再 manifest 补漏）",
        len(ordered),
    )
    for i, p in enumerate(ordered, 1):
        logger.debug("[epub_collect]   图 [%d/%d] %s", i, len(ordered), p)

    return ordered
