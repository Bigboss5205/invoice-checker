"""发票文件 → 字段。对外只暴露 :func:`extract_invoice`。

支持：``.pdf`` ``.ofd`` ``.xml`` 以及 ``.png/.jpg/.jpeg/.bmp/.webp/.tif/.tiff``。

每个格式的抽取器只负责「拿到文本」，字段解析统一交给 :mod:`.fields`，
这样格式支持和字段规则互不干扰。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fields as F
from . import image_ocr, ofd_text, pdf_text, xml_parse
from .errors import (  # noqa: F401  (对外转出，方便上层统一 catch)
    ExtractionError,
    ExtractionUnavailable,
    UnsupportedFormat,
)

log = logging.getLogger(__name__)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
OFD_EXTS = {".ofd"}
XML_EXTS = {".xml"}

SUPPORTED_EXTS = PDF_EXTS | OFD_EXTS | XML_EXTS | IMAGE_EXTS


@dataclass
class ExtractionResult:
    """一次抽取的完整结果。"""

    path: Path
    fields: dict[str, Any]
    source: str                      # 例如 "pdf:text-layer"
    text: str = ""                   # 抽取到的原始文本（供排查）
    warnings: list[str] = field(default_factory=list)
    used_structured_xml: bool = False

    @property
    def missing(self) -> list[str]:
        return F.missing_required(self.fields)

    @property
    def ok(self) -> bool:
        return not self.missing


def _ocr_func(cfg) -> Any:
    """给 PDF 渲染后的兜底 OCR 用。"""
    if not cfg.get("extract.image_ocr", True):
        return None
    _, func = image_ocr.get_engine() or (None, None)
    return func


def extract_invoice(path: Path, cfg) -> ExtractionResult:
    """抽取一张发票的字段。

    失败时抛 :class:`ExtractionError` 及其子类；上层据此决定
    「重试 / 转人工录入 / 标记失败」。
    """
    path = Path(path)
    ext = path.suffix.lower()

    if ext not in SUPPORTED_EXTS:
        raise UnsupportedFormat(f"不支持的格式：{ext or '(无扩展名)'}")

    densify = bool(cfg.get("extract.densify_digits", True))
    render_dpi = int(cfg.get("extract.render_dpi", 220))

    warnings: list[str] = []
    structured: dict[str, str] = {}
    used_structured = False

    # ---- 按格式取文本 ----
    if ext in PDF_EXTS:
        text, source = pdf_text.extract(
            path, render_dpi=render_dpi, ocr_func=_ocr_func(cfg)
        )

    elif ext in OFD_EXTS:
        text, source = ofd_text.extract(path)

    elif ext in XML_EXTS:
        text, source = xml_parse.extract(path)
        try:
            structured = xml_parse.structured_fields(path)
            used_structured = bool(structured)
        except Exception as exc:
            warnings.append(f"XML 结构化字段读取失败，改用文本解析：{exc}")
            log.debug("structured_fields 失败 %s: %s", path.name, exc)

    else:  # 图片
        if not cfg.get("extract.image_ocr", True):
            raise ExtractionUnavailable(
                "图片发票需要 OCR，但 extract.image_ocr 已关闭。请手工录入字段。"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ExtractionError(f"图片读取失败：{exc}") from exc
        text, source = image_ocr.extract(raw)

    from_ocr = "ocr" in source

    # ---- 解析字段 ----
    parsed = F.parse_fields(text, densify_digits=densify, from_ocr=from_ocr)

    # ---- XML 结构化结果覆盖正则结果（更权威）----
    if used_structured:
        for key, value in structured.items():
            if key == "invoice_date":
                norm = _normalize_date(value)
                if norm:
                    parsed["invoice_date"] = norm
                    parsed["_sources"]["invoice_date"] = "xml:structured"
                continue

            if key == "check_code":
                digits = "".join(ch for ch in value if ch.isdigit())
                if len(digits) == 20:
                    parsed["check_code"] = digits
                    parsed["check_code_last6"] = digits[-6:]
                    parsed["_sources"]["check_code"] = "xml:structured"
                elif len(digits) >= 6:
                    parsed["check_code_last6"] = digits[-6:]
                    parsed["_sources"]["check_code_last6"] = "xml:structured"
                continue

            if key in {"amount_total", "amount_excl_tax"}:
                amt = _normalize_amount(value)
                if amt:
                    parsed[key] = amt
                    parsed["_sources"][key] = "xml:structured"
                continue

            if key in {"invoice_code", "invoice_number"}:
                digits = "".join(ch for ch in value if ch.isdigit())
                if digits:
                    parsed[key] = digits
                    parsed["_sources"][key] = "xml:structured"
                continue

            if key in {"seller_name", "buyer_name"} and value:
                parsed[key] = value
                parsed["_sources"][key] = "xml:structured"

        num = parsed.get("invoice_number")
        parsed["invoice_kind"] = (
            "fully_digital" if num and len(str(num)) == 20
            else ("legacy" if num else None)
        )

    # ---- 合理性检查 ----
    warnings.extend(F.sanity_check(parsed, parsed.get('_raw_text')))
    if not parsed.get("invoice_number"):
        warnings.append("没有识别出发票号码")

    return ExtractionResult(
        path=path,
        fields=parsed,
        source=source,
        text=parsed.get("_raw_text", ""),
        warnings=warnings,
        used_structured_xml=used_structured,
    )


def _normalize_date(value: str) -> str | None:
    """把各种日期写法归一成 YYYY-MM-DD。"""
    import re

    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        y, m, d = digits[:4], digits[4:6], digits[6:8]
        try:
            if 1994 <= int(y) <= 2100 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
                return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            return None
    return None


def _normalize_amount(value: str) -> str | None:
    import re

    m = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
    if not m:
        return None
    try:
        return f"{float(m.group(0)):.2f}"
    except ValueError:
        return None
