"""日志配置：写轮转文件 + 控制台（源码运行时）。

打包成 .pyw 双击运行时没有控制台，日志文件就是唯一的排查依据，
所以出错现场、抽取文本这些都要落盘。

支持「换路径重配」：入口脚本会先按默认位置挂一份日志（保证启动早期
就有记录），随后按 config.yaml 里的 `paths.log` 再挂一次。路径没变
就什么都不做，变了就把旧 handler 摘掉换新的，避免同一条日志写两遍。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# 这些库在 DEBUG 级别会刷屏，压一压
_QUIET = {
    "asyncio": logging.WARNING,
    "playwright": logging.WARNING,
    "urllib3": logging.WARNING,
    "PIL": logging.WARNING,
    "pdfminer": logging.WARNING,
}

_MAX_BYTES = 2_000_000
_BACKUPS = 2


def _detach_own_handlers(root: logging.Logger) -> None:
    """只摘掉本模块挂的 handler，不动别人（比如测试或宿主）挂的。"""
    for handler in list(root.handlers):
        if getattr(handler, "_inv_handler", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def setup_logging(log_path: Path | str | None = None, level: int = logging.INFO,
                  console: bool | None = None) -> Path | None:
    """初始化日志。

    返回实际写入的日志文件路径（失败时为 None）。
    重复调用时：目标路径相同 → 直接返回，不做任何事。
    """
    root = logging.getLogger()
    wanted = Path(log_path) if log_path is not None else \
        getattr(root, "_inv_log_path", None)

    if getattr(root, "_inv_configured", False) and \
            wanted == getattr(root, "_inv_log_path", None):
        return getattr(root, "_inv_log_path", None)

    _detach_own_handlers(root)
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    written: Path | None = None
    if wanted is not None:
        try:
            wanted = Path(wanted)
            wanted.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                wanted, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
            handler.setFormatter(formatter)
            handler._inv_handler = True  # type: ignore[attr-defined]
            root.addHandler(handler)
            written = wanted
        except OSError as exc:
            logging.getLogger(__name__).warning("无法写日志文件 %s：%s", wanted, exc)

    # .pyw 双击运行时 sys.stderr 是 None，不能直接加 StreamHandler
    if console is None:
        console = sys.stderr is not None
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream._inv_handler = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    for name, lvl in _QUIET.items():
        logging.getLogger(name).setLevel(lvl)

    root._inv_configured = True  # type: ignore[attr-defined]
    root._inv_log_path = written  # type: ignore[attr-defined]
    return written
