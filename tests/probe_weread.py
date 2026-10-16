# -*- coding: utf-8 -*-
"""微信读书那层的探针：**零网络**（夹具驱动 + requests 换成抛异常）。

    python tests/probe_weread.py          # 在 StealthReader 目录下跑

验的是 `weread.py`：书架怎么合、章节怎么排、进度怎么读、错误怎么说人话、
没 Cookie 时是不是老老实实说"没配"。

**正文那条路不在探针里，也不该被"顺手"补上**：微信读书的正文要绕过它自己那套
签名 + 校验（`e_*`/`t_*`）才拿得到，那是它专门用来防程序取正文的，不做。
原因写在 `weread.py` 开头。
"""
import json
import os
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
requests.Session.get = requests.Session.post = lambda *a, **k: 1 / 0

import feed_net                                                          # noqa: E402
import weread                                                            # noqa: E402
from feed_net import FeedNetError                                        # noqa: E402

RESULTS = []
TMP = os.path.join(os.environ.get("TEMP", "."), "sr_probe_weread")


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("  ok  " if cond else "  FAIL") + " " + name + (("   << " + str(extra)) if extra else ""))


def fixture(name):
    path = os.path.join(FIX, name)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class FakeHttp(object):
    """读夹具的替身 Http（`posts` 记下每次 POST 的 url 和 body）。"""

    def __init__(self, routes=None, posts=None, cache=None):
        self.routes = routes or {}
        self.posts = posts if posts is not None else {}
        self.cache = cache
        self.calls = []
        self.served_stale = False

    def _find(self, url):
        for frag, obj in self.routes.items():
            if frag in url:
                return json.loads(json.dumps(obj))
        raise FeedNetError("接口不存在（404）")

    def get_json(self, url, ttl=None, what="", headers=None, timeout=None):
        self.calls.append(("GET", url))
        return self._find(url)

    def post_json(self, url, payload, ttl=None, what="", headers=None, timeout=None):
        self.calls.append(("POST", url))
        self.posts[url] = payload
        return self._find(url)


class StubResp(object):
    """`feed_net.Http._post` 的替身返回值（只要 .json() / .status_code / .text / .content）。"""

    def __init__(self, obj):
        self._obj = obj
        self.status_code = 200
        self.text = "{}"
        self.content = b"{}"

    def json(self):
        return self._obj


class PostHttp(feed_net.Http):
    """用来验"POST 的缓存键带上了 payload"：两本不同的书不能互相串。"""

    def __init__(self, *a, **k):
        feed_net.Http.__init__(self, *a, **k)
        self.calls = []

    def _post(self, url, payload, headers=None, timeout=None):
        self.calls.append((url, json.dumps(payload, sort_keys=True)))
        bid = str((payload.get("bookIds") or [""])[0])
        return StubResp({"data": [{"bookId": bid, "updated": [
            {"chapterUid": 1, "chapterIdx": 1, "title": "《%s》的第一章" % bid,
             "wordCount": 5, "paid": 0, "level": 1}]}]})


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

shelf_fx = fixture("weread_shelf.json")
chs_fx = fixture("weread_chapters.json")
prg_fx = fixture("weread_progress.json")
unauth_fx = fixture("weread_unauth_shelf.json")

# ---------------------------------------------------------------- 1. 书架
print("=== 1. 书架：books + bookProgress 合成一条条 ===")
if not (shelf_fx and chs_fx and prg_fx):
    print("  跳过：缺夹具，先跑 tests/record_fixtures.py weread")
else:
    http = FakeHttp({"/web/shelf/sync": shelf_fx, "/web/book/chapterInfos": chs_fx,
                     "/web/book/getProgress": prg_fx})
    wr = weread.Weread(http, {"cookie": "wr_vid=1"})
    books = wr.shelf()
    check("书架 3 本（夹具里就是 3 本）", len(books) == 3, len(books))
    first = books[0]
    check("最近读的排最前（按 updateTime 降序）",
          [b["updated"] for b in books] == sorted([b["updated"] for b in books], reverse=True),
          [b["title"][:8] for b in books])
    check("第一本合上了进度 89%", first["progress"] == 89, first["progress"])
    check("第一本合上了读到第 10 章", first["chapter_idx"] == 10, first["chapter_idx"])
    check("第一本合上了当前章 uid", first["chapter_uid"] == 10, first["chapter_uid"])
    check("阅读时长是秒（5649）", first["reading_time"] == 5649, first["reading_time"])
    check("书名/作者都在", bool(first["title"]) and bool(first["author"]), first["author"])
    check("finished 是布尔（不是 0/1 原样丢出去）",
          all(isinstance(b["finished"], bool) for b in books))
    check("每本都有网页版阅读地址",
          all(b["url"].startswith("https://weread.qq.com/web/reader/") for b in books),
          books[0]["url"][-14:])
    check("0% 的书也有进度记录（chapterUid 3）",
          any(b["progress"] == 0 and b["chapter_uid"] == 3 for b in books))
    # 已读完那本：把夹具里一个字段改成 1（形状是真的，值动一个）
    done_fx = json.loads(json.dumps(shelf_fx))
    done_fx["books"][0]["finishReading"] = 1
    wr2 = weread.Weread(FakeHttp({"/web/shelf/sync": done_fx}), {"cookie": "x=1"})
    check("finishReading=1 时 finished 是 True", wr2.shelf()[0]["finished"] is True)
    empty = weread.Weread(FakeHttp({"/web/shelf/sync": {"books": [], "bookProgress": []}}),
                          {"cookie": "x=1"})
    check("书架是空的时候返回空列表（不炸）", empty.shelf() == [])
    noname = weread.Weread(FakeHttp({"/web/shelf/sync": {"books": [{}, {"bookId": "A"}]}}),
                           {"cookie": "x=1"})
    check("没 bookId 的条目被丢掉（只留 A）", [b["id"] for b in noname.shelf()] == ["A"])

    # ------------------------------------------------------------ 2. 章节
    print("\n=== 2. 章节表 ===")
    chs = wr.chapters("CB_3PE3bc3eR5Gy74S75d2s9C27")
    check("章节 4 条（夹具里留了前 3 章 + 当前读到那章）", len(chs) == 4, len(chs))
    check("按 chapterIdx 排好", [c["idx"] for c in chs] == sorted(c["idx"] for c in chs),
          [c["idx"] for c in chs])
    check("带上了当前读到的那一章（uid 10）", any(c["uid"] == 10 for c in chs))
    check("字段都是数（uid/idx/words/paid）",
          all(isinstance(c[k], int) for c in chs for k in ("uid", "idx", "words", "paid")))
    check("标题和字数读出来了", bool(chs[0]["title"]) and chs[0]["words"] > 0,
          "%s / %s 字" % (chs[0]["title"][:10], chs[0]["words"]))
    check("POST 的 body 是 {\"bookIds\": [这本书]}",
          http.posts.get("https://weread.qq.com/web/book/chapterInfos") ==
          {"bookIds": ["CB_3PE3bc3eR5Gy74S75d2s9C27"]}, http.posts)
    check("章节表是空的也不炸（data=[]）",
          weread.Weread(FakeHttp({"/web/book/chapterInfos": {"data": []}}),
                        {"cookie": "x=1"}).chapters("B") == [])

    # ------------------------------------------------------------ 3. 进度
    print("\n=== 3. 读到哪了 ===")
    pr = wr.progress("CB_3PE3bc3eR5Gy74S75d2s9C27")
    check("进度 89%", pr["progress"] == 89, pr["progress"])
    check("当前章 uid=10 / idx=10", (pr["chapter_uid"], pr["chapter_idx"]) == (10, 10))
    check("它给的\"停在这一行\"读出来了", bool(pr["summary"]), pr["summary"][:24])
    check("summary 是裁过的（≤60 字）", len(pr["summary"]) <= 60, len(pr["summary"]))
    check("累计阅读时长读出来了", pr["reading_time"] == 5649, pr["reading_time"])
    check("返回体里没有 book 时给全 0，不炸",
          weread.Weread(FakeHttp({"/web/book/getProgress": {}}),
                        {"cookie": "x=1"}).progress("B")["progress"] == 0)

# ---------------------------------------------------------------- 4. 没 Cookie
print("\n=== 4. 没 Cookie 就老实说没配（而且一个包都不发）===")
bare_http = FakeHttp({"/web/shelf/sync": shelf_fx or {}})
bare = weread.Weread(bare_http, {})
check("available 是 False", bare.available is False)
try:
    bare.shelf()
    check("没 Cookie 调 shelf 会抛错", False, "没抛")
except FeedNetError as e:
    check("没 Cookie 调 shelf 会抛错，且话里点明 Cookie", "Cookie" in str(e), str(e)[:40])
check("而且没有发过任何请求", bare_http.calls == [], bare_http.calls)

# ---------------------------------------------------------------- 5. 错误说人话
print("\n=== 5. 200 里带错误码也能说人话 ===")
if not unauth_fx:
    print("  跳过：缺 weread_unauth_shelf.json")
else:
    check("未登录夹具里确实是 -2010", unauth_fx.get("errCode") == -2010, unauth_fx.get("errCode"))
    dead = weread.Weread(FakeHttp({"/web/shelf/sync": unauth_fx}), {"cookie": "wr_vid=1"})
    try:
        dead.shelf()
        check("Cookie 过期时抛错", False, "没抛")
    except FeedNetError as e:
        check("Cookie 过期时说\"要登录\"并要求重贴", "要登录" in str(e) and "Cookie" in str(e),
              str(e)[:44])
# 真站上抄下来的另一条错误（/web/book/search 参数不对时就是这个形状）
real_shape = {"errCode": -2003, "errMsg": "参数格式错误"}
bad = weread.Weread(FakeHttp({"/web/shelf/sync": real_shape}), {"cookie": "x=1"})
try:
    bad.shelf()
    check("别的错误码也抛错", False, "没抛")
except FeedNetError as e:
    check("别的错误码照样抄出它自己的 errCode", "-2003" in str(e), str(e)[:40])

# ---------------------------------------------------------------- 6. POST 缓存键
print("\n=== 6. 两本书的章节不会串（POST 的缓存键带上了 payload）===")
ph = PostHttp(cache=feed_net.Cache(os.path.join(TMP, "cache_%d" % time.time())))
wrp = weread.Weread(ph, {"cookie": "x=1"})
a1 = wrp.chapters("bookA")
a2 = wrp.chapters("bookA")
b1 = wrp.chapters("bookB")
check("A 的章节是 A 的", a1[0]["title"] == "《bookA》的第一章", a1[0]["title"])
check("B 的章节是 B 的（没拿到 A 的缓存）", b1[0]["title"] == "《bookB》的第一章", b1[0]["title"])
check("同一本第二次走缓存（只发了 2 次请求）", len(ph.calls) == 2, len(ph.calls))
check("第二次拿到的跟第一次一样", a2 == a1)

# ---------------------------------------------------------------- 7. 建客户端
print("\n=== 7. 按 feed_auth.json 建客户端 ===")
root = os.path.join(TMP, "root")
os.makedirs(root, exist_ok=True)
check("目录下没有 feed_auth.json 时 available=False",
      weread.build_weread(cache_root=TMP, root=root).available is False)
with open(os.path.join(root, "feed_auth.json"), "w", encoding="utf-8") as f:
    json.dump({"weread": {"cookie": "wr_vid=327147803; wr_skey=abc",
                          "user_agent": "UA-TEST"}}, f)
built = weread.build_weread(cache_root=TMP, root=root)
check("配了 Cookie 就 available=True", built.available is True)
check("Cookie 被拆成了 dict（wr_vid / wr_skey）",
      built.http.cookies.get("wr_vid") == "327147803" and built.http.cookies.get("wr_skey") == "abc",
      built.http.cookies)
check("UA 用的是文件里那个", built.http.headers.get("User-Agent") == "UA-TEST")
with open(os.path.join(root, "feed_auth.json"), "w", encoding="utf-8") as f:
    json.dump({"weread": {"user_agent": "UA-TEST"}}, f)
check("文件在但没有 cookie 字段 → available=False",
      weread.build_weread(cache_root=TMP, root=root).available is False)

# ---------------------------------------------------------------- 8. 阅读地址
print("\n=== 8. 网页版阅读地址 ===")
u = weread.Weread.reader_url({"deepLink": "https://weread.qq.com/book-detail?type=1&v=abc123def"})
check("从 deepLink 里掏出 /web/reader/<v>", u == "https://weread.qq.com/web/reader/abc123def", u)
check("没有 deepLink 就退回书架页（不给个打不开的地址）",
      weread.Weread.reader_url({}) == "https://weread.qq.com/web/shelf")
check("deepLink 是空串也退得回去",
      weread.Weread.reader_url({"deepLink": ""}).endswith("/web/shelf"))

# ---------------------------------------------------------------- 9. 不做正文
print("\n=== 9. 这条线里没有正文（也不该有）===")
check("Weread 上没有 content/chapter 取正文的方法",
      not any(hasattr(weread.Weread, n) for n in
              ("content", "chapter_content", "read", "chapter_text", "body")))
src = open(os.path.join(SR, "weread.py"), encoding="utf-8").read()
check("源码里没有去取正文的路径（文档里解释为什么不做不算）",
      not any(s in src for s in ('"/web/book/chapter/e_', '"/web/book/chapterContent"',
                                 '"/web/book/read"', "'/web/book/chapter/e_")))
check("源码里写清了为什么不做（防程序取正文）", "防" in src and "版权" in src)

# ---------------------------------------------------------------- 汇总
bad = [n for n, ok in RESULTS if not ok]
print("\n=== 汇总 ===")
print("一共 %d 项，失败 %d 项" % (len(RESULTS), len(bad)))
for n in bad:
    print("  FAIL " + n)
sys.exit(1 if bad else 0)
