"""Tkinter 界面：进度列表 + 日志 + 验证码弹窗 + 字段补录弹窗。

线程模型（这里是整个程序最容易出错的地方）
------------------------------------------
* **主线程**只跑 Tk：所有控件操作都必须在这里。
* **工作线程**跑 Runner（含 Playwright）。
* 两者之间只有一个 ``queue.Queue``，主线程用 ``root.after`` 每 80ms 取一次。

验证码和字段补录是「工作线程要等人回答」，所以走的是
「投递请求 → 工作线程阻塞在 threading.Event → 主线程弹窗 → 用户提交 → 唤醒」，
而不是让工作线程直接创建窗口（Tk 不允许跨线程操作控件）。
"""

from __future__ import annotations

import base64
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .captcha import ocr as captcha_ocr
from .captcha.prompt import captcha_hint_text
from .config import Config, load_config
from .runner import FileResult, RunReport, Runner

log = logging.getLogger(__name__)

# 界面配色
COLOR_OK = "#1a7f37"
COLOR_BAD = "#cf222e"
COLOR_WARN = "#bf8700"
COLOR_DIM = "#6b7280"

# 颜色提示在界面上要真的用那个颜色显示，这样一眼就能对上
_COLOR_FG = {
    "blue": "#0b5cd5",
    "red": "#cf222e",
    "green": "#1a7f37",
    "black": "#111827",
}

_STATUS_TAG = {
    "ok": "ok", "mismatch": "bad", "not_found": "bad",
    "captcha_wrong": "warn", "rate_limited": "warn",
    "unknown": "dim", "error": "bad", "skipped": "dim",
}


class _Answer:
    """工作线程等待中的一个回答槽。"""

    __slots__ = ("event", "value", "abandoned")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.abandoned = False

    def resolve(self, value: Any) -> None:
        self.value = value
        self.event.set()

    def abandon(self) -> None:
        self.abandoned = True
        self.event.set()


# ===========================================================================
#  界面桥
# ===========================================================================
class TkUi:
    """Runner 与 Tk 之间的桥。实现 runner.UiBridge 协议。"""

    def __init__(self, root: tk.Tk, window: "MainWindow"):
        self.root = root
        self.window = window
        self.queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()

    # -- 工作线程调用 -------------------------------------------------------
    def log(self, message: str) -> None:
        self.queue.put(("log", message, None))

    def progress(self, done: int, total: int, current: str) -> None:
        self.queue.put(("progress", (done, total, current), None))

    def file_done(self, result: FileResult) -> None:
        self.queue.put(("file_done", result, None))

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0,
                color: str | None = None, color_text: str = "") -> str | None:
        """要一个验证码。工作线程会在这里阻塞，直到主线程弹窗有了结果。"""
        box = _Answer()
        self.queue.put(("captcha", {
            "task_id": task_id, "png": png, "filename": filename,
            "hint": hint, "timeout": timeout,
            "color": color, "color_text": color_text,
        }, box))
        if not box.event.wait(timeout + 10):
            log.warning("等待验证码输入超时")
            box.abandoned = True
            return None
        return box.value or None

    def ask_fields(self, *, filename: str, preview: str, missing: list[str],
                   initial: dict[str, Any]) -> dict[str, Any] | None:
        """请用户手工补录字段。"""
        box = _Answer()
        self.queue.put(("fields", {
            "filename": filename, "preview": preview,
            "missing": missing, "initial": initial,
        }, box))
        if not box.event.wait(600):
            box.abandoned = True
            return None
        return box.value

    # -- 主线程调用 ---------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()

    def reset_stop(self) -> None:
        self._stop.clear()


# ===========================================================================
#  验证码弹窗
# ===========================================================================
class CaptchaDialog(tk.Toplevel):
    def __init__(self, parent: "MainWindow", payload: dict, box: _Answer):
        super().__init__(parent.root)
        self.box = box
        self.payload = payload
        self.deadline = _monotonic() + float(payload.get("timeout") or 180)

        self.title("需要输入验证码")
        self.resizable(False, False)
        self.transient(parent.root)
        self.protocol("WM_DELETE_WINDOW", self._skip)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=f"文件：{payload.get('filename') or payload.get('task_id')}",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        if payload.get("hint"):
            ttk.Label(body, text=f"发票：{payload['hint']}",
                      foreground=COLOR_DIM).pack(anchor="w", pady=(2, 0))

        # 颜色提示放在最上面：平台要求「只填蓝色 / 红色文字」，
        # 而图里混着好几种颜色的字符——不先看到这句就只能瞎猜。
        color = payload.get("color")
        color_key = str(color) if color else ""
        color_text = payload.get("color_text") or ""

        self.color_label = ttk.Label(
            body, text=captcha_hint_text(color, color_text),
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground=_COLOR_FG.get(color_key, COLOR_WARN),
            wraplength=460, justify="left")
        self.color_label.pack(anchor="w", pady=(6, 0))

        # 左：原图（点一下换一张）；右：按提示颜色分离出来的图，该填的就是这些字
        shots = ttk.Frame(body)
        shots.pack(pady=10)

        left = ttk.Frame(shots)
        left.pack(side="left")
        ttk.Label(left, text="原图（点图可换一张）",
                  foreground=COLOR_DIM).pack()
        self.image_label = ttk.Label(left, relief="solid", borderwidth=1)
        self.image_label.pack()
        self._render_image(payload.get("png") or b"")
        self.image_label.bind("<Button-1>", lambda _e: self._skip())

        self.iso_label = None
        isolated = None
        if color_key and payload.get("png"):
            try:
                isolated = captcha_ocr.color_filter(payload["png"], color_key)
            except Exception as exc:
                log.debug("验证码颜色分离失败：%s", exc)
        if isolated:
            right = ttk.Frame(shots)
            right.pack(side="left", padx=(12, 0))
            ttk.Label(right, text=f"只填这些（{captcha_ocr.color_label(color)}）",
                      foreground=_COLOR_FG.get(color_key, COLOR_DIM)).pack()
            self.iso_label = ttk.Label(right, relief="solid", borderwidth=1)
            self.iso_label.pack()
            self._render_image_into(self.iso_label, isolated)

        ttk.Label(body, text="请输入图中的字符（按回车，或点下面的「提交」）",
                  foreground=COLOR_DIM).pack(anchor="w")

        self.entry = ttk.Entry(body, font=("Consolas", 20), width=14,
                               justify="center")
        self.entry.pack(pady=8)
        self.entry.focus_set()
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<KeyRelease>", self._on_key)

        self.hint_label = ttk.Label(body, text="", foreground=COLOR_DIM)
        self.hint_label.pack(anchor="w")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="跳过 / 换一张", command=self._skip).pack(side="right")
        ttk.Button(buttons, text="提交", command=self._submit).pack(side="right", padx=6)

        self.update_idletasks()
        self._center(parent.root)
        self.grab_set()
        self._tick()

    def _render_image(self, png: bytes) -> None:
        self._render_image_into(self.image_label, png)

    def _render_image_into(self, label: ttk.Label, png: bytes,
                           max_px: int = 240) -> None:
        try:
            photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            factor = max(1, min(4, max_px // max(1, photo.width())))
            if factor > 1:
                photo = photo.zoom(factor)
            label.configure(image=photo)
            label.image = photo      # 必须留引用，否则被回收变白
        except Exception as exc:
            log.debug("验证码图片显示失败：%s", exc)
            label.configure(text="（验证码图片无法显示，请点「跳过」重试）",
                            padding=20)

    def _on_key(self, event) -> None:
        """只做「去空格 + 转大写」，**不自动提交**。

        曾经写成「满 4 位就自动提交」，但平台的验证码**不一定是 4 位**：
        提示是「请输入验证码图片中蓝色文字」，图里字符总数可能多于要填的数量，
        到底几位取决于其中有几个是蓝色的。长度不能写死，
        所以提交交给回车或「提交」按钮。
        """
        if event.keysym in {"Return", "Tab", "Shift_L", "Shift_R"}:
            return
        text = self.entry.get().replace(" ", "").upper()
        if text != self.entry.get():
            self.entry.delete(0, "end")
            self.entry.insert(0, text)

    def _submit(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self._close(text)

    def _skip(self) -> None:
        self._close(None)

    def _close(self, value: Any) -> None:
        if self.box.event.is_set():
            return
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.box.resolve(value)

    def _tick(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return          # 窗口已销毁，定时器不必再跑
        if self.box.abandoned or self.box.event.is_set():
            # 工作线程已经放弃了（超时/被停止），自己关掉
            self.destroy()
            return
        left = max(0.0, self.deadline - _monotonic())
        self.hint_label.configure(text=f"剩余 {left:.0f} 秒　（看不清就点「跳过」，会换一张）")
        if left <= 0:
            self._close(None)
            return
        self.after(250, self._tick)

    def _center(self, root: tk.Tk) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


# ===========================================================================
#  字段补录弹窗
# ===========================================================================
_FIELD_SPECS = [
    ("invoice_code", "发票代码", "老发票填 10/12 位；数电票留空"),
    ("invoice_number", "发票号码", "8 位（老票）或 20 位（数电票）"),
    ("invoice_date", "开票日期", "格式 YYYY-MM-DD，如 2024-05-20"),
    ("check_code", "校验码", "填 20 位或后 6 位都行；数电票留空"),
    ("amount", "开具金额", "数电票必填（不含税金额）"),
    ("seller_name", "销方名称", "可选，只用于记录"),
]


class FieldsDialog(tk.Toplevel):
    def __init__(self, parent: "MainWindow", payload: dict, box: _Answer):
        super().__init__(parent.root)
        self.box = box
        self.entries: dict[str, ttk.Entry] = {}

        self.title("手工补录发票字段")
        self.transient(parent.root)
        self.protocol("WM_DELETE_WINDOW", self._skip)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=f"文件：{payload.get('filename')}",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")

        missing = payload.get("missing") or []
        if missing:
            ttk.Label(body, text="缺少：" + "、".join(missing),
                      foreground=COLOR_WARN).pack(anchor="w", pady=(2, 0))

        grid = ttk.Frame(body)
        grid.pack(fill="x", pady=10)
        initial = payload.get("initial") or {}

        for row, (key, label, help_text) in enumerate(_FIELD_SPECS):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w",
                                             padx=(0, 8), pady=3)
            entry = ttk.Entry(grid, width=34)
            entry.grid(row=row, column=1, sticky="we", pady=3)
            value = initial.get(key)
            if value:
                entry.insert(0, str(value))
            self.entries[key] = entry
            ttk.Label(grid, text=help_text, foreground=COLOR_DIM).grid(
                row=row, column=2, sticky="w", padx=(10, 0))
        grid.columnconfigure(1, weight=1)

        # 校验码给了 20 位就自动带出后 6 位，省得用户自己数
        self.entries["check_code"].bind("<KeyRelease>", self._sync_check_code)
        self.check_last6 = tk.StringVar(value=str(initial.get("check_code_last6") or ""))
        ttk.Label(body, textvariable=self.check_last6, foreground=COLOR_DIM).pack(anchor="w")

        preview = payload.get("preview") or ""
        if preview:
            box_frame = ttk.LabelFrame(body, text="识别到的原始内容（供核对）", padding=6)
            box_frame.pack(fill="both", expand=True, pady=(10, 0))
            text = tk.Text(box_frame, height=7, wrap="word", font=("Consolas", 9))
            scroll = ttk.Scrollbar(box_frame, command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            text.insert("1.0", preview[:3000])
            text.configure(state="disabled")
            scroll.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="跳过这张", command=self._skip).pack(side="right")
        ttk.Button(buttons, text="保存并查验", command=self._submit).pack(side="right", padx=6)

        self.entries["invoice_number"].focus_set()
        self.bind("<Return>", lambda _e: self._submit())
        self.update_idletasks()
        self._center(parent.root)
        self.grab_set()

    def _sync_check_code(self, _event=None) -> None:
        digits = "".join(ch for ch in self.entries["check_code"].get() if ch.isdigit())
        if len(digits) >= 6:
            self.check_last6.set(f"→ 查验时使用后 6 位：{digits[-6:]}")
        else:
            self.check_last6.set("")

    def _submit(self) -> None:
        data: dict[str, Any] = {}
        for key, entry in self.entries.items():
            value = entry.get().strip()
            if value:
                data[key] = value
        if not data.get("invoice_number"):
            messagebox.showwarning("还缺字段", "发票号码必须填。", parent=self)
            return
        digits = "".join(ch for ch in str(data.get("check_code", "")) if ch.isdigit())
        if len(digits) == 20:
            data["check_code"] = digits
            data["check_code_last6"] = digits[-6:]
        elif len(digits) == 6:
            data["check_code_last6"] = digits
            data.pop("check_code", None)

        # 数电票（20 位号码）用不到发票代码和校验码，清掉避免填错框
        if len(str(data.get("invoice_number", ""))) == 20:
            data.pop("invoice_code", None)
            data.pop("check_code", None)
            data.pop("check_code_last6", None)
        self._close(data)

    def _skip(self) -> None:
        self._close(None)

    def _close(self, value: Any) -> None:
        if self.box.event.is_set():
            return
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self.box.resolve(value)

    def _center(self, root: tk.Tk) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + max(0, (root.winfo_height() - h) // 4)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")


# ===========================================================================
#  主窗口
# ===========================================================================
class MainWindow:
    def __init__(self, root: tk.Tk, cfg: Config):
        self.root = root
        self.cfg = cfg
        self.runner: Runner | None = None
        self.worker: threading.Thread | None = None
        self.ui = TkUi(root, self)
        self.report: RunReport | None = None
        self._row_of_path: dict[str, str] = {}

        root.title("发票批量查验")
        root.geometry("1000x680")
        root.minsize(820, 520)
        self._build()
        self._refresh_header()
        self._pump()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if cfg.get("ui.auto_start", False):
            root.after(600, self.start)

    # -- 布局 ---------------------------------------------------------------
    def _build(self) -> None:
        style = ttk.Style()
        for candidate in ("vista", "winnative", "clam"):
            if candidate in style.theme_names():
                style.theme_use(candidate)
                break
        default_font = ("Microsoft YaHei UI", 9)
        self.root.option_add("*Font", default_font)
        style.configure("Treeview", rowheight=22)
        style.configure("Big.TButton", padding=(14, 6))

        # 顶部信息
        head = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        head.pack(fill="x")

        title = ttk.Label(head, text="发票批量查验",
                          font=("Microsoft YaHei UI", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")
        self.header_label = ttk.Label(head, text="", foreground=COLOR_DIM)
        self.header_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        head.columnconfigure(0, weight=1)

        # 操作栏
        bar = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        bar.pack(fill="x")

        self.btn_start = ttk.Button(bar, text="开始查验", command=self.start,
                                    style="Big.TButton")
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="打开发票目录", command=self.open_workdir).pack(side="left", padx=6)
        ttk.Button(bar, text="重新选择目录…", command=self.pick_workdir).pack(side="left")
        ttk.Button(bar, text="查看记录", command=self.open_record).pack(side="left", padx=6)

        self.mode_label = ttk.Label(bar, text="", foreground=COLOR_DIM)
        self.mode_label.pack(side="right")

        # 进度
        prog = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(fill="x")
        self.progress_label = ttk.Label(prog, text="尚未开始", foreground=COLOR_DIM)
        self.progress_label.pack(anchor="w", pady=(4, 0))

        # 结果表
        mid = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        mid.pack(fill="both", expand=True)

        columns = ("file", "status", "label", "summary")
        self.tree = ttk.Treeview(mid, columns=columns, show="headings", height=12)
        for key, text, width, anchor in (
            ("file", "文件", 300, "w"),
            ("status", "状态", 80, "center"),
            ("label", "结论", 90, "center"),
            ("summary", "说明", 460, "w"),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "summary"))
        scroll = ttk.Scrollbar(mid, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.tag_configure("ok", foreground=COLOR_OK)
        self.tree.tag_configure("bad", foreground=COLOR_BAD)
        self.tree.tag_configure("warn", foreground=COLOR_WARN)
        self.tree.tag_configure("dim", foreground=COLOR_DIM)

        # 日志
        bottom = ttk.LabelFrame(self.root, text="日志", padding=6)
        bottom.pack(fill="both", expand=False, padx=14, pady=(0, 12))
        self.log_text = tk.Text(bottom, height=9, wrap="word",
                                font=("Consolas", 9), state="disabled")
        log_scroll = ttk.Scrollbar(bottom, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _refresh_header(self) -> None:
        cfg = self.cfg
        driver = str(cfg.get("verify.driver", "playwright"))
        channel = str(cfg.get("verify.browser_channel", "msedge")) or "auto"
        ocr = "开" if cfg.get("captcha.auto_ocr", True) else "关"
        self.header_label.configure(
            text=f"发票目录：{cfg.workdir}　　浏览器：{channel}　　验证码自动识别：{ocr}")
        self.mode_label.configure(
            text=("离线假驱动（演练）" if driver == "fake" else "真实查验")
            + f"　间隔 {cfg.get('verify.min_interval_seconds')}s")

    # -- 队列泵 -------------------------------------------------------------
    def _pump(self) -> None:
        """主线程每 80ms 取一次队列。所有控件操作都发生在这里。"""
        try:
            while True:
                kind, payload, box = self.ui.queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._update_progress(*payload)
                elif kind == "captcha":
                    CaptchaDialog(self, payload, box)
                elif kind == "fields":
                    FieldsDialog(self, payload, box)
                elif kind == "file_done":
                    self._add_result(payload)
                elif kind == "finished":
                    self._on_finished(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return          # 窗口已关闭，停止泵循环
        except Exception:
            log.exception("界面刷新出错")

        try:
            self.root.after(80, self._pump)
        except tk.TclError:
            pass            # 窗口已关闭，不再续订

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        keep = int(self.cfg.get("ui.log_lines", 400))
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > keep:
            self.log_text.delete("1.0", f"{lines - keep}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_progress(self, done: int, total: int, current: str) -> None:
        self.progress.configure(maximum=max(1, total), value=done)
        if total:
            self.progress_label.configure(
                text=f"进度 {done}/{total}" + (f"　当前：{current}" if current else ""))
        else:
            self.progress_label.configure(text="没有待处理的文件")

    def _add_result(self, result: FileResult) -> None:
        tag = _STATUS_TAG.get(result.status, "dim")
        self.tree.insert("", "end", values=(
            result.path.name, result.status, result.label,
            (result.summary or "")[:200],
        ), tags=(tag,))

    # -- 操作 ---------------------------------------------------------------
    def pick_workdir(self) -> None:
        chosen = filedialog.askdirectory(title="选择存放发票的目录",
                                         initialdir=str(self.cfg.workdir))
        if not chosen:
            return
        # 写回 config.yaml，下次打开还记得
        try:
            import yaml

            target = self.cfg.source or str(Path(self.cfg.base) / "config.yaml")
            data = {}
            path = Path(target)
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            data.setdefault("paths", {})["workdir"] = chosen
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
            self._append_log(f"已把发票目录保存到 {path.name}")
        except Exception as exc:
            self._append_log(f"⚠ 目录设置没能写入配置文件：{exc}")

        self.cfg = load_config(self.cfg.source)
        self._refresh_header()

    def open_workdir(self) -> None:
        _open_in_explorer(self.cfg.workdir)

    def open_record(self) -> None:
        _open_in_explorer(self.cfg.path("db").parent)

    def start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return

        self.cfg = load_config(self.cfg.source)
        self.cfg.ensure_dirs()
        problems = self.cfg.validate()
        for problem in problems:
            self._append_log(f"⚠ 配置：{problem}")
        if problems and not self.cfg.workdir.is_dir():
            messagebox.showerror("配置有问题", "\n".join(problems), parent=self.root)
            return

        self._refresh_header()
        self.tree.delete(*self.tree.get_children())
        self.progress.configure(value=0, maximum=1)

        self.ui.reset_stop()
        self.runner = Runner(self.cfg, self.ui)
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        def work() -> None:
            try:
                report = self.runner.run()
            except Exception as exc:
                log.exception("批处理线程异常")
                self.ui.queue.put(("log", f"✗ 批处理异常终止：{exc}", None))
                report = RunReport()
            self.ui.queue.put(("finished", report, None))

        self.worker = threading.Thread(target=work, name="runner", daemon=True)
        self.worker.start()

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()
        self.ui.request_stop()
        self._append_log("已请求停止，正在收尾…")
        self.btn_stop.configure(state="disabled")

    def _on_finished(self, report: RunReport) -> None:
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.report = report
        # 结果行在处理过程中已经通过 file_done 实时插入了，这里只补汇总日志
        if report.results:
            self._append_log("—" * 30)
            self._append_log(report.summary_line())

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("确认退出", "查验还在进行中，确定要退出吗？",
                                       parent=self.root):
                return
            self.stop()
        self.root.destroy()


def _open_in_explorer(path: Path) -> None:
    """在资源管理器里打开一个目录。"""
    import subprocess
    import sys

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        if sys.platform == "win32":
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        log.warning("打开目录失败：%s", exc)


def _monotonic() -> float:
    import time

    return time.monotonic()


# ===========================================================================
#  入口
# ===========================================================================
def main() -> int:
    from .logsetup import setup_logging

    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg.path("log"))

    for problem in cfg.validate():
        log.error("配置问题：%s", problem)

    root = tk.Tk()
    try:
        # 高 DPI 下不糊
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    window = MainWindow(root, cfg)
    root.mainloop()
    return 0
