"""OFD 发票文本抽取。

OFD 本质是个 ZIP 包（国标版式文档），发票的文字存在
``Doc_0/Pages/Page_N/Content.xml`` 里的 ``TextCode`` 元素上。

这里不做完整版式还原，只按文档顺序把文字拼起来——
对「按标签找值」的字段抽取来说足够了。
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .errors import ExtractionError

log = logging.getLogger(__name__)

# 单个 entry 解压上限，防 zip 炸弹
_MAX_ENTRY_BYTES = 40 * 1024 * 1024
_MAX_TOTAL_BYTES = 200 * 1024 * 1024


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _walk_text(root: ET.Element) -> str:
    """按文档顺序拼出文字。TextObject 之间换行。"""
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = "".join(current).strip()
            if joined:
                lines.append(joined)
            current.clear()

    for elem in root.iter():
        name = _localname(elem.tag)
        if name == "TextCode":
            if elem.text:
                current.append(elem.text)
        elif name == "TextObject":
            flush()

    flush()
    return "\n".join(lines)


def extract(path: Path) -> tuple[str, str]:
    """返回 ``(文本, 来源说明)``。"""
    if not zipfile.is_zipfile(path):
        raise ExtractionError("OFD 文件不是合法的 ZIP 包（可能已损坏或后缀名不对）")

    texts: list[str] = []
    total = 0

    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            # 优先 Pages 下的正文
            pages = [n for n in names if "/Pages/" in n or n.startswith("Pages/")]
            ordered = pages + [n for n in names if n not in pages]

            for name in ordered:
                info = zf.getinfo(name)
                if info.file_size > _MAX_ENTRY_BYTES:
                    log.warning("OFD entry 过大，跳过：%s (%d bytes)", name, info.file_size)
                    continue
                total += info.file_size
                if total > _MAX_TOTAL_BYTES:
                    log.warning("OFD 解压总量超限，停止读取：%s", path.name)
                    break
                try:
                    raw = zf.read(name)
                except (zipfile.BadZipFile, OSError) as exc:
                    log.debug("OFD entry 读取失败 %s: %s", name, exc)
                    continue
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError:
                    continue
                got = _walk_text(root)
                if got:
                    texts.append(got)
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f"OFD 解压失败：{exc}") from exc
    except OSError as exc:
        raise ExtractionError(f"OFD 读取失败：{exc}") from exc

    content = "\n".join(texts).strip()
    if not content:
        raise ExtractionError("OFD 里没有找到文字内容（可能是纯图片版式）")
    return content, "ofd:text-objects"
