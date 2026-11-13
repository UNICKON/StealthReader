# -*- coding: utf-8 -*-
"""阅读统计口径的自检：字数到底怎么算、"跳"怎么排除。

    python tests/probe_stats.py

只跑纯逻辑（`reading_stats.py`），不起 Qt、不碰真账。
2026-11-13 的账：一本 146 章的书 25 分钟记成 6.5 万字，而同期进度只推进 3 千多字。
原因就是点脚注/目录来回跳，每跳一次都按"读了这么多字"累加。这里把那件事钉成回归用例。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
sys.path.insert(0, SR)

import reading_stats as rs   # noqa: E402

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
        BAD.append("%s  << %s" % (name, extra))
        print("  FAIL %s  << %s" % (name, extra))


def section(title):
    print("\n=== %s ===" % title)


def main():
    section("一拍之内往前挪多少字，才算「读了」")
    check("正常翻一页（250 字）照实记", rs.count_forward(250, 250) == (250, False),
          rs.count_forward(250, 250))
    check("滚了三行（60 字）照实记", rs.count_forward(60, 250) == (60, False),
          rs.count_forward(60, 250))
    check("一页半以内（375 字）还当读", rs.count_forward(375, 250) == (375, False),
          rs.count_forward(375, 250))
    check("跳到下一章（3000 字）不算读", rs.count_forward(3000, 250) == (0, True),
          rs.count_forward(3000, 250))
    check("点脚注跳 3.3 万字不算读", rs.count_forward(33232, 250) == (0, True),
          rs.count_forward(33232, 250))
    check("没有位置变化就不记", rs.count_forward(0, 250) == (0, False), rs.count_forward(0, 250))
    check("往回翻（负数）本来就不记", rs.count_forward(-500, 250) == (0, False),
          rs.count_forward(-500, 250))
    check("算不出版式时门槛兜底 400 字", rs.count_forward(401, 0) == (0, True),
          rs.count_forward(401, 0))
    check("算不出版式时小页也照实记", rs.count_forward(400, 0) == (400, False),
          rs.count_forward(400, 0))
    check("小窗口（一页 200 字）门槛不跟着缩到 320 以下",
          rs.count_forward(350, 200) == (350, False), rs.count_forward(350, 200))
    check("大窗口（一页 1200 字）能容下 1900 字",
          rs.count_forward(1900, 1200) == (1900, False), rs.count_forward(1900, 1200))
    check("脏输入不炸", rs.count_forward(None, None) == (0, False), rs.count_forward(None, None))
    check("脏输入不炸（字符串页码）", rs.count_forward(250, "abc") == (250, False),
          rs.count_forward(250, "abc"))

    section("一场的平均速度封顶：1200 字/分")
    check("25 分钟读 1 万字，不动它", rs.clamp_chars(10000, 1500) == (10000, False),
          rs.clamp_chars(10000, 1500))
    check("25 分钟记 6.5 万字 → 砍到 3 万", rs.clamp_chars(64650, 1495) == (1495 * 20, True),
          rs.clamp_chars(64650, 1495))
    check("刚到上限的不砍", rs.clamp_chars(1200, 60) == (1200, False), rs.clamp_chars(1200, 60))
    check("时长是 0 就不砍（另一条路会把它挡掉）", rs.clamp_chars(500, 0) == (500, False),
          rs.clamp_chars(500, 0))
    check("脏输入不炸", rs.clamp_chars("x", "y") == (0, False), rs.clamp_chars("x", "y"))

    section("2026-11-13 那场重演：8 页正文 + 3 次脚注跳")
    page = 250
    events = [page, 33232, page, page, 20000, page, page, 11000, page, page, page]
    chars = 0
    jumped = 0
    for d in events:
        c, j = rs.count_forward(d, page)
        chars += c
        jumped += 1 if j else 0
    raw = sum(events)
    check("按老口径会记成 %.0f 字" % raw, raw == 66232, raw)
    check("新口径只记 8 页（%d 字）" % chars, chars == 8 * page, chars)
    check("3 次跳都被认出来了", jumped == 3, jumped)
    check("一天下来账面上的速度回到人味（%d 字/分）" % (chars / 25),
          chars / 25 < 1500, chars / 25)

    section("记账层：并场、短场、日账（老规矩不能坏）")
    d = rs.blank()
    t0 = 1_760_000_000.0
    rs.add_span(d, "k1", "书", t0, 600, 5000)
    n1 = len(d["sessions"])
    rs.add_span(d, "k1", "书", t0 + 660, 600, 4000)      # 隔 60 秒：并回同一场
    check("隔一分钟的两段并成一场", len(d["sessions"]) == n1 == 1, len(d["sessions"]))
    check("并场后字数相加", d["sessions"][0]["chars"] == 9000, d["sessions"][0]["chars"])
    check("并场后时长相加", d["sessions"][0]["sec"] == 1200, d["sessions"][0]["sec"])
    check("日账只记一条", d["days"][rs.day_key(t0)]["n"] == 1, d["days"][rs.day_key(t0)])

    d2 = rs.blank()
    rs.add_span(d2, "k2", "书", t0, 30, 200)             # 孤零零 30 秒
    check("接不上的 30 秒整段不记", d2["sessions"] == [] and not d2["days"], d2["sessions"])
    check("书账里也不会冒出这本书", "k2" not in d2["books"], list(d2["books"]))

    d3 = rs.blank()
    rs.add_span(d3, "k3", "书", t0, 1800, 20000)
    rs.add_span(d3, "k3", "书", t0 + 7200, 1800, 20000)  # 断 2 小时：新的一场
    check("断两小时算两场", len(d3["sessions"]) == 2, len(d3["sessions"]))
    check("日账 n 记了两次", d3["days"][rs.day_key(t0)]["n"] == 2, d3["days"][rs.day_key(t0)])

    total_before = (d3["days"][rs.day_key(t0)]["sec"], d3["days"][rs.day_key(t0)]["chars"])
    rs.consolidate(d3)
    total_after = (d3["days"][rs.day_key(t0)]["sec"], d3["days"][rs.day_key(t0)]["chars"])
    check("consolidate 只重排会话，不改账", total_before == total_after,
          "%s vs %s" % (total_before, total_after))

    print("\n%d 通过 / %d 失败" % (PASS, FAIL))
    if BAD:
        print("\n".join("  - " + b for b in BAD))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
