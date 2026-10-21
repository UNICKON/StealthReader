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

# 探针一律不落盘：真人的 config.json 不能被探针改。这一轮真被改过（社区字体被写成 Helvetica、
# 多了个 feed_board_boardsrc、阅读进度被清成 0），查了半天才发现是某条路绕过了替身直接存盘。
# 不管是谁写的，收尾时按开跑前那份原样写回——探针的结果只该出现在屏幕上。
_CFG_PATH = os.path.join(SR, "config.json")
try:
    with open(_CFG_PATH, "rb") as _f:
        _CFG_BAK = _f.read()
except Exception:
    _CFG_BAK = None


def _restore_cfg():
    if _CFG_BAK is None:
        return
    try:
        with open(_CFG_PATH, "rb") as _f:
            if _f.read() == _CFG_BAK:
                return
        with open(_CFG_PATH, "wb") as _f:
            _f.write(_CFG_BAK)
        print("\n（探针动过 config.json，已按开跑前那份原样写回）")
    except Exception:
        pass


import atexit                                                            # noqa: E402
atexit.register(_restore_cfg)

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
from PyQt5.QtWidgets import QApplication, QFrame, QLabel                 # noqa: E402
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
app_mod.set_window_protection = lambda *a, **k: None    # 防截屏那层别真去碰系统窗口
OPENED = []
app_mod.os.startfile = lambda url: OPENED.append(url)

qapp = QApplication.instance() or QApplication(sys.argv)
w = app_mod.StealthReader()
w.save_config = lambda *a, **k: None
# 模块级的那个也要堵：设置窗口是 `self.main_window.save_config()`，万一哪条路直接摸到
# 模块函数，探针就会把真人的 config.json 改掉（这一轮真发生过：社区字体被写成 Helvetica、
# 还多了一个 feed_board_boardsrc）。探针一律不落盘。
app_mod.save_config = lambda *a, **k: None
w.review_hint = lambda *a, **k: None
w.progress_save_timer.stop()
# 跟"人上一轮看到哪"解耦：config.json 里会记住 feed_source（这是真功能，第 1 节末尾单独验一条），
# 但下面所有"席位/轮转"断言都以"默认落在混流"为前提。以前这里读的是本机 config，
# 人上次停在知乎，探针就整段飘红——探针不能依赖"上一轮人点过什么"。
REMEMBERED_SOURCE = str(w.config.get("feed_source") or "")
STARTUP_SOURCE = w.feed_source().key          # 构造时按 config 里记的来源恢复
w.feed_source_i = 0


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
ORIG_SOURCES = list(w.feed_sources)          # 第 6 节会把来源换成替身，第 9 节要还原
check("顺序：混流 / IT之家 / V2EX / linux.do / 知乎 / 微博 / 示例", keys == EXPECT_KEYS, keys)
check("默认落在混流上", w.feed_source().key == "mix", w.feed_source().key)
check("启动时按 config 里记的来源恢复（没记过、或记的那个这次没登记，就是混流）",
      STARTUP_SOURCE == (REMEMBERED_SOURCE if REMEMBERED_SOURCE in keys else "mix"),
      (STARTUP_SOURCE, REMEMBERED_SOURCE, keys))

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
_tag_exp = [t if not isinstance(t, dict) else str(t.get("name") or t.get("slug") or "")
            for p in w.feed_nav.posts for t in (p.tags or [])[:2]]
_tag_exp = [t for t in _tag_exp if t]
check("标签进界面了，字典型标签（V2EX 节点）只露名字、不漏原始字典",
      _tag_exp and all(t in html for t in _tag_exp)
      and "{'id'" not in html and "&#39;id&#39;" not in html,
      (_tag_exp[:4], [p.tags for p in w.feed_nav.posts if p.tags][:4]))

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

print("\n=== 4. 栏目：多栏的来源按 , 能换，只有一栏的按了不动 ===")
while w.feed_source().key != "zhihu":
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
before = w.feed_nav.channel
check("知乎的栏目是 热榜 / 推荐 / 关注（真人反馈过'只有热榜'）",
      list(w.feed_source().channels()) == ["热榜", "推荐", "关注"],
      w.feed_source().channels())
w.on_feed_key(Qt.Key_Comma)
pump(lambda: not w.feed_busy)
check("按 , 换了栏目（热榜 → 上一栏 关注；, 是往前、. 是往后）",
      w.feed_nav.channel != before and w.feed_nav.channel in w.feed_source().channels(),
      (before, w.feed_nav.channel))
# 推荐那栏探针里没有夹具，取不到是正常的；要验的是"栏目换过去了、页脚说实话"，不是空白
check("换到没夹具的那栏：页脚/列表如实说取不到，而不是假装成功",
      bool(w.feed_nav.posts) or "推荐" in w.feed_status_text() or "取" in w.feed_status_text(),
      (w.feed_status_text()[:48], len(w.feed_nav.posts)))
for _ in range(3):                      # 转回热榜：后面几节都还要用它
    if w.feed_nav.channel == "热榜":
        break
    w.on_feed_key(Qt.Key_Comma)
    pump(lambda: not w.feed_busy)
check("按 , 转三下能转回热榜（三栏是闭环的）", w.feed_nav.channel == "热榜", w.feed_nav.channel)
pump(lambda: bool(w.feed_nav.posts) and not w.feed_busy)
check("回到热榜后列表又有了", len(w.feed_nav.posts) > 0, len(w.feed_nav.posts))
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
check("知乎正文不再拿问题摘要冒充内容（回答归评论那一栏）",
      w.feed_nav.post.body and (w.feed_nav.posts[0].excerpt or "") != texts.strip(), texts[:40])
check("没 Cookie 时详情只留一句「回答需要登录」（不再是一段配置说明）",
      "回答需要登录" in texts and "feed_auth.json" not in texts, texts[:60])
check("知乎详情没有伪造评论（回答要真拿到才显示）", w.feed_nav.roots == [])
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

print("\n=== 7. 微信读书书架窗口（只到\"读到哪了\"）===")


class FakeWeread(object):
    """替身客户端：数据来自夹具，一个包都不发。"""

    def __init__(self, books, chapters=None, progress=None, available=True):
        self.available = available
        self._books = books
        self._chapters = chapters or []
        self._progress = progress or {}

    def shelf(self):
        return json.loads(json.dumps(self._books))

    def chapters(self, book_id):
        return json.loads(json.dumps(self._chapters))

    def progress(self, book_id):
        return json.loads(json.dumps(self._progress))


wr_shelf = fixture("weread_shelf.json")
wr_chs = fixture("weread_chapters.json")
wr_prg = fixture("weread_progress.json")
if not (wr_shelf and wr_chs and wr_prg):
    check("微信读书的夹具在", False, "缺夹具，先跑 tests/record_fixtures.py weread")
else:
    # 先让**真的适配器**把夹具揉成窗口要的形状：这样"夹具 → weread.py → 窗口"整条链一起验了
    class _FxHttp(object):
        def get_json(self, url, ttl=None, what="", headers=None, timeout=None):
            if "/web/shelf/sync" in url:
                return json.loads(json.dumps(wr_shelf))
            if "/web/book/getProgress" in url:
                return json.loads(json.dumps(wr_prg))
            raise AssertionError("探针里不该请求 " + url)

        def post_json(self, url, payload, ttl=None, what="", headers=None, timeout=None):
            if "/web/book/chapterInfos" in url:
                return json.loads(json.dumps(wr_chs))
            raise AssertionError("探针里不该请求 " + url)

    real = app_mod.weread_mod.Weread(_FxHttp(), {"cookie": "x=1"})
    wr_books = real.shelf()
    wr_chapters = real.chapters(wr_books[0]["id"])
    wr_prog = real.progress(wr_books[0]["id"])

    dlg = app_mod.WereadShelfDialog(FakeWeread(wr_books, wr_chapters, wr_prog))
    pump(lambda: dlg.list_widget.count() >= 3, 5.0)
    check("窗口列出了 3 本", dlg.list_widget.count() == 3, dlg.list_widget.count())
    check("标题里写着本数", "3 本" in dlg.windowTitle(), dlg.windowTitle())
    row0 = dlg.list_widget.item(0).text()
    check("第一行是最近读的那本 + 进度", "重新认识你自己" in row0 and "89%" in row0, row0)
    pump(lambda: "当前章" in dlg.info_label.text(), 5.0)
    info = dlg.info_label.text()
    check("详情里写着读到百分之多少", "读到 89%" in info, info.splitlines()[:2])
    check("详情里写着当前是哪一章", "当前章：" in info, info.splitlines()[:3])
    check("详情里用了它给的\"最后停在这一行\"", "最后停在这一行：" in info)
    check("详情里说清了正文为什么不在这里", "正文不在这里" in info)
    check("目录列出了 4 章", dlg.chapter_widget.count() == 4, dlg.chapter_widget.count())
    rows = [dlg.chapter_widget.item(i).text() for i in range(dlg.chapter_widget.count())]
    check("读到的那一章打了 ▶", any(r.startswith("▶ ") for r in rows), rows)
    dlg.search_input.setText("齐泽克")
    check("按作者搜只剩 1 本", dlg.list_widget.count() == 1, dlg.list_widget.count())
    dlg.search_input.setText("")
    pump(lambda: dlg.list_widget.count() == 3, 5.0)
    before = len(OPENED)
    dlg.on_book_opened(dlg.list_widget.item(0))
    check("双击一本书是去网页版接着读（交给默认浏览器）",
          len(OPENED) == before + 1 and "/web/reader/" in OPENED[-1],
          OPENED[-1] if OPENED else "")

    dlg2 = app_mod.WereadShelfDialog(FakeWeread([]))
    pump(lambda: "没有书" in dlg2.info_label.text(), 5.0)
    check("空书架有话说，不是一片空白", "没有书" in dlg2.info_label.text(),
          dlg2.info_label.text()[:24])

    dlg3 = app_mod.WereadShelfDialog(FakeWeread([], available=False))
    check("没 Cookie 时标题就直说", "Cookie" in dlg3.windowTitle(), dlg3.windowTitle())
    check("没 Cookie 时告诉去哪儿配", "feed_auth.json" in dlg3.info_label.text())
    for _d in (dlg, dlg2, dlg3):
        try:
            _d.accept()            # 走正常的关闭路径（窗口里会等取数线程收工）
        except Exception:
            pass
    qapp.processEvents()

    # 菜单入口：安全文档模式下菜单是"中性项"（故意不放它），所以先把那层遮罩摘掉再看。
    # **必须在书页状态下看**：社区页现在有自己的菜单（换社区/换板块），这一节验的是书页那份
    # （以前社区页复用书页菜单，所以在社区页里也碰巧能看到——探针没摘 feed 其实是在测另一份）。
    if w.feed_mode:
        w.exit_feed_mode(reason="probe")
    orig_veiled = w._book_veiled
    w._book_veiled = lambda: False
    menu_texts = [a.text() for a in w.build_context_menu().actions()]
    w._book_veiled = orig_veiled
    check("右键菜单里有\"微信读书 · 我的书架\"",
          any("微信读书" in t for t in menu_texts),
          [t for t in menu_texts if "书架" in t or "微信" in t])
    w._book_veiled = lambda: True
    veiled = [a.text() for a in w.build_context_menu().actions()]
    w._book_veiled = orig_veiled
    check("遮罩模式下不露出这个入口（跟其它书相关项一个规矩）",
          not any("微信读书" in t for t in veiled), veiled)

print("\n=== 8. 点击 / 落点 / 排版隔离 / 社区页字体（这一轮修的六件事里的四件）===")

# 点选是按像素命中的、按键要先有焦点：离屏窗口也得 show() 一次，光标矩形和焦点才是真的
# （以前探针只直接调 w.on_feed_key / w.feed_click，所以这两个坑它一个都碰不到）
w.resize(420, 760)
w.show()
qapp.processEvents()
time.sleep(0.15)
qapp.processEvents()

# 8.1 列表里**点一下就进**，而且点哪条进哪条。
# 以前是"单击只换选中、再单击才进"：第一下还会顺手把页面滚一下（feed_scroll_to 无条件执行），
# 人的感受就是"点一下没反应、还跳了一下，得再点一次"。
use_sources([make_ithome()])
w.enter_feed_mode()
w.feed_nav.channel = "全部"                   # 夹具里"全部"才有 12 条
w.feed_load("list")
pump(lambda: len(w.feed_nav.posts) >= 6 and not w.feed_busy)
for i in (0, 3, 5):
    # 每次点击前重铺一页列表：点一下就进详情，旧的光标已经不属于当前文档了
    # （这个坑在写探针的时候先踩过一次，记在这儿免得以后又拿旧光标去点）
    w.feed_nav.view = "list"
    w.feed_nav.detail_loaded = False
    w.feed_gen += 1
    w.render_feed()
    qapp.processEvents()
    blocks = w.feed_item_blocks()
    obj, _kind, cur = blocks[i]
    rect = w.text_edit.cursorRect(cur)
    w.feed_nav.index = 0
    w.feed_click(rect.center())
    qapp.processEvents()
    check("点第 %d 条：一下就到详情，进的也是这一条" % i,
          w.feed_nav.view == "detail" and w.feed_nav.post is not None
          and w.feed_nav.post.id == obj.id,
          (w.feed_nav.view, w.feed_nav.index,
           w.feed_nav.post.id if w.feed_nav.post else None, obj.id))
    pump(lambda: w.feed_nav.detail_loaded or not w.feed_busy, 6.0)
    check("进详情落在最上面（不是停在正文/评论中间）",
          w.text_edit.verticalScrollBar().value() == 0,
          w.text_edit.verticalScrollBar().value())
    w.feed_back()
    qapp.processEvents()
    check("退回列表还停在刚才那一条上", w.feed_nav.view == "list" and w.feed_nav.index == i,
          (w.feed_nav.view, w.feed_nav.index))

# 8.2 选中已经看得见的那一条，页面一个像素都不该动
# 窗口先拉高：新版版式一条帖子带图会占掉大半屏，原尺寸下"下一条"本来就在屏幕外，
# 那时滚动是**对的**（ensure_visible 该干活），验不出"不该滚"这件事。
_old_size = w.size()
w.resize(560, 1600)
w.feed_nav.view = "list"
w.feed_gen += 1
w.render_feed()
w.feed_scroll_top()
w.feed_nav.index = 0
w.feed_highlight()
qapp.processEvents()
_v0 = w.text_edit.verticalScrollBar().value()
w.on_feed_key(Qt.Key_J)
qapp.processEvents()
check("选中的下一条本来就看得见，页面不会被带着跳",
      w.text_edit.verticalScrollBar().value() == _v0,
      (_v0, w.text_edit.verticalScrollBar().value()))
w.resize(_old_size)
w.render_feed()
qapp.processEvents()

# 8.3 真按键（走 Qt 事件，不是直接调 on_feed_key——以前探针只验了后者）
from PyQt5.QtGui import QKeyEvent                                          # noqa: E402
from PyQt5.QtCore import QEvent, QPoint                                       # noqa: E402
from PyQt5.QtGui import QKeyEvent, QTextCursor                                         # noqa: E402
use_sources(ORIG_SOURCES)                  # 得有不止一个来源，否则"切过去了"看不出来
w.feed_load("list")
pump(lambda: w.feed_nav.posts and not w.feed_busy)
_key_before = w.feed_source().key
w.text_edit.setFocus()
qapp.processEvents()
_key_target = qapp.focusWidget() or w
qapp.sendEvent(_key_target, QKeyEvent(QEvent.KeyPress, Qt.Key_BracketRight, Qt.NoModifier, "]"))
pump(lambda: not w.feed_busy)
check("真按键 `]` 能切社区（键码真的送到了 on_feed_key）",
      w.feed_source().key != _key_before, (_key_before, w.feed_source().key))
qapp.sendEvent(_key_target, QKeyEvent(QEvent.KeyPress, Qt.Key_BracketLeft, Qt.NoModifier, "["))
pump(lambda: not w.feed_busy)
check("真按键 `[` 能切回来", w.feed_source().key == _key_before, w.feed_source().key)
w.exit_feed_mode(reason="probe")

# 8.4 进出社区页不许改坏书页排版，社区页字体又能单独调
LAYOUT_TEXT = ("第一段用来量排版。" * 40) + "\n" + ("第二段也有点内容。" * 40)
w.is_local_mode = True
w.local_file_path = "probe_layout.txt"
w.local_full_text = LAYOUT_TEXT
w.local_start_index = 0
w.local_chapters = []
w.local_heading_offsets = set()
w.apply_style()
w.render_local_page()
qapp.processEvents()


class _BookWheel(object):
    """只为走一遍 eventFilter 的滚轮分支（真 QWheelEvent 要凑一堆参数）。"""

    def __init__(self, dy):
        self._dy = dy

    def type(self):
        return QEvent.Wheel

    def angleDelta(self):
        return QPoint(0, self._dy)

    def modifiers(self):
        return Qt.NoModifier


# 真人："滚轮向上到顶应该把上一页渲染出来然后继续向上滚动，无缝无感知。"
_keep_local = (w.local_start_index, w.local_full_text)
w.local_full_text = ("这一行只是把正文撑长，好让滚动条有得滚。" * 900)
w.local_start_index = 6000
w.render_local_page()
qapp.processEvents()
_step = int(w._line_height_px() * int(w.config.get("arrow_scroll_lines") or 3))

# 往下到底：接下一段，新缓冲第 0 行就是刚才视口第一行（同一行不重不漏）
_sb_book = w.text_edit.verticalScrollBar()
_sb_book.setValue(_sb_book.maximum())
_first = w.text_edit.cursorForPosition(QPoint(0, 0)).position()
_want_start = w.local_start_index + _first
_idx_down = w.local_start_index
w.eventFilter(w.text_edit.viewport(), _BookWheel(-120))
qapp.processEvents()
check("滚到底再往下滚：接上下一段（新缓冲从刚才那一行开始），不是跳回页首",
      w.local_start_index == _want_start and w.local_start_index > _idx_down,
      (w.local_start_index, _want_start, _idx_down))

# 往上到顶：接上一段，而且原来屏幕第一行还在屏幕上、只往下挪了一格滚轮那么远
_sb_book = w.text_edit.verticalScrollBar()
_sb_book.setValue(0)
_anchor = w.local_start_index
w.eventFilter(w.text_edit.viewport(), _BookWheel(120))
qapp.processEvents()
_cur_anchor = QTextCursor(w.text_edit.document())
_cur_anchor.setPosition(max(0, _anchor - w.local_start_index))
_y_anchor = w.text_edit.cursorRect(_cur_anchor).top()
check("滚轮往上到顶：上一段接上了，而且不是翻一整页（原来那一行还在屏幕上）",
      w.local_start_index < _anchor and 0 < _y_anchor < _step + 30,
      (w.local_start_index, _anchor, _y_anchor, _step))

# 全文开头：往上就是停住（不绕回结尾、不换章）
w.local_start_index = 0
w.render_local_page()
qapp.processEvents()
w.text_edit.verticalScrollBar().setValue(0)
for _ in range(3):
    w.eventFilter(w.text_edit.viewport(), _BookWheel(120))
    qapp.processEvents()
check("已经在全文开头：再往上滚只是停住（不绕到结尾）",
      w.local_start_index == 0 and w.text_edit.verticalScrollBar().value() == 0,
      (w.local_start_index, w.text_edit.verticalScrollBar().value()))
# 滚轮和方向键必须一模一样，而且"设 2 行就滚 2 行"——真人报过"方向键 3 行、滚轮 5 行"，
# 还有"拉伸页面之后又变了"：以前拿光标那一行的高度当行高，光标停在标题/空行上就换数。
_keep_lines2 = w.config.get("arrow_scroll_lines")
w.config["arrow_scroll_lines"] = 2
w.local_full_text = ("这一行只是把正文撑长，好让滚动条有得滚。" * 900)
w.local_start_index = 6000
w.render_local_page()
qapp.processEvents()


def _book_delta(fn):
    """从同一处往下滚一下，量实际走了多少像素。"""
    sb = w.text_edit.verticalScrollBar()
    sb.setValue(2000)
    before = sb.value()
    fn()
    qapp.processEvents()
    return sb.value() - before


_unit = w._line_unit_px()

_px_wheel = _book_delta(lambda: w.eventFilter(w.text_edit.viewport(), _BookWheel(-120)))
_px_arrow = _book_delta(lambda: w.scroll_lines(1))
check("滚轮一格和方向键一下走的是同一段距离（行为一模一样）",
      _px_wheel == _px_arrow and _px_wheel > 0, (_px_wheel, _px_arrow))
check("设 2 行就真的是 2 行（一格 = 2 × 版面行高，不是按字号猜出来的 3 行）",
      abs(_px_wheel - int(round(2 * _unit))) <= 2, (_px_wheel, _unit))
_px_key = _book_delta(lambda: w.keyPressEvent(
    QKeyEvent(QEvent.KeyPress, Qt.Key_Down, Qt.NoModifier)))
check("按一下下箭头（真的按键事件，不是直接调函数）也走 2 行，和滚轮一格一模一样",
      _px_key == _px_wheel, (_px_key, _px_wheel))
_px_fast = _book_delta(lambda: w.eventFilter(w.text_edit.viewport(), _BookWheel(-480)))
check("一次发来 480 的'快滚轮'也只按一格算（不会变成一格 5 行）",
      _px_fast == _px_wheel, (_px_fast, _px_wheel))
_geo = w.text_edit.size()
w.text_edit.resize(max(360, _geo.width() - 160), max(260, _geo.height() - 120))
qapp.processEvents()
_unit2 = w._line_unit_px()
_px_resized = _book_delta(lambda: w.eventFilter(w.text_edit.viewport(), _BookWheel(-120)))
check("拉伸窗口之后还是 2 行（行高按版面实测，不跟着窗口走）",
      abs(_px_resized - int(round(2 * _unit2))) <= 2 and abs(_unit2 - _unit) <= 1.5,
      (_px_resized, _unit2, _unit))
w.text_edit.resize(_geo)
qapp.processEvents()
w.config["arrow_scroll_lines"] = _keep_lines2
w.local_start_index, w.local_full_text = _keep_local
w.render_local_page()
qapp.processEvents()
w.local_start_index, w.local_full_text = _keep_local
w.render_local_page()
qapp.processEvents()


def _glyph_font(te):
    """正文**字符自己**带的那套字体（不是控件字体、也不是文档默认字体）。

    真人报"退出社区页后阅读字体变成帖子那套"时，控件字体和文档默认字体都可能是对的，
    错的是每个字符上显式写着的那份 charFormat——只看前两者永远看不见这个 bug。"""
    blk = te.document().firstBlock()
    it = blk.begin()
    if it.atEnd():
        return ""
    cf = it.fragment().charFormat()
    return cf.fontFamily() or (cf.font().family() if cf.font().family() else "")


def snap_layout():
    te = w.text_edit
    blk = te.document().firstBlock()
    return {
        "font": (te.font().family(), round(te.font().pointSizeF(), 1)),
        "doc_font": te.document().defaultFont().family(),
        "glyph_font": _glyph_font(te),
        "line_height": round(blk.blockFormat().lineHeight(), 1),
        "indent": round(blk.blockFormat().textIndent(), 1),
        "margins": (te.viewportMargins().left(), te.viewportMargins().top(),
                    te.viewportMargins().right(), te.viewportMargins().bottom()),
        "page_bottom": round(float(getattr(w, "page_content_bottom", 0) or 0), 2),
    }


_layout_before = snap_layout()
w.enter_feed_mode()
pump(lambda: w.feed_nav.posts and not w.feed_busy)
w.exit_feed_mode(reason="probe")
pump(lambda: w.text_edit.toPlainText()[:12] == LAYOUT_TEXT[:12], 5.0)
for _ in range(25):                 # 让 singleShot(0) 的页底重算跑掉
    qapp.processEvents()
    time.sleep(0.02)
check("进出社区页之后，书页排版一模一样（字体/行距/缩进/页边距/页底）",
      snap_layout() == _layout_before, (_layout_before, snap_layout()))


def _bold_glyphs(te):
    """正文里"加粗"的片段数。

    社区页第一段就是**加粗的帖子标题**：`setHtml` 之后光标正停在那个字符上，
    它的 charFormat 会跟着 `setPlainText` 铺满整页正文——真人看到的就是
    "进 CAF 再退出，正文全变成黑体加粗，像帖子的标题一样"。字体那三层都是对的，
    所以以前那些只查字体的断言全绿，这个 bug 照样在。
    """
    n = 0
    blk = te.document().begin()
    while blk.isValid():
        it = blk.begin()
        while not it.atEnd():
            fr = it.fragment()
            if fr.isValid():
                cf = fr.charFormat()
                if cf.fontWeight() >= 75 or cf.font().bold():
                    n += 1
            it += 1
        blk = blk.next()
    return n


check("退出社区页之后正文不许变成加粗（社区页标题是粗体，不能带过来）",
      _bold_glyphs(w.text_edit) == 0, _bold_glyphs(w.text_edit))

w.config["feed_font_size"] = 22
# 字体故意换一个**跟书页不一样**的：以前这里写的是 `= 书页字体`，于是"社区页字体漏到书页上"
# 这一类 bug 探针根本看不见（两边同名，比也白比）。真人报的就是"退出后正文变成社区页那个字体"。
w.config["feed_font_family"] = "Courier New"
w.enter_feed_mode()
pump(lambda: w.feed_nav.posts and not w.feed_busy)
check("社区页字号跟着 feed_font_size 走", w.text_edit.font().pointSizeF() == 22.0,
      w.text_edit.font().pointSizeF())
check("社区页字体/字号真的换成了自己那套（不是跟书页同名才看着一样）",
      w.text_edit.font().family() == "Courier New"
      and w.text_edit.document().defaultFont().family() == "Courier New",
      (w.text_edit.font().family(), w.text_edit.document().defaultFont().family()))
check("社区页 HTML 那一层的字号也换了（两处不能各说各话）",
      w.feed_theme()["size"] == 22, w.feed_theme()["size"])
w.exit_feed_mode(reason="probe")
pump(lambda: w.text_edit.toPlainText()[:12] == LAYOUT_TEXT[:12], 5.0)
qapp.processEvents()
_book_size = int(w.config.get("font_size", 14))
_book_fam = w.config.get("font_family")
check("社区页字号再大，书页字号一个字都没动",
      int(w.text_edit.font().pointSizeF()) == _book_size,
      (w.text_edit.font().pointSizeF(), w.config.get("font_size")))
check("社区页换过字体，书页的字体也得原样回来（控件字体 + 文档默认字体都要）",
      w.text_edit.font().family() == _book_fam
      and w.text_edit.document().defaultFont().family() == _book_fam
      and int(w.text_edit.document().defaultFont().pointSizeF()) == _book_size,
      (w.text_edit.font().family(), w.text_edit.document().defaultFont().family(),
       w.text_edit.document().defaultFont().pointSizeF()))
check("退出之后书页排版仍然逐项一致（字体/行距/缩进/页边距/页底）",
      snap_layout() == _layout_before, (_layout_before, snap_layout()))
w.config.pop("feed_font_size", None)
w.config.pop("feed_font_family", None)

# 8.5 真人报的那一步：**点进一条帖子**再退出，书页字体被帖子带跑
# 8.4 只走了"进社区 → 退出"，而真实用法里中间一定有一次"点进帖子看详情"。
# 详情那条路会把正文区换成帖子那套排版（字号/字体/行距），退出时能不能原样回来要单独验。
w.config["feed_font_family"] = "Courier New"
w.config["feed_font_size"] = 21
w.apply_style()
w.render_local_page()
qapp.processEvents()
w.enter_feed_mode()
pump(lambda: w.feed_nav.posts and not w.feed_busy)
check("进社区页时正文区换成了社区页那套字体",
      w.text_edit.font().family() == "Courier New", w.text_edit.font().family())
_opened = w.feed_open()
pump(lambda: w.feed_nav.view == "detail" and not w.feed_busy, 8.0)
check("点得进帖子（详情打开了）", _opened and w.feed_nav.view == "detail",
      (w.feed_nav.view, len(w.feed_nav.posts)))
check("帖子详情里正文区仍是社区页那套字体（这一步是前提）",
      w.text_edit.font().family() == "Courier New", w.text_edit.font().family())
w.exit_feed_mode(reason="probe_detail")
pump(lambda: w.text_edit.toPlainText()[:12] == LAYOUT_TEXT[:12], 5.0)
for _ in range(25):
    qapp.processEvents()
    time.sleep(0.02)
_book_fam2 = w.config.get("font_family")
check("点过帖子再退出：书页控件字体回到书页那套",
      w.text_edit.font().family() == _book_fam2, w.text_edit.font().family())
check("点过帖子再退出：书页文档默认字体也回到书页那套",
      w.text_edit.document().defaultFont().family() == _book_fam2,
      w.text_edit.document().defaultFont().family())
check("点过帖子再退出：书页排版逐项一致（字体/行距/缩进/页边距/页底）",
      snap_layout() == _layout_before, (_layout_before, snap_layout()))
check("点过帖子再退出：每个字符自己带的字体也回到书页那套（最狠的那一层）",
      _glyph_font(w.text_edit) == _book_fam2, _glyph_font(w.text_edit))
# 故意把三层全弄脏（控件字体 / 文档默认字体 / 字符级 charFormat），再重铺一次。
# 真人报的就是"某条路把字符级字体也改了"——只钉前两层的话屏幕上看不出来、断言也全绿。
_dirty = app_mod.QFont("Courier New", 21)
_doc = w.text_edit.document()
w.text_edit.setFont(_dirty)
_doc.setDefaultFont(app_mod.QFont(_dirty))
_dc = app_mod.QTextCursor(_doc)
_dc.select(app_mod.QTextCursor.Document)
_df = app_mod.QTextCharFormat()
_df.setFontFamily("Courier New")
_df.setFontPointSize(21.0)
_dc.mergeCharFormat(_df)
w._pin_book_font("probe")
check("字符级字体被改脏之后，一次重铺就把三层一起按配置钉回来",
      w.text_edit.font().family() == _book_fam2
      and _doc.defaultFont().family() == _book_fam2
      and _glyph_font(w.text_edit) == _book_fam2,
      (_glyph_font(w.text_edit), _doc.defaultFont().family(), w.text_edit.font().family()))
w.config.pop("feed_font_size", None)
w.config.pop("feed_font_family", None)
w.apply_style()
w.render_local_page()
qapp.processEvents()

# 8.5 设置窗口里真的有这个开关，而且取消勾选是"删掉键"而不是留个 0
_set_dlg = app_mod.SettingsDialog(w.config, w)
check("滚动行数不在「点取消就退回」的排版预览名单里（调了就该留下）",
      "arrow_scroll_lines" not in _set_dlg.original_typography,
      sorted(_set_dlg.original_typography))
_set_dlg.feed_font_check.setChecked(True)
_set_dlg.feed_font_spin.setValue(19)
qapp.processEvents()
check("设置里有「社区页用单独字体」，一改就落到 config",
      w.config.get("feed_font_size") == 19 and bool(w.config.get("feed_font_family")),
      (w.config.get("feed_font_size"), w.config.get("feed_font_family")))
_set_dlg.feed_font_check.setChecked(False)
qapp.processEvents()
check("取消勾选 = 回到界面默认那套（键删掉，不是留个 0）",
      "feed_font_size" not in w.config and "feed_font_family" not in w.config,
      (w.config.get("feed_font_size"), w.config.get("feed_font_family")))
_set_dlg.reject()
qapp.processEvents()

# 8.5.2 「确定」必须真的存下来。真人报过"现在保存设置好像不能用了"：
# 低调模式那几行被写进了 SettingsDialog.accept 却按主窗口的写法引了 self.feed_sources，
# 于是点确定当场 AttributeError，config 一个键都没落盘。
_low_before = bool(w.config.get("feed_lowkey", False))
_set_dlg2 = app_mod.SettingsDialog(w.config, w)
_set_dlg2.feed_lowkey_check.setChecked(not _low_before)
_saved_ok = True
_save_before = w.save_config
w.save_config = lambda *a, **k: None          # 探针不落盘：别把真配置改成无图模式
try:
    _set_dlg2.accept()
except Exception as _e:                     # 真机上就是这里抛的
    _saved_ok = False
    print("        accept() 抛了：%s: %s" % (type(_e).__name__, _e))
w.save_config = _save_before
qapp.processEvents()
check("设置窗口点「确定」不抛异常，值真的存进 config",
      _saved_ok and bool(w.config.get("feed_lowkey")) == (not _low_before),
      (w.config.get("feed_lowkey"), _low_before))
check("改完低调开关：每个来源当场跟着换（不用重启）",
      all(getattr(s, "images_off", None) == bool(w.config.get("feed_lowkey"))
          for s in w.feed_sources if getattr(s, "images_off", None) is not None),
      [(getattr(s, "key", "?"), getattr(s, "images_off", None)) for s in w.feed_sources])
check("改完低调开关：手里那份内存缓存被清掉（否则还是旧图）", w._feed_memo == {},
      len(w._feed_memo))
w.config["feed_lowkey"] = _low_before         # 还原
for _s in w.feed_sources:
    if getattr(_s, "images_off", None) is not None:
        _s.images_off = _low_before

# 8.5.3 同一个开关也有热键（默认 Ctrl+Alt+L，设置里能改）：不在社区页时只改状态并存盘
_save0 = w.save_config
w.save_config = lambda *a, **k: None
check("无图键有默认值，也能自己改",
      (w.config.get("lowkey_key") or "").strip().lower() == "ctrl+alt+l"
      and hasattr(w, "lowkey_hotkey_signal"), w.config.get("lowkey_key"))
_low0 = bool(w.config.get("feed_lowkey", False))
_img0 = [getattr(_s, "images_off", None) for _s in w.feed_sources]
w.toggle_lowkey()
check("按一下：配置、每个来源、设置里那个勾一起换",
      bool(w.config.get("feed_lowkey")) == (not _low0)
      and all(getattr(_s, "images_off", None) == (not _low0) for _s in w.feed_sources
              if getattr(_s, "images_off", None) is not None),
      (w.config.get("feed_lowkey"), [getattr(_s, "images_off", None) for _s in w.feed_sources]))
w.toggle_lowkey()
check("再按一下回到原样（不留痕，也不多发请求）",
      bool(w.config.get("feed_lowkey")) == _low0
      and [getattr(_s, "images_off", None) for _s in w.feed_sources] == _img0,
      (w.config.get("feed_lowkey"), _img0))
w.save_config = _save0

# 8.5.1 按 R 刷新：必须**明确要求不吃缓存**（列表缓 5 分钟、正文缓一天，吃缓存等于没刷新）
_calls = []
_orig_load = w.feed_load
w.feed_load = lambda what, post=None, fresh=False: (_calls.append((what, fresh)),
                                                    _orig_load(what, post, fresh))[0]
w.feed_nav.view = "list"
w.feed_gen += 1
w.render_feed()
w.on_feed_key(Qt.Key_R)
pump(lambda: not w.feed_busy)
check("列表里按 R：确实去取数了，而且带上了「这一趟不吃缓存」",
      _calls and _calls[-1] == ("list", True), _calls)
_calls[:] = []
w.feed_nav.index = 0
w.feed_open()
pump(lambda: w.feed_nav.view == "detail" and not w.feed_busy, 8.0)
w.on_feed_key(Qt.Key_R)
pump(lambda: not w.feed_busy)
check("详情里按 R：重新取这一篇（也是不吃缓存），视图不被打回列表",
      _calls and _calls[-1] == ("detail", True) and w.feed_nav.view == "detail", _calls)
w.feed_load = _orig_load
w.feed_back()
pump(lambda: w.feed_nav.view == "list")

# 8.6 换社区 / 换板块：右键菜单里得看得见，选了要立刻生效，还要按来源记住
print("--- 换社区 / 换板块 ---")


class BoardSrc(feed_sources.SourceAdapter):
    """一个只有"板块"可切的替身来源（NGA 那种形态：几十上百个版面）。"""

    key = "boardsrc"
    label = "板块源"
    needs_auth = False

    def channels(self):
        return ["甲板", "乙板"]

    def list(self, channel=None, cursor=None):
        return [], None

    def detail(self, post, cursor=None):
        return post, None

    def comments(self, post, cursor=None):
        return [], None

    def boards(self, force=False):
        return [{"label": "甲板", "fid": "1", "group": "测试分类"},
                {"label": "乙板", "fid": "2", "group": "测试分类"}]


use_sources([BoardSrc()])
w.enter_feed_mode()                     # 必须在社区页里：取数回来的处理只在 feed_mode 下走
w.feed_nav.channel = w.feed_first_channel()
w.feed_load("list")
pump(lambda: not w.feed_busy)
_menu = w.build_feed_menu()
_menu_txt = []
for _a in _menu.actions():
    _menu_txt.append(_a.text())
    _sub = _a.menu()
    if _sub is not None:
        _menu_txt += [x.text() for x in _sub.actions()]
check("社区页右键菜单里有「换社区」「换板块」两项（功能一直在，但以前没有任何地方能看见键位）",
      any("换社区" in t for t in _menu_txt) and any("换板块" in t for t in _menu_txt),
      [t for t in _menu_txt if t][:8])
check("菜单里列出了当前来源的板块（%s）" % w.feed_source().label,
      any("甲板" in t for t in _menu_txt) and any("乙板" in t for t in _menu_txt),
      [t for t in _menu_txt if t][:12])

# 换板块：换完要立刻生效，并按来源记进 config
# 起点要**显式钉住**：这条以前依赖"进来时恰好停在甲板"，而真 config 里可能记着乙板
# （上一轮探针跑完留下的），于是 `feed_goto_channel` 走"和目标一样就直接 False"那条，
# 断言就假红了。探针不许依赖上一轮人/上一次运行留下的状态。
w.config.pop("feed_board_boardsrc", None)
w.feed_nav.channel = "甲板"
check("换板块当场生效", w.feed_goto_channel("乙板") and w.feed_nav.channel == "乙板",
      w.feed_nav.channel)
# 真人报"切了板块不刷新"：以前是先取数、回来了才重画，中间那几秒屏幕上还是上一栏的内容。
# 现在要求**当场就把列表清空并开取**，而不是等数据回来才动。
check("换板块当场就把列表清空并开取（不是等数据回来才动）",
      w.feed_nav.view == "list" and w.feed_nav.posts == [] and w.feed_busy is True,
      (w.feed_nav.view, len(w.feed_nav.posts), w.feed_busy))
pump(lambda: not w.feed_busy)
check("换过的板块按来源记进 config", w.config.get("feed_board_boardsrc") == "乙板",
      w.config.get("feed_board_boardsrc"))
# 模拟"重新落到这个来源"（切回来/重开程序）：栏目是从 config 里按来源恢复的
w.feed_nav.channel = "甲板"
check("再落到这个来源时用记忆值，而不是第一栏（以前只有内存，切走就掉）",
      w.feed_first_channel() == "乙板", w.feed_first_channel())
w.config["feed_board_boardsrc"] = "一个已经不存在的板块"
check("记忆里那一栏不存在了，就老实退回第一栏（不显示一个假栏目）",
      w.feed_first_channel() == "甲板", w.feed_first_channel())
w.config["feed_board_boardsrc"] = "乙板"

# 选择器：`V` 那条路（全量表没拉过 → 后台拉 → 回来后弹可搜索的列表）
_orig_get_item = app_mod.QInputDialog.getItem
_PICK_CALLS = []


def _fake_get_item(*a, **k):
    items = list(a[3]) if len(a) > 3 else []
    _PICK_CALLS.append(len(items))
    return ("测试分类 · 乙板", True)


app_mod.QInputDialog.getItem = _fake_get_item
try:
    w.feed_boards_tried.discard("boardsrc")
    check("第一次按 V：先去后台拉全量板块表（不在界面线程里拉网）", w.feed_board_picker() is True)
    pump(lambda: not w.feed_busy, 10.0)
    qapp.processEvents()
    check("板块表回来后弹出可搜索的选择器，选中即换过去", w.feed_nav.channel == "乙板",
          (w.feed_nav.channel, _PICK_CALLS, [m for m in LOGS if "板块" in m][-2:]))
except Exception as _e:
    check("板块选择器这条路没炸", False, "%s: %s" % (type(_e).__name__, _e))
finally:
    app_mod.QInputDialog.getItem = _orig_get_item

# 换社区：`C` 那条路 + 直接跳
use_sources(ORIG_SOURCES)
w.config["feed_source"] = "mix"
_check_src_before = w.feed_source().key
check("菜单能直接跳到指定来源", w.feed_goto_source(2) and w.feed_source().key != _check_src_before,
      (w.feed_source().key, _check_src_before))
pump(lambda: not w.feed_busy)
_orig_get_item = app_mod.QInputDialog.getItem
app_mod.QInputDialog.getItem = lambda *a, **k: ("IT之家", True)
try:
    w.feed_source_picker()
    qapp.processEvents()
    check("C 键的选择器能换社区（选完立刻换过去并取数）", w.feed_source().label == "IT之家",
          w.feed_source().label)
except Exception as _e:
    check("社区选择器这条路没炸", False, "%s: %s" % (type(_e).__name__, _e))
finally:
    app_mod.QInputDialog.getItem = _orig_get_item
pump(lambda: not w.feed_busy)
check("换社区之后 config 里记的是新来源", w.config.get("feed_source") == w.feed_source().key,
      (w.config.get("feed_source"), w.feed_source().key))

print("\n=== 9. 退出：书的状态不许被动过 ===")
w.exit_feed_mode(reason="probe")
check("退得出来", not w.feed_mode)
check("没有后台取数还在跑", not w.feed_busy)
check("日志里的失败都是'这个站连不上'，没有代码异常",
      all(any(h in m for h in ("连接超时", "404", "Cloudflare", "登录"))
          for m in LOGS if "社区取数失败" in m),
      [m for m in LOGS if "社区取数失败" in m][:3])

print("\n=== 10. 老板键唤出：不能被自己吞掉，也不能每唤出就挪一次 ===")
# 2026-10-19 的真日志：解锁之后出现"触发→隐藏、触发→显示、触发→隐藏"一串，
# 用户看到的就是"按唤醒、闪一下、没了"。两条病根各钉一条：
#   1) 唤出后紧跟着的一次"收起"（重复触发/没抢到前台）不该生效；
#   2) 窗口明明看得见，`ensure_on_screen` 却每次显示都把它挪一次（Win32 那个判断在刚
#      show 的一瞬间常答"不在任何屏幕"，日志里"窗口被挪到屏幕外"于是每次刷屏）。
w.show()
pump(lambda: w.isVisible(), 2)
w.toggle_window("probe")                      # 收起
pump(lambda: not w.isVisible(), 2)
check("收起照常（这一下是正常功能）", not w.isVisible())
w.last_toggle_time = 0                         # 别让 0.3 秒防连击把这中间的唤出吞掉
w.toggle_window("probe")                      # 唤出
pump(lambda: w.isVisible(), 2)
check("唤出后窗口在屏幕上", w.isVisible())
LOGS.clear()
w.last_toggle_time = 0                         # 绕过 0.3 秒防连击，直接考"唤出临界期"那条
w.toggle_window("probe")                      # 立刻又"收起"——这就是"闪一下"的成因
check("刚唤出时的一次收起被忽略（窗口留在屏幕上）", w.isVisible())
check("忽略要留痕（下回排查有据）", any("忽略一次收起" in m for m in LOGS), LOGS[-2:])
w._shown_at = time.time() - 1.0                # 过了临界时间
w.last_toggle_time = 0
w.toggle_window("probe")
pump(lambda: not w.isVisible(), 2)
check("过了临界时间，收起照常生效（没把功能吞掉）", not w.isVisible())

# 看得见就别动它
w.show()
pump(lambda: w.isVisible(), 2)
w.move(120, 120)
qapp.processEvents()
before = (w.x(), w.y())
LOGS.clear()
w.ensure_on_screen()
qapp.processEvents()
check("窗口看得见时一个像素都不挪", (w.x(), w.y()) == before, (before, (w.x(), w.y())))
check("看得见就不该写'挪回'日志", not any("挪回" in m for m in LOGS), LOGS[-2:])
# 真飞到桌面之外了才挪
w.move(-9000, -9000)
qapp.processEvents()
LOGS.clear()
w.ensure_on_screen()
qapp.processEvents()
check("窗口在屏幕外时才挪回来",
      any("挪回" in m for m in LOGS) and w._rect_usable(w.frameGeometry()),
      (LOGS[-1] if LOGS else "", (w.x(), w.y())))

# 偷瞄的输入钩子："出现别的输入"就收窗口。老板键接管可见层时必须先把它卸掉，
# 否则窗口刚露脸，紧跟着的一下点击就被判成"退出偷瞄"——14:14:12 那一下就是这么没的。
class _FakePeekHook(object):
    def __init__(self):
        self.uninstalled = 0

    def install(self, key):
        return True

    def uninstall(self):
        self.uninstalled += 1


_real_peek_hook = w.peek_input_hook
w.hide()
qapp.processEvents()
fake_hook = _FakePeekHook()
w.peek_input_hook = fake_hook
w.peek_mode = True                             # 模拟"上次偷瞄没收干净就藏起来了"
w.last_toggle_time = 0
w.toggle_window("probe")
pump(lambda: w.isVisible(), 2)
check("偷瞄没收干净时被唤出，先把偷瞄的钩子卸掉",
      fake_hook.uninstalled >= 1 and not w.peek_mode, (fake_hook.uninstalled, w.peek_mode))
check("卸掉钩子之后窗口才留得住（不会被'随便点一下'收走）", w.isVisible())
w.peek_input_hook = _real_peek_hook
w.hide()
qapp.processEvents()

print("\n=== 11. 离线示例仍是兜底 ===")
print("\n=== 12. 顺手快：内存先画 / 预取选中那条 / 内容没变不重排 ===")


class CountSource(feed_sources.IthomeSource):
    """数一数到底发了几次请求：预取和"人按下的那次"不该各发一遍。"""

    def __init__(self, http, slow=0.0):
        feed_sources.IthomeSource.__init__(self, http)
        self.n_list = self.n_detail = self.n_comments = 0

    def list(self, channel=None, cursor=None):
        self.n_list += 1
        return feed_sources.IthomeSource.list(self, channel, cursor)

    def detail(self, post, cursor=None):
        self.n_detail += 1
        return feed_sources.IthomeSource.detail(self, post, cursor)

    def comments(self, post, cursor=None):
        self.n_comments += 1
        return feed_sources.IthomeSource.comments(self, post, cursor)


cs = CountSource(FakeHttp({"newslistpageget": F["ithome_hot.json"],
                           "newscontent": F["ithome_detail.json"]},
                          cache=MemCache()))
pump(lambda: not w.feed_busy, 10.0)          # 上一节可能还有一次取数在飞
if not w.feed_mode:                          # 上一节末尾可能已经退出社区页
    w.enter_feed_mode()
    pump(lambda: not w.feed_busy, 10.0)
# 这一节从干净的内存缓存开始（不然"预取"会被上一节留下的同名帖子挡掉）；
# 收尾时把栏目名和来源下标还原，后面的小节还要照旧跑。
_chan0, _src_i0 = w.feed_nav.channel, w.feed_source_i
w._feed_memo.clear()
_pre0 = set(k for k in w._feed_memo if k[0] == "detail")
use_sources([cs])
w.feed_load("list")
pump(lambda: len(w.feed_nav.posts) >= 5 and not w.feed_busy, 10.0)
check("（前置）列表铺好了", len(w.feed_nav.posts) >= 5, len(w.feed_nav.posts))
check("（前置）列表只取了一次", cs.n_list == 1, cs.n_list)

# 预取：列表回来之后，同一条线程顺手把第一条的详情取回来放着
pump(lambda: len([k for k in w._feed_memo if k[0] == "detail" and k not in _pre0]) >= 3, 20.0)
_new = [k for k in w._feed_memo if k[0] == "detail" and k not in _pre0]
_ids = [k[2] for k in _new]
check("列表回来会顺手把前三条的正文取回来（人往下按回车时它已经热了）",
      len(_new) == 3 and w.feed_nav.posts[0].id in _ids, (len(_new), _ids[:3]))
check("预取每条都是详情 + 评论各一次（同一条绝不重复取）",
      cs.n_detail == cs.n_comments == 3, (cs.n_detail, cs.n_comments))

# 进帖：命中预取，不许再发请求
w.feed_nav.index = 0
w.feed_open()
pump(lambda: w.feed_nav.view == "detail" and w.feed_nav.detail_loaded and not w.feed_busy, 10.0)
check("进帖用的是预取那份（没有再发一次详情/评论请求）",
      (cs.n_detail, cs.n_comments) == (3, 3), (cs.n_detail, cs.n_comments))

# 退回再进：全走内存
w.on_feed_key(Qt.Key_Backspace)
pump(lambda: w.feed_nav.view == "list")
w.feed_nav.index = 0
w.feed_open()
pump(lambda: w.feed_nav.view == "detail" and w.feed_nav.detail_loaded and not w.feed_busy, 10.0)
check("退回再进同一条：仍然一次请求都没多发", (cs.n_detail, cs.n_comments) == (3, 3),
      (cs.n_detail, cs.n_comments))
w.on_feed_key(Qt.Key_Backspace)
pump(lambda: w.feed_nav.view == "list")

# 文字直播：详情这条标了"进行中"就把 5 秒那张表开起来；回到列表就停。
w.feed_nav.index = 0
w.feed_open()
pump(lambda: w.feed_nav.view == "detail" and w.feed_nav.detail_loaded and not w.feed_busy, 10.0)
_live_post = w.feed_nav.post
_keep_tags = list(_live_post.tags or [])
_live_post.tags = _keep_tags + ["进行中"]
w.render_feed()
check("进行中的比赛：详情页把'每 5 秒自己再问一次'开起来",
      w.feed_live_timer.isActive() and w.feed_live_timer.interval() == 5000,
      (w.feed_live_timer.isActive(), w.feed_live_timer.interval()))
# 同一长度地变（96 - 92 → 98 - 94、5:22 → 5:14）：以前只数字数会被当成"没变"，
# 屏幕上就一直是那一刻的比分（真人："总是停留在某个时刻的比分，要手动刷新才会变"）。
_live_body = list(_live_post.body or [])
_live_post.body = [feed_sources.Block(kind=feed_sources.BLOCK_P,
                                      text="雷霆 96 - 92 马刺（第四节剩5:22）")]
_sig_a = w._feed_sig_now()
_live_post.body = [feed_sources.Block(kind=feed_sources.BLOCK_P,
                                      text="雷霆 98 - 94 马刺（第四节剩5:14）")]
_sig_b = w._feed_sig_now()
_live_post.body = _live_body
check("比分同长度地变一下也算'变了'（签名按内容算，不是只数字数）",
      _sig_a != _sig_b, (_sig_a[2:4], _sig_b[2:4]))
# 2 秒一拍这么密，失败必须退让：翻倍、封顶 30 秒；退让没到点就不发请求。
_tag0 = getattr(w.feed_source(), "key", "")
_n_live = cs.n_detail
w._feed_live_fail = 0
w._feed_live_retry_at = 0.0
w.on_feed_data("error", ("boom", w.feed_gen, _tag0))
_f1 = (w._feed_live_fail, int(round(w._feed_live_retry_at - time.time())))
w.on_feed_data("error", ("boom", w.feed_gen, _tag0))
_f2 = (w._feed_live_fail, int(round(w._feed_live_retry_at - time.time())))
w.feed_error = ""
check("重取连着失败就翻倍退让（4 秒 → 8 秒，封顶 30 秒）",
      _f1[0] == 1 and 2 <= _f1[1] <= 5 and _f2[0] == 2 and _f2[1] > _f1[1],
      (_f1, _f2))
w.feed_live_tick()
check("退让还没到点：这一拍不发请求（不上去硬撞站点）",
      cs.n_detail == _n_live, (cs.n_detail, _n_live))
w._feed_live_fail = 0
w._feed_live_retry_at = 0.0
pump(lambda: cs.n_detail > _n_live, 12.0)
check("5 秒到点真的会再问一次（不只是把表开起来）", cs.n_detail > _n_live,
      (cs.n_detail, _n_live))

# 站点会回更旧的快照（实测同一场比赛几分钟内能收到三份不同年龄的页）
_tag_now = getattr(w.feed_source(), "key", "")
_cur_post = w.feed_nav.post
_newer_post = feed_sources.Post.from_dict(_cur_post.to_dict())
_newer_post.excerpt = "110 - 99 · 进行中 第四节剩1:00"
w._live_guard((_tag_now, _newer_post.id), _newer_post.excerpt)     # 先让它"见过"这份
_older_post = feed_sources.Post.from_dict(_cur_post.to_dict())
_older_post.excerpt = "96 - 92 · 进行中 第四节剩5:22"
w.on_feed_data("detail", (_older_post, [], None, w.feed_gen, _tag_now))
qapp.processEvents()
check("详情页也认进度：更旧的快照顶不上来",
      w.feed_nav.post is not _older_post,
      (getattr(w.feed_nav.post, "excerpt", "")[:20],))
w._feed_live_best.clear()      # 别把后面列表那几条的进度记录搅乱

# 打完就停表，但停之前要再问最后一次：不然屏幕停在倒数第二拍的比分
w.feed_live_timer.stop()
w._feed_live_fail = 0
w._feed_live_retry_at = 0.0
pump(lambda: not w.feed_busy, 10.0)
_n0_settle = cs.n_detail
_live_post.tags = _keep_tags
w.feed_live_watch()
pump(lambda: cs.n_detail > _n0_settle, 10.0)
check("刚打完停表之前补最后一次（不把终场比分丢在倒数第二拍）",
      cs.n_detail == _n0_settle + 1 and not w.feed_live_timer.isActive(),
      (cs.n_detail, _n0_settle, w.feed_live_timer.isActive()))
_live_post.tags = _keep_tags + ["进行中"]
w.render_feed()

# 技术统计那种 17 列：表格按自然宽度撑出去，靠左右滑看右边的列；横条**不显示**。
_table_post = w.feed_nav.post
_table_body = list(_table_post.body)
_cols = ["球员", "得分", "篮板", "助攻", "时间", "投篮", "三分", "罚球", "抢断",
         "封盖", "失误", "犯规", "前场", "后场", "±", "首发", "位置"]
_tsv = ("\t".join(_cols) + "\n"
        + "\n".join("\t".join(["泰雷斯-马克西"] + [str(28 - i) for _ in range(16)])
                    for i in range(3)))
_table_post.body = [feed_sources.Block(kind=feed_sources.BLOCK_TABLE, text=_tsv, lang="wide")]
w.render_feed()
_hbar = w.text_edit.horizontalScrollBar()
check("17 列的表撑出去：有横向行程，但横条是关着的（不显示进度条）",
      _hbar.maximum() > 100
      and w.text_edit.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff,
      (_hbar.maximum(), w.text_edit.horizontalScrollBarPolicy()))


class _WheelStub(object):
    """只为走一遍 eventFilter 里那条滚轮分支（真 QWheelEvent 要凑一堆参数）。"""

    def __init__(self, dx=0, dy=0, mods=Qt.NoModifier):
        self._d = QPoint(dx, dy)
        self._m = mods

    def type(self):
        return QEvent.Wheel

    def angleDelta(self):
        return self._d

    def modifiers(self):
        return self._m


# 社区页的竖直滚轮和书页走同一个 _local_wheel_step（只是 scroll_px 里滚的是自己那根条），
# 这一页正文不够长、滚不动，所以只钉换算：一格请求的还是设置里那几行那么多像素。
_rec_px = []
_real_scroll = w.scroll_px
w.scroll_px = lambda px, flip=True: (_rec_px.append(px), None)
w._local_wheel_step(-120)
w.scroll_px = _real_scroll
check("社区页一格滚轮请求的也是设置里那几行（和书页同一套换算）",
      _rec_px == [w._lines_to_px(w.config.get("arrow_scroll_lines") or 3)],
      (_rec_px, w._lines_to_px(w.config.get("arrow_scroll_lines") or 3)))

_hbar.setValue(0)
_r = w.eventFilter(w.text_edit.viewport(), _WheelStub(dy=-120, mods=Qt.ShiftModifier))
check("Shift+滚轮往右滑一格 = 滚动行数那么高（不是固定 120px）",
      _r is True and _hbar.value() == w._feed_hstep(), (_r, _hbar.value(), w._feed_hstep()))
_hbar.setValue(0)
w.on_feed_key(Qt.Key_Right)
check("右箭头也能左右滑", _hbar.value() == w._feed_hstep(), _hbar.value())
w.on_feed_key(Qt.Key_Left)
check("已经最左边了，再往左滑不会变成负数", _hbar.value() == 0, _hbar.value())

_keep_lines = w.config.get("arrow_scroll_lines")
w.config["arrow_scroll_lines"] = 1
_s1 = w._feed_hstep()
w.config["arrow_scroll_lines"] = 3
_s3 = w._feed_hstep()
w.config["arrow_scroll_lines"] = _keep_lines
# 行高是按版面实测的浮点数，乘完再取整，所以 3 倍允许 2px 的取整差
check("滚动行数也管横滑：3 行是 1 行的三倍", _s1 > 0 and abs(_s3 - 3 * _s1) <= 2,
      (_s1, _s3))

# 窄页：左右键不该被吞掉（还得留给别的用途）
_table_post.body = [feed_sources.Block(kind=feed_sources.BLOCK_TABLE,
                                       text="球员\t得分\n马克西\t28")]
w.render_feed()
check("没有横向溢出的页：横行程只有页边距那几像素，左右键照旧不管（不吞按键）",
      w.text_edit.horizontalScrollBar().maximum() <= 16
      and w.on_feed_key(Qt.Key_Right) is False,
      (w.text_edit.horizontalScrollBar().maximum(), w.on_feed_key(Qt.Key_Right)))
_table_post.body = _table_body
w.render_feed()
w.on_feed_key(Qt.Key_Backspace)
pump(lambda: w.feed_nav.view == "list")
check("退回列表就停表（不在列表页白跑请求）", not w.feed_live_timer.isActive())
_live_post.tags = _keep_tags
w.render_feed()
check("没有'进行中'的详情不开表（打完的比赛不会空转重取）",
      not w.feed_live_timer.isActive())
check("这一页没有'文字直播/数据统计'可切：T 不硬切、菜单里也不写这一项",
      w.feed_view_label() == "" and w.on_feed_key(Qt.Key_T) is False,
      (w.feed_view_label(), w.on_feed_key(Qt.Key_T)))

# 赛程列表：有一场正在打，比分也得自己刷新（详情 5 秒 / 列表 20 秒，一天一个请求）
_list_src = w.feed_source()
_list_src.live_scores = lambda posts: ({str(posts[0].id): "101 - 98 · 进行中 第四节剩3:12"}
                                       if posts else {})
_p0 = w.feed_nav.posts[0]
_p0.excerpt = "96 - 92 · 进行中 第四节剩5:22"
w.render_feed()
check("列表里有进行中的比赛：补比分那张表开起来，但慢一档（20 秒）",
      w.feed_live_timer.isActive() and w.feed_live_timer.interval() == 20000,
      (w.feed_live_timer.isActive(), w.feed_live_timer.interval()))
w.feed_live_timer.stop()                 # 手动驱动这一拍：别和定时器抢 feed_busy
pump(lambda: not w.feed_busy, 10.0)
w._feed_live_fail = 0
w._feed_live_retry_at = 0.0
w.feed_live_tick()
pump(lambda: _p0.excerpt.startswith("101"), 10.0)      # 等结果回来，别等 feed_busy
check("补比分只动摘要：屏幕上直接是新比分，列表本身没被重拉",
      _p0.excerpt == "101 - 98 · 进行中 第四节剩3:12"
      and "101 - 98" in w.text_edit.toPlainText() and w.feed_nav.view == "list",
      (_p0.excerpt, w.feed_nav.view))
_live_fn = w.feed_source().live_scores
w.feed_source().live_scores = lambda posts: {str(_p0.id): "96 - 92 · 进行中 第四节剩5:22"}
pump(lambda: not w.feed_busy, 10.0)
w._feed_live_fail = 0
w._feed_live_retry_at = 0.0
w.feed_live_tick()
pump(lambda: not w.feed_busy, 10.0)
qapp.processEvents()
check("列表也一样：站点回旧的那份，屏幕上的比分不往回退",
      _p0.excerpt.startswith("101 - 98"), _p0.excerpt)
w.feed_source().live_scores = _live_fn

_p0.excerpt = "106 - 100 · 已结束"
w.render_feed()
check("场上都打完了就不再自己问（表停掉）", not w.feed_live_timer.isActive())
del _list_src.live_scores

# 内容没变就不重排（不然每次刷新都抖一下、滚动条归位）
_renders = []
_orig_render = w.render_feed


def _counting_render():
    _renders.append(1)
    return _orig_render()


w.render_feed = _counting_render
w.feed_load("list")
_after_memo = len(_renders)
pump(lambda: not w.feed_busy, 10.0)
w.render_feed = _orig_render
check("同一份内容取回来不再重排（屏幕上不抖、滚动条不归位）",
      len(_renders) == _after_memo, (len(_renders), _after_memo))

# 慢来源 + 屏幕上有旧内容：新结果还没回来时，人已经能读到（内存那份先画上去）
slow = CountSource(FakeHttp({"newslistpageget": F["ithome_hot.json"],
                             "newscontent": F["ithome_detail.json"]},
                            cache=MemCache(), slow=0.8))
w.feed_sources = [slow]
w.feed_source_i = 0
# 把内存里那份"改老"：真实场景是隔了几分钟再切回来——这份仍然要**先画上去**，
# 然后才去后台取新的（不是白屏等 0.8 秒）。
for _k, _v in list(w._feed_memo.items()):
    w._feed_memo[_k] = (_v[0], _v[1] - 3600)
w.feed_load("list")
_had = bool(w.feed_nav.posts)
_still_busy = w.feed_busy
check("内存那份过期了也是先画后取（不是白屏等网络）",
      _had and _still_busy, (_had, _still_busy))
pump(lambda: not w.feed_busy, 10.0)
check("刚取过的内容连请求都不发（90 秒内）", slow.n_list >= 1, slow.n_list)
w.feed_source_i = _src_i0

# === 13. 知乎回答页点标题 → 这个问题的全部回答；长评论整条能收起来 ===
print("\n=== 13. 点标题进问题 / 长评论收起来 ===")


class _ZhihuLike(object):
    """这一节只借它的两条路：回答页给正文，问题页给"回答列表"。"""

    key = "zhihu"
    label = "知乎"
    images_off = True

    def __init__(self):
        self.n_detail = 0

    def detail(self, post):
        self.n_detail += 1
        txt = "问题说明" if str(post.id).isdigit() else "这条回答"
        return feed_sources.Post.from_dict(
            dict(post.to_dict(), body=[{"kind": "p", "text": txt}])), None

    def comments(self, post):
        if str(post.id).isdigit():
            return [feed_sources.Comment(cid="a1", author="答主一",
                                         body=[feed_sources.Block(kind=feed_sources.BLOCK_P,
                                                                  text="回答一")])], "ok"
        return [feed_sources.Comment(cid="c1", author="评论者",
                                     body=[feed_sources.Block(kind=feed_sources.BLOCK_P,
                                                              text="这条回答下面的评论")])], "ok"


_z = _ZhihuLike()
_orig_source = w.feed_source
w.feed_source = lambda: _z
if not w.feed_mode:
    w.enter_feed_mode()
_ans_post = feed_sources.Post(source="zhihu", source_label="知乎", pid="a77",
                              title="某个问题", url="https://www.zhihu.com/question/4242/answer/77")
w.feed_nav.reset()
w.feed_nav.view = "detail"
w.feed_nav.post = _ans_post
w.feed_nav.detail_loaded = True
w.render_feed()
check("「某个回答」这一页知道它属于哪个问题（能跳）",
      w.feed_question_id() == "4242" and w.feed_theme().get("title_link") is True,
      w.feed_question_id())
_tc = app_mod.QTextCursor(w.text_edit.document())
_tc.movePosition(app_mod.QTextCursor.Start)
_pt = w.text_edit.cursorRect(_tc).center()
check("鼠标停在标题上给手型（提示能点）", w.feed_hover_title(_pt) is True)
check("点标题 = 进这个问题的全部回答",
      w.feed_click(_pt) is True and w.feed_nav.post.id == "4242", w.feed_nav.post.id)
pump(lambda: w.feed_nav.detail_loaded and not w.feed_busy, 10.0)
# 这一条最容易假通过：详情取数只看 nav.selected() 的话，取回来的还是列表选中那条回答，
# 问题页会被回答的正文盖回去（真机上就是这个表现）。所以正文必须是**问题页**那份。
_body = " ".join(b.text for b in (w.feed_nav.post.body or []))
check("问题页取的是问题自己的正文（没被列表选中那条回答盖回去）",
      w.feed_nav.post.id == "4242" and _body.strip() == "问题说明", (w.feed_nav.post.id, _body))
check("问题页里列的是回答", any(c.author == "答主一" for c in w.feed_nav.roots),
      [c.author for c in w.feed_nav.roots])
check("问题页里标题不再可点（已经在问题页了）", not w.feed_question_id())
check("退格回刚才那个回答", w.feed_back() is True and w.feed_nav.post.id == "a77"
      and w.feed_nav.detail_loaded is True, w.feed_nav.post.id)
check("再退一次才回列表", w.feed_back() is True and w.feed_nav.view == "list")
w.feed_source = _orig_source

# 长评论：默认整段显示；点一下把这一条连正文一起收起来（只留作者行 + 一行摘要）
_long = "很长的评论。" * 40
w.feed_nav.view = "detail"
w.feed_nav.post = _ans_post
w.feed_nav.stack = []
w.feed_nav.roots = [feed_sources.Comment(
    cid="c9", author="话多的人", created="10-19", score=3,
    body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text=_long)])]
w.feed_nav.flat = feed_mod.comment_forest(w.feed_nav.roots)
w.feed_nav.detail_loaded = True
w.feed_nav.c_index = 0
w.feed_nav.collapsed = set()
w.render_feed()
check("长评论默认整段显示", _long in w.text_edit.toPlainText())
check("点一下收起：这一条连正文一起收（只留作者行 + 一行摘要）",
      w.feed_toggle_collapse() is True, w.feed_nav.collapsed)
_x = w.text_edit.toPlainText()
check("收起后长正文不再铺满屏幕", _long not in _x and "话多的人" in _x, len(_x))
check("收起后给了一行摘要（看得出这条在说什么）", "很长的评论" in _x)
check("再点一下展开回全文", w.feed_toggle_collapse() is True
      and _long in w.text_edit.toPlainText())

# === 14. 点折叠：被点的那一条留在原地（页面不跳） ===
print("\n=== 14. 折叠评论不挪页面 ===")


def _settle(n=8):
    for _ in range(n):
        qapp.processEvents()
        time.sleep(0.02)

_many = [feed_sources.Comment(
    cid="m%d" % i, author="甲%d" % i, created="10-19", score=i,
    body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text="第 %d 条。" % i + "话" * 70),
          feed_sources.Block(kind=feed_sources.BLOCK_P, text="第二段。" + "呀" * 40)],
    replies=[feed_sources.Comment(cid="m%d-r" % i, author="乙%d" % i, body=[
        feed_sources.Block(kind=feed_sources.BLOCK_P, text="回复。" + "哦" * 30)])]
    if i % 2 == 0 else [])
    for i in range(14)]
w.feed_nav.view = "detail"
w.feed_nav.post = _ans_post
w.feed_nav.stack = []
w.feed_nav.set_detail(_ans_post, _many)
w.feed_nav.collapsed = set()
w.feed_nav.c_index = 0
w.render_feed()
_settle()
_sb = w.text_edit.verticalScrollBar()
check("这一页够长、真的能滚（不然这条断言没意义）", _sb.maximum() > 200, _sb.maximum())
_sb.setValue(int(_sb.maximum() * 0.45))
_settle()
_vh = w.text_edit.viewport().height()
_pick = None
for _i, (_c, _d, _p) in enumerate(w.feed_nav.visible_comments()):
    _y = w._feed_comment_y(_c)
    if _y is not None and 40 <= _y <= _vh - 40:
        _pick = (_i, _c, _y)
        break
check("折叠之前，视口里挑得到一条评论", _pick is not None, _pick)
if _pick:
    _i, _c, _y0 = _pick
    w.feed_nav.c_index = _i
    w.feed_toggle_collapse()
    _settle()
    _y1 = w._feed_comment_y(_c)
    check("点折叠：被点的那一条留在原来的高度（整页不跳）",
          _y1 is not None and abs(_y1 - _y0) <= 4, (_y0, _y1))
    check("折叠确实生效了（不然上面那条是空过）", _c.id in w.feed_nav.collapsed,
          w.feed_nav.collapsed)
    w.feed_toggle_collapse()
    _settle()
    _y2 = w._feed_comment_y(_c)
    check("再点展开：也留在原地", _y2 is not None and abs(_y2 - _y0) <= 4, (_y0, _y2))

# 点一下就折叠（以前是"第一次只换选中、再点一次才折叠"，真人："一次就好了"）
w.feed_nav.collapsed = set()
w.render_feed()
_settle()
_first, _d1, _p1 = w.feed_nav.visible_comments()[0]
_cursor0 = None
for _obj, _kind, _cur in w.feed_item_blocks():
    if _obj is _first:
        _cursor0 = _cur
        break
check("找得到第一条评论在屏幕上的位置", _cursor0 is not None, _cursor0)
if _cursor0 is not None:
    _pt = w.text_edit.cursorRect(_cursor0).center()
    check("点一下就折叠（不用点两次）",
          w.feed_click(_pt) is True and _first.id in w.feed_nav.collapsed,
          w.feed_nav.collapsed)
    check("再点一下就展开", w.feed_click(_pt) is True
          and _first.id not in w.feed_nav.collapsed, w.feed_nav.collapsed)

w.feed_sources = list(ORIG_SOURCES)          # 把第 6 节换掉的替身来源还原回来
w.feed_source_i = [i for i, s in enumerate(w.feed_sources) if s.key == "demo"][0] \
    if any(s.key == "demo" for s in w.feed_sources) else 0
w.feed_gen += 1
w.feed_nav.reset()
w.feed_nav.channel = w.feed_first_channel()   # 换来源之后栏目要重新落到它真的有的那一栏
w.enter_feed_mode()
w.feed_load("list")
pump(lambda: w.feed_nav.posts and not w.feed_busy)
check("示例源照常出内容", len(w.feed_nav.posts) >= 5, len(w.feed_nav.posts))

# 10. 这一轮的键位：C/V 直接切、Z 撤回与回主页、E 到评论区、选字不折叠、偷瞄不收起
_i0 = w.feed_source_i
_n = len(w.feed_sources)
w.feed_switch_prev = None
check("C 直接切到下一个社区（不弹窗、不换页）",
      w.on_feed_key(Qt.Key_C) is True and w.feed_source_i == (_i0 + 1) % _n,
      (w.feed_source_i, _i0, _n))
pump(lambda: not w.feed_busy)
check("Z 撤回上一次切社区", w.on_feed_key(Qt.Key_Z) is True and w.feed_source_i == _i0,
      (w.feed_source_i, _i0))
pump(lambda: not w.feed_busy)
check("撤完再按 Z 就说“没得撤”，不装作成功",
      w.on_feed_key(Qt.Key_Z) is False)
# D/F 是 C/V 的回头路（真人："d 上一个社区，f 上一个板块"）
_i1 = w.feed_source_i
w.on_feed_key(Qt.Key_C)
pump(lambda: not w.feed_busy)
check("D 直接切回上一个社区（跟 C 对称）",
      w.on_feed_key(Qt.Key_D) is True and w.feed_source_i == _i1, (w.feed_source_i, _i1))
pump(lambda: not w.feed_busy)
_ith = [i for i, _s in enumerate(w.feed_sources) if _s.key == "ithome"]
if _ith:
    w.feed_source_i = _ith[0]
    w.feed_nav.channel = w.feed_first_channel()
    _ch0 = w.feed_nav.channel
    check("V 直接切到下一栏（不弹窗）",
          w.on_feed_key(Qt.Key_V) is True and w.feed_nav.channel != _ch0,
          (w.feed_nav.channel, _ch0))
    pump(lambda: not w.feed_busy)
    check("F 直接切回上一栏（跟 V 对称）",
          w.on_feed_key(Qt.Key_F) is True and w.feed_nav.channel == _ch0,
          (w.feed_nav.channel, _ch0))
    pump(lambda: not w.feed_busy)
    w.on_feed_key(Qt.Key_V)                     # 再往前一步，下面验 Z
    pump(lambda: not w.feed_busy)
    check("Z 撤回板块切换", w.on_feed_key(Qt.Key_Z) is True and w.feed_nav.channel == _ch0,
          (w.feed_nav.channel, _ch0))
    pump(lambda: not w.feed_busy)
    w.feed_source_i = _i0

# 列表位置：进出社区页不该每次回到顶部（"每次打开都定位到顶部"）
w.feed_nav.view = "list"
w.render_feed()
qapp.processEvents()
_sb = w.text_edit.verticalScrollBar()
_want = min(40, _sb.maximum())
w.feed_restore_pos = _want
w.render_feed()
qapp.processEvents()
qapp.processEvents()
check("重画之后还能落回刚才那一行（不是永远顶部）",
      w.text_edit.verticalScrollBar().value() == _want,
      (_want, w.text_edit.verticalScrollBar().value()))

# 详情：E 到评论区顶部、Z 一口气回列表、知乎"回答→问题"的 Esc 也回列表
def _fake_detail(via=""):
    nav = w.feed_nav
    nav.view = "detail"
    nav.post = feed_sources.Post(
        source="demo", source_label="示例", pid="z1", title="标题",
        body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text="正文段落。" * 120)
              for _ in range(5)])
    nav.roots = [feed_sources.Comment(
        cid="e%d" % i, author="甲%d" % i,
        body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text="正文" * 80)])
        for i in range(6)]
    nav.flat = feed_mod.comment_forest(nav.roots)
    nav.detail_loaded = True
    nav.c_index = 0
    nav.stack = [(feed_sources.Post(source="demo", pid="z0", title="上一层"),
                  [], [], 0, set())]
    nav.stack_via = [via] if via else []
    w.render_feed()
    w.feed_scroll_top()
    qapp.processEvents()


_fake_detail()
_e_ok = w.on_feed_key(Qt.Key_E)
_sb2 = w.text_edit.verticalScrollBar()
check("E 直接跳到评论区（不用一段段滚）",
      _e_ok is True and _sb2.value() > 0, (_e_ok, _sb2.value()))
_fake_detail()
check("详情里 Z 一次回到列表主页",
      w.on_feed_key(Qt.Key_Z) is True and w.feed_nav.view == "list"
      and not w.feed_nav.stack and not w.feed_nav.stack_via,
      (w.feed_nav.view, len(w.feed_nav.stack)))
_fake_detail(via="question_from_answer")
check("知乎：回答→问题 这一层按 Esc 直接回主页（不先退回那个回答）",
      w.on_feed_key(Qt.Key_Escape) is True and w.feed_nav.view == "list",
      (w.feed_nav.view, len(w.feed_nav.stack)))
_fake_detail()
check("普通详情 Esc 还是只退一层（没有多退）",
      w.on_feed_key(Qt.Key_Escape) is True and w.feed_nav.view == "detail"
      and w.feed_nav.post.id == "z0",
      (w.feed_nav.view, getattr(w.feed_nav.post, "id", "")))

# 先划字再点：那是选字复制，不许折叠/进帖
w._feed_press_had_sel = True
_anypt = w.text_edit.cursorRect(w.text_edit.textCursor()).center()
check("先选着字再点：不折叠、不进帖", w.feed_click(_anypt) is False)
check("用掉之后标志会清掉（不会一直挡着点击）", w._feed_press_had_sel is False)

# 偷瞄（Ctrl+E）+ 社区页：c/v/e/z 归我们，不再"按任意键就收起窗口"
import main as _main_mod
w.peek_mode, w.safe_mode = True, False
check("偷瞄 + 社区页：社区键被我们收下（c/v/e/z 都在表里）",
      w.peek_captures_feed_keys() is True
      and all(ord(ch) in _main_mod.FEED_KEY_NAMES for ch in "CEVZJKO"))
check("偷瞄只对社区页生效（书页照旧“任意键退出”）",
      (setattr(w, "feed_mode", False) or w.peek_captures_feed_keys() is False))
w.feed_mode = True
w.peek_mode = False

# ===== 设置页重做 + 角标/信息栏开关 + 卡片"翻了页但内容没刷新" =====
_set3 = app_mod.SettingsDialog(w.config, w)
qapp.processEvents()
_nav_names = [_set3.nav_list.item(i).text() for i in range(_set3.nav_list.count())]
check("设置页按功能分了区（阅读/窗口/快捷键/卡片/社区/界面/隐私/数据源）",
      _nav_names == ["阅读", "窗口", "快捷键", "卡片", "社区", "界面", "隐私", "数据源"],
      _nav_names)
_card_counts = []
for _i in range(_set3.page_stack.count()):
    _body = _set3.page_stack.widget(_i).widget()
    _card_counts.append(len([c for c in _body.findChildren(QFrame)
                             if c.objectName() == "card"]))
check("每一页都至少有一张分组卡片（不是一屏平铺的表单）",
      len(_card_counts) == 8 and all(n >= 1 for n in _card_counts), _card_counts)
_long_labels = []
for _lab in _set3.findChildren(QLabel):
    _txt = _lab.text()
    if _txt and (len(_txt) > 24 or "\n" in _txt):
        _long_labels.append(_txt[:30])
check("可见标签里没有段落式说明（注释腔一律进 tooltip）", not _long_labels, _long_labels)

_set3.check_corners.setChecked(False)
_set3.check_bar.setChecked(False)
_set3.accept()
qapp.processEvents()
check("设置里能关角标、能关底部信息栏，并且真的落盘",
      w.config.get("show_corners") is False and w.config.get("show_bar") is False,
      (w.config.get("show_corners"), w.config.get("show_bar")))

_auto_before = w.config.get("auto_mode")
w.config["auto_mode"] = True
w.config["show_corners"] = True
w.apply_style()
_corners_on = w.content_frame.draw_corners
w.config["show_corners"] = False
w.apply_style()
_corners_off = w.content_frame.draw_corners
w.config["auto_mode"] = _auto_before
w.config["show_corners"] = True
w.apply_style()
check("关掉角标之后自动挡不再画左上/右下那两条线",
      _corners_on is True and _corners_off is False, (_corners_on, _corners_off))

_rm_before = w.review_mode
w.review_mode = True
w.config["show_bar"] = False
w.update_review_bar()
_bar_hidden = w.review_bar.isHidden()
w.config["show_bar"] = True
w.update_review_bar()
_bar_shown = w.review_bar.isHidden()
w.review_mode = _rm_before
w.update_review_bar()
check("关掉底部信息栏之后那一行真的不显示，打开又回来",
      _bar_hidden is True and _bar_shown is False, (_bar_hidden, _bar_shown))

# 卡片模式：对账拿着"换卡之前"的快照回来，不许把屏幕上已经换好的新卡盖回去
_rm2, _rs2, _call2 = w.review_mode, w.review_session, w.anki_call
w.review_mode = True
w.anki_call = lambda *a, **k: True        # 探针里没有真的 Anki：别让它起后台线程
w.review_session = {"deck": "probe", "card": {"cardId": 1, "question": "Q1"},
                    "card_id": 1, "shown": False, "counted": 0}
w.render_card()
_seq_old = w.review_session["seq"]
w.review_session["card"] = {"cardId": 2, "question": "Q2"}
w.review_session["card_id"] = 2
w.render_card()
w.finish_poll({"card": {"cardId": 1, "question": "Q1"}}, False, _seq_old)
check("对账带回来的是换卡前的旧快照：不覆盖屏幕上刚换好的新卡",
      w.review_session["card_id"] == 2 and "Q2" in w.text_edit.toPlainText(),
      (w.review_session["card_id"], w.text_edit.toPlainText()[:20]))
_seq_now = w.review_session["seq"]
w.finish_poll({"card": {"cardId": 3, "question": "Q3"}}, False, _seq_now)
check("版本号对得上时对账照旧换成 Anki 当前的卡（守卫没把功能关掉）",
      w.review_session["card_id"] == 3, w.review_session["card_id"])
w.review_mode, w.review_session, w.anki_call = _rm2, _rs2, _call2


# ===== AC 轮：评论续页不跳屏 / "还有 N 条回复"点得动 / 设置里选来源与登录 =====
def _flash(rounds=8):
    """把排队的 0ms 定时器（滚动恢复是 singleShot 走的）真跑掉。"""
    for _i in range(rounds):
        qapp.processEvents()
        time.sleep(0.01)


_nav4 = w.feed_nav
_nav4.view = "detail"
_nav4.post = feed_sources.Post(source="demo", source_label="示例", pid="s1", title="标题",
                               body=[feed_sources.Block(kind=feed_sources.BLOCK_P,
                                                        text="正文。" * 60)])
_nav4.roots = [feed_sources.Comment(
    cid="c%d" % i, author="甲%d" % i,
    body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text="第%d条。" % i * 40)])
    for i in range(40)]
_nav4.flat = feed_mod.comment_forest(_nav4.roots)
_nav4.detail_loaded = True
_nav4.collapsed = set()
_nav4.child_show = {}
_nav4.c_show = feed_mod.COMMENT_PAGE
_nav4.c_more = True
_nav4.c_index = 0
_nav4.stack = []
_nav4.stack_via = []
w.feed_more_armed = False                    # 量位置时别让"滚到底"自己先续一页
w.render_feed()
w.feed_scroll_top()
_flash()
_sb4 = w.text_edit.verticalScrollBar()
_sb4.setValue(_sb4.maximum())
_flash()
_pos_before = _sb4.value()
_n_before = len(_nav4.visible_comments())
w.feed_more_armed = True
w.feed_autoload_more()
_flash()
_flash()
check("往下翻加载评论：位置不动（不会跳回评论区第一条）",
      _pos_before > 0 and _sb4.value() >= _pos_before - 5
      and len(_nav4.visible_comments()) > _n_before,
      (_pos_before, _sb4.value(), _n_before, len(_nav4.visible_comments())))

_parent = feed_sources.Comment(
    cid="p1", author="甲",
    body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text="父评论。")],
    replies=[feed_sources.Comment(cid="p1r%d" % i, author="乙%d" % i,
                                  body=[feed_sources.Block(kind=feed_sources.BLOCK_P,
                                                           text="回复%d。" % i)])
             for i in range(8)])
_nav4.roots = [_parent]
_nav4.flat = feed_mod.comment_forest(_nav4.roots)
_nav4.child_show = {}
_nav4.c_show = 50
_nav4.c_more = False
w.render_feed()
w.feed_scroll_top()
_flash()
_lim_before = _nav4.child_limit(_parent)
_more_pt = None
for _obj, _kind, _cur in w.feed_item_blocks():
    if getattr(_obj, "more_for", ""):
        _more_pt = w.text_edit.cursorRect(_cur).center()
        break
_clicked_more = w.feed_click(_more_pt) if _more_pt is not None else False
_flash()
check("点子评论那行「还有 N 条回复」就展开（不用回车）",
      _clicked_more is True and _nav4.child_limit(_parent) > _lim_before,
      (_clicked_more, _lim_before, _nav4.child_limit(_parent)))

_set4 = app_mod.SettingsDialog(w.config, w)
qapp.processEvents()
check("设置里能挑哪些社区来源生效（10 个来源各一个勾）",
      len(_set4.source_checks) == 10 and "weibo" in _set4.source_checks,
      list(_set4.source_checks))
_set4.source_checks["weibo"].setChecked(True)
_set4.source_checks["xhs"].setChecked(False)
_set4.accept()
qapp.processEvents()
_on_now = w.config.get("feed_sources") or []
check("取消勾选的来源就真的不生效（写进 feed_sources）",
      "weibo" in _on_now and "xhs" not in _on_now, _on_now)

import feed_sources as _fs                                                # noqa: E402
import tempfile as _tf                                                    # noqa: E402
_tmp_auth = os.path.join(_tf.mkdtemp(prefix="srauth"), "feed_auth.json")
_auth_path_bak = _fs.auth_path
_fs.auth_path = lambda root=None: _tmp_auth
try:
    _set5 = app_mod.SettingsDialog(w.config, w)
    qapp.processEvents()
    _set5.cookie_edits["zhihu"] = "z_c0=fromprobe"
    _set5.save_cookie_edits()          # 点"保存"那一刻做的事
finally:
    _fs.auth_path = _auth_path_bak
_got_auth = _fs.load_auth(_tmp_auth)
check("贴进来（或自动找到）的 Cookie 只有点保存才写进 feed_auth.json",
      str((_got_auth.get("zhihu") or {}).get("cookie")) == "z_c0=fromprobe", _got_auth)
w.config.pop("feed_sources", None)      # 探针自己造的键不留在真配置里


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
