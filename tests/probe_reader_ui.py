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

        def progress_now():
            raw = cdp.js("fetch('/v1/state/progress').then(r=>r.json()).then(j=>JSON.stringify(j))", True)
            return json.loads(raw or "{}")

        # ---- 同步节奏：不做实时；翻页后 3 秒内不许动云端，关页面（pagehide）才推 ----
        pre_books = progress_now().get("books") or {}
        pos_before = cdp.js("document.querySelector('#pos').textContent")
        cdp.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true})); 1")
        time.sleep(0.8)
        pos1 = cdp.js("document.querySelector('#pos').textContent")
        check("按右箭头能翻页（%s → %s）" % (pos_before, pos1), pos1 != pos_before,
              "%s vs %s" % (pos_before, pos1))

        time.sleep(3.2)      # 老版本 2.5 秒就推了；现在这个时间点云端应该一点没动
        mid_books = progress_now().get("books") or {}
        check("翻页后不实时同步（3 秒内云端没动）", mid_books == pre_books, mid_books)

        want_pos = cdp.js("""(() => {
            const m = /([\\d,]+)\\s*\\//.exec(document.querySelector('#pos').textContent);
            return m ? Number(m[1].replace(/,/g,'')) : -1;
        })()""")
        cdp.js("window.dispatchEvent(new Event('pagehide')); 1")
        time.sleep(1.6)
        books = progress_now().get("books") or {}
        row = list(books.values())[0] if books else {}
        check("关页面（pagehide）时把位置推上去", bool(row) and row.get("pos", 0) > 0, row)
        check("推的就是关页面那一刻读到的字位（%s）" % want_pos, row.get("pos") == want_pos,
              "%s vs %s" % (row.get("pos"), want_pos))
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

        # ---- 目录：能开，也必须能关（.toc 的 display:flex 会把 [hidden] 顶掉，这里守回归）----
        cdp.js("document.querySelector('#toc').click(); 1")
        time.sleep(0.3)
        n_toc = cdp.js("document.querySelectorAll('.toc-item').length")
        check("目录能列出来（%d 条）" % (n_toc or 0), (n_toc or 0) >= 1, n_toc)
        check("目录打开时看得见", cdp.js("getComputedStyle(document.querySelector('#toc-panel')).display") != "none")
        cdp.js("document.querySelector('#toc-close').click(); 1")
        time.sleep(0.3)
        disp = cdp.js("getComputedStyle(document.querySelector('#toc-panel')).display")
        check("点 × 能把目录关掉（display=%s）" % disp, disp == "none", disp)
        cdp.js("document.querySelector('#toc').click(); 1")
        time.sleep(0.2)
        cdp.js("document.querySelector('#toc').click(); 1")
        time.sleep(0.3)
        disp2 = cdp.js("getComputedStyle(document.querySelector('#toc-panel')).display")
        check("再点一次目录按钮也能关（display=%s）" % disp2, disp2 == "none", disp2)

        # ---- 版式：单栏 / 双栏 ----
        def attr(name):
            return cdp.js("document.querySelector('#view').renderer.getAttribute('%s')" % name)

        cdp.js("document.querySelector('[data-act=\"cols-2\"]').click(); 1")
        time.sleep(0.8)
        check("切双栏：max-column-count=2", attr("max-column-count") == "2", attr("max-column-count"))
        cdp.js("document.querySelector('[data-act=\"cols-1\"]').click(); 1")
        time.sleep(0.6)
        check("切回单栏：max-column-count=1", attr("max-column-count") == "1", attr("max-column-count"))
        saved = cdp.js("localStorage.getItem('sr.layout.v1')") or ""
        check("版式记在本机（%s）" % saved, "cols" in saved and "flow" in saved, saved)

        # ---- 版式：上下滚动 / 左右翻页 ----
        cdp.js("document.querySelector('[data-act=\"flow-scroll\"]').click(); 1")
        time.sleep(1.0)
        check("切滚动：renderer flow=scrolled", attr("flow") == "scrolled", attr("flow"))
        rolled = cdp.js("""(async () => {
            const v = document.querySelector('#view');
            const r = v.renderer;
            const before = r.start;
            const arr = r.getContents() || [];
            const d = arr.length && arr[0].doc;
            if (d) d.dispatchEvent(new WheelEvent('wheel', {deltaY: 500, bubbles: true, cancelable: true}));
            await new Promise(r2 => setTimeout(r2, 400));
            return JSON.stringify({before, after: r.start, view: r.viewSize, scrolled: !!r.scrolled});
        })()""", True)
        roll = json.loads(rolled or "{}")
        check("滚动模式下滚轮能滚（start %s → %s）" % (roll.get("before"), roll.get("after")),
              (roll.get("after") or 0) > (roll.get("before") or 0), rolled)
        check("滚动模式下 renderer.scrolled=true", roll.get("scrolled") is True, rolled)

        cdp.js("document.querySelector('[data-act=\"flow-page\"]').click(); 1")
        time.sleep(1.2)
        check("切回翻页：renderer flow=paginated", attr("flow") == "paginated", attr("flow"))
        pos_c = cdp.js("document.querySelector('#pos').textContent")
        cdp.js("""(() => {
            const v = document.querySelector('#view');
            const arr = v.renderer.getContents() || [];
            const d = arr.length && arr[0].doc;
            if (d) d.dispatchEvent(new WheelEvent('wheel', {deltaY: 120, bubbles: true, cancelable: true}));
            return 1;
        })()""")
        time.sleep(0.9)
        pos_d = cdp.js("document.querySelector('#pos').textContent")
        check("翻页模式下滚轮能翻页（%s → %s）" % (pos_c, pos_d), pos_d != pos_c, "%s vs %s" % (pos_c, pos_d))

        # ---- 版式要能跨刷新记住：存好 → 真的刷新页面 → 再打开一本 ----
        cdp.js("localStorage.setItem('sr.layout.v1', JSON.stringify({flow:'scrolled', cols:2})); 1")
        cdp.call("Page.reload", {"ignoreCache": False})
        time.sleep(2.5)
        if cdp.js("(document.querySelector('#login') && !document.querySelector('#login').hidden) ? 1 : 0"):
            cdp.js("document.querySelector('#token').value='demo-token';"
                   "document.querySelector('#login-form').requestSubmit(); 1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 30, "刷新后书架（带 Cookie）")
        check("刷新后不用再输 token（Cookie 还在）",
              cdp.js("document.querySelectorAll('.book').length") >= 1)
        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("/\\d+%/.test(document.querySelector('#pos').textContent)", 40, "刷新后打开书")
        time.sleep(1.2)
        check("刷新后版式自动回到上次选的（flow=%s）" % attr("flow"), attr("flow") == "scrolled", attr("flow"))
        check("刷新后栏数也记住了（cols=%s）" % attr("max-column-count"),
              attr("max-column-count") == "2", attr("max-column-count"))
        check("刷新后正文照样渲染得出来（%s 个文档）" % cdp.js("document.querySelector('#view').renderer.getContents().length"),
              cdp.js("document.querySelector('#view').renderer.getContents().length") >= 1)

        errs_raw = json.loads(cdp.js("JSON.stringify(window.__errs||[])") or "[]")
        # 切版式（翻页↔滚动/单栏↔双栏）时浏览器会抱怨 ResizeObserver 通知没送完，
        # 这是浏览器自己的提示，不是我们代码的错；其余一律算错。
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
    if BAD:
        print("\n".join("  - " + b for b in BAD))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
