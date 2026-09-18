"""验证码自动识别（ddddocr + 颜色分离）。

这个平台的验证码有个特点（2026-09 实测）
----------------------------------------
图片提示是「请输入验证码图片中**蓝色文字**」——一张图里混着几种颜色的字符，
只需要填写**蓝色**的那些。而且字符是**中文与字母混排**的。

这对自动识别意味着两件事：

1. **必须做颜色分离**。ddddocr 只看形状不看颜色，直接喂原图它会把所有颜色的
   字符都读出来，结果基本必错。所以先按颜色把蓝色像素挑出来、转成黑字白底，
   再交给 ddddocr。
2. **成功率有限**。中文+字母混排本来就比纯字母数字难认。所以本模块只当
   「加速路径」，真正保证流程跑完的是「识别失败 → 刷新重试 → 弹窗人工输入」。

单次成功率可能只有两三成，但重试 N 次的整体成功率是 1-(1-p)^N，
配合人工兜底，实际体验是「大多数不用你动手，个别的弹窗输一下」。
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

_lock = threading.Lock()
_engine: Any = None
_probed = False

EXPECTED_LENGTH = 4

_JUNK = set(" \t\n\r=+*_-.,:;'\"`~^<>[]{}()\\/|")

# 颜色分离的判定阈值：蓝通道要比红绿都高出这么多，才算「蓝色文字」
_BLUE_MARGIN = 25
_BLUE_MIN = 90
# 分离后至少要有这么多像素才认为这张图确实是蓝色文字验证码
_MIN_BLUE_PIXELS = 20


def _build() -> Any:
    import ddddocr  # type: ignore

    return ddddocr.DdddOcr(show_ad=False)


def available() -> bool:
    """ddddocr 是否可用（结果缓存，只探测一次）。"""
    global _engine, _probed

    with _lock:
        if _probed:
            return _engine is not None
        _probed = True
        try:
            _engine = _build()
            log.info("验证码自动识别已就绪：ddddocr")
        except ImportError:
            log.warning("未安装 ddddocr，验证码将全部转人工输入")
            _engine = None
        except Exception as exc:
            log.warning("ddddocr 初始化失败，验证码将转人工输入：%s", exc)
            _engine = None
        return _engine is not None


def blue_filter(png: bytes) -> bytes | None:
    """只保留偏蓝的像素，转成黑字白底。

    返回 None 表示「这张图不是蓝色文字形态」或缺少 Pillow——
    调用方据此决定要不要退回到原图识别。
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        img = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception as exc:
        log.debug("验证码图片打开失败：%s", exc)
        return None

    width, height = img.size
    source = img.load()
    out = Image.new("RGB", (width, height), (255, 255, 255))
    target = out.load()

    kept = 0
    for y in range(height):
        for x in range(width):
            r, g, b = source[x, y]
            if b - max(r, g) >= _BLUE_MARGIN and b >= _BLUE_MIN:
                target[x, y] = (0, 0, 0)
                kept += 1

    if kept < _MIN_BLUE_PIXELS:
        log.debug("蓝色像素只有 %d 个，判定不是蓝色文字验证码", kept)
        return None

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    log.debug("颜色分离：保留 %d 个蓝色像素", kept)
    return buf.getvalue()


def _recognize(png: bytes) -> str | None:
    """跑一次 ddddocr，并校验结果可信度。"""
    try:
        with _lock:  # ddddocr 不是线程安全的
            raw = _engine.classification(png)
    except Exception as exc:
        log.debug("验证码识别异常：%s", exc)
        return None

    if not raw:
        return None

    text = "".join(ch for ch in str(raw) if ch not in _JUNK).strip()
    if len(text) != EXPECTED_LENGTH:
        log.debug("识别结果长度不是 %d，丢弃：%r", EXPECTED_LENGTH, raw)
        return None
    return text


def solve(png: bytes) -> str | None:
    """识别一张验证码。认不出或结果不可信时返回 None。

    依次尝试：颜色分离后的图 → 原图。
    先试分离版是因为原图混着多种颜色，直接识别基本必错。
    """
    if not png or not available():
        return None

    attempts = (("颜色分离", blue_filter(png)), ("原图", png))
    for tag, image in attempts:
        if not image:
            continue
        text = _recognize(image)
        if text:
            log.debug("验证码识别成功（%s）：%s", tag, text)
            return text

    return None


def engine_name() -> str:
    return "ddddocr" if available() else "none"
