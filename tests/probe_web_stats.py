# -*- coding: utf-8 -*-
"""网页端到底有没有在采集阅读统计：假后端 + 真 Edge，真读 70 秒，看账本里多没多出一场。

    python tests/probe_web_stats.py

验的是一条链：翻页 → 本机攒会话 → 退回书架时推上去 → 云端账本里多一场会话，
并且**没把原来的日账/别人的会话弄坏**（网页只加自己的会话，日账交给桌面端重算）。
"""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import mini_cdp                      # noqa: E402
import reader_mock_server as mock    # noqa: E402

PASS = 0
FAIL = 0
BAD = []

READ_SECONDS = 70        # 真读这么久（统计按 5 秒一拍记，一场至少要 60 秒才会被记下）


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        BAD.append(name + ("  << %s" % extra if extra else ""))
        print("  FAIL %s  << %s" % (name, extra))


def book_sec(data, key):
    return sum(float(s.get("sec") or 0) for s in (data.get("sessions") or [])
               if str(s.get("k") or "") == key)


def offline_merge():
    """第二步（不联网、不写文件）：桌面端能不能把网页那场会话并进真账本。

    网页只加"会话"，日账/书账由桌面端 merge_stats 重算——这一步就是在真账本上算一遍，
    确认数字是**加上去**的，不是覆盖。
    """
    import io
    import reading_stats as rs          # noqa: E402
    import sync_engine as se            # noqa: E402

    print("\n  -- 桌面端合并（离线，只读真账本） --")
    path = os.path.join(os.path.dirname(HERE), "reading_stats.json")
    if not os.path.isfile(path):
        check("找得到真账本 reading_stats.json", False, path)
        return
    local = json.load(io.open(path, encoding="utf-8"))
    today = time.strftime("%Y-%m-%d")
    before_day = dict((local.get("days") or {}).get(today) or {})
    rows = local.get("sessions") or []
    if not rows:
        check("真账本里有会话可对照", False, "空账本")
        return
    key = str(rows[-1].get("k") or "")
    # 放今天凌晨 00:02，离现有会话足够远：不然会被 consolidate 并进相邻那一场，
    # 秒数再被"墙钟跨度"截掉，看不出到底加上去没有
    start = rs._day_start_epoch(rs.day_key()) + 120
    web = {"k": key, "title": str(rows[-1].get("title") or ""),
           "start": round(start, 3), "end": round(start + 120, 3), "sec": 120, "chars": 2400}
    remote = se.stats_view(local, {}, "web-test")
    remote["sessions"] = list(remote.get("sessions") or []) + [web]
    merged, _notes = se.merge_stats(local, remote)
    after_day = dict((merged.get("days") or {}).get(today) or {})

    check("今天的秒数加上去了（%s → %s，+120）" % (before_day.get("sec"), after_day.get("sec")),
          abs(float(after_day.get("sec") or 0) - float(before_day.get("sec") or 0) - 120) < 1.0,
          after_day.get("sec"))
    check("今天的字数加上去了（%s → %s，+2400）" % (before_day.get("chars"), after_day.get("chars")),
          abs(float(after_day.get("chars") or 0) - float(before_day.get("chars") or 0) - 2400) < 1.0,
          after_day.get("chars"))
    check("那 120 秒确实记在这本书上（%s → %s）" % (book_sec(local, key), book_sec(merged, key)),
          book_sec(merged, key) - book_sec(local, key) >= 119.0,
          book_sec(merged, key) - book_sec(local, key))
    check("书账跟着涨，没被抹掉",
          float(((merged.get("books") or {}).get(key) or {}).get("chars") or 0)
          >= float(((local.get("books") or {}).get(key) or {}).get("chars") or 0) + 2400,
          ((merged.get("books") or {}).get(key) or {}).get("chars"))


def stats_doc():
    return mock.STATE.get("stats") or {}


def main():
    srv, port = mock.start(0)
    base = "http://127.0.0.1:%d" % port
    proc, cdp = mini_cdp.launch()
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Page.navigate", {"url": base + "/r/"})
        cdp.wait_js("!!document.querySelector('#token')", 20, "登录框出现")
        cdp.wait_js("!document.querySelector('#login').hidden", 20, "登录框可见")
        time.sleep(0.6)                     # 等模块把 submit 事件挂上
        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
        cdp.wait_js("document.querySelectorAll('.book').length>0", 25, "书架出来")
        cdp.js("document.querySelector('.book').click(); 1")
        cdp.wait_js("/\\d+%/.test(document.querySelector('#pos').textContent)", 40, "正文渲染完")

        # 先翻几页，让"一页多少字"估出来（不然跳转判定只能退回 400 字的下限）
        for _ in range(3):
            cdp.js("document.querySelector('#view').next(); 1")
            time.sleep(0.6)

        # 真读：每 5 秒翻一页，翻够 READ_SECONDS 秒
        turns = 0
        t0 = time.time()
        while time.time() - t0 < READ_SECONDS:
            cdp.js("""(() => {
                const v = document.querySelector('#view');
                const arr = v.renderer.getContents() || [];
                const d = arr.length && arr[0].doc;
                if (d) d.dispatchEvent(new WheelEvent('wheel', {deltaY: 120, bubbles: true, cancelable: true}));
                return 1;
            })()""")
            turns += 1
            time.sleep(5)
        print("  （真读了 %.0f 秒，翻了 %d 次）" % (time.time() - t0, turns))

        pos_now = cdp.js("document.querySelector('#pos').textContent")
        check("读的这几分钟位置在往前走（%s）" % pos_now, "%" in pos_now, pos_now)

        # 退回书架：flushStats 会把这一场推上去
        cdp.js("document.querySelector('#back').click(); 1")
        cdp.wait_js("!document.querySelector('#shelf-pane').hidden", 20, "回到书架")
        time.sleep(1.5)

        doc = stats_doc()
        sessions = doc.get("sessions") or []
        mine = [s for s in sessions if str(s.get("k") or "") == mock.BOOK["uid"]]
        check("云端账本里多了一场网页会话（共 %d 场）" % len(sessions), len(mine) == 1,
              json.dumps(sessions, ensure_ascii=False)[:200])
        row = mine[0] if mine else {}
        check("会话挂在正确的那本书上（%s）" % row.get("k"), row.get("k") == mock.BOOK["uid"], row.get("k"))
        sec = float(row.get("sec") or 0)
        chars = float(row.get("chars") or 0)
        check("时长记下来了（%.0f 秒，真读了 %d 秒）" % (sec, READ_SECONDS), 60 <= sec <= READ_SECONDS + 15, sec)
        check("字数记下来了（%.0f 字）" % chars, chars > 0, chars)
        check("平均速度没超过 1200 字/分（%.1f 字/秒）" % (chars / sec if sec else 0),
              sec > 0 and chars <= sec * 20 + 1, "%s / %s" % (chars, sec))
        check("会话带书名，桌面端账本能对上（%s）" % str(row.get("title"))[:40], bool(row.get("title")))
        check("会话带起止时间", float(row.get("start") or 0) > 0 and float(row.get("end") or 0) > 0,
              "%s..%s" % (row.get("start"), row.get("end")))

        days = doc.get("days") or {}
        today = time.strftime("%Y-%m-%d")
        cell = days.get(today) or {}
        check("原来的日账没被网页覆盖（今天还是 %s 秒 / %s 字）" % (cell.get("sec"), cell.get("chars")),
              float(cell.get("sec") or 0) >= 2385.0, cell)

        line = cdp.js("(document.querySelector('#shelf-stats')||{}).textContent||''")
        check("回到书架能看到统计那一行（%s）" % line, "分钟" in line, line)

    except Exception as e:
        check("整轮不中途崩", False, "%s" % e)
        traceback.print_exc()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        srv.shutdown()

    offline_merge()

    print("\n%d 通过 / %d 失败" % (PASS, FAIL))
    if BAD:
        print("\n".join("  - " + b for b in BAD))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
