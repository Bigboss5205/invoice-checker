"""用「视觉大模型 API」识别验证码。

为什么需要它
------------
平台这个验证码是「从彩色字符里挑出**指定颜色**的那些」，
字符还是中文与字母混排（例如「村朋FQ」）。ddddocr 是为纯数字/字母的
4 位验证码训练的，对这类图基本认不出，所以默认只能人工输入——
而人工输入意味着**每张票都要等你**，这才是整个流程里最慢的一环。

视觉大模型做这件事跟人一样：看图、按提示挑颜色、把字读出来。
实测比颜色分离 + ddddocr 靠谱得多，而且不用你动手。

接口
----
只要求**兼容 OpenAI Chat Completions**（``POST {base_url}/chat/completions``，
消息里带 ``image_url``）。国内外的服务大多兼容，配置里填
base_url / model / api_key 就行，**不引入任何新依赖**（用标准库发请求）。

成本与隐私
----------
* 每张验证码一次请求，都是很短的往返，花费极小但**不是零**；
* 发出去的**只有验证码图片本身**，不含任何发票信息；
* 不配 api_key 就自动跳过这一路，退回 ddddocr / 人工，功能不受影响。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# api_key 也可以只放环境变量，免得写进配置文件里
ENV_API_KEY = "INV_CAPTCHA_AI_KEY"

# 从模型回复里挑出验证码，顺手把解释性文字、标点、空格都丢掉
_JUNK = (" \t\r\n=+*_-.,:;'\"`~^<>[]{}()\\/|"
         "、。，！？：；“”‘’《》「」【】（）〔〕")
_RE_TAG = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")

MAX_LENGTH = 12


def _config(cfg) -> dict:
    key = str(cfg.get("captcha.ai.api_key", "") or "").strip()
    if not key:
        key = (os.environ.get(ENV_API_KEY) or "").strip()
    return {
        "base_url": str(cfg.get("captcha.ai.base_url", "") or "").strip(),
        "model": str(cfg.get("captcha.ai.model", "") or "").strip(),
        "api_key": key,
        "timeout": float(cfg.get("captcha.ai.timeout_seconds", 20) or 20),
    }


def enabled(cfg) -> bool:
    """是否配置齐全、可以走 AI 识别。"""
    conf = _config(cfg)
    return bool(conf["base_url"] and conf["model"] and conf["api_key"])


def describe(cfg) -> str:
    """给日志/界面看的一句话状态。"""
    if not enabled(cfg):
        return "未启用"
    return f"{_config(cfg)['model']} @ {_config(cfg)['base_url']}"


def _prompt(color_text: str, filtered: bool) -> str:
    hint = color_text.strip() if color_text else ""
    lines = [
        "这是一张网站验证码图片。",
    ]
    if hint:
        lines.append(f"页面上的提示是：「{hint}」。请严格按这个提示来。")
    else:
        lines.append("图中混着多种颜色的字符，通常只该填某一种颜色的那些。")
    if filtered:
        lines.append("第二张图是把该颜色的字符单独分离出来的结果，可以对照着看。")
    lines.append(
        "只输出需要填进输入框的字符本身，"
        "不要空格、不要标点、不要引号、不要任何解释或前后缀。"
    )
    return "\n".join(lines)


def _clean(text: str) -> str | None:
    """把模型回复收拾成验证码。

    **只做保守清理**：去掉首尾的引号/标点/空白，丢掉空白与标点本身。
    刻意**不**去剥「答案是」「验证码是」这类中文说明词——验证码本身
    就可能是中文（实测出现过「村朋FQ」），按词表剥会把真实的字吃掉，
    那种错误比"多几个字"更糟。所以靠提示词要求模型只输出字符本身
    （temperature=0），万一它没听话，平台会判验证码错误 → 自动重试，
    失败模式是安全的。
    """
    if not text:
        return None
    first = str(text).strip().splitlines()[0] if str(text).strip() else ""
    first = first.strip(_JUNK)
    kept = "".join(ch for ch in first if ch not in _JUNK)
    if not kept:
        return None
    if not _RE_TAG.search(kept):
        return None
    if len(kept) > MAX_LENGTH:
        log.debug("AI 返回内容过长，丢弃：%r", text[:80])
        return None
    return kept


def solve(png: bytes, color_text: str = "", cfg=None,
          filtered_png: bytes | None = None) -> str | None:
    """让视觉模型读一张验证码。失败返回 None（调用方会退回人工）。"""
    if not png or cfg is None:
        return None
    conf = _config(cfg)
    if not (conf["base_url"] and conf["model"] and conf["api_key"]):
        return None

    content: list[dict] = [
        {"type": "text", "text": _prompt(color_text, bool(filtered_png))},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
    ]
    if filtered_png:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64,"
                   + base64.b64encode(filtered_png).decode("ascii")}})

    payload = {
        "model": conf["model"],
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 32,
    }
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {conf['api_key']}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=conf["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        log.warning("AI 验证码接口返回 HTTP %s：%s", exc.code, body)
        return None
    except Exception as exc:
        log.warning("AI 验证码接口调用失败：%s", exc)
        return None

    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:
        log.warning("AI 验证码返回格式看不懂：%s", str(data)[:200])
        return None
    if isinstance(text, list):        # 有的实现会返回分段内容
        text = "".join(part.get("text", "") for part in text
                       if isinstance(part, dict))

    answer = _clean(text)
    if answer:
        log.info("AI 识别出验证码：%s", answer)
    else:
        log.debug("AI 没能给出可用的验证码，原始回复：%r", str(text)[:120])
    return answer
