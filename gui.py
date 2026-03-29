#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""kckfxgen 图形界面：将 EPUB / 漫画包 / 目录转为 KFX（tkinter，无额外依赖）。"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Any, Literal

# 保证从仓库根目录运行时能 import main、kckfxgen
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from kckfxgen.cli_log import configure_logging

import main as kck_main


class _GuiLogHandler(logging.Handler):
    """将日志行写入队列，由主线程刷到文本框。"""

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
        self.root.title("kckfxgen — Kindle 漫画 KFX")
        self.root.minsize(720, 560)
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

    def _build(self) -> None:
        pad = {"padx": 6, "pady": 4}
        root_f = ttk.Frame(self.root, padding=8)
        root_f.pack(fill=tk.BOTH, expand=True)

        # 输入
        ttk.Label(root_f, text="输入（EPUB / ZIP / CBZ / RAR / CBR 或含上述文件的文件夹）").grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )
        ttk.Entry(root_f, textvariable=self.var_input, width=64).grid(
            row=1, column=0, columnspan=2, sticky="ew", **pad
        )
        bf = ttk.Frame(root_f)
        bf.grid(row=1, column=2, sticky="e", **pad)
        ttk.Button(bf, text="浏览文件…", command=self._browse_file).pack(side=tk.LEFT, padx=2)
        ttk.Button(bf, text="浏览文件夹…", command=self._browse_dir).pack(side=tk.LEFT, padx=2)

        # 输出
        ttk.Label(
            root_f,
            text="输出目录（可选；留空则与各自输入同目录。多文件时若填写则全部 KFX 写入该目录）",
        ).grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Entry(root_f, textvariable=self.var_output, width=64).grid(
            row=3, column=0, columnspan=2, sticky="ew", **pad
        )
        ttk.Button(root_f, text="浏览…", command=self._browse_output).grid(
            row=3, column=2, sticky="e", **pad
        )

        # 元数据
        meta = ttk.LabelFrame(root_f, text="元数据覆盖（可选）", padding=6)
        meta.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Label(meta, text="书名").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(meta, textvariable=self.var_title, width=40).grid(
            row=0, column=1, sticky="ew", padx=4, pady=2
        )
        ttk.Label(meta, text="作者").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(meta, textvariable=self.var_author, width=40).grid(
            row=1, column=1, sticky="ew", padx=4, pady=2
        )
        ttk.Label(meta, text="出版社").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(meta, textvariable=self.var_publisher, width=40).grid(
            row=2, column=1, sticky="ew", padx=4, pady=2
        )
        meta.columnconfigure(1, weight=1)

        # 选项
        opt = ttk.LabelFrame(root_f, text="转换选项", padding=6)
        opt.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Checkbutton(opt, text="双页裁切（需 numpy）", variable=self.var_split).grid(
            row=0, column=0, sticky="w", padx=4, pady=2
        )
        ttk.Checkbutton(opt, text="横图逆时针旋转 90°", variable=self.var_rotate).grid(
            row=0, column=1, sticky="w", padx=4, pady=2
        )
        ttk.Checkbutton(opt, text="保留中间 .kpf", variable=self.var_keep_kpf).grid(
            row=1, column=0, sticky="w", padx=4, pady=2
        )
        ttk.Checkbutton(opt, text="详细日志 (DEBUG)", variable=self.var_debug).grid(
            row=1, column=1, sticky="w", padx=4, pady=2
        )

        ttk.Label(opt, text="翻页方向").grid(row=2, column=0, sticky="e", padx=4, pady=2)
        ttk.Combobox(
            opt,
            textvariable=self.var_page_prog,
            values=("ltr", "rtl"),
            state="readonly",
            width=12,
        ).grid(row=2, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(opt, text="裁切顺序").grid(row=3, column=0, sticky="e", padx=4, pady=2)
        ttk.Combobox(
            opt,
            textvariable=self.var_split_order,
            values=("right-left", "left-right"),
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(opt, text="KDF 布局").grid(row=4, column=0, sticky="e", padx=4, pady=2)
        self.cmb_layout = ttk.Combobox(
            opt,
            textvariable=self.var_layout,
            values=("fixed", "virtual"),
            state="readonly",
            width=12,
        )
        self.cmb_layout.grid(row=4, column=1, sticky="w", padx=4, pady=2)
        self.cmb_layout.bind("<<ComboboxSelected>>", lambda _e: self._sync_vaxis_state())

        ttk.Label(opt, text="虚拟面板轴向").grid(row=5, column=0, sticky="e", padx=4, pady=2)
        self.cmb_vaxis = ttk.Combobox(
            opt,
            textvariable=self.var_vaxis,
            values=("vertical", "horizontal"),
            state="readonly",
            width=12,
        )
        self.cmb_vaxis.grid(row=5, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(opt, text="并发线程（多文件）").grid(row=6, column=0, sticky="e", padx=4, pady=2)
        ttk.Spinbox(opt, from_=1, to=32, textvariable=self.var_jobs, width=8).grid(
            row=6, column=1, sticky="w", padx=4, pady=2
        )

        opt.columnconfigure(1, weight=1)
        self._sync_vaxis_state()

        # 日志
        ttk.Label(root_f, text="日志").grid(row=6, column=0, sticky="nw", **pad)
        log_f = ttk.Frame(root_f)
        log_f.grid(row=7, column=0, columnspan=3, sticky="nsew", **pad)
        self.txt_log = tk.Text(log_f, height=14, wrap="word", state="normal")
        sb = ttk.Scrollbar(log_f, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 按钮
        btn_f = ttk.Frame(root_f)
        btn_f.grid(row=8, column=0, columnspan=3, pady=8)
        self.btn_run = ttk.Button(btn_f, text="开始转换", command=self._on_run)
        self.btn_run.pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_f, text="清空日志", command=self._clear_log).pack(side=tk.LEFT, padx=4)

        root_f.rowconfigure(7, weight=1)
        root_f.columnconfigure(0, weight=1)

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
            # 排空队列
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
        KckfxgenGui().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
