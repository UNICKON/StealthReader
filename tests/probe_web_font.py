# -*- coding: utf-8 -*-
"""手机上的「字体」到底能不能改：真触摸点，按**画出来的字形指纹**判，不看 CSS 字符串。

    python tests/probe_web_font.py

为什么不能只看 computed fontFamily：那只是"浏览器被告知要用的字体列表"，
名字认不认得是另一件事——名字认得但字形没换，正文看着就是没反应。
CJK 每个字都是全角，advance width 恒等于 1em，量宽也量不出来，
所以这里把同一串字真画进 canvas、比像素指纹。

宋 / 楷 / 圆 现在**随包发**（web/fonts/，见 tests/build_fonts.py），所以本机四档都该是四种字形，
不依赖这台机器装没装中文字体。断言按这个来：

1) 手指点得到：Aa 打开面板、「字体」那一行在视口里、四个按钮 elementFromPoint 命中自己
2) 点下去 CSS 真的进了 iframe（SR Serif / SR Kai / SR Round 各自生效）
3) **画出来的字形真的不一样**：四档四种墨迹指纹
4) 自备字体真的送到了正文里：`fonts.check` 认得、woff2 真被请求过
5) 书自带的 `!important` 抢不走字体（手机上报的就是这条）
6) 翻页/换章之后字体还在；字号也确实跟着变
"""
import json
import os
import re
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
  window.__out = null;
  var docs = window.__docs();
  var d = docs[0];
  if (!d) { window.__out = JSON.stringify({docs: 0}); return 1; }
  (async function () {
    var out = {docs: docs.length, ok: true};
    var p = d.querySelector('p') || d.body;
    var cs = d.defaultView.getComputedStyle(p);
    out.family = cs.fontFamily || '';
    out.size = parseFloat(cs.fontSize);
    // 自备字体是按需下载的：量墨迹之前先把它等下来，否则量到的是兜底字形（假阴性）
    var first = out.family.split(',')[0].trim();
    try { await d.fonts.load('56px ' + first, '\u6c38\u548c\u4e5d\u5e74\u5c81\u5728\u7678\u4e11'); } catch (e) {}
    var cv = d.createElement('canvas');
    cv.width = 760; cv.height = 90;
    var g = cv.getContext('2d', {willReadFrequently: true});
    g.fillStyle = '#fff'; g.fillRect(0, 0, cv.width, cv.height);
    g.fillStyle = '#000'; g.font = '56px ' + out.family; g.textBaseline = 'top';
    g.fillText('\u6c38\u548c\u4e5d\u5e74\u5c81\u5728\u7678\u4e11\u66ae\u6625\u4e4b\u521d', 2, 6);
    var px = g.getImageData(0, 0, cv.width, cv.height).data;
    var h = 2166136261 >>> 0, n = 0;
    for (var i = 0; i < px.length; i += 4) {
      if (px[i] < 200) { n++; }
      h ^= px[i]; h = Math.imul(h, 16777619) >>> 0;
    }
    out.ink = {hash: h.toString(16), ink: n};
    out.text = (d.body.textContent || '').replace(/\\s+/g, '').slice(0, 24);
    out.start = (function () {
      try { return Number(document.querySelector('#view').renderer.start); } catch (e) { return null; }
    })();
    out.faces = ['SR Serif', 'SR Kai', 'SR Round'].map(function (f) {
      var ok = false;
      try { ok = d.fonts.check('16px "' + f + '"', '\u6c38\u548c\u4e5d\u5e74'); } catch (e) {}
      return f + '=' + ok;
    });
    out.reqs = [];
    try {
      out.reqs = d.defaultView.performance.getEntriesByType('resource')
        .filter(function (e) { return /woff2/.test(e.name); })
        .map(function (e) { return e.name.split('/').pop(); });
    } catch (e) {}
    out.fontStatus = (function () { try { return d.fonts.status; } catch (e) { return '?'; } })();
    window.__out = JSON.stringify(out);
  })();
  return 1;
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
    on: el.classList.contains('on'),
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


def read(cdp, timeout=8.0):
    """异步读：先触发，再等页面把结果写成窗口变量（自备字体要先下载完才量得准）。"""
    cdp.js(READ)
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = cdp.js("window.__out")
        if raw and raw != "null":
            return json.loads(raw)
        time.sleep(0.15)
    return {}


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

        print("\n— 2) 点下去 CSS 真的进了正文 —")
        seen = {}
        for face, want in (("serif", "SR Serif"), ("sans", "PingFang"),
                           ("kai", "SR Kai"), ("round", "SR Round")):
            info = btn(cdp, face)
            tap(cdp, info["cx"], info["cy"])
            time.sleep(2.4 if face != 'sans' else 0.6)
            v = read(cdp)
            seen[face] = v
            reqs = v.get("reqs") or []
            if face != "sans":
                pat = {"serif": r"^sr-serif\.woff2$", "round": r"^sr-round\.woff2$",
                       "kai": r"^lxgwwenkailite-regular-subset-\d+\.woff2$"}[face]
                hit = [n for n in reqs if re.search(pat, n)]
                check("选「%s」真的去下了这一档的字体文件（%d 个分片）"
                      % (info["label"], len(hit)), bool(hit), reqs[:6])
            check("选「%s」后正文的字体族里带着 %s" % (info["label"], want),
                  want in (v.get("family") or ""), (v.get("family") or "")[:60])
            check("选「%s」后按钮变成选中态" % info["label"], btn(cdp, face)["on"])

        print("\n— 3) 画出来的字形真的不一样（不是同一张）—")
        inks = {k: (v.get("ink") or {}).get("hash") for k, v in seen.items()}
        distinct = len(set(inks.values()))
        check("宋/黑/楷 的墨迹指纹互不相同（%s）" % inks,
              len({inks.get("serif"), inks.get("sans"), inks.get("kai")}) == 3, inks)
        check("「圆」也有自己的字形，不再是黑的复制品（%s vs %s）"
              % (inks.get("round"), inks.get("sans")),
              inks.get("round") != inks.get("sans"), inks)
        check("四档一共四种字形（%d）" % distinct, distinct == 4, inks)

        print("\n— 4) 三档自备字体都真的送到了正文里 —")
        # fonts 只在用到时才下载，所以"当前这一档认不认得"要用当前 family 断言，
        # 其余两档看它们有没有被请求过（section 2 里每档都点过）。
        v = read(cdp)
        reqs = v.get("reqs") or []
        families = {
            "serif": [n for n in reqs if n == "sr-serif.woff2"],
            "kai": [n for n in reqs if n.startswith("lxgwwenkailite-regular-subset-")],
            "round": [n for n in reqs if n == "sr-round.woff2"],
        }
        for fam, files in families.items():
            check("「%s」的字体文件被请求过（%d 个）" % (fam, len(files)), bool(files),
                  sorted(reqs)[:6])
        cur = (v.get("family") or "").split(",")[0].strip().strip("'\"")
        check("当前这一档就能在书里解析（%s）" % cur, cur in ("SR Serif", "SR Kai", "SR Round"),
              v.get("family"))
        check("font 状态是 loaded（%s）" % v.get("fontStatus"), v.get("fontStatus") == "loaded",
              v.get("fontStatus"))
        print("\n— 5) 书自带的 !important 抢不走字体（手机上报的就是这一条）—")
        cdp.js("document.querySelector('[data-act=\"face-kai\"]').click(); 1")
        time.sleep(0.8)
        base5 = read(cdp)
        check("先确认现在是楷体（%s）" % (base5.get("family") or "")[:30],
              "SR Kai" in (base5.get("family") or ""), (base5.get("family") or "")[:50])
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
              % (v5.get("family") or "")[:30], "SR Kai" in (v5.get("family") or ""),
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
              "SR Kai" in (after.get("family") or ""), (after.get("family") or "")[:40])
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
