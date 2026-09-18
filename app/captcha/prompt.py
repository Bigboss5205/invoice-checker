"""人工验证码输入的几种「提问者」实现。

驱动通过统一的 ``request()`` 接口要一个验证码，具体谁来回答由这里决定：

* :class:`NullPrompter`    —— 没人可问，直接放弃（自动识别失败就跳过这张）
* :class:`ConsolePrompter` —— 命令行里问，用于脚本化测试
* :class:`CallbackPrompter`—— 把问题交给外部回调（GUI 用它，弹出输入框）

GUI 那份实现的关键约束：`request()` 是在**工作线程**里被调用的，
而弹窗必须由 Tk 主线程创建。所以回调内部要做的是
「投递请求 → 阻塞等工作线程可见的 Event → 返回结果」，而不是直接建窗口。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from . import ocr as captcha_ocr

log = logging.getLogger(__name__)


def captcha_hint_text(color: str | None, color_text: str = "") -> str:
    """把「该填哪种颜色」拼成一句给人看的话。

    平台的提示是「请输入验证码图片中蓝色文字」——**颜色会变**（蓝/红都出现过），
    而且图里混着好几种颜色的字符，只该填指定颜色的那些。
    所以这句提示必须显示给用户，不能只写进 debug 日志。
    """
    label = captcha_ocr.color_label(color)
    if label:
        return f"这张图里有多种颜色，只填【{label}】的字符。"
    if color_text:
        return f"平台提示：{color_text}（程序没能读出颜色，请照这句提示填）"
    return "这张图里有多种颜色，只填平台指定颜色的字符（拿不准就点「跳过 / 换一张」）。"


class NullPrompter:
    """不提供人工输入。自动识别失败即放弃。"""

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0,
                color: str | None = None, color_text: str = "") -> str | None:
        log.info("任务 %s 需要人工验证码，但当前未启用人工输入", task_id)
        return None


class ConsolePrompter:
    """命令行提问。图片会存到临时文件并打印路径，供脚本/调试使用。"""

    def __init__(self, save_dir=None):
        self.save_dir = save_dir

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0,
                color: str | None = None, color_text: str = "") -> str | None:
        path = ""
        if self.save_dir is not None and png:
            try:
                from pathlib import Path

                target = Path(self.save_dir) / f"captcha_{task_id}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png)
                path = str(target)
            except OSError as exc:
                log.debug("验证码图片保存失败：%s", exc)

        print("\n" + "=" * 56)
        print(f"需要人工输入验证码 —— {filename or task_id}")
        if hint:
            print(f"发票：{hint}")
        if path:
            print(f"验证码图片：{path}")
        # 颜色提示必须打出来：这张图里混着好几种颜色，只填指定的那种
        print(captcha_hint_text(color, color_text))
        print("=" * 56)
        try:
            text = input("请输入验证码（直接回车 = 跳过）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return text or None


class CallbackPrompter:
    """把请求交给外部回调。

    回调签名：``fn(task_id, png, filename, hint, timeout, color, color_text)
    -> str | None``
    回调内部应负责线程切换（GUI 场景），并保证在 timeout 之前返回或返回 None。
    """

    def __init__(self, fn: Callable[..., str | None]):
        self._fn = fn

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0,
                color: str | None = None, color_text: str = "") -> str | None:
        try:
            return self._fn(task_id=task_id, png=png, filename=filename,
                            hint=hint, timeout=timeout,
                            color=color, color_text=color_text)
        except Exception as exc:
            log.warning("人工验证码回调出错：%s", exc)
            return None


class ThreadEventPrompter:
    """把「工作线程等待」这件事封装好，GUI 侧只需要调两个方法。

    用法（GUI 侧）::

        prompter = ThreadEventPrompter()
        # 工作线程里：
        text = prompter.request(task_id, png, ...)      # 阻塞
        # GUI 线程收到通知后：
        prompter.answer(text)                            # 唤醒
    """

    def __init__(self):
        self._event = threading.Event()
        self._answer: str | None = None
        self._lock = threading.Lock()
        self._pending: dict | None = None
        self.on_ask: Callable[[dict], None] | None = None  # GUI 侧注册

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0,
                color: str | None = None, color_text: str = "") -> str | None:
        with self._lock:
            self._answer = None
            self._event.clear()
            self._pending = {
                "task_id": task_id, "png": png, "filename": filename,
                "hint": hint, "timeout": timeout,
                "color": color, "color_text": color_text,
            }
            payload = dict(self._pending)

        if self.on_ask is None:
            log.info("没有注册验证码提问回调，跳过人工输入")
            return None

        try:
            self.on_ask(payload)
        except Exception as exc:
            log.warning("投递验证码请求失败：%s", exc)
            return None

        if not self._event.wait(timeout + 5):
            log.warning("等待人工验证码超时（%.0f 秒）", timeout)
            return None

        with self._lock:
            return self._answer

    def answer(self, text: str | None) -> None:
        """GUI 线程调用：给出答案（None 表示用户放弃）。"""
        with self._lock:
            self._answer = text or None
        self._event.set()

    @property
    def pending(self) -> dict | None:
        with self._lock:
            return dict(self._pending) if self._pending else None
