#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""kckfxgen 入口：将 EPUB / 漫画压缩包（ZIP·CBZ·RAR·CBR）或目录内全部转为 KFX。"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

from kckfxgen.cli_log import configure_logging, print_run_header
from kckfxgen.archive_comic import COMIC_ARCHIVE_SUFFIXES
from kckfxgen.pipeline import convert_to_kfx

logger = logging.getLogger(__name__)


_SUPPORTED_SUFFIXES = frozenset({".epub", *COMIC_ARCHIVE_SUFFIXES})


def discover_comic_inputs(root: Path) -> list[Path]:
    """递归列出目录下所有支持的输入，路径去重。"""
    root = root.resolve()
    seen: set[Path] = set()
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        r = p.resolve()
        if r in seen:
            continue
        seen.add(r)
        out.append(p)
    return sorted(out, key=lambda x: str(x).lower())


def resolve_input_list(path: Path) -> list[Path]:
    path = path.expanduser().resolve()
    if path.is_file():
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise ValueError(
                f"不支持的文件类型: {path.suffix}（支持 "
                f".epub、{', '.join(sorted(COMIC_ARCHIVE_SUFFIXES))}）"
            )
        return [path]
    if path.is_dir():
        found = discover_comic_inputs(path)
        if not found:
            raise ValueError(
                f"目录下未找到支持的漫画文件（.epub / .zip / .cbz / .rar / .cbr）: {path}"
            )
        return found
    raise FileNotFoundError(path)


def _validate_kfx_dir(p: Path, label: str) -> Path:
    p = p.expanduser().resolve()
    if p.exists() and not p.is_dir():
        raise ValueError(f"{label} 须为目录（用于存放 KFX 输出）: {p}")
    return p


def planned_kfx_output_dir(
    input_file: Path, *, output_o: Path | None, output_dir: Path | None, n_total: int
) -> Path:
    """每个输入文件对应的 KFX 输出目录。"""
    if output_o is not None and output_dir is not None:
        raise ValueError("不能同时使用 -o 与 --output-dir")
    if output_dir is not None:
        return _validate_kfx_dir(output_dir, "--output-dir")
    if output_o is not None:
        if n_total != 1:
            raise ValueError("-o/--output 仅适用于单个输入文件")
        return _validate_kfx_dir(output_o, "-o/--output")
    return input_file.parent.resolve()


def _convert_job(
    src: Path,
    kfx_dir: Path,
    *,
    split_spreads: bool,
    split_page_order: Literal["right-left", "left-right"],
    rotate_landscape_90: bool,
    book_title: str | None,
    book_author: str | None,
    book_publisher: str | None,
) -> tuple[Path, BaseException | None]:
    try:
        convert_to_kfx(
            src,
            kfx_dir,
            split_spreads=split_spreads,
            split_page_order=split_page_order,
            rotate_landscape_90=rotate_landscape_90,
            book_title=book_title,
            book_author=book_author,
            book_publisher=book_publisher,
        )
        return (src, None)
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        return (src, e)


def _install_terminate_signals() -> None:
    """将 SIGTERM（如 kill、容器停止）转为 KeyboardInterrupt，与 Ctrl+C 一并走相同清理逻辑。

    不替换 SIGINT：保留解释器默认的 Ctrl+C → KeyboardInterrupt。
    """
    def _handler(_signum: int, _frame: object | None) -> None:
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, OSError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "输入单个 EPUB / 漫画压缩包（.zip .cbz .rar .cbr），或包含上述文件的目录（递归），"
            "生成 KFX（内置 kfxlib：KPF → KfxContainer，无 Kindle Previewer）。"
            "多文件时使用线程池并发。"
        )
    )
    parser.add_argument(
        "path",
        type=Path,
        metavar="PATH",
        help="文件路径（.epub / .zip / .cbz / .rar / .cbr），或包含这些文件的目录",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="DIR",
        help="仅单文件时：指定 KFX 输出目录（默认：与输入文件同目录）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        metavar="DIR",
        help="批量时：将所有输入转出的 .kfx 写入该目录（文件名为 书名-作者.kfx，重名则加 _2、_3…）",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=max(1, min(8, os.cpu_count() or 4)),
        metavar="N",
        help="并发线程数（默认: min(8, CPU 核心数)）",
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="DEBUG 日志",
    )
    parser.add_argument(
        "--split-spreads",
        action="store_true",
        help="宽幅图（宽≥高×1.25）仅当检出空白中缝时沿正中裁切，否则保留整图；顺序见 --split-page-order；需 numpy、Pillow",
    )
    parser.add_argument(
        "--split-page-order",
        choices=("right-left", "left-right"),
        default="right-left",
        help="裁切后两半页顺序：right-left=先右后左（默认），left-right=先左后右",
    )
    parser.add_argument(
        "--rotate-landscape-90",
        action="store_true",
        help="将宽>高的横幅页在写入 KDF 前逆时针旋转 90°，以竖屏展示（竖图不变）",
    )
    parser.add_argument(
        "--title",
        default=None,
        metavar="STR",
        help="覆盖书名（漫画包默认从文件名解析；EPUB 则覆盖 OPF 中的 dc:title）",
    )
    parser.add_argument(
        "--author",
        default=None,
        metavar="STR",
        help="覆盖作者（漫画包：文件名中「 - 」右侧；EPUB 覆盖 dc:creator）",
    )
    parser.add_argument(
        "--publisher",
        default=None,
        metavar="STR",
        help="覆盖出版社（漫画包：文件名末尾 […] 或 (…) 内；EPUB 覆盖 dc:publisher）",
    )
    args = parser.parse_args()

    configure_logging(debug=args.debug)
    _install_terminate_signals()

    try:
        inputs = resolve_input_list(args.path)
    except (OSError, ValueError) as e:
        logger.error("%s", e)
        sys.exit(1)

    n = len(inputs)
    try:
        planned = [
            planned_kfx_output_dir(
                item,
                output_o=args.output,
                output_dir=args.output_dir,
                n_total=n,
            )
            for item in inputs
        ]
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    for d in {p for p in planned}:
        d.mkdir(parents=True, exist_ok=True)

    meta_override = any(
        x is not None for x in (args.title, args.author, args.publisher)
    )
    if n > 1 and meta_override:
        logger.info(
            "批量处理：--title / --author / --publisher 将应用于本次列出的每个输入文件"
        )

    print_run_header(
        input_count=n,
        jobs=max(1, args.jobs) if n > 1 else None,
        debug=args.debug,
    )

    if n == 1:
        src, kdir = inputs[0], planned[0]
        try:
            _, err = _convert_job(
                src,
                kdir,
                split_spreads=args.split_spreads,
                split_page_order=args.split_page_order,
                rotate_landscape_90=args.rotate_landscape_90,
                book_title=args.title,
                book_author=args.author,
                book_publisher=args.publisher,
            )
        except KeyboardInterrupt:
            logger.warning("已中断（Ctrl+C 或终止信号）")
            sys.exit(130)
        if err is not None:
            logger.error("%s -> %s", src, err)
            sys.exit(1)
        return

    jobs = max(1, args.jobs)
    failed: list[tuple[Path, BaseException]] = []
    interrupted = False
    ex = ThreadPoolExecutor(max_workers=jobs)
    try:
        futs = {
            ex.submit(
                _convert_job,
                src,
                kdir,
                split_spreads=args.split_spreads,
                split_page_order=args.split_page_order,
                rotate_landscape_90=args.rotate_landscape_90,
                book_title=args.title,
                book_author=args.author,
                book_publisher=args.publisher,
            ): src
            for src, kdir in zip(inputs, planned)
        }
        for fut in as_completed(futs):
            try:
                src, err = fut.result()
            except KeyboardInterrupt:
                interrupted = True
                logger.warning("已中断（Ctrl+C 或终止信号），正在取消未完成任务…")
                ex.shutdown(wait=False, cancel_futures=True)
                break
            if err is not None:
                logger.error("失败 %s: %s", src, err)
                failed.append((src, err))
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("已中断（Ctrl+C 或终止信号），正在取消未完成任务…")
        ex.shutdown(wait=False, cancel_futures=True)
    finally:
        if not interrupted:
            ex.shutdown(wait=True)

    if interrupted:
        sys.exit(130)

    if failed:
        logger.error("完成 %d/%d，失败 %d 个", n - len(failed), n, len(failed))
        sys.exit(1)
    logger.info("全部完成 · %d 个文件", n, extra={"cli_style": "success"})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        try:
            logging.getLogger(__name__).warning("已中断")
        except Exception:
            print("已中断", file=sys.stderr)
        sys.exit(130)
