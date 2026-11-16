# -*- coding: utf-8 -*-
"""手机上的"读到第几个字"准不准（和桌面端同一个坐标）。

为什么要单独探这一条：网页那边原来把 foliate 的**字节**比例乘上总字数当字符偏移，
而桌面端的 pos 是"抽出来的全文里第几个字"。两个坐标只有在全文字数和字节数成正比时
才相等，实测同一本《雨》在中段差 1.2%（约十页）。偏移表 schema>=2 会带每个文件的
起始字符偏移，网页据此把 [第几个字] ↔ [第几节 + 节内比例] 精确换算。

这个探针不看网页自己报的数（那只能自证），而是把当前这一页的第一段文字拿去桌面端
抽出来的全文里**反查**真正的字符偏移，再和网页报的数对比。所以：

  ① 桌面端写下第 P 个字 → 手机打开，屏幕上那段字真的在第 P 个字附近吗
  ② 手机此刻会写回去的 pos，和屏幕上那段字真正的偏移差多少

跑法：python tests/probe_progress.py
"""
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import mini_cdp
import reader_mock_server as mock
import probe_web_mobile as P
import main

PASS = 0
FAIL = 0
BAD = []

# 一页大约多少字（19px/1.7 行距、390 宽的手机上实测 150~170）
PAGE = 160
# 允许误差：半个到一个字页。老路（按字节比例）实测 1500+ 字，这里留足区分度
TOL = 600


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        BAD.append(name)
        print("  FAIL %s  << %s" % (name, extra))


def build_truth():
    """桌面端抽出来的全文 → (去掉空白的串, 原串下标表)，用来把屏幕上的字反查成字符偏移。"""
    text, _chapters = main.load_epub_text(mock.BOOK["path"])
    chars = []
    posmap = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch)
            posmap.append(i)
    return "".join(chars), posmap


def true_offset(stripped, posmap, snippet, around=None):
    """把屏幕上那段字反查成字符偏移。

    同一句话在书里可能出现好几次（目录里也列了章节名），所以给一个"大概在这儿"的
    提示 around，取离它最近的那一处；不给就取第一处。
    """
    s = re.sub(r"\s+", "", snippet or "")
    for n in (24, 16, 12, 8):
        if len(s) < n:
            continue
        needle = s[:n]
        hits = []
        k = stripped.find(needle)
        while k >= 0 and len(hits) < 40:
            hits.append(posmap[k])
            k = stripped.find(needle, k + 1)
        if hits:
            if around is None:
                return hits[0]
            return min(hits, key=lambda off: abs(off - around))
    return None


def pos_text(cdp):
    return cdp.js("document.querySelector('#pos').textContent").strip()


def pos_num(txt):
    m = re.search(r"·\s*([\d,]+)\s*/", txt or "")
    return int(m.group(1).replace(",", "")) if m else None


def main_run():
    srv, port = mock.start(0)
    uid = mock.BOOK["uid"]
    idx = main.book_char_index(mock.BOOK["path"])
    n_files = len(idx.get("files") or [])
    check("偏移表 schema>=2 且带 files（%d 个文件 / %d 字）" % (n_files, idx["total"]),
          int(idx.get("schema") or 0) >= 2 and n_files > 1)
    mock.LITE[uid] = dict(idx, uid=uid)
    stripped, posmap = build_truth()
    chs = idx["sections"]
    P0 = chs[9]["start"]                       # 桌面端写下的位置：第 10 章开头
    mock.STATE["progress"] = {"v": 1, "schema": 1, "updated_at": 900, "device": "pc",
                              "books": {uid: {"title": mock.BOOK["name"], "pos": P0,
                                              "total": idx["total"], "at": 900, "dev": "pc"}}}
    print("桌面端：pos=%d / %d 字（第 10 章 %r 开头）" % (P0, idx["total"], chs[9]["title"][:12]))

    proc = None
    try:
        proc, cdp = mini_cdp.launch(port=9252)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 {"width": P.W, "height": P.H, "deviceScaleFactor": 3, "mobile": True})
        cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        cdp.call("Emulation.setUserAgentOverride", {"userAgent": P.IPHONE_UA})
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": P.HOOK})
        cdp.call("Page.navigate", {"url": "http://127.0.0.1:%d/r/" % port})
        cdp.wait_js("!document.querySelector('#login').hidden", 25, "登录框")
        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit();1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 30, "书架")

        print("\n— 打开书：要落在桌面端那个字上 —")
        cdp.js("document.querySelectorAll('.book')[0].click();1")
        cdp.wait_js("window.__docs && window.__docs().length>0", 30, "正文")
        time.sleep(3.0)
        # 记下每次 relocate 时"屏幕最上面那一段字"的原文
        cdp.js("""
          window.__loc = null;
          window.__locs = [];
          document.querySelector('#view').addEventListener('relocate', (e) => {
            const d = e.detail || {}; const r = d.range;
            if (!r || !r.startContainer) return;
            window.__loc = {text: (r.startContainer.textContent || '')
                              .slice(r.startOffset, r.startOffset + 80),
                            index: d.section && d.section.current,
                            fraction: d.fraction};
            window.__locs.push(window.__loc);
          });
          1
        """)
        first = pos_text(cdp)
        n_first = pos_num(first)
        check("打开后报的位置在桌面端那个字附近（%s）" % first,
              n_first is not None and abs(n_first - P0) <= TOL,
              "报 %s，桌面端 %d" % (n_first, P0))

        # 翻一页再翻回来：让 foliate 重新报一次"当前这一页"的 range
        cdp.js("(async () => { await document.querySelector('#view').next(); })();1")
        time.sleep(1.1)
        cdp.js("(async () => { await document.querySelector('#view').prev(); })();1")
        time.sleep(1.1)
        loc = json.loads(cdp.js("JSON.stringify(window.__loc)"))
        back = pos_text(cdp)
        n_back = pos_num(back)
        t1 = true_offset(stripped, posmap, loc.get("text"), around=P0)
        check("屏幕上那段字能在桌面端全文里查到（%r）" % (loc.get("text") or "")[:12], t1 is not None)
        if t1 is not None:
            print("      屏幕这一页真正在第 %d 个字" % t1)
            check("恢复位置准确（真正 %d vs 桌面端 %d，差 %+d 字 / %.1f 页）"
                  % (t1, P0, t1 - P0, (t1 - P0) / float(PAGE)),
                  abs(t1 - P0) <= TOL, "超了 %d 字" % (t1 - P0))
            check("手机写回去的 pos 也准（报 %s vs 真正 %d，差 %+d 字 / %.1f 页）"
                  % (n_back, t1, (n_back or 0) - t1, ((n_back or 0) - t1) / float(PAGE)),
                  n_back is not None and abs(n_back - t1) <= TOL,
                  "报 %s，真正 %d" % (n_back, t1))

        # 再往后翻一页取第二个样本（章内更深处）
        cdp.js("(async () => { await document.querySelector('#view').next(); })();1")
        time.sleep(1.1)
        loc2 = json.loads(cdp.js("JSON.stringify(window.__loc)"))
        n2 = pos_num(pos_text(cdp))
        t2 = true_offset(stripped, posmap, loc2.get("text"), around=(t1 if t1 else P0) + PAGE)
        if t2 is not None and n2 is not None:
            print("      第二个样本：真正 %d，手机报 %d（差 %+d 字）" % (t2, n2, n2 - t2))
            check("第二处也准（差 %+d 字 / %.1f 页）" % (n2 - t2, (n2 - t2) / float(PAGE)),
                  abs(n2 - t2) <= TOL, "超了 %d 字" % (n2 - t2))

        errs = [e for e in json.loads(cdp.js("JSON.stringify(window.__errs||[])") or "[]")
                if "ResizeObserver loop" not in str(e)]
        check("整轮没有 JS 报错", errs == [], errs[:3])
    except Exception as e:
        check("整轮不中途崩", False, "%s" % e)
        traceback.print_exc()
    finally:
        try:
            if proc:
                proc.kill()
        except Exception:
            pass
        srv.shutdown()

    print("\n%d 通过 / %d 失败" % (PASS, FAIL))
    for b in BAD:
        print("  ! " + b)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main_run())
