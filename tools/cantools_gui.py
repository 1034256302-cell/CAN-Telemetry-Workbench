from __future__ import annotations

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import cantools_quick as cq
from cantools.database.can.database import Database as CanDatabase

STATE_FILE = ROOT / "tools" / ".gui_state.json"


def open_in_explorer(path: Path) -> None:
    """在 Windows 资源管理器中打开文件夹或高亮选中目标文件。"""
    target = path if path.exists() else path.parent
    if os.name == "nt":
        if target.is_file():
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(target)])


class CantoolsWorkbenchApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        cq.ensure_outputs()

        self.title("CAN 日志解析与分析工作台 (Cantools Workbench)")
        self.geometry("1180x780")
        self.minsize(980, 660)

        self.dbc_path_var = tk.StringVar(value=str(cq.DEFAULT_DBC))
        self.log_path_var = tk.StringVar(value=str(cq.SAMPLE_LOG))
        self.dbc_status_var = tk.StringVar(value="未加载")
        self.status_var = tk.StringVar(value="就绪 — 请选择 DBC 与 CAN 日志文件开始使用")
        self.last_output_path: Path = cq.OUTPUT_DIR

        self.db: CanDatabase | None = None
        self.db_encoding: str = "utf-8"
        self._current_plot_image: tk.PhotoImage | None = None
        self._current_plot_path: Path | None = None
        self._is_exporting: bool = False

        self._load_state()
        if not Path(self.log_path_var.get()).exists():
            cq.write_sample_log(cq.SAMPLE_LOG)
            self.log_path_var.set(str(cq.SAMPLE_LOG))

        self._configure_styles()
        self._build_ui()
        self.reload_dbc(silent=True)
        self.refresh_decode_preview()

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(8, 4))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 5))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TNotebook.Tab", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 6))
        style.configure("Treeview", font=("Consolas", 9), rowheight=24)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _load_state(self) -> None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                dbc_p = data.get("dbc_path", "")
                log_p = data.get("log_path", "")
                if dbc_p and Path(dbc_p).exists():
                    self.dbc_path_var.set(dbc_p)
                if log_p and Path(log_p).exists():
                    self.log_path_var.set(log_p)
            except Exception:
                pass

    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(
                json.dumps(
                    {
                        "dbc_path": self.dbc_path_var.get(),
                        "log_path": self.log_path_var.get(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _build_ui(self) -> None:
        # 1. 顶部文件与输出目录配置栏
        top_frame = ttk.LabelFrame(self, text=" 📂 文件与中文分类输出目录配置 ", padding=10)
        top_frame.pack(fill=tk.X, padx=10, pady=(8, 4))

        ttk.Label(top_frame, text="DBC 数据库:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6), pady=3)
        dbc_entry = ttk.Entry(top_frame, textvariable=self.dbc_path_var, width=68)
        dbc_entry.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=3)
        ttk.Button(top_frame, text="浏览选择 DBC...", command=self.browse_dbc).grid(row=0, column=2, padx=4, pady=3)
        ttk.Button(top_frame, text="重新加载 DBC", command=self.reload_dbc).grid(row=0, column=3, padx=4, pady=3)
        ttk.Label(top_frame, textvariable=self.dbc_status_var, foreground="#0066CC").grid(
            row=0, column=4, sticky=tk.W, padx=8, pady=3
        )

        ttk.Label(top_frame, text="CAN 日志文件:").grid(row=1, column=0, sticky=tk.W, padx=(0, 6), pady=3)
        log_entry = ttk.Entry(top_frame, textvariable=self.log_path_var, width=68)
        log_entry.grid(row=1, column=1, sticky=tk.EW, padx=4, pady=3)
        ttk.Button(top_frame, text="浏览选择日志...", command=self.browse_log).grid(row=1, column=2, padx=4, pady=3)
        ttk.Button(top_frame, text="生成测试日志", command=self.create_sample_log_action).grid(
            row=1, column=3, padx=4, pady=3
        )

        quick_dir_frame = ttk.Frame(top_frame)
        quick_dir_frame.grid(row=1, column=4, sticky=tk.W, padx=8, pady=3)
        ttk.Button(
            quick_dir_frame,
            text="📁 打开七大分类总目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIR),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            quick_dir_frame,
            text="📍 打开最近输出位置",
            command=lambda: open_in_explorer(self.last_output_path),
        ).pack(side=tk.LEFT)

        top_frame.columnconfigure(1, weight=1)

        # 2. 五大功能标签页
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        self._build_tab_decode()
        self._build_tab_health()
        self._build_tab_plot()
        self._build_tab_dbc()
        self._build_tab_encode()

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # 3. 底部状态栏与进度条
        bottom_frame = ttk.Frame(self, padding=(10, 4))
        bottom_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.progress = ttk.Progressbar(bottom_frame, mode="determinate", length=220)
        self.progress.pack(side=tk.RIGHT, padx=(8, 0))

        status_label = ttk.Label(bottom_frame, textvariable=self.status_var)
        status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _on_tab_changed(self, _event: object = None) -> None:
        current_tab = self.notebook.index(self.notebook.select())
        # 切换到【📈 信号波形绘图 (PNG)】标签页且尚未生成图片时，自动绘制默认信号预览
        if current_tab == 2 and self._current_plot_image is None:
            self.after(100, lambda: self.plot_selected_signals_action(silent_error=True))

    # ------------------------------------------------------------------
    # Tab 1: 日志解码与导出 (CSV / TXT)
    # ------------------------------------------------------------------
    def _build_tab_decode(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=" 🔍 日志解码与导出 (CSV/TXT) ")

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(toolbar, text="按报文过滤:").pack(side=tk.LEFT, padx=(0, 4))
        self.filter_msg_var = tk.StringVar(value="全部报文 (All)")
        self.filter_msg_combo = ttk.Combobox(
            toolbar, textvariable=self.filter_msg_var, state="readonly", width=32
        )
        self.filter_msg_combo.pack(side=tk.LEFT, padx=4)
        self.filter_msg_combo.bind("<<ComboboxSelected>>", lambda _: self.refresh_decode_preview())

        ttk.Button(toolbar, text="🔄 刷新预览 (前500帧)", command=self.refresh_decode_preview).pack(
            side=tk.LEFT, padx=6
        )

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(
            toolbar,
            text="📥 导出 Excel 表格 (存入 3_日志解码表格_CSV)",
            style="Accent.TButton",
            command=self.export_csv_action,
        ).pack(side=tk.LEFT, padx=4)

        ttk.Button(
            toolbar,
            text="📄 导出可读文本 (存入 2_日志解码文本_TXT)",
            command=self.export_txt_action,
        ).pack(side=tk.LEFT, padx=4)

        ttk.Button(
            toolbar,
            text="📂 打开 CSV 目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIRS["decoded_csv"]),
        ).pack(side=tk.RIGHT, padx=4)

        cols = ("index", "timestamp_sec", "channel", "frame_id", "message", "raw_data", "decoded_signals", "status")
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self.decode_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        headers = {
            "index": ("#", 50),
            "timestamp_sec": ("时间(秒)", 100),
            "channel": ("通道", 70),
            "frame_id": ("CAN ID", 85),
            "message": ("报文名称", 150),
            "raw_data": ("原始数据 (Hex)", 190),
            "decoded_signals": ("解码后物理信号值", 360),
            "status": ("状态", 90),
        }
        for c, (title, width) in headers.items():
            self.decode_tree.heading(c, text=title)
            self.decode_tree.column(c, width=width, anchor=tk.W if c != "index" else tk.CENTER)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.decode_tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.decode_tree.xview)
        self.decode_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.decode_tree.grid(row=0, column=0, sticky=tk.NSEW)
        vsb.grid(row=0, column=1, sticky=tk.NS)
        hsb.grid(row=1, column=0, sticky=tk.EW)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

    # ------------------------------------------------------------------
    # Tab 2: 日志体检与周期统计
    # ------------------------------------------------------------------
    def _build_tab_health(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=" 📊 日志体检与周期统计 ")

        top_bar = ttk.Frame(tab)
        top_bar.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(
            top_bar,
            text="🩺 立即体检并导出统计报告 (存入 7_日志体检与周期报告)",
            style="Accent.TButton",
            command=self.run_health_report_action,
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.health_summary_var = tk.StringVar(
            value="点击左侧按钮扫描 CAN 日志，自动统计各 CAN ID 帧数、平均周期与 DBC 匹配情况。"
        )
        ttk.Label(top_bar, textvariable=self.health_summary_var, foreground="#225588").pack(side=tk.LEFT, padx=6)

        ttk.Button(
            top_bar,
            text="📂 打开体检报告目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIRS["log_reports"]),
        ).pack(side=tk.RIGHT, padx=4)

        cols = (
            "frame_id_hex",
            "frame_id_dec",
            "message_name",
            "dbc_status",
            "frame_count",
            "avg_cycle_ms",
            "min_cycle_ms",
            "max_cycle_ms",
            "dbc_cycle_ms",
            "channels",
            "dlc_bytes",
        )
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self.health_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        headers = {
            "frame_id_hex": ("CAN ID (Hex)", 95),
            "frame_id_dec": ("ID (十进制)", 85),
            "message_name": ("DBC 报文名称", 170),
            "dbc_status": ("匹配状态", 85),
            "frame_count": ("总帧数", 80),
            "avg_cycle_ms": ("平均周期(ms)", 105),
            "min_cycle_ms": ("最小周期(ms)", 105),
            "max_cycle_ms": ("最大周期(ms)", 105),
            "dbc_cycle_ms": ("DBC定义周期(ms)", 120),
            "channels": ("通道", 80),
            "dlc_bytes": ("字节数(DLC)", 90),
        }
        for c, (title, width) in headers.items():
            self.health_tree.heading(c, text=title)
            self.health_tree.column(c, width=width, anchor=tk.CENTER if c != "message_name" else tk.W)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.health_tree.yview)
        self.health_tree.configure(yscrollcommand=vsb.set)
        self.health_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

    # ------------------------------------------------------------------
    # Tab 3: 信号波形绘图 (PNG)
    # ------------------------------------------------------------------
    def _build_tab_plot(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=" 📈 信号波形绘图 (PNG) ")

        paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # 左侧：信号多选列表
        left_frame = ttk.LabelFrame(paned, text=" 1. 选择要绘制的信号 (可点击多选，双击直接画图) ", padding=8)
        paned.add(left_frame, weight=2)

        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(search_frame, text="搜索信号:").pack(side=tk.LEFT)
        self.sig_search_var = tk.StringVar()
        self.sig_search_var.trace_add("write", lambda *_: self._populate_signal_listbox())
        ttk.Entry(search_frame, textvariable=self.sig_search_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.sig_listbox = tk.Listbox(
            list_frame, selectmode=tk.MULTIPLE, font=("Consolas", 9), exportselection=False
        )
        self.sig_listbox.bind("<Double-Button-1>", lambda _: self.plot_selected_signals_action())
        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.sig_listbox.yview)
        self.sig_listbox.configure(yscrollcommand=vsb.set)
        self.sig_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        btn_row = ttk.Frame(left_frame)
        btn_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(
            btn_row,
            text="全选并绘图",
            command=lambda: (self.sig_listbox.select_set(0, tk.END), self.plot_selected_signals_action()),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="清空选择", command=lambda: self.sig_listbox.selection_clear(0, tk.END)).pack(
            side=tk.LEFT, padx=4
        )

        # 右侧：波形图生成与预览
        right_frame = ttk.LabelFrame(
            paned, text=" 2. 生成与预览波形图 (保存至 4_信号波形图_PNG) ", padding=8
        )
        paned.add(right_frame, weight=5)

        ctrl_row = ttk.Frame(right_frame)
        ctrl_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(ctrl_row, text="输出文件名:").pack(side=tk.LEFT)
        self.plot_filename_var = tk.StringVar(value="signals_plot.png")
        ttk.Entry(ctrl_row, textvariable=self.plot_filename_var, width=24).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            ctrl_row,
            text="🎨 生成并预览波形图",
            style="Accent.TButton",
            command=self.plot_selected_signals_action,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            ctrl_row,
            text="🖼️ 用系统看图器打开大图",
            command=self.open_current_plot_external,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            ctrl_row,
            text="📂 打开图片目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIRS["plots"]),
        ).pack(side=tk.RIGHT, padx=4)

        self.plot_preview_label = ttk.Label(
            right_frame,
            text="👈 请在左侧选择 1 个或多个信号，然后点击【🎨 生成并预览波形图】",
            anchor=tk.CENTER,
        )
        self.plot_preview_label.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # Tab 4: DBC 结构浏览与生成 C 代码
    # ------------------------------------------------------------------
    def _build_tab_dbc(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=" 📋 DBC 结构浏览与生成 C 代码 ")

        action_bar = ttk.Frame(tab)
        action_bar.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(
            action_bar,
            text="📄 导出 DBC 详细结构与位图 (存入 1_DBC数据库文档)",
            style="Accent.TButton",
            command=self.export_dbc_dump_action,
        ).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Button(
            action_bar,
            text="💻 生成嵌入式 C 语言代码 (存入 5_生成C语言代码)",
            command=self.export_c_code_action,
        ).pack(side=tk.LEFT, padx=6)

        ttk.Button(
            action_bar,
            text="📂 打开 C 代码目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIRS["c_source"]),
        ).pack(side=tk.RIGHT, padx=4)
        ttk.Button(
            action_bar,
            text="📂 打开 DBC 文档目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIRS["dbc_info"]),
        ).pack(side=tk.RIGHT, padx=4)

        paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        msg_frame = ttk.LabelFrame(paned, text=" 报文列表 (Messages) ", padding=6)
        paned.add(msg_frame, weight=2)

        msg_cols = ("frame_id", "name", "length", "cycle_time", "senders", "sig_count")
        self.dbc_msg_tree = ttk.Treeview(msg_frame, columns=msg_cols, show="headings")
        msg_headers = {
            "frame_id": ("CAN ID", 75),
            "name": ("报文名称", 145),
            "length": ("字节数", 55),
            "cycle_time": ("周期(ms)", 70),
            "senders": ("发送节点", 80),
            "sig_count": ("信号数", 55),
        }
        for c, (title, w) in msg_headers.items():
            self.dbc_msg_tree.heading(c, text=title)
            self.dbc_msg_tree.column(c, width=w, anchor=tk.W if c in ("name", "senders") else tk.CENTER)

        self.dbc_msg_tree.pack(fill=tk.BOTH, expand=True)
        self.dbc_msg_tree.bind("<<TreeviewSelect>>", self._on_dbc_message_selected)

        sig_frame = ttk.LabelFrame(paned, text=" 选中报文的信号定义 (Signals) ", padding=6)
        paned.add(sig_frame, weight=4)

        sig_cols = ("name", "start", "length", "byte_order", "is_signed", "scale", "offset", "min_max", "unit", "choices")
        self.dbc_sig_tree = ttk.Treeview(sig_frame, columns=sig_cols, show="headings")
        sig_headers = {
            "name": ("信号名称", 130),
            "start": ("起始位", 60),
            "length": ("位长(bit)", 70),
            "byte_order": ("字节序", 85),
            "is_signed": ("符号", 60),
            "scale": ("比例(Scale)", 80),
            "offset": ("偏移(Offset)", 80),
            "min_max": ("物理范围", 110),
            "unit": ("单位", 60),
            "choices": ("枚举定义 (VAL_)", 180),
        }
        for c, (title, w) in sig_headers.items():
            self.dbc_sig_tree.heading(c, text=title)
            self.dbc_sig_tree.column(c, width=w, anchor=tk.W if c in ("name", "choices") else tk.CENTER)

        self.dbc_sig_tree.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # Tab 5: 信号反向编码器
    # ------------------------------------------------------------------
    def _build_tab_encode(self) -> None:
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text=" 🛠️ 信号反向编码器 ")

        top_row = ttk.Frame(tab)
        top_row.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(top_row, text="选择要编码的报文:").pack(side=tk.LEFT)
        self.encode_msg_var = tk.StringVar()
        self.encode_msg_combo = ttk.Combobox(top_row, textvariable=self.encode_msg_var, state="readonly", width=35)
        self.encode_msg_combo.pack(side=tk.LEFT, padx=6)
        self.encode_msg_combo.bind("<<ComboboxSelected>>", lambda _: self._populate_encode_fields())

        ttk.Button(
            top_row,
            text="⚡ 编码生成 CAN 帧 (存入 6_信号反向编码帧)",
            style="Accent.TButton",
            command=self.encode_frame_action,
        ).pack(side=tk.LEFT, padx=8)

        ttk.Button(
            top_row,
            text="📂 打开编码输出目录",
            command=lambda: open_in_explorer(cq.OUTPUT_DIRS["encoded_frames"]),
        ).pack(side=tk.RIGHT, padx=4)

        paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        encode_outer = ttk.LabelFrame(paned, text=" 输入各信号物理值 (或枚举值) ", padding=6)
        paned.add(encode_outer, weight=3)

        self.encode_canvas = tk.Canvas(encode_outer, highlightthickness=0)
        encode_vsb = ttk.Scrollbar(encode_outer, orient=tk.VERTICAL, command=self.encode_canvas.yview)
        self.encode_fields_frame = ttk.Frame(self.encode_canvas, padding=6)
        self.encode_fields_frame.bind(
            "<Configure>",
            lambda _: self.encode_canvas.configure(scrollregion=self.encode_canvas.bbox("all")),
        )
        self.encode_canvas.create_window((0, 0), window=self.encode_fields_frame, anchor=tk.NW)
        self.encode_canvas.configure(yscrollcommand=encode_vsb.set)
        self.encode_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        encode_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.encode_signal_vars: dict[str, tk.StringVar] = {}

        out_frame = ttk.LabelFrame(paned, text=" 编码生成的 CAN 报文结果 ", padding=10)
        paned.add(out_frame, weight=3)

        self.encode_output_text = tk.Text(out_frame, font=("Consolas", 10), height=12, wrap=tk.WORD)
        self.encode_output_text.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # 事件与操作处理
    # ------------------------------------------------------------------
    def browse_dbc(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 DBC / 数据库文件",
            initialdir=str(Path(self.dbc_path_var.get()).parent),
            filetypes=[
                ("CAN 数据库文件", "*.dbc *.arxml *.kcd *.sym *.cdd"),
                ("DBC 文件 (*.dbc)", "*.dbc"),
                ("所有文件 (*.*)", "*.*"),
            ],
        )
        if path:
            self.dbc_path_var.set(path)
            self._save_state()
            self._current_plot_image = None
            self.reload_dbc()
            self.refresh_decode_preview()

    def browse_log(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 CAN 日志文件",
            initialdir=str(Path(self.log_path_var.get()).parent),
            filetypes=[
                ("CAN 日志文件", "*.log *.trc *.asc *.blf *.txt"),
                ("candump 日志 (*.log)", "*.log"),
                ("PCAN Trace (*.trc)", "*.trc"),
                ("Vector ASC/BLF (*.asc *.blf)", "*.asc *.blf"),
                ("所有文件 (*.*)", "*.*"),
            ],
        )
        if path:
            self.log_path_var.set(path)
            self._save_state()
            self._current_plot_image = None
            self.plot_filename_var.set(f"{Path(path).stem}_signals.png")
            self.refresh_decode_preview()

    def create_sample_log_action(self) -> None:
        path = cq.write_sample_log()
        self.log_path_var.set(str(path))
        self._save_state()
        self._current_plot_image = None
        self.refresh_decode_preview()
        self.status_var.set(f"已生成包含 5 帧渐变温度数据的示例日志: {path}")
        messagebox.showinfo("生成成功", f"已生成示例 CAN 日志：\n{path}\n\n并已自动刷新预览！")

    def reload_dbc(self, silent: bool = False) -> None:
        dbc_path = Path(self.dbc_path_var.get().strip())
        if not dbc_path.exists():
            self.dbc_status_var.set("文件不存在")
            if not silent:
                messagebox.showerror("文件不存在", f"找不到 DBC 文件：\n{dbc_path}")
            return

        try:
            self.db, self.db_encoding = cq.load_database_smart(dbc_path, strict=False)
            sig_total = sum(len(m.signals) for m in self.db.messages)
            self.dbc_status_var.set(
                f"✅ 已加载: {len(self.db.messages)} 个报文, {sig_total} 个信号 (编码: {self.db_encoding})"
            )
            self._populate_filter_combo()
            self._populate_signal_listbox()
            self._populate_dbc_trees()
            self._populate_encode_combo()
            self._save_state()
            self.status_var.set(f"已成功加载数据库: {dbc_path.name}")
        except Exception as exc:
            self.dbc_status_var.set("❌ 加载失败")
            if not silent:
                messagebox.showerror("DBC 加载失败", f"无法解析数据库文件：\n{exc}")

    def _populate_filter_combo(self) -> None:
        items = ["全部报文 (All)"]
        if self.db:
            for msg in self.db.messages:
                items.append(f"0x{msg.frame_id:X} - {msg.name}")
        self.filter_msg_combo["values"] = items
        self.filter_msg_var.set(items[0])

    def _populate_signal_listbox(self) -> None:
        self.sig_listbox.delete(0, tk.END)
        self._listbox_sig_names: list[str] = []
        if not self.db:
            return
        keyword = self.sig_search_var.get().strip().lower()
        for msg in self.db.messages:
            for sig in msg.signals:
                full_name = f"{msg.name}.{sig.name}"
                unit_str = f" [{sig.unit}]" if sig.unit else ""
                display = f"{full_name}{unit_str}  (ID: 0x{msg.frame_id:X})"
                if not keyword or keyword in display.lower():
                    self.sig_listbox.insert(tk.END, display)
                    self._listbox_sig_names.append(f"{msg.name}.{sig.name}")

        if self._listbox_sig_names:
            self.sig_listbox.select_set(0, min(len(self._listbox_sig_names) - 1, 5))

    def _populate_dbc_trees(self) -> None:
        for item in self.dbc_msg_tree.get_children():
            self.dbc_msg_tree.delete(item)
        for item in self.dbc_sig_tree.get_children():
            self.dbc_sig_tree.delete(item)

        if not self.db:
            return

        for msg in self.db.messages:
            self.dbc_msg_tree.insert(
                "",
                tk.END,
                values=(
                    f"0x{msg.frame_id:X} ({msg.frame_id})",
                    msg.name,
                    msg.length,
                    msg.cycle_time if msg.cycle_time is not None else "-",
                    ",".join(msg.senders) if msg.senders else "-",
                    len(msg.signals),
                ),
            )
        children = self.dbc_msg_tree.get_children()
        if children:
            self.dbc_msg_tree.selection_set(children[0])
            self._on_dbc_message_selected()

    def _on_dbc_message_selected(self, _event: object = None) -> None:
        for item in self.dbc_sig_tree.get_children():
            self.dbc_sig_tree.delete(item)
        if not self.db:
            return
        sel = self.dbc_msg_tree.selection()
        if not sel:
            return
        msg_name = self.dbc_msg_tree.item(sel[0], "values")[1]
        try:
            msg = self.db.get_message_by_name(msg_name)
        except KeyError:
            return

        for sig in msg.signals:
            choices_str = ""
            if sig.choices:
                choices_str = ", ".join(f"{k}:{v}" for k, v in list(sig.choices.items())[:6])
            min_max = f"[{sig.minimum if sig.minimum is not None else '-'} | {sig.maximum if sig.maximum is not None else '-'}]"
            self.dbc_sig_tree.insert(
                "",
                tk.END,
                values=(
                    sig.name,
                    sig.start,
                    sig.length,
                    "Motorola(大端)" if sig.byte_order == "big_endian" else "Intel(小端)",
                    "有符号(-)" if sig.is_signed else "无符号(+)",
                    sig.scale,
                    sig.offset,
                    min_max,
                    sig.unit or "-",
                    choices_str or "-",
                ),
            )

    def _populate_encode_combo(self) -> None:
        if not self.db or not self.db.messages:
            self.encode_msg_combo["values"] = []
            return
        names = [m.name for m in self.db.messages]
        self.encode_msg_combo["values"] = names
        self.encode_msg_var.set(names[0])
        self._populate_encode_fields()

    def _populate_encode_fields(self) -> None:
        for child in self.encode_fields_frame.winfo_children():
            child.destroy()
        self.encode_signal_vars.clear()

        if not self.db:
            return
        msg_name = self.encode_msg_var.get()
        try:
            msg = self.db.get_message_by_name(msg_name)
        except KeyError:
            return

        for row_idx, sig in enumerate(msg.signals):
            default_val = sig.initial if sig.initial is not None else (sig.minimum if sig.minimum is not None else sig.offset)
            var = tk.StringVar(value=str(default_val))
            self.encode_signal_vars[sig.name] = var

            unit_str = f" ({sig.unit})" if sig.unit and sig.unit != "-" else ""
            ttk.Label(self.encode_fields_frame, text=f"{sig.name}{unit_str}:").grid(
                row=row_idx, column=0, sticky=tk.W, padx=4, pady=4
            )
            ttk.Entry(self.encode_fields_frame, textvariable=var, width=22).grid(
                row=row_idx, column=1, sticky=tk.W, padx=6, pady=4
            )

            hint_parts = [f"范围: [{sig.minimum}|{sig.maximum}]"]
            if sig.choices:
                choices_txt = ", ".join(f"{k}={v}" for k, v in sig.choices.items())
                hint_parts.append(f"可选值: {choices_txt}")
            ttk.Label(self.encode_fields_frame, text="  ".join(hint_parts), foreground="#666666").grid(
                row=row_idx, column=2, sticky=tk.W, padx=4, pady=4
            )

    def _get_selected_filter_frame_id(self) -> int | None:
        val = self.filter_msg_var.get()
        if not val or val.startswith("全部"):
            return None
        hex_part = val.split("-")[0].strip()
        try:
            return int(hex_part, 16)
        except ValueError:
            return None

    def refresh_decode_preview(self) -> None:
        for item in self.decode_tree.get_children():
            self.decode_tree.delete(item)

        log_path = Path(self.log_path_var.get().strip())
        if not log_path.exists():
            self.status_var.set(f"日志文件不存在: {log_path}")
            return

        filter_id = self._get_selected_filter_frame_id()
        first_dt: datetime.datetime | None = None
        shown = 0

        try:
            for _, frame in cq.iter_log_frames(log_path, keep_unknowns=True):
                if frame is None:
                    continue
                if filter_id is not None and frame.frame_id != filter_id:
                    continue

                shown += 1
                ts_sec, first_dt = cq.timestamp_to_seconds(frame.timestamp, first_dt)
                ts_str = f"{ts_sec:.6f}" if ts_sec is not None else "-"
                raw_hex = " ".join(f"{b:02X}" for b in frame.data)
                msg_name = "<未定义>"
                sig_summary = "-"
                status = "未定义 ID"

                if self.db is not None:
                    try:
                        msg = cq.lookup_message(self.db, frame)
                        msg_name = msg.name
                        decoded = msg.decode(frame.data, allow_truncated=True, allow_excess=True)
                        if isinstance(decoded, dict):
                            parts = []
                            for sig in msg.signals:
                                if sig.name in decoded:
                                    v = decoded[sig.name]
                                    u = f" {sig.unit}" if sig.unit and sig.unit != "-" and not isinstance(v, str) else ""
                                    parts.append(f"{sig.name}={v}{u}")
                            sig_summary = ", ".join(parts)
                        else:
                            sig_summary = str(decoded)
                        status = "OK"
                    except KeyError:
                        status = f"DBC未定义 (0x{frame.frame_id:X})"
                    except Exception as exc:
                        status = f"解码错误: {exc}"

                self.decode_tree.insert(
                    "",
                    tk.END,
                    values=(
                        shown,
                        ts_str,
                        frame.channel or "-",
                        f"0x{frame.frame_id:X}",
                        msg_name,
                        raw_hex,
                        sig_summary,
                        status,
                    ),
                )
                if shown >= 500:
                    break

            self.status_var.set(f"预览已更新：当前显示 {shown} 帧 ({log_path.name})")
        except Exception as exc:
            self.status_var.set(f"读取日志出错: {exc}")

    def export_csv_action(self) -> None:
        if self._is_exporting:
            return
        dbc_path = Path(self.dbc_path_var.get().strip())
        log_path = Path(self.log_path_var.get().strip())
        if not dbc_path.exists() or not log_path.exists():
            messagebox.showwarning("路径无效", "请先选择有效的 DBC 文件和 CAN 日志文件！")
            return

        self._is_exporting = True
        filter_id = self._get_selected_filter_frame_id()
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        self.status_var.set("正在流式导出 CSV 表格，请稍候...")

        msg_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        def worker() -> None:
            try:
                res = cq.export_log_to_csv(
                    dbc_path,
                    log_path,
                    filter_frame_id=filter_id,
                    progress_callback=lambda total, dec: msg_queue.put(("progress", (total, dec))),
                )
                msg_queue.put(("done", res))
            except Exception as exc:
                msg_queue.put(("error", exc))

        def poll_queue() -> None:
            try:
                while True:
                    kind, payload = msg_queue.get_nowait()
                    if kind == "progress":
                        total, dec = payload  # type: ignore[misc]
                        self.status_var.set(f"正在导出 CSV... 已处理 {total} 行 (成功解码 {dec} 帧)")
                    elif kind == "done":
                        res = payload  # type: ignore[assignment]
                        self._is_exporting = False
                        self.last_output_path = res.output_path
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=100)
                        self.status_var.set(f"✅ CSV 导出完成: {res.output_path}")
                        if messagebox.askyesno(
                            "CSV 导出成功",
                            f"已成功导出到【3_日志解码表格_CSV】：\n{res.output_path}\n\n"
                            f"• 总处理行数：{res.total_lines}\n"
                            f"• 成功解码帧数：{res.decoded_frames}\n"
                            f"• 未定义 ID 帧数：{res.unknown_id_frames}\n\n"
                            f"是否立即在资源管理器中打开该文件位置？",
                        ):
                            open_in_explorer(res.output_path)
                        return
                    elif kind == "error":
                        self._is_exporting = False
                        self.progress.stop()
                        self.status_var.set(f"导出失败: {payload}")
                        messagebox.showerror("导出失败", str(payload))
                        return
            except queue.Empty:
                pass
            if self._is_exporting:
                self.after(80, poll_queue)

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, poll_queue)

    def export_txt_action(self) -> None:
        dbc_path = Path(self.dbc_path_var.get().strip())
        log_path = Path(self.log_path_var.get().strip())
        if not dbc_path.exists() or not log_path.exists():
            messagebox.showwarning("路径无效", "请先选择有效的 DBC 文件和 CAN 日志文件！")
            return

        try:
            out = cq.export_log_to_text(dbc_path, log_path)
            self.last_output_path = out
            self.status_var.set(f"✅ TXT 导出完成: {out}")
            if messagebox.askyesno(
                "TXT 导出成功",
                f"已导出可读文本到【2_日志解码文本_TXT】：\n{out}\n\n是否立即打开所在文件夹？",
            ):
                open_in_explorer(out)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def run_health_report_action(self) -> None:
        log_path = Path(self.log_path_var.get().strip())
        dbc_path = Path(self.dbc_path_var.get().strip())
        if not log_path.exists():
            messagebox.showwarning("路径无效", "请先选择有效的 CAN 日志文件！")
            return

        try:
            report = cq.analyze_log_health(log_path, dbc_path if dbc_path.exists() else None)
            self.last_output_path = report.csv_report_path

            for item in self.health_tree.get_children():
                self.health_tree.delete(item)

            for st in report.frame_stats:
                avg_ms = f"{sum(st.intervals_ms)/len(st.intervals_ms):.2f}" if st.intervals_ms else "-"
                min_ms = f"{min(st.intervals_ms):.2f}" if st.intervals_ms else "-"
                max_ms = f"{max(st.intervals_ms):.2f}" if st.intervals_ms else "-"
                dbc_ms = f"{st.dbc_cycle_ms:.0f}" if st.dbc_cycle_ms is not None else "-"
                self.health_tree.insert(
                    "",
                    tk.END,
                    values=(
                        f"0x{st.frame_id:X}",
                        st.frame_id,
                        st.message_name,
                        "✅ 已匹配" if st.dbc_defined else "⚠️ 未定义",
                        st.count,
                        avg_ms,
                        min_ms,
                        max_ms,
                        dbc_ms,
                        ",".join(sorted(st.channels)) or "-",
                        ",".join(str(d) for d in sorted(st.dlcs)),
                    ),
                )

            self.health_summary_var.set(
                f"总帧数: {report.total_frames} | 已匹配: {report.decoded_frames} | "
                f"未定义: {report.unknown_frames} | 时长: {report.duration_sec:.3f}s | "
                f"已存至【7_日志体检与周期报告】"
            )
            self.status_var.set(f"✅ 日志体检完成，报告已保存至: {report.csv_report_path}")
        except Exception as exc:
            messagebox.showerror("日志体检失败", str(exc))

    def plot_selected_signals_action(self, silent_error: bool = False) -> None:
        dbc_path = Path(self.dbc_path_var.get().strip())
        log_path = Path(self.log_path_var.get().strip())
        sel_indices = self.sig_listbox.curselection()
        if not sel_indices:
            if self._listbox_sig_names:
                self.sig_listbox.select_set(0)
                sel_indices = (0,)
            else:
                if not silent_error:
                    messagebox.showwarning("未选择信号", "请先在左侧列表中点击选择至少 1 个信号！")
                return

        signals = [self._listbox_sig_names[i] for i in sel_indices]
        out_name = self.plot_filename_var.get().strip() or f"{log_path.stem}_signals.png"

        try:
            self.status_var.set(f"正在生成波形图: {', '.join(signals)} ...")
            self.update_idletasks()
            out_path = cq.export_signal_plot(dbc_path, log_path, signals, output_name=out_name)
            self.last_output_path = out_path
            self._current_plot_path = out_path

            # 使用内存字节流加载 PNG，彻底规避 Windows/Tcl 中文路径兼容问题与文件占用锁
            png_bytes = out_path.read_bytes()
            img = tk.PhotoImage(data=png_bytes)

            avail_w = max(560, self.plot_preview_label.winfo_width() - 20)
            avail_h = max(380, self.plot_preview_label.winfo_height() - 20)
            w, h = img.width(), img.height()
            if w > avail_w or h > avail_h:
                factor = max(1, int(max(w / avail_w, h / avail_h) + 0.85))
                if factor > 1:
                    img = img.subsample(factor, factor)

            self._current_plot_image = img
            self.plot_preview_label.configure(image=img, text="")
            self.status_var.set(f"✅ 波形图已生成并保存至【4_信号波形图_PNG】: {out_path.name}")
        except Exception as exc:
            if not silent_error:
                messagebox.showerror("绘图失败", f"生成信号波形图失败：\n{exc}")
            self.status_var.set(f"绘图失败: {exc}")

    def open_current_plot_external(self) -> None:
        if self._current_plot_path and self._current_plot_path.exists():
            os.startfile(str(self._current_plot_path))  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("提示", "请先点击【🎨 生成并预览波形图】生成图片。")

    def export_dbc_dump_action(self) -> None:
        dbc_path = Path(self.dbc_path_var.get().strip())
        if not dbc_path.exists():
            return
        try:
            out = cq.export_dbc_info(dbc_path, mode="dump")
            self.last_output_path = out
            self.status_var.set(f"✅ DBC 结构文档已导出: {out}")
            if messagebox.askyesno(
                "导出成功",
                f"DBC 详细结构与位图已保存至【1_DBC数据库文档】：\n{out}\n\n是否立即打开所在文件夹？",
            ):
                open_in_explorer(out)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def export_c_code_action(self) -> None:
        dbc_path = Path(self.dbc_path_var.get().strip())
        if not dbc_path.exists():
            return
        try:
            files = cq.export_c_source(dbc_path)
            self.last_output_path = files[0]
            file_list = "\n".join(f"• {f.name}" for f in files)
            self.status_var.set(f"✅ C 语言代码已生成至: {cq.OUTPUT_DIRS['c_source']}")
            if messagebox.askyesno(
                "C 代码生成成功",
                f"已在【5_生成C语言代码】生成以下文件：\n{file_list}\n\n是否立即打开该文件夹？",
            ):
                open_in_explorer(files[0])
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc))

    def encode_frame_action(self) -> None:
        dbc_path = Path(self.dbc_path_var.get().strip())
        msg_name = self.encode_msg_var.get().strip()
        if not dbc_path.exists() or not msg_name:
            return

        values: dict[str, object] = {}
        for sig_name, var in self.encode_signal_vars.items():
            values[sig_name] = cq.parse_signal_value(var.get())

        try:
            result_txt, out_path = cq.encode_signal_dict(dbc_path, msg_name, values)
            self.last_output_path = out_path
            self.encode_output_text.delete("1.0", tk.END)
            self.encode_output_text.insert(tk.END, result_txt + f"\n已保存文件: {out_path}\n")
            self.status_var.set(f"✅ 信号编码成功，已保存至【6_信号反向编码帧】: {out_path.name}")
        except Exception as exc:
            messagebox.showerror("编码失败", f"信号数值编码失败：\n{exc}")


def launch_gui() -> None:
    app = CantoolsWorkbenchApp()
    app.mainloop()


if __name__ == "__main__":
    launch_gui()
