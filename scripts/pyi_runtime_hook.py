"""PyInstaller runtime hook for GUI startup stability.

Runs before the app entrypoint. We only use stdlib here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _set_tcl_tk_from_meipass() -> None:
    if not getattr(sys, "frozen", False):
        return

    meip = getattr(sys, "_MEIPASS", None)
    if not meip:
        return

    root = Path(meip)

    def scan_dir(d: Path) -> None:
        if "TCL_LIBRARY" not in os.environ and (d / "init.tcl").is_file():
            os.environ["TCL_LIBRARY"] = str(d)
        if "TK_LIBRARY" not in os.environ and (d / "tk.tcl").is_file():
            os.environ["TK_LIBRARY"] = str(d)

    try:
        for child in root.iterdir():
            if child.is_dir():
                scan_dir(child)
                if "TCL_LIBRARY" in os.environ and "TK_LIBRARY" in os.environ:
                    break
                try:
                    for sub in child.iterdir():
                        if sub.is_dir():
                            scan_dir(sub)
                except OSError:
                    pass
            if "TCL_LIBRARY" in os.environ and "TK_LIBRARY" in os.environ:
                break
    except OSError:
        pass


_set_tcl_tk_from_meipass()
