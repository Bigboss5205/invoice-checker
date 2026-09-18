"""项目自检：不联网、不查真实发票，把一个部署是否健康查一遍。

用法：

    .build-venv\\Scripts\\python.exe -m scripts.selftest
    # 打包后的目录里：
    python -m scripts.selftest --browser

检查内容：
  1. 依赖是否装齐
  2. 配置加载、默认值合并、环境变量覆盖
  3. 发票字段解析规则（内嵌多组真实票面写法的用例）
  4. OFD / XML 文件端到端抽取
  5. **整条流程演练**：扫目录 → 抽字段 → 出结果 → 存「-已查验.pdf」→ 再跑不重复
  6. SQLite 查验记录
  7. 界面可用性（Tkinter 能否建窗、能否显示 PNG 验证码）
  8. 可选：浏览器能否启动（--browser）

退出码 0 表示没有 FAIL。SKIP 不算失败。
"""

from __future__ import annotations

import argparse
import base64
import importlib
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_RESULTS: list[tuple[str, str, str]] = []


# ---------------------------------------------------------------------------
#  迷你测试框架
# ---------------------------------------------------------------------------
def record(level: str, name: str, detail: str = "") -> None:
    _RESULTS.append((level, name, detail))
    icon = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP", "INFO": "    "}[level]
    print(f"  [{icon}] {name}", flush=True)
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}", flush=True)


def section(title: str) -> None:
    print(f"\n【{title}】", flush=True)


def check(name: str, func) -> bool:
    try:
        detail = func()
        record("PASS", name, str(detail) if detail else "")
        return True
    except AssertionError as exc:
        record("FAIL", name, str(exc))
        return False
    except Exception as exc:
        record("FAIL", name, f"{type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
#  1. 依赖
# ---------------------------------------------------------------------------
REQUIRED = {"yaml": "配置文件解析", "tkinter": "图形界面"}

RECOMMENDED = {
    "playwright": "浏览器自动化 —— 缺了就无法真实查验",
    "pdfplumber": "PDF 文本层抽取 —— 缺了无法处理 PDF 发票",
    "pypdfium2": "无文本层 PDF 渲染 —— 缺了无法处理扫描件",
    "PIL": "图片处理",
}

OPTIONAL = {
    "ddddocr": "验证码自动识别 —— 缺了全部走人工输入",
    "defusedxml": "XML 安全解析",
    "paddleocr": "图片发票全票面 OCR（可选加强）",
}


def check_dependencies() -> None:
    section("依赖")

    for mod, why in REQUIRED.items():
        def probe(m=mod):
            importlib.import_module(m)
            return why

        if not check(f"{mod}（必需 · {why}）", probe):
            record("INFO", "必需依赖缺失，程序无法正常启动")

    for mod, why in RECOMMENDED.items():
        try:
            importlib.import_module(mod)
            record("PASS", f"{mod}（重要 · {why}）")
        except ImportError:
            record("SKIP", f"{mod}（重要 · {why}）", "未安装")

    for mod, why in OPTIONAL.items():
        try:
            importlib.import_module(mod)
            record("PASS", f"{mod}（可选 · {why}）")
        except ImportError:
            record("SKIP", f"{mod}（可选 · {why}）", "未安装")


# ---------------------------------------------------------------------------
#  2. 配置
# ---------------------------------------------------------------------------
def check_config(tmp: Path) -> bool:
    section("配置")

    try:
        from app.config import DEFAULTS, load_config
    except Exception as exc:
        record("SKIP", "配置模块", f"无法导入 app.config：{exc}")
        return False

    cfg_file = tmp / "config.yaml"
    cfg_file.write_text(
        "paths:\n"
        f'  workdir: "{(tmp / "invoices").as_posix()}"\n'
        f'  db: "{(tmp / "db" / "x.sqlite3").as_posix()}"\n'
        f'  log: "{(tmp / "x.log").as_posix()}"\n'
        "verify:\n"
        "  driver: fake\n"
        "  min_interval_seconds: 3\n"
        "captcha:\n"
        "  max_auto_attempts: 5\n",
        encoding="utf-8",
    )
    os.environ["INV_CONFIG"] = str(cfg_file)

    holder: dict = {}

    def load():
        holder["cfg"] = load_config()
        return f"来源 {holder['cfg'].source}"

    if not check("加载 config.yaml", load):
        return False
    cfg = holder["cfg"]

    def merged():
        # 用户只写几个键，其余必须继承默认值
        assert cfg.get("platform.home_url") == DEFAULTS["platform"]["home_url"], \
            "platform.home_url 没继承默认值"
        assert cfg.get("verify.driver") == "fake", "用户配置未生效"
        assert cfg.get("captcha.max_auto_attempts") == 5, "整数覆盖失败"
        assert cfg.get("captcha.manual_fallback", None) is None or \
            cfg.get("captcha.manual_max_rounds") == 3, "默认值丢失"
        assert cfg.get("output.suffix") == "-已查验", "结果后缀默认值不对"
        return "深合并正常（dict 递归、标量覆盖）"

    check("默认值深合并", merged)

    def paths():
        assert cfg.workdir == (tmp / "invoices"), f"workdir 解析错误：{cfg.workdir}"
        cfg.ensure_dirs()
        assert cfg.workdir.is_dir(), "workdir 没建出来"
        assert cfg.out_dir == cfg.workdir, "output.subfolder 留空时应与发票同目录"
        return f"workdir = {cfg.workdir}"

    check("路径解析与目录创建", paths)

    def env_override():
        os.environ["INV_MIN_INTERVAL"] = "42"
        try:
            assert float(load_config().get("verify.min_interval_seconds")) == 42.0, \
                "环境变量覆盖失败"
        finally:
            os.environ.pop("INV_MIN_INTERVAL", None)
        return "INV_MIN_INTERVAL=42 生效"

    check("环境变量覆盖", env_override)

    def negative():
        cfg._data["verify"]["driver"] = "nonsense"
        problems = cfg.validate()
        cfg._data["verify"]["driver"] = "fake"
        assert any("driver" in p for p in problems), "非法 driver 没被拦下"
        return "非法 driver 能被拦下"

    check("配置校验（负例）", negative)
    return True


# ---------------------------------------------------------------------------
#  3. 字段解析
# ---------------------------------------------------------------------------
class StubCfg:
    """给抽取函数用的极简配置桩，让测试不依赖真实 config.yaml。"""

    def __init__(self, data: dict):
        self._d = data

    def get(self, path: str, default=None):
        node = self._d
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, path: str) -> dict:
        val = self.get(path, {})
        return val if isinstance(val, dict) else {}


EXTRACT_CFG = StubCfg({
    "extract": {"densify_digits": True, "render_dpi": 220,
                "image_ocr": False, "save_debug_text": False},
})

CASES: list[tuple[str, str, dict]] = [
    (
        "电子普通发票（数字被排版插入空格）",
        """
        增值税电子普通发票
        发票代码： 0 1 1 0 0 2 0 0 0 3 1 1
        发票号码： 1 2 3 4 5 6 7 8
        开票日期： 2024年03月15日
        校验码： 12345 67890 12345 67890
        价税合计（大写）壹佰贰拾叁圆肆角伍分 （小写）¥123.45
        """,
        {
            "invoice_code": "011002000311",
            "invoice_number": "12345678",
            "invoice_date": "2024-03-15",
            "check_code_last6": "567890",
            "amount_total": "123.45",
            "invoice_kind": "legacy",
        },
    ),
    (
        "数电票（20 位号码，无代码无校验码）",
        """
        电子发票（增值税专用发票）
        发票号码： 2 4 3 1 2 0 0 0 0 0 0 0 1 2 3 4 5 6 7 8
        开票日期： 2024年05月20日
        合 计 ¥1000.00 ¥130.00
        价税合计（大写）壹仟壹佰叁拾圆整 （小写）¥1130.00
        """,
        {
            "invoice_number": "24312000000012345678",
            "invoice_date": "2024-05-20",
            "amount_excl_tax": "1000.00",
            "amount_total": "1130.00",
            "invoice_kind": "fully_digital",
        },
    ),
    (
        "标签与值被拆到不同行",
        """
        发票代码
        011002000311
        发票号码
        87654321
        开票日期
        2024-01-05
        校验码
        09876543210987654321
        """,
        {
            "invoice_code": "011002000311",
            "invoice_number": "87654321",
            "invoice_date": "2024-01-05",
            "check_code_last6": "654321",
            "invoice_kind": "legacy",
        },
    ),
    (
        "专用发票（无校验码，改用金额）",
        """
        增值税专用发票
        发票代码： 011002000311
        发票号码： 12345678
        开票日期： 2024年02月10日
        金额： ¥500.00
        """,
        {
            "invoice_code": "011002000311",
            "invoice_number": "12345678",
            "invoice_date": "2024-02-10",
            "amount_excl_tax": "500.00",
            "invoice_kind": "legacy",
        },
    ),
    (
        "OCR 常见字符混淆（O/I 当 0/1）",
        """
        发票代码： O11OO2OOO311
        发票号码： I2345678
        开票日期： 2024年07月01日
        校验码： 123456789O123456789O
        """,
        {
            "invoice_number": "12345678",
            "invoice_date": "2024-07-01",
            "check_code_last6": "567890",
        },
    ),
]


def check_field_parsing() -> bool:
    section("字段解析")

    try:
        from app.extract import fields as F
    except Exception as exc:
        record("SKIP", "字段解析", f"无法导入 app.extract.fields：{exc}")
        return False

    ok = True
    for title, text, expected in CASES:
        def run(t=text, e=expected, name=title):
            parsed = F.parse_fields(t, from_ocr=("OCR" in name))
            bad = [f"{k}: 期望 {w!r}，实际 {parsed.get(k)!r}"
                   for k, w in e.items() if parsed.get(k) != w]
            assert not bad, "；".join(bad)
            return " / ".join(f"{k}={parsed.get(k)}" for k in e)

        ok &= check(title, run)

    def ticket_routing():
        legacy = F.parse_fields(
            "发票代码：011002000311\n发票号码：12345678\n开票日期：2024年01月01日",
            from_ocr=False)
        missing = F.missing_required(legacy)
        assert any("校验码" in m or "金额" in m for m in missing), \
            f"老发票缺校验码和金额，应报缺字段，实际：{missing}"

        digital = F.parse_fields(
            "发票号码：24312000000012345678\n开票日期：2024年05月20日\n合 计 ¥1000.00",
            from_ocr=False)
        ins = F.verification_inputs(digital)
        assert ins["kind"] == "fully_digital", "20 位号码应判为数电票"
        # 回归用例：曾经因为字段名对不上，数电票的金额会丢失，
        # 导致真实平台上所有数电票都报「入参不完整」。
        assert ins["amount_excl_tax"] == "1000.00", \
            f"数电票必须带出不含税金额，实际 {ins}"
        assert not F.missing_required(digital), "数电票字段齐全，不该报缺"

        # 平台第 4 个字段的标签是动态的，所以候选值必须都给出来，
        # 由驱动读标签再决定填哪个。
        for key in ("check_code_last6", "amount_excl_tax", "amount_total",
                    "invoice_number", "invoice_date", "invoice_code"):
            assert key in ins, f"verification_inputs 缺少候选键 {key}"
        return "票种分流正常，金额未丢失，候选值齐全"

    ok &= check("缺字段判定 / 候选值", ticket_routing)

    def sanity_negative():
        bad = F.parse_fields(
            "发票代码：123\n发票号码：999\n开票日期：2024年13月45日", from_ocr=False)
        warns = F.sanity_check(bad, bad.get("_raw_text"))
        assert len(warns) >= 3, f"应报 3 条（代码/号码/日期），实际 {len(warns)}：{warns}"
        return f"{len(warns)} 条告警，例如：{warns[0]}"

    ok &= check("合理性检查（负例）", sanity_negative)

    def sanity_positive():
        good = F.parse_fields(CASES[0][1], from_ocr=False)
        warns = F.sanity_check(good, good.get("_raw_text"))
        assert not warns, f"正常票面不该报警告：{warns}"
        return "正常票面零告警（不误报）"

    ok &= check("合理性检查（正例）", sanity_positive)
    return ok


# ---------------------------------------------------------------------------
#  4. 文件抽取
# ---------------------------------------------------------------------------
OFD_PAGE = """<?xml version="1.0" encoding="UTF-8"?>
<ofd:Page xmlns:ofd="http://www.ofd.org/ofd/1.0">
  <ofd:Content>
    <ofd:TextObject><ofd:TextCode>发票代码：011002000311</ofd:TextCode></ofd:TextObject>
    <ofd:TextObject><ofd:TextCode>发票号码：12345678</ofd:TextCode></ofd:TextObject>
    <ofd:TextObject><ofd:TextCode>开票日期：2024年03月15日</ofd:TextCode></ofd:TextObject>
    <ofd:TextObject><ofd:TextCode>校验码：12345678901234567890</ofd:TextCode></ofd:TextObject>
    <ofd:TextObject><ofd:TextCode>价税合计（小写）¥123.45</ofd:TextCode></ofd:TextObject>
  </ofd:Content>
</ofd:Page>
"""

EINVOICE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<EInvoice>
  <InvoiceNumber>24312000000012345678</InvoiceNumber>
  <IssueTime>2024-05-20</IssueTime>
  <TotalAmWithoutTax>1000.00</TotalAmWithoutTax>
  <TotalTax-includedAmount>1130.00</TotalTax-includedAmount>
  <SellerName>某某科技有限公司</SellerName>
</EInvoice>
"""


def make_ofd(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Doc_0/Pages/Page_0/Content.xml", OFD_PAGE)
    return path


def make_xml(path: Path) -> Path:
    path.write_text(EINVOICE_XML, encoding="utf-8")
    return path


def check_extraction(tmp: Path) -> bool:
    section("文件抽取")

    try:
        from app.extract import extract_invoice
        from app.extract.errors import ExtractionError, UnsupportedFormat
    except Exception as exc:
        record("SKIP", "文件抽取", f"无法导入 app.extract：{exc}")
        return False

    ok = True

    def ofd():
        result = extract_invoice(make_ofd(tmp / "sample.ofd"), EXTRACT_CFG)
        f = result.fields
        assert f["invoice_number"] == "12345678", f"号码错了：{f['invoice_number']}"
        assert f["invoice_code"] == "011002000311", f"代码错了：{f['invoice_code']}"
        assert f["invoice_date"] == "2024-03-15", f"日期错了：{f['invoice_date']}"
        assert f["check_code_last6"] == "567890", f"校验码错了：{f['check_code_last6']}"
        assert result.ok, f"应字段齐全，实际缺：{result.missing}"
        return f"来源 {result.source}"

    ok &= check("OFD 抽取", ofd)

    def einvoice():
        result = extract_invoice(make_xml(tmp / "sample.xml"), EXTRACT_CFG)
        f = result.fields
        assert f["invoice_number"] == "24312000000012345678", f"号码：{f['invoice_number']}"
        assert f["invoice_date"] == "2024-05-20", f"日期：{f['invoice_date']}"
        assert f["amount_excl_tax"] == "1000.00", f"不含税金额：{f['amount_excl_tax']}"
        assert f["amount_total"] == "1130.00", f"价税合计：{f['amount_total']}"
        assert f["invoice_kind"] == "fully_digital", "应判为数电票"
        assert f["seller_name"] == "某某科技有限公司", f"销方：{f['seller_name']}"
        assert result.used_structured_xml, "应走 XML 结构化解析"
        return f"来源 {result.source}（结构化命中）"

    ok &= check("数电票 XML 抽取（结构化）", einvoice)

    def unsupported():
        path = tmp / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        try:
            extract_invoice(path, EXTRACT_CFG)
        except UnsupportedFormat:
            return "非发票后缀被正确拒绝"
        raise AssertionError("UnsupportedFormat 没有被抛出")

    ok &= check("不支持的格式被拒绝", unsupported)

    def broken_pdf():
        path = tmp / "broken.pdf"
        path.write_bytes(b"this is definitely not a pdf")
        try:
            extract_invoice(path, EXTRACT_CFG)
        except ExtractionError:
            return "损坏文件抛 ExtractionError，不会崩"
        except ImportError:
            record("SKIP", "损坏 PDF 处理", "未安装 pdfplumber")
            return None
        return "没有抛错（可能返回了残缺结果）"

    check("损坏文件不崩进程", broken_pdf)

    def real_pdf():
        """生成一个合法 PDF，验证它能被打开、且不会误判为「有可用文本层」。"""
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            record("SKIP", "PDF 打开", "未安装 pdfplumber")
            return None
        from app.verify.fake import build_pdf
        from app.extract import pdf_text

        path = tmp / "plain.pdf"
        path.write_bytes(build_pdf(["Hello", "No invoice fields here"]))
        text = pdf_text.extract_text_layer(path)
        assert "Hello" in text, f"PDF 文本层读不出来：{text!r}"
        assert not pdf_text.looks_usable(text), \
            "没有票面关键词的 PDF 不该被判为「文本层可用」"
        return "能读文本层，且正确判别为不可用（会转 OCR 或人工录入）"

    check("PDF 文本层判别", real_pdf)
    return ok


# ---------------------------------------------------------------------------
#  5. 整条流程演练（最重要的一项）
# ---------------------------------------------------------------------------
def check_end_to_end(tmp: Path) -> bool:
    section("整条流程演练（离线假驱动）")

    try:
        from app.config import load_config
        from app.runner import NullUi, Runner
    except Exception as exc:
        record("SKIP", "流程演练", f"无法导入 app.runner：{exc}")
        return False

    work = tmp / "e2e"
    work.mkdir(parents=True, exist_ok=True)

    cfg_file = work / "config.yaml"
    cfg_file.write_text(
        "paths:\n"
        f'  workdir: "{work.as_posix()}"\n'
        f'  db: "{(work / "rec.sqlite3").as_posix()}"\n'
        f'  log: "{(work / "run.log").as_posix()}"\n'
        "verify:\n"
        "  driver: fake\n"
        "  min_interval_seconds: 0\n"
        "  max_attempts: 1\n"
        "  fake_delay_seconds: 0.05\n"
        "output:\n"
        "  pdf: true\n"
        '  suffix: "-已查验"\n'
        "scan:\n"
        "  skip_verified: true\n",
        encoding="utf-8",
    )

    make_xml(work / "数电票A.xml")
    make_ofd(work / "老发票B.ofd")

    cfg = load_config(cfg_file)
    cfg.ensure_dirs()

    ok = True
    report_holder: dict = {}

    def first_run():
        runner = Runner(cfg, NullUi())
        report = runner.run()
        report_holder["report"] = report
        assert report.total == 2, f"应扫到 2 个文件，实际 {report.total}"
        assert len(report.results) == 2, f"应处理 2 个，实际 {len(report.results)}"
        for res in report.results:
            assert res.pdf_path is not None, f"{res.path.name} 没有生成结果 PDF"
            assert res.pdf_path.exists(), f"{res.pdf_path} 不存在"
        return report.summary_line()

    if not check("扫描 → 查验 → 出 PDF", first_run):
        return False

    report = report_holder["report"]

    def naming():
        names = sorted(r.pdf_path.name for r in report.results)
        assert names == ["数电票A-已查验.pdf", "老发票B-已查验.pdf"], \
            f"命名不符合「原文件名-已查验」：{names}"
        return " / ".join(names)

    ok &= check("结果命名规则", naming)

    def valid_pdf():
        for res in report.results:
            head = res.pdf_path.read_bytes()[:5]
            assert head == b"%PDF-", f"{res.pdf_path.name} 不是合法 PDF：{head!r}"
        return "两个结果文件都是合法 PDF（%PDF- 头）"

    ok &= check("结果 PDF 可读", valid_pdf)

    def not_self_looping():
        """最容易犯的错：把自己的产物当发票再查一遍。"""
        runner = Runner(cfg, NullUi())
        again = runner.scan()
        assert again == [], \
            f"第二次扫描不该有任何文件（结果 PDF 被当发票了？）：{[p.name for p in again]}"
        return "重复运行时 0 个待处理（已跳过 / 未把自己的产物当发票）"

    ok &= check("重复运行不重复处理", not_self_looping)

    def record_db():
        import sqlite3

        conn = sqlite3.connect(str(work / "rec.sqlite3"))
        try:
            rows = conn.execute(
                "SELECT filename, invoice_number, amount, verify_status FROM tasks"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2, f"应有 2 条记录，实际 {len(rows)}"

        by_name = {r[0]: r for r in rows}
        digital = by_name.get("数电票A.xml")
        assert digital is not None, f"找不到数电票记录：{list(by_name)}"
        assert digital[1] == "24312000000012345678", f"号码记录错了：{digital[1]}"
        # 回归：金额曾经因为字段名对不上而永远记不进去
        assert digital[2] == "1000.00", f"数电票金额应记录为 1000.00，实际 {digital[2]!r}"
        return f"2 条记录，数电票号码 {digital[1]}，金额 {digital[2]}"

    ok &= check("查验记录落库（含金额）", record_db)

    def output_subfolder():
        """结果另存子目录时，子目录里的产物也不能被当发票扫进来。"""
        sub = tmp / "e2e_sub"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "已查验").mkdir(exist_ok=True)
        sub_cfg_file = sub / "config.yaml"
        sub_cfg_file.write_text(
            "paths:\n"
            f'  workdir: "{sub.as_posix()}"\n'
            f'  db: "{(sub / "r.sqlite3").as_posix()}"\n'
            f'  log: "{(sub / "r.log").as_posix()}"\n'
            "verify:\n  driver: fake\n  min_interval_seconds: 0\n"
            "  fake_delay_seconds: 0.02\n"
            "output:\n  subfolder: \"已查验\"\n",
            encoding="utf-8")
        make_xml(sub / "票C.xml")
        sub_cfg = load_config(sub_cfg_file)
        sub_cfg.ensure_dirs()
        runner = Runner(sub_cfg, NullUi())
        rep = runner.run()
        assert rep.total == 1, f"应扫到 1 个，实际 {rep.total}"
        res = rep.results[0]
        assert res.pdf_path is not None, "没生成结果"
        assert res.pdf_path.parent == (sub / "已查验"), (
            f"结果应存到发票目录下的「已查验」子目录，实际跑到 {res.pdf_path.parent}"
            "（subfolder 是相对发票目录解析的，不是相对程序目录）")
        # 二次扫描：结果 PDF 已躺在「已查验」子目录里，不该被当成发票再查一遍。
        # 只扫描不处理也要释放句柄，否则发票目录会被占用、移不动。
        checker = Runner(sub_cfg, NullUi())
        try:
            again = checker.scan()
            assert again == [], f"二次扫描不该有文件，实际 {[p.name for p in again]}"
        finally:
            checker.close()
        return f"结果落在 {res.pdf_path.parent.name}\\{res.pdf_path.name}，二次扫描 0 个"

    ok &= check("结果另存子目录", output_subfolder)

    def run_log_written():
        """窗口程序没有控制台，日志文件必须记录整次运行的过程。

        回归用例：曾经界面上的进展只发给 GUI，日志跑完是 0 字节，
        用户遇到问题拿不到任何排查线索。

        这里走真实的 setup_logging，顺带验证日志初始化本身能用。
        """
        import logging

        from app import logsetup

        log_path = tmp / "runlog.txt"
        written = logsetup.setup_logging(log_path, console=False)
        assert written == log_path, f"setup_logging 没有启用文件日志：{written}"

        root = logging.getLogger()
        try:
            work2 = tmp / "e2e_log"
            work2.mkdir(parents=True, exist_ok=True)
            cfg2_file = work2 / "config.yaml"
            cfg2_file.write_text(
                "paths:\n"
                f'  workdir: "{work2.as_posix()}"\n'
                f'  db: "{(work2 / "l.sqlite3").as_posix()}"\n'
                f'  log: "{(work2 / "l.log").as_posix()}"\n'
                "verify:\n  driver: fake\n  min_interval_seconds: 0\n"
                "  fake_delay_seconds: 0.02\n",
                encoding="utf-8")
            make_xml(work2 / "日志票.xml")
            cfg2 = load_config(cfg2_file)
            cfg2.ensure_dirs()
            Runner(cfg2, NullUi()).run()
        finally:
            # 摘掉文件 handler，否则临时目录在 Windows 上删不掉
            for handler in list(root.handlers):
                if isinstance(handler, logging.FileHandler):
                    root.removeHandler(handler)
                    handler.close()

        text = log_path.read_text(encoding="utf-8")
        assert text.strip(), "日志文件是空的 —— 界面消息没有镜像到日志"
        assert "发票目录" in text, "日志没记录发票目录（排查时第一个要看的）"
        assert "日志票.xml" in text, "日志没记录处理了哪个文件"
        assert "已生成" in text, "日志没记录结果产出"
        return f"日志 {len(text)} 字节，含目录/配置/文件名/产出"

    ok &= check("运行过程写入日志", run_log_written)
    return ok


# ---------------------------------------------------------------------------
#  6. 数据库
# ---------------------------------------------------------------------------
def check_database(tmp: Path) -> bool:
    section("查验记录（SQLite）")

    try:
        from app.db import Database
    except Exception as exc:
        record("SKIP", "状态存储", f"无法导入 app.db：{exc}")
        return False

    db = Database(tmp / "db" / "test.sqlite3")
    ok = True

    def basic():
        tid = db.add_task(filename="a.pdf", source_path="/x/a.pdf",
                          file_size=10, file_hash="hash1")
        task = db.get_task(tid)
        assert task is not None and task["status"] == "pending", "初始状态不对"
        db.update_task(tid, invoice_number="12345678", extract_notes=["w1", "w2"])
        task = db.get_task(tid)
        assert task["invoice_number"] == "12345678", "更新字段失败"
        assert "w1" in str(task["extract_notes"]), "列表字段应序列化成 JSON"
        return "增 / 改 / 查 正常"

    ok &= check("基本读写", basic)

    def reset():
        tids = [db.add_task(filename=f"c{i}.pdf", source_path=f"/x/c{i}.pdf",
                            file_hash=f"h{i}", status="verifying") for i in range(3)]
        n = db.reset_in_flight()
        assert n == 3, f"应重置 3 条，实际 {n}"
        for tid in tids:
            assert db.get_task(tid)["status"] == "pending", "重启后状态没退回"
        return "进程重启后卡住的任务能退回队列"

    ok &= check("重启后重置卡住的任务", reset)

    def counts():
        counts_by = db.count_by_status()
        assert isinstance(counts_by, dict) and "pending" in counts_by, "状态统计异常"
        return f"总计 {db.total()} 条"

    ok &= check("状态统计", counts)

    db.close()
    return ok


# ---------------------------------------------------------------------------
#  7. 界面
# ---------------------------------------------------------------------------
def check_gui() -> bool:
    section("界面（Tkinter）")

    try:
        import tkinter as tk
    except ImportError as exc:
        record("FAIL", "导入 tkinter",
               f"{exc}\nWindows 官方 Python 自带 tkinter；嵌入式/精简发行版会缺。")
        return False

    state: dict = {}

    def create_window():
        root = tk.Tk()
        root.withdraw()
        root.update()
        state["root"] = root
        return f"Tk {root.tk.call('info', 'patchlevel')}"

    if not check("创建窗口", create_window):
        return False

    root = state["root"]
    ok = True

    def png_support():
        """验证码是靠 PNG 显示在弹窗里的，这条链断了用户就没法输入。"""
        from app.verify.fake import _placeholder_png

        photo = tk.PhotoImage(data=base64.b64encode(_placeholder_png()).decode("ascii"))
        assert photo.width() >= 1, "PhotoImage 宽度异常"
        zoomed = photo.zoom(3)
        assert zoomed.width() == photo.width() * 3, "放大显示失败"
        return (f"能显示并放大 PNG"
                f"（{photo.width()}x{photo.height()} → {zoomed.width()}x{zoomed.height()}）")

    ok &= check("验证码图片显示能力", png_support)

    def build_main_window():
        from app.config import load_config
        from app.gui import MainWindow

        win = MainWindow(root, load_config())
        root.update()
        assert win.tree is not None, "任务列表没建出来"
        assert win.progress is not None, "进度条没建出来"
        state["win"] = win
        return "主窗口构建成功（控件齐全）"

    if not check("构建主窗口", build_main_window):
        try:
            root.destroy()
        except Exception:
            pass
        return False

    win = state["win"]

    def log_and_progress():
        win._append_log("测试日志一行")
        win._update_progress(3, 10, "x.pdf")
        root.update()
        assert "测试日志一行" in win.log_text.get("1.0", "end"), "日志没写进去"
        assert float(win.progress["value"]) == 3.0, "进度条数值不对"
        return "日志与进度条更新正常"

    ok &= check("日志与进度", log_and_progress)

    def result_row():
        from pathlib import Path

        from app.runner import FileResult

        win._add_result(FileResult(path=Path("测试发票.xml"), status="ok",
                                   label="一致", summary="查验成功"))
        root.update()
        rows = win.tree.get_children()
        assert rows, "结果行没插进列表"
        assert win.tree.item(rows[-1])["values"][0] == "测试发票.xml", "列表内容不对"
        return "结果能实时上屏"

    ok &= check("结果列表写入", result_row)

    def captcha_flow():
        """整个程序最关键的人工兜底：弹窗要能建、能提交、能唤醒工作线程。"""
        from app.gui import CaptchaDialog, _Answer
        from app.verify.fake import _placeholder_png

        box = _Answer()
        dlg = CaptchaDialog(win, {"task_id": "t1", "png": _placeholder_png(),
                                  "filename": "测试发票.pdf", "hint": "12345678",
                                  "timeout": 30}, box)
        root.update()
        assert dlg.winfo_exists(), "验证码弹窗没建起来"

        dlg.entry.insert(0, "ab1")
        dlg._on_key(type("_Evt", (), {"keysym": "1"})())   # 只做大写化
        root.update()
        # 回归用例：曾经写成「满 4 位自动提交」，但平台验证码**位数不固定**，
        # 所以输入过程中绝不能自动提交。
        assert not box.event.is_set(), "不该因为长度就自动提交（验证码位数不固定）"
        assert dlg.entry.get() == "AB1", f"应转成大写，实际 {dlg.entry.get()!r}"

        dlg._submit()                                       # 显式提交
        root.update()
        assert box.event.wait(2), "提交后没有唤醒等待方"
        assert box.value == "AB1", f"应返回大写验证码，实际 {box.value!r}"
        return "3 位也能提交，且输入过程中不会自动提交"

    ok &= check("验证码弹窗（人工兜底）", captcha_flow)

    def captcha_lengths():
        """验证码位数不固定：短的、长的都要能正常提交。"""
        from app.gui import CaptchaDialog, _Answer
        from app.verify.fake import _placeholder_png

        results = []
        for code in ("AB", "AB1", "AB12", "AB12CD", "AB12CDE"):
            box = _Answer()
            dlg = CaptchaDialog(win, {"task_id": f"t-{code}", "png": _placeholder_png(),
                                      "filename": "x.pdf", "hint": "",
                                      "timeout": 30}, box)
            root.update()
            dlg.entry.insert(0, code)
            dlg._submit()
            assert box.event.wait(2), f"{code!r} 没有提交成功"
            assert box.value == code, f"应原样返回 {code!r}，实际 {box.value!r}"
            results.append(str(len(code)))
        return f"长度 {', '.join(results)} 位全部原样提交"

    ok &= check("验证码长度不固定", captcha_lengths)

    def captcha_skip():
        from app.gui import CaptchaDialog, _Answer
        from app.verify.fake import _placeholder_png

        box = _Answer()
        dlg = CaptchaDialog(win, {"task_id": "t2", "png": _placeholder_png(),
                                  "filename": "x.pdf", "hint": "", "timeout": 30}, box)
        root.update()
        dlg._skip()
        assert box.event.wait(2), "跳过没有唤醒等待方"
        assert box.value is None, "跳过应返回 None"
        return "点「跳过」返回 None（驱动会刷新验证码再问一次）"

    ok &= check("验证码弹窗：跳过", captcha_skip)

    def fields_flow():
        from app.gui import FieldsDialog, _Answer

        box = _Answer()
        dlg = FieldsDialog(win, {"filename": "票.xml", "preview": "识别到的内容",
                                 "missing": ["校验码后6位"], "initial": {}}, box)
        root.update()
        dlg.entries["invoice_number"].insert(0, "12345678")
        dlg.entries["invoice_code"].insert(0, "011002000311")
        dlg.entries["invoice_date"].insert(0, "2024-03-15")
        dlg.entries["check_code"].insert(0, "12345678901234567890")
        dlg._submit()
        assert box.event.wait(2), "保存没有唤醒等待方"
        data = box.value or {}
        assert data.get("invoice_number") == "12345678", f"号码没取到：{data}"
        assert data.get("check_code_last6") == "567890", \
            f"20 位校验码应自动带出后 6 位，实际 {data.get('check_code_last6')!r}"
        return "可保存，且 20 位校验码自动取后 6 位"

    ok &= check("字段补录弹窗", fields_flow)

    def fields_digital():
        from app.gui import FieldsDialog, _Answer

        box = _Answer()
        dlg = FieldsDialog(win, {"filename": "数电票.xml", "preview": "",
                                 "missing": ["开具金额"], "initial": {}}, box)
        root.update()
        dlg.entries["invoice_number"].insert(0, "24312000000012345678")
        dlg.entries["invoice_date"].insert(0, "2024-05-20")
        dlg.entries["amount"].insert(0, "1000.00")
        dlg.entries["invoice_code"].insert(0, "011002000311")   # 数电票本不该有代码
        dlg._submit()
        assert box.event.wait(2), "保存没有唤醒等待方"
        data = box.value or {}
        assert "invoice_code" not in data, "数电票应自动去掉发票代码，避免填错框"
        assert data.get("amount") == "1000.00", f"金额没取到：{data}"
        return "数电票会自动去掉发票代码/校验码"

    ok &= check("补录弹窗：数电票分流", fields_digital)

    def stop_flag():
        win.ui.reset_stop()
        assert win.ui.should_stop() is False, "初始不该是停止状态"
        win.ui.request_stop()
        assert win.ui.should_stop() is True, "request_stop 没生效"
        win.ui.reset_stop()
        return "停止标志 置位/复位 正常（工作线程靠它判断是否中止）"

    ok &= check("停止信号", stop_flag)

    try:
        root.destroy()
    except Exception:
        pass
    return ok


# ---------------------------------------------------------------------------
#  8. 验证码识别
# ---------------------------------------------------------------------------
def check_captcha_ocr() -> bool:
    section("验证码识别")

    try:
        from app.captcha import ocr
    except Exception as exc:
        record("SKIP", "验证码识别", f"无法导入 app.captcha.ocr：{exc}")
        return False

    ok = True

    def blue_separation():
        """颜色分离：平台会指定「只填图片中蓝/红色文字」。

        造一张左蓝右红的图，分离后应只剩指定的那部分（且转成黑字白底）。
        """
        import io as _io

        from PIL import Image

        img = Image.new("RGB", (40, 20), (255, 255, 255))
        for x in range(2, 12):
            for y in range(2, 12):
                img.putpixel((x, y), (0, 0, 255))      # 蓝块
        for x in range(20, 30):
            for y in range(2, 12):
                img.putpixel((x, y), (255, 0, 0))      # 红块
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()

        blue = ocr.color_filter(png, "blue")
        assert blue, "应该能从图里分离出蓝色像素"
        colors = set(Image.open(_io.BytesIO(blue)).convert("RGB").getdata())
        assert (0, 0, 0) in colors, "蓝色像素应被转成黑色（便于 OCR）"
        assert (255, 0, 0) not in colors, "红色像素不该被保留"

        red = ocr.color_filter(png, "red")
        assert red, "应该能从图里分离出红色像素"
        colors_r = set(Image.open(_io.BytesIO(red)).convert("RGB").getdata())
        assert (0, 0, 0) in colors_r and (0, 0, 255) not in colors_r, \
            "按红色分离时应保留红块、丢弃蓝块"

        # 全灰的图不该被判成任一颜色
        grey = Image.new("RGB", (40, 20), (128, 128, 128))
        gbuf = _io.BytesIO()
        grey.save(gbuf, format="PNG")
        assert ocr.color_filter(gbuf.getvalue(), "blue") is None, \
            "没有蓝色像素时应返回 None，而不是给出一张空图"

        # 提示文字 → 颜色
        assert ocr.parse_color_hint("请输入验证码图片中蓝色文字") == "blue"
        assert ocr.parse_color_hint("请输入验证码图片中红色文字") == "red"
        assert ocr.parse_color_hint("请输入验证码") is None
        return "蓝/红分离各自正确，无该颜色时返回 None，提示文字解析正确"

    ok &= check("颜色分离（按提示取色）", blue_separation)

    if not ocr.available():
        record("SKIP", "ddddocr 初始化",
               "不可用 —— 验证码将全部转人工输入，功能不受影响")
        return ok

    record("PASS", "ddddocr 初始化", f"引擎：{ocr.engine_name()}")

    def blank_image():
        from app.verify.fake import _placeholder_png

        result = ocr.solve(_placeholder_png())
        # 1x1 纯色图不可能识别出 4 位验证码，返回 None 才是正确行为
        assert result is None, f"空白图不该识别出内容，实际返回 {result!r}"
        return "对无效图片返回 None（不会把脏结果填进表单）"

    check("无效图片处理", blank_image)
    return ok


# ---------------------------------------------------------------------------
#  9. 浏览器
# ---------------------------------------------------------------------------
def mock_check_page() -> str:
    """一个按**实测真实结构**搭的本地模拟页。

    类名和真实的查验平台一致（TDesign），还带一个能用的日期面板，
    这样不联网就能把驱动的关键机制跑一遍：元素定位、动态第 4 字段、
    只读日期选择器的年/月/日点选、验证码、结论判定、PDF 导出。
    """
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (96, 36), (250, 250, 250)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>模拟查验页</title></head>
<body>
<form class="t-form">
  <div class="t-form__item t-form-item__fpdm">
    <div class="t-form__label"><span>发票代码</span></div>
    <input class="t-input__inner" placeholder="请输入">
  </div>
  <div class="t-form__item t-form-item__fphm">
    <div class="t-form__label"><span>*发票号码</span></div>
    <input class="t-input__inner" placeholder="请输入">
  </div>
  <div class="t-form__item t-form-item__kprq">
    <div class="t-form__label"><span>*开票日期</span></div>
    <input class="t-input__inner" placeholder="YYYYMMDD" readonly>
  </div>
  <div class="t-form__item t-form-item__kpje">
    <div class="t-form__label"><span id="vlabel">*校验码</span></div>
    <input class="t-input__inner" placeholder="请输入">
  </div>
  <div class="t-form__item t-form-item__yzm">
    <div class="t-form__label"><span>*验证码</span></div>
    <input class="t-input__inner" placeholder="请输入">
    <img src="data:image/png;base64,""" + b64 + """" width="120" height="50" alt="captcha">
    <div class="form-box-tip"><span class="form-box-tip__yzm">点击图片刷新</span></div>
  </div>
  <button type="submit" class="t-button t-button--theme-primary">查 验</button>
  <div id="result"></div>
</form>

<!-- 日期面板（真实平台是 TDesign 的只读日历，必须先点输入框再选年/月/日） -->
<div class="t-date-picker__header-controller-year"><input readonly placeholder="请选择"></div>
<div class="t-date-picker__header-controller-month"><input readonly placeholder="请选择"></div>
<div class="t-select-option">2023</div>
<div class="t-select-option">2024</div>
<div class="t-select-option">2月</div>
<div class="t-select-option">3月</div>
<div class="t-date-picker__cell">14</div>
<div class="t-date-picker__cell">15</div>

<script>
  var pickedYear = '', pickedMonth = '';
  document.querySelectorAll('.t-select-option').forEach(function (o) {
    o.addEventListener('click', function () {
      var t = o.textContent.trim();
      if (/^\\d{4}$/.test(t)) pickedYear = t;
      else if (t.endsWith('月')) pickedMonth = t;
    });
  });
  document.querySelectorAll('.t-date-picker__cell').forEach(function (c) {
    c.addEventListener('click', function () {
      var d = c.textContent.trim().padStart(2, '0');
      var m = pickedMonth.replace('月', '').padStart(2, '0');
      var inp = document.querySelector('.t-form-item__kprq input');
      inp.removeAttribute('readonly');
      inp.value = pickedYear + m + d;
      inp.setAttribute('readonly', 'readonly');
    });
  });
  document.querySelector('button[type=submit]').addEventListener('click', function (e) {
    e.preventDefault();
    var y = document.querySelector('.t-form-item__yzm input').value.trim();
    document.getElementById('result').textContent =
      (y === 'AB12') ? '查验成功，发票信息一致' : '验证码错误！';
  });
</script>
</body></html>"""


_LEGACY_INPUTS = {
    "kind": "legacy",
    "invoice_code": "011002000311",
    "invoice_number": "12345678",
    "invoice_date": "2024-03-15",
    "check_code_last6": "567890",
    "amount_excl_tax": "1000.00",
    "amount_total": "1130.00",
}


def mock_legacy_page() -> str:
    """按**实测的旧版页面结构**搭的模拟页（id 与真实站点完全一致）。

    把这次实测踩到的四个坑都复现出来，作为回归用例：
      1) 第 4 字段形态**异步切换**（号码失焦后才变成「价税合计」）
      2) 开票日期是带假占位符的输入框，键盘输入会被站点 JS 抹掉
      3) 结论放在自绘弹窗 #popup_message 里，不在整页文本里
      4) 「确定」是 input[type=button] 而不是 <button>，不关掉会挡住后续点击
    """
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 50), (245, 245, 245)).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>模拟旧版查验页</title></head>
<body>
<table>
  <tr><td>发票代码：</td><td><input type="text" id="fpdm"></td></tr>
  <tr><td>*发票号码：</td><td><input type="text" id="fphm" maxlength="20"></td></tr>
  <tr><td>*开票日期：</td><td><input type="text" id="kprq" maxlength="8"
        value="YYYYMMDD" style="color:#999999"></td></tr>
  <tr><td><span id="context">开具金额(不含税)：</span></td>
      <td><input type="text" id="kjje"></td></tr>
  <tr><td>*验证码：</td><td><input type="text" id="yzm">
      <img id="yzm_img" width="120" height="50"
           src="data:image/png;base64,""" + b64 + """" />
      <span>点击图片刷新</span><span>请输入验证码图片中蓝色文字</span></td></tr>
</table>
<input type="button" value="扫描" id="smcy">
<button id="checkfp">查 验</button>
<div id="popup_overlay" style="display:none"></div>
<div id="popup_container" style="display:none">
  <h1 id="popup_title">提示</h1>
  <div id="popup_message"></div>
  <input type="button" value=" 确定 " id="popup_ok">
</div>
<script>
  // 坑1：号码失焦后才切换第 4 字段形态（异步）
  document.getElementById('fphm').addEventListener('blur', function () {
    document.getElementById('context').textContent = '价税合计：';
  });
  // 坑2：站点 JS 会「抹掉不合法的输入」——逐字符敲的时候，每个中间状态
  //      都不合法，所以键盘输入永远填不进去；一次性写入合法值才留得住。
  var kprq = document.getElementById('kprq');
  kprq.addEventListener('input', function () {
    if (!/^\\d{8}$/.test(kprq.value)) { kprq.value = 'YYYYMMDD'; }
  });
  // 坑3：结论放在自绘弹窗里
  document.getElementById('checkfp').addEventListener('click', function () {
    var y = document.getElementById('yzm').value.trim();
    document.getElementById('popup_message').textContent =
      (y === 'AB12') ? '查验成功，发票信息一致' : '验证码错误!';
    document.getElementById('popup_container').style.display = '';
    document.getElementById('popup_overlay').style.display = '';
  });
  document.getElementById('popup_ok').addEventListener('click', function () {
    document.getElementById('popup_container').style.display = 'none';
    document.getElementById('popup_overlay').style.display = 'none';
  });
</script>
</body></html>"""


def check_legacy_mode() -> bool:
    section("旧版页面流程（模拟页）")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        record("SKIP", "旧版流程", "未安装 playwright")
        return True
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        record("SKIP", "旧版流程", "未安装 Pillow")
        return True

    from app.config import load_config
    from app.verify.playwright_driver import PlaywrightVerifier

    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="legacypage-"))
    target = tmpdir / "legacy.html"
    target.write_text(mock_legacy_page(), encoding="utf-8")

    cfg = load_config()
    cfg.data["verify"]["browser_channel"] = "msedge"
    cfg.data["platform"]["mode"] = "legacy"
    cfg.data["platform"]["home_url"] = target.as_uri()
    cfg.data["captcha"]["auto_ocr"] = False
    cfg.data["captcha"]["max_auto_attempts"] = 0
    cfg.data["captcha"]["manual_max_rounds"] = 1

    class Stub:
        def __init__(self):
            self.calls = 0

        def request(self, task_id, png, *, filename="", hint="", timeout=180.0):
            self.calls += 1
            return "ZZZZ"        # 故意错：验证「弹窗报错 → 关闭 → 重试」

    stub = Stub()
    verifier = PlaywrightVerifier(cfg, stub)
    state: dict = {}
    ok = True

    def start():
        verifier.start()
        state["page"] = verifier._ensure_page()
        return f"浏览器已启动（{verifier._channel_used}），模式 {verifier.mode}"

    if not check("启动浏览器（legacy）", start):
        try:
            verifier.close()
        except Exception:
            pass
        return False

    page = state["page"]

    def load_mock():
        page.goto(cfg.start_url, wait_until="load", timeout=30000)
        page.wait_for_timeout(500)
        return f"已加载模拟旧版页面（{cfg.start_url}）"

    ok &= check("加载模拟旧版页面", load_mock)

    def locate():
        # alert_ok 不参与：它只在弹窗弹出时才可见
        for name in ("invoice_code", "invoice_number", "invoice_date",
                     "value_label", "value_input", "captcha_input",
                     "captcha_image", "submit"):
            assert verifier._locator(page, name, wait_ms=2000) is not None, \
                f"按旧版选择器定位不到 {name}"
        return "八个元素全部按旧版 id 选择器定位成功"

    ok &= check("旧版元素定位", locate)

    def date_js_setter():
        """坑2：键盘输入会被站点 JS 抹掉，必须用原生 setter 写值。"""
        assert verifier._set_date(page, "2024-03-15"), "日期设置失败"
        got = page.locator("#kprq").input_value()
        assert got == "20240315", f"日期回读不对：{got!r}"
        return f"日期写入成功且没被站点 JS 抹掉：{got}"

    ok &= check("旧版日期填充（原生 setter）", date_js_setter)

    def label_switch():
        """坑1：形态异步切换，必须等切换完再取值。"""
        page.locator("#fphm").fill("26447000001819068870")
        page.keyboard.press("Tab")          # 失焦才触发站点切换
        page.wait_for_timeout(800)
        label = verifier._read_value_label(page, settle_ms=800)
        assert "价税合计" in label, f"标签应切换为价税合计，实际 {label!r}"

        value, what = verifier._pick_value(label, _LEGACY_INPUTS)
        assert what == "价税合计", f"应取价税合计，实际 {what}"
        assert value == "1130.00", f"价税合计应取 amount_total，实际 {value!r}"

        value2, what2 = verifier._pick_value("开具金额(不含税)：", _LEGACY_INPUTS)
        assert what2 == "开具金额(不含税)" and value2 == "1000.00", \
            f"不含税形态取值不对：{what2}/{value2}"
        return f"价税合计→{value}，不含税→{value2}，按标签分流正确"

    ok &= check("旧版第 4 字段按标签取值", label_switch)

    def popup_flow():
        """坑3+4：结论在自绘弹窗里，且「确定」是 input[type=button]。"""
        assert verifier._fill_field(page, "captcha_input", "ZZZZ")
        assert verifier._click_submit(page), "查验按钮点不动"
        status, summary, _body = verifier._wait_result(page, "")
        assert status == "captcha_wrong", \
            f"应从 #popup_message 读到「验证码错误」，实际 {status}：{summary}"
        assert verifier._dismiss_alert(page), "弹窗没关掉（会挡住后续点击）"
        assert verifier._read_alert_message(page) == "", "关闭后弹窗文字应该读不到了"
        return f"读到弹窗结论「{summary}」并能正确关闭"

    ok &= check("旧版弹窗结论文案", popup_flow)

    def full_legacy_flow():
        """旧版完整走一遍：填表 → 取码 → 提交 → 读弹窗结论。"""
        flow = PlaywrightVerifier(cfg, Stub())
        flow.start()
        try:
            outcome = flow.verify("legacy-full", dict(_LEGACY_INPUTS),
                                  filename="旧版票.pdf", want_pdf=False)
        finally:
            flow.close()
        assert outcome.status == "captcha_wrong", \
            f"完整流程应判 captcha_wrong，实际 {outcome.status}：{outcome.summary}"
        return f"端到端跑通：{outcome.status} / {outcome.summary}"

    # 先关掉上一个驱动：Playwright 同步 API 同线程不能有两个实例
    try:
        verifier.close()
    except Exception as exc:
        record("FAIL", "关闭驱动（legacy）", str(exc))
        ok = False

    ok &= check("旧版完整流程（端到端）", full_legacy_flow)

    try:
        target.unlink()
        tmpdir.rmdir()
    except OSError:
        pass
    return ok


def check_driver() -> bool:
    section("查验驱动（对着模拟页面演练）")

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        record("SKIP", "驱动演练", "未安装 playwright")
        return True

    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        record("SKIP", "驱动演练", "未安装 Pillow，无法构造模拟页面")
        return True

    from app.config import load_config
    from app.verify.base import classify
    from app.verify.playwright_driver import PlaywrightVerifier

    cfg = load_config()
    cfg.data["verify"]["browser_channel"] = "msedge"
    # 这套测试用的是**新版（SPA）结构**的模拟页，所以显式指定 spa 模式，
    # 否则会拿旧版选择器去匹配 SPA 的类名，必然定位失败。
    cfg.data["platform"]["mode"] = "spa"

    verifier = PlaywrightVerifier(cfg)
    state: dict = {}
    ok = True

    def start():
        verifier.start()
        state["page"] = verifier._ensure_page()
        return f"浏览器已启动，用的是 {verifier._channel_used}"

    if not check("启动浏览器", start):
        try:
            verifier.close()
        except Exception:
            pass
        return False

    page = state["page"]

    def load_mock():
        page.set_content(mock_check_page())
        page.wait_for_timeout(200)
        return "模拟查验页面已加载（类名与真实平台一致）"

    ok &= check("加载模拟页面", load_mock)

    def locate():
        for name in ("invoice_code", "invoice_number", "invoice_date",
                     "value_item", "value_input", "captcha_input",
                     "captcha_image", "captcha_refresh", "submit"):
            assert verifier._locator(page, name, wait_ms=2000) is not None, \
                f"按配置定位不到 {name}"
        return "九个元素全部按配置里的候选选择器定位成功（代码里无硬编码）"

    ok &= check("元素定位", locate)

    def fill_basic():
        assert verifier._fill_field(page, "invoice_code", "011002000311")
        assert verifier._fill_field(page, "invoice_number", "12345678")
        got = page.locator(".t-form-item__fphm input").input_value()
        assert got == "12345678", f"发票号码回读不对：{got!r}"
        return f"发票代码/号码填入并回读一致（号码={got}）"

    ok &= check("基础字段填充", fill_basic)

    def value_field_by_label():
        """第 4 字段的标签是动态的——必须按标签决定填什么值。

        这是核心适配点：实测平台会按输入变成「校验码」/「开具金额(不含税)」/「价税合计」。
        """
        page.evaluate("document.getElementById('vlabel').textContent = '*校验码'")
        okc, why = verifier._fill_value_field(page, _LEGACY_INPUTS)
        assert okc, why
        got = page.locator(".t-form-item__kpje input").input_value()
        assert got == "567890", f"校验码形态应填后6位，实际 {got!r}"

        page.evaluate("document.getElementById('vlabel').textContent = '*价税合计'")
        page.locator(".t-form-item__kpje input").fill("")
        okm, whym = verifier._fill_value_field(page, _LEGACY_INPUTS)
        assert okm, whym
        got2 = page.locator(".t-form-item__kpje input").input_value()
        assert got2 == "1130.00", f"价税合计形态应填含税总额，实际 {got2!r}"

        page.evaluate("document.getElementById('vlabel').textContent = '*开具金额(不含税)'")
        page.locator(".t-form-item__kpje input").fill("")
        okn, whyn = verifier._fill_value_field(page, _LEGACY_INPUTS)
        assert okn, whyn
        got3 = page.locator(".t-form-item__kpje input").input_value()
        assert got3 == "1000.00", f"不含税形态应填不含税金额，实际 {got3!r}"
        return "校验码→567890，价税合计→1130.00，不含税→1000.00，三种形态都对"

    ok &= check("第 4 字段按标签取值", value_field_by_label)

    def date_picker():
        """开票日期是只读日历，必须点选年 → 月 → 日。"""
        assert verifier._set_date(page, "2024-03-15"), "日期点选失败"
        got = page.locator(".t-form-item__kprq input").input_value()
        assert got == "20240315", f"日期回读不对：{got!r}"
        return f"只读日历点选成功：2024-03-15 → {got}"

    ok &= check("开票日期点选（只读日历）", date_picker)

    def captcha_image_bytes():
        img = verifier._locator(page, "captcha_image", wait_ms=2000)
        png = img.screenshot()
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "验证码截图不是 PNG"
        return f"验证码截图成功（{len(png)} 字节）"

    ok &= check("验证码截图", captcha_image_bytes)

    def wrong_captcha():
        baseline = verifier._body_text(page)
        assert verifier._fill_field(page, "captcha_input", "XXXX")
        verifier._click_submit(page)
        status, summary, _body = verifier._wait_result(page, baseline)
        assert status == "captcha_wrong", f"应判 captcha_wrong，实际 {status}"
        return f"验证码错误被识别：{summary}"

    ok &= check("错误验证码判定", wrong_captcha)

    def good_captcha():
        page.set_content(mock_check_page())
        page.wait_for_timeout(150)
        baseline = verifier._body_text(page)
        assert verifier._fill_field(page, "captcha_input", "AB12")
        assert verifier._click_submit(page), "查验按钮点不动"
        status, summary, _body = verifier._wait_result(page, baseline)
        assert status == "ok", f"应判 ok，实际 {status}"
        return f"正确验证码 → 结论「一致」：{summary}"

    ok &= check("正确验证码判定", good_captcha)

    def export_pdf():
        data = verifier._capture_pdf(page)
        assert data, "PDF 导出返回空"
        assert data[:5] == b"%PDF-", f"不是合法 PDF：{data[:8]!r}"
        return f"结果页导出 PDF 成功（{len(data)} 字节，%PDF- 头）"

    ok &= check("导出查验结果 PDF", export_pdf)

    def keyword_classification():
        cases = [
            ("查验成功，发票信息一致", "ok"),
            ("该发票信息不一致，请核实", "mismatch"),
            ("查无此票", "not_found"),
            ("验证码错误！", "captcha_wrong"),
            ("超过该张发票当日查验次数", "rate_limited"),
            ("这是一段与结论无关的文字", "unknown"),
        ]
        for text, expect in cases:
            got = classify(text, cfg)
            assert got == expect, f"{text!r} 应判 {expect}，实际 {got}"
        return f"{len(cases)} 种平台文案全部归类正确"

    ok &= check("结论文案归类", keyword_classification)

    def api_payload():
        """结论优先取自 queryFpcyxx 接口返回（比页面文本更早更准）。"""
        code, msg = verifier._api_message(
            {"Response": {"Data": {"CyjgDm": "97", "CyjgMsg": "验证码错误！"}}})
        assert code == "97" and msg == "验证码错误！", f"接口字段解析不对：{code}/{msg}"
        assert classify(msg, cfg) == "captcha_wrong", "接口消息应能判为验证码错误"
        return "接口 JSON 解析 + 归类正确（实测结构：Response.Data.CyjgDm/CyjgMsg）"

    ok &= check("查验接口返回解析", api_payload)

    def baseline_guard():
        text = "查验说明：查验成功后请核对发票信息"
        assert classify(text, cfg, baseline=text) == "unknown", \
            "帮助文字里的关键词被误判成了结论"
        return "提交前已存在关键词不会误判（baseline 防护生效）"

    ok &= check("结果误判防护", baseline_guard)

    def missing_inputs():
        from app.verify.playwright_driver import _missing_inputs

        bad = _missing_inputs({"invoice_number": "1"})
        assert "invoice_date" in bad, f"应报缺开票日期：{bad}"
        assert not _missing_inputs(_LEGACY_INPUTS), "完整入参不该报缺"
        return f"缺项检查正常：{bad}"

    ok &= check("入参完整性检查", missing_inputs)

    def full_verify_flow():
        """完整跑一遍 _verify_inner（对着本地模拟页）。

        回归用例：曾经驱动里写的是 `from .. import captcha` + `captcha.available()`，
        但 app/captcha/__init__.py 只写了文档字符串、没把 ocr 的接口转出来，
        一跑到真实查验就 `AttributeError: module 'app.captcha' has no attribute
        'available'`。当时的驱动测试只逐个调用子方法、没跑完整流程，所以漏掉了。

        这个测试把整条链路走通：打开页面 → 填表 → 点选日期 → 取验证码
        → 提交 → 判定结论 → 导出 PDF。
        """
        import tempfile
        from pathlib import Path

        tmpdir = Path(tempfile.mkdtemp(prefix="mockpage-"))
        target = tmpdir / "mock.html"
        target.write_text(mock_check_page(), encoding="utf-8")

        flow_cfg = load_config()
        flow_cfg.data["verify"]["browser_channel"] = "msedge"
        flow_cfg.data["platform"]["mode"] = "spa"
        # 两个都要改：spa 模式下 start_url 取的是 spa_url，
        # 只改 home_url 会打到真实站点上去。
        flow_cfg.data["platform"]["home_url"] = target.as_uri()
        flow_cfg.data["platform"]["spa_url"] = target.as_uri()
        # 让 available() 真的被调用（这是出过问题的那一行），
        # 但把自动次数设成 0，直接走人工分支，测试才稳定快速。
        flow_cfg.data["captcha"]["auto_ocr"] = True
        flow_cfg.data["captcha"]["max_auto_attempts"] = 0
        flow_cfg.data["platform"]["selectors"]["captcha_image"] = ["form.t-form img"]

        class StubPrompter:
            def __init__(self):
                self.calls = 0

            def request(self, task_id, png, *, filename="", hint="", timeout=180.0):
                self.calls += 1
                return "AB12"

        stub = StubPrompter()
        flow = PlaywrightVerifier(flow_cfg, stub)
        flow.start()
        try:
            outcome = flow.verify("full-flow", dict(_LEGACY_INPUTS),
                                  filename="测试票.pdf", invoice_hint="h",
                                  want_pdf=True)
        finally:
            flow.close()
            try:
                target.unlink()
                tmpdir.rmdir()
            except OSError:
                pass

        assert outcome.status == "ok", \
            f"完整流程应判一致，实际 {outcome.status}：{outcome.summary}"
        assert stub.calls == 1, f"应调用人工验证码 1 次，实际 {stub.calls}"
        assert outcome.captcha_source == "manual", \
            f"验证码来源应为 manual，实际 {outcome.captcha_source}"
        assert outcome.pdf_bytes and outcome.pdf_bytes[:5] == b"%PDF-", \
            "完整流程没有产出合法 PDF"
        return (f"{outcome.status}，验证码来源 {outcome.captcha_source}，"
                f"PDF {len(outcome.pdf_bytes)} 字节")

    # 先把上一个驱动关干净：Playwright 的同步 API 在同一线程里
    # 不能同时有两个实例（会报 "Sync API inside the asyncio loop"）
    try:
        verifier.close()
    except Exception as exc:
        record("FAIL", "关闭驱动", f"关闭驱动报错：{exc}")
        ok = False

    ok &= check("完整查验流程（端到端）", full_verify_flow)
    return ok


# ---------------------------------------------------------------------------
#  主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="发票查验程序自检")
    parser.add_argument("--browser", action="store_true",
                        help="额外测试浏览器能否启动（会真的开一次浏览器）")
    args = parser.parse_args()

    print("=" * 68)
    print("  发票批量查验 —— 自检")
    print(f"  Python {sys.version.split()[0]}  ·  {sys.platform}")
    print("=" * 68)

    with tempfile.TemporaryDirectory(prefix="invcheck-") as tmpdir:
        tmp = Path(tmpdir)
        check_dependencies()
        check_config(tmp)
        check_field_parsing()
        check_extraction(tmp)
        check_end_to_end(tmp)
        check_database(tmp)

    check_gui()
    check_captcha_ocr()
    if args.browser:
        check_legacy_mode()
        check_driver()
    else:
        section("旧版页面流程（模拟页）")
        record("SKIP", "旧版流程", "未指定 --browser，跳过")
        section("查验驱动（对着模拟页面演练）")
        record("SKIP", "驱动演练", "未指定 --browser，跳过（加上它可完整验证驱动机制）")

    passed = sum(1 for lvl, _, _ in _RESULTS if lvl == "PASS")
    failed = [(n, d) for lvl, n, d in _RESULTS if lvl == "FAIL"]
    skipped = sum(1 for lvl, _, _ in _RESULTS if lvl == "SKIP")

    print()
    print("=" * 68)
    print(f"  通过 {passed} · 失败 {len(failed)} · 跳过 {skipped}")
    if failed:
        print()
        print("  失败项：")
        for name, detail in failed:
            print(f"    X {name}")
            print(f"      {detail}")
    print("=" * 68)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
