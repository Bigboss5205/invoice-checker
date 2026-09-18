"""从发票文本里抽字段，并归一化成查验平台需要的入参。

现实中的坑（这个模块主要就是在对付它们）
----------------------------------------
1. **数字之间被排版插了空格**：`发票号码： 1 2 3 4 5 6 7 8`。
   所以先把「数字-空格-数字」压掉（densify），再匹配。
2. **标签和值不在同一行**：PDF 里常被拆成两个文本对象，所以标签后面允许跨行。
3. **两套规则**：
   - 老发票（增值税专票/普票/电子普票）：发票代码 + 发票号码(8位) + 开票日期 + 校验码后6位
   - 数电票/全电发票：发票号码(20位) + 开票日期 + 开具金额(不含税)，**没有**发票代码和校验码
   20 位号码是识别数电票最可靠的标志。
4. **OCR 出来的字符混淆**：O/o→0、I/l→1、S→5 等，只在数字上下文里纠正。
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------
#  文本归一化
# --------------------------------------------------------------------------

_WS = re.compile(r"[ \t\u00a0\u3000]+")


def normalize_text(text: str) -> str:
    """统一换行、全角空格，压缩水平空白。保留换行（标签/值分行的判断依赖它）。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def densify(text: str) -> str:
    """去掉「数字之间的空白」，含跨行。

    `1 2 3 4` → `1234`；`1234\n5678` 这种也会被接上，
    但因为标签始终在值前面，对定位字段没有负面影响。
    """
    if not text:
        return ""
    prev = None
    out = text
    # 反复压，处理 `1 2 3` 这种被单次替换漏掉的
    while prev != out:
        prev = out
        out = re.sub(r"(?<=\d)[ \t\n]+(?=\d)", "", out)
    return out


_OCR_DIGIT_MAP = str.maketrans({
    "O": "0", "o": "0", "D": "0", "Q": "0",
    "I": "1", "l": "1", "i": "1", "|": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "b": "6", "G": "6",
    "B": "8",
    "g": "9", "q": "9",
})


def fix_ocr_digits(token: str) -> str:
    """把 OCR 常见混淆字符纠正成数字。只在纯数字字段上用。"""
    return token.translate(_OCR_DIGIT_MAP)


# --------------------------------------------------------------------------
#  字段正则
# --------------------------------------------------------------------------

# 允许标签和值之间有冒号/空格/换行
_SEP = r"[：: \t\n]*"

RE_INVOICE_CODE = re.compile(rf"发票代码{_SEP}(?<!\d)(\d{{10,12}})(?!\d)")
RE_INVOICE_NUMBER = re.compile(rf"发票号码{_SEP}(?<!\d)(\d{{20}}|\d{{8}})(?!\d)")

_DATE_YMD = r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?"
RE_INVOICE_DATE = re.compile(rf"开票日期{_SEP}{_DATE_YMD}")
# 兜底：任何看起来像日期的位置
RE_ANY_DATE = re.compile(_DATE_YMD)

# 校验码：整串 20 位，取后 6 位
RE_CHECK_CODE = re.compile(rf"校验码{_SEP}(?:后\s*6\s*位{_SEP})?(?<!\d)(\d{{20}})(?!\d)")
# 有的票面把校验码写成分组形式
RE_CHECK_CODE_LOOSE = re.compile(rf"校验码{_SEP}(?:后\s*6\s*位{_SEP})?((?:\d[\s]*){{20}})")

RE_TOTAL_WITH_TAX = re.compile(r"(?:价税合计|小写)[^\d¥￥]{0,12}[¥￥]?\s*([\d,]+\.\d{2})")
RE_TOTAL_CN = re.compile(r"价税合计\s*\(大写\)")
# 注意 [：:\s]* —— 票面上常见「金额： ¥500.00」，标签和金额之间夹着全角冒号，
# 只写 \s* 会漏掉这一类，是老发票抽不到金额的最常见原因。
RE_AMOUNT_EXCL = re.compile(r"(?:合\s*计|金额)[：:\s]*[¥￥]\s*([\d,]+\.\d{2})")
RE_ANY_AMOUNT = re.compile(r"[¥￥]\s*([\d,]+\.\d{2})")

RE_SELLER = re.compile(r"销\s*售\s*方[^\n]{0,20}?名\s*称\s*[：:]\s*([^\n]{2,40})")
RE_BUYER = re.compile(r"购\s*买\s*方[^\n]{0,20}?名\s*称\s*[：:]\s*([^\n]{2,40})")


# --------------------------------------------------------------------------
#  OCR 容错版正则
# --------------------------------------------------------------------------
# 关键点：**必须在匹配阶段就容忍混淆字符**，不能等匹配成功后再纠正。
# 因为 `发票号码：I2345678` 里那个 `I`，会让 `\d{8}` 从一开始就失配，
# 后面的 fix_ocr_digits 根本没机会执行。
# 所以这里把「可能被看成数字的字符」都放进字符类，匹配到之后再转换。
_OCR_DIGITS = "0-9OoIlZzSsBbGgqD|"

RE_INVOICE_CODE_OCR = re.compile(
    rf"发票代码{_SEP}(?<![0-9A-Za-z])([{_OCR_DIGITS}]{{10,12}})(?![0-9A-Za-z])")
RE_INVOICE_NUMBER_OCR = re.compile(
    rf"发票号码{_SEP}(?<![0-9A-Za-z])([{_OCR_DIGITS}]{{20}}|[{_OCR_DIGITS}]{{8}})(?![0-9A-Za-z])")
RE_CHECK_CODE_OCR = re.compile(
    rf"校验码{_SEP}(?:后\s*6\s*位{_SEP})?(?<![0-9A-Za-z])([{_OCR_DIGITS}]{{20}})(?![0-9A-Za-z])")


# --------------------------------------------------------------------------
#  解析
# --------------------------------------------------------------------------

def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def _amount(raw: str | None) -> str | None:
    """金额统一成 2 位小数的字符串（不带千分位）。"""
    if not raw:
        return None
    try:
        return f"{float(raw.replace(',', '')):.2f}"
    except ValueError:
        return None


def _clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip(" :：\t")
    # 去掉尾部粘连的标签
    name = re.split(r"\s*(?:纳税人识别号|统一社会信用代码|地\s*址|开户行|电\s*话)\s*[：:]", name)[0]
    name = name.strip()
    return name if len(name) >= 2 else None


def _format_date(y: str, m: str, d: str) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _valid_date(y: str, m: str, d: str) -> bool:
    try:
        yi, mi, di = int(y), int(m), int(d)
    except ValueError:
        return False
    return 1994 <= yi <= 2100 and 1 <= mi <= 12 and 1 <= di <= 31


def parse_fields(raw_text: str, *, densify_digits: bool = True,
                 from_ocr: bool = False) -> dict[str, Any]:
    """从发票文本抽字段。

    返回 dict，缺失字段为 None。同时返回 `_sources` 说明每个字段是怎么来的，
    方便排查（比如是 densify 之后才命中的）。
    """
    text = normalize_text(raw_text)
    dense = densify(text) if densify_digits else text

    # 两个版本都试：densify 版优先（对付空格），原版兜底（对付跨行拼接误伤）
    variants: list[tuple[str, str]] = []
    if dense != text:
        variants.append(("densified", dense))
    variants.append(("raw", text))

    out: dict[str, Any] = {
        "invoice_code": None,
        "invoice_number": None,
        "invoice_date": None,
        "check_code": None,        # 完整 20 位
        "check_code_last6": None,  # 平台要的后 6 位
        "amount_total": None,      # 价税合计
        "amount_excl_tax": None,   # 不含税金额
        "seller_name": None,
        "buyer_name": None,
        "invoice_kind": None,      # legacy | fully_digital
        "_sources": {},
        "_raw_text": text,
    }

    # ---- 发票代码 / 发票号码 ----
    # OCR 来源要换成容错版正则，否则带字母那一位会让整条正则失配
    number_re = RE_INVOICE_NUMBER_OCR if from_ocr else RE_INVOICE_NUMBER
    code_re = RE_INVOICE_CODE_OCR if from_ocr else RE_INVOICE_CODE

    for tag, body in variants:
        if out["invoice_number"] is None:
            val = _first_group(number_re, body)
            if val:
                out["invoice_number"] = fix_ocr_digits(val) if from_ocr else val
                out["_sources"]["invoice_number"] = tag
        if out["invoice_code"] is None:
            val = _first_group(code_re, body)
            if val:
                out["invoice_code"] = fix_ocr_digits(val) if from_ocr else val
                out["_sources"]["invoice_code"] = tag

    # ---- 开票日期 ----
    for tag, body in variants:
        m = RE_INVOICE_DATE.search(body)
        if m and _valid_date(*m.groups()):
            out["invoice_date"] = _format_date(*m.groups())
            out["_sources"]["invoice_date"] = tag
            break
    if out["invoice_date"] is None:
        # 兜底：取票面上第一个合法日期
        for tag, body in variants:
            for m in RE_ANY_DATE.finditer(body):
                if _valid_date(*m.groups()):
                    out["invoice_date"] = _format_date(*m.groups())
                    out["_sources"]["invoice_date"] = f"{tag}:fallback-any-date"
                    break
            if out["invoice_date"]:
                break

    # ---- 校验码 ----
    check_re = RE_CHECK_CODE_OCR if from_ocr else RE_CHECK_CODE
    for tag, body in variants:
        val = _first_group(check_re, body)
        if not val and not from_ocr:
            m = RE_CHECK_CODE_LOOSE.search(body)
            if m:
                val = re.sub(r"\s", "", m.group(1))
        if val and len(val) == 20:
            if from_ocr:
                val = fix_ocr_digits(val)
            out["check_code"] = val
            out["check_code_last6"] = val[-6:]
            out["_sources"]["check_code"] = tag
            break

    # ---- 金额 ----
    for tag, body in variants:
        if out["amount_total"] is None:
            got = _amount(_first_group(RE_TOTAL_WITH_TAX, body))
            if got:
                out["amount_total"] = got
                out["_sources"]["amount_total"] = tag
        if out["amount_excl_tax"] is None:
            got = _amount(_first_group(RE_AMOUNT_EXCL, body))
            if got:
                out["amount_excl_tax"] = got
                out["_sources"]["amount_excl_tax"] = tag
        if out["amount_total"] and out["amount_excl_tax"]:
            break

    # 只有金额没有价税合计时，用不不含税的兜底；都没有就取票面第一个金额
    if out["amount_total"] is None and out["amount_excl_tax"] is not None:
        out["amount_total"] = out["amount_excl_tax"]
        out["_sources"]["amount_total"] = "derived-from-excl-tax"
    if out["amount_total"] is None:
        for tag, body in variants:
            got = _amount(_first_group(RE_ANY_AMOUNT, body))
            if got:
                out["amount_total"] = got
                out["_sources"]["amount_total"] = f"{tag}:fallback-any-amount"
                break

    # ---- 购销方名称（锦上添花，缺了不影响查验）----
    for tag, body in variants:
        if out["seller_name"] is None:
            out["seller_name"] = _clean_name(_first_group(RE_SELLER, body))
        if out["buyer_name"] is None:
            out["buyer_name"] = _clean_name(_first_group(RE_BUYER, body))
        if out["seller_name"] and out["buyer_name"]:
            break

    # ---- 判定票种 ----
    num = out["invoice_number"]
    if num and len(num) == 20:
        out["invoice_kind"] = "fully_digital"
    elif num:
        out["invoice_kind"] = "legacy"

    return out


def verification_inputs(fields: dict[str, Any]) -> dict[str, Any]:
    """返回查验平台可能需要的**全部**取值，由驱动按表单实际标签挑选。

    为什么不能在这里就决定填哪个值
    ------------------------------
    实测（2026-09）平台第 4 个字段的**标签是动态的**，随发票号码/代码变化：

        空白 / 只有 8 位号码   → 「开具金额(不含税)」→ 填不含税金额
        12 位代码 + 8 位号码   → 「校验码」          → 填校验码后 6 位
        20 位号码（数电票）     → 「价税合计」        → 填价税合计

    所以这里把候选值全给出去，驱动读标签再决定填哪个。
    这样平台以后再改变种判定规则，也不用改这里的逻辑。
    """
    return {
        "kind": fields.get("invoice_kind"),
        "invoice_code": fields.get("invoice_code"),
        "invoice_number": fields.get("invoice_number"),
        "invoice_date": fields.get("invoice_date"),
        "check_code_last6": fields.get("check_code_last6"),
        "amount_excl_tax": fields.get("amount_excl_tax"),
        "amount_total": fields.get("amount_total"),
    }


def missing_required(fields: dict[str, Any]) -> list[str]:
    """返回还缺哪些「必需字段」的中文名。空列表代表可以查验。

    第 4 个字段是三选一（校验码 / 不含税金额 / 价税合计），
    只要有一个能填就行，所以这里不做票种区分。
    """
    missing: list[str] = []

    if not fields.get("invoice_number"):
        missing.append("发票号码")
    if not fields.get("invoice_date"):
        missing.append("开票日期")

    if not any(fields.get(k) for k in
               ("check_code_last6", "amount_excl_tax", "amount_total")):
        missing.append("校验码后6位或金额")

    return missing


def sanity_check(fields: dict[str, Any], text: str | None = None) -> list[str]:
    """格式层面的合理性检查，返回警告（不阻断）。

    传入 `text` 时还会额外检查「标签出现了、但没抽到合法值」。
    这一条对 OCR 票面尤其重要：否则用户只能看到一句「缺少字段」，
    无从判断到底是票面上没有这个字段，还是识别到了但格式不对。
    """
    warns: list[str] = []

    code = fields.get("invoice_code")
    if code and len(str(code)) not in (10, 12):
        warns.append(f"发票代码位数异常（{len(str(code))} 位，应为 10 或 12）")

    number = fields.get("invoice_number")
    if number and len(str(number)) not in (8, 20):
        warns.append(f"发票号码位数异常（{len(str(number))} 位，应为 8 或 20）")

    date = fields.get("invoice_date")
    if date:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(date))
        if not m or not _valid_date(*m.groups()):
            warns.append(f"开票日期不可信：{date}")

    cc = fields.get("check_code")
    if cc and len(str(cc)) != 20:
        warns.append(f"校验码位数异常（{len(str(cc))} 位，应为 20）")

    if text:
        if not fields.get("invoice_code") and "发票代码" in text:
            warns.append("票面有「发票代码」，但没抽到合法值（位数不符或识别有误）")
        if not fields.get("invoice_number") and "发票号码" in text:
            warns.append("票面有「发票号码」，但没抽到合法值（位数不符或识别有误）")
        if not fields.get("invoice_date") and "开票日期" in text:
            warns.append("票面有「开票日期」，但没抽到合法日期")
        if not fields.get("check_code") and "校验码" in text:
            warns.append("票面有「校验码」，但没抽到 20 位校验码")

    return warns
