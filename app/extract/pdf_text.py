"""PDF 发票文本抽取。

策略（两级）
-----------
1. 先用 pdfplumber 抽文本层。绝大多数从税局/开票软件下载的电子发票都有文本层。
2. 如果抽出来是空的、或者是乱码（字体子集没有 ToUnicode 映射），
   用 pypdfium2 渲染成图片，交给 OCR。

`looks_usable` 是这里的守门人：它决定「文本层能不能用」。
判据是发票票面上必然出现的几个中文关键词——没有这些词，
基本可以断定文本层是坏的（哪怕抽出来一堆字符）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .errors import ExtractionError, ExtractionUnavailable

log = logging.getLogger(__name__)

# 票面关键词：随便哪张增值税发票都会命中至少一个
_KEYWORDS = ("发票代码", "发票号码", "开票日期", "校验码", "增值税", "价税合计", "机器编号")


def looks_usable(text: str) -> bool:
    """文本层是否可信。"""
    if not text or len(text.strip()) < 20:
        return False
    hits = sum(1 for kw in _KEYWORDS if kw in text)
    if hits >= 1:
        return True
    # 一个关键词都没命中：看看是不是「一堆 CJK 但没有票面词」的乱码
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk > 40 and hits == 0:
        # 有大量中文却没有票面关键词，很可能是坏字体，交给 OCR 更稳
        return False
    # 纯 ASCII 但够长，可能是 XML/文本型 PDF，允许继续
    return len(text.strip()) >= 60


def extract_text_layer(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise ExtractionUnavailable("未安装 pdfplumber，无法解析 PDF") from exc

    chunks: list[str] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception as exc:  # 单页失败不影响整份
                    log.debug("pdfplumber 单页抽取失败 %s: %s", path.name, exc)
    except Exception as exc:
        raise ExtractionError(f"PDF 打开失败：{exc}") from exc

    return "\n".join(c for c in chunks if c)


def render_pages(path: Path, dpi: int = 220) -> list[bytes]:
    """把 PDF 每页渲染成 PNG 字节。

    用 pypdfium2 而不是 pdf2image：后者要调用外部 poppler 程序，
    Windows 上得让用户自己下载解压配置 PATH——对「双击就能用」是致命的。
    pypdfium2 自带 PDFium 动态库，pip 装完即可，没有任何外部依赖。
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ExtractionUnavailable(
            "未安装 pypdfium2，无法把扫描版 PDF 渲染成图片"
        ) from exc

    import io

    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise ExtractionError(f"PDF 打开失败：{exc}") from exc

    out: list[bytes] = []
    try:
        scale = max(0.5, dpi / 72.0)
        for index in range(len(document)):
            try:
                bitmap = document[index].render(scale=scale)
                image = bitmap.to_pil()
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                out.append(buf.getvalue())
            except Exception as exc:
                # 单页失败不影响其他页
                log.warning("PDF 第 %d 页渲染失败 %s: %s", index + 1, path.name, exc)
    finally:
        try:
            document.close()
        except Exception:
            pass

    return out


def extract(path: Path, *, render_dpi: int = 220, ocr_func=None) -> tuple[str, str]:
    """抽取 PDF 文本。

    返回 ``(文本, 来源说明)``。`ocr_func` 是 ``(bytes) -> str`` 的 OCR 回调，
    由上层注入，避免这个模块直接依赖 OCR 实现。
    """
    text = extract_text_layer(path)

    if looks_usable(text):
        return text, "pdf:text-layer"

    # 文本层不可信 → 渲染 + OCR
    if ocr_func is None:
        raise ExtractionError(
            "PDF 没有可用文本层（可能是扫描件或字体子集损坏），"
            "且 OCR 未启用"
        )

    images = render_pages(path, dpi=render_dpi)
    if not images:
        raise ExtractionError("PDF 渲染后没有得到任何页面")

    ocr_chunks: list[str] = []
    for idx, png in enumerate(images, 1):
        try:
            got = ocr_func(png)
            if got:
                ocr_chunks.append(got)
        except ExtractionUnavailable:
            raise
        except Exception as exc:
            log.warning("PDF 第 %d 页 OCR 失败 %s: %s", idx, path.name, exc)

    ocr_text = "\n".join(ocr_chunks)
    if not ocr_text.strip():
        raise ExtractionError("PDF 文本层不可用，OCR 也没有识别出内容")

    # OCR 结果优先；但要是不够，把文本层残渣接在后面当补充
    if looks_usable(ocr_text):
        return ocr_text, "pdf:ocr"
    return f"{ocr_text}\n{text}", "pdf:ocr+text-layer-partial"
