"""验证码自动识别（ddddocr + 按提示做颜色分离）。

这个平台的验证码有个特点（2026-09 实测）
----------------------------------------
图片旁边会写一句提示，形如：

    请输入验证码图片中**蓝色**文字
    请输入验证码图片中**红色**文字      ← 实测颜色会在蓝/红之间切换

也就是说一张图里混着几种颜色的字符，**只填指定颜色的那些**。
而且字符是**中文与字母混排**的（实测见过「村朋FQ」）。

这对自动识别意味着两件事：

1. **必须先读提示、再按颜色分离**。写死一种颜色，另一类验证码就会全认错。
2. **成功率有限**。中文+字母混排本来就比纯字母数字难认。所以本模块只当
   「加速路径」——真正保证流程跑完的是「识别失败 → 刷新重试 → 弹窗人工输入」。
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

_lock = threading.Lock()
_engine: Any = None
_probed = False

# 验证码长度**不固定**，不能写死 4 位。
#
# 平台的提示是「请输入验证码图片中蓝色文字」——图里字符总数可能多于要填的数量，
# 到底几位取决于其中有几个是蓝色的。所以这里只做一个宽松的合理性区间：
# 太短（1 个字符）基本是没认出来，太长则多半是把干扰线也读进来了。
MIN_LENGTH = 2
MAX_LENGTH = 8

_JUNK = set(" \t\n\r=+*_-.,:;'\"`~^<>[]{}()\\/|")

# 分离后至少要留下这么多像素，才认为「这张图确实是该颜色文字」
_MIN_PIXELS = 20

# 颜色判定：目标通道要比其他通道明显高
_MARGIN = 25
_MIN_LEVEL = 90


def _is_blue(r: int, g: int, b: int) -> bool:
    return b - max(r, g) >= _MARGIN and b >= _MIN_LEVEL


def _is_red(r: int, g: int, b: int) -> bool:
    return r - max(g, b) >= _MARGIN and r >= _MIN_LEVEL


def _is_green(r: int, g: int, b: int) -> bool:
    return g - max(r, b) >= 20 and g >= 80


def _is_black(r: int, g: int, b: int) -> bool:
    return max(r, g, b) < 100


_RULES: dict[str, Callable[[int, int, int], bool]] = {
    "blue": _is_blue,
    "red": _is_red,
    "green": _is_green,
    "black": _is_black,
}

# 提示里的中文颜色词 → 内部名字
_COLOR_ALIASES: tuple[tuple[str, str], ...] = (
    ("蓝色", "blue"), ("蓝", "blue"),
    ("红色", "red"), ("红", "red"),
    ("绿色", "green"), ("绿", "green"),
    ("黑色", "black"), ("黑", "black"),
)


def parse_color_hint(text: str) -> str | None:
    """从提示文字里解析出要填哪种颜色。认不出返回 None。"""
    if not text:
        return None
    for zh, name in _COLOR_ALIASES:
        if zh in text:
            return name
    return None


# 内部名字 → 中文，给界面提示用
_COLOR_ZH: dict[str, str] = {
    "blue": "蓝色",
    "red": "红色",
    "green": "绿色",
    "black": "黑色",
}


def color_label(color: str | None) -> str:
    """把内部颜色名转成中文（用于弹窗文案）。认不出就原样返回。"""
    if not color:
        return ""
    return _COLOR_ZH.get(str(color), str(color))


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


def color_filter(png: bytes, color: str) -> bytes | None:
    """只保留指定颜色的像素，转成黑字白底，便于 OCR。

    返回 None 表示「这张图里几乎没有该颜色的像素」或缺少 Pillow——
    调用方据此决定要不要退回原图。
    """
    rule = _RULES.get(color)
    if rule is None:
        return None

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
            if rule(r, g, b):
                target[x, y] = (0, 0, 0)
                kept += 1

    if kept < _MIN_PIXELS:
        log.debug("%s 色像素只有 %d 个，判定这张图不是该颜色验证码", color, kept)
        return None

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    log.debug("颜色分离（%s）：留下 %d 个像素", color, kept)
    return buf.getvalue()


def blue_filter(png: bytes) -> bytes | None:
    """保留向后兼容：等价于按蓝色分离。"""
    return color_filter(png, "blue")


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
    if not (MIN_LENGTH <= len(text) <= MAX_LENGTH):
        log.debug("识别结果长度 %d 不在 %d-%d 之间，丢弃：%r",
                  len(text), MIN_LENGTH, MAX_LENGTH, raw)
        return None
    return text


def solve(png: bytes, color: str | None = None) -> str | None:
    """识别一张验证码。认不出或结果不可信时返回 None。

    `color` 是页面提示里要求的颜色（blue/red/green/black）。
    先试按该颜色分离后的图，再退回原图——
    原图混着几种颜色，直接识别基本必错，所以分离版优先。

    没给颜色时，把常见颜色都试一遍。
    """
    if not png or not available():
        return None

    colors = [color] if color else ["blue", "red", "green"]
    attempts: list[tuple[str, bytes | None]] = []
    for name in colors:
        if name:
            attempts.append((f"{name}色分离", color_filter(png, name)))
    attempts.append(("原图", png))

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
