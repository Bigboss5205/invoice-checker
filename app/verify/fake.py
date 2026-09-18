"""离线假驱动：不联网、不开浏览器，把整条流程跑通。

用途：
* 第一次运行程序时确认「扫描 → 抽字段 → 出结果 → 存 -已查验.pdf」整条链路正常；
* 改代码后的回归验证，且**不消耗平台的查验次数**；
* 演示给别人看。

它会：
1. 按入参哈希稳定地给出一个结论（同一张票每次一样）；
2. 生成一个**真实合法的 PDF**，这样「保存 -已查验.pdf」这条路径也被真正测到；
3. 可选地走一遍人工验证码输入，方便测试弹窗。
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from .base import BaseVerifier, VerifyOutcome

log = logging.getLogger(__name__)

_OUTCOMES = ["ok", "ok", "ok", "ok", "ok", "ok", "mismatch", "not_found", "unknown"]

_SUMMARIES = {
    "ok": "【模拟】查验成功，发票信息一致",
    "mismatch": "【模拟】发票信息不一致",
    "not_found": "【模拟】查无此票",
    "unknown": "【模拟】未能识别平台返回内容",
}


class FakeVerifier(BaseVerifier):
    name = "fake"

    def __init__(self, cfg, prompter=None):
        super().__init__(cfg, prompter)
        self.force_manual = bool(cfg.get("verify.fake_force_manual", False))
        self.delay = float(cfg.get("verify.fake_delay_seconds", 0.6))

    def verify(self, task_id: str, inputs: dict[str, Any], *, filename: str = "",
               invoice_hint: str = "", want_pdf: bool = False) -> VerifyOutcome:
        key = "|".join(str(inputs.get(k) or "") for k in
                       ("invoice_code", "invoice_number", "invoice_date",
                        "check_code_last6", "amount"))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        status = _OUTCOMES[int(digest[:8], 16) % len(_OUTCOMES)]

        captcha_source, attempts = "ocr", 1

        if self.force_manual and self.prompter is not None:
            png = _placeholder_png()
            text = self.prompter.request(task_id, png, filename=filename,
                                         hint=invoice_hint, timeout=self.manual_timeout)
            captcha_source, attempts = "manual", 2
            if text is None:
                return VerifyOutcome(status="error",
                                     summary="【模拟】等待人工验证码超时",
                                     captcha_source="manual", captcha_attempts=attempts)

        time.sleep(self.delay)  # 模拟网络耗时，让进度条能被看见

        summary = _SUMMARIES.get(status, status)
        detail = (
            f"【模拟查验结果】\n"
            f"发票代码：{inputs.get('invoice_code') or '(数电票无)'}\n"
            f"发票号码：{inputs.get('invoice_number')}\n"
            f"开票日期：{inputs.get('invoice_date')}\n"
            f"校验码后6位：{inputs.get('check_code_last6') or '(数电票无)'}\n"
            f"金额：{inputs.get('amount')}\n"
            f"结论：{summary}\n"
            f"（本内容由离线假驱动生成，不来自任何真实平台）"
        )

        pdf = None
        if want_pdf:
            pdf = build_pdf([
                "INVOICE VERIFICATION RESULT (SIMULATED)",
                "",
                f"File          : {_ascii(filename)}",
                f"Invoice No.   : {inputs.get('invoice_number')}",
                f"Invoice Code  : {inputs.get('invoice_code') or 'N/A'}",
                f"Issue Date    : {inputs.get('invoice_date')}",
                f"Check Code L6 : {inputs.get('check_code_last6') or 'N/A'}",
                f"Amount        : {inputs.get('amount')}",
                "",
                f"Result        : {status.upper()}",
                "",
                "This PDF was produced by the offline fake driver.",
                "It is NOT a real tax bureau verification record.",
            ])

        return VerifyOutcome(
            status=status, summary=summary, detail=detail, raw_text=detail,
            captcha_source=captcha_source, captcha_attempts=attempts,
            pdf_bytes=pdf, extra={"fake": True, "digest": digest[:12]},
        )


def _ascii(text: str) -> str:
    """手写 PDF 用的是 Helvetica，装不下中文，转成可读的 ASCII 占位。"""
    return "".join(ch if ch.isascii() else "?" for ch in str(text or ""))


def build_pdf(lines: list[str]) -> bytes:
    """生成一个最小但**合法**的单页 PDF。

    故意手写而不是引入 reportlab：桌面版每多一个依赖，打包就多一份麻烦，
    而这里只需要「是个能被阅读器打开的 PDF」就够了。
    """
    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    parts = ["BT", "/F1 11 Tf", "50 790 Td", "16 TL"]
    for idx, line in enumerate(lines):
        if idx:
            parts.append("T*")
        parts.append(f"({esc(_ascii(line))}) Tj")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1", "replace")

    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
         b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for num, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


def _placeholder_png() -> bytes:
    """一张 1x1 占位 PNG，让「人工验证码」弹窗有东西可显示。"""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
