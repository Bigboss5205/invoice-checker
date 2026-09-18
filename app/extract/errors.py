"""抽取过程中的异常类型。"""

from __future__ import annotations


class ExtractionError(Exception):
    """文件能打开，但没能抽出可用字段。"""


class ExtractionUnavailable(ExtractionError):
    """缺少必要的解析能力（比如没装 OCR、没装 PDF 渲染库）。

    这类错误应该提示用户「补依赖」或「转人工录入」，而不是当成文件损坏。
    """


class UnsupportedFormat(ExtractionError):
    """不在支持列表里的扩展名。"""
