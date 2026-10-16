# -*- coding: utf-8 -*-
"""真机联网冒烟：**故意联网**，验"真网络 + 缓存 + 线程 + Qt 信号"这条线真的通。

    python tests/probe_live.py

和另外两个探针的分工：
  - `probe_sources.py` / `probe_ui.py`：零网络，验逻辑和界面（回归主要靠它们）；
  - 这个：只做一次真拉取，看每个站"到底能不能拿到"，连不上就如实报错。

会真下图片和真写缓存（都在 %TEMP% 下，跑完删掉），不会碰 config.json，也不动书架。
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)
os.chdir(SR)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import feed as feed_mod                                                  # noqa: E402
import feed_net                                                          # noqa: E402
import feed_sources                                                      # noqa: E402

CACHE = os.path.join(os.environ["TEMP"], "sr_live_smoke_cache")
shutil.rmtree(CACHE, ignore_errors=True)
FAILS = []
NOTES = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print(("  ok  " if cond else "  FAIL") + " " + name + (("   << " + str(extra)) if extra else ""))


def note(msg):
    NOTES.append(msg)
    print("  note " + msg)


def source(cls):
    """每个来源一个 Http（共用缓存目录，这样跨来源也能看到缓存命中）。"""
    return cls(feed_net.Http(cache=feed_net.Cache(CACHE), headers={"Referer": cls.base_url + "/"}))


def authed(cls):
    """按 build_real_sources 的做法把 feed_auth.json 里的凭据交给来源（配了才带）。"""
    cred = {}
    try:
        with open(os.path.join(SR, "feed_auth.json"), "r", encoding="utf-8") as f:
            node = (json.load(f) or {}).get(cls.key)
        cred = node if isinstance(node, dict) else {}
    except Exception:
        cred = {}
    http = feed_net.Http(cache=feed_net.Cache(CACHE),
                         cookies=feed_sources._cookies_from(cred.get("cookie")),
                         user_agent=cred.get("user_agent") or feed_net.UA,
                         headers={"Referer": cls.base_url + "/"})
    src = cls(http, cred)
    src.needs_auth = bool(getattr(cls, "needs_auth", False))
    return src


print("=== 1. 免登录的三个站，一个个试（拿不到就如实记下来）===")
got = {}
for name, cls in (("IT之家", feed_sources.IthomeSource),
                  ("知乎", feed_sources.ZhihuSource),
                  ("微博", feed_sources.WeiboSource)):
    s = source(cls)
    t0 = time.time()
    try:
        posts, nxt = s.list(s.channels()[0], 0)
        dt = time.time() - t0
        got[name] = s
        check("%s：第一次拉到 %d 条（%.1fs）" % (name, len(posts), dt), len(posts) >= 10, len(posts))
        check("%s：条目有标题和来源标记" % name, all(p.title and p.source for p in posts))
        t0 = time.time()
        again, _ = s.list(s.channels()[0], 0)
        dt2 = time.time() - t0
        check("%s：第二次走缓存，几乎瞬间（%.2fs）" % (name, dt2), dt2 < 2.0, "%.2f" % dt2)
        check("%s：两次内容一致" % name, [p.id for p in again] == [p.id for p in posts])
    except Exception as e:
        note("%s 这次拿不到：%s: %s" % (name, type(e).__name__, e))

if not got:
    note("一个免登录来源都没连上——网络环境变了？下面只验降级路径")
check("缓存目录里有东西了", os.path.isdir(CACHE))

if "IT之家" in got:
    ih, _ = got["IT之家"].detail(got["IT之家"].list("热榜", 0)[0][0])
    check("IT之家：正文能取到且图落地了", ih.text_length() > 100
          and all(not (b.url or "").startswith("http") for b in ih.body), ih.text_length())
if "知乎" in got:
    zd, _ = got["知乎"].detail(got["知乎"].list("热榜", 0)[0][0])
    txt = " ".join(b.text for b in zd.body)
    check("知乎：详情是'真摘要 + 说清正文要登录'", len(txt) > 30 and "登录" in txt, txt[:40])

print("\n=== 2. 混流：连得上的并行合流，连不上的只写页脚 ===")
live = [feed_sources.IthomeSource(feed_net.Http(cache=feed_net.Cache(CACHE))),
        feed_sources.V2exSource(feed_net.Http(cache=feed_net.Cache(CACHE))),
        feed_sources.DiscourseSource(feed_net.Http(cache=feed_net.Cache(CACHE))),
        feed_sources.ZhihuSource(feed_net.Http(cache=feed_net.Cache(CACHE))),
        feed_sources.WeiboSource(feed_net.Http(cache=feed_net.Cache(CACHE)))]
mix = feed_sources.MixSource(live)
t0 = time.time()
mposts, mnxt = mix.list("全部", 0)
dt = time.time() - t0
srcs = sorted(set(p.source for p in mposts))
check("混流出内容", len(mposts) >= 6, len(mposts))
check("并行取数：等待时间没被最慢的站叠加", dt < 20, "%.1fs" % dt)
check("活着的来源都分到了席位", len(srcs) >= 2, srcs)
check("按时间倒序", all((mposts[i].ts or 0) >= (mposts[i + 1].ts or 0)
                     for i in range(len(mposts) - 1)))
note("来源分布 %s（%.1fs）；页脚会说：%s" % (
    dict((s, sum(1 for p in mposts if p.source == s)) for s in srcs), dt,
    mix.last_error or "都连上了"))

print("\n=== 3. 整窗走一遍（真 HTTP + 真线程 + 真信号）===")
import main as app_mod                                                   # noqa: E402
from PyQt5.QtWidgets import QApplication                                 # noqa: E402

LOGS = []
app_mod.save_annotations = lambda data: None
app_mod.log_event = lambda msg: LOGS.append(str(msg))
app_mod.keyboard.add_hotkey = lambda *a, **k: 1
app_mod.keyboard.on_release = lambda cb: (lambda: None)
app_mod.keyboard.remove_hotkey = lambda *a, **k: None
app_mod.keyboard.unhook_all = lambda *a, **k: None
app_mod.GlobalInputHook.install = lambda self, *a, **k: None
try:
    app_mod._user32.SetWindowsHookExW = lambda *a, **k: 0x5EED
except Exception:
    pass
OPENED = []
app_mod.os.startfile = lambda url: OPENED.append(url)

qapp = QApplication.instance() or QApplication(sys.argv)
w = app_mod.StealthReader()
w.save_config = lambda *a, **k: None
w.review_hint = lambda *a, **k: None
w.progress_save_timer.stop()


def pump(until, seconds=40.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.05)
    return False


w.enter_feed_mode()
ok = pump(lambda: len(w.feed_nav.posts) >= 6 and not w.feed_busy)
check("混流第一页出来了", ok, len(w.feed_nav.posts))
check("页脚给了来源与条数", "全部来源" in w.feed_status_text(), w.feed_status_text())
note("页脚：" + w.feed_status_text())
if "知乎" in got:
    while w.feed_source().key != "zhihu":
        w.feed_cycle_source(1)
        pump(lambda: not w.feed_busy, 30.0)
    check("切到知乎并拿到内容", len(w.feed_nav.posts) >= 10, len(w.feed_nav.posts))
    w.feed_nav.index = 0
    w.feed_open()
    pump(lambda: w.feed_nav.view == "detail" and w.feed_nav.detail_loaded, 30.0)
    check("详情打开了并且有正文", len(w.text_edit.toPlainText()) > 50,
          len(w.text_edit.toPlainText()))
    check("O 打开真原帖", w.feed_open_original() is False and OPENED, OPENED[-1:])
check("日志里没有代码异常（只有'这个站连不上'这类实话）",
      all(any(h in m for h in ("连接超时", "404", "401", "403", "Cloudflare", "登录", "432"))
          for m in LOGS if "社区取数失败" in m),
      [m for m in LOGS if "社区取数失败" in m][:3])
w.exit_feed_mode(reason="smoke")
check("退得出来且没在取数", not w.feed_mode and not w.feed_busy)
try:
    w.hide()
    w.deleteLater()
    qapp.processEvents()
except Exception:
    pass

print("\n=== 4. 贴了 Cookie 的站：正文和评论到底拿不拿得到（有 feed_auth.json 才跑）===")
AUTH = os.path.join(SR, "feed_auth.json")
if not os.path.exists(AUTH):
    note("没有 feed_auth.json：跳过。想验就先把浏览器 Cookie 贴进去，再跑 tests/check_auth.py")
else:
    zs = authed(feed_sources.ZhihuSource)
    if not zs.has_auth:
        note("feed_auth.json 里没有知乎的 Cookie，跳过知乎正文/评论")
    else:
        try:
            zposts, _ = zs.list("热榜", 0)
            dp = zposts[0]
            t0 = time.time()
            d, _ = zs.detail(dp)
            body_txt = " ".join(b.text for b in d.body)
            check("知乎（带 Cookie）：正文真拿到了（%d 字，%.1fs）" % (d.text_length(), time.time() - t0),
                  d.text_length() > 100 and "用的是 feed_auth.json 里的 Cookie" in body_txt,
                  body_txt[:40])
            check("知乎（带 Cookie）：正文不外链（图都落到本地了）",
                  all(not (b.url or "").startswith("http") for b in d.body))
            t0 = time.time()
            cms, _ = zs.comments(dp)
            # 条数不设下限：这一条回答可能就几条评论（实测遇到过只有 2 条的）。
            # "评论接对了"由零网络探针拿真夹具验，这里只验"真网络这条路走得通"。
            check("知乎（带 Cookie）：评论这条路通了（%d 条，%.1fs）" % (len(cms), time.time() - t0),
                  len(cms) >= 1, len(cms))
            check("知乎（带 Cookie）：评论作者和正文都在",
                  all(c.author and c.body for c in cms),
                  [(c.author, c.body[0].text[:12]) for c in cms[:2]])
            check("知乎（带 Cookie）：详情和评论共用一次回答请求",
                  len(zs._ans) == 1, list(zs._ans.keys())[:2])
        except Exception as e:
            note("知乎带 Cookie 这条路这次没走通：%s: %s" % (type(e).__name__, e))

shutil.rmtree(CACHE, ignore_errors=True)
print("\n================ 结果 ================")
print("失败 %d 项%s" % (len(FAILS), ("：" + "；".join(FAILS)) if FAILS else "（无）"))
sys.exit(1 if FAILS else 0)
