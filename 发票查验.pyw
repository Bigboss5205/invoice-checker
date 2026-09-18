"""发票批量查验 —— 双击运行入口。

把这个文件（或打包出来的 发票查验.exe）和发票放在同一个目录，双击即可。

打包：见 build.ps1
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _app_dir() -> Path:
    """程序所在目录。打包后是 exe 旁边，源码运行是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bootstrap() -> None:
    """让 `import app...` 在源码运行和打包运行两种情况下都能找到包。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", _app_dir()))
    else:
        base = Path(__file__).resolve().parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def _write_crash_log(exc: BaseException) -> Path | None:
    """把启动失败的原因写到 exe 旁边。

    窗口程序没有控制台，异常一旦跑出去就是「双击没反应、什么都没有」，
    用户完全无从下手。所以无论如何都要在磁盘上留一份能带走的东西。
    """
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for base in (_app_dir(), Path.cwd()):
        try:
            target = base / "启动失败.txt"
            target.write_text(
                "发票查验 —— 启动失败\n"
                "=" * 50 + "\n"
                f"程序位置：{_app_dir()}\n"
                f"当前目录：{Path.cwd()}\n"
                f"Python：{sys.version}\n"
                "=" * 50 + "\n\n" + detail,
                encoding="utf-8",
            )
            return target
        except OSError:
            continue
    return None


def _report_fatal(exc: BaseException, crash_log: Path | None) -> None:
    """弹窗告知用户，并指出崩溃记录在哪。"""
    detail = f"{type(exc).__name__}: {exc}"
    if crash_log is not None:
        detail += f"\n\n详细信息已写入：\n{crash_log}"
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("发票查验 —— 启动失败", detail)
        root.destroy()
    except Exception:
        # 连弹窗都起不来（比如 Tk 本身初始化失败），至少写到 stderr
        try:
            print(f"启动失败：{detail}", file=sys.stderr)
        except Exception:
            pass


def main() -> int:
    _bootstrap()
    try:
        # 先把日志挂到默认位置：这样后面任何一步出问题都留得下记录，
        # 而不是等到配置读完了才有日志。
        from app.config import app_dir
        from app.logsetup import setup_logging

        setup_logging(app_dir() / "发票查验日志.txt")

        from app.gui import main as gui_main

        return gui_main()
    except Exception as exc:
        # 必须包住 gui_main() —— 只包 import 的话，启动期异常会静默退出
        crash_log = _write_crash_log(exc)
        try:
            import logging

            logging.getLogger("startup").exception("启动失败")
        except Exception:
            pass
        _report_fatal(exc, crash_log)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
