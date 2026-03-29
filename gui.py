#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""kckfxgen 图形界面：EPUB / 漫画包 → KFX。

使用 tkinter **ttk + 系统原生主题**（macOS Aqua、Windows vista/xpnative），
控件外观与系统设置一致。仅依赖标准库。
"""

from __future__ import annotations

import os
import sys

if sys.platform == "darwin":
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

from pathlib import Path


def _resolve_bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


def _crash_log_path() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Logs"
        d.mkdir(parents=True, exist_ok=True)
        return d / "kckfxgen-gui-crash.log"
    return Path.home() / "kckfxgen-gui-crash.log"


_ROOT = _resolve_bundle_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Any, Literal

from kckfxgen.cli_log import configure_logging

import main as kck_main


def _use_native_ttk_theme(root: tk.Tk) -> tkfont.Font:
    """选用系统原生 ttk 主题，不覆盖颜色/按钮样式。返回用于日志的系统等宽字体。"""
    style = ttk.Style(root)
    if sys.platform == "darwin":
        order = ("aqua", "clam", "default")
    elif sys.platform == "win32":
        order = ("vista", "xpnative", "windows", "clam", "default")
    else:
        order = ("clam", "default")
    for name in order:
        try:
            style.theme_use(name)
            break
        except tk.TclError:
            continue
    try:
        return tkfont.nametofont("TkFixedFont")
    except Exception:
        return tkfont.Font(root=root, family="Courier", size=10)


def _present_main_window(root: tk.Tk) -> None:
    try:
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)

        def _unset_topmost() -> None:
            try:
                root.attributes("-topmost", False)
            except tk.TclError:
                pass

        root.after_idle(_unset_topmost)
    except tk.TclError:
        pass
    if sys.platform == "darwin":

        def _mac_reopen() -> None:
            try:
                root.deiconify()
                root.lift()
            except tk.TclError:
                pass

        try:
            root.createcommand("tk::mac::ReopenApplication", _mac_reopen)
        except tk.TclError:
            pass


class _GuiLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue[str]) -> None:
        super().__init__()
        self._q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(self.format(record))
        except Exception:
            pass


def _setup_logging_for_gui(*, debug: bool, log_queue: queue.Queue[str]) -> None:
    configure_logging(debug=debug)
    gh = _GuiLogHandler(log_queue)
    gh.setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger().addHandler(gh)


def _run_conversion(
    *,
    input_path: Path,
    output_dir_str: str,
    split_spreads: bool,
    split_page_order: Literal["right-left", "left-right"],
    rotate_landscape_90: bool,
    page_progression: Literal["ltr", "rtl"],
    layout_view: Literal["fixed", "virtual"],
    virtual_panel_axis: Literal["vertical", "horizontal"],
    keep_kpf: bool,
    book_title: str | None,
    book_author: str | None,
    book_publisher: str | None,
    jobs: int,
    debug: bool,
    log_queue: queue.Queue[str],
    on_done: Any,
) -> None:
    ok = False
    err_msg: str | None = None
    try:
        _setup_logging_for_gui(debug=debug, log_queue=log_queue)
        inputs = kck_main.resolve_input_list(input_path)
        n = len(inputs)
        out_s = output_dir_str.strip()
        output_o: Path | None = None
        output_dir: Path | None = None
        if out_s:
            p = Path(out_s).expanduser().resolve()
            if n == 1:
                output_o = p
            else:
                output_dir = p

        planned = [
            kck_main.planned_kfx_output_dir(
                item,
                output_o=output_o,
                output_dir=output_dir,
                n_total=n,
            )
            for item in inputs
        ]
        for d in {p for p in planned}:
            d.mkdir(parents=True, exist_ok=True)

        log = logging.getLogger(__name__)
        log.info("共 %d 个输入", n)

        if n == 1:
            src, kdir = inputs[0], planned[0]
            _, err = kck_main._convert_job(
                src,
                kdir,
                split_spreads=split_spreads,
                split_page_order=split_page_order,
                rotate_landscape_90=rotate_landscape_90,
                page_progression=page_progression,
                layout_view=layout_view,
                virtual_panel_axis=virtual_panel_axis,
                keep_kpf=keep_kpf,
                book_title=book_title,
                book_author=book_author,
                book_publisher=book_publisher,
            )
            if err is not None:
                err_msg = f"{src.name}: {err}"
            else:
                ok = True
        else:
            j = max(1, jobs)
            failed: list[tuple[Path, BaseException]] = []
            with ThreadPoolExecutor(max_workers=j) as ex:
                futs = {
                    ex.submit(
                        kck_main._convert_job,
                        src,
                        kdir,
                        split_spreads=split_spreads,
                        split_page_order=split_page_order,
                        rotate_landscape_90=rotate_landscape_90,
                        page_progression=page_progression,
                        layout_view=layout_view,
                        virtual_panel_axis=virtual_panel_axis,
                        keep_kpf=keep_kpf,
                        book_title=book_title,
                        book_author=book_author,
                        book_publisher=book_publisher,
                    ): src
                    for src, kdir in zip(inputs, planned)
                }
                for fut in as_completed(futs):
                    src, err = fut.result()
                    if err is not None:
                        log.error("失败 %s: %s", src, err)
                        failed.append((src, err))
            if not failed:
                ok = True
                log.info("全部完成 · %d 个文件", n, extra={"cli_style": "success"})
            else:
                err_msg = f"{len(failed)}/{n} 个失败，见日志"
    except Exception as e:
        err_msg = str(e)
        try:
            logging.getLogger(__name__).exception("转换异常")
        except Exception:
            pass
    finally:
        on_done(ok, err_msg)


class KckfxgenGui:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("kckfxgen — Kindle 漫画 / EPUB → KFX")
        self.root.minsize(740, 640)
        self.root.geometry("980x740")

        self._log_font = _use_native_ttk_theme(self.root)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._busy = False
        self._poll_scheduled = False

        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_title = tk.StringVar()
        self.var_author = tk.StringVar()
        self.var_publisher = tk.StringVar()
        self.var_split = tk.BooleanVar(value=False)
        self.var_rotate = tk.BooleanVar(value=False)
        self.var_keep_kpf = tk.BooleanVar(value=False)
        self.var_debug = tk.BooleanVar(value=False)
        self.var_page_prog = tk.StringVar(value="ltr")
        self.var_split_order = tk.StringVar(value="right-left")
        self.var_layout = tk.StringVar(value="fixed")
        self.var_vaxis = tk.StringVar(value="vertical")
        self.var_jobs = tk.StringVar(value=str(max(1, min(8, os.cpu_count() or 4))))

        self._build()
        _present_main_window(self.root)

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        gy = 5
        px_lab = (0, 12)
        main = ttk.Frame(root, padding=(16, 14))
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)

        r = 0

        # --- 区块 1：路径（标签列右对齐 + 输入区拉伸 + 按钮列固定）---
        lf_io = ttk.LabelFrame(main, text="输入与输出", padding=(12, 10))
        lf_io.grid(row=r, column=0, sticky="nsew", pady=(0, 10))
        r += 1
        lf_io.columnconfigure(0, minsize=88, weight=0)
        lf_io.columnconfigure(1, weight=1)
        lf_io.columnconfigure(2, weight=0)

        ttk.Label(
            lf_io,
            text="选择 EPUB / ZIP / CBZ / RAR / CBR 文件，或包含这些文件的文件夹（递归扫描）。",
            wraplength=920,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, gy + 2))

        ttk.Label(lf_io, text="输入路径").grid(row=1, column=0, sticky="e", padx=px_lab, pady=gy)
        ttk.Entry(lf_io, textvariable=self.var_input).grid(row=1, column=1, sticky="ew", pady=gy)
        bf = ttk.Frame(lf_io)
        bf.grid(row=1, column=2, sticky="ew", padx=(10, 0), pady=gy)
        ttk.Button(bf, text="文件…", command=self._browse_file).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(bf, text="文件夹…", command=self._browse_dir).pack(side=tk.LEFT)

        ttk.Label(lf_io, text="输出目录").grid(row=2, column=0, sticky="e", padx=px_lab, pady=gy)
        ttk.Entry(lf_io, textvariable=self.var_output).grid(row=2, column=1, sticky="ew", pady=gy)
        ttk.Button(lf_io, text="浏览…", command=self._browse_output).grid(
            row=2, column=2, sticky="ew", padx=(10, 0), pady=gy
        )

        # --- 区块 2：元数据 ---
        lf_meta = ttk.LabelFrame(main, text="元数据（可选）", padding=(12, 10))
        lf_meta.grid(row=r, column=0, sticky="nsew", pady=(0, 10))
        r += 1
        lf_meta.columnconfigure(0, minsize=88, weight=0)
        lf_meta.columnconfigure(1, weight=1)
        for i, (lab, var) in enumerate(
            (
                ("书名", self.var_title),
                ("作者", self.var_author),
                ("出版社", self.var_publisher),
            ),
            start=0,
        ):
            ttk.Label(lf_meta, text=lab).grid(row=i, column=0, sticky="e", padx=px_lab, pady=gy)
            ttk.Entry(lf_meta, textvariable=var).grid(row=i, column=1, sticky="ew", pady=gy)

        # --- 区块 3：选项（勾选 2×2 等宽列；分隔线；下拉与数字统一网格）---
        lf_opt = ttk.LabelFrame(main, text="转换选项", padding=(12, 10))
        lf_opt.grid(row=r, column=0, sticky="nsew", pady=(0, 10))
        r += 1
        lf_opt.columnconfigure(0, weight=1)

        chk_wrap = ttk.Frame(lf_opt)
        chk_wrap.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        chk_wrap.columnconfigure(0, weight=1, uniform="chk")
        chk_wrap.columnconfigure(1, weight=1, uniform="chk")

        ttk.Checkbutton(chk_wrap, text="双页裁切（需 numpy）", variable=self.var_split).grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=gy
        )
        ttk.Checkbutton(chk_wrap, text="横图逆时针旋转 90°", variable=self.var_rotate).grid(
            row=0, column=1, sticky="w", padx=(0, 8), pady=gy
        )
        ttk.Checkbutton(chk_wrap, text="保留中间 .kpf", variable=self.var_keep_kpf).grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=gy
        )
        ttk.Checkbutton(chk_wrap, text="详细日志 (DEBUG)", variable=self.var_debug).grid(
            row=1, column=1, sticky="w", padx=(0, 8), pady=gy
        )

        ttk.Separator(lf_opt, orient=tk.HORIZONTAL).grid(row=1, column=0, sticky="ew", pady=(8, 10))

        adv = ttk.Frame(lf_opt)
        adv.grid(row=2, column=0, sticky="ew")
        adv.columnconfigure(0, minsize=120, weight=0)
        adv.columnconfigure(1, weight=1)

        def row_adv(row: int, caption: str, w: Any) -> None:
            ttk.Label(adv, text=caption).grid(row=row, column=0, sticky="e", padx=px_lab, pady=gy)
            w.grid(row=row, column=1, sticky="w", pady=gy)

        cb_pg = ttk.Combobox(
            adv,
            textvariable=self.var_page_prog,
            values=("ltr", "rtl"),
            state="readonly",
            width=20,
        )
        row_adv(0, "翻页方向", cb_pg)

        cb_so = ttk.Combobox(
            adv,
            textvariable=self.var_split_order,
            values=("right-left", "left-right"),
            state="readonly",
            width=20,
        )
        row_adv(1, "裁切顺序", cb_so)

        self.cmb_layout = ttk.Combobox(
            adv,
            textvariable=self.var_layout,
            values=("fixed", "virtual"),
            state="readonly",
            width=20,
        )
        row_adv(2, "KDF 布局", self.cmb_layout)

        self.cmb_vaxis = ttk.Combobox(
            adv,
            textvariable=self.var_vaxis,
            values=("vertical", "horizontal"),
            state="readonly",
            width=20,
        )
        row_adv(3, "虚拟面板轴向", self.cmb_vaxis)

        if hasattr(ttk, "Spinbox"):
            sp = ttk.Spinbox(adv, from_=1, to=32, textvariable=self.var_jobs, width=10)
        else:
            sp = tk.Spinbox(adv, from_=1, to=32, textvariable=self.var_jobs, width=10)
        row_adv(4, "并发线程", sp)

        self.cmb_layout.bind("<<ComboboxSelected>>", lambda _e: self._sync_vaxis_state())
        self._sync_vaxis_state()

        # --- 日志 ---
        ttk.Label(main, text="运行日志").grid(row=r, column=0, sticky="w", pady=(2, 6))
        r += 1

        log_box = ttk.Frame(main)
        log_box.grid(row=r, column=0, sticky="nsew", pady=(0, 10))
        main.rowconfigure(r, weight=1)
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)

        self.txt_log = tk.Text(
            log_box,
            height=14,
            wrap="word",
            font=self._log_font,
            padx=8,
            pady=8,
        )
        sb = ttk.Scrollbar(log_box, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns", padx=(4, 0))

        # --- 底部按钮 ---
        foot = ttk.Frame(main)
        foot.grid(row=r + 1, column=0, sticky="w", pady=(6, 0))
        self.btn_run = ttk.Button(foot, text="开始转换", command=self._on_run)
        self.btn_run.grid(row=0, column=0, padx=(0, 10))
        ttk.Button(foot, text="清空日志", command=self._clear_log).grid(row=0, column=1)

    def _sync_vaxis_state(self) -> None:
        if self.var_layout.get() == "virtual":
            self.cmb_vaxis.configure(state="readonly")
        else:
            self.cmb_vaxis.configure(state="disabled")

    def _browse_file(self) -> None:
        p = filedialog.askopenfilename(
            title="选择 EPUB 或漫画包",
            filetypes=[
                ("支持的格式", "*.epub *.zip *.cbz *.rar *.cbr"),
                ("所有文件", "*.*"),
            ],
        )
        if p:
            self.var_input.set(p)

    def _browse_dir(self) -> None:
        p = filedialog.askdirectory(title="选择包含漫画文件的文件夹")
        if p:
            self.var_input.set(p)

    def _browse_output(self) -> None:
        p = filedialog.askdirectory(title="选择 KFX 输出目录")
        if p:
            self.var_output.set(p)

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", tk.END)

    def _append_log(self, line: str) -> None:
        self.txt_log.insert(tk.END, line + "\n")
        self.txt_log.see(tk.END)

    def _poll_log_queue(self) -> None:
        self._poll_scheduled = False
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        if self._busy:
            self._poll_scheduled = True
            self.root.after(120, self._poll_log_queue)

    def _ensure_poll(self) -> None:
        if not self._poll_scheduled:
            self._poll_scheduled = True
            self.root.after(120, self._poll_log_queue)

    def _on_run(self) -> None:
        if self._busy:
            return
        raw = self.var_input.get().strip()
        if not raw:
            messagebox.showwarning("提示", "请先选择输入文件或文件夹。")
            return
        inp = Path(raw)
        if not inp.exists():
            messagebox.showerror("错误", f"路径不存在：{inp}")
            return

        try:
            kck_main.resolve_input_list(inp)
        except (OSError, ValueError) as e:
            messagebox.showerror("错误", str(e))
            return

        out_s = self.var_output.get().strip()
        if out_s:
            op = Path(out_s).expanduser()
            if op.exists() and not op.is_dir():
                messagebox.showerror("错误", "输出路径须为目录。")
                return

        try:
            jobs = max(1, min(32, int(self.var_jobs.get().strip() or "1")))
        except ValueError:
            messagebox.showerror("错误", "并发线程数须为整数。")
            return

        def _strip(s: str) -> str | None:
            t = s.strip()
            return t if t else None

        self._busy = True
        self.btn_run.configure(state="disabled")
        self._append_log("—— 开始转换 ——")
        self._ensure_poll()

        def done(ok: bool, err_msg: str | None) -> None:
            self._busy = False
            self.btn_run.configure(state="normal")
            self._poll_log_queue()
            if ok:
                messagebox.showinfo("完成", "转换成功。")
            else:
                messagebox.showerror("失败", err_msg or "未知错误")

        kw = {
            "input_path": inp,
            "output_dir_str": out_s,
            "split_spreads": self.var_split.get(),
            "split_page_order": self.var_split_order.get(),  # type: ignore[arg-type]
            "rotate_landscape_90": self.var_rotate.get(),
            "page_progression": self.var_page_prog.get(),  # type: ignore[arg-type]
            "layout_view": self.var_layout.get(),  # type: ignore[arg-type]
            "virtual_panel_axis": self.var_vaxis.get(),  # type: ignore[arg-type]
            "keep_kpf": self.var_keep_kpf.get(),
            "book_title": _strip(self.var_title.get()),
            "book_author": _strip(self.var_author.get()),
            "book_publisher": _strip(self.var_publisher.get()),
            "jobs": jobs,
            "debug": self.var_debug.get(),
            "log_queue": self._log_queue,
            "on_done": lambda o, m: self.root.after(0, lambda: done(o, m)),
        }

        threading.Thread(target=lambda: _run_conversion(**kw), daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    try:
        import multiprocessing

        multiprocessing.freeze_support()
    except ImportError:
        pass
    try:
        KckfxgenGui().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        tb = traceback.format_exc()
        log_p: Path | None = None
        try:
            log_p = _crash_log_path()
            log_p.write_text(tb, encoding="utf-8")
        except OSError:
            log_p = None
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(
                "kckfxgen 启动失败",
                (f"详情已写入：\n{log_p}\n\n" if log_p else "") + tb[:1200],
            )
            r.destroy()
        except Exception:
            pass
        sys.exit(1)
