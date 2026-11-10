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

    def get_json(self, url, ttl=None, what="", headers=None, timeout=None, **kw):
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
from PyQt5.QtGui import QKeyEvent                                          # noqa: E402
from PyQt5.QtWidgets import QApplication, QFrame, QLabel                 # noqa: E402
from PyQt5.QtCore import Qt, QEvent, QPoint                              # noqa: E402

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
# 2026-10-27「示例社区」下架了：列表最后不再挂 demo，真来源之后直接回到混流
EXPECT_KEYS = ["mix", "ithome", "v2ex", "linuxdo", "zhihu", "weibo"]

print("=== 1. 来源登记与默认落点 ===")
keys = [s.key for s in w.feed_sources]
ORIG_SOURCES = list(w.feed_sources)          # 第 6 节会把来源换成替身，第 9 节要还原
check("顺序：混流 / IT之家 / V2EX / linux.do / 知乎 / 微博（没有示例了）",
      keys == EXPECT_KEYS, keys)
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
check("五个来源都轮到了", seq == EXPECT_KEYS[1:], seq)
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

print("\n=== 4b. 组合键不算单键：ctrl+alt+f 不该顺带换板块 ===")
# keyboard 库的全局热键回调**不吞按键**，所以热键那一下还会以普通按键落到窗口里。
# 真人 2026-10-22："按 caf 也会触发 f 的切换板块"——窗口内的单键必须只在纯按时生效。
_ch0 = w.feed_nav.channel
w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.ControlModifier | Qt.AltModifier))
pump(lambda: not w.feed_busy)
check("按 ctrl+alt+f（社区热键）不再顺带触发 f=上一个板块",
      w.feed_nav.channel == _ch0, (w.feed_nav.channel, _ch0))
w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.ControlModifier))
pump(lambda: not w.feed_busy)
check("只按着 ctrl 也不换（组合键一律不算单键）",
      w.feed_nav.channel == _ch0, (w.feed_nav.channel, _ch0))
w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.NoModifier))
pump(lambda: True)
# 真人 2026-11-02：「给各个社区加上搜索，f键输入，回车确认」——f 从"上一个板块"改成搜索，
# 上一个板块挪到 Shift+F（下面这条就钉住它没丢）
_fbox = getattr(w, "feed_search_box", None)
check("单按 f 打开搜索条（f 现在是搜索，不再换板块）",
      _fbox is not None and not _fbox.isHidden() and w.feed_nav.channel == _ch0,
      (_fbox is not None, w.feed_nav.channel, _ch0))
if _fbox is not None:
    _fbox.hide()
w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.ShiftModifier))
pump(lambda: not w.feed_busy)
check("Shift+F 才是上一个板块（原 f 的活）", w.feed_nav.channel != _ch0, w.feed_nav.channel)
_lk0 = bool(w.config.get("feed_lowkey", False))
w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_E, Qt.ControlModifier))
pump(lambda: True)
check("按 ctrl+e（偷瞄热键）不再顺带翻一次图",
      bool(w.config.get("feed_lowkey", False)) == _lk0, w.config.get("feed_lowkey"))
w.feed_goto_channel("热榜")
pump(lambda: not w.feed_busy)


class _FakeU32(object):
    """只答 GetAsyncKeyState：报哪几个键"按着"。"""

    def __init__(self, down):
        self.down = set(int(v) for v in down)

    def GetAsyncKeyState(self, vk):
        return 0x8000 if int(vk) in self.down else 0


_orig_u32 = app_mod._user32
try:
    app_mod._user32 = _FakeU32({0x11})
    check("偷瞄钩子：按着 Ctrl 认得出来（组合键不抢）",
          w.peek_input_hook._modifiers_down() is True)
    app_mod._user32 = _FakeU32({0xA2})
    check("偷瞄钩子：单独按左 Ctrl 也算（左右 Ctrl 都认）",
          w.peek_input_hook._modifiers_down() is True)
    app_mod._user32 = _FakeU32({0x12})
    check("偷瞄钩子：按着 Alt 也算", w.peek_input_hook._modifiers_down() is True)
    app_mod._user32 = _FakeU32({0x10})
    check("偷瞄钩子：只按着 Shift 不算（shift+字母还是那个字母）",
          w.peek_input_hook._modifiers_down() is False)
    app_mod._user32 = _FakeU32(set())
    check("偷瞄钩子：什么都没按就照旧归我们", w.peek_input_hook._modifiers_down() is False)
finally:
    app_mod._user32 = _orig_u32

print("\n=== 4c. 幽灵模式：鼠标离开窗口，社区页的正文也要藏起来 ===")
# 真人 2026-10-22："幽灵模式在社区模式没生效"。病根：变色龙那条只改文字控件的
# `color`，可社区页的正文是**内联颜色**（标题强调色、灰色小字、表格），改控件颜色
# 压不住，于是字一直亮着。现在改成 alpha=0 的图形效果罩住整个文字控件。
_gmode0 = w.config.get("ghost_mode", False)
_amode0 = w.config.get("auto_mode", False)
_cursor_bak = app_mod.QCursor
_adjust_bak = w.adjust_color_to_background


class _FakeCursor(object):
    """光标在窗口外面（离得很远），leaveEvent 才肯真的"离开"。"""

    @staticmethod
    def pos():
        return QPoint(-4000, -4000)


try:
    w.adjust_color_to_background = lambda: None      # 探针里不真去抓壁纸取色
    app_mod.QCursor = _FakeCursor
    w.config["ghost_mode"] = True
    w.config["auto_mode"] = True
    _solid0 = w.config.get("auto_bg_solid")
    w.config["auto_bg_solid"] = False                # 这一节量的是老档（几乎全透）
    w.apply_style()
    _sb0 = w.text_edit.verticalScrollBar().value()
    w.leaveEvent(QEvent(QEvent.Leave))
    _eff = w.text_edit.graphicsEffect()
    check("幽灵态：文字控件罩上一层 alpha=0 的效果（内联颜色也一起藏）",
          _eff is not None and abs(float(_eff.opacity()) - 0.0) < 1e-6,
          None if _eff is None else _eff.opacity())
    check("幽灵态：ghost_veiled 标记置上、划线高亮一起藏",
          w.ghost_veiled is True and not w.text_edit.extraSelections(), w.ghost_veiled)
    check("幽灵态（老档）：窗口不整体淡出（变色龙底色留着）",
          abs(w.windowOpacity() - 1.0) < 1e-6, w.windowOpacity())
    check("幽灵态不重排：滚动位置一点没动",
          w.text_edit.verticalScrollBar().value() == _sb0,
          (w.text_edit.verticalScrollBar().value(), _sb0))
    w.enterEvent(QEvent(QEvent.Enter))
    check("鼠标回来：罩子摘掉、正文照常", w.text_edit.graphicsEffect() is None
          and w.ghost_veiled is False,
          (w.text_edit.graphicsEffect(), w.ghost_veiled))
    w.config["auto_mode"] = False                    # 非自动挡走整窗透明度那条老路
    w.leaveEvent(QEvent(QEvent.Leave))
    # 系统把不透明度量化成 0~255（0.005 → 1/255 ≈ 0.0039），所以按"几乎全透明"判
    check("非自动挡：照旧整窗淡到看不见（≈0.005），也不加罩子",
          w.windowOpacity() <= 0.01
          and w.text_edit.graphicsEffect() is None, w.windowOpacity())
    w.enterEvent(QEvent(QEvent.Enter))
    check("非自动挡：鼠标回来恢复成配置里的不透明度",
          abs(w.windowOpacity() - float(w.config.get("opacity", 1.0))) < 1e-6,
          w.windowOpacity())
finally:
    app_mod.QCursor = _cursor_bak
    w.adjust_color_to_background = _adjust_bak
    w.config["ghost_mode"] = _gmode0
    w.config["auto_mode"] = _amode0
    w.config["auto_bg_solid"] = _solid0
    w.set_ghost_veil(False)
    w.apply_style()

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

# 6.3 真人 2026-11-03：「点进帖子再退出，没有回到点进去时的浏览位置」。
#     病根：`feed_list_pos` 只在"进/切来源"时记，**进帖不记**；Z（feed_home）读不到就
#     把 `feed_restore_pos` 设成 None，渲染时 `None` 表示"保持原样"——于是把**详情页的
#     滚动位置**搬回了列表，实际落在顶上。现在进帖时先记，退出一律按它回位。
_posts = [feed_mod.Post(source="ithome", pid="p%d" % i, title="第 %d 条帖子" % i,
                        excerpt="内容 " * 120, n_comments=3) for i in range(200)]
w.show()                      # 前面几节把窗口藏过：不显示就不排版，滚动条恒为 0
w.resize(825, 500)
w.feed_nav.reset()
w.feed_nav.channel = "热榜"
w.feed_nav.posts = _posts
w.feed_nav.index = 0
w.feed_gen += 1
_sb = w.text_edit.verticalScrollBar()
w.render_feed()
pump(lambda: _sb.maximum() > 500, 6.0)      # 200 条排一会儿才量得准
check("列表铺得够长、能滚（不然这一条验不了）", _sb.maximum() > 500, _sb.maximum())
_orig_feed_load = w.feed_load
w.feed_load = lambda what, post=None, fresh=False: None    # 详情不取数：只验回位
try:
    for _how, _idx in (("Z", 9), ("Backspace", 5)):
        w.feed_nav.index = _idx
        w.feed_highlight(ensure=True)
        pump(lambda: False, 0.4)
        _p0 = _sb.value()
        w.feed_open()
        pump(lambda: False, 0.3)
        _in_detail = (w.feed_nav.view == "detail" and _sb.value() == 0)
        if _how == "Z":
            w.on_feed_key(Qt.Key_Z)
        else:
            w.on_feed_key(Qt.Key_Backspace)
        pump(lambda: False, 1.1)
        check("按 %s 退出详情：回到进帖时那一行（进帖前 %d → 退出后 %d）"
              % (_how, _p0, _sb.value()),
              _in_detail and w.feed_nav.view == "list" and _p0 > 0
              and abs(_sb.value() - _p0) <= 40,
              (_in_detail, _p0, _sb.value(), w.feed_nav.view))
finally:
    w.feed_load = _orig_feed_load

print("\n=== 7. 本地书架：书从哪来、进度怎么算、移出只动账 ===")

import shutil                                                            # noqa: E402
import tempfile                                                          # noqa: E402

from main import (LocalShelfDialog, list_book_files,                     # noqa: E402
                  read_book_text_quietly, when_text)

if w.feed_mode:
    w.exit_feed_mode(reason="probe")
_shelf_dir = tempfile.mkdtemp(prefix="sr_shelf_")
try:
    _book_a = os.path.join(_shelf_dir, "万历十五年.txt")
    _book_b = os.path.join(_shelf_dir, "沉默的大多数.txt")
    with open(_book_a, "w", encoding="utf-8") as f:
        f.write("第一章 开端\n" + "字" * 399)              # 401 字
    with open(_book_b, "w", encoding="utf-8") as f:
        f.write("另一本\n" + "文" * 119)                   # 121 字
    with open(os.path.join(_shelf_dir, "不该进书架.pdf"), "wb") as f:
        f.write(b"%PDF-1.4")
    os.makedirs(os.path.join(_shelf_dir, "子目录"), exist_ok=True)
    with open(os.path.join(_shelf_dir, "子目录", "深处的书.txt"), "w", encoding="utf-8") as f:
        f.write("不算")
    _gbk = os.path.join(_shelf_dir, "国标编码的书.txt")
    with open(_gbk, "w", encoding="gb18030") as f:
        f.write("国标编码")

    orig_veiled = w._book_veiled
    w._book_veiled = lambda: False
    menu_texts = [a.text() for a in w.build_context_menu().actions()]
    w._book_veiled = orig_veiled
    check("右键菜单里有「本地书架」，线上那几个入口没了",
          any("本地书架" in t for t in menu_texts)
          and not any(("微信读书" in t or "网络书架" in t or "章节目录 (网络)" in t)
                      for t in menu_texts),
          [t for t in menu_texts if "书" in t])

    check("书架只认 txt/epub/md，且只认目录第一层",
          set(os.path.basename(p) for p in list_book_files(_shelf_dir))
          == {"国标编码的书.txt", "万历十五年.txt", "沉默的大多数.txt"},
          [os.path.basename(p) for p in list_book_files(_shelf_dir)])

    _orig_dir = w.shelf_dir
    _orig_save = w.save_config
    _keys = ("local_book_positions", "local_book_totals", "local_book_times",
             "local_book_hidden")
    _backup = {k: json.loads(json.dumps(w.config.get(k)))
               for k in _keys if k in w.config}
    _orig_scan = w.scan_shelf_totals
    w.shelf_dir = lambda: _shelf_dir
    w.save_config = lambda: None                       # 这一节只验账，不落盘
    w.scan_shelf_totals = lambda items: None           # 算字数那条后台线程不在前半段跑

    key_a = w.book_pos_key(_book_a)
    w.config["local_book_positions"] = {key_a: 200}
    w.config["local_book_totals"] = {key_a: 400}
    w.config["local_book_times"] = {key_a: time.time() - 3700}
    books = w.shelf_books()
    titles = [b["title"] for b in books]
    check("书架 = 读过的 + 目录里的（不认识的扩展名不算）",
          "万历十五年" in titles and "沉默的大多数" in titles
          and "不该进书架" not in titles and "深处的书" not in titles, titles)
    row_a = [b for b in books if b["title"] == "万历十五年"][0]
    check("进度 = 读到第几个字 / 全书多少字（200/400 = 50%）",
          row_a["pos"] == 200 and row_a["total"] == 400 and int(row_a["pct"]) == 50,
          (row_a["pos"], row_a["total"], row_a["pct"]))
    row_b = [b for b in books if b["title"] == "沉默的大多数"][0]
    check("没算过字数的写 --（不假装 0%）", row_b["total"] == 0 and row_b["pct"] is None,
          (row_b["total"], row_b["pct"]))
    check("最近读的排前面", books[0]["title"] == "万历十五年", titles)
    check("「上次读」是相对时间", when_text(time.time() - 7200) == "2 小时前",
          when_text(time.time() - 7200))
    check("gb18030 的 txt 也能算字数", len(read_book_text_quietly(_gbk)) == 4,
          len(read_book_text_quietly(_gbk)))
    check("坏路径 / 空路径不炸",
          read_book_text_quietly("") == ""
          and read_book_text_quietly(os.path.join(_shelf_dir, "没有这本.txt")) == "", None)

    dlg = LocalShelfDialog(w, w)
    check("书架窗口：一行一本，共 3 本",
          dlg.list_widget.count() == 3 and "3 本" in dlg.windowTitle(),
          (dlg.list_widget.count(), dlg.windowTitle()))
    check("书架窗口：有进度的那本标着 50%",
          any("50%" in dlg.list_widget.item(i).text() for i in range(dlg.list_widget.count())),
          [dlg.list_widget.item(i).text() for i in range(dlg.list_widget.count())])
    dlg.search.setText("沉默")
    check("搜索框按书名过滤", dlg.list_widget.count() == 1, dlg.list_widget.count())
    dlg.search.setText("")

    target = None
    for i in range(dlg.list_widget.count()):
        if "万历十五年" in dlg.list_widget.item(i).text():
            target = i
    dlg.list_widget.setCurrentRow(target)
    dlg.forget_selected()
    _t = w.config.get("local_book_totals") or {}
    _p = w.config.get("local_book_positions") or {}
    check("移出书架：三本账一起删，文件还在",
          key_a not in _p and key_a not in _t and os.path.exists(_book_a),
          (key_a in _p, key_a in _t, os.path.exists(_book_a)))
    check("移出书架后列表少一本（目录里还有那份文件也不再冒出来）",
          len(w.shelf_books()) == 2, len(w.shelf_books()))
    check("移出记进隐藏名单", key_a in (w.config.get("local_book_hidden") or []),
          w.config.get("local_book_hidden"))
    w.note_book_opened(_book_a, 401)
    check("再打开它，隐藏名单收回、书架里又有了", len(w.shelf_books()) == 3,
          (len(w.shelf_books()), w.config.get("local_book_hidden")))
    dlg.deleteLater()

    # 后半段把真线程放回来：字数算完必须从信号回到账上，窗口才不会一直写 `--`
    w.scan_shelf_totals = _orig_scan
    key_b = w.book_pos_key(_book_b)
    _tot = w.config.get("local_book_totals") or {}
    _tot.pop(key_b, None)
    w.config["local_book_totals"] = _tot
    _want = len(read_book_text_quietly(_book_b))
    w.scan_shelf_totals([(key_b, _book_b)])
    _deadline = time.time() + 8
    while time.time() < _deadline and key_b not in (w.config.get("local_book_totals") or {}):
        qapp.processEvents()
        time.sleep(0.05)
    _got = (w.config.get("local_book_totals") or {}).get(key_b)
    check("字数在后台算完、从信号回到账上（书架不会一直转）",
          _got == _want, (_got, _want))
    _row_b2 = [b for b in w.shelf_books() if b["key"] == key_b]
    check("算完那本就有进度了（不再写 --）",
          bool(_row_b2) and _row_b2[0]["total"] == _want and _row_b2[0]["pct"] is not None,
          _row_b2[0]["total"] if _row_b2 else None)

    w.shelf_dir = _orig_dir
    w.save_config = _orig_save
    w.scan_shelf_totals = _orig_scan
    for k in _keys:
        if k in _backup:
            w.config[k] = _backup[k]
        else:
            w.config.pop(k, None)
finally:
    shutil.rmtree(_shelf_dir, ignore_errors=True)

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

from PyQt5.QtGui import QTextCursor  # noqa: E402
# ===== 8.3b 阅读页 d/c 翻页：和左右箭头同一套（真人 2026-11-05）=====
print("\n=== 8.3b 阅读页 d/c 翻页 ===")
_PROBE_LONG = "永和九年岁在癸丑暮春之初会于会稽山阴之兰亭，修禊事也。" * 400


def _local_jump(key, start, modifiers=Qt.NoModifier, text=_PROBE_LONG):
    """把书页摆到 start，按一次键，返回落在哪个下标。"""
    w.is_local_mode = True
    w.local_file_path = "probe_keys.txt"
    w.local_full_text = text
    w.local_start_index = start
    w.local_page_history = []
    w.local_chapters = []
    w.local_heading_offsets = set()
    w.apply_style()
    w.render_local_page()
    qapp.processEvents()
    before = w.local_start_index
    w.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers))
    qapp.processEvents()
    return before, w.local_start_index


_b_right, _a_right = _local_jump(Qt.Key_Right, 0)
_b_d, _a_d = _local_jump(Qt.Key_D, 0)
check("d 往下翻，落到和 → 完全同一处",
      _a_d == _a_right and _a_d > _b_d, (_a_d, _a_right))

_mid = max(0, _a_right)  # 翻过一页的位置，用来测往回翻
_b_left, _a_left = _local_jump(Qt.Key_Left, _mid)
_b_c, _a_c = _local_jump(Qt.Key_C, _mid)
check("c 往上翻，落到和 ← 完全同一处",
      _a_c == _a_left and _a_c < _b_c, (_a_c, _a_left))

_b_ctrl, _a_ctrl = _local_jump(Qt.Key_D, _mid, Qt.ControlModifier)
check("ctrl+d 不算翻页（单键只在纯按时生效）",
      _a_ctrl == _b_ctrl, (_b_ctrl, _a_ctrl))

_b_shift, _a_shift = _local_jump(Qt.Key_D, 0, Qt.ShiftModifier)
check("shift+d 仍然算单键（和 shift+箭头一个待遇）",
      _a_shift == _a_right, (_a_shift, _a_right))


def _next_step(start, overlap):
    w.is_local_mode = True
    w.local_file_path = "probe_overlap.txt"
    w.local_full_text = _PROBE_LONG
    w.local_start_index = start
    w.local_page_history = []
    w.local_chapters = []
    w.local_heading_offsets = set()
    w.config["page_overlap_lines"] = overlap
    w.apply_style()
    w.render_local_page()
    qapp.processEvents()
    return w.calc_next_page_start()


_ov2 = _next_step(0, 2)
_ov0 = _next_step(0, 0)
_back = QTextCursor(w.text_edit.document())
_back.setPosition(_ov0)
_back.movePosition(QTextCursor.Up, QTextCursor.MoveAnchor, 2)
check("翻页保留 2 行上文（退回的距离正好等于往上两行）",
      _ov2 < _ov0 and _back.position() == _ov2, (_ov2, _ov0, _back.position()))
w.config["page_overlap_lines"] = 2


# 偷瞄（ce）里按 c/d：要能翻页，而且**不能**把偷瞄收起来（真人 2026-11-05）
def _peek_jump(name, start):
    w.is_local_mode = True
    w.local_file_path = "probe_peek.txt"
    w.local_full_text = _PROBE_LONG
    w.local_start_index = start
    w.local_page_history = []
    w.local_chapters = []
    w.local_heading_offsets = set()
    w.apply_style()
    w.render_local_page()
    qapp.processEvents()
    before = w.local_start_index
    w.peek_mode = True
    try:
        w.on_peek_book_key(name)
    finally:
        alive = w.peek_mode
        w.peek_mode = False
    return before, w.local_start_index, alive


_pk_right, _pk_a_right, _ = _peek_jump("right", 0)
_pk_b, _pk_a_d, _pk_live_d = _peek_jump("d", 0)
check("偷瞄里 d 也往下翻（和 → 同一处），偷瞄没收起来",
      _pk_a_d == _pk_a_right and _pk_live_d and _pk_a_d > _pk_b,
      (_pk_a_d, _pk_a_right, _pk_live_d))

_pk_left, _pk_a_left, _ = _peek_jump("left", _pk_a_right)
_pk_c_b, _pk_a_c, _pk_live_c = _peek_jump("c", _pk_a_right)
check("偷瞄里 c 也往上翻（和 ← 同一处），偷瞄没收起来",
      _pk_a_c == _pk_a_left and _pk_live_c and _pk_a_c < _pk_c_b,
      (_pk_a_c, _pk_a_left, _pk_live_c))

check("钩子层认 c/d（BOOK_KEY_NAMES 有 0x43/0x44，否则会落到“任意键退出偷瞄”）",
      app_mod.BOOK_KEY_NAMES.get(0x43) == "c" and app_mod.BOOK_KEY_NAMES.get(0x44) == "d",
      (app_mod.BOOK_KEY_NAMES.get(0x43), app_mod.BOOK_KEY_NAMES.get(0x44)))

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
    """从同一处往下滚一下，量实际走了多少像素。

    合成事件是紧挨着发的（毫秒内），而真人两格之间不可能这么近——所以这里把"同一格去重"
    的记时清零，保证每次调用都被当成新的一格。"""
    w._wheel_last_t = 0.0
    w._wheel_last_d = 0
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
        "font": (te.font().family(),
                 te.font().pixelSize() if te.font().pixelSize() > 0
                 else round(te.font().pointSizeF(), 1)),
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
check("社区页字号跟着 feed_font_size 走", w.text_edit.font().pixelSize() == 22,
      w.text_edit.font().pixelSize())
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
      w.text_edit.font().pixelSize() == _book_size,
      (w.text_edit.font().pixelSize(), w.config.get("font_size")))
check("社区页换过字体，书页的字体也得原样回来（控件字体 + 文档默认字体都要）",
      w.text_edit.font().family() == _book_fam
      and w.text_edit.document().defaultFont().family() == _book_fam
      and w.text_edit.document().defaultFont().pixelSize() == _book_size,
      (w.text_edit.font().family(), w.text_edit.document().defaultFont().family(),
       w.text_edit.document().defaultFont().pixelSize()))
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
      (w.config.get("lowkey_key") or "").strip().lower()
      == (app_mod.DEFAULT_CONFIG.get("lowkey_key") or "").strip().lower()
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
# 真人报"切了板块不刷新"：以前是先取数、回来了才重画，中间那几秒屏幕上还是上一栏的内容。
# 现在要求**当场就清空旧列表并按新栏目开取**。
# 这里**不能盯 feed_busy**：离线夹具的取数线程可能抢在断言之前跑完（信号一被处理就把它
# 置回 False），量到的是"已经取完了"而不是"没取"——这条以前就这么假红过一次。
# 改成记 feed_load 到底被调了哪一趟，跟线程快慢无关。
_calls = []
_orig_load = w.feed_load


def _spy_load(what, *a, **k):
    _calls.append(what)
    return _orig_load(what, *a, **k)


w.feed_load = _spy_load
w.config.pop("feed_board_boardsrc", None)
w.feed_nav.channel = "甲板"
_switched = w.feed_goto_channel("乙板")
w.feed_load = _orig_load
check("换板块当场生效", _switched and w.feed_nav.channel == "乙板", w.feed_nav.channel)
check("换板块当场就按新栏目开取（不是等数据回来才动）",
      w.feed_nav.view == "list" and "list" in _calls,
      (w.feed_nav.view, _calls, w.feed_busy))
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

# === 14b. 知乎热榜那种长「回答」：点在正文里收起，要落到这条回答自己的顶部 ===
print("\n=== 14b. 长回答收起：落到它自己的顶部 ===")
_LBODY = "很长的回答。" * 600
_LROOTS = [feed_sources.Comment(
    cid="L0", author="长回答的人", created="10-27", score=9,
    body=[feed_sources.Block(kind=feed_sources.BLOCK_P, text=_LBODY)]),
    ] + [feed_sources.Comment(cid="L%d" % i, author="后面的回答%d" % i, created="10-27", score=1,
                              body=[feed_sources.Block(kind=feed_sources.BLOCK_P,
                                                       text=("后面的回答 %d 的正文。" % i) * 30)])
         for i in range(1, 9)]
w.feed_nav.view = "detail"
w.feed_nav.stack = []
w.feed_nav.set_detail(_ans_post, _LROOTS)
w.feed_nav.collapsed = set()
w.feed_nav.c_index = 0
w.render_feed()
_settle()
_sbL = w.text_edit.verticalScrollBar()
check("（前置）长回答这页滚得动", _sbL.maximum() > 1200, _sbL.maximum())
_sbL.setValue(1200)                  # 读到回答中间：表头早就滚出屏幕
_settle()
_L0 = w.feed_nav.roots[0]
_yL_before = w._feed_comment_y(_L0)
check("（前置）长回答：表头在屏幕上方、正文铺满屏幕",
      _yL_before is not None and _yL_before < -400, _yL_before)
w.feed_nav.c_index = [c.id for c, _d, _p in w.feed_nav.visible_comments()].index("L0")
_lock = w.feed_toggle_collapse()
_settle()
_yL_after = w._feed_comment_y(_L0)
check("长回答点在正文里收起：落到**它自己的顶部**（视口顶上）",
      _lock is True and _yL_after is not None and abs(_yL_after - 6) <= 12,
      (_yL_before, _yL_after))
check("…而且是真收起来了（整段长正文不再铺开，只留一行摘要）",
      "L0" in w.feed_nav.collapsed and _LBODY not in w.text_edit.toPlainText(),
      (sorted(w.feed_nav.collapsed), len(w.text_edit.toPlainText())))
_yL_2 = w._feed_comment_y(_L0)
check("…再点一下展开：还停在它自己的顶部（不往回跳）",
      w.feed_toggle_collapse() is True and w._feed_comment_y(_L0) is not None
      and abs(w._feed_comment_y(_L0) - _yL_2) <= 12,
      (_yL_2, w._feed_comment_y(_L0)))

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
# 无图开关就是一个字母 e（真人："Ctrl+Alt+L 太长了，e 就行"）：窗口里有焦点时才生效
_save_e = w.save_config
w.save_config = lambda *a, **k: None
_lk0 = w.config.get("lowkey_key")
w.config["lowkey_key"] = "e"                # 人自己可能配的是组合键，这里只验"配成 e 时"
_low_e = bool(w.config.get("feed_lowkey", False))
_e1 = w.on_feed_key(Qt.Key_E)
check("E 一键换无图/有图（不用按长组合键）",
      _e1 is True and bool(w.config.get("feed_lowkey")) == (not _low_e),
      (w.config.get("feed_lowkey"), _low_e))
w.on_feed_key(Qt.Key_E)
pump(lambda: not w.feed_busy)
w.save_config = _save_e
w.config["lowkey_key"] = _lk0
check("再按一下 E 换回原样", bool(w.config.get("feed_lowkey")) == _low_e,
      w.config.get("feed_lowkey"))
check("无图键的默认值就是一个字母 e（单键不挂全局钩子，不然打字就会翻图）",
      (app_mod.DEFAULT_CONFIG.get("lowkey_key") or "").strip().lower() == "e",
      app_mod.DEFAULT_CONFIG.get("lowkey_key"))

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
    check("Shift+F 直接切回上一栏（跟 V 对称）",
          w.on_feed_key(Qt.Key_F, shift=True) is True and w.feed_nav.channel == _ch0,
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

# 刷新（R）＝从头看：当场回到顶部，不等数据回来
# 真人 2026-10-22："刷新的时候应该自动跳到顶部"。以前只发请求，滚动位置被
# render_feed 当成"同一屏重画"保住了：列表换了一页内容，屏幕还停在旧偏移上。
_reload_bak = w.feed_load
_calls = []
w.feed_load = lambda what, post=None, fresh=False: _calls.append((what, fresh))
try:
    _was = w.text_edit.verticalScrollBar().value()
    check("（前置）这会儿在下面，有得回", _was > 0, _was)
    w._feed_pending_scroll = 4242            # 排队中的"恢复旧位置"应该被作废
    _ok_reload = w.feed_reload()
    check("刷新当场回到顶部（不等数据回来）",
          _ok_reload and w.text_edit.verticalScrollBar().value() == 0,
          (_ok_reload, _was, w.text_edit.verticalScrollBar().value()))
    check("刷新照旧不吃缓存（fresh=True），且刷的是列表",
          _calls[-1:] == [("list", True)], _calls[-1:])
    check("排队中的旧位置恢复作废（别把屏幕拽回去）", w._feed_pending_scroll is None,
          w._feed_pending_scroll)
finally:
    w.feed_load = _reload_bak

print("\n=== 4e. 子评论：占位行回车就去站上再取一批 ===")
# 真人 2026-10-23："小红书的子评论只会加载一条吗"。它首页每条评论只塞 1 条预览
# （`sub_comment_count` 却写着 33），所以"还有 N 条回复"回车要能真去问，不能点了没反应。
_src_cur = w.feed_source()
_had_cp = hasattr(_src_cur, "children_page")
_cp_bak = getattr(_src_cur, "children_page", None)
_cp_calls = []


def _fake_children_page(post, cid, have=0, want=10):
    _cp_calls.append((str(cid), int(have)))
    return ([feed_mod.Comment(cid="extra1", author="后来的人",
                              body=[feed_mod.Block(kind=feed_mod.BLOCK_P, text="补的一条")])], 0)


try:
    _src_cur.children_page = _fake_children_page
    _nav = w.feed_nav
    _p = feed_mod.Comment(cid="p9", author="主",
                          body=[feed_mod.Block(kind=feed_mod.BLOCK_P, text="主评论")],
                          replies=[feed_mod.Comment(cid="s9", author="早", body=[])],
                          n_replies=33)
    _nav.view = "detail"
    _nav.post = feed_mod.Post(source=getattr(_src_cur, "key", "demo"), pid="9", title="t")
    _nav.roots = [_p]
    _nav.flat = feed_mod.comment_forest(_nav.roots)
    _nav.child_show = {}
    _nav.c_index = 2                  # 主评论 / 那 1 条子评论 / "还有…"占位
    w.render_feed()
    qapp.processEvents()
    _tgt = _nav.current_comment()
    check("（前置）回车停在「还有 N 条回复」那一行",
          getattr(_tgt, "more_for", "") == "p9", getattr(_tgt, "more_for", ""))
    w.feed_toggle_collapse()
    pump(lambda: not w._feed_children_busy, 5)
    check("占位回车：手里没有了就去站上问，带上父评论 id",
          _cp_calls[:1] == [("p9", 1)], _cp_calls[:2])
    check("取回来的接上树并直接摆出来",
          any(c.id == "extra1" for c in _p.replies) and _nav.child_limit(_p) == 2,
          [c.id for c in _p.replies])
finally:
    try:
        if _had_cp:
            _src_cur.children_page = _cp_bak
        else:
            delattr(_src_cur, "children_page")
    except Exception:
        pass
    w._feed_children_busy = False

print("\n=== 4f. X 折起/摊开这一楼的子评论，Shift+X 整页只看主楼 ===")
_lk_bak = w.config.get("lowkey_key")
w.config["lowkey_key"] = "e"          # 别让"无图键"把 X 抢走
try:
    def _mk_thread_page():
        nav = w.feed_nav
        nav.view = "detail"
        nav.post = feed_mod.Post(source=getattr(w.feed_source(), "key", "demo"),
                                 pid="7", title="t")
        nav.roots = [feed_mod.Comment(
            cid="x%d" % k, author="楼主%d" % k,
            body=[feed_mod.Block(kind=feed_mod.BLOCK_P, text="楼%d 的正文。" % k)],
            replies=[feed_mod.Comment(cid="x%d-%d" % (k, i), author="甲%d" % i,
                                      body=[feed_mod.Block(kind=feed_mod.BLOCK_P, text="回复%d。" % i)])
                     for i in range(6)]) for k in (0, 1)]
        nav.flat = feed_mod.comment_forest(nav.roots)
        nav.child_show = {}
        nav.child_saved = {}
        nav.collapsed = set()
        nav.c_show = 50
        nav.c_more = False
        nav.detail_loaded = True
        nav.c_index = 2               # 第 0 楼的第 2 条子回复
        w.render_feed()
        w.feed_scroll_top()
        qapp.processEvents()

    _mk_thread_page()
    _root0 = w.feed_nav.roots[0]
    _n_before = len(w.feed_nav.visible_comments())
    _ok_x = w.on_feed_key(Qt.Key_X)
    _settle()
    _ids_x = [c.id for c, _d, _p in w.feed_nav.visible_comments()]
    check("X：在子回复上按，折的是整楼（回复全收、楼根还在）",
          _ok_x is True and w.feed_nav.folded(_root0) and _ids_x[:2] == ["x0", "__more__x0"], _ids_x)
    check("折完光标落在楼根上，楼根还在屏幕里（收的是哪一楼找得着）",
          w.feed_nav.current_comment().id == "x0" and (w._feed_comment_y(_root0) or -1) > 0,
          (w.feed_nav.current_comment().id, w._feed_comment_y(_root0)))
    _ok_x2 = w.on_feed_key(Qt.Key_X)
    _settle()
    check("再按一次 X：摊回折之前的条数（不是回到 3 条）",
          _ok_x2 is True and not w.feed_nav.folded(_root0)
          and len(w.feed_nav.visible_comments()) == _n_before,
          (len(w.feed_nav.visible_comments()), _n_before))
    _ok_all = w.on_feed_key(Qt.Key_X, shift=True)
    _settle()
    _ids_all = [c.id for c, _d, _p in w.feed_nav.visible_comments()]
    check("Shift+X：整页只看主楼（两楼各剩一行「还有 N 条回复」）",
          _ok_all is True and _ids_all == ["x0", "__more__x0", "x1", "__more__x1"], _ids_all)
    _ok_all2 = w.on_feed_key(Qt.Key_X, shift=True)
    _settle()
    check("Shift+X 再来一次：都摊回去",
          _ok_all2 is True and len(w.feed_nav.visible_comments()) == _n_before,
          len(w.feed_nav.visible_comments()))
    _child_cur = None
    for _obj, _kind, _cur in w.feed_item_blocks():
        if getattr(_obj, "id", "") == "x0-1":
            _child_cur = _cur
            break
    check("（前置）屏幕上找得到 x0-1 这条子评论", _child_cur is not None)
    if _child_cur is not None:
        _clicked = w.feed_click(w.text_edit.cursorRect(_child_cur).center())
        _settle()
        check("点子评论：折的是它所在的整楼（不是只收这一条子回复）",
              _clicked is True and w.feed_nav.folded(_root0)
              and "x0-1" not in w.feed_nav.collapsed,
              (w.feed_nav.folded(_root0), sorted(w.feed_nav.collapsed)))
        check("点完光标回到楼根", w.feed_nav.current_comment().id == "x0",
              w.feed_nav.current_comment().id)
finally:
    w.config["lowkey_key"] = _lk_bak

print("\n=== 4g. 点「还有 N 条回复」与点子评论不打架 ===")
_lk_bak2 = w.config.get("lowkey_key")
w.config["lowkey_key"] = "e"
try:
    def _mk_page2(n_reps=8):
        nav = w.feed_nav
        nav.view = "detail"
        nav.post = feed_mod.Post(source="demo", pid="8", title="t")
        nav.roots = [feed_mod.Comment(
            cid="y0", author="楼主",
            body=[feed_mod.Block(kind=feed_mod.BLOCK_P, text="楼正文。")],
            replies=[feed_mod.Comment(cid="y0-%d" % i, author="甲%d" % i,
                                      body=[feed_mod.Block(kind=feed_mod.BLOCK_P,
                                                           text="回复%d。" % i)])
                     for i in range(n_reps)])]
        nav.flat = feed_mod.comment_forest(nav.roots)
        nav.child_show = {}
        nav.child_saved = {}
        nav.collapsed = set()
        nav.c_show = 50
        nav.c_more = False
        nav.detail_loaded = True
        nav.c_index = 0
        w.render_feed()
        w.feed_scroll_top()
        qapp.processEvents()

    def _ids2():
        return [c.id for c, _d, _p in w.feed_nav.visible_comments()]

    def _click2(obj_id):
        for obj, _kind, cur in w.feed_item_blocks():
            if getattr(obj, "id", "") == obj_id:
                r = w.feed_click(w.text_edit.cursorRect(cur).center())
                _settle()
                return r
        return None

    _mk_page2()
    _root2 = w.feed_nav.roots[0]
    check("点「还有 5 条回复」那一行：摊开下一批 3 条",
          _click2("__more__y0") is True
          and _ids2()[:8] == ["y0", "y0-0", "y0-1", "y0-2", "y0-3", "y0-4", "y0-5", "__more__y0"],
          _ids2())
    check("摊开之后这一楼就**不算**折着了（不然按 X 的方向会反过来）",
          w.feed_nav.folded(_root2) is False and w.feed_nav.child_saved == {},
          (w.feed_nav.folded(_root2), w.feed_nav.child_saved))
    check("摊开之后再点子评论：还是折起来（不是原地不动）",
          _click2("y0-1") is True and w.feed_nav.folded(_root2)
          and _ids2() == ["y0", "__more__y0"], (w.feed_nav.folded(_root2), _ids2()))
    check("折着的时候点那一行：摊开 3 条（不被折抢走）",
          _click2("__more__y0") is True
          and _ids2() == ["y0", "y0-0", "y0-1", "y0-2", "__more__y0"], _ids2())
    check("摊开后按 X：折起来（不是又摊回去）",
          w.on_feed_key(Qt.Key_X) is True and w.feed_nav.folded(_root2)
          and _ids2() == ["y0", "__more__y0"], _ids2())
    check("再按 X：摊回 3 条（不是跳回 8 条）",
          w.on_feed_key(Qt.Key_X) is True and not w.feed_nav.folded(_root2)
          and _ids2() == ["y0", "y0-0", "y0-1", "y0-2", "__more__y0"], _ids2())
finally:
    w.config["lowkey_key"] = _lk_bak2

print("\n=== 4h. 折起之后楼根落在哪儿：受控 ===")
_lk_bak3 = w.config.get("lowkey_key")
w.config["lowkey_key"] = "e"
try:
    def _mk_page3(reps=30):
        nav = w.feed_nav
        nav.view = "detail"
        nav.post = feed_mod.Post(source="demo", pid="9", title="t")
        nav.roots = [feed_mod.Comment(
            cid="z%d" % k, author="楼主%d" % k,
            body=[feed_mod.Block(kind=feed_mod.BLOCK_P, text=("楼%d 的正文。" % k) * reps)],
            replies=[feed_mod.Comment(cid="z%d-%d" % (k, i), author="甲%d" % i,
                                      body=[feed_mod.Block(kind=feed_mod.BLOCK_P,
                                                           text=("回复%d 的内容。" % i) * 20)])
                     for i in range(10)]) for k in range(3)]
        nav.flat = feed_mod.comment_forest(nav.roots)
        nav.child_show = {}
        nav.child_saved = {}
        nav.collapsed = set()
        nav.c_show = 50
        nav.c_more = False
        nav.detail_loaded = True
        nav.c_index = 0
        w.render_feed()
        w.feed_scroll_top()
        qapp.processEvents()

    def _sb3():
        return w.text_edit.verticalScrollBar()

    def _y3(cid):
        for obj, _kind, cur in w.feed_item_blocks():
            if getattr(obj, "id", "") == cid:
                return w.text_edit.cursorRect(cur).top()
        return None

    def _scroll_to_root3(target_y, cid="z0"):
        for _ in range(8):
            y = _y3(cid)
            if y is None or abs(y - target_y) <= 8:
                break
            _sb3().setValue(max(0, min(_sb3().maximum(), _sb3().value() + (y - target_y))))
            qapp.processEvents()

    def _click3(cid):
        for obj, _kind, cur in w.feed_item_blocks():
            if getattr(obj, "id", "") == cid:
                w.feed_click(w.text_edit.cursorRect(cur).center())
                _settle()
                qapp.processEvents()
                time.sleep(0.05)
                qapp.processEvents()      # 让 QTimer(0) 那些排单都跑完
                return True
        return False

    def _victim3(prefix):
        vh = w.text_edit.viewport().height()
        for obj, _kind, cur in w.feed_item_blocks():
            oid = str(getattr(obj, "id", ""))
            if oid.startswith(prefix) and 60 <= w.text_edit.cursorRect(cur).top() <= vh - 60:
                return oid
        return None

    def _span3(cid):
        """这条评论自己那段的 (top, bottom)：底部 = 文档里紧跟它的那条定位文本。"""
        items = list(w.feed_item_blocks())
        for k, (obj, _kind, cur) in enumerate(items):
            if str(getattr(obj, "id", "")) != cid:
                continue
            top = w.text_edit.cursorRect(cur).top()
            if k + 1 < len(items):
                return top, w.text_edit.cursorRect(items[k + 1][2]).top()
            return top, top
        return None, None

    vh3 = w.text_edit.viewport().height()

    # ---- 短楼：底部本来就在屏幕上 -> 折起之后一动不动
    _mk_page3(reps=3)
    _scroll_to_root3(120, "z0")
    _v = _victim3("z0-")
    _b0 = _span3("z0")[1]
    check("（前置）短评论：楼根底部就在屏幕里", _v is not None and 0 <= _b0 <= vh3 - 40,
          (_v, _b0))
    check("短评论：底部在屏幕上时，折起之后它停在同一高度（页面不跳）",
          _click3(_v) and _span3("z0")[1] is not None
          and abs(_span3("z0")[1] - _b0) <= 10,
          (_v, _b0, _span3("z0")))

    # ---- 已经翻进回复里：楼根底部在屏幕上方 -> 定位到楼根底部（75%），不拽回开头
    _mk_page3(reps=120)                    # 长楼：楼根自己就比一屏高
    w.feed_nav.child_show = {"z0": 10, "z1": 10, "z2": 10}
    w.render_feed()
    qapp.processEvents()
    _sb3().setValue(min(_sb3().maximum(), _sb3().value() + 1300))   # 一眼在回复中段
    qapp.processEvents()
    _vb = _victim3("z0-")
    _bb = _span3("z0")[1]
    check("（前置）人已经翻进回复里，楼根底部滚到了屏幕上方", _vb is not None and _bb < 0,
          (_vb, _bb))
    _okb = _click3(_vb)
    _yb, _bab = _span3("z0")
    qapp.processEvents()
    check("点子回复折起：定位到**这条评论的底部**（75% 处）",
          _okb and _bab is not None
          and (abs(_bab - int(vh3 * 0.75)) <= 40 or _sb3().value() <= _sb3().minimum() + 2),
          (_vb, _bb, _bab, int(vh3 * 0.75)))
    check("…没有把这条评论的开头拽回来（真人报的「现在是顶部」）",
          _yb is not None and _yb < 0, (_yb, _bab))
    check("定位不会被事后那次「恢复旧位置」盖回去（滚完再跑事件循环还是那个位置）",
          abs(_span3("z0")[1] - _bab) <= 2, (_bab, _span3("z0")[1]))

    # ---- 第二楼同理
    _mk_page3(reps=3)
    _scroll_to_root3(120, "z1")
    _v3 = _victim3("z1-")
    _b3 = _span3("z1")[1]
    check("第二楼同理：折起后底部还在原来的高度",
          _click3(_v3) and _span3("z1")[1] is not None and abs(_span3("z1")[1] - _b3) <= 10,
          (_v3, _b3, _span3("z1")))

    # ================================================================ 4i
    print("\n=== 4i. 楼里有 3、4 层「之评论」：动作和两层时同一套 ===")

    def _mk_deep(long_root=False):
        def mk(cid, depth, reps):
            kids = []
            if depth < 3:
                kids = [mk(cid + "-%d" % k, depth + 1, 1) for k in range(2)]
            return feed_mod.Comment(
                cid=cid, author="甲" + cid,
                body=[feed_mod.Block(kind=feed_mod.BLOCK_P,
                                     text=("%s 的正文。" % cid) * reps)],
                replies=kids)

        nav = w.feed_nav
        nav.view = "detail"
        nav.post = feed_mod.Post(source="demo", pid="d9", title="t")
        nav.roots = [mk("d0", 0, 200 if long_root else 3)]
        nav.flat = feed_mod.comment_forest(nav.roots)
        nav.child_show = {}
        nav.child_saved = {}
        nav.collapsed = set()
        nav.c_show = 50
        nav.c_more = False
        nav.detail_loaded = True
        nav.c_index = 0
        w.render_feed()
        w.feed_scroll_top()
        qapp.processEvents()

    _mk_deep()
    _vis = w.feed_nav.visible_comments()
    check("树里真有 4 层（0..3）——知乎那种「之评论的之评论」",
          sorted(set(d for _c, d, _p in _vis)) == [0, 1, 2, 3],
          sorted(set(d for _c, d, _p in _vis)))
    _all_ids = [c.id for c, _d, _p in w.feed_nav.visible_comments()]
    _deep_id = "d0-0-1-0"
    w.feed_nav.c_index = _all_ids.index(_deep_id)
    check("（前置）光标落在第 3 层那条上", _deep_id.count("-") == 3, _deep_id)
    _click3(_deep_id)
    _nav = w.feed_nav
    check("点第 4 层那条：折的是**整楼楼根**（和点两层时同一套规矩）",
          _nav.child_limit(_nav.roots[0]) == 0 and _nav.roots[0].id in _nav.child_saved,
          (_nav.child_limit(_nav.roots[0]), list(_nav.child_saved)))
    check("折完光标回到楼根", _nav.current_comment() is not None
          and _nav.current_comment().id == "d0", getattr(_nav.current_comment(), "id", None))
    check("折起来只剩楼根 + 一行「还有 N 条回复」",
          len(_nav.visible_comments()) == 2,
          [c.id for c, _d, _p in _nav.visible_comments()])
    w.feed_fold()                          # 楼根上再折一次 = 摊开
    check("再折一次摊回折之前的条数（15 条，不是只摊 3 条）",
          _nav.child_limit(_nav.roots[0]) >= 3 and len(_nav.visible_comments()) == 15,
          (_nav.child_limit(_nav.roots[0]), len(_nav.visible_comments())))

    # 深楼（楼根很长、点的是最深那条）:定位到楼根底部，不拽回楼根开头
    _mk_deep(long_root=True)
    _nav = w.feed_nav
    _vis = _nav.visible_comments()
    _deep = [c.id for c, d, _p in _vis if d == 3]
    check("（前置）第 4 层那条在文档里", bool(_deep), _deep)
    if _deep:
        _sb3().setValue(min(_sb3().maximum(), int(_span3(_deep[0])[0]) - int(vh3 * 0.4)))
        qapp.processEvents()
        _click3(_deep[0])
        _y4, _b4 = _span3("d0")
        check("长楼里点最深那条：还是定位到楼根底部（75% 处），不是楼根开头",
              _b4 is not None and (abs(_b4 - int(vh3 * 0.75)) <= 70
                                   or _sb3().value() >= _sb3().maximum() - 2),
              (_b4, int(vh3 * 0.75)))
        check("…楼根开头确实在屏幕外（没被拽回去）", _y4 is not None and _y4 < 0, _y4)

    # 第 3 层的「还有 N 条回复」照样能摊开
    _mk_deep()
    _nav = w.feed_nav
    _nav.child_show["d0-0-1"] = 1          # 第 2 层那条只摆 1 条，留一条在占位行里
    w.render_feed()
    qapp.processEvents()
    _rows = [(c.id, d) for c, d, _p in _nav.visible_comments()]
    check("（前置）第 3 层那条的回复被收成了一行占位",
          ("__more__d0-0-1", 3) in _rows, [r for r in _rows if r[0].startswith("__more__")])
    _click3("__more__d0-0-1")
    check("点第 3 层那行「还有 N 条回复」：照常摊开（不再只是顶层能用）",
          _nav.child_limit(_nav.comment_by_id("d0-0-1")) >= 2,
          _nav.child_show.get("d0-0-1"))

finally:
    w.config["lowkey_key"] = _lk_bak3

# 详情：B 到评论区顶部（原来是 E，E 让给无图开关了）、Z 一口气回列表、
# 知乎"回答→问题"的 Esc 也回列表
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
_e_ok = w.on_feed_key(Qt.Key_B)
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
check("设置页按功能分了区（阅读/窗口/快捷键/卡片/社区/界面/隐私）",
      _nav_names == ["阅读", "窗口", "快捷键", "卡片", "社区", "界面", "隐私"],
      _nav_names)
_card_counts = []
for _i in range(_set3.page_stack.count()):
    _body = _set3.page_stack.widget(_i).widget()
    _card_counts.append(len([c for c in _body.findChildren(QFrame)
                             if c.objectName() == "card"]))
check("每一页都至少有一张分组卡片（不是一屏平铺的表单）",
      len(_card_counts) == 7 and all(n >= 1 for n in _card_counts), _card_counts)
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

# 角标只在**鼠标凑近那个角**的时候才画（真人 2026-10-23：「角标在鼠标移到附近的时候才显示」）
from PyQt5.QtGui import QImage                                            # noqa: E402
_solid_before = w.config.get("auto_bg_solid")       # 数像素要在"透底"上看（见下）
w.config["auto_bg_solid"] = False
w.config["auto_mode"] = True
w.config["show_corners"] = True
w.apply_style()
qapp.processEvents()
_fr = w.content_frame
check("融景模式 + 开着角标：才有人盯着鼠标（那条定时器在跑）",
      _fr._hot_timer.isActive(), _fr._hot_timer.isActive())
check("鼠标在窗口中间：两个角都不算凑近",
      _fr._corners_at(_fr.width() // 2, _fr.height() // 2) == (False, False),
      _fr._corners_at(_fr.width() // 2, _fr.height() // 2))
check("鼠标已经出窗口了：也不算凑近（不会隔着别的窗口亮起来）",
      _fr._corners_at(-5, 5) == (False, False) and
      _fr._corners_at(_fr.width() + 5, 5) == (False, False),
      (_fr._corners_at(-5, 5), _fr._corners_at(_fr.width() + 5, 5)))
check("凑近左上角只热左上、凑近右下角只热右下（各角各算各的）",
      _fr._corners_at(5, 5) == (True, False)
      and _fr._corners_at(_fr.width() - 5, _fr.height() - 5) == (False, True),
      (_fr._corners_at(5, 5), _fr._corners_at(_fr.width() - 5, _fr.height() - 5)))
check("离角再远一点（超过 CORNER_HOVER）就不亮了",
      _fr._corners_at(_fr.CORNER_HOVER + 10, _fr.CORNER_HOVER + 10) == (False, False),
      _fr._corners_at(_fr.CORNER_HOVER + 10, _fr.CORNER_HOVER + 10))


def _corner_pixels(px, py):
    """把这一帧真的画出来，数左上 / 右下两个 24×24 方块里有多少笔画像素。"""
    _fr._cursor_local = lambda: QPoint(px, py)
    _fr._poll_hot_corners()
    img = QImage(max(24, _fr.width()), max(24, _fr.height()), QImage.Format_ARGB32)
    img.fill(0)
    _fr.render(img)
    iw, ih = img.width(), img.height()

    def _count(x0, y0):
        n = 0
        for x in range(max(0, x0), min(iw, x0 + 24)):
            for y in range(max(0, y0), min(ih, y0 + 24)):
                if ((img.pixel(x, y) >> 24) & 0xFF) >= 120:
                    n += 1
        return n

    return _count(0, 0), _count(iw - 24, ih - 24)


_c_mid = _corner_pixels(_fr.width() // 2, _fr.height() // 2)
check("鼠标在中间：一帧里一条角标都没画", _c_mid == (0, 0), _c_mid)
_c_tl = _corner_pixels(4, 4)
check("凑到左上角：左上那条露出来、右下那条仍然不画",
      _c_tl[0] >= 20 and _c_tl[1] == 0, _c_tl)
_c_br = _corner_pixels(_fr.width() - 4, _fr.height() - 4)
check("凑到右下角：换成右下那条，左上又收回去", _c_br[0] == 0 and _c_br[1] >= 20, _c_br)
_c_again = _corner_pixels(_fr.width() // 2, _fr.height() // 2)
check("鼠标挪回中间：两条又都收回去（不是亮过就一直亮）", _c_again == (0, 0), _c_again)

w.config["show_corners"] = False
w.config["auto_bg_solid"] = _solid_before            # 这一节量完了，把实色档放回去
w.apply_style()
qapp.processEvents()
check("关掉角标：连定时器都不留（不盯着鼠标、也不画）",
      not _fr._hot_timer.isActive() and _fr._hot == (False, False),
      (_fr._hot_timer.isActive(), _fr._hot))
w.config["show_corners"] = True
w.config["auto_mode"] = False
w.apply_style()
qapp.processEvents()
check("退出融景模式：定时器停掉，角标状态清零",
      not _fr._hot_timer.isActive() and _fr._hot == (False, False),
      (_fr._hot_timer.isActive(), _fr._hot))
w.config["auto_mode"] = _auto_before
w.config["show_corners"] = True
w.apply_style()
qapp.processEvents()

# 重启用哪个解释器（2026-10-26：灵犀的 python-env 被换成半截解压，pythonw 直接没了，
# 托盘「重新启动」和解锁自动重启全都 WinError 2 —— 别再把命押在一个固定路径上）
_exe0 = w._restart_exe()
check("重启解释器：挑出来的那个真的在硬盘上", bool(_exe0) and os.path.isfile(_exe0), _exe0)
_old_env = os.environ.get("STEALTHREADER_PYW")
os.environ["STEALTHREADER_PYW"] = _exe0
check("重启解释器：启动器挑好的那个排第一（STEALTHREADER_PYW）",
      w._restart_exe() == _exe0, w._restart_exe())
_dead = os.path.join(os.path.dirname(_exe0), "nope_pythonw.exe")
os.environ["STEALTHREADER_PYW"] = _dead
_fb = w._restart_exe()
check("重启解释器：启动器给的那个已经不在了，就退到还活着的那个（不拿死路径去起进程）",
      os.path.isfile(_fb) and _fb != _dead, (_dead, _fb))
if _old_env is None:
    os.environ.pop("STEALTHREADER_PYW", None)
else:
    os.environ["STEALTHREADER_PYW"] = _old_env

# 启动器叫醒：隐藏状态下再点一次快捷方式，第二个进程撞上单实例互斥体，
# 它应该是"把那一个亮出来"，而不是弹个"已经在运行了"就退（2026-10-27 真人报的现象）
_was_visible = w.isVisible()
w.hide()
qapp.processEvents()
_hidden_now = w.isVisible()
_sig = app_mod.signal_wake()
qapp.processEvents()
w.poll_wake_event()                    # 探针里直接跑一次，不等那 400ms 的定时器
qapp.processEvents()
check("叫醒：隐藏着的时候收到启动器信号，窗口真的出来了",
      _hidden_now is False and _sig is True and w.isVisible() is True,
      (_hidden_now, _sig, w.isVisible()))
_sig2 = app_mod.signal_wake()
w.poll_wake_event()
qapp.processEvents()
check("叫醒：窗口本来就亮着时只把它提到前面，不会反手收起来",
      _sig2 is True and w.isVisible() is True, (_sig2, w.isVisible()))
_sig3 = app_mod.signal_wake("StealthReader_Wake_%d_没人监听" % os.getpid())
check("叫醒：没人监听这个名字时返回 False（老版本就退回到弹框那条路）",
      _sig3 is False, _sig3)
if not _was_visible:
    w.hide()
    qapp.processEvents()


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
check("设置里能挑哪些社区来源生效（11 个来源各一个勾）",
      len(_set4.source_checks) == 11 and "weibo" in _set4.source_checks
      and "douban" in _set4.source_checks,
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


# 托盘右键 → 重新启动（双开有害：两个进程同时响应老板键、互相覆盖进度）
check("命令行里认得出 --restart-wait 的 pid",
      app_mod.parse_restart_wait(["main.py", "--restart-wait", "1234"]) == 1234
      and app_mod.parse_restart_wait(["main.py"]) is None
      and app_mod.parse_restart_wait(["--restart-wait", "abc"]) is None,
      app_mod.parse_restart_wait(["main.py", "--restart-wait", "1234"]))
check("探活：自己的 pid 活着，0 是死的",
      app_mod._pid_alive(os.getpid()) is True and app_mod._pid_alive(0) is False,
      (app_mod._pid_alive(os.getpid()), app_mod._pid_alive(0)))

_tray_menu = w.tray_icon.contextMenu()
_tray_texts = [a.text() for a in _tray_menu.actions() if not a.isSeparator()]
check("托盘右键里有「重新启动」，就在「退出」上一行",
      "重新启动" in _tray_texts
      and _tray_texts.index("重新启动") == _tray_texts.index("退出") - 1,
      _tray_texts[-3:])

_spawned, _quits = [], []
_popen_bak = app_mod.subprocess.Popen
_pid_alive_bak = app_mod._pid_alive
_quit_bak = w.quit_app
_save_bak = w.save_before_restart


class _FakeProc(object):
    pid = 424242


app_mod.subprocess.Popen = lambda args, **kw: (_spawned.append((list(args), dict(kw))),
                                               _FakeProc())[1]
app_mod._pid_alive = lambda pid: True
w.quit_app = lambda: _quits.append(1)
w.save_before_restart = lambda: None
try:
    w.restart_app()
    _args, _kw = _spawned[0]
    check("重新启动：pythonw 起 main.py，并把本进程 pid 传过去让它先等",
          _args[0].lower().endswith("pythonw.exe")
          and _args[1].replace("\\", "/").endswith("main.py")
          and _args[2] == "--restart-wait" and _args[3] == str(os.getpid()),
          _args)
    check("新进程带 DETACHED_PROCESS（不跟着老进程被收走）",
          bool(_kw.get("creationflags", 0) & 0x00000008), _kw.get("creationflags"))
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.05)
    check("新进程起来了才退出自己", _quits == [1], _quits)

    _quits[:] = []
    app_mod._pid_alive = lambda pid: False          # 新进程起不来
    w.restart_app()
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.05)
    check("新进程没起来就不退出（别把程序弄没了）", _quits == [], _quits)
finally:
    app_mod.subprocess.Popen = _popen_bak
    app_mod._pid_alive = _pid_alive_bak
    w.quit_app = _quit_bak
    w.save_before_restart = _save_bak

# 锁屏解锁 → 后台静默重启，起来保持隐藏；进度与窗口位置在退出前就钉住
check("命令行里认得出 --hidden",
      app_mod.parse_hidden_flag(["main.py", "--hidden"]) is True
      and app_mod.parse_hidden_flag(["main.py"]) is False,
      app_mod.parse_hidden_flag(["main.py", "--hidden"]))

_unlock_spawned, _unlock_finished = [], []
_popen_bak2 = app_mod.subprocess.Popen
_pid_alive_bak2 = app_mod._pid_alive
_finish_bak = w._finish_restart
_save_bak2 = w.save_before_restart
_cfg_ru = w.config.get("restart_on_unlock", True)
_ilm_bak = w.is_local_mode
_veiled_bak = w._book_veiled
_cto_bak = w.current_top_offset
_rm_bak = w.review_mode
_save_bak3 = w.save_config
try:
    app_mod.subprocess.Popen = lambda args, **kw: (_unlock_spawned.append(list(args)),
                                                   _FakeProc())[1]
    app_mod._pid_alive = lambda pid: True
    w._finish_restart = lambda: _unlock_finished.append(1)
    w.save_before_restart = lambda: None
    w.save_config = lambda *a, **k: None

    # ① 解锁那条路：带 --hidden 起新进程
    w._last_unlock_restart = 0.0
    w.config["restart_on_unlock"] = True
    w.review_mode = False
    w.restart_on_unlock()
    check("解锁后重启：新进程带 --hidden（起来藏着，不弹到人脸上）",
          len(_unlock_spawned) == 1 and "--hidden" in _unlock_spawned[0],
          _unlock_spawned[0] if _unlock_spawned else None)

    # ② 二十秒内只重启一次（解锁通知会成串来）
    _n = len(_unlock_spawned)
    w.restart_on_unlock()
    check("解锁通知成串来的时候只重启一次", len(_unlock_spawned) == _n, len(_unlock_spawned))

    # ③ 正在隐形背诵 / 有弹窗 / 关了开关，都不重启
    w._last_unlock_restart = 0.0
    w.review_mode = True
    w.restart_on_unlock()
    w.review_mode = False
    w._last_unlock_restart = 0.0
    w.config["restart_on_unlock"] = False
    w.restart_on_unlock()
    w.config["restart_on_unlock"] = True
    check("背诵中 / 开关关着都不重启（别惊动 Anki、也让人能自己关掉）",
          len(_unlock_spawned) == _n, len(_unlock_spawned))

    # ④ 托盘手动重启照旧显示（不带 --hidden）
    _n = len(_unlock_spawned)
    w.restart_app()
    check("托盘那一次照常显示（不带 --hidden）",
          len(_unlock_spawned) == _n + 1 and "--hidden" not in _unlock_spawned[-1],
          _unlock_spawned[-1])

    # ⑤ 重启前真的把"进度 + 窗口位置"钉住
    w.is_local_mode = True
    w._book_veiled = lambda: False
    w.current_top_offset = lambda: 12345
    w.config["last_local_pos"] = 0
    w.save_before_restart = _save_bak2
    w.save_before_restart()
    check("重启前记下的是视口顶那一行（不是本页页首）", w.config.get("last_local_pos") == 12345,
          w.config.get("last_local_pos"))
    check("重启前窗口位置也记下来了",
          isinstance(w.config.get("win_x"), int) and isinstance(w.config.get("win_y"), int),
          (w.config.get("win_x"), w.config.get("win_y")))
finally:
    app_mod.subprocess.Popen = _popen_bak2
    app_mod._pid_alive = _pid_alive_bak2
    w._finish_restart = _finish_bak
    w.save_before_restart = _save_bak2
    w.save_config = _save_bak3
    w.is_local_mode = _ilm_bak
    w._book_veiled = _veiled_bak
    w.current_top_offset = _cto_bak
    w.review_mode = _rm_bak
    w.config["restart_on_unlock"] = _cfg_ru
    w._last_unlock_restart = 0.0

# 窗口摆放（"按了老板键，日志说显示了，屏幕上什么都没有，然后一闪一闪"）：
# 两个信号任一为真就摆一次，**摆只用 Qt 的坐标**（Win32 挪 + 除以 dpr 同步回来，两套坐标一打架
# 窗口就在屏幕内外来回跳 = 人看到的闪烁），摆完冷却 30 秒、摆不好安静五分钟。
_ors_bak = app_mod.window_on_real_screen
_usable_bak = w._rect_usable
_save_bak = w.save_config
_wx, _wy = w.config.get("win_x"), w.config.get("win_y")
w.save_config = lambda *a, **k: None
try:
    app_mod.window_on_real_screen = lambda hwnd: False
    app_mod.window_on_real_screen = lambda hwnd: True
    _b1, _w1 = w._window_off_screen()
    app_mod.window_on_real_screen = lambda hwnd: False
    _b2, _w2 = w._window_off_screen()
    check("Win32 说不在任何屏幕上就算有问题（Qt 说可见也不放过）",
          _b2 is True and "Win32" in _w2, (_b2, _w2))

    w._rect_usable = lambda rect: False
    app_mod.window_on_real_screen = lambda hwnd: True
    _b3, _w3 = w._window_off_screen()
    w._rect_usable = _usable_bak
    check("Qt 说看不见也算有问题（Win32 说可见也不放过）",
          _b3 is True and "Qt" in _w3, (_b3, _w3))

    w.show()
    qapp.processEvents()
    w.move(900, 900)                          # 屏幕外
    w.config["win_x"], w.config["win_y"] = 12, 34
    w._shown_at = time.time() - 5.0
    w._place_cooldown_until = 0.0
    w._place_fail_streak = 0
    app_mod.window_on_real_screen = lambda hwnd: False
    w.guard_window_visible()
    qapp.processEvents()
    check("摆到记住的位置（只用 Qt 的 setGeometry）", (w.x(), w.y()) == (12, 34), (w.x(), w.y()))
    check("摆完先安静 30 秒（不在屏幕上抽）",
          w._place_cooldown_until > time.time() + 20,
          round(w._place_cooldown_until - time.time(), 1))

    w.move(900, 900)
    w.guard_window_visible()
    qapp.processEvents()
    check("冷却期里再复核也不动它（这就是\"不闪\"的保证）", (w.x(), w.y()) == (900, 900),
          (w.x(), w.y()))
finally:
    app_mod.window_on_real_screen = _ors_bak
    w._rect_usable = _usable_bak
    w.save_config = _save_bak
    w.config["win_x"], w.config["win_y"] = _wx, _wy
    w._place_cooldown_until = 0.0
    w.move(465, 434)
    qapp.processEvents()

# ===== 热键叫不回来（锁屏后按老板键没反应）：两条自救路 =====
# 真凶之一：锁屏那一下的松键事件进了安全桌面，库以为 Win 还按着 → ctrl+s 等全匹配不上。
# 之二：WH_KEYBOARD_LL 被系统摘掉，库仍自报 listening=True，等看门狗重挂最多 90 秒。
_p1 = w._parse_poll_key("ctrl+s")
check("轮询认得出 ctrl+s（修饰键 = VK_CONTROL，主键 = S）",
      _p1 is not None and _p1[0] == ((0x11,),) and _p1[1] == 0x53, _p1)
check("轮询认不出乱写的键就返回 None（宁可不动，也不瞎按）",
      w._parse_poll_key("ctrl+notakey") is None and w._parse_poll_key("ctrl+") is None)
check("卡住的键写得成人话（左Win，不是十六进制）", w._scan_name(0x5B) == "左Win",
      w._scan_name(0x5B))

_kb = app_mod.keyboard
_pe_bak = dict(getattr(_kb, "_pressed_events", {}) or {})
_pd_bak = w._physically_down
_rec_bak = w.recover_hotkeys
_log_bak = app_mod.log_event
_msgs = []
app_mod.log_event = lambda _m: _msgs.append(str(_m))
w.hotkey_poll_timer.stop()
try:
    _kb._pressed_events.clear()
    _kb._pressed_events[0x5B] = object()        # 假装 Win 还按着（其实早松了）
    w._physically_down = lambda vks: False      # 物理上全松开
    _ret = w.hotkey_hook_probe(type("_Ev", (), {"scan_code": 0x1F, "name": "s"})())
    check("按下时就把卡住的 Win 擦掉（解锁后第一下就管用）",
          0x5B not in _kb._pressed_events and _ret is True,
          sorted(_kb._pressed_events))
    check("擦的时候日志里写人话（左Win）", any("左Win" in m for m in _msgs), _msgs)

    _kb._pressed_events.clear()
    _kb._pressed_events[0x5B] = object()
    w.hotkey_hook_probe(type("_Ev", (), {"scan_code": 0x5B, "name": "windows"})())
    check("这一下自己的键不动（不然松键时把自己擦了）",
          0x5B in _kb._pressed_events, sorted(_kb._pressed_events))

    _hit = []
    w.hotkey_poll_keys = [("试", ((), 0x53), lambda: _hit.append(1))]
    w.hotkey_poll_state = {}
    w._physically_down = lambda vks: True
    w._hook_seen_at = time.time()               # 钩子刚报过到 → 不判它瞎了
    for _ in range(3):
        w.poll_hotkeys()
    check("按着不放只算一下（看上升沿）", len(_hit) == 1, len(_hit))
    w._physically_down = lambda vks: False
    w.poll_hotkeys()
    w._physically_down = lambda vks: True
    w.poll_hotkeys()
    check("松开再按 = 新的一下", len(_hit) == 2, len(_hit))

    _reasons = []
    w.recover_hotkeys = lambda reason="": _reasons.append(reason)
    w.hotkey_poll_keys = [("试2", ((), 0x53), lambda: None)]
    w.hotkey_poll_state = {}
    w._physically_down = lambda vks: True
    w._hook_seen_at = time.time() - 30.0        # 钩子半分钟没报到了
    w.poll_hotkeys()
    pump(lambda: bool(_reasons), seconds=2.0)
    check("钩子半分钟没动静却看到按下 → 自己重挂一遍",
          any("钩子" in str(r) for r in _reasons), _reasons)
finally:
    app_mod.log_event = _log_bak
    w._physically_down = _pd_bak
    w.recover_hotkeys = _rec_bak
    _kb._pressed_events.clear()
    _kb._pressed_events.update(_pe_bak)
    w.hotkey_poll_state = {}
    w.refresh_hotkeys()                          # 轮询表/钩子恢复成配置里那套
    w.hotkey_poll_timer.start()

print("\n=== 27b. 轮换的流（知乎推荐）第一页不进内存 memo ===")
# 真人 2026-10-23：「知乎的推荐刷来刷去都是那几条」。来源那边已经不读 5 分钟缓存了，
# 界面这边还有一层 90 秒的内存 memo——轮换的流必须绕过它，不然切回来还是那几条。
_w = w.feed_source()
_orig_list = _w.list
_seen_calls = []


def _count_list(channel=None, cursor=None):
    _seen_calls.append(channel)
    return _orig_list(channel, cursor)


_w.list = _count_list
_w.rotates = lambda ch=None: True          # 骗一下：把当前来源当"流"
w._feed_memo.clear()
w.feed_load("list")
pump(lambda: _seen_calls and not w.feed_busy, 10.0)
_first_page_memo = [k for k in w._feed_memo if k[0] == "list" and not k[3]]
check("轮换的流：第一页不进 memo（否则 90 秒内再进来还是那几条）",
      len(_seen_calls) == 1 and _first_page_memo == [], (_seen_calls, _first_page_memo))
w.feed_load("list")                        # 立刻再进来一次
pump(lambda: len(_seen_calls) >= 2 and not w.feed_busy, 10.0)
check("轮换的流：再进来一次是真的又取了一遍（不是拿 memo 顶上）",
      len(_seen_calls) >= 2, len(_seen_calls))
_w.rotates = lambda ch=None: False         # 换回"榜单"那种
w._feed_memo.clear()
w.feed_load("list")
pump(lambda: not w.feed_busy, 10.0)
_plain_memo = [k for k in w._feed_memo if k[0] == "list" and not k[3]]
check("不轮换的来源照旧进 memo（切来切去不用重新取）", bool(_plain_memo), _plain_memo)
_w.list = _orig_list
try:
    del _w.rotates
except Exception:
    _w.rotates = None
print("\n=== 27b. 搜索：f 输入、回车确认（每个站一个样）===")
w.enter_feed_mode()            # 上一节把社区页退出去了；取数回来的处理只在 feed_mode 下走
# 换到知乎（能搜），并把它的 search 换成假的：探针一律不联网
while w.feed_source().key != "zhihu":
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
_src = w.feed_source()
_orig_search = _src.search
_calls = []


def _fake_search(q, cursor=None):
    _calls.append((q, cursor))
    return [feed_mod.Post(source=_src.key, pid="a9001", title="搜到的回答", ts=1,
                          channel="搜索：" + q)], None


_src.search = _fake_search
w.feed_goto_channel("热榜")
pump(lambda: not w.feed_busy)

check("知乎：能搜（can_search 为真）", w.feed_can_search()[0] is True)
_fret = w.on_feed_key(Qt.Key_F)
check("按 f 打开搜索条（浮在正文上的一条，不是模态对话框）",
      _fret is True and not w.feed_search_box.isHidden(),
      (_fret, w.feed_mode, None if w.feed_search_box is None else w.feed_search_box.isHidden()))
w.feed_search_box.setText("  苹果   手机  ")
w.feed_search_go()
pump(lambda: not w.feed_busy)
check("回车确认：按这个词去搜了（空白压成一个、只发一次）",
      _calls == [("苹果 手机", None)], _calls)
check("搜索结果落在「搜索：苹果 手机」这一栏（页脚能看见）",
      w.feed_nav.channel == "搜索：苹果 手机"
      and "搜索：苹果 手机" in w.feed_status_text(), w.feed_status_text())
pump(lambda: bool(w.feed_nav.posts) or not w.feed_busy, 10.0)
check("列表画出来的就是搜到的东西", [p.id for p in w.feed_nav.posts] == ["a9001"],
      ([p.id for p in w.feed_nav.posts], w.feed_status_text()[:60]))
# 真人 2026-11-02："搜索完怎么没别的交互了，我没法cd也没法esc"——hide() 一个拿着焦点的
# 子控件，焦点就悬空，窗口再也收不到 keyPressEvent。焦点必须还给正文。
check("回车之后焦点回到正文（不还回去的话 c/d/Esc 一起失灵）",
      w.focusWidget() is w.text_edit,
      type(w.focusWidget()).__name__ if w.focusWidget() is not None else None)
check("退出搜索：回到原来那一栏（热榜），不是回到第一栏",
      w.feed_search_exit() is True and w.feed_search_text == "" and w.feed_nav.channel == "热榜",
      (w.feed_search_text, w.feed_nav.channel))
pump(lambda: not w.feed_busy)

_prev_key = w.feed_source().key
_ok_c = w.on_feed_key(Qt.Key_C)
pump(lambda: not w.feed_busy)
check("搜完（以及退出搜索后）照样能按 c 切社区，不再'卡住'",
      _ok_c is True and w.feed_source().key != _prev_key,
      (_ok_c, _prev_key, w.feed_source().key))
_guard = 0
while w.feed_source().key != _src.key and _guard < 12:
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
    _guard += 1
w.feed_goto_channel("热榜")
pump(lambda: not w.feed_busy)

# Esc 收起输入条（不开搜索）也要把焦点还回去
w.feed_search_open()
_esc_ok = w.on_feed_key(Qt.Key_Escape)
check("Esc 收起输入条：焦点照样回到正文",
      _esc_ok is True and w.feed_search_box.isHidden() and w.focusWidget() is w.text_edit,
      (_esc_ok, w.feed_search_box.isHidden(),
       type(w.focusWidget()).__name__ if w.focusWidget() is not None else None))

w.feed_search_run("苹果")
pump(lambda: not w.feed_busy)
check("搜索也算一次切换：Z 撤回得回来（词也一起清掉）",
      w.on_feed_key(Qt.Key_Z) is True and w.feed_search_text == ""
      and w.feed_nav.channel == "热榜", (w.feed_search_text, w.feed_nav.channel))
pump(lambda: not w.feed_busy)

w.feed_search_run("苹果")
pump(lambda: not w.feed_busy)
w.feed_cycle_channel(1)
pump(lambda: not w.feed_busy)
check("切板块时把搜索扔掉（否则新栏目还按旧词去搜）",
      w.feed_search_text == "" and w.feed_nav.channel != "搜索：苹果", w.feed_nav.channel)

_ith_i = [i for i, _s in enumerate(w.feed_sources) if _s.key == "ithome"]
if _ith_i:
    w.feed_source_i = _ith_i[0]
    w.feed_nav.reset()
    w.feed_nav.channel = w.feed_first_channel()
    _can, _why = w.feed_can_search()
    check("IT之家：能搜 = False，原因里是实测那句（so.ithome.com / 404）",
          _can is False and ("404" in _why or "so.ithome.com" in _why), _why[:48])
    w.feed_search_box.hide()
    check("不能搜的来源：按 f 只给一句话，不弹一个没用的输入框",
          w.on_feed_key(Qt.Key_F) is True and w.feed_search_box.isHidden())
# 搜索 → 点进一条 → Esc：先退回**搜索结果这一页**，再按一次才退出搜索
# （真人 2026-11-02："esc 退出，退回的应该是搜索结果页，而不是直接退回了社区页"）
_orig_detail = _src.detail
_orig_cpage = getattr(_src, "comments_page", None)
_src.detail = lambda post: (post, "")
_src.comments_page = lambda post, cursor=None: ([], None)
# 上一段把来源换成 IT之家了（不能搜），这里先切回知乎
_guard2 = 0
while w.feed_source().key != _src.key and _guard2 < 12:
    w.feed_cycle_source(1)
    pump(lambda: not w.feed_busy)
    _guard2 += 1
w.feed_goto_channel("热榜")
pump(lambda: not w.feed_busy)
try:
    w.feed_search_run("苹果")
    pump(lambda: bool(w.feed_nav.posts) and not w.feed_busy, 10.0)
    _opened = w.feed_open()
    pump(lambda: w.feed_nav.view == "detail" and not w.feed_busy, 10.0)
    check("点进搜索结果：进了详情", _opened is True and w.feed_nav.view == "detail",
          (_opened, w.feed_nav.view))
    _e1 = w.on_feed_key(Qt.Key_Escape)
    pump(lambda: w.feed_nav.view == "list" and not w.feed_busy, 10.0)
    check("详情里按 Esc：退回的是**搜索结果页**（词还在，不是一退到底）",
          _e1 is True and w.feed_nav.view == "list" and w.feed_search_text == "苹果"
          and w.feed_nav.channel == "搜索：苹果",
          (_e1, w.feed_nav.view, w.feed_nav.channel, w.feed_search_text))
    check("退回之后搜索结果还在（条目没被清空）",
          [p.id for p in w.feed_nav.posts] == ["a9001"], [p.id for p in w.feed_nav.posts])
    _e2 = w.on_feed_key(Qt.Key_Escape)
    pump(lambda: not w.feed_busy)
    check("在搜索结果页上再按一次 Esc：这才退出搜索，回到原来那一栏",
          _e2 is True and w.feed_search_text == "" and w.feed_nav.channel == "热榜",
          (_e2, w.feed_search_text, w.feed_nav.channel)) 
finally:
    _src.detail = _orig_detail
    if _orig_cpage is not None:
        _src.comments_page = _orig_cpage

print("\n=== 27c. 详情滚到底：评论（知乎是回答）自动续页 ===")
# 真人 2026-11-02："一个帖子的评论有很多，为什么到底后没有自动加载，比如知乎"。
# 两个真原因：①详情数据回来时没重新武装"到底续页"的闸，闸一旦在某次被丢掉的结果上
# 关掉就再也不开；②知乎问题页的回答列表当时写死 offset=0，一页就是全部。
_src3 = w.feed_source()
_orig_cp3 = _src3.comments_page
_orig_dt3 = _src3.detail
_calls3 = []


def _page3(i):
    rows = [feed_mod.Comment(cid="p%d-%d" % (i, k), author="甲%d" % k,
                             created="刚刚", score=k,
                             body=[feed_mod.Block(kind=feed_mod.BLOCK_P,
                                                  text="第 %d 页第 %d 条评论" % (i + 1, k + 1))],
                             replies=[])
            for k in range(8)]
    if i == 0:
        rows[0].n_replies = 12        # 这一楼站上有 12 条回复，手里 0 条（子评论要翻页）
    return rows


_kid_calls = []


def _kids3(post, cid, have=0, want=10):
    _kid_calls.append((str(cid), int(have)))
    kids = [feed_mod.Comment(cid="k%d-%d" % (have, k), author="乙%d" % k, created="刚刚",
                             score=0, replies=[],
                             body=[feed_mod.Block(kind=feed_mod.BLOCK_P,
                                                  text="回复 %d" % (have + k))])
            for k in range(4)]
    return kids, 12


def _cp3(post, cursor=None):
    _calls3.append(cursor)
    i = int(cursor or 0)
    nxt = (i + 1) if (i + 1) < 3 else None
    return _page3(i), None, nxt


_src3.comments_page = _cp3
_src3.children_page = _kids3
_src3.detail = lambda post, cursor=None: (post, None)
try:
    w.resize(900, 700)
    w.show()
    if not w.feed_nav.posts:
        w.feed_reload()
        pump(lambda: bool(w.feed_nav.posts) and not w.feed_busy, 10.0)
    # 前面几节留下的详情缓存要清掉（不然进详情走的是缓存那一份，压根不会问评论）
    w._feed_memo.clear()
    try:
        w._feed_inflight.clear()
    except Exception:
        pass
    w.feed_open()
    pump(lambda: w.feed_nav.view == "detail" and not w.feed_busy, 10.0)
    # 正文拉长一点，保证页面能滚（不然滚动条 max=0，测不到"滚到底"这条路）
    w.feed_nav.post.body = [feed_mod.Block(kind=feed_mod.BLOCK_P, text="正文行 " * 600)]
    w.render_feed()
    pump(lambda: False, 0.3)
    check("进详情后'到底续页'的闸是开着的（以前在列表续页被丢掉后它就是关的）",
          w.feed_more_armed is True, w.feed_more_armed)
    check("首屏只有第一页评论，且知道还有下一页",
          len(w.feed_nav.roots) == 8 and bool(w.feed_nav.c_more),
          (len(w.feed_nav.roots), w.feed_nav.c_more))
    _sb3 = w.text_edit.verticalScrollBar()
    if _sb3.maximum() > 0:
        _sb3.setValue(_sb3.maximum())          # 滚到底 = 人用滚轮往下读到底
        pump(lambda: not w.feed_busy, 10.0)
        check("滚到底：自动取回第二页（不用按任何键）",
              len(w.feed_nav.roots) == 16, (len(w.feed_nav.roots), _calls3))
    else:
        check("（这台机器的探针窗口滚不动，滚到底这条跳过，只看下面的按键入口）", True, "")
        w.feed_more_comments_now()
        pump(lambda: not w.feed_busy, 10.0)
    check("再往下（光标已在最后一条）→ 第三页",
          w.feed_more_comments_now() is True and (pump(lambda: not w.feed_busy, 10.0) or True)
          and len(w.feed_nav.roots) == 24, (len(w.feed_nav.roots), _calls3))
    check("顶层评论到头了就停：不再要顶层（请求数停在 3）",
          not w.feed_nav.c_more and len(_calls3) == 3, (w.feed_nav.c_more, _calls3))
    _n3 = len(_calls3)
    check("没有下一页之后一个请求都不多发", len(_calls3) == _n3 == 3, _calls3)
    # 顶层评论到头了，子评论（"还有 N 条回复"那几楼）也得能自动接着取
    _k0 = len(w.feed_nav.roots[0].replies)
    _ok_kid = w.feed_fetch_children_next()
    pump(lambda: not w._feed_children_busy, 10.0)
    check("顶层到头后，到底接着取子评论（以前只有按回车才取）",
          _ok_kid and len(w.feed_nav.roots[0].replies) == _k0 + 4,
          (_ok_kid, _k0, len(w.feed_nav.roots[0].replies), _kid_calls))
    for _ in range(3):
        w.feed_fetch_children_next()
        pump(lambda: not w._feed_children_busy, 10.0)
    check("子评论取全了就停（站上说 12 条，手里 12 条）",
          len(w.feed_nav.roots[0].replies) == 12 and w.feed_fetch_children_next() is False,
          (len(w.feed_nav.roots[0].replies), _kid_calls))
    check("翻子评论问的是 offset=手里条数（不是每次都从 0 开始）",
          _kid_calls[:3] == [("p0-0", 0), ("p0-0", 4), ("p0-0", 8)], _kid_calls)
    # 闸：一次结果因为"换代/换来源"被丢掉，也必须把闸放回去（不然从此再也续不了）
    w.feed_more_armed = False
    w.on_feed_data("list", (list(w.feed_nav.posts), None, False, w.feed_gen - 1, "gone-source"))
    pump(lambda: not w.feed_busy, 6.0)
    check("一次被丢掉的结果不会把'到底续页'永久关掉",
          w.feed_more_armed is True, w.feed_more_armed)
finally:
    _src3.comments_page = _orig_cp3
    _src3.detail = _orig_dt3
    try:
        del _src3.children_page
    except Exception:
        pass
    w.feed_home()
    pump(lambda: not w.feed_busy)

_src.search = _orig_search
w._feed_memo.clear()

print("\n=== 27d. 列表一屏装得下（滚不动）时，键盘到底也接下一页 ===")
# 真人 2026-11-02：小红书一页 9 条，1200x900 根本不用滚——"滚到底"那条路永远触发不了，
# 而 feed_move 停在最后一条时是直接 return 的，于是列表就卡在第一页。
_src4 = w.feed_source()
_orig_ls4 = _src4.list
_ls4_calls = []


def _ls4(channel=None, cursor=None):
    _ls4_calls.append(cursor)
    i = int(cursor or 0)
    rows = [feed_mod.Post(source=_src4.key, pid="lp%d-%d" % (i, k),
                          title="列表第 %d 页第 %d 条" % (i + 1, k + 1),
                          ts=1700000000 - k) for k in range(5)]
    return rows, ((i + 1) if i < 2 else None)


_src4.list = _ls4
try:
    w._feed_memo.clear()
    w.feed_load("list")
    pump(lambda: not w.feed_busy, 10.0)
    _n0 = len(w.feed_nav.posts)
    w.feed_nav.index = _n0 - 1
    w.feed_move(1)                     # 停在最后一条再往下：这时候只剩键盘这条路
    pump(lambda: len(w.feed_nav.posts) > _n0, 10.0)
    check("（短页面）最后一条再往下会去要下一页", len(w.feed_nav.posts) == _n0 + 5,
          (len(w.feed_nav.posts), _ls4_calls[:3]))
    check("（短页面）续页带的是上一页给的游标", _ls4_calls[:2] == [None, 1], _ls4_calls[:3])
    # 到头了就不许再要（不然键盘一直按会一直问）
    w.feed_nav.index = len(w.feed_nav.posts) - 1
    w.feed_move(1)
    w.feed_move(1)
    pump(lambda: not w.feed_busy, 6.0)
    check("（短页面）列表到头就不再问（请求停在 3 次）", len(_ls4_calls) == 3, _ls4_calls)
finally:
    _src4.list = _orig_ls4
    w.feed_home()
    pump(lambda: not w.feed_busy)

w.exit_feed_mode(reason="probe")
try:
    w.hide()
    w.deleteLater()
    qapp.processEvents()
except Exception:
    pass

print("\n=== 27e. EPUB 正文：块里的源码换行不该把脚注标记顶成独立一行 ===")
# 真人 2026-11-02："现在每个注释符号都单独成行"。这本《庄子今注今译》把脚注标记写成
# `<sup>\n<a>①</a>\n</sup>`，XHTML 还是 CRLF——旧代码把源码里每个换行都当断行，
# 全书 7202 个标记各占一行，原文一句话被切成七八段。
_ep_p = ('<html><body><p class="normaltext4">北冥<sup class="suptext">\r\n\r\n'
         '<a id="Y1_1" href="#Z1_1">①</a>\r\n\r\n</sup>\r\n\r\n 有魚，其名為鯤'
         '<sup class="suptext">\r\n<a id="Y1_2" href="#Z1_2">②</a>\r\n</sup>\r\n 。</p>'
         '</body></html>')
_ep_txt, _ep_anchors, _ep_head, _ep_links = app_mod._epub_html_to_text(
    _ep_p.encode("utf-8"))
_ep_lines = _ep_txt.split("\n")
check("脚注标记留在原句里（一句话还是一行）",
      _ep_lines == ["北冥①有魚，其名為鯤②。"], _ep_lines)
check("标记旁边的源码换行不留空格（中文之间不塞空格）",
      "① 有" not in _ep_txt and "② 。" not in _ep_txt, _ep_txt[:40])
# 锚点现在记的是"第几行第几列"，要正落在 ① 上（只记行首的话，段中的标记会跳到段首）
check("标记上的锚点照样记着，而且正指着那个标记（跳注释还找得到）",
      set(_ep_anchors) == {"Y1_1", "Y1_2"}
      and _ep_txt[_ep_anchors["Y1_1"]] == "①"
      and _ep_txt[_ep_anchors["Y1_2"]] == "②", _ep_anchors)
# 注释那一段：标记 + 后面的注释文字是一行，不是"①"单独一行
_ep_note = ('<p class="note1">\r\n<a id="Z1_1" href="#Y1_1">①</a>\r\n 北冥：“冥”，通溟。</p>')
_ep_ntxt, _ep_nanch, _h, _l = app_mod._epub_html_to_text(_ep_note.encode("utf-8"))
check("注释段：①和它的注释文字在同一行",
      _ep_ntxt == "①北冥：“冥”，通溟。", _ep_ntxt)
# 但真的换行（<br/>）和"没有块标签的平文本"还得断行，别把整章并成一行
_ep_br, _a, _h, _l = app_mod._epub_html_to_text(
    '<p>甲<br/>乙</p><p>丙</p>'.encode("utf-8"))
check("<br/> 和 <p> 该断还是断（没把整章并成一行）",
      _ep_br.split("\n") == ["甲", "乙", "丙"], _ep_br.split("\n"))
_ep_plain, _a, _h, _l = app_mod._epub_html_to_text("甲\n乙\n丙".encode("utf-8"))
check("（没有块标签的）平文本仍按行分", _ep_plain.split("\n") == ["甲", "乙", "丙"],
      _ep_plain.split("\n"))
# 英文单词之间的空格不能被中文那条规则吃掉
_ep_en, _a, _h, _l = app_mod._epub_html_to_text('<p>hello\nworld</p>'.encode("utf-8"))
check("英文单词之间的空格留着", _ep_en == "hello world", _ep_en)

print("\n=== 27f. 注释点击互跳：点原文的 ① 跳到注释，点注释的 ① 跳回原文 ===")
# 真人 2026-11-02："我想实现注释点击的相互跳转"。书里的写法是
# `<a id="Y1_1" href="#Z1_1">①</a>`…`<a id="Z1_1" href="text00008.html#Y1_1">①</a>`，
# 两边成对；偏移必须精确到"那个字"，落到隔壁就是点了没反应。
_frag_page = ('<html><body><p class="normaltext4">北冥'
              '<a id="Y1_1" href="#Z1_1">①</a>有魚。</p>'
              '<p class="note1"><a id="Z1_1" href="text00008.html#Y1_1">①</a>'
              ' 北冥：“冥”，通溟。</p></body></html>')
_frg_txt, _frg_anch, _frg_head, _frg_links = app_mod._epub_html_to_text(
    _frag_page.encode("utf-8"))
_frg_map = app_mod._epub_links_map(
    _frg_txt, [("t.html", _frg_txt, _frg_anch, _frg_head, _frg_links)], {"t.html": 0})
_frg_i = _frg_txt.index("①")
_frg_j = _frg_txt.index("①", _frg_i + 1)
check("解析层：原文①指向注释①（带文件名的写法也认）",
      _frg_map.get(_frg_i) == _frg_j and _frg_map.get(_frg_j) == _frg_i,
      (_frg_i, _frg_j, _frg_map.get(_frg_i), _frg_map.get(_frg_j)))
check("解析层：偏移落在标记那个字上（不是隔壁的字）",
      _frg_txt[_frg_i] == "①" and _frg_txt[_frg_i + 1] == "有"
      and _frg_txt[_frg_j] == "①" and _frg_txt[_frg_j + 1] == "北", _frg_txt)
_frag2 = ('<p>甲<a id="A1" href="#B1">②</a>乙</p><p><a id="B1">②</a>注文</p>')
_2txt, _2anch, _2head, _2links = app_mod._epub_html_to_text(_frag2.encode("utf-8"))
_2map = app_mod._epub_links_map(_2txt, [("a.html", _2txt, _2anch, _2head, _2links)],
                                {"a.html": 0})
_2i = _2txt.index("②")
_2j = _2txt.index("②", _2i + 1)
check("只有去的链接时补一条回来（不然点进注释就出不来）",
      _2map.get(_2i) == _2j and _2map.get(_2j) == _2i, _2map)

try:
    from PyQt5.QtTest import QTest
    _qtest_ok = True
except Exception:
    _qtest_ok = False
_book_bak = (w.is_local_mode, w.local_full_text, w.local_start_index,
             w.local_chapters, w.local_heading_offsets,
             getattr(w, "book_links", None))


def _click_doc_pos(doc_pos):
    """在正文里"点"文档第 doc_pos 个字（合成鼠标事件，真走 release 那条路）。"""
    _c = app_mod.QTextCursor(w.text_edit.document())
    _c.setPosition(doc_pos)
    _r = w.text_edit.cursorRect(_c)
    _pt = QPoint(_r.center().x(), _r.center().y())
    if _qtest_ok:
        QTest.mouseClick(w.text_edit.viewport(), Qt.LeftButton, Qt.NoModifier, _pt)
    else:
        w.follow_reading_link(_pt)
    pump(lambda: False, 0.2)


try:
    _save_lc = w.local_heading_offsets
    w.is_local_mode = True
    w.local_full_text = "甲①乙\n\n①注文"
    w.local_chapters = []
    w.local_heading_offsets = set()
    w.book_links = {1: 5, 5: 1}
    w.local_start_index = 0
    w.render_local_page()
    pump(lambda: False, 0.25)
    check("（界面）这一小段铺出来了", w.text_edit.toPlainText().startswith("甲①乙"),
          w.text_edit.toPlainText()[:12])
    _click_doc_pos(1)
    check("点原文的①：跳到注释那一条", w.local_start_index == 5,
          (w.local_start_index, w.text_edit.toPlainText()[:12]))
    _click_doc_pos(0)
    check("点注释的①：跳回原文", w.local_start_index == 1,
          (w.local_start_index, w.text_edit.toPlainText()[:12]))
    _keep = w.local_start_index
    _click_doc_pos(len(w.text_edit.toPlainText()) - 1)
    check("点普通正文不跳", w.local_start_index == _keep, w.local_start_index)
    # 悬停要变手型（能点的地方得看得出来）
    _c2 = app_mod.QTextCursor(w.text_edit.document())
    _c2.setPosition(1)
    _r2 = w.text_edit.cursorRect(_c2)
    try:
        from PyQt5.QtCore import QPointF
        from PyQt5.QtGui import QMouseEvent
        _me = QMouseEvent(app_mod.QEvent.MouseMove, QPointF(_r2.center()), Qt.NoButton,
                          Qt.NoButton, Qt.NoModifier)
        w.update_viewport_cursor(_me)
        _shape = w.text_edit.viewport().cursor().shape()
        check("停在标记上光标变手型", _shape == Qt.PointingHandCursor, _shape)
    except Exception as e:
        check("停在标记上光标变手型（这条环境不支持，跳过）", True, str(e)[:40])
finally:
    (w.is_local_mode, w.local_full_text, w.local_start_index,
     w.local_chapters, w.local_heading_offsets, _links_bak) = _book_bak
    w.book_links = _links_bak or {}
    w.render_local_page()
    pump(lambda: False, 0.2)

print("\n=== 28. 单实例：老进程还在拆钩子时要重试，不能一撞就弹")
# 2026-10-22 真撞上一次：新进程比老进程先起来，第一次抢互斥体拿到 183，旧代码直接弹
# 阻塞提示框（pythonw 下连日志都没有），看起来就是"应用没打开"。现在给它六秒。
_seen = []


def _fake_create_late():
    _seen.append(1)
    # 句柄故意给一个不可能有效的值，CloseHandle 失败也无所谓
    return (1000000 + len(_seen), 183 if len(_seen) < 3 else 0)


_h, _ok = app_mod.acquire_single_instance(create=_fake_create_late, attempts=5, delay=0,
                                          sleep=lambda s: None)
check("前两次撞上 183 会重试，第三次抢到就算成功", _ok is True and len(_seen) == 3,
      (_ok, len(_seen)))
_h2, _ok2 = app_mod.acquire_single_instance(create=lambda: (1000001, 183), attempts=3, delay=0,
                                            sleep=lambda s: None)
check("一直被别人拿着就如实失败（交给提示框 + 日志）", _ok2 is False and _h2 is None, _ok2)
_h3, _ok3 = app_mod.acquire_single_instance(create=lambda: (1000002, 0), attempts=3, delay=0,
                                            sleep=lambda s: None)
check("一次就抢到时不睡也不重试", _ok3 is True and _h3 == 1000002, _h3)

# ===== 29b. 自带字体：fonts/ 里的字私有加载（MiSans 这类不装进系统）=====
print("\n=== 29b. 自带字体私有加载 ===")
import io
import shutil
_fdir = _tf.mkdtemp(prefix="srfonts")
_ftmp = os.path.join(_fdir, "arial_copy.ttf")
_src_ttf = r"C:\Windows\Fonts\arial.ttf"
shutil.copyfile(_src_ttf, _ftmp) if os.path.exists(_src_ttf) else None
io.open(os.path.join(_fdir, "坏的字体.ttf"), "wb").write(b"not a font at all")
io.open(os.path.join(_fdir, "说明.txt"), "w", encoding="utf-8").write("ignore me")

_loaded = app_mod.load_bundled_fonts(_fdir)
check("fonts/ 里的 ttf 能被私有加载（坏文件和非字体文件跳过，不抛）",
      "Arial" in _loaded, _loaded)
check("没有 fonts/ 目录时就返回空表（不影响启动）",
      app_mod.load_bundled_fonts(os.path.join(_fdir, "不存在")) == [], None)
check("字体栈里 MiSans 排在最前（装了就是它，没装才往下落）",
      feed_mod.FEED_TYPE_SCHEMES["lingxi"]["family"].lstrip().startswith('"MiSans"'),
      feed_mod.FEED_TYPE_SCHEMES["lingxi"]["family"][:40])

# ---- 阅读页（走 QFont 的那几处）也要过解析：没装的族名不许静默变宋体 ----
check("阅读页字体回退链第一顺位是 MiSans（和灵犀那套一致）",
      app_mod.BOOK_FONT_FALLBACKS[0] == "MiSans", app_mod.BOOK_FONT_FALLBACKS[:3])

_logged = []
_old_log = app_mod.log_event
_old_fams = app_mod.installed_families
app_mod.log_event = lambda msg, *a, **k: _logged.append(str(msg))
app_mod.installed_families = lambda: {"MiSans", "微软雅黑"}
check("装了 MiSans 就用 MiSans（别名解析不误伤真字体）",
      app_mod.resolve_font_family("MiSans") == "MiSans", None)
app_mod.installed_families = lambda: {"微软雅黑"}
check("没装 MiSans：落到微软雅黑，而不是让 Qt 悄悄替成宋体",
      app_mod.resolve_font_family("MiSans") == "微软雅黑", None)
check("替换要留一条日志（人得知道发生了什么）",
      any("MiSans" in m and "微软雅黑" in m for m in _logged), _logged[:2])
app_mod.installed_families = lambda: set()
check("字体库量不到（如 offscreen）就原样返回，别把人配置里写的字改掉",
      app_mod.resolve_font_family("MiSans") == "MiSans", None)
app_mod.installed_families = lambda: {"Microsoft YaHei UI", "微软雅黑"}
check("大小写/别名不同也算同一个字（Microsoft YaHei UI 不会被误判成没装）",
      app_mod.resolve_font_family("microsoft yahei ui") == "Microsoft YaHei UI", None)
app_mod.installed_families = _old_fams
app_mod.log_event = _old_log


# ===== 29. 社区页两套排版方案：灵犀 / DSH Desktop（真人 2026-11-05 的对照表）=====
print("\n=== 29. 社区页排版方案：灵犀 / DSH Desktop ===")
import feed as feed_mod

_BASE_CFG = {"text_color": "rgba(200, 200, 200, 255)"}


def _theme_for(scheme, **extra):
    cfg = dict(_BASE_CFG)
    cfg["feed_type_scheme"] = scheme
    cfg.update(extra)
    return feed_mod.theme_from_config(cfg)


_t_lx = _theme_for("lingxi")
_s_lx = feed_mod._scale(_t_lx)
check("灵犀：正文 16px / 行高 162.5% / 段间距 8px / 标题 24·21·18 / 代码 13px",
      (_t_lx["size"], _s_lx["line"], _s_lx["gap"], _s_lx["h1"], _s_lx["h2"], _s_lx["h3"],
       _s_lx["code"]) == (16, 162.5, 8, 24, 21, 18, 13),
      (_t_lx["size"], _s_lx["line"], _s_lx["gap"], _s_lx["h1"], _s_lx["h2"], _s_lx["h3"],
       _s_lx["code"]))

_t_ds = _theme_for("dsh")
_s_ds = feed_mod._scale(_t_ds)
check("DSH：正文 14px / 行高 171.4% / 段间距 16px / 标题 21·19·18 / 代码 11px",
      (_t_ds["size"], _s_ds["line"], _s_ds["gap"], _s_ds["h1"], _s_ds["h2"], _s_ds["h3"],
       _s_ds["code"]) == (14, 171.4, 16, 21, 19, 18, 11),
      (_t_ds["size"], _s_ds["line"], _s_ds["gap"], _s_ds["h1"], _s_ds["h2"], _s_ds["h3"],
       _s_ds["code"]))

_t_cx = _theme_for("classic")
_s_cx = feed_mod._scale(_t_cx)
check("经典：还是 15px / 175% / 段间距 9 / 标题只大 1px（老样子没被改坏）",
      (_t_cx["size"], _s_cx["line"], _s_cx["gap"], _s_cx["h1"], _s_cx["code"]) ==
      (15, 175.0, 9, 16, 14),
      (_t_cx["size"], _s_cx["line"], _s_cx["gap"], _s_cx["h1"], _s_cx["code"]))

check("灵犀的字体是 MiSans 优先 → 雅黑",
      "MiSans" in _t_lx["family"] and "YaHei" in _t_lx["family"], _t_lx["family"])
check("DSH 用纯系统字体栈（不写 MiSans）",
      "MiSans" not in _t_ds["family"] and "YaHei" in _t_ds["family"], _t_ds["family"])
check("字体单独配过就听配的（不被方案覆盖）",
      "Courier" in feed_mod.theme_from_config(
          dict(_BASE_CFG, feed_type_scheme="lingxi",
               feed_font_family="Courier New"))["family"],
      feed_mod.theme_from_config(dict(_BASE_CFG, feed_type_scheme="lingxi",
                                      feed_font_family="Courier New"))["family"])

_s_20 = feed_mod._scale(_theme_for("lingxi", feed_font_size=20))
check("字号单独调过就整体联动：20px 时标题 30 / 代码 16 / 段间距 10",
      (_s_20["h1"], _s_20["code"], _s_20["gap"]) == (30, 16, 10),
      (_s_20["h1"], _s_20["code"], _s_20["gap"]))

check("认不出来的方案名回到默认（不抛）",
      feed_mod.feed_scheme_key({"feed_type_scheme": "??? "}) == feed_mod.FEED_SCHEME_DEFAULT,
      feed_mod.feed_scheme_key({"feed_type_scheme": "??? "}))
check("设置页能选到三套方案", len(feed_mod.feed_scheme_names()) == 3,
      feed_mod.feed_scheme_names())


class _B(object):
    """只为渲染检查造的最小正文块。"""
    def __init__(self, kind, text="正文一段", url="", lang=""):
        self.kind, self.text, self.url, self.lang = kind, text, url, lang


_html_lx = "".join(feed_mod._block_paragraphs(
    [_B(feed_mod.BLOCK_P), _B(feed_mod.BLOCK_H, "小标题"), _B(feed_mod.BLOCK_CODE, "x = 1")],
    _t_lx))
check("灵犀渲染出来的 HTML：正文行高 162.5%、段间距 8px、标题 21px、代码 13px",
      "line-height:162.5%" in _html_lx and "margin:8px 0 0 0" in _html_lx
      and "font-size:21px" in _html_lx and "font-size:13px" in _html_lx,
      _html_lx[:160])

_html_ds = "".join(feed_mod._block_paragraphs(
    [_B(feed_mod.BLOCK_P), _B(feed_mod.BLOCK_H, "小标题"), _B(feed_mod.BLOCK_CODE, "x = 1")],
    _t_ds))
check("DSH 渲染出来的 HTML：行高 171.4%、段间距 16px、标题 19px、代码 11px",
      "line-height:171.4%" in _html_ds and "margin:16px 0 0 0" in _html_ds
      and "font-size:19px" in _html_ds and "font-size:11px" in _html_ds,
      _html_ds[:160])

# 设置页那个下拉框：切一下要当场落进 config
_cfg_scheme_bak = w.config.get("feed_type_scheme")
_dlg29 = app_mod.SettingsDialog(w.config, w)
_combos = getattr(_dlg29, "feed_scheme_combo", None)
check("设置页里有「排版方案」下拉框", _combos is not None and _combos.count() == 3,
      None if _combos is None else [_combos.itemData(i) for i in range(_combos.count())])
if _combos is not None and _combos.count() == 3:
    for _i in range(_combos.count()):
        if _combos.itemData(_i) == "dsh":
            _combos.setCurrentIndex(_i)
    check("切到 DSH 当场写进 config", w.config.get("feed_type_scheme") == "dsh",
          w.config.get("feed_type_scheme"))
    for _i in range(_combos.count()):
        if _combos.itemData(_i) == (_cfg_scheme_bak or "lingxi"):
            _combos.setCurrentIndex(_i)
    w.config["feed_type_scheme"] = _cfg_scheme_bak
# QFontComboBox + 动态字体 = 退出时 0xC0000005（量过），所以下拉框一律用 FontFamilyCombo
check("字体下拉框不是 QFontComboBox（它跟自带字体一起会让进程退出时崩）",
      type(_dlg29.feed_font_combo).__name__ != "QFontComboBox"
      and isinstance(_dlg29.feed_font_combo, app_mod.FontFamilyCombo),
      type(_dlg29.feed_font_combo).__name__)
_check_combo = app_mod.FontFamilyCombo()
_check_combo.setFamily("Microsoft YaHei")
check("没有的族名（别名）也如实选中，不会静默掉到第一项",
      _check_combo.family().lower() in ("microsoft yahei", "微软雅黑"),
      _check_combo.family())
_check_combo.deleteLater()
_dlg29.deleteLater()
pump(lambda: False, 0.1)

# ===== AE 轮：两本书来回切，各自的阅读进度不能互相吃掉 =====
import io
import tempfile as _tf

_bk_dir = _tf.mkdtemp(prefix="srbooks")
_bk_a = os.path.join(_bk_dir, "甲书.txt")
_bk_b = os.path.join(_bk_dir, "乙书.txt")
for _p, _tag in ((_bk_a, "甲"), (_bk_b, "乙")):
    with io.open(_p, "w", encoding="utf-8") as _f:
        _f.write("\n".join(["%s书第%d段：这是用来验证换书之后回来还在原地的一段文字。" % (_tag, i)
                            for i in range(400)]))

_cfg_bak = dict(w.config)
_save_bak = w.save_config
w.save_config = lambda *a, **k: w._stash_book_pos()   # 只按书记账，不落盘


def _read_to(pos):
    w.local_start_index = int(pos)
    w.config["last_local_pos"] = int(pos)
    w.save_config()


w.open_local_path(_bk_a)
_read_to(5000)
print("\n[AE] 甲书读到 %d" % w.local_start_index)
w.open_local_path(_bk_b)
_b0 = w.local_start_index
_read_to(900)
print("[AE] 换乙书：从 %d 开始，读 900" % _b0)
w.open_local_path(_bk_a)
_back_a = w.local_start_index
print("[AE] 切回甲书：落在 %d（期望 5000）" % _back_a)
w.open_local_path(_bk_b)
_back_b = w.local_start_index
print("[AE] 再切回乙书：落在 %d（期望 900）" % _back_b)

check("换书之后回到甲书，还在读到的地方", _back_a == 5000, _back_a)
check("换书之后回到乙书，还在读到的地方", _back_b == 900, _back_b)

# 表里两本各记各的（键是规范化绝对路径）
_pos_map = dict(w.config.get("local_book_positions") or {})
check("两本书的位置各自记在按书表里",
      _pos_map.get(w.book_pos_key(_bk_a)) == 5000 and _pos_map.get(w.book_pos_key(_bk_b)) == 900,
      _pos_map)

# 从没打开过的第三本：0，而且不能把前两本的记性冲掉
_bk_c = os.path.join(_bk_dir, "丙书.txt")
with io.open(_bk_c, "w", encoding="utf-8") as _f:
    _f.write("\n".join(["丙书第%d段：没读过的一本。" % i for i in range(50)]))
w.open_local_path(_bk_c)
_c_start = w.local_start_index
check("没读过的书打开是 0，且不影响别的书",
      _c_start == 0
      and int((w.config.get("local_book_positions") or {}).get(w.book_pos_key(_bk_b), -1)) == 900,
      (_c_start, dict(w.config.get("local_book_positions") or {})))

# 表不能无限长：塞到 80 本，落盘前裁到 60
_keep = w.config.setdefault("local_book_positions", {})
for _i in range(80):
    _keep["C:\\假想\\书%02d.txt" % _i] = _i
w._stash_book_pos()
check("按书记忆的表会裁剪（不无限长）",
      len(w.config.get("local_book_positions") or {}) <= 60,
      len(w.config.get("local_book_positions") or {}))

# 刚升级上来：按书表还是空的，只有老的 last_local_file/last_local_pos 那一个坑位
_lp_tab = w.config.get("local_book_positions")
w.config["local_book_positions"] = {}
w.config["last_local_file"] = _bk_a
w.config["last_local_pos"] = 4321
w.open_local_path(_bk_a)
check("升级上来（表还是空的）也接得上老坑位，不从头上开始",
      w.local_start_index == 4321, w.local_start_index)
w.config["local_book_positions"] = _lp_tab

print("\n=== 30. 融景实色档（背景用采到的颜色、不透明）+ 偷瞄前台换进程就退出 ===")

from PyQt5.QtGui import QColor                                        # noqa: E402

_auto_bak = w.config.get("auto_mode")
_solid_bak = w.config.get("auto_bg_solid")
try:
    # ---- 实色档：同一个颜色，两种 alpha ----
    w.show()
    w.config["auto_mode"] = True
    w.config["auto_bg_solid"] = False
    w.apply_style()
    w.content_frame.set_auto_bg_color(QColor(12, 34, 56))
    _veil = (w.content_frame.auto_bg_fill.red(), w.content_frame.auto_bg_fill.green(),
             w.content_frame.auto_bg_fill.blue(), w.content_frame.auto_bg_fill.alpha())
    w.config["auto_bg_solid"] = True
    w.apply_style()
    w.content_frame.set_auto_bg_color(QColor(12, 34, 56))
    _solid = (w.content_frame.auto_bg_fill.red(), w.content_frame.auto_bg_fill.green(),
              w.content_frame.auto_bg_fill.blue(), w.content_frame.auto_bg_fill.alpha())
    check("融景老档几乎全透（alpha 2）、实色档不透明（alpha 255）",
          _veil[3] == 2 and _solid[3] == 255, (_veil, _solid))
    check("实色档刷出来的就是采到的那个颜色（RGB 原样）",
          _solid[:3] == (12, 34, 56), _solid)

    w.config["auto_bg_solid"] = False
    w.apply_style()
    check("取消实色档又回到几乎全透",
          w.content_frame.auto_bg_fill.alpha() == 2,
          w.content_frame.auto_bg_fill.alpha())

    # 设置页里那个勾选框在位，关掉自动挡时不给勾
    _set4 = app_mod.SettingsDialog(w.config, w)
    qapp.processEvents()
    _has_chk = hasattr(_set4, "check_solid_bg")
    _chk_on = _has_chk and _set4.check_solid_bg.isChecked() is False
    _set4.check_auto_mode.setChecked(False)          # 关掉自动挡 → 实色档跟着灰掉
    qapp.processEvents()
    _grey = _has_chk and not _set4.check_solid_bg.isEnabled()
    check("设置页有「背景用实色」，且它跟着自动挡一起灰/亮",
          _has_chk and _chk_on and _grey, (_has_chk, _chk_on, _grey))
    _set4.reject()

    # ---- 偷瞄：前台进程一变就退出 ----
    _fg_bak = app_mod.foreground_pid
    try:
        w.show()
        _logged = []
        _log_bak2 = app_mod.log_event
        app_mod.log_event = lambda msg, *a, **k: _logged.append(str(msg))
        try:
            app_mod.foreground_pid = lambda: 4321
            w.peek_mode = True
            w.peek_owner_pid = 4321
            w._watch_peek_focus()
            _stays = w.peek_mode

            app_mod.foreground_pid = lambda: 5555     # 换了个进程（不是我们）
            w._watch_peek_focus()
            _flicker = w.peek_mode                    # 只晃一拍：还不退
            w._watch_peek_focus()                     # 同一个陌生进程连着两拍：退
            _left = (not w.peek_mode) and (not w.isVisible())
            _said = any("前台换了进程" in s for s in _logged)

            app_mod.foreground_pid = lambda: 0        # 拿不到前台：不乱退
            w.peek_mode = True
            w.peek_owner_pid = 4321
            w._watch_peek_focus()
            _keep0 = w.peek_mode

            app_mod.foreground_pid = lambda: os.getpid()   # 前台是我们自己
            w._watch_peek_focus()
            _keep_self = w.peek_mode
        finally:
            app_mod.log_event = _log_bak2
    finally:
        app_mod.foreground_pid = _fg_bak

    check("偷瞄时前台没变：不动", _stays, _stays)
    check("只晃一拍不退（Windows 上焦点会闪一下）", _flicker, _flicker)
    check("偷瞄时前台换了进程（连着两拍）：退出并藏起来（日志写明是哪个 pid）",
          _left and _said, (_left, _said))
    check("拿不到前台（0）不乱退", _keep0, _keep0)
    check("前台是我们自己（钩子/托盘把窗口叫出来）不乱退", _keep_self, _keep_self)

    check("退出偷瞄时那块盯前台的定时器也停掉了",
          not w.peek_watch_timer.isActive(), w.peek_watch_timer.isActive())
finally:
    w.peek_mode = False
    w.peek_owner_pid = 0
    try:
        w.peek_watch_timer.stop()
    except Exception:
        pass
    w.config["auto_mode"] = _auto_bak
    w.config["auto_bg_solid"] = _solid_bak
    try:
        w.apply_style()
    except Exception:
        pass
    w.show()

print("\n=== 31. 偷瞄：点到任务栏当场退出（不用等前台进程那 0.3 秒）===")

import ctypes                                                          # noqa: E402
_tb_u = ctypes.windll.user32
_tb_u.FindWindowW.restype = ctypes.c_void_p
_tb_u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_tb_hwnd = _tb_u.FindWindowW("Shell_TrayWnd", None)
if _tb_hwnd:
    _tb_rect = app_mod.window_rect(_tb_hwnd)
    _tb_x = (_tb_rect[0] + _tb_rect[2]) // 2
    _tb_y = (_tb_rect[1] + _tb_rect[3]) // 2
    print("任务栏矩形 =", _tb_rect)
else:
    _tb_x = _tb_y = 0
    print("这台机器上没找到 Shell_TrayWnd，后面的判定只能跳过")

_tb_testable = bool(_tb_hwnd) and app_mod.point_on_taskbar(_tb_x, _tb_y)
if _tb_hwnd and not _tb_testable:
    # 锁屏界面 / 全屏视频 / 全屏远程桌面都会把整块屏幕盖住，WindowFromPoint 永远拿到那个窗口，
    # 于是"任务栏正中"这一下必然打不中——这是环境验不了，不是代码坏了。
    print("任务栏正中打中的不是任务栏：屏幕上盖了别的东西（锁屏/全屏窗口），这一节跳过")

if _tb_hwnd and _tb_testable:
    check("任务栏正中判成任务栏", True, _tb_hwnd)

_wr = app_mod._RECT()
_tb_u.SystemParametersInfoW(0x0030, 0, ctypes.byref(_wr), 0)     # SPI_GETWORKAREA
_wx, _wy = (_wr.left + _wr.right) // 2, (_wr.top + _wr.bottom) // 2
check("工作区正中不算任务栏", not app_mod.point_on_taskbar(_wx, _wy), (_wx, _wy))
check("屏幕外的点不算任务栏", not app_mod.point_on_taskbar(-5000, -5000), None)

_reasons = []
_conn = w.peek_exit_signal.connect(lambda r: _reasons.append(str(r)))


def _mouse(msg, x, y):
    info = app_mod._MSLLHOOKSTRUCT()
    info.pt.x, info.pt.y = int(x), int(y)
    return w.peek_input_hook._on_mouse(0, msg, ctypes.addressof(info))


try:
    if _tb_testable:
        w.peek_mode = True
        _ret = _mouse(app_mod.WM_LBUTTONDOWN, _tb_x, _tb_y)
        _said = _reasons[-1] if _reasons else ""
        check("左键点在任务栏：钩子报「点到了任务栏」、并且点击照常放行（返回 0）",
              _said == "点到了任务栏" and _ret == 0, (_said, _ret))

        qapp.processEvents()
        check("这一下走完真退出：偷瞄状态清掉、窗口收起",
              (not w.peek_mode) and (not w.isVisible()), (w.peek_mode, w.isVisible()))
    else:
        print("  （跳过：上面两条要真桌面上有任务栏才验得了）")

    _reasons.clear()
    w.peek_mode = True
    _before = len(_reasons)
    _ret2 = _mouse(app_mod.WM_LBUTTONDOWN, _wx, _wy)
    qapp.processEvents()
    check("左键点在窗口/别处：既不退出也不报「任务栏」",
          len(_reasons) == _before and w.peek_mode, (_reasons, w.peek_mode))
finally:
    w.peek_exit_signal.disconnect(_conn)
    w.peek_mode = False
    w.show()

check("盯前台那张表的节奏调快了（150ms 一拍，两拍约 0.3 秒）",
      w.peek_watch_timer.interval() == 150, w.peek_watch_timer.interval())

print("\n=== 32. 幽灵态：实色档鼠标离开后要整窗淡出（不能剩一块实色空白）===")

_g_cache = (w.config.get("auto_mode"), w.config.get("auto_bg_solid"),
            w.config.get("ghost_mode"))
try:
    w.show()
    w.config["auto_mode"] = True
    w.config["auto_bg_solid"] = True
    w.config["ghost_mode"] = True
    w.apply_style()
    _op_before = w.windowOpacity()
    w._ghost_out()
    _op_solid = w.windowOpacity()
    _veiled = bool(getattr(w, "_ghost_veil_on", False))
    _timer_off = not w.chameleon_timer.isActive()
    check("实色档 + 鼠标离开：整扇窗淡到几乎看不见（不是只剩一块实色底）",
          _op_before > 0.9 and _op_solid <= 0.01, (_op_before, _op_solid))
    check("字也一起藏了、盯着取色的表停了", _veiled and _timer_off, (_veiled, _timer_off))

    w.apply_style()
    check("回来立刻恢复：不透明 + 罩子摘掉",
          w.windowOpacity() > 0.9 and not getattr(w, "_ghost_veil_on", False),
          (w.windowOpacity(), getattr(w, "_ghost_veil_on", False)))

    w.config["auto_bg_solid"] = False
    w.apply_style()
    w._ghost_out()
    _op_veil = w.windowOpacity()
    check("老档（几乎全透）鼠标离开：窗口不淡（只藏字，底色本来就是透的）",
          _op_veil > 0.9, _op_veil)

    w.config["auto_mode"] = False
    w.apply_style()
    w._ghost_out()
    _op_plain = w.windowOpacity()
    check("普通档（非融景）鼠标离开：整窗淡到几乎看不见", _op_plain <= 0.01, _op_plain)
finally:
    (w.config["auto_mode"], w.config["auto_bg_solid"],
     w.config["ghost_mode"]) = _g_cache
    w.apply_style()

print("\n=== 33. 融景实色档：字色别在临界线上来回翻、颜色没变别重刷样式 ===")

_c33_bg = (w.config.get("auto_mode"), w.config.get("opacity"))
_smpl_bak = w._sample_desktop_color
_style_bak = w.text_edit.setStyleSheet
_calls = [0]


def _count_style(css):
    _calls[0] += 1
    return _style_bak(css)


try:
    w.config["auto_mode"] = True
    w.config["opacity"] = 1.0
    w.show()
    qapp.processEvents()

    w._auto_text_dark = None
    _c1 = w._auto_text_color(QColor(130, 130, 130))
    w._auto_text_dark = True
    _c2 = w._auto_text_color(QColor(120, 120, 120))
    _c3 = w._auto_text_color(QColor(110, 110, 110))
    _d = w._auto_text_color(QColor(130, 130, 130))
    _e = w._auto_text_color(QColor(150, 150, 150))
    check("字色迟滞：黑字要暗到 116 以下才改白，白字要亮到 140 以上才改黑",
          _c1 == (0, 0, 0) and _c2 == (0, 0, 0) and _c3 == (255, 255, 255)
          and _d == (255, 255, 255) and _e == (0, 0, 0),
          (_c1, _c2, _c3, _d, _e))

    w._auto_text_dark = None
    w._auto_bg_key = None
    w._auto_text_sig = None
    w._sample_desktop_color = lambda: QColor(200, 200, 200)
    w.text_edit.setStyleSheet = _count_style
    w.adjust_color_to_background()
    _first = _calls[0]
    w.adjust_color_to_background()
    w.adjust_color_to_background()
    _same = _calls[0]
    check("底色一直没变：第一拍刷一次样式（%d 次），之后两拍一次都不刷（%d 次）"
          % (_first, _same), _first == 1 and _same == 1, (_first, _same))

    w._sample_desktop_color = lambda: QColor(40, 40, 40)
    w.adjust_color_to_background()
    check("底色翻到深色：字色换成白、样式重刷一次", _calls[0] == 2, _calls[0])

    _before_apply = _calls[0]
    w.apply_style()
    check("整页换样式之后又刷了一次（缓存作废，不会漏掉新底色）",
          _calls[0] == _before_apply + 1, (_before_apply, _calls[0]))
finally:
    w.text_edit.setStyleSheet = _style_bak
    w._sample_desktop_color = _smpl_bak
    w.config["auto_mode"], w.config["opacity"] = _c33_bg
    w._auto_bg_key = None
    w._auto_text_sig = None
    w.apply_style()

print("\n=== 34. 实色开关热键（默认 ctrl+alt+b）===")

_s34 = (w.config.get("auto_mode"), w.config.get("auto_bg_solid"))
check("默认配置里有实色键 ctrl+alt+b",
      app_mod.DEFAULT_CONFIG.get("solid_key") == "ctrl+alt+b",
      app_mod.DEFAULT_CONFIG.get("solid_key"))

try:
    w.config["auto_mode"] = True
    w.config["auto_bg_solid"] = False
    w.apply_style()
    _a0 = w.content_frame._bg_alpha()
    w.toggle_solid_bg()
    _on = (bool(w.config.get("auto_bg_solid")), w.content_frame._bg_alpha(),
           bool(w.content_frame.solid_bg))
    w.toggle_solid_bg()
    _off = (bool(w.config.get("auto_bg_solid")), w.content_frame._bg_alpha(),
            bool(w.content_frame.solid_bg))
    check("按一下开（alpha 255）、再按一下关（alpha 2），当场生效",
          _a0 == 2 and _on == (True, 255, True) and _off == (False, 2, False),
          (_a0, _on, _off))

    w.config["auto_mode"] = False
    w.toggle_solid_bg()
    check("不在融景下按也翻配置（切过去按新值走，不会两套状态）",
          bool(w.config.get("auto_bg_solid")) is True, w.config.get("auto_bg_solid"))

    _dlg = app_mod.SettingsDialog(w.config, w)
    _has = (hasattr(_dlg, "solid_key_input")
            and _dlg.solid_key_input.text() == (w.config.get("solid_key") or "ctrl+alt+b"))
    _dlg.deleteLater()
    check("设置 → 快捷键里有「实色」这一行，值就是配置里那个", _has, None)
finally:
    w.config["auto_mode"], w.config["auto_bg_solid"] = _s34
    w.apply_style()

print("\n=== 35. 滚轮：窗口内和偷瞄同一个步长，且同一格只算一次 ===")

_s35 = w.config.get("arrow_scroll_lines")
try:
    w.config["arrow_scroll_lines"] = 1
    w.config["auto_mode"] = False
    w.apply_style()
    _step = w._lines_to_px(1)
    _unit = max(1, int(round(w._line_unit_px())))

    _seen = []
    _real_scroll = w.scroll_px

    def _rec(px, flip=True):
        _seen.append(px)

    w.scroll_px = _rec
    try:
        def _one(delta=0, pixel=0, reset=True):
            if reset:
                w._wheel_last_t = 0.0
            w._local_wheel_step(delta, pixel)

        _seen.clear()
        _one(120, 0)
        _angle_px = _seen[-1] if _seen else None
        _one(-120, 0)
        _angle_back = _seen[-1] if _seen else None

        _seen.clear()
        w._wheel_px_acc = 0
        w._wheel_pixel_path_noted = False
        _one(0, 90)
        _pix_px = _seen[-1] if _seen else None
        _acc_after = getattr(w, "_wheel_px_acc", None)
        _one(0, -90)
        _pix_back = _seen[-1] if _seen else None

        check("角度路一格 = 设置里那几行（1 行 = %dpx）" % _step,
              _angle_px == -_step and _angle_back == _step, (_angle_px, _angle_back))

        check("像素路一格也只走同样这么多（以前按 90px 原样滚）",
              _pix_px == -_step and _pix_back == _step, (_pix_px, _pix_back))

        check("像素路的余数封顶不累积成雪球",
              -_unit <= int(_acc_after or 0) <= _unit, _acc_after)

        check("第一次走像素路会在日志里点明（好知道用的是哪条路）",
              getattr(w, "_wheel_pixel_path_noted", False) is True, None)

        # 同一格被拆成两拍（Qt 把 240 拆成两个 120 时就是这样）：只许滚一次
        _seen.clear()
        w._wheel_last_t = 0.0
        w._local_wheel_step(120, 0)
        w._local_wheel_step(120, 0)
        check("同一格被拆成两拍只滚一次（真人「cq 一格两行」的那条）",
              len(_seen) == 1, _seen)

        # 真的两格（间隔够）还是要走两次
        _seen.clear()
        w._wheel_last_t = 0.0
        w._local_wheel_step(120, 0)
        w._wheel_last_t = 0.0
        w._local_wheel_step(120, 0)
        check("两格各滚一次（去重不能把正常连滚吞掉）",
              len(_seen) == 2, _seen)
    finally:
        w.scroll_px = _real_scroll
finally:
    w.config["arrow_scroll_lines"] = _s35
    w.apply_style()

print("\n=== 36. 字号单位统一成像素（书页 / 卡片 / 社区页同一套）===")

_s36 = (w.config.get("font_size"), w.config.get("auto_mode"), w.config.get("font_unit"))
try:
    w.config["auto_mode"] = False
    w.config["font_size"] = 17
    w.apply_style()
    _u17 = w._line_unit_px()

    check("书页字号就是像素：设 17 就是 17px 的字（不再是磅）",
          w._book_font().pixelSize() == 17 and w.text_edit.font().pixelSize() == 17,
          (w._book_font().pixelSize(), w.text_edit.font().pixelSize()))

    w.config["font_size"] = 26
    w.apply_style()
    _u26 = w._line_unit_px()
    check("字号变大，一行跟着变高（17px → 26px，行高按比例涨）",
          _u26 > _u17 * 1.2, (_u17, _u26))

    check("字符级字体也按像素折成磅钉回去（不会半磅半像素）",
          abs(w._pt_for_px(17) - 17 * 72.0 / (w.text_edit.logicalDpiY() or 96.0)) < 1e-6,
          (w._pt_for_px(17), w.text_edit.logicalDpiY()))

    _merged = {"font_size": 13}
    _moved = app_mod.migrate_font_unit({}, _merged)
    check("老配置（13 磅）自动换成 17 像素，只换一次",
          _moved and _merged["font_size"] == 17 and _merged.get("font_unit") == "px",
          (_moved, _merged))
    _merged2 = {"font_size": 17, "font_unit": "px"}
    check("已经换过的配置再进来一个字都不动（不会反复放大）",
          app_mod.migrate_font_unit({"font_unit": "px"}, _merged2) is False
          and _merged2["font_size"] == 17, _merged2)

    _dlg = app_mod.SettingsDialog(w.config, None)
    try:
        check("设置里书页和社区页的字号都写明单位 px",
              "px" in _dlg.font_spin.suffix() and "px" in _dlg.feed_font_spin.suffix(),
              (_dlg.font_spin.suffix(), _dlg.feed_font_spin.suffix()))
    finally:
        _dlg.deleteLater()
finally:
    w.config["font_size"], w.config["auto_mode"] = _s36[0], _s36[1]
    w.apply_style()

w.config.clear()
w.config.update(_cfg_bak)
w.save_config = _save_bak

ok = sum(1 for _n, c in RESULTS if c)
bad = [n for n, c in RESULTS if not c]
print("\n================ 结果 ================")
print("通过 %d / %d" % (ok, len(RESULTS)))
for n in bad:
    print("  未通过: " + n)
sys.exit(0 if not bad else 1)
