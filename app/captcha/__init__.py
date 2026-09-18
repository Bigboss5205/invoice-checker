"""验证码：自动识别 + 人工输入兜底。

这里把 :mod:`.ocr` 的公开接口转出来，这样下面两种写法都成立：

    from app.captcha import ocr;  ocr.available()
    from app import captcha;      captcha.available()

（真实教训：曾经只写了文档字符串、没做转出，而驱动里用的是
 ``from .. import captcha`` + ``captcha.available()``，
 结果一跑到真实查验就 AttributeError——因为测试只调了驱动里的各个子方法，
 没有跑完整流程，所以没暴露。）
"""

from .ocr import available, blue_filter, engine_name, solve  # noqa: F401

__all__ = ["available", "blue_filter", "engine_name", "solve"]
