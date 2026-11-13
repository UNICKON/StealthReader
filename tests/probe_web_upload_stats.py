# -*- coding: utf-8 -*-
"""这一轮四件事的端到端验收（假后端 + 真 Edge，无头，走 CDP）：

    python tests/probe_web_upload_stats.py

1. 「回书架」不能被网络卡住——断网（进度 PUT 直接断连）时也得一点就回去；
2. 阅读要沉浸——底栏平时收起来、正文吃满屏，鼠标到底部又浮出来；
3. 有看阅读统计的入口——书架右上角「统计」点进去能看到日账和书账；
4. 网页能自己传书——传一本新 epub，书架上多一行，打开能算总字数、进账。
"""
import io
import json
import os
import sys
import tempfile
import time
import traceback
import zipfile

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


def make_epub(path):
    """造一本最小的合法 epub（三章、正文够长，够验总字数）。"""
    chapters = [("t%d.xhtml" % i, "第%d章 探针" % (i + 1),
                 ("这是一段用来测总字数的正文，故意写长一点。" * 40)) for i in range(3)]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("mimetype")
        z.writestr(info, "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>',
                   compress_type=zipfile.ZIP_DEFLATED)
        manifest = "".join('<item id="t%d" href="t%d.xhtml" media-type="application/xhtml+xml"/>' % (i, i)
                           for i in range(len(chapters)))
        spine = "".join('<itemref idref="t%d"/>' % i for i in range(len(chapters)))
        opf = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:identifier id="id">urn:uuid:probe-upload-book</dc:identifier>'
               '<dc:title>探针上传的书</dc:title><dc:language>zh</dc:language>'
               '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
               '<manifest>' + manifest + '</manifest><spine>' + spine + '</spine></package>')
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        for i, (name, title, body) in enumerate(chapters):
            z.writestr("OEBPS/" + name,
                       '<?xml version="1.0" encoding="UTF-8"?>'
                       '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>%s</title></head>'
                       '<body><h1>%s</h1><p>%s</p></body></html>' % (title, title, body),
                       compress_type=zipfile.ZIP_DEFLATED)
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return buf.getvalue()


def set_file_input(cdp, selector, paths):
    cdp.call("DOM.enable")
    doc = cdp.call("DOM.getDocument", {"depth": -1})
    node = cdp.call("DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": selector})
    cdp.call("DOM.setFileInputFiles", {"files": paths, "nodeId": node["nodeId"]})


def main():
    tmpdir = tempfile.mkdtemp(prefix="sr_upload_")
    epub_path = os.path.join(tmpdir, "probe_upload_book.epub")
    data = make_epub(epub_path)
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    uid = sha[:16]

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
        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 25, "书架出来")
        n0 = cdp.js("document.querySelectorAll('.book').length")

        # ───────── 一、沉浸 ─────────
        print("\n— 沉浸：底栏平时收起来 —")
        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("/\\d+%/.test(document.querySelector('#pos').textContent)", 40, "正文渲染完")
        time.sleep(0.4)
        check("刚打开时底栏是看得见的（先让人知道怎么回去）",
              not cdp.js("document.body.classList.contains('bare')"))
        time.sleep(3.4)
        check("几秒后底栏自己收起来（body.bare）",
              cdp.js("document.body.classList.contains('bare')"))
        check("底栏收起来时点不到（pointer-events=none）",
              cdp.js("getComputedStyle(document.querySelector('#hud')).pointerEvents") == "none",
              cdp.js("getComputedStyle(document.querySelector('#hud')).pointerEvents"))
        pad = cdp.js("(() => {const s = getComputedStyle(document.querySelector('#view-wrap'));"
                     "return s.paddingTop + '|' + s.paddingBottom})()")
        check("正文吃满整屏（上下边距一样，没有给底栏留 64px：%s）" % pad,
              pad.split("|")[0] == pad.split("|")[1], pad)
        # 鼠标挪到底部 → 底栏浮出来
        cdp.js("document.dispatchEvent(new MouseEvent('mousemove',"
               "{clientX: 400, clientY: window.innerHeight - 30, bubbles: true})); 1")
        time.sleep(0.35)
        check("鼠标挪到底部，底栏浮出来（不再 bare）",
              not cdp.js("document.body.classList.contains('bare')"))
        check("浮出来就能点了（pointer-events=auto）",
              cdp.js("getComputedStyle(document.querySelector('#hud')).pointerEvents") != "none")
        check("「← 书架」按钮确实看得见（opacity>0.9）",
              float(cdp.js("getComputedStyle(document.querySelector('#hud')).opacity") or 0) > 0.9,
              cdp.js("getComputedStyle(document.querySelector('#hud')).opacity"))

        # ───────── 二、回书架不能被网络卡住 ─────────
        print("\n— 回书架：断网也点得动 —")
        mock.DROP_PROGRESS_PUT = True
        cdp.js("document.querySelector('#back').click(); 1")
        t0 = time.time()
        ok_shelf = False
        while time.time() - t0 < 1.5:
            if cdp.js("document.querySelector('#shelf-pane').hidden ? 0 : 1"):
                ok_shelf = True
                break
            time.sleep(0.1)
        dt = time.time() - t0
        check("断网时点「书架」也立刻回去了（%.2f 秒）" % dt, ok_shelf)
        check("回书店架上书还在（%d 本）" % cdp.js("document.querySelectorAll('.book').length"),
              cdp.js("document.querySelectorAll('.book').length") >= n0)
        mock.DROP_PROGRESS_PUT = False

        # ───────── 三、统计入口 ─────────
        print("\n— 统计：书架右上角有入口 —")
        check("书架上有「统计」按钮", cdp.js("!!document.querySelector('#stats-btn')"))
        cdp.js("document.querySelector('#stats-btn').click(); 1")
        cdp.wait_js("document.querySelector('#stats-pane') && !document.querySelector('#stats-pane').hidden",
                    15, "统计页打开")
        time.sleep(0.6)
        body = cdp.js("document.querySelector('#stats-body').textContent")
        check("统计页里写着今天读了多少（%s）" % body[:40].replace("\n", " "),
              "分钟" in body and "字" in body, body[:80])
        check("今天的数对得上假后端（2385 秒 → 40 分钟）", "40 分钟" in body,
              [x for x in body.split() if "分钟" in x][:3])
        n_day = cdp.js("document.querySelectorAll('#stats-body .s-row').length")
        check("有「最近几天」的逐日行（%d 行）" % n_day, 1 <= n_day <= 7, n_day)
        n_book = cdp.js("document.querySelectorAll('#stats-body .s-book').length")
        check("有「每本书」的行（%d 行）" % n_book, n_book >= 1, n_book)
        check("书账里带书名（不是光一个 uid）",
              cdp.js("(document.querySelector('#stats-body .s-book .s-title')||{}).textContent||''")
              .strip() != uid)
        cdp.js("document.querySelector('#stats-close').click(); 1")
        time.sleep(0.5)
        check("点 × 回到书架",
              cdp.js("!document.querySelector('#shelf-pane').hidden")
              and cdp.js("document.querySelector('#stats-pane').hidden"))

        # ───────── 四、网页上传书籍 ─────────
        print("\n— 上传：网页自己传一本 —")
        check("书架上有「上传书籍」按钮", cdp.js("!!document.querySelector('#upload-btn')"))
        set_file_input(cdp, "#file", [epub_path])
        cdp.wait_js("document.querySelectorAll('.book').length > %d" % n0, 40, "书架多出一本")
        time.sleep(0.6)
        n1 = cdp.js("document.querySelectorAll('.book').length")
        check("书架上多了一本（%d → %d）" % (n0, n1), n1 == n0 + 1, n1)
        check("假后端收到了这本书（sha 对得上）", sha in mock.BLOBS)
        check("书是按内容存的名字也对（%s）" % mock.BLOBS.get(sha, {}).get("name"),
              mock.BLOBS.get(sha, {}).get("name") == "probe_upload_book.epub",
              mock.BLOBS.get(sha, {}).get("name"))
        titles = cdp.js("JSON.stringify(Array.from(document.querySelectorAll('.b-title'))"
                        ".map(e => e.textContent))")
        check("书架上是原名（%s）" % titles, "probe_upload_book.epub" in (titles or ""))

        # 打开它：网页自己数总字数（桌面端没给它算过）
        cdp.js("""(() => {
            const b = Array.from(document.querySelectorAll('.book'))
              .find(e => (e.querySelector('.b-title').textContent || '').indexOf('probe_upload_book') >= 0);
            b.click(); return 1;
        })()""")
        cdp.wait_js("(document.querySelector('#pos').textContent||'').indexOf('/')>0", 60, "新书算完总字数")
        pos_txt = cdp.js("document.querySelector('#pos').textContent")
        check("新书打开后有进度和总字数（%s）" % pos_txt, "字" in pos_txt, pos_txt)
        idx = mock.LITE.get(uid) or {}
        check("网页把章节表/总字数传上去了（total=%s）" % idx.get("total"),
              int(idx.get("total") or 0) > 1000, idx)
        check("章节表按篇数记（3 篇：%d）" % len(idx.get("sections") or []),
              len(idx.get("sections") or []) == 3, len(idx.get("sections") or []))
        chk_chars = int(idx.get("total") or 0)
        check("总字数跟造的正文量级对得上（%d 字）" % chk_chars, 1000 <= chk_chars <= 6000, chk_chars)
        cdp.js("document.querySelector('#back').click(); 1")
        cdp.wait_js("!document.querySelector('#shelf-pane').hidden", 10, "回到书架")
        time.sleep(1.6)          # 等回来那一下把进度推上去
        row = (mock.STATE.get("progress") or {}).get("books", {}).get(uid) or {}
        check("进度文档里也记了这本书（total=%s）" % row.get("total"),
              int(row.get("total") or 0) > 1000, row)

        # 再传一次同一本：内容寻址，不该多出一行
        set_file_input(cdp, "#file", [epub_path])
        time.sleep(2.4)
        n2 = cdp.js("document.querySelectorAll('.book').length")
        check("同一本再传一次不会多一行（%d）" % n2, n2 == n1, "%d vs %d" % (n2, n1))

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
