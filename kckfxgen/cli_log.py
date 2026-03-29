# SPDX-License-Identifier: GPL-3.0-or-later
"""非 debug 模式下的命令行日志格式（简洁、可选 ANSI 颜色）。"""

from __future__ import annotations

import logging
import sys


def _stderr_is_tty() -> bool:
    err = sys.stderr
    if err is None:
        return False
    isatty = getattr(err, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


class PrettyFormatter(logging.Formatter):
    """INFO 单行前缀；ERROR/WARNING 高亮。"""

    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _BOLD = "\033[1m"
    _GREEN = "\033[32m"
    _RED = "\033[91m"
    _YELLOW = "\033[33m"
    _CYAN = "\033[36m"

    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self._color = color

    def _paint(self, code: str, text: str) -> str:
        if not self._color:
            return text
        return f"{code}{text}{self._RESET}"

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.ERROR:
            return self._paint(self._BOLD + self._RED, "✗ ") + msg
        if record.levelno == logging.WARNING:
            return self._paint(self._YELLOW, "⚠ ") + msg
        if record.levelno == logging.INFO:
            style = getattr(record, "cli_style", None)
            if style == "success":
                return self._paint(self._GREEN, "✓ ") + msg
            if style == "dim":
                return self._paint(self._DIM, "  ") + msg
            return self._paint(self._CYAN, "› ") + msg
        return msg


def configure_logging(*, debug: bool) -> None:
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    err = sys.stderr
    if err is not None:
        handler: logging.Handler = logging.StreamHandler(err)
        if debug:
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(levelname)s %(name)s: %(message)s")
            )
            root.setLevel(logging.DEBUG)
        else:
            use_color = _stderr_is_tty()
            handler.setLevel(logging.INFO)
            handler.setFormatter(PrettyFormatter(color=use_color))
            root.setLevel(logging.INFO)
        root.addHandler(handler)
    else:
        # 无控制台（如 PyInstaller --windowed）：仅配置级别，由调用方挂 QueueHandler 等
        root.setLevel(logging.DEBUG if debug else logging.INFO)
        root.addHandler(logging.NullHandler())

    # Pillow 在根 logger 为 DEBUG 时会对每个 PNG chunk 打 DEBUG，淹没业务日志
    logging.getLogger("PIL").setLevel(logging.WARNING)


def print_run_header(*, input_count: int, jobs: int | None, debug: bool) -> None:
    """非 debug 时在 stderr 打印简短横幅。"""
    if debug:
        return
    out = sys.stderr
    if out is None:
        return
    tty = _stderr_is_tty()
    dim = "\033[2m" if tty else ""
    bold = "\033[1m" if tty else ""
    cyan = "\033[36m" if tty else ""
    reset = "\033[0m" if tty else ""
    line = "─" * 48
    out.write(f"{dim}{line}{reset}\n")
    out.write(f"{bold}{cyan}kckfxgen{reset}{dim} · ")
    if input_count == 1:
        out.write("1 个文件 → KFX\n")
    else:
        out.write(f"{input_count} 个文件 → KFX")
        if jobs is not None:
            out.write(f" · {jobs} 线程")
        out.write("\n")
    out.write(f"{dim}{line}{reset}\n\n")
