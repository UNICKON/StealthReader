# -*- coding: utf-8 -*-
"""手机那条路（iPhone 视口 + 触摸 + Mobile UA）：登录、书架、统计、字体、翻页、续章、全屏。

    python tests/probe_web_mobile.py

只验前端在这些条件下能不能用：390×844、mobile=true、真触摸事件、iPhone UA。
真机（iOS Safari / 微信内置浏览器）另说，但"布局挤爆""按钮点不到""换了字体没反应"
"横着一划什么都没发生""滚到底不接下一章"这类问题在这个尺寸下就能露出来。

正文在 foliate 的 closed shadow root 的 iframe 里，外面 querySelector 拿不到，
所以先注入一个 attachShadow 钩子把 shadow root 收着（只在这条探针里用）。
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mini_cdp                      # noqa: E402
import reader_mock_server as mock    # noqa: E402

PASS = 0
FAIL = 0
BAD = []

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
W, H = 390, 844

HOOK = """
window.__errs = [];
addEventListener('error', e => window.__errs.push(String((e.error && e.error.stack) || e.message)));
addEventListener('unhandledrejection', e => window.__errs.push('rejection: '
    + String((e.reason && e.reason.stack) || e.reason)));
window.__shadows = [];
const origAttach = Element.prototype.attachShadow;
Element.prototype.attachShadow = function (init) {
  const s = origAttach.call(this, init);
  try { window.__shadows.push(s) } catch (e) {}
  return s;
};
window.__docs = () => {
  const out = [];
  for (const s of window.__shadows || [])
    for (const f of s.querySelectorAll('iframe'))
      if (f.contentDocument && f.contentDocument.body) out.push(f.contentDocument);
  return out;
};
"""

READ = """JSON.stringify((() => {
  const t = (f, d) => { try { return f() } catch (e) { return d } };
  const v = document.querySelector('#view');
  const root = (window.__shadows || []).find(s => s.host === v) || null;
  const r = v.renderer;
  const docs = window.__docs();
  const d = docs[0] || null;
  const p = d && (d.querySelector('p') || d.body);
  return {
    docs: docs.length,
    paginators: root ? root.querySelectorAll('foliate-paginator').length : -1,
    title: t(() => v.book.metadata.title, null),
    chars: d ? (d.body.textContent || '').trim().length : 0,
    text: d ? (d.body.textContent || '').trim().slice(0, 40) : '',
    font: p ? getComputedStyle(p).fontFamily : null,
    pos: document.querySelector('#pos').textContent,
    start: t(() => Number(r.start), null),
    size: t(() => Number(r.size), null),
    viewSize: t(() => Number(r.viewSize), null),
    scrolled: t(() => r.scrolled, null),
    bare: document.body.classList.contains('bare'),
  };
})())"""


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        BAD.append(name + ("  << %s" % extra if extra else ""))
        print("  FAIL %s  << %s" % (name, extra))


def tap(cdp, x, y, jitter=0):
    """真的发一串触摸事件（不是 dispatchEvent 假造）：headless 里也算真触摸。

    jitter>0 时中途走一小段再松手——真机上"点一下"几乎总会带上十几像素抖动，
    这正是"点了没反应"的病根，所以要能复现。
    """
    cdp.call("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{"x": x, "y": y, "radiusX": 3, "radiusY": 3, "force": 1}],
    })
    time.sleep(0.05)
    if jitter:
        for i in (1, 2):
            cdp.call("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [{"x": x + jitter * i / 2, "y": y + jitter * i / 2,
                                 "radiusX": 3, "radiusY": 3, "force": 1}],
            })
            time.sleep(0.02)
    cdp.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def swipe(cdp, x0, y0, x1, y1, steps=8, dwell=0.014):
    """按住走一段再松手：手指往左（x1<x0）是"想看下一页"。"""
    cdp.call("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{"x": x0, "y": y0, "radiusX": 4, "radiusY": 4, "force": 1}],
    })
    for i in range(1, steps + 1):
        cdp.call("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{"x": x0 + (x1 - x0) * i / steps,
                             "y": y0 + (y1 - y0) * i / steps,
                             "radiusX": 4, "radiusY": 4, "force": 1}],
        })
        time.sleep(dwell)
    cdp.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def view(cdp):
    return json.loads(cdp.js(READ) or "{}")


def real_errs(cdp):
    raw = json.loads(cdp.js("JSON.stringify(window.__errs||[])") or "[]")
    return [e for e in raw if "ResizeObserver loop" not in str(e)]


def add_second_book():
    """往替身服务器里塞第 2 本真 epub：验"回书架换一本书"到底换没换。

    从 config.json 里那几本书挑（按体积从小到大，跳过替身服务器已经在用的那本），
    所以任何一台机器上跑都不用另备素材。
    """
    import hashlib
    try:
        with open(os.path.join(HERE, "..", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None, 0
    paths = [p for p in (cfg.get("local_book_positions") or {}) if os.path.isfile(p)]
    paths.sort(key=lambda q: os.path.getsize(q))
    for p in paths:
        try:
            data = open(p, "rb").read()
        except Exception:
            continue
        sha = hashlib.sha256(data).hexdigest()
        if sha in mock.BLOBS:
            continue                       # 替身服务器已经拿它当"第 1 本"了
        mock.BLOBS[sha] = {"bytes": data, "name": "第二本.epub"}
        return sha, len(data)
    return None, 0


def main():
    srv, port = mock.start(0)
    base = "http://127.0.0.1:%d" % port
    proc, cdp = mini_cdp.launch(port=9224)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": W, "height": H, "deviceScaleFactor": 3, "mobile": True,
        })
        cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        cdp.call("Emulation.setUserAgentOverride", {"userAgent": IPHONE_UA})
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        cdp.call("Page.navigate", {"url": base + "/r/"})
        cdp.wait_js("!document.querySelector('#login').hidden", 25, "登录框可见")

        print("\n— 登录页在 390 宽的屏幕上 —")
        rect = json.loads(cdp.js("JSON.stringify((() => {"
                                 "const b = document.querySelector('#login-go').getBoundingClientRect();"
                                 "const i = document.querySelector('#token').getBoundingClientRect();"
                                 "return {bw: b.width, bx: b.left, br: b.right, iw: i.width,"
                                 " ih: i.height, vh: window.innerHeight}})())"))
        check("输入框放得下（%.0f 宽 / 屏宽 %d）" % (rect["iw"], W), rect["iw"] <= W - 20, rect)
        check("「进去」按钮在屏幕里（right=%.0f < %d）" % (rect["br"], W), rect["br"] <= W, rect)
        check("输入框高度适合点（%.0f px）" % rect["ih"], rect["ih"] >= 34, rect["ih"])
        check("token 默认是遮住的（type=password）",
              cdp.js("document.querySelector('#token').type") == "password")
        cdp.js("document.querySelector('#peek').click(); 1")
        time.sleep(0.2)
        check("点「显示」能看见 token（type=text，按钮变「隐藏」）",
              cdp.js("document.querySelector('#token').type") == "text"
              and cdp.js("document.querySelector('#peek').textContent") == "隐藏",
              cdp.js("document.querySelector('#token').type"))
        cdp.js("document.querySelector('#peek').click(); 1")
        time.sleep(0.2)
        check("再点一下又遮回去",
              cdp.js("document.querySelector('#token').type") == "password")

        print("\n— 登录出错时要说人话 —")
        mock.DROP_LOGIN = True
        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        time.sleep(1.2)
        e1 = cdp.js("document.querySelector('#login-err').textContent")
        check("断网时不是没反应，而是报错（%s）" % e1, "连不上" in (e1 or ""), e1)
        mock.DROP_LOGIN = False
        cdp.js("document.querySelector('#token').value='wrong-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        time.sleep(1.2)
        e2 = cdp.js("document.querySelector('#login-err').textContent")
        check("token 不对时也说得明白（%s）" % e2, "token 不对" in (e2 or ""), e2)
        check("报错之后按钮还能再点（没被禁用卡住）",
              not cdp.js("document.querySelector('#login-go').disabled"))

        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 30, "书架出来")
        time.sleep(1.0)

        print("\n— 书架在手机上不许横向挤爆 —")
        ov = json.loads(cdp.js("JSON.stringify((() => {"
                               "const bar = document.querySelector('#shelf-pane .bar');"
                               "const bs = Array.from(document.querySelectorAll('.bar-actions button'))"
                               "  .map(b => +b.getBoundingClientRect().right.toFixed(0));"
                               "return {sw: bar.scrollWidth, cw: bar.clientWidth, rights: bs,"
                               " vw: window.innerWidth}})())"))
        check("标题栏没有溢出（scrollWidth %s ≤ clientWidth %s）" % (ov["sw"], ov["cw"]),
              ov["sw"] <= ov["cw"] + 1, ov)
        check("三个按钮都在屏幕里（%s）" % ov["rights"], all(r <= ov["vw"] for r in ov["rights"]), ov)
        check("书架上有统计那一行（%s）" % cdp.js("(document.querySelector('#shelf-stats')||{}).textContent"),
              "分钟" in (cdp.js("(document.querySelector('#shelf-stats')||{}).textContent") or ""))

        print("\n— 统计页在手机上 —")
        cdp.js("document.querySelector('#stats-btn').click(); 1")
        cdp.wait_js("!document.querySelector('#stats-pane').hidden", 15, "统计页打开")
        time.sleep(0.8)
        ov2 = json.loads(cdp.js("JSON.stringify((() => {"
                                "const bar = document.querySelector('#stats-pane .bar');"
                                "const body = document.querySelector('#stats-body');"
                                "return {sw: bar.scrollWidth, cw: bar.clientWidth,"
                                " bw: body.scrollWidth, vw: window.innerWidth}})())"))
        check("统计页标题栏没溢出（%s ≤ %s）" % (ov2["sw"], ov2["cw"]), ov2["sw"] <= ov2["cw"] + 1, ov2)
        check("统计内容没横向溢出（%s ≤ %s）" % (ov2["bw"], ov2["vw"]), ov2["bw"] <= ov2["vw"] + 1, ov2)
        check("统计页有每本书", cdp.js("document.querySelectorAll('#stats-body .s-book').length") >= 1)
        cdp.js("document.querySelector('#stats-close').click(); 1")
        time.sleep(0.5)

        print("\n— 打开书：得有字 —")
        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("window.__docs().length>0", 60, "正文文档出来了")
        v0 = view(cdp)
        check("打开就有字，不是一片空白（%d 字）" % v0["chars"], v0["chars"] > 0, v0["chars"])
        if v0["chars"] < 200:
            # 第一屏常常是封面/扉页（只有书名几个字）。等 openBook 的排版收尾，
            # 再按章节号跳进正文（foliate 的 goToFraction 要章节字数表，没准备好时不动）
            time.sleep(1.2)
            cdp.js("(() => { const v = document.querySelector('#view');"
                   "const n = (v.book.sections || []).length;"
                   "v.goTo(Math.floor(n * 0.35)); return 1 })()")
            time.sleep(1.8)
            v0 = view(cdp)
        check("跳进正文后读得到整段字（%d 字）" % v0["chars"], v0["chars"] > 200, v0["chars"])
        check("进度条不是「假装 0%%」（%s）" % v0["pos"], "字" in (v0["pos"] or ""), v0["pos"])
        time.sleep(3.4)
        check("读起来底栏自动收起（body.bare）",
              cdp.js("document.body.classList.contains('bare')"))

        print("\n— 换字体要看得见 —")
        # 书自带的样式表常给 p 写死字体；只有把所有后代都压住，换字体才看得见
        fonts = {}
        for face in ("serif", "sans", "kai"):
            cdp.js("document.querySelector('[data-act=\"face-%s\"]').click(); 1" % face)
            time.sleep(0.6)
            fonts[face] = view(cdp)["font"] or ""
        check("选「宋」正文就是宋体（%s）" % fonts["serif"][:28], "Songti" in fonts["serif"],
              fonts["serif"])
        check("选「楷」正文就是楷体（%s）" % fonts["kai"][:28], "Kaiti" in fonts["kai"], fonts["kai"])
        check("三种字体真的不一样（宋/黑/楷）",
              len({fonts["serif"], fonts["sans"], fonts["kai"]}) == 3, fonts)
        cdp.js("document.querySelector('[data-act=\"face-serif\"]').click(); 1")
        time.sleep(0.4)

        print("\n— 左右翻页（点 + 划）—")
        p0 = view(cdp)
        tap(cdp, int(W * 0.9), int(H * 0.5))       # 点右边三分之一
        time.sleep(0.9)
        p1 = view(cdp)
        check("点右边翻下一页（%s → %s）" % (p0["pos"], p1["pos"]), p1["pos"] != p0["pos"],
              "%s vs %s" % (p0["pos"], p1["pos"]))
        swipe(cdp, 320, int(H * 0.55), 70, int(H * 0.55) + 12)   # 手指往左划
        time.sleep(1.0)
        p2 = view(cdp)
        check("横着往左划也翻页（%s → %s）" % (p1["pos"], p2["pos"]), p2["pos"] != p1["pos"],
              "%s vs %s" % (p1["pos"], p2["pos"]))
        check("翻完停在整页上（没有停在两页中间：start=%s size=%s）"
              % (p2["start"], p2["size"]),
              p2["start"] is not None and p2["size"]
              and abs((p2["start"] / p2["size"]) - round(p2["start"] / p2["size"])) < 0.02,
              p2)
        swipe(cdp, 70, int(H * 0.55), 320, int(H * 0.55) + 12)   # 手指往右划
        time.sleep(1.0)
        p3 = view(cdp)
        check("往右划翻回上一页（%s → %s）" % (p2["pos"], p3["pos"]), p3["pos"] != p2["pos"],
              "%s vs %s" % (p2["pos"], p3["pos"]))
        # 中间那一下：真触摸（不是假造事件）→ 底栏有/无。
        print("\n— 单击屏幕中间：叫出底栏 / 再单击收起 —")
        # 顺手记下最近一次 tap 的分区：失败时能看出是"没抛事件"还是"抛错分区"
        cdp.js("window.__lastTap = null;"
               "document.querySelector('#view').addEventListener('tap', (e) => {"
               " window.__lastTap = {zone: e.detail.zone, x: e.detail.x, raw: e.detail.raw,"
               " start: document.querySelector('#view').renderer.start}; }); 1")
        cdp.js("document.body.classList.add('bare'); 1")
        tap(cdp, W // 2, int(H * 0.45))
        time.sleep(0.45)
        last_zone = json.loads(cdp.js("JSON.stringify(window.__lastTap)") or "null")
        check("单击中间，底栏出来", not cdp.js("document.body.classList.contains('bare')"), last_zone)
        p_mid = view(cdp)["pos"]
        tap(cdp, W // 2, int(H * 0.45))
        time.sleep(0.45)
        check("再单击一下又收回去", cdp.js("document.body.classList.contains('bare')"))
        check("这两下都没顺手翻页（%s）" % p_mid, view(cdp)["pos"] == p_mid,
              "%s vs %s；zone=%s" % (p_mid, view(cdp)["pos"], last_zone))
        # 真机上点一下会带十几像素抖动：以前累积超过 10px 就不算"点"，
        # 结果屏幕上点了半天什么都不发生。这里把那个抖动原样灌进去。
        cdp.js("document.body.classList.add('bare'); 1")
        tap(cdp, W // 2, int(H * 0.45), jitter=12)
        time.sleep(0.45)
        check("手抖了 12px 的单击，照样叫得出底栏",
              not cdp.js("document.body.classList.contains('bare')"))
        check("抖动那一下也没被当成划动去翻页（%s）" % p_mid, view(cdp)["pos"] == p_mid,
              "%s vs %s" % (p_mid, view(cdp)["pos"]))

        print("\n— 点最下面那一条：叫底栏，不翻页 —")
        before = view(cdp)["pos"]
        tap(cdp, W // 2, H - 30)
        time.sleep(0.4)
        check("摸一下屏幕底部，底栏浮出来",
              not cdp.js("document.body.classList.contains('bare')"))
        check("底部那一条不会顺手翻页（%s）" % before, view(cdp)["pos"] == before,
              "%s vs %s" % (before, view(cdp)["pos"]))

        print("\n— 上下滚：滚到底要接下一章 —")
        cdp.js("document.querySelector('[data-act=\"flow-scroll\"]').click(); 1")
        cdp.wait_js("document.querySelector('#view').renderer.scrolled", 15, "切到上下滚")
        time.sleep(1.2)
        s0 = view(cdp)
        swipe(cdp, W // 2, 700, W // 2, 240, steps=6, dwell=0.012)
        time.sleep(0.6)
        s1 = view(cdp)
        check("上下滚能滚动正文（start %s → %s / 屏高 %s）"
              % (s0["start"], s1["start"], s1["viewSize"]),
              s1["start"] is not None and s0["start"] is not None and s1["start"] > s0["start"],
              s0)
        # 一直滑到底（start 不再变为止），再多滑几下：应该换到下一节（文字变了）
        last = view(cdp)["start"]
        for _ in range(60):
            swipe(cdp, W // 2, 720, W // 2, 200, steps=6, dwell=0.010)
            time.sleep(0.22)
            now = view(cdp)["start"]
            if now is not None and last is not None and now <= last + 0.5:
                break
            last = now
        s2 = view(cdp)
        text_before = s2["text"]
        for _ in range(4):
            swipe(cdp, W // 2, 720, W // 2, 200, steps=6, dwell=0.010)
            time.sleep(0.6)
        s3 = view(cdp)
        check("滚到底继续往下，换到下一章（%r → %r）" % (text_before[:16], (s3["text"] or "")[:16]),
              s3["text"] != text_before and (s3["chars"] > 0), "%s vs %s" % (text_before, s3["text"]))
        # 滚动模式下点一下正文：以前这条分支直接 return，点哪儿都没反应，
        # 只有最底下那一条能叫出底栏。
        cdp.js("document.body.classList.add('bare'); 1")
        s_tap = view(cdp)
        tap(cdp, W // 2, int(H * 0.45))
        time.sleep(0.45)
        check("上下滚模式下单击中间也能叫出底栏",
              not cdp.js("document.body.classList.contains('bare')"))
        check("滚动模式下点中间不会翻章（start %s → %s）" % (s_tap["start"], view(cdp)["start"]),
              view(cdp)["start"] == s_tap["start"], "%s vs %s" % (s_tap["start"], view(cdp)["start"]))
        cdp.js("document.querySelector('[data-act=\"flow-page\"]').click(); 1")
        time.sleep(0.9)

        print("\n— 全屏 / 沉浸 —")
        mani = json.loads(cdp.js("""JSON.stringify((() => {
          const l = document.querySelector('link[rel=manifest]');
          return {
            href: l ? l.getAttribute('href') : null,
            apple: (document.querySelector('meta[name=apple-mobile-web-app-capable]')||{}).content || null,
            bar: (document.querySelector('meta[name=apple-mobile-web-app-status-bar-style]')||{}).content || null,
            icon: !!document.querySelector('link[rel=apple-touch-icon]'),
            full: !!document.querySelector('#full'),
          };
        })())"""))
        check("有 manifest、有「加到主屏幕」那套 meta（%s）" % mani["href"],
              bool(mani["href"]) and mani["apple"] == "yes" and mani["icon"], mani)
        check("状态栏是透明压正文的（black-translucent）",
              mani["bar"] == "black-translucent", mani["bar"])
        got = json.loads(cdp.js("(async () => {"
                                "const r = await fetch('/r/manifest.webmanifest');"
                                "const j = await r.json();"
                                "return JSON.stringify({status: r.status, display: j.display,"
                                " override: j.display_override || null,"
                                " icons: (j.icons||[]).length, start: j.start_url});})()",
                                await_promise=True) or "{}")
        check("manifest 拿得到、display=standalone（%s）" % got,
              got.get("status") == 200 and got.get("display") == "standalone"
              and got.get("icons", 0) >= 2, got)
        # 装到主屏幕之后：安卓/桌面先试 fullscreen（连状态栏/任务栏一起没有），
        # iOS 不认 display_override，落到 display: standalone。
        check("装成 App 后先要真全屏（display_override 首项 fullscreen）",
              (got.get("override") or [None])[0] == "fullscreen", got.get("override"))
        icon = json.loads(cdp.js("(async () => {"
                                 "const r = await fetch('/r/icons/icon-192.png');"
                                 "const b = await r.blob();"
                                 "return JSON.stringify({status: r.status, type: r.headers.get('content-type'),"
                                 " size: b.size});})()", await_promise=True) or "{}")
        check("图标能下来（%s）" % icon, icon.get("status") == 200 and icon.get("size", 0) > 500, icon)
        # iPhone 的 Safari 没有 requestFullscreen：按钮要说清楚怎么办
        # （这两个方法是挂在 Element.prototype 上的，删实例上的没用）
        cdp.js("Element.prototype.requestFullscreen = undefined;"
               "Element.prototype.webkitRequestFullscreen = undefined; 1")
        cdp.js("document.querySelector('#full').click(); 1")
        time.sleep(0.5)
        msg = cdp.js("document.querySelector('#toast').textContent")
        check("iPhone 上点全屏会教你「添加到主屏幕」（%s）" % (msg or "")[:30],
              "添加到主屏幕" in (msg or ""), msg)
        # 支持的设备上应该真的去要全屏
        cdp.js("window.__fs = 0;"
               "Element.prototype.requestFullscreen = function () {"
               " window.__fs = 1; return Promise.resolve() }; 1")
        cdp.js("document.querySelector('#full').click(); 1")
        time.sleep(0.4)
        check("能全屏的设备上真的调了 requestFullscreen", cdp.js("window.__fs") == 1)

        print("\n— 回书架 —")
        first_text = view(cdp)["text"]          # 书1 的正文，换书之后要拿它对比
        first_title = view(cdp)["title"]
        sha2, size2 = add_second_book()
        check("替身服务器备好了第 2 本书（%d KB）" % (size2 // 1024), bool(sha2))
        tap(cdp, W // 2, H - 30)
        time.sleep(0.3)
        tap(cdp, 40, H - 30)          # 左下角「← 书架」
        cdp.wait_js("!document.querySelector('#shelf-pane').hidden", 10, "回到书架")
        check("手机上点「← 书架」能回去", cdp.js("!document.querySelector('#shelf-pane').hidden"))

        print("\n— 换一本书：屏幕上不能还是上一本 —")
        cdp.wait_js("document.querySelectorAll('.book').length>1", 15, "书架两本")
        idx = cdp.js("Array.from(document.querySelectorAll('.book'))"
                     ".findIndex(b => b.textContent.indexOf('第二本') >= 0)")
        check("书架上有第 2 本（第 %s 行）" % idx, idx is not None and int(idx) >= 0, idx)
        cdp.js("document.querySelectorAll('.book')[%d].click(); 1" % max(int(idx or 0), 0))
        cdp.wait_js("window.__docs && window.__docs().length>0", 30, "第 2 本正文")
        time.sleep(2.2)
        b2 = view(cdp)
        check("换书后只剩一份正文（renderer %s 个 / iframe %s 个）"
              % (b2["paginators"], b2["docs"]),
              b2["paginators"] == 1 and b2["docs"] == 1, b2)
        check("书名也换了（%r → %r）" % (first_title, b2["title"]),
              bool(b2["title"]) and b2["title"] != first_title, b2["title"])
        if (b2["chars"] or 0) < 20:            # 这本的头一页是空白封面，翻一页再看正文
            cdp.js("document.querySelector('#view').next(); 1")
            time.sleep(1.3)
            b2 = view(cdp)
        check("正文也换了（%r → %r）" % ((first_text or "")[:14], (b2["text"] or "")[:14]),
              bool(b2["text"]) and b2["text"] != first_text and b2["chars"] > 50, b2["text"][:24])
        check("进度也跟着新书重算（%s）" % b2["pos"], "/" in (b2["pos"] or ""), b2["pos"])

        real = real_errs(cdp)
        check("整轮没有 JS 报错", real == [], real[:3])
        noisy = json.loads(cdp.js("JSON.stringify(window.__errs||[])") or "[]")
        noisy = [e for e in noisy if "ResizeObserver loop" in str(e)]
        if noisy:
            print("  （忽略 %d 条浏览器自身的 ResizeObserver 提示）" % len(noisy))
    except Exception as e:
        check("整轮不中途崩", False, "%s" % e)
        traceback.print_exc()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        srv.shutdown()

    print("\n%d 通过 / %d 失败" % (PASS, FAIL))
    for b in BAD:
        print("  ! " + b)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
