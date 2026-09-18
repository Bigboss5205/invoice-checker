"""批处理编排：扫目录 → 抽字段 → 查验 → 存「-已查验.pdf」。

跑在工作线程里，通过 ``UiBridge`` 和界面通信（日志、进度、弹窗提问）。
这样编排逻辑完全不认识 Tkinter，命令行或测试里换一个 bridge 就能跑。

为什么不做成「文件一放进去就自动处理」的常驻监听
-----------------------------------------------
桌面版的模型是「双击 → 跑一遍 → 结束」，用户对「什么时候开始」有完全的掌控。
常驻监听会带来一堆额外问题（进程残留、重复处理、用户以为关了其实没关），
对这个使用场景不划算。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import extract as extract_mod
from .db import Database
from .extract import ExtractionError, ExtractionUnavailable
from .verify.base import VerifyOutcome, label_for

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  与界面的契约
# ---------------------------------------------------------------------------
class UiBridge(Protocol):
    """界面需要提供的能力。GUI 实现它；测试时可以给一个纯打印的实现。"""

    def request(self, task_id: str, png: bytes, *, filename: str = "",
                hint: str = "", timeout: float = 180.0) -> str | None:
        """要一个验证码（同时满足驱动需要的 Prompter 协议）。"""

    def ask_fields(self, *, filename: str, preview: str, missing: list[str],
                   initial: dict[str, Any]) -> dict[str, Any] | None:
        """字段抽不全时请用户手工补录；返回 None 表示放弃这张。"""

    def log(self, message: str) -> None:
        """写一行日志到界面。"""

    def progress(self, done: int, total: int, current: str) -> None:
        """更新进度。"""

    def file_done(self, result: "FileResult") -> None:
        """一张票处理完了，界面可以立刻插一行结果。"""

    def should_stop(self) -> bool:
        """用户是否点了停止。"""


class NullUi:
    """不做任何界面交互的 bridge：自动识别失败就跳过，字段不全也跳过。"""

    def request(self, task_id, png, *, filename="", hint="", timeout=180.0):
        return None

    def ask_fields(self, *, filename, preview, missing, initial):
        return None

    def log(self, message):
        print(message)

    def progress(self, done, total, current):
        pass

    def file_done(self, result):
        print(f"  [{result.label}] {result.path.name} — {result.summary}")

    def should_stop(self):
        return False


# ---------------------------------------------------------------------------
#  结果
# ---------------------------------------------------------------------------
@dataclass
class FileResult:
    path: Path
    status: str                      # ok / mismatch / not_found / error / skipped ...
    label: str
    summary: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    pdf_path: Path | None = None
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class RunReport:
    total: int = 0
    results: list[FileResult] = field(default_factory=list)
    stopped: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def count(self, *statuses: str) -> int:
        return sum(1 for r in self.results if r.status in statuses)

    def summary_line(self) -> str:
        parts = [f"共 {len(self.results)} 张"]
        for status, name in (("ok", "一致"), ("mismatch", "不一致"),
                             ("not_found", "查无此票"), ("captcha_wrong", "验证码未过"),
                             ("rate_limited", "被限流"), ("unknown", "结果未知"),
                             ("error", "出错"), ("skipped", "跳过")):
            n = self.count(status)
            if n:
                parts.append(f"{name} {n}")
        if self.stopped:
            parts.append("（已中止）")
        return "，".join(parts)


# ---------------------------------------------------------------------------
#  编排
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, cfg, ui: UiBridge | None = None):
        self.cfg = cfg
        self.ui: UiBridge = ui or NullUi()

        self._stop = threading.Event()
        self._last_verify_at = 0.0
        self._consecutive_failures = 0
        self.verifier = None

        # 记录库延迟到真正要写的时候才打开：
        # scan() 这类只读操作不该占着 SQLite 文件句柄，
        # 否则用户移动/删除发票目录时会遇到「文件被占用」。
        self._db: Database | None = None
        self._db_failed = False

    def _say(self, message: str) -> None:
        """同时写界面和日志文件。

        打包成窗口程序后没有控制台，日志文件是出问题时唯一能带走的证据。
        所以界面上的每一条进展都要在日志里有对应记录——
        否则「跑完了一次，日志是空的」，用户拿不到任何排查线索。
        """
        log.info("%s", message)
        self.ui.log(message)

    def _get_db(self) -> Database | None:
        """按需打开记录库。打不开就静默降级为「不记录历史」。"""
        if self._db is None and not self._db_failed:
            try:
                self._db = Database(self.cfg.path("db"))
            except Exception as exc:
                self._db_failed = True
                log.warning("打不开记录数据库，本次不记录历史：%s", exc)
        return self._db

    def close(self) -> None:
        """释放记录库句柄。可重复调用。"""
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    # -- 控制 ---------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ==================================================================
    #  扫描
    # ==================================================================
    def scan(self) -> list[Path]:
        """列出待处理的发票文件。

        两个容易踩的点：
        1. **不能把程序自己的产物当发票** —— 结果 PDF 和原发票在同一目录，
           文件名又只差一个后缀，必须显式排除，否则会自我循环。
        2. 跳过已经有「-已查验」的文件，重复运行不会白跑一遍。
        """
        workdir = self.cfg.workdir
        if not workdir.is_dir():
            self._say(f"✗ 发票目录不存在：{workdir}")
            return []

        exts = {str(e).lower() for e in (self.cfg.get("scan.extensions") or [])}
        suffix = str(self.cfg.get("output.suffix", "-已查验"))
        recursive = bool(self.cfg.get("scan.recursive", False))
        skip_verified = bool(self.cfg.get("scan.skip_verified", True))
        out_dir = self.cfg.out_dir

        own_files = {self.cfg.path("db").name, self.cfg.path("log").name,
                     "config.yaml", "config.example.yaml"}

        found: list[Path] = []
        iterator = workdir.rglob("*") if recursive else workdir.glob("*")
        for path in sorted(iterator):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue

            name = path.name
            if name.startswith(".") or name.startswith("~$"):
                continue
            if name in own_files:
                continue
            if path.suffix.lower() not in exts:
                continue
            # 自己的产物：<原名>-已查验.pdf
            if path.stem.endswith(suffix):
                continue
            if skip_verified and (out_dir / f"{path.stem}{suffix}.pdf").exists():
                log.debug("已有查验结果，跳过：%s", name)
                continue
            if not self._readable(path):
                self._say(f"· 跳过（文件正被占用或无法读取）：{name}")
                continue

            found.append(path)

        return found

    @staticmethod
    def _readable(path: Path) -> bool:
        """文件是否可读。正在被拷贝/打开的文件会在这一步被挡掉。"""
        try:
            with path.open("rb") as fh:
                fh.read(1)
            return True
        except OSError:
            return False

    # ==================================================================
    #  主流程
    # ==================================================================
    def run(self) -> RunReport:
        report = RunReport(started_at=time.time())

        # 先把「这次是在什么环境下跑的」记进日志：排查问题时这几乎总是第一个要问的
        self._say(f"发票目录：{self.cfg.workdir}")
        self._say(f"配置：{self.cfg.source or '内置默认值'}　"
                  f"驱动：{self.cfg.get('verify.driver')}　"
                  f"间隔：{self.cfg.get('verify.min_interval_seconds')} 秒　"
                  f"验证码自动识别：{'开' if self.cfg.get('captcha.auto_ocr') else '关'}")

        files = self.scan()
        report.total = len(files)
        if not files:
            self._say("没有找到需要查验的发票。")
            self.ui.progress(0, 0, "")
            report.finished_at = time.time()
            self.close()
            return report

        self._say(f"发现 {len(files)} 个待查验文件。")
        try:
            self._start_verifier()
        except Exception as exc:
            self._say(f"✗ 浏览器启动失败：{exc}")
            report.finished_at = time.time()
            self.close()
            return report

        try:
            for index, path in enumerate(files, 1):
                if self._stop.is_set() or self.ui.should_stop():
                    report.stopped = True
                    self._say("已按你的要求停止。")
                    break

                self.ui.progress(index - 1, len(files), path.name)
                result = self._process(path)
                report.results.append(result)
                self.ui.file_done(result)
                self.ui.progress(index, len(files), path.name)

            report.finished_at = time.time()
            self._say(f"完成：{report.summary_line()}，用时 {report.elapsed:.0f} 秒。")
            return report
        finally:
            # 无论中途出什么岔子，浏览器和记录库的句柄都必须放掉，
            # 否则用户会遇到「文件正被另一个程序占用」，连目录都移不动。
            self._close_verifier()
            self.close()

    def _start_verifier(self) -> None:
        cfg = self.cfg
        if not cfg.get("verify.enabled", True):
            log.warning("verify.enabled=false：只抽字段，不联网查验")
            self.verifier = None
            return

        driver = str(cfg.get("verify.driver", "playwright"))
        if driver == "fake":
            from .verify.fake import FakeVerifier

            self.verifier = FakeVerifier(cfg, self.ui)
            self._say("· 使用离线假驱动（结果是模拟的，不来自真实平台）")
            return

        from .verify.playwright_driver import PlaywrightVerifier

        verifier = PlaywrightVerifier(cfg, self.ui)
        verifier.set_debug_dir(cfg.workdir / "发票查验出错现场")
        verifier.start()
        self.verifier = verifier

    def _close_verifier(self) -> None:
        if self.verifier is not None:
            try:
                self.verifier.close()
            except Exception as exc:
                log.debug("关闭查验驱动失败：%s", exc)
            self.verifier = None

    # ==================================================================
    #  单张处理
    # ==================================================================
    def _process(self, path: Path) -> FileResult:
        started = time.time()
        self._say(f"→ {path.name}")

        # ---- 1. 抽字段 ----
        fields: dict[str, Any] = {}
        preview = ""
        try:
            extraction = extract_mod.extract_invoice(path, self.cfg)
            fields = dict(extraction.fields)
            preview = extraction.text or ""
            for warn in extraction.warnings:
                self._say(f"    · {warn}")
        except ExtractionUnavailable as exc:
            self._say(f"    · 需要人工补录：{exc}")
            fields = self._prompt_fields(path, str(exc), [], {}, preview) or {}
        except ExtractionError as exc:
            self._say(f"    ✗ 无法解析：{exc}")
            fields = self._prompt_fields(path, str(exc), [], {}, preview) or {}
        except Exception as exc:
            log.exception("抽取 %s 时出现未预期错误", path.name)
            self._say(f"    ✗ 解析出错：{type(exc).__name__}: {exc}")
            fields = self._prompt_fields(
                path, f"{type(exc).__name__}: {exc}", [], {}, preview) or {}

        if fields:
            # 把识别到的关键字段记进日志。填表失败时，第一件要确认的就是
            # 「到底是没识别出字段，还是识别错了」——没有这行就只能猜。
            self._say(
                "    识别到："
                f"号码={fields.get('invoice_number')} "
                f"日期={fields.get('invoice_date')} "
                f"代码={fields.get('invoice_code')} "
                f"校验码后6位={fields.get('check_code_last6')} "
                f"不含税={fields.get('amount_excl_tax')} "
                f"价税合计={fields.get('amount_total')}"
            )

        if not fields:
            return self._finish(path, "skipped", "已跳过", "没有可用字段", {}, started)

        # ---- 2. 字段齐不齐 ----
        missing = extract_mod.F.missing_required(fields)
        if missing:
            self._say(f"    · 缺少字段：{'、'.join(missing)}")
            patched = self._prompt_fields(path, "缺少必要字段", missing, fields, preview)
            if not patched:
                return self._finish(path, "skipped", "已跳过",
                                    f"缺少字段：{'、'.join(missing)}", fields, started)
            fields = patched
            missing = extract_mod.F.missing_required(fields)
            if missing:
                return self._finish(path, "error", "出错",
                                    f"补录后仍缺少：{'、'.join(missing)}", fields, started)

        # ---- 3. 查验 ----
        if not self.cfg.get("verify.enabled", True):
            return self._finish(path, "skipped", "未查验",
                                "verify.enabled=false", fields, started)

        if self.verifier is None:
            return self._finish(path, "error", "出错", "查验驱动未就绪", fields, started)

        inputs = extract_mod.F.verification_inputs(fields)
        hint = f"{fields.get('invoice_number') or '?'} · {fields.get('invoice_date') or ''}"

        outcome = self._verify_with_retry(path, inputs, hint)
        # ---- 4. 输出 ----
        pdf_path = self._write_output(path, outcome)
        if pdf_path is not None:
            self._say(f"    ✓ 已生成：{pdf_path.name}")

        label = label_for(outcome.status, self.cfg)
        self._say(f"    → {label}：{outcome.summary}")

        result = self._finish(path, outcome.status, label, outcome.summary,
                              fields, started, pdf_path=pdf_path)
        self._record(path, result, outcome)
        return result

    def _verify_with_retry(self, path: Path, inputs: dict[str, Any],
                           hint: str) -> VerifyOutcome:
        max_attempts = max(1, int(self.cfg.get("verify.max_attempts", 2)))
        outcome = VerifyOutcome(status="error", summary="未执行")

        for attempt in range(1, max_attempts + 1):
            self._respect_rate_limit()

            try:
                outcome = self.verifier.verify(
                    task_id=f"{path.stem[:24]}",
                    inputs=inputs,
                    filename=path.name,
                    invoice_hint=hint,
                    want_pdf=bool(self.cfg.get("output.pdf", True)),
                )
            except Exception as exc:
                log.exception("查验 %s 抛出异常", path.name)
                outcome = VerifyOutcome(status="error",
                                        summary=f"{type(exc).__name__}: {exc}")
            finally:
                self._last_verify_at = time.time()

            if not outcome.is_retryable or attempt >= max_attempts:
                break

            self._say(f"    · 第 {attempt} 次未成功（{outcome.summary}），准备重试…")
            if self._stop.wait(3):
                break

        self._track_failures(outcome)
        return outcome

    def _respect_rate_limit(self) -> None:
        interval = float(self.cfg.get("verify.min_interval_seconds", 8) or 0)
        if interval <= 0 or self._last_verify_at <= 0:
            return
        wait = interval - (time.time() - self._last_verify_at)
        if wait > 0:
            log.debug("限速：等待 %.1f 秒", wait)
            self._stop.wait(wait)

    def _track_failures(self, outcome: VerifyOutcome) -> None:
        # 人工主动跳过不算失败，否则连跳几张就把队列停了
        if outcome.status in {"ok", "skipped"}:
            self._consecutive_failures = 0
            return

        self._consecutive_failures += 1
        limit = int(self.cfg.get("verify.stop_on_consecutive_failures", 0) or 0)
        if limit > 0 and self._consecutive_failures >= limit:
            self._say(f"⚠ 连续 {self._consecutive_failures} 张未得到一致结论，已自动停止。"
                        "请确认平台是否可正常访问。")
            self.stop()

    def _prompt_fields(self, path: Path, reason: str, missing: list[str],
                       initial: dict[str, Any], preview: str = "") -> dict[str, Any] | None:
        """请界面弹窗让用户手工录入字段。

        `preview` 由调用方传入（抽取到的原始文本），这里不重新解析文件——
        重复解析既有性能代价，也可能再次抛同样的异常。
        """
        try:
            return self.ui.ask_fields(filename=path.name, preview=preview or reason,
                                      missing=list(missing),
                                      initial=dict(initial or {}))
        except Exception as exc:
            log.warning("手工补录弹窗出错：%s", exc)
            return None

    # ==================================================================
    #  输出
    # ==================================================================
    def _write_output(self, src: Path, outcome: VerifyOutcome) -> Path | None:
        if not self.cfg.get("output.pdf", True) or not outcome.pdf_bytes:
            return None

        try:
            out_dir = self.cfg.out_dir
            out_dir.mkdir(parents=True, exist_ok=True)

            suffix = str(self.cfg.get("output.suffix", "-已查验"))
            target = out_dir / f"{src.stem}{suffix}.pdf"

            if target.exists() and not self.cfg.get("output.overwrite", True):
                n = 2
                while True:
                    candidate = out_dir / f"{src.stem}{suffix}_{n}.pdf"
                    if not candidate.exists():
                        target = candidate
                        break
                    n += 1

            target.write_bytes(outcome.pdf_bytes)

            if self.cfg.get("output.record_txt", False):
                record = target.with_suffix(".txt")
                record.write_text(
                    f"文件：{src.name}\n结论：{label_for(outcome.status, self.cfg)}\n"
                    f"摘要：{outcome.summary}\n验证码来源：{outcome.captcha_source}\n\n"
                    f"{outcome.detail}\n",
                    encoding="utf-8",
                )
            return target
        except OSError as exc:
            self._say(f"    ✗ 保存结果 PDF 失败：{exc}")
            return None

    def _record(self, path: Path, result: FileResult, outcome: VerifyOutcome) -> None:
        """把结果写进 SQLite，供后续查阅。失败不影响主流程。"""
        db = self._get_db()
        if db is None:
            return
        try:
            task_id = db.add_task(
                filename=path.name, source_path=str(path),
                file_size=path.stat().st_size if path.exists() else 0,
                status="done" if outcome.status != "error" else "error",
            )
            db.update_task(
                task_id,
                invoice_code=result.fields.get("invoice_code"),
                invoice_number=result.fields.get("invoice_number"),
                invoice_date=result.fields.get("invoice_date"),
                check_code_last6=result.fields.get("check_code_last6"),
                amount=(result.fields.get("amount_excl_tax")
                        or result.fields.get("amount_total")),
                seller_name=result.fields.get("seller_name"),
                invoice_kind=result.fields.get("invoice_kind"),
                verify_status=outcome.status,
                verify_label=result.label,
                verify_summary=outcome.summary,
                verify_detail=(outcome.detail or "")[:20000],
                captcha_source=outcome.captcha_source,
                captcha_attempts=outcome.captcha_attempts,
                stored_path=str(result.pdf_path) if result.pdf_path else None,
                error=outcome.summary if outcome.status == "error" else None,
                verified_at=time.time(),
            )
        except Exception as exc:
            log.debug("写入记录失败（忽略）：%s", exc)

    def _finish(self, path: Path, status: str, label: str, summary: str,
                fields: dict[str, Any], started: float,
                pdf_path: Path | None = None) -> FileResult:
        return FileResult(path=path, status=status, label=label, summary=summary,
                          fields=dict(fields or {}), pdf_path=pdf_path,
                          elapsed=time.time() - started)
