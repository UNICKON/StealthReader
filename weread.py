# -*- coding: utf-8 -*-
"""微信读书：我的书架、章节表、读到哪了、划线、书签。

**只做它公开给浏览器的那几个接口**（带上你自己的 Cookie）：

| 接口 | 拿到什么 |
| --- | --- |
| `GET  /web/shelf/sync` | 书架：书名/作者/封面，`bookProgress` 里还有每本读到哪 |
| `POST /web/book/chapterInfos` | 章节表：`chapterUid` / 标题 / 字数 / 是否付费 |
| `GET  /web/book/getProgress` | 某本读到哪：进度、当前章，连"停在那一行"的原文都有 |

**这两个接口试过，但代码里没有**（不留"看起来能用"的死代码）：

- `underlines`（划线）拿到的是一条条 **range**（实测形状 `{"range":"429-439","count":0,"type":0}`），
  要把它变成能读的文字就得先有**正文**——而正文那条路正是下面说不能走的那条，
  所以划线只做了一半，不做；
- `bookmarklist`（书签）：整个书架 50 本一本都没有，没有真形状就不写解析。

**为什么这里没有"正文"这一层**（写在这里，免得以后有人"顺手"加上）：

正文走的是 `POST /web/book/chapter/e_0`（还有 `e_1`/`e_2`/`e_3`/`t_0`/`t_1`）那一套：
请求要带它前端自己算出来的 `s` 签名（`sign` = 把参数排序拼串后跑一个自研散列），
响应还得先按 `md5(内容) + 内容` 的格式校验（`chk`）、再解一层字符混淆才拿得到文字——
这两样是它**专门用来防止程序取正文**的保护措施（`e_*`/`t_*` 这组名字和那层校验就是干这个的）。
照着移植过去，等于把受版权保护的书批量下载到本地，这件事不做。
所以这个模块给你的是"我的书架 + 读到哪了 + 划了哪些线"；正文请按 `O` 去网页版读，
进度、划线这些在那边是通的，读起来也跟平时一模一样。

凭据跟社区来源共用 `feed_auth.json`（读法也共用一份实现，免得两处对"文件坏了怎么办"
的判断跑偏）：

    {"weread": {"cookie": "wr_vid=...; wr_skey=...", "user_agent": "..."}}

`tests/import_touchfish_cookies.py --apply` 能从 VS Code 的摸鱼插件里把它搬过来。
"""

import os
import re
from urllib.parse import urlencode

import feed as feed_mod
import feed_net
from feed_net import FeedNetError, Http, TTL_LIST, TTL_DETAIL
from feed_sources import load_auth

BASE_URL = "https://weread.qq.com"
LIST_TIMEOUT = 8
DETAIL_TIMEOUT = 8


def _reason(obj):
    """微信读书连 200 都可能在体里报错（`errCode`/`errMsg`），这句把它翻成人话。"""
    err = obj.get("errCode")
    if not err:
        return ""
    msg = str(obj.get("errMsg") or "").strip()
    if int(err) == -2010:
        return "要登录（它说：%s）——Cookie 过期了，重新贴一次" % (msg or "用户不存在")
    return "它回了 %s%s" % (err, ("（" + msg + "）") if msg else "")


class Weread(object):
    """一个带 Cookie 的微信读书客户端。没有 Cookie 时不装作能用（`available` 为 False）。"""

    def __init__(self, http, cred=None):
        self.http = http
        self.cred = cred if isinstance(cred, dict) else {}
        self.available = bool(self.cred.get("cookie"))

    # -- 取数 -------------------------------------------------------------
    def _get(self, path, params=None, ttl=TTL_LIST, what=""):
        url = BASE_URL + path + (("?" + urlencode(params)) if params else "")
        obj = self.http.get_json(url, ttl=ttl, what=what, timeout=LIST_TIMEOUT)
        reason = _reason(obj if isinstance(obj, dict) else {})
        if reason:
            raise FeedNetError(reason)
        return obj

    def _post(self, path, payload, ttl=TTL_DETAIL, what=""):
        obj = self.http.post_json(BASE_URL + path, payload, ttl=ttl, what=what,
                                  timeout=DETAIL_TIMEOUT)
        reason = _reason(obj if isinstance(obj, dict) else {})
        if reason:
            raise FeedNetError(reason)
        return obj

    def _need_login(self):
        if not self.available:
            raise FeedNetError("没配微信读书的 Cookie：把浏览器 Cookie 放进 feed_auth.json 的 "
                               "weread 里（或者跑 tests/import_touchfish_cookies.py --apply）")

    # -- 书架 -------------------------------------------------------------
    @staticmethod
    def reader_url(book):
        """网页版阅读地址。它给的是 `/book-detail?...&v=<hash>`，阅读页就是 `/web/reader/<hash>`。"""
        deep = str((book or {}).get("deepLink") or "")
        m = re.search(r"[?&]v=([0-9a-zA-Z]+)", deep)
        if m:
            return "%s/web/reader/%s" % (BASE_URL, m.group(1))
        return BASE_URL + "/web/shelf"

    def shelf(self):
        """书架。把 `books`（书名/作者/封面）和 `bookProgress`（进度/读到哪章）合成一条条。"""
        self._need_login()
        data = self._get("/web/shelf/sync", ttl=TTL_LIST, what="微信读书书架")
        prog = {}
        for p in (data.get("bookProgress") or []):
            if isinstance(p, dict) and p.get("bookId"):
                prog[str(p["bookId"])] = p
        out = []
        for b in (data.get("books") or []):
            if not isinstance(b, dict) or not b.get("bookId"):
                continue
            bid = str(b["bookId"])
            p = prog.get(bid) or {}
            out.append({
                "id": bid,
                "title": str(b.get("title") or "").strip(),
                "author": str(b.get("author") or "").strip(),
                "cover": str(b.get("cover") or ""),
                "progress": max(0, min(100, int(p.get("progress") or 0))),
                "chapter_uid": int(p.get("chapterUid") or 0),
                "chapter_idx": int(p.get("chapterIdx") or 0),
                "reading_time": int(p.get("readingTime") or 0),
                "updated": feed_mod.parse_time(p.get("updateTime") or b.get("readUpdateTime")),
                "finished": bool(b.get("finishReading")),
                "url": self.reader_url(b),
            })
        # 最近读的排前面：书架本来就是"我上次读到哪"，不是"按标题排"
        out.sort(key=lambda x: (-x["updated"], x["title"]))
        return out

    def chapters(self, book_id):
        """章节表。`chapterInfos` 是 POST，缓存键带上了 bookIds（不然两本书会串）。"""
        data = self._post("/web/book/chapterInfos", {"bookIds": [str(book_id)]},
                          what="微信读书章节表")
        one = (data.get("data") or [{}])
        one = one[0] if one and isinstance(one[0], dict) else {}
        out = []
        for c in (one.get("updated") or []):
            if not isinstance(c, dict):
                continue
            out.append({
                "uid": int(c.get("chapterUid") or 0),
                "idx": int(c.get("chapterIdx") or 0),
                "title": str(c.get("title") or "").strip(),
                "words": int(c.get("wordCount") or 0),
                "paid": int(c.get("paid") or 0),
                "level": int(c.get("level") or 0),
            })
        out.sort(key=lambda x: x["idx"])
        return out

    def progress(self, book_id):
        """某本读到哪。`summary` 是它给的"停在这一行"的原文（有就显示，没有就算了）。"""
        data = self._get("/web/book/getProgress", {"bookId": str(book_id)},
                         ttl=TTL_LIST, what="微信读书进度")
        b = data.get("book") if isinstance(data.get("book"), dict) else {}
        return {
            "progress": max(0, min(100, int(b.get("progress") or 0))),
            "chapter_uid": int(b.get("chapterUid") or 0),
            "chapter_idx": int(b.get("chapterIdx") or 0),
            "summary": str(b.get("summary") or "").strip(),
            "reading_time": int(b.get("readingTime") or 0),
            "updated": feed_mod.parse_time(b.get("updateTime")),
        }

def build_weread(cache_root=None, root=None, log=None):
    """按 `feed_auth.json` 里的 `weread` 建一个客户端（没配也照样建，`available` 会是 False）。"""
    root = root or os.path.dirname(os.path.abspath(__file__))
    cred = load_auth(os.path.join(root, "feed_auth.json")).get("weread")
    cred = cred if isinstance(cred, dict) else {}
    cache = feed_net.Cache(os.path.join(cache_root, "weread")) if cache_root else None
    http = Http(cache=cache, timeout=LIST_TIMEOUT,
                cookies=None,
                user_agent=cred.get("user_agent") or feed_net.UA,
                headers={"Referer": BASE_URL + "/"})
    if cred.get("cookie"):
        http.cookies = _cookies_from(cred["cookie"])
    return Weread(http, cred)


def _cookies_from(text):
    """把浏览器里复制来的 "a=1; b=2" 解析成 dict（跟社区那边同一套写法）。"""
    out = {}
    for part in str(text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out
