# -*- coding: utf-8 -*-
"""阅读页（/r/）的端到端验收：假后端 + 真 Edge（无头，走 CDP）。

    python tests/probe_reader_ui.py

验的是"人能不能读"这件事本身：登录 → 书架 → 打开一本 → 翻页 → 位置写回 →
改字号写回设置 → 目录能跳。不连线上、不用真 token。
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


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        BAD.append(name + ("  << %s" % extra if extra else ""))
        print("  FAIL %s  << %s" % (name, extra))


def main():
    srv, port = mock.start(0)
    base = "http://127.0.0.1:%d" % port
    proc, cdp = mini_cdp.launch()
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source":
                 "window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));"})
        cdp.call("Page.navigate", {"url": base + "/r/"})

        cdp.wait_js("!!document.querySelector('#token')", 20, "登录框出现")
        cdp.wait_js("!document.querySelector('#login').hidden", 20, "登录框可见")
        check("打开 /r/ 先看到登录框（没登录不给看书）", cdp.js("!document.querySelector('#login').hidden"))

        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 25, "书架出来")
        n_books = cdp.js("document.querySelectorAll('.book').length")
        check("登录后看到书架（%d 本）" % n_books, n_books == 1, n_books)
        title = cdp.js("document.querySelector('.b-title').textContent")
        check("书架上显示书名", bool(title and title.strip()), repr(title))

        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("/\\d+%/.test(document.querySelector('#pos').textContent)", 40, "正文渲染完")
        pos0 = cdp.js("document.querySelector('#pos').textContent")
        check("正文渲染出来了，状态栏有进度（%s）" % pos0, "%" in pos0, pos0)

        # foliate-js 把正文放在 renderer 的 iframe 里，官方入口是 getContents()。
        # 刚打开时常常停在封面那一页（还没铺 iframe），先往后挪一两页再看。
        n_content = 0
        for _ in range(4):
            n_content = cdp.js("document.querySelector('#view').renderer.getContents().length")
            if n_content:
                break
            cdp.js("document.querySelector('#view').next(); 1")
            time.sleep(0.5)
        check("正文内容进到 renderer 里了（%s 个可见文档）" % n_content, n_content >= 1, n_content)
        seen = cdp.js("""(async () => {
            const v = document.querySelector('#view');
            const grab = () => {
                const arr = v.renderer.getContents() || [];
                return arr.map(x => (x.doc && x.doc.body) ? x.doc.body.innerText : '').join('');
            };
            let total = grab().length;
            for (let i = 0; i < 5; i++) {
                v.next();
                await new Promise(r => setTimeout(r, 260));
                total += grab().length;
            }
            return total;
        })()""", True)
        check("连翻 6 页，正文读得到字（累计 %d 字）" % (seen or 0), (seen or 0) > 500, seen)

        style = cdp.js("""(() => {
            const v = document.querySelector('#view');
            const arr = v.renderer.getContents() || [];
            const d = arr.length && arr[0].doc;
            if (!d) return '';
            const st = d.defaultView.getComputedStyle(d.body);
            return JSON.stringify({fs: st.fontSize, lh: st.lineHeight,
                                   p: d.querySelectorAll('p').length,
                                   indent: d.querySelector('p') ? d.defaultView.getComputedStyle(d.querySelector('p')).textIndent : ''});
        })()""")
        info = json.loads(style or "{}")
        check("排版样式注进正文了（%s）" % style,
              str(info.get("fs", "")).endswith("px") and info.get("p", 0) > 0, style)
        check("行距不是压死的 1 倍（%s）" % info.get("lh"),
              float(str(info.get("lh", "0")).replace("px", "") or 0) > float(str(info.get("fs", "0")).replace("px", "") or 0),
              style)

        pos_before = cdp.js("document.querySelector('#pos').textContent")
        cdp.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true})); 1")
        time.sleep(0.8)
        pos1 = cdp.js("document.querySelector('#pos').textContent")
        check("按右箭头能翻页（%s → %s）" % (pos_before, pos1), pos1 != pos_before,
              "%s vs %s" % (pos_before, pos1))

        time.sleep(3.4)      # 等防抖（2.5 秒）
        prog = cdp.js("fetch('/v1/state/progress').then(r=>r.json()).then(j=>JSON.stringify(j))", True)
        doc = json.loads(prog or "{}")
        books = doc.get("books") or {}
        row = list(books.values())[0] if books else {}
        check("翻页后把位置写回云端", bool(row) and row.get("pos", 0) > 0, row)
        check("写回的是字符坐标（pos/total 都有）",
              bool(row.get("total")) and isinstance(row.get("pos"), int), row)
        check("写回的行带 device 标记", str(row.get("dev", "")).startswith("web-"), row.get("dev"))
        check("只写了这一本书（没碰别的行）", len(books) == 1, list(books))

        cdp.js("document.querySelector('#open-tools').click(); 1")
        check("工具面板能打开", cdp.js("!document.querySelector('#tools').hidden"))
        size0 = int(cdp.js("document.querySelector('#v-font').textContent"))
        cdp.js("document.querySelector('[data-act=\"font+\"]').click(); 1")
        time.sleep(1.2)
        size1 = int(cdp.js("document.querySelector('#v-font').textContent"))
        check("点字号加号会变大（%d → %d）" % (size0, size1), size1 == size0 + 1, "%d/%d" % (size0, size1))
        css2 = cdp.js("""(() => {
            const v = document.querySelector('#view');
            const arr = v.renderer.getContents() || [];
            const d = arr.length && arr[0].doc;
            return d ? d.defaultView.getComputedStyle(d.body).fontSize : '';
        })()""")
        check("改完字号正文跟着变（%s）" % css2, css2.startswith(str(size1)), css2)

        sett = json.loads(cdp.js("fetch('/v1/state/settings').then(r=>r.json()).then(j=>JSON.stringify(j))", True) or "{}")
        values = sett.get("values") or {}
        clock = sett.get("clock") or {}
        check("字号写回设置对象", int(values.get("font_size") or 0) == size1, values.get("font_size"))
        check("设置带时间戳（多端靠它判新旧）", float(clock.get("font_size") or 0) > 0, clock.get("font_size"))

        cdp.js("document.querySelector('#toc').click(); 1")
        n_toc = cdp.js("document.querySelectorAll('.toc-item').length")
        check("目录能列出来（%d 条）" % (n_toc or 0), (n_toc or 0) >= 1, n_toc)

        errs = cdp.js("JSON.stringify(window.__errs||[])") or "[]"
        check("整轮没有 JS 报错", json.loads(errs) == [], errs)
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
    if BAD:
        print("\n".join("  - " + b for b in BAD))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
