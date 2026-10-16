# -*- coding: utf-8 -*-
"""社区统一流的真实来源适配器（批次 B / D / E）。

每个来源只做三件事：列表 / 正文 / 评论，全部返回 feed.py 的 Post / Comment。
界面层一行都不用改——这就是批次 A 定下的契约。

当前来源：
- **IT之家**：免登录，本机实测可用（移动端接口 + 正文接口）；
- **V2EX**：免登录的公开 API，但**本机网络到不了**（TCP 443 超时，见 README），代码按官方
  文档写、用夹具验；网络恢复即可用；
- **linux.do**：标准 Discourse，站前挂着 Cloudflare 盾，**必须贴浏览器 Cookie**
  （含 cf_clearance，且 UA 要一致）才能过；
- **知乎**：免登录只能拿热榜（移动端 `api.zhihu.com/topstory/hot-list`），正文/评论要登录；
- **微博**：免登录只能拿热搜榜，正文/评论要登录（**默认关闭**，属于"单独开关"那一档）；
- **混流**：把上面几个合页成一条，"混流为主"就靠它。
另有 feed.py 里的 DemoSource（离线示例）垫底。

Cookie 从应用目录下的 feed_auth.json 读（已 gitignore），格式：
    {"linuxdo": {"cookie": "...", "user_agent": "..."}, "v2ex": {"cookie": "..."}}
批次 C 会把它换成 DPAPI 加密 + 设置界面，这里先按最小可用做。
"""

import json
import os
import re
import threading
import time
from html import escape as html_escape
from urllib.parse import quote

import feed as feed_mod
from feed import Block, Comment, Post, SourceAdapter, BLOCK_IMG, BLOCK_P, BLOCK_QUOTE
import feed_net
from feed_net import FeedNetError, Http, TTL_LIST, TTL_DETAIL, TTL_FOREVER

PAGE_MIX = 12           # 混流每页几条
MAX_PAGES = 6           # 单个来源最多自动续几页（别把"快到底自动续页"变成无限请求）
LIST_TIMEOUT = 6        # 列表页的超时：比正文短，因为混流要等所有来源，一个卡住就整页卡住
COVER_PER_PAGE = 6      # 一页最多下几张封面：25 张图一起下会让"唤出社区页"等十几秒
FAIL_COOLDOWN = 60      # 某个来源刚失败过，就歇这么久再去碰它（别每翻一页都去撞一次南墙）


def load_auth(path):
    """读 feed_auth.json。读不到/格式不对都返回空字典（来源会退化成"未配置"，不炸）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class RealSource(SourceAdapter):
    """真实来源的公共部分：错误信息、图片目录、把异常收成一句人话。"""

    needs_auth = False
    base_url = ""

    def __init__(self, http, auth=None):
        self.http = http
        # 浏览器里复制来的 {"cookie": "...", "user_agent": "..."}（来自 feed_auth.json）。
        # 来源自己决定怎么用；没配就是空字典，"未配置"是一条正常状态，不是错误。
        self.auth = dict(auth or {})
        self.last_error = ""
        self.served_stale = False      # 这次给的是不是"断网时顶上的旧缓存"

    # -- 凭据 -------------------------------------------------------------
    @property
    def has_auth(self):
        return bool(str(self.auth.get("cookie") or "").strip())

    def auth_state(self):
        """给界面看的四个字：**配了没配**比"这个站要不要登录"更有用。

        知乎的列表免登录、正文要登录，所以"要不要"是一个来源内部就分层的概念；
        而"你贴没贴 Cookie"永远是当前状态，页脚/详情里要说的就是它。"""
        if self.has_auth:
            return "Cookie 已配置"
        return "Cookie 未配置（这个来源要登录）" if self.needs_auth else "免登录"

    def auth_note(self):
        """配了没配、配了管不管用——详情页里那句实话要用这个拼。"""
        if not self.has_auth:
            return "（这个站的正文/评论要登录，可以在应用目录的 feed_auth.json 里贴一次 Cookie）"
        return "（已经贴了 Cookie，但接口这次没给——见下面的原文）"

    # -- 公共 -------------------------------------------------------------
    def image_base(self):
        return self.http.cache.img_dir if self.http.cache else ""

    def _get(self, url, ttl=TTL_LIST, what="", timeout=None):
        try:
            obj = self.http.get_json(url, ttl=ttl, what=what, timeout=timeout)
            self.last_error = ""
            self.served_stale = bool(getattr(self.http, "served_stale", False))
            return obj
        except FeedNetError as e:
            self.last_error = str(e)
            raise
        except Exception as e:                      # 适配器自己的解析错也算"这个来源挂了"
            self.last_error = "解析失败（%s）" % type(e).__name__
            raise FeedNetError(self.last_error)

    def _cover(self, url):
        """封面图落地；失败就是没有封面（列表里少一张图，不影响别的）。"""
        if not url:
            return ""
        try:
            return self.http.fetch_image(url)
        except Exception:
            return ""

    def _localize_images(self, blocks, limit=12):
        """正文里的图也要落到本地：Qt 的 QTextDocument 读不了 http。

        **落不下来的图直接丢掉**（而不是留个 http 地址）：渲染层只会写出 <img src="...">，
        留 http 只会让正文里出现一排裂图标。宁可少几张图，不要一片坏图。"""
        out = []
        n = 0
        for b in (blocks or []):
            if b.kind != BLOCK_IMG:
                out.append(b)
                continue
            name = self._cover(b.url or b.text) if n < limit else ""
            if name:
                b.url = name
                n += 1
                out.append(b)
        return out


class IthomeSource(RealSource):
    """IT之家：免登录。列表走移动端接口（带封面和摘要），正文走 api 的 newscontent。"""

    key = "ithome"
    label = "IT之家"
    base_url = "https://www.ithome.com"
    CHANNELS = ("热榜", "最新")

    def channels(self):
        return list(self.CHANNELS)

    def _list_url(self, channel, page):
        if channel == "最新":
            return "https://m.ithome.com/api/news/newslistpageget?Tag=new&Page=%d" % page
        return "https://m.ithome.com/api/news/newslistpageget?Tag=hot&Page=%d" % page

    def _raw_items(self, channel, page):
        """拿一页原始条目。移动端接口挂了就退回 api/newslist/news（只有第一页）。"""
        try:
            data = self._get(self._list_url(channel, page), TTL_LIST, "IT之家列表",
                             timeout=LIST_TIMEOUT)
            items = data.get("Result") if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                return items
        except FeedNetError:
            if page > 0:
                raise
        if page > 0:
            return []
        data = self._get("https://api.ithome.com/json/newslist/news", TTL_LIST, "IT之家列表",
                         timeout=LIST_TIMEOUT)
        toplist = (data or {}).get("toplist") or []
        newslist = (data or {}).get("newslist") or []
        if channel == "热榜":
            # api 里的 toplist 往往只有一条（今天的主推），单靠它这一页就废了：
            # 用 toplist 打头 + newslist 补齐，再去重。
            merged, seen = [], set()
            for it in list(toplist) + list(newslist):
                if not isinstance(it, dict):
                    continue
                nid = it.get("newsid")
                if nid in seen:
                    continue
                seen.add(nid)
                merged.append(it)
            return merged
        return newslist or toplist

    def _to_post(self, it, channel, want_cover=False):
        pid = str(it.get("newsid") or it.get("id") or "")
        ts = feed_mod.parse_time(it.get("postdate") or it.get("orderdate"))
        url = it.get("url") or ""
        if url and not url.lower().startswith("http"):
            url = self.base_url + (url if url.startswith("/") else "/" + url)
        return Post(
            source=self.key, source_label=self.label, channel=channel, pid=pid,
            title=feed_mod.strip_html(it.get("title") or ""),
            author=(it.get("newsauthor") or "").strip(),
            created=feed_mod.human_time(ts), ts=ts,
            score=int(it.get("hitcount") or 0),
            score_label="阅读",                 # hitcount 是阅读量，不是赞
            n_comments=int(it.get("commentcount") or 0),
            cover=self._cover(it.get("image")) if want_cover else "",
            excerpt=feed_mod.strip_html(it.get("description") or it.get("subtitle") or ""),
            url=url, tags=[],
        )

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        page = int(cursor or 0)
        items = self._raw_items(channel, page)
        posts = [self._to_post(it, channel, want_cover=(i < COVER_PER_PAGE))
                 for i, it in enumerate(items) if isinstance(it, dict)]
        posts = [p for p in posts if p.id and p.title]
        nxt = None
        if posts and page + 1 < MAX_PAGES:
            nxt = page + 1
        return posts, nxt

    def detail(self, post, cursor=None):
        data = self._get("https://api.ithome.com/json/newscontent/%s" % post.id,
                         TTL_DETAIL, "IT之家正文")
        if not isinstance(data, dict) or not data.get("success", True):
            raise FeedNetError("正文取不到")
        out = Post.from_dict(post.to_dict())
        out.body = self._localize_images(
            feed_mod.blocks_from_html(data.get("detail") or ""), limit=8)
        out.title = feed_mod.strip_html(data.get("title") or out.title)
        out.author = (data.get("newsauthor") or out.author or "").strip()
        ts = feed_mod.parse_time(data.get("postdate"))
        if ts:
            out.ts = ts
            out.created = feed_mod.human_time(ts)
        if data.get("hitcount"):
            out.score = int(data["hitcount"])
        if data.get("commentcount") is not None:
            out.n_comments = int(data["commentcount"])
        if data.get("image"):
            out.cover = self._cover(data["image"]) or out.cover
        tags = []
        for t in (data.get("newstags") or []):
            if isinstance(t, dict) and t.get("keyword"):
                tags.append(str(t["keyword"]))
        out.tags = tags or out.tags
        if data.get("url"):
            u = str(data["url"])
            out.url = u if u.lower().startswith("http") else self.base_url + u
        return out, None

    def comments(self, post, cursor=None):
        # IT之家没有可用的评论接口（2026-10-16 把 dyn/api/m/www 四条路都试过，全 404），
        # 正文页就只有正文。宁可明说没有，也不编评论。
        return [], None


class V2exSource(RealSource):
    """V2EX：官方公开 API（免登录）。注意本机网络到不了 v2ex.com，见 README。"""

    key = "v2ex"
    label = "V2EX"
    base_url = "https://www.v2ex.com"
    CHANNELS = ("最热", "最新")

    def channels(self):
        return list(self.CHANNELS)

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        api = "hot.json" if channel == "最热" else "latest.json"
        data = self._get("%s/api/topics/%s" % (self.base_url, api), TTL_LIST, "V2EX 列表",
                         timeout=LIST_TIMEOUT)
        items = data if isinstance(data, list) else (data or {}).get("result") or []
        posts = []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("id") or "")
            if not pid:
                continue
            ts = feed_mod.parse_time(it.get("created") or it.get("last_modified"))
            member = it.get("member") or {}
            n = int(it.get("replies") or 0)
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=pid,
                title=feed_mod.strip_html(it.get("title") or ""),
                author=(member.get("username") or "") if isinstance(member, dict) else "",
                created=feed_mod.human_time(ts), ts=ts,
                # replies 是回复数、不是赞：别把它同时当 score，否则列表会写"12 赞 · 12 回复"
                score=0, n_comments=n, comment_label="回复",
                excerpt=feed_mod.strip_html(it.get("content") or it.get("content_rendered") or ""),
                url=it.get("url") or ("%s/t/%s" % (self.base_url, pid)),
                tags=list(it.get("tags") or []) if isinstance(it.get("tags"), list) else [],
            ))
        return posts, None            # 官方这两个接口本身就是一整页，没有分页

    def detail(self, post, cursor=None):
        data = self._get("%s/api/topics/show.json?id=%s" % (self.base_url, post.id),
                         TTL_DETAIL, "V2EX 正文")
        items = data if isinstance(data, list) else [data]
        it = items[0] if items and isinstance(items[0], dict) else {}
        out = Post.from_dict(post.to_dict())
        out.body = self._localize_images(feed_mod.blocks_from_html(
            it.get("content_rendered") or it.get("content") or ""), limit=6)
        member = it.get("member") or {}
        if isinstance(member, dict) and member.get("username"):
            out.author = member["username"]
        ts = feed_mod.parse_time(it.get("created"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        tags = it.get("tags")
        if isinstance(tags, list) and tags:
            out.tags = [str(t) for t in tags]
        return out, None

    def comments(self, post, cursor=None):
        data = self._get("%s/api/replies/show.json?topic_id=%s" % (self.base_url, post.id),
                         TTL_LIST, "V2EX 回复")
        items = data if isinstance(data, list) else []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            member = it.get("member") or {}
            ts = feed_mod.parse_time(it.get("created"))
            out.append(Comment(
                cid=str(it.get("id") or len(out)),
                author=(member.get("username") or "") if isinstance(member, dict) else "",
                created=feed_mod.human_time(ts), score=0,
                body=feed_mod.blocks_from_html(it.get("content_rendered") or it.get("content") or "",
                                               max_blocks=40),
            ))
        return out, None           # V2EX 回复是平铺的，没有层级


class DiscourseSource(RealSource):
    """标准 Discourse（linux.do 就是）。同一个类以后能直接接别的 Discourse 站。

    linux.do 前面挂着 Cloudflare 盾：必须贴浏览器 Cookie（含 cf_clearance、UA 要一致）。"""

    key = "linuxdo"
    label = "linux.do"
    base_url = "https://linux.do"
    CHANNELS = ("最新", "热门")

    def channels(self):
        return list(self.CHANNELS)

    def _endpoint(self, channel):
        return "/top.json" if channel == "热门" else "/latest.json"

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        page = int(cursor or 0)
        url = "%s%s?page=%d" % (self.base_url, self._endpoint(channel), page)
        data = self._get(url, TTL_LIST, "linux.do 列表", timeout=LIST_TIMEOUT)
        topics = ((data or {}).get("topic_list") or {}).get("topics") or []
        posts = []
        for i, t in enumerate(topics):
            if not isinstance(t, dict) or not t.get("id"):
                continue
            pid = str(t["id"])
            # 排序/显示都用"最后回复时间"：论坛的帖子热度看的是**还是不是活的**，
            # 拿建帖时间排会让一条今天还在聊的 2019 年老帖沉到最底下（混流里尤其明显）。
            ts = feed_mod.parse_time(t.get("last_posted_at") or t.get("created_at"))
            n = max(0, int(t.get("posts_count") or 1) - 1)
            slug = t.get("slug") or "t"
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=pid,
                title=feed_mod.strip_html(t.get("title") or t.get("fancy_title") or ""),
                author=(t.get("last_poster_username") or ""),
                created=feed_mod.human_time(ts), ts=ts,
                score=int(t.get("like_count") or 0), n_comments=n, comment_label="回复",
                cover=self._cover(t.get("image_url")) if i < COVER_PER_PAGE else "",
                excerpt=feed_mod.strip_html(t.get("excerpt") or ""),
                url="%s/t/%s/%s" % (self.base_url, slug, pid),
                tags=[str(x) for x in (t.get("tags") or [])],
            ))
        posts = [p for p in posts if p.title]
        # API 把置顶公告排在数组最前，而置顶公告的 last_posted_at 可能停在很多年前
        # （meta.discourse.org 那条 "New to Discourse? Start here!" 是 2013）——不重排的话，
        # 单看 linux.do 时最上面永远是一条陈旧公告。混流里它会自然沉下去，这里补上同一条规矩。
        posts.sort(key=lambda p: (p.ts or 0), reverse=True)
        nxt = page + 1 if posts and page + 1 < MAX_PAGES else None
        return posts, nxt

    def _topic(self, pid):
        return self._get("%s/t/%s.json" % (self.base_url, pid), TTL_DETAIL, "linux.do 正文")

    @staticmethod
    def _posts(data):
        return ((data or {}).get("post_stream") or {}).get("posts") or []

    def detail(self, post, cursor=None):
        data = self._topic(post.id)
        ps = self._posts(data)
        out = Post.from_dict(post.to_dict())
        first = ps[0] if ps and isinstance(ps[0], dict) else {}
        out.body = self._localize_images(
            feed_mod.blocks_from_html(first.get("cooked") or ""), limit=6)
        if first.get("username"):
            out.author = first["username"]
        ts = feed_mod.parse_time(first.get("created_at"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        score = first.get("reaction_users_count") or first.get("score")
        if score:
            out.score = int(score)
        if not out.title:
            out.title = feed_mod.strip_html(data.get("title") or "")
        return out, None

    def comments(self, post, cursor=None):
        """Discourse 的回复自带 reply_to_post_number，所以这里能还原出**真正的层级树**。"""
        data = self._topic(post.id)
        ps = [p for p in self._posts(data) if isinstance(p, dict)]
        if len(ps) <= 1:
            return [], None
        by_num = {}
        for p in ps[1:]:
            body = feed_mod.blocks_from_html(p.get("cooked") or "", max_blocks=40)
            score = p.get("reaction_users_count") or p.get("score") or 0
            by_num[p.get("post_number")] = Comment(
                cid=str(p.get("id") or p.get("post_number") or ""),
                author=(p.get("username") or p.get("name") or ""),
                created=feed_mod.human_time(feed_mod.parse_time(p.get("created_at"))),
                score=int(score or 0), body=body,
            )
        roots = []
        for p in ps[1:]:
            c = by_num.get(p.get("post_number"))
            if c is None:
                continue
            parent = by_num.get(p.get("reply_to_post_number"))
            if parent is not None and parent is not c:
                parent.replies.append(c)
            else:
                roots.append(c)
        # 兜底：万一数据里有环（谁回复了后面的人），把还没挂到树上的补到根上，
        # 保证"每条评论都能被看到"这件事不受数据形状影响
        placed = set()

        def mark(nodes):
            for n in nodes:
                placed.add(id(n))
                mark(n.replies)

        mark(roots)
        for c in by_num.values():
            if id(c) not in placed:
                roots.append(c)
        return roots, None


def fair_pages(merged, per_page=PAGE_MIX, cap=0):
    """把时间倒序的合并流切页，**每个来源每页最多 cap 条**，多出来的顺延到下一页。

    为什么需要它：IT之家一分钟一条，其它站几小时一条——纯按时间排的话"混流"第一页
    会被 IT之家整页占满，看着就不是混流。这里给每个来源一个每页席位，谁也不挤掉谁；
    被挤下去的条目只是往后挪一页，**一条都不会丢**（这一点有探针盯着）。
    """
    items = [p for p in (merged or []) if p is not None]
    if not items:
        return []
    if cap <= 0:
        return [items[i:i + per_page] for i in range(0, len(items), per_page)]
    pages = []
    pending = items
    guard = 0
    while pending and guard < 1000:
        guard += 1
        page, rest, counts = [], [], {}
        for p in pending:
            if len(page) >= per_page:
                rest.append(p)
                continue
            sid = getattr(p, "source", "")
            if counts.get(sid, 0) >= cap:
                rest.append(p)              # 这个来源这一页的席位满了，下一轮再来
                continue
            page.append(p)
            counts[sid] = counts.get(sid, 0) + 1
        if not page:                        # 理论上到不了（cap>=1 时每轮至少收一条）
            rest = pending
            page = pending[:per_page]
            rest = pending[per_page:]
        pages.append(page)
        pending = rest
    return pages


class ZhihuSource(RealSource):
    """知乎：列表免登录，正文/评论**贴了 Cookie 就能拿到**（这一点实测过了）。

    2026-10-16 在本机网络把几条路都走了一遍（当时的结论是"只有列表能免登录"）：
      - `api.zhihu.com/topstory/hot-list`（移动端接口）**不看 Cookie**：29 条热榜，
        每条带问题 id / 标题 / 摘要 / 回答数 / 关注数 / 热度文案 / 真原帖地址；
      - 免登录时详情全要签名或登录：`www.zhihu.com/api/v3/...hot-lists` 401「身份未经过验证」、
        `www.zhihu.com/hot` 与 `/billboard` 403（返回 `zh-zse-ck` 让你去算签名）、
        `api.zhihu.com/questions/<id>` 403「请求参数异常，请升级客户端」、
        答案列表 403 风控（试了加 `x-api-version` / App UA 也一样）。

    **贴上浏览器 Cookie 之后结论变了**（2026-10-16 复测）：答案接口
    `/api/v4/questions/<qid>/answers?include=...&limit=1` 直接 200、正文是 `data[0].content` 的 HTML；
    评论接口 `/api/v4/answers/<aid>/comments` 也 200（15 条）。所以之前担心的
    `x-zse-96` 前端签名在这一层**并不需要**——"光有 Cookie 不够"那句猜测已经被实测推翻，
    代码和文档都不再这么说。

    所以现在的分工：列表免登录；**配了 Cookie 才去试回答**，试到了就把回答摆进"评论"那一栏，
    试不到就把原因写在正文里（不编回答、不编评论）。

    **回答就是这一页的"评论"**（真人原话：「知乎显示的应该是评论而不是问题的内容」）。
    知乎的一个问题底下没有"评论"这种东西，只有回答——所以详情页的评论那一栏就是回答列表，
    正文那一栏只留问题自己的信息。以前是把热榜页给的问题摘要当正文摆着，那正是"显示的是问题
    的内容、不是评论"。
    """
    key = "zhihu"
    label = "知乎"
    base_url = "https://www.zhihu.com"
    needs_auth = False
    CHANNELS = ["热榜"]
    LIST_URL = "https://api.zhihu.com/topstory/hot-list?limit=50&reverse_order=0"
    # 一次问这么多条回答：它们就是详情页的"评论"那一栏。10 条足够一屏读，文档也不会太沉。
    ANSWERS_LIMIT = 10
    # 失败只记这么久。以前失败也缓存一辈子，于是第一次撞上风控之后，这个问题到进程结束都打不开。
    RETRY_AFTER = 90

    def __init__(self, http, auth=None):
        RealSource.__init__(self, http, auth)
        self._ans = {}          # 问题 id -> (回答列表, 失败原因, 取到的时刻)：详情和评论共用一次请求

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    @staticmethod
    def _hot(text):
        """把"1327 万热度"读成 13270000。读不出来就给 0（列表里干脆不显示这一项）。"""
        m = re.search(r"([\d.]+)\s*(亿|万)?", str(text or ""))
        if not m:
            return 0
        try:
            v = float(m.group(1))
        except ValueError:
            return 0
        unit = m.group(2)
        if unit == "亿":
            v *= 100000000
        elif unit == "万":
            v *= 10000
        return int(v)

    def list(self, channel=None, cursor=None):
        data = self._get(self.LIST_URL, TTL_LIST, "知乎热榜", timeout=LIST_TIMEOUT)
        items = (data or {}).get("data") or []
        posts = []
        for it in items:
            tgt = it.get("target") if isinstance(it, dict) else None
            if not isinstance(tgt, dict) or not tgt.get("id"):
                continue
            qid = str(tgt["id"])
            ts = feed_mod.parse_time(tgt.get("created"))
            posts.append(Post(
                source=self.key, source_label=self.label, channel="热榜", pid=qid,
                title=feed_mod.strip_html(tgt.get("title") or ""),
                author="",                      # 热榜不告诉你这是谁问的
                created=feed_mod.human_time(ts), ts=ts,
                score=self._hot(it.get("detail_text")), score_label="热度",
                n_comments=int(tgt.get("answer_count") or 0), comment_label="回答",
                excerpt=feed_mod.strip_html(tgt.get("excerpt") or ""),
                # target.url 是 api.zhihu.com 的接口地址；给人点的必须是网页地址，
                # 否则 O 打开的是 JSON（这条是探针里对着真实响应逼出来的）
                url="%s/question/%s" % (self.base_url, qid),
                tags=[],
            ))
        posts = [p for p in posts if p.id and p.title]
        return posts, None                  # 热榜就这一页，没有分页

    def detail(self, post, cursor=None):
        """正文栏只放**问题自己**的信息；回答属于"评论"那一栏（见 comments）。

        真人原话：「知乎显示的应该是评论而不是问题的内容」。知乎的一个问题底下只有回答、
        没有"评论"，所以这一页的评论栏就是回答列表。正文栏不再把问题摘要当正文摆着，
        只留一句"回答在下面"或者**没拿到的原因**（401 未登录 / 403 风控是两回事，分开说）。

        **配了 Cookie 就真去试一次**——不然"要登录"永远只是猜测，人也不知道自己贴的 Cookie
        到底管不管用；试的结果照实写。热榜页给的问题摘要不会丢：它不是回答，所以标成"问题摘要"
        放在最后一行，方便看这条问题到底在问什么。
        """
        out = Post.from_dict(post.to_dict())
        body = []
        answers, why = self._answers(str(post.id)) if self.has_auth else ([], "")
        if answers:
            n_all = int(post.n_comments or 0)
            body.append(Block(kind=BLOCK_P, text=(
                "这个问题下有 %s 条回答，下面按知乎的默认排序列出前 %d 条。"
                % (n_all if n_all else "若干", len(answers)))))
        elif not self.has_auth:
            body.append(Block(kind=BLOCK_QUOTE, text=(
                "知乎的回答要登录才给（这个接口回 401）。把浏览器 Cookie 贴进 feed_auth.json 的 "
                "\"zhihu\"，回答就会出现在下面这一栏里。\n\n现在这里只放热榜页给出的问题摘要，"
                "按 O 可以打开原帖看全文。")))
        else:
            body.append(Block(kind=BLOCK_QUOTE, text=(
                "这次没拿到回答：%s\n\n按 R 可以重试；按 O 打开原帖看全文。" % (why or "没给原因"))))
        if out.excerpt and not _zhihu_junk_excerpt(out.excerpt):
            # 标清楚它不是回答：真人不要的正是"拿问题内容冒充正文"
            body.append(Block(kind=BLOCK_QUOTE, text="问题摘要（热榜页给的，不是回答）：%s" % out.excerpt))
        out.body = body
        return out, None

    def _answers(self, qid):
        """问一次这个问题的回答列表。返回 (list[dict], 失败原因)。

        URL 的形状是**拿真 Cookie 试出来的**（2026-10-16：HTTP 200，`data[i].content` 是正文 HTML）。
        同一个问题只成功问一次（详情和评论共用这一份）；**失败只记 RETRY_AFTER 秒**——
        以前失败也缓存一辈子，第一次撞上风控之后这个问题就永远打不开了。
        """
        qid = str(qid)
        hit = self._ans.get(qid)
        if hit is not None:
            items, why, when = hit
            if items or (time.time() - when) < self.RETRY_AFTER:
                return items, why
        url = ("%s/api/v4/questions/%s/answers"
               "?include=data[*].content,excerpt,voteup_count,comment_count,created_time,author.name"
               "&limit=%d&offset=0&order=default" % (self.base_url, qid, self.ANSWERS_LIMIT))
        items, why = [], ""
        try:
            data = self.http.get_json(url, ttl=TTL_DETAIL, what="知乎回答",
                                      headers={"x-requested-with": "fetch"}, timeout=LIST_TIMEOUT)
        except FeedNetError as e:
            why = str(e)
        except Exception as e:
            why = type(e).__name__
        else:
            raw = (data or {}).get("data") if isinstance(data, dict) else None
            if isinstance(raw, list):
                items = [it for it in raw if isinstance(it, dict)]
                if not items:
                    why = "接口回了 200 但一条回答都没有（可能改版了）"
            elif isinstance(raw, dict):
                items = [raw]
            else:
                why = "接口回的形状不认识"
        self._ans[qid] = (items, _zhihu_why(why) if why else "", time.time())
        return self._ans[qid][0], self._ans[qid][1]

    def comments(self, post, cursor=None):
        """这一栏的"评论"就是**回答列表**——真人要的正是它们，能一条条读、能折叠。

        取不到就如实为空（原因写在正文那一栏，不在这里重复）。注意回答作者是 `author.name`
        那一层（评论才是嵌一层的 `author.member.name`，两种形状 `_zhihu_name` 都认）。
        """
        if not self.has_auth:
            return [], None
        answers, _why = self._answers(str(post.id))
        out = []
        for i, it in enumerate(answers):
            # 回答里的图也要落地（Qt 读不了 http，`_localize_images` 会把落不下来的直接丢掉，
            # 所以不会留一排在浏览器里能看、在这儿是裂图的地址）。
            # 只有第一条允许带 2 张：10 条回答的图全下载就是几十个请求、好几兆，
            # 而真正会被读完的通常只有最上面那条；其余的图按"落不下来"处理掉。
            blocks = self._localize_images(
                feed_mod.blocks_from_html(it.get("content") or "", max_blocks=40),
                limit=(2 if i == 0 else 0))
            if not blocks:
                # 有的回答只开放摘要（content 为空、excerpt 有字）：照实说，不留一条空评论
                short = feed_mod.strip_html(it.get("excerpt") or "")
                blocks = [Block(kind=BLOCK_P, text=(short or "（这条回答没给正文，可能是只开放摘要或已删除）"))]
            out.append(Comment(
                cid=str(it.get("id") or ("a%d" % i)),
                author=_zhihu_name(it.get("author")) or "匿名",
                created=feed_mod.human_time(feed_mod.parse_time(it.get("created_time"))),
                score=int(it.get("voteup_count") or 0),
                body=blocks,
            ))
        return out, None


def _zhihu_junk_excerpt(text):
    """热榜页的"摘要"经常是 `[2 张图片]` 这种，不是内容，不该当问题摘要摆出来。"""
    s = str(text or "").strip()
    if len(s) < 6:
        return True
    return bool(re.fullmatch(r"[\[【(（]?\s*\d+\s*张图片\s*[\]】)）]?", s))


def _zhihu_why(why):
    """把接口错误说成人话。401 和 403 是两回事，别混成一句"Cookie 过期了"。

    实测（2026-10-16）：风控时返回的是 403 + 「您当前请求存在异常，暂时限制本次访问」+
    一串十六进制 trace id——**几分钟后就恢复**（同一段代码几分钟前还在正常取 20 条评论）。
    所以那句"常见原因是 Cookie 过期了"是误导：Cookie 好好的，只是被限流了。
    """
    s = str(why or "")
    low = s.lower()
    if "401" in s:
        return "知乎说没登录（401）。feed_auth.json 里 \"zhihu\" 的 Cookie 没了或者不完整。"
    if "403" in s or "风控" in s or "暂时限制" in s:
        return ("知乎在风控限流（403「暂时限制本次访问」）。这**不是** Cookie 过期——"
                "等几分钟再按 R 重试通常就好了。")
    if "412" in s:
        return "知乎把这次请求当成脚本了（412）。等一会儿再试，或者换一次 Cookie。"
    if "超时" in s or "timeout" in low:
        return "请求超时（网络或者对面慢了）：" + s
    return s


def _zhihu_name(author):
    """知乎的"作者"有两种形状：`{"name": ...}`（回答）和 `{"role":..., "member":{"name":...}}`（评论）。

    认不出来就给空字符串——宁可显示"匿名"，也不显示 `{'role': 'normal'}` 这种给人看的乱码。"""
    if not isinstance(author, dict):
        return ""
    if author.get("name"):
        return str(author["name"])
    member = author.get("member")
    if isinstance(member, dict) and member.get("name"):
        return str(member["name"])
    return ""


class WeiboSource(RealSource):
    """微博热搜。**只有"榜单"这一层免登录**——和知乎是同一类情况，但更薄。

    2026-10-16 实测：
      - `weibo.com/ajax/side/hotSearch` **不要 Cookie**：52 条词条，带 `num`（热度）、
        `label_name`（新 / 热 / 爆）、`word_scheme`（能直接拼搜索地址）；
      - 正文和评论都要登录：`m.weibo.cn/api/container/getIndex...` 返回 **HTTP 432**（空体），
        网页版接口要前端签名，`m.weibo.cn` 还要 `X-Requested-With` + 签名参数。
    所以这里是"热榜"形态：一条就是**一个词条**，详情页给的是热度 + 一句实话 + 搜索地址，
    不编造正文和评论。

    **默认关闭**（设计文档里微博属于"单独开关"的那一档）：`feed_sources` 里写
    `["ithome","v2ex","linuxdo","zhihu","weibo"]` 才会出现在来源列表里。
    """
    key = "weibo"
    label = "微博"
    base_url = "https://weibo.com"
    needs_auth = False
    CHANNELS = ("热搜",)
    HOT_URL = "https://weibo.com/ajax/side/hotSearch"

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    def list(self, channel=None, cursor=None):
        data = self._get(self.HOT_URL, TTL_LIST, "微博热搜", timeout=LIST_TIMEOUT)
        items = ((data or {}).get("data") or {}).get("realtime") or []
        now = time.time()
        posts = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            word = feed_mod.strip_html(it.get("word") or "").strip()
            if not word:
                continue
            # 热榜没有"发布时间"——它本来就是此刻的榜单，所以按"现在"参与混流排序，
            # 不然 ts=0 会让它永远沉在混流最后（这是个诚实的选择，不是拍脑袋）。
            tags = []
            label = str(it.get("label_name") or "").strip()
            if label:
                tags.append(label)
            posts.append(Post(
                source=self.key, source_label=self.label, channel="热搜", pid=word,
                title=word, author="",
                created=feed_mod.human_time(now), ts=now,
                score=int(it.get("num") or 0), score_label="热度",
                n_comments=0,                     # 评论数拿不到，就不显示这一项
                excerpt="",                       # note 字段和 word 基本重复，不拿它凑摘要
                url=self._search_url(it.get("word_scheme") or word),
                tags=tags,
            ))
        return posts, None                        # 热搜就这一页

    @staticmethod
    def _search_url(word):
        """热搜词条给人点的地址。用 s.weibo.com 的搜索页：它认话题（#xx#）也认普通词。"""
        return "https://s.weibo.com/weibo?q=" + quote(str(word or ""), safe="")

    def detail(self, post, cursor=None):
        """微博给不了正文：把热度摆出来，直说为什么只有这么点，搜索地址留着给 O。"""
        out = Post.from_dict(post.to_dict())
        body = []
        if out.score:
            body.append(Block(kind=BLOCK_P, text="热度 %s（微博热搜榜）" % feed_mod._num(out.score)))
        note = ("微博的正文和评论要登录才给（移动端接口返回 432、网页接口要前端签名），"
                "这里只给热搜词条。按 O 去微博搜这个词看原帖。")
        if self.has_auth:
            # 不替微博打包票：贴的 Cookie 能不能让正文接口放行，我没有实测过，就不写"能"或"不能"。
            note += "（已经贴了 Cookie，但这一层还没接正文接口——它另外要前端签名，能不能过要单独试。）"
        body.append(Block(kind=BLOCK_QUOTE, text=note))
        out.body = body
        return out, None

    def comments(self, post, cursor=None):
        return [], None                           # 拿不到就是拿不到，不造评论


def _nga_parse(raw):
    """NGA 的 `__output=8` JSON：**必须 `strict=False`**。

    它返回的字符串里有裸换行/制表符（JSON 标准不允许），`json.loads` 默认会
    `Invalid control character` 直接报错——这不是"接口坏了"，是它就是这么发的。
    编码按响应里自称的 `encode` 字段走（实测是 `"encode":"gbk"`，按 utf-8 解会成乱码），
    认不出来就 utf-8 → gbk 逐个试。"""
    if isinstance(raw, (bytes, bytearray)):
        head = bytes(raw[:600]).lower()
        text = None
        if b'"encode"' in head and b"gbk" in head:
            text = raw.decode("gbk", "replace")
        else:
            for enc in ("utf-8", "gbk"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            if text is None:
                text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    try:
        return json.loads(text, strict=False)
    except Exception as e:
        raise FeedNetError("NGA 回的 JSON 读不出来（%s）" % type(e).__name__)


def _nga_rows(data, key):
    """把 `data.__T` / `data.__R` 那种"数字键字典"收成按楼层排好的列表。

    `thread.php` 的 `__T` 是**帖子表**（值是 dict）；`read.php` 的 `__T` 是**主题元信息**
    （平字典，值是字符串）——同一个键名两种含义，所以认不出 dict 就返回空列表，而不是硬解。"""
    node = (data or {}).get("data") if isinstance(data, dict) else None
    block = (node or {}).get(key) if isinstance(node, dict) else None
    if isinstance(block, list):
        items = list(block)
    elif isinstance(block, dict):
        try:
            items = [block[k] for k in sorted(block.keys(), key=lambda x: int(x))]
        except Exception:
            items = list(block.values())
    else:
        items = []
    return [it for it in items if isinstance(it, dict)]


def _nga_op(data):
    """`read.php` 的楼主帖 = `__R` 里 `lou == 0` 的那条（没有就退回第一条）。"""
    rows = _nga_rows(data, "__R")
    for it in rows:
        if int(it.get("lou") or 0) == 0:
            return it
    return rows[0] if rows else {}


def _nga_author(data, it):
    """作者名：先用楼里自带的名字，再查 `__U` 用户表。

    实测（2026-10-16）NGA 的接口**对其他用户只给 `UID:12345678` 这种占位串**，不给真昵称
    （只有楼主帖的 `__T.author` 是真名）。所以这里认不出真名就返回空字符串：
    页面上宁可少显示作者，也不显示 `UID:67020159` 这种给人看的占位串。"""
    for k in ("author", "username"):
        v = it.get(k)
        if isinstance(v, str) and v.strip() and not v.strip().startswith("UID:"):
            return v.strip()
    users = ((data or {}).get("data") or {}).get("__U") if isinstance(data, dict) else None
    node = (users or {}).get(str(it.get("authorid"))) if isinstance(users, dict) else None
    name = str((node or {}).get("username") or "").strip() if isinstance(node, dict) else ""
    return "" if name.startswith("UID:") else name


class NgaSource(RealSource):
    """NGA（艾泽拉斯国家地理）。**免登录完全进不去**，必须贴浏览器 Cookie。

    2026-10-16 实测（本机网络、浏览器 Cookie）：
      - 免登录：`thread.php?fid=-7&__output=8` 返 **403 + GBK 的 `{"error":{"0":"1:未登录"}}`**，
        首页也直接写着游客不能访问；
      - **贴上 Cookie 后列表 200**（49 条）：每条带 `tid / subject / author / postdate / lastpost /
        replies`；
      - **正文**走 `read.php?tid=<tid>&__output=8`：`data.__T` 是**主题元信息**（`subject` / `author` /
        `lastpost`），`data.__R` 是**按楼层**的字典（`lou=0` 是楼主帖，其余是回复），
        每层有 `content`（HTML 夹 BBCode）/ `authorid` / `postdatetimestamp` / `score`；
      - **老帖子会 403**，返回体写着 `5:帖子发布或回复时间超过限制`——**这不是登录问题**，
        是 NGA 自己的规矩（列表第一条恰好就是这种老帖，当时误以为是 Cookie 不对）。
        这种情况按原话报出来，不谎称"要登录"；
      - 回复**没有真正的层级**（引用写在正文里），所以按平铺给。

    解析全部按真响应写（`__T` 的两种含义、`strict=False`、GBK、`__U` 占位名都处理了）。
    """

    key = "nga"
    label = "NGA"
    base_url = "https://bbs.nga.cn"
    needs_auth = True                  # 这个是真的要登录：列表都进不去
    CHANNELS = ("网事杂谈",)
    FID = {"网事杂谈": "-7"}
    MAX_PAGE_BYTES = 4000000

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    def _json(self, url, ttl, what, timeout=None):
        raw = self.http.get_bytes(url, ttl=ttl, max_bytes=self.MAX_PAGE_BYTES, timeout=timeout)
        return _nga_parse(raw)

    @staticmethod
    def _attach_base(data):
        """图片的相对路径前缀（响应里自带，实测 `img.nga.cn/attachments`）。"""
        node = ((data or {}).get("data") or {}).get("__GLOBAL") if isinstance(data, dict) else None
        base = (node or {}).get("_ATTACH_BASE_VIEW") if isinstance(node, dict) else ""
        return str(base or "img.nga.cn/attachments").strip("/")

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.FID else self.CHANNELS[0]
        url = "%s/thread.php?fid=%s&__output=8" % (self.base_url, self.FID[channel])
        data = self._json(url, TTL_LIST, "NGA 列表", timeout=LIST_TIMEOUT)
        posts = []
        for it in _nga_rows(data, "__T"):
            tid = it.get("tid")
            title = feed_mod.strip_html(it.get("subject") or "")
            if not tid or not title:
                continue
            # 列表上的时间是"最后回复"——板块页本来就是"哪里还在动"，这比发布时间更有用
            ts = feed_mod.parse_time(it.get("lastpost") or it.get("postdate"))
            author = feed_mod.strip_html(it.get("author") or "")
            if ts <= 0 or not author:
                # 真夹具抓到的：`thread.php` 的 `__T` 里会混进一条**它自己的错误提示**
                # （`subject` 是"帖子发布或回复时间超过限制"，没有作者也没有时间）。
                # 当帖子收下的话，列表第一条就会变成一句报错——所以这里按"有没有作者和时间"筛掉。
                continue
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=str(tid),
                title=title,
                author=author,
                created=feed_mod.human_time(ts), ts=ts,
                score=0,                       # 列表里没有"赞"这种数，只有回复数
                n_comments=max(0, int(it.get("replies") or 0)), comment_label="回复",
                excerpt="",
                url="%s/read.php?tid=%s" % (self.base_url, tid),
                tags=[str(it.get("type"))] if str(it.get("type") or "") not in ("", "0") else [],
            ))
        posts.sort(key=lambda p: (p.ts or 0), reverse=True)
        return posts, None

    def detail(self, post, cursor=None):
        url = "%s/read.php?tid=%s&__output=8" % (self.base_url, post.id)
        try:
            data = self._json(url, TTL_DETAIL, "NGA 正文")
        except FeedNetError as e:
            if "时间超过限制" in str(e):
                raise FeedNetError("NGA 不让看这条老帖（它自己说的：帖子发布或回复时间超过限制）")
            raise
        out = Post.from_dict(post.to_dict())
        meta = ((data or {}).get("data") or {}).get("__T")
        meta = meta if isinstance(meta, dict) else {}
        first = _nga_op(data)
        if meta.get("subject"):
            out.title = feed_mod.strip_html(meta["subject"])
        name = feed_mod.strip_html(str(meta.get("author") or "")) or _nga_author(data, first)
        if name:
            out.author = name
        ts = feed_mod.parse_time(first.get("postdatetimestamp") or first.get("postdate"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        out.body = self._localize_images(
            _nga_blocks(first.get("content") or "", limit=40, attach_base=self._attach_base(data)),
            limit=8)
        if not out.body:
            out.body = [Block(kind=BLOCK_QUOTE, text="这条帖子的正文没取到（可能是权限或者改版了）。")]
        return out, None

    def comments(self, post, cursor=None):
        url = "%s/read.php?tid=%s&__output=8" % (self.base_url, post.id)
        try:
            data = self._json(url, TTL_LIST, "NGA 回复")
        except FeedNetError:
            return [], None                    # 拿不到就如实为空（老帖 403 是常态）
        out = []
        base = self._attach_base(data)
        for it in _nga_rows(data, "__R"):
            if int(it.get("lou") or 0) == 0:
                continue                       # 楼主帖已经在上面的正文里了
            body = self._localize_images(
                _nga_blocks(it.get("content") or "", attach_base=base), limit=6)
            if not body:
                continue
            ts = feed_mod.parse_time(it.get("postdatetimestamp") or it.get("postdate"))
            out.append(Comment(
                cid=str(it.get("pid") or it.get("lou") or len(out)),
                author=_nga_author(data, it),
                created=feed_mod.human_time(ts),
                score=0,                       # score 这个字段的含义没验证过，不往"赞"上摆
                body=body,
            ))
        return out, None


def _nga_blocks(html, limit=40, attach_base="img.nga.cn/attachments"):
    """NGA 的正文是 HTML 里夹 BBCode（`[quote]` / `[b]` / `[img]` / `[pid=...]`）。

    `[img]` 里的地址常常是**相对路径**（形如 `./mon_202610/16/xxx.jpg`），
    直接扔给解析层就会变成一条没用的文字。真实前缀由响应里的
    `__GLOBAL._ATTACH_BASE_VIEW` 给（实测是 `img.nga.cn/attachments`），
    所以这里先补成完整地址，交给 `_localize_images` 落盘。

    只处理"不处理就会看着别扭"的那几种标记，不做完整 BBCode 实现——
    认不出来的标记原样留着也比猜错强。"""
    text = str(html or "")

    def _img(m):
        src = (m.group(1) or "").strip()
        if src.startswith("./"):
            src = src[2:]
        if src and not src.lower().startswith(("http", "//", "data:")):
            src = "https://%s/%s" % (str(attach_base or "").strip("/"), src.lstrip("/"))
        elif src.startswith("//"):
            src = "https:" + src
        return '<img src="%s"/>' % src

    text = re.sub(r"\[img\](.*?)\[/img\]", _img, text, flags=re.I | re.S)
    text = re.sub(r"\[quote\](.*?)\[/quote\]", r"\1\n", text, flags=re.I | re.S)
    text = re.sub(r"\[/?[a-z=*\u4e00-\u9fff][^\]]{0,40}\]", "", text)   # [b] [/b] [pid=..] [uid=..]
    return feed_mod.blocks_from_html(text, max_blocks=limit)


def _xhs_state(html):
    """从小红书页面里抠出 `window.__INITIAL_STATE__` 并当 JSON 解。

    两处真坑（都是实测踩到的，不是假想）：
    1. **不能用非贪婪正则抠**——对象里第一个 `}<` 出现在字符串里（`"...} <..."`），
       正则会提前截断，实测少 1.5 KB 而且必然解不开。所以按括号配对扫，还得跳过字符串和转义；
    2. 它是 **JS 字面量**，里面有 `undefined` 和 `new Map([])` / `new Set([])` 这种 JSON 不认的写法，
       得先替换掉再当 JSON 解。
    """
    at = str(html or "").find("window.__INITIAL_STATE__")
    if at < 0:
        raise FeedNetError("小红书的页面里没有状态块（改版了，或者返回的是空壳页）")
    at = html.find("{", at)
    depth, i, n, in_str, quote, esc = 0, at, len(html), False, "", False
    text = None
    while 0 <= at < n and i < n:
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                text = html[at:i + 1]
                break
        i += 1
    if not text:
        raise FeedNetError("小红书的页面状态读不出来（括号没配对，多半是改版了）")
    text = re.sub(r"\bundefined\b", "null", text)
    text = re.sub(r"\bnew Map\(\[\]\)", "{}", text)
    text = re.sub(r"\bnew Set\(\[\]\)", "[]", text)
    text = re.sub(r"\bnew Map\(\[.*?\]\)", "{}", text, flags=re.S)
    text = re.sub(r"\bnew Set\(\[.*?\]\)", "[]", text, flags=re.S)
    try:
        return json.loads(text)
    except Exception as e:
        raise FeedNetError("小红书的页面状态解不开（%s）" % type(e).__name__)


def _xhs_text(desc):
    """正文里的 `#话题[话题]#` 标记：去掉标记只留话题名（读得顺，也不会把 `[话题]` 当正文）。"""
    t = re.sub(r"#([^#\[\]]{1,40})\[话题\]#", r"\1", str(desc or ""))
    t = html_escape(t)
    return re.sub(r"\n{2,}", "<br/><br/>", t.replace("\n", "<br/>"))


def _xhs_cover(cover):
    """封面图地址：列表里的 `cover.infoList[0].url`（`url` 字段本身常常是空的）。"""
    if not isinstance(cover, dict):
        return ""
    for k in ("urlDefault", "url"):
        if cover.get(k):
            return str(cover[k])
    for it in (cover.get("infoList") or []):
        if isinstance(it, dict) and it.get("url"):
            return str(it["url"])
    return ""


class XhsSource(RealSource):
    """小红书。**免登录只有空壳**，要贴浏览器 Cookie——这个站把内容服务端渲染在页面里。

    2026-10-16 实测（本机网络、浏览器 Cookie）：
      - 免登录：`/explore` 是一张没有内容的 SPA 壳（36 KB 左右）；
      - **带上 Cookie：`/explore` 228 KB，`window.__INITIAL_STATE__` 里 `feed.feeds` 30+ 条**，
        每条有 `id` / `xsecToken` / `noteCard`（`displayTitle` / `type` / `user.nickName` /
        `interactInfo.likedCount` / `cover.infoList[0].url`）；
      - 详情：`/explore/<id>?xsec_token=<token>&xsec_source=pc_feed` 200，
        `note.noteDetailMap[<id>].note` 里有 `desc`（正文）/ `imageList` / `tagList` /
        `interactInfo`；`time` 是**毫秒**时间戳（不除 1000 会显示成 5 万年后）；
      - **评论只有页面自带的那几条**：评论接口 `/api/sns/web/v2/comment/page` 实测
        **406 `{"code":-1,"success":false}`**（要前端签名），所以不做"滚动加载"，
        页面里带几条就显示几条，一条都没有就如实说没有。
      - 视频笔记跳过（`noteCard.type == "video"`）：这个窗口不放视频，收下来也只是个壳。
    """

    key = "xhs"
    label = "小红书"
    base_url = "https://www.xiaohongshu.com"
    needs_auth = True                  # 免登录拿到的是一张空壳页
    CHANNELS = ("推荐",)
    MAX_PAGE_BYTES = 3000000

    def __init__(self, http, auth=None):
        RealSource.__init__(self, http, auth)
        self._notes = {}               # note id -> (note, comments, 说明)：详情和评论共用一次请求

    def channels(self):
        return list(self.CHANNELS)

    def image_base(self):
        return self.http.cache.img_dir if self.http.cache is not None else ""

    def _page(self, url, ttl, what, timeout=None):
        raw = self.http.get_bytes(url, ttl=ttl, max_bytes=self.MAX_PAGE_BYTES, timeout=timeout)
        if isinstance(raw, (bytes, bytearray)):
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode("utf-8", "replace")
        return str(raw)

    def _note_url(self, nid, token):
        url = "%s/explore/%s" % (self.base_url, nid)
        if token:
            url += "?xsec_token=%s&xsec_source=pc_feed" % quote(str(token), safe="")
        return url

    def list(self, channel=None, cursor=None):
        channel = channel if channel in self.CHANNELS else self.CHANNELS[0]
        html = self._page(self.base_url + "/explore", TTL_LIST, "小红书 推荐", timeout=LIST_TIMEOUT)
        try:
            state = _xhs_state(html)
        except FeedNetError as e:
            # 免登录时拿到的就是一张没有状态块的空壳页——别让人对着"改版了"猜，直接说要 Cookie
            raise FeedNetError("小红书这会儿没给内容（%s）。它要登录：把浏览器 Cookie 贴到 feed_auth.json" % e)
        feeds = ((state.get("feed") or {}).get("feeds") or [])
        posts, videos = [], 0
        for i, it in enumerate(feeds):
            card = it.get("noteCard") if isinstance(it, dict) else None
            if not isinstance(card, dict):
                continue
            if str(card.get("type") or "") == "video":
                videos += 1
                continue
            nid = str(it.get("id") or "")
            title = feed_mod.strip_html(str(card.get("displayTitle") or ""))
            if not nid or not title:
                continue
            user = card.get("user") if isinstance(card.get("user"), dict) else {}
            info = card.get("interactInfo") if isinstance(card.get("interactInfo"), dict) else {}
            try:
                likes = int(str(info.get("likedCount") or "0").replace("+", "") or 0)
            except Exception:
                likes = 0
            cover = _xhs_cover(card.get("cover"))
            posts.append(Post(
                source=self.key, source_label=self.label, channel=channel, pid=nid,
                title=title,
                author=feed_mod.strip_html(str(user.get("nickName") or "")),
                created="", ts=0,            # 列表页不给发布时间，详情页才给
                score=likes, score_label="赞",
                n_comments=0, comment_label="评论",
                cover=self._cover(cover) if (cover and i < COVER_PER_PAGE) else "",
                excerpt="",
                url=self._note_url(nid, it.get("xsecToken")),
            ))
        if not posts:
            if videos:
                raise FeedNetError("小红书这一页全是视频笔记（这个窗口不放视频），没别的可显示")
            raise FeedNetError("小红书这会儿没给内容（多半是要登录：把浏览器 Cookie 贴到 feed_auth.json）")
        return posts, None

    def _note(self, post):
        """取一条笔记的 `note` 和页面自带的评论（同一份状态，详情和评论共用一次请求）。"""
        nid = str(post.id or "")
        if nid in self._notes:
            return self._notes[nid]
        url = post.url or self._note_url(nid, "")
        if "xsec_token" not in url:
            # 没有 token 的详情地址打不开——列表里给过，这里丢了就只能如实说
            self._notes[nid] = ({}, [], "这条的 xsec_token 丢了（它必须带着才能打开详情）")
            return self._notes[nid]
        html = self._page(url, TTL_DETAIL, "小红书 正文")
        state = _xhs_state(html)
        node = ((state.get("note") or {}).get("noteDetailMap") or {}).get(nid) or {}
        note = node.get("note") if isinstance(node, dict) else None
        comments = ((node.get("comments") or {}).get("list") or []) if isinstance(node, dict) else []
        why = ""
        if not isinstance(note, dict) or not note:
            why = "页面状态里没有这条笔记（多半是 xsec_token 过期了——它是有有效期的）"
            note = {}
        self._notes[nid] = (note, comments, why)
        return self._notes[nid]

    def detail(self, post, cursor=None):
        note, _comments, why = self._note(post)
        out = Post.from_dict(post.to_dict())
        if not note:
            out.body = [Block(kind=BLOCK_QUOTE, text=why or "这条笔记的正文没取到。")]
            return out, None
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        info = note.get("interactInfo") if isinstance(note.get("interactInfo"), dict) else {}
        if note.get("title"):
            out.title = feed_mod.strip_html(str(note["title"])) or out.title
        if user.get("nickname"):
            out.author = feed_mod.strip_html(str(user["nickname"]))
        # `feed.parse_time` 自己认毫秒（> 1e11 就除 1000），这里不用再动一次
        ts = feed_mod.parse_time(note.get("time"))
        if ts:
            out.ts, out.created = ts, feed_mod.human_time(ts)
        if info.get("commentCount") not in (None, ""):
            try:
                out.n_comments = max(0, int(str(info["commentCount"]).replace("+", "") or 0))
            except Exception:
                pass
        if str(info.get("likedCount") or "") != "":
            try:
                out.score = max(0, int(str(info["likedCount"]).replace("+", "") or 0))
            except Exception:
                pass
        tags = [str(t.get("name")) for t in (note.get("tagList") or [])
                if isinstance(t, dict) and t.get("name")]
        if tags:
            out.tags = tags[:6]

        parts = []
        if note.get("desc"):
            parts.append("<p>%s</p>" % _xhs_text(note.get("desc")))
        for img in (note.get("imageList") or [])[:9]:
            if isinstance(img, dict) and (img.get("urlDefault") or img.get("url")):
                parts.append('<img src="%s"/>' % (img.get("urlDefault") or img.get("url")))
        body = self._localize_images(feed_mod.blocks_from_html("".join(parts), max_blocks=20), limit=9)
        body.append(Block(kind=BLOCK_QUOTE, text=(
            "这里只有正文和图片：小红书的评论接口要前端签名（实测 406），"
            "页面自带的评论时有时无（录夹具时连看 10 条笔记，一条都没带上），"
            "所以评论这一层没有做——按 O 去原页看评论。")))
        out.body = body
        return out, None

    def comments(self, post, cursor=None):
        # **故意返回空**：评论接口要前端签名（`/api/sns/web/v2/comment/page` 实测
        # 406 `{"code":-1,"success":false}`），页面自带的 `comments.list` 十次里九次是空的
        # （录夹具时连看 10 条笔记，一条都没带上）。既然拿不到真数据，就不写"猜出来的解析"：
        # 宁可明说没有，也不把别人的评论编出来。哪天录到一份真带的，再把这一层打开。
        return [], None


class MixSource(SourceAdapter):
    """混流：把各来源的第一页合起来按时间倒序排，**默认就落在它上面**。

    详情/评论按 `post.source` 转给对应来源——所以在混流里点开一条帖子，正文和评论照样对。
    """

    key = "mix"
    label = "全部来源"
    needs_auth = False

    def __init__(self, sources):
        self.sources = [s for s in (sources or []) if s is not None]
        self.by_key = dict((s.key, s) for s in self.sources)
        self.last_error = ""
        self.errors = []
        self._skip_until = {}       # key -> 到什么时候为止先别碰它（刚失败过）
        self._lock = threading.Lock()
        self.stale = set()          # 这次给了"旧缓存顶上"的来源 key

    def channels(self):
        return ["全部"]

    def image_base(self):
        for s in self.sources:
            base = s.image_base()
            if base:
                return base
        return ""

    def _one(self, src, now, out, idx):
        """取一个来源的第一页。跑在自己的线程里，所以异常必须在这里收干净。"""
        try:
            self._skip_until.pop(src.key, None)
            channel = src.channels()[0] if src.channels() else None
            posts, _nxt = src.list(channel, 0)
            out[idx] = posts
            if getattr(src, "served_stale", False):
                # 这一家这次给的是"断网时顶上的旧缓存"：记下来，让页脚能说清
                with self._lock:
                    self.stale.add(src.key)
        except FeedNetError as e:
            with self._lock:
                self._skip_until[src.key] = time.time() + FAIL_COOLDOWN
            out[idx] = ("err", "%s：%s" % (src.label, e))
        except Exception as e:
            with self._lock:
                self._skip_until[src.key] = time.time() + FAIL_COOLDOWN
            out[idx] = ("err", "%s：%s" % (src.label, type(e).__name__))

    def list(self, channel=None, cursor=None):
        """并行问所有来源，再合页。

        为什么并行：V2EX 在本机网络根本连不上，串行的话整页要等它一个超时（10 秒）才出内容。
        并行之后最慢的那个决定等待时间，而且**它失败过就会被跳过 60 秒**，不会再挡路。"""
        page = int(cursor or 0)
        self.errors = []
        self.stale = set()
        now = time.time()
        todo = [s for s in self.sources if self._skip_until.get(s.key, 0) <= now]
        for s in self.sources:
            if s not in todo:
                self.errors.append("%s：刚才没连上，先跳过" % s.label)
        out = [None] * len(todo)
        if todo:
            threads = []
            for i, src in enumerate(todo):
                t = threading.Thread(target=self._one, args=(src, now, out, i), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=max(LIST_TIMEOUT * 2, 8))
        lists = []
        for item in out:
            if isinstance(item, tuple):
                self.errors.append(item[1])
            elif item:
                lists.append(item)
        self.last_error = "；".join(self.errors)
        merged = feed_mod.merge_posts(lists)
        # 每个来源的每页席位：来源越多席位越少，只有一个来源时不设限（那就不叫混流了）
        live = len(set(p.source for p in merged)) or 1
        cap = 0 if live <= 1 else max(2, PAGE_MIX // live)
        pages = fair_pages(merged, PAGE_MIX, cap)
        if page < 0 or page >= len(pages):
            return [], None
        nxt = page + 1 if page + 1 < len(pages) else None
        return pages[page], nxt

    def _route(self, post):
        src = self.by_key.get(getattr(post, "source", ""))
        if src is None:
            raise FeedNetError("这条帖子来自 %s，但该来源没启用" % (getattr(post, "source", "") or "未知来源"))
        return src

    def detail(self, post, cursor=None):
        return self._route(post).detail(post, cursor)

    def comments(self, post, cursor=None):
        return self._route(post).comments(post, cursor)


def build_real_sources(cache_root=None, config=None, log=None):
    """按配置建好来源。顺序就是界面里 `[`/`]` 的顺序：混流打头，然后 IT之家 / V2EX /
    linux.do / 知乎 / NGA（顺序由下面这张表定，不跟着配置串的顺序走，这样按键顺序永远可预期）。"""
    config = config or {}
    cache = feed_net.Cache(cache_root) if cache_root else None
    auth = load_auth(os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_auth.json"))
    enabled = config.get("feed_sources", None)
    if enabled is None:
        # 配置里没写过 -> 默认这几个；写成空列表 = 用户把它们全关了（只剩离线示例）。
        # 微博不在默认里：它的风控最紧，属于"单独开关"那一档。
        # NGA 和 小红书 也在默认里：它们确实要 Cookie，但没配的时候页脚会照实说
        # "这个来源要 Cookie"，不会假装成功——比"藏起来让人找不到"更好解释。
        enabled = ["ithome", "v2ex", "linuxdo", "zhihu", "nga", "xhs"]
    elif not isinstance(enabled, list):
        enabled = [str(enabled)]

    made = []
    for key, cls in (("ithome", IthomeSource), ("v2ex", V2exSource),
                     ("linuxdo", DiscourseSource), ("zhihu", ZhihuSource),
                     ("weibo", WeiboSource), ("nga", NgaSource), ("xhs", XhsSource)):
        if key not in enabled:
            continue
        cred = auth.get(key) if isinstance(auth.get(key), dict) else {}
        http = Http(cache=cache,
                    cookies=_cookies_from(cred.get("cookie")),
                    user_agent=cred.get("user_agent") or feed_net.UA,
                    headers={"Referer": cls.base_url + "/"})
        src = cls(http, cred)
        src.needs_auth = bool(getattr(cls, "needs_auth", False))
        made.append(src)

    if not made:
        return []
    return [MixSource(made)] + made


def _cookies_from(text):
    """把浏览器里复制来的 "a=1; b=2" 解析成 dict。"""
    out = {}
    for part in str(text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out
