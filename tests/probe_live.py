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
    check("知乎：详情只放问题自己的信息，并说清回答要登录",
          len(txt) > 20 and "登录" in txt, txt[:40])

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
_footer = w.feed_status_text()
# 页脚写的是**当前**来源（config 里记住上次停在哪，所以不能假定是混流——实测人上次停在知乎）；
# 分隔用的是全角空格，别拿 "·" 去认。
check("页脚给了来源与条数",
      "第 1/" in _footer
      and any(s in _footer for s in ("全部来源", "知乎", "IT之家", "V2EX", "linux.do", "微博")),
      _footer)
note("页脚：" + _footer)
if "知乎" in got:
    while w.feed_source().key != "zhihu":
        w.feed_cycle_source(1)
        pump(lambda: not w.feed_busy, 30.0)
    check("切到知乎并拿到内容", len(w.feed_nav.posts) >= 10, len(w.feed_nav.posts))
    w.feed_nav.index = 0
    w.feed_open()
    pump(lambda: w.feed_nav.view == "detail" and w.feed_nav.detail_loaded, 30.0)
    check("详情打开了，里面有东西（问题自己的信息 + 回答那一栏）",
          len(w.text_edit.toPlainText()) > 50, len(w.text_edit.toPlainText()))
    check("O 打开真原帖", w.feed_open_original() is False and OPENED, OPENED[-1:])
# "没有代码异常"这条要按**两类**看：
#   ① 话必须是"这个站连不上/不让我看"这类实话（措辞会随站和网络变，所以只认几个关键词；
#      实测 V2EX 在这台机器上是 SSL 出错，措辞是"证书/连接出错"，第一版的关键词表就没收进去）；
#   ② 绝不能出现 Python 异常类型名或 traceback——那才是代码 bug 混进了失败日志。
_EXC_MARKS = ("Traceback", "KeyError", "TypeError", "AttributeError", "IndexError",
              "ValueError", "ZeroDivisionError", "unhashable", "object has no attribute")
_FAIL_WORDS = ("连接超时", "连接出错", "证书", "超时", "404", "401", "403", "432", "Cloudflare",
               "登录", "被拒绝", "暂时")
_feed_fails = [m for m in LOGS if "社区取数失败" in m]
check("日志里没有代码异常（只有'这个站连不上'这类实话）",
      all((not any(x in m for x in _EXC_MARKS)) and any(h in m for h in _FAIL_WORDS)
          for m in _feed_fails),
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
            cms, _ = zs.comments(dp)
            ans_txt = " ".join(b.text for c in cms for b in c.body)
            check("知乎（带 Cookie）：回答拿到了（%d 条回答 / 正文栏 %d 字，%.1fs）"
                  % (len(cms), d.text_length(), time.time() - t0),
                  len(cms) >= 1 and len(ans_txt) > 100, (len(cms), body_txt[:40]))
            check("知乎（带 Cookie）：正文栏不再塞回答内容（回答归\"评论\"那一栏）",
                  ans_txt[:20] not in body_txt, (body_txt[:30], ans_txt[:30]))
            check("知乎（带 Cookie）：正文/回答都不外链（图都落到本地了）",
                  all(not (b.url or "").startswith("http") for b in d.body)
                  and all(not (b.url or "").startswith("http")
                          for c in cms for b in c.body))
            check("知乎（带 Cookie）：回答作者和正文都在",
                  all(c.author and c.body for c in cms),
                  [(c.author, c.body[0].text[:12]) for c in cms[:2]])
            check("知乎（带 Cookie）：详情和评论共用一次回答请求",
                  len(zs._ans) == 1, list(zs._ans.keys())[:2])
            if not cms:
                note("知乎这次一条回答都没拿到，正文栏写的是原因：%s" % body_txt[:80])
        except Exception as e:
            note("知乎带 Cookie 这条路这次没走通：%s: %s" % (type(e).__name__, e))

    ns = authed(feed_sources.NgaSource)
    if not ns.has_auth:
        note("feed_auth.json 里没有 NGA 的 Cookie，跳过 NGA")
    else:
        try:
            nposts, _ = ns.list("网事杂谈", 0)
            check("NGA（带 Cookie）：列表拿到了（%d 条）" % len(nposts), len(nposts) >= 5, len(nposts))
            check("NGA（带 Cookie）：列表没有把'错误提示'当帖子",
                  all(p.author and p.ts > 0 and "时间超过限制" not in p.title for p in nposts),
                  [(p.title[:12], p.author) for p in nposts[:2]])
            # 新的帖子才读得动：老的会被 NGA 自己挡（"帖子发布或回复时间超过限制"）。
            # 而且 NGA 首页常常是"只丢了个链接"的短帖，所以按前几条里**正文最长**的那条验收
            # （第一版只取第一条读得动的，结果碰上一条 17 字的，把一个正常的帖子判成失败）。
            picked, derr, lens = None, "", []
            for cand in nposts[:4]:
                try:
                    got, _ = ns.detail(cand)
                except Exception as e:
                    derr = "%s: %s" % (type(e).__name__, e)
                    continue
                lens.append(got.text_length())
                if picked is None or got.text_length() > picked.text_length():
                    picked = got
            check("NGA（带 Cookie）：正文拿到了（%d 字）" % (picked.text_length() if picked else 0),
                  bool(picked) and picked.text_length() > 20, lens or derr)
            if picked:
                check("NGA（带 Cookie）：正文不外链",
                      all(not (b.url or "").startswith("http") for b in picked.body))
                cms2, _ = ns.comments(nposts[0])
                note("NGA 第一条的评论拿到 %d 条（作者名 NGA 只给 UID 占位，所以多半是空的）"
                     % len(cms2))
                # 首页有的是 0 回复的短帖，这时"评论 0 条"就是对的答案——所以只在列表说它**有回复**
                # 时才要求评论非空（第一版无条件要求 ≥1，碰上一条 0 回复的就把正常结果判成失败）。
                want = int(getattr(nposts[0], "n_comments", 0) or 0)
                check("NGA（带 Cookie）：评论这条路通了（列表说 %d 条回复，拿到 %d 条）"
                      % (want, len(cms2)), len(cms2) >= 1 if want > 0 else len(cms2) == 0,
                      (want, len(cms2)))
        except Exception as e:
            note("NGA 这条路这次没走通：%s: %s" % (type(e).__name__, e))

    xs = authed(feed_sources.XhsSource)
    if not xs.has_auth:
        note("feed_auth.json 里没有小红书的 Cookie，跳过小红书")
    else:
        try:
            xposts, _ = xs.list("推荐", 0)
            check("小红书（带 Cookie）：列表拿到了（%d 条）" % len(xposts),
                  len(xposts) >= 5, len(xposts))
            check("小红书（带 Cookie）：封面落地了、每条的详情地址都带着 xsec_token",
                  all("xsec_token=" in p.url for p in xposts)
                  and any(p.cover for p in xposts),
                  (sum(1 for p in xposts if p.cover), xposts[0].url[-36:]))
            check("小红书（带 Cookie）：标题和作者都在",
                  all(p.title and p.author for p in xposts[:5]),
                  [(p.title[:12], p.author, p.score) for p in xposts[:3]])
            xd, _ = xs.detail(xposts[0])
            check("小红书（带 Cookie）：正文拿到了（%d 字）" % xd.text_length(),
                  xd.text_length() > 20, xd.title[:16])
            check("小红书（带 Cookie）：正文不外链",
                  all(not (b.url or "").startswith("http") for b in xd.body))
            cms3, _ = xs.comments(xposts[0])
            check("小红书（带 Cookie）：评论如实为空（接口要前端签名，实测 406）",
                  cms3 == [], len(cms3))
        except Exception as e:
            note("小红书这条路这次没走通：%s: %s" % (type(e).__name__, e))

    # 微信读书：只到"读到哪了"。正文那条路（e_*/t_* 那套签名与校验）故意不走，见 weread.py
    print("\n=== 5. 微信读书（要 Cookie，只到\"读到哪了\"）===")
    try:
        import weread                                                    # noqa: E402
        wr = weread.build_weread(cache_root=CACHE, root=SR)
    except Exception as e:
        wr = None
        note("weread.py 没起来：%s: %s" % (type(e).__name__, e))
    if wr is None:
        pass
    elif not wr.available:
        note("feed_auth.json 里没有微信读书的 Cookie，跳过")
    else:
        try:
            t0 = time.time()
            books = wr.shelf()
            check("微信读书（带 Cookie）：书架拿到了（%d 本，%.1fs）" % (len(books), time.time() - t0),
                  len(books) >= 3, len(books))
            # 书名人人都有；作者不一定——自己传上去的书（z-library 那种）微信读书就没有作者，实测 57 本里 2 本
            check("微信读书（带 Cookie）：书名都在，进度是 0-100",
                  all(b["title"] for b in books) and all(0 <= b["progress"] <= 100 for b in books),
                  (sum(1 for b in books if not b["author"]), len(books)))
            check("微信读书（带 Cookie）：每本都有能打开的网页版地址",
                  all(b["url"].startswith("https://weread.qq.com/web/") for b in books),
                  books[0]["url"][:52])
            read = [b for b in books if b["progress"] > 0]
            check("微信读书（带 Cookie）：有读到一半的书（书架不是空的壳）", bool(read),
                  [(b["title"][:10], b["progress"]) for b in read[:3]])
            if read:
                one = read[0]
                chs = wr.chapters(one["id"])
                check("微信读书（带 Cookie）：《%s》章节表拿到了（%d 章）"
                      % (one["title"][:10], len(chs)), len(chs) >= 2, len(chs))
                check("微信读书（带 Cookie）：章节有标题和字数",
                      all(c["title"] for c in chs[:5]) and any(c["words"] > 0 for c in chs),
                      [(c["title"][:10], c["words"]) for c in chs[:3]])
                pr = wr.progress(one["id"])
                check("微信读书（带 Cookie）：进度对得上（%d%%）" % pr["progress"],
                      pr["progress"] == one["progress"], (pr["progress"], one["progress"]))
                check("微信读书（带 Cookie）：当前章在那本书的章节表里",
                      any(c["uid"] == pr["chapter_uid"] for c in chs) or pr["chapter_uid"] == 0,
                      (pr["chapter_uid"], len(chs)))
                check("微信读书（带 Cookie）：没有去碰正文接口（这条线只到\"读到哪了\"）",
                      not hasattr(wr, "content") and not hasattr(wr, "read"))
            # 第二次调必须走缓存（书架是 80KB 的东西，不该每开一次窗口都重下一遍）
            t0 = time.time()
            again = wr.shelf()
            check("微信读书（带 Cookie）：第二次开书架走缓存（%.2fs）" % (time.time() - t0),
                  len(again) == len(books) and (time.time() - t0) < 1.0,
                  "%.2fs" % (time.time() - t0))
        except Exception as e:
            note("微信读书这条路这次没走通：%s: %s" % (type(e).__name__, e))

shutil.rmtree(CACHE, ignore_errors=True)
print("\n================ 结果 ================")
print("失败 %d 项%s" % (len(FAILS), ("：" + "；".join(FAILS)) if FAILS else "（无）"))
sys.exit(1 if FAILS else 0)
