#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""打包 GUI：使用 PyInstaller 生成分发包。

- **Windows**：在当前机器上生成单文件 ``dist/kckfxgen-gui.exe``（无控制台窗口）。
- **macOS**：在当前机器上生成 ``dist/kckfxgen-gui.app``（目录型 bundle，便于双击运行）。

PyInstaller **不能**在单一系统上交叉编译出另一系统的二进制；要同时具备 Win / Mac 安装包请：

1. 在 Windows 上运行本脚本得到 ``.exe``；
2. 在 macOS 上运行本脚本得到 ``.app``；或
3. 使用仓库内 ``.github/workflows/build-gui.yml``（push tag ``v*`` 或手动触发）由 GitHub Actions 在两种 runner 上分别构建并上传 artifact。

用法::

    pip install -r requirements.txt pyinstaller
    python scripts/build_gui.py

    python scripts/build_gui.py --skip-install   # 已安装依赖时
    python scripts/build_gui.py --zip             # 构建后在 dist/ 下再打 zip 便于分发
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _require_tkinter_or_exit() -> None:
    """GUI 依赖 tkinter / ``_tkinter``。构建所用 Python 若未编入 Tcl/Tk，PyInstaller 会排除 Tk，

    产物启动时在 ``import tkinter`` 即失败（--windowed 下无终端 → 表现为闪退）。
    在调用 PyInstaller 前失败并给出说明，避免打出坏包。
    """
    try:
        import _tkinter  # noqa: F401
    except ImportError:
        print(
            "错误: 当前 Python 无法加载 _tkinter（未包含 Tcl/Tk），无法打包 tkinter GUI。\n"
            "\n"
            "常见情况:\n"
            "  • macOS 上 pyenv/Homebrew Python 若编译时未链到 tcl-tk，会出现此问题。\n"
            "  • 处理: 使用带 Tcl/Tk 的解释器再执行本脚本，例如:\n"
            "      - 自 https://www.python.org/downloads/ 安装的官方 macOS 包；或\n"
            "      - brew install python-tk@3.12（或当前主版本），并用该 python 运行打包；或\n"
            "      - pyenv 前先 brew install tcl-tk，并令 Python 配置能找到 Tk 头文件与库。\n"
            "\n"
            "验证: python3 -c \"import tkinter; tkinter.Tk().destroy()\"",
            file=sys.stderr,
        )
        sys.exit(2)


def _pip_install() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
        ],
        cwd=_ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(_ROOT / "requirements.txt"),
            "pyinstaller>=6.0",
        ],
        cwd=_ROOT,
        check=True,
    )


def _pyinstaller_cmd() -> list[str]:
    sysname = platform.system()
    cmd: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--windowed",
        "--name",
        "kckfxgen-gui",
        "--paths",
        str(_ROOT),
        str(_ROOT / "gui.py"),
        "--hidden-import",
        "main",
        "--collect-submodules",
        "kckfxgen",
        "--hidden-import",
        "PIL._tkinter_finder",
        "--hidden-import",
        "amazon.ion",
        "--hidden-import",
        "amazon.ion.simpleion",
        "--hidden-import",
        "lxml.etree",
        # macOS / 部分环境：未打入 Tcl/Tk 时 tkinter 初始化即崩溃（窗口应用无控制台）
        "--collect-all",
        "tkinter",
    ]

    if sysname == "Windows":
        cmd.append("--onefile")
    elif sysname == "Darwin":
        cmd += [
            "--onedir",
            "--osx-bundle-identifier",
            "com.kckfxgen.gui",
        ]
        # 双击从 Finder 启动时更接近 CLI 的 argv 行为
        cmd += ["--argv-emulation"]
    else:
        print(
            "当前系统为 Linux 等：将尝试生成 onedir 目录包（无 .app）。"
            "官方发布请在 Windows / macOS 上构建。",
            file=sys.stderr,
        )
        cmd.append("--onedir")

    return cmd


def _make_zip() -> Path | None:
    dist = _ROOT / "dist"
    if not dist.is_dir():
        return None
    sysname = platform.system()
    if sysname == "Windows":
        exe = dist / "kckfxgen-gui.exe"
        if not exe.is_file():
            return None
        out = dist / "kckfxgen-gui-windows.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(exe, arcname=exe.name)
        return out
    if sysname == "Darwin":
        app = dist / "kckfxgen-gui.app"
        if not app.is_dir():
            return None
        out = dist / "kckfxgen-gui-macos.zip"
        if out.exists():
            out.unlink()
        base = app.parent

        def _arcname(p: Path) -> str:
            return str(p.relative_to(base)).replace("\\", "/")

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(app.rglob("*")):
                z.write(f, arcname=_arcname(f))
        return out
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 PyInstaller 打包 kckfxgen GUI")
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="跳过 pip install（假设已安装 requirements.txt 与 pyinstaller）",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="构建完成后将 dist 内主产物打成 zip（Windows: exe；macOS: .app）",
    )
    args = parser.parse_args()

    if not (_ROOT / "gui.py").is_file():
        print(f"找不到 gui.py：{_ROOT}", file=sys.stderr)
        sys.exit(1)

    _require_tkinter_or_exit()

    if not args.skip_install:
        _pip_install()

    cmd = _pyinstaller_cmd()
    print("运行:", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=_ROOT, check=True)

    dist = _ROOT / "dist"
    print("\n构建完成。输出目录:", dist, file=sys.stderr)
    if platform.system() == "Windows":
        print("  可执行文件:", dist / "kckfxgen-gui.exe", file=sys.stderr)
    elif platform.system() == "Darwin":
        print("  应用程序包:", dist / "kckfxgen-gui.app", file=sys.stderr)
    else:
        d = dist / "kckfxgen-gui"
        if d.is_dir():
            print("  目录:", d, file=sys.stderr)

    if args.zip:
        z = _make_zip()
        if z:
            print("已生成压缩包:", z, file=sys.stderr)
        else:
            print("警告: 未找到预期产物，跳过 zip", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
