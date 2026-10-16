# -*- coding: utf-8 -*-
"""界面集成探针：**零网络**，离屏 Qt 走完整形窗口（混流 / 切来源 / 详情 / 打开原帖 / 退出）。

    python tests/probe_ui.py

零网络的保证：所有来源换成"真适配器 + 读夹具的替身 Http"，并且把 requests 换成抛异常。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)
os.chdir(SR)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import requests                                                          # noqa: E402
requests.get = requests.post = requests.Session.request = lambda *a, **k: 1 / 0
requests.Session.get = lambda *a, **k: 1 / 0

import feed as feed_mod                                                  # noqa: E402
import feed_sources                                                      # noqa: E402
from feed_net import FeedNetError                                        # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("  ok  " if cond else "  FAIL") + " " + name + (("   << " + str(extra)) if extra else ""))


def fixture(name):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as f:
        return json.load(f)


class FakeHttp(object):
    """读夹具。v2ex 一律超时（本机网络真的连不上，页脚必须说出来）；slow 用来制造"结果还在飞"。"""

    def __init__(self, routes=None, fail=(), cache=None, slow=0.0, stale=False):
        self.routes = routes or {}
        self.fail = list(fail)
        self.cache = cache
        self.slow = slow
        self.served_stale = stale

    def get_json(self, url, ttl=None, what="", headers=None, timeout=None):
        if self.slow:
            time.sleep(self.slow)
        for frag in self.fail:
            if frag in url:
                raise FeedNetError("连接超时")
        for frag, obj in self.routes.items():
            if frag in url:
                return json.loads(json.dumps(obj))
        raise FeedNetError("接口不存在（404）")

    def fetch_image(self, url):
        return ("img_%d.jpg" % (abs(hash(url)) % 100000)) if url else ""

    def get_bytes(self, url, ttl=None, headers=None, max_bytes=0):
        raise FeedNetError("探针里不下载")


class MemCache(object):
    img_dir = os.path.join(os.environ["TEMP"], "sr_ui_probe_img")


F = {n: fixture(n) for n in ("ithome_hot.json", "ithome_newslist.json", "ithome_detail.json",
                            "discourse_latest.json", "discourse_topic.json",
                            "zhihu_hot_list.json", "weibo_hotsearch.json")}


def fake_build(cache_root=None, config=None, log=None):
    """替身来源。IT之家这一家报 served_stale（验页脚会不会说"用的是上次的缓存"）。"""
    ithome = feed_sources.IthomeSource(FakeHttp(
        {"newslistpageget": F["ithome_hot.json"], "newscontent": F["ithome_detail.json"],
         "newslist/news": F["ithome_newslist.json"]},
        cache=MemCache(), stale=True))
    v2ex = feed_sources.V2exSource(FakeHttp(fail=["v2ex.com"], cache=MemCache()))
    linux = feed_sources.DiscourseSource(FakeHttp(
        {"latest.json": F["discourse_latest.json"], "/t/": F["discourse_topic.json"]},
        cache=MemCache()))
    zhihu = feed_sources.ZhihuSource(FakeHttp({"hot-list": F["zhihu_hot_list.json"]},
                                              cache=MemCache()))
    weibo = feed_sources.WeiboSource(FakeHttp({"hotSearch": F["weibo_hotsearch.json"]},
                                              cache=MemCache()))
    return [feed_sources.MixSource([ithome, v2ex, linux, zhihu, weibo]),
            ithome, v2ex, linux, zhihu, weibo]


feed_sources.build_real_sources = fake_build

import main as app_mod                                                   # noqa: E402
from PyQt5.QtWidgets import QApplication                                 # noqa: E402
from PyQt5.QtCore import Qt                                              # noqa: E402

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


def pump(until, seconds=8.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


# 注意：on_feed_key 收的是**键码**（int），不是 QKeyEvent——这条是踩过的坑，
# 包成 QKeyEvent 传进去会静默什么都不做（探针里表现为"按键没反应"）。
EXPECT_KEYS = ["mix", "ithome", "v2ex", "linuxdo", "zhihu", "weibo", "demo"]

print("=== 1. 来源登记与默认落点 ===")
keys = [s.key for s in w.feed_sources]
ORIG_SOURCES = list(w.feed_sources)          # 第 6 节会把来源换成替身，第 8 节要还原
check("顺序：混流 / IT之家 / V2EX / linux.do / 知乎 / 微博 / 示例", keys == EXPECT_KEYS, keys)
check("默认落在混流上", w.feed_source().key == "mix", w.feed_source().key)

print("\n=== 2. 混流：活着的来源都在第一页，挂了的只写页脚 ===")
w.enter_feed_mode()
pump(lambda: len(w.feed_nav.posts) >= 6 and not w.feed_busy)
srcs = sorted(set(p.source for p in w.feed_nav.posts))
check("第一页出来了", len(w.feed_nav.posts) >= 6, len(w.feed_nav.posts))
check("四个连得上的来源都分到了席位",
      srcs == ["ithome", "linuxdo", "weibo", "zhihu"], srcs)
check("V2EX 连不上：页脚写清，其余照常",
      "V2EX" in w.feed_status_text() and len(w.feed_nav.posts) >= 6, w.feed_status_text())
check("页脚还说出了'用的是上次的缓存'", "缓存" in w.feed_status_text(), w.feed_status_text())
check("按时间倒序", all((w.feed_nav.posts[i].ts or 0) >= (w.feed_nav.posts[i + 1].ts or 0)
                     for i in range(len(w.feed_nav.posts) - 1)))
html = w.text_edit.toHtml()
check("知乎的'热度/回答'、微博的'热度'都渲染出来了", "热度" in html and "回答" in html)
check("微博的'新/热/爆'标记进界面了（tags 现在真的会显示）",
      any((">%s</span>" % t) in html for t in ("新", "热", "爆", "沸", "商")),
      [p.tags for p in w.feed_nav.posts if p.tags][:4])

print("\n=== 3. `[` `]` 轮完所有来源 ===")
seq = []
for _ in range(len(EXPECT_KEYS) - 1):
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
    seq.append(w.feed_source().key)
check("六个来源都轮到了", seq == EXPECT_KEYS[1:], seq)
w.feed_cycle_source(1)
pump(lambda: not w.feed_busy)
check("轮回到混流", w.feed_source().key == "mix", w.feed_source().key)

print("\n=== 4. 栏目：只有一栏的来源按 , 不动（不是坏了）===")
while w.feed_source().key != "zhihu":
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
before = (w.feed_nav.channel, len(w.feed_nav.posts))
w.on_feed_key(Qt.Key_Comma)
time.sleep(0.3)
qapp.processEvents()
check("知乎只有热榜一栏，按 , 后栏目和内容都没变",
      (w.feed_nav.channel, len(w.feed_nav.posts)) == before, (w.feed_nav.channel, before))
check("栏目停在一个真实存在的值上（不再显示不存在的'全部'）",
      w.feed_nav.channel in w.feed_source().channels(), w.feed_nav.channel)
while w.feed_source().key != "ithome":
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
c0 = w.feed_nav.channel
w.on_feed_key(Qt.Key_Period)
pump(lambda: not w.feed_busy)
check("IT之家有两栏，按 . 会切过去", w.feed_nav.channel != c0, (c0, w.feed_nav.channel))

print("\n=== 5. 知乎详情 + 打开原帖 ===")
while w.feed_source().key != "zhihu":
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
w.feed_nav.index = 0
w.feed_open()
pump(lambda: w.feed_nav.view == "detail" and not w.feed_busy)
check("进了知乎详情", w.feed_nav.view == "detail", w.feed_nav.view)
texts = " ".join(b.text for b in w.feed_nav.post.body)
check("详情给出热榜里的真摘要", len(texts) > 30, texts[:40])
check("详情直说正文要登录", "登录" in texts)
check("知乎详情没有伪造评论", w.feed_nav.roots == [])
check("O 打开的是真原帖地址", w.feed_open_original() is False
      and OPENED and "zhihu.com/question/" in OPENED[-1], OPENED[-1:])
w.on_feed_key(Qt.Key_Backspace)
pump(lambda: w.feed_nav.view == "list")
check("退格回列表", w.feed_nav.view == "list", w.feed_nav.view)

print("\n=== 6. 视图代次：还在飞的旧结果不许动现在的视图（这一轮修的真 bug）===")


def use_sources(sources):
    w.feed_sources = list(sources)
    w.feed_source_i = 0
    w.feed_gen += 1
    w.feed_nav.reset()
    w.feed_nav.channel = "热榜"


def make_ithome(slow=0.0):
    return feed_sources.IthomeSource(FakeHttp(
        {"newslistpageget": F["ithome_hot.json"], "newscontent": F["ithome_detail.json"]},
        cache=MemCache(), slow=slow))


# 6.1 列表重新取数还在飞的时候按回车进详情：列表结果回来不许把详情顶掉
#     （注意这里**不能**重置导航：重置会把列表清空，那就没得可点，等于没测到那个竞态）
use_sources([make_ithome()])
w.feed_load("list")
pump(lambda: len(w.feed_nav.posts) >= 5 and not w.feed_busy)
check("先用快来源把列表铺好", len(w.feed_nav.posts) >= 5, len(w.feed_nav.posts))
w.feed_sources = [make_ithome(slow=0.6)]   # 换成"慢"来源：列表要 0.6 秒才回来
w.feed_source_i = 0
w.feed_reload()                            # 列表重新取（在飞），列表内容还在
w.feed_nav.index = 0
w.feed_open()                              # 用户立刻回车进详情
pump(lambda: not w.feed_busy and w.feed_nav.detail_loaded, 10.0)
check("慢列表回来之后，人还在详情里（没被顶回列表）", w.feed_nav.view == "detail",
      w.feed_nav.view)
same = bool(w.feed_nav.post is not None and w.feed_nav.posts
            and w.feed_nav.post.id == w.feed_nav.posts[0].id)
check("详情那一条就是列表里选中的那一条", same,
      (w.feed_nav.post.id if w.feed_nav.post else None,
       w.feed_nav.posts[0].id if w.feed_nav.posts else None))
# 6.2 详情还在飞的时候退回列表：详情结果回来不许把视图又弹回详情
use_sources([make_ithome(slow=0.5)])
w.feed_load("list")
pump(lambda: len(w.feed_nav.posts) >= 5 and not w.feed_busy, 10.0)
w.feed_nav.index = 0
w.feed_open()                              # 详情在飞（0.5 秒）
time.sleep(0.05)
qapp.processEvents()
w.feed_back()                              # 立刻退回列表
pump(lambda: not w.feed_busy, 10.0)
check("退回列表之后，晚到的详情结果没有把视图弹回详情", w.feed_nav.view == "list",
      w.feed_nav.view)

print("\n=== 7. 退出：书的状态不许被动过 ===")
w.exit_feed_mode(reason="probe")
check("退得出来", not w.feed_mode)
check("没有后台取数还在跑", not w.feed_busy)
check("日志里的失败都是'这个站连不上'，没有代码异常",
      all(any(h in m for h in ("连接超时", "404", "Cloudflare", "登录"))
          for m in LOGS if "社区取数失败" in m),
      [m for m in LOGS if "社区取数失败" in m][:3])

print("\n=== 8. 离线示例仍是兜底 ===")
w.feed_sources = list(ORIG_SOURCES)          # 把第 6 节换掉的替身来源还原回来
w.feed_source_i = [i for i, s in enumerate(w.feed_sources) if s.key == "demo"][0] \
    if any(s.key == "demo" for s in w.feed_sources) else 0
w.feed_gen += 1
w.feed_nav.reset()
w.enter_feed_mode()
w.feed_load("list")
pump(lambda: w.feed_nav.posts and not w.feed_busy)
check("示例源照常出内容", len(w.feed_nav.posts) >= 5, len(w.feed_nav.posts))
w.exit_feed_mode(reason="probe")
try:
    w.hide()
    w.deleteLater()
    qapp.processEvents()
except Exception:
    pass

ok = sum(1 for _n, c in RESULTS if c)
bad = [n for n, c in RESULTS if not c]
print("\n================ 结果 ================")
print("通过 %d / %d" % (ok, len(RESULTS)))
for n in bad:
    print("  未通过: " + n)
sys.exit(0 if not bad else 1)
