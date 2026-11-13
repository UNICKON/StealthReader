# -*- coding: utf-8 -*-
"""网页端到底有没有在采集阅读统计：假后端 + 真 Edge，真读几分钟，一段一段验。

    python tests/probe_web_stats.py

  1. 翻页 → 本机攒会话 → 退回书架时推上去 → 云端账本多一场，且没把原来的日账弄坏；
  2. 切到后台（真切标签）→ 立刻收场并推上去（不丢）；切回来接着记；
  3. 推不上去（后端 500）→ 会话留在本机 localStorage，刷新后补推；
  4. 同一场会话重复推 → 云端还是那一场（幂等）；
  5. 离线把同样形状的会话并进真账本 → 数字是加上去的，不是覆盖。
"""
import json
import os
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import mini_cdp                      # noqa: E402
import reader_mock_server as mock    # noqa: E402

PASS = 0
FAIL = 0
BAD = []

READ_SECONDS = 65        # 每段真读这么久（按 5 秒一拍记，一场要过 60 秒才会被记下）
PEND_KEY = "sr.pendingstats.v1"


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


def stats_doc():
    return mock.STATE.get("stats") or {}


def mine_sessions():
    key = mock.BOOK["uid"]
    return [s for s in (stats_doc().get("sessions") or []) if str(s.get("k") or "") == key]


def now_sec():
    return book_sec(stats_doc(), mock.BOOK["uid"])


def read_for(cdp, seconds):
    """真读：每 5 秒翻一页。用 renderer 的 next()——滚轮要 iframe 里那个 doc 才收得到，
    刚打开时正文可能还没铺进去。"""
    t0 = time.time()
    n = 0
    while time.time() - t0 < seconds:
        cdp.js("""(() => {
            const v = document.querySelector('#view');
            if (v.renderer && v.renderer.getContents().length) v.next();
            else v.next();
            return 1;
        })()""")
        n += 1
        time.sleep(5)
    return n


def queue(cdp):
    raw = cdp.js("localStorage.getItem('%s')" % PEND_KEY)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def open_book(cdp):
    cdp.js("document.querySelector('.book').click(); 1")
    cdp.wait_js("/\\d+%/.test(document.querySelector('#pos').textContent)", 40, "正文渲染完")
    time.sleep(1.0)


def back_to_shelf(cdp):
    cdp.js("document.querySelector('#back').click(); 1")
    cdp.wait_js("!document.querySelector('#shelf-pane').hidden", 20, "回到书架")
    time.sleep(1.5)


def reload_shelf(cdp, label="刷新后书架"):
    cdp.call("Page.reload", {"ignoreCache": False})
    time.sleep(3.0)
    if cdp.js("(function(){var e=document.querySelector('#login');return !!(e&&!e.hidden)})()"):
        cdp.js("document.querySelector('#token').value='demo-token';"
               "document.querySelector('#login-form').requestSubmit(); 1")
    cdp.wait_js("document.querySelectorAll('.book').length>0", 40, label)
    time.sleep(2.5)                     # 进书架后会自动补推


def offline_merge():
    """最后一步（不联网、不写文件）：桌面端能不能把网页那场会话并进真账本。

    网页只加"会话"，日账/书账由桌面端 merge_stats 重算——这一步在真账本上算一遍，
    确认数字是**加上去**的，不是覆盖；同一场并两次也不该再涨。
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

    again, _ = se.merge_stats(merged, remote)
    check("同一场会话再并一次不重复记（秒数还是 %s）"
          % ((again.get("days") or {}).get(today) or {}).get("sec"),
          abs(float(((again.get("days") or {}).get(today) or {}).get("sec") or 0)
              - float(after_day.get("sec") or 0)) < 0.01,
          ((again.get("days") or {}).get(today) or {}).get("sec"))


def main():
    srv, port = mock.start(0)
    base = "http://127.0.0.1:%d" % port
    proc, cdp = mini_cdp.launch(port=9321)
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

        # ---- 1. 真读一段，退回书架就推上去 ----
        open_book(cdp)
        pos0 = cdp.js("document.querySelector('#pos').textContent")
        read_for(cdp, READ_SECONDS)
        pos1 = cdp.js("document.querySelector('#pos').textContent")
        check("读的这段位置在往前走（%s → %s）" % (pos0, pos1), pos0 != pos1, pos1)
        back_to_shelf(cdp)

        sessions = mine_sessions()
        check("云端账本里多了一场网页会话（共 %d 场）" % len(stats_doc().get("sessions") or []),
              len(sessions) == 1, json.dumps(sessions, ensure_ascii=False)[:200])
        row = sessions[0] if sessions else {}
        sec1 = float(row.get("sec") or 0)
        chars1 = float(row.get("chars") or 0)
        check("会话挂在正确的那本书上（%s）" % row.get("k"), row.get("k") == mock.BOOK["uid"], row.get("k"))
        check("时长记下来了（%.0f 秒）" % sec1, sec1 >= READ_SECONDS - 5, sec1)
        check("字数记下来了（%.0f 字）" % chars1, chars1 > 0, chars1)
        check("平均速度没超过 1200 字/分", sec1 > 0 and chars1 <= sec1 * 20 + 1, "%s / %s" % (chars1, sec1))
        check("会话带书名和起止时间", bool(row.get("title")) and float(row.get("start") or 0) > 0
              and float(row.get("end") or 0) > 0, str(row.get("title"))[:30])
        check("推成功之后本机队列空了（%s）" % cdp.js("localStorage.getItem('%s')" % PEND_KEY),
              not queue(cdp))
        days = stats_doc().get("days") or {}
        cell = days.get(time.strftime("%Y-%m-%d")) or {}
        check("原来的日账没被网页覆盖（今天还是 %s 秒）" % cell.get("sec"),
              float(cell.get("sec") or 0) >= 2385.0, cell)

        # ---- 2. 切到后台：立刻收场推上去；切回来接着记 ----
        ver = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % 9321, timeout=5).read())
        b = mini_cdp.CDP(mini_cdp.WS(ver["webSocketDebuggerUrl"]))
        open_book(cdp)
        read_for(cdp, READ_SECONDS)
        t2 = b.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        b.call("Target.activateTarget", {"targetId": t2})
        time.sleep(2.5)
        check("切到别的标签，阅读页变成后台（%s）" % cdp.js("document.visibilityState"),
              cdp.js("document.visibilityState") == "hidden")
        sec_hidden = now_sec()
        check("切后台那一刻这一场就收场推上去了（%.0f → %.0f 秒）" % (sec1, sec_hidden),
              sec_hidden >= sec1 + READ_SECONDS - 8, sec_hidden)

        b.call("Target.closeTarget", {"targetId": t2})
        time.sleep(2.0)
        check("切回来又是可见的（%s）" % cdp.js("document.visibilityState"),
              cdp.js("document.visibilityState") == "visible")
        read_for(cdp, READ_SECONDS)          # 回来接着读，这段也得记上
        back_to_shelf(cdp)
        sec_back = now_sec()
        check("切回来接着记，这段没丢（%.0f → %.0f 秒）" % (sec_hidden, sec_back),
              sec_back >= sec_hidden + READ_SECONDS - 8, sec_back)
        check("切后台前后这几段都在账本里（%d 场，共 %.0f 秒）" % (len(mine_sessions()), sec_back),
              len(mine_sessions()) >= 2 and sec_back >= 3 * READ_SECONDS - 25, sec_back)

        # ---- 3. 推不上去也不能丢：后端 500，会话留在本机，刷新后补推 ----
        fake_start = time.time() - 3600      # 一小时前读的那一场（离现在的会话够远，不会被并掉）
        fake = {"k": mock.BOOK["uid"], "title": "断网那一场", "start": round(fake_start, 3),
                "end": round(fake_start + 120, 3), "sec": 120, "chars": 2400, "gaps": []}
        sec_before_fail = now_sec()
        queued_before = queue(cdp)
        cdp.js("localStorage.setItem('%s', JSON.stringify(%s)); 1"
               % (PEND_KEY, json.dumps(queued_before + [fake], ensure_ascii=False)))
        mock.FAIL_STATS_PUT = True
        reload_shelf(cdp, "刷新后书架（推送会失败）")
        kept = queue(cdp)
        check("推送失败时会话留在本机队列（%d 场）" % len(kept), len(kept) == len(queued_before) + 1, kept)
        check("推送失败时云端没变（%.0f 秒）" % now_sec(), abs(now_sec() - sec_before_fail) < 0.01, now_sec())
        check("本机队列落在 localStorage 里，关掉页面也还在",
              bool(cdp.js("localStorage.getItem('%s')" % PEND_KEY)))

        mock.FAIL_STATS_PUT = False
        reload_shelf(cdp, "再刷新（这次能推）")
        check("刷新后补推成功、队列清空（%s）" % (str(cdp.js("localStorage.getItem('%s')" % PEND_KEY))[:40]),
              not queue(cdp), queue(cdp))
        check("补推之后云端接上了（%.0f → %.0f 秒）" % (sec_before_fail, now_sec()),
              now_sec() >= sec_before_fail + 119.0, now_sec())

        # ---- 4. 幂等：同一场会话重复推，云端还是那一场 ----
        n_before = len(stats_doc().get("sessions") or [])
        sec_before_idem = now_sec()
        cdp.js("localStorage.setItem('%s', JSON.stringify([%s])); 1"
               % (PEND_KEY, json.dumps(fake, ensure_ascii=False)))
        reload_shelf(cdp, "再刷新（重复推同一场）")
        check("同一场重复推不会记两遍（会话 %d → %d 场）" % (n_before, len(stats_doc().get("sessions") or [])),
              len(stats_doc().get("sessions") or []) == n_before, len(stats_doc().get("sessions") or []))
        check("同一场重复推秒数不变（%.0f → %.0f）" % (sec_before_idem, now_sec()),
              abs(now_sec() - sec_before_idem) < 0.01, now_sec())
        check("重复推之后队列也清空了（%s）" % (str(cdp.js("localStorage.getItem('%s')" % PEND_KEY))[:40]),
              not queue(cdp), queue(cdp))

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
