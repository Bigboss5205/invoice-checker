"""图片发票 OCR。

两个引擎，按能力从高到低尝试：

1. **PaddleOCR**（可选，镜像构建时 ``WITH_PADDLEOCR=true``）——
   中文全票面识别效果可用，是图片发票的正经方案。
2. **ddddocr**（默认装了，但它是为「4 位验证码」训练的）——
   对整张发票只能勉强认出一些字符，**不要对它期待太高**。
   它的价值在于：装不上 PaddleOCR 时，至少还能试一把。

两个都没有 / 都认不出来时，抛 :class:`ExtractionUnavailable`，
上层会把这张票转成「人工录入」，而不是当成失败丢掉。
"""

from __future__ import annotations

import io
import logging
import threading
from typing import Callable

from .errors import ExtractionUnavailable

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: tuple[str, Callable[[bytes], str]] | None = None
_probed = False


# --------------------------------------------------------------------------
#  PaddleOCR
# --------------------------------------------------------------------------

def _build_paddle() -> Callable[[bytes], str]:
    from paddleocr import PaddleOCR  # type: ignore

    # 只初始化一次，模型加载很慢
    engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    def run(png: bytes) -> str:
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(png)).convert("RGB")
        result = engine.ocr(np.array(img), cls=True)

        lines: list[str] = []
        for page in result or []:
            for item in page or []:
                try:
                    box, (text, _conf) = item[0], item[1]
                    ys = [pt[1] for pt in box]
                    xs = [pt[0] for pt in box]
                    lines.append((min(ys), min(xs), text))
                except (IndexError, TypeError, ValueError):
                    continue

        # 按「从上到下、从左到右」排，尽量还原阅读顺序
        lines.sort(key=lambda t: (round(t[0] / 12), t[1]))
        return "\n".join(t[2] for t in lines)

    return run


# --------------------------------------------------------------------------
#  ddddocr（弱兜底）
# --------------------------------------------------------------------------

def _build_ddddocr() -> Callable[[bytes], str]:
    import ddddocr  # type: ignore

    det = ddddocr.DdddOcr(det=True, show_ad=False)
    rec = ddddocr.DdddOcr(show_ad=False)

    def run(png: bytes) -> str:
        from PIL import Image

        boxes = det.detection(png)
        if not boxes:
            return ""

        img = Image.open(io.BytesIO(png)).convert("RGB")
        items: list[tuple[float, float, str]] = []

        for box in boxes:
            try:
                x1, y1, x2, y2 = (int(v) for v in box[:4])
            except (TypeError, ValueError):
                continue
            if x2 <= x1 or y2 <= y1:
                continue
            crop = img.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            try:
                text = rec.classification(buf.getvalue()) or ""
            except Exception:  # 单块失败不影响整体
                text = ""
            if text.strip():
                items.append((y1, x1, text.strip()))

        items.sort(key=lambda t: (round(t[0] / 14), t[1]))
        return "\n".join(t[2] for t in items)

    return run


# --------------------------------------------------------------------------
#  对外接口
# --------------------------------------------------------------------------

def get_engine(force_probe: bool = False) -> tuple[str, Callable[[bytes], str]] | None:
    """返回 ``(引擎名, ocr函数)``；一个都装不上时返回 None。结果会缓存。"""
    global _cached, _probed

    with _lock:
        if _cached is not None and not force_probe:
            return _cached
        if _probed and _cached is None and not force_probe:
            return None

        for name, builder in (("paddleocr", _build_paddle), ("ddddocr", _build_ddddocr)):
            try:
                func = builder()
            except ImportError:
                log.debug("OCR 引擎 %s 未安装", name)
                continue
            except Exception as exc:
                log.warning("OCR 引擎 %s 初始化失败：%s", name, exc)
                continue
            log.info("图片 OCR 引擎就绪：%s", name)
            _cached = (name, func)
            _probed = True
            return _cached

        log.warning("没有任何可用的图片 OCR 引擎，图片发票将转人工录入")
        _probed = True
        return None


def available() -> bool:
    return get_engine() is not None


def ocr_image(png: bytes) -> str:
    """对单张图片做 OCR。引擎不可用时抛 ExtractionUnavailable。"""
    engine = get_engine()
    if engine is None:
        raise ExtractionUnavailable(
            "没有可用的图片 OCR 引擎（PaddleOCR 未安装，ddddocr 也不可用）。"
            "请在 Web 界面手工录入该发票的字段。"
        )
    name, func = engine
    try:
        text = func(png)
    except ExtractionUnavailable:
        raise
    except Exception as exc:
        raise ExtractionUnavailable(f"OCR 引擎 {name} 执行失败：{exc}") from exc
    return text or ""


def extract(path_bytes: bytes) -> tuple[str, str]:
    """图片发票入口，返回 ``(文本, 来源说明)``。"""
    name, _ = get_engine() or ("none", None)  # type: ignore[misc]
    text = ocr_image(path_bytes)
    if not text.strip():
        raise ExtractionUnavailable("OCR 没有从图片中识别出任何文字")
    return text, f"image:{name}"
