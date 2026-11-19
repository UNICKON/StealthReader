# -*- coding: utf-8 -*-
"""手机上的「字体」到底能不能改：真触摸点，按**画出来的字形指纹**判，不看 CSS 字符串。

    python tests/probe_web_font.py

为什么不能只看 computed fontFamily：那只是"浏览器被告知要用的字体列表"，
设备上没装就会静默落到同一张字形上——这正是"点了没反应"的真身。
CJK 每个字都是全角，advance width 恒等于 1em，量宽也量不出来，
所以这里把同一串字真画进 canvas、比像素指纹。

1) 手指点得到：Aa 打开面板、「字体」那一行在视口里、四个按钮 elementFromPoint 命中自己
2) 点下去 CSS 真的进了 iframe（宋/黑/楷/圆各自的 family）
3) **画出来的字形真的不一样**：宋/黑/楷 在本机必须互不相同
4) 换了设备没装的那一档，界面必须**当场说明**（按钮虚线 + 面板里点名 + 点它给提示），
   而不是静默无变化
5) 翻页/换章之后字体还在，没被书自带的样式打回去；字号也确实跟着变
"""
import json
import os
import sys
import time

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

# 读正文文档：字体族、字号、以及"同样这串字用这个 family 画出来"的像素指纹
READ = """(function () {
  var out = {docs: 0};
  var docs = window.__docs();
  out.docs = docs.length;
  var d = docs[0];
  if (!d) { return JSON.stringify(out); }
  var p = d.querySelector('p') || d.body;
  var cv = document.createElement('canvas');
  cv.width = 760; cv.height = 90;
  var g = cv.getContext('2d', {willReadFrequently: true});
  var ink = function (family) {
    g.fillStyle = '#fff'; g.fillRect(0, 0, cv.width, cv.height);
    g.fillStyle = '#000'; g.font = '56px ' + family; g.textBaseline = 'top';
    g.fillText('永和九年岁在癸丑暮春之初', 2, 6);
    var px = g.getImageData(0, 0, cv.width, cv.height).data;
    var h = 2166136261 >>> 0, n = 0;
    for (var i = 0; i < px.length; i += 4) {
      if (px[i] < 200) { n++; }
      h ^= px[i]; h = Math.imul(h, 16777619) >>> 0;
    }
    return {hash: h.toString(16), ink: n};
  };
  var fam = d.defaultView.getComputedStyle(p).fontFamily;
  out.ok = true;
  out.family = fam;
  out.size = parseFloat(d.defaultView.getComputedStyle(p).fontSize);
  out.ink = ink(fam);
  out.text = (d.body.textContent || '').replace(/\\s+/g, '').slice(0, 24);
  out.start = (function () {
    try { return Number(document.querySelector('#view').renderer.start); } catch (e) { return null; }
  })();
  return JSON.stringify(out);
})()"""

BTN = """(function () {
  var el = document.querySelector('[data-act="' + window.__act + '"]');
  if (!el) { return JSON.stringify({found: false}); }
  var r = el.getBoundingClientRect();
  var cx = r.left + r.width / 2;
  var cy = r.top + r.height / 2;
  var hit = document.elementFromPoint(cx, cy);
  return JSON.stringify({
    found: true, cx: cx, cy: cy, w: r.width, h: r.height,
    top: r.top, bottom: r.bottom,
    inView: (r.top >= 0) && (r.bottom <= window.innerHeight),
    hitSelf: (hit === el),
    hitTag: hit ? hit.tagName : null,
    off: el.classList.contains('off'),
    on: el.classList.contains('on'),
    title: el.title || '',
    label: (el.textContent || '').trim(),
  });
})()"""


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
    """真触摸（不是 dispatchEvent 假造）：手机上是手指，不是 JS。"""
    cdp.call("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{"x": x, "y": y, "radiusX": 3, "radiusY": 3, "force": 1}],
    })
    time.sleep(0.06)
    cdp.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def read(cdp):
    return json.loads(cdp.js(READ) or "{}")


def btn(cdp, face):
    cdp.js("window.__act = 'face-%s'" % face)
    return json.loads(cdp.js(BTN) or "{}")


def main():
    srv, port = mock.start(0)
    base = "http://127.0.0.1:%d" % port
    proc, cdp = mini_cdp.launch(port=9225)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": W, "height": H, "deviceScaleFactor": 3, "mobile": True})
        cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        cdp.call("Emulation.setUserAgentOverride", {"userAgent": IPHONE_UA})
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        cdp.call("Page.navigate", {"url": base + "/r/"})
        cdp.wait_js("!document.querySelector('#login').hidden", 25, "登录框")
        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 30, "书架")

        print("\n— 打开一本，进正文 —")
        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("window.__docs().length>0", 60, "正文文档")
        time.sleep(1.2)
        cdp.js("(() => { const v = document.querySelector('#view');"
               "const n = (v.book.sections || []).length;"
               "if (n) v.goTo(Math.floor(n * 0.35)); return 1 })()")
        time.sleep(1.8)
        v0 = read(cdp)
        check("正文出来了（%d 字）" % len(v0.get("text") or ""), len(v0.get("text") or "") > 10,
              v0.get("text"))

        print("\n— 1) 手指点得到 —")
        cdp.js("document.body.classList.remove('bare')")
        time.sleep(0.3)
        r = json.loads(cdp.js("JSON.stringify((() => {"
                              "const b = document.querySelector('#open-tools');"
                              "const q = b.getBoundingClientRect();"
                              "return {x: q.left + q.width/2, y: q.top + q.height/2}})())"))
        tap(cdp, r["x"], r["y"])
        time.sleep(0.6)
        opened = not cdp.js("document.querySelector('#tools').hidden")
        check("手指点「Aa」能打开设置面板", opened)
        for face in ("serif", "sans", "kai", "round"):
            info = btn(cdp, face)
            check("「%s」按钮在屏幕里（top=%.0f bottom=%.0f）"
                  % (info.get("label"), info.get("top", -1), info.get("bottom", -1)),
                  info.get("inView"), info)
            check("「%s」手指按下去命中的是它自己" % info.get("label"), info.get("hitSelf"),
                  "%s %s" % (info.get("hitTag"), info.get("title")))
        check("提示行存在（没装字体时要用它说明）",
              cdp.js("!!document.querySelector('#font-note')"))

        print("\n— 2) 点下去 CSS 真的进了正文 —")
        seen = {}
        for face, want in (("serif", "Songti"), ("sans", "PingFang"),
                           ("kai", "Kaiti"), ("round", "Yuanti")):
            info = btn(cdp, face)
            tap(cdp, info["cx"], info["cy"])
            time.sleep(0.6)
            v = read(cdp)
            seen[face] = v
            check("选「%s」后正文的字体族里带着 %s" % (info["label"], want),
                  want in (v.get("family") or ""), (v.get("family") or "")[:60])
            check("选「%s」后按钮变成选中态" % info["label"], btn(cdp, face)["on"])

        print("\n— 3) 画出来的字形真的不一样（不是同一张）—")
        inks = {k: (v.get("ink") or {}).get("hash") for k, v in seen.items()}
        distinct = len(set(inks.values()))
        check("宋/黑/楷 的墨迹指纹互不相同（%s）" % inks,
              len({inks.get("serif"), inks.get("sans"), inks.get("kai")}) == 3, inks)
        print("     三个主字体的不同字形数：%d；含「圆」共 %d 种" % (distinct, len(set(inks.values()))))

        print("\n— 4) 设备没装的那一档，界面当场说明 —")
        offs = []
        for face in ("serif", "sans", "kai", "round"):
            info = btn(cdp, face)
            is_dup = inks.get(face) == inks.get("sans") and face != "sans"
            offs.append(info.get("off"))
            check("「%s」%s" % (info["label"],
                               "跟黑一样 → 标成没装" if is_dup else "有独立字形 → 不标"),
                  bool(info.get("off")) == bool(is_dup),
                  "off=%s dup=%s" % (info.get("off"), is_dup))
        note = cdp.js("(document.querySelector('#font-note')||{}).textContent || ''")
        if any(offs):
            check("面板里点名说了哪一档没装（%s）" % note, "没装" in note, note)
            # 点它要给提示，不能静默无变化
            for face in ("serif", "sans", "kai", "round"):
                if btn(cdp, face).get("off"):
                    info = btn(cdp, face)
                    cdp.js("document.querySelector('#toast').hidden = true")
                    tap(cdp, info["cx"], info["cy"])
                    time.sleep(0.4)
                    toast = cdp.js("(document.querySelector('#toast')||{}).textContent || ''")
                    check("点没装的那一档会说清楚（%s）" % toast, "没装" in toast, toast)
                    break
        else:
            check("这台设备四个字体都装齐了（不需要提示）", True)

        print("\n— 5) 书自带的 !important 抢不走字体（手机上报的就是这一条）—")
        cdp.js("document.querySelector('[data-act=\"face-kai\"]').click(); 1")
        time.sleep(0.8)
        base5 = read(cdp)
        check("先确认现在是楷体（%s）" % (base5.get("family") or "")[:30],
              "Kaiti" in (base5.get("family") or ""), (base5.get("family") or "")[:50])
        # 模拟中文 EPUB 常见的写法：更具体的选择器 + !important 把字体写死
        cdp.js("""(() => {
          const d = window.__docs()[0];
          const st = d.createElement('style');
          st.id = 'hostile';
          st.textContent = 'p, div, span, body, body * { font-family: "Times New Roman" !important }';
          d.head.appendChild(st);
          return 1
        })()""")
        time.sleep(0.8)
        v5 = read(cdp)
        check("书的 !important 塞进来之后，正文还是楷体（%s）"
              % (v5.get("family") or "")[:30], "Kaiti" in (v5.get("family") or ""),
              (v5.get("family") or "")[:60])
        check("字形也没被书抢走（指纹 %s）" % ((base5.get("ink") or {}).get("hash")),
              (v5.get("ink") or {}).get("hash") == (base5.get("ink") or {}).get("hash"),
              ((v5.get("ink") or {}).get("hash"), (base5.get("ink") or {}).get("hash")))
        cdp.js("(() => { const d = window.__docs()[0];"
               "const st = d.querySelector('#hostile'); if (st) st.remove(); return 1 })()")
        time.sleep(0.4)

        print("\n— 6) 翻页之后还在，字号也跟着变 —")
        before = read(cdp)
        cdp.js("(() => { const v = document.querySelector('#view'); v.next(); return 1 })()")
        time.sleep(1.4)
        after = read(cdp)
        check("翻一页之后还是楷体（%s）" % (after.get("family") or "")[:40],
              "Kaiti" in (after.get("family") or ""), (after.get("family") or "")[:40])
        check("确实翻过去了（start %.3f → %.3f）" % (before.get("start") or 0, after.get("start") or 0),
              (after.get("start") or 0) > (before.get("start") or 0),
              (before.get("start"), after.get("start")))
        size0 = after.get("size")
        cdp.js("document.querySelector('[data-act=\"font+\"]').click(); 1")
        time.sleep(0.8)
        size1 = read(cdp).get("size")
        check("点「A+」正文字号真的变大（%.1f → %.1f）" % (size0 or 0, size1 or 0),
              (size1 or 0) > (size0 or 0), (size0, size1))

        errs = [e for e in json.loads(cdp.js("JSON.stringify(window.__errs||[])") or "[]")
                if "ResizeObserver loop" not in str(e)]
        check("整轮没有 JS 报错", not errs, errs[:2])
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass

    print("\n通过 %d / %d" % (PASS, PASS + FAIL))
    if BAD:
        print("未通过：")
        for b in BAD:
            print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
