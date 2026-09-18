"""抓一份查验平台的「页面元素清单」，用来修复失效的选择器。

什么时候用它
------------
平台改版后，日志里会出现「找不到发票号码输入框 / 找不到查验按钮」这类报错。
这时在项目目录跑：

    .build-venv\\Scripts\\python.exe -m scripts.discover
    # 或用打包好的环境：python -m scripts.discover

它会打开查验平台，把页面上所有可见的输入框、按钮、图片列出来，
并且**顺便生成一份可直接粘进 config.yaml 的选择器建议**。

常用参数
--------
    --headed        开有头浏览器（某些情况下无头会被站点拒绝，便于肉眼确认）
    --wait 8        页面加载后额外等几秒，让懒加载的组件渲染完
    --url ...       临时换一个地址（默认取 config.yaml 里的 platform.home_url）
    --out discover.json             结果保存位置
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402

# 在页面里跑，把所有可见元素的属性抠出来
_COLLECT_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 &&
           s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const attrs = (el) => {
    const o = {};
    for (const a of el.attributes) o[a.name] = a.value;
    return o;
  };
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height)};
  };

  const out = {url: location.href, title: document.title,
                inputs: [], buttons: [], images: [], canvases: [],
                links: [], iframes: [], clickables: []};

  document.querySelectorAll('input, textarea, select').forEach((el) => {
    if (!visible(el)) return;
    out.inputs.push(Object.assign({tag: el.tagName.toLowerCase(),
      visible: true, box: box(el), value: el.value || ''}, attrs(el)));
  });

  document.querySelectorAll('button, [role=button], input[type=submit], input[type=button]')
    .forEach((el) => {
      if (!visible(el)) return;
      out.buttons.push(Object.assign({tag: el.tagName.toLowerCase(),
        text: (el.innerText || el.value || '').trim().slice(0, 40),
        box: box(el)}, attrs(el)));
    });

  document.querySelectorAll('img').forEach((el) => {
    if (!visible(el)) return;
    out.images.push(Object.assign({tag: 'img', box: box(el)}, attrs(el)));
  });

  document.querySelectorAll('canvas').forEach((el) => {
    if (!visible(el)) return;
    out.canvases.push(Object.assign({tag: 'canvas', box: box(el)}, attrs(el)));
  });

  document.querySelectorAll('a').forEach((el) => {
    if (!visible(el)) return;
    const t = (el.innerText || '').trim();
    if (t) out.links.push({text: t.slice(0, 40), href: el.getAttribute('href') || ''});
  });

  document.querySelectorAll('iframe').forEach((el) => {
    out.iframes.push(Object.assign({tag: 'iframe'}, attrs(el)));
  });

  // 页面上所有可见短文本，方便对照平台提示语
  const seen = new Set();
  document.querySelectorAll('body *').forEach((el) => {
    if (el.children.length) return;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 30 || seen.has(t)) return;
    if (!visible(el)) return;
    seen.add(t);
    out.clickables.push(t);
  });

  return out;
}
"""

# 关键词 → 内部字段名
_KEYWORD_MAP = [
    ("发票代码", "invoice_code"),
    ("发票号码", "invoice_number"),
    ("开票日期", "invoice_date"),
    ("日期", "invoice_date"),
    ("校验码", "check_code"),
    ("后6位", "check_code"),
    ("金额", "amount"),
    ("验证码", "captcha_input"),
]


def _pick_keyword(text: str) -> str | None:
    for kw, field in _KEYWORD_MAP:
        if kw in text:
            return field
    return None


def suggest(discovered: dict) -> dict[str, list[str]]:
    """从元素清单里推导候选选择器。顺序即优先级。"""
    result: dict[str, list[str]] = {}

    for el in discovered.get("inputs", []):
        hint = " ".join(str(el.get(k) or "") for k in ("placeholder", "id", "name", "aria-label"))
        field = _pick_keyword(hint)
        if not field:
            continue
        bucket = result.setdefault(field, [])
        if el.get("id"):
            bucket.append(f"#{el['id']}")
        if el.get("name"):
            bucket.append(f"input[name='{el['name']}']")
        ph = el.get("placeholder")
        if ph:
            # 只取占位符里最有辨识度的一小段
            key = ph
            for kw, _ in _KEYWORD_MAP:
                if kw in ph:
                    key = kw
                    break
            bucket.append(f"input[placeholder*='{key}']")

    for el in discovered.get("images", []):
        src = str(el.get("src") or "")
        if any(k in src.lower() for k in ("captcha", "yzm", "validate", "code")):
            bucket = result.setdefault("captcha_image", [])
            if el.get("id"):
                bucket.insert(0, f"#{el['id']}")
            if el.get("class"):
                cls = str(el["class"]).split()[0]
                if cls and not cls.startswith("el-"):
                    bucket.append(f"img.{cls}")
            bucket.append("img[src*='captcha']")

    for el in discovered.get("canvases", []):
        bucket = result.setdefault("captcha_image", [])
        if el.get("id"):
            bucket.append(f"canvas#{el['id']}")
        elif el.get("class"):
            bucket.append(f"canvas.{str(el['class']).split()[0]}")

    for el in discovered.get("buttons", []):
        text = str(el.get("text") or "")
        if "查验" in text:
            bucket = result.setdefault("submit", [])
            if el.get("id"):
                bucket.append(f"#{el['id']}")
            bucket.append(f"button:has-text('{text.strip()}')")
        if "确定" in text:
            bucket.setdefault("date_picker_confirm", []).append(
                f"button:has-text('{text.strip()}')")
        if "看不清" in text or "换一张" in text:
            bucket.setdefault("captcha_refresh", []).append(
                f"text={text.strip()}")

    for el in discovered.get("links", []):
        if "查验" in str(el.get("text") or ""):
            result.setdefault("submit", []).append(
                f"a:has-text('{str(el['text']).strip()}')")

    # 去重、保序
    for key, values in result.items():
        seen: set[str] = set()
        result[key] = [v for v in values if v and not (v in seen or seen.add(v))]
    return result


async def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    url = args.url or str(cfg.get("platform.home_url"))
    out_path = Path(args.out)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("✗ 没有安装 playwright，无法探测。请用项目镜像运行本脚本。", file=sys.stderr)
        return 2

    print(f"→ 正在打开 {url}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not args.headed,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1000}, ignore_https_errors=True,
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"✗ 打不开页面：{exc}", file=sys.stderr)
            await browser.close()
            return 3

        if args.wait:
            print(f"→ 等待 {args.wait} 秒让页面渲染完…")
            await asyncio.sleep(args.wait)

        discovered = await page.evaluate(_COLLECT_JS)

        if args.screenshot:
            shot = Path(args.screenshot)
            shot.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), full_page=True)
            print(f"→ 截图已保存：{shot}")

        await browser.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(discovered, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ---- 人看的报告 ----
    print()
    print("=" * 68)
    print(f"页面标题：{discovered.get('title')}")
    print(f"最终地址：{discovered.get('url')}")
    print("=" * 68)

    print(f"\n【输入框】{len(discovered['inputs'])} 个")
    for el in discovered["inputs"]:
        print(f"  id={el.get('id','-'):<22} name={el.get('name','-'):<16} "
              f"placeholder={el.get('placeholder','-')}")

    print(f"\n【按钮】{len(discovered['buttons'])} 个")
    for el in discovered["buttons"]:
        print(f"  text={el.get('text','')!r:<18} id={el.get('id','-')} "
              f"class={str(el.get('class','-'))[:40]}")

    print(f"\n【图片】{len(discovered['images'])} 个")
    for el in discovered["images"]:
        src = str(el.get("src", ""))[:70]
        if src.startswith("data:"):
            src = src[:30] + "…(内联)"
        print(f"  id={el.get('id','-'):<18} src={src}")

    if discovered["canvases"]:
        print(f"\n【canvas】{len(discovered['canvases'])} 个（验证码可能画在这里）")
        for el in discovered["canvases"]:
            print(f"  id={el.get('id','-')} class={el.get('class','-')} "
                  f"size={el['box']['w']}x{el['box']['h']}")

    if discovered["iframes"]:
        print(f"\n【iframe】{len(discovered['iframes'])} 个（表单可能在 iframe 里）")
        for el in discovered["iframes"]:
            print(f"  id={el.get('id','-')} src={str(el.get('src',''))[:70]}")

    suggested = suggest(discovered)

    print()
    print("=" * 68)
    print("把下面这段粘进 config.yaml 的 platform.selectors（按需删掉多余的）：")
    print("=" * 68)
    print()
    if not suggested:
        print("# 没有自动推导出任何选择器。可能原因：")
        print("#   1) 表单在 iframe 里 —— 看上面的 iframe 列表")
        print("#   2) 页面需要先点击某个页签才渲染表单 —— 加 --wait 或先手动点")
        print("#   3) 页面结构大改 —— 看上面的原始清单手工写")
    else:
        print("  selectors:")
        for key in sorted(suggested):
            print(f"    {key}:")
            for sel in suggested[key]:
                print(f"      - \"{sel}\"")

    # 页面上的短文本：用来核对 result_keywords
    if discovered.get("clickables"):
        print()
        print("-" * 68)
        print("页面上出现的短文本（可用来核对 result_keywords）：")
        print("  " + " | ".join(discovered["clickables"][:60]))

    print()
    print(f"完整清单已保存：{out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="抓取查验平台页面元素，用于修复 config.yaml 里的选择器")
    parser.add_argument("--url", default=None, help="覆盖 platform.home_url")
    parser.add_argument("--headed", action="store_true", help="开有头浏览器")
    parser.add_argument("--wait", type=float, default=5.0, help="加载后额外等待秒数")
    parser.add_argument("--out", default="discover.json", help="结果保存路径")
    parser.add_argument("--screenshot", default="discover.png",
                        help="整页截图路径（设为空字符串则不截图）")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
