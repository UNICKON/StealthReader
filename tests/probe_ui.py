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
app_mod.set_window_protection = lambda *a, **k: None    # 防截屏那层别真去碰系统窗口
OPENED = []
app_mod.os.startfile = lambda url: OPENED.append(url)

qapp = QApplication.instance() or QApplication(sys.argv)
w = app_mod.StealthReader()
w.save_config = lambda *a, **k: None
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
check("没 Cookie 时详情直说回答要登录、去哪儿配",
      "登录" in texts and "feed_auth.json" in texts, texts[:60])
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

# 8.3 真按键（走 Qt 事件，不是直接调 on_feed_key——以前探针只验了后者）
from PyQt5.QtGui import QKeyEvent                                          # noqa: E402
from PyQt5.QtCore import QEvent                                            # noqa: E402
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


def snap_layout():
    te = w.text_edit
    blk = te.document().firstBlock()
    return {
        "font": (te.font().family(), round(te.font().pointSizeF(), 1)),
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

# 8.5 设置窗口里真的有这个开关，而且取消勾选是"删掉键"而不是留个 0
_set_dlg = app_mod.SettingsDialog(w.config, w)
_set_dlg.feed_font_check.setChecked(True)
_set_dlg.feed_font_spin.setValue(19)
qapp.processEvents()
check("设置里有「社区页用单独字体」，一改就落到 config",
      w.config.get("feed_font_size") == 19 and bool(w.config.get("feed_font_family")),
      (w.config.get("feed_font_size"), w.config.get("feed_font_family")))
_set_dlg.feed_font_check.setChecked(False)
qapp.processEvents()
check("取消勾选 = 回到\"跟着书页走\"（键删掉，不是留个 0）",
      "feed_font_size" not in w.config and "feed_font_family" not in w.config,
      (w.config.get("feed_font_size"), w.config.get("feed_font_family")))
_set_dlg.reject()
qapp.processEvents()

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
