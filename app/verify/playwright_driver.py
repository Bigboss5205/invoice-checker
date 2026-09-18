"""真实查验驱动：Playwright 驱动浏览器，在税局查验平台上完成一次查验并导出结果 PDF。

本文件的选择器和流程都是**对着真实站点实测**出来的（2026-09），不是推测。

平台结构要点
------------
1. **入口必须是根地址** ``https://inv-veri.chinatax.gov.cn/fpcygzfw/``。
   直接访问 ``/national-invoice-check`` 路由会让懒加载 chunk 拿回 index.html
   （服务器对不存在的资源返回 200 + HTML），浏览器当 JS 解析报
   ``Unexpected token '<'``，整页白屏。从根进入由 SPA 自己路由才正常。

2. **表单是 TDesign，类名语义化且稳定**：
   ``.t-form-item__fpdm`` 发票代码 / ``__fphm`` 发票号码 / ``__kprq`` 开票日期 /
   ``__kpje`` 第4字段（动态）/ ``__yzm`` 验证码。

3. **第 4 字段的标签是动态的**：随输入变成「开具金额(不含税)」「校验码」「价税合计」。
   所以先读标签再决定填什么值——不能写死。

4. **开票日期是 readonly 的日期选择器**，填不进去，必须走日历面板：
   点输入框 → 选年 → 选月 → 点日。

5. **结果同时有两个来源**：页面文本，以及 ``queryFpcyxx`` 接口返回的 JSON
   （``{"Response":{"Data":{"CyjgDm":"97","CyjgMsg":"验证码错误！"}}}``）。
   接口返回更早也更准，所以优先用它分类，页面文本兜底。

6. **验证码是按颜色筛选的**（提示「请输入验证码图片中蓝色文字」），
   且字符混有中文与字母，自动识别成功率有限——人工输入是可靠路径。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..captcha import ocr as captcha_ocr
from .base import BaseVerifier, VerifyOutcome, classify

log = logging.getLogger(__name__)

_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
]

# 查验接口（跨域到 dppt.beijing.chinatax.gov.cn:8443），用来拿结构化结果
_QUERY_API_MARK = "queryFpcyxx"

# 实测：平台自己返回的结果码 97 = 验证码错误
_CODE_CAPTCHA_WRONG = "97"

# SPA 渲染失败时，整页重新导航的重试次数
_RENDER_RETRIES = 3


class PlaywrightVerifier(BaseVerifier):
    name = "playwright"

    def __init__(self, cfg, prompter=None):
        super().__init__(cfg, prompter)

        self.home_url = str(cfg.get("platform.home_url"))
        self.headless = bool(cfg.get("verify.headless", True))
        self.channel = str(cfg.get("verify.browser_channel", "msedge") or "").strip()
        self.nav_timeout = int(cfg.get("verify.nav_timeout_ms", 45000))
        self.result_timeout = int(cfg.get("verify.result_timeout_ms", 30000))

        self.auto_ocr = bool(cfg.get("captcha.auto_ocr", True))
        self.max_auto = int(cfg.get("captcha.max_auto_attempts", 8))
        self.max_manual = int(cfg.get("captcha.manual_max_rounds", 3))

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._debug_dir: Path | None = None
        self._channel_used = ""

        self._query_response = None      # 最近一次 queryFpcyxx 的响应对象
        self._query_seen = 0             # 已消费到第几次

    # ==================================================================
    #  生命周期
    # ==================================================================
    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "未安装 playwright。免安装版 exe 应当已内置；"
                "源码运行请执行：pip install playwright"
            ) from exc

        self._pw = sync_playwright().start()
        self._browser = self._launch(self._pw)
        self._context = self._browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1100},
            user_agent=_CHROME_UA,
            ignore_https_errors=True,
        )
        self._context.set_default_timeout(self.nav_timeout)
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self._page = self._context.new_page()
        self._page.on("response", self._on_response)
        self._install_page_logging(self._page)
        log.info("浏览器就绪（%s，headless=%s）", self._channel_used, self.headless)

    def _install_page_logging(self, page) -> None:
        """把页面里的 JS 异常、控制台报错、加载失败的资源记进日志。

        排查「页面打开了、标题也对，但内容没渲染出来」这类问题时，
        这是唯一能看清原因的地方——否则只能看到一句「找不到输入框」。
        """
        try:
            page.on("pageerror", self._on_page_error)
            page.on("console", self._on_console)
            page.on("requestfailed", self._on_request_failed)
        except Exception as exc:
            log.debug("安装页面日志监听失败：%s", exc)

    @staticmethod
    def _on_page_error(error) -> None:
        log.warning("页面 JS 异常：%s", str(error)[:400])

    @staticmethod
    def _on_console(message) -> None:
        try:
            if message.type in ("error", "warning"):
                log.warning("页面控制台[%s]：%s", message.type, str(message.text)[:400])
        except Exception:
            pass

    @staticmethod
    def _on_request_failed(request) -> None:
        try:
            log.warning("资源加载失败：%s → %s",
                        str(request.url)[:140], request.failure)
        except Exception:
            pass

    def _launch(self, pw):
        """依次尝试 msedge / chrome / 内置 chromium，用第一个能起来的。"""
        order: list[str | None] = []
        if self.channel and self.channel != "auto":
            order.append(self.channel)
        for extra in ("msedge", "chrome", None):
            if extra not in order:
                order.append(extra)

        errors: list[str] = []
        for channel in order:
            label = channel or "chromium(内置)"
            try:
                kwargs: dict[str, Any] = {"headless": self.headless, "args": _LAUNCH_ARGS}
                if channel:
                    kwargs["channel"] = channel
                browser = pw.chromium.launch(**kwargs)
                self._channel_used = label
                if channel != self.channel:
                    log.info("配置的浏览器不可用，改用 %s", label)
                return browser
            except Exception as exc:
                errors.append(f"{label}: {exc}")

        raise RuntimeError(
            "没有可用的浏览器。已尝试：\n  " + "\n  ".join(errors) +
            "\n请安装 Microsoft Edge 或 Google Chrome；"
            "或在 config.yaml 里把 verify.browser_channel 改成 auto。"
        )

    def close(self) -> None:
        for obj, label in ((self._context, "context"), (self._browser, "browser")):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception as exc:
                log.debug("关闭 %s 失败（忽略）：%s", label, exc)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception as exc:
                log.debug("停止 playwright 失败（忽略）：%s", exc)
        self._pw = self._browser = self._context = self._page = None

    def set_debug_dir(self, path) -> None:
        self._debug_dir = Path(path)

    def _ensure_page(self):
        if self._browser is None or not self._browser.is_connected():
            log.warning("浏览器连接已断开，正在重启")
            self.close()
            self.start()
        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
            self._page.on("response", self._on_response)
            self._install_page_logging(self._page)
        return self._page

    # ==================================================================
    #  查验接口响应监听
    # ==================================================================
    def _on_response(self, response) -> None:
        try:
            url = response.url
            if _QUERY_API_MARK in url:
                self._query_response = response
                return
            # 记录「.js 请求却返回 HTML」这种致命情况。
            # 这个平台的服务器对不存在的资源返回的是 index.html（状态码还是 200），
            # 浏览器把它当 JS 解析就报 "Unexpected token '<'"，SPA 直接白屏。
            # 能指出是哪个脚本，才知道是选择器问题还是平台自身部署不一致。
            if url.endswith(".js"):
                ctype = str(response.headers.get("content-type") or "").lower()
                if "html" in ctype:
                    log.error("脚本请求返回了 HTML（会让页面白屏）：%s [HTTP %s]",
                              url[:170], response.status)
        except Exception:
            pass

    def _take_query_payload(self) -> dict | None:
        """取回最近一次查验接口的 JSON（读不到就返回 None）。"""
        response = self._query_response
        if response is None or id(response) == self._query_seen:
            return None
        try:
            payload = response.json()
            self._query_seen = id(response)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _reset_query(self) -> None:
        self._query_response = None
        self._query_seen = 0

    @staticmethod
    def _api_message(payload: dict) -> tuple[str, str]:
        data = (payload.get("Response") or {}).get("Data") or {}
        return str(data.get("CyjgDm") or ""), str(data.get("CyjgMsg") or "")

    # ==================================================================
    #  主流程
    # ==================================================================
    def verify(self, task_id: str, inputs: dict[str, Any], *, filename: str = "",
               invoice_hint: str = "", want_pdf: bool = False) -> VerifyOutcome:
        try:
            return self._verify_inner(task_id, inputs, filename=filename,
                                      invoice_hint=invoice_hint, want_pdf=want_pdf)
        except Exception as exc:
            log.exception("任务 %s 查验异常", task_id)
            shot = self._grab_screenshot()
            self._dump_debug(task_id, shot)
            return VerifyOutcome(
                status="error",
                summary=f"查验过程异常：{type(exc).__name__}: {exc}",
                screenshot=shot,
            )

    def _verify_inner(self, task_id: str, inputs: dict[str, Any], *, filename: str,
                      invoice_hint: str, want_pdf: bool) -> VerifyOutcome:
        page = self._ensure_page()
        self._reset_query()

        missing = _missing_inputs(inputs)
        if missing:
            return VerifyOutcome(status="error",
                                 summary=f"入参不完整，缺少：{', '.join(missing)}")

        log.debug("打开 %s", self.home_url)
        page.goto(self.home_url, wait_until="load", timeout=self.nav_timeout)

        # 以「发票号码输入框出现」作为表单就绪信号（实测约 1-6 秒）。
        #
        # 实测这个平台的 SPA 会**间歇性渲染失败**：HTML 拿到了、标题也对，
        # 但 <div id="app"> 始终是空的——多半是懒加载的 chunk-vendors / index.js
        # 那一步没成功。遇到这种情况**整页重新导航**通常就正常了，
        # 不要因为一次渲染失败就白丢一张票。
        ready = self._locator(page, "invoice_number", wait_ms=25000)
        for attempt in range(1, _RENDER_RETRIES + 1):
            if ready is not None:
                break
            log.info("表单没渲染出来（第 %d 次重试）。当前页面：%s",
                     attempt, self._page_state(page))
            page.wait_for_timeout(1500)
            try:
                page.goto(self.home_url, wait_until="load", timeout=self.nav_timeout)
            except Exception as exc:
                log.debug("重新导航失败：%s", exc)
            ready = self._locator(page, "invoice_number", wait_ms=20000)

        if ready is None:
            log.error("重试 %d 次后仍拿不到表单。页面状态：%s",
                      _RENDER_RETRIES, self._page_state(page))
            shot = self._grab_screenshot(page)
            self._save_html(task_id, page)
            return VerifyOutcome(
                status="error",
                summary="页面上找不到发票号码输入框。可能是①平台改版，②网络慢没加载完，"
                        "③被限流。页面快照已保存，可跑 scripts/discover.py 核对结构。",
                screenshot=shot,
            )

        auto_left = self.max_auto if (self.auto_ocr and captcha_ocr.available()) else 0
        manual_left = self.max_manual if self.prompter is not None else 0
        # 记下初始额度，最后好如实报告实际用掉几次
        auto_budget, manual_budget = auto_left, manual_left
        if auto_left == 0 and manual_left == 0:
            return VerifyOutcome(
                status="error",
                summary="验证码自动识别与人工输入都不可用，无法继续"
                        "（未安装 ddddocr，且没有可用的输入界面）",
            )

        attempts = 0
        last_hash = ""
        source = ""

        while auto_left > 0 or manual_left > 0:
            attempts += 1

            # 每轮重填表单：验证码错一次之后平台通常会清空/刷新
            fill = self._fill_form(page, inputs)
            if not fill["ok"]:
                shot = self._grab_screenshot(page)
                self._save_html(task_id, page)
                return VerifyOutcome(status="error", summary=fill["reason"],
                                     screenshot=shot, captcha_attempts=attempts)

            img = self._locator(page, "captcha_image", wait_ms=5000)
            if img is None:
                shot = self._grab_screenshot(page)
                self._save_html(task_id, page)
                return VerifyOutcome(status="error",
                                     summary="找不到验证码图片元素（选择器可能已失效）",
                                     screenshot=shot, captcha_attempts=attempts)

            try:
                png = img.screenshot()
            except Exception as exc:
                return VerifyOutcome(status="error",
                                     summary=f"验证码截图失败：{exc}",
                                     captcha_attempts=attempts)

            png_hash = hashlib.sha256(png).hexdigest()

            if auto_left > 0:
                auto_left -= 1
                source = "ocr"
                text = captcha_ocr.solve(png)
                if not text:
                    log.debug("第 %d 次自动识别失败", attempts)
                    if png_hash == last_hash:
                        self._reload(page)
                    else:
                        self._refresh_captcha(page)
                    last_hash = png_hash
                    continue
            else:
                manual_left -= 1
                source = "manual"
                log.info("转人工输入验证码（%s）", filename or task_id)
                text = self.prompter.request(
                    task_id, png, filename=filename, hint=invoice_hint,
                    timeout=self.manual_timeout)
                if text is None:
                    if manual_left > 0:
                        log.info("人工未输入，刷新验证码后重试（还剩 %d 次）", manual_left)
                        self._refresh_captcha(page)
                        continue
                    shot = self._grab_screenshot(page)
                    return VerifyOutcome(
                        status="error",
                        summary=f"等待人工输入验证码超时或已跳过（{self.manual_timeout:.0f} 秒）",
                        screenshot=shot, captcha_source="manual",
                        captcha_attempts=attempts)

            last_hash = png_hash
            baseline = self._body_text(page)

            if not self._fill_field(page, "captcha_input", text):
                return VerifyOutcome(status="error",
                                     summary="验证码输入框填不进去（选择器可能已失效）",
                                     captcha_attempts=attempts)

            if self._locator(page, "submit", wait_ms=2000) is None:
                shot = self._grab_screenshot(page)
                self._save_html(task_id, page)
                return VerifyOutcome(status="error",
                                     summary="找不到「查验」按钮（选择器可能已失效）",
                                     screenshot=shot, captcha_attempts=attempts)

            # 提交，并顺手接住平台的接口返回。
            #
            # 用 expect_response 包住点击，而不是在事件回调里存 response 再回来读——
            # 后者在同步 API 里等到读的时候 body 已经失效（实测拿不到数据）。
            api: dict | None = None
            try:
                with page.expect_response(
                        lambda r: _QUERY_API_MARK in r.url, timeout=25000) as info:
                    self._click_submit(page)
                api = info.value.json()
            except Exception as exc:
                log.debug("没有捕获到查验接口返回（改用页面文本判定）：%s", exc)

            status, summary, body = self._wait_result(page, baseline, api)

            if status == "captcha_wrong":
                log.info("第 %d 次验证码被平台拒绝", attempts)
                continue

            if status == "unknown":
                shot = self._grab_screenshot(page)
                self._save_html(task_id, page)
                return VerifyOutcome(
                    status="unknown",
                    summary="页面已提交，但没能识别出结论（可重试）",
                    detail=self._trim(body), raw_text=body,
                    captcha_source=source, captcha_attempts=attempts,
                    screenshot=shot, page_url=getattr(page, "url", ""),
                    extra={"api": api},
                )

            # 有结论了：等结果区渲染完，再导 PDF
            page.wait_for_timeout(2500)
            body = self._body_text(page) or body
            pdf = self._capture_pdf(page) if want_pdf else None
            if want_pdf and pdf is None:
                log.warning("查验有结论，但结果 PDF 导出失败")

            return VerifyOutcome(
                status=status,
                summary=summary or self._summarize(status, body),
                detail=self._trim(body), raw_text=body,
                captcha_source=source, captcha_attempts=attempts,
                pdf_bytes=pdf, page_url=getattr(page, "url", ""),
                extra={"api": api},
            )

        # 走到这里说明验证码的路子用完了。如实报告实际用掉了几次，
        # 而不是笼统地写「自动 8 次 + 人工 3 次」——那会掩盖真实配置。
        used_auto = max(0, auto_budget - auto_left)
        used_manual = max(0, manual_budget - manual_left)
        parts: list[str] = []
        if used_auto:
            parts.append(f"自动识别 {used_auto} 次")
        if used_manual:
            parts.append(f"人工输入 {used_manual} 次")

        return VerifyOutcome(
            status="captcha_wrong",
            summary=f"验证码没通过（{' + '.join(parts) or '未尝试'}）",
            captcha_source=source, captcha_attempts=attempts,
        )

    # ==================================================================
    #  填表
    # ==================================================================
    def _fill_form(self, page, inputs: dict[str, Any]) -> dict[str, Any]:
        """把入参填进表单。

        顺序有讲究：先代码、再号码，**等表单切换完**，再填第 4 个字段
        （它的标签和含义都会随前面输入变化）。
        """
        code = inputs.get("invoice_code")
        if code:
            if not self._fill_field(page, "invoice_code", str(code)):
                # 数电票没有发票代码这一栏，找不到属正常
                log.debug("没有发票代码输入框，跳过")

        number = inputs.get("invoice_number")
        if not number:
            return {"ok": False, "reason": "缺少必填项 发票号码"}
        if not self._fill_field(page, "invoice_number", str(number)):
            log.error("填不进发票号码。页面状态：%s", self._page_state(page))
            return {"ok": False, "reason": "找不到发票号码输入框（选择器可能已失效）"}

        # 等平台根据号码/代码切换第 4 个字段的形态
        page.wait_for_timeout(1500)

        value_ok, reason = self._fill_value_field(page, inputs)
        if not value_ok:
            return {"ok": False, "reason": reason}

        date_iso = inputs.get("invoice_date")
        if not date_iso:
            return {"ok": False, "reason": "缺少必填项 开票日期"}
        if not self._set_date(page, str(date_iso)):
            return {"ok": False,
                    "reason": f"开票日期填不进去（{date_iso}）。"
                              "日期控件是只读的日历选择器，需要点选年/月/日"}

        return {"ok": True}

    def _fill_value_field(self, page, inputs: dict[str, Any]) -> tuple[bool, str]:
        """第 4 个字段：**先读标签，再决定填什么**。

        实测三种形态：
          开具金额(不含税) → 不含税金额
          校验码          → 校验码后 6 位
          价税合计        → 价税合计
        """
        item = self._locator(page, "value_item", wait_ms=4000)
        if item is None:
            return False, "找不到「校验码 / 金额」输入项（选择器可能已失效）"

        try:
            label = item.inner_text().replace(" ", "").replace("\n", "")
        except Exception:
            label = ""

        if "校验码" in label:
            value = inputs.get("check_code_last6")
            what = "校验码后6位"
        elif "价税合计" in label:
            value = inputs.get("amount_total") or inputs.get("amount_excl_tax")
            what = "价税合计"
        else:  # 开具金额(不含税) 或其它含「金额」的形态
            value = inputs.get("amount_excl_tax") or inputs.get("amount_total")
            what = "开具金额(不含税)"

        if not value:
            return False, f"表单当前需要「{what}」，但这张票没有识别出该值"

        if not self._fill_field(page, "value_input", str(value)):
            return False, f"「{what}」输入框填不进去（选择器可能已失效）"

        log.debug("第 4 字段标签含「%s」，填入 %s", label[:12], what)
        return True, ""

    def _fill_field(self, page, name: str, value: str) -> bool:
        loc = self._locator(page, name, wait_ms=5000)
        if loc is None:
            log.debug("找不到表单字段 %s", name)
            return False
        value = str(value)
        try:
            loc.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        for strategy in ("fill", "type"):
            try:
                if strategy == "fill":
                    loc.fill(value)
                else:
                    loc.click()
                    loc.type(value, delay=45)
            except Exception as exc:
                log.debug("字段 %s 用 %s 写入失败：%s", name, strategy, exc)
                continue
            got = self._input_value(loc)
            if got is None or got.strip() == value.strip():
                return True
        got = self._input_value(loc)
        log.warning("字段 %s 回读不一致：期望 %r 实际 %r", name, value, got)
        return False

    @staticmethod
    def _input_value(loc) -> str | None:
        try:
            return loc.input_value(timeout=2000)
        except Exception:
            return None

    # ==================================================================
    #  日期：只读的日历选择器，必须点选
    # ==================================================================
    def _set_date(self, page, iso_date: str) -> bool:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(iso_date or ""))
        if not m:
            return False
        year, month, day = m.group(1), str(int(m.group(2))), str(int(m.group(3)))

        loc = self._locator(page, "invoice_date", wait_ms=5000)
        if loc is None:
            return False

        try:
            loc.click()
        except Exception as exc:
            log.debug("点开日期面板失败：%s", exc)
            return False
        page.wait_for_timeout(1200)

        if not self._pick_dropdown(page, "date_year_select", {year}):
            log.warning("日期面板里选不到年份 %s", year)
            return False
        page.wait_for_timeout(900)

        if not self._pick_dropdown(page, "date_month_select", {f"{month}月"}):
            log.warning("日期面板里选不到月份 %s", month)
            return False
        page.wait_for_timeout(900)

        if not self._pick_cell(page, {day}):
            log.warning("日期面板里点不到 %s 号", day)
            return False

        page.wait_for_timeout(800)
        got = self._input_value(loc) or ""
        if got.replace("-", "").replace("/", "") != f"{year}{int(month):02d}{int(day):02d}":
            log.warning("日期回读不一致：期望 %s-%s-%s，实际 %r", year, month, day, got)
            return False
        return True

    def _pick_dropdown(self, page, select_name: str, wanted: set[str]) -> bool:
        trigger = self._locator(page, select_name, wait_ms=3000)
        if trigger is None:
            return False
        try:
            trigger.click()
        except Exception:
            return False
        page.wait_for_timeout(800)
        return self._scan_and_click(page, "date_option", wanted)

    def _pick_cell(self, page, wanted: set[str]) -> bool:
        return self._scan_and_click(page, "date_cell", wanted)

    def _scan_and_click(self, page, name: str, wanted: set[str]) -> bool:
        """在候选元素里找文本匹配的可见项并点击。

        统一去掉空白再比较：实测月选项文本是「3 月」（中间有空格），
        日格子里也可能带空白，直接精确比较会漏掉。
        """
        selector = ", ".join(self._candidates(name))
        if not selector:
            return False
        items = page.locator(selector)
        try:
            count = items.count()
        except Exception:
            return False
        for i in range(count):
            node = items.nth(i)
            try:
                if not node.is_visible():
                    continue
                text = node.inner_text().replace(" ", "").replace("\u00a0", "").strip()
            except Exception:
                continue
            if text in wanted:
                try:
                    node.click()
                    page.wait_for_timeout(500)
                    return True
                except Exception:
                    continue
        return False

    # ==================================================================
    #  提交与结果
    # ==================================================================
    def _click_submit(self, page) -> bool:
        """点「查验」。按钮在必填项齐了之后才会从 disabled 变可点。"""
        loc = self._locator(page, "submit", wait_ms=4000)
        if loc is None:
            return False
        # 等它变成可点（最多 8 秒）
        for _ in range(16):
            try:
                if loc.is_enabled():
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)
        try:
            loc.click(timeout=8000)
            page.wait_for_timeout(300)
            return True
        except Exception as exc:
            log.warning("点击查验按钮失败：%s", exc)
            return False

    def _wait_result(self, page, baseline: str, api: dict | None = None
                     ) -> tuple[str, str, str]:
        """等结论。返回 (状态, 摘要, 页面文本)。

        优先用 queryFpcyxx 接口的 JSON（更早更准），页面文本兜底。
        """
        # 1) 接口已经给了明确结论，直接用
        if api is not None:
            code, msg = self._api_message(api)
            status = classify(msg, self.cfg, "")
            if status == "unknown" and code == _CODE_CAPTCHA_WRONG:
                status = "captcha_wrong"
            if status != "unknown":
                return status, msg, self._body_text(page)

        deadline = time.monotonic() + self.result_timeout / 1000.0
        body = ""

        while time.monotonic() < deadline:
            # 2) 兜底：万一 expect_response 没接住，再看看监听器有没有存下响应
            if api is None:
                payload = self._take_query_payload()
                if payload is not None:
                    code, msg = self._api_message(payload)
                    status = classify(msg, self.cfg, "")
                    if status == "unknown" and code == _CODE_CAPTCHA_WRONG:
                        status = "captcha_wrong"
                    if status != "unknown":
                        return status, msg, self._body_text(page)

            # 3) 再兜底：读页面文本
            body = self._body_text(page)
            if body:
                status = classify(body, self.cfg, baseline)
                if status != "unknown":
                    return status, self._summarize(status, body), body

            page.wait_for_timeout(400)

        return "unknown", "", body

    def _summarize(self, status: str, body: str) -> str:
        for kw in (self.cfg.get(f"platform.result_keywords.{status}", []) or []):
            if kw and kw in body:
                for line in body.splitlines():
                    if kw in line:
                        return self._trim(line, 120)
                return str(kw)
        for line in body.splitlines():
            line = line.strip()
            if line and len(line) > 4:
                return self._trim(line, 120)
        return status

    # ==================================================================
    #  选择器 / 元素操作
    # ==================================================================
    def _candidates(self, name: str) -> list[str]:
        raw = self.cfg.get(f"platform.selectors.{name}", []) or []
        if isinstance(raw, str):
            return [raw]
        return [str(s) for s in raw if s]

    def _locator(self, page, name: str, wait_ms: int = 0):
        """按候选列表找第一个可见元素；wait_ms>0 时在这段时间内轮询。"""
        candidates = self._candidates(name)
        if not candidates:
            return None

        deadline = time.monotonic() + wait_ms / 1000.0
        while True:
            for sel in candidates:
                try:
                    loc = page.locator(sel).first
                    if loc.count() == 0:
                        continue
                    if not loc.is_visible():
                        continue
                    return loc
                except Exception:
                    continue
            if wait_ms <= 0 or time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(300)

    def _refresh_captcha(self, page) -> bool:
        for name in ("captcha_refresh", "captcha_image"):
            loc = self._locator(page, name, wait_ms=600)
            if loc is None:
                continue
            try:
                loc.click(timeout=4000)
                page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
        return False

    def _reload(self, page) -> None:
        try:
            page.reload(wait_until="load", timeout=self.nav_timeout)
            self._locator(page, "invoice_number", wait_ms=15000)
        except Exception as exc:
            log.debug("重载页面失败：%s", exc)

    # ==================================================================
    #  PDF 与调试
    # ==================================================================
    def _capture_pdf(self, page) -> bytes | None:
        """把当前结果页导成 PDF。

        用 CDP 而不是 page.pdf()：后者在**有头模式**下会直接报错，
        而用户完全可能把 headless 关掉。CDP 两种模式行为一致。
        """
        try:
            cdp = self._context.new_cdp_session(page)
            result = cdp.send("Page.printToPDF", {
                "printBackground": True,
                "paperWidth": 8.27,       # A4
                "paperHeight": 11.69,
                "marginTop": 0.4,
                "marginBottom": 0.4,
                "marginLeft": 0.4,
                "marginRight": 0.4,
                "preferCSSPageSize": False,
            })
            data = result.get("data")
            return base64.b64decode(data) if data else None
        except Exception as exc:
            log.warning("导出查验结果 PDF 失败：%s", exc)
            return None

    @staticmethod
    def _page_state(page) -> str:
        """页面当前状态摘要。

        用来回答「到底是 SPA 没渲染，还是选择器失效」——
        如果 inputs/formItems 都是 0、app 的子元素也是 0，那就是没渲染出来；
        如果表单元素都在、只是选择器匹配不到，那才是选择器问题。
        """
        try:
            state = page.evaluate(
                "() => ({path: location.pathname,"
                " bodyLen: document.body ? document.body.innerText.length : -1,"
                " inputs: document.querySelectorAll('input').length,"
                " formItems: document.querySelectorAll('.t-form__item').length,"
                " appChildren: (document.getElementById('app') || {}).childElementCount || 0})"
            )
            return str(state)
        except Exception as exc:
            return f"(读取页面状态失败: {exc})"

    @staticmethod
    def _body_text(page) -> str:
        try:
            return page.inner_text("body", timeout=5000)
        except Exception:
            return ""

    @staticmethod
    def _trim(text: str, limit: int = 4000) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def _grab_screenshot(self, page=None) -> bytes | None:
        page = page or self._page
        if page is None:
            return None
        try:
            return page.screenshot()
        except Exception:
            return None

    def _save_html(self, task_id: str, page) -> None:
        if self._debug_dir is None or page is None:
            return
        try:
            target = Path(self._debug_dir) / f"{task_id}.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page.content(), encoding="utf-8")
            log.info("已保存页面快照：%s", target)
        except Exception as exc:
            log.debug("保存页面快照失败：%s", exc)

    def _dump_debug(self, task_id: str, shot: bytes | None) -> None:
        if self._debug_dir is None:
            return
        try:
            if shot:
                target = Path(self._debug_dir) / f"{task_id}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(shot)
                log.info("已保存异常截图：%s", target)
            self._save_html(task_id, self._page)
        except Exception as exc:
            log.debug("保存调试产物失败：%s", exc)


def _missing_inputs(inputs: dict[str, Any]) -> list[str]:
    """入参完整性检查（对应平台表单的必填项）。"""
    missing: list[str] = []
    if not inputs.get("invoice_number"):
        missing.append("invoice_number")
    if not inputs.get("invoice_date"):
        missing.append("invoice_date")
    if not any(inputs.get(k) for k in
               ("check_code_last6", "amount_excl_tax", "amount_total")):
        missing.append("check_code_last6 或金额")
    return missing
