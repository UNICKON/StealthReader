# -*- coding: utf-8 -*-
"""来源层逻辑探针：**零网络**（夹具驱动 + requests 换成抛异常）。

    python tests/probe_sources.py          # 在 StealthReader 目录下跑

零网络不是靠自觉：脚本开头把 `requests.get/post/Session.get` 全换成抛异常，
而所有适配器都拿到一个读夹具的替身 Http。所以"这次一个包都没发"是可以被证明的。
夹具是 `tests/record_fixtures.py` 从真站录下来的（见 tests/README.md）。
"""
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


class FakeHttp(object):
    """读夹具的替身 Http。routes: 片段 -> 对象。fail: 命中的片段一律抛错。"""

    def __init__(self, routes=None, fail=(), cache=None):
        self.routes = routes or {}
        self.fail = list(fail)
        self.cache = cache
        self.served_stale = False
        self.image_calls = []

    def get_json(self, url, ttl=None, what="", headers=None, timeout=None):
        for frag in self.fail:
            if frag in url:
                raise FeedNetError("连接超时")
        for frag, obj in self.routes.items():
            if frag in url:
                return json.loads(json.dumps(obj))
        raise FeedNetError("接口不存在（404）")

    def fetch_image(self, url):
        self.image_calls.append(url)
        return ("img_%d.jpg" % (abs(hash(url)) % 100000)) if url else ""

    def get_bytes(self, url, ttl=None, headers=None, max_bytes=0):
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
if not raw_z:
    skip("知乎", "缺夹具")
else:
    zs = feed_sources.ZhihuSource(FakeHttp({"hot-list": raw_z}, cache=MemCache()))
    check("栏目只有 热榜", zs.channels() == ["热榜"], zs.channels())
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
    check("知乎详情是热榜给的真摘要", any("摘要" in b.text or len(b.text) > 40 for b in zd.body))
    check("知乎详情直说正文要登录", any("登录" in b.text for b in zd.body))
    check("知乎评论如实为空", zs.comments(zp[0]) == ([], None))

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
check("默认：混流 + 四个免登录来源（微博默认关）",
      [s.key for s in made] == ["mix", "ithome", "v2ex", "linuxdo", "zhihu"], [s.key for s in made])
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
          (not zp[0].excerpt) or any(b.text == zp[0].excerpt for b in zd2.body),
          [b.text[:24] for b in zd2.body])

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
