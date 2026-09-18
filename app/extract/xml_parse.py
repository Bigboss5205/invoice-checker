"""XML 发票抽取（数电票 / 全电发票的 XML 原件）。

数电票 XML 没有统一到「一个标准 schema」的程度，不同开票渠道的标签名不一样：
有的是 ``InvoiceNumber``，有的是中文标签，有的带命名空间。
所以这里做两件事：

1. 按「标签别名表」找结构化字段；
2. 无论找没找到，都把整个 XML 展平成文本，用通用正则再兜一遍。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from .errors import ExtractionError

log = logging.getLogger(__name__)

_MAX_BYTES = 60 * 1024 * 1024

# 标签别名（全部转小写比较）
_ALIASES: dict[str, set[str]] = {
    "invoice_code": {"invoicecode", "fpdm", "发票代码"},
    "invoice_number": {"invoicenumber", "fphm", "发票号码", "invoiceno"},
    "invoice_date": {
        "issuetime", "invoicedate", "kprq", "开票日期", "issuingtime", "billtime",
    },
    "check_code": {"checkcode", "kjh", "校验码", "checkcodeno"},
    "amount_total": {
        "totaltaxincludedamount", "totalamountwithtax", "价税合计",
        "totalincludingtax", "amountwithtax", "totaltax-includedamount",
    },
    "amount_excl_tax": {
        "totalamwithouttax", "totalamountwithouttax", "合计金额", "amountwithouttax",
        "totalexcltax", "totalamount",
    },
    "seller_name": {"sellername", "销方名称", "销售方名称"},
    "buyer_name": {"buyername", "购方名称", "购买方名称"},
}

_ALIAS_LOOKUP: dict[str, str] = {}
for _field, _names in _ALIASES.items():
    for _n in _names:
        _ALIAS_LOOKUP[_n] = _field


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _safe_parse(raw: bytes) -> ET.Element:
    """优先用 defusedxml 防 XXE / 实体爆炸；没有就退回标准库。"""
    try:
        from defusedxml.ElementTree import fromstring as safe_fromstring

        return safe_fromstring(raw)
    except ImportError:
        log.debug("未安装 defusedxml，使用标准库解析 XML")
        return ET.fromstring(raw)


def _flatten(root: ET.Element) -> str:
    """把 XML 展平成 ``标签: 值`` 的文本，交给通用正则处理。"""
    lines: list[str] = []

    def visit(elem: ET.Element) -> None:
        name = _localname(elem.tag)
        text = (elem.text or "").strip()
        if text:
            lines.append(f"{name}: {text}")
        for child in elem:
            visit(child)

    visit(root)
    return "\n".join(lines)


def extract(path: Path) -> tuple[str, str]:
    """返回 ``(文本, 来源说明)``。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractionError(f"XML 文件不可读：{exc}") from exc

    if size > _MAX_BYTES:
        raise ExtractionError(f"XML 文件过大（{size} bytes），已拒绝解析")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtractionError(f"XML 读取失败：{exc}") from exc

    # 编码：数电票 XML 可能是 UTF-8 带 BOM，也可能是 GBK
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    try:
        root = _safe_parse(raw)
    except ET.ParseError as exc:
        # 不是 XML，交给通用文本抽取兜底
        raise ExtractionError(f"XML 解析失败：{exc}") from exc
    except Exception as exc:  # defusedxml 会为恶意实体抛异常
        raise ExtractionError(f"XML 解析被拒绝（可能是恶意实体）：{exc}") from exc

    flat = _flatten(root)
    structured_hits = 0

    # 结构化命中用来判断「这份 XML 到底是不是发票」
    for elem in root.iter():
        name = _localname(elem.tag).lower()
        if name in _ALIAS_LOOKUP and (elem.text or "").strip():
            structured_hits += 1

    if not flat.strip():
        raise ExtractionError("XML 里没有任何文本内容")

    source = "xml:structured" if structured_hits >= 2 else "xml:flat"
    if structured_hits < 2:
        log.debug("XML 结构化字段命中 %d 个，改用展平文本：%s", structured_hits, path.name)

    return flat, source


def structured_fields(path: Path) -> dict[str, str]:
    """直接按标签取字段（结构化优先，比正则更准）。

    只在能明确命中时返回，命中不到就不放进结果，让正则去兜。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    root = _safe_parse(raw)

    found: dict[str, str] = {}
    for elem in root.iter():
        field = _ALIAS_LOOKUP.get(_localname(elem.tag).lower())
        if not field or field in found:
            continue
        text = re.sub(r"\s+", "", (elem.text or "").strip())
        if text:
            found[field] = text
    return found
