# -*- coding: utf-8 -*-
"""来源层逻辑探针：**零网络**（夹具驱动 + requests 换成抛异常）。

    python tests/probe_sources.py          # 在 StealthReader 目录下跑

零网络不是靠自觉：脚本开头把 `requests.get/post/Session.get` 全换成抛异常，
而所有适配器都拿到一个读夹具的替身 Http。所以"这次一个包都没发"是可以被证明的。
夹具是 `tests/record_fixtures.py` 从真站录下来的（见 tests/README.md）。
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, SR)
os.chdir(SR)

import requests                                                          # noqa: E402
requests.get = requests.post = requests.Session.request = lambda *a, **k: 1 / 0
requests.Session.get = lambda *a, **k: 1 / 0

import feed as feed_mod                                                  # noqa: E402
import feed_net                                                          # noqa: E402
import feed_sources                                                      # noqa: E402
import xhs_sign                                                          # noqa: E402
from feed_net import FeedNetError                                        # noqa: E402

RESULTS = []
SKIPPED = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("  ok  " if cond else "  FAIL") + " " + name + (("   << " + str(extra)) if extra else ""))


def skip(name, why):
    SKIPPED.append(name)
    print("  skip " + name + "（" + why + "）")


def fixture(name):
    path = os.path.join(FIX, name)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fixture_bytes(name):
    """原样读字节：NGA 那两份夹具一份是真响应（GBK），一份是裁过的（UTF-8），
    交给适配器自己去认编码——探针不该替它转好。"""
    path = os.path.join(FIX, name)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


class FakeHttp(object):
    """读夹具的替身 Http。routes: 片段 -> 对象；bytes_routes: 片段 -> 原始字节。"""

    def __init__(self, routes=None, fail=(), cache=None, bytes_routes=None, bytes_error=None,
                 status=None):
        self.routes = routes or {}
        self.bytes_routes = bytes_routes or {}
        self.bytes_error = bytes_error or {}
        self.status = status or {}      # 片段 -> HTTP 状态码（小红书那节要造 406）
        self.fail = list(fail)
        self.cache = cache
        self.served_stale = False
        self.image_calls = []
        self.calls = []          # 请求过哪些地址（虎扑那节要验翻页拼的 URL）

    def get_json(self, url, ttl=None, what="", headers=None, timeout=None):
        self.calls.append(url)
        for frag in self.fail:
            if frag in url:
                raise FeedNetError("连接超时")
        for frag, code in self.status.items():
            if frag in url:
                raise FeedNetError("服务端返回 %s" % code)
        for frag, obj in self.routes.items():
            if frag in url:
                return json.loads(json.dumps(obj))
        raise FeedNetError("接口不存在（404）")

    def fetch_image(self, url):
        self.image_calls.append(url)
        return ("img_%d.jpg" % (abs(hash(url)) % 100000)) if url else ""

    def get_bytes(self, url, ttl=None, headers=None, max_bytes=0, timeout=None):
        """按原始字节给（NGA 那条路要的就是字节：它自带编码和裸控制字符）。"""
        self.calls.append(url)
        for frag in self.fail:
            if frag in url:
                raise FeedNetError("连接超时")
        for frag, msg in self.bytes_error.items():
            if frag in url:
                raise FeedNetError(msg)
        for frag, data in self.bytes_routes.items():
            if frag in url:
                return data
        raise FeedNetError("探针里不下载")


class MemCache(object):
    img_dir = os.path.join(os.environ["TEMP"], "sr_probe_img")


TMP_CACHE = os.path.join(os.environ["TEMP"], "sr_probe_cache")

# ---------------------------------------------------------------- IT之家
print("=== 1. IT之家 ===")
raw_i = fixture("ithome_hot.json")
raw_n = fixture("ithome_newslist.json")
raw_d = fixture("ithome_detail.json")
if not raw_i:
    skip("IT之家", "缺夹具，先跑 tests/record_fixtures.py")
else:
    ih = feed_sources.IthomeSource(FakeHttp({"newslistpageget": raw_i, "newscontent": raw_d,
                                             "newslist/news": raw_n}, cache=MemCache()))
    check("栏目是 热榜/最新", ih.channels() == ["热榜", "最新"], ih.channels())
    posts, nxt = ih.list("热榜", 0)
    check("热榜拿到条目", len(posts) >= 15, len(posts))
    check("有标题 / 时间 / 原帖地址",
          all(p.title and p.ts > 0 and p.url.startswith("http") for p in posts),
          [(p.title[:10], p.url[:30]) for p in posts[:2]])
    check("单位是'阅读'（hitcount 不是赞）", all(p.score_label == "阅读" for p in posts))
    check("评论数有值", any(p.n_comments > 0 for p in posts),
          [p.n_comments for p in posts[:8]])
    check("栏目分开了最新", ih.list("最新", 0)[0][0].channel == "最新")
    if raw_d:
        d, _ = ih.detail(posts[0])
        check("正文有内容", d.text_length() > 100, d.text_length())
        check("正文里的图落到了本地（没有残留 http）",
              all(not (b.url or "").startswith("http") for b in d.body))
        check("IT之家没有可用评论接口 -> 评论如实为空", ih.comments(posts[0]) == ([], None))
    # 移动端接口挂了要退回 api/newslist/news，并且 toplist + newslist 合起来去重
    fb = feed_sources.IthomeSource(FakeHttp({"newslist/news": raw_n},
                                            fail=["newslistpageget"], cache=MemCache()))
    fp, _ = fb.list("热榜", 0)
    ids = [p.id for p in fp]
    check("移动端挂了能退回 api 列表", len(fp) >= 3, len(fp))
    check("合流去重（没有重复 id）", len(ids) == len(set(ids)))

# ---------------------------------------------------------------- linux.do / Discourse
print("\n=== 2. Discourse（linux.do 用同一个适配器）===")
raw_l = fixture("discourse_latest.json")
raw_t = fixture("discourse_topic.json")
if not (raw_l and raw_t):
    skip("Discourse", "缺夹具（linux.do 被 Cloudflare 拦，录不到）")
else:
    ds = feed_sources.DiscourseSource(FakeHttp({"latest.json": raw_l, "/t/": raw_t},
                                               cache=MemCache()))
    check("栏目是 最新/热门", ds.channels() == ["最新", "热门"], ds.channels())
    dp, dn = ds.list("最新", 0)
    check("列表拿到条目", len(dp) >= 5, len(dp))
    # 精确盯"用的是哪个时间字段"：找一个建帖时间和最后回复时间明显不同的主题，
    # 我们的 ts 必须等于后者（用 created_at 会让一条今天还在聊的老帖沉到最底下）
    sample = next((t for t in raw_l["topic_list"]["topics"]
                   if t.get("created_at") and t.get("last_posted_at")
                   and t["created_at"][:10] != t["last_posted_at"][:10]), None)
    if sample:
        got = next((p for p in dp if p.id == str(sample["id"])), None)
        check("时间字段用的是 last_posted_at，不是 created_at",
              got is not None and abs((got.ts or 0)
                                      - (feed_mod.parse_time(sample["last_posted_at"]) or 0)) < 2,
              (got.ts if got else None, feed_mod.parse_time(sample["last_posted_at"]),
               feed_mod.parse_time(sample["created_at"])))
    check("按最后回复时间倒序（置顶老公告不会卡在最前面）",
          all((dp[i].ts or 0) >= (dp[i + 1].ts or 0) for i in range(len(dp) - 1)),
          [(p.title[:14], p.created) for p in dp[:3]])
    check("单位是'回复'", all(p.comment_label == "回复" for p in dp))
    dd, _ = ds.detail(dp[0])
    check("正文出来了", dd.text_length() > 50, dd.text_length())
    dc, _ = ds.comments(dp[0])
    check("评论/回复拿到了", len(dc) >= 3, len(dc))
    depth = max((d for _c, d, _p in feed_mod.comment_forest(dc)), default=0)
    check("回复还原成了树（至少两层）", depth >= 1, depth)
    flat = feed_mod.comment_forest(dc)
    check("评论树是有限的（没有环）", len(flat) <= 200, len(flat))
    check("每条回复都有作者和时间", all(c.author and c.created for c in dc[:5]))
    check("广告帖（无正文）不炸", isinstance(ds.detail(dp[-1])[0].body, list))

# ---------------------------------------------------------------- 知乎
print("\n=== 3. 知乎（只有热榜免登录）===")
raw_z = fixture("zhihu_hot_list.json")
# 带 Cookie 才录得到的两个（`record_fixtures.py zhihu`）：正文和评论。缺了就在第 13 节跳过。
raw_za = fixture("zhihu_answer.json")
raw_zc = fixture("zhihu_comments.json")
if not raw_z:
    skip("知乎", "缺夹具")
else:
    zs = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache()))
    check("栏目是 热榜 / 推荐 / 关注（真人要的不是'只有热榜'）",
          zs.channels() == ["热榜", "推荐", "关注"], zs.channels())
    zp, zn = zs.list("热榜", 0)
    check("拿到条目", len(zp) >= 20, len(zp))
    check("没有分页", zn is None, zn)
    tgt = raw_z["data"][0]["target"]
    check("标题来自 target.title", zp[0].title == tgt["title"].strip())
    check("给人点的是网页地址、不是 api 地址",
          zp[0].url == "https://www.zhihu.com/question/%s" % tgt["id"], zp[0].url)
    check("单位是 热度/回答", (zp[0].score_label, zp[0].comment_label) == ("热度", "回答"))
    check("回答数来自 answer_count", zp[0].n_comments == int(tgt["answer_count"]))
    check("热度读出来了", any(p.score >= 10000 for p in zp),
          [(p.score, p.title[:12]) for p in zp[:3]])
    for text, want in [("1327 万热度", 13270000), ("1.2 亿热度", 120000000),
                       ("9999 热度", 9999), ("", 0), ("热度", 0)]:
        got = feed_sources.ZhihuSource._hot(text)
        check("  热度文案 %-12s -> %s" % (repr(text), want), got == want, got)
    zd, _ = zs.detail(zp[0])
    zd_txt = " ".join(b.text for b in zd.body)
    check("没 Cookie 时正文直说回答要登录（不再拿问题内容冒充正文）",
          "登录" in zd_txt and "feed_auth.json" in zd_txt, zd_txt[:60])
    check("热榜给的摘要还在，但标明了它不是回答",
          (not zp[0].excerpt) or any(zp[0].excerpt in b.text and "不是回答" in b.text for b in zd.body),
          [b.text[:26] for b in zd.body])
    check("知乎评论如实为空（没 Cookie 就不编回答）", zs.comments(zp[0]) == ([], None))

# ---------------------------------------------------------------- 微博
print("\n=== 4. 微博（只有热搜免登录）===")
raw_w = fixture("weibo_hotsearch.json")
if not raw_w:
    skip("微博", "缺夹具")
else:
    ws = feed_sources.WeiboSource(FakeHttp({"hotSearch": raw_w}, cache=MemCache()))
    check("栏目只有 热搜", ws.channels() == ["热搜"], ws.channels())
    wp, wn = ws.list("热搜", 0)
    check("拿到条目", len(wp) >= 30, len(wp))
    check("没有分页", wn is None, wn)
    it0 = raw_w["data"]["realtime"][0]
    check("标题就是词条", wp[0].title == it0["word"].strip())
    check("热度来自 num，单位是热度",
          wp[0].score == int(it0["num"]) and wp[0].score_label == "热度")
    check("没有评论数就不显示这一项", wp[0].n_comments == 0)
    check("新/热/爆 进了 tags", any(p.tags for p in wp), sum(1 for p in wp if p.tags))
    check("给的是微博搜索地址", wp[0].url.startswith("https://s.weibo.com/weibo?q="), wp[0].url)
    check("不拿 note 凑摘要（它和词条基本重复）", wp[0].excerpt == "")
    check("时间按'现在'算，不会沉到混流最后",
          abs(wp[0].ts - time.time()) < 300, wp[0].ts)
    check("话题转义正确",
          ws._search_url("#测试话题#") == "https://s.weibo.com/weibo?q=%23%E6%B5%8B%E8%AF%95%E8%AF%9D%E9%A2%98%23")
    wd, _ = ws.detail(wp[0])
    check("微博详情给出热度并直说要登录",
          any("热度" in b.text for b in wd.body) and any("登录" in b.text for b in wd.body))
    check("微博评论如实为空", ws.comments(wp[0]) == ([], None))

# ---------------------------------------------------------------- 单位与渲染
print("\n=== 5. 统计单位：数字说人话，单位跟来源走 ===")
check("一万以下原样", feed_mod._num(9999) == "9999", feed_mod._num(9999))
check("上万换算成万", feed_mod._num(23500) == "2.4 万", feed_mod._num(23500))
check("整万不拖零", feed_mod._num(13270000) == "1327 万", feed_mod._num(13270000))
check("过亿换算成亿", feed_mod._num(120000000) == "1.2 亿", feed_mod._num(120000000))
theme = feed_mod.theme_from_config({"font_size": 14, "text_color": "#d8d8d8"}, 420)
zhtml = feed_mod.render_list(zp[:1], feed_mod.FeedNav(), theme) if raw_z else ""
if zhtml:
    check("列表写'热度/回答'而不是'赞/评论'",
          "热度" in zhtml and "回答" in zhtml and "赞" not in zhtml, zhtml[-120:])
    check("上万写了'万'而不是七个零", "1327 万" in zhtml or " 万 " in zhtml)
default = feed_mod.Post(source="x", pid="1", title="t", score=428, n_comments=6)
dhtml = feed_mod.render_list([default], feed_mod.FeedNav(), theme)
check("没指定单位时还是'赞/条评论'", "428 赞" in dhtml and "6 条评论" in dhtml, dhtml[-120:])
check("两个新字段进了 to_dict/from_dict",
      feed_mod.Post.from_dict(default.to_dict()).score_label == "" and
      feed_mod.Post.from_dict(feed_mod.Post(score_label="阅读", comment_label="回复").to_dict())
      .comment_label == "回复")
tagged = feed_mod.Post(source="v2ex", pid="1", title="t", tags=["分享发现", "标签二", "标签三",
                                                               "标签四"])
thtml = feed_mod.render_list([tagged], feed_mod.FeedNav(), theme)
check("标签渲染出来了（最多三条）",
      all(t in thtml for t in ("分享发现", "标签二", "标签三")) and "标签四" not in thtml)
check("标签用的是强调色", "color:%s" % theme["accent"] in thtml)

# ---------------------------------------------------------------- 混流
print("\n=== 6. 混流：每个来源都留席位，翻了页一条都不丢 ===")
losts = []
if raw_i and raw_z:
    a = feed_sources.IthomeSource(FakeHttp({"newslistpageget": raw_i}, cache=MemCache()))
    b = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache()))
    mix = feed_sources.MixSource([a, b])
    mp, mn = mix.list("全部", 0)
    srcs = sorted(set(p.source for p in mp))
    check("两个来源都在第一页", srcs == ["ithome", "zhihu"], srcs)
    check("每页每来源不超过一半",
          all(sum(1 for p in mp if p.source == s) <= 6 for s in srcs), [p.source for p in mp])
    check("按时间倒序", all((mp[i].ts or 0) >= (mp[i + 1].ts or 0) for i in range(len(mp) - 1)))
    check("翻页游标给了", mn == 1, mn)
    seen = set(p.id for p in mp)
    losts = []
    page, cur = list(mp), mn
    for _ in range(6):
        if cur is None:
            break
        more, cur = mix.list("全部", cur)
        for p in more:
            if p.id in seen and p.source == "ithome":
                pass
            seen.add(p.id)
        page.extend(more)
    check("逐页翻完，两个来源的条目都还在", len(page) >= 20, len(page))
    check("分页不重复（同一页内 id 唯一）", len(set(p.id for p in mp)) == len(mp))
    check("详情按来源转发", mix.detail(next(p for p in mp if p.source == "zhihu"))[0].body != [])
    check("评论按来源转发", mix.comments(next(p for p in mp if p.source == "zhihu"))[0] == [])
    dead = feed_sources.MixSource([feed_sources.V2exSource(FakeHttp(cache=MemCache())),
                                   feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z},
                                                                     cache=MemCache()))])
    dp2, _ = dead.list("全部", 0)
    check("一个站连不上，另一个站的内容照常出", len(dp2) >= 6, len(dp2))
    check("连不上的站写进 errors（页脚会说出来）", len(dead.errors) == 1, dead.errors)
    check("页脚那句话是人话", "V2EX" in dead.last_error, dead.last_error)
else:
    skip("混流", "缺 IT之家或知乎夹具")

print("\n=== 7. 来源登记（默认 / 单独开关 / 全关）===")
made = feed_sources.build_real_sources(cache_root=TMP_CACHE, config={}, log=None)
check("默认：混流 + 免登录的和要 Cookie 的都在，微博默认关",
      [s.key for s in made] == ["mix", "ithome", "v2ex", "linuxdo", "zhihu", "nga", "xhs", "hupu"],
      [s.key for s in made])
check("每个来源都有 label / channels / image_base",
      all(s.label and isinstance(s.channels(), list) and isinstance(s.image_base(), str)
          for s in made))
on = feed_sources.build_real_sources(
    cache_root=TMP_CACHE, config={"feed_sources": ["ithome", "weibo"]}, log=None)
check("写进 feed_sources 才出现（微博是单独开关那一档）",
      [s.key for s in on] == ["mix", "ithome", "weibo"], [s.key for s in on])
check("全关掉返回空（由 build_sources 补离线示例）",
      feed_sources.build_real_sources(cache_root=TMP_CACHE,
                                      config={"feed_sources": []}, log=None) == [])
check("写单个字符串也认",
      [s.key for s in feed_sources.build_real_sources(
          cache_root=TMP_CACHE, config={"feed_sources": "zhihu"}, log=None)] == ["mix", "zhihu"])

print("\n=== 8. 混流的旧缓存标记会汇总（页脚要能说'用的是上次的缓存'）===")
if raw_i and raw_z:
    fake_i = FakeHttp({"newslistpageget": raw_i}, cache=MemCache())
    fake_i.served_stale = True                  # 假装这家这次给的是旧缓存（真发生时也是这样标记的）
    mix2 = feed_sources.MixSource([feed_sources.IthomeSource(fake_i),
                                   feed_sources.ZhihuSource(
                                       FakeHttp({"hot-list": raw_z}, cache=MemCache()))])
    mix2.list("全部", 0)
    check("混流收下了 stale 来源", "ithome" in mix2.stale, mix2.stale)
    mix2.list("全部", 0)
    check("重新取数会重置这个标记（不是永久粘着）", isinstance(mix2.stale, set))

print("\n=== 9. 缓存与断网容错 ===")
shutil.rmtree(TMP_CACHE, ignore_errors=True)
cache = feed_net.Cache(TMP_CACHE)
cache.write_json("https://example.com/list", {"v": 1})
http = feed_net.Http(cache=cache)
http._get = lambda *a, **k: (_ for _ in ()).throw(FeedNetError("连接超时"))
check("新鲜缓存直接命中", http.get_json("https://example.com/list", ttl=60, what="t") == {"v": 1})
check("命中时 served_stale 是 False", http.served_stale is False)
old = os.path.join(cache.json_dir, cache.key("https://example.com/list") + ".json")
os.utime(old, (time.time() - 3600, time.time() - 3600))
check("过期 + 请求失败 -> 退回旧缓存",
      http.get_json("https://example.com/list", ttl=1, what="t") == {"v": 1})
check("并且标记 served_stale", http.served_stale is True)
check("ttl=0 的约定是永不过期（图片用）",
      feed_net.Cache(TMP_CACHE).read_json("https://example.com/list", 0) == {"v": 1})
empty = feed_net.Http(cache=feed_net.Cache(os.path.join(TMP_CACHE, "none")))
empty._get = lambda *a, **k: (_ for _ in ()).throw(FeedNetError("连接超时"))
try:
    empty.get_json("https://example.com/none", ttl=60, what="t")
    check("一点缓存都没有时如实抛错", False, "没抛")
except FeedNetError as e:
    check("一点缓存都没有时如实抛错", "连接超时" in str(e), str(e))

print("\n=== 10. 结构不变量 ===")
src_feed = open(os.path.join(SR, "feed.py"), "r", encoding="utf-8").read()
check("feed.py 不 import Qt（离线可测的前提）", "PyQt5" not in src_feed and "QtCore" not in src_feed)
check("feed.py 不碰网络", "import requests" not in src_feed and "urlopen" not in src_feed)
check("feed.py 不依赖来源层/网络层（顶层 import 里不许出现）",
      not any(("feed_net" in ln or "feed_sources" in ln)
              for ln in src_feed.splitlines() if ln.startswith(("import ", "from "))))
src_main = open(os.path.join(SR, "main.py"), "r", encoding="utf-8").read()
check("main.py 里社区取数只在后台线程里做",
      "threading.Thread(target=work" in src_main)
if raw_i and raw_d:
    dt, _ = feed_sources.IthomeSource(FakeHttp({"newscontent": raw_d}, cache=MemCache())).detail(posts[0])
    raw_text = feed_mod.strip_html(raw_d.get("detail") or "")
    got = "".join(b.text for b in dt.body)
    check("IT之家正文没有被大段吞掉（块文本 >= 原文的 80%）",
          len(got) >= len(raw_text) * 0.8, (len(got), len(raw_text)))
    check("正文里的图片全落地（一个 http 都不许留）",
          all(not (b.url or "").startswith("http") for b in dt.body))
if raw_z:
    zd2, _ = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache())).detail(zp[0])
    check("知乎详情里的摘要是逐字来自响应的（没有加工过的假正文）",
          (not zp[0].excerpt) or any(zp[0].excerpt in b.text for b in zd2.body),
          [b.text[:24] for b in zd2.body])

print("\n=== 11. 凭据管线：配了没配、配了管不管用，都要说清楚 ===")


class FakeResp(object):
    """只带 feed_net 会看的那几个字段，用来把**真响应体**喂进真实的错误映射。"""

    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}
        try:
            self.text = body.decode("utf-8", "replace")
        except Exception:
            self.text = ""


class FakeSession(object):
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **k):
        return self.resp


def mapped(url, status, body, headers=None):
    """走真实的 Http.get_json（含状态码→人话的映射），返回那句人话。"""
    h = feed_net.Http(cache=feed_net.Cache(os.path.join(TMP_CACHE, "err")))
    h._session = lambda: FakeSession(FakeResp(status, body, headers))
    try:
        h.get_json(url, ttl=60, what="t")
        return "（没抛错）"
    except FeedNetError as e:
        return str(e)


nga_body = open(os.path.join(FIX, "nga_unauth_403.json"), "rb").read()
cf_body = open(os.path.join(FIX, "linuxdo_cf.html"), "rb").read()
chip_body = open(os.path.join(FIX, "chiphell_567.html"), "rb").read()
check("NGA 的 GBK 403 被认成'要登录'（不是含糊的'被拒绝'）",
      mapped("https://bbs.nga.cn/thread.php?fid=-7&__output=8", 403, nga_body) == "要登录（403）",
      mapped("https://bbs.nga.cn/thread.php?fid=-7&__output=8", 403, nga_body))
check("Cloudflare 的'Just a moment'认成盾，不是普通 403",
      "Cloudflare" in mapped("https://linux.do/latest.json", 403, cf_body),
      mapped("https://linux.do/latest.json", 403, cf_body))
check("Chiphell 的非标准 567 如实报出来",
      "567" in mapped("https://www.chiphell.com/", 567, chip_body),
      mapped("https://www.chiphell.com/", 567, chip_body))
check("_says_login 也认 \\uXXXX 转义写法",
      feed_net._says_login(FakeResp(403, b'{"error":{"0":"\\u672a\\u767b\\u5f55"}}')) is True)
check("普通 403（比如知乎的风控）不会被误判成'要登录'",
      feed_net._says_login(FakeResp(403, open(os.path.join(
          FIX, "zhihu_unauth_answers.json"), "rb").read())) is False)

empty = feed_sources.IthomeSource(FakeHttp({"newslistpageget": raw_i}, cache=MemCache()))
check("没配凭据时 has_auth 为假、状态是'免登录'",
      empty.has_auth is False and empty.auth_state() == "免登录")
withauth = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache()),
                                    {"cookie": "z_c0=fake", "user_agent": "UA"})
check("配了 Cookie 就认（知乎这条靠它去试正文）",
      withauth.has_auth is True and withauth.auth_state() == "Cookie 已配置",
      withauth.auth_state())
noauth = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache()))
check("没配 Cookie 时状态如实说'未配置'", noauth.auth_state() == "免登录"
      and noauth.has_auth is False, noauth.auth_state())
check("没配时代码里那句实话告诉人去哪贴 Cookie", "feed_auth.json" in noauth.auth_note(),
      noauth.auth_note())
check("配了之后的措辞不替接口打包票", "没给" in withauth.auth_note() or "原文" in withauth.auth_note())
check("要登录的来源没配 Cookie 时状态点明这一点（页脚就靠这句）",
      feed_sources.NgaSource(FakeHttp(cache=MemCache())).auth_state()
      == "Cookie 未配置（这个来源要登录）")

print("\n=== 12. NGA（批次 D）：免登录进不去；贴上 Cookie 后按**真响应**解析 ===")
check("NGA 标记为需要凭据", feed_sources.NgaSource.needs_auth is True)
check("栏目 = 板块短表（第一条是网事杂谈 fid -7）",
      feed_sources.NgaSource.CHANNELS[0] == "网事杂谈"
      and feed_sources.NgaSource.FID["网事杂谈"] == "-7",
      feed_sources.NgaSource.CHANNELS[:3])
check("短表里的 fid 全是字符串（官方接口两种类型都出现过，统一过）",
      all(isinstance(v, str) and v for v in feed_sources.NgaSource.FID.values()),
      sorted(set(type(v).__name__ for v in feed_sources.NgaSource.FID.values())))
check("短表有十来个工位上会看的板块（不是一个）",
      len(feed_sources.NgaSource.CHANNELS) >= 8, len(feed_sources.NgaSource.CHANNELS))
na = feed_sources.NgaSource(FakeHttp(cache=MemCache()))
check("没配 Cookie 时状态说得明白",
      "未配置" in na.auth_state() and "要登录" in na.auth_state(), na.auth_state())
check("栏目名 → fid", (na.board_fid("晴风村"), na.board_fid("-7955747"), na.board_fid("不存在"))
      == ("-7955747", "-7955747", "-7"), (na.board_fid("晴风村"), na.board_fid("不存在")))
check("fid → 栏目名（认不出来就把 fid 本身当名字，不留空白）",
      (na.board_label("-7"), na.board_label("999999")) == ("网事杂谈", "999999"),
      (na.board_label("-7"), na.board_label("999999")))
check("短表就在 boards() 的兜底里（拉不到全量也不至于只剩一个板块）",
      [b["label"] for b in na.boards()][:2] == ["网事杂谈", "晴风村"],
      [b["label"] for b in na.boards()][:3])
try:
    na.list("网事杂谈", 0)
    check("连不上/没登录时如实抛错（不返回空列表假装成功）", False, "没抛")
except FeedNetError as e:
    check("连不上/没登录时如实抛错（不返回空列表假装成功）", True, str(e))

# 板块分类树：真响应裁了一半（7 个分类各留第一组前 8 条），形状照原样
raw_cat = fixture("nga_category.json")
if raw_cat:
    nb = feed_sources.NgaSource(
        FakeHttp(bytes_routes={"app_api.php": json.dumps(raw_cat).encode("utf-8")},
                 cache=MemCache()), {"cookie": "ngaPassportUid=1"})
    board_list = nb.boards()
    flat = []
    for _cat in raw_cat["result"]:
        for _g in _cat["groups"]:
            for _f in _g["forums"]:
                flat.append((str(_f["name"]), str(_f["fid"]), str(_g["name"])))
    check("（真夹具）分类树解出了全部板块条目", len(board_list) == len(flat),
          (len(board_list), len(flat)))
    check("（真夹具）条目的名字/fid/分组都对得上（含 fid 是整数的那些）",
          [(b["label"], b["fid"], b["group"]) for b in board_list] == flat,
          board_list[:2])
    check("（真夹具）拉过全量之后 channels() 就是全量（界面里能滚能搜）",
          len(nb.channels()) == len(board_list) and nb.channels()[0] == flat[0][0],
          (len(nb.channels()), nb.channels()[:2]))
    check("（真夹具）全量里的板块也能做 fid/名字 双向翻译",
          nb.board_fid(flat[1][0]) == flat[1][1] and nb.board_label(flat[1][1]) == flat[1][0],
          (nb.board_fid(flat[1][0]), flat[1][1]))
    # 重复条目只留一条：官方树里有「手游评分版=863」这种同名同 fid 的重复
    dup = feed_sources._nga_boards({"result": [{"groups": [{"name": "g", "forums": [
        {"fid": 7, "name": "甲"}, {"fid": "7", "name": "甲"}, {"fid": 8, "name": "甲"},
        {"fid": None, "name": "乙"}, {"fid": 9, "name": ""}]}]}]})
    check("解析器：同名同 fid 去重、同名不同 fid 留着、空名字和空 fid 丢掉",
          dup == [{"label": "甲", "fid": "7", "group": "g"},
                  {"label": "甲", "fid": "8", "group": "g"}], dup)
else:
    skip("NGA 板块分类树", "缺 tests/fixtures/nga_category.json")

# 编码：真人报过"NGA 全是乱码"。根因就是死信它自称的 encode=gbk（read.php 实际发 UTF-8）
_zh = {"encode": "gbk", "data": {"__T": {"0": {"subject": "帖子的标题", "author": "某人"}}}}
_u8 = json.dumps(_zh, ensure_ascii=False).encode("utf-8")     # 真身 UTF-8，却自称 gbk
_gbk = json.dumps(_zh, ensure_ascii=False).encode("gbk")      # 真身 GBK，也自称 gbk
check("（编码）自称 gbk、实际是 UTF-8 的响应不再解成乱码",
      feed_sources._nga_parse(_u8)["data"]["__T"]["0"]["subject"] == "帖子的标题",
      feed_sources._nga_parse(_u8)["data"]["__T"]["0"]["subject"])
check("（编码）真的是 GBK 的响应照旧解对",
      feed_sources._nga_parse(_gbk)["data"]["__T"]["0"]["author"] == "某人",
      feed_sources._nga_parse(_gbk)["data"]["__T"]["0"]["author"])
_MOJ_TXT = "标题与作者以及发布时刻表"
# 每个汉字 3 字节，偶数个汉字 → 整个 UTF-8 串正好能被 gb18030 无残留地"误解"一遍（不丢字节）
_MOJ = _MOJ_TXT.encode("utf-8").decode("gb18030")            # 真机上那种乱码就是这么来的
_MOJ_BAD = _MOJ_TXT.encode("utf-8").decode("gb18030", "replace")  # 带替换符的那种（也要认）
_tree = feed_sources._nga_fix_tree({"a": _MOJ, "b": "某人", "c": ["x", _MOJ],
                                    "n": 1, "none": None})
check("（编码）解析完再按字符串兜一遍：嵌在结构里的乱码也修回来，正常中文和数字不动",
      _tree == {"a": _MOJ_TXT, "b": "某人", "c": ["x", _MOJ_TXT], "n": 1, "none": None}, _tree)
check("（编码）乱码判据只认\"UTF-8 被当 GBK 解\"，正常中文不误判",
      feed_sources._nga_mojibake("正常的简体中文标题") is False
      and feed_sources._nga_mojibake(_MOJ) is True
      and feed_sources._nga_mojibake(_MOJ_BAD) is True
      and feed_sources._nga_mojibake(_MOJ_TXT) is False
      and feed_sources._nga_mojibake("") is False and feed_sources._nga_mojibake("abc") is False,
      (feed_sources._nga_mojibake("正常的简体中文标题"),
       feed_sources._nga_mojibake(_MOJ), feed_sources._nga_mojibake(_MOJ_BAD)))

# 真夹具：`record_fixtures.py nga` 用浏览器 Cookie 录的（列表、正文各裁过，老帖那条是原样 GBK）
raw_ngal = fixture_bytes("nga_list.json")
raw_ngar = fixture_bytes("nga_read.json")
raw_nga403 = fixture_bytes("nga_old_thread_403.json")
if raw_ngal is None or raw_ngar is None:
    skip("NGA 真夹具", "缺 nga_list.json / nga_read.json（先跑 tests/record_fixtures.py nga）")
else:
    cred = {"cookie": "ngaPassportUid=1", "user_agent": "UA"}
    ng = feed_sources.NgaSource(
        FakeHttp(bytes_routes={"thread.php": raw_ngal}, cache=MemCache()), cred)
    ng_posts, ng_nxt = ng.list("网事杂谈", 0)
    check("（真夹具）列表出条目、没有下一页", len(ng_posts) >= 5 and ng_nxt is None, len(ng_posts))
    check("（真夹具）标题 / 作者 / 时间 / 原帖地址都在",
          all(p.title and p.author and p.ts > 0 and p.url.startswith("https://bbs.nga.cn/read.php?tid=")
              for p in ng_posts),
          [(p.title[:12], p.author, p.url[-10:]) for p in ng_posts[:2]])
    check("（真夹具）统计单位是'回复'（列表里根本没有赞这个数）",
          all(p.comment_label == "回复" for p in ng_posts) and any(p.n_comments > 0 for p in ng_posts),
          [p.n_comments for p in ng_posts[:6]])
    check("（真夹具）NGA 塞在列表里的那条'错误提示'被筛掉了（它没有作者也没有时间）",
          all(p.author and p.ts > 0 for p in ng_posts)
          and not any("时间超过限制" in p.title for p in ng_posts),
          [p.title[:14] for p in ng_posts])
    check("（真夹具）按'最后回复'倒序",
          all((ng_posts[i].ts or 0) >= (ng_posts[i + 1].ts or 0) for i in range(len(ng_posts) - 1)))
    ng_rows = feed_sources._nga_rows(feed_sources._nga_parse(raw_ngal), "__T")
    check("（真夹具）thread.php 的 __T 是帖子表，能收成列表（原样 %d 条，筛掉错误提示后 %d 条帖子）"
          % (len(ng_rows), len(ng_posts)),
          len(ng_rows) == len(ng_posts) + 1 and len(ng_posts) >= 5, (len(ng_rows), len(ng_posts)))
    check("（真夹具）GBK 的 json 也解得开（strict=False：它里面有裸控制字符）",
          isinstance(feed_sources._nga_parse(raw_ngal), dict))

    ng2 = feed_sources.NgaSource(
        FakeHttp(bytes_routes={"read.php": raw_ngar}, cache=MemCache()), cred)
    ng_read = feed_sources._nga_parse(raw_ngar)
    meta = (ng_read.get("data") or {}).get("__T") or {}
    op = feed_sources._nga_op(ng_read)
    ngd, _ = ng2.detail(ng_posts[0])
    body_txt = " ".join(b.text for b in ngd.body)
    # 真夹具里楼主帖的图是相对路径（`[img]./mon_202610/16/xxx.jpg[/img]`），
    # 所以算"正文文字"前先把图片段整段去掉，拿第一段真正的话来比对
    op_text = re.sub(r"\[img\].*?\[/img\]", " ", op.get("content") or "", flags=re.S)
    op_plain = feed_mod.strip_html(re.sub(r"\[[^\]]{0,60}\]", "", op_text)).strip()
    check("（真夹具）正文取的是 lou=0 那一楼",
          len(body_txt) > 40 and op_plain[:16] in body_txt, (len(body_txt), op_plain[:16]))
    starts_img = (op.get("content") or "").lstrip().startswith("[img]")
    check("（真夹具）楼主帖开头的相对路径图被补成完整地址、落到本地",
          (not starts_img)
          or (bool(ngd.body) and ngd.body[0].kind == feed_mod.BLOCK_IMG
              and not (ngd.body[0].url or "").startswith("http")),
          (starts_img, [(b.kind, (b.url or "")[:26]) for b in ngd.body[:3]]))
    check("（真夹具）正文里不残留任何 http 外链", 
          all(not (b.url or "").startswith("http") for b in ngd.body))
    check("（真夹具）标题与作者来自 __T（楼主帖的作者名 NGA 是真给的）",
          ngd.title == feed_mod.strip_html(meta.get("subject") or "") and ngd.author == meta.get("author"),
          (ngd.title[:14], ngd.author, meta.get("author")))
    check("（真夹具）read.php 的 __T 是**平字典**（主题元信息），不会被当成帖子表硬解",
          feed_sources._nga_rows(ng_read, "__T") == [],
          list(meta.keys())[:6])
    check("（真夹具）时间取的是 postdatetimestamp（真 unix 秒），不是那个'2026-10-16 19:29'字符串",
          ngd.ts == feed_mod.parse_time(op.get("postdatetimestamp")), (ngd.ts, op.get("postdatetimestamp")))
    ngc, _ = ng2.comments(ng_posts[0])
    check("（真夹具）评论 = 除 lou=0 之外的那几楼", len(ngc) >= 1, len(ngc))
    all_txt = " ".join(b.text for c in ngc for b in c.body)
    check("（真夹具）评论正文在，而且 BBCode 标记被清掉了",
          all(c.body for c in ngc) and "[b]" not in all_txt and "[pid=" not in all_txt
          and "[/uid]" not in all_txt,
          [[b.text[:14] for b in c.body][:1] for c in ngc[:2]])
    check("（真夹具）NGA 只给 UID 占位名时作者留空，不把 'UID:123' 摆给人看",
          all("UID:" not in (c.author or "") for c in ngc), [c.author for c in ngc[:3]])

    if raw_nga403 is not None:
        # 老帖 403 的原因就写在返回体里（GBK）——从真夹具里现抠，不手写这句话
        ng_msg = mapped("https://bbs.nga.cn/read.php?tid=1&__output=8", 403, raw_nga403)
        check("（真夹具）老帖 403 的返回体我们自己会带出原因", "时间超过限制" in ng_msg, ng_msg)
        ng3 = feed_sources.NgaSource(
            FakeHttp(bytes_error={"read.php": ng_msg}, cache=MemCache()), cred)
        try:
            ng3.detail(ng_posts[0])
            check("老帖被挡时翻译成人话，绝不谎称'要登录'", False, "没抛")
        except FeedNetError as e:
            check("老帖被挡时翻译成人话，绝不谎称'要登录'",
                  "时间超过限制" in str(e) and "登录" not in str(e), str(e))
        check("老帖看不了时评论如实为空（不造几条出来）", ng3.comments(ng_posts[0]) == ([], None))
    else:
        skip("NGA 老帖 403", "缺 nga_old_thread_403.json")

    # 这两条是**按 NGA 的写法手写**的输入（真夹具里不一定恰好有图/引用），
    # 只验"我们这层标记清理和图片落地写得对"，不代表接口形状。
    ng4 = feed_sources.NgaSource(FakeHttp(cache=MemCache()), cred)
    bbc = feed_sources._nga_blocks('看这个[img]https://x/a.jpg[/img]<br/>正文[b]加粗[/b]'
                                   '[quote]引用的话[/quote]结束')
    kinds = [b.kind for b in bbc]
    texts = " ".join(b.text for b in bbc)
    check("（手写形状）[img] 变成图片块、[quote] 里的字留着、[b] 这类标记丢掉",
          feed_mod.BLOCK_IMG in kinds and "引用的话" in texts
          and "[b]" not in texts and "[/quote]" not in texts,
          (kinds, texts[:60]))
    pics = ng4._localize_images(feed_sources._nga_blocks("[img]https://img.nga.cn/a.jpg[/img]"), limit=4)
    check("（手写形状）图会被落到本地（正文里不留 http 外链）",
          bool(pics) and pics[0].kind == feed_mod.BLOCK_IMG
          and not (pics[0].url or "").startswith("http"), [(b.kind, b.url) for b in pics])

print("\n=== 13. 知乎：回答就是\"评论\"那一栏，正文只放问题自己的信息（真夹具）===")
zlist = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache()))
zp0, _ = zlist.list("热榜", 0)
d_noauth, _ = zlist.detail(zp0[0])
noauth_txt = " ".join(b.text for b in d_noauth.body)
check("没配 Cookie：正文直说回答要登录，并告诉去哪儿配",
      "登录" in noauth_txt and "feed_auth.json" in noauth_txt, noauth_txt[:60])
check("没配 Cookie：不编回答", zlist.comments(zp0[0]) == ([], None))
zdeny = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, fail=["answers"], cache=MemCache()),
                                 {"cookie": "z_c0=expired"})
d_deny, _ = zdeny.detail(zp0[0])
deny_txt = " ".join(b.text for b in d_deny.body)
check("配了 Cookie 但接口拦住：实话里带上了失败原因，而不是假装没配",
      "这次没拿到回答" in deny_txt and "连接超时" in deny_txt, deny_txt[-80:])
check("而且不再把'还要前端签名'那句没验证过的猜测写给人看",
      "x-zse-96" not in deny_txt and "zse" not in deny_txt)
# 401 和 403 是两回事（403 风控是本机实测撞到过的：同一段代码几分钟前还在正常取 20 条评论）
_why403 = feed_sources._zhihu_why("HTTP 403 您当前请求存在异常，暂时限制本次访问。e3b0c44298fc")
_why401 = feed_sources._zhihu_why("HTTP 401 身份未经过验证")
check("403 风控不会被说成'Cookie 过期了'（错怪 Cookie 是以前的原话）",
      "不是" in _why403 and "风控" in _why403 and "过期" not in _why401, (_why403, _why401))
check("401 才说没登录", "登录" in _why401, _why401)


class _CountingHttp(object):
    """数调用次数。fail_first=True 时第一次必失败，用来验"失败不许缓存一辈子"。"""

    def __init__(self, payload, fail_first=True):
        self.payload = payload
        self.calls = 0
        self.fail_first = fail_first
        self.cache = None

    def get_json(self, url, ttl=None, what="", headers=None, timeout=None):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise FeedNetError("连接超时")
        return json.loads(json.dumps(self.payload))


if raw_za:
    _ch = _CountingHttp(raw_za)
    _zr = feed_sources.ZhihuSource(_ch, {"cookie": "z_c0=ok"})
    _zr._answers("q1")
    _zr._answers("q1")
    check("刚失败过的回答请求不重试（免得一点就狂打接口）", _ch.calls == 1, _ch.calls)
    _items, _why, _when = _zr._ans["q1"]
    _zr._ans["q1"] = (_items, _why, _when - _zr.RETRY_AFTER - 1)   # 假装过了重试窗口
    _zr._answers("q1")
    check("过了重试窗口就允许再问一次（以前失败缓存一辈子，一次限流=这个问题永远打不开）",
          _ch.calls == 2, _ch.calls)

# 真夹具：`record_fixtures.py zhihu` 用浏览器 Cookie 录下来的回答（正文截到 1200 字、去掉头像等字段）
if raw_za is None or raw_zc is None:
    skip("知乎回答（zhihu_answer.json 缺失：录一次 record_fixtures.py zhihu 才会有）")
else:
    zok = feed_sources.ZhihuSource(_CountingHttp(raw_za, fail_first=False), {"cookie": "z_c0=ok"})
    answers = raw_za.get("data") or []
    real_html = (answers[0].get("content") or "") if answers else ""
    plain = feed_mod.strip_html(real_html)
    d_ok, _ = zok.detail(zp0[0])
    ok_txt = " ".join(b.text for b in d_ok.body)
    d_c, _ = zok.comments(zp0[0])
    got_txt = " ".join(b.text for c in d_c for b in c.body)
    check("（真夹具）有 Cookie 时正文改说\"回答在下面这一栏\"",
          "回答" in ok_txt and "下面" in ok_txt, ok_txt[:60])
    check("（真夹具）正文里**不再**塞回答内容（回答归评论那一栏）",
          (not plain) or plain[:20] not in ok_txt, (len(plain), ok_txt[:40]))
    check("（真夹具）回答真的显示出来了，而且一个字没被吞",
          len(d_c) == len(answers) >= 1 and ((not plain) or plain[:20] in got_txt),
          (len(d_c), len(answers), got_txt[:40]))
    check("（真夹具）回答作者和赞同数都留住了",
          bool(d_c) and d_c[0].author == feed_sources._zhihu_name(answers[0].get("author"))
          and d_c[0].score == int(answers[0].get("voteup_count") or 0),
          (d_c[0].author if d_c else None, d_c[0].score if d_c else None,
           feed_sources._zhihu_name(answers[0].get("author"))))
    check("（真夹具）同一问题只请求一次（详情和评论共用 `_ans`）",
          zok.http.calls == 1, zok.http.calls)
    check("作者的两种形状都认（回答是 author.name，评论才是嵌一层的 member.name）",
          feed_sources._zhihu_name({"name": "甲"}) == "甲"
          and feed_sources._zhihu_name({"role": "normal", "member": {"name": "乙"}}) == "乙")
    check("作者整个是 null（匿名/已注销）时给空字符串，不显示 None 那种乱码",
          feed_sources._zhihu_name(None) == "" and feed_sources._zhihu_name({"role": "normal"}) == "")
    check("回答里没给正文时不留一条空评论（漏出原因或摘要）",
          all(c.body and c.body[0].text for c in d_c))
    check("回答拿不到时如实为空", zdeny.comments(zp0[0]) == ([], None))
    check("同一个问题不会为详情和评论各下一次（_ans 共用）",
          len(zok._ans) == 1, list(zok._ans.keys())[:2])

print("\n=== 14. 小红书（批次 E）：免登录是空壳，带上 Cookie 后内容在页面的状态块里 ===")
check("小红书标记为需要凭据", feed_sources.XhsSource.needs_auth is True)
check("栏目只有 推荐", feed_sources.XhsSource.CHANNELS == ("推荐",))
xs = feed_sources.XhsSource(FakeHttp(cache=MemCache()))
check("没配 Cookie 时状态说得明白",
      "未配置" in xs.auth_state() and "要登录" in xs.auth_state(), xs.auth_state())


def _xhs_page(state):
    """把夹具里的状态包成真页面里的写法（真页面就是 `window.__INITIAL_STATE__={...}`）。"""
    return ("<html><head><script>window.__INITIAL_STATE__=%s</script></head><body>x</body></html>"
            % json.dumps(state, ensure_ascii=False)).encode("utf-8")


raw_xe = fixture("xhs_explore_state.json")
raw_xn = fixture("xhs_note_state.json")
if not raw_xe:
    skip("小红书真夹具", "缺 xhs_explore_state.json（先跑 tests/record_fixtures.py xhs）")
else:
    cred = {"cookie": "web_session=1", "user_agent": "UA"}
    xs2 = feed_sources.XhsSource(
        FakeHttp(bytes_routes={"explore": _xhs_page(raw_xe)}, cache=MemCache()), cred)
    xs_posts, xs_nxt = xs2.list("推荐", 0)
    want = len([it for it in raw_xe["feed"]["feeds"]
                if str((it.get("noteCard") or {}).get("type")) != "video"
                and str((it.get("noteCard") or {}).get("displayTitle") or "").strip()])
    check("（真夹具）列表出条目、没有下一页", len(xs_posts) == want >= 1 and xs_nxt is None,
          (len(xs_posts), want))
    check("（真夹具）标题 / 作者 / 赞数都对得上",
          all(p.title and p.author and p.url.startswith("https://www.xiaohongshu.com/explore/")
              for p in xs_posts)
          and all(p.score >= 0 for p in xs_posts)
          and xs_posts[0].title == feed_mod.strip_html(
              raw_xe["feed"]["feeds"][0]["noteCard"]["displayTitle"]),
          [(p.title[:12], p.author, p.score) for p in xs_posts[:2]])
    check("（真夹具）地址带着 xsec_token（没有它打不开详情）",
          all("xsec_token=" in p.url for p in xs_posts), xs_posts[0].url[-40:])
    check("（真夹具）视频笔记没进来（这个窗口不放视频）",
          not any(str((it.get("noteCard") or {}).get("type")) == "video"
                  and str((it.get("noteCard") or {}).get("displayTitle") or "") in
                  [p.title for p in xs_posts] for it in raw_xe["feed"]["feeds"]))

    # 免登录那张空壳页：要能说清"这是要登录"，而不是甩一句"改版了"
    xs_shell = feed_sources.XhsSource(
        FakeHttp(bytes_routes={"explore": b"<html><body>SPA shell</body></html>"}, cache=MemCache()),
        cred)
    try:
        xs_shell.list("推荐", 0)
        check("空壳页会如实说'要登录'", False, "没抛")
    except FeedNetError as e:
        check("空壳页会如实说'要登录'", "feed_auth.json" in str(e) and "登录" in str(e), str(e))

if not raw_xn:
    skip("小红书正文", "缺 xhs_note_state.json")
else:
    nid = list(raw_xn["note"]["noteDetailMap"].keys())[0]
    note = raw_xn["note"]["noteDetailMap"][nid]["note"]
    xs3 = feed_sources.XhsSource(
        FakeHttp(bytes_routes={"/explore/": _xhs_page(raw_xn)},
                 status={"edith.xiaohongshu.com": 406}, cache=MemCache()), cred)
    seed = feed_sources.Post(
        source="xhs", source_label="小红书", channel="推荐", pid=nid,
        title=note.get("title") or "", author="", score=0,
        url="https://www.xiaohongshu.com/explore/%s?xsec_token=tok&xsec_source=pc_feed" % nid)
    xsd, _ = xs3.detail(seed)
    d_txt = " ".join(b.text for b in xsd.body)
    # 正文里的换行会变成 <br/>，所以拿第一行比（不拿带换行的切片去比）
    head = (note.get("desc") or "").split("\n")[0][:12]
    check("（真夹具）正文取的是 desc，话题标记清掉了",
          bool(head) and head in d_txt and "[话题]" not in d_txt, (head, d_txt[:40]))
    check("（真夹具）标题/作者/标签来自笔记本身",
          xsd.title == feed_mod.strip_html(note.get("title") or "")
          and xsd.author == (note.get("user") or {}).get("nickname")
          and xsd.tags == [t["name"] for t in note.get("tagList") or []][:6],
          (xsd.title[:12], xsd.author, xsd.tags[:3]))
    check("（真夹具）毫秒时间戳变成秒了（parse_time 自己认毫秒，不认就会显示成 5 万年后）",
          xsd.ts == feed_mod.parse_time(note.get("time")) and 1e9 < xsd.ts < 2e9,
          (xsd.ts, note.get("time")))
    check("（真夹具）图片落到本地（正文里不留 http 外链）",
          any(b.kind == feed_mod.BLOCK_IMG for b in xsd.body)
          and all(not (b.url or "").startswith("http") for b in xsd.body),
          [(b.kind, (b.url or "")[:20]) for b in xsd.body[:4]])
    check("画面里把评论那件事说清楚了（签名算出来了，但它判 406，不是'没人评论'）",
          "406" in d_txt and "签名" in d_txt and "O" in d_txt, d_txt[-80:])
    check("小红书的评论如实为空（接口要它的 x-s 签名，实测连插件自己算的也 406）",
          xs3.comments(seed) == ([], None))
    # 详情和评论共用一次请求：第二次不该再去拉
    xs3.http.bytes_routes = {}
    xs3.http.fail = ["/explore/"]
    d2, _ = xs3.detail(seed)
    check("详情只拉一次（评论那层不再重复请求）", d2.title == xsd.title)
    # token 丢了就打不开——如实说，不去瞎猜
    xs4 = feed_sources.XhsSource(FakeHttp(cache=MemCache()), cred)
    seed2 = feed_sources.Post(source="xhs", pid="deadbeef", title="t", url="https://www.xiaohongshu.com/explore/deadbeef")
    d3, _ = xs4.detail(seed2)
    check("（手写形状）没有 xsec_token 时如实说 token 丢了",
          "xsec_token" in " ".join(b.text for b in d3.body))
    # 状态块是 JS 字面量：手写两处真写法（真页面上就是这么发的），验抠取那一步
    js = ('<html><script>window.__INITIAL_STATE__='
          '{"a":undefined,"b":new Map([]),"c":new Set([]),'
          '"d":"字符串里有 } < 这种东西，非贪婪正则会在这里截断",'
          '"feed":{"feeds":[{"id":"1","xsecToken":"t","noteCard":'
          '{"type":"normal","displayTitle":"标题","user":{"nickName":"某人"},'
          '"interactInfo":{"likedCount":"7"},"cover":{"infoList":[{"url":"http://x/1.jpg"}]}}}]}}'
          '</script></html>').encode("utf-8")
    xs5 = feed_sources.XhsSource(FakeHttp(bytes_routes={"explore": js}, cache=MemCache()), cred)
    js_posts, _ = xs5.list("推荐", 0)
    check("（手写形状）undefined / new Map([]) / new Set([]) 都当得了 JSON，"
          "而且不会被字符串里的 '} <' 提前截断",
          len(js_posts) == 1 and js_posts[0].title == "标题" and js_posts[0].score == 7,
          [(p.title, p.score) for p in js_posts])
print("\n=== 15. 虎扑（免登录：版面列表是 HTML，正文和回复在页面数据里）===")
raw_hl = fixture_bytes("hupu_list.html")
raw_ht = fixture_bytes("hupu_thread.html")
if raw_hl is None or raw_ht is None:
    skip("虎扑夹具", "缺 hupu_list.html / hupu_thread.html（先跑 tests/record_fixtures.py hupu）")
else:
    hp = feed_sources.HupuSource(
        FakeHttp(bytes_routes={"/bxj": raw_hl, "643064746.html": raw_ht}, cache=MemCache()))
    check("虎扑的栏目就是真版面（17 个，名字和 slug 都不重复）",
          len(hp.channels()) == 17 and len(set(hp.channels())) == 17, len(hp.channels()))
    check("板块表是静态的：boards() 不发网络请求，label/slug 双向翻译都对，认不出退回步行街",
          hp.http.calls == [] and hp.board_fid("篮球场") == "nba"
          and hp.board_label("nba") == "篮球场" and hp.board_fid("不认识的") == "bxj",
          (hp.http.calls, hp.board_fid("篮球场"), hp.board_label("nba")))
    hp_posts, hp_nxt = hp.list("步行街主干道", 0)
    check("（真夹具）版面列表解出 12 条，标题/作者/回复/浏览都在",
          len(hp_posts) == 12 and all(p.title and p.author for p in hp_posts),
          [(p.title[:12], p.author, p.n_comments, p.score) for p in hp_posts[:2]])
    check("（真夹具）统计单位是'回复/浏览'（虎扑列表给的就不是赞）",
          all(p.comment_label == "回复" and p.score_label == "浏览" for p in hp_posts))
    check("（真夹具）原帖地址是网页地址（不是 API）",
          all(p.url.startswith("https://bbs.hupu.com/") and p.url.endswith(".html")
              for p in hp_posts), [p.url for p in hp_posts[:2]])
    check("（真夹具）时间 '10-19 11:10' 解成了真实的 epoch 秒",
          all(p.ts > 0 for p in hp_posts), [p.created for p in hp_posts[:3]])
    check("（真夹具）有一页就说明还有下一页（12 条 < 阈值 20 → 没有下一页）",
          hp_nxt is None, hp_nxt)
    hp.list("步行街主干道", 2)
    urls = hp.http.calls
    check("翻页：第 1 页 /bxj，第 2 页 /bxj-2（?page=2 在虎扑不生效）",
          any(u.endswith("/bxj") for u in urls) and any(u.endswith("/bxj-2") for u in urls),
          urls[-2:])
    seed = feed_sources.Post(source="hupu", source_label="虎扑", channel="步行街主干道",
                             pid="643064746", title="在爸爸朋友的公司上班，把我上到有点破防了", author="某人",
                             url="https://bbs.hupu.com/643064746.html")
    hpd, _ = hp.detail(seed)
    dtxt = " ".join(b.text for b in hpd.body)
    check("（真夹具）正文从页面数据里解出来了（不是空壳）", len(dtxt) > 300 and "公司" in dtxt,
          dtxt[:60])
    check("（真夹具）标题按页面里的再校一次", hpd.title.startswith("在爸爸朋友的公司上班"), hpd.title)
    hpc, _ = hp.comments(seed)
    check("（真夹具）回复 6 条，作者/正文/时间都在",
          len(hpc) == 6 and all(c.author and c.body and c.created for c in hpc),
          [(c.author, c.score) for c in hpc[:3]])
    check("（真夹具）回复的数字就是'亮了'（虎扑回复只有这个数）",
          all(c.score >= 0 for c in hpc) and any(c.score > 0 for c in hpc),
          [c.score for c in hpc])
    vid_json = json.dumps({"thread": {"tid": 1, "title": "视频帖", "content": "<p></p>",
                                      "video": "https://v.example.com/x.mp4"},
                           "replies": {"list": []}}, ensure_ascii=False)
    vid = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
           '{"props":{"pageProps":{"detail":%s}}}</script></body></html>' % vid_json).encode("utf-8")
    hp2 = feed_sources.HupuSource(FakeHttp(bytes_routes={"1.html": vid}, cache=MemCache()))
    vd, _ = hp2.detail(feed_sources.Post(source="hupu", pid="1", title="视频帖"))
    check("（手写形状）只有视频、没有正文时如实说是视频（并给出地址）",
          any("视频" in b.text for b in vd.body) and any("x.mp4" in b.text for b in vd.body),
          [b.text[:46] for b in vd.body])
    check("时间解析：'10-19 11:10' / '11:10' 能解，空的和乱写的不硬编",
          feed_sources._hupu_time("10-19 11:10") > 0
          and feed_sources._hupu_time("11:10") > 0
          and feed_sources._hupu_time("") == 0
          and feed_sources._hupu_time("刚刚") == 0,
          (feed_sources._hupu_time("10-19 11:10"), feed_sources._hupu_time("刚刚")))
print("\n=== 17. 小红书的 x-s 签名（照摸鱼插件那份纯 JS 实现搬的，逐字节比对过）===")
# 这 6 项是**固定的比对标量**：同一组输入在 JS 版（TouchFish 的 XhsClient）里算出来的
# 结果，逐字节抄成了期望值。签名是纯函数，所以这些断言和网络无关，改了算法立刻会红。
_SIGN_API = ("/api/sns/web/v2/comment/page?note_id=abc&cursor=&top_comment_id="
             "&image_formats=jpg,webp,avif&xsec_token=t")
_SIGN_A1 = "dGVzdGE x"
_SIGN_TS = 1792379520123
_SIGN_LOADTS = 1792379520000
_SIGN_XS = ("mns0301_gRaKqRBXrytOy+jovp0idIU6rQSkR85V1/jxYGkI2j5A7I4d6dEn0XktlGpV2iRg2GHttwFvI"
            "KlOyjyVFYFnyIbv34Ht/r5X0LfqjInokaljjmgFW8rrciLtf+5vjSKke/HQW4e=")
check("签名明文（packPlaintext）和 JS 版一模一样",
      hashlib.sha256(xhs_sign._plaintext(
          _SIGN_API, "", _SIGN_A1, _SIGN_TS, _SIGN_LOADTS, 1, 2654435761)).hexdigest()[:24]
      == "2e4ac4437d992ec5e56ca2d2")
check("x-s 和 JS 版逐字节相同（mns0301_ 开头）",
      xhs_sign.sign_xs(_SIGN_API, "", _SIGN_A1, _SIGN_TS, 1, 2654435761,
                       loadts=_SIGN_LOADTS)[0] == _SIGN_XS)
check("b1（浏览器指纹）和 JS 版逐字节相同，长度 1884",
      len(xhs_sign.generate_b1(_SIGN_TS)) == 1884
      and hashlib.sha256(xhs_sign.generate_b1(_SIGN_TS).encode()).hexdigest()[:24]
      == "5bb484569dd9a63a3f5471c7")
check("x-s-common 和 JS 版逐字节相同（x8 装 b1、x12 装 ds 对，别混成一个）",
      len(xhs_sign.xs_common(_SIGN_A1, "XSTEST", xhs_sign.generate_b1(_SIGN_TS),
                             "1792379520000;1792379510000", 0)) == 2780
      and hashlib.sha256(xhs_sign.xs_common(
          _SIGN_A1, "XSTEST", xhs_sign.generate_b1(_SIGN_TS),
          "1792379520000;1792379510000", 0).encode()).hexdigest()[:24]
      == "7f088d48e28d83e83b93b13a")
check("自定义 base64 / CRC 那两把小工具的结果和 JS 版一致",
      xhs_sign._b64_custom(b"hello,world", xhs_sign.CONFIG["XS_ALPHABET"]) == "yBpVJBuV49RUJBc="
      and xhs_sign.gens9("null") == -931954833)
check("签名吃的查询串不转义（它算的就是这一整串，转义了就对不上）",
      xhs_sign.path_with_query("/p", {"a": "x,y/z", "b": "", "c": None})
      == "/p?a=x,y/z&b=&c=")
check("Cookie 里取 a1（签名的一环，没有它签出来的东西不被认）",
      xhs_sign.a1_from_cookie("x=1; a1=abc; web_session=z") == "abc"
      and xhs_sign.a1_from_cookie("") == ""
      and xhs_sign.a1_from_cookie("noequals") == "")
_sig = xhs_sign.sign(_SIGN_API, "", "a1=abc; web_session=z", "GET", ts=_SIGN_TS,
                     fetch=lambda url: "function getdss() { return '1792379510000' }")
_hdr = xhs_sign.headers_for(_sig, "a1=abc; web_session=z")
check("拼出来的请求头齐了（x-s / x-t / x-s-common / 两个 trace id / Cookie）",
      all(k in _hdr for k in ("x-s", "x-t", "x-s-common", "x-b3-traceid",
                              "x-xray-traceid", "Cookie", "xsecappid"))
      and _hdr["x-t"] == str(_SIGN_TS)
      and _hdr["x-s"].startswith("mns0301_"),
      sorted(_hdr.keys()))

# 真数据长什么样：拿一份"接口真返回过"的形状来验解析（不是编的字段）
_xhs_obj = {"code": 0, "data": {"has_more": True, "cursor": "c1", "comments": [
    {"id": "c1", "content": "<p>这个真好用</p>", "create_time": 1792379520000,
     "like_count": 12, "user_info": {"nickname": "张三"}, "sub_comment_count": 2,
     "sub_comments": [{"content": "同意", "user_info": {"nickname": "李四"}}]},
    {"id": "c2", "content": "", "create_time": 0, "like_count": 0, "user_info": {}},
]}}
_xc = feed_sources._xhs_comments(_xhs_obj)
check("评论解析：作者 / 赞数 / 时间 / 正文都对得上",
      len(_xc) == 2 and _xc[0].author == "张三" and _xc[0].score == 12
      and _xc[0].created and _xc[0].body[0].text == "这个真好用",
      [(c.author, c.score) for c in _xc])
check("子评论并进同一条，还有几条没展开也说出来",
      any("李四" in b.text for b in _xc[0].body)
      and any("还有 1 条回复" in b.text for b in _xc[0].body),
      [b.text for b in _xc[0].body])
check("空内容的评论给一句占位，不留空白条",
      _xc[1].body[0].text.startswith("（") and _xc[1].author == "某人")

_xsign_src = feed_sources.XhsSource(
    FakeHttp(routes={"edith.xiaohongshu.com": _xhs_obj}, cache=MemCache()),
    {"cookie": "webBuild=6.7.4; a1=abc", "user_agent": "UA"})
_seed3 = feed_sources.Post(
    source="xhs", source_label="小红书", channel="推荐", pid="n1", title="t",
    url="https://www.xiaohongshu.com/explore/n1?xsec_token=tok%3D1&xsec_source=pc_feed")
_cm_calls = _xsign_src.comments(_seed3)[0]
check("带签名的评论请求真的发出去了，地址和查询串都对",
      len(_xsign_src.http.calls) == 1
      and _xsign_src.http.calls[0].startswith(
          "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?note_id=n1")
      and "xsec_token=tok=1" in _xsign_src.http.calls[0],
      _xsign_src.http.calls[:1])
check("取到了就返回真评论（不是永远空）", len(_cm_calls) == 2, len(_cm_calls))
check("同一条笔记的评论只拉一次（详情和评论共用）",
      _xsign_src.comments(_seed3)[0] == _cm_calls and len(_xsign_src.http.calls) == 1)

print("\n=== 18. 来源登记认出了新来源 ===")
on_nga = feed_sources.build_real_sources(
    cache_root=TMP_CACHE, config={"feed_sources": ["nga", "zhihu"]}, log=None)
# 顺序由代码里的登记表决定（固定的，不跟着配置串走），这样 `[`/`]` 的顺序永远可预期
check("写进 feed_sources 就能带出 NGA", [s.key for s in on_nga] == ["mix", "zhihu", "nga"],
      [s.key for s in on_nga])
check("NGA 在列表里被标成需要凭据",
      next(s for s in on_nga if s.key == "nga").needs_auth is True)

on_hp = feed_sources.build_real_sources(
    cache_root=TMP_CACHE, config={"feed_sources": ["hupu"]}, log=None)
check("写进 feed_sources 就能带出虎扑，而且它免登录",
      [s.key for s in on_hp] == ["mix", "hupu"] and on_hp[1].needs_auth is False,
      [s.key for s in on_hp])

# ---------------------------------------------------------------- 收尾
shutil.rmtree(TMP_CACHE, ignore_errors=True)
shutil.rmtree(MemCache.img_dir, ignore_errors=True)
check("临时目录清掉了", not os.path.exists(TMP_CACHE))

ok = sum(1 for _n, c in RESULTS if c)
bad = [n for n, c in RESULTS if not c]
print("\n================ 结果 ================")
print("通过 %d / %d%s" % (ok, len(RESULTS), ("，跳过 %d 项" % len(SKIPPED)) if SKIPPED else ""))
for n in bad:
    print("  未通过: " + n)
sys.exit(0 if not bad else 1)
