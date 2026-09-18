"""查验驱动的公共契约（同步版）。

桌面版用同步 API：Tkinter 主线程 + 一个工作线程，没有 asyncio 事件循环，
同步写法更直观，也和 Playwright 的 sync API 天然契合。

驱动只负责「给入参 → 返回结论 + 结果 PDF」，不碰文件、不碰界面。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

# 结论状态；键与 config 里 platform.result_labels 一一对应
STATUSES = (
    "ok",             # 查验一致
    "mismatch",       # 信息不一致
    "not_found",      # 查无此票
    "captcha_wrong",  # 验证码没通过
    "rate_limited",   # 触发平台限流
    "unknown",        # 页面拿到了但认不出结论
    "error",          # 系统/网络层出错，可重试
    "skipped",        # 未查验
)

TERMINAL_STATUSES = frozenset({"ok", "mismatch", "not_found", "skipped"})
RETRYABLE_STATUSES = frozenset({"error", "rate_limited", "unknown", "captcha_wrong"})


class Prompter(Protocol):
    """人工验证码输入接口。GUI 实现它，驱动只认这个方法。"""

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0) -> str | None:
        ...


@dataclass
class VerifyOutcome:
    status: str
    summary: str = ""
    detail: str = ""
    raw_text: str = ""
    captcha_source: str = ""          # ocr | manual | ""
    captcha_attempts: int = 0
    pdf_bytes: bytes | None = None    # 查验结果页导出的 PDF
    screenshot: bytes | None = None   # 出错时的截图，用于排查
    page_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"


def classify(text: str, cfg, baseline: str = "") -> str:
    """按平台文案判定结论。

    `baseline` 是提交前的页面文本：只有「提交后才出现的」关键词才算数，
    否则帮助文字里自带的「查验成功」会造成误判。
    """
    if not text:
        return "unknown"

    for status in ("captcha_wrong", "rate_limited", "not_found", "mismatch", "ok"):
        for kw in (cfg.get(f"platform.result_keywords.{status}", []) or []):
            if kw and kw in text and (not baseline or kw not in baseline):
                return status
    return "unknown"


def label_for(status: str, cfg) -> str:
    labels = cfg.section("platform.result_labels")
    return str(labels.get(status) or labels.get("unknown") or status)


class BaseVerifier(ABC):
    name = "base"

    def __init__(self, cfg, prompter: Prompter | None = None):
        self.cfg = cfg
        self.prompter = prompter
        self.manual_timeout = float(cfg.get("captcha.manual_timeout_seconds", 180))

    def start(self) -> None:
        """启动驱动（比如拉起浏览器）。默认什么都不做。"""

    def close(self) -> None:
        """释放资源。必须幂等，且不允许往外抛异常。"""

    def set_debug_dir(self, path) -> None:
        """可选：出错时把截图/HTML 落到哪个目录。"""

    @abstractmethod
    def verify(self, task_id: str, inputs: dict[str, Any], *, filename: str = "",
               invoice_hint: str = "", want_pdf: bool = False) -> VerifyOutcome:
        """查一张票。实现里不要往外抛异常——出问题请返回 status='error'。"""
