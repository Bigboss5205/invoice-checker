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

from ..captcha import ai as captcha_ai
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

# 结果页（jgbyz.html）里「结果：一致」那个字段
_RE_RESULT_FIELD = re.compile(r"结果\s*[:：]\s*([^\s|｜]{1,16})")

# 结果页底部这句是**固定说明，每张票都在**，而且里面带「不符」。
# 直接拿整页文本去匹配关键词，会把每一张票都判成「不一致」——
# 对发票工具来说这是最危险的误判，所以分类前先把它剔掉。
_RESULT_BOILERPLATE = (
    "发票信息不符时不得作为财务报销凭证",
    "任何单位和个人有权拒收并举报",
)


def result_field(text: str) -> str:
    """从结果页文本里精确取出「结果：xxx」的值（取不到返回空串）。"""
    m = _RE_RESULT_FIELD.search(text or "")
    return m.group(1) if m else ""


def strip_result_boilerplate(text: str) -> str:
    """去掉结果页上的固定说明，避免「不符」把结论带偏。"""
    for line in _RESULT_BOILERPLATE:
        text = text.replace(line, "")
    return text


class PlaywrightVerifier(BaseVerifier):
    name = "playwright"

    def __init__(self, cfg, prompter=None):
        super().__init__(cfg, prompter)

        self.mode = cfg.platform_mode          # legacy | spa
        self.home_url = cfg.start_url
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

        # 旧版平台把**查验结果开在新窗口里**（jgbyz.html），主页面不跳转。
        # 不盯着这些窗口就只能看到「页面已提交，但没有结论」——实测踩过这个坑。
        self._popups: list = []
        self._result_page = None         # 真正显示结论的那一页（可能是弹窗）

        # 站点校验不通过时弹的是**原生 alert**；Playwright 没有监听器时会
        # 默默把它点掉，页面上什么都不留。这里把它记下来。
        self._dialogs: list = []

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
        # 默认超时**刻意设短**：所有「等元素可点 / 可写」的动作都继承它。
        # 早先这里挂的是 nav_timeout(45s)，于是 `_set_date_legacy` 里那句
        # 只是「点一下让站点占位符逻辑先跑」的 click，在元素被判为不可点时
        # 硬等了 45 秒才抛异常——日志里就表现为「填表+取验证码 52.8s」。
        # 真正需要等很久的只有导航，而导航都是显式传 nav_timeout 的。
        self._context.set_default_timeout(10000)
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self._page = self._context.new_page()
        self._page.on("response", self._on_response)
        self._install_page_logging(self._page)
        # 必须在主页面创建**之后**再挂 page 监听：context.on("page") 对
        # 主页面本身也会触发，而那一刻 self._page 还没赋值，
        # 身份判等 `page is self._page` 恒为假 —— 结果是把主页面自己
        # 当成「平台弹出的结果窗口」记了一笔（日志里那句
        # 「平台打开了新窗口：about:blank」就是这么来的）。
        self._context.on("page", self._on_new_page)
        log.info("浏览器就绪（%s，headless=%s）", self._channel_used, self.headless)

    def _on_new_page(self, page) -> None:
        """记下平台新开的窗口。

        旧版查验成功后是用 ``window.open('jgbyz.html')`` 把结果**开在新窗口**里的：
        主页面既不跳转、接口也不返回 JSON，结论只存在于那个新窗口里。
        只盯主页面的话，一次成功的查验会被误判成「没能识别出结论」。
        """
        try:
            if page is self._page:
                return
            self._popups.append(page)
            page.on("response", self._on_response)
            self._install_page_logging(page)
            log.info("平台打开了新窗口（查验结果通常在这里）：%s",
                     (page.url or "")[:120] or "(正在加载)")
        except Exception as exc:
            log.debug("登记新窗口失败：%s", exc)

    def _result_pages(self, page) -> list:
        """可能要读结论的所有页面：主页面 + 平台弹出的结果窗口。"""
        pages = [page]
        for p in list(self._popups):
            try:
                if p is not page and not p.is_closed():
                    pages.append(p)
            except Exception:
                continue
        return pages

    def _install_page_logging(self, page) -> None:
        """把页面里的 JS 异常、控制台报错、加载失败的资源记进日志。

        排查「页面打开了、标题也对，但内容没渲染出来」这类问题时，
        这是唯一能看清原因的地方——否则只能看到一句「找不到输入框」。
        """
        try:
            page.on("pageerror", self._on_page_error)
            page.on("console", self._on_console)
            page.on("requestfailed", self._on_request_failed)
            page.on("dialog", self._on_dialog)
        except Exception as exc:
            log.debug("安装页面日志监听失败：%s", exc)

    def _on_dialog(self, dialog) -> None:
        """接住浏览器的**原生**弹窗（alert / confirm / prompt）。

        这个必须自己监听：Playwright 在没有 ``dialog`` 监听器时会
        **自动把弹窗点掉**，页面上不留任何痕迹。而旧版平台校验不通过时
        弹的正是原生 alert ——于是表现出来的现象是
        「点了查验，一个请求都没发出去，也没有任何提示，表单还被清空了」，
        完全无从下手。把文字记下来（并主动关掉，不关会把页面卡住）。
        """
        try:
            text = f"[{dialog.type}] {dialog.message}"
            self._dialogs.append(text)
            log.info("浏览器弹窗：%s", text)
        except Exception:
            pass
        try:
            dialog.dismiss()
        except Exception:
            pass

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

        log.debug("打开 %s（模式 %s）", self.home_url, self.mode)
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
        # 配了视觉大模型就一定有「自动」这条路：它比 ddddocr 靠谱得多，
        # 也是唯一能让你不用逐张手敲的办法。
        if captcha_ai.enabled(self.cfg):
            auto_left = max(auto_left, self.max_auto)
            log.info("验证码 AI 识别已启用：%s", captcha_ai.describe(self.cfg))
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
            t_fill = time.monotonic()
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

            png = self._captcha_png(img)
            if not png:
                return VerifyOutcome(status="error",
                                     summary="验证码图片取不到（选择器可能已失效）",
                                     captcha_attempts=attempts)
            cap_seconds = time.monotonic() - t_fill

            png_hash = hashlib.sha256(png).hexdigest()

            if auto_left > 0:
                auto_left -= 1
                source = "ocr"
                # 每次现读提示要哪个颜色：平台会在蓝/红之间切换，
                # 写死一种颜色会把另一类验证码全认错。
                color, hint_text = self._read_captcha_hint(page)
                if color:
                    log.debug("验证码要求填【%s】文字",
                              captcha_ocr.color_label(color))

                # 1) 先试视觉大模型——它跟人一样「看图 + 按提示挑颜色」，
                #    对中英混排的彩色验证码远好于 ddddocr。
                text = None
                if captcha_ai.enabled(self.cfg):
                    filtered = None
                    if color:
                        try:
                            filtered = captcha_ocr.color_filter(png, color)
                        except Exception:
                            filtered = None
                    text = captcha_ai.solve(png, hint_text, self.cfg,
                                            filtered_png=filtered)
                    if text:
                        source = "ai"

                # 2) 再退化到 ddddocr（颜色分离 + 识别）
                if not text:
                    text = captcha_ocr.solve(png, color)

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
                # 人工这条路更要读颜色：图里混着好几种颜色的字符，
                # 弹窗必须把「只填蓝色/红色」告诉用户，否则只能瞎猜。
                color, hint_text = self._read_captcha_hint(page)
                if color:
                    log.info("本次验证码要求填【%s】文字",
                             captcha_ocr.color_label(color))
                else:
                    log.info("未能从页面读出验证码颜色提示（原文：%r）", hint_text)
                log.info("转人工输入验证码（%s）", filename or task_id)
                text = self.prompter.request(
                    task_id, png, filename=filename, hint=invoice_hint,
                    timeout=self.manual_timeout,
                    color=color, color_text=hint_text)
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
            #
            # 同时记录提交期间的关键请求和主页面跳转：一旦「提交了却没结论」，
            # 这两样是唯一能说清原因的线索（接口到底发没发、页面是不是被整页刷掉了）。
            api: dict | None = None
            seen: list[str] = []
            navs: list[str] = []

            def _on_request(req):
                try:
                    if req.resource_type in ("xhr", "fetch", "document"):
                        # **保留查询串**：平台的结论很可能就挂在 URL 参数上，
                        # 早先把它截掉了，等于把最关键的线索扔了。
                        seen.append(f"{req.method} {req.url}"[:220])
                except Exception:
                    pass

            def _on_navigated(frame):
                try:
                    if frame == page.main_frame:
                        navs.append(frame.url.split("?")[0][-70:])
                except Exception:
                    pass

            page.on("request", _on_request)
            page.on("framenavigated", _on_navigated)
            self._dialogs.clear()
            t_submit = time.monotonic()
            try:
                with page.expect_response(
                        lambda r: _QUERY_API_MARK in r.url, timeout=12000) as info:
                    if not self._click_submit(page):
                        log.warning(
                            "没能点中「查验」按钮（可见且可点的那个没找到）"
                            "。页面状态：%s", self._page_state(page))
                api = info.value.json()
            except Exception as exc:
                log.debug("没有捕获到查验接口返回（改用页面文本判定）：%s", exc)

            status, summary, body = self._wait_result(page, baseline, api)
            # _wait_result 会把「能判定成结论」的弹窗消费掉；
            # 剩下的就是没认出来的，全是排查线索。
            left_dialogs = list(self._dialogs)

            try:
                page.remove_listener("request", _on_request)
                page.remove_listener("framenavigated", _on_navigated)
            except Exception:
                pass

            log.info("第 %d 轮：填表+取验证码 %.1fs，提交+等结论 %.1fs，查验接口%s",
                     attempts, cap_seconds, time.monotonic() - t_submit,
                     "已返回" if api is not None else "没返回")

            if status == "captcha_wrong":
                log.info("第 %d 次验证码被平台拒绝", attempts)
                # 平台是用一个模态框报错的，不点掉它就一直挡着，下一轮点击白点；
                # 同时主动刷新验证码——否则截图还是同一张，白费一次尝试。
                self._dismiss_alert(page)
                self._refresh_captcha(page)
                continue

            if status == "unknown":
                shot = self._grab_screenshot(page)
                self._save_html(task_id, page)
                # 这是最难查的一类失败：页面看着「提交过了」，却什么都没有。
                # 把提交期间看到的东西一次性打全，下次看日志就能定位：
                #   * 主页面跳转过 → 点击触发了整页提交，表单被刷空了；
                #   * 一个 xhr 都没有 → 点击压根没触发站点的 ajax 处理；
                #   * 有 xhr 但没结论 → 是结论识别的关键词/选择器要更新。
                log.warning(
                    "没能识别出结论：提交后主页面跳转 %d 次%s；关键请求 %d 个%s",
                    len(navs),
                    f"（最后一次：{navs[-1]}）" if navs else "",
                    len(seen),
                    "（" + "；".join(seen[-6:]) + "）" if seen else "")
                if left_dialogs:
                    log.warning("提交期间浏览器弹窗：%s", "；".join(left_dialogs[-3:]))
                # 把所有「可能是结果页」的窗口状态打出来：URL、是否还开着、
                # 正文开头。这一步是判断「结果窗口到底有没有内容」的唯一依据。
                for p in self._result_pages(page):
                    try:
                        log.warning("  候选结果页：url=%s closed=%s %s",
                                    (p.url or "")[:160], p.is_closed(),
                                    self._trim(self._body_text(p) or "(正文为空)", 160))
                    except Exception as exc:
                        log.warning("  候选结果页读取失败：%s", exc)
                return VerifyOutcome(
                    status="unknown",
                    summary="页面已提交，但没能识别出结论（可重试）",
                    detail=self._trim(body), raw_text=body,
                    captcha_source=source, captcha_attempts=attempts,
                    screenshot=shot, page_url=getattr(page, "url", ""),
                    extra={"api": api, "navigations": navs[-3:],
                           "requests": seen[-12:], "dialogs": left_dialogs[-5:]},
                )

            # 有结论了。结论可能在**另一个窗口**里（旧版 → jgbyz.html），
            # 所以后面一律用 _result_page：文本读它、PDF 也从它导出——
            # 那才是用户要的「已查验」那一页。
            result_page = self._result_page or page
            if result_page is not page:
                log.info("查验结论来自平台弹出的结果窗口：%s",
                         (getattr(result_page, "url", "") or "")[:120])
            result_page.wait_for_timeout(1000)      # 等结果区渲染稳
            body = self._body_text(result_page) or body
            pdf = self._capture_pdf(result_page) if want_pdf else None
            if want_pdf and pdf is None:
                log.warning("查验有结论，但结果 PDF 导出失败")

            return VerifyOutcome(
                status=status,
                summary=summary or self._summarize(status, body),
                detail=self._trim(body), raw_text=body,
                captcha_source=source, captcha_attempts=attempts,
                pdf_bytes=pdf, page_url=getattr(result_page, "url", ""),
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

        # 挪开焦点：平台是靠 onblur/onchange 来切换第 4 个字段形态的，
        # 一直停留在号码框里它就不切换，于是取值会按上一个形态取错。
        try:
            page.keyboard.press("Tab")
        except Exception:
            pass
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

    def _read_value_label(self, page, settle_ms: int = 2000) -> str:
        """读取第 4 个字段**当前**的标签。

        平台会按发票号码/代码把这个字段在「开具金额(不含税)」「价税合计」
        「校验码」之间切换，而且**切换是异步的**——填完号码立刻读，
        很可能还是上一个形态，于是取值就取错了（实测踩过：
        标签已经是「价税合计」，却按「不含税金额」取了 866.51 而不是 940.00，
        这种错误提交上去结论必然是错的）。

        所以这里先等一会儿，再连续读到两次一致才认。
        """
        page.wait_for_timeout(settle_ms)
        label = ""
        for _ in range(6):
            if self.mode == "legacy":
                node = self._locator(page, "value_label", wait_ms=800)
                text = node.inner_text() if node is not None else ""
            else:
                node = self._locator(page, "value_item", wait_ms=800)
                text = node.inner_text() if node is not None else ""
            text = (text or "").replace(" ", "").replace("\n", "")
            if text and text == label:
                return text
            label = text
            page.wait_for_timeout(600)
        return label

    def _fill_value_field(self, page, inputs: dict[str, Any]) -> tuple[bool, str]:
        """第 4 个字段：**先读标签，再决定填什么**，填完还要复核一遍。

        实测三种形态：
          开具金额(不含税) → 不含税金额
          校验码          → 校验码后 6 位
          价税合计        → 价税合计

        为什么要复核：平台的形态切换是**异步**的。实测第一轮读到的还是
        「开具金额(不含税)」于是填了不含税金额 866.51，可这张票的价税合计是 940.00——
        如果第一次验证码就通过，提交上去的就是错值，结论必然是错的。
        所以填完再读一次标签，变了就按新形态重填。
        """
        if self._locator(page, "value_input", wait_ms=4000) is None:
            return False, "找不到「校验码 / 金额」输入项（选择器可能已失效）"

        last_label = ""
        for attempt in range(3):
            label = self._read_value_label(page, settle_ms=2000 if attempt == 0 else 1200)
            value, what = self._pick_value(label, inputs)
            if value is None:
                return False, f"表单当前需要「{what}」，但这张票没有识别出该值"

            if not self._fill_field(page, "value_input", str(value)):
                return False, f"「{what}」输入框填不进去（选择器可能已失效）"

            log.info("第 4 字段标签「%s」→ 填入%s %s", label[:14], what, value)

            # 复核：形态没变就收工
            page.wait_for_timeout(700)
            again = self._read_value_label(page, settle_ms=600)
            if again == label or not again:
                return True, ""
            log.info("第 4 字段标签变成了「%s」，按新形态重填", again[:14])
            last_label = again

        log.warning("第 4 字段形态反复变化，最后一次按「%s」填的，请留意结论", last_label[:14])
        return True, ""

    @staticmethod
    def _pick_value(label: str, inputs: dict[str, Any]) -> tuple[Any, str]:
        """按标签决定取哪个值。返回值 (值, 中文名)；值为 None 表示这张票没有该项。"""
        if "校验码" in label:
            return inputs.get("check_code_last6"), "校验码后6位"
        if "价税合计" in label:
            return (inputs.get("amount_total") or inputs.get("amount_excl_tax"),
                    "价税合计")
        # 开具金额(不含税) 或其它含「金额」的形态
        return (inputs.get("amount_excl_tax") or inputs.get("amount_total"),
                "开具金额(不含税)")

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
        ymd = f"{m.group(1)}{m.group(2)}{m.group(3)}"

        loc = self._locator(page, "invoice_date", wait_ms=5000)
        if loc is None:
            return False

        if self.mode == "legacy":
            return self._set_date_legacy(page, loc, ymd)

        return self._set_date_spa(page, loc, m.group(1), str(int(m.group(2))),
                                  str(int(m.group(3))))

    def _set_date_legacy(self, page, loc, ymd: str) -> bool:
        """旧版：这是个带假占位符的普通输入框，但站点 JS 会覆盖键盘输入。

        实测三种写法只有「点一下清掉占位符，再用原生 setter 写值并派发事件」
        能生效——直接 type/fill 都会被站点自己的处理逻辑抹回 YYYYMMDD。
        """
        try:
            # 这一步只是「先点一下，让站点的占位符逻辑跑起来」，
            # 真正的写值靠下面的原生 setter。所以绝不能在这里久等：
            # 元素一旦被判为不可点（被日历面板压着等），
            # 用默认超时会白等几十秒，而跳过它对结果毫无影响。
            loc.click(timeout=2500)
        except Exception:
            pass
        page.wait_for_timeout(300)
        try:
            loc.evaluate(
                """(el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, v);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""",
                ymd,
            )
        except Exception as exc:
            log.warning("旧版日期写入失败：%s", exc)
            return False

        page.wait_for_timeout(600)
        got = (self._input_value(loc) or "").replace("-", "").replace("/", "")
        if got != ymd:
            log.warning("日期回读不一致：期望 %s，实际 %r", ymd, got)
            return False

        # 关掉日期弹出的日历面板。它会浮在页面上**挡住「查验」按钮**，
        # 这时点提交其实点在日历上，请求根本发不出去——实测踩过这个坑。
        self._dismiss_popups(page)
        return True

    @staticmethod
    def _dismiss_popups(page) -> None:
        """按 Esc 关掉可能挡住按钮的浮层（日期面板、下拉等）。"""
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            pass

    def _set_date_spa(self, page, loc, year: str, month: str, day: str) -> bool:
        """新版：readonly 的 TDesign 日历，只能点选年 → 月 → 日。"""
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
        """点「查验」。

        旧版有两个「查 验」按钮（#checkfp / #uncheckfp），由站点 JS 切换显示，
        所以要按顺序找到真正可见且可点的那一个；
        新版只有一个，且在必填项齐了之前是 disabled，需要等它变可点。
        """
        if self.mode == "legacy":
            # 提交前再关一次浮层：万一还有面板挡着，点击就白点了
            self._dismiss_popups(page)
            for sel in self._candidates("submit"):
                try:
                    loc = page.locator(sel).first
                    if loc.count() == 0 or not loc.is_visible() or not loc.is_enabled():
                        continue
                    loc.click(timeout=8000)
                    page.wait_for_timeout(300)
                    log.debug("已点击查验按钮 %s", sel)
                    return True
                except Exception as exc:
                    log.debug("点击 %s 失败：%s", sel, exc)
                    continue
            return False

        loc = self._locator(page, "submit", wait_ms=4000)
        if loc is None:
            return False
        for _ in range(16):          # 等它从 disabled 变可点（最多 8 秒）
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

        **关键**：旧版把结果开在新窗口（jgbyz.html），所以每一轮都要
        主页面和弹出窗口一起看；找到结论的那一页记在 ``self._result_page``，
        导出 PDF 也要用它——那才是用户要的「已查验」页面。
        """
        self._result_page = None

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
            # 0) 原生 alert：站点校验不通过时最先出现的就是它。
            #    它比接口和页面文本都早，而且只有这里能看到。
            while self._dialogs:
                msg = self._dialogs.pop(0)
                status = classify(msg, self.cfg, "")
                if status != "unknown":
                    self._result_page = page
                    return status, msg, self._body_text(page)

            for cand, owner in self._result_targets(page):
                # 0.2) 最准的一路：直接读结果页的「结果：」那一栏（#cyjg）。
                cell = self._read_result_cell(cand)
                if cell:
                    status = classify(cell, self.cfg, "")
                    if status != "unknown":
                        self._result_page = owner
                        return status, f"结果：{cell}", self._body_text(cand)

                # 0.5) 平台自绘的提示框——「验证码错误!」这类结论都放这里，
                #      优先读它：比扫整页文本准，也不会被帮助文字干扰
                alert = self._read_alert_message(cand)
                if alert:
                    status = classify(alert, self.cfg, "")
                    if status != "unknown":
                        self._result_page = owner
                        return status, alert, self._body_text(cand)

                # 2) 兜底：万一 expect_response 没接住，再看看监听器有没有存下响应
                if api is None:
                    payload = self._take_query_payload()
                    if payload is not None:
                        code, msg = self._api_message(payload)
                        status = classify(msg, self.cfg, "")
                        if status == "unknown" and code == _CODE_CAPTCHA_WRONG:
                            status = "captcha_wrong"
                        if status != "unknown":
                            self._result_page = owner
                            return status, msg, self._body_text(cand)

                # 3) 再兜底：读页面文本。
                #    弹窗页有它自己的 baseline——主页面跳没跳转跟它无关，
                #    结果页第一次读到就是新内容，所以这里不跟主页面的 baseline 比。
                text = self._body_text(cand)
                if text:
                    if cand is not page:
                        # 结果窗口：优先只用「结果：xxx」那个字段；
                        # 拿不到才退回整页文本，且先剔掉那句带「不符」的固定说明。
                        field = result_field(text)
                        if field:
                            status = classify(field, self.cfg, "")
                            if status != "unknown":
                                self._result_page = owner
                                return status, f"结果：{field}", text
                        status = classify(strip_result_boilerplate(text),
                                          self.cfg, "")
                        if status != "unknown":
                            self._result_page = owner
                            return status, self._summarize(status, text), text
                        continue

                    status = classify(text, self.cfg, baseline)
                    if status != "unknown":
                        self._result_page = owner
                        return status, self._summarize(status, text), text
                    body = text

            page.wait_for_timeout(400)

        return "unknown", "", body

    def _result_targets(self, page) -> list:
        """所有可能装着结论的地方，返回 ``[(可读对象, 所属页面), …]``。

        三层都要看：

        1. 主页面本身；
        2. **主页面的所有 iframe** —— 实测提交后只出现一个
           ``GET …/jgbyz.html`` 的 document 请求，而主页面既不跳转、
           也没有新窗口事件。那就是平台把结果**装进了 iframe**；
           而 ``page.inner_text("body")`` 读不到 iframe 里的内容，
           只看主页面自然什么都找不到。
        3. 平台弹出的窗口（旧版也可能用 ``window.open``）。

        返回的第二个元素是「所属页面」——iframe 自己不能导出 PDF，
        导出要用它所属的那个 Page。
        """
        targets: list = []
        for p in self._result_pages(page):
            targets.append((p, p))
            try:
                for frame in p.frames:
                    if frame is not p.main_frame:
                        targets.append((frame, p))
            except Exception:
                continue
        return targets

    def _read_result_cell(self, page) -> str:
        """直接读结果页上「结果：」那一栏的值。

        实测结果页（jgbyz.html）的结构是::

            <span>结果： <strong id="cyjg"></strong>
                  <span id="cysj">查验时间：…</span></span>

        直接读 ``#cyjg`` 比扫整页文本可靠得多：不受固定说明、
        表格排版、以及「结果：」后面紧跟别的标签的影响。
        """
        for sel in self._candidates("result_cell"):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                text = (loc.inner_text(timeout=1500) or "").strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

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
        """取候选选择器。两套页面结构不同，由 config 按 mode 给出对应的那一套。"""
        raw = self.cfg.selectors().get(name) or []
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

    @staticmethod
    def _captcha_png(img) -> bytes | None:
        """取验证码图片的原始字节。

        旧版的验证码是 ``data:image/png;base64,...`` 内联图，**直接解码 src**
        比 ``locator.screenshot()`` 好三点：

        * **快得多**——``screenshot()`` 会等元素「稳定」（连续两帧位置不变）
          才肯截，而这张图所在的容器带 hover 动画，实测能白等几十秒；
        * 拿到的是**原始像素**，不会把 CSS 缩放、叠加层一起截进来；
        * 不受窗口大小 / 滚动位置 / 元素被遮挡的影响。

        拿不到 data URL 时（新版 SPA 或别的形态）再退回截图。
        """
        try:
            src = img.get_attribute("src") or ""
        except Exception as exc:
            log.debug("读取验证码 src 失败：%s", exc)
            src = ""

        marker = "base64,"
        if src.startswith("data:image") and marker in src:
            try:
                data = base64.b64decode(src.split(marker, 1)[1])
                if data:
                    return data
            except Exception as exc:
                log.debug("验证码 data URL 解码失败：%s", exc)

        try:
            return img.screenshot(timeout=8000)
        except Exception as exc:
            log.debug("验证码截图失败：%s", exc)
            return None

    def _read_alert_message(self, page) -> str:
        """读平台自绘提示框里的文字。

        旧版把「验证码错误!」「查无此票」这类结论都放在
        ``<div id="popup_message">`` 里。直接读它比扫描整页文本准得多——
        整页文本里混着帮助说明，还容易把提交前就存在的文字误判成结论。
        """
        for sel in self._candidates("alert_message"):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                return (loc.inner_text() or "").strip()
            except Exception:
                continue
        return ""

    def _dismiss_alert(self, page) -> bool:
        """关掉平台弹出的「提示」模态框。

        旧版用 alert 风格的模态框报告「验证码错误!」，不点确定它就一直挡着，
        后续的点击全都落在它身上——实测踩过这个坑（点了提交却没有任何请求发出）。
        """
        for sel in self._candidates("alert_ok"):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                loc.click(timeout=3000)
                page.wait_for_timeout(500)
                log.debug("已关闭提示框（%s）", sel)
                return True
            except Exception:
                continue
        return False

    def _read_captcha_hint(self, page) -> tuple[str | None, str]:
        """读出「这次的验证码要填哪个颜色」，以及页面上那句提示原文。

        平台提示形如「请输入验证码图片中蓝色文字」/「…红色文字」，
        **颜色是会变的**（实测蓝、红都出现过），所以每次都要现读。
        写死一种颜色会把另一类验证码全部认错，还不如不做分离。

        返回 ``(颜色名或 None, 提示原文)``。原文要一路传到人工弹窗上——
        图里混着几种颜色的字符，不告诉用户填哪种就等于让他瞎猜。
        """
        best_text = ""
        for name in ("captcha_hint", "captcha_input"):
            loc = self._locator(page, name, wait_ms=400)
            if loc is None:
                continue
            try:
                text = (loc.inner_text() or "").strip()
            except Exception:
                continue
            if "验证码" in text and not best_text:
                best_text = text
            color = captcha_ocr.parse_color_hint(text)
            if color:
                return color, text

        # 兜底：整页文本里找一次
        body = self._body_text(page)
        index = body.find("验证码图片中")
        if index >= 0:
            snippet = body[index:index + 24].strip()
            if not best_text:
                best_text = snippet
            color = captcha_ocr.parse_color_hint(snippet)
            if color:
                return color, snippet
        return None, best_text

    def _read_captcha_color(self, page) -> str | None:
        """只要颜色（自动识别那条路用）。"""
        return self._read_captcha_hint(page)[0]

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
