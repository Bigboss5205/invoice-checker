"""配置：默认值 → config.yaml → 环境变量覆盖。

桌面版和服务器版的区别
----------------------
「工作目录」不再是固定的 /data，而是**程序所在目录**：

* 免安装 exe：``sys.executable`` 的目录（也就是你把 exe 放的地方）
* 源码运行：项目根目录

这样一来「把 exe 和发票放一起双击」就天然成立——不用配任何路径。
配置文件也是可选的：同目录下没有 config.yaml 就用内置默认值，照样能跑。
"""

from __future__ import annotations

import copy
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
#  路径基准
# --------------------------------------------------------------------------
def app_dir() -> Path:
    """程序所在目录 —— 发票默认就放这里。

    * PyInstaller 打包后：exe 所在的目录
    * 源码运行：项目根目录
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_dir() -> Path:
    """只读资源目录。打包后是解压出来的临时目录，源码运行就是项目根。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
#  默认值
# --------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "paths": {
        # "." 表示「程序所在目录」。改这里可以指向别的发票目录。
        "workdir": ".",
        "db": "发票查验记录.sqlite3",
        "log": "发票查验日志.txt",
    },
    "scan": {
        "extensions": [
            ".pdf", ".ofd", ".xml",
            ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff",
        ],
        "recursive": False,      # 是否连子目录一起查
        "skip_verified": True,   # 已经有「-已查验」结果的就跳过
        "stable_seconds": 1,     # 文件大小稳定这么久才认为拷贝完成
    },
    "output": {
        "suffix": "-已查验",      # 结果文件命名：<原名>-已查验.pdf
        "pdf": True,             # 是否导出查验结果 PDF
        "overwrite": True,       # 同名结果已存在时是否覆盖
        "record_txt": False,     # 额外导出一份纯文本查验记录
        "subfolder": "",         # 结果另存到子目录（留空 = 与发票同目录）
    },
    "extract": {
        "densify_digits": True,
        "render_dpi": 220,
        "image_ocr": True,
        "save_debug_text": False,  # 桌面版默认不落调试文本，保持目录干净
    },
    "captcha": {
        # 默认关闭自动识别，全部走人工弹窗。
        #
        # 原因：这个平台的验证码是「从彩色字符里挑出指定颜色」的那种
        # （提示会写「请输入验证码图片中蓝色文字」/「…红色文字」，颜色还会变），
        # 字符又是中文与字母混排。自动识别成功率很低，与其每张票白等
        # 几轮重试，不如直接弹窗让你照着图敲——又快又不会认错。
        # （验证码位数不固定，取决于图里指定颜色的字符有几个，别按位数猜。）
        #
        # 想试试自动识别就改成 true（认不出仍会转人工，不会卡住）。
        "auto_ocr": False,
        "max_auto_attempts": 6,    # 仅在 auto_ocr=true 时有意义
        "manual_timeout_seconds": 180,
        "manual_max_rounds": 3,    # 人工最多被要求输入几次
    },
    "verify": {
        "enabled": True,
        "driver": "playwright",    # playwright | fake（离线演练）
        "headless": True,          # False 会弹出真实浏览器窗口
        "browser_channel": "msedge",  # msedge | chrome | chromium | auto
        "nav_timeout_ms": 45000,
        "result_timeout_ms": 30000,
        "min_interval_seconds": 8, # 两张票之间的最小间隔，别调太小
        "max_attempts": 2,         # 同一张票整体重试次数
        "stop_on_consecutive_failures": 0,  # >0 时连续失败这么多次就停下
    },
    "platform": {
        # 默认用**旧版**页面：它是普通 HTML 表单，元素是最简单的 id（#fpdm/#fphm/…），
        # 加载过程没有任何懒加载 chunk，稳定得多。
        #
        # 为什么不用新版 /fpcygzfw/：新版是 Vue SPA，实测它依赖的
        # assets_res/js/chunk-vendors.ccf6a1dc.js 在服务器上**并不存在**
        # （请求它返回的是 index.html，HTTP 还是 200）。浏览器把它当 JS 解析就报
        # "Unexpected token '<'"，Vue 从未挂载、整页空白——而且是间歇性的。
        # 这是平台自身的部署缺陷，我们绕开它。
        "mode": "legacy",          # legacy = 旧版普通页面；spa = 新版 SPA
        "home_url": "https://inv-veri.chinatax.gov.cn/index.html",
        "spa_url": "https://inv-veri.chinatax.gov.cn/fpcygzfw/",

        # 旧版（legacy）的选择器——都是页面上的原始 id
        "legacy_selectors": {
            "invoice_code": ["#fpdm"],
            "invoice_number": ["#fphm"],
            "invoice_date": ["#kprq"],
            # 第 4 个字段的**标签是动态的**（开具金额(不含税)/价税合计/校验码），
            # 标签文字就在 #context 里，值填在 #kjje
            "value_label": ["#context"],
            "value_input": ["#kjje"],
            "captcha_input": ["#yzm"],
            "captcha_image": ["#yzm_img"],
            "captcha_refresh": ["text=点击图片刷新", "#yzm_img"],
            "captcha_hint": ["#yzminfo", "#yzm"],
            "submit": ["#checkfp", "#uncheckfp"],
            # 旧版把提示/错误都塞进这个自绘弹窗里：
            #   <div id="popup_message">验证码错误!</div>
            #   <input type="button" value="&nbsp;确定&nbsp;" id="popup_ok">
            # 注意「确定」是 input[type=button] 不是 <button>，用 button:has-text 选不中。
            "alert_message": ["#popup_message"],
            "alert_ok": ["#popup_ok", "input[value*='确定']", "button:has-text('确定')"],
        },

        # 新版（spa）的选择器。平台改版后跑 scripts/discover.py 重新抓。
        "selectors": {
            "invoice_number": [".t-form-item__fphm input", "input[placeholder*='发票号码']"],
            "invoice_code": [".t-form-item__fpdm input", "input[placeholder*='发票代码']"],
            "invoice_date": [".t-form-item__kprq input", "input[placeholder='YYYYMMDD']"],
            "value_item": [".t-form-item__kpje", ".t-form-item__kjh"],
            "value_input": [".t-form-item__kpje input", ".t-form-item__kjh input"],
            "value_label": [".t-form__label"],
            "captcha_input": [".t-form-item__yzm input", "input[placeholder*='验证码']"],
            "captcha_image": ["form.t-form img", "img[src*='captcha']"],
            "captcha_refresh": [".form-box-tip__yzm", "text=点击图片刷新"],
            "captcha_hint": [".yzm-tips", ".form-box-tip"],
            "submit": ["button.t-button[type='submit']", "button:has-text('查 验')"],
            "date_year_select": [".t-date-picker__header-controller-year input"],
            "date_month_select": [".t-date-picker__header-controller-month input"],
            "date_option": [".t-select-option"],
            "date_cell": [".t-date-picker__cell"],
            "alert_ok": ["#popup_ok", "button:has-text('确定')", ".t-dialog button"],
            "alert_message": ["#popup_message", ".t-dialog__body"],
            "result_area": ["form.t-form", "body"],
        },
        "result_keywords": {
            "captcha_wrong": ["验证码错误", "验证码不正确", "验证码有误", "请重新输入验证码"],
            "rate_limited": ["超过该张发票当日查验次数", "查验次数已达上限",
                             "请稍后再试", "访问过于频繁"],
            "not_found": ["查无此票", "未查到", "查验失败"],
            "mismatch": ["不一致", "不符"],
            "ok": ["查验成功", "查验一致", "信息一致", "发票信息一致"],
        },
        "result_labels": {
            "ok": "一致",
            "mismatch": "不一致",
            "not_found": "查无此票",
            "captcha_wrong": "验证码失败",
            "rate_limited": "超次数限流",
            "unknown": "结果未知",
            "error": "系统错误",
            "skipped": "跳过",
        },
    },
    "ui": {
        "auto_start": False,     # 打开程序就自动开始查验
        "log_lines": 400,
    },
}

_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "INV_WORKDIR": ("paths", "workdir", str),
    "INV_DRIVER": ("verify", "driver", str),
    "INV_HEADLESS": ("verify", "headless", bool),
    "INV_MIN_INTERVAL": ("verify", "min_interval_seconds", float),
    "INV_AUTO_OCR": ("captcha", "auto_ocr", bool),
    "INV_HOME_URL": ("platform", "home_url", str),
    "INV_BROWSER_CHANNEL": ("verify", "browser_channel", str),
}


def _coerce(value: str, kind: type) -> Any:
    if kind is bool:
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return kind(value)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


class Config:
    def __init__(self, data: dict[str, Any], source: str | None = None,
                 base: Path | None = None):
        self._data = data
        self.source = source
        self.base = Path(base) if base else app_dir()
        self.warnings: list[str] = []

    # -- 取值 ---------------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, path: str) -> dict[str, Any]:
        val = self.get(path, {})
        return val if isinstance(val, dict) else {}

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # -- 路径 ---------------------------------------------------------------
    def resolve(self, raw: str | Path) -> Path:
        """相对路径按「程序所在目录」解析。"""
        p = Path(str(raw)).expanduser()
        return p if p.is_absolute() else (self.base / p)

    def path(self, key: str) -> Path:
        raw = self.get(f"paths.{key}")
        if not raw:
            raise KeyError(f"配置缺少 paths.{key}")
        return self.resolve(raw)

    @property
    def workdir(self) -> Path:
        return self.path("workdir")

    # -- 平台页面模式 -------------------------------------------------------
    @property
    def platform_mode(self) -> str:
        """legacy = 旧版普通 HTML 页面；spa = 新版 Vue 单页应用。"""
        mode = str(self.get("platform.mode", "legacy") or "legacy").strip().lower()
        return mode if mode in {"legacy", "spa"} else "legacy"

    def selectors(self) -> dict[str, Any]:
        """当前模式对应的选择器表。

        两种模式的页面结构完全不同，所以选择器是两套，由 mode 决定用哪套。
        """
        key = ("platform.legacy_selectors" if self.platform_mode == "legacy"
               else "platform.selectors")
        return self.section(key)

    @property
    def start_url(self) -> str:
        """当前模式该打开的入口地址。"""
        if self.platform_mode == "legacy":
            return str(self.get("platform.home_url"))
        return str(self.get("platform.spa_url") or self.get("platform.home_url"))

    @property
    def out_dir(self) -> Path:
        """结果输出目录。

        注意 subfolder 是**相对发票目录**解析的，不是相对程序目录——
        这里的意图是「把结果放在发票旁边的某个子目录里」。
        相对程序目录会让结果落到 exe 所在处，发票在别的盘时就完全跑偏了。
        """
        sub = str(self.get("output.subfolder") or "").strip()
        if not sub:
            return self.workdir
        candidate = Path(sub).expanduser()
        return candidate if candidate.is_absolute() else (self.workdir / candidate)

    def ensure_dirs(self) -> None:
        for target in (self.workdir, self.path("db").parent, self.path("log").parent,
                       self.out_dir):
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.warnings.append(f"无法创建目录 {target}：{exc}")

    # -- 校验 ---------------------------------------------------------------
    def validate(self) -> list[str]:
        problems: list[str] = []

        if self.get("verify.enabled", True):
            driver = str(self.get("verify.driver", "playwright"))
            if driver not in {"playwright", "fake"}:
                problems.append(f"verify.driver 只能是 playwright 或 fake，当前 {driver!r}")

            channel = str(self.get("verify.browser_channel", "") or "")
            if channel not in {"", "auto", "msedge", "chrome", "chromium"}:
                problems.append(
                    f"verify.browser_channel 只能是 msedge/chrome/chromium/auto，当前 {channel!r}")

            home = str(self.get("platform.home_url", ""))
            if not home.startswith("http"):
                problems.append(f"platform.home_url 必须以 http 开头，当前 {home!r}")

        try:
            if float(self.get("verify.min_interval_seconds", 8)) < 0:
                problems.append("verify.min_interval_seconds 不能是负数")
        except (TypeError, ValueError):
            problems.append("verify.min_interval_seconds 不是数字")

        if str(self.get("platform.mode", "legacy")).lower() not in {"legacy", "spa"}:
            problems.append("platform.mode 只能是 legacy 或 spa")

        sel = self.selectors()
        for need in ("invoice_number", "captcha_input", "submit"):
            if not sel.get(need):
                problems.append(
                    f"platform.{'legacy_selectors' if self.platform_mode == 'legacy' else 'selectors'}"
                    f".{need} 不能为空")

        if self.get("verify.enabled", True) and not self.start_url.startswith("http"):
            problems.append(f"平台入口地址必须以 http 开头，当前是 {self.start_url!r}")

        if not self.workdir.is_dir():
            problems.append(f"发票目录不存在：{self.workdir}")

        return problems


def config_path() -> Path:
    env = os.environ.get("INV_CONFIG")
    if env:
        return Path(env).expanduser()
    return app_dir() / "config.yaml"


def load_config(path: str | Path | None = None) -> Config:
    cfg_file = Path(path).expanduser() if path else config_path()

    user_data: dict[str, Any] = {}
    source: str | None = None

    if cfg_file.exists():
        try:
            raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                user_data = raw
                source = str(cfg_file)
            else:
                log.warning("配置文件根节点不是字典，已忽略：%s", cfg_file)
        except yaml.YAMLError as exc:
            log.error("config.yaml 解析失败，改用默认配置：%s", exc)
        except OSError as exc:
            log.error("config.yaml 读取失败，改用默认配置：%s", exc)
    else:
        log.info("未找到 config.yaml，使用内置默认值（发票目录 = 程序所在目录）")

    data = _deep_merge(DEFAULTS, user_data)

    for env_key, (sec, key, kind) in _ENV_OVERRIDES.items():
        raw_env = os.environ.get(env_key)
        if not raw_env:
            continue
        try:
            data.setdefault(sec, {})[key] = _coerce(raw_env, kind)
        except (TypeError, ValueError):
            log.warning("环境变量 %s=%r 无法解析为 %s，已忽略", env_key, raw_env, kind.__name__)

    cfg = Config(data, source=source, base=app_dir())
    for problem in cfg.validate():
        log.error("配置问题：%s", problem)
    return cfg
