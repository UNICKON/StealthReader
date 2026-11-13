# -*- coding: utf-8 -*-
"""手机那条路（iPhone 视口 + 触摸 + Mobile UA）：登录、书架、统计、翻页、沉浸。

    python tests/probe_web_mobile.py

只验前端在这些条件下能不能用：390×844、mobile=true、touch 事件、iPhone UA。
真机（iOS Safari / 微信内置浏览器）另说，但"布局挤爆""按钮点不到""登录没反应"这类
问题在这个尺寸下就能露出来。
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


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        BAD.append(name + ("  << %s" % extra if extra else ""))
        print("  FAIL %s  << %s" % (name, extra))


def tap(cdp, x, y):
    """真的发一串触摸事件（不是 dispatchEvent 假造）：headless 里也算真触摸。"""
    cdp.call("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{"x": x, "y": y, "radiusX": 3, "radiusY": 3, "force": 1}],
    })
    time.sleep(0.06)
    cdp.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


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
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source":
                 "window.__errs=[];"
                 "addEventListener('error',e=>window.__errs.push(String((e.error&&e.error.stack)||e.message)));"
                 "addEventListener('unhandledrejection',e=>window.__errs.push('rejection: '+"
                 "String((e.reason&&e.reason.stack)||e.reason)));"})
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
        # 上面已经确认真触摸能点到按钮（「← 书架」用的是真 tap）；这里用点击省得算坐标
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

        print("\n— 打开书、摸一下、翻页、回去 —")
        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("/\\d+%/.test(document.querySelector('#pos').textContent)", 45, "正文渲染")
        check("正文渲染出来了（%s）" % cdp.js("document.querySelector('#pos').textContent"),
              "%" in cdp.js("document.querySelector('#pos').textContent"))
        time.sleep(3.6)
        check("读起来底栏自动收起（body.bare）",
              cdp.js("document.body.classList.contains('bare')"))
        tap(cdp, W // 2, H - 30)      # 点最下面那一条
        time.sleep(0.4)
        check("摸一下屏幕底部，底栏浮出来",
              not cdp.js("document.body.classList.contains('bare')"))
        pos0 = cdp.js("document.querySelector('#pos').textContent")
        tap(cdp, int(W * 0.9), int(H * 0.5))   # 点右边三分之一翻页
        time.sleep(0.8)
        pos1 = cdp.js("document.querySelector('#pos').textContent")
        check("点右边翻页（%s → %s）" % (pos0, pos1), pos1 != pos0, "%s vs %s" % (pos0, pos1))
        tap(cdp, W // 2, H - 30)
        time.sleep(0.3)
        tap(cdp, 40, H - 30)          # 左下角「← 书架」
        cdp.wait_js("!document.querySelector('#shelf-pane').hidden", 10, "回到书架")
        check("手机上点「← 书架」能回去", cdp.js("!document.querySelector('#shelf-pane').hidden"))

        errs_raw = json.loads(cdp.js("JSON.stringify(window.__errs||[])") or "[]")
        noisy = [e for e in errs_raw if "ResizeObserver loop" in str(e)]
        real = [e for e in errs_raw if "ResizeObserver loop" not in str(e)]
        check("整轮没有 JS 报错", real == [], real)
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
