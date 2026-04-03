# SPDX-License-Identifier: GPL-3.0-or-later
# KDF / ION writing derived from kpfgen kdf.py (https://github.com/xxyzz/kpfgen)

from __future__ import annotations

import io
import json
import logging
import os
import random
import sqlite3
import string
import tempfile
import time
from pathlib import Path
from typing import Literal

from amazon.ion import simpleion
from amazon.ion.core import IonType
from amazon.ion.simple_types import IonPyDict, IonPyText
from amazon.ion.symbols import SymbolTableCatalog, shared_symbol_table
from PIL import Image

from .epub_collect import EPUBMetadata
from .yj_symbols import YJ_CONVERSION_SYMBOLS, YJ_SYMBOLS

logger = logging.getLogger(__name__)

TOOL_NAME = "kckfxgen"
TOOL_VERSION = "0.1.0"

# 旋转、彩虹纹削弱等路径下 JPEG 必须重编码时使用（Pillow quality 约等于「百分比」；80 兼顾体积与观感）
JPEG_REENCODE_QUALITY = 80

# sqlite_master.sql 须与 kfxlib 的 kpf_container.py 中单行 DDL 完全一致（Calibre KFX Output 用 set 成员比对）
_KDF_SCHEMA_SQL = (
    "CREATE TABLE capabilities(key char(20), version smallint, primary key (key, version)) without rowid;\n"
    "CREATE TABLE fragments(id char(40), payload_type char(10), payload_value blob, primary key (id));\n"
    "CREATE TABLE fragment_properties(id char(40), key char(40), value char(40), primary key (id, key, value)) without rowid;\n"
    "CREATE TABLE gc_fragment_properties(id varchar(40), key varchar(40), value varchar(40), primary key (id, key, value)) without rowid;\n"
    "CREATE TABLE gc_reachable(id varchar(40), primary key (id)) without rowid;\n"
    "INSERT INTO capabilities VALUES('db.schema', 1);\n"
)


def remove_ion_table(binary: bytes) -> bytes:
    return b"\xe0\x01\x00\xea" + binary[36:]


def int_to_base32(num: int) -> str:
    if num == 0:
        return "0"
    symbols = "0123456789ABCDEFGHJKMNPRSTUVWXYZ"
    digits: list[str] = []
    while num > 0:
        digits.append(symbols[num % 32])
        num //= 32
    digits.reverse()
    return "".join(digits)


def _unlink_retry(path: Path, attempts: int = 8, base_delay: float = 0.05) -> None:
    """Windows 上杀毒/索引可能短暂占用目标文件，删除时做有限次重试。"""
    for i in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (i + 1))


def _install_db_from_temp(tmp_path: Path, final_path: Path) -> None:
    """SQLite 关闭后再把临时库原子替换为 book.kdf，避免长时间写入固定名路径被外进程锁住。"""
    final_path = final_path.resolve()
    tmp_path = tmp_path.resolve()
    if final_path.exists():
        _unlink_retry(final_path)
    tmp_path.replace(final_path)


def _first_portrait_cover_index(
    paths: list[Path], *, rotate_landscape_90: bool = False
) -> int:
    """第一张竖屏图（height > width）在列表中的下标；若无竖屏则 0。

    ``rotate_landscape_90`` 为真时，宽>高 的图视为旋转后竖屏，参与「首张竖屏」封面选择。
    """
    for i, p in enumerate(paths):
        try:
            with Image.open(p) as im:
                w, h = im.size
        except OSError as e:
            logger.debug("[kdf] 封面候选跳过（无法读取尺寸）: %s — %s", p, e)
            continue
        effective_portrait = h > w or (rotate_landscape_90 and w > h)
        if effective_portrait:
            logger.debug("[kdf] 选用竖屏图作封面: 下标=%d path=%s (%dx%d)", i, p, w, h)
            return i
    logger.debug("[kdf] 无竖屏图，封面沿用列表首张")
    return 0


def _sniff_jpeg_png_format(raw: bytes, suffix: str) -> str | None:
    """按魔数识别 JPEG / PNG；魔数不明时仅信任 .jpg/.jpeg/.png 后缀。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    s = suffix.lower()
    if s in (".jpg", ".jpeg"):
        return "jpg"
    if s == ".png":
        return "png"
    return None


def _read_raster_passthrough(path: Path) -> tuple[bytes, str, int, int]:
    """读取栅格原文件字节（不重编码），并解析宽高（仅解码元数据/必要扫描）。"""
    raw = path.read_bytes()
    fmt = _sniff_jpeg_png_format(raw, path.suffix)
    if fmt is None:
        raise ValueError(
            f"内页仅支持原样嵌入 JPEG/PNG，且须与文件内容一致: {path}"
        )
    with Image.open(io.BytesIO(raw)) as im:
        w, h = im.size
    return raw, fmt, w, h


def _transpose_rotate_90_ccw(im: Image.Image) -> Image.Image:
    """逆时针 90°（宽图变竖图）；兼容 Pillow 9+ Transpose 与旧常量。"""
    trans = getattr(Image, "Transpose", None)
    if trans is not None:
        return im.transpose(trans.ROTATE_90)
    return im.transpose(Image.ROTATE_90)  # type: ignore[attr-defined]


def _rgb_for_jpeg(im: Image.Image) -> Image.Image:
    """转为 RGB，供 JPEG 保存（RGBA 白底；其它模式 convert）。"""
    if im.mode == "RGBA":
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg.convert("RGB")
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def _encode_rotated_raster(raw: bytes, ion_fmt: str) -> tuple[bytes, int, int]:
    """解码后逆时针 90°，再按 ion_fmt（jpg/png）编码（旋转无法避免重编码）。"""
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        rotated = _transpose_rotate_90_ccw(im).copy()
    w, h = rotated.size
    buf = io.BytesIO()
    if ion_fmt == "jpg":
        _rgb_for_jpeg(rotated).save(
            buf, format="JPEG", quality=JPEG_REENCODE_QUALITY, optimize=False
        )
    else:
        rotated.save(buf, format="PNG", compress_level=3)
    return buf.getvalue(), w, h


def _apply_colorsoft_rainbow_erase_bytes(raw: bytes, ion_fmt: str) -> tuple[bytes, int, int]:
    """对 JPEG/PNG 字节流做 Colorsoft 彩虹纹削弱（频域滤波），并重编码为同格式。"""
    from .rainbow_artifacts_eraser import erase_rainbow_artifacts

    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        is_color = im.mode in ("RGB", "RGBA")
        cleaned = erase_rainbow_artifacts(im, is_color)
    w, h = cleaned.size
    buf = io.BytesIO()
    if ion_fmt == "jpg":
        _rgb_for_jpeg(cleaned).save(
            buf, format="JPEG", quality=JPEG_REENCODE_QUALITY, optimize=False
        )
    elif ion_fmt == "png":
        cleaned.save(buf, format="PNG", compress_level=3)
    else:
        raise ValueError(f"彩虹纹削弱仅支持 jpg/png 重编码，当前: {ion_fmt!r}")
    return buf.getvalue(), w, h


def _load_page_raster_for_kdf(
    image_path: Path,
    *,
    rotate_landscape_90: bool,
    erase_colorsoft_rainbow: bool,
) -> tuple[bytes, str, int, int]:
    image_bytes, ion_fmt, im_width, im_height = _read_raster_passthrough(image_path)
    if erase_colorsoft_rainbow:
        logger.debug("[kdf] Colorsoft 彩虹纹削弱: %s", image_path.name)
        image_bytes, im_width, im_height = _apply_colorsoft_rainbow_erase_bytes(
            image_bytes, ion_fmt
        )
    if rotate_landscape_90 and im_width > im_height:
        image_bytes, im_width, im_height = _encode_rotated_raster(image_bytes, ion_fmt)
        logger.debug(
            "[kdf] 横幅旋转 90°（逆时针）: %s -> %dx%d",
            image_path.name,
            im_width,
            im_height,
        )
    return image_bytes, ion_fmt, im_width, im_height


class ImageKdfWriter:
    def __init__(self, epub_metadata: EPUBMetadata) -> None:
        self.epub_metadata = epub_metadata
        self.symbol_table = shared_symbol_table(
            "YJ_symbols", 10, YJ_SYMBOLS + YJ_CONVERSION_SYMBOLS
        )
        self.catalog = SymbolTableCatalog()
        self.catalog.register(self.symbol_table)
        self.fragment_id = 0
        self.conn: sqlite3.Connection | None = None
        self.res_dir: Path | None = None

    def create_fragment_id(self, prefix: str) -> str:
        fragment_id_str = prefix + int_to_base32(self.fragment_id)
        self.fragment_id += 1
        return fragment_id_str

    def create_kdf(
        self,
        _work_root: Path,
        db_path: Path,
        image_paths: list[Path],
        *,
        cover_from_first_portrait: bool = False,
        rotate_landscape_90: bool = False,
        erase_colorsoft_rainbow: bool = False,
        page_progression: Literal["ltr", "rtl"] = "ltr",
        layout_view: Literal["fixed", "virtual"] = "fixed",
        virtual_panel_axis: Literal["vertical", "horizontal"] = "vertical",
    ) -> None:
        self.res_dir = db_path.parent / "res"
        cover_idx = (
            _first_portrait_cover_index(
                image_paths, rotate_landscape_90=rotate_landscape_90
            )
            if cover_from_first_portrait
            else 0
        )
        logger.debug(
            "[kdf] 初始化 KDF: db=%s res_dir=%s 图片数=%d cover_idx=%d portrait=%s rotate_landscape_90=%s erase_colorsoft_rainbow=%s page_progression=%s layout_view=%s virtual_panel_axis=%s",
            db_path,
            self.res_dir,
            len(image_paths),
            cover_idx,
            cover_from_first_portrait,
            rotate_landscape_90,
            erase_colorsoft_rainbow,
            page_progression,
            layout_view,
            virtual_panel_axis,
        )
        self.res_dir.mkdir(exist_ok=True)
        db_path = db_path.resolve()
        parent = db_path.parent
        tmp_fd, tmp_name = tempfile.mkstemp(
            suffix=".kdf",
            prefix=".book_kdf_wip_",
            dir=parent,
        )
        os.close(tmp_fd)
        tmp_db = Path(tmp_name)
        logger.debug("[kdf] 临时 SQLite 路径: %s（完成后替换为 %s）", tmp_db, db_path)
        try:
            logger.debug("[kdf] 创建 SQLite 表 (capabilities/fragments/...)")
            self._create_kdf_tables(tmp_db)
            assert self.conn is not None
            # 单次事务批量写入，减少磁盘 fsync 与日志开销
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                logger.debug("[kdf] 写入 Ion 共享符号表 ($ion_symbol_table / max_id)")
                self._insert_ion_symbol_table()

                all_structure_ids: dict[str, list[tuple[str, int]]] = {}
                section_ids: list[str] = []
                first_cover_res = ""
                authoring_aux_ids: list[str] = []

                for idx, img_path in enumerate(image_paths):
                    logger.debug(
                        "[kdf] 写入固定版式页 [%d/%d]: section+resource <- %s",
                        idx + 1,
                        len(image_paths),
                        img_path,
                    )
                    if layout_view == "virtual":
                        section_id, spm_list, res_id, aux_id = (
                            self._add_virtual_panel_image_section(
                                img_path,
                                rotate_landscape_90=rotate_landscape_90,
                                erase_colorsoft_rainbow=erase_colorsoft_rainbow,
                                virtual_panel_axis=virtual_panel_axis,
                            )
                        )
                        authoring_aux_ids.append(aux_id)
                    else:
                        section_id, spm_list, res_id = self._add_fixed_image_section(
                            img_path,
                            rotate_landscape_90=rotate_landscape_90,
                            erase_colorsoft_rainbow=erase_colorsoft_rainbow,
                        )
                    section_ids.append(section_id)
                    all_structure_ids[section_id] = spm_list
                    if idx == cover_idx:
                        first_cover_res = res_id

                authoring_root_id: str | None = None
                if layout_view == "virtual":
                    authoring_root_id = self._insert_authoring_root(authoring_aux_ids)

                logger.debug("[kdf] 写入 book_metadata 与 content_features")
                self._insert_book_metadata(
                    first_cover_res, layout_view=layout_view
                )
                logger.debug("[kdf] 写入 document_data / metadata（阅读顺序与 sections）")
                self._create_document_data(
                    section_ids,
                    cover_res_id=first_cover_res,
                    page_progression=page_progression,
                    layout_view=layout_view,
                    authoring_root_id=authoring_root_id,
                )
                logger.debug("[kdf] 写入 yj.section_pid_count_map")
                self._create_section_pid_count_map(all_structure_ids)
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            # 不写入 location_map（$550）：kfxlib 要求 value 为 IonList[IonStruct{$182:...}]；
            # 单 struct + reading_order_name/locations 会在 fix_kpf_prepub_book 里迭代到 IonSymbol 而崩溃。
            # Calibre KFX Output 会在 is_kpf_prepub 时用 generate_approximate_locations 补全 $550。
            logger.debug("[kdf] 提交事务并关闭 SQLite")
            self.conn.close()
            self.conn = None
            _install_db_from_temp(tmp_db, db_path)
        except BaseException:
            if self.conn is not None:
                try:
                    self.conn.close()
                except sqlite3.Error:
                    pass
                self.conn = None
            tmp_db.unlink(missing_ok=True)
            raise
        logger.debug("[kdf] KDF 生成完毕")

    def _create_kdf_tables(self, db_path: Path) -> None:
        # isolation_level=None：显式 BEGIN/COMMIT，避免每条 INSERT 单独提交
        # timeout：缓解 Windows 上短暂 SQLITE_BUSY / 外进程扫盘导致的锁竞争
        self.conn = sqlite3.connect(
            db_path,
            isolation_level=None,
            timeout=60.0,
        )
        self.conn.executescript(_KDF_SCHEMA_SQL)

    def _insert_ion_symbol_table(self) -> None:
        max_id = 9 + len(YJ_SYMBOLS) + len(YJ_CONVERSION_SYMBOLS)
        conversion_symbols = ", ".join(f'"{x}"' for x in YJ_CONVERSION_SYMBOLS)
        ion_str = f"""{{
 max_id: {max_id},
 imports: [{{name: "YJ_symbols", version: 10, max_id: {len(YJ_SYMBOLS)}}}],
 symbols: [{conversion_symbols}],
 }}
 """
        self._insert_blob_fragment("$ion_symbol_table", ion_str, "$ion_symbol_table")
        self._insert_fragment("max_id", "blob", simpleion.dumps(max_id, binary=True))
        self._insert_fragment_properties(
            [
                ("$ion_symbol_table", "element_type", "$ion_symbol_table"),
                ("max_id", "element_type", "max_id"),
            ]
        )

    def _insert_fragment(
        self, fragment_id: str, payload_type: str, payload_value: str | bytes
    ) -> None:
        assert self.conn is not None
        # BLOB 列若写入 str，sqlite 读回仍是 str，KFX/kfxlib 的 prep_payload_blob 需要 bytes
        if isinstance(payload_value, str):
            payload_value = payload_value.encode("utf-8")
        self.conn.execute(
            "INSERT INTO fragments VALUES(?, ?, ?)",
            (fragment_id, payload_type, payload_value),
        )

    def _insert_fragment_property(self, fragment_id: str, key: str, value: str) -> None:
        assert self.conn is not None
        self.conn.execute(
            "INSERT INTO fragment_properties VALUES(?, ?, ?)",
            (fragment_id, key, value),
        )

    def _insert_fragment_properties(self, data: list[tuple[str, str, str]]) -> None:
        assert self.conn is not None
        self.conn.executemany(
            "INSERT INTO fragment_properties VALUES(?, ?, ?)", data
        )

    def _insert_blob_fragment(
        self, fragment_id: str, ion: str | IonPyDict, annotation: str = ""
    ) -> None:
        if isinstance(ion, str):
            value = simpleion.loads(ion, catalog=self.catalog)
            value = IonPyDict.from_value(IonType.STRUCT, value, (annotation,))
        else:
            value = ion
        self._insert_fragment(
            fragment_id,
            "blob",
            remove_ion_table(
                simpleion.dumps(value, binary=True, imports=(self.symbol_table,))
            ),
        )

    def _insert_book_metadata(
        self,
        cover_res_id: str,
        *,
        layout_view: Literal["fixed", "virtual"] = "fixed",
    ) -> None:
        # 勿在 kindle_title_metadata 中写入 cde_content_type / ASIN 等：kpf_container 据此会把
        # is_kpf_prepub=False，从而跳过 fix_kpf_prepub_book（不生成 $389/$264/$265/$419/$550），
        # 且 $609 的 fragment.fid 与 section_name 的 -spm 规则不一致。PDOC 由 kpf_to_kfx 里 YJ_Metadata 写入。
        metadata: list[dict[str, object]] = [
            {
                "key": "book_id",
                "value": "".join(
                    random.choices(string.digits + string.ascii_letters, k=23)
                ),
            },
        ]
        if cover_res_id:
            # kfx_id 与页资源 fid 一致；单文件 KFX 导出时 kpf_to_kfx 会把该值规范为 str。
            metadata.append(
                {
                    "key": "cover_image",
                    "value": IonPyText.from_value(
                        IonType.STRING, cover_res_id, ("kfx_id",)
                    ),
                }
            )
        for metadata_key in ("language", "title", "description", "author", "publisher"):
            value = getattr(self.epub_metadata, metadata_key, "") or ""
            if isinstance(value, Path):
                continue
            if len(str(value)) > 0:
                metadata.append({"key": metadata_key, "value": str(value)})

        ion = IonPyDict.from_value(
            IonType.STRUCT,
            {
                "categorised_metadata": [
                    {
                        "category": "kindle_ebook_metadata",
                        "metadata": [
                            {"key": "selection", "value": "enabled"},
                            {"key": "nested_span", "value": "enabled"},
                        ],
                    },
                    {
                        "category": "kindle_capability_metadata",
                        "metadata": [
                            {"key": "yj_fixed_layout", "value": 1},
                            {
                                "key": "yj_publisher_panels",
                                "value": 0 if layout_view == "virtual" else 1,
                            },
                        ],
                    },
                    {
                        "category": "kindle_audit_metadata",
                        "metadata": [
                            {"key": "file_creator", "value": TOOL_NAME},
                            {"key": "creator_version", "value": TOOL_VERSION},
                        ],
                    },
                    {"category": "kindle_title_metadata", "metadata": metadata},
                ]
            },
            ("book_metadata",),
        )
        self._insert_blob_fragment("book_metadata", ion)
        self._insert_fragment_property(
            "book_metadata", "element_type", "book_metadata"
        )
        self._insert_content_features(layout_view=layout_view)

    def _insert_content_features(
        self, *, layout_view: Literal["fixed", "virtual"] = "fixed"
    ) -> None:
        if layout_view == "virtual":
            ion_text = """{
 kfx_id: content_features,
 features: [
 {
 namespace: "com.amazon.yjconversion",
 key: "yj_non_pdf_fixed_layout",
 version_info: {version: {major_version: 2, minor_version: 0}}
 }
 ]
}"""
        else:
            ion_text = """{
 kfx_id: content_features,
 features: [
 {
 namespace: "com.amazon.yjconversion",
 key: "reflow-style",
 version_info: {version: {major_version: 1, minor_version: 0}}
 }
 ]
}"""
        self._insert_blob_fragment("content_features", ion_text, "content_features")
        self._insert_fragment_property(
            "content_features", "element_type", "content_features"
        )

    def _insert_authoring_root(self, resource_aux_ids: list[str]) -> str:
        """Kindle Create 虚拟视图：document_data.auxiliary_data.yj.authoring 指向根 auxiliary。"""
        if not resource_aux_ids:
            raise ValueError("virtual layout 需要至少一页图源 auxiliary。")
        root_id = self.create_fragment_id("d")
        parts = ",\n ".join(f'kfx_id::"{x}"' for x in resource_aux_ids)
        ion_text = f"""{{
 kfx_id: kfx_id::"{root_id}",
 metadata: [
 {{
 key: "auxData_resource_list",
 value: [
 {parts}
 ]
 }}
 ]
}}"""
        self._insert_blob_fragment(root_id, ion_text, "auxiliary_data")
        self._insert_fragment_property(root_id, "element_type", "auxiliary_data")
        return root_id

    def _add_virtual_panel_image_section(
        self,
        image_path: Path,
        *,
        rotate_landscape_90: bool = False,
        erase_colorsoft_rainbow: bool = False,
        virtual_panel_axis: Literal["vertical", "horizontal"] = "vertical",
    ) -> tuple[str, list[tuple[str, int]], str, str]:
        """虚拟视图（对齐 Kindle Create）：页模板 ``virtual_panel: enabled``、SPM 三槽、中层 ``layout: vertical|horizontal``。"""
        assert self.res_dir is not None
        image_bytes, ion_fmt, im_width, im_height = _load_page_raster_for_kdf(
            image_path,
            rotate_landscape_90=rotate_landscape_90,
            erase_colorsoft_rainbow=erase_colorsoft_rainbow,
        )

        axis_ion = (
            "vertical" if virtual_panel_axis == "vertical" else "horizontal"
        )
        section_id = self.create_fragment_id("c")
        section_struct_id = self.create_fragment_id("i")
        middle_id = self.create_fragment_id("i")
        image_struct_id = self.create_fragment_id("i")
        story_id = self.create_fragment_id("l")
        aux_id = self.create_fragment_id("d")

        section_text = f"""{{
 section_name: kfx_id::"{section_id}",
 page_templates: [
 structure::{{
 kfx_id: kfx_id::"{section_struct_id}",
 story_name: kfx_id::"{story_id}",
 fixed_width: {im_width},
 virtual_panel: enabled,
 fixed_height: {im_height},
 layout: scale_fit,
 float: center,
 type: container
 }}
 ]
}}"""
        self._insert_blob_fragment(section_id, section_text, "section")
        self._insert_fragment_property(section_id, "element_type", "section")

        storyline_text = f"""{{
 story_name: kfx_id::"{story_id}",
 content_list: [
 kfx_id::"{middle_id}"
 ]
}}"""
        self._insert_blob_fragment(story_id, storyline_text, "storyline")
        self._insert_fragment_properties(
            [
                (story_id, "child", middle_id),
                (story_id, "child", story_id),
                (story_id, "element_type", "storyline"),
            ]
        )

        middle_text = f"""{{
 kfx_id: kfx_id::"{middle_id}",
 width: {im_width},
 height: {im_height},
 sizing_bounds: content_bounds,
 layout: {axis_ion},
 type: container,
 content_list: [
 kfx_id::"{image_struct_id}"
 ]
}}"""
        self._insert_blob_fragment(middle_id, middle_text, "structure")
        self._insert_fragment_properties(
            [
                (middle_id, "element_type", "structure"),
                (middle_id, "child", image_struct_id),
            ]
        )

        res_id = self.create_fragment_id("e")
        res_loc_id = self.create_fragment_id("rsrc")
        dest = self.res_dir / res_loc_id
        dest.write_bytes(image_bytes)

        loc_json = json.dumps(str(image_path.resolve()), ensure_ascii=False)
        mtime_s = str(int(image_path.stat().st_mtime))
        size_s = str(len(image_bytes))

        aux_text = f"""{{
 kfx_id: kfx_id::"{aux_id}",
 metadata: [
 {{key: "location", value: {loc_json}}},
 {{key: "modified_time", value: "{mtime_s}"}},
 {{key: "size", value: "{size_s}"}},
 {{key: "type", value: "resource"}},
 {{key: "resource_stream", value: "{res_loc_id}"}}
 ]
}}"""
        self._insert_blob_fragment(aux_id, aux_text, "auxiliary_data")
        self._insert_fragment_property(aux_id, "element_type", "auxiliary_data")

        # 勿写入带点的 Ion 字段名（如 yj.authoring.source_file_name）：kfxlib IonBinary 会解析为多值导致 decode 失败。
        res_text = f"""{{
 format: {ion_fmt},
 location: "{res_loc_id}",
 auxiliary_data: kfx_id::"{aux_id}",
 resource_width: {im_width}.0e0,
 resource_name: kfx_id::"{res_id}",
 resource_height: {im_height}.0e0
}}"""
        self._insert_blob_fragment(res_id, res_text, "external_resource")
        self._insert_fragment_properties(
            [
                (res_id, "child", res_loc_id),
                (res_id, "element_type", "external_resource"),
                (res_loc_id, "element_type", "bcRawMedia"),
            ]
        )
        self._insert_fragment(res_loc_id, "blob", image_bytes)

        struct_text = f"""{{
 kfx_id: kfx_id::"{image_struct_id}",
 width: {im_width},
 resource_name: kfx_id::"{res_id}",
 height: {im_height},
 sizing_bounds: content_bounds,
 type: image,
 position: fixed
}}"""
        self._insert_blob_fragment(image_struct_id, struct_text, "structure")
        self._insert_fragment_properties(
            [
                (image_struct_id, "element_type", "structure"),
                (image_struct_id, "child", res_id),
            ]
        )

        spm_text = f"""{{
 section_name: kfx_id::"{section_id}",
 contains: [[1, kfx_id::"{section_struct_id}"], [2, kfx_id::"{middle_id}"], [3, kfx_id::"{image_struct_id}"]]
}}"""
        spm_id = f"{section_id}-spm"
        self._insert_blob_fragment(spm_id, spm_text, "section_position_id_map")
        self._insert_fragment_property(
            spm_id, "element_type", "section_position_id_map"
        )
        self._insert_section_auxiliary_data(section_id)

        spm_list: list[tuple[str, int]] = [
            (section_struct_id, 0),
            (middle_id, 1),
            (image_struct_id, 2),
        ]
        return section_id, spm_list, res_id, aux_id

    def _insert_section_auxiliary_data(self, section_id: str) -> None:
        ad_id = section_id + "-ad"
        ion_text = f"""{{
 kfx_id: kfx_id::"{ad_id}",
 metadata: [{{key: "IS_TARGET_SECTION", value: true}}]
}}"""
        self._insert_blob_fragment(ad_id, ion_text, "auxiliary_data")
        self._insert_fragment_properties(
            [
                (section_id, "child", ad_id),
                (ad_id, "element_type", "auxiliary_data"),
            ]
        )

    def _add_fixed_image_section(
        self,
        image_path: Path,
        *,
        rotate_landscape_90: bool = False,
        erase_colorsoft_rainbow: bool = False,
    ) -> tuple[str, list[tuple[str, int]], str]:
        assert self.res_dir is not None
        image_bytes, ion_fmt, im_width, im_height = _load_page_raster_for_kdf(
            image_path,
            rotate_landscape_90=rotate_landscape_90,
            erase_colorsoft_rainbow=erase_colorsoft_rainbow,
        )

        section_id = self.create_fragment_id("c")
        section_struct_id = self.create_fragment_id("i")
        story_id = self.create_fragment_id("l")
        section_text = f"""{{
 section_name: kfx_id::"{section_id}",
 page_templates: [
 structure::{{
 kfx_id: kfx_id::"{section_struct_id}",
 story_name: kfx_id::"{story_id}",
 fixed_width: {im_width},
 fixed_height: {im_height},
 layout: scale_fit,
 float: center,
 type: container
 }}
 ]
}}"""
        self._insert_blob_fragment(section_id, section_text, "section")
        self._insert_fragment_property(section_id, "element_type", "section")

        struct_id = self.create_fragment_id("i")
        storyline_text = f"""{{
 story_name: kfx_id::"{story_id}",
 content_list: [
 kfx_id::"{struct_id}"
 ]
}}"""
        self._insert_blob_fragment(story_id, storyline_text, "storyline")
        self._insert_fragment_properties(
            [
                (story_id, "child", struct_id),
                (story_id, "child", story_id),
                (story_id, "element_type", "storyline"),
            ]
        )

        res_id = self.create_fragment_id("e")
        res_loc_id = self.create_fragment_id("rsrc")
        dest = self.res_dir / res_loc_id
        dest.write_bytes(image_bytes)
        # location 须为普通路径名（非 kfx_id）：kpf_fix_fragment 要求 IonString，并 fix 为 resource/<id> 与 $417 片段 fid 对齐
        res_text = f"""{{
 format: {ion_fmt},
 location: "{res_loc_id}",
 resource_width: {im_width},
 resource_name: kfx_id::"{res_id}",
 resource_height: {im_height}
}}"""
        self._insert_blob_fragment(res_id, res_text, "external_resource")
        self._insert_fragment_properties(
            [
                (res_id, "child", res_loc_id),
                (res_id, "element_type", "external_resource"),
                (res_loc_id, "element_type", "bcRawMedia"),
            ]
        )
        # 须用 blob 嵌入媒体：KPF 常仅含 book.kdf，无 zip 内 res/* 文件时 path 行无法加载，不会生成 $417，封面/内页图均丢失
        self._insert_fragment(res_loc_id, "blob", image_bytes)

        style_id = self.create_fragment_id("s")
        style_text = f"""{{
 font_size: {{value: 1.0e0, unit: rem}},
 line_height: {{value: 1.0e0, unit: lh}},
 style_name: kfx_id::"{style_id}"
}}"""
        self._insert_blob_fragment(style_id, style_text, "style")

        struct_text = f"""{{
 kfx_id: kfx_id::"{struct_id}",
 style: kfx_id::"{style_id}",
 type: image,
 resource_name: kfx_id::"{res_id}"
}}"""
        self._insert_blob_fragment(struct_id, struct_text, "structure")
        self._insert_fragment_property(struct_id, "element_type", "structure")
        self._insert_fragment_property(struct_id, "child", res_id)

        # kfxlib 读入后键为 $174/$181；Ion 文本须用符号表字段名 section_name/contains（勿写 $174，会被当成非法本地 SID）。
        spm_text = f"""{{
 section_name: kfx_id::"{section_id}",
 contains: [[1, kfx_id::"{section_struct_id}"], [2, kfx_id::"{struct_id}"]]
}}"""
        spm_id = f"{section_id}-spm"
        self._insert_blob_fragment(spm_id, spm_text, "section_position_id_map")
        self._insert_fragment_property(
            spm_id, "element_type", "section_position_id_map"
        )
        self._insert_section_auxiliary_data(section_id)

        # SPM 每项对应一个 pid 槽位；yj.section_pid_count_map 的 length 须为槽位数（len），
        # 勿用“内容长度”之和，否则终止行 [length+1,0] 错位，map 会出现 len=0 块并与正文采集不一致。
        spm_list: list[tuple[str, int]] = [
            (section_struct_id, 0),
            (struct_id, 1),
        ]
        return section_id, spm_list, res_id

    def _create_document_data(
        self,
        section_ids: list[str],
        *,
        cover_res_id: str,
        page_progression: Literal["ltr", "rtl"],
        layout_view: Literal["fixed", "virtual"] = "fixed",
        authoring_root_id: str | None = None,
    ) -> None:
        section_ion_str = ",".join(f'kfx_id::"{s}"' for s in section_ids)
        dir_ion = "rtl" if page_progression == "rtl" else "ltr"
        if layout_view == "virtual":
            if not authoring_root_id:
                raise ValueError("virtual layout 需要 authoring_root_id")
            max_id = self.fragment_id
            document_data_ion = f"""{{
 direction: {dir_ion},
 writing_mode: horizontal_tb,
 column_count: auto,
 selection: enabled,
 spacing_percent_base: width,
 font_size: 16.0e0,
 max_id: {max_id},
 pan_zoom: enabled,
 auxiliary_data: {{
  'yj.authoring': kfx_id::"{authoring_root_id}"
 }},
 reading_orders: [
 {{
 reading_order_name: default,
 sections: [{section_ion_str}]
 }}
 ]
}}"""
        else:
            document_data_ion = f"""{{
 direction: {dir_ion},
 writing_mode: horizontal_tb,
 column_count: auto,
 selection: enabled,
 spacing_percent_base: width,
 reading_orders: [
 {{
 reading_order_name: default,
 sections: [{section_ion_str}]
 }}
 ]
}}"""
        self._insert_blob_fragment("document_data", document_data_ion, "document_data")
        self._insert_fragment_property(
            "document_data", "element_type", "document_data"
        )
        # 顶层 $258：与 document_data 同阅读顺序，并供 get_metadata_value 回退。
        reading_orders_block = f"""reading_orders: [
 {{
 reading_order_name: default,
 sections: [{section_ion_str}]
 }}
]"""
        meta_parts: list[str] = [reading_orders_block]
        m = self.epub_metadata
        if (m.title or "").strip():
            meta_parts.append(f"title: {json.dumps(m.title.strip(), ensure_ascii=False)}")
        if (m.author or "").strip():
            meta_parts.append(f"author: {json.dumps(m.author.strip(), ensure_ascii=False)}")
        if (m.publisher or "").strip():
            meta_parts.append(
                f"publisher: {json.dumps(m.publisher.strip(), ensure_ascii=False)}"
            )
        if (m.language or "").strip():
            meta_parts.append(
                f"language: {json.dumps(m.language.strip(), ensure_ascii=False)}"
            )
        if (m.description or "").strip():
            meta_parts.append(
                f"description: {json.dumps(m.description.strip(), ensure_ascii=False)}"
            )
        if cover_res_id:
            meta_parts.append(f'cover_image: kfx_id::"{cover_res_id}"')
        metadata_ion = "{\n " + ",\n ".join(meta_parts) + "\n}"
        self._insert_blob_fragment("metadata", metadata_ion, "metadata")
        self._insert_fragment_property("metadata", "element_type", "metadata")

    def _create_section_pid_count_map(
        self, structure_ids: dict[str, list[tuple[str, int]]]
    ) -> None:
        section_lens = {sid: len(spml) for sid, spml in structure_ids.items()}
        # 与 SPM 相同：用 section_name / length / contains，序列化后与 kfxlib 期望的 $174/$144/$181 一致。
        parts: list[str] = []
        for section_id, section_len in section_lens.items():
            parts.append(
                f'{{section_name: kfx_id::"{section_id}", length: {int(section_len)}}}'
            )
        map_ion_text = "{\n contains: [\n  " + ",\n  ".join(parts) + "\n ]\n}"
        self._insert_blob_fragment(
            "yj.section_pid_count_map",
            map_ion_text,
            "yj.section_pid_count_map",
        )
        self._insert_fragment_property(
            "yj.section_pid_count_map",
            "element_type",
            "yj.section_pid_count_map",
        )
